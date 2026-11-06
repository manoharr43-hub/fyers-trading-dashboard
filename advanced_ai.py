
import streamlit as st
import pandas as pd
import numpy as np
import datetime as dt
import inspect

# ============================================================
# NSE AI PRO V17
# ADVANCED AI - TIME MACHINE
#
# Reusable module for scanner.py
#
# IMPORTANT:
# 1. Do not call st.set_page_config() here.
# 2. Do not create another FYERS login.
# 3. FYERS client comes from app.py.
# 4. Supports scanner history_fetcher callback.
# ============================================================


# ============================================================
# OPTIONAL XGBOOST
# ============================================================

try:
    import xgboost as xgb
    XGB_AVAILABLE = True
except Exception:
    xgb = None
    XGB_AVAILABLE = False


# ============================================================
# TECHNICAL INDICATORS
# ============================================================

def ema(series, period):

    return series.ewm(
        span=period,
        adjust=False,
        min_periods=period
    ).mean()


def rsi(series, period=14):

    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    return 100 - (100 / (1 + rs))


def atr(df, period=14):

    previous_close = df["Close"].shift(1)

    true_range = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - previous_close).abs(),
            (df["Low"] - previous_close).abs()
        ],
        axis=1
    ).max(axis=1)

    return true_range.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()


def adx(df, period=14):

    up_move = df["High"].diff()
    down_move = -df["Low"].diff()

    plus_dm = pd.Series(
        np.where(
            (up_move > down_move) & (up_move > 0),
            up_move,
            0.0
        ),
        index=df.index
    )

    minus_dm = pd.Series(
        np.where(
            (down_move > up_move) & (down_move > 0),
            down_move,
            0.0
        ),
        index=df.index
    )

    previous_close = df["Close"].shift(1)

    true_range = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - previous_close).abs(),
            (df["Low"] - previous_close).abs()
        ],
        axis=1
    ).max(axis=1)

    avg_tr = true_range.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    plus_di = (
        100 * plus_dm.ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period
        ).mean() / avg_tr.replace(0, np.nan)
    )

    minus_di = (
        100 * minus_dm.ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period
        ).mean() / avg_tr.replace(0, np.nan)
    )

    denominator = (plus_di + minus_di).replace(0, np.nan)

    dx = (
        100 * (plus_di - minus_di).abs() / denominator
    )

    return dx.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()


# ============================================================
# DATA NORMALIZATION
# ============================================================

def normalize_history(raw):

    if raw is None:
        raise RuntimeError("History fetcher returned empty data.")

    if isinstance(raw, tuple) and len(raw) > 0:
        raw = raw[0]

    if isinstance(raw, dict):

        status = raw.get("s")

        if status is not None and status != "ok":
            raise RuntimeError(
                f"History API error: {raw}"
            )

        raw = (
            raw.get("candles")
            or raw.get("data")
            or raw.get("history")
        )

        if raw is None:
            raise RuntimeError(
                "History response has no candles/data."
            )

    if isinstance(raw, pd.DataFrame):

        df = raw.copy()

    else:

        try:
            df = pd.DataFrame(
                raw,
                columns=[
                    "Timestamp",
                    "Open",
                    "High",
                    "Low",
                    "Close",
                    "Volume"
                ]
            )
        except Exception as exc:
            raise RuntimeError(
                f"Cannot convert history to DataFrame: {exc}"
            )

    # Normalize column names.
    normalized_names = {}

    for column in df.columns:

        name = str(column).strip().lower()

        normalized_names[name] = column

    aliases = {
        "Timestamp": [
            "timestamp", "time", "datetime",
            "date", "ts"
        ],
        "Open": ["open", "o"],
        "High": ["high", "h"],
        "Low": ["low", "l"],
        "Close": ["close", "c", "ltp"],
        "Volume": [
            "volume", "vol", "v", "traded_volume"
        ]
    }

    rename_columns = {}

    for standard, possible_names in aliases.items():

        for alias in possible_names:

            if alias in normalized_names:

                rename_columns[
                    normalized_names[alias]
                ] = standard

                break

    df = df.rename(columns=rename_columns)

    required = [
        "Open",
        "High",
        "Low",
        "Close",
        "Volume"
    ]

    missing = [
        column for column in required
        if column not in df.columns
    ]

    if missing:
        raise RuntimeError(
            f"Missing OHLCV columns: {missing}"
        )

    if "Timestamp" not in df.columns:

        if isinstance(df.index, pd.DatetimeIndex):
            df["Timestamp"] = df.index

    for column in required:

        df[column] = pd.to_numeric(
            df[column],
            errors="coerce"
        )

    df = df.dropna(subset=required)

    if "Timestamp" in df.columns:

        df = df.dropna(subset=["Timestamp"])

        df = df.sort_values("Timestamp")

        df = df.drop_duplicates(
            subset=["Timestamp"],
            keep="last"
        )

    df = df.reset_index(drop=True)

    if len(df) < 100:
        raise RuntimeError(
            f"Only {len(df)} candles received. "
            "At least 100 candles are required."
        )

    return df


# ============================================================
# SCANNER HISTORY FETCHER ADAPTER
# ============================================================

def call_history_fetcher(
    history_fetcher,
    client,
    symbol,
    days,
    resolution
):

    if history_fetcher is None:
        return None

    if not callable(history_fetcher):
        raise RuntimeError(
            "history_fetcher must be a callable function."
        )

    today = dt.date.today()
    start_date = today - dt.timedelta(days=int(days))

    available_values = {

        # FYERS client aliases
        "client": client,
        "fyers": client,
        "fyers_client": client,
        "api": client,
        "api_client": client,

        # Symbol aliases
        "symbol": symbol,
        "ticker": symbol,
        "stock": symbol,
        "stock_symbol": symbol,

        # Timeframe aliases
        "resolution": str(resolution),
        "timeframe": str(resolution),
        "interval": str(resolution),
        "tf": str(resolution),

        # Historical range aliases
        "days": int(days),
        "lookback_days": int(days),
        "history_days": int(days),
        "period_days": int(days),

        "start_date": start_date.strftime("%Y-%m-%d"),
        "from_date": start_date.strftime("%Y-%m-%d"),
        "range_from": start_date.strftime("%Y-%m-%d"),

        "end_date": today.strftime("%Y-%m-%d"),
        "to_date": today.strftime("%Y-%m-%d"),
        "range_to": today.strftime("%Y-%m-%d")
    }

    try:

        signature = inspect.signature(history_fetcher)

    except (TypeError, ValueError):

        # Fallback for callables without inspectable signatures.
        return history_fetcher(
            client,
            symbol,
            str(resolution)
        )

    positional = []
    keyword = {}

    unresolved = []

    for parameter in signature.parameters.values():

        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD
        ):
            continue

        name = parameter.name.lower()

        if name in available_values:

            value = available_values[name]

            if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
                positional.append(value)

            else:
                keyword[parameter.name] = value

        elif parameter.default == inspect.Parameter.empty:

            unresolved.append(parameter.name)

    if not unresolved:

        return history_fetcher(
            *positional,
            **keyword
        )

    raise RuntimeError(
        "Cannot match required parameters in history_fetcher: "
        f"{unresolved}. Please check the function signature "
        "in scanner.py."
    )


# ============================================================
# FYERS HISTORY
# ============================================================

def fetch_history(
    client,
    symbol,
    days,
    resolution,
    history_fetcher=None
):

    # Prefer Scanner's existing history fetcher when supplied.
    if history_fetcher is not None:

        raw = call_history_fetcher(
            history_fetcher,
            client,
            symbol,
            days,
            resolution
        )

        return normalize_history(raw)

    # Otherwise use FYERS directly.
    if client is None:
        raise RuntimeError(
            "FYERS client is not connected."
        )

    end_date = dt.date.today()

    start_date = end_date - dt.timedelta(
        days=int(days)
    )

    payload = {

        "symbol": symbol,

        "resolution": str(resolution),

        "date_format": "1",

        "range_from": start_date.strftime("%Y-%m-%d"),

        "range_to": end_date.strftime("%Y-%m-%d"),

        "cont_flag": "1"
    }

    response = client.history(data=payload)

    return normalize_history(response)


# ============================================================
# FEATURE ENGINEERING
# ============================================================

def build_features(df):

    df = normalize_history(df)

    close = df["Close"]
    volume = df["Volume"]

    out = pd.DataFrame(index=df.index)

    out["ema20"] = ema(close, 20)

    out["ema50"] = ema(close, 50)

    out["rsi"] = rsi(close, 14)

    macd_line = (
        ema(close, 12) - ema(close, 26)
    )

    out["macd_hist"] = (
        macd_line - ema(macd_line, 9)
    )

    out["atr"] = atr(df, 14)

    out["adx"] = adx(df, 14)

    average_volume = volume.rolling(
        20,
        min_periods=20
    ).mean()

    out["rel_vol"] = (
        volume / average_volume.replace(0, np.nan)
    )

    out["ret1"] = close.pct_change()

    # Predict next candle direction.
    next_close = close.shift(-1)

    out["target"] = (
        next_close > close
    ).astype(float)

    # Last candle has no known future result.
    out.loc[
        next_close.isna(),
        "target"
    ] = np.nan

    out = out.replace(
        [np.inf, -np.inf],
        np.nan
    )

    return out.dropna()


# ============================================================
# XGBOOST MODEL
# ============================================================

def make_model():

    if not XGB_AVAILABLE:

        raise RuntimeError(
            "XGBoost missing. Add "
            "xgboost>=2.1.0 to requirements.txt."
        )

    return xgb.XGBClassifier(

        n_estimators=100,

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


# ============================================================
# WALK-FORWARD TIME MACHINE
# ============================================================

def run_time_machine(
    client,
    symbol,
    resolution,
    days,
    test_bars,
    history_fetcher=None
):

    if not XGB_AVAILABLE:

        raise RuntimeError(
            "XGBoost is not installed."
        )

    raw = fetch_history(

        client=client,

        symbol=symbol,

        days=days,

        resolution=resolution,

        history_fetcher=history_fetcher
    )

    data = build_features(raw)

    if len(data) < 100:

        raise RuntimeError(
            f"Only {len(data)} usable rows. "
            "At least 100 are required."
        )

    # Keep at least 80 previous observations for training.
    test_bars = min(
        int(test_bars),
        len(data) - 80
    )

    if test_bars < 1:

        raise RuntimeError(
            "Not enough candles for walk-forward testing."
        )

    feature_columns = [

        column for column in data.columns

        if column != "target"
    ]

    actual = []

    predicted = []

    probabilities = []

    first_test_index = len(data) - test_bars

    for index in range(
        first_test_index,
        len(data)
    ):

        train = data.iloc[:index]

        test = data.iloc[[index]]

        if train["target"].nunique() < 2:
            continue

        model = make_model()

        model.fit(
            train[feature_columns],
            train["target"]
        )

        probability_up = float(

            model.predict_proba(
                test[feature_columns]
            )[0, 1]
        )

        actual.append(
            int(test["target"].iloc[0])
        )

        predicted.append(
            int(probability_up >= 0.50)
        )

        probabilities.append(
            probability_up
        )

    if not actual:

        raise RuntimeError(
            "No valid walk-forward predictions generated."
        )

    y_true = np.asarray(actual)

    y_pred = np.asarray(predicted)

    # Confusion matrix
    tp = int(
        ((y_true == 1) & (y_pred == 1)).sum()
    )

    tn = int(
        ((y_true == 0) & (y_pred == 0)).sum()
    )

    fp = int(
        ((y_true == 0) & (y_pred == 1)).sum()
    )

    fn = int(
        ((y_true == 1) & (y_pred == 0)).sum()
    )

    total = len(y_true)

    accuracy = (
        100 * (tp + tn) / max(total, 1)
    )

    precision = (
        100 * tp / max(tp + fp, 1)
    )

    recall = (
        100 * tp / max(tp + fn, 1)
    )

    return {

        "Symbol": symbol,

        "Timeframe": str(resolution),

        "History Days": int(days),

        "Accuracy %": round(accuracy, 2),

        "Precision UP %": round(precision, 2),

        "Recall UP %": round(recall, 2),

        "Average P(UP) %": round(
            100 * float(np.mean(probabilities)),
            2
        ),

        "Test Samples": total,

        "TP": tp,

        "TN": tn,

        "FP": fp,

        "FN": fn
    }


# ============================================================
# ADVANCED AI USER INTERFACE
# ============================================================

def show_advanced_ai(
    client,
    history_fetcher=None,
    **kwargs
):

    st.subheader(
        "🧠 ADVANCED AI — TIME MACHINE"
    )

    st.caption(
        "FYERS Historical Data | XGBoost | "
        "Walk-Forward Market Analysis"
    )

    if client is None:

        st.error(
            "FYERS client is not connected. Please login."
        )

        return

    if not XGB_AVAILABLE:

        st.error(
            "XGBoost is missing. Add "
            "xgboost>=2.1.0 to requirements.txt."
        )

        return

    # --------------------------------------------------------
    # INPUTS
    # --------------------------------------------------------

    left, right = st.columns(2)

    with left:

        symbol = st.selectbox(

            "Select Symbol",

            [

                "NSE:RELIANCE-EQ",

                "NSE:TCS-EQ",

                "NSE:HDFCBANK-EQ",

                "NSE:INFY-EQ",

                "NSE:SBIN-EQ",

                "NSE:ICICIBANK-EQ",

                "NSE:NIFTYBANK-INDEX",

                "NSE:NIFTY50-INDEX"
            ],

            key="advanced_ai_symbol"
        )

        resolution = st.selectbox(

            "Timeframe",

            ["5", "15", "30", "60", "D"],

            index=3,

            format_func=lambda x: {

                "5": "5 Minutes",

                "15": "15 Minutes",

                "30": "30 Minutes",

                "60": "1 Hour",

                "D": "Daily"

            }[x],

            key="advanced_ai_resolution"
        )

    with right:

        days = st.selectbox(

            "Historical Days",

            [90, 180, 270, 365, 500],

            index=3,

            key="advanced_ai_days"
        )

        test_bars = st.slider(

            "Test Candles",

            min_value=20,

            max_value=100,

            value=60,

            step=10,

            key="advanced_ai_test_bars"
        )

    # --------------------------------------------------------
    # RUN
    # --------------------------------------------------------

    run_button = st.button(

        "🚀 RUN AI TIME MACHINE",

        type="primary",

        use_container_width=True,

        key="advanced_ai_run"
    )

    current_key = (
        symbol,
        resolution,
        days,
        test_bars
    )

    if run_button:

        try:

            with st.spinner(
                "Fetching market data and running AI backtest..."
            ):

                result = run_time_machine(

                    client=client,

                    symbol=symbol,

                    resolution=resolution,

                    days=days,

                    test_bars=test_bars,

                    history_fetcher=history_fetcher
                )

            st.session_state[
                "advanced_ai_result"
            ] = result

            st.session_state[
                "advanced_ai_result_key"
            ] = current_key

        except Exception as exc:

            st.session_state.pop(
                "advanced_ai_result",
                None
            )

            st.session_state.pop(
                "advanced_ai_result_key",
                None
            )

            st.error(
                f"Advanced AI failed: {exc}"
            )

            return

    # --------------------------------------------------------
    # RESULTS
    # --------------------------------------------------------

    result = st.session_state.get(
        "advanced_ai_result"
    )

    saved_key = st.session_state.get(
        "advanced_ai_result_key"
    )

    if not result:

        st.info(
            "Select symbol and timeframe, "
            "then click RUN AI TIME MACHINE."
        )

        return

    if saved_key != current_key:

        st.info(
            "Settings changed. Click RUN AI TIME MACHINE "
            "to generate a fresh report."
        )

        return

    st.divider()

    st.subheader(
        "📊 HISTORICAL AI PERFORMANCE"
    )

    a, b, c, d = st.columns(4)

    a.metric(
        "Accuracy",
        f"{result['Accuracy %']:.2f}%"
    )

    b.metric(
        "Precision UP",
        f"{result['Precision UP %']:.2f}%"
    )

    c.metric(
        "Recall UP",
        f"{result['Recall UP %']:.2f}%"
    )

    d.metric(
        "Test Samples",
        result["Test Samples"]
    )

    st.divider()

    st.subheader(
        "🎯 MODEL PROBABILITY"
    )

    st.metric(

        "Average Probability of UP",

        f"{result['Average P(UP) %']:.2f}%"
    )

    st.divider()

    st.subheader(
        "🔍 PREDICTION BREAKDOWN"
    )

    breakdown = pd.DataFrame(

        [

            {
                "Metric": "True Positive",
                "Count": result["TP"]
            },

            {
                "Metric": "True Negative",
                "Count": result["TN"]
            },

            {
                "Metric": "False Positive",
                "Count": result["FP"]
            },

            {
                "Metric": "False Negative",
                "Count": result["FN"]
            }
        ]
    )

    st.dataframe(

        breakdown,

        use_container_width=True,

        hide_index=True
    )

    st.divider()

    st.subheader(
        "📋 COMPLETE AI REPORT"
    )

    report = pd.DataFrame(
        [result]
    )

    st.dataframe(

        report,

        use_container_width=True,

        hide_index=True
    )

    st.download_button(

        "📥 DOWNLOAD TIME MACHINE REPORT",

        data=report.to_csv(
            index=False
        ).encode("utf-8"),

        file_name="advanced_ai_time_machine.csv",

        mime="text/csv",

        use_container_width=True,

        key="advanced_ai_download"
    )

    st.warning(

        "This report measures historical next-candle "
        "direction classification. It is not a live trading "
        "signal or a guarantee of future returns. "
        "Brokerage and slippage are not included."
    )


# ============================================================
# SCANNER COMPATIBILITY FUNCTION
# ============================================================

def show_advanced_ai_tab(
    client,
    history_fetcher=None,
    **kwargs
):

    show_advanced_ai(

        client=client,

        history_fetcher=history_fetcher,

        **kwargs
    )


# ============================================================
# OTHER COMPATIBILITY FUNCTIONS
# ============================================================

def show_advanced_ai_time_machine(
    client,
    history_fetcher=None,
    **kwargs
):

    show_advanced_ai(

        client=client,

        history_fetcher=history_fetcher,

        **kwargs
    )


def ai_backtest_ui(
    client,
    history_fetcher=None,
    **kwargs
):

    show_advanced_ai(

        client=client,

        history_fetcher=history_fetcher,

        **kwargs
    )
