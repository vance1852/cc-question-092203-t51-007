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

- 用户名：`admin`
- 密码：`admin123`

## 电池资产追溯（v2）

库存从"站点一个满电数字"扩展为可追溯的个体资产。每块电池持久化记录
**序列号（唯一）、规格、容量、健康度 SOH、电量、状态、所在站点/车辆**，
并保留只追加、不可改写的全生命周期事件历史（`battery_events`）。

### 状态机与合法迁移

| 状态 | 含义 | 可迁移到 |
| --- | --- | --- |
| `pending_charge` | 待充 | 充电中、隔离 |
| `charging` | 充电中 | 可换、隔离 |
| `ready` | 可换（满电） | 装车、隔离 |
| `installed` | 装车（在某辆车上） | 待充（仅经换电卸下） |
| `isolated` | 隔离（召回/异常） | 待充（解除隔离）、退役 |
| `retired` | 退役（终态） | — |

- 车上电池不能直接隔离，必须先换电卸下回站，防止"召回电池随车继续跑"；
- 退役只能从隔离进入，为终态；隔离解除后回到待充，需重新充电才能出库；
- 调拨仅允许在仓的待充/可换电池，充电中、隔离、装车、退役均不可调拨。

### 换电事务

`POST /api/swaps` 在**同一数据库事务**内完成：

1. 校验车型与电池规格兼容（车辆 `battery_spec` 与电池 `spec` 一致）；
2. 明确**装上**的满电电池（`installed_serial`，须在本站、状态可换、未隔离）
   与**卸下**的亏电电池（`removed_serial`，须装在该车上，可空）；
3. 原子地把装上电池 `ready→installed`（归车）、卸下电池 `installed→pending_charge`（归站）；
4. 更新车辆电量、写换电记录、为两块电池各追加一条事件。

任一校验失败整体回滚；状态落库用带状态前置条件的原子 `UPDATE ... WHERE`，
并发的两次换电不可能把同一块满电电池出两次库。

**幂等**：请求带 `request_id` 时，同一键的重复上报直接返回首条记录，
不会产生第二次迁移（数据库唯一约束兜底）。

### 汇总数不与明细分叉

站点的 `battery_ready / battery_pending / battery_charging / battery_isolated /
battery_total` 与仪表盘电池指标**全部由电池明细实时聚合派生**，不再单独落库。
因此无论操作成功还是失败，汇总数恒等于明细统计，结构上杜绝分叉。
站点/车辆在仍持有电池资产时禁止删除。

### 主要接口

- `GET/POST /api/batteries`：资产列表（可按站点/车辆/状态/规格/序列号过滤）、登记
- `GET  /api/batteries/{serial}`：按序列号查资产
- `GET  /api/batteries/{serial}/history`：**按序列号查完整迁移历史**
- `POST /api/batteries/{serial}/charge/start|complete`：开始充电 / 充电完成（可回写 SOH）
- `POST /api/batteries/{serial}/transfer`：站点调拨
- `POST /api/batteries/{serial}/isolate|unisolate|retire`：隔离 / 解除隔离 / 退役
- `PATCH /api/batteries/{serial}/health`：维护健康度
- `POST /api/swaps`：换电（支持 `installed_serial`/`removed_serial`/`request_id`）

## 旧调用方式（只传电量）兼容策略

旧客户端的换电请求体 `{vehicle_id, station_id, soc_before, soc_after}`
**继续可用、无需改动**，平台自动进入兼容模式（响应中 `is_legacy=true`）：

- 系统按车型规格在本站自动选配健康度最优的满电电池，并自动取下车辆当前在册电池；
- 60 秒内车辆/站点/电量完全相同的重复上报按自然键去重，不重复扣减库存；
- 强烈建议新系统改用 `installed_serial`/`removed_serial` 显式换电，
  并携带 `request_id` 以获得完整追溯与强幂等。

站点写接口中的 `battery_ready` 字段已**废弃**：上送不报错但被忽略
（库存只能由登记电池形成），响应中返回的是真实派生值。

## 已实现的基础功能

- 登录签发 JWT、获取当前用户（`/api/auth/login`、`/api/auth/me`）
- 换电站增删改查（`/api/stations`，库存数实时派生）
- 车辆增删改查（`/api/vehicles`，返回当前装车电池序列号）
- 可追溯电池资产全生命周期管理（`/api/batteries`）
- 换电记录查询与登记（`/api/swaps`，双电池同事务 + 幂等）
- 仪表盘统计（`/api/dashboard/stats`，含隔离/退役电池数）
- 健康检查（`/api/health`）

除 `login` 与 `health` 外，所有接口均需携带 `Authorization: Bearer <token>`。

## 测试

```bash
pip install -r requirements.txt
pytest -q
```

## 编码说明

源码与数据均为 UTF-8；FastAPI 响应为 UTF-8 JSON，中文不转义、不乱码。
Windows 控制台若为 GBK，仅影响终端打印观感，不影响接口返回。
