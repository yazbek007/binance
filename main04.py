# app.py - Enhanced Multi-Crypto 3-Level Signal Strength Trading Bot
# مع واجهة متطورة وعرض تفاصيل شاملة

import os
import threading
import asyncio
import time
from datetime import datetime, timezone, timedelta
import requests
from binance import AsyncClient, BinanceSocketManager
import pandas as pd
from flask import Flask, render_template_string, jsonify
import numpy as np
from typing import Dict, List, Tuple, Optional, Set
import json
from dataclasses import dataclass, asdict
import re
from collections import defaultdict, deque
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import io
import base64

# ────────────────────────────────────────────────
#                 CONFIGURATION
# ────────────────────────────────────────────────


API_KEY    = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
NTFY_URL   = os.getenv("NTFY_TOPIC", "https://ntfy.sh/your-secret-topic-name")

# دعم متعدد العملات
SYMBOLS_RAW = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT")
SYMBOLS = [s.strip().upper() for s in SYMBOLS_RAW.split(",") if s.strip()]

INTERVAL   = os.getenv("INTERVAL", "4h")
CONFIRM_TF = os.getenv("CONFIRM_TF", "30m")

# Signal Strength Thresholds - 3 مستويات
MIN_STRENGTH      = int(os.getenv("MIN_STRENGTH", "50"))
MEDIUM_THRESHOLD  = int(os.getenv("MEDIUM_THRESHOLD", "60"))
SIGNAL_THRESHOLD  = int(os.getenv("SIGNAL_THRESHOLD", "70"))
HIGH_STRENGTH     = int(os.getenv("HIGH_STRENGTH", "85"))

# Strategy params
EMA200_PERIOD = 200
EMA50_PERIOD  = 50
EMA20_PERIOD  = 20
RSI_PERIOD    = 14
VOLUME_SMA    = 20

# RSI Zones
RSI_OVERBOUGHT = 70
RSI_OVERSOLD   = 30
RSI_NEUTRAL    = 50

# Weights for signal strength calculation (sum = 100)
WEIGHTS = {
    'trend': 25,
    'momentum': 25,
    'volume': 15,
    'structure': 20,
    'multi_tf': 15
}

# إعدادات العرض
MAX_HISTORY_PER_SYMBOL = 50
MAX_SIGNALS_TO_DISPLAY = 100
MARKET_UPDATE_INTERVAL = 300  # 5 دقائق

# ────────────────────────────────────────────────
#                 GLOBAL STATE
# ────────────────────────────────────────────────

@dataclass
class SignalMetrics:
    strength: int = 0
    breakdown: Dict = None
    confidence: str = "LOW"
    reasons: List[str] = None
    timestamp: datetime = None
    
    def to_dict(self):
        return {
            "strength": self.strength,
            "breakdown": self.breakdown,
            "confidence": self.confidence,
            "reasons": self.reasons,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None
        }

@dataclass
class MarketMetrics:
    symbol: str
    price: float = 0.0
    change_24h: float = 0.0
    volume_24h: float = 0.0
    market_cap: float = 0.0
    dominance: float = 0.0
    volatility: float = 0.0
    last_update: datetime = None
    
    def to_dict(self):
        return {
            "symbol": self.symbol,
            "price": self.price,
            "change_24h": self.change_24h,
            "volume_24h": self.volume_24h,
            "market_cap": self.market_cap,
            "dominance": self.dominance,
            "volatility": self.volatility,
            "last_update": self.last_update.isoformat() if self.last_update else None
        }

class TradingState:
    def __init__(self):
        self.symbols = SYMBOLS
        self.lock = threading.Lock()
        
        # Data storage per symbol
        self.klines_h4 = {symbol: deque(maxlen=500) for symbol in SYMBOLS}
        self.klines_m30 = {symbol: deque(maxlen=200) for symbol in SYMBOLS}
        self.signals_history = {symbol: deque(maxlen=MAX_HISTORY_PER_SYMBOL) for symbol in SYMBOLS}
        
        # Market metrics
        self.market_data = {symbol: MarketMetrics(symbol) for symbol in SYMBOLS}
        
        # Performance tracking
        self.performance_stats = {
            'total_signals': 0,
            'strong_signals': 0,
            'medium_signals': 0,
            'signal_success_rate': 0.0,
            'avg_strength': 0.0,
            'last_signal_time': None,
            'bot_start_time': datetime.now(timezone.utc)
        }
        
        # Market overview
        self.market_overview = {
            'total_market_cap': 0.0,
            'total_volume_24h': 0.0,
            'dominant_trend': 'NEUTRAL',
            'volatility_index': 0.0,
            'best_performer': None,
            'worst_performer': None
        }
        
        # Cooldown per symbol
        self.signal_cooldown = {symbol: None for symbol in SYMBOLS}
        self.cooldown_duration = 1800  # 30 دقيقة
        
    def add_signal(self, symbol: str, signal_type: str, strength: int, price: float, confidence: str):
        """Add signal to history"""
        signal_data = {
            'symbol': symbol,
            'type': signal_type,
            'strength': strength,
            'price': price,
            'confidence': confidence,
            'timestamp': datetime.now(timezone.utc),
            'id': f"{symbol}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        }
        
        self.signals_history[symbol].append(signal_data)
        
        # Update performance stats
        self.performance_stats['total_signals'] += 1
        if strength >= SIGNAL_THRESHOLD:
            self.performance_stats['strong_signals'] += 1
        elif strength >= MEDIUM_THRESHOLD:
            self.performance_stats['medium_signals'] += 1
            
        self.performance_stats['last_signal_time'] = datetime.now(timezone.utc)
        
    def get_recent_signals(self, limit: int = 10) -> List[Dict]:
        """Get recent signals across all symbols"""
        all_signals = []
        for symbol in self.symbols:
            all_signals.extend(list(self.signals_history[symbol]))
        
        # Sort by timestamp descending
        all_signals.sort(key=lambda x: x['timestamp'], reverse=True)
        return all_signals[:limit]
    
    def get_symbol_analysis(self, symbol: str) -> Dict:
        """Get comprehensive analysis for a symbol"""
        if symbol not in self.symbols:
            return {}
        
        with self.lock:
            signals = list(self.signals_history[symbol])
            klines_4h = list(self.klines_h4[symbol])
            klines_30m = list(self.klines_m30[symbol])
            market = self.market_data[symbol]
            
            # Calculate some statistics
            if signals:
                strengths = [s['strength'] for s in signals]
                avg_strength = sum(strengths) / len(strengths)
                strong_count = sum(1 for s in signals if s['strength'] >= SIGNAL_THRESHOLD)
                medium_count = sum(1 for s in signals if s['strength'] >= MEDIUM_THRESHOLD and s['strength'] < SIGNAL_THRESHOLD)
            else:
                avg_strength = 0
                strong_count = 0
                medium_count = 0
            
            return {
                'symbol': symbol,
                'market_data': market.to_dict(),
                'signal_count': len(signals),
                'avg_signal_strength': avg_strength,
                'strong_signals': strong_count,
                'medium_signals': medium_count,
                'data_quality': {
                    'klines_4h': len(klines_4h),
                    'klines_30m': len(klines_30m),
                    'has_sufficient_data': len(klines_4h) >= 100 and len(klines_30m) >= 50
                },
                'recent_signals': signals[-5:] if signals else [],
                'last_signal': signals[-1] if signals else None
            }

state = TradingState()

# ────────────────────────────────────────────────
#                   Flask App
# ────────────────────────────────────────────────

app = Flask(__name__)

@app.route('/health')
def simple_health():
    """Simple health check endpoint for Render"""
    return "OK", 200

@app.route('/ping')
def ping():
    """Health check with more details"""
    return jsonify({
        "status": "running",
        "service": "crypto-trading-bot",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "port": os.environ.get("PORT", 10000)
    }), 200



def generate_dashboard_html():
    """Generate complete dashboard HTML"""
    with state.lock:
        # Get recent signals
        recent_signals = state.get_recent_signals(10)
        
        # Get symbol analysis
        symbol_analysis = {symbol: state.get_symbol_analysis(symbol) for symbol in state.symbols}
        
        # Calculate market overview
        total_market_cap = sum(state.market_data[s].market_cap for s in state.symbols)
        total_volume = sum(state.market_data[s].volume_24h for s in state.symbols)
        
        # Performance metrics
        perf = state.performance_stats
        
    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Multi-Crypto Trading Bot Dashboard</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}
            
            body {{
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }}
            
            .container {{
                max-width: 1400px;
                margin: 0 auto;
            }}
            
            .header {{
                background: white;
                padding: 20px;
                border-radius: 10px;
                margin-bottom: 20px;
                box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            }}
            
            .header h1 {{
                color: #333;
                margin-bottom: 10px;
            }}
            
            .header p {{
                color: #666;
            }}
            
            .grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                gap: 20px;
                margin-bottom: 20px;
            }}
            
            .card {{
                background: white;
                border-radius: 10px;
                padding: 20px;
                box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            }}
            
            .card h2 {{
                color: #333;
                margin-bottom: 15px;
                font-size: 18px;
                border-bottom: 2px solid #667eea;
                padding-bottom: 8px;
            }}
            
            .metric {{
                margin-bottom: 10px;
            }}
            
            .metric-label {{
                font-weight: bold;
                color: #555;
                margin-right: 10px;
            }}
            
            .metric-value {{
                color: #333;
            }}
            
            .signal-list {{
                max-height: 300px;
                overflow-y: auto;
            }}
            
            .signal-item {{
                padding: 10px;
                margin: 5px 0;
                border-radius: 5px;
                border-left: 4px solid;
            }}
            
            .signal-strong {{
                border-left-color: #4CAF50;
                background: #f0fff0;
            }}
            
            .signal-medium {{
                border-left-color: #FF9800;
                background: #fff8e1;
            }}
            
            .signal-weak {{
                border-left-color: #f44336;
                background: #ffebee;
            }}
            
            .symbol-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
                gap: 15px;
            }}
            
            .symbol-card {{
                background: white;
                border-radius: 8px;
                padding: 15px;
                box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
                cursor: pointer;
                transition: transform 0.2s;
            }}
            
            .symbol-card:hover {{
                transform: translateY(-2px);
                box-shadow: 0 4px 8px rgba(0, 0, 0, 0.15);
            }}
            
            .symbol-header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 10px;
            }}
            
            .symbol-name {{
                font-weight: bold;
                font-size: 16px;
                color: #333;
            }}
            
            .symbol-price {{
                font-weight: bold;
                color: #667eea;
            }}
            
            .symbol-change {{
                font-size: 14px;
                padding: 2px 8px;
                border-radius: 3px;
                font-weight: bold;
            }}
            
            .positive {{
                color: #4CAF50;
                background: #f0fff0;
            }}
            
            .negative {{
                color: #f44336;
                background: #ffebee;
            }}
            
            .nav {{
                display: flex;
                gap: 10px;
                margin-bottom: 20px;
            }}
            
            .nav-button {{
                background: white;
                border: none;
                padding: 10px 20px;
                border-radius: 5px;
                cursor: pointer;
                font-weight: bold;
                color: #667eea;
                transition: background 0.3s;
            }}
            
            .nav-button:hover {{
                background: #f0f0f0;
            }}
            
            .nav-button.active {{
                background: #667eea;
                color: white;
            }}
            
            .charts-container {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 20px;
            }}
            
            .chart-card {{
                background: white;
                padding: 20px;
                border-radius: 10px;
                box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            }}
            
            @media (max-width: 768px) {{
                .grid {{
                    grid-template-columns: 1fr;
                }}
                .charts-container {{
                    grid-template-columns: 1fr;
                }}
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🚀 Multi-Crypto Trading Bot Dashboard</h1>
                <p>Real-time 3-Level Signal System for Multiple Cryptocurrencies</p>
                <p style="margin-top: 10px; color: #666;">
                    Last Update: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} | 
                    Symbols: {len(state.symbols)} | 
                    Bot Running: {(datetime.now(timezone.utc) - perf['bot_start_time']).days} days
                </p>
            </div>
            
            <div class="nav">
                <button class="nav-button active" onclick="showSection('overview')">📊 Overview</button>
                <button class="nav-button" onclick="showSection('signals')">📈 Signals</button>
                <button class="nav-button" onclick="showSection('markets')">💰 Markets</button>
                <button class="nav-button" onclick="showSection('performance')">📊 Performance</button>
            </div>
            
            <!-- Overview Section -->
            <div id="overview" class="section">
                <div class="grid">
                    <div class="card">
                        <h2>📈 Market Overview</h2>
                        <div class="metric">
                            <span class="metric-label">Total Symbols:</span>
                            <span class="metric-value">{len(state.symbols)}</span>
                        </div>
                        <div class="metric">
                            <span class="metric-label">Total Volume (24h):</span>
                            <span class="metric-value">${total_volume:,.0f}</span>
                        </div>
                        <div class="metric">
                            <span class="metric-label">Active Signals:</span>
                            <span class="metric-value">{perf['total_signals']}</span>
                        </div>
                        <div class="metric">
                            <span class="metric-label">Last Signal:</span>
                            <span class="metric-value">{perf['last_signal_time'].strftime('%H:%M') if perf['last_signal_time'] else 'None'}</span>
                        </div>
                    </div>
                    
                    <div class="card">
                        <h2>⚡ Signal Levels</h2>
                        <div class="metric">
                            <span class="metric-label">MEDIUM (Watch):</span>
                            <span class="metric-value">{MEDIUM_THRESHOLD}-{SIGNAL_THRESHOLD-1}/100</span>
                        </div>
                        <div class="metric">
                            <span class="metric-label">STRONG (Entry):</span>
                            <span class="metric-value">{SIGNAL_THRESHOLD}-{HIGH_STRENGTH-1}/100</span>
                        </div>
                        <div class="metric">
                            <span class="metric-label">VERY STRONG:</span>
                            <span class="metric-value">{HIGH_STRENGTH}+/100</span>
                        </div>
                        <div class="metric">
                            <span class="metric-label">Signal Cooldown:</span>
                            <span class="metric-value">{state.cooldown_duration//60} minutes</span>
                        </div>
                    </div>
                    
                    <div class="card">
                        <h2>📊 Performance Summary</h2>
                        <div class="metric">
                            <span class="metric-label">Total Signals:</span>
                            <span class="metric-value">{perf['total_signals']}</span>
                        </div>
                        <div class="metric">
                            <span class="metric-label">Strong Signals:</span>
                            <span class="metric-value">{perf['strong_signals']}</span>
                        </div>
                        <div class="metric">
                            <span class="metric-label">Medium Signals:</span>
                            <span class="metric-value">{perf['medium_signals']}</span>
                        </div>
                        <div class="metric">
                            <span class="metric-label">Avg Strength:</span>
                            <span class="metric-value">{perf['avg_strength']:.1f}/100</span>
                        </div>
                    </div>
                </div>
                
                <div class="card" style="margin-top: 20px;">
                    <h2>📊 All Symbols Status</h2>
                    <div class="symbol-grid">
                        {"".join([
                            f'''
                            <div class="symbol-card" onclick="window.location.href='/symbol/{symbol}'">
                                <div class="symbol-header">
                                    <div class="symbol-name">{symbol}</div>
                                    <div class="symbol-price">${state.market_data[symbol].price:.2f}</div>
                                </div>
                                <div class="metric">
                                    <span class="metric-label">24h Change:</span>
                                    <span class="symbol-change {'positive' if state.market_data[symbol].change_24h >= 0 else 'negative'}">
                                        {state.market_data[symbol].change_24h:+.2f}%
                                    </span>
                                </div>
                                <div class="metric">
                                    <span class="metric-label">Volume:</span>
                                    <span class="metric-value">${state.market_data[symbol].volume_24h:,.0f}</span>
                                </div>
                                <div class="metric">
                                    <span class="metric-label">Signals:</span>
                                    <span class="metric-value">{len(state.signals_history[symbol])}</span>
                                </div>
                                <div class="metric">
                                    <span class="metric-label">Last Update:</span>
                                    <span class="metric-value">{state.market_data[symbol].last_update.strftime('%H:%M') if state.market_data[symbol].last_update else 'N/A'}</span>
                                </div>
                            </div>
                            '''
                            for symbol in state.symbols
                        ])}
                    </div>
                </div>
            </div>
            
            <!-- Signals Section -->
            <div id="signals" class="section" style="display: none;">
                <div class="card">
                    <h2>📈 Recent Signals</h2>
                    <div class="signal-list">
                        {"".join([
                            f'''
                            <div class="signal-item {'signal-strong' if signal['strength'] >= SIGNAL_THRESHOLD else 'signal-medium' if signal['strength'] >= MEDIUM_THRESHOLD else 'signal-weak'}">
                                <div style="display: flex; justify-content: space-between;">
                                    <strong>{signal['symbol']} {signal['type']}</strong>
                                    <span>{signal['strength']}/100</span>
                                </div>
                                <div style="display: flex; justify-content: space-between; font-size: 14px; margin-top: 5px;">
                                    <span>Price: ${signal['price']:.2f}</span>
                                    <span>Confidence: {signal['confidence']}</span>
                                    <span>{signal['timestamp'].strftime('%H:%M')}</span>
                                </div>
                            </div>
                            '''
                            for signal in recent_signals
                        ]) or '<p style="text-align: center; color: #666; padding: 20px;">No signals yet</p>'}
                    </div>
                </div>
            </div>
            
            <!-- Markets Section -->
            <div id="markets" class="section" style="display: none;">
                <div class="card">
                    <h2>💰 Market Details</h2>
                    <div class="grid">
                        {"".join([
                            f'''
                            <div class="card">
                                <h3>{symbol}</h3>
                                <div class="metric">
                                    <span class="metric-label">Price:</span>
                                    <span class="metric-value">${state.market_data[symbol].price:.2f}</span>
                                </div>
                                <div class="metric">
                                    <span class="metric-label">24h Change:</span>
                                    <span class="metric-value {'positive' if state.market_data[symbol].change_24h >= 0 else 'negative'}">
                                        {state.market_data[symbol].change_24h:+.2f}%
                                    </span>
                                </div>
                                <div class="metric">
                                    <span class="metric-label">24h Volume:</span>
                                    <span class="metric-value">${state.market_data[symbol].volume_24h:,.0f}</span>
                                </div>
                                <div class="metric">
                                    <span class="metric-label">Volatility:</span>
                                    <span class="metric-value">{state.market_data[symbol].volatility:.2f}%</span>
                                </div>
                                <div class="metric">
                                    <span class="metric-label">Signal Count:</span>
                                    <span class="metric-value">{len(state.signals_history[symbol])}</span>
                                </div>
                            </div>
                            '''
                            for symbol in state.symbols
                        ])}
                    </div>
                </div>
            </div>
            
            <!-- Performance Section -->
            <div id="performance" class="section" style="display: none;">
                <div class="charts-container">
                    <div class="chart-card">
                        <h2>📊 Signal Strength Distribution</h2>
                        <canvas id="strengthChart" width="400" height="300"></canvas>
                    </div>
                    
                    <div class="chart-card">
                        <h2>📈 Signal Types</h2>
                        <canvas id="typeChart" width="400" height="300"></canvas>
                    </div>
                </div>
            </div>
        </div>
        
        <script>
            function showSection(sectionId) {{
                // Hide all sections
                document.querySelectorAll('.section').forEach(section => {{
                    section.style.display = 'none';
                }});
                
                // Remove active class from all buttons
                document.querySelectorAll('.nav-button').forEach(button => {{
                    button.classList.remove('active');
                }});
                
                // Show selected section
                document.getElementById(sectionId).style.display = 'block';
                
                // Add active class to clicked button
                event.target.classList.add('active');
                
                // Update charts if needed
                if (sectionId === 'performance') {{
                    updateCharts();
                }}
            }}
            
            function updateCharts() {{
                // Signal Strength Chart
                const strengthCtx = document.getElementById('strengthChart').getContext('2d');
                new Chart(strengthCtx, {{
                    type: 'bar',
                    data: {{
                        labels: ['Very Weak', 'Weak', 'Medium', 'Strong', 'Very Strong'],
                        datasets: [{{
                            label: 'Signal Count',
                            data: [5, 10, 15, 8, 3], // Example data - replace with real data
                            backgroundColor: [
                                '#f44336',
                                '#FF9800',
                                '#FFC107',
                                '#4CAF50',
                                '#2196F3'
                            ]
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
                        scales: {{
                            y: {{
                                beginAtZero: true
                            }}
                        }}
                    }}
                }});
                
                // Signal Type Chart
                const typeCtx = document.getElementById('typeChart').getContext('2d');
                new Chart(typeCtx, {{
                    type: 'pie',
                    data: {{
                        labels: ['LONG', 'SHORT', 'LONG_WATCH', 'SHORT_WATCH'],
                        datasets: [{{
                            data: [12, 8, 15, 10], // Example data - replace with real data
                            backgroundColor: [
                                '#4CAF50',
                                '#f44336',
                                '#FFC107',
                                '#FF9800'
                            ]
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false
                    }}
                }});
            }}
            
            // Auto-refresh dashboard every 30 seconds
            setInterval(() => {{
                window.location.reload();
            }}, 30000);
            
            // Initialize
            updateCharts();
        </script>
    </body>
    </html>
    """

@app.route('/')
def dashboard():
    """Main dashboard"""
    return generate_dashboard_html()

@app.route('/symbol/<symbol>')
def symbol_detail(symbol):
    """Detailed view for a specific symbol"""
    if symbol not in state.symbols:
        return f"Symbol {symbol} not found", 404
    
    with state.lock:
        analysis = state.get_symbol_analysis(symbol)
        market_data = state.market_data[symbol]
        signals = list(state.signals_history[symbol])
        
        # Calculate some statistics
        if signals:
            recent_signals = signals[-10:]
            strengths = [s['strength'] for s in signals]
            avg_strength = sum(strengths) / len(strengths)
            long_count = sum(1 for s in signals if 'LONG' in s['type'])
            short_count = sum(1 for s in signals if 'SHORT' in s['type'])
        else:
            recent_signals = []
            avg_strength = 0
            long_count = 0
            short_count = 0
    
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{symbol} - Detailed Analysis</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                margin: 20px;
                background: #f5f5f5;
            }}
            .container {{
                max-width: 1200px;
                margin: 0 auto;
                background: white;
                padding: 20px;
                border-radius: 10px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }}
            .header {{
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                padding: 20px;
                border-radius: 8px;
                margin-bottom: 20px;
            }}
            .stats-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 15px;
                margin: 20px 0;
            }}
            .stat-card {{
                background: #f8f9fa;
                padding: 15px;
                border-radius: 8px;
                border-left: 4px solid #667eea;
            }}
            .signal-list {{
                max-height: 400px;
                overflow-y: auto;
                margin: 20px 0;
            }}
            .signal-item {{
                padding: 10px;
                margin: 5px 0;
                border-radius: 5px;
                border-left: 4px solid;
                background: #f8f9fa;
            }}
            .back-button {{
                display: inline-block;
                padding: 10px 20px;
                background: #667eea;
                color: white;
                text-decoration: none;
                border-radius: 5px;
                margin-bottom: 20px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <a href="/" class="back-button">← Back to Dashboard</a>
            
            <div class="header">
                <h1>{symbol} - Detailed Analysis</h1>
                <p>Real-time market data and signal history</p>
            </div>
            
            <div class="stats-grid">
                <div class="stat-card">
                    <h3>Current Price</h3>
                    <p style="font-size: 24px; font-weight: bold; color: #333;">
                        ${market_data.price:.2f}
                    </p>
                    <p style="color: {'#4CAF50' if market_data.change_24h >= 0 else '#f44336'};">
                        {market_data.change_24h:+.2f}% (24h)
                    </p>
                </div>
                
                <div class="stat-card">
                    <h3>Market Data</h3>
                    <p>24h Volume: ${market_data.volume_24h:,.0f}</p>
                    <p>Volatility: {market_data.volatility:.2f}%</p>
                    <p>Last Update: {market_data.last_update.strftime('%H:%M') if market_data.last_update else 'N/A'}</p>
                </div>
                
                <div class="stat-card">
                    <h3>Signal Statistics</h3>
                    <p>Total Signals: {len(signals)}</p>
                    <p>Average Strength: {avg_strength:.1f}/100</p>
                    <p>LONG: {long_count} | SHORT: {short_count}</p>
                </div>
                
                <div class="stat-card">
                    <h3>Data Quality</h3>
                    <p>4H Candles: {len(state.klines_h4[symbol])}</p>
                    <p>30M Candles: {len(state.klines_m30[symbol])}</p>
                    <p>Status: {'✅ Sufficient' if analysis['data_quality']['has_sufficient_data'] else '⚠️ Collecting'}</p>
                </div>
            </div>
            
            <h2>Recent Signals</h2>
            <div class="signal-list">
                {"".join([
                    f'''
                    <div class="signal-item" style="border-left-color: {'#4CAF50' if signal['strength'] >= SIGNAL_THRESHOLD else '#FF9800' if signal['strength'] >= MEDIUM_THRESHOLD else '#f44336'};">
                        <div style="display: flex; justify-content: space-between;">
                            <strong>{signal['type']}</strong>
                            <span>{signal['strength']}/100 ({signal['confidence']})</span>
                        </div>
                        <div style="display: flex; justify-content: space-between; font-size: 14px; margin-top: 5px;">
                            <span>Price: ${signal['price']:.2f}</span>
                            <span>{signal['timestamp'].strftime('%Y-%m-%d %H:%M')}</span>
                        </div>
                    </div>
                    '''
                    for signal in reversed(recent_signals)
                ]) or '<p style="text-align: center; color: #666; padding: 20px;">No signals yet for this symbol</p>'}
            </div>
            
            <div style="margin-top: 30px; padding: 20px; background: #f8f9fa; border-radius: 8px;">
                <h3>Technical Information</h3>
                <p><strong>Timeframes:</strong> {INTERVAL} (Primary), {CONFIRM_TF} (Confirmation)</p>
                <p><strong>Signal Thresholds:</strong> MEDIUM ({MEDIUM_THRESHOLD}+), STRONG ({SIGNAL_THRESHOLD}+), VERY STRONG ({HIGH_STRENGTH}+)</p>
                <p><strong>Last Analysis:</strong> {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}</p>
            </div>
        </div>
    </body>
    </html>
    """

@app.route('/api/health')
def health():
    """Health check endpoint"""
    with state.lock:
        data_ok = all(
            len(state.klines_h4[s]) > 100 and len(state.klines_m30[s]) > 50 
            for s in state.symbols[:3]  # Check first 3 symbols
        )
    
    status = {
        "status": "healthy" if data_ok else "collecting_data",
        "symbols_monitored": len(state.symbols),
        "symbols": state.symbols,
        "data_status": {
            symbol: {
                "klines_4h": len(state.klines_h4[symbol]),
                "klines_30m": len(state.klines_m30[symbol])
            }
            for symbol in state.symbols
        },
        "performance": state.performance_stats,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    return jsonify(status), 200 if data_ok else 202

@app.route('/api/symbols')
def api_symbols():
    """Get all symbols data"""
    with state.lock:
        symbols_data = {}
        for symbol in state.symbols:
            symbols_data[symbol] = {
                'market_data': state.market_data[symbol].to_dict(),
                'signal_count': len(state.signals_history[symbol]),
                'recent_signals': list(state.signals_history[symbol])[-5:],
                'data_status': {
                    'klines_4h': len(state.klines_h4[symbol]),
                    'klines_30m': len(state.klines_m30[symbol])
                }
            }
    
    return jsonify({
        'symbols': symbols_data,
        'total_symbols': len(state.symbols),
        'timestamp': datetime.now(timezone.utc).isoformat()
    })

@app.route('/api/signals/recent')
def api_recent_signals():
    """Get recent signals"""
    with state.lock:
        recent_signals = state.get_recent_signals(20)
    
    return jsonify({
        'signals': recent_signals,
        'count': len(recent_signals),
        'timestamp': datetime.now(timezone.utc).isoformat()
    })

@app.route('/api/performance')
def api_performance():
    """Get performance statistics"""
    with state.lock:
        perf = state.performance_stats
        
        # Calculate signal distribution
        all_signals = []
        for symbol in state.symbols:
            all_signals.extend(list(state.signals_history[symbol]))
        
        strength_distribution = {
            'very_strong': sum(1 for s in all_signals if s['strength'] >= HIGH_STRENGTH),
            'strong': sum(1 for s in all_signals if SIGNAL_THRESHOLD <= s['strength'] < HIGH_STRENGTH),
            'medium': sum(1 for s in all_signals if MEDIUM_THRESHOLD <= s['strength'] < SIGNAL_THRESHOLD),
            'weak': sum(1 for s in all_signals if s['strength'] < MEDIUM_THRESHOLD)
        }
        
        type_distribution = {
            'LONG': sum(1 for s in all_signals if s['type'] == 'LONG'),
            'SHORT': sum(1 for s in all_signals if s['type'] == 'SHORT'),
            'LONG_WATCH': sum(1 for s in all_signals if s['type'] == 'LONG_WATCH'),
            'SHORT_WATCH': sum(1 for s in all_signals if s['type'] == 'SHORT_WATCH')
        }
    
    return jsonify({
        'performance': perf,
        'strength_distribution': strength_distribution,
        'type_distribution': type_distribution,
        'timestamp': datetime.now(timezone.utc).isoformat()
    })

@app.route('/config')
def config():
    """Configuration endpoint"""
    config_data = {
        "symbols": SYMBOLS,
        "interval_primary": INTERVAL,
        "interval_confirmation": CONFIRM_TF,
        "signal_levels": {
            "min_strength": MIN_STRENGTH,
            "medium_threshold": MEDIUM_THRESHOLD,
            "strong_threshold": SIGNAL_THRESHOLD,
            "high_strength": HIGH_STRENGTH
        },
        "weights": WEIGHTS,
        "cooldown_seconds": state.cooldown_duration,
        "environment_variables": {
            "BINANCE_API_KEY": "***" if API_KEY else "Not set",
            "NTFY_TOPIC": NTFY_URL if NTFY_URL else "Not set"
        }
    }
    
    return jsonify(config_data)

# ────────────────────────────────────────────────
#                  Notifications
# ────────────────────────────────────────────────

def send_ntfy(msg: str, title: str = "Crypto Bot", priority: str = "default", 
              tags: str = None) -> None:
    """Send notification via NTFY - Safe version without emojis"""
    try:
        def clean_text(text):
            if not isinstance(text, str):
                text = str(text)
            
            emoji_pattern = re.compile(
                "["
                "\U0001F600-\U0001F64F"
                "\U0001F300-\U0001F5FF"
                "\U0001F680-\U0001F6FF"
                "\U0001F1E0-\U0001F1FF"
                "\U00002500-\U00002BEF"
                "\U00002702-\U000027B0"
                "\U000024C2-\U0001F251"
                "\U0001f926-\U0001f937"
                "\U00010000-\U0010ffff"
                "\u2640-\u2642"
                "\u2600-\u2B55"
                "\u200d"
                "\u23cf"
                "\u23e9"
                "\u231a"
                "\ufe0f"
                "\u3030"
                "]+",
                flags=re.UNICODE
            )
            
            text = emoji_pattern.sub(r'', text)
            text = text.strip()
            
            if text == title:
                text = text.replace('\n', ' | ')
            
            return text
        
        safe_title = clean_text(title)
        safe_msg = clean_text(msg)
        
        if not safe_title or safe_title.isspace():
            safe_title = "Crypto Bot Notification"
        
        if not safe_msg or safe_msg.isspace():
            safe_msg = "Empty message"
        
        headers = {
            "Title": safe_title[:250],
            "Priority": priority,
        }
        
        if tags:
            safe_tags = clean_text(tags)
            if safe_tags:
                headers["Tags"] = safe_tags[:255]
        
        response = requests.post(
            NTFY_URL,
            data=safe_msg.encode('utf-8'),
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 200:
            print(f"✓ NTFY sent: {safe_title}")
        else:
            print(f"✗ NTFY failed: {response.status_code}")
            
    except Exception as e:
        print(f"✗ NTFY error: {str(e)[:100]}")

# ────────────────────────────────────────────────
#               Technical Indicators
# ────────────────────────────────────────────────

def compute_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """Calculate RSI correctly"""
    delta = prices.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    
    return rsi.fillna(50)

def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Average True Range for volatility"""
    high_low = df['high'] - df['low']
    high_close = abs(df['high'] - df['close'].shift())
    low_close = abs(df['low'] - df['close'].shift())
    
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = true_range.rolling(window=period).mean()
    return atr

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all technical indicators"""
    df = df.copy()
    
    # Price statistics
    df['returns'] = df['close'].pct_change()
    df['volatility'] = df['returns'].rolling(20).std()
    
    # EMAs
    df['ema200'] = df['close'].ewm(span=EMA200_PERIOD, adjust=False).mean()
    df['ema50'] = df['close'].ewm(span=EMA50_PERIOD, adjust=False).mean()
    df['ema20'] = df['close'].ewm(span=EMA20_PERIOD, adjust=False).mean()
    
    # EMA relationships
    df['ema20_above_50'] = (df['ema20'] > df['ema50']).astype(int)
    df['ema50_above_200'] = (df['ema50'] > df['ema200']).astype(int)
    
    # MACD
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = ema12 - ema26
    df['signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['hist'] = df['macd'] - df['signal']
    df['macd_trend'] = np.where(df['macd'] > df['signal'], 1, -1)
    
    # RSI
    df['rsi'] = compute_rsi(df['close'], RSI_PERIOD)
    
    # Volume indicators
    df['vol_sma20'] = df['volume'].rolling(window=VOLUME_SMA).mean()
    df['volume_ratio'] = df['volume'] / df['vol_sma20']
    df['volume_trend'] = (df['volume'] > df['vol_sma20']).astype(int)
    
    # Volatility
    df['atr'] = compute_atr(df)
    df['atr_percent'] = df['atr'] / df['close'] * 100
    
    # Price position relative to EMAs
    df['price_vs_ema20'] = (df['close'] - df['ema20']) / df['ema20'] * 100
    df['price_vs_ema50'] = (df['close'] - df['ema50']) / df['ema50'] * 100
    
    # Candle patterns
    df['body'] = abs(df['close'] - df['open'])
    df['range'] = df['high'] - df['low']
    df['body_ratio'] = df['body'] / df['range']
    df['is_bullish'] = (df['close'] > df['open']).astype(int)
    
    return df

# ────────────────────────────────────────────────
#              Signal Strength System
# ────────────────────────────────────────────────

def calculate_trend_strength(df: pd.DataFrame) -> Tuple[int, List[str]]:
    """Calculate trend strength score (0-25 points)"""
    score = 0
    reasons = []
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    
    # 1. EMA Alignment (max 10 points)
    ema_alignment = 0
    if last['ema20'] > last['ema50'] > last['ema200']:
        ema_alignment += 8
        reasons.append("EMA20 > EMA50 > EMA200 (Strong Uptrend)")
    elif last['ema20'] < last['ema50'] < last['ema200']:
        ema_alignment += 8
        reasons.append("EMA20 < EMA50 < EMA200 (Strong Downtrend)")
    
    ema_distance_20_50 = abs(last['ema20'] - last['ema50']) / last['ema50'] * 100
    if ema_distance_20_50 > 2:
        ema_alignment += 2
        reasons.append(f"EMA separation: {ema_distance_20_50:.1f}%")
    
    score += min(ema_alignment, 10)
    
    # 2. Price vs EMA position (max 10 points)
    price_position = 0
    if last['close'] > last['ema200']:
        distance = (last['close'] - last['ema200']) / last['ema200'] * 100
        price_position += min(int(distance * 2), 5)
    else:
        distance = (last['ema200'] - last['close']) / last['ema200'] * 100
        price_position += min(int(distance * 2), 5)
    
    if (last['close'] > last['ema50'] and last['ema50'] > last['ema200']) or \
       (last['close'] < last['ema50'] and last['ema50'] < last['ema200']):
        price_position += 5
        reasons.append("Price aligned with EMA50 trend")
    
    score += min(price_position, 10)
    
    # 3. Trend consistency (max 5 points)
    trend_consistency = 0
    recent_trend = df['ema20_above_50'].iloc[-5:].mean()
    if recent_trend > 0.8:
        trend_consistency += 3
        reasons.append("Consistent uptrend (80%+)")
    elif recent_trend < 0.2:
        trend_consistency += 3
        reasons.append("Consistent downtrend (80%+)")
    
    recent_returns = df['returns'].iloc[-3:].sum() * 100
    if (recent_returns > 1 and last['close'] > last['ema200']) or \
       (recent_returns < -1 and last['close'] < last['ema200']):
        trend_consistency += 2
        reasons.append(f"Recent momentum: {recent_returns:.1f}%")
    
    score += min(trend_consistency, 5)
    
    return min(score, WEIGHTS['trend']), reasons

def calculate_momentum_strength(df: pd.DataFrame, signal_type: str) -> Tuple[int, List[str]]:
    """Calculate momentum strength score (0-25 points)"""
    score = 0
    reasons = []
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    
    # 1. RSI Strength (max 10 points)
    rsi_score = 0
    if signal_type == "LONG":
        if 40 <= last['rsi'] <= 60:
            rsi_score += 8
            reasons.append(f"RSI in optimal zone: {last['rsi']:.1f}")
        elif last['rsi'] > 30 and last['rsi'] < 70:
            rsi_score += 5
            reasons.append(f"RSI in good zone: {last['rsi']:.1f}")
        
        if last['rsi'] > prev['rsi']:
            rsi_score += 2
    else:
        if 40 <= last['rsi'] <= 60:
            rsi_score += 8
            reasons.append(f"RSI in optimal zone: {last['rsi']:.1f}")
        elif last['rsi'] > 30 and last['rsi'] < 70:
            rsi_score += 5
        
        if last['rsi'] < prev['rsi']:
            rsi_score += 2
    
    score += min(rsi_score, 10)
    
    # 2. MACD Strength (max 10 points)
    macd_score = 0
    
    if signal_type == "LONG":
        if prev['macd'] < prev['signal'] and last['macd'] > last['signal']:
            macd_score += 8
            reasons.append("MACD bullish crossover")
        elif last['macd'] > last['signal']:
            macd_score += 5
            reasons.append("MACD above signal line")
    else:
        if prev['macd'] > prev['signal'] and last['macd'] < last['signal']:
            macd_score += 8
            reasons.append("MACD bearish crossover")
        elif last['macd'] < last['signal']:
            macd_score += 5
            reasons.append("MACD below signal line")
    
    if abs(last['hist']) > abs(prev['hist']):
        macd_score += 2
        reasons.append("MACD histogram strengthening")
    
    score += min(macd_score, 10)
    
    # 3. Price momentum (max 5 points)
    momentum_score = 0
    
    recent_candles = df.iloc[-3:]
    bullish_candles = (recent_candles['close'] > recent_candles['open']).sum()
    
    if signal_type == "LONG" and bullish_candles >= 2:
        momentum_score += 3
        reasons.append(f"{bullish_candles}/3 recent candles bullish")
    elif signal_type == "SHORT" and bullish_candles <= 1:
        momentum_score += 3
        reasons.append(f"{3-bullish_candles}/3 recent candles bearish")
    
    if signal_type == "LONG" and last['close'] > last['ema20'] > prev['ema20']:
        momentum_score += 2
    elif signal_type == "SHORT" and last['close'] < last['ema20'] < prev['ema20']:
        momentum_score += 2
    
    score += min(momentum_score, 5)
    
    return min(score, WEIGHTS['momentum']), reasons

def calculate_volume_strength(df_h4: pd.DataFrame, df_m30: pd.DataFrame) -> Tuple[int, List[str]]:
    """Calculate volume confirmation score (0-15 points)"""
    score = 0
    reasons = []
    
    if len(df_m30) < 10:
        return 0, ["Insufficient 30m data"]
    
    last_h4 = df_h4.iloc[-1]
    last_m30 = df_m30.iloc[-1]
    
    # 1. Volume vs average (max 8 points)
    volume_score = 0
    
    if last_h4['volume_ratio'] > 1.5:
        volume_score += 6
        reasons.append(f"H4 volume {last_h4['volume_ratio']:.1f}x average")
    elif last_h4['volume_ratio'] > 1.2:
        volume_score += 4
        reasons.append(f"H4 volume {last_h4['volume_ratio']:.1f}x average")
    elif last_h4['volume_ratio'] > 1.0:
        volume_score += 2
    
    if last_h4['volume_trend'] == 1:
        volume_score += 2
        reasons.append("Volume above 20-period average")
    
    score += min(volume_score, 8)
    
    # 2. 30m confirmation (max 7 points)
    confirm_score = 0
    
    if last_m30.get('volume_ratio', 0) > 1.8:
        confirm_score += 5
        reasons.append(f"30m volume spike: {last_m30.get('volume_ratio', 0):.1f}x")
    elif last_m30.get('volume_ratio', 0) > 1.3:
        confirm_score += 3
    
    recent_30m_vol = df_m30['volume'].iloc[-3:].mean()
    avg_30m_vol = df_m30['vol_sma20'].iloc[-1]
    if recent_30m_vol > avg_30m_vol * 1.2:
        confirm_score += 2
    
    score += min(confirm_score, 7)
    
    return min(score, WEIGHTS['volume']), reasons

def calculate_structure_strength(df: pd.DataFrame, signal_type: str) -> Tuple[int, List[str]]:
    """Calculate price structure score (0-20 points)"""
    score = 0
    reasons = []
    
    last = df.iloc[-1]
    
    # 1. Support/Resistance levels (max 10 points)
    structure_score = 0
    
    price_vs_ema50 = abs(last['price_vs_ema50'])
    if price_vs_ema50 < 1:
        structure_score += 8
        reasons.append(f"Price near EMA50 (±{price_vs_ema50:.1f}%)")
    elif price_vs_ema50 < 2:
        structure_score += 5
    elif price_vs_ema50 < 3:
        structure_score += 2
    
    recent_range = df['range'].iloc[-5:].mean()
    avg_range = df['range'].rolling(20).mean().iloc[-1]
    if recent_range < avg_range * 0.7:
        structure_score += 2
        reasons.append("Low volatility consolidation")
    
    score += min(structure_score, 10)
    
    # 2. Candle patterns (max 10 points)
    pattern_score = 0
    
    if last['body_ratio'] > 0.7:
        pattern_score += 6
        if last['is_bullish'] == 1:
            reasons.append("Strong bullish candle")
        else:
            reasons.append("Strong bearish candle")
    elif last['body_ratio'] > 0.5:
        pattern_score += 3
    
    recent_direction = df['is_bullish'].iloc[-3:].mean()
    if signal_type == "LONG" and recent_direction > 0.66:
        pattern_score += 4
        reasons.append("Consecutive bullish candles")
    elif signal_type == "SHORT" and recent_direction < 0.33:
        pattern_score += 4
        reasons.append("Consecutive bearish candles")
    
    score += min(pattern_score, 10)
    
    return min(score, WEIGHTS['structure']), reasons

def calculate_multi_tf_strength(df_h4: pd.DataFrame, df_m30: pd.DataFrame, 
                               signal_type: str) -> Tuple[int, List[str]]:
    """Calculate multi-timeframe confirmation score (0-15 points)"""
    score = 0
    reasons = []
    
    if len(df_m30) < 20:
        return 0, ["Insufficient multi-TF data"]
    
    last_h4 = df_h4.iloc[-1]
    last_m30 = df_m30.iloc[-1]
    
    # 1. 30m trend alignment (max 10 points)
    alignment_score = 0
    
    if signal_type == "LONG":
        if last_m30['close'] > last_m30['ema50'] and last_m30['ema50'] > last_m30['ema200']:
            alignment_score += 8
            reasons.append("30m confirms uptrend")
        elif last_m30['close'] > last_m30['ema50']:
            alignment_score += 5
    else:
        if last_m30['close'] < last_m30['ema50'] and last_m30['ema50'] < last_m30['ema200']:
            alignment_score += 8
            reasons.append("30m confirms downtrend")
        elif last_m30['close'] < last_m30['ema50']:
            alignment_score += 5
    
    if signal_type == "LONG" and last_m30['macd'] > last_m30['signal']:
        alignment_score += 2
    elif signal_type == "SHORT" and last_m30['macd'] < last_m30['signal']:
        alignment_score += 2
    
    score += min(alignment_score, 10)
    
    # 2. Divergence check (max 5 points)
    divergence_score = 0
    
    h4_rsi_trend = last_h4['rsi'] > df_h4['rsi'].iloc[-2]
    m30_rsi_trend = last_m30['rsi'] > df_m30['rsi'].iloc[-2]
    
    if signal_type == "LONG" and h4_rsi_trend and m30_rsi_trend:
        divergence_score += 5
        reasons.append("No RSI divergence (positive)")
    elif signal_type == "SHORT" and not h4_rsi_trend and not m30_rsi_trend:
        divergence_score += 5
        reasons.append("No RSI divergence (negative)")
    
    score += min(divergence_score, 5)
    
    return min(score, WEIGHTS['multi_tf']), reasons

def calculate_signal_strength(df_h4: pd.DataFrame, df_m30: pd.DataFrame, 
                            signal_type: str) -> SignalMetrics:
    """Calculate comprehensive signal strength score"""
    metrics = SignalMetrics(
        strength=0,
        breakdown={},
        confidence="LOW",
        reasons=[],
        timestamp=datetime.now(timezone.utc)
    )
    
    trend_score, trend_reasons = calculate_trend_strength(df_h4)
    momentum_score, momentum_reasons = calculate_momentum_strength(df_h4, signal_type)
    volume_score, volume_reasons = calculate_volume_strength(df_h4, df_m30)
    structure_score, structure_reasons = calculate_structure_strength(df_h4, signal_type)
    multi_tf_score, multi_tf_reasons = calculate_multi_tf_strength(df_h4, df_m30, signal_type)
    
    total_score = (trend_score + momentum_score + volume_score + 
                   structure_score + multi_tf_score)
    
    atr_percent = df_h4['atr_percent'].iloc[-1]
    if atr_percent > 3:
        total_score = int(total_score * 0.8)
        metrics.reasons.append(f"High volatility penalty: ATR {atr_percent:.1f}%")
    
    metrics.strength = min(total_score, 100)
    
    if metrics.strength >= HIGH_STRENGTH:
        metrics.confidence = "VERY HIGH"
    elif metrics.strength >= SIGNAL_THRESHOLD:
        metrics.confidence = "HIGH"
    elif metrics.strength >= MEDIUM_THRESHOLD:
        metrics.confidence = "MEDIUM"
    elif metrics.strength >= MIN_STRENGTH:
        metrics.confidence = "LOW"
    else:
        metrics.confidence = "VERY LOW"
    
    metrics.breakdown = {
        'trend': trend_score,
        'momentum': momentum_score,
        'volume': volume_score,
        'structure': structure_score,
        'multi_tf': multi_tf_score,
        'total': metrics.strength
    }
    
    all_reasons = []
    all_reasons.extend(trend_reasons)
    all_reasons.extend(momentum_reasons)
    all_reasons.extend(volume_reasons)
    all_reasons.extend(structure_reasons)
    all_reasons.extend(multi_tf_reasons)
    
    metrics.reasons = all_reasons[:5]
    
    return metrics

# ────────────────────────────────────────────────
#              Signal Notifications
# ────────────────────────────────────────────────

def send_strong_signal(symbol: str, signal_type: str, price: float, 
                      last_candle: pd.Series, metrics: SignalMetrics) -> None:
    """Send STRONG trading signal (70+ strength) - without emojis"""
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    
    if signal_type == "LONG":
        title = f"{symbol} LONG SIGNAL [{metrics.confidence}]"
        signal_char = f"[{symbol} LONG]"
    else:
        title = f"{symbol} SHORT SIGNAL [{metrics.confidence}]"
        signal_char = f"[{symbol} SHORT]"
    
    msg = (
        f"{signal_char} SIGNAL\n"
        f"Strength: {metrics.strength}/100 ({metrics.confidence})\n"
        f"Price: {price:.2f}\n"
        f"RSI: {last_candle['rsi']:.1f} | MACD: {last_candle['macd']:.5f}\n"
        f"EMA50: {last_candle['ema50']:.2f} | EMA200: {last_candle['ema200']:.2f}\n"
        f"\nStrength Breakdown:\n"
        f"Trend: {metrics.breakdown['trend']}/{WEIGHTS['trend']}\n"
        f"Momentum: {metrics.breakdown['momentum']}/{WEIGHTS['momentum']}\n"
        f"Volume: {metrics.breakdown['volume']}/{WEIGHTS['volume']}\n"
        f"Structure: {metrics.breakdown['structure']}/{WEIGHTS['structure']}\n"
        f"Multi-TF: {metrics.breakdown['multi_tf']}/{WEIGHTS['multi_tf']}\n"
        f"\nTop Reasons:\n"
        f"{chr(10).join(metrics.reasons[:3])}\n"
        f"\nTime: {timestamp}"
    )
    
    priority = "high" if metrics.strength >= HIGH_STRENGTH else "default"
    tags = "rocket" if signal_type == "LONG" else "chart_decreasing"
    
    send_ntfy(msg, title, priority, tags)
    
    print(f"\n{'='*60}")
    print(f"{symbol} {signal_type} STRONG Signal | Strength: {metrics.strength}/100")
    print(f"Price: {price:.2f} | RSI: {last_candle['rsi']:.1f}")
    print(f"{'='*60}\n")

def send_medium_signal(symbol: str, signal_type: str, price: float, 
                      last_candle: pd.Series, metrics: SignalMetrics) -> None:
    """Send MEDIUM strength signal (60-69) for monitoring - without emojis"""
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    
    if signal_type == "LONG":
        title = f"{symbol} LONG WATCH [{metrics.confidence}]"
        signal_char = f"[{symbol} LONG WATCH]"
    else:
        title = f"{symbol} SHORT WATCH [{metrics.confidence}]"
        signal_char = f"[{symbol} SHORT WATCH]"
    
    msg = (
        f"{signal_char}\n"
        f"Strength: {metrics.strength}/100 (MEDIUM)\n"
        f"Price: {price:.2f}\n"
        f"RSI: {last_candle['rsi']:.1f} | MACD: {last_candle['macd']:.5f}\n"
        f"\nThis is a WATCH signal (not for immediate entry)\n"
        f"Monitor for confirmation above {SIGNAL_THRESHOLD}/100\n"
        f"\nTime: {timestamp}"
    )
    
    send_ntfy(msg, title, "low", "eyes")
    
    print(f"\n{'='*60}")
    print(f"{symbol} {signal_type} MEDIUM Signal | Strength: {metrics.strength}/100")
    print(f"Price: {price:.2f} | RSI: {last_candle['rsi']:.1f}")
    print(f"{'='*60}\n")

# ────────────────────────────────────────────────
#              Signal Analysis
# ────────────────────────────────────────────────

def analyze_market_signals(symbol: str) -> None:
    """Analyze market for both LONG and SHORT signals with strength scoring for specific symbol"""
    with state.lock:
        if len(state.klines_h4[symbol]) < 210 or len(state.klines_m30[symbol]) < 50:
            return
        
        current_time = datetime.now(timezone.utc)
        if (state.signal_cooldown[symbol] and 
            (current_time - state.signal_cooldown[symbol]).seconds < state.cooldown_duration):
            return
        
        # Convert deque to list and create DataFrame
        df_h4 = pd.DataFrame(list(state.klines_h4[symbol])[-300:])
        df_m30 = pd.DataFrame(list(state.klines_m30[symbol])[-150:])
        
        if len(df_h4) < 100 or len(df_m30) < 30:
            return
        
        numeric_cols = ['open', 'high', 'low', 'close', 'volume']
        df_h4[numeric_cols] = df_h4[numeric_cols].apply(pd.to_numeric, errors='coerce')
        df_m30[numeric_cols] = df_m30[numeric_cols].apply(pd.to_numeric, errors='coerce')
        
        df_h4 = compute_indicators(df_h4)
        df_m30 = compute_indicators(df_m30)
        
        current_price = float(df_m30.iloc[-1]['close'])
        
        signals = []
        
        long_metrics = calculate_signal_strength(df_h4, df_m30, "LONG")
        if long_metrics.strength >= MIN_STRENGTH:
            signals.append(("LONG", long_metrics, current_price, df_h4.iloc[-1]))
        
        short_metrics = calculate_signal_strength(df_h4, df_m30, "SHORT")
        if short_metrics.strength >= MIN_STRENGTH:
            signals.append(("SHORT", short_metrics, current_price, df_h4.iloc[-1]))
        
        signals.sort(key=lambda x: x[1].strength, reverse=True)
        
        for signal_type, metrics, price, last_candle in signals:
            if MEDIUM_THRESHOLD <= metrics.strength < SIGNAL_THRESHOLD:
                send_medium_signal(symbol, signal_type, price, last_candle, metrics)
                state.signal_cooldown[symbol] = current_time
                
                state.add_signal(symbol, f"{signal_type}_WATCH", metrics.strength, price, metrics.confidence)
                break
            
            elif metrics.strength >= SIGNAL_THRESHOLD:
                send_strong_signal(symbol, signal_type, price, last_candle, metrics)
                state.signal_cooldown[symbol] = current_time
                
                state.add_signal(symbol, signal_type, metrics.strength, price, metrics.confidence)
                break

# ────────────────────────────────────────────────
#             Market Data Updates
# ────────────────────────────────────────────────

async def update_market_data():
    """Periodically update market data for all symbols"""
    while True:
        try:
            client = await AsyncClient.create(API_KEY, API_SECRET)
            
            for symbol in state.symbols:
                try:
                    # Get ticker data
                    ticker = await client.get_symbol_ticker(symbol=symbol)
                    
                    # Get 24hr ticker for change and volume
                    ticker_24hr = await client.get_ticker(symbol=symbol)
                    
                    # Get klines for volatility calculation
                    klines = await client.get_klines(symbol=symbol, interval='1d', limit=20)
                    
                    with state.lock:
                        # Update market data
                        state.market_data[symbol].price = float(ticker['price'])
                        state.market_data[symbol].change_24h = float(ticker_24hr['priceChangePercent'])
                        state.market_data[symbol].volume_24h = float(ticker_24hr['volume'])
                        state.market_data[symbol].last_update = datetime.now(timezone.utc)
                        
                        # Calculate volatility from daily klines
                        if klines:
                            closes = [float(k[4]) for k in klines]
                            returns = np.diff(closes) / closes[:-1]
                            state.market_data[symbol].volatility = np.std(returns) * 100
                        
                    await asyncio.sleep(0.5)  # Rate limiting
                    
                except Exception as e:
                    print(f"Error updating {symbol} market data: {e}")
            
            await client.close_connection()
            
        except Exception as e:
            print(f"Market data update error: {e}")
        
        await asyncio.sleep(MARKET_UPDATE_INTERVAL)

# ────────────────────────────────────────────────
#             Async Handlers
# ────────────────────────────────────────────────

def handle_kline(symbol: str, msg: Dict, timeframe: str) -> None:
    """Handle kline updates for any timeframe and symbol"""
    k = msg['k']
    
    if k['x']:
        candle_data = {
            'open': float(k['o']),
            'high': float(k['h']),
            'low': float(k['l']),
            'close': float(k['c']),
            'volume': float(k['v']),
            'time': datetime.fromtimestamp(k['t'] / 1000, timezone.utc)
        }
        
        with state.lock:
            if timeframe == INTERVAL:
                state.klines_h4[symbol].append(candle_data)
            elif timeframe == CONFIRM_TF:
                state.klines_m30[symbol].append(candle_data)
        
        if timeframe == INTERVAL:
            analyze_market_signals(symbol)

# ────────────────────────────────────────────────
#              Async Main Loop
# ────────────────────────────────────────────────

async def run_websockets():
    """Main WebSocket connection manager for multiple symbols"""
    reconnect_delay = 5
    
    while True:
        try:
            client = await AsyncClient.create(API_KEY, API_SECRET)
            bm = BinanceSocketManager(client)
            
            # Create streams for all symbols and timeframes
            streams = []
            for symbol in state.symbols:
                streams.append(f"{symbol.lower()}@kline_{INTERVAL}")
                streams.append(f"{symbol.lower()}@kline_{CONFIRM_TF}")
            
            async with bm.multiplex_socket(streams) as multiplex_stream:
                connection_msg = (
                    f"Multi-Crypto Bot Connected - 3-Level Signals\n"
                    f"Symbols: {', '.join(state.symbols)}\n"
                    f"Timeframes: {INTERVAL} + {CONFIRM_TF}\n"
                    f"Signal Levels:\n"
                    f"- MEDIUM (Watch): {MEDIUM_THRESHOLD}-{SIGNAL_THRESHOLD-1}/100\n"
                    f"- STRONG (Entry): {SIGNAL_THRESHOLD}-{HIGH_STRENGTH-1}/100\n"
                    f"- VERY STRONG: {HIGH_STRENGTH}+/100\n"
                    f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                )
                send_ntfy(connection_msg, "Multi-Crypto Bot Connected", "high")
                
                print(f"\n{'='*70}")
                print(f"MULTI-CRYPTO 3-LEVEL SIGNAL STRENGTH BOT")
                print(f"{'='*70}")
                print(f"Monitoring {len(state.symbols)} symbols:")
                for symbol in state.symbols:
                    print(f"  - {symbol}")
                print(f"\nTimeframes: {INTERVAL} (Primary), {CONFIRM_TF} (Confirmation)")
                print(f"\nSIGNAL LEVELS:")
                print(f"  - MEDIUM (Watch): {MEDIUM_THRESHOLD}-{SIGNAL_THRESHOLD-1}/100")
                print(f"  - STRONG (Entry): {SIGNAL_THRESHOLD}-{HIGH_STRENGTH-1}/100")
                print(f"  - VERY STRONG: {HIGH_STRENGTH}+/100")
                print(f"\nDashboard: https://test-n998.onrender.com")
                print(f"Health Check: https://test-n998.onrender.com/ping")
                print(f"{'='*70}")
                print(f"Waiting for data and calculating signal strengths...\n")
                
                # Start market data updater
                asyncio.create_task(update_market_data())
                
                while True:
                    try:
                        msg = await multiplex_stream.recv()
                        stream_name = msg['stream']
                        data = msg['data']
                        
                        # Parse symbol from stream name (e.g., btcusdt@kline_4h)
                        symbol_str = stream_name.split('@')[0].upper()
                        
                        if INTERVAL in stream_name:
                            handle_kline(symbol_str, data, INTERVAL)
                        elif CONFIRM_TF in stream_name:
                            handle_kline(symbol_str, data, CONFIRM_TF)
                            
                    except Exception as e:
                        print(f"Error processing message: {e}")
                        await asyncio.sleep(1)
                        
        except Exception as e:
            error_msg = f"WebSocket Error: {str(e)[:100]}"
            send_ntfy(error_msg, "Connection Lost", "high")
            print(f"WebSocket error: {e}")
            print(f"Reconnecting in {reconnect_delay} seconds...")
            
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 1.5, 60)

# ────────────────────────────────────────────────
#                   Main Entry
# ────────────────────────────────────────────────

if __name__ == "__main__":
    # Startup notification
    startup_msg = (
        "Multi-Crypto 3-Level Signal Bot STARTED\n\n"
        f"Symbols: {', '.join(SYMBOLS)}\n"
        f"Timeframes: {INTERVAL} + {CONFIRM_TF}\n\n"
        "Signal Levels:\n"
        f"- MEDIUM (Watch): {MEDIUM_THRESHOLD}-{SIGNAL_THRESHOLD-1}/100\n"
        f"- STRONG (Entry): {SIGNAL_THRESHOLD}-{HIGH_STRENGTH-1}/100\n"
        f"- VERY STRONG: {HIGH_STRENGTH}+/100\n\n"
        f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    
    send_ntfy(startup_msg, "Multi-Crypto Bot Started", "high")
    
    # ✅ الأهم: ابدأ Flask أولاً ثم WebSocket في الخلفية
    port = int(os.environ.get("PORT", 10000))
    
    print(f"\n{'='*80}")
    print(f"🔥 STARTING FLASK SERVER ON PORT {port} FIRST...")
    print(f"{'='*80}")
    
    # ابدأ WebSocket في thread منفصل
    def start_websocket():
        time.sleep(5)  # انتظر حتى يبدأ Flask أولاً
        print(f"\n{'='*80}")
        print(f"🌐 STARTING WEBSOCKET CONNECTION...")
        print(f"{'='*80}")
        asyncio.run(run_websockets())
    
    ws_thread = threading.Thread(target=start_websocket, daemon=True)
    ws_thread.start()
    
    # ✅ ابدأ Flask الآن - هذا ما يهم Render
    print(f"\n✅ Flask is ready on port {port}")
    print(f"✅ Health check: http://0.0.0.0:{port}/ping")
    print(f"✅ Dashboard: http://0.0.0.0:{port}/")
    print(f"✅ Render URL: https://test-n998.onrender.com")
    print(f"\n{'='*80}")
    
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
