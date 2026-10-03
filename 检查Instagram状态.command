#!/bin/bash
# 检查 Instagram 现在能不能同步 —— 双击运行，不下载任何东西。
# 用来回答"封锁解除了吗"：被限流时同步会把条目下进错误的收藏夹，
# 所以大规模同步前先双击这里看一眼最稳妥。同一时间有同步在跑会提示稍后再查。

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

code=0
"$PY" "$TOOL" probe --output-root "$OUT" || code=$?

echo
case "$code" in
  0) echo "封锁已解除，可以双击「同步Instagram收藏.command」了。" ;;
  1) echo "Instagram 还在封锁本会话（今天请求太多），过几个小时再双击这里查。" ;;
  2) echo "连不上浏览器：先打开专用浏览器窗口（双击同步启动器会自动打开）。" ;;
  3) echo "有同步正在进行，等它跑完再来查。" ;;
  *) echo "意外退出码 ${code}，把上面的输出发给维护者看看。" ;;
esac
echo
read -r -p "按回车关闭窗口..."
