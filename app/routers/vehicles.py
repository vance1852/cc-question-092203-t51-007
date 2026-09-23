"""车辆管理路由（需登录）。"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import STATE_INSTALLED, Battery, SwapRecord, Vehicle
from ..schemas import VehicleCreate, VehicleOut, VehicleUpdate

router = APIRouter(prefix="/api/vehicles", tags=["车辆"], dependencies=[Depends(get_current_user)])


def _to_out(db: Session, vehicle: Vehicle) -> VehicleOut:
    current_battery = (
        db.query(Battery)
        .filter(Battery.vehicle_id == vehicle.id, Battery.state == STATE_INSTALLED)
        .first()
    )
    return VehicleOut(
        id=vehicle.id,
        plate=vehicle.plate,
        model=vehicle.model,
        battery_capacity=vehicle.battery_capacity,
        battery_spec=vehicle.battery_spec,
        current_soc=vehicle.current_soc,
        status=vehicle.status,
        created_at=vehicle.created_at,
        current_battery_serial=current_battery.serial_no if current_battery else None,
    )


@router.get("", response_model=list[VehicleOut])
def list_vehicles(db: Session = Depends(get_db)):
    vehicles = db.query(Vehicle).order_by(Vehicle.id).all()
    return [_to_out(db, v) for v in vehicles]


@router.post("", response_model=VehicleOut, status_code=status.HTTP_201_CREATED)
def create_vehicle(payload: VehicleCreate, db: Session = Depends(get_db)):
    if db.query(Vehicle).filter(Vehicle.plate == payload.plate).first():
        raise HTTPException(status_code=409, detail="车牌已存在")
    vehicle = Vehicle(**payload.model_dump())
    db.add(vehicle)
    db.commit()
    db.refresh(vehicle)
    return _to_out(db, vehicle)


@router.get("/{vehicle_id}", response_model=VehicleOut)
def get_vehicle(vehicle_id: int, db: Session = Depends(get_db)):
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="车辆不存在")
    return _to_out(db, vehicle)


@router.put("/{vehicle_id}", response_model=VehicleOut)
def update_vehicle(vehicle_id: int, payload: VehicleUpdate, db: Session = Depends(get_db)):
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="车辆不存在")
    data = payload.model_dump(exclude_unset=True)
    if "plate" in data and data["plate"] != vehicle.plate:
        if db.query(Vehicle).filter(Vehicle.plate == data["plate"]).first():
            raise HTTPException(status_code=409, detail="车牌已存在")
    for key, value in data.items():
        setattr(vehicle, key, value)
    db.commit()
    db.refresh(vehicle)
    return _to_out(db, vehicle)


@router.delete("/{vehicle_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_vehicle(vehicle_id: int, db: Session = Depends(get_db)):
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="车辆不存在")
    # 车上仍装有在册电池时禁止删除，避免电池挂到不存在的车辆
    installed = (
        db.query(Battery)
        .filter(Battery.vehicle_id == vehicle_id, Battery.state == STATE_INSTALLED)
        .count()
    )
    if installed:
        raise HTTPException(status_code=409, detail="车辆仍装有在册电池，请先换电卸下后再删除")
    # 有换电历史的车辆保留，保证换电与电池事件引用可追溯
    if db.query(SwapRecord.id).filter(SwapRecord.vehicle_id == vehicle_id).first():
        raise HTTPException(status_code=409, detail="该车辆存在换电记录，不可删除")
    db.delete(vehicle)
    db.commit()
    return None
