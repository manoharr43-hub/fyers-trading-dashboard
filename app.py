import streamlit as st
from fyers_apiv3 import fyersModel
import datetime as dt
import numpy as np
import pandas as pd

# ==========================================
# PAGE CONFIG
# ==========================================
st.set_page_config(
    page_title="NSE AI PRO V12",
    page_icon="📈",
    layout="wide",
)

# ==========================================
# FYERS CONFIG
# ==========================================
REQUIRED_SECRETS = ["FYERS_CLIENT_ID", "FYERS_SECRET_KEY", "FYERS_REDIRECT_URI"]
missing = [k for k in REQUIRED_SECRETS if k not in st.secrets]
if missing:
    st.error(
        "Missing required secrets in `.streamlit/secrets.toml`: "
        + ", ".join(missing)
        + ". Add them and rerun the app."
    )
    st.stop()

CLIENT_ID = st.secrets["FYERS_CLIENT_ID"]
SECRET_KEY = st.secrets["FYERS_SECRET_KEY"]
REDIRECT_URI = st.secrets["FYERS_REDIRECT_URI"]

# ==========================================
# SESSION & LOGIN
# ==========================================
if "access_token" not in st.session_state:
    st.session_state.access_token = None
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False

session = fyersModel.SessionModel(
    client_id=CLIENT_ID,
    secret_key=SECRET_KEY,
    redirect_uri=REDIRECT_URI,
    response_type="code",
    grant_type="authorization_code",
)

if not st.session_state.logged_in:
    st.title("📈 NSE AI PRO V12")
    st.caption("Institutional-style F&O AI Decision Engine, powered by your Fyers account.")

    try:
        login_url = session.generate_authcode()
        st.link_button("🔑 Login With FYERS", login_url, use_container_width=True)
    except Exception as e:
        st.error("Could not generate the Fyers login link. Check your Fyers app credentials.")
        st.exception(e)
        st.stop()

    params = st.query_params
    if "auth_code" in params:
        try:
            session.set_token(params["auth_code"])
            response = session.generate_token()
            if response.get("s") == "ok":
                st.session_state.access_token = response["access_token"]
                st.session_state.logged_in = True
                st.query_params.clear()
                st.rerun()
            else:
                st.error(f"Fyers login failed: {response}")
        except Exception as e:
            st.error("Something went wrong while completing Fyers login.")
            st.exception(e)
    st.stop()

# ==========================================
# AI BACKTEST HELPERS
# ==========================================
try:
    import xgboost as xgb
    XGB_AVAILABLE = True
except Exception:
    xgb = None
    XGB_AVAILABLE = False


def _ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _rsi(s, n=14):
    d = s.diff()
    gain = d.clip(lower=0)
    loss = -d.clip(upper=0)
    ag = gain.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    al = loss.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    rs = ag / al.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _atr(df, n=14):
    pc = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - pc).abs(),
        (df["Low"] - pc).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()


def _adx(df, n=14):
    up = df["High"].diff()
    down = -df["Low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["High"].shift(1)).abs(),
        (df["Low"] - df["Low"].shift(1)).abs()
    ], axis=1).max(axis=1)
    atrv = tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    pdi = 100 * plus_dm.ewm(alpha=1/n, adjust=False, min_periods=n).mean() / atrv
    mdi = 100 * minus_dm.ewm(alpha=1/n, adjust=False, min_periods=n).mean() / atrv
    denom = (pdi + mdi).replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / denom
    return dx.ewm(alpha=1/n, adjust=False, min_periods=n).mean()


def _feature_frame(df):
    close = pd.to_numeric(df["Close"], errors="coerce")
    volume = pd.to_numeric(df["Volume"], errors="coerce")
    out = pd.DataFrame(index=df.index)
    out["ema20"] = _ema(close, 20)
    out["ema50"] = _ema(close, 50)
    out["rsi"] = _rsi(close)
    macd_fast = _ema(close, 12)
    macd_slow = _ema(close, 26)
    macd_line = macd_fast - macd_slow
    macd_signal = macd_line.ewm(span=9, adjust=False, min_periods=9).mean()
    out["macd_hist"] = macd_line - macd_signal
    out["atr"] = _atr(df)
    out["adx"] = _adx(df)
    out["rel_vol"] = volume / volume.rolling(20, min_periods=20).mean()
    out["ret1"] = close.pct_change()
    # Target is next candle direction. It is created after features so no
    # future value is used as an input feature.
    out["target"] = (close.shift(-1) > close).astype(int)
    return out.dropna().copy()


def _make_xgb():
    return xgb.XGBClassifier(
        n_estimators=120,
        max_depth=3,
        learning_rate=0.05,
        subsample=0.85,
        colsample_bytree=0.85,
        min_child_weight=3,
        reg_lambda=1.0,
        objective="binary:logistic",
        eval_metric="logloss",
        tree_method="hist",
        random_state=42,
        n_jobs=2,
        verbosity=0,
    )


def _metrics(y, p):
    y = np.asarray(y, dtype=int)
    p = np.asarray(p, dtype=int)
    tp = int(((y == 1) & (p == 1)).sum())
    tn = int(((y == 0) & (p == 0)).sum())
    fp = int(((y == 0) & (p == 1)).sum())
    fn = int(((y == 1) & (p == 0)).sum())
    return {
        "Accuracy %": round(100 * (tp + tn) / max(len(y), 1), 2),
        "Precision UP %": round(100 * tp / max(tp + fp, 1), 2),
        "Recall UP %": round(100 * tp / max(tp + fn, 1), 2),
        "Test Samples": len(y),
        "TP": tp, "TN": tn, "FP": fp, "FN": fn,
    }


def _fyers_history(fyers, symbol, days=120, resolution="60"):
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    payload = {
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "1",
        "range_from": start.strftime("%Y-%m-%d"),
        "range_to": end.strftime("%Y-%m-%d"),
        "cont_flag": "1",
    }
    resp = fyers.history(data=payload)
    if not isinstance(resp, dict) or resp.get("s") != "ok":
        raise RuntimeError(f"Fyers history error: {resp}")
    candles = resp.get("candles", [])
    if not candles:
        raise RuntimeError("No historical candles returned by Fyers.")
    df = pd.DataFrame(candles, columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"])
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["Open", "High", "Low", "Close"])


def run_app_backtest(fyers, symbol, resolution, days, test_bars):
    if not XGB_AVAILABLE:
        raise RuntimeError("XGBoost is not installed. Add xgboost>=2.1.0 to requirements.txt.")

    df = _fyers_history(fyers, symbol, days=days, resolution=resolution)
    data = _feature_frame(df)

    if len(data) < 140:
        raise RuntimeError(
            f"Only {len(data)} usable rows were available. Need at least 140 for a reliable test."
        )

    # Test only the most recent N unseen bars. Each bar is predicted using
    # training data strictly before that bar.
    test_bars = min(test_bars, len(data) - 100)
    start = len(data) - test_bars

    y_true, y_pred, probs = [], [], []

    for i in range(start, len(data)):
        train = data.iloc[:i]
        test = data.iloc[[i]]

        if train["target"].nunique() < 2:
            continue

        features = [c for c in data.columns if c != "target"]
        model = _make_xgb()
        model.fit(train[features], train["target"])
        prob_up = float(model.predict_proba(test[features])[0, 1])
        pred = int(prob_up >= 0.50)

        y_true.append(int(test["target"].iloc[0]))
        y_pred.append(pred)
        probs.append(prob_up)

    if not y_true:
        raise RuntimeError("No valid walk-forward test samples were produced.")

    m = _metrics(y_true, y_pred)
    m["Symbol"] = symbol
    m["Resolution"] = resolution
    m["History Days"] = days
    m["Average P(UP) %"] = round(100 * float(np.mean(probs)), 2)
    return m


# ==========================================
# LAZY PAGE MODULES
# ==========================================
def render_page(menu, fyers):
    if menu == "🏠 Dashboard":
        from dashboard import show_dashboard
        show_dashboard(fyers)
    elif menu == "📈 Market":
        from market import show_market
        show_market(fyers)
    elif menu == "🧠 AI Market Intelligence":
        from ai_market_intelligence import show_ai_market_intelligence
        show_ai_market_intelligence(fyers)

        # --------------------------------------------------------------
        # AI BACKTEST is intentionally added here, so it appears below
        # the existing AI Market Intelligence page instead of in a
        # different/unused module.
        # --------------------------------------------------------------
        st.divider()
        with st.expander("🧪 AI BACKTEST — Actual Historical Accuracy", expanded=True):
            st.caption(
                "Chronological walk-forward test. Each prediction uses only "
                "candles available before that test candle. Accuracy is NOT the "
                "same as the live AI Confidence percentage."
            )

            if not XGB_AVAILABLE:
                st.error("XGBoost is not available. Check requirements.txt for xgboost>=2.1.0.")
            else:
                col1, col2, col3 = st.columns(3)

                with col1:
                    bt_symbol = st.selectbox(
                        "Select stock / index",
                        [
                            "NSE:RELIANCE-EQ",
                            "NSE:TCS-EQ",
                            "NSE:HDFCBANK-EQ",
                            "NSE:INFY-EQ",
                            "NSE:SBIN-EQ",
                            "NSE:ICICIBANK-EQ",
                            "NSE:NIFTYBANK-INDEX",
                            "NSE:NIFTY50-INDEX",
                        ],
                        key="app_ai_bt_symbol",
                    )

                with col2:
                    bt_resolution = st.selectbox(
                        "Timeframe",
                        ["15", "30", "60", "D"],
                        index=2,
                        key="app_ai_bt_resolution",
                    )

                with col3:
                    bt_bars = st.slider(
                        "Test bars",
                        min_value=20,
                        max_value=100,
                        value=60,
                        step=10,
                        key="app_ai_bt_bars",
                    )

                bt_days = st.slider(
                    "Historical data window (calendar days)",
                    min_value=90,
                    max_value=500,
                    value=180,
                    step=30,
                    key="app_ai_bt_days",
                )

                if st.button(
                    "🚀 RUN AI BACKTEST",
                    type="primary",
                    use_container_width=True,
                    key="app_run_ai_backtest",
                ):
                    with st.spinner(
                        f"Running XGBoost walk-forward backtest for {bt_symbol}..."
                    ):
                        try:
                            result = run_app_backtest(
                                fyers,
                                bt_symbol,
                                bt_resolution,
                                bt_days,
                                bt_bars,
                            )
                            st.session_state["app_ai_bt_result"] = result
                        except Exception as e:
                            st.session_state["app_ai_bt_result"] = None
                            st.error(f"Backtest failed: {e}")

                result = st.session_state.get("app_ai_bt_result")
                if result:
                    st.success(f"Backtest completed: {result['Symbol']}")

                    a, b, c, d = st.columns(4)
                    a.metric("Actual Accuracy", f"{result['Accuracy %']:.2f}%")
                    b.metric("Precision UP", f"{result['Precision UP %']:.2f}%")
                    c.metric("Recall UP", f"{result['Recall UP %']:.2f}%")
                    d.metric("Test Samples", result["Test Samples"])

                    st.dataframe(
                        pd.DataFrame([result]),
                        use_container_width=True,
                        hide_index=True,
                    )

                    st.download_button(
                        "⬇️ Export Backtest CSV",
                        data=pd.DataFrame([result]).to_csv(index=False).encode("utf-8"),
                        file_name=f"ai_backtest_{dt.date.today()}.csv",
                        mime="text/csv",
                        key="app_ai_bt_csv",
                    )

    elif menu == "💼 Portfolio":
        from portfolio import show_portfolio
        show_portfolio(fyers)
    elif menu == "📋 Orders":
        from orders import show_orders
        show_orders(fyers)
    elif menu == "⚙️ Option Chain":
        from option_chain import show_option_chain
        show_option_chain(fyers)
    elif menu == "🤖 Scanner":
        from scanner import show_scanner
        show_scanner(fyers)
    elif menu == "📊 Charts":
        from charts import show_charts
        show_charts(fyers)
    elif menu == "💹 Trading":
        from trading import show_trading
        show_trading(fyers)
    elif menu == "👤 Profile":
        from profile import show_profile
        show_profile(fyers)
    elif menu == "⚙️ Settings":
        from settings import show_settings
        show_settings(fyers)


# ==========================================
# MAIN APP
# ==========================================
try:
    fyers = fyersModel.FyersModel(
        client_id=CLIENT_ID,
        token=st.session_state.access_token,
        is_async=False,
    )
except Exception as e:
    st.error("Could not initialize the Fyers client with your access token. Please log in again.")
    st.exception(e)
    if st.button("🔁 Reset login"):
        st.session_state.clear()
        st.rerun()
    st.stop()

menu = st.sidebar.radio(
    "Navigation",
    [
        "🏠 Dashboard",
        "📈 Market",
        "🧠 AI Market Intelligence",
        "💼 Portfolio",
        "📋 Orders",
        "⚙️ Option Chain",
        "🤖 Scanner",
        "📊 Charts",
        "💹 Trading",
        "👤 Profile",
        "⚙️ Settings",
    ],
)

try:
    render_page(menu, fyers)
except ModuleNotFoundError as e:
    st.error(f"The `{menu}` page module isn't available yet ({e}).")
except Exception as e:
    st.error(f"Error Loading {menu}")
    st.exception(e)

st.sidebar.divider()
if st.sidebar.button("🚪 Logout"):
    st.session_state.clear()
    st.rerun()
