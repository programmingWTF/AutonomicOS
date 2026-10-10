#!/usr/bin/python3
"""test-plan -- 给 autonomicplan 的纯逻辑部分做的自检。

为什么值得单独写一个测试文件：这些函数是整个 AI 通道的**入口过滤器**，
它们一旦判断错，后面所有的 dry_run / apply / verify 都是在错误的前提上工作。
而它们恰好是纯函数 —— 不碰磁盘、不碰网络 —— 所以能被真实验证，不该靠"看起来对"。

跑法（项目根目录）：
    python3 scripts/test-plan.py
退出码 0 = 全通过。CI 里也跑这一个。
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "packaging", "autonomic-agent", "src"))

import autonomicplan as plan  # noqa: E402

# actd 真实返回的清单形状
CATALOGUE = [
    {"name": "file.write", "reversible": True,
     "params": ["path", "content", "mode?"]},
    {"name": "file.restore", "reversible": True, "params": ["backup"]},
    {"name": "service.set", "reversible": True, "params": ["unit", "state"]},
    {"name": "self.test", "reversible": True, "params": []},
]
META = {
    "write_allowlist": ["/etc/autonomic/", "/etc/sysctl.d/", "/run/autonomic/"],
    "write_denylist": ["/etc/shadow", "/etc/passwd", "/etc/sudoers"],
}

FAILED = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, extra))
        FAILED.append(name)


def steps_ok(obj) -> list:
    return plan.validate(obj, CATALOGUE, tuple(META["write_allowlist"]),
                         tuple(META["write_denylist"]))[0]


def errors_of(obj) -> list:
    return plan.validate(obj, CATALOGUE, tuple(META["write_allowlist"]),
                         tuple(META["write_denylist"]))[1]


print("extract_json：把 JSON 从模型的废话里抠出来")

wrapped = '好的，这是计划：\n```json\n{"intent":"x","steps":[]}\n```\n需要改动再说。'
obj, why = plan.extract_json(wrapped)
check("markdown 代码块里的 JSON 能取出来", isinstance(obj, dict) and why == "", why)

# content 值里带反括号 —— 天真版读者会在这里把对象提前截断
tricky = '{"intent":"i","steps":[{"action":"file.write","params":{"path":"/etc/autonomic/a.conf",' \
         '"content":"[sec]}\\n # not a brace: { \\"k\\":\\"v\\" }"},"reason":"r"}]}'
obj, why = plan.extract_json(tricky)
check("字符串内部的 { } 不会破坏括号配平",
      obj is not None and len(obj.get("steps", [])) == 1, why)
if obj:
    got = obj["steps"][0]["params"]["content"]
    # 内容里混进的 } 和 " 都没能把对象提前截断，且原样保留
    check("带转义的内容逐字节保真",
          "\n" in got and '"k":"v"' in got and got.endswith("}")
          and len(got) > 30, repr(got))

check("纯散文（无 JSON）被判失败", plan.extract_json("我做不到这件事")[0] is None)
check("空输入不会抛异常", plan.extract_json("")[0] is None)
check("截断的 JSON 被判失败，不是被猜出来",
      plan.extract_json('{"intent":"x","steps":[')[0] is None)
check("数组不是我们接受的对象形状",
      plan.extract_json('[1,2,3]')[0] is None)

print()
print("validate：不合格的计划必须整份退回")

good = {"intent": "写个配置", "steps": [
    {"action": "file.write",
     "params": {"path": "/etc/autonomic/x.conf", "content": "a=1\n"},
     "reason": "用户要求开启该开关"}]}
res, err = plan.validate(good, CATALOGUE, tuple(META["write_allowlist"]),
                         tuple(META["write_denylist"]))
check("合法计划通过", len(res) == 1 and not err, str(err))

check("未登记的 action 被拒",
      any("未登记的 action" in e for e in errors_of(
          {"intent": "i", "steps": [{"action": "rm_rf", "params": {},
                                     "reason": "r"}]})))
check("缺 reason 的动作被拒",
      any("reason" in e for e in errors_of(
          {"intent": "i", "steps": [{"action": "self.test", "params": {}}]})))
check("空白 reason 不等于有 reason",
      any("reason" in e for e in errors_of(
          {"intent": "i", "steps": [{"action": "self.test", "params": {},
                                     "reason": "   "}]})))
check("未知参数被拒",
      any("未知参数" in e for e in errors_of(
          {"intent": "i", "steps": [{"action": "self.test",
                                     "params": {"force": "1"}, "reason": "r"}]})))
check("缺必填参数被拒",
      any("缺必填参数" in e for e in errors_of(
          {"intent": "i", "steps": [{"action": "file.write",
                                     "params": {"path": "/etc/autonomic/a"},
                                     "reason": "r"}]})))
check("写到许可前缀之外被拒",
      any("可写前缀" in e for e in errors_of(
          {"intent": "i", "steps": [{"action": "file.write",
                                     "params": {"path": "/etc/cron.d/evil",
                                                "content": "x"},
                                     "reason": "r"}]})))
check("写到 /etc/shadow 被拒",
      any("保护名单" in e for e in errors_of(
          {"intent": "i", "steps": [{"action": "file.write",
                                     "params": {"path": "/etc/shadow",
                                                "content": "x"},
                                     "reason": "r"}]})))
check("超过步数上限被拒",
      any("超过上限" in e for e in errors_of(
          {"intent": "i", "steps": [
              {"action": "self.test", "params": {}, "reason": "r"}
              for _ in range(plan.MAX_STEPS + 1)]})))
check("非八进制 mode 被拒",
      any("八进制" in e for e in errors_of(
          {"intent": "i", "steps": [{"action": "file.write",
                                     "params": {"path": "/etc/autonomic/a",
                                                "content": "x", "mode": "9999"},
                                     "reason": "r"}]})))

# 有错的时候绝不能返回一个"看起来能用"的部分计划
bad_plan = {"intent": "i", "steps": [
    {"action": "file.write", "params": {"path": "/etc/autonomic/ok",
                                        "content": "x"}, "reason": "r"},
    {"action": "self.test", "params": {}}]}
partial, perr = plan.validate(bad_plan, CATALOGUE,
                              tuple(META["write_allowlist"]),
                              tuple(META["write_denylist"]))
check("部分合格的计划整体退回，不留半成品", partial == [] and perr != [])

empty_plan = {"intent": "做不到", "note": "缺少能力", "steps": []}
res, err = plan.validate(empty_plan, CATALOGUE, tuple(META["write_allowlist"]),
                         tuple(META["write_denylist"]))
check("空 steps 是模型的合法回答，不算错误", res == [] and err == [])

check("steps 不是数组被拒", any("steps" in e for e in errors_of({"steps": "x"})))

print()
print("准备给模型的提示词里必须说清楚边界")

prompt = plan.build_system_prompt(CATALOGUE, META)
check("提示词列出了所有 action", all(a["name"] in prompt for a in CATALOGUE))
check("提示词列出了可写前缀", all(p in prompt for p in META["write_allowlist"]))
check("提示词列出了禁写路径", all(p in prompt for p in META["write_denylist"]))
check("提示词强调了没有 shell", "没有 shell" in prompt or "没有 shell" in prompt)

print()
if FAILED:
    print("%d 项失败：%s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("全部通过。")
