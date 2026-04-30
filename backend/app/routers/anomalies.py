"""
/anomalies router — Phase 5.

POST /anomalies/detect — run detection on a queue + date range, persist results.
GET  /anomalies        — list recent anomalies (used by the get_anomalies tool too).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.anomalies import (
    AnomaliesListResponse,
    AnomalyDetectRequest,
    AnomalyDetectResponse,
    AnomalyOut,
)
from app.services.anomaly import AnomalyService

log = logging.getLogger("wfm.anomaly.router")
router = APIRouter(prefix="/anomalies", tags=["anomalies"])


@router.post("/detect", response_model=AnomalyDetectResponse)
def detect(req: AnomalyDetectRequest, db: Session = Depends(get_db)) -> AnomalyDetectResponse:
    if req.end_date < req.start_date:
        raise HTTPException(400, "end_date must be on or after start_date")

    svc = AnomalyService(db)
    inserted, skipped, ran = svc.detect(
        queue=req.queue, start_date=req.start_date, end_date=req.end_date
    )

    drift_score: float | None = None
    if req.include_skill_drift:
        drift_inserted, drift_skipped, drift_score = svc.detect_skill_mix_drift(
            queue=req.queue, target_date=req.end_date
        )
        inserted += drift_inserted
        skipped += drift_skipped

    log.info(
        "anomaly.detect queue=%s [%s..%s] inserted=%d skipped=%d detectors=%s drift=%s",
        req.queue, req.start_date, req.end_date, inserted, skipped, ran, drift_score,
    )
    return AnomalyDetectResponse(
        inserted=inserted,
        skipped_duplicates=skipped,
        detectors_run=ran,  # type: ignore[arg-type]
        skill_drift_score=drift_score,
    )


@router.get("", response_model=AnomaliesListResponse)
def list_anomalies(
    since_date: date | None = None,
    queue: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
) -> AnomaliesListResponse:
    if since_date is None:
        since_date = date.today() - timedelta(days=7)
    svc = AnomalyService(db)
    rows = svc.list(since=since_date, queue=queue, limit=limit)
    return AnomaliesListResponse(items=[AnomalyOut(**r) for r in rows])
