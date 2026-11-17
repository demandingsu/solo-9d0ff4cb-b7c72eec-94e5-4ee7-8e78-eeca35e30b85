"""逐小时净电量计算。

规则（与需求逐条对应）：
- 每小时净电量 = 总表相邻整点差值 - 直接下级表差值之和；孙表及更深不再扣；
- 参与表（总表 + 全部直接下级）缺任一整点 -> 该小时只给待核原因；
- 任一参与表读数倒退（差值为负）-> 该小时只给待核原因；
- 净值为负 -> 该小时只给待核原因。

两条数据来源：
- compute_daily_net：未封存日，差值由原始累计读数现场相减得到；
- compute_daily_net_from_deltas：已封存日，差值直接取自 reading_deltas
  （封存时按表逐小时保存），层级关系仍在查询时读取——因此换层级后旧日可重算。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from .parsing import TS_FORMAT

EPS = 1e-9  # 浮点容差：|x| <= EPS 视为 0


def hour_str(day: date, hour: int) -> str:
    dt = datetime.combine(day, time(hour % 24), tzinfo=timezone.utc)
    if hour == 24:
        dt += timedelta(days=1)
    return dt.strftime(TS_FORMAT)


def _empty_entry(t0: str) -> dict:
    return {
        "hour_start": t0,
        "status": "ok",
        "net_kwh": None,
        "master_delta_kwh": None,
        "children_delta_kwh": None,
        "review_reason": None,
    }


def _finalize(entry: dict, meter_id: str, children: list[str], deltas: dict[str, float]) -> None:
    """差值齐备时的公共判定：倒退待核、负净值待核，否则给出净值。"""
    backwards = [
        f"表 {m} 读数倒退: 小时差值 {deltas[m]} 为负"
        for m in [meter_id, *children]
        if deltas[m] < -EPS
    ]
    if backwards:
        entry["status"] = "pending_review"
        entry["review_reason"] = "; ".join(backwards)
        return

    master_delta = deltas[meter_id]
    children_delta = sum(deltas[c] for c in children)
    net = master_delta - children_delta
    entry["master_delta_kwh"] = master_delta
    entry["children_delta_kwh"] = children_delta

    if net < -EPS:
        entry["status"] = "pending_review"
        entry["review_reason"] = (
            f"净电量为负: 总表差值 {master_delta} - 直接下级差值和 "
            f"{children_delta} = {net}"
        )
    else:
        entry["net_kwh"] = 0.0 if abs(net) <= EPS else net


def compute_daily_net(
    meter_id: str,
    day: date,
    children: list[str],
    readings: dict[tuple[str, str], float],
) -> list[dict]:
    """计算 24 个小时的净电量（未封存日：原始累计读数现场算差值）。

    readings: {(meter_id, ts_utc): kwh}，由调用方在同一事务快照内取出。
    """
    participants = [meter_id, *children]
    hours: list[dict] = []

    for h in range(24):
        t0, t1 = hour_str(day, h), hour_str(day, h + 1)
        entry = _empty_entry(t0)

        # 1) 参与表缺任一整点
        missing = [
            f"表 {m} 缺 {t}" for m in participants for t in (t0, t1) if (m, t) not in readings
        ]
        if missing:
            entry["status"] = "pending_review"
            entry["review_reason"] = "缺整点读数: " + "; ".join(missing)
            hours.append(entry)
            continue

        # 2) 读数倒退
        deltas = {m: readings[(m, t1)] - readings[(m, t0)] for m in participants}
        backwards = [
            f"表 {m} 读数倒退: {t0}={readings[(m, t0)]} -> {t1}={readings[(m, t1)]}"
            for m in participants
            if deltas[m] < -EPS
        ]
        if backwards:
            entry["status"] = "pending_review"
            entry["review_reason"] = "; ".join(backwards)
            hours.append(entry)
            continue

        master_delta = deltas[meter_id]
        children_delta = sum(deltas[c] for c in children)
        net = master_delta - children_delta
        entry["master_delta_kwh"] = master_delta
        entry["children_delta_kwh"] = children_delta

        # 3) 净值为负
        if net < -EPS:
            entry["status"] = "pending_review"
            entry["review_reason"] = (
                f"净电量为负: 总表差值 {master_delta} - 直接下级差值和 "
                f"{children_delta} = {net}"
            )
        else:
            entry["net_kwh"] = 0.0 if abs(net) <= EPS else net
        hours.append(entry)

    return hours


def compute_daily_net_from_deltas(
    meter_id: str,
    day: date,
    children: list[str],
    deltas: dict[tuple[str, str], float],
) -> list[dict]:
    """计算 24 个小时的净电量（已封存日：差值直接取自封存表）。

    deltas: {(meter_id, hour_start): delta_kwh}，由调用方在同一事务快照内取出。
    缺差值等价于原始路径的缺整点（封存时缺一个端点就无法形成该小时差值）。
    倒退差值按负值原样封存，在此仍判待核；层级按查询时的当前关系取直接下级。
    """
    participants = [meter_id, *children]
    hours: list[dict] = []

    for h in range(24):
        t0 = hour_str(day, h)
        entry = _empty_entry(t0)

        # 1) 参与表缺该小时差值（封存前即缺整点，无法补回）
        missing = [f"表 {m} 缺封存差值: {t0}" for m in participants if (m, t0) not in deltas]
        if missing:
            entry["status"] = "pending_review"
            entry["review_reason"] = "缺整点读数: " + "; ".join(missing)
            hours.append(entry)
            continue

        hour_deltas = {m: deltas[(m, t0)] for m in participants}
        # 2) 倒退（负差值）/ 3) 负净值，与原始路径同一套判定
        _finalize(entry, meter_id, children, hour_deltas)
        hours.append(entry)

    return hours
