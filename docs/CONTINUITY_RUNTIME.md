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
- **constraints**: blocking, important, or advisory truths that should survive every run until explicitly retired.
- **milestones**: intermediate states with explicit completion criteria.
- **commitments**: promises the agent must eventually close or intentionally drop.
- **questions**: unresolved uncertainty that should not vanish from context.
- **facts**: durable learnings accepted from completed steps or external sources. Facts can be superseded explicitly; old versions stay in history but leave the active context.
- **resources**: authoritative project files or external references.
- **current step**: one bounded action with preconditions and expected evidence.


## Correction semantics

Long-running work changes its mind. Continuity therefore avoids two dangerous patterns: silently deleting old state and endlessly injecting obsolete state.

- retiring a constraint changes it from `active` to `retired`, records the reason and timestamp, and keeps the original record in the ledger;
- superseding a fact marks the old fact `superseded`, links it to the replacement, and only the active replacement enters future context packs;
- commitments are closed as `done` or `dropped`;
- questions are explicitly resolved;
- milestones transition rather than being overwritten.

The current context is therefore a projection of live obligations and beliefs, while the ledger preserves how those beliefs changed.


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
    +--> record_evidence -> source + pass/fail/unknown + verified flag
    |
    +--> finish_step -> evidence gate -> learnings + optional milestone completion
    |
    └--> fail_step   -> classified failure recorded in the ledger
```

Continuity does not pretend an agent can certify its own success. If a step declares expected evidence, it cannot finish until every criterion has a latest **verified passing** record. Sources explicitly labelled `agent`, `model`, `llm`, or `self` cannot mark their own evidence verified. CI, deterministic graders, humans, or domain adapters can supply verified records.

Milestone completion has a second gate: every criterion declared on that milestone must also have verified passing evidence in the finishing step. A step may bind only to the currently active milestone, and the active milestone cannot be switched underneath a running bound step.

The `verified` flag is still a caller assertion, not cryptographic provenance. A host with direct write access can lie about a source or rewrite the whole workspace. The hash chain detects accidental/casual mutation, not a malicious actor with filesystem control. An emergency override remains possible only with an explicit reason preserved in the ledger.

## CLI

From `engine/`:

```bash
python continuity_cli.py init ./demo \
  --title "Ship parser" \
  --objective "Implement and verify a backwards-compatible parser"

python continuity_cli.py constraint ./demo "Never delete user-owned source."
python continuity_cli.py fact ./demo "The API endpoint is /v1"

# Corrections never delete history:
python continuity_cli.py fact ./demo "The API endpoint is /v2" \
  --supersedes fact_xxx
python continuity_cli.py constraint-retire ./demo constraint_xxx \
  "The migration replaced this constraint"

python continuity_cli.py milestone ./demo "Parser" \
  --criterion "Regression tests pass" --activate
python continuity_cli.py commitment ./demo "Keep API v1 compatible"
python continuity_cli.py question ./demo "Which schema is canonical?"
python continuity_cli.py resource ./demo docs/schema.md --role source-of-truth

# Long-lived state can be closed or advanced instead of accumulating forever:
python continuity_cli.py commitment-close ./demo commitment_xxx
python continuity_cli.py question-resolve ./demo question_xxx "schema/v1"
python continuity_cli.py milestone-activate ./demo milestone_xxx

python continuity_cli.py step-begin ./demo "Implement recovery" \
  --expect "Parser regression suite passes"

python continuity_cli.py step-evidence ./demo \
  "Parser regression suite passes" "24 parser tests passed" \
  --source ci --status pass --verified

python continuity_cli.py step-finish ./demo "Recovery implemented" \
  --learning "Recovery must preserve token spans"

python continuity_cli.py context ./demo
python continuity_cli.py verify ./demo
```

## NovelKit adapter

```bash
python continuity_cli.py novelkit-bootstrap /path/to/book
```

The adapter creates Continuity state alongside the book, mapping NovelKit sources into generic primitives without changing the existing story engine:

- `redlines.json` (and optional rendered redline table) -> blocking constraint source;
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
- model-based grading;
- distributed locking.

Those belong above or beside this layer.

The v0.1 ledger stores a full resulting state snapshot in every event. This deliberately favors simple recovery and auditable history over storage efficiency. Context sent back to an agent stays bounded, but the on-disk ledger is not yet optimized for extremely large, multi-thousand-step projects. A future format can introduce checkpointed/delta events behind a new storage version without changing the semantic state schema.

The next useful expansion is a verifier adapter interface that can accept deterministic CI evidence or human approval without making the state core dependent on any one agent framework.
