from __future__ import annotations

import json
import re
from datetime import datetime
from hashlib import sha256
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

from centaur_data_lens.models import (
    QueryIntent,
    QueryOperation,
    QueryPlan,
    QueryResult,
    QueryScope,
    QueryStatus,
)
from centaur_data_lens.query import build_query_plan, compile_query

_MAX_REFERENCE_IDS = 100
_FOLLOW_UP_PATTERNS = {
    "day": re.compile(r"\b(?:on )?that day\b", re.IGNORECASE),
    "facet": re.compile(
        r"\bwhich of those(?: (?:was|were|is|are))?(?: the)? most common\b",
        re.IGNORECASE,
    ),
    "comparison": re.compile(r"\bcompare that with (?P<platform>google|meta)\b", re.IGNORECASE),
    "record": re.compile(r"\b(?:did|does) that record\b", re.IGNORECASE),
    "previous": re.compile(
        r"\b(?:show|display)(?: me)? the previous result again\b",
        re.IGNORECASE,
    ),
}


class ResultReference(BaseModel):
    """Bounded identifiers retained from one deterministic query result."""

    model_config = ConfigDict(frozen=True)

    result_id: str
    fact_ids: tuple[str, ...] = Field(default=(), max_length=_MAX_REFERENCE_IDS)
    record_ids: tuple[str, ...] = Field(default=(), max_length=_MAX_REFERENCE_IDS)
    unambiguous_record_id: str | None = None


class ActiveScope(BaseModel):
    """The small, value-free scope needed to resolve explicit follow-ups."""

    model_config = ConfigDict(frozen=True)

    platforms: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    start_utc: datetime | None = None
    end_utc: datetime | None = None
    timezone: str | None = None
    facet: str | None = None


class ConversationTurn(BaseModel):
    """One prior plan and its bounded result reference; never a transcript."""

    model_config = ConfigDict(frozen=True)

    plan: QueryPlan
    result: ResultReference
    scope: ActiveScope


class ConversationState(BaseModel):
    """Immutable, bounded, in-memory conversation state."""

    model_config = ConfigDict(frozen=True)

    previous_turn: ConversationTurn | None = None
    timezone: str | None = None
    turn_count: int = Field(default=0, ge=0)

    def reset(self) -> ConversationState:
        return self.model_copy(update={"previous_turn": None})

    def with_timezone(self, timezone: str) -> ConversationState:
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {timezone}") from exc
        return self.model_copy(update={"timezone": timezone, "previous_turn": None})

    def after(self, result: QueryResult) -> ConversationState:
        if result.status != QueryStatus.OK:
            return self
        reference = _result_reference(result)
        scope = _active_scope(result)
        return self.model_copy(
            update={
                "previous_turn": ConversationTurn(
                    plan=result.plan,
                    result=reference,
                    scope=scope,
                ),
                "turn_count": self.turn_count + 1,
            }
        )


class ResolvedContext(BaseModel):
    """Minimal prior-turn context that may be included in a model request."""

    model_config = ConfigDict(frozen=True)

    previous_result_id: str
    referent_kind: Literal["day", "facet", "platform", "record", "previous_result"]
    referent_value: str


class ResolvedTurn(BaseModel):
    """A fresh plan plus optional explanation of a locally resolved referent."""

    model_config = ConfigDict(frozen=True)

    plan: QueryPlan
    context: ResolvedContext | None = None


def _result_reference(result: QueryResult) -> ResultReference:
    fact_ids = tuple(dict.fromkeys(fact.fact_id for fact in result.facts))[:_MAX_REFERENCE_IDS]
    record_ids = tuple(dict.fromkeys(record.record_id for record in result.evidence))[
        :_MAX_REFERENCE_IDS
    ]
    identity = json.dumps(
        {
            "plan_id": result.plan.plan_id,
            "status": result.status.value,
            "total_records": result.total_records,
            "matching_records": result.matching_records,
            "fact_ids": fact_ids,
            "record_ids": record_ids,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    unambiguous = record_ids[0] if result.matching_records == 1 and len(record_ids) == 1 else None
    return ResultReference(
        result_id=f"result-{sha256(identity.encode()).hexdigest()[:24]}",
        fact_ids=fact_ids,
        record_ids=record_ids,
        unambiguous_record_id=unambiguous,
    )


def _active_scope(result: QueryResult) -> ActiveScope:
    plan_scope = result.plan.scope
    evidence_platforms = tuple(sorted({record.platform for record in result.evidence}))
    evidence_categories = tuple(sorted({record.category for record in result.evidence}))
    return ActiveScope(
        platforms=plan_scope.platforms or evidence_platforms,
        categories=plan_scope.categories or evidence_categories,
        start_utc=plan_scope.start_utc,
        end_utc=plan_scope.end_utc,
        timezone=plan_scope.timezone,
        facet=plan_scope.facet.value if plan_scope.facet else None,
    )


def _clarification(question: str, message: str) -> ResolvedTurn:
    return ResolvedTurn(
        plan=build_query_plan(
            question=question,
            intent=QueryIntent.CLARIFICATION,
            operation=QueryOperation.COVERAGE_ONLY,
            clarification=message,
        )
    )


def _repeat_plan(
    question: str,
    previous: ConversationTurn,
    *,
    kind: Literal["day", "facet", "previous_result"],
    value: str,
) -> ResolvedTurn:
    plan = build_query_plan(
        question=question,
        intent=previous.plan.intent,
        operation=previous.plan.operation,
        scope=previous.plan.scope,
        assumptions=previous.plan.assumptions,
    )
    return ResolvedTurn(
        plan=plan,
        context=ResolvedContext(
            previous_result_id=previous.result.result_id,
            referent_kind=kind,
            referent_value=value,
        ),
    )


def resolve_turn(question: str, state: ConversationState) -> ResolvedTurn:
    """Resolve only explicit, unambiguous follow-ups without a model."""

    normalized = " ".join(question.strip().split())
    previous = state.previous_turn

    if _FOLLOW_UP_PATTERNS["day"].search(normalized):
        if previous is None or previous.plan.operation != QueryOperation.DATE_RANGE:
            return _clarification(
                normalized,
                '"That day" is ambiguous. Ask with an explicit calendar date first.',
            )
        timezone = previous.scope.timezone or "the selected timezone"
        assert previous.scope.start_utc is not None
        local_date = (
            previous.scope.start_utc.astimezone(ZoneInfo(timezone)).date()
            if previous.scope.timezone
            else previous.scope.start_utc.date()
        )
        value = f"{local_date.isoformat()} ({timezone})"
        return _repeat_plan(normalized, previous, kind="day", value=value)

    if _FOLLOW_UP_PATTERNS["facet"].search(normalized):
        if previous is None or previous.plan.operation != QueryOperation.FACET_COUNTS:
            return _clarification(
                normalized,
                '"Those" is ambiguous. Ask for a specific facet, such as services or devices.',
            )
        facet = previous.scope.facet or "facet"
        return _repeat_plan(normalized, previous, kind="facet", value=facet)

    comparison = _FOLLOW_UP_PATTERNS["comparison"].search(normalized)
    if comparison:
        requested_platform = comparison.group("platform").lower()
        prior_platforms = previous.scope.platforms if previous else ()
        if (
            previous is None
            or len(prior_platforms) != 1
            or prior_platforms[0] == requested_platform
        ):
            return _clarification(
                normalized,
                '"That" does not identify one other platform. Name both platforms to compare.',
            )
        platforms = (prior_platforms[0], requested_platform)
        return ResolvedTurn(
            plan=build_query_plan(
                question=normalized,
                intent=QueryIntent.COMPARISON,
                operation=QueryOperation.PLATFORM_COMPARISON,
                scope=QueryScope(platforms=platforms),
            ),
            context=ResolvedContext(
                previous_result_id=previous.result.result_id,
                referent_kind="platform",
                referent_value=prior_platforms[0],
            ),
        )

    if _FOLLOW_UP_PATTERNS["record"].search(normalized):
        record_id = previous.result.unambiguous_record_id if previous else None
        if previous is None or record_id is None:
            return _clarification(
                normalized,
                '"That record" is ambiguous. Name or cite one record ID.',
            )
        return ResolvedTurn(
            plan=build_query_plan(
                question=normalized,
                intent=QueryIntent.RECORD_DETAIL,
                operation=QueryOperation.RECORD_BY_ID,
                scope=QueryScope(record_ids=(record_id,)),
            ),
            context=ResolvedContext(
                previous_result_id=previous.result.result_id,
                referent_kind="record",
                referent_value=record_id,
            ),
        )

    if _FOLLOW_UP_PATTERNS["previous"].search(normalized):
        if previous is None:
            return _clarification(normalized, "There is no previous result in this session.")
        return _repeat_plan(
            normalized,
            previous,
            kind="previous_result",
            value=previous.result.result_id,
        )

    return ResolvedTurn(plan=compile_query(normalized, timezone=state.timezone))
