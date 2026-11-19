"""
NSE AI PRO V18 (compact) - Multi-Agent Scanner + Live Dashboard
Entry point: show_ai_multi_agent_scanner(client=None)

Kept   : 17 OHLCV agents x 5M/15M/1H, ATR stop, sweep + breakout, live signal ledger,
         1:1.5 WIN/LOSS tracking, CSV export, FYERS depth (optional, shortlist only).
Fixed  : entry = signal candle close; outcome measured from candle close (no MISSED hack);
         dedupe by candle (not blanket 60 min); price refresh also works after market close;
         correct 'inserted' count; Nifty trend filter; no signals after 15:00.
Removed: Excel builder, ML brain, news, Supabase pre-scan, background worker threads.

requirements.txt: yfinance>=0.2.50
"""
import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import streamlit as st

try:
    import yfinance as yf
except Exception:
    yf = None

IST = ZoneInfo("Asia/Kolkata")
OPEN_T, CLOSE_T = pd.Timedelta(hours=9, minutes=15), pd.Timedelta(hours=15, minutes=30)
NSE_HOLIDAYS = {
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03",
    "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02",
    "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25",
}
INDEX_NAMES = {"NIFTY", "NIFTY50", "BANKNIFTY", "NIFTYBANK", "FINNIFTY", "MIDCPNIFTY",
               "NIFTYNEXT50", "SENSEX", "BANKEX"}
TFS = {"5M": ("5m", 5), "15M": ("15m", 15), "1H": ("60m", 60)}
WEIGHTS = {"5M": .25, "15M": .45, "1H": .30}
AGENTS = ["Accumulation", "Distribution", "Liquidity Sweep", "Volume Expansion", "VWAP",
          "EMA Trend", "RSI Momentum", "Fair Value Gap", "HH/HL", "LH/LL", "Pre-Big",
          "Chart Pattern", "Volume Analysis", "SMC Structure", "Order Block",
          "Buy/Sell Pressure", "Momentum Divergence"]
DB_PATH = Path(__file__).with_name("ai_scanner_events.sqlite3")
RR = 1.5
LAST_SIGNAL_TIME = pd.Timedelta(hours=15)      # no new signals after 15:00
FRESH_MINUTES = 45
_YF_LOCK = threading.Lock()
_DB_READY = False
_REFRESH_LOCK = threading.Lock()
_LAST_REFRESH = 0.0


# ------------------------------------------------------------------ helpers
def _num(x, d=0.0):
    try:
        x = float(x)
        return x if math.isfinite(x) else d
    except Exception:
        return d


def _stretch():
    try:
        a, b = (int(x) for x in st.__version__.split(".")[:2])
        if (a, b) >= (1, 49):
            return {"width": "stretch"}
    except Exception:
        pass
    return {"use_container_width": True}


def _market_open(now=None):
    now = (now or datetime.now(IST)).astimezone(IST)
    if now.weekday() >= 5 or now.strftime("%Y-%m-%d") in NSE_HOLIDAYS:
        return False
    return (9, 15) <= (now.hour, now.minute) < (15, 30)


def _ist(value):
    try:
        if isinstance(value, str):
            value = value.replace("IST", "").strip()
        t = pd.Timestamp(value)
        if pd.isna(t):
            return None
        return t.tz_localize(IST) if t.tzinfo is None else t.tz_convert(IST)
    except Exception:
        return None


def _normalise(values):
    if values is None:
        return []
    if isinstance(values, pd.DataFrame):
        for c in ("symbol", "Symbol", "fyers_symbol", "tradingsymbol"):
            if c in values.columns:
                values = values[c].dropna().astype(str).tolist()
                break
        else:
            return []
    out, seen = [], set()
    for it in ([values] if isinstance(values, str) else values):
        if isinstance(it, dict):
            it = it.get("symbol") or it.get("Symbol") or it.get("fyers_symbol")
        s = str(it).strip() if it else ""
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _base(sym):
    v = str(sym).strip().upper()
    for p in ("NSE:", "BSE:"):
        if v.startswith(p):
            v = v[len(p):]
    for s in ("-INDEX", "-EQ", "-BE", "_EQ"):
        if v.endswith(s):
            v = v[:-len(s)]
    return v


def _is_index(sym):
    return str(sym).upper().endswith("-INDEX") or _base(sym).split("_")[0] in INDEX_NAMES


def _yahoo(sym):
    v = _base(sym)
    return v if v.endswith((".NS", ".BO")) else f"{v}.NS"


# ------------------------------------------------------------------ database
@contextmanager
def _db():
    global _DB_READY
    conn = sqlite3.connect(str(DB_PATH), timeout=20)
    try:
        if not _DB_READY:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS signal_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL,
                symbol TEXT NOT NULL, yahoo_symbol TEXT, signal_type TEXT NOT NULL,
                direction TEXT, entry REAL, stop REAL, target REAL,
                signal_time TEXT NOT NULL, created_at TEXT NOT NULL,
                current_price REAL, price_time TEXT,
                status TEXT DEFAULT 'ACTIVE', outcome TEXT DEFAULT 'PENDING', outcome_time TEXT)""")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_ev_status ON signal_events(status)")
            _DB_READY = True
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ------------------------------------------------------------------ data
def _flatten(df):
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        want = {"open", "high", "low", "close", "volume"}
        for lv in range(out.columns.nlevels):
            if len(want & {str(x).lower() for x in out.columns.get_level_values(lv)}) >= 4:
                out.columns = out.columns.get_level_values(lv)
                break
        else:
            return pd.DataFrame()
    out.columns = [str(c).strip().capitalize() for c in out.columns]
    return out


def _extract(batch, ticker):
    if batch is None or batch.empty:
        return pd.DataFrame()
    if not isinstance(batch.columns, pd.MultiIndex):
        return _flatten(batch)
    for lv in range(batch.columns.nlevels):
        for val in batch.columns.get_level_values(lv).unique():
            if str(val).upper() == ticker.upper():
                try:
                    return _flatten(batch.xs(val, axis=1, level=lv, drop_level=True))
                except Exception:
                    pass
    return pd.DataFrame()


def _closed(df, minutes):
    """Clean OHLCV and keep only completed bars inside the NSE session."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out["Time"] = pd.to_datetime(out.index, errors="coerce")
    out = out.dropna(subset=["Time"])
    try:
        out["Time"] = out["Time"].dt.tz_localize(IST) if out["Time"].dt.tz is None \
            else out["Time"].dt.tz_convert(IST)
    except Exception:
        return pd.DataFrame()
    need = ["Open", "High", "Low", "Close", "Volume"]
    if any(c not in out.columns for c in need):
        return pd.DataFrame()
    for c in need:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=need).sort_values("Time").drop_duplicates("Time", keep="last")
    hi_oc, lo_oc = out[["Open", "Close"]].max(axis=1), out[["Open", "Close"]].min(axis=1)
    out = out[(out[["Open", "High", "Low", "Close"]] > 0).all(axis=1) & (out["High"] >= hi_oc)
              & (out["Low"] <= lo_oc) & (out["Volume"] >= 0)]
    tod = out["Time"] - out["Time"].dt.normalize()
    end = out["Time"] + pd.Timedelta(minutes=minutes)
    ok = ((tod >= OPEN_T) & (tod < CLOSE_T) & (end <= out["Time"].dt.normalize() + CLOSE_T)
          & (end <= pd.Timestamp.now(tz=IST)))
    if minutes == 60:                         # 15:15 hourly bar is only 15 min of trading
        ok &= tod != pd.Timedelta(hours=15, minutes=15)
    return out.loc[ok, ["Time"] + need].reset_index(drop=True)


def _download(tickers, interval, period):
    if yf is None:
        raise RuntimeError("yfinance not installed")
    with _YF_LOCK:
        data = yf.download(tickers=list(tickers), period=period, interval=interval,
                           auto_adjust=False, actions=False, progress=False,
                           threads=8, group_by="ticker", timeout=25)
    return data


def _close_time(frame, minutes):
    if frame is None or frame.empty:
        return None
    return pd.Timestamp(frame["Time"].iloc[-1]) + pd.Timedelta(minutes=minutes)


def _market_bias():
    """Nifty 15M trend: UP / DOWN / FLAT. Used to block counter-trend signals."""
    try:
        raw = _download(["^NSEI"], "15m", "5d")
        f = _closed(_extract(raw, "^NSEI") if isinstance(raw.columns, pd.MultiIndex) else _flatten(raw), 15)
        if len(f) < 25:
            return "UNKNOWN"
        c = f["Close"]
        e9, e20 = c.ewm(span=9, adjust=False).mean().iloc[-1], c.ewm(span=20, adjust=False).mean().iloc[-1]
        return "UP" if c.iloc[-1] > e9 > e20 else "DOWN" if c.iloc[-1] < e9 < e20 else "FLAT"
    except Exception:
        return "UNKNOWN"


# ------------------------------------------------------------------ indicators / agents
def _rsi(c, n=14):
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    ls = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    r = 100 - 100 / (1 + g / ls.replace(0, np.nan))
    r = r.where(~((ls == 0) & (g > 0)), 100.0)
    return r.where(~((ls == 0) & (g == 0)), 50.0)


def _atr(d, n=14):
    day = d["Time"].dt.date
    first = day.ne(day.shift(1))
    pc = d["Close"].shift(1).where(~first, d["Open"])
    tr = pd.concat([d["High"] - d["Low"], (d["High"] - pc).abs(), (d["Low"] - pc).abs()], axis=1).max(axis=1)
    v = tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean().iloc[-1]
    return float(v) if pd.notna(v) and v > 0 else None


def _vwap(d):
    x = d[d["Time"].dt.date == d["Time"].dt.date.iloc[-1]]
    tp, vol = (x["High"] + x["Low"] + x["Close"]) / 3, x["Volume"]
    return _num((tp * vol).sum() / vol.sum(), np.nan) if vol.sum() > 0 else np.nan


def _run_agents(d, tf):
    """17 rule-based agents -> {name: (direction, score)}. Scores are heuristics, not probabilities."""
    if d is None or len(d) < 30:
        return {n: ("NO DATA", 0) for n in AGENTS}
    d = d.reset_index(drop=True)
    o, h, l, c, v = (d[k].astype(float) for k in ("Open", "High", "Low", "Close", "Volume"))
    last = c.iloc[-1]
    if last <= 0:
        return {n: ("NO DATA", 0) for n in AGENTS}
    e9, e21, e50 = (c.ewm(span=p, adjust=False).mean() for p in (9, 21, 50))
    rsi = _rsi(c)
    avg = _num(v.shift(1).rolling(20, min_periods=10).mean().iloc[-1])
    rvol = v.iloc[-1] / avg if avg > 0 else 0.0
    span = max(h.iloc[-1] - l.iloc[-1], 1e-9)
    loc = (last - l.iloc[-1]) / span
    body_up = c.iloc[-1] > o.iloc[-1]
    body_dn = c.iloc[-1] < o.iloc[-1]
    r10 = (h.iloc[-10:].max() - l.iloc[-10:].min()) / last * 100
    lim = {"5M": 1.5, "15M": 3.0, "1H": 5.0}[tf]
    A = {}
    A["Accumulation"] = ("BUY", 1) if r10 <= lim and loc >= .65 and last >= c.iloc[-5] else ("WAIT", 0)
    A["Distribution"] = ("SELL", -1) if r10 <= lim and loc <= .35 and last <= c.iloc[-5] else ("WAIT", 0)
    ph, pl = h.iloc[-11:-1].max(), l.iloc[-11:-1].min()
    A["Liquidity Sweep"] = (("SELL", -2) if h.iloc[-1] > ph and last < ph else
                            ("BUY", 2) if l.iloc[-1] < pl and last > pl else ("WAIT", 0))
    A["Volume Expansion"] = (("BUY", 2) if rvol >= 1.8 and body_up else
                             ("SELL", -2) if rvol >= 1.8 and body_dn else ("WAIT", 0))
    vw = _vwap(d)
    A["VWAP"] = (("BUY", 1) if pd.notna(vw) and last > vw * 1.001 else
                 ("SELL", -1) if pd.notna(vw) and last < vw * .999 else ("WAIT", 0))
    A["EMA Trend"] = (("BUY", 2) if e9.iloc[-1] > e21.iloc[-1] and last > e50.iloc[-1] else
                      ("SELL", -2) if e9.iloc[-1] < e21.iloc[-1] and last < e50.iloc[-1] else ("WAIT", 0))
    r_now = _num(rsi.iloc[-1], 50)
    A["RSI Momentum"] = ("BUY", 1) if r_now >= 55 else ("SELL", -1) if r_now <= 45 else ("WAIT", 0)
    A["Fair Value Gap"] = (("BUY", 1) if l.iloc[-1] > h.iloc[-3] else
                           ("SELL", -1) if h.iloc[-1] < l.iloc[-3] else ("WAIT", 0))
    old, new = d.iloc[-10:-5], d.iloc[-5:]
    hh, hl = new["High"].max() > old["High"].max(), new["Low"].min() > old["Low"].min()
    lh, ll = new["High"].max() < old["High"].max(), new["Low"].min() < old["Low"].min()
    A["HH/HL"] = ("BUY", 2) if hh and hl else ("WAIT", 0)
    A["LH/LL"] = ("SELL", -2) if lh and ll else ("WAIT", 0)
    contraction = (h.iloc[-20:-8].max() - l.iloc[-20:-8].min()) > 0 and \
        (h.iloc[-8:].max() - l.iloc[-8:].min()) < .70 * (h.iloc[-20:-8].max() - l.iloc[-20:-8].min())
    A["Pre-Big"] = (("PRE-BIG BUY", 2) if contraction and rvol >= 1.2 and last >= h.iloc[-8:].max() * .995 else
                    ("PRE-BIG SELL", -2) if contraction and rvol >= 1.2 and last <= l.iloc[-8:].min() * 1.005
                    else ("WAIT", 0))
    # candle patterns
    po, pc_ = o.iloc[-2], c.iloc[-2]
    bull_eng = pc_ < po and body_up and o.iloc[-1] <= pc_ and last >= po
    bear_eng = pc_ > po and body_dn and o.iloc[-1] >= pc_ and last <= po
    res, sup = h.iloc[-21:-1].max(), l.iloc[-21:-1].min()
    A["Chart Pattern"] = (("BUY", 2) if bull_eng or (last > res and rvol >= 1.2) else
                          ("SELL", -2) if bear_eng or (last < sup and rvol >= 1.2) else ("WAIT", 0))
    up_v = v.iloc[-5:][c.iloc[-5:] >= o.iloc[-5:]].sum()
    dn_v = v.iloc[-5:][c.iloc[-5:] < o.iloc[-5:]].sum()
    bal = (up_v - dn_v) / (up_v + dn_v) if up_v + dn_v > 0 else 0.0
    A["Volume Analysis"] = (("BUY", 2) if rvol >= 1.5 and bal >= .2 else
                            ("SELL", -2) if rvol >= 1.5 and bal <= -.2 else ("WAIT", 0))
    sh, sl = h.iloc[-11:-1].max(), l.iloc[-11:-1].min()
    A["SMC Structure"] = ("BUY", 2) if last > sh else ("SELL", -2) if last < sl else ("WAIT", 0)
    # simplified order block: last opposing candle that later got displaced, then retested
    ob = ("WAIT", 0)
    win = d.iloc[-11:-1]
    for i in range(len(win) - 1, -1, -1):
        k, later = win.iloc[i], d.iloc[-(len(win) - i):]
        if k["Close"] < k["Open"] and later["Close"].max() > k["High"] and k["Low"] <= last <= k["High"] * 1.003 \
                and body_up:
            ob = ("BUY", 2)
            break
        if k["Close"] > k["Open"] and later["Close"].min() < k["Low"] and k["Low"] * .997 <= last <= k["High"] \
                and body_dn:
            ob = ("SELL", -2)
            break
    A["Order Block"] = ob
    ps = (loc - .5) * 2 + bal
    A["Buy/Sell Pressure"] = ("BUY", 2) if ps >= .65 else ("SELL", -2) if ps <= -.65 else ("WAIT", 0)
    lo_o, lo_n = l.iloc[-10:-5].idxmin(), l.iloc[-5:].idxmin()
    hi_o, hi_n = h.iloc[-10:-5].idxmax(), h.iloc[-5:].idxmax()
    if l[lo_n] < l[lo_o] and rsi[lo_n] > rsi[lo_o] + 2:
        A["Momentum Divergence"] = ("BUY", 1)
    elif h[hi_n] > h[hi_o] and rsi[hi_n] < rsi[hi_o] - 2:
        A["Momentum Divergence"] = ("SELL", -1)
    else:
        A["Momentum Divergence"] = ("WAIT", 0)
    return A


def _tf_direction(agents):
    sc = sum(s for _, s in agents.values())
    b = sum(d in ("BUY", "PRE-BIG BUY") for d, _ in agents.values())
    s_ = sum(d in ("SELL", "PRE-BIG SELL") for d, _ in agents.values())
    if sum(d != "NO DATA" for d, _ in agents.values()) < 8:
        return "NO DATA", sc
    if sc >= 7 and b >= 4 and b >= s_ + 3:
        return "BUY", sc
    if sc <= -7 and s_ >= 4 and s_ >= b + 3:
        return "SELL", sc
    return "WAIT", sc


def _sweep(d, minutes):
    """Confirmed liquidity sweep on the latest completed candle -> (flag, level)."""
    if d is None or len(d) < 31:
        return "NO", None
    w = d.tail(31).reset_index(drop=True)
    prior, last = w.iloc[:-1], w.iloc[-1]
    hi, lo = prior["High"].max(), prior["Low"].min()
    span = max(last["High"] - last["Low"], 1e-9)
    avg = prior["Volume"].tail(20).mean()
    rv = last["Volume"] / avg if avg > 0 else 0
    up_w = last["High"] - max(last["Open"], last["Close"])
    dn_w = min(last["Open"], last["Close"]) - last["Low"]
    up = last["High"] > hi and last["Close"] < hi and up_w / span >= .30 and last["Close"] < last["Open"] and rv >= 1.05
    dn = last["Low"] < lo and last["Close"] > lo and dn_w / span >= .30 and last["Close"] > last["Open"] and rv >= 1.05
    if up and dn:
        return "NO", None
    return ("UP", round(float(hi), 2)) if up else ("DOWN", round(float(lo), 2)) if dn else ("NO", None)


def _breakout(d, lookback=20):
    if d is None or len(d) < lookback + 1:
        return "WAIT"
    w = d.tail(lookback + 1)
    base, last = w.iloc[:-1], w.iloc[-1]
    span = max(last["High"] - last["Low"], 1e-9)
    body, cl = abs(last["Close"] - last["Open"]) / span, (last["Close"] - last["Low"]) / span
    avg = base["Volume"].mean()
    rv = last["Volume"] / avg if avg > 0 else 0
    if last["Close"] > base["High"].max() and last["Close"] > last["Open"] and body >= .55 and cl >= .75 and rv >= 1.5:
        return "BREAKOUT"
    if last["Close"] < base["Low"].min() and last["Close"] < last["Open"] and body >= .55 and cl <= .25 and rv >= 1.5:
        return "BREAKDOWN"
    return "WAIT"


# ------------------------------------------------------------------ result row
def _make_row(symbol, ticker, frames, bias):
    an = {}
    for tf in TFS:
        ag = _run_agents(frames.get(tf), tf)
        direction, score = _tf_direction(ag)
        an[tf] = {"agents": ag, "dir": direction, "score": score,
                  "n": len(frames.get(tf, [])) if frames.get(tf) is not None else 0}
    row = {"Symbol": symbol, "Yahoo Symbol": ticker}
    ok = all(an[tf]["dir"] != "NO DATA" for tf in TFS)
    weighted, signal, strength = 0.0, "NO DATA", 0
    if ok:
        weighted = sum(an[tf]["score"] * WEIGHTS[tf] for tf in TFS)
        dirs = [an[tf]["dir"] for tf in TFS]
        nb, ns = dirs.count("BUY"), dirs.count("SELL")
        pb = sum(an[tf]["agents"]["Pre-Big"][0] == "PRE-BIG BUY" for tf in TFS)
        ps = sum(an[tf]["agents"]["Pre-Big"][0] == "PRE-BIG SELL" for tf in TFS)
        if nb >= 2 and ns == 0 and weighted >= 5:
            signal = "BUY"
        elif ns >= 2 and nb == 0 and weighted <= -5:
            signal = "SELL"
        elif pb >= 2 and ns == 0 and weighted > 0:
            signal = "PRE-BIG BUY"
        elif ps >= 2 and nb == 0 and weighted < 0:
            signal = "PRE-BIG SELL"
        else:
            signal = "WAIT"
        strength = min(100, round(abs(weighted) / 20 * 100))
    f15 = frames.get("15M")
    atr = _atr(f15) if f15 is not None and len(f15) else None
    close15 = round(float(f15["Close"].iloc[-1]), 2) if f15 is not None and len(f15) else None
    entry = stop = None
    if signal != "WAIT" and signal != "NO DATA" and close15 and atr:
        entry = close15
        stop = round(entry - 1.5 * atr, 2) if "BUY" in signal else round(entry + 1.5 * atr, 2)
    sweep, sweep_level = _sweep(f15, 15)
    ct = _close_time(f15, 15)
    row.update({
        "Final Signal": signal, "Consensus Score": round(weighted, 2), "Strength %": strength,
        "5M": an["5M"]["dir"], "15M": an["15M"]["dir"], "1H": an["1H"]["dir"],
        "Last Price": close15, "Entry Price": entry, "Stop Loss": stop,
        "15M ATR": round(atr, 2) if atr else None,
        "Signal Time (IST)": ct.strftime("%Y-%m-%d %H:%M:%S") if ct is not None else None,
        "Sweep": sweep, "Sweep Level": sweep_level,
        "Breakout 15M": _breakout(f15), "Market Bias": bias,
        "Candles 5M/15M/1H": "/".join(str(an[t]["n"]) for t in TFS),
    })
    for tf in TFS:
        for name, (d, _) in an[tf]["agents"].items():
            row[f"{tf} {name}"] = d
    return row


def _scan(items, period, bias, progress=None):
    rows = []
    for start in range(0, len(items), 50):
        batch = items[start:start + 50]
        tickers = [t for _, t in batch]
        frames = {tf: {} for tf in TFS}
        for tf, (interval, minutes) in TFS.items():
            try:
                raw = _download(tickers, interval, period)
            except Exception:
                raw = pd.DataFrame()
            for t in tickers:
                frames[tf][t] = _closed(_extract(raw, t), minutes)
        for sym, t in batch:
            try:
                rows.append(_make_row(sym, t, {tf: frames[tf][t] for tf in TFS}, bias))
            except Exception as exc:
                rows.append({"Symbol": sym, "Yahoo Symbol": t, "Final Signal": "NO DATA",
                             "Strength %": 0, "Error": str(exc)[:120]})
        if progress:
            progress(min(start + 50, len(items)) / len(items))
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ event ledger
def _levels(row, direction, entry):
    stop = _num(row.get("Stop Loss"), np.nan)
    if not np.isfinite(stop) or (direction == "UP" and stop >= entry) or (direction == "DOWN" and stop <= entry):
        atr = _num(row.get("15M ATR"), np.nan)
        if not np.isfinite(atr) or atr <= 0:
            return None, None
        stop = entry - 1.5 * atr if direction == "UP" else entry + 1.5 * atr
    risk = abs(entry - stop)
    return (float(stop), float(entry + RR * risk if direction == "UP" else entry - RR * risk)) if risk > 0 else (None, None)


def _capture_events(result):
    """Save fresh actionable signals. Entry = signal candle close; unique per symbol+type+candle."""
    if result is None or result.empty:
        return 0
    now = datetime.now(IST)
    rows = []
    for _, r in result.iterrows():
        sig, brk, sweep = str(r.get("Final Signal")), r.get("Breakout 15M"), r.get("Sweep")
        cands = []
        if sig in ("BUY", "SELL"):
            cands.append((sig, "UP" if sig == "BUY" else "DOWN"))
        if sweep == "UP":
            cands.append(("SWEEP UP", "DOWN"))
        if sweep == "DOWN":
            cands.append(("SWEEP DOWN", "UP"))
        if brk == "BREAKOUT" and sig == "BUY":
            cands.append(("CONFIRMED BREAKOUT", "UP"))
        if brk == "BREAKDOWN" and sig == "SELL":
            cands.append(("CONFIRMED BREAKDOWN", "DOWN"))
        stamp = _ist(r.get("Signal Time (IST)"))
        entry = _num(r.get("Last Price"))            # = 15M close of the signal candle
        if not cands or stamp is None or entry <= 0:
            continue
        age = (now - stamp).total_seconds() / 60
        if not (-2 <= age <= FRESH_MINUTES):
            continue
        if stamp - stamp.normalize() >= LAST_SIGNAL_TIME:      # too late in the day
            continue
        bias = r.get("Market Bias")
        for stype, direction in cands:
            if (direction == "UP" and bias == "DOWN") or (direction == "DOWN" and bias == "UP"):
                continue                                      # against Nifty trend
            stop, target = _levels(r, direction, entry)
            if stop is None:
                continue                                      # cannot be resolved -> do not log
            st_txt = stamp.isoformat(timespec="seconds")
            rows.append((f"{r['Symbol']}|{stype}|{st_txt}", r["Symbol"], r.get("Yahoo Symbol"), stype,
                         direction, entry, stop, target, st_txt, now.isoformat(timespec="seconds")))
    if not rows:
        return 0
    with _db() as conn:
        before = conn.total_changes
        conn.executemany("""INSERT OR IGNORE INTO signal_events
            (event_key,symbol,yahoo_symbol,signal_type,direction,entry,stop,target,signal_time,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", rows)
        return conn.total_changes - before


def _refresh_prices():
    """Update active events from Yahoo 1M data and resolve WIN/LOSS from the signal candle close.
    Works after market close too (uses today's 1M data)."""
    global _LAST_REFRESH
    if yf is None:
        return {"updated": 0, "resolved": 0, "error": None}
    with _REFRESH_LOCK:
        if time.monotonic() - _LAST_REFRESH < 55:
            return {"updated": 0, "resolved": 0, "error": None}
        _LAST_REFRESH = time.monotonic()
    today = datetime.now(IST).strftime("%Y-%m-%d")
    with _db() as conn:
        # events from earlier days cannot be resolved with today's 1M data
        conn.execute("UPDATE signal_events SET status='EXPIRED' WHERE status='ACTIVE' AND substr(signal_time,1,10)<?",
                     (today,))
        act = pd.read_sql_query("SELECT * FROM signal_events WHERE status='ACTIVE'", conn)
    if act.empty:
        return {"updated": 0, "resolved": 0, "error": None}
    tickers = sorted({str(t or _yahoo(s)) for t, s in zip(act["yahoo_symbol"], act["symbol"])})
    updated = resolved = 0
    try:
        for s in range(0, len(tickers), 50):
            batch = tickers[s:s + 50]
            raw = _download(batch, "1m", "1d")
            if raw is None or raw.empty:
                continue
            for t in batch:
                f = _extract(raw, t)
                if f.empty or "Close" not in f:
                    continue
                idx = pd.to_datetime(f.index, errors="coerce")
                idx = idx.tz_localize(IST) if getattr(idx, "tz", None) is None else idx.tz_convert(IST)
                px = pd.DataFrame({"t": idx, "hi": pd.to_numeric(f["High"], errors="coerce").values,
                                   "lo": pd.to_numeric(f["Low"], errors="coerce").values,
                                   "c": pd.to_numeric(f["Close"], errors="coerce").values}).dropna()
                px = px[px["c"] > 0].sort_values("t")
                if px.empty:
                    continue
                last_c, last_t = float(px["c"].iloc[-1]), px["t"].iloc[-1].isoformat(timespec="seconds")
                evs = act[act["yahoo_symbol"].astype(str).eq(t) | act["symbol"].map(_yahoo).eq(t)]
                with _db() as conn:
                    for _, e in evs.iterrows():
                        conn.execute("UPDATE signal_events SET current_price=?,price_time=? WHERE event_id=?",
                                     (last_c, last_t, int(e["event_id"])))
                        updated += 1
                        anchor = _ist(e["signal_time"])           # measured from candle close
                        bars = px[px["t"] >= anchor]
                        for _, b in bars.iterrows():
                            if e["direction"] == "UP":
                                hit_stop, hit_tgt = b["lo"] <= e["stop"], b["hi"] >= e["target"]
                            else:
                                hit_stop, hit_tgt = b["hi"] >= e["stop"], b["lo"] <= e["target"]
                            if hit_stop or hit_tgt:
                                res = "LOSS" if hit_stop else "WIN"   # both in one bar -> LOSS (conservative)
                                conn.execute("""UPDATE signal_events SET outcome=?,status=?,outcome_time=?
                                    WHERE event_id=? AND outcome='PENDING'""",
                                             (res, res, b["t"].isoformat(timespec="seconds"), int(e["event_id"])))
                                resolved += 1
                                break
        return {"updated": updated, "resolved": resolved, "error": None}
    except Exception as exc:
        return {"updated": updated, "resolved": resolved, "error": str(exc)[:200]}


def _load_events(limit=2000):
    try:
        with _db() as conn:
            return pd.read_sql_query("SELECT * FROM signal_events ORDER BY created_at DESC LIMIT ?",
                                     conn, params=(int(limit),))
    except Exception:
        return pd.DataFrame()


# ------------------------------------------------------------------ dashboard
def _dashboard_body():
    info = _refresh_prices()
    ev = _load_events()
    st.subheader("📡 LIVE MARKET DASHBOARD")
    st.caption("Refresh: 60s • Entry = signal candle close • Target 1:1.5 • Prices: Yahoo 1M")
    if info.get("error"):
        st.warning(f"Yahoo price update issue: {info['error']}")
    if ev.empty:
        st.info("No signal events yet. Run the scanner; fresh signals are saved here.")
        st.divider()
        return
    now = pd.Timestamp.now(tz=IST)
    ev["Age (min)"] = ((now - pd.to_datetime(ev["created_at"], utc=True).dt.tz_convert(IST))
                       .dt.total_seconds() / 60).clip(lower=0).round(0)
    sign = np.where(ev["direction"] == "DOWN", -1.0, 1.0)
    ev["Move % (in favour)"] = ((ev["current_price"] / ev["entry"] - 1) * 100 * sign).round(2)
    done = ev[ev["outcome"].isin(["WIN", "LOSS"])]
    cols = st.columns(4)
    for col, label in zip(cols, ("BUY", "SELL", "SWEEP UP", "SWEEP DOWN")):
        sub = done[done["signal_type"].str.startswith(label)]
        w = int((sub["outcome"] == "WIN").sum())
        col.metric(f"{label} accuracy", f"{w / len(sub) * 100:.1f}%" if len(sub) else "N/A", f"{w}/{len(sub)} WIN")
    st.caption("Accuracy = WIN ÷ (WIN + LOSS). Pending/expired excluded. Same 1M bar touching both levels = LOSS.")
    act = ev[ev["status"] == "ACTIVE"]
    m = st.columns(3)
    m[0].metric("Active", len(act))
    m[1].metric("Fresh ≤ 5m", int((act["Age (min)"] <= 5).sum()))
    m[2].metric("Tracked stocks", ev["symbol"].nunique())
    show = ["symbol", "signal_type", "entry", "stop", "target", "current_price", "Move % (in favour)",
            "outcome", "signal_time", "Age (min)", "status"]
    st.markdown("#### 🔴 ACTIVE SIGNALS")
    st.dataframe(act[show].head(150), hide_index=True, **_stretch()) if len(act) else st.caption("No active signals.")
    st.markdown("#### 📈 ALL RECENT SIGNALS")
    st.dataframe(ev[show].head(200), hide_index=True, **_stretch())
    st.caption(f"Last refresh {datetime.now(IST):%H:%M:%S} IST • updated {info['updated']} • resolved {info['resolved']}")
    st.divider()


def _render_dashboard():
    if hasattr(st, "fragment"):
        @st.fragment(run_every="60s")
        def _frag():
            _dashboard_body()
        _frag()
    else:
        _dashboard_body()


# ------------------------------------------------------------------ FYERS depth (optional)
def _depth_for(client, symbols):
    rows = []
    for s in symbols:
        fy = f"NSE:{_base(s)}-EQ"
        try:
            r = client.depth(data={"symbol": fy, "ohlcv_flag": "1"})
            book = next(iter((r.get("d") or {}).values()), {})
            bq = sum(_num(x.get("volume", x.get("qty", 0))) for x in (book.get("bids") or [])[:5])
            sq = sum(_num(x.get("volume", x.get("qty", 0))) for x in (book.get("ask") or book.get("asks") or [])[:5])
            imb = (bq - sq) / (bq + sq) * 100 if bq + sq > 0 else None
            sig = "N/A" if imb is None else "BUY-SIDE" if imb >= 15 else "SELL-SIDE" if imb <= -15 else "BALANCED"
            rows.append({"Symbol": s, "Buy Qty": int(bq), "Sell Qty": int(sq),
                         "Imbalance %": round(imb, 1) if imb is not None else None, "Depth": sig})
        except Exception as exc:
            rows.append({"Symbol": s, "Depth": f"UNAVAILABLE: {str(exc)[:60]}"})
        time.sleep(1.05)
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ page
def show_ai_multi_agent_scanner(client=None):
    st.title("🚀 NSE AI PRO V18 — LIVE DASHBOARD + SCANNER")
    st.caption("17 OHLCV agents × 5M / 15M / 1H • Yahoo candles • FYERS depth (shortlist only)")
    _render_dashboard()
    st.warning("Yahoo intraday data may be delayed or rate-limited. Strength % is a heuristic score, "
               "not a probability. Signals are screening output, not trade advice.")
    if yf is None:
        st.error("Add yfinance>=0.2.50 to requirements.txt and redeploy.")
        return
    try:
        from scanner import load_nse_equity_symbols, load_fo_stocks
    except Exception as exc:
        st.error(f"Could not import symbol loaders from scanner.py: {exc}")
        return

    c1, c2, c3 = st.columns(3)
    universe = c1.selectbox("Universe", ["F&O Stocks", "Both (NSE + F&O)", "NSE Equity"], key="ma_universe")
    period = c2.selectbox("Yahoo history", ["30d", "59d"], key="ma_period")
    depth_limit = c3.selectbox("FYERS depth limit", [0, 5, 10, 20], index=2, key="ma_depth")
    try:
        fo = _normalise(load_fo_stocks()) if universe != "NSE Equity" else []
        nse = _normalise(load_nse_equity_symbols()) if universe != "F&O Stocks" else []
        symbols = [s for s in _normalise(fo + nse) if not _is_index(s)]
    except Exception as exc:
        st.error(f"Symbol list error: {exc}")
        return
    items, seen = [], set()
    for s in symbols:
        t = _yahoo(s)
        if t not in seen:
            seen.add(t)
            items.append((s, t))
    st.caption(f"{len(items)} unique tickers (F&O first when 'Both').")

    if st.button("🚀 RUN SCAN", type="primary", key="ma_run", **_stretch()):
        bar = st.progress(0.0, text="Scanning…")
        bias = _market_bias()
        res = _scan(items, period, bias, progress=lambda p: bar.progress(p, text=f"Scanning… {p:.0%}"))
        order = {"BUY": 0, "SELL": 0, "PRE-BIG BUY": 1, "PRE-BIG SELL": 1, "WAIT": 2, "NO DATA": 3}
        res["_p"] = res["Final Signal"].map(order).fillna(9)
        res = res.sort_values(["_p", "Strength %"], ascending=[True, False]).drop(columns="_p").reset_index(drop=True)
        if depth_limit and client is not None and callable(getattr(client, "depth", None)):
            short = res[res["Final Signal"].isin(["BUY", "SELL"])].head(depth_limit)["Symbol"].tolist()
            st.session_state["ma_depth_table"] = _depth_for(client, short)
        else:
            st.session_state["ma_depth_table"] = None
        st.session_state["ma_result"] = res
        st.session_state["ma_time"] = datetime.now(IST)
        bar.empty()
        try:
            n = _capture_events(res)
            st.success(f"Scan complete: {len(res)} stocks • Nifty bias: {bias} • {n} new event(s) saved.")
        except Exception as exc:
            st.warning(f"Event save issue: {exc}")

    res = st.session_state.get("ma_result")
    if not isinstance(res, pd.DataFrame) or res.empty:
        st.info("No results yet. Press RUN SCAN.")
        return

    st.subheader("📊 SCAN REPORT")
    counts = res["Final Signal"].value_counts()
    cols = st.columns(6)
    for col, lab in zip(cols, ("BUY", "SELL", "PRE-BIG BUY", "PRE-BIG SELL", "WAIT", "NO DATA")):
        col.metric(lab, int(counts.get(lab, 0)))
    flt = st.selectbox("Filter", ["ALL", "BUY", "SELL", "PRE-BIG BUY", "PRE-BIG SELL", "WAIT", "NO DATA",
                                  "SWEEP UP", "SWEEP DOWN", "BREAKOUT", "BREAKDOWN"], key="ma_filter")
    view = res
    if flt in ("SWEEP UP", "SWEEP DOWN"):
        view = res[res["Sweep"] == flt.split()[1]]
    elif flt in ("BREAKOUT", "BREAKDOWN"):
        view = res[res["Breakout 15M"] == flt]
    elif flt != "ALL":
        view = res[res["Final Signal"] == flt]
    show = [c for c in ["Symbol", "Final Signal", "Strength %", "Last Price", "Entry Price", "Stop Loss", "5M", "15M",
                        "1H", "Sweep", "Sweep Level", "Breakout 15M", "Market Bias", "Signal Time (IST)"]
            if c in view.columns]
    st.dataframe(view[show].head(300), hide_index=True, **_stretch())
    st.caption("Signal Time = latest completed 15M candle close (IST). Entry = that close. Stop = 1.5 × 15M ATR.")

    dt = st.session_state.get("ma_depth_table")
    if isinstance(dt, pd.DataFrame) and not dt.empty:
        st.subheader("📚 FYERS Market Depth — shortlist")
        st.dataframe(dt, hide_index=True, **_stretch())

    stamp = st.session_state.get("ma_time", datetime.now(IST))
    st.download_button("⬇️ Download Full CSV", res.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"scan_{stamp:%Y%m%d_%H%M%S}.csv", mime="text/csv", key="ma_csv")
    with st.expander("Agents & limitations"):
        st.write("17 deterministic OHLCV rule agents run on completed 5M/15M/1H candles (partial 15:15 hourly "
                 "bar excluded). A timeframe is BUY/SELL only with strong score and vote separation. Final "
                 "signal needs 2 of 3 timeframes aligned with none opposite. Counter-Nifty-trend signals and "
                 "signals after 15:00 are not logged to the live ledger.")
        st.dataframe(pd.DataFrame({"Agent": AGENTS}), hide_index=True, **_stretch())
