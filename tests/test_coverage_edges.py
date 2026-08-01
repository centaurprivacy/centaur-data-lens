from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

import centaur_data_lens.ai as ai
import centaur_data_lens.platforms.base as platform_base
from centaur_data_lens.ai import (
    AnthropicAdapter,
    GeminiAdapter,
    OllamaAdapter,
    OpenAIAdapter,
    answer_question,
    create_adapter,
    prepare_question,
)
from centaur_data_lens.analysis import AnalysisSession, SourceSpec, analyze_sources
from centaur_data_lens.archive import ArchiveEntry, ArchiveReader
from centaur_data_lens.errors import ArchiveSafetyError, ModelAdapterError
from centaur_data_lens.models import (
    AIClaim,
    ArchiveManifest,
    CalculatedFact,
    ManifestEntry,
    QueryIntent,
    QueryOperation,
    QueryPlan,
    QueryResult,
    QueryStatus,
)
from centaur_data_lens.platforms import get_platform
from centaur_data_lens.platforms.base import PlatformDefinition, PlatformParser
from centaur_data_lens.query import build_manifest, compile_query, execute_query, manifest_entries


class _Response:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        redirect: bool = False,
        http_error: bool = False,
    ) -> None:
        self.is_redirect = redirect
        self._chunks = chunks
        self._http_error = http_error

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def raise_for_status(self) -> None:
        if self._http_error:
            request = httpx.Request("POST", "https://provider.invalid")
            response = httpx.Response(500, request=request)
            raise httpx.HTTPStatusError("synthetic", request=request, response=response)

    def iter_bytes(self) -> Iterator[bytes]:
        yield from self._chunks


class _Client:
    response: _Response

    def __init__(self, **_: object) -> None:
        pass

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def stream(self, *_: object, **__: object) -> _Response:
        return self.response


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (_Response([b"{}"], redirect=True), "redirect"),
        (_Response([b"x" * (2 * 1024 * 1024 + 1)]), "size limit"),
        (_Response([b"not-json"]), "failed safely"),
        (_Response([b"[]"]), "unsupported response"),
        (_Response([b"{}"], http_error=True), "failed safely"),
    ],
)
def test_http_adapter_rejects_unsafe_provider_responses(
    monkeypatch: pytest.MonkeyPatch,
    response: _Response,
    message: str,
) -> None:
    _Client.response = response
    monkeypatch.setattr(ai.httpx, "Client", _Client)

    with pytest.raises(ModelAdapterError, match=message):
        OllamaAdapter()._post_json(
            "http://127.0.0.1:11434/api/chat",
            headers={},
            content=b"{}",
        )


def test_http_adapter_reads_chunked_object(monkeypatch: pytest.MonkeyPatch) -> None:
    _Client.response = _Response([b'{"synthetic":', b"true}"])
    monkeypatch.setattr(ai.httpx, "Client", _Client)

    assert OllamaAdapter()._post_json(
        "http://127.0.0.1:11434/api/chat",
        headers={},
        content=b"{}",
    ) == {"synthetic": True}


@pytest.mark.parametrize(
    ("adapter", "response", "message"),
    [
        (OllamaAdapter(), {}, "Ollama"),
        (OpenAIAdapter("synthetic"), {}, "provider"),
        (OpenAIAdapter("synthetic"), {"choices": [1]}, "provider"),
        (
            OpenAIAdapter("synthetic"),
            {"choices": [{"finish_reason": "content_filter"}]},
            "declined",
        ),
        (OpenAIAdapter("synthetic"), {"choices": [{"message": 1}]}, "provider"),
        (OpenAIAdapter("synthetic"), {"choices": [{"message": {}}]}, "provider"),
        (AnthropicAdapter("synthetic"), {}, "Anthropic"),
        (AnthropicAdapter("synthetic"), {"content": [{"type": "text"}]}, "Anthropic"),
        (GeminiAdapter("synthetic"), {}, "Gemini"),
        (GeminiAdapter("synthetic"), {"candidates": [{}]}, "Gemini"),
        (
            GeminiAdapter("synthetic"),
            {"candidates": [{"content": {"parts": []}}]},
            "Gemini",
        ),
        (
            GeminiAdapter("synthetic"),
            {"candidates": [{"content": {"parts": [{"thought": True, "text": "hidden"}]}}]},
            "Gemini",
        ),
    ],
)
def test_provider_parsers_reject_malformed_shapes(
    monkeypatch: pytest.MonkeyPatch,
    adapter: OllamaAdapter | OpenAIAdapter | AnthropicAdapter | GeminiAdapter,
    response: dict[str, object],
    message: str,
) -> None:
    monkeypatch.setattr(adapter, "_post_json", lambda *args, **kwargs: response)
    body = adapter.build_request_body(system="synthetic", user="synthetic")

    with pytest.raises(ModelAdapterError, match=message):
        adapter.complete(request_body=body)


def test_adapter_input_validation_and_factory_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ModelAdapterError, match="model name"):
        OllamaAdapter(model="bad model")
    with pytest.raises(ModelAdapterError, match="credentials"):
        OpenAIAdapter("synthetic", endpoint="https://user@example.invalid/v1")
    with pytest.raises(ModelAdapterError, match="loopback"):
        OllamaAdapter(endpoint="https://example.invalid")
    with pytest.raises(ModelAdapterError, match="invalid"):
        OpenAIAdapter("synthetic", endpoint="https:///missing-host")
    with pytest.raises(ModelAdapterError, match="API key"):
        OpenAIAdapter("bad key")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ModelAdapterError, match="Set OPENAI_API_KEY"):
        create_adapter("openai")
    monkeypatch.setattr(ai.getpass, "getpass", lambda _: "synthetic")
    assert create_adapter("openai", prompt_for_key=True).name == "openai"
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    with pytest.raises(ModelAdapterError, match="endpoint is required"):
        create_adapter("openai-compatible")
    compatible = create_adapter(
        "openai-compatible",
        endpoint="http://127.0.0.1:8080/v1",
    )
    assert compatible.is_local


def _fact(fact_id: str, dimensions: dict[str, str]) -> CalculatedFact:
    return CalculatedFact(
        fact_id=fact_id,
        scope="archive",
        scope_definition="all_supported_records",
        metric="record_count",
        value=1,
        dimensions=dimensions,
        provenance="Synthetic local aggregate.",
    )


def test_fact_sensitivity_classes_cover_supported_dimensions() -> None:
    assert ai._fact_sensitivity_classes([]) == set()
    classes = ai._fact_sensitivity_classes(
        [
            _fact("fact-1", {"device": "synthetic", "service": "synthetic"}),
            _fact("fact-2", {"category": "advertising_search"}),
            _fact("fact-3", {"category": "device_profile_location"}),
        ]
    )
    assert classes == {"advertising", "browsing", "derived", "device", "identity", "location"}


def test_preparation_contract_and_answer_boundary_edges(google_export: Path) -> None:
    local = OllamaAdapter()
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        result = session.query("summarize this export")
        with pytest.raises(TypeError, match="exactly one"):
            prepare_question(result, "not-an-adapter")
        with pytest.raises(TypeError, match="requires a question"):
            prepare_question(session, local)
        prepared = prepare_question(result, local)

    with pytest.raises(ModelAdapterError, match="does not match"):
        answer_question(
            prepared,
            adapter=OllamaAdapter(model="different"),
            allow_cloud=True,
        )
    with pytest.raises(ModelAdapterError, match="request is invalid"):
        answer_question(
            replace(prepared, request_body=b""),
            adapter=local,
            allow_cloud=True,
        )


def test_preparation_rejects_invalid_empty_results() -> None:
    plan = QueryPlan(
        plan_id="plan-synthetic",
        question="synthetic",
        intent=QueryIntent.ARCHIVE_OVERVIEW,
        operation=QueryOperation.ARCHIVE_OVERVIEW,
    )
    result = QueryResult(
        plan=plan,
        status=QueryStatus.OK,
        total_records=0,
        matching_records=0,
    )
    with pytest.raises(ModelAdapterError, match="No supported records"):
        prepare_question(result, OllamaAdapter())

    coverage = result.model_copy(
        update={
            "status": QueryStatus.NOT_PRESENT,
            "message": "Synthetic local coverage result.",
        }
    )
    with pytest.raises(ModelAdapterError, match="no transmittable fact"):
        prepare_question(coverage, OllamaAdapter())


def test_claim_text_must_not_be_empty() -> None:
    with pytest.raises(ValidationError, match="cannot be empty"):
        AIClaim(text=" ", kind="inference")


class _AbstractParserProbe(PlatformParser):
    definition = PlatformDefinition(
        platform_id="probe",
        display_name="Probe",
        last_verified="2026-01-01",
        official_url="https://example.invalid",
        supported=(),
        excluded=(),
        guide=(),
    )

    def supported_path(self, path: str) -> bool:
        return super().supported_path(path)

    def category_for(self, path: str) -> str:
        return super().category_for(path)


def test_platform_parser_abstract_contract_raises() -> None:
    parser = _AbstractParserProbe()
    with pytest.raises(NotImplementedError):
        parser.supported_path("synthetic.json")
    with pytest.raises(NotImplementedError):
        parser.category_for("synthetic.json")


def test_platform_scalar_timestamp_and_normalization_edges() -> None:
    assert platform_base._nested_scalar(None) is None
    assert platform_base._nested_scalar({"value": {"unsupported": []}}) is None
    assert platform_base._first_scalar({"name": ""}, ("name",)) is None

    microsecond, micro_precision = platform_base._parse_timestamp(
        {"timestamp_usec": 1_735_786_800_000_000}
    )
    millisecond, milli_precision = platform_base._parse_timestamp({"timestamp": 1_735_786_800_000})
    naive, naive_precision = platform_base._parse_timestamp({"date": "2025-01-02T03:04:05"})
    missing, missing_precision = platform_base._parse_timestamp(
        {"timestamp": 10**400, "date": "not-a-date"}
    )
    assert microsecond and micro_precision == "microsecond"
    assert millisecond and milli_precision == "millisecond"
    assert naive and naive.tzinfo == UTC and naive_precision == "provided"
    assert (missing, missing_precision) == (None, None)

    record = platform_base.normalize_record(
        platform="meta",
        category="device_profile_location",
        path="root/product/synthetic.json",
        source_id="synthetic-source",
        pointer="/0",
        value={
            "title": {"value": "Synthetic title"},
            "timestamp": {"value": "2025-01-02T03:04:05Z"},
            "products": ["Synthetic Product"],
            "url": "ftp://example.invalid/private",
            "device_name": "Synthetic Device",
            "password": "must-be-excluded",
            "safe": True,
        },
    )
    assert record.service == "Synthetic Product"
    assert record.hostname is None
    assert record.attributes["safe"] is True
    assert "password" not in record.attributes
    assert record.sensitivity_tags == {"device", "identity", "location"}


def test_invalid_object_and_scalar_json_are_rejected(tmp_path: Path) -> None:
    object_path = tmp_path / "bad-object.zip"
    scalar_path = tmp_path / "scalar.zip"
    import zipfile

    with zipfile.ZipFile(object_path, "w") as archive:
        archive.writestr("Takeout/My Activity/Search/MyActivity.json", '{"broken":')
    with zipfile.ZipFile(scalar_path, "w") as archive:
        archive.writestr("Takeout/My Activity/Search/MyActivity.json", "42")

    for path in (object_path, scalar_path):
        with (
            ArchiveReader([path]) as reader,
            pytest.raises(ArchiveSafetyError, match=r"malformed|object"),
        ):
            list(get_platform("google").iter_records(reader))


def test_query_compiler_and_manifest_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    assert compile_query("").operation == QueryOperation.COVERAGE_ONLY
    assert compile_query("compare things").intent == QueryIntent.CLARIFICATION
    assert compile_query("what happened?!").intent == QueryIntent.ARCHIVE_OVERVIEW
    assert compile_query("show records from 2025-02-30").intent == QueryIntent.CLARIFICATION
    with pytest.raises(ValueError, match="Unknown timezone"):
        compile_query("show records from 2025-01-01", timezone="Synthetic/Nowhere")
    fixed = timezone(timedelta(hours=5, minutes=30), name="Synthetic Fixed")
    assert compile_query("show records from 2025-01-01", timezone=fixed).scope.timezone == (
        "Synthetic Fixed"
    )
    monkeypatch.setenv("TZ", "Synthetic/Nowhere")
    assert compile_query("show records from 2025-01-01").scope.timezone

    parser = get_platform("google")
    entries = manifest_entries(
        platform="google",
        parser=parser,
        entries=(
            ArchiveEntry(
                source_id="synthetic",
                path="orphan",
                size=1,
                compressed_size=1,
            ),
        ),
    )
    assert entries[0].product == "orphan"
    assert entries[0].extension == "[no extension]"
    assert build_manifest(entries).formats[0].name == "[no extension]"


def _manifest_entry(
    *,
    platform: str = "google",
    product: str = "account_activity",
    supported: bool,
) -> ManifestEntry:
    return ManifestEntry(
        source_id="synthetic",
        platform=platform,
        internal_path=f"{product}/synthetic.json",
        product=product,
        extension=".json",
        compressed_size=1,
        uncompressed_size=1,
        nested_archive=False,
        parser_supported=supported,
    )


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        (build_manifest((_manifest_entry(supported=True),)), QueryStatus.MATCHING_DATA_ABSENT),
        (build_manifest((_manifest_entry(supported=False),)), QueryStatus.PRODUCT_UNSUPPORTED),
        (ArchiveManifest(), QueryStatus.NOT_PRESENT),
    ],
)
def test_empty_overview_distinguishes_coverage_states(
    manifest: ArchiveManifest,
    expected: QueryStatus,
) -> None:
    with AnalysisSession() as session:
        result = execute_query(
            session._connection,
            manifest,
            compile_query("summarize this export"),
        )
    assert result.status == expected


def test_platform_comparison_reports_missing_platform() -> None:
    with AnalysisSession() as session:
        session.add_manifest_entries((_manifest_entry(supported=True),))
        result = session.query("compare google and meta")
    assert result.status == QueryStatus.NOT_PRESENT
    assert any(note.code == "platform_not_present" for note in result.coverage_notes)
