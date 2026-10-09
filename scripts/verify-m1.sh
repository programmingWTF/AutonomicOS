#!/bin/bash
# verify-m1.sh -- 在**原厂 Ubuntu** 上逐条验证 M1 验收标准。
#
# 用法（在目标机上）：
#   sudo ./scripts/verify-m1.sh [REPO_URL]
#   REPO_URL 默认 http://127.0.0.1:8000  （即仓库被静态托管出来的那个地址）
#
# 它做完整的一圈：加源 -> 装 -> 验身份 -> 卸载 -> 验清洁。
# 退出码 0 = 全部通过。

set -u

REPO_URL="${1:-${REPO_URL:-http://127.0.0.1:8000}}"
SUITE="${SUITE:-stable}"
COMPONENT="${COMPONENT:-main}"
KEYRING=/etc/apt/keyrings/autonomic-archive-keyring.gpg
LIST=/etc/apt/sources.list.d/autonomic.list
PKG=autonomic-agent

PASS=0
FAIL=0

ok()   { echo "  PASS  $1"; PASS=$((PASS + 1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL + 1)); }
head1() { echo; echo "== $1"; }

require_root() {
    [ "$(id -u)" = 0 ] || { echo "verify-m1.sh: needs root (sudo)" >&2; exit 2; }
}

# ---------------------------------------------------------------- 1. 加源
head1 "1. one line in sources.list, apt update"

if [ -f "$KEYRING" ]; then
    ok "signing key already installed at $KEYRING"
else
    # 仓库自带公钥；真实部署时它由带外渠道分发（HTTPS / 包 / 人）
    if [ -f /srv/autonomic-repo/autonomic-archive-keyring.gpg ]; then
        install -d -m 0755 /etc/apt/keyrings
        install -m 0644 /srv/autonomic-repo/autonomic-archive-keyring.gpg "$KEYRING"
        ok "installed signing key from the repo"
    else
        bad "no signing key: put the archive key at $KEYRING"
        exit 1
    fi
fi

echo "deb [signed-by=$KEYRING] $REPO_URL $SUITE $COMPONENT" > "$LIST"
echo "    -> $(cat "$LIST")"

if apt-get update -o Dir::Etc::sourcelist="$LIST" \
                  -o Dir::Etc::sourceparts=/dev/null \
                  -o APT::Get::List-Cleanup=0 2>&1 | sed 's/^/    /' | grep -qiE "signature|not signed|NO_PUBKEY"; then
    bad "apt rejected the repository signature"
else
    ok "apt update accepted the signed repository"
fi

apt-cache policy "$PKG" 2>/dev/null | grep -q "Candidate" \
    && ok "apt sees $PKG: $(apt-cache policy $PKG | awk '/Candidate:/{print $2}')" \
    || bad "apt cannot see $PKG in the repository"

# ---------------------------------------------------------------- 2. 安装
head1 "2. apt install"

DEBIAN_FRONTEND=noninteractive apt-get install -y "$PKG" >/tmp/apt-install.log 2>&1 \
    && ok "apt install $PKG" || { bad "apt install failed"; tail -20 /tmp/apt-install.log; }

# ---------------------------------------------------------------- 3. 身份
head1 "3. the agent has an identity, not just a process"

if getent passwd autonomic >/dev/null; then
    ok "system user: $(getent passwd autonomic)"
else
    bad "no system user 'autonomic'"
fi

if systemctl is-active --quiet autonomic-agent.service; then
    ok "unit active: $(systemctl is-active autonomic-agent.service) ($(systemctl show -p SubState --value autonomic-agent.service))"
else
    bad "unit not active: $(systemctl is-active autonomic-agent.service 2>&1)"
fi

PID=$(systemctl show -p MainPID --value autonomic-agent.service 2>/dev/null || echo 0)
if [ "${PID:-0}" -gt 0 ]; then
    PUID=$(awk '/^Uid:/{print $2}' /proc/$PID/status 2>/dev/null)
    AUID=$(id -u autonomic)
    [ "$PUID" = "$AUID" ] \
        && ok "runs as its own uid (pid=$PID uid=$PUID)" \
        || bad "runs as uid=$PUID, expected $AUID"
else
    bad "no MainPID"
fi

CG=$(systemctl show -p ControlGroup --value autonomic-agent.service 2>/dev/null)
if [ -n "$CG" ] && [ -f "/sys/fs/cgroup${CG}/memory.max" ]; then
    MM=$(cat "/sys/fs/cgroup${CG}/memory.max")
    CM=$(cat "/sys/fs/cgroup${CG}/cpu.max" 2>/dev/null || echo "?")
    [ "$MM" != "max" ] && ok "cgroup quota live: memory.max=$MM cpu.max=$CM" \
                       || bad "cgroup present but memory.max=max (no quota)"
else
    bad "no cgroup for the unit ($CG)"
fi

if journalctl --namespace=autonomic -n 1 --no-pager >/dev/null 2>&1; then
    LINES=$(journalctl --namespace=autonomic --no-pager 2>/dev/null | grep -c "autonomic-agentd" || true)
    [ "${LINES:-0}" -gt 0 ] \
        && ok "own journal namespace: $LINES line(s) via 'journalctl --namespace=autonomic'" \
        || bad "namespace exists but no agent lines in it"
else
    bad "journalctl --namespace=autonomic failed (LogNamespace unsupported?)"
fi

if [ -f /run/autonomic/world ]; then
    PERM=$(stat -c '%a %U' /run/autonomic/world)
    ok "world marker present: $(tr '\n' ' ' < /run/autonomic/world) (mode/owner: $PERM)"
    [ "$(stat -c '%a' /run/autonomic/world)" = "444" ] \
        && ok "world marker is read-only (0444)" \
        || bad "world marker is not 0444"
else
    bad "no /run/autonomic/world"
fi

# action channel: 通道里过一次，计数器必须单调 +1
BEFORE=$(awk -F= '/action_seq/{print $2}' /run/autonomic/world 2>/dev/null || echo "")
if [ -n "$BEFORE" ]; then
    autonomicctl bump >/dev/null 2>&1; sleep 1
    AFTER=$(awk -F= '/action_seq/{print $2}' /run/autonomic/world 2>/dev/null || echo "")
    [ "$AFTER" = "$((BEFORE + 1))" ] \
        && ok "Action channel is monotonic: action_seq $BEFORE -> $AFTER" \
        || bad "action_seq did not advance ($BEFORE -> $AFTER)"
fi

# ---------------------------------------------------------------- 4. 卸载
head1 "4. clean uninstall"

DEBIAN_FRONTEND=noninteractive apt-get purge -y "$PKG" >/tmp/apt-purge.log 2>&1 \
    && ok "apt purge $PKG" || { bad "purge failed"; tail -20 /tmp/apt-purge.log; }

getent passwd autonomic >/dev/null && bad "user 'autonomic' survived purge" || ok "no residual user"
[ -e /etc/apt/sources.list.d/autonomic.list ] && echo "    (sources.list kept on purpose)"
systemctl list-unit-files 2>/dev/null | grep -q "^autonomic-agent.service" \
    && bad "unit file survived purge" || ok "no residual unit"
[ -d /var/lib/autonomic ] && bad "/var/lib/autonomic survived purge" || ok "no residual state dir"
[ -f /usr/sbin/autonomic-agentd ] && bad "binary survived purge" || ok "no residual binary"

# ---------------------------------------------------------------- 结果
echo
echo "======================================"
echo "  PASS=$PASS  FAIL=$FAIL"
echo "======================================"
[ "$FAIL" -eq 0 ] || exit 1
