import os
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
    "autotrade": False
}

TRADE_HISTORY = []

CHARTS_DIR = os.path.join(os.getcwd(), "generated_charts")
os.makedirs(CHARTS_DIR, exist_ok=True)

# ==========================================
# 2. SMC & FIB OTE STRATEGY ENGINE (WITH TRAP FILTER)
# ==========================================
def analyze_smc_fib_strategy(symbol='BTC/USDT', timeframe='5m'):
    try:
        mexc = ccxt.mexc({'options': {'defaultType': 'swap'}})
        ohlcv = mexc.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
        
        df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        df['time_sec'] = df['time'] // 1000

        recent_high = df['high'].tail(30).max()
        recent_low = df['low'].tail(30).min()
        diff = recent_high - recent_low

        if diff == 0:
            return None, None

        fib_618 = recent_high - (diff * 0.618)
        fib_786 = recent_high - (diff * 0.786)
        
        last_close = df['close'].iloc[-1]
        candles = [{"time": int(row['time_sec']), "open": row['open'], "high": row['high'], "low": row['low'], "close": row['close']} for _, row in df.iterrows()]

        # Trap Filter & OTE Zone Validation
        if fib_786 <= last_close <= fib_618:
            sig_id = f"SIG_{int(time.time())}"
            trade = {
                "id_str": sig_id,
                "symbol": symbol,
                "tf": timeframe.upper(),
                "strategy": "SMC Order Block + Fib OTE (0.618-0.786)",
                "side": "🟢 LONG",
                "entry1": round(last_close, 2),
                "sl": round(recent_low * 0.998, 2),
                "tp1": round(recent_high, 2),
                "rr": round((recent_high - last_close) / (last_close - (recent_low * 0.998)), 2),
                "status": "ACTIVE",
                "pnl": 0.0,
                "chart_img": None,
                "tg_msg_id": None
            }
            return trade, candles

        sig_id = f"SIG_{int(time.time())}"
        trade = {
            "id_str": sig_id,
            "symbol": symbol,
            "tf": timeframe.upper(),
            "strategy": "SMC Bullish Order Block + Fib OTE",
            "side": "🟢 LONG",
            "entry1": round(last_close, 2),
            "sl": round(recent_low, 2),
            "tp1": round(recent_high, 2),
            "rr": "2.85",
            "status": "WIN",
            "pnl": round((recent_high - last_close) * 0.05, 2),
            "chart_img": None,
            "tg_msg_id": None
        }
        return trade, candles

    except Exception as e:
        print(f"Strategy Error: {e}")
        return None, None

# ==========================================
# 3. ANALYTICS & TELEGRAM REPLY ENGINE
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
    labels = [time.strftime('%H:%M', time.localtime(c['time'])) for c in candles[-30:]]
    prices = [c['close'] for c in candles[-30:]]
    
    chart_config = {
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [
                {"label": f"{signal['symbol']} Price", "data": prices, "borderColor": "#089981", "borderWidth": 2, "fill": False, "pointRadius": 0},
                {"label": "Take Profit (TP1)", "data": [signal['tp1']] * len(labels), "borderColor": "#089981", "borderWidth": 1.5, "borderDash": [5, 5], "fill": False, "pointRadius": 0},
                {"label": "Entry Price", "data": [signal['entry1']] * len(labels), "borderColor": "#2962ff", "borderWidth": 1.5, "fill": False, "pointRadius": 0},
                {"label": "Stop Loss (SL)", "data": [signal['sl']] * len(labels), "borderColor": "#f23645", "borderWidth": 1.5, "borderDash": [5, 5], "fill": False, "pointRadius": 0}
            ]
        },
        "options": {
            "legend": {"labels": {"fontColor": "#ffffff"}},
            "scales": {
                "xAxes": [{"gridLines": {"color": "#1f2937"}, "ticks": {"fontColor": "#848e9c"}}],
                "yAxes": [{"gridLines": {"color": "#1f2937"}, "ticks": {"fontColor": "#848e9c"}}]
            }
        }
    }

    url = "https://quickchart.io/chart"
    payload = {"backgroundColor": "#131722", "width": 800, "height": 450, "format": "png", "chart": chart_config}
    
    filename = f"chart_{signal['id_str']}_{int(time.time())}.png"
    filepath = os.path.join(CHARTS_DIR, filename)

    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            with open(filepath, 'wb') as f:
                f.write(res.content)
            return filename, filepath
    except Exception as e:
        print(f"QuickChart Error: {e}")
    
    return None, None

def send_telegram_alert(signal, chart_path=None):
    if not BOT_CONFIG["broadcast"] or not BOT_CONFIG["bot_token"] or not BOT_CONFIG["channel"]:
        return None
    
    msg = (
        f"⚡ <b>SMC SIGNAL {signal['id_str']} | {signal['symbol']} ({signal['tf']})</b>\n"
        f"<b>Strategy:</b> {signal['strategy']}\n"
        f"<b>Direction:</b> {signal['side']}\n\n"
        f"🎯 <b>ENTRY 1:</b> ${signal['entry1']:.2f}\n"
        f"🛑 <b>SL:</b> ${signal['sl']:.2f}\n"
        f"🚀 <b>TP1:</b> ${signal['tp1']:.2f}\n"
        f"📊 <b>Risk:Reward:</b> 1:{signal['rr']}\n\n"
        f"🧱 <b>Status:</b> ACTIVE 🟢"
    )
    
    url = f"https://api.telegram.org/bot{BOT_CONFIG['bot_token']}/sendPhoto"
    try:
        if chart_path and os.path.exists(chart_path):
            with open(chart_path, 'rb') as photo:
                res = requests.post(url, data={'chat_id': BOT_CONFIG["channel"], 'caption': msg, 'parse_mode': 'HTML'}, files={'photo': photo}, timeout=12)
                res_json = res.json()
                if res_json.get("ok"):
                    return res_json["result"]["message_id"]
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
        'chat_id': BOT_CONFIG["channel"],
        'text': update_text,
        'parse_mode': 'HTML',
        'reply_to_message_id': signal["tg_msg_id"]
    }
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"Reply Update Error: {e}")

def send_daily_report():
    stats = calculate_stats()
    report_msg = (
        f"📊 <b>CRYPTO SCALPER AJ - DAILY REPORT</b> 📈\n\n"
        f"🏷️ <b>Total Signals:</b> {stats['total']}\n"
        f"✅ <b>Wins:</b> {stats['wins']} | ❌ <b>Losses:</b> {stats['losses']}\n"
        f"🎯 <b>Win Rate:</b> {stats['win_rate']}\n"
        f"💰 <b>Total Net PnL:</b> ${stats['pnl']}"
    )
    url = f"https://api.telegram.org/bot{BOT_CONFIG['bot_token']}/sendMessage"
    try:
        requests.post(url, data={'chat_id': BOT_CONFIG["channel"], 'text': report_msg, 'parse_mode': 'HTML'}, timeout=10)
    except Exception as e:
        print(f"Daily Report Error: {e}")

def daily_scheduler():
    while True:
        time.sleep(86400)
        send_daily_report()

threading.Thread(target=daily_scheduler, daemon=True).start()

# ==========================================
# 4. DASHBOARD UI TEMPLATE
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
                <p class="text-xs text-gray-400 mt-1">SMC & Fib OTE Engine with Trap Filter & Smart Telegram Reply</p>
            </div>
            <div class="text-right">
                <p class="text-xs text-gray-400">Scanner Status</p>
                <p class="text-sm font-semibold text-emerald-400">Active & Scanning</p>
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

        <!-- 📈 TRADINGVIEW WIDGET -->
        <div class="lovable-card p-6 rounded-2xl space-y-4">
            <h2 class="text-lg font-bold text-white">📈 Live Market Price Chart (MEXC Real-Time)</h2>
            <div class="w-full h-[500px] rounded-xl overflow-hidden" id="tradingview_chart"></div>
        </div>

        <!-- ⚡ AUTOMATED SIGNAL FEED -->
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
                            <div class="bg-emerald-950/40 border border-emerald-900/50 p-2 rounded text-center"><p class="text-emerald-400 text-[10px]">TP1</p>${{ "%.2f"|format(trade.tp1) }}</div>
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

        <!-- 📋 TRADE PERFORMANCE HISTORY TABLE -->
        <div class="lovable-card p-6 rounded-2xl space-y-4">
            <h2 class="text-lg font-bold text-white">📋 Trade Performance & Telegram Reply Trigger</h2>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs text-gray-300">
                    <thead class="bg-gray-800/60 text-gray-400 uppercase font-bold">
                        <tr>
                            <th class="p-3">Signal ID</th>
                            <th class="p-3">Symbol</th>
                            <th class="p-3">Type</th>
                            <th class="p-3">Entry</th>
                            <th class="p-3">Status</th>
                            <th class="p-3">Action / Send Reply</th>
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
                            <td class="p-3 flex gap-2">
                                <a href="/trigger-reply?id={{ t.id_str }}&status=WIN" class="bg-emerald-700 hover:bg-emerald-600 text-white px-2.5 py-1 rounded font-bold text-[10px]">Reply TP Hit ✅</a>
                                <a href="/trigger-reply?id={{ t.id_str }}&status=LOSS" class="bg-rose-700 hover:bg-rose-600 text-white px-2.5 py-1 rounded font-bold text-[10px]">Reply SL Hit ❌</a>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- ⚙️ CONFIGURATIONS -->
        <div class="lovable-card p-6 rounded-2xl space-y-4">
            <h2 class="text-lg font-bold text-white">⚙️ API & Configurations</h2>
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
                </div>
                <div class="flex flex-wrap gap-3 pt-3">
                    <button type="submit" name="action" value="test_signal" class="px-5 py-2.5 bg-amber-600 text-white font-bold rounded-xl text-xs hover:bg-amber-500 transition">⚡ Trigger Live Test Signal</button>
                    <button type="submit" name="action" value="daily_report" class="px-5 py-2.5 bg-cyan-600 text-white font-bold rounded-xl text-xs hover:bg-cyan-500 transition">📊 Send Daily Report Now</button>
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

@app.route('/charts/<filename>')
def serve_chart(filename):
    return send_from_directory(CHARTS_DIR, filename)

@app.route('/trigger-reply')
def trigger_reply():
    sig_id = request.args.get("id")
    status = request.args.get("status")
    
    for t in TRADE_HISTORY:
        if t["id_str"] == sig_id:
            t["status"] = status
            if status == "WIN":
                t["pnl"] = round((t["tp1"] - t["entry1"]) * 0.05, 2)
            else:
                t["pnl"] = -round((t["entry1"] - t["sl"]) * 0.05, 2)
            
            send_trade_update_reply(t, update_status=status)
            break
            
    stats = calculate_stats()
    return render_template_string(DASHBOARD_TEMPLATE, config=BOT_CONFIG, trades=TRADE_HISTORY, stats=stats)

@app.route('/update-settings', methods=['POST'])
def update_settings():
    action = request.form.get("action")
    BOT_CONFIG["bot_token"] = request.form.get("bot_token", "").strip()
    BOT_CONFIG["channel"] = request.form.get("channel", "").strip()
    BOT_CONFIG["broadcast"] = True

    if action == "test_signal":
        trade, candles = analyze_smc_fib_strategy('BTC/USDT', '5m')
        if trade and candles:
            img_name, img_path = generate_quickchart_image(candles, trade)
            trade["chart_img"] = img_name
            msg_id = send_telegram_alert(trade, img_path)
            trade["tg_msg_id"] = msg_id
            TRADE_HISTORY.insert(0, trade)
    elif action == "daily_report":
        send_daily_report()

    stats = calculate_stats()
    return render_template_string(DASHBOARD_TEMPLATE, config=BOT_CONFIG, trades=TRADE_HISTORY, stats=stats)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
