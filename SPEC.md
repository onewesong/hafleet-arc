# HAFleet ARC Service Specification

Status: Draft v1 (implementation-aligned)

Purpose: Define the finite-lifecycle HAFleet ARC service that turns a ROOT
requirement tree into a tested, traceable, Git-backed project through a small
set of cooperating coding-agent roles.

This document is intentionally written as a service contract rather than as a
description of one particular Python module. The current reference
implementation lives under `hafleet_arc/` and `arcbench-agent-runtime/`.

## Normative language

The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHOULD**, **SHOULD NOT**,
**RECOMMENDED**, **MAY**, and **OPTIONAL** in this document are to be
interpreted as described in RFC 2119.

`Implementation-defined` means that the behavior is part of the HAFleet
contract but this document does not require one universal implementation.
Implementations MUST document their selected behavior.

## 1. Problem statement

HAFleet ARC is a finite, resumable coding-agent factory. It accepts a
requirement bundle and an output workspace, then coordinates architecture,
planning, implementation, review, integration, and delivery validation.

The service solves these operational problems:

- converting a requirement tree into a deterministic module execution plan;
- keeping architecture, implementation plans, source changes, traceability,
  events, and Git checkpoints in one inspectable output workspace;
- separating role responsibilities while preserving one coherent project;
- supporting sequential execution and optional isolated parallel worktrees;
- retrying transient coding-agent failures without silently accepting an empty
  turn;
- pausing at safe phase boundaries and resuming at module granularity; and
- proving that a web delivery builds and starts before marking the run complete.

HAFleet ARC is not a general issue-tracker daemon. It does not poll an external
ticket system, keep a permanently resident worker process, or guarantee that a
project is production-ready beyond the configured requirement and postflight
checks.

## 2. Goals and non-goals

### 2.1 Goals

HAFleet ARC MUST:

1. validate a ROOT requirement tree before agent work begins;
2. preserve existing files when initializing or resuming an output workspace;
3. create a global architecture contract before feature modules are implemented;
4. process direct ROOT children in stable, dependency-aware order;
5. give every module a plan, implementation turn, read-only review loop, and
   durable checkpoint;
6. emit machine-readable lifecycle and requirement-state events;
7. keep runtime state, Codex home, checkpoints, and traceability under the
   output workspace unless explicitly configured otherwise;
8. use role-specific Git identities without changing the user's global Git
   configuration;
9. provide bounded retries, timeouts, pause handling, and postflight repair;
10. expose a read-only local dashboard when explicitly enabled; and
11. leave a completed run with a runnable project and a final integration
    checkpoint.

### 2.2 Non-goals

HAFleet ARC does not:

- implement a hosted multi-tenant control plane;
- prescribe a specific model vendor or model name;
- replace a general-purpose workflow engine; its bounded pipeline YAML is scoped
  to the ARC run and supported node types;
- replace the coding agent's own file-editing, testing, or tool protocol;
- write to a remote Git provider or open pull requests;
- expose a mutable dashboard control API;
- guarantee uninterrupted execution across process crashes; or
- require every task type to use web-specific build and HTTP checks.

## 3. System overview

### 3.1 Main components

1. **CLI entrypoint** (`main.py`)
   - Parses requirement, output, task-type, port, parallel, and dashboard
     options.
   - Creates the runtime and owns the top-level process lifecycle.

2. **Requirement loader and module planner**
   - Loads `requirements.yaml`.
   - Requires a node with `id: ROOT` and at least one direct child.
   - Normalizes each direct child into a `RequirementModule`.
   - Produces stable, dependency-aware module order.

3. **Fleet orchestrator** (`FleetOrchestrator`)
   - Owns phase transitions, module sequencing, pause checks, checkpoint
     updates, message routing, review loops, retries through the driver, and
     final integration.

4. **Codex fleet driver** (`CodexFleet`)
   - Starts and reuses one in-process agent thread per `(role, workspace)`.
   - Sends turns, enforces timeouts, detects empty turns, and retries transient
     failures with a fresh role thread.

5. **ARC runtime** (`AgentRuntime`)
   - Provides event emission, traceability persistence, Git operations, and
     environment-derived paths.

6. **Workspace manager** (`WorktreeManager`, optional execution path)
   - Creates module branches/worktrees for parallel mode.
   - Commits module results, cherry-picks them into the main workspace, and
     removes successful worktrees.

7. **Postflight validator**
   - Checks required web structure, builds the frontend, starts the backend on
     a safe smoke port, waits for readiness, and stops rehearsal processes.

8. **Dashboard** (optional)
   - Reads output files and Git history only.
   - Shows roles, module states, conversations, traceability, and role/stage
     scoped file diffs.

### 3.2 Layering contract

The reference implementation is organized into these boundaries:

- **Policy**: role prompts and architecture instructions;
- **Coordination**: orchestrator and checkpoint state machine;
- **Execution**: Codex turns, filesystem, Git, worktrees, and postflight;
- **Persistence**: JSON, JSONL, traceability, and Git history;
- **Presentation**: dashboard API and static/Vite UI.

Role prompts MUST NOT be treated as the source of durable state. Durable state
belongs in checkpoint files, events, traceability stores, plans, and Git.

## 4. Inputs and outputs

### 4.1 Required inputs

The CLI accepts:

```text
python3 main.py REQUIREMENT_DIR --output-dir OUTPUT_DIR --type web|cli|android
```

`REQUIREMENT_DIR` MUST contain `requirements.yaml`. The normalized root MUST
have `id: ROOT` and at least one direct child module.

### 4.2 Output workspace

The output directory is the authoritative project workspace. HAFleet MUST NOT
overwrite an existing file when copying starter/template contents. A typical
workspace contains:

```text
<output>/
  .arc/
    checkpoint.json
    runner-events.jsonl
    traceability/
    hafleet/
      architecture.md
      plans/<module-id>.md
      pipeline.yaml                 # optional declarative pipeline
      messages.jsonl                # append-only Agent/Pipeline bus
      codex-home/
      worktrees/<module-id>/       # parallel mode only
  frontend/                         # web task contract
  backend/                          # web task contract
```

`CODEX_HOME` MUST be redirected to `.arc/hafleet/codex-home` for the run so
agent state does not depend on a writable user home directory.

The built-in pipeline and role prompts are maintained in
`hafleet_arc/pipeline.yaml`. A run-local `.arc/hafleet/pipeline.yaml` overrides
the node graph and may override individual `roles.<role>` prompt strings; omitted
role prompts inherit the built-in definitions. Orchestration policy and Agent
instructions are therefore versioned together in YAML rather than hard-coded in
the driver.

### 4.3 Completion result

A successful run MUST have:

- `runner_state=completed` in `runner-events.jsonl`;
- `architecture_completed=true`;
- every planned module in `checkpoint.json.completed`;
- `final_review_completed=true`;
- a final checkpoint named `ROOT: final HAFleet integration review` (unless
  Git reports there was nothing new to commit); and
- for web tasks, a passing postflight on the configured smoke port.

## 5. Domain model

### 5.1 Requirement tree

A requirement node is an opaque JSON object with at least an `id`, name or
description, type, and optional children/dependencies. HAFleet MUST preserve
the complete tree in traceability storage. Direct children of ROOT are the
execution modules; descendants remain inside their parent module subtree.

### 5.2 Requirement module

Logical fields:

- `node_id`: stable module identifier, for example `REQ-2`;
- `name`: display name;
- `index` and `total`: stable execution position;
- `dependencies`: direct module dependencies;
- `subtree`: complete requirement subtree supplied to agents.

### 5.3 Run state

The run is one finite state machine:

```text
initialized
  -> architecture
  -> module.design
  -> module.implement
  -> module.review
  -> module.checkpoint
  -> next module
  -> final-review
  -> postflight
  -> final-checkpoint
  -> completed
```

Any unrecoverable error transitions the process to `failed`. A pause request
transitions it to `paused` at the current safe boundary. A resumed invocation
reads the checkpoint and skips completed modules.

### 5.4 Agent session

An agent session is identified by the Codex SDK and persisted in Codex JSONL
under the output-local Codex home. HAFleet treats a session as operational
metadata, not as the source of truth for completion.

The in-process reuse key is `(role, workspace_path)`. A role thread is therefore
**run-scoped**, not a permanent resident service: it stays reusable while the
HAFleet process is alive, is discarded after a retryable failure, and is not
required to survive process exit.

### 5.5 Workspace

In sequential mode every role operates in the main output workspace. In
parallel mode each module receives an isolated Git worktree and branch under
`.arc/hafleet/worktrees/`. Agents in a parallel module MUST modify only their
assigned worktree.

## 6. Role contract

### 6.1 Architect

The architect runs once per output workspace. It receives the full ROOT tree
and MUST:

- write `.arc/hafleet/architecture.md`;
- establish or refactor a minimal runnable project skeleton;
- define frontend/backend boundaries, persistence, APIs, validation,
  permissions, testing, and module ownership; and
- avoid implementing the complete feature tree during architecture.

The architecture checkpoint is `ROOT: architecture scaffold` and uses the
`HAFleet-Architect <architect@hafleet.local>` identity by default.

### 6.2 Planner

For each incomplete module, the planner receives the module subtree, current
repository context, completed module IDs, and architecture path. It MUST write
only the concrete plan to:

```text
.arc/hafleet/plans/<module-id>.md
```

The plan SHOULD cover data model, routes/UI, persistence, validation,
scenarios, and verification. A missing or empty plan is a planner failure and
may trigger one corrective planner turn before the module fails.

### 6.3 Implementer

The implementer reads the architecture and module plan, then implements the
entire module subtree. It MUST preserve earlier behavior, use real persistence
where required, run focused checks, and avoid starting long-running servers.

### 6.4 Reviewer

The reviewer runs after implementation in a read-only sandbox. It MUST test
against the requirement scenarios, inspect cross-module behavior, and return a
structured verdict with blocker, major, minor, or info findings. It MUST NOT
modify project files or Git state. Blocker/major findings are routed back to
the implementer through the message bus; the reviewer runs again after repair
until the loop passes or reaches its configured limit.

### 6.5 Postflight

Postflight is a deterministic delivery stage, not a separate coding-agent
role. It validates the final artifact and may route exact failures back to an
implementer repair turn. The final Git checkpoint is attributed to Postflight.

## 7. Message and control flow

### 7.1 Coordinator-to-agent messages

The orchestrator sends a role turn containing:

- task type;
- requirement source and output workspace;
- module position and ID, if applicable;
- previously completed module IDs;
- architecture and plan paths;
- parallel worktree path and branch, if applicable; and
- the complete module subtree or full ROOT tree for architecture.

The message is a prompt to the coding agent and is persisted in the run-local
append-only message bus at `.arc/hafleet/messages.jsonl`. The resulting files,
events, feedback, and Git commit remain durable outputs as well.

### 7.2 Agent-to-coordinator results

The Codex driver returns a turn result and may produce project file changes.
HAFleet considers a turn successful only if it has a usable final response or
changed project files. A result with neither is an `empty turn` failure.

The driver logs start, finish, failure, attempt number, timeout, and file-count
delta. The orchestrator then validates required artifacts (for example, the
planner file) before advancing.

### 7.3 Runtime event messages

Events are append-only JSONL records. Important event families are:

- `runner_state`: `running`, `paused`, `failed`, `completed`;
- `requirement_state`: node ID, phase, status, timestamp, message;
- `signal`: refresh hints such as commit-history or traceability changes; and
- traceability records emitted by the runtime and skills.

The event stream is observability data. It MUST NOT be used as the only resume
source; `checkpoint.json` is authoritative for module completion and pause
state.

### 7.4 Message bus and dashboard stream

The Agent message bus is an append-only JSONL log at
`.arc/hafleet/messages.jsonl`. Each envelope has a monotonic `sequence`, unique
`id`, run and conversation identifiers, sender/recipient, kind, module/phase/round,
timestamp, and structured payload. Message kinds include `turn.request`,
`turn.started`, `turn.completed`, `agent.message`, `review.feedback`,
`review.verdict`, `operation.*`, `pipeline.state`, and `checkpoint.created`.

The bus supports replay after a sequence cursor, idempotent publication by message
ID, and in-process subscriptions. Subscribers also poll the durable file, so a
Dashboard process separate from the orchestrator receives new messages without
sharing Python objects. `GET /api/stream` exposes the same stream as SSE, honors
`Last-Event-ID` and optional `module_id`, and emits periodic heartbeat events.
The stream is read-only and does not replace `/api/state` or session detail APIs.

### 7.4 Phase transition sequence

For a sequential module, the required sequence is:

1. checkpoint `design` and emit planner started;
2. run planner and verify plan file;
3. emit planner/design completed;
4. checkpoint `implement` and emit implementer started;
5. run implementer;
6. checkpoint `review_loop` and run the read-only reviewer;
7. route structured blocker/major feedback to implementer and repeat until
   approved or bounded failure;
8. emit implementation completed;
9. commit the module checkpoint as reviewer;
10. add the module to `checkpoint.json.completed`; and
11. continue to the next module.

The checkpoint MUST be created before the module is marked completed.

## 8. Sequential and parallel execution

### 8.1 Sequential mode

Sequential mode is the default. Modules run one at a time in the main output
workspace. This mode provides the simplest dependency and resume semantics.

### 8.2 Parallel mode

Parallel mode MAY be enabled with `--parallel` or `HAFLEET_PARALLEL=1`.

The orchestrator MUST:

- create a worktree and branch per pending independent module;
- record path, branch, base commit, and phase in `active_worktrees`;
- run planner, implementer, and reviewer inside that worktree;
- create a reviewer-authored module commit in the worktree;
- cherry-pick module commits into the main workspace in module order;
- copy the module plan into the main `.arc/hafleet/plans/` directory; and
- remove successful worktrees, while retaining failed or conflicted ones for
  diagnosis and resume.

A cherry-pick conflict or worker failure MUST pause the run and preserve enough
checkpoint metadata to resume with `--parallel`.

## 9. Git and checkpoint policy

### 9.1 Checkpoint commits

The reference checkpoint messages are:

```text
init
ROOT: architecture scaffold
<module-id>: implement and review <module-name>
ROOT: final HAFleet integration review
```

Role attribution follows a uniform rule:

```text
name  = HAFleet-{NormalizedRoleTitle}
email = {normalized-role}@hafleet.local
```

The default role mapping is Architect for initialization and architecture,
Reviewer for module checkpoints, and Postflight for final integration. Git
identity is injected into subprocess environments; HAFleet MUST NOT modify the
user's global Git configuration.

Implementations MAY override the name prefix and email domain with
`HAFLEET_GIT_NAME_PREFIX` and `HAFLEET_GIT_EMAIL_DOMAIN`, but MUST retain the
normalization and fallback rules.

### 9.2 Checkpoint file

`.arc/checkpoint.json` MUST contain, at minimum:

- `version`;
- `architecture_completed`;
- `completed` module IDs;
- `last_completed_index`;
- `current_node_id` and `current_phase` when active or paused;
- `paused`;
- `final_review_completed`;
- `parallel_mode` and `max_workers`; and
- `active_worktrees`, `failed_modules`, and `conflicted_modules` as applicable.

When a review loop is active it SHOULD also contain `current_pipeline_node`,
`current_round`, `loop_status`, `last_feedback_message_id`,
`last_feedback_hash`, `review_findings`, `reviewer_write_violation`, and the
last consumed message sequence.

Checkpoint writes SHOULD be atomic. A malformed checkpoint is treated as an
empty/default checkpoint rather than as permission to claim completion.

## 10. Reliability and failure semantics

Each agent turn MUST have a finite timeout. The reference defaults are three
attempts, `1200` seconds per turn, and retry delays of `30,60` seconds.

Only transient failures SHOULD be retried, including timeout, transport,
authentication, rate-limit, overload, and empty-turn failures. Before a retry,
the driver MUST discard the failed `(role, workspace)` thread so the next
attempt starts a fresh agent session.

Non-transient failures MUST propagate to the orchestrator. The process MUST
emit a failed runner state and MUST NOT mark the module completed.

Pause requests are represented by a file (default
`.arc/pause-request`, configurable by `ARCBENCH_PAUSE_REQUEST_PATH`). HAFleet
checks it at architecture, design, implement, review, parallel merge, final
review, and postflight boundaries. A pause exits with status `130`.

## 11. Web postflight contract

For `--type web`, the generated project MUST:

- have `frontend/package.json` with a working `npm run build`;
- have `backend/package.json` with a working `npm run start`;
- read `process.env.PORT` in the backend;
- serve the built frontend from the backend; and
- avoid binding the grading port during generation.

Postflight runs on `HAFLEET_SMOKE_PORT` (default `3100`) and MUST stop every
server it starts. A workspace-scoped port guard MAY terminate only processes
owned by this submission; it MUST NOT kill unrelated listeners.

If rehearsal fails, HAFleet MAY send the exact error to the implementer for up to
`HAFLEET_POSTFLIGHT_REPAIRS` repair attempts. Only a passing rehearsal permits
the final checkpoint and successful completion.

For `cli` and `android` tasks, web-specific checks are skipped unless an
implementation defines an equivalent task-type contract.

## 12. Persistence and observability contract

The following files have stable meanings:

| File | Contract |
| --- | --- |
| `.arc/runner-events.jsonl` | Append-only lifecycle, requirement, signal, and traceability events |
| `.arc/hafleet/messages.jsonl` | Ordered Agent/Pipeline messages, feedback, verdicts, and operation events |
| `.arc/checkpoint.json` | Resume and completion state machine |
| `.arc/traceability/` | Requirement, scenario, test, interface, node-state, and call-edge stores |
| `.arc/hafleet/architecture.md` | Global architecture contract written by Architect |
| `.arc/hafleet/plans/*.md` | One implementation plan per module |
| `.arc/hafleet/codex-home/` | Run-local Codex session state |
| `.arc/hafleet/worktrees/` | Active/failed parallel module worktrees |
| Git history | Durable source checkpoints and role-attributed diffs |

The optional dashboard MUST be read-only with respect to the output workspace.
It MAY run as an integrated local server or as an API-only process, but it MUST
not let browser code read the local filesystem directly.

Dashboard file changes SHOULD be scoped by role and stage:

- planner output: the selected module plan;
- architect commit: the architecture checkpoint;
- implementer/reviewer worktree: current active changes;
- module checkpoint: the reviewed module commit; and
- postflight commit/worktree: final integration changes.

## 13. Configuration

CLI flags take precedence over their corresponding environment defaults.
Implementations SHOULD preserve these reference variables:

| Variable | Default | Meaning |
| --- | ---: | --- |
| `HAFLEET_MAX_ATTEMPTS` | `3` | Maximum attempts per agent turn |
| `HAFLEET_RETRY_DELAYS` | `30,60` | Retry delays in seconds |
| `HAFLEET_TURN_TIMEOUT` | `1200` | Agent turn timeout in seconds |
| `HAFLEET_SMOKE_PORT` | `3100` | Safe web rehearsal port |
| `HAFLEET_POSTFLIGHT_REPAIRS` | `2` | Implementer repairs after failed rehearsal |
| `HAFLEET_NPM_TIMEOUT` | `600` | Per-command npm timeout |
| `HAFLEET_READY_TIMEOUT` | `45` | Backend readiness timeout |
| `HAFLEET_FINAL_REVIEW` | `1` | Enable whole-project reviewer pass |
| `HAFLEET_POSTFLIGHT` | `1` | Enable web delivery rehearsal |
| `HAFLEET_PARALLEL` | `0` | Enable module worktrees |
| `HAFLEET_MAX_WORKERS` | `2` | Parallel worktree concurrency |
| `HAFLEET_DASHBOARD` | `0` | Enable local dashboard |
| `HAFLEET_DASHBOARD_PORT` | `3200` | Dashboard port |
| `HAFLEET_*_MODEL` | empty | Role-specific model fallback to `MODEL` |
| `ARCBENCH_PAUSE_REQUEST_PATH` | `<output>/.arc/pause-request` | Pause request file |
| `ARCBENCH_CHECKPOINT_PATH` | `<output>/.arc/checkpoint.json` | Checkpoint override |
| `HAFLEET_GIT_NAME_PREFIX` | `HAFleet-` | Git author name prefix |
| `HAFLEET_GIT_EMAIL_DOMAIN` | `hafleet.local` | Git author email domain |

The runtime additionally honors ARC-Bench paths such as
`ARCBENCH_OUTPUT_DIR`, `ARCBENCH_PROJECT_DIR`,
`ARCBENCH_RUNNER_EVENTS_PATH`, and `ARCBENCH_TRACEABILITY_DIR`.

## 14. Security and safety posture

The reference Codex sessions use full workspace access for Architect, Planner,
and Implementer, while Reviewer sessions use `Sandbox.read_only`; approvals are
denied by the SDK configuration. This is a trusted-runner posture: the
requirement bundle and agent prompts are inside the execution trust boundary.

Implementations MUST:

- keep the grading port separate from the smoke port;
- scope generated Codex state to the output workspace;
- avoid mutating global Git configuration for role identity;
- keep dashboard APIs read-only;
- bound diff, event, and session payloads before serving them; and
- stop rehearsal processes before exit.

Deployments that run untrusted requirements SHOULD add OS-level sandboxing,
network restrictions, filesystem restrictions, and approval controls. Those
controls are deployment policy and are not supplied by this specification.

## 15. Compatibility and evolution

Implementations MAY add fields to JSON events, checkpoints, and dashboard
responses, but MUST preserve existing fields and meanings. Unknown checkpoint
fields MUST be ignored. New phases MUST define their event, checkpoint, retry,
and resume semantics before being enabled.

The service is finite by default. A future resident supervisor MAY invoke
multiple HAFleet runs, but residency, queueing, and external work acquisition
are outside this specification.

## 16. Verification requirements

The reference implementation SHOULD be checked with:

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -q
python3 -m compileall -q main.py hafleet_arc tests arcbench-agent-runtime/src
git diff --check
```

For dashboard changes, also run:

```bash
node --check hafleet_arc/dashboard/static/app.js
pnpm --dir hafleet_arc/dashboard/frontend build
```

A HAFleet implementation is conformant to this draft when it preserves the
phase, artifact, checkpoint, role, Git, retry, and postflight contracts above,
or explicitly documents an implementation-defined alternative.
