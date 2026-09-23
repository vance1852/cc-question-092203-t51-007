"""仪表盘统计路由（需登录）。"""
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import Battery, Station, SwapRecord, Vehicle
from ..schemas import DashboardStats
from ..services import battery_service as bs

router = APIRouter(prefix="/api/dashboard", tags=["仪表盘"], dependencies=[Depends(get_current_user)])


@router.get("/stats", response_model=DashboardStats)
def stats(db: Session = Depends(get_db)):
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    # battery_ready_total 直接用资产明细统计，天然与各站汇总一致
    ready_total = (
        db.query(func.count(Battery.id)).filter(Battery.status == bs.READY).scalar()
    )
    return DashboardStats(
        station_total=db.query(Station).count(),
        station_running=db.query(Station).filter(Station.status == "running").count(),
        vehicle_total=db.query(Vehicle).count(),
        vehicle_fault=db.query(Vehicle).filter(Vehicle.status == "fault").count(),
        swap_today=db.query(SwapRecord).filter(SwapRecord.swapped_at >= today_start).count(),
        battery_ready_total=int(ready_total or 0),
        battery_total=db.query(Battery).count(),
        battery_isolated=db.query(Battery).filter(Battery.status == bs.ISOLATED).count(),
    )
