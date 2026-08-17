# PATCH FILE: Apply these fixes to option_chain.py
# This file documents critical bug fixes and patches

# ══════════════════════════════════════════════════════════════════════════
# FIX #1: Technical Reasons Accumulation Bug (Line ~1470)
# ══════════════════════════════════════════════════════════════════════════

def generate_trade_signal(df_dict: dict[str, pd.DataFrame], spot: float, mss: dict[str, dict],
                          fyers_available: bool) -> Optional[TradeSignal]:
    """Generate a multi-timeframe confirmed trade signal. Returns None if FYERS is unavailable."""
    
    if not fyers_available:
        logger.warning("FYERS not available - cannot generate trade signal")
        return None
    
    if not df_dict or not any(df_dict.values()):
        return None
    
    # Get 5M as primary timeframe for signal
    df_5m = df_dict.get("5M")
    if df_5m is None or df_5m.empty or len(df_5m) < 10:
        return None
    
    # Analyze 5M technical setup
    current_close = float(df_5m["close"].iloc[-1])
    current_high = float(df_5m["high"].iloc[-1])
    current_low = float(df_5m["low"].iloc[-1])
    current_rsi = float(df_5m["rsi"].iloc[-1]) if "rsi" in df_5m.columns else 50.0
    current_ema_9 = float(df_5m["ema_9"].iloc[-1]) if "ema_9" in df_5m.columns else current_close
    current_ema_21 = float(df_5m["ema_21"].iloc[-1]) if "ema_21" in df_5m.columns else current_close
    current_macd = float(df_5m["macd"].iloc[-1]) if "macd" in df_5m.columns else 0.0
    current_macd_hist = float(df_5m["macd_hist"].iloc[-1]) if "macd_hist" in df_5m.columns else 0.0
    current_rvol = float(df_5m["rvol"].iloc[-1]) if "rvol" in df_5m.columns else 1.0
    
    # Detect structure
    levels_5m = detect_structure_levels(df_5m)
    resistance = levels_5m.get("resistance", current_close)
    support = levels_5m.get("support", current_close)
    
    signal_type = "HOLD"
    confidence_score = 0.0
    probability_score = 50.0
    
    # FIX: Use separate reason lists for BUY and SELL
    buy_reasons = []
    sell_reasons = []
    confirmed_tfs = []
    
    # ─── BUY Signal Logic ───
    buy_score = 0.0
    
    # Condition 1: Price above 9 EMA
    if current_close > current_ema_9:
        buy_score += 25
        buy_reasons.append("Price > EMA 9")
    
    # Condition 2: 9 EMA > 21 EMA (trend)
    if current_ema_9 > current_ema_21:
        buy_score += 20
        buy_reasons.append("EMA 9 > EMA 21")
    
    # Condition 3: MACD above zero line and histogram positive
    if current_macd > 0 and current_macd_hist > 0:
        buy_score += 20
        buy_reasons.append("MACD bullish")
    
    # Condition 4: RSI not overbought but bullish (30-70)
    if 40 <= current_rsi <= 70:
        buy_score += 15
        buy_reasons.append(f"RSI {current_rsi:.0f} (bullish zone)")
    
    # Condition 5: High volume
    if current_rvol > 1.2:
        buy_score += 10
        buy_reasons.append("High volume")
    
    # Condition 6: MSS confirmation
    if mss.get("5M", {}).get("mss") and mss["5M"].get("direction") == "UP":
        buy_score += 15
        buy_reasons.append("MSS confirmed (UP)")
        confirmed_tfs.append("5M")
    
    # ─── SELL Signal Logic ───
    sell_score = 0.0
    
    # Condition 1: Price below 9 EMA
    if current_close < current_ema_9:
        sell_score += 25
        sell_reasons.append("Price < EMA 9")
    
    # Condition 2: 9 EMA < 21 EMA (downtrend)
    if current_ema_9 < current_ema_21:
        sell_score += 20
        sell_reasons.append("EMA 9 < EMA 21")
    
    # Condition 3: MACD below zero line and histogram negative
    if current_macd < 0 and current_macd_hist < 0:
        sell_score += 20
        sell_reasons.append("MACD bearish")
    
    # Condition 4: RSI not oversold but bearish (30-70)
    if 30 <= current_rsi <= 60:
        sell_score += 15
        sell_reasons.append(f"RSI {current_rsi:.0f} (bearish zone)")
    
    # Condition 5: High volume
    if current_rvol > 1.2:
        sell_score += 10
        sell_reasons.append("High volume")
    
    # Condition 6: MSS confirmation
    if mss.get("5M", {}).get("mss") and mss["5M"].get("direction") == "DOWN":
        sell_score += 15
        sell_reasons.append("MSS confirmed (DOWN)")
        confirmed_tfs.append("5M")
    
    # Determine signal - use appropriate reason list
    technical_reasons = []
    if buy_score > sell_score and buy_score >= 60:
        signal_type = "BUY"
        confidence_score = min(buy_score, 100.0)
        probability_score = 50.0 + (buy_score / 2)
        technical_reasons = buy_reasons  # Use BUY reasons only
    elif sell_score > buy_score and sell_score >= 60:
        signal_type = "SELL"
        confidence_score = min(sell_score, 100.0)
        probability_score = 50.0 + (sell_score / 2)
        technical_reasons = sell_reasons  # Use SELL reasons only
    else:
        signal_type = "HOLD"
        confidence_score = max(buy_score, sell_score)
        probability_score = 50.0
        technical_reasons = ["Inconclusive signals"]
    
    # Calculate entry, SL, and targets
    if signal_type == "BUY":
        entry = current_close
        stop_loss = support * 0.995  # 0.5% below support
        range_val = entry - stop_loss
        target_1 = entry + range_val  # 1:1 RR
        target_2 = entry + (range_val * 1.5)  # 1.5:1 RR
        target_3 = entry + (range_val * 2.0)  # 2:1 RR
    elif signal_type == "SELL":
        entry = current_close
        stop_loss = resistance * 1.005  # 0.5% above resistance
        range_val = stop_loss - entry
        target_1 = entry - range_val
        target_2 = entry - (range_val * 1.5)
        target_3 = entry - (range_val * 2.0)
    else:  # HOLD
        entry = current_close
        stop_loss = support
        target_1 = (resistance + entry) / 2
        target_2 = resistance
        target_3 = resistance * 1.01
    
    risk_reward = abs(entry - target_1) / abs(entry - stop_loss) if entry != stop_loss else 1.0
    
    return TradeSignal(
        signal=signal_type,
        entry=entry,
        stop_loss=stop_loss,
        target_1=target_1,
        target_2=target_2,
        target_3=target_3,
        risk_reward_ratio=risk_reward,
        probability=min(probability_score, 100.0),
        confidence=confidence_score,
        confirmation_timeframes=confirmed_tfs if confirmed_tfs else ["5M"],
        technical_reasons=technical_reasons,
    )


# ══════════════════════════════════════════════════════════════════════════
# FIX #2: Unsafe DataFrame Access (Line ~1735)
# ══════════════════════════════════════════════════════════════════════════

def _do_fetch_and_process(cfg: dict, fyers: Any = None) -> Optional[dict]:
    """Full fetch -> parse -> validate -> analytics pipeline."""
    preferred_expiry = st.session_state.get("oc_selected_expiry", "")
    stock_name = cfg["symbol"] if not cfg["is_index"] else ""
    fetch_result = fetch_chain_unified(
        fyers, cfg["symbol"], cfg["is_index"], stock_name, preferred_expiry, cfg["strike_count"],
    )
    if cfg["debug_mode"]:
        st.write("**Fetch result:**", fetch_result.get("ok"), fetch_result.get("source"), fetch_result.get("error"))

    if not fetch_result.get("ok"):
        st.error(
            f"⚠️ Could not fetch option chain for **{cfg['symbol']}**: "
            f"{fetch_result.get('error', 'Unknown error.')} "
        )
        return None

    df_all: pd.DataFrame = fetch_result["df"]
    meta: dict = fetch_result["meta"]
    data_source: str = fetch_result.get("source", "UNKNOWN")

    if not validate_chain_df(df_all):
        st.error(
            f"⚠️ Received a response for **{cfg['symbol']}**, but it did not contain a usable "
            "option chain."
        )
        return None

    spot = meta["spot_price"]
    df = filter_strikes_around_atm(df_all, spot, cfg["strike_count"])
    if df.empty:
        df = df_all

    # FIX: Safe ATM strike calculation with validation
    expiry_label = meta["selected_expiry"]
    
    if spot and not df.empty:
        # Calculate closest to ATM
        try:
            atm_idx = (df["strike_price"] - spot).abs().argmin()
            atm_strike = float(df.iloc[atm_idx]["strike_price"])
        except (IndexError, KeyError) as e:
            logger.warning("Error calculating ATM strike: %s, using median", e)
            atm_strike = float(df["strike_price"].median())
    elif not df.empty:
        atm_strike = float(df["strike_price"].median())
    else:
        logger.error("No valid strike data available")
        return None

    df = add_greeks_columns(df, spot, expiry_label)
    df = classify_buildup(df)
    df = classify_moneyness(df, spot)
    df = compute_ai_scores(df, spot, atm_strike, calc_max_pain(df), calc_pcr(df))
    df = detect_institutional_smart_money(df)

    pcr = calc_pcr(df)
    max_pain = calc_max_pain(df)
    support, resistance = calc_support_resistance(df)
    max_oi = calc_max_oi(df)

    atm_iv = _atm_iv(df, spot)
    update_iv_history(cfg["symbol"], expiry_label, atm_iv)
    iv_rank, iv_percentile = compute_iv_rank_percentile(cfg["symbol"], expiry_label, atm_iv)

    gex_dex = compute_gex_dex(df, spot, cfg["lot_size"])

    oi_shift_notes = detect_oi_shift(cfg["symbol"], expiry_label, support, resistance)

    # Price Action Analysis (FYERS-dependent)
    price_action_data = None
    trade_signal = None
    if cfg["analyze_price_action"] and fyers is not None:
        fyers_symbol_candidates = (
            _fyers_index_candidates(cfg["symbol"]) if cfg["is_index"] else fyers_stock_symbol_candidates(stock_name)
        )
        fyers_symbol = fyers_symbol_candidates[0] if fyers_symbol_candidates else None
        
        if fyers_symbol:
            df_dict = {}
            for tf_name, tf_mins in TIMEFRAMES.items():
                df_tf = fetch_fyers_candles(fyers, fyers_symbol, tf_mins, count=100)
                df_dict[tf_name] = df_tf
            
            if any(df_dict.values()):
                # Add technical indicators to candles
                for tf_name in df_dict:
                    if df_dict[tf_name] is not None and not df_dict[tf_name].empty:
                        df_dict[tf_name] = add_technical_indicators(df_dict[tf_name])
                
                # Detect MSS and generate signal
                mss = detect_mss(df_dict)
                trade_signal = generate_trade_signal(df_dict, spot, mss, fyers is not None)
                
                price_action_data = {
                    "df_dict": df_dict,
                    "mss": mss,
                    "trade_signal": trade_signal,
                }

    return {
        "df": df, "meta": meta, "spot": spot, "atm_strike": atm_strike, "expiry_label": expiry_label,
        "pcr": pcr, "max_pain": max_pain, "support": support, "resistance": resistance, "max_oi": max_oi,
        "atm_iv": atm_iv, "iv_rank": iv_rank, "iv_percentile": iv_percentile, "gex_dex": gex_dex,
        "oi_shift_notes": oi_shift_notes, "data_source": data_source,
        "price_action_data": price_action_data, "trade_signal": trade_signal,
    }


# ══════════════════════════════════════════════════════════════════════════
# FIX #3: Cache Staleness (Line ~1592)
# ══════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=5, show_spinner=False)  # REDUCED: 15s → 5s for fresher data
def fetch_option_chain_raw(symbol: str, is_index: bool) -> dict:
    """Cached (5s TTL) raw NSE option-chain JSON fetch. NSE is used only as
    fallback for option chains when FYERS is unavailable."""
    session = get_nse_session()
    url = NSE_INDEX_CHAIN_URL if is_index else NSE_EQUITY_CHAIN_URL
    payload, error = fetch_json_with_retry(session, url, params={"symbol": symbol})
    if payload is None:
        return {"ok": False, "payload": None, "error": error or "No data returned."}
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, dict) or not records.get("data"):
        return {"ok": False, "payload": payload, "error": "Response had no option-chain records."}
    return {"ok": True, "payload": payload, "error": None}


# ══════════════════════════════════════════════════════════════════════════
# FIX #4: Input Validation in FYERS Functions
# ══════════════════════════════════════════════════════════════════════════

def fetch_fyers_candles(fyers: Any, symbol: str, timeframe_minutes: int, count: int = 100) -> Optional[pd.DataFrame]:
    """Fetches OHLCV candles from FYERS for a given timeframe. Returns None if unavailable."""
    if fyers is None:
        return None
    
    # FIX: Add input validation
    if not symbol or not isinstance(symbol, str):
        logger.error("Invalid symbol provided: %s", symbol)
        return None
    
    if timeframe_minutes <= 0:
        logger.error("Invalid timeframe_minutes: %d", timeframe_minutes)
        return None
    
    if count <= 0:
        logger.error("Invalid count: %d", count)
        return None

    resolution_map = {
        5: "5",
        15: "15",
        30: "30",
        60: "60",
        1440: "1D",
    }
    resolution = resolution_map.get(timeframe_minutes, str(timeframe_minutes))

    resp = _fyers_call_history(fyers, symbol, resolution, count)
    if not isinstance(resp, dict) or resp.get("s") != "ok":
        logger.warning("FYERS history returned non-ok status for %s: %s", symbol, resp.get("s") if resp else None)
        return None

    data = resp.get("candles", []) if isinstance(resp.get("candles"), list) else []
    if not data:
        logger.warning("FYERS history returned empty candles for %s", symbol)
        return None

    rows = []
    for candle in data:
        if not isinstance(candle, list) or len(candle) < 5:
            continue
        try:
            rows.append({
                "timestamp": int(candle[0]) if len(candle) > 0 else 0,
                "open": float(candle[1]) if len(candle) > 1 else 0.0,
                "high": float(candle[2]) if len(candle) > 2 else 0.0,
                "low": float(candle[3]) if len(candle) > 3 else 0.0,
                "close": float(candle[4]) if len(candle) > 4 else 0.0,
                "volume": float(candle[5]) if len(candle) > 5 else 0.0,
            })
        except (TypeError, ValueError, IndexError):
            continue

    if not rows:
        return None

    df = pd.DataFrame(rows)
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


# ══════════════════════════════════════════════════════════════════════════
# FIX #5: Enhanced Validation in Greeks Calculation
# ══════════════════════════════════════════════════════════════════════════

def add_greeks_columns(df: pd.DataFrame, spot: float, expiry_label: str,
                        r: float = RISK_FREE_RATE) -> pd.DataFrame:
    d = df.copy()
    if d.empty:
        for col in ("ce_delta", "ce_gamma", "ce_theta", "ce_vega",
                    "pe_delta", "pe_gamma", "pe_theta", "pe_vega"):
            d[col] = 0.0
        return d

    # FIX: Enhanced expiry validation
    t_years = parse_days_to_expiry(expiry_label) / 365.0
    
    if t_years <= 0:
        logger.warning("Invalid expiry date calculated: %s. Using default 7 days", expiry_label)
        t_years = 7.0 / 365.0

    ce_g = d.apply(
        lambda row: bs_greeks(spot, row["strike_price"], t_years, r, row.get("ce_iv", 0) / 100.0 if row.get("ce_iv", 0) > 0 else 0.20, True),
        axis=1,
    )
    pe_g = d.apply(
        lambda row: bs_greeks(spot, row["strike_price"], t_years, r, row.get("pe_iv", 0) / 100.0 if row.get("pe_iv", 0) > 0 else 0.20, False),
        axis=1,
    )
    for key in ("delta", "gamma", "theta", "vega"):
        d[f"ce_{key}"] = ce_g.apply(lambda x: x[key])
        d[f"pe_{key}"] = pe_g.apply(lambda x: x[key])
    return d


# ══════════════════════════════════════════════════════════════════════════
# SUMMARY OF FIXES
# ══════════════════════════════════════════════════════════════════════════
"""
FIX #1 (CRITICAL): Technical Reasons Accumulation
  - Separated buy_reasons and sell_reasons lists
  - Prevents mixed signal reasons in output
  - Impact: Accurate signal reasoning

FIX #2 (CRITICAL): Unsafe DataFrame Access  
  - Added try-except for .argmin() and .iloc access
  - Fallback to median if ATM calculation fails
  - Impact: Prevents crashes on edge cases

FIX #3 (MEDIUM): Cache Staleness
  - Reduced TTL from 15s to 5s
  - Better data freshness for fast-moving options
  - Impact: More up-to-date pricing

FIX #4 (MEDIUM): Input Validation
  - Added symbol, timeframe, count validation
  - Logs clear error messages
  - Impact: Cleaner error handling

FIX #5 (MEDIUM): Greeks Calculation
  - Added expiry validation with fallback
  - Default IV for missing data (20%)
  - Impact: Prevents silent failures in Greeks
"""
