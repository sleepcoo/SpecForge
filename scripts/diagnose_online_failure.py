#!/usr/bin/env python3
"""Diagnose a pipeline failure bundle and decide whether auto-restart is safe."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List


def load_bundle(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def contains_any(lines: List[str], patterns: List[str]) -> bool:
    joined = "\n".join(lines).lower()
    return any(pattern.lower() in joined for pattern in patterns)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose a SpecForge online pipeline failure bundle."
    )
    parser.add_argument("bundle_path", help="Path to failure bundle JSON")
    args = parser.parse_args()

    bundle_path = Path(args.bundle_path)
    bundle = load_bundle(bundle_path)
    step_log_tail = bundle.get("step_log_tail") or []
    last_error = str(bundle.get("last_error") or "")
    current_step = str(bundle.get("current_step") or "")

    decision = {
        "bundle_path": str(bundle_path),
        "current_step": current_step,
        "restart_allowed": False,
        "reason": "unknown",
        "notes": [],
    }

    lower_error = last_error.lower()

    if "filenotfounderror" in lower_error or contains_any(
        step_log_tail, ["filenotfounderror", "no such file or directory"]
    ):
        decision["restart_allowed"] = True
        decision["reason"] = "missing_path"
        decision["notes"].append("Failure looks like missing output path or file path issue.")
    elif contains_any(step_log_tail, ["connection refused", "service unavailable", "timed out"]):
        decision["restart_allowed"] = True
        decision["reason"] = "transient_service_error"
        decision["notes"].append("Failure looks transient at the service/network layer.")
    elif contains_any(
        step_log_tail,
        ["out of memory", "cuda out of memory", "cublas", "nccl", "illegal memory access"],
    ):
        decision["restart_allowed"] = False
        decision["reason"] = "gpu_runtime_error"
        decision["notes"].append("GPU runtime failure needs manual inspection before restart.")
    elif contains_any(step_log_tail, ["permission denied", "authentication", "unauthorized"]):
        decision["restart_allowed"] = False
        decision["reason"] = "auth_or_permission_error"
        decision["notes"].append("Credential or permission error should not be auto-restarted.")
    else:
        decision["restart_allowed"] = False
        decision["reason"] = "unclassified_failure"
        decision["notes"].append("No safe automatic recovery rule matched.")

    output_path = bundle_path.with_suffix(".diagnosis.json")
    output_path.write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(decision, ensure_ascii=False))
    return 0 if decision["restart_allowed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
