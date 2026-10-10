#!/bin/bash
# install-test.sh -- 完整装机验收：ISO 启动 -> autonomic-install 落盘 -> 硬盘启动再验收。
#
#   ./scripts/install-test.sh
#
# 这是"可以安装在计算机里"的最终证据：虚拟机从 ISO 起来，跑一条
# autonomic-install /dev/sda，关机、拔盘、从硬盘启动，还是那套 Autonomic OS。

set -u

ISO="${ISO:-/d/Code/AutonomicOS/dist/autonomic-os-0.1-amd64.iso}"
VM="${VM:-autonomic-install-test}"
HD="${HD:-/d/Code/AutonomicOS/dist/test-disk.vdi}"
KEY=/d/Code/AOS/vm/keys/id_ed25519
VBM="${VBM:-/c/Program Files/Oracle/VirtualBox/VBoxManage.exe}"
BOOT_WAIT="${BOOT_WAIT:-210}"

SSH="ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=25 -p 2223 root@127.0.0.1"

step() { echo; echo "======== $1 ========"; }

step "0. 清理旧环境"
"$VBM" controlvm "$VM" poweroff >/dev/null 2>&1; sleep 4
"$VBM" unregistervm "$VM" --delete >/dev/null 2>&1
rm -f "$HD"
"$VBM" createmedium disk --filename "$HD" --size 8192 --format VDI >/dev/null

step "1. 建 VM（EFI64 + 8G 虚拟硬盘 + ISO）"
"$VBM" createvm --name "$VM" --ostype Ubuntu_64 --register >/dev/null
"$VBM" modifyvm "$VM" --memory 4096 --cpus 4 --vram 32 --firmware efi64 \
    --boot1 dvd --boot2 disk --boot3 none --boot4 none --nic1 nat
"$VBM" storagectl "$VM" --name SATA --add sata --controller IntelAhci
"$VBM" storageattach "$VM" --storagectl SATA --port 0 --device 0 --type hdd --medium "$HD"
"$VBM" storageattach "$VM" --storagectl SATA --port 1 --device 0 --type dvddrive --medium "$ISO"
"$VBM" modifyvm "$VM" --natpf1 "sshrule,tcp,,2223,,22"

step "2. 从 ISO 启动（等 ${BOOT_WAIT}s）"
"$VBM" startvm "$VM" --type headless >/dev/null
sleep "$BOOT_WAIT"

step "3. 落盘：autonomic-install /dev/sda"
AILOG=/d/Code/AutonomicOS/dist/install.log
if $SSH 'autonomic-install /dev/sda' > "$AILOG" 2>&1; then
    tail -6 "$AILOG"
else
    tail -20 "$AILOG"
    echo "install-test.sh: 落盘失败，终止"
    "$VBM" controlvm "$VM" poweroff >/dev/null 2>&1
    exit 1
fi

step "4. 关机，摘掉 ISO，改为硬盘启动"
"$VBM" controlvm "$VM" poweroff >/dev/null; sleep 5
"$VBM" storageattach "$VM" --storagectl SATA --port 1 --device 0 --type dvddrive --medium emptydrive
"$VBM" modifyvm "$VM" --boot1 disk --boot2 none --boot3 none --boot4 none

step "5. 从硬盘启动（等 ${BOOT_WAIT}s）"
"$VBM" startvm "$VM" --type headless >/dev/null
sleep "$BOOT_WAIT"

step "6. 验收：这应该是硬盘上的 Autonomic OS"
$SSH 'echo "os-release: $(head -1 /etc/os-release)";
      echo "hostname  : $(hostname)";
      echo "root on   : $(findmnt -n -o SOURCE /)";
      echo "fstab     : $(head -1 /etc/fstab | cut -c1-60)...";
      systemctl status autonomic-agent --no-pager | sed -n "1,4p";
      autonomicctl status 2>&1 | tail -3' 2>&1

step "7. 验收：Action 通道 —— agent 真的能操作这台机器"
$SSH 'echo "--- 两个守护进程（思考者 + 执行者）---";
      systemctl is-active autonomic-agent autonomic-actd;
      echo;
      echo "--- 通道愿意做的事，一共就这些 ---";
      autonomicctl actions 2>&1 | sed -n "1,10p";
      echo;
      echo "--- 一次真实变更：预览 -> 执行 -> 复核 ---";
      autonomicctl act self.test --yes --reason="install acceptance run" 2>&1;
      echo;
      echo "--- 世界版本号：一次 apply 应该让它 +1 ---";
      autonomicctl world 2>&1 | head -2;
      echo;
      echo "--- 边界核验：试着写 /etc/shadow，必须被拒 ---";
      autonomicctl act file.write path=/etc/shadow content=pwned --yes 2>&1 | sed -n "1,6p";
      echo;
      echo "--- 凭据落地权限（credentials.env 必须是 640 root:autonomic）---";
      stat -c "%a %U:%G %n" /etc/autonomic/credentials.env /etc/autonomic/agent.conf 2>&1' 2>&1

step "8. 验收：AI 闭环 —— 说人话 -> 校验 -> 预演 -> 落地 -> 回滚"
# 这一节用仓库自带的假 LLM 端点（scripts/mock-llm.py）在**虚机里**跑完整条
# AI 路径。为什么要用假的：这条链路最贵的东西不是 LLM 的智力，而是
# "你确实按纪律做了"—— 不许部分执行、预演先于落地、每步单独核实、
# 越界整份退回。这些性质跟对面是真 GPT 还是假服务毫无关系，而用假的
# 可以重复上千遍、每次返回的字节一模一样。
$SSH 'pkill -f mock-llm.py 2>/dev/null; sleep 1;
      python3 /usr/local/share/mock-llm.py --port 8099 >/tmp/mockllm.log 2>&1 &
      sleep 2;
      autonomicctl setup --base-url http://127.0.0.1:8099/v1 --api-key mock-key \
          --model mock-1 --non-interactive 2>&1 | tail -3' 2>&1

echo
echo "--- 8a. 合法计划：两步，必须全部预演通过后才落地 ---"
$SSH 'autonomicctl ask "mode=good 把通道自检再做一遍" --yes 2>&1' 2>&1

echo
echo "--- 8b. 越界计划：写到 /etc/ssh/，必须整份退回且一步未执行 ---"
$SSH 'autonomicctl ask "mode=illegal 帮我改一下 sshd 配置" --yes 2>&1 | tail -8' 2>&1

echo
echo "--- 8c. 不合格计划：未登记 action + 缺 reason，必须整份退回 ---"
$SSH 'autonomicctl ask "mode=badstep 删掉根目录" --yes 2>&1 | tail -6' 2>&1

echo
echo "--- 8d. 回滚：先改，再用 file.restore 收回去 ---"
$SSH 'printf "%s\n" "before = 原始内容" > /etc/autonomic/rollback-demo.conf
      autonomicctl act file.write path=/etc/autonomic/rollback-demo.conf \
          content="after = 被 AI 改过" --yes --reason="回滚演示" 2>&1 | tail -4
      echo "  改后: $(cat /etc/autonomic/rollback-demo.conf)"
      BK=$(ls -t /var/lib/autonomic/backups/*rollback-demo.conf 2>/dev/null | head -1)
      echo "  备份: $BK"
      autonomicctl act file.restore backup="$BK" --yes --reason="回滚演示" 2>&1 | tail -4
      echo "  还原后: $(cat /etc/autonomic/rollback-demo.conf)"' 2>&1

echo
echo "--- 8e. 每次改动都留痕，且日志可读 ---"
$SSH 'ls -1 /var/lib/autonomic/plans/ 2>/dev/null | tail -3;
      echo "--- 审计文件片段 ---";
      tail -c 600 "$(ls -t /var/lib/autonomic/plans/*.json | head -1)"' 2>&1

$SSH 'pkill -f mock-llm.py 2>/dev/null; true' 2>&1

echo
step "完成。虚拟机 $VM 保留（可随时开机复查）。"
