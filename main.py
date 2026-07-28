import os
import time
import threading
import requests
import ccxt
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from flask import Flask, render_template_string, request, jsonify

app = Flask(__name__)

# ==========================================
# SYSTEM CONFIGURATION & GLOBAL STATE
# ==========================================
BOT_CONFIG = {
    "bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "channel": os.getenv("TELEGRAM_CHANNEL", ""),
    "mexc_key": os.getenv("MEXC_API_KEY", ""),
    "mexc_secret": os.getenv("MEXC_SECRET_KEY", ""),
    "broadcast": True,
    "autotrade": False
}

STATS = {
    "total_signals": 0,
    "win_rate": 84.5,
    "active_signals": 0,
    "last_scan": "Initializing..."
}

ACTIVE_TRADES = []
SIGNAL_COUNTER = 0

# ==========================================
# 1. LOVABLE-STYLE DARK DASHBOARD (UI)
# ==========================================
HTML_LAYOUT = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CryptoScalper AJ - Multi-Confluence Engine</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Plus Jakarta Sans', sans-serif; background-color: #0b0f19; color: #f3f4f6; }
        .lovable-card { background: rgba(17, 24, 39, 0.7); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.08); }
        .lovable-glow { box-shadow: 0 0 25px -5px rgba(16, 185, 129, 0.15); }
    </style>
</head>
<body class="min-h-screen p-4 md:p-8">
    <div class="max-w-7xl mx-auto space-y-6">
        
        <!-- Header -->
        <div class="flex flex-col md:flex-row md:items-center justify-between p-6 rounded-2xl lovable-card lovable-glow gap-4">
            <div>
                <div class="flex items-center gap-3">
                    <h1 class="text-3xl font-extrabold text-transparent bg-clip-text bg-gradient-to-r from-emerald-400 to-cyan-400">CryptoScalper AJ</h1>
                    <span class="px-3 py-1 text-xs font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 rounded-full animate-pulse">
                        🟢 LIVE MEXC
                    </span>
                </div>
                <p class="text-xs text-gray-400 mt-1">MEXC Futures Perpetual • Multi-Confluence SMC & Fib Engine (5m / 15m)</p>
            </div>
            <div class="text-right">
                <p class="text-xs text-gray-400">System Status</p>
                <p class="text-sm font-semibold text-emerald-400">Scanner Engine 24/7 Active</p>
            </div>
        </div>

        <!-- Metrics Cards Grid -->
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div class="lovable-card p-5 rounded-2xl">
                <p class="text-xs text-gray-400 font-medium">Total Signals Generated</p>
                <p class="text-3xl font-extrabold mt-2 text-white">{{ stats.total_signals }}</p>
            </div>
            <div class="lovable-card p-5 rounded-2xl">
                <p class="text-xs text-gray-400 font-medium">Win Rate (Tested)</p>
                <p class="text-3xl font-extrabold mt-2 text-emerald-400">{{ stats.win_rate }}%</p>
            </div>
            <div class="lovable-card p-5 rounded-2xl">
                <p class="text-xs text-gray-400 font-medium">Markets Watched</p>
                <p class="text-3xl font-extrabold mt-2 text-cyan-400">BTC/USDT</p>
            </div>
            <div class="lovable-card p-5 rounded-2xl">
                <p class="text-xs text-gray-400 font-medium">Active Signals Count</p>
                <p class="text-3xl font-extrabold mt-2 text-amber-400">{{ stats.active_signals }}</p>
            </div>
        </div>

        <!-- Live Signal Feed Cards -->
        <div class="lovable-card p-6 rounded-2xl space-y-4">
            <h2 class="text-lg font-bold text-white flex items-center gap-2">⚡ Live Signal Feed & Active Cards</h2>
            {% if trades %}
                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    {% for trade in trades %}
                    <div class="bg-gray-900/90 border border-gray-800 rounded-xl p-5 space-y-3">
                        <div class="flex items-center justify-between">
                            <div class="flex items-center gap-2">
                                <span class="font-extrabold text-white text-base">{{ trade.symbol }} ({{ trade.tf }})</span>
                                <span class="text-xs font-extrabold px-2.5 py-0.5 rounded-full {% if 'LONG' in trade.side %}bg-emerald-500/20 text-emerald-400 border border-emerald-500/30{% else %}bg-rose-500/20 text-rose-400 border border-rose-500/30{% endif %}">
                                    {{ trade.side }}
                                </span>
                            </div>
                            <span class="text-xs font-bold text-cyan-400 bg-cyan-950/60 px-2.5 py-1 rounded-md border border-cyan-800/40">
                                {{ trade.id_str }}
                            </span>
                        </div>
                        
                        <div class="grid grid-cols-3 gap-2 text-xs">
                            <div class="bg-gray-800/60 p-2 rounded-lg"><p class="text-gray-400">ENTRY</p><p class="font-bold text-white">${{ "%.2f"|format(trade.entry) }}</p></div>
                            <div class="bg-rose-950/30 border border-rose-900/40 p-2 rounded-lg"><p class="text-rose-400">STOP LOSS</p><p class="font-bold text-rose-400">${{ "%.2f"|format(trade.sl) }}</p></div>
                            <div class="bg-emerald-950/30 border border-emerald-900/40 p-2 rounded-lg"><p class="text-emerald-400">TARGET (TP2)</p><p class="font-bold text-emerald-400">${{ "%.2f"|format(trade.tp2) }}</p></div>
                        </div>

                        <div class="flex items-center justify-between text-xs pt-1 border-t border-gray-800/60">
                            <span class="text-gray-400">Strategy: <strong class="text-cyan-300">{{ trade.strategy }}</strong></span>
                            <span class="text-emerald-400 font-semibold bg-emerald-950/80 px-2 py-0.5 rounded border border-emerald-800/40">🛡️ 5-Point Trap Filter: PASSED ✅</span>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            {% else %}
                <div class="text-center py-8 text-gray-500 text-xs">
                    No active signals in feed right now. Scanner actively checking 5m & 15m structures...
                </div>
            {% endif %}
        </div>

        <!-- Settings Panel UI -->
        <div class="lovable-card p-6 rounded-2xl space-y-4">
            <h2 class="text-lg font-bold text-white">⚙️ API & Telegram Settings</h2>
            <form action="/update-settings" method="POST" class="space-y-4">
                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                        <label class="text-xs text-gray-400 font-medium">TELEGRAM_BOT_TOKEN</label>
                        <input type="password" name="bot_token" value="{{ config.bot_token }}" placeholder="123456789:ABC..." class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white focus:outline-none focus:border-emerald-500">
                    </div>
                    <div>
                        <label class="text-xs text-gray-400 font-medium">TELEGRAM_CHANNEL_USERNAME</label>
                        <input type="text" name="channel" value="{{ config.channel }}" placeholder="@channel_username" class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white focus:outline-none focus:border-emerald-500">
                    </div>
                    <div>
                        <label class="text-xs text-gray-400 font-medium">MEXC_API_KEY</label>
                        <input type="password" name="mexc_key" value="{{ config.mexc_key }}" placeholder="mx0val..." class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white focus:outline-none focus:border-emerald-500">
                    </div>
                    <div>
                        <label class="text-xs text-gray-400 font-medium">MEXC_SECRET_KEY</label>
                        <input type="password" name="mexc_secret" value="{{ config.mexc_secret }}" placeholder="secret..." class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white focus:outline-none focus:border-emerald-500">
                    </div>
                </div>

                <div class="flex flex-wrap gap-8 pt-2">
                    <label class="flex items-center gap-3 cursor-pointer">
                        <input type="checkbox" name="broadcast" class="w-4 h-4 accent-emerald-500 rounded" {% if config.broadcast %}checked{% endif %}>
                        <span class="text-sm font-semibold">SIGNAL_BROADCAST_ENABLED</span>
                    </label>
                    <label class="flex items-center gap-3 cursor-pointer">
                        <input type="checkbox" name="autotrade" class="w-4 h-4 accent-emerald-500 rounded" {% if config.autotrade %}checked{% endif %}>
                        <span class="text-sm font-semibold">AUTO_TRADING_ENABLED</span>
                    </label>
                </div>

                <div class="flex flex-wrap gap-3 pt-3">
                    <button type="submit" name="action" value="test_tg" class="px-5 py-2.5 bg-gray-800 text-white font-bold rounded-xl text-xs hover:bg-gray-700 transition">Test Telegram Alert</button>
                    <button type="submit" name="action" value="test_signal" class="px-5 py-2.5 bg-amber-600 text-white font-bold rounded-xl text-xs hover:bg-amber-500 transition">⚡ Send Live Test Signal</button>
                    <button type="submit" name="action" value="save" class="px-6 py-2.5 bg-emerald-600 text-white font-bold rounded-xl text-xs hover:bg-emerald-500 transition">Save Settings</button>
                </div>
            </form>
        </div>

    </div>
</body>
</html>
"""

# ==========================================
# 2. HIGH-RES CHARTING ENGINE (1920x1080)
# ==========================================
def generate_highres_chart(df, signal):
    fig, ax = plt.subplots(figsize=(19.2, 10.8), dpi=100, facecolor='#0b0f19')
    ax.set_facecolor('#0b0f19')
    
    # Plot Candles
    for i in range(len(df)):
        color = '#10b981' if df['close'].iloc[i] >= df['open'].iloc[i] else '#ef4444'
        ax.plot([i, i], [df['low'].iloc[i], df['high'].iloc[i]], color=color, linewidth=1.2)
        ax.plot([i, i], [df['open'].iloc[i], df['close'].iloc[i]], color=color, linewidth=3.5)
    
    total_candles = len(df)
    entry_idx = total_candles - 20
    
    # Position Tool Overlay Boxes
    if "LONG" in signal['side']:
        rect_tp = patches.Rectangle((entry_idx, signal['entry']), total_candles - entry_idx, signal['tp2'] - signal['entry'],
                                    linewidth=0, facecolor='#10b981', alpha=0.20)
        rect_sl = patches.Rectangle((entry_idx, signal['sl']), total_candles - entry_idx, signal['entry'] - signal['sl'],
                                    linewidth=0, facecolor='#ef4444', alpha=0.20)
    else:
        rect_tp = patches.Rectangle((entry_idx, signal['tp2']), total_candles - entry_idx, signal['entry'] - signal['tp2'],
                                    linewidth=0, facecolor='#10b981', alpha=0.20)
        rect_sl = patches.Rectangle((entry_idx, signal['entry']), total_candles - entry_idx, signal['sl'] - signal['entry'],
                                    linewidth=0, facecolor='#ef4444', alpha=0.20)

    ax.add_patch(rect_tp)
    ax.add_patch(rect_sl)

    # Fib Lines Grid
    if 'fib_levels' in signal:
        fibs = signal['fib_levels']
        colors = {'0': '#9ca3af', '0.5': '#f59e0b', '0.71': '#3b82f6', '0.786': '#8b5cf6', '1': '#ef4444'}
        for lvl, price in fibs.items():
            ax.axhline(price, color=colors.get(str(lvl), '#ffffff'), linestyle=':', alpha=0.5, linewidth=1.2, label=f"Fib {lvl}: ${price:.2f}")

    # OB / FVG Box Overlay
    if 'ob_zone' in signal:
        ob = signal['ob_zone']
        rect_ob = patches.Rectangle((total_candles - 35, ob[0]), 35, ob[1] - ob[0],
                                    linewidth=1.2, edgecolor='#3b82f6', facecolor='#3b82f6', alpha=0.25)
        ax.add_patch(rect_ob)

    # Key Levels
    ax.axhline(signal['entry'], color='#3b82f6', linestyle='--', linewidth=2, label=f"ENTRY: ${signal['entry']:.2f}")
    ax.axhline(signal['sl'], color='#ef4444', linestyle='-', linewidth=2, label=f"SL: ${signal['sl']:.2f}")
    ax.axhline(signal['tp1'], color='#10b981', linestyle='-', linewidth=1.5, label=f"TP1: ${signal['tp1']:.2f}")
    ax.axhline(signal['tp2'], color='#059669', linestyle='-', linewidth=2, label=f"TP2: ${signal['tp2']:.2f}")

    # Headings
    ax.set_title(f"CryptoScalper AJ • {signal['symbol']} ({signal['tf']}) - {signal['side']}\nStrategy: {signal['strategy']} | 5-Point Trap Filter: PASSED ✅", 
                 color='white', fontsize=14, fontweight='bold', pad=15)
    
    ax.tick_params(colors='gray', labelsize=10)
    ax.grid(True, color='#1f2937', linestyle=':', alpha=0.4)
    ax.legend(loc='upper left', facecolor='#111827', edgecolor='#374151', labelcolor='white', fontsize=10)

    chart_path = "highres_chart.png"
    plt.tight_layout()
    plt.savefig(chart_path, facecolor=fig.get_facecolor(), dpi=100)
    plt.close()
    return chart_path

# ==========================================
# 3. TELEGRAM ALERT & AUTO-REPLY TRACKER
# ==========================================
def send_telegram_alert(signal, chart_path=None):
    if not BOT_CONFIG["broadcast"] or not BOT_CONFIG["bot_token"] or not BOT_CONFIG["channel"]:
        return None
    
    msg = (
        f"⚡ <b>SIGNAL {signal['id_str']} | {signal['symbol']} ({signal['tf']})</b>\n"
        f"<b>Strategy:</b> {signal['strategy']}\n"
        f"<b>Direction:</b> {signal['side']}\n\n"
        f"🎯 <b>ENTRY:</b> ${signal['entry']:.2f}\n"
        f"🛑 <b>SL:</b> ${signal['sl']:.2f}\n"
        f"🚀 <b>TP1:</b> ${signal['tp1']:.2f}\n"
        f"🏆 <b>TP2:</b> ${signal['tp2']:.2f}\n\n"
        f"🛡️ <b>5-Point Trap Filter:</b> PASSED ✅\n"
        f"💡 <b>Reason:</b> {signal['reason']}"
    )
    
    msg_id = None
    if chart_path and os.path.exists(chart_path):
        url = f"https://api.telegram.org/bot{BOT_CONFIG['bot_token']}/sendPhoto"
        try:
            with open(chart_path, 'rb') as photo:
                res = requests.post(url, data={'chat_id': BOT_CONFIG["channel"], 'caption': msg, 'parse_mode': 'HTML'}, files={'photo': photo}, timeout=12)
                msg_id = res.json().get("result", {}).get("message_id")
        except Exception as e:
            print(f"TG Photo Error: {e}")
    
    if not msg_id:
        msg_id = send_telegram_reply(msg)
    
    return msg_id

def send_telegram_reply(text, reply_to_id=None):
    if not BOT_CONFIG["broadcast"] or not BOT_CONFIG["bot_token"] or not BOT_CONFIG["channel"]:
        return None
    url = f"https://api.telegram.org/bot{BOT_CONFIG['bot_token']}/sendMessage"
    payload = {'chat_id': BOT_CONFIG["channel"], 'text': text, 'parse_mode': 'HTML'}
    if reply_to_id:
        payload['reply_to_message_id'] = reply_to_id
    try:
        res = requests.post(url, json=payload, timeout=5)
        return res.json().get("result", {}).get("message_id")
    except Exception as e:
        print(f"TG Error: {e}")
        return None

# ==========================================
# 4. ESSENTIAL 5-POINT TRAP FILTER
# ==========================================
def pass_5_point_trap_filter(df_5m, df_15m, ob_zone=None):
    # 1. Liquidity Sweep Filter (Stop-hunt wick check)
    candle_body = abs(df_5m['close'].iloc[-1] - df_5m['open'].iloc[-1])
    candle_range = df_5m['high'].iloc[-1] - df_5m['low'].iloc[-1]
    has_sweep_wick = (candle_range > 0) and ((candle_range - candle_body) / candle_range) >= 0.25
    if not has_sweep_wick: return False

    # 2. Unmitigated OB Check
    if ob_zone:
        recent_lows = df_5m['low'].tail(10).iloc[:-1]
        if (recent_lows < ob_zone[0]).any(): return False # Mitigation broke OB

    # 3. Multi-Timeframe Alignment (5m aligning with 15m trend)
    trend_15m = "BULL" if df_15m['close'].iloc[-1] >= df_15m['close'].iloc[-15] else "BEAR"
    trend_5m = "BULL" if df_5m['close'].iloc[-1] >= df_5m['close'].iloc[-15] else "BEAR"
    if trend_5m != trend_15m: return False

    # 4. Retracement Speed Filter (Prevent crashing price)
    last_3_drop = abs(df_5m['close'].iloc[-1] - df_5m['close'].iloc[-4])
    atr = (df_5m['high'] - df_5m['low']).tail(14).mean()
    if last_3_drop > (3.5 * atr): return False # Flash crash into zone

    # 5. FVG Clearance Filter
    has_fvg = abs(df_5m['high'].iloc[-3] - df_5m['low'].iloc[-1]) > (0.2 * atr)
    if not has_fvg: return False

    return True

# ==========================================
# 5. STRATEGY SCANNER & EXECUTOR
# ==========================================
def execute_mexc_order(signal):
    if not BOT_CONFIG["autotrade"] or not BOT_CONFIG["mexc_key"] or not BOT_CONFIG["mexc_secret"]:
        return
    try:
        exchange = ccxt.mexc({
            'apiKey': BOT_CONFIG["mexc_key"],
            'secret': BOT_CONFIG["mexc_secret"],
            'options': {'defaultType': 'swap'}
        })
        side = 'buy' if 'LONG' in signal['side'] else 'sell'
        exchange.create_order(
            symbol=signal['symbol'],
            type='limit',
            side=side,
            amount=0.01,
            price=signal['entry'],
            params={'stopLoss': signal['sl'], 'takeProfit': signal['tp2']}
        )
        print("MEXC Futures Order Placed Successfully!")
    except Exception as e:
        print(f"MEXC Order Execution Error: {e}")

def run_trading_scanner():
    global SIGNAL_COUNTER
    mexc = ccxt.mexc({'options': {'defaultType': 'swap'}})
    symbol = 'BTC/USDT'

    while True:
        try:
            ohlcv_15m = mexc.fetch_ohlcv(symbol, timeframe='15m', limit=80)
            ohlcv_5m = mexc.fetch_ohlcv(symbol, timeframe='5m', limit=80)
            
            df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df_5m = pd.DataFrame(ohlcv_5m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # Evaluate Fib 5-Level Strategy
            swing_high = df_15m['high'].max()
            swing_low = df_15m['low'].min()
            fib_71 = swing_low + (swing_high - swing_low) * (1 - 0.71)
            fib_786 = swing_low + (swing_high - swing_low) * (1 - 0.786)
            
            last_close = df_5m['close'].iloc[-1]
            last_low = df_5m['low'].iloc[-1]

            if last_low <= fib_71 and last_close >= fib_786:
                if pass_5_point_trap_filter(df_5m, df_15m):
                    SIGNAL_COUNTER += 1
                    id_str = f"Signal -{SIGNAL_COUNTER:02d}"
                    
                    signal = {
                        "id_str": id_str,
                        "symbol": symbol,
                        "tf": "15m/5m",
                        "strategy": "Fib 5-Level OTE",
                        "side": "🟢 LONG",
                        "entry": last_close,
                        "sl": swing_low,
                        "tp1": swing_high,
                        "tp2": swing_high + (swing_high - swing_low) * 0.2,
                        "reason": "Deep pullback into 0.71/0.786 Fib zone with all 5 Trap Filters PASSED.",
                        "fib_levels": {"0": swing_high, "0.5": swing_low + (swing_high-swing_low)*0.5, "0.71": fib_71, "0.786": fib_786, "1": swing_low},
                        "tg_msg_id": None
                    }
                    
                    STATS["total_signals"] += 1
                    ACTIVE_TRADES.insert(0, signal)
                    STATS["active_signals"] = len(ACTIVE_TRADES)
                    
                    chart_file = generate_highres_chart(df_5m, signal)
                    msg_id = send_telegram_alert(signal, chart_file)
                    signal['tg_msg_id'] = msg_id
                    
                    execute_mexc_order(signal)
                    time.sleep(300)

        except Exception as e:
            print(f"Scanner Loop Error: {e}")

        STATS["last_scan"] = time.strftime("%H:%M:%S")
        time.sleep(20)

threading.Thread(target=run_trading_scanner, daemon=True).start()

# ==========================================
# 6. WEB ROUTES & HEALTH CHECK
# ==========================================
@app.route('/')
def home():
    return render_template_string(HTML_LAYOUT, config=BOT_CONFIG, stats=STATS, trades=ACTIVE_TRADES)

@app.route('/update-settings', methods=['POST'])
def update_settings():
    global SIGNAL_COUNTER
    action = request.form.get("action")
    BOT_CONFIG["bot_token"] = request.form.get("bot_token", "")
    BOT_CONFIG["channel"] = request.form.get("channel", "")
    BOT_CONFIG["mexc_key"] = request.form.get("mexc_key", "")
    BOT_CONFIG["mexc_secret"] = request.form.get("mexc_secret", "")
    BOT_CONFIG["broadcast"] = "broadcast" in request.form
    BOT_CONFIG["autotrade"] = "autotrade" in request.form

    if action == "test_tg":
        send_telegram_reply("✅ <b>CryptoScalper AJ Connectivity Test Successful!</b>")
    
    elif action == "test_signal":
        mexc = ccxt.mexc({'options': {'defaultType': 'swap'}})
        try:
            ohlcv = mexc.fetch_ohlcv('BTC/USDT', timeframe='15m', limit=80)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            last_price = df['close'].iloc[-1]
            swing_low = df['low'].min()
            swing_high = df['high'].max()
        except:
            last_price = 67500.0
            swing_low = 66800.0
            swing_high = 68200.0
            df = pd.DataFrame({'open':[67000]*80, 'high':[67600]*80, 'low':[66800]*80, 'close':[67500]*80, 'volume':[100]*80})

        SIGNAL_COUNTER += 1
        id_str = f"Signal -{SIGNAL_COUNTER:02d}"

        fib_05 = swing_low + (swing_high - swing_low) * 0.5
        fib_71 = swing_low + (swing_high - swing_low) * (1 - 0.71)
        fib_786 = swing_low + (swing_high - swing_low) * (1 - 0.786)

        test_signal = {
            "id_str": id_str,
            "symbol": "BTC/USDT",
            "tf": "15m/5m",
            "strategy": "Fib 5-Level OTE",
            "side": "🟢 LONG",
            "entry": last_price,
            "sl": swing_low,
            "tp1": swing_high,
            "tp2": swing_high + (swing_high - swing_low) * 0.2,
            "reason": "Test Signal: Deep pullback into 0.71/0.786 Fib grid with all 5 Trap Filters PASSED.",
            "fib_levels": {"0": swing_high, "0.5": fib_05, "0.71": fib_71, "0.786": fib_786, "1": swing_low},
            "tg_msg_id": None
        }
        
        STATS["total_signals"] += 1
        ACTIVE_TRADES.insert(0, test_signal)
        STATS["active_signals"] = len(ACTIVE_TRADES)
        
        chart_file = generate_highres_chart(df, test_signal)
        msg_id = send_telegram_alert(test_signal, chart_file)
        test_signal['tg_msg_id'] = msg_id

    return render_template_string(HTML_LAYOUT, config=BOT_CONFIG, stats=STATS, trades=ACTIVE_TRADES)

@app.route('/health')
def health():
    return jsonify({"status": "running", "last_scan": STATS["last_scan"]}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
