#!/bin/bash
# 验证 deb 里被 Linux 执行的文件都是 LF，且 shebang 能被内核读懂。
set -u
cd /tmp && rm -rf dc && mkdir dc && cd dc
dpkg-deb -x /mnt/d/Code/AutonomicOS/dist/autonomic-agent_0.3.0-1_all.deb .
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
