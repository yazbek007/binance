
# ====================== المكتبات الأساسية ======================
import os
import asyncio
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import requests
import time
import logging
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential
import warnings
warnings.filterwarnings('ignore')

# ====================== تحميل الإعدادات ======================
load_dotenv()

# ====================== ⚙️ الإعدادات الرئيسية (قابلة للتعديل) ======================

# 🔑 إعدادات Binance API
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

# 📊 الرموز والأطر الزمنية
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "ADAUSDT"]
TIMEFRAME = "4h"  # 1m, 5m, 15m, 1h, 4h, 1d

# 🔔 إعدادات ntfy
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "crypto_signals")
ENABLE_NTFY = True

# 🤖 إعدادات البوت المنفذ
EXECUTOR_BOT_URL = os.getenv("EXECUTOR_URL", "http://localhost:8001")
EXECUTOR_API_KEY = os.getenv("EXECUTOR_API_KEY", "")
SEND_TO_EXECUTOR = True  # إرسال الإشارات للتنفيذ

# ⚖️ عتبات القوة (من 1 إلى 10)
STRONG_SIGNAL_THRESHOLD = 7.5    # إشارات قوية → إرسال للتنفيذ
MEDIUM_SIGNAL_THRESHOLD = 5.0    # إشارات متوسطة → إشعار فقط
IGNORE_THRESHOLD = 3.0           # إشارات ضعيفة → تجاهل

# 📈 إعدادات المؤشرات الفنية
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
EMA_FAST = 9
EMA_SLOW = 21
EMA_TREND = 50
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# ⏰ إعدادات التوقيت
SCAN_INTERVAL = 300  # ثانية بين كل مسح (300 = 5 دقائق)
MAX_DATA_DAYS = 30   # عدد الأيام للبيانات التاريخية

# 📝 التسجيل
LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR
SAVE_LOGS_TO_FILE = False

# ====================== إعداد التسجيل ======================
log_format = '%(asctime)s - %(levelname)s - %(message)s'
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format=log_format,
    handlers=[logging.StreamHandler()]
)
if SAVE_LOGS_TO_FILE:
    file_handler = logging.FileHandler('signals.log', encoding='utf-8')
    file_handler.setFormatter(logging.Formatter(log_format))
    logging.getLogger().addHandler(file_handler)

logger = logging.getLogger(__name__)

# ====================== دوال المساعدة ======================

def send_ntfy_notification(title, message, priority=3, tags=""):
    """إرسال إشعار عبر ntfy"""
    if not ENABLE_NTFY:
        return False
    
    try:
        url = f"https://ntfy.sh/{NTFY_TOPIC}"
        headers = {
            "Title": title,
            "Priority": str(priority),
            "Tags": tags if tags else "chart_with_upwards_trend"
        }
        
        response = requests.post(
            url, 
            data=message.encode('utf-8'), 
            headers=headers, 
            timeout=10
        )
        
        if response.status_code in [200, 202]:
            logger.info(f"تم إرسال إشعار ntfy: {title}")
            return True
        else:
            logger.warning(f"فشل إرسال ntfy: {response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"خطأ في إرسال ntfy: {e}")
        return False


async def send_to_executor(signal_data):
    """إرسال إشارة إلى بوت التنفيذ"""
    if not SEND_TO_EXECUTOR or not EXECUTOR_API_KEY:
        logger.warning("إرسال الإشارات للتنفيذ معطل - تحقق من الإعدادات")
        return False
    
    try:
        headers = {
            "Authorization": f"Bearer {EXECUTOR_API_KEY}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "signal": signal_data,
            "timestamp": time.time(),
            "source": "signals_scanner"
        }
        
        async with requests.AsyncClient() as client:
            response = await client.post(
                f"{EXECUTOR_BOT_URL}/api/receive-signal",
                json=payload,
                headers=headers,
                timeout=15
            )
            
        if response.status_code == 200:
            logger.info(f"تم إرسال إشارة {signal_data['action']} للتنفيذ: {signal_data['symbol']}")
            return True
        else:
            logger.warning(f"فشل إرسال الإشارة: {response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"خطأ في التواصل مع بوت التنفيذ: {e}")
        return False


def interval_to_minutes(interval):
    """تحويل الإطار الزمني إلى دقائق"""
    mapping = {
        '1m': 1, '3m': 3, '5m': 5, '15m': 15, '30m': 30,
        '1h': 60, '2h': 120, '4h': 240, '6h': 360, '8h': 480,
        '12h': 720, '1d': 1440, '3d': 4320, '1w': 10080
    }
    return mapping.get(interval, 240)  # 4h افتراضي


# ====================== الكلاس الرئيسي لمكتشف الإشارات ======================
class SignalsScanner:
    def __init__(self):
        self.data_cache = {}
        self.last_scan_time = {}
        self.stats = {
            "total_scans": 0,
            "buy_signals": 0,
            "sell_signals": 0,
            "strong_signals": 0,
            "medium_signals": 0,
            "ignored_signals": 0,
            "last_scan": None
        }
        logger.info("تم تهيئة مكتشف الإشارات")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
    async def fetch_binance_data(self, symbol, timeframe):
        """جلب بيانات الشموع من Binance"""
        try:
            # استخدام Binance API العامة (لا تحتاج مفاتيح للبيانات العامة)
            base_url = "https://api.binance.com"
            endpoint = "/api/v3/klines"
            
            interval_minutes = interval_to_minutes(timeframe)
            limit = int((MAX_DATA_DAYS * 24 * 60) / interval_minutes)
            limit = min(limit, 1000)  # الحد الأقصى لـ Binance
            
            params = {
                'symbol': symbol,
                'interval': timeframe,
                'limit': limit
            }
            
            async with requests.AsyncClient() as client:
                response = await client.get(
                    f"{base_url}{endpoint}",
                    params=params,
                    timeout=15
                )
                response.raise_for_status()
                klines = response.json()
            
            # تحويل البيانات إلى DataFrame
            df = pd.DataFrame(klines, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                'taker_buy_quote', 'ignore'
            ])
            
            # تحويل الأنواع
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            numeric_cols = ['open', 'high', 'low', 'close', 'volume']
            for col in numeric_cols:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            
            df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
            df = df.sort_values('timestamp').reset_index(drop=True)
            
            self.data_cache[symbol] = df
            logger.debug(f"تم جلب {len(df)} شمعة لـ {symbol} ({timeframe})")
            return True
            
        except Exception as e:
            logger.error(f"خطأ في جلب بيانات {symbol}: {e}")
            return False

    def calculate_indicators(self, symbol):
        """حساب المؤشرات الفنية"""
        if symbol not in self.data_cache:
            return False
        
        df = self.data_cache[symbol]
        
        # حساب RSI
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        
        avg_gain = gain.rolling(window=RSI_PERIOD).mean()
        avg_loss = loss.rolling(window=RSI_PERIOD).mean()
        
        rs = avg_gain / avg_loss
        rs = rs.replace([np.inf, -np.inf], 100).fillna(100)
        df['rsi'] = 100 - (100 / (1 + rs))
        
        # المتوسطات المتحركة الأسية
        df['ema_fast'] = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()
        df['ema_trend'] = df['close'].ewm(span=EMA_TREND, adjust=False).mean()
        
        # MACD
        ema12 = df['close'].ewm(span=MACD_FAST, adjust=False).mean()
        ema26 = df['close'].ewm(span=MACD_SLOW, adjust=False).mean()
        df['macd'] = ema12 - ema26
        df['macd_signal'] = df['macd'].ewm(span=MACD_SIGNAL, adjust=False).mean()
        df['macd_hist'] = df['macd'] - df['macd_signal']
        
        # متوسط حجم التداول
        df['volume_ma'] = df['volume'].rolling(window=20).mean()
        
        self.data_cache[symbol] = df
        return True

    def analyze_symbol(self, symbol):
        """تحليل الرمز وإنتاج الإشارات"""
        if symbol not in self.data_cache:
            return None
        
        df = self.data_cache[symbol]
        if len(df) < 50:  # تحتاج بيانات كافية
            return None
        
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        
        # جمع نقاط القوة للإشارة
        buy_points = 0
        sell_points = 0
        reasons = []
        
        # 1. تحليل RSI
        rsi = latest['rsi']
        if rsi < RSI_OVERSOLD:
            buy_points += 2
            reasons.append(f"RSI منخفض ({rsi:.1f})")
        elif rsi < 35:
            buy_points += 1
            reasons.append(f"RSي قريب من التشبع البيعي ({rsi:.1f})")
        
        if rsi > RSI_OVERBOUGHT:
            sell_points += 2
            reasons.append(f"RSI مرتفع ({rsi:.1f})")
        elif rsi > 65:
            sell_points += 1
            reasons.append(f"RSI قريب من التشبع الشرائي ({rsi:.1f})")
        
        # 2. تحليل المتوسطات المتحركة
        if latest['ema_fast'] > latest['ema_slow']:
            buy_points += 1
            reasons.append("EMA سريع فوق البطيء")
        elif latest['ema_fast'] < latest['ema_slow']:
            sell_points += 1
            reasons.append("EMA سريع تحت البطيء")
        
        # 3. تحليل MACD
        if latest['macd_hist'] > 0 and prev['macd_hist'] <= 0:
            buy_points += 2
            reasons.append("MACD إيجابي وصاعد")
        elif latest['macd_hist'] < 0 and prev['macd_hist'] >= 0:
            sell_points += 2
            reasons.append("MACD سلبي وهابط")
        
        # 4. تحليل الحجم
        volume_ratio = latest['volume'] / latest['volume_ma'] if latest['volume_ma'] > 0 else 1
        if volume_ratio > 1.5:
            if buy_points > sell_points:
                buy_points += 1
                reasons.append(f"حجم شراء قوي ({volume_ratio:.1f}x)")
            elif sell_points > buy_points:
                sell_points += 1
                reasons.append(f"حجم بيع قوي ({volume_ratio:.1f}x)")
        
        # تحديد الإشارة النهائية وقوتها
        signal_strength = max(buy_points, sell_points)
        signal_type = "HOLD"
        
        if buy_points > sell_points and buy_points >= 2:
            signal_type = "BUY"
        elif sell_points > buy_points and sell_points >= 2:
            signal_type = "SELL"
        
        # تحضير بيانات الإشارة
        signal_data = {
            "symbol": symbol,
            "action": signal_type,
            "strength": signal_strength,
            "price": float(latest['close']),
            "rsi": float(rsi),
            "ema_fast": float(latest['ema_fast']),
            "ema_slow": float(latest['ema_slow']),
            "macd_hist": float(latest['macd_hist']),
            "volume_ratio": float(volume_ratio),
            "reasons": reasons,
            "timestamp": datetime.now().isoformat(),
            "timeframe": TIMEFRAME
        }
        
        return signal_data

    async def process_signal(self, signal_data):
        """معالجة الإشارة المكتشفة"""
        if not signal_data or signal_data["action"] == "HOLD":
            return
        
        symbol = signal_data["symbol"]
        action = signal_data["action"]
        strength = signal_data["strength"]
        
        # تحديث الإحصائيات
        self.stats["total_scans"] += 1
        if action == "BUY":
            self.stats["buy_signals"] += 1
        elif action == "SELL":
            self.stats["sell_signals"] += 1
        
        # تحديد نوع التعامل مع الإشارة
        signal_category = ""
        
        if strength >= STRONG_SIGNAL_THRESHOLD:
            # إشارة قوية → إرسال للتنفيذ + إشعار
            signal_category = "قوية"
            self.stats["strong_signals"] += 1
            
            # إرسال للتنفيذ
            if SEND_TO_EXECUTOR:
                await send_to_executor(signal_data)
            
            # إرسال إشعار ntfy
            title = f"🚨 إشارة {action} {signal_category} - {symbol}"
            message = (
                f"السعر: {signal_data['price']:.2f}\n"
                f"القوة: {strength}/10\n"
                f"الوقت: {datetime.now().strftime('%H:%M')}\n"
                f"الأسباب: {', '.join(signal_data['reasons'][:3])}"
            )
            send_ntfy_notification(title, message, priority=4, 
                                 tags="rocket" if action=="BUY" else "warning")
        
        elif strength >= MEDIUM_SIGNAL_THRESHOLD:
            # إشارة متوسطة → إشعار فقط
            signal_category = "متوسطة"
            self.stats["medium_signals"] += 1
            
            title = f"⚠️ إشارة {action} {signal_category} - {symbol}"
            message = (
                f"السعر: {signal_data['price']:.2f}\n"
                f"القوة: {strength}/10\n"
                f"ملاحظة: إشارة للمراقبة فقط"
            )
            send_ntfy_notification(title, message, priority=3,
                                 tags="eyes" if action=="BUY" else "eyes")
        
        else:
            # إشارة ضعيفة → تجاهل (تسجيل فقط)
            signal_category = "ضعيفة"
            self.stats["ignored_signals"] += 1
            logger.debug(f"تم تجاهل إشارة {action} ضعيفة لـ {symbol} (قوة: {strength})")
        
        logger.info(f"إشارة {action} {signal_category} لـ {symbol} - القوة: {strength}/10")

    async def scan_symbol(self, symbol):
        """المسح الكامل لرمز معين"""
        try:
            # جلب البيانات
            success = await self.fetch_binance_data(symbol, TIMEFRAME)
            if not success:
                return
            
            # حساب المؤشرات
            self.calculate_indicators(symbol)
            
            # التحليل وإنتاج الإشارة
            signal_data = self.analyze_symbol(symbol)
            
            # معالجة الإشارة
            await self.process_signal(signal_data)
            
            self.last_scan_time[symbol] = time.time()
            
        except Exception as e:
            logger.error(f"خطأ في مسح {symbol}: {e}")

    async def scan_all_symbols(self):
        """المسح الكامل لجميع الرموز"""
        logger.info(f"بدء المسح الدوري لـ {len(SYMBOLS)} رموز...")
        
        tasks = [self.scan_symbol(symbol) for symbol in SYMBOLS]
        await asyncio.gather(*tasks, return_exceptions=True)
        
        self.stats["last_scan"] = datetime.now().isoformat()
        logger.info(f"اكتمل المسح الدوري - إحصائيات: {self.stats}")

    def print_stats(self):
        """عرض إحصائيات النظام"""
        print("\n" + "="*50)
        print("📊 إحصائيات مكتشف الإشارات")
        print("="*50)
        print(f"عدد المسوحات: {self.stats['total_scans']}")
        print(f"إشارات الشراء: {self.stats['buy_signals']}")
        print(f"إشارات البيع: {self.stats['sell_signals']}")
        print(f"إشارات قوية: {self.stats['strong_signals']}")
        print(f"إشارات متوسطة: {self.stats['medium_signals']}")
        print(f"إشارات م تجاهلة: {self.stats['ignored_signals']}")
        print(f"آخر مسح: {self.stats['last_scan']}")
        print("="*50 + "\n")


# ====================== الدالة الرئيسية ======================
async def main():
    """الدالة الرئيسية للتشغيل المستمر"""
    scanner = SignalsScanner()
    
    logger.info("="*60)
    logger.info("🚀 بدء تشغيل مكتشف إشارات التداول الآلي")
    logger.info("="*60)
    logger.info(f"الرموز: {SYMBOLS}")
    logger.info(f"الإطار الزمني: {TIMEFRAME}")
    logger.info(f"فاصل المسح: {SCAN_INTERVAL} ثانية")
    logger.info(f"عتبة القوة القوية: {STRONG_SIGNAL_THRESHOLD}")
    logger.info(f"عتبة القوة المتوسطة: {MEDIUM_SIGNAL_THRESHOLD}")
    logger.info(f"إرسال للتنفيذ: {'مفعل' if SEND_TO_EXECUTOR else 'معطل'}")
    logger.info(f"إشعارات ntfy: {'مفعل' if ENABLE_NTFY else 'معطل'}")
    logger.info("="*60)
    
    # إرسال إشعار بدء التشغيل
    if ENABLE_NTFY:
        send_ntfy_notification(
            "بدء التشغيل",
            f"مكتشف الإشارات يعمل الآن\nالرموز: {', '.join(SYMBOLS)}\nالفاصل: {SCAN_INTERVAL} ثانية",
            priority=2,
            tags="rocket,gear"
        )
    
    # الحلقة الرئيسية
    while True:
        try:
            await scanner.scan_all_symbols()
            scanner.print_stats()
            
            logger.info(f"انتظار {SCAN_INTERVAL} ثانية للمسح التالي...")
            await asyncio.sleep(SCAN_INTERVAL)
            
        except KeyboardInterrupt:
            logger.info("تلقي إشارة إيقاف...")
            break
        except Exception as e:
            logger.error(f"خطأ غير متوقع في الحلقة الرئيسية: {e}")
            await asyncio.sleep(60)  # انتظار دقيقة وإعادة المحاولة


# ====================== نقطة الدخول ======================
if __name__ == "__main__":
    # تشغيل البوت
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("تم إيقاف مكتشف الإشارات")
        if ENABLE_NTFY:
            send_ntfy_notification(
                "إيقاف التشغيل",
                "تم إيقاف مكتشف الإشارات",
                priority=1,
                tags="stop,power"
            )
    except Exception as e:
        logger.error(f"خطأ فادح: {e}")
