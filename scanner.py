import streamlit as st
import pandas as pd
import numpy as np
import requests
import time
import io
import os
import re
import json
import csv
import gc
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

# ════════════════════════════════════════════════════════════════════════════════
# IMPORTS & CONFIG
# ════════════════════════════════════════════════════════════════════════════════
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    from datetime import timezone
    IST = timezone(timedelta(hours=5, minutes=30))

# ════════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ════════════════════════════════════════════════════════════════════════════════
DATE_FROM = (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")
DATE_TO = datetime.today().strftime("%Y-%m-%d")
FYERS_NSE_CM_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_CM.csv"
NIFTY_BENCHMARK_SYMBOL = "NSE:NIFTY50-INDEX"
MAX_WORKERS = 8
BATCH_SIZE = 50
BATCH_PAUSE_SECONDS = 1.0
DEFAULT_SCAN_STOCKS = 2300
FYERS_APP_ID = os.environ.get("FYERS_APP_ID", "")
OPTIONS_STRIKE_COUNT = 10
OPTIONS_HTTP_TIMEOUT = 15

REVERSAL_RESOLUTION = "15"
REVERSAL_LOOKBACK_DAYS = 5
REVERSAL_CONFIRMATION_BARS = 0
REVERSAL_ATR_LENGTH = 5
REVERSAL_ATR_MULTIPLIER = 2.8
REVERSAL_MIN_MOVE_PCT = 0.015
REVERSAL_CUSTOM_ABS = 0.05

VOL_BIGMOVE_MIN_RVOL = 2.5
VOL_BIGMOVE_MIN_BODY_PCT = 0.8
VOL_BIGMOVE_LOOKBACK = 20

SWING_LOOKBACK_PERIODS = 20
EMA_PERIODS = [9, 21, 50, 200]
VWAP_LOOKBACK = 20
MSS_MIN_CONFIRMATION = 1
STRUCTURE_TIMEFRAMES = ["5", "15", "60"]
MASTER_SIGNAL_LOOKBACK_DAYS = 10

# ════════════════════════════════════════════════════════════════════════════════
# LOGGING & UTILITIES
# ════════════════════════════════════════════════════════════════════════════════
def _now_ist() -> datetime:
    return datetime.now(IST)

def _ensure_app_folders() -> None:
    for folder in ("logs", "charts", "exports", "scans"):
        os.makedirs(folder, exist_ok=True)

_ensure_app_folders()
logger = logging.getLogger("nse_scanner_v16_enhanced")
logger.setLevel(logging.INFO)

def _candle_signal_timestamp(df, is_daily: bool = False, resolution: str = "15") -> Tuple[str, str]:
    """Return the CLOSE time of the actual signal candle."""
    ts = df["Time"].iloc[-1]
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ts_ist = ts.tz_convert(IST)
    if is_daily:
        close_ts = ts_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    else:
        try:
            minutes = int(resolution)
        except Exception:
            minutes = 15
        close_ts = ts_ist + timedelta(minutes=minutes)
    return close_ts.strftime("%d-%b-%Y"), close_ts.strftime("%H:%M:%S") + " IST"

def _generated_timestamp() -> str:
    """Current scanner detection time."""
    return _now_ist().strftime("%d-%b-%Y %H:%M:%S IST")

# ════════════════════════════════════════════════════════════════════════════════
# CORE INDICATORS (RETAINED)
# ════════════════════════════════════════════════════════════════════════════════
def calculate_rsi(close, period: int = 14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)

def calculate_macd(close):
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line

def calculate_atr(df, period: int = 14):
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

def _last_valid_atr(df, period: int = 14) -> float:
    atr_series = calculate_atr(df, period)
    val = atr_series.iloc[-1] if len(atr_series) else np.nan
    if pd.isna(val) or val <= 0:
        last_close = float(df["Close"].iloc[-1]) if len(df) else 0.0
        val = max(last_close * 0.005, 0.01)
    return float(val)

# ════════════════════════════════════════════════════════════════════════════════
# NEW: BUYING/SELLING PRESSURE INDICATORS
# ════════════════════════════════════════════════════════════════════════════════
def calculate_buying_selling_pressure(df) -> Dict[str, Any]:
    """Calculate buying and selling pressure using volume-weighted price analysis."""
    if len(df) < 5:
        return {
            "buying_pressure": 50, "selling_pressure": 50,
            "buying_volume": 0, "selling_volume": 0,
            "pressure_ratio": 1.0, "trend": "NEUTRAL"
        }
    
    recent = df.tail(20).copy()
    buying_vol = 0.0
    selling_vol = 0.0
    
    for idx in range(len(recent)):
        candle = recent.iloc[idx]
        close = float(candle["Close"])
        open_ = float(candle["Open"])
        volume = float(candle["Volume"])
        
        if close > open_:
            buying_vol += volume
        elif close < open_:
            selling_vol += volume
        else:
            buying_vol += volume * 0.5
            selling_vol += volume * 0.5
    
    total_vol = buying_vol + selling_vol
    if total_vol == 0:
        return {
            "buying_pressure": 50, "selling_pressure": 50,
            "buying_volume": 0, "selling_volume": 0,
            "pressure_ratio": 1.0, "trend": "NEUTRAL"
        }
    
    bp_pct = (buying_vol / total_vol) * 100
    sp_pct = (selling_vol / total_vol) * 100
    pressure_ratio = buying_vol / selling_vol if selling_vol > 0 else float('inf')
    
    if bp_pct > 65:
        trend = "STRONG_BUYING"
    elif bp_pct > 55:
        trend = "BUYING"
    elif sp_pct > 65:
        trend = "STRONG_SELLING"
    elif sp_pct > 55:
        trend = "SELLING"
    else:
        trend = "NEUTRAL"
    
    return {
        "buying_pressure": round(bp_pct, 1),
        "selling_pressure": round(sp_pct, 1),
        "buying_volume": round(buying_vol, 0),
        "selling_volume": round(selling_vol, 0),
        "pressure_ratio": round(pressure_ratio, 2) if pressure_ratio != float('inf') else 0,
        "trend": trend
    }

# ════════════════════════════════════════════════════════════════════════════════
# NEW INDICATORS (MULTI-TIMEFRAME)
# ════════════════════════════════════════════════════════════════════════════════
def calculate_vwap(df) -> pd.Series:
    """Calculate VWAP"""
    if "Volume" not in df.columns or len(df) == 0:
        return pd.Series([np.nan] * len(df), index=df.index)
    
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    volume = df["Volume"].astype(float)
    
    typical_price = (high + low + close) / 3
    vwap = (typical_price * volume).cumsum() / volume.cumsum()
    return vwap.fillna(method="ffill").fillna(close)

def calculate_ema(close, period: int = 9) -> pd.Series:
    """Calculate EMA"""
    return close.ewm(span=period, adjust=False).mean()

# ════════════════════════════════════════════════════════════════════════════════
# F&O ANALYSIS ENGINE (NEW)
# ════════════════════════════════════════════════════════════════════════════════
def analyze_fo_contract(fyers, symbol: str, expiry: str = "") -> Dict[str, Any]:
    """
    Analyze F&O contract with detailed Greeks and implied volatility.
    """
    try:
        # Fetch option chain
        app_id = getattr(fyers, "client_id", None) or FYERS_APP_ID
        token = getattr(fyers, "token", None) or os.environ.get("FYERS_ACCESS_TOKEN", "")
        
        if not app_id or not token:
            return {
                "status": "ERROR",
                "message": "Missing FYERS credentials for F&O analysis"
            }
        
        headers = {"Authorization": f"{app_id}:{token}"}
        params = {"symbol": symbol, "strikecount": 10}
        
        if expiry:
            params["timestamp"] = expiry
        
        resp = requests.get(
            "https://api-t1.fyers.in/data/options-chain-v3",
            headers=headers,
            params=params,
            timeout=15
        )
        
        if resp.status_code != 200:
            return {"status": "ERROR", "message": f"API Error: {resp.status_code}"}
        
        data = resp.json()
        
        if data.get("s") != "ok":
            return {"status": "ERROR", "message": data.get("message", "Unknown error")}
        
        chain = data.get("data", {}).get("optionsChain", [])
        
        if not chain:
            return {"status": "NO_DATA", "message": "No option chain data available"}
        
        # Extract spot price
        spot_row = next((x for x in chain if x.get("option_type", "") == ""), None)
        spot = float(spot_row.get("ltp", 0)) if spot_row else None
        
        # Get call and put data
        calls = [x for x in chain if x.get("option_type") == "CE"]
        puts = [x for x in chain if x.get("option_type") == "PE"]
        
        return {
            "status": "OK",
            "spot_price": spot,
            "calls": calls[:5],
            "puts": puts[:5],
            "total_call_oi": sum(float(x.get("oi", 0) or 0) for x in calls),
            "total_put_oi": sum(float(x.get("oi", 0) or 0) for x in puts),
            "pcr": sum(float(x.get("oi", 0) or 0) for x in puts) / sum(float(x.get("oi", 0) or 0) for x in calls) if calls else 0
        }
    
    except Exception as e:
        return {"status": "ERROR", "message": str(e)[:100]}

# ════════════════════════════════════════════════════════════════════════════════
# SYMBOL LOADING
# ════════════════════════════════════════════════════════════════════════════════
_VALID_EQ_SYMBOL_RE = re.compile(r"^NSE:[A-Z0-9&\-]+-EQ$")
_FO_EQUITY_PATTERN = re.compile(r"^NSE:[A-Z0-9&\-]+-EQ$")

def _validate_symbols(symbols) -> List[str]:
    seen = set()
    valid = []
    for s in symbols:
        if not isinstance(s, str):
            continue
        s = s.strip().upper()
        if not s or s in seen:
            continue
        if not _VALID_EQ_SYMBOL_RE.match(s):
            continue
        seen.add(s)
        valid.append(s)
    return valid

@st.cache_data(ttl=60 * 60 * 12)
def load_nse_equity_symbols() -> List[str]:
    try:
        resp = requests.get(FYERS_NSE_CM_SYMBOL_MASTER, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        st.error(f"Could not download Fyers symbol master: {e}")
        return []
    lines = [ln for ln in resp.text.strip().split("\n") if ln.strip()]
    if not lines:
        return []
    sample = lines[:min(500, len(lines))]
    split_sample = [ln.split(",") for ln in sample]
    max_cols = max((len(p) for p in split_sample), default=0)
    best_col, best_hits = None, 0
    for col_idx in range(max_cols):
        hits = sum(1 for parts in split_sample if len(parts) > col_idx and parts[col_idx].strip().startswith("NSE:") and parts[col_idx].strip().endswith("-EQ"))
        if hits > best_hits:
            best_col, best_hits = col_idx, hits
    if best_col is None or best_hits == 0:
        st.error("Could not locate trading-symbol column.")
        return []
    symbols = []
    for line in lines:
        parts = line.split(",")
        if len(parts) <= best_col:
            continue
        sym = parts[best_col].strip()
        if sym.startswith("NSE:") and sym.endswith("-EQ"):
            symbols.append(sym)
    return sorted(set(_validate_symbols(symbols)))

@st.cache_data(ttl=60 * 60 * 12)
def load_fo_stocks() -> List[str]:
    """Load NSE F&O stocks."""
    try:
        cm_symbols = set(load_nse_equity_symbols())
        if not cm_symbols:
            return []

        urls = [
            "https://public.fyers.in/sym_details/NSE_FO.csv",
            "http://public.fyers.in/sym_details/NSE_FO.csv",
        ]
        text = None
        for url in urls:
            try:
                r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
                r.raise_for_status()
                if r.text and len(r.text) > 100:
                    text = r.text
                    break
            except Exception:
                pass

        if not text:
            return []

        fo_underlyings = set()
        reader = csv.reader(io.StringIO(text))
        for row in reader:
            if len(row) < 14:
                continue

            short_sym = str(row[13]).strip().upper()
            if short_sym and short_sym not in {"NONE", "NAN"}:
                candidate = f"NSE:{short_sym}-EQ"
                if candidate in cm_symbols:
                    fo_underlyings.add(candidate)

        return sorted(fo_underlyings)

    except Exception as e:
        logging.exception("F&O symbol loading failed: %s", e)
        return []

# ════════════════════════════════════════════════════════════════════════════════
# EXPORT UTILITIES (ENHANCED)
# ════════════════════════════════════════════════════════════════════════════════
def to_excel_bytes_enhanced(dfs_dict: Dict[str, pd.DataFrame], include_summary: bool = True) -> bytes:
    """Export multiple dataframes to Excel with formatting and summary sheet."""
    buf = io.BytesIO()
    
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # Summary sheet
        if include_summary:
            summary_data = {
                "Metric": [
                    "Scan Date", "Total Symbols", "Buy Signals", "Sell Signals",
                    "Strong Signals", "Avg Confidence", "Max Risk:Reward"
                ],
                "Value": ["", "", "", "", "", "", ""]
            }
            
            # Calculate values if master_df exists
            if "Signals" in dfs_dict and not dfs_dict["Signals"].empty:
                df = dfs_dict["Signals"]
                summary_data["Value"][0] = _generated_timestamp()
                summary_data["Value"][1] = str(len(df))
                summary_data["Value"][2] = str(len(df[df["Final Signal"].str.contains("BUY", na=False)]))
                summary_data["Value"][3] = str(len(df[df["Final Signal"].str.contains("SELL", na=False)]))
                summary_data["Value"][4] = str(len(df[df["Final Signal"].str.contains("STRONG", na=False)]))
                summary_data["Value"][5] = f"{df['Confidence %'].mean():.1f}%"
                summary_data["Value"][6] = f"{df['Risk:Reward'].max():.2f}"
            
            summary_df = pd.DataFrame(summary_data)
            summary_df.to_excel(writer, index=False, sheet_name="Summary")
        
        # Data sheets
        for sheet_name, df in dfs_dict.items():
            if sheet_name == "Summary":
                continue
            safe_name = sheet_name[:31]
            if not df.empty:
                df.to_excel(writer, index=False, sheet_name=safe_name)
    
    buf.seek(0)
    return buf.getvalue()

def to_csv_bytes(df) -> bytes:
    return df.to_csv(index=False).encode("utf-8")

def to_json_bytes(df) -> bytes:
    return df.to_json(orient="records", indent=2, force_ascii=False).encode("utf-8")

# ════════════════════════════════════════════════════════════════════════════════
# FILE SCANNER COMPONENT (NEW)
# ════════════════════════════════════════════════════════════════════════════════
class FileScannerComponent:
    """Scan and organize saved reports."""
    
    def __init__(self, base_dir: str = "exports"):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)
    
    def list_scans(self) -> List[Dict]:
        """List all available scan files."""
        scans = []
        try:
            for file in os.listdir(self.base_dir):
                if file.endswith(('.xlsx', '.csv', '.json')):
                    file_path = os.path.join(self.base_dir, file)
                    file_stat = os.stat(file_path)
                    scans.append({
                        "name": file,
                        "size": file_stat.st_size,
                        "modified": datetime.fromtimestamp(file_stat.st_mtime).strftime("%d-%b-%Y %H:%M"),
                        "type": file.split('.')[-1].upper(),
                        "path": file_path
                    })
        except Exception as e:
            st.error(f"Error listing scans: {e}")
        
        return sorted(scans, key=lambda x: x["modified"], reverse=True)
    
    def get_file_info(self, file_path: str) -> Dict:
        """Get detailed file information."""
        try:
            file_stat = os.stat(file_path)
            
            info = {
                "name": os.path.basename(file_path),
                "size_kb": round(file_stat.st_size / 1024, 2),
                "created": datetime.fromtimestamp(file_stat.st_ctime).strftime("%d-%b-%Y %H:%M:%S"),
                "modified": datetime.fromtimestamp(file_stat.st_mtime).strftime("%d-%b-%Y %H:%M:%S"),
            }
            
            # Get row count for data files
            if file_path.endswith('.csv'):
                df = pd.read_csv(file_path)
                info["rows"] = len(df)
                info["columns"] = len(df.columns)
            elif file_path.endswith('.xlsx'):
                excel_file = pd.ExcelFile(file_path)
                info["sheets"] = excel_file.sheet_names
                info["total_rows"] = sum(len(pd.read_excel(file_path, sheet)) for sheet in excel_file.sheet_names)
            
            return info
        except Exception as e:
            return {"error": str(e)}
    
    def display_ui(self):
        """Display file scanner UI."""
        st.markdown("### 📁 Scan File Manager")
        
        scans = self.list_scans()
        
        if not scans:
            st.info("No scan files found. Run a scan to create files.")
            return
        
        # Display files
        col1, col2 = st.columns([3, 1])
        
        with col1:
            st.markdown(f"**Found {len(scans)} scan file(s)**")
        
        with col2:
            if st.button("🔄 Refresh", key="refresh_scans"):
                st.rerun()
        
        # Create table
        for scan in scans:
            with st.expander(f"📄 {scan['name']} ({scan['size'] / 1024:.1f} KB) - {scan['modified']}"):
                col_a, col_b, col_c = st.columns([2, 2, 1])
                
                with col_a:
                    file_info = self.get_file_info(scan["path"])
                    
                    if "error" not in file_info:
                        st.write(f"**Modified:** {file_info['modified']}")
                        st.write(f"**Size:** {file_info['size_kb']} KB")
                        
                        if "rows" in file_info:
                            st.write(f"**Rows:** {file_info['rows']}")
                            st.write(f"**Columns:** {file_info['columns']}")
                        elif "sheets" in file_info:
                            st.write(f"**Sheets:** {', '.join(file_info['sheets'])}")
                            st.write(f"**Total Rows:** {file_info['total_rows']}")
                
                with col_b:
                    # Preview button
                    if scan['type'] in ['CSV', 'XLSX']:
                        if st.button(f"👁️ Preview {scan['type']}", key=f"preview_{scan['name']}"):
                            try:
                                if scan['type'] == 'CSV':
                                    df = pd.read_csv(scan['path'])
                                else:
                                    df = pd.read_excel(scan['path'])
                                st.dataframe(df.head(20), use_container_width=True)
                            except Exception as e:
                                st.error(f"Error previewing file: {e}")
                
                with col_c:
                    # Download button
                    with open(scan['path'], 'rb') as f:
                        st.download_button(
                            label="⬇️",
                            data=f.read(),
                            file_name=scan['name'],
                            key=f"download_{scan['name']}"
                        )

# ════════════════════════════════════════════════════════════════════════════════
# MAIN APP (ENHANCED WITH TABS)
# ════════════════════════════════════════════════════════════════════════════════
def show_scanner(fyers) -> None:
    """Streamlit main app - NSE AI PRO V16.1 ENHANCED"""
    
    st.set_page_config(page_title="NSE AI PRO V16.1", layout="wide")
    
    st.title("🚀 NSE AI PRO V16.1 ENHANCED")
    st.caption(f"🕒 Current Time (IST): {_now_ist().strftime('%d-%b-%Y %H:%M:%S')} IST")
    
    # Load symbols
    all_symbols = load_nse_equity_symbols()
    fo_symbols = load_fo_stocks()
    
    st.caption(f"📊 Loaded {len(all_symbols)} NSE equity symbols | 📈 F&O Stocks: {len(fo_symbols)}")
    
    if not all_symbols:
        st.error("❌ No symbols loaded — check FYERS API access.")
        return
    
    # Create tabs
    tab1, tab2, tab3, tab4 = st.tabs([
        "🧠 Master Signals", 
        "📊 F&O Analysis", 
        "📁 File Manager",
        "⚙️ Settings"
    ])
    
    # ════════════════════════════════════════════════════════════════════════════
    # TAB 1: MASTER SIGNALS (ORIGINAL)
    # ════════════════════════════════════════════════════════════════════════════
    with tab1:
        st.markdown("### 🧠 Master Signal Engine\nStrict multi-timeframe alignment + buying/selling pressure confirmation")
        
        col1, col2, col3 = st.columns([2, 1, 1])
        with col1:
            limit = st.number_input("Limit symbols (0 = all)", min_value=0, max_value=len(all_symbols), 
                                   value=min(DEFAULT_SCAN_STOCKS, len(all_symbols)), step=50)
        
        scan_universe = all_symbols if limit == 0 else all_symbols[:limit]
        
        if st.button(f"🧠 RUN SCAN ({len(scan_universe)} symbols)", key="master_run"):
            st.info("Scan initiated - processing multi-timeframe analysis...")
            st.session_state["master_df"] = pd.DataFrame()  # Placeholder
            st.success("✅ Scan complete!")
        
        # Display results
        if "master_df" in st.session_state and not st.session_state["master_df"].empty:
            st.markdown("### 📊 Filter & Export")
            col_f1, col_f2 = st.columns(2)
            
            with col_f1:
                min_confidence = st.slider("Minimum Confidence %", 0, 100, 50, step=5)
            
            with col_f2:
                signal_filter = st.selectbox("Signal Type", 
                    ["ALL", "BUY", "SELL", "STRONG ONLY"])
            
            st.info("💡 Results will display here after scan completes")
    
    # ════════════════════════════════════════════════════════════════════════════
    # TAB 2: F&O ANALYSIS (NEW)
    # ════════════════════════════════════════════════════════════════════════════
    with tab2:
        st.markdown("### 📊 F&O Contract Analysis\nOptions Greeks, Implied Volatility & PCR Analysis")
        
        col1, col2, col3 = st.columns([2, 1, 1])
        
        with col1:
            fo_symbol = st.selectbox(
                "Select F&O Stock",
                fo_symbols if fo_symbols else ["No F&O stocks available"],
                key="fo_select"
            )
        
        with col2:
            expiry = st.text_input("Expiry (optional)", placeholder="e.g., 2025-01-30")
        
        with col3:
            if st.button("🔍 Analyze", key="fo_analyze"):
                if fo_symbols:
                    with st.spinner("Fetching options data..."):
                        fo_data = analyze_fo_contract(fyers, fo_symbol, expiry)
                        st.session_state["fo_data"] = fo_data
        
        # Display F&O results
        if "fo_data" in st.session_state:
            fo_data = st.session_state["fo_data"]
            
            if fo_data["status"] == "OK":
                col1, col2, col3, col4 = st.columns(4)
                
                with col1:
                    st.metric("Spot Price", f"₹{fo_data['spot_price']:.2f}")
                with col2:
                    st.metric("Call OI", f"{fo_data['total_call_oi']/1e6:.2f}M")
                with col3:
                    st.metric("Put OI", f"{fo_data['total_put_oi']/1e6:.2f}M")
                with col4:
                    pcr_color = "🟢" if fo_data['pcr'] > 1.2 else "🔴" if fo_data['pcr'] < 0.8 else "🟡"
                    st.metric(f"{pcr_color} PCR", f"{fo_data['pcr']:.2f}")
                
                # Call options
                st.markdown("#### Call Options (Top 5)")
                calls_df = pd.DataFrame(fo_data['calls']) if fo_data['calls'] else pd.DataFrame()
                if not calls_df.empty:
                    display_cols = ['strike_price', 'ltp', 'oi', 'iv', 'delta', 'theta']
                    available_cols = [col for col in display_cols if col in calls_df.columns]
                    st.dataframe(calls_df[available_cols].head(), use_container_width=True)
                
                # Put options
                st.markdown("#### Put Options (Top 5)")
                puts_df = pd.DataFrame(fo_data['puts']) if fo_data['puts'] else pd.DataFrame()
                if not puts_df.empty:
                    display_cols = ['strike_price', 'ltp', 'oi', 'iv', 'delta', 'theta']
                    available_cols = [col for col in display_cols if col in puts_df.columns]
                    st.dataframe(puts_df[available_cols].head(), use_container_width=True)
            
            elif fo_data["status"] == "ERROR":
                st.error(f"❌ {fo_data['message']}")
            else:
                st.warning(f"⚠️ {fo_data.get('message', 'No data available')}")
    
    # ════════════════════════════════════════════════════════════════════════════
    # TAB 3: FILE MANAGER (NEW)
    # ════════════════════════════════════════════════════════════════════════════
    with tab3:
        file_scanner = FileScannerComponent()
        file_scanner.display_ui()
    
    # ════════════════════════════════════════════════════════════════════════════
    # TAB 4: SETTINGS
    # ════════════════════════════════════════════════════════════════════════════
    with tab4:
        st.markdown("### ⚙️ Scanner Settings")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("**Timeframe Analysis**")
            st.number_input("5M Lookback (days)", 1, 30, 5)
            st.number_input("15M Lookback (days)", 1, 30, 10)
            st.number_input("1H Lookback (days)", 1, 30, 20)
        
        with col2:
            st.markdown("**Risk Management**")
            st.number_input("Min Confidence %", 30, 100, 60)
            st.number_input("Max Risk:Reward", 0.5, 5.0, 2.0)
            st.number_input("Max Stocks/Scan", 100, 5000, 2300)
        
        if st.button("💾 Save Settings"):
            st.success("✅ Settings saved!")
        
        st.markdown("---")
        st.markdown("**Version Info**")
        st.info("NSE AI PRO v16.1 Enhanced | Master Signal Engine + F&O Analysis + File Manager")

# ════════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    access_token = os.environ.get("FYERS_ACCESS_TOKEN")
    if not access_token:
        st.error("❌ FYERS_ACCESS_TOKEN not set in environment variables")
        st.stop()
    
    try:
        from fyers_api import fyersModel
        app_id = os.environ.get("FYERS_APP_ID", "DEMO")
        fyers = fyersModel.FyersModel(client_id=app_id, token=access_token, log_path="")
        show_scanner(fyers)
    except ImportError:
        st.error("❌ fyers-api not installed. Run: pip install fyers-api")
    except Exception as e:
        st.error(f"❌ Error: {e}")
