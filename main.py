# app.py

import os
import time
import threading
import asyncio
from datetime import datetime
import requests
from binance import AsyncClient, BinanceSocketManager
from binance.enums import BinanceInterval
import pandas as pd
from flask import Flask

# ────────────────────────────────────────────────
#                 CONFIGURATION
# ────────────────────────────────────────────────

API_KEY    = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
NTFY_URL   = os.getenv("NTFY_TOPIC", "https://ntfy.sh/your-secret-topic-name")

SYMBOL     = os.getenv("SYMBOL", "BTCUSDT")
INTERVAL   = BinanceInterval.KLINE_INTERVAL_4HOUR      # H4
CONFIRM_TF = BinanceInterval.KLINE_INTERVAL_30MINUTE   # M30

# Strategy params
EMA200_PERIOD = 200
EMA50_PERIOD  = 50
RSI_PERIOD    = 14
VOLUME_SMA    = 20
RSI_MIN       = 52
RSI_MAX       = 65

# Global state
in_position = False
klines_h4  = []   # list of dicts
klines_m30 = []   # for volume confirmation

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

    if len(klines_h4) < 210:
        return False

    df_h4 = pd.DataFrame(klines_h4)
    df_h4[['close','open','high','low','volume']] = df_h4[['close','open','high','low','volume']].astype(float)
    df_h4 = compute_indicators(df_h4)

    last = df_h4.iloc[-1]
    prev = df_h4.iloc[-2]

    # 1. Trend filter
    if last['close'] <= last['ema200']:
        return False

    # 2. Pullback + return near ema50 (heuristic)
    recent_low = df_h4['low'].iloc[-8:].min()
    if recent_low > last['ema50'] * 1.005:   # no real pullback
        return False
    if last['close'] < last['ema50'] * 0.992:  # too far below
        return False

    # 3. MACD bullish crossover
    macd_cross_up = (prev['macd'] < prev['signal']) and (last['macd'] >= last['signal'])
    if not macd_cross_up:
        return False
    if last['macd'] < -0.0005 * last['close']:  # small tolerance
        return False

    # 4. RSI filter
    if not (RSI_MIN <= last['rsi'] <= RSI_MAX):
        return False

    # 5. Volume confirmation (M30)
    if len(klines_m30) < 25:
        return False

    df_m30 = pd.DataFrame(klines_m30)
    df_m30['volume'] = df_m30['volume'].astype(float)
    df_m30['vol_sma'] = df_m30['volume'].rolling(20).mean()

    recent_vol = df_m30['volume'].iloc[-1]
    vol_sma    = df_m30['vol_sma'].iloc[-1]
    vol_max5   = df_m30['volume'].iloc[-5:].max()

    volume_ok = (recent_vol > vol_sma) or (recent_vol > vol_max5)

    if not volume_ok:
        return False

    # ENTRY SIGNAL
    entry_price = float(klines_m30[-1]['close'])
    msg = (
        f"ENTRY LONG {SYMBOL}\n"
        f"Price: {entry_price:.2f}\n"
        f"RSI(14): {last['rsi']:.1f}\n"
        f"MACD: {last['macd']:.5f}  Hist: {last['hist']:.5f}\n"
        f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    )
    send_ntfy(msg, "LONG ENTRY SIGNAL")

    in_position = True
    # ← هنا يمكنك إضافة client.futures_create_order للتنفيذ الفعلي

    return True

# ────────────────────────────────────────────────
#             Async WebSocket Handlers
# ────────────────────────────────────────────────

def handle_kline_h4(msg):
    k = msg['k']
    if k['x']:  # candle closed
        klines_h4.append(k)
        if len(klines_h4) > 500:
            klines_h4.pop(0)

        send_ntfy(f"H4 closed • Close: {float(k['c']):.2f}", "Data Update", priority="low")
        check_long_entry()

def handle_kline_m30(msg):
    k = msg['k']
    if k['x']:  # only keep closed candles for volume check
        klines_m30.append(k)
        if len(klines_m30) > 100:
            klines_m30.pop(0)

# ────────────────────────────────────────────────
#              Async Main Loop
# ────────────────────────────────────────────────

async def run_websockets():
    client = await AsyncClient.create(API_KEY, API_SECRET)
    bm = BinanceSocketManager(client)

    async with bm.multiplex_socket([
        f"{SYMBOL.lower()}@kline_{INTERVAL}",
        f"{SYMBOL.lower()}@kline_{CONFIRM_TF}"
    ]) as multiplex_stream:

        send_ntfy(f"WebSockets connected • {SYMBOL}  • {INTERVAL} + {CONFIRM_TF}", "Connection OK")

        while True:
            try:
                msg = await multiplex_stream.recv()
                stream_name = msg['stream']

                if '_4h' in stream_name or INTERVAL in stream_name:
                    handle_kline_h4(msg['data'])
                elif '_30m' in stream_name or CONFIRM_TF in stream_name:
                    handle_kline_m30(msg['data'])

            except Exception as e:
                send_ntfy(f"WebSocket error: {str(e)}", "ERROR", priority="high")
                await asyncio.sleep(8)

# ────────────────────────────────────────────────
#                   Flask (Render keep-alive)
# ────────────────────────────────────────────────

app = Flask(__name__)

@app.route('/')
def home():
    return f"Bot running • {SYMBOL} • {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"

@app.route('/health')
def health():
    return "OK", 200

# ────────────────────────────────────────────────

if __name__ == "__main__":
    # إشعار بدء التشغيل
    send_ntfy(
        f"Bot script STARTED\n"
        f"Symbol: {SYMBOL}\n"
        f"Python: {sys.version}\n"
        f"Render port: {os.environ.get('PORT', 'unknown')}",
        title="BOT STARTED",
        priority="high"
    )

    # تشغيل الـ websockets في thread منفصل
    def run_async():
        asyncio.run(run_websockets())

    ws_thread = threading.Thread(target=run_async, daemon=True)
    ws_thread.start()

    # تشغيل Flask
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
