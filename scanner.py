# ==========================================================
# NSE AI PRO V13
# PART 1 - IMPORTS & CONFIGURATION
# ==========================================================

import os
import json
import time
import logging
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import streamlit as st
import pandas as pd
import numpy as np

# ==========================================================
# PROJECT FOLDERS
# ==========================================================

BASE_DIR = Path.cwd()

SIGNALS_DIR = BASE_DIR / "signals"
BUY_DIR = SIGNALS_DIR / "buy"
SELL_DIR = SIGNALS_DIR / "sell"
LOG_DIR = BASE_DIR / "logs"
EXPORT_DIR = BASE_DIR / "exports"
CHART_DIR = BASE_DIR / "charts"

for folder in [
    SIGNALS_DIR,
    BUY_DIR,
    SELL_DIR,
    LOG_DIR,
    EXPORT_DIR,
    CHART_DIR
]:
    folder.mkdir(parents=True, exist_ok=True)

# ==========================================================
# LOGGING
# ==========================================================

logging.basicConfig(
    filename=LOG_DIR / "scanner.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

# ==========================================================
# TIMEFRAMES
# ==========================================================

TIMEFRAMES = {
    "5 Minute": "5",
    "15 Minute": "15",
    "1 Hour": "60",
    "4 Hour": "240",
    "Daily": "D"
}

# ==========================================================
# SCANNER TYPES
# ==========================================================

SCANNERS = [

    "Intraday 5 Minute",

    "Intraday 15 Minute",

    "Swing",

    "Fuel",

    "Golden Cross",

    "4 Hour",

    "Pre Market",

    "After Market",

    "F&O Scanner"

]

# ==========================================================
# AI SCORE WEIGHTS
# ==========================================================

AI_WEIGHTS = {

    "BOS":10,

    "CHOCH":10,

    "CISD":10,

    "Bullish Order Block":15,

    "Bearish Order Block":15,

    "Volume":10,

    "VWAP":5,

    "RSI":5,

    "MACD":5,

    "Supertrend":5,

    "RVOL":10

}

# ==========================================================
# SIGNAL CACHE
# ==========================================================

LAST_SIGNALS = {}

# ==========================================================
# CREATE SIGNAL
# ==========================================================

def create_signal():

    return {

        "Signal Date":"",

        "Signal Time":"",

        "Stock":"",

        "LTP":0,

        "Gap %":0,

        "Target":0,

        "Stoploss":0,

        "SMC Structure":"",

        "CISD":"",

        "Bullish Order Block":"",

        "Bearish Order Block":"",

        "Order Block Zone":"",

        "Order Block Strength":"",

        "Order Block Signal":"",

        "Order Block Entry":0,

        "Order Block SL":0,

        "Order Block Target1":0,

        "Order Block Target2":0,

        "BOS":"",

        "CHOCH":"",

        "Liquidity Sweep":"",

        "FVG":"",

        "Volume Confirmation":"",

        "AI Score":0,

        "AI Trend":"",

        "AI Confidence (%)":0,

        "Trade Decision":"WATCH"

    }

# ==========================================================
# AI SCORE
# ==========================================================

def calculate_ai_score(signal):

    score = 0

    for key, weight in AI_WEIGHTS.items():

        value = str(signal.get(key,""))

        if value not in ["","NO","False","0"]:

            score += weight

    return min(score,100)
# ==========================================================
# PART 2 - SMC DETECTION ENGINE
# BOS • CHOCH • CISD • Order Block • FVG
# ==========================================================

import pandas as pd
import numpy as np
from datetime import datetime

# ----------------------------------------------------------
# Volume Confirmation
# ----------------------------------------------------------

def volume_confirmation(df):

    avg_vol = df["Volume"].tail(20).mean()

    current = df["Volume"].iloc[-1]

    if current > avg_vol * 2:
        return "Very Strong"

    elif current > avg_vol:
        return "Strong"

    return "Weak"

# ----------------------------------------------------------
# Break Of Structure (BOS)
# ----------------------------------------------------------

def detect_bos(df):

    if len(df) < 30:
        return False

    previous_high = df["High"].rolling(20).max().shift(1).iloc[-1]

    current_close = df["Close"].iloc[-1]

    return current_close > previous_high

# ----------------------------------------------------------
# Change Of Character (CHOCH)
# ----------------------------------------------------------

def detect_choch(df):

    if len(df) < 30:
        return False

    previous_low = df["Low"].rolling(20).min().shift(1).iloc[-1]

    current_close = df["Close"].iloc[-1]

    return current_close < previous_low

# ----------------------------------------------------------
# CISD
# ----------------------------------------------------------

def detect_cisd(df):

    if len(df) < 5:
        return False

    c1 = df.iloc[-2]

    c2 = df.iloc[-1]

    if (

        c1["Close"] < c1["Open"]

        and

        c2["Close"] > c1["High"]

    ):

        return True

    return False

# ----------------------------------------------------------
# Bullish Order Block
# ----------------------------------------------------------

def bullish_order_block(df):

    if len(df) < 30:
        return None

    previous = df.iloc[-2]

    current = df.iloc[-1]

    if (

        previous["Close"] < previous["Open"]

        and

        current["Close"] > previous["High"]

    ):

        return {

            "Type":"Bullish",

            "High":round(previous["High"],2),

            "Low":round(previous["Low"],2),

            "Entry":round(current["Close"],2),

            "SL":round(previous["Low"],2),

            "Target1":round(current["Close"]*1.02,2),

            "Target2":round(current["Close"]*1.04,2)

        }

    return None

# ----------------------------------------------------------
# Bearish Order Block
# ----------------------------------------------------------

def bearish_order_block(df):

    if len(df) < 30:
        return None

    previous = df.iloc[-2]

    current = df.iloc[-1]

    if (

        previous["Close"] > previous["Open"]

        and

        current["Close"] < previous["Low"]

    ):

        return {

            "Type":"Bearish",

            "High":round(previous["High"],2),

            "Low":round(previous["Low"],2),

            "Entry":round(current["Close"],2),

            "SL":round(previous["High"],2),

            "Target1":round(current["Close"]*0.98,2),

            "Target2":round(current["Close"]*0.96,2)

        }

    return None

# ----------------------------------------------------------
# Fair Value Gap
# ----------------------------------------------------------

def detect_fvg(df):

    if len(df) < 3:
        return False

    c1 = df.iloc[-3]

    c3 = df.iloc[-1]

    return c3["Low"] > c1["High"]

# ----------------------------------------------------------
# Liquidity Sweep
# ----------------------------------------------------------

def detect_liquidity_sweep(df):

    if len(df) < 10:
        return False

    previous_high = df["High"].tail(10).max()

    current = df.iloc[-1]

    return (

        current["High"] > previous_high

        and

        current["Close"] < previous_high

    )

# ----------------------------------------------------------
# AI Signal Builder
# ----------------------------------------------------------

def build_ai_signal(df, symbol):

    signal = create_signal()

    signal["Stock"] = symbol

    signal["Signal Date"] = datetime.now().strftime("%d-%m-%Y")

    signal["Signal Time"] = datetime.now().strftime("%H:%M:%S")

    signal["LTP"] = round(df["Close"].iloc[-1],2)

    signal["BOS"] = detect_bos(df)

    signal["CHOCH"] = detect_choch(df)

    signal["CISD"] = detect_cisd(df)

    signal["FVG"] = detect_fvg(df)

    signal["Liquidity Sweep"] = detect_liquidity_sweep(df)

    signal["Volume Confirmation"] = volume_confirmation(df)

    bull = bullish_order_block(df)

    bear = bearish_order_block(df)

    if bull:

        signal["Bullish Order Block"] = "YES"

        signal["Order Block Signal"] = "BUY"

        signal["Order Block Entry"] = bull["Entry"]

        signal["Order Block SL"] = bull["SL"]

        signal["Order Block Target1"] = bull["Target1"]

        signal["Order Block Target2"] = bull["Target2"]

    if bear:

        signal["Bearish Order Block"] = "YES"

        signal["Order Block Signal"] = "SELL"

        signal["Order Block Entry"] = bear["Entry"]

        signal["Order Block SL"] = bear["SL"]

        signal["Order Block Target1"] = bear["Target1"]

        signal["Order Block Target2"] = bear["Target2"]

    signal["AI Score"] = calculate_ai_score(signal)

    return signal
