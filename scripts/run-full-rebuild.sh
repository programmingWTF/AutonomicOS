#!/bin/bash
# 完整链：deb -> 签名仓库 -> rootfs -> ISO。
# 中间那步（make-repo.sh）不能省：rootfs 从签名仓库取包，不是从 dist/。
set -u
cd /mnt/d/Code/AutonomicOS
LOG=dist/fullbuild.log
: > "$LOG"
{
  echo "=== 1/4 deb ===";    bash ./build/build.sh          || { echo "STEP1 FAIL"; exit 1; }
  echo "=== 2/4 repo ===";   bash ./repo/make-repo.sh       || { echo "STEP2 FAIL"; exit 1; }
  echo "=== 3/4 rootfs ==="; rm -f dist/*.iso; bash ./build/build-rootfs.sh || { echo "STEP3 FAIL"; exit 1; }
  echo "=== 4/4 iso ===";    bash ./build/make-iso.sh       || { echo "STEP4 FAIL"; exit 1; }
  echo "=== DONE ==="
  ls -la dist/*.deb dist/*.iso
} >> "$LOG" 2>&1
sync
