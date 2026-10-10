#!/usr/bin/python3
"""mock-llm -- 一个假的 OpenAI-compatible 端点，用来验证整条 AI 链路。

为什么要有它：这条链路最贵的部分不是 LLM，而是"你是不是真的按纪律
（不许部分执行、先全部预演、每步单独核实）做对了"。而这些纪律跟用的
是真 GPT 还是假 GPT 毫无关系 —— 一个返回固定计划的本地服务就能把它们
全部验一遍，而且是**可重复**的：每次返回的字节一模一样。

于是没有 key 的人（包括 CI、包括第一次 clone 这个仓库的人）也能：

    python3 scripts/mock-llm.py &
    autonomicctl setup --base-url http://127.0.0.1:8099/v1 \\
                       --api-key mock --model mock-1 --non-interactive
    autonomicctl ask "把通道自检再做一遍"

它实现两个端点：
    GET  /models            给 autonomicctl llm 的连通性自检用
    POST /chat/completions  返回一份预先写好的计划

用 --mode 切换它返回什么，用来验不同的分支：
    good      一份合法的两步计划（默认）
    empty     空 steps —— 模型说"我做不到"
    illegal   想把文件写到可写前缀之外 —— 必须被校验拦下
    badstep   有未登记的 action 且缺 reason —— 必须整份退回
    notjson   只说散文，不给 JSON —— 必须被判定解析失败
    fence     JSON 裹在 ```json 代码块里 —— 必须仍能解析

退出：Ctrl-C。只监听 127.0.0.1，不对外。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "mock-1"

# 注意内容里的字符串是**确定的**：这里曾经想塞个时间戳进去看起来更真实，
# 但一旦内容随时间变化，apply 写进去的 T1 和 verify 重算的 T2 就永远
# 对不上，通道会把自己判成失败。看起来更真实，实际上只会让它永远是失败。
PLAN_GOOD = {
    "intent": "在 Autonomic 的配置目录里留下一张身份卡，并确认 Action 通道仍然通畅",
    "note": "两步都落在可写前缀内",
    "steps": [
        {
            "action": "file.write",
            "params": {
                "path": "/etc/autonomic/identity-card.conf",
                "content": "# 这张卡是 AI 通过 Action 通道写下来的\n"
                           "owner = autonomic\n"
                           "written_by = planned-action\n",
                "mode": "0644",
            },
            "reason": "给这台机器留一份可核验的来源说明",
        },
        {
            "action": "self.test",
            "params": {},
            "reason": "写完之后要确认通道本身还是通的",
        },
    ],
}

PLAN_EMPTY = {
    "intent": "这件事现有能力做不到",
    "note": "缺少能读取 package manager 的原语",
    "steps": [],
}

PLAN_ILLEGAL = {
    "intent": "改 sshd 的配置",
    "steps": [
        {
            "action": "file.write",
            "params": {
                "path": "/etc/ssh/sshd_config",
                "content": "PermitRootLogin yes\n",
            },
            "reason": "演示一个越界请求",
        }
    ],
}

PLAN_BADSTEP = {
    "intent": "乱来",
    "steps": [
        {
            "action": "rm_rf",
            "params": {"target": "/"},
            "reason": "",
        }
    ],
}

PLAN_NOTJSON = "抱歉，我需要更多信息才能帮你做这件事。你想要在什么发行版上做？"

PLAN_FENCE = PLAN_GOOD


def _reply_plan(plan_obj: dict) -> str:
    content = json.dumps(plan_obj, ensure_ascii=False, indent=2)
    return json.dumps({
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }, ensure_ascii=False)


def _reply_text(text: str) -> str:
    return json.dumps({
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }, ensure_ascii=False)


class Handler(BaseHTTPRequestHandler):
    # 让访问日志短一点，别把模型的废话又刷一遍
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("mock-llm %s - %s\n" % (self.command, args[0] if args else ""))

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _send(self, payload: str, code: int = 200) -> None:
        body = payload.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _norm(self) -> str:
        """剥掉可选的 /v1 前缀。

        base_url 到底写不写 /v1 各家不一样，`autonomicctl setup` 的提示里按
        通行写法给的是带 /v1 的。两边都对，所以这里两种都接。
        """
        path = self.path.split("?")[0]
        if path.startswith("/v1/"):
            return path[3:]
        return path

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler 的命名约定)
        if self._norm().startswith("/models"):
            self._send(json.dumps({
                "object": "list",
                "data": [{"id": MODEL, "object": "model", "owned_by": "mock"}],
            }))
            return
        self._send(json.dumps({"error": {"message": "not found"}}), 404)

    def do_POST(self) -> None:  # noqa: N802
        raw = self._read_body()
        if not self._norm().startswith("/chat/completions"):
            self._send(json.dumps({"error": {"message": "not found"}}), 404)
            return

        # 请求里没带 Authorization 就当没钥匙。
        # 这一条是有意的：凭证真的被送出去过这件事，应当被检查。
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not auth[7:].strip():
            self._send(json.dumps({
                "error": {"message": "missing api key", "type": "invalid_request"}
            }), 401)
            return

        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            self._send(json.dumps({"error": {"message": "bad json"}}), 400)
            return

        mode = SERVER_MODE
        user_text = ""
        msgs = payload.get("messages") or []
        for msg in msgs:
            if msg.get("role") == "user":
                user_text = str(msg.get("content", ""))
        # 请求里直接点名要哪种返回，方便一条命令测多个分支
        hit = re.search(r"mode[=: ]+(good|empty|illegal|badstep|notjson|fence)",
                        user_text, re.I)
        if hit:
            mode = hit.group(1).lower()

        if mode == "notjson":
            self._send(_reply_text(PLAN_NOTJSON))
        elif mode == "illegal":
            self._send(_reply_plan(PLAN_ILLEGAL))
        elif mode == "badstep":
            self._send(_reply_plan(PLAN_BADSTEP))
        elif mode == "empty":
            self._send(_reply_plan(PLAN_EMPTY))
        elif mode == "fence":
            self._send(_reply_text(
                "好的，这是我的计划：\n```json\n"
                + json.dumps(PLAN_FENCE, ensure_ascii=False, indent=2)
                + "\n```\n如果还需要调整请告诉我。"))
        else:
            self._send(_reply_plan(PLAN_GOOD))


SERVER_MODE = "good"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--mode", default="good",
                    choices=("good", "empty", "illegal", "badstep",
                             "notjson", "fence"))
    args = ap.parse_args()

    global SERVER_MODE
    SERVER_MODE = args.mode

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("mock-llm 监听 http://%s:%d/v1  模式=%s" % (args.host, args.port, args.mode))
    print("Ctrl-C 退出。它只服务 /models 和 /chat/completions，别的都 404。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
