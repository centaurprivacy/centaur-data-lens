# Privacy model

Centaur Data Lens is designed to work without a Centaur account or telemetry.

## Local analysis

The `platforms`, `guide`, `inspect`, and deterministic `report` workflows do not
open network connections. Export contents are read locally. Normalized records
are kept in a randomly named, user-only temporary SQLite database and deleted
when the session ends.

Deletion is best-effort. Filesystems, SSD wear levelling, backups, swap, crash
dumps, and snapshots can retain data after a file is removed. Use full-disk
encryption and protect the original export.

## Saved output

Reports and diagnostics are saved only to a path the user chooses. Reports can
contain sensitive derived information and cited evidence. Diagnostics contain
schema version and per-platform category counts—not record values or paths.

## Optional AI

Q&A first calculates archive-wide and question-matching facts in the local
ephemeral index. It then selects a diversified evidence set within a 256 KiB
provider request-body cap. Query execution defaults to 100 evidence candidates,
but that is a caller-configurable selection target rather than a transmission
limit; the immutable request-body size is the final transmission boundary.

The natural-language compiler selects one primary allowlisted operation. It does
not compose mixed scopes: additional text in a facet, date, trend, overview, or
comparison question may not narrow the selected operation. Callers that require
intersections must construct an explicit supported plan or ask one scoped
question at a time. When no records match the selected plan, Centaur returns a
deterministic local no-match answer and makes no model request. Counts and other
derived facts describe one person's export and must themselves be treated as
personal data.

Conversational Q&A validates and indexes the explicitly selected sources once,
then reuses that ephemeral local index. Every turn compiles and executes a new
deterministic query over the complete applicable scope. A follow-up is never
answered from a previous model response or previous bounded evidence selection.
Clarifications and no-match results that can be stated locally make zero model
calls.

In-memory conversation state is bounded to one previous typed plan, one derived
result ID, at most 100 fact IDs, at most 100 record IDs, active scope, timezone,
and a locally proven unambiguous referent. It does not retain model responses or
full transcripts and is never persisted. Changing the timezone clears prior
referents. `:reset` clears them explicitly.

Ollama is restricted to the local loopback interface and is the recommended
provider. A validated OpenAI-compatible loopback endpoint is also local. Local
models may receive calculated facts and selected normalized record values
without cloud authorization.

Bring-your-own-key cloud adapters are an advanced escape hatch. Before each
cloud question, the CLI shows the provider, model, destination, exact request-body
size, archive and matching counts, fact and evidence counts, categories,
transmitted field names, and detected sensitivity classes. The reported byte
count covers the exact immutable JSON request body, including the system prompt,
static structured-output schema, model options, and provider envelope; HTTP
headers and transport framing are not included. It warns that the question,
calculated facts, and records may be personal data. Interactive use requires
typing `SEND PERSONAL DATA` for that question; non-interactive use requires
explicit provider selection and `--allow-cloud`.

Interactive cloud chat always requires typed authorization for each
transmitted turn; one-shot `--allow-cloud` does not carry into chat and consent
is never cached. Its preview additionally lists the exact conversation-state
fields included and the active timezone/scope assumptions. Local deterministic
follow-up resolution happens before authorization, but it makes no model,
planning, classification, summarization, or embedding request. The exact
previewed immutable bytes are the bytes passed to the adapter after consent.

No model, embedding, classification, or planning request occurs before that
authorization. Authorization is never reused for another question. The exact
previewed request-body bytes are transmitted without rebuilding them. Cloud
structured-output schemas are static and contain no record or fact IDs; citation
IDs are validated locally after the response. Model request bodies exclude
source paths, archive identifiers, internal filenames, complete archives, media,
API keys, and unsupported categories.

Cloud providers may retain or use requests under their own terms. The privacy
promise is therefore **local by default with explicit per-question cloud
disclosure**, not anonymity and not a claim that cloud models never receive
personal information. Centaur communicates directly with the selected provider
and does not receive the key, prompt, response, or export.

The conversational product boundary is: **An ephemeral local analysis session
that re-queries the selected exports for each turn; local by default, with
explicit per-turn cloud disclosure.**

Centaur Data Lens has no analytics, crash reporter, automatic updater, or
background service.
