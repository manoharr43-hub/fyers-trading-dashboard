"""Scheduled NSE + F&O background pre-scan worker for NSE AI PRO V18.

Runs outside Streamlit, reuses the existing V18 _scan_batch() and agent logic,
persists reports in Supabase Postgres, and persists the candle SQLite cache in a
private Supabase Storage bucket so later 30-minute runs can use overlap refresh.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

import ai_multi_agent_scanner as scanner
from scanner import load_fo_stocks, load_nse_equity_symbols

IST = ZoneInfo("Asia/Kolkata")
UNIVERSE = "Both (NSE + F&O)"
BATCH_SIZE = int(os.getenv("PRESCAN_BATCH_SIZE", "25"))
PERIOD = os.getenv("PRESCAN_PERIOD", "59d")
STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "ai-scanner-memory")
STORAGE_OBJECT = "ai_multi_agent_candle_memory.sqlite3"


def supabase_config():
    url = os.environ["SUPABASE_URL"].strip().rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip()
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    return url, key, headers


def request_ok(response):
    if not response.ok:
        raise RuntimeError(f"Supabase HTTP {response.status_code}: {response.text[:1200]}")
    return response


def storage_download(url, headers):
    path = f"{url}/storage/v1/object/{STORAGE_BUCKET}/{STORAGE_OBJECT}"
    response = requests.get(path, headers=headers, timeout=90)
    if response.status_code == 404:
        print("No previous candle cache found; first scan will warm up the cache.")
        return
    request_ok(response)
    scanner.MEMORY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    scanner.MEMORY_DB_PATH.write_bytes(response.content)
    scanner._MEMORY_INITIALIZED = False
    print(f"Restored persistent candle cache ({len(response.content):,} bytes).")


def storage_upload(url, headers):
    db_path = scanner.MEMORY_DB_PATH
    if not db_path.exists():
        print("No SQLite candle cache file was created; skipping cache upload.")
        return
    import sqlite3
    try:
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.execute("PRAGMA wal_checkpoint(FULL)")
        conn.close()
    except Exception:
        pass
    path = f"{url}/storage/v1/object/{STORAGE_BUCKET}/{STORAGE_OBJECT}"
    upload_headers = {**headers, "Content-Type": "application/octet-stream", "x-upsert": "true"}
    response = requests.post(path, headers=upload_headers, data=db_path.read_bytes(), timeout=180)
    request_ok(response)
    print(f"Uploaded persistent candle cache ({db_path.stat().st_size:,} bytes).")


def json_safe(value):
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return None
        return value
    return str(value)


def update_run(url, headers, run_id, values):
    response = requests.patch(
        f"{url}/rest/v1/prescan_runs?id=eq.{run_id}",
        headers={**headers, "Content-Type": "application/json", "Prefer": "return=minimal"},
        json=values, timeout=45,
    )
    request_ok(response)


def create_run(url, headers, symbols, run_type):
    now = datetime.now(IST).isoformat()
    payload = {
        "id": str(uuid.uuid4()), "status": "RUNNING", "universe": UNIVERSE,
        "scheduled_type": run_type, "started_at": now,
        "total_symbols": len(symbols), "completed_symbols": 0,
        "symbols": [[s, t] for s, t in symbols],
    }
    response = requests.post(
        f"{url}/rest/v1/prescan_runs",
        headers={**headers, "Content-Type": "application/json", "Prefer": "return=representation"},
        json=payload, timeout=45,
    )
    request_ok(response)
    return payload["id"]


def save_rows(url, headers, run_id, rows):
    payload = []
    saved_at = datetime.now(IST).isoformat()
    for row in rows:
        payload.append({
            "scan_id": run_id,
            "symbol": str(row.get("Symbol", "")),
            "ticker": str(row.get("Yahoo Symbol", "")),
            "payload": json_safe(row),
            "saved_at": saved_at,
        })
    if not payload:
        return
    response = requests.post(
        f"{url}/rest/v1/prescan_rows?on_conflict=scan_id,symbol",
        headers={**headers, "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=payload, timeout=120,
    )
    request_ok(response)


def is_market_holiday(now):
    if now.weekday() >= 5:
        return True
    return now.strftime("%Y-%m-%d") in scanner.NSE_HOLIDAYS


def main():
    now = datetime.now(IST)
    if is_market_holiday(now):
        print(f"Skipping NSE holiday/weekend: {now:%Y-%m-%d %A}")
        return 0

    # GitHub schedule: 09:00 IST is PREMARKET; remaining cron events are MARKET.
    run_type = "PREMARKET" if (now.hour < 9 or (now.hour == 9 and now.minute < 15)) else "MARKET"
    url, _key, headers = supabase_config()
    storage_download(url, headers)

    fo = scanner._normalise_symbols(load_fo_stocks())
    nse = scanner._normalise_symbols(load_nse_equity_symbols())
    ordered = [s for s in (fo + nse) if not scanner._is_index_symbol(s)]
    seen, symbols = set(), []
    for original in ordered:
        ticker = scanner._to_yahoo_symbol(original)
        if ticker not in seen:
            seen.add(ticker)
            symbols.append((original, ticker))
    if not symbols:
        raise RuntimeError("scanner.py returned no NSE/F&O stock symbols.")

    run_id = create_run(url, headers, symbols, run_type)
    print(f"Starting {run_type} scan: {len(symbols)} unique NSE/F&O stocks; batch={BATCH_SIZE}.")
    started = time.time()
    completed = 0
    try:
        for start in range(0, len(symbols), BATCH_SIZE):
            batch = symbols[start:start + BATCH_SIZE]
            rows = scanner._scan_batch(batch, PERIOD, include_early=False)
            if not isinstance(rows, list) or len(rows) != len(batch):
                raise RuntimeError(f"Batch returned an unexpected row count at offset {start}.")
            # Precompute the transparent 18th Best Stock Analyst score without
            # FYERS depth; the app recalculates it after its optional live-depth step.
            for row in rows:
                row["Direction Movement"] = scanner._direction_movement(row)
                row.update(scanner._best_stock_analyst(pd.Series(row)))
            save_rows(url, headers, run_id, rows)
            completed += len(rows)
            update_run(url, headers, run_id, {
                "completed_symbols": completed,
                "updated_at": datetime.now(IST).isoformat(),
            })
            print(f"Progress: {completed}/{len(symbols)} ({time.time() - started:.1f}s)")

        storage_upload(url, headers)
        completed_at = datetime.now(IST).isoformat()
        update_run(url, headers, run_id, {
            "status": "DONE", "completed_symbols": completed,
            "completed_at": completed_at, "updated_at": completed_at,
            "error": None,
        })
        print(f"Completed {run_type} scan {run_id}: {completed} stocks in {(time.time()-started)/60:.1f} minutes.")
        return 0
    except Exception as exc:
        try:
            update_run(url, headers, run_id, {
                "status": "FAILED", "completed_symbols": completed,
                "updated_at": datetime.now(IST).isoformat(), "error": str(exc)[:2000],
            })
        except Exception as update_exc:
            print(f"Could not update failed run status: {update_exc}")
        raise


if __name__ == "__main__":
    sys.exit(main())
