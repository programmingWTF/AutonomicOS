#!/bin/bash
# make-iso.sh -- 从根文件系统生成一个可启动的 Autonomic OS ISO。
#
# 走的是所有 Debian Live 都走的路：squashfs 根 + live-boot + grub-mkrescue。
# 全程不需要 loop 设备（WSL 的内核屏蔽了它），也不需要自己写一行引导代码。
#
#   sudo ./build/make-iso.sh
#
# 需要：squashfs-tools xorriso grub-pc grub-efi-amd64（rootfs 里要有 live-boot）
#   sudo apt-get install -y squashfs-tools xorriso

set -eu

# 同样别用 /tmp（WSL 的 tmpfs，实例终止即丢），见 build/build-rootfs.sh 里的说明。
ROOTFS="${ROOTFS:-/root/autonomic-rootfs}"
ISO_DIR="${ISO_DIR:-/root/autonomic-iso}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${OUT:-$ROOT/dist/autonomic-os-0.1-amd64.iso}"
LABEL="${LABEL:-AUTONOMIC_OS}"

[ -d "$ROOTFS" ] || { echo "make-iso.sh: no rootfs at $ROOTFS -- run build/build-rootfs.sh" >&2; exit 1; }
command -v mksquashfs >/dev/null || {
    echo "make-iso.sh: needs squashfs-tools (sudo apt-get install -y squashfs-tools xorriso)" >&2
    exit 1
}

echo "make-iso.sh: rootfs=$ROOTFS -> $OUT"

# ---------------------------------------------------------------- 1. live 支持
# live-boot 让 initramfs 能在启动时找到并挂载 squashfs，再叠一层可写层。
# 这是上游的成熟件，不是我们自己拼的东西。
# 不管 live-boot 是不是已经装了，initramfs 都必须重生成一次 ——
# 只有带着 live 钩子的 initramfs 才认得 squashfs 根。
echo "make-iso.sh: ensuring live-boot + regenerating initramfs"

# rootfs 里的 /etc/resolv.conf 被指到了 systemd-resolved 的 stub，
# 但那东西要系统真正跑起来才有 —— 在 chroot 里它是个坏链接，apt 的 DNS 全废，
# 症状是 "Temporary failure resolving ..."。临时塞一个能用的进去，用完还原。
cp /etc/resolv.conf "$ROOTFS/etc/resolv.conf.buildbak" 2>/dev/null || true
# 它多半是个指向 /run/systemd/resolve/stub-resolv.conf 的**符号链接**，
# 直接 > 重定向只会写进那个还不存在的目标文件，等于什么都没做。
# 必须先删掉链接本身，再写真实文件。
rm -f "$ROOTFS/etc/resolv.conf"
printf 'nameserver 223.5.5.5\nnameserver 8.8.8.8\n' > "$ROOTFS/etc/resolv.conf"

mount -t proc proc "$ROOTFS/proc" 2>/dev/null || true
mount --bind /dev "$ROOTFS/dev" 2>/dev/null || true
chroot "$ROOTFS" sh -c '
    export DEBIAN_FRONTEND=noninteractive
    unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
    apt-get update -o Acquire::http::Proxy=DIRECT -o Acquire::https::Proxy=DIRECT
    apt-get install -y --no-install-recommends \
        -o Acquire::http::Proxy=DIRECT -o Acquire::https::Proxy=DIRECT \
        live-boot live-boot-initramfs-tools
    update-initramfs -u -k all
'
umount -l "$ROOTFS/dev" 2>/dev/null || true
umount -l "$ROOTFS/proc" 2>/dev/null || true

[ -f "$ROOTFS/etc/resolv.conf.buildbak" ] \
    && mv "$ROOTFS/etc/resolv.conf.buildbak" "$ROOTFS/etc/resolv.conf"

# ---------------------------------------------------------------- 2. 内核与 initramfs
rm -rf "$ISO_DIR"
mkdir -p "$ISO_DIR/live" "$ISO_DIR/boot/grub"

VMLINUZ=$(ls -1 "$ROOTFS"/boot/vmlinuz-* 2>/dev/null | grep -v -- '-rescue' | sort -V | tail -1)
INITRD=$(ls -1 "$ROOTFS"/boot/initrd.img-* 2>/dev/null | grep -v -- '-rescue' | sort -V | tail -1)

[ -n "$VMLINUZ" ] && [ -n "$INITRD" ] || {
    echo "make-iso.sh: no kernel/initrd in $ROOTFS/boot" >&2
    ls -1 "$ROOTFS"/boot 2>&1 | head
    exit 1
}
echo "make-iso.sh: kernel  $(basename "$VMLINUZ")"
echo "make-iso.sh: initrd  $(basename "$INITRD")"

cp "$VMLINUZ" "$ISO_DIR/live/vmlinuz"
cp "$INITRD"  "$ISO_DIR/live/initrd.img"

# ---------------------------------------------------------------- 3. squashfs 根
rm -f "$ISO_DIR/live/filesystem.squashfs"
mksquashfs "$ROOTFS" "$ISO_DIR/live/filesystem.squashfs" \
    -comp xz -Xbcj x86 -b 1M -noappend -e boot -processors "$(nproc)" \
    | tail -2
echo "make-iso.sh: squashfs $(du -h "$ISO_DIR/live/filesystem.squashfs" | cut -f1)"

# ---------------------------------------------------------------- 4. 引导
cat > "$ISO_DIR/boot/grub/grub.cfg" <<'EOF'
set default=0
set timeout=5

insmod all_video
insmod gzio
insmod part_gpt
insmod ext2
insmod squash4
insmod loopback

# 串口 console 是给 headless 调试用的：VirtualBox 无头模式截图经常拿到 0x0，
# 只有 ttyS0 能告诉我们它到底起没起来、卡在哪一步。
menuentry "Autonomic OS (live)" {
    linux /live/vmlinuz boot=live components console=ttyS0,115200 console=tty0
    initrd /live/initrd.img
}

menuentry "Autonomic OS (live, verbose)" {
    linux /live/vmlinuz boot=live components console=ttyS0,115200 console=tty0 debug
    initrd /live/initrd.img
}
EOF

cat > "$ISO_DIR/boot/grub/loopback.cfg" <<'EOF'
menuentry "Autonomic OS (live)" {
    linux /live/vmlinuz boot=live components quiet splash iso-scan/filename=${iso_path}
    initrd /live/initrd.img
}
EOF

# ---------------------------------------------------------------- 5. 出 ISO
rm -f "$OUT"
grub-mkrescue -o "$OUT" "$ISO_DIR" -- -volid "$LABEL" 2>&1 | tail -5

echo
echo "make-iso.sh: done -> $OUT ($(du -h "$OUT" | cut -f1))"
echo "make-iso.sh: 虚拟机直接挂这个 ISO 就能启动；也可以 dd 到 U 盘。"
