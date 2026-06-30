import streamlit as st
import pandas as pd
import requests
import time
import io
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Configuration ────────────────────────────────────────────────────────────
# 365 days of daily candles also serves as our 52-week high/low window.
DATE_FROM = (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")
DATE_TO = datetime.today().strftime("%Y-%m-%d")

# Fyers publishes a daily-refreshed master of all tradable NSE Capital
# Market (equity) symbols at this public URL. We use it instead of a
# hardcoded list so the scanner covers the whole NSE equity universe.
FYERS_NSE_CM_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_CM.csv"

# Fyers rate-limits the history/quotes API (commonly ~10 req/sec on most
# plans). Scanning 2000+ symbols with unlimited threads will trigger 429s
# or silent throttling, so we cap concurrency and batch with small pauses.
MAX_WORKERS = 8
BATCH_SIZE = 50
BATCH_PAUSE_SECONDS = 1.0


# ── Symbol Universe ──────────────────────────────────────────────────────────
@st.cache_data(ttl=60 * 60 * 12)  # refresh twice a day at most
def load_nse_equity_symbols() -> list[str]:
    """
    Downloads Fyers' NSE Capital Market symbol master and returns all
    NSE equity (-EQ) symbols in 'NSE:SYMBOL-EQ' format.
    """
    try:
        resp = requests.get(FYERS_NSE_CM_SYMBOL_MASTER, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        st.error(f"Could not download Fyers symbol master: {e}")
        return []

    # The CSV has no header row; Fyers' documented columns are positional.
    # Column 9 (0-indexed) is typically the Fyers trading symbol.
    lines = resp.text.strip().split("\n")
    symbols = []
    for line in lines:
        parts = line.split(",")
        if len(parts) < 10:
            continue
        sym = parts[9].strip()
        if sym.startswith("NSE:") and sym.endswith("-EQ"):
            symbols.append(sym)

    return sorted(set(symbols))


# ── Helper Functions ─────────────────────────────────────────────────────────
def _fetch_symbol(fyers, symbol: str):
    """Returns (result_dict_or_None, error_message_or_None)."""
    try:
        resp = fyers.history({
            "symbol": symbol, "resolution": "D", "date_format": "1",
            "range_from": DATE_FROM, "range_to": DATE_TO, "cont_flag": "1"
        })
    except Exception as e:
        return None, f"{symbol}: exception {e}"

    if not isinstance(resp, dict):
        return None, f"{symbol}: no response"
    if resp.get("s") != "ok":
        return None, f"{symbol}: {resp.get('message', resp.get('s'))}"
    candles = resp.get("candles")
    if not candles or len(candles) < 30:
        return None, f"{symbol}: insufficient history ({len(candles) if candles else 0} candles)"

    df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
    df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")

    try:
        return _analyse(symbol, df), None
    except Exception as e:
        return None, f"{symbol}: analysis error {e}"


def _calculate_smc_and_cisd(df: pd.DataFrame):
    """Simplified Smart Money Concepts structure + CISD detection on daily candles.
    Returns (smc_structure, cisd_signal, signal_time_str)."""
    if len(df) < 30:
        return "Range ➖", "None", "N/A"

    d = df.copy()
    d["Prev_High"] = d["High"].shift(1)
    d["Prev_Low"] = d["Low"].shift(1)
    d["Bullish_CISD"] = (d["Low"] < d["Prev_Low"]) & (d["Close"] > d["Prev_High"])
    d["Bearish_CISD"] = (d["High"] > d["Prev_High"]) & (d["Close"] < d["Prev_Low"])

    d["Local_High"] = d["High"].rolling(window=10).max().shift(1)
    d["Local_Low"] = d["Low"].rolling(window=10).min().shift(1)
    d["EMA20"] = d["Close"].ewm(span=20).mean()
    d["EMA50"] = d["Close"].ewm(span=50).mean()
    d["Bullish_Trend"] = d["EMA20"] > d["EMA50"]
    d["Break_Up"] = d["Close"] > d["Local_High"]
    d["Break_Down"] = d["Close"] < d["Local_Low"]

    recent = d.tail(20)

    cisd_events = recent[recent["Bullish_CISD"] | recent["Bearish_CISD"]]
    cisd_signal = "None"
    cisd_time_str = "N/A"
    if not cisd_events.empty:
        is_bull = bool(cisd_events["Bullish_CISD"].iloc[-1])
        cisd_signal = "Bullish CISD 🚀" if is_bull else "Bearish CISD 🩸"
        cisd_time_str = cisd_events["Time"].iloc[-1].strftime("%d-%b-%Y")

    smc_events = recent[recent["Break_Up"] | recent["Break_Down"]]
    smc_structure = "Range ➖"
    smc_time_str = "N/A"
    if not smc_events.empty:
        is_up = bool(smc_events["Break_Up"].iloc[-1])
        is_bull_trend = bool(smc_events["Bullish_Trend"].iloc[-1])
        if is_up:
            smc_structure = "BOS 📈" if is_bull_trend else "CHOCH 🐂"
        else:
            smc_structure = "BOS 📉" if not is_bull_trend else "CHOCH 🐻"
        smc_time_str = smc_events["Time"].iloc[-1].strftime("%d-%b-%Y")

    signal_time = cisd_time_str if cisd_signal != "None" else (
        smc_time_str if smc_structure != "Range ➖" else df["Time"].iloc[-1].strftime("%d-%b-%Y")
    )

    return smc_structure, cisd_signal, signal_time


def _analyse(symbol: str, df: pd.DataFrame) -> dict:
    close, volume = df["Close"], df["Volume"]

    ema20 = close.ewm(span=20).mean().iloc[-1]
    ema50 = close.ewm(span=50).mean().iloc[-1]
    ema200 = close.ewm(span=200).mean().iloc[-1] if len(close) >= 200 else close.ewm(span=len(close)).mean().iloc[-1]

    vol_avg20 = volume.tail(20).mean()
    rvol = (volume.iloc[-1] / vol_avg20) if vol_avg20 > 0 else 0

    trend_score = sum([close.iloc[-1] > ema20, close.iloc[-1] > ema50, close.iloc[-1] > ema200]) / 3

    roc = (close.iloc[-1] / close.iloc[-10] - 1) * 100 if len(close) >= 10 else 0

    ai_score = min(round((rvol * 15) + (trend_score * 40) + min(max(roc, 0), 10) * 2 + 20, 1), 100)

    # ── Gap % (today's open vs previous close) ─────────────────────────────
    gap_pct = 0.0
    if len(df) >= 2:
        gap_pct = ((df["Open"].iloc[-1] - df["Close"].iloc[-2]) / df["Close"].iloc[-2]) * 100
    gap_str = f"{gap_pct:.2f}%"
    if gap_pct >= 0.5:
        gap_str += " 🟢"
    elif gap_pct <= -0.5:
        gap_str += " 🔴"

    # ── SMC structure / CISD / signal time ──────────────────────────────────
    smc_structure, cisd_signal, signal_time = _calculate_smc_and_cisd(df)

    # ── 52-week high/low status (DATE_FROM window ≈ 52 weeks) ──────────────
    h52w = df["High"].max()
    l52w = df["Low"].min()
    last_close = close.iloc[-1]
    if last_close >= h52w * 0.97:
        status_52w = "🟢 Near High"
    elif last_close <= l52w * 1.03:
        status_52w = "🔴 Near Low"
    else:
        status_52w = "Mid Range"

    # ── Breakout (vs prior 20-day high/low, excluding today) ───────────────
    breakout_high = df["High"].rolling(20).max().shift(1).iloc[-1]
    breakout_low = df["Low"].rolling(20).min().shift(1).iloc[-1]
    if pd.notna(breakout_high) and last_close > breakout_high:
        breakout = "📈 Bullish"
    elif pd.notna(breakout_low) and last_close < breakout_low:
        breakout = "📉 Bearish"
    else:
        breakout = "NO"

    return {
        "Symbol": symbol.replace("NSE:", "").replace("-EQ", ""),
        "Close": round(close.iloc[-1], 2),
        "RVOL": round(rvol, 2),
        "AI Score": ai_score,
        "Smart Money": "🏦 Institutional" if ai_score > 70 else "⚖️ Neutral" if ai_score > 45 else "🔻 Distribution",
        "Signal": "🟢 BUY" if ai_score > 65 else "🔴 SELL" if ai_score < 40 else "🟡 HOLD",
        "Signal Time": signal_time,
        "Gap %": gap_str,
        "SMC Structure": smc_structure,
        "CISD": cisd_signal,
        "52W Status": status_52w,
        "Breakout": breakout,
    }


def run_scan(fyers, symbols: list[str]):
    """Threaded, rate-limited scan with a progress bar. Returns (results, errors)."""
    results, errors = [], []
    progress = st.progress(0.0, text=f"Scanning 0 / {len(symbols)}")
    done = 0

    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_symbol, fyers, s): s for s in batch}
            for future in as_completed(futures):
                res, err = future.result()
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                done += 1
                progress.progress(done / len(symbols), text=f"Scanning {done} / {len(symbols)}")

        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)  # throttle between batches to respect rate limits

    progress.empty()
    return results, errors


def _color_code(val):
    if isinstance(val, str):
        if any(x in val for x in ["BUY", "Institutional", "🟢", "BOS 📈", "CHOCH 🐂", "Bullish CISD 🚀", "Near High", "Bullish"]):
            return "color: green; font-weight: bold;"
        if any(x in val for x in ["SELL", "Distribution", "🔴", "BOS 📉", "CHOCH 🐻", "Bearish CISD 🩸", "Near Low", "Bearish"]):
            return "color: red; font-weight: bold;"
    return ""


def to_excel_bytes(df: pd.DataFrame) -> bytes:
    """Builds an in-memory formatted .xlsx from the scan results."""
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


# ── Main Application ──────────────────────────────────────────────────────────
def show_scanner(fyers):
    st.title("🚀 NSE AI PRO V13 — Institutional Scanner")

    symbols = load_nse_equity_symbols()
    st.caption(f"Loaded {len(symbols)} NSE equity symbols from Fyers symbol master.")

    if not symbols:
        st.warning("No symbols loaded — check network access to public.fyers.in.")
        return

    col1, col2 = st.columns([1, 3])
    with col1:
        limit = st.number_input(
            "Limit symbols (0 = all)", min_value=0, max_value=len(symbols), value=200, step=50,
            help="Scanning all 2000+ symbols can take several minutes and may hit API rate limits. "
                 "Start with a smaller limit to test."
        )
    with col2:
        st.caption(
            f"Estimated time at {MAX_WORKERS} concurrent workers: "
            f"~{((limit or len(symbols)) / MAX_WORKERS) * 0.3 / 60:.1f}–"
            f"{((limit or len(symbols)) / MAX_WORKERS) * 1.0 / 60:.1f} min (rough estimate)."
        )

    scan_universe = symbols if limit == 0 else symbols[:limit]

    if st.button(f"🚀 Run Scan ({len(scan_universe)} symbols)"):
        with st.spinner("Scanning…"):
            results, errors = run_scan(fyers, scan_universe)
            scan_df = pd.DataFrame(results)

        st.session_state["scan_df"] = scan_df
        st.session_state["scan_errors"] = errors

        if errors:
            st.warning(f"{len(errors)} of {len(scan_universe)} symbols failed or were skipped.")

    if "scan_df" in st.session_state:
        df = st.session_state["scan_df"]

        if df.empty:
            st.error("Scan returned no usable results. Expand the error log below.")
        else:
            sorted_df = df.sort_values("AI Score", ascending=False)
            st.dataframe(sorted_df.style.applymap(_color_code), use_container_width=True, height=500)
            st.bar_chart(df.set_index("Symbol")["AI Score"])

            st.download_button(
                "📥 Download as Excel",
                data=to_excel_bytes(sorted_df),
                file_name=f"nse_scan_{datetime.today().strftime('%Y%m%d_%H%M')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

        if st.session_state.get("scan_errors"):
            with st.expander(f"⚠️ Errors / skipped symbols ({len(st.session_state['scan_errors'])})"):
                st.text("\n".join(st.session_state["scan_errors"][:200]))
                if len(st.session_state["scan_errors"]) > 200:
                    st.caption(f"...and {len(st.session_state['scan_errors']) - 200} more.")


# Fyers ఆబ్జెక్ట్‌ను ఇక్కడ పాస్ చేయండి
# show_scanner(fyers)
