#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ELITE 數據獵手 — 背景偵測與 Telegram 推送
在 GitHub Actions 上每 5 分鐘跑一次，網頁不用開、電腦不用開機。

偵測三種訊號：
  1. 評分 >= 門檻 的做多/做空推薦（市場結構/動能/費率/多空比/CVD/相對強弱）
  2. 背離訊號（RSI 底背離 / 頂背離）
  3. 持倉異常警報（OI 驟變 / 爆量）

資料來源（全部免費、無需金鑰）：
  - Binance 期貨：行情、K線、多空比、主動買賣量(CVD)
  - OKX：永續合約清單、未平倉量(OI)、資金費率

去重：把已推送的訊號指紋存進 state.json（由 GitHub Actions 快取/commit 保存），
      同一訊號 COOLDOWN_HOURS 小時內不重複推。
"""

import os
import json
import time
import math
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# ════════════════════════════════════════
# 設定（可用環境變數覆蓋，或直接改這裡的預設值）
# ════════════════════════════════════════
TG_TOKEN    = os.environ.get("TG_TOKEN", "")          # Telegram Bot Token
TG_CHAT_ID  = os.environ.get("TG_CHAT_ID", "")        # 你的 Chat ID
SCORE_MIN   = int(os.environ.get("SCORE_MIN", "70"))  # 推薦評分門檻
TOP_N       = int(os.environ.get("TOP_N", "40"))      # 掃描成交量前 N 大幣
COOLDOWN_HOURS = float(os.environ.get("COOLDOWN_HOURS", "4"))  # 同訊號冷卻時間
STATE_FILE  = os.environ.get("STATE_FILE", "state.json")

# 開關：要不要推某類訊號
ENABLE_SCORE      = os.environ.get("ENABLE_SCORE", "1") == "1"
ENABLE_DIVERGENCE = os.environ.get("ENABLE_DIVERGENCE", "1") == "1"
ENABLE_ANOMALY    = os.environ.get("ENABLE_ANOMALY", "1") == "1"

BINANCE_FAPI = "https://fapi.binance.com"
OKX_API      = "https://www.okx.com"

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


# ════════════════════════════════════════
# 資料抓取
# ════════════════════════════════════════
def get_binance_tickers():
    """全市場 24h 行情。回傳 {SYMBOL: {...}}"""
    data = http_get(f"{BINANCE_FAPI}/fapi/v1/ticker/24hr")
    out = {}
    for t in data:
        s = t.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        if "_" in s:  # 過濾交割合約
            continue
        out[s] = {
            "symbol": s,
            "price": float(t.get("lastPrice", 0) or 0),
            "chg24h": float(t.get("priceChangePercent", 0) or 0),
            "vol": float(t.get("quoteVolume", 0) or 0),
            "high": float(t.get("highPrice", 0) or 0),
            "low": float(t.get("lowPrice", 0) or 0),
        }
    return out


def get_okx_swaps():
    """OKX 有永續合約的幣種集合（純符號，如 BTC）"""
    try:
        d = http_get(f"{OKX_API}/api/v5/public/instruments?instType=SWAP")
        s = set()
        for it in d.get("data", []):
            inst = it.get("instId", "")
            if inst.endswith("-USDT-SWAP"):
                s.add(inst.replace("-USDT-SWAP", ""))
        return s
    except Exception as e:
        print("OKX swaps 抓取失敗:", e)
        return set()


def get_okx_oi():
    """OKX 全市場真實未平倉量。回傳 {SYMBOL_USDT: oiUsd}"""
    try:
        d = http_get(f"{OKX_API}/api/v5/public/open-interest?instType=SWAP")
        out = {}
        for it in d.get("data", []):
            inst = it.get("instId", "")
            if inst.endswith("-USDT-SWAP"):
                sym = inst.replace("-USDT-SWAP", "") + "USDT"
                out[sym] = float(it.get("oiUsd", 0) or 0)
        return out
    except Exception as e:
        print("OKX OI 抓取失敗:", e)
        return {}


def get_okx_funding(sym):
    """單幣 OKX 資金費率"""
    try:
        d = http_get(f"{OKX_API}/api/v5/public/funding-rate?instId={sym}-USDT-SWAP")
        data = d.get("data", [])
        if data and data[0].get("fundingRate"):
            return float(data[0]["fundingRate"])
    except Exception:
        pass
    return None


def get_binance_ls(sym):
    """大戶持倉多空比（最近一筆）。回傳 longRatio(0~1) 或 None"""
    try:
        url = f"{BINANCE_FAPI}/futures/data/topLongShortPositionRatio?symbol={sym}USDT&period=15m&limit=1"
        d = http_get(url)
        if d:
            return float(d[-1]["longAccount"])
    except Exception:
        pass
    return None


def get_binance_cvd(sym):
    """近 1 小時 CVD 淨買賣偏向 %。回傳 deltaPct 或 None"""
    try:
        url = f"{BINANCE_FAPI}/futures/data/takerlongshortRatio?symbol={sym}USDT&period=5m&limit=12"
        d = http_get(url)
        if not d:
            return None
        buy = sum(float(k["buyVol"]) for k in d)
        sell = sum(float(k["sellVol"]) for k in d)
        tot = buy + sell
        return (buy - sell) / tot * 100 if tot > 0 else 0
    except Exception:
        return None


def get_klines(sym, interval="1h", limit=100):
    """Binance K線。回傳 [{o,h,l,c,v}] 由舊到新"""
    try:
        url = f"{BINANCE_FAPI}/fapi/v1/klines?symbol={sym}USDT&interval={interval}&limit={limit}"
        d = http_get(url)
        return [{"o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                 "c": float(k[4]), "v": float(k[5])} for k in d]
    except Exception:
        return None


# ════════════════════════════════════════
# 指標計算
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
    """偵測 RSI 背離。回傳 dict 或 None"""
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

    # 底背離
    low_piv = [i for i in find_pivots(lows, 3, 2, "low") if rsi[i] is not None]
    if len(low_piv) >= 2:
        recent = [i for i in low_piv if i >= recent_cut]
        if recent:
            p2 = recent[-1]
            prior = [i for i in low_piv if i <= p2 - 3]
            if prior:
                p1 = min(prior, key=lambda i: lows[i])
                if lows[p2] < lows[p1] and rsi[p2] > rsi[p1] + 1 and rsi[p2] < 55:
                    vol_shrink = vols[p2] < vols[p1]
                    gap = rsi[p2] - rsi[p1]
                    strength = min(100, round(gap * 2.5 + (20 if vol_shrink else 0) +
                                              (15 if rsi[p2] < 35 else 0) + 25))
                    return {"type": "long", "label": "底背離", "emoji": "↗",
                            "strength": strength,
                            "desc": f"價創新低但 RSI 回升（{rsi[p1]:.0f}→{rsi[p2]:.0f}）"
                                    f"{'，量縮確認' if vol_shrink else ''}"}

    # 頂背離
    high_piv = [i for i in find_pivots(highs, 3, 2, "high") if rsi[i] is not None]
    if len(high_piv) >= 2:
        recent = [i for i in high_piv if i >= recent_cut]
        if recent:
            p2 = recent[-1]
            prior = [i for i in high_piv if i <= p2 - 3]
            if prior:
                p1 = max(prior, key=lambda i: highs[i])
                if highs[p2] > highs[p1] and rsi[p2] < rsi[p1] - 1 and rsi[p2] > 45:
                    vol_shrink = vols[p2] < vols[p1]
                    gap = rsi[p1] - rsi[p2]
                    strength = min(100, round(gap * 2.5 + (20 if vol_shrink else 0) +
                                              (15 if rsi[p2] > 65 else 0) + 25))
                    return {"type": "short", "label": "頂背離", "emoji": "↘",
                            "strength": strength,
                            "desc": f"價創新高但 RSI 走弱（{rsi[p1]:.0f}→{rsi[p2]:.0f}）"
                                    f"{'，量縮確認' if vol_shrink else ''}"}
    return None


def compute_score(side, chg24h, oi_chg1h, fr, long_ratio, cvd_pct, rs):
    """還原網頁的 7 項評分（滿分 100）"""
    is_long = side == "long"
    pts = 0

    # 1. 市場結構 OI×價格×CVD (max 30)
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
    pts += s * 2  # max 30

    # 2. 動能 24H (max 15)
    mom = chg24h if is_long else -chg24h
    pts += max(0, min(15, mom * 1.5))

    # 3. 資金費率 (max 10)
    if is_long:
        pts += 10 if fr < -0.005 else 7 if fr < 0 else 4 if fr < 0.005 else 1
    else:
        pts += 10 if fr > 0.01 else 7 if fr > 0.003 else 4 if fr > 0 else 1

    # 4. 多空比 (max 10)
    if is_long:
        pts += 10 if long_ratio < 0.4 else 7 if long_ratio < 0.5 else 4 if long_ratio < 0.6 else 1
    else:
        pts += 10 if long_ratio > 0.6 else 7 if long_ratio > 0.5 else 4 if long_ratio > 0.4 else 1

    # 5. CVD 方向 (max 10)
    if is_long:
        pts += 10 if cvd_pct > 10 else 6 if cvd_pct > 3 else 3 if cvd_pct > 0 else 0
    else:
        pts += 10 if cvd_pct < -10 else 6 if cvd_pct < -3 else 3 if cvd_pct < 0 else 0

    # 6. 相對強弱 vs BTC (max 15)
    r = rs if is_long else -rs
    pts += max(0, min(15, 7.5 + r * 1.5))

    # 7. OI 規模分 (max 10) — 簡化給固定中間值
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
    if not ts:
        return False
    return (time.time() - ts) < COOLDOWN_HOURS * 3600


def mark_pushed(state, key):
    state[key] = time.time()


# ════════════════════════════════════════
# Telegram
# ════════════════════════════════════════
def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠ 未設定 TG_TOKEN / TG_CHAT_ID，略過推送。訊息內容：\n", text)
        return False
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        http_post(url, {
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        })
        return True
    except Exception as e:
        print("Telegram 推送失敗:", e)
        return False


# ════════════════════════════════════════
# 主流程
# ════════════════════════════════════════
def main():
    print("=== ELITE 偵測開始", datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"), "(台北) ===")
    state = load_state()

    tickers = get_binance_tickers()
    print(f"Binance 行情: {len(tickers)} 個")
    okx_swaps = get_okx_swaps()
    print(f"OKX 永續: {len(okx_swaps)} 個")
    okx_oi = get_okx_oi()
    btc_chg = tickers.get("BTCUSDT", {}).get("chg24h", 0)

    # 取成交量前 N 大、OKX 有合約的幣
    cands = [t for s, t in tickers.items()
             if s.replace("USDT", "") in okx_swaps and t["price"] > 0]
    cands.sort(key=lambda t: t["vol"], reverse=True)
    cands = cands[:TOP_N]
    print(f"掃描候選: {len(cands)} 個")

    # OI 變化快照（用 state 裡上次的 OI 比對）
    oi_hist = state.get("_oi_snapshot", {})
    new_oi_snap = {}

    msgs = []  # 要推送的訊息

    for t in cands:
        sym = t["symbol"].replace("USDT", "")
        price = t["price"]
        chg24h = t["chg24h"]
        oi_usd = okx_oi.get(t["symbol"], 0)

        # OI 1H 變化（與上次快照比對；GitHub Actions 每5分鐘跑，約12次=1H）
        oi_chg1h = 0
        prev = oi_hist.get(sym)
        if prev and prev.get("oi", 0) > 0:
            age_min = (time.time() - prev.get("ts", 0)) / 60
            if age_min >= 40:  # 至少40分鐘前的快照
                oi_chg1h = (oi_usd - prev["oi"]) / prev["oi"] * 100
        # 累積快照：間隔≥8分鐘才更新基準
        if not prev or (time.time() - prev.get("ts", 0)) / 60 >= 8:
            new_oi_snap[sym] = {"oi": oi_usd, "ts": time.time()}
        else:
            new_oi_snap[sym] = prev

        # ── 1. 持倉異常警報（OI驟變/爆量）──
        if ENABLE_ANOMALY:
            avg_vol = sum(c["vol"] for c in cands) / max(1, len(cands)) if False else None
            vol_ratio = 0
            # 用 24h 量 vs 候選平均粗估爆量
            allvol = [c["vol"] for c in cands]
            if allvol:
                avgv = sum(allvol) / len(allvol)
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

        # ── 2. 評分推薦 ──
        if ENABLE_SCORE:
            fr = get_okx_funding(sym) or (chg24h * 0.0008)
            lr = get_binance_ls(sym)
            lr = lr if lr is not None else 0.5
            cvd = get_binance_cvd(sym)
            cvd = cvd if cvd is not None else chg24h * 2
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

        # ── 3. 背離訊號 ──
        if ENABLE_DIVERGENCE:
            candles = get_klines(sym, "1h", 100)
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

        time.sleep(0.1)  # 輕微限流

    # 保存 OI 快照
    state["_oi_snapshot"] = new_oi_snap

    # 推送（合併成批，避免洗版；每則之間留空行）
    print(f"本次偵測到 {len(msgs)} 則新訊號")
    if msgs:
        header = f"🎯 <b>ELITE 數據獵手</b> · {datetime.now(timezone(timedelta(hours=8))).strftime('%H:%M')}\n" + "─" * 18
        # 一則訊息最多塞 8 個訊號，避免超過 Telegram 長度上限
        for i in range(0, len(msgs), 8):
            batch = msgs[i:i + 8]
            text = header + "\n\n" + "\n\n".join(batch)
            send_telegram(text)
            time.sleep(1)

    save_state(state)
    print("=== 偵測結束 ===")


if __name__ == "__main__":
    main()
