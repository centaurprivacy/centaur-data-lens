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
```

For non-interactive cloud use, the provider must be selected explicitly and
`--allow-cloud` must be supplied for that one request. Cloud payloads can contain
the question, personal derived statistics, and selected raw records. Source
paths, archive identifiers, filenames, and API keys are never included. The
preview measures the immutable provider request body, including the system
prompt, static response schema, and provider envelope. Questions with no
matching records are answered locally without a model request.

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
