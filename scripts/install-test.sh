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
      stat -c "%a %U:%G %n" /etc/autonomic/credentials.env /etc/autonomic/agent.conf 2>&1;
      echo;
      echo "--- 不可逆原语的两级授权：只给 --yes 时必须仍被拦下 ---";
      echo "  （这一步不会真的建用户：dry_run 在授权检查之后才跑，被拦下就没到那一步）";
      autonomicctl act user.add name=aos-acceptance-probe --yes 2>&1 | sed -n "1,8p";
      echo;
      echo "--- 反过来：用户名非法时，连 dry_run 都进不去 ---";
      autonomicctl act user.add name="Bad;Name" --yes 2>&1 | sed -n "1,4p";
      echo;
      echo "--- 包名注入守卫 ---";
      autonomicctl act pkg.install packages="nginx; rm -rf /" --yes 2>&1 | sed -n "1,4p"' 2>&1

step "8. 验收：AI 闭环 —— 说人话 -> 校验 -> 预演 -> 落地 -> 回滚"
# 这一节用仓库自带的假 LLM 端点（scripts/mock-llm.py）在**虚机里**跑完整条
# AI 路径。为什么要用假的：这条链路最贵的东西不是 LLM 的智力，而是
# "你确实按纪律做了"—— 不许部分执行、预演先于落地、每步单独核实、
# 越界整份退回。这些性质跟对面是真 GPT 还是假服务毫无关系，而用假的
# 可以重复上千遍、每次返回的字节一模一样。
# ⚠️ 起后台进程这件事有两个坑，都踩过，别再回头：
#
# 坑 A：`python3 ... &` 之后 SSH 命令一结束，进程就跟会话一起被收走 ——
#       症状是"起过、但下一秒就没了"，setup 永远 connection refused。
#       （曾以为 setsid 能解决，但它并不总能脱开 SSH 继承的 fd。）
#
# 坑 B（更阴）：`pkill -f mock-llm.py` 写在这条 SSH 命令里时，
#       **pkill 会匹配到承载这条命令的 shell 自己**（它的命令行里就含
#       "mock-llm.py" 这几个字），于是把自己杀掉 —— SSH 返回 255、
#       整段输出凭空消失。看起来像"网络问题"，其实是自杀。
#
# 正解：交给 systemd 托管。`systemd-run` 让进程完全脱离本次 SSH 会话，
# 不继承任何 fd，也不再和这条命令的文本产生任何关系。
# 停它用 `systemctl stop`，不用 pkill，于是坑 B 也一并消失。
$SSH 'systemctl stop aosmock 2>/dev/null; systemctl reset-failed aosmock 2>/dev/null;
      systemd-run --unit=aosmock --collect \
          python3 /usr/local/share/mock-llm.py --port 8099 >/tmp/mockrun.txt 2>&1;
      sleep 3;
      echo "--- mock 端点自检 ---";
      systemctl is-active aosmock;
      curl -s -m 5 http://127.0.0.1:8099/v1/models | head -c 160; echo;
      echo "--- 写入 LLM 凭据 ---";
      autonomicctl setup --base-url http://127.0.0.1:8099/v1 --api-key mock-key \
          --model mock-1 --non-interactive 2>&1 | head -6;
      echo "--- 凭据落地核实 ---";
      ls -la /etc/autonomic/credentials.env;
      grep -c "mock-key" /etc/autonomic/credentials.env' 2>&1

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
# 这一节的关键是**读回内容来判**，而不是信输出的"完成"两个字。
# 之前这里踩过一个坑：先写文件再 apply，备份是在写完**之后**取的，
# 于是 sha256_before 和 sha256_target 相同，restore 正确地判定
# "已经就是备份里的样子，无需还原"，输出却是"完成" —— 测试看着绿，
# 实际上什么都没验证到。所以现在：
#   ① 先落一个已知原稿 ② 记下它的 sha ③ 改 ④ 还原 ⑤ 逐字节比对原稿
$SSH 'set -e
      rm -f /etc/autonomic/rollback-demo.conf
      printf "%s\n" "before = 原始内容" > /etc/autonomic/rollback-demo.conf
      ORIG_SHA=$(sha256sum /etc/autonomic/rollback-demo.conf | cut -d" " -f1)
      echo "  原稿 sha : $ORIG_SHA"

      autonomicctl act file.write path=/etc/autonomic/rollback-demo.conf \
          content="after = 被 AI 改过" --yes --reason="回滚演示" >/dev/null 2>&1
      echo "  改后内容 : $(cat /etc/autonomic/rollback-demo.conf)"

      # 备份名里不允许出现字面的 %f（strftime 没有这个指令）
      BK=$(ls -t /var/lib/autonomic/backups/*rollback-demo.conf 2>/dev/null | head -1)
      echo "  备份     : $BK"
      case "$BK" in
        *%f*) echo "  !! 备份名里出现字面 %f —— 微秒没生效，同秒内会互相覆盖";;
      esac
      echo "  sidecar  : $(cat "$BK.json" 2>/dev/null | head -c 120)"

      autonomicctl act file.restore backup="$BK" --yes --reason="回滚演示" >/dev/null 2>&1
      NEW_SHA=$(sha256sum /etc/autonomic/rollback-demo.conf | cut -d" " -f1)
      echo "  还原后   : $(cat /etc/autonomic/rollback-demo.conf)"
      if [ "$NEW_SHA" = "$ORIG_SHA" ]; then
          echo "  => PASS 逐字节回到原稿（sha 一致）"
      else
          echo "  => FAIL 没有回到原稿（$NEW_SHA != $ORIG_SHA）"
      fi
      echo
      echo "  --- autonomicctl backups 视角 ---"
      autonomicctl backups 2>&1 | head -8' 2>&1

echo
echo "--- 8e. 每次改动都留痕，且日志可读 ---"
$SSH 'ls -1 /var/lib/autonomic/plans/ 2>/dev/null | tail -3;
      echo "--- 审计文件片段 ---";
      tail -c 600 "$(ls -t /var/lib/autonomic/plans/*.json | head -1)"' 2>&1

$SSH 'systemctl stop aosmock 2>/dev/null; systemctl reset-failed aosmock 2>/dev/null; true' 2>&1

echo
step "完成。虚拟机 $VM 保留（可随时开机复查）。"
