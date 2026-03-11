---
name: specforge-online-trainer
description: Run end-to-end SpecForge online training with minimal manual operations (prepare data, mandatory regen, auto parallel planning, training, optional Gmail notifications, and run-state outputs). Supports both inline pipeline mode and sequential shared-GPU mode where regen and training must run in separate phases.
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
- Execution mode:
  - `execution.mode=inline_pipeline` when regen and training do not compete for the same GPU allocation.
  - `execution.mode=sequential_shared_gpus` when regen and training must both use the full same GPU set on one machine.
- Notification decision (must ask user before run):
  - Ask: "本次训练要不要开启 Gmail 通知？"
  - If user says no: set `notifications.enabled=false`.
  - If user says yes: configure one Gmail hook and use env var `SMTP_PASSWORD`.

## Default Workflow

1. Decide execution mode first.
2. Create/update a spec from `configs/pipeline_online.example.json` or an existing run-specific spec.
3. Run parallel planning first:

```bash
python3 scripts/run_online_pipeline.py --spec <spec.json> --plan-only
```

4. Choose one of two execution paths:

### Mode A: `inline_pipeline`

- Use this only when regen servers can stay alive during training without stealing the training GPUs.
- Rely on built-in regen orchestration:
  - `run_online_pipeline.py` executes `REGEN_SERVER_PREFLIGHT` and `REGEN_DATA`.
  - Use skill `specforge-regen-orchestrator` only for standalone debug/recovery runs.
- Run full pipeline:

```bash
python3 scripts/run_online_pipeline.py --spec <spec.json>
```

### Mode B: `sequential_shared_gpus`

- Use this when regen and training both need the full same local GPU set.
- Do not keep regen servers alive into training.
- Run the stages explicitly:
  1. Launch regen SGLang servers across the intended GPUs.
  2. Run `scripts/regenerate_train_data.py`.
  3. Stop the regen SGLang servers and verify GPUs are free.
  4. Run `scripts/train_eagle3.py` or `scripts/train_dflash.py` with the regenerated dataset.
- In this mode, built-in `run_online_pipeline.py` is useful for planning and artifacts, but it should not be the executor unless the codebase has explicit server cleanup between regen and training.

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
- If regen and training share GPUs on the same machine, prefer `execution.mode=sequential_shared_gpus`.
- In `sequential_shared_gpus` mode, explicitly clean up regen servers before training starts.
- If regen server preflight fails, stop and report the missing server/bootstrap details.
