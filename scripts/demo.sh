#!/bin/bash
# demo.sh -- 一条命令说清"Agent 是系统的一个角色，不是一堆脚本"。
#
# 在已装好 autonomic-agent 的目标机上跑：
#   sudo ./scripts/demo.sh
#
# 三段，每段都只做一件事：
#   1. 身份全景   -- 系统怎么看它（用户 / unit / cgroup / 日志 / 世界标记）
#   2. Action 通道 -- 副作用只能从通道里过，计数器单调
#   3. 漂移检测   -- 绕过通道直接改世界，会被当场发现
#
# 第 3 段是整个 M1 里最值得看的一段：计数器住在系统外，系统里只有一份
# **只读投影**。有人绕过去改了它，投影就跟真值对不上 —— 这就是证据。

set -u

[ "$(id -u)" = 0 ] || { echo "demo.sh: needs root (sudo)" >&2; exit 2; }

command -v autonomicctl >/dev/null || {
    echo "demo.sh: autonomicctl not found -- apt install autonomic-agent first" >&2
    exit 2
}

step() { echo; echo "────────────────────────────────────────────"; echo "$1"; echo "────────────────────────────────────────────"; }
wait_tick() { echo "    (waiting for the agent's next tick...)"; sleep 6; }

# ---------------------------------------------------------------- 1
step "1/3  IDENTITY — 系统把它当成一个角色来认识"
autonomicctl status || true

echo
echo "  ps 里它跑在自己的 UID 上:"
ps -o pid,user,comm -u autonomic 2>/dev/null | sed 's/^/    /'
echo
echo "  它的日志有自己的命名空间（系统日志里查不到它）:"
echo "    journalctl --namespace=autonomic -n 3 --no-pager"
journalctl --namespace=autonomic -n 3 --no-pager 2>&1 | sed 's/^/      /'

# ---------------------------------------------------------------- 2
step "2/3  ACTION CHANNEL — 副作用从通道里过，计数器单调"
echo "    before: $(autonomicctl world 2>/dev/null | head -2 | tr '\n' ' ')"
for i in 1 2 3; do
    autonomicctl bump | sed 's/^/    /'
done
wait_tick
echo "    after : $(autonomicctl world 2>/dev/null | head -2 | tr '\n' ' ')"
echo "    -> action_seq 只增不减，且每一次都留在这条通道的记录里"

# ---------------------------------------------------------------- 3
step "3/3  DRIFT DETECTION — 绕过通道改世界，会被当场发现"

WORLD=/run/autonomic/world
echo "    世界标记现在是只读的: $(stat -c '%a' "$WORLD")  $(tr '\n' ' ' < "$WORLD")"
echo
echo "    现在绕过 Action 通道，直接改它（模拟有人手工动过世界）："
chmod 644 "$WORLD"
sed -i 's/^action_seq=.*/action_seq=9999/' "$WORLD"
chmod 444 "$WORLD"
echo "      touched: $(tr '\n' ' ' < "$WORLD")"

wait_tick

echo
echo "    agent 的反应："
if journalctl --namespace=autonomic --since "30 sec ago" --no-pager 2>/dev/null | grep -qi drift; then
    journalctl --namespace=autonomic --since "30 sec ago" --no-pager 2>/dev/null \
        | grep -i drift | tail -2 | sed 's/^/      /'
    echo
    echo "    -> 它不需要信任任何人：投影与真值对不上就是证据。"
else
    echo "      (no drift line yet -- try again, the agent ticks every 5s)"
fi

echo
echo "    而标记已经被它自己纠正回来:"
echo "      now: $(tr '\n' ' ' < "$WORLD")"
echo
echo "────────────────────────────────────────────"
echo "  一个用户、一个 unit、一个 cgroup、一份日志、一个世界版本。"
echo "  这不是一堆脚本，这是系统在册的一个角色。"
echo "────────────────────────────────────────────"
