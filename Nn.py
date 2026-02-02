@app.route('/')
def dashboard():
    """Enhanced dashboard with real-time monitoring"""
    with state.lock:
        # جلب جميع البيانات
        market_data = get_market_data()
        indicators = get_current_indicators()
        signals = get_recent_signals()
        performance = get_bot_performance()
        
    return render_template('dashboard.html',
                         market_data=market_data,
                         indicators=indicators,
                         signals=signals,
                         performance=performance,
                         config=config)

@app.route('/api/market')
def api_market():
    """API for real-time market data"""
    with state.lock:
        if not state.klines_m30:
            return jsonify({"error": "No data"}), 404
        
        latest = state.klines_m30[-1]
        data = {
            "price": latest['close'],
            "open": latest['open'],
            "high": latest['high'],
            "low": latest['low'],
            "volume": latest['volume'],
            "time": latest['time'].isoformat(),
            "change_24h": calculate_24h_change(),
            "signal_strength": get_current_signal_strength()
        }
    return jsonify(data)
