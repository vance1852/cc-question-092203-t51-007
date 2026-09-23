"""电池资产追溯体系测试：状态机、换电事务、兼容模式、幂等、召回拦截与对账。"""
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


def _serial() -> str:
    return f"TST-{uuid.uuid4().hex[:10].upper()}"


def _make_station(headers, name="临时测试站", slot_total=20) -> int:
    r = client.post("/api/stations", json={"name": name, "slot_total": slot_total}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_vehicle(headers, spec="STD-100", soc=10.0) -> dict:
    plate = f"测{uuid.uuid4().hex[:6]}"
    r = client.post(
        "/api/vehicles",
        json={"plate": plate, "model": "资产测试车", "battery_spec": spec, "current_soc": soc},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _make_ready_battery(headers, station_id, spec="STD-100", soc=100.0) -> str:
    """登记 -> 充电 -> 充满，返回序列号。"""
    serial = _serial()
    r = client.post(
        "/api/batteries",
        json={"serial_no": serial, "spec": spec, "capacity_kwh": 100.0, "station_id": station_id},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "pending"
    assert client.post(f"/api/batteries/{serial}/charge-start", json={}, headers=headers).status_code == 200
    done = client.post(
        f"/api/batteries/{serial}/charge-complete", json={"soc": soc}, headers=headers
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "ready"
    return serial


# ---------- 建档与状态机 ----------

def test_register_duplicate_serial_conflicts():
    h = _headers()
    sid = _make_station(h)
    serial = _serial()
    body = {"serial_no": serial, "spec": "STD-100", "station_id": sid}
    assert client.post("/api/batteries", json=body, headers=h).status_code == 201
    dup = client.post("/api/batteries", json=body, headers=h)
    assert dup.status_code == 409


def test_register_unknown_station_404():
    h = _headers()
    r = client.post(
        "/api/batteries",
        json={"serial_no": _serial(), "spec": "STD-100", "station_id": 999999},
        headers=h,
    )
    assert r.status_code == 404


def test_illegal_transition_rejected_and_atomic():
    """待充电池直接上报充电完成必须失败，且状态/汇总不变。"""
    h = _headers()
    sid = _make_station(h)
    serial = _serial()
    client.post(
        "/api/batteries",
        json={"serial_no": serial, "spec": "STD-100", "station_id": sid},
        headers=h,
    )
    bad = client.post(f"/api/batteries/{serial}/charge-complete", json={"soc": 100.0}, headers=h)
    assert bad.status_code == 422

    b = client.get(f"/api/batteries/{serial}", headers=h).json()
    assert b["status"] == "pending"
    assert client.get(f"/api/stations/{sid}", headers=h).json()["battery_ready"] == 0
    # 历史只有建档一条，没有产生虚假的迁移台账
    hist = client.get(f"/api/batteries/{serial}/history", headers=h).json()
    assert [e["event_type"] for e in hist["events"]] == ["register"]


def test_charge_lifecycle_updates_station_summary():
    h = _headers()
    sid = _make_station(h)
    serial = _make_ready_battery(h, sid)
    assert client.get(f"/api/stations/{sid}", headers=h).json()["battery_ready"] == 1
    b = client.get(f"/api/batteries/{serial}", headers=h).json()
    assert b["station_id"] == sid and b["soc"] == 100.0


# ---------- 换电（资产模式）----------

def test_asset_swap_links_two_batteries_vehicle_and_station():
    h = _headers()
    sid = _make_station(h)
    serial_in = _make_ready_battery(h, sid)
    vehicle = _make_vehicle(h)

    swap = client.post(
        "/api/swaps",
        json={
            "vehicle_id": vehicle["id"],
            "station_id": sid,
            "battery_in_serial": serial_in,
            "soc_before": 10.0,
            "soc_after": 100.0,
        },
        headers=h,
    )
    assert swap.status_code == 201, swap.text
    data = swap.json()
    assert data["legacy_mode"] is False
    assert data["battery_in_serial"] == serial_in
    assert data["battery_out_serial"] is None

    b = client.get(f"/api/batteries/{serial_in}", headers=h).json()
    assert b["status"] == "installed"
    assert b["vehicle_id"] == vehicle["id"]
    assert b["station_id"] is None
    v = client.get(f"/api/vehicles/{vehicle['id']}", headers=h).json()
    assert v["current_battery_id"] == b["id"]
    assert v["current_soc"] == 100.0
    # 一块可换电池出库，站点汇总同步减一
    assert client.get(f"/api/stations/{sid}", headers=h).json()["battery_ready"] == 0

    # 台账完整记录出库
    hist = client.get(f"/api/batteries/{serial_in}/history", headers=h).json()
    types = [e["event_type"] for e in hist["events"]]
    assert types == ["register", "charge_start", "charge_complete", "swap_out"]
    assert hist["events"][-1]["swap_record_id"] == data["id"]


def test_second_swap_requires_and_validates_out_serial():
    h = _headers()
    sid = _make_station(h)
    first_in = _make_ready_battery(h, sid)
    second_in = _make_ready_battery(h, sid)
    vehicle = _make_vehicle(h)

    r1 = client.post(
        "/api/swaps",
        json={"vehicle_id": vehicle["id"], "station_id": sid, "battery_in_serial": first_in},
        headers=h,
    )
    assert r1.status_code == 201, r1.text

    # 车上已有电池却不传卸下序列号 -> 报错信息应带出车上电池
    missing = client.post(
        "/api/swaps",
        json={"vehicle_id": vehicle["id"], "station_id": sid, "battery_in_serial": second_in},
        headers=h,
    )
    assert missing.status_code == 422
    assert first_in in missing.json()["detail"]

    # 卸下序列号与车上实际电池不符 -> 拒绝
    other = _make_ready_battery(h, sid)
    mismatch = client.post(
        "/api/swaps",
        json={
            "vehicle_id": vehicle["id"],
            "station_id": sid,
            "battery_in_serial": second_in,
            "battery_out_serial": other,
        },
        headers=h,
    )
    assert mismatch.status_code == 422

    # 正确上报两块电池：换电成功（车辆运营后亏电至 8%）
    ok = client.post(
        "/api/swaps",
        json={
            "vehicle_id": vehicle["id"],
            "station_id": sid,
            "battery_in_serial": second_in,
            "battery_out_serial": first_in,
            "soc_before": 8.0,
            "soc_after": 100.0,
        },
        headers=h,
    )
    assert ok.status_code == 201, ok.text
    out = ok.json()
    assert out["battery_in_serial"] == second_in
    assert out["battery_out_serial"] == first_in

    # 卸下的旧包带着亏电电量回到本站待充，新包装车，站点汇总只剩 other 一块可换
    old = client.get(f"/api/batteries/{first_in}", headers=h).json()
    assert old["status"] == "pending"
    assert old["station_id"] == sid and old["vehicle_id"] is None
    assert old["soc"] == 8.0
    new = client.get(f"/api/batteries/{second_in}", headers=h).json()
    assert new["status"] == "installed" and new["vehicle_id"] == vehicle["id"]
    assert client.get(f"/api/stations/{sid}", headers=h).json()["battery_ready"] == 1


def test_spec_incompatibility_blocks_swap():
    h = _headers()
    sid = _make_station(h)
    std100 = _make_ready_battery(h, sid, spec="STD-100")
    vehicle = _make_vehicle(h, spec="STD-160")

    r = client.post(
        "/api/swaps",
        json={"vehicle_id": vehicle["id"], "station_id": sid, "battery_in_serial": std100},
        headers=h,
    )
    assert r.status_code == 422
    assert "车型不兼容" in r.json()["detail"]
    # 被拒电池仍是可换、未离站
    b = client.get(f"/api/batteries/{std100}", headers=h).json()
    assert b["status"] == "ready" and b["station_id"] == sid


def test_failed_swap_leaves_no_partial_migration():
    """装上电池存在但卸下校验失败：两块资产、车辆、站点汇总全部保持原状。"""
    h = _headers()
    sid = _make_station(h)
    first_in = _make_ready_battery(h, sid)
    second_in = _make_ready_battery(h, sid)
    vehicle = _make_vehicle(h)
    client.post(
        "/api/swaps",
        json={"vehicle_id": vehicle["id"], "station_id": sid, "battery_in_serial": first_in},
        headers=h,
    )
    ready_before = client.get(f"/api/stations/{sid}", headers=h).json()["battery_ready"]

    bad = client.post(
        "/api/swaps",
        json={
            "vehicle_id": vehicle["id"],
            "station_id": sid,
            "battery_in_serial": second_in,
            "battery_out_serial": "NOT-EXIST",
        },
        headers=h,
    )
    assert bad.status_code == 404

    v = client.get(f"/api/vehicles/{vehicle['id']}", headers=h).json()
    assert v["current_battery_id"] == client.get(
        f"/api/batteries/{first_in}", headers=h
    ).json()["id"]
    still_ready = client.get(f"/api/batteries/{second_in}", headers=h).json()
    assert still_ready["status"] == "ready"
    assert client.get(f"/api/stations/{sid}", headers=h).json()["battery_ready"] == ready_before


# ---------- 召回拦截 ----------

def test_isolated_battery_cannot_leave_station():
    h = _headers()
    sid = _make_station(h)
    serial = _make_ready_battery(h, sid)
    vehicle = _make_vehicle(h)

    r = client.post(
        f"/api/batteries/{serial}/isolate", json={"reason": "疑似衰减批次召回"}, headers=h
    )
    assert r.status_code == 200
    assert r.json()["status"] == "isolated"
    assert client.get(f"/api/stations/{sid}", headers=h).json()["battery_ready"] == 0

    # 召回电池不能再次出库
    swap = client.post(
        "/api/swaps",
        json={"vehicle_id": vehicle["id"], "station_id": sid, "battery_in_serial": serial},
        headers=h,
    )
    assert swap.status_code == 422
    assert "隔离" in swap.json()["detail"]

    # 恢复后（满电）回到可换，才能继续出库
    assert client.post(f"/api/batteries/{serial}/restore", json={}, headers=h).status_code == 200
    assert client.get(f"/api/batteries/{serial}", headers=h).json()["status"] == "ready"
    ok = client.post(
        "/api/swaps",
        json={"vehicle_id": vehicle["id"], "station_id": sid, "battery_in_serial": serial},
        headers=h,
    )
    assert ok.status_code == 201, ok.text


def test_isolate_installed_battery_unlinks_vehicle():
    """召回涉及装车中的电池：强制下车，车辆指针在同一事务解联，历史保留车辆信息。"""
    h = _headers()
    sid = _make_station(h)
    serial = _make_ready_battery(h, sid)
    vehicle = _make_vehicle(h)
    swap = client.post(
        "/api/swaps",
        json={"vehicle_id": vehicle["id"], "station_id": sid, "battery_in_serial": serial},
        headers=h,
    )
    assert swap.status_code == 201

    r = client.post(
        f"/api/batteries/{serial}/isolate", json={"reason": "在途召回"}, headers=h
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "isolated"
    assert r.json()["vehicle_id"] is None
    v = client.get(f"/api/vehicles/{vehicle['id']}", headers=h).json()
    assert v["current_battery_id"] is None

    # 台账仍记录它装过哪辆车
    hist = client.get(f"/api/batteries/{serial}/history", headers=h).json()
    assert hist["events"][-1]["event_type"] == "isolate"
    assert hist["events"][-1]["vehicle_id"] == vehicle["id"]


def test_retired_battery_is_terminal():
    h = _headers()
    sid = _make_station(h)
    serial = _make_ready_battery(h, sid)
    assert client.post(
        f"/api/batteries/{serial}/retire", json={"reason": "寿命到期"}, headers=h
    ).status_code == 200
    # 退役后不能再隔离/恢复/充电
    assert client.post(f"/api/batteries/{serial}/isolate", json={"reason": "x"}, headers=h).status_code == 422
    assert client.post(f"/api/batteries/{serial}/restore", json={}, headers=h).status_code == 422


# ---------- 调拨 ----------

def test_transfer_moves_ready_asset_and_both_summaries():
    h = _headers()
    s1 = _make_station(h, "调出站")
    s2 = _make_station(h, "调入站")
    serial = _make_ready_battery(h, s1)
    assert client.get(f"/api/stations/{s1}", headers=h).json()["battery_ready"] == 1

    r = client.post(f"/api/batteries/{serial}/transfer", json={"to_station_id": s2}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["station_id"] == s2
    assert client.get(f"/api/stations/{s1}", headers=h).json()["battery_ready"] == 0
    assert client.get(f"/api/stations/{s2}", headers=h).json()["battery_ready"] == 1

    # 装车中/隔离中的电池不能调拨
    assert client.post(
        f"/api/batteries/{serial}/transfer", json={"to_station_id": s1}, headers=h
    ).status_code == 200
    vehicle = _make_vehicle(h)
    client.post(
        "/api/swaps",
        json={"vehicle_id": vehicle["id"], "station_id": s1, "battery_in_serial": serial},
        headers=h,
    )
    blocked = client.post(
        f"/api/batteries/{serial}/transfer", json={"to_station_id": s2}, headers=h
    )
    assert blocked.status_code == 422


# ---------- 幂等 ----------

def test_charge_complete_idempotent():
    h = _headers()
    sid = _make_station(h)
    serial = _serial()
    client.post(
        "/api/batteries",
        json={"serial_no": serial, "spec": "STD-100", "station_id": sid},
        headers=h,
    )
    client.post(f"/api/batteries/{serial}/charge-start", json={}, headers=h)
    key = f"evt-{uuid.uuid4().hex}"
    body = {"soc": 100.0, "request_id": key}
    first = client.post(f"/api/batteries/{serial}/charge-complete", json=body, headers=h)
    second = client.post(f"/api/batteries/{serial}/charge-complete", json=body, headers=h)
    assert first.status_code == 200 and second.status_code == 200

    hist = client.get(f"/api/batteries/{serial}/history", headers=h).json()
    complete_events = [e for e in hist["events"] if e["event_type"] == "charge_complete"]
    assert len(complete_events) == 1  # 重复上报没有制造第二次迁移
    assert client.get(f"/api/stations/{sid}", headers=h).json()["battery_ready"] == 1


def test_swap_idempotent():
    h = _headers()
    sid = _make_station(h)
    serial_in = _make_ready_battery(h, sid)
    vehicle = _make_vehicle(h)
    key = f"swap-{uuid.uuid4().hex}"
    body = {
        "vehicle_id": vehicle["id"],
        "station_id": sid,
        "battery_in_serial": serial_in,
        "request_id": key,
    }
    first = client.post("/api/swaps", json=body, headers=h)
    second = client.post("/api/swaps", json=body, headers=h)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    # 电池只出库一次，站点汇总只减一次
    hist = client.get(f"/api/batteries/{serial_in}/history", headers=h).json()
    assert [e["event_type"] for e in hist["events"]].count("swap_out") == 1
    assert client.get(f"/api/stations/{sid}", headers=h).json()["battery_ready"] == 0


# ---------- 兼容模式 ----------

def test_legacy_payload_still_works_with_notice():
    """旧调用方只传电量：自动选包、标记 legacy_mode、给升级提示，且资产账依然自洽。"""
    h = _headers()
    sid = _make_station(h)
    _make_ready_battery(h, sid, spec="STD-100")
    vehicle = _make_vehicle(h, spec="STD-100", soc=12.0)

    r = client.post(
        "/api/swaps",
        json={"vehicle_id": vehicle["id"], "station_id": sid, "soc_before": 12.0, "soc_after": 100.0},
        headers=h,
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["legacy_mode"] is True
    assert data["battery_in_serial"] is not None
    assert "升级" in data["notice"]
    assert client.get(f"/api/stations/{sid}", headers=h).json()["battery_ready"] == 0


def test_legacy_mode_ignores_incompatible_pool():
    """兼容模式也只能自动选到同规格电池；无兼容电池时按 422 失败且不动账。"""
    h = _headers()
    sid = _make_station(h)
    _make_ready_battery(h, sid, spec="STD-40")
    vehicle = _make_vehicle(h, spec="STD-160")
    r = client.post(
        "/api/swaps",
        json={"vehicle_id": vehicle["id"], "station_id": sid, "soc_before": 5.0, "soc_after": 100.0},
        headers=h,
    )
    assert r.status_code == 422
    assert client.get(f"/api/stations/{sid}", headers=h).json()["battery_ready"] == 1


# ---------- 历史与对账 ----------

def test_history_unknown_serial_404():
    h = _headers()
    assert client.get("/api/batteries/NO-SUCH/history", headers=h).status_code == 404


def test_reconcile_consistent_after_all_operations():
    r = client.get("/api/batteries/reconcile", headers=_headers())
    assert r.status_code == 200
    report = r.json()
    assert report["consistent"] is True
    for item in report["stations"]:
        assert item["summary_battery_ready"] == item["actual_ready_assets"]


def test_seed_data_is_internally_consistent():
    """种子数据：每站汇总等于明细，仪表盘新增资产统计可用。"""
    h = _headers()
    stations = client.get("/api/stations", headers=h).json()
    batteries = client.get("/api/batteries", headers=h).json()
    for s in stations:
        ready = sum(
            1 for b in batteries if b["station_id"] == s["id"] and b["status"] == "ready"
        )
        assert s["battery_ready"] == ready
    stats = client.get("/api/dashboard/stats", headers=h).json()
    assert stats["battery_total"] >= 49
    assert stats["battery_ready_total"] == sum(1 for b in batteries if b["status"] == "ready")
    assert stats["battery_isolated"] >= 1
