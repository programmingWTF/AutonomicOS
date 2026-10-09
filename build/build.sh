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

# ---- 1/3 可复现：时间 ----
# 2026-10-09T13:30:00Z = debian/changelog 里 0.1.0-1 的日期。
# 改版本时同步改这里，不要改成 $(date +%s)。
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1791552600}"
export SOURCE_DATE_EPOCH

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
SRC="$ROOT/packaging/autonomic-agent"
DIST="${DIST:-$ROOT/dist}"

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

echo "build.sh: SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH"
echo "build.sh: source tree   $SRC"

rm -rf "$DIST"
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
