# pipeline 确认点失效与 agent 输出可见性修复设计

日期: 2026-07-02
状态: 设计
对应实现: `/wuhao/workspace/agent-pipeline/pipeline.py`

## 1. 问题

### 1.1 确认点 `c`/`a` 输入失效(阻塞)

用户报告:`--resume` 跑到 3b-review 完成后,`confirm()` 打印 `>` 提示符,输入 `c` 无反应;多次 enter 后输入 `a` 才退出。

**根因**:`InterruptHub.start()` 在 plain 模式下起了一个 `_listen` 线程,用 `select.select([sys.stdin]...)` + `sys.stdin.readline()` 持续监听 `q` 键以触发中止。该线程在 agent 结束后**不会退出**(`_triggered=False`,无 EOF),持续从 stdin `readline()`。当 `confirm()` 调 `input("> ")` 读 stdin 时,字符已被 `_listen` 线程抢走,`input()` 永远读不到。

`--resume` 能看到 agent 输出,是因为 `run_plain` 的 `_stream` 线程把 `popen.stdout` tee 到 `sys.stdout`——送输出(好)与抢输入(坏)是同一个 `run_plain` 调用里两个线程的相反作用。agent 一结束,送输出的线程停,抢输入的线程还在,`confirm()` 就废了。

### 1.2 tmux 模式 agent 输出在前台不可见

`run_in_tmux_pane` 靠 `tmux pipe-pane` 落日志,前台只打 `[run] codex: ...` 一行,agent 输出不进终端。用户必须 `tmux attach -t pipe-{label}` 才能看,体验差,也容易误判为「卡死」。

## 2. 方案

**方案 A(选定)**:去掉 `InterruptHub` 的 q 键监听,中止统一走 Ctrl-C。

- 删掉 `_listen` 线程整段逻辑,消除 stdin 抢夺根因
- Ctrl-C(OS 发 SIGINT 给 pipeline 主进程)经 `signal.signal` handler 转发给 agent:plain 模式 set `interrupt_event`(让 `run_plain` 的 `_watch` 线程给 popen 发 SIGINT);tmux 模式 `tmux send-keys -t {pane} C-c`
- agent 调用结束后 `signal.signal(signal.SIGINT, signal.SIG_DFL)` 恢复,确保后续 `confirm()` 的 `input()` 不被干扰,且用户在 confirm 阶段按 Ctrl-C = 直接终止 pipeline(等于中止)

**未选方案**:
- B(保留 q 键 + 加 stop 机制):q 键与 Ctrl-C 在 plain 模式功能重叠,冗余;线程停止时机精细易漏;stdin 抢夺风险仍在
- C(plain 去 q、tmux 留 q):tmux 模式 q 监听同样有 stdin 抢夺问题,收益小

## 3. 架构与组件

改动范围:`pipeline.py` 的 `InterruptHub` / `run_plain` / `run_in_tmux_pane` / `_run_cli` + `tests/test_pipeline.py`。不新增文件。

### 3.1 InterruptHub 瘦身

删除:
- `start()` 方法整个
- `_listen` 线程逻辑
- `_listener_thread` 字段
- `sys.stdin.isatty()` 检查与 warn

保留:
- `__init__(mode, target, interrupt_event=None)`:字段不变(`mode` / `target` / `interrupt_event` / `_triggered`)
- `trigger()`:`tmux` 模式 `send-keys -t {target} C-c`;`plain` 模式 `interrupt_event.set()`
- `mark_external_interrupt()` / `is_triggered()`

新增:
- `_sigint_received` 字段,初值 `False`,用于重入保护

### 3.2 信号注册机制(模块级)

`_run_cli` 在 agent 调用前注册 SIGINT handler,`finally` 块恢复原 handler(用 `signal.signal` 的返回值,支持嵌套注册场景)。`_prev_handler` 是 `_run_cli` 局部变量,非 `InterruptHub` 字段:

```python
def _run_cli(cmd, cwd, prompt, dry_run, label, cfg=None):
    ...
    hub = InterruptHub(...)
    _prev_handler = signal.signal(signal.SIGINT, lambda *_: _on_sigint(hub))
    try:
        ...  # 调 run_plain / run_in_tmux_pane
    finally:
        signal.signal(signal.SIGINT, _prev_handler)
        ...
```

`_on_sigint(hub)`(模块级函数):
- 若 `hub._sigint_received` 已为 True:直接 `raise KeyboardInterrupt`(让用户连按两次 Ctrl-C 能强杀,不被 handler 拦)
- 否则设 `hub._sigint_received = True`,调 `hub.trigger()`(tmux 发 C-c,plain set event)
- `trigger()` 内部已有 `_triggered` 守卫防重入

### 3.3 run_plain 中断路径(不变)

- `_watch` 线程:`interrupt_event.wait()` → `popen.send_signal(SIGINT)`,已有 `try/except Exception`
- Ctrl-C → handler set event → `_watch` 发 SIGINT → agent 退出 → `popen.wait()` 返回非零
- `run_plain` 返回 `(非零码, log_path)`,`_run_cli` 恢复 `SIG_DFL` 后返回

### 3.4 run_in_tmux_pane 中断路径

- Ctrl-C → handler 调 `hub.trigger()` → `tmux send-keys -t {pane} C-c` → pane 内 agent 收 SIGINT
- `send-keys` 包 `try/except`(pane 可能已死)
- 不再依赖前台 q 键

### 3.5 tmux 模式输出可见性

`_run_cli` tmux 分支,在 `run_in_tmux_pane` 调用期间起 `_tail` 线程把 `log_path` 新增内容打到前台:

```python
def _tail(log_path, stop_event):
    pos = 0
    while not stop_event.is_set():
        try:
            with open(log_path) as f:
                f.seek(pos)
                chunk = f.read()
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                pos = f.tell()
        except OSError:
            pass
        time.sleep(0.5)
```

- `run_in_tmux_pane` 返回后 `stop_event.set()` + `join`
- 文件被 truncate(头部回填路径)时 `seek(0)` 重置(下一轮 read 从头)
- pipe-pane 的 `cat >>` append 与 `_tail` 读同一文件无竞态(只读不影响 append)

### 3.6 main loop 不变

- `confirm()` 的 `input()` 现在能正常读到字符
- 4-fix 后自动进 3b-review 的逻辑保留

## 4. 数据流

### 4.1 plain 模式 Ctrl-C

```
用户按 Ctrl-C
 → OS 发 SIGINT 给 pipeline 主进程
 → _on_sigint(hub) 触发 (主线程)
 → hub._sigint_received = True
 → hub.trigger() → interrupt_event.set()
 → _watch 线程检测到 event
 → popen.send_signal(SIGINT)
 → agent 收到 SIGINT 退出
 → popen.wait() 返回非零
 → run_plain 返回 (非零, log_path)
 → _run_cli finally 恢复 SIG_DFL, 返回 (非零, stdout_text)
 → stage_* print [error], 返回 ("a")
 → main loop 走中止路径
```

### 4.2 tmux 模式 Ctrl-C

```
用户按 Ctrl-C
 → _on_sigint(hub) 触发
 → hub.trigger() → tmux send-keys -t {pane} C-c
 → pane 内 agent 收到 SIGINT 退出
 → exit_path 写非零 returncode (或 pane 死推断 130)
 → run_in_tmux_pane 轮询到 exit_path, 返回非零
 → _run_cli finally 恢复 SIG_DFL, _tail stop + join, 返回
```

## 5. 错误处理

- `_on_sigint` 里 `hub.trigger()` 包 `try/except`(tmux 可能已死)
- `_watch` 线程 `send_signal` 已有 `try/except Exception`(popen 可能已死)
- `_tail` 线程 `try/except OSError`(文件可能被 truncate 或不存在)
- `finally` 块恢复 `signal.SIG_DFL`,确保即使 agent 调用抛异常,信号 handler 不泄漏
- 重入保护:连按两次 Ctrl-C,第二次 `_sigint_received` 已 True → `raise KeyboardInterrupt` → 强杀路径

## 6. 测试调整

删除:
- `test_interrupt_hub_skips_when_not_tty`(不再有 stdin 监听)

保留(行为不变):
- `test_interrupt_hub_trigger_tmux_sends_cc`
- `test_interrupt_hub_trigger_plain_sets_event`
- `test_interrupt_hub_mark_external_interrupt`

新增:
- `test_run_plain_ctrl_c_sends_sigint`:mock `signal.signal`,验证 SIGINT handler 注册 + finally 恢复 `SIG_DFL`;mock `interrupt_event.set` 验证 Ctrl-C 触发 event
- `test_run_in_tmux_pane_ctrl_c_sends_cc_to_pane`:mock handler,验证 `tmux send-keys C-c` 被调用
- `test_run_cli_finally_restores_sigint_handler`:在 `run_plain` 抛异常的路径下,断言 `signal.signal(signal.SIGINT, signal.SIG_DFL)` 在 finally 被调用
- `test_tail_thread_outputs_log_content_to_stdout`(tmux 模式):写 log_path 内容,启动 `_tail` 线程,断言 `sys.stdout` 收到内容

## 7. 验收标准

| 场景 | 期望 |
|------|------|
| plain 模式跑 codex | 前台流式看到 codex 输出,日志落全 |
| tmux 模式跑 codex | 前台流式看到 codex 输出(经 _tail 转发),`tmux ls` 看到 session,日志落全 |
| plain 模式 agent 跑时按 Ctrl-C | agent 收 SIGINT 退出,非零退出码,`[error] ... 中止`,state 留当前阶段 |
| tmux 模式 agent 跑时按 Ctrl-C | pane 收 C-c,agent 退出,非零退出码,state 留当前阶段 |
| review/fix 完成后 `confirm()` 按 `c`/`a` | `input()` 正常读到字符,不再被吞 |
| agent 跑完后按 Ctrl-C(在 confirm 等) | pipeline 直接终止(默认 SIGINT 行为,等于中止) |
| 连按两次 Ctrl-C | 第二次绕过 handler,强杀 pipeline |
| `pytest tests/` | 全绿,新增 4 个测试通过 |

## 8. 不改动范围

- `run_plain` 的 `_stream` / `_watch` 线程核心逻辑
- `run_in_tmux_pane` 的 heredoc / pipe-pane / 轮询逻辑
- `parse_plan_steps` / `stage_impl` / `stage_fix` / mtime 检查
- 006/007 review 发现的其他 bug(路径 quoting、句柄泄漏、单引号注入、头部回填时机等)——这些是 codex 产出的代码缺陷,留给下一轮 fix 阶段处理

## 9. 风险

- **信号 handler 多线程语义**:Python `signal.signal` 只在主线程收到信号时触发,`_stream`/`_watch`/`_tail` 是子线程不收 SIGINT——符合预期(handler 在主线程,set event,子线程看到 event 行动)
- **handler 泄漏**:`finally` 块确保恢复 `_prev_handler`(用 `signal.signal` 的返回值,而非硬编码 `SIG_DFL`,支持嵌套注册场景)
- **_tail 与头部回填竞态**:头部回填用 `r+` + `replace` 重写整个文件,`_tail` 此时可能读到空或重复——可接受(回填在 agent 结束后,`_tail` 即将退出,丢几行无影响)
