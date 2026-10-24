#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小智 MCP 工具服务 (stdio)
=========================
被 mcp_pipe.py 拉起, 通过标准输入输出与小智云端交换 JSON-RPC。
本进程只做协议转换, 业务一律转给 bridge_server 的 HTTP API。

零第三方依赖。所有日志走 stderr, 保证 stdout 只有 JSON-RPC。

工具清单:
  mcd_plan    按人数/预算/忌口配一单
  mcd_cart    查看当前餐盘与算价
  mcd_add     按名称或编码加入餐盘
  mcd_remove  减少或移除
  mcd_clear   清空餐盘
  mcd_key     模拟硬件按键 1-8
  mcd_menu    查看当前可售菜单摘要
  yb_search   元宝搜索联网查证(热量/营养/饮食建议/市场行情)
"""

import json
import os
import sys
import urllib.request
import urllib.error

BRIDGE = "http://127.0.0.1:8787"
PROTOCOL = "2024-11-05"
SERVER_INFO = {"name": "mcd-order-bridge", "version": "0.2.0"}

# 元宝搜索（腾讯云 WSA）服务 API KEY 方式
# 文档: https://cloud.tencent.com/document/product/1806/130615
YUANBAO_ENDPOINT = "https://api.wsa.cloud.tencent.com/SearchPro"


def log(msg):
    print(f"[mcp] {msg}", file=sys.stderr, flush=True)


def bridge_post(path, payload=None, timeout=60):
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BRIDGE + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def bridge_get(path, timeout=30):
    with urllib.request.urlopen(BRIDGE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ------------------------------------------------------------------ 工具实现

def tool_plan(args):
    try:
        r = bridge_post("/api/plan", {
            "people": args.get("people", 2),
            "budget": args.get("budget", 60),
            "avoid": args.get("avoid", ""),
        })
    except Exception as e:
        return f"配单失败: {e}"
    if r.get("error"):
        return f"配单失败: {r['error']}"
    lines = " / ".join(l.strip() for l in r.get("lines", []))
    out = f"方案: {lines}\n原价 {r['original']:.2f} 元, 应付 {r['payable']:.2f} 元, 已省 {r['discount']:.2f} 元"
    if r.get("balance") and r.get("next"):
        out += f"\n再加 {r['balance']:.2f} 元可解锁 {r['next']}"
    return out[:1000]


def tool_cart(args):
    try:
        s = bridge_get("/api/state")
    except Exception as e:
        return f"读取失败: {e}"
    cart = s.get("cart") or []
    if not cart:
        return "餐盘是空的。"
    items = ", ".join(f"{i['name']}x{i['qty']}" for i in cart)
    p = s.get("price") or {}
    out = f"餐盘: {items}\n应付 {p.get('payable', 0):.2f} 元, 已省 {p.get('discount', 0):.2f} 元"
    if p.get("balance") and p.get("next"):
        out += f"\n再加 {p['balance']:.2f} 元可解锁 {p['next']}"
    return out[:1000]


def tool_add(args):
    name = (args.get("name") or "").strip()
    qty = int(args.get("qty", 1))
    if not name:
        return "请告诉我餐品名称。"
    try:
        menu = bridge_get("/api/menu")
        hit = None
        for c in menu.get("catalog", []):
            if name in c["name"] or c["name"] in name:
                hit = c
                break
        code = hit["code"] if hit else None
        if code is None:
            return f"菜单里没有找到「{name}」。"
        bridge_post("/api/add", {"code": code, "qty": qty})
        return f"已加入 {hit['name']} x{qty}。"
    except Exception as e:
        return f"加入失败: {e}"


def tool_remove(args):
    name = (args.get("name") or "").strip()
    try:
        menu = bridge_get("/api/menu")
        hit = None
        for c in menu.get("catalog", []):
            if name in c["name"] or c["name"] in name:
                hit = c
                break
        if not hit:
            return f"没找到「{name}」。"
        bridge_post("/api/remove", {"code": hit["code"], "qty": int(args.get("qty", 1))})
        return f"已减少 {hit['name']}。"
    except Exception as e:
        return f"移除失败: {e}"


def tool_clear(args):
    try:
        bridge_post("/api/clear", {})
        return "餐盘已清空。"
    except Exception as e:
        return f"清空失败: {e}"


def tool_key(args):
    n = int(args.get("key", 0))
    if n < 1 or n > 8:
        return "按键只能是 1 到 8。"
    try:
        r = bridge_post("/api/key", {"key": n})
    except Exception as e:
        return f"按键失败: {e}"
    if r.get("error"):
        return str(r["error"])
    if n == 8:
        p = r.get("price") or {}
        return f"已确认, 应付 {p.get('payable', 0):.2f} 元, 已省 {p.get('discount', 0):.2f} 元。"
    return f"按键 {n}: 已加入 {r.get('name', '')}。"


def tool_menu(args):
    try:
        menu = bridge_get("/api/menu")
        s = bridge_get("/api/state")
    except Exception as e:
        return f"读取失败: {e}"
    cats = {}
    for c in menu.get("catalog", []):
        cats.setdefault(c.get("cat", "其他"), []).append(c["name"])
    parts = [f"{k}: {', '.join(v[:6])}" for k, v in list(cats.items())[:6]]
    return (f"门店 {s.get('storeCode')}, 共 {len(menu.get('catalog', []))} 款在售。\n"
            + "\n".join(parts))[:1000]


def tool_yb_search(args):
    """联网搜索。当用户问到需要查证的问题时使用: 餐品热量、营养成分、
    食材来源、饮食建议、健身计划、市场行情等本地菜单里没有的信息。
    keywords 用简短中文关键词, 一次问一个点。"""
    keywords = (args.get("keywords") or "").strip()
    if not keywords:
        return "请给出发起搜索的关键词。"
    api_key = os.environ.get("YUANBAO_API_KEY") or os.environ.get("TENCENTCLOUD_WSA_APIKEY")
    if not api_key:
        return "未配置元宝搜索 API KEY（环境变量 YUANBAO_API_KEY）。"
    body = json.dumps({"Query": keywords}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(YUANBAO_ENDPOINT, data=body, method="POST")
    req.add_header("Authorization", "Bearer " + api_key)
    req.add_header("Content-Type", "application/json; charset=UTF-8")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return f"搜索失败: HTTP {e.code} {e.read().decode('utf-8', 'ignore')[:120]}"
    except Exception as e:
        return f"搜索失败: {e}"
    pages = (data.get("Response") or {}).get("Pages") or []
    if not pages:
        return f"没有搜到关于「{keywords}」的可靠结果, 建议换关键词再试。"
    lines = [f"「{keywords}」的搜索结果:"]
    budget = 900  # 官方提示: 返回值通常限制在 1024 字节内
    for p in pages[:3]:
        try:
            item = json.loads(p) if isinstance(p, str) else p
        except json.JSONDecodeError:
            continue
        title = (item.get("title") or "").strip()[:40]
        passage = (item.get("passage") or "").replace("<strong>", "").replace("</strong>", "")
        passage = " ".join(passage.split())[:150]
        url = (item.get("url") or "").strip()[:120]
        block = f"- {title}: {passage}\n  来源 {url}"
        if len(block.encode("utf-8")) > budget:
            break
        budget -= len(block.encode("utf-8"))
        lines.append(block)
    # 官方提示: 设备侧返回值通常限制在 1024 字节内, 按 UTF-8 字节截断
    out = "\n".join(lines)
    if len(out.encode("utf-8")) > 1000:
        out = out.encode("utf-8")[:1000].decode("utf-8", "ignore") + "…"
    return out


TOOLS = {
    "mcd_plan": (tool_plan, "当用户说要吃什么、给一个预算和人数、或让你帮忙配一单时使用。"
                            "按预算和忌口凑出最划算的组合, 并返回真实优惠金额。",
                 {"people": "integer", "budget": "number", "avoid": "string"}, ["budget"]),
    "mcd_cart": (tool_cart, "查看当前餐盘里有什么、一共多少钱、省了多少。", {}, []),
    "mcd_add": (tool_add, "把某个餐品加入餐盘。用户说要加什么时使用。",
                {"name": "string", "qty": "integer"}, ["name"]),
    "mcd_remove": (tool_remove, "从餐盘里减少或移除某个餐品。",
                   {"name": "string", "qty": "integer"}, ["name"]),
    "mcd_clear": (tool_clear, "清空整个餐盘。", {}, []),
    "mcd_key": (tool_key, "模拟按下硬件上的 1 到 8 号按键, 1 到 7 是快捷加菜, 8 是确认这一单。",
                {"key": "integer"}, ["key"]),
    "mcd_menu": (tool_menu, "查看这家店现在卖什么、有哪些分类。", {}, []),
    "yb_search": (tool_yb_search, "联网搜索真实信息。当用户问到菜单里没有、需要查证的问题时使用: "
                  "比如餐品热量、营养成分、食材、饮食建议、健身计划、市场价格等。"
                  "返回带来源链接的搜索结果, 用户要求给依据时必须调用。",
                  {"keywords": "string"}, ["keywords"]),
}


def tools_list():
    out = []
    for name, (fn, desc, props, required) in TOOLS.items():
        schema = {"type": "object", "properties": {}, "required": required}
        for p, t in props.items():
            schema["properties"][p] = {"type": t}
        out.append({
            "name": name,
            "description": desc,
            "inputSchema": schema,
        })
    return out


# ------------------------------------------------------------------ JSON-RPC

def respond(msg_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result},
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()


def respond_err(msg_id, code, text):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id,
                                "error": {"code": code, "message": text}},
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req):
    method = req.get("method")
    msg_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        respond(msg_id, {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
        return

    if method == "notifications/initialized" or (method or "").startswith("notifications/"):
        return

    if method == "ping":
        respond(msg_id, {})
        return

    if method == "tools/list":
        respond(msg_id, {"tools": tools_list()})
        return

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        entry = TOOLS.get(name)
        if not entry:
            respond(msg_id, {"content": [{"type": "text", "text": f"没有这个工具: {name}"}],
                             "isError": True})
            return
        fn = entry[0]
        try:
            text = fn(args)
        except Exception as e:
            log(f"工具 {name} 异常: {e}")
            text = f"执行出错: {e}"
            respond(msg_id, {"content": [{"type": "text", "text": text}],
                             "isError": True})
            return
        log(f"调用 {name} -> {text[:80]}")
        respond(msg_id, {"content": [{"type": "text", "text": text}], "isError": False})
        return

    if msg_id is not None:
        respond_err(msg_id, -32601, f"不支持的方法: {method}")


def main():
    log("stdio MCP 服务已启动")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            handle(req)
        except Exception as e:
            log(f"处理异常: {e}")


if __name__ == "__main__":
    main()
