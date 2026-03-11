---
name: specforge-online-trainer
description: Run end-to-end SpecForge online training with minimal manual operations (prepare data, mandatory regen server preflight and regen, auto parallel planning, training, progress notifications, and run-state outputs). Use this when users want the agent to execute a full training run instead of manually editing config/sh scripts.
---

# SpecForge Online Trainer

Use this skill to execute one full online training run with SpecForge and reduce manual friction.

## When To Use

- User wants "one-command" training execution.
- User wants regen-to-train auto handoff.
- User wants status notifications (webhook/smtp/gmail).
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

## Default Workflow

1. Create/update a pipeline spec from `configs/pipeline_online.example.json`.
2. Run parallel planning first (calls skill `specforge-parallel-planner`):

```bash
python3 scripts/run_online_pipeline.py --spec <spec.json> --plan-only
```

3. Always call skill `specforge-regen-orchestrator` before training starts to ensure configured SGLang endpoints are reachable and regen data is produced.
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

- Prefer webhook for real-time events.
- Enable `gmail` or `smtp` hooks for failed/completed summaries.
- Keep `STEP_PROGRESS` notifications throttled via `notifications.cooldown_sec`.

## Guardrails

- This skill only targets online mode.
- Do not use hidden-state offline preparation in this workflow.
- Preserve reproducibility: keep resolved spec and generated plans in output artifacts.
- Regen is mandatory for this workflow; do not bypass it.
- If regen server preflight fails, stop and report the missing server/bootstrap details.
