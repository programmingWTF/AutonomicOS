#!/usr/bin/python3
"""test-primitives -- 给新增原语（pkg.*、user.*）做的纯逻辑自检。

这些函数是**参数校验的最后一关**。pkg.* 会执行别人写的 postinst（以 root），
user.* 不可逆 —— 校验一旦漏掉一种输入，代价不是"操作失败"，而是"操作成功但
做错事"。它们恰好是纯函数，所以能被真实验证，不该靠"看起来对"。

跑法（项目根目录）：
    python3 scripts/test-primitives.py
退出码 0 = 全通过。
"""

from __future__ import annotations

import inspect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "packaging", "autonomic-agent", "src"))

import autonomicact as act  # noqa: E402

FAILED = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, extra))
        FAILED.append(name)


print("原语清单：新加的东西必须真的登记上，且边界声明正确")

check("pkg.install 已登记", "pkg.install" in act.ACTIONS)
check("pkg.remove 已登记", "pkg.remove" in act.ACTIONS)
check("pkg.list 已登记", "pkg.list" in act.ACTIONS)
check("user.add 已登记", "user.add" in act.ACTIONS)

# 这是本轮最重要的一条断言：不可逆必须被**声明**出来。
# 若 user.add 悄悄标成 reversible=True，调用侧就不会要求 --force-irreversible，
# 于是一个本该书面上不可逆的动作可以被一句"继续"带过。
check("user.add 声明为不可逆", act.ACTIONS["user.add"].reversible is False,
      "user.add.reversible = %r" % act.ACTIONS["user.add"].reversible)
check("pkg.install 声明为可逆（apt remove 能拿掉）",
      act.ACTIONS["pkg.install"].reversible is True)
check("pkg.list 是只读（可逆）", act.ACTIONS["pkg.list"].reversible is True)

print()
print("_split_packages：包名是第一个注入面，必须一个都不放过")

# 合法：正常包名
ok_list, err = act._split_packages("nginx, curl")
check("逗号分隔的多个包能拆开", ok_list == ["nginx", "curl"] and not err, err)

ok_list, err = act._split_packages(["ca-certificates", "gcc-12"])
check("已经是数组的能直接收", ok_list == ["ca-certificates", "gcc-12"] and not err, err)

ok_list, err = act._split_packages("python3.11")
check("包名里的点号合法", ok_list == ["python3.11"] and not err, err)

# 关键：以 - 开头会被 apt 当成自己的选项吃下去（--allow-unauthenticated 之类）
_, err = act._split_packages("--allow-unauthenticated")
check("以 - 开头的'包名'被拒（防 apt 选项注入）", bool(err), err)

_, err = act._split_packages("-o")
check("单个 -o 被拒", bool(err), err)

# 命令注入的经典形状：没有 shell 也照样要在这一层挡掉，纵深防御
for bad in ("nginx; rm -rf /", "a$(id)", "a`id`", "a|b", "a b", "A-Upper"):
    _, err = act._split_packages(bad)
    check("非法包名 %r 被拒" % bad, bool(err), err)

# 版本指定：能挑版本就能挑到一个有洞的版本
_, err = act._split_packages("nginx=1.14")
check("带 = 指定版本被拒", bool(err), err)

_, err = act._split_packages("")
check("空列表被拒", bool(err), err)

_, err = act._split_packages("a,b,c,d,e,f")
check("超过 %d 个包被拒（爆炸半径写死在代码里）" % act.MAX_PKGS_PER_ACTION,
      bool(err), err)

# 边界：正好 5 个应该过，第 6 个才拒
ok_list, err = act._split_packages("a,b,c,d,e")
check("正好 5 个包通过", len(ok_list) == 5 and not err, err)

print()
print("_USER_RE：用户名要过 POSIX 可移植字符集，且不能是纯数字")

import re  # noqa: E402
for good_user in ("alice", "_svc", "git-lab", "user_1"):
    check("合法用户名 %r" % good_user, bool(re.match(act._USER_RE, good_user)))
for bad_user in ("Alice", "1lady", "-lead", "a b", "user;rm", "", "a" * 40):
    check("非法用户名 %r 被拒" % bad_user,
          not re.match(act._USER_RE, bad_user), repr(bad_user))

print()
print("dispatch：校验在进入函数之前就完成，非法参数不应触达真实的 apt/useradd")

# 这两条用 dry_run + 非法参数，确认在 dispatch 层就被拒、不会去碰系统
r = act.dispatch("pkg.install", {"packages": "nginx; rm -rf /"}, "dry_run")
check("dispatch 挡住注入的包名", r["ok"] is False and r["changed"] is False,
      str(r.get("detail")))

r = act.dispatch("pkg.install", {}, "dry_run")
check("dispatch 挡住缺必填参数", r["ok"] is False, str(r.get("detail")))

r = act.dispatch("user.add", {"name": "Bad Name"}, "dry_run")
check("dispatch 挡住非法用户名", r["ok"] is False, str(r.get("detail")))

r = act.dispatch("user.add", {"name": "okuser", "shell": "/bin/evil"}, "dry_run")
check("dispatch 挡住不在列表里的 shell", r["ok"] is False, str(r.get("detail")))

r = act.dispatch("no.such.action", {}, "dry_run")
check("dispatch 挡住未登记的 action", r["ok"] is False, str(r.get("detail")))

print()
print("dispatcher 必须强制覆盖 reversible —— 这是实测抓出来的静默漏洞")

# 这个 bug 的危害值得写清：ok() 的默认参数是 reversible=True，
# 而 dispatch 原来用 setdefault 填 reversible。两者一叠加，**任何**返回
# ok(...) 却没显式传 reversible 的 handler，dry_run 都会被报成"可逆"。
#
# 症状（2026-10-11 装机验收实测）：user.add 登记为 reversible=False，
# `actions` 列表也正确显示"不可逆"，但它的 dry_run 输出是"可逆：是" ——
# 于是 cmd_act 里那道两级授权拦截根本不触发，一个不可逆的动作
# 可以被一句 --yes 带过。
#
# 这里三处都要断言，因为它们各自都会被"只改一处"的修法绕过：
#   1) 登记值      —— @register 说了什么
#   2) dry_run 返回值 —— 人第一次看到的东西
#   3) apply  返回值 —— 落地时依据的东西

dr = act.dispatch("user.add", {"name": "aos-probe"}, "dry_run")
check("user.add dry_run 里 reversible 必须是 False（不是 ok() 的默认 True）",
      dr.get("reversible") is False, "dry_run.reversible = %r" % dr.get("reversible"))

# 上面那条在非 Linux 上走的是"pwd 模块不存在"的异常分支 —— 能过，
# 但没测到 user.add 真正的正常路径。把平台相关的查表函数换掉，
# 就能在任何系统上走一遍它该走的路。**这正是把 _passwd_lookup 抽出来的理由。**
print()
print("user.add 的正常路径（把平台查表替换掉，任何系统都能测）")
_saved_lookup = act._passwd_lookup
try:
    act._passwd_lookup = lambda n: None          # 假装这个用户还不存在
    r = act.dispatch("user.add", {"name": "aos-probe", "shell": "/bin/sh"},
                     "dry_run")
    check("新用户 dry_run 成功且报'将新建'",
          r.get("ok") is True and "将新建" in r.get("detail", ""),
          str(r.get("detail")))
    check("该 dry_run 的 reversible 仍是 False（不被 ok() 默认值污染）",
          r.get("reversible") is False, "got %r" % r.get("reversible"))
    check("dry_run 的 already_exists 是 False",
          r.get("evidence", {}).get("already_exists") is False,
          str(r.get("evidence")))

    act._passwd_lookup = lambda n: {"uid": 1001, "home": "/home/" + n,
                                    "shell": "/bin/sh"}
    r2 = act.dispatch("user.add", {"name": "aos-probe", "shell": "/bin/sh"},
                      "dry_run")
    check("已存在的用户 dry_run 报 changed=False 且说'已经存在'",
          r2.get("ok") is True and r2.get("changed") is False
          and "已经存在" in r2.get("detail", ""), str(r2.get("detail")))

    act._passwd_lookup = lambda n: {"uid": 1001, "home": "/home/" + n,
                                    "shell": "/bin/bash"}
    r3 = act.dispatch("user.add", {"name": "aos-probe", "shell": "/bin/sh"},
                      "verify")
    check("verify 发现 shell 与目标不符时判失败",
          r3.get("ok") is False and "shell" in r3.get("detail", ""),
          str(r3.get("detail")))
finally:
    act._passwd_lookup = _saved_lookup

dr_ok = act.dispatch("pkg.list", {}, "dry_run")
check("pkg.list（只读可逆）的 dry_run 仍报 True",
      dr_ok.get("reversible") is True, "got %r" % dr_ok.get("reversible"))

# 反过来钉一条：如果某个 handler 在返回值里**撒谎**说自己是可逆的，
# dispatcher 也必须把它纠正回来 —— 可逆性只认 @register 那一处。
print()
print("可逆性只认一处声明：handler 自己撒谎也纠正回来")
_orig = act.ACTIONS["user.add"]
_saved = _orig


def _lying_handler(params, phase):
    return act.ok("我在撒谎，声称自己可逆", reversible=True)


_lying_handler.action_name = "user.add"
_lying_handler.reversible = False          # 登记说是不可逆
_lying_handler.schema = ("name",)
act.ACTIONS["user.add"] = _lying_handler
try:
    r = act.dispatch("user.add", {"name": "x"}, "dry_run")
    check("handler 声称 reversible=True，dispatcher 仍按登记的 False 纠正",
          r.get("reversible") is False, "got %r" % r.get("reversible"))
finally:
    act.ACTIONS["user.add"] = _saved

# catalogue 必须把这些新原语也报出去 —— 否则 LLM 永远规划不到它们
names = {c["name"] for c in act.catalogue()}
for want in ("pkg.install", "pkg.remove", "pkg.list", "user.add",
             "sys.read", "service.status"):
    check("catalogue 里有 %s" % want, want in names)


# --------------------------------------------------------------------------
# M9 感知层：sys.read / service.status
#
# 只读原语最容易出的两个错，都不是"读错了值"，而是：
#   ① 把指标名当路径用 —— 那就等于开了任意文件读取漏洞；
#   ② 只读却被算成"改了世界" —— 那会把世界版本号这个账本搅浑。
# 下面两条分别钉住它们。值本身不检查（每台机器都不一样），
# 检查的是**这条路的形状**。

print()
print("sys.read：白名单是闭集，指标名不是路径")

_r = act.dispatch("sys.read", {"metric": "/etc/shadow"}, "dry_run")
check("把路径当指标名传 -> 拒绝", _r.get("ok") is False, "got %r" % _r.get("ok"))
check("拒绝时把合法指标名全列出来（否则模型只能瞎猜）",
      "os.release" in (_r.get("detail") or ""), "detail=%r" % _r.get("detail"))
_r = act.dispatch("sys.read", {"metric": "..%s.." % os.sep + "etc/passwd"}, "dry_run")
check("带路径穿越的指标名 -> 拒绝", _r.get("ok") is False)
_r = act.dispatch("sys.read", {}, "dry_run")
check("缺 metric -> 拒绝", _r.get("ok") is False)
_r = act.dispatch("sys.read", {"metric": ""}, "dry_run")
check("空 metric -> 拒绝", _r.get("ok") is False)
_r = act.dispatch("sys.read", {"metric": "OS.RELEASE"}, "dry_run")
check("指标名大小写敏感（不做猜测性归一化）", _r.get("ok") is False)

print()
print("sys.read：只读的动作不许推进世界版本号")

for _m in sorted(act._READ_PROBES):
    _r = act.dispatch("sys.read", {"metric": _m}, "dry_run")
    check("指标 %s 的 dry_run 是 changed=False" % _m, _r.get("changed") is False,
          "got %r" % _r.get("changed"))

print()
print("sys.read 的正常路径（把读文件替换掉，任何系统都能测）")

_orig_read = act._read
_orig_probe_cpu = act._probe_cpu_count
_orig_probe_disk = act._probe_disk_usage
try:
    act._read = lambda p: "PRETTY_NAME=\"AutonomicOS 9.9 (probe)\"\nID=autonomic\n"
    _r = act.dispatch("sys.read", {"metric": "os.release"}, "dry_run")
    check("os.release 正常读到并压成一行 PRETTY_NAME",
          _r.get("ok") is True and _r["evidence"]["value"] == "AutonomicOS 9.9 (probe)",
          "got %r" % _r.get("evidence"))

    act._read = lambda p: None
    _r = act.dispatch("sys.read", {"metric": "kernel"}, "dry_run")
    check("读不到时是 fail 而不是 ok（不能把'读失败'伪装成成功）",
          _r.get("ok") is False, "got %r" % _r.get("ok"))
    check("读不到的措辞同时提到'没有这个文件'和'被沙箱挡住'两种情况",
          "没有" in (_r.get("detail") or "") and "沙箱" in (_r.get("detail") or ""),
          "detail=%r" % _r.get("detail"))

    act._read = _orig_read
    act._probe_cpu_count = lambda: "online_cpus=8"
    _r = act.dispatch("sys.read", {"metric": "cpuinfo_count"}, "dry_run")
    check("cpuinfo_count 走探针函数而不是直接调 os",
          _r.get("ok") is True and "8" in _r["evidence"]["value"],
          "got %r" % _r.get("evidence"))

    act._probe_disk_usage = lambda: "total=1 used=1 free=0 used_pct=100.0%"
    _r = act.dispatch("sys.read", {"metric": "disk_usage"}, "dry_run")
    check("disk_usage 走探针函数（没有 statvfs 的平台也能测）",
          _r.get("ok") is True and "total=1" in _r["evidence"]["value"],
          "got %r" % _r.get("evidence"))
finally:
    act._read = _orig_read
    act._probe_cpu_count = _orig_probe_cpu
    act._probe_disk_usage = _orig_probe_disk

print()
print("statvfs 不存在的平台：说的是'指标不可得'，不是'权限不足'")

_saved_statvfs = getattr(os, "statvfs", None)
try:
    if _saved_statvfs is not None:
        del os.statvfs
    _v = act._probe_disk_usage()
    check("没有 statvfs 时有明确措辞（不把它说成读取失败）",
          "statvfs" in _v and "不可得" in _v, "got %r" % _v)
finally:
    if _saved_statvfs is not None:
        os.statvfs = _saved_statvfs

print()
print("service.status：只读，且 unit 名要挡住参数注入")

for _bad in ("--help", "-x", "../../etc/passwd", "a/b"):
    _r = act.dispatch("service.status", {"unit": _bad}, "dry_run")
    check("unit=%r -> 拒绝" % _bad, _r.get("ok") is False,
          "got %r" % _r.get("ok"))
_r = act.dispatch("service.status", {}, "dry_run")
check("缺 unit -> 拒绝", _r.get("ok") is False)

_orig_run = act._run
try:
    act._run = lambda argv: (0, {"is-active": "active", "is-enabled": "enabled",
                                 "show": "running"}.get(argv[1], "unknown"))
    _r = act.dispatch("service.status", {"unit": "sshd"}, "dry_run")
    check("正常 unit 走通且 changed=False",
          _r.get("ok") is True and _r.get("changed") is False,
          "got ok=%r changed=%r" % (_r.get("ok"), _r.get("changed")))
    check("service.status 的 dry_run 回报'可逆'但语义上什么都没改",
          _r.get("reversible") is True)
finally:
    act._run = _orig_run

print()
if FAILED:
    print("%d 项失败：%s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("全部通过。")
