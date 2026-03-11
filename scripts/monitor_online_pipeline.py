#!/usr/bin/env python3
"""Periodically monitor an online pipeline run and restart it on failure."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso8601(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_tail(path: Path, max_lines: int = 120) -> List[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-max_lines:]


def is_pipeline_running(spec_path: Path) -> bool:
    proc = subprocess.run(
        ["pgrep", "-af", "scripts/run_online_pipeline.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        return False

    spec_str = str(spec_path.resolve())
    for line in proc.stdout.splitlines():
        if spec_str in line and "monitor_online_pipeline.py" not in line:
            return True
    return False


def launch_pipeline(workdir: Path, spec_path: Path, launch_log: Path) -> Optional[int]:
    ensure_dir(launch_log.parent)
    with launch_log.open("ab") as handle:
        proc = subprocess.Popen(
            [sys.executable, "scripts/run_online_pipeline.py", "--spec", str(spec_path)],
            cwd=workdir,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return proc.pid


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def acquire_lock(lock_path: Path) -> None:
    ensure_dir(lock_path.parent)
    if lock_path.exists():
        try:
            old_pid = int(lock_path.read_text(encoding="utf-8").strip())
            os.kill(old_pid, 0)
            raise RuntimeError(f"monitor already running with pid {old_pid}")
        except ProcessLookupError:
            pass
        except ValueError:
            pass
    lock_path.write_text(str(os.getpid()), encoding="utf-8")


def release_lock(lock_path: Path) -> None:
    if lock_path.exists():
        lock_path.unlink()


def append_log(log_path: Path, message: str) -> None:
    ensure_dir(log_path.parent)
    timestamp = utc_now().isoformat()
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def collect_diagnostics(
    run_state_path: Path,
    spec_path: Path,
    monitor_log: Path,
) -> Path:
    run_dir = run_state_path.parent
    diagnostics_dir = run_dir / "diagnostics"
    ensure_dir(diagnostics_dir)

    state = read_json(run_state_path)
    current_step = state.get("current_step")
    artifacts = state.get("artifacts", {})
    step_status = state.get("step_status", {})
    last_error = state.get("last_error")

    summary_path = None
    log_path = None
    if current_step:
        for idx, (step_name, step_info) in enumerate(step_status.items(), start=1):
            if step_name == current_step:
                summary_path = artifacts.get(f"step_{idx:02d}_summary")
                log_path = artifacts.get(f"step_{idx:02d}_log")
                break

    bundle = {
        "captured_at": utc_now().isoformat(),
        "spec_path": str(spec_path),
        "run_state_path": str(run_state_path),
        "status": state.get("status"),
        "current_step": current_step,
        "last_error": last_error,
        "resolved_spec_path": artifacts.get("resolved_spec"),
        "parallel_plan_path": artifacts.get("parallel_plan"),
        "step_summary_path": summary_path,
        "step_log_path": log_path,
        "monitor_log_tail": read_tail(monitor_log, 80),
        "step_log_tail": read_tail(Path(log_path), 120) if log_path else [],
        "step_summary": read_json(Path(summary_path)) if summary_path and Path(summary_path).exists() else None,
        "run_state": state,
    }

    bundle_path = diagnostics_dir / f"failure_{utc_now().strftime('%Y%m%dT%H%M%SZ')}.json"
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return bundle_path


def run_diagnostic_hook(command: str, bundle_path: Path, monitor_log: Path) -> bool:
    env = os.environ.copy()
    env["SPECFORGE_DIAGNOSTIC_BUNDLE"] = str(bundle_path)
    proc = subprocess.run(
        shlex.split(command) + [str(bundle_path)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    append_log(
        monitor_log,
        f"diagnostic hook exit_code={proc.returncode} stdout={proc.stdout.strip()[:500]} stderr={proc.stderr.strip()[:500]}",
    )
    return proc.returncode == 0


def run_codex_diagnosis(
    workdir: Path,
    bundle_path: Path,
    monitor_log: Path,
    schema_path: Path,
) -> bool:
    output_path = bundle_path.with_suffix(".codex_diagnosis.json")
    prompt = f"""
You are diagnosing a failed SpecForge online training run.

Read the failure bundle at:
{bundle_path}

Use the bundle and any referenced local files to decide whether an automatic restart is safe.
Be conservative. Only allow restart if the failure is clearly recoverable with the same spec and resume flow.

Return JSON matching the provided schema with:
- restart_allowed: boolean
- reason: short machine-friendly label
- summary: short diagnosis
- next_action: short concrete next step
""".strip()

    proc = subprocess.run(
        [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--cd",
            str(workdir),
            "--sandbox",
            "read-only",
            "--output-schema",
            str(schema_path),
            "-o",
            str(output_path),
            prompt,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    append_log(
        monitor_log,
        f"codex diagnosis exit_code={proc.returncode} stdout={proc.stdout.strip()[:500]} stderr={proc.stderr.strip()[:500]}",
    )
    if proc.returncode != 0 or not output_path.exists():
        return False

    try:
        decision = json.loads(output_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        append_log(monitor_log, f"invalid codex diagnosis json: {output_path}")
        return False

    append_log(
        monitor_log,
        f"codex diagnosis decision restart_allowed={decision.get('restart_allowed')} reason={decision.get('reason')} next_action={decision.get('next_action')}",
    )
    return bool(decision.get("restart_allowed"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Monitor an online pipeline run and restart on failure."
    )
    parser.add_argument("--spec", required=True, help="Pipeline spec path")
    parser.add_argument("--run-state", required=True, help="run_state.json path")
    parser.add_argument(
        "--interval-sec", type=int, default=1200, help="Polling interval in seconds"
    )
    parser.add_argument(
        "--stale-sec",
        type=int,
        default=3600,
        help="Restart if state is not updated for this many seconds and no pipeline process exists",
    )
    parser.add_argument(
        "--launch-log",
        default=None,
        help="Where to append stdout/stderr from auto-restarted pipeline launches",
    )
    parser.add_argument(
        "--monitor-log", default=None, help="Where to write monitor activity log"
    )
    parser.add_argument(
        "--max-restarts",
        type=int,
        default=20,
        help="Maximum number of auto restarts before monitor exits",
    )
    parser.add_argument(
        "--diagnostic-mode",
        choices=["codex", "command"],
        default="codex",
        help="How to diagnose failures before restart.",
    )
    parser.add_argument(
        "--diagnostic-cmd",
        default=None,
        help="Required when --diagnostic-mode=command. Bundle path is appended as the final argument.",
    )
    args = parser.parse_args()

    workdir = Path.cwd()
    spec_path = Path(args.spec).resolve()
    run_state_path = Path(args.run_state).resolve()
    launch_log = (
        Path(args.launch_log).resolve()
        if args.launch_log
        else run_state_path.parent / "logs" / "monitor_restarts.log"
    )
    monitor_log = (
        Path(args.monitor_log).resolve()
        if args.monitor_log
        else run_state_path.parent / "logs" / "monitor.log"
    )
    lock_path = run_state_path.parent / ".monitor.lock"
    schema_path = workdir / "configs" / "codex_failure_diagnosis.schema.json"

    acquire_lock(lock_path)

    restarts = 0

    def _cleanup(*_: Any) -> None:
        append_log(monitor_log, "received stop signal, exiting monitor")
        release_lock(lock_path)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    append_log(
        monitor_log,
        f"monitor started: interval={args.interval_sec}s stale={args.stale_sec}s spec={spec_path}",
    )

    try:
        while True:
            if not run_state_path.exists():
                append_log(monitor_log, "run_state.json missing; waiting for next interval")
                time.sleep(args.interval_sec)
                continue

            state = read_json(run_state_path)
            status = state.get("status", "unknown")
            updated_at = state.get("updated_at")
            current_step = state.get("current_step")

            append_log(
                monitor_log,
                f"status={status} current_step={current_step} updated_at={updated_at}",
            )

            if status == "completed":
                append_log(monitor_log, "run completed; monitor exiting")
                return 0

            if status == "failed":
                bundle_path = collect_diagnostics(run_state_path, spec_path, monitor_log)
                append_log(monitor_log, f"captured failure bundle: {bundle_path}")
                if args.diagnostic_mode == "codex":
                    ok = run_codex_diagnosis(
                        workdir=workdir,
                        bundle_path=bundle_path,
                        monitor_log=monitor_log,
                        schema_path=schema_path,
                    )
                else:
                    if not args.diagnostic_cmd:
                        append_log(
                            monitor_log,
                            "diagnostic command is required for restart; automatic restart disabled",
                        )
                        return 1
                    ok = run_diagnostic_hook(args.diagnostic_cmd, bundle_path, monitor_log)
                if not ok:
                    append_log(
                        monitor_log,
                        "diagnosis did not approve restart; automatic restart disabled",
                    )
                    return 1
                if restarts >= args.max_restarts:
                    append_log(monitor_log, "max restarts reached after failed status; exiting")
                    return 1
                if is_pipeline_running(spec_path):
                    append_log(monitor_log, "status=failed but pipeline process still exists; skip restart")
                else:
                    pid = launch_pipeline(workdir, spec_path, launch_log)
                    restarts += 1
                    append_log(
                        monitor_log,
                        f"restarted pipeline after failed status; pid={pid} restart_count={restarts}",
                    )
            elif updated_at:
                age_sec = (utc_now() - parse_iso8601(str(updated_at))).total_seconds()
                if age_sec > args.stale_sec and not is_pipeline_running(spec_path):
                    if restarts >= args.max_restarts:
                        append_log(
                            monitor_log,
                            "max restarts reached after stale status; exiting",
                        )
                        return 1
                    pid = launch_pipeline(workdir, spec_path, launch_log)
                    restarts += 1
                    append_log(
                        monitor_log,
                        f"restarted stale pipeline; pid={pid} state_age_sec={int(age_sec)} restart_count={restarts}",
                    )

            time.sleep(args.interval_sec)
    finally:
        release_lock(lock_path)


if __name__ == "__main__":
    raise SystemExit(main())
