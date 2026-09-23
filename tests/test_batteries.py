"""电池资产追溯测试。

覆盖：资产登记、状态机合法/非法迁移、充电、调拨、隔离/退役、
换电同事务双电池更新、车型兼容校验、幂等、兼容老调用、序列号历史，
以及"任何失败都不让站点汇总数与资产明细分叉"。
"""
import uuid

from fastapi.testclient import TestClient

from app.main import app
from app.seed import init_db

init_db()
client = TestClient(app)


def _headers() -> dict:
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _sn(prefix="BT-T") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _station(h, slot_total=50, **kw):
    body = {"name": f"资产测试站-{uuid.uuid4().hex[:6]}", "slot_total": slot_total}
    body.update(kw)
    r = client.post("/api/stations", json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def _vehicle(h, spec=None, plate=None):
    body = {"plate": plate or f"测{uuid.uuid4().hex[:6]}", "model": "测试车型", "battery_spec": spec}
    r = client.post("/api/vehicles", json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def _register(h, station_id, spec="STD-M", state="pending_charge", soc=0.0, serial=None, health=100.0):
    body = {
        "serial_no": serial or _sn(),
        "spec": spec,
        "station_id": station_id,
        "capacity_kwh": 100.0,
        "health": health,
        "current_soc": soc,
        "state": state,
    }
    r = client.post("/api/batteries", json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def _ready(h, station_id, *, serial=None, soc=100.0, spec="STD-M"):
    """登记一块直接可换的满电电池（待充→充电→完成，走真实状态机）。"""
    b = _register(h, station_id, spec=spec, state="pending_charge", soc=0.0, serial=serial)
    assert client.post(f"/api/batteries/{b['serial_no']}/charge/start", headers=h).status_code == 200
    r = client.post(
        f"/api/batteries/{b['serial_no']}/charge/complete",
        json={"soc": soc},
        headers=h,
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 登记
def test_register_battery_and_duplicate_serial():
    h = _headers()
    s = _station(h)
    serial = _sn()
    b = _register(h, s["id"], serial=serial, spec="STD-L", health=95.5)
    assert b["state"] == "pending_charge"
    assert b["station_id"] == s["id"]
    assert b["health"] == 95.5
    # 序列号唯一
    dup = client.post(
        "/api/batteries",
        json={"serial_no": serial, "spec": "STD-L", "station_id": s["id"]},
        headers=h,
    )
    assert dup.status_code == 409


def test_register_respects_slot_capacity():
    h = _headers()
    zero = _station(h, slot_total=0)
    r = client.post(
        "/api/batteries",
        json={"serial_no": _sn(), "spec": "STD-M", "station_id": zero["id"]},
        headers=h,
    )
    assert r.status_code == 422

    full = _station(h, slot_total=1)
    _register(h, full["id"])
    overflow = client.post(
        "/api/batteries",
        json={"serial_no": _sn(), "spec": "STD-M", "station_id": full["id"]},
        headers=h,
    )
    assert overflow.status_code == 422


# ---------------------------------------------------------------- 状态机
def test_illegal_transitions_rejected():
    h = _headers()
    s = _station(h)
    b = _register(h, s["id"])  # pending_charge
    # 待充不能直接充电完成
    r = client.post(f"/api/batteries/{b['serial_no']}/charge/complete", json={"soc": 100}, headers=h)
    assert r.status_code == 422
    # 可换才能装车；待充不能直接退役
    r = client.post(f"/api/batteries/{b['serial_no']}/retire", json={"reason": "x"}, headers=h)
    assert r.status_code == 422


def test_charge_lifecycle_and_complete_is_idempotent():
    h = _headers()
    s = _station(h)
    b = _register(h, s["id"])
    assert client.post(f"/api/batteries/{b['serial_no']}/charge/start", headers=h).status_code == 200
    ok = client.post(
        f"/api/batteries/{b['serial_no']}/charge/complete",
        json={"soc": 100.0, "health": 97.0},
        headers=h,
    )
    assert ok.status_code == 200
    assert ok.json()["state"] == "ready"
    assert ok.json()["health"] == 97.0

    hist1 = client.get(f"/api/batteries/{b['serial_no']}/history", headers=h).json()
    n_events = len(hist1["events"])
    # 重复上报充电完成：幂等返回，不产生第二条事件、不覆盖健康度
    again = client.post(
        f"/api/batteries/{b['serial_no']}/charge/complete",
        json={"soc": 80.0, "health": 50.0},
        headers=h,
    )
    assert again.status_code == 200
    assert again.json()["current_soc"] == 100.0
    assert again.json()["health"] == 97.0
    hist2 = client.get(f"/api/batteries/{b['serial_no']}/history", headers=h).json()
    assert len(hist2["events"]) == n_events

    # 站点派生可换数应反映这块满电电池
    station = client.get(f"/api/stations/{s['id']}", headers=h).json()
    assert station["battery_ready"] >= 1


# ---------------------------------------------------------------- 调拨
def test_transfer_battery():
    h = _headers()
    s1 = _station(h)
    s2 = _station(h)
    b = _register(h, s1["id"])  # 待充，可调拨
    r = client.post(f"/api/batteries/{b['serial_no']}/transfer", json={"to_station_id": s2["id"]}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["station_id"] == s2["id"]

    # 充电中不可调拨
    assert client.post(f"/api/batteries/{b['serial_no']}/charge/start", headers=h).status_code == 200
    bad = client.post(f"/api/batteries/{b['serial_no']}/transfer", json={"to_station_id": s1["id"]}, headers=h)
    assert bad.status_code == 422

    # 调入满仓站点被拒
    full = _station(h, slot_total=0)
    b2 = _register(h, s2["id"])
    bad2 = client.post(f"/api/batteries/{b2['serial_no']}/transfer", json={"to_station_id": full["id"]}, headers=h)
    assert bad2.status_code == 422


# ---------------------------------------------------------------- 隔离/退役
def test_isolate_blocks_dispatch_and_retire_flow():
    h = _headers()
    s = _station(h)
    v = _vehicle(h, spec="STD-M")
    b = _ready(h, station_id=s["id"], serial=_sn())  # 满电可换

    # 召回隔离
    r = client.post(f"/api/batteries/{b['serial_no']}/isolate", json={"reason": "疑似衰减召回"}, headers=h)
    assert r.status_code == 200
    assert r.json()["state"] == "isolated"

    # 关键：被隔离（召回）电池不能再次出库装车
    swap = client.post(
        "/api/swaps",
        json={
            "vehicle_id": v["id"],
            "station_id": s["id"],
            "soc_before": 10.0,
            "soc_after": 100.0,
            "installed_serial": b["serial_no"],
        },
        headers=h,
    )
    assert swap.status_code == 422
    # 隔离电池不计入可换数
    station = client.get(f"/api/stations/{s['id']}", headers=h).json()
    assert station["battery_isolated"] >= 1

    # 隔离 → 退役终态
    ret = client.post(f"/api/batteries/{b['serial_no']}/retire", json={"reason": "确认衰减"}, headers=h)
    assert ret.status_code == 200
    assert ret.json()["state"] == "retired"
    # 退役后不可再操作
    assert client.post(f"/api/batteries/{b['serial_no']}/isolate", json={"reason": "x"}, headers=h).status_code == 422


def test_installed_battery_cannot_isolate_directly():
    h = _headers()
    s = _station(h)
    v = _vehicle(h, spec="STD-M")
    b = _ready(h, station_id=s["id"], serial=_sn())
    swap = client.post(
        "/api/swaps",
        json={"vehicle_id": v["id"], "station_id": s["id"], "soc_before": 10.0,
              "soc_after": 100.0, "installed_serial": b["serial_no"]},
        headers=h,
    )
    assert swap.status_code == 201, swap.text
    # 车上电池必须先换电卸下，不能直接隔离
    r = client.post(f"/api/batteries/{b['serial_no']}/isolate", json={"reason": "召回"}, headers=h)
    assert r.status_code == 422


# ---------------------------------------------------------------- 换电事务
def test_swap_transaction_two_assets_and_history():
    h = _headers()
    s = _station(h)
    v = _vehicle(h, spec="STD-M")
    b1 = _ready(h, station_id=s["id"], serial=_sn())
    b2 = _ready(h, station_id=s["id"], serial=_sn())

    station_before = client.get(f"/api/stations/{s['id']}", headers=h).json()
    ready_before = station_before["battery_ready"]

    # 第一次换电：装上 b1（车辆此前无在册电池）
    sw1 = client.post(
        "/api/swaps",
        json={"vehicle_id": v["id"], "station_id": s["id"], "soc_before": 12.0,
              "soc_after": 100.0, "installed_serial": b1["serial_no"]},
        headers=h,
    )
    assert sw1.status_code == 201, sw1.text
    assert sw1.json()["installed_serial"] == b1["serial_no"]
    assert sw1.json()["removed_serial"] is None

    b1_after = client.get(f"/api/batteries/{b1['serial_no']}", headers=h).json()
    assert b1_after["state"] == "installed"
    assert b1_after["vehicle_id"] == v["id"]
    assert b1_after["station_id"] is None

    # 第二次换电：明确卸下 b1、装上 b2
    sw2 = client.post(
        "/api/swaps",
        json={"vehicle_id": v["id"], "station_id": s["id"], "soc_before": 8.0,
              "soc_after": 99.0, "removed_serial": b1["serial_no"],
              "installed_serial": b2["serial_no"]},
        headers=h,
    )
    assert sw2.status_code == 201, sw2.text
    rec = sw2.json()
    assert rec["removed_serial"] == b1["serial_no"]
    assert rec["installed_serial"] == b2["serial_no"]

    b1_after2 = client.get(f"/api/batteries/{b1['serial_no']}", headers=h).json()
    b2_after2 = client.get(f"/api/batteries/{b2['serial_no']}", headers=h).json()
    # 卸下：回站待充、低电量；装上：装车、满电
    assert b1_after2["state"] == "pending_charge"
    assert b1_after2["station_id"] == s["id"]
    assert b1_after2["vehicle_id"] is None
    assert b1_after2["current_soc"] == 8.0
    assert b2_after2["state"] == "installed"
    assert b2_after2["vehicle_id"] == v["id"]

    v_after = client.get(f"/api/vehicles/{v['id']}", headers=h).json()
    assert v_after["current_soc"] == 99.0
    assert v_after["current_battery_serial"] == b2["serial_no"]

    # 两次换电：两块满电（b1、b2）先后出库装车，b1 以待充身份回站，
    # 故可换数 -2、待充数 +1。
    station_after = client.get(f"/api/stations/{s['id']}", headers=h).json()
    assert station_after["battery_ready"] == ready_before - 2
    assert station_after["battery_pending"] == station_before["battery_pending"] + 1

    # 完整历史：b1 经历 登记→充→完成→装上→卸下
    hist = client.get(f"/api/batteries/{b1['serial_no']}/history", headers=h).json()
    types = [e["event_type"] for e in hist["events"]]
    assert types == ["register", "charge_start", "charge_complete", "swap_in", "swap_out"]
    assert hist["events"][-1]["to_station_id"] == s["id"]


def test_swap_rejects_incompatible_spec_and_rolls_back():
    h = _headers()
    s = _station(h)
    v = _vehicle(h, spec="STD-L")  # 车辆要求 L
    b = _ready(h, station_id=s["id"], spec="STD-M", serial=_sn())  # 电池是 M

    station_before = client.get(f"/api/stations/{s['id']}", headers=h).json()
    swaps_before = len(client.get("/api/swaps", headers=h).json())

    r = client.post(
        "/api/swaps",
        json={"vehicle_id": v["id"], "station_id": s["id"], "soc_before": 10.0,
              "soc_after": 100.0, "installed_serial": b["serial_no"]},
        headers=h,
    )
    assert r.status_code == 422

    # 失败整体回滚：电池没动、站点可换数不变、没有产生换电记录
    b_after = client.get(f"/api/batteries/{b['serial_no']}", headers=h).json()
    assert b_after["state"] == "ready"
    station_after = client.get(f"/api/stations/{s['id']}", headers=h).json()
    assert station_after["battery_ready"] == station_before["battery_ready"]
    swaps_after = len(client.get("/api/swaps", headers=h).json())
    assert swaps_after == swaps_before


def test_swap_request_id_idempotent():
    h = _headers()
    s = _station(h)
    v = _vehicle(h, spec="STD-M")
    _ready(h, station_id=s["id"], serial=_sn())  # 自动选配兜底
    b = _ready(h, station_id=s["id"], serial=_sn())
    req_id = f"req-{uuid.uuid4().hex}"
    body = {
        "vehicle_id": v["id"], "station_id": s["id"],
        "soc_before": 20.0, "soc_after": 100.0,
        "installed_serial": b["serial_no"], "request_id": req_id,
    }
    first = client.post("/api/swaps", json=body, headers=h)
    assert first.status_code == 201, first.text
    ready_after_first = client.get(f"/api/stations/{s['id']}", headers=h).json()["battery_ready"]

    # 重复上报：返回同一条记录，不产生第二次迁移
    second = client.post("/api/swaps", json=body, headers=h)
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    ready_after_second = client.get(f"/api/stations/{s['id']}", headers=h).json()["battery_ready"]
    assert ready_after_second == ready_after_first


# ---------------------------------------------------------------- 兼容老调用
def test_legacy_swap_auto_selects_and_dedups():
    h = _headers()
    s = _station(h)
    v = _vehicle(h)  # 不声明规格，兼容任意
    _ready(h, station_id=s["id"], serial=_sn())

    body = {"vehicle_id": v["id"], "station_id": s["id"], "soc_before": 30.0, "soc_after": 100.0}
    first = client.post("/api/swaps", json=body, headers=h)
    assert first.status_code == 201, first.text
    assert first.json()["is_legacy"] is True
    assert first.json()["installed_serial"]  # 系统自动选配了一块
    ready_after_first = client.get(f"/api/stations/{s['id']}", headers=h).json()["battery_ready"]

    # 完全相同的老调用短时间内重复：自然键去重，不再扣减可换数
    second = client.post("/api/swaps", json=body, headers=h)
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert client.get(f"/api/stations/{s['id']}", headers=h).json()["battery_ready"] == ready_after_first


def test_removed_serial_must_match_vehicle():
    h = _headers()
    s = _station(h)
    v1 = _vehicle(h, spec="STD-M")
    v2 = _vehicle(h, spec="STD-M")
    b_on_v1 = _ready(h, station_id=s["id"], serial=_sn())
    b_new = _ready(h, station_id=s["id"], serial=_sn())
    # 把 b_on_v1 装到 v1
    client.post("/api/swaps", json={
        "vehicle_id": v1["id"], "station_id": s["id"], "soc_before": 10.0,
        "soc_after": 100.0, "installed_serial": b_on_v1["serial_no"]}, headers=h)
    # v2 想卸下一块根本不在自己车上的电池 → 拒绝
    r = client.post("/api/swaps", json={
        "vehicle_id": v2["id"], "station_id": s["id"], "soc_before": 10.0,
        "soc_after": 100.0, "removed_serial": b_on_v1["serial_no"],
        "installed_serial": b_new["serial_no"]}, headers=h)
    assert r.status_code == 422


# ---------------------------------------------------------------- 汇总一致
def test_station_totals_always_match_detail():
    h = _headers()
    s = _station(h)
    # 登记若干、充满若干、隔离一块，对比列表明细与站点汇总
    _register(h, s["id"])
    r2 = _register(h, s["id"])
    _ready(h, station_id=s["id"], serial=_sn())
    client.post(f"/api/batteries/{r2['serial_no']}/isolate", json={"reason": "抽检"}, headers=h)

    station = client.get(f"/api/stations/{s['id']}", headers=h).json()
    batteries = client.get("/api/batteries", params={"station_id": s["id"]}, headers=h).json()
    by_state = {}
    for b in batteries:
        by_state[b["state"]] = by_state.get(b["state"], 0) + 1
    assert station["battery_ready"] == by_state.get("ready", 0)
    assert station["battery_pending"] == by_state.get("pending_charge", 0)
    assert station["battery_isolated"] == by_state.get("isolated", 0)
    assert station["battery_total"] == len(batteries)
