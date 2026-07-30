from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ClaimKind(StrEnum):
    OBSERVED = "observed"
    CALCULATED = "calculated"
    INFERENCE = "inference"


class AIClaimKind(StrEnum):
    OBSERVED = "observed"
    CALCULATED = "calculated"
    INFERENCE = "inference"


class SourceReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    archive_id: str
    internal_path: str
    pointer: str

    @property
    def label(self) -> str:
        return f"{self.archive_id}:{self.internal_path}{self.pointer}"


class NormalizedRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = "1"
    record_id: str
    platform: str
    category: str
    activity_type: str
    service: str | None = None
    timestamp: datetime | None = None
    timestamp_precision: str | None = None
    title: str | None = None
    hostname: str | None = None
    device: str | None = None
    attributes: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    sensitivity_tags: set[str] = Field(default_factory=set)
    sources: tuple[SourceReference, ...]


class CoverageItem(BaseModel):
    platform: str
    category: str
    record_count: int
    earliest: datetime | None = None
    latest: datetime | None = None


class EvidenceItem(BaseModel):
    record_id: str
    platform: str
    category: str
    title: str
    timestamp: datetime | None = None
    source: str
    claim_kind: ClaimKind = ClaimKind.OBSERVED


class PrivacySnapshot(BaseModel):
    generated_at: datetime
    platforms: list[str]
    total_records: int
    coverage: list[CoverageItem]
    common_hostnames: list[tuple[str, int]]
    overlapping_hostnames: list[str]
    overlapping_devices: list[str] = Field(default_factory=list)
    overlapping_services: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem]
    omissions: dict[str, list[str]]


class CalculatedFact(BaseModel):
    """A deterministic local calculation that can be cited by a model."""

    model_config = ConfigDict(frozen=True)

    fact_id: str
    scope: Literal["archive", "matching"]
    scope_definition: str
    metric: str
    value: str | int | float
    dimensions: dict[str, str] = Field(default_factory=dict)
    provenance: str
    transmittable: bool = True


class QueryIntent(StrEnum):
    ARCHIVE_OVERVIEW = "archive_overview"
    DATE_LOOKUP = "date_lookup"
    FACET = "facet"
    TREND = "trend"
    COMPARISON = "comparison"
    FULL_TEXT = "full_text"
    RECORD_DETAIL = "record_detail"
    CLARIFICATION = "clarification"
    UNSUPPORTED = "unsupported"


class QueryOperation(StrEnum):
    ARCHIVE_OVERVIEW = "archive_overview"
    DATE_RANGE = "date_range"
    FACET_COUNTS = "facet_counts"
    TIME_BUCKETS = "time_buckets"
    PLATFORM_COMPARISON = "platform_comparison"
    CATEGORY_COMPARISON = "category_comparison"
    FULL_TEXT_MATCH = "full_text_match"
    RECORD_BY_ID = "record_by_id"
    COVERAGE_ONLY = "coverage_only"


class QueryFacet(StrEnum):
    SERVICE = "service"
    DEVICE = "device"
    HOSTNAME = "hostname"
    ACTIVITY_TYPE = "activity_type"


class QueryStatus(StrEnum):
    OK = "ok"
    NO_MATCHING_RECORDS = "no_matching_records"
    MATCHING_DATA_ABSENT = "matching_data_absent"
    PRODUCT_UNSUPPORTED = "product_present_but_unsupported"
    NOT_PRESENT = "product_or_category_not_present"
    CLARIFICATION_REQUIRED = "clarification_required"
    UNSUPPORTED = "unsupported"


class QueryAssumption(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str


class CoverageNote(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    platform: str | None = None
    category: str | None = None
    product: str | None = None


class QueryScope(BaseModel):
    model_config = ConfigDict(frozen=True)

    platforms: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    facet: QueryFacet | None = None
    text_terms: tuple[str, ...] = ()
    record_ids: tuple[str, ...] = ()
    start_utc: datetime | None = None
    end_utc: datetime | None = None
    timezone: str | None = None


class QueryPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan_id: str
    question: str
    intent: QueryIntent
    operation: QueryOperation
    scope: QueryScope = Field(default_factory=QueryScope)
    assumptions: tuple[QueryAssumption, ...] = ()
    clarification: str | None = None

    @model_validator(mode="after")
    def validate_operation_scope(self) -> Self:
        expected_operations = {
            QueryIntent.ARCHIVE_OVERVIEW: frozenset({QueryOperation.ARCHIVE_OVERVIEW}),
            QueryIntent.DATE_LOOKUP: frozenset({QueryOperation.DATE_RANGE}),
            QueryIntent.FACET: frozenset({QueryOperation.FACET_COUNTS}),
            QueryIntent.TREND: frozenset({QueryOperation.TIME_BUCKETS}),
            QueryIntent.COMPARISON: frozenset(
                {
                    QueryOperation.PLATFORM_COMPARISON,
                    QueryOperation.CATEGORY_COMPARISON,
                }
            ),
            QueryIntent.FULL_TEXT: frozenset({QueryOperation.FULL_TEXT_MATCH}),
            QueryIntent.RECORD_DETAIL: frozenset({QueryOperation.RECORD_BY_ID}),
            QueryIntent.CLARIFICATION: frozenset({QueryOperation.COVERAGE_ONLY}),
            QueryIntent.UNSUPPORTED: frozenset({QueryOperation.COVERAGE_ONLY}),
        }
        if self.operation not in expected_operations[self.intent]:
            raise ValueError(
                f"Operation {self.operation.value} is incompatible with intent {self.intent.value}."
            )

        allowed_scope_fields = {
            QueryOperation.ARCHIVE_OVERVIEW: frozenset(),
            QueryOperation.DATE_RANGE: frozenset({"start_utc", "end_utc", "timezone"}),
            QueryOperation.FACET_COUNTS: frozenset({"categories", "facet"}),
            QueryOperation.TIME_BUCKETS: frozenset(),
            QueryOperation.PLATFORM_COMPARISON: frozenset({"platforms"}),
            QueryOperation.CATEGORY_COMPARISON: frozenset({"categories"}),
            QueryOperation.FULL_TEXT_MATCH: frozenset({"text_terms"}),
            QueryOperation.RECORD_BY_ID: frozenset({"record_ids"}),
            QueryOperation.COVERAGE_ONLY: frozenset(),
        }
        populated_scope_fields = {
            field_name for field_name, value in self.scope if value is not None and value != ()
        }
        unexpected = populated_scope_fields - allowed_scope_fields[self.operation]
        if unexpected:
            rendered = ", ".join(sorted(unexpected))
            raise ValueError(
                f"Operation {self.operation.value} does not accept scope fields: {rendered}."
            )

        if self.operation == QueryOperation.DATE_RANGE:
            start = self.scope.start_utc
            end = self.scope.end_utc
            if start is None or end is None:
                raise ValueError("Date-range plans require both start_utc and end_utc.")
            if start.utcoffset() is None or end.utcoffset() is None:
                raise ValueError("Date-range bounds must be timezone-aware.")
            if start >= end:
                raise ValueError("Date-range start_utc must be earlier than end_utc.")
        elif self.operation == QueryOperation.FACET_COUNTS and self.scope.facet is None:
            raise ValueError("Facet-count plans require a facet.")
        elif self.operation == QueryOperation.PLATFORM_COMPARISON and len(self.scope.platforms) < 2:
            raise ValueError("Platform-comparison plans require at least two platforms.")
        elif (
            self.operation == QueryOperation.CATEGORY_COMPARISON and len(self.scope.categories) < 2
        ):
            raise ValueError("Category-comparison plans require at least two categories.")
        elif self.operation == QueryOperation.FULL_TEXT_MATCH and not self.scope.text_terms:
            raise ValueError("Full-text plans require at least one text term.")
        elif self.operation == QueryOperation.RECORD_BY_ID and not self.scope.record_ids:
            raise ValueError("Record-detail plans require at least one record ID.")
        return self


class ManifestEntry(BaseModel):
    """Local-only metadata for one safely inventoried archive entry."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    platform: str
    internal_path: str
    product: str
    extension: str
    compressed_size: int
    uncompressed_size: int
    nested_archive: bool
    parser_supported: bool


class ManifestGroup(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    entry_count: int
    compressed_size: int
    uncompressed_size: int
    parser_supported_entries: int


class ArchiveManifest(BaseModel):
    """Complete local inventory. Entry paths and source IDs must not be serialized to models."""

    model_config = ConfigDict(frozen=True)

    entries: tuple[ManifestEntry, ...] = ()
    products: tuple[ManifestGroup, ...] = ()
    formats: tuple[ManifestGroup, ...] = ()
    entry_count: int = 0
    compressed_size: int = 0
    uncompressed_size: int = 0
    nested_archive_count: int = 0
    parser_supported_entries: int = 0
    parser_unsupported_entries: int = 0


class QueryResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan: QueryPlan
    status: QueryStatus
    total_records: int
    matching_records: int
    facts: tuple[CalculatedFact, ...] = ()
    evidence: tuple[NormalizedRecord, ...] = ()
    coverage_notes: tuple[CoverageNote, ...] = ()
    assumptions: tuple[QueryAssumption, ...] = ()
    message: str | None = None


class AIClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=4_000)
    kind: AIClaimKind
    record_ids: list[str] = Field(default_factory=list, max_length=100)
    fact_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Claim text cannot be empty.")
        return stripped


class AIAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[AIClaim] = Field(max_length=50)


JsonObject = dict[str, Any]
