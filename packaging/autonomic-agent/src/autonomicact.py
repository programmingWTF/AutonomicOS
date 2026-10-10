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
import shutil
import socket
import subprocess
import time

VERSION = "0.2.0"

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
    """唯一的入口。actd 只调这个，不直接碰 action 函数。"""
    if phase not in ("dry_run", "apply", "verify"):
        return fail("未知阶段 %r（dry_run | apply | verify）" % phase)

    fn = ACTIONS.get(action)
    if fn is None:
        return fail(
            "未知 action %r。已登记的：%s" % (action, ", ".join(sorted(ACTIONS)))
        )

    checked, why = _check_schema(fn.schema, params)
    if why:
        return fail(why)

    try:
        result = fn(checked, phase)
    except Exception as exc:  # 不能让一个 action 把 daemon 带走
        return fail("%s 在 %s 阶段出错：%s: %s"
                    % (action, phase, type(exc).__name__, exc))
    result.setdefault("reversible", fn.reversible)
    result.setdefault("phase", phase)
    result.setdefault("action", action)
    return result


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
    real = os.path.normpath(path)
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
    """apply 之前把原文件存一份。回滚的凭据就指着它。"""
    if not os.path.exists(path):
        return ""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    target = os.path.join(
        BACKUP_DIR, "%s_%s" % (stamp, path.strip("/").replace("/", "_"))
    )
    shutil.copy2(path, target)
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
