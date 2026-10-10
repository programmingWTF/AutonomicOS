# Autonomic OS

> **Agent 不是跑在系统上的一个进程，而是系统的一个器官。**
> 骨头全部来自 Debian/Ubuntu（内核、glibc、systemd、apt 一个字节都不重写），
> 我们只加一层：让 Agent 成为这台机器的一等公民。

<br>

> ## ⚠️ 先说清楚：这不保证维护
>
> **这是一个研究原型，不是产品，也没有任何形式的维护承诺。**
>
> - **没有 LTS、没有版本迁移保证、没有响应时间承诺**
> - **可能出现无人修复的安全问题** —— 底层安全更新跟着 Ubuntu 上游走，
>   但这一层（约 1500 行 Python）没有人 7×24 盯着
> - 它可能停在某个版本不再往前走，也可能某天被推倒重做
>
> 它存在的理由是**验证一个命题**：Agent 能不能不靠一个 root shell、
> 而是靠一条有强制力的通道，真正管住一台机器。
> 它不是为了成为一个可靠的日常操作系统。
>
> **对待它的正确方式是一次实验**：虚拟机里跑、别放生产、别放重要数据、
> 随时准备好重装。如果你要的是"能用十年的发行版"，这里是错的入口。

<br>

## 目标

做一个**能装进真机的操作系统**，满足两件事：

1. **完整的 Linux 生态**照常可用 —— 所有 Debian/Ubuntu 的包照常 `apt install`，
   "AI 系统"不等于一个功能贫瘠的玩具
2. **任何操作都可以对话完成** —— 不是给你一个好看的聊天框，而是底层真的有一条
   通道：可预览、可归因、可验证、可回滚

可以有交付界面，但**不追求桌面完整度**；有装机向导，但不追求图形安装器那种排场。

<br>

## 现在到哪了

| | 状态 | 一句话 |
|---|---|---|
| **M1** 身份 | ✅ | agent 是 uid/GID/unit/cgroup/日志命名空间齐全的**系统主体**，不是一个 Python 进程 |
| **M2** 启动 | ✅ | 250 MB ISO 能从 UEFI 起来（已装机到硬盘实测） |
| **M3** 装机 | ✅ | `autonomic-install /dev/sda` 一条命令落盘，拔盘重启照常进系统 |
| **M4** 通电 | ✅ | LLM 凭据 + `autonomicctl setup`，密钥走 systemd LoadCredential，不落地到环境变量 |
| **M5** 通道 | ✅ | **Action 通道**：dry_run / apply / verify 三段 + 可逆性声明 + `file.restore` |
| **M6** 向导 | ✅ | 装机 TUI（whiptail）：选盘 → 二次手打设备名 → 主机名 → 用户 → LLM（可跳过） |
| **M7** 闭环 | ✅ | `autonomicctl ask "…"`：说人话进，受校验的动作出；模型提议，通道裁决 |

每次 push 由 CI 编两遍比对 sha256 发布；apt 仓库是静态站点，丢任何 HTTPS 托管上都能用。

<br>

## 装它

### 方式一：在现有 Debian/Ubuntu 上加一层

```bash
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://programmingwtf.github.io/AutonomicOS/autonomic-archive-keyring.gpg \
  | sudo tee /etc/apt/keyrings/autonomic-archive-keyring.gpg >/dev/null
echo "deb [signed-by=/etc/apt/keyrings/autonomic-archive-keyring.gpg] \
https://programmingwtf.github.io/AutonomicOS stable main" \
  | sudo tee /etc/apt/sources.list.d/autonomic.list

sudo apt update && sudo apt install autonomic-agent
sudo autonomicctl status
```

### 方式二：整个系统

```bash
./build/build-rootfs.sh && ./build/make-iso.sh     # -> dist/autonomic-os-0.1-amd64.iso
```

ISO 从 UEFI 起来后：`autonomic-install /dev/sda`，关机拔盘，重启就是从硬盘进 Autonomic OS。

<br>

## 核心：Action 通道

**"让 AI 管住整台机器"不等于给它一个 root shell。** 那不是赋权，那是等一个坏天气出事。

真正的做法是给它一条**比 shell 更有强制力**的通道 —— 因为有约束，所以才有力量：

```
autonomic-agentd   uid=autonomic，ProtectSystem=strict —— 它能思考，碰不到世界
        │          unix socket，0660 root:autonomic
        │          连接建立后先用 SO_PEERCRED 验身份，请求字节还没解析就可能被拒
        ▼
autonomic-actd     root，但只认四个登记过的原语，读写路径锁在四个前缀内
```

每个 action 必须回答三个问题，而不是给一个承诺：

```bash
$ sudo autonomicctl act file.write path=/etc/autonomic/note content="hello"

Action: file.write
  OK   dry_run  将新建 /etc/autonomic/note（0 行 -> 1 行）
        可逆  : 是

  真的执行？输入 yes 继续：yes

  OK   apply    已写入 /etc/autonomic/note
  OK   verify   已核实 /etc/autonomic/note 内容正确

  => file.write 完成。世界版本号已 +1。
```

- **dry_run** 先告诉你将要发生什么，一个字节都不动
- **apply** 真的做，每一步进 journal（自己的命名空间）
- **verify** 事后回到现场再数一遍 —— **verify 和 apply 不一致时，以 verify 为准**，
  这不是 bug，这是设计

以及每个 action 必须声明的性质：**可逆，还是不可逆**。不可逆的事要额外一个显式开关才准做。

而且"可逆"不是口号 —— 每次覆盖文件都留一份原稿和一个记录**它原本属于哪里**的
sidecar，`autonomicctl backups` 列出来，`file.restore` 把它放回去。
一个声明自己可逆、却没人知道怎么把它转回去的 action，说的是一句愿望。

### 说人话，让系统自己变成一个受管的计划

通道就绪之后，缺的是最后一段：**把一句话翻译成一串通道能接受的动作**。

```bash
$ sudo autonomicctl ask "把通道自检再做一遍"

请求：把通道自检再做一遍
模型：gpt-4o-mini

  模型思考 1.7 秒完成。
意图：在 Autonomic 的配置目录里留下一张身份卡，并确认通道仍然通畅
计划 2 步：
  1. file.write
       path       /etc/autonomic/identity-card.conf
       content    # 这张卡是 AI 通过 Action 通道写下来的\nowner = autonomic\n...
       理由        给这台机器留一份可核验的来源说明
  2. self.test
       理由        写完之后要确认通道本身还是通的

  [1/2] dry_run OK     将新建 /etc/autonomic/identity-card.conf（0 行 -> 3 行）
  [2/2] dry_run OK     将写入 /run/autonomic/channel.probe

  执行这 2 步？输入 yes 继续：yes

  [1/2] apply   OK     已写入 /etc/autonomic/identity-card.conf
  [1/2] verify  OK     已核实 /etc/autonomic/identity-card.conf 内容正确
  [2/2] apply   OK     已写入 /run/autonomic/channel.probe
  [2/2] verify  OK     已核实 /run/autonomic/channel.probe 内容正确

  => 2/2 步完成并核实。世界版本号 +2。
  留痕：/var/lib/autonomic/plans/plan-20261011T014000.json
```

规矩写在这里，因为它们是这个功能的全部分量所在：

- **模型提议，通道裁决。** 模型碰不到执行入口，也决定不了 phase。它输出的东西要连过
  三道：结构校验（是不是合法 JSON、action 认不认识、参数在不在 schema 里、路径在不在
  可写前缀里）→ 语义校验 → **全计划预演**。
- **每一步必须有 reason。** 没有理由的写操作一律拒绝。理由不是仪式感：三个月后翻
  审计日志，唯一能回答"它当时为什么动这个文件"的就是它。
- **预演先于任何一次落地。** 第 1 步成功、第 3 步被拒这种半成品结果，比整个拒绝糟糕
  得多 —— 它留下一个既不是目的地也不是出发点的世界。
- **能力清单是从 actd 实时取的，不在代码里抄一份。** 抄的那份从加入第一个新原语那天
  就开始说谎，而模型会照着谎话规划一个不存在的 API。
- **没有 shell 逃生口。** 缺能力就承认缺，加原语。一旦开了"内置动作不够就用 shell"
  的口子，上面全部退化成装饰。

### 为什么这套东西不是花架子

每次 apply 成功都会让 `action_seq` +1，而这个计数器投影出 `/run/autonomic/world`。
**世界版本号是真实变更的账本，不是装饰。**

于是：**绕过通道的改动不计数** —— 差异本身就是检测信号。有人偷偷改了世界标记而不走
Action 通道，agent 下一个 tick 就会 WARN 并纠正回来。这是整套设计里最值钱的一句。

### 不花一分钱地验证这一切

```bash
python3 scripts/test-plan.py          # 21 项：解析与校验逻辑（纯函数，跑得飞快）
python3 scripts/mock-llm.py &         # 一个假的 OpenAI-compatible 端点
sudo autonomicctl setup --base-url http://127.0.0.1:8099/v1 \
    --api-key mock --model mock-1 --non-interactive
sudo autonomicctl ask "把通道自检再做一遍"
```

这条链路最贵的东西不是 LLM 的智力，而是**你确实按纪律做了** —— 不许部分执行、
预演先于落地、每步单独核实、越界整份退回。这些性质跟对面是真 GPT 还是假服务毫无
关系，所以 CI 和任何一个刚 clone 的人都该能把它验上千遍。

<br>

## 构建

```bash
./build/build.sh        # -> dist/autonomic-agent_0.3.0-1_all.deb
./build/build-rootfs.sh # -> 643 MB rootfs（签证我们自己的包 + 改默认值）
./build/make-iso.sh     # -> dist/autonomic-os-0.1-amd64.iso
```

可复现：固定 `SOURCE_DATE_EPOCH`、构建前修权限位、锁定基础环境。
同一棵树连编两次 sha256 一致 —— CI 每次 push 都在跑这个检查。

<br>

## 明确不做

不重写内核 / init / C 库 / 包管理格式 · 不做自己的桌面环境（GNOME 那套 Debian 已经做好
而且比我们做得好）· 不给 agent 一个 shell 逃生口 · 不做多 Agent 编排 ·
不做"没有人点头时的多轮自主循环" · 不做性能优化 ——
现在是要证明这条路走得通，不是要跑分。

<br>

## 目录

```
docs/                        CHARTER.md（第一性原理，不可违背）· M1.md · M2.md · M3.md · M7.md
packaging/autonomic-agent/   debian/ + src/
                               autonomic-agentd   身份守护进程（无特权）
                               autonomic-actd     通道的执行半边（有特权、有边界）
                               autonomicact.py    原语目录：dry_run/apply/verify + 可逆性
                               autonomicplan.py   说人话 -> 受校验的动作（模型提议，通道裁决）
                               autonomicconf.py   配置与凭据（两个文件，刻意分开）
                               autonomicctl       status / setup / llm / ask / act / actions / backups
build/                       build.sh · build-rootfs.sh · make-iso.sh · autonomic-install
scripts/                     install-test.sh（装机全自动验收）· boot-test.sh
                             test-plan.py（21 项纯逻辑自检，CI 必跑）
                             mock-llm.py（假模型端点：没有 key 也能验整条 AI 路径）
```

<br>

_Autonomic OS · research prototype · 2026-10-11_
