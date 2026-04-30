"""
CSV ingest for interval history.

Expected CSV columns (header row required):
    queue, channel, interval_start, interval_minutes, offered, handled,
    abandoned, aht_seconds, asa_seconds, service_level

`channel`, `interval_minutes`, `asa_seconds`, and `service_level` are optional.

Example call:
    curl -X POST http://localhost:8000/ingest/intervals \
        -F "file=@intervals.csv"

Behaviour:
- Reads the upload in-memory with pandas (fine for files up to ~hundreds of MB).
- Uses an UPSERT (ON CONFLICT) so re-uploading the same file is idempotent.
- Returns row counts so you can sanity-check the load.
"""
from __future__ import annotations

import io
import logging

import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db

log = logging.getLogger("wfm.ingest")
router = APIRouter(prefix="/ingest", tags=["ingest"])

REQUIRED_COLS = {"queue", "interval_start", "offered", "handled", "aht_seconds"}
OPTIONAL_COLS = {
    "channel": "voice",
    "interval_minutes": 30,
    "abandoned": 0,
    "asa_seconds": None,
    "service_level": None,
}


@router.post("/intervals")
async def ingest_intervals(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Upload must be a .csv file")

    raw = await file.read()
    try:
        df = pd.read_csv(io.BytesIO(raw))
    except Exception as exc:
        raise HTTPException(400, f"Could not parse CSV: {exc}") from exc

    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        raise HTTPException(400, f"Missing required columns: {sorted(missing)}")

    # Fill optional columns with defaults if absent.
    for col, default in OPTIONAL_COLS.items():
        if col not in df.columns:
            df[col] = default

    # Coerce types defensively. Bad data is the #1 cause of WFM bugs.
    df["interval_start"] = pd.to_datetime(df["interval_start"], utc=True, errors="coerce")
    if df["interval_start"].isna().any():
        raise HTTPException(400, "Some interval_start values failed to parse")

    int_cols = ["offered", "handled", "abandoned", "interval_minutes"]
    for c in int_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)

    num_cols = ["aht_seconds", "asa_seconds", "service_level"]
    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # Bulk upsert via raw SQL — much faster than ORM row-by-row at any meaningful size.
    insert_sql = text("""
        INSERT INTO interval_history (
            queue, channel, interval_start, interval_minutes,
            offered, handled, abandoned, aht_seconds, asa_seconds, service_level
        ) VALUES (
            :queue, :channel, :interval_start, :interval_minutes,
            :offered, :handled, :abandoned, :aht_seconds, :asa_seconds, :service_level
        )
        ON CONFLICT (queue, channel, interval_start) DO UPDATE SET
            offered       = EXCLUDED.offered,
            handled       = EXCLUDED.handled,
            abandoned     = EXCLUDED.abandoned,
            aht_seconds   = EXCLUDED.aht_seconds,
            asa_seconds   = EXCLUDED.asa_seconds,
            service_level = EXCLUDED.service_level
    """)

    rows = df.to_dict(orient="records")
    # NaN -> None so psycopg sends proper SQL NULLs
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and pd.isna(v):
                r[k] = None

    try:
        db.execute(insert_sql, rows)
        db.commit()
    except Exception as exc:
        db.rollback()
        log.exception("Ingest failed")
        raise HTTPException(500, f"DB write failed: {exc}") from exc

    log.info("Ingested %d interval rows", len(rows))
    return {
        "rows_ingested": len(rows),
        "queues": sorted(df["queue"].unique().tolist()),
        "min_interval": str(df["interval_start"].min()),
        "max_interval": str(df["interval_start"].max()),
    }
