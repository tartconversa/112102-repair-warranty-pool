"""两个时点之间的证据与建议变化解释。

回答“使用者用后来补交的拆检或签收记录，解释原建议为何变化”：
分别构造 T1、T2 的知识视图并重新跑同一套分析，差异必须能追溯到具体
的新到达证据（检查结论、补录凭证、互换许可、交接事件），而不是算法
黑箱。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import clock, risk, scenarios, warranty


@dataclass
class EvidenceArrival:
    day: int
    kind: str
    label: str
    detail: str


@dataclass
class AsOfDiff:
    earlier: int
    later: int
    new_evidence: list[EvidenceArrival] = field(default_factory=list)
    part_changes: list[dict] = field(default_factory=list)
    lot_changes: list[dict] = field(default_factory=list)
    chain_changes: list[dict] = field(default_factory=list)
    spare_changes: list[dict] = field(default_factory=list)
    scenario_deltas: list[dict] = field(default_factory=list)
    actions_now: list[str] = field(default_factory=list)


def _new_evidence(conn, t1: int, t2: int) -> list[EvidenceArrival]:
    out: list[EvidenceArrival] = []
    queries = [
        ("SELECT known_day AS d, part AS k, result, inspection_day, known_day "
         "FROM checks WHERE known_day > ? AND known_day <= ?",
         "检查结论",
         lambda r: f"{r['k']} 第 {r['inspection_day']} 天检查结论"
                   f" {r['result']}（第 {r['known_day']} 天可知）"),
        ("SELECT known_day AS d, part AS k, lot, evidence, known_day "
         "FROM lot_corrections WHERE known_day > ? AND known_day <= ?",
         "实物批号补录",
         lambda r: f"{r['k']} 批号补录为 {r['lot']}（{r['evidence']}）"),
        ("SELECT known_day AS d, spare || '@' || site AS k, approval, known_day "
         "FROM interchange WHERE known_day > ? AND known_day <= ?",
         "互换许可",
         lambda r: f"{r['k']} 许可状态 {r['approval']}"),
        ("SELECT day AS d, event AS k, action, spare, site, returned_part, day "
         "FROM replacement_events WHERE day > ? AND day <= ?",
         "替换交接",
         lambda r: f"{r['k']}：{r['spare']} 在 {r['site']} 站替换 "
                   f"{r['returned_part']}，动作 {r['action']}"),
        ("SELECT day AS d, event AS k, action, spare, site, returned_part, day "
         "FROM local_events WHERE day > ? AND day <= ?",
         "本地登记交接",
         lambda r: f"{r['k']}：{r['spare']} 在 {r['site']} 站替换 "
                   f"{r['returned_part']}，动作 {r['action']}"),
        ("SELECT known_day AS d, spare || '@' || site AS k, approval, known_day "
         "FROM local_approvals WHERE known_day > ? AND known_day <= ?",
         "复验放行登记",
         lambda r: f"{r['k']} 复验状态 {r['approval']}"),
    ]
    for sql, kind, render in queries:
        for r in conn.execute(sql, (t1, t2)).fetchall():
            out.append(EvidenceArrival(
                day=r["d"], kind=kind, label=render(dict(r)),
                detail=str(dict(r)),
            ))
    out.sort(key=lambda e: (e.day, e.kind))
    return out


def diff_as_of(conn, t1: int, t2: int,
               scenario_names: list[str] | None = None) -> AsOfDiff:
    if t2 < t1:
        t1, t2 = t2, t1
    v1 = clock.build_view(conn, t1)
    v2 = clock.build_view(conn, t2)
    rec1 = {r.part: r for r in risk.classify_parts(v1)}
    rec2 = {r.part: r for r in risk.classify_parts(v2)}
    diff = AsOfDiff(earlier=t1, later=t2,
                    new_evidence=_new_evidence(conn, t1, t2))

    for part in sorted(set(rec1) | set(rec2)):
        a, b = rec1.get(part), rec2.get(part)
        if a is None and b is not None:
            diff.part_changes.append({
                "part": part, "change": "新观察窗进入台账",
                "before": None, "after": risk.STATUS_LABELS[b.status],
            })
            continue
        if b is None:
            continue
        changes = []
        if a.status != b.status:
            changes.append(
                f"状态 {risk.STATUS_LABELS[a.status]} → "
                f"{risk.STATUS_LABELS[b.status]}"
            )
        if a.lot != b.lot:
            changes.append(f"批次归属 {a.lot or '未知'} → {b.lot or '未知'}")
        if not a.is_event and b.is_event:
            changes.append(
                f"新增故障证据（第 {b.failure_inspection_day} 天检查确认，"
                f"第 {b.failure_known_day} 天可知"
                + ("，属补交结论" if b.failure_late else "") + "）"
            )
        if changes:
            diff.part_changes.append({
                "part": part,
                "change": "；".join(changes),
                "before": risk.STATUS_LABELS[a.status],
                "after": risk.STATUS_LABELS[b.status],
            })
        if a.lot != b.lot:
            diff.lot_changes.append({
                "part": part, "before": a.lot, "after": b.lot,
                "lot_source": b.lot_source,
            })

    c1 = {(c.spare, c.site, c.returned_part, c.term): c
          for c in warranty.build_chains(v1)}
    c2 = {(c.spare, c.site, c.returned_part, c.term): c
          for c in warranty.build_chains(v2)}
    for key in sorted(set(c1) | set(c2)):
        a, b = c1.get(key), c2.get(key)
        if a is None:
            diff.chain_changes.append({
                "chain": b.chain_id, "change": f"新增替换链，状态 {b.state}",
                "eligibility": warranty.ELIGIBILITY_LABELS[b.eligibility],
            })
        elif b is not None and (a.state != b.state or a.eligibility != b.eligibility):
            state_text = (
                f"状态 {warranty.CHAIN_STATE_LABELS.get(a.state, a.state)} "
                f"→ {warranty.CHAIN_STATE_LABELS.get(b.state, b.state)}"
                if a.state != b.state else "链状态未变"
            )
            diff.chain_changes.append({
                "chain": b.chain_id,
                "change": state_text,
                "eligibility": (
                    f"{warranty.ELIGIBILITY_LABELS[a.eligibility]} → "
                    f"{warranty.ELIGIBILITY_LABELS[b.eligibility]}"
                ),
            })

    s1 = {(p.spare, p.site): p for p in warranty.spare_positions(v1, list(c1.values()))}
    s2 = {(p.spare, p.site): p for p in warranty.spare_positions(v2, list(c2.values()))}
    for key in sorted(set(s1) | set(s2)):
        a, b = s1.get(key), s2.get(key)
        if a is None and b is not None:
            diff.spare_changes.append({
                "spare": f"{b.spare}@{b.site}",
                "change": f"许可进入台账（{b.approval}），"
                          f"可兑现: {'是' if b.deliverable else '否'}",
            })
        elif a is not None and b is not None and (
            a.deliverable != b.deliverable or a.approval != b.approval
        ):
            diff.spare_changes.append({
                "spare": f"{b.spare}@{b.site}",
                "change": f"可兑现 {'是' if a.deliverable else '否'} → "
                          f"{'是' if b.deliverable else '否'}；"
                          f"阻碍: {'; '.join(b.blockers) or '无'}",
            })

    names = scenario_names or [s["name"] for s in scenarios.list_scenarios(conn)]
    for name in names:
        try:
            r1 = scenarios.evaluate(conn, v1, rec1 and list(rec1.values()), name)
            r2 = scenarios.evaluate(conn, v2, list(rec2.values()), name)
        except KeyError:
            continue
        diff.scenario_deltas.append({
            "scenario": name,
            "expected_failures": round(
                r2.totals["expected_failures"] - r1.totals["expected_failures"], 3),
            "covered": round(
                r2.totals["covered_failures"] - r1.totals["covered_failures"], 3),
            "cost": round(
                r2.totals["expected_cost"] - r1.totals["expected_cost"], 2),
        })

    # 目前仍可采取的措施（基于 T2 的最新状态）
    latest_chains = list(c2.values())
    for c in latest_chains:
        if c.is_open and c.eligibility == "pending":
            diff.actions_now.append(
                f"链 {c.chain_id}（{c.spare}/{c.site}）尚未闭环：核对 "
                f"{c.returned_part} 实物批号并安排交回，避免逾期按成本采购"
            )
    for p in warranty.spare_positions(v2, latest_chains):
        if not p.deliverable:
            diff.actions_now.append(
                f"备件 {p.spare}@{p.site} 当前不可兑现：{'; '.join(p.blockers)}"
            )
    open_unconfirmed = warranty.revalidation_notes(latest_chains)
    for n in open_unconfirmed:
        if not n["still_supported"]:
            diff.actions_now.append(
                f"未确认计划（{n['spare']}/{n['site']}，件 "
                f"{n['returned_part']}）按最新证据已不获支持："
                f"{'; '.join(n['reasons'])}，应撤销或改走采购"
            )
    return diff
