# Agent Usage Guide

## Purpose

This project is meant to be used *by* an agent, not *as* the agent.

Use it to gather bounded, trustworthy Moodle context before editing. Do not expect it to:

- modify code
- run a repair loop
- orchestrate CI
- decide the final patch strategy for you

## Recommended Flow

### 1. Resolve the anchor

Use:

- `find-definition` for symbols
- `file-context` for files

Goal:

- identify the canonical implementation anchor

### 2. Expand to nearby structural context

Use:

- `find-related-definitions`
- `dependency-neighborhood`

Goal:

- identify direct companions such as tests, services, renderers, forms, templates, or JS imports

### 3. Estimate likely edit scope

Use:

- `suggest-edit-surface`
- `propose-change-plan`

Goal:

- separate required edits from likely or optional follow-up edits

### 4. Pull similar examples only if needed

Use:

- `semantic-context`

Goal:

- find bounded examples or semantically similar patterns without abandoning the structural anchor

### 5. Add validation and safety

Use:

- `assess-test-impact`
- `execution-guardrails`

Goal:

- understand likely tests, contract checks, build steps, and local risk

### 6. Package the working set

Use:

- `build-context-bundle`

Goal:

- produce one compact context package that can be loaded into the agent working set

## Recommended Default

For most real editing tasks, the best default pattern is:

1. `find-definition`
2. `build-context-bundle`

Then only fall back to individual intermediate commands if the bundle is still not specific enough.

## How To Read The Outputs

### Primary items

These are the most important files or symbols to inspect first.

### Supporting items

These are direct companions that help interpret the local slice safely.

### Optional items

These are lower-priority examples or references. They are useful when the local slice is unclear, but they should not displace the primary working set.

### Confidence

Treat high-confidence structural items as the strongest basis for editing decisions. Medium-confidence semantic examples are usually best treated as references, not direct edit targets.

## Boundaries

This project deliberately stops at:

- context selection
- bounded planning
- test impact
- safety guidance
- compact packaging

Patch synthesis, execution, and review policy belong to the consuming agent or workflow.
