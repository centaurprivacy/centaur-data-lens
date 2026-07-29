# v0.1 security release checklist

- [ ] All fixtures are synthetic.
- [ ] Deterministic commands pass with network sockets disabled.
- [ ] Archive traversal, symlink, malformed JSON, and resource-limit tests pass.
- [ ] Terminal control and HTML injection payloads render as inert text.
- [ ] Offline HTML CSP hashes match the embedded script and stylesheet.
- [ ] Offline HTML performs no network requests or persistent browser storage.
- [ ] API keys are absent from command arguments, configuration, logs, errors,
      diagnostics, reports, and artifacts.
- [ ] Cloud destinations use verified TLS, no redirects, no ambient proxies, a
      bounded response, and explicit confirmation.
- [ ] Cloud preview measures the exact immutable provider request body,
      including the system prompt, static schema, and provider envelope, and
      confirmation is required again for every transmitted question.
- [ ] Cloud schemas contain no personal fact or record IDs; citations are
      validated locally.
- [ ] Model payloads exclude source paths, archive identifiers, internal
      filenames, and API keys.
- [ ] Model records are bounded and model output cannot execute tools or code.
- [ ] Ruff, strict mypy, pytest with coverage, package build, and dependency
      audit pass.
- [ ] Threat model and known limitations are included in release notes.
- [ ] Release is labelled alpha and not described as externally audited.
