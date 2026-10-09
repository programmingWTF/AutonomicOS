# Autonomic OS

> **在 Debian/Ubuntu 之上，做一个"Agent 原生层 + 一套默认值"的可安装系统：
> 不重写系统，而是让系统第一次把 Agent 当成自己的器官。**

- **对外英文名：`Autonomic OS`**（桂鱼 2026-10-09 定）；中文名待定
- 定位：**research prototype** —— 只承诺"这个 release 当时能跑"
- 第一性原理：`docs/CHARTER.md`（不可违背）
- 代码目录：`D:\Code\AutonomicOS`；旧 `D:\Code\AOS` 是**只读参考**，定位已换代

> 最终目标：**一个可以安装在计算机里的操作系统，且原生支持 Agent。**
> 路线是 仓库 → 包 → 镜像，骨头全部来自 Debian/Ubuntu，我们只加一层默认值。

---

## 它和 AOS 的区别（别搞混）

| | AOS（旧，已作废） | Autonomic OS（本仓库） |
|---|---|---|
| 第一性原理 | **不碰系统**：带外控制平面附体在外部 | **成为系统的一部分**：原生、内生、有强制力 |
| 交付形态 | hypervisor 台架 + 外部控制平面 | **一个独立的、Agent 原生的 Linux 系统** |
| 与上游关系 | 数据平面零改动 | **衍生版**：骨头与绝大多数包复用 Debian/Ubuntu |

「独立系统」和「复用上游」**不矛盾**：独立性看的是名字 / 发布物 / 仓库 / **默认值** / 一句承诺，
跟"字节是不是我写的"无关（Ubuntu 的内核也不是 Canonical 写的）。

---

## M1：Agent 在系统里有一个**身份**，而不是一个进程

**状态：✅ 已验收（2026-10-09，17/17 通过，见下）**

```bash
# 目标机（原厂 Ubuntu 24.04）上只需要一行：
echo "deb [signed-by=/etc/apt/keyrings/autonomic-archive-keyring.gpg] <REPO-URL> stable main" \
  | sudo tee /etc/apt/sources.list.d/autonomic.list

sudo apt update && sudo apt install autonomic-agent
sudo systemctl status autonomic-agent      # 绿
sudo autonomicctl status                   # 身份全景
```

装上之后，Agent 有五样东西：

| 它有的 | 怎么来的 | 为什么这是"身份" |
|---|---|---|
| 独立系统用户 | `adduser --system autonomic`（uid 109 / gid 112） | 系统把它当**一个主体**记账，不是 root 下的子进程 |
| systemd unit | `autonomic-agent.service`，hardening 全开 | 服务管理器认识它、能启停、能设依赖 |
| cgroup 限额 | `memory.max=268435456`、`cpu.max=25000/100000` | 它占用资源是**受控**的 |
| 自己的日志命名空间 | `LogNamespace=autonomic` | 有自己的**审计流**，不与系统日志混 |
| 只读世界版本标记 | `/run/autonomic/world` = `(snapshot_id, action_seq)`，0444 | 计数器在系统外，系统里只有只读投影 → **可检测绕过通道的漂移** |

### 验收结果（原厂 Ubuntu 24.04.5 VM，2026-10-09）

```
== 1. one line in sources.list, apt update          PASS x3
== 2. apt install                                    PASS
== 3. the agent has an identity, not just a process  PASS x8
      system user: autonomic:x:109:112
      runs as its own uid (pid=3691 uid=109)
      cgroup quota live: memory.max=268435456 cpu.max=25000 100000
      own journal namespace: journalctl --namespace=autonomic
      world marker: snapshot_id=bootstrap action_seq=0  (mode 444, owner autonomic)
      Action channel is monotonic: action_seq 0 -> 1
== 4. clean uninstall                                PASS x5
====================== PASS=17  FAIL=0 ======================
```

---

## 一条命令看卖点

```bash
sudo ./scripts/demo.sh
```

三段：① 身份全景 ② Action 通道（计数器单调）③ **漂移检测** ——
绕过通道直接改世界标记，agent 下一个 tick 就会发现并记一条 `WARN`，然后把标记纠正回来。

> 计数器住在系统外，系统里只有一份只读投影。
> 投影跟真值对不上，就是有人绕过了通道。

---

## 构建（可复现）

```bash
./build/build.sh       # -> dist/autonomic-agent_0.1.0-1_all.deb
./repo/make-repo.sh    # -> repo/out/  （GPG 签名的静态 apt 仓库）
```

三根支柱：`SOURCE_DATE_EPOCH` 固定 · 权限位在构建前修正 · 基础环境锁定（见 `build/Dockerfile`）。

**已验证**：同一棵树连编两次，sha256 完全一致
（`90214bf310e205c2b3621d643770ffd2f4ac2839f9c4c240b8ecedfdaf8e3060`）。

CI（`.github/workflows/build.yml`）每次都跑这个"编两次比对"的检查，
并把 `repo/out/` 发布成静态站点 —— 纯静态目录，丢到任何 HTTPS 托管上就是一个 apt 源。

---

## 目录

```
docs/                        CHARTER.md（权威）· Naming.md · M1.md
packaging/autonomic-agent/   debian/ + src/（agent 守护进程与 autonomicctl）
build/                       build.sh · Dockerfile（可复现构建）
repo/                        make-repo.sh（GPG 签名仓库）
scripts/                     verify-m1.sh（逐条验收）· demo.sh · serve-repo.sh
```

---

## 明确不做（这一版）

不重写内核 / init / C 库 / 包管理格式 · **不做完整 ISO**（顺序是 仓库 → 包 → ISO）·
不做自主根因分析 · 不搬旧 VirtualBox 台架 · 不迁移旧 AOS 代码 · 不做多 Agent 编排。

---

_Autonomic OS · M1 · 2026-10-09_
