"""
Crypto Signal Analyzer Bot - Simplified SELL Edition
Version 5.0.1 - Enhanced: real indicators even when BTC filter off
All notifications in English, no emojis.
"""

import os
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
# Logging setup
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
# Basic data structures
# ======================
class SignalType(Enum):
    STRONG_BUY = "STRONG BUY"
    BUY = "BUY"
    NEUTRAL = "NEUTRAL"
    SELL = "SELL"
    STRONG_SELL = "STRONG SELL"

class IndicatorType(Enum):
    TREND = "trend"
    RSI = "rsi"
    VOLUME = "volume"
    STRUCTURE = "structure"

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
    raw_score: float      # 0 or 1
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
    total_percentage: float   # bearish score (0-100)
    signal_type: SignalType
    signal_strength: str
    signal_color: str
    indicator_scores: Dict[str, IndicatorScore]
    last_updated: datetime
    fear_greed_value: int     # for display
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
# Application Configuration (simplified)
# ======================
class AppConfig:
    COINS = [
        CoinConfig("BTC/USDT", "Bitcoin", "BTC", "USDT"),
        CoinConfig("ETH/USDT", "Ethereum", "ETH", "USDT"),
        CoinConfig("BNB/USDT", "Binance Coin", "BNB", "USDT"),
        CoinConfig("SOL/USDT", "Solana", "SOL", "USDT"),
        CoinConfig("XRP/USDT", "Ripple", "XRP", "USDT"),
        CoinConfig("LTC/USDT", "Litecoin", "LTC", "USDT"),
    ]

    SIGNAL_THRESHOLDS = {
        SignalType.STRONG_SELL: 3,
        SignalType.SELL: 2,
        SignalType.NEUTRAL: 1,
    }

    UPDATE_INTERVAL = 120
    MAX_CANDLES = 100

    INDICATOR_COLORS = {
        IndicatorType.TREND.value: '#E63946',
        IndicatorType.RSI.value: '#F4A261',
        IndicatorType.VOLUME.value: '#3BB273',
        IndicatorType.STRUCTURE.value: '#8F2D56',
    }

    INDICATOR_DISPLAY_NAMES = {
        IndicatorType.TREND.value: 'Bearish Trend (EMA 50/200)',
        IndicatorType.RSI.value: 'Overbought RSI (>65)',
        IndicatorType.VOLUME.value: 'Selling Volume Surge',
        IndicatorType.STRUCTURE.value: 'Support Break',
    }

    INDICATOR_DESCRIPTIONS = {
        IndicatorType.TREND.value: 'Price below EMA50 and EMA200',
        IndicatorType.RSI.value: 'RSI above 65 (strong above 75)',
        IndicatorType.VOLUME.value: 'Volume spike while price drops',
        IndicatorType.STRUCTURE.value: 'Price breaks recent support level',
    }

# ======================
# External APIs config
# ======================
class ExternalAPIConfig:
    BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', '')
    BINANCE_SECRET_KEY = os.environ.get('BINANCE_SECRET_KEY', '')
    NTFY_TOPIC = os.environ.get('NTFY_TOPIC', 'crypto_sell_alerts')
    NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
    FGI_API_URL = "https://api.alternative.me/fng/"
    REQUEST_TIMEOUT = 10
    MAX_RETRIES = 2

# ======================
# Binance Client
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

    def get_current_price(self, symbol: str) -> float:
        ticker = self.fetch_ticker(symbol)
        return ticker['last'] if ticker else 0.0

# ======================
# Fear & Greed Index Fetcher
# ======================
class FearGreedFetcher:
    def __init__(self):
        self.last_value = 50
        self.last_update = None
        self.cache_ttl = 300

    def get(self) -> Tuple[int, int]:   # return (value, raw_value)
        now = datetime.now()
        if self.last_update and (now - self.last_update).total_seconds() < self.cache_ttl:
            return self.last_value, self.last_value

        try:
            resp = requests.get(ExternalAPIConfig.FGI_API_URL, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if 'data' in data and data['data']:
                    value = int(data['data'][0]['value'])
                    self.last_value = value
                    self.last_update = now
                    return value, value
        except Exception as e:
            logger.error(f"FGI fetch error: {e}")

        return self.last_value, self.last_value

# ======================
# Indicator Calculators (simplified)
# ======================
class IndicatorCalculator:
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

    @staticmethod
    def bearish_trend(close_prices: List[float]) -> int:
        if len(close_prices) < 200:
            return 0
        try:
            ema50 = IndicatorCalculator.ema(close_prices, 50)[-1]
            ema200 = IndicatorCalculator.ema(close_prices, 200)[-1]
            current = close_prices[-1]
            if current < ema50 < ema200:
                return 1
            return 0
        except:
            return 0

    @staticmethod
    def overbought_rsi(close_prices: List[float]) -> int:
        if len(close_prices) < 14:
            return 0
        try:
            rsi_vals = IndicatorCalculator.rsi(close_prices, 14)
            last_rsi = rsi_vals[-1] if rsi_vals[-1] is not None else 50
            if last_rsi > 65:
                return 1
            return 0
        except:
            return 0

    @staticmethod
    def selling_volume(volumes: List[float], close_prices: List[float]) -> int:
        if len(volumes) < 20 or len(close_prices) < 2:
            return 0
        try:
            avg_vol = sum(volumes[-20:]) / 20
            current_vol = volumes[-1]
            price_change = (close_prices[-1] - close_prices[-2]) / close_prices[-2]
            if current_vol > avg_vol and price_change < 0:
                return 1
            return 0
        except:
            return 0

    @staticmethod
    def support_break(highs: List[float], lows: List[float], close_prices: List[float]) -> int:
        if len(lows) < 20:
            return 0
        try:
            recent_lows = lows[-20:]
            support = min(recent_lows)
            if close_prices[-1] < support:
                return 1
            return 0
        except:
            return 0

# ======================
# Signal Processor
# ======================
class SellSignalProcessor:
    @staticmethod
    def calculate_bearish_score(indicator_scores: Dict[str, int]) -> Dict:
        total_score = sum(indicator_scores.values())
        total_percentage = (total_score / len(indicator_scores)) * 100 if indicator_scores else 0

        if total_score >= AppConfig.SIGNAL_THRESHOLDS[SignalType.STRONG_SELL]:
            signal_type = SignalType.STRONG_SELL
        elif total_score >= AppConfig.SIGNAL_THRESHOLDS[SignalType.SELL]:
            signal_type = SignalType.SELL
        else:
            signal_type = SignalType.NEUTRAL

        signal_strength = SellSignalProcessor.get_signal_strength(total_score)
        signal_color = SellSignalProcessor.get_signal_color(signal_type)

        weighted_scores = {}
        for name, raw in indicator_scores.items():
            weighted_scores[name] = IndicatorScore(
                name=name,
                raw_score=raw,
                weighted_score=raw,
                percentage=raw * 100,
                weight=1.0,
                description=AppConfig.INDICATOR_DESCRIPTIONS.get(name, ''),
                color=AppConfig.INDICATOR_COLORS.get(name, '#E63946')
            )

        return {
            'total_percentage': total_percentage,
            'weighted_scores': weighted_scores,
            'signal_type': signal_type,
            'signal_strength': signal_strength,
            'signal_color': signal_color
        }

    @staticmethod
    def get_signal_strength(total_score: int) -> str:
        if total_score >= 3:
            return "Extreme Sell"
        if total_score == 2:
            return "Moderate Sell"
        return "Weak or No Sell"

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
# Notification Manager
# ======================
class NotificationManager:
    def __init__(self):
        self.history: List[Notification] = []
        self.max_history = 50
        self.last_notification_time = {}
        self.min_interval = 1800

    def add(self, notification: Notification):
        self.history.append(notification)
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]

    def get_recent(self, limit: int = 10) -> List[Notification]:
        return self.history[-limit:] if self.history else []

    def should_send(self, coin_symbol: str, total_score: int, signal_type: SignalType) -> bool:
        if signal_type not in [SignalType.SELL, SignalType.STRONG_SELL]:
            return False
        now = datetime.now()
        if coin_symbol in self.last_notification_time:
            delta = now - self.last_notification_time[coin_symbol]
            if delta.total_seconds() < self.min_interval:
                return False
        return True

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
        total_score = int(round(coin_signal.total_percentage / 100 * len(AppConfig.INDICATOR_DISPLAY_NAMES)))
        if not self.should_send(coin_signal.symbol, total_score, coin_signal.signal_type):
            return None

        title = f"{coin_signal.signal_type.value} Signal: {coin_signal.name}"
        message = (
            f"{title}\n"
            f"Bearish Score: {total_score}/4\n"
            f"Price: ${coin_signal.current_price:,.2f}\n"
            f"24h Change: {coin_signal.price_change_24h:+.2f}%\n"
            f"Time: {coin_signal.last_updated.strftime('%H:%M')}"
        )

        tags_map = {
            SignalType.STRONG_SELL: "heavy_minus_sign",
            SignalType.SELL: "chart_decreasing"
        }
        tags = tags_map.get(coin_signal.signal_type, "loudspeaker")

        priority_map = {
            SignalType.STRONG_SELL: "5",
            SignalType.SELL: "4"
        }
        priority = priority_map.get(coin_signal.signal_type, "4")

        if self.send_ntfy(message, title, priority, tags):
            notification = Notification(
                id=f"{coin_signal.symbol}_{int(datetime.now().timestamp())}",
                timestamp=datetime.now(),
                coin_symbol=coin_signal.symbol,
                coin_name=coin_signal.name,
                message=message,
                notification_type=coin_signal.signal_type.name.lower(),
                signal_strength=coin_signal.total_percentage,
                price=coin_signal.current_price
            )
            self.add(notification)
            self.last_notification_time[coin_signal.symbol] = datetime.now()
            return notification
        return None

# ======================
# Signal Manager (SELL)
# ======================
class SellSignalManager:
    def __init__(self):
        self.signals: Dict[str, CoinSignal] = {}
        self.history: List[Dict] = []
        self.last_update: Optional[datetime] = None
        self.binance = BinanceClient()
        self.lock = Lock()
        self.notification_manager = NotificationManager()
        self.btc_bearish = False
        self.fgi_fetcher = FearGreedFetcher()
        self.fear_greed_index = 50

    def check_btc_trend(self) -> bool:
        try:
            ohlcv = self.binance.fetch_ohlcv("BTC/USDT", '15m', 200)
            if not ohlcv or len(ohlcv) < 200:
                return False
            closes = [c[4] for c in ohlcv]
            ema50 = IndicatorCalculator.ema(closes, 50)[-1]
            ema200 = IndicatorCalculator.ema(closes, 200)[-1]
            current = closes[-1]
            if current < ema50 < ema200:
                return True
            return False
        except Exception as e:
            logger.error(f"Error checking BTC trend: {e}")
            return False

    def update_all(self) -> bool:
        with self.lock:
            logger.info(f"Updating {len(AppConfig.COINS)} coins for SELL signals...")
            self.btc_bearish = self.check_btc_trend()
            logger.info(f"BTC bearish trend: {self.btc_bearish}")

            # Update Fear & Greed for display
            fgi_raw, self.fear_greed_index = self.fgi_fetcher.get()

            success_count = 0
            for coin in AppConfig.COINS:
                if not coin.enabled:
                    continue
                try:
                    signal = self._process_coin(coin)
                    if signal and signal.is_valid:
                        self.signals[coin.symbol] = signal
                        success_count += 1
                        if self.btc_bearish:
                            self.notification_manager.create_notification(signal)
                except Exception as e:
                    logger.error(f"Error on {coin.symbol}: {e}")

            self.last_update = datetime.now()
            self._save_history()
            logger.info(f"Updated {success_count}/{len(AppConfig.COINS)}")
            return success_count > 0

    def _process_coin(self, coin: CoinConfig) -> Optional[CoinSignal]:
        ohlcv = self.binance.fetch_ohlcv(coin.symbol, '15m', AppConfig.MAX_CANDLES)
        if not ohlcv or len(ohlcv) < AppConfig.MAX_CANDLES:
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

        # Calculate real indicator scores (0/1)
        scores = {
            IndicatorType.TREND.value: IndicatorCalculator.bearish_trend(closes),
            IndicatorType.RSI.value: IndicatorCalculator.overbought_rsi(closes),
            IndicatorType.VOLUME.value: IndicatorCalculator.selling_volume(volumes, closes),
            IndicatorType.STRUCTURE.value: IndicatorCalculator.support_break(highs, lows, closes),
        }

        # Real total percentage based on active indicators
        real_total_percentage = (sum(scores.values()) / len(scores)) * 100

        # Determine final signal based on BTC filter
        if self.btc_bearish:
            result = SellSignalProcessor.calculate_bearish_score(scores)
            total_percentage = result['total_percentage']
            signal_type = result['signal_type']
            signal_strength = result['signal_strength']
            signal_color = result['signal_color']
        else:
            # BTC not bearish: keep real scores and percentage but force NEUTRAL signal
            total_percentage = real_total_percentage
            signal_type = SignalType.NEUTRAL
            signal_strength = "Neutral (BTC not bearish)"
            signal_color = "secondary"
            # scores remain unchanged; they will be displayed as real

        # Create IndicatorScore objects for display (using real raw scores)
        indicator_scores = {}
        for name, raw in scores.items():
            indicator_scores[name] = IndicatorScore(
                name=name,
                raw_score=raw,
                weighted_score=raw,
                percentage=raw * 100,
                weight=1.0,
                description=AppConfig.INDICATOR_DESCRIPTIONS.get(name, ''),
                color=AppConfig.INDICATOR_COLORS.get(name, '#E63946')
            )

        return CoinSignal(
            symbol=coin.symbol,
            name=coin.name,
            current_price=current_price,
            price_change_24h=change_24h,
            high_24h=high_24h,
            low_24h=low_24h,
            volume_24h=volume_24h,
            total_percentage=total_percentage,
            signal_type=signal_type,
            signal_strength=signal_strength,
            signal_color=signal_color,
            indicator_scores=indicator_scores,
            last_updated=datetime.now(),
            fear_greed_value=self.fear_greed_index,
            is_valid=True
        )

    def _save_history(self):
        entry = {
            'timestamp': datetime.now(),
            'signals': {s: self.signals[s].total_percentage for s in self.signals},
            'btc_bearish': self.btc_bearish,
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
        data.sort(key=lambda x: x['total_percentage'], reverse=True)
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
            'total_percentage': 0,
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

        strong_sell = sum(1 for c in valid if c['total_percentage'] >= 75)
        sell = sum(1 for c in valid if 50 <= c['total_percentage'] < 75)
        neutral = sum(1 for c in valid if 25 <= c['total_percentage'] < 50)
        buy = sum(1 for c in valid if 0 < c['total_percentage'] < 25)
        strong_buy = sum(1 for c in valid if c['total_percentage'] == 0)

        avg = sum(percentages) / len(percentages) if percentages else 0

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
            'btc_bearish': self.btc_bearish,
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
# Routes
# ======================
@app.route('/')
def index():
    coins = signal_manager.get_coins_data()
    stats = signal_manager.get_stats()
    notifications = signal_manager.notification_manager.get_recent(10)
    return render_template(
        'index_sell.html',
        coins=coins,
        stats=stats,
        notifications=notifications,
        indicator_weights={}
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
        'btc_bearish': signal_manager.btc_bearish,
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

def send_startup_notification():
    try:
        msg = (
            f"Crypto SELL Signal Analyzer Started (Simplified Edition)\n"
            f"Version: 5.0.1\n"
            f"Tracking {len(AppConfig.COINS)} coins\n"
            f"Update interval: {AppConfig.UPDATE_INTERVAL//60} minutes\n"
            f"Indicators: Trend, RSI, Volume, Structure"
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
    logger.info("Crypto SELL Signal Analyzer v5.0.1 (Enhanced Bearish Edition)")
    logger.info(f"Coins: {len(AppConfig.COINS)}")
    logger.info(f"Update every {AppConfig.UPDATE_INTERVAL//60} minutes")
    logger.info(f"NTFY: {ExternalAPIConfig.NTFY_URL}")
    logger.info("Indicators: Trend, RSI, Volume, Structure")
    logger.info("=" * 50)

    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)
