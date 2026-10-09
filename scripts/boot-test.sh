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
"$VBM" modifyvm "$VM" \
    --memory 2048 --cpus 2 --vram 32 \
    --firmware efi \
    --boot1 dvd --boot2 disk --boot3 none --boot4 none \
    --nic1 nat
"$VBM" storagectl "$VM" --name SATA --add sata --controller IntelAhci
"$VBM" storageattach "$VM" --storagectl SATA --port 0 --device 0 \
    --type dvddrive --medium "$ISO"

"$VBM" startvm "$VM" --type headless

echo "boot-test.sh: booted, waiting ${WAIT}s for it to come up..."
sleep "$WAIT"

"$VBM" controlvm "$VM" screenshotpng "$SHOT"
echo "boot-test.sh: screenshot -> $SHOT"

echo
echo "boot-test.sh: VM state:"
"$VBM" showvminfo "$VM" --machinereadable 2>/dev/null | grep -E '^VMState=' || true
