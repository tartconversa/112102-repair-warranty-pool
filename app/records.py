"""本地登记：替换交接动作、复验放行与审计快照。

与来源 CSV 分离存放，重新导入来源不会清空人工登记。登记动作受状态机
约束：不能跳过调出直接确认交回；已确认调出的链不接受“撤销”，只接受
后续推进动作。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import store
from .clock import build_view
from .warranty import build_chains

# 允许的本地动作及其必须存在的前置动作
ACTION_PREREQ = {
    "reserved": {"requested"},
    "dispatched": {"reserved", "requested"},
    "awaiting_inspection": {"dispatched"},
    "return_confirmed": {"dispatched", "awaiting_inspection"},
    "receipt_recorded": {"dispatched", "return_confirmed"},
}


class RegistrationError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def register_event(
    conn,
    *,
    event: str,
    spare: str,
    site: str,
    returned_part: str,
    action: str,
    day: int,
    term: str | None = None,
    note: str = "",
) -> None:
    """登记一条本地替换交接事件（含来源与本地链的统一校验）。"""
    if action not in store.VALID_ACTIONS:
        raise RegistrationError(f"不支持的动作: {action}")
    if action not in ACTION_PREREQ and action != "requested":
        raise RegistrationError(f"动作 {action} 不允许本地登记")
    if conn.execute(
        "SELECT 1 FROM replacement_events WHERE event = ? "
        "UNION ALL SELECT 1 FROM local_events WHERE event = ?",
        (event, event),
    ).fetchone():
        raise RegistrationError(f"事件编号 {event} 已存在")

    # 引用完整性
    if not conn.execute(
        "SELECT 1 FROM service_intervals WHERE part = ?", (returned_part,)
    ).fetchone():
        raise RegistrationError(f"部件 {returned_part} 不存在")
    if not conn.execute(
        "SELECT 1 FROM interchange WHERE spare = ?", (spare,)
    ).fetchone():
        raise RegistrationError(f"备件 {spare} 不存在于互换台账")
    if term is not None and not conn.execute(
        "SELECT 1 FROM warranty_terms WHERE term = ?", (term,)
    ).fetchone():
        raise RegistrationError(f"条款 {term} 不存在")

    # 前置状态：来源事件与本地事件合并判断
    prior = [
        dict(r) for r in conn.execute(
            "SELECT action, day FROM replacement_events WHERE spare = ? "
            "AND site = ? AND returned_part = ? "
            "UNION ALL SELECT action, day FROM local_events WHERE spare = ? "
            "AND site = ? AND returned_part = ?",
            (spare, site, returned_part, spare, site, returned_part),
        ).fetchall()
    ]
    prior_actions = {r["action"] for r in prior}
    if action != "requested":
        if not (prior_actions & ACTION_PREREQ[action]):
            raise RegistrationError(
                f"动作 {action} 缺少前置动作之一: "
                f"{sorted(ACTION_PREREQ[action])}；当前链上动作: "
                f"{sorted(prior_actions) or '无'}"
            )
        last_day = max((r["day"] for r in prior), default=0)
        if day < last_day:
            raise RegistrationError(
                f"登记日 {day} 早于链上最近动作日 {last_day}，"
                "交接记录不能倒填到已确认事实之前"
            )

    with conn:
        conn.execute(
            "INSERT INTO local_events(event, spare, site, returned_part, "
            "term, action, day, note, created_at) VALUES (?, ?, ?, ?, ?, "
            "?, ?, ?, ?)",
            (event, spare, site, returned_part, term, action, day, note, _now()),
        )


def register_approval(
    conn, *, spare: str, site: str, approval: str, known_day: int,
    note: str = "",
) -> None:
    """登记备件复验结论（只能把 inspection_required 推进为 approved）。"""
    if approval not in store.VALID_APPROVALS:
        raise RegistrationError(f"非法许可状态: {approval}")
    if not conn.execute(
        "SELECT 1 FROM interchange WHERE spare = ? AND site = ?",
        (spare, site),
    ).fetchone():
        raise RegistrationError(f"互换台账中没有 {spare}@{site}")
    view = build_view(conn, known_day)
    current = view.approvals.get((spare, site))
    if current is not None and current.approval == "approved" and approval == "approved":
        raise RegistrationError("该备件在该站点已放行，无需重复登记")
    with conn:
        conn.execute(
            "INSERT INTO local_approvals(spare, site, approval, known_day, "
            "note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (spare, site, approval, known_day, note, _now()),
        )


def save_snapshot(conn, as_of_day: int, payload: dict, note: str = "") -> int:
    """留存某时点的结论快照，供日后解释“原建议是什么”。"""
    with conn:
        cur = conn.execute(
            "INSERT INTO advice_snapshots(as_of_day, created_at, note, payload) "
            "VALUES (?, ?, ?, ?)",
            (as_of_day, _now(), note, json.dumps(payload, ensure_ascii=False)),
        )
        return cur.lastrowid


def list_snapshots(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, as_of_day, created_at, note FROM advice_snapshots "
        "ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def open_chain_audit(conn, as_of_day: int) -> list[dict]:
    """供主管核对：当前未闭环链的确认状态与证据清单。"""
    view = build_view(conn, as_of_day)
    out = []
    for c in build_chains(view):
        if c.is_open:
            out.append({
                "chain": c.chain_id,
                "spare": c.spare,
                "site": c.site,
                "returned_part": c.returned_part,
                "term": c.term,
                "state": c.state,
                "confirmed_locked": c.confirmed_locked,
                "part_lot": c.part_lot,
                "spare_approval": c.spare_approval,
                "reasons": c.eligibility_reasons,
                "events": [
                    {"event": e.event, "action": e.action, "day": e.day,
                     "origin": e.origin}
                    for e in c.events
                ],
            })
    return out
