#!/usr/bin/env bash
# 启动提词器服务。
#
# 用法：
#   ./run.sh                      正常启动（加载 ASR 模型）
#   ./run.sh --dry                只起网页，不加载模型（调界面用，秒开）
#   ./run.sh --script 稿子.txt    预置稿件并锁定（网页端就不能再改）
#   ./run.sh --device cpu         强制 CPU（会跟不上实时，仅排障用）
#
# 环境变量：
#   PY   指定 python 解释器，默认用 funasr 那个 venv
set -euo pipefail

cd "$(dirname "$0")"

PY="${PY:-$HOME/.venvs/funasr/bin/python}"
if [ ! -x "$PY" ]; then
  echo "❌ 找不到解释器：$PY"
  echo "   它是 FunASR 的环境（funasr + torch + pypinyin 都在里面）。"
  echo "   要么把它建起来，要么用 PY=/path/to/python ./run.sh 指一个装齐依赖的解释器。"
  exit 1
fi

PORT=$("$PY" - <<'EOF'
import json, pathlib
c = json.loads(pathlib.Path("config.json").read_text(encoding="utf-8"))
print(c["http"]["port"], c["ws"]["port"], c["http"]["public_port"], c["ws"]["public_port"])
EOF
)
read -r HTTP_PORT WS_PORT HTTP_PUB WS_PUB <<< "$PORT"

echo "──────────────────────────────────────────────────────────────"
echo " 提词器要用的两个端口已就绪："
echo "   本机  http://127.0.0.1:${HTTP_PORT}/     ws://127.0.0.1:${WS_PORT}/"
echo "   手机  https://<机器>.<tailnet>.ts.net:${HTTP_PUB}/  （需 tailscale serve 映射）"
echo "──────────────────────────────────────────────────────────────"

TS="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
if [ -x "$TS" ]; then
  STATUS="$("$TS" serve status 2>/dev/null || true)"
  if ! grep -q ":${HTTP_PUB}" <<< "$STATUS"; then
    echo "⚠️  tailscale serve 还没映射 ${HTTP_PUB} 端口。手机要用 HTTPS 就得先挂上："
    echo "     $TS serve --bg --https=${HTTP_PUB} http://127.0.0.1:${HTTP_PORT}"
    echo "     $TS serve --bg --https=${WS_PUB} http://127.0.0.1:${WS_PORT}"
    echo "   （--bg 会持久化，重启后不用再挂；tailscale serve reset 可以撤掉）"
    echo "──────────────────────────────────────────────────────────────"
  fi
else
  echo "⚠️  没装 Tailscale。手机端必须 HTTPS：装 Tailscale，或用 mkcert 自签证书。"
  echo "──────────────────────────────────────────────────────────────"
fi

exec "$PY" server/main.py "$@"
