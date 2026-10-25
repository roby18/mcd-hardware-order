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
import urllib.error
import urllib.parse
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _autoload_env():
    """本地调试便利: 关键环境变量未设置时, 尝试从项目外 .workbuddy/credentials-*.env 读取。
    仅本地开发用; 文件缺失或变量已设置则跳过, 不影响部署(部署仍走环境变量)。
    必须在 import mcd_saver 之前调用, 确保麦当劳 Token 就位。"""
    import glob
    here = os.path.dirname(os.path.abspath(__file__))
    want = ("MCD_MCP_TOKEN", "MCD_MCP_ENDPOINT",
            "YUANBAO_API_KEY", "TENCENTCLOUD_WSA_APIKEY")
    for base in (os.path.join(here, "..", "..", ".workbuddy"),
                 os.path.join(here, "..", ".workbuddy"),
                 os.path.join(here, ".workbuddy")):
        for path in glob.glob(os.path.join(base, "credentials-*.env")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip()
                        if k in want and v and k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass


_autoload_env()

# 复用麦当劳 MCP 封装: 仓库内副本优先(克隆即可跑), 其次复用 dine-mcd-saver 开发版
_here = os.path.dirname(os.path.abspath(__file__))
_dine_path = os.path.join(_here, "..", "dine-mcd-saver", "scripts")
if not os.path.exists(os.path.join(_here, "mcd_saver.py")):
    if os.path.isdir(_dine_path):
        sys.path.insert(0, _dine_path)
try:
    import mcd_saver as mcd
except ImportError:
    mcd = None

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
IMG_DIR = os.path.join(WEB_DIR, "img")
os.makedirs(IMG_DIR, exist_ok=True)


def _read_version():
    """界面版本号动态来源: 读仓库根 VERSION 文件, 避免前端写死。"""
    try:
        with open(os.path.join(HERE, "VERSION"), encoding="utf-8") as f:
            return (f.read() or "").strip() or "0.0.0"
    except Exception:
        return "0.0.0"


VERSION = _read_version()


# ---------------------------------------------------------------- 文件日志
def _log_dir():
    """统一日志目录: 优先项目根 logs/ (与 start-all.bat / tail-log.bat 约定一致),
    根目录不可写时退回本目录 logs/。"""
    for cand in (os.path.join(HERE, "..", "..", "logs"), os.path.join(HERE, "logs")):
        try:
            os.makedirs(cand, exist_ok=True)
            probe = os.path.join(cand, ".w")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
            return os.path.abspath(cand)
        except Exception:
            continue
    return None


LOG_DIR = _log_dir()
BRIDGE_LOG = os.path.join(LOG_DIR, "bridge.log") if LOG_DIR else None
LOG_MAX_BYTES = 5 * 1024 * 1024          # 单文件超过 5MB 滚动一个 .1


class _Tee:
    """把 print 同时写到控制台和日志文件(带时间戳), 方便 tail -f 观察。"""

    def __init__(self, stream, path):
        self.stream = stream
        self.path = path

    def write(self, text):
        try:
            self.stream.write(text)
        except Exception:
            pass
        if not text or not self.path:
            return
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > LOG_MAX_BYTES:
                os.replace(self.path, self.path + ".1")
            with open(self.path, "a", encoding="utf-8") as f:
                for line in text.splitlines():
                    if line.strip():
                        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
        except Exception:
            pass

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass


def setup_file_log(tag="bridge"):
    """把 stdout/stderr 双写到日志文件; 返回日志路径(失败返回 None)。"""
    if not BRIDGE_LOG:
        return None
    try:
        with open(BRIDGE_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n===== {tag} 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        sys.stdout = _Tee(sys.__stdout__, BRIDGE_LOG)
        sys.stderr = _Tee(sys.__stderr__, BRIDGE_LOG)
        return BRIDGE_LOG
    except Exception:
        return None


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
        self.menu_cached = False      # True = 当前用的是本地缓存菜单(门店打烊/接口异常)
        self.voice_active = False   # 旋钮按下: 语音交互中
        # 外送链路缓存: 地址列表(60s) + 各外送门店菜单(5min)
        self._addr_cache = []
        self._addr_ts = 0.0
        self._dmenu_cache = {}      # storeCode -> (catalog, ts)
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
        """OpenAI 兼容 chat/completions。返回 (ok, content_or_error)。
        兼容推理模型(答案在 content, 思考在 reasoning_content); content 为空时回退 reasoning_content。
        429/5xx/超时自动重试(指数退避 2s/4s), 应对路演现场偶发限流。"""
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
        last_err = "未知错误"
        for attempt in range(3):  # 第 1 次 + 2 次重试
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Authorization", "Bearer " + self.llm["key"])
            req.add_header("Content-Type", "application/json; charset=utf-8")
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    d = json.loads(r.read().decode("utf-8"))
                msg = (d.get("choices") or [{}])[0].get("message") or {}
                content = (msg.get("content") or "").strip()
                if not content:
                    content = (msg.get("reasoning_content") or "").strip()
                return True, content
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "ignore")[:200]
                except Exception:
                    pass
                if e.code == 429 or 500 <= e.code < 600:
                    last_err = f"接口繁忙(HTTP {e.code})"
                    if attempt < 2:
                        wait = 2 * (2 ** attempt)  # 2s, 4s
                        print(f"[llm] HTTP {e.code}, {wait}s 后第 {attempt + 2} 次重试...")
                        time.sleep(wait)
                        continue
                else:
                    return False, f"大模型调用失败: HTTP {e.code} {detail}"
            except (socket.timeout, TimeoutError):
                last_err = "接口超时"
                if attempt < 2:
                    wait = 2 * (2 ** attempt)
                    print(f"[llm] 超时, {wait}s 后第 {attempt + 2} 次重试...")
                    time.sleep(wait)
                    continue
            except Exception as e:
                return False, f"大模型调用失败: {e}"
        return False, f"{last_err}, 已自动重试 2 次仍失败"

    @staticmethod
    def _extract_codes(content):
        """从 LLM 返回中稳健提取 codes 列表, 返回 (codes, reason)。
        容忍: markdown代码块 / reasoning_content思考杂质 / 多个JSON候选 / 尾随逗号。
        推理模型偶发把思考过程回灌到正文, 贪婪正则会跨匹配到杂质导致解析失败。"""
        candidates = []
        # 1) markdown ```json 代码块优先
        for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.S):
            candidates.append(m.group(1))
        # 2) 花括号配平扫描: 从每个 { 开始, 找到平衡的 } 作为候选
        depth, start = 0, None
        for i, ch in enumerate(content):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}" and depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidates.append(content[start:i + 1])
                    start = None
        for raw in reversed(candidates):  # 最后的完整对象优先, 避免采纳思考中的格式示例
            for text in (raw, re.sub(r",\s*([}\]])", r"\1", raw)):  # 容忍尾随逗号
                try:
                    d = json.loads(text)
                except Exception:
                    continue
                if isinstance(d, dict) and isinstance(d.get("codes"), list):
                    return d["codes"], str(d.get("reason", ""))
        return None, None

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
        # 推理模型的思考也占用 max_tokens, 给足预算(实测约需 1100+)
        ok, content = self.llm_chat(
            [{"role": "user", "content": prompt}], temperature=0.3, max_tokens=2000)
        if not ok:
            return {"error": content, "fallback": True}
        # 解析 LLM 返回的 JSON(容忍前后杂质)
        print(f"[llm_plan] LLM 原始返回({len(content)}字): {content[:400]}", flush=True)
        picked, reason = self._extract_codes(content)
        if not picked:
            return {"error": "大模型返回无法解析", "fallback": True}
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
        # 预算门禁: LLM 只管"选什么", 超预算的组合一律不采纳(与本地算法对齐)
        if r["payable"] > self.budget + 0.5:
            return {"error": f"大模型选餐超预算({r['payable']:.0f}>{self.budget:.0f}元)",
                    "fallback": True}
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
        if reason:
            r["reason"] = reason
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
                "menuCached": self.menu_cached,
                "coupons": self.coupons,
                "price": {
                    "original": pr.get("original", 0.0),
                    "payable": pr.get("payable", 0.0),
                    "discount": pr.get("discount", 0.0),
                    "balance": pr.get("balance", 0.0),
                    "next": pr.get("next_discount"),
                    "takeWayCode": pr.get("takeWayCode", ""),
                },
                "log": self.log[-12:],
                "voice": self.voice_active,
                "keymap": dict(self.keymap),
                "engine": "llm" if self.llm_ready() else "local",
                "version": VERSION,
                "ts": time.time(),
            }

    def push_log(self, text):
        with self.lock:
            self.log.append({"t": time.strftime("%H:%M:%S"), "text": text})
            if len(self.log) > 60:
                self.log = self.log[-60:]
        # 同步落到日志文件(tail -f 可实时看演示流程: 加菜/算价/下单)
        try:
            print(f"[事件] {text}", file=sys.stderr, flush=True)
        except Exception:
            pass

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
    def yb_search(self, keywords, round_no=1, industry="", freshness="", cnt=0, site=""):
        """单次元宝搜索, 返回结构化结果 + 证据信息(RequestId/时间戳)。
        每次查询都留痕, 供侦探赛道截图与证据链报告使用。
        支持元宝特有参数: Industry(gov/news/acad/finance 垂直领域)、
        Freshness(d/m/y 时效)、Cnt(结果条数)、Site(指定/排除站点)。"""
        import urllib.request
        import urllib.error
        keywords = (keywords or "").strip()
        if not keywords:
            return {"error": "关键词不能为空"}
        api_key = os.environ.get("YUANBAO_API_KEY") or os.environ.get("TENCENTCLOUD_WSA_APIKEY")
        if not api_key:
            return {"error": "未配置 YUANBAO_API_KEY 环境变量"}
        params = {"Query": keywords}
        if industry:   params["Industry"] = industry
        if freshness:  params["Freshness"] = freshness
        if cnt:        params["Cnt"] = int(cnt)
        if site:       params["Site"] = site
        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
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
            passage = " ".join((item.get("passage") or "").split())
            if not passage:
                passage = " ".join((item.get("content") or "").split())
            pics = []
            for pic in (item.get("pics") or [])[:4]:
                if not isinstance(pic, dict):
                    continue
                u = (pic.get("origin_url") or "").strip()
                if u:
                    pics.append({"caption": (pic.get("caption") or "").strip(), "url": u})
            pages.append({
                "title": (item.get("title") or "").strip(),
                "passage": passage,
                "url": (item.get("url") or "").strip(),
                "site": (item.get("site") or "").strip(),
                "date": (item.get("date") or "").strip(),
                "score": item.get("score"),
                "pics": pics,
            })
        elapsed = round(time.time() - started, 2)
        self.push_log(f"[侦探] 第{round_no}轮「{keywords}」-> {len(pages)} 条 / {elapsed}s")
        return {
            "round": round_no,
            "keywords": keywords,
            "industry": industry,
            "freshness": freshness,
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
    MENU_CACHE = os.path.join(HERE, "config", "menu_cache.json")

    def _save_menu_cache(self):
        """菜单拉取成功后落盘, 供门店打烊/断网时回退, 保证演示不白屏。"""
        try:
            os.makedirs(os.path.dirname(self.MENU_CACHE), exist_ok=True)
            with open(self.MENU_CACHE, "w", encoding="utf-8") as f:
                json.dump({
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "storeCode": self.store_code,
                    "storeInfo": self.store_info,
                    "catalog": self.catalog,
                    "coupons": self.coupons,
                }, f, ensure_ascii=False)
        except Exception as e:
            print(f"[cache] 菜单缓存写入失败: {e}")

    def _load_menu_cache(self):
        try:
            if not os.path.exists(self.MENU_CACHE):
                return False
            with open(self.MENU_CACHE, encoding="utf-8") as f:
                d = json.load(f)
            if not d.get("catalog"):
                return False
            self.catalog = d["catalog"]
            self.coupons = d.get("coupons") or []
            self.store_code = d.get("storeCode") or self.store_code
            self.store_info = d.get("storeInfo") or self.store_info
            self.menu_loaded = True
            self.menu_cached = True
            self.push_log(f"门店无在售菜单, 已回退缓存(落盘于 {d.get('ts','?')})")
            return True
        except Exception as e:
            print(f"[cache] 菜单缓存读取失败: {e}")
            return False

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
            if not self.catalog:      # 门店打烊时接口返回空列表(不抛异常)
                self.menu_loaded = False
                if self._load_menu_cache():
                    return True
                return False
            self._save_menu_cache()
        except Exception as e:
            self.push_log(f"菜单加载失败: {e}")
            if self._load_menu_cache():
                return True
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
            takeways = data.get("takeWayList") or data.get("takewayList") or []
            take_way_code = ""
            if takeways and isinstance(takeways[0], dict):
                take_way_code = str(takeways[0].get("code") or takeways[0].get("takeWayCode") or "")
            self.price_result = {
                "original": mcd.fen(data.get("originalPrice") or data.get("productOriginalPrice")),
                "payable": mcd.fen(data.get("price") or data.get("productPrice")),
                "discount": mcd.fen(data.get("discount")),
                "balance": mcd.fen((data.get("enjoyable") or {}).get("balance")),
                "next_discount": _fmt_next(data.get("enjoyable")),
                "takeWayCode": take_way_code,
            }
        except Exception as e:
            self.push_log(f"算价失败: {e}")

    def create_real_order(self, need_tableware=False, order_type=1, address_id=""):
        """通过麦当劳 MCP 创建真实订单（不会自动支付）。
        order_type=1 到店自取 / order_type=2 外送(需 address_id)。"""
        if mcd is None:
            return {"error": "mcd_saver 未加载"}
        with self.lock:
            if not self.cart:
                return {"error": "餐盘为空"}
            price = dict(self.price_result or {})
        try:
            if int(order_type) == 2:
                return self._create_delivery_order(address_id, need_tableware, price)
            items = [{"productCode": code, "quantity": int(entry.get("qty", 1))}
                     for code, entry in self.cart.items()]
            # 下单前强制重新算价，避免使用过期价格或取餐方式。
            self.recalc()
            price = dict(self.price_result or price)
            take_way = str(price.get("takeWayCode") or "")
            if not take_way:
                return {"error": "未获取到到店取餐方式，请先刷新菜单并重新算价"}
            args = {
                "storeCode": self.store_code,
                "orderType": 1,
                "beType": 1,
                "takeWayCode": take_way,
                "items": items,
                "needTableware": bool(need_tableware),
            }
            data = mcd.call("create-order", args) or {}
            self.push_log(f"已创建真实到店订单: {data.get('orderId', '已提交')}")
            return {"success": True, "order": data, "price": price,
                    "orderType": 1, "store": self.store_info}
        except Exception as e:
            self.push_log(f"真实下单失败: {e}")
            return {"error": _short_err(e)}

    # ------------------------------------------------ 外送(麦当劳 MCP delivery-*)
    def delivery_addresses(self):
        """收货地址列表 (MCP delivery-query-addresses), 60s 缓存。"""
        if mcd is None:
            return {"error": "mcd_saver 未加载"}
        now = time.time()
        with self.lock:
            if self._addr_cache and now - self._addr_ts < 60:
                return {"addresses": list(self._addr_cache)}
        try:
            data = mcd.call("delivery-query-addresses", {}) or {}
        except Exception as e:
            return {"error": _short_err(e)}
        out = []
        for a in (data.get("addresses") or []):
            if not isinstance(a, dict):
                continue
            out.append({
                "addressId": str(a.get("addressId") or ""),
                "contactName": a.get("contactName") or a.get("name") or "",
                "phone": a.get("phone") or a.get("mobilePhone") or "",
                "fullAddress": a.get("fullAddress") or a.get("address") or "",
                "isDefault": bool(a.get("isDefault") or a.get("defaultAddress")),
            })
        with self.lock:
            self._addr_cache, self._addr_ts = out, now
        return {"addresses": out}

    def delivery_store(self, address_id):
        """按收货地址查可外送门店 (MCP delivery-query-stores), 取第一家营业中门店。"""
        if mcd is None:
            return {"error": "mcd_saver 未加载"}
        address_id = str(address_id or "").strip()
        if not address_id:
            return {"error": "缺少 addressId"}
        try:
            data = mcd.call("delivery-query-stores",
                            {"addressId": address_id, "beType": 2}) or {}
        except Exception as e:
            return {"error": _short_err(e)}
        stores = data if isinstance(data, list) else (data.get("stores") or [])
        picked = None
        for s in stores:
            if isinstance(s, dict) and s.get("businessStatus"):
                picked = s
                break
        if picked is None and stores and isinstance(stores[0], dict):
            picked = stores[0]
        if picked is None:
            return {"error": "该地址附近没有可外送的门店"}
        return {"store": {
            "storeCode": str(picked.get("storeCode") or ""),
            "beCode": str(picked.get("beCode") or ""),
            "storeName": picked.get("storeName") or "",
            "address": picked.get("address") or "",
        }}

    def _delivery_catalog(self, store_code, be_code):
        """外送门店菜单 (query-meals orderType=2/beType=2/beCode)。
        实测返回的 meals 是扁平 dict(key=商品码), 摊平成 catalog 结构, 按门店缓存 5 分钟。
        出错时返回 {"error": ...}(调用方需判 isinstance(res, dict) and res.get("error"))。"""
        key = str(store_code)
        now = time.time()
        with self.lock:
            hit = self._dmenu_cache.get(key)
            if hit and now - hit[1] < 300:
                return hit[0]
        try:
            data = mcd.call("query-meals", {
                "storeCode": key, "orderType": 2, "beType": 2,
                "beCode": str(be_code or ""),
            }) or {}
        except Exception as e:
            return {"error": _short_err(e)}
        meals = data.get("meals") if isinstance(data, dict) else data
        pairs = []
        if isinstance(meals, dict):
            pairs = list(meals.items())
        elif isinstance(meals, list):
            pairs = [(m.get("productCode") or m.get("code"), m)
                     for m in meals if isinstance(m, dict)]
        cat = []
        for code, m in pairs:
            if not code or not isinstance(m, dict):
                continue
            cat.append({
                "code": str(code),
                "name": m.get("name") or "",
                "price": _money(m.get("currentPrice")),
                "original": _money(m.get("originalPrice")),
                "image": m.get("image") or "",
                "role": "other", "cat": "外送",
            })
        with self.lock:
            self._dmenu_cache[key] = (cat, now)
        return cat

    def match_delivery(self, name, dcat):
        """把餐盘餐品名匹配到外送门店菜单: 归一化精确 -> 核心词包含, 取重合最多。
        算法与 match_by_name(957-981 行)一致, 只是匹配对象换成传入的 dcat。"""
        q = _norm_name(name)
        if not q:
            return None
        for c in dcat:
            if _norm_name(c.get("name", "")) == q:
                return c
        qc = _core_name(q)
        best, best_score = None, 0
        for c in dcat:
            n = _norm_name(c.get("name", ""))
            nc = _core_name(n)
            score = 0
            if qc and nc and (qc in nc or nc in qc):
                score = min(len(qc), len(nc)) + 1
            elif q and n and (q in n or n in q):
                score = min(len(q), len(n))
            if score > best_score:
                best_score, best = score, c
        return best if best_score >= 2 else None

    def order_preview(self, order_type=1, address_id=""):
        """下单前预览: 到店(当前门店算价) / 外送(按地址选门店+同名换码+含配送费算价)。
        供前端弹窗实时展示, 不产生真实订单。"""
        if mcd is None:
            return {"error": "mcd_saver 未加载"}
        with self.lock:
            if not self.cart:
                return {"error": "餐盘为空"}
        if int(order_type) != 2:
            return {"success": True, "orderType": 1,
                    "store": self.store_info,
                    "price": self.price_result or {},
                    "missing": []}
        addr = self.delivery_addresses()
        if addr.get("error"):
            return addr
        addresses = addr.get("addresses") or []
        if not addresses:
            return {"error": "账号里还没有收货地址, 请先在麦当劳 APP 添加"}
        picked = None
        for a in addresses:
            if str(a.get("addressId")) == str(address_id or ""):
                picked = a
                break
        if picked is None:
            for a in addresses:
                if a.get("isDefault"):
                    picked = a
                    break
        if picked is None:
            picked = addresses[0]
        st = self.delivery_store(picked["addressId"])
        if st.get("error"):
            return st
        store = st["store"]
        dcat = self._delivery_catalog(store["storeCode"], store.get("beCode", ""))
        if isinstance(dcat, dict) and dcat.get("error"):
            return dcat
        items, missing = [], []
        with self.lock:
            for code, e in self.cart.items():
                hit = self.match_delivery(e["item"].get("name", ""), dcat)
                if hit:
                    items.append({"productCode": hit["code"],
                                  "quantity": int(e.get("qty", 1))})
                else:
                    missing.append(e["item"].get("name", code))
        if not items:
            return {"error": "外送门店没有可售的对应餐品: " + "、".join(missing[:5])}
        try:
            data = mcd.call("calculate-price", {
                "storeCode": store["storeCode"], "orderType": 2, "beType": 2,
                "beCode": store.get("beCode", ""), "addressId": picked["addressId"],
                "items": items,
            }) or {}
        except Exception as e:
            return {"error": "外送算价失败: " + _short_err(e)}
        price = {
            "original": mcd.fen(data.get("originalPrice") or data.get("productOriginalPrice")),
            "payable": mcd.fen(data.get("price") or data.get("productPrice")),
            "discount": mcd.fen(data.get("discount")),
            "deliveryFee": mcd.fen(data.get("deliveryPrice") or data.get("realDeliveryPrice")
                                   or data.get("deliveryFee")),
            "balance": mcd.fen((data.get("enjoyable") or {}).get("balance")),
        }
        return {"success": True, "orderType": 2, "store": store,
                "address": picked, "addresses": addresses,
                "price": price, "missing": missing}

    def _create_delivery_order(self, address_id, need_tableware, price):
        """外送真实下单: 复用 order_preview 选门店/换码/算价, 再 create-order(orderType=2)。"""
        prev = self.order_preview(2, address_id)
        if prev.get("error"):
            return prev
        store = prev["store"]
        addr = prev["address"]
        dcat = self._delivery_catalog(store["storeCode"], store.get("beCode", ""))
        items = []
        with self.lock:
            for code, e in self.cart.items():
                hit = self.match_delivery(e["item"].get("name", ""), dcat)
                if hit:
                    items.append({"productCode": hit["code"],
                                  "quantity": int(e.get("qty", 1))})
        if not items:
            return {"error": "外送门店无可售商品"}
        args = {
            "storeCode": store["storeCode"],
            "orderType": 2, "beType": 2,
            "beCode": store.get("beCode", ""),
            "addressId": addr["addressId"],
            "items": items,
            "needTableware": bool(need_tableware),
        }
        data = mcd.call("create-order", args) or {}
        self.push_log(f"已创建真实外送订单: {data.get('orderId', '已提交')} · "
                      f"{store.get('storeName', '')}")
        return {"success": True, "order": data,
                "price": prev.get("price") or price,
                "orderType": 2, "store": store, "address": addr,
                "missing": prev.get("missing") or []}

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

    # ------------------------------------------------ 真实历史订单 (MCP)
    orders_cache = {"ts": 0.0, "data": None}

    # ------------------------------------------------ 卡包优惠券 (MCP)
    coupons_cache = {"ts": 0.0, "data": None}

    def my_coupons(self, force=False):
        """
        麦当劳账号卡包里的真实优惠券 (MCP query-my-coupons)。
        返回可用/已用完两种状态 + 官方券图 + 有效期 + 使用标签。
        """
        now = time.time()
        cached = self.coupons_cache["data"]
        if (not force) and cached is not None and now - self.coupons_cache["ts"] < 60:
            return dict(cached, cached=True)
        if mcd is None:
            return {"error": "mcd_saver 未加载"}
        try:
            data = mcd.call("query-my-coupons", {"page": "1", "pageSize": "50"}) or {}
        except Exception as e:
            return {"error": _short_err(e)}
        out = []
        for c in data.get("coupons") or []:
            di = c.get("discountInfo") or {}
            instr = c.get("instructions") or {}
            avail = []
            for a in instr.get("availableTime") or []:
                avail.append(a.get("text", ""))
            tags = [t.get("label", "") for t in (c.get("tags") or [])]
            # 是否"现在就能用": 今日可用次数>0 且 券处于启用态。
            # totalAvailableCount>0 只说明券还没用完(可能未到使用时间), 不能算"现在可用"。
            available = (int(c.get("currentDayAvailableCount") or 0) > 0
                         and int(c.get("enable") or 0) == 1)
            out.append({
                "id": c.get("id", ""),
                "title": c.get("title", ""),
                "subtitle": c.get("subtitle", ""),
                "image": c.get("image", "") or c.get("bigImage", ""),
                "value": di.get("discountValue", ""),
                "desc": di.get("discountDesc", ""),
                "available": available,
                "totalCount": c.get("totalCount", 0),
                "availText": " ".join(avail),
                "tradeStart": c.get("tradeStartDate", ""),
                "tradeEnd": c.get("tradeEndDate", ""),
                "tags": tags,
                "enable": c.get("enable", 0),
            })
        claimable = []
        try:
            for c in mcd.fetch_available_coupons():
                if str(c.get("couponStatus", "")).upper() == "CAN_GET":
                    claimable.append({"name": c.get("couponName", ""),
                                      "image": c.get("couponImage", ""),
                                      "label": c.get("label", "可领取")})
        except Exception:
            pass
        res = {"coupons": out, "total": len(out), "claimable": claimable, "cached": False}
        Bridge.coupons_cache = {"ts": now, "data": res}
        return res

    def bind_coupons(self):
        """一键领取当前账号可领取的麦麦省优惠券。"""
        if mcd is None:
            return {"error": "mcd_saver 未加载"}
        try:
            d = mcd.call("auto-bind-coupons", {}) or {}
            self.coupons_cache = {"ts": 0.0, "data": None}
            ok = d.get("successCoupons") or []
            failed = d.get("failedCoupons") or []
            self.push_log(f"优惠券领取: 成功 {len(ok)} 张, 失败 {len(failed)} 张")
            return {"success": [{"name": x.get("couponName", ""), "image": x.get("couponImage", "")}
                                for x in ok],
                    "failed": [{"name": x.get("couponName", ""), "reason": x.get("reason", "")}
                               for x in failed],
                    "successCount": len(ok), "failedCount": len(failed)}
        except Exception as e:
            return {"error": _short_err(e)}

    def orders(self, force=False):
        """
        麦当劳账号的真实历史订单 (MCP order-list, 非商城订单)。
        order-list 每次约 1-2 秒且接口有限流, 这里做 45 秒缓存。
        """
        now = time.time()
        cached = self.orders_cache["data"]
        if (not force) and cached is not None and now - self.orders_cache["ts"] < 45:
            return dict(cached, cached=True)
        if mcd is None:
            return {"error": "mcd_saver 未加载"}
        try:
            data = mcd.call("order-list", {}) or {}
        except Exception as e:
            return {"error": _short_err(e)}
        out = []
        for o in data.get("list") or []:
            items = []
            for p in o.get("orderProductList") or []:
                items.append({
                    "name": p.get("productName", ""),
                    "code": str(p.get("productCode", "")),
                    "qty": int(p.get("quantity") or 1),
                    "subs": [{"name": s.get("name", ""), "qty": int(s.get("quantity") or 1)}
                             for s in (p.get("comboItemList") or [])],
                })
            out.append({
                "orderId": str(o.get("orderId", "")),
                "store": o.get("storeName", ""),
                "storeCode": str(o.get("storeCode", "")),
                "time": o.get("createTime", ""),
                "status": o.get("orderStatus", ""),
                "type": "外送" if str(o.get("orderType")) == "2" else "到店",
                "amount": _money(o.get("realTotalAmount")),
                "count": sum(i["qty"] for i in items),
                "items": items,
            })
        res = {"orders": out, "total": len(out), "cached": False}
        Bridge.orders_cache = {"ts": now, "data": res}
        return res

    def order_detail(self, order_id):
        """单个订单详情 (MCP query-order): 官方商品图/优惠明细/取餐码/配送费。"""
        order_id = (order_id or "").strip()
        if not order_id:
            return {"error": "缺少 orderId"}
        if mcd is None:
            return {"error": "mcd_saver 未加载"}
        try:
            d = mcd.call("query-order", {"orderId": order_id}) or {}
        except Exception as e:
            return {"error": _short_err(e)}
        if not isinstance(d, dict) or not d:
            return {"error": "未查到该订单"}
        items = []
        for p in d.get("orderProductList") or []:
            items.append({
                "name": p.get("productName", ""),
                "code": str(p.get("productCode", "")),
                "img": p.get("productImage", ""),
                "qty": int(p.get("quantity") or 1),
                "price": _money(p.get("price")),
                "subs": [{"name": s.get("itemName", ""), "qty": int(s.get("itemQuantity") or 1)}
                         for s in (p.get("comboItemList") or [])],
            })
        # 外送订单的收货地址: 实测结构是顶层 deliveryInfo.deliveryAddress / addressDetail /
        # customerNickname / mobilePhone (deliveryInfo.deliveryType=立即送出)。
        # 不同账号/接口版本字段名可能有出入, 所以这里对 deliveryInfo 内做多字段兜底。
        info = d.get("deliveryInfo") if isinstance(d.get("deliveryInfo"), dict) else {}
        delivery_address = (info.get("deliveryAddress") or info.get("fullAddress") or
                            info.get("address") or info.get("addressDetail") or "")
        detail = info.get("addressDetail") or ""
        if detail and detail not in delivery_address:
            delivery_address = (delivery_address + " " + detail).strip()
        delivery_name = info.get("customerNickname") or info.get("name") or ""
        delivery_phone = info.get("mobilePhone") or info.get("phone") or ""
        return {
            "orderId": d.get("orderId", order_id),
            "status": d.get("orderStatus", ""),
            "store": d.get("storeName", ""),
            "address": d.get("storeAddress", ""),
            "deliveryType": info.get("deliveryType", ""),
            "deliveryAddress": delivery_address,
            "deliveryName": delivery_name,
            "deliveryPhone": delivery_phone,
            "time": d.get("createTime", ""),
            "takeWay": d.get("takeWay", ""),
            "pickupCode": d.get("pickupCode", ""),
            "productPrice": _money(d.get("productPrice")),
            "deliveryPrice": _money(d.get("realDeliveryPrice") or d.get("deliveryPrice")),
            "packingFee": _money(d.get("realPackingFeeTotalPrice")),
            "discount": _money(d.get("totalDiscountAmount")),
            "payable": _money(d.get("realTotalAmount")),
            "coupons": [{"name": c.get("couponName", ""), "amount": _money(c.get("discountAmount"))}
                        for c in (d.get("couponList") or [])],
            "items": items,
        }

    def match_exact(self, name):
        """当前门店菜单里是否存在同名餐品(归一化后完全相等)。"""
        q = _norm_name(name)
        if not q:
            return None
        for c in self.catalog:
            if _norm_name(c.get("name", "")) == q:
                return c
        return None

    def match_by_name(self, name):
        """
        按餐品名在当前门店菜单里找最接近的一款。
        历史订单可能来自别的门店/别的时期(如「麦咖啡™热奶铁中杯」),
        所以先精确、再去品牌与杯型温度后包含匹配, 取重合字数最多的一款。
        """
        q = _norm_name(name)
        if not q:
            return None
        hit = self.match_exact(name)
        if hit:
            return hit
        qc = _core_name(q)
        best, best_score = None, 0
        for c in self.catalog:
            n = _norm_name(c.get("name", ""))
            nc = _core_name(n)
            score = 0
            if qc and nc and (qc in nc or nc in qc):
                score = min(len(qc), len(nc)) + 1     # 核心词命中优先
            elif q and n and (q in n or n in q):
                score = min(len(q), len(n))
            if score > best_score:
                best_score, best = score, c
        return best if best_score >= 2 else None      # 至少重合 2 个字才认

    def reorder(self, order_id):
        """历史订单一键再来: 同名餐品直接加, 没有就拆成套餐子项逐个匹配。"""
        det = self.order_detail(order_id)
        if det.get("error"):
            return det
        if not self.catalog:
            return {"error": "当前门店菜单还没加载完"}
        added, missing = [], []
        for p in det["items"]:
            qty = int(p.get("qty") or 1)
            picks = []
            whole = self.match_exact(p["name"])
            if whole:
                picks = [whole]                       # 门店有同名餐品/同名套餐
            elif p.get("subs"):                       # 没有同名套餐 -> 拆子项
                for s in p["subs"]:
                    h = self.match_by_name(s["name"])
                    if h and h["code"] not in [x["code"] for x in picks]:
                        picks.append(h)
            else:
                h = self.match_by_name(p["name"])     # 单品: 模糊匹配
                if h:
                    picks = [h]
            if not picks:
                missing.append(p["name"])
                continue
            for h in picks:
                self.add(h["code"], qty)
                added.append(h["name"])
            if len(picks) > 1:
                self.push_log(f"「{p['name']}」当前门店无同名套餐, 已拆为单品加入")
        self.push_log(f"再来一单: 加入 {len(added)} 样"
                      + (f", {len(missing)} 样当前门店没有" if missing else ""))
        return {"added": added, "missing": missing,
                "store": det.get("store", ""), "orderId": det.get("orderId", "")}

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
                lr = self._plan_local()
                lr["note"] = "大模型不可用, 已回退本地算法(" + str(r.get("error", ""))[:60] + ")"
                return lr
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


def _money(v):
    """MCP 金额字段有时是字符串('65.8')有时是数字, 统一成 float 元。"""
    if v is None or v == "":
        return 0.0
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0.0


def _short_err(e):
    return f"{type(e).__name__}: {e}"[:200]


def _norm_name(s):
    """餐品名归一化: 去商标/空格/括号, 便于跨门店匹配同名餐品。"""
    s = (s or "").replace("™", "").replace("®", "").replace(" ", "")
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    return s.strip()


# 品牌前缀与杯型/温度后缀: 不同门店、不同时期叫法不一样, 匹配时先剥掉
_CORE_STRIP = ("麦咖啡", "中杯", "大杯", "小杯", "热", "冰", "冷")


def _core_name(s):
    c = s or ""
    for w in _CORE_STRIP:
        c = c.replace(w, "")
    return c


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

        # 根路径默认新版(深色 stage); 旧版留档在 /classic, /index.html 也仍可访问
        if path == "/" or path == "/stage" or path == "/index.html" or path == "/classic":
            name = "index.html" if path in ("/index.html", "/classic") else "stage.html"
            p = os.path.join(WEB_DIR, name)
            if not os.path.exists(p) and name == "stage.html":
                p = os.path.join(WEB_DIR, "index.html")   # 新版缺失时回退旧版
            if os.path.exists(p):
                with open(p, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            else:
                self._send(404, name + " 不存在")
            return

        if path == "/radial":
            p = os.path.join(WEB_DIR, "radial.html")
            if os.path.exists(p):
                with open(p, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            else:
                self._send(404, "radial.html 不存在")
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
                        "engine": "llm" if BRIDGE.llm_ready() else "local",
                        "version": VERSION})
            return

        if path == "/api/menu":
            self._json({"catalog": BRIDGE.catalog, "coupons": BRIDGE.coupons})
            return

        # 麦当劳账号真实历史订单 (MCP order-list)
        if path == "/api/orders":
            force = "force" in (self.path.split("?")[1] if "?" in self.path else "")
            self._json(BRIDGE.orders(force=force))
            return

        # 卡包真实优惠券 (MCP query-my-coupons)
        if path == "/api/coupons":
            force = "force" in (self.path.split("?")[1] if "?" in self.path else "")
            self._json(BRIDGE.my_coupons(force=force))
            return

        # 单个订单详情 (MCP query-order)
        if path == "/api/order":
            q = urllib.parse.parse_qs(self.path.split("?")[1] if "?" in self.path else "")
            self._json(BRIDGE.order_detail((q.get("orderId") or [""])[0]))
            return

        if path == "/api/events":
            self._sse()
            return

        # 静态素材: /assets/<file> (生成的托盘底图等, 随仓库提交)
        if path.startswith("/assets/"):
            name = os.path.basename(path[len("/assets/"):])
            p = os.path.join(WEB_DIR, "assets", name)
            if name and os.path.exists(p):
                ext = os.path.splitext(name)[1].lower()
                ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                         ".webp": "image/webp", ".svg": "image/svg+xml"}.get(
                    ext, "application/octet-stream")
                with open(p, "rb") as f:
                    self._send(200, f.read(), ctype)
            else:
                self._json({"error": "asset not found"}, 404)
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

        if path == "/api/reorder":
            # 历史订单一键再来: 按餐品名匹配当前门店菜单后放回餐盘
            self._json(BRIDGE.reorder(b.get("orderId", "")))
            return

        if path in ("/api/create-order", "/api/order/create"):
            # 真实下单：前端必须先完成二次确认；接口不会自动支付。
            if not b.get("confirmed"):
                self._json({"error": "需要确认后才能创建真实订单"}, 400)
                return
            self._json(BRIDGE.create_real_order(
                bool(b.get("needTableware", False)),
                int(b.get("orderType", 1) or 1),
                str(b.get("addressId", "") or "")))
            return

        if path == "/api/order-preview":
            # 下单前预览: 到店(当前门店算价) / 外送(按地址选门店+同名换码+含配送费算价)
            self._json(BRIDGE.order_preview(int(b.get("orderType", 1) or 1),
                                            str(b.get("addressId", "") or "")))
            return

        if path == "/api/coupons/bind":
            self._json(BRIDGE.bind_coupons())
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
                # 测试连通: 推理模型思考也占 token, 必须给足 max_tokens 才能拿到正文
                ok, msg = BRIDGE.llm_chat(
                    [{"role": "user", "content": "请只回复四个字: 连接正常"}],
                    max_tokens=2000)
                empty = ok and not (msg or "").strip()
                self._json({"ok": ok and not empty,
                            "msg": ((msg or "").strip()[:120] if not empty
                                    else "模型返回为空(可能是推理模型或额度不足)")})
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
            # 侦探赛道: 元宝搜索单次查询(带证据信息 + 垂直领域/时效/条数)
            r = BRIDGE.yb_search(
                b.get("keywords", ""), b.get("round", 1),
                industry=b.get("industry", ""),
                freshness=b.get("freshness", ""),
                cnt=b.get("cnt", 0),
                site=b.get("site", ""),
            )
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

    log_path = setup_file_log("bridge")
    if log_path:
        print(f"[bridge] 日志文件: {log_path}", file=sys.stderr)

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
