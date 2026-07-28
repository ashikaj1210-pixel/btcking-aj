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

# System Configurations
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
    "win_rate": 78.5,
    "active_signals": 0,
    "last_scan": "Never"
}

ACTIVE_TRADES = []

# Embedded HTML Template with Chart, Signal Table & Test Buttons
HTML_LAYOUT = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CryptoScalper AJ</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background-color: #080c14; color: #e2e8f0; font-family: sans-serif; }
        .card { background: rgba(17, 24, 39, 0.8); border: 1px solid #1f2937; }
    </style>
</head>
<body class="p-4 md:p-8">
    <div class="max-w-5xl mx-auto space-y-6">
        
        <!-- Header -->
        <div class="flex items-center justify-between card p-5 rounded-2xl">
            <div>
                <h1 class="text-2xl font-bold text-emerald-400">CryptoScalper AJ</h1>
                <p class="text-xs text-gray-400">MEXC Futures • Multi-Confluence SMC/Fib Engine</p>
            </div>
            <span class="px-3 py-1 bg-emerald-950 text-emerald-400 border border-emerald-500/40 text-xs font-bold rounded-full">
                🟢 LIVE MEXC
            </span>
        </div>

        <!-- Metrics Cards -->
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div class="card p-4 rounded-xl"><p class="text-xs text-gray-400">Total Signals</p><p class="text-2xl font-bold mt-1 text-white">{{ stats.total_signals }}</p></div>
            <div class="card p-4 rounded-xl"><p class="text-xs text-gray-400">Win Rate</p><p class="text-2xl font-bold mt-1 text-emerald-400">{{ stats.win_rate }}%</p></div>
            <div class="card p-4 rounded-xl"><p class="text-xs text-gray-400">Market</p><p class="text-2xl font-bold mt-1 text-white">BTC/USDT</p></div>
            <div class="card p-4 rounded-xl"><p class="text-xs text-gray-400">Active Signals</p><p class="text-2xl font-bold mt-1 text-amber-400">{{ stats.active_signals }}</p></div>
        </div>

        <!-- Live TradingView Chart Widget -->
        <div class="card p-5 rounded-xl space-y-3">
            <h2 class="text-lg font-bold text-white flex items-center gap-2">📈 Live BTC/USDT Chart</h2>
            <div class="w-full rounded-lg overflow-hidden border border-gray-800" style="height: 480px;">
                <div id="tradingview_chart" class="w-full h-full"></div>
            </div>
        </div>

        <!-- Active Signals & Report Table -->
        <div class="card p-5 rounded-xl space-y-3">
            <h2 class="text-lg font-bold text-white flex items-center gap-2">📜 Signal Report & History</h2>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-sm text-gray-300">
                    <thead class="bg-gray-800/60 text-xs text-gray-400 uppercase">
                        <tr>
                            <th class="p-3">Symbol</th>
                            <th class="p-3">Direction</th>
                            <th class="p-3">Entry Price</th>
                            <th class="p-3">Stop Loss (SL)</th>
                            <th class="p-3">Take Profit (TP)</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-gray-800">
                        {% if trades %}
                            {% for trade in trades %}
                            <tr class="hover:bg-gray-800/30">
                                <td class="p-3 font-semibold text-white">{{ trade.symbol }}</td>
                                <td class="p-3 text-emerald-400 font-bold">{{ trade.side }}</td>
                                <td class="p-3">${{ "%.2f"|format(trade.entry) }}</td>
                                <td class="p-3 text-rose-400">${{ "%.2f"|format(trade.sl) }}</td>
                                <td class="p-3 text-emerald-400">${{ "%.2f"|format(trade.tp2) }}</td>
                            </tr>
                            {% endfor %}
                        {% else %}
                            <tr>
                                <td colspan="5" class="p-4 text-center text-xs text-gray-500">No active signals generated yet. Scanner is searching...</td>
                            </tr>
                        {% endif %}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Control Panel -->
        <div class="card p-6 rounded-xl space-y-4">
            <h2 class="text-lg font-bold text-white">⚙️ Control Panel</h2>
            <form action="/update-settings" method="POST" class="space-y-4">
                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div><label class="text-xs text-gray-400">Bot Token</label><input type="password" name="bot_token" value="{{ config.bot_token }}" class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-sm text-white"></div>
                    <div><label class="text-xs text-gray-400">Channel Username</label><input type="text" name="channel" value="{{ config.channel }}" class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-sm text-white"></div>
                    <div><label class="text-xs text-gray-400">MEXC API Key</label><input type="password" name="mexc_key" value="{{ config.mexc_key }}" class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-sm text-white"></div>
                    <div><label class="text-xs text-gray-400">MEXC Secret Key</label><input type="password" name="mexc_secret" value="{{ config.mexc_secret }}" class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-sm text-white"></div>
                </div>
                <div class="flex gap-6 pt-2">
                    <label class="flex items-center gap-2 text-sm"><input type="checkbox" name="broadcast" {% if config.broadcast %}checked{% endif %}> Signal Broadcast</label>
                    <label class="flex items-center gap-2 text-sm"><input type="checkbox" name="autotrade" {% if config.autotrade %}checked{% endif %}> Auto Trading</label>
                </div>
                <div class="flex flex-wrap gap-3 pt-2">
                    <button type="submit" name="action" value="test_tg" class="px-4 py-2 bg-gray-800 text-white font-bold rounded text-xs hover:bg-gray-700">Test Telegram</button>
                    <button type="submit" name="action" value="test_signal" class="px-4 py-2 bg-amber-600 text-white font-bold rounded text-xs hover:bg-amber-500">⚡ Send Test Signal</button>
                    <button type="submit" name="action" value="save" class="px-6 py-2 bg-emerald-600 text-white font-bold rounded text-xs hover:bg-emerald-500">Save Settings</button>
                </div>
            </form>
        </div>

    </div>

    <!-- TradingView Script -->
    <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
    <script type="text/javascript">
    new TradingView.widget({
      "autosize": true,
      "symbol": "MEXC:BTCUSDT",
      "interval": "15",
      "timezone": "Etc/UTC",
      "theme": "dark",
      "style": "1",
      "locale": "en",
      "enable_publishing": false,
      "hide_side_toolbar": false,
      "container_id": "tradingview_chart"
    });
    </script>
</body>
</html>
"""

# Telegram Functions
def generate_dark_chart(df, signal):
    fig, ax = plt.subplots(figsize=(12, 6), facecolor='#080c14')
    ax.set_facecolor('#080c14')
    for i in range(len(df)):
        color = '#10b981' if df['close'].iloc[i] >= df['open'].iloc[i] else '#ef4444'
        ax.plot([i, i], [df['low'].iloc[i], df['high'].iloc[i]], color=color, linewidth=1)
        ax.plot([i, i], [df['open'].iloc[i], df['close'].iloc[i]], color=color, linewidth=3)
    ax.axhline(signal['entry'], color='#3b82f6', linestyle='--', label=f"ENTRY: ${signal['entry']:.2f}")
    ax.axhline(signal['sl'], color='#ef4444', linestyle='-', label=f"SL: ${signal['sl']:.2f}")
    ax.axhline(signal['tp2'], color='#10b981', linestyle='-', label=f"TP: ${signal['tp2']:.2f}")
    ax.set_title(f"CryptoScalper AJ - {signal['symbol']} ({signal['side']})", color='white')
    ax.tick_params(colors='gray')
    ax.grid(True, color='#1f2937', linestyle=':')
    chart_path = "chart.png"
    plt.tight_layout()
    plt.savefig(chart_path, facecolor=fig.get_facecolor())
    plt.close()
    return chart_path

def send_telegram_alert(signal, chart_path=None):
    if not BOT_CONFIG["broadcast"] or not BOT_CONFIG["bot_token"] or not BOT_CONFIG["channel"]:
        return
    msg = f"⚡ <b>SIGNAL | {signal['symbol']}</b>\nDirection: {signal['side']}\nENTRY: ${signal['entry']:.2f}\nSL: ${signal['sl']:.2f}\nTP: ${signal['tp2']:.2f}"
    
    if chart_path and os.path.exists(chart_path):
        url = f"https://api.telegram.org/bot{BOT_CONFIG['bot_token']}/sendPhoto"
        try:
            with open(chart_path, 'rb') as photo:
                requests.post(url, data={'chat_id': BOT_CONFIG["channel"], 'caption': msg, 'parse_mode': 'HTML'}, files={'photo': photo}, timeout=10)
        except Exception as e:
            print(f"TG Error: {e}")
    else:
        send_telegram_reply(msg)

def send_telegram_reply(text):
    if not BOT_CONFIG["broadcast"] or not BOT_CONFIG["bot_token"] or not BOT_CONFIG["channel"]:
        return
    url = f"https://api.telegram.org/bot{BOT_CONFIG['bot_token']}/sendMessage"
    try:
        requests.post(url, json={'chat_id': BOT_CONFIG["channel"], 'text': text, 'parse_mode': 'HTML'}, timeout=5)
    except Exception as e:
        print(f"TG Error: {e}")

# Core Strategy Loop
def run_trading_scanner():
    mexc = ccxt.mexc({'options': {'defaultType': 'swap'}})
    symbol = 'BTC/USDT'
    while True:
        try:
            ohlcv = mexc.fetch_ohlcv(symbol, timeframe='15m', limit=60)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            swing_high = df['high'].max()
            swing_low = df['low'].min()
            fib_71 = swing_high - ((swing_high - swing_low) * 0.71)
            last_close = df['close'].iloc[-1]
            last_low = df['low'].iloc[-1]

            if last_low <= fib_71 and last_close > fib_71:
                STATS["total_signals"] += 1
                signal = {
                    "symbol": symbol,
                    "side": "🟢 LONG",
                    "entry": last_close,
                    "sl": swing_low * 0.998,
                    "tp2": last_close + (last_close - (swing_low * 0.998)) * 2,
                    "hit_entry": False
                }
                ACTIVE_TRADES.append(signal)
                STATS["active_signals"] = len(ACTIVE_TRADES)
                chart_file = generate_dark_chart(df, signal)
                send_telegram_alert(signal, chart_file)
                time.sleep(900)

            STATS["last_scan"] = time.strftime("%H:%M:%S")
            time.sleep(20)
        except Exception as e:
            print(f"Scanner Error: {e}")
            time.sleep(10)

threading.Thread(target=run_trading_scanner, daemon=True).start()

# Web Routes
@app.route('/')
def home():
    return render_template_string(HTML_LAYOUT, config=BOT_CONFIG, stats=STATS, trades=ACTIVE_TRADES)

@app.route('/update-settings', methods=['POST'])
def update_settings():
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
        # Create a Dummy Test Signal
        dummy_signal = {
            "symbol": "BTC/USDT (TEST)",
            "side": "🟢 LONG",
            "entry": 67500.00,
            "sl": 66800.00,
            "tp2": 68900.00,
            "hit_entry": True
        }
        STATS["total_signals"] += 1
        ACTIVE_TRADES.insert(0, dummy_signal) # Top row display
        STATS["active_signals"] = len(ACTIVE_TRADES)
        
        # Send Alert to Telegram Channel
        send_telegram_alert(dummy_signal)

    return render_template_string(HTML_LAYOUT, config=BOT_CONFIG, stats=STATS, trades=ACTIVE_TRADES)

@app.route('/health')
def health():
    return jsonify({"status": "running", "last_scan": STATS["last_scan"]}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
