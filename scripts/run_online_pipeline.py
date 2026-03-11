#!/usr/bin/env python3
"""SpecForge online training orchestrator.

This script orchestrates online training in four phases:
1) Prepare data
2) Regen server preflight/bootstrap (optional)
3) Regenerate data (optional)
4) Train draft model (EAGLE3 or DFlash)

It also provides:
- Parallel config planning (local/ssh GPU probing)
- State persistence (`run_state.json`)
- Step summaries (`step_xx_summary.json`)
- Event notifications via hook channels (webhook/smtp/gmail)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_json(path: Path, data: Dict[str, Any]) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_spec(path: Path) -> Dict[str, Any]:
    suffix = path.suffix.lower()
    text = read_text(path)

    if suffix == ".json":
        return json.loads(text)

    if suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "YAML spec requires PyYAML. Install with `pip install pyyaml` "
                "or use a JSON spec file instead."
            ) from exc
        return yaml.safe_load(text)

    raise ValueError(f"Unsupported spec format: {path}")


def load_notify_api(repo_root: Path):
    """Load notification API with fallback to direct module loading.

    Importing `specforge.notify` can fail in partially configured envs because
    `specforge/__init__.py` imports heavy modules. Fallback avoids that.
    """
    try:
        from specforge.notify import NotificationManager, create_hooks  # type: ignore

        return NotificationManager, create_hooks
    except Exception:
        notify_file = repo_root / "specforge" / "notify.py"
        spec = spec_from_file_location("specforge_notify_module", notify_file)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Failed to load notify module from {notify_file}")
        mod = module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.NotificationManager, mod.create_hooks


@dataclass
class CommandResult:
    returncode: int
    duration_sec: float
    command: str
    log_path: Optional[str] = None


class PipelineError(RuntimeError):
    pass


class StateStore:
    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.state: Dict[str, Any] = {
            "run_id": run_id,
            "status": "initialized",
            "current_step": None,
            "step_status": {},
            "artifacts": {},
            "metrics": {},
            "history": [],
            "updated_at": utc_now_iso(),
            "started_at": utc_now_iso(),
            "last_error": None,
        }
        self.flush()

    def flush(self) -> None:
        self.state["updated_at"] = utc_now_iso()
        write_json(self.path, self.state)

    def append_history(self, event: str, data: Optional[Dict[str, Any]] = None) -> None:
        self.state["history"].append(
            {
                "time": utc_now_iso(),
                "event": event,
                "data": data or {},
            }
        )
        self.flush()

    def set_status(self, status: str) -> None:
        self.state["status"] = status
        self.flush()

    def set_step(self, step: str, status: str) -> None:
        self.state["current_step"] = step
        self.state["step_status"][step] = {
            "status": status,
            "time": utc_now_iso(),
        }
        self.flush()

    def set_metric(self, key: str, value: Any) -> None:
        self.state["metrics"][key] = value
        self.flush()

    def set_artifact(self, key: str, value: Any) -> None:
        self.state["artifacts"][key] = value
        self.flush()

    def set_error(self, error: str) -> None:
        self.state["last_error"] = error
        self.flush()


class PipelineNotifier:
    def __init__(
        self,
        manager: Any,
        run_id: str,
        cooldown_sec: int = 600,
        enabled: bool = True,
    ) -> None:
        self.manager = manager
        self.run_id = run_id
        self.cooldown_sec = cooldown_sec
        self.enabled = enabled
        self.last_sent: Dict[str, float] = {}

    def send(
        self,
        event: str,
        title: str,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        force: bool = False,
        dedupe_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.enabled or self.manager is None:
            return {
                "event": event,
                "sent": 0,
                "skipped": 0,
                "errors": [],
                "disabled": True,
            }

        key = dedupe_key or event
        now = time.time()
        if not force and key in self.last_sent:
            if now - self.last_sent[key] < self.cooldown_sec:
                return {
                    "event": event,
                    "sent": 0,
                    "skipped": 1,
                    "errors": [],
                    "reason": "cooldown",
                }

        payload_context = dict(context or {})
        payload_context.setdefault("run_id", self.run_id)

        result = self.manager.notify(
            event=event,
            title=title,
            message=message,
            context=payload_context,
            fail_fast=False,
        )
        self.last_sent[key] = now
        return result


class CommandRunner:
    def __init__(self, machine_cfg: Mapping[str, Any], default_cwd: Path):
        self.mode = str(machine_cfg.get("mode", "local")).lower()
        self.default_cwd = str(machine_cfg.get("workspace", default_cwd))
        self.extra_env = dict(machine_cfg.get("env", {}))

        ssh_cfg = machine_cfg.get("ssh", {})
        self.ssh_host = ssh_cfg.get("host")
        self.ssh_user = ssh_cfg.get("user")
        self.ssh_port = int(ssh_cfg.get("port", 22))
        self.ssh_identity_file = ssh_cfg.get("identity_file")
        self.ssh_strict_host_key_checking = bool(
            ssh_cfg.get("strict_host_key_checking", False)
        )

        if self.mode not in {"local", "ssh"}:
            raise ValueError(f"Unsupported machine mode: {self.mode}")

        if self.mode == "ssh" and (not self.ssh_host or not self.ssh_user):
            raise ValueError(
                "SSH mode requires `machine.ssh.host` and `machine.ssh.user`."
            )

    def _join_cmd(self, command: Sequence[str]) -> str:
        return " ".join(shlex.quote(str(x)) for x in command)

    def _build_ssh_command(
        self, command: Sequence[str], cwd: Optional[str]
    ) -> List[str]:
        remote_cmd_parts: List[str] = []

        working_dir = cwd or self.default_cwd
        if working_dir:
            remote_cmd_parts.append(f"cd {shlex.quote(working_dir)}")

        if self.extra_env:
            env_assign = " ".join(
                f"{k}={shlex.quote(str(v))}" for k, v in self.extra_env.items()
            )
            remote_cmd_parts.append(env_assign)

        remote_cmd_parts.append(self._join_cmd(command))
        remote_cmd = " && ".join(remote_cmd_parts)

        ssh_cmd = ["ssh", "-p", str(self.ssh_port)]
        if self.ssh_identity_file:
            ssh_cmd.extend(["-i", self.ssh_identity_file])
        if not self.ssh_strict_host_key_checking:
            ssh_cmd.extend(
                [
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "UserKnownHostsFile=/dev/null",
                ]
            )

        ssh_cmd.append(f"{self.ssh_user}@{self.ssh_host}")
        ssh_cmd.append(remote_cmd)
        return ssh_cmd

    def prepare(
        self, command: Sequence[str], cwd: Optional[str] = None
    ) -> Tuple[List[str], Optional[str], Dict[str, str]]:
        if self.mode == "local":
            env = os.environ.copy()
            env.update(self.extra_env)
            return list(command), cwd or self.default_cwd, env

        return self._build_ssh_command(command, cwd=cwd), None, os.environ.copy()

    def run_capture(
        self,
        command: Sequence[str],
        cwd: Optional[str] = None,
        timeout_sec: int = 30,
    ) -> Tuple[int, str, str]:
        full_cmd, run_cwd, env = self.prepare(command, cwd=cwd)
        proc = subprocess.run(
            full_cmd,
            cwd=run_cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def run_stream(
        self,
        command: Sequence[str],
        log_path: Path,
        cwd: Optional[str] = None,
        on_line: Optional[Callable[[str], None]] = None,
        dry_run: bool = False,
    ) -> CommandResult:
        full_cmd, run_cwd, env = self.prepare(command, cwd=cwd)
        cmd_str = self._join_cmd(full_cmd)

        if dry_run:
            ensure_dir(log_path.parent)
            log_path.write_text(
                f"[DRY RUN] {cmd_str}\n",
                encoding="utf-8",
            )
            return CommandResult(
                returncode=0,
                duration_sec=0.0,
                command=cmd_str,
                log_path=str(log_path),
            )

        ensure_dir(log_path.parent)
        start = time.time()
        with log_path.open("w", encoding="utf-8") as logf:
            logf.write(f"$ {cmd_str}\n")
            logf.flush()

            proc = subprocess.Popen(
                full_cmd,
                cwd=run_cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            assert proc.stdout is not None
            for line in proc.stdout:
                logf.write(line)
                logf.flush()
                if on_line is not None:
                    on_line(line.rstrip("\n"))

            returncode = proc.wait()

        return CommandResult(
            returncode=returncode,
            duration_sec=time.time() - start,
            command=cmd_str,
            log_path=str(log_path),
        )


class ProgressReporter:
    TQDM_PERCENT = re.compile(r"(?P<percent>\d{1,3})%\|")
    TQDM_FRACTION = re.compile(r"(?P<current>\d+)\s*/\s*(?P<total>\d+)")
    EVAL_STEP = re.compile(r"Eval - Step (?P<step>\d+)")
    PLOSS = re.compile(r"pLoss:\s*(?P<loss>[-+0-9.eE]+)")
    ACC = re.compile(r"Acc:\s*(?P<acc>[-+0-9.eE]+)")

    def __init__(
        self,
        notifier: PipelineNotifier,
        state: StateStore,
        phase: str,
        run_id: str,
        total_steps_hint: Optional[int] = None,
        percent_step: int = 5,
        min_interval_sec: int = 600,
    ):
        self.notifier = notifier
        self.state = state
        self.phase = phase
        self.run_id = run_id
        self.total_steps_hint = total_steps_hint
        self.percent_step = percent_step
        self.min_interval_sec = min_interval_sec

        self.started_at = time.time()
        self.last_sent_at = 0.0
        self.last_sent_percent = -1.0

    def _should_send(self, percent: Optional[float]) -> bool:
        now = time.time()
        if now - self.last_sent_at >= self.min_interval_sec:
            return True
        if percent is None:
            return False
        if self.last_sent_percent < 0:
            return True
        return percent - self.last_sent_percent >= self.percent_step

    def _format_message(self, snapshot: Dict[str, Any]) -> str:
        lines = [f"phase: {self.phase}"]
        if "current" in snapshot and "total" in snapshot:
            lines.append(
                f"progress: {snapshot['current']}/{snapshot['total']} ({snapshot.get('percent', 'n/a')}%)"
            )
        elif "percent" in snapshot:
            lines.append(f"progress: {snapshot['percent']}%")

        if "elapsed_sec" in snapshot:
            lines.append(f"elapsed_sec: {snapshot['elapsed_sec']}")
        if "eta_sec" in snapshot:
            lines.append(f"eta_sec: {snapshot['eta_sec']}")
        if "loss" in snapshot:
            lines.append(f"loss: {snapshot['loss']}")
        if "acc" in snapshot:
            lines.append(f"acc: {snapshot['acc']}")
        return "\n".join(lines)

    def parse_line(self, line: str) -> None:
        if not line:
            return

        snapshot: Dict[str, Any] = {}

        m_pct = self.TQDM_PERCENT.search(line)
        if m_pct:
            snapshot["percent"] = float(m_pct.group("percent"))

        m_frac = self.TQDM_FRACTION.search(line)
        if m_frac:
            snapshot["current"] = int(m_frac.group("current"))
            snapshot["total"] = int(m_frac.group("total"))
            if "percent" not in snapshot and snapshot["total"] > 0:
                snapshot["percent"] = round(
                    snapshot["current"] * 100.0 / snapshot["total"], 2
                )

        m_step = self.EVAL_STEP.search(line)
        if m_step:
            step = int(m_step.group("step"))
            snapshot["current"] = step
            if self.total_steps_hint and self.total_steps_hint > 0:
                snapshot["total"] = self.total_steps_hint
                snapshot["percent"] = round(step * 100.0 / self.total_steps_hint, 2)

        m_loss = self.PLOSS.search(line)
        if m_loss:
            snapshot["loss"] = float(m_loss.group("loss"))

        m_acc = self.ACC.search(line)
        if m_acc:
            snapshot["acc"] = float(m_acc.group("acc"))

        if not snapshot:
            return

        elapsed = int(time.time() - self.started_at)
        snapshot["elapsed_sec"] = elapsed
        percent = snapshot.get("percent")
        if isinstance(percent, (int, float)) and percent > 0:
            eta = int(elapsed * (100.0 - float(percent)) / float(percent))
            snapshot["eta_sec"] = max(eta, 0)

        self.state.set_metric(f"progress.{self.phase}", snapshot)

        if self._should_send(percent):
            self.last_sent_at = time.time()
            if percent is not None:
                self.last_sent_percent = float(percent)
            self.notifier.send(
                event="STEP_PROGRESS",
                title=f"{self.phase} progress",
                message=self._format_message(snapshot),
                context={
                    "phase": self.phase,
                    "snapshot": snapshot,
                    "run_id": self.run_id,
                },
                dedupe_key=f"STEP_PROGRESS:{self.phase}",
            )


def parse_model_size_b(model_path: str) -> Optional[float]:
    match = re.search(r"(\d+(?:\.\d+)?)\s*[bB]", model_path)
    if match:
        return float(match.group(1))
    return None


def probe_gpu_info(runner: CommandRunner) -> List[Dict[str, Any]]:
    query_cmd = [
        "nvidia-smi",
        "--query-gpu=name,memory.total",
        "--format=csv,noheader,nounits",
    ]
    try:
        rc, out, _ = runner.run_capture(query_cmd, timeout_sec=20)
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return []
    if rc != 0:
        return []

    gpus: List[Dict[str, Any]] = []
    for line in out.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 2:
            continue
        name = parts[0]
        try:
            memory_total = int(parts[1])
        except ValueError:
            continue
        gpus.append({"name": name, "memory_total_mb": memory_total})
    return gpus


def choose_parallel_plan(
    spec: Mapping[str, Any], gpus: List[Dict[str, Any]]
) -> Dict[str, Any]:
    training = spec.get("training", {})
    parallel = spec.get("parallel", {})
    model_path = str(spec.get("models", {}).get("target_model_path", ""))

    requested_gpus = parallel.get("num_gpus", training.get("num_gpus"))
    if requested_gpus is not None:
        requested_gpus = int(requested_gpus)

    available = len(gpus)
    if requested_gpus is None:
        selected_gpus = available if available > 0 else 1
    else:
        if available > 0:
            selected_gpus = max(1, min(available, requested_gpus))
        else:
            selected_gpus = max(1, requested_gpus)

    model_size_b = parse_model_size_b(model_path)
    user_tp_size = parallel.get("tp_size", training.get("tp_size"))

    rationale: List[str] = []
    if user_tp_size is not None:
        tp_size = int(user_tp_size)
        rationale.append("Using user-specified tp_size.")
    else:
        min_mem_mb = min([g["memory_total_mb"] for g in gpus], default=0)

        if model_size_b is None:
            tp_size = 1 if selected_gpus == 1 else min(selected_gpus, 2)
            rationale.append(
                "Model size could not be inferred from path; conservative tp_size heuristic used."
            )
        elif model_size_b <= 8:
            tp_size = 1 if min_mem_mb >= 24000 else min(selected_gpus, 2)
            rationale.append(
                "<=8B dense model: prefer tp_size=1 unless memory is constrained."
            )
        elif model_size_b <= 16:
            tp_size = min(selected_gpus, 2)
            rationale.append("8B-16B model: prefer tp_size up to 2.")
        elif model_size_b <= 40:
            tp_size = min(selected_gpus, 4)
            rationale.append("16B-40B model: prefer tp_size up to 4.")
        else:
            tp_size = min(selected_gpus, 8)
            rationale.append(">40B model: prefer higher tensor parallelism up to 8.")

    tp_size = max(1, min(tp_size, selected_gpus))
    dp_size = max(1, selected_gpus // tp_size)

    batch_size = int(training.get("batch_size", 1))
    if batch_size < 1:
        batch_size = 1

    plan = {
        "available_gpus": available,
        "selected_gpus": selected_gpus,
        "nproc_per_node": selected_gpus,
        "tp_size": tp_size,
        "dp_size": dp_size,
        "batch_size": batch_size,
        "target_model_backend": str(
            training.get(
                "target_model_backend",
                spec.get("models", {}).get("target_model_backend", "sglang"),
            )
        ),
        "model_size_b": model_size_b,
        "rationale": rationale,
        "gpu_info": gpus,
    }

    return plan


def build_prepare_data_command(
    spec: Mapping[str, Any], python_bin: str
) -> Tuple[List[str], str]:
    data_cfg = spec.get("data", {})
    source_cfg = data_cfg.get("source", {})

    source_type = str(source_cfg.get("type", "built_in"))
    if source_type == "path":
        path = str(source_cfg.get("train_data_path", ""))
        if not path:
            raise ValueError("`data.source.train_data_path` is required when type=path")
        return [], path

    dataset = source_cfg.get("dataset")
    if not dataset:
        raise ValueError("`data.source.dataset` is required for built_in source")

    output_path = str(data_cfg.get("prepared_output_path", "./cache/dataset"))

    cmd = [python_bin, "scripts/prepare_data.py", "--dataset", str(dataset)]
    if output_path:
        cmd.extend(["--output-path", output_path])
    if data_cfg.get("split_eval", False):
        cmd.append("--split-eval")

    train_path = str(Path(output_path) / f"{dataset}_train.jsonl")
    return cmd, train_path


def build_regen_command(
    spec: Mapping[str, Any],
    python_bin: str,
    input_path: str,
    output_path: str,
    server_addresses: Optional[Sequence[str]] = None,
) -> List[str]:
    regen = spec.get("data", {}).get("regen", {})
    if not regen.get("enabled", False):
        return []

    model = regen.get("model") or spec.get("models", {}).get("target_model_path")
    if not model:
        raise ValueError(
            "regen requires `data.regen.model` or models.target_model_path"
        )

    configured_server_address = regen.get("server_address")
    if server_addresses is None:
        server_address = configured_server_address
    else:
        server_address = list(server_addresses)
    if not server_address:
        raise ValueError("regen requires at least one server address")

    cmd = [
        python_bin,
        "scripts/regenerate_train_data.py",
        "--model",
        str(model),
        "--concurrency",
        str(regen.get("concurrency", 64)),
        "--max-tokens",
        str(regen.get("max_tokens", 4096)),
        "--temperature",
        str(regen.get("temperature", 0.7)),
        "--input-file-path",
        input_path,
        "--output-file-path",
        output_path,
        "--server-address",
    ]

    if isinstance(server_address, list):
        cmd.extend([str(x) for x in server_address])
    else:
        cmd.append(str(server_address))

    if regen.get("is_reasoning_model", False):
        cmd.append("--is-reasoning-model")
    if regen.get("is_gpt_oss", False):
        cmd.append("--is-gpt-oss")
    if regen.get("resume", False):
        cmd.append("--resume")
    if regen.get("num_samples") is not None:
        cmd.extend(["--num-samples", str(regen["num_samples"])])

    return cmd


def normalize_server_addresses(raw: Any) -> List[str]:
    if raw is None:
        return []

    values: List[Any]
    if isinstance(raw, list):
        values = raw
    else:
        values = [raw]

    normalized: List[str] = []
    seen: set = set()
    for value in values:
        addr = str(value).strip()
        if not addr:
            continue

        addr = re.sub(r"^https?://", "", addr)
        if "/" in addr:
            raise ValueError(
                f"Invalid server address `{addr}`. Expected host:port without path."
            )
        if ":" not in addr:
            raise ValueError(
                f"Invalid server address `{addr}`. Expected format host:port."
            )

        host, port = addr.rsplit(":", 1)
        if not host or not port.isdigit():
            raise ValueError(
                f"Invalid server address `{addr}`. Expected format host:port."
            )

        if addr in seen:
            continue
        seen.add(addr)
        normalized.append(addr)

    return normalized


def sanitize_filename_fragment(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", text)


def probe_regen_server(
    runner: CommandRunner,
    python_bin: str,
    address: str,
    timeout_sec: int,
) -> Tuple[bool, str]:
    probe_code = (
        "import sys\n"
        "from urllib.request import urlopen\n"
        "addr = sys.argv[1]\n"
        "timeout = float(sys.argv[2])\n"
        "url = f'http://{addr}/v1/models'\n"
        "try:\n"
        "    with urlopen(url, timeout=timeout) as resp:\n"
        "        status = getattr(resp, 'status', 200)\n"
        "        print(f'status={status}')\n"
        "        raise SystemExit(0 if 200 <= status < 300 else 1)\n"
        "except Exception as exc:\n"
        "    print(str(exc))\n"
        "    raise SystemExit(1)\n"
    )
    rc, out, err = runner.run_capture(
        [python_bin, "-c", probe_code, address, str(timeout_sec)],
        timeout_sec=max(5, timeout_sec + 3),
    )
    detail = (out or err or "").strip()
    if not detail:
        detail = f"exit_code={rc}"
    return rc == 0, detail


def launch_regen_server(
    runner: CommandRunner,
    launch_cmd: str,
    log_path: Path,
    timeout_sec: int = 20,
) -> Tuple[Optional[str], str, str]:
    if runner.mode == "ssh":
        launch_log_path = f"/tmp/{log_path.name}"
    else:
        ensure_dir(log_path.parent)
        launch_log_path = str(log_path)

    shell_cmd = f"nohup {launch_cmd} > {shlex.quote(launch_log_path)} 2>&1 < /dev/null & echo $!"
    rc, out, err = runner.run_capture(["sh", "-lc", shell_cmd], timeout_sec=timeout_sec)
    detail = (out or err or "").strip()
    if rc != 0:
        return None, detail or f"exit_code={rc}", launch_log_path

    pid = ""
    if out.strip():
        pid = out.strip().splitlines()[-1]
    return (pid or None), detail or "launched", launch_log_path


def run_regen_server_preflight_step(
    step_index: int,
    spec: Mapping[str, Any],
    runner: CommandRunner,
    state: StateStore,
    notifier: PipelineNotifier,
    logs_dir: Path,
    summaries_dir: Path,
    run_id: str,
    dry_run: bool,
    python_bin: str,
) -> List[str]:
    step_name = "REGEN_SERVER_PREFLIGHT"
    started_at = time.time()
    log_path = logs_dir / f"step_{step_index:02d}_{step_name.lower()}.log"
    summary_path = (
        summaries_dir / f"step_{step_index:02d}_{step_name.lower()}_summary.json"
    )
    log_lines: List[str] = []

    regen_cfg = spec.get("data", {}).get("regen", {})
    requested_addresses = normalize_server_addresses(regen_cfg.get("server_address"))
    if not requested_addresses:
        raise ValueError(
            "regen requires `data.regen.server_address` with at least one host:port"
        )

    bootstrap_cfg = regen_cfg.get("server_bootstrap", {})
    if bootstrap_cfg is None:
        bootstrap_cfg = {}
    if not isinstance(bootstrap_cfg, dict):
        raise ValueError("`data.regen.server_bootstrap` must be an object when set")

    require_all = bool(bootstrap_cfg.get("require_all", True))
    ready_timeout_sec = max(1, int(bootstrap_cfg.get("ready_timeout_sec", 600)))
    poll_interval_sec = max(1, int(bootstrap_cfg.get("poll_interval_sec", 5)))
    probe_timeout_sec = max(1, int(bootstrap_cfg.get("probe_timeout_sec", 5)))

    entries_raw = bootstrap_cfg.get("entries", [])
    if entries_raw is None:
        entries_raw = []
    if not isinstance(entries_raw, list):
        raise ValueError("`data.regen.server_bootstrap.entries` must be a list")

    entry_by_address: Dict[str, Dict[str, str]] = {}
    for idx, item in enumerate(entries_raw):
        if not isinstance(item, dict):
            raise ValueError(
                f"`data.regen.server_bootstrap.entries[{idx}]` must be an object"
            )
        address_raw = item.get("address")
        launch_cmd = str(item.get("launch_cmd", "")).strip()
        if address_raw is None:
            raise ValueError(
                f"`data.regen.server_bootstrap.entries[{idx}].address` is required"
            )
        if not launch_cmd:
            raise ValueError(
                f"`data.regen.server_bootstrap.entries[{idx}].launch_cmd` is required"
            )

        parsed = normalize_server_addresses(address_raw)
        if len(parsed) != 1:
            raise ValueError(
                f"`data.regen.server_bootstrap.entries[{idx}].address` must be one host:port"
            )
        entry_by_address[parsed[0]] = {"launch_cmd": launch_cmd}

    state.set_step(step_name, "running")
    state.append_history("step_started", {"step": step_name, "index": step_index})
    notifier.send(
        event="STEP_STARTED",
        title=f"{step_name} started",
        message=f"Started step `{step_name}`.",
        context={"step": step_name, "index": step_index},
        force=True,
        dedupe_key=f"STEP_STARTED:{step_name}",
    )

    launched: List[Dict[str, Any]] = []
    initial_probe: Dict[str, Dict[str, Any]] = {}
    final_probe: Dict[str, Dict[str, Any]] = {}
    available_addresses: List[str] = []
    unavailable_addresses: List[str] = []

    def finalize_and_maybe_fail(error_message: Optional[str]) -> List[str]:
        ensure_dir(log_path.parent)
        if not log_lines:
            log_lines.append("No preflight logs generated.")
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

        returncode = 0 if error_message is None else 1
        summary = {
            "step": step_name,
            "index": step_index,
            "command": "internal: regen server preflight",
            "returncode": returncode,
            "duration_sec": round(time.time() - started_at, 4),
            "log_path": str(log_path),
            "finished_at": utc_now_iso(),
            "run_id": run_id,
            "dry_run": dry_run,
            "require_all": require_all,
            "requested_addresses": requested_addresses,
            "available_addresses": available_addresses,
            "unavailable_addresses": unavailable_addresses,
            "bootstrap_entries": sorted(entry_by_address.keys()),
            "launched": launched,
            "initial_probe": initial_probe,
            "final_probe": final_probe,
        }
        write_json(summary_path, summary)
        state.set_artifact(f"step_{step_index:02d}_summary", str(summary_path))
        state.set_artifact(f"step_{step_index:02d}_log", str(log_path))

        if error_message is not None:
            state.set_step(step_name, "failed")
            notifier.send(
                event="STEP_FAILED",
                title=f"{step_name} failed",
                message=error_message,
                context={
                    "step": step_name,
                    "returncode": 1,
                    "log_path": str(log_path),
                    "summary_path": str(summary_path),
                },
                force=True,
                dedupe_key=f"STEP_FAILED:{step_name}",
            )
            raise PipelineError(error_message)

        state.set_step(step_name, "completed")
        state.append_history(
            "step_completed",
            {
                "step": step_name,
                "index": step_index,
                "duration_sec": round(time.time() - started_at, 4),
            },
        )
        notifier.send(
            event="STEP_COMPLETED",
            title=f"{step_name} completed",
            message=(
                f"Step `{step_name}` completed. "
                f"available={len(available_addresses)}, unavailable={len(unavailable_addresses)}."
            ),
            context={
                "step": step_name,
                "available_addresses": available_addresses,
                "unavailable_addresses": unavailable_addresses,
                "log_path": str(log_path),
                "summary_path": str(summary_path),
            },
            force=True,
            dedupe_key=f"STEP_COMPLETED:{step_name}",
        )
        return available_addresses

    log_lines.append(
        f"Preflight start: require_all={require_all}, dry_run={dry_run}, requested={requested_addresses}"
    )
    if dry_run:
        available_addresses.extend(requested_addresses)
        log_lines.append("Dry-run enabled: skipping server probing and bootstrap.")
        state.set_metric(
            "regen_server_preflight",
            {
                "requested": requested_addresses,
                "available": available_addresses,
                "unavailable": unavailable_addresses,
                "dry_run": True,
            },
        )
        return finalize_and_maybe_fail(None)

    availability_map: Dict[str, bool] = {}
    for address in requested_addresses:
        ok, detail = probe_regen_server(
            runner=runner,
            python_bin=python_bin,
            address=address,
            timeout_sec=probe_timeout_sec,
        )
        availability_map[address] = ok
        initial_probe[address] = {"ok": ok, "detail": detail}
        log_lines.append(
            f"Initial probe {address}: {'OK' if ok else 'FAIL'} ({detail})"
        )

    for address in requested_addresses:
        if availability_map.get(address, False):
            continue

        entry = entry_by_address.get(address)
        if entry is None:
            log_lines.append(
                f"{address}: unavailable and no bootstrap launch_cmd configured."
            )
            continue

        server_log_path = (
            logs_dir / f"regen_server_{sanitize_filename_fragment(address)}.log"
        )
        pid, launch_detail, launch_log_path = launch_regen_server(
            runner=runner,
            launch_cmd=entry["launch_cmd"],
            log_path=server_log_path,
        )
        launched_item: Dict[str, Any] = {
            "address": address,
            "launch_cmd": entry["launch_cmd"],
            "pid": pid,
            "server_log_path": launch_log_path,
            "launch_detail": launch_detail,
        }
        launched.append(launched_item)

        if pid is None:
            log_lines.append(f"Launch failed {address}: {launch_detail}")
            continue

        log_lines.append(
            f"Launched {address} with pid={pid}; waiting up to {ready_timeout_sec}s."
        )
        deadline = time.time() + ready_timeout_sec
        while time.time() < deadline:
            ok, detail = probe_regen_server(
                runner=runner,
                python_bin=python_bin,
                address=address,
                timeout_sec=probe_timeout_sec,
            )
            if ok:
                availability_map[address] = True
                launched_item["ready"] = True
                launched_item["ready_detail"] = detail
                log_lines.append(f"{address} became ready ({detail}).")
                break
            time.sleep(poll_interval_sec)

        if not availability_map.get(address, False):
            launched_item["ready"] = False
            log_lines.append(f"{address} did not become ready before timeout.")

    for address in requested_addresses:
        ok, detail = probe_regen_server(
            runner=runner,
            python_bin=python_bin,
            address=address,
            timeout_sec=probe_timeout_sec,
        )
        final_probe[address] = {"ok": ok, "detail": detail}
        if ok:
            available_addresses.append(address)
        else:
            unavailable_addresses.append(address)

    state.set_metric(
        "regen_server_preflight",
        {
            "requested": requested_addresses,
            "available": available_addresses,
            "unavailable": unavailable_addresses,
            "require_all": require_all,
            "dry_run": False,
        },
    )
    state.set_artifact("regen_available_server_addresses", available_addresses)

    if not available_addresses:
        return finalize_and_maybe_fail(
            "Regen server preflight failed: no available server addresses."
        )

    if require_all and unavailable_addresses:
        return finalize_and_maybe_fail(
            "Regen server preflight failed: unavailable addresses: "
            + ", ".join(unavailable_addresses)
        )

    if unavailable_addresses:
        log_lines.append(
            "Continuing with partial server availability (require_all=false): "
            + ", ".join(unavailable_addresses)
        )
    return finalize_and_maybe_fail(None)


def validate_regen_is_required(spec: Mapping[str, Any]) -> None:
    data_cfg = spec.get("data", {})
    if not isinstance(data_cfg, dict):
        raise ValueError("`data` must be an object.")

    regen_cfg = data_cfg.get("regen")
    if not isinstance(regen_cfg, dict):
        raise ValueError(
            "Online pipeline requires regen. Please configure `data.regen`."
        )

    if not bool(regen_cfg.get("enabled", False)):
        raise ValueError(
            "Online pipeline requires regen. Please set `data.regen.enabled=true`."
        )

    server_addresses = normalize_server_addresses(regen_cfg.get("server_address"))
    if not server_addresses:
        raise ValueError(
            "Online pipeline requires regen server addresses. "
            "Please set `data.regen.server_address` to at least one host:port."
        )


def append_tracking_args(cmd: List[str], tracking: Mapping[str, Any]) -> None:
    report_to = tracking.get("report_to")
    if report_to:
        cmd.extend(["--report-to", str(report_to)])

    mapping = {
        "wandb_project": "--wandb-project",
        "wandb_name": "--wandb-name",
        "wandb_key": "--wandb-key",
        "swanlab_project": "--swanlab-project",
        "swanlab_name": "--swanlab-name",
        "swanlab_key": "--swanlab-key",
        "mlflow_tracking_uri": "--mlflow-tracking-uri",
        "mlflow_experiment_name": "--mlflow-experiment-name",
        "mlflow_run_name": "--mlflow-run-name",
    }
    for key, flag in mapping.items():
        value = tracking.get(key)
        if value is not None:
            cmd.extend([flag, str(value)])


def build_train_command(
    spec: Mapping[str, Any],
    plan: Mapping[str, Any],
    train_data_path: str,
    output_dir: Path,
    python_bin: str,
) -> Tuple[List[str], Optional[int]]:
    algo = str(spec.get("task", {}).get("algo", "eagle3")).lower()
    models = spec.get("models", {})
    training = spec.get("training", {})
    tracking = spec.get("tracking", {})

    nproc_per_node = int(plan.get("nproc_per_node", 1))
    tp_size = int(plan.get("tp_size", 1))

    torchrun = str(training.get("torchrun_bin", "torchrun"))

    base_cmd = [torchrun, "--standalone", "--nproc_per_node", str(nproc_per_node)]
    if algo == "eagle3":
        cmd = base_cmd + [
            "scripts/train_eagle3.py",
            "--target-model-path",
            str(models.get("target_model_path")),
            "--train-data-path",
            train_data_path,
            "--output-dir",
            str(output_dir),
            "--chat-template",
            str(models.get("chat_template", training.get("chat_template", "llama3"))),
            "--tp-size",
            str(tp_size),
            "--target-model-backend",
            str(plan.get("target_model_backend", "sglang")),
        ]

        if models.get("draft_model_config"):
            cmd.extend(["--draft-model-config", str(models["draft_model_config"])])
        if models.get("embedding_key"):
            cmd.extend(["--embedding-key", str(models["embedding_key"])])

        int_args = {
            "num_epochs": "--num-epochs",
            "batch_size": "--batch-size",
            "max_length": "--max-length",
            "eval_interval": "--eval-interval",
            "save_interval": "--save-interval",
            "log_interval": "--log-interval",
            "build_dataset_num_proc": "--build-dataset-num-proc",
            "max_num_steps": "--max-num-steps",
        }
        float_args = {
            "learning_rate": "--learning-rate",
            "warmup_ratio": "--warmup-ratio",
            "max_grad_norm": "--max-grad-norm",
        }

        for key, flag in int_args.items():
            if training.get(key) is not None:
                cmd.extend([flag, str(training[key])])
        for key, flag in float_args.items():
            if training.get(key) is not None:
                cmd.extend([flag, str(training[key])])

        if training.get("resume", False):
            cmd.append("--resume")
        if training.get("ckpt_dir"):
            cmd.extend(["--ckpt-dir", str(training["ckpt_dir"])])
        if training.get("trust_remote_code", False) or models.get(
            "trust_remote_code", False
        ):
            cmd.append("--trust-remote-code")
        if training.get("is_preformatted", False):
            cmd.append("--is-preformatted")

    elif algo == "dflash":
        cmd = base_cmd + [
            "scripts/train_dflash.py",
            "--target-model-path",
            str(models.get("target_model_path")),
            "--train-data-path",
            train_data_path,
            "--output-dir",
            str(output_dir),
            "--chat-template",
            str(models.get("chat_template", training.get("chat_template", "qwen"))),
            "--tp-size",
            str(tp_size),
            "--target-model-backend",
            str(plan.get("target_model_backend", "sglang")),
        ]

        if models.get("draft_model_config"):
            cmd.extend(["--draft-config-path", str(models["draft_model_config"])])

        int_args = {
            "num_epochs": "--num-epochs",
            "batch_size": "--batch-size",
            "max_length": "--max-length",
            "eval_interval": "--eval-interval",
            "save_interval": "--save-interval",
            "log_interval": "--log-interval",
            "build_dataset_num_proc": "--build-dataset-num-proc",
            "block_size": "--block-size",
            "num_anchors": "--num-anchors",
            "num_draft_layers": "--num-draft-layers",
        }
        float_args = {
            "learning_rate": "--learning-rate",
            "warmup_ratio": "--warmup-ratio",
            "max_grad_norm": "--max-grad-norm",
            "loss_decay_gamma": "--loss-decay-gamma",
        }

        for key, flag in int_args.items():
            if training.get(key) is not None:
                cmd.extend([flag, str(training[key])])
        for key, flag in float_args.items():
            if training.get(key) is not None:
                cmd.extend([flag, str(training[key])])

        if training.get("attention_backend"):
            cmd.extend(["--attention-backend", str(training["attention_backend"])])
        if training.get("resume", False):
            cmd.append("--resume")
        if training.get("ckpt_dir"):
            cmd.extend(["--ckpt-dir", str(training["ckpt_dir"])])
        if training.get("trust_remote_code", False) or models.get(
            "trust_remote_code", False
        ):
            cmd.append("--trust-remote-code")
        if training.get("is_preformatted", False):
            cmd.append("--is-preformatted")
    else:
        raise ValueError(f"Unsupported algo: {algo}. Expected `eagle3` or `dflash`.")

    append_tracking_args(cmd, tracking)

    if training.get("additional_args"):
        cmd.extend([str(x) for x in training["additional_args"]])

    total_hint = training.get("max_num_steps")
    if total_hint is not None:
        total_hint = int(total_hint)

    if python_bin and python_bin != "python3":
        # For environments without torchrun in PATH, allow user to provide module style runner.
        # Keep default unchanged when torchrun is available.
        pass

    return cmd, total_hint


def run_step(
    step_index: int,
    step_name: str,
    command: Sequence[str],
    runner: CommandRunner,
    state: StateStore,
    notifier: PipelineNotifier,
    logs_dir: Path,
    summaries_dir: Path,
    run_id: str,
    dry_run: bool,
    total_steps_hint: Optional[int] = None,
    cwd: Optional[str] = None,
) -> CommandResult:
    state.set_step(step_name, "running")
    state.append_history("step_started", {"step": step_name, "index": step_index})

    notifier.send(
        event="STEP_STARTED",
        title=f"{step_name} started",
        message=f"Started step `{step_name}`.",
        context={"step": step_name, "index": step_index},
        force=True,
        dedupe_key=f"STEP_STARTED:{step_name}",
    )

    progress = ProgressReporter(
        notifier=notifier,
        state=state,
        phase=step_name,
        run_id=run_id,
        total_steps_hint=total_steps_hint,
        percent_step=5,
        min_interval_sec=600,
    )

    log_path = logs_dir / f"step_{step_index:02d}_{step_name.lower()}.log"

    result = runner.run_stream(
        command=command,
        log_path=log_path,
        cwd=cwd,
        on_line=progress.parse_line,
        dry_run=dry_run,
    )

    summary = {
        "step": step_name,
        "index": step_index,
        "command": result.command,
        "returncode": result.returncode,
        "duration_sec": result.duration_sec,
        "log_path": result.log_path,
        "finished_at": utc_now_iso(),
    }
    summary_path = (
        summaries_dir / f"step_{step_index:02d}_{step_name.lower()}_summary.json"
    )
    write_json(summary_path, summary)

    state.set_artifact(f"step_{step_index:02d}_summary", str(summary_path))
    state.set_artifact(f"step_{step_index:02d}_log", str(log_path))

    if result.returncode != 0:
        state.set_step(step_name, "failed")
        notifier.send(
            event="STEP_FAILED",
            title=f"{step_name} failed",
            message=(
                f"Step `{step_name}` failed with exit code {result.returncode}. "
                f"See log: {log_path}"
            ),
            context={
                "step": step_name,
                "returncode": result.returncode,
                "log_path": str(log_path),
            },
            force=True,
            dedupe_key=f"STEP_FAILED:{step_name}",
        )
        raise PipelineError(
            f"Step {step_name} failed with exit code {result.returncode}. Log: {log_path}"
        )

    state.set_step(step_name, "completed")
    state.append_history(
        "step_completed",
        {
            "step": step_name,
            "index": step_index,
            "duration_sec": result.duration_sec,
        },
    )

    notifier.send(
        event="STEP_COMPLETED",
        title=f"{step_name} completed",
        message=f"Step `{step_name}` completed successfully in {result.duration_sec:.1f}s.",
        context={
            "step": step_name,
            "duration_sec": round(result.duration_sec, 2),
            "log_path": str(log_path),
            "summary_path": str(summary_path),
        },
        force=True,
        dedupe_key=f"STEP_COMPLETED:{step_name}",
    )

    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run SpecForge online training pipeline"
    )
    parser.add_argument("--spec", type=str, required=True, help="Path to pipeline spec")
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Only generate parallel plan and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not execute commands; only generate commands and summaries",
    )
    parser.add_argument(
        "--python-bin",
        type=str,
        default="python3",
        help="Python executable for child scripts",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parent.parent
    spec_path = Path(args.spec).expanduser().resolve()
    spec = load_spec(spec_path)

    run_cfg = spec.get("run", {})
    run_id = str(run_cfg.get("id", f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"))
    output_dir = Path(
        run_cfg.get("output_dir", str(repo_root / "outputs" / run_id))
    ).expanduser()
    output_dir = output_dir if output_dir.is_absolute() else (repo_root / output_dir)
    ensure_dir(output_dir)

    logs_dir = output_dir / "logs"
    summaries_dir = output_dir / "summaries"
    ensure_dir(logs_dir)
    ensure_dir(summaries_dir)

    dry_run = bool(run_cfg.get("dry_run", False)) or args.dry_run

    state = StateStore(output_dir / "run_state.json", run_id=run_id)
    resolved_spec_path = output_dir / "resolved_spec.json"
    write_json(resolved_spec_path, spec)
    state.set_artifact("resolved_spec", str(resolved_spec_path))

    NotificationManager, create_hooks = load_notify_api(repo_root)
    notifications_cfg = spec.get("notifications", {})
    hooks_cfg = notifications_cfg.get("hooks", [])
    manager = NotificationManager(create_hooks(hooks_cfg)) if hooks_cfg else None

    notifier = PipelineNotifier(
        manager=manager,
        run_id=run_id,
        cooldown_sec=int(notifications_cfg.get("cooldown_sec", 600)),
        enabled=bool(notifications_cfg.get("enabled", True)),
    )

    machine_cfg = spec.get("machine", {})
    runner = CommandRunner(machine_cfg=machine_cfg, default_cwd=repo_root)

    state.set_status("running")
    state.append_history("run_started", {"spec": str(spec_path), "dry_run": dry_run})
    notifier.send(
        event="RUN_STARTED",
        title="Pipeline started",
        message=f"SpecForge online pipeline started: {run_id}",
        context={
            "spec": str(spec_path),
            "output_dir": str(output_dir),
            "dry_run": dry_run,
        },
        force=True,
        dedupe_key="RUN_STARTED",
    )

    try:
        # Step 0: precheck + parallel planning
        state.set_step("PRECHECK", "running")
        gpu_info = probe_gpu_info(runner)
        parallel_plan = choose_parallel_plan(spec, gpus=gpu_info)
        parallel_plan_path = output_dir / "parallel_plan.json"
        write_json(parallel_plan_path, parallel_plan)
        state.set_artifact("parallel_plan", str(parallel_plan_path))
        state.set_metric("gpu_count", len(gpu_info))
        state.set_step("PRECHECK", "completed")

        notifier.send(
            event="PARALLEL_PLAN_READY",
            title="Parallel plan ready",
            message=(
                f"selected_gpus={parallel_plan['selected_gpus']}, "
                f"tp_size={parallel_plan['tp_size']}, dp_size={parallel_plan['dp_size']}"
            ),
            context={"parallel_plan": parallel_plan, "path": str(parallel_plan_path)},
            force=True,
            dedupe_key="PARALLEL_PLAN_READY",
        )

        if args.plan_only:
            state.set_status("completed")
            state.append_history(
                "plan_only_completed", {"parallel_plan": parallel_plan}
            )
            notifier.send(
                event="RUN_COMPLETED",
                title="Plan generated",
                message="Plan-only mode completed successfully.",
                context={"parallel_plan_path": str(parallel_plan_path)},
                force=True,
                dedupe_key="RUN_COMPLETED",
            )
            print(
                json.dumps(
                    {
                        "run_id": run_id,
                        "status": "completed",
                        "parallel_plan_path": str(parallel_plan_path),
                        "parallel_plan": parallel_plan,
                    },
                    indent=2,
                )
            )
            return 0

        validate_regen_is_required(spec)

        python_bin = args.python_bin

        # Step 1: prepare data (or use provided path)
        step_index = 1
        prepare_cmd, prepared_train_path = build_prepare_data_command(spec, python_bin)
        if prepare_cmd:
            run_step(
                step_index=step_index,
                step_name="PREPARE_DATA",
                command=prepare_cmd,
                runner=runner,
                state=state,
                notifier=notifier,
                logs_dir=logs_dir,
                summaries_dir=summaries_dir,
                run_id=run_id,
                dry_run=dry_run,
            )
        else:
            state.append_history(
                "step_skipped",
                {
                    "step": "PREPARE_DATA",
                    "reason": "data.source.type=path",
                    "train_data_path": prepared_train_path,
                },
            )

        state.set_artifact("train_data_path", prepared_train_path)

        # Step 2: regen server preflight + regenerate data (required)
        regen_cfg = spec.get("data", {}).get("regen", {})
        final_train_path = prepared_train_path
        step_index += 1
        available_regen_addresses = run_regen_server_preflight_step(
            step_index=step_index,
            spec=spec,
            runner=runner,
            state=state,
            notifier=notifier,
            logs_dir=logs_dir,
            summaries_dir=summaries_dir,
            run_id=run_id,
            dry_run=dry_run,
            python_bin=python_bin,
        )

        step_index += 1
        regen_output_path = str(
            regen_cfg.get(
                "output_file_path",
                output_dir / "data" / "train_regen.jsonl",
            )
        )
        regen_cmd = build_regen_command(
            spec=spec,
            python_bin=python_bin,
            input_path=prepared_train_path,
            output_path=regen_output_path,
            server_addresses=available_regen_addresses,
        )
        run_step(
            step_index=step_index,
            step_name="REGEN_DATA",
            command=regen_cmd,
            runner=runner,
            state=state,
            notifier=notifier,
            logs_dir=logs_dir,
            summaries_dir=summaries_dir,
            run_id=run_id,
            dry_run=dry_run,
        )
        final_train_path = regen_output_path
        state.set_artifact("regen_output_path", final_train_path)

        # Step 3: train online
        step_index += 1
        train_output_dir = output_dir / "model_outputs"
        ensure_dir(train_output_dir)
        train_cmd, total_hint = build_train_command(
            spec=spec,
            plan=parallel_plan,
            train_data_path=final_train_path,
            output_dir=train_output_dir,
            python_bin=python_bin,
        )
        run_step(
            step_index=step_index,
            step_name="TRAIN_ONLINE",
            command=train_cmd,
            runner=runner,
            state=state,
            notifier=notifier,
            logs_dir=logs_dir,
            summaries_dir=summaries_dir,
            run_id=run_id,
            dry_run=dry_run,
            total_steps_hint=total_hint,
        )

        state.set_artifact("model_output_dir", str(train_output_dir))
        state.set_status("completed")
        state.append_history("run_completed", {"output_dir": str(output_dir)})

        notifier.send(
            event="RUN_COMPLETED",
            title="Pipeline completed",
            message=f"SpecForge online pipeline completed: {run_id}",
            context={
                "output_dir": str(output_dir),
                "model_output_dir": str(train_output_dir),
                "run_state": str(state.path),
            },
            force=True,
            dedupe_key="RUN_COMPLETED",
        )

        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": "completed",
                    "output_dir": str(output_dir),
                },
                indent=2,
            )
        )
        return 0

    except Exception as exc:
        state.set_status("failed")
        state.set_error(str(exc))
        state.append_history("run_failed", {"error": str(exc)})

        notifier.send(
            event="RUN_FAILED",
            title="Pipeline failed",
            message=str(exc),
            context={"run_state": str(state.path), "output_dir": str(output_dir)},
            force=True,
            dedupe_key="RUN_FAILED",
        )

        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
