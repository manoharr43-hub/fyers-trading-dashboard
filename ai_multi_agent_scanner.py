"""
AI Multi-Agent Scanner for NSE AI PRO V17
Yahoo Finance market data edition.

- Uses existing scanner.py only to load the NSE/F&O symbol universes.
- Uses Yahoo Finance (yfinance) for all scanner candle data.
- Does not call FYERS history/quotes and does not place orders.
- Keeps the existing multi-timeframe scoring and exports.
"""

import concurrent.futures
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

try:
    import yfinance as yf
except Exception:
    yf = None

IST = ZoneInfo("Asia/Kolkata")

INDEX_NAMES = {
    "NIFTY", "NIFTY50", "BANKNIFTY", "NIFTYBANK", "FINNIFTY",
    "MIDCPNIFTY", "NIFTYNEXT50", "SENSEX", "BANKEX",
}


def _normalise_symbols(values):
    """Accept a list/tuple/set or a DataFrame returned by scanner.py."""
    if values is None:
        return []
    if isinstance(values, pd.DataFrame):
        for col in ("symbol", "Symbol", "fyers_symbol", "tradingsymbol"):
            if col in values.columns:
                values = values[col].dropna().astype(str).tolist()
                break
        else:
            return []
    if isinstance(values, dict):
        values = values.get("symbols", values.get("data", []))

    result = []
    for item in values:
        if isinstance(item, dict):
            item = (
                item.get("symbol")
                or item.get("Symbol")
                or item.get("fyers_symbol")
                or item.get("tradingsymbol")
            )
        if item:
            symbol = str(item).strip()
            if symbol and symbol not in result:
                result.append(symbol)
    return result


def _is_index_symbol(symbol):
    text = str(symbol).upper().replace("-INDEX", "").replace("NSE:", "")
    token = text.split("-")[0].split("_")[0]
    return token in INDEX_NAMES or any(token.startswith(name) for name in INDEX_NAMES)


def _to_yahoo_symbol(symbol):
    """
    Convert common FYERS/NSE symbols to Yahoo tickers.
    Examples:
      NSE:RELIANCE-EQ -> RELIANCE.NS
      NSE:SBIN-EQ     -> SBIN.NS
    """
    value = str(symbol).strip().upper()
    value = value.replace("NSE:", "").replace("BSE:", "")
    value = value.replace("-EQ", "").replace("-BE", "")
    value = value.replace("_EQ", "")
    if value.endswith(".NS") or value.endswith(".BO"):
        return value
    # The F&O stock universe is NSE; use .NS for plain NSE symbols.
    return f"{value}.NS"


def _closed_candles(df, minutes):
    """Keep only fully completed candles; return standard OHLCV columns."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    out = df.copy()
    # yfinance normally returns a DatetimeIndex.
    if "Time" not in out.columns:
        if "Timestamp" in out.columns:
            out["Time"] = out["Timestamp"]
        else:
            out["Time"] = out.index

    out["Time"] = pd.to_datetime(out["Time"], errors="coerce")
    out = out.dropna(subset=["Time"]).sort_values("Time")
    if out.empty:
        return pd.DataFrame()

    times = out["Time"]
    try:
        if times.dt.tz is None:
            times = times.dt.tz_localize(IST, ambiguous="NaT", nonexistent="shift_forward")
        else:
            times = times.dt.tz_convert(IST)
    except (TypeError, AttributeError):
        return pd.DataFrame()

    now = datetime.now(IST)
    out = out.loc[(times + pd.Timedelta(minutes=minutes)) <= now].copy()

    # Yahoo columns may use lower-case names depending on caller/version.
    rename = {}
    for col in out.columns:
        name = str(col).strip().lower()
        if name in ("open", "high", "low", "close", "volume"):
            rename[col] = name.capitalize()
    out = out.rename(columns=rename)

    required = ["Open", "High", "Low", "Close", "Volume"]
    if any(col not in out.columns for col in required):
        return pd.DataFrame()

    for col in required:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.dropna(subset=required).reset_index(drop=True)


def _fetch_yahoo_timeframe(symbol, interval, period="60d"):
    """Fetch one symbol/timeframe from Yahoo Finance."""
    if yf is None:
        raise RuntimeError(
            "yfinance is not installed. Add 'yfinance>=0.2.50' to requirements.txt."
        )

    ticker = _to_yahoo_symbol(symbol)
    try:
        data = yf.download(
            tickers=ticker,
            period=period,
            interval=interval,
            auto_adjust=False,
            actions=False,
            progress=False,
            threads=False,
            group_by="column",
            timeout=20,
        )
    except Exception as exc:
        raise RuntimeError(f"Yahoo download failed for {ticker}: {exc}") from exc

    if data is None or data.empty:
        raise RuntimeError(
            f"Yahoo returned no {interval} candles for {ticker}. "
            "The symbol may be unavailable, rate-limited, or outside Yahoo's intraday history."
        )

    # Recent yfinance versions can return MultiIndex columns even for one ticker.
    if isinstance(data.columns, pd.MultiIndex):
        levels = [list(data.columns.get_level_values(i).unique()) for i in range(data.columns.nlevels)]
        price_names = {"Open", "High", "Low", "Close", "Adj Close", "Volume"}
        selected_level = None
        for level_no in range(data.columns.nlevels):
            if price_names.intersection(set(map(str, levels[level_no]))):
                selected_level = level_no
                break
        if selected_level is not None:
            data.columns = data.columns.get_level_values(selected_level)
        else:
            raise RuntimeError(f"Unexpected Yahoo column format for {ticker}: {data.columns}")

    # Remove duplicate column labels if Yahoo returns them.
    data = data.loc[:, ~pd.Index(data.columns).duplicated()]
    return data


def _agent_analysis(df):
    """Deterministic technical agent; returns direction, score and reasons."""
    if df is None or len(df) < 25:
        return {
            "direction": "NO TRADE",
            "score": 0,
            "reasons": ["Insufficient completed candles"],
        }

    d = df.copy()
    close = pd.to_numeric(d["Close"], errors="coerce")
    volume = pd.to_numeric(d["Volume"], errors="coerce")

    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))

    avg_vol = volume.rolling(20).mean()
    rvol = (
        float(volume.iloc[-1] / avg_vol.iloc[-1])
        if pd.notna(avg_vol.iloc[-1]) and avg_vol.iloc[-1] > 0
        else 0.0
    )

    last = float(close.iloc[-1])
    score = 0
    reasons = []

    if ema9.iloc[-1] > ema21.iloc[-1]:
        score += 1
        reasons.append("EMA 9 above EMA 21")
    else:
        score -= 1
        reasons.append("EMA 9 below EMA 21")

    if last > ema50.iloc[-1]:
        score += 1
        reasons.append("Price above EMA 50")
    else:
        score -= 1
        reasons.append("Price below EMA 50")

    if pd.notna(rsi.iloc[-1]):
        if rsi.iloc[-1] >= 55:
            score += 1
            reasons.append(f"RSI bullish ({rsi.iloc[-1]:.1f})")
        elif rsi.iloc[-1] <= 45:
            score -= 1
            reasons.append(f"RSI bearish ({rsi.iloc[-1]:.1f})")
        else:
            reasons.append(f"RSI neutral ({rsi.iloc[-1]:.1f})")

    prior_high = d["High"].iloc[-21:-1].max()
    prior_low = d["Low"].iloc[-21:-1].min()
    if last > prior_high:
        score += 1
        reasons.append("20-candle upside breakout")
    elif last < prior_low:
        score -= 1
        reasons.append("20-candle downside breakdown")

    if rvol >= 1.5:
        reasons.append(f"Relative volume elevated ({rvol:.2f}x)")
        if score > 0:
            score += 1
        elif score < 0:
            score -= 1

    direction = "BUY" if score >= 3 else "SELL" if score <= -3 else "HOLD"
    return {
        "direction": direction,
        "score": int(score),
        "rsi": round(float(rsi.iloc[-1]), 2) if pd.notna(rsi.iloc[-1]) else None,
        "rvol": round(rvol, 2),
        "close": round(last, 2),
        "reasons": reasons,
    }


def _fetch_one(symbol):
    """Fetch 5M/15M/1H from Yahoo; no FYERS calls are made here."""
    frames = {}
    errors = []

    intervals = (
        ("5M", "5m", 5),
        ("15M", "15m", 15),
        ("1H", "60m", 60),
    )

    for label, interval, minutes in intervals:
        try:
            raw = _fetch_yahoo_timeframe(symbol, interval, period="60d")
            frames[label] = _closed_candles(raw, minutes)
            if frames[label].empty:
                errors.append(f"{label}: no usable completed Yahoo candles")
        except Exception as exc:
            frames[label] = pd.DataFrame()
            errors.append(f"{label}: {exc}")

    analyses = {tf: _agent_analysis(data) for tf, data in frames.items()}
    valid = [a for a in analyses.values() if a["direction"] != "NO TRADE"]

    if len(valid) != 3:
        signal = "NO TRADE"
        confidence = 0
    else:
        weights = {"5M": 0.30, "15M": 0.40, "1H": 0.30}
        weighted = sum(analyses[tf]["score"] * weights[tf] for tf in weights)
        all_buy = all(analyses[tf]["direction"] == "BUY" for tf in weights)
        all_sell = all(analyses[tf]["direction"] == "SELL" for tf in weights)
        signal = (
            "BUY" if all_buy and weighted >= 3
            else "SELL" if all_sell and weighted <= -3
            else "HOLD"
        )
        confidence = min(99, round(abs(weighted) / 4 * 100))

    return {
        "Symbol": symbol,
        "Yahoo Symbol": _to_yahoo_symbol(symbol),
        "Signal": signal,
        "Confidence %": confidence,
        "5M": analyses["5M"]["direction"],
        "15M": analyses["15M"]["direction"],
        "1H": analyses["1H"]["direction"],
        "5M Score": analyses["5M"]["score"],
        "15M Score": analyses["15M"]["score"],
        "1H Score": analyses["1H"]["score"],
        "Last Price": analyses["15M"].get("close"),
        "15M RSI": analyses["15M"].get("rsi"),
        "15M RVOL": analyses["15M"].get("rvol"),
        "Reason": " | ".join(analyses["15M"]["reasons"]),
        "Errors": " | ".join(errors),
    }


def show_ai_multi_agent_scanner(client=None):
    """Streamlit page. client is retained for app.py compatibility but not used."""
    st.title("🤖 AI Multi-Agent Scanner")
    st.caption(
        "Yahoo Finance data • Completed 5M / 15M / 1H candles • "
        "Multi-timeframe confirmation"
    )
    st.info("Scanner market data is fetched from Yahoo Finance. FYERS quote/history calls are not used.")
    st.warning(
        "Yahoo Finance is an unofficial data source. Intraday data may be delayed, "
        "missing, rate-limited, or unavailable for some symbols. Signals are research "
        "outputs, not guaranteed predictions. No orders are placed."
    )

    if yf is None:
        st.error("Missing dependency: add 'yfinance>=0.2.50' to requirements.txt and redeploy.")
        return

    try:
        from scanner import load_nse_equity_symbols, load_fo_stocks
    except Exception as exc:
        st.error(f"Could not import existing scanner.py symbol loaders: {exc}")
        st.info(
            "The existing scanner.py must contain load_nse_equity_symbols "
            "and load_fo_stocks. This module no longer imports its FYERS data-fetch helper."
        )
        return

    c1, c2, c3 = st.columns(3)
    with c1:
        universe = st.selectbox(
            "Universe", ["F&O Stocks", "NSE Equity"], key="ma_universe"
        )
    with c2:
        workers = st.selectbox(
            "Parallel workers", [1, 2, 3, 4], index=1, key="ma_workers"
        )
    with c3:
        st.metric("Market data source", "Yahoo Finance")

    try:
        if universe == "F&O Stocks":
            symbols = _normalise_symbols(load_fo_stocks())
        else:
            symbols = _normalise_symbols(load_nse_equity_symbols())
    except Exception as exc:
        st.error(f"Symbol list error: {exc}")
        return

    symbols = [s for s in symbols if not _is_index_symbol(s)]
    # Remove duplicates while preserving order.
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        st.error("No stock symbols were returned by the existing scanner symbol loader.")
        return

    max_symbols = len(symbols)
    st.caption(
        f"Available symbols: {max_symbols}. Index symbols are excluded. "
        "Select the full universe to scan every symbol returned by the existing loader."
    )
    limit_options = list(dict.fromkeys([10, 25, 50, 100, 200, max_symbols]))
    default_index = limit_options.index(max_symbols)
    limit = st.selectbox(
        "Maximum symbols to scan",
        limit_options,
        index=default_index,
        key="ma_limit",
    )
    selected_symbols = symbols[:limit]
    st.caption(f"Selected universe: {len(selected_symbols)} / {max_symbols} stocks.")

    if st.button(
        "🚀 RUN YAHOO 11-AGENT SCAN",
        type="primary",
        use_container_width=True,
        key="run_multi_agent",
    ):
        progress = st.progress(0)
        status = st.empty()
        rows = []
        errors = []
        completed = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_fetch_one, sym): sym for sym in selected_symbols
            }
            for future in concurrent.futures.as_completed(futures):
                sym = futures[future]
                try:
                    rows.append(future.result())
                except Exception as exc:
                    errors.append({"Symbol": sym, "Error": str(exc)})
                completed += 1
                progress.progress(completed / max(len(futures), 1))
                status.write(f"Scanned {completed}/{len(futures)}: {sym}")

        result = pd.DataFrame(rows)
        if not result.empty:
            result = result.sort_values(
                ["Confidence %", "Symbol"], ascending=[False, True]
            )
            st.session_state["multi_agent_results"] = result
        else:
            st.session_state["multi_agent_results"] = pd.DataFrame()

        st.session_state["multi_agent_errors"] = errors
        status.success(
            f"Scan completed: {len(rows)} symbols processed, {len(errors)} unexpected errors."
        )

    result = st.session_state.get("multi_agent_results")
    if isinstance(result, pd.DataFrame) and not result.empty:
        buy_count = int((result["Signal"] == "BUY").sum())
        sell_count = int((result["Signal"] == "SELL").sum())
        hold_count = int((result["Signal"] == "HOLD").sum())
        no_trade_count = int((result["Signal"] == "NO TRADE").sum())

        a, b, c, d = st.columns(4)
        a.metric("BUY", buy_count)
        b.metric("SELL", sell_count)
        c.metric("HOLD", hold_count)
        d.metric("NO TRADE", no_trade_count)

        filter_signal = st.selectbox(
            "Show signals",
            ["ALL", "BUY", "SELL", "HOLD", "NO TRADE"],
            key="ma_filter",
        )
        view = result if filter_signal == "ALL" else result[result["Signal"] == filter_signal]
        st.dataframe(view, use_container_width=True, hide_index=True)

        st.download_button(
            "⬇️ Download CSV",
            result.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"multi_agent_scan_{datetime.now(IST):%Y%m%d_%H%M%S}.csv",
            mime="text/csv",
            key="ma_csv",
        )

        try:
            import io
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                result.to_excel(writer, index=False, sheet_name="AI Scanner")
            st.download_button(
                "⬇️ Download Excel",
                buffer.getvalue(),
                file_name=f"multi_agent_scan_{datetime.now(IST):%Y%m%d_%H%M%S}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="ma_xlsx",
            )
        except Exception as exc:
            st.info(f"Excel export unavailable: {exc}")

        # A row can be processed but still have incomplete timeframes.
        incomplete = result[result["Signal"] == "NO TRADE"]
        if not incomplete.empty:
            with st.expander(f"Yahoo data missing / insufficient ({len(incomplete)})"):
                st.dataframe(
                    incomplete[["Symbol", "Yahoo Symbol", "Errors"]],
                    use_container_width=True,
                    hide_index=True,
                )

        errors = st.session_state.get("multi_agent_errors", [])
        if errors:
            with st.expander(f"Unexpected scan errors ({len(errors)})"):
                st.dataframe(pd.DataFrame(errors), use_container_width=True, hide_index=True)
