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
- deterministic file-role classification
- parser-first PHP extraction with resilient fallback logic
- symbol indexing for classes, interfaces, traits, functions, and methods
- structural relationship indexing for `extends`, `implements`, and method-to-class ownership
- capability extraction from `db/access.php`
- language string extraction from `lang/en/*.php`
- detection of obvious `require_capability`, `has_capability`, and `get_string` usage
- PHPUnit and Behat discovery
- related-file suggestions with explanation strings
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
- `extractors.py`: Moodle-specific extraction for relationships, capabilities, strings, and tests
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

## CLI Usage

The tool does not assume it lives beside the Moodle checkout. Always pass the Moodle repository path explicitly.

The path you pass with `--moodle-path` is the source of truth for stored file paths. If you point the CLI at the actual Moodle web root, indexed paths are stored relative to that exact directory, for example `mod/forum/lib.php` rather than an accidental wrapper prefix such as `public/mod/forum/lib.php`.

If you accidentally point the CLI at a one-level hosting wrapper that contains a single obvious Moodle root such as `public/`, the indexer will detect that nested Moodle root and still persist repo-relative paths like `mod/forum/lib.php`.

Build a fresh index:

```bash
moodle-indexer index \
  --moodle-path /path/to/moodle \
  --db-path /path/to/moodle-index.sqlite
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
  --moodle-path /path/to/moodle \
  --file admin/tool/demo/settings.php
```

You can also run the package directly:

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

`file-context` surfaces the indexed data already known for a file without becoming a dump of the whole database:

```json
{
  "command": "file-context",
  "status": "ok",
  "data": {
    "component": "mod_forum",
    "file": "mod/forum/lib.php",
    "file_role": "lib_file",
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

`file-context` uses the repository metadata stored in the SQLite index. After indexing, it only needs `--db-path` and a repo-relative `--file` value such as `mod/forum/lib.php`.

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

## Testing

The test suite uses a synthetic Moodle-like fixture tree under `tests/fixtures/moodle_sample/`. It does not require a full Moodle checkout.

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
- component-summary output
- related-file suggestion explanations
- CLI JSON output

The repository also includes a root `_smoke_test/` directory reserved for generated smoke artifacts.

## Current Limitations

- PHP parsing is pragmatic rather than fully semantic. The project prefers parser-based extraction when available and falls back to resilient text-based extraction when needed.
- Relationship extraction is structural, not a full call graph.
- Component inference covers common Moodle layouts and core subsystem mappings, but it is not yet a complete model of every Moodle convention.
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
