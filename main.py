# app.py

import os
import threading
import asyncio
from datetime import datetime
import requests
from binance import AsyncClient, BinanceSocketManager
import pandas as pd
from flask import Flask
import numpy as np
from typing import Dict, List, Optional

# ────────────────────────────────────────────────
#                 CONFIGURATION
# ────────────────────────────────────────────────

API_KEY    = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
NTFY_URL   = os.getenv("NTFY_TOPIC", "https://ntfy.sh/your-secret-topic-name")

SYMBOL     = os.getenv("SYMBOL", "BTCUSDT").upper()
INTERVAL   = os.getenv("INTERVAL", "4h")
CONFIRM_TF = os.getenv("CONFIRM_TF", "30m")

# Strategy params
EMA200_PERIOD = 200
EMA50_PERIOD  = 50
EMA20_PERIOD  = 20
RSI_PERIOD    = 14
VOLUME_SMA    = 20

# Entry conditions
LONG_RSI_MIN  = 52
LONG_RSI_MAX  = 65
SHORT_RSI_MIN = 35
SHORT_RSI_MAX = 48

# Global state with thread safety
class TradingState:
    def __init__(self):
        self.in_position = False
        self.position_type = None  # 'LONG' or 'SHORT'
        self.entry_price = None
        self.klines_h4 = []
        self.klines_m30 = []
        self.lock = threading.Lock()
        self.last_signal_time = None
        self.signal_cooldown = 3600  # 1 hour cooldown between signals

state = TradingState()

# ────────────────────────────────────────────────
#                  Notifications
# ────────────────────────────────────────────────

def send_ntfy(msg: str, title: str = "Crypto Bot", priority: str = "default") -> None:
    """Send notification via NTFY"""
    try:
        requests.post(
            NTFY_URL,
            data=msg.encode('utf-8'),
            headers={
                "Title": title, 
                "Priority": priority,
                "Tags": "chart_with_upwards_trend" if "LONG" in title else "chart_with_downwards_trend"
            },
            timeout=5
        )
        print(f"Notification sent: {title}")
    except Exception as e:
        print(f"ntfy failed: {e}")

# ────────────────────────────────────────────────
#               Technical Indicators
# ────────────────────────────────────────────────

def compute_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """Calculate RSI correctly without division by zero"""
    delta = prices.diff()
    
    # Separate gains and losses
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    
    # Calculate average gains and losses
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    
    # Calculate RS and RSI
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    
    # Fill NaN values
    return rsi.fillna(50)

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all technical indicators"""
    df = df.copy()
    
    # EMAs
    df['ema200'] = df['close'].ewm(span=EMA200_PERIOD, adjust=False).mean()
    df['ema50'] = df['close'].ewm(span=EMA50_PERIOD, adjust=False).mean()
    df['ema20'] = df['close'].ewm(span=EMA20_PERIOD, adjust=False).mean()
    
    # MACD
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = ema12 - ema26
    df['signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['hist'] = df['macd'] - df['signal']
    
    # RSI (using corrected function)
    df['rsi'] = compute_rsi(df['close'], RSI_PERIOD)
    
    # Volume indicators
    df['vol_sma20'] = df['volume'].rolling(window=VOLUME_SMA).mean()
    df['volume_ratio'] = df['volume'] / df['vol_sma20']
    
    # Price action
    df['body'] = abs(df['close'] - df['open'])
    df['range'] = df['high'] - df['low']
    df['body_ratio'] = df['body'] / df['range']
    
    return df

# ────────────────────────────────────────────────
#              Check Entry Conditions
# ────────────────────────────────────────────────

def check_long_entry(df_h4: pd.DataFrame, df_m30: pd.DataFrame) -> bool:
    """Check conditions for LONG entry"""
    last = df_h4.iloc[-1]
    prev = df_h4.iloc[-2]
    
    # Price above EMA200 (uptrend)
    if last['close'] <= last['ema200']:
        return False
    
    # Price near EMA50 for potential bounce
    recent_low = df_h4['low'].iloc[-8:].min()
    ema50 = last['ema50']
    current_price = last['close']
    
    if not (ema50 * 0.99 <= recent_low <= ema50 * 1.01):
        return False
    
    # MACD bullish crossover
    macd_cross_up = (prev['macd'] < prev['signal']) and (last['macd'] > last['signal'])
    if not macd_cross_up:
        return False
    
    # RSI in optimal range for longs
    if not (LONG_RSI_MIN <= last['rsi'] <= LONG_RSI_MAX):
        return False
    
    # Volume confirmation on 30m
    if len(df_m30) < 25:
        return False
    
    recent_vol = df_m30['volume'].iloc[-1]
    vol_sma = df_m30['vol_sma'].iloc[-1]
    
    # Volume spike confirmation
    if recent_vol < vol_sma * 1.1:  # Need at least 10% above average
        return False
    
    return True

def check_short_entry(df_h4: pd.DataFrame, df_m30: pd.DataFrame) -> bool:
    """Check conditions for SHORT entry"""
    last = df_h4.iloc[-1]
    prev = df_h4.iloc[-2]
    
    # Price below EMA200 (downtrend)
    if last['close'] >= last['ema200']:
        return False
    
    # Price near EMA50 for potential rejection
    recent_high = df_h4['high'].iloc[-8:].max()
    ema50 = last['ema50']
    
    if not (ema50 * 0.99 <= recent_high <= ema50 * 1.01):
        return False
    
    # MACD bearish crossover
    macd_cross_down = (prev['macd'] > prev['signal']) and (last['macd'] < last['signal'])
    if not macd_cross_down:
        return False
    
    # RSI in optimal range for shorts
    if not (SHORT_RSI_MIN <= last['rsi'] <= SHORT_RSI_MAX):
        return False
    
    # Volume confirmation on 30m
    if len(df_m30) < 25:
        return False
    
    recent_vol = df_m30['volume'].iloc[-1]
    vol_sma = df_m30['vol_sma'].iloc[-1]
    
    # Volume spike confirmation
    if recent_vol < vol_sma * 1.1:
        return False
    
    return True

def analyze_market() -> None:
    """Analyze market conditions for both LONG and SHORT signals"""
    with state.lock:
        if state.in_position:
            return
        
        # Check if we have enough data
        if len(state.klines_h4) < 210 or len(state.klines_m30) < 25:
            return
        
        # Apply cooldown between signals
        current_time = datetime.utcnow()
        if (state.last_signal_time and 
            (current_time - state.last_signal_time).seconds < state.signal_cooldown):
            return
        
        # Prepare dataframes
        df_h4 = pd.DataFrame(state.klines_h4)
        df_m30 = pd.DataFrame(state.klines_m30)
        
        # Convert to numeric
        numeric_cols = ['open', 'high', 'low', 'close', 'volume']
        df_h4[numeric_cols] = df_h4[numeric_cols].apply(pd.to_numeric, errors='coerce')
        df_m30[numeric_cols] = df_m30[numeric_cols].apply(pd.to_numeric, errors='coerce')
        
        # Compute indicators
        df_h4 = compute_indicators(df_h4)
        df_m30['vol_sma'] = df_m30['volume'].rolling(20).mean()
        
        # Check for signals
        current_price = float(state.klines_m30[-1]['close'])
        last_h4 = df_h4.iloc[-1]
        
        if check_long_entry(df_h4, df_m30):
            send_signal("LONG", current_price, last_h4)
            state.last_signal_time = current_time
            
        elif check_short_entry(df_h4, df_m30):
            send_signal("SHORT", current_price, last_h4)
            state.last_signal_time = current_time

def send_signal(signal_type: str, price: float, last_candle: pd.Series) -> None:
    """Send trading signal notification"""
    timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    
    if signal_type == "LONG":
        title = "📈 LONG ENTRY SIGNAL"
        emoji = "🟢"
    else:
        title = "📉 SHORT ENTRY SIGNAL"
        emoji = "🔴"
    
    msg = (
        f"{emoji} {signal_type} SIGNAL {SYMBOL}\n"
        f"Price: {price:.2f}\n"
        f"RSI: {last_candle['rsi']:.1f}\n"
        f"EMA50: {last_candle['ema50']:.2f} | EMA200: {last_candle['ema200']:.2f}\n"
        f"MACD: {last_candle['macd']:.5f} | Signal: {last_candle['signal']:.5f}\n"
        f"Histogram: {last_candle['hist']:.5f}\n"
        f"Time: {timestamp}"
    )
    
    send_ntfy(msg, title, "high")
    
    # Log to console
    print(f"\n{'='*50}")
    print(f"{signal_type} Signal detected at {timestamp}")
    print(f"Price: {price:.2f}")
    print(f"RSI: {last_candle['rsi']:.1f}")
    print(f"{'='*50}\n")

# ────────────────────────────────────────────────
#             Async Handlers
# ────────────────────────────────────────────────

def handle_kline_h4(msg: Dict) -> None:
    """Handle 4-hour kline updates"""
    k = msg['k']
    
    if k['x']:  # Candle closed
        with state.lock:
            state.klines_h4.append({
                'open': float(k['o']),
                'high': float(k['h']),
                'low': float(k['l']),
                'close': float(k['c']),
                'volume': float(k['v']),
                'time': datetime.utcfromtimestamp(k['t'] / 1000)
            })
            
            # Keep only last 500 candles
            if len(state.klines_h4) > 500:
                state.klines_h4.pop(0)
        
        # Send update notification
        send_ntfy(
            f"H4 closed • Price: {float(k['c']):.2f} • Volume: {float(k['v']):.2f}",
            "📊 4H Candle Closed",
            "low"
        )
        
        # Analyze for signals
        analyze_market()

def handle_kline_m30(msg: Dict) -> None:
    """Handle 30-minute kline updates"""
    k = msg['k']
    
    if k['x']:  # Candle closed
        with state.lock:
            state.klines_m30.append({
                'open': float(k['o']),
                'high': float(k['h']),
                'low': float(k['l']),
                'close': float(k['c']),
                'volume': float(k['v']),
                'time': datetime.utcfromtimestamp(k['t'] / 1000)
            })
            
            # Keep only last 100 candles
            if len(state.klines_m30) > 100:
                state.klines_m30.pop(0)

# ────────────────────────────────────────────────
#              Async Main Loop
# ────────────────────────────────────────────────

async def run_websockets():
    """Main WebSocket connection manager with reconnection"""
    reconnect_delay = 5
    
    while True:
        try:
            client = await AsyncClient.create(API_KEY, API_SECRET)
            bm = BinanceSocketManager(client)
            
            streams = [
                f"{SYMBOL.lower()}@kline_{INTERVAL}",
                f"{SYMBOL.lower()}@kline_{CONFIRM_TF}"
            ]
            
            async with bm.multiplex_socket(streams) as multiplex_stream:
                send_ntfy(
                    f"✅ WebSockets connected • {SYMBOL} • {INTERVAL} + {CONFIRM_TF}",
                    "Connection Established",
                    "high"
                )
                
                while True:
                    try:
                        msg = await multiplex_stream.recv()
                        stream_name = msg['stream']
                        data = msg['data']
                        
                        if INTERVAL in stream_name:
                            handle_kline_h4(data)
                        elif CONFIRM_TF in stream_name:
                            handle_kline_m30(data)
                            
                    except Exception as e:
                        print(f"Error processing message: {e}")
                        await asyncio.sleep(1)
                        
        except Exception as e:
            send_ntfy(
                f"WebSocket disconnected: {str(e)} • Reconnecting in {reconnect_delay}s",
                "⚠️ Connection Lost",
                "high"
            )
            print(f"WebSocket error: {e}. Reconnecting in {reconnect_delay} seconds...")
            await asyncio.sleep(reconnect_delay)
            
            # Exponential backoff
            reconnect_delay = min(reconnect_delay * 2, 60)

# ────────────────────────────────────────────────
#                   Flask App
# ────────────────────────────────────────────────

app = Flask(__name__)

@app.route('/')
def home():
    """Main dashboard page"""
    with state.lock:
        status = "IN POSITION" if state.in_position else "AWAITING SIGNAL"
        position_type = state.position_type if state.position_type else "None"
        
        h4_count = len(state.klines_h4)
        m30_count = len(state.klines_m30)
        
        current_price = "N/A"
        if state.klines_m30:
            current_price = float(state.klines_m30[-1]['close'])
    
    return f"""
    <html>
        <head><title>Crypto Trading Bot</title></head>
        <body>
            <h1>📊 Crypto Trading Bot</h1>
            <p><strong>Symbol:</strong> {SYMBOL}</p>
            <p><strong>Status:</strong> {status} ({position_type})</p>
            <p><strong>Current Price:</strong> {current_price}</p>
            <p><strong>Data Available:</strong> {h4_count} H4 candles, {m30_count} 30m candles</p>
            <p><strong>Time:</strong> {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</p>
            <hr>
            <p><a href="/health">Health Check</a> • <a href="/stats">Statistics</a></p>
        </body>
    </html>
    """

@app.route('/health')
def health():
    """Health check endpoint"""
    with state.lock:
        data_ok = len(state.klines_h4) > 50 and len(state.klines_m30) > 50
    
    if data_ok:
        return "OK", 200
    else:
        return "Collecting data...", 202

@app.route('/stats')
def stats():
    """Statistics endpoint"""
    with state.lock:
        if not state.klines_h4:
            return "No data available yet", 200
        
        df = pd.DataFrame(state.klines_h4[-100:])
        df[['open', 'high', 'low', 'close', 'volume']] = \
            df[['open', 'high', 'low', 'close', 'volume']].apply(pd.to_numeric, errors='coerce')
        
        df = compute_indicators(df)
        last = df.iloc[-1]
    
    return f"""
    <html>
        <head><title>Market Statistics</title></head>
        <body>
            <h1>📈 Market Statistics</h1>
            <p><strong>Symbol:</strong> {SYMBOL}</p>
            <p><strong>Price:</strong> {last['close']:.2f}</p>
            <p><strong>RSI (14):</strong> {last['rsi']:.2f}</p>
            <p><strong>EMA20:</strong> {last['ema20']:.2f}</p>
            <p><strong>EMA50:</strong> {last['ema50']:.2f}</p>
            <p><strong>EMA200:</strong> {last['ema200']:.2f}</p>
            <p><strong>MACD:</strong> {last['macd']:.5f}</p>
            <p><strong>Signal:</strong> {last['signal']:.5f}</p>
            <p><strong>Volume Ratio:</strong> {last.get('volume_ratio', 'N/A'):.2f}</p>
            <hr>
            <p><a href="/">Back to Dashboard</a></p>
        </body>
    </html>
    """

# ────────────────────────────────────────────────
#                   Main Entry
# ────────────────────────────────────────────────

if __name__ == "__main__":
    # Startup notification
    startup_msg = f"""
🤖 Bot STARTED Successfully
Symbol: {SYMBOL}
Intervals: {INTERVAL} + {CONFIRM_TF}
Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
---
Signal Types: LONG & SHORT
LONG RSI Range: {LONG_RSI_MIN}-{LONG_RSI_MAX}
SHORT RSI Range: {SHORT_RSI_MIN}-{SHORT_RSI_MAX}
---
Ready to analyze market conditions!
    """
    
    send_ntfy(startup_msg, "🚀 Trading Bot Started", "high")
    
    # Start WebSocket thread
    def run_async():
        asyncio.run(run_websockets())
    
    ws_thread = threading.Thread(target=run_async, daemon=True)
    ws_thread.start()
    
    # Start Flask server
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅ Bot started successfully!")
    print(f"📊 Symbol: {SYMBOL}")
    print(f"⏰ Intervals: {INTERVAL} + {CONFIRM_TF}")
    print(f"🌐 Web server running on port {port}")
    print(f"⏳ Waiting for data and signals...\n")
    
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
