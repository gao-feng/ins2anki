#!/bin/bash
# 同一平台同时只会运行一个同步：重复双击会被拒绝，等它跑完即可。
# Instagram 收藏夹同步 —— 双击运行（原「同步收藏夹.command」）。
#
# 确定性工具：不依赖 AI / skill，不导出 cookie，也不读 macOS 钥匙串，
# 因此不会弹"访问机密信息"的授权框。首次运行会打开一个专用浏览器窗口
# （~/.ins2anki/browser-profile，与你平时的 Edge/Chrome 互不影响），
# 在里面登录一次 Instagram 即可，之后每次双击都是增量同步。

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

TOOL="scripts/browser_sync.py"
OUT="${INS2ANKI_OUTPUT:-$PWD/instagram-saved}"

echo "=== 检查浏览器会话 ==="
if ! "$PY" "$TOOL" check >/dev/null 2>&1; then
  cat <<'TXT'

首次使用：接下来会打开一个专用浏览器窗口。
  1. 在这个窗口里登录 Instagram（只需一次）
  2. 登录成功后回到本窗口按回车

TXT
  "$PY" "$TOOL" launch >/dev/null 2>&1 || true
  read -r -p "登录完成后按回车继续..."
  echo
fi

echo "=== 开始同步（已下载的会自动跳过）==="
# --jobs: 直连 CDN 的并行下载数；签名直链彼此独立，并发是安全的。
if "$PY" "$TOOL" sync --all-collections --launch --jobs "${INS2ANKI_JOBS:-6}" \
     --output-root "$OUT"; then
  echo
  echo "=== 清扫已被视频替代的封面备份 ==="
  "$PY" "$TOOL" repair --output-root "$OUT" --clean
  echo
  echo "完成。文件位置：${OUT}"
  echo "每个收藏夹一个子目录，含 manifest.json 与 sync-state.json。"
  echo "没有可用替代的备份会保留（帖子被删时那是最后一份）。"
else
  code=$?
  echo
  if [ "$code" -eq 3 ]; then
    echo "已经有一个同步在跑，等它结束后再来。"
  else
    echo "有项目没同步成功，退出码 ${code}（上面列出了失败条目和原因）。"
    echo "若上面提到 rate-limit（限流）：什么都没下载，等配额恢复后先用"
    echo "「检查Instagram状态.command」探测，答 ✓ 再双击这里。"
    echo "其它失败：再次双击会自动重试，已下载完的不会重复下载。"
    echo "如果反复失败，运行自诊断看看页面实际请求了什么："
    echo "  python3 ${TOOL} diagnose --launch"
  fi
fi
echo
read -r -p "按回车关闭窗口..."
