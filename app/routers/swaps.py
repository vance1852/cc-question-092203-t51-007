"""换电记录路由（需登录）。

换电在单事务内完成两块电池（卸下 + 装上）、车辆、换电记录与电池事件
的更新，任何一步失败整体回滚；支持 request_id 幂等，重复上报不会产生
第二次迁移。

兼容策略：旧客户端只上送 vehicle_id/station_id/soc_before/soc_after
（不传任何电池序列号）时进入"兼容模式"——由系统按车型规格自动选配
满电电池、自动取下车辆当前在册电池，记录以 is_legacy=true 标记，并在
短时间窗内对完全相同的上报做自然键去重。建议尽快改用
installed_serial/removed_serial 显式换电。
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import Station, SwapRecord, Vehicle
from ..schemas import SwapCreate, SwapOut
from ..services import battery_service

router = APIRouter(prefix="/api/swaps", tags=["换电记录"], dependencies=[Depends(get_current_user)])


def _to_out(record: SwapRecord) -> SwapOut:
    return SwapOut(
        id=record.id,
        vehicle_id=record.vehicle_id,
        station_id=record.station_id,
        soc_before=record.soc_before,
        soc_after=record.soc_after,
        swapped_at=record.swapped_at,
        vehicle_plate=record.vehicle.plate if record.vehicle else None,
        station_name=record.station.name if record.station else None,
        removed_battery_id=record.removed_battery_id,
        installed_battery_id=record.installed_battery_id,
        removed_serial=record.removed_battery.serial_no if record.removed_battery else None,
        installed_serial=record.installed_battery.serial_no if record.installed_battery else None,
        request_id=record.request_id,
        is_legacy=record.is_legacy,
    )


@router.get("", response_model=list[SwapOut])
def list_swaps(db: Session = Depends(get_db)):
    records = db.query(SwapRecord).order_by(SwapRecord.swapped_at.desc()).all()
    return [_to_out(r) for r in records]


@router.post("", response_model=SwapOut, status_code=status.HTTP_201_CREATED)
def create_swap(payload: SwapCreate, db: Session = Depends(get_db)):
    vehicle = db.get(Vehicle, payload.vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="车辆不存在")
    station = db.get(Station, payload.station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")

    is_legacy = payload.installed_serial is None and payload.removed_serial is None

    # 老调用（无序列号、无幂等键）：短时间窗自然键去重，防止网络重试
    # 在自动选配模式下制造第二次迁移。
    if is_legacy and not payload.request_id:
        duplicate = battery_service.find_legacy_duplicate(
            db,
            vehicle_id=payload.vehicle_id,
            station_id=payload.station_id,
            soc_before=payload.soc_before,
            soc_after=payload.soc_after,
        )
        if duplicate:
            return _to_out(duplicate)

    try:
        record = battery_service.perform_swap(
            db,
            vehicle=vehicle,
            station=station,
            soc_before=payload.soc_before,
            soc_after=payload.soc_after,
            installed_serial=payload.installed_serial,
            removed_serial=payload.removed_serial,
            request_id=payload.request_id,
            is_legacy=is_legacy,
        )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        # request_id 唯一键冲突：并发/重复上报，返回首条记录，不再迁移
        db.rollback()
        existing = (
            db.query(SwapRecord).filter(SwapRecord.request_id == payload.request_id).first()
        )
        if existing:
            return _to_out(existing)
        raise HTTPException(status_code=409, detail="换电请求冲突")
    db.refresh(record)
    return _to_out(record)
