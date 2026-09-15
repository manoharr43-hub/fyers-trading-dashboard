import streamlit as st
import datetime as dt
import numpy as np
import pandas as pd

st.set_page_config(
    page_title="NSE AI PRO V17",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

try:
    from fyers_apiv3 import fyersModel
except Exception as e:
    st.error(f"FYERS import error: {e}")
    st.stop()

# ---------------- SESSION ----------------
st.session_state.setdefault("access_token", None)
st.session_state.setdefault("logged_in", False)
st.session_state.setdefault("main_navigation", "🏠 Dashboard")

# ---------------- SECRETS ----------------
required = ["FYERS_CLIENT_ID", "FYERS_SECRET_KEY", "FYERS_REDIRECT_URI"]
missing = [x for x in required if x not in st.secrets]
if missing:
    st.error("Missing Streamlit secrets:")
    for x in missing:
        st.write(f"- {x}")
    st.stop()

CLIENT_ID = st.secrets["FYERS_CLIENT_ID"]
SECRET_KEY = st.secrets["FYERS_SECRET_KEY"]
REDIRECT_URI = st.secrets["FYERS_REDIRECT_URI"]

# ---------------- LOGIN ----------------
login_session = fyersModel.SessionModel(
    client_id=CLIENT_ID,
    secret_key=SECRET_KEY,
    redirect_uri=REDIRECT_URI,
    response_type="code",
    grant_type="authorization_code",
)

if not st.session_state["logged_in"]:
    st.title("📈 NSE AI PRO V17")
    st.subheader("FYERS Login")
    try:
        st.link_button(
            "🔑 Login with FYERS",
            login_session.generate_authcode(),
            use_container_width=True,
        )
    except Exception as e:
        st.error(f"Login URL error: {e}")

    params = st.query_params
    if "auth_code" in params:
        try:
            login_session.set_token(params["auth_code"])
            response = login_session.generate_token()
            if isinstance(response, dict) and response.get("s") == "ok":
                st.session_state["access_token"] = response["access_token"]
                st.session_state["logged_in"] = True
                st.query_params.clear()
                st.rerun()
            else:
                st.error(f"FYERS login failed: {response}")
        except Exception as e:
            st.error(f"Login error: {e}")
    st.stop()

try:
    fyers = fyersModel.FyersModel(
        client_id=CLIENT_ID,
        token=st.session_state["access_token"],
        is_async=False,
    )
except Exception as e:
    st.error(f"FYERS client error: {e}")
    st.stop()

# ---------------- AI BACKTEST ----------------
try:
    import xgboost as xgb
    XGB_AVAILABLE = True
except Exception:
    xgb = None
    XGB_AVAILABLE = False

def ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()

def rsi(s, n=14):
    d = s.diff()
    gain = d.clip(lower=0)
    loss = -d.clip(upper=0)
    ag = gain.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    al = loss.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    rs = ag / al.replace(0, np.nan)
    return 100 - 100 / (1 + rs)

def atr(df, n=14):
    pc = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - pc).abs(),
        (df["Low"] - pc).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()

def adx(df, n=14):
    up = df["High"].diff()
    down = -df["Low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["High"].shift(1)).abs(),
        (df["Low"] - df["Low"].shift(1)).abs()
    ], axis=1).max(axis=1)
    av = tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    pdi = 100 * plus_dm.ewm(alpha=1/n, adjust=False, min_periods=n).mean() / av
    mdi = 100 * minus_dm.ewm(alpha=1/n, adjust=False, min_periods=n).mean() / av
    denom = (pdi + mdi).replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / denom
    return dx.ewm(alpha=1/n, adjust=False, min_periods=n).mean()

def features(df):
    close = pd.to_numeric(df["Close"], errors="coerce")
    volume = pd.to_numeric(df["Volume"], errors="coerce")
    out = pd.DataFrame(index=df.index)
    out["ema20"] = ema(close, 20)
    out["ema50"] = ema(close, 50)
    out["rsi"] = rsi(close)
    mf, ms = ema(close, 12), ema(close, 26)
    ml = mf - ms
    out["macd_hist"] = ml - ml.ewm(span=9, adjust=False, min_periods=9).mean()
    out["atr"] = atr(df)
    out["adx"] = adx(df)
    out["rel_vol"] = volume / volume.rolling(20, min_periods=20).mean()
    out["ret1"] = close.pct_change()
    out["target"] = (close.shift(-1) > close).astype(int)
    return out.dropna()

def make_model():
    return xgb.XGBClassifier(
        n_estimators=120, max_depth=3, learning_rate=0.05,
        subsample=0.85, colsample_bytree=0.85, min_child_weight=3,
        reg_lambda=1.0, objective="binary:logistic",
        eval_metric="logloss", tree_method="hist",
        random_state=42, n_jobs=2, verbosity=0
    )

def history(client, symbol, days, resolution):
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    payload = {
        "symbol": symbol, "resolution": resolution, "date_format": "1",
        "range_from": start.strftime("%Y-%m-%d"),
        "range_to": end.strftime("%Y-%m-%d"), "cont_flag": "1"
    }
    r = client.history(data=payload)
    if not isinstance(r, dict) or r.get("s") != "ok":
        raise RuntimeError(f"FYERS history error: {r}")
    candles = r.get("candles", [])
    if not candles:
        raise RuntimeError("No historical candles returned.")
    df = pd.DataFrame(candles, columns=["Timestamp","Open","High","Low","Close","Volume"])
    for c in ["Open","High","Low","Close","Volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["Open","High","Low","Close"])

def run_backtest(client, symbol, resolution, days, test_bars):
    if not XGB_AVAILABLE:
        raise RuntimeError("XGBoost missing. Add xgboost>=2.1.0 to requirements.txt.")
    data = features(history(client, symbol, days, resolution))
    if len(data) < 140:
        raise RuntimeError(f"Only {len(data)} usable rows; need at least 140.")
    test_bars = min(test_bars, len(data)-100)
    start = len(data)-test_bars
    feats = [c for c in data.columns if c != "target"]
    yt, yp, probs = [], [], []
    for i in range(start, len(data)):
        train, test = data.iloc[:i], data.iloc[[i]]
        if train["target"].nunique() < 2:
            continue
        model = make_model()
        model.fit(train[feats], train["target"])
        p = float(model.predict_proba(test[feats])[0,1])
        yt.append(int(test["target"].iloc[0]))
        yp.append(int(p >= .5))
        probs.append(p)
    if not yt:
        raise RuntimeError("No valid walk-forward samples.")
    y, p = np.asarray(yt), np.asarray(yp)
    tp = int(((y==1)&(p==1)).sum())
    tn = int(((y==0)&(p==0)).sum())
    fp = int(((y==0)&(p==1)).sum())
    fn = int(((y==1)&(p==0)).sum())
    return {
        "Symbol": symbol, "Resolution": resolution, "History Days": days,
        "Accuracy %": round(100*(tp+tn)/len(y),2),
        "Precision UP %": round(100*tp/max(tp+fp,1),2),
        "Recall UP %": round(100*tp/max(tp+fn,1),2),
        "Average P(UP) %": round(100*float(np.mean(probs)),2),
        "Test Samples": len(y), "TP": tp, "TN": tn, "FP": fp, "FN": fn
    }

def ai_backtest_ui(client):
    st.divider()
    with st.expander("🧪 AI BACKTEST — Historical Walk-Forward Accuracy", expanded=False):
        st.caption("Historical accuracy is not a guarantee of future performance.")
        if not XGB_AVAILABLE:
            st.error("XGBoost is not installed.")
            return
        c1,c2,c3 = st.columns(3)
        with c1:
            symbol = st.selectbox("Select stock / index", [
                "NSE:RELIANCE-EQ","NSE:TCS-EQ","NSE:HDFCBANK-EQ",
                "NSE:INFY-EQ","NSE:SBIN-EQ","NSE:ICICIBANK-EQ",
                "NSE:NIFTYBANK-INDEX","NSE:NIFTY50-INDEX"
            ], key="bt_symbol")
        with c2:
            resolution = st.selectbox("Timeframe", ["15","30","60","D"], index=2, key="bt_resolution")
        with c3:
            bars = st.slider("Test bars", 20, 100, 60, 10, key="bt_bars")
        days = st.slider("Historical days", 90, 500, 180, 30, key="bt_days")
        if st.button("🚀 RUN AI BACKTEST", type="primary", use_container_width=True, key="run_bt"):
            with st.spinner("Running walk-forward backtest..."):
                try:
                    st.session_state["bt_result"] = run_backtest(client, symbol, resolution, days, bars)
                except Exception as e:
                    st.session_state["bt_result"] = None
                    st.error(f"Backtest failed: {e}")
        result = st.session_state.get("bt_result")
        if result:
            a,b,c,d = st.columns(4)
            a.metric("Actual Accuracy", f"{result['Accuracy %']:.2f}%")
            b.metric("Precision UP", f"{result['Precision UP %']:.2f}%")
            c.metric("Recall UP", f"{result['Recall UP %']:.2f}%")
            d.metric("Test Samples", result["Test Samples"])
            st.dataframe(pd.DataFrame([result]), use_container_width=True, hide_index=True)
            st.download_button(
                "⬇️ Export Backtest CSV",
                pd.DataFrame([result]).to_csv(index=False).encode(),
                f"ai_backtest_{dt.date.today()}.csv",
                "text/csv",
                key="bt_csv"
            )

# ---------------- PAGE RENDER ----------------
def render_page(menu, client):
    try:
        if menu == "🏠 Dashboard":
            from dashboard import show_dashboard
            show_dashboard(client)

        elif menu == "📈 Market":
            from market import show_market
            show_market(client)

        elif menu == "🧠 AI Market Intelligence":
            from ai_market_intelligence import show_ai_market_intelligence
            show_ai_market_intelligence(client)
            ai_backtest_ui(client)

        elif menu == "💼 Portfolio":
            from portfolio import show_portfolio
            show_portfolio(client)

        elif menu == "📋 Orders":
            from orders import show_orders
            show_orders(client)

        elif menu == "⚙️ Option Chain":
            from option_chain import show_option_chain
            show_option_chain(client)

        elif menu == "🤖 Scanner":
            from scanner import show_scanner
            show_scanner(client)

        elif menu == "📊 Charts":
            from charts import show_charts
            show_charts(client)

        elif menu == "💹 Trading":
            from trading import show_trading
            show_trading(client)

        elif menu == "👤 Profile":
            from profile import show_profile
            show_profile(client)

        elif menu == "⚙️ Settings":
            from settings import show_settings
            show_settings(client)

        elif menu == "🤖 AI Chart Analysis":
            from ai_chart_analysis import show_ai_chart_analysis
            show_ai_chart_analysis(client)

    except Exception as e:
        st.error(f"❌ Error loading {menu}: {e}")
        st.exception(e)

# ---------------- SIDEBAR ----------------
with st.sidebar:
    st.title("📈 NSE AI PRO V17")
    items = [
        "🏠 Dashboard","📈 Market","🧠 AI Market Intelligence",
        "💼 Portfolio","📋 Orders","⚙️ Option Chain","🤖 Scanner",
        "📊 Charts","💹 Trading","👤 Profile","⚙️ Settings",
        "🤖 AI Chart Analysis"
    ]
    current = st.session_state.get("main_navigation", items[0])
    if current not in items:
        current = items[0]
    # IMPORTANT: Do not assign the widget value back to the same
    # session_state key in the same statement. Streamlit raises:
    # StreamlitAPIException: st.session_state["main_navigation"] cannot be
    # modified after the widget with key "main_navigation" is instantiated.
    st.radio(
        "Navigation",
        items,
        index=items.index(current),
        key="main_navigation",
    )
    st.divider()
    st.caption("Main entry point: app.py")
    if st.button("🚪 Logout", use_container_width=True):
        st.session_state.clear()
        st.rerun()

# ---------------- RUN ONLY SELECTED PAGE ----------------
render_page(st.session_state["main_navigation"], fyers)
