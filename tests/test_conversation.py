from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

from centaur_data_lens.ai import answer_question, prepare_question
from centaur_data_lens.analysis import AnalysisSession, SourceSpec, analyze_sources
from centaur_data_lens.conversation import ConversationState, resolve_turn
from centaur_data_lens.models import QueryIntent, QueryOperation, QueryStatus


def test_overview_then_date_and_record_detail_are_fresh_plans(
    google_export,
) -> None:
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        state = ConversationState(timezone="America/Los_Angeles")

        overview = resolve_turn("summarize this export", state)
        overview_result = session.execute_query(overview.plan)
        state = state.after(overview_result)

        dated = resolve_turn("what happened on March 31, 2025?", state)
        dated_result = session.execute_query(dated.plan)
        state = state.after(dated_result)

        detail = resolve_turn("did that record include media?", state)

    assert overview.plan.intent == QueryIntent.ARCHIVE_OVERVIEW
    assert dated.plan.intent == QueryIntent.DATE_LOOKUP
    assert dated.plan.plan_id != overview.plan.plan_id
    assert dated_result.matching_records == 1
    assert detail.plan.operation == QueryOperation.RECORD_BY_ID
    assert detail.plan.plan_id != dated.plan.plan_id
    assert detail.context is not None
    assert detail.context.referent_kind == "record"


def test_on_that_day_reexecutes_previous_date_scope(google_export) -> None:
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        first = resolve_turn(
            "what happened on January 1, 2025?",
            ConversationState(timezone="America/Los_Angeles"),
        )
        state = ConversationState(timezone="America/Los_Angeles").after(
            session.execute_query(first.plan)
        )
        follow_up = resolve_turn("which activities were on that day?", state)
        second_result = session.execute_query(follow_up.plan)

    assert follow_up.plan.operation == QueryOperation.DATE_RANGE
    assert follow_up.plan.scope == first.plan.scope
    assert follow_up.plan.plan_id != first.plan.plan_id
    assert second_result.status == QueryStatus.OK
    assert follow_up.context is not None
    assert follow_up.context.referent_kind == "day"


def test_facet_follow_up_and_previous_result_are_fresh(google_export) -> None:
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        first = resolve_turn("which services were present?", ConversationState())
        state = ConversationState().after(session.execute_query(first.plan))
        most_common = resolve_turn("which of those were most common?", state)
        state = state.after(session.execute_query(most_common.plan))
        previous = resolve_turn("show me the previous result again", state)

    assert most_common.plan.operation == QueryOperation.FACET_COUNTS
    assert most_common.plan.plan_id != first.plan.plan_id
    assert most_common.context is not None
    assert most_common.context.referent_kind == "facet"
    assert previous.plan.operation == QueryOperation.FACET_COUNTS
    assert previous.plan.plan_id != most_common.plan.plan_id


def test_ambiguous_referents_create_local_clarification_plans() -> None:
    for question in (
        "what was on that day?",
        "which of those were most common?",
        "compare that with Google",
        "did that record include media?",
        "show me the previous result again",
    ):
        resolved = resolve_turn(question, ConversationState())
        assert resolved.plan.intent == QueryIntent.CLARIFICATION
        assert resolved.plan.operation == QueryOperation.COVERAGE_ONLY
        assert resolved.plan.clarification


def test_state_is_bounded_and_reset_preserves_timezone(google_export) -> None:
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        state = ConversationState(timezone="UTC")
        for question in ("summarize this export", "which services were present?") * 80:
            result = session.execute_query(resolve_turn(question, state).plan)
            state = state.after(result)

    assert state.turn_count == 160
    assert state.previous_turn is not None
    assert len(state.previous_turn.result.fact_ids) <= 100
    assert len(state.previous_turn.result.record_ids) <= 100
    assert "summarize this export" not in state.model_dump_json()
    reset = state.reset()
    assert reset.previous_turn is None
    assert reset.timezone == "UTC"


def test_timezone_boundary_is_preserved_in_follow_up(google_export) -> None:
    state = ConversationState(timezone="America/Los_Angeles")
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        first = resolve_turn("what happened on January 1, 2025?", state)
        state = state.after(session.execute_query(first.plan))
        follow_up = resolve_turn("what else happened on that day?", state)

    assert first.plan.scope.start_utc == datetime(2025, 1, 1, 8, tzinfo=UTC)
    assert first.plan.scope.end_utc == datetime(2025, 1, 2, 8, tzinfo=UTC)
    assert follow_up.plan.scope == first.plan.scope


def test_date_follow_up_retains_local_date_for_non_iana_timezone_label(
    google_export,
) -> None:
    non_iana_timezone = timezone(timedelta(hours=-8), "Pacific Standard Time")
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        first_plan = session.compile_query(
            "what happened on January 1, 2025?",
            timezone=non_iana_timezone,
        )
        state = ConversationState().after(session.execute_query(first_plan))

        first_follow_up = resolve_turn("what else happened on that day?", state)
        state = state.after(session.execute_query(first_follow_up.plan))
        second_follow_up = resolve_turn("show activities on that day", state)

    assert first_plan.scope.timezone == "Pacific Standard Time"
    assert state.previous_turn is not None
    assert state.previous_turn.scope.local_date is not None
    assert state.previous_turn.scope.local_date.isoformat() == "2025-01-01"
    assert first_follow_up.context is not None
    assert first_follow_up.context.referent_value == ("2025-01-01 (Pacific Standard Time)")
    assert second_follow_up.plan.operation == QueryOperation.DATE_RANGE
    assert second_follow_up.context is not None


def test_changing_timezone_clears_stale_date_referent() -> None:
    state = ConversationState(timezone="UTC").with_timezone("America/Los_Angeles")
    assert state.timezone == "America/Los_Angeles"
    assert state.previous_turn is None


def test_compare_that_with_google_uses_one_unambiguous_prior_platform(
    google_export,
    meta_export,
) -> None:
    state = ConversationState()
    with AnalysisSession() as session:
        analyze_sources(
            session,
            [
                SourceSpec("google", google_export),
                SourceSpec("meta", meta_export),
            ],
        )
        meta_result = session.execute_query(resolve_turn("Open source software", state).plan)
        state = state.after(meta_result)
        comparison = resolve_turn("compare that with Google", state)
        comparison_result = session.execute_query(comparison.plan)

    assert state.previous_turn is not None
    assert state.previous_turn.scope.platforms == ("meta",)
    assert comparison.plan.operation == QueryOperation.PLATFORM_COMPARISON
    assert comparison.plan.scope.platforms == ("meta", "google")
    assert comparison.context is not None
    assert comparison.context.referent_value == "meta"
    assert comparison_result.status == QueryStatus.OK


def test_prepared_follow_up_contains_only_minimal_context_and_frozen_bytes(
    google_export,
) -> None:
    class CapturingAdapter:
        name = "synthetic-cloud"
        model = "fixed"
        destination = "https://provider.invalid"
        is_local = False

        def __init__(self) -> None:
            self.sent: list[bytes] = []

        def build_request_body(self, *, system, user, answer_schema=None) -> bytes:
            return user.encode()

        def complete(self, *, request_body: bytes) -> str:
            self.sent.append(request_body)
            payload = json.loads(request_body)
            fact_id = payload["calculated_facts"][0]["fact_id"]
            return json.dumps(
                {
                    "claims": [
                        {
                            "text": "Synthetic cited answer.",
                            "kind": "calculated",
                            "record_ids": [],
                            "fact_ids": [fact_id],
                        }
                    ]
                }
            )

    adapter = CapturingAdapter()
    state = ConversationState(timezone="America/Los_Angeles")
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        first = resolve_turn("what happened on March 31, 2025?", state)
        state = state.after(session.execute_query(first.plan))
        follow_up = resolve_turn("did that record include media?", state)
        result = session.execute_query(follow_up.plan)
        prepared = prepare_question(
            result,
            adapter,
            conversation_context=follow_up.context,
            include_query_plan=True,
        )
        previewed_bytes = prepared.request_body
        answer_question(prepared, adapter=adapter, allow_cloud=True)

    payload = json.loads(previewed_bytes)
    assert adapter.sent == [previewed_bytes]
    assert len(previewed_bytes) == prepared.preview.payload_bytes
    assert set(payload["conversation_context"]) == {
        "previous_result_id",
        "referent_kind",
        "referent_value",
    }
    assert payload["query_plan"]["scope"]["record_ids"]
    assert "summarize this export" not in prepared.payload
    assert str(google_export) not in prepared.payload
    assert google_export.name not in prepared.payload
    assert "Takeout/" not in prepared.payload
    assert "archive-" not in prepared.payload
    assert "API_KEY" not in prepared.payload
    assert "source" not in prepared.preview.conversation_state_fields
    assert prepared.preview.conversation_state_fields == (
        "conversation_context.previous_result_id",
        "conversation_context.referent_kind",
        "conversation_context.referent_value",
    )


def test_clarification_preparation_is_local_and_never_calls_provider() -> None:
    class NoCallAdapter:
        name = "synthetic-cloud"
        model = "fixed"
        destination = "https://provider.invalid"
        is_local = False

        def build_request_body(self, **kwargs) -> bytes:
            raise AssertionError("Clarification must not build a provider request.")

        def complete(self, *, request_body: bytes) -> str:
            raise AssertionError("Clarification must not call the provider.")

    resolved = resolve_turn("did that record include media?", ConversationState())
    with AnalysisSession() as session:
        result = session.execute_query(resolved.plan)
        prepared = prepare_question(result, NoCallAdapter())
        answer = answer_question(prepared, adapter=NoCallAdapter(), allow_cloud=False)

    assert prepared.preview.will_transmit is False
    assert prepared.request_body == b""
    assert answer.claims[0].text == '"That record" is ambiguous. Name or cite one record ID.'
