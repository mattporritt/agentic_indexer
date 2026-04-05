# Architecture

## Purpose

Moodle AI Indexer is a bounded code-intelligence system for Moodle LMS.

Its purpose is to:

- index Moodle-aware structural facts
- expose them through deterministic JSON commands
- synthesize bounded retrieval, planning, safety, and packaging layers on top

It is intentionally not a generic program-analysis platform or autonomous runtime.

## Layers

### 1. Indexing and extraction

Main modules:

- [indexer.py](/Users/mattp/projects/agentic_indexer/src/moodle_indexer/indexer.py)
- [extractors.py](/Users/mattp/projects/agentic_indexer/src/moodle_indexer/extractors.py)
- [store.py](/Users/mattp/projects/agentic_indexer/src/moodle_indexer/store.py)

Responsibilities:

- scan the repository
- infer Moodle components and file roles
- extract PHP symbols and relationships
- extract service definitions, tests, capabilities, strings, and JS module metadata
- persist those facts into SQLite

This layer is the source of truth for everything above it.

### 2. Structural navigation

Main commands:

- `find-symbol`
- `find-definition`
- `file-context`
- `suggest-related`

Responsibilities:

- resolve canonical anchors
- expose direct structural metadata
- give users and agents a trustworthy first lookup step

### 3. Bounded navigation

Main commands:

- `find-related-definitions`
- `suggest-edit-surface`
- `dependency-neighborhood`

Responsibilities:

- surface the most relevant local companions
- stay bounded and confidence-aware
- avoid pretending to provide a full graph

### 4. Hybrid retrieval

Main command:

- `semantic-context`

Responsibilities:

- preserve the structural anchor as the spine
- add bounded lexical + hashed-vector similarity
- surface similar examples without drowning out direct local context

### 5. Planning and safety

Main commands:

- `propose-change-plan`
- `assess-test-impact`
- `execution-guardrails`

Responsibilities:

- turn structural and semantic evidence into conservative edit plans
- estimate likely validation surfaces
- summarize pre/post-edit checks and local risk

### 6. Context packaging

Main command:

- `build-context-bundle`

Responsibilities:

- package the existing trusted outputs into a compact working set
- separate essential, supporting, and optional context
- make the result usable in a real agent context window

## Design Principles

### Structural anchors stay primary

Every higher-level feature starts from structural anchors rather than replacing them.

### Boundedness matters more than exhaustiveness

The project is optimized for usable working sets, not giant flattened graphs.

### Explanations must stay practical

Outputs should help a developer or agent decide what to inspect next, not just list nearby files.

### Safety is conservative

Planning and guardrails prefer under-inclusion to speculative noise.

## What To Extend Carefully

The safest extension order is usually:

1. add extraction
2. expose it through structural lookup
3. use it in bounded navigation
4. package it into planning/safety
5. include it in context bundles only after it is trustworthy
