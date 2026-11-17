"""封存（archiving）场景测试。

覆盖：
- 封存后旧日结果与封存前一致（差值重算 == 原始点现场算）；
- 跨界小时：边界点保留，封存日走差值、未封存日走原始点，边界两侧都可查；
- 边界只能前进（相等/回退拒收）、边界必须 UTC 零点；
- 补传触及边界及更早整点整批拒收，合法补传仍可落库；
- 缺数/倒退在封存后仍为待核；换层级后旧日按新直属分表重算；
- 封存事务失败整体回滚（旧差值/原始点/边界全部不变，旧结果可查）；
- 封存、补传、查询并发交错时的快照一致性。
"""
from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.test_api import full_day_rows, hierarchy_csv, readings_csv, upload_hierarchy, upload_readings

DAY = "2026-01-15"
NEXT_DAY = "2026-01-16"
BOUNDARY = "2026-01-16T00:00:00Z"  # 封存 15 日全天：小时起点 15T00 .. 15T23


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    # raise_server_exceptions=False：让 500 走真实 HTTP 响应路径，便于验证回滚后服务可用
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def archive(client: TestClient, boundary: str):
    return client.post("/api/archive", json={"boundary_utc": boundary})


def daily_net(client: TestClient, meter: str, day: str = DAY):
    resp = client.get(f"/api/meters/{meter}/daily-net", params={"date": day})
    assert resp.status_code == 200, resp.text
    return resp.json()


def seed_standard_day(client: TestClient) -> dict:
    """M -> C1（另含孙表 G1 不扣）；15 日全天 25 点，净值恒为 70。"""
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1"), ("C1", "G1")])).status_code == 200
    rows = (
        full_day_rows("M", 100.0, base=1000.0)
        + full_day_rows("C1", 30.0, base=500.0)
        + full_day_rows("G1", 10.0, base=200.0)
    )
    assert upload_readings(client, readings_csv(rows)).status_code == 200
    return {"before": daily_net(client, "M"), "rows": rows}


# ---------- 1. 基本封存：差值重算与封存前完全一致 ----------

def test_archive_reproduces_old_results_from_deltas(client):
    seed = seed_standard_day(client)
    resp = archive(client, BOUNDARY)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 3 只表 × 24 个完整小时 = 72 条差值；删除 3×24=72 个早于边界的原始点
    assert body["deltas_written"] == 72
    assert body["raw_points_deleted"] == 72
    assert body["previous_boundary_utc"] is None
    assert body["boundary_utc"] == BOUNDARY

    state = client.get("/api/archive").json()
    assert state["boundary_utc"] == BOUNDARY
    assert state["updated_at"] is not None

    after = daily_net(client, "M")
    assert after["data_source"] == "archived_deltas"
    assert after["child_meters"] == ["C1"]
    assert after["hours"] == seed["before"]["hours"]  # 与原始点路径逐字段一致
    for h in after["hours"]:
        assert h["status"] == "ok"
        assert h["net_kwh"] == pytest.approx(70.0)


def test_archive_keeps_boundary_point_and_deletes_older_raw(client):
    seed_standard_day(client)
    archive(client, BOUNDARY)
    conn = sqlite3.connect(client.app.state.db_path)
    # 边界点（次日 00:00）保留：每只表各 1 条
    kept = conn.execute(
        "SELECT meter_id FROM readings WHERE ts_utc = ? ORDER BY meter_id", (BOUNDARY,)
    ).fetchall()
    assert [r[0] for r in kept] == ["C1", "G1", "M"]
    # 早于边界的原始点全部删除
    assert conn.execute(
        "SELECT COUNT(*) FROM readings WHERE ts_utc < ?", (BOUNDARY,)
    ).fetchone()[0] == 0
    # 差值表 72 条，无负值
    assert conn.execute("SELECT COUNT(*) FROM reading_deltas").fetchone()[0] == 72
    assert conn.execute(
        "SELECT COUNT(*) FROM reading_deltas WHERE delta_kwh < 0"
    ).fetchone()[0] == 0
    # 边界已落库
    assert conn.execute("SELECT boundary_utc FROM archive_state WHERE id=1").fetchone()[0] == BOUNDARY
    conn.close()


# ---------- 2. 跨界小时：边界两侧都可查，后续小时靠边界点衔接 ----------

def test_cross_boundary_hours_both_sides_queryable(client):
    # 15 日 + 16 日两天数据（共 49 个点）
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1")])).status_code == 200
    day1 = full_day_rows("M", 100.0) + full_day_rows("C1", 30.0)
    day2_rows = []
    for h in range(1, 25):
        ts = f"2026-01-{16 + h // 24:02d}T{h % 24:02d}:00:00Z"
        day2_rows.append(("M", ts, 1000.0 + 100.0 * (24 + h)))
        day2_rows.append(("C1", ts, 1000.0 + 30.0 * (24 + h)))
    assert upload_readings(client, readings_csv(day1 + day2_rows)).status_code == 200

    before_15 = daily_net(client, "M", DAY)
    before_16 = daily_net(client, "M", NEXT_DAY)

    # 封存边界 = 16 日零点：15 日整日转差值，16 日原始点保留
    assert archive(client, BOUNDARY).status_code == 200

    after_15 = daily_net(client, "M", DAY)
    after_16 = daily_net(client, "M", NEXT_DAY)
    assert after_15["data_source"] == "archived_deltas"
    assert after_16["data_source"] == "raw_readings"
    assert after_15["hours"] == before_15["hours"]
    assert after_16["hours"] == before_16["hours"]

    # 再前进到 17 日零点：只封存新区间，16 日差值与 15 日衔接，边界点继续保留
    resp = archive(client, "2026-01-17T00:00:00Z")
    assert resp.status_code == 200
    assert resp.json()["previous_boundary_utc"] == BOUNDARY
    assert resp.json()["deltas_written"] == 48  # 2 表 × 24 小时
    after_16_v2 = daily_net(client, "M", NEXT_DAY)
    assert after_16_v2["data_source"] == "archived_deltas"
    assert after_16_v2["hours"] == before_16["hours"]
    conn = sqlite3.connect(client.app.state.db_path)
    assert conn.execute(
        "SELECT COUNT(*) FROM readings WHERE ts_utc='2026-01-17T00:00:00Z'"
    ).fetchone()[0] == 2
    conn.close()


# ---------- 3. 边界只能前进 / 必须零点 ----------

def test_boundary_cannot_go_backwards_or_stay(client):
    seed_standard_day(client)
    assert archive(client, BOUNDARY).status_code == 200

    resp = archive(client, BOUNDARY)  # 相等
    assert resp.status_code == 422 and "只能前进" in resp.json()["detail"]
    resp = archive(client, "2026-01-15T00:00:00Z")  # 回退
    assert resp.status_code == 422 and "只能前进" in resp.json()["detail"]
    # 被拒后边界不变
    assert client.get("/api/archive").json()["boundary_utc"] == BOUNDARY
    # 审计留痕
    batches = client.get("/api/batches").json()
    assert any(b["kind"] == "archive" and b["status"] == "rejected" for b in batches)


def test_boundary_must_be_utc_midnight(client):
    seed_standard_day(client)
    resp = archive(client, "2026-01-16T08:00:00Z")
    assert resp.status_code == 422 and "零点" in resp.json()["detail"]
    resp = archive(client, "2026-01-16 00:00:00")  # 缺 UTC 标记
    assert resp.status_code == 422 and "UTC" in resp.json()["detail"]
    assert client.get("/api/archive").json()["boundary_utc"] is None


# ---------- 4. 补传触及边界及更早整点：整批拒收 ----------

def test_backfill_at_or_before_boundary_rejected_entire_batch(client):
    seed_standard_day(client)
    archive(client, BOUNDARY)

    # 一批里既有合法点（边界之后）又有越界点 -> 整批拒收，合法点也不能入库
    mixed = readings_csv([
        ("M", "2026-01-16T01:00:00Z", 9999.0),   # 合法
        ("M", BOUNDARY, 1234.0),                 # 恰好等于边界 -> 拒收
    ])
    resp = upload_readings(client, mixed)
    assert resp.status_code == 422 and "封存边界" in resp.json()["detail"]

    conn = sqlite3.connect(client.app.state.db_path)
    assert conn.execute(
        "SELECT COUNT(*) FROM readings WHERE ts_utc='2026-01-16T01:00:00Z'"
    ).fetchone()[0] == 0
    conn.close()

    # 全部晚于边界的补传正常落库
    ok = upload_readings(client, readings_csv([("M", "2026-01-16T01:00:00Z", 9999.0)]))
    assert ok.status_code == 200
    batches = client.get("/api/batches").json()
    assert any(b["kind"] == "readings" and b["status"] == "rejected" and "封存" in (b["reason"] or "")
               for b in batches)


# ---------- 5. 缺数 / 倒退：封存后仍待核 ----------

def test_missing_points_become_missing_deltas_and_stay_pending(client):
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1")])).status_code == 200
    # C1 缺 03:00 -> 02-03、03-04 两个小时无法形成差值
    rows = full_day_rows("M", 100.0) + full_day_rows("C1", 30.0, skip_hours={3})
    assert upload_readings(client, readings_csv(rows)).status_code == 200
    before = daily_net(client, "M")
    archive(client, BOUNDARY)
    after = daily_net(client, "M")

    assert after["data_source"] == "archived_deltas"
    for h in (2, 3):
        assert after["hours"][h]["status"] == "pending_review"
        assert after["hours"][h]["net_kwh"] is None
        assert "缺整点读数" in after["hours"][h]["review_reason"]
        assert "C1" in after["hours"][h]["review_reason"]
    # 状态分布与封存前一致
    assert [h["status"] for h in after["hours"]] == [h["status"] for h in before["hours"]]
    # 差值表里 C1 确实只有 22 条（缺两个小时）
    conn = sqlite3.connect(client.app.state.db_path)
    assert conn.execute(
        "SELECT COUNT(*) FROM reading_deltas WHERE meter_id='C1'"
    ).fetchone()[0] == 22
    conn.close()


def test_backwards_delta_kept_negative_and_pending(client):
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1")])).status_code == 200
    rows = full_day_rows("M", 100.0) + full_day_rows("C1", 30.0)
    rows = [(m, t, (v - 500.0 if (m == "M" and t == "2026-01-15T05:00:00Z") else v))
            for m, t, v in rows]
    assert upload_readings(client, readings_csv(rows)).status_code == 200
    archive(client, BOUNDARY)
    after = daily_net(client, "M")
    assert after["hours"][4]["status"] == "pending_review"
    assert "读数倒退" in after["hours"][4]["review_reason"]
    assert "M" in after["hours"][4]["review_reason"]
    # 负差值原样留存，没有被钳成 0
    conn = sqlite3.connect(client.app.state.db_path)
    neg = conn.execute(
        "SELECT delta_kwh FROM reading_deltas WHERE meter_id='M' AND hour_start='2026-01-15T04:00:00Z'"
    ).fetchone()[0]
    assert neg < 0
    conn.close()


def test_negative_net_still_pending_after_archive(client):
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1"), ("M", "C2")])).status_code == 200
    rows = full_day_rows("M", 50.0) + full_day_rows("C1", 30.0) + full_day_rows("C2", 50.0)
    assert upload_readings(client, readings_csv(rows)).status_code == 200
    archive(client, BOUNDARY)
    hours = daily_net(client, "M")["hours"]
    assert all(h["status"] == "pending_review" for h in hours)
    assert "净电量为负" in hours[0]["review_reason"]


# ---------- 6. 换层级后旧日按新直属分表重算（差值不绑层级） ----------

def test_hierarchy_change_recomputes_archived_day(client):
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1")])).status_code == 200
    rows = (
        full_day_rows("M", 100.0) + full_day_rows("C1", 30.0) + full_day_rows("C2", 20.0)
    )
    assert upload_readings(client, readings_csv(rows)).status_code == 200
    archive(client, BOUNDARY)

    v1 = daily_net(client, "M")
    assert v1["child_meters"] == ["C1"]
    assert v1["hours"][0]["net_kwh"] == pytest.approx(70.0)

    # 整版替换层级：M 的直属下级改为 C2，旧日按封存差值 + 新关系重算
    assert upload_hierarchy(client, hierarchy_csv([("M", "C2")])).status_code == 200
    v2 = daily_net(client, "M")
    assert v2["data_source"] == "archived_deltas"
    assert v2["child_meters"] == ["C2"]
    assert v2["hours"][0]["net_kwh"] == pytest.approx(80.0)
    assert v2["hours"][0]["master_delta_kwh"] == pytest.approx(100.0)


# ---------- 7. 封存事务失败整体回滚 ----------

def test_failed_archive_rolls_back_everything_and_old_results_remain(client):
    seed = seed_standard_day(client)
    db_path = client.app.state.db_path

    # 注入一个必然失败的触发器：差值写入第一条即 ABORT，模拟封存中途故障
    admin = sqlite3.connect(db_path)
    admin.execute(
        "CREATE TRIGGER fail_archive BEFORE INSERT ON reading_deltas"
        " BEGIN SELECT RAISE(ABORT, 'injected archive failure'); END"
    )
    admin.commit()
    admin.close()

    resp = archive(client, BOUNDARY)
    assert resp.status_code == 500  # 非业务错误：服务端 500，且事务已回滚

    conn = sqlite3.connect(db_path)
    # 边界未推进、差值表为空、原始点一条不少
    assert conn.execute("SELECT COUNT(*) FROM archive_state").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM reading_deltas").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == len(seed["rows"])
    conn.close()

    # 旧查询结果仍可查且不变（走原始点路径）
    after = daily_net(client, "M")
    assert after["data_source"] == "raw_readings"
    assert after["hours"] == seed["before"]["hours"]

    # 去掉触发器后同一边界可正常封存（失败未留下任何半成品）
    admin = sqlite3.connect(db_path)
    admin.execute("DROP TRIGGER fail_archive")
    admin.commit()
    admin.close()
    assert archive(client, BOUNDARY).status_code == 200
    assert daily_net(client, "M")["hours"] == seed["before"]["hours"]


# ---------- 8. 封存 / 补传 / 查询并发交错的快照一致性 ----------

def test_interleaved_archive_backfill_and_query_snapshot(client):
    assert upload_hierarchy(client, hierarchy_csv([("M", "C1")])).status_code == 200
    base_rows = (
        full_day_rows("M", 100.0, base=1000.0)
        + full_day_rows("C1", 30.0, base=500.0)
    )
    assert upload_readings(client, readings_csv(base_rows)).status_code == 200

    errors: list[str] = []
    stop = threading.Event()

    def archiver():
        # 反复尝试推进边界：成功一次，之后只能收到 422（边界只能前进），不允许部分提交
        while not stop.is_set():
            r = archive(client, BOUNDARY)
            if r.status_code not in (200, 422):
                errors.append(f"archive unexpected status {r.status_code}: {r.text}")
                return

    def backfiller():
        # 整体翻转两套完整 25 点数据集（每套内部每小时差值恒定），
        # 因此任何一个查询快照内部都必须自洽；TestClient 跨线程复用连接，
        # 不写"只改单个点"的场景（真实 uvicorn 下每请求独立连接，由 get_conn 保证隔离）
        base = 2000.0
        while not stop.is_set():
            rows = (
                full_day_rows("M", 100.0, base=base)
                + full_day_rows("C1", 30.0, base=base / 2)
            )
            r = upload_readings(client, readings_csv(rows))
            if r.status_code not in (200, 422):  # 封存后该批触及边界 -> 422 也是预期
                errors.append(f"backfill unexpected status {r.status_code}: {r.text}")
                return
            base = 3000.0 if base == 2000.0 else 2000.0

    def reader():
        for _ in range(300):
            result = daily_net(client, "M")
            ok_hours = [h for h in result["hours"] if h["status"] == "ok"]
            # 快照自洽：同一次查询里所有 ok 小时必须来自同一已提交状态——
            # 总表差值彼此相等、净值彼此相等（封存推进/更正切换时不得新旧混杂）
            masters = {h["master_delta_kwh"] for h in ok_hours}
            nets = {h["net_kwh"] for h in ok_hours}
            if len(masters) != 1 or len(nets) != 1:
                errors.append(
                    f"混杂快照 src={result['data_source']}: masters={masters}, nets={nets}"
                )
                return
            # 无论走哪条路径，参与表与每小时字段结构必须完整
            if result["child_meters"] != ["C1"] or len(result["hours"]) != 24:
                errors.append(f"结果结构异常: {result['child_meters']}, {len(result['hours'])}")
                return

    threads = [threading.Thread(target=archiver), threading.Thread(target=backfiller)]
    r = threading.Thread(target=reader)
    for t in threads:
        t.start()
    r.start()
    r.join()
    stop.set()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors[:3]

    # 终态：封存已生效；边界点补传最终必被拒（边界及更早整点不可补传）
    assert client.get("/api/archive").json()["boundary_utc"] == BOUNDARY
    final = upload_readings(client, readings_csv([("M", BOUNDARY, 9000.0)]))
    assert final.status_code == 422 and "封存边界" in final.json()["detail"]
