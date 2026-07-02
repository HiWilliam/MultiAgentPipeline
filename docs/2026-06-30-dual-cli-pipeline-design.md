# dual-cli-pipeline 设计

日期: 2026-06-30
状态: 设计 v2(经试跑校验,trial-001 / trial-002 已跑通,修订项 R1-R9 已并入)
配套项目: hos-manager(见 `/wuhao/workspace/hos_manager/docs/superpowers/specs/2026-06-30-hos-manager-design.md`)
试跑记录: `runs/trial-001/`、`runs/trial-002/`(含 friction-log)

## 1. 定位

半自动编排框架,驱动 Claude Code CLI(设计/review)与 Codex CLI(实现/修复)协作开发。
仓库: `/wuhao/workspace/agent-pipeline`
技术栈: shell + python(不用 Go)。

## 2. 流水线阶段

```
┌─────────────┐    ┌──────────┐    ┌───────────────┐    ┌──────────────┐    ┌──────────┐
│ 1. 设计阶段  │ →  │ 2. 实现阶段│ →  │ 3a. 自测门     │ →  │ 3b. 审查阶段   │ →  │ 4. 修复阶段│
│ Claude Code │    │ Codex     │    │ Codex 自跑测试 │    │ Claude Code  │    │ Codex     │
│ 需求解析     │    │ 串行按计划 │    │ 红则自修回3a   │    │ 代码 vs 计划   │    │ 按 review │
│ 方案设计     │    │ 文档实现   │    │ 自修失败上限N  │    │ 主观评审      │    │ 文档修复   │
│ 输出计划文档  │    │ 代码+测试  │    │ 超限→送3b判因 │    │ 输出结构化review│   │ 代码+测试  │
└─────────────┘    └──────────┘    └───────────────┘    └──────────────┘    └──────────┘
                                                                       │
                                              ┌────────────────────────┘
                                              ↓
                                         ┌──────────┐
                                         │ 5. 达标?  │
                                         │  判定    │
                                         │ 否→回3a   │
                                         │ 是→结束  │
                                         └──────────┘
```

阶段拆分说明:
- **阶段 3a 自测门(Codex)**: 实现完先自己跑测试,红了读测试输出自修、再跑,直到全绿。
  - **3a 的收益定位(R2)**: 核心是**拦住崩代码**(语法错/导入错/跑不过的代码)不进 3b,避免浪费 Claude Code review 预算——而非「补 Codex 的漏实现」(靠漏实现触发 3a 不可靠,Codex 常一次写对)。
  - **3a 自修失败上限(R7)**: 设 `selftest_max_fixes=N`(默认 3)。Codex 自修 N 次仍红,标记 `selftest_stall`,**直接送 3b 让 Claude Code 判根因**——因为不可自修的红通常是设计层问题(需求矛盾/计划错误),不是代码 bug,Codex 在 a 阶段死循环无意义。Codex 须在自测日志中报告:每次红在哪、自修了几次、为何判定不可自修。
  - 测试是机器可判定的,不消耗 Claude Code 预算(仅当超限送 3b 时才消耗)。
- **阶段 3b 审查(Claude Code)**: 3a 全绿**或 3a 自修失败交棒**后进。做主观评审:代码 vs 计划一致性、设计合理性、边界、命名等,输出结构化 review。若 3a 是卡点交棒的,review 须先判根因(代码 bug / 计划矛盾 / 需求歧义)。
- 拆分的理由:测试失败(确定性)与 review 不通过(主观)修复路径不同,混在一起会让 Claude Code 浪费预算在明显崩的代码上。

角色分工:
- **Claude Code CLI**: 大脑——需求理解、方案设计、代码审查、判定是否达标、3a 卡点时判根因
- **Codex CLI**: 手——按文档串行实现、按 review 修复、自测门内自修、3a 卡点时自报不可自修原因

**计划文档作为 ground truth**: fix 与 review 阶段都以 001-plan.md 为基准。review 必须校验「代码 vs 计划」一致性,而不仅是「代码 vs 测试」;fix 须在计划范围内修复(详见 §4.4 的 plan 漂移机制 R8/R9)。

## 3. 实现方式: 串行

Codex 串行按计划文档逐步实现,不并行 subagent。
计划文档需明确「有序的实现步骤」,而非「可并行的独立任务块」。

## 4. 关键工件(artifact)

每次循环产出文档,作为两个 CLI 间的契约。

```
agent-pipeline/
├── artifacts/
│   ├── 001-plan.md           # 阶段1产出: 计划文档(ground truth, 后续只读基准)
│   ├── 002-review.md         # 阶段3b产出: 结构化 review(含收敛趋势)
│   ├── 003-fix.md            # 阶段4产出: 修复说明
│   ├── 004-review.md         # 第二轮 review
│   └── ...
├── state.json                # 流水线状态(见 §5)
└── config.yaml               # pipeline 配置
```

### 4.1 计划文档(001-plan.md)规范

计划文档是后续所有阶段的只读基准,必须包含**有序的实现步骤**,每个步骤含:

```markdown
## 实现步骤

### 步骤 1: <目标简述>
- 目标: <这一步要达成什么>
- 涉及文件: <path1>, <path2>
- 自测验收点: <3a 自测门如何判断这一步完成;通常是某测试用例名或测试文件>

### 步骤 2: ...
```

粒度规范:
- 每个步骤应能在一次 Codex 实现内完成,不跨多个不相关模块。
- 步骤数建议与 `max_iterations` 同量级或更少——每轮 fix 至少能消化一个步骤级别的问题。
- 自测验收点必须可机器判定(测试名 / 测试文件 / 命令退出码),不写「代码看起来对了」。

**步骤的边界(R6)**: 实现步骤是给 Codex 的**实现顺序指引**,不是逐步验收边界。自测验收点针对**整个阶段 2 产出**(全部步骤完成后一起跑 3a),不针对单步。理由:步骤间常有依赖(如「步骤1写测试、步骤2实现函数」,步骤1单独跑必红),逐步验收会卡死。脚本化时阶段 2 作为整体跑完再进 3a。

**plan 内部一致性(试跑教训)**: plan 的「需求」与「自测验收点」必须语义一致——不能需求说 A、验收点断言非 A。设计阶段须自检:对同一输入,所有验收点的期望值不得互相矛盾。矛盾会导致 3a 出现「不可自修的红」(见 §2 R7),徒增一轮。

### 4.2 review 文档规范(结构化)

review 必须输出结构化字段,供 pipeline 脚本解析与收敛监测:

```markdown
STATUS: PASS | FAIL

## 收敛趋势
- 本轮问题数(BLOCKER+MAJOR): <N>
- 上轮问题数(BLOCKER+MAJOR): <M 或 N/A>
- 趋势: 下降 | 持平 | 上升 | none(首轮无基线)

## 问题清单
### [BLOCKER-1] <问题标题>
- 类别: 测试失败 | 设计偏离计划 | 边界缺失 | 命名 | 安全 | 冗余 | 可读性 | 其他
- 位置: <file:line>
- 期望: <应是什么样的>
- 实际: <现在是什么样的>
- 根因: <代码bug | 计划矛盾 | 需求歧义 | 其他>  (3a卡点交棒时必填,正常review可选)

### [MAJOR-2] ...

### [MINOR-3] ...
```

字段约定:
- `STATUS`: PASS 表示达标可结束;FAIL 表示需进修复阶段。
- **ISSUE id(R1)**: 每个 ISSUE 必须有稳定 id(`BLOCKER-1` / `MAJOR-2` / `MINOR-3`,按 severity 内序号)。fix 文档用 id 引用,下一轮 review 用 id 关联「原 ISSUE 是否解决」+「是否同类重复」。脚本用 `### \[(BLOCKER|MAJOR|MINOR)-(\d+)\]` 正则解析。
- severity 三档: BLOCKER(必须修才能过)、MAJOR(应修)、MINOR(可修可不修,pipeline 不计入收敛)。
- **类别(R1 扩展)**: 枚举增加 `冗余` / `可读性`,与「设计偏离计划」区分。类别用于 §5.2「同类问题重复」判定,issue_history 须记录每轮的 categories 集合。
- **根因字段**: 3a 卡点交棒进 3b 时必填(判代码 bug / 计划矛盾 / 需求歧义);正常 review 可选。用于决定升级路径(计划矛盾 → 走 §4.4 plan 漂移或回阶段1;代码 bug → 走 fix)。
- 收敛趋势: 首轮标 `none`(无基线,不触发停滞判定);后续轮由脚本对比 history 计算。

### 4.3 fix 文档规范

fix 文档须包含:

```markdown
## 修复内容
### [BLOCKER-1→修] <处置>
- 改动: <file:line, 做了什么>
- 是否触及 plan 漂移: 是 | 否  (R9,见下)
- 若漂移: <说明改了 plan 范围内的什么代码产物,为何>

### [MAJOR-2→拒] <处置>
- 拒绝理由: <为什么不修>

## 自测结果
- pytest 输出 / 自修次数
```

**逐条回应(R4)**: fix 必须逐条引用 review 的 ISSUE id(`[BLOCKER-1→修|拒]`),标注处置(修/拒绝+理由)。不得偷懒只改一半。下一轮 review 据此核查每个 BLOCKER/MAJOR id 是否被回应。

**修复边界(R8)**: fix 允许改 plan 范围内的**代码产物**(测试断言、实现代码),但**禁止改 plan 的需求语义**(那是回阶段1的事)。即 fix 可改 `test_value_type` 的断言值,但不能改「值为整数转 int」这条需求本身。理由(trial-002):Codex 能识别「计划矛盾落在代码侧」时,改测试断言一轮即可收敛,严守「plan 只读」反而徒劳一轮。但需求语义若可随意改会让 plan 失去基准作用,故划界。

**plan 漂移点标注(R9)**: fix 若改了 plan 范围内的代码产物(如修正了 plan 里写错的测试断言),须在 fix 文档显式标注「触及 plan 漂移: 是」并说明改了什么。下一轮 review 据此核查:
- 漂移是否合理(是否真的修正了 plan 内部错误,而非偏离需求)
- plan 文档本身是否需同步更新(见 §4.4)

**自修次数自报**: fix 文档的自测结果里 Codex 自报「自修次数」,仅作参考,脚本无法核实。

### 4.4 artifact 流向(契约方向)

```
001-plan ──(只读基准)──┐
                       ├──→ 3b review: 校验「代码 vs plan」一致性(含 plan 漂移核查)
                       └──→ 4 fix:    在 plan 范围内修复(允许改代码产物,禁止改需求语义 R8)

002/004-review ──(工作指令)──→ 4 fix: 按 ISSUE id 清单逐条修

003-fix ──(变更说明)──→ 下一轮 3b review: 按 id 核查 ISSUE 是否真消除 + 有无新增 + plan 漂移是否合理
```

契约要点:
- plan → 后续: **只读基准**,但承认「plan 内部可能有错」。fix 不改 plan 文档本身;若 fix 改了 plan 范围内的代码产物(如测试断言),产生 **plan 漂移**,须在 fix 文档标注(R9),review 核查合理性。
- **plan 漂移的处理(R8/R9)**:
  - 漂移落在「代码产物」(测试断言等)且修正了 plan 内部错误 → fix 可修,标注漂移,review 核查后接受。plan 文档可由用户在阶段1后手动同步(不在 fix 阶段自动改 plan)。
  - 漂移触及「需求语义」→ fix 不得修,应触发 §5.2「回阶段1 重设计」(说明计划本身有缺陷)。
- review → fix: 是工作指令,fix 必须逐条回应每个 BLOCKER/MAJOR 的 id。
- fix → review: 是变更说明,review 据此判断「问题是否真消除」+「有无新增问题」+「plan 漂移是否合理」。

## 5. 编排工具(半自动)

一个 python CLI 工具 `pipeline`,职责:

1. **驱动阶段切换**: 调用对应 CLI(Claude Code / Codex),传入当前阶段 prompt + 上一阶段产出的文档路径
2. **人工确认点**: 每个阶段完成后暂停,打印「阶段 X 完成,产出在 <path>。回车继续 / e 编辑 / a 中止」。关键决策(计划是否 OK、是否达标)由用户确认
3. **状态持久化**: `state.json` 记录状态(见下),中断后能续跑
4. **达标判定**: Claude Code 在 review 阶段输出 `STATUS: PASS/FAIL`,脚本读该字段决定终止或回阶段 3a(柔性出口见 §5.3)
5. **收敛监测**: 解析每轮 review 的「收敛趋势」与问题类别,在停滞时升级处理(见 5.2)

### 5.1 state.json 字段(形式化)

```json
{
  "stage": "3b-review",
  "iteration": 2,
  "plan_artifact": "artifacts/001-plan.md",
  "last_review": "artifacts/002-review.md",
  "open_issues": {
    "blocker": 1,
    "major": 2,
    "minor": 3
  },
  "issue_history": [
    {"iteration": 1, "blocker": 3, "major": 4, "minor": 1, "categories": ["测试失败", "设计偏离计划"]},
    {"iteration": 2, "blocker": 1, "major": 2, "minor": 3, "categories": ["设计偏离计划"]}
  ],
  "convergence_trend": "下降",
  "stall_flags": {
    "same_category_repeat": 0,
    "issue_count_not_decreasing": false
  },
  "selftest_stall": {
    "occurred": false,
    "iteration": null,
    "auto_fix_attempts": 0,
    "codex_report": null
  }
}
```

字段说明:
- `stage`: 当前阶段(1-design / 2-impl / 3a-selftest / 3b-review / 4-fix / done)。
- `iteration`: 第几轮 review-fix 循环(从 1 起)。
- `open_issues`: 当前轮 review 的剩余问题数(按 severity)。
- `issue_history`: 每轮的问题数快照 + **categories 集合**(R1),用于判断收敛趋势与同类重复。
- `convergence_trend`: `none`(首轮无基线)/ 下降 / 持平 / 上升,由脚本对比 history 计算。首轮为 `none`,不触发停滞判定。
- `stall_flags`: 停滞标记,触发升级处理(§5.2)。
- **`selftest_stall`(R7)**: 3a 自修失败记录。`occurred` 是否发生、`iteration` 哪轮、`auto_fix_attempts` 自修次数、`codex_report` Codex 的卡点说明(为何不可自修)。发生时 3a 直接送 3b,review 须读此字段判根因。

### 5.2 收敛监测与升级处理

`max_iterations` 是硬上限,但在到达上限前应主动识别停滞并升级:

| 停滞信号 | 触发条件 | 升级动作 |
|---------|---------|---------|
| 同类问题重复 | 连续 2 轮 review 出现同 `类别` 的 BLOCKER | 回阶段 1 重设计(说明计划本身有缺陷) |
| 问题数不降 | 本轮 BLOCKER+MAJOR 数 ≥ 上轮(且非首轮,且非 3a 卡点交棒轮) | 暂停,人工介入决定是否调整范围 |
| 3a 自修失败(R7) | 3a 自修达 `selftest_max_fixes` 次仍红 | 送 3b 判根因;若 review 判为「计划矛盾」直接回阶段1 |
| 硬上限 | `iteration > max_iterations` | 强制停,人工介入 |

升级动作统一走「人工确认点」: 脚本暂停并打印停滞原因 + 建议动作,由用户决定回阶段 1 / 继续硬撑 / 中止。

注:「问题数不降」判定须排除「3a 卡点交棒轮」——该轮红是设计问题非实现问题,问题数对比无意义(参考 trial-002 轮1)。「同类问题重复」按 issue_history 的 categories 集合交集判断。

### 5.3 达标判定的柔性出口(R5,可选)

`STATUS: PASS` 默认结束流水线。但允许用户在确认点选择「再 review 一轮」(打磨用,不消耗 `max_iterations` 配额)。

适用场景: 代码已达标,但用户想再让 Claude Code 找找潜在改进点(MINOR 级)。

第一版可不实现,默认 PASS 即结束。后续按需加。

## 6. pipeline 配置(config.yaml)

```yaml
project:
  name: hos-manager
  root: /wuhao/workspace/hos_manager

clis:
  claude:
    cmd: "claude"
  codex:
    cmd: "codex"

artifacts_dir: pipeline/artifacts
max_iterations: 5           # 最多循环 5 轮,防死循环

pass_marker: "STATUS: PASS"

# 自测门(阶段 3a): Codex 实现完先自跑测试,红了自修不惊动 Claude Code
self_test_gate: true        # false 则退化为原设计(直接进 3b review)
selftest_max_fixes: 3       # R7: 3a 自修上限,超限送 3b 判根因

# 收敛监测(§5.2)
stall_detection:
  same_category_repeat: 2   # 连续 N 轮同类 BLOCKER → 回阶段 1 重设计
  issue_count_not_decreasing: true  # 问题数不降 → 暂停人工介入
```

## 7. 半自动的人工确认点

需要用户介入的点:
- 阶段1后: 计划文档是否值得让 Codex 实现(步骤是否可执行、自测验收点是否可判定、需求与验收点是否语义一致)
- 阶段3a 卡点时(R7): Codex 自修失败交棒,review 判根因后,人工确认下一步(回阶段1 修 plan / 继续 fix)
- 阶段3b后每轮: review 是否通过(再修一轮 / 够了 / 回阶段 1 重设计)
- 收敛停滞时(§5.2): 同类问题重复或问题数不降,人工决定下一步
- 兜底: 达到 `max_iterations` 强制停,人工介入

其余(3a 自测门内 Codex 自修在限额内、调 Codex 实现、跑测试、读 review 文档、解析收敛趋势)脚本自动做。

## 8. 第一版范围

包含: 串行实现 + 半自动人工确认 + 状态续跑 + 达标判定 + 3a 自测门(含自修失败上限 R7) + 结构化 review(含 ISSUE id R1) + 收敛监测升级 + plan 漂移机制(R8/R9)。
不包含: 并行 subagent、全自动无人介入、达标柔性出口(R5,可选)。

注: 3a 自测门、收敛监测、plan 漂移机制都带 config 开关(`self_test_gate` / `selftest_max_fixes` / `stall_detection`),可先关闭退化到最小可用形态,跑顺后再开。R8/R9 的「修复边界」与「漂移标注」是 prompt 层约束(写进 Codex 的 fix prompt),无独立开关,默认启用。

## 9. 开发顺序

1. ~~先手动跑通 pipeline 一次(纯手工在两个 CLI 间切),沉淀第一阶段文档模板~~ **已完成(trial-001 / trial-002)**,产出 9 条修订项 R1-R9 已并入本设计。已知盲区 T3/T4(收敛停滞真实触发)未实测,判定逻辑已手算验证,留待真实使用中暴露
2. 实现 dual-cli-pipeline 编排工具(python,先能用),用它驱动后续开发
3. 用 pipeline 开发 hos-manager 的 config + proc 模块(核心逻辑,无 TUI,先有 CLI 子命令能跑)
4. 加 TUI 层(bubbletea)
5. 打磨 pipeline(根据开发体验优化人工确认点、artifact 格式)
