"""数据库模型。

业务主题：新能源物流车换电站运营管理。

资产可追溯模型：
- Battery：每块电池一条持久化资产档案（序列号、规格、健康度、状态、所在站点/车辆）。
- BatteryEvent：电池生命周期台账，只追加，按序列号即可还原完整历史。
- 站点 battery_ready 为资产明细推导出的汇总数，只允许由状态迁移在同一事务内维护。
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


class User(Base):
    """后台用户（本平台只有 admin 一个管理员角色）。"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    display_name = Column(String(64), nullable=False, default="管理员")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Station(Base):
    """换电站。"""

    __tablename__ = "stations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    address = Column(String(256), nullable=False, default="")
    # 电池仓位总数
    slot_total = Column(Integer, nullable=False, default=0)
    # 当前满电可换电池数（只读汇总：等于本站 status=ready 的电池资产数，
    # 由电池状态迁移在同一事务内维护，不接受客户端直接写入）
    battery_ready = Column(Integer, nullable=False, default=0)
    # 运营状态：running 运营中 / maintenance 维护中 / offline 离线
    status = Column(String(16), nullable=False, default="running")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    swaps = relationship("SwapRecord", back_populates="station")
    batteries = relationship("Battery", back_populates="station")


class Vehicle(Base):
    """新能源物流车。"""

    __tablename__ = "vehicles"

    id = Column(Integer, primary_key=True, index=True)
    plate = Column(String(32), unique=True, nullable=False, index=True)
    model = Column(String(64), nullable=False, default="")
    # 兼容的电池规格码，只有 spec 相同的电池才允许装车
    battery_spec = Column(String(32), nullable=False, default="STD-100")
    battery_capacity = Column(Float, nullable=False, default=100.0)  # kWh
    current_soc = Column(Float, nullable=False, default=100.0)  # 0-100 百分比
    # 当前装车电池（资产外键），无电池时为空
    current_battery_id = Column(Integer, ForeignKey("batteries.id"), nullable=True)
    # 状态：idle 空闲 / running 运营 / charging 换电中 / fault 故障
    status = Column(String(16), nullable=False, default="idle")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    swaps = relationship("SwapRecord", back_populates="vehicle")
    current_battery = relationship("Battery", foreign_keys=[current_battery_id])


class Battery(Base):
    """电池资产：序列号唯一，状态机受 battery_service 约束。

    状态：pending 待充 / charging 充电中 / ready 可换 /
          installed 装车 / isolated 隔离 / retired 退役。
    """

    __tablename__ = "batteries"

    id = Column(Integer, primary_key=True, index=True)
    serial_no = Column(String(64), unique=True, nullable=False, index=True)
    # 电池规格码（见 battery_service.SPECS），决定与车型的兼容性
    spec = Column(String(32), nullable=False, index=True)
    capacity_kwh = Column(Float, nullable=False, default=100.0)
    # 健康度 SOH，0-100
    soh = Column(Float, nullable=False, default=100.0)
    # 当前电量 SOC，0-100
    soc = Column(Float, nullable=False, default=0.0)
    status = Column(String(16), nullable=False, default="pending", index=True)
    # 所在站点：在站电池必填；装车中随车辆离站为空，历史由台账保留
    station_id = Column(Integer, ForeignKey("stations.id"), nullable=True, index=True)
    # 装车中的车辆；在站/隔离/退役时为空
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    station = relationship("Station", back_populates="batteries", foreign_keys=[station_id])
    vehicle = relationship("Vehicle", foreign_keys=[vehicle_id])
    events = relationship(
        "BatteryEvent",
        back_populates="battery",
        order_by="BatteryEvent.id",
        cascade="all, delete-orphan",
    )


class BatteryEvent(Base):
    """电池生命周期台账（只追加）。

    每次合法状态迁移写一条；from_*/to_* 为迁移发生时的快照，
    站点/车辆删除后历史依然可查，因此快照列不用外键。
    """

    __tablename__ = "battery_events"

    id = Column(Integer, primary_key=True, index=True)
    battery_id = Column(Integer, ForeignKey("batteries.id"), nullable=False, index=True)
    # register/charge_start/charge_complete/swap_out/swap_in/transfer/isolate/restore/retire
    event_type = Column(String(32), nullable=False, index=True)
    from_status = Column(String(16), nullable=True)
    to_status = Column(String(16), nullable=True)
    from_station_id = Column(Integer, nullable=True)
    to_station_id = Column(Integer, nullable=True)
    vehicle_id = Column(Integer, nullable=True)
    swap_record_id = Column(Integer, ForeignKey("swap_records.id"), nullable=True, index=True)
    soc = Column(Float, nullable=True)
    soh = Column(Float, nullable=True)
    note = Column(String(256), nullable=False, default="")
    # 客户端幂等键：重复上报命中同一条事件，不产生第二次迁移
    idempotency_key = Column(String(64), unique=True, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    battery = relationship("Battery", back_populates="events")


class SwapRecord(Base):
    """换电记录。"""

    __tablename__ = "swap_records"

    id = Column(Integer, primary_key=True, index=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=False, index=True)
    station_id = Column(Integer, ForeignKey("stations.id"), nullable=False, index=True)
    # 卸下（低电量返站）与装上（满电出库）的两块电池
    battery_out_id = Column(Integer, ForeignKey("batteries.id"), nullable=True)
    battery_in_id = Column(Integer, ForeignKey("batteries.id"), nullable=True)
    soc_before = Column(Float, nullable=False, default=0.0)
    soc_after = Column(Float, nullable=False, default=100.0)
    # 兼容模式：旧调用方未提供序列号，由系统自动选包
    legacy_mode = Column(Boolean, nullable=False, default=False)
    # 上报幂等键（同一键重复提交不产生第二次迁移）
    request_id = Column(String(64), unique=True, nullable=True, index=True)
    swapped_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    vehicle = relationship("Vehicle", back_populates="swaps", foreign_keys=[vehicle_id])
    station = relationship("Station", back_populates="swaps", foreign_keys=[station_id])
    battery_out = relationship("Battery", foreign_keys=[battery_out_id])
    battery_in = relationship("Battery", foreign_keys=[battery_in_id])
