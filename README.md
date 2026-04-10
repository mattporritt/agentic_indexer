# Moodle AI Indexer

Moodle AI Indexer is a local, SQLite-backed code-intelligence layer for Moodle LMS.

It is built for two audiences:

- human developers who need Moodle-aware navigation and change context
- agentic coding systems that need bounded, structured, explainable context before editing

The project is intentionally not a generic code search engine and not an autonomous coding agent. Its job is to make Moodle structure explicit, queryable, and safe to consume.

## Project Status

This repository is at a natural **v1 complete** point for its current scope.

v1 includes:

- structural code navigation
- related-definition traversal
- edit-surface suggestions
- bounded dependency neighborhoods
- hybrid semantic context retrieval
- bounded change planning
- test-impact estimation
- execution guardrails
- compact context-bundle packaging for agents

v1 intentionally stops before:

- autonomous code modification
- autonomous patch planning
- execution orchestration
- CI orchestration
- repository-wide graph expansion

That boundary is deliberate. This project provides the context layer that an agent can trust; the agent layer decides what to do with that context.

## Why This Exists

Moodle development is highly convention-driven. Real implementation context is often spread across:

- plugin and subsystem boundaries
- `db/services.php`, `db/access.php`, `settings.php`, and `lib.php`
- renderers, output classes, and Mustache templates
- forms and framework bases
- AMD JavaScript source modules and build artifacts
- PHPUnit and Behat coverage

Plain text search can find files, but it does not reliably answer questions like:

- where is this symbol actually defined?
- what else matters around this method or file?
- what is the likely edit surface?
- what tests are most likely affected?
- what should an agent read first?

This project turns those Moodle-specific structures into bounded, machine-friendly JSON outputs.

## What The Project Is

Today the project is best understood as a:

- **code intelligence layer** for Moodle
- **structural + semantic context provider**
- **bounded planning and safety layer**
- **context-bundle packager** for agent workflows

It is designed to help with serious Moodle development tasks while staying explicit and explainable.

## What The Project Is Not

The project is **not** currently:

- an autonomous coding agent
- a code modification engine
- an execution orchestrator
- a test runner
- a CI system
- a full static call-graph engine
- a semantic memory system for the whole repository

Those responsibilities belong in higher-level tooling. This project provides the trusted substrate those tools can consume.

## Current Scope

The current capability surface includes:

- full rebuild indexing into SQLite
- Moodle component inference and file-role classification
- PHP symbol extraction and structural relationship indexing
- AMD JavaScript source/import/superclass/build extraction
- capability, language string, and test discovery
- service-definition extraction from `db/services.php`
- `find-definition` for PHP symbols and Moodle JS modules
- `find-related-definitions`
- `suggest-edit-surface`
- `dependency-neighborhood`
- `semantic-context`
- `propose-change-plan`
- `assess-test-impact`
- `execution-guardrails`
- `build-context-bundle`

The design stays intentionally bounded:

- structural navigation is the spine
- semantic retrieval is constrained by structural anchors
- planning is conservative rather than exhaustive
- safety outputs prefer concrete local evidence over generic advice
- bundles are compact enough for real agent context windows

## Installation

Python 3.12+ is assumed.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

After installation, the canonical entrypoint is:

```bash
moodle-indexer --help
```

## Quick Start

Build an index:

```bash
moodle-indexer index \
  --moodle-path /path/to/moodle \
  --db-path /path/to/moodle-index.sqlite \
  --workers 8
```

Look up a definition:

```bash
moodle-indexer find-definition \
  --db-path /path/to/moodle-index.sqlite \
  --symbol 'mod_assign\external\start_submission::execute'
```

Get the immediate structural neighborhood:

```bash
moodle-indexer dependency-neighborhood \
  --db-path /path/to/moodle-index.sqlite \
  --symbol 'assign::view'
```

Package a compact agent-ready working set:

```bash
moodle-indexer build-context-bundle \
  --db-path /path/to/moodle-index.sqlite \
  --query 'add a parameter to a Moodle external API method and update its tests'
```

Query the runtime-facing contract wrapper:

```bash
moodle-indexer find-definition \
  --db-path /path/to/moodle-index.sqlite \
  --symbol 'mod_assign\external\start_submission::execute' \
  --json-contract
```

## Core Commands

The commands below are the main public surface for v1.

### `find-definition`

Use when you need the canonical definition of a PHP symbol or Moodle JS module.

Accepts:

- `--symbol`
- optional `--type`

Returns:

- definition records
- signatures and docblock summaries where available
- bounded usage examples
- inheritance and related structural metadata

Call this first when you need a trustworthy anchor.

### `find-related-definitions`

Use when you want the most relevant definitions and artifacts around a symbol or file.

Accepts:

- `--symbol`
- `--file`

Returns:

- bounded primary and secondary related definitions
- direct companions such as services, tests, renderers, templates, forms, and JS neighbors

Call this after `find-definition` when you need nearby context but not yet a change plan.

### `suggest-edit-surface`

Use when you want to know which files are most likely to be edited next.

Accepts:

- `--symbol`
- `--file`

Returns:

- `primary_edit_surface`
- `secondary_edit_surface`

Call this when moving from “what is this?” to “what is the likely edit set?”

### `dependency-neighborhood`

Use when you need a bounded dependency view around a symbol or file.

Accepts:

- `--symbol`
- `--file`

Returns sections such as:

- `likely_callers`
- `likely_callees`
- `linked_tests`
- `linked_services`
- `linked_rendering_artifacts`
- `linked_forms`
- `linked_javascript`

Call this when you want execution-adjacent context without pretending you have a full call graph.

### `semantic-context`

Use when structural context alone is not enough and you need bounded similar examples or semantically relevant nearby context.

Accepts:

- `--symbol`
- `--file`
- `--query`

Returns:

- `primary_semantic_context`
- `secondary_semantic_context`

Structural anchors remain primary. Semantic results broaden recall without replacing trusted structure.

### `propose-change-plan`

Use when you want a conservative edit-set proposal around a symbol, file, or free-text change goal.

Accepts:

- `--symbol`
- `--file`
- `--query`

Returns:

- `required_edits`
- `likely_edits`
- `optional_edits`
- `validation_impact`
- `recommended_sequence`

Call this when you are moving from navigation into change planning.

### `assess-test-impact`

Use when you want a bounded view of likely validation impact.

Accepts:

- `--symbol`
- `--file`
- `--query`

Returns:

- `direct_tests`
- `likely_tests`
- `environment_steps`
- `contract_checks`
- `manual_review_points`

Call this after or alongside change planning to understand what needs to be checked.

### `execution-guardrails`

Use when you want bounded risk and safety guidance before editing.

Accepts:

- `--symbol`
- `--file`
- `--query`

Returns:

- `change_risk`
- `pre_edit_checks`
- `post_edit_checks`
- `do_not_assume`
- `watch_points`

Call this before making or finalizing changes when you need a practical safety layer.

### `build-context-bundle`

Use when you want one compact working set instead of calling several endpoints and merging them yourself.

Accepts:

- `--symbol`
- `--file`
- `--query`

Returns:

- `anchor`
- `primary_context`
- `supporting_context`
- `optional_context`
- `tests_to_consider`
- `guardrails`
- `example_patterns`
- `recommended_reading_order`
- `recommended_next_actions`
- `bundle_stats`

Call this when an agent is about to start a task and needs a bounded context package that is safe to load into a working context window.

## Runtime-Facing Contract

The indexer now exposes a small runtime-facing contract mode aligned to the
shared outer-envelope style used by the related internal tools.

Currently supported commands:

- `find-definition`
- `semantic-context`
- `build-context-bundle`

Enable it with:

- `--json-contract`

This mode is intentionally narrow:

- it reuses the existing command logic
- it leaves the current human-oriented output modes unchanged
- it only changes the outer wrapper, not the inner meaning of the command

### Contract Envelope

The outer envelope is:

```json
{
  "tool": "agentic_indexer",
  "version": "v1",
  "query": "...",
  "normalized_query": "...",
  "intent": {
    "command": "...",
    "query_kind": "...",
    "response_mode": "...",
    "symbol_type_filter": null,
    "include_usages": null,
    "limit": 10
  },
  "results": []
}
```

### Provenance Semantics

Every runtime result includes a normalized `source` object:

- `name`: always `code_index`
- `type`: always `indexed_codebase`
- `url`: `null`
- `canonical_url`: `null`
- `path`: relevant indexed file path when applicable
- `document_title`: `null`
- `section_title`: `null`
- `heading_path`: always a list, usually `[]` for code results

This keeps provenance explicit and predictable for runtime callers.

### Confidence Semantics

Confidence is intentionally coarse:

- `high`: direct structural anchor or high-confidence indexed companion
- `medium`: useful but less direct structural or semantic support
- `low`: reserved for weaker cases and used sparingly

### Stable IDs

The runtime contract adds deterministic IDs derived from stable identifying
fields such as:

- command
- file path
- symbol or module name
- chunk ID
- nested role/step identity where practical

These IDs are intended for:

- deduping
- traceability
- runtime-side merging across repeated calls

### Examples

Definition lookup:

```bash
moodle-indexer find-definition \
  --db-path /path/to/moodle-index.sqlite \
  --symbol 'mod_assign\external\start_submission::execute' \
  --json-contract
```

Semantic context:

```bash
moodle-indexer semantic-context \
  --db-path /path/to/moodle-index.sqlite \
  --symbol 'mod_assign\external\start_submission::execute' \
  --json-contract
```

Context bundle:

```bash
moodle-indexer build-context-bundle \
  --db-path /path/to/moodle-index.sqlite \
  --query 'add a parameter to a Moodle external API method and update its tests' \
  --json-contract
```

## Architecture And Mental Model

The system is layered. Each layer builds on the previous one.

1. **Indexing and extraction**
   - scan the repository
   - infer components and file roles
   - extract structural Moodle-aware facts into SQLite

2. **Structural navigation**
   - `find-symbol`
   - `find-definition`
   - `file-context`
   - `suggest-related`

3. **Bounded navigation**
   - `find-related-definitions`
   - `suggest-edit-surface`
   - `dependency-neighborhood`

4. **Hybrid retrieval**
   - `semantic-context`

5. **Planning and safety**
   - `propose-change-plan`
   - `assess-test-impact`
   - `execution-guardrails`

6. **Context packaging**
   - `build-context-bundle`

The important design rule is:

**Later layers do not replace earlier ones.**

They package, prioritize, and synthesize the same trusted structural anchors rather than inventing disconnected logic.

## How Agents Should Use This Project

Recommended usage flow for an agent:

1. **Resolve the anchor**
   - use `find-definition` for a symbol
   - use `file-context` for a file

2. **Expand to direct structural context**
   - use `find-related-definitions` or `dependency-neighborhood`

3. **Estimate likely edit scope**
   - use `suggest-edit-surface`
   - or go directly to `propose-change-plan`

4. **Pull in similar examples only if needed**
   - use `semantic-context`

5. **Add safety guidance**
   - use `assess-test-impact`
   - use `execution-guardrails`

6. **Package a working set**
   - use `build-context-bundle`

For most agent workflows, `build-context-bundle` should be the final project-level call before the agent switches into its own reasoning and editing logic.

This project stops at:

- context selection
- change-surface synthesis
- safety guidance
- bounded packaging

Planning beyond that point belongs to the agent layer, not this project.

More explicit agent guidance lives in [docs/agent_usage.md](/Users/mattp/projects/agentic_indexer/docs/agent_usage.md).

## Validation And Quality

The project has been validated iteratively against real Moodle-oriented slices, especially:

- service flows
- rendering flows
- provider/form flows
- JS module flows
- free-text service-pattern workflows

Canonical validation artifacts live in [validation_runs/](/Users/mattp/projects/agentic_indexer/validation_runs).

Those runs are useful because they show what “good” output looks like against the local index and local Moodle checkout, not just the synthetic test fixtures.

### Automated Verification

The main automated suite lives under [tests/](/Users/mattp/projects/agentic_indexer/tests).

Typical verification commands:

```bash
python3 -m pytest -q
python3 -m pytest -q tests/test_indexer.py tests/test_cli.py
python3 -m pytest -q tests/test_agent_workflows.py tests/test_cli_agent_workflows.py
```

### Validation Principles

Good output should be:

- bounded
- concrete
- confidence-aware
- slice-pure
- useful to inspect or act on immediately

Good outputs should generally prefer:

- direct implementation files
- direct registration or entrypoint files
- direct tests
- direct rendering/form/JS companions

They should generally avoid:

- weak generic spillover
- giant flattened neighborhoods
- speculative graph edges

More detailed validation guidance lives in [docs/validation.md](/Users/mattp/projects/agentic_indexer/docs/validation.md).

## Repository Layout

The main code lives in [src/moodle_indexer/](/Users/mattp/projects/agentic_indexer/src/moodle_indexer).

Key modules:

- [cli.py](/Users/mattp/projects/agentic_indexer/src/moodle_indexer/cli.py): command surface and JSON CLI entrypoints
- [indexer.py](/Users/mattp/projects/agentic_indexer/src/moodle_indexer/indexer.py): full rebuild indexing pipeline
- [extractors.py](/Users/mattp/projects/agentic_indexer/src/moodle_indexer/extractors.py): PHP, JS, service, test, capability, and string extraction
- [queries.py](/Users/mattp/projects/agentic_indexer/src/moodle_indexer/queries.py): structural navigation, semantic retrieval, planning, and context packaging
- [agent_safety.py](/Users/mattp/projects/agentic_indexer/src/moodle_indexer/agent_safety.py): bounded test-impact and guardrail synthesis
- [store.py](/Users/mattp/projects/agentic_indexer/src/moodle_indexer/store.py): SQLite schema and persistence helpers

Supporting docs:

- [docs/architecture.md](/Users/mattp/projects/agentic_indexer/docs/architecture.md)
- [docs/agent_usage.md](/Users/mattp/projects/agentic_indexer/docs/agent_usage.md)
- [docs/validation.md](/Users/mattp/projects/agentic_indexer/docs/validation.md)

## Extending The Project Later

Future work should stay grounded in the same design constraints:

- structural truth first
- bounded retrieval
- conservative planning
- explicit safety guidance
- compact packaging for real agent contexts

The easiest safe way to extend the project is usually:

1. add or improve extraction
2. expose it through structural navigation
3. feed it into bounded planning/safety layers
4. package it only after it is already trustworthy

## Supporting Docs

- [Architecture](/Users/mattp/projects/agentic_indexer/docs/architecture.md)
- [Agent Usage Guide](/Users/mattp/projects/agentic_indexer/docs/agent_usage.md)
- [Validation Guide](/Users/mattp/projects/agentic_indexer/docs/validation.md)
