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

VERSION = "0.4.0"

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

    if phase == "dry_run":
        # --simulate：让 apt 自己把"会装哪些、会卸哪些、要下多少"算一遍。
        # 这一步是免费的，而且比我们自己猜依赖树可信得多。
        rc, out = _run(["apt-get", "-s", verb] + todo)
        evidence["simulate_tail"] = "\n".join(out.splitlines()[-6:])
        if rc != 0:
            return fail("apt 预演失败（这条计划落不了地）：%s" % out[-400:], evidence)
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
                ["apt-get", "-y", "--no-install-recommends", verb] + todo,
                capture_output=True, text=True, timeout=APT_TIMEOUT, env=env,
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


@register("user.add", ("name", "shell?", "groups?", "gecos?"), reversible=False)
def act_user_add(params: dict, phase: str) -> dict:
    name = str(params["name"]).strip()
    shell = str(params.get("shell") or "/bin/bash").strip()
    groups = str(params.get("groups") or "").strip()
    gecos = str(params.get("gecos") or "").strip()[:64]

    if not re.match(_USER_RE, name):
        return fail("用户名 %r 不合法（小写字母/下划线开头，后接字母数字_-，最长 32）"
                    % name)
    if shell not in ("/bin/bash", "/bin/sh", "/bin/zsh", "/usr/bin/fish",
                     "/usr/sbin/nologin", "/bin/false"):
        return fail("shell %r 不在允许列表里" % shell)
    if groups and any(c in groups for c in " ;|$&`'\""):
        return fail("groups 里有可疑字符：%r" % groups)

    try:
        import pwd as _pwd
        exists = _pwd.getpwnam(name) is not None
    except KeyError:
        exists = False

    evidence = {"name": name, "shell": shell, "groups": groups,
                "already_exists": exists}

    if exists:
        return ok("用户 %s 已经存在" % name, changed=False, evidence=evidence)

    if phase == "dry_run":
        return ok("将新建用户 %s（shell=%s%s）"
                  % (name, shell, "，附加组=" + groups if groups else ""),
                  evidence=evidence)

    if phase == "apply":
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

    # verify
    try:
        import pwd as _pwd
        ent = _pwd.getpwnam(name)
    except KeyError:
        return fail("verify 失败：用户 %s 不存在" % name, evidence)
    evidence["uid"] = ent.pw_uid
    evidence["home"] = ent.pw_dir
    if ent.pw_shell != shell:
        return fail("verify 失败：shell 是 %s，期望 %s" % (ent.pw_shell, shell),
                    evidence)
    return ok("已核实用户 %s（uid=%d，家目录 %s）" % (name, ent.pw_uid, ent.pw_dir),
              changed=False, evidence=evidence)


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
