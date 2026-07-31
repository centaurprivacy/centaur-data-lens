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
from centaur_data_lens.models import NormalizedRecord, SourceReference


class FakeAdapter:
    name = "fake"
    model = "fake-model"
    destination = "https://provider.invalid"
    is_local = False

    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls = 0
        self.answer_schema: dict[str, object] | None = None
        self.request_body: bytes | None = None

    def build_request_body(
        self,
        *,
        system: str,
        user: str,
        answer_schema: dict[str, object] | None = None,
    ) -> bytes:
        self.answer_schema = answer_schema
        return json.dumps(
            {
                "model": self.model,
                "system": system,
                "user": user,
                "schema": answer_schema,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()

    def complete(self, *, request_body: bytes) -> str:
        self.calls += 1
        self.request_body = request_body
        request = json.loads(request_body)
        system = request["system"]
        user = request["user"]
        assert isinstance(system, str)
        assert isinstance(user, str)
        assert "untrusted data" in system
        assert "record_ids" in system
        assert "fact_ids" in system
        assert "source_ids" not in system
        decoded = json.loads(user)
        assert "calculated_facts" in decoded
        assert "evidence_records" in decoded
        return json.dumps(self.response)


def assert_answer_schema(schema: object) -> None:
    assert isinstance(schema, dict)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    properties = schema["properties"]
    assert isinstance(properties, dict)
    claims = properties["claims"]
    assert isinstance(claims, dict)
    assert claims["minItems"] == 1
    assert claims["maxItems"] == 8
    item = claims["items"]
    assert isinstance(item, dict)
    variant_properties = item["properties"]
    assert isinstance(variant_properties, dict)
    kind = variant_properties["kind"]
    assert isinstance(kind, dict)
    assert kind["enum"] == ["observed", "calculated", "inference"]
    fact_ids = variant_properties["fact_ids"]
    assert isinstance(fact_ids, dict)
    assert fact_ids["type"] == "array"
    assert fact_ids["items"] == {"type": "string"}
    assert item["required"] == ["text", "kind", "record_ids", "fact_ids"]
    assert item["additionalProperties"] is False


def complete_directly(
    adapter: OllamaAdapter | OpenAIAdapter | AnthropicAdapter | GeminiAdapter,
    *,
    system: str = "s",
    user: str = "u",
) -> str:
    request_body = adapter.build_request_body(system=system, user=user)
    return adapter.complete(request_body=request_body)


def test_cloud_requires_explicit_confirmation(google_export: Path) -> None:
    adapter = FakeAdapter({"answer": "No call", "claims": []})
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        prepared = prepare_question(session, "What was searched?", adapter)
        with pytest.raises(ModelAdapterError, match="not confirmed"):
            answer_question(
                prepared,
                adapter=adapter,
                allow_cloud=False,
            )
    assert adapter.calls == 0


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
        prepared = prepare_question(session, "privacy", adapter)
        with pytest.raises(ModelAdapterError, match="fabricated"):
            answer_question(
                prepared,
                adapter=adapter,
                allow_cloud=True,
            )


def test_context_is_bounded_and_preview_is_explicit(google_export: Path) -> None:
    adapter = FakeAdapter({"answer": "ok", "claims": []})
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        prepared = prepare_question(session, "privacy", adapter)
    preview = prepared.preview
    payload = prepared.payload
    assert isinstance(preview, TransmissionPreview)
    assert preview.payload_bytes <= 256 * 1024
    assert preview.payload_bytes == len(prepared.request_body)
    assert preview.payload_bytes > len(payload.encode())
    request = json.loads(prepared.request_body)
    assert request["user"] == payload
    assert "untrusted data" in request["system"]
    assert "conversational language" in request["system"]
    assert "Do not emit one" in request["system"]
    assert "claim per record" in request["system"]
    assert_answer_schema(request["schema"])
    rendered_schema = json.dumps(request["schema"])
    assert all(fact_id not in rendered_schema for fact_id in prepared.valid_fact_ids)
    assert all(record_id not in rendered_schema for record_id in prepared.valid_record_ids)
    assert preview.destination == "https://provider.invalid"
    assert preview.total_records == 5
    assert preview.matching_records == 1
    assert preview.fact_count >= 2
    assert preview.data_mode == "cloud_raw_personal_data"
    assert preview.will_transmit
    assert "request.system_prompt" in preview.transmitted_fields
    assert "request.structured_output_schema" in preview.transmitted_fields
    assert "privacy tools" in payload
    decoded = json.loads(payload)
    assert decoded["scope"]["total_records"] == 5
    assert "query_plan" not in decoded
    assert "conversation_context" not in decoded
    assert decoded["scope"]["matching_records"] == 1
    assert decoded["calculated_facts"]
    assert decoded["evidence_records"]
    assert "source_references" not in payload
    assert "source_ids" not in payload
    assert "archive_id" not in payload
    assert "internal_path" not in payload
    assert "Takeout/" not in payload


def test_authorized_submission_uses_exact_prepared_request_body(
    google_export: Path,
) -> None:
    adapter = FakeAdapter({"claims": []})
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        prepared = prepare_question(session, "privacy", adapter)
        fact_id = next(iter(prepared.valid_fact_ids))
        adapter.response = {
            "claims": [
                {
                    "text": "A local calculation was provided.",
                    "kind": "calculated",
                    "record_ids": [],
                    "fact_ids": [fact_id],
                }
            ]
        }
        answer_question(prepared, adapter=adapter, allow_cloud=True)

    assert adapter.request_body is prepared.request_body


def test_anthropic_preview_includes_static_schema_and_complete_request_body(
    google_export: Path,
) -> None:
    adapter = AnthropicAdapter("synthetic")
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        prepared = prepare_question(session, "privacy", adapter)

    request = json.loads(prepared.request_body)
    assert request["system"]
    assert request["messages"][0]["content"] == prepared.payload
    schema = request["output_config"]["format"]["schema"]
    rendered_schema = json.dumps(schema)
    assert all(fact_id not in rendered_schema for fact_id in prepared.valid_fact_ids)
    assert all(record_id not in rendered_schema for record_id in prepared.valid_record_ids)
    assert prepared.preview.payload_bytes == len(prepared.request_body)
    assert prepared.preview.payload_bytes > len(prepared.payload.encode())


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
        prepared = prepare_question(session, "What privacy searches appear?", adapter)
        answer = answer_question(
            prepared,
            adapter=adapter,
            allow_cloud=True,
        )
    assert answer.claims[0].record_ids == [record_id]


def test_rejects_answer_without_validated_claims(google_export: Path) -> None:
    adapter = FakeAdapter({"claims": []})
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        prepared = prepare_question(session, "What happened?", adapter)
        with pytest.raises(ModelAdapterError, match="at least one cited claim"):
            answer_question(
                prepared,
                adapter=adapter,
                allow_cloud=True,
            )


def test_rejects_fragmented_answer_with_too_many_claims(google_export: Path) -> None:
    adapter = FakeAdapter({"claims": []})
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        prepared = prepare_question(session, "summarize this export", adapter)
        fact_id = next(iter(prepared.valid_fact_ids))
        adapter.response = {
            "claims": [
                {
                    "text": f"Fragment {index}",
                    "kind": "calculated",
                    "record_ids": [],
                    "fact_ids": [fact_id],
                }
                for index in range(9)
            ]
        }
        with pytest.raises(ModelAdapterError, match="invalid structured answer"):
            answer_question(prepared, adapter=adapter, allow_cloud=True)


def test_calculated_claim_requires_fact_evidence(google_export: Path) -> None:
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
        prepared = prepare_question(session, "privacy", adapter)
        with pytest.raises(ModelAdapterError, match="requires calculated-fact"):
            answer_question(
                prepared,
                adapter=adapter,
                allow_cloud=True,
            )
    assert adapter.calls == 1


def test_local_model_retries_one_invalid_citation_contract(google_export: Path) -> None:
    class LocalSequenceAdapter(FakeAdapter):
        destination = "http://127.0.0.1:11434"
        is_local = True

        def __init__(self) -> None:
            super().__init__({"claims": []})
            self.responses: list[dict[str, object]] = []

        def complete(self, *, request_body: bytes) -> str:
            self.response = self.responses[self.calls]
            return super().complete(request_body=request_body)

    adapter = LocalSequenceAdapter()
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        prepared = prepare_question(session, "privacy", adapter)
        record_id = next(iter(prepared.valid_record_ids))
        adapter.responses = [
            {
                "claims": [
                    {
                        "text": "Wrong evidence type.",
                        "kind": "calculated",
                        "record_ids": [record_id],
                        "fact_ids": [],
                    }
                ]
            },
            {
                "claims": [
                    {
                        "text": "Corrected evidence type.",
                        "kind": "observed",
                        "record_ids": [record_id],
                        "fact_ids": [],
                    }
                ]
            },
        ]
        answer = answer_question(prepared, adapter=adapter, allow_cloud=False)
    assert adapter.calls == 2
    assert answer.claims[0].kind.value == "observed"
    assert adapter.answer_schema is not None
    properties = adapter.answer_schema["properties"]
    assert isinstance(properties, dict)
    claims = properties["claims"]
    assert isinstance(claims, dict)
    item = claims["items"]
    assert isinstance(item, dict)
    claim_properties = item["properties"]
    assert isinstance(claim_properties, dict)
    kind = claim_properties["kind"]
    assert isinstance(kind, dict)
    assert kind["enum"] == ["observed", "calculated", "inference"]
    record_ids = claim_properties["record_ids"]
    assert isinstance(record_ids, dict)
    record_items = record_ids["items"]
    assert isinstance(record_items, dict)
    assert set(record_items["enum"]) == prepared.valid_record_ids


def test_calculated_claim_accepts_valid_fact_evidence(google_export: Path) -> None:
    adapter = FakeAdapter({"claims": []})
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        prepared = prepare_question(session, "privacy", adapter)
        fact_id = next(iter(prepared.valid_fact_ids))
        adapter.response = {
            "claims": [
                {
                    "text": "A local calculation was provided.",
                    "kind": "calculated",
                    "fact_ids": [fact_id],
                }
            ]
        }
        answer = answer_question(prepared, adapter=adapter, allow_cloud=True)
    assert answer.claims[0].fact_ids == [fact_id]
    assert adapter.answer_schema is not None
    properties = adapter.answer_schema["properties"]
    assert isinstance(properties, dict)
    claims = properties["claims"]
    assert isinstance(claims, dict)
    item = claims["items"]
    assert isinstance(item, dict)
    claim_properties = item["properties"]
    assert isinstance(claim_properties, dict)
    fact_ids = claim_properties["fact_ids"]
    assert isinstance(fact_ids, dict)
    fact_items = fact_ids["items"]
    assert isinstance(fact_items, dict)
    assert fact_items == {"type": "string"}


def test_rejects_fabricated_fact_citation(google_export: Path) -> None:
    adapter = FakeAdapter(
        {
            "claims": [
                {
                    "text": "A fabricated calculation.",
                    "kind": "calculated",
                    "fact_ids": ["fact-fabricated"],
                }
            ]
        }
    )
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        prepared = prepare_question(session, "privacy", adapter)
        with pytest.raises(ModelAdapterError, match="fabricated or unavailable fact"):
            answer_question(prepared, adapter=adapter, allow_cloud=True)


def test_no_match_question_is_answered_locally_without_archive_facts(
    google_export: Path,
) -> None:
    adapter = FakeAdapter({"claims": []})
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        prepared = prepare_question(session, "quantum zebras", adapter)
    decoded = json.loads(prepared.payload)
    assert prepared.preview.matching_records == 0
    assert prepared.preview.record_count == 0
    assert prepared.preview.fact_count == 0
    assert prepared.preview.payload_bytes == 0
    assert not prepared.preview.will_transmit
    assert prepared.preview.data_mode == "local_no_match"
    assert prepared.preview.sensitivity_classes == ()
    assert prepared.request_body == b""
    assert decoded["evidence_records"] == []
    assert len(decoded["calculated_facts"]) == 1
    assert decoded["calculated_facts"][0]["scope"] == "matching"
    assert decoded["calculated_facts"][0]["value"] == 0
    answer = answer_question(prepared, adapter=adapter, allow_cloud=False)
    assert answer.claims[0].text == "No matching records were found for this question."
    assert adapter.calls == 0


def test_missing_device_facet_has_specific_local_answer() -> None:
    adapter = FakeAdapter({"claims": []})
    with AnalysisSession() as session:
        session.add_record(
            NormalizedRecord(
                record_id="synthetic-install",
                platform="google",
                category="app_installs",
                activity_type="app installs",
                service="Google Play Store",
                title="Installed Synthetic App",
                sources=(
                    SourceReference(
                        archive_id="synthetic-archive",
                        internal_path="synthetic/installs.json",
                        pointer="/0",
                    ),
                ),
            )
        )
        session.commit()
        prepared = prepare_question(session, "Which devices are present?", adapter)
        answer = answer_question(prepared, adapter=adapter, allow_cloud=False)

    assert answer.claims[0].text == (
        "No device values were found in the supported records from this export."
    )
    assert adapter.calls == 0


def test_observed_claim_requires_evidence(google_export: Path) -> None:
    adapter = FakeAdapter(
        {
            "claims": [{"text": "No evidence", "kind": "observed", "record_ids": []}],
        }
    )
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        prepared = prepare_question(session, "What happened?", adapter)
        with pytest.raises(ModelAdapterError, match="requires record evidence"):
            answer_question(
                prepared,
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
        prepared = prepare_question(session, "What might this mean?", adapter)
        with pytest.raises(ModelAdapterError, match="requires supporting evidence"):
            answer_question(
                prepared,
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
        prepared = prepare_question(session, "privacy", adapter)
        with pytest.raises(ModelAdapterError, match="invalid structured answer"):
            answer_question(
                prepared,
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
    assert '"answer"' in complete_directly(ollama)

    openai = OpenAIAdapter("synthetic")
    monkeypatch.setattr(
        openai,
        "_post_json",
        lambda *args, **kwargs: {
            "choices": [{"message": {"content": '{"answer":"ok","claims":[]}'}}]
        },
    )
    assert '"answer"' in complete_directly(openai)

    anthropic = AnthropicAdapter("synthetic")
    monkeypatch.setattr(
        anthropic,
        "_post_json",
        lambda *args, **kwargs: {"content": [{"text": '{"answer":"ok","claims":[]}'}]},
    )
    assert '"answer"' in complete_directly(anthropic)

    gemini = GeminiAdapter("synthetic")
    monkeypatch.setattr(
        gemini,
        "_post_json",
        lambda *args, **kwargs: {
            "candidates": [{"content": {"parts": [{"text": '{"answer":"ok","claims":[]}'}]}}]
        },
    )
    assert '"answer"' in complete_directly(gemini)


def test_ollama_requests_structured_output(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    ollama = OllamaAdapter()

    def respond(*args, **kwargs):
        captured["payload"] = json.loads(kwargs["content"])
        return {
            "done_reason": "stop",
            "message": {"content": '{"claims":[]}'},
        }

    monkeypatch.setattr(ollama, "_post_json", respond)

    assert complete_directly(ollama) == '{"claims":[]}'
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert_answer_schema(payload["format"])
    assert payload["options"] == {"temperature": 0}


def test_openai_requests_strict_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    openai = OpenAIAdapter("synthetic")

    def respond(*args, **kwargs):
        captured["payload"] = json.loads(kwargs["content"])
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": '{"claims":[]}'},
                }
            ]
        }

    monkeypatch.setattr(openai, "_post_json", respond)

    assert complete_directly(openai) == '{"claims":[]}'
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
        captured["payload"] = json.loads(kwargs["content"])
        return {
            "stop_reason": "end_turn",
            "content": [
                {"type": "thinking", "thinking": "synthetic"},
                {"type": "text", "text": '{"claims":'},
                {"type": "text", "text": "[]}"},
            ],
        }

    monkeypatch.setattr(anthropic, "_post_json", respond)

    assert complete_directly(anthropic) == '{"claims":[]}'
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
        captured["payload"] = json.loads(kwargs["content"])
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

    assert complete_directly(gemini) == '{"claims":[]}'
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
        complete_directly(anthropic)


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
        complete_directly(ollama)


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
        complete_directly(openai)


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
        complete_directly(gemini)


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
        complete_directly(gemini)


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
