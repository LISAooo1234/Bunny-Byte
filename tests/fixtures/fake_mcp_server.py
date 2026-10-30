"""测试用的假 MCP stdio 服务器：只用标准库。行为由命令行参数控制。

用法：python fake_mcp_server.py [--crash-on-call] [--slow-call SECONDS] [--paginate] [--noisy] [--exit-at-start]
"""

import json
import os
import sys
import time

ARGS = sys.argv[1:]
TOOLS = [
    {
        "name": "echo",
        "description": "Echo the text back.",
        "inputSchema": {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {"text": {"type": "string"}, "times": {"type": "integer"}},
            "required": ["text"],
        },
    },
    {
        "name": "write_note",
        "description": "Write a note file into the working directory.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "body": {"type": "string"}},
            "required": ["path", "body"],
        },
    },
    {"name": "fail", "description": "Always reports a tool error.", "inputSchema": {"type": "object"}},
    {"name": "env", "description": "Return selected environment variables.", "inputSchema": {"type": "object"}},
    {"name": "slow", "description": "Sleeps before answering.", "inputSchema": {"type": "object"}},
    {
        "name": "union",
        "description": "Schema with constructs some providers reject.",
        "inputSchema": {
            "type": "object",
            "$defs": {"Id": {"type": "string"}},
            "properties": {
                "id": {"$ref": "#/$defs/Id"},
                "tags": {"type": "array"},
                "value": {"anyOf": [{"type": "string"}, {"$ref": "#/$defs/Id"}]},
            },
            "required": ["id", "ghost"],
            "oneOf": [{"required": ["id"]}],
        },
    },
]


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def text(value, is_error=False):
    result = {"content": [{"type": "text", "text": value}]}
    if is_error:
        result["isError"] = True
    return result


def call(name, arguments):
    if name == "echo":
        return text(str(arguments.get("text", "")) * int(arguments.get("times", 1)))
    if name == "write_note":
        with open(arguments["path"], "w", encoding="utf-8") as handle:
            handle.write(arguments["body"])
        return text(f"wrote {arguments['path']}")
    if name == "fail":
        return text("disk is on fire", is_error=True)
    if name == "env":
        return text(json.dumps({key: os.environ.get(key) for key in ("FAKE_TOKEN", "SECRET_API_KEY", "HOME")}))
    if name == "slow":
        time.sleep(float(ARGS[ARGS.index("--slow-call") + 1]) if "--slow-call" in ARGS else 5)
        return text("finally")
    return None


def main():
    if "--exit-at-start" in ARGS:
        sys.stderr.write("boom at start\n")
        sys.exit(3)
    if "--noisy" in ARGS:
        print("starting fake server (not json)", flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": [1], "result": {}}), flush=True)  # 不可哈希的 id
    if "--spawn-child" in ARGS:
        import subprocess

        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        with open(ARGS[ARGS.index("--spawn-child") + 1], "w") as handle:
            handle.write(str(child.pid))
    for line in sys.stdin:
        message = json.loads(line)
        method, request_id = message.get("method"), message.get("id")
        if request_id is None:
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}})
            if "--noisy" in ARGS:
                send({"jsonrpc": "2.0", "id": 900, "method": "roots/list"})
                send({"jsonrpc": "2.0", "id": 901, "method": "ping"})
        elif method == "tools/list":
            if "--paginate" in ARGS:
                cursor = (message.get("params") or {}).get("cursor")
                page = TOOLS[:2] if not cursor else TOOLS[2:]
                result = {"tools": page}
                if not cursor:
                    result["nextCursor"] = "page2"
            else:
                result = {"tools": TOOLS}
            send({"jsonrpc": "2.0", "id": request_id, "result": result})
        elif method == "tools/call":
            if "--crash-on-call" in ARGS:
                os._exit(7)
            params = message.get("params") or {}
            result = call(params.get("name"), params.get("arguments") or {})
            if result is None:
                send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "unknown tool"}})
            else:
                send({"jsonrpc": "2.0", "id": request_id, "result": result})
        else:
            send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "nope"}})


if __name__ == "__main__":
    main()
