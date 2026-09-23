"""轻量数据库迁移（SQLite，无 Alembic 依赖）。

v1 的 stations.battery_ready 只是汇总数字、没有电池明细表。升级到 v2 时：
1. 给已存在的老表补齐新增列（SQLite 仅支持 ADD COLUMN，默认值/可空安全）；
2. 若电池明细表为空而老 stations 表仍带 battery_ready，则按各站旧汇总数
   回填等额"可换"电池资产，使升级后的派生库存与旧数字完全一致，不分叉。

迁移幂等：列已存在或已回填过都会跳过。全新数据库为空操作。
"""
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .models import STATE_READY

# 老表需要补的列：表 -> [(列名, 列定义)]
_ADDED_COLUMNS = {
    "vehicles": [("battery_spec", "VARCHAR(32)")],
    "swap_records": [
        ("removed_battery_id", "INTEGER"),
        ("installed_battery_id", "INTEGER"),
        ("request_id", "VARCHAR(64)"),
        ("is_legacy", "BOOLEAN DEFAULT 0 NOT NULL"),
    ],
}


def _existing_columns(engine: Engine, table: str) -> set[str]:
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table)}


def migrate(engine: Engine) -> None:
    # 1. 给老表补列
    for table, columns in _ADDED_COLUMNS.items():
        present = _existing_columns(engine, table)
        if not present:
            continue  # 表尚不存在，create_all 已按新结构建好
        for name, ddl in columns:
            if name not in present:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))

    # 2. 老库存回填为电池资产明细
    station_cols = _existing_columns(engine, "stations")
    battery_cols = _existing_columns(engine, "batteries")
    if "battery_ready" not in station_cols or not battery_cols:
        return  # 全新库（stations 已无该列）或明细表尚未建

    with engine.begin() as conn:
        already = conn.execute(text("SELECT COUNT(*) FROM batteries")).scalar()
        if already:
            return  # 已有资产，不重复回填
        rows = conn.execute(
            text("SELECT id, battery_ready FROM stations WHERE battery_ready > 0")
        ).all()
        seq = 1
        for station_id, ready_count in rows:
            for _ in range(int(ready_count)):
                serial = f"BT-MIG-{seq:05d}"
                conn.execute(
                    text(
                        "INSERT INTO batteries "
                        "(serial_no, spec, capacity_kwh, health, state, current_soc, "
                        " station_id, vehicle_id, created_at, updated_at) "
                        "VALUES (:sn, 'STD-M', 100.0, 100.0, :st, 100.0, :sid, NULL, "
                        " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"sn": serial, "st": STATE_READY, "sid": station_id},
                )
                bid = conn.execute(
                    text("SELECT id FROM batteries WHERE serial_no = :sn"), {"sn": serial}
                ).scalar()
                conn.execute(
                    text(
                        "INSERT INTO battery_events "
                        "(battery_id, event_type, from_state, to_state, "
                        " from_station_id, to_station_id, from_vehicle_id, to_vehicle_id, "
                        " soc, note, created_at) "
                        "VALUES (:bid, 'register', NULL, :st, NULL, :sid, NULL, NULL, "
                        " 100.0, 'v1 库存回填迁移', CURRENT_TIMESTAMP)"
                    ),
                    {"bid": bid, "st": STATE_READY, "sid": station_id},
                )
                seq += 1
