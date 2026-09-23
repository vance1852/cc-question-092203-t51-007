"""首次启动时初始化数据库：建表 + 内置管理员 + 种子业务数据。

种子数据全部通过领域服务写入，保证站点 battery_ready 汇总数与
电池资产明细从一开始就完全一致、台账完整。
"""
from datetime import datetime, timedelta

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from .auth import hash_password
from .config import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME
from .database import Base, SessionLocal, engine
from .models import Station, User, Vehicle
from .services import battery_service as bs
from .services.swap_service import perform_swap


# 旧版本库（只有汇总数字的 v1 结构）需要补齐的列
_ADDED_COLUMNS = {
    "stations": [],  # v1 已含 battery_ready
    "vehicles": [
        ("battery_spec", "VARCHAR(32) NOT NULL DEFAULT 'STD-100'"),
        ("current_battery_id", "INTEGER"),
    ],
    "swap_records": [
        ("battery_out_id", "INTEGER"),
        ("battery_in_id", "INTEGER"),
        ("legacy_mode", "BOOLEAN NOT NULL DEFAULT 1"),
        ("request_id", "VARCHAR(64)"),
    ],
}


def _migrate_legacy_schema() -> None:
    """为 v1 旧库 ALTER 补列；新库/已迁移库无操作。SQLite 支持 ADD COLUMN。"""
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing:
                continue
            present = {col["name"] for col in inspector.get_columns(table)}
            for name, ddl in columns:
                if name not in present:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def init_db() -> None:
    """创建所有表、迁移旧库并灌入种子数据（幂等：已存在则跳过）。"""
    _migrate_legacy_schema()
    Base.metadata.create_all(bind=engine)
    db: Session = SessionLocal()
    try:
        _seed_admin(db)
        _seed_business(db)
        db.commit()
    finally:
        db.close()

# 规格码 -> 标称容量(kWh)
SPECS = {
    "STD-40": 42.0,
    "STD-100": 100.0,
    "STD-120": 120.0,
    "STD-140": 141.0,
    "STD-160": 160.0,
}

# 各站可换电池池的规格构成（序列号在填充时顺序生成）
READY_POOLS: dict[int, list[str]] = {
    0: ["STD-100"] * 6 + ["STD-140"] * 4 + ["STD-120"] * 2 + ["STD-40"] * 2,   # 14
    1: ["STD-100"] * 7 + ["STD-120"] * 2 + ["STD-160"] * 2,                    # 11
    2: ["STD-100"] * 2 + ["STD-40"] + ["STD-120"],                             # 4
    3: ["STD-100"] * 8 + ["STD-160"] * 6 + ["STD-140"] * 3 + ["STD-40"] * 3,   # 20
}


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


def _make_ready(db: Session, serial: str, spec: str, station_id: int, soh: float = 100.0) -> None:
    """登记一块电池并走到可换状态。"""
    bs.register_battery(
        db, serial_no=serial, spec=spec, capacity_kwh=SPECS[spec],
        soh=soh, soc=0.0, station_id=station_id,
    )
    bs.start_charging(db, serial)
    bs.complete_charging(db, serial, soc=100.0, soh=soh)


def _seed_business(db: Session) -> None:
    if db.query(Station).count() == 0:
        _seed_fresh(db)
        return
    if db.query(bs.Battery).count() == 0:
        # v1 旧库升级：站点只有汇总数字，没有资产明细，
        # 按各站现存 battery_ready 回填同数量可换电池，使汇总与明细立即一致
        _backfill_legacy_assets(db)


def _backfill_legacy_assets(db: Session) -> None:
    counter = 0
    for station in db.query(Station).order_by(Station.id).all():
        for _ in range(station.battery_ready):
            counter += 1
            _make_ready(db, f"LEGACY-S{station.id}-{counter:04d}", "STD-100", station.id)
        bs.recalc_station_ready(db, station.id)


def _seed_fresh(db: Session) -> None:
    stations = [
        Station(name="城东物流园换电站", address="城东大道 128 号", slot_total=20, status="running"),
        Station(name="临港枢纽换电站", address="临港四路 9 号", slot_total=16, status="running"),
        Station(name="北郊配送中心换电站", address="北环高速出口 3 公里", slot_total=12, status="maintenance"),
        Station(name="高新园区换电站", address="科创路 66 号", slot_total=24, status="running"),
    ]
    db.add_all(stations)
    db.flush()

    # 填充各站可换电池池
    for idx, specs in READY_POOLS.items():
        for n, spec in enumerate(specs, start=1):
            # 个别电池健康度偏低，模拟疑似衰减
            soh = 83.5 if (idx == 2 and n == 1) else (91.0 if (idx == 0 and spec == "STD-40" and n == 1) else 100.0)
            _make_ready(db, f"BAT-S{idx + 1}-{n:04d}", spec, stations[idx].id, soh=soh)

    # 一些非可换状态的在站资产
    bs.register_battery(db, serial_no="BAT-S1-0101", spec="STD-100", capacity_kwh=100.0,
                        soh=88.0, soc=12.0, station_id=stations[0].id)
    bs.register_battery(db, serial_no="BAT-S1-0102", spec="STD-120", capacity_kwh=120.0,
                        soh=97.0, soc=30.0, station_id=stations[0].id)
    bs.start_charging(db, "BAT-S1-0102")
    bs.isolate_battery(db, "BAT-S1-0101", reason="巡检发现疑似衰减，待质量团队复检")

    vehicles = [
        Vehicle(plate="沪EV1234", model="远程星瀚 H", battery_spec="STD-140",
                battery_capacity=141.0, current_soc=82.0, status="running"),
        Vehicle(plate="沪EV5678", model="比亚迪 T5", battery_spec="STD-100",
                battery_capacity=100.0, current_soc=23.0, status="charging"),
        Vehicle(plate="苏EV9012", model="江淮恺达 EX8", battery_spec="STD-120",
                battery_capacity=120.0, current_soc=56.0, status="idle"),
        Vehicle(plate="浙EV3456", model="开瑞优优 EV", battery_spec="STD-40",
                battery_capacity=42.0, current_soc=9.0, status="fault"),
        Vehicle(plate="沪EV7788", model="远程星智 G", battery_spec="STD-160",
                battery_capacity=160.0, current_soc=95.0, status="running"),
    ]
    db.add_all(vehicles)
    db.flush()

    # 历史换电（资产模式）：从对应站点选一块同规格可换电池装车
    now = datetime.utcnow()
    plans = [
        # (车辆, 站点, 池内第几个同规格电池序号, soc_before, soc_after, 时间)
        (vehicles[0], stations[0], 1, 12.0, 100.0, now - timedelta(hours=2)),
        (vehicles[1], stations[1], 1, 8.0, 98.0, now - timedelta(hours=5)),
        (vehicles[2], stations[0], 1, 15.0, 100.0, now - timedelta(days=1, hours=1)),
        (vehicles[4], stations[3], 1, 20.0, 100.0, now - timedelta(minutes=40)),
    ]
    for vehicle, station, pick, soc_before, soc_after, when in plans:
        result = perform_swap(
            db,
            vehicle_id=vehicle.id,
            station_id=station.id,
            soc_before=soc_before,
            soc_after=soc_after,
            battery_in_serial=_pick_serial(db, station.id, vehicle.battery_spec, pick),
            battery_out_serial=None,
            request_id=None,
        )
        result.record.swapped_at = when

    # 故障车浙EV3456 无电池在车，保持低电量待修状态


def _pick_serial(db: Session, station_id: int, spec: str, nth: int) -> str:
    from .models import Battery

    rows = (
        db.query(Battery.serial_no)
        .filter(Battery.station_id == station_id, Battery.status == bs.READY, Battery.spec == spec)
        .order_by(Battery.id)
        .all()
    )
    return rows[nth - 1][0]
