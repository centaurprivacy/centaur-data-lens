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
- Cloud AI is optional, uses your own API key, and requires an explicit
  transmission confirmation.
- Reports are written only when you provide an output path.

See [PRIVACY.md](PRIVACY.md) and [THREAT_MODEL.md](THREAT_MODEL.md) before using
the tool with a sensitive export.

## Install and run

Python 3.11 or newer is required.

```bash
uvx centaur-data-lens
```

For a persistent installation:

```bash
uv tool install centaur-data-lens
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
