from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
