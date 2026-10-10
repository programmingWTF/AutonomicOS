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
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

SRC = sys.argv[1] if len(sys.argv) > 1 else "/usr/share/autonomic"
sys.path.insert(0, SRC)
import autonomicact as act  # noqa: E402

FAILED = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print("  %s %s %s" % ("ok  " if cond else "FAIL", name, extra))
    if not cond:
        FAILED.append(name)


sand = tempfile.mkdtemp(prefix="aos-bk-")
work = os.path.join(sand, "etc", "autonomic")
bk = os.path.join(sand, "backups")
os.makedirs(work)
os.makedirs(bk)
act.BACKUP_DIR = bk
act.WRITE_ALLOWLIST = (work + "/",)
act.WRITE_DENYLIST = ()

target = os.path.join(work, "demo.conf")
with open(target, "w") as fh:
    fh.write("v1\n")

print("1. 同一秒内连写两次")
first = act._backup(target)
with open(target, "w") as fh:
    fh.write("v2\n")
second = act._backup(target)

print("   first : %s" % os.path.basename(first))
print("   second: %s" % os.path.basename(second))
check("两次备份不是同一个文件", first != second)
check("备份名里没有字面的 %f",
      "%f" not in os.path.basename(first)
      and "%f" not in os.path.basename(second))
check("两份备份内容各是各的",
      open(first).read() == "v1\n" and open(second).read() == "v2\n",
      "first=%r second=%r" % (open(first).read(), open(second).read()))

print()
print("2. sidecar 记录的原位置")
meta_first = first + ".json"
check("sidecar 存在", os.path.exists(meta_first))
import json  # noqa: E402

meta = json.load(open(meta_first))
check("sidecar 里的 path 就是原文件", meta.get("path") == target,
      repr(meta.get("path")))
check("sidecar 里有 sha256", bool(meta.get("sha256")))

print()
print("3. 用备份还原回去（真读文件比对）")
with open(target, "w") as fh:
    fh.write("v3-changed\n")
res = act.dispatch("file.restore", {"backup": first}, "apply")
check("restore apply 成功", res.get("ok"), res.get("detail", ""))
restored = open(target).read()
check("内容真的回到 v1", restored == "v1\n", repr(restored))

print()
print("4. 没有 sidecar 时必须拒绝，而不是猜")
orphan = os.path.join(bk, "99999999T999999999999_orphan.conf")
with open(orphan, "w") as fh:
    fh.write("whatever\n")
res = act.dispatch("file.restore", {"backup": orphan}, "apply")
check("无 sidecar 被拒", not res.get("ok"), res.get("detail", "")[:60])

shutil.rmtree(sand, ignore_errors=True)

print()
if FAILED:
    print("%d 项失败：%s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("全部通过。")
