import os
import json
import time
import threading
import requests
import ccxt
import pandas as pd
from flask import Flask, render_template_string, request, send_from_directory

app = Flask(__name__)

# ==========================================
# 1. GLOBAL CONFIG & STATE
# ==========================================
BOT_CONFIG = {
    "bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "channel": os.getenv("TELEGRAM_CHANNEL", ""),
    "mexc_key": os.getenv("MEXC_API_KEY", ""),
    "mexc_secret": os.getenv("MEXC_SECRET_KEY", ""),
    "broadcast": True,
    "autotrade": False,          # acts as "auto-scan enabled" toggle
    "symbols": ["BTC/USDT"],     # symbols the scanner loop watches
    "timeframe": "5m",
    "scan_interval": 300         # seconds between auto-scans
}

TRADE_HISTORY = []
STATE_LOCK = threading.Lock()

CHARTS_DIR = os.path.join(os.getcwd(), "generated_charts")
os.makedirs(CHARTS_DIR, exist_ok=True)

DATA_DIR = os.path.join(os.getcwd(), "data")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "state.json")


def load_state():
    """Restore config + trade history from disk so a restart doesn't wipe everything."""
    global BOT_CONFIG, TRADE_HISTORY
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                data = json.load(f)
            BOT_CONFIG.update(data.get("config", BOT_CONFIG))
            TRADE_HISTORY.extend(data.get("trades", []))
            print(f"State restored: {len(TRADE_HISTORY)} trades loaded.")
        except Exception as e:
            print(f"State load error: {e}")


def save_state():
    """Persist config + trade history to disk. Call this after any mutation."""
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump({"config": BOT_CONFIG, "trades": TRADE_HISTORY}, f)
    except Exception as e:
        print(f"State save error: {e}")


def get_mexc_client():
    return ccxt.mexc({
        'apiKey': BOT_CONFIG.get('mexc_key', ''),
        'secret': BOT_CONFIG.get('mexc_secret', ''),
        'options': {'defaultType': 'swap'}
    })

# ==========================================
# 2. SMC & FIB OTE STRATEGY ENGINE
# ==========================================
def find_order_block(df_slice, bullish=True):
    """
    Simple OB detector:
    - Bullish OB = last down (red) candle before the impulsive up move.
    - Bearish OB = last up (green) candle before the impulsive down move.
    Falls back to the extreme candle itself if none is found.
    """
    if bullish:
        down = df_slice[df_slice['close'] < df_slice['open']]
        c = down.iloc[-1] if not down.empty else df_slice.iloc[0]
    else:
        up = df_slice[df_slice['close'] > df_slice['open']]
        c = up.iloc[-1] if not up.empty else df_slice.iloc[0]
    return float(c['high']), float(c['low'])


def find_fvg(df_slice, bullish=True):
    """
    3-candle Fair Value Gap detector.
    Bullish FVG: candle[i-1].high < candle[i+1].low
    Bearish FVG: candle[i-1].low  > candle[i+1].high
    Returns the most recent gap found, or None.
    """
    rows = df_slice.reset_index(drop=True)
    gap = None
    for i in range(1, len(rows) - 1):
        prev_c = rows.iloc[i - 1]
        next_c = rows.iloc[i + 1]
        if bullish and prev_c['high'] < next_c['low']:
            gap = (float(prev_c['high']), float(next_c['low']))
        elif not bullish and prev_c['low'] > next_c['high']:
            gap = (float(next_c['high']), float(prev_c['low']))
    return gap


def analyze_smc_fib_strategy(symbol='BTC/USDT', timeframe='5m'):
    try:
        mexc = get_mexc_client()
        ohlcv = mexc.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)

        df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        df['time_sec'] = df['time'] // 1000

        last30 = df.tail(30).reset_index(drop=True)
        recent_high = last30['high'].max()
        recent_low = last30['low'].min()
        diff = recent_high - recent_low

        if diff == 0:
            return None, None

        low_idx = int(last30['low'].idxmin())
        high_idx = int(last30['high'].idxmax())
        bullish = high_idx > low_idx

        fib_618 = recent_high - (diff * 0.618)
        fib_786 = recent_high - (diff * 0.786)

        last_close = df['close'].iloc[-1]

        if bullish:
            ob_slice = last30.iloc[max(0, low_idx - 3): low_idx + 1]
            ob_top, ob_bottom = find_order_block(ob_slice, bullish=True)
        else:
            ob_slice = last30.iloc[max(0, high_idx - 3): high_idx + 1]
            ob_top, ob_bottom = find_order_block(ob_slice, bullish=False)

        move_slice = last30.iloc[min(low_idx, high_idx): max(low_idx, high_idx) + 1]
        fvg = find_fvg(move_slice, bullish=bullish)
        if fvg:
            fvg_bottom, fvg_top = fvg
        else:
            fvg_bottom, fvg_top = fib_786, fib_618

        candles = [{"time": int(row['time_sec']), "open": row['open'], "high": row['high'],
                    "low": row['low'], "close": row['close'], "volume": row['volume']}
                   for _, row in df.iterrows()]

        sig_id = f"SIG_{int(time.time())}"
        trade = {
            "id_str": sig_id,
            "symbol": symbol,
            "tf": timeframe.upper(),
            "strategy": "SMC Order Block + FVG + Fib OTE",
            "side": "🟢 LONG" if bullish else "🔴 SHORT",
            "trend_bullish": bullish,
            "entry1": round(last_close, 2),
            "entry2": round(last_close * (0.9995 if bullish else 1.0005), 2),
            "sl": round(recent_low if bullish else recent_high, 2),
            "tp1": round(recent_high if bullish else recent_low, 2),
            "rr": "2.75",
            "status": "ACTIVE",
            "pnl": 0.0,
            "chart_img": None,
            "tg_msg_id": None,
            "ob_top": round(ob_top, 6),
            "ob_bottom": round(ob_bottom, 6),
            "fvg_top": round(fvg_top, 6),
            "fvg_bottom": round(fvg_bottom, 6),
            "fib_618": round(fib_618, 6),
            "fib_786": round(fib_786, 6),
        }
        return trade, candles

    except Exception as e:
        print(f"Strategy Error: {e}")
        return None, None

# ==========================================
# 3. ANALYTICS & CANDLESTICK CHART ENGINE
# ==========================================
def calculate_stats():
    total_trades = len(TRADE_HISTORY)
    if total_trades == 0:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": "0.0%", "pnl": 0.0}

    wins = sum(1 for t in TRADE_HISTORY if t.get('status') == 'WIN')
    losses = sum(1 for t in TRADE_HISTORY if t.get('status') == 'LOSS')
    pnl = sum(t.get('pnl', 0.0) for t in TRADE_HISTORY)
    win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0

    return {
        "total": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": f"{win_rate:.1f}%",
        "pnl": round(pnl, 2)
    }


def generate_quickchart_image(candles, signal):
    """
    Dark, TradingView-style candlestick chart with OB zone, FVG zone,
    Fib 0.618/0.786 OTE zone, and Entry/SL/TP lines, via quickchart.io.
    """
    visible = candles[-45:]
    chart_data = [{
        "x": time.strftime('%H:%M', time.localtime(c['time'])),
        "o": c['open'], "h": c['high'], "l": c['low'], "c": c['close']
    } for c in visible]

    bullish = signal.get("trend_bullish", True)
    up_color = "#089981"
    down_color = "#f23645"
    ob_color = "rgba(41, 98, 255, 0.18)"
    ob_border = "rgba(41, 98, 255, 0.65)"
    fvg_color = "rgba(255, 193, 7, 0.15)"
    fvg_border = "rgba(255, 193, 7, 0.55)"
    ote_color = "rgba(8, 153, 129, 0.15)" if bullish else "rgba(242, 54, 69, 0.15)"
    ote_border = "rgba(8, 153, 129, 0.55)" if bullish else "rgba(242, 54, 69, 0.55)"

    annotations = [
        {
            "drawTime": "beforeDatasetsDraw", "type": "box", "yScaleID": "yAxes",
            "yMin": min(signal['ob_top'], signal['ob_bottom']),
            "yMax": max(signal['ob_top'], signal['ob_bottom']),
            "backgroundColor": ob_color, "borderColor": ob_border, "borderWidth": 1,
            "label": {"content": "OB", "enabled": True, "position": "left", "fontColor": "#8fb8ff", "fontSize": 10}
        },
        {
            "drawTime": "beforeDatasetsDraw", "type": "box", "yScaleID": "yAxes",
            "yMin": min(signal['fvg_top'], signal['fvg_bottom']),
            "yMax": max(signal['fvg_top'], signal['fvg_bottom']),
            "backgroundColor": fvg_color, "borderColor": fvg_border, "borderWidth": 1,
            "label": {"content": "FVG", "enabled": True, "position": "left", "fontColor": "#ffd76a", "fontSize": 10}
        },
        {
            "drawTime": "beforeDatasetsDraw", "type": "box", "yScaleID": "yAxes",
            "yMin": min(signal['fib_618'], signal['fib_786']),
            "yMax": max(signal['fib_618'], signal['fib_786']),
            "backgroundColor": ote_color, "borderColor": ote_border, "borderWidth": 1,
            "label": {"content": "OTE 0.618-0.786", "enabled": True, "position": "left", "fontColor": "#cfd8dc", "fontSize": 10}
        },
        {
            "type": "line", "mode": "horizontal", "scaleID": "yAxes",
            "value": signal['tp1'], "borderColor": up_color if bullish else down_color,
            "borderWidth": 2, "borderDash": [4, 4],
            "label": {"content": f"TP1: {signal['tp1']}", "enabled": True, "position": "right",
                      "backgroundColor": up_color if bullish else down_color}
        },
        {
            "type": "line", "mode": "horizontal", "scaleID": "yAxes",
            "value": signal['entry1'], "borderColor": "#2962ff", "borderWidth": 2,
            "label": {"content": f"Entry 1: {signal['entry1']}", "enabled": True, "position": "right",
                      "backgroundColor": "#2962ff"}
        },
        {
            "type": "line", "mode": "horizontal", "scaleID": "yAxes",
            "value": signal['entry2'], "borderColor": "#64b5f6", "borderWidth": 1, "borderDash": [2, 2],
            "label": {"content": f"Entry 2: {signal['entry2']}", "enabled": True, "position": "right",
                      "backgroundColor": "#64b5f6"}
        },
        {
            "type": "line", "mode": "horizontal", "scaleID": "yAxes",
            "value": signal['sl'], "borderColor": down_color if bullish else up_color, "borderWidth": 2,
            "borderDash": [4, 4],
            "label": {"content": f"SL: {signal['sl']}", "enabled": True, "position": "right",
                      "backgroundColor": down_color if bullish else up_color}
        },
    ]

    chart_config = {
        "type": "candlestick",
        "data": {
            "datasets": [{
                "label": f"{signal['symbol']} {signal['tf']}",
                "data": chart_data,
                "color": {"up": up_color, "down": down_color, "unchanged": "#999999"}
            }]
        },
        "options": {
            "backgroundColor": "#0b0f19",
            "legend": {"display": False},
            "title": {
                "display": True,
                "text": f"{signal['symbol']} · {signal['tf']} · {signal['strategy']}",
                "fontColor": "#e5e7eb", "fontSize": 14
            },
            "scales": {
                "xAxes": [{"gridLines": {"color": "#1f2937"}, "ticks": {"fontColor": "#848e9c"}}],
                "yAxes": [{"id": "yAxes", "position": "right",
                           "gridLines": {"color": "#1f2937"}, "ticks": {"fontColor": "#848e9c"}}]
            },
            "plugins": {"annotation": {"annotations": annotations}}
        }
    }

    url = "https://quickchart.io/chart"
    payload = {
        "backgroundColor": "#0b0f19", "width": 1000, "height": 560,
        "format": "png", "version": "2.9.4", "chart": chart_config
    }

    filename = f"chart_{signal['id_str']}_{int(time.time())}.png"
    filepath = os.path.join(CHARTS_DIR, filename)

    try:
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code == 200:
            with open(filepath, 'wb') as f:
                f.write(res.content)
            return filename, filepath
        else:
            print(f"QuickChart HTTP {res.status_code}: {res.text[:300]}")
    except Exception as e:
        print(f"QuickChart Candlestick Error: {e}")

    return None, None


def send_telegram_alert(signal, chart_path=None):
    if not BOT_CONFIG["broadcast"] or not BOT_CONFIG["bot_token"] or not BOT_CONFIG["channel"]:
        return None

    msg = (
        f"⚡ <b>SMC SIGNAL {signal['id_str']} | {signal['symbol']} ({signal['tf']})</b>\n"
        f"<b>Strategy:</b> {signal['strategy']}\n"
        f"<b>Direction:</b> {signal['side']}\n\n"
        f"🧱 <b>OB Zone:</b> {signal['ob_bottom']} - {signal['ob_top']}\n"
        f"⚡ <b>FVG Zone:</b> {signal['fvg_bottom']} - {signal['fvg_top']}\n"
        f"🎯 <b>OTE (0.618-0.786):</b> {signal['fib_786']} - {signal['fib_618']}\n\n"
        f"🎯 <b>ENTRY 1:</b> ${signal['entry1']:.2f}\n"
        f"🎯 <b>ENTRY 2:</b> ${signal['entry2']:.2f}\n"
        f"🛑 <b>SL:</b> ${signal['sl']:.2f}\n"
        f"🚀 <b>TP1:</b> ${signal['tp1']:.2f}\n"
        f"📊 <b>Risk:Reward:</b> 1:{signal['rr']}\n\n"
        f"🧱 <b>Status:</b> ACTIVE 🟢"
    )

    url = f"https://api.telegram.org/bot{BOT_CONFIG['bot_token']}/sendPhoto"
    try:
        if chart_path and os.path.exists(chart_path):
            with open(chart_path, 'rb') as photo:
                res = requests.post(url, data={'chat_id': BOT_CONFIG["channel"], 'caption': msg, 'parse_mode': 'HTML'},
                                     files={'photo': photo}, timeout=20)
                res_json = res.json()
                if res_json.get("ok"):
                    return res_json["result"]["message_id"]
                else:
                    print(f"Telegram sendPhoto failed: {res_json}")
        else:
            url_msg = f"https://api.telegram.org/bot{BOT_CONFIG['bot_token']}/sendMessage"
            res = requests.post(url_msg, data={'chat_id': BOT_CONFIG["channel"], 'text': msg, 'parse_mode': 'HTML'}, timeout=12)
            res_json = res.json()
            if res_json.get("ok"):
                return res_json["result"]["message_id"]
    except Exception as e:
        print(f"TG Alert Error: {e}")
    return None


def send_trade_update_reply(signal, update_status="WIN"):
    if not BOT_CONFIG["bot_token"] or not BOT_CONFIG["channel"] or not signal.get("tg_msg_id"):
        return

    if update_status == "WIN":
        update_text = f"✅ <b>TARGET HIT (TP1) REACHED!</b> 🎉\nSignal {signal['id_str']} successfully secured profit! 🚀"
    else:
        update_text = f"❌ <b>STOP LOSS (SL) HIT!</b>\nSignal {signal['id_str']} closed at stop loss. Risk managed."

    url = f"https://api.telegram.org/bot{BOT_CONFIG['bot_token']}/sendMessage"
    payload = {
        'chat_id': BOT_CONFIG["channel"], 'text': update_text,
        'parse_mode': 'HTML', 'reply_to_message_id': signal["tg_msg_id"]
    }
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"Reply Update Error: {e}")

# ==========================================
# 4. BACKGROUND WORKERS (auto-scan + auto TP/SL monitor)
# ==========================================
def scanner_loop():
    """Runs forever. When autotrade (auto-scan) is on, periodically scans the
    configured symbols and fires a new signal if that symbol has no ACTIVE trade."""
    while True:
        try:
            if BOT_CONFIG.get("autotrade"):
                symbols = BOT_CONFIG.get("symbols", ["BTC/USDT"])
                tf = BOT_CONFIG.get("timeframe", "5m")
                for sym in symbols:
                    with STATE_LOCK:
                        has_active = any(t["symbol"] == sym and t["status"] == "ACTIVE" for t in TRADE_HISTORY)
                    if has_active:
                        continue
                    trade, candles = analyze_smc_fib_strategy(sym, tf)
                    if trade and candles:
                        img_name, img_path = generate_quickchart_image(candles, trade)
                        trade["chart_img"] = img_name
                        msg_id = send_telegram_alert(trade, img_path)
                        trade["tg_msg_id"] = msg_id
                        with STATE_LOCK:
                            TRADE_HISTORY.insert(0, trade)
                            save_state()
                        print(f"[scanner] New {trade['side']} signal for {sym}")
        except Exception as e:
            print(f"Scanner loop error: {e}")
        time.sleep(max(30, int(BOT_CONFIG.get("scan_interval", 300))))


def monitor_loop():
    """Runs forever. Polls live price for every ACTIVE trade and auto-closes
    it as WIN/LOSS when TP1 or SL is hit, then sends the Telegram reply."""
    while True:
        try:
            with STATE_LOCK:
                active_trades = [t for t in TRADE_HISTORY if t["status"] == "ACTIVE"]
            if active_trades:
                mexc = get_mexc_client()
                for t in active_trades:
                    try:
                        ticker = mexc.fetch_ticker(t["symbol"])
                        price = ticker.get("last")
                        if price is None:
                            continue
                        is_long = "LONG" in t["side"]
                        hit_tp = price >= t["tp1"] if is_long else price <= t["tp1"]
                        hit_sl = price <= t["sl"] if is_long else price >= t["sl"]
                        if hit_tp or hit_sl:
                            status = "WIN" if hit_tp else "LOSS"
                            with STATE_LOCK:
                                t["status"] = status
                                if status == "WIN":
                                    t["pnl"] = round(abs(t["tp1"] - t["entry1"]) * 0.05, 2)
                                else:
                                    t["pnl"] = -round(abs(t["entry1"] - t["sl"]) * 0.05, 2)
                                save_state()
                            send_trade_update_reply(t, update_status=status)
                            print(f"[monitor] {t['id_str']} closed as {status}")
                    except Exception as e:
                        print(f"Monitor ticker error for {t['symbol']}: {e}")
        except Exception as e:
            print(f"Monitor loop error: {e}")
        time.sleep(20)

# ==========================================
# 5. DASHBOARD UI TEMPLATE
# ==========================================
DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CryptoScalper AJ Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
    <style>
        body { font-family: 'Inter', sans-serif; background-color: #0b0f19; color: #f3f4f6; }
        .lovable-card { background: rgba(17, 24, 39, 0.85); border: 1px solid rgba(255, 255, 255, 0.08); }
    </style>
</head>
<body class="min-h-screen p-4 md:p-8">
    <div class="max-w-7xl mx-auto space-y-6">

        <!-- Header -->
        <div class="flex flex-col md:flex-row justify-between items-center p-6 rounded-2xl lovable-card gap-4">
            <div>
                <h1 class="text-3xl font-extrabold text-emerald-400">CryptoScalper AJ</h1>
                <p class="text-xs text-gray-400 mt-1">SMC (OB + FVG) & Fib OTE Candlestick Engine with Telegram Reply System</p>
            </div>
            <div class="text-right">
                <p class="text-xs text-gray-400">Auto-Scan</p>
                <p class="text-sm font-semibold {% if config.autotrade %}text-emerald-400{% else %}text-gray-500{% endif %}">
                    {% if config.autotrade %}ON · every {{ config.scan_interval }}s{% else %}OFF{% endif %}
                </p>
            </div>
        </div>

        <!-- 📊 ANALYTICS PANEL -->
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div class="lovable-card p-5 rounded-2xl">
                <p class="text-xs font-semibold text-gray-400">TOTAL SIGNALS</p>
                <p class="text-2xl font-extrabold text-white mt-1">{{ stats.total }}</p>
            </div>
            <div class="lovable-card p-5 rounded-2xl">
                <p class="text-xs font-semibold text-gray-400">WIN RATE</p>
                <p class="text-2xl font-extrabold text-emerald-400 mt-1">{{ stats.win_rate }}</p>
            </div>
            <div class="lovable-card p-5 rounded-2xl">
                <p class="text-xs font-semibold text-gray-400">WIN / LOSS RATIO</p>
                <p class="text-2xl font-extrabold text-blue-400 mt-1">{{ stats.wins }}W / {{ stats.losses }}L</p>
            </div>
            <div class="lovable-card p-5 rounded-2xl">
                <p class="text-xs font-semibold text-gray-400">ESTIMATED PNL ($)</p>
                <p class="text-2xl font-extrabold {% if stats.pnl >= 0 %}text-emerald-400{% else %}text-rose-400{% endif %} mt-1">
                    {% if stats.pnl >= 0 %}+{% endif %}${{ stats.pnl }}
                </p>
            </div>
        </div>

        <!-- 📈 TRADINGVIEW LIVE WIDGET -->
        <div class="lovable-card p-6 rounded-2xl space-y-4">
            <h2 class="text-lg font-bold text-white">📈 Live Market Price Chart (MEXC Real-Time)</h2>
            <div class="w-full h-[500px] rounded-xl overflow-hidden" id="tradingview_chart"></div>
        </div>

        <!-- ⚡ AUTOMATED SIGNAL FEED -->
        <div class="lovable-card p-6 rounded-2xl space-y-4">
            <h2 class="text-lg font-bold text-white">⚡ Automated Signal Feed & Candlestick Setup Charts</h2>
            {% if trades %}
                <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                    {% for trade in trades %}
                    <div class="bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-4">
                        <div class="flex justify-between items-center">
                            <span class="font-bold text-white">{{ trade.symbol }} ({{ trade.tf }}) - <span class="text-emerald-400">{{ trade.side }}</span></span>
                            <span class="text-xs font-bold text-cyan-400 bg-cyan-950/60 px-2 py-1 rounded border border-cyan-800/40">{{ trade.id_str }}</span>
                        </div>

                        {% if trade.chart_img %}
                        <div class="w-full overflow-hidden rounded-lg border border-gray-800">
                            <img src="/charts/{{ trade.chart_img }}" class="w-full h-auto cursor-pointer" onclick="window.open('/charts/{{ trade.chart_img }}', '_blank')">
                        </div>
                        {% endif %}

                        <div class="grid grid-cols-3 gap-2 text-xs font-bold">
                            <div class="bg-gray-800 p-2 rounded text-center"><p class="text-gray-400 text-[10px]">ENTRY 1</p>${{ "%.2f"|format(trade.entry1) }}</div>
                            <div class="bg-rose-950/40 border border-rose-900/50 p-2 rounded text-center"><p class="text-rose-400 text-[10px]">STOP LOSS</p>${{ "%.2f"|format(trade.sl) }}</div>
                            <div class="bg-emerald-950/40 border border-emerald-900/50 p-2 rounded text-center"><p class="text-emerald-400 text-[10px]">TP1</p>${{ "%.2f"|format(trade.tp1) }}</div>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            {% else %}
                <div class="text-center py-8 text-gray-500 text-sm">
                    No active signal generated yet. Click "Trigger Live Test Signal" below, or turn Auto-Scan on.
                </div>
            {% endif %}
        </div>

        <!-- 📋 TRADE PERFORMANCE HISTORY & TELEGRAM REPLY -->
        <div class="lovable-card p-6 rounded-2xl space-y-4">
            <h2 class="text-lg font-bold text-white">📋 Trade Performance & Telegram Reply Control</h2>
            <p class="text-[11px] text-gray-500">TP/SL are also monitored automatically in the background — these buttons are for manual override.</p>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs text-gray-300">
                    <thead class="bg-gray-800/60 text-gray-400 uppercase font-bold">
                        <tr>
                            <th class="p-3">Signal ID</th>
                            <th class="p-3">Symbol</th>
                            <th class="p-3">Type</th>
                            <th class="p-3">Entry</th>
                            <th class="p-3">Status</th>
                            <th class="p-3">Manual Override</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-gray-800">
                        {% for t in trades %}
                        <tr class="hover:bg-gray-800/40">
                            <td class="p-3 font-bold text-white">{{ t.id_str }}</td>
                            <td class="p-3">{{ t.symbol }}</td>
                            <td class="p-3 font-bold text-emerald-400">{{ t.side }}</td>
                            <td class="p-3">${{ "%.2f"|format(t.entry1) }}</td>
                            <td class="p-3">
                                {% if t.status == 'WIN' %}
                                    <span class="bg-emerald-950 text-emerald-400 border border-emerald-800 px-2 py-0.5 rounded font-bold">WIN</span>
                                {% elif t.status == 'LOSS' %}
                                    <span class="bg-rose-950 text-rose-400 border border-rose-800 px-2 py-0.5 rounded font-bold">LOSS</span>
                                {% else %}
                                    <span class="bg-amber-950 text-amber-400 border border-amber-800 px-2 py-0.5 rounded font-bold">ACTIVE</span>
                                {% endif %}
                            </td>
                            <td class="p-3">
                                <div class="flex gap-2">
                                    <form method="POST" action="/trigger-reply">
                                        <input type="hidden" name="id" value="{{ t.id_str }}">
                                        <input type="hidden" name="status" value="WIN">
                                        <button type="submit" class="bg-emerald-700 hover:bg-emerald-600 text-white px-2.5 py-1 rounded font-bold text-[10px]">TP Hit ✅</button>
                                    </form>
                                    <form method="POST" action="/trigger-reply">
                                        <input type="hidden" name="id" value="{{ t.id_str }}">
                                        <input type="hidden" name="status" value="LOSS">
                                        <button type="submit" class="bg-rose-700 hover:bg-rose-600 text-white px-2.5 py-1 rounded font-bold text-[10px]">SL Hit ❌</button>
                                    </form>
                                </div>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- ⚙️ CONFIGURATIONS -->
        <div class="lovable-card p-6 rounded-2xl space-y-4">
            <h2 class="text-lg font-bold text-white">⚙️ API & Scanner Configurations</h2>
            <form action="/update-settings" method="POST" class="space-y-4">
                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                        <label class="text-xs text-gray-400">TELEGRAM_BOT_TOKEN</label>
                        <input type="password" name="bot_token" value="{{ config.bot_token }}" class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white">
                    </div>
                    <div>
                        <label class="text-xs text-gray-400">TELEGRAM_CHANNEL_USERNAME</label>
                        <input type="text" name="channel" value="{{ config.channel }}" class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white">
                    </div>
                    <div>
                        <label class="text-xs text-gray-400">MEXC_API_KEY</label>
                        <input type="password" name="mexc_key" value="{{ config.mexc_key }}" class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white">
                    </div>
                    <div>
                        <label class="text-xs text-gray-400">MEXC_SECRET_KEY</label>
                        <input type="password" name="mexc_secret" value="{{ config.mexc_secret }}" class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white">
                    </div>
                    <div>
                        <label class="text-xs text-gray-400">SYMBOLS (comma-separated)</label>
                        <input type="text" name="symbols" value="{{ config.symbols|join(', ') }}" class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white">
                    </div>
                    <div>
                        <label class="text-xs text-gray-400">TIMEFRAME</label>
                        <select name="timeframe" class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white">
                            {% for tf in ['1m','5m','15m','1h'] %}
                            <option value="{{ tf }}" {% if config.timeframe == tf %}selected{% endif %}>{{ tf }}</option>
                            {% endfor %}
                        </select>
                    </div>
                    <div>
                        <label class="text-xs text-gray-400">SCAN INTERVAL (seconds, min 30)</label>
                        <input type="number" min="30" name="scan_interval" value="{{ config.scan_interval }}" class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white">
                    </div>
                    <div class="flex items-center gap-2 mt-5">
                        <input type="checkbox" id="autotrade" name="autotrade" {% if config.autotrade %}checked{% endif %} class="w-4 h-4">
                        <label for="autotrade" class="text-xs text-gray-300">Enable Auto-Scan (background signal generation)</label>
                    </div>
                </div>
                <div class="flex flex-wrap gap-3 pt-3">
                    <button type="submit" name="action" value="test_signal" class="px-5 py-2.5 bg-amber-600 text-white font-bold rounded-xl text-xs hover:bg-amber-500 transition">⚡ Trigger Candlestick Signal & Test</button>
                    <button type="submit" name="action" value="save" class="px-6 py-2.5 bg-emerald-600 text-white font-bold rounded-xl text-xs hover:bg-emerald-500 transition">Save Configurations</button>
                </div>
            </form>
        </div>

    </div>

    <script type="text/javascript">
        new TradingView.widget({
            "autosize": true,
            "symbol": "MEXC:BTCUSDT.P",
            "interval": "5",
            "timezone": "Etc/UTC",
            "theme": "dark",
            "style": "1",
            "locale": "en",
            "container_id": "tradingview_chart"
        });
    </script>
</body>
</html>
"""

# ==========================================
# 6. FLASK SERVER ROUTES
# ==========================================
@app.route('/')
def home():
    stats = calculate_stats()
    return render_template_string(DASHBOARD_TEMPLATE, config=BOT_CONFIG, trades=TRADE_HISTORY, stats=stats)


@app.route('/charts/<filename>')
def serve_chart(filename):
    return send_from_directory(CHARTS_DIR, filename)


@app.route('/trigger-reply', methods=['POST'])
def trigger_reply():
    sig_id = request.form.get("id")
    status = request.form.get("status")

    with STATE_LOCK:
        for t in TRADE_HISTORY:
            if t["id_str"] == sig_id:
                t["status"] = status
                if status == "WIN":
                    t["pnl"] = round(abs(t["tp1"] - t["entry1"]) * 0.05, 2)
                else:
                    t["pnl"] = -round(abs(t["entry1"] - t["sl"]) * 0.05, 2)
                save_state()
                send_trade_update_reply(t, update_status=status)
                break

    stats = calculate_stats()
    return render_template_string(DASHBOARD_TEMPLATE, config=BOT_CONFIG, trades=TRADE_HISTORY, stats=stats)


@app.route('/update-settings', methods=['POST'])
def update_settings():
    action = request.form.get("action")
    BOT_CONFIG["bot_token"] = request.form.get("bot_token", "").strip()
    BOT_CONFIG["channel"] = request.form.get("channel", "").strip()
    BOT_CONFIG["mexc_key"] = request.form.get("mexc_key", "").strip()
    BOT_CONFIG["mexc_secret"] = request.form.get("mexc_secret", "").strip()
    BOT_CONFIG["broadcast"] = True

    symbols_raw = request.form.get("symbols", "BTC/USDT")
    BOT_CONFIG["symbols"] = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
    BOT_CONFIG["timeframe"] = request.form.get("timeframe", "5m")
    try:
        BOT_CONFIG["scan_interval"] = max(30, int(request.form.get("scan_interval", 300)))
    except ValueError:
        BOT_CONFIG["scan_interval"] = 300
    BOT_CONFIG["autotrade"] = request.form.get("autotrade") == "on"

    if action == "test_signal":
        sym = BOT_CONFIG["symbols"][0] if BOT_CONFIG["symbols"] else "BTC/USDT"
        trade, candles = analyze_smc_fib_strategy(sym, BOT_CONFIG["timeframe"])
        if trade and candles:
            img_name, img_path = generate_quickchart_image(candles, trade)
            trade["chart_img"] = img_name
            msg_id = send_telegram_alert(trade, img_path)
            trade["tg_msg_id"] = msg_id
            with STATE_LOCK:
                TRADE_HISTORY.insert(0, trade)

    with STATE_LOCK:
        save_state()

    stats = calculate_stats()
    return render_template_string(DASHBOARD_TEMPLATE, config=BOT_CONFIG, trades=TRADE_HISTORY, stats=stats)


# Restore saved state as soon as the module loads (works under gunicorn too)
load_state()

if __name__ == '__main__':
    threading.Thread(target=scanner_loop, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
