---
name: specforge-online-trainer
description: Run end-to-end SpecForge online training with minimal manual operations (prepare data, mandatory regen server preflight and regen, auto parallel planning, training, optional Gmail notifications, and run-state outputs). Use this when users want the agent to execute a full training run instead of manually editing config/sh scripts.
---

# SpecForge Online Trainer

Use this skill to execute one full online training run with SpecForge and reduce manual friction.

## When To Use

- User wants "one-command" training execution.
- User wants regen-to-train auto handoff.
- User wants optional status notifications (Gmail only).
- User wants run state and summaries for audit/recovery.

## Inputs Required

- `models.target_model_path`: Hugging Face model ID or local model path.
- `task.algo`: `eagle3` or `dflash`.
- Training data source:
  - Built-in dataset via `data.source.dataset`, or
  - Existing JSONL via `data.source.type=path` and `data.source.train_data_path`.
- Machine mode:
  - `machine.mode=local`, or
  - `machine.mode=ssh` with host/user/port/workspace.
- Notification decision (must ask user before run):
  - Ask: "本次训练要不要开启 Gmail 通知？"
  - If user says no: set `notifications.enabled=false`.
  - If user says yes: configure one Gmail hook and use env var `SMTP_PASSWORD`.

## Default Workflow

1. Create/update a pipeline spec from `configs/pipeline_online.example.json`.
2. Run parallel planning first (equivalent to using skill `specforge-parallel-planner`):

```bash
python3 scripts/run_online_pipeline.py --spec <spec.json> --plan-only
```

3. Rely on built-in regen orchestration (no separate regen skill call required):
   - `run_online_pipeline.py` already executes `REGEN_SERVER_PREFLIGHT` and `REGEN_DATA`.
   - Use skill `specforge-regen-orchestrator` only for standalone debug/recovery runs.
4. Run full pipeline:

```bash
python3 scripts/run_online_pipeline.py --spec <spec.json>
```

5. Check outputs:
- `outputs/<run_id>/run_state.json`
- `outputs/<run_id>/parallel_plan.json`
- `outputs/<run_id>/summaries/step_*_summary.json`
- `outputs/<run_id>/logs/*.log`

## Notifications

- Only Gmail notification hook is supported in this workflow.
- Always ask user whether to enable notifications before execution.
- If enabled, prefer minimal event set for smoke runs:
  - `RUN_FAILED`
  - `RUN_COMPLETED`
- Keep notification credentials in environment variables (for example `SMTP_PASSWORD`), not inline plaintext.

## Guardrails

- This skill only targets online mode.
- Do not use hidden-state offline preparation in this workflow.
- Preserve reproducibility: keep resolved spec and generated plans in output artifacts.
- Regen is mandatory for this workflow; do not bypass it.
- If regen server preflight fails, stop and report the missing server/bootstrap details.
