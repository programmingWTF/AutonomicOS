# AutonomicOS 交接提示词

> 给下一个接手的 Agent。直接读这个文件就能上手。
> 详细设计不要在这里复述 —— 仓库里有原文，只给路径。

---

## 1. 这个项目是什么

目标原文（用户原话，不可曲解）：

> "现在就是希望做出一个既能完整运行 Linux 生态，又能让 AI 直接操控底层系统的这样一个系统，我希望最后是一个类似 Ubuntu 的真正能安装的操作系统，也可以没有图形化界面，但是要有向导，随后任何操作都可以和 AI 对话来完成。向着这个目标推进。最后要有发行版类似的完整度，但是不意味着我要持续维护，至少我们可以在开源仓库上第一页上就写上不一定保证维护，只是一个尝试。"

四条验收标准：

1. 完整 Linux 生态照常可用（说的是"能装什么"，不是"预装了什么"）
2. AI 直接操控底层系统，且有强制力
3. 有装机向导（可以没有 GUI，但 APIKey + BaseURL 这一步要有）
4. README 首屏写明"不保证维护，只是一个尝试"

**第一性原理在 `docs/CHARTER.md`，动手前必读。** 里程碑设计在 `docs/M1.md` ~ `M9.md`（M4-M6 合并写过，缺失的编号不是遗漏）。

## 2. 工作方式（用户明确授权过的，继续有效）

> "你还是和之前一样继续操作，然后如果有任何进展给我发邮件，但只要不是有问题，就别停下，继续做，有进展是告知。然后如果有任何需要 OpenClaw 帮忙评判、分析的，也可以邮件告诉我，继续吧"

翻译成三条：

- **不要停下来问**。有判断就做，做了再告知。用户要的是持续推进，不是逐步确认。
- **有实质进展就发邮件**（见第 6 节发信方式）。
- 需要外部评判/分析的，写进邮件让用户转给 OpenClaw，不要自己卡住等。

另外一条用户长期偏好：**不要把成果文件自动弹出预览**。生成文件后用文字给路径，让用户自己决定何时打开。

## 3. 现在到哪了

**M9 感知层已完成并全部闭环**：

- 本地 `main` = 远端 `main` = `5a74bdd`，工作区干净
- CI 全绿（编两遍比对 sha256 + 142 项自检 + 发 apt 源）
- ISO：`dist/autonomic-os-0.1-amd64.iso`（238 MB，内层已 mount 验证是 0.5.0）
- deb：`dist/autonomic-agent_0.5.0-1_all.deb`
- 汇报邮件已发（Resend id `01a12916-...`）

通道当前有 **11 个原语**（9 写 + 2 只读感知）。

## 4. 代码在哪（都是路径，自己读）

| 东西 | 路径 |
|---|---|
| 原语目录（dry_run/apply/verify + 可逆性登记） | `packaging/autonomic-agent/src/autonomicact.py` |
| 通道的执行半边（有特权、有边界、发 SIGUSR1） | `packaging/autonomic-agent/src/autonomic-actd` |
| agent 守护进程（无特权、心跳、投影） | `packaging/autonomic-agent/src/autonomic-agentd` |
| 人话 → 受校验的计划（模型提议，通道裁决） | `packaging/autonomic-agent/src/autonomicplan.py` |
| 命令行（status/setup/llm/ask/act/actions/backups/**sense**） | `packaging/autonomic-agent/src/autonomicctl` |
| 配置与凭据 | `packaging/autonomic-agent/src/autonomicconf.py` |
| 装机向导 | `build/autonomic-install` |
| 测试（CI 必跑） | `scripts/test-plan.py`（24）+ `test-primitives.py`（80）+ `test-sense.py`（28）+ `test-backup-naming.py`（9） |
| 装机全自动验收 | `scripts/install-test.sh` |
| ISO 四步链 | `scripts/rebuild-iso.sh`（需 root：`wsl -d Ubuntu -u root -- bash -c "cd /mnt/d/Code/AutonomicOS && bash scripts/run-full-rebuild.sh"`） |
| 当日工作日志（踩坑全在这） | `.workbuddy/memory/2026-10-11.md` |

**测试怎么跑**：`python3 scripts/test-*.py packaging/autonomic-agent/src`
Windows 上能跑（平台相关代码都抽成了可替换点），Linux 上是全绿基线。

## 5. 设计红线（这些是踩过真 bug 换来的，不要绕）

1. **模型提议，通道裁决。** 模型只能提议动作串，碰不到 `dispatch`，也决定不了 phase。新增能力靠加 `@register` 原语，**不靠放宽沙箱、不开 shell 逃生口**。
2. **三道闸门**：结构校验（合法 JSON、括号配平跳过字符串内部）→ 语义校验（action 在真实清单、参数过 schema、路径在可写前缀、每步必须有 reason）→ 全计划预演。能力清单实时取自 actd，**绝不在代码里抄一份**。
3. **整份通过或整份退回。** errors 非空时 steps 必为空。
4. **可逆性只能声明一次**（`@register` 的 `reversible=`），别处一律副本。`dispatch` 的 `seal()` 会强制覆盖 —— handler 撒谎也会被纠正（有测试钉住）。
5. **只读原语一律 `changed=False`**，不推进 `action_seq`。读操作推高版本号会让账本退化成访问计数器。
6. **`sys.read` 的指标名是闭集，不是路径。** 收路径就是任意文件读取漏洞（`/etc/shadow` 也是文件）。
7. **感知也走通道。** agent 跑在 `ProtectSystem=strict` + `ProtectProc=invisible` 下物理上读不到 `/proc`。放宽沙箱是加权力，让 root 的 actd 替它读是加能力 —— 选后者。
8. **每次 ask 都落审计**（`/var/lib/autonomic/plans/plan-<时间>.json`），被拒也留。

## 6. 环境坑（本轮真踩过，照做能省一小时）

- **WSL 里跑构建前必须 `unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY`**。宿主的 `127.0.0.1:7897` 在 WSL 里指的是 WSL 自己，会让 `apt-get update` 假死（日志停着不动，其实在等一个连不上的代理）。
- **要 root 就用 `wsl -d Ubuntu -u root`**，WSL 里 `sudo` 要密码，`sudo -n` 直接失败。
- **判推送成功只能看远端 sha，不能看退出码**：`git push ... | tail` 的 `$?` 是 tail 的。用
  ```bash
  remote=$(git ls-remote origin main | cut -f1); local=$(git rev-parse main)
  [ "$remote" = "$local" ] && echo PUSH_OK
  ```
- **发信用 Resend**（Windows 侧）：密钥在 `C:\Users\34774\AppData\Local\Temp\opencode\resend.env`，字段 `RESEND_API_KEY`。POST `https://api.resend.com/emails`，`from: Lobster <openclaw@liguiyu.com>`，`to: openclaw@liguiyu.com`，正文 `html` 字段。**必须显式清掉代理 env，且请求头要给一个普通 UA**（`curl/8.5.0`）—— Cloudflare 会把 `Python-urllib` 默认 UA 判成机器人返 403(1010)。密钥只走进程内读取，**绝不写入任何文件/commit**。
- **网络时好时坏**：Windows 侧 schannel 抖，WSL 侧通常是更稳的出口。GitHub 推送优先走 WSL。
- **9p 桥接慢**：WSL 里对 `/mnt/d` 做 `rm -rf` 会卡死；用 Windows 侧 `cmd /c rmdir /s /q`。构建下载慢时看 `partial/*.deb` 是否在长大，别急着 kill。
- **重建/构建期间不要改源码**，改了就中断重跑，否则 ISO 里装的是旧代码。

## 7. 下一步（按优先级）

1. **`network.set`** —— 第一个"改错会把自己锁在外面"的原语。**必须先做自动回滚机制**（改完探活，探活失败自动还原），否则一次错配就把机器彻底关在外面。这是整个项目里风险最高的一类，值得单独一个里程碑 + 单独一份设计文档。
2. **`disk.*`** —— 唯一可能让机器再也起不来的类别。要更保守：可能要考虑只允许在明确不涉及的设备上操作。
3. **把感知喂回模型** —— `autonomicctl sense` 已经能产快照并投影到 `/run/autonomic/sense`，但还没接进 `autonomicplan` 的提示词。接上之后 agent 才真正是"看着这台机器做决策"，而不是"蒙着眼睛按模板改"。
4. `user.del` / `user.modify`、plan 的历史 replay。

## 8. 动之前的三个习惯

- **先读 `docs/CHARTER.md`**。它是这个项目唯一不可违背的东西，所有"这样做好像更省事"的念头都先去对一遍。
- **每个里程碑的教训写进 `docs/M<n>.md`**，尤其是真 bug 的修法。M8/M9 里那些"可逆性会被静默谎报""平台差异会伪装成读取失败"的教训，都是这么留下来的。
- **收尾更新 `.workbuddy/memory/<日期>.md`**（append-only）。今天的事今天记，别攒着。

---

*最后更新：2026-10-12，M9 闭环后。*
