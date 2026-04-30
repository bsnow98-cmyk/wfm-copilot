"""
/schedules router.

POST   /schedules                — solve a new schedule against a staffing scenario
GET    /schedules                — list schedules
GET    /schedules/{id}           — full detail (segments + coverage)
GET    /schedules/{id}/coverage  — just the coverage rows (lightweight)

CP-SAT runtimes: 50 agents × 7 days typically solves in 5-30 seconds.
We run it inline (synchronous) to keep the API simple. If you scale to
hundreds of agents or longer horizons, move the solve into a Celery task
publishing on the Redis container that's already in compose.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.schedules import (
    CoverageRow,
    ScheduleDetail,
    ScheduleRequest,
    ScheduleSummary,
    ShiftSegment,
)
from app.services.scheduling import ScheduleService

log = logging.getLogger("wfm.schedules.router")
router = APIRouter(prefix="/schedules", tags=["schedules"])


@router.post(
    "",
    response_model=ScheduleDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Solve a new schedule from a staffing scenario.",
)
def create_schedule(
    body: ScheduleRequest,
    db: Session = Depends(get_db),
) -> ScheduleDetail:
    svc = ScheduleService(db)
    try:
        summary = svc.solve(
            staffing_id=body.staffing_id,
            name=body.name,
            agent_count=body.agent_count,
            horizon_days=body.horizon_days,
            target_shifts_per_week=body.target_shifts_per_week,
            min_rest_hours=body.min_rest_hours,
            max_consecutive_days=body.max_consecutive_days,
            max_solve_time_seconds=body.max_solve_time_seconds,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    detail = _load_detail(db, summary["schedule_id"])
    if detail is None:
        raise HTTPException(500, "schedule was created but could not be loaded back")
    return detail


@router.get("", response_model=list[ScheduleSummary])
def list_schedules(
    staffing_id: int | None = None,
    limit: int = 20,
    db: Session = Depends(get_db),
) -> list[ScheduleSummary]:
    sql = """
        SELECT id, name, staffing_id, start_date, end_date, status,
               solver_status, solver_runtime_seconds, objective_value,
               total_understaffed_intervals, error_message,
               created_at, started_at, completed_at
        FROM schedules
        {where}
        ORDER BY created_at DESC
        LIMIT :limit
    """
    where = "WHERE staffing_id = :sid" if staffing_id else ""
    params: dict = {"limit": limit}
    if staffing_id:
        params["sid"] = staffing_id
    rows = db.execute(text(sql.format(where=where)), params).mappings().all()
    return [ScheduleSummary(**dict(r)) for r in rows]


@router.get("/{schedule_id}", response_model=ScheduleDetail)
def get_schedule(schedule_id: int, db: Session = Depends(get_db)) -> ScheduleDetail:
    detail = _load_detail(db, schedule_id)
    if detail is None:
        raise HTTPException(404, f"schedules id={schedule_id} not found")
    return detail


@router.get("/{schedule_id}/coverage", response_model=list[CoverageRow])
def get_schedule_coverage(
    schedule_id: int, db: Session = Depends(get_db)
) -> list[CoverageRow]:
    rows = db.execute(
        text("""
            SELECT interval_start, required_agents, scheduled_agents, shortage
            FROM schedule_coverage
            WHERE schedule_id = :id
            ORDER BY interval_start
        """),
        {"id": schedule_id},
    ).mappings().all()
    if not rows:
        exists = db.execute(
            text("SELECT 1 FROM schedules WHERE id = :id"),
            {"id": schedule_id},
        ).scalar_one_or_none()
        if not exists:
            raise HTTPException(404, f"schedules id={schedule_id} not found")
    return [CoverageRow(**dict(r)) for r in rows]


# ----- helpers ---------------------------------------------------------
def _load_detail(db: Session, schedule_id: int) -> ScheduleDetail | None:
    parent = db.execute(
        text("""
            SELECT id, name, staffing_id, start_date, end_date, status,
                   solver_status, solver_runtime_seconds, objective_value,
                   total_understaffed_intervals, error_message,
                   created_at, started_at, completed_at
            FROM schedules WHERE id = :id
        """),
        {"id": schedule_id},
    ).mappings().first()
    if not parent:
        return None

    segments = db.execute(
        text("""
            SELECT s.agent_id, a.employee_id, a.full_name,
                   s.segment_type, s.start_time, s.end_time
            FROM shift_segments s
            JOIN agents a ON a.id = s.agent_id
            WHERE s.schedule_id = :id
            ORDER BY s.start_time, a.employee_id
        """),
        {"id": schedule_id},
    ).mappings().all()

    coverage = db.execute(
        text("""
            SELECT interval_start, required_agents, scheduled_agents, shortage
            FROM schedule_coverage
            WHERE schedule_id = :id
            ORDER BY interval_start
        """),
        {"id": schedule_id},
    ).mappings().all()

    return ScheduleDetail(
        **dict(parent),
        shift_segments=[ShiftSegment(**dict(r)) for r in segments],
        coverage=[CoverageRow(**dict(r)) for r in coverage],
    )
