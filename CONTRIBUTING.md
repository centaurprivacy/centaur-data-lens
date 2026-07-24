# Contributing

Contributions are welcome through pull requests.

1. Use only synthetic fixtures.
2. Never include real export data, names, URLs, identifiers, messages, API
   keys, reports, or unredacted diagnostics.
3. Add parser tests for every supported shape.
4. Run `uv run ruff check .`, `uv run mypy src`, and `uv run pytest`.
5. Treat all archive and model content as hostile input.

By contributing, you agree that your contribution is licensed under Apache-2.0.
