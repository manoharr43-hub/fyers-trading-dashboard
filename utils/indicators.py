"""
utils/indicators.py

Technical Indicators for FYERS Trading Dashboard
"""

import pandas as pd
import numpy as np


# ============================================
# Simple Moving Average (SMA)
# ============================================

def sma(df, period=20):
    return df["close"].rolling(period).mean()


# ============================================
# Exponential Moving Average (EMA)
# ============================================

def ema(df, period=20):
    return df["close"].ewm(span=period, adjust=False).mean()


# ============================================
# Relative Strength Index (RSI)
# ============================================

def rsi(df, period=14):

    delta = df["close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss

    rsi = 100 - (100 / (1 + rs))

    return rsi


# ============================================
# MACD
# ============================================

def macd(df):

    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()

    macd_line = ema12 - ema26
    signal = macd_line.ewm(span=9, adjust=False).mean()
    histogram = macd_line - signal

    return macd_line, signal, histogram


# ============================================
# Bollinger Bands
# ============================================

def bollinger_bands(df, period=20):

    sma20 = sma(df, period)

    std = df["close"].rolling(period).std()

    upper = sma20 + (2 * std)
    lower = sma20 - (2 * std)

    return upper, sma20, lower


# ============================================
# VWAP
# ============================================

def vwap(df):

    tp = (df["high"] + df["low"] + df["close"]) / 3

    return (tp * df["volume"]).cumsum() / df["volume"].cumsum()


# ============================================
# ATR
# ============================================

def atr(df, period=14):

    high_low = df["high"] - df["low"]

    high_close = np.abs(df["high"] - df["close"].shift())

    low_close = np.abs(df["low"] - df["close"].shift())

    ranges = pd.concat(
        [high_low, high_close, low_close],
        axis=1
    )

    true_range = ranges.max(axis=1)

    atr = true_range.rolling(period).mean()

    return atr


# ============================================
# Supertrend
# ============================================

def supertrend(df, period=10, multiplier=3):

    atr_value = atr(df, period)

    hl2 = (df["high"] + df["low"]) / 2

    upperband = hl2 + multiplier * atr_value
    lowerband = hl2 - multiplier * atr_value

    trend = []

    for i in range(len(df)):
        if i == 0:
            trend.append(True)
        else:
            if df["close"].iloc[i] > upperband.iloc[i - 1]:
                trend.append(True)
            elif df["close"].iloc[i] < lowerband.iloc[i - 1]:
                trend.append(False)
            else:
                trend.append(trend[-1])

    return pd.Series(trend, index=df.index)


# ============================================
# Add All Indicators
# ============================================

def add_indicators(df):

    df = df.copy()

    df["SMA20"] = sma(df, 20)
    df["EMA20"] = ema(df, 20)
    df["RSI"] = rsi(df)

    macd_line, signal, hist = macd(df)

    df["MACD"] = macd_line
    df["MACD_SIGNAL"] = signal
    df["MACD_HIST"] = hist

    upper, middle, lower = bollinger_bands(df)

    df["BB_UPPER"] = upper
    df["BB_MIDDLE"] = middle
    df["BB_LOWER"] = lower

    df["VWAP"] = vwap(df)

    df["ATR"] = atr(df)

    df["SUPERTREND"] = supertrend(df)

    return df
