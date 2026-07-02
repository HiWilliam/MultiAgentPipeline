"""pipeline.py 单元测试。

覆盖设计文档 §7.2 的 7 组测试:
- tmux_session 命名/残留清理/检测 tmux 缺失
- heredoc 分隔符冲突检测
- InterruptHub 非 tty 跳过 / tmux C-c / plain event / mark_external
- run_in_tmux_pane 推断 130 / 正常读 exit_path / 超时 124
- _run_cli 分发 tmux/plain / 默认 plain / dry-run 跳过 / 异常清理 / tmux 缺失 / 日志写失败
"""

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# 让测试能 import pipeline
sys.path.insert(0, str(Path(__file__).parent.parent))
import pipeline  # noqa: E402


# ---------- 测试 1: tmux_session 命名与残留清理 ----------

@patch("pipeline.shutil.which", return_value="/usr/bin/tmux")
@patch("pipeline.subprocess.run")
def test_tmux_session_creates_with_label(mock_run, mock_which):
    # has-session 返回非零 (不存在), new-session 返回零
    mock_run.side_effect = [
        MagicMock(returncode=1),  # has-session
        MagicMock(returncode=0),  # new-session
    ]
    pipeline.tmux_session("codex", Path("/tmp"))
    # 第二次调用应是 new-session
    new_session_call = mock_run.call_args_list[1]
    assert "new-session" in new_session_call.args[0]
    assert "-s" in new_session_call.args[0]
    assert "pipe-codex" in new_session_call.args[0]
    assert "-c" in new_session_call.args[0]


@patch("pipeline.shutil.which", return_value="/usr/bin/tmux")
@patch("pipeline.subprocess.run")
def test_tmux_session_kills_stale(mock_run, mock_which):
    mock_run.side_effect = [
        MagicMock(returncode=0),  # has-session (存在)
        MagicMock(returncode=0),  # kill-session
        MagicMock(returncode=0),  # new-session
    ]
    pipeline.tmux_session("codex", Path("/tmp"))
    assert "kill-session" in mock_run.call_args_list[1].args[0]
    assert "new-session" in mock_run.call_args_list[2].args[0]


# ---------- 测试 2: tmux_session 检测 tmux 缺失 ----------

@patch("pipeline.shutil.which", return_value=None)
def test_tmux_session_raises_when_no_tmux(mock_which):
    with pytest.raises(RuntimeError, match="未找到 tmux"):
        pipeline.tmux_session("codex", Path("/tmp"))


# ---------- 测试 parse_plan_steps ----------

def test_parse_plan_steps_parses_real_plan(tmp_path):
    """用真实 001-plan.md 解析, 应得 5 个步骤, 标题含步骤号。"""
    plan = Path(__file__).parent.parent / "pipeline" / "artifacts" / "001-plan.md"
    if not plan.exists():
        pytest.skip("001-plan.md 不存在")
    steps = pipeline.parse_plan_steps(plan)
    assert len(steps) == 5
    assert steps[0][0] == 1  # step_no
    assert "tmux_session" in steps[0][1]  # title
    assert "tmux_session" in steps[0][2] or "shutil.which" in steps[0][2]  # body 含内容


def test_parse_plan_steps_handles_missing_impl_section(tmp_path):
    """plan 无 ## 实现步骤 段 → 返回空列表。"""
    p = tmp_path / "plan.md"
    p.write_text("# 某设计\n\n## 别的\n内容\n")
    assert pipeline.parse_plan_steps(p) == []


def test_parse_plan_steps_stops_at_next_h2(tmp_path):
    """步骤解析应在下一个一级 ## 标题前停止, 不混入「plan 内部一致性自检」段。"""
    p = tmp_path / "plan.md"
    p.write_text(
        "# plan\n\n## 实现步骤\n\n"
        "### 步骤 1: foo\n- 目标: a\n\n"
        "### 步骤 2: bar\n- 目标: b\n\n"
        "## plan 内部一致性自检\n- 不该被解析进步骤\n"
    )
    steps = pipeline.parse_plan_steps(p)
    assert len(steps) == 2
    assert steps[0][0] == 1
    assert "foo" in steps[0][1]
    assert "目标: a" in steps[0][2]
    assert steps[1][0] == 2
    assert "目标: b" in steps[1][2]
    # body 不应混入自检段
    assert "不该被解析进步骤" not in steps[1][2]


def test_parse_plan_steps_no_steps_returns_empty(tmp_path):
    """有 ## 实现步骤 段但无 ### 步骤 N 标题 → 空列表。"""
    p = tmp_path / "plan.md"
    p.write_text("# plan\n\n## 实现步骤\n\n一些描述但没有步骤标题\n")
    assert pipeline.parse_plan_steps(p) == []


# ---------- 测试 3: heredoc 分隔符冲突检测 ----------

def test_make_heredoc_delimiter_rejects_prompt_with_delimiter():
    # 模拟生成分隔符后 prompt 含该字面量
    import uuid as _uuid_mod
    with patch.object(_uuid_mod, "uuid4") as mock_uuid:
        mock_uuid.return_value.hex = "abcd1234"
        prompt = f"normal text\nPROMPT_EOF_abcd1234\nmore text"
        with pytest.raises(ValueError, match="prompt 含 heredoc 分隔符"):
            pipeline._make_heredoc_delimiter(prompt)


def test_make_heredoc_delimiter_ok_when_no_conflict():
    import uuid as _uuid_mod
    with patch.object(_uuid_mod, "uuid4") as mock_uuid:
        mock_uuid.return_value.hex = "abcd1234"
        prompt = "normal text without delimiter"
        delim = pipeline._make_heredoc_delimiter(prompt)
        assert delim == "PROMPT_EOF_abcd1234"


# ---------- 测试 4: InterruptHub 在非 tty 下不启动监听 ----------

def test_interrupt_hub_skips_when_not_tty(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    hub = pipeline.InterruptHub("tmux", "pipe-codex")
    hub.start()
    # 监听线程未启动
    assert hub._listener_thread is None
    # 未触发
    assert hub.is_triggered() is False
    # 打印了 warn
    captured = capsys.readouterr()
    assert "[warn]" in captured.out


# ---------- 测试 5: InterruptHub trigger 行为 ----------

@patch("pipeline.subprocess.run")
def test_interrupt_hub_trigger_tmux_sends_cc(mock_run):
    hub = pipeline.InterruptHub("tmux", "pipe-codex")
    hub.trigger()
    assert hub.is_triggered() is True
    send_call = mock_run.call_args_list[0]
    assert "send-keys" in send_call.args[0]
    assert "pipe-codex" in send_call.args[0]
    assert "C-c" in send_call.args[0]


def test_interrupt_hub_trigger_plain_sets_event():
    ev = threading.Event()
    hub = pipeline.InterruptHub("plain", None, interrupt_event=ev)
    hub.trigger()
    assert hub.is_triggered() is True
    assert ev.is_set() is True


def test_interrupt_hub_mark_external_interrupt():
    hub = pipeline.InterruptHub("tmux", "pipe-codex")
    hub.mark_external_interrupt()
    assert hub.is_triggered() is True


# ---------- 测试 6: _read_exit_path / run_in_tmux_pane 轮询推断 ----------

def test_read_exit_path_returns_none_when_missing(tmp_path):
    assert pipeline._read_exit_path(tmp_path / "nonexistent.json") is None


def test_read_exit_path_reads_returncode(tmp_path):
    p = tmp_path / "exit.json"
    p.write_text(json.dumps({"returncode": 0}))
    assert pipeline._read_exit_path(p) == 0


def test_pane_runner_infers_interrupt_when_session_dead_without_exit_path(tmp_path):
    """mock 轮询探测函数: 第一轮 exit_path 不存在 + tmux_session_alive 返回 False → 130。"""
    log_path = tmp_path / "last.log"
    exit_path = tmp_path / "exit.json"  # 不创建
    with patch("pipeline._probe_exit_path_exists", return_value=False):
        with patch("pipeline.tmux_session_alive", return_value=False):
            with patch("pipeline.subprocess.run"):
                code, _ = pipeline.run_in_tmux_pane(
                    "pipe-codex", ["codex"], "prompt", "codex",
                    log_path, exit_path, timeout=60,
                )
    assert code == 130


def test_pane_runner_rejects_prompt_with_delimiter(tmp_path):
    """prompt 含生成的分隔符字面量 → run_in_tmux_pane 抛 ValueError。"""
    import uuid as _uuid_mod
    log_path = tmp_path / "last.log"
    exit_path = tmp_path / "exit.json"
    with patch.object(_uuid_mod, "uuid4") as mock_uuid:
        mock_uuid.return_value.hex = "deadbeef"
        prompt = f"normal text\nPROMPT_EOF_deadbeef\nmore text"
        with pytest.raises(ValueError, match="prompt 含 heredoc 分隔符"):
            pipeline.run_in_tmux_pane(
                "pipe-codex", ["codex"], prompt, "codex",
                log_path, exit_path, timeout=60,
            )


def test_pane_runner_reads_exit_path_when_normal(tmp_path):
    """mock 轮询探测函数: 第一轮 exit_path 存在且内容 {"returncode":0} → 返回 0。"""
    exit_path = tmp_path / "exit.json"
    exit_path.write_text(json.dumps({"returncode": 0}))
    log_path = tmp_path / "last.log"
    with patch("pipeline._probe_exit_path_exists", return_value=True):
        with patch("pipeline._read_exit_path", return_value=0):
            with patch("pipeline.subprocess.run"):
                code, _ = pipeline.run_in_tmux_pane(
                    "pipe-codex", ["codex"], "prompt", "codex",
                    log_path, exit_path, timeout=60,
                )
    assert code == 0


def test_pane_runner_timeout_returns_124(tmp_path):
    """mock 超时触发: exit_path 始终不存在 + session 始终存活 → 124,
    且调用了 tmux send-keys C-c 与最终 tmux kill-session。"""
    log_path = tmp_path / "last.log"
    exit_path = tmp_path / "exit.json"
    send_keys_calls = []

    def fake_subprocess_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args")
        if "send-keys" in cmd and "C-c" in cmd:
            send_keys_calls.append(cmd)
        return MagicMock(returncode=0)

    # time.time: 用单调递增 side_effect, 每次调用 +1。首次调用算 deadline
    # (deadline = t0 + timeout), 后续调用都 > deadline, 主循环与 grace 循环都立即
    # 过期退出。不耦合精确调用次数 (永不会 StopIteration), 实现侧增减 time.time
    # 调用都不影响断言。
    t_counter = [0.0]
    def fake_time():
        t_counter[0] += 1.0
        return t_counter[0]

    with patch("pipeline._probe_exit_path_exists", return_value=False):
        with patch("pipeline.tmux_session_alive", return_value=True):
            with patch("pipeline.tmux_kill") as mock_kill:
                with patch("pipeline.subprocess.run", side_effect=fake_subprocess_run):
                    with patch("pipeline.time.time", side_effect=fake_time):
                        with patch("pipeline.time.sleep", return_value=None):
                            code, _ = pipeline.run_in_tmux_pane(
                                "pipe-codex", ["codex"], "prompt", "codex",
                                log_path, exit_path, timeout=0,
                            )
    assert code == 124
    assert any("send-keys" in c and "C-c" in c for c in send_keys_calls), \
        f"未调用 send-keys C-c, calls={send_keys_calls}"
    assert mock_kill.called, "未调用 tmux_kill"


# ---------- 测试 7: _run_cli 分发逻辑 ----------

def _make_cfg(mode="plain"):
    return {"observe_mode": mode, "clis": {"claude": {"cmd": "claude"}, "codex": {"cmd": "codex"}}}


@patch("pipeline.tmux_kill")
@patch("pipeline.tmux_session", return_value="pipe-codex")
@patch("pipeline.run_in_tmux_pane", return_value=(0, None))
def test_run_cli_dispatches_tmux(mock_pane, mock_session, mock_kill, tmp_path):
    cfg = _make_cfg("tmux")
    code, _ = pipeline._run_cli(
        ["codex"], tmp_path, "prompt", dry_run=False, label="codex", cfg=cfg,
    )
    assert mock_session.called
    assert mock_pane.called
    assert code == 0


@patch("pipeline.run_plain", return_value=(0, None))
def test_run_cli_dispatches_plain(mock_plain, tmp_path):
    cfg = _make_cfg("plain")
    pipeline._run_cli(["codex"], tmp_path, "prompt", dry_run=False, label="codex", cfg=cfg)
    assert mock_plain.called


@patch("pipeline.run_plain", return_value=(0, None))
def test_run_cli_defaults_to_plain_when_mode_absent(mock_plain, tmp_path):
    cfg = {}  # 无 observe_mode
    pipeline._run_cli(["codex"], tmp_path, "prompt", dry_run=False, label="codex", cfg=cfg)
    assert mock_plain.called


@patch("pipeline.tmux_session")
def test_run_cli_dry_run_skips_all(mock_session, tmp_path):
    cfg = _make_cfg("tmux")
    code, out = pipeline._run_cli(
        ["codex"], tmp_path, "prompt", dry_run=True, label="codex", cfg=cfg,
    )
    assert not mock_session.called
    assert code == 0
    assert "dry-run" in out


@patch("pipeline.tmux_kill")
@patch("pipeline.tmux_session", return_value="pipe-codex")
def test_run_cli_cleans_up_on_exception(mock_session, mock_kill, tmp_path):
    """run_in_tmux_pane 抛 RuntimeError, finally 块应调 tmux_kill + 清 exit_path。"""
    cfg = _make_cfg("tmux")
    # 预先放一个 exit_path 文件, 验证 finally 块会 unlink 它
    exit_path = tmp_path / "pipeline" / "exit-codex.json"
    exit_path.parent.mkdir(parents=True, exist_ok=True)
    exit_path.write_text('{"returncode":0}')
    assert exit_path.exists()

    with patch("pipeline.run_in_tmux_pane", side_effect=RuntimeError("boom")):
        with patch("pipeline.InterruptHub"):
            with pytest.raises(RuntimeError):
                pipeline._run_cli(
                    ["codex"], tmp_path, "prompt", dry_run=False,
                    label="codex", cfg=cfg,
                )
    # finally 块应调用 tmux_kill (至少一次)
    assert mock_kill.called
    # exit_path 应被 finally 清理掉
    assert not exit_path.exists()


@patch("pipeline.tmux_session", side_effect=RuntimeError("未找到 tmux"))
def test_run_cli_tmux_unavailable_returns_nonzero(mock_session, tmp_path):
    cfg = _make_cfg("tmux")
    code, out = pipeline._run_cli(
        ["codex"], tmp_path, "prompt", dry_run=False, label="codex", cfg=cfg,
    )
    assert code == 1
    assert out == ""


def test_run_cli_log_write_failure_does_not_kill_agent(tmp_path):
    """run_plain 返回 (0, None), log_path 已存在但 read_text 抛 OSError。
    退化语义 (§6.9): code 仍是 agent 返回值 0, stdout_text 退化为空串, 不向外抛。"""
    cfg = _make_cfg("plain")
    # 让 log_path 是一个目录而非文件: exists() 返回 True (触发 read_text 分支),
    # 但 read_text() 抛 IsADirectoryError (OSError 子类)。真实构造 OSError 触发点,
    # 不全局 patch pathlib.Path.read_text (会误伤 pytest 内部对 Path.read_text 的调用)。
    log_path = tmp_path / "pipeline" / "last-codex.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.mkdir()
    assert log_path.exists()

    with patch("pipeline.run_plain", return_value=(0, None)):
        code, out = pipeline._run_cli(
            ["codex"], tmp_path, "prompt", dry_run=False, label="codex", cfg=cfg,
        )
    # code 仍是 run_plain 的返回值 0 (agent 已正常结束), stdout_text 退化为 ""
    assert code == 0
    assert out == ""


# ---------- 测试: run_plain (单元 3) ----------

def test_run_plain_streams_output_to_log(tmp_path):
    """run_plain 跑 echo 类命令, 断言 log_path 含输出、返回码 0。"""
    log_path = tmp_path / "plain-echo.log"
    interrupt_event = threading.Event()
    rc, returned_log = pipeline.run_plain(
        cmd=["bash", "-c", "echo hello-plain-output"],
        prompt="",
        label="echo",
        log_path=log_path,
        cwd=str(tmp_path),
        timeout=10,
        interrupt_event=interrupt_event,
    )
    assert rc == 0
    assert returned_log == log_path
    content = log_path.read_text()
    assert "hello-plain-output" in content


def test_run_plain_sends_sigint_on_interrupt_event(tmp_path):
    """run_plain 跑 sleep, 启动后 set interrupt_event, 断言返回码非 0 (被 SIGINT)。"""
    log_path = tmp_path / "plain-sleep.log"
    interrupt_event = threading.Event()

    # 在独立线程里 set event, 确保进程已启动后再触发
    def _fire():
        time.sleep(0.3)
        interrupt_event.set()

    threading.Thread(target=_fire, daemon=True).start()

    rc, _ = pipeline.run_plain(
        cmd=["bash", "-c", "sleep 30"],
        prompt="",
        label="sleep",
        log_path=log_path,
        cwd=str(tmp_path),
        timeout=15,
        interrupt_event=interrupt_event,
    )
    # event set → run_plain 发 SIGINT → sleep 被 INT 杀死, rc 非 0
    assert rc != 0


def test_run_plain_sends_sigint_on_interrupt_event_direct(tmp_path):
    """sleep 不忽略 SIGINT, event 触发后进程被 INT 杀死, rc 非 0。"""
    log_path = tmp_path / "plain-sleep2.log"
    interrupt_event = threading.Event()

    def _fire():
        time.sleep(0.3)
        interrupt_event.set()

    threading.Thread(target=_fire, daemon=True).start()

    rc, _ = pipeline.run_plain(
        cmd=["bash", "-c", "sleep 30"],
        prompt="",
        label="sleep2",
        log_path=log_path,
        cwd=str(tmp_path),
        timeout=15,
        interrupt_event=interrupt_event,
    )
    assert rc != 0


def test_run_plain_timeout_returns_124(tmp_path):
    """run_plain 跑 sleep 5, timeout=1, 断言返回码 124。"""
    log_path = tmp_path / "plain-timeout.log"
    interrupt_event = threading.Event()
    rc, _ = pipeline.run_plain(
        cmd=["bash", "-c", "sleep 5"],
        prompt="",
        label="timeout",
        log_path=log_path,
        cwd=str(tmp_path),
        timeout=1,
        interrupt_event=interrupt_event,
    )
    assert rc == 124


# ---------- 测试 8: stage_review stage 推进 ----------

def _make_full_cfg(tmp_path):
    """构造跑 stage_review 所需的完整 cfg (含 _root/_artifacts_dir/_prompts_dir 等)。"""
    (tmp_path / "prompts").mkdir(exist_ok=True)
    (tmp_path / "prompts" / "03-review.md").write_text(
        "{{plan_artifact_name}} {{last_fix_artifact}} {{review_artifact_name}} "
        "{{iteration}} {{prev_blocker_major}} {{prev_categories}}"
    )
    artifacts = tmp_path / "pipeline" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    return {
        "_root": tmp_path,
        "_artifacts_dir": artifacts,
        "_prompts_dir": tmp_path / "prompts",
        "_state_file": tmp_path / "pipeline" / "state.json",
        "artifacts_dir": "pipeline/artifacts",
        "max_iterations": 5,
        "pass_marker": "STATUS: PASS",
    }


@patch("pipeline.run_claude", return_value=(0, None))
def test_stage_review_fail_sets_stage_to_4fix(mock_claude, tmp_path):
    """review=FAIL 时, stage_review 应把 state.stage 设为 4-fix (回归 Bug A)。"""
    cfg = _make_full_cfg(tmp_path)
    state = {
        "stage": "3b-review", "iteration": 1, "plan_artifact": "pipeline/artifacts/001-plan.md",
        "last_review": None, "last_fix": None,
        "open_issues": {}, "issue_history": [], "convergence_trend": "none",
    }
    # stage_review 内部用 next_artifact_name 算 review 文件名, 这里同步算出再写
    review_name = pipeline.next_artifact_name(cfg, "review")
    review_path = cfg["_artifacts_dir"] / review_name
    def _write_fail_review(*a, **kw):
        review_path.write_text(
            "STATUS: FAIL\n\n## 收敛趋势\n- 本轮: 1\n- 上轮: N/A\n- 趋势: none\n\n"
            "## 问题清单\n### [BLOCKER-1] x\n- 类别: 代码bug\n### [MAJOR-2] y\n- 类别: 代码bug\n"
        )
        return (0, None)
    mock_claude.side_effect = _write_fail_review

    new_state, choice = pipeline.stage_review(state, cfg, dry_run=False)
    assert choice == "c"
    assert new_state["stage"] == "4-fix", f"FAIL 路径应推进到 4-fix, 实际 {new_state['stage']}"
    assert new_state["iteration"] == 2
    assert new_state["last_review"].endswith(review_name)
    assert new_state["open_issues"] == {"blocker": 1, "major": 1, "minor": 0}


@patch("pipeline.run_claude", return_value=(0, None))
def test_stage_review_pass_sets_stage_to_done(mock_claude, tmp_path):
    """review=PASS 时, stage_review 应把 state.stage 设为 done。"""
    cfg = _make_full_cfg(tmp_path)
    state = {
        "stage": "3b-review", "iteration": 1, "plan_artifact": "pipeline/artifacts/001-plan.md",
        "last_review": None, "last_fix": None,
        "open_issues": {}, "issue_history": [], "convergence_trend": "none",
    }
    review_name = pipeline.next_artifact_name(cfg, "review")
    review_path = cfg["_artifacts_dir"] / review_name
    def _write_pass_review(*a, **kw):
        review_path.write_text("STATUS: PASS\n\n## 收敛趋势\n- 本轮: 0\n- 趋势: none\n")
        return (0, None)
    mock_claude.side_effect = _write_pass_review

    new_state, choice = pipeline.stage_review(state, cfg, dry_run=False)
    assert choice == "c"
    assert new_state["stage"] == "done"
    assert new_state["open_issues"] == {"blocker": 0, "major": 0, "minor": 0}


@patch("pipeline.run_claude", return_value=(0, None))
def test_stage_review_fail_at_max_iterations_aborts(mock_claude, tmp_path):
    """iteration 已达 max_iterations, FAIL 路径应返回 a (中止) 而非进 4-fix。"""
    cfg = _make_full_cfg(tmp_path)
    cfg["max_iterations"] = 2
    state = {
        "stage": "3b-review", "iteration": 1, "plan_artifact": "pipeline/artifacts/001-plan.md",
        "last_review": None, "last_fix": None,
        "open_issues": {}, "issue_history": [], "convergence_trend": "none",
    }
    review_name = pipeline.next_artifact_name(cfg, "review")
    review_path = cfg["_artifacts_dir"] / review_name
    def _write_fail_review(*a, **kw):
        review_path.write_text(
            "STATUS: FAIL\n\n### [BLOCKER-1] x\n- 类别: 代码bug\n"
        )
        return (0, None)
    mock_claude.side_effect = _write_fail_review

    new_state, choice = pipeline.stage_review(state, cfg, dry_run=False)
    # iteration 从 1 涨到 2, == max_iterations=2 → 走 stall 中止
    assert choice == "a"
    assert new_state["iteration"] == 2

