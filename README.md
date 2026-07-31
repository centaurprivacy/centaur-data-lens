# Centaur Data Lens

Explore the personal data disclosed in your Google and Meta exports—locally,
without creating an account or uploading an archive to Centaur.

> **Alpha software:** export formats change frequently. Centaur Data Lens reports
> what it can observe in the files you provide; it does not claim to show
> everything a platform knows about you and is not legal advice.

## Privacy promises

- Deterministic analysis is local and makes no network requests.
- There is no telemetry and no Centaur account.
- Temporary indexes are deleted when the process exits.
- Conversational analysis runs a fresh deterministic local query for every
  question and retains only one bounded prior result reference.
- Q&A uses local archive-wide calculations plus a bounded evidence set; Ollama
  on the local loopback interface is the recommended provider.
- Cloud AI is an advanced option that uses your own API key and transmits
  personal data only after an exact preview and per-question authorization.
- Reports are written only when you provide an output path.

See [PRIVACY.md](PRIVACY.md) and [THREAT_MODEL.md](THREAT_MODEL.md) before using
the tool with a sensitive export.

## Install and run

Python 3.11 or newer is required.

Until the first PyPI release, run Centaur Data Lens from a source checkout:

```bash
git clone https://github.com/centaurprivacy/centaur-data-lens.git
cd centaur-data-lens
uv run centaur-data-lens
```

For a persistent installation:

```bash
uv tool install .
centaur-data-lens
```

## Direct commands

```bash
centaur-data-lens platforms
centaur-data-lens guide google
centaur-data-lens inspect google ~/Downloads/takeout.zip
centaur-data-lens inspect meta ~/Downloads/facebook-export.zip
centaur-data-lens report \
  --source google=~/Downloads/takeout.zip \
  --source meta=~/Downloads/facebook-export.zip \
  --format html \
  --output ./privacy-report.html
centaur-data-lens ask \
  --source google=~/Downloads/takeout.zip \
  --provider ollama
centaur-data-lens chat \
  --source google=~/Downloads/takeout.zip \
  --source meta=~/Downloads/facebook-export.zip \
  --provider ollama \
  --timezone America/Los_Angeles
```

`chat` validates and indexes the selected exports once, then keeps that
`AnalysisSession` alive until `:exit`, EOF, cancellation, or an exception. Each
question—including a resolved follow-up—compiles and executes a new typed
`QueryPlan` over the original ephemeral index. It never answers from a saved
model response or a previous evidence sample. Use `:help`, `:coverage`, `:scope`,
`:timezone [ZONE]`, `:reset`, and `:exit` inside the session.

Conversation memory contains only the previous plan, a derived result ID,
bounded valid fact/record IDs, active scope, timezone, and an unambiguous
referent. The session retains the immediately previous question as part of its
typed query plan. It does not retain older turns, model responses, or a full
transcript. Conversation state is never written to disk, and archive values are
not retained in it.

For non-interactive cloud use, the provider must be selected explicitly and
`--allow-cloud` must be supplied for that one request. Cloud payloads can contain
the question, personal derived statistics, and selected raw records. Source
paths, archive identifiers, filenames, and API keys are never included. The
preview measures the immutable provider request body, including the system
prompt, static response schema, and provider envelope. Questions with no
matching records are answered locally without a model request.

Cloud-backed chat has no reusable `--allow-cloud` switch. Before every
transmitted turn it shows the complete immutable request-body size, local and
matching counts, fact/evidence counts, categories, fields, sensitivity classes,
included conversation-state fields, timezone, and scope assumptions. The user
must type `SEND PERSONAL DATA` again for that turn. Cancellation, clarification,
and local no-match responses make zero model calls.

Centaur Data Lens is **an ephemeral local analysis session that re-queries the
selected exports for each turn; local by default, with explicit per-turn cloud
disclosure.**

See the [synthetic chat transcript](docs/synthetic-chat-transcript.md) for a
multi-turn example with citations, local referent resolution, timezone
disclosure, and fail-closed cloud authorization.

Google and Meta exports must be requested in JSON format. The CLI explains the
currently supported categories with `guide google` and `guide meta`.

## Development

```bash
uv sync --all-groups
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

Only synthetic export fixtures may be committed. Never post an archive, API
key, unredacted diagnostic, or report in an issue.
