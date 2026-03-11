# SpecForge Online 训练 Agent 化改造研究

## 1. 目标重定义（基于最新需求）

本研究只关注 **Online 模式**，并将目标明确为：
- 不是让模型效果更好（不是算法研究优先）。
- 而是让 LLM/Agent **替代人执行一次完整训练流程**。
- 核心价值是降低人工摩擦，减少手动串流程导致的延误和错误。

你当前指出的关键摩擦点非常明确：
1. 每次都要手改 `config.json`。
2. 每次都要手写/改 `sh` 启动脚本。
3. `regen` 结束后还要人工手动接着启动训练。
4. 训练过程里还要人工盯 WandB（状态、异常、是否中断）。

本次方案全部围绕这 4 个点展开。

---

## 2. 当前流程的人力摩擦拆解

以一次典型 Online 训练为例，人工链路通常是：

1. 人工准备数据 -> 跑 `prepare_data.py`
2. 人工决定是否 `regen` -> 跑 `regenerate_train_data.py`
3. 人工等待 `regen` 完成后再手动启动训练
4. 人工改 `config.json` / 命令参数 / shell 脚本
5. 人工盯 WandB，看是否 loss 异常、是否 OOM、是否挂掉
6. 人工决定是否 resume、是否终止、是否重跑
7. 人工汇总结果

痛点本质：
- 任务是“长链路 + 强依赖顺序 + 大量机械操作”。
- 人在流程中主要做了“状态传递”和“命令拼接”，这正是 Agent 最适合替代的部分。

---

## 3. 目标态：Online 单链路自动执行

### 3.1 自动化边界

本改造只覆盖 Online：
- 包含：数据准备、可选 regen、自动启动 train_eagle3、WandB 监控、失败恢复、结果汇总。
- 不包含：offline hidden states 生成路径（`prepare_hidden_states.py` 不进入主路径）。

### 3.2 一次训练任务的目标态流程

1. 读取任务规范（一个 YAML/JSON）。
2. 自动生成运行参数与训练命令（无需手改 shell）。
3. 执行数据准备。
4. 如配置开启 regen：执行 regen，并在完成后自动衔接训练。
5. 启动 Online 训练（`scripts/train_eagle3.py`）。
6. 持续监控训练进度与 WandB 状态。
7. 异常时自动判定：重试 / resume / 失败退出。
8. 产出结构化报告（本次 run 的输入、指标、错误、结论）。

---

## 4. Agent 化设计（面向执行，不改训练算法）

## 4.1 核心思路

不是重写训练脚本，而是在现有脚本外新增一个 **Orchestrator Agent 层**：
- 复用现有脚本能力：
  - `scripts/prepare_data.py`
  - `scripts/regenerate_train_data.py`
  - `scripts/train_eagle3.py`
- 增加任务状态机、模板化参数、自动触发和自动监控。

## 4.2 推荐最小架构

一个主 Agent（必选）+ 两个轻量辅助模块（可先内嵌）：

1. `Run Orchestrator`（主控）
- 读取任务 spec
- 串行调度 step
- 管理状态与恢复

2. `Config Composer`（参数合成）
- 根据模板和任务 spec 生成训练参数
- 输出最终命令（可复现）

3. `Monitor`（运行监控）
- 跟踪进程存活、日志关键字、WandB 心跳
- 触发告警和自动恢复

---

## 5. 建议新增文件与契约

## 5.1 任务规范文件（单一事实来源）

建议新增：`pipeline_spec_online.yaml`

作用：
- 用声明式方式描述一次训练任务。
- 彻底替代手改配置 + 手写 shell。

示例：

```yaml
run:
  id: qwen3_8b_online_20260311
  output_dir: ./outputs/qwen3_8b_online_20260311
  dry_run: false

mode:
  training_mode: online

models:
  target_model_path: Qwen/Qwen3-8B
  draft_model_config: ./configs/qwen3-8b-eagle3.json
  chat_template: qwen3

data:
  source:
    type: built_in
    dataset: sharegpt
  regen:
    enabled: true
    model: Qwen/Qwen3-8B
    server_address: ["localhost:30000"]
    concurrency: 128
    temperature: 0.8

training:
  num_epochs: 6
  batch_size: 1
  learning_rate: 1e-4
  max_length: 2048
  eval_interval: 2000
  save_interval: 2000
  log_interval: 50
  resume: true

tracking:
  report_to: wandb
  wandb_project: specforge-online
  wandb_name: qwen3_8b_online_20260311

recovery:
  max_retries: 2
  retry_on:
    - transient_network_error
    - sglang_server_not_ready
    - dataloader_timeout
```

## 5.2 运行状态文件

建议新增：`outputs/<run_id>/run_state.json`

字段建议：
- `current_step`
- `step_status`
- `artifacts`
- `pids`
- `wandb_run_url`
- `last_error`
- `retry_count`
- `updated_at`

作用：
- 支持中断恢复。
- 支持 Agent 接管后继续执行，不重复已完成步骤。

## 5.3 步骤摘要文件

建议每个 step 输出 `step_xx_summary.json`，至少包含：
- 输入参数快照
- 产物路径
- 核心计数（样本数、成功/失败数）
- 耗时
- exit code

---

## 6. 在线训练自动化状态机（建议）

```text
INIT
  -> PRECHECK
  -> PREPARE_DATA
  -> REGEN_DATA (optional)
  -> TRAIN_ONLINE
  -> MONITOR
  -> EVALUATE_SUMMARY
  -> DONE

任一状态失败 -> RECOVERY_DECISION
RECOVERY_DECISION -> RETRY_CURRENT | RESUME_TRAIN | FAIL
```

状态说明：
- `PRECHECK`：检查依赖、GPU、输出目录、WandB key、SGLang 连通性。
- `PREPARE_DATA`：执行并校验数据文件是否生成。
- `REGEN_DATA`：执行 regen 并等待完成，不需人工接力。
- `TRAIN_ONLINE`：自动拼接并启动 `train_eagle3.py`。
- `MONITOR`：抓取日志和 WandB 指标，识别异常模式。
- `RECOVERY_DECISION`：按策略自动重试或 resume。

---

## 7. 你关心的四个摩擦点如何被替代

## 7.1 手改 config.json -> 模板+参数合成

改造：
- 使用 `pipeline_spec_online.yaml` + 模板生成最终参数。
- 对于小 dense 模型默认不手改 draft config：由 `train_eagle3.py` 自动从 target 生成并对齐 hidden 维度。
- 保留最终“已解析参数快照”到 `resolved_config.json`。

收益：
- 避免手改错误与历史漂移。
- 每次 run 可完整复现。

## 7.2 手写 shell -> Agent 生成并执行命令

改造：
- Orchestrator 直接生成标准命令并执行，不再依赖人工写 `sh`。
- 可选保留一份 `run.sh` 仅用于审计。

收益：
- 减少机械操作，命令来源统一。

## 7.3 regen 后手动启动训练 -> 事件驱动自动衔接

改造：
- `REGEN_DATA` 成功后自动触发 `TRAIN_ONLINE`。
- 在状态文件中记录 `regen_output_path` 作为训练输入。

收益：
- 消除“人等机器、机器等人”的空窗。

## 7.4 人工盯 WandB -> 自动监控与告警

改造：
- 统一通知 Hook：`webhook` / `smtp` / `gmail` 三种通道走同一事件协议。
- 定期拉取 WandB run 状态（或从本地日志采样）。
- 规则触发：长时间无 step 增长、loss 为 NaN、进程退出。
- 自动执行恢复动作并记录到 `run_state.json`。

收益：
- 人从“盯盘”转为“接收异常摘要”。

---

## 8. MVP 实施计划（建议）

1. 新增 `scripts/run_online_pipeline.py`
- 读取 `pipeline_spec_online.yaml`
- 执行 `prepare_data -> regen(optional) -> train`
- 写 `run_state.json` 和 step summaries

2. 新增 `configs/pipeline_online.example.yaml`
- 提供标准任务模板

3. 在训练启动前生成 `resolved_config.json`
- 将最终参数固化，便于审计和复现

4. 增加基础监控器
- 先用日志+进程存活+WandB run alive 三个信号

5. 增加最小恢复策略
- 仅实现 2 类：重试当前 step、从最新 checkpoint resume

---

## 9. 验收标准（以“替代人工”为核心）

如果以下条件满足，说明这轮 Agent 化改造有效：

1. 一次 Online 训练从启动到结束，人工只需提供 spec 文件并执行 1 条命令。
2. regen 完成后无需人工干预，训练能自动启动。
3. 配置变更不再通过手改脚本完成，全部来自 spec。
4. 训练中断后可自动执行至少一次 resume。
5. 最终输出统一实验报告，包含 run 参数、关键指标、失败与恢复记录。

---

## 10. 与仓库现有能力的映射

可直接复用：
- 数据准备：`scripts/prepare_data.py`
- 数据重生成：`scripts/regenerate_train_data.py`
- Online 训练：`scripts/train_eagle3.py`
- 跟踪系统：`specforge/tracker.py` + `--report-to`

需要新增：
- Orchestrator（任务编排）
- 状态持久化（`run_state.json`）
- 结构化 step summary
- 自动恢复策略

---

## 11. 下一步实施建议

建议下一步直接落代码，不再停留在研究：

1. 先实现 `run_online_pipeline.py`（最小可运行编排器）。
2. 再补 `pipeline_spec_online.yaml` 的 schema 校验。
3. 最后接 WandB 监控与自动恢复策略。

这样可以最快把“人工串流程”变成“Agent 一键执行流程”。
