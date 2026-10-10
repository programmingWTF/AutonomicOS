#!/bin/bash
# rebuild-iso.sh -- 只重建 rootfs 和 ISO（deb 已在 build.sh 里做好了）。
# 放在项目内而不是 /tmp：git-bash 的 /tmp 与 WSL 的 /tmp 不是同一个地方，
# 跨过去只会得到 "No such file or directory"。
set -eu
cd /mnt/d/Code/AutonomicOS
rm -f dist/*.iso
bash ./build/build-rootfs.sh
bash ./build/make-iso.sh
echo "ISO 重建完成："
ls -la dist/*.iso
