from __future__ import annotations

import argparse
import os
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from hafleet_arc import (
    FleetOrchestrator,
    PauseRequested,
    load_requirement_tree,
    plan_modules,
)
from hafleet_arc.checkpoint import CheckpointStore
from hafleet_arc.codex_driver import CodexFleet
from hafleet_arc.orchestrator import copy_template_contents
from hafleet_arc.postflight import WorkspacePortGuard
from hafleet_arc.log import log


def _console(message: str) -> None:
    """Print a flushed, human-readable lifecycle message for local runs."""

    log(f"[hafleet-arc] {message}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Implement an ARC-Bench task with HAFleet ARC.")
    parser.add_argument(
        "requirement_path",
        nargs="?",
        default=os.environ.get("ARCBENCH_TASK_DIR", "requirements"),
        help="Directory containing requirements.yaml.",
    )
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("ARCBENCH_OUTPUT_DIR", "."),
        help="Target directory for the generated project.",
    )
    parser.add_argument(
        "--type",
        dest="task_type",
        default=os.environ.get("ARCBENCH_TASK_TYPE", "web"),
        choices=("web", "cli", "android"),
        help="ARC-Bench task type.",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=int(os.environ.get("ARCBENCH_WEB_PORT", os.environ.get("ARC_WEB_PORT", "3000"))),
        help="ARC-Bench grading port. HAFleet never binds this port during generation.",
    )
    parser.add_argument(
        "--smoke-port",
        type=int,
        default=int(os.environ.get("HAFLEET_SMOKE_PORT", "3100")),
        help="Safe port used for generation-time startup checks.",
    )
    parser.add_argument(
        "--parallel",
        action="store_true",
        default=os.environ.get("HAFLEET_PARALLEL", "0").strip().lower() in {"1", "true", "yes"},
        help="Run independent ROOT modules in separate git worktrees.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=int(os.environ.get("HAFLEET_MAX_WORKERS", "2")),
        help="Maximum concurrent module worktrees in parallel mode.",
    )
    return parser.parse_args(argv)


def _runtime(output_dir: Path) -> Any:
    # The submission vendors the ARC-Bench runtime. The runner normally installs
    # it from requirements.txt; direct source-tree runs use this local fallback.
    local_sdk_src = Path(__file__).resolve().parent / "arcbench-agent-runtime" / "src"
    if local_sdk_src.is_dir() and str(local_sdk_src) not in sys.path:
        sys.path.insert(0, str(local_sdk_src))
    try:
        from arcbench_agent_runtime import AgentRuntime
    except ImportError as exc:
        raise RuntimeError("ARC-Bench runtime SDK is unavailable; install requirements.txt first.") from exc
    return AgentRuntime.from_env(project_dir=str(output_dir))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    requirements_dir = Path(args.requirement_path).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    started_at = time.monotonic()
    _console(
        f"starting: requirements={requirements_dir} output={output_dir} "
        f"type={args.task_type} grading_port={args.web_port} smoke_port={args.smoke_port}"
    )
    if not requirements_dir.is_dir():
        raise FileNotFoundError(f"Requirement directory not found: {requirements_dir}")
    if args.web_port <= 0 or args.smoke_port <= 0:
        raise ValueError("web and smoke ports must be positive")
    if args.max_workers < 1:
        raise ValueError("max-workers must be at least 1")
    if args.task_type == "web" and args.web_port == args.smoke_port:
        raise ValueError("smoke port must differ from the ARC-Bench grading port")

    agent_root = Path(__file__).resolve().parent
    output_dir.mkdir(parents=True, exist_ok=True)
    copy_template_contents(agent_root / "template" / args.task_type, output_dir)
    _console("template copied (existing files preserved)")
    requirement_tree = load_requirement_tree(requirements_dir)
    modules = plan_modules(requirement_tree)
    _console(f"loaded requirement tree ROOT with {len(modules)} top-level module(s)")
    _console("module order: " + ", ".join(f"{item.node_id} ({item.name})" for item in modules))
    runtime = _runtime(output_dir)
    runtime.traceability.init_store()
    runtime.traceability.store_requirement_tree(requirement_tree)
    runtime.git.ensure_repo()
    runtime.events.mark_run_started(f"HAFleet ARC started with {len(modules)} modules")
    _console(
        "runtime initialized; entering architect/planner/implementer/reviewer pipeline "
        f"(parallel={args.parallel}, max_workers={args.max_workers})"
    )

    skills_dir = agent_root / "skills"
    try:
        port_guard = (
            WorkspacePortGuard(output_dir, args.web_port, args.smoke_port)
            if args.task_type == "web"
            else nullcontext()
        )
        with port_guard, CodexFleet(
            output_dir,
            skills_dir if skills_dir.is_dir() else None,
            task_type=args.task_type,
            grading_port=args.web_port,
            smoke_port=args.smoke_port,
        ) as fleet:
            FleetOrchestrator(
                driver=fleet,
                runtime=runtime,
                checkpoint=CheckpointStore.from_env(output_dir),
                requirements_dir=requirements_dir,
                output_dir=output_dir,
                task_type=args.task_type,
                smoke_port=args.smoke_port,
                requirement_tree=requirement_tree,
                parallel=args.parallel,
                max_workers=args.max_workers,
            ).run(modules)
    except PauseRequested as exc:
        runtime.events.mark_run_paused(str(exc))
        _console(f"paused: {exc}")
        return 130
    except Exception as exc:
        runtime.events.mark_run_failed(str(exc))
        _console(f"FAILED after {time.monotonic() - started_at:.1f}s: {exc}")
        raise

    runtime.events.mark_run_completed("HAFleet ARC completed")
    _console(f"completed successfully in {time.monotonic() - started_at:.1f}s")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
