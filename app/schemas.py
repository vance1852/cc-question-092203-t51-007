"""Pydantic 数据模型（请求体与响应体）。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------- 认证 ----------
class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str

    model_config = {"from_attributes": True}


# ---------- 换电站 ----------
# 兼容说明：battery_ready 仍出现在响应里，但属于只读汇总（=本站 ready 资产数）。
# 建/改站点时传入该字段会被忽略并给出警告，汇总只能随电池迁移变化。
class StationBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    address: str = ""
    slot_total: int = Field(0, ge=0)
    status: str = Field("running", pattern="^(running|maintenance|offline)$")


class StationCreate(StationBase):
    # 已废弃：保留在模型中仅为接收旧请求体，服务端忽略；详见 README 兼容策略
    battery_ready: Optional[int] = Field(None, ge=0, deprecated=True)


class StationUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    address: Optional[str] = None
    slot_total: Optional[int] = Field(None, ge=0)
    status: Optional[str] = Field(None, pattern="^(running|maintenance|offline)$")
    battery_ready: Optional[int] = Field(None, ge=0, deprecated=True)


class StationOut(BaseModel):
    id: int
    name: str
    address: str
    slot_total: int
    battery_ready: int
    status: str
    created_at: datetime
    # 当旧客户端在创建/修改时传了 battery_ready，响应中提示该值被忽略
    warning: Optional[str] = None

    model_config = {"from_attributes": True}


# ---------- 电池资产 ----------
BATTERY_STATUS_PATTERN = "^(pending|charging|ready|installed|isolated|retired)$"


class BatteryCreate(BaseModel):
    serial_no: str = Field(..., min_length=1, max_length=64)
    spec: str = Field(..., min_length=1, max_length=32, description="电池规格码，需与车型 battery_spec 一致")
    capacity_kwh: float = Field(100.0, gt=0)
    soh: float = Field(100.0, ge=0, le=100)
    soc: float = Field(0.0, ge=0, le=100)
    station_id: Optional[int] = Field(None, description="登记入站；不填则为待充且暂未到站")


class BatteryOut(BaseModel):
    id: int
    serial_no: str
    spec: str
    capacity_kwh: float
    soh: float
    soc: float
    status: str
    station_id: Optional[int]
    station_name: Optional[str] = None
    vehicle_id: Optional[int]
    vehicle_plate: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class BatteryChargeStart(BaseModel):
    """开始充电：待充 -> 充电中。"""

    request_id: Optional[str] = Field(None, max_length=64, description="幂等键，重复上报不会二次迁移")


class BatteryChargeComplete(BaseModel):
    """充电完成上报：充电中 -> 可换。"""

    soc: float = Field(100.0, ge=0, le=100)
    soh: Optional[float] = Field(None, ge=0, le=100, description="本次充电检测到的健康度，不填则保持不变")
    request_id: Optional[str] = Field(None, max_length=64, description="幂等键，重复上报不会二次迁移")


class BatteryTransfer(BaseModel):
    """调拨：把电池调到另一个站点。"""

    to_station_id: int
    request_id: Optional[str] = Field(None, max_length=64)


class BatteryIsolate(BaseModel):
    """隔离：召回/异常冻结，隔离中的电池不能出库装车。"""

    reason: str = Field(..., min_length=1, max_length=256)
    request_id: Optional[str] = Field(None, max_length=64)


class BatteryRestore(BaseModel):
    """解除隔离：按当前电量回到待充或可换。"""

    request_id: Optional[str] = Field(None, max_length=64)


class BatteryRetire(BaseModel):
    """退役。"""

    reason: str = Field("", max_length=256)
    request_id: Optional[str] = Field(None, max_length=64)


class BatteryEventOut(BaseModel):
    id: int
    battery_id: int
    event_type: str
    from_status: Optional[str]
    to_status: Optional[str]
    from_station_id: Optional[int]
    to_station_id: Optional[int]
    vehicle_id: Optional[int]
    swap_record_id: Optional[int]
    soc: Optional[float]
    soh: Optional[float]
    note: str
    idempotency_key: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class BatteryHistory(BaseModel):
    battery: BatteryOut
    events: list[BatteryEventOut]


class StationReconcileItem(BaseModel):
    station_id: int
    station_name: str
    summary_battery_ready: int
    actual_ready_assets: int


class ReconcileReport(BaseModel):
    """站点汇总数与资产明细对账结果。"""

    consistent: bool
    stations: list[StationReconcileItem]


# ---------- 车辆 ----------
class VehicleBase(BaseModel):
    plate: str = Field(..., min_length=1, max_length=32)
    model: str = ""
    battery_spec: str = Field("STD-100", min_length=1, max_length=32)
    battery_capacity: float = Field(100.0, gt=0)
    current_soc: float = Field(100.0, ge=0, le=100)
    status: str = Field("idle", pattern="^(idle|running|charging|fault)$")


class VehicleCreate(VehicleBase):
    pass


class VehicleUpdate(BaseModel):
    plate: Optional[str] = Field(None, min_length=1, max_length=32)
    model: Optional[str] = None
    battery_spec: Optional[str] = Field(None, min_length=1, max_length=32)
    battery_capacity: Optional[float] = Field(None, gt=0)
    current_soc: Optional[float] = Field(None, ge=0, le=100)
    status: Optional[str] = Field(None, pattern="^(idle|running|charging|fault)$")


class VehicleOut(BaseModel):
    id: int
    plate: str
    model: str
    battery_spec: str
    battery_capacity: float
    current_soc: float
    current_battery_id: Optional[int] = None
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------- 换电 ----------
class SwapCreate(BaseModel):
    """换电请求。

    新接入方（资产模式，推荐）：必须提供 vehicle_id/station_id/battery_in_serial，
    且车辆上已有电池时提供 battery_out_serial；服务端校验规格兼容与两块资产状态。

    旧调用方（兼容模式）：只传 vehicle_id/station_id/soc_before/soc_after，
    服务端自动选择同规格可换电池、FIFO 卸下装车电池，并在响应中标注
    legacy_mode=true 与升级提示。该模式仍在同一事务内完成全部资产迁移。
    """

    vehicle_id: int
    station_id: int
    soc_before: Optional[float] = Field(None, ge=0, le=100)
    soc_after: Optional[float] = Field(None, ge=0, le=100)
    # 装上车辆的满电电池序列号（资产模式必填）
    battery_in_serial: Optional[str] = Field(None, max_length=64)
    # 从车辆卸下的亏电电池序列号（车辆已装车时必填）
    battery_out_serial: Optional[str] = Field(None, max_length=64)
    # 上报幂等键：同一键重复提交返回首次结果，不产生第二次迁移
    request_id: Optional[str] = Field(None, max_length=64)


class SwapOut(BaseModel):
    id: int
    vehicle_id: int
    station_id: int
    battery_out_id: Optional[int] = None
    battery_out_serial: Optional[str] = None
    battery_in_id: Optional[int] = None
    battery_in_serial: Optional[str] = None
    soc_before: float
    soc_after: float
    legacy_mode: bool = False
    request_id: Optional[str] = None
    swapped_at: datetime
    vehicle_plate: Optional[str] = None
    station_name: Optional[str] = None
    notice: Optional[str] = None

    model_config = {"from_attributes": True}


# ---------- 仪表盘 ----------
class DashboardStats(BaseModel):
    station_total: int
    station_running: int
    vehicle_total: int
    vehicle_fault: int
    swap_today: int
    battery_ready_total: int
    battery_total: int
    battery_isolated: int
