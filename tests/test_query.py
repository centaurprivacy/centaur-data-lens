from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from centaur_data_lens.ai import prepare_question
from centaur_data_lens.analysis import AnalysisSession, SourceSpec, analyze_sources
from centaur_data_lens.models import (
    ArchiveManifest,
    ManifestEntry,
    ManifestGroup,
    NormalizedRecord,
    QueryFacet,
    QueryIntent,
    QueryOperation,
    QueryPlan,
    QueryScope,
    QueryStatus,
    SourceReference,
)
from centaur_data_lens.query import compile_query, execute_query


class LocalEnvelopeAdapter:
    name = "synthetic"
    model = "synthetic"
    destination = "http://127.0.0.1:1"
    is_local = True

    def build_request_body(
        self,
        *,
        system: str,
        user: str,
        answer_schema: dict[str, object] | None = None,
    ) -> bytes:
        return json.dumps(
            {"system": system, "user": user, "schema": answer_schema},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()

    def complete(self, *, request_body: bytes) -> str:
        raise AssertionError("Query preparation must not call a model.")


def _record(
    record_id: str,
    *,
    platform: str = "google",
    category: str = "account_activity",
    timestamp: datetime | None = None,
    service: str | None = "Synthetic Service",
    device: str | None = "Synthetic Device",
    hostname: str | None = "synthetic.example",
    title: str = "Synthetic privacy event",
) -> NormalizedRecord:
    return NormalizedRecord(
        record_id=record_id,
        platform=platform,
        category=category,
        activity_type=category.replace("_", " "),
        service=service,
        timestamp=timestamp,
        timestamp_precision="provided" if timestamp else None,
        title=title,
        hostname=hostname,
        device=device,
        attributes={"fixture": True},
        sensitivity_tags={"browsing"},
        sources=(
            SourceReference(
                archive_id="synthetic-source",
                internal_path="synthetic/records.json",
                pointer=f"/{record_id}",
            ),
        ),
    )


def _populate(session: AnalysisSession, count: int = 240) -> None:
    session.add_manifest_entries(
        (
            ManifestEntry(
                source_id="synthetic-source",
                platform="google",
                internal_path="Takeout/My Activity/Search/MyActivity.json",
                product="account_activity",
                extension=".json",
                compressed_size=100,
                uncompressed_size=200,
                nested_archive=False,
                parser_supported=True,
            ),
            ManifestEntry(
                source_id="synthetic-source",
                platform="meta",
                internal_path="your_facebook_activity/search/search_history.json",
                product="search_history",
                extension=".json",
                compressed_size=100,
                uncompressed_size=200,
                nested_archive=False,
                parser_supported=True,
            ),
        )
    )
    for index in range(count):
        session.add_record(
            _record(
                f"{index:024x}",
                platform="google" if index % 2 == 0 else "meta",
                category=("account_activity", "search_history", "advertising")[index % 3],
                timestamp=datetime(
                    2025 + index % 2,
                    index % 12 + 1,
                    index % 27 + 1,
                    tzinfo=UTC,
                ),
                service=f"Synthetic Service {index % 5}",
                device=f"Synthetic Device {index % 4}",
                hostname=f"group-{index % 7}.example",
                title=f"Synthetic privacy event {index}",
            )
        )
    session.commit()


def test_complete_manifest_and_aggregates_are_archive_wide(tmp_path: Path) -> None:
    export = tmp_path / "broad.zip"
    with zipfile.ZipFile(export, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Takeout/My Activity/Search/MyActivity.json",
            json.dumps(
                [
                    {
                        "title": "Synthetic search",
                        "time": "2026-07-20T12:00:00Z",
                    }
                ]
            ),
        )
        for index in range(180):
            archive.writestr(
                f"Takeout/Google Photos/image-{index:03d}.jpg",
                b"synthetic-media",
            )
        archive.writestr("Takeout/Other/nested.zip", b"synthetic-nested-archive")
        archive.writestr(
            "Takeout/Other/private.SYNTHETIC_PRIVATE_MARKER",
            b"synthetic-private-format",
        )

    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", export)])
        manifest = session.manifest
        overview = session.query("summarize this export")
        prepared = prepare_question(overview, LocalEnvelopeAdapter())

    assert manifest.entry_count == 183
    assert len(manifest.entries) == 183
    assert manifest.parser_supported_entries == 1
    assert manifest.parser_unsupported_entries == 182
    assert manifest.nested_archive_count == 1
    assert sum(group.entry_count for group in manifest.formats) == 183
    assert {group.name for group in manifest.formats} == {
        ".jpg",
        ".json",
        ".zip",
        "[other]",
    }
    assert sum(group.entry_count for group in manifest.products) == 183
    assert manifest.uncompressed_size == sum(entry.uncompressed_size for entry in manifest.entries)
    assert overview.total_records == 1
    assert overview.matching_records == 1
    assert any(fact.metric == "archive_entry_count" for fact in overview.facts)
    assert any(fact.metric == "parser_unsupported_entry_count" for fact in overview.facts)
    assert all("product" not in fact.dimensions for fact in overview.facts)
    assert "synthetic_private_marker" not in prepared.payload.lower()


def test_compiler_covers_all_allowlisted_plan_families() -> None:
    cases = {
        "summarize this export": (
            QueryIntent.ARCHIVE_OVERVIEW,
            QueryOperation.ARCHIVE_OVERVIEW,
        ),
        "how many records are there?": (
            QueryIntent.ARCHIVE_OVERVIEW,
            QueryOperation.ARCHIVE_OVERVIEW,
        ),
        "tell me some trends": (
            QueryIntent.ARCHIVE_OVERVIEW,
            QueryOperation.ARCHIVE_OVERVIEW,
        ),
        "what stands out in my data?": (
            QueryIntent.ARCHIVE_OVERVIEW,
            QueryOperation.ARCHIVE_OVERVIEW,
        ),
        "what happened on July 20, 2026?": (
            QueryIntent.DATE_LOOKUP,
            QueryOperation.DATE_RANGE,
        ),
        "which devices are present?": (QueryIntent.FACET, QueryOperation.FACET_COUNTS),
        "what services appear most often?": (QueryIntent.FACET, QueryOperation.FACET_COUNTS),
        "how did activity change over time?": (
            QueryIntent.TREND,
            QueryOperation.TIME_BUCKETS,
        ),
        "compare Google and Meta": (
            QueryIntent.COMPARISON,
            QueryOperation.PLATFORM_COMPARISON,
        ),
        "compare search history and advertising": (
            QueryIntent.COMPARISON,
            QueryOperation.CATEGORY_COMPARISON,
        ),
        "find records about privacy": (
            QueryIntent.FULL_TEXT,
            QueryOperation.FULL_TEXT_MATCH,
        ),
        "show record 000000000000000000000001 details": (
            QueryIntent.RECORD_DETAIL,
            QueryOperation.RECORD_BY_ID,
        ),
    }
    for question, expected in cases.items():
        plan = compile_query(question, timezone="America/Los_Angeles")
        assert (plan.intent, plan.operation) == expected
    assert compile_query("which devices are present?").scope.facet == QueryFacet.DEVICE
    assert compile_query("what services appear most often?").scope.facet == QueryFacet.SERVICE
    assert compile_query("which activity types are present?").scope.facet == (
        QueryFacet.ACTIVITY_TYPE
    )
    invalid_date = compile_query("what happened on July 99, 2026?")
    assert invalid_date.intent == QueryIntent.CLARIFICATION


def test_vague_patterns_use_overview_but_explicit_time_trends_require_timestamps() -> None:
    with AnalysisSession() as session:
        session.add_record(_record("000000000000000000000001", timestamp=None))
        session.commit()
        count = session.query("how many records are there?")
        patterns = session.query("tell me some trends")
        temporal = session.query("show activity over time")

    assert count.status == QueryStatus.OK
    assert count.matching_records == 1
    assert any(
        fact.metric == "record_count"
        and fact.scope == "archive"
        and fact.dimensions == {}
        and fact.value == 1
        for fact in count.facts
    )
    assert patterns.status == QueryStatus.OK
    assert patterns.plan.operation == QueryOperation.ARCHIVE_OVERVIEW
    assert temporal.status == QueryStatus.MATCHING_DATA_ABSENT
    assert temporal.plan.operation == QueryOperation.TIME_BUCKETS


@pytest.mark.parametrize(
    ("intent", "operation", "scope", "message"),
    [
        (
            QueryIntent.DATE_LOOKUP,
            QueryOperation.DATE_RANGE,
            QueryScope(),
            "require both",
        ),
        (
            QueryIntent.DATE_LOOKUP,
            QueryOperation.DATE_RANGE,
            QueryScope(
                start_utc=datetime(2026, 7, 21, tzinfo=UTC),
                end_utc=datetime(2026, 7, 20, tzinfo=UTC),
            ),
            "earlier",
        ),
        (
            QueryIntent.DATE_LOOKUP,
            QueryOperation.DATE_RANGE,
            QueryScope(
                start_utc=datetime(2026, 7, 20),
                end_utc=datetime(2026, 7, 21),
            ),
            "timezone-aware",
        ),
        (
            QueryIntent.FACET,
            QueryOperation.FACET_COUNTS,
            QueryScope(),
            "require a facet",
        ),
        (
            QueryIntent.COMPARISON,
            QueryOperation.PLATFORM_COMPARISON,
            QueryScope(platforms=("google",)),
            "at least two platforms",
        ),
        (
            QueryIntent.COMPARISON,
            QueryOperation.CATEGORY_COMPARISON,
            QueryScope(categories=("search_history",)),
            "at least two categories",
        ),
        (
            QueryIntent.FULL_TEXT,
            QueryOperation.FULL_TEXT_MATCH,
            QueryScope(),
            "at least one text term",
        ),
        (
            QueryIntent.RECORD_DETAIL,
            QueryOperation.RECORD_BY_ID,
            QueryScope(),
            "at least one record ID",
        ),
        (
            QueryIntent.ARCHIVE_OVERVIEW,
            QueryOperation.ARCHIVE_OVERVIEW,
            QueryScope(text_terms=("ignored",)),
            "does not accept scope fields",
        ),
        (
            QueryIntent.FACET,
            QueryOperation.ARCHIVE_OVERVIEW,
            QueryScope(),
            "incompatible with intent",
        ),
    ],
)
def test_public_query_plan_rejects_invalid_operation_scope_combinations(
    intent: QueryIntent,
    operation: QueryOperation,
    scope: QueryScope,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        QueryPlan(
            plan_id="plan-synthetic",
            question="Synthetic question",
            intent=intent,
            operation=operation,
            scope=scope,
        )


def test_execute_query_revalidates_copied_plans_before_sqlite() -> None:
    valid = compile_query("find records about privacy")
    invalid = valid.model_copy(update={"scope": QueryScope()})

    with (
        AnalysisSession() as session,
        pytest.raises(
            ValidationError,
            match="at least one text term",
        ),
    ):
        session.execute_query(invalid)


def test_overview_facts_do_not_scale_with_untrusted_product_cardinality() -> None:
    product_count = 50_000
    manifest = ArchiveManifest(
        products=tuple(
            ManifestGroup(
                name=f"synthetic-product-{index}",
                entry_count=1,
                compressed_size=1,
                uncompressed_size=1,
                parser_supported_entries=0,
            )
            for index in range(product_count)
        ),
        entry_count=product_count,
        compressed_size=product_count,
        uncompressed_size=product_count,
        parser_unsupported_entries=product_count,
    )

    with AnalysisSession() as session:
        explicit_manifest_result = execute_query(
            session._connection,
            manifest,
            compile_query("summarize this export"),
        )

    assert len(explicit_manifest_result.facts) == 6
    assert all("product" not in fact.dimensions for fact in explicit_manifest_result.facts)


def test_query_execution_uses_complete_population_with_bounded_evidence() -> None:
    with AnalysisSession() as session:
        _populate(session)
        overview = session.query("summarize this export")
        devices = session.query("which devices are present?")
        services = session.query("what services appear most often?")
        trend = session.query("how did activity change over time?")
        comparison = session.query("compare Google and Meta")
        category_comparison = session.query("compare search history and advertising")
        text = session.query("find records about privacy")
        detail = session.query("show record 000000000000000000000001 details")

    for result in (
        overview,
        devices,
        services,
        trend,
        comparison,
        category_comparison,
        text,
    ):
        assert result.status == QueryStatus.OK
        assert result.total_records == 240
        assert len(result.evidence) <= 100
    assert overview.matching_records == 240
    assert devices.matching_records == 240
    assert services.matching_records == 240
    assert text.matching_records == 240
    assert any(
        fact.dimensions.get("device") == "Synthetic Device 0" and fact.value == 60
        for fact in devices.facts
    )
    assert any(fact.dimensions.get("month_utc") for fact in trend.facts)
    assert {record.platform for record in comparison.evidence} == {"google", "meta"}
    assert {record.category for record in category_comparison.evidence} == {
        "search_history",
        "advertising",
    }
    assert detail.matching_records == 1
    assert detail.evidence[0].record_id == "000000000000000000000001"


def test_date_lookup_uses_explicit_timezone_and_utc_boundaries() -> None:
    timestamps = (
        datetime(2026, 7, 20, 6, 59, 59, tzinfo=UTC),
        datetime(2026, 7, 20, 7, 0, 0, tzinfo=UTC),
        datetime(2026, 7, 21, 6, 59, 59, tzinfo=UTC),
        datetime(2026, 7, 21, 7, 0, 0, tzinfo=UTC),
    )
    with AnalysisSession() as session:
        for index, timestamp in enumerate(timestamps):
            session.add_record(_record(f"{index:024x}", timestamp=timestamp))
        session.commit()
        plan = session.compile_query(
            "what happened on July 20, 2026?",
            timezone="America/Los_Angeles",
        )
        result = session.execute_query(plan)

    assert plan.scope.timezone == "America/Los_Angeles"
    assert plan.scope.start_utc == datetime(2026, 7, 20, 7, 0, tzinfo=UTC)
    assert plan.scope.end_utc == datetime(2026, 7, 21, 7, 0, tzinfo=UTC)
    assert result.matching_records == 2
    assert {record.record_id for record in result.evidence} == {
        "000000000000000000000001",
        "000000000000000000000002",
    }
    assert result.assumptions[0].code == "local_timezone"


def test_ambiguous_unsupported_and_absence_statuses_are_distinct() -> None:
    with AnalysisSession() as session:
        session.add_manifest_entries(
            (
                ManifestEntry(
                    source_id="synthetic-source",
                    platform="google",
                    internal_path="Takeout/Google Play Store/Installs.json",
                    product="app_installs",
                    extension=".json",
                    compressed_size=10,
                    uncompressed_size=20,
                    nested_archive=False,
                    parser_supported=True,
                ),
                ManifestEntry(
                    source_id="synthetic-meta",
                    platform="meta",
                    internal_path="messages/inbox/message.json",
                    product="messages",
                    extension=".json",
                    compressed_size=10,
                    uncompressed_size=20,
                    nested_archive=False,
                    parser_supported=False,
                ),
            )
        )
        session.add_record(
            _record(
                "000000000000000000000001",
                category="app_installs",
                device=None,
            )
        )
        session.commit()
        missing_facet = session.query("which devices are present?")
        missing_category = session.query("what did I search for?")
        unsupported_product = session.query("compare Google and Meta")
        conversational = session.query("show me data")
        unsupported = session.query("explain causality between every event")

    assert missing_facet.status == QueryStatus.MATCHING_DATA_ABSENT
    assert missing_category.status == QueryStatus.NOT_PRESENT
    assert unsupported_product.status == QueryStatus.PRODUCT_UNSUPPORTED
    assert conversational.status == QueryStatus.OK
    assert conversational.plan.operation == QueryOperation.ARCHIVE_OVERVIEW
    assert unsupported.status == QueryStatus.UNSUPPORTED
    assert unsupported.message


def test_unsupported_category_is_not_reported_as_empty_data() -> None:
    with AnalysisSession() as session:
        session.add_manifest_entries(
            (
                ManifestEntry(
                    source_id="synthetic-source",
                    platform="meta",
                    internal_path="unsupported/search.json",
                    product="search_history",
                    extension=".json",
                    compressed_size=10,
                    uncompressed_size=20,
                    nested_archive=False,
                    parser_supported=False,
                ),
                ManifestEntry(
                    source_id="synthetic-source",
                    platform="meta",
                    internal_path="supported/advertising.json",
                    product="advertising",
                    extension=".json",
                    compressed_size=10,
                    uncompressed_size=20,
                    nested_archive=False,
                    parser_supported=True,
                ),
            )
        )
        session.add_record(
            _record(
                "000000000000000000000001",
                platform="meta",
                category="advertising",
            )
        )
        session.commit()
        facet = session.query("what did I search for?")
        comparison = session.query("compare search history and advertising")

    for result in (facet, comparison):
        assert result.status == QueryStatus.PRODUCT_UNSUPPORTED
        assert any(
            note.code == "product_present_but_unsupported" and note.category == "search_history"
            for note in result.coverage_notes
        )


def test_plans_facts_and_injection_handling_are_deterministic() -> None:
    question = 'find records about privacy" OR 1=1; DROP TABLE records; --'
    first = compile_query(question)
    second = compile_query(question)
    assert first == second
    assert first.plan_id == second.plan_id
    assert all(term.isalnum() or term.replace("_", "").isalnum() for term in first.scope.text_terms)

    with AnalysisSession() as session:
        _populate(session, count=4)
        first_result = session.execute_query(first)
        second_result = session.execute_query(second)
        remaining = list(session.records())

    assert first_result == second_result
    assert [fact.fact_id for fact in first_result.facts] == [
        fact.fact_id for fact in second_result.facts
    ]
    assert len(remaining) == 4


def test_explicit_query_result_preparation_excludes_local_provenance() -> None:
    private_path = "Takeout/PRIVATE-FILENAME.json"
    private_source = "private-source-identifier"
    with AnalysisSession() as session:
        session.add_manifest_entries(
            (
                ManifestEntry(
                    source_id=private_source,
                    platform="google",
                    internal_path=private_path,
                    product="account_activity",
                    extension=".json",
                    compressed_size=10,
                    uncompressed_size=20,
                    nested_archive=False,
                    parser_supported=True,
                ),
                ManifestEntry(
                    source_id=private_source,
                    platform="google",
                    internal_path="PRIVATE-UNSUPPORTED-FILENAME.bin",
                    product="private_unsupported_filename",
                    extension=".bin",
                    compressed_size=10,
                    uncompressed_size=20,
                    nested_archive=False,
                    parser_supported=False,
                ),
            )
        )
        session.add_record(
            _record("000000000000000000000001").model_copy(
                update={
                    "sources": (
                        SourceReference(
                            archive_id=private_source,
                            internal_path=private_path,
                            pointer="/0",
                        ),
                    )
                },
            )
        )
        session.commit()
        result = session.query("summarize this export")
        prepared = prepare_question(result, LocalEnvelopeAdapter())

    assert prepared.preview.record_count == 1
    assert prepared.preview.record_count <= 100
    assert prepared.preview.payload_bytes <= 256 * 1024
    assert private_path not in prepared.payload
    assert private_source not in prepared.payload
    assert "private_unsupported_filename" not in prepared.payload
    assert "transmittable" not in prepared.payload
    assert "internal_path" not in prepared.payload
    assert "source_id" not in prepared.payload
    assert "archive_id" not in prepared.payload
