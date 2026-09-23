"""电池资产管理路由（需登录）。

提供：
- 资产登记、列表、按序列号查询；
- 充电开始/完成、站点调拨、隔离/解除隔离/退役；
- 健康度维护；
- 按序列号查询完整迁移历史。

所有写操作都走 services.battery_service 的状态机校验，并在单事务内提交。
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import Battery
from ..schemas import (
    BatteryCreate,
    BatteryEventOut,
    BatteryHealthUpdate,
    BatteryHistoryOut,
    BatteryOut,
    ChargeCompleteRequest,
    IsolateRequest,
    RetireRequest,
    TransferRequest,
)
from ..services import battery_service

router = APIRouter(
    prefix="/api/batteries",
    tags=["电池资产"],
    dependencies=[Depends(get_current_user)],
)


def _to_out(battery: Battery) -> BatteryOut:
    return BatteryOut(
        id=battery.id,
        serial_no=battery.serial_no,
        spec=battery.spec,
        capacity_kwh=battery.capacity_kwh,
        health=battery.health,
        state=battery.state,
        current_soc=battery.current_soc,
        station_id=battery.station_id,
        vehicle_id=battery.vehicle_id,
        station_name=battery.station.name if battery.station else None,
        vehicle_plate=battery.vehicle.plate if battery.vehicle else None,
        created_at=battery.created_at,
        updated_at=battery.updated_at,
    )


@router.get("", response_model=list[BatteryOut])
def list_batteries(
    station_id: int | None = None,
    vehicle_id: int | None = None,
    state: str | None = None,
    spec: str | None = None,
    serial_no: str | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(Battery)
    if station_id is not None:
        query = query.filter(Battery.station_id == station_id)
    if vehicle_id is not None:
        query = query.filter(Battery.vehicle_id == vehicle_id)
    if state is not None:
        query = query.filter(Battery.state == state)
    if spec is not None:
        query = query.filter(Battery.spec == spec)
    if serial_no is not None:
        query = query.filter(Battery.serial_no.like(f"%{serial_no}%"))
    return [_to_out(b) for b in query.order_by(Battery.id).all()]


@router.post("", response_model=BatteryOut, status_code=status.HTTP_201_CREATED)
def register_battery(payload: BatteryCreate, db: Session = Depends(get_db)):
    try:
        battery = battery_service.register_battery(
            db,
            serial_no=payload.serial_no,
            spec=payload.spec,
            station_id=payload.station_id,
            capacity_kwh=payload.capacity_kwh,
            health=payload.health,
            current_soc=payload.current_soc,
            state=payload.state,
        )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"电池序列号 {payload.serial_no} 已存在")
    db.refresh(battery)
    return _to_out(battery)


@router.post("/{serial_no}/charge/start", response_model=BatteryOut)
def start_charging(serial_no: str, db: Session = Depends(get_db)):
    battery = battery_service.get_battery_or_404(db, serial_no)
    try:
        battery_service.start_charging(db, battery)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    db.refresh(battery)
    return _to_out(battery)


@router.post("/{serial_no}/charge/complete", response_model=BatteryOut)
def complete_charging(serial_no: str, payload: ChargeCompleteRequest, db: Session = Depends(get_db)):
    battery = battery_service.get_battery_or_404(db, serial_no)
    try:
        battery_service.complete_charging(
            db,
            battery,
            soc=payload.soc,
            health=payload.health,
            request_id=payload.request_id,
        )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    db.refresh(battery)
    return _to_out(battery)


@router.post("/{serial_no}/transfer", response_model=BatteryOut)
def transfer_battery(serial_no: str, payload: TransferRequest, db: Session = Depends(get_db)):
    battery = battery_service.get_battery_or_404(db, serial_no)
    try:
        battery_service.transfer_battery(db, battery, to_station_id=payload.to_station_id)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    db.refresh(battery)
    return _to_out(battery)


@router.post("/{serial_no}/isolate", response_model=BatteryOut)
def isolate_battery(serial_no: str, payload: IsolateRequest, db: Session = Depends(get_db)):
    battery = battery_service.get_battery_or_404(db, serial_no)
    try:
        battery_service.isolate_battery(db, battery, reason=payload.reason)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    db.refresh(battery)
    return _to_out(battery)


@router.post("/{serial_no}/unisolate", response_model=BatteryOut)
def unisolate_battery(serial_no: str, db: Session = Depends(get_db)):
    battery = battery_service.get_battery_or_404(db, serial_no)
    try:
        battery_service.unisolate_battery(db, battery)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    db.refresh(battery)
    return _to_out(battery)


@router.post("/{serial_no}/retire", response_model=BatteryOut)
def retire_battery(serial_no: str, payload: RetireRequest, db: Session = Depends(get_db)):
    battery = battery_service.get_battery_or_404(db, serial_no)
    try:
        battery_service.retire_battery(db, battery, reason=payload.reason)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    db.refresh(battery)
    return _to_out(battery)


@router.patch("/{serial_no}/health", response_model=BatteryOut)
def update_health(serial_no: str, payload: BatteryHealthUpdate, db: Session = Depends(get_db)):
    battery = battery_service.get_battery_or_404(db, serial_no)
    battery.health = payload.health
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(battery)
    return _to_out(battery)


@router.get("/{serial_no}/history", response_model=BatteryHistoryOut)
def battery_history(serial_no: str, db: Session = Depends(get_db)):
    battery = battery_service.get_battery_or_404(db, serial_no)
    events = battery_service.battery_history(db, battery)
    return BatteryHistoryOut(
        battery=_to_out(battery),
        events=[BatteryEventOut.model_validate(e) for e in events],
    )


@router.get("/{serial_no}", response_model=BatteryOut)
def get_battery(serial_no: str, db: Session = Depends(get_db)):
    return _to_out(battery_service.get_battery_or_404(db, serial_no))
