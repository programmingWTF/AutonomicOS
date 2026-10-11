#!/usr/bin/python3
"""autonomicact -- the Action channel: the only way the agent touches the world.

Everything in this file exists to answer three questions with evidence
instead of promises:

    what exactly will change?      -> dry_run
    did it actually change?        -> verify
    can we get back?               -> reversible:

Three phases, always available, always meaningful in this order:

    dry_run   return what would change. Touch nothing.
    apply     make it so. Record every step.
    verify    check the world now matches what apply promised.

And one declaration every action must make, with no room for hedging:

    reversible = True    there is an inverse, and using it is safe
    reversible = False   this cannot be undone -- say so before doing it

Here is the thing worth being explicit about, because it is the whole point:

**the agent is not root.** It runs as uid=autonomic under ProtectSystem=strict
and can touch almost nothing. Everything it wants goes to a privileged
companion (autonomic-actd) over a unix socket, and that companion accepts
nothing except the primitives registered here -- each with a parameter
schema and a write allowlist.

"Let the AI control the whole machine" is not achieved by handing it a root
shell. That is not empowerment, it is just an accident waiting for a bad
Tuesday. It is achieved by giving it a channel that is *more* powerful than
a shell precisely *because* it is constrained: every action previewable,
every action attributable to a reason, every action verifiable after the
fact, and reversible wherever physics allows it.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import socket
import subprocess
import time

VERSION = "0.5.0"

# ------------------------------------------------------------------ 写权限边界
# 白名单，不是黑名单。
# 黑名单的思路是"我能想出所有危险的地方"；白名单的思路是"我知道哪里是安全的"。
# 前者每次漏掉一个新想法就是一个漏洞，后者默认就是拒绝。
WRITE_ALLOWLIST = (
    "/etc/autonomic/",
    "/etc/sysctl.d/",
    "/etc/systemd/system/",
    "/run/autonomic/",
)

# 保险丝：即使落在上面的前缀里，这几个地方也永远不碰。
WRITE_DENYLIST = (
    "/etc/shadow",
    "/etc/passwd",
    "/etc/gshadow",
    "/etc/group",
    "/etc/sudoers",
    "/etc/ssh/",
)

BACKUP_DIR = "/var/lib/autonomic/backups"
MAX_FILE_BYTES = 1 << 20  # 一个 action 最多写 1 MB：够改配置，不够塞东西

ACTIONS: dict = {}


# ------------------------------------------------------------------ 结果


def ok(detail: str, changed: bool = True, reversible: bool = True,
       evidence: dict | None = None) -> dict:
    return {
        "ok": True,
        "detail": detail,
        "changed": changed,
        "reversible": reversible,
        "evidence": evidence or {},
    }


def fail(detail: str, evidence: dict | None = None) -> dict:
    return {
        "ok": False,
        "detail": detail,
        "changed": False,
        "reversible": True,
        "evidence": evidence or {},
    }


# ------------------------------------------------------------------ 注册


def register(name: str, schema: tuple, *, reversible: bool):
    """把一个 action 登记进白名单。没登记的东西墙都进不来。

    schema 里 "key" 是必填，"key?" 是可填。
    reversible 强制写成关键字形式 —— 调用处看到 reversible=False 时
    应该停顿一下，那是"这一步回头不了"的意思。
    """

    def wrap(fn):
        fn.action_name = name
        fn.reversible = reversible
        fn.schema = schema
        ACTIONS[name] = fn
        return fn

    return wrap


def _check_schema(schema: tuple, params: dict) -> tuple[dict, str]:
    checked = dict(params)
    for spec in schema:
        required = not spec.endswith("?")
        key = spec.rstrip("?")
        if required and not str(checked.get(key, "")).strip():
            return {}, "缺必填参数：%s" % key
    return checked, ""


def dispatch(action: str, params: dict, phase: str) -> dict:
    """唯一的入口。actd 只调这个，不直接碰 action 函数。

    ⚠️ 这个函数最关键的一件事不是"调用 handler"，而是**让 reversible
    只能有一个来源**。

    踩过的坑（2026-10-11 装机验收实测抓到）：原来这里用
    ``result.setdefault("reversible", fn.reversible)``，而 ``ok()`` 的
    默认参数就是 ``reversible=True``。两者一叠加 —— **任何**返回 ``ok(...)``
    却没显式传 reversible 的 handler，dry_run 都会被报成"可逆"。

    症状：``user.add`` 登记为 ``reversible=False``，``autonomicctl actions``
    也正确显示"不可逆"，但它的 dry_run 输出是"可逆：是" —— 于是
    ``cmd_act`` 里那道两级授权拦截 ``if not preview.get("reversible")``
    **根本不触发**，一个不可逆的动作可以被一句 ``--yes`` 带过。

    而且早返回的路径（未知 action / 参数不合格 / handler 抛异常）都走
    ``fail()``，那个函数也硬写着 ``reversible: True`` —— 一并在下面收口。

    教训：可逆性**只能声明一次**（``@register``），别处一律是副本，
    而副本会撒谎。所以这里不是在"填默认值"，是**强制覆盖**。
    """

    def seal(res: dict, known: bool = True) -> dict:
        """把所有返回路径都收口：可逆性只认登记值，谁说了都不算。

        known=False 表示连 action 都没认出来（未知阶段 / 未知 action），
        这时无可逆性可言，如实标成 None，别假装它"可逆"。
        """
        res["reversible"] = fn.reversible if known else None
        res.setdefault("phase", phase)
        res.setdefault("action", action)
        return res

    if phase not in ("dry_run", "apply", "verify"):
        return seal(fail("未知阶段 %r（dry_run | apply | verify）" % phase), False)

    fn = ACTIONS.get(action)
    if fn is None:
        return seal(fail(
            "未知 action %r。已登记的：%s" % (action, ", ".join(sorted(ACTIONS)))
        ), False)

    checked, why = _check_schema(fn.schema, params)
    if why:
        return seal(fail(why))

    try:
        result = fn(checked, phase)
    except Exception as exc:  # 不能让一个 action 把 daemon 带走
        return seal(fail("%s 在 %s 阶段出错：%s: %s"
                         % (action, phase, type(exc).__name__, exc)))
    return seal(result)


def catalogue() -> list:
    return [
        {
            "name": fn.action_name,
            "reversible": fn.reversible,
            "params": list(fn.schema),
        }
        for fn in ACTIONS.values()
    ]


# ------------------------------------------------------------------ 工具


def _check_write_path(path: str) -> tuple[bool, str]:
    if not path.startswith("/"):
        return False, "路径必须是绝对路径：%r" % path
    # posixpath，不是 os.path：这里判断的是目标系统里的路径语义。
    real = posixpath.normpath(path)
    for bad in WRITE_DENYLIST:
        if real == bad or real.startswith(bad):
            return False, "拒绝：%s 在系统保护名单上" % real
    for prefix in WRITE_ALLOWLIST:
        if real.startswith(prefix):
            return True, real
    return False, (
        "拒绝：%s 不在可写目录里。允许的前缀：%s"
        % (real, ", ".join(WRITE_ALLOWLIST))
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _backup(path: str) -> str:
    """apply 之前把原文件存一份。回滚的凭据就指着它。

    备份名带微秒：原来只到秒，同一秒内对同一个文件连续 apply 两次，
    第二次会静默覆盖第一次的备份 —— 也就是把"上一版"弄丢了。回滚从来
    都是为了回到上一个状态，备份之间互相覆盖等于没备份。

    ⚠️ 微秒只能用 time.time() 自己算，**不能用 strftime("%f")** ——
    strftime 里根本没有 %f 这个指令（那是 datetime 的），它会把 %f
    原样吐出来，于是备份名里出现一个字面的 "%f"。后果和"只到秒"完全
    一样：同一秒内的两次备份还是撞在一起。这个坑已经踩过一次了。

    除了内容副本，还写一个同名 .json 的 sidecar 记录它**原本属于哪里**。
    这不是冗余：备份名里的路径分隔符是 `_`，而 Linux 路径本身允许 `_`，
    所以 `/etc/autonomic/my_conf` 编码成 `etc_autonomic_my_conf` 之后，
    谁也分不清那是三个目录还是两个目录加一个下划线。
    **猜一个文件从哪来，正是"回滚"变成"覆盖"的方式。**
    机器要还原，就必须读 sidecar；没有 sidecar 就拒绝还原。
    """
    if not os.path.exists(path):
        return ""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    now = time.time()
    micros = int(round((now - int(now)) * 1_000_000))
    # 万一四舍五入到 1000000（闰秒级的边界），挪进下一秒，别产生 7 位数
    if micros >= 1_000_000:
        micros = 999_999
    stamp = "%s%06d" % (time.strftime("%Y%m%dT%H%M%S", time.localtime(now)), micros)
    target = os.path.join(
        BACKUP_DIR, "%s_%s" % (stamp, path.strip("/").replace("/", "_"))
    )
    shutil.copy2(path, target)
    try:
        meta = {
            "path": path,
            "sha256": _sha(_read(path) or ""),
            "mode": oct(os.stat(path).st_mode & 0o7777),
            "at": stamp,
        }
        with open(target + ".json", "w", encoding="utf-8") as fh:
            json.dump(meta, fh, sort_keys=True)
    except OSError:
        # sidecar 写不出来不该让这次变更失败 —— 代价是这个备份将来不能被
        # file.restore 自动还原（它仍旧可以被人手动 cp 回去）。
        pass
    return target


def _run(argv: list) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


# ------------------------------------------------------------------ action: file.write


@register("file.write", ("path", "content", "mode?"), reversible=True)
def act_file_write(params: dict, phase: str) -> dict:
    raw_path = str(params["path"])
    content = params["content"]
    mode = str(params.get("mode") or "0644")

    allowed, resolved = _check_write_path(raw_path)
    if not allowed:
        return fail(resolved, {"path": raw_path})

    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        return fail("内容 %d 字节，超过 %d 上限"
                    % (len(content.encode("utf-8")), MAX_FILE_BYTES))

    before = _read(resolved)
    before_sha = _sha(before) if before is not None else ""
    after_sha = _sha(content)

    evidence = {
        "path": resolved,
        "existed": before is not None,
        "sha256_before": before_sha,
        "sha256_after": after_sha,
    }

    if before_sha == after_sha:
        return ok("内容已经是目标状态，无需改动", changed=False, evidence=evidence)

    if phase == "dry_run":
        lines_before = before.splitlines() if before else []
        lines_after = content.splitlines()
        return ok(
            "将 %s %s（%d 行 -> %d 行）"
            % ("改写" if before is not None else "新建", resolved,
               len(lines_before), len(lines_after)),
            evidence=evidence,
        )

    if phase == "apply":
        backup = _backup(resolved)
        evidence["backup"] = backup or "(原本不存在)"
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        tmp = resolved + ".autonomic.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(tmp, int(mode, 8))
        os.replace(tmp, resolved)
        return ok("已写入 %s" % resolved, evidence=evidence)

    # verify
    now = _read(resolved)
    if now is None:
        return fail("verify 失败：%s 不存在" % resolved, evidence)
    if _sha(now) != after_sha:
        return fail("verify 失败：%s 内容与目标不符" % resolved, evidence)
    return ok("已核实 %s 内容正确" % resolved, changed=False, evidence=evidence)


# ------------------------------------------------------------------ action: file.restore
#
# 这个 action 存在的理由只有一句：**reversible=True 之前只是一个声明。**
#
# 一个 action 说自己可逆，却没有任何人知道怎么把它转回去，那它说的是
# 一句愿望。file.write 每次 apply 都存了备份 + sidecar，但没有机制把备份
# 放回原位就等于：给你降落伞，但不给你拉环。
#
# 有了它，"AI 可以改一切" 才算配得上 "AI 也能收回它"。力气越大，越需要
# 能往回走的那条路，否则一次误会就是永久性的。


@register("file.restore", ("backup",), reversible=True)
def act_file_restore(params: dict, phase: str) -> dict:
    raw_backup = str(params["backup"]).strip()
    backup = posixpath.normpath(raw_backup)

    # 两端都要卡死：源必须在备份目录里，目标必须在可写前缀里。
    # 只卡一头的话，攻击者可以把"备份"指向任意文件再让它还原到别处。
    if not backup.startswith(BACKUP_DIR.rstrip("/") + "/"):
        return fail(
            "拒绝：备份文件必须在 %s/ 之下（拿到的是 %r）" % (BACKUP_DIR, raw_backup),
            {"backup": raw_backup},
        )
    if not os.path.isfile(backup):
        return fail("备份不存在：%s" % backup, {"backup": backup})

    meta_path = backup + ".json"
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
    except OSError:
        return fail(
            "备份缺少 sidecar（%s），无法确定它原本属于哪里。\n"
            "刻意不去猜文件名 —— 猜错一次就是把别人的文件覆盖掉。"
            % meta_path,
            {"backup": backup},
        )
    except ValueError:
        return fail("sidecar 不是合法 JSON：%s" % meta_path, {"backup": backup})

    target_raw = str(meta.get("path", ""))
    want_sha = str(meta.get("sha256", ""))
    if not target_raw or not want_sha:
        return fail("sidecar 缺少 path/sha256，拒绝还原", {"backup": backup})

    allowed, resolved = _check_write_path(target_raw)
    if not allowed:
        return fail(resolved, {"backup": backup})

    before = _read(resolved)
    evidence = {
        "restore_to": resolved,
        "backup": backup,
        "sha256_before": _sha(before) if before is not None else "(不存在)",
        "sha256_target": want_sha,
    }

    if before is not None and _sha(before) == want_sha:
        return ok("%s 已经就是备份里的样子，无需还原" % resolved,
                  changed=False, evidence=evidence)

    if phase == "dry_run":
        return ok(
            "将把 %s 还原到备份 %s 的内容" % (resolved, os.path.basename(backup)),
            evidence=evidence,
        )

    if phase == "apply":
        # 还原本身也要先备份现状：回滚不是单行道，回滚错了还得能再回滚。
        evidence["backup_of_current"] = _backup(resolved) or "(原本不存在)"
        tmp = resolved + ".autonomic.tmp"
        shutil.copyfile(backup, tmp)
        try:
            os.chmod(tmp, int(str(meta.get("mode", "0644")), 8))
        except (ValueError, OSError):
            os.chmod(tmp, 0o644)
        os.replace(tmp, resolved)
        return ok("已还原 %s" % resolved, evidence=evidence)

    now = _read(resolved)
    if now is None:
        return fail("verify 失败：%s 不存在" % resolved, evidence)
    if _sha(now) != want_sha:
        return fail("verify 失败：%s 内容与备份不符" % resolved, evidence)
    return ok("已核实 %s 与备份一致" % resolved, changed=False, evidence=evidence)


# ------------------------------------------------------------------ action: service.set


_SERVICE_STATES = ("started", "stopped", "restarted", "enabled", "disabled")


@register("service.set", ("unit", "state"), reversible=True)
def act_service_set(params: dict, phase: str) -> dict:
    unit = str(params["unit"]).strip()
    state = str(params["state"]).strip()

    if state not in _SERVICE_STATES:
        return fail("state 必须是 %s 之一" % ", ".join(_SERVICE_STATES))
    if unit.startswith("-") or "/" in unit or ".." in unit:
        return fail("unit 名不合法：%r" % unit)

    # is-active / is-enabled 是查询真相的唯一口径
    rc_active, active = _run(["systemctl", "is-active", unit])
    rc_enabled, enabled = _run(["systemctl", "is-enabled", unit])
    evidence = {
        "unit": unit,
        "active_before": active,
        "enabled_before": enabled,
    }

    verb = {"started": "start", "stopped": "stop", "restarted": "restart",
            "enabled": "enable", "disabled": "disable"}[state]

    already = (
        (state == "started" and rc_active == 0)
        or (state == "stopped" and rc_active != 0)
        or (state == "enabled" and rc_enabled == 0)
        or (state == "disabled" and rc_enabled != 0)
    )
    if already and state != "restarted":
        return ok("%s 已经是 %s" % (unit, state), changed=False, evidence=evidence)

    if phase == "dry_run":
        return ok("将执行 systemctl %s %s" % (verb, unit), evidence=evidence)

    if phase == "apply":
        rc, out = _run(["systemctl", verb, unit])
        evidence["systemctl_output"] = out
        if rc != 0:
            return fail("systemctl %s %s 失败：%s" % (verb, unit, out), evidence)
        return ok("已 systemctl %s %s" % (verb, unit), evidence=evidence)

    # verify
    if state in ("started", "restarted"):
        rc, cur = _run(["systemctl", "is-active", unit])
        good = rc == 0
        want = "active"
    elif state == "stopped":
        rc, cur = _run(["systemctl", "is-active", unit])
        good = rc != 0
        want = "inactive"
    else:
        rc, cur = _run(["systemctl", "is-enabled", unit])
        good = (rc == 0) == (state == "enabled")
        want = {"enabled": "enabled", "disabled": "disabled"}[state]
    evidence["observed"] = cur
    if not good:
        return fail("verify 失败：期望 %s，实际 %s" % (want, cur), evidence)
    return ok("已核实 %s 处于 %s" % (unit, want), changed=False, evidence=evidence)


# ------------------------------------------------------------------ action: sysctl.set


SYSCTL_FILE = "/etc/sysctl.d/99-autonomic.conf"


@register("sysctl.set", ("key", "value"), reversible=True)
def act_sysctl_set(params: dict, phase: str) -> dict:
    key = str(params["key"]).strip()
    value = str(params["value"]).strip()

    if not key or "." not in key or any(c in key for c in " ;|$&`"):
        return fail("key 不像一个 sysctl 参数名：%r" % key)
    if not value or any(c in value for c in " ;|$&`"):
        return fail("value 里有可疑字符：%r" % value)

    rc, current = _run(["sysctl", "-n", key])
    evidence = {"key": key, "value_before": current if rc == 0 else "(读取失败)"}

    if rc == 0 and current.strip() == value:
        return ok("%s 已经是 %s" % (key, value), changed=False, evidence=evidence)

    if phase == "dry_run":
        return ok("将设 %s = %s（当前 %s）"
                  % (key, value, current.strip() if rc == 0 else "未知"),
                  evidence=evidence)

    if phase == "apply":
        # 先落到 /etc/sysctl.d/ 保证重启后还在，再 -w 让它立刻生效。
        # 少任何一步都不算完成：只 -w 是重启即失，只写文件是现在不生效。
        existing = _read(SYSCTL_FILE) or ""
        lines = [ln for ln in existing.splitlines()
                 if ln.strip() and ln.split("=")[0].strip() != key]
        lines.append("%s = %s" % (key, value))
        backup = _backup(SYSCTL_FILE)
        evidence["backup"] = backup or "(原本不存在)"
        tmp = SYSCTL_FILE + ".autonomic.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, SYSCTL_FILE)

        rc_now, out_now = _run(["sysctl", "-w", "%s=%s" % (key, value)])
        evidence["sysctl_output"] = out_now
        if rc_now != 0:
            return fail("立刻生效失败（持久化文件已写）：%s" % out_now, evidence)
        # 写完立刻读一次再报成功。有些环境里 sysctl 返回码是 0 却根本没生效 ——
        # 比如执行器带着 systemd 的 ProtectKernelTunables=yes 时 /proc/sys 是
        # 只读的，或者容器环境压根不允许改。verify 迟早也会把这个差异指出来，
        # 但让 apply 当场承认，排查的人能少绕一圈。
        rc_check, got = _run(["sysctl", "-n", key])
        evidence["value_after_write"] = got.strip() if rc_check == 0 else "(读不到)"
        if rc_check != 0 or got.strip() != value:
            return fail(
                "写入后立刻复查没生效（想要 %s，实际 %s）。\n"
                "常见原因：执行器的 sandbox 禁止改内核参数"
                "（ProtectKernelTunables=yes），或容器/虚拟机环境不允许。"
                "这不是 bug —— 它说明边界真的在生效。"
                % (value, got.strip()),
                evidence,
            )
        return ok("已设 %s = %s，并写入 %s 以便重启后仍生效"
                  % (key, value, SYSCTL_FILE), evidence=evidence)

    rc_now, cur = _run(["sysctl", "-n", key])
    persisted = _read(SYSCTL_FILE) or ""
    evidence["value_after"] = cur.strip() if rc_now == 0 else "(读取失败)"
    evidence["persisted"] = key in persisted
    if rc_now != 0 or cur.strip() != value:
        return fail("verify 失败：%s 实际为 %s" % (key, cur.strip()), evidence)
    if not evidence["persisted"]:
        return fail("verify 失败：%s 当前生效了，但没持久化到 %s"
                    % (key, SYSCTL_FILE), evidence)
    return ok("已核实 %s = %s 且已持久化" % (key, value), changed=False,
              evidence=evidence)


# ------------------------------------------------------------------ action: pkg.*
#
# 软件包是这个系统"还是 Linux"的证明：能装 nginx 就能当服务器，能装 gcc 就能
# 编译，能装 python3 就能跑你自己的东西。没有 pkg.*，通道就只是"能改配置的
# 玩具"；有了它，AI 才真的能把这台机器变成用户想要的样子。
#
# 但 pkg.* 是这套设计里第一个**执行别人写的代码**的原语：apt 的 postinst
# 以 root 跑，装进来什么就执行什么。所以规则写死在这里，别处不许松：
#
#   1. 只走 `apt-get`，且参数逐个给出（argv 列表），**绝不经过 shell** ——
#      包名里任何一个 `;` `|` `$` 都到不了 shell，因为压根没有 shell。
#   2. 包名必须匹配 Debian 的合法字符集，且**不许以 `-` 开头** ——
#      否则 `--allow-unauthenticated` 这种东西会被 apt 当成自己的选项吃掉。
#   3. 不许带版本、不许带 `=`、不许带通配符。要装什么版本由仓库决定，
#      不由提议者决定：能指定版本就能把它降到一个有洞的版本。
#   4. 一次最多装/卸 5 个包。爆炸半径写死在代码里，不写在对面的自律里。

# Debian 包名（policy 7.6）：小写字母数字与 + - . ，必须以字母数字开头。
_PKG_RE = r"^[a-z0-9][a-z0-9+.\-]*$"
MAX_PKGS_PER_ACTION = 5
APT_TIMEOUT = 300


def _split_packages(raw) -> tuple[list, str]:
    """把 "nginx, curl" 拆成 [nginx, curl] 并逐个校验。

    也接受已经在列表里的形式（LLM 有时直接给数组）。返回 (列表, 错误)。
    """
    if isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw]
    else:
        items = [p.strip() for p in str(raw).replace("\n", ",").split(",")]
    items = [p for p in items if p]
    if not items:
        return [], "包名列表是空的"
    if len(items) > MAX_PKGS_PER_ACTION:
        return [], ("一次最多 %d 个包（给了 %d 个）。"
                    "分几次做，每一步都该有人能看明白。"
                    % (MAX_PKGS_PER_ACTION, len(items)))
    for p in items:
        if not re.match(_PKG_RE, p):
            return [], ("包名 %r 不合法（只允许小写字母、数字、+ . -，且以字母数字开头）。"
                        "想指定版本或选项的话，这条通道刻意不支持 —— "
                        "能挑版本就能挑到一个有洞的版本。" % p)
    return items, ""


def _dpkg_installed(pkg: str) -> bool:
    """这个包现在是'已配置'状态吗？dpkg-query 是唯一权威口径。"""
    rc, out = _run(["dpkg-query", "-W", "-f=${Status}", pkg])
    return rc == 0 and out.strip() == "install ok installed"


def _pkg_act(params: dict, phase: str, verb: str) -> dict:
    packages, why = _split_packages(params.get("packages"))
    if why:
        return fail(why)
    if verb == "install":
        already = [p for p in packages if _dpkg_installed(p)]
        todo = [p for p in packages if p not in already]
    else:
        already = [p for p in packages if not _dpkg_installed(p)]
        todo = [p for p in packages if p not in already]

    evidence = {
        "packages": packages,
        "already_in_state": already,
        "to_change": todo,
    }

    if not todo:
        return ok("这些包都已经在目标状态了：%s" % ", ".join(packages),
                  changed=False, evidence=evidence)

    # dry_run 和 apply **必须用同一套参数**。如果预演用的是 A、落地用的是 B，
    # 那"预演"就没有在预演真正要发生的那件事 —— 它会变成一个安心仪式。
    argv = ["apt-get", "-y", verb] + todo

    if phase == "dry_run":
        # -s（simulate）：让 apt 自己把"会装哪些、会卸哪些、要下多少"算一遍。
        # 免费，而且比我们自己猜依赖树可信得多。
        # 注意这里刻意**不加 --no-install-recommends**：用户说"装 nginx"
        # 要的是能用的 nginx，不是缺了一半配置的精简版。预演会把连带装进来的
        # 数量算出来给人看，要不要接受是人的决定，不是这条通道替人做的决定。
        rc, out = _run(["apt-get", "-s", verb] + todo)
        evidence["simulate_tail"] = "\n".join(out.splitlines()[-6:])
        if rc != 0:
            return fail("apt 预演失败（这条计划落不了地）：%s" % out[-400:], evidence)
        # 从 -s 的输出里数出"将会新装 N 个包"，这是人点头之前最该看到的一个数
        extra = 0
        for ln in out.splitlines():
            m = re.search(r"(\d+) newly installed", ln)
            if m:
                extra = int(m.group(1))
        evidence["newly_installed"] = extra
        tail = [ln for ln in out.splitlines()
                if ln.startswith("Need to get") or "newly installed" in ln
                or "to remove" in ln]
        return ok("将 %s：%s。%s"
                  % (verb, ", ".join(todo), " / ".join(tail) if tail else ""),
                  evidence=evidence)

    if phase == "apply":
        env = dict(os.environ)
        env.update({
            "DEBIAN_FRONTEND": "noninteractive",
            "APT_LISTCHANGES_FRONTEND": "none",
            "LC_ALL": "C",
        })
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=APT_TIMEOUT, env=env,
            )
        except subprocess.TimeoutExpired:
            return fail("apt-get %s 超过 %ds 没结束，已放弃" % (verb, APT_TIMEOUT),
                        evidence)
        out = (proc.stdout + proc.stderr).strip()
        evidence["apt_rc"] = proc.returncode
        evidence["apt_tail"] = "\n".join(out.splitlines()[-8:])
        if proc.returncode != 0:
            return fail("apt-get %s 失败（rc=%d）：%s"
                        % (verb, proc.returncode, out[-400:]), evidence)
        return ok("已 %s：%s" % (verb, ", ".join(todo)), evidence=evidence)

    # verify：不看 apt 说了什么，只问 dpkg 现在是什么状态
    wrong = []
    for p in packages:
        want = (verb == "install")
        if _dpkg_installed(p) != want:
            wrong.append(p)
    evidence["still_wrong"] = wrong
    if wrong:
        return fail("verify 失败：这些包不在目标状态：%s" % ", ".join(wrong), evidence)
    return ok("已核实 %s 个包处于目标状态" % len(packages), changed=False,
              evidence=evidence)


@register("pkg.install", ("packages",), reversible=True)
def act_pkg_install(params: dict, phase: str) -> dict:
    """装包。标 reversible=True 是**诚实但有限度**的：`pkg.remove` 能把包拿掉，
    但 /etc 下的配置残留不会自动消失（那要 purge），家目录里的数据也不会。

    之所以仍然标 True：这条通道承诺的是"有回头路"，而不是"世界完全复原"。
    把"卸了但留了配置"说成不可逆，会让真正不可逆的那些（user.add）失去重量。
    诚实的做法是把边界写在明处，而不是把所有事都夸大。
    """
    return _pkg_act(params, phase, "install")


@register("pkg.remove", ("packages",), reversible=True)
def act_pkg_remove(params: dict, phase: str) -> dict:
    """卸包。和 install 一样，卸载本身可逆（再装回来即可），配置残留归 purge 管。"""
    return _pkg_act(params, phase, "remove")


@register("pkg.list", ("pattern?",), reversible=True)
def act_pkg_list(params: dict, phase: str) -> dict:
    """只读查询：这台机器上装了什么。给 LLM 用的 —— 它要规划"装 nginx 之前
    先看看是不是已经有 apache"，就得能问这一句。只读，三段行为一致。
    """
    pattern = str(params.get("pattern") or "*").strip() or "*"
    # 允许 * ? 通配，但不允许空格/引号 —— dpkg-query 的 pattern 不该有这些
    if any(c in pattern for c in " ;|$&`'\"\\"):
        return fail("pattern 里有可疑字符：%r" % pattern)
    rc, out = _run(["dpkg-query", "-W", "-f=${Package} ${Version}\\n", pattern])
    lines = [ln for ln in out.splitlines() if ln.strip()]
    evidence = {"pattern": pattern, "count": len(lines),
                "sample": lines[:40]}
    if rc != 0 and not lines:
        # dpkg-query 在"一个都没匹配上"时返回 1，这不是错误，是"没有"
        return ok("没有匹配 %r 的包" % pattern, changed=False, evidence=evidence)
    return ok("匹配 %r 的包有 %d 个" % (pattern, len(lines)),
              changed=False, evidence=evidence)


# ------------------------------------------------------------------ action: user.*
#
# 这是整条通道上第一个**真正不可逆**的原语，也是第一次让
# `--force-irreversible` 这个开关有实际用途。
#
# 为什么 user.add 不可逆：删掉一个用户，不会把它创建过的文件、发过的邮件、
# 跑过的服务一起带走。`userdel -r` 会删家目录，但散落在 /var、/srv、数据库里
# 的东西还在，UID 也可能已经被别的进程写进了配置。**把"删了用户"当成
# "回到没建过这个用户"是错的**，所以它必须标 reversible=False，让调用者
# 在动手前停顿一次。这个停顿就是安全机制本身。


# 用户名：POSIX 可移植字符集，不能以 - 开头，不能是纯数字（会撞 UID）
_USER_RE = r"^[a-z_][a-z0-9_-]{0,31}$"

# 允许的登录 shell。**白名单**，不是"看着像路径就放行"：
# 让调用者指定任意路径 = 让他在 /etc/passwd 里挂一个任意可执行文件，
# 而 /etc/passwd 恰好在 DENYLIST 上正是为了不让通道改它 —— 那么从
# 这里开后门就毫无意义了。
_ALLOWED_SHELLS = (
    "/bin/bash", "/bin/sh", "/bin/zsh", "/usr/bin/fish",
    "/usr/sbin/nologin", "/bin/false",
)


def _passwd_lookup(name: str) -> dict | None:
    """查一个用户在 /etc/passwd 里的条目；没有就返回 None。

    单独抽出来是为了两件事：
      1. 让 act_user_add 的三个阶段共用同一个"真相来源"，而不是各写一遍
         try/import/except（三份副本迟早会漂移）；
      2. 让这段平台相关的代码**可以在测试里被替换掉** —— `pwd` 模块只有
         POSIX 有，不抽出来的话，在非 Linux 上跑测试时 user.add 永远只会
         走到"ModuleNotFoundError"那一条分支，dry_run 的正常路径一次都测不到。
    """
    try:
        import pwd
    except ImportError:
        return None
    try:
        ent = pwd.getpwnam(name)
    except KeyError:
        return None
    return {"uid": ent.pw_uid, "home": ent.pw_dir, "shell": ent.pw_shell}


@register("user.add", ("name", "shell?", "groups?", "gecos?"), reversible=False)
def act_user_add(params: dict, phase: str) -> dict:
    name = str(params["name"]).strip()
    shell = str(params.get("shell") or "/bin/bash").strip()
    groups = str(params.get("groups") or "").strip()
    gecos = str(params.get("gecos") or "").strip()[:64]

    if not re.match(_USER_RE, name):
        return fail("用户名 %r 不合法（小写字母/下划线开头，后接字母数字_-，最长 32）"
                    % name)
    if shell not in _ALLOWED_SHELLS:
        return fail("shell %r 不在允许列表里：%s"
                    % (shell, ", ".join(_ALLOWED_SHELLS)))
    if groups and any(c in groups for c in " ;|$&`'\""):
        return fail("groups 里有可疑字符：%r" % groups)

    existing = _passwd_lookup(name)

    evidence = {"name": name, "shell": shell, "groups": groups,
                "already_exists": existing is not None}

    # ⚠️ 这个"已经存在就早返回"**只能用在 dry_run**。
    #
    # 一开始它写在三个阶段前面，结果 verify 变成了一个空操作：
    # apply 建完用户，verify 一看"用户存在"就报成功返回 —— 可它从没检查过
    # 建出来的那个用户 shell 对不对、uid 是不是我们要的。**verify 的全部
    # 价值就是"去现场把 apply 承诺的每一项再数一遍"，提前返回等于自己
    # 把这一步废掉了。**
    #
    # 所以：dry_run 看到"已存在"可以说"无需改动"（这是给操作者的预告）；
    # verify 必须继续往下走到真正的核对。
    if existing is not None and phase == "dry_run":
        return ok("用户 %s 已经存在" % name, changed=False, evidence=evidence)

    if phase == "dry_run":
        return ok("将新建用户 %s（shell=%s%s）"
                  % (name, shell, "，附加组=" + groups if groups else ""),
                  evidence=evidence)

    if phase == "apply":
        # 已经存在就别再 useradd 了 —— 它会失败，而"你要建的用户早就有了"
        # 不是一次失败，是一次"无需改动"。apply 也不该把已经正确的事再报
        # 成"我改了它"：世界版本号是要为真实变更 +1 的。
        if existing is not None:
            return ok("用户 %s 已经存在，无需新建" % name, changed=False,
                      evidence=evidence)
        argv = ["useradd", "-m", "-s", shell]
        if gecos:
            argv += ["-c", gecos]
        if groups:
            argv += ["-G", groups]
        argv.append(name)
        rc, out = _run(argv)
        evidence["useradd_output"] = out
        if rc != 0:
            return fail("useradd 失败（rc=%d）：%s" % (rc, out), evidence)
        return ok("已新建用户 %s" % name, evidence=evidence)

    # verify：去 /etc/passwd 再看一眼，而不是相信 useradd 的返回码
    ent = _passwd_lookup(name)
    if ent is None:
        return fail("verify 失败：用户 %s 不存在" % name, evidence)
    evidence.update(ent)
    if ent["shell"] != shell:
        return fail("verify 失败：shell 是 %s，期望 %s" % (ent["shell"], shell),
                    evidence)
    return ok("已核实用户 %s（uid=%d，家目录 %s）"
              % (name, ent["uid"], ent["home"]), changed=False, evidence=evidence)


# ------------------------------------------------------------------ action: sys.read
#
# 为什么感知也要走通道，而不是让 agent 自己去读 /proc：
#
#   1. **它读不到。** agent 跑在 ProtectSystem=strict + ProtectProc=invisible
#      下面，看不见别的进程，也看不见大部分 /proc 和 /sys。想让它"看见"，
#      要么放宽它的沙箱（那是**加权力**），要么让有权限的一方替它读（这是
#      **加能力**）。这套设计的全部纪律都在选后者。
#
#   2. **看也要留痕。** "agent 什么时候看了什么"和"改了什么"一样是审计的
#      一部分 —— 一个半夜在遍历 /proc 的进程是值得问一句"它在找什么"的。
#
#   3. **必须是白名单化的指标名，不能是"读任意文件"。** 后者等于开了一个
#      任意文件读取漏洞：/etc/shadow 也是文件。所以下面这张表是**闭集**，
#      每个名字对应一段我们写死的读法，调用者给不出路径。
#
# 只读原语一律 changed=False：它们不改变世界，所以也不推进世界版本号。
# 一个会把 action_seq 推高的"读取"会让账本失去意义。
#
# 平台相关的那几段（statvfs / sched_getaffinity）全部藏在**可替换的小函数**
# 里，理由和 bug 3 的 `_passwd_lookup` 一模一样：POSIX-only 的调用会让这些
# 原语在非 Linux 上永远只撞 AttributeError，正常路径一次都跑不到，测试就
# 只剩下错误分支可测。抽成函数之后，测试可以替掉它们，走完整条路。

# 名字 -> (读法, 参数)
# 读法要么是 ("file", 路径) 读一个小文件，要么是 ("count_cpu", None) /
# ("disk_usage", None) 这类我们写死的小逻辑。
_READ_PROBES = {
    "os.release": ("file", "/etc/os-release"),
    "kernel": ("file", "/proc/sys/kernel/osrelease"),
    "uptime": ("file", "/proc/uptime"),
    "loadavg": ("file", "/proc/loadavg"),
    "meminfo": ("file", "/proc/meminfo"),
    "cpuinfo_count": ("count_cpu", None),
    "disk_usage": ("disk_usage", None),
    "hostname": ("file", "/proc/sys/kernel/hostname"),
    "timezone": ("file", "/etc/timezone"),
}


def _probe_disk_usage() -> str:
    """根文件系统的使用情况。给 agent 一个'这台机器还喘得过来吗'的抓手。"""
    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:
        # 不是 statvfs 失败，是**这台机器的 Python 根本没有这个调用**。
        # 两者要说清楚："没有这个指标"和"有指标但我读不动"对操作者是
        # 两件不同的事，混成一句话会让人去查权限，其实该查的是平台。
        return "(这台机器的 Python 上没有 statvfs，指标不可得)"
    try:
        st = statvfs("/")
    except OSError as exc:
        return "(statvfs 失败：%s)" % exc
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - free
    if total <= 0:
        return "(读到 0 字节的容量，跳过)"
    return "total=%d used=%d free=%d used_pct=%.1f%%" % (
        total, used, free, 100.0 * used / total)


def _probe_cpu_count() -> str:
    getaff = getattr(os, "sched_getaffinity", None)
    if getaff is not None:
        try:
            return "online_cpus=%d" % len(getaff(0))
        except OSError:
            pass
    n = 0
    try:
        with open("/proc/cpuinfo") as fh:
            n = sum(1 for ln in fh if ln.startswith("processor"))
    except OSError:
        return "(既没有 sched_getaffinity，也读不到 /proc/cpuinfo，指标不可得)"
    return "online_cpus=%d (from /proc/cpuinfo)" % n


@register("sys.read", ("metric",), reversible=True)
def act_sys_read(params: dict, phase: str) -> dict:
    """只读地看一眼这台机器的某个指标。

    metric 必须在这张闭集表里 —— 调用者提供的是**指标名**，不是路径。
    想加新指标就改这张表；改表要过 code review、要写测试，
    而不是由提示词里的一句话决定。
    """
    metric = str(params["metric"]).strip()
    if metric not in _READ_PROBES:
        return fail(
            "未知指标 %r。可读的指标只有这些：%s"
            % (metric, ", ".join(sorted(_READ_PROBES))),
            {"metric": metric, "known": sorted(_READ_PROBES)},
        )

    kind, arg = _READ_PROBES[metric]
    if kind == "file":
        text = _read(arg)
        if text is None:
            # ⚠️ 这句话刻意说得含糊，是因为**在 actd 里我们分不清**：
            # agent 侧看是"路径不存在"，但真到了 root 上，多半是
            # 这台机器（容器/非 Linux）压根没这个文件。硬要二选一
            # 会给出确定的错答案，所以并列两种情况，让操作者自己核。
            return fail(
                "读不到指标 %s（%s）—— 可能这台机器上没有这个文件，"
                "也可能被沙箱挡住" % (metric, arg),
                {"metric": metric, "path": arg, "kind": kind})
        value = text.strip()
        # os-release 是一堆 KEY=value，压成一行更好放进 journal
        if metric == "os.release":
            for ln in value.splitlines():
                if ln.startswith("PRETTY_NAME="):
                    value = ln.split("=", 1)[1].strip().strip('"')
                    break
    elif kind == "count_cpu":
        value = _probe_cpu_count()
    elif kind == "disk_usage":
        value = _probe_disk_usage()
    else:  # pragma: no cover - 表是闭集，写错在这里就会炸而不是静默
        return fail("内部错误：指标 %r 的读法 %r 没实现" % (metric, kind))

    # 只读：三个阶段的回答完全一样，身份保持不变。
    # 刻意不主张"可逆/不可逆"那套语义 —— 它没改任何东西，所以 reversible
    # 这个字段对它没有意义（dispatcher 仍会按登记的 True 填，无害）。
    return ok("%s = %s" % (metric, value), changed=False,
              evidence={"metric": metric, "value": value})


# 服务的状态，只读。为什么单独一个原语而不是让 sys.read 去读 systemctl：
# 它走的是 systemctl 的机器可读输出，而"解析 systemctl 输出"这件事
# 应该只有一份实现。
@register("service.status", ("unit",), reversible=True)
def act_service_status(params: dict, phase: str) -> dict:
    unit = str(params["unit"]).strip()
    if unit.startswith("-") or "/" in unit or ".." in unit:
        return fail("unit 名不合法：%r" % unit)
    rc_a, active = _run(["systemctl", "is-active", unit])
    rc_e, enabled = _run(["systemctl", "is-enabled", unit])
    rc_s, sub = _run(["systemctl", "show", "-p", "SubState", "--value", unit])
    value = "active=%s enabled=%s substate=%s" % (
        active or "unknown", enabled or "unknown", sub or "unknown")
    return ok("%s: %s" % (unit, value), changed=False,
              evidence={"unit": unit, "active": active, "enabled": enabled,
                        "substate": sub})


# ------------------------------------------------------------------ action: self.test


@register("self.test", (), reversible=True)
def act_self_test(params: dict, phase: str) -> dict:
    """通道本身的体检：三段跑通说明从 agent 到系统这条路是通的。

    故意在允许目录里留一个真实文件 —— 抽象地"报告成功"没有意义，
    能在磁盘上指出一个刚被改动过的字节才算证据。

    ⚠️ 内容必须是**确定性的**。这里曾经塞过 time.time()，后果是 apply
    写进去 T1、verify 重算出 T2，三方永远对不上，通道把自己判成失败。
    **这是 Action 通道的一条硬约束：不幂等的 action 无法被验证。**
    同理，内容里也不能放 phase —— apply 和 verify 的 phase 本来就不同。
    """
    probe = "/run/autonomic/channel.probe"
    stamp = json.dumps(
        {
            "action": "self.test",
            "version": VERSION,
            "host": socket.gethostname(),
        },
        sort_keys=True,
    )
    return act_file_write({"path": probe, "content": stamp}, phase)
