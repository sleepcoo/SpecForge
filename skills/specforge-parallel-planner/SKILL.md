---
name: specforge-parallel-planner
description: Generate parallel training configuration for SpecForge online runs by probing local or remote GPUs and applying dense-model heuristics for TP/DP/torchrun process counts. Use this before launching training to avoid manual parallel tuning.
---

# SpecForge Parallel Planner

Use this skill to produce a practical parallel plan (`nproc_per_node`, `tp_size`, `dp_size`) before training.

## When To Use

- User is unsure about GPU parallel settings.
- User switches between local and remote machines.
- User wants agent-driven parallel setup instead of manual trial-and-error.

## Inputs Required

- `models.target_model_path`
- `machine.mode` (`local` or `ssh`)
- Optional constraints:
  - `parallel.num_gpus`
  - `parallel.tp_size` (override)

## Command

```bash
python3 scripts/run_online_pipeline.py --spec <spec.json> --plan-only
```

## Output Contract

Planner writes `outputs/<run_id>/parallel_plan.json` with at least:
- `available_gpus`
- `selected_gpus`
- `nproc_per_node`
- `tp_size`
- `dp_size`
- `model_size_b` (when inferable)
- `rationale`
- `gpu_info`

## Heuristic Policy

- Dense small models default to low TP.
- Respect user override for `parallel.tp_size`.
- Never exceed available selected GPUs.
- Return rationale text for every automatic decision.

## Integration

This skill is called by `specforge-online-trainer` before full training execution.
