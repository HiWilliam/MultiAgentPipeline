#!/usr/bin/env python3
"""dual-cli-pipeline 编排工具 (MVP)。

驱动 Claude Code CLI (设计/review) 与 Codex CLI (实现/fix) 协作开发。
对应 docs/2026-06-30-dual-cli-pipeline-design.md。

MVP 范围:
- 阶段机: 1-design → 2-impl → 3b-review → 4-fix → done (跳过 3a)
- 人工确认点: 阶段 1 后 / 每轮 review FAIL 后 / max_iterations 兜底
- state.json 持久化与续跑
- 达标判定: 解析 review 的 STATUS 行

阶段 1 接收一份「设计文档」(架构/定位/模块拆分级),由 Claude Code 转译成
§4.1 规范的有序实现步骤 plan.md。不接收零散需求文本。
"""

import argparse
import json
import os
import re
import shutil
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import yaml


# ---------- 配置与状态 ----------

def load_config(config_path):
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    root = Path(cfg["project"]["root"]).expanduser().resolve()
    cfg["_root"] = root
    cfg["_artifacts_dir"] = (root / cfg["artifacts_dir"]).resolve()
    cfg["_state_file"] = (root / cfg["state_file"]).resolve()
    cfg["_prompts_dir"] = Path(__file__).parent / "prompts"
    return cfg


def load_state(cfg):
    sf = cfg["_state_file"]
    if sf.exists():
        with open(sf) as f:
            return json.load(f)
    return None


def save_state(cfg, state):
    sf = cfg["_state_file"]
    sf.parent.mkdir(parents=True, exist_ok=True)
    with open(sf, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def init_state(cfg):
    return {
        "stage": "1-design",
        "iteration": 0,
        "plan_artifact": None,
        "last_review": None,
        "last_fix": None,
        "open_issues": {"blocker": 0, "major": 0, "minor": 0},
        "issue_history": [],
        "convergence_trend": "none",
    }


# ---------- artifact 命名 ----------

def next_artifact_name(cfg, role):
    """扫 artifacts_dir 取最大 NNN + 1, 拼 NNN-{role}.md。"""
    d = cfg["_artifacts_dir"]
    d.mkdir(parents=True, exist_ok=True)
    max_n = 0
    for p in d.glob("[0-9][0-9][0-9]-*.md"):
        m = re.match(r"(\d{3})-", p.name)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"{max_n + 1:03d}-{role}.md"


# ---------- prompt 渲染 ----------

def render_prompt(cfg, template_name, **kwargs):
    """读 prompts/<template>, 做 {{key}} 简单替换。"""
    tpl = (cfg["_prompts_dir"] / template_name).read_text()
    for k, v in kwargs.items():
        tpl = tpl.replace("{{" + k + "}}", str(v))
    leftover = re.findall(r"\{\{[^}]+\}\}", tpl)
    if leftover:
        print(f"[warn] prompt 模板 {template_name} 有未替换占位符: {leftover}", file=sys.stderr)
    return tpl


# ---------- plan 步骤解析 ----------

STEP_HEADER_RE = re.compile(r"^###\s+步骤\s+(\d+)\s*[:：]\s*(.*)$", re.MULTILINE)
IMPL_SECTION_RE = re.compile(r"^##\s+实现步骤\s*$", re.MULTILINE)


def parse_plan_steps(plan_path):
    """解析 plan 文档的「## 实现步骤」段, 返回 [(step_no, title, body), ...]。

    - 定位 `## 实现步骤` 之后, 到下一个一级 `## ` 标题前
    - 在该范围内按 `### 步骤 N: <标题>` 切分, body 是该标题下到下一步骤/段落末尾的全部内容
    - 顺序保留 plan 内出现的顺序
    """
    text = Path(plan_path).read_text()
    # 找「## 实现步骤」段起点
    m = IMPL_SECTION_RE.search(text)
    if not m:
        return []
    start = m.end()
    # 找下一个一级标题 (## 开头但不是 ###)
    rest = text[start:]
    next_h2 = re.search(r"^##\s+", rest, re.MULTILINE)
    section = rest[: next_h2.start()] if next_h2 else rest

    steps = []
    # 找所有步骤标题位置
    headers = list(STEP_HEADER_RE.finditer(section))
    for i, hm in enumerate(headers):
        step_no = int(hm.group(1))
        title = hm.group(2).strip()
        body_start = hm.end()
        body_end = headers[i + 1].start() if i + 1 < len(headers) else len(section)
        body = section[body_start:body_end].strip()
        steps.append((step_no, f"步骤 {step_no}: {title}", body))
    return steps


# ---------- CLI 调用 ----------

# observe_mode: agent 输出的观察模式
#   plain - pipeline 直接 spawn agent, 前台流式输出, 无 tmux 依赖 (默认, 向后兼容)
#   tmux  - 在独立 tmux session 里跑 agent, 用户 attach 观察, 可 pane 内 Ctrl-C 中止


def run_claude(cfg, prompt, dry_run=False):
    """claude -p --output-format text, prompt 经 stdin。工作目录 = project.root。

    --permission-mode acceptEdits: 让 claude 能写 artifact 文件 (-p 模式默认无权限)。
    """
    cmd = [
        cfg["clis"]["claude"]["cmd"],
        "-p",
        "--output-format", "text",
        "--permission-mode", "acceptEdits",
    ]
    return _run_cli(cmd, cfg["_root"], prompt, dry_run, label="claude", cfg=cfg)


def run_codex(cfg, prompt, dry_run=False):
    """codex exec --sandbox workspace-write, prompt 经 stdin。工作目录 = project.root。

    --skip-git-repo-check: 允许在非 git 仓库目录下写代码 (hos_manager 可能尚未 git init)。
    """
    cmd = [
        cfg["clis"]["codex"]["cmd"],
        "exec",
        "--sandbox", "workspace-write",
        "--skip-git-repo-check",
        "-C", str(cfg["_root"]),
    ]
    return _run_cli(cmd, cfg["_root"], prompt, dry_run, label="codex", cfg=cfg)


# ---------- 单元 1: tmux_session — tmux 会话生命周期 ----------

def tmux_session(label, root):
    """创建 tmux session pipe-{label}, 工作目录 root。返回 session_name。

    - shutil.which("tmux") 检测, 无则 raise RuntimeError (不自动降级 plain)
    - 若 session pipe-{label} 已存在则先 kill (防脏), 打印 warn
    - tmux new-session -d -s pipe-{label} -c {root}
    """
    if shutil.which("tmux") is None:
        raise RuntimeError(
            "observe_mode=tmux 但未找到 tmux 可执行, 请安装 tmux 或改 observe_mode: plain"
        )
    session_name = f"pipe-{label}"
    if tmux_session_alive(session_name):
        print(f"[warn] 发现残留 session {session_name}, 已清理")
        tmux_kill(session_name)
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", str(root)],
        check=True,
        capture_output=True,
    )
    return session_name


def tmux_kill(session_name):
    """kill-session, 忽略非零退出码 (已死就算了)。"""
    subprocess.run(
        ["tmux", "kill-session", "-t", session_name],
        capture_output=True,
    )


def tmux_session_alive(session_name):
    """tmux has-session 退出码判定。"""
    r = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    return r.returncode == 0


# ---------- 单元 3: plain_runner — 无 tmux 的流式 runner ----------

def run_plain(cmd, prompt, label, log_path, cwd, timeout, interrupt_event):
    """Popen 启动 agent, 流式读 stdout/stderr 落日志 + tee 到前台。

    - popen.stdin.write(prompt); close
    - 读线程循环 readline popen.stdout: 行 → 写 log_path + 写 sys.stdout (tee)
    - 监听 interrupt_event: set 则 popen.send_signal(SIGINT)
    - 主线程 popen.wait(timeout) → returncode; 超时 popen.kill() → 124
    返回 (returncode, log_path)。

    读线程独立于主线程, 保证无输出命令 (如 sleep) 也能被 timeout/interrupt 中止:
    主线程的 wait() 带超时, 不依赖 stdout 流关闭才返回。
    """
    import threading
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "w")
    returncode = None
    try:
        log_f.write(f"=== cmd: {' '.join(cmd)}\n")
        log_f.write("=== exit: (running)\n")
        log_f.write("=== stdout (stream) ===\n")
        log_f.flush()
        try:
            popen = subprocess.Popen(
                cmd, cwd=str(cwd), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
        except FileNotFoundError as e:
            log_f.write(f"[error] 启动失败: {e}\n")
            returncode = 127
            return returncode, log_path
        try:
            popen.stdin.write(prompt)
            popen.stdin.close()
        except BrokenPipeError:
            pass

        # 监听线程: interrupt_event set → 发 SIGINT
        def _watch():
            interrupt_event.wait()
            try:
                popen.send_signal(signal.SIGINT)
            except Exception:
                pass
        t_watch = threading.Thread(target=_watch, daemon=True)
        t_watch.start()

        # 读线程: 流式读 stdout, tee 到前台 + 落日志
        def _stream():
            try:
                for line in popen.stdout:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    try:
                        log_f.write(line)
                        log_f.flush()
                    except OSError as e:
                        print(f"[error] 日志写入失败 {log_path}: {e}")
            except OSError as e:
                print(f"[error] 日志写入失败 {log_path}: {e}")
        t_stream = threading.Thread(target=_stream, daemon=True)
        t_stream.start()

        # 主线程: 带超时 wait, 不依赖 stdout 流关闭
        try:
            returncode = popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            popen.kill()
            popen.wait()
            returncode = 124

        # 等读线程排空残余输出 (进程已死, stdout 即将 EOF)
        t_stream.join(timeout=5)
        return returncode, log_path
    finally:
        # 回填日志头 exit 行 (FileNotFoundError=127 / 超时=124 / 正常值),
        # 与各路径语义一致, 避免残留 (running)。finally 保证任何异常路径都 close。
        if returncode is not None:
            try:
                log_f.seek(0)
                log_f.write(f"=== cmd: {' '.join(cmd)}\n")
                log_f.write(f"=== exit: {returncode}\n")
                log_f.write("=== stdout (stream) ===\n")
            except OSError:
                pass
        log_f.close()


# ---------- 单元 4: InterruptHub — 中止信号统一入口 ----------

class InterruptHub:
    """统一两条 SIGINT 触发路径 (pipeline 前台 q 键 / tmux pane Ctrl-C 推断)。

    mode="tmux": target=session_name; trigger() 调 tmux send-keys -t {session} C-c
    mode="plain": target=Popen; interrupt_event 必传; trigger() set event, 由 run_plain 监听发 SIGINT
    """

    def __init__(self, mode, target=None, interrupt_event=None):
        self.mode = mode
        self.target = target
        self.interrupt_event = interrupt_event
        self._triggered = False
        self._listener_thread = None
        if mode == "tmux" and target is None:
            raise ValueError("tmux 模式 InterruptHub 必须传 target (session_name)")

    def start(self):
        """启动 stdin 监听线程, 读到 'q' 调 trigger()。非 tty 跳过 + warn。"""
        if not sys.stdin.isatty():
            print("[warn] stdin 非 tty, q 键中止不可用, 可在 tmux pane 内 Ctrl-C")
            return
        import threading
        import select
        def _listen():
            while True:
                try:
                    r, _, _ = select.select([sys.stdin], [], [], 0.5)
                except Exception:
                    return
                if not r:
                    if self._triggered:
                        return
                    continue
                line = sys.stdin.readline()
                if not line:
                    return
                if line.strip().lower().startswith("q"):
                    self.trigger()
                    return
        self._listener_thread = threading.Thread(target=_listen, daemon=True)
        self._listener_thread.start()

    def trigger(self):
        """触发中止。tmux: send-keys C-c; plain: set interrupt_event。"""
        if self._triggered:
            return
        self._triggered = True
        if self.mode == "tmux":
            subprocess.run(
                ["tmux", "send-keys", "-t", self.target, "C-c"],
                capture_output=True,
            )
        else:  # plain
            if self.interrupt_event is not None:
                self.interrupt_event.set()

    def mark_external_interrupt(self):
        """tmux 模式下 pane 死了但 exit_path 没出现时调用, 标记外部已中止。"""
        self._triggered = True

    def is_triggered(self):
        return self._triggered


# ---------- 单元 2: tmux_pane_runner — 在 pane 里跑 agent 并拿结果 ----------

def _make_heredoc_delimiter(prompt):
    """生成随机分隔符 PROMPT_EOF_{hex}, 扫 prompt 确认不含该字面量。"""
    import uuid
    delim = f"PROMPT_EOF_{uuid.uuid4().hex[:8]}"
    if delim in prompt:
        raise ValueError(f"prompt 含 heredoc 分隔符 {delim}, 无法安全注入")
    return delim


def _read_exit_path(exit_path):
    """读 exit-{label}.json 的 returncode。文件不存在/解析失败返回 None。"""
    if not exit_path.exists():
        return None
    try:
        data = json.loads(exit_path.read_text())
        return data.get("returncode")
    except (json.JSONDecodeError, OSError):
        return None


def _probe_exit_path_exists(exit_path):
    """轮询探测点 1: exit_path 是否已生成。抽成纯函数便于 mock 注入。"""
    return exit_path.exists()


def run_in_tmux_pane(session_name, cmd, prompt, label, log_path, exit_path, timeout, hub=None):
    """把 cmd 塞进 tmux pane 跑, pipe-pane 落日志, 拿退出码。

    - 生成 heredoc 分隔符 + 冲突检测
    - tmux pipe-pane -o "cat >> {log_path}" 落日志
    - tmux send-keys 注入: sh -c '{cmd} <<{delim}\n{prompt}\n{delim}\necho "{\"returncode\":$?}" > {exit_path}'
    - 轮询: exit_path 存在 → 读 returncode; session 死 → 130; 超时 → send C-c + grace + kill, 124
    返回 (returncode, log_path)。
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # 清掉可能的旧 exit_path
    exit_path.unlink(missing_ok=True)

    delim = _make_heredoc_delimiter(prompt)
    # pipe-pane 落日志 (log_path 做 shell quoting, 防 project.root 含空格/元字符)
    subprocess.run(
        ["tmux", "pipe-pane", "-t", f"{session_name}:0.0", "-o",
         f"cat >> {shlex.quote(str(log_path))}"],
        capture_output=True,
    )
    # 在 pane 头部写 cmd 标记; 记录 exit 行的字节偏移, 回填时 seek 到此处重写,
    # 不依赖字面量匹配 (避免 agent stdout 恰含 "=== exit: (running)" 时 replace 误伤)。
    exit_line = f"=== exit: (running)\n"
    with open(log_path, "w") as f:
        f.write(f"=== cmd: {' '.join(cmd)}\n")
        exit_line_offset = f.tell()
        f.write(exit_line)
        f.write("=== stdout (tmux pane stream) ===\n")

    # 注入命令: 对每个 cmd 元素做 shell quoting 再拼接, 防 cmd 含单引号破坏外层 sh -c。
    # 整个脚本块 (cmd + heredoc + echo 写 exit_path) 拼成单行后用 shlex.quote 整体转义,
    # 再交给 `sh -c <quoted>` 执行: 外层 send-keys 注入的字符串不再含未闭合引号,
    # 内部 prompt/路径含任意元字符 (含单引号/空格) 均安全。
    cmd_str = " ".join(shlex.quote(c) for c in cmd)
    exit_path_q = shlex.quote(str(exit_path))
    script = (
        f"{cmd_str} <<{delim}\n{prompt}\n{delim}\n"
        f"echo '{{\"returncode\":$?}}' > {exit_path_q}"
    )
    inject = f"sh -c {shlex.quote(script)}"
    subprocess.run(
        ["tmux", "send-keys", "-t", f"{session_name}:0.0", inject, "Enter"],
        capture_output=True,
    )

    deadline = time.time() + timeout
    returncode = None
    while True:
        if _probe_exit_path_exists(exit_path):
            returncode = _read_exit_path(exit_path)
            if returncode is not None:
                break
        if not tmux_session_alive(session_name):
            # pane 死了但 exit_path 没出现 → 推断用户中止
            if hub is not None:
                hub.mark_external_interrupt()
            returncode = 130
            break
        if time.time() > deadline:
            # 超时: 先 send C-c, 等 10s grace
            subprocess.run(
                ["tmux", "send-keys", "-t", f"{session_name}:0.0", "C-c"],
                capture_output=True,
            )
            grace_deadline = time.time() + 10
            while time.time() < grace_deadline:
                if _probe_exit_path_exists(exit_path) or not tmux_session_alive(session_name):
                    break
                time.sleep(0.5)
            if _probe_exit_path_exists(exit_path):
                returncode = _read_exit_path(exit_path)
            if returncode is None:
                # 仍不死 → kill-session
                tmux_kill(session_name)
                returncode = 124
            break
        time.sleep(0.5)

    # 在日志头部回填 exit: seek 到 exit 行偏移, 写入新值并右补空格覆盖旧占位符
    # (returncode 数字短于 "(running)"), 不读全文、不依赖字面量匹配, 更稳健。
    try:
        with open(log_path, "r+") as f:
            f.seek(exit_line_offset)
            new_line = f"=== exit: {returncode}\n"
            if len(new_line) < len(exit_line):
                new_line = new_line[:-1].ljust(len(exit_line) - 1) + "\n"
            f.write(new_line)
    except OSError:
        pass
    return returncode, log_path


# ---------- 单元 5: _run_cli 分发层 ----------

def _run_cli(cmd, cwd, prompt, dry_run, label, cfg=None):
    """按 cfg.observe_mode 分发到 tmux 或 plain runner。对外签名 (returncode, stdout_text) 不变。"""
    if dry_run:
        print(f"[dry-run] {label} 命令: {' '.join(cmd)}")
        print(f"[dry-run] prompt 长度: {len(prompt)} 字符")
        print(f"[dry-run] prompt 前 200 字: {prompt[:200]}")
        return 0, "(dry-run, 未执行)"
    print(f"[run] {label}: {' '.join(cmd)}")
    log_dir = cwd / "pipeline"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"last-{label}.log"
    timeout = 1800  # 30 分钟

    mode = (cfg or {}).get("observe_mode", "plain")

    if mode == "tmux":
        exit_path = log_dir / f"exit-{label}.json"
        session = None
        try:
            try:
                session = tmux_session(label, cwd)
            except RuntimeError as e:
                # tmux 不可用: 不降级, 返回非零
                print(f"[error] {e}")
                return 1, ""
            hub = InterruptHub("tmux", session)
            hub.start()
            code, _ = run_in_tmux_pane(
                session, cmd, prompt, label, log_path, exit_path, timeout, hub=hub,
            )
        finally:
            if session is not None:
                tmux_kill(session)
            exit_path.unlink(missing_ok=True)
        try:
            stdout_text = log_path.read_text() if log_path.exists() else ""
        except OSError as e:
            print(f"[warn] {label} 日志读取失败, stdout 退化为空: {e}")
            stdout_text = ""
        if code != 0:
            print(f"[error] {label} 退出码 {code}, 日志见 {log_path}")
        return code, stdout_text
    else:  # plain
        import threading
        interrupt_event = threading.Event()
        # plain 模式 trigger 只用 interrupt_event, 不需要 target (popen)
        hub = InterruptHub("plain", interrupt_event=interrupt_event)
        hub.start()
        code, _ = run_plain(
            cmd, prompt, label, log_path, cwd, timeout, interrupt_event,
        )
        try:
            stdout_text = log_path.read_text() if log_path.exists() else ""
        except OSError as e:
            print(f"[warn] {label} 日志读取失败, stdout 退化为空: {e}")
            stdout_text = ""
        if code != 0:
            print(f"[error] {label} 退出码 {code}, 日志见 {log_path}")
        return code, stdout_text


# ---------- review 解析 ----------

STATUS_RE = re.compile(r"^STATUS:\s*(PASS|FAIL)\s*$", re.MULTILINE)
ISSUE_RE = re.compile(r"^###\s*\[(BLOCKER|MAJOR|MINOR)(?:-(\d+))?\]", re.MULTILINE)


def parse_status(artifact_path):
    """读 review 文件, 返回 'PASS' / 'FAIL' / None。"""
    if not Path(artifact_path).exists():
        return None
    text = Path(artifact_path).read_text()
    m = STATUS_RE.search(text)
    return m.group(1) if m else None


CATEGORY_RE = re.compile(r"^\s*-\s*类别\s*[:：]\s*(.+?)\s*$", re.MULTILINE)


def parse_issues(artifact_path):
    """读 review 文件, 返回 {blocker, major, minor} 计数 + categories 列表。

    categories 从每个 ISSUE 的 `- 类别: a | b` 行解析 (| 分隔, 去重保序),
    支撑 §5.2 同类重复判定。
    """
    out = {"blocker": 0, "major": 0, "minor": 0, "categories": []}
    if not Path(artifact_path).exists():
        return out
    text = Path(artifact_path).read_text()
    cats = []
    seen = set()
    for m in ISSUE_RE.finditer(text):
        sev = m.group(1).lower()
        out[sev] += 1
        # 在该 issue 标题之后、下一个 issue/段标题之前找类别行
        start = m.end()
        nxt = ISSUE_RE.search(text, pos=start)
        end = nxt.start() if nxt else len(text)
        for cm in CATEGORY_RE.finditer(text, pos=start, endpos=end):
            for raw in cm.group(1).split("|"):
                c = raw.strip()
                if c and c not in seen:
                    seen.add(c)
                    cats.append(c)
    out["categories"] = cats
    return out


# ---------- 人工确认 ----------

def confirm(msg, choices):
    """打印 msg + 选项, 读 stdin 返回选中字符。choices=[(char, desc), ...]。"""
    print()
    print(msg)
    for c, d in choices:
        print(f"  [{c}] {d}")
    if os.environ.get("PIPELINE_AUTO_CONFIRM"):
        auto = os.environ["PIPELINE_AUTO_CONFIRM"][0].lower()
        if auto in {c for c, _ in choices}:
            print(f"> (auto) {auto}")
            return auto
    while True:
        try:
            line = input("> ").strip().lower()
        except EOFError:
            return "a"
        if not line:
            continue
        if line[0] in {c for c, _ in choices}:
            return line[0]


# ---------- 阶段函数 ----------

def _snapshot_mtimes(paths):
    """对一组路径取 mtime 快照 (不存在记 None)。用于检测 codex 是否真改了文件。"""
    snap = {}
    for p in paths:
        try:
            snap[str(p)] = p.stat().st_mtime
        except FileNotFoundError:
            snap[str(p)] = None
    return snap


def _codex_produced_changes(before, after, new_files=None):
    """对比前后快照 + 检查 new_files 是否生成。有任何变化/新生成返回 True。"""
    for k in before:
        if before[k] != after.get(k):
            return True
    for f in (new_files or []):
        if f.exists():
            return True
    return False


def stage_design(state, cfg, design_doc, dry_run=False):
    print("\n=== 阶段 1: 设计 (Claude Code) ===")
    plan_name = next_artifact_name(cfg, "plan")
    prompt = render_prompt(
        cfg, "01-design.md",
        design_doc=design_doc,
        artifacts_dir=cfg["artifacts_dir"],
        plan_artifact_name=plan_name,
        max_iterations=cfg["max_iterations"],
    )
    code, _ = run_claude(cfg, prompt, dry_run=dry_run)
    if code != 0 and not dry_run:
        print(f"[error] claude 退出码 {code}, 中止")
        return state, "a"
    plan_path = cfg["_artifacts_dir"] / plan_name
    if plan_path.exists() or dry_run:
        state["plan_artifact"] = f"{cfg['artifacts_dir']}/{plan_name}"
        state["stage"] = "2-impl"
        return state, "c"
    print(f"[error] 期望产出 {plan_path} 不存在, 中止")
    return state, "a"


def stage_impl(state, cfg, dry_run=False):
    print("\n=== 阶段 2: 实现 (Codex) ===")
    plan_path = cfg["_root"] / state["plan_artifact"]
    if not plan_path.exists() and not dry_run:
        print(f"[error] plan 不存在: {plan_path}, 中止 (state 损坏?)")
        return state, "a"

    # 解析 plan 步骤, 按步骤分多次调 codex (避免单次上下文爆掉)
    steps = parse_plan_steps(plan_path)
    if not dry_run and not steps:
        print(f"[error] plan 未解析出任何实现步骤 (缺 ## 实现步骤 段或 ### 步骤 N 标题), 中止")
        return state, "a"
    if dry_run and not steps:
        # dry-run 容错: plan 不存在或无步骤时仍走一次旧式调用以验证 prompt 渲染
        steps = [(0, "(dry-run 单步)", "")]

    # 进入前快照关键文件 mtime, 用于检测 codex 是否真产出 (防静默零产出进 review 死循环)
    watch_files = [
        cfg["_root"] / "pipeline.py",
        cfg["_root"] / "config.example.yaml",
        cfg["_root"] / "README.md",
    ]
    before = _snapshot_mtimes(watch_files)

    for step_no, step_title, step_body in steps:
        if not dry_run:
            print(f"\n--- 步骤 {step_no}: {step_title} ---")
        prompt = render_prompt(
            cfg, "02-impl.md",
            project_root=str(cfg["_root"]),
            step_title=step_title,
            step_body=step_body,
        )
        code, _ = run_codex(cfg, prompt, dry_run=dry_run)
        if code != 0 and not dry_run:
            print(f"[error] codex 退出码 {code} (步骤 {step_no}), 中止")
            return state, "a"

    if not dry_run:
        after = _snapshot_mtimes(watch_files)
        if not _codex_produced_changes(before, after):
            print(
                f"[warn] codex 退出码 0 但无任何文件改动 (pipeline.py/config/README mtime 均未变), "
                f"可能上下文耗尽未实际产出。建议中止并重跑阶段 2, 或检查 codex 调用链路"
            )
            return state, "a"
    state["stage"] = "3b-review"
    return state, "c"


def stage_review(state, cfg, dry_run=False):
    state["iteration"] += 1
    it = state["iteration"]
    print(f"\n=== 阶段 3b: 审查 (Claude Code) — 第 {it} 轮 ===")

    prev_bm = 0
    prev_cats = "[]"
    if state["issue_history"]:
        h = state["issue_history"][-1]
        prev_bm = h["blocker"] + h["major"]
        prev_cats = json.dumps(h.get("categories", []), ensure_ascii=False)
    trend = state["convergence_trend"] if it > 1 else "none"

    review_name = next_artifact_name(cfg, "review")
    prompt = render_prompt(
        cfg, "03-review.md",
        artifacts_dir=cfg["artifacts_dir"],
        plan_artifact_name=Path(state["plan_artifact"]).name,
        last_fix_artifact=Path(state["last_fix"]).name if state["last_fix"] else "(无首轮)",
        review_artifact_name=review_name,
        iteration=it,
        prev_blocker_major=prev_bm if it > 1 else "N/A",
        prev_categories=prev_cats,
    )
    code, _ = run_claude(cfg, prompt, dry_run=dry_run)
    if code != 0 and not dry_run:
        print(f"[error] claude 退出码 {code}, 中止")
        return state, "a"

    review_path = cfg["_artifacts_dir"] / review_name
    state["last_review"] = f"{cfg['artifacts_dir']}/{review_name}"

    # dry-run: 不解析真实文件,模拟 FAIL 走完主流程 (受 max_iterations 兜底约束)
    if dry_run:
        print(f"[dry-run] 模拟 review=FAIL, 不解析 {review_name}")
        state["open_issues"] = {"blocker": 0, "major": 0, "minor": 0}
        state["issue_history"].append({
            "iteration": it, "blocker": 0, "major": 0, "minor": 0, "categories": [],
        })
        state["convergence_trend"] = "none" if it == 1 else "持平"
        if state["iteration"] >= cfg["max_iterations"]:
            print(f"[stall] iteration={it} 已达 max_iterations={cfg['max_iterations']}, 中止")
            return state, "a"
        state["stage"] = "4-fix"
        return state, "c"

    status = parse_status(review_path)
    if status is None:
        print(f"[error] 无法从 {review_path} 解析 STATUS, 中止")
        return state, "a"

    issues = parse_issues(review_path)
    state["open_issues"] = {"blocker": issues["blocker"], "major": issues["major"], "minor": issues["minor"]}
    state["issue_history"].append({
        "iteration": it,
        "blocker": issues["blocker"],
        "major": issues["major"],
        "minor": issues["minor"],
        "categories": issues["categories"],
    })
    if it == 1:
        state["convergence_trend"] = "none"
    else:
        cur = issues["blocker"] + issues["major"]
        if cur < prev_bm:
            state["convergence_trend"] = "下降"
        elif cur == prev_bm:
            state["convergence_trend"] = "持平"
        else:
            state["convergence_trend"] = "上升"

    print(f"[parse] STATUS={status}, issues={state['open_issues']}, trend={state['convergence_trend']}")

    if status == "PASS":
        state["stage"] = "done"
        return state, "c"
    if state["iteration"] >= cfg["max_iterations"]:
        print(f"[stall] iteration={it} 已达 max_iterations={cfg['max_iterations']}, 中止")
        return state, "a"
    state["stage"] = "4-fix"
    return state, "c"


def stage_fix(state, cfg, dry_run=False):
    print(f"\n=== 阶段 4: 修复 (Codex) — 第 {state['iteration']} 轮 ===")
    if not state.get("last_review"):
        print("[error] state.last_review 为空, 无法 fix (需先跑 review), 中止")
        return state, "a"
    review_path = cfg["_root"] / state["last_review"]
    if not review_path.exists() and not dry_run:
        print(f"[error] last_review 不存在: {review_path}, 中止 (state 损坏?)")
        return state, "a"
    fix_name = next_artifact_name(cfg, "fix")
    prompt = render_prompt(
        cfg, "04-fix.md",
        artifacts_dir=cfg["artifacts_dir"],
        plan_artifact_name=Path(state["plan_artifact"]).name,
        last_review_artifact=Path(state["last_review"]).name,
        fix_artifact_name=fix_name,
    )
    # 进入前快照关键文件 mtime (fix 可能改代码, 也可能只产出 fix 文档)
    watch_files = [
        cfg["_root"] / "pipeline.py",
        cfg["_root"] / "config.example.yaml",
        cfg["_root"] / "README.md",
    ]
    before = _snapshot_mtimes(watch_files)
    code, _ = run_codex(cfg, prompt, dry_run=dry_run)
    if code != 0 and not dry_run:
        print(f"[error] codex 退出码 {code}, 中止")
        return state, "a"
    fix_path = cfg["_artifacts_dir"] / fix_name
    if not dry_run:
        # 检查 fix 是否真产出 (改了代码 或 生成了 fix 文档)
        after = _snapshot_mtimes(watch_files)
        if not _codex_produced_changes(before, after, new_files=[fix_path]):
            print(
                f"[warn] codex 退出码 0 但无任何文件改动且未生成 {fix_name}, "
                f"可能上下文耗尽未实际产出。建议中止并重跑, 或检查 codex 调用链路"
            )
            return state, "a"
    state["last_fix"] = f"{cfg['artifacts_dir']}/{fix_name}"
    state["stage"] = "3b-review"
    return state, "c"


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser(description="dual-cli-pipeline 编排工具 (MVP)")
    ap.add_argument("config", help="config.yaml 路径")
    ap.add_argument("--design-doc", help="设计文档路径 (阶段 1 输入,新跑时必填)")
    ap.add_argument("--resume", action="store_true", help="从 state.json 续跑")
    ap.add_argument("--dry-run", action="store_true", help="只打印不执行 CLI")
    args = ap.parse_args()

    cfg = load_config(args.config)
    state = load_state(cfg)
    if state is None or not args.resume:
        if state is not None and not args.resume:
            print(f"[warn] 已存在 {cfg['_state_file']}, 未带 --resume 将从头开始 (覆盖)")
        if not args.design_doc and not args.resume:
            print("[error] 新跑必须提供 --design-doc")
            return 1
        state = init_state(cfg)
    design_doc = args.design_doc or ""

    while True:
        stage = state["stage"]
        if stage == "1-design":
            state, choice = stage_design(state, cfg, design_doc, dry_run=args.dry_run)
            if not args.dry_run: save_state(cfg, state)
            if choice == "a":
                print("[abort] 用户中止")
                return 1
            if choice == "c":
                c = confirm(
                    f"阶段 1 完成, plan 已写到 {state['plan_artifact']}",
                    [("c", "继续进阶段 2 实现"), ("e", "中止让我编辑 plan"), ("a", "中止")],
                )
                if c == "a":
                    print("[abort]")
                    return 1
                if c == "e":
                    print("[pause] 请手动编辑 plan 后, 用 --resume 继续")
                    return 0
        elif stage == "2-impl":
            state, choice = stage_impl(state, cfg, dry_run=args.dry_run)
            if not args.dry_run: save_state(cfg, state)
            if choice == "a":
                print("[abort] 实现 (codex) 失败, 中止")
                return 1
        elif stage == "3b-review":
            state, choice = stage_review(state, cfg, dry_run=args.dry_run)
            if not args.dry_run: save_state(cfg, state)
            if choice == "a":
                return 1
            if state["stage"] == "done":
                print(f"\n[done] STATUS=PASS, iteration={state['iteration']}, 流水线完成")
                return 0
            c = confirm(
                f"阶段 3b 第 {state['iteration']} 轮 review = FAIL, "
                f"open_issues={state['open_issues']}, trend={state['convergence_trend']}",
                [("c", "进阶段 4 fix"), ("a", "中止")],
            )
            if c == "a":
                print("[abort]")
                return 1
        elif stage == "4-fix":
            state, choice = stage_fix(state, cfg, dry_run=args.dry_run)
            if not args.dry_run: save_state(cfg, state)
            if choice == "a":
                print("[abort] 修复 (codex) 失败, 中止")
                return 1
        elif stage == "done":
            print("[done] 流水线已完成")
            return 0
        else:
            print(f"[error] 未知 stage: {stage}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
