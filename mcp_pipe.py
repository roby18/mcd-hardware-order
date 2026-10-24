#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小智 MCP 接入点桥接器
=====================
把本地 stdio MCP 服务接到小智云端的 MCP 接入点。

    小智云端 (wss://api.xiaozhi.me/mcp/?token=...)
        |<-- WebSocket 文本帧 -->|
    mcp_pipe.py (本文件)
        |<-- stdin / stdout -->|
    mcp_server.py (本地 stdio MCP 服务)

连接是本机主动出站, 所以本地服务不需要公网 IP。

用法:
    set MCP_ENDPOINT=wss://api.xiaozhi.me/mcp/?token=你的接入点token
    python mcp_pipe.py

依赖: websockets (pip install websockets)
"""

import asyncio
import os
import sys
import signal
import subprocess
import argparse

try:
    import websockets
except ImportError:
    print("缺少依赖, 请先执行: pip install websockets", file=sys.stderr)
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "mcp_server.py")


def log(msg):
    print(f"[pipe] {msg}", file=sys.stderr, flush=True)


async def pump_stdin(ws, proc):
    """WebSocket -> 子进程 stdin"""
    try:
        async for message in ws:
            if isinstance(message, bytes):
                message = message.decode("utf-8", errors="ignore")
            message = message.strip()
            if not message:
                continue
            proc.stdin.write((message + "\n").encode("utf-8"))
            await proc.stdin.drain()
    except websockets.exceptions.ConnectionClosed:
        log("与小智的连接已关闭")
    except Exception as e:
        log(f"下行中断: {e}")


async def pump_stdout(ws, proc):
    """子进程 stdout -> WebSocket"""
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="ignore").strip()
            if not text:
                continue
            await ws.send(text)
    except websockets.exceptions.ConnectionClosed:
        log("上行连接已关闭")
    except Exception as e:
        log(f"上行中断: {e}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=os.environ.get("MCP_ENDPOINT", ""),
                    help="小智 MCP 接入点地址, 形如 wss://api.xiaozhi.me/mcp/?token=...")
    a = ap.parse_args()

    if not a.endpoint:
        print("请提供 MCP 接入点地址:", file=sys.stderr)
        print("  方式一: python mcp_pipe.py --endpoint wss://...", file=sys.stderr)
        print("  方式二: set MCP_ENDPOINT=wss://... 后再运行", file=sys.stderr)
        print("接入点获取: 小智控制台 -> 配置角色 -> 高级设置 -> MCP设置 -> 获取 MCP 接入点",
              file=sys.stderr)
        sys.exit(2)

    python = sys.executable
    proc = await asyncio.create_subprocess_exec(
        python, SERVER,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=None,
    )
    log(f"已拉起 mcp_server: {SERVER}")

    retry = 0
    while True:
        try:
            log("正在连接小智 MCP 接入点...")
            async with websockets.connect(a.endpoint, ping_interval=20,
                                          ping_timeout=20, close_timeout=10) as ws:
                retry = 0
                log("已连接小智 MCP 接入点")
                await asyncio.gather(
                    pump_stdin(ws, proc),
                    pump_stdout(ws, proc),
                )
        except Exception as e:
            retry += 1
            wait = min(30, 2 ** min(retry, 5))
            log(f"连接失败({type(e).__name__}: {e}), {wait}s 后重试")
            await asyncio.sleep(wait)
        if proc.returncode is not None:
            log("mcp_server 已退出, 桥接器结束")
            break


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("已停止")
