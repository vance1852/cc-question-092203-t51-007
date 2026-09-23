"""换电记录路由（需登录）。

资产模式（推荐）：请求体带 battery_in_serial / battery_out_serial。
兼容模式：旧调用方只传电量，服务端自动选包，响应带 legacy_mode 与升级提示。
两种模式均在单一事务内完成两块电池、车辆、站点汇总与台账的更新。
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import SwapRecord
from ..schemas import SwapCreate, SwapOut
from ..services import swap_service
from ..services.battery_service import BusinessError

router = APIRouter(prefix="/api/swaps", tags=["换电记录"], dependencies=[Depends(get_current_user)])

LEGACY_NOTICE = (
    "本次为兼容模式（仅上报电量），系统已自动选择电池。"
    "请尽快升级为上报 battery_in_serial/battery_out_serial，"
    "以支持精确追溯与召回拦截。"
)


def _to_out(result: swap_service.SwapResult) -> SwapOut:
    record = result.record
    return SwapOut(
        id=record.id,
        vehicle_id=record.vehicle_id,
        station_id=record.station_id,
        battery_out_id=record.battery_out_id,
        battery_out_serial=record.battery_out.serial_no if record.battery_out else None,
        battery_in_id=record.battery_in_id,
        battery_in_serial=record.battery_in.serial_no if record.battery_in else None,
        soc_before=record.soc_before,
        soc_after=record.soc_after,
        legacy_mode=record.legacy_mode,
        request_id=record.request_id,
        swapped_at=record.swapped_at,
        vehicle_plate=record.vehicle.plate if record.vehicle else None,
        station_name=record.station.name if record.station else None,
        notice=LEGACY_NOTICE if result.legacy_mode else None,
    )


@router.get("", response_model=list[SwapOut])
def list_swaps(db: Session = Depends(get_db)):
    records = db.query(SwapRecord).order_by(SwapRecord.swapped_at.desc()).all()
    return [_to_out(swap_service.SwapResult(r, r.legacy_mode)) for r in records]


@router.post("", response_model=SwapOut, status_code=status.HTTP_201_CREATED)
def create_swap(payload: SwapCreate, db: Session = Depends(get_db)):
    try:
        result = swap_service.perform_swap(
            db,
            vehicle_id=payload.vehicle_id,
            station_id=payload.station_id,
            soc_before=payload.soc_before,
            soc_after=payload.soc_after,
            battery_in_serial=payload.battery_in_serial,
            battery_out_serial=payload.battery_out_serial,
            request_id=payload.request_id,
        )
        db.commit()
    except BusinessError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    except IntegrityError:
        # request_id 唯一约束：并发下的重复上报，回查首次结果幂等返回
        db.rollback()
        if payload.request_id:
            existing = db.query(SwapRecord).filter(SwapRecord.request_id == payload.request_id).first()
            if existing:
                return _to_out(swap_service.SwapResult(existing, existing.legacy_mode))
        raise HTTPException(status_code=409, detail="重复提交，未产生新迁移")

    db.refresh(result.record)
    return _to_out(result)
