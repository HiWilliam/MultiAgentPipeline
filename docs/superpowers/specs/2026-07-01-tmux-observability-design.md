# 扩展路线第4条:tmux 实时可观测性 设计

日期: 2026-07-01
状态: 设计 v1(待实现)
对应 README §6.1 第4条
关联设计: [dual-cli-pipeline 设计](../2026-06-30-dual-cli-pipeline-design.md)

## 1. 背景与定位

当前 `_run_cli` 用 `subprocess.run(capture_output=True)` 阻塞一次性捕获 agent 输出,用户只能事后读 `pipeline/last-{label}.log`,长阶段(尤其阶段 2 实现、3a 自修)全程黑盒,卡点时无法判断 agent 是在思考还是真卡住。

本设计把 CLI 调用从「阻塞捕获」改为「流式 + 可观察 + 可中途干预」,满足三条诉求:
1. 实时看到 agent 输出
2. 流式落日志(替代事后一次性写)
3. 中途 Ctrl-C 中止当前阶段

定位为 MVP 编排工具的增强,不改变半自动核心定位与阶段调度模型。

## 2. 需求与范围

### 范围(最大档)
- **看**:agent 启动后,用户能实时看到当前输出
- **流式日志**:边跑边写 `last-{label}.log`(替代当前一次性写)
- **中途干预**:用户可 Ctrl-C 中止当前阶段,pipeline 走现有中止路径

### 不在范围
- pipeline 主动解析流式输出做结构化判断(留给第5条 token 统计扩展)
- 自动降级 tmux → plain(用户显式选 tmux 就尊重选择,降级会让用户误判模式)
- 新增 confirm 点或 state 字段
- 全自动无人介入(README §6.3 已排除)

## 3. 架构总览

核心改造点:把 `_run_cli` 里 `subprocess.run(capture_output=True)` 换成两套进程模型,由 config `observe_mode: tmux|plain` 切换(默认 `plain`,向后兼容)。

### 3.1 进程拓扑(tmux 模式)

```
pipeline 主进程 (前台)
  ├── tmux new-session -d -s pipe-{label}
  │     └── pane 0: 直接跑 agent 命令 (claude -p ... / codex exec ...)
  │           父进程 = tmux server
  │           stdout/stderr ──► pane 实时显示 + tmux pipe-pane 落 last-{label}.log
  │           退出码 ──► pane 进程结束后写 pipeline/exit-{label}.json
  │
  ├── pipeline 主进程: 轮询 exit_path 文件 + tmux session 存活
  │
  └── SIGINT 路径(两路径同归):
        ├─ 用户在 agent pane 按 Ctrl-C ──► tmux 把 SIGINT 发给 pane 里的 agent(原生)
        └─ 用户在 pipeline 前台按 q    ──► pipeline 调 tmux send-keys -t pipe-{label} C-c
```

### 3.2 进程拓扑(plain 模式)

```
pipeline 主进程 (前台, 持有 Popen)
  └── agent 子进程 (Popen 启动, 父进程是 pipeline)
        stdout/stderr ──► 边读边写 last-{label}.log + tee 到 pipeline stdout
        退出码 ──► Popen.wait()
        SIGINT ──► Popen.send_signal(SIGINT) (pipeline 前台按 q 触发)
```

### 3.3 关键性质
- 两种模式下 SIGINT 最终都让 agent 收到信号、pane 死掉、pipeline 检测到 → 走现有 `choice="a"` 中止路径
- tmux 模式下 agent 真在 pane 里,pane Ctrl-C 原生打到 agent
- tmux 模式下 pipeline 不持有 Popen,通过 tmux 命令间接管控;plain 模式直接持有 Popen
- tmux 模式流式日志靠 `tmux pipe-pane` 落文件(混终端控制字符,第5条 token 统计届时再治理)

### 3.4 不改动
- 阶段调度模型(`stage_design/impl/review/fix` 的状态机与 confirm 点)
- artifact 命名、state.json 结构、review 解析逻辑
- 人工确认点位置
- `run_claude/run_codex` 对外签名

### 3.5 改动面
- `_run_cli` 重构为 `_run_cli_tmux` + `_run_cli_plain`,由 `observe_mode` 分发
- 新增 tmux session 生命周期管理模块
- 新增退出码文件协议(pane 结束后写 `exit-{label}.json`)
- 新增 SIGINT 统一入口(pipeline 前台 q 键 → tmux send-keys C-c 或 Popen.send_signal)
- config 加 `observe_mode: tmux|plain` 字段,默认 `plain`
- tmux 依赖检测:启动时若选 tmux 但无 tmux 可执行,报错退出(不降级)

## 4. 组件拆分

`pipeline.py` 当前单文件 450 行,改动仍放同文件内(不拆模块),靠函数分组 + 注释分隔。共 5 个单元。

### 4.1 单元 1: `tmux_session` — tmux 会话生命周期

**职责**: 封装 tmux session 的创建、查询、销毁,屏蔽 tmux 命令细节。

**接口**:
```python
tmux_session(label, root) -> session_name
  # - shutil.which("tmux") 检测, 无则 raise RuntimeError
  # # - 若 session pipe-{label} 已存在则先 kill(防脏), 打印 warn
  # # - tmux new-session -d -s pipe-{label} -c {root}
  # # - 返回 "pipe-{label}"

tmux_kill(session_name)
  # tmux kill-session -t {session_name}, 忽略非零退出码(已死就算了)

tmux_session_alive(session_name) -> bool
  # tmux has-session -t {session_name} 退出码判定
```

**依赖**: 仅标准库 `subprocess` + `shutil.which`。不依赖 pipeline 内部状态。

**为何独立**: tmux 命令细节(命名规则、检测逻辑)易变,独立后其他单元只调三个函数。

### 4.2 单元 2: `tmux_pane_runner` — 在 pane 里跑 agent 并拿结果

**职责**: 把 CLI 命令塞进 tmux pane 跑,落日志,拿退出码。tmux 模式的"run_cli 等价物"。

**接口**:
```python
run_in_tmux_pane(session_name, cmd, prompt, label, log_path, exit_path, cwd, timeout)
  -> (returncode, log_path)
  # - 生成随机 heredoc 分隔符 PROMPT_EOF_{uuid4.hex[:8]}, 扫 prompt 确认不含该字面量(含则 raise ValueError)
  # - tmux pipe-pane -t {session}:0.0 -o "cat >> {log_path}"  (pane 输出边跑边落日志)
  # - tmux send-keys 注入: sh -c '{cmd} <<{delimiter}\n{prompt}\n{delimiter}\necho "{\"returncode\":$?}" > {exit_path}' Enter
  # - 轮询循环(每 0.5s):
  #     * exit_path 存在 → 读 returncode, 跳出
  #     * not tmux_session_alive → 推断用户中止, returncode=130, 跳出
  #     * now > deadline → tmux send-keys C-c, 等 10s grace, 仍不死则 tmux kill-session, returncode=124
  # - 返回 (returncode, log_path)
```

**依赖**: 单元 1(`tmux_session_alive`)。

**为何独立**: 把"在 pane 里跑命令"的所有杂活(heredoc、pipe-pane、退出码文件、轮询、超时)收成一坨,与阶段函数解耦。轮询的三个探测点(检查 exit_path / 检查 session / 检查超时)抽成可注入函数,便于单元测试 mock。

### 4.3 单元 3: `plain_runner` — 无 tmux 的流式 runner

**职责**: Popen 启动 agent,流式读 stdout/stderr 落日志 + tee 到前台,支持 q 键 SIGINT。plain 模式的"run_cli 等价物",也是现有 `_run_cli` 的直接演进。

**接口**:
```python
run_plain(cmd, prompt, label, log_path, cwd, timeout, interrupt_event)
  -> (returncode, log_path)
  # - subprocess.Popen(cmd, stdin=PIPE, stdout=PIPE, stderr=STDOUT, cwd=cwd, text=True)
  # - popen.stdin.write(prompt); close
  # - 主线程循环 readline popen.stdout: 行 → 写 log_path + 写 sys.stdout(tee)
  # - 监听 interrupt_event(threading.Event): set 则 popen.send_signal(SIGINT)
  # - popen.wait(timeout) → returncode; 超时 popen.kill() → 124
  # - 返回 (returncode, log_path)
```

**依赖**: 标准库 `subprocess`、`threading.Event`。

**为何独立**: 与 tmux 路径进程模型完全不同(Popen vs tmux server),独立避免互相污染。

### 4.4 单元 4: `InterruptHub` — 中止信号统一入口

**职责**: 统一两条 SIGINT 触发路径(pipeline 前台 q 键 / tmux pane Ctrl-C 推断),转成对当前 runner 的中止指令。

**接口**:
```python
class InterruptHub:
    def __init__(self, mode, target, interrupt_event=None):
        # mode: "tmux" | "plain"
        # target: tmux 模式为 session_name; plain 模式为 Popen 对象
        # interrupt_event: plain 模式必传(threading.Event), trigger() 时 set 它供 run_plain 监听
        #                  tmux 模式不用(传 None)

    def start(self):
        # 检测 sys.stdin.isatty(), 非 tty 则跳过监听(打印 warn), 不阻塞
        # 启动后台线程轮询 stdin(select 非阻塞), 读到 'q' 调 self.trigger()

    def trigger(self):
        # mode=tmux: tmux send-keys -t {session} C-c
        # mode=plain: interrupt_event.set() (由 run_plain 的监听线程发 SIGINT 给 Popen)
        # 设 self._triggered = True

    def mark_external_interrupt(self):
        # tmux 模式下 pane 死了但 exit_path 没出现时, 由 run_in_tmux_pane 调用
        # 标记"外部已中止"(用户在 pane 里 Ctrl-C 了), is_triggered() 返回 True

    def is_triggered(self) -> bool:
        # 返回是否触发过中止(无论是 q 键还是外部 Ctrl-C)
```

**关键设计**: tmux pane 里的 Ctrl-C 由 tmux 直接发给 agent,pipeline 不参与发送——pipeline 只是通过"pane 死了 + exit_path 未出现"推断出"用户主动中止了",调 `mark_external_interrupt()` 走中止路径。plain 模式下 `trigger()` 经 `interrupt_event` 中转,由 `run_plain` 的监听线程对 Popen 发 SIGINT(不在 InterruptHub 线程里直接动 Popen,避免跨线程 Popen 操作)。

**依赖**: tmux 模式用单元 1;plain 模式用 `Popen` 对象。

### 4.5 单元 5: `_run_cli` 重构 — 分发层

**职责**: 按 `observe_mode` 分发到单元 2 或单元 3,统一返回 `(returncode, stdout_text)` 签名(对外不变,阶段函数无感)。

**接口**:
```python
def _run_cli(cmd, cwd, prompt, dry_run, label, cfg) -> (returncode, stdout_text):
    log_path = cwd / "pipeline" / f"last-{label}.log"
    if dry_run:
        # 保持原 dry-run 逻辑(打印不执行), 不碰 tmux
        return 0, "(dry-run, 未执行)"

    mode = cfg.get("observe_mode", "plain")
    try:
        if mode == "tmux":
            exit_path = cwd / "pipeline" / f"exit-{label}.json"
            session = tmux_session(label, cwd)
            hub = InterruptHub("tmux", session); hub.start()
            code, _ = run_in_tmux_pane(session, cmd, prompt, label, log_path, exit_path, cwd, timeout=1800)
        else:  # plain
            popen = subprocess.Popen(...)
            interrupt_event = threading.Event()
            hub = InterruptHub("plain", popen, interrupt_event); hub.start()  # plain 模式构造时传入 event, trigger() 时 set 它
            code, _ = run_plain(cmd, prompt, label, log_path, cwd, timeout=1800, interrupt_event=interrupt_event)
        stdout_text = log_path.read_text() if log_path.exists() else ""
        return code, stdout_text
    finally:
        if mode == "tmux":
            tmux_kill(session)
            exit_path.unlink(missing_ok=True)
```

**关键性质**: 阶段函数 `run_claude/run_codex` 与 `stage_*` 完全不改,它们调的 `_run_cli` 签名不变。中止发生时返回非零退出码,阶段函数走现有 `if code != 0: return state, "a"` 路径,无需新逻辑。finally 块保证临时文件与 session 无论正常/异常都清理。

### 4.6 组件依赖图

```
stage_design/impl/review/fix  (不改)
        │
        ▼
  run_claude / run_codex       (不改, 仅签名透传)
        │
        ▼
     _run_cli (分发)            ← 单元 5
       ├── tmux 模式 ──► tmux_session(1) + tmux_pane_runner(2) + InterruptHub(4, tmux)
       └── plain 模式 ──► plain_runner(3) + InterruptHub(4, plain)
```

## 5. 数据流

以 `stage_impl` 调 `run_codex`(tmux 模式)为例走完整生命周期,plain 模式只在差异点标注。

### 5.1 进入 `_run_cli`
```
stage_impl
  → render_prompt 渲染 02-impl.md → prompt 字符串
  → run_codex(cfg, prompt)
      → _run_cli(cmd, cwd, prompt, dry_run, label="codex", cfg)
```

### 5.2 tmux 模式启动
```
_run_cli (observe_mode=tmux)
  1. log_path = cwd/"pipeline"/"last-codex.log"
  2. exit_path = cwd/"pipeline"/"exit-codex.json"   # 临时, 跑完删
  3. session = tmux_session("codex", cwd)
       - shutil.which("tmux") 检测, 无则 raise → _run_cli catch 后返回 (1, "")
       - tmux new-session -d -s pipe-codex -c {cwd}
  4. hub = InterruptHub("tmux", session); hub.start()
       - 启动 stdin 监听线程, 等待 'q' 键
  5. run_in_tmux_pane(session, cmd, prompt, "codex", log_path, exit_path, cwd, timeout=1800)
```

### 5.3 agent 在 pane 里跑(主进程等待)

pane 里执行的命令(经 `tmux send-keys` 注入):
```bash
sh -c 'codex exec --sandbox workspace-write --skip-git-repo-check -C {cwd} <<PROMPT_EOF_a1b2c3d4
{prompt}
PROMPT_EOF_a1b2c3d4
echo "{\"returncode\":$?}" > {exit_path}'
```
同时 `tmux pipe-pane -t pipe-codex:0.0 -o "cat >> {log_path}"` 把 pane 输出边跑边落日志。

主进程的等待逻辑(`run_in_tmux_pane` 内):
```
deadline = now + timeout
loop:
  if exit_path 存在:           读 returncode → 跳出
  if not tmux_session_alive:   hub.mark_external_interrupt(); returncode=130; 跳出
  if now > deadline:           tmux send-keys C-c; 等 10s grace;
                               仍不死 → tmux kill-session; returncode=124; 跳出
  sleep(0.5)
```

### 5.4 中止信号的两条路径(同归)

**路径 A: 用户在 agent pane 按 Ctrl-C**
```
tmux 把 Ctrl-C 解释为 SIGINT 发给 pane 里的 agent 进程
  → agent 退出, pane shell 结束
  → 但 echo $? 那行仍会执行 → exit_path 被写(returncode 可能是 130)
  → 主进程下一轮 poll 发现 exit_path → 读 returncode
```
**路径 B: 用户在 pipeline 前台按 q**
```
InterruptHub 的 stdin 监听线程读到 'q'
  → hub.trigger()
  → mode=tmux: tmux send-keys -t pipe-codex:0.0 C-c
  → 效果同路径 A: agent 收 SIGINT → 退出 → exit_path 被写
```
两条路径最终都汇聚到"agent 退出 → exit_path 出现 → 主进程读 returncode"。

**边界: Ctrl-C 打断了 pane shell 本身**(`echo $?` 没执行)→ 走 5.3 的 `not tmux_session_alive` 分支,returncode=130。

### 5.5 退出码与日志回流
```
run_in_tmux_pane 返回 (returncode=130, log_path)
  → _run_cli:
      finally: tmux_kill(session); exit_path.unlink(missing_ok)
      stdout_text = log_path.read_text()
      返回 (130, stdout_text)
  → run_codex 返回 (130, stdout_text)
  → stage_impl:
      if code != 0 and not dry_run:
          print(f"[error] codex 退出码 {code}, 中止")
          return state, "a"        # 走现有中止路径, 不新增交互
```
主流程 `main` 收到 `choice="a"` → `[abort]` 退出, state 已保存(中止前 stage 仍是 `2-impl`, 下次 `--resume` 重跑该阶段)。

### 5.6 plain 模式差异点
```
_run_cli (observe_mode=plain)
  1. log_path = .../last-codex.log
  2. popen = Popen(cmd, stdin=PIPE, stdout=PIPE, stderr=STDOUT, cwd, text=True)
  3. hub = InterruptHub("plain", popen); hub.start()
  4. run_plain(popen, log_path, timeout, hub.interrupt_event):
       - popen.stdin.write(prompt); close
       - 循环 readline popen.stdout: 行 → 写 log_path + 写 sys.stdout(tee)
       - 监听 interrupt_event: set 则 popen.send_signal(SIGINT)
       - popen.wait(timeout) → returncode; 超时 popen.kill() → 124
  5. stdout_text = log_path.read_text()
  6. 返回 (returncode, stdout_text)
```
plain 模式下用户直接在 pipeline 前台看输出(tee 到 stdout), 无需 attach tmux; q 键经 `Popen.send_signal` 直达 agent。

### 5.7 数据流关键不变量
- `_run_cli` 对外签名 `(returncode, stdout_text)` 不变 → 阶段函数零改动
- `last-{label}.log` 路径与用途不变(头三行 `=== cmd/exit/stdout` 改为流式写, 但文件位置与查阅习惯不变)
- 中止 = 非零退出码 → 走现有 `choice="a"` 路径 → 不新增 confirm 点
- `state.json` 结构不变, 中止时 stage 留在当前阶段 → `--resume` 行为不变

## 6. 错误处理

按"会出错的点"逐个列,每个给处理策略。总原则:所有错误最终都收敛到"非零退出码"或"异常抛出到 _run_cli catch 后返回非零",走现有 `choice="a"` 中止路径,不新增 confirm 点,临时文件/session 在 finally 块清理。

### 6.1 tmux 不可用
**触发**: `observe_mode: tmux` 但 `shutil.which("tmux")` 返回 None。
**处理**: `tmux_session` 抛 `RuntimeError("observe_mode=tmux 但未找到 tmux 可执行, 请安装 tmux 或改 observe_mode: plain")`。`_run_cli` catch 后打印错误, 返回 `(1, "")` → 阶段函数走 `code != 0` 中止路径。**不自动降级到 plain**。

### 6.2 tmux session 已存在(脏 session)
**触发**: 上次跑异常退出, `pipe-{label}` session 没清理。
**处理**: `tmux_session` 创建前先 `tmux has-session`, 存在则 `tmux kill-session` 再新建, 打印 `[warn] 发现残留 session pipe-{label}, 已清理`。

### 6.3 pane 里 agent 命令启动失败(cmd 不存在)
**触发**: `claude` 或 `codex` 可执行文件不在 PATH。
**处理**: pane 里 shell 报 `command not found`, exit code = 127, `exit_path` 写 `{"returncode":127}`。主进程正常读到 127 → 走 `code != 0` 中止。日志里有 shell 错误信息可供诊断。无需特殊处理。

### 6.4 heredoc 注入失败(prompt 含分隔符字面量)
**触发**: prompt 文本里恰好出现 `PROMPT_EOF_xxxx` 行, 导致 heredoc 提前结束。
**处理**: 用随机生成的分隔符 `PROMPT_EOF_{uuid4.hex[:8]}`, 且 `run_in_tmux_pane` 启动前扫描 prompt 确认不含该分隔符, 含则 `raise ValueError`(概率极低, 真出现说明 prompt 有问题)。

### 6.5 exit_path 一直不出现 + session 还活着
**触发**: agent 卡死, 既不退出也不写 exit_path; 或 `echo $? > exit_path` 那行本身没执行(pane shell 异常)。
**处理**: 主进程的 `deadline` 超时机制兜底——超时后 `tmux send-keys C-c`, 等 10s grace; 仍无 exit_path 则 `tmux kill-session`, 返回 `(124, log_text)`。与现有 `_run_cli` 的 `TimeoutExpired` 路径行为一致(现有 timeout 也返回 124)。

### 6.6 pane 死了但 exit_path 没出现(用户 Ctrl-C 路径)
**触发**: 用户在 agent pane 按 Ctrl-C, agent 收 SIGINT 退出, 但 pane shell 的 `echo $? > exit_path` 那行没来得及执行(Ctrl-C 也打断了 shell 本身)。
**处理**: 主进程检测到 `not tmux_session_alive(session)` 且 `exit_path` 不存在 → 推断为"用户主动中止", 返回 `(130, log_text)`(130 = SIGINT 标准退出码)。`stage_impl` 走 `code != 0` 中止。state 留在当前阶段, `--resume` 可重跑。
**边界**: agent 因 OOM 或 tmux server 重启导致 pane 死掉(非用户主动)也会被误判为 130。但无论哪种都是非零退出码 → 走中止路径, 行为正确(退出码值不精确)。可接受。

### 6.7 pipeline 前台 q 键监听失败(stdin 非 tty)
**触发**: `pipeline run` 在非交互环境跑(stdin 是管道或文件, 如 CI 里 `pipeline run config.yaml < input.txt`)。
**处理**: `InterruptHub.start()` 检测 `sys.stdin.isatty()`, 非 tty 则跳过 q 键监听线程(打印 `[warn] stdin 非 tty, q 键中止不可用, 可在 tmux pane 内 Ctrl-C`)。tmux 模式下 pane Ctrl-C 仍可用; plain 模式下彻底无干预手段(只能等 timeout), 这是非交互环境的固有局限, 文档说明。

### 6.8 tmux kill-session 失败(session 已被外部 kill)
**触发**: 用户在 tmux 外部手动 `tmux kill-session -t pipe-codex`。
**处理**: `tmux_kill` 调用 `tmux kill-session` 时忽略非零退出码(已死就算了)。`_run_cli` 的 finally 块仍执行 `exit_path.unlink(missing_ok)`, 清理本地临时文件。

### 6.9 日志文件写失败(磁盘满 / 权限)
**触发**: `last-{label}.log` 写入时报 `OSError`。
**处理**: `run_in_tmux_pane` / `run_plain` 的写日志操作包 try/except, 写失败时打印 `[error] 日志写入失败 {log_path}: {e}`, 但**不中止 agent**(agent 还在 pane 里跑, 只是日志没落全)。agent 结束后返回的 `stdout_text` 可能是空字符串或部分内容, 阶段函数按现有逻辑处理(解析失败会报 `[error] 无法从 {path} 解析 STATUS`)。退化行为, 不新增逻辑。

### 6.10 dry-run 与 observe_mode 的交互
**触发**: `--dry-run` 时 observe_mode 配置仍生效, 但 dry-run 不实际执行 CLI。
**处理**: dry-run 优先——`_run_cli` 开头 `if dry_run:` 分支直接返回, 不碰 tmux、不创建 session。dry-run 本就不该有副作用, observe_mode 在 dry-run 下无意义。打印的 `[dry-run] codex 命令: ...` 保持不变。

## 7. 测试策略

pipeline.py 当前无测试套件(脚本形态, 无 `tests/` 目录)。本设计不追求建完整测试框架, 只覆盖"改动风险高 + 易构造"的部分, 优先级排序。

### 7.1 不测什么(YAGNI)
- **不测阶段调度逻辑**: 没改动, 且依赖完整 CLI 调用链, 测试成本高收益低
- **不测 tmux 命令本身的正确性**: tmux 是外部工具, 测它没意义
- **不测 claude/codex 真实调用**: 依赖外部 CLI + 网络, 属于集成测试范畴, 本次不做

### 7.2 必测单元(按风险排序)

**测试 1: `tmux_session` 命名与残留清理**
```python
def test_tmux_session_creates_with_label():
    # mock shutil.which("tmux") 返回 "/usr/bin/tmux"
    # mock subprocess.run 记录调用
    tmux_session("codex", Path("/tmp"))
    # 断言: subprocess.run 被调用, 参数含 "new-session -d -s pipe-codex -c /tmp"

def test_tmux_session_kills_stale():
    # mock tmux has-session 返回 0(存在)
    tmux_session("codex", Path("/tmp"))
    # 断言: 先调 kill-session, 再调 new-session
```
方式: mock `subprocess.run` + `shutil.which`, 纯单元测试, 不依赖真实 tmux。

**测试 2: `tmux_session` 检测 tmux 缺失**
```python
def test_tmux_session_raises_when_no_tmux():
    mock shutil.which("tmux") 返回 None
    with pytest.raises(RuntimeError, match="未找到 tmux"):
        tmux_session("codex", Path("/tmp"))
```

**测试 3: heredoc 分隔符冲突检测**
```python
def test_pane_runner_rejects_prompt_with_delimiter():
    delimiter = "PROMPT_EOF_abcd1234"
    prompt = f"normal text\nPROMPT_EOF_abcd1234\nmore text"
    with pytest.raises(ValueError, match="prompt 含 heredoc 分隔符"):
        run_in_tmux_pane(..., prompt=prompt, ...)
```
实现前提: `run_in_tmux_pane` 内部生成分隔符后扫 prompt 的逻辑要单独抽成纯函数才好测。

**测试 4: `InterruptHub` 在非 tty 下不启动监听**
```python
def test_interrupt_hub_skips_when_not_tty():
    mock sys.stdin.isatty() 返回 False
    hub = InterruptHub("tmux", session_name)
    hub.start()
    # 断言: 监听线程未启动, is_triggered() == False
    # 断言: 打印了 warn 消息
```

**测试 5: exit_path 推断逻辑(用户中止 vs 正常退出)**
```python
def test_pane_runner_infers_interrupt_when_session_dead_without_exit_path():
    mock: 第一轮 poll → exit_path 不存在, session_alive=False
    run_in_tmux_pane(...)
    # 断言: 返回 returncode=130

def test_pane_runner_reads_exit_path_when_normal():
    mock: 第一轮 poll → exit_path 存在, 内容 {"returncode":0}
    # 断言: 返回 returncode=0
```
实现前提: `run_in_tmux_pane` 的轮询逻辑要可注入"探测函数", 否则没法 mock。设计上把轮询的每一步(检查 exit_path / 检查 session)抽成闭包或可替换函数。

**测试 6: `_run_cli` 分发逻辑**
```python
def test_run_cli_dispatches_tmux():
    cfg = {"observe_mode": "tmux", ...}
    mock tmux_session / run_in_tmux_pane / tmux_kill
    _run_cli(cmd, cwd, prompt, dry_run=False, label="codex", cfg=cfg)
    # 断言: run_in_tmux_pane 被调用, run_plain 未被调用

def test_run_cli_dispatches_plain():
    cfg = {"observe_mode": "plain", ...}
    mock run_plain
    _run_cli(...)
    # 断言: run_plain 被调用, tmux 相关未调用

def test_run_cli_dry_run_skips_all():
    cfg = {"observe_mode": "tmux", ...}
    _run_cli(..., dry_run=True, ...)
    # 断言: tmux_session 未调用, 只打印 dry-run 信息
```

**测试 7: finally 块清理(exit_path + session)**
```python
def test_run_cli_cleans_up_on_exception():
    mock run_in_tmux_pane 抛 RuntimeError
    mock tmux_kill / exit_path.unlink 记录调用
    with pytest.raises(RuntimeError):
        _run_cli(...)
    # 断言: tmux_kill 被调用, exit_path.unlink 被调用
```

### 7.3 测试基础设施
- **框架**: pytest(项目无依赖, 需新增 `requirements-dev.txt` 或 README 注明)
- **mock 策略**: 统一用 `unittest.mock.patch` mock `subprocess.run`、`shutil.which`、`sys.stdin.isatty`、`Path.exists/read_text/unlink`。不引入额外 mock 库
- **测试文件位置**: `tests/test_pipeline.py`(项目根下新建 `tests/` 目录)。与 `pipeline.py` 同级, 扁平结构, 不拆模块
- **运行方式**: `pytest tests/` —— README 补一节"开发"说明如何跑测试

### 7.4 手工验收清单(真实环境)
自动化测试用 mock, 真实 tmux + claude/codex 的端到端行为靠手工验收:
1. `observe_mode: plain` 跑 `--dry-run` → 行为与改造前一致
2. `observe_mode: plain` 真跑 codex → 前台看到流式输出, 日志落全, 退出码正确
3. `observe_mode: tmux` 真跑 codex → `tmux ls` 看到 `pipe-codex`, attach 后看到 agent 输出
4. tmux 模式下 agent pane 按 Ctrl-C → pipeline 报 `[error] codex 退出码 130, 中止`, state 留在 `2-impl`
5. tmux 模式下 pipeline 前台按 q → 同上效果
6. tmux 模式下 agent 正常完成 → session 自动 kill, 无残留
7. `observe_mode: tmux` 但无 tmux → 报错提示安装, 不崩
8. 超时(把 timeout 改成 5s 跑 codex)→ 报 `[error] codex 超时`, session 被清理

## 8. config 与文档变更

### 8.1 config.yaml 新增字段
```yaml
# observe_mode: agent 输出的观察模式
#   plain - pipeline 直接 spawn agent, 前台流式输出, 无 tmux 依赖(默认, 向后兼容)
#   tmux  - 在独立 tmux session 里跑 agent, 用户 attach 观察, 可 pane 内 Ctrl-C 中止
observe_mode: plain
```
`config.example.yaml` 同步加此字段与注释。

### 8.2 README 变更
- §6.1 第4条标注「已实现, 见 [设计文档](./docs/superpowers/specs/2026-07-01-tmux-observability-design.md)」
- 新增「开发」小节: 跑测试的方式 `pytest tests/`
- §3 配置说明补 `observe_mode` 字段

### 8.3 不变更
- `state.json` 结构不变
- artifact 命名规则不变
- prompt 模板(`prompts/*.md`)不变
- `pipeline.py` 的命令行参数不变(`--design-doc` / `--resume` / `--dry-run` 保持)

## 9. 与其他扩展路线的关联

- **第5条(token 统计)**: 依赖本设计落地的流式日志。但 tmux 模式下 `pipe-pane` 输出混终端控制字符, 第5条实现时需另开「agent 同时 tee 一份纯文本 JSON 到单独文件」的机制, 或优先在 plain 模式下做 token 统计。本设计不预留接口, 届时再定。
- **第1-3条(3a 自测门 / 收敛监测 / 柔性出口)**: 与本设计正交, 互不影响。
- **§6.2 后续打磨第4条(Web UI 观察台)**: 可基于本设计落地的 `last-{label}.log` 流式日志做时序图, 但本设计不为其预留接口。

## 10. 实现优先级(给 writing-plans 的提示)

按组件依赖序, 建议实现顺序:
1. 单元 1 `tmux_session`(最底层, 无依赖)
2. 单元 3 `plain_runner`(独立, 不依赖 tmux, 可先跑通 plain 模式)
3. 单元 4 `InterruptHub`(依赖 1 + Popen)
4. 单元 2 `tmux_pane_runner`(依赖 1 + 4)
5. 单元 5 `_run_cli` 分发层(依赖以上全部)
6. config 字段 + README 文档
7. 测试套件(随各单元同步写, 不最后补)

每步完成后跑对应单元测试 + 手工验收清单的相关项, 不积压。
