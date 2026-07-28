"""
CryptoScalper AJ — MEXC Futures Multi-Confluence Signal Engine
Flask dashboard + 24/7 background scanner/monitor + Telegram alerts.

Run:  python main.py
Deploy on Render with the included Procfile.
"""
import os
import json
import time
import threading
from datetime import datetime, timezone

import requests
import ccxt
import pandas as pd
import numpy as np
from flask import Flask, render_template_string, request, jsonify, send_from_directory

app = Flask(__name__)

# ==========================================================
# 1. GLOBAL CONFIG & STATE
# ==========================================================
BOT_CONFIG = {
    "bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "channel": os.getenv("TELEGRAM_CHANNEL", ""),
    "mexc_key": os.getenv("MEXC_API_KEY", ""),
    "mexc_secret": os.getenv("MEXC_SECRET_KEY", ""),
    "broadcast_enabled": True,
    "autotrade_enabled": False,
    "symbols": ["BTC/USDT"],
    "timeframes": ["5m", "15m"],
    "scan_interval": 60,     # seconds between scans
    "order_qty": 0.001,      # contract size used for auto-trading (BTC)
}

TRADE_HISTORY = []           # list of signal/trade dicts, newest first
SIGNAL_COUNTER = 0           # for "Signal-01", "Signal-02" ...
STATE_LOCK = threading.Lock()

CHARTS_DIR = os.path.join(os.getcwd(), "generated_charts")
os.makedirs(CHARTS_DIR, exist_ok=True)

DATA_DIR = os.path.join(os.getcwd(), "data")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "state.json")

# Tiny TTL cache for "live price" lookups used by the dashboard, so page
# renders don't hammer the exchange with a REST call per open signal.
_PRICE_CACHE = {}
_PRICE_CACHE_TTL = 5  # seconds


def load_state():
    global BOT_CONFIG, TRADE_HISTORY, SIGNAL_COUNTER
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            BOT_CONFIG.update(data.get("config", {}))
            TRADE_HISTORY.extend(data.get("trades", []))
            SIGNAL_COUNTER = data.get("signal_counter", 0)
            print(f"[state] restored {len(TRADE_HISTORY)} trades, counter={SIGNAL_COUNTER}")
        except Exception as e:
            print(f"[state] load error: {e}")


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "config": BOT_CONFIG,
                "trades": TRADE_HISTORY,
                "signal_counter": SIGNAL_COUNTER
            }, f)
    except Exception as e:
        print(f"[state] save error: {e}")


def get_mexc_client():
    return ccxt.mexc({
        "apiKey": BOT_CONFIG.get("mexc_key", ""),
        "secret": BOT_CONFIG.get("mexc_secret", ""),
        "options": {"defaultType": "swap"},
        "enableRateLimit": True,
    })


def next_signal_id():
    global SIGNAL_COUNTER
    SIGNAL_COUNTER += 1
    return f"Signal-{SIGNAL_COUNTER:02d}"


def get_live_price(symbol):
    now = time.time()
    cached = _PRICE_CACHE.get(symbol)
    if cached and (now - cached[1]) < _PRICE_CACHE_TTL:
        return cached[0]
    try:
        client = get_mexc_client()
        ticker = client.fetch_ticker(symbol)
        price = ticker.get("last")
        if price is not None:
            _PRICE_CACHE[symbol] = (price, now)
        return price
    except Exception as e:
        print(f"[price] fetch error for {symbol}: {e}")
        return cached[0] if cached else None


# ==========================================================
# 2. INDICATOR / STRUCTURE HELPERS
# ==========================================================
def fetch_df(client, symbol, timeframe, limit=150):
    ohlcv = client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
    df["time_sec"] = df["time"] // 1000
    return df


def atr(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def detect_trend(df, lookback=30):
    """Very simple structure bias: compare the two most recent swing extremes."""
    recent = df.tail(lookback).reset_index(drop=True)
    low_idx = int(recent["low"].idxmin())
    high_idx = int(recent["high"].idxmax())
    bullish = high_idx > low_idx
    return "LONG" if bullish else "SHORT", low_idx, high_idx, recent


def detect_bos(df, lookback=40):
    """
    Break of Structure: does the latest close break beyond the prior
    swing high (bullish BOS) or swing low (bearish BOS)?
    """
    recent = df.tail(lookback).reset_index(drop=True)
    if len(recent) < 10:
        return None
    prior = recent.iloc[:-5]
    last_close = recent["close"].iloc[-1]
    prior_high = prior["high"].max()
    prior_low = prior["low"].min()
    if last_close > prior_high:
        return "LONG"
    if last_close < prior_low:
        return "SHORT"
    return None


def find_order_block(df_slice, bullish=True):
    if bullish:
        down = df_slice[df_slice["close"] < df_slice["open"]]
        c = down.iloc[-1] if not down.empty else df_slice.iloc[0]
    else:
        up = df_slice[df_slice["close"] > df_slice["open"]]
        c = up.iloc[-1] if not up.empty else df_slice.iloc[0]
    return float(c["high"]), float(c["low"])


def find_fvg(df_slice, bullish=True):
    rows = df_slice.reset_index(drop=True)
    gap = None
    for i in range(1, len(rows) - 1):
        prev_c = rows.iloc[i - 1]
        next_c = rows.iloc[i + 1]
        if bullish and prev_c["high"] < next_c["low"]:
            gap = (float(prev_c["high"]), float(next_c["low"]))
        elif not bullish and prev_c["low"] > next_c["high"]:
            gap = (float(next_c["high"]), float(prev_c["low"]))
    return gap


def volume_burst(df, factor=1.8):
    vol = df["volume"]
    sma20 = vol.rolling(20).mean()
    if sma20.iloc[-1] == 0 or pd.isna(sma20.iloc[-1]):
        return False
    return bool(vol.iloc[-1] >= factor * sma20.iloc[-1])


# ==========================================================
# 3. 5-POINT TRAP FILTER  (all 5 must pass)
# ==========================================================
def filter_liquidity_sweep(df, bullish, lookback=20):
    """A stop-hunt wick beyond the prior swing extreme, closing back inside."""
    recent = df.tail(lookback).reset_index(drop=True)
    if len(recent) < 5:
        return False
    prior = recent.iloc[:-3]
    last_few = recent.iloc[-3:]
    if bullish:
        prior_low = prior["low"].min()
        swept = (last_few["low"] < prior_low).any()
        closed_back_inside = last_few["close"].iloc[-1] > prior_low
        return bool(swept and closed_back_inside)
    else:
        prior_high = prior["high"].max()
        swept = (last_few["high"] > prior_high).any()
        closed_back_inside = last_few["close"].iloc[-1] < prior_high
        return bool(swept and closed_back_inside)


def filter_unmitigated_ob(df, ob_top, ob_bottom, ob_end_idx, bullish):
    """OB is 'fresh' if price hasn't already traded back through the zone
    since it formed (excluding the current retest candle)."""
    after = df.iloc[ob_end_idx + 1: -1]  # exclude the OB candle itself and the live candle
    if after.empty:
        return True
    touched = ((after["low"] <= ob_top) & (after["high"] >= ob_bottom)).any()
    return not bool(touched)


def filter_mtf_alignment(bias_5m, bias_15m):
    return bias_5m == bias_15m


def filter_retracement_speed(df, lookback=8, max_big_candles=2):
    """Reject if the pullback into the zone was a handful of oversized candles
    (a 'crash') rather than a gradual multi-candle retrace."""
    recent = df.tail(lookback)
    body = (recent["close"] - recent["open"]).abs()
    candle_range = (recent["high"] - recent["low"]).replace(0, np.nan)
    avg_range = candle_range.mean()
    if pd.isna(avg_range) or avg_range == 0:
        return True
    big_candles = (candle_range > 1.8 * avg_range).sum()
    return bool(big_candles <= max_big_candles)


def filter_fvg_clearance(df, fvg, bullish):
    """Require that price actually traded into/through the FVG on the way
    to the current zone (i.e. the gap was 'cleared', not skipped)."""
    if not fvg:
        return False
    fvg_bottom, fvg_top = fvg
    recent = df.tail(15)
    traded_into_gap = ((recent["low"] <= fvg_top) & (recent["high"] >= fvg_bottom)).any()
    return bool(traded_into_gap)


def run_trap_filter(df_5m, df_15m, bias_5m, bias_15m, ob_top, ob_bottom, ob_end_idx, fvg):
    bullish = bias_5m == "LONG"
    results = {
        "liquidity_sweep": filter_liquidity_sweep(df_5m, bullish),
        "unmitigated_ob": filter_unmitigated_ob(df_5m, ob_top, ob_bottom, ob_end_idx, bullish),
        "mtf_alignment": filter_mtf_alignment(bias_5m, bias_15m),
        "retracement_speed": filter_retracement_speed(df_5m),
        "fvg_clearance": filter_fvg_clearance(df_5m, fvg, bullish),
    }
    results["all_passed"] = all(results.values())
    return results


# ==========================================================
# 4. STRATEGY ENGINES
# ==========================================================
def strategy_fib_ote(df_5m, bias_5m, lookback=30):
    """
    Strategy 1: Fib 5-Level OTE.
    Clean impulse leg (>=70% directional candles, range >= 1.5x ATR),
    entry on deep pullback into the 0.71-0.786 zone.
    """
    recent = df_5m.tail(lookback).reset_index(drop=True)
    bullish = bias_5m == "LONG"
    low_idx = int(recent["low"].idxmin())
    high_idx = int(recent["high"].idxmax())
    leg = recent.iloc[min(low_idx, high_idx): max(low_idx, high_idx) + 1]

    if len(leg) < 4:
        return None

    directional = leg[leg["close"] > leg["open"]] if bullish else leg[leg["close"] < leg["open"]]
    directional_ratio = len(directional) / len(leg)

    leg_range = leg["high"].max() - leg["low"].min()
    avg_atr = atr(df_5m).iloc[-1]
    if pd.isna(avg_atr) or avg_atr == 0:
        return None

    clean_impulse = directional_ratio >= 0.70 and leg_range >= 1.5 * avg_atr
    if not clean_impulse:
        return None

    fib_0 = leg["high"].max() if bullish else leg["low"].min()
    fib_1 = leg["low"].min() if bullish else leg["high"].max()
    diff = abs(fib_0 - fib_1)
    if diff == 0:
        return None

    zone_71 = fib_0 - 0.71 * (fib_0 - fib_1) if bullish else fib_0 + 0.71 * (fib_1 - fib_0)
    zone_786 = fib_0 - 0.786 * (fib_0 - fib_1) if bullish else fib_0 + 0.786 * (fib_1 - fib_0)

    last_close = df_5m["close"].iloc[-1]
    zone_lo, zone_hi = sorted([zone_71, zone_786])
    in_zone = zone_lo <= last_close <= zone_hi
    if not in_zone:
        return None

    # Reversal candle confirmation: last candle closes back in the trade direction
    last_candle = df_5m.iloc[-1]
    reversal_ok = (last_candle["close"] > last_candle["open"]) if bullish else \
                  (last_candle["close"] < last_candle["open"])
    if not reversal_ok:
        return None

    tp2_ext = fib_0 + (fib_0 - fib_1) if bullish else fib_0 - (fib_1 - fib_0)

    return {
        "strategy": "Fib 5-Level OTE",
        "entry": round(last_close, 2),
        "sl": round(fib_1, 2),
        "tp1": round(fib_0, 2),
        "tp2": round(tp2_ext, 2),
        "fib_0": round(fib_0, 2),
        "fib_1": round(fib_1, 2),
        "ob_top": round(zone_hi, 6),
        "ob_bottom": round(zone_lo, 6),
        "fvg": None,
        "ob_end_idx": max(low_idx, high_idx),
    }


def strategy_smc_ob_bos(df_5m, bias_5m, lookback=40):
    """
    Strategy 2: SMC Order Block + Break of Structure.
    Entry on OB retrace with a strong rejection candle, confluence =
    volume burst + liquidity sweep (sweep is verified later by the trap filter).
    """
    bullish = bias_5m == "LONG"
    bos_dir = detect_bos(df_5m, lookback=lookback)
    if bos_dir != bias_5m:
        return None

    recent = df_5m.tail(lookback).reset_index(drop=True)
    low_idx = int(recent["low"].idxmin())
    high_idx = int(recent["high"].idxmax())

    if bullish:
        ob_slice = recent.iloc[max(0, low_idx - 3): low_idx + 1]
        ob_top, ob_bottom = find_order_block(ob_slice, bullish=True)
        ob_end_idx = low_idx
    else:
        ob_slice = recent.iloc[max(0, high_idx - 3): high_idx + 1]
        ob_top, ob_bottom = find_order_block(ob_slice, bullish=False)
        ob_end_idx = high_idx

    last_close = df_5m["close"].iloc[-1]
    in_ob_zone = ob_bottom <= last_close <= ob_top
    if not in_ob_zone:
        return None

    last_candle = df_5m.iloc[-1]
    body = abs(last_candle["close"] - last_candle["open"])
    candle_range = max(last_candle["high"] - last_candle["low"], 1e-9)
    strong_rejection = (body / candle_range) >= 0.5 and (
        (last_candle["close"] > last_candle["open"]) if bullish
        else (last_candle["close"] < last_candle["open"])
    )
    if not strong_rejection:
        return None

    if not volume_burst(df_5m):
        return None

    move_slice = recent.iloc[min(low_idx, high_idx): max(low_idx, high_idx) + 1]
    fvg = find_fvg(move_slice, bullish=bullish)

    recent_high = recent["high"].max()
    recent_low = recent["low"].min()
    diff = recent_high - recent_low
    tp1 = recent_high if bullish else recent_low
    tp2 = recent_high + diff * 0.5 if bullish else recent_low - diff * 0.5
    sl = ob_bottom if bullish else ob_top

    return {
        "strategy": "SMC Order Block + BOS",
        "entry": round(last_close, 2),
        "sl": round(sl, 2),
        "tp1": round(tp1, 2),
        "tp2": round(tp2, 2),
        "fib_0": None,
        "fib_1": None,
        "ob_top": round(ob_top, 6),
        "ob_bottom": round(ob_bottom, 6),
        "fvg": fvg,
        "ob_end_idx": ob_end_idx,
    }


# ==========================================================
# 5. SIGNAL BUILDER (ties strategies + trap filter together)
# ==========================================================
def build_signal(symbol):
    """
    Runs both strategies on the 5m/15m pair for `symbol`. Returns a full
    trade dict only if a strategy fires AND all 5 trap filters pass.
    """
    try:
        client = get_mexc_client()
        df_5m = fetch_df(client, symbol, "5m", limit=150)
        df_15m = fetch_df(client, symbol, "15m", limit=150)

        bias_5m, _, _, _ = detect_trend(df_5m)
        bias_15m, _, _, _ = detect_trend(df_15m)

        setup = strategy_fib_ote(df_5m, bias_5m) or strategy_smc_ob_bos(df_5m, bias_5m)
        if not setup:
            return None, None

        trap = run_trap_filter(
            df_5m, df_15m, bias_5m, bias_15m,
            setup["ob_top"], setup["ob_bottom"], setup["ob_end_idx"], setup["fvg"]
        )
        if not trap["all_passed"]:
            return None, None

        rr = None
        try:
            risk = abs(setup["entry"] - setup["sl"])
            reward = abs(setup["tp1"] - setup["entry"])
            rr = round(reward / risk, 2) if risk > 0 else None
        except Exception:
            pass

        trade = {
            "id_str": next_signal_id(),
            "symbol": symbol,
            "tf": "5M",
            "side": "🟢 LONG" if bias_5m == "LONG" else "🔴 SHORT",
            "bullish": bias_5m == "LONG",
            "strategy": setup["strategy"],
            "entry": setup["entry"],
            "sl": setup["sl"],
            "tp1": setup["tp1"],
            "tp2": setup["tp2"],
            "rr": rr,
            "ob_top": setup["ob_top"],
            "ob_bottom": setup["ob_bottom"],
            "fib_0": setup["fib_0"],
            "fib_1": setup["fib_1"],
            "fvg": setup["fvg"],
            "trap_filter": trap,
            "status": "PENDING",       # PENDING -> ACTIVE -> TP1_HIT -> WIN/LOSS
            "tp1_hit": False,
            "pnl": 0.0,
            "chart_img": None,
            "tg_msg_id": None,
            "order_id": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return trade, df_5m
    except Exception as e:
        print(f"[build_signal] error for {symbol}: {e}")
        return None, None


# ==========================================================
# 6. CHART SNAPSHOT (1920x1080 dark-mode via QuickChart)
# ==========================================================
def generate_chart(trade, df_5m):
    visible = df_5m.tail(110).reset_index(drop=True)
    chart_data = [{
        "x": time.strftime("%H:%M", time.localtime(row["time_sec"])),
        "o": row["open"], "h": row["high"], "l": row["low"], "c": row["close"]
    } for _, row in visible.iterrows()]

    bullish = trade["bullish"]
    up_color, down_color = "#089981", "#f23645"
    ob_color, ob_border = "rgba(41,98,255,0.18)", "rgba(41,98,255,0.65)"
    fvg_color, fvg_border = "rgba(255,193,7,0.15)", "rgba(255,193,7,0.55)"

    annotations = [
        {
            "drawTime": "beforeDatasetsDraw", "type": "box", "yScaleID": "yAxes",
            "yMin": trade["ob_bottom"], "yMax": trade["ob_top"],
            "backgroundColor": ob_color, "borderColor": ob_border, "borderWidth": 1,
            "label": {"content": "OB", "enabled": True, "position": "left", "fontColor": "#8fb8ff"}
        },
        {
            "type": "line", "mode": "horizontal", "scaleID": "yAxes",
            "value": trade["tp1"], "borderColor": up_color if bullish else down_color,
            "borderWidth": 2, "borderDash": [4, 4],
            "label": {"content": f"TP1: {trade['tp1']}", "enabled": True, "position": "right",
                      "backgroundColor": up_color if bullish else down_color}
        },
        {
            "type": "line", "mode": "horizontal", "scaleID": "yAxes",
            "value": trade["tp2"], "borderColor": up_color if bullish else down_color,
            "borderWidth": 1, "borderDash": [2, 2],
            "label": {"content": f"TP2: {trade['tp2']}", "enabled": True, "position": "right",
                      "backgroundColor": up_color if bullish else down_color}
        },
        {
            "type": "line", "mode": "horizontal", "scaleID": "yAxes",
            "value": trade["entry"], "borderColor": "#2962ff", "borderWidth": 2,
            "label": {"content": f"Entry: {trade['entry']}", "enabled": True, "position": "right",
                      "backgroundColor": "#2962ff"}
        },
        {
            "type": "line", "mode": "horizontal", "scaleID": "yAxes",
            "value": trade["sl"], "borderColor": down_color if bullish else up_color, "borderWidth": 2,
            "borderDash": [4, 4],
            "label": {"content": f"SL: {trade['sl']}", "enabled": True, "position": "right",
                      "backgroundColor": down_color if bullish else up_color}
        },
    ]

    if trade.get("fib_0") is not None and trade.get("fib_1") is not None:
        annotations.append({
            "type": "line", "mode": "horizontal", "scaleID": "yAxes",
            "value": trade["fib_0"], "borderColor": "#9c27b0", "borderWidth": 1, "borderDash": [1, 3],
            "label": {"content": "Fib 0.0", "enabled": True, "position": "left", "backgroundColor": "#9c27b0"}
        })
        annotations.append({
            "type": "line", "mode": "horizontal", "scaleID": "yAxes",
            "value": trade["fib_1"], "borderColor": "#9c27b0", "borderWidth": 1, "borderDash": [1, 3],
            "label": {"content": "Fib 1.0", "enabled": True, "position": "left", "backgroundColor": "#9c27b0"}
        })

    if trade.get("fvg"):
        fvg_bottom, fvg_top = trade["fvg"]
        annotations.append({
            "drawTime": "beforeDatasetsDraw", "type": "box", "yScaleID": "yAxes",
            "yMin": fvg_bottom, "yMax": fvg_top,
            "backgroundColor": fvg_color, "borderColor": fvg_border, "borderWidth": 1,
            "label": {"content": "FVG", "enabled": True, "position": "left", "fontColor": "#ffd76a"}
        })

    chart_config = {
        "type": "candlestick",
        "data": {"datasets": [{
            "label": f"{trade['symbol']} {trade['tf']}",
            "data": chart_data,
            "color": {"up": up_color, "down": down_color, "unchanged": "#999999"}
        }]},
        "options": {
            "backgroundColor": "#0b0f19",
            "legend": {"display": False},
            "title": {
                "display": True,
                "text": f"{trade['id_str']} · {trade['symbol']} · {trade['strategy']}",
                "fontColor": "#e5e7eb", "fontSize": 16
            },
            "scales": {
                "xAxes": [{"gridLines": {"color": "#1f2937"}, "ticks": {"fontColor": "#848e9c"}}],
                "yAxes": [{"id": "yAxes", "position": "right",
                           "gridLines": {"color": "#1f2937"}, "ticks": {"fontColor": "#848e9c"}}]
            },
            "plugins": {"annotation": {"annotations": annotations}}
        }
    }

    payload = {
        "backgroundColor": "#0b0f19", "width": 1920, "height": 1080,
        "format": "png", "version": "2.9.4", "chart": chart_config
    }

    filename = f"{trade['id_str']}_{int(time.time())}.png"
    filepath = os.path.join(CHARTS_DIR, filename)
    try:
        res = requests.post("https://quickchart.io/chart", json=payload, timeout=20)
        if res.status_code == 200:
            with open(filepath, "wb") as f:
                f.write(res.content)
            return filename, filepath
        print(f"[chart] HTTP {res.status_code}: {res.text[:300]}")
    except Exception as e:
        print(f"[chart] error: {e}")
    return None, None


# ==========================================================
# 7. TELEGRAM
# ==========================================================
def telegram_send_photo(chart_path, caption):
    url = f"https://api.telegram.org/bot{BOT_CONFIG['bot_token']}/sendPhoto"
    with open(chart_path, "rb") as photo:
        res = requests.post(
            url,
            data={"chat_id": BOT_CONFIG["channel"], "caption": caption, "parse_mode": "HTML"},
            files={"photo": photo}, timeout=25
        )
    return res.json()


def telegram_send_message(text, reply_to=None):
    url = f"https://api.telegram.org/bot{BOT_CONFIG['bot_token']}/sendMessage"
    payload = {"chat_id": BOT_CONFIG["channel"], "text": text, "parse_mode": "HTML"}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    res = requests.post(url, data=payload, timeout=12)
    return res.json()


def send_signal_alert(trade, chart_path=None):
    if not BOT_CONFIG["broadcast_enabled"] or not BOT_CONFIG["bot_token"] or not BOT_CONFIG["channel"]:
        return None

    trap = trade["trap_filter"]
    trap_badges = "".join("✅" if trap[k] else "❌" for k in
                           ["liquidity_sweep", "unmitigated_ob", "mtf_alignment",
                            "retracement_speed", "fvg_clearance"])

    msg = (
        f"⚡ <b>{trade['id_str']} | {trade['symbol']} ({trade['tf']})</b>\n"
        f"<b>Strategy:</b> {trade['strategy']}\n"
        f"<b>Direction:</b> {trade['side']}\n"
        f"<b>Trap Filter:</b> {trap_badges} (5/5 passed)\n\n"
        f"🎯 <b>ENTRY:</b> ${trade['entry']}\n"
        f"🛑 <b>SL:</b> ${trade['sl']}\n"
        f"🚀 <b>TP1:</b> ${trade['tp1']}\n"
        f"🚀 <b>TP2:</b> ${trade['tp2']}\n"
        f"📊 <b>R:R:</b> 1:{trade['rr']}\n\n"
        f"🧱 <b>Status:</b> PENDING (limit order)"
    )
    try:
        if chart_path and os.path.exists(chart_path):
            res_json = telegram_send_photo(chart_path, msg)
        else:
            res_json = telegram_send_message(msg)
        if res_json.get("ok"):
            return res_json["result"]["message_id"]
        print(f"[telegram] send failed: {res_json}")
    except Exception as e:
        print(f"[telegram] error: {e}")
    return None


def send_status_update(trade, event):
    """event in {'ENTRY','TP1','TP2','SL'}"""
    if not BOT_CONFIG["bot_token"] or not BOT_CONFIG["channel"] or not trade.get("tg_msg_id"):
        return
    texts = {
        "ENTRY": f"🔔 <b>ENTRY HIT</b> — {trade['id_str']} is now ACTIVE at ${trade['entry']}",
        "TP1": f"🎯 <b>TP1 HIT</b> — {trade['id_str']} secured partial profit! SL moved to breakeven.",
        "TP2": f"🎯 <b>TP2 HIT</b> — {trade['id_str']} closed full target! 🚀",
        "SL": f"🛑 <b>SL HIT</b> — {trade['id_str']} closed at stop loss. Risk managed.",
    }
    try:
        telegram_send_message(texts.get(event, f"Update: {event}"), reply_to=trade["tg_msg_id"])
    except Exception as e:
        print(f"[telegram] status update error: {e}")


def test_telegram_alert():
    if not BOT_CONFIG["bot_token"] or not BOT_CONFIG["channel"]:
        return False, "Bot token / channel not set."
    try:
        res_json = telegram_send_message("✅ CryptoScalper AJ — test alert. Your bot is connected.")
        if res_json.get("ok"):
            return True, "Test alert sent."
        return False, str(res_json)
    except Exception as e:
        return False, str(e)


# ==========================================================
# 8. MEXC AUTO-EXECUTION (optional — only if enabled + keys set)
# ==========================================================
def place_mexc_order(trade):
    if not BOT_CONFIG.get("autotrade_enabled"):
        return None
    if not BOT_CONFIG.get("mexc_key") or not BOT_CONFIG.get("mexc_secret"):
        return None
    try:
        client = get_mexc_client()
        side = "buy" if trade["bullish"] else "sell"
        qty = BOT_CONFIG.get("order_qty", 0.001)
        order = client.create_order(
            trade["symbol"], "limit", side, qty, trade["entry"],
            params={
                "stopLossPrice": trade["sl"],
                "takeProfitPrice": trade["tp1"],
            }
        )
        return order.get("id")
    except Exception as e:
        print(f"[mexc] order placement error: {e}")
        return None


# ==========================================================
# 9. BACKGROUND ENGINE (scanner + live monitor)
# ==========================================================
def scanner_loop():
    """Runs forever. Scans configured symbols, and if a clean setup passes
    the full 5-point trap filter, opens a new PENDING signal (max one
    open/pending signal per symbol at a time)."""
    while True:
        try:
            for symbol in BOT_CONFIG.get("symbols", ["BTC/USDT"]):
                with STATE_LOCK:
                    has_open = any(
                        t["symbol"] == symbol and t["status"] in ("PENDING", "ACTIVE", "TP1_HIT")
                        for t in TRADE_HISTORY
                    )
                if has_open:
                    continue

                trade, df_5m = build_signal(symbol)
                if not trade:
                    continue

                img_name, img_path = generate_chart(trade, df_5m)
                trade["chart_img"] = img_name
                trade["tg_msg_id"] = send_signal_alert(trade, img_path)
                trade["order_id"] = place_mexc_order(trade)

                with STATE_LOCK:
                    TRADE_HISTORY.insert(0, trade)
                    save_state()
                print(f"[scanner] {trade['id_str']} {trade['side']} {symbol} via {trade['strategy']}")
        except Exception as e:
            print(f"[scanner] loop error: {e}")
        time.sleep(max(20, int(BOT_CONFIG.get("scan_interval", 60))))


def monitor_loop():
    """Runs forever. Tracks live price against PENDING/ACTIVE/TP1_HIT trades
    and progresses their lifecycle, posting Telegram status replies."""
    while True:
        try:
            with STATE_LOCK:
                open_trades = [t for t in TRADE_HISTORY if t["status"] in ("PENDING", "ACTIVE", "TP1_HIT")]
            for t in open_trades:
                price = get_live_price(t["symbol"])
                if price is None:
                    continue
                bullish = t["bullish"]
                changed = False

                if t["status"] == "PENDING":
                    entered = (price <= t["entry"]) if bullish else (price >= t["entry"])
                    if entered:
                        t["status"] = "ACTIVE"
                        send_status_update(t, "ENTRY")
                        changed = True

                elif t["status"] in ("ACTIVE", "TP1_HIT"):
                    hit_sl = (price <= t["sl"]) if bullish else (price >= t["sl"])
                    hit_tp1 = (price >= t["tp1"]) if bullish else (price <= t["tp1"])
                    hit_tp2 = (price >= t["tp2"]) if bullish else (price <= t["tp2"])

                    if t["status"] == "ACTIVE" and hit_tp1 and not t["tp1_hit"]:
                        t["tp1_hit"] = True
                        t["status"] = "TP1_HIT"
                        t["sl"] = t["entry"]  # move SL to breakeven
                        send_status_update(t, "TP1")
                        changed = True
                    elif t["status"] == "TP1_HIT" and hit_tp2:
                        t["status"] = "WIN"
                        t["pnl"] = round(abs(t["tp2"] - t["entry"]) * 0.05, 2)
                        send_status_update(t, "TP2")
                        changed = True
                    elif hit_sl:
                        t["status"] = "LOSS" if t["status"] == "ACTIVE" else "WIN"
                        if t["status"] == "LOSS":
                            t["pnl"] = -round(abs(t["entry"] - t["sl"]) * 0.05, 2)
                            send_status_update(t, "SL")
                        else:
                            t["pnl"] = round(abs(t["tp1"] - t["entry"]) * 0.05, 2)
                        changed = True

                if changed:
                    with STATE_LOCK:
                        save_state()
        except Exception as e:
            print(f"[monitor] loop error: {e}")
        time.sleep(15)


# ==========================================================
# 10. DASHBOARD TEMPLATE (Lovable-style dark theme)
# ==========================================================
DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CryptoScalper AJ</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body { font-family: 'Inter', system-ui, sans-serif; background: #0b0f19; color: #f3f4f6; }
  .card { background: rgba(17,24,39,0.85); border: 1px solid rgba(255,255,255,0.08); }
  .toggle { appearance: none; width: 42px; height: 24px; border-radius: 999px; background: #374151;
            position: relative; cursor: pointer; transition: .2s; }
  .toggle:checked { background: #059669; }
  .toggle::before { content: ""; position: absolute; width: 18px; height: 18px; border-radius: 50%;
                     background: white; top: 3px; left: 3px; transition: .2s; }
  .toggle:checked::before { transform: translateX(18px); }
</style>
</head>
<body class="min-h-screen p-4 md:p-8">
<div class="max-w-7xl mx-auto space-y-6">

  <!-- Header -->
  <div class="flex flex-col md:flex-row justify-between items-center p-6 rounded-2xl card gap-4">
    <div>
      <h1 class="text-3xl font-extrabold text-emerald-400">CryptoScalper AJ</h1>
      <p class="text-xs text-gray-400 mt-1">MEXC Futures • Multi-Confluence Engine</p>
    </div>
    <span class="px-3 py-1.5 rounded-full text-xs font-bold bg-emerald-950 text-emerald-400 border border-emerald-700 flex items-center gap-2">
      <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span> LIVE MEXC
    </span>
  </div>

  <!-- Metrics grid -->
  <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
    <div class="card p-5 rounded-2xl">
      <p class="text-xs font-semibold text-gray-400">TOTAL SIGNALS</p>
      <p class="text-2xl font-extrabold text-white mt-1">{{ stats.total }}</p>
    </div>
    <div class="card p-5 rounded-2xl">
      <p class="text-xs font-semibold text-gray-400">WIN RATE</p>
      <p class="text-2xl font-extrabold text-emerald-400 mt-1">{{ stats.win_rate }}</p>
    </div>
    <div class="card p-5 rounded-2xl">
      <p class="text-xs font-semibold text-gray-400">MARKETS WATCHED</p>
      <p class="text-2xl font-extrabold text-blue-400 mt-1">{{ config.symbols|length }}</p>
    </div>
    <div class="card p-5 rounded-2xl">
      <p class="text-xs font-semibold text-gray-400">ACTIVE SIGNALS</p>
      <p class="text-2xl font-extrabold text-amber-400 mt-1">{{ stats.active }}</p>
    </div>
  </div>

  <!-- Live signal feed -->
  <div class="card p-6 rounded-2xl space-y-4">
    <h2 class="text-lg font-bold text-white">⚡ Live Signal Feed</h2>
    {% if trades %}
    <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
      {% for t in trades %}
      <div class="bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-3">
        <div class="flex justify-between items-center">
          <span class="font-bold text-white">{{ t.symbol }} ({{ t.tf }}) <span class="ml-1">{{ t.side }}</span></span>
          <span class="text-xs font-bold text-cyan-400 bg-cyan-950/60 px-2 py-1 rounded border border-cyan-800/40">{{ t.id_str }}</span>
        </div>
        <div class="flex flex-wrap gap-2 text-[10px] font-bold">
          <span class="px-2 py-1 rounded bg-gray-800 text-gray-300">{{ t.strategy }}</span>
          {% if t.trap_filter.all_passed %}
          <span class="px-2 py-1 rounded bg-emerald-950 text-emerald-400 border border-emerald-800">🛡️ Trap Filter 5/5</span>
          {% endif %}
          <span class="px-2 py-1 rounded
            {% if t.status == 'WIN' %}bg-emerald-950 text-emerald-400 border border-emerald-800
            {% elif t.status == 'LOSS' %}bg-rose-950 text-rose-400 border border-rose-800
            {% elif t.status in ['ACTIVE','TP1_HIT'] %}bg-blue-950 text-blue-400 border border-blue-800
            {% else %}bg-amber-950 text-amber-400 border border-amber-800{% endif %}">
            {{ t.status }}
          </span>
        </div>

        {% if t.chart_img %}
        <div class="w-full overflow-hidden rounded-lg border border-gray-800">
          <img src="/charts/{{ t.chart_img }}" class="w-full h-auto cursor-pointer" onclick="window.open('/charts/{{ t.chart_img }}','_blank')">
        </div>
        {% endif %}

        <div class="grid grid-cols-2 gap-2 text-xs">
          <div class="bg-gray-800 p-2 rounded text-center"><p class="text-gray-400 text-[10px]">LIVE PRICE</p>${{ live_prices.get(t.symbol, '—') }}</div>
          <div class="bg-gray-800 p-2 rounded text-center"><p class="text-gray-400 text-[10px]">ENTRY</p>${{ t.entry }}</div>
        </div>
        <div class="grid grid-cols-3 gap-2 text-xs font-bold">
          <div class="bg-rose-950/40 border border-rose-900/50 p-2 rounded text-center"><p class="text-rose-400 text-[10px]">SL</p>${{ t.sl }}</div>
          <div class="bg-emerald-950/40 border border-emerald-900/50 p-2 rounded text-center"><p class="text-emerald-400 text-[10px]">TP1</p>${{ t.tp1 }}</div>
          <div class="bg-emerald-950/40 border border-emerald-900/50 p-2 rounded text-center"><p class="text-emerald-400 text-[10px]">TP2</p>${{ t.tp2 }}</div>
        </div>
        <p class="text-[10px] text-gray-500">R:R 1:{{ t.rr }}</p>
      </div>
      {% endfor %}
    </div>
    {% else %}
    <div class="text-center py-8 text-gray-500 text-sm">No signals yet — the engine is scanning in the background.</div>
    {% endif %}
  </div>

  <!-- Settings panel -->
  <div class="card p-6 rounded-2xl space-y-5">
    <h2 class="text-lg font-bold text-white">⚙️ Settings</h2>
    <form action="/update-settings" method="POST" class="space-y-5">
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <label class="text-xs text-gray-400">TELEGRAM_BOT_TOKEN</label>
          <input type="password" name="bot_token" value="{{ config.bot_token }}" placeholder="leave blank for now" class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white">
        </div>
        <div>
          <label class="text-xs text-gray-400">TELEGRAM_CHANNEL_USERNAME</label>
          <input type="text" name="channel" value="{{ config.channel }}" placeholder="@yourchannel" class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white">
        </div>
        <div>
          <label class="text-xs text-gray-400">MEXC_API_KEY</label>
          <input type="password" name="mexc_key" value="{{ config.mexc_key }}" placeholder="leave blank for now" class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white">
        </div>
        <div>
          <label class="text-xs text-gray-400">MEXC_SECRET_KEY</label>
          <input type="password" name="mexc_secret" value="{{ config.mexc_secret }}" placeholder="leave blank for now" class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white">
        </div>
        <div>
          <label class="text-xs text-gray-400">SYMBOLS (comma-separated)</label>
          <input type="text" name="symbols" value="{{ config.symbols|join(', ') }}" class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white">
        </div>
        <div>
          <label class="text-xs text-gray-400">SCAN INTERVAL (seconds, min 20)</label>
          <input type="number" min="20" name="scan_interval" value="{{ config.scan_interval }}" class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white">
        </div>
      </div>

      <div class="flex flex-wrap gap-8 pt-2">
        <label class="flex items-center gap-3 text-sm text-gray-300">
          <input type="checkbox" class="toggle" name="broadcast_enabled" {% if config.broadcast_enabled %}checked{% endif %}>
          SIGNAL_BROADCAST_ENABLED
        </label>
        <label class="flex items-center gap-3 text-sm text-gray-300">
          <input type="checkbox" class="toggle" name="autotrade_enabled" {% if config.autotrade_enabled %}checked{% endif %}>
          AUTO_TRADING_ENABLED
        </label>
      </div>

      <div class="flex flex-wrap gap-3 pt-2">
        <button type="submit" name="action" value="save" class="px-6 py-2.5 bg-emerald-600 text-white font-bold rounded-xl text-xs hover:bg-emerald-500 transition">Save Configurations</button>
        <button type="submit" name="action" value="test_signal" class="px-5 py-2.5 bg-amber-600 text-white font-bold rounded-xl text-xs hover:bg-amber-500 transition">⚡ Force Scan Now</button>
      </div>
    </form>
    <form action="/test-telegram" method="POST">
      <button type="submit" class="px-5 py-2.5 bg-blue-600 text-white font-bold rounded-xl text-xs hover:bg-blue-500 transition">📨 Test Telegram Alert</button>
    </form>
    {% if telegram_test_msg %}
    <p class="text-xs {% if telegram_test_ok %}text-emerald-400{% else %}text-rose-400{% endif %}">{{ telegram_test_msg }}</p>
    {% endif %}
  </div>

</div>
</body>
</html>
"""


# ==========================================================
# 11. FLASK ROUTES
# ==========================================================
def calculate_stats():
    total = len(TRADE_HISTORY)
    wins = sum(1 for t in TRADE_HISTORY if t["status"] == "WIN")
    losses = sum(1 for t in TRADE_HISTORY if t["status"] == "LOSS")
    closed = wins + losses
    win_rate = f"{(wins / closed * 100):.1f}%" if closed else "0.0%"
    active = sum(1 for t in TRADE_HISTORY if t["status"] in ("PENDING", "ACTIVE", "TP1_HIT"))
    return {"total": total, "wins": wins, "losses": losses, "win_rate": win_rate, "active": active}


def render_dashboard(telegram_test_msg=None, telegram_test_ok=None):
    stats = calculate_stats()
    with STATE_LOCK:
        trades_snapshot = list(TRADE_HISTORY[:20])
    live_prices = {}
    for t in trades_snapshot:
        if t["status"] in ("PENDING", "ACTIVE", "TP1_HIT"):
            price = get_live_price(t["symbol"])
            if price is not None:
                live_prices[t["symbol"]] = round(price, 2)
    return render_template_string(
        DASHBOARD_TEMPLATE, config=BOT_CONFIG, trades=trades_snapshot, stats=stats,
        live_prices=live_prices, telegram_test_msg=telegram_test_msg, telegram_test_ok=telegram_test_ok
    )


@app.route("/")
def home():
    return render_dashboard()


@app.route("/health")
def health():
    return jsonify({"status": "running"})


@app.route("/charts/<filename>")
def serve_chart(filename):
    return send_from_directory(CHARTS_DIR, filename)


@app.route("/update-settings", methods=["POST"])
def update_settings():
    action = request.form.get("action")
    BOT_CONFIG["bot_token"] = request.form.get("bot_token", "").strip()
    BOT_CONFIG["channel"] = request.form.get("channel", "").strip()
    BOT_CONFIG["mexc_key"] = request.form.get("mexc_key", "").strip()
    BOT_CONFIG["mexc_secret"] = request.form.get("mexc_secret", "").strip()

    symbols_raw = request.form.get("symbols", "BTC/USDT")
    BOT_CONFIG["symbols"] = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]

    try:
        BOT_CONFIG["scan_interval"] = max(20, int(request.form.get("scan_interval", 60)))
    except ValueError:
        BOT_CONFIG["scan_interval"] = 60

    BOT_CONFIG["broadcast_enabled"] = request.form.get("broadcast_enabled") == "on"
    BOT_CONFIG["autotrade_enabled"] = request.form.get("autotrade_enabled") == "on"

    if action == "test_signal":
        for symbol in BOT_CONFIG["symbols"]:
            trade, df_5m = build_signal(symbol)
            if trade:
                img_name, img_path = generate_chart(trade, df_5m)
                trade["chart_img"] = img_name
                trade["tg_msg_id"] = send_signal_alert(trade, img_path)
                trade["order_id"] = place_mexc_order(trade)
                with STATE_LOCK:
                    TRADE_HISTORY.insert(0, trade)
                break  # only force one signal per click

    with STATE_LOCK:
        save_state()

    return render_dashboard()


@app.route("/test-telegram", methods=["POST"])
def test_telegram():
    ok, msg = test_telegram_alert()
    return render_dashboard(telegram_test_msg=msg, telegram_test_ok=ok)


# ==========================================================
# 12. STARTUP
# ==========================================================
load_state()

_threads_started = False


def start_background_engine():
    global _threads_started
    if _threads_started:
        return
    threading.Thread(target=scanner_loop, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    _threads_started = True
    print("[startup] background scanner + monitor threads started")


# Start the 24/7 engine whether run via `python main.py` or a WSGI server
# (e.g. gunicorn on Render) that imports this module directly.
start_background_engine()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
