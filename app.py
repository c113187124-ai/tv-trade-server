# -*- coding: utf-8 -*-
"""
app_master_web_OKX_UI_FINAL.py - 完整修正版

✅ 修復持倉顯示和計算問題
✅ 統一合約單位計算
✅ 正確顯示持倉價值和保證金
✅ 修復 set_leverage 缺少 mgnMode 參數的問題
✅ 修復 favicon.ico 404 錯誤
✅ 修復本單保證金計算顯示
"""

import os
import time
import json
import hmac
import base64
import hashlib
import math
import decimal
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, List

import requests
from flask import Flask, request, jsonify, Response, send_file


# ===================== 你的設定（6 幣種） =====================
APP_PORT = int(os.getenv("APP_PORT", "9000"))

COIN_ORDER = ["SOL", "XAU", "AXS", "FOGO", "BERA", "BTC"]
STEP_CAPITAL = int(os.getenv("STEP_CAPITAL", "1"))   # UI 每次調整本金比例 %
STEP_LEV = int(os.getenv("STEP_LEV", "1"))           # UI 每次調整槓桿

CFG: Dict[str, Any] = {
    "live": True,
    "order": COIN_ORDER,
    "coins": {
        "SOL":  {"capital_pct": 30, "leverage": 25, "instId": "SOL-USDT-SWAP"},
        "XAU":  {"capital_pct": 10, "leverage": 20, "instId": "XAU-USDT-SWAP"},
        "AXS":  {"capital_pct": 10, "leverage": 20, "instId": "AXS-USDT-SWAP"},
        "FOGO": {"capital_pct": 10, "leverage": 20, "instId": "FOGO-USDT-SWAP"},
        "BERA": {"capital_pct": 10, "leverage": 20, "instId": "BERA-USDT-SWAP"},
        "BTC":  {"capital_pct": 10, "leverage": 10, "instId": "BTC-USDT-SWAP"},
    }
}

# 可選：允許你用 env 覆蓋 OKX_BASE_URL（一般不用）
OKX_BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")

# ===================== 本地合約規格（0.1.6 同款） =====================
# 讀取 okx_swaps_spec.txt（JSON），提供 ctVal / lotSz / minSz
SPEC_FILE = os.getenv("OKX_SPEC_FILE", "okx_swaps_spec.txt")

def load_local_spec() -> Dict[str, Any]:
    try:
        if os.path.exists(SPEC_FILE):
            with open(SPEC_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

LOCAL_SPEC: Dict[str, Any] = load_local_spec()

def get_spec_by_coin(coin: str) -> Dict[str, float]:
    c = (coin or "").upper()
    info = LOCAL_SPEC.get(c) if isinstance(LOCAL_SPEC, dict) else None
    if isinstance(info, dict):
        return {
            "ctVal": float(info.get("ctVal") or 1.0),
            "lotSz": float(info.get("lotSz") or 1.0),
            "minSz": float(info.get("minSz") or 1.0),
            "instId": str(info.get("instId") or ""),
        }
    return {"ctVal": 1.0, "lotSz": 1.0, "minSz": 1.0, "instId": ""}

def round_down(x: float, step: float) -> float:
    try:
        if step <= 0:
            return float(x)
        return math.floor(x / step) * step
    except Exception:
        return 0.0

def fmt_sz(sz: float, lotSz: float) -> str:
    """避免科學記號，並與 lotSz 精度對齊"""
    if lotSz >= 1:
        return str(int(sz))
    # lotSz < 1：用 lotSz 決定小數位
    s = f"{lotSz:.12f}".rstrip("0")
    decimals = len(s.split(".")[1]) if "." in s else 0
    return f"{sz:.{decimals}f}"

def contracts_to_coins(contracts: float, ctVal: float) -> float:
    """將合約張數轉換為等價的幣數量"""
    return contracts * ctVal

def coins_to_contracts(coins: float, ctVal: float) -> float:
    """將幣數量轉換為合約張數"""
    if ctVal <= 0:
        return 0.0
    return coins / ctVal


# ===================== OKX Client =====================
class OKXError(Exception):
    pass


def _okx_ts() -> str:
    # ✅ OKX 官方要求：UTC ISO8601 + milliseconds + Z
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _f2(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


class OKXClient:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.api_key = os.getenv("OKX_API_KEY", "")
        self.api_secret = os.getenv("OKX_API_SECRET", "")
        self.api_passphrase = os.getenv("OKX_API_PASSPHRASE", "")
        if not self.api_key or not self.api_secret or not self.api_passphrase:
            raise RuntimeError("缺少 OKX API 環境變數：OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE")

    def _sign(self, ts: str, method: str, path: str, body: str) -> str:
        msg = f"{ts}{method.upper()}{path}{body}"
        mac = hmac.new(self.api_secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(mac).decode("utf-8")

    def _headers(self, ts: str, method: str, path: str, body: str) -> Dict[str, str]:
        return {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": self._sign(ts, method, path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.api_passphrase,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> Any:
        ts = _okx_ts()
        body = json.dumps(payload, separators=(",", ":")) if payload else ""
        url = self.base + path

        r = requests.request(
            method=method,
            url=url,
            headers=self._headers(ts, method, path, body),
            data=body if payload else None,
            timeout=10,
        )

        # OKX 多數回 JSON；少數錯誤可能不是，做保護
        try:
            j = r.json()
        except Exception:
            raise OKXError(f"OKX non-JSON response: http={r.status_code} text={r.text[:200]}")

        if j.get("code") != "0":
            raise OKXError(f"OKX API error: http={r.status_code} json={j}")
        return j.get("data", [])

    # ---- 封裝 API ----
    def equity_usdt(self) -> float:
        data = self._request("GET", "/api/v5/account/balance")
        # data[0]['totalEq'] = 總權益（折算 USDT）
        return _f2(data[0].get("totalEq", 0.0), 0.0) if data else 0.0

    def positions_all(self) -> list:
        # 全部持倉（含 SWAP）
        return self._request("GET", "/api/v5/account/positions")

    def last_price(self, instId: str) -> float:
        data = self._request("GET", f"/api/v5/market/ticker?instId={instId}")
        return _f2(data[0].get("last", 0.0), 0.0) if data else 0.0

    def mark_price(self, instId: str) -> float:
        """獲取標記價格（用於計算保證金和盈虧）"""
        data = self._request("GET", f"/api/v5/public/mark-price?instId={instId}")
        return _f2(data[0].get("markPx", 0.0), 0.0) if data else 0.0

    def set_leverage(self, instId: str, lever: int, mgnMode: str = "cross", tdMode: str = "cross") -> Any:
        return self._request("POST", "/api/v5/account/set-leverage", {
            "instId": instId,
            "lever": str(int(lever)),
            "mgnMode": mgnMode,
            "tdMode": tdMode,
        })

    def place_order_market(self, instId: str, side: str, sz: float, tdMode: str = "cross", reduceOnly: bool = False) -> Any:
        # 下單：市價單
        payload = {
            "instId": instId,
            "tdMode": tdMode,
            "side": side,
            "ordType": "market",
            "sz": str(sz),
        }
        if reduceOnly:
            payload["reduceOnly"] = True
        return self._request("POST", "/api/v5/trade/order", payload)

    def close_position(self, instId: str, tdMode: str = "cross", mgnMode: Optional[str] = None) -> Any:
        payload = {"instId": instId, "tdMode": tdMode}
        if mgnMode:
            payload["mgnMode"] = mgnMode
        return self._request("POST", "/api/v5/trade/close-position", payload)


# ===================== 核心：計算 / 顯示 =====================
def total_capital_pct() -> int:
    return int(sum(int(CFG["coins"][c]["capital_pct"]) for c in CFG["order"] if c in CFG["coins"]))


def calc_margin(equity: float, capital_pct: float, leverage: float) -> float:
    """計算本單應該使用的保證金"""
    return max(0.0, equity * (capital_pct / 100.0))


def calc_position_margin(pos_contracts: float, avg_price: float, ctVal: float, leverage: float) -> float:
    """計算持倉實際使用的保證金"""
    if pos_contracts == 0 or avg_price == 0 or ctVal == 0 or leverage == 0:
        return 0.0
    # 持倉價值 = 合約張數 * 合約乘數 * 平均價格
    position_value = abs(pos_contracts) * ctVal * avg_price
    # 保證金 = 持倉價值 / 槓桿
    return position_value / leverage


def calc_order_sz(instId: str, equity: float, capital_pct: float, leverage: int) -> Tuple[float, float, float, Dict[str, float], float]:
    """
    計算下單張數和幣數量
    回傳：(contracts, coins_amount, price, spec, actual_margin)
    """
    try:
        price = OKX_CLIENT.last_price(instId)
        if equity <= 0 or price <= 0:
            return 0.0, 0.0, price, {}, 0.0

        lev = int(leverage)
        pct = int(capital_pct)
        lev = max(1, lev)
        pct = max(1, min(100, pct))

        # 計算目標保證金
        target_margin = equity * (pct / 100.0)
        # 計算名目價值
        notional = target_margin * lev

        # 找 coin 以讀 spec
        coin_guess = None
        for c in CFG["coins"]:
            if CFG["coins"][c]["instId"] == instId:
                coin_guess = c
                break
        spec = get_spec_by_coin(coin_guess or "")
        ctVal = float(spec.get("ctVal") or 1.0)
        lotSz = float(spec.get("lotSz") or 1.0)
        minSz = float(spec.get("minSz") or 1.0)

        if ctVal <= 0:
            ctVal = 1.0
        if lotSz <= 0:
            lotSz = 1.0
        if minSz <= 0:
            minSz = 1.0

        # 計算幣數量
        coins_amount = notional / price
        # 計算合約張數
        contracts_raw = coins_amount / ctVal
        contracts = round_down(contracts_raw, lotSz)

        # 防浮點殘差
        try:
            d = decimal.Decimal(str(lotSz))
            contracts = float(decimal.Decimal(str(contracts)).quantize(d, rounding=decimal.ROUND_DOWN))
        except Exception:
            pass

        # 重新計算實際的幣數量（根據調整後的合約張數）
        actual_coins = contracts_to_coins(contracts, ctVal)
        
        # 計算實際名目價值和實際保證金
        actual_notional = actual_coins * price
        actual_margin = actual_notional / lev if lev > 0 else 0.0
        
        if contracts < minSz:
            return 0.0, 0.0, price, spec, 0.0

        return float(contracts), float(actual_coins), price, spec, actual_margin
    except Exception as e:
        print(f"[calc_order_sz] ERROR: {e}")
        return 0.0, 0.0, 0.0, {}, 0.0


def build_positions_view() -> Dict[str, Any]:
    equity = OKX_CLIENT.equity_usdt()
    raw = OKX_CLIENT.positions_all()

    out: Dict[str, Any] = {}
    
    # 初始化所有幣種的數據
    for coin in CFG["order"]:
        instId = CFG["coins"][coin]["instId"]
        spec = get_spec_by_coin(coin)
        ctVal = float(spec.get("ctVal") or 1.0)
        lotSz = float(spec.get("lotSz") or 1.0)
        minSz = float(spec.get("minSz") or 1.0)
        
        out[coin] = {
            "instId": instId,
            "hasPos": False,
            "pos_contracts": 0.0,      # 合約張數
            "pos_coins": 0.0,          # 等價幣數量
            "posSide": None,
            "upl": 0.0,
            "roe": 0.0,                # 收益率
            "avgPx": 0.0,              # 開倉均價
            "lever": 1,                # 槓桿
            "margin": 0.0,             # 持倉保證金
            "mgnMode": None,           # 保證金模式
            "ctVal": ctVal,            # 合約乘數
            "lotSz": lotSz,            # 最小交易單位
            "minSz": minSz,            # 最小下單數量
            "markPrice": 0.0,          # 標記價格
            "notional": 0.0,           # 名目價值
            "target_margin": calc_margin(equity, CFG["coins"][coin]["capital_pct"], CFG["coins"][coin]["leverage"]),  # 目標保證金
        }

    for p in raw:
        instId = p.get("instId", "")
        if not instId:
            continue
            
        coin = instId.split("-")[0]
        if coin not in out:
            continue
            
        pos_contracts = _f2(p.get("pos", 0.0), 0.0)
        if abs(pos_contracts) <= 0:
            continue

        # 獲取持倉相關數據
        upl = _f2(p.get("upl", 0.0), 0.0)
        uplRatio = _f2(p.get("uplRatio", 0.0), 0.0) * 100.0
        avgPx = _f2(p.get("avgPx", 0.0), 0.0)
        lever = _f2(p.get("lever", 1.0), 1.0)
        mgnMode = p.get("mgnMode") or None
        
        # 獲取合約規格
        spec = get_spec_by_coin(coin)
        ctVal = float(spec.get("ctVal") or 1.0)
        
        # 計算幣數量
        pos_coins = contracts_to_coins(abs(pos_contracts), ctVal)
        
        # 獲取標記價格
        markPrice = OKX_CLIENT.mark_price(instId)
        
        # 計算名目價值
        notional = abs(pos_contracts) * ctVal * markPrice
        
        # 計算持倉保證金
        margin = calc_position_margin(pos_contracts, avgPx, ctVal, lever)
        
        # 計算目標保證金（基於當前設定）
        target_margin = calc_margin(equity, CFG["coins"][coin]["capital_pct"], CFG["coins"][coin]["leverage"])
        
        out[coin].update({
            "hasPos": True,
            "pos_contracts": pos_contracts,
            "pos_coins": pos_coins,
            "posSide": p.get("posSide") or None,
            "upl": upl,
            "roe": uplRatio,
            "avgPx": avgPx,
            "lever": lever,
            "margin": margin,
            "mgnMode": mgnMode,
            "markPrice": markPrice,
            "notional": notional,
            "target_margin": target_margin,
        })

    return {"ok": True, "equity_usdt": equity, "positions": out}


# ===================== Flask App =====================
app = Flask(__name__)
OKX_CLIENT = OKXClient(OKX_BASE_URL)

# 處理 favicon.ico 請求，避免 404 錯誤
@app.route('/favicon.ico')
def favicon():
    # 返回一個空的圖標
    return send_file('favicon.ico', mimetype='image/vnd.microsoft.icon')

# 如果沒有 favicon.ico 文件，創建一個空的
if not os.path.exists('favicon.ico'):
    with open('favicon.ico', 'wb') as f:
        f.write(b'')


@app.get("/api/config")
def api_config():
    return jsonify({"ok": True, "config": CFG}), 200


@app.post("/api/config/coin/<coin>")
def api_config_coin(coin: str):
    if coin not in CFG["coins"]:
        return jsonify({"ok": False, "error": "UNKNOWN_COIN"}), 400

    data = request.get_json(silent=True) or {}
    cur = CFG["coins"][coin]

    new_cap = int(data.get("capital_pct", cur["capital_pct"]))
    new_lev = int(data.get("leverage", cur["leverage"]))

    if new_cap < 0:
        new_cap = 0
    if new_lev < 1:
        new_lev = 1

    other = total_capital_pct() - int(cur["capital_pct"])
    if other + new_cap > 100:
        return jsonify({"ok": False, "error": "CAP_OVER_100"}), 400

    cur["capital_pct"] = new_cap
    cur["leverage"] = new_lev
    return jsonify({"ok": True, "config": CFG}), 200


@app.post("/api/live")
def api_live():
    data = request.get_json(silent=True) or {}
    live = bool(data.get("live", False))
    CFG["live"] = live
    return jsonify({"ok": True, "live": CFG["live"]}), 200


@app.get("/api/positions")
def api_positions():
    try:
        return jsonify(build_positions_view()), 200
    except Exception as e:
        print("[api_positions] ERROR:", repr(e))
        return jsonify({"ok": True, "equity_usdt": 0.0, "positions": {}}), 200


@app.post("/api/close/<coin>")
def api_close(coin: str):
    if coin not in CFG["coins"]:
        return jsonify({"ok": False, "error": "UNKNOWN_COIN"}), 400
    instId = CFG["coins"][coin]["instId"]
    try:
        # 帶回真實 mgnMode/tdMode
        mgn = "cross"
        try:
            pos_all = OKX_CLIENT.positions_all()
            for p in pos_all:
                if p.get("instId") == instId and abs(_f2(p.get("pos"), 0.0)) > 0:
                    mgn = (p.get("mgnMode") or p.get("tdMode") or "cross")
                    break
        except Exception:
            pass
        OKX_CLIENT.close_position(instId, tdMode=mgn, mgnMode=mgn)
        return jsonify({"ok": True}), 200
    except Exception as e:
        print("[api_close] ERROR:", repr(e))
        return jsonify({"ok": False, "error": str(e)}), 200


@app.post("/api/webhook")
def api_webhook():
    """
    TV 來的訊號格式（你可用 python requests 模擬）：
    {
      "coin": "BTC",
      "action": "BUY" | "SELL"
    }
    """
    data = request.get_json(silent=True) or {}
    coin = (data.get("coin") or data.get("symbol") or "").upper()
    action = (data.get("action") or "").upper()

    if coin not in CFG["coins"]:
        return jsonify({"ok": False, "error": "UNKNOWN_COIN"}), 200
    if action not in ("BUY", "SELL"):
        return jsonify({"ok": False, "error": "UNKNOWN_ACTION"}), 200

    instId = CFG["coins"][coin]["instId"]
    cap = int(CFG["coins"][coin]["capital_pct"])
    lev = int(CFG["coins"][coin]["leverage"])
    live = bool(CFG.get("live", False))

    # 計算下單張數和幣數量
    equity = 0.0
    price = 0.0
    contracts = 0.0
    coins_amount = 0.0
    spec = {}
    actual_margin = 0.0
    
    try:
        equity = OKX_CLIENT.equity_usdt()
        contracts, coins_amount, price, spec, actual_margin = calc_order_sz(instId, equity, cap, lev)
    except Exception as e:
        print("[TV] calc ERROR:", repr(e))
        return jsonify({"ok": False, "error": "CALC_FAIL"}), 200

    print(f"[TV] action={action} coin={coin} inst={instId} pct={cap}% lev=x{lev} live={live} equity={equity:.4f} price={price:.4f}")
    print(f"[TV] 計算結果: contracts={contracts:.4f}, coins={coins_amount:.6f}, actual_margin={actual_margin:.3f}")

    if not live:
        return jsonify({"ok": True, "dry_run": True, "coin": coin, "instId": instId, 
                       "contracts": contracts, "coins": coins_amount, "price": price, 
                       "target_margin": calc_margin(equity, cap, lev), "actual_margin": actual_margin}), 200

    try:
        side = "buy" if action == "BUY" else "sell"
        
        # 檢查是否有反向持倉，有則先平倉
        try:
            pos_all = OKX_CLIENT.positions_all()
            current_pos = 0.0
            current_mgn = "cross"
            for p in pos_all:
                if p.get("instId") == instId:
                    current_pos = _f2(p.get("pos"), 0.0)
                    current_mgn = p.get("mgnMode") or "cross"
                    break
            
            # 檢查是否為反向倉位
            if current_pos > 0 and action == "SELL":
                print(f"[TV] 先平多倉，再開空倉")
                # 平多倉
                try:
                    OKX_CLIENT.place_order_market(instId, "sell", abs(current_pos), tdMode=current_mgn, reduceOnly=True)
                    time.sleep(0.2)  # 等待平倉完成
                except Exception as e:
                    print(f"[TV] 平倉失敗: {e}")
            
            elif current_pos < 0 and action == "BUY":
                print(f"[TV] 先平空倉，再開多倉")
                # 平空倉
                try:
                    OKX_CLIENT.place_order_market(instId, "buy", abs(current_pos), tdMode=current_mgn, reduceOnly=True)
                    time.sleep(0.2)  # 等待平倉完成
                except Exception as e:
                    print(f"[TV] 平倉失敗: {e}")
                    
        except Exception as e:
            print(f"[TV] 檢查持倉錯誤: {e}")

        if contracts <= 0:
            return jsonify({"ok": False, "error": "SZ_TOO_SMALL", "coin": coin, "instId": instId, 
                           "price": price, "equity": equity, "pct": cap, "lev": lev, 
                           "ctVal": spec.get("ctVal"), "lotSz": spec.get("lotSz"), 
                           "minSz": spec.get("minSz")}), 200
        
        # 設槓桿（修正：添加 mgnMode 參數）
        try:
            OKX_CLIENT.set_leverage(instId, lev, mgnMode="cross", tdMode="cross")
            print(f"[TV] 設槓桿成功: x{lev}")
        except Exception as e:
            print(f"[TV] 設槓桿失敗: {e}")
            # 如果槓桿設置失敗，嘗試使用較低槓桿
            for try_lev in [lev-2, lev-4, 10, 5, 3, 1]:
                if try_lev > 0:
                    try:
                        OKX_CLIENT.set_leverage(instId, try_lev, mgnMode="cross", tdMode="cross")
                        print(f"[TV] 改用槓桿 x{try_lev}")
                        lev = try_lev  # 更新實際使用的槓桿
                        break
                    except:
                        continue
        
        # 重新計算 sz（因槓桿可能已改變）
        if lev != int(CFG["coins"][coin]["leverage"]):
            contracts, coins_amount, price, spec, actual_margin = calc_order_sz(instId, equity, cap, lev)
        
        sz_str = fmt_sz(contracts, float(spec.get("lotSz") or 1.0))
        print(f"[TV] 下單: {side.upper()} {sz_str} 張 (約 {coins_amount:.6f} 幣)")
        print(f"[TV] 合約規格: ctVal={spec.get('ctVal')} lotSz={spec.get('lotSz')} minSz={spec.get('minSz')}")
        print(f"[TV] 目標保證金: {calc_margin(equity, cap, lev):.3f} USDT, 實際保證金: {actual_margin:.3f} USDT")
        
        # 下單
        OKX_CLIENT.place_order_market(instId, side=side, sz=sz_str, tdMode="cross")
        
        return jsonify({"ok": True, "coin": coin, "instId": instId, "side": side, 
                       "contracts": sz_str, "coins": coins_amount, "price": price, 
                       "actual_leverage": lev, "target_margin": calc_margin(equity, cap, lev), 
                       "actual_margin": actual_margin}), 200
    except Exception as e:
        print("[TV] order ERROR:", repr(e))
        return jsonify({"ok": False, "error": str(e)}), 200


# ---------------------------- UI ----------------------------
PANEL_HTML = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>交易控制面板</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>📊</text></svg>">
<style>
  body{{background:#0f1115;color:#fff;font-family:system-ui,Segoe UI,Arial;margin:18px}}
  .topbar{{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}}
  .badge{{padding:6px 10px;border-radius:999px;background:#1c1f26;font-size:14px}}
  .toggle{{display:flex;gap:10px;align-items:center}}
  .switch{{cursor:pointer;border:1px solid #2b2f3a;background:#11141a;border-radius:12px;padding:8px 12px;color:#fff}}
  .grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}}
  .card{{background:#1c1f26;border-radius:14px;padding:16px;box-shadow:0 2px 10px rgba(0,0,0,.35)}}
  .title{{font-size:18px;font-weight:900;margin-bottom:8px}}
  .row{{display:flex;align-items:center;justify-content:center;gap:16px;margin:10px 0}}
  .btn{{width:54px;height:54px;font-size:28px;border-radius:12px;border:1px solid #2b2f3a;background:#11141a;color:#fff;cursor:pointer}}
  .btn:active{{transform:scale(.98)}}
  .val{{min-width:90px;text-align:center;font-size:22px;font-weight:800}}
  .sub{{font-size:13px;opacity:.85;margin-top:2px;text-align:center}}
  .pos{{margin-top:10px;text-align:center;font-size:14px;opacity:.95;min-height:20px}}
  .close{{margin-top:12px;width:100%;height:44px;border-radius:12px;border:1px solid #3a1f1f;background:#b33;color:#fff;font-size:16px;cursor:pointer}}
  .close:disabled{{opacity:.45;cursor:not-allowed}}
  .equity{{font-size:14px;opacity:.9}}
  .pos-info{{font-size:12px;opacity:.8;margin-top:4px}}
  .margin-info{{font-size:11px;opacity:.7;margin-top:2px}}
  .margin-label{{font-size:12px;font-weight:600;margin-top:4px;color:#4a9eff}}
  @media (max-width: 980px){{ .grid{{grid-template-columns:repeat(2,1fr)}} }}
  @media (max-width: 640px){{ .grid{{grid-template-columns:1fr}} }}
</style>
</head>
<body>
  <div class="topbar">
    <div style="font-size:20px;font-weight:900">交易控制面板</div>
    <div class="toggle">
      <div class="badge" id="liveBadge">LIVE: 讀取中…</div>
      <button class="switch" id="liveBtn">切換 LIVE</button>
      <div class="badge equity" id="eqBadge">餘額: 讀取中…</div>
    </div>
  </div>

  <div class="grid" id="grid"></div>

<script>
const STEP_CAPITAL = {STEP_CAPITAL};
const STEP_LEV = {STEP_LEV};

let cfg = null;
let positions = {{}};
let equity = 0;

async function loadConfigOnce(){{
  const r = await fetch('/api/config');
  const j = await r.json();
  cfg = j.config;
  renderCards();
  refreshLiveBadge();
}}

function refreshLiveBadge(){{
  document.getElementById('liveBadge').innerText = 'LIVE: ' + (cfg.live ? 'True' : 'False');
}}

async function toggleLive(){{
  const live = !cfg.live;
  const r = await fetch('/api/live', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{live}})}});
  const j = await r.json();
  if(j.ok){{ cfg.live = j.live; refreshLiveBadge(); }}
}}

document.getElementById('liveBtn').onclick = toggleLive;

function orderCoins(){{
  return cfg.order || Object.keys(cfg.coins);
}}

function coinCardHtml(coin){{
  const c = cfg.coins[coin];
  const p = positions[coin];

  // 從 positions API 獲取保證金數據
  const target_margin = p ? p.target_margin || 0 : 0;
  
  let posText = '無持倉';
  let canClose = false;
  let posDetails = '';
  let actualPosMargin = 0;
  
  if(p && p.hasPos){{
    canClose = true;
    const direction = p.posSide === 'long' ? '多' : (p.posSide === 'short' ? '空' : '未知');
    posText = `${{direction}} | 張數: ${{p.pos_contracts.toFixed(4)}} | 幣數: ${{p.pos_coins.toFixed(6)}}`;
    posDetails = `持倉保證金: ${{p.margin.toFixed(3)}} USDT | 均價: ${{p.avgPx.toFixed(2)}} | 槓桿: x${{p.lever}}`;
    posDetails += ` | 收益率: ${{p.roe.toFixed(3)}}% | 盈虧: ${{p.upl.toFixed(3)}}`;
    actualPosMargin = p.margin;
  }}

  return `
    <div class="card">
      <div class="title">${{coin}}</div>

      <div class="sub">本金比例</div>
      <div class="row">
        <button class="btn" onclick="changePct('${{coin}}', -${{STEP_CAPITAL}})">−</button>
        <div class="val">${{c.capital_pct}}%</div>
        <button class="btn" onclick="changePct('${{coin}}', ${{STEP_CAPITAL}})">+</button>
      </div>
      
      <div class="margin-label">保證金計算</div>
      <div class="margin-info">目標保證金: ${{target_margin.toFixed(3)}} USDT</div>
      ${{actualPosMargin > 0 ? `<div class="margin-info">實際持倉保證金: ${{actualPosMargin.toFixed(3)}} USDT</div>` : ''}}
      ${{p && p.ctVal ? `<div class="margin-info">合約乘數: ${{p.ctVal}}, 最小單位: ${{p.lotSz}}, 最小下單: ${{p.minSz}}</div>` : ''}}

      <div class="sub">槓桿</div>
      <div class="row">
        <button class="btn" onclick="changeLev('${{coin}}', -${{STEP_LEV}})">−</button>
        <div class="val">x${{c.leverage}}</div>
        <button class="btn" onclick="changeLev('${{coin}}', ${{STEP_LEV}})">+</button>
      </div>

      <div class="pos" id="pos_${{coin}}">${{posText}}</div>
      <div class="pos-info" id="pos_details_${{coin}}">${{posDetails}}</div>
      <button class="close" id="close_${{coin}}" onclick="closeCoin('${{coin}}')" ${{canClose ? '' : 'disabled'}}>一鍵平倉</button>
    </div>
  `;
}}

function renderCards(){{
  const g = document.getElementById('grid');
  let html = '';
  for(const coin of orderCoins()){{
    if(!cfg.coins[coin]) continue;
    html += coinCardHtml(coin);
  }}
  g.innerHTML = html;
}}

async function applyCoin(coin, patch){{
  const r = await fetch('/api/config/coin/' + coin, {{
    method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify(patch)
  }});
  const j = await r.json();
  if(!j.ok){{ alert(j.error || '更新失敗'); return; }}
  cfg = j.config;
  renderCards();
}}

async function changePct(coin, delta){{
  // 前端先擋總和 > 100
  let sum = 0;
  for(const k of orderCoins()) sum += cfg.coins[k].capital_pct;
  const cur = cfg.coins[coin].capital_pct;
  const next = cur + delta;
  const nextSum = sum - cur + next;
  if(nextSum > 100){{
    alert('本金比例總和不可超過 100%');
    return;
  }}
  await applyCoin(coin, {{capital_pct: next}});
}}

async function changeLev(coin, delta){{
  const cur = cfg.coins[coin].leverage;
  await applyCoin(coin, {{leverage: cur + delta}});
}}

async function closeCoin(coin){{
  await fetch('/api/close/' + coin, {{method:'POST'}});
  await refreshPositions();
}}

async function refreshPositions(){{
  const r = await fetch('/api/positions');
  const j = await r.json();
  if(j.ok){{
    positions = j.positions || {{}};
    equity = Number(j.equity_usdt || 0);
    document.getElementById('eqBadge').innerText = '餘額: ' + equity.toFixed(3) + ' USDT';
  }}

  // 更新持倉顯示
  for(const coin of orderCoins()){{
    const p = positions[coin];
    const posEl = document.getElementById('pos_' + coin);
    const detailsEl = document.getElementById('pos_details_' + coin);
    const btn = document.getElementById('close_' + coin);
    
    if(!posEl || !detailsEl || !btn) continue;

    if(p && p.hasPos){{
      const direction = p.posSide === 'long' ? '多' : (p.posSide === 'short' ? '空' : '未知');
      posEl.innerText = `${{direction}} | 張數: ${{p.pos_contracts.toFixed(4)}} | 幣數: ${{p.pos_coins.toFixed(6)}}`;
      detailsEl.innerHTML = `持倉保證金: ${{p.margin.toFixed(3)}} USDT | 均價: ${{p.avgPx.toFixed(2)}} | 槓桿: x${{p.lever}}`;
      detailsEl.innerHTML += ` | 收益率: ${{p.roe.toFixed(3)}}% | 盈虧: ${{p.upl.toFixed(3)}}`;
      btn.disabled = false;
    }} else {{
      posEl.innerText = '無持倉';
      detailsEl.innerHTML = '';
      btn.disabled = true;
    }}
  }}

  // 更新保證金顯示（重新 render）
  renderCards();
}}

(async function main(){{
  await loadConfigOnce();
  await refreshPositions();
  setInterval(refreshPositions, 2000);
}})();
</script>
</body>
</html>
"""


@app.get("/panel")
def panel():
    return Response(PANEL_HTML, mimetype='text/html; charset=utf-8')


if __name__ == "__main__":
    print("START PORT =", APP_PORT)
    print("LIVE (config.json) =", bool(CFG.get("live", False)))
    print("OKX_BASE_URL =", OKX_BASE_URL)
    app.run(host="0.0.0.0", port=APP_PORT)