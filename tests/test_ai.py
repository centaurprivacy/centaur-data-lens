from __future__ import annotations

import json
from pathlib import Path

import pytest

from centaur_data_lens.ai import (
    AnthropicAdapter,
    GeminiAdapter,
    OllamaAdapter,
    OpenAIAdapter,
    TransmissionPreview,
    answer_question,
    create_adapter,
    prepare_question,
)
from centaur_data_lens.analysis import AnalysisSession, SourceSpec, analyze_sources
from centaur_data_lens.errors import ModelAdapterError


class FakeAdapter:
    name = "fake"
    model = "fake-model"
    destination = "https://provider.invalid"
    is_local = False

    def __init__(self, response: dict[str, object]) -> None:
        self.response = response

    def complete(self, *, system: str, user: str) -> str:
        assert "untrusted data" in system
        assert "record_ids" in system
        assert "source_ids" not in system
        assert "records" in json.loads(user)
        return json.dumps(self.response)


def test_cloud_requires_explicit_confirmation(google_export: Path) -> None:
    adapter = FakeAdapter({"answer": "No call", "claims": []})
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        with pytest.raises(ModelAdapterError, match="not confirmed"):
            answer_question(
                session,
                question="What was searched?",
                adapter=adapter,
                allow_cloud=False,
            )


def test_validates_model_citations(google_export: Path) -> None:
    adapter = FakeAdapter(
        {
            "claims": [
                {
                    "text": "Unsupported",
                    "kind": "observed",
                    "record_ids": ["fabricated"],
                }
            ],
        }
    )
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        with pytest.raises(ModelAdapterError, match="fabricated"):
            answer_question(
                session,
                question="What was searched?",
                adapter=adapter,
                allow_cloud=True,
            )


def test_context_is_bounded_and_preview_is_explicit(google_export: Path) -> None:
    adapter = FakeAdapter({"answer": "ok", "claims": []})
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        preview, payload, _ = prepare_question(session, "privacy", adapter)
    assert isinstance(preview, TransmissionPreview)
    assert preview.payload_bytes <= 256 * 1024
    assert preview.destination == "https://provider.invalid"
    assert "privacy tools" in payload
    decoded = json.loads(payload)
    assert "source_references" in decoded["records"][0]
    assert "source_ids" not in decoded["records"][0]


def test_complete_payload_rejects_oversized_question(google_export: Path) -> None:
    adapter = FakeAdapter({"answer": "unused", "claims": []})
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        with pytest.raises(ModelAdapterError, match="question exceeds"):
            prepare_question(session, "q" * 300_000, adapter)


def test_custom_endpoint_requires_https_or_loopback() -> None:
    with pytest.raises(ModelAdapterError, match="HTTPS"):
        OpenAIAdapter("synthetic-key", endpoint="http://provider.invalid/v1")
    local = OpenAIAdapter(
        "synthetic-key",
        endpoint="http://127.0.0.1:8080/v1",
        compatible=True,
    )
    assert local.is_local


def test_successful_answer_uses_available_record_id(google_export: Path) -> None:
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        record_id = session.search("privacy")[0].record_id
        adapter = FakeAdapter(
            {
                "claims": [
                    {
                        "text": "A privacy-related search was observed.",
                        "kind": "observed",
                        "record_ids": [record_id],
                    }
                ],
            }
        )
        _, answer = answer_question(
            session,
            question="What privacy searches appear?",
            adapter=adapter,
            allow_cloud=True,
        )
    assert answer.claims[0].record_ids == [record_id]


def test_rejects_answer_without_validated_claims(google_export: Path) -> None:
    adapter = FakeAdapter({"claims": []})
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        with pytest.raises(ModelAdapterError, match="at least one cited claim"):
            answer_question(
                session,
                question="What happened?",
                adapter=adapter,
                allow_cloud=True,
            )


def test_rejects_calculated_ai_claim_kind(google_export: Path) -> None:
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        record_id = session.search("privacy")[0].record_id
        adapter = FakeAdapter(
            {
                "claims": [
                    {
                        "text": "A calculated claim.",
                        "kind": "calculated",
                        "record_ids": [record_id],
                    }
                ],
            }
        )
        with pytest.raises(ModelAdapterError, match="invalid structured answer"):
            answer_question(
                session,
                question="What happened?",
                adapter=adapter,
                allow_cloud=True,
            )


def test_observed_claim_requires_evidence(google_export: Path) -> None:
    adapter = FakeAdapter(
        {
            "claims": [{"text": "No evidence", "kind": "observed", "record_ids": []}],
        }
    )
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        with pytest.raises(ModelAdapterError, match="without evidence"):
            answer_question(
                session,
                question="What happened?",
                adapter=adapter,
                allow_cloud=True,
            )


def test_inference_claim_requires_supporting_evidence(google_export: Path) -> None:
    adapter = FakeAdapter(
        {
            "claims": [{"text": "An inference", "kind": "inference", "record_ids": []}],
        }
    )
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        with pytest.raises(ModelAdapterError, match="without evidence"):
            answer_question(
                session,
                question="What might this mean?",
                adapter=adapter,
                allow_cloud=True,
            )


def test_rejects_ambiguous_legacy_source_ids(google_export: Path) -> None:
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        record_id = session.search("privacy")[0].record_id
        adapter = FakeAdapter(
            {
                "claims": [
                    {
                        "text": "Uses the ambiguous legacy field.",
                        "kind": "observed",
                        "source_ids": [record_id],
                    }
                ],
            }
        )
        with pytest.raises(ModelAdapterError, match="invalid structured answer"):
            answer_question(
                session,
                question="What happened?",
                adapter=adapter,
                allow_cloud=True,
            )


def test_provider_response_parsers(monkeypatch: pytest.MonkeyPatch) -> None:
    ollama = OllamaAdapter()
    monkeypatch.setattr(
        ollama,
        "_post_json",
        lambda *args, **kwargs: {"message": {"content": '{"answer":"ok","claims":[]}'}},
    )
    assert '"answer"' in ollama.complete(system="s", user="u")

    openai = OpenAIAdapter("synthetic")
    monkeypatch.setattr(
        openai,
        "_post_json",
        lambda *args, **kwargs: {
            "choices": [{"message": {"content": '{"answer":"ok","claims":[]}'}}]
        },
    )
    assert '"answer"' in openai.complete(system="s", user="u")

    anthropic = AnthropicAdapter("synthetic")
    monkeypatch.setattr(
        anthropic,
        "_post_json",
        lambda *args, **kwargs: {"content": [{"text": '{"answer":"ok","claims":[]}'}]},
    )
    assert '"answer"' in anthropic.complete(system="s", user="u")

    gemini = GeminiAdapter("synthetic")
    monkeypatch.setattr(
        gemini,
        "_post_json",
        lambda *args, **kwargs: {
            "candidates": [{"content": {"parts": [{"text": '{"answer":"ok","claims":[]}'}]}}]
        },
    )
    assert '"answer"' in gemini.complete(system="s", user="u")


def test_adapter_factory_uses_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic")
    assert create_adapter("openai").name == "openai"
    assert create_adapter("anthropic").name == "anthropic"
    assert create_adapter("gemini").name == "gemini"
    assert create_adapter("ollama").is_local
    with pytest.raises(ModelAdapterError, match="Unsupported"):
        create_adapter("unknown")
