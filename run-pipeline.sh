#!/usr/bin/env bash
# pipeline wrapper: 临时清理从 root tmux 继承的环境变量, 跑完 pipeline 后复原。
#
# 背景: 当前环境通过 root tmux session 接入, 普通用户 wuhao 的 shell 继承了
# TMUX=/tmp/tmux-0/default, TMUX_SOCKET=/tmp/tmux-0/default 等变量, 导致
# tmux new-session 试图连 root 的 socket (Permission denied)。
#
# 本脚本在子 shell 里跑 pipeline.py, 跑前 unset, 跑完自动复原 (trap EXIT),
# 不影响当前 shell 的 tmux 环境。
#
# 用法:
#   ./run-pipeline.sh <config> [--design-doc <path>] [--resume] [--dry-run]
#   参数透传给 pipeline.py, 例如:
#   ./run-pipeline.sh config.yaml --design-doc docs/xxx.md
#   ./run-pipeline.sh config.yaml --resume
#   ./run-pipeline.sh config.yaml docs/xxx.md --dry-run

set -euo pipefail

# ---------- tmux 环境变量保存/清理/复原 ----------

# 需要保存并复原的变量列表 (空格分隔)
_TMUX_VARS=(
    TMUX
    TMUX_SOCKET
    TMUX_PANE
    TMUX_CONF
    TMUX_CONF_LOCAL
    TMUX_PLUGIN_MANAGER_PATH
)

# 保存: 把每个变量的当前值存到 _SAVE_<name>
_tmux_save() {
    for v in "${_TMUX_VARS[@]}"; do
        eval "_SAVE_${v}=\"\${${v}:-}\""
    done
}

# 清理: unset 所有 tmux 变量, 让 tmux 用当前用户默认 socket
_tmux_clear() {
    unset "${_TMUX_VARS[@]}" || true
}

# 复原: 把保存的值写回 (空值也算, 还原成 unset 前的原本状态)
# 用 eval 内嵌 export, 保证变量既被赋值又被导出到子进程
_tmux_restore() {
    local v val
    for v in "${_TMUX_VARS[@]}"; do
        eval "val=\"\${_SAVE_${v}:-}\""
        if [[ -n "$val" ]]; then
            eval "export ${v}=\"\${_SAVE_${v}}\""
        else
            # 原本就是空的, 保持 unset 状态
            unset "$v" || true
        fi
    done
}

# ---------- 主逻辑 ----------

# 必须传至少一个参数 (config 路径)
if [[ $# -lt 1 ]]; then
    echo "用法: $0 <config> [--design-doc <path>] [--resume] [--dry-run]" >&2
    exit 2
fi

CONFIG="$1"; shift

# pipeline.py 路径 = 本脚本同目录下的 pipeline.py
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_PY="${SCRIPT_DIR}/pipeline.py"

if [[ ! -f "$PIPELINE_PY" ]]; then
    echo "[error] 找不到 $PIPELINE_PY" >&2
    exit 2
fi

# 保存当前 tmux 环境
_tmux_save

# 复原钩子: 正常退出 / Ctrl-C / 被杀都触发
_trap_action() {
    local code=$?
    _tmux_restore
    exit $code
}
trap _trap_action EXIT
trap _trap_action INT TERM

# 清理 tmux 变量, 跑 pipeline
_tmux_clear
echo "[wrapper] 已临时清理 tmux 环境变量, 跑完自动复原"
echo "[wrapper] config: $CONFIG"
echo "[wrapper] args:   $*"
echo

python3 "$PIPELINE_PY" "$CONFIG" "$@"
