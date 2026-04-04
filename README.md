# Moodle AI Indexer

Moodle AI Indexer is a Phase 1, SQLite-backed code indexer for a local Moodle LMS checkout. It is aimed at agentic coding systems and engineers who need Moodle-aware navigation and retrieval, not a speculative universal code graph.

The project focuses on practical structure that matters in Moodle work:

- infer Moodle components and plugin ownership from paths
- classify important Moodle file roles
- extract PHP symbols and useful structural relationships
- index capabilities, language strings, and test artifacts
- suggest likely companion files for common Moodle change patterns
- expose the indexed data through a small deterministic JSON CLI

## Why This Exists

Moodle development depends heavily on repository conventions. Relevant context for a change is often spread across:

- plugin/component boundaries
- `db/access.php`, `db/services.php`, and `db/tasks.php`
- `lang/en/*.php`
- renderers, output classes, and templates
- PHPUnit and Behat coverage

Plain text search can find files, but it does not reliably answer:

- which component owns this file?
- where is this symbol defined?
- what does this class extend or implement?
- where are the capability definitions and checks?
- which language strings and tests are likely relevant?

This tool builds a compact local index so those questions become cheap and machine-friendly to answer.

## What Phase 1 Includes

- full rebuild indexing into SQLite
- explicit Moodle component inference for common plugin families and core subsystems
- subplugin-aware component inference via `db/subplugins.json`
- deterministic file-role classification
- parser-first PHP extraction with resilient fallback logic
- Moodle-aware AMD JavaScript extraction for `amd/src/` source files
- symbol indexing for classes, interfaces, traits, functions, and methods
- structural relationship indexing for `extends`, `implements`, and method-to-class ownership
- output/rendering-aware suggestions for `classes/output/` and `templates/` when production PHP references Moodle output classes
- framework-aware suggestions for `settings.php`, `lib/adminlib.php`, Moodle form classes, and `lib/formslib.php`
- capability extraction from `db/access.php`
- web service extraction from `db/services.php`
- capability attribution to the owning component of the defining file
- language string extraction from `lang/en/*.php`
- detection of obvious `require_capability`, `has_capability`, and `get_string` usage
- `db/services.php` extraction with support for both deprecated `classpath` implementations and modern `classname`-based external classes
- JavaScript import/dependency extraction for both modern ES module syntax and older Moodle `define([...])` AMD modules
- source/build awareness for `amd/src/*.js` and `amd/build/*.min.js`
- service-aware suggestions that link `db/services.php` to implementation files and likely PHPUnit coverage
- coherent linked-artifact navigation across service definitions, rendering artifacts, and Moodle AMD source modules
- PHPUnit and Behat discovery
- related-file suggestions with explanation strings
- IDE-like definition lookup for PHP symbols and Moodle JS modules, with signatures, modifiers, docblocks, inheritance hints, and bounded usage examples
- JSON CLI commands for indexing and querying

## What Phase 1 Does Not Include

- embeddings or vector search
- incremental indexing
- a web UI
- live IDE integration
- deep JavaScript analysis
- a precise call graph
- runtime tracing
- background workers

## High-Level Design

The package lives under `src/moodle_indexer/` and keeps responsibilities separated:

- `cli.py`: CLI entrypoint and argument handling
- `indexer.py`: full rebuild orchestration
- `scanner.py`: repository scanning
- `components.py`: Moodle component inference rules
- `file_roles.py`: path-based file-role classification
- `php_parser.py`: parser-first PHP symbol extraction with fallback logic
- `extractors.py`: Moodle-specific extraction for PHP, JS modules, relationships, capabilities, strings, and tests
- `store.py`: SQLite schema and persistence helpers
- `queries.py`: query services used by the CLI
- `suggestions.py`: deterministic related-file heuristics

The SQLite schema stays intentionally small and extensible:

- `repositories`
- `components`
- `files`
- `symbols`
- `relationships`
- `capabilities`
- `capability_usages`
- `language_strings`
- `language_string_usages`
- `tests`
- `js_modules`
- `js_imports`

## Moodle-Aware Suggestions

Phase 1 intentionally mixes a few different sources of truth:

- explicitly extracted relationships:
  - PHP `extends` / `implements`
  - web service definitions from `db/services.php`
  - capability definitions from `db/access.php`
  - Moodle AMD source module imports, exports, inheritance, and source/build pairings
- deterministic Moodle heuristics:
  - `settings.php` suggests `lib/adminlib.php`
  - `classes/output/*.php` suggests paired Mustache templates when present
  - `db/services.php` suggests resolved `classpath` and `classname` implementation files
  - resolved service implementations suggest likely PHPUnit files such as `tests/external/*_test.php` or `tests/externallib_test.php`
  - PHP class references and instantiations can resolve companion files for Moodle output classes and plugin form classes
  - form classes extending `moodleform` suggest the core base implementation in `lib/formslib.php`
  - `amd/src/*.js` files surface resolved imported module source files and related `amd/build/*.min.js` artifacts
  - file-context and suggest-related now group service, rendering, JavaScript, and entrypoint links into explicit `linked_artifacts` sections

Current JavaScript support is intentionally Moodle-specific rather than a
generic JS graph. Phase 1 supports:

- modern ES module imports such as `import Foo from 'core_admin/foo'`
- named imports such as `import {call as fetchMany} from 'core/ajax'`
- older Moodle AMD dependencies declared with `define([...], function(...) {})`
- default export / exported class detection where practical
- superclass detection for exported classes that extend imported modules
- deterministic source/build linking between `amd/src/*.js` and `amd/build/*.min.js`

JavaScript module resolution follows a fixed precedence order:

1. exact hit in the indexed `js_modules` registry
2. explicit external runtime dependency classification for modules such as `jquery`
3. indexed component-root lookup plus deterministic Moodle path mapping
4. static component-root fallback rules
5. explicit unresolved result

The current resolver understands:

- `core/<module>` -> `lib/amd/src/<module>.js`
- `core_<subsystem>/<module>` -> `<core subsystem root>/amd/src/<module>.js`
- frankenstyle plugin modules such as `mod_assign/foo` or `tool_analytics/bar` -> `<component root>/amd/src/<module>.js`

For agentic work, the canonical editable implementation is always the source
file in `amd/src/`. Matching `amd/build/*.min.js` files are treated as related
artifacts, not as the primary implementation target.

The suggestion engine is still heuristic in places. It is designed to be
explainable and useful for local navigation, not to claim perfect semantic
coverage of Moodle's runtime behavior.

## Setup

Python 3.12+ is assumed.

Install dependencies and the package:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

`requirements.txt` is the baseline dependency file. `pyproject.toml` adds package metadata and the `moodle-indexer` CLI entrypoint.

After `pip install -e .`, the canonical way to run the tool is:

```bash
moodle-indexer --help
```

Normal development usage should not require `PYTHONPATH=src python -m ...`.

## CLI Usage

The tool does not assume it lives beside the Moodle checkout. Always pass the Moodle repository path explicitly.

The path you pass with `--moodle-path` is always treated as the repository root.

The indexer separately detects an application root:

- classic Moodle 5.0-style layout:
  repository root and application root are the same directory
- split Moodle 5.1-style layout:
  repository root is the checkout path you passed, while application root is typically `<repository_root>/public`

This means the index stores two path views for each file:

- `repository_relative_path`: the real on-disk path relative to the repository root
- `moodle_path`: the Moodle-native path relative to the application root when the file lives under it

Examples:

- classic layout:
  - repository-relative `mod/forum/lib.php`
  - moodle path `mod/forum/lib.php`
- split layout:
  - repository-relative `public/mod/forum/lib.php`
  - moodle path `mod/forum/lib.php`
- repository-level file outside `public/`:
  - repository-relative `admin/cli/install_database.php`
  - moodle path `admin/cli/install_database.php`

User-facing lookups such as `file-context` prefer the Moodle-native path where that is the most natural interface, so `mod/forum/lib.php` continues to work in both classic and split layouts.

Build a fresh index:

```bash
moodle-indexer index \
  --moodle-path /path/to/moodle \
  --db-path /path/to/moodle-index.sqlite \
  --workers 8
```

Find a symbol:

```bash
moodle-indexer find-symbol \
  --db-path /path/to/moodle-index.sqlite \
  --symbol discussion_exporter
```

Inspect a file:

```bash
moodle-indexer file-context \
  --db-path /path/to/moodle-index.sqlite \
  --file mod/forum/renderer.php
```

Files outside the application root can also be queried directly:

```bash
moodle-indexer file-context \
  --db-path /path/to/moodle-index.sqlite \
  --file admin/cli/install_database.php
```

Summarize a component:

```bash
moodle-indexer component-summary \
  --db-path /path/to/moodle-index.sqlite \
  --component mod_forum
```

Suggest related files:

```bash
moodle-indexer suggest-related \
  --db-path /path/to/moodle-index.sqlite \
  --file admin/tool/demo/settings.php
```

Find related definitions around a symbol or file:

```bash
moodle-indexer find-related-definitions \
  --db-path /path/to/moodle-index.sqlite \
  --symbol mod_assign\\external\\start_submission::execute

moodle-indexer find-related-definitions \
  --db-path /path/to/moodle-index.sqlite \
  --file mod/assign/locallib.php
```

Suggest the likely edit surface around a symbol or file:

```bash
moodle-indexer suggest-edit-surface \
  --db-path /path/to/moodle-index.sqlite \
  --symbol aiprovider_openai\\provider::get_action_settings

moodle-indexer suggest-edit-surface \
  --db-path /path/to/moodle-index.sqlite \
  --file mod/assign/db/services.php
```

Inspect the bounded dependency neighborhood around a symbol or file:

```bash
moodle-indexer dependency-neighborhood \
  --db-path /path/to/moodle-index.sqlite \
  --symbol mod_assign\\external\\start_submission::execute

moodle-indexer dependency-neighborhood \
  --db-path /path/to/moodle-index.sqlite \
  --file mod/assign/locallib.php
```

Find a definition:

```bash
moodle-indexer find-definition \
  --db-path /path/to/moodle-index.sqlite \
  --symbol get_string
```

JavaScript module lookups use the same command:

```bash
moodle-indexer find-definition \
  --db-path /path/to/moodle-index.sqlite \
  --symbol core/ajax \
  --type js_module
```

Method lookups support both short and fully qualified forms:

```bash
moodle-indexer find-definition \
  --db-path /path/to/moodle-index.sqlite \
  --symbol assign::view

moodle-indexer find-definition \
  --db-path /path/to/moodle-index.sqlite \
  --symbol mod_assign\\external\\start_submission::execute
```

The package can still be run directly for debugging:

```bash
python -m moodle_indexer --help
```

## Example Output

`find-symbol` returns a compact summary of symbol definitions plus structural context:

```json
{
  "command": "find-symbol",
  "status": "ok",
  "data": {
    "query": "discussion_exporter",
    "matches": [
      {
        "component": "mod_forum",
        "container_name": null,
        "file": "mod/forum/classes/external/discussion_exporter.php",
        "file_role": "external_api_class",
        "fqname": "mod_forum\\external\\discussion_exporter",
        "line": 5,
        "name": "discussion_exporter",
        "namespace": "mod_forum\\external",
        "referenced_by": [],
        "relationships": [
          {
            "line": 5,
            "target": "\\external_api",
            "type": "extends"
          }
        ],
        "symbol_type": "class"
      }
    ]
  }
}
```

`find-definition` is designed to feel more like an IDE “go to definition” view.
For supported PHP functions, classes, methods, and Moodle AMD source modules it returns:

- file, line, component, namespace, and owning class
- signature, parameters, defaults, and return type where available
- docblock summary and selected tags
- method modifiers such as visibility, `static`, `final`, and `abstract`
- inheritance/navigation context such as `inheritance_role`,
  `parent_definition`, `overrides_definition`, `implements_definitions`, and a
  bounded `child_overrides` list where available
- for JS modules: canonical source file, build artifact, import metadata,
  superclass module/file, and reverse import examples where available
- bounded `linked_artifacts` chains so a definition can still navigate into
  service -> implementation -> test flows, output -> renderer -> template
  flows, form -> intermediate base -> framework base flows, and JS
  source/import/build flows
- bounded follow-on hops on those linked artifacts so a direct hit can still
  expose one or two trusted next steps without turning into an open-ended graph
- a small number of ranked usage examples plus a compact `usage_summary`

Phase 4A adds two agent-oriented navigation endpoints on top of those same
relationships:

- `find-related-definitions`: bounded related symbols and artifacts around a
  symbol or file
- `suggest-edit-surface`: the likely primary and secondary files/definitions an
  agent would inspect or edit next

Phase 4B adds one more bounded, graph-like view:

- `dependency-neighborhood`: a small confidence-aware neighborhood around a
  symbol or file, split into likely callers, likely callees, linked tests, and
  linked artifact companion sections where the current index has strong local
  evidence

These agent-oriented navigation endpoints are intentionally confidence-aware:

- primary items are usually high-confidence, directly connected artifacts such
  as service definitions, implementation files, concrete tests, output classes,
  renderers, templates, concrete forms, framework bases, JS imports, and JS
  superclass/build links
- primary items are path-deduplicated so the same file does not normally appear
  multiple times under slightly different labels; when multiple relationships
  converge on one file, the strongest label wins and related relationships are
  folded into the same item
- secondary items are usually supporting context or weaker fallbacks
- low-confidence suggestions are intentionally rare; the tool prefers a smaller
  bounded surface over a noisy graph
- JS-oriented outputs keep JS-specific relationship wording such as imports,
  superclass modules, and build artifacts rather than reusing PHP-style
  inheritance wording in Phase 4A navigation responses

For `dependency-neighborhood`, "likely callers" and "likely callees" are
bounded local edges, not a full call graph:

- likely callers come from strong direct evidence such as service
  registrations, direct usage examples, and direct JS importers
- likely callees come from direct linked artifacts such as service
  implementations, output classes, renderers, templates, concrete forms,
  framework bases, JS imports, and JS superclass modules
- linked tests are returned as a first-class section when concrete PHPUnit
  files can be tied directly to the symbol or file
- each section is bounded and confidence-aware so the output stays small enough
  for an agent to inspect immediately

Phase 2 usage examples intentionally prefer precision over recall. The indexer
will rank direct static calls, simple `new ClassName(...)` to `$var->method()`
patterns, service-definition references, and a few other high-confidence
linkages above weaker matches, and it may return zero examples when it cannot
do so without becoming misleading.

Usage examples also include a `usage_kind` and `confidence` field so callers can
distinguish between, for example, a `service_definition`, `test_usage`,
`renderer_usage`, `static_method_call`, `instance_method_call`, or
`js_import_usage`.

Ambiguity is explicit. If a short query such as `execute` matches multiple
methods, the command returns multiple distinguishable matches instead of
pretending there is only one.

`file-context` surfaces the indexed data already known for a file without becoming a dump of the whole database. In addition to raw extracted records, it now includes bounded `linked_artifacts` chains for:

- services: `db/services.php` -> implementation -> likely tests
- rendering: output class <-> renderer <-> Mustache template links
- JavaScript: source module -> imports -> superclass -> build artifact
- entrypoints: high-value Moodle workflow files such as `settings.php`, `locallib.php`, `externallib.php`, and `amd/src/*.js`

Those chains remain intentionally small. The query layer prefers explicit,
trusted follow-on hops that are already derivable from indexed relationships,
for example:

- provider -> concrete form -> intermediate form base -> `lib/formslib.php`
- output class -> template + renderer
- service definition -> implementation -> concrete PHPUnit file

This keeps navigation coherent without turning the indexer into a generic
recursive scoring engine.

`find-definition` now reuses that same linked-artifact model for the defining
file where practical, so moving from a PHP method/class or JS module
definition into the surrounding Moodle feature slice does not require a second
manual lookup.

Example:

```json
{
  "command": "file-context",
  "status": "ok",
  "data": {
    "application_root": "/path/to/moodle/public",
    "repository_root": "/path/to/moodle",
    "component": "mod_forum",
    "file": "mod/forum/lib.php",
    "absolute_path": "/path/to/moodle/public/mod/forum/lib.php",
    "file_role": "lib_file",
    "moodle_path": "mod/forum/lib.php",
    "path_scope": "application",
    "repository_relative_path": "public/mod/forum/lib.php",
    "capability_checks": [
      {
        "capability_name": "mod/forum:viewdiscussion",
        "function_name": "require_capability",
        "line": 7
      }
    ],
    "related_suggestions": [
      {
        "path": "mod/forum/db/access.php",
        "reason": "Capability-related work usually needs the component capability definition file."
      }
    ],
    "symbols": [
      {
        "fqname": "forum_user_can_view_discussion",
        "line": 6,
        "name": "forum_user_can_view_discussion",
        "namespace": null,
        "symbol_type": "function"
      }
    ]
  }
}
```

JSON output is deterministic:

- stable top-level success/error envelopes
- sorted object keys
- predictable list ordering in query responses

`index` reports the paths it detected so you can see exactly what was indexed:

- `input_path`: the raw CLI value passed to `--moodle-path`
- `repository_root`: the normalized checkout root used for scanning
- `application_root`: the detected Moodle application root used for Moodle-native paths
- `layout_type`: `classic` or `split_public`

During indexing, human-readable diagnostics are written to stderr while the final JSON result stays on stdout. The logs distinguish:

- repository scan and discovery
- parsing/extraction progress
- serial SQLite persistence progress
- final counts for discovered, processed, persisted, skipped, and failed files
- worker configuration and lightweight timing information

The final `index` JSON also includes:

- `discovered_files`
- `processed_files`
- `persisted_files`
- `skipped_files`
- `failed_files`
- `ignored_files`
- `worker_usage`
- `timings`

`file-context` uses the repository metadata stored in the SQLite index. After indexing, it only needs `--db-path` and a `--file` value that is either:

- a Moodle-native path such as `mod/forum/lib.php`
- a repository-relative path such as `public/mod/forum/lib.php`
- an absolute path inside the indexed repository

During `index`, the CLI emits phase-based progress on stderr so long-running rebuilds are easier to monitor. The `--workers` option controls parallel extraction threads; SQLite persistence still happens serially in the main process, so the worker count is only one part of overall throughput.

## Moodle Component Coverage

Phase 1 handles common Moodle conventions more carefully than a generic path prefix check. The inference rules cover, among others:

- `mod/*`
- `blocks/*`
- `local/*`
- `admin/tool/*`
- `admin/report/*`
- `auth/*`
- `enrol/*`
- `repository/*`
- `question/type/*`
- `question/behaviour/*`
- `question/format/*`
- `availability/condition/*`
- `course/format/*`
- `grade/report/*`
- `grade/export/*`
- `grade/import/*`
- `editor/*`
- `media/player/*`
- `plagiarism/*`
- `theme/*`
- `payment/gateway/*`
- `contentbank/contenttype/*`

For non-plugin paths, the indexer falls back to sensible core subsystem mapping such as `core_admin`, `core_course`, and `core_question`.

When a plugin declares subplugins in `db/subplugins.json`, files under those declared roots are attributed to the child component rather than the parent plugin. For example, `mod/forum/report/summary/...` is indexed as `forumreport_summary`, so `component-summary --component mod_forum` does not mix in `forumreport_summary` capability definitions.

Service definitions in `db/services.php` are also indexed as first-class records. Older services that point at `externallib.php` through `classpath` are resolved directly to that file, while modern `classname` definitions are resolved through Moodle autoloading conventions to files such as `classes/external/start_submission.php`.

## Testing

The test suite uses synthetic Moodle-like fixture trees for both classic and split layouts under `tests/fixtures/`. It does not require a full Moodle checkout.

Run tests with:

```bash
pytest
```

The current suite validates:

- component inference across a broader set of Moodle path conventions
- file-role classification
- PHP symbol and relationship extraction
- capability extraction
- language string extraction
- file-context output
- repository-root vs application-root path semantics
- component-summary output
- related-file suggestion explanations
- CLI JSON output

The repository also includes a root `_smoke_test/` directory reserved for generated smoke artifacts.

## Current Limitations

- PHP parsing is pragmatic rather than fully semantic. The project prefers parser-based extraction when available and falls back to resilient text-based extraction when needed.
- Relationship extraction is structural, not a full call graph.
- Component inference covers common Moodle layouts, split `public/` application roots, and core subsystem mappings, but it is not yet a complete model of every Moodle convention.
- Related-file suggestions are deterministic heuristics, not ranked by usage data.
- Indexing is rebuild-only in Phase 1.

## Future Directions

- stronger PHP parsing support for more modern syntax patterns
- broader extraction from additional Moodle `db/*.php` conventions
- richer symbol reference resolution
- better result ranking for retrieval workflows
- optional hybrid retrieval layers on top of the structured index

## Development Notes

This project is intentionally a maintainable Phase 1 foundation. The goal is to make Moodle structure explicit in a form an agent or engineer can trust today, while keeping the codebase simple enough to extend in later phases.
