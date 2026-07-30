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
preparation. Product labels are path-derived, so product aggregates are confined
to the local manifest and are not materialized as `CalculatedFact` values.
This prevents untrusted product cardinality from expanding overview results.
Safe aggregate counts, sizes, extensions, and parser coverage may be included
downstream. Extensions enter downstream facts only when they match a short fixed
allowlist; every other filename suffix is aggregated as `[other]`.

## Planning

`compile_query(question, timezone=...)` returns an immutable `QueryPlan` with a
stable ID, one `QueryIntent`, one allowlisted `QueryOperation`, a typed
`QueryScope`, and explicit assumptions. Supported operations are:

- archive overview
- UTC date range derived from an explicit local timezone
- service, device, hostname, and activity-type facets
- monthly UTC trend buckets
- platform and category comparison
- parameterized FTS lookup
- record detail for explicit result IDs
- coverage-only clarification or unsupported-form results

The compiler uses local parsing only. It cannot emit SQL or code. Full-text terms
and record IDs become SQLite parameters; identifiers and SQL templates are
selected exclusively from local enums.

`QueryPlan` validates its intent/operation pairing and operation-specific scope
at construction time. Required date bounds, facets, comparison members, text
terms, and record IDs cannot be omitted, and scope fields that an operation
would ignore are rejected. Invalid public plans therefore fail with structured
Pydantic validation. `execute_query()` revalidates its plan at the boundary
before touching SQLite as defense in depth.

The compiler chooses one primary operation by deterministic precedence. It does
not compose mixed scopes. For example, a device-facet question with additional
search text remains a device-facet plan; the extra text does not create an
intersection. Callers should inspect the returned `QueryPlan`, construct an
explicit supported plan, or ask one scoped question at a time when modifiers
must be combined.

## Results and PR-B integration

`AnalysisSession.query()` compiles and executes a plan across the complete
matching normalized population. Aggregates therefore do not depend on the
bounded evidence sample. `QueryResult` contains:

- the exact plan and status
- total and matching record counts
- stable `CalculatedFact` IDs
- diversified evidence candidates, with a caller-configurable target that
  defaults to 100
- local provenance retained on evidence records
- structured `CoverageNote` and `QueryAssumption` values

The default of 100 evidence candidates is not a hard transmission cap. Final
model preparation adds candidates only while the complete immutable provider
request remains within 256 KiB.

Statuses distinguish no record matches, absent facet data, a present but
unsupported product, and a product/category that is not present. Ambiguous and
unsupported questions are coverage-only local results and never invoke a model.

PR-B should keep a `QueryResult` as the turn boundary. It may render local
clarifications directly or pass a successful result to
`prepare_question(query_result, adapter)`. It must not reconstruct retrieval from
conversation text or transmit manifest entry paths/source IDs. Because the local
compiler does not compose mixed scopes, PR-B must present the selected plan or
ask for clarification when its interaction design requires modifier
intersections. Record-detail follow-ups must carry explicit result IDs from an
earlier `QueryResult`.

Known unsupported forms include causal “why” analysis, recommendations,
predictions, pronoun resolution, and implicit follow-up references. Callers
should ask for a more specific overview, date, facet, trend, comparison, text
lookup, or explicit result ID.
