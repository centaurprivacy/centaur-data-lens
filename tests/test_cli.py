from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import centaur_data_lens.cli as cli
from centaur_data_lens.ai import TransmissionPreview
from centaur_data_lens.analysis import AnalysisSession, SourceSpec, analyze_sources
from centaur_data_lens.cli import app
from centaur_data_lens.models import AIAnswer, AIClaim, AIClaimKind

runner = CliRunner()


def test_platforms_and_guides() -> None:
    platforms = runner.invoke(app, ["platforms"])
    assert platforms.exit_code == 0
    assert "google" in platforms.stdout
    assert "meta" in platforms.stdout

    guide = runner.invoke(app, ["guide", "google"])
    assert guide.exit_code == 0
    assert "Deselect all" in guide.stdout
    assert "last verified" in guide.stdout


def test_direct_report(google_export: Path, tmp_path: Path) -> None:
    output = tmp_path / "report.html"
    result = runner.invoke(
        app,
        [
            "report",
            "--source",
            f"google={google_export}",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert output.exists()
    assert "Saved report" in result.stdout


def test_platform_mismatch_is_safe(meta_export: Path) -> None:
    result = runner.invoke(app, ["inspect", "google", str(meta_export)])
    assert result.exit_code == 2
    assert "No supported Google JSON categories" in result.output
    assert "Traceback" not in result.output


def test_inspect_diagnostics_version_and_invalid_source(
    google_export: Path, tmp_path: Path
) -> None:
    inspected = runner.invoke(app, ["inspect", "google", str(google_export)])
    assert inspected.exit_code == 0
    assert "Privacy snapshot" in inspected.stdout

    diagnostics = tmp_path / "diagnostics.json"
    result = runner.invoke(
        app,
        [
            "diagnostics",
            "google",
            str(google_export),
            "--output",
            str(diagnostics),
        ],
    )
    assert result.exit_code == 0
    assert '"values_included": false' in diagnostics.read_text(encoding="utf-8")
    assert runner.invoke(app, ["version"]).exit_code == 0

    invalid = runner.invoke(
        app,
        ["report", "--source", "bad-source", "--output", str(tmp_path / "x.html")],
    )
    assert invalid.exit_code == 2
    assert "PLATFORM=PATH" in invalid.output


class _Prompt:
    def __init__(self, value: object) -> None:
        self.value = value

    def ask(self) -> object:
        return self.value


def test_wizard_exit_and_analysis(monkeypatch, google_export: Path) -> None:
    answers = iter(["exit"])
    monkeypatch.setattr(
        cli.questionary,
        "select",
        lambda *args, **kwargs: _Prompt(next(answers)),
    )
    cli.run_wizard()


def test_wizard_ask_and_save_helpers(monkeypatch, google_export: Path, tmp_path: Path) -> None:
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])

        selections = iter(["ollama"])
        texts = iter(["What was searched?"])
        monkeypatch.setattr(
            cli.questionary,
            "select",
            lambda *args, **kwargs: _Prompt(next(selections)),
        )
        monkeypatch.setattr(
            cli.questionary,
            "text",
            lambda *args, **kwargs: _Prompt(next(texts)),
        )
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(cli, "_ask_once", lambda *args, **kwargs: calls.append(kwargs))
        cli._wizard_ask(session)
        assert calls[0]["provider"] == "ollama"

        output = tmp_path / "wizard-report.html"
        selections = iter(["html"])
        monkeypatch.setattr(
            cli.questionary,
            "select",
            lambda *args, **kwargs: _Prompt(next(selections)),
        )
        monkeypatch.setattr(
            cli.questionary,
            "path",
            lambda *args, **kwargs: _Prompt(str(output)),
        )
        monkeypatch.setattr(
            cli.questionary,
            "confirm",
            lambda *args, **kwargs: _Prompt(True),
        )
        cli._wizard_save(session)
        assert output.exists()


def test_wizard_routes_snapshot_and_coverage_to_distinct_views(
    monkeypatch: pytest.MonkeyPatch,
    google_export: Path,
) -> None:
    answers = iter(["google", "analyze", "coverage", "snapshot", "exit"])
    monkeypatch.setattr(
        cli.questionary,
        "select",
        lambda *args, **kwargs: _Prompt(next(answers)),
    )
    monkeypatch.setattr(
        cli,
        "_select_paths",
        lambda platform: [SourceSpec(platform, google_export)],
    )
    calls = {"snapshot": 0, "coverage": 0}
    monkeypatch.setattr(
        cli,
        "_print_snapshot",
        lambda session: calls.__setitem__("snapshot", calls["snapshot"] + 1),
    )
    monkeypatch.setattr(
        cli,
        "_print_coverage_and_omissions",
        lambda session: calls.__setitem__("coverage", calls["coverage"] + 1),
    )

    cli.run_wizard()

    assert calls == {"snapshot": 2, "coverage": 1}


def test_coverage_view_includes_excluded_categories(
    google_export: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        cli._print_coverage_and_omissions(session)

    output = capsys.readouterr().out
    assert "Coverage and omissions" in output
    assert "account_activity" in output
    assert "Gmail" in output


def test_ask_once_prints_every_citation_before_long_claim_text(
    monkeypatch: pytest.MonkeyPatch,
    google_export: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class LocalAdapter:
        name = "local-test"
        model = "test"
        destination = "http://127.0.0.1:1234"
        is_local = True

        def complete(self, *, system: str, user: str) -> str:
            raise AssertionError("mock answer_question should be used")

    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        monkeypatch.setattr(cli, "create_adapter", lambda *args, **kwargs: LocalAdapter())
        monkeypatch.setattr(
            cli,
            "answer_question",
            lambda *args, **kwargs: (
                TransmissionPreview(
                    provider="local-test",
                    model="test",
                    destination="http://127.0.0.1:1234",
                    record_count=1,
                    payload_bytes=10,
                    categories=("account_activity",),
                ),
                AIAnswer(
                    claims=[
                        AIClaim(
                            text="x" * 3_000,
                            kind=AIClaimKind.INFERENCE,
                            record_ids=["first-record"],
                        ),
                        AIClaim(
                            text="Second claim",
                            kind=AIClaimKind.OBSERVED,
                            record_ids=["second-record"],
                        ),
                    ],
                ),
            ),
        )
        cli._ask_once(
            session,
            question="privacy",
            provider="ollama",
            model=None,
            endpoint=None,
            allow_cloud=False,
        )
    output = capsys.readouterr().out
    lines = output.splitlines()
    first_citation_line = next(index for index, line in enumerate(lines) if "first-record" in line)
    first_text_line = next(index for index, line in enumerate(lines) if set(line) == {"x"})
    assert first_citation_line < first_text_line
    assert output.count("x") >= 3_000
    assert output.index("second-record") < output.index("Second claim")


def test_select_paths_uses_explicit_platform(monkeypatch, google_export: Path) -> None:
    monkeypatch.setattr(
        cli.questionary,
        "path",
        lambda *args, **kwargs: _Prompt(str(google_export)),
    )
    monkeypatch.setattr(
        cli.questionary,
        "confirm",
        lambda *args, **kwargs: _Prompt(False),
    )
    specs = cli._select_paths("google")
    assert specs == [SourceSpec("google", google_export)]

    answers = iter(["google", "analyze", "exit"])
    monkeypatch.setattr(
        cli.questionary,
        "select",
        lambda *args, **kwargs: _Prompt(next(answers)),
    )
    monkeypatch.setattr(
        cli,
        "_select_paths",
        lambda platform: [SourceSpec(platform, google_export)],
    )
    cli.run_wizard()


@pytest.mark.parametrize(
    "format_path",
    [
        pytest.param(lambda path: f"{path} ", id="trailing-space"),
        pytest.param(lambda path: f"  {path}  ", id="surrounding-spaces"),
        pytest.param(lambda path: f"'{path}'", id="single-quotes"),
        pytest.param(lambda path: f'  "{path}"  ', id="quotes-and-spaces"),
    ],
)
def test_select_paths_accepts_pasted_path_formatting(
    monkeypatch,
    google_export: Path,
    format_path,
) -> None:
    monkeypatch.setattr(
        cli.questionary,
        "path",
        lambda *args, **kwargs: _Prompt(format_path(google_export)),
    )
    monkeypatch.setattr(
        cli.questionary,
        "confirm",
        lambda *args, **kwargs: _Prompt(False),
    )

    specs = cli._select_paths("google")

    assert specs == [SourceSpec("google", google_export)]


def test_existing_prompt_path_prefers_normalized_windows_alias(
    monkeypatch,
    google_export: Path,
) -> None:
    pasted_path = Path(f"{google_export} ")
    existing_paths = {google_export, pasted_path}
    monkeypatch.setattr(Path, "exists", lambda path: path in existing_paths)

    path = cli._existing_prompt_path(str(pasted_path))

    assert path == google_export
