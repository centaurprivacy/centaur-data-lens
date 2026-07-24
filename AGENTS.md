# Agent guidance

- Use `uv run` for Python commands.
- Never commit or print real personal exports, API keys, report bodies, or
  unredacted diagnostics.
- All fixtures must be synthetic.
- Local deterministic workflows must remain network-free.
- Archive paths, JSON values, model output, and filenames are untrusted.
- Do not add Centaur accounts, hosted inference, telemetry, executable bundles,
  or persistent workspaces without an explicit scope change.
- Run lint, format checks, strict typing, tests, and package builds before opening a PR.
