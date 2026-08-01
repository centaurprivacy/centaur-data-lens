# Synthetic conversational CLI transcript

This abbreviated transcript uses generated Google and Meta fixtures and a fake
cloud adapter. No personal export, real API key, real archive ID, or real model
response is included. Counts and opaque citation IDs are illustrative.

```text
$ centaur-data-lens chat \
    --source google=/tmp/synthetic-google.zip \
    --source meta=/tmp/synthetic-meta.zip \
    --provider openai \
    --model synthetic-fixed \
    --timezone America/Los_Angeles

Ephemeral chat ready. Each turn runs a fresh local query; use :help for commands.

> summarize this export
Transmission preview
Provider: openai
Model: synthetic-fixed
Destination: https://api.openai.com
Complete provider request body: 18,420 bytes
Archive records analyzed locally: 9
Question-matching records: 9
Calculated facts: 12
Selected evidence records: 9
Conversation-state fields included: none
Timezone assumption: none
Scope assumptions: none
Type SEND PERSONAL DATA to authorize this turn: SEND PERSONAL DATA

The selected synthetic exports contain 9 supported records.
Evidence (calculated) — facts: fact-synthetic-overview

Supported activity appears in both the Google and Meta fixtures.
Evidence (observed) — records: record-synthetic-google-1, record-synthetic-meta-1

> what happened on July 20, 2026?
Transmission preview
Complete provider request body: 6,912 bytes
Archive records analyzed locally: 9
Question-matching records: 1
Calculated facts: 7
Selected evidence records: 1
Conversation-state fields included:
conversation_context.recent_turns.question,
conversation_context.recent_turns.plan_id,
conversation_context.recent_turns.result_id,
conversation_context.recent_turns.intent,
conversation_context.recent_turns.operation,
conversation_context.recent_turns.scope
Timezone assumption: America/Los_Angeles
Scope assumptions: Interpreted the requested calendar date in America/Los_Angeles.
Timezone disclosure: assuming America/Los_Angeles; UTC boundary checked from
2026-07-20T07:00:00+00:00 to 2026-07-21T07:00:00+00:00.
Type SEND PERSONAL DATA to authorize this turn: SEND PERSONAL DATA

One supported synthetic record matched July 20 in America/Los_Angeles.
Evidence (calculated) — facts: fact-synthetic-date-count

The matching fixture record is a synthetic Google activity.
Evidence (observed) — records: record-synthetic-google-2

> did that record include media?
Transmission preview
Complete provider request body: 5,844 bytes
Archive records analyzed locally: 9
Question-matching records: 1
Calculated facts: 7
Selected evidence records: 1
Conversation-state fields included:
conversation_context.recent_turns.question,
conversation_context.recent_turns.plan_id,
conversation_context.recent_turns.result_id,
conversation_context.recent_turns.intent,
conversation_context.recent_turns.operation,
conversation_context.recent_turns.scope,
conversation_context.resolved_referent.previous_result_id,
conversation_context.resolved_referent.referent_kind,
conversation_context.resolved_referent.referent_value
Timezone assumption: none
Scope assumptions: none
Type SEND PERSONAL DATA to authorize this turn: SEND PERSONAL DATA

The fresh record-detail query matched one record.
Evidence (calculated) — facts: fact-synthetic-record-count

The normalized synthetic record has no included media field. Export coverage
does not establish whether the platform holds media elsewhere.
Evidence (observed) — records: record-synthetic-google-2

> show me the previous result again
Transmission preview
Complete provider request body: 5,901 bytes
Conversation-state fields included:
conversation_context.recent_turns.question,
conversation_context.recent_turns.plan_id,
conversation_context.recent_turns.result_id,
conversation_context.recent_turns.intent,
conversation_context.recent_turns.operation,
conversation_context.recent_turns.scope,
conversation_context.resolved_referent.previous_result_id,
conversation_context.resolved_referent.referent_kind,
conversation_context.resolved_referent.referent_value
Type SEND PERSONAL DATA to authorize this turn: no
Cloud request cancelled; consent was not retained.

> :exit
Chat ended. Temporary analysis was deleted.
```

The third and fourth questions are resolved locally from bounded identifiers,
but each still creates and executes a fresh `QueryPlan`. The fourth turn is not
sent because authorization is required again and the typed response does not
match exactly.
