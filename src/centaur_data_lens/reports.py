# ruff: noqa: E501
from __future__ import annotations

import base64
import hashlib
import json
import webbrowser
from pathlib import Path
from typing import Literal

from centaur_data_lens.models import PrivacySnapshot
from centaur_data_lens.security import markdown_text, safe_embedded_json, secure_write_text

ReportFormat = Literal["html", "markdown", "json"]

_STYLE = """
:root{color-scheme:light;font-family:Inter,ui-sans-serif,system-ui,sans-serif;background:#f8fafc;color:#0f172a}
*{box-sizing:border-box}body{margin:0}.shell{max-width:1100px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:2rem;margin:0 0 4px}.lede,.muted{color:#475569}.notice{background:#eef2ff;border:1px solid #c7d2fe;border-radius:12px;padding:14px;margin:20px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin:20px 0}.card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:18px}
.metric{font-size:1.8rem;font-weight:700}.section{margin-top:30px}table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e2e8f0}
th,td{text-align:left;padding:10px;border-bottom:1px solid #e2e8f0;vertical-align:top}th{background:#f1f5f9}
input{width:100%;padding:10px;border:1px solid #94a3b8;border-radius:8px;margin:8px 0 12px}
.pill{display:inline-block;background:#e0e7ff;color:#3730a3;border-radius:999px;padding:3px 9px;margin:2px;font-size:.8rem}
.hidden{display:none}.source{font-family:ui-monospace,SFMono-Regular,monospace;font-size:.78rem;overflow-wrap:anywhere;color:#64748b}
""".strip()

_SCRIPT = """
"use strict";
const data=JSON.parse(document.getElementById("report-data").textContent);
const byId=(id)=>document.getElementById(id);
const node=(tag,text,cls)=>{const el=document.createElement(tag);if(text!==undefined)el.textContent=String(text);if(cls)el.className=cls;return el;};
const add=(parent,child)=>{parent.appendChild(child);return child;};
byId("generated").textContent=new Date(data.generated_at).toLocaleString();
byId("total").textContent=data.total_records.toLocaleString();
byId("platforms").textContent=data.platforms.join(", ");
const coverage=byId("coverage-body");
for(const row of data.coverage){const tr=add(coverage,node("tr"));for(const value of [row.platform,row.category,row.record_count,row.earliest||"—",row.latest||"—"])add(tr,node("td",value));}
const overlaps=byId("overlaps");
if(data.overlapping_hostnames.length===0)add(overlaps,node("span","No hostname overlap was observed in supported categories.","muted"));
for(const host of data.overlapping_hostnames)add(overlaps,node("span",host,"pill"));
const deviceOverlaps=byId("device-overlaps");
if(data.overlapping_devices.length===0)add(deviceOverlaps,node("span","No device overlap was observed.","muted"));
for(const device of data.overlapping_devices)add(deviceOverlaps,node("span",device,"pill"));
const serviceOverlaps=byId("service-overlaps");
if(data.overlapping_services.length===0)add(serviceOverlaps,node("span","No service overlap was observed.","muted"));
for(const service of data.overlapping_services)add(serviceOverlaps,node("span",service,"pill"));
const omissions=byId("omissions");
for(const [platform,items] of Object.entries(data.omissions)){add(omissions,node("h3",platform));const ul=add(omissions,node("ul"));for(const item of items)add(ul,node("li",item));}
const evidenceBody=byId("evidence-body");
const renderEvidence=(query)=>{
  evidenceBody.replaceChildren();
  const normalized=query.trim().toLowerCase();
  for(const item of data.evidence){
    const haystack=[item.platform,item.category,item.title,item.source].join(" ").toLowerCase();
    if(normalized&&!haystack.includes(normalized))continue;
    const tr=add(evidenceBody,node("tr"));
    add(tr,node("td",item.platform));
    add(tr,node("td",item.category));
    add(tr,node("td",item.title));
    add(tr,node("td",item.timestamp||"—"));
    add(tr,node("td",item.source,"source"));
  }
};
renderEvidence("");
byId("evidence-filter").addEventListener("input",(event)=>renderEvidence(event.target.value));
""".strip()


def _sha256_csp(content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def render_html(snapshot: PrivacySnapshot) -> str:
    payload = safe_embedded_json(snapshot.model_dump(mode="json"))
    csp = (
        "default-src 'none'; "
        f"script-src 'sha256-{_sha256_csp(_SCRIPT)}'; "
        f"style-src 'sha256-{_sha256_csp(_STYLE)}'; "
        "connect-src 'none'; img-src 'none'; font-src 'none'; object-src 'none'; "
        "frame-src 'none'; base-uri 'none'; form-action 'none'"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<title>Centaur Data Lens privacy report</title>
<style>{_STYLE}</style>
</head>
<body>
<main class="shell">
  <h1>Centaur Data Lens</h1>
  <p class="lede">Offline privacy snapshot generated <span id="generated"></span></p>
  <div class="notice">This report covers only supported records disclosed in the exports you
  selected. It is not a complete account of everything a platform may know.</div>
  <section class="grid">
    <div class="card"><div class="metric" id="total"></div><div class="muted">normalized records</div></div>
    <div class="card"><div class="metric" id="platforms"></div><div class="muted">platforms</div></div>
  </section>
  <section class="section"><h2>Coverage</h2>
    <table><thead><tr><th>Platform</th><th>Category</th><th>Records</th><th>Earliest</th><th>Latest</th></tr></thead>
    <tbody id="coverage-body"></tbody></table>
  </section>
  <section class="section"><h2>Cross-platform hostname overlap</h2><div id="overlaps"></div></section>
  <section class="section"><h2>Cross-platform device overlap</h2><div id="device-overlaps"></div></section>
  <section class="section"><h2>Cross-platform service overlap</h2><div id="service-overlaps"></div></section>
  <section class="section"><h2>Coverage omissions</h2><div id="omissions"></div></section>
  <section class="section"><h2>Cited evidence</h2>
    <label for="evidence-filter">Filter evidence</label>
    <input id="evidence-filter" type="search" autocomplete="off" placeholder="Platform, category, title, or source">
    <table><thead><tr><th>Platform</th><th>Category</th><th>Observation</th><th>Time</th><th>Source</th></tr></thead>
    <tbody id="evidence-body"></tbody></table>
  </section>
</main>
<script id="report-data" type="application/json">{payload}</script>
<script>{_SCRIPT}</script>
</body>
</html>
"""


def render_markdown(snapshot: PrivacySnapshot) -> str:
    lines = [
        "# Centaur Data Lens privacy report",
        "",
        f"Generated: {snapshot.generated_at.isoformat()}",
        "",
        "> This report covers only supported records disclosed in the selected exports.",
        "",
        f"- Platforms: {', '.join(markdown_text(item) for item in snapshot.platforms)}",
        f"- Normalized records: {snapshot.total_records:,}",
        "",
        "## Coverage",
        "",
        "| Platform | Category | Records | Earliest | Latest |",
        "|---|---|---:|---|---|",
    ]
    for coverage_item in snapshot.coverage:
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_text(coverage_item.platform),
                    markdown_text(coverage_item.category),
                    str(coverage_item.record_count),
                    coverage_item.earliest.isoformat() if coverage_item.earliest else "—",
                    coverage_item.latest.isoformat() if coverage_item.latest else "—",
                ]
            )
            + " |"
        )
    lines.extend(["", "## Cross-platform hostname overlap", ""])
    lines.extend(f"- {markdown_text(hostname)}" for hostname in snapshot.overlapping_hostnames)
    if not snapshot.overlapping_hostnames:
        lines.append("- No overlap observed in supported categories.")
    lines.extend(["", "### Devices", ""])
    lines.extend(f"- {markdown_text(device)}" for device in snapshot.overlapping_devices)
    if not snapshot.overlapping_devices:
        lines.append("- No device overlap observed.")
    lines.extend(["", "### Services", ""])
    lines.extend(f"- {markdown_text(service)}" for service in snapshot.overlapping_services)
    if not snapshot.overlapping_services:
        lines.append("- No service overlap observed.")
    lines.extend(["", "## Coverage omissions", ""])
    for platform, omissions in snapshot.omissions.items():
        lines.append(f"### {markdown_text(platform)}")
        lines.extend(f"- {markdown_text(item)}" for item in omissions)
        lines.append("")
    lines.extend(["## Cited evidence", ""])
    for evidence_item in snapshot.evidence:
        when = (
            evidence_item.timestamp.isoformat() if evidence_item.timestamp else "time unavailable"
        )
        lines.append(
            f"- **{markdown_text(evidence_item.platform)}/"
            f"{markdown_text(evidence_item.category)}** — "
            f"{markdown_text(evidence_item.title)} ({when})  \n"
            f"  Source: {markdown_text(evidence_item.source)}"
        )
    return "\n".join(lines).rstrip() + "\n"


def render_report(snapshot: PrivacySnapshot, report_format: ReportFormat) -> str:
    if report_format == "html":
        return render_html(snapshot)
    if report_format == "markdown":
        return render_markdown(snapshot)
    return json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"


def write_report(
    snapshot: PrivacySnapshot,
    *,
    report_format: ReportFormat,
    output: Path,
    overwrite: bool = False,
    open_report: bool = False,
) -> None:
    if open_report and report_format != "html":
        raise ValueError("Only HTML reports can be opened in a browser.")
    secure_write_text(output, render_report(snapshot, report_format), overwrite=overwrite)
    if open_report:
        webbrowser.open(output.expanduser().resolve().as_uri(), new=2)
