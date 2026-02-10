# main.py
import os
import time
import logging
from datetime import datetime, timedelta
from threading import Thread
import json
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, asdict

import ccxt
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from flask import Flask, render_template_string, jsonify, request
from dotenv import load_dotenv

load_dotenv()

# ────────────────────────────────────────────────
# إعدادات الاستراتيجية (V-hybrid-2)
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
    entry_z: float
    exit_z: float

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

# ────────────────────────────────────────────────
# إعدادات النظام
# ────────────────────────────────────────────────

class TradingMode:
    BACKTEST = 'backtest'
    PAPER = 'paper'
    LIVE = 'live'

TRADING_MODE = os.getenv('TRADING_MODE', TradingMode.PAPER)
INITIAL_BALANCE = float(os.getenv('INITIAL_BALANCE', 1000.0))

# ────────────────────────────────────────────────
# إعداد Flask + logging
# ────────────────────────────────────────────────

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(mode)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log")
    ]
)
logger = logging.getLogger(__name__)

# إضافة mode إلى جميع سجلات logging
class ModeFilter(logging.Filter):
    def filter(self, record):
        record.mode = TRADING_MODE.upper()
        return True

logger.addFilter(ModeFilter())

# ────────────────────────────────────────────────
# متغيرات التداول العالمية
# ────────────────────────────────────────────────

trades: List[Trade] = []
current_position = None
entry_time = None
entry_price_ratio = None
entry_z = None
current_balance = INITIAL_BALANCE
equity_curve = []
paper_positions = {}

# ────────────────────────────────────────────────
# إدارة البيانات
# ────────────────────────────────────────────────

class DataManager:
    def __init__(self):
        self.exchange = None
        self.setup_exchange()
        
    def setup_exchange(self):
        """إعداد اتصال بالتبادل"""
        try:
            if TRADING_MODE in [TradingMode.PAPER, TradingMode.LIVE]:
                self.exchange = ccxt.binance({
                    'apiKey': os.getenv('BINANCE_TESTNET_API_KEY'),
                    'secret': os.getenv('BINANCE_TESTNET_SECRET'),
                    'enableRateLimit': True,
                    'options': {
                        'defaultType': 'future',
                        'test': True,
                    }
                })
                logger.info(f"✅ تم الاتصال بـ Binance Futures Testnet (وضع: {TRADING_MODE})")
        except Exception as e:
            logger.error(f"❌ خطأ في الاتصال: {e}")
            
    def fetch_historical_data(self, symbol: str, days: int = 30) -> pd.DataFrame:
        """جلب بيانات تاريخية للـ Backtesting"""
        try:
            since = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
            
            # استخدام ccxt لجلب البيانات
            temp_exchange = ccxt.binance()
            all_ohlcv = []
            
            while True:
                ohlcv = temp_exchange.fetch_ohlcv(
                    symbol, 
                    TIMEFRAME, 
                    since=since,
                    limit=1000
                )
                if not ohlcv:
                    break
                
                all_ohlcv.extend(ohlcv)
                since = ohlcv[-1][0] + 1
                
                if len(ohlcv) < 1000:
                    break
                time.sleep(temp_exchange.rateLimit / 1000)
            
            df = pd.DataFrame(all_ohlcv, 
                             columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            
            logger.info(f"📊 تم جلب {len(df)} شمعة لـ {symbol}")
            return df
            
        except Exception as e:
            logger.error(f"❌ خطأ في جلب البيانات التاريخية: {e}")
            return pd.DataFrame()
    
    def fetch_live_data(self, symbol: str, limit: int = 300) -> pd.DataFrame:
        """جلب بيانات حية"""
        if not self.exchange:
            return pd.DataFrame()
            
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=limit)
            df = pd.DataFrame(ohlcv, 
                             columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            return df
        except Exception as e:
            logger.error(f"❌ خطأ في جلب البيانات الحية: {e}")
            return pd.DataFrame()

data_manager = DataManager()

# ────────────────────────────────────────────────
# دوال المؤشرات والإشارات
# ────────────────────────────────────────────────

def compute_indicators(df_xrp: pd.DataFrame, df_ada: pd.DataFrame) -> pd.DataFrame:
    """حساب جميع المؤشرات المطلوبة"""
    df = pd.DataFrame(index=df_xrp.index)
    df['xrp'] = df_xrp['close']
    df['ada'] = df_ada['close']
    df['ratio'] = df['xrp'] / df['ada']

    # Z-score
    df['z_mean'] = df['ratio'].rolling(Z_WINDOW).mean()
    df['z_std'] = df['ratio'].rolling(Z_WINDOW).std()
    df['z'] = (df['ratio'] - df['z_mean']) / df['z_std']

    # Bollinger Bands
    df['bb_mid'] = df['ratio'].rolling(BB_WINDOW).mean()
    df['bb_std'] = df['ratio'].rolling(BB_WINDOW).std()
    df['bb_upper'] = df['bb_mid'] + BB_STD * df['bb_std']
    df['bb_lower'] = df['bb_mid'] - BB_STD * df['bb_std']
    df['bb_width'] = df['bb_upper'] - df['bb_lower']
    df['bb_width_ma5'] = df['bb_width'].rolling(5).mean()

    return df.dropna()

def generate_signal(df: pd.DataFrame, i: int) -> Tuple[Optional[str], Dict]:
    """توليد إشارة تداول"""
    if i < max(Z_WINDOW, BB_WINDOW):
        return None, {}
    
    latest = df.iloc[i]
    
    # فلتر عرض الباند
    if latest['bb_width'] <= latest['bb_width_ma5'] * BB_WIDTH_MULTIPLIER:
        return None, {}
    
    signal_data = {
        'ratio': latest['ratio'],
        'z': latest['z'],
        'bb_upper': latest['bb_upper'],
        'bb_lower': latest['bb_lower'],
        'timestamp': df.index[i]
    }
    
    if latest['z'] < -Z_THRESHOLD or latest['ratio'] < latest['bb_lower']:
        return 'long_ada_short_xrp', signal_data
    
    if latest['z'] > Z_THRESHOLD or latest['ratio'] > latest['bb_upper']:
        return 'short_ada_long_xrp', signal_data
    
    return None, {}

def check_exit_conditions(position: str, entry_data: Dict, current_data: Dict) -> Tuple[bool, str]:
    """فحص شروط الخروج"""
    entry_ratio = entry_data['ratio']
    current_ratio = current_data['ratio']
    entry_z = entry_data['z']
    current_z = current_data['z']
    
    # حساب الربح/الخسارة
    if position == 'long_ada_short_xrp':
        pnl_pct = (entry_ratio - current_ratio) / entry_ratio * 100  # الربح عندما ratio يهبط
    else:  # short_ada_long_xrp
        pnl_pct = (current_ratio - entry_ratio) / entry_ratio * 100  # الربح عندما ratio يرتفع
    
    # Take Profit
    if pnl_pct >= TP_PCT:
        return True, f"TP {pnl_pct:.2f}%"
    
    # Stop Loss
    if pnl_pct <= SL_PCT:
        return True, f"SL {pnl_pct:.2f}%"
    
    # Stop Loss على Z-score
    if abs(current_z) <= 0.4:
        return True, "Z near mean"
    
    # Time-based exit (3 أيام)
    entry_time = entry_data.get('timestamp', datetime.now())
    current_time = current_data.get('timestamp', datetime.now())
    days_held = (current_time - entry_time).total_seconds() / 86400
    
    if days_held >= 3:
        return True, "Timeout"
    
    return False, ""

# ────────────────────────────────────────────────
# نظام Paper Trading
# ────────────────────────────────────────────────

class PaperTrading:
    def __init__(self, initial_balance: float = 1000):
        self.balance = initial_balance
        self.positions = {}
        self.trades = []
        self.equity_curve = [initial_balance]
        
    def enter_position(self, direction: str, ratio: float, z: float, timestamp: datetime):
        """فتح مركز في Paper Trading"""
        if self.positions:
            return False  # يوجد مركز مفتوح بالفعل
        
        position_value = self.balance * 0.1  # 10% من الرصيد
        self.positions = {
            'direction': direction,
            'entry_ratio': ratio,
            'entry_z': z,
            'entry_time': timestamp,
            'position_value': position_value
        }
        
        logger.info(f"📝 [Paper] دخول {direction} عند ratio={ratio:.4f}")
        return True
    
    def exit_position(self, exit_ratio: float, exit_z: float, reason: str, timestamp: datetime):
        """إغلاق المركز في Paper Trading"""
        if not self.positions:
            return 0
        
        position = self.positions
        entry_ratio = position['entry_ratio']
        
        # حساب PnL
        if position['direction'] == 'long_ada_short_xrp':
            pnl_pct = (entry_ratio - exit_ratio) / entry_ratio * 100
        else:
            pnl_pct = (exit_ratio - entry_ratio) / entry_ratio * 100
        
        pnl_amount = (pnl_pct / 100) * position['position_value']
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
        
        logger.info(f"📝 [Paper] خروج: {reason} | PnL: {pnl_pct:.2f}% | الرصيد: {self.balance:.2f}")
        
        # مسح المركز
        self.positions = {}
        return pnl_pct
    
    def get_stats(self) -> Dict:
        """الحصول على إحصائيات Paper Trading"""
        if not self.trades:
            return {}
        
        df_trades = pd.DataFrame([asdict(t) for t in self.trades])
        
        stats = {
            'balance': self.balance,
            'total_return': ((self.balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100),
            'total_trades': len(self.trades),
            'winning_trades': len(df_trades[df_trades['pnl_pct'] > 0]),
            'losing_trades': len(df_trades[df_trades['pnl_pct'] <= 0]),
            'win_rate': (len(df_trades[df_trades['pnl_pct'] > 0]) / len(self.trades) * 100 
                        if self.trades else 0),
            'total_pnl': df_trades['pnl_pct'].sum(),
            'avg_pnl': df_trades['pnl_pct'].mean(),
            'max_win': df_trades['pnl_pct'].max(),
            'max_loss': df_trades['pnl_pct'].min(),
            'current_position': self.positions.get('direction', 'None')
        }
        
        return stats

paper_trader = PaperTrading(INITIAL_BALANCE)

# ────────────────────────────────────────────────
# نظام Backtesting
# ────────────────────────────────────────────────

def run_backtest(days: int = 30) -> BacktestResult:
    """تشغيل Backtest على البيانات التاريخية"""
    logger.info(f"🚀 بدء Backtest لـ {days} يوم...")
    
    # جلب البيانات التاريخية
    df_xrp = data_manager.fetch_historical_data(SYMBOL_XRP, days)
    df_ada = data_manager.fetch_historical_data(SYMBOL_ADA, days)
    
    if df_xrp.empty or df_ada.empty:
        logger.error("❌ فشل في جلب البيانات التاريخية")
        return BacktestResult(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, [])
    
    # حساب المؤشرات
    df = compute_indicators(df_xrp, df_ada)
    
    # متغيرات Backtest
    trades = []
    current_position = None
    entry_data = {}
    equity = [INITIAL_BALANCE]
    returns = []
    
    # تشغيل المحاكاة
    for i in range(len(df)):
        current_data = {
            'ratio': df.iloc[i]['ratio'],
            'z': df.iloc[i]['z'],
            'timestamp': df.index[i]
        }
        
        # إذا كان هناك مركز مفتوح
        if current_position:
            should_exit, reason = check_exit_conditions(
                current_position, 
                entry_data, 
                current_data
            )
            
            if should_exit:
                # حساب PnL
                if current_position == 'long_ada_short_xrp':
                    pnl_pct = (entry_data['ratio'] - current_data['ratio']) / entry_data['ratio'] * 100
                else:
                    pnl_pct = (current_data['ratio'] - entry_data['ratio']) / entry_data['ratio'] * 100
                
                # تحديث الرصيد (افترض 10% من الرصيد لكل صفقة)
                position_value = equity[-1] * 0.1
                equity.append(equity[-1] + (pnl_pct / 100) * position_value)
                returns.append(pnl_pct)
                
                # تسجيل الصفقة
                trade = Trade(
                    entry_time=entry_data['timestamp'],
                    exit_time=current_data['timestamp'],
                    direction=current_position,
                    entry_ratio=entry_data['ratio'],
                    exit_ratio=current_data['ratio'],
                    pnl_pct=pnl_pct,
                    reason=reason,
                    entry_z=entry_data['z'],
                    exit_z=current_data['z']
                )
                trades.append(trade)
                
                logger.debug(f"📊 [Backtest] خروج: {reason} | PnL: {pnl_pct:.2f}%")
                current_position = None
                entry_data = {}
        
        # فحص إشارة الدخول
        if not current_position:
            signal, signal_data = generate_signal(df, i)
            if signal:
                current_position = signal
                entry_data = signal_data.copy()
                logger.debug(f"📊 [Backtest] دخول: {signal} عند ratio={signal_data['ratio']:.4f}")
    
    # حساب الإحصائيات
    if trades:
        df_trades = pd.DataFrame([asdict(t) for t in trades])
        
        # حساب Sharpe Ratio
        if returns:
            returns_series = pd.Series(returns)
            sharpe = (returns_series.mean() / returns_series.std() * np.sqrt(365/12) 
                     if returns_series.std() > 0 else 0)
        else:
            sharpe = 0
        
        # حساب Maximum Drawdown
        equity_series = pd.Series(equity)
        rolling_max = equity_series.expanding().max()
        drawdowns = (equity_series - rolling_max) / rolling_max * 100
        max_dd = drawdowns.min()
        
        result = BacktestResult(
            total_trades=len(trades),
            winning_trades=len(df_trades[df_trades['pnl_pct'] > 0]),
            losing_trades=len(df_trades[df_trades['pnl_pct'] <= 0]),
            win_rate=(len(df_trades[df_trades['pnl_pct'] > 0]) / len(trades) * 100 
                     if trades else 0),
            total_pnl=df_trades['pnl_pct'].sum(),
            avg_pnl=df_trades['pnl_pct'].mean(),
            max_win=df_trades['pnl_pct'].max(),
            max_loss=df_trades['pnl_pct'].min(),
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            trades=trades
        )
    else:
        result = BacktestResult(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, [])
    
    logger.info(f"✅ اكتمل Backtest: {result.total_trades} صفقة")
    return result

# ────────────────────────────────────────────────
# دورة التداول الرئيسية
# ────────────────────────────────────────────────

def trading_loop():
    """الدورة الرئيسية للتداول (Live/Paper)"""
    global current_position, entry_time, entry_price_ratio, entry_z
    
    while True:
        try:
            # جلب البيانات الحية
            df_xrp = data_manager.fetch_live_data(SYMBOL_XRP)
            df_ada = data_manager.fetch_live_data(SYMBOL_ADA)
            
            if df_xrp.empty or df_ada.empty:
                time.sleep(60)
                continue
            
            # حساب المؤشرات
            df = compute_indicators(df_xrp, df_ada)
            
            if len(df) < 1:
                time.sleep(LOOP_INTERVAL_SECONDS)
                continue
            
            latest = df.iloc[-1]
            current_data = {
                'ratio': latest['ratio'],
                'z': latest['z'],
                'timestamp': df.index[-1]
            }
            
            # Paper Trading Mode
            if TRADING_MODE == TradingMode.PAPER:
                handle_paper_trading(df)
            
            # Live Trading Mode
            elif TRADING_MODE == TradingMode.LIVE and data_manager.exchange:
                handle_live_trading(df)
            
            time.sleep(LOOP_INTERVAL_SECONDS)
            
        except Exception as e:
            logger.error(f"❌ خطأ في حلقة التداول: {e}")
            time.sleep(60)

def handle_paper_trading(df: pd.DataFrame):
    """معالجة Paper Trading"""
    global current_position, entry_time, entry_price_ratio, entry_z
    
    latest = df.iloc[-1]
    current_data = {
        'ratio': latest['ratio'],
        'z': latest['z'],
        'timestamp': df.index[-1]
    }
    
    # فحص إشارة الدخول
    if not paper_trader.positions:
        signal, signal_data = generate_signal(df, -1)
        if signal:
            paper_trader.enter_position(
                signal, 
                signal_data['ratio'], 
                signal_data['z'], 
                signal_data['timestamp']
            )
    
    # فحص شروط الخروج
    else:
        position = paper_trader.positions
        entry_data = {
            'ratio': position['entry_ratio'],
            'z': position['entry_z'],
            'timestamp': position['entry_time']
        }
        
        should_exit, reason = check_exit_conditions(
            position['direction'],
            entry_data,
            current_data
        )
        
        if should_exit:
            paper_trader.exit_position(
                current_data['ratio'],
                current_data['z'],
                reason,
                current_data['timestamp']
            )

def handle_live_trading(df: pd.DataFrame):
    """معالجة التداول الحقيقي (يحتاج إلى تطوير)"""
    logger.warning("⚠️ وضع التداول الحقيقي يحتاج إلى تطوير الأوامر الفعلية")
    # TODO: إضافة أوامر التداول الفعلية هنا

# ────────────────────────────────────────────────
# واجهة Flask
# ────────────────────────────────────────────────

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>نظام تداول XRP/ADA - Backtesting & Paper Trading</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <style>
        :root {
            --primary: #2c3e50;
            --secondary: #34495e;
            --success: #27ae60;
            --danger: #e74c3c;
            --warning: #f39c12;
            --info: #3498db;
        }
        body { background: #f8f9fa; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
        .navbar { background: var(--primary) !important; }
        .card { border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        .stat-card { transition: transform 0.3s; }
        .stat-card:hover { transform: translateY(-5px); }
        .mode-badge { font-size: 0.8rem; padding: 5px 10px; }
        .profit { color: var(--success); font-weight: bold; }
        .loss { color: var(--danger); font-weight: bold; }
        .table-hover tbody tr:hover { background-color: rgba(52, 152, 219, 0.1); }
    </style>
</head>
<body>
    <nav class="navbar navbar-dark mb-4">
        <div class="container">
            <span class="navbar-brand mb-0 h1">🤖 نظام تداول XRP/ADA المتقدم</span>
            <span class="badge bg-warning mode-badge">وضع التشغيل: {{ mode|upper }}</span>
        </div>
    </nav>

    <div class="container">
        <!-- معلومات النظام -->
        <div class="row mb-4">
            <div class="col-md-12">
                <div class="card">
                    <div class="card-header bg-primary text-white">
                        <h5 class="mb-0">📊 معلومات النظام</h5>
                    </div>
                    <div class="card-body">
                        <div class="row">
                            <div class="col-md-3">
                                <strong>الوضع الحالي:</strong> 
                                <span class="badge bg-{{ 'success' if mode=='live' else 'warning' if mode=='paper' else 'info' }}">
                                    {{ mode|upper }}
                                </span>
                            </div>
                            <div class="col-md-3">
                                <strong>الرصيد الأولي:</strong> ${{ "%.2f"|format(initial_balance) }}
                            </div>
                            <div class="col-md-3">
                                <strong>إجمالي الصفقات:</strong> {{ stats.total_trades }}
                            </div>
                            <div class="col-md-3">
                                <strong>معدل الربح:</strong> 
                                <span class="{{ 'profit' if stats.win_rate > 50 else 'loss' }}">
                                    {{ "%.1f"|format(stats.win_rate) }}%
                                </span>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- إحصائيات Paper Trading -->
        {% if mode == 'paper' %}
        <div class="row mb-4">
            <div class="col-md-12">
                <div class="card">
                    <div class="card-header bg-success text-white">
                        <h5 class="mb-0">📝 Paper Trading Statistics</h5>
                    </div>
                    <div class="card-body">
                        <div class="row text-center">
                            <div class="col-md-2 mb-3">
                                <div class="card stat-card bg-light">
                                    <div class="card-body">
                                        <h6 class="text-muted">الرصيد الحالي</h6>
                                        <h4 class="{{ 'profit' if paper_stats.balance > initial_balance else 'loss' }}">
                                            ${{ "%.2f"|format(paper_stats.balance) }}
                                        </h4>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-2 mb-3">
                                <div class="card stat-card bg-light">
                                    <div class="card-body">
                                        <h6 class="text-muted">إجمالي العائد</h6>
                                        <h4 class="{{ 'profit' if paper_stats.total_return > 0 else 'loss' }}">
                                            {{ "%.2f"|format(paper_stats.total_return) }}%
                                        </h4>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-2 mb-3">
                                <div class="card stat-card bg-light">
                                    <div class="card-body">
                                        <h6 class="text-muted">المركز الحالي</h6>
                                        <h6>{{ paper_stats.current_position or 'لا يوجد' }}</h6>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-2 mb-3">
                                <div class="card stat-card bg-light">
                                    <div class="card-body">
                                        <h6 class="text-muted">أفضل صفقة</h6>
                                        <h4 class="profit">{{ "%.2f"|format(paper_stats.max_win) }}%</h4>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-2 mb-3">
                                <div class="card stat-card bg-light">
                                    <div class="card-body">
                                        <h6 class="text-muted">أسوأ صفقة</h6>
                                        <h4 class="loss">{{ "%.2f"|format(paper_stats.max_loss) }}%</h4>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-2 mb-3">
                                <div class="card stat-card bg-light">
                                    <div class="card-body">
                                        <h6 class="text-muted">متوسط الربح/صفقة</h6>
                                        <h4 class="{{ 'profit' if paper_stats.avg_pnl > 0 else 'loss' }}">
                                            {{ "%.2f"|format(paper_stats.avg_pnl) }}%
                                        </h4>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        {% endif %}

        <!-- أدوات التحكم -->
        <div class="row mb-4">
            <div class="col-md-12">
                <div class="card">
                    <div class="card-header bg-info text-white">
                        <h5 class="mb-0">🎮 أدوات التحكم</h5>
                    </div>
                    <div class="card-body">
                        <div class="row">
                            <div class="col-md-4 mb-3">
                                <div class="d-grid gap-2">
                                    <button class="btn btn-primary" onclick="runBacktest(7)">
                                        🔄 تشغيل Backtest (7 أيام)
                                    </button>
                                    <button class="btn btn-secondary" onclick="runBacktest(30)">
                                        🔄 تشغيل Backtest (30 يوم)
                                    </button>
                                    <button class="btn btn-warning" onclick="runBacktest(90)">
                                        🔄 تشغيل Backtest (90 يوم)
                                    </button>
                                </div>
                            </div>
                            <div class="col-md-4 mb-3">
                                <div class="d-grid gap-2">
                                    <button class="btn btn-success" onclick="switchMode('paper')">
                                        📝 تبديل إلى Paper Trading
                                    </button>
                                    <button class="btn btn-danger" onclick="switchMode('live')">
                                        ⚡ تبديل إلى Live Trading
                                    </button>
                                </div>
                            </div>
                            <div class="col-md-4 mb-3">
                                <div class="d-grid gap-2">
                                    <button class="btn btn-outline-info" onclick="refreshData()">
                                        🔄 تحديث البيانات
                                    </button>
                                    <button class="btn btn-outline-dark" onclick="exportTrades()">
                                        📥 تصدير الصفقات
                                    </button>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- نتائج Backtest -->
        {% if backtest_result %}
        <div class="row mb-4">
            <div class="col-md-12">
                <div class="card">
                    <div class="card-header bg-dark text-white">
                        <h5 class="mb-0">📈 نتائج Backtest</h5>
                    </div>
                    <div class="card-body">
                        <div class="row">
                            <div class="col-md-6">
                                <div id="equityChart" style="height: 400px;"></div>
                            </div>
                            <div class="col-md-6">
                                <div id="pnlDistribution" style="height: 400px;"></div>
                            </div>
                        </div>
                        <div class="row mt-4">
                            <div class="col-md-12">
                                <h6>📊 إحصائيات Backtest:</h6>
                                <div class="table-responsive">
                                    <table class="table table-sm">
                                        <tr><th>إجمالي الصفقات</th><td>{{ backtest_result.total_trades }}</td></tr>
                                        <tr><th>الصفقات الرابحة</th><td>{{ backtest_result.winning_trades }}</td></tr>
                                        <tr><th>الصفقات الخاسرة</th><td>{{ backtest_result.losing_trades }}</td></tr>
                                        <tr><th>معدل الربح</th><td>{{ "%.2f"|format(backtest_result.win_rate) }}%</td></tr>
                                        <tr><th>إجمالي PnL</th><td class="{{ 'profit' if backtest_result.total_pnl > 0 else 'loss' }}">{{ "%.2f"|format(backtest_result.total_pnl) }}%</td></tr>
                                        <tr><th>متوسط PnL/صفقة</th><td class="{{ 'profit' if backtest_result.avg_pnl > 0 else 'loss' }}">{{ "%.2f"|format(backtest_result.avg_pnl) }}%</td></tr>
                                        <tr><th>Sharpe Ratio</th><td>{{ "%.2f"|format(backtest_result.sharpe_ratio) }}</td></tr>
                                        <tr><th>أقصى خسارة متتالية</th><td class="loss">{{ "%.2f"|format(backtest_result.max_drawdown) }}%</td></tr>
                                    </table>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        {% endif %}

        <!-- سجل الصفقات -->
        <div class="row">
            <div class="col-md-12">
                <div class="card">
                    <div class="card-header bg-secondary text-white">
                        <h5 class="mb-0">📋 سجل الصفقات</h5>
                    </div>
                    <div class="card-body">
                        <div class="table-responsive">
                            <table class="table table-hover">
                                <thead>
                                    <tr>
                                        <th>تاريخ الدخول</th>
                                        <th>الاتجاه</th>
                                        <th>الدخول Ratio</th>
                                        <th>تاريخ الخروج</th>
                                        <th>الخروج Ratio</th>
                                        <th>Z الدخول</th>
                                        <th>Z الخروج</th>
                                        <th>PnL %</th>
                                        <th>السبب</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {% for trade in trades %}
                                    <tr>
                                        <td>{{ trade.entry_time.strftime('%Y-%m-%d %H:%M') }}</td>
                                        <td>
                                            {% if trade.direction == 'long_ada_short_xrp' %}
                                            <span class="badge bg-success">Long ADA / Short XRP</span>
                                            {% else %}
                                            <span class="badge bg-danger">Short ADA / Long XRP</span>
                                            {% endif %}
                                        </td>
                                        <td>{{ "%.4f"|format(trade.entry_ratio) }}</td>
                                        <td>{{ trade.exit_time.strftime('%Y-%m-%d %H:%M') }}</td>
                                        <td>{{ "%.4f"|format(trade.exit_ratio) }}</td>
                                        <td>{{ "%.2f"|format(trade.entry_z) }}</td>
                                        <td>{{ "%.2f"|format(trade.exit_z) }}</td>
                                        <td class="{{ 'profit' if trade.pnl_pct > 0 else 'loss' }}">
                                            {{ "%.2f"|format(trade.pnl_pct) }}%
                                        </td>
                                        <td><span class="badge bg-info">{{ trade.reason }}</span></td>
                                    </tr>
                                    {% else %}
                                    <tr>
                                        <td colspan="9" class="text-center">لا توجد صفقات مكتملة بعد</td>
                                    </tr>
                                    {% endfor %}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <footer class="mt-4 text-center text-muted">
            <p>نظام تداول XRP/ADA Hybrid Strategy | إصدار 2.0 مع Backtesting & Paper Trading</p>
        </footer>
    </div>

    <script>
        function runBacktest(days) {
            fetch(`/backtest/${days}`)
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        alert(`✅ تم تشغيل Backtest لـ ${days} يوم\n` +
                              `النتائج: ${data.result.total_trades} صفقة\n` +
                              `إجمالي PnL: ${data.result.total_pnl.toFixed(2)}%\n` +
                              `معدل الربح: ${data.result.win_rate.toFixed(1)}%`);
                        location.reload();
                    } else {
                        alert('❌ فشل تشغيل Backtest: ' + data.error);
                    }
                });
        }

        function switchMode(mode) {
            if (confirm(`هل تريد التبديل إلى وضع ${mode.toUpperCase()}؟`)) {
                fetch(`/set_mode/${mode}`)
                    .then(response => response.json())
                    .then(data => {
                        if (data.success) {
                            alert(`✅ تم التبديل إلى وضع ${mode.toUpperCase()}`);
                            location.reload();
                        }
                    });
            }
        }

        function refreshData() {
            location.reload();
        }

        function exportTrades() {
            fetch('/export_trades')
                .then(response => response.blob())
                .then(blob => {
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = 'trades_export.csv';
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                });
        }

        // عرض المخططات إذا كان هناك نتائج Backtest
        {% if backtest_result and backtest_result.trades %}
        document.addEventListener('DOMContentLoaded', function() {
            // مخطط equity curve
            var equityData = [{
                x: Array.from({length: {{ backtest_result.trades|length + 1 }}, (_, i) => i}),
                y: [1000{% for trade in backtest_result.trades %}, 
                    {{ 1000 * (1 + trade.pnl_pct/100) }}{% endfor %}],
                type: 'scatter',
                mode: 'lines+markers',
                name: 'Equity Curve',
                line: {color: '#27ae60', width: 2}
            }];
            
            var equityLayout = {
                title: 'منحنى رأس المال',
                xaxis: {title: 'رقم الصفقة'},
                yaxis: {title: 'رأس المال ($)'},
                hovermode: 'closest'
            };
            
            Plotly.newPlot('equityChart', equityData, equityLayout);
            
            // توزيع PnL
            var pnlData = [{
                y: [{% for trade in backtest_result.trades %}{{ trade.pnl_pct }},{% endfor %}],
                type: 'histogram',
                name: 'توزيع PnL',
                marker: {color: '#3498db'}
            }];
            
            var pnlLayout = {
                title: 'توزيع الأرباح والخسائر',
                xaxis: {title: 'PnL %'},
                yaxis: {title: 'التكرار'}
            };
            
            Plotly.newPlot('pnlDistribution', pnlData, pnlLayout);
        });
        {% endif %}
    </script>
</body>
</html>
"""

@app.route('/')
def dashboard():
    """لوحة التحكم الرئيسية"""
    stats = {
        'total_trades': len(trades),
        'winning_trades': len([t for t in trades if t.pnl_pct > 0]) if trades else 0,
        'losing_trades': len([t for t in trades if t.pnl_pct <= 0]) if trades else 0,
        'win_rate': (len([t for t in trades if t.pnl_pct > 0]) / len(trades) * 100 
                    if trades else 0)
    }
    
    paper_stats = paper_trader.get_stats() if TRADING_MODE == TradingMode.PAPER else {}
    
    return render_template_string(
        HTML_TEMPLATE,
        mode=TRADING_MODE,
        initial_balance=INITIAL_BALANCE,
        stats=stats,
        paper_stats=paper_stats,
        trades=trades,
        backtest_result=None  # يمكن إضافة نتائج backtest مؤقتة هنا
    )

@app.route('/backtest/<int:days>')
def run_backtest_endpoint(days):
    """تشغيل backtest من خلال API"""
    try:
        result = run_backtest(days)
        return jsonify({
            'success': True,
            'result': {
                'total_trades': result.total_trades,
                'winning_trades': result.winning_trades,
                'losing_trades': result.losing_trades,
                'win_rate': result.win_rate,
                'total_pnl': result.total_pnl,
                'avg_pnl': result.avg_pnl,
                'max_win': result.max_win,
                'max_loss': result.max_loss,
                'sharpe_ratio': result.sharpe_ratio,
                'max_drawdown': result.max_drawdown
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/set_mode/<mode>')
def set_mode(mode):
    """تغيير وضع التداول"""
    global TRADING_MODE
    if mode in [TradingMode.BACKTEST, TradingMode.PAPER, TradingMode.LIVE]:
        TRADING_MODE = mode
        logger.info(f"🔄 تغيير وضع التداول إلى: {mode}")
        return jsonify({'success': True, 'mode': mode})
    return jsonify({'success': False, 'error': 'وضع غير صالح'})

@app.route('/export_trades')
def export_trades():
    """تصدير الصفقات إلى CSV"""
    if not trades:
        return jsonify({'success': False, 'error': 'لا توجد صفقات'})
    
    df = pd.DataFrame([asdict(t) for t in trades])
    csv = df.to_csv(index=False)
    
    from flask import Response
    return Response(
        csv,
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=trades_export.csv"}
    )

# ────────────────────────────────────────────────
# بدء النظام
# ────────────────────────────────────────────────

if __name__ == '__main__':
    # طباعة معلومات النظام
    logger.info(f"🚀 بدء نظام التداول...")
    logger.info(f"📊 وضع التشغيل: {TRADING_MODE.upper()}")
    logger.info(f"💰 الرصيد الأولي: ${INITIAL_BALANCE}")
    
    # بدء خيط التداول (للوضعين PAPER و LIVE فقط)
    if TRADING_MODE in [TradingMode.PAPER, TradingMode.LIVE]:
        trading_thread = Thread(target=trading_loop, daemon=True)
        trading_thread.start()
        logger.info(f"✅ بدأ خيط التداول في وضع {TRADING_MODE.upper()}")
    
    # تشغيل Flask
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
