"""电池资产领域服务：状态机、生命周期操作与站点汇总维护。

所有电池状态变更必须且只能经过本模块的函数；每个函数都在调用方的
数据库事务内执行，保证"两块资产 + 车辆 + 站点计数 + 换电记录 + 台账"
要么全部生效，要么全部回滚，站点汇总数永远不会与资产明细分叉。

合法迁移图：
    register                       -> pending
    charge_start     pending       -> charging
    charge_complete  charging      -> ready
    swap_out         ready         -> installed（满电出库装车）
    swap_in          installed     -> pending（亏电返站待充）
    transfer         在站三态       -> 同状态（仅换站）
    isolate          除 retired    -> isolated
    restore          isolated      -> pending / ready（按当前电量）
    retire           除 retired    -> retired

隔离只能经 restore 离开；退役为终态。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from ..models import Battery, BatteryEvent, Station, Vehicle

# 电池状态
PENDING = "pending"
CHARGING = "charging"
READY = "ready"
INSTALLED = "installed"
ISOLATED = "isolated"
RETIRED = "retired"

# 在站（受站点仓位管理）的三种状态
ON_SITE_STATUSES = (PENDING, CHARGING, READY)

STATUS_CN = {
    PENDING: "待充",
    CHARGING: "充电中",
    READY: "可换",
    INSTALLED: "装车",
    ISOLATED: "隔离",
    RETIRED: "退役",
}

# 操作 -> 允许的起始状态
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "register": frozenset(),
    "charge_start": frozenset({PENDING}),
    "charge_complete": frozenset({CHARGING}),
    "swap_out": frozenset({READY}),
    "swap_in": frozenset({INSTALLED}),
    "transfer": frozenset({PENDING, CHARGING, READY}),
    "isolate": frozenset({PENDING, CHARGING, READY, INSTALLED, ISOLATED}),
    "restore": frozenset({ISOLATED}),
    "retire": frozenset({PENDING, CHARGING, READY, INSTALLED, ISOLATED}),
}


class BusinessError(Exception):
    """可映射为 HTTP 4xx 的业务错误，带状态码。"""

    def __init__(self, detail: str, status_code: int = 422):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def get_battery(db: Session, serial_no: str) -> Battery:
    battery = db.query(Battery).filter(Battery.serial_no == serial_no).first()
    if not battery:
        raise BusinessError(f"电池 {serial_no} 不存在", 404)
    return battery


def replay_if_seen(db: Session, idempotency_key: Optional[str]) -> Optional[Battery]:
    """幂等键命中已有台账时，返回对应电池（调用方应直接返回，不再迁移）。"""
    if not idempotency_key:
        return None
    event = (
        db.query(BatteryEvent)
        .filter(BatteryEvent.idempotency_key == idempotency_key)
        .first()
    )
    if event is None:
        return None
    return db.get(Battery, event.battery_id)


def assert_transition(event_type: str, current: str) -> None:
    allowed = ALLOWED_TRANSITIONS[event_type]
    if current not in allowed:
        allowed_cn = "、".join(STATUS_CN[s] for s in sorted(allowed))
        raise BusinessError(
            f"电池当前为「{STATUS_CN.get(current, current)}」状态，不支持该操作"
            f"（允许的来源状态：{allowed_cn}）"
        )


def _add_event(
    db: Session,
    battery: Battery,
    event_type: str,
    *,
    from_status: Optional[str],
    to_status: Optional[str],
    from_station_id: Optional[int],
    to_station_id: Optional[int],
    vehicle_id: Optional[int] = None,
    swap_record_id: Optional[int] = None,
    soc: Optional[float] = None,
    soh: Optional[float] = None,
    note: str = "",
    idempotency_key: Optional[str] = None,
) -> BatteryEvent:
    """追加一条台账（不单独 commit，随外层事务提交）。"""
    event = BatteryEvent(
        battery_id=battery.id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        from_station_id=from_station_id,
        to_station_id=to_station_id,
        vehicle_id=vehicle_id,
        swap_record_id=swap_record_id,
        soc=soc if soc is not None else battery.soc,
        soh=soh if soh is not None else battery.soh,
        note=note,
        idempotency_key=idempotency_key,
    )
    db.add(event)
    return event


def recalc_station_ready(db: Session, station_id: Optional[int]) -> None:
    """用资产明细重算并回写站点 battery_ready（须在迁移所在事务内调用）。"""
    if station_id is None:
        return
    # 会话配置了 autoflush=False，先落盘未提交的资产变更，COUNT 才能读到新状态
    db.flush()
    count = (
        db.query(func.count(Battery.id))
        .filter(Battery.station_id == station_id, Battery.status == READY)
        .scalar()
    )
    station = db.get(Station, station_id)
    if station is not None:
        station.battery_ready = int(count or 0)


def _unlink_vehicle(db: Session, battery: Battery, vehicle_id: Optional[int]) -> None:
    """装车中的电池被隔离/退役时，在同一事务内解除车辆对它的引用。"""
    if vehicle_id is None:
        return
    vehicle = db.get(Vehicle, vehicle_id)
    if vehicle is not None and vehicle.current_battery_id == battery.id:
        vehicle.current_battery_id = None


def register_battery(
    db: Session,
    *,
    serial_no: str,
    spec: str,
    capacity_kwh: float,
    soh: float,
    soc: float,
    station_id: Optional[int],
    idempotency_key: Optional[str] = None,
) -> Battery:
    if db.query(Battery).filter(Battery.serial_no == serial_no).first():
        raise BusinessError(f"电池序列号 {serial_no} 已存在", 409)
    if station_id is not None and not db.get(Station, station_id):
        raise BusinessError("所在站点不存在", 404)

    battery = Battery(
        serial_no=serial_no,
        spec=spec,
        capacity_kwh=capacity_kwh,
        soh=soh,
        soc=soc,
        status=PENDING,
        station_id=station_id,
    )
    db.add(battery)
    db.flush()  # 取 id
    _add_event(
        db,
        battery,
        "register",
        from_status=None,
        to_status=PENDING,
        from_station_id=None,
        to_station_id=station_id,
        soc=soc,
        soh=soh,
        note="资产建档",
        idempotency_key=idempotency_key,
    )
    db.flush()
    return battery


def start_charging(db: Session, serial_no: str, *, idempotency_key: Optional[str] = None) -> Battery:
    """待充 -> 充电中。"""
    if (seen := replay_if_seen(db, idempotency_key)) is not None:
        return seen
    battery = get_battery(db, serial_no)
    assert_transition("charge_start", battery.status)
    station_id = battery.station_id
    battery.status = CHARGING
    _add_event(
        db, battery, "charge_start",
        from_status=PENDING, to_status=CHARGING,
        from_station_id=station_id, to_station_id=station_id,
        note="开始充电",
        idempotency_key=idempotency_key,
    )
    db.flush()
    return battery


def complete_charging(
    db: Session,
    serial_no: str,
    *,
    soc: float,
    soh: Optional[float] = None,
    idempotency_key: Optional[str] = None,
) -> Battery:
    """充电中 -> 可换；同事务重算站点汇总。"""
    if (seen := replay_if_seen(db, idempotency_key)) is not None:
        return seen
    battery = get_battery(db, serial_no)
    assert_transition("charge_complete", battery.status)
    station_id = battery.station_id
    battery.soc = soc
    if soh is not None:
        battery.soh = soh
    battery.status = READY
    _add_event(
        db, battery, "charge_complete",
        from_status=CHARGING, to_status=READY,
        from_station_id=station_id, to_station_id=station_id,
        soc=soc, soh=battery.soh,
        note="充电完成，转入可换",
        idempotency_key=idempotency_key,
    )
    recalc_station_ready(db, station_id)
    db.flush()
    return battery


def transfer_battery(
    db: Session,
    serial_no: str,
    *,
    to_station_id: int,
    idempotency_key: Optional[str] = None,
) -> Battery:
    """调拨：在站电池（待充/充电中/可换）调到另一站点，状态不变。"""
    if (seen := replay_if_seen(db, idempotency_key)) is not None:
        return seen
    battery = get_battery(db, serial_no)
    assert_transition("transfer", battery.status)
    if not db.get(Station, to_station_id):
        raise BusinessError("目标站点不存在", 404)
    from_station = battery.station_id
    if from_station == to_station_id:
        raise BusinessError("电池已在目标站点，无需调拨")
    battery.station_id = to_station_id
    _add_event(
        db, battery, "transfer",
        from_status=battery.status, to_status=battery.status,
        from_station_id=from_station, to_station_id=to_station_id,
        note="站点调拨",
        idempotency_key=idempotency_key,
    )
    # 可换电池跨站，两个站点的汇总都要在本事务内重算
    if battery.status == READY:
        recalc_station_ready(db, from_station)
        recalc_station_ready(db, to_station_id)
    db.flush()
    return battery


def isolate_battery(
    db: Session,
    serial_no: str,
    *,
    reason: str,
    idempotency_key: Optional[str] = None,
) -> Battery:
    """隔离（召回冻结）：除退役外均可隔离；隔离后不能出库装车。"""
    if (seen := replay_if_seen(db, idempotency_key)) is not None:
        return seen
    battery = get_battery(db, serial_no)
    assert_transition("isolate", battery.status)
    from_status = battery.status
    station_id = battery.station_id
    vehicle_id = battery.vehicle_id
    battery.status = ISOLATED
    battery.vehicle_id = None
    _unlink_vehicle(db, battery, vehicle_id)
    _add_event(
        db, battery, "isolate",
        from_status=from_status, to_status=ISOLATED,
        from_station_id=station_id, to_station_id=station_id,
        vehicle_id=vehicle_id,
        note=f"隔离：{reason}",
        idempotency_key=idempotency_key,
    )
    recalc_station_ready(db, station_id)
    db.flush()
    return battery


def restore_battery(
    db: Session,
    serial_no: str,
    *,
    idempotency_key: Optional[str] = None,
) -> Battery:
    """解除隔离：按当前电量进入待充（<90%）或可换（>=90%）。"""
    if (seen := replay_if_seen(db, idempotency_key)) is not None:
        return seen
    battery = get_battery(db, serial_no)
    assert_transition("restore", battery.status)
    station_id = battery.station_id
    target = READY if battery.soc >= 90.0 else PENDING
    battery.status = target
    _add_event(
        db, battery, "restore",
        from_status=ISOLATED, to_status=target,
        from_station_id=station_id, to_station_id=station_id,
        note="解除隔离",
        idempotency_key=idempotency_key,
    )
    recalc_station_ready(db, station_id)
    db.flush()
    return battery


def retire_battery(
    db: Session,
    serial_no: str,
    *,
    reason: str = "",
    idempotency_key: Optional[str] = None,
) -> Battery:
    """退役：除已退役外均可退役；退役为终态，不再参与运营操作。"""
    if (seen := replay_if_seen(db, idempotency_key)) is not None:
        return seen
    battery = get_battery(db, serial_no)
    assert_transition("retire", battery.status)
    from_status = battery.status
    station_id = battery.station_id
    vehicle_id = battery.vehicle_id
    battery.status = RETIRED
    battery.vehicle_id = None
    _unlink_vehicle(db, battery, vehicle_id)
    _add_event(
        db, battery, "retire",
        from_status=from_status,
        to_status=RETIRED,
        from_station_id=station_id,
        to_station_id=station_id,
        vehicle_id=vehicle_id,
        note=f"退役：{reason}" if reason else "退役",
        idempotency_key=idempotency_key,
    )
    recalc_station_ready(db, station_id)
    db.flush()
    return battery


def get_history(db: Session, serial_no: str) -> tuple[Battery, list[BatteryEvent]]:
    battery = get_battery(db, serial_no)
    events = (
        db.query(BatteryEvent)
        .filter(BatteryEvent.battery_id == battery.id)
        .order_by(BatteryEvent.id)
        .all()
    )
    return battery, events


def reconcile(db: Session) -> list[tuple[Station, int]]:
    """对账：返回 (站点, 资产明细实际可换数)；与 station.battery_ready 不等即分叉。"""
    ready_count = func.count(Battery.id)
    rows = (
        db.query(Station, func.coalesce(ready_count, 0))
        .outerjoin(
            Battery,
            and_(Battery.station_id == Station.id, Battery.status == READY),
        )
        .group_by(Station.id)
        .order_by(Station.id)
        .all()
    )
    return [(station, int(count)) for station, count in rows]
