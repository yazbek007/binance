# app.py - 3-Level Signal Strength Trading Bot
# الإصدار النهائي المعدل بدون إيموجي في NTFY

import os
import threading
import asyncio
from datetime import datetime, timedelta
import requests
from binance import AsyncClient, BinanceSocketManager
import pandas as pd
from flask import Flask
import numpy as np
from typing import Dict, List, Tuple, Optional
import json
from dataclasses import dataclass
import re

# ────────────────────────────────────────────────
#                 CONFIGURATION
# ────────────────────────────────────────────────

API_KEY    = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
NTFY_URL   = os.getenv("NTFY_TOPIC", "https://ntfy.sh/your-secret-topic-name")

SYMBOL     = os.getenv("SYMBOL", "BTCUSDT").upper()
INTERVAL   = os.getenv("INTERVAL", "4h")
CONFIRM_TF = os.getenv("CONFIRM_TF", "30m")

# Signal Strength Thresholds - 3 مستويات
MIN_STRENGTH      = int(os.getenv("MIN_STRENGTH", "50"))
MEDIUM_THRESHOLD  = int(os.getenv("MEDIUM_THRESHOLD", "60"))
SIGNAL_THRESHOLD  = int(os.getenv("SIGNAL_THRESHOLD", "70"))
HIGH_STRENGTH     = int(os.getenv("HIGH_STRENGTH", "85"))

# Strategy params
EMA200_PERIOD = 200
EMA50_PERIOD  = 50
EMA20_PERIOD  = 20
RSI_PERIOD    = 14
VOLUME_SMA    = 20

# RSI Zones
RSI_OVERBOUGHT = 70
RSI_OVERSOLD   = 30
RSI_NEUTRAL    = 50

# Weights for signal strength calculation (sum = 100)
WEIGHTS = {
    'trend': 25,
    'momentum': 25,
    'volume': 15,
    'structure': 20,
    'multi_tf': 15
}

# Global state with thread safety
@dataclass
class SignalMetrics:
    strength: int = 0
    breakdown: Dict = None
    confidence: str = "LOW"
    reasons: List[str] = None

class TradingState:
    def __init__(self):
        self.klines_h4 = []
        self.klines_m30 = []
        self.signals_history = []
        self.lock = threading.Lock()
        self.last_signal_time = None
        self.signal_cooldown = 1800
        self.avg_signal_strength = 0
        self.success_rate = 0
        
state = TradingState()

# ────────────────────────────────────────────────
#                  Notifications
# ────────────────────────────────────────────────

def send_ntfy(msg: str, title: str = "Crypto Bot", priority: str = "default", 
              tags: str = None) -> None:
    """Send notification via NTFY - Safe version without emojis"""
    try:
        # دالة مساعدة لتنظيف النص
        def clean_text(text):
            if not isinstance(text, str):
                text = str(text)
            
            # قائمة بجميع الإيموجي الممكنة
            emoji_pattern = re.compile(
                "["
                "\U0001F600-\U0001F64F"  # emoticons
                "\U0001F300-\U0001F5FF"  # symbols & pictographs
                "\U0001F680-\U0001F6FF"  # transport & map symbols
                "\U0001F1E0-\U0001F1FF"  # flags
                "\U00002500-\U00002BEF"  # Chinese characters
                "\U00002702-\U000027B0"
                "\U000024C2-\U0001F251"
                "\U0001f926-\U0001f937"
                "\U00010000-\U0010ffff"
                "\u2640-\u2642"
                "\u2600-\u2B55"
                "\u200d"
                "\u23cf"
                "\u23e9"
                "\u231a"
                "\ufe0f"  # dingbats
                "\u3030"
                "]+",
                flags=re.UNICODE
            )
            
            # إزالة الإيموجي
            text = emoji_pattern.sub(r'', text)
            
            # إزالة المسافات البادئة والزائدة
            text = text.strip()
            
            # استبدال الأسطر الجديدة بمسافات في العنوان
            if text == title:
                text = text.replace('\n', ' | ')
            
            return text
        
        # تنظيف المدخلات
        safe_title = clean_text(title)
        safe_msg = clean_text(msg)
        
        # تأكد من أن القيم ليست فارغة
        if not safe_title or safe_title.isspace():
            safe_title = "Crypto Bot Notification"
        
        if not safe_msg or safe_msg.isspace():
            safe_msg = "Empty message"
        
        # إعداد الهيدرات
        headers = {
            "Title": safe_title[:250],
            "Priority": priority,
        }
        
        # تنظيف وتقصير الـ tags إذا وجدت
        if tags:
            safe_tags = clean_text(tags)
            if safe_tags:
                headers["Tags"] = safe_tags[:255]
        
        # إرسال الإشعار
        response = requests.post(
            NTFY_URL,
            data=safe_msg.encode('utf-8'),
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 200:
            print(f"✓ NTFY sent successfully: {safe_title}")
        else:
            print(f"✗ NTFY failed with status {response.status_code}")
            
    except Exception as e:
        print(f"✗ NTFY error: {str(e)[:100]}")

# ────────────────────────────────────────────────
#               Technical Indicators
# ────────────────────────────────────────────────

def compute_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """Calculate RSI correctly"""
    delta = prices.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    
    return rsi.fillna(50)

def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Average True Range for volatility"""
    high_low = df['high'] - df['low']
    high_close = abs(df['high'] - df['close'].shift())
    low_close = abs(df['low'] - df['close'].shift())
    
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = true_range.rolling(window=period).mean()
    return atr

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all technical indicators"""
    df = df.copy()
    
    # Price statistics
    df['returns'] = df['close'].pct_change()
    df['volatility'] = df['returns'].rolling(20).std()
    
    # EMAs
    df['ema200'] = df['close'].ewm(span=EMA200_PERIOD, adjust=False).mean()
    df['ema50'] = df['close'].ewm(span=EMA50_PERIOD, adjust=False).mean()
    df['ema20'] = df['close'].ewm(span=EMA20_PERIOD, adjust=False).mean()
    
    # EMA relationships
    df['ema20_above_50'] = (df['ema20'] > df['ema50']).astype(int)
    df['ema50_above_200'] = (df['ema50'] > df['ema200']).astype(int)
    
    # MACD
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = ema12 - ema26
    df['signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['hist'] = df['macd'] - df['signal']
    df['macd_trend'] = np.where(df['macd'] > df['signal'], 1, -1)
    
    # RSI
    df['rsi'] = compute_rsi(df['close'], RSI_PERIOD)
    
    # RSI Zones
    df['rsi_zone'] = pd.cut(df['rsi'], 
                           bins=[0, 30, 40, 60, 70, 100],
                           labels=['OVERSOLD', 'BEARISH', 'NEUTRAL', 'BULLISH', 'OVERBOUGHT'])
    
    # Volume indicators
    df['vol_sma20'] = df['volume'].rolling(window=VOLUME_SMA).mean()
    df['volume_ratio'] = df['volume'] / df['vol_sma20']
    df['volume_trend'] = (df['volume'] > df['vol_sma20']).astype(int)
    
    # Volatility
    df['atr'] = compute_atr(df)
    df['atr_percent'] = df['atr'] / df['close'] * 100
    
    # Price position relative to EMAs
    df['price_vs_ema20'] = (df['close'] - df['ema20']) / df['ema20'] * 100
    df['price_vs_ema50'] = (df['close'] - df['ema50']) / df['ema50'] * 100
    
    # Candle patterns
    df['body'] = abs(df['close'] - df['open'])
    df['range'] = df['high'] - df['low']
    df['body_ratio'] = df['body'] / df['range']
    df['is_bullish'] = (df['close'] > df['open']).astype(int)
    
    return df

# ────────────────────────────────────────────────
#              Signal Strength System
# ────────────────────────────────────────────────

def calculate_trend_strength(df: pd.DataFrame) -> Tuple[int, List[str]]:
    """Calculate trend strength score (0-25 points)"""
    score = 0
    reasons = []
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    
    # 1. EMA Alignment (max 10 points)
    ema_alignment = 0
    if last['ema20'] > last['ema50'] > last['ema200']:
        ema_alignment += 8
        reasons.append("EMA20 > EMA50 > EMA200 (Strong Uptrend)")
    elif last['ema20'] < last['ema50'] < last['ema200']:
        ema_alignment += 8
        reasons.append("EMA20 < EMA50 < EMA200 (Strong Downtrend)")
    
    ema_distance_20_50 = abs(last['ema20'] - last['ema50']) / last['ema50'] * 100
    if ema_distance_20_50 > 2:
        ema_alignment += 2
        reasons.append(f"EMA separation: {ema_distance_20_50:.1f}%")
    
    score += min(ema_alignment, 10)
    
    # 2. Price vs EMA position (max 10 points)
    price_position = 0
    if last['close'] > last['ema200']:
        distance = (last['close'] - last['ema200']) / last['ema200'] * 100
        price_position += min(int(distance * 2), 5)
    else:
        distance = (last['ema200'] - last['close']) / last['ema200'] * 100
        price_position += min(int(distance * 2), 5)
    
    if (last['close'] > last['ema50'] and last['ema50'] > last['ema200']) or \
       (last['close'] < last['ema50'] and last['ema50'] < last['ema200']):
        price_position += 5
        reasons.append("Price aligned with EMA50 trend")
    
    score += min(price_position, 10)
    
    # 3. Trend consistency (max 5 points)
    trend_consistency = 0
    recent_trend = df['ema20_above_50'].iloc[-5:].mean()
    if recent_trend > 0.8:
        trend_consistency += 3
        reasons.append("Consistent uptrend (80%+)")
    elif recent_trend < 0.2:
        trend_consistency += 3
        reasons.append("Consistent downtrend (80%+)")
    
    recent_returns = df['returns'].iloc[-3:].sum() * 100
    if (recent_returns > 1 and last['close'] > last['ema200']) or \
       (recent_returns < -1 and last['close'] < last['ema200']):
        trend_consistency += 2
        reasons.append(f"Recent momentum: {recent_returns:.1f}%")
    
    score += min(trend_consistency, 5)
    
    return min(score, WEIGHTS['trend']), reasons

def calculate_momentum_strength(df: pd.DataFrame, signal_type: str) -> Tuple[int, List[str]]:
    """Calculate momentum strength score (0-25 points)"""
    score = 0
    reasons = []
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    
    # 1. RSI Strength (max 10 points)
    rsi_score = 0
    if signal_type == "LONG":
        if 40 <= last['rsi'] <= 60:
            rsi_score += 8
            reasons.append(f"RSI in optimal zone: {last['rsi']:.1f}")
        elif last['rsi'] > 30 and last['rsi'] < 70:
            rsi_score += 5
            reasons.append(f"RSI in good zone: {last['rsi']:.1f}")
        
        if last['rsi'] > prev['rsi']:
            rsi_score += 2
    else:
        if 40 <= last['rsi'] <= 60:
            rsi_score += 8
            reasons.append(f"RSI in optimal zone: {last['rsi']:.1f}")
        elif last['rsi'] > 30 and last['rsi'] < 70:
            rsi_score += 5
        
        if last['rsi'] < prev['rsi']:
            rsi_score += 2
    
    score += min(rsi_score, 10)
    
    # 2. MACD Strength (max 10 points)
    macd_score = 0
    
    if signal_type == "LONG":
        if prev['macd'] < prev['signal'] and last['macd'] > last['signal']:
            macd_score += 8
            reasons.append("MACD bullish crossover")
        elif last['macd'] > last['signal']:
            macd_score += 5
            reasons.append("MACD above signal line")
    else:
        if prev['macd'] > prev['signal'] and last['macd'] < last['signal']:
            macd_score += 8
            reasons.append("MACD bearish crossover")
        elif last['macd'] < last['signal']:
            macd_score += 5
            reasons.append("MACD below signal line")
    
    if abs(last['hist']) > abs(prev['hist']):
        macd_score += 2
        reasons.append("MACD histogram strengthening")
    
    score += min(macd_score, 10)
    
    # 3. Price momentum (max 5 points)
    momentum_score = 0
    
    recent_candles = df.iloc[-3:]
    bullish_candles = (recent_candles['close'] > recent_candles['open']).sum()
    
    if signal_type == "LONG" and bullish_candles >= 2:
        momentum_score += 3
        reasons.append(f"{bullish_candles}/3 recent candles bullish")
    elif signal_type == "SHORT" and bullish_candles <= 1:
        momentum_score += 3
        reasons.append(f"{3-bullish_candles}/3 recent candles bearish")
    
    if signal_type == "LONG" and last['close'] > last['ema20'] > prev['ema20']:
        momentum_score += 2
    elif signal_type == "SHORT" and last['close'] < last['ema20'] < prev['ema20']:
        momentum_score += 2
    
    score += min(momentum_score, 5)
    
    return min(score, WEIGHTS['momentum']), reasons

def calculate_volume_strength(df_h4: pd.DataFrame, df_m30: pd.DataFrame) -> Tuple[int, List[str]]:
    """Calculate volume confirmation score (0-15 points)"""
    score = 0
    reasons = []
    
    if len(df_m30) < 10:
        return 0, ["Insufficient 30m data"]
    
    last_h4 = df_h4.iloc[-1]
    last_m30 = df_m30.iloc[-1]
    
    # 1. Volume vs average (max 8 points)
    volume_score = 0
    
    if last_h4['volume_ratio'] > 1.5:
        volume_score += 6
        reasons.append(f"H4 volume {last_h4['volume_ratio']:.1f}x average")
    elif last_h4['volume_ratio'] > 1.2:
        volume_score += 4
        reasons.append(f"H4 volume {last_h4['volume_ratio']:.1f}x average")
    elif last_h4['volume_ratio'] > 1.0:
        volume_score += 2
    
    if last_h4['volume_trend'] == 1:
        volume_score += 2
        reasons.append("Volume above 20-period average")
    
    score += min(volume_score, 8)
    
    # 2. 30m confirmation (max 7 points)
    confirm_score = 0
    
    if last_m30.get('volume_ratio', 0) > 1.8:
        confirm_score += 5
        reasons.append(f"30m volume spike: {last_m30.get('volume_ratio', 0):.1f}x")
    elif last_m30.get('volume_ratio', 0) > 1.3:
        confirm_score += 3
    
    recent_30m_vol = df_m30['volume'].iloc[-3:].mean()
    avg_30m_vol = df_m30['vol_sma20'].iloc[-1]
    if recent_30m_vol > avg_30m_vol * 1.2:
        confirm_score += 2
    
    score += min(confirm_score, 7)
    
    return min(score, WEIGHTS['volume']), reasons

def calculate_structure_strength(df: pd.DataFrame, signal_type: str) -> Tuple[int, List[str]]:
    """Calculate price structure score (0-20 points)"""
    score = 0
    reasons = []
    
    last = df.iloc[-1]
    
    # 1. Support/Resistance levels (max 10 points)
    structure_score = 0
    
    price_vs_ema50 = abs(last['price_vs_ema50'])
    if price_vs_ema50 < 1:
        structure_score += 8
        reasons.append(f"Price near EMA50 (±{price_vs_ema50:.1f}%)")
    elif price_vs_ema50 < 2:
        structure_score += 5
    elif price_vs_ema50 < 3:
        structure_score += 2
    
    recent_range = df['range'].iloc[-5:].mean()
    avg_range = df['range'].rolling(20).mean().iloc[-1]
    if recent_range < avg_range * 0.7:
        structure_score += 2
        reasons.append("Low volatility consolidation")
    
    score += min(structure_score, 10)
    
    # 2. Candle patterns (max 10 points)
    pattern_score = 0
    
    if last['body_ratio'] > 0.7:
        pattern_score += 6
        if last['is_bullish'] == 1:
            reasons.append("Strong bullish candle")
        else:
            reasons.append("Strong bearish candle")
    elif last['body_ratio'] > 0.5:
        pattern_score += 3
    
    recent_direction = df['is_bullish'].iloc[-3:].mean()
    if signal_type == "LONG" and recent_direction > 0.66:
        pattern_score += 4
        reasons.append("Consecutive bullish candles")
    elif signal_type == "SHORT" and recent_direction < 0.33:
        pattern_score += 4
        reasons.append("Consecutive bearish candles")
    
    score += min(pattern_score, 10)
    
    return min(score, WEIGHTS['structure']), reasons

def calculate_multi_tf_strength(df_h4: pd.DataFrame, df_m30: pd.DataFrame, 
                               signal_type: str) -> Tuple[int, List[str]]:
    """Calculate multi-timeframe confirmation score (0-15 points)"""
    score = 0
    reasons = []
    
    if len(df_m30) < 20:
        return 0, ["Insufficient multi-TF data"]
    
    last_h4 = df_h4.iloc[-1]
    last_m30 = df_m30.iloc[-1]
    
    # 1. 30m trend alignment (max 10 points)
    alignment_score = 0
    
    if signal_type == "LONG":
        if last_m30['close'] > last_m30['ema50'] and last_m30['ema50'] > last_m30['ema200']:
            alignment_score += 8
            reasons.append("30m confirms uptrend")
        elif last_m30['close'] > last_m30['ema50']:
            alignment_score += 5
    else:
        if last_m30['close'] < last_m30['ema50'] and last_m30['ema50'] < last_m30['ema200']:
            alignment_score += 8
            reasons.append("30m confirms downtrend")
        elif last_m30['close'] < last_m30['ema50']:
            alignment_score += 5
    
    if signal_type == "LONG" and last_m30['macd'] > last_m30['signal']:
        alignment_score += 2
    elif signal_type == "SHORT" and last_m30['macd'] < last_m30['signal']:
        alignment_score += 2
    
    score += min(alignment_score, 10)
    
    # 2. Divergence check (max 5 points)
    divergence_score = 0
    
    h4_rsi_trend = last_h4['rsi'] > df_h4['rsi'].iloc[-2]
    m30_rsi_trend = last_m30['rsi'] > df_m30['rsi'].iloc[-2]
    
    if signal_type == "LONG" and h4_rsi_trend and m30_rsi_trend:
        divergence_score += 5
        reasons.append("No RSI divergence (positive)")
    elif signal_type == "SHORT" and not h4_rsi_trend and not m30_rsi_trend:
        divergence_score += 5
        reasons.append("No RSI divergence (negative)")
    
    score += min(divergence_score, 5)
    
    return min(score, WEIGHTS['multi_tf']), reasons

def calculate_signal_strength(df_h4: pd.DataFrame, df_m30: pd.DataFrame, 
                            signal_type: str) -> SignalMetrics:
    """Calculate comprehensive signal strength score"""
    metrics = SignalMetrics(
        strength=0,
        breakdown={},
        confidence="LOW",
        reasons=[]
    )
    
    trend_score, trend_reasons = calculate_trend_strength(df_h4)
    momentum_score, momentum_reasons = calculate_momentum_strength(df_h4, signal_type)
    volume_score, volume_reasons = calculate_volume_strength(df_h4, df_m30)
    structure_score, structure_reasons = calculate_structure_strength(df_h4, signal_type)
    multi_tf_score, multi_tf_reasons = calculate_multi_tf_strength(df_h4, df_m30, signal_type)
    
    total_score = (trend_score + momentum_score + volume_score + 
                   structure_score + multi_tf_score)
    
    atr_percent = df_h4['atr_percent'].iloc[-1]
    if atr_percent > 3:
        total_score = int(total_score * 0.8)
        metrics.reasons.append(f"High volatility penalty: ATR {atr_percent:.1f}%")
    
    metrics.strength = min(total_score, 100)
    
    if metrics.strength >= HIGH_STRENGTH:
        metrics.confidence = "VERY HIGH"
    elif metrics.strength >= SIGNAL_THRESHOLD:
        metrics.confidence = "HIGH"
    elif metrics.strength >= MEDIUM_THRESHOLD:
        metrics.confidence = "MEDIUM"
    elif metrics.strength >= MIN_STRENGTH:
        metrics.confidence = "LOW"
    else:
        metrics.confidence = "VERY LOW"
    
    metrics.breakdown = {
        'trend': trend_score,
        'momentum': momentum_score,
        'volume': volume_score,
        'structure': structure_score,
        'multi_tf': multi_tf_score,
        'total': metrics.strength
    }
    
    all_reasons = []
    all_reasons.extend(trend_reasons)
    all_reasons.extend(momentum_reasons)
    all_reasons.extend(volume_reasons)
    all_reasons.extend(structure_reasons)
    all_reasons.extend(multi_tf_reasons)
    
    metrics.reasons = all_reasons[:5]
    
    return metrics

# ────────────────────────────────────────────────
#              Signal Notifications
# ────────────────────────────────────────────────

def send_strong_signal(signal_type: str, price: float, 
                      last_candle: pd.Series, metrics: SignalMetrics) -> None:
    """Send STRONG trading signal (70+ strength) - without emojis"""
    timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    
    if signal_type == "LONG":
        title = f"LONG SIGNAL [{metrics.confidence}]"
        signal_char = "[LONG]"
    else:
        title = f"SHORT SIGNAL [{metrics.confidence}]"
        signal_char = "[SHORT]"
    
    msg = (
        f"{signal_char} SIGNAL {SYMBOL}\n"
        f"Strength: {metrics.strength}/100 ({metrics.confidence})\n"
        f"Price: {price:.2f}\n"
        f"RSI: {last_candle['rsi']:.1f} | MACD: {last_candle['macd']:.5f}\n"
        f"EMA50: {last_candle['ema50']:.2f} | EMA200: {last_candle['ema200']:.2f}\n"
        f"\nStrength Breakdown:\n"
        f"Trend: {metrics.breakdown['trend']}/{WEIGHTS['trend']}\n"
        f"Momentum: {metrics.breakdown['momentum']}/{WEIGHTS['momentum']}\n"
        f"Volume: {metrics.breakdown['volume']}/{WEIGHTS['volume']}\n"
        f"Structure: {metrics.breakdown['structure']}/{WEIGHTS['structure']}\n"
        f"Multi-TF: {metrics.breakdown['multi_tf']}/{WEIGHTS['multi_tf']}\n"
        f"\nTime: {timestamp}"
    )
    
    priority = "high" if metrics.strength >= HIGH_STRENGTH else "default"
    
    send_ntfy(msg, title, priority)
    
    print(f"\n{'='*60}")
    print(f"{signal_type} STRONG Signal | Strength: {metrics.strength}/100")
    print(f"Price: {price:.2f} | RSI: {last_candle['rsi']:.1f}")
    print(f"{'='*60}\n")

def send_medium_signal(signal_type: str, price: float, 
                      last_candle: pd.Series, metrics: SignalMetrics) -> None:
    """Send MEDIUM strength signal (60-69) for monitoring - without emojis"""
    timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    
    if signal_type == "LONG":
        title = f"LONG WATCH [{metrics.confidence}]"
        signal_char = "[LONG WATCH]"
    else:
        title = f"SHORT WATCH [{metrics.confidence}]"
        signal_char = "[SHORT WATCH]"
    
    msg = (
        f"{signal_char} {SYMBOL}\n"
        f"Strength: {metrics.strength}/100 (MEDIUM)\n"
        f"Price: {price:.2f}\n"
        f"RSI: {last_candle['rsi']:.1f} | MACD: {last_candle['macd']:.5f}\n"
        f"\nThis is a WATCH signal (not for immediate entry)\n"
        f"Monitor for confirmation above {SIGNAL_THRESHOLD}/100\n"
        f"\nTime: {timestamp}"
    )
    
    send_ntfy(msg, title, "low")
    
    print(f"\n{'='*60}")
    print(f"{signal_type} MEDIUM Signal | Strength: {metrics.strength}/100")
    print(f"Price: {price:.2f} | RSI: {last_candle['rsi']:.1f}")
    print(f"{'='*60}\n")

# ────────────────────────────────────────────────
#              Signal Analysis
# ────────────────────────────────────────────────

def analyze_market_signals() -> None:
    """Analyze market for both LONG and SHORT signals with strength scoring"""
    with state.lock:
        if len(state.klines_h4) < 210 or len(state.klines_m30) < 50:
            return
        
        current_time = datetime.utcnow()
        if (state.last_signal_time and 
            (current_time - state.last_signal_time).seconds < state.signal_cooldown):
            return
        
        df_h4 = pd.DataFrame(state.klines_h4[-300:])
        df_m30 = pd.DataFrame(state.klines_m30[-150:])
        
        numeric_cols = ['open', 'high', 'low', 'close', 'volume']
        df_h4[numeric_cols] = df_h4[numeric_cols].apply(pd.to_numeric, errors='coerce')
        df_m30[numeric_cols] = df_m30[numeric_cols].apply(pd.to_numeric, errors='coerce')
        
        df_h4 = compute_indicators(df_h4)
        df_m30 = compute_indicators(df_m30)
        
        current_price = float(state.klines_m30[-1]['close'])
        
        signals = []
        
        long_metrics = calculate_signal_strength(df_h4, df_m30, "LONG")
        if long_metrics.strength >= MIN_STRENGTH:
            signals.append(("LONG", long_metrics, current_price, df_h4.iloc[-1]))
        
        short_metrics = calculate_signal_strength(df_h4, df_m30, "SHORT")
        if short_metrics.strength >= MIN_STRENGTH:
            signals.append(("SHORT", short_metrics, current_price, df_h4.iloc[-1]))
        
        signals.sort(key=lambda x: x[1].strength, reverse=True)
        
        for signal_type, metrics, price, last_candle in signals:
            if MEDIUM_THRESHOLD <= metrics.strength < SIGNAL_THRESHOLD:
                send_medium_signal(signal_type, price, last_candle, metrics)
                state.last_signal_time = current_time
                
                state.signals_history.append({
                    'time': current_time,
                    'type': signal_type + "_WATCH",
                    'strength': metrics.strength,
                    'price': price,
                    'confidence': metrics.confidence
                })
                
                break
            
            elif metrics.strength >= SIGNAL_THRESHOLD:
                send_strong_signal(signal_type, price, last_candle, metrics)
                state.last_signal_time = current_time
                
                state.signals_history.append({
                    'time': current_time,
                    'type': signal_type,
                    'strength': metrics.strength,
                    'price': price,
                    'confidence': metrics.confidence
                })
                
                break

# ────────────────────────────────────────────────
#             Async Handlers
# ────────────────────────────────────────────────

def handle_kline(msg: Dict, timeframe: str) -> None:
    """Handle kline updates for any timeframe"""
    k = msg['k']
    
    if k['x']:
        candle_data = {
            'open': float(k['o']),
            'high': float(k['h']),
            'low': float(k['l']),
            'close': float(k['c']),
            'volume': float(k['v']),
            'time': datetime.utcfromtimestamp(k['t'] / 1000)
        }
        
        with state.lock:
            if timeframe == INTERVAL:
                state.klines_h4.append(candle_data)
                if len(state.klines_h4) > 500:
                    state.klines_h4.pop(0)
            elif timeframe == CONFIRM_TF:
                state.klines_m30.append(candle_data)
                if len(state.klines_m30) > 200:
                    state.klines_m30.pop(0)
        
        if timeframe == INTERVAL:
            analyze_market_signals()

# ────────────────────────────────────────────────
#              Async Main Loop
# ────────────────────────────────────────────────

async def run_websockets():
    """Main WebSocket connection manager"""
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
                connection_msg = (
                    f"WebSockets Connected - 3-Level Signals\n"
                    f"Symbol: {SYMBOL}\n"
                    f"Timeframes: {INTERVAL} + {CONFIRM_TF}\n"
                    f"Signal Levels:\n"
                    f"- MEDIUM (Watch): {MEDIUM_THRESHOLD}-{SIGNAL_THRESHOLD-1}/100\n"
                    f"- STRONG (Entry): {SIGNAL_THRESHOLD}-{HIGH_STRENGTH-1}/100\n"
                    f"- VERY STRONG: {HIGH_STRENGTH}+/100\n"
                    f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
                )
                send_ntfy(connection_msg, "Bot Connected - 3 Levels", "high")
                
                print(f"\nConnected to Binance WebSocket")
                print(f"Monitoring: {SYMBOL}")
                print(f"Timeframes: {INTERVAL}, {CONFIRM_TF}")
                print(f"Signal Levels:")
                print(f"  - MEDIUM (Watch): {MEDIUM_THRESHOLD}-{SIGNAL_THRESHOLD-1}/100")
                print(f"  - STRONG (Entry): {SIGNAL_THRESHOLD}-{HIGH_STRENGTH-1}/100")
                print(f"  - VERY STRONG: {HIGH_STRENGTH}+/100")
                print(f"{'='*60}\n")
                
                while True:
                    try:
                        msg = await multiplex_stream.recv()
                        stream_name = msg['stream']
                        data = msg['data']
                        
                        if INTERVAL in stream_name:
                            handle_kline(data, INTERVAL)
                        elif CONFIRM_TF in stream_name:
                            handle_kline(data, CONFIRM_TF)
                            
                    except Exception as e:
                        print(f"Error processing message: {e}")
                        await asyncio.sleep(1)
                        
        except Exception as e:
            error_msg = f"WebSocket Error: {str(e)[:100]}"
            send_ntfy(error_msg, "Connection Lost", "high")
            print(f"WebSocket error: {e}")
            print(f"Reconnecting in {reconnect_delay} seconds...")
            
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 1.5, 60)

# ────────────────────────────────────────────────
#                   Flask App
# ────────────────────────────────────────────────

app = Flask(__name__)

@app.route('/')
def dashboard():
    """Main dashboard with enhanced market information"""
    with state.lock:
        h4_count = len(state.klines_h4)
        m30_count = len(state.klines_m30)
        
        # تهيئة القاموس بقيم افتراضية
        market_data = {
            'current_price': "N/A",
            'price_change_24h': "N/A",
            'price_change_color': "black",
            'volume_24h': "N/A",
            'high_24h': "N/A",
            'low_24h': "N/A",
            'current_rsi': "N/A",
            'rsi_trend': "N/A",
            'rsi_zone': "N/A",
            'macd_value': "N/A",
            'macd_signal': "N/A",
            'macd_status': "N/A",
            'macd_histogram': "N/A",
            'trend_status': "N/A",
            'trend_color': "gray",
            'atr_percent': "N/A",
            'volatility': "N/A",
            'resistance_level': "N/A",
            'support_level': "N/A",
            'market_sentiment': "N/A",
            'long_strength': 0,
            'long_confidence': "N/A",
            'short_strength': 0,
            'short_confidence': "N/A",
            'ema_alignment': "N/A",
            'volume_status': "N/A",
            'ema_distance_20_50': "N/A",
            'ema_distance_50_200': "N/A"
        }
        
        try:
            if state.klines_h4 and state.klines_m30:
                # تحضير البيانات
                df_h4 = pd.DataFrame(state.klines_h4[-100:])
                df_m30 = pd.DataFrame(state.klines_m30[-50:])
                
                numeric_cols = ['open', 'high', 'low', 'close', 'volume']
                df_h4[numeric_cols] = df_h4[numeric_cols].apply(pd.to_numeric, errors='coerce')
                df_m30[numeric_cols] = df_m30[numeric_cols].apply(pd.to_numeric, errors='coerce')
                
                df_h4 = compute_indicators(df_h4)
                df_m30 = compute_indicators(df_m30)
                
                last_h4 = df_h4.iloc[-1]
                prev_h4 = df_h4.iloc[-2]
                last_m30 = df_m30.iloc[-1]
                
                # معلومات الأسعار والتغيرات
                current_price = float(state.klines_m30[-1]['close'])
                market_data['current_price'] = f"{current_price:,.2f}"
                
                # حساب التغير في آخر 24 ساعة (أو آخر 6 شموع 4H)
                if len(state.klines_h4) >= 6:
                    price_24h_ago = float(state.klines_h4[-6]['close'])
                    price_change = ((current_price - price_24h_ago) / price_24h_ago) * 100
                    market_data['price_change_24h'] = f"{price_change:+.2f}%"
                    market_data['price_change_color'] = "green" if price_change >= 0 else "red"
                
                # أعلى وأقل سعر في آخر 24 ساعة
                if len(state.klines_h4) >= 6:
                    recent_prices = [float(k['high']) for k in state.klines_h4[-6:]]
                    market_data['high_24h'] = f"{max(recent_prices):,.2f}"
                    recent_lows = [float(k['low']) for k in state.klines_h4[-6:]]
                    market_data['low_24h'] = f"{min(recent_lows):,.2f}"
                
                # حجم التداول
                if len(state.klines_h4) >= 6:
                    recent_volume = sum(float(k['volume']) for k in state.klines_h4[-6:])
                    market_data['volume_24h'] = f"{recent_volume:,.0f}"
                
                # مؤشرات فنية
                market_data['current_rsi'] = f"{last_h4.get('rsi', 0):.1f}"
                market_data['rsi_trend'] = "صاعد" if last_h4.get('rsi', 0) > prev_h4.get('rsi', 0) else "هابط"
                
                # تحديد منطقة RSI
                rsi_val = last_h4.get('rsi', 50)
                if rsi_val >= 70:
                    market_data['rsi_zone'] = "OVERBOUGHT"
                elif rsi_val >= 60:
                    market_data['rsi_zone'] = "BULLISH"
                elif rsi_val >= 40:
                    market_data['rsi_zone'] = "NEUTRAL"
                elif rsi_val >= 30:
                    market_data['rsi_zone'] = "BEARISH"
                else:
                    market_data['rsi_zone'] = "OVERSOLD"
                
                # MACD
                market_data['macd_value'] = f"{last_h4.get('macd', 0):.5f}"
                market_data['macd_signal'] = f"{last_h4.get('signal', 0):.5f}"
                market_data['macd_status'] = "إيجابي" if last_h4.get('macd', 0) > last_h4.get('signal', 0) else "سلبي"
                market_data['macd_histogram'] = f"{last_h4.get('hist', 0):.5f}"
                
                # الاتجاه العام
                ema20 = last_h4.get('ema20', 0)
                ema50 = last_h4.get('ema50', 0)
                ema200 = last_h4.get('ema200', 0)
                
                if ema20 > ema50 > ema200:
                    market_data['trend_status'] = "صاعد قوي"
                    market_data['trend_color'] = "green"
                elif ema20 < ema50 < ema200:
                    market_data['trend_status'] = "هابط قوي"
                    market_data['trend_color'] = "red"
                else:
                    market_data['trend_status'] = "جانبي"
                    market_data['trend_color'] = "gray"
                
                # تقارب/تباعد المتوسطات
                if ema50 > 0:
                    ema_distance_20_50 = abs(ema20 - ema50) / ema50 * 100
                    market_data['ema_distance_20_50'] = f"{ema_distance_20_50:.1f}%"
                
                if ema200 > 0:
                    ema_distance_50_200 = abs(ema50 - ema200) / ema200 * 100
                    market_data['ema_distance_50_200'] = f"{ema_distance_50_200:.1f}%"
                
                market_data['ema_alignment'] = f"20-50: {market_data['ema_distance_20_50']} | 50-200: {market_data['ema_distance_50_200']}"
                
                # التقلب
                atr_percent = last_h4.get('atr_percent', 0)
                market_data['atr_percent'] = f"{atr_percent:.2f}%"
                market_data['volatility'] = "مرتفع" if atr_percent > 2 else "منخفض"
                
                # مستويات الدعم والمقاومة (مبسطة)
                if len(df_h4) >= 10:
                    recent_highs = df_h4['high'].iloc[-10:].tolist()
                    recent_lows = df_h4['low'].iloc[-10:].tolist()
                    if recent_highs:
                        market_data['resistance_level'] = f"{max(recent_highs):,.2f}"
                    if recent_lows:
                        market_data['support_level'] = f"{min(recent_lows):,.2f}"
                
                # قوة الإشارات
                try:
                    long_metrics = calculate_signal_strength(df_h4, df_m30, "LONG")
                    short_metrics = calculate_signal_strength(df_h4, df_m30, "SHORT")
                    
                    market_data['long_strength'] = long_metrics.strength
                    market_data['long_confidence'] = long_metrics.confidence
                    market_data['short_strength'] = short_metrics.strength
                    market_data['short_confidence'] = short_metrics.confidence
                    
                    # تحليل المشاعر
                    if long_metrics.strength >= SIGNAL_THRESHOLD:
                        market_data['market_sentiment'] = "صعودي قوي"
                    elif short_metrics.strength >= SIGNAL_THRESHOLD:
                        market_data['market_sentiment'] = "هبوطي قوي"
                    elif long_metrics.strength > short_metrics.strength:
                        market_data['market_sentiment'] = "صعودي معتدل"
                    else:
                        market_data['market_sentiment'] = "هبوطي معتدل"
                except Exception as e:
                    print(f"Error calculating signal strength: {e}")
                    market_data['market_sentiment'] = "غير متوفر"
                
                # حالة الحجم
                volume_ratio = last_h4.get('volume_ratio', 1)
                if volume_ratio > 1.5:
                    market_data['volume_status'] = "مرتفع جداً"
                elif volume_ratio > 1.2:
                    market_data['volume_status'] = "مرتفع"
                elif volume_ratio > 0.8:
                    market_data['volume_status'] = "طبيعي"
                else:
                    market_data['volume_status'] = "منخفض"
                
        except Exception as e:
            print(f"Dashboard error: {e}")
            market_data['error'] = str(e)
        
        # تاريخ الإشارات
        recent_signals = state.signals_history[-10:] if state.signals_history else []
        signals_html = ""
        for sig in reversed(recent_signals):
            if sig['strength'] >= SIGNAL_THRESHOLD:
                strength_color = "green"
                signal_type = f"<strong>{sig['type']}</strong>"
                signal_icon = "🚀"
            elif sig['strength'] >= MEDIUM_THRESHOLD:
                strength_color = "orange"
                signal_type = f"<em>{sig['type']}</em>"
                signal_icon = "👁️"
            else:
                strength_color = "gray"
                signal_type = sig['type']
                signal_icon = "📊"
                
            signals_html += f"""
            <div class="signal-card" style="border-color: {strength_color}">
                <div class="signal-header">
                    <span class="signal-icon">{signal_icon}</span>
                    <span class="signal-type">{signal_type}</span>
                    <span class="signal-strength" style="color:{strength_color}">{sig['strength']}/100</span>
                </div>
                <div class="signal-body">
                    <div>السعر: {sig['price']:.2f}</div>
                    <div>الثقة: {sig['confidence']}</div>
                    <div>الوقت: {sig['time'].strftime('%H:%M')}</div>
                </div>
            </div>
            """
        
        # تحليل إحصائي
        stats_html = ""
        if state.signals_history:
            total_signals = len(state.signals_history)
            strong_signals = len([s for s in state.signals_history if s['strength'] >= SIGNAL_THRESHOLD])
            long_signals = len([s for s in state.signals_history if 'LONG' in s['type']])
            avg_strength = sum(s['strength'] for s in state.signals_history) / total_signals
            
            stats_html = f"""
            <div class="stats-summary">
                <div class="stat-item">
                    <span class="stat-label">إجمالي الإشارات:</span>
                    <span class="stat-value">{total_signals}</span>
                </div>
                <div class="stat-item">
                    <span class="stat-label">إشارات قوية:</span>
                    <span class="stat-value">{strong_signals}</span>
                </div>
                <div class="stat-item">
                    <span class="stat-label">إشارات شراء:</span>
                    <span class="stat-value">{long_signals}</span>
                </div>
                <div class="stat-item">
                    <span class="stat-label">متوسط القوة:</span>
                    <span class="stat-value">{avg_strength:.1f}/100</span>
                </div>
            </div>
            """
    
    # التحديث النهائي لواجهة HTML مع التصحيحات
    return f"""
    <html>
        <head>
            <title>روبوت التداول - نظام الإشارات ثلاثي المستويات</title>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                :root {{
                    --primary-color: #3498db;
                    --success-color: #2ecc71;
                    --danger-color: #e74c3c;
                    --warning-color: #f39c12;
                    --info-color: #9b59b6;
                    --dark-color: #2c3e50;
                    --light-color: #ecf0f1;
                    --gray-color: #95a5a6;
                }}
                
                * {{
                    box-sizing: border-box;
                    margin: 0;
                    padding: 0;
                }}
                
                body {{
                    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                    line-height: 1.6;
                    color: #333;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    min-height: 100vh;
                    padding: 20px;
                }}
                
                .container {{
                    max-width: 1400px;
                    margin: 0 auto;
                }}
                
                .header {{
                    text-align: center;
                    margin-bottom: 30px;
                    padding: 20px;
                    background: rgba(255, 255, 255, 0.95);
                    border-radius: 15px;
                    box-shadow: 0 10px 30px rgba(0,0,0,0.1);
                }}
                
                .header h1 {{
                    color: var(--dark-color);
                    font-size: 2.5rem;
                    margin-bottom: 10px;
                    background: linear-gradient(90deg, var(--primary-color), var(--info-color));
                    -webkit-background-clip: text;
                    -webkit-text-fill-color: transparent;
                }}
                
                .header p {{
                    color: var(--gray-color);
                    font-size: 1.1rem;
                }}
                
                .main-grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(350px, 1fr));
                    gap: 20px;
                    margin-bottom: 30px;
                }}
                
                .card {{
                    background: rgba(255, 255, 255, 0.95);
                    border-radius: 15px;
                    padding: 25px;
                    box-shadow: 0 8px 25px rgba(0,0,0,0.08);
                    transition: transform 0.3s ease;
                    border: 1px solid rgba(255,255,255,0.2);
                }}
                
                .card:hover {{
                    transform: translateY(-5px);
                }}
                
                .card-title {{
                    color: var(--dark-color);
                    font-size: 1.4rem;
                    margin-bottom: 20px;
                    padding-bottom: 10px;
                    border-bottom: 2px solid var(--light-color);
                    display: flex;
                    align-items: center;
                    gap: 10px;
                }}
                
                .card-title i {{
                    color: var(--primary-color);
                }}
                
                .info-grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
                    gap: 15px;
                }}
                
                .info-item {{
                    background: var(--light-color);
                    padding: 15px;
                    border-radius: 10px;
                    transition: all 0.3s ease;
                }}
                
                .info-item:hover {{
                    background: #d5dbdb;
                }}
                
                .info-label {{
                    font-size: 0.9rem;
                    color: var(--gray-color);
                    margin-bottom: 5px;
                }}
                
                .info-value {{
                    font-size: 1.2rem;
                    font-weight: bold;
                    color: var(--dark-color);
                }}
                
                .signal-grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
                    gap: 15px;
                    margin-top: 20px;
                }}
                
                .signal-card {{
                    background: white;
                    border-radius: 10px;
                    padding: 15px;
                    border-left: 4px solid;
                    box-shadow: 0 4px 12px rgba(0,0,0,0.05);
                }}
                
                .signal-header {{
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    margin-bottom: 10px;
                }}
                
                .signal-body {{
                    display: grid;
                    grid-template-columns: repeat(2, 1fr);
                    gap: 8px;
                    font-size: 0.9rem;
                }}
                
                .strength-bar {{
                    height: 10px;
                    background: var(--light-color);
                    border-radius: 5px;
                    margin-top: 10px;
                    overflow: hidden;
                }}
                
                .strength-fill {{
                    height: 100%;
                    border-radius: 5px;
                    transition: width 1s ease;
                }}
                
                .strength-fill.long {{
                    background: linear-gradient(90deg, var(--success-color), #27ae60);
                }}
                
                .strength-fill.short {{
                    background: linear-gradient(90deg, var(--danger-color), #c0392b);
                }}
                
                .signal-levels {{
                    display: flex;
                    justify-content: space-between;
                    margin-top: 20px;
                    text-align: center;
                }}
                
                .level {{
                    flex: 1;
                    padding: 15px;
                    border-radius: 10px;
                    margin: 0 5px;
                }}
                
                .level.medium {{
                    background: linear-gradient(135deg, #fdc830, #f37335);
                    color: white;
                }}
                
                .level.strong {{
                    background: linear-gradient(135deg, #ff416c, #ff4b2b);
                    color: white;
                }}
                
                .level.very-strong {{
                    background: linear-gradient(135deg, #11998e, #38ef7d);
                    color: white;
                }}
                
                .status-badge {{
                    display: inline-block;
                    padding: 5px 15px;
                    border-radius: 20px;
                    font-size: 0.8rem;
                    font-weight: bold;
                    text-transform: uppercase;
                }}
                
                .status-active {{
                    background: var(--success-color);
                    color: white;
                }}
                
                .status-warning {{
                    background: var(--warning-color);
                    color: white;
                }}
                
                .status-danger {{
                    background: var(--danger-color);
                    color: white;
                }}
                
                .stats-summary {{
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                    gap: 15px;
                    margin-top: 20px;
                }}
                
                .stat-item {{
                    background: white;
                    padding: 15px;
                    border-radius: 10px;
                    text-align: center;
                    border: 1px solid var(--light-color);
                }}
                
                .stat-label {{
                    display: block;
                    color: var(--gray-color);
                    font-size: 0.9rem;
                    margin-bottom: 5px;
                }}
                
                .stat-value {{
                    display: block;
                    font-size: 1.5rem;
                    font-weight: bold;
                    color: var(--primary-color);
                }}
                
                @media (max-width: 768px) {{
                    .main-grid {{
                        grid-template-columns: 1fr;
                    }}
                    
                    .header h1 {{
                        font-size: 2rem;
                    }}
                }}
                
                .refresh-info {{
                    text-align: center;
                    margin-top: 20px;
                    color: var(--gray-color);
                    font-size: 0.9rem;
                }}
                
                .nav-links {{
                    display: flex;
                    justify-content: center;
                    gap: 20px;
                    margin-top: 20px;
                }}
                
                .nav-link {{
                    color: var(--primary-color);
                    text-decoration: none;
                    padding: 10px 20px;
                    border-radius: 25px;
                    border: 2px solid var(--primary-color);
                    transition: all 0.3s ease;
                }}
                
                .nav-link:hover {{
                    background: var(--primary-color);
                    color: white;
                }}
            </style>
            <script>
                function refreshData() {{
                    location.reload();
                }}
                
                // تحديث تلقائي كل 60 ثانية
                setTimeout(refreshData, 60000);
            </script>
        </head>
        <body>
            <div class="container">
                <!-- Header -->
                <div class="header">
                    <h1>📈 روبوت التداول الذكي</h1>
                    <p>نظام الإشارات ثلاثي المستويات | {SYMBOL}</p>
                    <div class="nav-links">
                        <a href="/health" class="nav-link">الحالة الصحية</a>
                        <a href="/stats" class="nav-link">إحصائيات مفصلة</a>
                        <a href="/config" class="nav-link">الإعدادات</a>
                        <a href="#" class="nav-link" onclick="refreshData()">🔄 تحديث</a>
                    </div>
                </div>
                
                <!-- Main Grid -->
                <div class="main-grid">
                    
                    <!-- Market Overview Card -->
                    <div class="card">
                        <h2 class="card-title">📊 نظرة عامة على السوق</h2>
                        <div class="info-grid">
                            <div class="info-item">
                                <div class="info-label">السعر الحالي</div>
                                <div class="info-value">{market_data['current_price']}</div>
                            </div>
                            <div class="info-item">
                                <div class="info-label">التغير (24س)</div>
                                <div class="info-value" style="color:{market_data['price_change_color']}">
                                    {market_data['price_change_24h']}
                                </div>
                            </div>
                            <div class="info-item">
                                <div class="info-label">الحجم (24س)</div>
                                <div class="info-value">{market_data['volume_24h']}</div>
                            </div>
                            <div class="info-item">
                                <div class="info-label">الأعلى (24س)</div>
                                <div class="info-value">{market_data['high_24h']}</div>
                            </div>
                            <div class="info-item">
                                <div class="info-label">الأدنى (24س)</div>
                                <div class="info-value">{market_data['low_24h']}</div>
                            </div>
                            <div class="info-item">
                                <div class="info-label">مشاعر السوق</div>
                                <div class="info-value" style="color:{market_data['trend_color']}">
                                    {market_data['market_sentiment']}
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <!-- Technical Indicators Card -->
                    <div class="card">
                        <h2 class="card-title">🔍 المؤشرات الفنية</h2>
                        <div class="info-grid">
                            <div class="info-item">
                                <div class="info-label">RSI</div>
                                <div class="info-value">{market_data['current_rsi']}</div>
                                <small>{market_data['rsi_zone']} | {market_data['rsi_trend']}</small>
                            </div>
                            <div class="info-item">
                                <div class="info-label">MACD</div>
                                <div class="info-value" style="color:{'green' if market_data['macd_status'] == 'إيجابي' else 'red'}">
                                    {market_data['macd_status']}
                                </div>
                                <small>القيمة: {market_data['macd_value']}</small>
                            </div>
                            <div class="info-item">
                                <div class="info-label">الاتجاه</div>
                                <div class="info-value" style="color:{market_data['trend_color']}">
                                    {market_data['trend_status']}
                                </div>
                            </div>
                            <div class="info-item">
                                <div class="info-label">التقلب (ATR)</div>
                                <div class="info-value">{market_data['atr_percent']}</div>
                                <small>{market_data['volatility']}</small>
                            </div>
                            <div class="info-item">
                                <div class="info-label">حالة الحجم</div>
                                <div class="info-value">{market_data['volume_status']}</div>
                            </div>
                            <div class="info-item">
                                <div class="info-label">تقارب المتوسطات</div>
                                <div class="info-value" style="font-size:0.9rem">{market_data['ema_alignment']}</div>
                            </div>
                        </div>
                    </div>
                    
                    <!-- Signal Strengths Card -->
                    <div class="card">
                        <h2 class="card-title">📈 قوة الإشارات</h2>
                        
                        <div style="margin-bottom: 20px;">
                            <div style="display: flex; justify-content: space-between; margin-bottom: 5px;">
                                <span>قوة الشراء: {market_data['long_strength']}/100</span>
                                <span style="color: var(--success-color)">{market_data['long_confidence']}</span>
                            </div>
                            <div class="strength-bar">
                                <div class="strength-fill long" style="width: {market_data['long_strength']}%"></div>
                            </div>
                        </div>
                        
                        <div style="margin-bottom: 20px;">
                            <div style="display: flex; justify-content: space-between; margin-bottom: 5px;">
                                <span>قوة البيع: {market_data['short_strength']}/100</span>
                                <span style="color: var(--danger-color)">{market_data['short_confidence']}</span>
                            </div>
                            <div class="strength-bar">
                                <div class="strength-fill short" style="width: {market_data['short_strength']}%"></div>
                            </div>
                        </div>
                        
                        <div class="signal-levels">
                            <div class="level medium">
                                <div>👁️ MEDIUM</div>
                                <div>{MEDIUM_THRESHOLD}-{SIGNAL_THRESHOLD-1}</div>
                                <small>مراقبة</small>
                            </div>
                            <div class="level strong">
                                <div>🚀 STRONG</div>
                                <div>{SIGNAL_THRESHOLD}-{HIGH_STRENGTH-1}</div>
                                <small>تنفيذ</small>
                            </div>
                            <div class="level very-strong">
                                <div>💎 VERY STRONG</div>
                                <div>{HIGH_STRENGTH}+</div>
                                <small>ممتاز</small>
                            </div>
                        </div>
                    </div>
                    
                    <!-- Support & Resistance Card -->
                    <div class="card">
                        <h2 class="card-title">🎯 الدعم والمقاومة</h2>
                        <div class="info-grid">
                            <div class="info-item">
                                <div class="info-label">مستوى المقاومة</div>
                                <div class="info-value" style="color: var(--danger-color)">{market_data['resistance_level']}</div>
                                <small>أعلى 10 شموع</small>
                            </div>
                            <div class="info-item">
                                <div class="info-label">مستوى الدعم</div>
                                <div class="info-value" style="color: var(--success-color)">{market_data['support_level']}</div>
                                <small>أقل 10 شموع</small>
                            </div>
                        </div>
                        
                        <div style="margin-top: 20px; padding: 15px; background: var(--light-color); border-radius: 10px;">
                            <h3 style="margin-bottom: 10px; color: var(--dark-color);">🎲 إحصائيات النظام</h3>
                            {stats_html if stats_html else "<p>لا توجد إحصائيات بعد</p>"}
                        </div>
                    </div>
                    
                    <!-- Recent Signals Card -->
                    <div class="card" style="grid-column: span 2;">
                        <h2 class="card-title">📨 الإشارات الأخيرة</h2>
                        <div class="signal-grid">
                            {signals_html if signals_html else '<div style="text-align: center; padding: 40px; color: var(--gray-color);">لا توجد إشارات حالياً</div>'}
                        </div>
                    </div>
                    
                    <!-- Data Status Card -->
                    <div class="card">
                        <h2 class="card-title">🔄 حالة البيانات</h2>
                        <div class="info-grid">
                            <div class="info-item">
                                <div class="info-label">شموع 4 ساعات</div>
                                <div class="info-value">{h4_count}</div>
                                <small>المطلوب: 210</small>
                            </div>
                            <div class="info-item">
                                <div class="info-label">شموع 30 دقيقة</div>
                                <div class="info-value">{m30_count}</div>
                                <small>المطلوب: 50</small>
                            </div>
                            <div class="info-item">
                                <div class="info-label">الإشارات المسجلة</div>
                                <div class="info-value">{len(state.signals_history)}</div>
                            </div>
                            <div class="info-item">
                                <div class="info-label">آخر تحديث</div>
                                <div class="info-value">{datetime.utcnow().strftime('%H:%M UTC')}</div>
                                <small>{datetime.utcnow().strftime('%Y-%m-%d')}</small>
                            </div>
                        </div>
                        
                        <div style="margin-top: 20px; text-align: center;">
                            <span class="status-badge status-active">✅ النظام يعمل</span>
                            {f'<span style="margin-left: 10px; color: var(--gray-color); font-size: 0.9rem;">آخر إشارة: {state.last_signal_time.strftime("%H:%M") if state.last_signal_time else "لا يوجد"}</span>' if state.last_signal_time else ''}
                        </div>
                    </div>
                    
                </div>
                
                <div class="refresh-info">
                    <p>سيتم تحديث البيانات تلقائياً خلال <span id="countdown">60</span> ثانية</p>
                    <p><small>© 2024 نظام التداول الذكي ثلاثي المستويات</small></p>
                </div>
            </div>
            
            <script>
                // عداد التحديث التلقائي
                let countdown = 60;
                setInterval(function() {{
                    countdown--;
                    document.getElementById('countdown').textContent = countdown;
                    if (countdown <= 0) {{
                        refreshData();
                    }}
                }}, 1000);
            </script>
        </body>
    </html>
    """

@app.route('/health')
def health():
    """Health check endpoint"""
    with state.lock:
        data_ok = len(state.klines_h4) > 100 and len(state.klines_m30) > 50
    
    status = {
        "status": "healthy" if data_ok else "collecting_data",
        "data_4h_candles": len(state.klines_h4),
        "data_30m_candles": len(state.klines_m30),
        "last_signal": state.last_signal_time.isoformat() if state.last_signal_time else None,
        "signal_levels": {
            "medium_threshold": MEDIUM_THRESHOLD,
            "strong_threshold": SIGNAL_THRESHOLD,
            "high_strength": HIGH_STRENGTH
        },
        "timestamp": datetime.utcnow().isoformat()
    }
    
    return status, 200 if data_ok else 202

@app.route('/stats')
def stats():
    """Detailed statistics endpoint"""
    with state.lock:
        if not state.klines_h4:
            return {"error": "Insufficient data"}, 200
        
        try:
            df = pd.DataFrame(state.klines_h4[-100:])
            numeric_cols = ['open', 'high', 'low', 'close', 'volume']
            df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors='coerce')
            df = compute_indicators(df)
            last = df.iloc[-1]
            
            stats_data = {
                "price": float(last['close']),
                "rsi": float(last['rsi']),
                "ema20": float(last['ema20']),
                "ema50": float(last['ema50']),
                "ema200": float(last['ema200']),
                "macd": float(last['macd']),
                "signal_line": float(last['signal']),
                "volume_ratio": float(last.get('volume_ratio', 0)),
                "atr_percent": float(last.get('atr_percent', 0)),
                "signal_levels": {
                    "medium": MEDIUM_THRESHOLD,
                    "strong": SIGNAL_THRESHOLD,
                    "very_strong": HIGH_STRENGTH
                },
                "total_signals": len(state.signals_history),
                "recent_signals": [
                    {
                        "type": sig['type'],
                        "strength": sig['strength'],
                        "time": sig['time'].isoformat()
                    }
                    for sig in state.signals_history[-3:]
                ]
            }
            
            return stats_data
        except Exception as e:
            return {"error": str(e)}, 500

@app.route('/config')
def config():
    """Configuration endpoint"""
    config_data = {
        "symbol": SYMBOL,
        "interval_primary": INTERVAL,
        "interval_confirmation": CONFIRM_TF,
        "signal_levels": {
            "min_strength": MIN_STRENGTH,
            "medium_threshold": MEDIUM_THRESHOLD,
            "strong_threshold": SIGNAL_THRESHOLD,
            "high_strength": HIGH_STRENGTH
        },
        "weights": WEIGHTS,
        "signal_cooldown_seconds": state.signal_cooldown,
        "environment_variables": {
            "BINANCE_API_KEY": "***" if API_KEY else "Not set",
            "NTFY_TOPIC": NTFY_URL if NTFY_URL else "Not set"
        }
    }
    
    return config_data

# ────────────────────────────────────────────────
#                   Main Entry
# ────────────────────────────────────────────────

if __name__ == "__main__":
    # Startup notification
    startup_msg = (
        "3-Level Signal Strength Bot STARTED\n\n"
        f"Symbol: {SYMBOL}\n"
        f"Timeframes: {INTERVAL} + {CONFIRM_TF}\n\n"
        "Signal Levels:\n"
        f"- MEDIUM (Watch): {MEDIUM_THRESHOLD}-{SIGNAL_THRESHOLD-1}/100\n"
        f"- STRONG (Entry): {SIGNAL_THRESHOLD}-{HIGH_STRENGTH-1}/100\n"
        f"- VERY STRONG: {HIGH_STRENGTH}+/100\n\n"
        "Weights:\n"
        f"- Trend: {WEIGHTS['trend']}%\n"
        f"- Momentum: {WEIGHTS['momentum']}%\n"
        f"- Volume: {WEIGHTS['volume']}%\n"
        f"- Structure: {WEIGHTS['structure']}%\n"
        f"- Multi-TF: {WEIGHTS['multi_tf']}%\n\n"
        f"Status: Monitoring for signals\n"
        f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    )
    
    send_ntfy(startup_msg, "3-Level Signal Bot Started", "high")
    
    # Start WebSocket thread
    def run_async():
        asyncio.run(run_websockets())
    
    ws_thread = threading.Thread(target=run_async, daemon=True)
    ws_thread.start()
    
    # Start Flask server
    port = int(os.environ.get("PORT", 5000))
    
    print(f"\n{'='*70}")
    print(f"3-LEVEL SIGNAL STRENGTH TRADING BOT")
    print(f"{'='*70}")
    print(f"Symbol: {SYMBOL}")
    print(f"Timeframes: {INTERVAL} (Primary), {CONFIRM_TF} (Confirmation)")
    print(f"\nSIGNAL LEVELS:")
    print(f"  - MEDIUM (Watch): {MEDIUM_THRESHOLD}-{SIGNAL_THRESHOLD-1}/100")
    print(f"  - STRONG (Entry): {SIGNAL_THRESHOLD}-{HIGH_STRENGTH-1}/100")
    print(f"  - VERY STRONG: {HIGH_STRENGTH}+/100")
    print(f"\nWeights: Trend({WEIGHTS['trend']}%) | Momentum({WEIGHTS['momentum']}%)")
    print(f"         Volume({WEIGHTS['volume']}%) | Structure({WEIGHTS['structure']}%)")
    print(f"         Multi-TF({WEIGHTS['multi_tf']}%)")
    print(f"\nWeb Dashboard: http://localhost:{port}")
    print(f"{'='*70}")
    print(f"Waiting for data and calculating signal strengths...\n")
    
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
