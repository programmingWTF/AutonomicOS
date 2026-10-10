#!/bin/bash
# 验证 deb 里被 Linux 执行的文件都是 LF，且 shebang 能被内核读懂。
#
# ⚠️ 别把版本号写死在这里。写死过一次，症状是：构建已经升到 0.4.0，
# 这个脚本还去解 0.3.0 那个包（早已被覆盖或删掉），于是所有文件报"<缺失>"，
# 看起来像"deb 坏了"，其实是检查脚本自己指向了不存在的东西。
set -u
DEB="${1:-$(ls -t /mnt/d/Code/AutonomicOS/dist/autonomic-agent_*.deb 2>/dev/null | head -1)}"
if [ -z "$DEB" ] || [ ! -f "$DEB" ]; then
    echo "check-deb-lf.sh: 找不到 deb，先跑 build.sh（或把 deb 路径作为第一个参数传进来）"
    exit 1
fi
echo "检查：$DEB"
cd /tmp && rm -rf dc && mkdir dc && cd dc
dpkg-deb -x "$DEB" .
rc=0
for f in usr/bin/autonomicctl usr/sbin/autonomic-agentd usr/sbin/autonomic-actd \
         usr/share/autonomic/autonomicplan.py usr/share/autonomic/autonomicact.py \
         usr/share/autonomic/autonomicconf.py; do
  if [ ! -f "$f" ]; then echo "$f  <缺失>"; rc=1; continue; fi
  sn=$(head -1 "$f")
  if printf '%s' "$sn" | grep -q $'\r'; then
    echo "$f  !! CR 仍在"
    rc=1
  else
    echo "$f  LF ok  [$sn]"
  fi
done

# 最直接的一步：真的执行它，看内核认不认这个 shebang
echo
echo "--- 实执 ---"
PATH=/tmp/dc/usr/bin:$PATH PYTHONPATH=/tmp/dc/usr/share/autonomic \
  /tmp/dc/usr/bin/autonomicctl version && echo "autonomicctl 可执行 OK" || { echo "autonomicctl 执行失败"; rc=1; }
exit $rc
