#!/bin/bash
# boot-test.sh -- 在 VirtualBox 里从 ISO 启动 Autonomic OS，截图为证。
#
#   ./scripts/boot-test.sh [ISO路径] [等待秒数]
#
# 为什么一定要 EFI：本机 Hyper-V 占着 VT-x，VirtualBox 会回退到 NEM 并进入
# Snail execution mode（unrestricted guest 被屏蔽），16 位实模式模拟慢到不可用 ——
# 固件走 EFI 才能正常启动。这是 AOS 时期就实测过的坑，别再踩一次。

set -u

ISO="${1:-/d/Code/AutonomicOS/dist/autonomic-os-0.1-amd64.iso}"
WAIT="${2:-150}"
VM="${VM:-autonomic-boot-test}"
SHOT="${SHOT:-/d/Code/AutonomicOS/dist/boot.png}"
VBM="${VBM:-/c/Program Files/Oracle/VirtualBox/VBoxManage.exe}"

[ -f "$ISO" ] || { echo "boot-test.sh: no ISO at $ISO" >&2; exit 1; }
[ -x "$VBM" ] || { echo "boot-test.sh: VBoxManage not at $VBM" >&2; exit 1; }

echo "boot-test.sh: ISO  $ISO"
echo "boot-test.sh: VM   $VM (EFI, 2 vCPU, 2 GB)"

"$VBM" controlvm "$VM" poweroff >/dev/null 2>&1
sleep 2
"$VBM" unregistervm "$VM" --delete >/dev/null 2>&1
sleep 1

"$VBM" createvm --name "$VM" --ostype Ubuntu_64 --register >/dev/null
# 固件必须是 **efi64**，不能是 efi：后者会让 VirtualBox 有机会选到 32 位 EFI，
# 在 x64 CPU 上直接 #GP 崩在 CpuDxe（串口里能看到 X64 Exception Type - 0D）。
"$VBM" modifyvm "$VM" \
    --memory 4096 --cpus 4 --vram 32 \
    --firmware efi64 \
    --boot1 dvd --boot2 disk --boot3 none --boot4 none \
    --nic1 nat
"$VBM" storagectl "$VM" --name SATA --add sata --controller IntelAhci
"$VBM" storageattach "$VM" --storagectl SATA --port 0 --device 0 \
    --type dvddrive --medium "$ISO"

# 端口转发：宿主 2223 -> guest 22，起来之后能直接 ssh 进去验收
# 语法：natpf1（没有连字符），规则 = "名称,tcp,,宿主端口,,guest端口"
"$VBM" modifyvm "$VM" --natpf1 "sshrule,tcp,,2223,,22"

# 无头模式下截图经常拿到 0x0（guest 还没初始化显示），看不出死在哪一步。
# 串口才是真相来源 —— ISO 的 grub.cfg 默认带 console=ttyS0,115200。
SERIAL_WIN="${SHOT%.*}-serial.log"
SERIAL_RAW="${SERIAL_WIN#D:}"
SERIAL_RAW="${SERIAL_RAW//\\//}"
"$VBM" modifyvm "$VM" --uart1 0x3F8 4 \
    --uartmode1 file "${SERIAL_WIN}"
rm -f "$SERIAL_WIN"

"$VBM" startvm "$VM" --type headless

echo "boot-test.sh: booted, waiting ${WAIT}s for it to come up..."
sleep "$WAIT"

"$VBM" controlvm "$VM" screenshotpng "$SHOT" 2>/dev/null \
    && echo "boot-test.sh: screenshot -> $SHOT" \
    || echo "boot-test.sh: (no screenshot -- guest has no display yet; serial log is what matters)"

echo
echo "boot-test.sh: VM state:"
"$VBM" showvminfo "$VM" --machinereadable 2>/dev/null | grep -E '^VMState=' || true

echo
echo "boot-test.sh: === serial log (last 25 lines) ==="
tail -25 "$SERIAL_WIN" 2>/dev/null || echo "(no serial output -- did the firmware even start?)"
