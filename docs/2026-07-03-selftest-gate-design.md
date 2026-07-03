# 5.1 里程碑 — 开 3a 自测门 设计文档

日期: 2026-07-03
状态: 设计初稿
对应里程碑: [README §5.1](../README.md) "开 3a 自测门"
依赖文档:
- [dual-cli-pipeline 设计](./2026-06-30-dual-cli-pipeline-design.md) §2 / §5.1 / §5.2
- [pipeline.py 实现架构分析](./2026-07-02-pipeline-implementation-architecture.md)
- [README](../README.md) §4.3 / §4.4 / §4.5

## 1. 目标与定位

### 1.1 一句话目标
把 config 里 `self_test_gate: false` 翻成 `true` 的路径打通: 阶段 2 (impl) 全部步骤实现完后, **Codex 自己先跑测试**, 红了读输出自修、再跑, 直到全绿或达 `selftest_max_fixes` 上限才交棒。

### 1.2 收益定位 (设计 §2 R2, 重要边界)
- ✅ 核心价值: **拦住崩代码** (语法错 / 导入错 / 跑不过的代码) 不进 3b, 省 Claude Code review 预算。
- ❌ 不指望拦「漏实现」——Codex 常一次写对, 靠漏实现触发 3a 不可靠 (参考 [README §4.4](../README.md))。
- 因此**测试编排** (命令、测试文件位置) 由 pipeline 决定, Codex 不自由发挥。

### 1.3 与现状的差距

| 维度 | MVP (现状) | 5.1 里程碑 |
|------|------------|-----------|
| 阶段流转 | `2-impl → 3b-review` (pipeline.py:716) | `2-impl → 3a-selftest → 3b-review` |
| impl prompt 末句 | "不要跑测试 (那是阶段 3a 的事, MVP 暂跳过)" (prompts/02-impl.md:22) | 改为"实现完即可退出, 测试由 3a 调度跑" |
| state.stage | 无 `3a-selftest` | 新增 `3a-selftest` |
| state.selftest_stall | 字段已定义但未填充 | 实际写入 |
| config 开关 | `self_test_gate: false` | `true` 生效; `false` 仍能退化到 MVP |

---

## 2. 阶段状态机

### 2.1 流转图 (带 3a 分支)

```
2-impl (所有步骤跑完)
   │
   ▼
3a-selftest  ──跑测试──► 全绿 ───────────► 3b-review
   │                          ▲
   │ 红了                     │
   ▼                          │
 Codex 自修 (≤ N 次) ─────────┘
   │
   │ 达 selftest_max_fixes 仍红
   ▼
 设置 selftest_stall → 3b-review (卡点交棒)
```

### 2.2 state 新字段 (设计 §5.1 已定义, 本里程碑要落地)

`pipeline/state.json` 增量字段:

```json
{
  "stage": "3a-selftest",
  "selftest_stall": {
    "occurred": false,
    "iteration": null,
    "auto_fix_attempts": 0,
    "codex_report": null
  }
}
```

字段语义 (设计 §5.1):
- `occurred`: 本轮 3a 是否卡点交棒 (达上限仍红)
- `iteration`: 哪轮卡住 (与 `state.iteration` 对齐)
- `auto_fix_attempts`: Codex 自修尝试次数 (Codex 自报, **仅诊断用, 不可核实**, 见 [README §4.3](../README.md))
- `codex_report`: Codex 的卡点说明 (为何不可自修), 交棒时必填, review 须读

### 2.3 stage 取值集合更新
```
1-design / 2-impl / 3a-selftest / 3b-review / 4-fix / done
```

---

## 3. 自测命令的约定

### 3.1 由谁决定测试命令
**pipeline 决定**, 不交给 Codex 自由选。理由:
- Codex 自由选命令容易跑错 (漏 `pytest -q`、跑全量、跑错目录)
- 也容易跑漏 (只跑某个文件掩盖其他失败)

### 3.2 config 增量

```yaml
# 阶段 3a 自测门
self_test_gate: true
selftest_max_fixes: 3

# 测试命令模板 (相对 project.root 执行)
# 支持 {plan_step_files} 占位符: 若 plan 指定每步测试文件, 按步聚合; 否则全量
selftest:
  cmd_template: "pytest -q"        # Go 项目可配 "go test ./..."
  working_dir: "{{project_root}}"
  timeout_sec: 300                 # 单次测试超时, 防 hang
```

### 3.3 plan 协议的扩展 (最小)
plan 模板 prompts/01-design.md 的「自测验收点」字段允许写:
- 单测试用例名 (如 `test_value_type`) → 3a 跑该用例
- 测试文件路径 (如 `tests/test_foo.py`) → 3a 跑该文件
- 留空 → 3a 跑 `cmd_template` 默认全量

**不**引入每步独立测试命令——保持「步骤是顺序指引不是验收边界」的原设计 (设计 §2 R6)。

---

## 4. 阶段 3a 执行流程

### 4.1 伪代码

```python
def stage_selftest(state, cfg, dry_run=False):
    # 进入 3a 前重置本轮 stall 字段
    state["selftest_stall"] = {occurred: False, iteration: state["iteration"],
                               auto_fix_attempts: 0, codex_report: None}
    if not cfg["self_test_gate"]:
        state["stage"] = "3b-review"      # 开关关: 退化为 MVP
        return state, "c"

    test_cmd = render_test_cmd(cfg, state)   # 解析 plan 自测验收点
    for attempt in range(cfg["selftest_max_fixes"] + 1):   # 首跑 + N 次自修
        rc, out = run_test(test_cmd, dry_run=dry_run)
        if rc == 0:
            break                            # 全绿, 进 3b
        # 红: 调 codex 自修
        code, _ = run_codex(cfg, render_selftest_fix_prompt(out, attempt), dry_run=dry_run)
        state["selftest_stall"]["auto_fix_attempts"] = attempt + 1
        if code != 0 and not dry_run:
            return state, "a"                # codex 崩, 中止

    else:
        # 达上限仍红: 卡点交棒
        state["selftest_stall"]["occurred"] = True
        state["selftest_stall"]["codex_report"] = read_codex_selftest_report()
        # 直接送 3b, review 须判根因 (设计 §5.2 R7)

    state["stage"] = "3b-review"
    return state, "c"
```

### 4.2 关键决策点

| 决策 | 选择 | 理由 |
|------|------|------|
| 自修 prompt 怎么写 | 把上次测试 stdout 全量贴给 Codex, 要求"只改代码不改测试断言除非断言本身错" | 与 fix 阶段 R8 边界一致 (prompts/04-fix.md:13) |
| 自修产出校验 | 同 pipeline.py:710 的 mtime 快照机制 | 复用现有"防静默零产出"逻辑 |
| 全绿后是否写文档 | 不写, 直接进 3b | 减少冗余 artifact; 3a 全绿在 state 里有迹可查 |
| 卡点交棒是否写文档 | **写** `NNN-selftest.md` (含失败测试输出摘要 + Codex 自修历史 + codex_report) | review 阶段要读这个文件判根因 |
| dry-run 行为 | 模拟"首次就绿"直接进 3b | 与 pipeline.py:753 的 dry-run 不写真文件策略一致; 同时**不**写 selftest_stall (避免污染 --resume) |

### 4.3 卡点交棒协议 (设计 §2 R7)

`NNN-selftest.md` 模板 (卡点交棒时才产出):

```markdown
# 阶段 3a 自测门 — 卡点交棒

## 自测命令
<实际跑的命令>

## 自修历史
- 尝试 1: <失败测试摘要> → 自修改动: <file:line>
- 尝试 2: ...
- 尝试 N: <仍红>

## Codex 自报不可自修原因 (codex_report)
<为何判定不可自修 — Codex 必填>

## 交棒给 3b
请 review 阶段判根因: 代码bug | 计划矛盾 | 需求歧义
```

review prompt prompts/03-review.md 需补一段: 当 `selftest_stall.occurred=true` 时, **先读 `NNN-selftest.md` 判根因**, 再走常规评审。根因字段在 ISSUE 块里**必填** (prompts/03-review.md:37)。

---

## 5. 与收敛监测的耦合

设计 §5.2 的关键约束:

> 「问题数不降」判定须**排除 3a 卡点交棒轮**——该轮红是设计问题非实现问题, 问题数对比无意义 (参考 trial-002 轮1)。

实现要点:
- `stage_review` 计算 `convergence_trend` 时, 若上一轮 `selftest_stall.occurred=true`, **跳过** `issue_count_not_decreasing` 触发 (pipeline.py:725-731 现有逻辑要加这个 guard)。
- `stall_flags.same_category_repeat` 不受影响——同类问题重复是跨轮的语义比较。

---

## 6. 人工确认点

新增 1 个确认点 (设计 §7):

| 触发 | 选项 | 说明 |
|------|------|------|
| 3a 卡点交棒后, 3b review 判根因完成 | `c` 进阶段 4 fix / `r` 回阶段 1 修 plan / `a` 中止 | review 判「计划矛盾」时默认建议 `r`; 判「代码 bug」时默认建议 `c` |

pipeline 主循环在 `3b-review` 之后、`4-fix` 之前插入此确认点 (仅当 `selftest_stall.occurred=true` 时呈现 `r` 选项)。

---

## 7. prompt 模板改动清单

| 文件 | 改动 |
|------|------|
| prompts/02-impl.md | 末句"不要跑测试...MVP 暂跳过" → "实现完本步骤代码即可退出; 测试由阶段 3a 调度跑, 不要主动跑" |
| prompts/03a-selftest-fix.md (**新增**) | Codex 自修 prompt: 贴上次测试 stdout + 修复边界 R8 + 要求产出"自修说明"片段供 codex_report 用 |
| prompts/03-review.md | 增 `{{selftest_stall_occurred}}` / `{{selftest_artifact}}` 占位符; 当 occurred=true 时优先读 selftest 文档判根因 |
| prompts/04-fix.md | 自测结果段补一句"若来自 3a 交棒轮, 本轮 fix 须让 3a 的测试先转绿再谈 review 的 ISSUE" |

---

## 8. 实施步骤 (建议顺序)

1. **config 协议**: 在 `config.example.yaml` 增 `selftest` 块; `load_config` 做向后兼容 (缺字段时给默认值)。
2. **state 协议**: `init_state` 写入 `selftest_stall` 默认结构; `load_state` 兼容旧 state (缺字段补默认)。
3. **3a 主流程**: 实现 `stage_selftest` + `run_test` + `render_selftest_fix_prompt`; mtime 校验复用 `_codex_produced_changes`。
4. **prompt 模板**: 按 §7 改 4 个文件。
5. **主循环接线**: `main()` 在 `2-impl` 后插 `3a-selftest` 分支; `3b-review` 后加卡点确认点。
6. **review 收敛 guard**: `stage_review` 跳过 3a 交棒轮的"问题数不降"判定。
7. **测试**:
   - 单测: `stage_selftest` 在 dry-run 下首次绿 / 首次红自修后绿 / 达上限仍红三条路径
   - 集成: 构造 plan 写自相矛盾验收点 (参考 trial-002 坑 2) 跑端到端, 验证卡点交棒 → review 判根因 → 人工确认 `r` 回阶段 1
8. **文档**: 更新 [README §4.5](../README.md) "已知盲区"表中 3a 行从"MVP 关闭"改为"已实现"; §5.1 状态从 ⏳ 改 ✅。

---

## 9. 风险与边界

| 风险 | 缓解 |
|------|------|
| Codex 自修时偷偷改测试断言"骗绿" | selftest-fix prompt 明确禁止改断言 (除非断言本身与 plan 矛盾, 且必须标 R9 漂移); 3b review 抽查 |
| `auto_fix_attempts` 自报不可核实 ([README §4.3](../README.md)) | 接受限制; 3a 次数只用于诊断, **不**用于达标判定 |
| 测试 hang 导致 3a 卡死 | `selftest.timeout_sec` 兜底, 超时记为"红" |
| 旧 state.json 无 selftest_stall 字段 | `load_state` 补默认, 不报错 |
| `self_test_gate: false` 退化路径 | `stage_selftest` 开头判断, 直接 `stage = "3b-review"` 返回, 与 MVP 行为逐字一致 |

---

## 10. 不在本里程碑内

- 收敛监测的真实触发 ([README §4.5](../README.md) 第二行, 属 5.1 另一项"开收敛监测")
- 达标柔性出口 R5 ([README §5.1](../README.md) 第三项)
- token / API 用量统计 ([README §5.1](../README.md) 第四项)

这些在 5.1 是平行项, 可独立排期, 不阻塞 3a 自测门。
