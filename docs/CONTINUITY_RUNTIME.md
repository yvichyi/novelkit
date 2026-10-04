# Continuity Runtime

Continuity is an experimental, zero-dependency semantic state layer extracted from the design lessons inside NovelKit.

It is **not** another LLM framework. It does not call models, dispatch tools, schedule retries, or replace conversation memory. Its job is narrower:

> Keep a long-running agent honest about what it is trying to achieve, what must remain true, what it has promised, what is unresolved, what evidence it produced, and what the next run needs to know.

## Why this exists

NovelKit already solves a domain-specific version of the long-horizon problem:

- world state must survive hundreds of chapters;
- hard redlines must remain active;
- stages define intermediate objectives;
- foreshadowing creates commitments that may live for dozens of steps;
- chapter review produces evidence and corrections;
- a ledger prevents the model from silently rewriting history.

Continuity extracts those mechanics without the fiction vocabulary.

## Position in an agent stack

```text
model / agent SDK
       |
       v
conversation memory --------+
durable workflow engine ----+---- execution
                            |
                            v
                     Continuity
                     semantic task state
                            |
       +--------------------+--------------------+
       |                    |                    |
   constraints          commitments          milestones
       |                    |                    |
       +------------ evidence / facts ---------+
                            |
                            v
                     next context pack
```

A session remembers **what was said**. A durable executor remembers **where code was running**. Continuity remembers **what the project means across runs**.

## Workspace

Continuity lives inside a project:

```text
project/
└── .continuity/
    ├── ledger.jsonl   # authoritative append-only event chain
    └── state.json     # repairable current-state projection
```

Every event contains the full resulting state snapshot and is chained with SHA-256. If `state.json` is missing or stale, it is rebuilt from the ledger.

The chain is an integrity mechanism for accidental/casual mutation, not a cryptographic identity or trust system.

## State primitives

- **objective**: the long-horizon outcome.
- **constraints**: blocking, important, or advisory truths that should survive every run.
- **milestones**: intermediate states with explicit completion criteria.
- **commitments**: promises the agent must eventually close or intentionally drop.
- **questions**: unresolved uncertainty that should not vanish from context.
- **facts**: durable learnings accepted from completed steps.
- **resources**: authoritative project files or external references.
- **current step**: one bounded action with preconditions and expected evidence.

## Step lifecycle

```text
context pack
    |
    v
begin_step
    |
    +--> objective
    +--> preconditions
    +--> expected evidence
    +--> baseline revision + context hash
    |
    v
agent/tool execution
    |
    +--> finish_step -> evidence + learnings + optional milestone completion
    |
    └--> fail_step   -> classified failure recorded in the ledger
```

The runtime deliberately does not decide whether evidence is *good*. A model, deterministic grader, CI system, human, or domain adapter can do that. Continuity makes the evidence requirement persistent and inspectable.

## CLI

From `engine/`:

```bash
python continuity_cli.py init ./demo \
  --title "Ship parser" \
  --objective "Implement and verify a backwards-compatible parser"

python continuity_cli.py constraint ./demo "Never delete user-owned source."
python continuity_cli.py milestone ./demo "Parser" \
  --criterion "Regression tests pass" --activate

python continuity_cli.py step-begin ./demo "Implement recovery" \
  --expect "Parser regression suite passes"

python continuity_cli.py step-finish ./demo "Recovery implemented" \
  --evidence "24 parser tests passed" \
  --learning "Recovery must preserve token spans"

python continuity_cli.py context ./demo
python continuity_cli.py verify ./demo
```

## NovelKit adapter

```bash
python continuity_cli.py novelkit-bootstrap /path/to/book
```

The adapter creates Continuity state alongside the book, mapping NovelKit sources into generic primitives without changing the existing story engine:

- redline table -> blocking constraint source;
- consistency checklist -> blocking constraint source;
- stage anchors -> milestones;
- event / foreshadow ledgers -> history / commitment resources;
- unresolved foreshadowing -> a long-lived commitment.

This is deliberately an adapter, not a rewrite. NovelKit remains usable even if Continuity is removed.

## Current boundary

v0.1 intentionally stops before:

- LLM/provider integration;
- tool execution;
- multi-agent routing;
- semantic retrieval;
- automatic grading;
- distributed locking.

Those belong above or beside this layer. The next useful expansion is an evaluator interface that can accept deterministic CI evidence, human approval, or model review without making the state core dependent on any one agent framework.
