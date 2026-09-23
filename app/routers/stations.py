"""换电站管理路由（需登录）。

站点库存数（可换/待充/充电中/隔离）全部由电池资产明细实时派生，
创建/更新站点时上送的 battery_ready 会被忽略并在响应中返回真实派生值，
从结构上保证汇总数与明细不会分叉。
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import (
    STATE_CHARGING,
    STATE_INSTALLED,
    STATE_ISOLATED,
    STATE_PENDING,
    STATE_READY,
    STATE_RETIRED,
    Station,
    SwapRecord,
)
from ..schemas import StationCreate, StationOut, StationUpdate
from ..services import battery_service

router = APIRouter(prefix="/api/stations", tags=["换电站"], dependencies=[Depends(get_current_user)])


def _to_out(station: Station, db: Session) -> StationOut:
    inv = battery_service.station_inventory(db, station.id)
    on_site_total = sum(
        v for k, v in inv.items() if k not in (STATE_INSTALLED, STATE_RETIRED)
    )
    return StationOut(
        id=station.id,
        name=station.name,
        address=station.address,
        slot_total=station.slot_total,
        status=station.status,
        created_at=station.created_at,
        battery_ready=inv[STATE_READY],
        battery_pending=inv[STATE_PENDING],
        battery_charging=inv[STATE_CHARGING],
        battery_isolated=inv[STATE_ISOLATED],
        battery_total=on_site_total,
    )


@router.get("", response_model=list[StationOut])
def list_stations(db: Session = Depends(get_db)):
    stations = db.query(Station).order_by(Station.id).all()
    return [_to_out(s, db) for s in stations]


@router.post("", response_model=StationOut, status_code=status.HTTP_201_CREATED)
def create_station(payload: StationCreate, db: Session = Depends(get_db)):
    # battery_ready 已废弃：库存只能通过登记电池资产形成
    station = Station(
        name=payload.name,
        address=payload.address,
        slot_total=payload.slot_total,
        status=payload.status,
    )
    db.add(station)
    db.commit()
    db.refresh(station)
    return _to_out(station, db)


@router.get("/{station_id}", response_model=StationOut)
def get_station(station_id: int, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    return _to_out(station, db)


@router.put("/{station_id}", response_model=StationOut)
def update_station(station_id: int, payload: StationUpdate, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    data = payload.model_dump(exclude_unset=True)
    data.pop("battery_ready", None)  # 废弃字段：显式忽略，不能直接改库存
    for key, value in data.items():
        setattr(station, key, value)
    db.commit()
    db.refresh(station)
    return _to_out(station, db)


@router.delete("/{station_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_station(station_id: int, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    # 站内仍有电池资产时禁止删除，避免资产成为无站点孤儿
    inventory = battery_service.station_inventory(db, station_id)
    if any(inventory.values()):
        raise HTTPException(
            status_code=409,
            detail="站内仍有电池资产（含待充/充电中/可换/隔离），请先调拨或隔离退役后再删除",
        )
    # 有换电历史的站点保留，保证换电与电池事件引用可追溯
    if db.query(SwapRecord.id).filter(SwapRecord.station_id == station_id).first():
        raise HTTPException(status_code=409, detail="该站点存在换电记录，不可删除")
    db.delete(station)
    db.commit()
    return None
