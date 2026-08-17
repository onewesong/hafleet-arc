# HAFleet ARC

HAFleet ARC is a finite-lifecycle multi-role coding agent for ARC-Bench. It keeps
HAFleet's planner, implementer, reviewer, task-state, and checkpoint concepts but
does not start the HAFleet backend, tmux, Matrix, or dashboard services.

## Entrypoint

ARC-Bench runs the submission as:

```bash
python3 main.py /path/to/requirements --output-dir /path/to/output --type web
```

The runner supplies `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `MODEL`, and all
`ARCBENCH_*` runtime paths. The requirement bundle must contain a ROOT
`requirements.yaml`.

Codex local state is isolated under `.arc/hafleet/codex-home` because evaluation
hosts may expose a read-only home directory.

## Fleet workflow

For each direct ROOT child, in dependency-aware order:

1. `planner` inspects the project and writes a module plan under `.arc/hafleet/`.
2. `implementer` implements the complete subtree.
3. `reviewer` runs checks and repairs defects.
4. ARC-Bench traceability, runtime events, git checkpoint, and resume state are updated.

After all modules, the reviewer performs one integration repair pass. Set
`HAFLEET_FINAL_REVIEW=0` to disable that pass for cheaper local experiments.

## Local checks

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q main.py hafleet_arc tests
```

For submission, keep `main.py`, `hafleet_arc/`, `requirements.txt`, and optional
`skills/` at the ZIP root. ARC-Bench injects its runtime SDK through `PYTHONPATH`.
