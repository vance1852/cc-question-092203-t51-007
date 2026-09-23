"""首次启动时初始化数据库：建表 + 内置管理员 + 种子业务数据。

种子数据中的站点库存数完全由电池资产明细派生：每个站点登记相应数量的
待充/充电中/可换/隔离电池，每辆车挂一块装车电池，保证"汇总数 = 明细数"。
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .auth import hash_password
from .config import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME
from .database import Base, SessionLocal, engine
from .migrate import migrate
from .models import (
    EVENT_REGISTER,
    EVENT_SWAP_IN,
    STATE_CHARGING,
    STATE_INSTALLED,
    STATE_ISOLATED,
    STATE_PENDING,
    STATE_READY,
    Battery,
    BatteryEvent,
    Station,
    SwapRecord,
    User,
    Vehicle,
)


def init_db() -> None:
    """创建所有表、执行迁移并灌入种子数据（幂等：已存在则跳过）。"""
    Base.metadata.create_all(bind=engine)
    migrate(engine)
    db: Session = SessionLocal()
    try:
        _seed_admin(db)
        _seed_business(db)
        db.commit()
    finally:
        db.close()


def _seed_admin(db: Session) -> None:
    if db.query(User).filter(User.username == DEFAULT_ADMIN_USERNAME).first():
        return
    db.add(
        User(
            username=DEFAULT_ADMIN_USERNAME,
            password_hash=hash_password(DEFAULT_ADMIN_PASSWORD),
            display_name="平台管理员",
        )
    )


# 规格代码：按车型电量分档
SPEC_S, SPEC_M, SPEC_L, SPEC_XL = "STD-S", "STD-M", "STD-L", "STD-XL"
SPEC_CAPACITY = {
    SPEC_S: 42.0,
    SPEC_M: 100.0,
    SPEC_L: 140.0,
    SPEC_XL: 160.0,
}


def _add_battery(
    db: Session,
    *,
    serial_no: str,
    spec: str,
    state: str,
    soc: float,
    health: float = 100.0,
    station_id=None,
    vehicle_id=None,
    installed_at_station_id=None,
) -> Battery:
    """登记一块种子电池，并写注册事件（装车电池再补一条装上事件）。"""
    battery = Battery(
        serial_no=serial_no,
        spec=spec,
        capacity_kwh=SPEC_CAPACITY[spec],
        health=health,
        state=state,
        current_soc=soc,
        station_id=station_id,
        vehicle_id=vehicle_id,
    )
    db.add(battery)
    db.flush()
    db.add(
        BatteryEvent(
            battery_id=battery.id,
            event_type=EVENT_REGISTER,
            from_state=None,
            to_state=state,
            from_station_id=None,
            to_station_id=installed_at_station_id if state == STATE_INSTALLED else station_id,
            to_vehicle_id=vehicle_id,
            soc=soc,
            note="资产登记（种子）",
        )
    )
    if state == STATE_INSTALLED:
        db.add(
            BatteryEvent(
                battery_id=battery.id,
                event_type=EVENT_SWAP_IN,
                from_state=STATE_READY,
                to_state=STATE_INSTALLED,
                from_station_id=installed_at_station_id,
                to_station_id=None,
                from_vehicle_id=None,
                to_vehicle_id=vehicle_id,
                soc=soc,
                note="换电装上（种子）",
            )
        )
    return battery


def _seed_business(db: Session) -> None:
    if db.query(Station).count() > 0:
        return

    stations = [
        Station(name="城东物流园换电站", address="城东大道 128 号", slot_total=20, status="running"),
        Station(name="临港枢纽换电站", address="临港四路 9 号", slot_total=16, status="running"),
        Station(name="北郊配送中心换电站", address="北环高速出口 3 公里", slot_total=12, status="maintenance"),
        Station(name="高新园区换电站", address="科创路 66 号", slot_total=24, status="running"),
    ]
    db.add_all(stations)
    db.flush()

    vehicles = [
        Vehicle(plate="沪EV1234", model="远程星瀚 H", battery_capacity=141.0, battery_spec=SPEC_L,
                current_soc=82.0, status="running"),
        Vehicle(plate="沪EV5678", model="比亚迪 T5", battery_capacity=100.0, battery_spec=SPEC_M,
                current_soc=23.0, status="charging"),
        Vehicle(plate="苏EV9012", model="江淮恺达 EX8", battery_capacity=120.0, battery_spec=SPEC_L,
                current_soc=56.0, status="idle"),
        Vehicle(plate="浙EV3456", model="开瑞优优 EV", battery_capacity=42.0, battery_spec=SPEC_S,
                current_soc=9.0, status="fault"),
        Vehicle(plate="沪EV7788", model="远程星智 G", battery_capacity=160.0, battery_spec=SPEC_XL,
                current_soc=95.0, status="running"),
    ]
    db.add_all(vehicles)
    db.flush()

    # 车辆当前装车电池（last swap station 与历史换电记录对应）
    _add_battery(db, serial_no="BT-V-0001", spec=SPEC_L, state=STATE_INSTALLED, soc=82.0,
                 vehicle_id=vehicles[0].id, installed_at_station_id=stations[0].id)
    _add_battery(db, serial_no="BT-V-0002", spec=SPEC_M, state=STATE_INSTALLED, soc=23.0,
                 vehicle_id=vehicles[1].id, installed_at_station_id=stations[1].id)
    _add_battery(db, serial_no="BT-V-0003", spec=SPEC_L, state=STATE_INSTALLED, soc=56.0,
                 vehicle_id=vehicles[2].id, installed_at_station_id=stations[0].id)
    _add_battery(db, serial_no="BT-V-0004", spec=SPEC_S, state=STATE_INSTALLED, soc=9.0,
                 health=71.5, vehicle_id=vehicles[3].id, installed_at_station_id=stations[2].id)
    _add_battery(db, serial_no="BT-V-0005", spec=SPEC_XL, state=STATE_INSTALLED, soc=95.0,
                 vehicle_id=vehicles[4].id, installed_at_station_id=stations[3].id)

    # 各站站内资产：(数量, 状态, 规格循环)
    plan = [
        # 站0：14 可换 + 2 充电中 + 2 待充
        (stations[0].id, 14, STATE_READY, [SPEC_M, SPEC_L, SPEC_S, SPEC_XL]),
        (stations[0].id, 2, STATE_CHARGING, [SPEC_L, SPEC_M]),
        (stations[0].id, 2, STATE_PENDING, [SPEC_S, SPEC_M]),
        # 站1：11 可换 + 3 待充
        (stations[1].id, 11, STATE_READY, [SPEC_M, SPEC_L, SPEC_XL]),
        (stations[1].id, 3, STATE_PENDING, [SPEC_M, SPEC_S, SPEC_L]),
        # 站2（维护中）：4 可换 + 1 隔离 + 2 待充
        (stations[2].id, 4, STATE_READY, [SPEC_S, SPEC_M]),
        (stations[2].id, 1, STATE_ISOLATED, [SPEC_M]),
        (stations[2].id, 2, STATE_PENDING, [SPEC_S]),
        # 站3：20 可换 + 2 充电中
        (stations[3].id, 20, STATE_READY, [SPEC_XL, SPEC_L, SPEC_M, SPEC_S]),
        (stations[3].id, 2, STATE_CHARGING, [SPEC_XL]),
    ]
    seq = 1
    for station_id, count, state, specs in plan:
        for i in range(count):
            spec = specs[i % len(specs)]
            soc = 100.0 if state == STATE_READY else (45.0 if state == STATE_CHARGING else 8.0)
            # 隔离一块健康度偏低的电池作为召回示例
            health = 68.0 if state == STATE_ISOLATED else 100.0
            _add_battery(
                db,
                serial_no=f"BT-{seq:05d}",
                spec=spec,
                state=state,
                soc=soc,
                health=health,
                station_id=station_id,
            )
            seq += 1

    now = datetime.utcnow()
    swaps = [
        SwapRecord(vehicle_id=vehicles[0].id, station_id=stations[0].id, soc_before=12.0, soc_after=100.0, swapped_at=now - timedelta(hours=2)),
        SwapRecord(vehicle_id=vehicles[1].id, station_id=stations[1].id, soc_before=8.0, soc_after=98.0, swapped_at=now - timedelta(hours=5)),
        SwapRecord(vehicle_id=vehicles[2].id, station_id=stations[0].id, soc_before=15.0, soc_after=100.0, swapped_at=now - timedelta(days=1, hours=1)),
        SwapRecord(vehicle_id=vehicles[4].id, station_id=stations[3].id, soc_before=20.0, soc_after=100.0, swapped_at=now - timedelta(minutes=40)),
    ]
    db.add_all(swaps)
