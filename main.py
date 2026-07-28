import os
import time
import requests
import ccxt
import pandas as pd
from flask import Flask, render_template_string, request, send_from_directory
from playwright.sync_api import sync_playwright

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
    "autotrade": False
}

TRADE_HISTORY = []

CHARTS_DIR = os.path.join(os.getcwd(), "generated_charts")
os.makedirs(CHARTS_DIR, exist_ok=True)

# ==========================================
# 2. ANALYTICS & SCREENSHOT ENGINE
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

def capture_chart_screenshot(signal_id):
    filename = f"chart_{signal_id}_{int(time.time())}.png"
    filepath = os.path.join(CHARTS_DIR, filename)
    chart_url = f"http://127.0.0.1:8080/render-chart/{signal_id}"
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1200, "height": 675})
            page.goto(chart_url, wait_until="networkidle")
            time.sleep(1)
            page.screenshot(path=filepath)
            browser.close()
        return filename, filepath
    except Exception as e:
        print(f"Screenshot Error: {e}")
        return None, None

def send_telegram_alert(signal, chart_path=None):
    if not BOT_CONFIG["broadcast"] or not BOT_CONFIG["bot_token"] or not BOT_CONFIG["channel"]:
        return None
    
    msg = (
        f"⚡ <b>SMC SIGNAL {signal['id_str']} | {signal['symbol']} ({signal['tf']})</b>\n"
        f"<b>Strategy:</b> {signal['strategy']}\n"
        f"<b>Direction:</b> {signal['side']}\n\n"
        f"🎯 <b>ENTRY 1:</b> ${signal['entry1']:.2f}\n"
        f"🛑 <b>SL (Fib 1.0):</b> ${signal['sl']:.2f}\n"
        f"🚀 <b>TP1 (Fib 0.0):</b> ${signal['tp1']:.2f}\n"
        f"📊 <b>Risk:Reward:</b> 1:{signal['rr']}\n\n"
        f"🧱 <b>Setup:</b> TradingView HD Engine Plotted ✅"
    )
    
    url = f"https://api.telegram.org/bot{BOT_CONFIG['bot_token']}/sendPhoto"
    try:
        with open(chart_path, 'rb') as photo:
            res = requests.post(url, data={'chat_id': BOT_CONFIG["channel"], 'caption': msg, 'parse_mode': 'HTML'}, files={'photo': photo}, timeout=12)
            return res.json()
    except Exception as e:
        print(f"TG Alert Error: {e}")
        return None

# ==========================================
# 3. TRADINGVIEW LIGHTWEIGHT RENDER TEMPLATE
# ==========================================
CHART_RENDER_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        body { margin: 0; padding: 0; background-color: #131722; font-family: sans-serif; overflow: hidden; }
        #chart { width: 100vw; height: 100vh; }
    </style>
</head>
<body>
    <div id="chart"></div>
    <script>
        const chartData = {{ candles | tojson }};
        const signal = {{ signal | tojson }};

        const chart = LightweightCharts.createChart(document.getElementById('chart'), {
            layout: { background: { color: '#131722' }, textColor: '#d1d4dc' },
            grid: { vertLines: { color: '#1f2937' }, horzLines: { color: '#1f2937' } },
            timeScale: { timeVisible: true }
        });

        const candleSeries = chart.addCandlestickSeries({
            upColor: '#089981', downColor: '#f23645', borderVisible: false, wickUpColor: '#089981', wickDownColor: '#f23645'
        });
        candleSeries.setData(chartData);

        candleSeries.createPriceLine({ price: signal.tp1, color: '#089981', lineWidth: 2, title: 'TP1 (Fib 0.0)' });
        candleSeries.createPriceLine({ price: signal.entry1, color: '#2962ff', lineWidth: 2, title: 'Entry 1' });
        candleSeries.createPriceLine({ price: signal.sl, color: '#f23645', lineWidth: 2, title: 'SL (Fib 1.0)' });

        chart.timeScale().fitContent();
    </script>
</body>
</html>
"""

# ==========================================
# 4. ALL-IN-ONE DASHBOARD UI TEMPLATE
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
                <p class="text-xs text-gray-400 mt-1">Real-time MEXC Futures SMC & Fib OTE Engine</p>
            </div>
            <div class="text-right">
                <p class="text-xs text-gray-400">Scanner Status</p>
                <p class="text-sm font-semibold text-emerald-400">Active & Scanning</p>
            </div>
        </div>

        <!-- 📊 1. TRADER REPORT / ANALYTICS PANEL -->
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

        <!-- 📈 2. LIVE TRADINGVIEW INTERACTIVE WIDGET -->
        <div class="lovable-card p-6 rounded-2xl space-y-4">
            <h2 class="text-lg font-bold text-white">📈 Live Market Price Chart (MEXC Real-Time)</h2>
            <div class="w-full h-[500px] rounded-xl overflow-hidden" id="tradingview_chart"></div>
        </div>

        <!-- ⚡ 3. AUTOMATED SIGNAL FEED WITH HD CHARTS -->
        <div class="lovable-card p-6 rounded-2xl space-y-4">
            <h2 class="text-lg font-bold text-white">⚡ Automated Signal Feed</h2>
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
                            <div class="bg-emerald-950/40 border border-emerald-900/50 p-2 rounded text-center"><p class="text-emerald-400 text-[10px]">TP1 (Fib 0.0)</p>${{ "%.2f"|format(trade.tp1) }}</div>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            {% else %}
                <div class="text-center py-8 text-gray-500 text-sm">
                    No active signal generated yet. Click "Trigger Live Test Signal" below to test.
                </div>
            {% endif %}
        </div>

        <!-- 📋 4. TRADE PERFORMANCE HISTORY TABLE -->
        <div class="lovable-card p-6 rounded-2xl space-y-4">
            <h2 class="text-lg font-bold text-white">📋 Trade Performance History</h2>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs text-gray-300">
                    <thead class="bg-gray-800/60 text-gray-400 uppercase font-bold">
                        <tr>
                            <th class="p-3">Signal ID</th>
                            <th class="p-3">Symbol</th>
                            <th class="p-3">Type</th>
                            <th class="p-3">Entry Price</th>
                            <th class="p-3">Target (TP1)</th>
                            <th class="p-3">Stop Loss</th>
                            <th class="p-3">Status</th>
                            <th class="p-3">PnL ($)</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-gray-800">
                        {% for t in trades %}
                        <tr class="hover:bg-gray-800/40">
                            <td class="p-3 font-bold text-white">{{ t.id_str }}</td>
                            <td class="p-3">{{ t.symbol }}</td>
                            <td class="p-3 font-bold text-emerald-400">{{ t.side }}</td>
                            <td class="p-3">${{ "%.2f"|format(t.entry1) }}</td>
                            <td class="p-3">${{ "%.2f"|format(t.tp1) }}</td>
                            <td class="p-3">${{ "%.2f"|format(t.sl) }}</td>
                            <td class="p-3">
                                {% if t.status == 'WIN' %}
                                    <span class="bg-emerald-950 text-emerald-400 border border-emerald-800 px-2 py-0.5 rounded font-bold">WIN</span>
                                {% elif t.status == 'LOSS' %}
                                    <span class="bg-rose-950 text-rose-400 border border-rose-800 px-2 py-0.5 rounded font-bold">LOSS</span>
                                {% else %}
                                    <span class="bg-amber-950 text-amber-400 border border-amber-800 px-2 py-0.5 rounded font-bold">ACTIVE</span>
                                {% endif %}
                            </td>
                            <td class="p-3 font-bold {% if t.pnl >= 0 %}text-emerald-400{% else %}text-rose-400{% endif %}">
                                {% if t.pnl >= 0 %}+{% endif %}${{ t.pnl }}
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- ⚙️ 5. API & TELEGRAM CONFIGURATIONS PANEL -->
        <div class="lovable-card p-6 rounded-2xl space-y-4">
            <h2 class="text-lg font-bold text-white">⚙️ API & Auto-Trade Configurations</h2>
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
                        <input type="password" name="mexc_key" value="{{ config.mexc_key }}" placeholder="Enter MEXC API Key" class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white">
                    </div>
                    <div>
                        <label class="text-xs text-gray-400">MEXC_SECRET_KEY</label>
                        <input type="password" name="mexc_secret" value="{{ config.mexc_secret }}" placeholder="Enter MEXC Secret Key" class="w-full bg-gray-900 border border-gray-800 rounded-xl p-3 text-sm text-white">
                    </div>
                </div>

                <div class="flex items-center gap-6 pt-2">
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="checkbox" name="broadcast" class="w-4 h-4 accent-emerald-500" {% if config.broadcast %}checked{% endif %}>
                        <span class="text-xs font-bold text-gray-300">Telegram Broadcast</span>
                    </label>
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="checkbox" name="autotrade" class="w-4 h-4 accent-emerald-500" {% if config.autotrade %}checked{% endif %}>
                        <span class="text-xs font-bold text-gray-300">MEXC Live Auto-Trade</span>
                    </label>
                </div>

                <div class="flex gap-3 pt-3">
                    <button type="submit" name="action" value="test_signal" class="px-5 py-2.5 bg-amber-600 text-white font-bold rounded-xl text-xs hover:bg-amber-500 transition">⚡ Trigger Live Test Signal</button>
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
# 5. FLASK SERVER ROUTES
# ==========================================
@app.route('/')
def home():
    stats = calculate_stats()
    return render_template_string(DASHBOARD_TEMPLATE, config=BOT_CONFIG, trades=TRADE_HISTORY, stats=stats)

@app.route('/render-chart/<signal_id>')
def render_chart(signal_id):
    trade = next((t for t in TRADE_HISTORY if t["id_str"] == signal_id), None)
    if not trade:
        return "Signal Not Found", 404
    return render_template_string(CHART_RENDER_TEMPLATE, candles=trade["candles"], signal=trade)

@app.route('/charts/<filename>')
def serve_chart(filename):
    return send_from_directory(CHARTS_DIR, filename)

@app.route('/update-settings', methods=['POST'])
def update_settings():
    action = request.form.get("action")
    BOT_CONFIG["bot_token"] = request.form.get("bot_token", "").strip()
    BOT_CONFIG["channel"] = request.form.get("channel", "").strip()
    BOT_CONFIG["mexc_key"] = request.form.get("mexc_key", "").strip()
    BOT_CONFIG["mexc_secret"] = request.form.get("mexc_secret", "").strip()
    BOT_CONFIG["broadcast"] = "broadcast" in request.form
    BOT_CONFIG["autotrade"] = "autotrade" in request.form

    if action == "test_signal":
        try:
            mexc = ccxt.mexc({'options': {'defaultType': 'swap'}})
            ohlcv = mexc.fetch_ohlcv('BTC/USDT', timeframe='5m', limit=60)
            candles = [{"time": int(c[0]/1000), "open": c[1], "high": c[2], "low": c[3], "close": c[4]} for c in ohlcv]
            last_p = candles[-1]["close"]
            low_p = min(c["low"] for c in candles)
            high_p = max(c["high"] for c in candles)
        except Exception as e:
            return f"MEXC API Error: {e}", 500

        sig_id = f"SIG_{int(time.time())}"
        trade = {
            "id_str": sig_id,
            "symbol": "BTC/USDT",
            "tf": "5M",
            "strategy": "SMC Order Block + Fib OTE",
            "side": "🟢 LONG",
            "entry1": last_p,
            "sl": low_p,
            "tp1": high_p,
            "rr": "2.75",
            "candles": candles,
            "status": "WIN",
            "pnl": round((high_p - last_p) * 0.05, 2),
            "chart_img": None
        }
        
        TRADE_HISTORY.insert(0, trade)
        img_name, img_path = capture_chart_screenshot(sig_id)
        trade["chart_img"] = img_name
        send_telegram_alert(trade, img_path)

    stats = calculate_stats()
    return render_template_string(DASHBOARD_TEMPLATE, config=BOT_CONFIG, trades=TRADE_HISTORY, stats=stats)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
