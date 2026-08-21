# RTO package

This package provides one objective-count-independent (`1..N`) offline RTO architecture.
The current architecture and compatibility gates are summarized in
`docs/rto/01_RTO系统综合说明.md`; historical design baselines are preserved under
`docs/rto/archive/`.

The active unified path provides:

- a context-free strict `OptimizationIntent` for one or more objectives;
- atomic capabilities, trusted context schema, system policy, and a sanitized manifest;
- a provider-neutral domain-model communication contract with bounded repair and
  deterministic user clarification;
- one deterministic `OptimizationProblem` builder and problem-feature analyzer;
- explicit `SolverPort`, registry, router, scalar-grid, and Pareto-grid plugins;
- common candidate, vector-evaluation, solver-result, workflow, and strategy contracts;
- M2/M4 paired evaluation, strict evidence replay, recoverable orchestration, and the
  default unified Python and CLI entry points.

The explicit legacy compatibility layer preserves:

- strict, versioned neutral contracts and canonical fingerprints;
- fixed RTO catalog/context/intent loading and deterministic problem building;
- provider-neutral simulator and request-factory ports;
- M2/M4 candidate pair compilation and strict pair-difference checks;
- a CDU M7 adapter using only the public `preview`, `run`, and `read_run` API;
- paired M2 KPI/constraint evaluation with semantic candidate and baseline caches;
- deterministic 25-point coarse search plus at most eight new refinement points;
- Top-3 M4 acceptance evaluation and deterministic final selection;
- immutable strategy entries with append-only lifecycle events and explicit releases;
- exact sampled-anchor lookup without interval interpolation;
- recoverable, manifest-last offline orchestration and strict evidence replay;
- strict historical external JSON parsing, trusted binding, and request provenance;
- deterministic 81-point three-objective Pareto search and complete non-dominated layers;
- explicit lexicographic preference, Top-5 M4 verification, and an independent publishability gate;
- resumable workflow evidence and compact, immutable strategy drafts;
- historical version-routed readers and commands required by existing evidence.

The historical scalar and Pareto workflows remain available only as migration and strict
replay baselines. They are not separate future product lines and must not receive new
business features. They cannot be removed until historical absolute evidence references
have a verified relocation path.

Only `adapters/cdu_m7.py` may import CDU runtime code. All strategy outputs remain
offline simulation artifacts unless an external, separately authorized field layer is built.
