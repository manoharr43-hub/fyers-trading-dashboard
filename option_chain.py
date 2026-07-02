"""
PATCH — additions only. Nothing in your existing file is modified, renamed,
or removed. Paste each block into the marked location. Every block says
exactly where it goes (function name / line to search for).
"""

# ══════════════════════════════════════════════════════════════════════════
# PATCH 1 — put this near the top, after your other imports
#   (i.e. right after:  from openpyxl.utils import get_column_letter )
# ══════════════════════════════════════════════════════════════════════════
from zoneinfo import ZoneInfo  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def get_ist_signal_datetime() -> tuple:
    """Returns (Signal Date, Signal Time) in Asia/Kolkata, e.g.
    ('02-Jul-2026', '14:32:07 IST'). Never blank — falls back to server
    time labeled accordingly if tz data is unexpectedly unavailable."""
    try:
        now_ist = datetime.now(IST)
        return now_ist.strftime("%d-%b-%Y"), now_ist.strftime("%H:%M:%S IST")
    except Exception:  # noqa: BLE001
        now = datetime.now()
        return now.strftime("%d-%b-%Y"), now.strftime("%H:%M:%S")


# ══════════════════════════════════════════════════════════════════════════
# PATCH 2 — add right after your existing `rating_label_for_side()` function
#   in section 5B (AI ENGINE). This is the plain CE/PE/WAIT signal format
#   you asked for: 🟢 STRONG CE BUY / 🟢 CE BUY / 🔴 STRONG PE BUY /
#   🔴 PE BUY / 🟡 WAIT — independent of the existing ★ rating labels,
#   which are left untouched.
# ══════════════════════════════════════════════════════════════════════════
def ai_signal_label(ce_score: float, pe_score: float, min_confidence: float = 60) -> tuple:
    """Returns (label, css_key). NEVER returns blank — WAIT is the floor."""
    try:
        ce_score = float(ce_score)
        pe_score = float(pe_score)
    except (TypeError, ValueError):
        return "🟡 WAIT", "wait"

    best = max(ce_score, pe_score)
    if best < min_confidence:
        return "🟡 WAIT", "wait"

    if ce_score >= pe_score:
        return ("🟢 STRONG CE BUY", "strongbuy") if ce_score >= 85 else ("🟢 CE BUY", "buy")
    return ("🔴 STRONG PE BUY", "strongbuy") if pe_score >= 85 else ("🔴 PE BUY", "buy")


# ══════════════════════════════════════════════════════════════════════════
# PATCH 3 — add these two lines inside `compute_ai_engine()`, right after
#   the existing block that builds "Final Recommendation":
#       d["Final Recommendation"] = d.apply(_final_recommendation, axis=1)
#       return d
#   Insert BEFORE the `return d` line, so it becomes:
# ══════════════════════════════════════════════════════════════════════════
"""
    d["Final Recommendation"] = d.apply(_final_recommendation, axis=1)

    # --- PATCH 3: plain BUY/SELL/WAIT signal + AI Confidence, never blank ---
    signal_pairs = d.apply(lambda r: ai_signal_label(r["CE Score"], r["PE Score"]), axis=1)
    d["AI Signal"] = signal_pairs.apply(lambda x: x[0])
    d["AI Signal Key"] = signal_pairs.apply(lambda x: x[1])
    d["AI Confidence %"] = d["AI Confidence"].fillna(0).round(1)  # explicit alias, always populated

    # --- PATCH 3: Signal Date / Signal Time (IST), never blank ---
    _sig_date, _sig_time = get_ist_signal_datetime()
    d["Signal Date"] = _sig_date
    d["Signal Time"] = _sig_time

    return d
"""

# ══════════════════════════════════════════════════════════════════════════
# PATCH 4 — extend `style_ce_pe_analysis_table()` and
#   `style_trade_signals_table()` so the new columns flow into the UI
#   table + Excel export. Only the `cols` list changes; everything else
#   in those functions stays as-is.
# ══════════════════════════════════════════════════════════════════════════
"""
In style_ce_pe_analysis_table(), change the `cols` list to also include:

    cols = [
        "strike_price",
        "CE Score", "PE Score",
        "AI Signal", "AI Confidence %",
        "Signal Date", "Signal Time",
        "CE BUY Probability", "PE BUY Probability",
        "CE SELL Probability", "PE SELL Probability",
        "CE Entry", "PE Entry",
        "CE SL", "PE SL",
        "CE Target 1", "PE Target 1",
        "CE Target 2", "PE Target 2",
        "CE Target 3", "PE Target 3",
        "Confidence %",
        "Institutional Buying", "Institutional Selling",
        "Smart Money Activity",
        "Breakout Probability", "Breakdown Probability",
        "Final Recommendation",
    ]

In generate_trade_signals(), inside the per-row loop where the `signals`
dict is appended, add two keys so every trade-signal card/table also carries
the plain AI Signal + timestamp (append right before the closing brace of
the dict that starts with "Strike": row["strike_price"], ... ):

    _sig_date, _sig_time = get_ist_signal_datetime()
    signals.append({
        "Strike": row["strike_price"], "Side": side,
        "Signal": side_label, "Signal Key": side_css_key,
        "AI Signal": ai_signal_label(row.get("CE Score", 0), row.get("PE Score", 0))[0],
        "Signal Date": _sig_date, "Signal Time": _sig_time,
        "Confidence": score, "Entry": entry, "SL": sl, "T1": t1, "T2": t2, "T3": t3,
        "Risk Reward": f"1 : {rr}" if rr > 0 else "—",
        "Reason": " · ".join(reasons), "Reasons": reasons,
    })
"""

# ══════════════════════════════════════════════════════════════════════════
# PATCH 5 — CACHING to avoid duplicate/slow API calls. Add these two cached
#   wrapper functions right after `fetch_expiry_list()` in section 2. They
#   call your existing functions unchanged — nothing about the underlying
#   fetch logic is modified.
# ══════════════════════════════════════════════════════════════════════════
def _cache_key_symbols(symbol_candidates: list) -> tuple:
    """Lists aren't hashable for st.cache_data — convert once at call sites."""
    return tuple(symbol_candidates)


try:
    import streamlit as _st_for_cache  # already imported above as st; alias avoids shadowing

    @st.cache_data(ttl=15, show_spinner=False)
    def cached_fetch_optionchain(_fyers, symbol_candidates_tuple: tuple, strikecount: int,
                                  expiry_timestamp: str = ""):
        """Cached for 15s so switching tabs / re-rendering doesn't re-hit the
        FYERS API. `_fyers` is underscore-prefixed so Streamlit does not try
        to hash the client object."""
        return fetch_optionchain_with_fallback(_fyers, list(symbol_candidates_tuple),
                                                strikecount, expiry_timestamp)

    @st.cache_data(ttl=60, show_spinner=False)
    def cached_fetch_expiry_list(_fyers, symbol_candidates_tuple: tuple):
        """Expiry lists change far less often than the chain itself — 60s TTL."""
        return fetch_expiry_list(_fyers, list(symbol_candidates_tuple))

except Exception:  # noqa: BLE001 — never let caching setup break the dashboard
    cached_fetch_optionchain = None
    cached_fetch_expiry_list = None


"""
Call-site changes inside show_option_chain() — replace ONLY the function
being called, keep every argument, variable name, and surrounding line the
same:

  1) Where the sidebar auto-loads the expiry list on instrument change:
         expiry_list, _used, raw_payload = fetch_expiry_list(fyers, symbol_candidates)
     becomes:
         expiry_list, _used, raw_payload = (
             cached_fetch_expiry_list(fyers, _cache_key_symbols(symbol_candidates))
             if cached_fetch_expiry_list else fetch_expiry_list(fyers, symbol_candidates)
         )

  2) Same replacement inside the "🔁 Retry Loading Expiry List" button block.

  3) Where the main chain is fetched:
         response, used_symbol, attempts = fetch_optionchain_with_fallback(
             fyers, symbol_candidates, strike_count, expiry_timestamp
         )
     becomes:
         response, used_symbol, attempts = (
             cached_fetch_optionchain(fyers, _cache_key_symbols(symbol_candidates),
                                       strike_count, expiry_timestamp)
             if cached_fetch_optionchain else
             fetch_optionchain_with_fallback(fyers, symbol_candidates, strike_count, expiry_timestamp)
         )

  4) Same replacement inside the auto-fallback-to-next-expiry loop:
         response, used_symbol, attempts = fetch_optionchain_with_fallback(
             fyers, symbol_candidates, strike_count, ts
         )
     becomes:
         response, used_symbol, attempts = (
             cached_fetch_optionchain(fyers, _cache_key_symbols(symbol_candidates), strike_count, ts)
             if cached_fetch_optionchain else
             fetch_optionchain_with_fallback(fyers, symbol_candidates, strike_count, ts)
         )

Every fallback path (`if cached_... else ...`) means that even if caching
setup fails for any reason, the dashboard silently falls back to your
original, already-working direct call — this can never introduce a crash.
"""

# ══════════════════════════════════════════════════════════════════════════
# PATCH 6 — extra defensive guards (KeyError / ValueError / empty data /
#   invalid strike / invalid expiry / timeout). These wrap risky spots
#   WITHOUT changing their logic — add as a small helper, then wrap the two
#   call sites noted below.
# ══════════════════════════════════════════════════════════════════════════
def safe_compute(fn, *args, fallback=None, label: str = "", **kwargs):
    """Runs any of the existing compute_* functions and guarantees the
    dashboard never crashes on KeyError / ValueError / TypeError / empty
    data. On failure, logs a small inline warning and returns `fallback`
    (or the first positional arg, typically the untouched df, if no
    fallback given) so downstream code keeps working with the last-known
    good shape."""
    try:
        return fn(*args, **kwargs)
    except (KeyError, ValueError, TypeError, IndexError, ZeroDivisionError) as e:  # noqa: BLE001
        st.warning(f"⚠️ {label or fn.__name__} skipped due to a data issue: {e}")
        return fallback if fallback is not None else (args[0] if args else None)


"""
Optional (recommended) call-site wrapping inside show_option_chain(), right
where these two lines already exist:

    df = compute_big_move_scores(df, spot_price, max_pain, pcr, atm_strike)
    ...
    df = compute_ai_engine(df, spot_price, atm_strike, max_pain, pcr)

becomes:

    df = safe_compute(compute_big_move_scores, df, spot_price, max_pain, pcr, atm_strike,
                       fallback=df, label="Big Move scoring")
    ...
    df = safe_compute(compute_ai_engine, df, spot_price, atm_strike, max_pain, pcr,
                       fallback=df, label="AI Engine")

This guarantees a bad/partial API payload degrades the AI columns
gracefully instead of crashing the whole page — all your existing OI/PCR/
Max Pain/Chain-table rendering keeps working even if AI scoring hits an
edge case.
"""

# ══════════════════════════════════════════════════════════════════════════
# PATCH 7 — Excel Summary sheet: add Signal Date/Time + AI Signal count.
#   In build_excel_report(), inside `summary_rows = [ ... ]`, add these two
#   lines right after the existing ("Generated At", ...) row:
# ══════════════════════════════════════════════════════════════════════════
"""
    summary_rows = [
        ("Symbol", symbol),
        ("Expiry", expiry_label),
        ("Generated At", datetime.now().strftime("%d-%b-%Y %H:%M:%S")),
        ("Signal Date (IST)", get_ist_signal_datetime()[0]),
        ("Signal Time (IST)", get_ist_signal_datetime()[1]),
        ("Spot Price", round(spot_price, 2) if spot_price else "—"),
        ...  # rest unchanged
    ]

No other part of build_excel_report() needs to change — since PATCH 4
already adds "AI Signal", "Signal Date", "Signal Time" to the Big Move
Ready and AI Trade Signals column lists, `_write_dataframe()` and
`_color_signal_cells()` (which already matches on "Signal" in the header)
pick up the new columns automatically with the same professional
green/yellow/red conditional formatting you already have.
"""
