"""仪表盘统计路由（需登录）。"""
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import (
    STATE_ISOLATED,
    STATE_READY,
    STATE_RETIRED,
    Battery,
    Station,
    SwapRecord,
    Vehicle,
)
from ..schemas import DashboardStats

router = APIRouter(prefix="/api/dashboard", tags=["仪表盘"], dependencies=[Depends(get_current_user)])


def _count(db: Session, *filters) -> int:
    return db.query(func.count(Battery.id)).filter(*filters).scalar() or 0


@router.get("/stats", response_model=DashboardStats)
def stats(db: Session = Depends(get_db)):
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return DashboardStats(
        station_total=db.query(Station).count(),
        station_running=db.query(Station).filter(Station.status == "running").count(),
        vehicle_total=db.query(Vehicle).count(),
        vehicle_fault=db.query(Vehicle).filter(Vehicle.status == "fault").count(),
        swap_today=db.query(SwapRecord).filter(SwapRecord.swapped_at >= today_start).count(),
        # 电池指标全部来自资产明细，与站点派生数同源，不会分叉
        battery_ready_total=_count(db, Battery.state == STATE_READY),
        battery_total=_count(db, Battery.state != STATE_RETIRED),
        battery_isolated_total=_count(db, Battery.state == STATE_ISOLATED),
        battery_retired_total=_count(db, Battery.state == STATE_RETIRED),
    )
