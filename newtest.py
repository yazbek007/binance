# -*- coding: utf-8 -*-
"""
كود لجمع بيانات بيتكوين ساعية + تحليل أفضل/أسوأ ساعات في اليوم من ناحية السعر
التاريخ الحالي: فبراير 2026 ← يجمع آخر ~90 يوم
"""

import pandas as pd
import requests
from datetime import datetime
import matplotlib.pyplot as plt
import seaborn as sns

# ─── 1. جمع البيانات من CoinGecko ────────────────────────────────────────

def fetch_bitcoin_hourly_ohlc(days=90):
    url = "https://api.coingecko.com/api/v3/coins/bitcoin/ohlc"
    params = {
        "vs_currency": "usd",
        "days": str(days)          # 90 يوم → دقة ساعية (عادة)
    }
    
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; HourlyBTC Analyzer/1.0)"
    }
    
    try:
        response = requests.get(url, params=params, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        print("خطأ أثناء جلب البيانات:", e)
        print("الرابط المستخدم:", response.url if 'response' in locals() else url)
        return None
    
    if not data:
        print("لم يتم إرجاع بيانات")
        return None
    
    # تحويل إلى DataFrame
    df = pd.DataFrame(data, columns=["timestamp_ms", "open", "high", "low", "close"])
    df["datetime"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df = df.drop(columns=["timestamp_ms"])
    df = df.set_index("datetime")
    
    print(f"تم جلب {len(df)} شمعة ساعية")
    print("أول تاريخ  :", df.index.min())
    print("آخر تاريخ :", df.index.max())
    
    return df


# ─── 2. التحليل ─────────────────────────────────────────────────────────────

def analyze_daily_hour_patterns(df):
    if df is None or df.empty:
        print("لا توجد بيانات للتحليل")
        return None
    
    df = df.copy()
    
    # استخراج رقم الساعة (UTC)
    df["hour"] = df.index.hour
    
    # نحسب متوسط سعر الإغلاق لكل ساعة
    hourly_avg = df.groupby("hour")["close"].agg([
        "mean",
        "median",
        "count",
        "std"
    ]).round(2)
    
    hourly_avg = hourly_avg.rename(columns={
        "mean":   "متوسط_الإغلاق",
        "median": "الوسيط",
        "count":  "عدد_الملاحظات",
        "std":    "الانحراف_المعياري"
    })
    
    # ترتيب من الأقل سعراً إلى الأعلى
    sorted_low_to_high = hourly_avg.sort_values("متوسط_الإغلاق")
    
    return hourly_avg, sorted_low_to_high


# ─── 3. عرض النتائج بشكل واضح ─────────────────────────────────────────────

def print_clear_results(hourly_avg, sorted_df):
    print("\n" + "="*70)
    print("          متوسط سعر الإغلاق حسب الساعة (UTC) – آخر ~90 يوم")
    print("="*70)
    print(hourly_avg[["متوسط_الإغلاق", "الوسيط", "عدد_الملاحظات"]])
    print()
    
    print("أقل 8 ساعات سعراً (الأكثر احتمالاً للظهور بأسعار منخفضة):\n")
    print(sorted_df.head(8)[["متوسط_الإغلاق", "الوسيط", "عدد_الملاحظات"]])
    print()
    
    print("أعلى 8 ساعات سعراً (الأكثر احتمالاً للظهور بأسعار مرتفعة):\n")
    print(sorted_df.tail(8)[["متوسط_الإغلاق", "الوسيط", "عدد_الملاحظات"]].sort_values("متوسط_الإغلاق", ascending=False))
    print("\n" + "="*70)


# ─── 4. رسم بياني بسيط (اختياري) ───────────────────────────────────────────

def plot_hourly_prices(hourly_avg):
    plt.figure(figsize=(12, 6))
    sns.lineplot(x=hourly_avg.index, y=hourly_avg["متوسط_الإغلاق"], marker="o")
    plt.title("متوسط سعر إغلاق البيتكوين حسب الساعة في اليوم (UTC)", fontsize=14)
    plt.xlabel("الساعة (0–23 UTC)")
    plt.ylabel("متوسط سعر الإغلاق (USD)")
    plt.grid(True, alpha=0.3)
    plt.xticks(range(0, 24))
    plt.tight_layout()
    plt.show()


# ─── التنفيذ الرئيسي ────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("جاري جمع بيانات البيتكوين الساعية من CoinGecko...\n")
    
    btc_df = fetch_bitcoin_hourly_ohlc(days=90)
    
    if btc_df is not None:
        stats, sorted_stats = analyze_daily_hour_patterns(btc_df)
        
        if stats is not None:
            print_clear_results(stats, sorted_stats)
            # إذا أردت الرسم البياني، قم بإلغاء تعليق السطر التالي
            # plot_hourly_prices(stats)
