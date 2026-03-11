# Patch Assets

## `sglang_qwen3_5_eagle3_compat.patch`

Adds `set_eagle3_layers_to_capture(...)` to `sglang/srt/models/qwen3_5.py` so
Qwen3.5 target model can provide Eagle3 auxiliary hidden-state capture config.

Typical apply flow (installed package):

```bash
cd /path/to/site-packages
patch -p0 < /path/to/SpecForge/patches/sglang_qwen3_5_eagle3_compat.patch
```

After apply, restart training/inference processes that import `sglang`.
