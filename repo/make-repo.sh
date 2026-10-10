#!/bin/sh
# make-repo.sh -- turn dist/*.deb into a signed apt repository.
#
# 刻意用 Debian 自带的 apt-ftparchive + gpg，不引 aptly/reprepro：
# 第一性原理是"复用 Debian 已有的"，工具链也不例外。少一个 Go 二进制，
# 就少一个只有作者机器上才有的依赖。
#
# 产物（repo/out/）是纯静态文件 —— 丢到任何静态 HTTPS 托管上就是一个 apt 源。
#
#   ./repo/make-repo.sh                 # 用 repo/gnupg 里的（或自动生成的）密钥签名
#   AUTONOMIC_GPG_KEY=<fpr> ./repo/make-repo.sh
#
# 需要：apt-utils（apt-ftparchive）gnupg
#   sudo apt-get install -y apt-utils gnupg
#
# ⚠️ 密钥绝不入库：repo/gnupg/ 已在 .gitignore 里。

set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
DIST="${DIST:-$ROOT/dist}"
OUT="${OUT:-$ROOT/repo/out}"

ORIGIN="Autonomic OS"
LABEL="Autonomic OS"
SUITE="stable"
CODENAME="stable"
COMPONENT="main"
ARCHES="amd64"
DESCRIPTION="Autonomic OS research prototype - agent-native layer for Debian/Ubuntu"

# 固定 Release 的时间戳，让两次构建能逐字节比对（可复现 > 好看）
RELEASE_DATE="Fri, 09 Oct 2026 13:30:00 UTC"

for tool in apt-ftparchive gpg; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "make-repo.sh: missing $tool (sudo apt-get install -y apt-utils gnupg)" >&2
        exit 1
    }
done

[ -d "$DIST" ] || { echo "make-repo.sh: no $DIST -- run build/build.sh first" >&2; exit 1; }
ls "$DIST"/*.deb >/dev/null 2>&1 || { echo "make-repo.sh: no .deb in $DIST" >&2; exit 1; }

# ---------------------------------------------------------------- GPG
# 独立 GNUPGHOME，绝不碰 ~/.gnupg，也绝不进仓库。
GNUPGHOME="${AUTONOMIC_GNUPGHOME:-$ROOT/repo/gnupg}"
mkdir -p "$GNUPGHOME"
chmod 700 "$GNUPGHOME"
export GNUPGHOME

KEY_UID="Autonomic OS Archive <contact@liguiyu.com>"

if [ -z "${AUTONOMIC_GPG_KEY:-}" ]; then
    AUTONOMIC_GPG_KEY=$(gpg --batch --list-secret-keys --with-colons "$KEY_UID" 2>/dev/null \
        | awk -F: '/^fpr:/ {print $10; exit}')
fi

if [ -z "${AUTONOMIC_GPG_KEY:-}" ]; then
    echo "make-repo.sh: no signing key found -- generating a local one in $GNUPGHOME"
    echo "make-repo.sh: (development only; a real release must supply AUTONOMIC_GPG_KEY)"
    gpg --batch --quiet --pinentry-mode loopback --passphrase '' \
        --quick-gen-key "$KEY_UID" rsa3072 sign never
    AUTONOMIC_GPG_KEY=$(gpg --batch --list-secret-keys --with-colons "$KEY_UID" \
        | awk -F: '/^fpr:/ {print $10; exit}')
fi

echo "make-repo.sh: signing with $AUTONOMIC_GPG_KEY"

# ---------------------------------------------------------------- layout
rm -rf "$OUT"
mkdir -p "$OUT/pool/$COMPONENT"
mkdir -p "$OUT/dists/$SUITE/$COMPONENT"

for deb in "$DIST"/*.deb; do
    [ -e "$deb" ] || continue
    name=$(dpkg-deb -f "$deb" Package)
    first=$(printf '%s' "$name" | cut -c1)
    mkdir -p "$OUT/pool/$COMPONENT/$first/$name"
    cp "$deb" "$OUT/pool/$COMPONENT/$first/$name/"
    echo "make-repo.sh: pool <- $(basename "$deb")"
done

# ---------------------------------------------------------------- indexes
# 必须 cd 进 $OUT 用**相对**路径扫描：apt-ftparchive 会把传给它的路径前缀
# 原样写进 Packages 的 Filename 字段。用绝对路径的话，apt 会去
# http://<host>//mnt/d/... 找包 —— 404，而且错误信息看起来像天书。
for arch in $ARCHES; do
    d="$OUT/dists/$SUITE/$COMPONENT/binary-$arch"
    mkdir -p "$d"
    (cd "$OUT" && apt-ftparchive --arch "$arch" packages pool) > "$d/Packages"
    gzip -9cn "$d/Packages" > "$d/Packages.gz"
    echo "make-repo.sh: binary-$arch Packages ($(grep -c '^Package:' "$d/Packages") entries)"
done

# ---------------------------------------------------------------- Release
mkdir -p "$OUT/dists/$SUITE"
(cd "$OUT" && apt-ftparchive \
    -o "APT::FTPArchive::Release::Origin=$ORIGIN" \
    -o "APT::FTPArchive::Release::Label=$LABEL" \
    -o "APT::FTPArchive::Release::Suite=$SUITE" \
    -o "APT::FTPArchive::Release::Codename=$CODENAME" \
    -o "APT::FTPArchive::Release::Architectures=$ARCHES" \
    -o "APT::FTPArchive::Release::Components=$COMPONENT" \
    -o "APT::FTPArchive::Release::Description=$DESCRIPTION" \
    -o "APT::FTPArchive::Release::Date=$RELEASE_DATE" \
    release "dists/$SUITE") > "$OUT/dists/$SUITE/Release"

rm -f "$OUT/dists/$SUITE/Release.gpg" "$OUT/dists/$SUITE/InRelease"
gpg --batch --yes --default-key "$AUTONOMIC_GPG_KEY" \
    --armor --detach-sign -o "$OUT/dists/$SUITE/Release.gpg" "$OUT/dists/$SUITE/Release"
gpg --batch --yes --default-key "$AUTONOMIC_GPG_KEY" \
    --clearsign -o "$OUT/dists/$SUITE/InRelease" "$OUT/dists/$SUITE/Release"

# ---------------------------------------------------------------- public key
gpg --batch --yes --export --armor "$AUTONOMIC_GPG_KEY" \
    > "$OUT/autonomic-archive-keyring.asc"
gpg --batch --yes --export "$AUTONOMIC_GPG_KEY" \
    > "$OUT/autonomic-archive-keyring.gpg"

cat > "$OUT/README.txt" <<EOF
Autonomic OS apt repository (research prototype)

Add one line to /etc/apt/sources.list.d/autonomic.list:

  deb [signed-by=/etc/apt/keyrings/autonomic-archive-keyring.gpg] <URL> $SUITE $COMPONENT

Then:

  sudo apt update && sudo apt install autonomic-agent

This repository is a static directory. Any static HTTPS host will serve it.
No warranty: this release is only promised to have built and run when published.
EOF

echo "make-repo.sh: signed Release + InRelease"
echo "make-repo.sh: public key -> $OUT/autonomic-archive-keyring.gpg"
echo "make-repo.sh: done -> $OUT"
