
import streamlit as st
import pandas as pd
import numpy as np
import datetime as dt
import inspect

# ============================================================
# NSE AI PRO V17 - ADVANCED AI TIME MACHINE
#
# Reusable module for scanner.py
#
# IMPORTANT:
# - No st.set_page_config() in this file.
# - No FYERS login here.
# - FYERS client comes from app.py / scanner.py.
# - Compatible with show_advanced_ai_tab().
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
# DATA NORMALIZATION
# ============================================================

def normalize_history(raw):

    if raw is None:
        raise RuntimeError(
            "History fetcher returned empty data."
        )

    # Handle common tuple responses.
    if isinstance(raw, tuple):

        candidates = [
            item for item in raw
            if isinstance(
                item,
                (pd.DataFrame, dict, list, tuple)
            )
        ]

        if not candidates:
            raise RuntimeError(
                "History fetcher returned an invalid tuple."
            )

        raw = candidates[0]

    # Handle FYERS dictionaries or wrapped data.
    if isinstance(raw, dict):

        if raw.get("s") not in (None, "ok"):
            raise RuntimeError(
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
            raise RuntimeError(
                "No candles/data found in history response."
            )

        raw = extracted

    # DataFrame or FYERS candle list.
    if isinstance(raw, pd.DataFrame):

        df = raw.copy()

    else:

        try:
            df = pd.DataFrame(raw)
        except Exception as exc:
            raise RuntimeError(
                f"Cannot convert history data: {exc}"
            )

    # Handle FYERS six-column candle arrays.
    if list(df.columns) == list(range(6)):

        df.columns = [
            "Timestamp",
            "Open",
            "High",
            "Low",
            "Close",
            "Volume"
        ]

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

        normalized = (
            str(column)
            .strip()
            .lower()
            .replace(" ", "_")
        )

        if normalized in aliases:
            rename[column] = aliases[normalized]

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
        raise RuntimeError(
            f"Missing OHLC columns: {missing}"
        )

    # Some index data sources may not provide volume.
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

    df = df.reset_index(
        drop=True
    )

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

    start_date = today - dt.timedelta(
        days=int(days)
    )

    values = {

        "client": client,
        "fyers": client,
        "fyers_client": client,
        "fyers_obj": client,
        "api": client,
        "api_client": client,

        "symbol": symbol,
        "ticker": symbol,
        "stock": symbol,
        "stock_symbol": symbol,

        "resolution": str(resolution),
        "timeframe": str(resolution),
        "time_frame": str(resolution),
        "interval": str(resolution),
        "tf": str(resolution),

        "days": int(days),
        "history_days": int(days),
        "lookback_days": int(days),
        "period_days": int(days),

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

    except (TypeError, ValueError):

        raise RuntimeError(
            "Cannot inspect history_fetcher signature. "
            "Please check scanner.py."
        )

    positional = []
    keyword = {}

    has_var_keyword = False
    unresolved = []

    for parameter in signature.parameters.values():

        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            continue

        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            has_var_keyword = True
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

        raise RuntimeError(
            "Cannot match these required history_fetcher "
            f"parameters: {unresolved}. "
            "Please check the function signature in scanner.py."
        )

    if has_var_keyword:

        for key in [
            "client",
            "symbol",
            "resolution",
            "days"
        ]:

            if key not in keyword:
                keyword[key] = values[key]

    return history_fetcher(
        *positional,
        **keyword
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

    if history_fetcher is not None:

        raw = call_history_fetcher(
            history_fetcher=history_fetcher,
            client=client,
            symbol=symbol,
            days=days,
            resolution=resolution
        )

        return normalize_history(raw)

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

    relative_volume = (
        volume / average_volume.replace(0, np.nan)
    )

    # Neutral value when volume is unavailable.
    features["rel_vol"] = relative_volume.fillna(1.0)

    features["ret1"] = close.pct_change()

    # Next-candle direction target.
    next_close = close.shift(-1)

    features["target"] = (
        next_close > close
    ).astype(float)

    # Last candle has no known future outcome.
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

    data = build_features(
        raw
    )

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
            "Not enough data for testing."
        )

    feature_columns = [

        col for col in data.columns

        if col != "target"
    ]

    actual = []

    predicted = []

    probabilities = []

    first_test_index = (
        len(data) - test_bars
    )

    # Expanding-window walk-forward testing.
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

            index=3,

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
                "Fetching FYERS data and running AI backtest..."
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
            "Settings changed. Run the analysis again "
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
        "direction classification. It is not a guaranteed "
        "future trading signal. Brokerage, slippage and "
        "live execution are not included."
    )


# ============================================================
# SCANNER COMPATIBILITY
# IMPORTANT: Define this function only once.
# *args and **kwargs prevent duplicate argument binding.
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

    # Find a positional callback without binding it twice.
    if history_fetcher is None:

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
