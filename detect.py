#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ELITE 數據獵手 — 背景偵測與 Telegram 推送（OKX 全資料源版）

【為什麼全用 OKX】
GitHub Actions 伺服器在美國，Binance 期貨 API 封鎖美國 IP（HTTP 451）。
OKX 不封美國 IP，所以雲端跑必須全部用 OKX。
（rubik 統計端點在瀏覽器 file:// 會被 CORS 擋，但從 Python 伺服器抓沒有 CORS 問題）

偵測三種訊號：
  1. 評分 >= 門檻 的做多/做空推薦
  2. 背離訊號（RSI 底背離 / 頂背離）
  3. 持倉異常警報（OI 驟變 / 爆量）
"""

import os
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# ════════════════════════════════════════
# 設定
# ════════════════════════════════════════
TG_TOKEN    = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID  = os.environ.get("TG_CHAT_ID", "")
SCORE_MIN   = int(os.environ.get("SCORE_MIN", "70"))
TOP_N       = int(os.environ.get("TOP_N", "40"))
COOLDOWN_HOURS = float(os.environ.get("COOLDOWN_HOURS", "4"))
STATE_FILE  = os.environ.get("STATE_FILE", "state.json")

ENABLE_SCORE      = os.environ.get("ENABLE_SCORE", "1") == "1"
ENABLE_DIVERGENCE = os.environ.get("ENABLE_DIVERGENCE", "1") == "1"
ENABLE_ANOMALY    = os.environ.get("ENABLE_ANOMALY", "1") == "1"

OKX = "https://www.okx.com"
UA = {"User-Agent": "Mozilla/5.0 (elite-bot)"}


# ════════════════════════════════════════
# HTTP 工具
# ════════════════════════════════════════
def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def http_post(url, data, timeout=15):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def okx_get(path, timeout=15):
    try:
        d = http_get(OKX + path, timeout=timeout)
        if d.get("code") == "0":
            return d.get("data", [])
    except Exception as e:
        print(f"OKX {path[:45]} 失敗: {e}")
    return []


# ════════════════════════════════════════
# 資料抓取（全 OKX）
# ════════════════════════════════════════
def get_tickers():
    data = okx_get("/api/v5/market/tickers?instType=SWAP")
    out = {}
    for t in data:
        inst = t.get("instId", "")
        if not inst.endswith("-USDT-SWAP"):
            continue
        sym = inst.replace("-USDT-SWAP", "")
        last = float(t.get("last", 0) or 0)
        open24 = float(t.get("open24h", 0) or 0)
        chg24h = ((last - open24) / open24 * 100) if open24 > 0 else 0
        out[sym + "USDT"] = {
            "symbol": sym + "USDT", "sym": sym, "inst": inst,
            "price": last, "chg24h": chg24h,
            "vol": float(t.get("volCcy24h", 0) or 0),
            "high": float(t.get("high24h", 0) or 0),
            "low": float(t.get("low24h", 0) or 0),
        }
    return out


def get_oi():
    data = okx_get("/api/v5/public/open-interest?instType=SWAP")
    out = {}
    for it in data:
        inst = it.get("instId", "")
        if inst.endswith("-USDT-SWAP"):
            sym = inst.replace("-USDT-SWAP", "") + "USDT"
            out[sym] = float(it.get("oiUsd", 0) or 0)
    return out


def get_funding(sym):
    data = okx_get(f"/api/v5/public/funding-rate?instId={sym}-USDT-SWAP")
    if data and data[0].get("fundingRate"):
        return float(data[0]["fundingRate"])
    return None


def get_long_short(sym):
    """多空帳戶比。data: [[ts, ratio(多/空)], ...] → 回傳多方占比 0~1"""
    data = okx_get(f"/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy={sym}&period=5m&limit=1")
    if data:
        try:
            ratio = float(data[0][1])
            return ratio / (1 + ratio) if ratio > 0 else 0.5
        except Exception:
            pass
    return None


def get_cvd(sym):
    """主動買賣量近1H淨偏向%。data: [[ts, sellVol, buyVol], ...]"""
    data = okx_get(f"/api/v5/rubik/stat/taker-volume?ccy={sym}&instType=SWAP&period=5m&limit=12")
    if not data:
        return None
    try:
        buy = sum(float(r[2]) for r in data)
        sell = sum(float(r[1]) for r in data)
        tot = buy + sell
        return (buy - sell) / tot * 100 if tot > 0 else 0
    except Exception:
        return None


def get_klines(sym, bar="1H", limit=100):
    data = okx_get(f"/api/v5/market/candles?instId={sym}-USDT-SWAP&bar={bar}&limit={limit}")
    if not data:
        return None
    now = time.time() * 1000
    out = []
    for k in data:
        t = float(k[0])
        if t + 3600000 <= now:
            out.append({"o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                        "c": float(k[4]), "v": float(k[5])})
    out.reverse()
    return out


# ════════════════════════════════════════
# 指標計算（與網頁版一致）
# ════════════════════════════════════════
def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return []
    rsi = [None] * len(closes)
    gain = loss = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gain += d
        else:
            loss -= d
    gain /= period
    loss /= period
    rsi[period] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = d if d >= 0 else 0
        l = -d if d < 0 else 0
        gain = (gain * (period - 1) + g) / period
        loss = (loss * (period - 1) + l) / period
        rsi[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return rsi


def find_pivots(arr, wl=3, wr=2, kind="low"):
    piv = []
    for i in range(wl, len(arr) - wr):
        ok = True
        for j in range(i - wl, i + wr + 1):
            if j == i:
                continue
            if kind == "low" and arr[j] < arr[i]:
                ok = False
                break
            if kind == "high" and arr[j] > arr[i]:
                ok = False
                break
        if ok:
            piv.append(i)
    return piv


def detect_divergence(candles):
    if not candles or len(candles) < 30:
        return None
    closes = [c["c"] for c in candles]
    lows = [c["l"] for c in candles]
    highs = [c["h"] for c in candles]
    vols = [c["v"] for c in candles]
    rsi = calc_rsi(closes, 14)
    if len([x for x in rsi if x is not None]) < 20:
        return None
    n = len(candles)
    recent_cut = n - 10

    low_piv = [i for i in find_pivots(lows, 3, 2, "low") if rsi[i] is not None]
    if len(low_piv) >= 2:
        recent = [i for i in low_piv if i >= recent_cut]
        if recent:
            p2 = recent[-1]
            prior = [i for i in low_piv if i <= p2 - 3]
            if prior:
                p1 = min(prior, key=lambda i: lows[i])
                if lows[p2] < lows[p1] and rsi[p2] > rsi[p1] + 1 and rsi[p2] < 55:
                    vs = vols[p2] < vols[p1]
                    gap = rsi[p2] - rsi[p1]
                    strength = min(100, round(gap * 2.5 + (20 if vs else 0) +
                                              (15 if rsi[p2] < 35 else 0) + 25))
                    return {"type": "long", "label": "底背離", "emoji": "↗",
                            "strength": strength,
                            "desc": f"價創新低但 RSI 回升（{rsi[p1]:.0f}→{rsi[p2]:.0f}）"
                                    f"{'，量縮確認' if vs else ''}"}

    high_piv = [i for i in find_pivots(highs, 3, 2, "high") if rsi[i] is not None]
    if len(high_piv) >= 2:
        recent = [i for i in high_piv if i >= recent_cut]
        if recent:
            p2 = recent[-1]
            prior = [i for i in high_piv if i <= p2 - 3]
            if prior:
                p1 = max(prior, key=lambda i: highs[i])
                if highs[p2] > highs[p1] and rsi[p2] < rsi[p1] - 1 and rsi[p2] > 45:
                    vs = vols[p2] < vols[p1]
                    gap = rsi[p1] - rsi[p2]
                    strength = min(100, round(gap * 2.5 + (20 if vs else 0) +
                                              (15 if rsi[p2] > 65 else 0) + 25))
                    return {"type": "short", "label": "頂背離", "emoji": "↘",
                            "strength": strength,
                            "desc": f"價創新高但 RSI 走弱（{rsi[p1]:.0f}→{rsi[p2]:.0f}）"
                                    f"{'，量縮確認' if vs else ''}"}
    return None


def compute_score(side, chg24h, oi_chg1h, fr, long_ratio, cvd_pct, rs):
    is_long = side == "long"
    pts = 0
    oi_up, oi_dn = oi_chg1h > 0.3, oi_chg1h < -0.3
    cvd_buy, cvd_sell = cvd_pct > 3, cvd_pct < -3
    if is_long:
        if oi_up and chg24h > 0:
            s = 15 if chg24h > 5 else 12
        elif oi_dn and chg24h > 0:
            s = 9
        elif oi_up and chg24h < 0:
            s = 2
        elif oi_dn and chg24h < 0:
            s = 3
        else:
            s = 5 if chg24h > 0 else 2
        if cvd_buy:
            s = min(15, s + 3)
        elif cvd_sell:
            s = max(0, s - 3)
    else:
        if oi_up and chg24h < 0:
            s = 15 if chg24h < -5 else 12
        elif oi_dn and chg24h < 0:
            s = 9
        elif oi_up and chg24h > 0:
            s = 2
        elif oi_dn and chg24h > 0:
            s = 3
        else:
            s = 5 if chg24h < 0 else 2
        if cvd_sell:
            s = min(15, s + 3)
        elif cvd_buy:
            s = max(0, s - 3)
    pts += s * 2
    mom = chg24h if is_long else -chg24h
    pts += max(0, min(15, mom * 1.5))
    if is_long:
        pts += 10 if fr < -0.005 else 7 if fr < 0 else 4 if fr < 0.005 else 1
    else:
        pts += 10 if fr > 0.01 else 7 if fr > 0.003 else 4 if fr > 0 else 1
    if is_long:
        pts += 10 if long_ratio < 0.4 else 7 if long_ratio < 0.5 else 4 if long_ratio < 0.6 else 1
    else:
        pts += 10 if long_ratio > 0.6 else 7 if long_ratio > 0.5 else 4 if long_ratio > 0.4 else 1
    if is_long:
        pts += 10 if cvd_pct > 10 else 6 if cvd_pct > 3 else 3 if cvd_pct > 0 else 0
    else:
        pts += 10 if cvd_pct < -10 else 6 if cvd_pct < -3 else 3 if cvd_pct < 0 else 0
    r = rs if is_long else -rs
    pts += max(0, min(15, 7.5 + r * 1.5))
    pts += 6
    return round(min(100, pts))


# ════════════════════════════════════════
# 去重狀態
# ════════════════════════════════════════
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        print("save_state 失敗:", e)


def already_pushed(state, key):
    ts = state.get(key)
    return bool(ts) and (time.time() - ts) < COOLDOWN_HOURS * 3600


def mark_pushed(state, key):
    state[key] = time.time()


# ════════════════════════════════════════
# Telegram
# ════════════════════════════════════════
def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠ 未設定 TG_TOKEN / TG_CHAT_ID，略過推送。內容：\n", text)
        return False
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        http_post(url, {
            "chat_id": TG_CHAT_ID, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        })
        return True
    except Exception as e:
        print("Telegram 推送失敗:", e)
        return False


# ════════════════════════════════════════
# 主流程
# ════════════════════════════════════════
def main():
    now_tpe = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    print(f"=== ELITE 偵測開始 {now_tpe} (台北) ===")
    state = load_state()

    tickers = get_tickers()
    print(f"OKX 行情: {len(tickers)} 個 SWAP")
    if not tickers:
        print("⚠ 抓不到行情，結束")
        return
    oi_map = get_oi()
    btc_chg = tickers.get("BTCUSDT", {}).get("chg24h", 0)

    cands = [t for t in tickers.values() if t["price"] > 0]
    cands.sort(key=lambda t: t["vol"], reverse=True)
    cands = cands[:TOP_N]
    print(f"掃描候選: {len(cands)} 個")

    oi_hist = state.get("_oi_snapshot", {})
    new_oi_snap = {}
    msgs = []

    for t in cands:
        sym = t["sym"]
        price = t["price"]
        chg24h = t["chg24h"]
        oi_usd = oi_map.get(t["symbol"], 0)

        oi_chg1h = 0
        prev = oi_hist.get(sym)
        if prev and prev.get("oi", 0) > 0:
            if (time.time() - prev.get("ts", 0)) / 60 >= 40:
                oi_chg1h = (oi_usd - prev["oi"]) / prev["oi"] * 100
        if not prev or (time.time() - prev.get("ts", 0)) / 60 >= 8:
            new_oi_snap[sym] = {"oi": oi_usd, "ts": time.time()}
        else:
            new_oi_snap[sym] = prev

        # 1. 持倉異常
        if ENABLE_ANOMALY:
            allvol = [c["vol"] for c in cands]
            avgv = sum(allvol) / len(allvol) if allvol else 0
            vol_ratio = t["vol"] / avgv if avgv > 0 else 0
            anomalies = []
            if abs(oi_chg1h) > 8:
                anomalies.append(f"OI 1H {oi_chg1h:+.1f}%")
            rng = (t["high"] - t["low"]) / t["low"] * 100 if t["low"] > 0 else 0
            if rng > 12 and vol_ratio > 1.5:
                anomalies.append(f"日內波幅 {rng:.1f}%")
            if anomalies:
                key = f"anomaly:{sym}:{'/'.join(a.split()[0] for a in anomalies)}"
                if not already_pushed(state, key):
                    side_txt = "🟢看漲" if chg24h >= 0 else "🔴看跌"
                    msgs.append(
                        f"⚠️ <b>持倉異常 · {sym}</b> {side_txt}\n"
                        f"價格 ${price:g}（24H {chg24h:+.2f}%）\n"
                        f"異常：{' / '.join(anomalies)}\n"
                        f"<a href=\"https://www.tradingview.com/chart/?symbol=OKX:{sym}USDT.P\">📈 TradingView</a>"
                    )
                    mark_pushed(state, key)

        # 2. 評分推薦
        if ENABLE_SCORE:
            fr = get_funding(sym)
            fr = fr if fr is not None else (chg24h * 0.0008)
            lr = get_long_short(sym)
            lr = lr if lr is not None else 0.5
            cvd = get_cvd(sym)
            cvd = cvd if cvd is not None else (chg24h * 2)
            rs = chg24h - btc_chg
            side = "long" if chg24h >= 0 else "short"
            score = compute_score(side, chg24h, oi_chg1h, fr, lr, cvd, rs)
            if score >= SCORE_MIN:
                key = f"score:{sym}:{side}"
                if not already_pushed(state, key):
                    side_txt = "🟢 做多推薦" if side == "long" else "🔴 做空推薦"
                    msgs.append(
                        f"{side_txt} · <b>{sym}</b>（評分 {score}）\n"
                        f"價格 ${price:g}（24H {chg24h:+.2f}%）\n"
                        f"OI 1H {oi_chg1h:+.1f}% · 費率 {fr*100:+.3f}% · "
                        f"多空 {lr*100:.0f}% · CVD {cvd:+.1f}%\n"
                        f"<a href=\"https://www.tradingview.com/chart/?symbol=OKX:{sym}USDT.P\">📈 TradingView</a>"
                    )
                    mark_pushed(state, key)

        # 3. 背離
        if ENABLE_DIVERGENCE:
            candles = get_klines(sym, "1H", 100)
            div = detect_divergence(candles)
            if div and div["strength"] >= 50:
                key = f"div:{sym}:{div['type']}"
                if not already_pushed(state, key):
                    side_txt = "🟢 做多參考" if div["type"] == "long" else "🔴 做空參考"
                    msgs.append(
                        f"{div['emoji']} <b>{div['label']} · {sym}</b>（強度 {div['strength']}）{side_txt}\n"
                        f"價格 ${price:g}（24H {chg24h:+.2f}%）\n"
                        f"{div['desc']}\n"
                        f"<a href=\"https://www.tradingview.com/chart/?symbol=OKX:{sym}USDT.P\">📈 TradingView</a>"
                    )
                    mark_pushed(state, key)

        time.sleep(0.12)

    state["_oi_snapshot"] = new_oi_snap

    print(f"本次偵測到 {len(msgs)} 則新訊號")
    if msgs:
        header = f"🎯 <b>ELITE 數據獵手</b> · {datetime.now(timezone(timedelta(hours=8))).strftime('%H:%M')}\n" + "─" * 18
        for i in range(0, len(msgs), 8):
            send_telegram(header + "\n\n" + "\n\n".join(msgs[i:i + 8]))
            time.sleep(1)

    save_state(state)
    print("=== 偵測結束 ===")


if __name__ == "__main__":
    main()
