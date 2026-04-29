# agentic_indexer Maintenance Audit

Date: 2026-04-29

## Baseline

- Worktree state at audit start: clean
- Baseline verification: `python3 -m pytest`
- Result: `96 passed, 1 warning`
- Warning: pytest cache write warning under `.pytest_cache`; non-functional

## Current Shape

`agentic_indexer` already has a stronger public-docstring baseline than `agentic_orchestrator`. The immediate maintenance issue is not missing top-level documentation. The bigger issue is concentration of retrieval and packaging logic in a few large modules.

## Hotspots

- `src/moodle_indexer/queries.py` (`10200` lines)
  - Primary maintenance risk
  - Dense retrieval heuristics, ranking logic, and context-bundle assembly
  - Should be handled in small, test-backed slices only
- `src/moodle_indexer/extractors.py` (`959` lines)
  - Large extraction surface with multiple content types
- `src/moodle_indexer/agent_safety.py` (`912` lines)
  - Safety/risk logic is substantial and should stay explicit
- `src/moodle_indexer/cli.py` (`667` lines)
  - Public surface is stable but dispatch/setup can be clearer
- `src/moodle_indexer/php_parser.py` (`654` lines)
  - Parser behavior is important enough that readability changes must stay conservative
- `src/moodle_indexer/runtime_contract.py` (`481` lines)
  - Public contract logic is already documented, but bundle packaging can be broken into clearer helpers

## Audit Findings

- Public top-level docstrings are generally present across `src/moodle_indexer`.
- The first maintenance pass should prioritize:
  - public-surface clarity in `cli.py`
  - helper extraction and intent clarity in `runtime_contract.py`
- The first pass should not start in `queries.py`; that module needs its own targeted follow-up plan.

## Recommended Maintenance Order

1. `cli.py`
   - clarify dispatch flow
   - centralize repeated database-opening behavior
   - preserve CLI output contract exactly
2. `runtime_contract.py`
   - extract compact helper phases for intent and bundle packaging
   - preserve runtime envelope and nested payload shape exactly
3. `queries.py`
   - do a dedicated audit before edits
   - split only test-covered, conceptually isolated regions
4. `agent_safety.py`
   - review for similar helper extraction opportunities after `queries.py`

## First Slice Goal

Complete a low-risk maintenance slice in `cli.py` and `runtime_contract.py` with:

- no runtime behavior changes
- no output-shape changes
- focused tests plus a full-suite verification pass
