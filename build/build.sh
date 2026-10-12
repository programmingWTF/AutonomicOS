#!/bin/sh
# build.sh -- build the .deb reproducibly.
#
# 同一个脚本在三处跑，产出同一个包：
#   * WSL / 本机 Ubuntu       ./build/build.sh
#   * 干净容器                docker build -f build/Dockerfile .
#   * CI                      .github/workflows/build.yml
#
# 可复现的三根支柱：
#   1. SOURCE_DATE_EPOCH 固定（钉在 changelog 那个提交上，不用 now）
#   2. 构建路径固定（都在 /build 或源码树内的固定相对路径）
#   3. 基础环境锁定（见 Dockerfile 的镜像 digest；CI 用 ubuntu-24.04 runner）
#
# 需要的包：dpkg-dev debhelper fakeroot
#   sudo apt-get install -y dpkg-dev debhelper fakeroot

set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
SRC="$ROOT/packaging/autonomic-agent"
DIST="${DIST:-$ROOT/dist}"

# ---- 1/3 可复现：时间 ----
# SOURCE_DATE_EPOCH 钉在"当前版本 changelog 条目的日期"上。
# 改版本时同步改这里，不要改成 $(date +%s) —— 那会让同棵树两次编出两个包。
#   0.6.0-1 -> 2026-10-12T14:00:00Z = 1791813600
#   0.5.0-1 -> 2026-10-11T22:40:00Z = 1791758400
#   0.1.0-1 -> 2026-10-09T13:30:00Z = 1791552600
# 想临时覆盖（比如重编历史版本）就设 SOURCE_DATE_EPOCH_OVERRIDE。
# ⚠️ 别在别处再抄一份这个映射 —— 两处维护迟早对不上，而"对不上"的表现是
# 构建莫名不可复现，排查起来像灵异事件。这三个数是 `date -u -d '<ISO>' +%s`
# 算出来的，**别手算**（手算错过一次：把 2026 当成 2025，差整整一年）。
TOP_VERSION=$(grep -m1 '^autonomic-agent (' "$SRC/debian/changelog" \
              | sed 's/.*(\([0-9.]*\)-.*/\1/')
case "$TOP_VERSION" in
    0.6.0) EPOCH=1791813600 ;;
    0.5.0) EPOCH=1791758400 ;;
    *)     EPOCH=1791552600 ;;
esac
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH_OVERRIDE:-$EPOCH}"
export SOURCE_DATE_EPOCH
echo "build.sh: SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH (changelog top = $TOP_VERSION)"

# ---- 依赖自检（缺什么说什么，别让 dpkg-buildpackage 报天书）----
missing=""
for tool in dpkg-buildpackage dh fakeroot; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
if [ -n "$missing" ]; then
    echo "build.sh: missing tools:$missing" >&2
    echo "build.sh: install them with: sudo apt-get install -y dpkg-dev debhelper fakeroot" >&2
    exit 1
fi

# ---- 行尾自检：装进包里的东西必须是 LF，否则 kernel 读不懂 shebang ----
# 这不是理论问题。源码在 Windows 侧被文本模式写回成 CRLF 之后，Linux 内核
# 读出来的解释器是 "/usr/bin/python3\r"，于是**每一个**入口都是
#
#     /usr/bin/autonomicctl: cannot execute: required file not found
#
# 文件在、权限对、可执行位有 —— 就是起不来。而且它**不会让构建失败**，
# 只在装机之后的硬盘系统上炸，排查起来像灵异事件。
# 所以这里在构建时就把它拦下：便宜、确定、不会漏。
if grep -rIl $'\r' "$SRC/src" "$SRC/debian" >/dev/null 2>&1; then
    echo "build.sh: 下面的文件带 CR（CRLF 行尾），会被打进 deb：" >&2
    grep -rIl $'\r' "$SRC/src" "$SRC/debian" >&2
    echo "build.sh: 修法：把行尾转回 LF，并确认 .gitattributes 里的 eol=lf 生效。" >&2
    exit 1
fi

echo "build.sh: SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH"
echo "build.sh: source tree   $SRC"

# 只清自己产出的东西，别 rm -rf 整个 dist：那里可能还放着 ISO，
# 而 ISO 正被某个虚拟机挂着的概率不小 —— 一被占用 rm 就失败，整条链断在第一步。
rm -f "$DIST"/*.deb "$DIST"/BUILDINFO.txt
mkdir -p "$DIST"

# dpkg-buildpackage 会把产物放在源码树的**父目录**，所以在临时目录里构建
# 不会污染仓库；同时也让"构建路径"这一根支柱可控。
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

cp -a "$SRC" "$WORK/autonomic-agent"

# ---- 2/3 可复现：权限位 ----
# 在 Windows 文件系统（/mnt/d）上所有文件都是 0777，debhelper 会把
# debian/install 这类**数据**配置文件当成可执行配置去运行然后失败。
# 复制到 ext4 的临时目录后权限位才有效，所以修正放在这里。
(
    cd "$WORK/autonomic-agent"
    chmod 0755 debian/rules debian/postinst debian/prerm debian/postrm
    find debian -type f ! -perm -u+x -exec chmod 0644 {} +
    chmod 0644 debian/install debian/control debian/changelog debian/copyright \
               debian/autonomic-agent.service debian/source/format
    chmod 0755 src/autonomic-agentd src/autonomicctl
)

(
    cd "$WORK/autonomic-agent"
    dpkg-buildpackage -us -uc -b -rfakeroot
)

for deb in "$WORK"/*.deb; do
    [ -e "$deb" ] || continue
    echo "build.sh: -> $(basename "$deb")"
    mv "$deb" "$DIST/"
done

# 构建信息留档，便于"别人重跑出同一个包"时逐项比对。
(
    cd "$DIST"
    for deb in *.deb; do
        [ -e "$deb" ] || continue
        echo "== $deb"
        dpkg-deb -f "$deb" Package Version Architecture Depends
    done
) | tee "$DIST/BUILDINFO.txt"

echo "build.sh: done -> $DIST"
