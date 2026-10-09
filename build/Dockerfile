# 可复现构建环境 —— Autonomic OS / autonomic-agent
#
# 本机 docker daemon 不可用（docker-desktop 已注销），所以这个文件现在的角色是
# **可复现性的契约**：任何人只要有一个 OCI 运行时，就能用它重跑出同一个包。
# CI 走 GitHub Actions 的 ubuntu-24.04 runner，跑的是同一个 build/build.sh。
#
#   docker build -f build/Dockerfile -t autonomic-build .
#   docker run --rm -v "$PWD/dist:/out" autonomic-build cp -a /build/dist/. /out/
#
# 三根支柱里的"基础环境锁定"在这里：镜像按 digest 钉死，工具版本可复算。

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# 只装构建真正需要的三样。多了就违背了"复用不造骨头"之外的另一条：
# 构建环境越窄，可复现越可信。
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        dpkg-dev \
        debhelper \
        fakeroot \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY . /build

RUN ./build/build.sh

CMD ["sh", "-c", "ls -l /build/dist"]
