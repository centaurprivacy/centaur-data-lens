# Architecture

```text
explicit platform choice
          ↓
safe ArchiveReader → local manifest + provider parser → NormalizedRecord
                                                        ↓
                                             ephemeral SQLite + FTS5
                                               ↙                ↘
                                  deterministic report    typed QueryPlan
                                                               ↓
                                                     complete local query
                                                               ↓
                                                    QueryResult facts +
                                                 request-size-bounded evidence
                                                               ↓
                                                    immutable prepared question
                                                   ↓
                                      loopback model (default) or
                                      authorized cloud adapter (advanced)
```

The wizard and direct commands call the same application services. Platform
selection is always explicit; parser validation catches mismatches but does not
infer or switch platforms.

## Extension points

`PlatformDefinition` owns display copy, export guidance, supported categories,
exclusions, validation, and its parser. A new platform is registered in one
place.

`AnalysisSession` calculates fixed archive and matching facts over the complete
local query scope. It selects a configurable evidence set diversified across
platform, category, time, and repeated value groups; final model preparation is
bounded by the immutable request-body size. Calculated facts and records have
separate citation namespaces.

The local archive manifest and typed query contracts are documented in
[`local-query-engine.md`](local-query-engine.md). Query planning and execution
are deterministic and network-free; plans select fixed operations and pass only
parameters to SQLite.

`ModelAdapter` exposes destination and locality metadata, deterministic
request-body construction, and one completion operation. A prepared question
freezes the exact provider request-body bytes, preview, and valid fact/record IDs
before authorization. Cloud schemas are static; citation IDs stay in message
content and are validated locally. No-match questions return a deterministic
local answer and never invoke an adapter. Adapters receive no source paths,
archive identifiers, internal filenames, or tools.

## Conversational orchestration

`chat` adds orchestration around the same `AnalysisSession`, `QueryPlan`,
`QueryResult`, preparation, and claim-validation interfaces used by one-shot
`ask`. Source validation and indexing happen once. The provider adapter and
model selection are also created once. Every ordinary question and every safely
resolved follow-up executes a newly identified plan against the complete local
index.

```text
current question + bounded recent typed context
                    ↓
 deterministic local resolver/compiler
          ↙                    ↘
fresh typed plan          clarification plan
          ↓                    ↓
complete local query       local response
          ↓                    (zero model calls)
current result + bounded structured context
          ↓
immutable request preparation → preview → per-turn cloud authorization
```

`ConversationState` is immutable and holds at most eight `ConversationTurn`
objects. Each contains a typed plan, a hash-derived result ID, at most 100 fact
IDs, at most 100 record IDs, an optional unambiguous record ID, and active
platform/category/date/facet scope. Typed plans retain their user questions so
later turns have bounded conversational context. The state does not contain a
model answer, raw archive values, or a full transcript. `:reset` drops all turns.
Changing `:timezone` also drops them so a date referent cannot silently cross
timezone assumptions.

The resolver recognizes only explicit forms such as “on that day,” “which of
those were most common?”, “compare that with Google,” “did that record include
media?”, “what does this mean?”, and “show me the previous result again.”
Interpretive follow-ups re-run the prior typed scope before model explanation; a
singular record or platform
referent is resolved only when the prior result identifies exactly one. An
ambiguous form becomes a fresh local clarification plan and never reaches a
model.

The compiler uses literal full-text matching only for explicit lookup wording.
Unrecognized open-ended questions select the archive overview, allowing the
model to answer conversationally from local aggregate facts and bounded current
evidence instead of producing a misleading empty search result.

Every model request includes at most eight prior structured turn summaries:
question, plan/result IDs, intent, operation, and value-free active scope. It
does not resend evidence from old results or any previous model response.

Archive overviews prioritize deterministic profile facts—record/category
counts, distinct-value counts, and present/missing field coverage—and limit raw
illustrations to 12 current records. The model receives each calculated fact as
an opaque citation ID, scope, and plain-language meaning rather than internal
metric/schema fields. Overview answers must lead with an aggregate, identify the
represented category, disclose relevant time-coverage limits, and contain no
more than four claims. Invalid local output gets one corrective retry; a second
failure returns a locally constructed, citation-valid summary. Interpretive
follow-ups have a separate recovery form explaining what records represent and
what cannot safely be inferred.

Ordinal and subjective follow-ups have distinct contracts. A request for the
first item resolves the first bounded record ID from the immediately previous
result and executes `RECORD_BY_ID`. A request for a surprising, unusual, or
standout item re-runs the previous scope but enters item-selection mode: one or
two claims, one normalized chosen-record citation, and an explicit statement
that the judgment covers only the evidence examples selected for that turn.

## Ephemeral state

Each process creates a random, user-only temporary directory carrying a marker.
SQLite uses `secure_delete` and the DELETE journal mode. Normal shutdown,
handled cancellation, and exceptions unwinding through the session context close
and remove it. Abrupt termination, including `SIGKILL` or an unhandled
`SIGTERM`, can leave the marked session directory behind. On a later startup,
the CLI removes marked sessions older than 24 hours. See `PRIVACY.md` for the
limits of best-effort deletion.

Conversation state exists only in process memory. It is discarded with the
session on normal exit, `:exit`, EOF, handled cancellation, and exception
unwinding.
