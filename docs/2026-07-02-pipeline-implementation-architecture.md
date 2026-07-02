# pipeline.py 实现架构分析

日期: 2026-07-02
状态: 实现分析(对应 `pipeline.py` MVP, 923 行)
关联文档:
- [dual-cli-pipeline 设计](./2026-06-30-dual-cli-pipeline-design.md) — 完整设计 v2
- [tmux 实时可观测性 设计](./superpowers/specs/2026-07-01-tmux-observability-design.md) — observe_mode 双路 runner 的设计原由
- [README](../README.md) §3 — 工具作用与使用方式

本文档描述 `pipeline.py` 的**实际代码结构**，与设计文档互补：设计文档讲「为什么这么做」，本文档讲「现在代码长什么样、模块怎么组织、调用链怎么走」。读完能快速定位到对应源码。

## 1. 文件整体结构

`pipeline.py` 按功能分 9 个段落，源码注释用 `# ---------- 段名 ----------` 分隔：

| 段落 | 行号范围 | 职责 |
|------|---------|------|
| 配置与状态 | 34-70 | load_config / load_state / save_state / init_state |
| artifact 命名 | 75-84 | next_artifact_name(扫最大编号 +1) |
| prompt 渲染 | 89-97 | render_prompt(`{{key}}` 简单替换) |
| plan 步骤解析 | 102-134 | parse_plan_steps(正则切 `### 步骤 N`) |
| CLI 调用 | 144-170 | run_claude / run_codex(组装命令) |
| tmux_session 单元 | 175-212 | tmux 会话生命周期 |
| plain_runner 单元 | 217-301 | run_plain(Popen + 双线程) |
| InterruptHub 单元 | 306-367 | 统一中止信号入口 |
| tmux_pane_runner 单元 | 372-486 | run_in_tmux_pane(heredoc 注入 + 轮询) |
| _run_cli 分发层 | 491-549 | 按 observe_mode 分发到 tmux/plain |
| review 解析 | 554-596 | parse_status / parse_issues |
| 人工确认 | 601-620 | confirm |
| 阶段函数 | 625-843 | stage_design/impl/review/fix |
| 主流程 | 848-923 | main(stage 派发循环) |

## 2. 调用链总览

```
main (848)
  └─ while stage: 派发到 stage_*
       ├─ stage_design  (647) ── run_claude ─┐
       ├─ stage_impl    (670) ── run_codex ──┤
       ├─ stage_review  (720) ── run_claude ─┤
       └─ stage_fix     (803) ── run_codex ──┤
                                             │
            run_claude (144) / run_codex (158)
                  │  组装命令 + 调 _run_cli
                  ▼
            _run_cli (491)  ← 分发层
                  │
        ┌─────────┴──────────┐
   dry_run?              observe_mode
   打印+return(0)            │
                  ┌─────────┴──────────┐
              plain (533)          tmux (506)
                  │                    │
            run_plain (217)    run_in_tmux_pane (397)
                  │                    │
            [Popen + 双线程]    [tmux session + heredoc 注入]
                  │                    │
            InterruptHub(plain)  InterruptHub(tmux)
                  │                    │
            返回 (returncode, stdout_text) ← 统一签名
```

**关键不变量**：`run_claude`/`run_codex` 对外签名 `(returncode, stdout_text)` 不变，下层 plain/tmux 切换对上层透明。

## 3. 配置与状态

### 3.1 load_config (pipeline.py:34-42)

读 config.yaml，把相对路径 resolve 成绝对路径存入 `cfg` 的下划线字段：
- `cfg["_root"]` — `project.root` 绝对路径
- `cfg["_artifacts_dir"]` — artifacts 目录绝对路径
- `cfg["_state_file"]` — state.json 绝对路径
- `cfg["_prompts_dir"]` — prompts 目录(相对 `pipeline.py` 所在目录)

### 3.2 state.json 结构 (pipeline.py:60-70)

```json
{
  "stage": "1-design",
  "iteration": 0,
  "plan_artifact": null,
  "last_review": null,
  "last_fix": null,
  "open_issues": {"blocker": 0, "major": 0, "minor": 0},
  "issue_history": [],
  "convergence_trend": "none"
}
```

主循环 `main()` 是 `while True` 的 stage 派发器：读 `state["stage"]` → 调对应 `stage_*` → save_state → 进入下一轮。这种设计让 `--resume` 续跑非常自然：state.json 即进度。

### 3.3 save_state 时机 (pipeline.py:871, 888, 894, 910)

每个阶段函数返回后，主循环统一 `if not args.dry_run: save_state(...)`。**dry-run 全程不写 state**，避免脏 state 污染下次 `--resume`(对应记忆中的 feedback: dry-run 必须跳过 save_state)。

## 4. artifact 命名 (pipeline.py:75-84)

`next_artifact_name(cfg, role)` 扫 `artifacts_dir` 下所有 `[0-9][0-9][0-9]-*.md`，取最大编号 +1，返回 `NNN-{role}.md`。

特点：
- 不依赖时间戳，跨 resume 安全
- 编号严格递增，时间顺序明确
- role 取值: `plan` / `review` / `fix`

## 5. prompt 渲染 (pipeline.py:89-97)

`render_prompt(cfg, template_name, **kwargs)` 读 `prompts/<template>`，做 `{{key}}` 字面量替换。

容错：替换后扫剩余 `{{...}}`，有未替换占位符打印 warn 到 stderr。**不报错**，让流程继续走(便于 dry-run 检查)。

## 6. plan 步骤解析 (pipeline.py:102-134)

`parse_plan_steps(plan_path)` 解析 plan 文档的「## 实现步骤」段：

1. 定位 `## 实现步骤` 标题
2. 截到下一个一级 `## ` 标题前
3. 在该范围内按 `### 步骤 N: <标题>` 切分
4. body 是该标题下到下一步骤的全部内容
5. 返回 `[(step_no, title, body), ...]`

这是 prompt 与 codex 之间的契约：`01-design.md` 模板必须产出符合该格式的 plan，`stage_impl` 据此分多次调 codex(防单次上下文爆掉)。

## 7. CLI 调用层

### 7.1 run_claude (pipeline.py:144-155)

```python
cmd = [
    cfg["clis"]["claude"]["cmd"],
    "-p",
    "--output-format", "text",
    "--permission-mode", "acceptEdits",
]
```

- `-p`: 非交互模式, prompt 经 stdin
- `--permission-mode acceptEdits`: 让 claude 能写 artifact 文件(-p 模式默认无权限)
- 工作目录 = `cfg["_root"]`

### 7.2 run_codex (pipeline.py:158-170)

```python
cmd = [
    cfg["clis"]["codex"]["cmd"],
    "exec",
    "--sandbox", "workspace-write",
    "--skip-git-repo-check",
    "-C", str(cfg["_root"]),
]
```

- `exec`: 非交互执行
- `--sandbox workspace-write`: 允许写工作目录
- `--skip-git-repo-check`: 允许在非 git 仓库目录下写代码
- prompt 经 stdin

### 7.3 _run_cli 分发层 (pipeline.py:491-549)

统一签名 `(returncode, stdout_text)`。三路分支：

**dry_run 路径** (pipeline.py:493-497):
```
打印命令 + prompt 长度 + 前 200 字
return (0, "(dry-run, 未执行)")
```
**在分发前 return**，不创建 session / Popen，不写日志。

**tmux 路径** (pipeline.py:506-532):
1. `tmux_session(label, cwd)` 创建 `pipe-{label}` session
2. `InterruptHub("tmux", session)` 启动 stdin 监听
3. `run_in_tmux_pane(...)` 跑 agent
4. finally: `tmux_kill(session)` + 删 `exit-{label}.json`
5. 读 `log_path` 作为 stdout_text

tmux 不可用时**不降级**，返回非零(pipeline.py:510-515)。

**plain 路径** (pipeline.py:533-549):
1. `threading.Event()` 做中止信号载体
2. `InterruptHub("plain", interrupt_event=interrupt_event)` 启动监听
3. `run_plain(...)` 跑 agent
4. 读 `log_path` 作为 stdout_text

两路都固定 `timeout = 1800`(30 分钟, pipeline.py:502)。

## 8. plain runner (pipeline.py:217-301)

`run_plain(cmd, prompt, label, log_path, cwd, timeout, interrupt_event)` 用 Popen 启动 agent。

### 8.1 三线程协作

```
主线程 (wait timeout)
  ├─ _stream 线程: readline popen.stdout → tee 到 sys.stdout + 写 log_f
  └─ _watch 线程: wait interrupt_event → popen.send_signal(SIGINT)
```

**为什么需要独立的 _watch 线程**：主线程的 `popen.wait(timeout)` 带超时，不依赖 stdout 流关闭才返回。如果只靠读 stdout 检测中止，无输出命令(如 `sleep`)会卡死。独立监听线程保证随时能响应中止。

### 8.2 日志头回填 (pipeline.py:291-301)

`finally` 块里 `log_f.seek(0)` 回到开头重写 `=== exit: {returncode}` 行。原因：跑期间头部先写 `=== exit: (running)` 占位，跑完才知道真实退出码。finally 保证任何异常路径(包括 127 启动失败、124 超时)都 close 文件。

## 9. tmux_pane_runner (pipeline.py:397-486)

`run_in_tmux_pane(session_name, cmd, prompt, label, log_path, exit_path, timeout, hub=None)` 把命令塞进 tmux pane 跑，拿退出码。

### 9.1 注入命令的构造 (pipeline.py:426-440)

```python
cmd_str = " ".join(shlex.quote(c) for c in cmd)          # 第一层: cmd 各元素 quote
exit_path_q = shlex.quote(str(exit_path))
script = (
    f"{cmd_str} <<{delim}\n{prompt}\n{delim}\n"
    f"echo '{{\"returncode\":$?}}' > {exit_path_q}"
)
inject = f"sh -c {shlex.quote(script)}"                   # 第二层: 整个脚本块 quote
```

注入到 pane 的最终命令是 `sh -c '<script>'`，其中 script 含 cmd + heredoc + echo 写 exit_path。

### 9.2 shell 注入安全(双层 shlex.quote)

- **第一层**：cmd 每个元素 `shlex.quote`，防 cmd 含单引号破坏外层 `sh -c`
- **第二层**：整个 script 块 `shlex.quote` 后交给 `sh -c`，外层 send-keys 注入的字符串不再含未闭合引号
- **heredoc 分隔符**：`_make_heredoc_delimiter` 生成随机 `PROMPT_EOF_{hex}` 并扫 prompt 确认不含该字面量(pipeline.py:372-378)，防 prompt 内出现分隔符导致 heredoc 提前闭合

三层防护保证 prompt / 路径含任意元字符(单引号、空格、`$`、反引号)都安全。

### 9.3 轮询与超时 (pipeline.py:442-473)

```
deadline = now + timeout
while True:
    if exit_path 存在: 读 returncode, break
    if session 死了:   推断用户中止, hub.mark_external_interrupt(), returncode=130, break
    if now > deadline:
        send C-c, 等 10s grace
        if exit_path 出现: 读 returncode
        if 仍 None: tmux_kill, returncode=124
        break
    sleep 0.5
```

三种终止路径：
- 正常完成 → exit_path 文件出现，读 `{"returncode": N}`
- pane Ctrl-C → session 死但 exit_path 没出现 → 推断 130
- 超时 → C-c + grace + kill 兜底 → 124

### 9.4 日志头回填 (pipeline.py:475-485)

`run_in_tmux_pane` 也用 seek 回填 exit 行，但比 plain 更精细：

```python
exit_line_offset = f.tell()   # 记录 exit 行的字节偏移
# ... 跑完后
f.seek(exit_line_offset)
new_line = f"=== exit: {returncode}\n"
if len(new_line) < len(exit_line):    # 数字短于 "(running)"
    new_line = new_line[:-1].ljust(len(exit_line) - 1) + "\n"  # 右补空格覆盖占位符
f.write(new_line)
```

**不读全文、不依赖字面量匹配**——避免 agent stdout 恰含 `=== exit: (running)` 时 replace 误伤。

## 10. InterruptHub (pipeline.py:306-367)

统一两条 SIGINT 触发路径：

| 模式 | target | trigger() 行为 |
|------|--------|---------------|
| tmux | session_name | `tmux send-keys -t {session} C-c` |
| plain | interrupt_event | `interrupt_event.set()`(由 run_plain 的 _watch 线程发 SIGINT) |

### 10.1 stdin 监听 (pipeline.py:322-346)

`start()` 启动守护线程，用 `select.select([sys.stdin], [], [], 0.5)` 轮询 stdin，读到 `q` 开头行调 `trigger()`。

非 tty 跳过并 warn(pipeline.py:324-326)：`[warn] stdin 非 tty, q 键中止不可用, 可在 tmux pane 内 Ctrl-C`。

### 10.2 防重复触发 (pipeline.py:348-360)

`_triggered` 标志位保证只触发一次。`trigger()` 已被调过则直接 return。

### 10.3 mark_external_interrupt (pipeline.py:362-364)

tmux 模式下 pane 死了但 exit_path 没出现时，`run_in_tmux_pane` 调 `hub.mark_external_interrupt()` 标记「外部已中止」——区分「用户在 pane 按 Ctrl-C」与「正常完成」。

## 11. 阶段函数

四个阶段函数签名统一: `stage_xxx(state, cfg, ..., dry_run=False) -> (state, choice)`，`choice ∈ {"c": 继续, "a": 中止, "e": 编辑(仅阶段1)}`。

### 11.1 stage_design (pipeline.py:647-667)

渲染 `01-design.md` → run_claude → 检查 plan 文件是否生成 → 更新 state.plan_artifact + state.stage。

### 11.2 stage_impl (pipeline.py:670-717)

1. 解析 plan 步骤
2. **进入前快照** watch_files(pipeline.py/config/README) 的 mtime
3. 按步骤循环调 run_codex，每次只传当前步骤的 prompt(含步骤标题 + body + 工作目录)
4. **退出后对比** mtime，若 codex 退 0 但无任何文件改动 → 判定上下文耗尽，return `"a"` 中止

防静默零产出机制(pipeline.py:686-715)：避免 codex 静默退 0 进 review 死循环。

### 11.3 stage_review (pipeline.py:720-800)

1. iteration += 1
2. 渲染 `03-review.md`，传入上一轮 issue 数 + 类别
3. run_claude
4. 解析 `STATUS: PASS/FAIL`(STATUS_RE) 和 issue 计数 + 类别(ISSUE_RE + CATEGORY_RE)
5. 更新 state.open_issues / issue_history / convergence_trend
6. PASS → stage=done; FAIL 且未达 max_iterations → stage=4-fix; 达上限 → 中止

收敛趋势判定(pipeline.py:780-789)：对比当前 `blocker+major` 与上一轮，标 `下降/持平/上升`。**不直接终止**——终止只看 `STATUS: PASS` 或 `max_iterations`。

dry-run 特殊处理(pipeline.py:753-764)：模拟 FAIL 走完主流程但 issues 全 0，受 max_iterations 兜底约束——避免 dry-run 无限循环。

### 11.4 stage_fix (pipeline.py:803-843)

类似 stage_impl 的 mtime 快照机制，但 fix 可能改代码也可能只产出 fix 文档，所以 `_codex_produced_changes` 额外检查 `fix_path.exists()`(pipeline.py:835)。

## 12. review 解析 (pipeline.py:554-596)

### 12.1 STATUS 行

```python
STATUS_RE = re.compile(r"^STATUS:\s*(PASS|FAIL)\s*$", re.MULTILINE)
```

`parse_status` 返回 `"PASS"` / `"FAIL"` / `None`(文件不存在或无匹配)。

### 12.2 issue 计数与类别

```python
ISSUE_RE = re.compile(r"^###\s*\[(BLOCKER|MAJOR|MINOR)(?:-(\d+))?\]", re.MULTILINE)
CATEGORY_RE = re.compile(r"^\s*-\s*类别\s*[:：]\s*(.+?)\s*$", re.MULTILINE)
```

`parse_issues` 返回 `{blocker, major, minor, categories}`：
- 按 `### [BLOCKER-1]` / `### [MAJOR-2]` / `### [MINOR-3]` 标题计数
- 在每个 issue 标题之后、下一个 issue 标题之前找 `- 类别: a | b` 行
- 类别按 `|` 分隔，去重保序存入 categories 列表

categories 支撑 §5.2 同类重复判定(收敛监测，MVP 关闭，字段保留)。

## 13. 人工确认 (pipeline.py:601-620)

`confirm(msg, choices)` 打印选项读 stdin。

自动化支持：环境变量 `PIPELINE_AUTO_CONFIRM` 设为首字符(`c`/`e`/`a` 之一)，跳过 input 直接返回(pipeline.py:607-611)。CI/自动化测试用。

`EOFError`(stdin 关闭)默认返 `"a"` 中止(pipeline.py:615-616)。

## 14. 主流程 (pipeline.py:848-923)

`main()` 解析 argparse → load_config → load_state → while stage 派发。

### 14.1 启动逻辑 (pipeline.py:856-865)

- state 已存在且无 `--resume` → warn「未带 --resume 将从头开始 (覆盖)」
- 新跑无 `--design-doc` → error 退出 1
- 否则 init_state

### 14.2 阶段派发 (pipeline.py:867-919)

每个分支：调 `stage_*` → `if not dry_run: save_state` → 按 choice 决定继续/中止/暂停。

阶段 1 后额外有 `e`(编辑 plan) 选项：打印 `[pause] 请手动编辑 plan 后, 用 --resume 继续` 后 return 0(pipeline.py:883-885)。这是用户干预 plan 内容的官方路径。

## 15. 关键设计性质汇总

1. **统一签名**：`run_claude`/`run_codex` → `(returncode, stdout_text)`，下层 plain/tmux 切换对上层透明
2. **dry-run 全程不写 state**：分发前 return + 主循环 `if not dry_run: save_state`，双重保证
3. **防静默零产出**：stage_impl / stage_fix 用 mtime 快照拦截 codex 退 0 但无产出的情况
4. **shell 注入双层防护**：cmd 元素 quote + script 块整体 quote + 随机 heredoc 分隔符
5. **日志头 seek 回填**：不依赖字面量匹配，避免 agent 输出污染日志头
6. **中止两路径同归**：tmux pane Ctrl-C 与 pipeline 前台 q 键最终都让 agent 收 SIGINT，走 `choice="a"` 中止路径，state 留当前阶段可 `--resume`
7. **不自动降级 tmux → plain**：用户显式选 tmux 就尊重选择，降级会让用户误判模式
8. **artifact 编号扫目录取最大 +1**：不依赖时间戳，跨 resume 安全
