"""停用风险分析（以使用小时为生存时间）。

口径声明（维修主管可据此核对每个数字的证据等级）
==================================================

1. **不使用送修顺序构造故障时间**。故障只承认检查结论，且结论必须在
   分析时点之前“可知”（``known_day <= as_of``）。
2. 故障属于**区间删失**：真实故障发生在上一次 clear 检查与本次 failed
   检查之间。事件时间取 failed 检查时刻的折算小时（区间上沿），同时
   保留区间下沿，不给单点伪精度。
3. 右删失分三档：仍在役（in_service）、计划拆换（planned）、换站带走
   （transfer），各自的删失时间是当时累计使用小时，不与故障混算。
4. 已停用但在时点前拿不到故障证据的件进“不能归属”桶，不计入故障，
   也不静默丢弃；批号空白且无补录凭证的件同样标注不能归属。
5. 比较供应商时先按批次、站点分层，再用合并站点暴露权重做直接标化，
   消除“检得勤的站点占比高”与用量差异；同时并列展示朴素送修计数，
   让偏差本身可见。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .clock import KnowledgeView

# 状态桶（中文标签同时供网页与 CLI 使用）
STATUS_LABELS = {
    "interval_failure": "检查间隔内确认的故障",
    "in_service": "仍在役（右删失）",
    "planned": "计划拆换（右删失）",
    "transfer": "换站带走、观察中断（右删失）",
    "unattributed": "不能归属（无故障证据或批号未明）",
}


@dataclass
class PartRecord:
    part: str
    vendor: str
    site: str
    lot: Optional[str]
    lot_source: str
    exposure_hours: float
    status: str
    n_checks: int
    check_days: list[int]
    mean_check_interval: Optional[float]
    failure_inspection_day: Optional[int] = None
    failure_known_day: Optional[int] = None
    failure_late: bool = False
    event_hours_upper: Optional[float] = None
    event_hours_lower: Optional[float] = None
    censor_hours: Optional[float] = None
    observed_to: Optional[int] = None
    ending: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    @property
    def is_event(self) -> bool:
        return self.status == "interval_failure"


@dataclass
class RateRow:
    key: tuple
    label: str
    parts: int
    events: int
    exposure_hours: float
    rate_per_1000h: float
    ci_low: float
    ci_high: float

    @property
    def rate_text(self) -> str:
        return f"{self.rate_per_1000h:.3f}"


def _poisson_ci(events: int, exposure: float) -> tuple[float, float]:
    """事件率的 95% 置信区间（每小时）。

    有事件用正态近似；零事件用泊松精确上沿 3.69/exposure（即
    0.5·χ²(0.975, 2)），下沿为 0。
    """
    if exposure <= 0:
        return 0.0, float("inf")
    if events == 0:
        return 0.0, 3.69 / exposure
    rate = events / exposure
    half = 1.96 * math.sqrt(events) / exposure
    return max(0.0, rate - half), rate + half


def classify_parts(view: KnowledgeView) -> list[PartRecord]:
    """把知识视图转成逐件生存记录。"""
    records: list[PartRecord] = []
    checks_by_part: dict[str, list] = {}
    for c in view.checks:
        checks_by_part.setdefault(c.part, []).append(c)
    for rows in checks_by_part.values():
        rows.sort(key=lambda c: c.inspection_day)

    for part, iv in sorted(view.intervals.items(), key=lambda kv: kv[0]):
        attr = view.lots[part]
        rows = checks_by_part.get(part, [])
        n_checks = len(rows)
        intervals = [
            rows[i].inspection_day - rows[i - 1].inspection_day
            for i in range(1, len(rows))
        ]
        mean_gap = (sum(intervals) / len(intervals)) if intervals else None
        failed = next((c for c in rows if c.result == "failed"), None)
        base = dict(
            part=part, vendor=iv.vendor, site=iv.site, lot=attr.lot,
            lot_source=attr.source, n_checks=n_checks,
            check_days=[c.inspection_day for c in rows],
            mean_check_interval=mean_gap,
            observed_to=iv.observed_to, ending=iv.ending,
        )
        notes: list[str] = []
        if attr.source == "unattributed":
            notes.append("批号空白且补录凭证尚未到达，风险不能归属到批次")
        elif attr.source == "correction":
            notes.append(
                f"批号由第 {attr.known_day} 天到达的凭证"
                f"（{attr.evidence}）补录为 {attr.lot}"
            )

        if failed is not None:
            upper = iv.usage_at(failed.inspection_day)
            prior_clear = [
                c for c in rows
                if c.result == "clear" and c.inspection_day < failed.inspection_day
            ]
            lower = (
                iv.usage_at(prior_clear[-1].inspection_day)
                if prior_clear else 0.0
            )
            rec = PartRecord(
                **base,
                exposure_hours=upper,
                status="interval_failure",
                failure_inspection_day=failed.inspection_day,
                failure_known_day=failed.known_day,
                failure_late=failed.is_late,
                event_hours_upper=upper,
                event_hours_lower=lower,
                notes=notes + [
                    f"故障区间：第 {prior_clear[-1].inspection_day if prior_clear else iv.observed_from}"
                    f"–{failed.inspection_day} 天检查之间确认"
                    + ("（结论补交）" if failed.is_late else "")
                ],
            )
        else:
            censor_hours = iv.usage_at(min(view.as_of, iv.observed_to))
            if attr.source == "unattributed":
                # 批号未明的件不能并入任何批次的风险估计，优先于结束原因归类
                status = "unattributed"
                notes.append("批号归属未明，该观察不能计入任何供应商/批次分层")
            elif not iv.is_complete:
                status = "in_service"
                notes.append(
                    f"观察窗尚未结束（计划到第 {iv.observed_to} 天），"
                    "结束原因在当前时点不可知，按在役右删失处理"
                )
            elif iv.ending == "withdrawn":
                status = "unattributed"
                notes.append(
                    "已停用但时点前没有可知的故障检查结论，不能按送修日期推定故障"
                )
            elif iv.ending == "planned":
                status = "planned"
            elif iv.ending == "transfer":
                status = "transfer"
                notes.append("整机换站带走，后续观察不在本站台账单内")
            else:
                status = "in_service"
            rec = PartRecord(
                **base,
                exposure_hours=censor_hours,
                status=status,
                censor_hours=censor_hours,
                notes=notes,
            )
        records.append(rec)
    return records


def _stratify(records: list[PartRecord], key_fields: tuple[str, ...]):
    groups: dict[tuple, list[PartRecord]] = {}
    for r in records:
        key = tuple(getattr(r, f) for f in key_fields)
        groups.setdefault(key, []).append(r)
    return groups


def strata_rates(
    records: list[PartRecord],
    key_fields: tuple[str, ...] = ("lot",),
    *,
    include_unattributed: bool = True,
) -> list[RateRow]:
    """分层事件率（每 1000 使用小时）。

    不能归属的件默认单列（lot=None），绝不并入任何供应商批次。
    """
    out: list[RateRow] = []
    groups = _stratify(records, key_fields)
    for key in sorted(groups, key=lambda k: tuple("￿" if x is None else str(x) for x in k)):
        rows = groups[key]
        if not include_unattributed and any(r.lot is None for r in rows):
            continue
        events = sum(r.is_event for r in rows)
        exposure = sum(r.exposure_hours for r in rows)
        low, high = _poisson_ci(events, exposure)
        label = "/".join("未知" if x is None else str(x) for x in key)
        out.append(RateRow(
            key=key, label=label, parts=len(rows), events=events,
            exposure_hours=exposure,
            rate_per_1000h=1000 * events / exposure if exposure else 0.0,
            ci_low=1000 * low,
            ci_high=(1000 * high) if math.isfinite(high) else float("inf"),
        ))
    return out


def kaplan_meier(records: list[PartRecord]) -> list[dict]:
    """使用小时尺度的 Kaplan–Meier 阶梯。

    事件取区间上沿；同一点位的删失先于事件处理是常见保守口径，这里
    采用“先删失后事件”的标准 lifetable 顺序并在输出中同时给出该点的
    在险数，便于人工复核。
    """
    points: list[tuple[float, str]] = []
    for r in records:
        if r.is_event:
            points.append((r.event_hours_upper, "event"))
        elif r.censor_hours is not None:
            points.append((r.censor_hours, "censor"))
    points.sort(key=lambda p: (p[0], 0 if p[1] == "censor" else 1))
    n_at_risk = len(points)
    survival = 1.0
    table: list[dict] = []
    i = 0
    while i < len(points):
        t = points[i][0]
        d = 0
        c = 0
        while i < len(points) and points[i][0] == t:
            if points[i][1] == "event":
                d += 1
            else:
                c += 1
            i += 1
        n_start = n_at_risk
        n_at_risk -= c  # 先删失
        if d:
            survival *= 1 - d / n_at_risk if n_at_risk else survival
            n_at_risk -= d
        table.append({
            "hours": round(t, 2), "at_risk_start": n_start,
            "censored": c, "events": d, "survival": round(survival, 6),
        })
    return table


def nelson_aalen_hazard(records: list[PartRecord]) -> list[dict]:
    points: list[tuple[float, str]] = []
    for r in records:
        if r.is_event:
            points.append((r.event_hours_upper, "event"))
        elif r.censor_hours is not None:
            points.append((r.censor_hours, "censor"))
    points.sort(key=lambda p: (p[0], 0 if p[1] == "censor" else 1))
    n = len(points)
    cumulative = 0.0
    table: list[dict] = []
    i = 0
    while i < len(points):
        t = points[i][0]
        d = c = 0
        while i < len(points) and points[i][0] == t:
            if points[i][1] == "event":
                d += 1
            else:
                c += 1
            i += 1
        n -= c
        if d:
            cumulative += d / n if n else 0.0
            n -= d
        table.append({
            "hours": round(t, 2), "events": d, "censored": c,
            "cumulative_hazard": round(cumulative, 6),
        })
    return table


def standardized_vendor_rates(
    records: list[PartRecord], lot: str
) -> list[dict]:
    """同一批次内按站点暴露权重直接标化的供应商故障率对比。"""
    pool = [r for r in records if r.lot == lot]
    sites = sorted({r.site for r in pool})
    # 标准权重：各站点在该批次的合计暴露
    site_exposure = {
        s: sum(r.exposure_hours for r in pool if r.site == s) for s in sites
    }
    total = sum(site_exposure.values()) or 1.0
    weights = {s: site_exposure[s] / total for s in sites}
    out = []
    for vendor in sorted({r.vendor for r in pool}):
        per_site = {}
        crude_events = crude_exp = 0
        for s in sites:
            rows = [r for r in pool if r.vendor == vendor and r.site == s]
            exp = sum(r.exposure_hours for r in rows)
            ev = sum(r.is_event for r in rows)
            rate = ev / exp if exp else 0.0
            per_site[s] = {"events": ev, "exposure": exp, "rate": rate}
            crude_events += ev
            crude_exp += exp
        adjusted = sum(weights[s] * per_site[s]["rate"] for s in sites)
        out.append({
            "vendor": vendor,
            "crude_rate_per_1000h": 1000 * crude_events / crude_exp if crude_exp else 0.0,
            "adjusted_rate_per_1000h": 1000 * adjusted,
            "per_site": per_site,
        })
    return out


def inspection_coverage(view: KnowledgeView,
                        records: list[PartRecord]) -> list[dict]:
    """各站点检查覆盖与发现延迟：解释“检得勤看起来更差”。"""
    by_site: dict[str, list[PartRecord]] = {}
    for r in records:
        by_site.setdefault(r.site, []).append(r)
    out = []
    for site in sorted(by_site):
        rows = by_site[site]
        gaps = [r.mean_check_interval for r in rows if r.mean_check_interval]
        failures = [r for r in rows if r.is_event]
        delays = [
            (r.failure_inspection_day - r.check_days[-2])
            for r in failures if len(r.check_days) >= 2
        ]
        out.append({
            "site": site,
            "parts": len(rows),
            "mean_checks_per_part": round(
                sum(r.n_checks for r in rows) / len(rows), 2
            ),
            "mean_interval_days": round(sum(gaps) / len(gaps), 2) if gaps else None,
            "failures_found": len(failures),
            "mean_detection_window_days": round(
                sum(delays) / len(delays), 2
            ) if delays else None,
        })
    return out


def naive_repair_ranking(records: list[PartRecord]) -> list[dict]:
    """朴素“送修/停用计数”口径——仅用于暴露偏差，不用于结论。"""
    agg: dict[str, dict] = {}
    for r in records:
        bucket = agg.setdefault(r.vendor, {"vendor": r.vendor, "withdrawn": 0,
                                           "events": 0, "exposure": 0.0})
        if r.ending == "withdrawn":
            bucket["withdrawn"] += 1
        bucket["events"] += int(r.is_event)
        bucket["exposure"] += r.exposure_hours
    rows = list(agg.values())
    rows.sort(key=lambda x: x["withdrawn"], reverse=True)
    return rows


@dataclass
class DemandProjection:
    horizon_days: int
    usage_multiplier: float
    site: str
    lot: Optional[str]
    continuing_parts: int
    transferred_parts: int = 0
    future_exposure_hours: float = 0.0
    expected_failures: float = 0.0
    demand_p90: float = 0.0
    planned_removals: int = 0
    total_spares_needed: float = 0.0


def project_demand(
    records: list[PartRecord],
    view: KnowledgeView,
    horizon_days: int,
    usage_multiplier: float,
) -> list[DemandProjection]:
    """不同用量假设下的备件需求（按站点 × 批次）。

    - 只有时点上仍在役的件贡献未来暴露；计划拆换按确定性需求另计；
      换站带走的件不再占用原站点需求。
    - 事件率取同站点同批次的分层率（无该层数据时回退到批次合计率）。
    - p90 用泊松正态近似 λ + 1.282√λ。
    """
    lot_rates = {
        row.key[0]: (row.events / row.exposure_hours if row.exposure_hours else 0.0)
        for row in strata_rates(records, ("lot",))
    }
    site_lot_rates: dict[tuple, float] = {}
    for row in strata_rates(records, ("site", "lot")):
        site_lot_rates[(row.key[0], row.key[1])] = (
            row.events / row.exposure_hours if row.exposure_hours else 0.0
        )

    buckets: dict[tuple, dict] = {}
    for r in records:
        key = (r.site, r.lot)
        b = buckets.setdefault(key, {
            "future_exposure": 0.0, "continuing": 0, "planned": 0,
            "transferred": 0,
        })
        iv = view.intervals[r.part]
        if r.status in {"in_service", "transfer"}:
            # 换站件离开本站但仍在机队服役，继续承担未来暴露（单列数量）
            daily_rate = iv.usage_hours / max(1, iv.observed_to - iv.observed_from)
            b["future_exposure"] += daily_rate * horizon_days * usage_multiplier
            b["continuing"] += 1
            if r.status == "transfer":
                b["transferred"] += 1
        elif r.status == "planned":
            if r.observed_to is not None and r.observed_to <= view.as_of + horizon_days:
                b["planned"] += 1

    out: list[DemandProjection] = []
    for (site, lot), b in sorted(buckets.items(), key=lambda kv: tuple("￿" if x is None else str(x) for x in kv)):
        rate = site_lot_rates.get((site, lot))
        if rate is None:
            rate = lot_rates.get(lot, 0.0)
        lam = rate * b["future_exposure"]
        p90 = lam + 1.282 * math.sqrt(lam)
        out.append(DemandProjection(
            horizon_days=horizon_days,
            usage_multiplier=usage_multiplier,
            site=site, lot=lot,
            continuing_parts=b["continuing"],
            transferred_parts=b["transferred"],
            future_exposure_hours=round(b["future_exposure"], 2),
            expected_failures=round(lam, 3),
            demand_p90=round(p90 + b["planned"], 3),
            planned_removals=b["planned"],
            total_spares_needed=round(lam + b["planned"], 3),
        ))
    return out


def status_counts(records: list[PartRecord]) -> dict[str, int]:
    counts = {k: 0 for k in STATUS_LABELS}
    for r in records:
        counts[r.status] += 1
    return counts
