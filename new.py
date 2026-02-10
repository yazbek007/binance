

# main.py
import os
import time
import logging
from datetime import datetime, timedelta
from threading import Thread

import ccxt
import pandas as pd
import numpy as np
from flask import Flask, render_template_string
from dotenv import load_dotenv

load_dotenv()

# ────────────────────────────────────────────────
# إعدادات الاستراتيجية (V-hybrid-2)
# ────────────────────────────────────────────────

SYMBOL_XRP = 'XRP/USDT'
SYMBOL_ADA = 'ADA/USDT'
TIMEFRAME = '1h'
LOOP_INTERVAL_SECONDS = 3600          # فحص كل ساعة

Z_WINDOW = 10
Z_THRESHOLD = 0.55

BB_WINDOW = 17
BB_STD = 1.85

TP_PCT = 2.0
SL_PCT = -7.0
SL_Z = 1.35

BB_WIDTH_MULTIPLIER = 1.3

# ────────────────────────────────────────────────
# إعداد Flask + logging
# ────────────────────────────────────────────────

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log")
    ]
)
logger = logging.getLogger(__name__)

# متغيرات عالمية للتقرير
trades = []                     # قائمة الصفقات
current_position = None         # None / 'long_ada' / 'short_ada'
entry_time = None
entry_price_ratio = None
entry_z = None

# ────────────────────────────────────────────────
# الاتصال بـ Binance Futures Testnet
# ────────────────────────────────────────────────

exchange = ccxt.binance({
    'apiKey': os.getenv('BINANCE_TESTNET_API_KEY'),
    'secret': os.getenv('BINANCE_TESTNET_SECRET'),
    'enableRateLimit': True,
    'options': {
        'defaultType': 'future',
        'test': True,
    }
})

logger.info("تم الاتصال بـ Binance Futures Testnet")

# ────────────────────────────────────────────────
# دوال المساعدة
# ────────────────────────────────────────────────

def fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=300):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df
    except Exception as e:
        logger.error(f"خطأ في جلب البيانات {symbol}: {e}")
        return None

def compute_indicators(df_xrp, df_ada):
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

def can_enter():
    df_xrp = fetch_ohlcv(SYMBOL_XRP)
    df_ada = fetch_ohlcv(SYMBOL_ADA)
    if df_xrp is None or df_ada is None:
        return None, None

    df = compute_indicators(df_xrp, df_ada)
    if len(df) < BB_WINDOW:
        return None, None

    latest = df.iloc[-1]

    # فلتر عرض الباند
    if latest['bb_width'] <= latest['bb_width_ma5'] * BB_WIDTH_MULTIPLIER:
        return None, None

    if latest['z'] < -Z_THRESHOLD or latest['ratio'] < latest['bb_lower']:
        return 'long_ada_short_xrp', latest

    if latest['z'] > Z_THRESHOLD or latest['ratio'] > latest['bb_upper']:
        return 'short_ada_long_xrp', latest

    return None, None

# ────────────────────────────────────────────────
# خيط التشغيل الرئيسي
# ────────────────────────────────────────────────

def trading_loop():
    global current_position, entry_time, entry_price_ratio, entry_z

    while True:
        try:
            direction, data = can_enter()

            if direction and current_position is None:
                current_position = direction
                entry_time = datetime.utcnow()
                entry_price_ratio = data['ratio']
                entry_z = data['z']

                logger.info(f"دخول جديد: {direction} | ratio={entry_price_ratio:.4f} | z={entry_z:.3f}")

                # هنا تضع أوامر حقيقية في المستقبل
                # الآن مجرد تسجيل للتقرير

            # فحص الخروج (بسيط – يمكن توسيعه)
            if current_position:
                days_held = (datetime.utcnow() - entry_time).total_seconds() / 86400
                current_ratio = data['ratio'] if data is not None else entry_price_ratio

                pnl_pct = (current_ratio - entry_price_ratio) / entry_price_ratio * 100
                if current_position == 'short_ada_long_xrp':
                    pnl_pct = -pnl_pct

                exit_reason = None
                if pnl_pct >= TP_PCT:
                    exit_reason = f"TP {pnl_pct:.2f}%"
                elif pnl_pct <= SL_PCT:
                    exit_reason = f"SL {pnl_pct:.2f}%"
                elif abs(data['z']) <= 0.4:
                    exit_reason = "Z near mean"
                elif days_held >= 3:
                    exit_reason = "Timeout"

                if exit_reason:
                    trades.append({
                        'entry_time': entry_time,
                        'direction': current_position,
                        'entry_ratio': entry_price_ratio,
                        'exit_time': datetime.utcnow(),
                        'exit_ratio': current_ratio,
                        'pnl_pct': pnl_pct,
                        'reason': exit_reason
                    })

                    logger.info(f"خروج: {exit_reason} | PnL: {pnl_pct:.2f}%")

                    current_position = None
                    entry_time = None
                    entry_price_ratio = None
                    entry_z = None

            time.sleep(LOOP_INTERVAL_SECONDS)

        except Exception as e:
            logger.error(f"خطأ في الحلقة: {e}")
            time.sleep(60)

# ────────────────────────────────────────────────
# صفحة التقرير (لوحة تحكم بسيطة)
# ────────────────────────────────────────────────

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>تقرير البوت - XRP/ADA</title>
    <style>
        body { font-family: system-ui, sans-serif; background:#f8f9fa; padding:20px; }
        h1 { color:#2c3e50; }
        table { border-collapse: collapse; width:100%; margin-top:20px; }
        th, td { border:1px solid #ddd; padding:10px; text-align:center; }
        th { background:#34495e; color:white; }
        .profit { color:#27ae60; font-weight:bold; }
        .loss { color:#e74c3c; font-weight:bold; }
    </style>
</head>
<body>
    <h1>تقرير البوت - XRP/ADA Hybrid Strategy</h1>
    <p>آخر تحديث: {{ now }}</p>

    <h2>الوضع الحالي</h2>
    <p>
        المركز: <strong>{{ position if position else 'مسطح (flat)' }}</strong><br>
        عدد الصفقات المكتملة: <strong>{{ trades|length }}</strong>
    </p>

    <h2>سجل الصفقات</h2>
    {% if trades %}
    <table>
        <tr>
            <th>تاريخ الدخول</th>
            <th>الاتجاه</th>
            <th>الدخول Ratio</th>
            <th>تاريخ الخروج</th>
            <th>PnL %</th>
            <th>السبب</th>
        </tr>
        {% for trade in trades %}
        <tr>
            <td>{{ trade.entry_time.strftime('%Y-%m-%d %H:%M') }}</td>
            <td>{{ trade.direction }}</td>
            <td>{{ "%.4f"|format(trade.entry_ratio) }}</td>
            <td>{{ trade.exit_time.strftime('%Y-%m-%d %H:%M') }}</td>
            <td class="{% if trade.pnl_pct > 0 %}profit{% else %}loss{% endif %}">
                {{ "%.2f"|format(trade.pnl_pct) }}%
            </td>
            <td>{{ trade.reason }}</td>
        </tr>
        {% endfor %}
    </table>
    {% else %}
    <p>لا توجد صفقات مكتملة بعد.</p>
    {% endif %}
</body>
</html>
"""

@app.route('/')
def dashboard():
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    position_str = current_position if current_position else 'مسطح (flat)'

    return render_template_string(HTML_TEMPLATE,
                                 now=now,
                                 position=position_str,
                                 trades=trades)

# ────────────────────────────────────────────────
# بدء الخيط + تشغيل Flask
# ────────────────────────────────────────────────

if __name__ == '__main__':
    # بدء خيط التداول في الخلفية
    trading_thread = Thread(target=trading_loop, daemon=True)
    trading_thread.start()

    # تشغيل Flask
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
