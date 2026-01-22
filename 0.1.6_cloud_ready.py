# tv_okx_micro_trade_server.py
# 固定 6 幣（SOL + 5 小幣）多幣反手面板（逐倉 isolated）
# - UI 顯示「商品最大槓桿」（SOL=50；小幣由 TXT）
# - 實際下單前用 account/leverage-info 查「帳戶此刻可設上限」並 clamp，避免爆單
# - 每幣可獨立調整 槓桿 / 本金%（所有幣本金%總和 <= 100%）
# - 調整時即時計算「預估保證金 / 名目價值 / 張數」（用 mark price）
# - TV Webhook: {"symbol":"{{ticker}}","action":"BUY"/"SELL"}

import base64
import hashlib
import hmac
import json
import math
import threading
import time
import uuid
import os
import argparse
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, List

import requests
from flask import Flask, request, jsonify

# UI 只在本機模式使用；雲端（Render/VPS）可用 --headless 跳過 UI
try:
    import tkinter as tk
    from tkinter import ttk, messagebox
    TK_AVAILABLE = True
except Exception:
    tk = None
    ttk = None
    messagebox = None
    TK_AVAILABLE = False


# =========================
# 基本設定
# =========================
HOST = "127.0.0.1"
PORT = 9001

# =========================
# Follower（遠端跟單）設定
# 0.1.4 穩定版 + 只新增『送出 JSON』功能（不動 UI）
# ※ 改為「每個 follower 一把金鑰」：Server 送出的是『加密密文』，中途看到也無法得知內容
# =========================
ENABLE_FOLLOWER = True
FOLLOWER_TIMEOUT_SEC = 2

# 你可以在這裡加多個 follower：每個人一個 url + 一把 key（base64）
# key 產生方式（在你這台 server 跑一次）：
#   python -c "from cryptography.hazmat.primitives.ciphers.aead import AESGCM; import base64; print(base64.b64encode(AESGCM.generate_key(bit_length=256)).decode())"
FOLLOWERS = [
    {
        "name": "f1",
        "url": "https://brigitte-interjugal-aleisha.ngrok-free.dev",
        "key_b64": "LVOWQYneUe6fMFM/45c3LmTnfEnLrss/gymI+AEyS/A=",
    },
    # {
    #     "name": "f3",
    #     "url": "https://xxxxx.ngrok-free.dev",
    #     "key_b64": "XXX",
    # },
    # {
    #     "name": "f4",
    #     "url": "https://xxxxx.ngrok-free.dev",
    #     "key_b64": "XXX",
    # },

    # {
    #     "name": "f5",
    #     "url": "https://xxxxx.ngrok-free.dev",
    #     "key_b64": "XXX",
    # },
    # {
    #     "name": "f6",
    #     "url": "https://xxxxx.ngrok-free.dev",
    #     "key_b64": "XXX",
    # },
]

def _encrypt_for_follower(plain_obj: dict, key_b64: str) -> dict:
    key = base64.b64decode(key_b64.encode("ascii"))
    aes = AESGCM(key)
    nonce = os.urandom(12)
    plaintext = json.dumps(plain_obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ciphertext = aes.encrypt(nonce, plaintext, None)
    return {
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
    }

def _send_to_follower_async(inst_id: str, side: str, capital_pct: int, leverage: int, reduce_only: bool = False):
    """非阻塞送出跟單指令：永遠不影響本地交易與 UI
    - inst_id: 例如 SOL-USDT-SWAP
    - side: buy/sell
    - capital_pct/leverage: 用於 follower 端依自身 equity 計算張數
    """
    if not ENABLE_FOLLOWER or not FOLLOWERS:
        return

    order_id = str(uuid.uuid4())
    ts = int(time.time())

    # 明文只存在 server 記憶體中；送出去的是密文
    plain = {
        "order_id": order_id,
        "ts": ts,
        "instId": inst_id,
        "side": side,
        "capital_pct": int(capital_pct),
        "leverage": int(leverage),
        "reduceOnly": bool(reduce_only),
    }

    def _worker(one_f: dict):
        try:
            enc = _encrypt_for_follower(plain, one_f["key_b64"])
            payload = {
                "order_id": order_id,
                "ts": ts,
                "ciphertext": enc["ciphertext"],
                "nonce": enc["nonce"],
            }
            r = requests.post(one_f["url"].rstrip("/") + "/execute", json=payload, timeout=FOLLOWER_TIMEOUT_SEC)

            # follower 可能回：{ok:false,msg:'contracts too small'}，這裡只記錄，不影響本地
            try:
                j = r.json()
            except Exception:
                j = {"raw": (r.text or "")[:120]}

            # 只做事件提示，避免刷屏
            pce = globals().get("push_coin_event")
            if callable(pce):
                ok = j.get("ok", None)
                msg = j.get("msg") or ""
                name = one_f.get("name") or "follower"
                pce(inst_id.split("-")[0], f"[Follower:{name}] HTTP {r.status_code} ok={ok} {msg}")

        except Exception as e:
            pce = globals().get("push_coin_event")
            if callable(pce):
                name = one_f.get("name") or "follower"
                pce(inst_id.split("-")[0], f"[Follower:{name}] 送出失敗：{e}")

    for one_f in FOLLOWERS:
        th = threading.Thread(target=_worker, args=(one_f,), daemon=True)
        th.start()

TD_MODE = "isolated"  # 逐倉

KEY_FILE = "OKX API 0.3.txt"
SPEC_FILE = "okx_swaps_spec.txt"
LEV_PROFILE_FILE = "okx_smallcoins_leverage.txt"

MAIN_COIN = "SOL"
SOL_PRODUCT_MAX_LEV = 50  # UI 顯示用（永遠 50）

API_SLEEP = 0.05
REVERSE_CLOSE_WAIT = 0.20
UI_REFRESH_MS = 700

CAP_MIN = 0
CAP_MAX = 100
CAP_STEP = 5

# 允許最小到 0（0 代表該幣不出手 / 不使用資金）
LEV_MIN = 0

# 槓桿嘗試策略（方案 B）：UI 起點，被拒就 -2 再送，直到成功或放棄
LEV_RETRY_STEP = 2
LEV_RETRY_MAX_ATTEMPTS = 8
LEV_RETRY_SLEEP = 0.35
LEV_RETRY_COOLDOWN = 20.0


def lev_step_for(product_max_lev: int) -> int:
    return 1 if int(product_max_lev) < 10 else 5

# =========================
# 工具
# =========================
def now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")

def f(x, d=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return d

def round_down(x: float, step: float) -> float:
    if step <= 0:
        return x
    return math.floor(x / step) * step

def tv_symbol_to_base(symbol: str) -> str:
    ss = (symbol or "").upper().strip()
    if ":" in ss:
        ss = ss.split(":", 1)[1]
    ss = ss.replace(".P", "")
    ss = ss.replace("USDT", "")
    return ss

def load_keys(path: str) -> Tuple[str, str, str]:
    kv = {}
    with open(path, "r", encoding="utf-8") as f2:
        for line in f2:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
            elif ":" in line:
                k, v = line.split(":", 1)
                kv[k.strip()] = v.strip()

    api_key = kv.get("API_KEY") or kv.get("OKX_API_KEY") or kv.get("OK-ACCESS-KEY")
    api_secret = kv.get("API_SECRET") or kv.get("OKX_API_SECRET") or kv.get("OK-ACCESS-SECRET")
    passphrase = kv.get("API_PASSPHRASE") or kv.get("OKX_API_PASSPHRASE") or kv.get("OK-ACCESS-PASSPHRASE")

    if not api_key or not api_secret or not passphrase:
        raise RuntimeError("OKX API 0.3.txt 缺少 API_KEY / API_SECRET / API_PASSPHRASE")
    return api_key, api_secret, passphrase

def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f2:
        return json.load(f2)

def normalize_okx_spec(spec_raw: Any) -> Dict[str, Dict[str, Any]]:
    """把 okx_swaps_spec.txt 正規化成：base -> info（至少含 instId/lotSz/minSz/ctVal/lever 可能有）

    兼容三種常見格式：
    1) dict(base -> info)
    2) dict(instId -> info)
    3) list[info]
    """

    out: Dict[str, Dict[str, Any]] = {}

    def add_one(info: Dict[str, Any]):
        inst = (info.get("instId") or "").strip()
        if not inst:
            return
        base = inst.split("-")[0]
        out[base] = info

    if isinstance(spec_raw, list):
        for it in spec_raw:
            if isinstance(it, dict):
                add_one(it)
        return out

    if isinstance(spec_raw, dict):
        # 先看 value 是否帶 instId
        for k, v in spec_raw.items():
            if isinstance(v, dict) and v.get("instId"):
                add_one(v)
            elif isinstance(v, dict) and isinstance(k, str) and "-" in k:
                # k 可能是 instId
                v2 = dict(v)
                v2.setdefault("instId", k)
                add_one(v2)
            elif isinstance(v, dict) and isinstance(k, str):
                # k 可能是 base
                v2 = dict(v)
                if "instId" not in v2 and v2.get("uly"):
                    pass
                # base->info 仍然收，避免資料缺漏
                out[k.upper()] = v2
        # 若 out 仍然缺 instId 的 base，嘗試二次修正
        for b, info in list(out.items()):
            if isinstance(info, dict) and not info.get("instId") and isinstance(b, str) and b and b != b.upper():
                out[b.upper()] = out.pop(b)
        return out

    return out

def fetch_public_lever(inst_id: str) -> Optional[int]:
    """public instruments 查商品最大槓桿（不需 API Key）。"""
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/public/instruments",
            params={"instType": "SWAP", "instId": inst_id},
            timeout=8,
        )
        r.raise_for_status()
        j = r.json()
        if j.get("code") != "0" or not j.get("data"):
            return None
        return int(float(j["data"][0].get("lever", 0) or 0))
    except Exception:
        return None

# =========================
# OKX Client（私有 API + 簽名）
# =========================
class OKX:
    def __init__(self, api_key: str, api_secret: str, passphrase: str, base_url: str = "https://www.okx.com"):
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self.passphrase = passphrase
        self.base_url = base_url.rstrip("/")
        self.s = requests.Session()
        self.lock = threading.Lock()

    def _iso_ts(self) -> str:
        dt = datetime.now(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _sign(self, ts: str, method: str, path: str, body: str) -> str:
        msg = f"{ts}{method}{path}{body}"
        mac = hmac.new(self.api_secret, msg.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(mac).decode()

    def _headers(self, ts: str, sign: str) -> Dict[str, str]:
        return {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "OK-ACCESS-KEY-VERSION": "2",
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, body: Optional[Dict[str, Any]] = None) -> Any:
        method = method.upper()

        if params:
            qs = requests.Request("GET", "http://x", params=params).prepare().path_url
            if "?" in qs:
                path_full = path + "?" + qs.split("?", 1)[1]
            else:
                path_full = path
        else:
            path_full = path

        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body else ""

        with self.lock:
            ts = self._iso_ts()
            sig = self._sign(ts, method, path_full, body_str)
            r = self.s.request(
                method,
                self.base_url + path_full,
                headers=self._headers(ts, sig),
                data=body_str.encode("utf-8") if body else None,
                timeout=10,
            )

        r.raise_for_status()
        j = r.json()
        if j.get("code") != "0":
            raise RuntimeError(j)
        time.sleep(API_SLEEP)
        return j.get("data", [])

    # 公共
    def public_time_ok(self) -> bool:
        r = self.s.get(self.base_url + "/api/v5/public/time", timeout=4)
        r.raise_for_status()
        j = r.json()
        return bool(j.get("data"))

    def mark_px(self, inst_id: str) -> float:
        r = self.s.get(self.base_url + "/api/v5/public/mark-price", params={"instType": "SWAP", "instId": inst_id}, timeout=6)
        r.raise_for_status()
        return float(r.json()["data"][0]["markPx"])

    # 私有
    def balance_usdt(self) -> float:
        d = self.request("GET", "/api/v5/account/balance", params={"ccy": "USDT"})
        if not d:
            return 0.0
        details = d[0].get("details") or []
        if not details:
            return 0.0
        x = details[0]
        return float(x.get("availBal") or x.get("cashBal") or 0.0)

    def equity_usdt(self) -> float:
        """帳戶總資金（USDT equity）。用於『本金%以總資金為基準』的計算基底。"""
        d = self.request("GET", "/api/v5/account/balance", params={"ccy": "USDT"})
        if not d:
            return 0.0
        details = d[0].get("details") or []
        if not details:
            return 0.0
        x = details[0]
        # 優先 eq（包含已占用保證金與未實現），其次 cashBal
        return float(x.get("eq") or x.get("cashBal") or x.get("availBal") or 0.0)


    def positions_all(self) -> List[Dict[str, Any]]:
        return self.request("GET", "/api/v5/account/positions", params={"instType": "SWAP"}) or []

    def set_leverage(self, inst_id: str, lev: int) -> None:
        self.request("POST", "/api/v5/account/set-leverage", body={"instId": inst_id, "lever": str(int(lev)), "mgnMode": TD_MODE})

    def leverage_info(self, inst_id: str) -> int:
        d = self.request("GET", "/api/v5/account/leverage-info", params={"instId": inst_id, "mgnMode": TD_MODE})
        if not d:
            raise RuntimeError("no leverage-info")
        return int(float(d[0]["lever"]))

    def market_order(self, inst_id: str, side: str, sz: float, reduce_only: bool = False) -> None:
        sz_str = f"{float(sz):.8f}".rstrip("0").rstrip(".")
        body = {
            "instId": inst_id,
            "tdMode": TD_MODE,
            "side": side,
            "ordType": "market",
            "sz": sz_str,
        }
        if reduce_only:
            body["reduceOnly"] = True
        self.request("POST", "/api/v5/trade/order", body=body)

# =========================
# 計算（contracts）
# =========================
def calc_order_contracts(
    bal_usdt: float,
    price: float,
    leverage: int,
    capital_pct: int,
    lot_sz: float,
    min_sz: float,
    ct_val: float,
) -> Tuple[float, float, float]:
    if bal_usdt <= 0 or price <= 0 or ct_val <= 0:
        return 0.0, 0.0, 0.0

    lev = int(leverage)
    pct = int(capital_pct)
    if lev <= 0 or pct <= 0:
        return 0.0, 0.0, 0.0

    lev = max(1, lev)
    pct = max(1, min(100, pct))

    target_margin = bal_usdt * (pct / 100.0)
    notional = target_margin * lev

    qty_base = notional / price
    contracts_raw = qty_base / ct_val

    contracts = round_down(contracts_raw, lot_sz)
    if contracts < min_sz:
        return 0.0, 0.0, 0.0

    notional2 = (contracts * ct_val) * price
    margin_used = notional2 / lev
    return float(contracts), float(margin_used), float(notional2)

# =========================
# 全域狀態
# =========================
# 关键修正：用 RLock，避免同一執行緒重入死鎖
state_lock = threading.RLock()

STATE: Dict[str, Any] = {
    "enabled": True,
    "usdt_balance": 0.0,
    "net_ok": False,
    "pub_ok": False,
    "prv_ok": False,
    "last_update": "",
    "server": f"http://{HOST}:{PORT}/webhook",
    "global_event": "（尚無）",
}

COINS: Dict[str, Dict[str, Any]] = {}
COIN_ORDER: List[str] = []

def set_global_event(msg: str):
    with state_lock:
        STATE["global_event"] = msg

def push_coin_event(base: str, msg: str):
    with state_lock:
        COINS[base]["event"] = msg
        COINS[base]["event_ts"] = now_str()

def total_capital_pct_no_lock() -> int:
    # 呼叫端已持鎖時用這個，避免再套娃
    return int(sum(int(COINS[b]["capital_pct"]) for b in COIN_ORDER))

def total_capital_pct() -> int:
    with state_lock:
        return total_capital_pct_no_lock()

def clamp_leverage_by_product_no_lock(base: str, lev: int) -> int:
    product_max = int(COINS[base]["productMaxLev"])
    lev = int(lev)
    if lev < LEV_MIN:
        lev = LEV_MIN
    if lev > product_max:
        lev = product_max
    return lev

def get_pos_for_inst(pos_list: List[Dict[str, Any]], inst_id: str) -> Tuple[float, float, float, float]:
    pos = 0.0
    avg = 0.0
    upl = 0.0
    upr = 0.0
    for p in pos_list:
        if (p.get("instId") or "") == inst_id:
            pos = f(p.get("pos"), 0.0)
            avg = f(p.get("avgPx"), 0.0)
            upl = f(p.get("upl"), 0.0)
            upr = f(p.get("uplRatio"), 0.0)
            break
    return pos, avg, upl, upr

# =========================
# 交易引擎
# =========================
OKX_CLIENT: Optional[OKX] = None
SPEC: Dict[str, Dict[str, Any]] = {}

def close_position_reduce_only(base: str, inst_id: str, pos: float) -> bool:
    info = SPEC[base]
    lot = f(info.get("lotSz"), 0.0)
    min_sz = f(info.get("minSz"), 0.0)

    close_side = "sell" if pos > 0 else "buy"
    sz = round_down(abs(pos), lot) if lot > 0 else abs(pos)
    if sz < min_sz:
        push_coin_event(base, f"[平倉失敗] 數量不足 minSz（pos={pos}, minSz={min_sz}）")
        return False

    OKX_CLIENT.market_order(inst_id, close_side, sz, reduce_only=True)
    push_coin_event(base, f"[平倉] {close_side.upper()} {sz}（reduceOnly）")
    return True

def _is_retryable_reject(err: Exception) -> bool:
    """判斷是否屬於『槓桿/風險/保證金限制』類拒單，可用 -2 槓桿重試。

    OKX 私有 API 失敗時我們丟出的 RuntimeError 會帶 dict：{code,msg,data...}
    """
    try:
        payload = err.args[0] if err.args else None
        if isinstance(payload, dict):
            msg = str(payload.get('msg','')).lower()
            code = str(payload.get('code',''))
        else:
            msg = str(err).lower()
            code = ''
    except Exception:
        msg = str(err).lower()
        code = ''

    # 常見關鍵字（寧可保守：只在像槓桿/風險/保證金限制時才重試）
    keys = [
        'lever', 'leverage', 'risk', 'tier', 'position', 'margin', 'insufficient',
        'exceeded', 'limit', 'mgn', 'isolated'
    ]
    if any(k in msg for k in keys):
        return True

    # 有些 code 只靠 msg 不穩（保留擴充）
    if code in {'51000','51008','51010','51119','51120','51121','51290'}:
        return True

    return False


def _clamp_product_lev_no_lock(base: str, lev: int) -> int:
    """只以『商品最大槓桿』做 UI 顯示/按鈕 clamp，不用 account/leverage-info 砍到 3x。"""
    return clamp_leverage_by_product_no_lock(base, lev)

def open_position(base: str, inst_id: str, side: str) -> bool:
    info = SPEC[base]
    lot = f(info.get("lotSz"), 0.0)
    min_sz = f(info.get("minSz"), 0.0)
    ct_val = f(info.get("ctVal"), 0.0)

    with state_lock:
        lev_cfg = int(COINS[base]["leverage"])
        cap_pct = int(COINS[base]["capital_pct"])
        cooldown_until = float(COINS[base].get("cooldown_until", 0.0) or 0.0)

    # 0 代表不出手
    if lev_cfg <= 0 or cap_pct <= 0:
        push_coin_event(base, f"[忽略] 槓桿或本金為 0（lev={lev_cfg} cap={cap_pct}%）")
        return False

    now = time.time()
    if now < cooldown_until:
        push_coin_event(base, f"[冷卻中] {int(cooldown_until - now)}s")
        return False

    # 本金% 計算基準：帳戶總資金（equity），不是程式內的剩餘分配
    bal_total = OKX_CLIENT.equity_usdt()
    price = OKX_CLIENT.mark_px(inst_id)

    # UI 設定值只做『商品最大槓桿』 clamp
    with state_lock:
        lev_try = _clamp_product_lev_no_lock(base, lev_cfg)
        product_max = int(COINS[base]["productMaxLev"])

    # 每幣交易鎖：避免同幣同時反手/重試造成卡死
    lock: threading.Lock = COINS[base].setdefault("trade_lock", threading.Lock())
    if not lock.acquire(blocking=False):
        push_coin_event(base, "[忙碌] 前一筆交易尚未完成")
        return False

    try:
        attempts = 0
        while lev_try > 0 and attempts < LEV_RETRY_MAX_ATTEMPTS:
            attempts += 1
            try:
                OKX_CLIENT.set_leverage(inst_id, int(lev_try))

                contracts, margin_used, notional = calc_order_contracts(
                    bal_total, price, int(lev_try), int(cap_pct), lot, min_sz, ct_val
                )
                if contracts <= 0:
                    push_coin_event(base, f"[開倉失敗] 本金太小或低於 minSz（cap={cap_pct}% lev={lev_try}）")
                    return False

                OKX_CLIENT.market_order(inst_id, side, contracts, reduce_only=False)

                # follower：非阻塞同步（不影響本地交易/UI）
                _send_to_follower_async(inst_id, side, int(cap_pct), int(lev_try), reduce_only=False)

                # 記錄實際成交槓桿（UI 顯示用）
                with state_lock:
                    COINS[base]["last_exec_lev"] = int(lev_try)

                push_coin_event(
                    base,
                    f"[開倉] {side.upper()} {contracts}｜保證金≈{margin_used:.2f}｜名目≈{notional:.2f}｜lev={lev_try} cap={cap_pct}%｜策略=UI起點拒單-2/上限{LEV_RETRY_MAX_ATTEMPTS}"
                )
                return True

            except Exception as e:
                # 只在『像槓桿/風險/保證金限制』拒單時才 -2 重試
                if _is_retryable_reject(e) and (lev_try - LEV_RETRY_STEP) >= 1:
                    push_coin_event(base, f"[拒單] lev={lev_try} → {max(1, lev_try-LEV_RETRY_STEP)}（-2 重試）")
                    lev_try = max(1, int(lev_try) - LEV_RETRY_STEP)
                    time.sleep(LEV_RETRY_SLEEP)
                    continue

                # 非可重試：直接回報錯誤並進冷卻
                with state_lock:
                    COINS[base]["cooldown_until"] = time.time() + float(LEV_RETRY_COOLDOWN)
                push_coin_event(base, f"[錯誤] {e}｜進入冷卻{int(LEV_RETRY_COOLDOWN)}s")
                return False

        # 放棄：達上限
        with state_lock:
            COINS[base]["cooldown_until"] = time.time() + float(LEV_RETRY_COOLDOWN)
        push_coin_event(base, f"[放棄] 達嘗試上限 {LEV_RETRY_MAX_ATTEMPTS}｜進入冷卻{int(LEV_RETRY_COOLDOWN)}s")
        return False

    finally:
        try:
            lock.release()
        except Exception:
            pass

def handle_signal(tv_symbol: str, action: str):
    if OKX_CLIENT is None:
        set_global_event("OKX 尚未初始化")
        return

    base = tv_symbol_to_base(tv_symbol)
    action = (action or "").upper().strip()
    if action not in ("BUY", "SELL"):
        return

    with state_lock:
        enabled = bool(STATE["enabled"])

    if not enabled:
        set_global_event("交易未啟用（忽略快訊）")
        return

    with state_lock:
        if base not in COINS:
            set_global_event(f"忽略：{tv_symbol} -> {base}（不在 6 幣白名單）")
            return
        inst_id = COINS[base]["instId"]

    want_side = "buy" if action == "BUY" else "sell"
    push_coin_event(base, f"[TV] {tv_symbol} / {action}")

    try:
        pos_list = OKX_CLIENT.positions_all()
        pos, _, _, _ = get_pos_for_inst(pos_list, inst_id)

        if pos > 0 and want_side == "buy":
            push_coin_event(base, "[忽略] 已多單（同向）")
            return
        if pos < 0 and want_side == "sell":
            push_coin_event(base, "[忽略] 已空單（同向）")
            return

        if pos != 0:
            ok = close_position_reduce_only(base, inst_id, pos)
            if not ok:
                return
            time.sleep(REVERSE_CLOSE_WAIT)
            open_position(base, inst_id, want_side)
            return

        open_position(base, inst_id, want_side)

    except Exception as e:
        push_coin_event(base, f"[錯誤] {e}")

def close_all_positions(reason: str):
    if OKX_CLIENT is None:
        return
    try:
        pos_list = OKX_CLIENT.positions_all()
        any_sent = False
        for p in pos_list:
            inst_id = p.get("instId") or ""
            if not inst_id.endswith("-SWAP"):
                continue
            base = inst_id.split("-")[0] if "-" in inst_id else inst_id
            pos = f(p.get("pos"), 0.0)
            if pos == 0:
                continue
            if base not in SPEC:
                continue

            info = SPEC[base]
            lot = f(info.get("lotSz"), 0.0)
            min_sz = f(info.get("minSz"), 0.0)

            side = "sell" if pos > 0 else "buy"
            sz = round_down(abs(pos), lot) if lot > 0 else abs(pos)
            if sz < min_sz:
                continue

            OKX_CLIENT.market_order(inst_id, side, sz, reduce_only=True)
            any_sent = True

        if any_sent:
            set_global_event(f"[全平送出] {reason}")
        else:
            set_global_event(f"[全平] 無可平倉位（或不足最小單位）｜{reason}")
    except Exception as e:
        set_global_event(f"[全平失敗] {e}")

# =========================
# Flask Webhook
# =========================
app = Flask(__name__)

@app.get("/health")
def health():
    with state_lock:
        return jsonify({
            "ok": True,
            "enabled": STATE["enabled"],
            "usdt_balance": STATE["usdt_balance"],
            "coins": {k: {"instId": COINS[k]["instId"], "lev": COINS[k]["leverage"], "cap": COINS[k]["capital_pct"]} for k in COIN_ORDER},
        })

@app.post("/webhook")
def webhook():
    payload = request.get_json(force=True, silent=True) or {}
    tv_symbol = payload.get("symbol", "")
    action = payload.get("action", "")
    threading.Thread(target=handle_signal, args=(tv_symbol, action), daemon=True).start()
    return jsonify({"ok": True})

def run_flask():
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)

# =========================
if TK_AVAILABLE:
    # UI：6 個幣框（固定順序）
    # =========================
    class CoinPanel(ttk.LabelFrame):
        def __init__(self, master, base: str):
            super().__init__(master, text=base, padding=10)
            self.base = base

            self.lbl_cfg = ttk.Label(self, text="", justify="left")
            self.lbl_cfg.grid(row=0, column=0, columnspan=6, sticky="w")

            ttk.Label(self, text="槓桿").grid(row=1, column=0, sticky="w", padx=(0, 6))
            self.btn_lev_minus = ttk.Button(self, text="－", width=4, command=self.lev_minus)
            self.btn_lev_minus.grid(row=1, column=1)
            self.lbl_lev = ttk.Label(self, text="x1", width=6, anchor="center")
            self.lbl_lev.grid(row=1, column=2, padx=6)
            self.btn_lev_plus = ttk.Button(self, text="＋", width=4, command=self.lev_plus)
            self.btn_lev_plus.grid(row=1, column=3)

            ttk.Label(self, text="本金%").grid(row=2, column=0, sticky="w", padx=(0, 6))
            self.btn_cap_minus = ttk.Button(self, text="－", width=4, command=self.cap_minus)
            self.btn_cap_minus.grid(row=2, column=1)
            self.lbl_cap = ttk.Label(self, text="0%", width=6, anchor="center")
            self.lbl_cap.grid(row=2, column=2, padx=6)
            self.btn_cap_plus = ttk.Button(self, text="＋", width=4, command=self.cap_plus)
            self.btn_cap_plus.grid(row=2, column=3)

            self.lbl_est = ttk.Label(self, text="預估保證金：-｜名目：-｜張數：-", justify="left")
            self.lbl_est.grid(row=3, column=0, columnspan=6, sticky="w", pady=(6, 0))

            self.txt_pos = tk.Text(self, height=6, width=44, font=("Consolas", 10))
            self.txt_pos.grid(row=4, column=0, columnspan=6, sticky="nsew", pady=(8, 6))

            self.lbl_evt = ttk.Label(self, text="（尚無）", wraplength=330, justify="left")
            self.lbl_evt.grid(row=5, column=0, columnspan=6, sticky="w")

            self.columnconfigure(5, weight=1)

        def lev_minus(self):
            with state_lock:
                product_max = int(COINS[self.base]["productMaxLev"])
                step = lev_step_for(product_max)
                cur = int(COINS[self.base]["leverage"])
                cur = max(LEV_MIN, cur - step)
                COINS[self.base]["leverage"] = clamp_leverage_by_product_no_lock(self.base, cur)

        def lev_plus(self):
            with state_lock:
                product_max = int(COINS[self.base]["productMaxLev"])
                step = lev_step_for(product_max)
                cur = int(COINS[self.base]["leverage"])
                cur = cur + step
                COINS[self.base]["leverage"] = clamp_leverage_by_product_no_lock(self.base, cur)

        def cap_minus(self):
            with state_lock:
                cur = int(COINS[self.base]["capital_pct"])
                cur = max(CAP_MIN, cur - CAP_STEP)
                COINS[self.base]["capital_pct"] = cur
                total_now = total_capital_pct_no_lock()
            push_coin_event(self.base, f"[調整] 本金%={cur}（總和={total_now}%）")

        def cap_plus(self):
            # 总和 <= 100%
            with state_lock:
                cur = int(COINS[self.base]["capital_pct"])
                if cur >= CAP_MAX:
                    total_now = total_capital_pct_no_lock()
                    # 不變，但仍回報
                    pass
                else:
                    total_now = total_capital_pct_no_lock()
                    if total_now + CAP_STEP > 100:
                        # 鎖外再推事件，避免鎖內再上鎖
                        msg = f"[拒絕] 本金%總和不可超過 100%（目前={total_now}%）"
                        # release lock then push
                        pass
                    else:
                        cur = min(CAP_MAX, cur + CAP_STEP)
                        COINS[self.base]["capital_pct"] = cur
                        total_now = total_capital_pct_no_lock()
                        msg = f"[調整] 本金%={cur}（總和={total_now}%）"
                        # release lock then push
                        pass

            # 這段要在鎖外才能安全呼叫 push_coin_event
            with state_lock:
                # 重新判斷一下剛剛的狀態，決定訊息
                cur2 = int(COINS[self.base]["capital_pct"])
                total2 = total_capital_pct_no_lock()
            # 若本次其實沒加成功，顯示拒絕訊息
            if total2 > 100:
                total2 = 100
            if cur2 == cur and (total2 == total_now) and (total_now + CAP_STEP > 100):
                push_coin_event(self.base, f"[拒絕] 本金%總和不可超過 100%（目前={total_now}%）")
            else:
                push_coin_event(self.base, f"[調整] 本金%={cur2}（總和={total2}%）")

        def refresh(self):
            with state_lock:
                c = COINS[self.base]
                lev = int(c["leverage"])
                cap = int(c["capital_pct"])
                product_max = int(c["productMaxLev"])
                inst = c["instId"]
                pos = float(c["pos"])
                avg = float(c["avgPx"])
                upl = float(c["upl"])
                upr = float(c["uplRatio"])
                last_exec_lev = c.get("last_exec_lev")
                evt = str(c["event"])
                evt_ts = str(c["event_ts"])

                est_margin = c.get("est_margin")
                est_notional = c.get("est_notional")
                est_contracts = c.get("est_contracts")

            self.lbl_cfg.config(text=f"合約：{inst}｜商品最大槓桿：{product_max}x｜步進：{lev_step_for(product_max)}")
            self.lbl_lev.config(text=f"x{lev}")
            self.lbl_cap.config(text=f"{cap}%")

            if est_margin is None or est_notional is None or est_contracts is None:
                self.lbl_est.config(text="預估保證金：-｜名目：-｜張數：-")
            else:
                self.lbl_est.config(text=f"預估保證金：{est_margin:.3f}｜名目：{est_notional:.2f}｜張數：{est_contracts:.6f}")

            self.txt_pos.delete("1.0", "end")
            if pos == 0:
                self.txt_pos.insert("end", "目前無持倉")
            else:
                direction = "多單" if pos > 0 else "空單"
                self.txt_pos.insert("end", f"方向：{direction}\n")
                self.txt_pos.insert("end", f"張數：{abs(pos):.6f}\n")
                self.txt_pos.insert("end", f"均價：{avg:.6f}\n")
                self.txt_pos.insert("end", f"未實現損益：{upl:.4f} USDT\n")
                self.txt_pos.insert("end", f"收益率：{upr*100.0:.2f}%\n")
                if last_exec_lev is not None:
                    try:
                        self.txt_pos.insert("end", f"實際成交槓桿：x{int(last_exec_lev)}\n")
                    except Exception:
                        pass

            if evt_ts:
                self.lbl_evt.config(text=f"{evt_ts} {evt}")
            else:
                self.lbl_evt.config(text=evt)

    class Dashboard(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("OKX 固定6幣 多幣反手面板（逐倉）")
            self.geometry("1180x860")

            try:
                ttk.Style().theme_use("clam")
            except Exception:
                pass

            self.var_enabled = tk.BooleanVar(value=True)

            top = ttk.LabelFrame(self, text="全域控制", padding=10)
            top.pack(fill="x", padx=10, pady=(10, 8))

            self.lbl_balance = ttk.Label(top, text="USDT 可用：-", font=("Microsoft JhengHei", 12, "bold"))
            self.lbl_balance.pack(side="left")

            ttk.Checkbutton(top, text="啟用交易", variable=self.var_enabled, command=self.on_toggle).pack(side="left", padx=12)

            ttk.Button(top, text="一鍵全平", command=self.on_flat_all).pack(side="right")
            self.lbl_conn = ttk.Label(top, text="連線：-", justify="left")
            self.lbl_conn.pack(side="right", padx=12)

            self.lbl_global_evt = ttk.Label(self, text="（尚無）", padding=10)
            self.lbl_global_evt.pack(fill="x", padx=10, pady=(0, 8))

            grid = ttk.Frame(self)
            grid.pack(fill="both", expand=True, padx=10, pady=(0, 10))
            for r in range(2):
                grid.rowconfigure(r, weight=1)
            for c in range(3):
                grid.columnconfigure(c, weight=1)

            self.panels: Dict[str, CoinPanel] = {}
            for i, base in enumerate(COIN_ORDER):
                r = 0 if i < 3 else 1
                c = i if i < 3 else i - 3
                p = CoinPanel(grid, base)
                p.grid(row=r, column=c, sticky="nsew", padx=6, pady=6)
                self.panels[base] = p

            self.after(UI_REFRESH_MS, self.refresh_ui)

        def on_toggle(self):
            with state_lock:
                STATE["enabled"] = bool(self.var_enabled.get())
            set_global_event(f"交易啟用={STATE['enabled']}")

        def on_flat_all(self):
            if messagebox.askyesno("確認", "確定要一鍵全平（所有倉位 reduceOnly）？"):
                threading.Thread(target=close_all_positions, args=("手動一鍵全平",), daemon=True).start()

        def refresh_ui(self):
            with state_lock:
                bal = float(STATE["usdt_balance"])
                net_ok = bool(STATE["net_ok"])
                pub_ok = bool(STATE["pub_ok"])
                prv_ok = bool(STATE["prv_ok"])
                last_upd = str(STATE["last_update"])
                evt = str(STATE["global_event"])
                cap_sum = total_capital_pct_no_lock()

            self.lbl_balance.config(text=f"USDT 可用：{bal:.4f}｜本金%總和：{cap_sum}%")
            self.lbl_conn.config(text=f"連線：網路={'OK' if net_ok else 'NG'} 行情={'OK' if pub_ok else 'NG'} 帳戶={'OK' if prv_ok else 'NG'} 更新={last_upd}")
            self.lbl_global_evt.config(text=evt)

            for panel in self.panels.values():
                panel.refresh()

            self.after(UI_REFRESH_MS, self.refresh_ui)

    # =========================
    # 背景更新：餘額/持倉/連線/預估
    # =========================
else:
    class Dashboard:
        def __init__(self, *args, **kwargs):
            raise RuntimeError('tkinter 不可用：若在雲端請用 --headless；若要本機 UI 請安裝 tkinter')
def worker_refresh():
    while True:
        ts = now_str()
        net_ok = False
        pub_ok = True
        prv_ok = True

        try:
            net_ok = OKX_CLIENT.public_time_ok() if OKX_CLIENT else False
        except Exception:
            net_ok = False

        bal = 0.0
        pos_list: List[Dict[str, Any]] = []
        try:
            if OKX_CLIENT:
                bal = OKX_CLIENT.balance_usdt()
                bal_total = OKX_CLIENT.equity_usdt()
                pos_list = OKX_CLIENT.positions_all()
        except Exception:
            prv_ok = False

        # pos
        try:
            for base in COIN_ORDER:
                inst = COINS[base]["instId"]
                pos, avg, upl, upr = get_pos_for_inst(pos_list, inst)
                with state_lock:
                    COINS[base]["pos"] = float(pos)
                    COINS[base]["avgPx"] = float(avg)
                    COINS[base]["upl"] = float(upl)
                    COINS[base]["uplRatio"] = float(upr)
        except Exception:
            prv_ok = False

        # 預估（mark price）
        for base in COIN_ORDER:
            try:
                inst = COINS[base]["instId"]
                mark = OKX_CLIENT.mark_px(inst)

                info = SPEC[base]
                lot = f(info.get("lotSz"), 0.0)
                min_sz = f(info.get("minSz"), 0.0)
                ct_val = f(info.get("ctVal"), 0.0)

                with state_lock:
                    lev_cfg = int(COINS[base]["leverage"])
                    cap_pct = int(COINS[base]["capital_pct"])
                    lev_est = clamp_leverage_by_product_no_lock(base, lev_cfg)

                contracts, margin_used, notional = calc_order_contracts(
                    bal_total, mark, lev_est, cap_pct, lot, min_sz, ct_val
                )
                with state_lock:
                    if contracts <= 0:
                        COINS[base]["est_contracts"] = None
                        COINS[base]["est_margin"] = None
                        COINS[base]["est_notional"] = None
                    else:
                        COINS[base]["est_contracts"] = float(contracts)
                        COINS[base]["est_margin"] = float(margin_used)
                        COINS[base]["est_notional"] = float(notional)

            except Exception:
                with state_lock:
                    COINS[base]["est_contracts"] = None
                    COINS[base]["est_margin"] = None
                    COINS[base]["est_notional"] = None
                pub_ok = False

        with state_lock:
            STATE["usdt_balance"] = float(bal)
            STATE["net_ok"] = bool(net_ok)
            STATE["pub_ok"] = bool(pub_ok)
            STATE["prv_ok"] = bool(prv_ok)
            STATE["last_update"] = ts

        time.sleep(1.2)

# =========================
# 初始化 6 幣（固定順序）
# =========================
def init_coins():
    global COINS, COIN_ORDER

    small_profile = load_json(LEV_PROFILE_FILE)

    # 你指定：把 SENT 換成 BTC（維持 5 個小幣）
    if isinstance(small_profile, dict):
        if 'SENT' in small_profile and 'BTC' not in small_profile:
            _tmp = dict(small_profile.pop('SENT'))
            _tmp['instId'] = 'BTC-USDT-SWAP'
            small_profile['BTC'] = _tmp

    small_bases = list(small_profile.keys())
    if len(small_bases) != 5:
        raise RuntimeError(f"{LEV_PROFILE_FILE} 不是 5 個幣（目前 {len(small_bases)}）")

    COIN_ORDER = [MAIN_COIN] + [(b or "").upper() for b in small_bases]
    COINS = {}

    sol_inst = SPEC[MAIN_COIN]["instId"]
    sol_default = (SOL_PRODUCT_MAX_LEV + 1) // 2

    COINS[MAIN_COIN] = {
        "base": MAIN_COIN,
        "instId": sol_inst,
        "productMaxLev": int(SOL_PRODUCT_MAX_LEV),
        "leverage": int(sol_default),
        "capital_pct": 30,
        "last_exec_lev": None,
        "trade_lock": threading.Lock(),
        "cooldown_until": 0.0,
        "pos": 0.0, "avgPx": 0.0, "upl": 0.0, "uplRatio": 0.0,
        "event": "初始化完成",
        "event_ts": now_str(),
        "est_contracts": None, "est_margin": None, "est_notional": None,
    }

    for b0 in small_bases:
        base = (b0 or "").upper()
        inst_id = small_profile[b0].get("instId")
        if not inst_id:
            raise RuntimeError(f"{LEV_PROFILE_FILE} 缺少 {b0} 的 instId")

        # ===== 商品最大槓桿（UI 上限） =====
        # 優先：SPEC 裡的 lever（public instruments）
        # 若 SPEC 沒有 lever：用 public instruments 即時查一次
        # 最後保底：沿用 TXT 的 maxLeverage（避免整個 UI 壞掉）
        spec_info = SPEC.get(base, {}) if isinstance(SPEC, dict) else {}
        product_max = None
        try:
            if isinstance(spec_info, dict) and (spec_info.get("lever") is not None):
                product_max = int(float(spec_info.get("lever") or 0))
        except Exception:
            product_max = None

        if not product_max or product_max <= 0:
            product_max = fetch_public_lever(inst_id)

        if not product_max or product_max <= 0:
            try:
                product_max = int(float(small_profile[b0].get("maxLeverage") or 0))
            except Exception:
                product_max = 0

        if not product_max or product_max <= 0:
            product_max = 1

        # ===== 預設槓桿：商品最大的一半（取整） =====
        default_lev = (int(product_max) + 1) // 2

        COINS[base] = {
            "base": base,
            "instId": inst_id,
            "productMaxLev": int(product_max),
            "leverage": int(default_lev),
            "capital_pct": 10,
            "last_exec_lev": None,
            "trade_lock": threading.Lock(),
            "cooldown_until": 0.0,
            "pos": 0.0, "avgPx": 0.0, "upl": 0.0, "uplRatio": 0.0,
            "event": "初始化完成",
            "event_ts": now_str(),
            "est_contracts": None, "est_margin": None, "est_notional": None,
        }

    # 保底：總和不得超 100
    with state_lock:
        total = total_capital_pct_no_lock()
        if total > 100:
            for b in reversed(COIN_ORDER):
                if total <= 100:
                    break
                if b == MAIN_COIN:
                    continue
                cur = int(COINS[b]["capital_pct"])
                if cur > CAP_MIN:
                    cut = min(cur - CAP_MIN, total - 100)
                    COINS[b]["capital_pct"] = int(cur - cut)
                    total = total_capital_pct_no_lock()

# =========================
# Main
# =========================
def main():
    global OKX_CLIENT, SPEC, HOST, PORT

    parser = argparse.ArgumentParser(description="0.1.6 TV→OKX micro trade server")
    parser.add_argument("--headless", "--server", action="store_true", help="雲端/無UI模式：只跑 Flask + 背景工作")
    args = parser.parse_args()

    # Render/雲端通常會透過環境變數 PORT 指定監聽埠
    if args.headless:
        HOST = "0.0.0.0"
        try:
            PORT = int(os.getenv("PORT", str(PORT)))
        except Exception:
            pass

    SPEC_RAW = load_json(SPEC_FILE)
    SPEC = normalize_okx_spec(SPEC_RAW)
    if MAIN_COIN not in SPEC:
        raise RuntimeError(f"{SPEC_FILE} 沒有 {MAIN_COIN}")

    api_key, api_secret, passphrase = load_keys(KEY_FILE)
    OKX_CLIENT = OKX(api_key, api_secret, passphrase)

    init_coins()
    set_global_event(f"初始化完成：{', '.join(COIN_ORDER)}｜Webhook={STATE['server']}")

    # 背景刷新（保留）
    threading.Thread(target=worker_refresh, daemon=True).start()

    if args.headless:
        # 雲端模式：不啟動 Tk UI，Flask 以前景方式執行以保持程序常駐
        run_flask()
        return

    # 本機模式：需要 Tk UI
    if not TK_AVAILABLE:
        raise RuntimeError("本機 UI 需要 tkinter，但目前環境缺少 tkinter。若在雲端請加 --headless")
    threading.Thread(target=run_flask, daemon=True).start()

    ui = Dashboard()
    ui.mainloop()

if __name__ == "__main__":
    main()