---
name: specforge-online-trainer
description: Run end-to-end SpecForge online training with minimal manual operations (prepare data, optional regen, auto parallel planning, training, progress notifications, and run-state outputs). Use this when users want the agent to execute a full training run instead of manually editing config/sh scripts.
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

3. Run full pipeline:

```bash
python3 scripts/run_online_pipeline.py --spec <spec.json>
```

4. Check outputs:
- `outputs/<run_id>/run_state.json`
- `outputs/<run_id>/parallel_plan.json`
- `outputs/<run_id>/summaries/step_*_summary.json`
- `outputs/<run_id>/logs/*.log`

## Execution Expectations

- Treat this skill as managed execution, not just fire-and-forget launch.
- Prefer periodic monitoring over constant terminal attachment for long runs. Default policy: check `run_state.json` and recent logs every 20 minutes unless the user asks for a different interval.
- On failure, first collect diagnosis context (`run_state.json`, failing step summary, failing log tail, recent launcher output, resolved spec, parallel plan).
- A failed run must be diagnosed before any automatic restart. Do not restart first and analyze later.
- If an LLM diagnostic hook or agent entrypoint is available in the environment, call it with the diagnosis context before deciding whether to restart.
- If no automated LLM hook is available, stop automatic recovery after writing the diagnosis bundle and surface the failure context for user review or the next agent turn.
- After a successful diagnosis, restart the pipeline from the same spec only when recovery is low-risk (`resume=true`, artifact paths still valid, no destructive cleanup needed).
- Report step transitions and restart events concisely rather than continuously streaming logs.

## Notifications

- Prefer webhook for real-time events.
- Enable `gmail` or `smtp` hooks for failed/completed summaries.
- Keep `STEP_PROGRESS` notifications throttled via `notifications.cooldown_sec`.

## Guardrails

- This skill only targets online mode.
- Do not use hidden-state offline preparation in this workflow.
- Preserve reproducibility: keep resolved spec and generated plans in output artifacts.
