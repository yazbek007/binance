# main.py
import ccxt
import pandas as pd
import numpy as np
from datetime import datetime
import time
from flask import Flask, render_template_string

app = Flask(__name__)

# ────────────────────────────────────────────────
# إعدادات الاستراتيجية
# ────────────────────────────────────────────────

SYMBOL_XRP = 'XRP/USDT'
SYMBOL_ADA = 'ADA/USDT'
TIMEFRAME = '1d'
SINCE = int(time.mktime(datetime(2025, 1, 1).timetuple()) * 1000)
LIMIT = 1000

Z_WINDOW = 10
Z_THRESHOLD = 0.55

BB_WINDOW = 17
BB_STD = 1.85

TP_PCT = 2.0
SL_PCT = -7.0
MAX_HOLD_DAYS = 3

BB_WIDTH_MULTIPLIER = 1.3

# ────────────────────────────────────────────────
# جلب البيانات
# ────────────────────────────────────────────────

exchange = ccxt.binance({'enableRateLimit': True})

def fetch_all_ohlcv(symbol, timeframe, since):
    data = []
    while True:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=LIMIT)
        if not ohlcv:
            break
        data.extend(ohlcv)
        since = ohlcv[-1][0] + 1
        time.sleep(0.5)
    return data

# جلب مرة واحدة عند بدء السيرفر
print("جاري جلب البيانات من Binance...")
xrp_data = fetch_all_ohlcv(SYMBOL_XRP, TIMEFRAME, SINCE)
ada_data = fetch_all_ohlcv(SYMBOL_ADA, TIMEFRAME, SINCE)

df_xrp = pd.DataFrame(xrp_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
df_ada = pd.DataFrame(ada_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

df_xrp['timestamp'] = pd.to_datetime(df_xrp['timestamp'], unit='ms')
df_ada['timestamp'] = pd.to_datetime(df_ada['timestamp'], unit='ms')

df = pd.DataFrame(index=df_xrp['timestamp'])
df['XRP'] = df_xrp.set_index('timestamp')['close']
df['ADA'] = df_ada.set_index('timestamp')['close']
df = df.dropna()

print(f"تم جلب {len(df)} شمعة")

# ────────────────────────────────────────────────
# حساب المؤشرات + الباكتست
# ────────────────────────────────────────────────

df['ratio'] = df['XRP'] / df['ADA']

df['z_mean'] = df['ratio'].rolling(Z_WINDOW).mean()
df['z_std'] = df['ratio'].rolling(Z_WINDOW).std()
df['z'] = (df['ratio'] - df['z_mean']) / df['z_std']

df['bb_mid'] = df['ratio'].rolling(BB_WINDOW).mean()
df['bb_std'] = df['ratio'].rolling(BB_WINDOW).std()
df['bb_upper'] = df['bb_mid'] + BB_STD * df['bb_std']
df['bb_lower'] = df['bb_mid'] - BB_STD * df['bb_std']
df['bb_width'] = df['bb_upper'] - df['bb_lower']
df['bb_width_ma5'] = df['bb_width'].rolling(5).mean()

df = df.dropna()

trades = []
position = None
entry_date = None
entry_ratio = None

for date, row in df.iterrows():
    z = row['z']
    ratio = row['ratio']
    upper = row['bb_upper']
    lower = row['bb_lower']
    width = row['bb_width']
    width_ma = row['bb_width_ma5']

    if width <= width_ma * BB_WIDTH_MULTIPLIER:
        continue

    if position:
        days_held = (date - entry_date).days
        pnl_pct = (ratio - entry_ratio) / entry_ratio * 100
        if position == 'short_ada_long_xrp':
            pnl_pct = -pnl_pct

        exit_reason = None
        if pnl_pct >= TP_PCT:
            exit_reason = f"TP {pnl_pct:.2f}%"
        elif pnl_pct <= SL_PCT:
            exit_reason = f"SL {pnl_pct:.2f}%"
        elif abs(z) <= 0.4:
            exit_reason = "Z near mean"
        elif days_held >= MAX_HOLD_DAYS:
            exit_reason = "Timeout"

        if exit_reason:
            trades.append({
                'entry': entry_date.strftime('%Y-%m-%d'),
                'exit': date.strftime('%Y-%m-%d'),
                'dir': position,
                'pnl': round(pnl_pct, 2),
                'days': days_held,
                'reason': exit_reason
            })
            position = None

    if not position:
        if z < -Z_THRESHOLD or ratio < lower:
            position = 'long_ada_short_xrp'
            entry_date = date
            entry_ratio = ratio
        elif z > Z_THRESHOLD or ratio > upper:
            position = 'short_ada_long_xrp'
            entry_date = date
            entry_ratio = ratio

# ────────────────────────────────────────────────
# صفحة الويب
# ────────────────────────────────────────────────

HTML = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<title>تقرير باكتست XRP/ADA</title>
<style>
body {font-family:Arial, sans-serif; background:#f5f5f5; padding:20px;}
h1 {color:#2c3e50;}
table {border-collapse:collapse; width:100%; margin:20px 0;}
th, td {border:1px solid #ccc; padding:10px; text-align:center;}
th {background:#34495e; color:white;}
.profit {color:#27ae60; font-weight:bold;}
.loss {color:#c0392b; font-weight:bold;}
.stats {background:#fff; padding:15px; border-radius:8px; box-shadow:0 2px 5px rgba(0,0,0,0.1);}
</style>
</head>
<body>
<h1>تقرير باكتست V-hybrid-2 (Binance بيانات حقيقية)</h1>

<div class="stats">
<p><strong>الفترة:</strong> {{ period }}</p>
<p><strong>عدد الصفقات:</strong> {{ trades|length }}</p>
<p><strong>نسبة الفوز:</strong> {{ win_rate }}</p>
<p><strong>إجمالي PnL:</strong> <span class="{% if total_pnl > 0 %}profit{% else %}loss{% endif %}">{{ total_pnl }}%</span></p>
</div>

{% if trades %}
<table>
<tr>
<th>دخول</th><th>خروج</th><th>الاتجاه</th><th>PnL %</th><th>أيام</th><th>السبب</th>
</tr>
{% for t in trades %}
<tr>
<td>{{ t.entry }}</td>
<td>{{ t.exit }}</td>
<td>{{ t.dir }}</td>
<td class="{% if t.pnl > 0 %}profit{% else %}loss{% endif %}">{{ t.pnl }}%</td>
<td>{{ t.days }}</td>
<td>{{ t.reason }}</td>
</tr>
{% endfor %}
</table>
{% else %}
<p style="color:#e74c3c;">لم يتم تنفيذ أي صفقة في هذه الفترة.</p>
{% endif %}
</body>
</html>
"""

@app.route('/')
def home():
    if not trades:
        return render_template_string(HTML,
                                     period="غير متوفر",
                                     trades=[],
                                     win_rate="غير متوفر",
                                     total_pnl=0)
    
    df_trades = pd.DataFrame(trades)
    total_pnl = df_trades['pnl'].sum()
    win_rate = (df_trades['pnl'] > 0).mean() * 100 if len(df_trades) > 0 else 0
    
    return render_template_string(HTML,
                                 period=f"{df.index[0].date()} → {df.index[-1].date()}",
                                 trades=trades,
                                 win_rate=f"{win_rate:.1f}%",
                                 total_pnl=f"{total_pnl:+.2f}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
