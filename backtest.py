# -*- coding: utf-8 -*-
"""
Backtest كامل لاستراتيجية V-hybrid-2 على بيانات Binance حقيقية
(زوج XRP/USDT و ADA/USDT - إطار زمني يومي أو ساعي)
"""

import ccxt
import pandas as pd
import numpy as np
from datetime import datetime
import time

# ────────────────────────────────────────────────
#  الإعدادات الأساسية للاستراتيجية
# ────────────────────────────────────────────────

SYMBOL_XRP = 'XRP/USDT'
SYMBOL_ADA = 'ADA/USDT'
TIMEFRAME = '1d'           # يمكن تغييره إلى '4h' أو '1h' أو '1w'
SINCE = int(time.mktime(datetime(2025, 1, 1).timetuple()) * 1000)  # من بداية 2025
LIMIT = 1000               # عدد الشموع لكل طلب (ccxt يسمح بحد أقصى معين)

# معاملات الاستراتيجية V-hybrid-2
Z_WINDOW = 10
Z_THRESHOLD = 0.55

BB_WINDOW = 17
BB_STD = 1.85

TP_PCT = 2.0
SL_PCT = -7.0
SL_Z = 1.35

BB_WIDTH_MULTIPLIER = 1.3
MAX_HOLD_DAYS = 3

# ────────────────────────────────────────────────
#  جلب البيانات من Binance
# ────────────────────────────────────────────────

exchange = ccxt.binance({
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'},  # spot للبيانات التاريخية البسيطة
})

def fetch_all_ohlcv(symbol, timeframe, since):
    """جلب كل البيانات التاريخية منذ تاريخ معين"""
    all_data = []
    while True:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=LIMIT)
        if not ohlcv:
            break
        all_data.extend(ohlcv)
        since = ohlcv[-1][0] + 1  # التاريخ التالي
        time.sleep(0.5)           # تجنب rate limit
    return all_data

print("جاري جلب بيانات XRP/USDT ...")
xrp_ohlcv = fetch_all_ohlcv(SYMBOL_XRP, TIMEFRAME, SINCE)

print("جاري جلب بيانات ADA/USDT ...")
ada_ohlcv = fetch_all_ohlcv(SYMBOL_ADA, TIMEFRAME, SINCE)

# تحويل إلى DataFrame
df_xrp = pd.DataFrame(xrp_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
df_ada = pd.DataFrame(ada_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

df_xrp['timestamp'] = pd.to_datetime(df_xrp['timestamp'], unit='ms')
df_ada['timestamp'] = pd.to_datetime(df_ada['timestamp'], unit='ms')

df_xrp.set_index('timestamp', inplace=True)
df_ada.set_index('timestamp', inplace=True)

# دمج البيانات على نفس التواريخ
df = pd.DataFrame(index=df_xrp.index)
df['XRP'] = df_xrp['close']
df['ADA'] = df_ada['close']

# حذف الصفوف التي بها NaN
df = df.dropna()

print(f"تم جلب {len(df)} شمعة من {df.index[0].date()} إلى {df.index[-1].date()}")

# ────────────────────────────────────────────────
#  حساب المؤشرات
# ────────────────────────────────────────────────

df['ratio'] = df['XRP'] / df['ADA']

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

df = df.dropna()

# ────────────────────────────────────────────────
#  محاكاة الصفقات
# ────────────────────────────────────────────────

trades = []
position = None
entry_date = None
entry_ratio = None
entry_z = None

for date, row in df.iterrows():
    z = row['z']
    ratio = row['ratio']
    upper = row['bb_upper']
    lower = row['bb_lower']
    width = row['bb_width']
    width_ma = row['bb_width_ma5']

    # فلتر العرض
    if width <= width_ma * BB_WIDTH_MULTIPLIER:
        continue

    # فحص الخروج
    if position is not None:
        days_held = (date - entry_date).days
        pnl_pct = (ratio - entry_ratio) / entry_ratio * 100
        if position == 'short_ada_long_xrp':
            pnl_pct = -pnl_pct

        exit_reason = None
        exit_price = ratio

        if pnl_pct >= TP_PCT:
            exit_reason = f"Take Profit {pnl_pct:.2f}%"
        elif pnl_pct <= SL_PCT:
            exit_reason = f"Stop Loss {pnl_pct:.2f}%"
        elif abs(z) <= 0.4:
            exit_reason = "Z near mean"
        elif days_held >= MAX_HOLD_DAYS:
            exit_reason = "Timeout"

        if exit_reason:
            trades.append({
                'entry_date': entry_date.date(),
                'exit_date': date.date(),
                'direction': position,
                'entry_ratio': round(entry_ratio, 4),
                'exit_ratio': round(exit_price, 4),
                'pnl_pct': round(pnl_pct, 2),
                'days_held': days_held,
                'reason': exit_reason
            })
            print(f"خروج: {exit_reason} | PnL: {pnl_pct:+.2f}% | {date.date()}")

            position = None

    # فحص الدخول
    if position is None:
        if (z < -Z_THRESHOLD or ratio < lower):
            position = 'long_ada_short_xrp'
            entry_date = date
            entry_ratio = ratio
            entry_z = z
            print(f"دخول LONG ADA SHORT XRP | {date.date()} | ratio={ratio:.4f} | z={z:.3f}")

        elif (z > Z_THRESHOLD or ratio > upper):
            position = 'short_ada_long_xrp'
            entry_date = date
            entry_ratio = ratio
            entry_z = z
            print(f"دخول SHORT ADA LONG XRP | {date.date()} | ratio={ratio:.4f} | z={z:.3f}")

# إغلاق الصفقة الأخيرة إن وجدت
if position is not None:
    last = df.iloc[-1]
    pnl_pct = (last['ratio'] - entry_ratio) / entry_ratio * 100
    if position == 'short_ada_long_xrp':
        pnl_pct = -pnl_pct
    trades.append({
        'entry_date': entry_date.date(),
        'exit_date': df.index[-1].date(),
        'direction': position,
        'entry_ratio': round(entry_ratio, 4),
        'exit_ratio': round(last['ratio'], 4),
        'pnl_pct': round(pnl_pct, 2),
        'days_held': (df.index[-1] - entry_date).days,
        'reason': 'نهاية البيانات'
    })

# ────────────────────────────────────────────────
#  التقرير النهائي
# ────────────────────────────────────────────────

if not trades:
    print("\nلا توجد صفقات خلال الفترة.")
else:
    df_trades = pd.DataFrame(trades)
    
    print("\n" + "═" * 80)
    print("          تقرير الباكتست الكامل - V-hybrid-2 (Binance بيانات حقيقية)")
    print("═" * 80)
    print(f"الفترة: {df.index[0].date()}  ←→  {df.index[-1].date()}")
    print(f"إطار زمني: {TIMEFRAME}     عدد الشموع: {len(df)}")
    print(f"عدد الصفقات الكاملة: {len(df_trades)}")
    print(f"نسبة الفوز: {(df_trades['pnl_pct'] > 0).mean():.1%}")
    print(f"إجمالي العائد: {df_trades['pnl_pct'].sum():+.2f}%")
    print(f"أعلى ربح صفقة: {df_trades['pnl_pct'].max():+.2f}%")
    print(f"أكبر خسارة صفقة: {df_trades['pnl_pct'].min():+.2f}%")
    print(f"متوسط الربح لكل صفقة: {df_trades['pnl_pct'].mean():+.2f}%")
    print(f"متوسط مدة الصفقة: {df_trades['days_held'].mean():.1f} يوم")
    
    print("\nسجل الصفقات التفصيلي:")
    print(df_trades.to_string(index=False))
    print("═" * 80)
