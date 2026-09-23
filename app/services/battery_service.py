"""电池资产领域服务。

所有改变电池状态、车辆、站点库存、换电记录的操作都集中在本模块，
并保证：

1. 合法迁移：每次状态变更都对照 ALLOWED_TRANSITIONS 校验；
2. 原子事务：换电涉及的两块电池、车辆、换电记录、事件在同一事务内
   提交，任何一步失败整体回滚，站点汇总数（由明细实时派生）不会与
   资产明细分叉；
3. 并发安全：状态落库用带状态前置条件的原子 UPDATE（compare-and-swap），
   并发的两次换电不可能把同一块满电电池出两次库；
4. 幂等：换电支持 request_id 唯一键，重复上报直接返回首条记录，
   不会产生第二次状态迁移。
"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from ..models import (
    ALLOWED_TRANSITIONS,
    BATTERY_STATES,
    EVENT_CHARGE_COMPLETE,
    EVENT_CHARGE_START,
    EVENT_ISOLATE,
    EVENT_REGISTER,
    EVENT_RETIRE,
    EVENT_SWAP_IN,
    EVENT_SWAP_OUT,
    EVENT_TRANSFER,
    EVENT_UNISOLATE,
    STATE_CHARGING,
    STATE_INSTALLED,
    STATE_ISOLATED,
    STATE_PENDING,
    STATE_READY,
    STATE_RETIRED,
    TRANSFERABLE_STATES,
    Battery,
    BatteryEvent,
    Station,
    SwapRecord,
    Vehicle,
)

# 兼容老调用（不传电池序列号）时，自然键去重的时间窗（秒）
LEGACY_DEDUP_WINDOW_SECONDS = 60

_UNSET = object()


class Conflict(HTTPException):
    """业务冲突（409）。"""

    def __init__(self, detail: str):
        super().__init__(status_code=409, detail=detail)


class Unprocessable(HTTPException):
    """业务规则不满足（422）。"""

    def __init__(self, detail: str):
        super().__init__(status_code=422, detail=detail)


# ---------------------------------------------------------------- 库存派生
def station_inventory(db: Session, station_id: int) -> dict[str, int]:
    """由电池明细实时聚合站点各状态数量。

    汇总数是明细的只读投影，绝不单独落库维护，因此不可能与明细分叉。
    """
    rows = dict(
        db.query(Battery.state, func.count(Battery.id))
        .filter(Battery.station_id == station_id)
        .group_by(Battery.state)
        .all()
    )
    return {state: int(rows.get(state, 0)) for state in BATTERY_STATES}


def station_battery_ready(db: Session, station_id: int) -> int:
    """站点可换（满电）电池数。"""
    return (
        db.query(func.count(Battery.id))
        .filter(Battery.station_id == station_id, Battery.state == STATE_READY)
        .scalar()
        or 0
    )


def station_on_site_total(db: Session, station_id: int) -> int:
    """站内电池总数（装车在外、退役出库的不计入仓位占用）。"""
    return db.query(func.count(Battery.id)).filter(Battery.station_id == station_id).scalar() or 0


def _assert_capacity(db: Session, station: Station, *, incoming: int = 1) -> None:
    """仓位容量约束：站内电池数（含新增/调入）不得超过仓位总数。"""
    if station.slot_total <= 0:
        raise Unprocessable(f"站点 {station.name} 未配置电池仓位（slot_total=0），无法存放电池")
    used = station_on_site_total(db, station.id)
    if used + incoming > station.slot_total:
        raise Unprocessable(
            f"站点 {station.name} 仓位不足：现有 {used} 块，仓位 {station.slot_total}，"
            f"无法再放入 {incoming} 块"
        )


# ---------------------------------------------------------------- 原语
def _guard(battery: Battery, to_state: str) -> None:
    """只读校验状态机合法性，不改内存对象。"""
    if to_state not in ALLOWED_TRANSITIONS.get(battery.state, frozenset()):
        raise Unprocessable(
            f"电池 {battery.serial_no} 状态 {battery.state} 不允许迁移到 {to_state}"
        )


def _cas(
    db: Session,
    battery: Battery,
    *,
    expect_state: str,
    expect_station=_UNSET,
    expect_vehicle=_UNSET,
    **set_values,
) -> bool:
    """带前置条件的原子状态更新（compare-and-swap）。

    UPDATE batteries SET ... WHERE id=? AND state=? [AND station_id=? ...]
    仅当行仍处于预期状态时才生效，返回是否抢占成功。成功后令会话内对象
    过期，后续访问读到库中最新值，避免脏内存覆盖。
    """
    conditions = [Battery.id == battery.id, Battery.state == expect_state]
    if expect_station is not _UNSET:
        conditions.append(Battery.station_id == expect_station)
    if expect_vehicle is not _UNSET:
        conditions.append(Battery.vehicle_id == expect_vehicle)
    result = db.execute(
        update(Battery).where(*conditions).values(**set_values).execution_options(
            synchronize_session=False
        )
    )
    ok = result.rowcount == 1
    if ok:
        db.expire(battery)
    return ok


def _add_event(
    db: Session,
    battery: Battery,
    event_type: str,
    *,
    from_state: Optional[str],
    to_state: Optional[str],
    from_station_id: Optional[int] = None,
    to_station_id: Optional[int] = None,
    from_vehicle_id: Optional[int] = None,
    to_vehicle_id: Optional[int] = None,
    soc: Optional[float] = None,
    swap_record_id: Optional[int] = None,
    request_id: Optional[str] = None,
    note: str = "",
) -> BatteryEvent:
    """追加一条不可改写的电池事件。

    未显式给出的 from/to 站点、车辆默认取电池当前归属，保证每一条历史
    都能还原"从哪里来、到哪里去"。
    """
    event = BatteryEvent(
        battery_id=battery.id,
        event_type=event_type,
        from_state=from_state,
        to_state=to_state,
        from_station_id=battery.station_id if from_station_id is None else from_station_id,
        to_station_id=battery.station_id if to_station_id is None else to_station_id,
        from_vehicle_id=battery.vehicle_id if from_vehicle_id is None else from_vehicle_id,
        to_vehicle_id=battery.vehicle_id if to_vehicle_id is None else to_vehicle_id,
        soc=soc if soc is not None else battery.current_soc,
        swap_record_id=swap_record_id,
        request_id=request_id,
        note=note,
    )
    db.add(event)
    return event


# ---------------------------------------------------------------- 登记
def register_battery(
    db: Session,
    *,
    serial_no: str,
    spec: str,
    station_id: int,
    capacity_kwh: float = 100.0,
    health: float = 100.0,
    current_soc: float = 0.0,
    state: str = STATE_PENDING,
) -> Battery:
    """登记一块新电池资产并写入注册事件。

    仅允许以在站状态（待充/充电中/可换/隔离）登记；装车、退役不能凭空出现。
    """
    if db.query(Battery).filter(Battery.serial_no == serial_no).first():
        raise Conflict(f"电池序列号 {serial_no} 已存在")
    station = db.get(Station, station_id)
    if not station:
        raise Unprocessable("换电站不存在")
    if state not in (STATE_PENDING, STATE_CHARGING, STATE_READY, STATE_ISOLATED):
        raise Unprocessable(f"新登记电池不允许初始状态 {state}")
    _assert_capacity(db, station)

    battery = Battery(
        serial_no=serial_no,
        spec=spec,
        capacity_kwh=capacity_kwh,
        health=health,
        current_soc=current_soc,
        state=state,
        station_id=station_id,
        vehicle_id=None,
    )
    db.add(battery)
    db.flush()  # 取 battery.id 供事件使用
    db.add(
        BatteryEvent(
            battery_id=battery.id,
            event_type=EVENT_REGISTER,
            from_state=None,
            to_state=state,
            from_station_id=None,
            to_station_id=station_id,
            from_vehicle_id=None,
            to_vehicle_id=None,
            soc=current_soc,
            note="资产登记",
        )
    )
    return battery


# ---------------------------------------------------------------- 充电
def start_charging(db: Session, battery: Battery) -> Battery:
    """待充 → 充电中。"""
    _guard(battery, STATE_CHARGING)
    if not _cas(db, battery, expect_state=STATE_PENDING, state=STATE_CHARGING):
        raise Unprocessable(f"电池 {battery.serial_no} 状态已变化，请刷新后重试")
    _add_event(db, battery, EVENT_CHARGE_START, from_state=STATE_PENDING, to_state=STATE_CHARGING)
    return battery


def complete_charging(
    db: Session,
    battery: Battery,
    *,
    soc: float = 100.0,
    health: Optional[float] = None,
    request_id: Optional[str] = None,
) -> Battery:
    """充电完成：充电中 → 可换，记录满电电量，可选更新健康度。

    已处于可换的电池重复上报充电完成：直接幂等返回，不再产生事件，
    也不覆盖既有电量/健康度。
    """
    if battery.state == STATE_READY:
        return battery
    _guard(battery, STATE_READY)
    values = {"state": STATE_READY, "current_soc": soc}
    if health is not None:
        values["health"] = health
    if not _cas(db, battery, expect_state=STATE_CHARGING, **values):
        raise Unprocessable(f"电池 {battery.serial_no} 状态已变化，请刷新后重试")
    _add_event(
        db,
        battery,
        EVENT_CHARGE_COMPLETE,
        from_state=STATE_CHARGING,
        to_state=STATE_READY,
        soc=soc,
        request_id=request_id,
        note="充电完成",
    )
    return battery


# ---------------------------------------------------------------- 调拨
def transfer_battery(
    db: Session,
    battery: Battery,
    *,
    to_station_id: int,
) -> Battery:
    """在站点间调拨一块在仓电池（仅待充/可换）。"""
    if battery.state not in TRANSFERABLE_STATES:
        raise Unprocessable(f"电池 {battery.serial_no} 当前状态 {battery.state} 不可调拨")
    from_station_id = battery.station_id
    if from_station_id == to_station_id:
        raise Unprocessable("目标站点与当前站点相同")
    target = db.get(Station, to_station_id)
    if not target:
        raise Unprocessable("目标换电站不存在")
    _assert_capacity(db, target)  # 调入一块，目标站不得超仓位

    if not _cas(
        db,
        battery,
        expect_state=battery.state,
        expect_station=from_station_id,
        station_id=to_station_id,
    ):
        raise Unprocessable(f"电池 {battery.serial_no} 状态已变化，请刷新后重试")
    _add_event(
        db,
        battery,
        EVENT_TRANSFER,
        from_state=battery.state,
        to_state=battery.state,
        from_station_id=from_station_id,
        to_station_id=to_station_id,
        note="站点调拨",
    )
    return battery


# ---------------------------------------------------------------- 隔离
def isolate_battery(db: Session, battery: Battery, *, reason: str = "") -> Battery:
    """隔离电池。

    车上电池（installed）不允许直接隔离，必须先经换电卸下回站，
    防止"车带着被召回电池继续跑"。
    """
    if battery.state == STATE_ISOLATED:
        return battery  # 幂等：重复隔离不产生第二条事件
    previous_state = battery.state
    _guard(battery, STATE_ISOLATED)
    if not _cas(db, battery, expect_state=previous_state, state=STATE_ISOLATED):
        raise Unprocessable(f"电池 {battery.serial_no} 状态已变化，请刷新后重试")
    _add_event(
        db,
        battery,
        EVENT_ISOLATE,
        from_state=previous_state,
        to_state=STATE_ISOLATED,
        note=reason or "隔离",
    )
    return battery


def unisolate_battery(db: Session, battery: Battery) -> Battery:
    """解除隔离：隔离 → 待充（需重新检测充电后才能再出库）。"""
    _guard(battery, STATE_PENDING)
    if not _cas(db, battery, expect_state=STATE_ISOLATED, state=STATE_PENDING):
        raise Unprocessable(f"电池 {battery.serial_no} 状态已变化，请刷新后重试")
    _add_event(
        db,
        battery,
        EVENT_UNISOLATE,
        from_state=STATE_ISOLATED,
        to_state=STATE_PENDING,
        note="解除隔离",
    )
    return battery


def retire_battery(db: Session, battery: Battery, *, reason: str = "") -> Battery:
    """退役：仅隔离电池可退役，退役为终态，并脱离站点仓位。"""
    _guard(battery, STATE_RETIRED)
    last_station_id = battery.station_id  # CAS 后会置空，先快照用于历史事件
    if not _cas(
        db, battery, expect_state=STATE_ISOLATED, state=STATE_RETIRED, station_id=None
    ):
        raise Unprocessable(f"电池 {battery.serial_no} 状态已变化，请刷新后重试")
    _add_event(
        db,
        battery,
        EVENT_RETIRE,
        from_state=STATE_ISOLATED,
        to_state=STATE_RETIRED,
        from_station_id=last_station_id,
        to_station_id=None,
        note=reason or "退役",
    )
    return battery


# ---------------------------------------------------------------- 换电
def _spec_compatible(vehicle: Vehicle, battery: Battery) -> bool:
    """车型兼容性：车辆声明了 battery_spec 时必须与电池规格一致。"""
    return not vehicle.battery_spec or vehicle.battery_spec == battery.spec


def perform_swap(
    db: Session,
    *,
    vehicle: Vehicle,
    station: Station,
    soc_before: float,
    soc_after: float,
    installed_serial: Optional[str] = None,
    removed_serial: Optional[str] = None,
    request_id: Optional[str] = None,
    is_legacy: bool = False,
) -> SwapRecord:
    """执行一次换电，单事务内完成全部状态迁移。

    步骤（任一校验失败抛异常，由调用方回滚，不产生任何落库变更）：
      1. request_id 幂等检查；
      2. 确定装上电池（指定序列号，或老调用自动选配），要求可换、
         在本站、规格与车型兼容、未隔离；
      3. 确定卸下电池（指定序列号，或取车辆当前在册电池，可空）；
      4. 写换电记录；
      5. 原子抢占装上电池 ready → installed（归车）；
      6. 原子抢占卸下电池 installed → pending_charge（归站、低电量）；
      7. 更新车辆电量；两块电池各写一条 swap_in/swap_out 事件。
    """
    # 1. 显式幂等键
    if request_id:
        existing = db.query(SwapRecord).filter(SwapRecord.request_id == request_id).first()
        if existing:
            return existing

    if soc_after <= soc_before:
        raise Unprocessable("换电后电量应高于换电前电量")

    # 2. 装上的满电电池
    if installed_serial:
        installed = db.query(Battery).filter(Battery.serial_no == installed_serial).first()
        if not installed:
            raise Unprocessable(f"装上电池 {installed_serial} 不存在")
        if installed.state != STATE_READY:
            raise Unprocessable(
                f"装上电池 {installed_serial} 当前状态 {installed.state}，不可换电"
            )
        if installed.station_id != station.id:
            raise Unprocessable("装上电池不在该换电站")
    else:
        # 兼容老调用：在本站可换电池中按规格、健康度选最优一块
        query = db.query(Battery).filter(
            Battery.station_id == station.id,
            Battery.state == STATE_READY,
        )
        if vehicle.battery_spec:
            query = query.filter(Battery.spec == vehicle.battery_spec)
        installed = query.order_by(Battery.health.desc(), Battery.id).first()
        if not installed:
            raise Unprocessable("该换电站无兼容的满电电池可换")

    if not _spec_compatible(vehicle, installed):
        raise Unprocessable(
            f"车型兼容电池规格为 {vehicle.battery_spec}，"
            f"电池 {installed.serial_no} 规格为 {installed.spec}"
        )

    # 3. 卸下的亏电电池
    removed: Optional[Battery] = None
    if removed_serial:
        removed = db.query(Battery).filter(Battery.serial_no == removed_serial).first()
        if not removed:
            raise Unprocessable(f"卸下电池 {removed_serial} 不存在")
        if removed.state != STATE_INSTALLED or removed.vehicle_id != vehicle.id:
            raise Unprocessable(f"卸下电池 {removed_serial} 未装在该车辆上")
    else:
        # 老车辆可能没有在册电池，允许为空
        removed = (
            db.query(Battery)
            .filter(Battery.vehicle_id == vehicle.id, Battery.state == STATE_INSTALLED)
            .first()
        )

    # 4. 换电记录（先落库以取得 id；后续抢占失败会整体回滚，记录不会残留）
    record = SwapRecord(
        vehicle_id=vehicle.id,
        station_id=station.id,
        soc_before=soc_before,
        soc_after=soc_after,
        removed_battery_id=removed.id if removed else None,
        installed_battery_id=installed.id,
        request_id=request_id,
        is_legacy=is_legacy,
    )
    db.add(record)
    db.flush()

    # 5. 原子抢占装上电池：必须仍在本站且可换
    if not _cas(
        db,
        installed,
        expect_state=STATE_READY,
        expect_station=station.id,
        state=STATE_INSTALLED,
        station_id=None,
        vehicle_id=vehicle.id,
        current_soc=soc_after,
    ):
        raise Unprocessable(
            f"装上电池 {installed.serial_no} 已不可用（可能刚被其他换电占用或被隔离）"
        )

    # 6. 原子抢占卸下电池：必须仍装在该车上
    if removed is not None:
        if not _cas(
            db,
            removed,
            expect_state=STATE_INSTALLED,
            expect_vehicle=vehicle.id,
            state=STATE_PENDING,
            station_id=station.id,
            vehicle_id=None,
            current_soc=soc_before,
        ):
            raise Unprocessable(
                f"卸下电池 {removed.serial_no} 已不在该车辆上（可能存在并发操作）"
            )

    # 7. 事件 + 车辆（事件用显式 from/to，不依赖可能已过期的内存归属）
    db.add(
        BatteryEvent(
            battery_id=installed.id,
            event_type=EVENT_SWAP_IN,
            from_state=STATE_READY,
            to_state=STATE_INSTALLED,
            from_station_id=station.id,
            to_station_id=None,
            from_vehicle_id=None,
            to_vehicle_id=vehicle.id,
            soc=soc_after,
            swap_record_id=record.id,
            request_id=request_id,
            note="换电装上",
        )
    )
    if removed is not None:
        db.add(
            BatteryEvent(
                battery_id=removed.id,
                event_type=EVENT_SWAP_OUT,
                from_state=STATE_INSTALLED,
                to_state=STATE_PENDING,
                from_station_id=None,
                to_station_id=station.id,
                from_vehicle_id=vehicle.id,
                to_vehicle_id=None,
                soc=soc_before,
                swap_record_id=record.id,
                request_id=request_id,
                note="换电卸下",
            )
        )

    vehicle.current_soc = soc_after
    if vehicle.status != "fault":  # 故障状态不因换电被掩盖
        if vehicle.status == "idle":
            vehicle.status = "running"
    db.flush()
    return record


def find_legacy_duplicate(
    db: Session,
    *,
    vehicle_id: int,
    station_id: int,
    soc_before: float,
    soc_after: float,
) -> Optional[SwapRecord]:
    """老调用（无序列号、无 request_id）的自然键去重。

    短时间窗内同一车辆、站点、电量完全相同的换电视为重复上报，
    返回首条记录而不是再造一次迁移。
    """
    cutoff_dt = datetime.utcnow() - timedelta(seconds=LEGACY_DEDUP_WINDOW_SECONDS)
    return (
        db.query(SwapRecord)
        .filter(
            SwapRecord.vehicle_id == vehicle_id,
            SwapRecord.station_id == station_id,
            SwapRecord.soc_before == soc_before,
            SwapRecord.soc_after == soc_after,
            SwapRecord.is_legacy.is_(True),
            SwapRecord.request_id.is_(None),
            SwapRecord.swapped_at >= cutoff_dt,
        )
        .order_by(SwapRecord.id.desc())
        .first()
    )


def get_battery_or_404(db: Session, serial_no: str) -> Battery:
    battery = db.query(Battery).filter(Battery.serial_no == serial_no).first()
    if not battery:
        raise HTTPException(status_code=404, detail=f"电池 {serial_no} 不存在")
    return battery


def battery_history(db: Session, battery: Battery) -> list[BatteryEvent]:
    """按序列号查询的完整迁移历史（时间正序，最早的登记在最前）。"""
    return (
        db.query(BatteryEvent)
        .filter(BatteryEvent.battery_id == battery.id)
        .order_by(BatteryEvent.created_at.asc(), BatteryEvent.id.asc())
        .all()
    )
