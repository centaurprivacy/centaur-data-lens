# Architecture

```text
explicit platform choice
          ↓
safe ArchiveReader → provider parser → NormalizedRecord
                                          ↓
                               ephemeral SQLite + FTS5
                                 ↙                ↘
                    deterministic report       selected context
                         ↓                          ↓
                  offline HTML/MD/JSON     local or BYOK adapter
```

The wizard and direct commands call the same application services. Platform
selection is always explicit; parser validation catches mismatches but does not
infer or switch platforms.

## Extension points

`PlatformDefinition` owns display copy, export guidance, supported categories,
exclusions, validation, and its parser. A new platform is registered in one
place.

`ModelAdapter` exposes destination and locality metadata plus one completion
operation. Model output is parsed into cited `AIAnswer` claims. Adapters receive
only locally selected normalized records and have no tools.

## Ephemeral state

Each process creates a random, user-only temporary directory carrying a marker.
SQLite uses `secure_delete` and the DELETE journal mode. Normal shutdown,
handled cancellation, and exceptions unwinding through the session context close
and remove it. Abrupt termination, including `SIGKILL` or an unhandled
`SIGTERM`, can leave the marked session directory behind. On a later startup,
the CLI removes marked sessions older than 24 hours. See `PRIVACY.md` for the
limits of best-effort deletion.
