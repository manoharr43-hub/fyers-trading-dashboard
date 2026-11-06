
import streamlit as st
import pandas as pd
import numpy as np
import datetime as dt
import inspect


# ============================================================
# NSE AI PRO V17
# ADVANCED AI — TIME MACHINE
#
# Reusable module for scanner.py
#
# IMPORTANT:
# 1. No st.set_page_config() in this file.
# 2. No FYERS login in this file.
# 3. FYERS client comes from app.py / scanner.py.
# 4. Scanner history fetcher is optional.
# 5. If Scanner data is empty, use direct FYERS fallback.
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
        ).mean()
        / avg_tr.replace(0, np.nan)
    )

    minus_di = (
        100 * minus_dm.ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period
        ).mean()
        / avg_tr.replace(0, np.nan)
    )

    denominator = (
        plus_di + minus_di
    ).replace(0, np.nan)

    dx = (
        100 * (plus_di - minus_di).abs()
        / denominator
    )

    return dx.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()


# ============================================================
# NORMALIZE HISTORY DATA
# ============================================================

def normalize_history(raw):

    if raw is None:
        raise ValueError("History data is None.")

    # Handle tuple responses.
    if isinstance(raw, tuple):

        candidates = [
            item for item in raw
            if isinstance(
                item,
                (pd.DataFrame, dict, list, tuple)
            )
        ]

        if not candidates:
            raise ValueError(
                "History fetcher returned an empty tuple."
            )

        raw = candidates[0]

    # Handle API dictionary.
    if isinstance(raw, dict):

        status = raw.get("s")

        if status is not None and status != "ok":
            raise ValueError(
                f"History API error: {raw}"
            )

        extracted = None

        for key in [
            "candles",
            "data",
            "history",
            "df",
            "result"
        ]:

            if key in raw and raw[key] is not None:
                extracted = raw[key]
                break

        if extracted is None:
            raise ValueError(
                "No candles found in history response."
            )

        raw = extracted

    # Convert input into DataFrame.
    if isinstance(raw, pd.DataFrame):

        df = raw.copy()

    else:

        if raw is None or len(raw) == 0:
            raise ValueError("History data is empty.")

        df = pd.DataFrame(raw)

    if df.empty:
        raise ValueError("History DataFrame is empty.")

    # FYERS list format:
    # Timestamp, Open, High, Low, Close, Volume
    if list(df.columns) == list(range(6)):

        df.columns = [
            "Timestamp",
            "Open",
            "High",
            "Low",
            "Close",
            "Volume"
        ]

    # Column name aliases.
    aliases = {
        "timestamp": "Timestamp",
        "time": "Timestamp",
        "datetime": "Timestamp",
        "date": "Timestamp",
        "ts": "Timestamp",

        "open": "Open",
        "o": "Open",

        "high": "High",
        "h": "High",

        "low": "Low",
        "l": "Low",

        "close": "Close",
        "c": "Close",
        "ltp": "Close",

        "volume": "Volume",
        "vol": "Volume",
        "v": "Volume"
    }

    rename = {}

    for column in df.columns:

        key = (
            str(column)
            .strip()
            .lower()
            .replace(" ", "_")
        )

        if key in aliases:
            rename[column] = aliases[key]

    df = df.rename(columns=rename)

    required = [
        "Open",
        "High",
        "Low",
        "Close"
    ]

    missing = [
        col for col in required
        if col not in df.columns
    ]

    if missing:
        raise ValueError(
            f"Missing OHLC columns: {missing}"
        )

    # Volume can be zero for index data.
    if "Volume" not in df.columns:
        df["Volume"] = 0.0

    for col in [
        "Open",
        "High",
        "Low",
        "Close",
        "Volume"
    ]:

        df[col] = pd.to_numeric(
            df[col],
            errors="coerce"
        )

    df = df.dropna(
        subset=required
    )

    if "Timestamp" in df.columns:

        df = df.dropna(
            subset=["Timestamp"]
        )

        df = df.sort_values(
            "Timestamp"
        )

        df = df.drop_duplicates(
            subset=["Timestamp"],
            keep="last"
        )

    elif isinstance(df.index, pd.DatetimeIndex):

        df["Timestamp"] = df.index

    df = df.reset_index(
        drop=True
    )

    if len(df) < 100:

        raise ValueError(
            f"Only {len(df)} valid candles found. "
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

    if not callable(history_fetcher):

        raise ValueError(
            "history_fetcher must be a callable function."
        )

    today = dt.date.today()

    start_date = today - dt.timedelta(
        days=int(days)
    )

    values = {

        # Client aliases
        "client": client,
        "fyers": client,
        "fyers_client": client,
        "fyers_obj": client,
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
        "time_frame": str(resolution),
        "interval": str(resolution),
        "tf": str(resolution),

        # Period aliases
        "days": int(days),
        "history_days": int(days),
        "lookback_days": int(days),
        "period_days": int(days),

        # Date aliases
        "start_date": start_date.strftime("%Y-%m-%d"),
        "from_date": start_date.strftime("%Y-%m-%d"),
        "range_from": start_date.strftime("%Y-%m-%d"),

        "end_date": today.strftime("%Y-%m-%d"),
        "to_date": today.strftime("%Y-%m-%d"),
        "range_to": today.strftime("%Y-%m-%d")
    }

    try:

        signature = inspect.signature(
            history_fetcher
        )

    except (TypeError, ValueError) as exc:

        raise ValueError(
            f"Cannot inspect history_fetcher: {exc}"
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

        if name in values:

            value = values[name]

            if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
                positional.append(value)

            else:
                keyword[parameter.name] = value

        elif parameter.default == inspect.Parameter.empty:

            unresolved.append(parameter.name)

    if unresolved:

        raise ValueError(
            "Unknown required history_fetcher parameters: "
            f"{unresolved}"
        )

    return history_fetcher(
        *positional,
        **keyword
    )


# ============================================================
# DIRECT FYERS HISTORY
# ============================================================

def fetch_direct_fyers_history(
    client,
    symbol,
    days,
    resolution
):

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

        "range_from": start_date.strftime(
            "%Y-%m-%d"
        ),

        "range_to": end_date.strftime(
            "%Y-%m-%d"
        ),

        "cont_flag": "1"
    }

    response = client.history(
        data=payload
    )

    return normalize_history(
        response
    )


# ============================================================
# HISTORY FETCHING WITH FALLBACK
# ============================================================

def fetch_history(
    client,
    symbol,
    days,
    resolution,
    history_fetcher=None
):

    callback_error = None

    # First try Scanner's history function.
    if history_fetcher is not None:

        try:

            raw = call_history_fetcher(

                history_fetcher=history_fetcher,

                client=client,

                symbol=symbol,

                days=days,

                resolution=resolution
            )

            return normalize_history(raw)

        except Exception as exc:

            callback_error = str(exc)

    # Scanner data unavailable or invalid:
    # fallback to direct FYERS API.
    try:

        return fetch_direct_fyers_history(

            client=client,

            symbol=symbol,

            days=days,

            resolution=resolution
        )

    except Exception as exc:

        if callback_error:

            raise RuntimeError(
                "Scanner history fetcher failed: "
                f"{callback_error}. "
                "Direct FYERS fallback also failed: "
                f"{exc}"
            )

        raise RuntimeError(
            f"FYERS history failed: {exc}"
        )


# ============================================================
# FEATURE ENGINEERING
# ============================================================

def build_features(df):

    df = normalize_history(df)

    close = df["Close"]

    volume = df["Volume"]

    features = pd.DataFrame(
        index=df.index
    )

    features["ema20"] = ema(
        close,
        20
    )

    features["ema50"] = ema(
        close,
        50
    )

    features["rsi"] = rsi(
        close,
        14
    )

    macd_line = (
        ema(close, 12) - ema(close, 26)
    )

    features["macd_hist"] = (
        macd_line - ema(macd_line, 9)
    )

    features["atr"] = atr(
        df,
        14
    )

    features["adx"] = adx(
        df,
        14
    )

    average_volume = volume.rolling(
        20,
        min_periods=20
    ).mean()

    features["rel_vol"] = (
        volume / average_volume.replace(0, np.nan)
    )

    # Index volume may be unavailable.
    features["rel_vol"] = features[
        "rel_vol"
    ].fillna(1.0)

    features["ret1"] = close.pct_change()

    # Next-candle direction.
    next_close = close.shift(-1)

    features["target"] = (
        next_close > close
    ).astype(float)

    # Last candle has no known next-candle result.
    features.loc[
        next_close.isna(),
        "target"
    ] = np.nan

    features = features.replace(
        [np.inf, -np.inf],
        np.nan
    )

    return features.dropna()


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
            f"Only {len(data)} usable feature rows. "
            "At least 100 are required."
        )

    test_bars = min(
        int(test_bars),
        len(data) - 80
    )

    if test_bars < 1:

        raise RuntimeError(
            "Not enough data for walk-forward testing."
        )

    feature_columns = [

        col for col in data.columns

        if col != "target"
    ]

    actual = []
    predicted = []
    probabilities = []

    first_test_index = len(data) - test_bars

    # Train only with candles preceding each test candle.
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
            "No valid walk-forward predictions."
        )

    y_true = np.asarray(actual)

    y_pred = np.asarray(predicted)

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

        "Accuracy %": round(
            accuracy,
            2
        ),

        "Precision UP %": round(
            precision,
            2
        ),

        "Recall UP %": round(
            recall,
            2
        ),

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
# ADVANCED AI UI
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
            "FYERS client is not connected."
        )

        return

    if not XGB_AVAILABLE:

        st.error(
            "XGBoost missing. Add "
            "xgboost>=2.1.0 to requirements.txt."
        )

        return

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

            index=0,

            format_func=lambda value: {

                "5": "5 Minutes",
                "15": "15 Minutes",
                "30": "30 Minutes",
                "60": "1 Hour",
                "D": "Daily"

            }[value],

            key="advanced_ai_resolution"
        )

    with right:

        days = st.selectbox(

            "Historical Days",

            [90, 180, 270, 365, 500],

            index=1,

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

    current_key = (
        symbol,
        resolution,
        days,
        test_bars
    )

    if st.button(

        "🚀 RUN AI TIME MACHINE",

        type="primary",

        use_container_width=True,

        key="advanced_ai_run"
    ):

        try:

            with st.spinner(
                "Fetching historical candles and running AI..."
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
            "Settings changed. Run the analysis again."
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
        "direction classification. It is not a guaranteed "
        "future trading signal. Brokerage, slippage and "
        "live execution are not included."
    )


# ============================================================
# SCANNER COMPATIBILITY
# Define this function only once.
# ============================================================

def show_advanced_ai_tab(*args, **kwargs):

    client = kwargs.pop(
        "client",
        None
    )

    if client is None:
        client = kwargs.pop(
            "fyers",
            None
        )

    if client is None:
        client = kwargs.pop(
            "fyers_client",
            None
        )

    history_fetcher = kwargs.pop(
        "history_fetcher",
        None
    )

    if history_fetcher is None:
        history_fetcher = kwargs.pop(
            "data_fetcher",
            None
        )

    if history_fetcher is None:
        history_fetcher = kwargs.pop(
            "history_func",
            None
        )

    # First positional argument is normally FYERS client.
    if client is None and len(args) >= 1:
        client = args[0]

    # Second positional argument may be a callback.
    if history_fetcher is None and len(args) >= 2:

        for value in args[1:]:

            if callable(value):
                history_fetcher = value
                break

    if client is None:

        st.error(
            "Advanced AI: FYERS client is missing."
        )

        return

    return show_advanced_ai(

        client=client,

        history_fetcher=history_fetcher
    )


# ============================================================
# OTHER COMPATIBILITY FUNCTIONS
# ============================================================

def show_advanced_ai_time_machine(
    client,
    history_fetcher=None,
    **kwargs
):

    return show_advanced_ai(

        client=client,

        history_fetcher=history_fetcher
    )


def ai_backtest_ui(
    client,
    history_fetcher=None,
    **kwargs
):

    return show_advanced_ai(

        client=client,

        history_fetcher=history_fetcher
    )
