# Threat model

## Protected assets

- Platform export contents and derived records
- API keys
- Saved reports and diagnostics
- The integrity of the user's filesystem and terminal

## Threats in scope

- Malicious or malformed ZIP/JSON input
- Path traversal, symlinks, nested archives, and resource-exhaustion archives
- Terminal escape and hyperlink injection
- HTML/script injection in offline reports
- Accidental cloud transmission or credential disclosure
- Preview/transmission drift or consent accidentally reused across questions
- Ambiguous conversational referents selecting the wrong date, platform,
  category, facet, or record
- Conversation history or prior model output leaking into a later request
- Redirect, proxy, and custom-endpoint surprises
- Prompt injection contained in an export
- Sensitive data in logs, exceptions, diagnostics, and temporary files
- Dependency and release-pipeline compromise

## Security boundaries

Archive records are untrusted data. They are never executed, rendered as HTML,
passed to a shell, or granted tools. Local deterministic workflows must not open
sockets. Calculations, retrieval, and request preparation happen locally.

Loopback model endpoints are local. Every non-loopback endpoint is cloud. Cloud
model adapters are kept behind an advanced flow and require a deliberate
provider choice, an exact immutable-payload preview, and authorization for every
question. Source paths, archive identifiers, filenames, and API keys are not
model context. Cloud consent is a disclosure control, not de-identification.

The chat resolver is deterministic and allowlisted. It resolves a singular
referent only when the immediately previous local query has one valid target;
otherwise it executes a local clarification plan. Every resolved follow-up
re-queries the ephemeral index. Models cannot select a referent, reuse an old
evidence window, or access a transcript.

Conversation state is restricted to one previous plan, bounded IDs, active
scope, timezone, and unambiguous referents. It is held in memory only. Cloud
chat previews the complete immutable request body and requires typed consent
again before each adapter completion call. Denial, EOF, Ctrl-C, local
clarification, and no-match paths do not invoke the provider.

## Known limitations

- Python cannot guarantee secret zeroization from process memory.
- Best-effort deletion cannot guarantee physical erasure.
- A process crash can expose bounded in-memory conversation metadata in swap or
  a crash dump even though no transcript is stored.
- Provider export schemas are undocumented and change over time.
- Selected cloud providers can receive personal questions, derived facts, and
  normalized record values after authorization and may retain them under their
  own policies.
- Lexical full-text matching can miss semantically related records; calculated
  matching counts describe the deterministic query scope, not semantic recall.
- The natural-language compiler selects one primary operation and does not
  compose mixed-scope modifiers. Callers must inspect the resulting plan or ask
  one scoped question at a time when an intersection matters.
- The default evidence selection target is 100 records, but callers can request
  a different target. The enforced transmission boundary is the 256 KiB
  immutable provider request body, not a fixed record count.
- v0.1 is an alpha release and is not externally audited.

An independent security review is required before a stable v1.0 release.
