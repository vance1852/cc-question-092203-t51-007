"""电池资产路由（需登录）：建档、生命周期操作、历史与对账。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import Battery
from ..schemas import (
    BatteryChargeComplete,
    BatteryChargeStart,
    BatteryCreate,
    BatteryEventOut,
    BatteryHistory,
    BatteryIsolate,
    BatteryOut,
    BatteryRestore,
    BatteryRetire,
    BatteryTransfer,
    ReconcileReport,
    StationReconcileItem,
)
from ..services import battery_service as bs

router = APIRouter(prefix="/api/batteries", tags=["电池资产"], dependencies=[Depends(get_current_user)])


def _to_out(battery: Battery) -> BatteryOut:
    return BatteryOut(
        id=battery.id,
        serial_no=battery.serial_no,
        spec=battery.spec,
        capacity_kwh=battery.capacity_kwh,
        soh=battery.soh,
        soc=battery.soc,
        status=battery.status,
        station_id=battery.station_id,
        station_name=battery.station.name if battery.station else None,
        vehicle_id=battery.vehicle_id,
        vehicle_plate=_vehicle_plate(battery),
        created_at=battery.created_at,
        updated_at=battery.updated_at,
    )


def _vehicle_plate(battery: Battery):
    return battery.vehicle.plate if battery.vehicle else None


def _run(db: Session, action):
    """统一的事务边界：业务失败/约束冲突一律回滚，绝不留半迁移。"""
    try:
        result = action()
        db.commit()
        return result
    except bs.BusinessError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="重复提交（序列号或幂等键冲突），未产生新迁移")


@router.get("", response_model=list[BatteryOut])
def list_batteries(
    status: str | None = None,
    station_id: int | None = None,
    spec: str | None = None,
    serial_no: str | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(Battery)
    if status:
        if status not in bs.STATUS_CN:
            raise HTTPException(status_code=422, detail="非法电池状态")
        query = query.filter(Battery.status == status)
    if station_id is not None:
        query = query.filter(Battery.station_id == station_id)
    if spec:
        query = query.filter(Battery.spec == spec)
    if serial_no:
        query = query.filter(Battery.serial_no.contains(serial_no))
    return [_to_out(b) for b in query.order_by(Battery.id).all()]


@router.get("/reconcile", response_model=ReconcileReport)
def reconcile(db: Session = Depends(get_db)):
    """站点汇总数 vs 资产明细对账。"""
    rows = bs.reconcile(db)
    items = [
        StationReconcileItem(
            station_id=station.id,
            station_name=station.name,
            summary_battery_ready=station.battery_ready,
            actual_ready_assets=actual,
        )
        for station, actual in rows
    ]
    return ReconcileReport(
        consistent=all(i.summary_battery_ready == i.actual_ready_assets for i in items),
        stations=items,
    )


@router.post("", response_model=BatteryOut, status_code=201)
def register_battery(payload: BatteryCreate, db: Session = Depends(get_db)):
    battery = _run(
        db,
        lambda: bs.register_battery(
            db,
            serial_no=payload.serial_no,
            spec=payload.spec,
            capacity_kwh=payload.capacity_kwh,
            soh=payload.soh,
            soc=payload.soc,
            station_id=payload.station_id,
        ),
    )
    db.refresh(battery)
    return _to_out(battery)


@router.get("/{serial_no}", response_model=BatteryOut)
def get_battery(serial_no: str, db: Session = Depends(get_db)):
    battery = db.query(Battery).filter(Battery.serial_no == serial_no).first()
    if not battery:
        raise HTTPException(status_code=404, detail=f"电池 {serial_no} 不存在")
    return _to_out(battery)


@router.get("/{serial_no}/history", response_model=BatteryHistory)
def battery_history(serial_no: str, db: Session = Depends(get_db)):
    try:
        battery, events = bs.get_history(db, serial_no)
    except bs.BusinessError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return BatteryHistory(battery=_to_out(battery), events=[BatteryEventOut.model_validate(e) for e in events])


@router.post("/{serial_no}/charge-start", response_model=BatteryOut)
def charge_start(serial_no: str, payload: BatteryChargeStart, db: Session = Depends(get_db)):
    battery = _run(db, lambda: bs.start_charging(db, serial_no, idempotency_key=payload.request_id))
    db.refresh(battery)
    return _to_out(battery)


@router.post("/{serial_no}/charge-complete", response_model=BatteryOut)
def charge_complete(serial_no: str, payload: BatteryChargeComplete, db: Session = Depends(get_db)):
    battery = _run(
        db,
        lambda: bs.complete_charging(
            db,
            serial_no,
            soc=payload.soc,
            soh=payload.soh,
            idempotency_key=payload.request_id,
        ),
    )
    db.refresh(battery)
    return _to_out(battery)


@router.post("/{serial_no}/transfer", response_model=BatteryOut)
def transfer(serial_no: str, payload: BatteryTransfer, db: Session = Depends(get_db)):
    battery = _run(
        db,
        lambda: bs.transfer_battery(
            db, serial_no, to_station_id=payload.to_station_id, idempotency_key=payload.request_id
        ),
    )
    db.refresh(battery)
    return _to_out(battery)


@router.post("/{serial_no}/isolate", response_model=BatteryOut)
def isolate(serial_no: str, payload: BatteryIsolate, db: Session = Depends(get_db)):
    battery = _run(
        db,
        lambda: bs.isolate_battery(
            db, serial_no, reason=payload.reason, idempotency_key=payload.request_id
        ),
    )
    db.refresh(battery)
    return _to_out(battery)


@router.post("/{serial_no}/restore", response_model=BatteryOut)
def restore(serial_no: str, payload: BatteryRestore, db: Session = Depends(get_db)):
    battery = _run(db, lambda: bs.restore_battery(db, serial_no, idempotency_key=payload.request_id))
    db.refresh(battery)
    return _to_out(battery)


@router.post("/{serial_no}/retire", response_model=BatteryOut)
def retire(serial_no: str, payload: BatteryRetire, db: Session = Depends(get_db)):
    battery = _run(
        db,
        lambda: bs.retire_battery(
            db, serial_no, reason=payload.reason, idempotency_key=payload.request_id
        ),
    )
    db.refresh(battery)
    return _to_out(battery)
