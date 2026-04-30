"""
/staffing-requirements router.

POST   /staffing-requirements        — compute requirements from a forecast (synchronous, fast)
GET    /staffing-requirements        — list, optionally filtered by forecast_run_id
GET    /staffing-requirements/{id}   — full detail with per-interval rows
GET    /staffing-requirements/{id}/intervals  — just the interval rows

Erlang C is fast (~1 ms per interval), so a full 14-day, 30-min forecast (672
intervals) computes in well under a second. We do it inline in the request
rather than as a BackgroundTask.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.staffing import (
    StaffingDetail,
    StaffingIntervalRow,
    StaffingRequest,
    StaffingSummary,
)
from app.services.staffing import StaffingService

log = logging.getLogger("wfm.staffing.router")
router = APIRouter(prefix="/staffing-requirements", tags=["staffing"])


@router.post(
    "",
    response_model=StaffingDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Compute staffing requirements (Erlang C) from a completed forecast.",
)
def create_staffing(
    body: StaffingRequest,
    db: Session = Depends(get_db),
) -> StaffingDetail:
    svc = StaffingService(db)
    try:
        staffing_id = svc.compute(
            forecast_run_id=body.forecast_run_id,
            service_level_target=body.service_level_target,
            target_answer_seconds=body.target_answer_seconds,
            shrinkage=body.shrinkage,
            target_asa_seconds=body.target_asa_seconds,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    return _load_detail(db, staffing_id)


@router.get("", response_model=list[StaffingSummary])
def list_staffing(
    forecast_run_id: int | None = None,
    limit: int = 20,
    db: Session = Depends(get_db),
) -> list[StaffingSummary]:
    sql = """
        SELECT s.id, s.forecast_run_id, s.service_level_target, s.target_answer_seconds,
               s.target_asa_seconds, s.shrinkage, s.interval_minutes, s.created_at,
               COUNT(i.staffing_id) AS intervals_count,
               COALESCE(MAX(i.required_agents), 0) AS peak_required_agents
        FROM staffing_requirements s
        LEFT JOIN staffing_requirement_intervals i ON i.staffing_id = s.id
        {where}
        GROUP BY s.id
        ORDER BY s.created_at DESC
        LIMIT :limit
    """
    where = "WHERE s.forecast_run_id = :fid" if forecast_run_id else ""
    params: dict = {"limit": limit}
    if forecast_run_id:
        params["fid"] = forecast_run_id

    rows = db.execute(text(sql.format(where=where)), params).mappings().all()
    return [StaffingSummary(**dict(r)) for r in rows]


@router.get("/{staffing_id}", response_model=StaffingDetail)
def get_staffing(staffing_id: int, db: Session = Depends(get_db)) -> StaffingDetail:
    detail = _load_detail(db, staffing_id)
    if detail is None:
        raise HTTPException(404, f"staffing_requirements id={staffing_id} not found")
    return detail


@router.get("/{staffing_id}/intervals", response_model=list[StaffingIntervalRow])
def get_staffing_intervals(
    staffing_id: int, db: Session = Depends(get_db)
) -> list[StaffingIntervalRow]:
    rows = db.execute(
        text("""
            SELECT interval_start, forecast_offered, forecast_aht_seconds,
                   required_agents_raw, required_agents,
                   expected_service_level, expected_asa_seconds, occupancy
            FROM staffing_requirement_intervals
            WHERE staffing_id = :id
            ORDER BY interval_start
        """),
        {"id": staffing_id},
    ).mappings().all()
    if not rows:
        exists = db.execute(
            text("SELECT 1 FROM staffing_requirements WHERE id = :id"),
            {"id": staffing_id},
        ).scalar_one_or_none()
        if not exists:
            raise HTTPException(404, f"staffing_requirements id={staffing_id} not found")
    return [StaffingIntervalRow(**dict(r)) for r in rows]


# ----- helpers ---------------------------------------------------------
def _load_detail(db: Session, staffing_id: int) -> StaffingDetail | None:
    parent = db.execute(
        text("""
            SELECT s.id, s.forecast_run_id, s.service_level_target, s.target_answer_seconds,
                   s.target_asa_seconds, s.shrinkage, s.interval_minutes, s.created_at,
                   COUNT(i.staffing_id) AS intervals_count,
                   COALESCE(MAX(i.required_agents), 0) AS peak_required_agents
            FROM staffing_requirements s
            LEFT JOIN staffing_requirement_intervals i ON i.staffing_id = s.id
            WHERE s.id = :id
            GROUP BY s.id
        """),
        {"id": staffing_id},
    ).mappings().first()
    if not parent:
        return None

    intervals = db.execute(
        text("""
            SELECT interval_start, forecast_offered, forecast_aht_seconds,
                   required_agents_raw, required_agents,
                   expected_service_level, expected_asa_seconds, occupancy
            FROM staffing_requirement_intervals
            WHERE staffing_id = :id
            ORDER BY interval_start
        """),
        {"id": staffing_id},
    ).mappings().all()

    return StaffingDetail(
        **dict(parent),
        intervals=[StaffingIntervalRow(**dict(r)) for r in intervals],
    )
