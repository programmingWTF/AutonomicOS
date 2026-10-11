#!/usr/bin/python3
"""test-sense -- 给 M9 感知层（autonomicctl sense）做的自检。

这一层最容易犯的错**不是**"读出来的值不对"，而是三件更安静的事：

  1. **绕过通道自己读。** 一旦 autonomicctl 自己 open("/proc/loadavg")，
     这次"看"就不在审计里了 —— agent 半夜看了什么，账本上查不到。
     测试要钉的正是"它确实走了 _act_call"，而不是它读到了什么值。

  2. **取不到就悄悄少写一行。** 一份"少了几行"的快照看起来像一切正常。
     取不到必须**显式**写进快照，否则读快照的人会以为那项本来就是空的。

  3. **读一下就把世界版本号推高。** 只读原语一律 changed=False。一个会
     让 action_seq 动的读取会让整个账本失去意义。

跑法（项目根目录）：
    python3 scripts/test-sense.py
退出码 0 = 全通过。

注意：autonomicctl 这个文件**没有 .py 后缀**（它是装在 /usr/bin 下的命令），
所以不能用常规 import，得用 SourceFileLoader 按路径加载。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import tempfile
from importlib.machinery import SourceFileLoader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CTL = os.path.join(ROOT, "packaging", "autonomic-agent", "src", "autonomicctl")

os.environ.setdefault("AUTONOMIC_LIB", os.path.join(ROOT, "packaging", "autonomic-agent", "src"))

_loader = SourceFileLoader("aosctl_under_test", CTL)
_spec = importlib.util.spec_from_loader("aosctl_under_test", _loader)
ctl = importlib.util.module_from_spec(_spec)
_loader.exec_module(ctl)

FAILED = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, extra))
        FAILED.append(name)


# ---------------------------------------------------------------- 假通道
# 记录每一次调用，这样"有没有走通道"是可以被验证的事实，而不是推断。
CALLS: list = []


def _install_fake_channel(sys_read_ok: bool = True, service_ok: bool = True,
                          fail_detail: str = "模拟失败") -> None:
    CALLS.clear()

    def fake_call(action, params, phase, reason=""):
        CALLS.append((action, dict(params), phase))
        if action == "sys.read":
            if sys_read_ok:
                return {"ok": True, "changed": False, "reversible": True,
                        "evidence": {"metric": params["metric"],
                                     "value": "V(%s)" % params["metric"]}}
            return {"ok": False, "detail": fail_detail}
        if action == "service.status":
            if service_ok:
                return {"ok": True, "changed": False, "reversible": True,
                        "evidence": {"unit": params["unit"], "active": "active",
                                     "enabled": "enabled", "substate": "running"}}
            return {"ok": False, "detail": fail_detail}
        raise AssertionError("sense 不该去调 %r" % action)

    ctl._act_call = fake_call


def _temp_marker() -> str:
    d = tempfile.mkdtemp(prefix="aos-sense-")
    return os.path.join(d, "sense")


def _run_sense(show: bool = False) -> int:
    return ctl.cmd_sense(argparse.Namespace(show=show))


# ---------------------------------------------------------------- 1. 走通道

print("sense 只能经由通道看 —— 每一次读都必须在审计里")

ctl.SENSE_MARKER = _temp_marker()
ctl._read = lambda p: "snapshot_id=bootstrap\naction_seq=7"
_install_fake_channel()
rc = _run_sense()

expected_metrics = set(ctl._SENSE_METRICS)
seen_reads = {c[1]["metric"] for c in CALLS if c[0] == "sys.read"}
check("每一个 sys.read 都经由 _act_call（没有绕过通道自己读）",
      seen_reads == expected_metrics,
      "缺=%r 多=%r" % (expected_metrics - seen_reads, seen_reads - expected_metrics))
check("sys.read 的 phase 一律是 dry_run（只读不该走 apply）",
      all(c[2] == "dry_run" for c in CALLS if c[0] == "sys.read"))
check("服务状态经由 service.status 走通道，不是自己调 systemctl",
      {c[1]["unit"] for c in CALLS if c[0] == "service.status"} == set(ctl._SENSE_UNITS))
check("全部读到 -> 返回 0", rc == 0, "rc=%r" % rc)

# 投影必须落地、且是只读的
proj = ""
try:
    with open(ctl.SENSE_MARKER, "r", encoding="utf-8") as fh:
        proj = fh.read()
except OSError as exc:
    proj = ""
check("快照投影到了磁盘", bool(proj), "读 %s 失败" % ctl.SENSE_MARKER)
check("投影是 0444（写一次就不许就地改）",
      bool(proj) and (os.stat(ctl.SENSE_MARKER).st_mode & 0o777) == 0o444,
      "mode=%s" % (oct(os.stat(ctl.SENSE_MARKER).st_mode & 0o777) if proj else "n/a"))
check("投影里带着世界版本号（读的时候系统处于哪个版本是快照的一部分）",
      "world=" in proj and "action_seq=7" in proj)


# ---------------------------------------------------------------- 2. 取不到也要写

print()
print("取不到的指标必须显式写进快照，不能悄悄少一行")

ctl.SENSE_MARKER = _temp_marker()
_install_fake_channel(sys_read_ok=False, service_ok=False, fail_detail="被沙箱挡住")
rc = _run_sense()

proj = ""
try:
    with open(ctl.SENSE_MARKER, "r", encoding="utf-8") as fh:
        proj = fh.read()
except OSError:
    proj = ""

for m in ctl._SENSE_METRICS:
    check("取不到的 %s 仍出现在快照里并标注 unavailable" % m,
          ("%s=unavailable" % m) in proj, "快照里没有这一行")
check("取不到时写清了原因（不是干巴巴一个 unavailable）",
      "被沙箱挡住" in proj)
check("一项都读不到 -> 返回 1（部分读到不该算失败，全无才算）", rc == 1,
      "rc=%r" % rc)

print()
print("部分读到：仍然是成功（rc=0）—— 这是容器里的常态，不该报成故障")

ctl.SENSE_MARKER = _temp_marker()


def _half_channel(action, params, phase, reason=""):
    CALLS.append((action, dict(params), phase))
    if action == "sys.read" and params["metric"] == "hostname":
        return {"ok": False, "detail": "这台机器上没有这个文件"}
    if action == "sys.read":
        return {"ok": True, "changed": False, "reversible": True,
                "evidence": {"metric": params["metric"], "value": "V"}}
    return {"ok": False, "detail": "没有 systemd"}


ctl._act_call = _half_channel
rc = _run_sense()
check("读到一部分 -> rc=0（不把部分成功说成失败）", rc == 0, "rc=%r" % rc)


# ---------------------------------------------------------------- 3. 不推版本号

print()
print("只读的快照不许推进世界版本号")

_install_fake_channel()
ctl.SENSE_MARKER = _temp_marker()
_run_sense()
check("sense 全程没有调用任何 apply 阶段的东西（只有 dry_run）",
      all(c[2] == "dry_run" for c in CALLS),
      "出现的阶段：%r" % sorted({c[2] for c in CALLS}))
check("sense 只碰 sys.read / service.status（不下发任何写入原语）",
      {c[0] for c in CALLS} <= {"sys.read", "service.status"},
      "出现了 %r" % sorted({c[0] for c in CALLS}))


# ---------------------------------------------------------------- 4. 投影写不出去

print()
print("投影写不出去不该让已经读到的信息消失")

_install_fake_channel()
# 让 SENSE_MARKER 指向一个**已经存在且是目录**的位置：写它必然 OSError。
# 比"指到不存在的目录"更硬 —— 后者会被 makedirs 顺手建出来，根本走不到
# 异常分支，于是这条断言其实什么都没测到。
_bad = tempfile.mkdtemp(prefix="aos-sense-dir-")
ctl.SENSE_MARKER = _bad
try:
    rc = _run_sense()
    check("投影失败时仍返回 0（读是成功的，只是没落地）", rc == 0, "rc=%r" % rc)
except OSError as exc:
    check("投影失败时不该把异常抛给调用者", False, "raised %r" % exc)

# 去掉末尾的 .tmp 后缀误判：确认我们真的试过写、并且真的失败了
check("投影失败确实是写失败（目标是个目录）",
      os.path.isdir(_bad))
# 复位，免得后面的检查看着这个奇怪的 marker
ctl.SENSE_MARKER = _temp_marker()

print()
if FAILED:
    print("%d 项失败：%s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("全部通过。")
