from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

import centaur_data_lens.cli as cli
from centaur_data_lens.analysis import AnalysisSession, SourceSpec, analyze_sources
from centaur_data_lens.cli import app
from centaur_data_lens.errors import ModelAdapterError

runner = CliRunner()


class _Prompt:
    def __init__(self, value: object) -> None:
        self.value = value

    def ask(self) -> object:
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class _LocalAdapter:
    name = "synthetic-local"
    model = "fixed"
    destination = "http://127.0.0.1:11434"
    is_local = True

    def __init__(self) -> None:
        self.calls: list[bytes] = []

    def build_request_body(self, *, system, user, answer_schema=None) -> bytes:
        return user.encode()

    def complete(self, *, request_body: bytes) -> str:
        self.calls.append(request_body)
        payload = json.loads(request_body)
        fact_id = payload["calculated_facts"][0]["fact_id"]
        record_ids = [record["record_id"] for record in payload["evidence_records"][:1]]
        lowered_question = payload["question"].lower()
        if "surprising" in lowered_question:
            return json.dumps(
                {
                    "claims": [
                        {
                            "text": (
                                "Among the selected examples, this item is the most surprising "
                                "to me; that choice is subjective."
                            ),
                            "kind": "inference",
                            "record_ids": record_ids,
                            "fact_ids": [],
                        }
                    ]
                }
            )
        if "first item" in lowered_question:
            return json.dumps(
                {
                    "claims": [
                        {
                            "text": (
                                "This is the first record in the previous result's deterministic "
                                "evidence order."
                            ),
                            "kind": "observed",
                            "record_ids": record_ids,
                            "fact_ids": [],
                        }
                    ]
                }
            )
        claims = [
            {
                "text": "Synthetic cited answer.",
                "kind": "calculated",
                "record_ids": record_ids,
                "fact_ids": [fact_id],
            }
        ]
        if payload["scope"]["selection_mode"] == "archive_overview":
            category = payload["evidence_records"][0]["category"].replace("_", " ")
            claims.append(
                {
                    "text": f"Synthetic {category} overview with timestamp context.",
                    "kind": "calculated",
                    "record_ids": [],
                    "fact_ids": [fact_id],
                }
            )
        if "what does" in lowered_question:
            claims.append(
                {
                    "text": "Synthetic interpretation.",
                    "kind": "inference",
                    "record_ids": [],
                    "fact_ids": [fact_id],
                }
            )
        return json.dumps(
            {
                "claims": claims,
            }
        )


class _CloudAdapter(_LocalAdapter):
    name = "synthetic-cloud"
    destination = "https://provider.invalid"
    is_local = False


def _prompt_from(values: Iterator[object]):
    def prompt(*args, **kwargs) -> _Prompt:
        return _Prompt(next(values))

    return prompt


def test_chat_indexes_once_and_executes_a_fresh_plan_per_question(
    monkeypatch: pytest.MonkeyPatch,
    google_export: Path,
) -> None:
    adapter = _LocalAdapter()
    prompts = iter(
        [
            "summarize this export",
            "what happened on March 31, 2025?",
            "did that record include media?",
            "what does this mean?",
            ":exit",
        ]
    )
    monkeypatch.setattr(cli.questionary, "text", _prompt_from(prompts))
    monkeypatch.setattr(cli, "create_adapter", lambda *args, **kwargs: adapter)
    index_calls = 0
    query_plans: list[str] = []
    original_analyze = cli.analyze_sources
    original_execute = AnalysisSession.execute_query

    def counted_analyze(*args, **kwargs):
        nonlocal index_calls
        index_calls += 1
        return original_analyze(*args, **kwargs)

    def counted_execute(self, plan, **kwargs):
        query_plans.append(plan.plan_id)
        return original_execute(self, plan, **kwargs)

    monkeypatch.setattr(cli, "analyze_sources", counted_analyze)
    monkeypatch.setattr(AnalysisSession, "execute_query", counted_execute)
    result = runner.invoke(
        app,
        [
            "chat",
            "--source",
            f"google={google_export}",
            "--provider",
            "ollama",
            "--timezone",
            "America/Los_Angeles",
        ],
    )

    assert result.exit_code == 0, result.output
    assert index_calls == 1
    assert len(query_plans) == 4
    assert len(set(query_plans)) == 4
    assert len(adapter.calls) == 4
    final_payload = json.loads(adapter.calls[-1])
    recent_turns = final_payload["conversation_context"]["recent_turns"]
    assert [turn["question"] for turn in recent_turns] == [
        "summarize this export",
        "what happened on March 31, 2025?",
        "did that record include media?",
    ]
    assert (
        final_payload["conversation_context"]["resolved_referent"]["referent_kind"]
        == "previous_result"
    )
    assert "Evidence (" in result.stdout
    assert "facts:" in result.stdout
    assert "Local analysis:" in result.stdout
    assert "Transmission preview" not in result.stdout
    assert (
        "Timezone disclosure: assuming America/Los_Angeles; UTC boundary checked" in result.stdout
    )
    assert "Temporary analysis was deleted" in result.stdout


def test_chat_resolves_subjective_and_first_item_follow_ups(
    monkeypatch: pytest.MonkeyPatch,
    google_export: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = _LocalAdapter()
    monkeypatch.setattr(
        cli.questionary,
        "text",
        _prompt_from(
            iter(
                [
                    "show me a summary",
                    "whats the most surprising item in there?",
                    "whats the first item?",
                    ":exit",
                ]
            )
        ),
    )

    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        cli._chat_loop(session, adapter=adapter, timezone="UTC")

    payloads = [json.loads(request) for request in adapter.calls]
    assert len(payloads) == 3
    assert payloads[1]["query_plan"]["operation"] == "archive_overview"
    assert payloads[2]["query_plan"]["operation"] == "record_by_id"
    output = capsys.readouterr().out
    assert "No matching records were found" not in output
    assert "subjective" in output


def test_chat_help_describes_ephemeral_requery_contract() -> None:
    result = runner.invoke(app, ["chat", "--help"])
    assert result.exit_code == 0
    assert "ephemeral chat" in result.stdout
    assert "re-queries" in result.stdout


def test_chat_commands_scope_timezone_reset_and_exit(
    monkeypatch: pytest.MonkeyPatch,
    google_export: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompts = iter(
        [
            ":help",
            ":coverage",
            ":scope",
            ":timezone",
            ":timezone America/Los_Angeles",
            "summarize this export",
            ":scope",
            ":reset",
            ":scope",
            ":exit",
        ]
    )
    monkeypatch.setattr(cli.questionary, "text", _prompt_from(prompts))
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        cli._chat_loop(session, adapter=_LocalAdapter(), timezone=None)

    output = capsys.readouterr().out
    assert "Chat commands" in output
    assert "Coverage and omissions" in output
    assert "Timezone: system default" in output
    assert "Timezone set to America/Los_Angeles" in output
    assert "Previous plan:" in output
    assert output.count("Active scope: none") == 2


def test_cloud_chat_requires_fresh_authorization_for_every_transmitted_turn(
    monkeypatch: pytest.MonkeyPatch,
    google_export: Path,
) -> None:
    adapter = _CloudAdapter()
    questions = iter(["summarize this export", "which services were present?", ":exit"])
    confirmations = iter(["SEND PERSONAL DATA", "no"])
    confirmation_prompts = 0

    def prompt(message: str) -> _Prompt:
        nonlocal confirmation_prompts
        if message.startswith("Type SEND PERSONAL DATA"):
            confirmation_prompts += 1
            return _Prompt(next(confirmations))
        return _Prompt(next(questions))

    monkeypatch.setattr(cli.questionary, "text", prompt)
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        cli._chat_loop(session, adapter=adapter, timezone="UTC")

    assert confirmation_prompts == 2
    assert len(adapter.calls) == 1


@pytest.mark.parametrize(
    "questions",
    [
        ["did that record include media?", ":exit"],
        ["quantum zebras", ":exit"],
    ],
    ids=["clarification", "no-match"],
)
def test_local_chat_responses_make_zero_provider_calls(
    monkeypatch: pytest.MonkeyPatch,
    google_export: Path,
    questions: list[str],
) -> None:
    adapter = _CloudAdapter()
    monkeypatch.setattr(cli.questionary, "text", _prompt_from(iter(questions)))
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        cli._chat_loop(session, adapter=adapter, timezone="UTC")

    assert adapter.calls == []


def test_cancellation_makes_zero_provider_calls(
    monkeypatch: pytest.MonkeyPatch,
    google_export: Path,
) -> None:
    adapter = _CloudAdapter()
    questions = iter(["summarize this export"])

    def prompt(message: str) -> _Prompt:
        if message.startswith("Type SEND PERSONAL DATA"):
            return _Prompt(KeyboardInterrupt())
        return _Prompt(next(questions))

    monkeypatch.setattr(cli.questionary, "text", prompt)
    with pytest.raises(KeyboardInterrupt), AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        cli._chat_loop(session, adapter=adapter, timezone="UTC")

    assert adapter.calls == []
    assert not session.database_path.parent.exists()


@pytest.mark.parametrize("ending", [":exit", None, EOFError(), KeyboardInterrupt()])
def test_chat_cleanup_on_normal_exit_eof_and_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    google_export: Path,
    ending: object,
) -> None:
    monkeypatch.setattr(cli.questionary, "text", lambda *args, **kwargs: _Prompt(ending))
    try:
        with AnalysisSession() as session:
            directory = session.database_path.parent
            analyze_sources(session, [SourceSpec("google", google_export)])
            cli._chat_loop(session, adapter=_LocalAdapter(), timezone="UTC")
    except (EOFError, KeyboardInterrupt):
        pass
    assert not directory.exists()


def test_chat_cleanup_on_provider_exception(
    monkeypatch: pytest.MonkeyPatch,
    google_export: Path,
) -> None:
    class FailingAdapter(_LocalAdapter):
        def complete(self, *, request_body: bytes) -> str:
            raise ModelAdapterError("Synthetic provider failure.")

    prompts = iter(["summarize this export"])
    monkeypatch.setattr(cli.questionary, "text", _prompt_from(prompts))
    with pytest.raises(ModelAdapterError), AnalysisSession() as session:
        directory = session.database_path.parent
        analyze_sources(session, [SourceSpec("google", google_export)])
        cli._chat_loop(session, adapter=FailingAdapter(), timezone="UTC")
    assert not directory.exists()
