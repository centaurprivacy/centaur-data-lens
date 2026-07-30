# Local query engine contracts

PR-A adds a deterministic, network-free boundary between archive analysis and
model preparation. It does not add a conversational interface.

## Archive manifest

`ArchiveManifest` inventories every safely named archive entry from the ZIP
central directory or selected directory tree. Inventory does not extract files
or open unsupported contents. Each local-only `ManifestEntry` records the source
identity, platform, internal path, product grouping, extension, compressed and
uncompressed sizes, nested-archive status, and parser coverage.

The manifest also aggregates products, formats, sizes, nested archives, and
parser-supported versus unsupported entry counts. Entry `source_id` and
`internal_path` fields are provenance only. They are never serialized by model
preparation. Product labels are path-derived, so product-level facts are also
marked local-only; safe aggregate counts, sizes, extensions, and parser coverage
may be included downstream.

## Planning

`compile_query(question, timezone=...)` returns an immutable `QueryPlan` with a
stable ID, one `QueryIntent`, one allowlisted `QueryOperation`, a typed
`QueryScope`, and explicit assumptions. Supported operations are:

- archive overview
- UTC date range derived from an explicit local timezone
- service, device, hostname, and activity-type facets
- monthly UTC trend buckets
- platform comparison
- parameterized FTS lookup
- record detail for explicit result IDs
- coverage-only clarification or unsupported-form results

The compiler uses local parsing only. It cannot emit SQL or code. Full-text terms
and record IDs become SQLite parameters; identifiers and SQL templates are
selected exclusively from local enums.

## Results and PR-B integration

`AnalysisSession.query()` compiles and executes a plan across the complete
matching normalized population. Aggregates therefore do not depend on the
bounded evidence sample. `QueryResult` contains:

- the exact plan and status
- total and matching record counts
- stable `CalculatedFact` IDs
- at most 100 diversified evidence candidates by default
- local provenance retained on evidence records
- structured `CoverageNote` and `QueryAssumption` values

Statuses distinguish no record matches, absent facet data, a present but
unsupported product, and a product/category that is not present. Ambiguous and
unsupported questions are coverage-only local results and never invoke a model.

PR-B should keep a `QueryResult` as the turn boundary. It may render local
clarifications directly or pass a successful result to
`prepare_question(query_result, adapter)`. It must not reconstruct retrieval from
conversation text, silently broaden a no-match result, or transmit manifest
entry paths/source IDs. Record-detail follow-ups must carry explicit result IDs
from an earlier `QueryResult`.

Known unsupported forms include causal “why” analysis, recommendations,
predictions, pronoun resolution, and implicit follow-up references. Callers
should ask for a more specific overview, date, facet, trend, comparison, text
lookup, or explicit result ID.
