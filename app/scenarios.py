"""续约方案建模与并排比较。

一个方案 = 一组保修条款（可停用、可改额度/交回天数/单价）+ 一个用量
假设（展望天数 × 用量倍数）。预测只回答三件事：

1. 按风险模块的分层事件率，各站点/批次预计多少停用、需要多少备件；
2. 其中多少落在条款覆盖内、先行额度够不够、预计多少保修额度兑现；
3. 不能覆盖、预计逾期与额度超限的部分形成多少按成本采购的费用敞口。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from . import risk
from .clock import KnowledgeView

TERM_FIELDS = (
    "term", "vendor", "lot", "supported_site", "return_days",
    "unit_cost", "advance_limit",
)


@dataclass
class ScenarioRow:
    site: str
    lot: Optional[str]
    vendor_mix: str
    expected_failures: float
    planned_removals: int
    spares_needed: float
    covering_terms: list[str]
    covered_failures: float
    uncovered_failures: float
    expected_cost: float


@dataclass
class ScenarioResult:
    name: str
    description: str
    horizon_days: int
    usage_multiplier: float
    rows: list[ScenarioRow]
    totals: dict
    limit_flags: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_scenario(
    conn,
    name: str,
    horizon_days: int,
    usage_multiplier: float,
    terms: list[dict],
    *,
    description: str = "",
    is_baseline: bool = False,
) -> None:
    """写入或整体替换一个方案（同名覆盖）。"""
    if horizon_days <= 0 or usage_multiplier <= 0:
        raise ValueError("展望天数与用量倍数必须为正数")
    if not name.strip():
        raise ValueError("方案名称不能为空")
    with conn:
        conn.execute("DELETE FROM scenario_terms WHERE scenario = ?", (name,))
        conn.execute("DELETE FROM scenarios WHERE name = ?", (name,))
        conn.execute(
            "INSERT INTO scenarios(name, description, horizon_days, "
            "usage_multiplier, created_at, is_baseline) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, description, horizon_days, usage_multiplier, _now(),
             int(is_baseline)),
        )
        for t in terms:
            conn.execute(
                "INSERT INTO scenario_terms(scenario, term, vendor, lot, "
                "supported_site, return_days, unit_cost, advance_limit, enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, t["term"], t["vendor"], t["lot"], t["supported_site"],
                 int(t["return_days"]), float(t["unit_cost"]),
                 int(t["advance_limit"]), int(t.get("enabled", 1))),
            )


def baseline_from_sources(conn) -> str:
    """用台账内现行条款建立/刷新“基线（现行条款）”方案。"""
    terms = [dict(r) for r in conn.execute(
        "SELECT term, vendor, lot, supported_site, return_days, unit_cost, "
        "advance_limit FROM warranty_terms ORDER BY term"
    ).fetchall()]
    name = "基线（现行条款）"
    save_scenario(
        conn, name, horizon_days=28, usage_multiplier=1.0, terms=terms,
        description="按台账现行 warranty_terms 原样建模", is_baseline=True,
    )
    return name


def list_scenarios(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM scenarios ORDER BY is_baseline DESC, name"
    ).fetchall()]


def _load_terms(conn, name: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM scenario_terms WHERE scenario = ? AND enabled = 1 "
        "ORDER BY term",
        (name,),
    ).fetchall()]


def evaluate(
    conn,
    view: KnowledgeView,
    records: list[risk.PartRecord],
    name: str,
) -> ScenarioResult:
    header = conn.execute(
        "SELECT * FROM scenarios WHERE name = ?", (name,)
    ).fetchone()
    if header is None:
        raise KeyError(f"方案不存在: {name}")
    terms = _load_terms(conn, name)
    horizon = header["horizon_days"]
    multiplier = header["usage_multiplier"]
    projections = risk.project_demand(records, view, horizon, multiplier)

    # 站点×批次的供应商构成（用于覆盖匹配时定位条款）
    vendor_mix: dict[tuple, dict[str, float]] = {}
    for r in records:
        vendor_mix.setdefault((r.site, r.lot), {}).setdefault(r.vendor, 0.0)
        vendor_mix[(r.site, r.lot)][r.vendor] += r.exposure_hours

    rows: list[ScenarioRow] = []
    totals = {
        "expected_failures": 0.0, "planned_removals": 0,
        "spares_needed": 0.0, "covered_failures": 0.0,
        "uncovered_failures": 0.0, "expected_cost": 0.0,
        "advance_capacity": sum(int(t["advance_limit"]) for t in terms),
    }
    for p in projections:
        key = (p.site, p.lot)
        mix = vendor_mix.get(key, {})
        mix_text = ", ".join(
            f"{v} {round(h / sum(mix.values()) * 100)}%"
            for v, h in sorted(mix.items())
        ) if mix else "—"
        covering = [
            t for t in terms
            if t["supported_site"] == p.site
            and (p.lot is not None and t["lot"] == p.lot)
        ]
        # 按暴露份额把预计故障分摊到覆盖供应商，未覆盖部分计入敞口
        covered = 0.0
        cost = 0.0
        for t in covering:
            share = (
                mix.get(t["vendor"], 0.0) / sum(mix.values())
                if sum(mix.values()) else 0.0
            )
            covered += p.expected_failures * share
        uncovered = max(0.0, p.expected_failures - covered)
        # 未覆盖故障与计划拆换按该站点可查的最低条款单价估算成本；
        # 覆盖部分按保修兑现，不计采购成本。无任何条款的站点单价记 0 待询。
        available_costs = [
            float(t["unit_cost"]) for t in terms
            if t["supported_site"] == p.site
        ]
        unit = min(available_costs) if available_costs else 0.0
        cost = (uncovered + p.planned_removals) * unit
        rows.append(ScenarioRow(
            site=p.site, lot=p.lot, vendor_mix=mix_text,
            expected_failures=p.expected_failures,
            planned_removals=p.planned_removals,
            spares_needed=p.total_spares_needed,
            covering_terms=[t["term"] for t in covering],
            covered_failures=round(covered, 3),
            uncovered_failures=round(uncovered, 3),
            expected_cost=round(cost, 2),
        ))
        totals["expected_failures"] += p.expected_failures
        totals["planned_removals"] += p.planned_removals
        totals["spares_needed"] += p.total_spares_needed
        totals["covered_failures"] += covered
        totals["uncovered_failures"] += uncovered
        totals["expected_cost"] += cost

    # 额度充足性：把同条款下各覆盖行的并发需求峰值与 advance_limit 比较
    limit_flags: list[str] = []
    for t in terms:
        peak = sum(
            row.expected_failures
            for row in rows
            if t["term"] in row.covering_terms
        )
        if peak > t["advance_limit"]:
            limit_flags.append(
                f"条款 {t['term']} 预计并发未闭环替换 {peak:.2f} "
                f"> 先行额度 {t['advance_limit']}"
            )
    for k in ("expected_failures", "spares_needed", "covered_failures",
              "uncovered_failures", "expected_cost"):
        totals[k] = round(totals[k], 3)

    return ScenarioResult(
        name=name, description=header["description"],
        horizon_days=horizon, usage_multiplier=multiplier,
        rows=rows, totals=totals, limit_flags=limit_flags,
    )


def compare(conn, view, records, names: list[str]) -> list[ScenarioResult]:
    return [evaluate(conn, view, records, n) for n in names]
