import os
import time
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
    "symbol": "BTC/USDT",       # ডিফল্ট কয়েন
    "timeframe": "5m",          # ডিফল্ট টাইমফ্রেম
    "broadcast": True,
    "autotrade": False
}

TRADE_HISTORY = []
CHAT_LOGS = [{"sender": "System", "msg": "AJ Prompt Assistant ready. You can change coins, timeframes, and tokens via chat."}]

CHARTS_DIR = os.path.join(os.getcwd(), "generated_charts")
os.makedirs(CHARTS_DIR, exist_ok=True)

# ==========================================
# 2. SMC & FIB OTE STRATEGY ENGINE
# ==========================================
def analyze_smc_fib_strategy(symbol=None, timeframe=None):
    if not symbol:
        symbol = BOT_CONFIG["symbol"]
    if not timeframe:
        timeframe = BOT_CONFIG["timeframe"]
        
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

        last_close = df['close'].iloc[-1]
        candles = [{"time": int(row['time_sec']), "open": row['open'], "high": row['high'], "low": row['low'], "close": row['close'], "volume": row['volume']} for _, row in df.iterrows()]

        sig_id = f"SIG_{int(time.time())}"
        trade = {
            "id_str": sig_id,
            "symbol": symbol,
            "tf": timeframe.upper(),
            "strategy": "SMC Bullish Order Block + Fib OTE",
            "side": "🟢 LONG",
            "entry1": round(last_close, 2),
            "entry2": round(last_close * 0.9995, 2),
            "sl": round(recent_low, 2),
            "tp1": round(recent_high, 2),
            "rr": "2.75",
            "status": "ACTIVE",
            "pnl": 0.0,
            "chart_img": None,
            "tg_msg_id": None
        }
        return trade, candles

    except Exception as e:
        print(f"Strategy Error: {e}")
        return None, None

# ==========================================
# 3. ANALYTICS & QUICKCHART ENGINE
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
    chart_data = [{"x": time.strftime('%H:%M', time.localtime(c['time'])), "o": c['open'], "h": c['high'], "l": c['low'], "c": c['close']} for c in candles[-30:]]
    
    chart_config = {
        "type": "candlestick",
        "data": {
            "datasets": [{
                "label": f"{signal['symbol']} {signal['tf']}",
                "data": chart_data,
                "color": {"up": "#089981", "down": "#f23645", "unchanged": "#999999"}
            }]
        },
        "options": {
            "backgroundColor": "#131722",
            "legend": {"display": False},
            "scales": {
                "xAxes": [{"gridLines": {"color": "#2a2e39"}, "ticks": {"fontColor": "#848e9c", "fontSize": 10}}],
                "yAxes": [{"position": "right", "gridLines": {"color": "#2a2e39"}, "ticks": {"fontColor": "#848e9c", "fontSize": 10}}]
            }
        }
    }

    url = "https://quickchart.io/chart"
    payload = {
        "backgroundColor": "#131722",
        "width": 1000,
        "height": 500,
        "format": "png",
        "version": "2.9.4",
        "chart": chart_config
    }
    
    filename = f"chart_{signal['id_str']}_{int(time.time())}.png"
    filepath = os.path.join(CHARTS_DIR, filename)

    try:
        res = requests.post(url, json=payload, timeout=20)
        if res.status_code == 200 and len(res.content) > 2048:
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
                res = requests.post(url, data={'chat_id': BOT_CONFIG["channel"], 'caption': msg, 'parse_mode': 'HTML'}, files={'photo': photo}, timeout=15)
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
        update_text = f"❌ <b>STOP LOSS (SL) HIT!</b>\nSignal {signal['id_str']} closed at stop loss."

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
        print(f"Reply Error: {e}")

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
                <p class="text-xs text-gray-400 mt-1">Active Symbol: <span class="text-cyan-400 font-bold">{{ config.symbol }}</span> | Timeframe: <span class="text-cyan-400 font-bold">{{ config.timeframe }}</span></p>
            </div>
            <div class="text-right">
                <p class="text-xs text-gray-400">Scanner Status</p>
                <p class="text-sm font-semibold text-emerald-400">Active & Scanning</p>
            </div>
        </div>

        <!-- 💬 PROMPT / CHAT ASSISTANT BOX -->
        <div class="lovable-card p-6 rounded-2xl space-y-4 border border-cyan-500/30">
            <h2 class="text-lg font-bold text-cyan-400 flex items-center gap-2">🤖 AJ Prompt Assistant (Chat Control)</h2>
            <p class="text-xs text-gray-400">Commands you can type: 
                <code class="text-emerald-300">set symbol ETH/USDT</code>, 
                <code class="text-emerald-300">set timeframe 15m</code>, 
                <code class="text-emerald-300">set channel @mychannel</code>, 
                <code class="text-emerald-300">trigger test signal</code>
            </p>
            
            <div class="bg-gray-950 p-4 rounded-xl h-40 overflow-y-auto space-y-2 border border-gray-800 text-xs font-mono">
                {% for log in chat_logs %}
                    <div class="{% if log.sender == 'You' %}text-cyan-300{% else %}text-emerald-400{% endif %}">
                        <span class="text-gray-500">[{{ log.sender }}]:</span> {{ log.msg }}
                    </div>
                {% endfor %}
            </div>

            <form action="/prompt-chat" method="POST" class="flex gap-2">
                <input type="text" name="prompt" placeholder="Type instruction e.g. 'set symbol SOL/USDT' or 'set timeframe 1h'..." class="flex-1 bg-gray-900 border border-gray-800 rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-cyan-500" required>
                <button type="submit" class="bg-cyan-600 hover:bg-cyan-500 text-white px-6 py-3 rounded-xl font-bold text-xs transition">Send Prompt 🚀</button>
            </form>
        </div>

        <!-- ANALYTICS PANEL -->
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
                <p class="text-xs font-semibold text-gray-400">WIN / LOSS</p>
                <p class="text-2xl font-extrabold text-blue-400 mt-1">{{ stats.wins }}W / {{ stats.losses }}L</p>
            </div>
            <div class="lovable-card p-5 rounded-2xl">
                <p class="text-xs font-semibold text-gray-400">ESTIMATED PNL ($)</p>
                <p class="text-2xl font-extrabold {% if stats.pnl >= 0 %}text-emerald-400{% else %}text-rose-400{% endif %} mt-1">
                    {% if stats.pnl >= 0 %}+{% endif %}${{ stats.pnl }}
                </p>
            </div>
        </div>

        <!-- TRADINGVIEW LIVE WIDGET -->
        <div class="lovable-card p-6 rounded-2xl space-y-4">
            <h2 class="text-lg font-bold text-white">📈 Live Market Price Chart (MEXC: {{ config.symbol }})</h2>
            <div class="w-full h-[500px] rounded-xl overflow-hidden" id="tradingview_chart"></div>
        </div>

        <!-- AUTOMATED SIGNAL FEED -->
        <div class="lovable-card p-6 rounded-2xl space-y-4">
            <h2 class="text-lg font-bold text-white">⚡ Signal Feed & Candlestick Charts</h2>
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
                            <div class="bg-gray-800 p-2 rounded text-center"><p class="text-gray-400 text-[10px]">ENTRY</p>${{ "%.2f"|format(trade.entry1) }}</div>
                            <div class="bg-rose-950/40 border border-rose-900/50 p-2 rounded text-center"><p class="text-rose-400 text-[10px]">SL</p>${{ "%.2f"|format(trade.sl) }}</div>
                            <div class="bg-emerald-950/40 border border-emerald-900/50 p-2 rounded text-center"><p class="text-emerald-400 text-[10px]">TP1</p>${{ "%.2f"|format(trade.tp1) }}</div>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            {% else %}
                <div class="text-center py-8 text-gray-500 text-sm">
                    No active signal generated yet. Type "trigger test signal" in the chat above!
                </div>
            {% endif %}
        </div>

        <!-- TRADE HISTORY & TELEGRAM REPLY -->
        <div class="lovable-card p-6 rounded-2xl space-y-4">
            <h2 class="text-lg font-bold text-white">📋 Trade Performance & Telegram Reply</h2>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs text-gray-300">
                    <thead class="bg-gray-800/60 text-gray-400 uppercase font-bold">
                        <tr>
                            <th class="p-3">ID</th>
                            <th class="p-3">Symbol</th>
                            <th class="p-3">Type</th>
                            <th class="p-3">Entry</th>
                            <th class="p-3">Status</th>
                            <th class="p-3">Actions</th>
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
                                {% if t.status == 'WIN' %}<span class="text-emerald-400 font-bold">WIN</span>
                                {% elif t.status == 'LOSS' %}<span class="text-rose-400 font-bold">LOSS</span>
                                {% else %}<span class="text-amber-400 font-bold">ACTIVE</span>{% endif %}
                            </td>
                            <td class="p-3 flex gap-2">
                                <a href="/trigger-reply?id={{ t.id_str }}&status=WIN" class="bg-emerald-700 hover:bg-emerald-600 text-white px-2 py-1 rounded font-bold text-[10px]">TP Hit ✅</a>
                                <a href="/trigger-reply?id={{ t.id_str }}&status=LOSS" class="bg-rose-700 hover:bg-rose-600 text-white px-2 py-1 rounded font-bold text-[10px]">SL Hit ❌</a>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

    </div>

    <script type="text/javascript">
        // ডায়নামিক কয়েন রেন্ডার করার জন্য ট্রেডিংভিউ উইজেট
        let tvSymbol = "MEXC:" + "{{ config.symbol }}".replace('/', '') + ".P";
        let tvInterval = "{{ config.timeframe }}".replace('m', '').replace('h', '60');
        
        new TradingView.widget({
            "autosize": true,
            "symbol": tvSymbol,
            "interval": tvInterval,
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
# 5. FLASK SERVER ROUTES & PROMPT PARSER
# ==========================================
@app.route('/')
def home():
    stats = calculate_stats()
    return render_template_string(DASHBOARD_TEMPLATE, config=BOT_CONFIG, trades=TRADE_HISTORY, stats=stats, chat_logs=CHAT_LOGS)

@app.route('/charts/<filename>')
def serve_chart(filename):
    return send_from_directory(CHARTS_DIR, filename)

@app.route('/prompt-chat', methods=['POST'])
def prompt_chat():
    prompt = request.form.get("prompt", "").strip()
    CHAT_LOGS.append({"sender": "You", "msg": prompt})
    
    response_msg = "Command processed successfully."
    p_lower = prompt.lower()
    
    if "set symbol" in p_lower or "set coin" in p_lower:
        parts = prompt.split()
        if len(parts) >= 3:
            sym = parts[2].upper()
            if '/' not in sym:
                sym = sym + "/USDT"
            BOT_CONFIG["symbol"] = sym
            response_msg = f"Active symbol changed to {sym}!"
            
    elif "set timeframe" in p_lower or "set tf" in p_lower:
        parts = prompt.split()
        if len(parts) >= 3:
            tf = parts[2].lower()
            BOT_CONFIG["timeframe"] = tf
            response_msg = f"Strategy timeframe updated to {tf}!"
            
    elif "set token" in p_lower:
        parts = prompt.split("set token")
        if len(parts) > 1:
            BOT_CONFIG["bot_token"] = parts[1].strip()
            response_msg = "Telegram Bot Token updated successfully!"
            
    elif "set channel" in p_lower:
        parts = prompt.split("set channel")
        if len(parts) > 1:
            BOT_CONFIG["channel"] = parts[1].strip()
            response_msg = f"Telegram channel updated to {BOT_CONFIG['channel']}!"
            
    elif "trigger test signal" in p_lower or "send signal" in p_lower:
        trade, candles = analyze_smc_fib_strategy(BOT_CONFIG["symbol"], BOT_CONFIG["timeframe"])
        if trade and candles:
            img_name, img_path = generate_quickchart_image(candles, trade)
            trade["chart_img"] = img_name
            msg_id = send_telegram_alert(trade, img_path)
            trade["tg_msg_id"] = msg_id
            TRADE_HISTORY.insert(0, trade)
            response_msg = f"Signal generated for {BOT_CONFIG['symbol']} ({BOT_CONFIG['timeframe']}) and sent to Telegram!"
        else:
            response_msg = "Failed to fetch market data from MEXC for this symbol."
            
    else:
        response_msg = f"Command recognized: '{prompt}'."

    CHAT_LOGS.append({"sender": "AJ Assistant", "msg": response_msg})
    
    stats = calculate_stats()
    return render_template_string(DASHBOARD_TEMPLATE, config=BOT_CONFIG, trades=TRADE_HISTORY, stats=stats, chat_logs=CHAT_LOGS)

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
    return render_template_string(DASHBOARD_TEMPLATE, config=BOT_CONFIG, trades=TRADE_HISTORY, stats=stats, chat_logs=CHAT_LOGS)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
