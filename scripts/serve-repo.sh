#!/bin/sh
# serve-repo.sh -- 把 repo/out 临时挂成一个静态 HTTP 源，给验收靶机用。
#
#   ./scripts/serve-repo.sh            # 默认 0.0.0.0:8000
#   PORT=8080 ./scripts/serve-repo.sh
#
# 只是验收用的临时脚手架；长期托管是任何静态 HTTPS（对象存储 / Pages / NAS）。

set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
OUT="${OUT:-$ROOT/repo/out}"
PORT="${PORT:-8000}"

[ -d "$OUT" ] || { echo "serve-repo.sh: no $OUT -- run repo/make-repo.sh first" >&2; exit 1; }

echo "serve-repo.sh: serving $OUT on http://0.0.0.0:$PORT"
echo "serve-repo.sh: sources.list line (target machine):"
echo "  deb [signed-by=/etc/apt/keyrings/autonomic-archive-keyring.gpg] http://<HOST-IP>:$PORT stable main"
echo

cd "$OUT"
exec python3 -m http.server "$PORT" --bind 0.0.0.0
