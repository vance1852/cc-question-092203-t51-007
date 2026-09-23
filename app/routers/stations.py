"""换电站管理路由（需登录）。

注意：station.battery_ready 是由电池资产明细推导的只读汇总数，
建/改站点时传入该字段会被忽略并在响应中给出 warning。
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import Battery, Station
from ..schemas import StationCreate, StationOut, StationUpdate

router = APIRouter(prefix="/api/stations", tags=["换电站"], dependencies=[Depends(get_current_user)])

_IGNORED_WARNING = "battery_ready 为只读汇总（=本站可换电池资产数），已忽略手工赋值"


def _to_out(station: Station, warning: str | None = None) -> StationOut:
    return StationOut(
        id=station.id,
        name=station.name,
        address=station.address,
        slot_total=station.slot_total,
        battery_ready=station.battery_ready,
        status=station.status,
        created_at=station.created_at,
        warning=warning,
    )


@router.get("", response_model=list[StationOut])
def list_stations(db: Session = Depends(get_db)):
    return [_to_out(s) for s in db.query(Station).order_by(Station.id).all()]


@router.post("", response_model=StationOut, status_code=status.HTTP_201_CREATED)
def create_station(payload: StationCreate, db: Session = Depends(get_db)):
    data = payload.model_dump(exclude={"battery_ready"})
    station = Station(**data, battery_ready=0)
    db.add(station)
    db.commit()
    db.refresh(station)
    warning = _IGNORED_WARNING if payload.battery_ready is not None else None
    return _to_out(station, warning)


@router.get("/{station_id}", response_model=StationOut)
def get_station(station_id: int, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    return _to_out(station)


@router.put("/{station_id}", response_model=StationOut)
def update_station(station_id: int, payload: StationUpdate, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    data = payload.model_dump(exclude_unset=True, exclude={"battery_ready"})
    for key, value in data.items():
        setattr(station, key, value)
    db.commit()
    db.refresh(station)
    warning = _IGNORED_WARNING if payload.battery_ready is not None else None
    return _to_out(station, warning)


@router.delete("/{station_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_station(station_id: int, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    # 仍持有电池资产（含待充/充电中/可换/隔离）的站点不允许删除，避免资产成孤儿
    asset_count = db.query(Battery).filter(Battery.station_id == station_id).count()
    if asset_count:
        raise HTTPException(status_code=409, detail=f"该站点仍有 {asset_count} 块电池资产，请先调拨或退役")
    db.delete(station)
    db.commit()
    return None
