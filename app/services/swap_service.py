"""换电领域服务：一次换电的原子事务编排。

同事务内严格按顺序完成：
  1. 幂等检查（request_id 命中直接返回首次记录，不产生第二次迁移）；
  2. 校验车辆、站点、装上/卸下两块资产的状态与车型规格兼容性；
  3. 写换电记录；
  4. 装上电池 ready -> installed（离站、绑定车辆）；
  5. 卸下电池 installed -> pending（入站待充，带回换电前电量）；
  6. 更新车辆当前电池与电量；
  7. 用资产明细重算站点 battery_ready。

任一步失败抛 BusinessError，由路由统一回滚，站点汇总与明细不可能分叉。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from ..models import Battery, Station, SwapRecord, Vehicle
from . import battery_service as bs
from .battery_service import INSTALLED, PENDING, READY, BusinessError, _add_event

# 视为满电、可作为"装上"电池的 SOC 下限
READY_SWAP_MIN_SOC = 90.0


@dataclass
class SwapResult:
    record: SwapRecord
    legacy_mode: bool


def _idempotent_record(db: Session, request_id: Optional[str]) -> Optional[SwapRecord]:
    if not request_id:
        return None
    return db.query(SwapRecord).filter(SwapRecord.request_id == request_id).first()


def perform_swap(
    db: Session,
    *,
    vehicle_id: int,
    station_id: int,
    soc_before: Optional[float],
    soc_after: Optional[float],
    battery_in_serial: Optional[str],
    battery_out_serial: Optional[str],
    request_id: Optional[str],
) -> SwapResult:
    """执行换电；资产模式与兼容模式共用同一套原子迁移。"""
    existing = _idempotent_record(db, request_id)
    if existing is not None:
        return SwapResult(record=existing, legacy_mode=existing.legacy_mode)

    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        raise BusinessError("车辆不存在", 404)
    station = db.get(Station, station_id)
    if not station:
        raise BusinessError("换电站不存在", 404)

    legacy_mode = battery_in_serial is None

    if legacy_mode:
        return _perform_legacy(
            db,
            vehicle=vehicle,
            station=station,
            soc_before=soc_before,
            soc_after=soc_after,
            request_id=request_id,
        )
    return _perform_asset(
        db,
        vehicle=vehicle,
        station=station,
        soc_before=soc_before,
        soc_after=soc_after,
        battery_in_serial=battery_in_serial,
        battery_out_serial=battery_out_serial,
        request_id=request_id,
    )


def _perform_asset(
    db: Session,
    *,
    vehicle: Vehicle,
    station: Station,
    soc_before: Optional[float],
    soc_after: Optional[float],
    battery_in_serial: str,
    battery_out_serial: Optional[str],
    request_id: Optional[str],
) -> SwapResult:
    in_battery = (
        db.query(Battery).filter(Battery.serial_no == battery_in_serial).first()
    )
    if not in_battery:
        raise BusinessError(f"待装上电池 {battery_in_serial} 不存在", 404)

    # 装上电池必须是本站可换（ready）的同规格电池——隔离/退役/在充/在他站都拿不到
    if in_battery.status != READY:
        raise BusinessError(
            f"电池 {in_battery.serial_no} 当前为「{bs.STATUS_CN[in_battery.status]}」，"
            "只有可换状态的电池允许出库装车"
        )
    if in_battery.station_id != station.id:
        raise BusinessError(f"电池 {in_battery.serial_no} 不在本站，无法出库")
    if in_battery.spec != vehicle.battery_spec:
        raise BusinessError(
            f"车型不兼容：车辆要求规格 {vehicle.battery_spec}，"
            f"电池 {in_battery.serial_no} 规格为 {in_battery.spec}"
        )

    # 卸下电池必须显式指定，且确实装在该车上
    current = (
        db.get(Battery, vehicle.current_battery_id)
        if vehicle.current_battery_id
        else None
    )
    if current is not None:
        if not battery_out_serial:
            raise BusinessError(
                f"车辆已装有电池 {current.serial_no}，换电必须显式提供 battery_out_serial"
            )
        out_battery = (
            db.query(Battery).filter(Battery.serial_no == battery_out_serial).first()
        )
        if not out_battery:
            raise BusinessError(f"待卸下电池 {battery_out_serial} 不存在", 404)
        if out_battery.id != current.id:
            raise BusinessError(
                f"卸下电池与车辆实际装车电池不符：车上为 {current.serial_no}，"
                f"上报为 {out_battery.serial_no}"
            )
        if out_battery.status != INSTALLED or out_battery.vehicle_id != vehicle.id:
            raise BusinessError("卸下电池当前未处于装车状态或不属于该车辆")
    elif battery_out_serial:
        raise BusinessError("车辆当前没有装车电池，不应提供 battery_out_serial")
    else:
        out_battery = None

    # SOC 默认值：卸下前取车辆当前电量，装上后取电池实际电量
    eff_soc_before = vehicle.current_soc if soc_before is None else soc_before
    eff_soc_after = in_battery.soc if soc_after is None else soc_after
    if eff_soc_after <= eff_soc_before:
        raise BusinessError("换电后电量应高于换电前电量")
    if eff_soc_after < READY_SWAP_MIN_SOC:
        raise BusinessError("装上的电池电量不足，不可作为满电电池出库")

    record = SwapRecord(
        vehicle_id=vehicle.id,
        station_id=station.id,
        battery_out_id=out_battery.id if out_battery else None,
        battery_in_id=in_battery.id,
        soc_before=eff_soc_before,
        soc_after=eff_soc_after,
        legacy_mode=False,
        request_id=request_id,
    )
    db.add(record)
    db.flush()  # 取 record.id 写台账

    # 装上：ready -> installed，离站、绑定车辆
    in_battery.status = INSTALLED
    in_battery.station_id = None
    in_battery.vehicle_id = vehicle.id
    in_battery.soc = eff_soc_after
    _add_event(
        db, in_battery, "swap_out",
        from_status=READY, to_status=INSTALLED,
        from_station_id=station.id, to_station_id=None,
        vehicle_id=vehicle.id, swap_record_id=record.id,
        soc=eff_soc_after, note=f"出库装车：{vehicle.plate}",
    )

    # 卸下：installed -> pending，入站待充，带回亏电电量
    if out_battery is not None:
        out_battery.status = PENDING
        out_battery.station_id = station.id
        out_battery.vehicle_id = None
        out_battery.soc = eff_soc_before
        _add_event(
            db, out_battery, "swap_in",
            from_status=INSTALLED, to_status=PENDING,
            from_station_id=None, to_station_id=station.id,
            vehicle_id=vehicle.id, swap_record_id=record.id,
            soc=eff_soc_before, note=f"亏电返站：{vehicle.plate}",
        )

    vehicle.current_battery_id = in_battery.id
    vehicle.current_soc = eff_soc_after

    # 一块 ready 离开本站：同事务重算汇总
    bs.recalc_station_ready(db, station.id)
    db.flush()
    return SwapResult(record=record, legacy_mode=False)


def _perform_legacy(
    db: Session,
    *,
    vehicle: Vehicle,
    station: Station,
    soc_before: Optional[float],
    soc_after: Optional[float],
    request_id: Optional[str],
) -> SwapResult:
    """兼容模式：旧调用方只传电量。系统按 FIFO 自动选同规格可换电池。"""
    if soc_before is None:
        raise BusinessError("兼容模式必须提供 soc_before（建议升级为电池序列号上报）")
    eff_soc_after = 100.0 if soc_after is None else soc_after
    if eff_soc_after <= soc_before:
        raise BusinessError("换电后电量应高于换电前电量")

    in_battery = (
        db.query(Battery)
        .filter(
            Battery.station_id == station.id,
            Battery.status == READY,
            Battery.spec == vehicle.battery_spec,
        )
        .order_by(Battery.updated_at.asc(), Battery.id.asc())
        .first()
    )
    if not in_battery:
        raise BusinessError("该换电站暂无与车型兼容的满电电池可换")

    out_battery = (
        db.get(Battery, vehicle.current_battery_id)
        if vehicle.current_battery_id
        else None
    )

    record = SwapRecord(
        vehicle_id=vehicle.id,
        station_id=station.id,
        battery_out_id=out_battery.id if out_battery else None,
        battery_in_id=in_battery.id,
        soc_before=soc_before,
        soc_after=eff_soc_after,
        legacy_mode=True,
        request_id=request_id,
    )
    db.add(record)
    db.flush()

    in_battery.status = INSTALLED
    in_battery.station_id = None
    in_battery.vehicle_id = vehicle.id
    in_battery.soc = eff_soc_after
    _add_event(
        db, in_battery, "swap_out",
        from_status=READY, to_status=INSTALLED,
        from_station_id=station.id, to_station_id=None,
        vehicle_id=vehicle.id, swap_record_id=record.id,
        soc=eff_soc_after, note=f"[兼容模式] 自动选包出库：{vehicle.plate}",
    )

    if out_battery is not None:
        out_battery.status = PENDING
        out_battery.station_id = station.id
        out_battery.vehicle_id = None
        out_battery.soc = soc_before
        _add_event(
            db, out_battery, "swap_in",
            from_status=INSTALLED, to_status=PENDING,
            from_station_id=None, to_station_id=station.id,
            vehicle_id=vehicle.id, swap_record_id=record.id,
            soc=soc_before, note=f"[兼容模式] 亏电返站：{vehicle.plate}",
        )

    vehicle.current_battery_id = in_battery.id
    vehicle.current_soc = eff_soc_after
    bs.recalc_station_ready(db, station.id)
    db.flush()
    return SwapResult(record=record, legacy_mode=True)
