#!/bin/bash
# 在 Linux 上跑仓库里的自检脚本，并把输出同时落盘（WSL 转发偶尔丢 stdout）。
set -u
cd /mnt/d/Code/AutonomicOS
OUT=dist/selftest.log
: > "$OUT"
for t in scripts/test-plan.py scripts/test-backup-naming.py; do
    echo "======== $t ========" | tee -a "$OUT"
    python3 "$t" packaging/autonomic-agent/src 2>&1 | tee -a "$OUT"
    echo | tee -a "$OUT"
done
sync
