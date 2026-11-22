"""时点知识视图（as-of clock）。

分析的每一步都只能使用“在第 T 天结束时已经可知”的记录：

- 检查结论以 ``known_day`` 为准而非 ``inspection_day``：补交的拆检结论
  在补交日之前不可见，避免结论穿越。
- 实物批号以当时已到达的补录凭证为准；凭证未到达前保持“不能归属”。
- 观察窗在其 ``observed_to`` 当天才成为完整事实。
- 备件互换许可以最新的已知许可为准，本地登记的复验放行可覆盖
  ``inspection_required``。
- 已确认调出（dispatched/return_confirmed）的事实不会因刷新消失。

时间尺度另有“使用小时”：窗内只有累计小时，因此按日历日线性折算
（合成台账下的标准做法），风险分析一律以小时为生存时间。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Interval:
    part: str
    vendor: str
    raw_lot: Optional[str]
    site: str
    observed_from: int
    observed_to: int
    usage_hours: float
    ending: str
    is_complete: bool = True

    def usage_at(self, day: int) -> float:
        """把窗内累计使用小时线性折算到第 ``day`` 天。"""
        if day <= self.observed_from:
            return 0.0
        if day >= self.observed_to:
            return self.usage_hours
        span = self.observed_to - self.observed_from
        return self.usage_hours * (day - self.observed_from) / span


@dataclass(frozen=True)
class Check:
    part: str
    inspection_day: int
    result: str
    known_day: int

    @property
    def is_late(self) -> bool:
        return self.known_day > self.inspection_day


@dataclass(frozen=True)
class LotAttribution:
    part: str
    lot: Optional[str]
    source: str  # "raw" | "correction" | "unattributed"
    known_day: Optional[int]
    evidence: Optional[str]


@dataclass(frozen=True)
class InterchangeApproval:
    spare: str
    site: str
    approval: str
    known_day: int
    origin: str  # "source" | "local"


@dataclass(frozen=True)
class ReplacementEvent:
    event: str
    spare: str
    site: str
    returned_part: str
    term: Optional[str]
    action: str
    day: int
    origin: str  # "source" | "local"


@dataclass
class KnowledgeView:
    as_of: int
    intervals: dict[str, Interval]
    checks: list[Check]
    lots: dict[str, LotAttribution]
    approvals: dict[tuple[str, str], InterchangeApproval]
    events: list[ReplacementEvent]
    terms: list[dict]
    corrections: list[dict]

    def checks_for(self, part: str) -> list[Check]:
        return sorted(
            (c for c in self.checks if c.part == part),
            key=lambda c: c.inspection_day,
        )

    def latest_check(self, part: str) -> Optional[Check]:
        rows = self.checks_for(part)
        return rows[-1] if rows else None

    def events_for_chain(self, spare: str, site: str, returned_part: str,
                         term: Optional[str]) -> list[ReplacementEvent]:
        return sorted(
            (
                e for e in self.events
                if e.spare == spare and e.site == site
                and e.returned_part == returned_part
                and (e.term or None) == (term or None)
            ),
            key=lambda e: (e.day, e.event),
        )


def _rows(conn, sql: str, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def build_view(conn, as_of: int) -> KnowledgeView:
    """构造截至第 ``as_of`` 天（含当天）的知识视图。"""
    # 观察窗自 observed_from 起逐步可见；未到 observed_to 的窗按截断处理，
    # 结束原因在窗完整前不可知（统一视为仍在役的右删失观察）
    interval_rows = _rows(
        conn,
        "SELECT part, vendor, lot, site, observed_from, observed_to, "
        "usage_hours, ending FROM service_intervals "
        "WHERE observed_from <= ? ORDER BY part",
        (as_of,),
    )
    intervals = {}
    for r in interval_rows:
        complete = r["observed_to"] <= as_of
        intervals[r["part"]] = Interval(
            part=r["part"], vendor=r["vendor"], raw_lot=r["lot"],
            site=r["site"], observed_from=r["observed_from"],
            observed_to=r["observed_to"], usage_hours=float(r["usage_hours"]),
            ending=r["ending"] if complete else "in_service",
            is_complete=complete,
        )

    check_rows = _rows(
        conn,
        "SELECT part, inspection_day, result, known_day FROM checks "
        "WHERE known_day <= ? ORDER BY part, inspection_day",
        (as_of,),
    )
    checks = [Check(**r) for r in check_rows]

    correction_rows = _rows(
        conn,
        "SELECT part, lot, known_day, evidence FROM lot_corrections "
        "WHERE known_day <= ? ORDER BY part, known_day",
        (as_of,),
    )
    corrections_by_part: dict[str, dict] = {}
    for r in correction_rows:
        corrections_by_part[r["part"]] = r  # 后者覆盖前者
    lots: dict[str, LotAttribution] = {}
    for part, iv in intervals.items():
        corr = corrections_by_part.get(part)
        if iv.raw_lot:
            lots[part] = LotAttribution(
                part, iv.raw_lot, "raw", iv.observed_from, None
            )
        elif corr is not None:
            lots[part] = LotAttribution(
                part, corr["lot"], "correction", corr["known_day"],
                corr["evidence"],
            )
        else:
            lots[part] = LotAttribution(part, None, "unattributed", None, None)

    approval_rows = _rows(
        conn,
        "SELECT spare, site, approval, known_day, 'source' AS origin "
        "FROM interchange WHERE known_day <= ?",
        (as_of,),
    )
    approval_rows += _rows(
        conn,
        "SELECT spare, site, approval, known_day, 'local' AS origin "
        "FROM local_approvals WHERE known_day <= ?",
        (as_of,),
    )
    approvals: dict[tuple[str, str], InterchangeApproval] = {}
    for r in sorted(approval_rows, key=lambda x: (x["known_day"], x["origin"])):
        key = (r["spare"], r["site"])
        current = approvals.get(key)
        if current is None or r["known_day"] >= current.known_day:
            approvals[key] = InterchangeApproval(
                spare=r["spare"], site=r["site"], approval=r["approval"],
                known_day=r["known_day"], origin=r["origin"],
            )

    event_rows = _rows(
        conn,
        "SELECT event, spare, site, returned_part, term, action, day, "
        "'source' AS origin FROM replacement_events WHERE day <= ?",
        (as_of,),
    )
    event_rows += _rows(
        conn,
        "SELECT event, spare, site, returned_part, term, action, day, "
        "'local' AS origin FROM local_events WHERE day <= ?",
        (as_of,),
    )
    events = [
        ReplacementEvent(
            event=r["event"], spare=r["spare"], site=r["site"],
            returned_part=r["returned_part"], term=r["term"],
            action=r["action"], day=r["day"], origin=r["origin"],
        )
        for r in sorted(event_rows, key=lambda x: (x["day"], x["event"]))
    ]

    terms = _rows(
        conn,
        "SELECT term, vendor, lot, supported_site, return_days, unit_cost, "
        "advance_limit FROM warranty_terms ORDER BY term"
    )
    return KnowledgeView(
        as_of=as_of,
        intervals=intervals,
        checks=checks,
        lots=lots,
        approvals=approvals,
        events=events,
        terms=terms,
        corrections=correction_rows,
    )


def knowledge_milestones(conn) -> list[int]:
    """所有可能改变结论的日期（供两个时点对比使用）。"""
    days: set[int] = set()
    for sql in (
        "SELECT known_day AS d FROM checks",
        "SELECT known_day AS d FROM lot_corrections",
        "SELECT known_day AS d FROM interchange",
        "SELECT day AS d FROM replacement_events",
        "SELECT day AS d FROM local_events",
        "SELECT known_day AS d FROM local_approvals",
        "SELECT observed_to AS d FROM service_intervals",
    ):
        days.update(r["d"] for r in conn.execute(sql).fetchall())
    return sorted(days)
