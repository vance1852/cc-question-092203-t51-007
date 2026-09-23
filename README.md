# 换电站运营管理平台（纯后端）

新能源物流车换电站后台管理的纯后端 API 服务，提供站点、车辆、**可追溯电池资产**和换电记录的统一管理能力。

## 技术栈

- FastAPI + Uvicorn
- SQLAlchemy + SQLite（本地文件，开箱即用）
- PyJWT（JWT 鉴权）
- 密码哈希用标准库 `hashlib.pbkdf2_hmac`，无额外依赖

所有数据本地、离线可运行，不依赖任何外部服务。

## 运行

```bash
pip install -r requirements.txt
python run.py
```

服务启动在 `http://127.0.0.1:7634`，首次启动自动建表并灌入种子数据。
交互式文档：`http://127.0.0.1:7634/docs`。

## 内置账号

首次启动自动创建唯一管理员（本平台只有 admin 一个角色）：

- 用户名：`admin`
- 密码：`admin123`

## 电池资产追溯模型（v2）

平台库存不再只是站点上的一个汇总数字，每块电池都是可追溯的持久化资产。

### 资产档案

`batteries` 记录：序列号（唯一）、规格码 `spec`、标称容量、健康度 SOH、
当前电量 SOC、状态、所在站点、所装车辆。
`battery_events` 为**只追加**的生命周期台账，按序列号可查询完整历史
（建档、开始充电、充电完成、出库、返站、调拨、隔离、恢复、退役）。

### 六态状态机（合法迁移）

| 状态 | 含义 | 合法迁出 |
| --- | --- | --- |
| `pending` 待充 | 亏电返站/新登记 | → `charging` / `isolated` / `retired` |
| `charging` 充电中 | 正在充电 | → `ready` / `isolated` / `retired` |
| `ready` 可换 | 满电在站，可出库 | → `installed`（换电出库）/ `isolated` / `retired` |
| `installed` 装车 | 装在某辆车上 | → `pending`（换电返站）/ `isolated` / `retired` |
| `isolated` 隔离 | 召回/异常冻结，**禁止出库** | → `pending`/`ready`（解除隔离，按电量）/ `retired` |
| `retired` 退役 | 终态 | 不可再迁移 |

在站三态（待充/充电中/可换）可在站点间**调拨**，状态不变。
站点的 `battery_ready` 是只读汇总数，恒等于本站 `ready` 资产数，
由迁移逻辑在同一事务内重算；接口不再接受手工写入（传入会被忽略并在响应中
返回 `warning`）。可随时用 `GET /api/batteries/reconcile` 对账。

### 换电事务

`POST /api/swaps` 在**单一数据库事务**内完成：

1. `request_id` 幂等检查（重复上报直接返回首次结果，不产生第二次迁移）；
2. 显式校验**卸下**（`battery_out_serial`）与**装上**（`battery_in_serial`）
   两块资产：装上电池必须是本站 `ready`、且规格与车辆 `battery_spec` 一致
   （**车型兼容性校验**），卸下电池必须确实装在该车上；
3. 装上电池 `ready → installed`（离站、绑定车辆）；
4. 卸下电池 `installed → pending`（携亏电电量返站待充）；
5. 更新车辆当前电池与电量；
6. 重算站点汇总数；
7. 两块资产各写一条台账并关联换电记录。

任何一步失败整笔回滚，站点汇总与资产明细不可能分叉。
**隔离/退役中的电池无法通过任何路径出库装车**，召回电池不会再次上线。

### 旧调用方式（只传电量）的兼容策略

旧客户端 `POST /api/swaps` 只传 `vehicle_id/station_id/soc_before/soc_after`
仍然可用（**兼容模式**）：服务端按 FIFO 自动选择本站同规格的可换电池、
自动识别车上电池卸下，并且同样在一个事务内完成全部资产迁移。区别仅是：

- 响应中 `legacy_mode: true`，并带 `notice` 提示尽快升级；
- 记录里标记 `legacy_mode`，仍可追溯系统当时实际选中的两块电池序列号；
- 无同规格可换电池时返回 422，不会用错规格电池。

升级方式：请求体增加 `battery_in_serial`（必填）与
`battery_out_serial`（车辆已装车时必填），即进入资产模式。
建议同时携带 `request_id` 做上报幂等。

## 已实现的接口

- 登录签发 JWT、获取当前用户（`/api/auth/login`、`/api/auth/me`）
- 换电站增删改查（`/api/stations`，`battery_ready` 只读）
- 车辆增删改查（`/api/vehicles`，含 `battery_spec` 与当前装车电池）
- 电池资产（`/api/batteries`）：
  - `POST` 建档、`GET` 列表/筛选（状态、站点、规格、序列号模糊）
  - `GET /{serial_no}` 资产详情、`GET /{serial_no}/history` 完整历史
  - `POST /{serial_no}/charge-start`、`/charge-complete`
  - `POST /{serial_no}/transfer` 调拨、`/isolate` 隔离、`/restore` 解除隔离、`/retire` 退役
  - `GET /reconcile` 站点汇总与资产明细对账
- 换电记录查询与登记（`/api/swaps`，资产模式 + 兼容模式）
- 仪表盘统计（`/api/dashboard/stats`，含资产总数、隔离数）
- 健康检查（`/api/health`）

除 `login` 与 `health` 外，所有接口均需携带 `Authorization: Bearer <token>`。
换电与各生命周期操作均支持 `request_id` 幂等键，重复上报不会制造第二次迁移。

## 测试

```bash
pip install -r requirements.txt
pytest -q
```

## 编码说明

源码与数据均为 UTF-8；FastAPI 响应为 UTF-8 JSON，中文不转义、不乱码。
Windows 控制台若为 GBK，仅影响终端打印观感，不影响接口返回。
