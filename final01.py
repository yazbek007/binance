# main.py
import os
import time
import logging
import json
import traceback
from datetime import datetime, timedelta
from threading import Thread
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, asdict, field
from pathlib import Path

import ccxt
import pandas as pd
import numpy as np
from flask import Flask, render_template_string, jsonify, request, Response
from dotenv import load_dotenv
import warnings
warnings.filterwarnings('ignore')

load_dotenv()

# ────────────────────────────────────────────────
# إعدادات الاستراتيجية
# ────────────────────────────────────────────────

SYMBOL_XRP = 'XRP/USDT'
SYMBOL_ADA = 'ADA/USDT'
TIMEFRAME = '1h'
LOOP_INTERVAL_SECONDS = 3600

# معلمات الاستراتيجية
Z_WINDOW = 10
Z_THRESHOLD = 0.55
BB_WINDOW = 17
BB_STD = 1.85
TP_PCT = 2.0
SL_PCT = -7.0
SL_Z = 1.35
BB_WIDTH_MULTIPLIER = 1.3

# ────────────────────────────────────────────────
# هياكل البيانات
# ────────────────────────────────────────────────

@dataclass
class Trade:
    entry_time: datetime
    exit_time: datetime
    direction: str
    entry_ratio: float
    exit_ratio: float
    pnl_pct: float
    reason: str
    entry_z: float = 0.0
    exit_z: float = 0.0
    
    def to_dict(self):
        """تحويل إلى قاموس مع معالجة التواريخ"""
        result = asdict(self)
        # تحويل التواريخ إلى strings
        for date_field in ['entry_time', 'exit_time']:
            if isinstance(result[date_field], datetime):
                result[date_field] = result[date_field].strftime('%Y-%m-%d %H:%M:%S')
        return result

@dataclass
class BacktestResult:
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    avg_pnl: float
    max_win: float
    max_loss: float
    sharpe_ratio: float
    max_drawdown: float
    trades: List[Trade]
    
    def to_dict(self):
        """تحويل إلى قاموس"""
        result = asdict(self)
        result['trades'] = [trade.to_dict() for trade in self.trades]
        return result

# ────────────────────────────────────────────────
# إعدادات النظام
# ────────────────────────────────────────────────

class TradingMode:
    BACKTEST = 'backtest'
    PAPER = 'paper'
    LIVE = 'live'

# قراءة إعدادات البيئة
TRADING_MODE = os.getenv('TRADING_MODE', TradingMode.BACKTEST)
INITIAL_BALANCE = float(os.getenv('INITIAL_BALANCE', 1000.0))
EXCHANGE_TYPE = os.getenv('EXCHANGE_TYPE', 'binance').lower()

# ────────────────────────────────────────────────
# نظام Logging محسّن
# ────────────────────────────────────────────────

class SafeLogger:
    """فئة لـ Logging آمن مع معالجة الأخطاء"""
    
    def __init__(self, name="trading_bot"):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.INFO)
        
        # تنسيق السجلات
        formatter = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # معالج الملف
        try:
            file_handler = logging.FileHandler("trading_bot.log", encoding='utf-8')
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)
        except Exception as e:
            print(f"⚠️ تعذر إنشاء ملف السجل: {e}")
        
        # معالج الكونسول
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)
        
        # إضافة معلومات الوضع
        self.mode = TRADING_MODE.upper()
    
    def _add_mode(self, msg):
        """إضافة معلومات الوضع للرسالة"""
        return f"[{self.mode}] {msg}"
    
    def info(self, msg):
        self.logger.info(self._add_mode(msg))
    
    def error(self, msg, exc_info=False):
        self.logger.error(self._add_mode(msg), exc_info=exc_info)
    
    def warning(self, msg):
        self.logger.warning(self._add_mode(msg))
    
    def debug(self, msg):
        self.logger.debug(self._add_mode(msg))

# إنشاء الـ logger
logger = SafeLogger()

# ────────────────────────────────────────────────
# متغيرات التداول العالمية
# ────────────────────────────────────────────────

trades: List[Trade] = []
current_position = None
entry_time = None
entry_price_ratio = None
entry_z = None
current_balance = INITIAL_BALANCE
paper_positions = {}

# ────────────────────────────────────────────────
# إدارة البيانات
# ────────────────────────────────────────────────

class DataManager:
    """فئة لإدارة بيانات السوق"""
    
    def __init__(self):
        self.exchange = None
        self.exchange_type = EXCHANGE_TYPE
        self.setup_exchange()
    
    def setup_exchange(self):
        """إعداد اتصال بالمنصة المالية"""
        try:
            if TRADING_MODE in [TradingMode.BACKTEST, TradingMode.PAPER]:
                # في وضعي Backtest و Paper، يمكن استخدام بيانات وهمية أو Testnet
                self.setup_testnet_exchange()
            elif TRADING_MODE == TradingMode.LIVE:
                self.setup_live_exchange()
            else:
                logger.warning(f"وضع غير معروف: {TRADING_MODE}")
                self.exchange = None
                
        except Exception as e:
            logger.error(f"❌ فشل إعداد المنصة: {e}")
            self.exchange = None
    
    def setup_testnet_exchange(self):
        """إعداد اتصال Testnet"""
        try:
            if self.exchange_type == 'bybit':
                # استخدام Bybit Testnet
                api_key = os.getenv('BYBIT_TESTNET_API_KEY', '')
                secret = os.getenv('BYBIT_TESTNET_SECRET', '')
                
                if not api_key or not secret:
                    logger.warning("⚠️ مفاتيح Bybit Testnet غير موجودة، استخدام وضع Offline")
                    return
                
                self.exchange = ccxt.bybit({
                    'apiKey': api_key,
                    'secret': secret,
                    'enableRateLimit': True,
                    'options': {
                        'defaultType': 'future',
                        'test': True,
                    }
                })
                logger.info("✅ تم الاتصال بـ Bybit Testnet")
                
            else:  # افتراضي Binance
                # استخدام Binance Testnet أو وضع Offline
                api_key = os.getenv('BINANCE_TESTNET_API_KEY', '')
                secret = os.getenv('BINANCE_TESTNET_SECRET', '')
                
                if not api_key or not secret:
                    logger.info("📊 استخدام وضع Offline للـ Backtesting")
                    self.exchange = None
                    return
                
                self.exchange = ccxt.binance({
                    'apiKey': api_key,
                    'secret': secret,
                    'enableRateLimit': True,
                    'options': {
                        'defaultType': 'future',
                        'test': True,
                    }
                })
                logger.info("✅ تم الاتصال بـ Binance Testnet")
            
            if self.exchange:
                # اختبار الاتصال
                try:
                    balance = self.exchange.fetch_balance()
                    usdt_balance = balance.get('USDT', {}).get('free', 0)
                    logger.info(f"💰 الرصيد المتاح: {usdt_balance} USDT")
                except:
                    logger.warning("⚠️ تعذر جلب الرصيد، لكن الاتصال ناجح")
                    
        except Exception as e:
            logger.error(f"❌ فشل إعداد Testnet: {e}")
            self.exchange = None
    
    def setup_live_exchange(self):
        """إعداد اتصال Live للتداول الحقيقي"""
        logger.warning("⚠️ وضع Live Trading يحتاج إلى إعدادات خاصة وتأمين")
        # هنا يمكن إضافة إعدادات التداول الحقيقي
        self.exchange = None
    
    def fetch_historical_data(self, symbol: str, days: int = 30) -> pd.DataFrame:
        """جلب بيانات تاريخية للـ Backtesting"""
        try:
            logger.info(f"📥 جلب بيانات {symbol} لـ {days} يوم...")
            
            # استخدام ccxt لجلب البيانات
            temp_exchange = ccxt.binance()
            
            # حساب وقت البداية
            end_time = datetime.now()
            start_time = end_time - timedelta(days=days)
            since = int(start_time.timestamp() * 1000)
            
            all_ohlcv = []
            current_since = since
            
            while True:
                try:
                    ohlcv = temp_exchange.fetch_ohlcv(
                        symbol, 
                        TIMEFRAME, 
                        since=current_since,
                        limit=1000
                    )
                    
                    if not ohlcv or len(ohlcv) == 0:
                        break
                    
                    all_ohlcv.extend(ohlcv)
                    
                    # تحديث الوقت للدفعة التالية
                    last_timestamp = ohlcv[-1][0]
                    if last_timestamp <= current_since:
                        break
                    
                    current_since = last_timestamp + 1
                    
                    # التوقف إذا وصلنا للوقت الحالي
                    if len(ohlcv) < 1000 or datetime.fromtimestamp(last_timestamp/1000) >= end_time:
                        break
                    
                    # احترام rate limit
                    time.sleep(temp_exchange.rateLimit / 1000)
                    
                except Exception as e:
                    logger.warning(f"⚠️ خطأ في جلب الدفعة: {e}")
                    break
            
            if not all_ohlcv:
                logger.warning(f"⚠️ لا توجد بيانات لـ {symbol}")
                return pd.DataFrame()
            
            # إنشاء DataFrame
            df = pd.DataFrame(
                all_ohlcv,
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )
            
            # تحويل التواريخ
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            
            logger.info(f"✅ تم جلب {len(df)} شمعة لـ {symbol}")
            return df
            
        except Exception as e:
            logger.error(f"❌ خطأ في جلب البيانات التاريخية لـ {symbol}: {e}")
            return pd.DataFrame()
    
    def fetch_live_data(self, symbol: str, limit: int = 300) -> pd.DataFrame:
        """جلب بيانات حية من المنصة"""
        if not self.exchange:
            logger.warning(f"⚠️ لا يوجد اتصال بالمنصة لـ {symbol}")
            return pd.DataFrame()
        
        try:
            # تعديل الرمز لـ Bybit إذا لزم
            if self.exchange_type == 'bybit':
                symbol = symbol.replace('/', '')
            
            ohlcv = self.exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=limit)
            
            if not ohlcv:
                return pd.DataFrame()
            
            df = pd.DataFrame(
                ohlcv,
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )
            
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            
            return df
            
        except Exception as e:
            logger.error(f"❌ خطأ في جلب البيانات الحية لـ {symbol}: {e}")
            return pd.DataFrame()

# إنشاء مدير البيانات
data_manager = DataManager()

# ────────────────────────────────────────────────
# دوال المؤشرات والإشارات
# ────────────────────────────────────────────────

def compute_indicators(df_xrp: pd.DataFrame, df_ada: pd.DataFrame) -> pd.DataFrame:
    """حساب جميع المؤشرات المطلوبة"""
    if df_xrp.empty or df_ada.empty:
        return pd.DataFrame()
    
    # محاذاة البيانات
    common_index = df_xrp.index.intersection(df_ada.index)
    if len(common_index) == 0:
        return pd.DataFrame()
    
    df = pd.DataFrame(index=common_index)
    df['xrp'] = df_xrp.loc[common_index, 'close']
    df['ada'] = df_ada.loc[common_index, 'close']
    df['ratio'] = df['xrp'] / df['ada']
    
    # Z-score
    df['z_mean'] = df['ratio'].rolling(Z_WINDOW, min_periods=1).mean()
    df['z_std'] = df['ratio'].rolling(Z_WINDOW, min_periods=1).std()
    df['z'] = (df['ratio'] - df['z_mean']) / df['z_std'].replace(0, 1e-10)
    
    # Bollinger Bands
    df['bb_mid'] = df['ratio'].rolling(BB_WINDOW, min_periods=1).mean()
    df['bb_std'] = df['ratio'].rolling(BB_WINDOW, min_periods=1).std()
    df['bb_upper'] = df['bb_mid'] + BB_STD * df['bb_std']
    df['bb_lower'] = df['bb_mid'] - BB_STD * df['bb_std']
    df['bb_width'] = df['bb_upper'] - df['bb_lower']
    df['bb_width_ma5'] = df['bb_width'].rolling(5, min_periods=1).mean()
    
    return df.dropna()

def generate_signal(df: pd.DataFrame, index: int = -1) -> Tuple[Optional[str], Dict]:
    """توليد إشارة تداول"""
    if df.empty or abs(index) > len(df):
        return None, {}
    
    latest = df.iloc[index] if index < 0 else df.iloc[index]
    
    # فلتر عرض الباند
    if latest['bb_width'] <= latest['bb_width_ma5'] * BB_WIDTH_MULTIPLIER:
        return None, {}
    
    signal_data = {
        'ratio': float(latest['ratio']),
        'z': float(latest['z']),
        'bb_upper': float(latest['bb_upper']),
        'bb_lower': float(latest['bb_lower']),
        'timestamp': df.index[index] if index >= 0 else df.index[-1]
    }
    
    if latest['z'] < -Z_THRESHOLD or latest['ratio'] < latest['bb_lower']:
        return 'long_ada_short_xrp', signal_data
    
    if latest['z'] > Z_THRESHOLD or latest['ratio'] > latest['bb_upper']:
        return 'short_ada_long_xrp', signal_data
    
    return None, {}

def check_exit_conditions(
    position: str, 
    entry_data: Dict, 
    current_data: Dict,
    days_held: float = None
) -> Tuple[bool, str, float]:
    """فحص شروط الخروج"""
    entry_ratio = entry_data.get('ratio', 0)
    current_ratio = current_data.get('ratio', 0)
    current_z = current_data.get('z', 0)
    
    if entry_ratio == 0:
        return False, "لا يوجد سعر دخول", 0
    
    # حساب الربح/الخسارة
    if position == 'long_ada_short_xrp':
        pnl_pct = (entry_ratio - current_ratio) / entry_ratio * 100
    else:  # short_ada_long_xrp
        pnl_pct = (current_ratio - entry_ratio) / entry_ratio * 100
    
    # Take Profit
    if pnl_pct >= TP_PCT:
        return True, f"Take Profit {pnl_pct:.2f}%", pnl_pct
    
    # Stop Loss
    if pnl_pct <= SL_PCT:
        return True, f"Stop Loss {pnl_pct:.2f}%", pnl_pct
    
    # Stop Loss على Z-score
    if abs(current_z) <= 0.4:
        return True, f"Z-score قريب من المتوسط ({current_z:.2f})", pnl_pct
    
    # Time-based exit (3 أيام)
    if days_held and days_held >= 3:
        return True, f"Timeout بعد {days_held:.1f} يوم", pnl_pct
    
    return False, "", pnl_pct

# ────────────────────────────────────────────────
# نظام Paper Trading
# ────────────────────────────────────────────────

class PaperTrading:
    """فئة لمحاكاة التداول الورقي"""
    
    def __init__(self, initial_balance: float = 1000):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.positions = {}
        self.trades: List[Trade] = []
        self.equity_curve = [initial_balance]
        self.current_pnl = 0
    
    def enter_position(
        self, 
        direction: str, 
        ratio: float, 
        z: float, 
        timestamp: datetime
    ) -> bool:
        """فتح مركز في Paper Trading"""
        if self.positions:
            logger.warning("⚠️ يوجد مركز مفتوح بالفعل")
            return False
        
        # حساب حجم المركز (10% من الرصيد)
        position_size = self.balance * 0.1
        
        self.positions = {
            'direction': direction,
            'entry_ratio': ratio,
            'entry_z': z,
            'entry_time': timestamp,
            'position_size': position_size,
            'entry_balance': self.balance
        }
        
        logger.info(f"📝 دخول {direction} عند ratio={ratio:.4f}, z={z:.2f}")
        return True
    
    def update_position(self, current_ratio: float, current_z: float) -> float:
        """تحديث قيمة المركز المفتوح"""
        if not self.positions:
            return 0
        
        position = self.positions
        entry_ratio = position['entry_ratio']
        
        # حساب PnL الحالي
        if position['direction'] == 'long_ada_short_xrp':
            pnl_pct = (entry_ratio - current_ratio) / entry_ratio * 100
        else:  # short_ada_long_xrp
            pnl_pct = (current_ratio - entry_ratio) / entry_ratio * 100
        
        pnl_amount = (pnl_pct / 100) * position['position_size']
        self.current_pnl = pnl_amount
        
        return pnl_pct
    
    def exit_position(
        self, 
        exit_ratio: float, 
        exit_z: float, 
        reason: str, 
        timestamp: datetime
    ) -> float:
        """إغلاق المركز في Paper Trading"""
        if not self.positions:
            logger.warning("⚠️ لا يوجد مركز مفتوح")
            return 0
        
        position = self.positions
        entry_ratio = position['entry_ratio']
        
        # حساب PnL النهائي
        if position['direction'] == 'long_ada_short_xrp':
            pnl_pct = (entry_ratio - exit_ratio) / entry_ratio * 100
        else:  # short_ada_long_xrp
            pnl_pct = (exit_ratio - entry_ratio) / entry_ratio * 100
        
        pnl_amount = (pnl_pct / 100) * position['position_size']
        self.balance += pnl_amount
        
        # تسجيل الصفقة
        trade = Trade(
            entry_time=position['entry_time'],
            exit_time=timestamp,
            direction=position['direction'],
            entry_ratio=entry_ratio,
            exit_ratio=exit_ratio,
            pnl_pct=pnl_pct,
            reason=reason,
            entry_z=position['entry_z'],
            exit_z=exit_z
        )
        
        self.trades.append(trade)
        self.equity_curve.append(self.balance)
        
        # تحديث القائمة العالمية للصفقات
        global trades
        trades.append(trade)
        
        logger.info(
            f"📝 خروج: {reason} | "
            f"PnL: {pnl_pct:.2f}% | "
            f"الرصيد: {self.balance:.2f} USDT"
        )
        
        # مسح المركز
        self.positions = {}
        self.current_pnl = 0
        
        return pnl_pct
    
    def get_stats(self) -> Dict:
        """الحصول على إحصائيات Paper Trading"""
        try:
            if not self.trades:
                return {
                    'balance': self.balance,
                    'total_return': 0,
                    'total_trades': 0,
                    'winning_trades': 0,
                    'losing_trades': 0,
                    'win_rate': 0,
                    'total_pnl': 0,
                    'avg_pnl': 0,
                    'max_win': 0,
                    'max_loss': 0,
                    'current_position': None,
                    'current_pnl': self.current_pnl
                }
            
            # تحويل الصفقات إلى DataFrame
            trades_data = []
            for trade in self.trades:
                trades_data.append(trade.to_dict())
            
            df_trades = pd.DataFrame(trades_data)
            
            # حساب الإحصائيات
            winning_trades = len(df_trades[df_trades['pnl_pct'] > 0])
            total_trades = len(df_trades)
            
            stats = {
                'balance': float(self.balance),
                'total_return': float(((self.balance - self.initial_balance) / self.initial_balance * 100)),
                'total_trades': total_trades,
                'winning_trades': winning_trades,
                'losing_trades': total_trades - winning_trades,
                'win_rate': float((winning_trades / total_trades * 100) if total_trades > 0 else 0),
                'total_pnl': float(df_trades['pnl_pct'].sum()),
                'avg_pnl': float(df_trades['pnl_pct'].mean()),
                'max_win': float(df_trades['pnl_pct'].max()),
                'max_loss': float(df_trades['pnl_pct'].min()),
                'current_position': self.positions.get('direction') if self.positions else None,
                'current_pnl': float(self.current_pnl)
            }
            
            return stats
            
        except Exception as e:
            logger.error(f"❌ خطأ في حساب الإحصائيات: {e}")
            return {}

# إنشاء Paper Trader
paper_trader = PaperTrading(INITIAL_BALANCE)

# ────────────────────────────────────────────────
# نظام Backtesting
# ────────────────────────────────────────────────

def run_backtest(days: int = 30) -> BacktestResult:
    """تشغيل Backtest على البيانات التاريخية"""
    logger.info(f"🚀 بدء Backtest لـ {days} يوم...")
    
    try:
        # جلب البيانات التاريخية
        df_xrp = data_manager.fetch_historical_data(SYMBOL_XRP, days)
        df_ada = data_manager.fetch_historical_data(SYMBOL_ADA, days)
        
        if df_xrp.empty or df_ada.empty:
            logger.error("❌ فشل في جلب البيانات التاريخية")
            return BacktestResult(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, [])
        
        # حساب المؤشرات
        df = compute_indicators(df_xrp, df_ada)
        
        if df.empty:
            logger.error("❌ فشل في حساب المؤشرات")
            return BacktestResult(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, [])
        
        logger.info(f"📊 بيانات Backtest: {len(df)} سجلة")
        
        # متغيرات Backtest
        backtest_trades = []
        current_position = None
        entry_data = {}
        entry_index = 0
        equity = [INITIAL_BALANCE]
        returns = []
        
        # تشغيل المحاكاة
        for i in range(len(df)):
            current_row = df.iloc[i]
            current_time = df.index[i]
            
            current_data = {
                'ratio': float(current_row['ratio']),
                'z': float(current_row['z']),
                'timestamp': current_time
            }
            
            # إذا كان هناك مركز مفتوح
            if current_position:
                # حساب الأيام المنقضية
                entry_time = entry_data.get('timestamp')
                days_held = (current_time - entry_time).total_seconds() / 86400 if entry_time else 0
                
                # فحص شروط الخروج
                should_exit, exit_reason, pnl_pct = check_exit_conditions(
                    current_position, 
                    entry_data, 
                    current_data,
                    days_held
                )
                
                if should_exit:
                    # تحديث الرصيد (10% من الرصيد لكل صفقة)
                    position_value = equity[-1] * 0.1
                    equity.append(equity[-1] + (pnl_pct / 100) * position_value)
                    returns.append(pnl_pct)
                    
                    # تسجيل الصفقة
                    trade = Trade(
                        entry_time=entry_data['timestamp'],
                        exit_time=current_time,
                        direction=current_position,
                        entry_ratio=entry_data['ratio'],
                        exit_ratio=current_data['ratio'],
                        pnl_pct=pnl_pct,
                        reason=exit_reason,
                        entry_z=entry_data.get('z', 0),
                        exit_z=current_data['z']
                    )
                    backtest_trades.append(trade)
                    
                    # تحديث القائمة العالمية
                    global trades
                    trades.append(trade)
                    
                    logger.debug(f"📊 خروج: {exit_reason} | PnL: {pnl_pct:.2f}%")
                    
                    # إعادة تعيين المتغيرات
                    current_position = None
                    entry_data = {}
            
            # فحص إشارة الدخول (إذا لم يكن هناك مركز)
            if not current_position:
                signal, signal_data = generate_signal(df, i)
                if signal:
                    current_position = signal
                    entry_data = signal_data.copy()
                    entry_index = i
                    logger.debug(f"📊 دخول: {signal} عند ratio={signal_data['ratio']:.4f}")
        
        # إغلاق المركز المفتوح إذا كان موجوداً في النهاية
        if current_position and entry_data:
            last_row = df.iloc[-1]
            last_time = df.index[-1]
            
            last_data = {
                'ratio': float(last_row['ratio']),
                'z': float(last_row['z']),
                'timestamp': last_time
            }
            
            entry_time = entry_data.get('timestamp')
            days_held = (last_time - entry_time).total_seconds() / 86400 if entry_time else 0
            
            should_exit, exit_reason, pnl_pct = check_exit_conditions(
                current_position, 
                entry_data, 
                last_data,
                days_held
            )
            
            # إغلاق بالقوة إذا لم يكن هناك خروج
            if not should_exit:
                exit_reason = "إغلاق عند نهاية Backtest"
                if current_position == 'long_ada_short_xrp':
                    pnl_pct = (entry_data['ratio'] - last_data['ratio']) / entry_data['ratio'] * 100
                else:
                    pnl_pct = (last_data['ratio'] - entry_data['ratio']) / entry_data['ratio'] * 100
            
            # تحديث الرصيد
            position_value = equity[-1] * 0.1
            equity.append(equity[-1] + (pnl_pct / 100) * position_value)
            returns.append(pnl_pct)
            
            # تسجيل الصفقة
            trade = Trade(
                entry_time=entry_data['timestamp'],
                exit_time=last_time,
                direction=current_position,
                entry_ratio=entry_data['ratio'],
                exit_ratio=last_data['ratio'],
                pnl_pct=pnl_pct,
                reason=exit_reason,
                entry_z=entry_data.get('z', 0),
                exit_z=last_data['z']
            )
            backtest_trades.append(trade)
            
            # تحديث القائمة العالمية
            trades.append(trade)
        
        # حساب الإحصائيات
        if backtest_trades:
            # تحويل الصفقات إلى DataFrame
            trades_data = []
            for trade in backtest_trades:
                trades_data.append(trade.to_dict())
            
            df_trades = pd.DataFrame(trades_data)
            
            # حساب Sharpe Ratio
            if returns:
                returns_series = pd.Series(returns)
                if returns_series.std() > 0:
                    sharpe = (returns_series.mean() / returns_series.std()) * np.sqrt(365/12)
                else:
                    sharpe = 0
            else:
                sharpe = 0
            
            # حساب Maximum Drawdown
            equity_series = pd.Series(equity)
            rolling_max = equity_series.expanding().max()
            drawdowns = (equity_series - rolling_max) / rolling_max * 100
            max_dd = drawdowns.min() if not drawdowns.empty else 0
            
            # إحصائيات أخرى
            winning_trades = len(df_trades[df_trades['pnl_pct'] > 0])
            total_trades = len(df_trades)
            
            result = BacktestResult(
                total_trades=total_trades,
                winning_trades=winning_trades,
                losing_trades=total_trades - winning_trades,
                win_rate=(winning_trades / total_trades * 100) if total_trades > 0 else 0,
                total_pnl=float(df_trades['pnl_pct'].sum()),
                avg_pnl=float(df_trades['pnl_pct'].mean()),
                max_win=float(df_trades['pnl_pct'].max()),
                max_loss=float(df_trades['pnl_pct'].min()),
                sharpe_ratio=float(sharpe),
                max_drawdown=float(max_dd),
                trades=backtest_trades
            )
        else:
            result = BacktestResult(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, [])
        
        logger.info(
            f"✅ اكتمل Backtest: {result.total_trades} صفقة | "
            f"معدل الربح: {result.win_rate:.1f}% | "
            f"إجمالي PnL: {result.total_pnl:.2f}%"
        )
        
        return result
        
    except Exception as e:
        logger.error(f"❌ خطأ في Backtest: {e}", exc_info=True)
        return BacktestResult(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, [])

# ────────────────────────────────────────────────
# دورة التداول الرئيسية
# ────────────────────────────────────────────────

def trading_loop():
    """الدورة الرئيسية للتداول (Live/Paper)"""
    logger.info("🔄 بدء حلقة التداول...")
    
    while True:
        try:
            if TRADING_MODE == TradingMode.PAPER:
                handle_paper_trading()
            elif TRADING_MODE == TradingMode.LIVE:
                handle_live_trading()
            else:
                # في وضع Backtest، لا نحتاج للحلقة
                time.sleep(60)
                continue
            
            # انتظار الفاصل الزمني
            time.sleep(LOOP_INTERVAL_SECONDS)
            
        except Exception as e:
            logger.error(f"❌ خطأ في حلقة التداول: {e}", exc_info=True)
            time.sleep(60)

def handle_paper_trading():
    """معالجة Paper Trading"""
    try:
        # جلب البيانات الحية
        df_xrp = data_manager.fetch_live_data(SYMBOL_XRP)
        df_ada = data_manager.fetch_live_data(SYMBOL_ADA)
        
        if df_xrp.empty or df_ada.empty:
            logger.warning("⚠️ لا توجد بيانات حية متاحة")
            return
        
        # حساب المؤشرات
        df = compute_indicators(df_xrp, df_ada)
        
        if df.empty:
            return
        
        latest = df.iloc[-1]
        current_time = df.index[-1]
        
        current_data = {
            'ratio': float(latest['ratio']),
            'z': float(latest['z']),
            'timestamp': current_time
        }
        
        # إذا كان هناك مركز مفتوح
        if paper_trader.positions:
            position = paper_trader.positions
            entry_data = {
                'ratio': position['entry_ratio'],
                'z': position['entry_z'],
                'timestamp': position['entry_time']
            }
            
            # حساب الأيام المنقضية
            days_held = (current_time - position['entry_time']).total_seconds() / 86400
            
            # فحص شروط الخروج
            should_exit, exit_reason, pnl_pct = check_exit_conditions(
                position['direction'],
                entry_data,
                current_data,
                days_held
            )
            
            if should_exit:
                paper_trader.exit_position(
                    current_data['ratio'],
                    current_data['z'],
                    exit_reason,
                    current_time
                )
            else:
                # تحديث PnL الحالي
                current_pnl = paper_trader.update_position(
                    current_data['ratio'],
                    current_data['z']
                )
                logger.debug(f"📊 PnL حالي: {current_pnl:.2f}%")
        
        # فحص إشارة الدخول
        else:
            signal, signal_data = generate_signal(df, -1)
            if signal:
                paper_trader.enter_position(
                    signal,
                    signal_data['ratio'],
                    signal_data['z'],
                    signal_data['timestamp']
                )
                
    except Exception as e:
        logger.error(f"❌ خطأ في Paper Trading: {e}")

def handle_live_trading():
    """معالجة التداول الحقيقي"""
    logger.warning("⚠️ وضع Live Trading غير مفعّل حالياً")
    # يمكن إضافة منطق التداول الحقيقي هنا لاحقاً

# ────────────────────────────────────────────────
# تطبيق Flask
# ────────────────────────────────────────────────

app = Flask(__name__)

# تعطيل تسجيل Flask الافتراضي
import logging as flask_logging
flask_logging.getLogger('werkzeug').setLevel(flask_logging.ERROR)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>نظام تداول XRP/ADA</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { 
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        .glass-card {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            border: 1px solid rgba(255, 255, 255, 0.2);
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
        }
        .navbar {
            background: rgba(255, 255, 255, 0.95) !important;
            backdrop-filter: blur(10px);
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
        }
        .stat-card {
            transition: transform 0.3s, box-shadow 0.3s;
            border: none;
            border-radius: 10px;
            overflow: hidden;
        }
        .stat-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 10px 20px rgba(0, 0, 0, 0.15) !important;
        }
        .profit { 
            color: #10b981 !important; 
            font-weight: 700;
        }
        .loss { 
            color: #ef4444 !important; 
            font-weight: 700;
        }
        .badge-mode {
            font-size: 0.75rem;
            padding: 5px 10px;
            border-radius: 20px;
        }
        .table-hover tbody tr:hover {
            background-color: rgba(59, 130, 246, 0.05);
        }
        .btn-glow {
            transition: all 0.3s;
            border: none;
            font-weight: 600;
        }
        .btn-glow:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0, 0, 0, 0.2);
        }
        .page-title {
            color: white;
            text-shadow: 0 2px 4px rgba(0,0,0,0.3);
            font-weight: 700;
        }
    </style>
</head>
<body>
    <!-- شريط التنقل -->
    <nav class="navbar navbar-expand-lg navbar-light mb-4">
        <div class="container">
            <a class="navbar-brand fw-bold" href="#">
                🤖 <span class="text-primary">نظام التداول الآلي</span>
            </a>
            <div class="d-flex align-items-center">
                <span class="badge badge-mode bg-{{ 'success' if mode=='live' else 'warning' if mode=='paper' else 'info' }} me-3">
                    {{ mode|upper }}
                </span>
                <span class="text-muted">{{ current_time }}</span>
            </div>
        </div>
    </nav>

    <div class="container">
        <!-- بطاقة معلومات النظام -->
        <div class="row mb-4">
            <div class="col-12">
                <div class="glass-card p-4">
                    <div class="row align-items-center">
                        <div class="col-md-8">
                            <h3 class="mb-1">📊 نظام تداول XRP/ADA</h3>
                            <p class="text-muted mb-0">استراتيجية Z-Score مع Bollinger Bands</p>
                        </div>
                        <div class="col-md-4 text-end">
                            <h4 class="mb-0">💰 ${{ "%.2f"|format(initial_balance) }}</h4>
                            <small class="text-muted">الرصيد الأولي</small>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- إحصائيات سريعة -->
        <div class="row mb-4">
            <div class="col-md-3 col-6 mb-3">
                <div class="stat-card bg-white p-3 shadow-sm">
                    <div class="d-flex justify-content-between align-items-center">
                        <div>
                            <h6 class="text-muted mb-1">إجمالي الصفقات</h6>
                            <h3 class="mb-0">{{ stats.total_trades }}</h3>
                        </div>
                        <div class="icon bg-primary rounded-circle p-2">
                            <span class="text-white">📈</span>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="col-md-3 col-6 mb-3">
                <div class="stat-card bg-white p-3 shadow-sm">
                    <div class="d-flex justify-content-between align-items-center">
                        <div>
                            <h6 class="text-muted mb-1">معدل الربح</h6>
                            <h3 class="mb-0 {{ 'profit' if stats.win_rate > 50 else 'loss' }}">
                                {{ "%.1f"|format(stats.win_rate) }}%
                            </h3>
                        </div>
                        <div class="icon {{ 'bg-success' if stats.win_rate > 50 else 'bg-danger' }} rounded-circle p-2">
                            <span class="text-white">🎯</span>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="col-md-3 col-6 mb-3">
                <div class="stat-card bg-white p-3 shadow-sm">
                    <div class="d-flex justify-content-between align-items-center">
                        <div>
                            <h6 class="text-muted mb-1">إجمالي PnL</h6>
                            <h3 class="mb-0 {{ 'profit' if stats.total_pnl > 0 else 'loss' }}">
                                {{ "%.2f"|format(stats.total_pnl) }}%
                            </h3>
                        </div>
                        <div class="icon {{ 'bg-success' if stats.total_pnl > 0 else 'bg-danger' }} rounded-circle p-2">
                            <span class="text-white">💰</span>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="col-md-3 col-6 mb-3">
                <div class="stat-card bg-white p-3 shadow-sm">
                    <div class="d-flex justify-content-between align-items-center">
                        <div>
                            <h6 class="text-muted mb-1">المركز الحالي</h6>
                            <h5 class="mb-0">
                                {% if paper_stats.current_position %}
                                    <span class="badge {{ 'bg-success' if 'long' in paper_stats.current_position else 'bg-danger' }}">
                                        {{ paper_stats.current_position }}
                                    </span>
                                {% else %}
                                    <span class="badge bg-secondary">لا يوجد</span>
                                {% endif %}
                            </h5>
                        </div>
                        <div class="icon bg-info rounded-circle p-2">
                            <span class="text-white">⚡</span>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- إحصائيات Paper Trading -->
        {% if mode == 'paper' and paper_stats %}
        <div class="row mb-4">
            <div class="col-12">
                <div class="glass-card p-4">
                    <h5 class="mb-3">📝 إحصائيات Paper Trading</h5>
                    <div class="row">
                        <div class="col-md-2 col-6 mb-2">
                            <small class="text-muted">الرصيد الحالي</small>
                            <h5 class="{{ 'profit' if paper_stats.balance > initial_balance else 'loss' }}">
                                ${{ "%.2f"|format(paper_stats.balance) }}
                            </h5>
                        </div>
                        <div class="col-md-2 col-6 mb-2">
                            <small class="text-muted">العائد الإجمالي</small>
                            <h5 class="{{ 'profit' if paper_stats.total_return > 0 else 'loss' }}">
                                {{ "%.2f"|format(paper_stats.total_return) }}%
                            </h5>
                        </div>
                        <div class="col-md-2 col-6 mb-2">
                            <small class="text-muted">متوسط PnL</small>
                            <h5 class="{{ 'profit' if paper_stats.avg_pnl > 0 else 'loss' }}">
                                {{ "%.2f"|format(paper_stats.avg_pnl) }}%
                            </h5>
                        </div>
                        <div class="col-md-2 col-6 mb-2">
                            <small class="text-muted">أفضل صفقة</small>
                            <h5 class="profit">{{ "%.2f"|format(paper_stats.max_win) }}%</h5>
                        </div>
                        <div class="col-md-2 col-6 mb-2">
                            <small class="text-muted">أسوأ صفقة</small>
                            <h5 class="loss">{{ "%.2f"|format(paper_stats.max_loss) }}%</h5>
                        </div>
                        <div class="col-md-2 col-6 mb-2">
                            <small class="text-muted">PnL الحالي</small>
                            <h5 class="{{ 'profit' if paper_stats.current_pnl > 0 else 'loss' }}">
                                {{ "%.2f"|format(paper_stats.current_pnl) }}%
                            </h5>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        {% endif %}

        <!-- أدوات التحكم -->
        <div class="row mb-4">
            <div class="col-12">
                <div class="glass-card p-4">
                    <h5 class="mb-3">🎮 أدوات التحكم</h5>
                    <div class="row g-2">
                        <div class="col-md-2 col-6">
                            <button class="btn btn-primary btn-glow w-100" onclick="runBacktest(7)">
                                🔄 7 أيام
                            </button>
                        </div>
                        <div class="col-md-2 col-6">
                            <button class="btn btn-primary btn-glow w-100" onclick="runBacktest(30)">
                                🔄 30 يوم
                            </button>
                        </div>
                        <div class="col-md-2 col-6">
                            <button class="btn btn-primary btn-glow w-100" onclick="runBacktest(90)">
                                🔄 90 يوم
                            </button>
                        </div>
                        <div class="col-md-2 col-6">
                            <button class="btn btn-success btn-glow w-100" onclick="switchMode('paper')">
                                📝 Paper
                            </button>
                        </div>
                        <div class="col-md-2 col-6">
                            <button class="btn btn-warning btn-glow w-100" onclick="refreshPage()">
                                🔄 تحديث
                            </button>
                        </div>
                        <div class="col-md-2 col-6">
                            <button class="btn btn-danger btn-glow w-100" onclick="clearTrades()">
                                🗑️ مسح الصفقات
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- سجل الصفقات -->
        <div class="row">
            <div class="col-12">
                <div class="glass-card p-4">
                    <div class="d-flex justify-content-between align-items-center mb-3">
                        <h5 class="mb-0">📋 سجل الصفقات</h5>
                        <span class="badge bg-secondary">{{ trades|length }} صفقة</span>
                    </div>
                    
                    {% if trades and trades|length > 0 %}
                    <div class="table-responsive">
                        <table class="table table-hover">
                            <thead class="table-light">
                                <tr>
                                    <th>التاريخ</th>
                                    <th>الاتجاه</th>
                                    <th>الدخول</th>
                                    <th>الخروج</th>
                                    <th>Z الدخول</th>
                                    <th>Z الخروج</th>
                                    <th>PnL</th>
                                    <th>السبب</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for trade in trades %}
                                <tr>
                                    <td>
                                        <small>{{ trade.entry_time|safe }}</small>
                                    </td>
                                    <td>
                                        {% if 'long' in trade.direction %}
                                        <span class="badge bg-success">Long ADA</span>
                                        {% else %}
                                        <span class="badge bg-danger">Short ADA</span>
                                        {% endif %}
                                    </td>
                                    <td>{{ "%.4f"|format(trade.entry_ratio) }}</td>
                                    <td>{{ "%.4f"|format(trade.exit_ratio) }}</td>
                                    <td>{{ "%.2f"|format(trade.entry_z) }}</td>
                                    <td>{{ "%.2f"|format(trade.exit_z) }}</td>
                                    <td>
                                        <span class="{{ 'profit' if trade.pnl_pct > 0 else 'loss' }}">
                                            {{ "%.2f"|format(trade.pnl_pct) }}%
                                        </span>
                                    </td>
                                    <td>
                                        <span class="badge bg-info">{{ trade.reason }}</span>
                                    </td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                    {% else %}
                    <div class="text-center py-5">
                        <div class="mb-3">
                            <span style="font-size: 3rem;">📭</span>
                        </div>
                        <h5 class="text-muted">لا توجد صفقات مكتملة بعد</h5>
                        <p class="text-muted">اضغط على أحد أزرار Backtest لبدء الاختبار</p>
                    </div>
                    {% endif %}
                </div>
            </div>
        </div>

        <!-- تذييل الصفحة -->
        <footer class="mt-4 text-center">
            <p class="text-white">
                نظام تداول XRP/ADA | إصدار 2.0 | 
                <small>آخر تحديث: {{ current_time }}</small>
            </p>
        </footer>
    </div>

    <script>
        function runBacktest(days) {
            if (confirm(`هل تريد تشغيل Backtest لـ ${days} يوم؟`)) {
                showLoading();
                fetch(`/api/backtest/${days}`)
                    .then(response => response.json())
                    .then(data => {
                        hideLoading();
                        if (data.success) {
                            const result = data.result;
                            alert(
                                `✅ تم تشغيل Backtest بنجاح\n\n` +
                                `الصفقات: ${result.total_trades}\n` +
                                `معدل الربح: ${result.win_rate.toFixed(1)}%\n` +
                                `إجمالي PnL: ${result.total_pnl.toFixed(2)}%\n` +
                                `Sharpe Ratio: ${result.sharpe_ratio.toFixed(2)}`
                            );
                            location.reload();
                        } else {
                            alert('❌ فشل تشغيل Backtest: ' + data.error);
                        }
                    })
                    .catch(error => {
                        hideLoading();
                        alert('❌ خطأ في الاتصال بالخادم');
                    });
            }
        }

        function switchMode(newMode) {
            if (confirm(`هل تريد التبديل إلى وضع ${newMode.toUpperCase()}؟`)) {
                fetch(`/api/set_mode/${newMode}`)
                    .then(response => response.json())
                    .then(data => {
                        if (data.success) {
                            alert(`✅ تم التبديل إلى وضع ${newMode.toUpperCase()}`);
                            location.reload();
                        } else {
                            alert('❌ فشل تبديل الوضع: ' + data.error);
                        }
                    });
            }
        }

        function refreshPage() {
            location.reload();
        }

        function clearTrades() {
            if (confirm('هل تريد مسح جميع الصفقات؟ هذا الإجراء لا يمكن التراجع عنه.')) {
                fetch('/api/clear_trades')
                    .then(response => response.json())
                    .then(data => {
                        if (data.success) {
                            alert('✅ تم مسح جميع الصفقات');
                            location.reload();
                        }
                    });
            }
        }

        function showLoading() {
            const loading = document.createElement('div');
            loading.id = 'loading-overlay';
            loading.innerHTML = `
                <div style="
                    position: fixed;
                    top: 0;
                    left: 0;
                    width: 100%;
                    height: 100%;
                    background: rgba(0,0,0,0.5);
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    z-index: 9999;
                ">
                    <div style="
                        background: white;
                        padding: 30px;
                        border-radius: 10px;
                        text-align: center;
                    ">
                        <div class="spinner-border text-primary mb-3"></div>
                        <h5>جاري المعالجة...</h5>
                        <p>يرجى الانتظار</p>
                    </div>
                </div>
            `;
            document.body.appendChild(loading);
        }

        function hideLoading() {
            const loading = document.getElementById('loading-overlay');
            if (loading) {
                loading.remove();
            }
        }

        // تحديث الوقت كل دقيقة
        function updateTime() {
            const now = new Date();
            const timeElements = document.querySelectorAll('.current-time');
            timeElements.forEach(el => {
                el.textContent = now.toLocaleTimeString('ar-SA');
            });
        }
        
        setInterval(updateTime, 60000);
        updateTime();
    </script>
</body>
</html>
"""

@app.route('/')
def dashboard():
    """لوحة التحكم الرئيسية"""
    try:
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # حساب الإحصائيات
        total_trades = len(trades)
        if total_trades > 0:
            winning_trades = len([t for t in trades if t.pnl_pct > 0])
            losing_trades = total_trades - winning_trades
            win_rate = (winning_trades / total_trades) * 100
            total_pnl = sum(t.pnl_pct for t in trades)
            avg_pnl = total_pnl / total_trades if total_trades > 0 else 0
        else:
            winning_trades = losing_trades = 0
            win_rate = total_pnl = avg_pnl = 0
        
        stats = {
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': win_rate,
            'total_pnl': total_pnl,
            'avg_pnl': avg_pnl
        }
        
        # بيانات Paper Trading
        paper_stats = {}
        if TRADING_MODE == TradingMode.PAPER:
            paper_stats = paper_trader.get_stats()
        
        # تحويل التواريخ في الصفقات لسلسلة نصية
        safe_trades = []
        for trade in trades:
            trade_dict = trade.to_dict()
            safe_trades.append(trade_dict)
        
        logger.info(f"تحميل لوحة التحكم: {len(safe_trades)} صفقة")
        
        return render_template_string(
            HTML_TEMPLATE,
            mode=TRADING_MODE,
            current_time=current_time,
            initial_balance=INITIAL_BALANCE,
            stats=stats,
            paper_stats=paper_stats,
            trades=safe_trades
        )
        
    except Exception as e:
        logger.error(f"❌ خطأ في لوحة التحكم: {e}", exc_info=True)
        return """
        <!DOCTYPE html>
        <html>
        <head>
            <title>خطأ</title>
            <style>
                body { 
                    font-family: system-ui, sans-serif; 
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    height: 100vh;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                }
                .error-box {
                    background: white;
                    padding: 40px;
                    border-radius: 15px;
                    text-align: center;
                    box-shadow: 0 10px 30px rgba(0,0,0,0.2);
                    max-width: 500px;
                }
            </style>
        </head>
        <body>
            <div class="error-box">
                <h1 style="color: #ef4444;">⚠️ حدث خطأ</h1>
                <p style="color: #666; margin: 20px 0;">تفاصيل الخطأ: """ + str(e) + """</p>
                <a href="/" style="
                    display: inline-block;
                    background: #3b82f6;
                    color: white;
                    padding: 10px 20px;
                    border-radius: 5px;
                    text-decoration: none;
                    font-weight: bold;
                ">↻ إعادة تحميل</a>
            </div>
        </body>
        </html>
        """, 500

# ────────────────────────────────────────────────
# واجهات API
# ────────────────────────────────────────────────

@app.route('/api/backtest/<int:days>')
def api_backtest(days):
    """واجهة API لـ Backtest"""
    try:
        if days not in [7, 30, 90]:
            return jsonify({
                'success': False,
                'error': 'المدة يجب أن تكون 7, 30, أو 90 يوم'
            })
        
        result = run_backtest(days)
        
        return jsonify({
            'success': True,
            'result': result.to_dict(),
            'message': f'تم إكمال Backtest لـ {days} يوم'
        })
        
    except Exception as e:
        logger.error(f"❌ خطأ في Backtest API: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e)
        })

@app.route('/api/set_mode/<mode>')
def api_set_mode(mode):
    """تغيير وضع التداول"""
    global TRADING_MODE
    
    try:
        if mode not in [TradingMode.BACKTEST, TradingMode.PAPER, TradingMode.LIVE]:
            return jsonify({
                'success': False,
                'error': 'وضع غير صالح'
            })
        
        TRADING_MODE = mode
        logger.mode = mode.upper()
        logger.info(f"🔄 تغيير وضع التداول إلى: {mode}")
        
        # إعادة تعيين Paper Trader إذا لزم
        if mode == TradingMode.PAPER:
            global paper_trader
            paper_trader = PaperTrading(INITIAL_BALANCE)
        
        return jsonify({
            'success': True,
            'mode': mode,
            'message': f'تم التبديل إلى وضع {mode}'
        })
        
    except Exception as e:
        logger.error(f"❌ خطأ في تغيير الوضع: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        })

@app.route('/api/clear_trades')
def api_clear_trades():
    """مسح جميع الصفقات"""
    global trades
    
    try:
        trades.clear()
        logger.info("🗑️ تم مسح جميع الصفقات")
        
        return jsonify({
            'success': True,
            'message': 'تم مسح جميع الصفقات'
        })
        
    except Exception as e:
        logger.error(f"❌ خطأ في مسح الصفقات: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        })

@app.route('/api/health')
def api_health():
    """فحص صحة النظام"""
    try:
        return jsonify({
            'status': 'healthy',
            'timestamp': datetime.now().isoformat(),
            'mode': TRADING_MODE,
            'trades_count': len(trades),
            'paper_balance': paper_trader.balance if TRADING_MODE == TradingMode.PAPER else None,
            'version': '2.0'
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'error': str(e)
        }), 500

@app.route('/api/trades')
def api_trades():
    """الحصول على الصفقات"""
    try:
        trades_data = [trade.to_dict() for trade in trades]
        return jsonify({
            'success': True,
            'trades': trades_data,
            'count': len(trades_data)
        })
    except Exception as e:
        logger.error(f"❌ خطأ في جلب الصفقات: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        })

# ────────────────────────────────────────────────
# بدء النظام
# ────────────────────────────────────────────────

if __name__ == '__main__':
    try:
        # عرض معلومات النظام
        logger.info("=" * 60)
        logger.info("🚀 بدء نظام التداول الآلي")
        logger.info("=" * 60)
        logger.info(f"📊 وضع التشغيل: {TRADING_MODE.upper()}")
        logger.info(f"💰 الرصيد الأولي: ${INITIAL_BALANCE}")
        logger.info(f"🏦 المنصة: {EXCHANGE_TYPE.upper()}")
        logger.info(f"📈 الرموز: {SYMBOL_XRP} / {SYMBOL_ADA}")
        logger.info(f"⏰ الإطار الزمني: {TIMEFRAME}")
        logger.info("=" * 60)
        
        # بدء خيط التداول إذا لزم
        if TRADING_MODE in [TradingMode.PAPER, TradingMode.LIVE]:
            trading_thread = Thread(target=trading_loop, daemon=True)
            trading_thread.start()
            logger.info(f"✅ بدأ خيط التداول في وضع {TRADING_MODE.upper()}")
        
        # تشغيل خادم Flask
        port = int(os.environ.get('PORT', 5000))
        logger.info(f"🌐 بدء خادم Flask على المنفذ {port}")
        logger.info(f"📱 افتح المتصفح على: http://localhost:{port}")
        logger.info("=" * 60)
        
        app.run(
            host='0.0.0.0',
            port=port,
            debug=False,
            threaded=True,
            use_reloader=False
        )
        
    except KeyboardInterrupt:
        logger.info("⏹️ تم إيقاف النظام بواسطة المستخدم")
    except Exception as e:
        logger.error(f"❌ خطأ في بدء النظام: {e}", exc_info=True)
