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


def assert_answer_schema(schema: object) -> None:
    assert isinstance(schema, dict)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    properties = schema["properties"]
    assert isinstance(properties, dict)
    claims = properties["claims"]
    assert isinstance(claims, dict)
    item = claims["items"]
    assert isinstance(item, dict)
    assert item["required"] == ["text", "kind", "record_ids"]
    assert item["additionalProperties"] is False


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


def test_ollama_requests_structured_output(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    ollama = OllamaAdapter()

    def respond(*args, **kwargs):
        captured["payload"] = kwargs["payload"]
        return {
            "done_reason": "stop",
            "message": {"content": '{"claims":[]}'},
        }

    monkeypatch.setattr(ollama, "_post_json", respond)

    assert ollama.complete(system="s", user="u") == '{"claims":[]}'
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert_answer_schema(payload["format"])


def test_openai_requests_strict_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    openai = OpenAIAdapter("synthetic")

    def respond(*args, **kwargs):
        captured["payload"] = kwargs["payload"]
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": '{"claims":[]}'},
                }
            ]
        }

    monkeypatch.setattr(openai, "_post_json", respond)

    assert openai.complete(system="s", user="u") == '{"claims":[]}'
    payload = captured["payload"]
    assert isinstance(payload, dict)
    response_format = payload["response_format"]
    assert isinstance(response_format, dict)
    assert response_format["type"] == "json_schema"
    json_schema = response_format["json_schema"]
    assert isinstance(json_schema, dict)
    assert json_schema["name"] == "privacy_answer"
    assert json_schema["strict"] is True
    assert_answer_schema(json_schema["schema"])


def test_anthropic_requests_and_reads_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    anthropic = AnthropicAdapter("synthetic")

    def respond(*args, **kwargs):
        captured["payload"] = kwargs["payload"]
        return {
            "stop_reason": "end_turn",
            "content": [
                {"type": "thinking", "thinking": "synthetic"},
                {"type": "text", "text": '{"claims":'},
                {"type": "text", "text": "[]}"},
            ],
        }

    monkeypatch.setattr(anthropic, "_post_json", respond)

    assert anthropic.complete(system="s", user="u") == '{"claims":[]}'
    payload = captured["payload"]
    assert isinstance(payload, dict)
    output_config = payload["output_config"]
    assert isinstance(output_config, dict)
    output_format = output_config["format"]
    assert isinstance(output_format, dict)
    assert output_format["type"] == "json_schema"
    assert_answer_schema(output_format["schema"])


def test_gemini_requests_and_reads_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    gemini = GeminiAdapter("synthetic")

    def respond(*args, **kwargs):
        captured["payload"] = kwargs["payload"]
        return {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [
                            {"thought": True, "text": "synthetic reasoning"},
                            {"text": '{"claims":'},
                            {"text": "[]}"},
                        ]
                    },
                }
            ]
        }

    monkeypatch.setattr(gemini, "_post_json", respond)

    assert gemini.complete(system="s", user="u") == '{"claims":[]}'
    payload = captured["payload"]
    assert isinstance(payload, dict)
    generation_config = payload["generationConfig"]
    assert isinstance(generation_config, dict)
    assert generation_config["responseMimeType"] == "application/json"
    assert_answer_schema(generation_config["responseJsonSchema"])


@pytest.mark.parametrize(
    ("stop_reason", "message"),
    [
        ("max_tokens", "output limit"),
        ("refusal", "declined"),
    ],
)
def test_anthropic_reports_incomplete_structured_output(
    monkeypatch: pytest.MonkeyPatch,
    stop_reason: str,
    message: str,
) -> None:
    anthropic = AnthropicAdapter("synthetic")
    monkeypatch.setattr(
        anthropic,
        "_post_json",
        lambda *args, **kwargs: {
            "stop_reason": stop_reason,
            "content": [{"type": "text", "text": '{"claims":'}],
        },
    )

    with pytest.raises(ModelAdapterError, match=message):
        anthropic.complete(system="s", user="u")


def test_ollama_reports_incomplete_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ollama = OllamaAdapter()
    monkeypatch.setattr(
        ollama,
        "_post_json",
        lambda *args, **kwargs: {
            "done_reason": "length",
            "message": {"content": '{"claims":'},
        },
    )

    with pytest.raises(ModelAdapterError, match="output limit"):
        ollama.complete(system="s", user="u")


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": '{"claims":'},
                    }
                ]
            },
            "output limit",
        ),
        (
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"refusal": "synthetic refusal"},
                    }
                ]
            },
            "declined",
        ),
    ],
)
def test_openai_reports_incomplete_structured_output(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object],
    message: str,
) -> None:
    openai = OpenAIAdapter("synthetic")
    monkeypatch.setattr(openai, "_post_json", lambda *args, **kwargs: response)

    with pytest.raises(ModelAdapterError, match=message):
        openai.complete(system="s", user="u")


@pytest.mark.parametrize(
    ("finish_reason", "message"),
    [
        ("MAX_TOKENS", "output limit"),
        ("SAFETY", "declined"),
    ],
)
def test_gemini_reports_incomplete_structured_output(
    monkeypatch: pytest.MonkeyPatch,
    finish_reason: str,
    message: str,
) -> None:
    gemini = GeminiAdapter("synthetic")
    monkeypatch.setattr(
        gemini,
        "_post_json",
        lambda *args, **kwargs: {
            "candidates": [
                {
                    "finishReason": finish_reason,
                    "content": {"parts": [{"text": '{"claims":'}]},
                }
            ]
        },
    )

    with pytest.raises(ModelAdapterError, match=message):
        gemini.complete(system="s", user="u")


def test_gemini_reports_prompt_level_safety_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gemini = GeminiAdapter("synthetic")
    monkeypatch.setattr(
        gemini,
        "_post_json",
        lambda *args, **kwargs: {
            "promptFeedback": {"blockReason": "SAFETY"},
        },
    )

    with pytest.raises(ModelAdapterError, match="declined"):
        gemini.complete(system="s", user="u")


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
