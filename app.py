"""
Crypto Signal Analyzer Bot - Smart SELL Edition (High Sensitivity)
Version 3.5.2-sell - Detects sell signals with advanced bearish logic
All notifications in English, no emojis.
"""

import os
import json
import time
import math
import logging
import threading
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict
from enum import Enum
from threading import Lock

from flask import Flask, render_template, jsonify, request
import ccxt

# ======================
# إعدادات التسجيل
# ======================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('crypto_signal_sell.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# ======================
# هياكل البيانات الأساسية
# ======================
class SignalType(Enum):
    STRONG_BUY = "STRONG BUY"
    BUY = "BUY"
    NEUTRAL = "NEUTRAL"
    SELL = "SELL"
    STRONG_SELL = "STRONG SELL"

class IndicatorType(Enum):
    TREND = "trend"
    MOMENTUM = "momentum"
    VOLUME = "volume"
    VOLATILITY = "volatility"
    SENTIMENT = "sentiment"
    STRUCTURE = "structure"
    SUPPORT_RESISTANCE = "support_resistance"
    # إضافات جديدة لتحسين حساسية البيع
    BEARISH_DIVERGENCE = "bearish_divergence"
    OVERBOUGHT = "overbought"

@dataclass
class CoinConfig:
    symbol: str
    name: str
    base_asset: str
    quote_asset: str
    enabled: bool = True

@dataclass
class IndicatorScore:
    name: str
    raw_score: float      # 0 to 1, where 1 means strong bearish signal (for sell-focused bot)
    weighted_score: float
    percentage: float      # raw_score * 100
    weight: float
    description: str
    color: str

@dataclass
class CoinSignal:
    symbol: str
    name: str
    current_price: float
    price_change_24h: float
    high_24h: float
    low_24h: float
    volume_24h: float
    total_percentage: float   # bearish score (0-100, higher means stronger sell)
    signal_type: SignalType
    signal_strength: str
    signal_color: str
    indicator_scores: Dict[str, IndicatorScore]
    last_updated: datetime
    fear_greed_value: int
    is_valid: bool = True
    error_message: Optional[str] = None

@dataclass
class Notification:
    id: str
    timestamp: datetime
    coin_symbol: str
    coin_name: str
    message: str
    notification_type: str
    signal_strength: float
    price: float

# ======================
# إعدادات التطبيق (معدلة لصالح البيع)
# ======================
class AppConfig:
    @staticmethod
    def get_top_coins(limit=10):
        """جلب أفضل العملات من حيث حجم التداول"""
        try:
            exchange = ccxt.binance()
            tickers = exchange.fetch_tickers()
            usdt_pairs = {k: v for k, v in tickers.items() 
                         if k.endswith('/USDT') and v.get('quoteVolume')}
            sorted_pairs = sorted(usdt_pairs.items(), 
                                key=lambda x: x[1]['quoteVolume'] or 0, 
                                reverse=True)
            coins = []
            EXCLUDED_COINS = ['LUNA', 'UST', 'FTT', 'TERRA','USD1','USDC']
            for symbol, ticker in sorted_pairs[:limit]:
                base = symbol.replace('/USDT', '')
                if base not in EXCLUDED_COINS:
                    coins.append(CoinConfig(symbol, base, base, 'USDT'))
            if coins:
                logger.info(f"✅ تم جلب {len(coins)} عملة من Binance")
                return coins
            else:
                return AppConfig._get_default_coins()
        except Exception as e:
            logger.error(f"❌ خطأ في جلب العملات: {e}")
            return AppConfig._get_default_coins()

    @staticmethod
    def _get_default_coins():
        return [
            CoinConfig("BTC/USDT", "Bitcoin", "BTC", "USDT"),
            CoinConfig("ETH/USDT", "Ethereum", "ETH", "USDT"),
            CoinConfig("BNB/USDT", "Binance Coin", "BNB", "USDT"),
            CoinConfig("SOL/USDT", "Solana", "SOL", "USDT"),
            CoinConfig("XRP/USDT", "Ripple", "XRP", "USDT"),
            CoinConfig("ADA/USDT", "Cardano", "ADA", "USDT"),
            CoinConfig("DOGE/USDT", "Dogecoin", "DOGE", "USDT"),
            CoinConfig("AVAX/USDT", "Avalanche", "AVAX", "USDT"),
            CoinConfig("DOT/USDT", "Polkadot", "DOT", "USDT"),
            CoinConfig("MATIC/USDT", "Polygon", "MATIC", "USDT"),
            CoinConfig("LINK/USDT", "Chainlink", "LINK", "USDT"),
            CoinConfig("TRX/USDT", "TRON", "TRX", "USDT"),
            CoinConfig("ZEC/USDT", "Zcash", "ZEC", "USDT"),
            CoinConfig("LTC/USDT", "Litecoin", "LTC", "USDT"),
            CoinConfig("BCH/USDT", "Bitcoin Cash", "BCH", "USDT"),
        ]

    COINS = get_top_coins(15)

    # أوزان معدلة لصالح مؤشرات البيع (زِيدت أوزان المؤشرات الهابطة)
    INDICATOR_WEIGHTS = {
        IndicatorType.TREND.value: 0.20,
        IndicatorType.MOMENTUM.value: 0.20,
        IndicatorType.VOLUME.value: 0.15,
        IndicatorType.VOLATILITY.value: 0.10,
        IndicatorType.SENTIMENT.value: 0.10,
        IndicatorType.STRUCTURE.value: 0.10,
        IndicatorType.SUPPORT_RESISTANCE.value: 0.10,
        IndicatorType.BEARISH_DIVERGENCE.value: 0.05,   # مؤشر جديد
        IndicatorType.OVERBOUGHT.value: 0.05            # مؤشر جديد
    }

    # عتبات الإشارة (تم تعديلها لجعل البيع أكثر حساسية)
    SIGNAL_THRESHOLDS = {
        SignalType.STRONG_SELL: 70,    # كانت 30 سابقاً، الآن أي نسبة أعلى من 70 تعتبر بيع قوي
        SignalType.SELL: 60,
        SignalType.NEUTRAL: 40,
        SignalType.BUY: 30,
        SignalType.STRONG_BUY: 20
    }

    UPDATE_INTERVAL = 120  # 2 minutes
    MAX_CANDLES = 200

    # ألوان المؤشرات (تم تعديل بعضها لتعكس الاتجاه البيعي)
    INDICATOR_COLORS = {
        IndicatorType.TREND.value: '#E63946',        # أحمر للاتجاه الهابط
        IndicatorType.MOMENTUM.value: '#F4A261',
        IndicatorType.VOLUME.value: '#3BB273',
        IndicatorType.VOLATILITY.value: '#F18F01',
        IndicatorType.SENTIMENT.value: '#6C757D',
        IndicatorType.STRUCTURE.value: '#8F2D56',
        IndicatorType.SUPPORT_RESISTANCE.value: '#6A4C93',
        IndicatorType.BEARISH_DIVERGENCE.value: '#B23B3B',
        IndicatorType.OVERBOUGHT.value: '#D94F4F'
    }

    INDICATOR_DISPLAY_NAMES = {
        IndicatorType.TREND.value: 'Bearish Trend',
        IndicatorType.MOMENTUM.value: 'Bearish Momentum',
        IndicatorType.VOLUME.value: 'Selling Volume',
        IndicatorType.VOLATILITY.value: 'Volatility (high risk)',
        IndicatorType.SENTIMENT.value: 'Market Sentiment (greed)',
        IndicatorType.STRUCTURE.value: 'Price Structure (tops)',
        IndicatorType.SUPPORT_RESISTANCE.value: 'Resistance nearby',
        IndicatorType.BEARISH_DIVERGENCE.value: 'Bearish Divergence',
        IndicatorType.OVERBOUGHT.value: 'Overbought Condition'
    }

    INDICATOR_DESCRIPTIONS = {
        IndicatorType.TREND.value: 'Price below key moving averages → strong sell signal',
        IndicatorType.MOMENTUM.value: 'RSI overbought or falling momentum → sell',
        IndicatorType.VOLUME.value: 'High volume on red candles → distribution',
        IndicatorType.VOLATILITY.value: 'High volatility often precedes sharp drops',
        IndicatorType.SENTIMENT.value: 'Extreme greed (F&G > 80) signals potential top',
        IndicatorType.STRUCTURE.value: 'Price near recent highs, forming double tops',
        IndicatorType.SUPPORT_RESISTANCE.value: 'Price approaching strong resistance',
        IndicatorType.BEARISH_DIVERGENCE.value: 'Price makes higher high but RSI lower → reversal',
        IndicatorType.OVERBOUGHT.value: 'RSI > 70 or Bollinger upper band touch'
    }

# ======================
# إعدادات APIs الخارجية
# ======================
class ExternalAPIConfig:
    BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', '')
    BINANCE_SECRET_KEY = os.environ.get('BINANCE_SECRET_KEY', '')
    NTFY_TOPIC = os.environ.get('NTFY_TOPIC_SELL', 'crypto_sell_alerts')  # topic مختلف
    NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
    FGI_API_URL = "https://api.alternative.me/fng/"
    REQUEST_TIMEOUT = 10
    MAX_RETRIES = 2

# ======================
# Binance Client (نفس الكود السابق)
# ======================
class BinanceClient:
    def __init__(self):
        self.exchange = ccxt.binance({
            'apiKey': ExternalAPIConfig.BINANCE_API_KEY,
            'secret': ExternalAPIConfig.BINANCE_SECRET_KEY,
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'}
        })

    def fetch_ohlcv(self, symbol: str, timeframe: str = '15m', limit: int = 200) -> Optional[List]:
        try:
            return self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        except Exception as e:
            logger.error(f"Binance OHLCV error {symbol}: {e}")
            return None

    def fetch_ticker(self, symbol: str) -> Optional[Dict]:
        try:
            return self.exchange.fetch_ticker(symbol)
        except Exception as e:
            logger.error(f"Binance ticker error {symbol}: {e}")
            return None

    def fetch_24h_stats(self, symbol: str) -> Dict:
        ticker = self.fetch_ticker(symbol)
        if ticker:
            return {
                'change': ticker.get('percentage', 0.0),
                'high': ticker.get('high', 0.0),
                'low': ticker.get('low', 0.0),
                'volume': ticker.get('quoteVolume', 0.0)
            }
        return {'change': 0.0, 'high': 0.0, 'low': 0.0, 'volume': 0.0}

    def get_current_price(self, symbol: str) -> float:
        ticker = self.fetch_ticker(symbol)
        return ticker['last'] if ticker else 0.0

# ======================
# Fear & Greed Fetcher (مع تعديل لتفسير الجشع كإشارة بيع)
# ======================
class FearGreedFetcher:
    def __init__(self):
        self.last_value = 50
        self.last_update = None
        self.cache_ttl = 300

    def get(self) -> Tuple[float, int]:
        now = datetime.now()
        if self.last_update and (now - self.last_update).total_seconds() < self.cache_ttl:
            return self._to_bearish_score(self.last_value), self.last_value

        try:
            resp = requests.get(ExternalAPIConfig.FGI_API_URL, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if 'data' in data and data['data']:
                    value = int(data['data'][0]['value'])
                    self.last_value = value
                    self.last_update = now
                    return self._to_bearish_score(value), value
        except Exception as e:
            logger.error(f"FGI fetch error: {e}")

        return self._to_bearish_score(self.last_value), self.last_value

    def _to_bearish_score(self, value: int) -> float:
        # كلما زاد الخوف، قلت إشارة البيع (لأن الخوف قد يعني قاع)
        # كلما زاد الجشع، زادت إشارة البيع (تصحيح مرتقب)
        if value >= 80:
            return 0.95   # جشع شديد → بيع قوي
        if value >= 60:
            return 0.75
        if value >= 40:
            return 0.50   # محايد
        if value >= 20:
            return 0.30
        return 0.10       # خوف شديد → لا توجد إشارة بيع

# ======================
# Indicator Calculators (معدلة لتعطي درجات عالية عندما يكون السيناريو هابطاً)
# ======================
class IndicatorCalculator:
    @staticmethod
    def sma(prices: List[float], period: int) -> List[Optional[float]]:
        result = []
        for i in range(len(prices)):
            if i < period - 1:
                result.append(None)
            else:
                result.append(sum(prices[i - period + 1:i + 1]) / period)
        return result

    @staticmethod
    def ema(prices: List[float], period: int) -> List[float]:
        if not prices:
            return []
        k = 2 / (period + 1)
        ema_values = [prices[0]]
        for i in range(1, len(prices)):
            ema_values.append(prices[i] * k + ema_values[-1] * (1 - k))
        return ema_values

    @staticmethod
    def rsi(prices: List[float], period: int = 14) -> List[Optional[float]]:
        if len(prices) < period + 1:
            return [None] * len(prices)
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]

        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        rsi_values = [None] * period

        for i in range(period, len(prices)):
            if avg_loss == 0:
                rsi = 100
            else:
                rs = avg_gain / avg_loss
                rsi = 100 - (100 / (1 + rs))
            rsi_values.append(rsi)
            if i < len(prices) - 1:
                avg_gain = (avg_gain * (period - 1) + gains[i]) / period
                avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        return rsi_values

    # ===== مؤشرات الاتجاه الهابط =====
    @staticmethod
    def bearish_trend_strength(close_prices: List[float]) -> float:
        """كلما كان السعر أقل من المتوسطات، كلما زادت الدرجة (إشارة بيع قوية)."""
        if len(close_prices) < 30:
            return 0.5
        sma_20 = IndicatorCalculator.sma(close_prices, 20)[-1]
        sma_50 = IndicatorCalculator.sma(close_prices, 50)[-1]
        sma_100 = IndicatorCalculator.sma(close_prices, 100)[-1]
        if None in (sma_20, sma_50, sma_100):
            return 0.5

        current = close_prices[-1]
        score = 0.0
        if current < sma_20:
            score += 0.4
        if current < sma_50:
            score += 0.3
        if current < sma_100:
            score += 0.3
        if sma_20 < sma_50 < sma_100:
            score += 0.3   # ترتيب هابط
        elif sma_20 > sma_50 > sma_100:
            score -= 0.2   # ترتيب صاعد

        return max(0.0, min(1.0, (score + 1) / 2))

    @staticmethod
    def bearish_momentum(close_prices: List[float]) -> float:
        """RSI فوق 70 يعطي درجة عالية (بيع)، وأسفل 30 يعطي درجة منخفضة."""
        if len(close_prices) < 20:
            return 0.5

        rsi_vals = IndicatorCalculator.rsi(close_prices, 14)
        last_rsi = rsi_vals[-1] if rsi_vals[-1] is not None else 50

        if last_rsi > 70:
            rsi_score = 0.9
        elif last_rsi > 60:
            rsi_score = 0.7
        elif last_rsi > 50:
            rsi_score = 0.5
        elif last_rsi > 30:
            rsi_score = 0.3
        else:
            rsi_score = 0.1

        # Rate of change سلبي يعطي قوة بيع
        roc_14 = (close_prices[-1] - close_prices[-14]) / close_prices[-14] * 100
        if roc_14 < -2:
            roc_score = 0.8
        elif roc_14 < 0:
            roc_score = 0.6
        elif roc_14 < 2:
            roc_score = 0.4
        else:
            roc_score = 0.2

        return rsi_score * 0.6 + roc_score * 0.4

    @staticmethod
    def selling_volume(volumes: List[float], close_prices: List[float]) -> float:
        """تحليل الحجم: حجم مرتفع مع شموع حمراء → بيع."""
        if len(volumes) < 20:
            return 0.5
        current_vol = volumes[-1]
        avg_vol = sum(volumes[-20:]) / 20
        ratio = current_vol / avg_vol if avg_vol > 0 else 1.0

        # حجم أعلى من المتوسط
        if ratio > 2.0:
            vol_score = 0.8
        elif ratio > 1.5:
            vol_score = 0.7
        elif ratio > 1.0:
            vol_score = 0.6
        elif ratio > 0.7:
            vol_score = 0.5
        else:
            vol_score = 0.4

        # التحقق من لون الشمعة: إذا كانت حمراء (close < open) أو انخفاض السعر
        price_change = (close_prices[-1] - close_prices[-2]) / close_prices[-2]
        if price_change < -0.01 and vol_score > 0.6:
            vol_score += 0.2   # حجم مرتفع مع انخفاض → ضغط بيع
        elif price_change > 0.01 and vol_score > 0.6:
            vol_score -= 0.1   # حجم مرتفع مع ارتفاع → قد يكون شراء

        return max(0.0, min(1.0, vol_score))

    @staticmethod
    def bearish_volatility(high: List[float], low: List[float], close: List[float]) -> float:
        """تقلبات عالية غالباً تسبق هبوط، خاصة إذا كان السعر قرب الحد العلوي لبولينجر."""
        if len(close) < 20:
            return 0.5
        sma_20 = IndicatorCalculator.sma(close, 20)[-1]
        std_dev = 0
        for i in range(-20, 0):
            std_dev += (close[i] - sma_20) ** 2
        std_dev = math.sqrt(std_dev / 20)
        upper = sma_20 + 2 * std_dev
        lower = sma_20 - 2 * std_dev
        if upper == lower:
            return 0.5

        position = (close[-1] - lower) / (upper - lower)
        # إذا كان السعر قرب الحد العلوي → احتمالية ارتداد هابط
        if position > 0.9:
            return 0.9
        if position > 0.8:
            return 0.7
        if position < 0.2:
            return 0.2   # قرب القاع قد يكون دعم
        return 0.5

    @staticmethod
    def bearish_structure(high: List[float], low: List[float], close: List[float]) -> float:
        """تحديد القمم المحلية: إذا كان السعر قرب قمة حديثة، إشارة بيع."""
        if len(high) < 30:
            return 0.5
        recent_high = max(high[-30:])
        recent_low = min(low[-30:])
        if recent_high == recent_low:
            return 0.5
        position = (close[-1] - recent_low) / (recent_high - recent_low)
        if position > 0.9:
            return 0.9
        if position > 0.8:
            return 0.7
        if position < 0.2:
            return 0.2
        return 0.5

    @staticmethod
    def resistance_levels(high: List[float], low: List[float], close: List[float]) -> float:
        """تحديد مستويات مقاومة قريبة."""
        if len(high) < 40:
            return 0.5
        highs = high[-40:]
        lows = low[-40:]
        resistance_candidates = []

        for i in range(2, len(highs) - 2):
            if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
                resistance_candidates.append(highs[i])

        if not resistance_candidates:
            return 0.5

        current = close[-1]
        closest_resistance = min([r for r in resistance_candidates if r > current], default=None)

        if closest_resistance:
            distance = (closest_resistance - current) / current
            if distance < 0.02:
                return 0.9   # مقاومة قوية جداً قريبة
            if distance < 0.05:
                return 0.7
            if distance < 0.10:
                return 0.5
        return 0.3

    @staticmethod
    def bearish_divergence(high: List[float], close: List[float]) -> float:
        """ divergence هابط: سعر يصنع قمة أعلى و RSI يصنع قمة أقل."""
        if len(high) < 30 or len(close) < 30:
            return 0.5
        rsi_vals = IndicatorCalculator.rsi(close, 14)
        # آخر 14 شمعة
        recent_highs = high[-14:]
        recent_rsi = [r for r in rsi_vals[-14:] if r is not None]
        if len(recent_rsi) < 10:
            return 0.5

        # البحث عن قمة سعرية أعلى وقمة RSI أقل
        price_peak = max(recent_highs)
        price_peak_idx = recent_highs.index(price_peak)
        rsi_at_peak = recent_rsi[price_peak_idx] if price_peak_idx < len(recent_rsi) else 50

        # قارن مع القمة السابقة
        prev_highs = high[-28:-14]
        if prev_highs:
            prev_price_peak = max(prev_highs)
            prev_idx = prev_highs.index(prev_price_peak)
            prev_rsi_vals = IndicatorCalculator.rsi(close[-28:-14], 14)
            prev_rsi_peak = prev_rsi_vals[prev_idx] if prev_idx < len(prev_rsi_vals) and prev_rsi_vals[prev_idx] is not None else 50

            if price_peak > prev_price_peak and rsi_at_peak < prev_rsi_peak:
                return 0.9   # divergence هابط قوي
        return 0.5

    @staticmethod
    def overbought_condition(high: List[float], low: List[float], close: List[float]) -> float:
        """تقييم ظروف ذروة الشراء (RSI > 70 أو السعر فوق Bollinger upper)."""
        score = 0.0
        # RSI
        rsi_vals = IndicatorCalculator.rsi(close, 14)
        last_rsi = rsi_vals[-1] if rsi_vals[-1] is not None else 50
        if last_rsi > 80:
            score += 0.6
        elif last_rsi > 70:
            score += 0.4
        elif last_rsi > 60:
            score += 0.2

        # Bollinger
        if len(close) > 20:
            sma_20 = IndicatorCalculator.sma(close, 20)[-1]
            std_dev = 0
            for i in range(-20, 0):
                std_dev += (close[i] - sma_20) ** 2
            std_dev = math.sqrt(std_dev / 20)
            upper = sma_20 + 2 * std_dev
            if close[-1] > upper:
                score += 0.4
            elif close[-1] > sma_20 + 1.5 * std_dev:
                score += 0.2

        return min(1.0, score)

# ======================
# Signal Processor (معدل لحساب درجة البيع)
# ======================
class SellSignalProcessor:
    @staticmethod
    def calculate_bearish_score(indicator_scores: Dict[str, float]) -> Dict:
        total_weighted = 0.0
        weighted_scores = {}

        for indicator, score in indicator_scores.items():
            weight = AppConfig.INDICATOR_WEIGHTS.get(indicator, 0.1)
            weighted = score * weight
            total_weighted += weighted

            weighted_scores[indicator] = IndicatorScore(
                name=indicator,
                raw_score=score,
                weighted_score=weighted,
                percentage=score * 100,
                weight=weight,
                description=AppConfig.INDICATOR_DESCRIPTIONS.get(indicator, ''),
                color=AppConfig.INDICATOR_COLORS.get(indicator, '#E63946')
            )

        total_percentage = total_weighted * 100
        signal_type = SellSignalProcessor.get_signal_type(total_percentage)
        signal_strength = SellSignalProcessor.get_signal_strength(total_percentage)
        signal_color = SellSignalProcessor.get_signal_color(signal_type)

        return {
            'total_percentage': total_percentage,
            'weighted_scores': weighted_scores,
            'signal_type': signal_type,
            'signal_strength': signal_strength,
            'signal_color': signal_color
        }

    @staticmethod
    def get_signal_type(percentage: float) -> SignalType:
        if percentage >= AppConfig.SIGNAL_THRESHOLDS[SignalType.STRONG_SELL]:
            return SignalType.STRONG_SELL
        if percentage >= AppConfig.SIGNAL_THRESHOLDS[SignalType.SELL]:
            return SignalType.SELL
        if percentage >= AppConfig.SIGNAL_THRESHOLDS[SignalType.NEUTRAL]:
            return SignalType.NEUTRAL
        if percentage >= AppConfig.SIGNAL_THRESHOLDS[SignalType.BUY]:
            return SignalType.BUY
        return SignalType.STRONG_BUY

    @staticmethod
    def get_signal_strength(percentage: float) -> str:
        if percentage >= 85:
            return "Extreme Sell"
        if percentage >= 70:
            return "Strong Sell"
        if percentage >= 55:
            return "Moderate Sell"
        if percentage >= 40:
            return "Weak Sell"
        return "Very Weak Sell"

    @staticmethod
    def get_signal_color(signal_type: SignalType) -> str:
        mapping = {
            SignalType.STRONG_SELL: "danger",
            SignalType.SELL: "warning",
            SignalType.NEUTRAL: "secondary",
            SignalType.BUY: "primary",
            SignalType.STRONG_BUY: "success"
        }
        return mapping.get(signal_type, "secondary")

# ======================
# Notification Manager (نفس الكود مع إشارات بيع)
# ======================
class NotificationManager:
    def __init__(self):
        self.history: List[Notification] = []
        self.max_history = 50
        self.last_notification_time = {}
        self.min_interval = 300  # 5 minutes

    def add(self, notification: Notification):
        self.history.append(notification)
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]

    def get_recent(self, limit: int = 10) -> List[Notification]:
        return self.history[-limit:] if self.history else []

    def should_send(self, coin_symbol: str, percentage: float) -> bool:
        now = datetime.now()
        if coin_symbol in self.last_notification_time:
            delta = now - self.last_notification_time[coin_symbol]
            if delta.total_seconds() < self.min_interval:
                return False

        thresholds = AppConfig.SIGNAL_THRESHOLDS
        # نرسل فقط إذا كانت إشارة بيع (SELL أو STRONG_SELL)
        if percentage >= thresholds[SignalType.STRONG_SELL] or percentage >= thresholds[SignalType.SELL]:
            return True
        return False

    def send_ntfy(self, message: str, title: str = "Crypto Sell Signal", priority: str = "4", tags: str = "chart") -> bool:
        try:
            headers = {
                "Title": title,
                "Priority": priority,
                "Tags": tags,
                "Content-Type": "text/plain; charset=utf-8"
            }
            safe_message = message.encode('ascii', errors='replace').decode('ascii')
            resp = requests.post(
                ExternalAPIConfig.NTFY_URL,
                data=safe_message.encode('utf-8'),
                headers=headers,
                timeout=5
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"NTFY error: {e}")
            return False

    def create_notification(self, coin_signal: CoinSignal) -> Optional[Notification]:
        if not self.should_send(coin_signal.symbol, coin_signal.total_percentage):
            return None

        coin = coin_signal
        signal_type = coin.signal_type

        if signal_type not in [SignalType.SELL, SignalType.STRONG_SELL]:
            return None

        title = f"{signal_type.value} Signal: {coin.name}"
        message = (
            f"{title}\n"
            f"Bearish Strength: {coin.total_percentage:.1f}%\n"
            f"Price: ${coin.current_price:,.2f}\n"
            f"24h Change: {coin.price_change_24h:+.2f}%\n"
            f"Time: {coin.last_updated.strftime('%H:%M')}"
        )

        tags_map = {
            SignalType.STRONG_SELL: "heavy_minus_sign",
            SignalType.SELL: "chart_decreasing"
        }
        tags = tags_map.get(signal_type, "loudspeaker")

        priority_map = {
            SignalType.STRONG_SELL: "5",   # أولوية قصوى للبيع القوي
            SignalType.SELL: "4"
        }
        priority = priority_map.get(signal_type, "4")

        if self.send_ntfy(message, title, priority, tags):
            notification = Notification(
                id=f"{coin.symbol}_{int(datetime.now().timestamp())}",
                timestamp=datetime.now(),
                coin_symbol=coin.symbol,
                coin_name=coin.name,
                message=message,
                notification_type=signal_type.name.lower(),
                signal_strength=coin.total_percentage,
                price=coin.current_price
            )
            self.add(notification)
            self.last_notification_time[coin.symbol] = datetime.now()
            return notification
        return None

# ======================
# Signal Manager (معدل لاستخدام الدوال الجديدة)
# ======================
class SellSignalManager:
    def __init__(self):
        self.signals: Dict[str, CoinSignal] = {}
        self.last_coins_update = None
        self.history: List[Dict] = []
        self.last_update: Optional[datetime] = None
        self.fgi_fetcher = FearGreedFetcher()
        self.notification_manager = NotificationManager()
        self.binance = BinanceClient()
        self.lock = Lock()
        self.fear_greed_index = 50
        self.fear_greed_score = 0.5

    def update_coins_list(self):
        now = datetime.now()
        if not self.last_coins_update or (now - self.last_coins_update).seconds > 3600:
            new_coins = AppConfig.get_top_coins(15)
            if new_coins:
                AppConfig.COINS = new_coins
                self.last_coins_update = now
                logger.info(f"🔄 تم تحديث قائمة العملات: {len(new_coins)} عملة")

    def update_all(self) -> bool:
        with self.lock:
            self.update_coins_list()
            logger.info(f"🔄 Updating {len(AppConfig.COINS)} coins for SELL signals...")
            success_count = 0
            self.fear_greed_score, self.fear_greed_index = self.fgi_fetcher.get()

            for coin in AppConfig.COINS:
                if not coin.enabled:
                    continue
                try:
                    signal = self._process_coin(coin)
                    if signal and signal.is_valid:
                        self.signals[coin.symbol] = signal
                        success_count += 1
                        self.notification_manager.create_notification(signal)
                except Exception as e:
                    logger.error(f"Error on {coin.symbol}: {e}")

            self.last_update = datetime.now()
            self._save_history()
            logger.info(f"✅ Updated {success_count}/{len(AppConfig.COINS)}")
            return success_count > 0

    def _process_coin(self, coin: CoinConfig) -> Optional[CoinSignal]:
        ohlcv = self.binance.fetch_ohlcv(coin.symbol, '15m', AppConfig.MAX_CANDLES)
        if not ohlcv or len(ohlcv) < 50:
            return None

        closes = [c[4] for c in ohlcv]
        highs = [c[2] for c in ohlcv]
        lows = [c[3] for c in ohlcv]
        volumes = [c[5] for c in ohlcv]

        ticker = self.binance.fetch_ticker(coin.symbol)
        if not ticker:
            return None

        current_price = ticker['last']
        change_24h = ticker.get('percentage', 0.0)
        high_24h = ticker.get('high', 0.0)
        low_24h = ticker.get('low', 0.0)
        volume_24h = ticker.get('quoteVolume', 0.0)

        # حساب الدرجات (كلها مصممة لتعطي قيم عالية في الحالات الهابطة)
        scores = {
            IndicatorType.TREND.value: IndicatorCalculator.bearish_trend_strength(closes),
            IndicatorType.MOMENTUM.value: IndicatorCalculator.bearish_momentum(closes),
            IndicatorType.VOLUME.value: IndicatorCalculator.selling_volume(volumes, closes),
            IndicatorType.VOLATILITY.value: IndicatorCalculator.bearish_volatility(highs, lows, closes),
            IndicatorType.SENTIMENT.value: self.fear_greed_score,
            IndicatorType.STRUCTURE.value: IndicatorCalculator.bearish_structure(highs, lows, closes),
            IndicatorType.SUPPORT_RESISTANCE.value: IndicatorCalculator.resistance_levels(highs, lows, closes),
            IndicatorType.BEARISH_DIVERGENCE.value: IndicatorCalculator.bearish_divergence(highs, closes),
            IndicatorType.OVERBOUGHT.value: IndicatorCalculator.overbought_condition(highs, lows, closes)
        }

        result = SellSignalProcessor.calculate_bearish_score(scores)

        return CoinSignal(
            symbol=coin.symbol,
            name=coin.name,
            current_price=current_price,
            price_change_24h=change_24h,
            high_24h=high_24h,
            low_24h=low_24h,
            volume_24h=volume_24h,
            total_percentage=result['total_percentage'],
            signal_type=result['signal_type'],
            signal_strength=result['signal_strength'],
            signal_color=result['signal_color'],
            indicator_scores=result['weighted_scores'],
            last_updated=datetime.now(),
            fear_greed_value=self.fear_greed_index,
            is_valid=True
        )

    def _save_history(self):
        entry = {
            'timestamp': datetime.now(),
            'signals': {s: self.signals[s].total_percentage for s in self.signals},
            'fgi': self.fear_greed_index
        }
        self.history.append(entry)
        if len(self.history) > 50:
            self.history = self.history[-50:]

    def get_coins_data(self) -> List[Dict]:
        data = []
        for coin in AppConfig.COINS:
            signal = self.signals.get(coin.symbol)
            if signal and signal.is_valid:
                data.append(self._format_coin(signal))
            else:
                data.append(self._default_coin(coin))
        data.sort(key=lambda x: x['total_percentage'], reverse=True)  # الأعلى (أكثر بيعاً) أولاً
        return data

    def _format_coin(self, s: CoinSignal) -> Dict:
        indicators = []
        for k, v in s.indicator_scores.items():
            indicators.append({
                'name': k,
                'display_name': AppConfig.INDICATOR_DISPLAY_NAMES.get(k, k),
                'description': AppConfig.INDICATOR_DESCRIPTIONS.get(k, ''),
                'raw_score': v.raw_score * 100,
                'percentage': v.percentage,
                'color': v.color,
                'weight': v.weight * 100
            })
        return {
            'symbol': s.symbol,
            'name': s.name,
            'current_price': s.current_price,
            'formatted_price': self._format_number(s.current_price),
            'price_change_24h': s.price_change_24h,
            'formatted_24h_change': self._format_percentage(s.price_change_24h),
            'volume_24h': s.volume_24h,
            'formatted_volume_24h': self._format_number(s.volume_24h),
            'total_percentage': s.total_percentage,
            'signal_type': s.signal_type.value,
            'signal_strength': s.signal_strength,
            'signal_color': s.signal_color,
            'indicators': indicators,
            'last_updated_str': self._format_time_delta(s.last_updated),
            'fear_greed_value': s.fear_greed_value,
            'is_valid': True
        }

    def _default_coin(self, coin: CoinConfig) -> Dict:
        return {
            'symbol': coin.symbol,
            'name': coin.name,
            'current_price': 0,
            'formatted_price': '0',
            'price_change_24h': 0,
            'formatted_24h_change': '0.00%',
            'volume_24h': 0,
            'formatted_volume_24h': '0',
            'total_percentage': 50,
            'signal_type': SignalType.NEUTRAL.value,
            'signal_strength': 'Unavailable',
            'signal_color': 'secondary',
            'indicators': [],
            'last_updated_str': 'Unknown',
            'fear_greed_value': self.fear_greed_index,
            'is_valid': False
        }

    @staticmethod
    def _format_number(v: float) -> str:
        try:
            if v >= 1_000_000:
                return f"{v/1_000_000:.2f}M"
            if v >= 1_000:
                return f"{v/1_000:.2f}K"
            return f"{v:.2f}"
        except:
            return "0"

    @staticmethod
    def _format_percentage(v: float) -> str:
        try:
            return f"{v:+.2f}%" if v else "0.00%"
        except:
            return "0.00%"

    @staticmethod
    def _format_time_delta(dt: datetime) -> str:
        if not dt:
            return "Unknown"
        delta = datetime.now() - dt
        if delta.days > 0:
            return f"{delta.days} day(s) ago"
        if delta.seconds >= 3600:
            return f"{delta.seconds//3600} hour(s) ago"
        if delta.seconds >= 60:
            return f"{delta.seconds//60} minute(s) ago"
        return "Just now"

    def get_stats(self) -> Dict:
        coins = self.get_coins_data()
        valid = [c for c in coins if c['is_valid']]
        percentages = [c['total_percentage'] for c in valid]

        strong_sell = sum(1 for c in valid if c['total_percentage'] >= 70)
        sell = sum(1 for c in valid if 60 <= c['total_percentage'] < 70)
        neutral = sum(1 for c in valid if 40 < c['total_percentage'] < 60)
        buy = sum(1 for c in valid if 20 < c['total_percentage'] <= 40)
        strong_buy = sum(1 for c in valid if c['total_percentage'] <= 20)

        avg = sum(percentages) / len(percentages) if percentages else 50

        return {
            'total_coins': len(AppConfig.COINS),
            'updated_coins': len(valid),
            'avg_signal': avg,
            'strong_sell_signals': strong_sell,
            'sell_signals': sell,
            'neutral_signals': neutral,
            'buy_signals': buy,
            'strong_buy_signals': strong_buy,
            'last_update_str': self._format_time_delta(self.last_update) if self.last_update else 'Unknown',
            'total_notifications': len(self.notification_manager.history),
            'fear_greed_index': self.fear_greed_index,
            'system_status': 'healthy' if len(valid) >= len(AppConfig.COINS) * 0.7 else 'warning'
        }

# ======================
# Background Updater
# ======================
def background_updater():
    while True:
        try:
            signal_manager.update_all()
            time.sleep(AppConfig.UPDATE_INTERVAL)
        except Exception as e:
            logger.error(f"Update error: {e}")
            time.sleep(60)

# ======================
# Flask App
# ======================
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'crypto-sell-secret-2026')
signal_manager = SellSignalManager()
start_time = time.time()

updater_thread = threading.Thread(target=background_updater, daemon=True)
updater_thread.start()
signal_manager.update_all()

# ======================
# Context Processor
# ======================
@app.context_processor
def utility_processor():
    def signal_color_to_css(color_name):
        mapping = {
            'danger': 'var(--danger)',
            'warning': 'var(--warning)',
            'secondary': 'var(--gray)',
            'primary': 'var(--primary)',
            'success': 'var(--success)'
        }
        return mapping.get(color_name, 'var(--secondary)')
    return dict(
        signal_color_to_css=signal_color_to_css,
        get_indicator_color=lambda k: AppConfig.INDICATOR_COLORS.get(k, '#E63946'),
        get_indicator_display_name=lambda k: AppConfig.INDICATOR_DISPLAY_NAMES.get(k, k)
    )

# ======================
# Routes (نفس المسارات لكن البيانات مختلفة)
# ======================
@app.route('/')
def index():
    coins = signal_manager.get_coins_data()
    stats = signal_manager.get_stats()
    notifications = signal_manager.notification_manager.get_recent(10)
    return render_template(
        'index_sell.html',   # قالب خاص بالبيع (يمكن استخدام نفس القالب مع تغيير العناوين)
        coins=coins,
        stats=stats,
        notifications=notifications,
        indicator_weights=AppConfig.INDICATOR_WEIGHTS
    )

@app.route('/api/signals')
def api_signals():
    return jsonify({
        'status': 'success',
        'data': signal_manager.get_coins_data(),
        'timestamp': datetime.now().isoformat()
    })

@app.route('/api/update', methods=['POST'])
def manual_update():
    success = signal_manager.update_all()
    return jsonify({
        'status': 'success' if success else 'warning',
        'message': 'Update completed',
        'timestamp': datetime.now().isoformat()
    })

@app.route('/api/health')
def health():
    now = datetime.now()
    last = signal_manager.last_update
    status = 'healthy'
    if last and (now - last).total_seconds() > 600:
        status = 'warning'
    return jsonify({
        'status': status,
        'last_update': last.isoformat() if last else None,
        'coins': len(signal_manager.signals),
        'uptime': time.time() - start_time,
        'fear_greed': signal_manager.fear_greed_index,
        'notifications': len(signal_manager.notification_manager.history)
    })

@app.route('/api/notifications')
def get_notifications():
    limit = request.args.get('limit', 10, type=int)
    nots = signal_manager.notification_manager.get_recent(limit)
    return jsonify({
        'notifications': [asdict(n) for n in nots],
        'total': len(signal_manager.notification_manager.history)
    })

@app.route('/api/test_ntfy')
def test_ntfy():
    msg = "Test sell notification - System is working properly"
    success = signal_manager.notification_manager.send_ntfy(msg, "Sell Test", "4", "test_tube")
    return jsonify({'success': success})

@app.route('/api/indicator_weights')
def indicator_weights():
    return jsonify({
        'weights': AppConfig.INDICATOR_WEIGHTS,
        'display_names': AppConfig.INDICATOR_DISPLAY_NAMES,
        'colors': AppConfig.INDICATOR_COLORS
    })

def send_startup_notification():
    try:
        msg = (
            f"Crypto SELL Signal Analyzer Started\n"
            f"Version: 3.5.2-sell (English notifications)\n"
            f"Tracking {len(AppConfig.COINS)} coins\n"
            f"Update interval: {AppConfig.UPDATE_INTERVAL//60} minutes"
        )
        signal_manager.notification_manager.send_ntfy(msg, "Sell System Started", "4", "rocket")
    except Exception as e:
        logger.error(f"Startup notification error: {e}")

def delayed_startup():
    time.sleep(5)
    send_startup_notification()

threading.Thread(target=delayed_startup, daemon=True).start()

if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("🚀 Crypto SELL Signal Analyzer v3.5.2 (High Sensitivity)")
    logger.info(f"📊 Coins: {len(AppConfig.COINS)}")
    logger.info(f"🔄 Update every {AppConfig.UPDATE_INTERVAL//60} minutes")
    logger.info(f"📢 NTFY: {ExternalAPIConfig.NTFY_URL}")
    logger.info("=" * 50)

    port = int(os.environ.get('PORT', 5001))   # تشغيل على منفذ مختلف
    app.run(host='0.0.0.0', port=port)
