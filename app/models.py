"""数据库模型。

业务主题：新能源物流车换电站运营管理。

电池资产从站点汇总数字扩展为可追溯个体：
- Battery 记录每块电池的序列号、规格、健康度（SOH）、状态与所在站点/车辆；
- BatteryEvent 记录电池全生命周期的每一次状态迁移（充入历史、不可改写）；
- SwapRecord 明确一次换电卸下与装上的两块电池。
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import relationship

from .database import Base

# ---------------- 电池状态机 ----------------
# 待充 / 充电中 / 可换 / 装车 / 隔离 / 退役
STATE_PENDING = "pending_charge"
STATE_CHARGING = "charging"
STATE_READY = "ready"
STATE_INSTALLED = "installed"
STATE_ISOLATED = "isolated"
STATE_RETIRED = "retired"

BATTERY_STATES = (
    STATE_PENDING,
    STATE_CHARGING,
    STATE_READY,
    STATE_INSTALLED,
    STATE_ISOLATED,
    STATE_RETIRED,
)

# 合法迁移表：退役为终态；车上电池必须先经换电卸下才能隔离
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATE_PENDING: frozenset({STATE_CHARGING, STATE_ISOLATED}),
    STATE_CHARGING: frozenset({STATE_READY, STATE_ISOLATED}),
    STATE_READY: frozenset({STATE_INSTALLED, STATE_ISOLATED}),
    STATE_INSTALLED: frozenset({STATE_PENDING}),
    STATE_ISOLATED: frozenset({STATE_PENDING, STATE_RETIRED}),
    STATE_RETIRED: frozenset(),
}

# 可在站点间调拨的状态：仅空仓位上的待充/可换电池；
# 充电中、隔离、装车、退役均不可调拨。
TRANSFERABLE_STATES = frozenset({STATE_PENDING, STATE_READY})

# ---------------- 事件类型 ----------------
EVENT_REGISTER = "register"          # 资产登记
EVENT_CHARGE_START = "charge_start"  # 开始充电
EVENT_CHARGE_COMPLETE = "charge_complete"  # 充电完成
EVENT_SWAP_OUT = "swap_out"          # 换电卸下（车 → 站）
EVENT_SWAP_IN = "swap_in"            # 换电装上（站 → 车）
EVENT_TRANSFER = "transfer"          # 站点调拨
EVENT_ISOLATE = "isolate"            # 隔离
EVENT_UNISOLATE = "unisolate"        # 解除隔离
EVENT_RETIRE = "retire"              # 退役


class User(Base):
    """后台用户（本平台只有 admin 一个管理员角色）。"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    display_name = Column(String(64), nullable=False, default="管理员")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Station(Base):
    """换电站。

    battery_ready 等库存数不再落库人工维护，而由 Battery 明细实时派生
    （见 services.battery_service.station_inventory），从结构上保证
    汇总数与资产明细永不分叉。
    """

    __tablename__ = "stations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    address = Column(String(256), nullable=False, default="")
    # 电池仓位总数
    slot_total = Column(Integer, nullable=False, default=0)
    # 运营状态：running 运营中 / maintenance 维护中 / offline 离线
    status = Column(String(16), nullable=False, default="running")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    swaps = relationship("SwapRecord", back_populates="station")
    batteries = relationship("Battery", back_populates="station")


class Vehicle(Base):
    """新能源物流车。battery_spec 为兼容的电池规格代码；空表示不限制。"""

    __tablename__ = "vehicles"

    id = Column(Integer, primary_key=True, index=True)
    plate = Column(String(32), unique=True, nullable=False, index=True)
    model = Column(String(64), nullable=False, default="")
    battery_capacity = Column(Float, nullable=False, default=100.0)  # kWh
    # 兼容的电池规格代码（对应 Battery.spec）；为空表示不校验规格
    battery_spec = Column(String(32), nullable=True)
    current_soc = Column(Float, nullable=False, default=100.0)  # 0-100 百分比
    # 状态：idle 空闲 / running 运营 / charging 换电中 / fault 故障
    status = Column(String(16), nullable=False, default="idle")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    swaps = relationship("SwapRecord", back_populates="vehicle")
    batteries = relationship("Battery", back_populates="vehicle")


class Battery(Base):
    """可追溯的电池资产。"""

    __tablename__ = "batteries"

    id = Column(Integer, primary_key=True, index=True)
    serial_no = Column(String(64), unique=True, nullable=False, index=True)
    spec = Column(String(32), nullable=False, index=True)  # 规格代码
    capacity_kwh = Column(Float, nullable=False, default=100.0)
    # 健康度 SOH，0-100 百分比
    health = Column(Float, nullable=False, default=100.0)
    # 状态机当前状态，取值见 BATTERY_STATES
    state = Column(String(16), nullable=False, default=STATE_PENDING, index=True)
    current_soc = Column(Float, nullable=False, default=0.0)
    # 装车中（state=installed）时 station_id 为空、vehicle_id 有值；
    # 其余在站状态 station_id 有值、vehicle_id 为空。
    station_id = Column(Integer, ForeignKey("stations.id"), nullable=True, index=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    station = relationship("Station", back_populates="batteries")
    vehicle = relationship("Vehicle", back_populates="batteries")
    events = relationship(
        "BatteryEvent",
        back_populates="battery",
        order_by="BatteryEvent.created_at.desc()",
    )


class BatteryEvent(Base):
    """电池全生命周期事件（只追加，不改写）。

    from/to 的站点、车辆引用不用外键约束：资产历史必须长期保留，
    即使主数据被删除也能还原迁移轨迹。
    """

    __tablename__ = "battery_events"

    id = Column(Integer, primary_key=True, index=True)
    battery_id = Column(Integer, ForeignKey("batteries.id"), nullable=False, index=True)
    event_type = Column(String(32), nullable=False, index=True)
    from_state = Column(String(16), nullable=True)
    to_state = Column(String(16), nullable=True)
    from_station_id = Column(Integer, nullable=True)
    to_station_id = Column(Integer, nullable=True)
    from_vehicle_id = Column(Integer, nullable=True)
    to_vehicle_id = Column(Integer, nullable=True)
    soc = Column(Float, nullable=True)
    swap_record_id = Column(Integer, nullable=True, index=True)
    request_id = Column(String(64), nullable=True, index=True)
    note = Column(String(256), nullable=False, default="")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    battery = relationship("Battery", back_populates="events")


class SwapRecord(Base):
    """换电记录：一次换电明确卸下与装上的两块电池。"""

    __tablename__ = "swap_records"

    id = Column(Integer, primary_key=True, index=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=False, index=True)
    station_id = Column(Integer, ForeignKey("stations.id"), nullable=False, index=True)
    soc_before = Column(Float, nullable=False, default=0.0)
    soc_after = Column(Float, nullable=False, default=100.0)
    # 卸下的低电量电池（老的兼容调用可能为空：车辆此前无在册电池）
    removed_battery_id = Column(
        Integer, ForeignKey("batteries.id"), nullable=True, index=True
    )
    # 装上的满电电池
    installed_battery_id = Column(
        Integer, ForeignKey("batteries.id"), nullable=True, index=True
    )
    # 客户端幂等键：同一 request_id 的重复上报直接返回首条记录
    request_id = Column(String(64), unique=True, nullable=True, index=True)
    # 兼容标记：未传电池序列号、由系统自动选配的换电
    is_legacy = Column(Boolean, nullable=False, default=False)
    swapped_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    vehicle = relationship("Vehicle", back_populates="swaps", foreign_keys=[vehicle_id])
    station = relationship("Station", back_populates="swaps")
    removed_battery = relationship("Battery", foreign_keys=[removed_battery_id])
    installed_battery = relationship("Battery", foreign_keys=[installed_battery_id])
