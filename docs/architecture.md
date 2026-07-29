# Architecture

```text
explicit platform choice
          ↓
safe ArchiveReader → provider parser → NormalizedRecord
                                          ↓
                               ephemeral SQLite + FTS5
                                 ↙                ↘
                    deterministic report    facts + diversified evidence
                         ↓                          ↓
                  offline HTML/MD/JSON     immutable prepared question
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
local query scope. It selects a bounded evidence set diversified across
platform, category, time, and repeated value groups. Calculated facts and
records have separate citation namespaces.

`ModelAdapter` exposes destination and locality metadata plus one completion
operation. A prepared question freezes the exact payload, preview, and valid
fact/record IDs before authorization. Adapters receive no source paths, archive
identifiers, internal filenames, or tools.

## Ephemeral state

Each process creates a random, user-only temporary directory carrying a marker.
SQLite uses `secure_delete` and the DELETE journal mode. Normal shutdown,
handled cancellation, and exceptions unwinding through the session context close
and remove it. Abrupt termination, including `SIGKILL` or an unhandled
`SIGTERM`, can leave the marked session directory behind. On a later startup,
the CLI removes marked sessions older than 24 hours. See `PRIVACY.md` for the
limits of best-effort deletion.
