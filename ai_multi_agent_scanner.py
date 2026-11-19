"""
V18 audit patches -- drop-in replacements for ai_multi_agent_scanner.py
=======================================================================
Each section says WHAT to replace. Nothing here touches scanner.py.
Needs only names already imported in the main module (pd, np, st, sqlite3, time,
threading, uuid, requests, os, Path, datetime, IST, _memory_connect, etc.).

ONE-LINE EDITS (do these by hand)
---------------------------------
[E1] _refresh_live_signal_prices(), first line:
        if yf is None or not _is_market_hours():
     ->  if yf is None or not _is_tracking_window():

[E2] _render_live_dashboard_body(), first line:
        refresh_result = _refresh_live_signal_prices()
     ->  _ensure_live_tracker(); refresh_result = dict(_LIVE_TRACKER_STATUS)

[E3] _TTL_CACHE_MAX_ITEMS = 32   ->   _TTL_CACHE_MAX_ITEMS = 6

[E4] _advanced_big_movement_analysis(): replace the weak_up / weak_down lines with
        weak_up = broke_up and (body_ratio < BREAKOUT_BODY_MIN or close_location < 0.75 or rvol_base < BREAKOUT_RVOL_MIN)
        weak_down = broke_down and (body_ratio < BREAKOUT_BODY_MIN or close_location > 0.25 or rvol_base < BREAKOUT_RVOL_MIN)
     (constants defined below; they match _breakout_retest_confirmation)

[E5] Row-wise applies (main + show_ai_multi_agent_scanner):
        result["Direction Movement"] = result.apply(_direction_movement, axis=1)
     -> result["Direction Movement"] = [_direction_movement(r) for r in result.to_dict("records")]
        analyst = result.apply(_best_stock_analyst, axis=1, result_type="expand")
     -> analyst = _rowwise(result, _best_stock_analyst)
        new_early = result.apply(_early_signal_decision, axis=1, result_type="expand")
     -> new_early = _rowwise(result, _early_signal_decision)
     and in the _capture_* functions: `for _, row in result.iterrows():`
     -> `for row in result.to_dict("records"):`

[E6] _make_result_row(), right after `frames = payload.get("frames", {})`:
        frames = {tf: (f.tail(_ANALYSIS_TAIL[tf]).reset_index(drop=True)
                       if tf in _ANALYSIS_TAIL and isinstance(f, pd.DataFrame) else f)
                  for tf, f in frames.items()}
     (candle counts in reports are then capped at 600 for 5M/15M; >=30 checks unchanged)

[E7] _capture_volume_expansion_events(), after `stamp = ...` fallback, add:
        age_min = (pd.Timestamp.now(tz=IST) - stamp).total_seconds() / 60.0 if stamp is not None else 9e9
        if age_min > TIMEFRAMES[tf][1] * 2 + 5:
            continue

[E8] _build_excel(): replace the 7 dash_rows between "TOTAL" and the blank row with
        dash_rows = [
            ["NSE AI PRO V18 — ADVANCED BIG MOVEMENT DASHBOARD", ""],
            ["Generated (IST)", stamp.strftime("%Y-%m-%d %H:%M:%S IST")],
            ["Total Stocks Scanned", int(len(result))],
            *_dashboard_signal_counts(result),
            ["", ""],
            ["TOP QUALIFIED STOCKS", ""],
        ]
     and replace `wb._sheets = [wb["Dashboard"], wb["AI Scanner"]]` with
        wb.move_sheet("Dashboard", offset=-wb.index(wb["Dashboard"]))

[E9] show_ai_multi_agent_scanner(): call `_expire_stale_jobs()` before the first
     `job_id = st.session_state.get("ma_scan_job_id")`, and replace the loader calls with
        fo_symbols = _normalise_symbols(_cached_symbols("fo", load_fo_stocks))
        nse_symbols = _normalise_symbols(_cached_symbols("nse", load_nse_equity_symbols))
     (and likewise for the single-universe branches).
"""

import functools

BREAKOUT_BODY_MIN = 0.55
BREAKOUT_RVOL_MIN = 1.5
_ANALYSIS_TAIL = {"5M": 600, "15M": 600}


# ---------------------------------------------------------------- B1
def _cached_candles_are_fresh(frame, minutes, now=None):
    """Cache is fresh only until the NEXT candle could have closed (age < minutes)."""
    if frame is None or frame.empty or "Time" not in frame.columns:
        return False
    now = pd.Timestamp(now if now is not None else pd.Timestamp.now(tz=IST))
    now = now.tz_localize(IST) if now.tzinfo is None else now.tz_convert(IST)
    try:
        last = pd.Timestamp(frame["Time"].iloc[-1])
        last = last.tz_localize(IST) if last.tzinfo is None else last.tz_convert(IST)
        close_time = (last.normalize() + SESSION_CLOSE
                      if minutes == 60 and last.hour == 15 and last.minute == 15
                      else last + pd.Timedelta(minutes=minutes))
        open_time = now.normalize() + pd.Timedelta(hours=9, minutes=15)
        if open_time <= now < open_time + pd.Timedelta(minutes=minutes):
            previous_close = _last_session_close(now.normalize() + pd.Timedelta(hours=9), minutes=minutes)
            return close_time >= previous_close - pd.Timedelta(minutes=1)
        if minutes == 60 and pd.Timestamp("09:15").time() <= now.time() < pd.Timestamp("10:15").time():
            previous_close = _last_session_close(now.normalize() + pd.Timedelta(hours=9), minutes=60)
            return close_time >= previous_close - pd.Timedelta(minutes=1)
        if not _is_market_hours(now.to_pydatetime()):
            return close_time >= (_last_session_close(now, minutes=minutes) - pd.Timedelta(minutes=1))
        age = (now - close_time).total_seconds() / 60.0
        return -1.0 <= age < float(minutes)          # FIX: was minutes + 2
    except Exception:
        return False


# ---------------------------------------------------------------- B4 / P3
_EXTRA_INDEXES_DONE = False
_RETENTION_LAST = 0.0


def _ensure_extra_indexes():
    global _EXTRA_INDEXES_DONE
    if _EXTRA_INDEXES_DONE:
        return
    with _memory_connect() as conn:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_candles_tf_time ON candles(timeframe, candle_time)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_liq_captured ON liquidity_sweep_events(captured_at_ist)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vol_captured ON volume_expansion_events(captured_at_ist)")
    _EXTRA_INDEXES_DONE = True


def _retention_cleanup_if_due(min_interval=3600):
    """Candle retention at most once per hour instead of after every batch."""
    global _RETENTION_LAST
    if time.time() - _RETENTION_LAST < min_interval:
        return
    _RETENTION_LAST = time.time()
    cutoff_1m = (pd.Timestamp.now(tz=IST) - pd.Timedelta(days=8)).isoformat()
    cutoff_core = (pd.Timestamp.now(tz=IST) - pd.Timedelta(days=65)).isoformat()
    with _memory_connect() as conn:
        conn.execute("DELETE FROM candles WHERE timeframe='1M' AND candle_time < ?", (cutoff_1m,))
        conn.execute("DELETE FROM candles WHERE timeframe!='1M' AND candle_time < ?", (cutoff_core,))


def _save_candle_memory(batch, per_tf):
    now_text = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
    records = []
    for original, ticker in batch:
        for tf, frame_map in per_tf.items():
            frame = frame_map.get(ticker)
            if frame is None or frame.empty:
                continue
            for r in frame[["Time", "Open", "High", "Low", "Close", "Volume"]].itertuples(index=False, name=None):
                ts = pd.Timestamp(r[0])
                ts = ts.tz_localize(IST) if ts.tzinfo is None else ts.tz_convert(IST)
                records.append((str(original), tf, ts.isoformat(), float(r[1]), float(r[2]),
                                float(r[3]), float(r[4]), float(r[5]), now_text))
    if not records:
        return 0
    with _memory_connect() as conn:
        conn.executemany("""INSERT INTO candles
            (symbol,timeframe,candle_time,open,high,low,close,volume,saved_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol,timeframe,candle_time) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume, saved_at=excluded.saved_at""", records)
    try:
        _ensure_extra_indexes()
        _retention_cleanup_if_due()
    except Exception:
        pass
    return len(records)


# ---------------------------------------------------------------- B3
_LIVE_TRACKER_LOCK = threading.Lock()
_LIVE_TRACKER_THREAD = None
_LIVE_TRACKER_STATUS = {"updated": 0, "error": None, "symbols": 0, "skipped": True}


def _is_tracking_window(now=None):
    """Market hours plus 10 minutes after close, so late outcomes can still resolve."""
    now = now or datetime.now(IST)
    if _is_market_hours(now):
        return True
    now = now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
    if now.weekday() >= 5 or now.strftime("%Y-%m-%d") in NSE_HOLIDAYS:
        return False
    return (15, 30) <= (now.hour, now.minute) < (15, 40)


def _live_tracker_loop():
    global _LIVE_TRACKER_STATUS
    while True:
        try:
            if _is_tracking_window():
                _LIVE_TRACKER_STATUS = _refresh_live_signal_prices()
        except Exception as exc:
            _LIVE_TRACKER_STATUS = {"updated": 0, "error": str(exc)[:200], "symbols": 0}
        time.sleep(60)


def _ensure_live_tracker():
    """Start one daemon thread per process; tracking no longer depends on an open browser tab."""
    global _LIVE_TRACKER_THREAD
    with _LIVE_TRACKER_LOCK:
        if _LIVE_TRACKER_THREAD is not None and _LIVE_TRACKER_THREAD.is_alive():
            return
        _LIVE_TRACKER_THREAD = threading.Thread(
            target=_live_tracker_loop, name="live-signal-tracker", daemon=True)
        _LIVE_TRACKER_THREAD.start()


# ---------------------------------------------------------------- P5
def _load_live_signal_events(limit=1500):
    """Bounded load: recent events only, and snapshots only for those events."""
    try:
        with _memory_connect() as conn:
            events = pd.read_sql_query("""SELECT event_id,event_key,symbol,yahoo_symbol,
                signal_type,direction,signal_price,signal_time_ist,created_at_ist,
                current_price,current_price_time_ist,data_status,status,stop_price,target_price,
                outcome,outcome_time_ist,risk_reward
                FROM signal_events ORDER BY created_at_ist DESC LIMIT ?""", conn, params=(int(limit),))
            snapshots = pd.read_sql_query("""SELECT event_id,elapsed_minutes,price,
                price_time_ist,change_pct FROM signal_price_snapshots
                WHERE event_id IN (SELECT event_id FROM signal_events
                                   ORDER BY created_at_ist DESC LIMIT ?)
                ORDER BY event_id,elapsed_minutes""", conn, params=(int(limit),))
        return events, snapshots
    except Exception:
        return pd.DataFrame(), pd.DataFrame()


# ---------------------------------------------------------------- B2
def _dashboard_signal_counts(result):
    """Same 7 rows/order as before (chart refs unchanged), with correct matching."""
    act = result.get("Action Signal", pd.Series(dtype=str)).fillna("WAIT").astype(str)
    return [
        ["BUY", int(act.isin(["BUY", "BUY CONFIRMED"]).sum())],
        ["SELL", int(act.isin(["SELL", "SELL CONFIRMED"]).sum())],
        ["SWEEP UP", int((act == "SWEEP UP").sum())],
        ["SWEEP DOWN", int((act == "SWEEP DOWN").sum())],
        ["BREAKOUT", int(act.str.startswith("BREAKOUT DETECTED").sum())],
        ["BREAKDOWN", int(act.str.startswith("BREAKDOWN DETECTED").sum())],
        ["WAIT", int((act == "WAIT").sum())],
    ]


# ---------------------------------------------------------------- B5
def _merge_remote_candle_cache(url, headers):
    """Stream the cloud DB to disk, upsert only candles that are newer than local."""
    bucket = str(os.getenv("SUPABASE_STORAGE_BUCKET", "ai-scanner-memory"))
    tmp = MEMORY_DB_PATH.with_suffix(f".remote.{uuid.uuid4().hex[:8]}.tmp")
    merged = 0
    try:
        with requests.get(
            f"{url}/storage/v1/object/{bucket}/ai_multi_agent_candle_memory.sqlite3",
            headers=headers, timeout=(10, 120), stream=True,
        ) as resp:
            if not resp.ok:
                return 0
            with tmp.open("wb") as fh:
                for chunk in resp.iter_content(1 << 20):
                    if chunk:
                        fh.write(chunk)
        if not tmp.exists() or tmp.stat().st_size == 0:
            return 0
        with _memory_connect() as conn:
            conn.execute("ATTACH DATABASE ? AS remote", (str(tmp),))
            try:
                before = conn.total_changes
                conn.execute("""INSERT INTO candles
                    (symbol,timeframe,candle_time,open,high,low,close,volume,saved_at)
                    SELECT symbol,timeframe,candle_time,open,high,low,close,volume,saved_at
                    FROM remote.candles WHERE true
                    ON CONFLICT(symbol,timeframe,candle_time) DO UPDATE SET
                        open=excluded.open, high=excluded.high, low=excluded.low,
                        close=excluded.close, volume=excluded.volume, saved_at=excluded.saved_at
                    WHERE excluded.saved_at >= candles.saved_at""")
                conn.execute("DELETE FROM candles WHERE timeframe='1H' AND substr(candle_time,12,5)='15:15'")
                merged = conn.total_changes - before
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.execute("DETACH DATABASE remote")
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(tmp) + suffix).unlink()
            except Exception:
                pass
    return merged


# ---------------------------------------------------------------- B7
def _expire_stale_jobs(max_hours=12):
    """Cancel RUNNING/PAUSED jobs older than max_hours so they cannot block new scans."""
    cutoff = (datetime.now(IST) - pd.Timedelta(hours=max_hours)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _memory_connect() as conn:
            conn.execute(
                """UPDATE scan_jobs SET status='CANCELLED', error='Expired (stale job)'
                   WHERE status IN ('RUNNING','PAUSED') AND substr(created_at,1,19) < ?""",
                (cutoff,),
            )
    except Exception:
        pass


# ---------------------------------------------------------------- P1
def _rowwise(df, fn):
    """Much faster than DataFrame.apply(axis=1) on very wide frames."""
    return pd.DataFrame([fn(r) for r in df.to_dict("records")], index=df.index)


# ---------------------------------------------------------------- P4
def _real_ai_training_data(max_bars_per_symbol=2500):
    """One window query, newest N bars per symbol; avoids N+1 queries and huge RAM use."""
    try:
        with _memory_connect() as conn:
            rows = conn.execute(
                """SELECT symbol,candle_time,open,high,low,close,volume FROM (
                       SELECT symbol,candle_time,open,high,low,close,volume,
                              ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY candle_time DESC) AS rn
                       FROM candles WHERE timeframe='5M')
                   WHERE rn <= ? ORDER BY symbol,candle_time""",
                (int(max_bars_per_symbol),),
            ).fetchall()
    except Exception:
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    all_df = pd.DataFrame(rows, columns=["Symbol", "Time", "Open", "High", "Low", "Close", "Volume"])
    all_df["Time"] = pd.to_datetime(all_df["Time"], errors="coerce", utc=True)
    samples = []
    for _symbol, d in all_df.groupby("Symbol", sort=False):
        if len(d) < 45:
            continue
        d = d.drop(columns="Symbol").reset_index(drop=True)
        feat = _real_ai_feature_frame(d)
        session_key = d["Time"].dt.tz_convert(IST).dt.date
        future_close = d["Close"].groupby(session_key).shift(-3)
        future_time = d["Time"].groupby(session_key).shift(-3)
        future_return = future_close / d["Close"] - 1
        atr_pct = feat["AI ATR %"] / 100.0
        deadband = np.maximum(0.0010, 0.80 * atr_pct * math.sqrt(3.0))
        label = pd.Series(np.where(future_return > deadband, 2,
                                   np.where(future_return < -deadband, 0, 1)), index=d.index)
        valid = feat.notna().all(axis=1) & future_return.notna() & future_time.notna()
        part = feat.loc[valid].copy()
        part["__label"] = label.loc[valid].astype(int)
        part["__time"] = d.loc[valid, "Time"].values
        part["__target_time"] = future_time.loc[valid].values
        samples.append(part)
    if not samples:
        return pd.DataFrame()
    return pd.concat(samples, ignore_index=True).sort_values("__time").reset_index(drop=True)


# ---------------------------------------------------------------- P7
@functools.lru_cache(maxsize=1)
def _stretch_kwargs_cached():
    try:
        major, minor = (int(x) for x in st.__version__.split(".")[:2])
        if (major, minor) >= (1, 49):
            return (("width", "stretch"),)
    except Exception:
        pass
    return (("use_container_width", True),)


def _stretch():
    return dict(_stretch_kwargs_cached())


def _cached_symbols(name, loader, ttl=3600):
    """Symbol universe is loaded once per hour, not on every Streamlit rerun."""
    key = ("symbols", name)
    cached = _ttl_cache_get(key, ttl)
    if cached is not None:
        return cached
    value = loader()
    if isinstance(value, pd.DataFrame):
        value = _normalise_symbols(value)
    _ttl_cache_set(key, value)
    return value
