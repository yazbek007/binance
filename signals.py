# ====================== المكتبات ======================
import os
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
import warnings
import logging
from tenacity import retry, stop_after_attempt, wait_exponential
from dotenv import load_dotenv
import asyncio
import httpx
import time
from fastapi import FastAPI, HTTPException
import uvicorn
from typing import Dict, Any, List, Optional

warnings.filterwarnings('ignore')
load_dotenv()

# ====================== إعدادات FastAPI ======================
app = FastAPI(title="Crypto BUY/SELL Signals Scanner", version="3.0.0")

# ====================== إعدادات التفعيل / الإلغاء ======================
ENABLE_TRAILING_STOP = False
ENABLE_DYNAMIC_POSITION_SIZING = False
ENABLE_MARKET_REGIME_FILTER = True
ENABLE_ATR_SL_TP = False
ENABLE_SUPPORT_RESISTANCE_FILTER = True
ENABLE_TIME_FILTER = True
ENABLE_WALK_FORWARD = False
ENABLE_LOGGING = True
ENABLE_DETAILED_REPORT = False
ENABLE_FUTURES_TRADING = True
ENABLE_SIGNAL_SENDING = True
ENABLE_NTFY_ALERTS = True  # تغيير من TELEGRAM إلى NTFY
ENABLE_HEARTBEAT = True

# ====================== إعدادات البوت المنفذ ======================
EXECUTOR_BOT_URL = os.getenv("EXECUTOR_BOT_URL", "https://your-executor-bot.onrender.com")
EXECUTOR_BOT_API_KEY = os.getenv("EXECUTOR_BOT_API_KEY", "")
EXECUTE_TRADES = os.getenv("EXECUTE_TRADES", "false").lower() == "true"

# ====================== إعدادات المسح ======================
SCAN_INTERVAL = 600  # 10 دقائق بين كل فحص
HEARTBEAT_INTERVAL = 7200  # 2 ساعة بين كل نبضة
CONFIDENCE_THRESHOLD = 7.5  # عتبة الثقة للإشارات (من 1-10)

# ====================== دالة مساعدة ======================
def interval_to_hours(interval):
    mapping = {
        '1m': 1/60, '3m': 3/60, '5m': 5/60, '15m': 15/60,
        '30m': 30/60, '1h': 1, '2h': 2, '4h': 4, '6h': 6,
        '8h': 8, '12h': 12, '1d': 24, '3d': 72, '1w': 168, '1M': 720
    }
    return mapping.get(interval, 4)

# ====================== الإعدادات الأساسية ======================
TRADE_CONFIG = {
    'symbols': ["BTCUSDT", "SOLUSDT", "ADAUSDT", "BNBUSDT", "ETHUSDT", "LINKUSDT"],
    'timeframe': '4h',
    'initial_balance': 200,
    'leverage': 10,
    'base_stop_loss': 0.015,
    'base_take_profit': 0.045,
    'base_position_size': 0.25,
    'max_positions': 4,
    'paper_trading': True,
    'use_trailing_stop': ENABLE_TRAILING_STOP,
    'trailing_stop_percent': 0.01,
    'trailing_activation': 0.015,
    'max_trade_duration': 48,
    'atr_multiplier_sl': 1.5,
    'atr_multiplier_tp': 3.0,
    'atr_period': 14,
    'support_resistance_window': 20,
    'peak_hours': [0, 4, 8, 12, 16, 20],
    'min_volume_ratio': 1.2,
    'market_type': 'FUTURES',
    'margin_mode': 'ISOLATED'
}

INDICATOR_CONFIG = {
    'rsi_period': 21,
    'rsi_overbought': 70,
    'rsi_oversold': 30,
    'ema_fast': 9,
    'ema_slow': 21,
    'ema_trend': 50,
    'ema_regime': 200,
    'macd_fast': 12,
    'macd_slow': 26,
    'macd_signal': 9
}

SIGNAL_CONFIG = {
    'min_conditions': 3,
    'use_trend_filter': True,
    'use_volume_filter': True,
    'prevent_conflicts': True,
    'min_signal_strength': 7.5,
    'max_signal_strength': 10,
    'require_trend_confirmation': True,
    'min_volume_ratio': 1.0
}

# ====================== إعداد التسجيل ======================
if ENABLE_LOGGING:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | %(message)s',
        handlers=[logging.StreamHandler()]
    )
logger = logging.getLogger(__name__) if ENABLE_LOGGING else None

# ====================== إحصائيات النظام ======================
system_stats = {
    "start_time": time.time(),
    "total_scans": 0,
    "total_signals_sent": 0,
    "last_scan_time": None,
    "executor_connected": False,
    "last_signal_time": None,
    "last_heartbeat": None,
    "signals_by_symbol": {},
    "buy_signals": 0,
    "sell_signals": 0,
    "heartbeats_sent": 0
}

# ====================== دوال التسجيل الآمن ======================
def safe_log_info(message: str):
    """تسجيل آمن للمعلومات"""
    try:
        if ENABLE_LOGGING:
            logger.info(message)
    except Exception as e:
        print(f"خطأ في التسجيل: {e} - الرسالة: {message}")

def safe_log_error(message: str):
    """تسجيل آمن للأخطاء"""
    try:
        if ENABLE_LOGGING:
            logger.error(message)
    except Exception as e:
        print(f"خطأ في تسجيل الخطأ: {e} - الرسالة: {message}")

# ====================== إشعارات NTFY فقط ======================
class NtfyNotifier:
    """كلاس الإشعارات باستخدام ntfy فقط"""
    
    def __init__(self):
        self.ntfy_topic = os.getenv("NTFY_TOPIC", "").strip()
        self.enable_ntfy = bool(self.ntfy_topic) and ENABLE_NTFY_ALERTS
        
        if not self.enable_ntfy:
            safe_log_info("إشعارات ntfy معطلة (تحقق من NTFY_TOPIC)")
        else:
            safe_log_info(f"إشعارات ntfy مفعلة (topic: {self.ntfy_topic[:8]}...)")
    
    def _send_ntfy(self, message: str, title: str = "إشعار", priority: int = 3, tags: str = "") -> bool:
        """إرسال إلى ntfy"""
        if not self.enable_ntfy:
            return False
            
        url = f"https://ntfy.sh/{self.ntfy_topic}"
        headers = {
            "Title": title,
            "Priority": str(priority),
        }
        if tags:
            headers["Tags"] = tags
            
        try:
            r = requests.post(url, data=message.encode('utf-8'), headers=headers, timeout=8)
            success = r.status_code in (200, 202)
            if success:
                safe_log_info(f"[ntfy] تم الإرسال → {title}")
            else:
                safe_log_warning(f"[ntfy] فشل {r.status_code}: {r.text[:80]}")
            return success
        except Exception as e:
            safe_log_error(f"[ntfy] خطأ: {e}")
            return False
    
    async def send_signal_alert(self, signal_data: dict) -> bool:
        """إرسال إشعار إشارة تداول"""
        action = signal_data.get('action', 'unknown')
        symbol = signal_data.get('symbol', 'غير معروف')
        price = signal_data.get('price', '?')
        strength = signal_data.get('analysis', {}).get('signal_strength', '?')
        reason = signal_data.get('reason', '')
        
        if action == 'SELL':
            title = f"إشارة بيع - {symbol}"
            tags = "rotating_light,chart_decreasing"
            emoji = "🔴"
        elif action == 'BUY':
            title = f"إشارة شراء - {symbol}"
            tags = "green_circle,chart_increasing"
            emoji = "🟢"
        else:
            title = f"إشارة - {symbol}"
            tags = "warning"
            emoji = "⚠️"
        
        message = (
            f"{emoji} {title}\n"
            f"السعر: {price:.2f}\n"
            f"قوة: {strength}/10\n"
            f"الوقت: {datetime.now().strftime('%H:%M:%S')}"
        )
        if reason:
            message += f"\nسبب: {reason[:100]}..."
        
        priority = 5 if strength and float(strength) >= 8 else 4
        
        return self._send_ntfy(
            message=message,
            title=title,
            priority=priority,
            tags=tags
        )
    
    async def send_heartbeat(self) -> bool:
        """إرسال نبضة النظام"""
        message = f"Heartbeat • ماسح الإشارات يعمل • {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        
        return self._send_ntfy(
            message=message,
            title="نبضة النظام",
            priority=2,
            tags="green_circle,clock"
        )
    
    async def send_startup_message(self) -> bool:
        """إرسال رسالة بدء التشغيل"""
        message = (
            f"بدء تشغيل ماسح إشارات البيع والشراء\n"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"الإشعارات النشطة: ntfy فقط"
        )
        
        return self._send_ntfy(
            message=message,
            title="بدء التشغيل",
            priority=3,
            tags="rocket,gear"
        )

# ====================== عميل البوت المنفذ ======================
class ExecutorBotClient:
    """عميل للتواصل مع بوت التنفيذ"""
    
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.api_key = api_key
        self.client = httpx.AsyncClient(timeout=30.0)

    async def send_trade_signal(self, signal_data: Dict[str, Any]) -> bool:
        """إرسال إشارة تداول إلى البوت المنفذ"""
        if not EXECUTE_TRADES:
            safe_log_info("تنفيذ الصفقات معطل في الإعدادات")
            return False
            
        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "signal": signal_data,
                "timestamp": time.time(),
                "source": "signals_bot",
                "system_stats": system_stats
            }
            
            response = await self.client.post(
                f"{self.base_url}/api/trade/signal",
                json=payload,
                headers=headers
            )
            
            if response.status_code == 200:
                result = response.json()
                safe_log_info(f"تم إرسال إشارة {signal_data.get('action')} للتنفيذ: {result.get('message', '')}")
                
                system_stats["total_signals_sent"] += 1
                system_stats["last_signal_time"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
                symbol = signal_data.get('symbol', 'unknown')
                if symbol not in system_stats["signals_by_symbol"]:
                    system_stats["signals_by_symbol"][symbol] = 0
                system_stats["signals_by_symbol"][symbol] += 1
                
                # تحديث إحصائيات البيع والشراء
                action = signal_data.get('action', '')
                if action == 'BUY':
                    system_stats["buy_signals"] += 1
                elif action == 'SELL':
                    system_stats["sell_signals"] += 1
                
                return True
            else:
                safe_log_error(f"فشل إرسال الإشارة: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            safe_log_error(f"خطأ في التواصل مع البوت المنفذ: {e}")
            return False

    async def health_check(self) -> bool:
        """فحص حالة البوت المنفذ"""
        try:
            response = await self.client.get(f"{self.base_url}/health", timeout=10.0)
            system_stats["executor_connected"] = (response.status_code == 200)
            return response.status_code == 200
        except Exception as e:
            system_stats["executor_connected"] = False
            safe_log_error(f"فحص صحة البوت المنفذ فشل: {e}")
            return False

    async def close(self):
        await self.client.aclose()

# ====================== الكلاس الرئيسي كماسح لإشارات البيع والشراء ======================
class SignalsScanner:
    def __init__(self, trade_config, indicator_config, signal_config):
        self.trade_config = trade_config
        self.indicator_config = indicator_config
        self.signal_config = signal_config
        self.data = {}
        self.executor_client = ExecutorBotClient(EXECUTOR_BOT_URL, EXECUTOR_BOT_API_KEY)
        self.ntfy_notifier = NtfyNotifier()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
    async def fetch_binance_data(self, symbol: str, timeframe: str, days: int = 30):
        """جلب البيانات من Binance Futures"""
        try:
            limit = 500
            all_data = []
            end_time = int(datetime.now().timestamp() * 1000)
            interval_h = interval_to_hours(timeframe)
            required_candles = int(days * 24 / interval_h) + 50

            safe_log_info(f"جلب {required_candles} شمعة من العقود الآجلة {symbol} ({timeframe})")

            while len(all_data) < required_candles:
                params = {
                    'symbol': symbol,
                    'interval': timeframe,
                    'limit': min(limit, required_candles - len(all_data)),
                    'endTime': end_time
                }
                
                async with httpx.AsyncClient() as client:
                    response = await client.get("https://fapi.binance.com/fapi/v1/klines", params=params, timeout=15)
                    response.raise_for_status()
                    data = response.json()
                
                if not data or len(data) == 0:
                    break
                all_data = data + all_data
                end_time = data[0][0] - 1

            df = pd.DataFrame(all_data, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_asset_volume', 'number_of_trades',
                'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
            ])

            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')

            df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
            df = df.sort_values('timestamp').reset_index(drop=True)
            df = df.drop_duplicates(subset='timestamp')

            self.data[symbol] = df
            self.calculate_indicators(symbol)
            
            safe_log_info(f"تم جلب {len(df)} شمعة من العقود الآجلة بنجاح لـ {symbol}")
            return True

        except Exception as e:
            safe_log_error(f"خطأ في جلب بيانات العقود الآجلة لـ {symbol}: {e}")
            return False

    def calculate_indicators(self, symbol: str):
        """حساب المؤشرات الفنية"""
        df = self.data[symbol]
        p = self.indicator_config

        # RSI
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)
        
        avg_gain = gain.rolling(window=p['rsi_period'], min_periods=1).mean()
        avg_loss = loss.rolling(window=p['rsi_period'], min_periods=1).mean()
        
        rs = avg_gain / avg_loss
        rs = rs.replace([np.inf, -np.inf], 0).fillna(0)
        df['rsi'] = 100 - (100 / (1 + rs))

        # المتوسطات المتحركة
        df['ema_fast'] = df['close'].ewm(span=p['ema_fast'], adjust=False, min_periods=1).mean()
        df['ema_slow'] = df['close'].ewm(span=p['ema_slow'], adjust=False, min_periods=1).mean()
        df['ema_trend'] = df['close'].ewm(span=p['ema_trend'], adjust=False, min_periods=1).mean()
        df['ema_regime'] = df['close'].ewm(span=p['ema_regime'], adjust=False, min_periods=1).mean()

        # MACD
        ema_fast = df['close'].ewm(span=p['macd_fast'], adjust=False, min_periods=1).mean()
        ema_slow = df['close'].ewm(span=p['macd_slow'], adjust=False, min_periods=1).mean()
        df['macd'] = ema_fast - ema_slow
        df['macd_signal'] = df['macd'].ewm(span=p['macd_signal'], adjust=False, min_periods=1).mean()
        df['macd_histogram'] = df['macd'] - df['macd_signal']

        # Volume MA
        df['volume_ma'] = df['volume'].rolling(20, min_periods=1).mean()

        self.data[symbol] = df
        safe_log_info(f"تم حساب جميع المؤشرات للعقود الآجلة لـ {symbol}")

    def get_market_regime(self, symbol: str, row):
        """تحديد نظام السوق"""
        if not ENABLE_MARKET_REGIME_FILTER:
            return "NEUTRAL"
        price = row['close']
        ema200 = row['ema_regime']
        if price > ema200 * 1.05:
            return "BULL"
        elif price < ema200 * 0.95:
            return "BEAR"
        else:
            return "SIDEWAYS"

    def calculate_signal_strength(self, conditions, symbol: str, row, is_buy: bool):
        """حساب قوة الإشارة المتدرجة من 1-10"""
        try:
            base_conditions = conditions
            
            if base_conditions == 0:
                return 1
            
            rsi = row['rsi'] if not pd.isna(row['rsi']) else 50
            ema_trend_position = 1 if row['close'] > row['ema_trend'] else 0
            macd_strength = abs(row['macd_histogram']) / row['close'] * 1000 if not pd.isna(row['macd_histogram']) else 0
            volume_strength = min(row['volume'] / row['volume_ma'], 3) if not pd.isna(row['volume_ma']) and row['volume_ma'] > 0 else 1
            
            strength_points = 0
            
            # قوة الإشارات بناءً على النوع
            if is_buy:
                # قوة إشارات الشراء
                if rsi < 25:
                    strength_points += 2
                elif rsi < 30:
                    strength_points += 1
                
                if ema_trend_position == 1:  # فوق المتوسط 50
                    strength_points += 1
                
                if macd_strength > 0.8 and row['macd_histogram'] > 0:
                    strength_points += 1
                elif macd_strength > 0.5 and row['macd_histogram'] > 0:
                    strength_points += 0.5
            else:
                # قوة إشارات البيع
                if rsi > 75:
                    strength_points += 2
                elif rsi > 70:
                    strength_points += 1
                
                if ema_trend_position == 0:  # تحت المتوسط 50
                    strength_points += 1
                
                if macd_strength > 0.8 and row['macd_histogram'] < 0:
                    strength_points += 1
                elif macd_strength > 0.5 and row['macd_histogram'] < 0:
                    strength_points += 0.5
            
            if volume_strength > 2.0:
                strength_points += 1.5
            elif volume_strength > 1.5:
                strength_points += 1
            elif volume_strength > 1.2:
                strength_points += 0.5
            
            regime = self.get_market_regime(symbol, row)
            if is_buy:
                if regime == "BULL":
                    strength_points += 1
                elif regime == "SIDEWAYS":
                    strength_points += 0.5
            else:
                if regime == "BEAR":
                    strength_points += 1
                elif regime == "SIDEWAYS":
                    strength_points += 0.5
            
            total_strength = min(base_conditions + strength_points, 10)
            total_strength = max(total_strength, 1)
            
            return total_strength
            
        except Exception as e:
            safe_log_error(f"خطأ في حساب قوة الإشارة لـ {symbol}: {e}")
            return 1

    def generate_buy_signal(self, symbol: str, row):
        """توليد إشارات شراء"""
        try:
            required_columns = ['rsi', 'ema_fast', 'ema_slow', 'macd', 'ema_trend', 'volume_ma']
            if any(pd.isna(row[col]) for col in required_columns):
                return 'HOLD', 1, "بيانات ناقصة"

            buy_conditions = 0
            condition_details = []

            rsi = row['rsi']
            if rsi < self.indicator_config['rsi_oversold']:
                buy_conditions += 2
                condition_details.append(f"RSI منخفض ({rsi:.1f}) - تشبع بيعي")
            elif rsi < 35:
                buy_conditions += 1
                condition_details.append(f"RSI قريب من التشبع البيعي ({rsi:.1f})")

            ema_fast = row['ema_fast']
            ema_slow = row['ema_slow']
            if ema_fast > ema_slow:
                buy_conditions += 2
                condition_details.append("EMA صاعد بقوة")
            elif ema_fast > ema_slow * 0.99:  # شبه متقاطع صاعد
                buy_conditions += 1
                condition_details.append("EMA على وشك التقاطع الصعودي")

            macd_histogram = row['macd_histogram']
            macd_strength = abs(macd_histogram) > (row['close'] * 0.001)
            
            if macd_histogram > 0.002 and macd_strength:
                buy_conditions += 2
                condition_details.append("MACD صاعد بقوة")
            elif macd_histogram > 0:
                buy_conditions += 1
                condition_details.append("MACD إيجابي")

            if self.signal_config['use_trend_filter']:
                if row['close'] > row['ema_trend']:
                    buy_conditions += 1
                    condition_details.append("فوق المتوسط 50 - اتجاه صاعد")
                elif row['close'] > row['ema_trend'] * 0.98:
                    buy_conditions += 0.5
                    condition_details.append("قرب المتوسط 50 - دعم")

            volume_ratio = row['volume'] / row['volume_ma'] if row['volume_ma'] > 0 else 1
            if volume_ratio > 1.5:
                buy_conditions += 1
                condition_details.append(f"حجم مرتفع ({volume_ratio:.1f}x) - تأكيد شراء")

            # فحص دعم EMA 200
            if row['close'] > row['ema_regime']:
                buy_conditions += 1
                condition_details.append("فوق المتوسط 200 - سوق صاعد")

            signal_strength = self.calculate_signal_strength(buy_conditions, symbol, row, is_buy=True)

            regime = self.get_market_regime(symbol, row)
            regime_ok = not ENABLE_MARKET_REGIME_FILTER or regime in ["BULL", "SIDEWAYS"]

            hour = row['timestamp'].hour
            time_ok = not ENABLE_TIME_FILTER or hour in self.trade_config['peak_hours']

            signal = 'HOLD'
            min_conditions = self.signal_config['min_conditions']

            if buy_conditions >= min_conditions and signal_strength >= CONFIDENCE_THRESHOLD and regime_ok and time_ok:
                signal = 'BUY'

            details = " | ".join(condition_details) if condition_details else "لا توجد إشارات شراء قوية"
            
            if signal == 'BUY':
                details += f" | قوة الشراء: {signal_strength:.1f}/10"
                details += f" | شروط الشراء: {buy_conditions}"
            
            return signal, signal_strength, details
            
        except Exception as e:
            safe_log_error(f"خطأ في توليد إشارة الشراء لـ {symbol}: {e}")
            return 'HOLD', 1, f"خطأ: {str(e)}"

    def generate_sell_signal(self, symbol: str, row):
        """توليد إشارات بيع"""
        try:
            required_columns = ['rsi', 'ema_fast', 'ema_slow', 'macd', 'ema_trend', 'volume_ma']
            if any(pd.isna(row[col]) for col in required_columns):
                return 'HOLD', 1, "بيانات ناقصة"

            sell_conditions = 0
            condition_details = []

            rsi = row['rsi']
            if rsi > self.indicator_config['rsi_overbought']:
                sell_conditions += 2
                condition_details.append(f"RSI مرتفع ({rsi:.1f}) - تشبع شرائي")
            elif rsi > 65:
                sell_conditions += 1
                condition_details.append(f"RSI قريب من التشبع الشرائي ({rsi:.1f})")

            ema_fast = row['ema_fast']
            ema_slow = row['ema_slow']
            if ema_fast < ema_slow:
                sell_conditions += 2
                condition_details.append("EMA هابط بقوة")
            elif ema_fast < ema_slow * 1.01:  # شبه متقاطع هابط
                sell_conditions += 1
                condition_details.append("EMA على وشك التقاطع الهبوطي")

            macd_histogram = row['macd_histogram']
            macd_strength = abs(macd_histogram) > (row['close'] * 0.001)
            
            if macd_histogram < -0.002 and macd_strength:
                sell_conditions += 2
                condition_details.append("MACD هابط بقوة")
            elif macd_histogram < 0:
                sell_conditions += 1
                condition_details.append("MACD سلبي")

            if self.signal_config['use_trend_filter']:
                if row['close'] < row['ema_trend']:
                    sell_conditions += 1
                    condition_details.append("تحت المتوسط 50 - اتجاه هابط")
                elif row['close'] < row['ema_trend'] * 1.02:
                    sell_conditions += 0.5
                    condition_details.append("قرب المتوسط 50 - مقاومة")

            volume_ratio = row['volume'] / row['volume_ma'] if row['volume_ma'] > 0 else 1
            if volume_ratio > 1.5:
                sell_conditions += 1
                condition_details.append(f"حجم مرتفع ({volume_ratio:.1f}x) - تأكيد بيع")

            # فحص مقاومة EMA 200
            if row['close'] < row['ema_regime']:
                sell_conditions += 1
                condition_details.append("تحت المتوسط 200 - سوق هابط")

            signal_strength = self.calculate_signal_strength(sell_conditions, symbol, row, is_buy=False)

            regime = self.get_market_regime(symbol, row)
            regime_ok = not ENABLE_MARKET_REGIME_FILTER or regime in ["BEAR", "SIDEWAYS"]

            hour = row['timestamp'].hour
            time_ok = not ENABLE_TIME_FILTER or hour in self.trade_config['peak_hours']

            signal = 'HOLD'
            min_conditions = self.signal_config['min_conditions']

            if sell_conditions >= min_conditions and signal_strength >= CONFIDENCE_THRESHOLD and regime_ok and time_ok:
                signal = 'SELL'

            details = " | ".join(condition_details) if condition_details else "لا توجد إشارات بيع قوية"
            
            if signal == 'SELL':
                details += f" | قوة البيع: {signal_strength:.1f}/10"
                details += f" | شروط البيع: {sell_conditions}"
            
            return signal, signal_strength, details
            
        except Exception as e:
            safe_log_error(f"خطأ في توليد إشارة البيع لـ {symbol}: {e}")
            return 'HOLD', 1, f"خطأ: {str(e)}"

    async def scan_symbol(self, symbol: str):
        """المسح الضوئي لرمز معين للبحث عن إشارات شراء وبيع"""
        try:
            success = await self.fetch_binance_data(
                symbol, 
                self.trade_config['timeframe'], 
                days=30
            )
            
            if not success or symbol not in self.data or len(self.data[symbol]) < 50:
                safe_log_error(f"فشل في جلب البيانات أو البيانات غير كافية لـ {symbol}")
                return None

            latest_row = self.data[symbol].iloc[-1]
            
            # فحص إشارة الشراء
            buy_signal, buy_strength, buy_details = self.generate_buy_signal(symbol, latest_row)
            # فحص إشارة البيع
            sell_signal, sell_strength, sell_details = self.generate_sell_signal(symbol, latest_row)
            
            signal_data = None
            
            # أولوية البيع إذا كانت قوية، وإلا الشراء إذا كانت قوية
            if sell_signal == 'SELL' and sell_strength >= CONFIDENCE_THRESHOLD:
                signal_data = {
                    "symbol": symbol,
                    "action": sell_signal,
                    "signal_type": "SELL_ENTRY",
                    "timeframe": self.trade_config['timeframe'],
                    "price": float(latest_row['close']),
                    "confidence_score": sell_strength * 10,
                    "reason": sell_details,
                    "analysis": {
                        "rsi": float(latest_row['rsi']),
                        "ema_fast": float(latest_row['ema_fast']),
                        "ema_slow": float(latest_row['ema_slow']),
                        "macd_histogram": float(latest_row['macd_histogram']),
                        "volume_ratio": float(latest_row['volume'] / latest_row['volume_ma']) if latest_row['volume_ma'] > 0 else 1.0,
                        "signal_strength": sell_strength,
                        "market_regime": self.get_market_regime(symbol, latest_row)
                    },
                    "timestamp": time.time(),
                    "system_version": "3.0.0 - BUY/SELL"
                }
            elif buy_signal == 'BUY' and buy_strength >= CONFIDENCE_THRESHOLD:
                signal_data = {
                    "symbol": symbol,
                    "action": buy_signal,
                    "signal_type": "BUY_ENTRY",
                    "timeframe": self.trade_config['timeframe'],
                    "price": float(latest_row['close']),
                    "confidence_score": buy_strength * 10,
                    "reason": buy_details,
                    "analysis": {
                        "rsi": float(latest_row['rsi']),
                        "ema_fast": float(latest_row['ema_fast']),
                        "ema_slow": float(latest_row['ema_slow']),
                        "macd_histogram": float(latest_row['macd_histogram']),
                        "volume_ratio": float(latest_row['volume'] / latest_row['volume_ma']) if latest_row['volume_ma'] > 0 else 1.0,
                        "signal_strength": buy_strength,
                        "market_regime": self.get_market_regime(symbol, latest_row)
                    },
                    "timestamp": time.time(),
                    "system_version": "3.0.0 - BUY/SELL"
                }
            
            if signal_data and ENABLE_SIGNAL_SENDING:
                sent = await self.executor_client.send_trade_signal(signal_data)
                if sent:
                    safe_log_info(f"تم إرسال إشارة {signal_data['action']} لـ {symbol} - قوة: {signal_data['analysis']['signal_strength']}/10")
                    await self.ntfy_notifier.send_signal_alert(signal_data)
                    return signal_data
                else:
                    safe_log_error(f"فشل إرسال إشارة {signal_data['action']} لـ {symbol}")
                    return None
            else:
                if signal_data:
                    safe_log_info(f"إشارة {signal_data['action']} مكتشفة ولكن الإرسال معطل: {signal_data['action']} لـ {symbol} - قوة: {signal_data['analysis']['signal_strength']}/10")
                    await self.ntfy_notifier.send_signal_alert(signal_data)
                    return signal_data
                else:
                    safe_log_info(f"لا توجد إشارات قوية لـ {symbol} - قوة البيع: {sell_strength}/10 - قوة الشراء: {buy_strength}/10")
                    return None
                
        except Exception as e:
            safe_log_error(f"خطأ في المسح الضوئي لـ {symbol}: {e}")
            return None

    async def scan_all_symbols(self):
        """المسح الضوئي لجميع الرموز للبحث عن إشارات شراء وبيع"""
        signals_found = []
        
        for symbol in self.trade_config['symbols']:
            try:
                signal_data = await self.scan_symbol(symbol)
                if signal_data:
                    signals_found.append(signal_data)
                await asyncio.sleep(2)
            except Exception as e:
                safe_log_error(f"خطأ في معالجة {symbol}: {e}")
                continue
                
        return signals_found

# ====================== تهيئة الماسح ======================
scanner = SignalsScanner(TRADE_CONFIG, INDICATOR_CONFIG, SIGNAL_CONFIG)

# ====================== واجهات API ======================
@app.get("/")
async def root():
    return {
        "message": "Crypto BUY/SELL Signals Scanner - نظام البيع والشراء المتكامل",
        "version": "3.0.0 - BUY & SELL",
        "status": "running",
        "symbols": TRADE_CONFIG['symbols'],
        "timeframe": TRADE_CONFIG['timeframe'],
        "signal_type": "BUY_SELL_SIGNALS",
        "signal_sending_enabled": ENABLE_SIGNAL_SENDING,
        "confidence_threshold": CONFIDENCE_THRESHOLD,
        "ntfy_alerts": ENABLE_NTFY_ALERTS
    }

@app.get("/health")
async def health_check():
    """فحص صحة النظام"""
    executor_health = await scanner.executor_client.health_check()
    
    return {
        "status": "healthy",
        "executor_connected": executor_health,
        "system_stats": system_stats,
        "signal_type": "BUY_SELL_SIGNALS",
        "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }

@app.post("/scan")
async def scan_signals():
    """المسح الضوئي اليدوي لإشارات البيع والشراء"""
    try:
        signals_found = await scanner.scan_all_symbols()
        
        if signals_found:
            return {
                "status": "success",
                "signals_found": len(signals_found),
                "signal_type": "BUY_SELL_SIGNALS",
                "signals": signals_found,
                "message": f"تم اكتشاف {len(signals_found)} إشارة وإرسالها بنجاح"
            }
        else:
            return {
                "status": "success", 
                "signals_found": 0,
                "signal_type": "BUY_SELL_SIGNALS",
                "message": "لا توجد إشارات قوية حالياً"
            }
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"خطأ في المسح الضوئي: {str(e)}")

@app.post("/scan/{symbol}")
async def scan_single_symbol(symbol: str):
    """المسح الضوئي لرمز معين للبحث عن إشارات بيع وشراء"""
    try:
        if symbol not in TRADE_CONFIG['symbols']:
            raise HTTPException(status_code=400, detail=f"الرمز {symbol} غير مدعوم")
            
        signal_data = await scanner.scan_symbol(symbol)
        
        if signal_data:
            return {
                "status": "success",
                "signal_found": True,
                "signal_type": signal_data.get('action', 'UNKNOWN'),
                "signal_data": signal_data,
                "message": "تم اكتشاف إشارة وإرسالها بنجاح"
            }
        else:
            return {
                "status": "success", 
                "signal_found": False,
                "signal_type": "BUY_SELL",
                "message": "لا توجد إشارات قوية حالياً لهذا الرمز"
            }
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"خطأ في المسح الضوئي: {str(e)}")

@app.get("/system-stats")
async def get_system_stats():
    """الحصول على إحصائيات النظام"""
    uptime_seconds = time.time() - system_stats["start_time"]
    
    days = int(uptime_seconds // 86400)
    hours = int((uptime_seconds % 86400) // 3600)
    minutes = int((uptime_seconds % 3600) // 60)
    
    if days > 0:
        uptime_str = f"{days} يوم, {hours} ساعة, {minutes} دقيقة"
    elif hours > 0:
        uptime_str = f"{hours} ساعة, {minutes} دقيقة"
    else:
        uptime_str = f"{minutes} دقيقة"
    
    return {
        "system_stats": system_stats,
        "uptime": uptime_str,
        "config": {
            "symbols": TRADE_CONFIG['symbols'],
            "timeframe": TRADE_CONFIG['timeframe'],
            "confidence_threshold": CONFIDENCE_THRESHOLD,
            "scan_interval": SCAN_INTERVAL,
            "signal_sending_enabled": ENABLE_SIGNAL_SENDING,
            "trade_execution_enabled": EXECUTE_TRADES,
            "ntfy_alerts": ENABLE_NTFY_ALERTS,
            "signal_type": "BUY_SELL_SIGNALS"
        },
        "current_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }

# ====================== المهام الدورية ======================
async def periodic_scanner_task():
    """المهمة الدورية للمسح الضوئي لإشارات البيع والشراء"""
    safe_log_info("بدء المهمة الدورية للمسح الضوئي لإشارات البيع والشراء")
    
    while True:
        try:
            signals_found = await scanner.scan_all_symbols()
            system_stats["total_scans"] += 1
            system_stats["last_scan_time"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            if signals_found:
                safe_log_info(f"اكتمل المسح الدوري - تم العثور على {len(signals_found)} إشارة وإرسالها")
            else:
                safe_log_info(f"اكتمل المسح الدوري - لا توجد إشارات قوية")
            
            await asyncio.sleep(SCAN_INTERVAL)
            
        except Exception as e:
            safe_log_error(f"خطأ في المهمة الدورية: {e}")
            await asyncio.sleep(60)

async def heartbeat_task():
    """المهمة الدورية لإرسال النبضات"""
    if not ENABLE_HEARTBEAT:
        return
        
    safe_log_info("بدء المهمة الدورية لإرسال النبضات")
    
    # انتظار قليل قبل أول نبضة
    await asyncio.sleep(300)  # 5 دقائق
    
    while True:
        try:
            await scanner.ntfy_notifier.send_heartbeat()
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            
        except Exception as e:
            safe_log_error(f"خطأ في مهمة النبضات: {e}")
            await asyncio.sleep(300)

# ====================== التشغيل ======================
@app.on_event("startup")
async def startup_event():
    """حدث بدء التشغيل"""
    safe_log_info("بدء تشغيل ماسح إشارات البيع والشراء")
    safe_log_info(f"الرموز: {TRADE_CONFIG['symbols']}")
    safe_log_info(f"الإطار الزمني: {TRADE_CONFIG['timeframe']}")
    safe_log_info(f"فاصل المسح: {SCAN_INTERVAL} ثانية")
    safe_log_info(f"عتبة الثقة: {CONFIDENCE_THRESHOLD}")
    safe_log_info(f"إرسال الإشارات: {'مفعل' if ENABLE_SIGNAL_SENDING else 'معطل'}")
    safe_log_info(f"تنفيذ الصفقات: {'مفعل' if EXECUTE_TRADES else 'معطل'}")
    safe_log_info(f"تنبيهات ntfy: {'مفعل' if ENABLE_NTFY_ALERTS else 'معطل'}")
    safe_log_info(f"النبضات الدورية: {'مفعل' if ENABLE_HEARTBEAT else 'معطل'}")
    safe_log_info(f"نوع الإشارات: إشارات بيع وشراء")
    
    # فحص اتصال البوت المنفذ
    executor_connected = await scanner.executor_client.health_check()
    safe_log_info(f"اتصال البوت المنفذ: {'ناجح' if executor_connected else 'فاشل'}")
    
    # إرسال رسالة بدء التشغيل
    await scanner.ntfy_notifier.send_startup_message()
    
    # بدء المهام الدورية
    asyncio.create_task(periodic_scanner_task())
    asyncio.create_task(heartbeat_task())
    
    safe_log_info("اكتمل بدء تشغيل النظام وجاهز للعمل")

@app.on_event("shutdown")
async def shutdown_event():
    """حدث إيقاف التشغيل"""
    safe_log_info("إيقاف ماسح إشارات البيع والشراء")
    await scanner.executor_client.close()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
