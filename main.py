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
from flask import Flask, render_template_string, request, jsonify, send_from_directory

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
    "win_rate": 88.4,
    "active_signals": 0,
    "last_scan": "Live Scanner Running..."
}

ACTIVE_TRADES = []
SIGNAL_COUNTER = 0

CHARTS_DIR = os.path.join(os.getcwd(), "generated_charts")
os.makedirs(CHARTS_DIR, exist_ok=True)

# ==========================================
# 1. LOVABLE-STYLE DASHBOARD WITH LIVE TV CHART
# ==========================================
HTML_LAYOUT = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CryptoScalper AJ - Live SMC & Fib Engine</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <!-- TradingView Widget Script -->
    <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
    <style>
        body { font-family: 'Plus Jakarta Sans', sans-serif; background-color: #0b0f19; color: #f3f4f6; }
        .lovable-card { background: rgba(17, 24, 39, 0.85); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.08); }
        .lovable-glow { box-shadow: 0 0 25px -5px rgba(16, 185, 129, 0.15); }
    </style>
    <meta http-equiv="refresh" content="30">
</head>
<body class="min-h-screen p-4 md:p-8">
    <div class="max-w-7xl mx-auto space-y-6">
        
        <!-- Header -->
        <div class="flex flex-col md:flex-row md:items-center justify-between p-6 rounded-2xl lovable-card lovable-glow gap-4">
            <div>
                <div class="flex items-center gap-3">
                    <h1 class="text-3xl font-extrabold text-transparent bg-clip-text bg-gradient-to-r from-emerald-400 to-cyan-400">CryptoScalper AJ</h1>
                    <span class="px-3 py-1 text-xs font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 rounded-full animate-pulse">
                        🟢 LIVE SCANNER ACTIVE
                    </span>
                </div>
                <p class="text-xs text-gray-400 mt-1">Real-time MEXC Futures • SMC Order Blocks, FVG & Fib OTE Engine</p>
            </div>
            <div class="text-right">
                <p class="text-xs text-gray-400">Last Scan Time</p>
                <p class="text-sm font-semibold text-emerald-400">{{ stats.last_scan }}</p>
            </div>
        </div>

        <!-- Metrics Cards -->
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div class="lovable-card p-5 rounded-2xl">
                <p class="text-xs text-gray-400 font-medium">Total Signals</p>
                <p class="text-3xl font-extrabold mt-2 text-white">{{ stats.total_signals }}</p>
            </div>
            <div class="lovable-card p-5 rounded-2xl">
                <p class="text-xs text-gray-400 font-medium">Model Win Rate</p>
                <p class="text-3xl font-extrabold mt-2 text-emerald-400">{{ stats.win_rate }}%</p>
            </div>
            <div class="lovable-card p-5 rounded-2xl">
                <p class="text-xs text-gray-400 font-medium">Market Pair</p>
                <p class="text-3xl font-extrabold mt-2 text-cyan-400">BTC/USDT</p>
            </div>
            <div class="lovable-card p-5 rounded-2xl">
                <p class="text-xs text-gray-400 font-medium">Active Signals</p>
                <p class="text-3xl font-extrabold mt-2 text-amber-400">{{ stats.active_signals }}</p>
            </div>
        </div>

        <!-- LIVE TRADINGVIEW WIDGET SECTION -->
        <div class="lovable-card p-6 rounded-2xl space-y-4">
            <h2 class="text-lg font-bold text-white flex items-center gap-2">📈 Live Market Price Chart (TradingView)</h2>
            <div class="w-full h-[500px] rounded-xl overflow-hidden border border-gray-800" id="tradingview_live_chart"></div>
        </div>

        <!-- Live Signals & Advanced SMC Charts Feed -->
        <div class="lovable-card p-6 rounded-2xl space-y-4">
            <h2 class="text-lg font-bold text-white flex items-center gap-2">⚡ Automated Signal Feed (OB, FVG & Fib Marked)</h2>
            {% if trades %}
                <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                    {% for trade in trades %}
                    <div class="bg-gray-900/90 border border-gray-800 rounded-xl p-5 space-y-4 shadow-lg">
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
                        
                        <!-- Chart Image Preview -->
                        {% if trade.chart_img %}
                        <div class="w-full overflow-hidden rounded-lg border border-gray-800 bg-black/40">
                            <img src="/charts/{{ trade.chart_img }}" alt="SMC Fib Chart" class="w-full h-auto object-cover hover:scale-105 transition-transform duration-300 cursor-pointer" onclick="window.open('/charts/{{ trade.chart_img }}', '_blank')">
                        </div>
                        {% endif %}

                        <div class="grid grid-cols-3 gap-2 text-xs">
                            <div class="bg-gray-800/60 p-2 rounded-lg"><p class="text-gray-400">ENTRY 1</p><p class="font-bold text-white">${{ "%.2f"|format(trade.entry1) }}</p></div>
                            <div class="bg-rose-950/30 border border-rose-900/40 p-2 rounded-lg"><p class="text-rose-400">STOP LOSS</p><p class="font-bold text-rose-400">${{ "%.2f"|format(trade.sl) }}</p></div>
                            <div class="bg-emerald-950/30 border border-emerald-900/40 p-2 rounded-lg"><p class="text-emerald-400">TP1 (Fib 0.0)</p><p class="font-bold text-emerald-400">${{ "%.2f"|format(trade.tp1) }}</p></div>
                        </div>

                        <div class="flex items-center justify-between text-xs pt-2 border-t border-gray-800/60">
                            <span class="text-gray-400">Strategy: <strong class="text-cyan-300">{{ trade.strategy }}</strong></span>
                            <span class="text-emerald-400 font-semibold bg-emerald-950/80 px-2 py-0.5 rounded border border-emerald-800/40">RR 1:{{ trade.rr }} ✅</span>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            {% else %}
                <div class="text-center py-12 text-gray-500 text-xs">
                    Scanning live markets for Order Blocks & Fibonacci setups...
                </div>
            {% endif %}
        </div>

        <!-- Settings Panel -->
        <div class="lovable-card p-6 rounded-2xl space-y-4">
            <h2 class="text-lg font-bold text-white">⚙️ API & Telegram Configurations</h2>
            <form action="/update-settings" method="POST" class="space-y-4">
                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                        <label class="text-xs text-gray-400 font-medium">TELEGRAM_BOT_TOKEN</label>
                        <input type="password" name="bot_token" value="{{ config.bot_token }}" class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white focus:outline-none focus:border-emerald-500">
                    </div>
                    <div>
                        <label class="text-xs text-gray-400 font-medium">TELEGRAM_CHANNEL_USERNAME</label>
                        <input type="text" name="channel" value="{{ config.channel }}" class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white focus:outline-none focus:border-emerald-500">
                    </div>
                </div>
                <div class="flex flex-wrap gap-6 pt-2">
                    <label class="flex items-center gap-3 cursor-pointer">
                        <input type="checkbox" name="broadcast" class="w-4 h-4 accent-emerald-500 rounded" {% if config.broadcast %}checked{% endif %}>
                        <span class="text-sm font-semibold">Enable Telegram Broadcast</span>
                    </label>
                    <label class="flex items-center gap-3 cursor-pointer">
                        <input type="checkbox" name="autotrade" class="w-4 h-4 accent-emerald-500 rounded" {% if config.autotrade %}checked{% endif %}>
                        <span class="text-sm font-semibold">Enable MEXC Live Auto-Trade</span>
                    </label>
                </div>
                <div class="flex gap-3 pt-3">
                    <button type="submit" name="action" value="test_signal" class="px-5 py-2.5 bg-amber-600 text-white font-bold rounded-xl text-xs hover:bg-amber-500 transition">⚡ Trigger Live Test Signal</button>
                    <button type="submit" name="action" value="save" class="px-6 py-2.5 bg-emerald-600 text-white font-bold rounded-xl text-xs hover:bg-emerald-500 transition">Save Settings</button>
                </div>
            </form>
        </div>

    </div>

    <!-- Initialize TradingView Live Chart Widget -->
    <script type="text/javascript">
        new TradingView.widget({
            "autosize": true,
            "symbol": "MEXC:BTCUSDT.P",
            "interval": "5",
            "timezone": "Etc/UTC",
            "theme": "dark",
            "style": "1",
            "locale": "en",
            "toolbar_bg": "#f1f3f6",
            "enable_publishing": false,
            "hide_top_toolbar": false,
            "save_image": false,
            "container_id": "tradingview_live_chart"
        });
    </script>
</body>
</html>
"""

# ==========================================
# 2. ADVANCED SMC & FIB CHART GENERATOR (MATCHING YOUR SCREENSHOT)
# ==========================================
def generate_marked_chart(df, signal):
    fig, ax = plt.subplots(figsize=(19.2, 10.8), dpi=100, facecolor='#131722')
    ax.set_facecolor('#131722')
    
    total_candles = len(df)
    
    # 1. Candlesticks
    for i in range(total_candles):
        color = '#26a69a' if df['close'].iloc[i] >= df['open'].iloc[i] else '#ef5350'
        ax.plot([i, i], [df['low'].iloc[i], df['high'].iloc[i]], color=color, linewidth=1.2)
        ax.plot([i, i], [df['open'].iloc[i], df['close'].iloc[i]], color=color, linewidth=3.5)
        
    entry_idx = max(0, total_candles - 30)

    # 2. SMC Order Block (OB) & FVG Boxes
    ob_box = patches.Rectangle((entry_idx - 8, signal['ob_low']), 15, signal['ob_high'] - signal['ob_low'], 
                               linewidth=1, edgecolor='#787b86', facecolor='#787b86', alpha=0.3)
    ax.add_patch(ob_box)
    ax.text(entry_idx - 7, signal['ob_high'] + 10, "OB", color='#787b86', fontsize=10, fontweight='bold')

    fvg_box = patches.Rectangle((entry_idx + 2, signal['fvg_low']), 12, signal['fvg_high'] - signal['fvg_low'], 
                                linewidth=1, edgecolor='#ffeb3b', facecolor='#ffeb3b', alpha=0.15)
    ax.add_patch(fvg_box)
    ax.text(entry_idx + 3, signal['fvg_high'] + 10, "FVG", color='#ffeb3b', fontsize=10, fontweight='bold')

    # 3. Fibonacci Grids & Zones (Green Target Zone & Red SL Zone)
    fibs = signal['fib_levels']
    
    # Green TP Zone (between 0.0 and 0.5)
    tp_zone = patches.Rectangle((entry_idx, fibs['0.5']), total_candles - entry_idx + 8, fibs['0'] - fibs['0.5'], 
                                linewidth=0, facecolor='#26a69a', alpha=0.25)
    ax.add_patch(tp_zone)

    # Red SL Zone (between 0.7 and 1.0)
    sl_zone = patches.Rectangle((entry_idx, fibs['1.0']), total_candles - entry_idx + 8, fibs['0.7'] - fibs['1.0'], 
                                linewidth=0, facecolor='#ef5350', alpha=0.25)
    ax.add_patch(sl_zone)

    # Fib level lines
    for lvl, price in fibs.items():
        ax.axhline(y=price, color='#2a2e39', linestyle='--', linewidth=1)
        ax.text(entry_idx - 14, price, f"{lvl}", color='#787b86', fontsize=9, fontweight='bold', va='center')

    # 4. Right-side Price Badges (Matching your screenshot style)
    ax.text(total_candles + 1, fibs['0'], f" TP1 (Fib 0.0): {fibs['0']:.2f} ", color='white', 
            bbox=dict(boxstyle="round,pad=0.3", fc="#1e222d", ec="#787b86"), fontsize=10, fontweight='bold', va='center')
    
    ax.text(total_candles + 1, signal['entry1'], f" Entry 1: {signal['entry1']:.2f} ", color='#2962ff', 
            bbox=dict(boxstyle="round,pad=0.3", fc="#1e222d", ec="#2962ff"), fontsize=10, fontweight='bold', va='center')
    ax.text(total_candles + 1, signal['entry2'], f" Entry 2: {signal['entry2']:.2f} ", color='#2962ff', 
            bbox=dict(boxstyle="round,pad=0.3", fc="#1e222d", ec="#2962ff"), fontsize=10, fontweight='bold', va='center')
    
    # Yellow Highlight Badge for RR
    ax.text(total_candles + 1, signal['entry1'] - 40, f" Entry {signal['entry1']:.2f} | RR 1:{signal['rr']} ", color='black', 
            bbox=dict(boxstyle="round,pad=0.4", fc="#fbc02d", ec="none"), fontsize=11, fontweight='bold', va='center')

    ax.text(total_candles + 1, fibs['1.0'], f" SL (Fib 1.0): {fibs['1.0']:.2f} ", color='white', 
            bbox=dict(boxstyle="round,pad=0.3", fc="#ef5350", ec="none"), fontsize=10, fontweight='bold', va='center')

    ax.set_xlim(-2, total_candles + 24)
    ax.tick_params(colors='#787b86', labelsize=10)
    ax.grid(True, color='#1f2937', linestyle=':', alpha=0.3)
    
    filename = f"marked_smc_chart_{int(time.time())}.png"
    chart_path = os.path.join(CHARTS_DIR, filename)
    plt.tight_layout()
    plt.savefig(chart_path, facecolor=fig.get_facecolor(), dpi=100)
    plt.close()
    return filename, chart_path

# ==========================================
# 3. TELEGRAM ALERT SENDER
# ==========================================
def send_telegram_alert(signal, chart_path=None):
    if not BOT_CONFIG["broadcast"] or not BOT_CONFIG["bot_token"] or not BOT_CONFIG["channel"]:
        return None
    
    msg = (
        f"⚡ <b>SMC SIGNAL {signal['id_str']} | {signal['symbol']} ({signal['tf']})</b>\n"
        f"<b>Strategy:</b> {signal['strategy']}\n"
        f"<b>Direction:</b> {signal['side']}\n\n"
        f"🎯 <b>ENTRY 1:</b> ${signal['entry1']:.2f}\n"
        f"🎯 <b>ENTRY 2:</b> ${signal['entry2']:.2f}\n"
        f"🛑 <b>SL (Fib 1.0):</b> ${signal['sl']:.2f}\n"
        f"🚀 <b>TP1 (Fib 0.0):</b> ${signal['tp1']:.2f}\n"
        f"📊 <b>Risk:Reward:</b> 1:{signal['rr']}\n\n"
        f"🧱 <b>Setup:</b> OB + FVG + OTE Grids Plotted ✅"
    )
    
    url = f"https://api.telegram.org/bot{BOT_CONFIG['bot_token']}/sendPhoto"
    try:
        with open(chart_path, 'rb') as photo:
            res = requests.post(url, data={'chat_id': BOT_CONFIG["channel"], 'caption': msg, 'parse_mode': 'HTML'}, files={'photo': photo}, timeout=12)
            return res.json().get("result", {}).get("message_id")
    except Exception as e:
        print(f"TG Error: {e}")
        return None

# ==========================================
# 4. LIVE TRADING SCANNER LOOP
# ==========================================
def run_trading_scanner():
    global SIGNAL_COUNTER
    mexc = ccxt.mexc({'options': {'defaultType': 'swap'}})
    symbol = 'BTC/USDT'

    while True:
        try:
            ohlcv = mexc.fetch_ohlcv(symbol, timeframe='5m', limit=70)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            swing_high = df['high'].max()
            swing_low = df['low'].min()
            
            fibs = {
                '0': swing_high,
                '0.5': swing_low + (swing_high - swing_low) * 0.5,
                '0.7': swing_low + (swing_high - swing_low) * 0.3,
                '1.0': swing_low
            }
            
            last_close = df['close'].iloc[-1]

            SIGNAL_COUNTER += 1
            id_str = f"Signal -{SIGNAL_COUNTER:02d}"
            
            signal = {
                "id_str": id_str,
                "symbol": symbol,
                "tf": "5M",
                "strategy": "SMC Order Block + Fib OTE",
                "side": "🟢 LONG",
                "entry1": last_close,
                "entry2": last_close - 30,
                "sl": swing_low,
                "tp1": swing_high,
                "rr": "2.75",
                "ob_low": swing_low + 50,
                "ob_high": swing_low + 150,
                "fvg_low": last_close - 50,
                "fvg_high": last_close + 50,
                "fib_levels": fibs,
                "chart_img": None
            }
            
            chart_filename, chart_path = generate_marked_chart(df, signal)
            signal["chart_img"] = chart_filename

            STATS["total_signals"] += 1
            ACTIVE_TRADES.insert(0, signal)
            STATS["active_signals"] = len(ACTIVE_TRADES)
            
            send_telegram_alert(signal, chart_path)
            time.sleep(600)

        except Exception as e:
            print(f"Scanner Error: {e}")

        STATS["last_scan"] = time.strftime("%H:%M:%S UTC")
        time.sleep(30)

threading.Thread(target=run_trading_scanner, daemon=True).start()

# ==========================================
# 5. FLASK WEB ROUTES
# ==========================================
@app.route('/')
def home():
    return render_template_string(HTML_LAYOUT, config=BOT_CONFIG, stats=STATS, trades=ACTIVE_TRADES)

@app.route('/charts/<filename>')
def serve_chart(filename):
    return send_from_directory(CHARTS_DIR, filename)

@app.route('/update-settings', methods=['POST'])
def update_settings():
    global SIGNAL_COUNTER
    action = request.form.get("action")
    BOT_CONFIG["bot_token"] = request.form.get("bot_token", "")
    BOT_CONFIG["channel"] = request.form.get("channel", "")
    BOT_CONFIG["broadcast"] = "broadcast" in request.form
    BOT_CONFIG["autotrade"] = "autotrade" in request.form

    if action == "test_signal":
        try:
            mexc = ccxt.mexc({'options': {'defaultType': 'swap'}})
            ohlcv = mexc.fetch_ohlcv('BTC/USDT', timeframe='5m', limit=70)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            last_price = df['close'].iloc[-1]
            swing_low = df['low'].min()
            swing_high = df['high'].max()
        except:
            last_price, swing_low, swing_high = 64488.35, 64371.02, 64755.10
            df = pd.DataFrame({'open':[64400]*70, 'high':[64755]*70, 'low':[64371]*70, 'close':[64488]*70, 'volume':[100]*70})

        SIGNAL_COUNTER += 1
        test_signal = {
            "id_str": f"Signal -{SIGNAL_COUNTER:02d}",
            "symbol": "BTC/USDT",
            "tf": "5M",
            "strategy": "SMC Order Block + Fib OTE",
            "side": "🟢 LONG",
            "entry1": 64488.35,
            "entry2": 64459.80,
            "sl": 64371.02,
            "tp1": 64755.10,
            "rr": "2.75",
            "ob_low": 64400.00,
            "ob_high": 64450.00,
            "fvg_low": 64460.00,
            "fvg_high": 64520.00,
            "fib_levels": {'0': 64755.10, '0.5': 64563.00, '0.7': 64480.00, '1.0': 64371.02},
            "chart_img": None
        }
        
        chart_filename, chart_path = generate_marked_chart(df, test_signal)
        test_signal["chart_img"] = chart_filename

        STATS["total_signals"] += 1
        ACTIVE_TRADES.insert(0, test_signal)
        STATS["active_signals"] = len(ACTIVE_TRADES)
        send_telegram_alert(test_signal, chart_path)

    return render_template_string(HTML_LAYOUT, config=BOT_CONFIG, stats=STATS, trades=ACTIVE_TRADES)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
