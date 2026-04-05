# Validation Guide

## Why Validation Matters Here

This project is built around bounded, explainable outputs. That means quality is not just about whether a file appears somewhere in the result.

Good validation checks whether outputs are:

- coherent
- bounded
- well-ranked
- slice-pure
- actionable for a human or agent

## Canonical Validation Slices

The most important recurring validation slices are:

### Service slice

Typical chain:

- implementation
- `db/services.php`
- concrete PHPUnit test

### Rendering slice

Typical chain:

- legacy method or entrypoint
- output/renderable class
- renderer
- template

### Provider/form slice

Typical chain:

- provider method
- concrete form
- intermediate form base
- framework base

### JS slice

Typical chain:

- AMD source module
- imports
- superclass module
- build artifact

### Free-text service-pattern slice

Typical pattern:

- representative external implementation
- representative service registration
- representative PHPUnit coverage
- contract and guardrail guidance

## Where Validation Artifacts Live

Human-review validation bundles live in:

- [validation_runs/](/Users/mattp/projects/agentic_indexer/validation_runs)

These are useful because they capture real outputs from the local index and local Moodle checkout, not only fixture-based tests.

## Automated Checks

Typical commands:

```bash
python3 -m pytest -q
python3 -m pytest -q tests/test_indexer.py tests/test_cli.py
python3 -m pytest -q tests/test_agent_workflows.py tests/test_cli_agent_workflows.py
```

## What “Good” Output Looks Like

At a high level, good output should:

- surface the direct implementation early
- keep concrete tests above generic hints
- keep service, rendering, provider, and JS slices semantically pure
- avoid dumping giant neighborhoods
- provide reasons that help the next decision

## When To Add New Validation

Add or expand validation when:

- a new command is introduced
- ranking or confidence logic changes materially
- slice purity or boundedness rules change
- free-text behavior is recalibrated

For future work, prefer turning real validation findings into regression tests whenever possible.
