#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
点餐桥接服务 (Bridge Server)
============================
职责:
  1. 持有"餐盘"状态(已选餐品、预算、热量、算价结果)
  2. 提供 HTTP API 给网页 / ESP32 按键 / MCP server 调用
  3. 通过 SSE 把状态变化实时推给网页
  4. 底层真实调用麦当劳 MCP(经 mcd_saver 封装)

零第三方依赖: 仅用 Python 标准库。
启动: python bridge_server.py [--port 8787] [--store 1400038]
"""

import json
import os
import re
import sys
import time
import threading
import argparse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 复用已跑通的麦当劳 MCP 封装
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "dine-mcd-saver", "scripts"))
try:
    import mcd_saver as mcd
except ImportError:
    mcd = None

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
IMG_DIR = os.path.join(WEB_DIR, "img")
os.makedirs(IMG_DIR, exist_ok=True)

DEFAULT_STORE = "1400038"          # 麦当劳广州英雄广场餐厅
DEFAULT_CITY = "广州"
DEFAULT_KEYWORD = "中山大学"

# 元宝搜索（腾讯云 WSA）服务 API KEY 方式
# 文档: https://cloud.tencent.com/document/product/1806/130615
YUANBAO_URL = "https://api.wsa.cloud.tencent.com/SearchPro"


class Bridge:
    """全局餐盘状态。所有变更都走这里, 变更后广播给 SSE 客户端。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.store_code = DEFAULT_STORE
        self.store_info = {}
        self.catalog = []           # 候选餐品 [{code,name,price,original,role,cat}]
        self.cart = {}              # code -> {"item":..., "qty": n}
        self.selection = 0          # 当前高亮(旋钮切换)
        self.budget = 60.0
        self.people = 2
        self.avoid = []
        self.price_result = None    # 最近一次真实算价
        self.coupons = []
        self.log = []               # 事件流水, 网页上滚动显示
        self.subscribers = []       # SSE 客户端队列
        self.menu_loaded = False
        self.voice_active = False   # 旋钮按下: 语音交互中
        # 大模型备选引擎(OpenAI 兼容) + 按键映射, 持久化在 config/ 下
        self.llm = {
            "enabled": False,       # 是否启用大模型引擎(配单/建议)
            "base": "",             # 如 https://api.deepseek.com
            "key": "",
            "model": "deepseek-v4.1-flash",
        }
        self.keymap = {}            # {"1": {"type":"item","code":"5022"}, "8": {"type":"action","action":"confirm"}}
        self._load_config()

    # ------------------------------------------------ 配置持久化
    def _load_config(self):
        """读取 config/llm.json 与 config/keymap.json, 缺省用内置值。"""
        try:
            p = os.path.join(HERE, "config", "llm.json")
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                self.llm.update({k: d.get(k, self.llm[k]) for k in self.llm})
        except Exception as e:
            print(f"[config] llm.json 读取失败: {e}")
        try:
            p = os.path.join(HERE, "config", "keymap.json")
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    self.keymap = json.load(f)
        except Exception as e:
            print(f"[config] keymap.json 读取失败: {e}")

    def save_llm(self, cfg):
        with self.lock:
            for k in ("enabled", "base", "key", "model"):
                if k in cfg:
                    self.llm[k] = cfg[k]
        try:
            os.makedirs(os.path.join(HERE, "config"), exist_ok=True)
            with open(os.path.join(HERE, "config", "llm.json"), "w", encoding="utf-8") as f:
                json.dump(self.llm, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[config] llm.json 写入失败: {e}")
        self.push_log(f"大模型引擎配置已更新: {self.llm['model'] or '(未设模型)'}")
        return dict(self.llm)

    def save_keymap(self, km):
        with self.lock:
            self.keymap = km or {}
        try:
            os.makedirs(os.path.join(HERE, "config"), exist_ok=True)
            with open(os.path.join(HERE, "config", "keymap.json"), "w", encoding="utf-8") as f:
                json.dump(self.keymap, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[config] keymap.json 写入失败: {e}")
        return dict(self.keymap)

    def key_target(self, n):
        """按键 n 的目标: 优先用户配置, 否则默认 1-7=前7个主食, 8=确认。"""
        if n == 8:
            return {"type": "action", "action": "confirm"}
        cfg = self.keymap.get(str(n))
        if cfg:
            return cfg
        with self.lock:
            mains = [c for c in self.catalog if c.get("role") == "main"]
            pool = mains if len(mains) >= n else self.catalog
            if n - 1 >= len(pool):
                return None
            return {"type": "item", "code": pool[n - 1]["code"]}

    # ------------------------------------------------ 大模型引擎
    def llm_ready(self):
        return bool(self.llm.get("enabled") and self.llm.get("base")
                    and self.llm.get("key") and self.llm.get("model"))

    def llm_chat(self, messages, temperature=0.4, max_tokens=700):
        """OpenAI 兼容 chat/completions。返回 (ok, content_or_error)。"""
        if not self.llm_ready():
            return False, "大模型未配置完整(base/key/model)"
        url = self.llm["base"].rstrip("/") + "/chat/completions"
        body = json.dumps({
            "model": self.llm["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", "Bearer " + self.llm["key"])
        req.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                d = json.loads(r.read().decode("utf-8"))
            return True, d["choices"][0]["message"]["content"]
        except Exception as e:
            return False, f"大模型调用失败: {e}"

    def llm_plan(self, people, budget, avoid):
        """让大模型选餐, 但**价格一律回店用 MCP 真实计算**。
        LLM 只负责"选什么", 不负责"多少钱"——这是与纯 LLM 应用的本质区别。"""
        if mcd is None or not self.catalog:
            return {"error": "菜单未加载或缺少 mcd_saver"}
        menu_txt = "\n".join(
            f"{c['code']}|{c['name']}|{c['price']:.1f}元|{c.get('role', '')}"
            for c in self.catalog[:60]
        )
        avoid_txt = "、".join(avoid) if avoid else "无"
        prompt = (
            f"你在麦当劳点餐。门店菜单(编号|名称|现价|角色):\n{menu_txt}\n\n"
            f"需求: {people} 人, 总预算 {budget} 元, 忌口: {avoid_txt}。\n"
            "请只返回 JSON, 不要任何其他文字。格式: "
            '{"codes":[{"code":"5022","qty":1}],"reason":"一句话说明"}\n'
            "约束: 份量要够所有人吃饱; 总现价不超过预算; 有主食有小食有饮品; 编号必须来自上面的菜单。"
        )
        ok, content = self.llm_chat(
            [{"role": "user", "content": prompt}], temperature=0.3, max_tokens=400)
        if not ok:
            return {"error": content, "fallback": True}
        # 解析 LLM 返回的 JSON(容忍前后杂质)
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            return {"error": "大模型返回无法解析", "fallback": True}
        try:
            picked = json.loads(m.group(0)).get("codes", [])
        except Exception:
            return {"error": "大模型返回 JSON 解析失败", "fallback": True}
        by_code = {c["code"]: c for c in self.catalog}
        combo = []
        for p in picked:
            it = by_code.get(str(p.get("code", "")))
            if it and int(p.get("qty", 1)) > 0:
                combo.extend([it] * int(p.get("qty", 1)))
        if not combo:
            return {"error": "大模型选的餐品都不在菜单里", "fallback": True}
        r = mcd.evaluate(self.store_code, combo)
        if not r or r["payable"] <= 0:
            return {"error": "MCP 算价失败", "fallback": True}
        with self.lock:
            self.cart.clear()
            for it in combo:
                self.cart.setdefault(it["code"], {"item": it, "qty": 0})
                self.cart[it["code"]]["qty"] += 1
        self.price_result = {k: r.get(k) for k in
                             ("original", "payable", "discount", "balance", "next_discount")}
        self.push_log(f"大模型 {self.llm['model']} 选餐完成, MCP 已核价")
        self.broadcast()
        r["engine"] = "llm"
        r["model"] = self.llm["model"]
        return r

    # ------------------------------------------------ 状态快照
    def snapshot(self):
        with self.lock:
            items = []
            for code, e in self.cart.items():
                it = e["item"]
                items.append({
                    "code": code,
                    "name": it["name"],
                    "price": it["price"],
                    "role": it.get("role", "other"),
                    "qty": e["qty"],
                    "subtotal": round(it["price"] * e["qty"], 2),
                })
            pr = self.price_result or {}
            return {
                "store": self.store_info,
                "storeCode": self.store_code,
                "catalogSize": len(self.catalog),
                "cart": items,
                "selection": self.selection,
                "selected": (self.catalog[self.selection]["name"]
                             if self.catalog and 0 <= self.selection < len(self.catalog)
                             else ""),
                "budget": self.budget,
                "people": self.people,
                "avoid": self.avoid,
                "menuLoaded": self.menu_loaded,
                "coupons": self.coupons,
                "price": {
                    "original": pr.get("original", 0.0),
                    "payable": pr.get("payable", 0.0),
                    "discount": pr.get("discount", 0.0),
                    "balance": pr.get("balance", 0.0),
                    "next": pr.get("next_discount"),
                },
                "log": self.log[-12:],
                "voice": self.voice_active,
                "keymap": dict(self.keymap),
                "engine": "llm" if self.llm_ready() else "local",
                "ts": time.time(),
            }

    def push_log(self, text):
        with self.lock:
            self.log.append({"t": time.strftime("%H:%M:%S"), "text": text})
            if len(self.log) > 60:
                self.log = self.log[-60:]

    def voice(self, state="down"):
        """旋钮按下/松开: 语音交互(PTT)状态。"""
        down = str(state) == "down"
        with self.lock:
            self.voice_active = down
        if down:
            self.push_log("旋钮按下: 语音交互开启, 请吩咐...")
        else:
            self.push_log("旋钮松开: 语音交互结束")
        self.broadcast()

    # ------------------------------------------------ 元宝搜索（侦探赛道）
    def yb_search(self, keywords, round_no=1):
        """单次元宝搜索, 返回结构化结果 + 证据信息(RequestId/时间戳)。
        每次查询都留痕, 供侦探赛道截图与证据链报告使用。"""
        import urllib.request
        import urllib.error
        keywords = (keywords or "").strip()
        if not keywords:
            return {"error": "关键词不能为空"}
        api_key = os.environ.get("YUANBAO_API_KEY") or os.environ.get("TENCENTCLOUD_WSA_APIKEY")
        if not api_key:
            return {"error": "未配置 YUANBAO_API_KEY 环境变量"}
        body = json.dumps({"Query": keywords}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(YUANBAO_URL, data=body, method="POST")
        req.add_header("Authorization", "Bearer " + api_key)
        req.add_header("Content-Type", "application/json; charset=UTF-8")
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            self.push_log(f"[侦探] 第{round_no}轮搜索失败 HTTP {e.code}")
            return {"error": f"HTTP {e.code}", "round": round_no}
        except Exception as e:
            self.push_log(f"[侦探] 第{round_no}轮搜索异常 {e}")
            return {"error": str(e), "round": round_no}
        resp = data.get("Response") or {}
        pages_raw = resp.get("Pages") or []
        pages = []
        for p in pages_raw:
            try:
                item = json.loads(p) if isinstance(p, str) else (p or {})
            except json.JSONDecodeError:
                continue
            pages.append({
                "title": (item.get("title") or "").strip(),
                "passage": " ".join((item.get("passage") or "").split()),
                "url": (item.get("url") or "").strip(),
                "site": (item.get("site") or "").strip(),
                "date": (item.get("date") or "").strip(),
                "score": item.get("score"),
            })
        elapsed = round(time.time() - started, 2)
        self.push_log(f"[侦探] 第{round_no}轮「{keywords}」-> {len(pages)} 条 / {elapsed}s")
        return {
            "round": round_no,
            "keywords": keywords,
            "requestId": resp.get("RequestId", ""),
            "ts": time.strftime("%H:%M:%S"),
            "elapsed": elapsed,
            "count": len(pages),
            "pages": pages,
        }

    def broadcast(self):
        snap = self.snapshot()
        payload = json.dumps(snap, ensure_ascii=False)
        dead = []
        for q in list(self.subscribers):
            try:
                q.put(payload)
            except Exception:
                dead.append(q)
        for q in dead:
            if q in self.subscribers:
                self.subscribers.remove(q)

    # ------------------------------------------------ 麦当劳调用
    def load_menu(self, city=DEFAULT_CITY, keyword=DEFAULT_KEYWORD):
        if mcd is None:
            self.push_log("未找到 mcd_saver 模块, 菜单加载跳过")
            return False
        try:
            store = mcd.find_store(city, keyword)
            self.store_code = str(store.get("storeCode"))
            self.store_info = store
        except Exception as e:
            self.store_code = DEFAULT_STORE
            self.store_info = {"storeName": f"指定门店 {self.store_code}",
                               "address": "-"}
            self.push_log(f"查门店失败, 用默认门店: {e}")

        try:
            cats, meals = mcd.fetch_menu(self.store_code)
            self.catalog = mcd.build_catalog(cats, meals, self.avoid)
            # 先写好本地图片路径(文件由后台线程补下载, 未就绪时前端回退远程URL)
            for it in self.catalog:
                url = it.get("image") or ""
                ext = os.path.splitext(url)[1].lower() or ".png"
                if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                    ext = ".png"
                it["localImg"] = "/img/" + it["code"] + ext
            self.menu_loaded = True
            self.push_log(f"菜单已加载: {len(self.catalog)} 款")
        except Exception as e:
            self.push_log(f"菜单加载失败: {e}")
            return False

        try:
            self.coupons = mcd.fetch_available_coupons()
        except Exception:
            self.coupons = []
        threading.Thread(target=self.prefetch_images, daemon=True).start()
        return True

    def prefetch_images(self):
        """把菜单图片下载到 web/img/ 本地缓存: 现场断网/网络差也能演示。
        已有缓存跳过; 单张失败不影响整体。路径字段已在 load_menu 写好。"""
        import urllib.request
        ok = skip = fail = 0
        for it in self.catalog:
            url = it.get("image") or ""
            if not url:
                continue
            ext = os.path.splitext(url)[1].lower() or ".png"
            if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                ext = ".png"
            local = os.path.join(IMG_DIR, it["code"] + ext)
            if os.path.exists(local) and os.path.getsize(local) > 0:
                skip += 1
                continue
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = r.read()
                if len(data) < 200:          # 太小视为无效图
                    raise ValueError("too small")
                with open(local, "wb") as f:
                    f.write(data)
                ok += 1
            except Exception:
                fail += 1
        self.push_log(f"图片缓存: 新增 {ok} / 已有 {skip} / 失败 {fail}")

    def recalc(self):
        """对当前餐盘真实算价。"""
        if not self.cart:
            self.price_result = None
            return
        if mcd is None:
            return
        items = []
        with self.lock:
            for code, e in self.cart.items():
                items.append({"productCode": code, "quantity": e["qty"]})
        try:
            data = mcd.call("calculate-price", {
                "storeCode": self.store_code,
                "orderType": 1, "beType": 1, "items": items,
            })
            self.price_result = {
                "original": mcd.fen(data.get("originalPrice") or data.get("productOriginalPrice")),
                "payable": mcd.fen(data.get("price") or data.get("productPrice")),
                "discount": mcd.fen(data.get("discount")),
                "balance": mcd.fen((data.get("enjoyable") or {}).get("balance")),
                "next_discount": _fmt_next(data.get("enjoyable")),
            }
        except Exception as e:
            self.push_log(f"算价失败: {e}")

    # ------------------------------------------------ 操作
    def add(self, code=None, qty=1):
        with self.lock:
            if code is None:
                if not self.catalog:
                    return False
                code = self.catalog[self.selection]["code"]
            item = None
            for c in self.catalog:
                if c["code"] == code:
                    item = c
                    break
            if item is None:
                if not self.catalog:
                    return False
                item = self.catalog[self.selection]
                code = item["code"]
            e = self.cart.get(code)
            if e:
                e["qty"] += qty
            else:
                self.cart[code] = {"item": item, "qty": qty}
            self.push_log(f"加入 {item['name']} x{qty}")
        self.recalc()
        self.broadcast()
        return True

    def remove(self, code=None, qty=1):
        with self.lock:
            if code is None:
                if not self.catalog:
                    return False
                code = self.catalog[self.selection]["code"]
            e = self.cart.get(code)
            if not e:
                return False
            name = e["item"]["name"]
            e["qty"] -= qty
            if e["qty"] <= 0:
                del self.cart[code]
            self.push_log(f"移除 {name} x{qty}")
        self.recalc()
        self.broadcast()
        return True

    def clear(self):
        with self.lock:
            self.cart.clear()
            self.price_result = None
            self.push_log("清空餐盘")
        self.broadcast()

    def dial(self, delta):
        with self.lock:
            if not self.catalog:
                return
            n = len(self.catalog)
            self.selection = (self.selection + delta) % n
        self.broadcast()

    def select(self, index):
        with self.lock:
            if self.catalog:
                self.selection = max(0, min(index, len(self.catalog) - 1))
        self.broadcast()

    def key(self, n):
        """
        按键 1-7: 快捷加菜(目标可配置: 指定餐品或动作, 见 config/keymap.json)
        按键 8: 确认下单(这里只做算价 + 打日志, 不真的下单)
        """
        target = self.key_target(n)
        if n == 8 or (target or {}).get("action") == "confirm":
            self.recalc()
            pr = self.price_result or {}
            self.push_log(f"确认: 应付 {pr.get('payable', 0):.2f} 元, "
                          f"已省 {pr.get('discount', 0):.2f} 元")
            self.broadcast()
            return {"action": "confirm", "price": pr}

        if not target:
            return {"error": f"没有第 {n} 个快捷位"}

        # 动作型按键: 留了扩展位(如 clear/plan), 当前只有 confirm
        if target.get("type") == "action":
            act = target.get("action")
            if act == "clear":
                self.clear()
                return {"action": "clear"}
            return {"error": f"未知动作 {act}"}

        with self.lock:
            if not self.catalog:
                return {"error": "菜单未加载"}
            item = next((c for c in self.catalog if c["code"] == target.get("code")), None)
            if not item:
                return {"error": "该键位绑定的餐品已下架, 请重新配置"}
            self.selection = self.catalog.index(item)
            code = item["code"]
            e = self.cart.get(code)
            if e:
                e["qty"] += 1
            else:
                self.cart[code] = {"item": item, "qty": 1}
            self.push_log(f"按键 {n}: {item['name']}")
        self.recalc()
        self.broadcast()
        return {"action": "add", "name": item["name"]}

    def plan(self, people=None, budget=None, avoid=None, engine="local"):
        """按人数/预算/忌口生成推荐组合并写入餐盘。
        engine="llm" 走大模型选餐(价格仍由 MCP 真实计算), 失败自动回退本地算法。"""
        if people is not None:
            self.people = int(people)
        if budget is not None:
            self.budget = float(budget)
        if avoid is not None:
            self.avoid = [s.strip() for s in str(avoid).split(",") if s.strip()]

        if mcd is None or not self.catalog:
            return {"error": "菜单未加载或缺少 mcd_saver"}

        if engine == "llm":
            if not self.llm_ready():
                r = self._plan_local()
                r["note"] = "大模型未配置, 已回退本地算法(设置里可填 Base URL/Key/模型)"
                return r
            r = self.llm_plan(self.people, self.budget, self.avoid)
            if "error" in r and r.get("fallback"):
                r = self._plan_local()
                r["note"] = "大模型不可用, 已回退本地算法"
                return r
            return r
        return self._plan_local()

    def _plan_local(self):
        """本地组合搜索: 枚举可行组合, 用 MCP 真实算价, 选省钱最多的。"""

        combos = mcd.plan(self.catalog, self.people, self.budget)
        results = []
        for c in combos[:8]:
            r = mcd.evaluate(self.store_code, c)
            if r and r["payable"] > 0 and r["payable"] <= self.budget + 0.5:
                results.append(r)
        if not results:
            return {"error": "预算内没有可行组合"}

        best = max(results, key=lambda r: (r["discount"], -abs(self.budget - r["payable"])))

        with self.lock:
            self.cart.clear()
            for line in best["lines"]:
                pass
            # best["lines"] 是展示文本, 这里按原始数据重建餐盘
            for c in combos:
                r = mcd.evaluate(self.store_code, c)
                if r and abs(r["payable"] - best["payable"]) < 0.01 \
                        and abs(r["discount"] - best["discount"]) < 0.01:
                    for it in c:
                        self.cart.setdefault(it["code"], {"item": it, "qty": 0})
                        self.cart[it["code"]]["qty"] += 1
                    break

        self.price_result = {
            "original": best["original"],
            "payable": best["payable"],
            "discount": best["discount"],
            "balance": best["balance"],
            "next_discount": best["next_discount"],
        }
        self.push_log(f"已按 {self.people} 人 / {self.budget} 元生成方案")
        self.broadcast()
        return {
            "lines": best["lines"],
            "original": best["original"],
            "payable": best["payable"],
            "discount": best["discount"],
            "balance": best["balance"],
            "next": best["next_discount"],
        }


def _fmt_next(enjoyable):
    if not isinstance(enjoyable, dict):
        return None
    if enjoyable.get("realDiscount") is None:
        return None
    label = {"1": "特价", "2": "折扣", "3": "立减"}.get(
        str(enjoyable.get("amountType")), "优惠")
    return f"{label} {enjoyable.get('realDiscount')}"


BRIDGE = Bridge()


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 不打到 stdout, 避免污染 MCP stdio 通道

    # ------------------------------------------------ helpers
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ------------------------------------------------ GET
    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/" or path == "/index.html":
            p = os.path.join(WEB_DIR, "index.html")
            if os.path.exists(p):
                with open(p, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            else:
                self._send(404, "index.html 不存在")
            return

        if path == "/detective":
            p = os.path.join(WEB_DIR, "detective.html")
            if os.path.exists(p):
                with open(p, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            else:
                self._send(404, "detective.html 不存在")
            return

        if path == "/api/state":
            self._json(BRIDGE.snapshot())
            return

        if path == "/api/config":
            # 配置下发: 大模型(脱敏 key) + 按键映射
            llm = dict(BRIDGE.llm)
            llm["key"] = ("***" + llm["key"][-4:]) if llm.get("key") else ""
            self._json({"llm": llm, "keymap": BRIDGE.keymap,
                        "engine": "llm" if BRIDGE.llm_ready() else "local"})
            return

        if path == "/api/menu":
            self._json({"catalog": BRIDGE.catalog, "coupons": BRIDGE.coupons})
            return

        if path == "/api/events":
            self._sse()
            return

        # 菜品图片本地缓存: /img/<code>.png (断网可演示)
        if path.startswith("/img/"):
            name = path[len("/img/"):]
            if not name.replace(".png", "").replace(".jpg", "").isdigit():
                self._json({"error": "bad name"}, 404)
                return
            for ext in (".png", ".jpg", ".jpeg", ".webp"):
                p = os.path.join(IMG_DIR, name if name.endswith(ext) else name + ext)
                if os.path.exists(p):
                    with open(p, "rb") as f:
                        ctype = "image/png" if ext == ".png" else (
                            "image/jpeg" if ext in (".jpg", ".jpeg") else "image/webp")
                        self._send(200, f.read(), ctype)
                    return
            self._json({"error": "img not cached"}, 404)
            return

        self._json({"error": "not found"}, 404)

    def _sse(self):
        import queue
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q = queue.Queue()
        BRIDGE.subscribers.append(q)
        try:
            q.put(json.dumps(BRIDGE.snapshot(), ensure_ascii=False))
            while True:
                payload = q.get()
                chunk = f"data: {payload}\n\n".encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            if q in BRIDGE.subscribers:
                BRIDGE.subscribers.remove(q)

    # ------------------------------------------------ POST
    def do_POST(self):
        path = self.path.split("?")[0]
        b = self._body()

        if path == "/api/key":
            n = int(b.get("key", 0))
            self._json(BRIDGE.key(n))
            return

        if path == "/api/dial":
            BRIDGE.dial(int(b.get("delta", 0)))
            self._json({"ok": True, "selection": BRIDGE.selection})
            return

        if path == "/api/add":
            BRIDGE.add(b.get("code"), int(b.get("qty", 1)))
            self._json({"ok": True})
            return

        if path == "/api/remove":
            BRIDGE.remove(b.get("code"), int(b.get("qty", 1)))
            self._json({"ok": True})
            return

        if path == "/api/clear":
            BRIDGE.clear()
            self._json({"ok": True})
            return

        if path == "/api/select":
            BRIDGE.select(int(b.get("index", 0)))
            self._json({"ok": True})
            return

        if path == "/api/voice":
            BRIDGE.voice(b.get("state", "down"))
            self._json({"ok": True, "voice": BRIDGE.voice_active})
            return

        if path == "/api/plan":
            r = BRIDGE.plan(b.get("people"), b.get("budget"), b.get("avoid"),
                            b.get("engine", "local"))
            self._json(r)
            return

        if path == "/api/llm-config":
            if b.get("test"):
                # 测试连通: 发一条最小请求
                ok, msg = BRIDGE.llm_chat([{"role": "user", "content": "回复OK两个字"}],
                                          max_tokens=8)
                self._json({"ok": ok, "msg": (msg or "")[:120]})
                return
            cfg = BRIDGE.save_llm(b)
            self._json({"ok": True, "llm": cfg})
            return

        if path == "/api/keymap":
            self._json({"ok": True, "keymap": BRIDGE.save_keymap(b.get("keymap"))})
            return

        if path == "/api/reload":
            ok = BRIDGE.load_menu(b.get("city", DEFAULT_CITY),
                                  b.get("keyword", DEFAULT_KEYWORD))
            BRIDGE.broadcast()
            self._json({"ok": ok})
            return

        if path == "/api/yb":
            # 侦探赛道: 元宝搜索单次查询(带证据信息)
            r = BRIDGE.yb_search(b.get("keywords", ""), b.get("round", 1))
            self._json(r)
            return

        if path == "/api/say":
            # 语音/文本指令入口: 交给上层 MCP 或网页调用
            self._json({"ok": True, "echo": b.get("text", "")})
            return

        self._json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--store", default=DEFAULT_STORE)
    ap.add_argument("--city", default=DEFAULT_CITY)
    ap.add_argument("--keyword", default=DEFAULT_KEYWORD)
    ap.add_argument("--no-menu", action="store_true", help="启动时不预加载菜单")
    a = ap.parse_args()

    BRIDGE.store_code = a.store
    if not a.no_menu and mcd is not None:
        print("[bridge] 预加载菜单中...", file=sys.stderr)
        BRIDGE.load_menu(a.city, a.keyword)
        print(f"[bridge] 菜单 {len(BRIDGE.catalog)} 款, 门店 {BRIDGE.store_code}",
              file=sys.stderr)

    srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    print(f"[bridge] 监听 http://127.0.0.1:{a.port}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] 已停止", file=sys.stderr)


if __name__ == "__main__":
    main()
