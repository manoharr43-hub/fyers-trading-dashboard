# BPAI module for NSE AI PRO V17
# Paste ABOVE def show_scanner(fyers). Uses existing:
# _fetch_timeframe_data, _pre_move_signal, detect_big_move_setup
# Scores are heuristic clue scores, NOT probabilities.

def _bpai_num(x, default=0.0):
    try:
        y = float(x)
        return y if pd.notna(y) else default
    except Exception:
        return default


def _bpai_candle_analysis(df):
    out = {"status": "NO_DATA", "trend": "UNKNOWN", "direction": "NONE",
           "activity_score": 0, "rvol": 0.0, "close": None, "vwap": None,
           "sweep": "NONE", "phase": "UNCONFIRMED", "reason": "Insufficient candles"}
    cols = ["Open", "High", "Low", "Close", "Volume"]
    if not isinstance(df, pd.DataFrame) or len(df) < 30 or any(c not in df for c in cols):
        return out
    d = df.copy().reset_index(drop=True)
    for c in cols:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=cols).reset_index(drop=True)
    if len(d) < 30:
        return out
    z = d.iloc[-1]
    close, opn, high, low, vol = map(_bpai_num, [z.Close, z.Open, z.High, z.Low, z.Volume])
    if close <= 0 or high < low:
        return {**out, "reason": "Invalid OHLCV"}
    avgvol = _bpai_num(d["Volume"].iloc[-21:-1].mean())
    rvol = vol / avgvol if avgvol > 0 else 0
    e9 = d.Close.ewm(span=9, adjust=False).mean().iloc[-1]
    e21 = d.Close.ewm(span=21, adjust=False).mean().iloc[-1]
    e50 = d.Close.ewm(span=50, adjust=False).mean().iloc[-1]
    trend = "BULLISH" if close > e9 > e21 > e50 else "BEARISH" if close < e9 < e21 < e50 else "SIDEWAYS"
    rng = max(high - low, 1e-9)
    loc = (close - low) / rng
    body_pct = abs(close - opn) / rng
    ret3 = (close / max(_bpai_num(d.Close.iloc[-4]), 1e-9) - 1) * 100
    ret6 = (close / max(_bpai_num(d.Close.iloc[-7]), 1e-9) - 1) * 100
    # Reference levels use prior candles only; detect sweep-and-reclaim clues.
    prev_hi = _bpai_num(d.High.iloc[-21:-1].max())
    prev_lo = _bpai_num(d.Low.iloc[-21:-1].min())
    sweep = "NONE"
    if high > prev_hi and close < prev_hi:
        sweep = "HIGH_SWEEP"
    elif low < prev_lo and close > prev_lo:
        sweep = "LOW_SWEEP"
    typical = (d.High + d.Low + d.Close) / 3
    vtot = _bpai_num(d.Volume.sum())
    vwap = _bpai_num((typical * d.Volume).sum() / vtot) if vtot > 0 else close
    buy = sell = 0
    reasons = []
    if rvol >= 2:
        reasons.append("unusually high relative volume")
        if loc >= .65: buy += 22
        elif loc <= .35: sell += 22
        else: buy += 8; sell += 8
    elif rvol >= 1.3:
        if loc >= .7: buy += 12
        elif loc <= .3: sell += 12
    if loc >= .75 and body_pct >= .5: buy += 15; reasons.append("strong close near high")
    if loc <= .25 and body_pct >= .5: sell += 15; reasons.append("strong close near low")
    if ret3 > .25: buy += 10
    elif ret3 < -.25: sell += 10
    if ret6 > .5: buy += 8
    elif ret6 < -.5: sell += 8
    if close > vwap: buy += 8
    elif close < vwap: sell += 8
    if trend == "BULLISH": buy += 15
    elif trend == "BEARISH": sell += 15
    if sweep == "LOW_SWEEP": buy += 18; reasons.append("possible low sweep/reclaim")
    elif sweep == "HIGH_SWEEP": sell += 18; reasons.append("possible high sweep/rejection")
    direction = "BUY" if buy >= sell + 12 else "SELL" if sell >= buy + 12 else "NONE"
    phase = ("POSSIBLE_ACCUMULATION" if rvol >= 1.3 else "BULLISH_PRESSURE") if direction == "BUY" else (
            ("POSSIBLE_DISTRIBUTION" if rvol >= 1.3 else "BEARISH_PRESSURE") if direction == "SELL" else "MIXED_ACTIVITY")
    return {"status":"OK", "trend":trend, "direction":direction,
            "activity_score":min(100, max(buy, sell)), "rvol":round(rvol,2),
            "close":round(close,2), "vwap":round(vwap,2), "sweep":sweep,
            "phase":phase, "reason":"; ".join(reasons) or "No unusual directional volume clue",
            "ret3_pct":round(ret3,2), "ret6_pct":round(ret6,2)}


def _bpai_analyze_symbol(fyers, symbol, market="NSE"):
    out = {"SYMBOL":symbol, "MARKET":market, "DIRECTION":"WAIT",
           "EARLY SCORE":0, "ACTIVITY SCORE":0, "CONTINUATION SCORE":0,
           "5M TREND":"N/A", "15M TREND":"N/A", "1H TREND":"N/A",
           "PHASE CLUE":"N/A", "RVOL 5M":0, "LIQUIDITY SWEEP":"NONE",
           "PRE-MOVE":"N/A", "BIG MOVE":"N/A", "LTP":None,
           "BREAKOUT LEVEL":None, "BREAKDOWN LEVEL":None, "REASON":""}
    frames = {}
    errors = []
    for tf in ("5", "15", "60"):
        try:
            df = _fetch_timeframe_data(fyers, symbol, tf, lookback_days=30)
            if isinstance(df, pd.DataFrame) and len(df) >= 30:
                frames[tf] = (df, _bpai_candle_analysis(df))
            else:
                errors.append(tf + "m data unavailable")
        except Exception as exc:
            errors.append(tf + "m: " + type(exc).__name__)
    if "5" not in frames:
        out["REASON"] = "; ".join(errors) or "5m data unavailable"
        return out
    d5, a5 = frames["5"]
    a15 = frames.get("15", (None, {"trend":"UNKNOWN"}))[1]
    a60 = frames.get("60", (None, {"trend":"UNKNOWN"}))[1]
    try:
        pre = _pre_move_signal(d5)
    except Exception as exc:
        pre = {"status":"N/A","direction":"NONE","score":0,"reason":"PRE-MOVE error: "+type(exc).__name__}
    try:
        big = detect_big_move_setup(d5)
    except Exception as exc:
        big = {"signal":"N/A","direction":"NONE","score":0,"reason":"BIG MOVE error: "+type(exc).__name__}
    trends = [a5.get("trend"), a15.get("trend"), a60.get("trend")]
    bulls, bears = trends.count("BULLISH"), trends.count("BEARISH")
    predir = str(pre.get("direction","NONE")).upper()
    bigdir = str(big.get("direction","NONE")).upper()
    early = min(100, round(_bpai_num(pre.get("score"))))
    continuation = (35 if bulls >= 2 or bears >= 2 else 0)
    continuation += 20 if a5.get("rvol",0) >= 1.5 else 0
    continuation += 20 if ((bulls >= 2 and a5.get("close",0) > a5.get("vwap",float("inf"))) or
                           (bears >= 2 and a5.get("close",0) < a5.get("vwap",0))) else 0
    continuation += 25 if ((bigdir == "BUY" and bulls >= 2) or (bigdir == "SELL" and bears >= 2)) else 0
    direction = "BUY" if bulls >= 2 and (a5.get("direction")=="BUY" or predir=="BUY" or bigdir=="BUY") else (
                "SELL" if bears >= 2 and (a5.get("direction")=="SELL" or predir=="SELL" or bigdir=="SELL") else (
                predir if predir == a5.get("direction") and predir in ("BUY","SELL") and early >= 60 else "WAIT"))
    out.update({"DIRECTION":direction, "EARLY SCORE":early,
        "ACTIVITY SCORE":a5.get("activity_score",0), "CONTINUATION SCORE":min(100,continuation),
        "5M TREND":a5.get("trend","N/A"), "15M TREND":a15.get("trend","DATA_UNAVAILABLE"),
        "1H TREND":a60.get("trend","DATA_UNAVAILABLE"), "PHASE CLUE":a5.get("phase","N/A"),
        "RVOL 5M":a5.get("rvol",0), "LIQUIDITY SWEEP":a5.get("sweep","NONE"),
        "PRE-MOVE":pre.get("status","N/A"), "BIG MOVE":big.get("signal","N/A"),
        "LTP":a5.get("close"), "BREAKOUT LEVEL":pre.get("breakout_level"),
        "BREAKDOWN LEVEL":pre.get("breakdown_level"),
        "REASON":" | ".join([str(a5.get("reason","")),str(pre.get("reason","")),str(big.get("reason",""))])[:600]})
    if errors: out["REASON"] += " | Data notes: " + ", ".join(errors)
    return out


def _bpai_backtest(df, horizon=3, min_score=60, min_move_pct=.40):
    """Simple historical event test; not a full trading-system backtest."""
    cols = ["Open","High","Low","Close","Volume"]
    if not isinstance(df,pd.DataFrame) or len(df)<80 or any(c not in df for c in cols):
        return {"samples":0,"hit_rate_pct":None,"avg_favorable_return_pct":None,
                "note":"Need at least 80 valid OHLCV candles."}
    d=df.copy().reset_index(drop=True)
    for c in cols: d[c]=pd.to_numeric(d[c],errors="coerce")
    d=d.dropna(subset=cols).reset_index(drop=True)
    close=d.Close; e9=close.ewm(span=9,adjust=False).mean(); e21=close.ewm(span=21,adjust=False).mean()
    e50=close.ewm(span=50,adjust=False).mean(); avgvol=d.Volume.rolling(20).mean().shift(1)
    events=[]; i=30; end=len(d)-int(horizon)
    while i<end:
        price=_bpai_num(close.iloc[i])
        if price<=0: i+=1; continue
        rv=_bpai_num(d.Volume.iloc[i]/avgvol.iloc[i]) if _bpai_num(avgvol.iloc[i])>0 else 0
        rng=max(_bpai_num(d.High.iloc[i]-d.Low.iloc[i]),1e-9)
        loc=(_bpai_num(close.iloc[i])-_bpai_num(d.Low.iloc[i]))/rng
        bull=int(close.iloc[i]>e9.iloc[i]>e21.iloc[i]>e50.iloc[i])
        bear=int(close.iloc[i]<e9.iloc[i]<e21.iloc[i]<e50.iloc[i])
        bs=35*bull+20*int(rv>=1.5)+20*int(loc>=.7)+25*int(close.iloc[i]>close.iloc[i-3])
        ss=35*bear+20*int(rv>=1.5)+20*int(loc<=.3)+25*int(close.iloc[i]<close.iloc[i-3])
        side,score=("BUY",bs) if bs>ss else ("SELL",ss)
        if score>=min_score:
            fwd=(_bpai_num(close.iloc[i+horizon])/price-1)*100
            favorable=fwd if side=="BUY" else -fwd
            events.append((favorable, favorable>=min_move_pct))
            i+=int(horizon) # reduce overlapping events
        else: i+=1
    if not events:
        return {"samples":0,"hit_rate_pct":None,"avg_favorable_return_pct":None,
                "note":"No events at this score threshold."}
    returns=[x[0] for x in events]
    return {"samples":len(events),"hit_rate_pct":round(100*sum(x[1] for x in events)/len(events),2),
            "avg_favorable_return_pct":round(sum(returns)/len(returns),3),
            "note":"Sample historical hit rate only; not future probability. Costs/slippage are excluded."}


def _show_big_player_ai_tab(fyers, nse_symbols, fo_symbols):
    st.markdown("## 🧠 BIG PLAYER ACTIVITY + HISTORICAL DATA AI")
    st.warning("Scores are heuristic clues, NOT probabilities. OHLCV cannot identify a specific institution.")
    universe=st.radio("Market",["NSE Stocks","F&O Stocks","Both"],horizontal=True,key="bpai_market")
    limit=st.number_input("Max symbols per scan",min_value=5,max_value=300,value=40,step=5,key="bpai_limit")
    if universe=="NSE Stocks":
        symbols=list(nse_symbols or []); market={s:"NSE" for s in symbols}
    elif universe=="F&O Stocks":
        symbols=list(fo_symbols or []); market={s:"F&O" for s in symbols}
    else:
        symbols=list(dict.fromkeys(list(nse_symbols or [])+list(fo_symbols or [])))
        foset=set(fo_symbols or []); market={s:("F&O" if s in foset else "NSE") for s in symbols}
    symbols=symbols[:int(limit)]
    st.caption(f"{len(symbols)} symbols | 5m + 15m + 1h | Existing PRE-MOVE and BIG MOVE reused")
    if st.button("🔍 RUN BIG PLAYER + PRE-MOVE SCAN",key="bpai_run"):
        rows=[]; errors=[]; bar=st.progress(0.0)
        for i,sym in enumerate(symbols):
            try: rows.append(_bpai_analyze_symbol(fyers,sym,market.get(sym,"NSE")))
            except Exception as exc: errors.append(f"{sym}: {type(exc).__name__}: {str(exc)[:80]}")
            bar.progress((i+1)/max(1,len(symbols)),text=f"{i+1}/{len(symbols)}")
        bar.empty()
        result=pd.DataFrame(rows)
        if not result.empty:
            result=result.sort_values(["CONTINUATION SCORE","EARLY SCORE","ACTIVITY SCORE"],ascending=False)
        st.session_state["bpai_results"]=result; st.session_state["bpai_errors"]=errors
    result=st.session_state.get("bpai_results")
    if isinstance(result,pd.DataFrame):
        st.dataframe(result,use_container_width=True,hide_index=True,height=520)
        if not result.empty:
            st.download_button("📥 DOWNLOAD CSV",result.to_csv(index=False).encode("utf-8-sig"),
                file_name="big_player_activity_ai.csv",mime="text/csv",key="bpai_csv")
        if st.session_state.get("bpai_errors"):
            with st.expander("Scan errors"): st.write(st.session_state["bpai_errors"][:100])
    st.markdown("### Historical check (one symbol)")
    sym=st.text_input("FYERS symbol",value="NSE:SBIN-EQ",key="bpai_bt_symbol").strip().upper()
    tf=st.selectbox("Timeframe",["5","15","60"],format_func=lambda x:{"5":"5m","15":"15m","60":"1h"}[x],key="bpai_bt_tf")
    days=st.selectbox("History days",[30,60,90],index=1,key="bpai_bt_days")
    horizon=st.selectbox("Outcome horizon in candles",[2,3,6],index=1,key="bpai_bt_horizon")
    if st.button("🧪 RUN HISTORICAL CHECK",key="bpai_bt_run"):
        try:
            hist=_fetch_timeframe_data(fyers,sym,tf,lookback_days=int(days))
            st.session_state["bpai_bt_result"]=_bpai_backtest(hist,horizon=int(horizon))
        except Exception as exc:
            st.session_state["bpai_bt_result"]={"samples":0,"note":f"{type(exc).__name__}: {str(exc)[:150]}"}
    bt=st.session_state.get("bpai_bt_result")
    if isinstance(bt,dict):
        c1,c2,c3=st.columns(3)
        c1.metric("Historical events",bt.get("samples",0))
        c2.metric("Sample hit rate",f"{bt['hit_rate_pct']}%" if bt.get("hit_rate_pct") is not None else "N/A")
        c3.metric("Avg favorable return",f"{bt['avg_favorable_return_pct']}%" if bt.get("avg_favorable_return_pct") is not None else "N/A")
        st.info(bt.get("note","Historical sample metric only."))
        st.caption("Quick diagnostic only: costs, slippage, stop/target fills, survivorship bias and walk-forward validation are not included.")


# INTEGRATION:
# 1) Add "🧠 BIG PLAYER AI" to the end of the existing st.tabs([...]) list.
# 2) After the existing `with tabs[17]:` AMD Scanner section, still inside show_scanner:
#       with tabs[18]:
#           _show_big_player_ai_tab(fyers, all_symbols, fo_symbols)
