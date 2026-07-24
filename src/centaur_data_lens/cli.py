from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import questionary
import typer
from questionary import Choice
from rich.console import Console
from rich.table import Table
from rich.text import Text

from centaur_data_lens import __version__
from centaur_data_lens.ai import (
    ModelAdapter,
    answer_question,
    create_adapter,
    prepare_question,
)
from centaur_data_lens.analysis import (
    AnalysisSession,
    SourceSpec,
    analyze_sources,
    parse_source_values,
)
from centaur_data_lens.errors import DataLensError, ModelAdapterError
from centaur_data_lens.platforms import get_platform, list_platforms
from centaur_data_lens.reports import write_report
from centaur_data_lens.security import sanitize_terminal, secure_write_text

console = Console()
error_console = Console(stderr=True)

app = typer.Typer(
    add_completion=True,
    no_args_is_help=False,
    help="Explore personal data disclosed in platform exports, locally.",
)


class ReportFormatOption(StrEnum):
    HTML = "html"
    MARKDOWN = "markdown"
    JSON = "json"


def _safe_print(value: object, *, style: str | None = None) -> None:
    text = Text(sanitize_terminal(value))
    if style:
        text.stylize(style)
    console.print(text)


def _fail(exc: DataLensError) -> None:
    error_console.print(Text(f"Error: {sanitize_terminal(exc)}", style="bold red"))
    raise typer.Exit(2)


def _progress(message: str) -> None:
    _safe_print(message, style="dim")


def _print_banner() -> None:
    console.print(Text("Centaur Data Lens", style="bold indigo"))
    _safe_print("Explore the personal data disclosed in your platform exports.")
    console.print()
    for promise in (
        "✓ Runs locally",
        "✓ No Centaur account",
        "✓ No telemetry",
        "✓ Nothing is uploaded without explicit approval",
    ):
        _safe_print(promise, style="green")
    console.print()


def _print_guide(platform_id: str) -> None:
    parser = get_platform(platform_id)
    definition = parser.definition
    console.print(Text(definition.display_name, style="bold"))
    _safe_print(f"Official export page: {definition.official_url}")
    _safe_print(f"Instructions last verified: {definition.last_verified}", style="dim")
    console.print()
    for index, step in enumerate(definition.guide, 1):
        _safe_print(f"{index}. {step}")
    console.print()
    _safe_print("Supported in this release:", style="bold")
    for item in definition.supported:
        _safe_print(f"  • {item}")
    _safe_print("Intentionally excluded:", style="bold")
    for item in definition.excluded:
        _safe_print(f"  • {item}")


def _print_snapshot(session: AnalysisSession) -> None:
    snapshot = session.snapshot()
    console.print()
    console.print(Text("Privacy snapshot", style="bold"))
    _safe_print(f"Platforms: {', '.join(snapshot.platforms)}")
    _safe_print(f"Normalized records: {snapshot.total_records:,}")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Platform")
    table.add_column("Category")
    table.add_column("Records", justify="right")
    table.add_column("Earliest")
    table.add_column("Latest")
    for item in snapshot.coverage:
        table.add_row(
            item.platform,
            item.category,
            f"{item.record_count:,}",
            item.earliest.date().isoformat() if item.earliest else "—",
            item.latest.date().isoformat() if item.latest else "—",
        )
    console.print(table)
    if len(snapshot.platforms) > 1:
        console.print(Text("Cross-platform hostname overlap", style="bold"))
        if snapshot.overlapping_hostnames:
            _safe_print(", ".join(snapshot.overlapping_hostnames[:20]))
        else:
            _safe_print("No overlap observed in supported categories.", style="dim")


def _print_preview(adapter: ModelAdapter, session: AnalysisSession, question: str) -> None:
    preview, _, _ = prepare_question(session, question, adapter)
    console.print(Text("Transmission preview", style="bold yellow"))
    _safe_print(f"Provider: {preview.provider}")
    _safe_print(f"Model: {preview.model}")
    _safe_print(f"Destination: {preview.destination}")
    _safe_print(f"Selected records: {preview.record_count}")
    _safe_print(f"Payload: {preview.payload_bytes:,} bytes")
    _safe_print(f"Categories: {', '.join(preview.categories)}")
    _safe_print("Excluded: the complete archive, media, unsupported categories, and API key")


def _ask_once(
    session: AnalysisSession,
    *,
    question: str,
    provider: str,
    model: str | None,
    endpoint: str | None,
    allow_cloud: bool,
) -> None:
    adapter = create_adapter(
        provider,
        model=model,
        endpoint=endpoint,
        prompt_for_key=True,
    )
    _print_preview(adapter, session, question)
    confirmed = allow_cloud or adapter.is_local
    if not confirmed:
        confirmed = bool(
            questionary.confirm(
                "Send only this selected context to the destination above?",
                default=False,
            ).ask()
        )
    if not confirmed:
        _safe_print("Cloud request cancelled.", style="yellow")
        return
    _, answer = answer_question(
        session,
        question=question,
        adapter=adapter,
        allow_cloud=confirmed,
    )
    console.print()
    _safe_print(answer.answer)


def _select_paths(platform_id: str) -> list[SourceSpec]:
    specs: list[SourceSpec] = []
    while True:
        raw_path = questionary.path(
            f"Path to a {platform_id} ZIP or extracted directory:",
            only_directories=False,
        ).ask()
        if not raw_path:
            break
        path = Path(raw_path).expanduser()
        if not path.exists():
            _safe_print("That path does not exist.", style="red")
            continue
        specs.append(SourceSpec(platform=platform_id, path=path))
        if not questionary.confirm("Add another ZIP part or directory?", default=False).ask():
            break
    return specs


def _wizard_ask(session: AnalysisSession) -> None:
    provider = questionary.select(
        "Choose an analysis provider:",
        choices=[
            Choice("Local Ollama", "ollama"),
            Choice("My OpenAI API key", "openai"),
            Choice("My Anthropic API key", "anthropic"),
            Choice("My Gemini API key", "gemini"),
            Choice("Back", "back"),
        ],
    ).ask()
    if not provider or provider == "back":
        return
    question = questionary.text("What would you like to know?").ask()
    if not question:
        return
    _ask_once(
        session,
        question=question,
        provider=provider,
        model=None,
        endpoint=None,
        allow_cloud=False,
    )


def _wizard_save(session: AnalysisSession) -> None:
    selected = questionary.select(
        "Report format:",
        choices=[
            Choice("Offline HTML dashboard", "html"),
            Choice("Markdown", "markdown"),
            Choice("JSON", "json"),
        ],
    ).ask()
    if not selected:
        return
    output_value = questionary.path("Save report to:").ask()
    if not output_value:
        return
    _safe_print(
        "Warning: this report contains sensitive derived data and cited evidence.",
        style="bold yellow",
    )
    if not questionary.confirm("Save the report?", default=False).ask():
        return
    output = Path(output_value).expanduser()
    overwrite = output.exists() and bool(
        questionary.confirm("The file exists. Replace it securely?", default=False).ask()
    )
    write_report(
        session.snapshot(),
        report_format=selected,
        output=output,
        overwrite=overwrite,
    )
    _safe_print(f"Saved report to {output.resolve()}", style="green")


def run_wizard() -> None:
    _print_banner()
    with AnalysisSession() as session:
        has_data = False
        while True:
            parser = questionary.select(
                "Choose a platform:",
                choices=[
                    Choice(item.definition.display_name, item.definition.platform_id)
                    for item in list_platforms()
                ]
                + [Choice("Exit", "exit")],
            ).ask()
            if not parser or parser == "exit":
                return
            action = questionary.select(
                f"Do you already have a {get_platform(parser).definition.display_name} export?",
                choices=[
                    Choice("Analyze an export", "analyze"),
                    Choice("Show me how to request one", "guide"),
                    Choice("Back", "back"),
                ],
            ).ask()
            if action == "guide":
                _print_guide(parser)
                continue
            if action != "analyze":
                continue
            specs = _select_paths(parser)
            if not specs:
                continue
            analyze_sources(session, specs, progress=_progress)
            has_data = True
            _print_snapshot(session)

            while has_data:
                choices: list[Choice] = [
                    Choice("View privacy snapshot", "snapshot"),
                    Choice("Review coverage and omissions", "snapshot"),
                    Choice("Ask questions", "ask"),
                    Choice("Add another platform", "add"),
                    Choice("Export a report", "save"),
                    Choice("Exit and delete temporary analysis", "exit"),
                ]
                if len(session.snapshot().platforms) > 1:
                    choices.insert(2, Choice("Compare platforms", "snapshot"))
                next_action = questionary.select(
                    "What would you like to do?", choices=choices
                ).ask()
                if next_action == "snapshot":
                    _print_snapshot(session)
                elif next_action == "ask":
                    _wizard_ask(session)
                elif next_action == "save":
                    _wizard_save(session)
                elif next_action == "add":
                    break
                else:
                    return


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        try:
            run_wizard()
        except (KeyboardInterrupt, EOFError):
            _safe_print("Cancelled. Temporary analysis was deleted.", style="yellow")
        except DataLensError as exc:
            _fail(exc)


@app.command("platforms")
def platforms_command() -> None:
    """List platforms supported by this release."""
    for parser in list_platforms():
        _safe_print(f"{parser.definition.platform_id}: {parser.definition.display_name}")


@app.command("guide")
def guide_command(platform: str) -> None:
    """Show local instructions for requesting a supported export."""
    try:
        _print_guide(platform)
    except DataLensError as exc:
        _fail(exc)


@app.command("inspect")
def inspect_command(
    platform: str,
    exports: Annotated[list[Path], typer.Argument(help="ZIP files or extracted directories")],
    allow_large_archive: Annotated[bool, typer.Option("--allow-large-archive")] = False,
) -> None:
    """Validate and summarize a user-selected platform export."""
    try:
        specs = [SourceSpec(platform=platform.lower(), path=path) for path in exports]
        get_platform(platform)
        with AnalysisSession() as session:
            analyze_sources(
                session,
                specs,
                allow_large_archive=allow_large_archive,
                progress=_progress,
            )
            _print_snapshot(session)
    except DataLensError as exc:
        _fail(exc)


@app.command("report")
def report_command(
    source: Annotated[
        list[str], typer.Option("--source", help="Repeat PLATFORM=PATH for each export")
    ],
    output: Annotated[Path, typer.Option("--output", help="Destination report path")],
    report_format: Annotated[
        ReportFormatOption, typer.Option("--format")
    ] = ReportFormatOption.HTML,
    overwrite: Annotated[bool, typer.Option("--overwrite")] = False,
    open_report: Annotated[bool, typer.Option("--open")] = False,
    allow_large_archive: Annotated[bool, typer.Option("--allow-large-archive")] = False,
) -> None:
    """Create an offline report from one or more explicitly labelled sources."""
    try:
        specs = parse_source_values(source)
        with AnalysisSession() as session:
            analyze_sources(
                session,
                specs,
                allow_large_archive=allow_large_archive,
                progress=_progress,
            )
            write_report(
                session.snapshot(),
                report_format=report_format.value,
                output=output,
                overwrite=overwrite,
                open_report=open_report,
            )
        _safe_print(f"Saved report to {output.expanduser().resolve()}", style="green")
    except (DataLensError, ValueError) as exc:
        _fail(DataLensError(str(exc)))


@app.command("ask")
def ask_command(
    source: Annotated[list[str], typer.Option("--source", help="PLATFORM=PATH")],
    provider: Annotated[str, typer.Option("--provider")],
    question: Annotated[str | None, typer.Option("--question")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    endpoint: Annotated[str | None, typer.Option("--endpoint")] = None,
    allow_cloud: Annotated[bool, typer.Option("--allow-cloud")] = False,
) -> None:
    """Ask one cited question using a local model or your own API key."""
    try:
        specs = parse_source_values(source)
        actual_question = question or questionary.text("What would you like to know?").ask()
        if not actual_question:
            raise ModelAdapterError("A question is required.")
        with AnalysisSession() as session:
            analyze_sources(session, specs, progress=_progress)
            _ask_once(
                session,
                question=actual_question,
                provider=provider,
                model=model,
                endpoint=endpoint,
                allow_cloud=allow_cloud,
            )
    except DataLensError as exc:
        _fail(exc)


@app.command("diagnostics")
def diagnostics_command(
    platform: str,
    exports: Annotated[list[Path], typer.Argument()],
    output: Annotated[Path, typer.Option("--output")],
    overwrite: Annotated[bool, typer.Option("--overwrite")] = False,
) -> None:
    """Write value-free parser counts suitable for a redacted bug report."""
    try:
        get_platform(platform)
        specs = [SourceSpec(platform=platform.lower(), path=path) for path in exports]
        with AnalysisSession() as session:
            analyze_sources(session, specs, progress=_progress)
            content = json.dumps(session.diagnostics(), indent=2, sort_keys=True) + "\n"
            secure_write_text(output, content, overwrite=overwrite)
        _safe_print(f"Saved redacted diagnostics to {output.expanduser().resolve()}", style="green")
    except DataLensError as exc:
        _fail(exc)


@app.command("version")
def version_command() -> None:
    _safe_print(__version__)


def main() -> None:
    app()
