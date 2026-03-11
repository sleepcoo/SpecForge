---
name: specforge-regen-orchestrator
description: Orchestrate SpecForge regen readiness and execution by probing SGLang endpoints, optionally launching missing servers, waiting for readiness, and running regenerate_train_data.py with fail-fast behavior.
---

# SpecForge Regen Orchestrator

Use this skill for every online training run. Regen is treated as a required phase.

## When To Use

- Any online training run is about to start.
- User expects one-command regen without manually starting SGLang.
- Existing server ports are unstable or partially unavailable.
- A run must fail fast instead of silently reducing server count.

## Inputs Required

- `models.target_model_path` (or `data.regen.model`)
- `data.regen.server_address` (list of `host:port`)
- `data.source` (prepared dataset path or built-in dataset config)
- `machine.mode` (`local` or `ssh`)

Optional bootstrap contract (recommended in spec for deterministic runs):

- `data.regen.server_bootstrap.require_all` (default: `true`)
- `data.regen.server_bootstrap.ready_timeout_sec` (default: `600`)
- `data.regen.server_bootstrap.poll_interval_sec` (default: `5`)
- `data.regen.server_bootstrap.entries[]`:
  - `address`: `host:port` matching one element in `server_address`
  - `launch_cmd`: shell command to start SGLang for that address

## Default Workflow

1. Validate regen inputs and normalize server addresses.
2. Probe each server:

```bash
curl -sf http://<host:port>/v1/models
```

3. For unavailable addresses:
- If `launch_cmd` exists for the address, launch it (local or SSH workspace).
- Poll readiness until timeout.
- If still unavailable and `require_all=true`, fail the run.

4. Once readiness passes, run regen:

```bash
python3 scripts/regenerate_train_data.py \
  --model <model> \
  --server-address <addr1> [<addr2> ...] \
  --input-file-path <prepared_train.jsonl> \
  --output-file-path <regen_output.jsonl> \
  --concurrency <n> \
  --max-tokens <n> \
  --temperature <t>
```

5. Validate outputs:
- Regen output exists and has non-zero lines.
- Error file is recorded when present.

## Integration

- `specforge-online-trainer` should call this skill before every full pipeline execution.
- This skill can also run as a standalone regen entrypoint for debug/recovery runs.

## Guardrails

- Never silently skip regen because a port is down.
- Never proceed with zero available servers.
- Use explicit `require_all=false` only when user accepts partial server availability.
- Report exact failed addresses and launch commands in the summary.
