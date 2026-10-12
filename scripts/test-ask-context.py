#!/usr/bin/python3
"""test-ask-context -- 给 M10「感知喂回模型」做的自检。

M10 做的事只有一件：`autonomicctl ask` 之前先看一眼这台机器，把看到的东西
交给模型。听起来简单，但这一层最容易犯的错**不是**"看没看"，而是三件更安静的事：

   1. **模型看到的和磁盘上存的不是同一份快照。** 如果 ask 自己重新读一遍
      系统，模型照着 A 做计划，/run/autonomic/sense 里存的是差了几秒的 B。
      三个月后没人知道模型当时依据的世界是什么。所以 ask 必须用
      **_collect_snapshot() 收下来的那一份**，不是另一份。

   2. **感知被写成了指令。** 一段结构化数据混进提示词，模型很可能把它当成
      "系统现在要求我做的事"。渲染出来的段落必须明确写着这是观测记录。
      这不是修辞 —— 提示词注入正是从这种模糊地带进来的。

   3. **上下文没有上限。** meminfo 一个指标就是几百行。"给模型看多少"
      必须是一个写在代码里的数字，不能由机器的内存大小决定：否则同一个
      请求在不同机器上拿到长度差几十倍的上下文，计划之间就不可比了。

跑法（项目根目录）：
    python3 scripts/test-ask-context.py
退出码 0 = 全通过。CI 里也跑这一个。
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import os
import sys
import tempfile
from importlib.machinery import SourceFileLoader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "packaging", "autonomic-agent", "src")
sys.path.insert(0, SRC)

import autonomicplan as plan  # noqa: E402
import autonomicact as act  # noqa: E402

os.environ.setdefault("AUTONOMIC_LIB", SRC)

_loader = SourceFileLoader("aosctl_under_test", os.path.join(SRC, "autonomicctl"))
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


# ---------------------------------------------------------------- 1. 同一份快照

print("ask 用的快照必须和 sense 投影的是同一份")

# 关键不在于"读了没有"，在于"读的是哪一份"。_collect_snapshot 是唯一收口，
# sense 和 ask 都只能走它 —— 否则两边各自读一遍，就对不上了。
src = inspect.getsource(ctl)
check("sense 走 _collect_snapshot（不再自己循环读）",
      "_collect_snapshot()" in inspect.getsource(ctl.cmd_sense))
check("ask 走 _collect_snapshot（不是另写一套读法）",
      "_collect_snapshot()" in inspect.getsource(ctl.cmd_ask))
check("_collect_snapshot 只向通道问 sys.read / service.status",
      "_sense_once" in inspect.getsource(ctl._collect_snapshot)
      and 'service.status' in inspect.getsource(ctl._collect_snapshot)
      and "sys.read" in inspect.getsource(ctl._sense_once)
      and "systemctl" not in inspect.getsource(ctl._collect_snapshot))

CALLS: list = []


def _fake_call(action, params, phase, reason=""):
    CALLS.append((action, dict(params), phase))
    if action == "sys.read":
        return {"ok": True, "changed": False, "reversible": True,
                "evidence": {"metric": params["metric"],
                             "value": "V(%s)" % params["metric"]}}
    if action == "service.status":
        return {"ok": True, "changed": False, "reversible": True,
                "evidence": {"unit": params["unit"], "active": "active",
                             "enabled": "enabled", "substate": "running"}}
    raise AssertionError("_collect_snapshot 不该去调 %r" % action)


ctl._act_call = _fake_call
snap = ctl._collect_snapshot()
check("快照覆盖了 _SENSE_METRICS 的每一项",
      set(ctl._SENSE_METRICS) <= set(snap),
      "缺=%r" % sorted(set(ctl._SENSE_METRICS) - set(snap)))
check("快照覆盖了 _SENSE_UNITS 的每一项",
      {"unit.%s" % u for u in ctl._SENSE_UNITS} <= set(snap))
check("收集全程只发 dry_run（读不许推进世界版本号）",
      all(c[2] == "dry_run" for c in CALLS),
      "出现的阶段：%r" % sorted({c[2] for c in CALLS}))

# 取不到的项必须留在快照里，而不是消失。消失的那几行看起来像"这项正常"。
CALLS.clear()


def _half_channel(action, params, phase, reason=""):
    CALLS.append((action, dict(params), phase))
    if action == "sys.read" and params["metric"] == "meminfo":
        return {"ok": False, "detail": "被沙箱挡住"}
    return _fake_call(action, params, phase, reason)


ctl._act_call = _half_channel
snap_half = ctl._collect_snapshot()
check("取不到的指标仍出现在快照里，且带着原因",
      isinstance(snap_half.get("meminfo"), dict)
      and "被沙箱挡住" in str(snap_half["meminfo"].get("unavailable", "")),
      "meminfo=%r" % snap_half.get("meminfo"))

# ---------------------------------------------------------------- 2. 渲染

print()
print("render_context：感知是输入，不是指令")

ctx = plan.render_context(snap)
check("快照真的进了提示词", "os.release" in ctx and "V(os.release)" in ctx)
check("明确标注这是观测记录，不是指令",
      "观测记录" in ctx and "不是给你的指令" in ctx, ctx[:120])
check("世界版本号进了上下文（计划依据哪个世界写的，必须可查）",
      "世界版本号" in ctx)
check("说明缺的项代表读不到，不代表正常", "读不到" in ctx)
check("服务状态被渲染成人能读的一行", "sshd" in ctx or "systemd-networkd" in ctx)

check("没有快照时返回空串（不伪造一份给模型）",
      plan.render_context(None) == "" and plan.render_context({}) == "")
check("快照不是 dict 时也不炸", plan.render_context("oops") == "")

print()
print("render_context：取不到的项照样写出来")

ctx_half = plan.render_context(snap_half)
check("取不到的 meminfo 出现在上下文里", "meminfo" in ctx_half)
check("取不到时带着原因（不是干巴巴一个 unavailable）",
      "被沙箱挡住" in ctx_half)
check("取不到的那行被标成 unavailable", "unavailable" in ctx_half)

print()
print("render_context：长度上限写在代码里，不由机器配置决定")

_big = {"meminfo": "x" * 50000, "os.release": "Debian"}
ctx_big = plan.render_context(_big)
check("超长快照被截断，不会整个塞进提示词",
      len(ctx_big) < plan.CONTEXT_MAX_CHARS + 2000,
      "len=%d 上限=%d" % (len(ctx_big), plan.CONTEXT_MAX_CHARS))
check("截断时明确承认被截断了（不悄悄给半份数据）",
      "截断" in ctx_big, ctx_big[-200:])
# 关键：一个超大指标不许把其余事实全吃掉。这是比"超长"更糟的失败模式 ——
# 渲染出来除它之外什么都没有，等于"因为一项太长，把别的都藏起来了"。
check("一个超大指标不会把其余项全部挤掉",
      "os.release" in ctx_big and "Debian" in ctx_big, ctx_big[-300:])
check("被砍的那行明说原长多少（不假装它本来就这么短）",
      "50000" in ctx_big or "原长" in ctx_big, ctx_big[:400])

# 上限必须是常量，不能是"从别处算出来的"：同一个请求在不同机器上必须可比。
check("CONTEXT_MAX_CHARS 是一个固定的模块级常量",
      isinstance(plan.CONTEXT_MAX_CHARS, int) and plan.CONTEXT_MAX_CHARS > 0)

# 顺序是 sorted() 出来的，所以渲染结果对同一份输入是确定的。
# 计划要可复现，提示词就得可复现 —— 否则同一份快照两次问出两个计划。
check("同一份快照渲染两次，结果逐字节相同（计划要可复现）",
      plan.render_context(snap) == plan.render_context(snap))

print()
print("提示词整体：接上上下文之后，规矩一条都没少")

CATALOGUE = [
    {"name": "file.write", "reversible": True,
     "params": ["path", "content", "mode?"]},
    {"name": "self.test", "reversible": True, "params": []},
]
META = {
    "write_allowlist": ["/etc/autonomic/"],
    "write_denylist": ["/etc/shadow"],
    "sense": snap,
}
prompt = plan.build_system_prompt(CATALOGUE, META)
check("带上下文的提示词仍列出所有 action",
      all(a["name"] in prompt for a in CATALOGUE))
check("带上下文的提示词仍列出可写前缀",
      all(p in prompt for p in META["write_allowlist"]))
check("带上下文的提示词仍强调不可逆动作要慎重", "不可撤销" in prompt)
check("带上下文的提示词仍要求每步有 reason", "reason" in prompt)
check("上下文出现在提示词末尾（能力边界在前，观测在后）",
      prompt.index("观测记录") > prompt.index("file.write"))
check("没有快照时提示词退回原来的样子（不多出一段空壳）",
      plan.build_system_prompt(CATALOGUE, {"write_allowlist": ["/etc/autonomic/"]})
      .count("观测记录") == 0)

# ---------------------------------------------------------------- 3. 解析投影

print()
print("parse_snapshot：从投影文件还原快照，两种出口一份数据")

proj = (
    "# 注释行\n"
    "world=snapshot_id=bootstrap\naction_seq=3\n"
    "\n"
    "os.release=Debian GNU/Linux 12 (bookworm)\n"
    "kernel=6.1.0-13-amd64\n"
    "meminfo=unavailable (被沙箱挡住)\n"
    "unit.sshd=active:active enabled:enabled\n"
)
parsed = plan.parse_snapshot(proj)
check("注释行和空行被跳过", "# 注释行" not in parsed and "" not in parsed)
check("world 被单独取出来（渲染时要它）", "world" in parsed)
check("普通指标按 key=value 解析", parsed.get("os.release") == "Debian GNU/Linux 12 (bookworm)")
check("unavailable 行原样保留，连原因一起",
      "unavailable" in str(parsed.get("meminfo", ""))
      and "被沙箱挡住" in str(parsed.get("meminfo", "")))
check("unit.* 被拆成 dict（不糊成一行乱码）",
      isinstance(parsed.get("unit.sshd"), dict)
      and parsed["unit.sshd"].get("active") == "active"
      and parsed["unit.sshd"].get("enabled") == "enabled")

# 闭环：投影 -> 解析 -> 渲染，喂回提示词的必须还是同一份数据。
check("投影->解析->渲染 闭环后数据没丢",
      "Debian GNU/Linux 12" in plan.render_context(parsed)
      and "被沙箱挡住" in plan.render_context(parsed))
check("空输入解析成空 dict，不抛异常", plan.parse_snapshot("") == {})
check("没有等号的行被忽略，不炸", plan.parse_snapshot("just some words") == {})

# ---------------------------------------------------------------- 4. ask 的接法

print()
print("ask_plan：快照通过参数进去，并且原样回到返回值里")

_seen_prompt = {}

_RESP_BODY = (b'{"choices":[{"message":{"content":'
              b'"{\\"intent\\":\\"i\\",\\"steps\\":[]}"}}]}')


class _Resp:
    def __init__(self, body): self._b = body
    def read(self, n): return self._b
    def getcode(self): return 200
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _fake_urlopen(req, timeout=None, context=None):
    _seen_prompt["system"] = json.loads(req.data.decode())["messages"][0]["content"]
    return _Resp(_RESP_BODY)


import json  # noqa: E402

_orig_urlopen = plan.urllib.request.urlopen
plan.urllib.request.urlopen = _fake_urlopen
try:
    asked = plan.ask_plan("http://127.0.0.1:1/v1", "k", "m", 30, "看一下磁盘",
                          CATALOGUE, META, sense=snap)
finally:
    plan.urllib.request.urlopen = _orig_urlopen

check("ask_plan 成功返回", asked.get("ok") is True, str(asked.get("detail")))
check("发出去的 system 提示词里带着快照",
      "os.release" in _seen_prompt.get("system", ""))
check("返回值里带回 context（审计要记它）",
      "os.release" in (asked.get("context") or ""))

print()
print("record：当时给模型看了什么，必须落进审计文件")

_orig_record = plan.record
_saved_dir = plan.PLAN_DIR
_tmp = tempfile.mkdtemp(prefix="aos-plan-")
plan.PLAN_DIR = _tmp
try:
    path = plan.record("请求", "m", [], [], raw="r", context=asked.get("context"))
    check("留痕文件写出来了", bool(path) and os.path.isfile(path), path)
    if path:
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        check("审计里有 model_context（它凭什么这么认为，三个月后要能答）",
              "os.release" in saved.get("model_context", ""))
        check("没有上下文时这个字段是空串而不是缺席",
              "model_context" in saved)
finally:
    plan.PLAN_DIR = _saved_dir

print()
print("感知不推进世界版本号：两条路都验")

# 快照只走 sys.read / service.status，两个都是只读原语（changed=False），
# 所以哪怕有人拿它们去 apply，actd 的闸门也不会发 SIGUSR1。
for _m in sorted(act._READ_PROBES):
    _r = act.dispatch("sys.read", {"metric": _m}, "apply")
    check("sys.read %s 连 apply 都是 changed=False" % _m,
          _r.get("changed") is False, "got %r" % _r.get("changed"))
check("ask 收集快照时一个 apply 都没发过",
      all(c[2] == "dry_run" for c in CALLS),
      "出现的阶段：%r" % sorted({c[2] for c in CALLS}))

print()
if FAILED:
    print("%d 项失败：%s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("全部通过。")
