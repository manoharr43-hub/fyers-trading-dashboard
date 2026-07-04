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
# ==========================================================
# PART 3
# INTRADAY 15 MINUTE AI SCANNER
# ==========================================================

import pandas as pd
import numpy as np

# ==========================================================
# RSI
# ==========================================================

def calculate_rsi(close, period=14):

    delta = close.diff()

    gain = delta.clip(lower=0)

    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()

    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    return 100 - (100 / (1 + rs))

# ==========================================================
# EMA
# ==========================================================

def ema(close, period):
    return close.ewm(span=period, adjust=False).mean()

# ==========================================================
# MACD
# ==========================================================

def calculate_macd(close):

    ema12 = ema(close,12)

    ema26 = ema(close,26)

    macd = ema12-ema26

    signal = macd.ewm(span=9,adjust=False).mean()

    return macd.iloc[-1],signal.iloc[-1]

# ==========================================================
# VWAP
# ==========================================================

def calculate_vwap(df):

    tp = (df["High"]+df["Low"]+df["Close"])/3

    return ((tp*df["Volume"]).cumsum()/df["Volume"].cumsum()).iloc[-1]

# ==========================================================
# SUPERTREND
# ==========================================================

def supertrend_signal(df):

    ema20 = ema(df["Close"],20)

    return "Bullish" if df["Close"].iloc[-1]>ema20.iloc[-1] else "Bearish"

# ==========================================================
# AI DECISION
# ==========================================================

def ai_trade_decision(signal):

    score = signal["AI Score"]

    if score >= 90:
        return "🟢 STRONG BUY"

    elif score >= 75:
        return "🟢 BUY"

    elif score >= 60:
        return "🟡 WATCH"

    elif score >= 40:
        return "🟠 WEAK"

    return "🔴 SELL"

# ==========================================================
# 15 MIN AI SCANNER
# ==========================================================

def intraday_15m_ai(df,symbol):

    signal = build_ai_signal(df,symbol)

    signal["RSI"] = round(
        calculate_rsi(df["Close"]).iloc[-1],2
    )

    macd,macd_signal = calculate_macd(df["Close"])

    signal["MACD Signal"] = "Bullish" if macd>macd_signal else "Bearish"

    signal["VWAP"] = round(
        calculate_vwap(df),2
    )

    signal["Supertrend"] = supertrend_signal(df)

    signal["Trade Decision"] = ai_trade_decision(signal)

    signal["AI Confidence (%)"] = signal["AI Score"]

    if signal["Trade Decision"]=="🟢 STRONG BUY":

        signal["Signal Strength"]="★★★★★"

    elif signal["Trade Decision"]=="🟢 BUY":

        signal["Signal Strength"]="★★★★"

    elif signal["Trade Decision"]=="🟡 WATCH":

        signal["Signal Strength"]="★★★"

    else:

        signal["Signal Strength"]="★"

    return signal

# ==========================================================
# RESULT TABLE
# ==========================================================

def scanner_dataframe(results):

    if len(results)==0:

        return pd.DataFrame()

    cols=[

        "Signal Date",

        "Signal Time",

        "Stock",

        "LTP",

        "Trade Decision",

        "AI Score",

        "AI Confidence (%)",

        "Signal Strength",

        "RSI",

        "MACD Signal",

        "VWAP",

        "Supertrend",

        "Bullish Order Block",

        "Bearish Order Block",

        "BOS",

        "CHOCH",

        "CISD",

        "Liquidity Sweep",

        "FVG",

        "Order Block Entry",

        "Order Block SL",

        "Order Block Target1",

        "Order Block Target2"

    ]

    return pd.DataFrame(results)[cols]
# ==========================================================
# PART 4
# FYERS HISTORY API + AUTO SCANNER
# ==========================================================

import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# ==========================================================
# TIMEFRAME MAP
# ==========================================================

FYERS_INTERVAL = {
    "5 Minute": "5",
    "15 Minute": "15",
    "1 Hour": "60",
    "Daily": "D"
}

# ==========================================================
# GET HISTORY
# ==========================================================

def get_history(fyers, symbol, timeframe="15 Minute", candles=200):

    try:

        data = {
            "symbol": symbol,
            "resolution": FYERS_INTERVAL[timeframe],
            "date_format": "1",
            "range_from": "2025-01-01",
            "range_to": datetime.now().strftime("%Y-%m-%d"),
            "cont_flag": "1"
        }

        response = fyers.history(data)

        if response.get("s") != "ok":
            return None

        df = pd.DataFrame(
            response["candles"],
            columns=[
                "Timestamp",
                "Open",
                "High",
                "Low",
                "Close",
                "Volume"
            ]
        )

        df["Timestamp"] = pd.to_datetime(
            df["Timestamp"],
            unit="s"
        )

        return df.tail(candles)

    except Exception:
        return None

# ==========================================================
# DEFAULT NSE UNIVERSE
# ==========================================================

DEFAULT_STOCKS = [
    "NSE:RELIANCE-EQ",
    "NSE:TCS-EQ",
    "NSE:INFY-EQ",
    "NSE:HDFCBANK-EQ",
    "NSE:ICICIBANK-EQ",
    "NSE:SBIN-EQ",
    "NSE:LT-EQ",
    "NSE:AXISBANK-EQ",
    "NSE:ITC-EQ",
    "NSE:BHARTIARTL-EQ"
]

# ==========================================================
# SCAN ONE SYMBOL
# ==========================================================

def scan_symbol(fyers, symbol):

    df = get_history(fyers, symbol, "15 Minute")

    if df is None or len(df) < 50:
        return None

    signal = intraday_15m_ai(df, symbol)

    return signal

# ==========================================================
# MULTI THREAD SCANNER
# ==========================================================

def run_scanner(fyers, symbols):

    results = []

    progress = st.progress(0)

    total = len(symbols)

    with ThreadPoolExecutor(max_workers=8) as executor:

        futures = {
            executor.submit(scan_symbol, fyers, s): s
            for s in symbols
        }

        completed = 0

        for future in as_completed(futures):

            completed += 1

            progress.progress(completed / total)

            try:
                signal = future.result()

                if signal:
                    results.append(signal)

            except Exception:
                pass

    return results

# ==========================================================
# STREAMLIT PAGE
# ==========================================================

def show_scanner(fyers):

    st.title("📈 NSE AI PRO V13 Scanner")

    scanner_type = st.selectbox(
        "Scanner",
        [
            "15 Minute AI Scanner"
        ]
    )

    universe = st.multiselect(
        "Stocks",
        DEFAULT_STOCKS,
        default=DEFAULT_STOCKS
    )

    if st.button("🚀 Start Scan"):

        with st.spinner("Scanning..."):

            results = run_scanner(
                fyers,
                universe
            )

            if len(results) == 0:

                st.warning("No Signals Found")

            else:

                df = scanner_dataframe(results)

                st.dataframe(
                    df,
                    use_container_width=True,
                    height=600
                )

                st.download_button(
                    "📥 Download CSV",
                    df.to_csv(index=False),
                    file_name="scanner_results.csv",
                    mime="text/csv"
                )

                st.success(f"{len(df)} Signals Found")
# ==========================================================
# PART 5
# PROFESSIONAL DASHBOARD
# ==========================================================

from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font

# ----------------------------------------------------------
# AI RANK
# ----------------------------------------------------------

def ai_rank(score):

    if score >= 90:
        return "★★★★★"

    elif score >= 80:
        return "★★★★"

    elif score >= 70:
        return "★★★"

    elif score >= 60:
        return "★★"

    return "★"

# ----------------------------------------------------------
# PRE MARKET
# ----------------------------------------------------------

def premarket_status(df):

    prev_close = df["Close"].iloc[-2]

    today_open = df["Open"].iloc[-1]

    gap = ((today_open-prev_close)/prev_close)*100

    if gap > 1:

        return "Gap Up"

    elif gap < -1:

        return "Gap Down"

    return "Flat"

# ----------------------------------------------------------
# AFTER MARKET
# ----------------------------------------------------------

def closing_strength(df):

    close = df["Close"].iloc[-1]

    high = df["High"].iloc[-1]

    low = df["Low"].iloc[-1]

    pos = (close-low)/(high-low+0.01)

    if pos > 0.75:

        return "Strong Close"

    elif pos < 0.30:

        return "Weak Close"

    return "Neutral"

# ----------------------------------------------------------
# F&O
# ----------------------------------------------------------

def is_fno(symbol):

    FNO = [

        "NSE:RELIANCE-EQ",

        "NSE:TCS-EQ",

        "NSE:INFY-EQ",

        "NSE:HDFCBANK-EQ",

        "NSE:ICICIBANK-EQ"

    ]

    return symbol in FNO

# ----------------------------------------------------------
# NEWS PLACEHOLDER
# ----------------------------------------------------------

def latest_news(symbol):

    return "No News"

# ----------------------------------------------------------
# EXCEL EXPORT
# ----------------------------------------------------------

def export_excel(df):

    wb = Workbook()

    ws = wb.active

    ws.title = "Scanner"

    header_fill = PatternFill(
        fill_type="solid",
        start_color="1F4E78"
    )

    buy_fill = PatternFill(
        fill_type="solid",
        start_color="00AA00"
    )

    sell_fill = PatternFill(
        fill_type="solid",
        start_color="FF0000"
    )

    watch_fill = PatternFill(
        fill_type="solid",
        start_color="FFD966"
    )

    for col, name in enumerate(df.columns,1):

        c = ws.cell(row=1,column=col)

        c.value = name

        c.fill = header_fill

        c.font = Font(
            bold=True,
            color="FFFFFF"
        )

    for r in df.itertuples(index=False):

        ws.append(list(r))

    decision_col = list(df.columns).index("Trade Decision")+1

    for row in range(2,ws.max_row+1):

        cell = ws.cell(row=row,column=decision_col)

        if "BUY" in str(cell.value):

            cell.fill = buy_fill

        elif "SELL" in str(cell.value):

            cell.fill = sell_fill

        else:

            cell.fill = watch_fill

    filename = "exports/NSE_AI_PRO.xlsx"

    wb.save(filename)

    return filename
