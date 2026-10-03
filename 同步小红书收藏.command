#!/bin/bash
# 小红书收藏同步 —— 双击运行（抖音把 INS2ANKI_PLATFORM=douyin 换一下即可）。
#
# 确定性工具：不依赖 AI / skill，不导出 cookie，也不读 macOS 钥匙串，
# 因此不会弹"访问机密信息"的授权框。首次运行会打开一个专用浏览器窗口
# （~/.ins2anki/browser-profile，与你平时的 Edge/Chrome 互不影响），
# 在里面登录一次小红书即可；之后每次双击都是增量同步，已下载的自动跳过。
#
# 可覆盖的环境变量：
#   INS2ANKI_PLATFORM   xiaohongshu（默认）或 douyin
#   INS2ANKI_OUTPUT     输出目录（默认 xhs-saved/收藏）
#   INS2ANKI_FOLDER     只同步某个收藏夹（侧栏里的名字）
#   INS2ANKI_JOBS       并行下载数（默认 4）
#   INS2ANKI_BROWSER    指定浏览器可执行文件

set -uo pipefail
cd "$(dirname "$0")" || exit 1

PY=""
for candidate in ".codex-work/venv/bin/python" "$(command -v python3)"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "找不到可用的 python3，请先安装 Python 3.10+。"
  read -r -p "按回车关闭窗口..."
  exit 1
fi

TOOL="scripts/favorites_sync.py"
PLATFORM="${INS2ANKI_PLATFORM:-xiaohongshu}"
OUT="${INS2ANKI_OUTPUT:-$PWD/xhs-saved/收藏}"
ARGS=(--platform "$PLATFORM")
[ -n "${INS2ANKI_BROWSER:-}" ] && ARGS+=(--browser "$INS2ANKI_BROWSER")
# --folder only exists on `sync`; check/launch must not receive it.
SYNC_ARGS=("${ARGS[@]}")
[ -n "${INS2ANKI_FOLDER:-}" ] && SYNC_ARGS+=(--folder "$INS2ANKI_FOLDER")

echo "=== 检查浏览器会话 ==="
if ! "$PY" "$TOOL" check "${ARGS[@]}" >/dev/null 2>&1; then
  cat <<'TXT'

首次使用：接下来会打开一个专用浏览器窗口。
  1. 在这个窗口里登录（只需一次，之后一直有效）
  2. 进入「我 → 收藏」，能看到收藏列表
  3. 回到本窗口按回车

TXT
  "$PY" "$TOOL" launch "${ARGS[@]}" >/dev/null 2>&1 || true
  read -r -p "登录完成后按回车继续..."
  echo
fi

echo "=== 开始同步（已下载的会自动跳过）==="
if "$PY" "$TOOL" sync "${SYNC_ARGS[@]}" --output-dir "$OUT" --jobs "${INS2ANKI_JOBS:-4}"; then
  echo
  echo "完成。文件位置：${OUT}"
  echo "每条收藏一个子目录，含 manifest.json；sync-state.json 记录增量状态。"
else
  code=$?
  echo
  echo "有项目没同步成功，退出码 ${code}（上面列出了失败条目和原因）。"
  echo "再次双击会自动重试：已下载完的文件不会重复下载。"
  echo "如果一条都抓不到，运行自诊断看看页面实际请求了什么："
  echo "  python3 ${TOOL} diagnose --platform ${PLATFORM}"
fi
echo
read -r -p "按回车关闭窗口..."
