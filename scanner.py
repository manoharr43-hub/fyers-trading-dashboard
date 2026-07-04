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
