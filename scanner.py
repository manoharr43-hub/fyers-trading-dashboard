# ══════════════════════════════════════════════════════════════════════════
# NSE AI PRO V13 — PATCH RESULT
# ──────────────────────────────────────────────────────────────────────────
# This file contains the THREE additions to apply to your existing
# nse_ai_scanner.py.  Each section is clearly delimited.
#
# APPLY IN ORDER:
#   A) Paste SECTION A (two functions) into nse_ai_scanner.py, right after
#      run_fo_cisd_scan() and before run_premarket_scan().
#
#   B) In show_scanner() find the existing st.tabs() call and replace it
#      with SECTION B (one line).
#
#   C) Paste SECTION C (the tab UI block) inside show_scanner(), right
#      after the closing lines of "with tab_fo_cisd:" and before the
#      final scan-errors expander.
# ══════════════════════════════════════════════════════════════════════════


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION A — Two new functions (paste after run_fo_cisd_scan)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _fetch_fo_15min_cisd_signal(fyers, symbol: str):
    """
    Fetches the last INTRADAY_CISD_LOOKBACK_DAYS days of 15-minute candles
    for a single F&O equity symbol, detects a CISD shift on completed
    candles only, and applies the strict 7-condition BUY / SELL filter.

    Signal Date and Signal Time always come from the candle whose CLOSE
    confirmed the CISD shift — never from datetime.now() or scan time.

    Returns (row_dict, None) when a qualifying signal is found,
            (None, None)   when there is no live signal (not an error), or
            (None, str)    on a data/API error.
    """
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"

    date_from = (datetime.today() - timedelta(days=INTRADAY_CISD_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to   = datetime.today().strftime("%Y-%m-%d")

    resp, err = _safe_history(fyers, {
        "symbol": symbol, "resolution": "15", "date_format": "1",
        "range_from": date_from, "range_to": date_to, "cont_flag": "1",
    })
    if err:
        return None, f"{symbol}: {err}"

    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 30:
        return None, None  # too little intraday history yet — not an error

    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(
            pd.to_numeric, errors="coerce"
        )
        df = df.dropna(subset=["Open", "High", "Low", "Close"])

        # ── Use only COMPLETED candles — drop the last row if it is still
        # open (i.e. its timestamp is within the current 15-min bar).
        now_ist = _now_ist()
        # A 15-min candle whose open-time equals the current 15-min slot
        # has NOT yet closed. We compare by truncating both sides to the
        # nearest 15-min boundary.
        current_bar_open = now_ist.replace(
            minute=(now_ist.minute // 15) * 15, second=0, microsecond=0
        )
        last_candle_time = df["Time"].iloc[-1]
        # tz-aware comparison
        if hasattr(last_candle_time, "tzinfo") and last_candle_time.tzinfo is None:
            import pytz
            last_candle_time = last_candle_time.tz_localize("Asia/Kolkata")
        current_bar_open_aware = now_ist  # already tz-aware from _now_ist()
        # Drop the in-progress bar
        if last_candle_time >= current_bar_open_aware.replace(
            minute=(now_ist.minute // 15) * 15, second=0, microsecond=0
        ):
            df = df.iloc[:-1]

        if len(df) < 30:
            return None, None

        # ── Detect CISD / SMC on completed candles ─────────────────────
        smc_structure, cisd_signal, event_ts = _calculate_smc_and_cisd(df)
        if cisd_signal == "None":
            return None, None  # no CISD event — normal, not an error

        # ── Compute indicators (all on completed candles) ───────────────
        close  = df["Close"]
        volume = df["Volume"]

        ema20 = close.ewm(span=20, adjust=False).mean()
        ema50 = close.ewm(span=50, adjust=False).mean()
        ema20_last = float(ema20.iloc[-1])
        ema50_last = float(ema50.iloc[-1])

        macd_line, signal_line, macd_hist = calculate_macd(close)
        macd_bullish = bool(macd_line.iloc[-1] > signal_line.iloc[-1])
        macd_str = "🟢 Bullish" if macd_bullish else "🔴 Bearish"

        supertrend_label, supertrend_bullish, _ = calculate_supertrend(df)

        # For intraday candles: use a simple rolling VWAP over the window
        # (same approximation used in add_indicators for intraday mode).
        typical = (df["High"] + df["Low"] + df["Close"]) / 3
        vol_sum = volume.rolling(20).sum()
        vwap_series = (typical * volume).rolling(20).sum() / vol_sum.replace(0, np.nan)
        vwap_val = float(vwap_series.iloc[-1]) if not np.isnan(vwap_series.iloc[-1]) else None

        vol_avg20 = volume.tail(20).mean()
        last_volume = float(volume.iloc[-1])
        rvol_raw = round(last_volume / vol_avg20, 2) if vol_avg20 > 0 else 0.0

        rsi_val = round(float(calculate_rsi(close).iloc[-1]), 1)

        last_close = float(close.iloc[-1])
        atr = float(calculate_atr(df).iloc[-1])
        if pd.isna(atr) or atr <= 0:
            atr = last_close * 0.005

        resistance = float(df["High"].rolling(20).max().shift(1).iloc[-1])
        support    = float(df["Low"].rolling(20).min().shift(1).iloc[-1])
        if last_close > resistance:
            breakout = "📈 Bullish"
        elif last_close < support:
            breakout = "📉 Bearish"
        else:
            breakout = "NO"

        pattern = detect_chart_pattern(df)

        gap_pct = 0.0
        if len(df) >= 2 and pd.notna(df["Close"].iloc[-2]) and df["Close"].iloc[-2] != 0:
            gap_pct = ((df["Open"].iloc[-1] - df["Close"].iloc[-2]) / df["Close"].iloc[-2]) * 100

        is_bullish_cisd = "Bullish" in cisd_signal
        vwap_above      = vwap_val is not None and last_close > vwap_val
        volume_above    = vol_avg20 > 0 and last_volume > vol_avg20

        # ── Strict 7-condition BUY / SELL filter ───────────────────────
        # BUY only when ALL of:
        #   1. Bullish CISD confirmed
        #   2. EMA20 > EMA50
        #   3. MACD Bullish
        #   4. Supertrend Bullish
        #   5. Price above VWAP
        #   6. RVOL > 1.5
        #   7. Volume > Average Volume
        #
        # SELL only when ALL opposite conditions are confirmed.
        # Otherwise: WAIT.

        bull_conditions = {
            "Bullish CISD":      is_bullish_cisd,
            "EMA20 > EMA50":     ema20_last > ema50_last,
            "MACD Bullish":      macd_bullish,
            "Supertrend Bullish": supertrend_bullish is True,
            "VWAP Above":        vwap_above,
            "RVOL > 1.5":        rvol_raw > 1.5,
            "Volume > Avg Vol":  volume_above,
        }
        bear_conditions = {
            "Bearish CISD":      not is_bullish_cisd,
            "EMA20 < EMA50":     ema20_last < ema50_last,
            "MACD Bearish":      not macd_bullish,
            "Supertrend Bearish": supertrend_bullish is False,
            "VWAP Below":        vwap_val is not None and last_close < vwap_val,
            "RVOL > 1.5":        rvol_raw > 1.5,
            "Volume > Avg Vol":  volume_above,
        }

        bull_count = sum(bull_conditions.values())
        bear_count = sum(bear_conditions.values())

        if bull_count == 7:
            trade_signal  = "🟢 BUY"
            active_checks = bull_conditions
            confirmed     = bull_count
        elif bear_count == 7:
            trade_signal  = "🔴 SELL"
            active_checks = bear_conditions
            confirmed     = bear_count
        else:
            trade_signal  = "🟡 WAIT"
            # Show whichever direction is closer to confirming
            active_checks = bull_conditions if bull_count >= bear_count else bear_conditions
            confirmed     = max(bull_count, bear_count)

        # ── Signal Strength (star rating) ──────────────────────────────
        if confirmed == 7:
            signal_strength = "★★★★★ Very Strong"
        elif confirmed >= 6:
            signal_strength = "★★★★ Strong"
        elif confirmed >= 5:
            signal_strength = "★★★ Medium"
        elif confirmed >= 4:
            signal_strength = "★★ Weak"
        else:
            signal_strength = "★ Very Weak"

        # ── Entry / SL / Targets ────────────────────────────────────────
        entry = round(last_close, 2)
        if trade_signal == "🟢 BUY":
            sl = round(entry - 1.0 * atr, 2)
            t1 = round(entry + 1.0 * atr, 2)
            t2 = round(entry + 1.8 * atr, 2)
            t3 = round(entry + 2.6 * atr, 2)
            holding_time = "15–60 Minutes"
            trade_type   = "Intraday BUY"
        elif trade_signal == "🔴 SELL":
            sl = round(entry + 1.0 * atr, 2)
            t1 = round(entry - 1.0 * atr, 2)
            t2 = round(entry - 1.8 * atr, 2)
            t3 = round(entry - 2.6 * atr, 2)
            holding_time = "15–60 Minutes"
            trade_type   = "Intraday SELL"
        else:
            sl = round(entry - 1.0 * atr, 2)
            t1 = t2 = t3 = entry
            holding_time = "Wait"
            trade_type   = "No Trade"

        risk   = abs(entry - sl)
        reward = abs(t1 - entry)
        rr     = round(reward / risk, 2) if risk > 0 else 0.0

        # Confidence: based on confirmed-condition count + RVOL bonus
        confidence = round(min(95.0, max(30.0,
            35 + confirmed * 8 + min(rvol_raw, 3) * 3
        )), 1)

        # ── AI Score (lightweight, intraday-appropriate) ────────────────
        ai_score = round(min(max(
            50 + (is_bullish_cisd * 2 - 1) * 15
               + (ema20_last > ema50_last) * 8
               + (macd_bullish * 2 - 1) * 6
               + (vwap_above * 2 - 1) * 5
               + (rsi_val - 50) * 0.25
               + min(rvol_raw, 3) * 3,
            0), 100), 1)

        # ── Signal Date / Signal Time (candle-derived, never system clock)
        # event_ts = timestamp of the CISD-confirming completed candle.
        # This is a real 15-min intraday candle, so is_daily=False.
        signal_date_str, signal_time_str = (
            _format_signal_timestamp(event_ts, is_daily=False)
            if event_ts is not None
            else _candle_signal_timestamp(df, is_daily=False)
        )

        reason_parts = [k for k, v in active_checks.items() if v]
        reason_str   = ", ".join(reason_parts) if reason_parts else "Mixed signals"

        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")

        row = {
            "Signal Date":        signal_date_str,
            "Signal Time":        signal_time_str,
            "Stock":              stock_ticker,
            "LTP":                round(last_close, 2),
            "Signal":             trade_signal,
            "Signal Strength":    signal_strength,
            "Confidence %":       confidence,
            "AI Score":           ai_score,
            "Entry":              entry,
            "Stop Loss":          sl,
            "Target 1":           t1,
            "Target 2":           t2,
            "Target 3":           t3,
            "Risk Reward":        rr,
            "SMC Structure":      smc_structure,
            "CISD Signal":        cisd_signal,
            "XGBoost Trend":      "🟡 N/A (intraday)",
            "XGBoost Confidence": "—",
            "MTF Trend":          "—",
            "AI Trend":           "📈 Bullish" if ai_score >= 65 else ("📉 Bearish" if ai_score <= 40 else "➖ Neutral"),
            "RSI":                rsi_val,
            "MACD":               macd_str,
            "Supertrend":         supertrend_label,
            "VWAP":               round(vwap_val, 2) if vwap_val is not None else None,
            "Support":            round(support, 2)    if pd.notna(support)    else None,
            "Resistance":         round(resistance, 2) if pd.notna(resistance) else None,
            "Breakout":           breakout,
            "Pattern":            pattern,
            "RVOL":               _format_rvol_display(rvol_raw),
            "Volume":             int(last_volume),
            "Average Volume":     int(round(vol_avg20)),
            "Gap %":              f"{gap_pct:.2f}%",
            "Trend Strength":     signal_strength,
            "Trade Type":         trade_type,
            "Holding Time":       holding_time,
            "Reason":             reason_str,
        }
        return row, None

    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError) as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"
    except Exception as e:
        return None, f"{symbol}: unexpected error ({type(e).__name__})"


def run_fo_15min_cisd_scan(fyers, symbols: list):
    """
    Threaded, rate-limited scan of the F&O equity universe on 15-minute
    candles. Returns (results, errors, stats). Mirrors the same batch/
    concurrency/progress-bar pattern used by all other scanners in this file.
    """
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Scanning F&O 15-Min CISD 0 / {len(symbols)}")
    done = 0

    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(_fetch_fo_15min_cisd_signal, fyers, s): s
                for s in batch
            }
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{futures[future]}: worker error ({type(e).__name__})"
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(
                    done / len(symbols),
                    text=f"Scanning F&O 15-Min CISD {done} / {len(symbols)}"
                )
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)

    progress.empty()
    return results, errors, stats



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION B — Replace the st.tabs() call in show_scanner() with this line
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    tab_scanner, tab_intraday, tab_swing, tab_fo, \
        tab_intraday_cisd, tab_fo_cisd, tab_golden_death, tab_premarket, tab_fo_15min = st.tabs(
        ["📊 Full Scanner", "⚡ Intraday Scanner", "📈 Swing Trade Scanner", "🏛️ F&O Stocks Scanner",
         "🕐 Intraday CISD Signals", "🎯 F&O CISD Scanner", "✝️ Swing Trading (Golden/Death Cross)",
         "🌅 Pre-Market Scanner", "📈 NSE F&O 15-Min CISD Scanner"]
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION C — Tab UI block (paste inside show_scanner after tab_fo_cisd block)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # ── NSE F&O 15-Minute CISD Scanner tab ─────────────────────────────
    with tab_fo_15min:
        st.caption(
            "Scans only F&O-permitted NSE equity stocks. Uses 15-minute completed candles only — "
            "the in-progress bar is always dropped before analysis.\n\n"
            "**BUY** requires all 7: Bullish CISD + EMA20>EMA50 + MACD Bullish + "
            "Supertrend Bullish + Price>VWAP + RVOL>1.5 + Volume>Avg. "
            "**SELL** requires all 7 opposite conditions. Everything else shows **WAIT**.\n\n"
            "Signal Date and Signal Time come from the candle whose close confirmed the CISD "
            "shift — never from the scan clock or download time."
        )
    
        fo15_symbols = load_nse_fo_stock_symbols()
        st.caption(f"Loaded {len(fo15_symbols)} F&O-permitted NSE stocks (indices and invalid symbols excluded).")
    
        if not fo15_symbols:
            st.warning("No F&O stock symbols loaded — check network access to public.fyers.in.")
        else:
            fo15_col1, fo15_col2 = st.columns([1, 2])
            with fo15_col1:
                fo15_limit = st.number_input(
                    "Limit F&O symbols (0 = all)",
                    min_value=0, max_value=len(fo15_symbols),
                    value=len(fo15_symbols), step=25, key="fo15_limit",
                    help="All F&O stocks (~180) scan in 2–4 minutes at the default concurrency.",
                )
            with fo15_col2:
                fo15_filter = st.selectbox(
                    "Signal Filter",
                    options=["All Signals", "BUY Only", "SELL Only", "WAIT Only"],
                    key="fo15_filter",
                )
    
            fo15_universe = fo15_symbols if fo15_limit == 0 else fo15_symbols[:fo15_limit]
    
            if st.button(
                f"📈 Run F&O 15-Min CISD Scan ({len(fo15_universe)} symbols)", key="fo15_run"
            ):
                with st.spinner("Scanning F&O stocks on 15-minute completed candles…"):
                    fo15_results, fo15_errors, fo15_stats = run_fo_15min_cisd_scan(fyers, fo15_universe)
                    fo15_df = pd.DataFrame(fo15_results)
    
                st.session_state["fo15_df"]     = fo15_df
                st.session_state["fo15_errors"] = fo15_errors
                st.session_state["fo15_stats"]  = fo15_stats
    
            if "fo15_stats" in st.session_state:
                _display_scan_summary(st.session_state["fo15_stats"])
    
            fo15_df = st.session_state.get("fo15_df")
            if fo15_df is not None and not fo15_df.empty:
    
                # Apply sidebar signal filter
                view = fo15_df.copy()
                try:
                    if fo15_filter == "BUY Only":
                        view = view[view["Signal"] == "🟢 BUY"]
                    elif fo15_filter == "SELL Only":
                        view = view[view["Signal"] == "🔴 SELL"]
                    elif fo15_filter == "WAIT Only":
                        view = view[view["Signal"] == "🟡 WAIT"]
                except (KeyError, TypeError):
                    pass
    
                if view.empty:
                    st.info(f"No stocks match the '{fo15_filter}' filter for this scan.")
                else:
                    # Sort by: BUY/SELL first (actionable), then by Confidence
                    priority = {"🟢 BUY": 0, "🔴 SELL": 1, "🟡 WAIT": 2}
                    view = view.copy()
                    view["_sort_key"] = view["Signal"].map(priority).fillna(3)
                    view = view.sort_values(["_sort_key", "Confidence %"], ascending=[True, False])
                    view = view.drop(columns=["_sort_key"])
    
                    st.caption(
                        f"Showing {len(view)} stocks "
                        f"({'BUY: ' + str((view['Signal']=='🟢 BUY').sum()) + ', ' if fo15_filter == 'All Signals' else ''}"
                        f"{'SELL: ' + str((view['Signal']=='🔴 SELL').sum()) + ', ' if fo15_filter == 'All Signals' else ''}"
                        f"{'WAIT: ' + str((view['Signal']=='🟡 WAIT').sum()) if fo15_filter == 'All Signals' else ''}"
                        f")."
                    )
                    st.dataframe(_style_dataframe(view), use_container_width=True, height=600)
    
                    st.download_button(
                        "📥 Download F&O 15-Min CISD Signals as Excel",
                        data=to_excel_bytes(view, "F&O 15Min CISD"),
                        file_name=f"nse_fo_15min_cisd_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key="dl_fo15",
                    )
            elif "fo15_df" in st.session_state:
                st.info("No CISD signals found for this scan. Market may be closed or no F&O stocks have an active 15-min CISD shift right now.")
            else:
                st.info("Run a scan above to see F&O 15-Min CISD signals.")
    
            if st.session_state.get("fo15_errors"):
                with st.expander(
                    f"⚠️ Skipped/failed symbols ({len(st.session_state['fo15_errors'])})"
                ):
                    st.caption(
                        "Showing up to 20 — most stocks are skipped for missing/invalid data, not app errors."
                    )
                    st.text("\\n".join(st.session_state["fo15_errors"][:20]))
