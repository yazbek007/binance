                
                
                3-
                <p><
            <p>
                <a href="/health">Health Check</a> | 
                <a href="/stats">Detailed Stats</a> |
                <a href="/config">Configuration</a>
            </p>
            <p><small>Last update: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</small></p>
        </body>
    </html>
    """

@app.route('/health')
def health():
    """Health check endpoint"""
    with state.lock:
        data_ok = len(state.klines_h4) > 100 and len(state.klines_m30) > 50

# app.py - مع واجهة مطورة كاملة

# ... (الكود السابق يبقى كما هو)

# أضف هذه الدوال الجديدة
def get_market_data():
    """Get current market data"""
    with state.lock:
        if not state.klines_m30 or not state.klines_h4:
            return {
                "price": "N/A",
                "change_24h": "N/A",
                "volume_24h": "N/A",
                "market_cap": "N/A"
            }
        
        latest_30m = state.klines_m30[-1]
        latest_h4 = state.klines_h4[-1] if state.klines_h4 else latest_30m
        
        # حساب التغير خلال 24 ساعة
        if len(state.klines_h4) >= 6:  # 6 شموع × 4 ساعات = 24 ساعة
            price_24h_ago = state.klines_h4[-6]['close'] if len(state.klines_h4) >= 6 else latest_h4['close']
            change_24h = ((latest_h4['close'] - price_24h_ago) / price_24h_ago) * 100
        else:
            change_24h = 0
        
        return {
            "symbol": SYMBOL,
            "current_price": latest_30m['close'],
            "open": latest_h4['open'],
            "high_24h": get_24h_high(),
            "low_24h": get_24h_low(),
            "volume_24h": get_24h_volume(),
            "change_24h": change_24h,
            "timestamp": datetime.utcnow().isoformat()
        }

def get_current_indicators():
    """Get current technical indicators"""
    with state.lock:
        if len(state.klines_h4) < 100 or len(state.klines_m30) < 50:
            return {"error": "Insufficient data"}
        
        try:
            df_h4 = pd.DataFrame(state.klines_h4[-100:])
            df_m30 = pd.DataFrame(state.klines_m30[-50:])
            
            numeric_cols = ['open', 'high', 'low', 'close', 'volume']
            df_h4[numeric_cols] = df_h4[numeric_cols].apply(pd.to_numeric, errors='coerce')
            df_m30[numeric_cols] = df_m30[numeric_cols].apply(pd.to_numeric, errors='coerce')
            
            df_h4 = compute_indicators(df_h4)
            last_h4 = df_h4.iloc[-1]
            
            long_strength = calculate_signal_strength(df_h4, df_m30, "LONG")
            short_strength = calculate_signal_strength(df_h4, df_m30, "SHORT")
            
            return {
                "rsi": float(last_h4['rsi']),
                "macd": float(last_h4['macd']),
                "macd_signal": float(last_h4['signal']),
                "macd_histogram": float(last_h4['hist']),
                "ema20": float(last_h4['ema20']),
                "ema50": float(last_h4['ema50']),
                "ema200": float(last_h4['ema200']),
                "atr_percent": float(last_h4.get('atr_percent', 0)),
                "volume_ratio": float(last_h4.get('volume_ratio', 0)),
                "signal_strengths": {
                    "long": long_strength.strength,
                    "short": short_strength.strength,
                    "long_confidence": long_strength.confidence,
                    "short_confidence": short_strength.confidence
                },
                "trend": "BULLISH" if last_h4['ema50'] > last_h4['ema200'] else "BEARISH",
                "momentum": "BULLISH" if last_h4['macd'] > last_h4['signal'] else "BEARISH"
            }
        except Exception as e:
            return {"error": str(e)}

def get_recent_signals(limit=10):
    """Get recent trading signals"""
    with state.lock:
        signals = state.signals_history[-limit:] if state.signals_history else []
        
        formatted_signals = []
        for sig in signals:
            strength_color = "success" if sig['strength'] >= SIGNAL_THRESHOLD else "warning" if sig['strength'] >= MEDIUM_THRESHOLD else "secondary"
            
            formatted_signals.append({
                "id": len(formatted_signals) + 1,
                "type": sig['type'],
                "strength": sig['strength'],
                "confidence": sig['confidence'],
                "price": sig['price'],
                "time": sig['time'].strftime('%Y-%m-%d %H:%M UTC'),
                "time_ago": get_time_ago(sig['time']),
                "color": strength_color,
                "icon": "📈" if "LONG" in sig['type'] else "📉"
            })
        
        return {
            "total": len(state.signals_history),
            "recent": formatted_signals,
            "stats": {
                "strong_signals": len([s for s in state.signals_history if s['strength'] >= SIGNAL_THRESHOLD]),
                "medium_signals": len([s for s in state.signals_history if MEDIUM_THRESHOLD <= s['strength'] < SIGNAL_THRESHOLD]),
                "success_rate": calculate_success_rate()
            }
        }

def get_bot_performance():
    """Get bot performance metrics"""
    with state.lock:
        if not state.signals_history:
            return {
                "uptime": get_uptime(),
                "signals_sent": 0,
                "success_rate": 0,
                "avg_strength": 0,
                "status": "COLLECTING_DATA"
            }
        
        signals = state.signals_history
        strong_signals = [s for s in signals if s['strength'] >= SIGNAL_THRESHOLD]
        
        return {
            "uptime": get_uptime(),
            "total_signals": len(signals),
            "strong_signals": len(strong_signals),
            "medium_signals": len([s for s in signals if MEDIUM_THRESHOLD <= s['strength'] < SIGNAL_THRESHOLD]),
            "avg_signal_strength": sum(s['strength'] for s in signals) / len(signals) if signals else 0,
            "success_rate": calculate_success_rate(),
            "last_signal_time": state.last_signal_time.isoformat() if state.last_signal_time else None,
            "data_collected": {
                "4h_candles": len(state.klines_h4),
                "30m_candles": len(state.klines_m30),
                "start_time": state.klines_h4[0]['time'].isoformat() if state.klines_h4 else None
            },
            "status": "ACTIVE"
        }

def get_24h_high():
    """Get 24-hour high price"""
    with state.lock:
        if len(state.klines_h4) < 6:
            return "N/A"
        
        recent_candles = state.klines_h4[-6:]
        highs = [c['high'] for c in recent_candles]
        return max(highs)

def get_24h_low():
    """Get 24-hour low price"""
    with state.lock:
        if len(state.klines_h4) < 6:
            return "N/A"
        
        recent_candles = state.klines_h4[-6:]
        lows = [c['low'] for c in recent_candles]
        return min(lows)

def get_24h_volume():
    """Get 24-hour volume"""
    with state.lock:
        if len(state.klines_h4) < 6:
            return "N/A"
        
        recent_candles = state.klines_h4[-6:]
        volumes = [c['volume'] for c in recent_candles]
        return sum(volumes)

def calculate_success_rate():
    """Calculate signal success rate (مثال مبسط)"""
    # في الإصدار الحقيقي، تحتاج لتتبع نتائج الإشارات
    return 0  # مؤقتاً

def get_time_ago(dt):
    """Get human-readable time ago"""
    now = datetime.utcnow()
    diff = now - dt
    
    if diff.days > 0:
        return f"{diff.days}d ago"
    elif diff.seconds // 3600 > 0:
        return f"{diff.seconds // 3600}h ago"
    elif diff.seconds // 60 > 0:
        return f"{diff.seconds // 60}m ago"
    else:
        return "Just now"

def get_uptime():
    """Get bot uptime"""
    if not state.klines_h4:
        return "N/A"
    
    start_time = state.klines_h4[0]['time']
    uptime = datetime.utcnow() - start_time
    
    if uptime.days > 0:
        return f"{uptime.days}d {uptime.seconds // 3600}h"
    elif uptime.seconds // 3600 > 0:
        return f"{uptime.seconds // 3600}h {(uptime.seconds % 3600) // 60}m"
    else:
        return f"{uptime.seconds // 60}m"

# Routes جديدة ومطورة
@app.route('/dashboard')
def enhanced_dashboard():
    """Enhanced dashboard with all monitoring data"""
    with state.lock:
        market_data = get_market_data()
        indicators = get_current_indicators()
        signals = get_recent_signals(15)
        performance = get_bot_performance()
        
        config_info = {
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "confirm_tf": CONFIRM_TF,
            "thresholds": {
                "medium": MEDIUM_THRESHOLD,
                "strong": SIGNAL_THRESHOLD,
                "high": HIGH_STRENGTH
            },
            "weights": WEIGHTS
        }
    
    # إنشاء HTML متقدم مع Bootstrap
    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Crypto Trading Bot - Advanced Dashboard</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body {{ background: #f8f9fa; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }}
            .card {{ border-radius: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); margin-bottom: 20px; }}
            .card-header {{ border-radius: 15px 15px 0 0 !important; font-weight: 600; }}
            .price-display {{ font-size: 2.5rem; font-weight: 700; }}
            .change-positive {{ color: #28a745; }}
            .change-negative {{ color: #dc3545; }}
            .signal-strength {{ height: 20px; border-radius: 10px; }}
            .signal-long {{ background: linear-gradient(90deg, #28a745, #20c997); }}
            .signal-short {{ background: linear-gradient(90deg, #dc3545, #fd7e14); }}
            .badge-signal {{ font-size: 0.9em; padding: 5px 10px; }}
            .table-hover tbody tr:hover {{ background-color: rgba(0,0,0,0.02); }}
            .indicator-value {{ font-weight: 600; font-size: 1.1em; }}
            .progress {{ height: 25px; border-radius: 12px; }}
            .status-active {{ color: #28a745; }}
            .status-warning {{ color: #ffc107; }}
            .status-inactive {{ color: #6c757d; }}
        </style>
    </head>
    <body>
        <nav class="navbar navbar-dark bg-dark">
            <div class="container-fluid">
                <a class="navbar-brand" href="#">
                    <i class="fas fa-robot"></i> Crypto Trading Bot
                </a>
                <div class="text-white">
                    <span class="badge bg-info">v2.0</span>
                    <span class="badge bg-success">LIVE</span>
                </div>
            </div>
        </nav>
        
        <div class="container-fluid mt-4">
            <!-- Row 1: Market Overview -->
            <div class="row">
                <div class="col-lg-12">
                    <div class="card">
                        <div class="card-header bg-primary text-white">
                            <i class="fas fa-chart-line"></i> Market Overview - {market_data['symbol']}
                        </div>
                        <div class="card-body">
                            <div class="row">
                                <div class="col-md-3 text-center">
                                    <div class="price-display {'change-positive' if market_data.get('change_24h', 0) > 0 else 'change-negative'}">
                                        ${market_data['current_price']:.2f if isinstance(market_data['current_price'], (int, float)) else market_data['current_price']}
                                    </div>
                                    <div class="mt-2">
                                        <span class="badge {'bg-success' if market_data.get('change_24h', 0) > 0 else 'bg-danger'}">
                                            24h: {market_data.get('change_24h', 0):.2f}%
                                        </span>
                                        <span class="badge bg-info">
                                            High: ${market_data.get('high_24h', 0):.2f if isinstance(market_data.get('high_24h'), (int, float)) else market_data.get('high_24h', 'N/A')}
                                        </span>
                                        <span class="badge bg-warning">
                                            Low: ${market_data.get('low_24h', 0):.2f if isinstance(market_data.get('low_24h'), (int, float)) else market_data.get('low_24h', 'N/A')}
                                        </span>
                                    </div>
                                </div>
                                
                                <div class="col-md-9">
                                    <div class="row">
                                        <div class="col-md-4">
                                            <div class="card bg-light">
                                                <div class="card-body text-center">
                                                    <h6><i class="fas fa-bolt"></i> Current Strength</h6>
                                                    <div class="mt-3">
                                                        <div class="d-flex justify-content-between mb-1">
                                                            <span>LONG</span>
                                                            <span>{indicators.get('signal_strengths', {{}}).get('long', 0)}/100</span>
                                                        </div>
                                                        <div class="progress">
                                                            <div class="progress-bar signal-long" style="width: {indicators.get('signal_strengths', {{}}).get('long', 0)}%"></div>
                                                        </div>
                                                    </div>
                                                    <div class="mt-3">
                                                        <div class="d-flex justify-content-between mb-1">
                                                            <span>SHORT</span>
                                                            <span>{indicators.get('signal_strengths', {{}}).get('short', 0)}/100</span>
                                                        </div>
                                                        <div class="progress">
                                                            <div class="progress-bar signal-short" style="width: {indicators.get('signal_strengths', {{}}).get('short', 0)}%"></div>
                                                        </div>
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                        
                                        <div class="col-md-4">
                                            <div class="card bg-light">
                                                <div class="card-body text-center">
                                                    <h6><i class="fas fa-chart-bar"></i> Volume</h6>
                                                    <h4 class="mt-3">{market_data.get('volume_24h', 0):,.0f if isinstance(market_data.get('volume_24h'), (int, float)) else market_data.get('volume_24h', 'N/A')}</h4>
                                                    <p class="text-muted">24h Volume</p>
                                                    <span class="badge {'bg-success' if indicators.get('volume_ratio', 0) > 1 else 'bg-warning'}">
                                                        Ratio: {indicators.get('volume_ratio', 0):.2f}
                                                    </span>
                                                </div>
                                            </div>
                                        </div>
                                        
                                        <div class="col-md-4">
                                            <div class="card bg-light">
                                                <div class="card-body text-center">
                                                    <h6><i class="fas fa-tachometer-alt"></i> Market Trend</h6>
                                                    <h4 class="mt-3 {'text-success' if indicators.get('trend') == 'BULLISH' else 'text-danger'}">
                                                        {indicators.get('trend', 'N/A')}
                                                    </h4>
                                                    <div class="mt-3">
                                                        <span class="badge bg-info">RSI: {indicators.get('rsi', 0):.1f}</span>
                                                        <span class="badge {'bg-success' if indicators.get('momentum') == 'BULLISH' else 'bg-danger'}">
                                                            MACD: {'BULL' if indicators.get('momentum') == 'BULLISH' else 'BEAR'}
                                                        </span>
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Row 2: Technical Indicators & Performance -->
            <div class="row">
                <!-- Technical Indicators -->
                <div class="col-lg-6">
                    <div class="card">
                        <div class="card-header bg-info text-white">
                            <i class="fas fa-cogs"></i> Technical Indicators
                        </div>
                        <div class="card-body">
                            <div class="row">
                                <div class="col-md-6">
                                    <table class="table table-sm">
                                        <tbody>
                                            <tr>
                                                <td><i class="fas fa-wave-square"></i> RSI (14)</td>
                                                <td class="text-end">
                                                    <span class="indicator-value {'text-danger' if indicators.get('rsi', 50) > 70 else 'text-success' if indicators.get('rsi', 50) < 30 else ''}">
                                                        {indicators.get('rsi', 0):.2f}
                                                    </span>
                                                </td>
                                            </tr>
                                            <tr>
                                                <td><i class="fas fa-exchange-alt"></i> MACD</td>
                                                <td class="text-end">
                                                    <span class="indicator-value {'text-success' if indicators.get('macd', 0) > indicators.get('macd_signal', 0) else 'text-danger'}">
                                                        {indicators.get('macd', 0):.6f}
                                                    </span>
                                                </td>
                                            </tr>
                                            <tr>
                                                <td><i class="fas fa-signal"></i> MACD Signal</td>
                                                <td class="text-end">{indicators.get('macd_signal', 0):.6f}</td>
                                            </tr>
                                            <tr>
                                                <td><i class="fas fa-chart-area"></i> MACD Histogram</td>
                                                <td class="text-end">
                                                    <span class="indicator-value {'text-success' if indicators.get('macd_histogram', 0) > 0 else 'text-danger'}">
                                                        {indicators.get('macd_histogram', 0):.6f}
                                                    </span>
                                                </td>
                                            </tr>
                                        </tbody>
                                    </table>
                                </div>
                                <div class="col-md-6">
                                    <table class="table table-sm">
                                        <tbody>
                                            <tr>
                                                <td><i class="fas fa-chart-line"></i> EMA 20</td>
                                                <td class="text-end">${indicators.get('ema20', 0):.2f}</td>
                                            </tr>
                                            <tr>
                                                <td><i class="fas fa-chart-line"></i> EMA 50</td>
                                                <td class="text-end">${indicators.get('ema50', 0):.2f}</td>
                                            </tr>
                                            <tr>
                                                <td><i class="fas fa-chart-line"></i> EMA 200</td>
                                                <td class="text-end">${indicators.get('ema200', 0):.2f}</td>
                                            </tr>
                                            <tr>
                                                <td><i class="fas fa-exclamation-triangle"></i> ATR %</td>
                                                <td class="text-end">
                                                    <span class="indicator-value {'text-danger' if indicators.get('atr_percent', 0) > 3 else 'text-warning'}">
                                                        {indicators.get('atr_percent', 0):.2f}%
                                                    </span>
                                                </td>
                                            </tr>
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
                
                <!-- Bot Performance -->
                <div class="col-lg-6">
                    <div class="card">
                        <div class="card-header bg-success text-white">
                            <i class="fas fa-chart-pie"></i> Bot Performance
                        </div>
                        <div class="card-body">
                            <div class="row">
                                <div class="col-md-6">
                                    <div class="text-center mb-4">
                                        <h6><i class="fas fa-clock"></i> Uptime</h6>
                                        <h3>{performance.get('uptime', 'N/A')}</h3>
                                    </div>
                                    <div class="text-center mb-4">
                                        <h6><i class="fas fa-bell"></i> Total Signals</h6>
                                        <h3>{performance.get('total_signals', 0)}</h3>
                                    </div>
                                </div>
                                <div class="col-md-6">
                                    <div class="text-center mb-4">
                                        <h6><i class="fas fa-check-circle"></i> Success Rate</h6>
                                        <h3 {'class="text-success"' if performance.get('success_rate', 0) > 50 else 'class="text-warning"'}>
                                            {performance.get('success_rate', 0)}%
                                        </h3>
                                    </div>
                                    <div class="text-center mb-4">
                                        <h6><i class="fas fa-database"></i> Data Collected</h6>
                                        <div class="mt-2">
                                            <span class="badge bg-primary">{performance.get('data_collected', {{}}).get('4h_candles', 0)} 4H</span>
                                            <span class="badge bg-secondary">{performance.get('data_collected', {{}}).get('30m_candles', 0)} 30M</span>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Row 3: Recent Signals -->
            <div class="row">
                <div class="col-lg-12">
                    <div class="card">
                        <div class="card-header bg-warning text-dark">
                            <i class="fas fa-history"></i> Recent Signals ({signals.get('total', 0)} total)
                        </div>
                        <div class="card-body">
                            <div class="table-responsive">
                                <table class="table table-hover">
                                    <thead>
                                        <tr>
                                            <th>#</th>
                                            <th>Type</th>
                                            <th>Strength</th>
                                            <th>Confidence</th>
                                            <th>Price</th>
                                            <th>Time</th>
                                            <th>Status</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {"".join([
                                            f'''
                                            <tr>
                                                <td>{sig['id']}</td>
                                                <td>
                                                    <span class="badge {'bg-success' if 'LONG' in sig['type'] else 'bg-danger'} badge-signal">
                                                        {sig['icon']} {sig['type']}
                                                    </span>
                                                </td>
                                                <td>
                                                    <div class="progress" style="height: 10px;">
                                                        <div class="progress-bar bg-{sig['color']}" style="width: {sig['strength']}%"></div>
                                                    </div>
                                                    <small class="text-muted">{sig['strength']}/100</small>
                                                </td>
                                                <td>
                                                    <span class="badge bg-{sig['color']}">
                                                        {sig['confidence']}
                                                    </span>
                                                </td>
                                                <td><strong>${sig['price']:.2f}</strong></td>
                                                <td>
                                                    <small>{sig['time']}</small><br>
                                                    <small class="text-muted">{sig['time_ago']}</small>
                                                </td>
                                                <td>
                                                    <span class="badge {'bg-success' if sig['strength'] >= SIGNAL_THRESHOLD else 'bg-warning'}">
                                                        { 'STRONG' if sig['strength'] >= SIGNAL_THRESHOLD else 'MEDIUM' }
                                                    </span>
                                                </td>
                                            </tr>
                                            ''' for sig in signals.get('recent', [])
                                        ]) if signals.get('recent') else '''
                                        <tr>
                                            <td colspan="7" class="text-center text-muted">
                                                <i class="fas fa-info-circle"></i> No signals yet. The bot is collecting data...
                                            </td>
                                        </tr>
                                        '''}
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Row 4: Configuration -->
            <div class="row">
                <div class="col-lg-12">
                    <div class="card">
                        <div class="card-header bg-secondary text-white">
                            <i class="fas fa-sliders-h"></i> Configuration
                        </div>
                        <div class="card-body">
                            <div class="row">
                                <div class="col-md-4">
                                    <h6><i class="fas fa-cog"></i> Bot Settings</h6>
                                    <ul class="list-group list-group-flush">
                                        <li class="list-group-item d-flex justify-content-between">
                                            <span>Symbol</span>
                                            <span class="badge bg-info">{config_info['symbol']}</span>
                                        </li>
                                        <li class="list-group-item d-flex justify-content-between">
                                            <span>Primary TF</span>
                                            <span class="badge bg-primary">{config_info['interval']}</span>
                                        </li>
                                        <li class="list-group-item d-flex justify-content-between">
                                            <span>Confirmation TF</span>
                                            <span class="badge bg-secondary">{config_info['confirm_tf']}</span>
                                        </li>
                                    </ul>
                                </div>
                                
                                <div class="col-md-4">
                                    <h6><i class="fas fa-signal"></i> Signal Thresholds</h6>
                                    <ul class="list-group list-group-flush">
                                        <li class="list-group-item d-flex justify-content-between">
                                            <span>MEDIUM (Watch)</span>
                                            <span class="badge bg-warning">≥{config_info['thresholds']['medium']}/100</span>
                                        </li>
                                        <li class="list-group-item d-flex justify-content-between">
                                            <span>STRONG (Entry)</span>
                                            <span class="badge bg-success">≥{config_info['thresholds']['strong']}/100</span>
                                        </li>
                                        <li class="list-group-item d-flex justify-content-between">
                                            <span>VERY STRONG</span>
                                            <span class="badge bg-danger">≥{config_info['thresholds']['high']}/100</span>
                                        </li>
                                    </ul>
                                </div>
                                
                                <div class="col-md-4">
                                    <h6><i class="fas fa-weight-hanging"></i> Signal Weights</h6>
                                    <ul class="list-group list-group-flush">
                                        <li class="list-group-item d-flex justify-content-between">
                                            <span>Trend</span>
                                            <span class="badge bg-primary">{config_info['weights']['trend']}%</span>
                                        </li>
                                        <li class="list-group-item d-flex justify-content-between">
                                            <span>Momentum</span>
                                            <span class="badge bg-info">{config_info['weights']['momentum']}%</span>
                                        </li>
                                        <li class="list-group-item d-flex justify-content-between">
                                            <span>Volume</span>
                                            <span class="badge bg-success">{config_info['weights']['volume']}%</span>
                                        </li>
                                        <li class="list-group-item d-flex justify-content-between">
                                            <span>Structure</span>
                                            <span class="badge bg-warning">{config_info['weights']['structure']}%</span>
                                        </li>
                                        <li class="list-group-item d-flex justify-content-between">
                                            <span>Multi-TF</span>
                                            <span class="badge bg-secondary">{config_info['weights']['multi_tf']}%</span>
                                        </li>
                                    </ul>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Footer -->
            <footer class="mt-4 mb-4 text-center text-muted">
                <hr>
                <p>
                    <i class="fas fa-robot"></i> Crypto Trading Bot v2.0 | 
                    Last Update: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')} |
                    <a href="/health" class="text-decoration-none">Health Check</a> | 
                    <a href="/stats" class="text-decoration-none">API Stats</a> | 
                    <a href="/config" class="text-decoration-none">Config</a>
                </p>
                <p class="small">
                    Status: <span class="status-active"><i class="fas fa-circle"></i> ACTIVE</span> | 
                    Auto-refresh: <span id="refresh-time">30s</span>
                </p>
            </footer>
        </div>
        
        <script>
            // Auto-refresh page every 30 seconds
            let refreshTime = 30;
            setInterval(() => {{
                refreshTime--;
                document.getElementById('refresh-time').textContent = refreshTime + 's';
                if (refreshTime <= 0) {{
                    location.reload();
                }}
            }}, 1000);
            
            // Update prices every 5 seconds
            setInterval(() => {{
                fetch('/api/market')
                    .then(response => response.json())
                    .then(data => {{
                        if (data.price) {{
                            const priceElement = document.querySelector('.price-display');
                            if (priceElement) {{
                                priceElement.textContent = '$' + parseFloat(data.price).toFixed(2);
                                // Optional: Add animation for price change
                            }}
                        }}
                    }})
                    .catch(error => console.error('Error fetching market data:', error));
            }}, 5000);
        </script>
    </body>
    </html>
    """

# API endpoints جديدة
@app.route('/api/signals')
def api_signals():
    """API for signals data"""
    signals = get_recent_signals(50)
    return jsonify(signals)

@app.route('/api/performance')
def api_performance():
    """API for performance data"""
    performance = get_bot_performance()
    return jsonify(performance)

@app.route('/api/indicators')
def api_indicators():
    """API for indicators data"""
    indicators = get_current_indicators()
    return jsonify(indicators)

# Route جديد للمخططات
@app.route('/charts')
def charts():
    """Charts and graphs page"""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Charts - Crypto Trading Bot</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            .chart-container { width: 100%; height: 400px; margin: 20px 0; }
        </style>
    </head>
    <body>
        <h1>Charts & Analytics</h1>
        <div class="chart-container">
            <canvas id="priceChart"></canvas>
        </div>
        <div class="chart-container">
            <canvas id="signalStrengthChart"></canvas>
        </div>
        <script>
            // سيتم إضافة charts هنا
        </script>
    </body>
    </html>
    """

# ... (بقية الكود يبقى كما هو)


