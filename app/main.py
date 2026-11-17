"""园区公共用电逐小时净电量 API。

- POST /api/hierarchy            上传整版电表层级 CSV（上级表, 下级表），整版替换
- POST /api/readings             按批补传累计电量 CSV（表号, UTC整点, kWh读数）
- POST /api/archive              按 UTC 零点封存：边界前完整小时转逐表差值，删原始点
- GET  /api/archive              查看当前封存边界
- GET  /api/meters/{id}/daily-net?date=YYYY-MM-DD   查询某 UTC 日逐小时净电量
- GET  /api/hierarchy            查看当前已生效层级（便于演示核对）
- GET  /api/batches              查看批次审计（含被拒批次及原因）
- GET  /api/health

一致性约定：每个请求一个事务；校验全部通过后才落库，任一步失败即回滚，
已提交的旧层级/旧读数/旧差值/旧边界/旧查询结果不受影响。封存把"差值写入、
原始点删除、边界推进"放在同一个已提交事务内，查询在单个读事务快照内同时
取边界、层级与读数（或差值），交错执行时看到的必是同一已提交状态。
"""
from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from . import core, db
from .parsing import CsvError, parse_hierarchy_csv, parse_readings_csv, parse_utc_midnight

DEFAULT_DB_PATH = os.environ.get(
    "ENERGY_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "energy.db")
)


class ArchiveRuleError(Exception):
    """违反封存边界规则（边界回退/补传触及已封存整点），整批拒收，HTTP 422。"""


# ---------- 响应模型（让 /docs 自描述） ----------

class BatchResult(BaseModel):
    batch_id: str
    status: str
    kind: str
    rows_applied: int
    message: str


class ArchiveRequest(BaseModel):
    boundary_utc: str = Field(
        ..., description="封存边界（UTC 零点），如 2026-01-16T00:00:00Z；只能前进"
    )


class ArchiveResult(BaseModel):
    batch_id: str
    status: str
    previous_boundary_utc: Optional[str]
    boundary_utc: str
    deltas_written: int
    raw_points_deleted: int
    message: str


class ArchiveState(BaseModel):
    boundary_utc: Optional[str]
    updated_at: Optional[str]


class HourResult(BaseModel):
    hour_start: str
    status: str  # "ok" | "pending_review"
    net_kwh: Optional[float] = None
    master_delta_kwh: Optional[float] = None
    children_delta_kwh: Optional[float] = None
    review_reason: Optional[str] = None


class DailyNetResult(BaseModel):
    meter_id: str
    date: str
    child_meters: list[str]
    # raw_readings=未封存日，沿用原始点现场算差值；archived_deltas=已封存日，按封存差值重算
    data_source: str
    hours: list[HourResult]


class HierarchyEdge(BaseModel):
    parent_meter: str
    child_meter: str


class BatchRecord(BaseModel):
    batch_id: str
    kind: str
    status: str
    reason: Optional[str]
    row_count: int
    created_at: str


# ---------- 应用工厂 ----------

def create_app(db_path: Optional[str] = None) -> FastAPI:
    app = FastAPI(
        title="园区公共用电逐小时净电量 API",
        description=(
            "上传整版电表层级与按批补传累计电量，查询指定总表某 UTC 日的逐小时净电量。"
            "每小时 = 总表相邻整点差值 - 直接下级表差值之和（孙表不再扣）。"
        ),
        version="1.0.0",
    )
    app.state.db_path = os.path.abspath(db_path or DEFAULT_DB_PATH)
    db.init_db(app.state.db_path)

    def get_conn(request: Request):
        conn = db.connect(request.app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _record_rejected(conn, kind: str, reason: str) -> None:
        """拒收批次留痕。独立短事务，不影响主数据。"""
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO batches (batch_id, kind, status, reason, row_count, created_at)"
                " VALUES (?, ?, 'rejected', ?, 0, ?)",
                (str(uuid.uuid4()), kind, reason, _utcnow()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")

    # ---------- 上传：整版层级 ----------

    @app.post("/api/hierarchy", response_model=BatchResult, summary="上传整版电表层级 CSV（整版替换）")
    async def upload_hierarchy(file: UploadFile = File(...), conn=Depends(get_conn)):
        text = await _read_csv_text(file)
        try:
            edges = parse_hierarchy_csv(text)  # 纯函数校验：成环/多上级/自环在此拒收
        except CsvError as exc:
            _record_rejected(conn, "hierarchy", str(exc))
            raise HTTPException(status_code=422, detail=str(exc))

        batch_id = str(uuid.uuid4())
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM hierarchy")  # 整版替换
            conn.executemany(
                "INSERT INTO hierarchy (child_meter, parent_meter) VALUES (?, ?)",
                [(child, parent) for parent, child in edges],
            )
            conn.execute(
                "INSERT INTO batches (batch_id, kind, status, reason, row_count, created_at)"
                " VALUES (?, 'hierarchy', 'applied', NULL, ?, ?)",
                (batch_id, len(edges), _utcnow()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return BatchResult(
            batch_id=batch_id, status="applied", kind="hierarchy",
            rows_applied=len(edges),
            message=f"层级已整版替换，生效 {len(edges)} 条上下级关系",
        )

    # ---------- 上传：一批读数 ----------

    @app.post("/api/readings", response_model=BatchResult, summary="按批补传累计电量 CSV")
    async def upload_readings(file: UploadFile = File(...), conn=Depends(get_conn)):
        text = await _read_csv_text(file)
        try:
            rows = parse_readings_csv(text)  # 批内同键异值在此整批拒收
        except CsvError as exc:
            _record_rejected(conn, "readings", str(exc))
            raise HTTPException(status_code=422, detail=str(exc))

        now = _utcnow()
        batch_id = str(uuid.uuid4())
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 封存边界及更早整点已转为差值且原始点已删，禁止补传：整批拒收。
            # 放在写事务内读取边界，避免与封存推进之间的竞态（读旧边界后通过、落库越界）。
            row = conn.execute("SELECT boundary_utc FROM archive_state WHERE id = 1").fetchone()
            if row is not None:
                boundary = row["boundary_utc"]
                offending = sorted({ts for _, ts, _ in rows if ts <= boundary})
                if offending:
                    raise ArchiveRuleError(
                        f"补传触及封存边界 {boundary} 及更早整点（共 {len(offending)} 个），"
                        f"整批拒收: {', '.join(offending[:10])}"
                    )
            # 同键异值 = 更正（覆盖）；同值重传 = 幂等（WHERE 条件使其不变）
            conn.executemany(
                """
                INSERT INTO readings (meter_id, ts_utc, kwh, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (meter_id, ts_utc) DO UPDATE SET
                    kwh = excluded.kwh, updated_at = excluded.updated_at
                WHERE readings.kwh <> excluded.kwh
                """,
                [(m, ts, kwh, now) for m, ts, kwh in rows],
            )
            conn.execute(
                "INSERT INTO batches (batch_id, kind, status, reason, row_count, created_at)"
                " VALUES (?, 'readings', 'applied', NULL, ?, ?)",
                (batch_id, len(rows), now),
            )
            conn.execute("COMMIT")
        except ArchiveRuleError as exc:
            conn.execute("ROLLBACK")
            _record_rejected(conn, "readings", str(exc))
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return BatchResult(
            batch_id=batch_id, status="applied", kind="readings",
            rows_applied=len(rows),
            message=f"读数批次已提交，{len(rows)} 个（表号, 整点）键已写入/更正",
        )

    # ---------- 封存：按 UTC 零点推进边界 ----------

    @app.post("/api/archive", response_model=ArchiveResult,
              summary="封存边界以前的完整小时：转逐表差值并删除原始点")
    def archive(payload: ArchiveRequest, conn=Depends(get_conn)):
        try:
            boundary = parse_utc_midnight(payload.boundary_utc)
        except CsvError as exc:
            _record_rejected(conn, "archive", str(exc))
            raise HTTPException(status_code=422, detail=str(exc))

        now = _utcnow()
        batch_id = str(uuid.uuid4())
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT boundary_utc FROM archive_state WHERE id = 1"
            ).fetchone()
            previous = row["boundary_utc"] if row is not None else None
            # 边界只能前进：相等或回退一律拒收（封存不可逆）
            if previous is not None and boundary <= previous:
                raise ArchiveRuleError(
                    f"封存边界只能前进: 当前边界 {previous}，收到 {boundary}"
                )
            lower = previous  # 已有边界时只封存 (previous, boundary) 新区间，差值天然不重叠

            # 1) 边界以前的完整小时 -> 逐表差值。
            #    用相邻整点自连接：缺任一端点的小时不产生差值（查询时按缺数待核）；
            #    倒退小时差值为负，原样留存，查询时再判待核。
            if lower is None:
                delta_sql = """
                    INSERT INTO reading_deltas (meter_id, hour_start, delta_kwh, archived_at)
                    SELECT r1.meter_id, r1.ts_utc, r2.kwh - r1.kwh, ?
                    FROM readings r1
                    JOIN readings r2
                      ON r2.meter_id = r1.meter_id
                     AND r2.ts_utc = strftime('%Y-%m-%dT%H:00:00Z', r1.ts_utc, '+1 hour')
                    WHERE r1.ts_utc < ?
                    ON CONFLICT (meter_id, hour_start) DO UPDATE SET
                        delta_kwh = excluded.delta_kwh, archived_at = excluded.archived_at
                """
                cur = conn.execute(delta_sql, (now, boundary))
            else:
                delta_sql = """
                    INSERT INTO reading_deltas (meter_id, hour_start, delta_kwh, archived_at)
                    SELECT r1.meter_id, r1.ts_utc, r2.kwh - r1.kwh, ?
                    FROM readings r1
                    JOIN readings r2
                      ON r2.meter_id = r1.meter_id
                     AND r2.ts_utc = strftime('%Y-%m-%dT%H:00:00Z', r1.ts_utc, '+1 hour')
                    WHERE r1.ts_utc >= ? AND r1.ts_utc < ?
                    ON CONFLICT (meter_id, hour_start) DO UPDATE SET
                        delta_kwh = excluded.delta_kwh, archived_at = excluded.archived_at
                """
                cur = conn.execute(delta_sql, (now, lower, boundary))
            deltas_written = cur.rowcount

            # 2) 删除早于边界的原始点；边界点保留，供后续小时与新差值衔接
            deleted = conn.execute(
                "DELETE FROM readings WHERE ts_utc < ?", (boundary,)
            ).rowcount

            # 3) 推进边界（单行表 upsert）——与上面两步同一事务同时生效
            conn.execute(
                "INSERT INTO archive_state (id, boundary_utc, updated_at) VALUES (1, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET"
                " boundary_utc = excluded.boundary_utc, updated_at = excluded.updated_at",
                (boundary, now),
            )
            conn.execute(
                "INSERT INTO batches (batch_id, kind, status, reason, row_count, created_at)"
                " VALUES (?, 'archive', 'applied', NULL, ?, ?)",
                (batch_id, deltas_written, now),
            )
            conn.execute("COMMIT")
        except ArchiveRuleError as exc:
            conn.execute("ROLLBACK")
            _record_rejected(conn, "archive", str(exc))
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception:
            conn.execute("ROLLBACK")
            raise

        return ArchiveResult(
            batch_id=batch_id,
            status="applied",
            previous_boundary_utc=previous,
            boundary_utc=boundary,
            deltas_written=deltas_written,
            raw_points_deleted=deleted,
            message=(
                f"已封存至 {boundary}：写入 {deltas_written} 条逐表小时差值，"
                f"删除 {deleted} 个早于边界的原始整点，边界点保留用于衔接"
            ),
        )

    @app.get("/api/archive", response_model=ArchiveState, summary="查看当前封存边界")
    def get_archive_state(conn=Depends(get_conn)):
        conn.execute("BEGIN")
        try:
            row = conn.execute(
                "SELECT boundary_utc, updated_at FROM archive_state WHERE id = 1"
            ).fetchone()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        if row is None:
            return ArchiveState(boundary_utc=None, updated_at=None)
        return ArchiveState(boundary_utc=row["boundary_utc"], updated_at=row["updated_at"])

    # ---------- 查询：逐小时净电量 ----------

    @app.get(
        "/api/meters/{meter_id}/daily-net",
        response_model=DailyNetResult,
        summary="查询指定总表某 UTC 日的逐小时净电量",
    )
    def daily_net(
        meter_id: str,
        date_: str = Query(..., alias="date", description="UTC 日期，格式 YYYY-MM-DD"),
        conn=Depends(get_conn),
    ):
        try:
            day = date.fromisoformat(date_)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"日期格式应为 YYYY-MM-DD: {date_!r}")

        # 单事务快照：边界、层级与读数（或差值）取自同一已提交状态
        conn.execute("BEGIN")
        try:
            boundary_row = conn.execute(
                "SELECT boundary_utc FROM archive_state WHERE id = 1"
            ).fetchone()
            boundary = boundary_row["boundary_utc"] if boundary_row is not None else None
            children = [
                r["child_meter"]
                for r in conn.execute(
                    "SELECT child_meter FROM hierarchy WHERE parent_meter = ?"
                    " ORDER BY child_meter",
                    (meter_id,),
                )
            ]
            participants = [meter_id, *children]
            placeholders = ",".join("?" for _ in participants)
            t_start, t_end = core.hour_str(day, 0), core.hour_str(day, 24)

            # 整日都在边界以前（次日零点 <= 边界）-> 封存差值重算；
            # 否则沿用原始点查询（含跨界日：边界只在零点，整日不会被拆分）。
            if boundary is not None and t_end <= boundary:
                delta_rows = conn.execute(
                    f"SELECT meter_id, hour_start, delta_kwh FROM reading_deltas"
                    f" WHERE meter_id IN ({placeholders})"
                    f" AND hour_start >= ? AND hour_start < ?",
                    (*participants, t_start, t_end),
                ).fetchall()
                deltas = {(r["meter_id"], r["hour_start"]): r["delta_kwh"] for r in delta_rows}
                hours = core.compute_daily_net_from_deltas(meter_id, day, children, deltas)
                data_source = "archived_deltas"
            else:
                rows = conn.execute(
                    f"SELECT meter_id, ts_utc, kwh FROM readings"
                    f" WHERE meter_id IN ({placeholders}) AND ts_utc BETWEEN ? AND ?",
                    (*participants, t_start, t_end),
                ).fetchall()
                readings = {(r["meter_id"], r["ts_utc"]): r["kwh"] for r in rows}
                hours = core.compute_daily_net(meter_id, day, children, readings)
                data_source = "raw_readings"
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        return DailyNetResult(
            meter_id=meter_id, date=day.isoformat(), child_meters=children,
            data_source=data_source, hours=hours,
        )

    # ---------- 辅助端点 ----------

    @app.get("/api/hierarchy", response_model=list[HierarchyEdge], summary="查看当前已生效层级")
    def get_hierarchy(conn=Depends(get_conn)):
        conn.execute("BEGIN")
        try:
            rows = conn.execute(
                "SELECT parent_meter, child_meter FROM hierarchy"
                " ORDER BY parent_meter, child_meter"
            ).fetchall()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return [HierarchyEdge(**dict(r)) for r in rows]

    @app.get("/api/batches", response_model=list[BatchRecord], summary="批次审计（含被拒批次）")
    def list_batches(conn=Depends(get_conn)):
        conn.execute("BEGIN")
        try:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return [BatchRecord(**dict(r)) for r in rows]

    @app.get("/api/health", summary="健康检查")
    def health():
        return {"status": "ok"}

    return app


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _read_csv_text(file: UploadFile) -> str:
    raw = await file.read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="CSV 必须是 UTF-8 编码")


app = create_app()
