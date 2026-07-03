# 多智能体流水线式协作

日期: 2026-07-01
状态: 文档综述(基于 [dual-cli-pipeline 设计](./docs/2026-06-30-dual-cli-pipeline-design.md) 与 [试跑计划](./docs/2026-06-30-dual-cli-pipeline-trial-run-plan.md),整合 trial-001 / trial-002 试跑校验产出)
对应实现: `pipeline.py`(MVP)

## 1. 工具作用

**dual-cli-pipeline** 是一个半自动编排框架,驱动 **Claude Code CLI** 与 **Codex CLI** 两个智能体以流水线方式协作完成软件开发。

核心定位:
- **不是全自动**——关键决策(计划是否 OK、是否达标、停滞升级)由用户确认,脚本不替用户拍板。
- **不是单 agent**——两个 CLI 分工:Claude Code 当"大脑"(需求理解、方案设计、代码审查),Codex 当"手"(串行实现、按 review 修复、自测门内自修)。
- **不是 prompt 链**——阶段间靠**文档 artifact**(plan/review/fix)作为契约,而非上下文传递。每个 CLI 在自己阶段独立起会话,产物落盘,下一个 CLI 读盘续跑。

解决的实际问题:
1. 单 agent 干完整活容易"既当裁判又当运动员",审查时对自己代码宽容。
2. 纯自动 agent 链缺乏收敛监测,容易死循环或徒劳多轮。
3. 上下文窗口压力大——长会话跨阶段会丢上下文,文档契约避免这个问题。

## 2. 组成部分

### 2.1 流水线阶段

```mermaid
flowchart TD
    S1["1. 设计阶段<br/>Claude Code<br/>需求解析 / 方案设计<br/>输出计划文档"]
    S2["2. 实现阶段<br/>Codex<br/>串行按计划文档实现<br/>代码 + 测试"]
    S3A["3a. 自测门<br/>Codex 自跑测试<br/>红则自修回 3a<br/>自修失败上限 N,超限→送 3b 判因"]
    S3B["3b. 审查阶段<br/>Claude Code<br/>代码 vs 计划 / 主观评审<br/>输出结构化 review"]
    S4["4. 修复阶段<br/>Codex<br/>按 review 文档修复<br/>代码 + 测试"]
    S5{"5. 达标判定<br/>STATUS: PASS / FAIL"}

    S1 --> S2 --> S3A --> S3B --> S4 --> S5
    S5 -- "是 (PASS)" --> DONE(["结束"])
    S5 -- "否 (FAIL)" --> S3A
```

| 阶段 | 执行者 | 输入 | 输出 |
|------|--------|------|------|
| 1. 设计 | Claude Code | 设计文档(定位/模块拆分/技术选型) | `NNN-plan.md`(有序实现步骤,ground truth) |
| 2. 实现 | Codex | plan 文档 | 代码 + 测试 |
| 3a. 自测门 | Codex | 阶段2产出 | 自修日志;超限则卡点交棒 |
| 3b. 审查 | Claude Code | plan + 代码 + 上轮 fix(若有) | `NNN-review.md`(结构化,STATUS + ISSUE 清单) |
| 4. 修复 | Codex | plan + 上轮 review | 改后代码 + `NNN-fix.md` |
| 5. 达标判定 | 脚本 | review 的 STATUS 行 | 结束 / 回 3a |

### 2.2 关键工件(artifact)

每次循环产出文档,作为两个 CLI 间的契约:

```
agent-pipeline/
├── pipeline/
│   ├── artifacts/
│   │   ├── 001-plan.md       # 阶段1产出: 计划文档(ground truth, 后续只读基准)
│   │   ├── 002-review.md     # 阶段3b产出: 结构化 review(含收敛趋势)
│   │   ├── 003-fix.md        # 阶段4产出: 修复说明
│   │   ├── 004-review.md     # 第二轮 review
│   │   └── ...
│   ├── state.json            # 流水线状态
│   └── last-{claude,codex}.log  # 每次调用的完整 stdout/stderr
├── prompts/                  # 各阶段的 prompt 模板
├── config.yaml               # pipeline 配置
└── pipeline.py               # 编排工具
```

artifact 的具体格式规范(plan/review/fix 的字段约定、ISSUE id 规则、plan 漂移处理等)见 [设计文档](./docs/2026-06-30-dual-cli-pipeline-design.md)。

### 2.3 编排工具(pipeline.py)

Python CLI 工具,职责:
1. 驱动阶段切换——调用对应 CLI,传入当前阶段 prompt + 上一阶段产出文档路径
2. 人工确认点——每阶段完成后暂停,等待用户确认继续/编辑/中止
3. 状态持久化——`state.json` 记录状态,中断后 `--resume` 续跑
4. 达标判定——解析 review 的 `STATUS: PASS/FAIL` 决定终止或回 3a
5. 收敛监测——解析每轮 review 的 BLOCKER+MAJOR 数与类别,在停滞时升级处理

调用细节(plain/tmux 双路 runner、CLI 命令参数、prompt 模板渲染、shell 注入安全、防静默零产出)见 [pipeline.py 实现架构分析](./docs/2026-07-02-pipeline-implementation-architecture.md)。

## 3. 使用方式

### 3.1 安装与配置

1. 确保已安装 Claude Code CLI(`claude`)和 Codex CLI(`codex`)并配置好认证
2. 复制 `config.example.yaml` 为 `config.yaml`,按需修改:

```yaml
project:
  name: <你的项目名>
  root: <项目根目录绝对路径>

clis:
  claude:
    cmd: "claude"
  codex:
    cmd: "codex"

# 相对 project.root
artifacts_dir: pipeline/artifacts
state_file: pipeline/state.json

max_iterations: 5
pass_marker: "STATUS: PASS"

# observe_mode: agent 输出的观察模式 (plain 默认 / tmux 独立 session 可 attach + Ctrl-C)
observe_mode: plain

# 阶段 3a 自测门(MVP 关)
self_test_gate: false
selftest_max_fixes: 3

# 收敛监测(MVP 关;字段保留为后续开接口)
stall_detection:
  same_category_repeat: 2
  issue_count_not_decreasing: false
```

3. 准备一份**设计文档**(架构/定位/模块拆分级),作为阶段 1 的输入。注意:不接收零散需求文本,设计文档要给出需求定位、模块拆分、技术选型,Claude Code 只做"转译成有序实现步骤"

### 3.2 启动流水线

两种启动方式:`run-pipeline.sh`(推荐,封装了 tmux 环境清理)或直接调 `pipeline.py`。

```bash
# 方式一: run-pipeline.sh (推荐)
# 参数透传给 pipeline.py, 自动清理从 root tmux 继承的 TMUX_* 环境变量, 跑完复原
./run-pipeline.sh config.yaml --design-doc path/to/design.md          # 新跑
./run-pipeline.sh config.yaml --resume                                # 续跑
./run-pipeline.sh config.yaml --design-doc path/to/design.md --dry-run  # 只打印不执行 CLI

# 方式二: 直接调 pipeline.py
python pipeline.py config.yaml --design-doc path/to/design.md
python pipeline.py config.yaml --resume
python pipeline.py config.yaml --design-doc path/to/design.md --dry-run
```

> 注:`run-pipeline.sh` 解决的场景——当通过 root tmux session 接入时,普通用户 shell 会继承 `TMUX`/`TMUX_SOCKET` 等变量指向 root 的 socket,pipeline 起子 session 时会 `Permission denied`。该 wrapper 在子 shell 里临时 unset 这些变量,跑完 `trap EXIT` 自动复原,不影响当前 shell 的 tmux 环境。直接调 `pipeline.py` 时不触发这个问题,但若在 root tmux 环境下用 `observe_mode: tmux` 仍会踩到,建议优先用 wrapper。

环境变量:
- `PIPELINE_AUTO_CONFIRM`: 设为首字符(c/e/a 之一),跳过人工确认点(用于自动化测试)

### 3.3 人工确认点

需要用户介入的点:
- **阶段 1 后**:计划文档是否值得让 Codex 实现(步骤是否可执行、自测验收点是否可判定、需求与验收点是否语义一致)。可选 `c`(继续进阶段 2)/`e`(中止让我编辑 plan)/`a`(中止)
- **阶段 3a 卡点时**:Codex 自修失败交棒,review 判根因后,人工确认下一步(回阶段 1 修 plan / 继续 fix)
- **阶段 3b 后每轮**:review 是否通过(再修一轮 / 够了 / 回阶段 1 重设计)。可选 `c`(进阶段 4 fix)/`a`(中止)
- **收敛停滞时**:同类问题重复或问题数不降,人工决定下一步
- **兜底**:达到 `max_iterations` 强制停,人工介入

其余(3a 自测门内 Codex 自修在限额内、调 Codex 实现、跑测试、读 review 文档、解析收敛趋势)脚本自动做。

### 3.4 编辑 plan 后续跑

阶段 1 完成选 `e` 时,脚本暂停并提示「请手动编辑 plan 后, 用 --resume 继续」。这是用户干预 plan 内容的官方路径——plan 是 ground truth,但人工判断可推翻。编辑后用 `--resume` 从阶段 2 继续。

## 4. 注意事项

### 4.1 plan 内部一致性(试跑教训)

plan 的「需求」与「自测验收点」必须语义一致——不能需求说 A、验收点断言非 A。设计阶段须自检:对同一输入,所有验收点的期望值不得互相矛盾。

trial-002 埋了「需求说『值为整数转 int』,但步骤 3 `test_value_type` 断言 `{"a": "1"}`(字符串)」的矛盾坑,Codex 必然踩一个 test,fix 改一边必踩另一边,徒劳多轮。这类矛盾应在阶段 1 自检排除。

### 4.2 步骤间依赖

实现步骤是给 Codex 的**实现顺序指引**,不是逐步验收边界。自测验收点针对**整个阶段 2 产出**(全部步骤完成后一起跑 3a),不针对单步。理由:步骤间常有依赖(如「步骤 1 写测试、步骤 2 实现函数」,步骤 1 单独跑必红),逐步验收会卡死。脚本化时阶段 2 作为整体跑完再进 3a。

### 4.3 自修次数自报不可核实

fix 文档的自测结果里 Codex 自报「自修次数」,仅作参考,脚本无法核实。设计上接受这个限制——3a 自修次数只用于诊断,不用于达标判定。

### 4.4 埋坑触发 3a 不可靠

不要靠「故意在 plan 里漏边界用例」来验证 3a——Codex 常一次写对,3a 不红。要真正验证 3a,需在 plan 里写自相矛盾的需求(如 trial-002 坑 2)或语法错。3a 的真正价值是**拦崩代码**(语法错/导入错/跑不过),不是拦漏实现。

### 4.5 已知盲区(MVP 未实现)

| 盲区 | 说明 | 补跑方式 |
|------|------|---------|
| 3a 自测门 | MVP 关闭(`self_test_gate: false`) | 第二里程碑开 |
| 收敛停滞真实触发 | T3/T4 未实测(Codex 太会修,构造不出停滞) | trial-003 构造「fix 改不动」场景 |
| 达标柔性出口(R5) | STATUS=PASS 时用户可选继续打磨 | 第一版可不实现,默认 PASS 即结束 |
| 全自动无人介入 | 设计明确不包含 | 不在路线内 |

## 5. Roadmap

状态图例: ✅ 已实现 | ⏳ 计划中 | ❌ 不在路线内

### 5.1 近期里程碑

| 状态 | 项 | 说明 |
|------|----|------|
| ⏳ | 开 3a 自测门 | 实现 `self_test_gate: true` 路径,Codex 实现完先自跑测试,红了自修,达 `selftest_max_fixes` 仍红送 3b 判根因 |
| ⏳ | 开收敛监测 | 实现 `stall_detection` 的两条触发路径(同类问题重复 / 问题数不降),升级动作走人工确认点 |
| ⏳ | 达标的柔性出口(R5) | STATUS=PASS 时允许用户选「再 review 一轮」(不消耗 `max_iterations` 配额) |
| ✅ | tmux 实时可观测性 | 结合 tmux 等终端多路复用工具,实现 agent 任务执行实时可观测性。见 [设计文档](./docs/superpowers/specs/2026-07-01-tmux-observability-design.md) |
| ⏳ | token / API 用量统计 | 记录 pipeline 运行期间 agent 使用的 token 总量与 API 请求次数,按阶段记录并支持汇总 |

### 5.2 后续打磨

| 状态 | 项 | 说明 |
|------|----|------|
| ⏳ | plan 漂移自动同步 | 加 `pipeline sync-plan` 子命令,读 fix 文档的「触及 plan 漂移: 是」条目,生成 plan 更新建议供用户审核 |
| ⏳ | ISSUE 跨轮关联自动化 | 在 review 模板里加「原 ISSUE 状态表」字段,脚本解析后给下一轮 review 做对照 |
| ⏳ | 多项目并行 | 加 `pipeline run-all <config-dir>` 批量驱动多个项目的流水线 |
| ⏳ | Web UI 观察台 | 把 `state.json` + `issue_history` 渲染成时序图,直观看到收敛趋势与停滞点 |
| ✅ | 阶段 2/4 分步驱动 codex | 按 plan 的「### 步骤 N」分多次调 codex,每次只传当前步骤的 prompt,杜绝「上下文耗尽 → 静默零产出 → review 死循环」 |

### 5.3 不在路线内

| 状态 | 项 | 说明 |
|------|----|------|
| ❌ | 并行 subagent | 设计明确串行,不并行。理由:串行按 plan 步骤实现,plan 文档需明确「有序的实现步骤」而非「可并行的独立任务块」。并行会引入 subagent 间状态共享与冲突,复杂度收益不划算 |
| ❌ | 全自动无人介入 | 半自动是核心定位,关键决策(计划是否 OK、是否达标、停滞升级)由用户确认。全自动会让流水线在计划缺陷时死循环或产出错误代码 |

## 6. 开发

### 6.1 跑测试

```bash
pip install -r requirements-dev.txt   # 含 pytest
pytest tests/                          # 跑全部测试
pytest tests/test_pipeline.py -k tmux_session   # 只跑某单元
```

### 6.2 手工验收清单(真实环境)

1. `observe_mode: plain` 跑 `--dry-run` → 行为与改造前一致
2. `observe_mode: plain` 真跑 codex → 前台看到流式输出,日志落全,退出码正确
3. `observe_mode: tmux` 真跑 codex → `tmux ls` 看到 `pipe-codex`,attach 后看到 agent 输出
4. tmux 模式下 agent pane 按 Ctrl-C → pipeline 报中止,state 留在 `2-impl`
5. tmux 模式下 pipeline 前台按 q → 同上效果
6. tmux 模式下 agent 正常完成 → session 自动 kill,无残留
7. `observe_mode: tmux` 但无 tmux → 报错提示安装,不崩
8. 超时(把 timeout 改成 5s 跑 codex)→ 报超时,session 被清理

## 7. 相关文档

- [dual-cli-pipeline 设计](./docs/2026-06-30-dual-cli-pipeline-design.md) — 完整设计 v2(经试跑校验,trial-001 / trial-002 已跑通,修订项 R1-R9 已并入)
- [dual-cli-pipeline 试跑计划](./docs/2026-06-30-dual-cli-pipeline-trial-run-plan.md) — 手动跑通实施计划,沉淀文档模板并反哺设计
- [pipeline.py 实现架构分析](./docs/2026-07-02-pipeline-implementation-architecture.md) — 实际代码结构、调用栈、plain/tmux 双路 runner、InterruptHub、shell 注入安全、防静默零产出
- `runs/trial-001/`、`runs/trial-002/` — 试跑记录(含 friction-log 与 artifact 实例)
- `pipeline.py` — MVP 编排工具实现
- `prompts/` — 各阶段 prompt 模板
- `config.example.yaml` — 配置样例
