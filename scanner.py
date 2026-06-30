import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import io
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from xgboost import XGBClassifier
import warnings

warnings.filterwarnings('ignore')

# ==========================================
# 1. PAGE SETUP & CONFIGURATION
# ==========================================
st.set_page_config(page_title="NSE AI PRO V11.11", layout="wide", page_icon="🚀")

st.markdown("""
    <style>
    .stApp { background-color: #f8f9fa; }
    .css-1r6slp0 { padding: 2rem; }
    .stSidebar { background-color: #ffffff; }
    </style>
    """, unsafe_allow_html=True)

st.title("🚀 NSE AI PRO V11.11 - Institutional Ultimate")
st.markdown("**News Momentum | Gap Tracking | VWAP Bounce | RVOL System | AI Visuals**")
st.markdown("---")

if 'v11_master_data' not in st.session_state:
    st.session_state.v11_master_data = pd.DataFrame()
if 'v11_last_run' not in st.session_state:
    st.session_state.v11_last_run = None
if 'v11_errors' not in st.session_state:
    st.session_state.v11_errors = []

RESULT_COLUMNS = [
    "Signal Time", "Symbol", "Close", "Gap %", "Target", "Stoploss", "SMC Structure",
    "CISD Signal", "XGB Prediction", "XGB Confidence", "Alerts", "MTF Status",
    "AI Trend", "AI Confidence", "Relative Strength", "Support 1", "Resistance 1",
    "52W High", "52W Low", "52W Status", "RSI", "Breakout", "MACD", "Supertrend",
    "VWAP", "Pattern", "RVOL", "Score", "Final Signal",
]

# ==========================================
# 2. SIDEBAR CONFIGURATION
# ==========================================
with st.sidebar:
    st.header("⚙️ Settings & Controls")
    auto_refresh = st.checkbox("🔄 Auto Refresh (Every 3 Mins)")
    interval = st.selectbox("Interval", ["5m", "15m", "30m", "1h", "1d"], index=1)
    period = st.selectbox("Period", ["5d", "1mo", "3mo", "6mo", "1y"], index=2)

    sector_stocks = {
        "Banking": ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK"],
        "IT": ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM"],
        "Pharma": ["SUNPHARMA", "CIPLA", "DIVISLAB", "DRREDDY"],
        "Energy": ["RELIANCE", "ONGC", "BPCL", "NTPC"],
        "Auto": ["TATAMOTORS", "M&M", "EICHERMOT", "HEROMOTOCO"],
        "FMCG": ["ITC", "HINDUNILVR", "BRITANNIA", "DABUR"],
    }
    sector = st.selectbox("Sector", ["All NSE500"] + list(sector_stocks.keys()))

    if sector == "All NSE500":
        scan_limit = st.number_input(
            "Limit symbols (0 = all ~500)", min_value=0, max_value=500, value=100, step=50,
            help="Scanning all 500 with yfinance is slow and prone to rate limiting. Start small."
        )
    else:
        scan_limit = 0

    st.markdown("---")
    run_button = st.button("🚀 RUN ULTIMATE SCANNER", type="primary", use_container_width=True)

# ==========================================
# 3. CORE MATHEMATICS & AI ENGINE
# ==========================================
@st.cache_data(ttl=86400)
def load_nse500():
    try:
        import requests
        url = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5)
        df = pd.read_csv(io.StringIO(response.text))
        return sorted(df["Symbol"].dropna().unique().tolist())
    except Exception:
        return ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "ITC", "LT", "AXISBANK", "TATAMOTORS"]

stocks = load_nse500()

@st.cache_data(ttl=120)
def get_data(symbol, interval, period):
    try:
        df = yf.download(f"{symbol}.NS" if "^" not in symbol else symbol,
                          interval=interval, period=period, auto_adjust=True, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=3600)
def get_bulk_daily_data(stocks_list):
    try:
        return yf.download([f"{s}.NS" for s in stocks_list], period="1y", interval="1d",
                            progress=False, auto_adjust=True)
    except Exception:
        return pd.DataFrame()

def predict_trend_ai(prices):
    if len(prices) < 20:
        return "Neutral", 0
    y = prices[-20:].values
    x = np.arange(len(y))
    slope, intercept = np.polyfit(x, y, 1)
    correlation = np.corrcoef(x, y)[0, 1]
    confidence = min(round(abs(correlation) * 100, 2), 99)
    if slope > 0 and confidence > 50:
        return "UP 🚀", confidence
    elif slope < 0 and confidence > 50:
        return "DOWN 🔻", confidence
    else:
        return "SIDEWAYS ➖", confidence

def calculate_smc_and_cisd(df):
    if len(df) < 30:
        return "Range ➖", "None", "Normal", "N/A"
    try:
        df = df.copy()
        df['Prev_High'] = df['High'].shift(1)
        df['Prev_Low'] = df['Low'].shift(1)
        df['Bullish_CISD'] = (df['Low'] < df['Prev_Low']) & (df['Close'] > df['Prev_High'])
        df['Bearish_CISD'] = (df['High'] > df['Prev_High']) & (df['Close'] < df['Prev_Low'])

        df['Local_High'] = df['High'].rolling(window=10).max().shift(1)
        df['Local_Low'] = df['Low'].rolling(window=10).min().shift(1)
        df['EMA20'] = df['Close'].ewm(span=20).mean()
        df['EMA50'] = df['Close'].ewm(span=50).mean()
        df['Bullish_Trend'] = df['EMA20'] > df['EMA50']

        df['Break_Up'] = df['Close'] > df['Local_High']
        df['Break_Down'] = df['Close'] < df['Local_Low']

        recent_df = df.tail(20)
        cisd_events = recent_df[recent_df['Bullish_CISD'] | recent_df['Bearish_CISD']]
        cisd_signal = "None"
        cisd_time_str = "N/A"

        if not cisd_events.empty:
            last_cisd_idx = cisd_events.index[-1]
            is_bull = cisd_events['Bullish_CISD'].iloc[-1]
            cisd_signal = "Bullish CISD 🚀" if is_bull else "Bearish CISD 🩸"
            cisd_time_str = last_cisd_idx.strftime("%d-%b %I:%M %p")

        smc_events = recent_df[recent_df['Break_Up'] | recent_df['Break_Down']]
        smc_structure = "Range ➖"
        smc_time_str = "N/A"
        smc_alert = "Normal"

        if not smc_events.empty:
            last_smc_idx = smc_events.index[-1]
            is_up = smc_events['Break_Up'].iloc[-1]
            is_bull_trend = smc_events['Bullish_Trend'].iloc[-1]

            if is_up:
                smc_structure = "BOS 📈" if is_bull_trend else "CHOCH 🐂"
                smc_alert = "Structure Broken Upward"
            else:
                smc_structure = "BOS 📉" if not is_bull_trend else "CHOCH 🐻"
                smc_alert = "Trend Reversal Bearish"
            smc_time_str = last_smc_idx.strftime("%d-%b %I:%M %p")

        final_time = "N/A"
        if cisd_signal != "None":
            final_time = cisd_time_str
        elif smc_structure != "Range ➖":
            final_time = smc_time_str

        return smc_structure, cisd_signal, smc_alert, final_time
    except Exception:
        return "Range ➖", "None", "Normal", "N/A"

def train_xgboost_predictor(df):
    if len(df) < 50:
        return "Neutral", 0.0
    try:
        df_ml = df.copy()
        try:
            df_ml['Hour'] = df_ml.index.hour
            df_ml['Minute'] = df_ml.index.minute
        except Exception:
            df_ml['Hour'] = 0
            df_ml['Minute'] = 0

        df_ml['Close'] = pd.to_numeric(df_ml['Close'], errors='coerce')
        df_ml['Volume'] = pd.to_numeric(df_ml['Volume'], errors='coerce')
        df_ml['RSI'] = pd.to_numeric(df_ml['RSI'], errors='coerce')
        df_ml['AVG_VOL'] = pd.to_numeric(df_ml['AVG_VOL'], errors='coerce')
        df_ml['EMA20'] = pd.to_numeric(df_ml['EMA20'], errors='coerce')
        df_ml['EMA50'] = pd.to_numeric(df_ml['EMA50'], errors='coerce')

        df_ml['Return'] = df_ml['Close'].pct_change()
        df_ml['RSI_Norm'] = df_ml['RSI'] / 100.0

        df_ml['Vol_Ratio'] = np.where(df_ml['AVG_VOL'] > 0, df_ml['Volume'] / df_ml['AVG_VOL'], 1.0)
        df_ml['EMA_Gap'] = np.where(df_ml['EMA50'] > 0, (df_ml['EMA20'] - df_ml['EMA50']) / df_ml['EMA50'], 0.0)

        df_ml['Target_Direction'] = np.where(df_ml['Close'].shift(-1) > df_ml['Close'], 1, 0)

        df_ml.replace([np.inf, -np.inf], np.nan, inplace=True)
        df_ml.dropna(subset=['Return', 'RSI_Norm', 'Vol_Ratio', 'EMA_Gap', 'Hour', 'Minute', 'Target_Direction'], inplace=True)

        if len(df_ml) < 30:
            return "Neutral", 0.0

        feature_cols = ['Return', 'RSI_Norm', 'Vol_Ratio', 'EMA_Gap', 'Hour', 'Minute']
        X = df_ml[feature_cols].values.astype('float32')
        y = df_ml['Target_Direction'].values.astype('int32')

        if len(np.unique(y[:-1])) < 2:
            return "SIDEWAYS ➖", 50.0

        model = XGBClassifier(n_estimators=25, max_depth=4, learning_rate=0.05,
                               eval_metric='logloss', random_state=42, n_jobs=1)
        model.fit(X[:-1], y[:-1])

        latest_vector = X[-1].reshape(1, -1)
        prediction = int(model.predict(latest_vector)[0])
        probabilities = model.predict_proba(latest_vector)[0]
        confidence = round(float(probabilities[prediction]) * 100, 2)

        return "BULLISH 🚀" if prediction == 1 else "BEARISH 🔻", confidence
    except Exception as e:
        return f"Err: {str(e)[:8]}", 0.0

def calculate_supertrend(df, period=10, multiplier=3):
    high, low, close = df['High'], df['Low'], df['Close']
    tr = np.maximum(high - low, np.maximum(abs(high - close.shift(1)), abs(low - close.shift(1))))
    atr = tr.rolling(window=period).mean()
    hl2 = (high + low) / 2
    upperband, lowerband = hl2 + (multiplier * atr), hl2 - (multiplier * atr)
    supertrend, direction = np.zeros(len(df)), np.zeros(len(df))

    for i in range(1, len(df)):
        if close.iloc[i] > upperband.iloc[i - 1]:
            direction[i] = 1
        elif close.iloc[i] < lowerband.iloc[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]

        if direction[i] == 1:
            lowerband.iloc[i] = max(lowerband.iloc[i], lowerband.iloc[i - 1])
            supertrend[i] = lowerband.iloc[i]
        else:
            upperband.iloc[i] = min(upperband.iloc[i], upperband.iloc[i - 1])
            supertrend[i] = upperband.iloc[i]
    df['Supertrend'], df['ST_Direction'] = supertrend, direction
    return df

def get_candlestick_pattern(df):
    if len(df) < 2:
        return "None"
    O1, C1 = df['Open'].iloc[-2], df['Close'].iloc[-2]
    O2, C2, H2, L2 = df['Open'].iloc[-1], df['Close'].iloc[-1], df['High'].iloc[-1], df['Low'].iloc[-1]
    body = abs(C2 - O2)
    rng = H2 - L2 if (H2 - L2) > 0 else 0.001
    if body <= (rng * 0.1):
        return "Doji"
    if C1 < O1 and C2 > O2 and O2 < C1 and C2 > O1:
        return "Bullish Engulfing"
    if C1 > O1 and C2 < O2 and O2 > C1 and C2 < O1:
        return "Bearish Engulfing"
    lower_shadow, upper_shadow = min(O2, C2) - L2, H2 - max(O2, C2)
    if lower_shadow > 2 * body and upper_shadow < 0.2 * body:
        return "Hammer"
    return "Normal"

def add_indicators(df, interval):
    if len(df) < 60:
        return df
    df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
    df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()
    delta = df["Close"].diff()
    df["RSI"] = 100 - (100 / (1 + (delta.clip(lower=0).ewm(com=13, adjust=False).mean() /
                                     -delta.clip(upper=0).ewm(com=13, adjust=False).mean())))
    df["MACD_Line"] = df["Close"].ewm(span=12, adjust=False).mean() - df["Close"].ewm(span=26, adjust=False).mean()
    df["Signal_Line"] = df["MACD_Line"].ewm(span=9, adjust=False).mean()

    tp = (df['High'] + df['Low'] + df['Close']) / 3
    if 'd' not in interval and 'wk' not in interval and 'mo' not in interval:
        df['Date'] = df.index.date
        df['VWAP'] = (df['Volume'] * tp).groupby(df['Date']).cumsum() / df['Volume'].groupby(df['Date']).cumsum()
    else:
        df['VWAP'] = (df['Volume'] * tp).rolling(20).sum() / df['Volume'].rolling(20).sum()

    df = calculate_supertrend(df)
    df['Pivot'] = (df['High'].shift(1) + df['Low'].shift(1) + df['Close'].shift(1)) / 3
    df['Resistance_1'] = (2 * df['Pivot']) - df['Low'].shift(1)
    df['Support_1'] = (2 * df['Pivot']) - df['High'].shift(1)
    df["AVG_VOL"] = df["Volume"].rolling(20).mean()

    df['H-L'] = df['High'] - df['Low']
    df['H-PC'] = abs(df['High'] - df['Close'].shift(1))
    df['L-PC'] = abs(df['Low'] - df['Close'].shift(1))
    df['TR'] = df[['H-L', 'H-PC', 'L-PC']].max(axis=1)
    df['ATR'] = df['TR'].rolling(window=14).mean()
    return df

# ==========================================
# 4. MASTER PROCESSOR THREAD
# ==========================================
def process_stock_thread(symbol, interval, period, h52w, l52w, nifty_return, daily_close_series):
    df = get_data(symbol, interval, period)
    if df.empty or len(df) < 60:
        return None, f"{symbol}: insufficient data ({len(df)} rows)"

    try:
        df = add_indicators(df, interval)
        close = float(df["Close"].iloc[-1])
        score = 0

        gap_pct = 0.0
        try:
            if 'd' not in interval and 'wk' not in interval and 'mo' not in interval:
                today_date = df.index[-1].date()
                today_data = df[df.index.date == today_date]
                prev_data = df[df.index.date < today_date]
                if not today_data.empty and not prev_data.empty:
                    today_open = today_data['Open'].iloc[0]
                    prev_close = prev_data['Close'].iloc[-1]
                    gap_pct = ((today_open - prev_close) / prev_close) * 100
            else:
                if len(df) >= 2:
                    gap_pct = ((df['Open'].iloc[-1] - df['Close'].iloc[-2]) / df['Close'].iloc[-2]) * 100
        except Exception:
            pass

        gap_str = f"{gap_pct:.2f}%"
        if gap_pct >= 0.5:
            gap_str += " 🟢 Up"
        elif gap_pct <= -0.5:
            gap_str += " 🔴 Down"

        stock_return = ((close - df['Close'].iloc[0]) / df['Close'].iloc[0]) * 100
        rs_score = round(stock_return - nifty_return, 2) if nifty_return is not None else 0
        rs_status = "💪 Outperform" if rs_score > 0 else "📉 Underperform"

        ai_trend, ai_conf = predict_trend_ai(df["Close"])
        xgb_prediction, xgb_confidence = train_xgboost_predictor(df)
        smc_structure, cisd_signal, smc_alert, exact_signal_time = calculate_smc_and_cisd(df)

        mtf_status = "Not Aligned"
        if daily_close_series is not None and len(daily_close_series) >= 50:
            d_ema20 = daily_close_series.ewm(span=20, adjust=False).mean().iloc[-1]
            d_ema50 = daily_close_series.ewm(span=50, adjust=False).mean().iloc[-1]
            if (d_ema20 > d_ema50) == (df["EMA20"].iloc[-1] > df["EMA50"].iloc[-1]):
                mtf_status = "ALIGNED 🟢" if (d_ema20 > d_ema50) else "ALIGNED 🔻"

        alerts = []
        rvol_val = 0.0
        avg_vol = float(df["AVG_VOL"].iloc[-1])
        current_vol = float(df["Volume"].iloc[-1])

        if pd.notna(avg_vol) and avg_vol > 0:
            rvol_val = current_vol / avg_vol

        rvol_str = f"{rvol_val:.2f}x"
        if rvol_val >= 3.0:
            rvol_str += " 🔥🔥"
            alerts.append("🔥🔥 Massive RVOL")
        elif rvol_val >= 2.0:
            rvol_str += " 🔥"
            alerts.append("🔥 High RVOL")
        elif rvol_val >= 1.5:
            rvol_str += " 🟢"

        if abs(gap_pct) >= 2.0 and rvol_val >= 3.0:
            alerts.append("📰 NEWS MOMENTUM 🚀")
            score += 2

        try:
            vwap_val = float(df["VWAP"].iloc[-1])
            low_val = float(df["Low"].iloc[-1])
            high_val = float(df["High"].iloc[-1])

            dist_to_vwap_low = abs(low_val - vwap_val) / vwap_val
            dist_to_vwap_high = abs(high_val - vwap_val) / vwap_val

            if dist_to_vwap_low <= 0.005 and close > vwap_val and rvol_val >= 1.2:
                alerts.append("💧 VWAP Bounce")
            elif dist_to_vwap_high <= 0.005 and close < vwap_val and rvol_val >= 1.2:
                alerts.append("🛑 VWAP Rejection")
        except Exception:
            pass

        rsi_val = float(df["RSI"].iloc[-1])
        if rsi_val > 70:
            alerts.append("🚨 RSI Overbought")
        elif rsi_val < 30:
            alerts.append("⚠️ RSI Oversold")
        if smc_alert != "Normal":
            alerts.append(f"🏛️ {smc_structure}")
        if cisd_signal != "None":
            alerts.append(f"⚡ {cisd_signal}")

        breakout_high = df["High"].rolling(20).max().shift(1).iloc[-1]
        breakout_low = df["Low"].rolling(20).min().shift(1).iloc[-1]
        brk_sig = "NO"
        if close > breakout_high:
            brk_sig = "BULLISH"
            alerts.append("📈 Breakout High")
        elif close < breakout_low:
            brk_sig = "BEARISH"
            alerts.append("📉 Breakout Low")

        pattern = get_candlestick_pattern(df)
        if pattern in ["Bullish Engulfing", "Hammer"]:
            alerts.append(f"✨ {pattern}")
        alert_str = ", ".join(alerts) if alerts else "No Alerts"

        macd_val = "BULLISH" if df["MACD_Line"].iloc[-1] > df["Signal_Line"].iloc[-1] else "BEARISH"
        st_dir = "UP" if df["ST_Direction"].iloc[-1] == 1 else "DOWN"
        vwap_sig = "ABOVE" if close > float(df["VWAP"].iloc[-1]) else "BELOW"

        if df["EMA20"].iloc[-1] > df["EMA50"].iloc[-1]:
            score += 1
        else:
            score -= 1
        if rsi_val > 55:
            score += 1
        elif rsi_val < 45:
            score -= 1
        if macd_val == "BULLISH":
            score += 1
        else:
            score -= 1
        if st_dir == "UP":
            score += 1
        else:
            score -= 1
        if vwap_sig == "ABOVE":
            score += 1
        else:
            score -= 1
        if brk_sig == "BULLISH":
            score += 1
        elif brk_sig == "BEARISH":
            score -= 1
        if smc_structure in ["BOS 📈", "CHOCH 🐂"] or cisd_signal == "Bullish CISD 🚀":
            score += 1

        signal = ("STRONG BUY" if score >= 4 else "BUY" if score >= 2 else
                  "STRONG SELL" if score <= -4 else "SELL" if score <= -2 else "WAIT")

        target, stoploss = "-", "-"
        try:
            atr_val = float(df["ATR"].iloc[-1])
            if pd.notna(atr_val) and atr_val > 0:
                if signal in ["STRONG BUY", "BUY"]:
                    stoploss = round(close - (1.5 * atr_val), 2)
                    target = round(close + (3.0 * atr_val), 2)
                elif signal in ["STRONG SELL", "SELL"]:
                    stoploss = round(close + (1.5 * atr_val), 2)
                    target = round(close - (3.0 * atr_val), 2)
        except Exception:
            pass

        status_52w = "Mid Range"
        if h52w and l52w:
            if close >= h52w * 0.97:
                status_52w = "🟢 Near High"
            elif close <= l52w * 1.03:
                status_52w = "🔴 Near Low"

        row = [
            exact_signal_time, symbol.replace('.NS', ''), round(close, 2), gap_str, target, stoploss,
            smc_structure, cisd_signal, xgb_prediction, f"{xgb_confidence}%", alert_str, mtf_status,
            ai_trend, f"{ai_conf}%", f"{rs_score}% ({rs_status})",
            round(float(df["Support_1"].iloc[-1]), 2), round(float(df["Resistance_1"].iloc[-1]), 2),
            round(h52w, 2) if h52w else "N/A", round(l52w, 2) if l52w else "N/A", status_52w,
            round(rsi_val, 2), brk_sig, macd_val, st_dir, vwap_sig, pattern, rvol_str, score, signal,
        ]
        return row, None
    except Exception as e:
        return None, f"{symbol}: processing error {e}"

def color_code(val):
    if isinstance(val, str):
        if any(x in val for x in ["STRONG BUY", "BULLISH", "UP", "ABOVE", "Outperform", "🟢",
                                   "BOS 📈", "CHOCH 🐂", "Bullish CISD 🚀", "🔥", "💧", "📰"]):
            return 'color: green; font-weight: bold;'
        if any(x in val for x in ["STRONG SELL", "BEARISH", "DOWN", "BELOW", "Underperform", "🔻",
                                   "🚨", "BOS 📉", "CHOCH 🐻", "Bearish CISD 🩸", "🔴", "🛑"]):
            return 'color: red; font-weight: bold;'
    return ''

def to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Scan Results")
        ws = writer.sheets["Scan Results"]
        from openpyxl.styles import Font, PatternFill, Alignment
        header_font = Font(bold=True, color="FFFFFF", name="Arial")
        header_fill = PatternFill("solid", start_color="1F2937")
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center")
        for col_cells in ws.columns:
            length = max((len(str(c.value)) for c in col_cells if c.value is not None), default=10)
            ws.column_dimensions[col_cells[0].column_letter].width = max(length + 2, 10)
        ws.freeze_panes = "A2"
    buf.seek(0)
    return buf.getvalue()

def run_full_scan(selected_stocks, interval, period):
    nifty_df = get_data("^NSEI", interval, period)
    nifty_return = ((nifty_df['Close'].iloc[-1] - nifty_df['Close'].iloc[0]) / nifty_df['Close'].iloc[0]) * 100 \
        if not nifty_df.empty else 0

    st.write("⚡ Executing Machine Learning Vectors and Liquidity Sweep Analytics...")
    high_52w_dict, low_52w_dict, daily_series_dict = {}, {}, {}

    try:
        bulk_df = get_bulk_daily_data(selected_stocks)
        if not bulk_df.empty:
            for s in selected_stocks:
                t = f"{s}.NS"
                try:
                    if isinstance(bulk_df.columns, pd.MultiIndex):
                        high_52w_dict[s] = bulk_df['High'][t].max()
                        low_52w_dict[s] = bulk_df['Low'][t].min()
                        daily_series_dict[s] = bulk_df['Close'][t].dropna()
                    else:
                        high_52w_dict[s] = bulk_df['High'].max()
                        low_52w_dict[s] = bulk_df['Low'].min()
                        daily_series_dict[s] = bulk_df['Close'].dropna()
                except Exception:
                    pass
    except Exception:
        pass

    progress = st.progress(0.0, text=f"Scanning 0 / {len(selected_stocks)}")
    results, errors = [], []
    done = 0

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(
                process_stock_thread, s, interval, period,
                high_52w_dict.get(s), low_52w_dict.get(s), nifty_return, daily_series_dict.get(s)
            ): s for s in selected_stocks
        }
        for future in as_completed(futures):
            row, err = future.result()
            if row:
                results.append(row)
            if err:
                errors.append(err)
            done += 1
            progress.progress(done / len(selected_stocks), text=f"Scanning {done} / {len(selected_stocks)}")

    progress.empty()
    df_result = pd.DataFrame(results, columns=RESULT_COLUMNS) if results else pd.DataFrame(columns=RESULT_COLUMNS)
    return df_result, errors

# ==========================================
# 5. UI TABS & RUN EXECUTION
# ==========================================
tab1, tab2 = st.tabs(["🚀 V11.11 PRO Master Dashboard", "🔍 Custom Stock Search"])

with tab1:
    should_run = run_button or (
        auto_refresh and (
            st.session_state.v11_last_run is None or
            (datetime.now() - st.session_state.v11_last_run).total_seconds() >= 180
        )
    )

    if should_run:
        selected_stocks = stocks if sector == "All NSE500" else sector_stocks[sector]
        if sector == "All NSE500" and scan_limit:
            selected_stocks = selected_stocks[:scan_limit]

        with st.spinner("Running full scan…"):
            df_result, errors = run_full_scan(selected_stocks, interval, period)

        st.session_state.v11_master_data = df_result
        st.session_state.v11_errors = errors
        st.session_state.v11_last_run = datetime.now()

    if not st.session_state.v11_master_data.empty:
        df = st.session_state.v11_master_data
        st.caption(f"Last run: {st.session_state.v11_last_run.strftime('%d-%b %Y %I:%M:%S %p')} | "
                   f"{len(df)} symbols scanned successfully.")

        sort_col = st.selectbox("Sort by", ["Score", "RVOL", "Close"], index=0)
        df_sorted = df.sort_values(sort_col, ascending=False, key=lambda s: pd.to_numeric(
            s.astype(str).str.extract(r'(-?\d+\.?\d*)')[0], errors='coerce')) if sort_col != "Score" \
            else df.sort_values("Score", ascending=False)

        styled = df_sorted.style.applymap(color_code)
        st.dataframe(styled, use_container_width=True, height=600)

        st.download_button(
            "📥 Download as Excel",
            data=to_excel_bytes(df_sorted),
            file_name=f"nse_ai_scan_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        if st.session_state.v11_errors:
            with st.expander(f"⚠️ Errors / skipped symbols ({len(st.session_state.v11_errors)})"):
                st.text("\n".join(st.session_state.v11_errors[:200]))
    else:
        st.info("👈 Configure settings in the sidebar and click **RUN ULTIMATE SCANNER**.")

    if auto_refresh and st.session_state.v11_last_run is not None:
        elapsed = (datetime.now() - st.session_state.v11_last_run).total_seconds()
        remaining = max(0, 180 - elapsed)
        st.caption(f"⏱️ Auto-refresh in ~{int(remaining)}s")
        time.sleep(2)
        st.rerun()

with tab2:
    st.subheader("🔍 Custom Stock Search")
    custom_symbol = st.text_input("Enter NSE Symbol (e.g. RELIANCE)", "RELIANCE").strip().upper()
    search_btn = st.button("🔎 Analyze Stock")

    if search_btn and custom_symbol:
        with st.spinner(f"Analyzing {custom_symbol}…"):
            nifty_df = get_data("^NSEI", interval, period)
            nifty_return = ((nifty_df['Close'].iloc[-1] - nifty_df['Close'].iloc[0]) / nifty_df['Close'].iloc[0]) * 100 \
                if not nifty_df.empty else 0

            bulk_df = get_bulk_daily_data([custom_symbol])
            h52w = l52w = None
            daily_series = None
            try:
                if isinstance(bulk_df.columns, pd.MultiIndex):
                    t = f"{custom_symbol}.NS"
                    h52w, l52w = bulk_df['High'][t].max(), bulk_df['Low'][t].min()
                    daily_series = bulk_df['Close'][t].dropna()
                elif not bulk_df.empty:
                    h52w, l52w = bulk_df['High'].max(), bulk_df['Low'].min()
                    daily_series = bulk_df['Close'].dropna()
            except Exception:
                pass

            row, err = process_stock_thread(custom_symbol, interval, period, h52w, l52w, nifty_return, daily_series)

        if err:
            st.error(err)
        elif row:
            result_df = pd.DataFrame([row], columns=RESULT_COLUMNS)
            st.dataframe(result_df.style.applymap(color_code), use_container_width=True)

            raw_df = get_data(custom_symbol, interval, period)
            if not raw_df.empty:
                st.line_chart(raw_df["Close"])
