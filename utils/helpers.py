"""
utils/helpers.py

Common Helper Functions
FYERS Trading Dashboard
"""

import pandas as pd
from datetime import datetime


# ==========================================
# Convert FYERS History to DataFrame
# ==========================================

def candles_to_dataframe(response):
    """
    Convert FYERS history response into DataFrame
    """

    if response.get("s") != "ok":
        return pd.DataFrame()

    candles = response.get("candles", [])

    if not candles:
        return pd.DataFrame()

    df = pd.DataFrame(
        candles,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ],
    )

    df["datetime"] = pd.to_datetime(
        df["timestamp"],
        unit="s"
    )

    return df


# ==========================================
# Price Formatting
# ==========================================

def price(value):

    try:
        return f"₹{float(value):,.2f}"
    except:
        return "₹0.00"


# ==========================================
# Percentage Formatting
# ==========================================

def percent(value):

    try:
        return f"{float(value):.2f}%"
    except:
        return "0.00%"


# ==========================================
# Quantity Formatting
# ==========================================

def qty(value):

    try:
        return int(value)
    except:
        return 0


# ==========================================
# Number Formatting
# ==========================================

def number(value):

    try:
        return f"{float(value):,.2f}"
    except:
        return "0"


# ==========================================
# Current Time
# ==========================================

def now():

    return datetime.now().strftime(
        "%d-%m-%Y %H:%M:%S"
    )


# ==========================================
# Exchange Symbols
# ==========================================

NIFTY = "NSE:NIFTY50-INDEX"
BANKNIFTY = "NSE:NIFTYBANK-INDEX"
FINNIFTY = "NSE:FINNIFTY-INDEX"
MIDCPNIFTY = "NSE:MIDCPNIFTY-INDEX"
SENSEX = "BSE:SENSEX-INDEX"


# ==========================================
# Default Watchlist
# ==========================================

DEFAULT_WATCHLIST = [
    "NSE:RELIANCE-EQ",
    "NSE:TCS-EQ",
    "NSE:INFY-EQ",
    "NSE:HDFCBANK-EQ",
    "NSE:ICICIBANK-EQ",
    "NSE:SBIN-EQ",
    "NSE:LT-EQ",
    "NSE:ITC-EQ",
    "NSE:BHARTIARTL-EQ",
    "NSE:AXISBANK-EQ",
]


# ==========================================
# Signal Colors
# ==========================================

BUY_COLOR = "green"
SELL_COLOR = "red"
HOLD_COLOR = "orange"


# ==========================================
# Buy/Sell Signal
# ==========================================

def signal(close, ema20):

    if close > ema20:
        return "BUY"

    elif close < ema20:
        return "SELL"

    return "HOLD"
