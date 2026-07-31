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
current question + bounded previous reference
                    ↓
       deterministic local resolver
          ↙                    ↘
fresh typed plan          clarification plan
          ↓                    ↓
complete local query       local response
          ↓                    (zero model calls)
current result + minimal referent context
          ↓
immutable request preparation → preview → per-turn cloud authorization
```

`ConversationState` is immutable and holds at most one `ConversationTurn`.
That turn contains the previous plan, a hash-derived result ID, at most 100
fact IDs, at most 100 record IDs, an optional unambiguous record ID, and active
platform/category/date/facet scope. It does not contain a model answer, raw
archive values, or a transcript. The session retains the immediately previous
question as part of its typed query plan. It does not retain older turns, model
responses, or a full transcript. `:reset` drops the turn. Changing `:timezone`
also drops it so a date referent cannot silently cross timezone assumptions.

The resolver recognizes only explicit forms such as “on that day,” “which of
those were most common?”, “compare that with Google,” “did that record include
media?”, and “show me the previous result again.” A singular record or platform
referent is resolved only when the prior result identifies exactly one. An
ambiguous form becomes a fresh local clarification plan and never reaches a
model.

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
