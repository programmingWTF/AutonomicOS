#!/bin/bash
# rebuild-iso.sh -- 完整重建，从 deb 一直到 ISO。
#
# ⚠️ 顺序是这个脚本存在的全部理由，也是踩过的一个坑：
#
#     build.sh  ->  make-repo.sh  ->  build-rootfs.sh  ->  make-iso.sh
#
# build-rootfs.sh 装 autonomic-agent **不是**从 dist/*.deb，而是从签名的
# apt 仓库（repo/out）。所以如果 make-repo.sh 没跑，rootfs 里装进去的是
# **上一次** 发到仓库的那个版本 —— 于是 ISO 看起来构建成功、体积正常、
# 也能开机，但里面跑的是旧代码。
#
# 这个失败模式最难发现，因为它什么都不报：构建绿灯、开机正常、
# 只是"我明明写了的新功能不在"。所以这里把四步串成一条链，
# 不给人漏掉中间那一步的机会。
#
# 放在项目内而不是 /tmp：git-bash 的 /tmp 与 WSL 的 /tmp 不是同一个地方，
# 跨过去只会得到 "No such file or directory"。
set -eu
cd /mnt/d/Code/AutonomicOS

echo "=== 1/4 构建 deb ==="
bash ./build/build.sh

echo "=== 2/4 重建签名 apt 仓库（rootfs 从这里取包）==="
bash ./repo/make-repo.sh

echo "=== 3/4 构建 rootfs ==="
rm -f dist/*.iso
bash ./build/build-rootfs.sh

echo "=== 4/4 打包 ISO ==="
bash ./build/make-iso.sh

echo
echo "完成："
ls -la dist/*.deb dist/*.iso
