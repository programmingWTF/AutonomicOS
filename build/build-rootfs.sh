#!/bin/bash
# build-rootfs.sh -- 用 mmdebstrap 构建 Autonomic OS 的根文件系统。
#
# 这是从"一个能装的包"走向"一个能装在计算机里的系统"的第一步。
# 骨头全部来自上游（内核 / glibc / systemd / apt 一律不碰），
# 我们只往里加一样东西：Agent 作为**开箱就有的系统角色**。
#
#   sudo ./build/build-rootfs.sh
#   OUT=/somewhere SUITE=noble ./build/build-rootfs.sh
#
# 需要：mmdebstrap（Debian/Ubuntu 官方工具）
#   sudo apt-get install -y mmdebstrap

set -eu

SUITE="${SUITE:-noble}"                       # Ubuntu 24.04 LTS
# ⚠️ 别用 /tmp：WSL 的 /tmp 是 tmpfs，WSL 实例一终止（比如构建跑完、或机器休眠）
# 内容就没了 —— 昨晚就是这样白跑了一整轮。必须落在 ext4 的持久路径上。
OUT="${OUT:-/root/autonomic-rootfs}"
# 构建机在中国大陆时 archive.ubuntu.com 只有 ~160 kB/s，整个 rootfs 要跑半小时。
# 镜像只影响构建速度，rootfs 里的源会写成同一个地址，装机后可自行替换。
MIRROR="${MIRROR:-http://mirrors.aliyun.com/ubuntu}"
# ⚠️ MIRROR 只是"构建时从哪儿下载"，PUB_MIRROR 才是"装完之后长期去哪儿更新"。
# 这两件事必须分开：构建机在中国大陆时用 aliyun 快 20 倍，但把 aliyun 写进
# 发布出去的 ISO 是错的 —— ① 国外用户 apt upgrade 会莫名其妙地慢；
# ② 一个开源项目把软件源钉在某个厂商镜像上，看着就不像中立上游。
# 所以 mmdebstrap 阶段照旧走 MIRROR，收尾时再把 sources.list 改写成官方地址。
PUB_MIRROR="${PUB_MIRROR:-http://archive.ubuntu.com/ubuntu}"
SEC_MIRROR="${SEC_MIRROR:-http://security.ubuntu.com/ubuntu}"
# linux-image-generic 会拉 200MB+ 的 modules 包，慢到能把构建拖死；
# virtual 内核够跑虚拟机和大多数真机，先跑通流程。要装机到裸机时再换 generic。
KERNEL_PKG="${KERNEL_PKG:-linux-image-virtual}"
REPO_URL="${AUTONOMIC_REPO_URL:-https://programmingWTF.github.io/AutonomicOS}"
# ⚠️ 别用 HOSTNAME 当变量名：它是 bash 内置变量，WSL 里早就等于宿主机名，
# ${HOSTNAME:-autonomic} 永远取到宿主机名（ThinkBook），默认值形同虚设。
OS_HOSTNAME="${OS_HOSTNAME:-autonomic}"
ROOT_PASSWORD="${ROOT_PASSWORD:-autonomic}"   # prototype 默认值，装机后第一件事就是改掉
USER_NAME="${USER_NAME:-guiyu}"
USER_PASSWORD="${USER_PASSWORD:-autonomic}"

DIST="$(cd "$(dirname "$0")/.." && pwd)/dist"

command -v mmdebstrap >/dev/null || {
    echo "build-rootfs.sh: needs mmdebstrap (sudo apt-get install -y mmdebstrap)" >&2
    exit 1
}

[ "$(id -u)" = 0 ] || { echo "build-rootfs.sh: needs root" >&2; exit 2; }

echo "build-rootfs.sh: suite=$SUITE out=$OUT mirror=$MIRROR"

# WSL 里 apt 常被配成走 Windows 宿主的代理（127.0.0.1:7897），而 WSL 自己的
# 127.0.0.1 上没有它 —— 结果是满屏 502 Bad Gateway。
# 光给 apt 传 -o Proxy=DIRECT 不够（环境变量会赢），必须把变量也清掉。
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY || true
export DEBIAN_FRONTEND=noninteractive
# chroot 会继承宿主的 PATH，而 WSL 的 PATH 里塞了一长串 /mnt/c 下的 Windows 目录。
# 在 rootfs 内部那条 PATH 是没用的，结果是 chroot 里连 apt-get 都找不到。
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export APT_DIRECT='Acquire::http::Proxy "DIRECT"'
export APT_DIRECT_HTTPS='Acquire::https::Proxy "DIRECT"'

rm -rf "$OUT"

# ---------------------------------------------------------------- 1. 骨头
# 注意这里没有一样是我们自己写的：内核、init、C 库、包管理全部来自上游。
# variant 必须是 apt（= essential + apt）：mmdebstrap 的 minbase 里**没有 apt**，
# 装出来的 rootfs 只有 dpkg，进 chroot 后 apt-get: not found。
mmdebstrap \
    --variant=apt \
    --architecture=amd64 \
    --aptopt="$APT_DIRECT" \
    --aptopt="$APT_DIRECT_HTTPS" \
    --include="
        $KERNEL_PKG
        grub-efi-amd64
        initramfs-tools
        systemd-sysv
        systemd-resolved
        dbus
        udev
        sudo
        openssh-server
        ca-certificates
        gnupg
        curl
        iproute2
        iputils-ping
        locales
        nano
        python3
        adduser
        parted
        dosfstools
        rsync
        whiptail
    " \
    "$SUITE" "$OUT" \
    "deb $MIRROR $SUITE main restricted universe multiverse" \
    "deb $MIRROR $SUITE-updates main restricted universe multiverse" \
    "deb $MIRROR $SUITE-security main restricted universe multiverse"

echo "build-rootfs.sh: base rootfs done ($(du -sh "$OUT" | cut -f1))"

# ---------------------------------------------------------------- 2. 身份
# 我们的 apt 仓库的公钥进系统信任链。装机之后 apt upgrade 走的就是自己的仓库。
mkdir -p "$OUT/etc/apt/keyrings"
if [ -f "$DIST/../repo/out/autonomic-archive-keyring.gpg" ]; then
    install -m 0644 "$DIST/../repo/out/autonomic-archive-keyring.gpg" \
        "$OUT/etc/apt/keyrings/autonomic-archive-keyring.gpg"
    echo "deb [signed-by=/etc/apt/keyrings/autonomic-archive-keyring.gpg] $REPO_URL stable main" \
        > "$OUT/etc/apt/sources.list.d/autonomic.list"
    echo "build-rootfs.sh: our repo wired in -> $REPO_URL"
else
    echo "build-rootfs.sh: WARNING no archive keyring -- run repo/make-repo.sh first"
fi

# ---- 装我们自己的包（本地 deb，构建时不依赖网络上的仓库）----
mkdir -p "$OUT/tmp/autonomic"
found=0
for deb in "$DIST"/*.deb; do
    [ -e "$deb" ] || continue
    cp "$deb" "$OUT/tmp/autonomic/"
    found=1
done

mount_chroot() {
    mount -t proc  proc  "$OUT/proc"
    mount -t sysfs sys   "$OUT/sys"
    mount --bind   /dev  "$OUT/dev"
    mount -t devpts devpts "$OUT/dev/pts"
}
umount_chroot() {
    for m in "$OUT/dev/pts" "$OUT/dev" "$OUT/sys" "$OUT/proc"; do
        umount -l "$m" 2>/dev/null || true
    done
}
trap umount_chroot EXIT

mount_chroot

if [ "$found" = 1 ]; then
    chroot "$OUT" sh -c '
        set -e
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
            -o Acquire::http::Proxy=DIRECT -o Acquire::https::Proxy=DIRECT \
            /tmp/autonomic/*.deb
        rm -rf /tmp/autonomic
        systemctl enable autonomic-agent.service
    '
    echo "build-rootfs.sh: autonomic-agent installed + enabled"
fi

# ---------------------------------------------------------------- 3. 默认值
# 独立性 = 默认值必须是我们的。装机后第一次开机就该是这样，
# 而不是"装完了你自己去配"。

# 软件源：把 mmdebstrap 写进去的 MIRROR 换成发布地址。
# 这个文件是装机之后长期在用的东西 —— 它决定了这台机器以后从哪儿拿安全更新。
cat > "$OUT/etc/apt/sources.list" <<EOF
deb $PUB_MIRROR $SUITE main restricted universe multiverse
deb $PUB_MIRROR $SUITE-updates main restricted universe multiverse
deb $PUB_MIRROR $SUITE-backports main restricted universe multiverse
deb $SEC_MIRROR $SUITE-security main restricted universe multiverse
EOF
echo "build-rootfs.sh: apt sources rewritten to $PUB_MIRROR (built via $MIRROR)"

echo "$OS_HOSTNAME" > "$OUT/etc/hostname"
cat > "$OUT/etc/hosts" <<EOF
127.0.0.1   localhost
127.0.1.1   $OS_HOSTNAME
::1         localhost ip6-localhost ip6-loopback
EOF

# 开机就有网：systemd-networkd + DHCP，比 netplan 配置更少、更可预测
cat > "$OUT/etc/systemd/network/80-dhcp.network" <<'EOF'
[Match]
Name=en* eth*

[Network]
DHCP=yes
EOF
chroot "$OUT" sh -c '
    systemctl enable systemd-networkd systemd-resolved
    ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
'

# 一个能登录的用户（prototype 默认口令，装机后必须改）
chroot "$OUT" sh -c "
    set -e
    echo 'root:$ROOT_PASSWORD' | chpasswd
    if ! id -u '$USER_NAME' >/dev/null 2>&1; then
        adduser --disabled-password --gecos 'Autonomic OS user' '$USER_NAME'
        echo '$USER_NAME:$USER_PASSWORD' | chpasswd
        usermod -aG sudo '$USER_NAME'
    fi
"

# SSH 允许口令登录，方便第一次从宿主机连进来（prototype 便利优先）
chroot "$OUT" sh -c '
    sed -i "s/^#\?PasswordAuthentication.*/PasswordAuthentication yes/" /etc/ssh/sshd_config
    systemctl enable ssh
'

# 登录横幅（getty 读的是 /etc/issue，不是 os-release —— 不写它，
# 开机第一眼还是 "Ubuntu 24.04 LTS"，默认值就白设了）
printf '%s\n' 'Autonomic OS 0.1 (research prototype)' ' \l' '' > "$OUT/etc/issue"
cp "$OUT/etc/issue" "$OUT/etc/issue.net"

# 落盘安装器（M3）：live 系统里跑一条命令就能装进硬盘
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
install -m 0755 "$ROOT_DIR/build/autonomic-install" "$OUT/usr/sbin/autonomic-install"

# 假的 OpenAI-compatible 端点（验收用）：让"AI 闭环"这件事可以在没有任何
# 真 API key 的情况下被完整、反复地验证。它打包进 live 环境，因为它服务
# 的对象是验收脚本，不是终端用户 —— 装机后的系统里需要真的连一个真的模型。
install -m 0755 "$ROOT_DIR/scripts/mock-llm.py" "$OUT/usr/local/share/mock-llm.py"

# SSH 公钥（验收/运维用）：AOS 台架的公钥预置进 rootfs，否则每次重建都得手动加
KEY_PUB="/mnt/d/Code/AOS/vm/keys/id_ed25519.pub"
if [ -f "$KEY_PUB" ]; then
    PUB=$(cat "$KEY_PUB")
    for u in root guiyu; do
        if [ "$u" = root ]; then d="$OUT/root"; else d="$OUT/home/$u"; fi
        mkdir -p "$d/.ssh"
        printf '%s\n' "$PUB" > "$d/.ssh/authorized_keys"
        chmod 700 "$d/.ssh"
        chmod 600 "$d/.ssh/authorized_keys"
        if [ "$u" = guiyu ]; then
            chown -R 1000:1000 "$d/.ssh"
        fi
    done
    echo "build-rootfs.sh: ssh pubkey installed for root + guiyu"
fi

# release 标识 —— 让人一眼知道这是哪个系统
cat > "$OUT/etc/os-release" <<EOF
PRETTY_NAME="Autonomic OS 0.1 (research prototype)"
NAME="Autonomic OS"
VERSION_ID="0.1"
VERSION="0.1 (M2)"
ID=autonomic
ID_LIKE=debian
HOME_URL="https://github.com/programmingWTF/AutonomicOS"
SUPPORT_URL="https://github.com/programmingWTF/AutonomicOS"
BUG_REPORT_URL="https://github.com/programmingWTF/AutonomicOS/issues"
EOF

# ---------------------------------------------------------------- 4. 收尾
chroot "$OUT" sh -c '
    DEBIAN_FRONTEND=noninteractive apt-get clean
    rm -rf /var/lib/apt/lists/*
    # apt-get clean 只管 /var/cache/apt/archives 里的 deb，而 pkgcache.bin 和
    # srcpkgcache.bin 这两个二进制索引缓存它碰都不碰 —— 实测这俩白占 111MB，
    # 会被原封不动压进 squashfs 然后再进 ISO。这两个索引装机后 apt update
    # 会自己重建，留着毫无用处。
    rm -rf /var/cache/apt/*
'
umount_chroot

echo
echo "build-rootfs.sh: done -> $OUT"
echo "build-rootfs.sh: size $(du -sh "$OUT" | cut -f1)"
echo "build-rootfs.sh: packages: $(chroot "$OUT" dpkg -l 2>/dev/null | grep -c '^ii' || echo '?')"
