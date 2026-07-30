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

## Known limitations

- Python cannot guarantee secret zeroization from process memory.
- Best-effort deletion cannot guarantee physical erasure.
- Provider export schemas are undocumented and change over time.
- Selected cloud providers can receive personal questions, derived facts, and
  normalized record values after authorization and may retain them under their
  own policies.
- Lexical full-text matching can miss semantically related records; calculated
  matching counts describe the deterministic query scope, not semantic recall.
- v0.1 is an alpha release and is not externally audited.

An independent security review is required before a stable v1.0 release.
