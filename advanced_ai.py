import streamlit as st
import pandas as pd
import numpy as np
import datetime as dt

# Reusable Advanced AI module for NSE AI PRO V17.
# Do not add st.set_page_config() here; app.py owns page configuration.

try:
    import xgboost as xgb
    XGB_AVAILABLE = True
except Exception:
    xgb = None
    XGB_AVAILABLE = False


def _ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _rsi(s, n=14):
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
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
    plus = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    pc = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - pc).abs(),
        (df["Low"] - pc).abs()
    ], axis=1).max(axis=1)
    av = tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    pdi = 100 * plus.ewm(alpha=1/n, adjust=False, min_periods=n).mean() / av.replace(0, np.nan)
    mdi = 100 * minus.ewm(alpha=1/n, adjust=False, min_periods=n).mean() / av.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1/n, adjust=False, min_periods=n).mean()


def build_features(df):
    close = pd.to_numeric(df["Close"], errors="coerce")
    volume = pd.to_numeric(df["Volume"], errors="coerce")
    out = pd.DataFrame(index=df.index)
    out["ema20"] = _ema(close, 20)
    out["ema50"] = _ema(close, 50)
    out["rsi"] = _rsi(close)
    macd = _ema(close, 12) - _ema(close, 26)
    out["macd_hist"] = macd - _ema(macd, 9)
    out["atr"] = _atr(df)
    out["adx"] = _adx(df)
    vavg = volume.rolling(20, min_periods=20).mean()
    out["rel_vol"] = volume / vavg.replace(0, np.nan)
    out["ret1"] = close.pct_change()

    next_close = close.shift(-1)
    out["target"] = (next_close > close).astype(float)
    out.loc[next_close.isna(), "target"] = np.nan
    return out.replace([np.inf, -np.inf], np.nan).dropna()


def fetch_history(client, symbol, days, resolution):
    if client is None:
        raise RuntimeError("FYERS client is not connected.")

    end = dt.date.today()
    start = end - dt.timedelta(days=int(days))
    payload = {
        "symbol": symbol,
        "resolution": str(resolution),
        "date_format": "1",
        "range_from": start.strftime("%Y-%m-%d"),
        "range_to": end.strftime("%Y-%m-%d"),
        "cont_flag": "1"
    }
    response = client.history(data=payload)
    if not isinstance(response, dict) or response.get("s") != "ok":
        raise RuntimeError(f"FYERS history error: {response}")

    candles = response.get("candles") or []
    if not candles:
        raise RuntimeError("No historical candles returned.")

    df = pd.DataFrame(
        candles,
        columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"]
    )
    for col in ["Timestamp", "Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["Timestamp", "Open", "High", "Low", "Close", "Volume"])
    df = df.sort_values("Timestamp").drop_duplicates("Timestamp").reset_index(drop=True)
    if len(df) < 160:
        raise RuntimeError(f"Only {len(df)} candles received; need at least 160.")
    return df


def make_model():
    if not XGB_AVAILABLE:
        raise RuntimeError("XGBoost missing. Add xgboost>=2.1.0 to requirements.txt.")
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
        verbosity=0
    )


def run_time_machine(client, symbol, resolution, days, test_bars):
    raw = fetch_history(client, symbol, days, resolution)
    data = build_features(raw)
    if len(data) < 140:
        raise RuntimeError(f"Only {len(data)} usable rows; need at least 140.")

    test_bars = min(int(test_bars), len(data) - 100)
    if test_bars < 1:
        raise RuntimeError("Not enough data for the selected test window.")

    features = [c for c in data.columns if c != "target"]
    actual, predicted, probabilities = [], [], []

    # Expanding-window walk-forward: train only on earlier rows.
    for i in range(len(data) - test_bars, len(data)):
        train, test = data.iloc[:i], data.iloc[[i]]
        if train["target"].nunique() < 2:
            continue
        model = make_model()
        model.fit(train[features], train["target"])
        prob_up = float(model.predict_proba(test[features])[0, 1])
        actual.append(int(test["target"].iloc[0]))
        predicted.append(int(prob_up >= 0.50))
        probabilities.append(prob_up)

    if not actual:
        raise RuntimeError("No valid walk-forward predictions.")

    y = np.asarray(actual)
    p = np.asarray(predicted)
    tp = int(((y == 1) & (p == 1)).sum())
    tn = int(((y == 0) & (p == 0)).sum())
    fp = int(((y == 0) & (p == 1)).sum())
    fn = int(((y == 1) & (p == 0)).sum())
    total = len(y)

    return {
        "Symbol": symbol,
        "Timeframe": str(resolution),
        "History Days": int(days),
        "Accuracy %": round(100 * (tp + tn) / total, 2),
        "Precision UP %": round(100 * tp / max(tp + fp, 1), 2),
        "Recall UP %": round(100 * tp / max(tp + fn, 1), 2),
        "Average P(UP) %": round(100 * float(np.mean(probabilities)), 2),
        "Test Samples": total,
        "TP": tp, "TN": tn, "FP": fp, "FN": fn
    }


def show_advanced_ai(client):
    st.subheader("🧠 ADVANCED AI — TIME MACHINE")
    st.caption("FYERS historical candles | XGBoost | Walk-forward evaluation")

    if client is None:
        st.error("FYERS client is not connected.")
        return
    if not XGB_AVAILABLE:
        st.error("XGBoost missing. Add xgboost>=2.1.0 to requirements.txt.")
        return

    left, right = st.columns(2)
    with left:
        symbol = st.selectbox(
            "Select Symbol",
            [
                "NSE:RELIANCE-EQ", "NSE:TCS-EQ", "NSE:HDFCBANK-EQ",
                "NSE:INFY-EQ", "NSE:SBIN-EQ", "NSE:ICICIBANK-EQ",
                "NSE:NIFTYBANK-INDEX", "NSE:NIFTY50-INDEX"
            ],
            key="advanced_ai_symbol"
        )
        resolution = st.selectbox(
            "Timeframe", ["5", "15", "30", "60", "D"],
            index=3, key="advanced_ai_resolution"
        )
    with right:
        days = st.selectbox(
            "Historical Days", [90, 180, 270, 365, 500],
            index=1, key="advanced_ai_days"
        )
        test_bars = st.slider(
            "Test Candles", 20, 100, 60, 10,
            key="advanced_ai_test_bars"
        )

    if st.button(
        "🚀 RUN AI TIME MACHINE",
        type="primary",
        use_container_width=True,
        key="advanced_ai_run"
    ):
        try:
            with st.spinner("Fetching FYERS data and running backtest..."):
                result = run_time_machine(
                    client, symbol, resolution, days, test_bars
                )
            st.session_state["advanced_ai_result"] = result
        except Exception as exc:
            st.session_state.pop("advanced_ai_result", None)
            st.error(f"Time Machine failed: {exc}")

    result = st.session_state.get("advanced_ai_result")
    if not result:
        st.info("Select settings and click RUN AI TIME MACHINE.")
        return

    st.divider()
    st.subheader("📊 Historical AI Performance")
    a, b, c, d = st.columns(4)
    a.metric("Accuracy", f"{result['Accuracy %']:.2f}%")
    b.metric("Precision UP", f"{result['Precision UP %']:.2f}%")
    c.metric("Recall UP", f"{result['Recall UP %']:.2f}%")
    d.metric("Test Samples", result["Test Samples"])

    st.metric("Average Model Probability of UP", f"{result['Average P(UP) %']:.2f}%")

    st.subheader("🔍 Prediction Breakdown")
    breakdown = pd.DataFrame([
        {"Metric": "True Positive", "Count": result["TP"]},
        {"Metric": "True Negative", "Count": result["TN"]},
        {"Metric": "False Positive", "Count": result["FP"]},
        {"Metric": "False Negative", "Count": result["FN"]}
    ])
    st.dataframe(breakdown, use_container_width=True, hide_index=True)

    report = pd.DataFrame([result])
    st.subheader("📋 Complete Report")
    st.dataframe(report, use_container_width=True, hide_index=True)
    st.download_button(
        "📥 DOWNLOAD TIME MACHINE REPORT",
        data=report.to_csv(index=False).encode("utf-8"),
        file_name="advanced_ai_time_machine.csv",
        mime="text/csv",
        use_container_width=True,
        key="advanced_ai_download"
    )
    st.caption(
        "Historical next-candle classification results are not a guarantee "
        "of future returns. Brokerage, slippage and live execution are not included."
    )


# Compatibility aliases for existing callers.
def show_advanced_ai_time_machine(client):
    show_advanced_ai(client)
# Compatibility with scanner.py
def show_advanced_ai_tab(
    client,
    history_fetcher=None,
    **kwargs
):
    show_advanced_ai(client)

def ai_backtest_ui(client):
    show_advanced_ai(client)# Compatibility function for scanner.py
def show_advanced_ai_tab(client):
    show_advanced_ai(client)
