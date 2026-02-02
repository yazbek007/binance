import os
import time
import threading
from datetime import datetime
import requests
from binance.client import Client
from binance import ThreadedWebsocketManager
import pandas as pd
import numpy as np
from flask import Flask

# ────────────────────────────────────────────────
#                 CONFIGURATION
# ────────────────────────────────────────────────

API_KEY    = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
NTFY_URL   = os.getenv("NTFY_TOPIC", "https://ntfy.sh/test-crypto-bot")

SYMBOL     = os.getenv("SYMBOL", "BTCUSDT")
INTERVAL   = os.getenv("INTERVAL", "4h")          # 4h = H4
CONFIRM_TF = "30m"                                 # M30

# Strategy params
EMA200_PERIOD = 200
EMA50_PERIOD  = 50
RSI_PERIOD    = 14
VOLUME_SMA    = 20
RSI_MIN       = 52
RSI_MAX       = 65

# Global state
in_position = False
last_h4_close_time = None
klines_h4 = []     # list of dicts
klines_m30 = []    # for volume confirmation

# ────────────────────────────────────────────────
#                  Notifications
# ────────────────────────────────────────────────

def send_ntfy(msg, title="Crypto Bot"):
    try:
        requests.post(
            NTFY_URL,
            data=msg.encode('utf-8'),
            headers={"Title": title, "Priority": "default"}
        )
    except Exception as e:
        print(f"ntfy failed: {e}")

# ────────────────────────────────────────────────
#               Technical Indicators
# ────────────────────────────────────────────────

def compute_indicators(df):
    df = df.copy()
    df['ema200'] = df['close'].ewm(span=EMA200_PERIOD, adjust=False).mean()
    df['ema50']  = df['close'].ewm(span=EMA50_PERIOD,  adjust=False).mean()

    # MACD
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd']   = ema12 - ema26
    df['signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['hist']   = df['macd'] - df['signal']

    # RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=RSI_PERIOD).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_PERIOD).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))

    # Volume SMA
    df['vol_sma20'] = df['volume'].rolling(window=VOLUME_SMA).mean()

    return df

# ────────────────────────────────────────────────
#              Check Entry Conditions
# ────────────────────────────────────────────────

def check_long_entry():
    global in_position

    if in_position:
        send_ntfy("Already in position → skipping check", "Status")
        return False

    if len(klines_h4) < 210:  # enough data for indicators
        return False

    df_h4 = pd.DataFrame(klines_h4)
    df_h4['close']  = df_h4['close'].astype(float)
    df_h4['volume'] = df_h4['volume'].astype(float)
    df_h4 = compute_indicators(df_h4)

    last = df_h4.iloc[-1]
    prev = df_h4.iloc[-2]

    # 1. Trend filter
    if last['close'] <= last['ema200']:
        return False

    # 2. Pullback + return near ema50 (simple heuristic)
    recent_low = df_h4['low'].iloc[-8:].min()
    if recent_low > last['ema50'] * 1.005:  # no real pullback
        return False
    if last['close'] < last['ema50'] * 0.992:  # too far below
        return False

    # 3. MACD bullish
    macd_cross_up = (prev['macd'] < prev['signal']) and (last['macd'] >= last['signal'])
    if not macd_cross_up:
        return False
    if last['macd'] < -0.0005 * last['close']:  # tolerance ~0.05%
        return False

    # 4. RSI filter
    if not (RSI_MIN <= last['rsi'] <= RSI_MAX):
        return False

    # 5. Volume confirmation on M30
    if len(klines_m30) < 25:
        return False

    df_m30 = pd.DataFrame(klines_m30)
    df_m30['volume'] = df_m30['volume'].astype(float)
    df_m30['vol_sma'] = df_m30['volume'].rolling(20).mean()

    recent_vol = df_m30['volume'].iloc[-1]
    vol_sma = df_m30['vol_sma'].iloc[-1]
    vol_max5 = df_m30['volume'].iloc[-5:].max()

    volume_ok = (recent_vol > vol_sma) or (recent_vol > vol_max5)

    if not volume_ok:
        return False

    # All conditions met → ENTRY
    entry_price = float(klines_m30[-1]['close'])
    msg = (
        f"ENTRY LONG {SYMBOL}\n"
        f"Price: {entry_price:.2f}\n"
        f"RSI: {last['rsi']:.1f}\n"
        f"MACD: {last['macd']:.4f}  Hist: {last['hist']:.4f}\n"
        f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    )
    send_ntfy(msg, "LONG ENTRY")

    in_position = True
    # هنا يمكنك إضافة أمر شراء حقيقي عبر client.futures_create_order(...)
    # حالياً مجرد إشعار – أضف التنفيذ إذا أردت التداول الحقيقي

    return True

# ────────────────────────────────────────────────
#             WebSocket Handlers
# ────────────────────────────────────────────────

def handle_kline_h4(msg):
    global last_h4_close_time, klines_h4

    k = msg['k']
    close_time = int(k['T'])

    if k['x']:  # candle closed
        if last_h4_close_time != close_time:
            last_h4_close_time = close_time
            klines_h4.append(k)
            if len(klines_h4) > 500:
                klines_h4.pop(0)

            send_ntfy(f"H4 candle closed • {k['c']}", "Data")
            check_long_entry()

def handle_kline_m30(msg):
    global klines_m30

    k = msg['k']
    if k['x']:  # closed candle → keep for volume check
        klines_m30.append(k)
        if len(klines_m30) > 100:
            klines_m30.pop(0)

# ────────────────────────────────────────────────
#                  Main Logic
# ────────────────────────────────────────────────

def start_websockets():
    twm = ThreadedWebsocketManager(
        api_key=API_KEY,
        api_secret=API_SECRET
    )
    twm.start()

    twm.start_kline_socket(
        callback=handle_kline_h4,
        symbol=SYMBOL,
        interval=INTERVAL
    )

    twm.start_kline_socket(
        callback=handle_kline_m30,
        symbol=SYMBOL,
        interval=CONFIRM_TF
    )

    send_ntfy(f"WebSockets started • {SYMBOL} {INTERVAL}+{CONFIRM_TF}", "Bot Started")

    twm.join()   # keeps running

# ────────────────────────────────────────────────
#                   Flask (لـ Render)
# ────────────────────────────────────────────────

app = Flask(__name__)

@app.route('/')
def home():
    return f"Bot is running • {SYMBOL} {INTERVAL} • {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"

@app.route('/health')
def health():
    return "OK", 200

# ────────────────────────────────────────────────

if __name__ == "__main__":
    send_ntfy("Bot script started (before threads)", "Init")

    # ابدأ WebSocket في thread منفصل
    ws_thread = threading.Thread(target=start_websockets, daemon=True)
    ws_thread.start()

    # شغّل Flask (Render يحتاجه)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
