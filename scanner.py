#!/usr/bin/env python3
"""
NSE AI PRO V14 - Standalone Scanner
3-in-1: 15-Min Reversal + Volume BigMove + F&O OI Analysis
Run: python scanner.py
"""

import pandas as pd
import numpy as np
import requests
import time
import os
import re
from datetime import datetime, timedelta
from typing import List, Tuple, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import warnings
warnings.filterwarnings('ignore')

# ════════════════════════════════════════════════════════════════════════════════
# CONFIG & CONSTANTS
# ════════════════════════════════════════════════════════════════════════════════
try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except:
    from datetime import timezone
    IST = timezone(timedelta(hours=5, minutes=30))

# Fyers Config
FYERS_ACCESS_TOKEN = os.environ.get("FYERS_ACCESS_TOKEN", "")
FYERS_NSE_CM_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_CM.csv"

# Scanner Settings
DATE_FROM = (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")
DATE_TO = datetime.today().strftime("%Y-%m-%d")
MAX_WORKERS = 8
BATCH_SIZE = 50
BATCH_PAUSE_SECONDS = 0.5

# 15-Min Reversal Settings
REVERSAL_RESOLUTION = "15"
REVERSAL_LOOKBACK_DAYS = 5
REVERSAL_ATR_LENGTH = 5
REVERSAL_ATR_MULTIPLIER = 2.8
REVERSAL_MIN_MOVE_PCT = 0.015

# Volume BigMove Settings
VOL_BIGMOVE_MIN_RVOL = 2.5
VOL_BIGMOVE_MIN_BODY_PCT = 0.8
VOL_BIGMOVE_LOOKBACK = 20

# F&O OI Settings
FO_OI_PCR_THRESHOLD_HIGH = 1.5
FO_OI_PCR_THRESHOLD_LOW = 0.7
FO_OI_OI_CHANGE_THRESHOLD = 5
FO_OI_MIN_OI_VALUE = 100000

# ════════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ════════════════════════════════════════════════════════════════════════════════
def now_ist():
    return datetime.now(IST)

def print_header(text):
    print(f"\n{'='*80}")
    print(f"  {text}")
    print(f"{'='*80}\n")

def print_section(text):
    print(f"\n{text}")
    print("-" * len(text))

# ════════════════════════════════════════════════════════════════════════════════
# FYERS API WRAPPER
# ════════════════════════════════════════════════════════════════════════════════
class FyersAPI:
    def __init__(self, token):
        self.token = token
        self.base_url = "https://api-t2.fyers.in/api/v3"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
    
    def history(self, params):
        """Fetch candle history"""
        url = f"{self.base_url}/history"
        try:
            resp = requests.get(url, params=params, headers=self.headers, timeout=15)
            return resp.json()
        except Exception as e:
            return {"s": "error", "message": str(e)}

# ════════════════════════════════════════════════════════════════════════════════
# INDICATORS
# ════════════════════════════════════════════════════════════════════════════════
def calculate_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)

def calculate_macd(close):
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def calculate_atr(df, period=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()

def last_atr(df, period=14):
    atr_series = calculate_atr(df, period)
    val = atr_series.iloc[-1] if len(atr_series) > 0 else 0.01
    if pd.isna(val) or val <= 0:
        val = max(float(df["Close"].iloc[-1] * 0.005), 0.01)
    return float(val)

# ════════════════════════════════════════════════════════════════════════════════
# SYMBOL LOADING
# ════════════════════════════════════════════════════════════════════════════════
def load_symbols(limit=100):
    """Load NSE symbols"""
    print_section("Loading NSE symbols...")
    try:
        resp = requests.get(FYERS_NSE_CM_SYMBOL_MASTER, timeout=20)
        lines = [ln.strip() for ln in resp.text.split("\n") if ln.strip()]
        
        symbols = []
        for line in lines:
            parts = line.split(",")
            if len(parts) > 0:
                sym = parts[0].strip()
                if sym.startswith("NSE:") and sym.endswith("-EQ"):
                    symbols.append(sym)
        
        symbols = sorted(set(symbols))
        if limit and limit > 0:
            symbols = symbols[:limit]
        
        print(f"✓ Loaded {len(symbols)} symbols")
        return symbols
    except Exception as e:
        print(f"✗ Error loading symbols: {e}")
        return []

# ════════════════════════════════════════════════════════════════════════════════
# SAFE HISTORY FETCH
# ════════════════════════════════════════════════════════════════════════════════
def safe_history(fyers, params, max_retries=3):
    """Fetch history with retry logic"""
    symbol = params.get("symbol", "?")
    for attempt in range(1, max_retries + 1):
        try:
            resp = fyers.history(params)
            if resp.get("s") == "ok":
                return resp, None
            else:
                msg = resp.get("message", "unknown error")
                if "rate" in msg.lower():
                    time.sleep(1.0 * attempt)
                    continue
                return None, f"{symbol}: {msg}"
        except Exception as e:
            if attempt < max_retries:
                time.sleep(0.5 * attempt)
                continue
            return None, f"{symbol}: {str(e)}"
    return None, f"{symbol}: Max retries exceeded"

# ════════════════════════════════════════════════════════════════════════════════
# 15-MIN REVERSAL SCANNER
# ════════════════════════════════════════════════════════════════════════════════
def detect_reversal(df):
    """Detect reversal zones"""
    if len(df) < 30:
        return {"type": "NONE"}
    
    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    
    atr_val = last_atr(df, REVERSAL_ATR_LENGTH)
    threshold = max(float(close.iloc[-1]) * REVERSAL_MIN_MOVE_PCT / 100, REVERSAL_ATR_MULTIPLIER * atr_val)
    
    lookback = 20
    recent = df.tail(lookback)
    recent_high = float(recent["High"].max())
    recent_low = float(recent["Low"].min())
    last_close = float(close.iloc[-1])
    
    # Bearish signal
    if abs(recent_high - last_close) < threshold * 1.5:
        if close.iloc[-1] < close.iloc[-2]:
            return {
                "type": "SELL",
                "price": round(recent_high, 2),
                "strength": "Strong" if abs(recent_high - last_close) < threshold * 0.5 else "Medium",
            }
    
    # Bullish signal
    if abs(last_close - recent_low) < threshold * 1.5:
        if close.iloc[-1] > close.iloc[-2]:
            return {
                "type": "BUY",
                "price": round(recent_low, 2),
                "strength": "Strong" if abs(last_close - recent_low) < threshold * 0.5 else "Medium",
            }
    
    return {"type": "NONE"}

def scan_reversal(fyers, symbol):
    """Scan 15-min reversal"""
    stock = symbol.replace("NSE:", "").replace("-EQ", "")
    date_from = (datetime.today() - timedelta(days=REVERSAL_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    
    resp, err = safe_history(fyers, {
        "symbol": symbol,
        "resolution": REVERSAL_RESOLUTION,
        "date_format": "1",
        "range_from": date_from,
        "range_to": datetime.today().strftime("%Y-%m-%d"),
        "cont_flag": "1",
    })
    
    if err or not resp:
        return None, err
    
    candles = resp.get("candles", [])
    if not candles or len(candles) < 30:
        return None, None
    
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Close"]).sort_values("Time").reset_index(drop=True)
        
        if len(df) < 30:
            return None, None
        
        # Remove unclosed candle
        if len(df) > 0:
            last_time = df["Time"].iloc[-1]
            candle_age = (now_ist() - last_time).total_seconds() / 60
            if candle_age < 15:
                df = df.iloc[:-1].reset_index(drop=True)
        
        if len(df) < 30:
            return None, None
        
        reversal = detect_reversal(df)
        if reversal["type"] == "NONE":
            return None, None
        
        last_close = float(df["Close"].iloc[-1])
        atr = last_atr(df)
        rsi_val = float(calculate_rsi(df["Close"]).iloc[-1])
        macd_line, macd_sig, _ = calculate_macd(df["Close"])
        macd_bullish = bool(macd_line.iloc[-1] > macd_sig.iloc[-1])
        
        is_buy = reversal["type"] == "BUY"
        entry = round(last_close, 2)
        sl = round(entry - 1.5 * atr, 2) if is_buy else round(entry + 1.5 * atr, 2)
        t1 = round(entry + 1.5 * atr, 2) if is_buy else round(entry - 1.5 * atr, 2)
        t2 = round(entry + 2.5 * atr, 2) if is_buy else round(entry - 2.5 * atr, 2)
        
        risk = abs(entry - sl)
        rr = round((abs(t1 - entry) / risk), 2) if risk > 0 else 0.0
        
        vol_avg = float(df["Volume"].tail(20).mean())
        rvol = round(float(df["Volume"].iloc[-1] / vol_avg), 2) if vol_avg > 0 else 0.0
        
        conf = min(95.0, max(35.0, 50 + abs(rsi_val - 50) * 0.5 + rvol * 8 + rr * 5))
        
        return {
            "Stock": stock,
            "LTP": entry,
            "Type": "BUY" if is_buy else "SELL",
            "Entry": entry,
            "SL": sl,
            "T1": t1,
            "T2": t2,
            "RR": rr,
            "Confidence": round(conf, 1),
            "RSI": round(rsi_val, 1),
            "MACD": "Bull" if macd_bullish else "Bear",
            "RVOL": rvol,
            "Zone": reversal["price"],
        }, None
        
    except Exception as e:
        return None, f"{symbol}: {type(e).__name__}"

# ════════════════════════════════════════════════════════════════════════════════
# VOLUME BIG MOVEMENT SCANNER
# ════════════════════════════════════════════════════════════════════════════════
def detect_bigmove(df):
    """Detect volume big move"""
    if len(df) < VOL_BIGMOVE_LOOKBACK:
        return "NONE", 0.0
    
    last = df.iloc[-1]
    recent = df.tail(VOL_BIGMOVE_LOOKBACK)
    
    vol_avg = float(recent["Volume"].mean())
    last_vol = float(last["Volume"])
    rvol = last_vol / vol_avg if vol_avg > 0 else 0.0
    
    if rvol < VOL_BIGMOVE_MIN_RVOL:
        return "NONE", 0.0
    
    body = abs(float(last["Close"]) - float(last["Open"]))
    body_pct = (body / float(last["Open"]) * 100) if last["Open"] > 0 else 0.0
    
    if body_pct < VOL_BIGMOVE_MIN_BODY_PCT:
        return "NONE", 0.0
    
    atr = last_atr(df)
    
    if float(last["Close"]) > float(last["Open"]) and body > atr * 0.6:
        confidence = min(95.0, 40 + (body_pct * 5) + (rvol * 8))
        return "UP", confidence
    
    if float(last["Close"]) < float(last["Open"]) and body > atr * 0.6:
        confidence = min(95.0, 40 + (body_pct * 5) + (rvol * 8))
        return "DOWN", confidence
    
    return "NONE", 0.0

def scan_bigmove(fyers, symbol):
    """Scan volume big move"""
    stock = symbol.replace("NSE:", "").replace("-EQ", "")
    
    resp, err = safe_history(fyers, {
        "symbol": symbol,
        "resolution": "D",
        "date_format": "1",
        "range_from": DATE_FROM,
        "range_to": DATE_TO,
        "cont_flag": "1",
    })
    
    if err or not resp:
        return None, err
    
    candles = resp.get("candles", [])
    if not candles or len(candles) < 30:
        return None, None
    
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Close"])
        
        if len(df) < 30:
            return None, None
        
        move_type, confidence = detect_bigmove(df)
        if move_type == "NONE":
            return None, None
        
        last_close = float(df["Close"].iloc[-1])
        last_open = float(df["Open"].iloc[-1])
        body = abs(last_close - last_open)
        body_pct = (body / last_open * 100) if last_open > 0 else 0.0
        atr = last_atr(df)
        
        vol_avg = float(df["Volume"].tail(20).mean())
        last_vol = float(df["Volume"].iloc[-1])
        rvol = round(last_vol / vol_avg, 2) if vol_avg > 0 else 0.0
        
        rsi_val = float(calculate_rsi(df["Close"]).iloc[-1])
        macd_line, macd_sig, _ = calculate_macd(df["Close"])
        macd_bullish = bool(macd_line.iloc[-1] > macd_sig.iloc[-1])
        
        is_up = move_type == "UP"
        entry = round(last_close, 2)
        sl = round(entry - 2.0 * atr, 2) if is_up else round(entry + 2.0 * atr, 2)
        t1 = round(entry + 2.0 * atr, 2) if is_up else round(entry - 2.0 * atr, 2)
        
        risk = abs(entry - sl)
        rr = round((abs(t1 - entry) / risk), 2) if risk > 0 else 0.0
        
        return {
            "Stock": stock,
            "LTP": entry,
            "Type": "BIG UP" if is_up else "BIG DOWN",
            "Entry": entry,
            "SL": sl,
            "T1": t1,
            "RR": rr,
            "Body%": round(body_pct, 2),
            "RVOL": rvol,
            "Confidence": round(confidence, 1),
            "RSI": round(rsi_val, 1),
            "MACD": "Bull" if macd_bullish else "Bear",
        }, None
        
    except Exception as e:
        return None, f"{symbol}: {type(e).__name__}"

# ════════════════════════════════════════════════════════════════════════════════
# F&O OI SCANNER
# ════════════════════════════════════════════════════════════════════════════════
def get_expiries():
    """Get F&O expiry dates"""
    today = datetime.now()
    expiries = []
    
    for month_offset in range(4):
        month = (today.month + month_offset - 1) % 12 + 1
        year = today.year + (today.month + month_offset - 1) // 12
        
        for day in range(30, 20, -1):
            try:
                test_date = datetime(year, month, day)
                if test_date.weekday() == 3:  # Thursday
                    expiry_str = test_date.strftime("%d%b%y").upper()
                    if test_date > today:
                        expiries.append(expiry_str)
                    break
            except ValueError:
                continue
    
    return sorted(set(expiries))[:4]

def scan_oi(fyers, symbol, expiry):
    """Scan F&O OI"""
    stock = symbol.replace("NSE:", "").replace("-EQ", "")
    
    fo_call = f"NSE:{stock}{expiry}CE"
    fo_put = f"NSE:{stock}{expiry}PE"
    
    call_resp, _ = safe_history(fyers, {
        "symbol": fo_call,
        "resolution": "D",
        "date_format": "1",
        "range_from": (datetime.today() - timedelta(days=10)).strftime("%Y-%m-%d"),
        "range_to": datetime.today().strftime("%Y-%m-%d"),
        "cont_flag": "1",
    })
    
    put_resp, _ = safe_history(fyers, {
        "symbol": fo_put,
        "resolution": "D",
        "date_format": "1",
        "range_from": (datetime.today() - timedelta(days=10)).strftime("%Y-%m-%d"),
        "range_to": datetime.today().strftime("%Y-%m-%d"),
        "cont_flag": "1",
    })
    
    if not call_resp or not put_resp:
        return None, None
    
    call_candles = call_resp.get("candles", [])
    put_candles = put_resp.get("candles", [])
    
    if not call_candles or not put_candles:
        return None, None
    
    try:
        call_latest = call_candles[-1]
        put_latest = put_candles[-1]
        
        call_oi = float(call_latest[5]) if len(call_latest) > 5 else 0
        put_oi = float(put_latest[5]) if len(put_latest) > 5 else 0
        
        if call_oi < FO_OI_MIN_OI_VALUE or put_oi < FO_OI_MIN_OI_VALUE:
            return None, None
        
        pcr = round(put_oi / call_oi, 2) if call_oi > 0 else 0.0
        
        call_oi_5d = float(call_candles[0][5]) if len(call_candles) > 0 else call_oi
        put_oi_5d = float(put_candles[0][5]) if len(put_candles) > 0 else put_oi
        
        call_change = round(((call_oi - call_oi_5d) / call_oi_5d * 100), 1) if call_oi_5d > 0 else 0
        put_change = round(((put_oi - put_oi_5d) / put_oi_5d * 100), 1) if put_oi_5d > 0 else 0
        
        # Determine signal
        if pcr > FO_OI_PCR_THRESHOLD_HIGH:
            signal = "BEARISH"
            conf = min(85.0, 55 + (pcr - FO_OI_PCR_THRESHOLD_HIGH) * 10)
        elif pcr < FO_OI_PCR_THRESHOLD_LOW:
            signal = "BULLISH"
            conf = min(85.0, 55 + (FO_OI_PCR_THRESHOLD_LOW - pcr) * 10)
        else:
            signal = "NEUTRAL"
            conf = 50.0
        
        buildup = "NONE"
        if abs(call_change) > FO_OI_OI_CHANGE_THRESHOLD:
            buildup = "CALL+" if call_change > 0 else "CALL-"
            conf = min(95.0, conf + 15)
        elif abs(put_change) > FO_OI_OI_CHANGE_THRESHOLD:
            buildup = "PUT+" if put_change > 0 else "PUT-"
            conf = min(95.0, conf + 15)
        
        total_oi = call_oi + put_oi
        
        return {
            "Stock": stock,
            "Expiry": expiry,
            "Signal": signal,
            "PCR": pcr,
            "Call OI (M)": round(call_oi / 1e6, 1),
            "Put OI (M)": round(put_oi / 1e6, 1),
            "Total OI (M)": round(total_oi / 1e6, 1),
            "Call %": round(call_oi / total_oi * 100, 1),
            "Put %": round(put_oi / total_oi * 100, 1),
            "Call Chg%": call_change,
            "Put Chg%": put_change,
            "Buildup": buildup,
            "Confidence": round(conf, 1),
        }, None
        
    except Exception as e:
        return None, f"{symbol}: {type(e).__name__}"

# ════════════════════════════════════════════════════════════════════════════════
# THREADED SCANNING
# ════════════════════════════════════════════════════════════════════════════════
def run_scan(fyers, symbols, scan_type="reversal", expiry=None):
    """Run threaded scan"""
    results = []
    errors = []
    scanned = 0
    
    print_section(f"Scanning {len(symbols)} symbols ({scan_type})...")
    
    if scan_type == "reversal":
        scan_func = scan_reversal
    elif scan_type == "bigmove":
        scan_func = scan_bigmove
    else:  # oi
        scan_func = lambda fy, s: scan_oi(fy, s, expiry)
    
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(scan_func, fyers, s): s for s in batch}
            
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                    if res:
                        results.append(res)
                    if err:
                        errors.append(err)
                except Exception as e:
                    errors.append(f"{futures[future]}: {str(e)}")
                
                scanned += 1
                pct = (scanned / len(symbols)) * 100
                print(f"  [{pct:5.1f}%] {scanned}/{len(symbols)}", end="\r")
        
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    
    print(f"  [100.0%] {scanned}/{len(symbols)} ✓")
    return results, errors

# ════════════════════════════════════════════════════════════════════════════════
# EXPORT & DISPLAY
# ════════════════════════════════════════════════════════════════════════════════
def export_results(results, filename):
    """Export results to CSV"""
    if not results:
        return
    
    df = pd.DataFrame(results)
    
    # Sort by confidence
    if "Confidence" in df.columns:
        df = df.sort_values("Confidence", ascending=False)
    
    df.to_csv(filename, index=False)
    print(f"✓ Saved to {filename}")
    
    return df

def display_results(results, scan_type):
    """Display results in table format"""
    if not results:
        print("  No signals found")
        return
    
    df = pd.DataFrame(results)
    
    # Sort by confidence
    if "Confidence" in df.columns:
        df = df.sort_values("Confidence", ascending=False)
    
    print(f"\n{len(df)} signals found:\n")
    print(df.to_string(index=False))

# ════════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════════
def main():
    print_header("NSE AI PRO V14 - Standalone Scanner")
    print(f"Start time: {now_ist().strftime('%d-%b-%Y %H:%M:%S IST')}\n")
    
    # Check token
    if not FYERS_ACCESS_TOKEN:
        print("❌ Error: FYERS_ACCESS_TOKEN not set")
        print("   Run: export FYERS_ACCESS_TOKEN='your_token'")
        return
    
    print(f"✓ Token: {FYERS_ACCESS_TOKEN[:20]}...\n")
    
    # Initialize Fyers
    try:
        fyers = FyersAPI(FYERS_ACCESS_TOKEN)
    except Exception as e:
        print(f"❌ Error initializing Fyers: {e}")
        return
    
    # Load symbols
    symbols = load_symbols(limit=200)
    if not symbols:
        print("❌ Failed to load symbols")
        return
    
    # Run scans
    print()
    
    # 1. 15-Min Reversal
    print_section("1️⃣  15-MINUTE REVERSAL SCANNER")
    rev_results, rev_errors = run_scan(fyers, symbols, "reversal")
    print(f"   Signals: {len(rev_results)} | Errors: {len(rev_errors)}")
    display_results(rev_results, "reversal")
    rev_df = export_results(rev_results, "nse_reversal_15min.csv")
    
    # 2. Volume BigMove
    print_section("2️⃣  VOLUME BIG MOVEMENT SCANNER")
    vol_results, vol_errors = run_scan(fyers, symbols, "bigmove")
    print(f"   Signals: {len(vol_results)} | Errors: {len(vol_errors)}")
    display_results(vol_results, "bigmove")
    vol_df = export_results(vol_results, "nse_volume_bigmove.csv")
    
    # 3. F&O OI Analysis
    print_section("3️⃣  F&O OI ANALYSIS")
    expiries = get_expiries()
    if expiries:
        print(f"   Available expiries: {', '.join(expiries)}")
        expiry = expiries[0]
        print(f"   Using: {expiry}\n")
        oi_results, oi_errors = run_scan(fyers, symbols[:100], "oi", expiry=expiry)
        print(f"   Signals: {len(oi_results)} | Errors: {len(oi_errors)}")
        display_results(oi_results, "oi")
        oi_df = export_results(oi_results, "nse_oi_analysis.csv")
    else:
        print("   ❌ No expiries available")
    
    # Summary
    print_header("SCAN COMPLETE")
    print(f"End time: {now_ist().strftime('%d-%b-%Y %H:%M:%S IST')}\n")
    print("📁 Files saved:")
    print("   • nse_reversal_15min.csv")
    print("   • nse_volume_bigmove.csv")
    print("   • nse_oi_analysis.csv")
    print()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
    except Exception as e:
        print(f"\n\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
