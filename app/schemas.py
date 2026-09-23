"""Pydantic 数据模型（请求体与响应体）。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

# 电池状态正则（供 pydantic pattern 使用）
_BATTERY_STATE_PATTERN = (
    r"^(pending_charge|charging|ready|installed|isolated|retired)$"
)
_INITIAL_STATE_PATTERN = r"^(pending_charge|charging|ready|isolated)$"


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
class StationBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    address: str = ""
    slot_total: int = Field(0, ge=0)
    status: str = Field("running", pattern="^(running|maintenance|offline)$")


class StationCreate(StationBase):
    # 兼容字段：旧客户端仍可上送，但电池数量已改为由资产明细实时派生，
    # 该值会被忽略；请改用 /api/batteries 登记电池来形成库存。
    battery_ready: Optional[int] = Field(
        default=None, ge=0, deprecated=True, description="已废弃：库存由电池明细派生，上送将被忽略"
    )


class StationUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    address: Optional[str] = None
    slot_total: Optional[int] = Field(None, ge=0)
    status: Optional[str] = Field(None, pattern="^(running|maintenance|offline)$")
    # 兼容字段：同 StationCreate，上送即忽略（不能再用它直接改库存）
    battery_ready: Optional[int] = Field(
        default=None, ge=0, deprecated=True, description="已废弃：库存由电池明细派生，上送将被忽略"
    )


class StationOut(BaseModel):
    id: int
    name: str
    address: str
    slot_total: int
    status: str
    created_at: datetime
    # 以下数量全部由 Battery 明细实时聚合，不单独落库，杜绝分叉
    battery_ready: int = Field(0, description="可换（满电）电池数")
    battery_pending: int = Field(0, description="待充电池数")
    battery_charging: int = Field(0, description="充电中电池数")
    battery_isolated: int = Field(0, description="隔离中电池数")
    battery_total: int = Field(0, description="站内电池总数（不含装车、退役）")

    model_config = {"from_attributes": True}


# ---------- 车辆 ----------
class VehicleBase(BaseModel):
    plate: str = Field(..., min_length=1, max_length=32)
    model: str = ""
    battery_capacity: float = Field(100.0, gt=0)
    battery_spec: Optional[str] = Field(
        None, max_length=32, description="兼容的电池规格代码；为空表示不限制"
    )
    current_soc: float = Field(100.0, ge=0, le=100)
    status: str = Field("idle", pattern="^(idle|running|charging|fault)$")


class VehicleCreate(VehicleBase):
    pass


class VehicleUpdate(BaseModel):
    plate: Optional[str] = Field(None, min_length=1, max_length=32)
    model: Optional[str] = None
    battery_capacity: Optional[float] = Field(None, gt=0)
    battery_spec: Optional[str] = Field(None, max_length=32)
    current_soc: Optional[float] = Field(None, ge=0, le=100)
    status: Optional[str] = Field(None, pattern="^(idle|running|charging|fault)$")


class VehicleOut(VehicleBase):
    id: int
    created_at: datetime
    current_battery_serial: Optional[str] = Field(
        None, description="当前装车电池序列号（无在册电池时为空）"
    )

    model_config = {"from_attributes": True}


# ---------- 电池资产 ----------
class BatteryCreate(BaseModel):
    serial_no: str = Field(..., min_length=1, max_length=64, description="电池序列号（唯一）")
    spec: str = Field(..., min_length=1, max_length=32, description="电池规格代码")
    station_id: int = Field(..., description="初始所在站点")
    capacity_kwh: float = Field(100.0, gt=0)
    health: float = Field(100.0, ge=0, le=100, description="健康度 SOH（%）")
    current_soc: float = Field(0.0, ge=0, le=100)
    state: str = Field(
        "pending_charge",
        pattern=_INITIAL_STATE_PATTERN,
        description="初始状态，仅允许待充/充电中/可换/隔离",
    )


class BatteryOut(BaseModel):
    id: int
    serial_no: str
    spec: str
    capacity_kwh: float
    health: float
    state: str
    current_soc: float
    station_id: Optional[int]
    vehicle_id: Optional[int]
    station_name: Optional[str] = None
    vehicle_plate: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class BatteryHealthUpdate(BaseModel):
    health: float = Field(..., ge=0, le=100, description="新的健康度 SOH（%）")


class ChargeCompleteRequest(BaseModel):
    soc: float = Field(100.0, ge=0, le=100, description="充满后的电量")
    health: Optional[float] = Field(None, ge=0, le=100, description="本次充电后复测的健康度（可选）")
    request_id: Optional[str] = Field(None, max_length=64, description="幂等键（可选）")


class TransferRequest(BaseModel):
    to_station_id: int
    note: Optional[str] = Field(None, max_length=256)


class IsolateRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=256, description="隔离/召回原因")


class RetireRequest(BaseModel):
    reason: str = Field("退役", min_length=1, max_length=256)


class BatteryEventOut(BaseModel):
    id: int
    battery_id: int
    event_type: str
    from_state: Optional[str]
    to_state: Optional[str]
    from_station_id: Optional[int]
    to_station_id: Optional[int]
    from_vehicle_id: Optional[int]
    to_vehicle_id: Optional[int]
    soc: Optional[float]
    swap_record_id: Optional[int]
    request_id: Optional[str]
    note: str
    created_at: datetime

    model_config = {"from_attributes": True}


class BatteryHistoryOut(BaseModel):
    battery: BatteryOut
    events: list[BatteryEventOut]


# ---------- 换电记录 ----------
class SwapCreate(BaseModel):
    vehicle_id: int
    station_id: int
    soc_before: float = Field(..., ge=0, le=100)
    soc_after: float = Field(100.0, ge=0, le=100)
    # 新接口：明确卸下与装上的电池序列号
    removed_serial: Optional[str] = Field(
        None, description="卸下的亏电电池序列号；不传则取车辆当前在册电池"
    )
    installed_serial: Optional[str] = Field(
        None,
        description="装上的满电电池序列号；不传为兼容模式，由系统按车型规格自动选配",
    )
    # 幂等键：同一 request_id 的重复上报不会产生第二次迁移
    request_id: Optional[str] = Field(None, max_length=64)


class SwapOut(BaseModel):
    id: int
    vehicle_id: int
    station_id: int
    soc_before: float
    soc_after: float
    swapped_at: datetime
    vehicle_plate: Optional[str] = None
    station_name: Optional[str] = None
    removed_battery_id: Optional[int] = None
    installed_battery_id: Optional[int] = None
    removed_serial: Optional[str] = None
    installed_serial: Optional[str] = None
    request_id: Optional[str] = None
    is_legacy: bool = False

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
    battery_isolated_total: int
    battery_retired_total: int
