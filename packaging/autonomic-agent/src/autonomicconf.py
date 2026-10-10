#!/usr/bin/python3
"""autonomicconf -- 读 Autonomic Agent 的配置和凭据。

配置刻意分成两个文件，这不是洁癖：

  /etc/autonomic/agent.conf        非敏感，0644，随便看
  /etc/autonomic/credentials.env   敏感，0640 root:autonomic

分开之后，配置文件可以进版本管理、可以贴进 issue、可以 diff；
而密钥文件从生成那一刻起就不该离开这台机器、不该进备份、
不该出现在任何一次 「帮我看看这个报错」 的截图里。

凭据也不只靠文件权限保护 —— unit 里用 systemd 的 LoadCredential=
把文件交给内核持有一份副本，进程从 $CREDENTIALS_DIRECTORY 拿到它。
好处：① 非特权用户读不到；② 密钥从未进入进程的环境变量，
所以 journal、/proc/<pid>/environ、ps aux 里都 grep 不到。

依赖只用标准库： urllib 足够发一个 HTTP 请求，没必要为这个拖进 requests。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

CONFIG_FILE = "/etc/autonomic/agent.conf"
CREDENTIALS_FILE = "/etc/autonomic/credentials.env"

# systemd LoadCredential=autonomic-llm:... 注入时，systemd 会把 CREDENTIALS_DIRECTORY
# 指到一个只有本服务能读的目录。跑在 systemd 之外时这个变量是空的，
# 这时退化到直接读文件（前提是调用者有权读）。
CRED_DIR = os.environ.get("CREDENTIALS_DIRECTORY", "")
CRED_NAME = "autonomic-llm"

API_KEY_ENV = "AUTONOMIC_LLM_API_KEY"

DEFAULTS = {
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
    "timeout": "20",
}


# ---------------------------------------------------------------- 读


def load_config(path: str = CONFIG_FILE) -> dict:
    """读 key = value 配置。缺文件、缺字段都用默认值兜住。

    配置文件是给人手改的，所以要容忍注释、空行、多余的空格，
    但不要容忍「半个文件写坏了导致 agent 起不来」——宁可读出默认值。
    """
    conf = dict(DEFAULTS)
    try:
        with open(path, "r") as fh:
            lines = fh.readlines()
    except OSError:
        return conf
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, val = line.partition("=")
        if sep:
            conf[key.strip()] = val.strip()
    return conf


def get_timeout(conf: dict) -> int:
    try:
        return max(1, int(conf.get("timeout", 20)))
    except (TypeError, ValueError):
        return 20


def _extract_key(fh) -> str:
    for line in fh:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, val = line.partition("=")
        if sep and key.strip() == API_KEY_ENV:
            return val.strip().strip("\"'")
    return ""


def load_api_key() -> tuple[str, str]:
    """返回 (key, source)。source 用于 ``autonomicctl llm`` 显示密钥从哪来的。

    没配就返回 ("", "") —— 这是正常状态，不是错误：一台没连 LLM 的
    Autonomic OS 仍然是一台 Autonomic OS，agent 只是还不能思考。
    """
    if CRED_DIR:
        path = os.path.join(CRED_DIR, CRED_NAME)
        try:
            with open(path, "r") as fh:
                key = _extract_key(fh)
        except OSError:
            key = ""
        if key:
            return key, "systemd credential (%s)" % path

    try:
        with open(CREDENTIALS_FILE, "r") as fh:
            key = _extract_key(fh)
    except OSError:
        return "", ""
    return key, (CREDENTIALS_FILE if key else "")


# ---------------------------------------------------------------- 写
# 只有 autonomicctl setup 会走到这一段。


def write_config(base_url: str, model: str, timeout: int,
                 path: str = CONFIG_FILE) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("# Autonomic Agent -- 非敏感配置（密钥不在这里）\n")
        fh.write("base_url = %s\n" % base_url)
        fh.write("model    = %s\n" % model)
        fh.write("timeout  = %s\n" % timeout)
        fh.write("\n# 改完不必重启整机：systemctl restart autonomic-agent\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def write_api_key(key: str, path: str = CREDENTIALS_FILE) -> None:
    """写密钥。权限在建临时文件时就用 O_CREAT 的 mode 定死 0640，
    不给 umask 插手的空隙 —— 中途某个瞬间出现过 0644 就等于泄露了。
    """
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w") as fh:
        fh.write("# 由 autonomicctl setup 写入。不要贴到任何地方。\n")
        fh.write("%s=%s\n" % (API_KEY_ENV, key))
    # os.replace 会带着源文件的 mode 走，所以最终落地的也是 0640
    os.replace(tmp, path)
    try:
        import grp

        os.chown(path, 0, grp.getgrnam("autonomic").gr_gid)
    except (KeyError, OSError, ImportError):
        pass
    os.chmod(path, 0o640)


# ---------------------------------------------------------------- 连通性自检


def probe(base_url: str, api_key: str, timeout: int = 20) -> dict:
    """真的发一个请求过去，看凭据通不通。

    用 GET /models 而不是 chat completion：它不改动任何东西、不花钱、
    也不需要构造 prompt —— 一个纯粹的「这把钥匙插得进去吗」的检查。

    返回 {"ok": bool, "status": int, "detail": str}
    """
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + api_key)
    req.add_header("User-Agent", "autonomic-agent/0.2")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(65536)
            code = resp.getcode()
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "detail": _http_hint(exc.code)}
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "status": 0,
            "detail": "连不上：%s" % (exc.reason,),
        }
    except OSError as exc:
        return {"ok": False, "status": 0, "detail": str(exc)}

    count = _count_models(body)
    return {
        "ok": True,
        "status": code,
        "detail": "GET /models -> %d，列出 %s 个模型" % (code, count)
        if count
        else "GET /models -> %d" % code,
    }


def _http_hint(code: int) -> str:
    return {
        400: "400 -- 请求被拒，检查 Base URL 是否是 API 根路径（通常带 /v1）",
        401: "401 未授权 -- key 不对，或者它不属于这个组织/项目",
        403: "403 禁止 -- key 有效但没权限访问 /models，换个端点或换权限",
        404: "404 -- 路径不对。Base URL 应指向 API 根，常见写法是 https://host/v1",
        429: "429 -- 限流了（速率或配额），稍后再试",
    }.get(code, "HTTP %d" % code)


def _count_models(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return ""
    items = data.get("data") if isinstance(data, dict) else None
    return str(len(items)) if isinstance(items, list) else ""
