#!/usr/bin/python3
"""test-backup-naming -- 验证备份名的唯一性与 sidecar 的可还原性。

两个具体主张，各自都能被真验一遍：

  1. **同一秒内连写两次，必须产生两个不同的备份。**
     原来的实现用 time.strftime("%Y%m%dT%H%M%S%f") 想要微秒，但 strftime
     根本没有 %f 这个指令（那是 datetime 的），它会把 %f 原样吐出来，
     于是备份名里出现字面的 "%f"，同一秒内两次备份还是撞在一起 ——
     也就是把"上一版"弄丢，而回滚的全部意义就是回到上一版。

  2. **sidecar 记下的原位置必须能还回去。**
     备份名把路径里的 "/" 编码成 "_"，而 Linux 路径本身允许 "_"，
     所以光看名字是分不清 `etc_autonomic_my_conf` 到底是
     `/etc/autonomic/my_conf` 还是 `/etc/autonomic_my/conf`。
     机器要还原就必须读 sidecar，不能猜。

在真 Linux 上跑（默认值改到临时沙箱，不碰真系统）：
    python3 scripts/test-backup-naming.py [源码目录]

Windows 上也能跑，而且**测的是同一套规则**。关键点是：`_backup` /
`file.restore` / `_check_write_path` 内部刻意用 `posixpath` —— 它们判断的是
**目标 Linux 系统**里的路径语义，不该跟着"这段代码跑在什么 OS 上"变。
所以要跨过 Windows 那道坎，正确做法不是改被测代码，而是给它一个
**Linux 形状的沙箱**：把 BACKUP_DIR / 可写前缀指到临时目录，路径仍按
`/` 书写，只有真正落盘的那一下由本机路径拼出来。

这么安排还顺带钉住了一条边界：**可写前缀的比较用的是 posixpath.normpath**，
所以 `C:/...` 这种形态会被明确拒绝（"路径必须是绝对路径"）—— 那是对的，
通道里出现的路径本来就该是 Linux 路径。
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # sandboxpath：跨平台的沙箱路径映射
import sandboxpath  # noqa: E402

SRC = sys.argv[1] if len(sys.argv) > 1 else "/usr/share/autonomic"
sys.path.insert(0, SRC)
import autonomicact as act  # noqa: E402

FAILED = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print("  %s %s %s" % ("ok  " if cond else "FAIL", name, extra))
    if not cond:
        FAILED.append(name)


# ---- 沙箱 ----
# 这个测试跨两种"路径形状"，分工必须写清楚，否则每次改动都会踩一次：
#
#   * **Linux 形态**（`/etc/autonomic/demo.conf`）
#     传给 dispatch / _check_write_path / 记进 sidecar 的。被测代码判断
#     路径语义用的是 posixpath —— 它描述的是目标 Linux 系统，不该跟着
#     "这段代码跑在什么 OS 上"变。这是**设计**，不是遗漏。
#
#   * **本机形态**（Windows 上是 `C:\...\etc\autonomic\demo.conf`）
#     真正碰磁盘的地方：open() / makedirs / shutil.copy2 / os.stat。
#     在 Linux 上两者逐字节相同，所以这个区分只在 Windows 上有意义。
#
# 桥就是 sandboxpath.native()。**混用一次的后果不是报错难看**：要么一个
# 合法操作被判成越界（安全边界误伤），要么沙箱被指到别处（测试动了真文件）。
# ---- 沙箱 ----
# 这个测试跨两种"路径形状"。分工必须写清楚，否则每次改动都会踩一次：
#
#   * **语义形状**：WRITE_ALLOWLIST / WRITE_DENYLIST 里的前缀，以及喂给
#     dispatch 的路径。真机上它们是 `/etc/autonomic/` 这种 Linux 形态，
#     和 `_check_write_path` 里 posixpath 的判断配套。
#
#   * **磁盘形状**：真正要 open() / copy2() / makedirs() 的路径。
#     在 Linux 上两者逐字节相同；在 Windows 上前者不是合法文件名。
#
# 桥是 sandboxpath.native()。**混用一次的后果不是报错难看**：要么一个合法
# 操作被判成越界（安全边界误伤），要么沙箱被指到别处（测试动了真文件）。
#
# 所以这里把沙箱指到临时目录，并让**前缀表和路径用同一种形状**（都是本机
# 形态）—— 被测代码的判断逻辑一个字节没改，改的只是它比对的参照物。
WORK_NATIVE = sandboxpath.native("/etc/autonomic")
BK_NATIVE = sandboxpath.native("/var/lib/autonomic/backups")
os.makedirs(WORK_NATIVE, exist_ok=True)
os.makedirs(BK_NATIVE, exist_ok=True)
act.BACKUP_DIR = BK_NATIVE
act.WRITE_ALLOWLIST = (WORK_NATIVE + os.sep,)
act.WRITE_DENYLIST = ()

target_native = os.path.join(WORK_NATIVE, "demo.conf")
with open(target_native, "w") as fh:
    fh.write("v1\n")

print("1. 同一秒内连写两次")
first = act._backup(target_native)
with open(target_native, "w") as fh:
    fh.write("v2\n")
second = act._backup(target_native)

print("   first : %s" % os.path.basename(first))
print("   second: %s" % os.path.basename(second))
check("两次备份不是同一个文件", bool(first) and first != second)
check("备份名里没有字面的 %f",
      "%f" not in os.path.basename(first)
      and "%f" not in os.path.basename(second))
check("两份备份内容各是各的",
      bool(first) and bool(second)
      and open(first).read() == "v1\n" and open(second).read() == "v2\n",
      "first=%r second=%r" % (first, second))

print()
print("2. sidecar 记录的原位置")
meta_first = first + ".json"
check("sidecar 存在", os.path.exists(meta_first))
import json  # noqa: E402

meta = json.load(open(meta_first))
check("sidecar 里的 path 就是原文件", meta.get("path") == target_native,
      repr(meta.get("path")))
check("sidecar 里有 sha256", bool(meta.get("sha256")))

print()
print("3. 用备份还原回去（真读文件比对）")
with open(target_native, "w") as fh:
    fh.write("v3-changed\n")
res = act.dispatch("file.restore", {"backup": first}, "apply")
check("restore apply 成功", res.get("ok"), res.get("detail", ""))
restored = open(target_native).read()
check("内容真的回到 v1", restored == "v1\n", repr(restored))

print()
print("4. 没有 sidecar 时必须拒绝，而不是猜")
orphan = os.path.join(BK_NATIVE, "99999999T999999999999_orphan.conf")
with open(orphan, "w") as fh:
    fh.write("whatever\n")
res = act.dispatch("file.restore", {"backup": orphan}, "apply")
check("无 sidecar 被拒", not res.get("ok"), res.get("detail", "")[:60])

print()
print("5. 越界的备份路径必须被拒（两端都要卡死）")
res = act.dispatch("file.restore",
                   {"backup": sandboxpath.native("/etc/shadow")}, "dry_run")
check("备份不在备份目录里 -> 拒绝",
      not res.get("ok") and "备份" in res.get("detail", ""),
      res.get("detail", "")[:80])

print()
if FAILED:
    print("%d 项失败：%s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("全部通过。")
