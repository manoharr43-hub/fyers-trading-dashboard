import pandas as pd
import numpy as np


# ==========================================
# Simple Moving Average (SMA)
# ==========================================
def sma(close, period=20):
    return close.rolling(window=period).mean()


# ==========================================
# Exponential Moving Average (EMA)
# ==========================================
def ema(close, period=20):
    return close.ewm(span=period, adjust=False).mean()


# ==========================================
# Relative Strength Index (RSI)
# ==========================================
def rsi(close, period=14):

    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss

    rsi = 100 - (100 / (1 + rs))

    return rsi


# ==========================================
# MACD
# ==========================================
def macd(close):

    ema12 = ema(close, 12)
    ema26 = ema(close, 26)

    macd_line = ema12 - ema26
    signal_line = ema(macd_line, 9)
    histogram = macd_line - signal_line

    return macd_line, signal_line, histogram


# ==========================================
# VWAP
# ==========================================
def vwap(df):

    typical_price = (
        df["High"] +
        df["Low"] +
        df["Close"]
    ) / 3

    return (
        typical_price * df["Volume"]
    ).cumsum() / df["Volume"].cumsum()


# ==========================================
# Bollinger Bands
# ==========================================
def bollinger_bands(close, period=20, std_dev=2):

    middle = sma(close, period)

    std = close.rolling(period).std()

    upper = middle + (std_dev * std)

    lower = middle - (std_dev * std)

    return upper, middle, lower


# ==========================================
# Supertrend (Basic)
# ==========================================
def atr(df, period=14):

    high_low = df["High"] - df["Low"]

    high_close = abs(df["High"] - df["Close"].shift())

    low_close = abs(df["Low"] - df["Close"].shift())

    tr = pd.concat(
        [high_low, high_close, low_close],
        axis=1
    ).max(axis=1)

    return tr.rolling(period).mean()


# ==========================================
# Volume Average
# ==========================================
def volume_average(volume, period=20):

    return volume.rolling(period).mean()


# ==========================================
# Buy Signal
# ==========================================
def buy_signal(df):

    df["EMA20"] = ema(df["Close"], 20)
    df["EMA50"] = ema(df["Close"], 50)
    df["RSI"] = rsi(df["Close"])

    last = df.iloc[-1]

    return (
        last["EMA20"] > last["EMA50"]
        and last["RSI"] > 60
    )


# ==========================================
# Sell Signal
# ==========================================
def sell_signal(df):

    df["EMA20"] = ema(df["Close"], 20)
    df["EMA50"] = ema(df["Close"], 50)
    df["RSI"] = rsi(df["Close"])

    last = df.iloc[-1]

    return (
        last["EMA20"] < last["EMA50"]
        and last["RSI"] < 40
    )
