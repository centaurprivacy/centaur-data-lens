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

Ollama is restricted to the local loopback interface by default. Bring-your-own
key adapters communicate directly with the selected provider after showing a
transmission preview and receiving confirmation. Centaur does not receive the
key, prompt, response, or export.

Centaur Data Lens has no analytics, crash reporter, automatic updater, or
background service.
