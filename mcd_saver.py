#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦门省钱助手 / MCD Saver
------------------------
基于麦当劳中国 MCP 的真实数据, 在给定人数/预算/忌口条件下,
凑出"最划算的一单", 并给出真实优惠金额与"再凑多少可解锁下一档优惠"的建议。

核心差异点:
  麦当劳 MCP 的 calculate-price 会返回 enjoyable.balance(门槛差额),
  即"距离下一档优惠还差多少钱"。本工具据此给出凑单建议,
  这是单纯查菜单价目表做不到的。

所有价格单位为"分", 输出时转换为元。

用法:
  export MCD_MCP_TOKEN=你的Token
  python mcd_saver.py --city 广州 --keyword 中山大学 --people 2 --budget 60

依赖: 仅标准库
"""

import json
import os
import sys
import itertools
from collections import Counter
import urllib.request
import urllib.error
from typing import Any

MCP_URL = "https://mcp.mcd.cn"
TOKEN_ENV = "MCD_MCP_TOKEN"

# 角色识别关键词。判定顺序: 主食 -> 小食 -> 饮品
# 注意: 不能用"早餐""鸡""奶"这类宽泛词, 否则早餐时段会把牛奶判成主食
MAIN_KEYWORDS = ("堡", "卷", "麦满分", "饭", "猪柳", "鸡腿", "牛肉", "鱼排",
                 "炒双蛋", "蛋堡", "鸡排", "牛排")
SIDE_KEYWORDS = ("薯", "鸡块", "鸡翅", "派", "新地", "麦旋风", "甜筒", "玉米",
                 "脆", "饼", "肠", "小食", "沙拉", "鸡米花")
DRINK_KEYWORDS = ("咖啡", "茶", "牛奶", "可乐", "橙", "豆浆", "酷", "饮",
                  "拿铁", "美式", "水", "雪碧")


class McpError(Exception):
    pass


def call(tool: str, arguments: dict) -> dict:
    """调用麦当劳 MCP 的 tools/call, 返回解析后的 data 字段。"""
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise McpError(f"请先设置环境变量 {TOKEN_ENV}")

    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": tool, "arguments": arguments}},
        ensure_ascii=False,
    ).encode("utf-8")

    req = urllib.request.Request(MCP_URL, data=body, method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")

    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise McpError("401 Token 无效或已过期, 请检查 MCD_MCP_TOKEN")
        if e.code == 429:
            raise McpError("429 触发限流(600次/分钟), 请放慢请求频率")
        raise McpError(f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}")

    if "error" in raw:
        raise McpError(f"MCP error: {raw['error']}")

    text = "".join(
        c.get("text", "") for c in raw.get("result", {}).get("content", [])
        if isinstance(c, dict)
    )
    return extract_data(text)


def extract_data(text: str) -> dict:
    """
    MCP 返回体是"字段说明 + 原始 JSON"的混合文本,
    这里稳健地提取最后一个可解析且含 data 的 JSON 对象。
    """
    blocks, i = [], 0
    while True:
        s = text.find("{", i)
        if s < 0:
            break
        depth = 0
        end = -1
        for j in range(s, len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        if end < 0:
            break
        blocks.append(text[s:end])
        i = end

    for b in reversed(blocks):
        try:
            obj = json.loads(b)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "data" in obj:
            if obj.get("success") is False:
                raise McpError(obj.get("message", "接口返回失败"))
            return obj.get("data") or {}
    raise McpError("未能从响应中解析出 data 字段")


def fen(x) -> float:
    """分转元。"""
    try:
        return round(int(x) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- 业务步骤

def find_store(city: str, keyword: str) -> dict:
    """按位置查询附近可点餐门店, 返回最近的一家。"""
    data = call("query-nearby-stores",
                {"beType": 1, "searchType": 2, "city": city, "keyword": keyword})
    stores = data if isinstance(data, list) else data.get("data", [])
    if not stores:
        raise McpError(f"未在 {city} 找到关键词为 {keyword} 的门店")
    stores = sorted(stores, key=lambda s: s.get("distance") or 999999)
    return stores[0]


def fetch_menu(store_code: str) -> tuple[list, dict]:
    """拉取门店当前可售菜单, 返回 (分类列表, 餐品映射表)。"""
    data = call("query-meals",
                {"storeCode": store_code, "orderType": 1, "beType": 1})
    categories = data.get("categories", []) or []
    meals = data.get("meals", {}) or {}
    return categories, meals


def fetch_available_coupons() -> list:
    """查询当前可领取的麦麦省优惠券(只读, 不会真的领券)。"""
    try:
        data = call("available-coupons", {})
    except McpError:
        return []
    if isinstance(data, list):
        return data
    return data.get("data", []) or []


def price_it(store_code: str, items: list) -> dict:
    """真实算价, 返回含优惠金额与门槛差额的原始结果。"""
    return call("calculate-price", {
        "storeCode": store_code,
        "orderType": 1,
        "beType": 1,
        "items": items,
    })


def build_catalog(categories: list, meals: dict, avoid: list) -> list:
    """把菜单摊平成候选列表, 标注角色并过滤忌口。"""
    catalog = []
    for cat in categories:
        cat_name = (cat.get("name") or "").replace("\n", "")
        for m in cat.get("meals", []) or []:
            code = str(m.get("code"))
            detail = meals.get(code) or {}
            name = detail.get("name") or f"商品{code}"
            if any(a in name for a in avoid):
                continue
            try:
                price = float(detail.get("currentPrice") or 0)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            role = "other"
            if any(k in name or k in cat_name for k in MAIN_KEYWORDS):
                role = "main"
            elif any(k in name or k in cat_name for k in SIDE_KEYWORDS):
                role = "side"
            elif any(k in name or k in cat_name for k in DRINK_KEYWORDS):
                role = "drink"

            catalog.append({
                "code": code,
                "name": name,
                "price": price,
                "original": float(detail.get("originalPrice") or price),
                "image": detail.get("image") or "",
                "discountType": detail.get("discountType"),
                "cat": cat_name,
                "role": role,
            })
    # 去重(同名同码)
    seen, uniq = set(), []
    for it in catalog:
        if it["code"] in seen:
            continue
        seen.add(it["code"])
        uniq.append(it)
    return uniq


def plan(catalog: list, people: int, budget: float) -> list[dict]:
    """
    生成候选组合: 每人一份主食, 再按预算补充小食/饮料。
    返回按"原价合计"降序的候选列表(越接近预算越靠前)。
    """
    mains = sorted([c for c in catalog if c["role"] == "main"], key=lambda x: -x["price"])
    sides = sorted([c for c in catalog if c["role"] == "side"], key=lambda x: -x["price"])
    drinks = sorted([c for c in catalog if c["role"] == "drink"], key=lambda x: -x["price"])
    fillers = (sides + drinks)[:12]

    if not mains:
        mains = sorted(catalog, key=lambda x: -x["price"])[:12]

    combos = []
    # 只保留"x人数后仍放得进预算"的主食, 再按价格降序取前6(便宜主食也有机会)
    # 否则四件套价位(50+)的主食会把低价场景全部挤掉, 导致"预算内没有可行组合"
    main_picks = [m for m in mains if m["price"] * people <= budget][:6]
    for m in main_picks:
        base = [m] * people
        base_cost = m["price"] * people
        if base_cost > budget:
            continue
        combos.append(base)
        # 补充 1-2 件小食/饮料
        for extra in itertools.combinations(fillers, 1):
            cost = base_cost + sum(e["price"] for e in extra)
            if cost <= budget:
                combos.append(base + list(extra))
        for extra in itertools.combinations(fillers, 2):
            cost = base_cost + sum(e["price"] for e in extra)
            if cost <= budget:
                combos.append(base + list(extra))

    # 去重 + 排序: 优先用满预算(一般优惠档位更高)
    uniq, seen = [], set()
    for c in combos:
        key = tuple(sorted(Counter(i["code"] for i in c).items()))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    uniq.sort(key=lambda c: -sum(i["price"] for i in c))
    return uniq[:12]


def evaluate(store_code: str, combo: list) -> dict | None:
    """对单个组合真实算价, 汇总结果。"""
    agg: dict[str, dict] = {}
    for i in combo:
        agg.setdefault(i["code"], {"item": i, "qty": 0})
        agg[i["code"]]["qty"] += 1

    items = [{"productCode": c, "quantity": v["qty"]} for c, v in agg.items()]
    try:
        data = price_it(store_code, items)
    except McpError:
        return None

    discount = fen(data.get("discount"))
    original = fen(data.get("originalPrice")) or fen(data.get("productOriginalPrice"))
    payable = fen(data.get("price")) or fen(data.get("productPrice"))

    # 门槛差额: 距离下一档优惠还差多少
    enjoyable = data.get("enjoyable") or {}
    balance = fen(enjoyable.get("balance")) if isinstance(enjoyable, dict) else 0.0
    next_discount = None
    if isinstance(enjoyable, dict) and enjoyable.get("realDiscount") is not None:
        at = enjoyable.get("amountType")
        label = {"1": "特价", "2": "折扣", "3": "立减"}.get(str(at), "优惠")
        next_discount = f"{label} {enjoyable.get('realDiscount')}"

    lines = []
    for c, v in agg.items():
        it = v["item"]
        lines.append(f"  {it['name']} x{v['qty']}  @{it['price']:.1f}元")

    return {
        "lines": lines,
        "original": original,
        "payable": payable,
        "discount": discount,
        "balance": balance,
        "next_discount": next_discount,
        "raw": data,
    }


def report(store: dict, catalog: list, results: list, coupons: list, budget: float = 0.0) -> str:
    best_budget = budget
    out = []
    out.append("=" * 56)
    out.append("麦门省钱助手 | 实时算价报告")
    out.append("=" * 56)
    out.append(f"门店: {store.get('storeName')}")
    out.append(f"地址: {store.get('address')}")
    out.append(f"营业: {store.get('businessStartTime')} - {store.get('businessEndTime')}")
    out.append(f"菜单规模: {len(catalog)} 款在售餐品")
    if coupons:
        out.append(f"可领券: {len(coupons)} 张 (未自动领取)")
    out.append("")

    if not results:
        out.append("没有可在预算内成立的组合, 试试提高预算。")
        return "\n".join(out)

    # 选优: 先省得多, 同额时用得更接近预算
    best = max(results, key=lambda r: (r["discount"], -abs(best_budget - r["payable"])))
    out.append("【推荐组合】")
    out.extend(best["lines"])
    out.append("-" * 56)
    out.append(f"原价合计 : {best['original']:.2f} 元")
    out.append(f"应付金额 : {best['payable']:.2f} 元")
    out.append(f"已省     : {best['discount']:.2f} 元")
    if best["balance"] > 0 and best["next_discount"]:
        out.append(f"凑单提示 : 再加 {best['balance']:.2f} 元, 可解锁 {best['next_discount']}")
    out.append("")

    out.append("【其他候选(按省得多少排序)】")
    for r in sorted(results, key=lambda x: -x["discount"])[1:6]:
        head = " + ".join(l.strip().split(" x")[0] for l in r["lines"])
        out.append(f"  省 {r['discount']:>5.2f} 元 | 付 {r['payable']:>6.2f} 元 | {head[:34]}")
        if r["balance"] > 0 and r["next_discount"]:
            out.append(f"             再加 {r['balance']:.2f} 元可解锁 {r['next_discount']}")
    out.append("")
    out.append("注: 价格与优惠以麦当劳官方实时接口为准; 本工具只读算价, 不下单。")
    return "\n".join(out)


def main():
    import argparse
    p = argparse.ArgumentParser(description="麦门省钱助手 - 用麦当劳 MCP 凑出最划算的一单")
    p.add_argument("--city", default="广州", help="城市名")
    p.add_argument("--keyword", default="中山大学", help="位置关键词")
    p.add_argument("--store", default="", help="指定门店编码(可选, 跳过查门店)")
    p.add_argument("--people", type=int, default=2, help="用餐人数")
    p.add_argument("--budget", type=float, default=60.0, help="预算上限(元)")
    p.add_argument("--avoid", default="", help="忌口关键词, 逗号分隔")
    a = p.parse_args()

    avoid = [s.strip() for s in a.avoid.split(",") if s.strip()]

    try:
        if a.store:
            store = {"storeCode": a.store, "storeName": f"指定门店 {a.store}",
                     "address": "-", "businessStartTime": "-", "businessEndTime": "-"}
        else:
            store = find_store(a.city, a.keyword)
        store_code = str(store.get("storeCode"))

        categories, meals = fetch_menu(store_code)
        catalog = build_catalog(categories, meals, avoid)
        coupons = fetch_available_coupons()

        combos = plan(catalog, a.people, a.budget)
        results = []
        for c in combos:
            r = evaluate(store_code, c)
            if r and r["payable"] > 0 and r["payable"] <= a.budget + 0.5:
                results.append(r)

        print(report(store, catalog, results, coupons, a.budget))

    except McpError as e:
        print(f"[错误] {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
