# Moodle AI Indexer

Moodle AI Indexer is a Phase 1, SQLite-backed code indexer for a local Moodle LMS checkout. It is designed for agentic coding systems and engineers who need better structural navigation than plain text search, without jumping straight to a complex universal code graph.

The MVP focuses on practical Moodle-aware indexing:

- infer Moodle components/plugins from file paths
- classify common Moodle file roles
- extract useful PHP symbols and basic relationships
- capture capabilities and language strings
- discover PHPUnit and Behat artifacts
- return deterministic JSON from a compact CLI

## Why This Exists

Moodle has strong repository conventions, rich plugin boundaries, and a lot of metadata spread across files like `db/access.php`, `lang/en/*.php`, `db/services.php`, renderers, templates, and tests. Generic code search often misses those conventions.

This project builds a small structured index so an AI agent can answer questions such as:

- what is this symbol or file?
- where is it defined?
- which Moodle component owns it?
- what related files are usually needed for a complete change?
- where are the capabilities, strings, and tests?

## Phase 1 Scope

Phase 1 includes:

- full rebuild indexing into SQLite
- Moodle component inference from repository paths
- path-based file-role classification
- parser-first PHP symbol extraction, with fallback logic for resilience
- basic relationship extraction for `extends`, `implements`, and defined methods
- capability extraction from `db/access.php`
- language string extraction from `lang/en/*.php`
- detection of common capability and `get_string` usages
- discovery of PHPUnit classes/methods and Behat features/contexts
- a small JSON CLI for indexing and querying
- deterministic, explainable related-file suggestions

Phase 1 does not include:

- embeddings or vector search
- incremental indexing
- a web UI
- live editor integration
- advanced JavaScript analysis
- a precise call graph
- runtime analysis

## High-Level Design

The project is structured as a clean installable Python package under [`src/moodle_indexer`](/Users/mattp/projects/agentic_indexer/src/moodle_indexer).

Key modules:

- [`cli.py`](/Users/mattp/projects/agentic_indexer/src/moodle_indexer/cli.py): CLI entrypoint and command handling
- [`indexer.py`](/Users/mattp/projects/agentic_indexer/src/moodle_indexer/indexer.py): full rebuild orchestration
- [`scanner.py`](/Users/mattp/projects/agentic_indexer/src/moodle_indexer/scanner.py): repository scanning
- [`components.py`](/Users/mattp/projects/agentic_indexer/src/moodle_indexer/components.py): Moodle component inference
- [`file_roles.py`](/Users/mattp/projects/agentic_indexer/src/moodle_indexer/file_roles.py): deterministic file-role classification
- [`php_parser.py`](/Users/mattp/projects/agentic_indexer/src/moodle_indexer/php_parser.py): parser-first PHP symbol extraction
- [`extractors.py`](/Users/mattp/projects/agentic_indexer/src/moodle_indexer/extractors.py): Moodle-specific metadata extraction
- [`store.py`](/Users/mattp/projects/agentic_indexer/src/moodle_indexer/store.py): SQLite schema and persistence
- [`queries.py`](/Users/mattp/projects/agentic_indexer/src/moodle_indexer/queries.py): query services for CLI commands
- [`suggestions.py`](/Users/mattp/projects/agentic_indexer/src/moodle_indexer/suggestions.py): related-file heuristics

The data model stays intentionally small and normalized:

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

## Project Structure

```text
agentic_indexer/
├── README.md
├── requirements.txt
├── pyproject.toml
├── src/
│   └── moodle_indexer/
│       ├── cli.py
│       ├── components.py
│       ├── config.py
│       ├── extractors.py
│       ├── file_roles.py
│       ├── indexer.py
│       ├── json_output.py
│       ├── models.py
│       ├── paths.py
│       ├── php_parser.py
│       ├── queries.py
│       ├── scanner.py
│       ├── store.py
│       └── suggestions.py
└── tests/
    ├── fixtures/
    └── test_*.py
```

## Setup

Python 3.12+ is assumed.

Install dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

`requirements.txt` is the baseline dependency file. `pyproject.toml` adds package metadata and the CLI entrypoint.

## CLI Usage

The tool never assumes it lives beside Moodle. Always pass the Moodle checkout path explicitly.

Build a fresh index:

```bash
moodle-indexer index \
  --moodle-path /path/to/moodle \
  --db-path /tmp/moodle-index.sqlite
```

Find a symbol:

```bash
moodle-indexer find-symbol \
  --db-path /tmp/moodle-index.sqlite \
  --symbol discussion_exporter
```

Inspect file context:

```bash
moodle-indexer file-context \
  --db-path /tmp/moodle-index.sqlite \
  --moodle-path /path/to/moodle \
  --file mod/forum/db/access.php
```

Summarize a component:

```bash
moodle-indexer component-summary \
  --db-path /tmp/moodle-index.sqlite \
  --component mod_forum
```

Suggest related files:

```bash
moodle-indexer suggest-related \
  --db-path /tmp/moodle-index.sqlite \
  --moodle-path /path/to/moodle \
  --file admin/tool/demo/settings.php
```

You can also run the package directly:

```bash
python -m moodle_indexer --help
```

## Example JSON Output

`find-symbol` example:

```json
{
  "command": "find-symbol",
  "status": "ok",
  "data": {
    "query": "discussion_exporter",
    "matches": [
      {
        "component": "mod_forum",
        "file": "mod/forum/classes/external/discussion_exporter.php",
        "fqname": "mod_forum\\external\\discussion_exporter",
        "line": 5,
        "name": "discussion_exporter",
        "namespace": "mod_forum\\external",
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

`file-context` example:

```json
{
  "command": "file-context",
  "status": "ok",
  "data": {
    "component": "mod_forum",
    "file": "mod/forum/db/access.php",
    "file_role": "access_definition",
    "capabilities": [
      {
        "name": "mod/forum:viewdiscussion",
        "captype": "read",
        "contextlevel": "CONTEXT_MODULE"
      }
    ],
    "related_suggestions": [
      {
        "path": "mod/forum/lang/en/mod_forum.php",
        "reason": "access.php changes often require new or updated language strings for mod_forum."
      }
    ]
  }
}
```

JSON output is deterministic and machine-friendly:

- stable top-level envelope
- sorted object keys
- predictable list ordering in query responses

## Testing

The test suite uses a synthetic Moodle-like fixture tree in [`tests/fixtures/moodle_sample`](/Users/mattp/projects/agentic_indexer/tests/fixtures/moodle_sample). It does not require a real Moodle checkout.

Run tests with:

```bash
pytest
```

The repository also includes a root [`_smoke_test`](/Users/mattp/projects/agentic_indexer/_smoke_test) directory reserved for any generated smoke artifacts.

## Current Limitations

- PHP parsing uses `phply`, which is practical for an MVP but not a full modern PHP semantic engine.
- Reference extraction is intentionally shallow; it supports likely definition and relationship lookup rather than precise call resolution.
- Component inference covers common Moodle layouts and sensible core mappings, but it is not yet a complete convention engine.
- Related-file suggestions are heuristic and deterministic, not learned or usage-ranked.
- Indexing is rebuild-only in Phase 1.

## Suggested Future Phases

- incremental indexing and change detection
- richer Moodle convention extraction from more `db/*.php` files
- improved PHP parsing with stronger modern syntax support
- better symbol reference resolution
- plugin-aware retrieval ranking
- optional embeddings or hybrid retrieval
- editor and agent workflow integrations

## Development Notes

The implementation aims to be easy to extend rather than exhaustive. Each major module has a short docstring describing its role, and the SQLite schema is intentionally straightforward so a new engineer can add tables, relationships, or new extractors without untangling a speculative framework.
