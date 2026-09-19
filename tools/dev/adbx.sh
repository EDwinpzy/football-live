#!/usr/bin/env bash
# 稳定 adb 包装。
# 起因（2026-09-19）：本机 adb daemon 反复出现 "daemon not running; starting now" 重启，
# 导致单条 adb 命令经常超时或报 device offline；这里统一串起 server 启动 + 设备等待 + 重试。
#
# 用法：
#   bash tools/dev/adbx.sh shell "wm size"
#   bash tools/dev/adbx.sh shot <本地png路径>
#   bash tools/dev/adbx.sh install <apk路径>
#   bash tools/dev/adbx.sh <任意 adb 子命令...>
#
# 设备选择：环境变量 OTV_SERIAL（默认 emulator-5554）。
#   TV 模拟器通常是 emulator-5556，用法：OTV_SERIAL=emulator-5556 bash tools/dev/adbx.sh ...
#
# adb 定位顺序：PATH → $ANDROID_HOME/platform-tools → $ANDROID_SDK_ROOT/platform-tools
#   → 常见默认安装路径（Windows: %LOCALAPPDATA%/Android/Sdk）。
# 找不到 adb 时直接报错退出，不静默失败。

set -u

# --- 定位 adb ---------------------------------------------------------------
# 依赖 adb 子命令之外的工具都能找到 adb；这里只负责把 platform-tools 塞进 PATH。
_adb_dir() {
  # 已在 PATH 里就直接用
  command -v adb >/dev/null 2>&1 && return 0
  # 注意：Windows 下 $LOCALAPPDATA 是反斜杠形式（C:\Users\x\AppData\Local）。
  # 而且 Git Bash 的 command -v 不认 "C:/..." 形式的 PATH 项，必须转成 "/c/..."。
  _cands=""
  [ -n "${ANDROID_HOME:-}" ]         && _cands="$_cands ${ANDROID_HOME}/platform-tools"
  [ -n "${ANDROID_SDK_ROOT:-}" ]     && _cands="$_cands ${ANDROID_SDK_ROOT}/platform-tools"
  [ -n "${LOCALAPPDATA:-}" ]         && _cands="$_cands ${LOCALAPPDATA}/Android/Sdk/platform-tools"
  _cands="$_cands $HOME/Android/Sdk/platform-tools"
  for _d in $_cands; do
    # 反斜杠 → 正斜杠（用参数展开，避免 tr '\\' 的可移植性警告）
    _d="${_d//\\//}"
    # "C:/x" → "/c/x"（Git Bash / MSYS 需要这种形式才能被 PATH 解析）
    case "$_d" in
      [A-Za-z]:/*)
        _drive=$(printf '%s' "${_d%%:*}" | tr 'A-Z' 'a-z')
        _d="/${_drive}${_d#*:}"
        ;;
    esac
    if [ -x "$_d/adb" ] || [ -x "$_d/adb.exe" ]; then
      PATH="$PATH:$_d"
      export PATH
      return 0
    fi
  done
  return 1
}

if ! _adb_dir; then
  echo "未找到 adb。请把 platform-tools 加进 PATH，或设置 ANDROID_HOME / ANDROID_SDK_ROOT。" >&2
  exit 127
fi

SERIAL=${OTV_SERIAL:-emulator-5554}

_ensure() {
  for _ in 1 2 3 4 5 6 7 8; do
    if [ "$(adb devices 2>/dev/null | grep -c "^${SERIAL}[[:space:]]*device")" = "1" ]; then
      return 0
    fi
    adb start-server >/dev/null 2>&1
    sleep 3
  done
  echo "设备 $SERIAL 未就绪（可用 adb devices 查看当前在线设备）" >&2
  return 1
}

case "${1:-}" in
  shot)
    [ $# -ge 2 ] || { echo "用法: adbx.sh shot <本地png路径>" >&2; exit 2; }
    _ensure || exit 1
    for _ in 1 2 3; do
      if adb -s "$SERIAL" exec-out screencap -p > "$2" 2>/dev/null && [ -s "$2" ]; then
        exit 0
      fi
      sleep 3
    done
    echo "截图失败" >&2; exit 1 ;;
  install)
    [ $# -ge 2 ] || { echo "用法: adbx.sh install <apk路径>" >&2; exit 2; }
    _ensure || exit 1
    adb -s "$SERIAL" install -r -d "$2" ;;
  shell)
    [ $# -ge 2 ] || { echo "用法: adbx.sh shell \"<命令>\"" >&2; exit 2; }
    _ensure || exit 1
    adb -s "$SERIAL" shell "$2" ;;
  "")
    echo "用法: adbx.sh {shell|shot|install|<adb子命令>} ..." >&2; exit 2 ;;
  *)
    _ensure || exit 1
    adb -s "$SERIAL" "$@" ;;
esac
