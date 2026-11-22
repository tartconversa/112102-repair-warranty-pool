"""保修资格、先行替换额度、共池备件与费用敞口。

业务规则（与来源说明逐条对应）
==============================

* 供应商只对“能交回并确认属于覆盖批次”的件提供保修额度：资格同时
  要求供应商、批次、站点三项与条款一致，且原件已确认交回并有可知的
  实物归属（原始批号或已到达的补录凭证）。
* ``return_days`` 从**发出替换件**（dispatched）起算；逾期交回的一份
  先行替换按成本采购结算，不再产生保修额度。
* ``advance_limit`` 是条款下**尚未闭环**的替换额度：已确认交回的链
  释放额度，处于申请/预留/待复验等中间态的链占用额度。
* 共池备件同一实物可在多站点使用；``inspection_required`` 的站点许
  可不算放行，待复验中的件不能计入可兑现数量；被未闭环链占用的实物
  同样不可再分配。
* 已确认调出的事实不会因分析刷新自动取回；未确认的计划（requested/
  reserved）每次按最新证据重新核对，资格不符会被显式标记。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .clock import KnowledgeView, ReplacementEvent

# 状态机：链上出现过的最强动作
CHAIN_ORDER = [
    "requested", "reserved", "dispatched", "awaiting_inspection",
    "return_confirmed", "receipt_recorded",
]
CHAIN_STATE_LABELS = {
    "requested": "已申请（未确认，刷新时重核）",
    "reserved": "已预留（未确认，刷新时重核）",
    "dispatched": "已调出（确认，不自动取回）",
    "awaiting_inspection": "待复验",
    "return_confirmed": "原件交回已确认",
    "receipt_recorded": "签收回执已登记",
}
OPEN_STATES = {"requested", "reserved", "dispatched", "awaiting_inspection"}

ELIGIBILITY_LABELS = {
    "eligible": "符合保修覆盖",
    "overdue_cost": "逾期交回，按成本采购结算",
    "lot_uncovered": "批次/供应商不在条款覆盖内",
    "site_unsupported": "站点不在条款支持范围",
    "lot_unattributed": "实物归属未明，暂不能主张额度",
    "pending": "链条未闭环，结论待定",
}


@dataclass
class ReplacementChain:
    chain_id: str
    spare: str
    site: str
    returned_part: str
    term: Optional[str]
    events: list[ReplacementEvent]
    state: str
    dispatch_day: Optional[int]
    return_day: Optional[int]
    days_used: Optional[int]
    eligibility: str
    eligibility_reasons: list[str] = field(default_factory=list)
    term_row: Optional[dict] = None
    part_lot: Optional[str] = None
    part_vendor: Optional[str] = None
    spare_approval: Optional[str] = None
    confirmed_locked: bool = False

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def cost_exposure(self) -> float:
        """当前时点已锁定或可预见的成本敞口。"""
        if self.term_row is None:
            return 0.0
        cost = float(self.term_row["unit_cost"])
        if self.eligibility == "overdue_cost":
            return cost
        if self.is_open and self.dispatch_day is not None:
            # 已调出但未闭环：存在逾期风险，按概率前的全额提示敞口
            return cost
        return 0.0


def build_chains(view: KnowledgeView) -> list[ReplacementChain]:
    keys = sorted({
        (e.spare, e.site, e.returned_part, e.term) for e in view.events
    })
    chains: list[ReplacementChain] = []

    def c_is_open(state: str) -> bool:
        return state in OPEN_STATES

    for idx, (spare, site, part, term) in enumerate(keys, start=1):
        evs = view.events_for_chain(spare, site, part, term)
        strongest = max(
            (e.action for e in evs),
            key=lambda a: CHAIN_ORDER.index(a) if a in CHAIN_ORDER else -1,
        )
        dispatch = next((e for e in evs if e.action == "dispatched"), None)
        returned = next(
            (e for e in evs if e.action in ("return_confirmed", "receipt_recorded")),
            None,
        )
        term_row = next((t for t in view.terms if t["term"] == term), None)
        attr = view.lots.get(part)
        iv = view.intervals.get(part)
        approval = view.approvals.get((spare, site))
        days_used = None
        if dispatch and returned:
            days_used = returned.day - dispatch.day

        reasons: list[str] = []
        eligibility = "pending"
        if term_row is None:
            eligibility = "lot_uncovered"
            reasons.append(f"条款 {term} 在当前台账中不存在")
        else:
            part_vendor = iv.vendor if iv else None
            part_lot = attr.lot if attr else None
            # 覆盖匹配：未闭环链同样立即核对，未确认计划据此重核
            mismatch = False
            if part_vendor and part_vendor != term_row["vendor"]:
                eligibility = "lot_uncovered"
                mismatch = True
                reasons.append(
                    f"原件供应商 {part_vendor} 与条款供应商 "
                    f"{term_row['vendor']} 不一致"
                )
            if part_lot is None:
                eligibility = "lot_unattributed"
                reasons.append("原件批号未明，等待实物归属凭证；额度暂不能主张")
            elif part_lot != term_row["lot"]:
                eligibility = "lot_uncovered"
                mismatch = True
                reasons.append(
                    f"原件批次 {part_lot} 不在条款 {term_row['term']} "
                    f"覆盖批次 {term_row['lot']} 内"
                )
            if site != term_row["supported_site"]:
                eligibility = "site_unsupported"
                mismatch = True
                reasons.append(
                    f"发生站点 {site} 不受条款支持"
                    f"（仅支持 {term_row['supported_site']}）"
                )
            if not mismatch and part_lot is not None:
                eligibility = "eligible" if not c_is_open(strongest) else "pending"
            if returned is not None and dispatch is not None:
                if days_used > term_row["return_days"]:
                    eligibility = "overdue_cost"
                    reasons.append(
                        f"发出第 {dispatch.day} 天、交回第 {returned.day} 天，"
                        f"用时 {days_used} 天 > 允许 {term_row['return_days']} 天，"
                        "先行替换按成本采购结算"
                    )
                elif not mismatch and part_lot is not None:
                    eligibility = "eligible"
                    reasons.append("覆盖批次、站点一致且按期交回，保修额度可兑现")
            if c_is_open(strongest) and mismatch:
                if strongest in {"requested", "reserved"}:
                    reasons.append("覆盖已不符且计划尚未确认：应撤销预留或改走采购")
                else:
                    reasons.append("调出已确认不可取回：该链将按成本采购结算，不产生保修额度")
            if approval is None:
                reasons.append(f"备件 {spare} 在 {site} 站点尚无可知的互换许可")
            elif approval.approval != "approved":
                reasons.append(
                    f"备件 {spare} 在 {site} 站点状态为 "
                    f"{approval.approval}，复验放行前不能计入可兑现数量"
                )

        chains.append(ReplacementChain(
            chain_id=f"C{idx}",
            spare=spare, site=site, returned_part=part, term=term,
            events=evs, state=strongest,
            dispatch_day=dispatch.day if dispatch else None,
            return_day=returned.day if returned else None,
            days_used=days_used,
            eligibility=eligibility,
            eligibility_reasons=reasons,
            term_row=term_row,
            part_lot=attr.lot if attr else None,
            part_vendor=iv.vendor if iv else None,
            spare_approval=approval.approval if approval else None,
            confirmed_locked=strongest in {"dispatched", "awaiting_inspection",
                                           "return_confirmed", "receipt_recorded"},
        ))
    return chains


@dataclass
class TermPosition:
    term: dict
    open_chains: list[ReplacementChain]
    closed_chains: list[ReplacementChain]
    advance_used: int
    advance_remaining: int
    over_limit: bool
    cost_exposure: float
    eligible_credits: int
    overdue_costs: float
    uncovered_chains: list[ReplacementChain]


def term_positions(view: KnowledgeView,
                   chains: list[ReplacementChain]) -> list[TermPosition]:
    """逐条款的额度占用、可兑现额度与费用敞口。"""
    out: list[TermPosition] = []
    for term in view.terms:
        related = [c for c in chains if c.term == term["term"]]
        open_ = [c for c in related if c.is_open]
        closed = [c for c in related if not c.is_open]
        limit = int(term["advance_limit"])
        eligible = sum(1 for c in closed if c.eligibility == "eligible")
        overdue = [c for c in closed if c.eligibility == "overdue_cost"]
        uncovered = [
            c for c in closed
            if c.eligibility in {"lot_uncovered", "site_unsupported",
                                 "lot_unattributed"}
        ]
        exposure = sum(c.cost_exposure for c in open_)
        overdue_cost = sum(
            float(c.term_row["unit_cost"]) for c in overdue if c.term_row
        )
        out.append(TermPosition(
            term=term,
            open_chains=open_,
            closed_chains=closed,
            advance_used=len(open_),
            advance_remaining=max(0, limit - len(open_)),
            over_limit=len(open_) > limit,
            cost_exposure=round(exposure + overdue_cost, 2),
            eligible_credits=eligible,
            overdue_costs=overdue_cost,
            uncovered_chains=uncovered,
        ))
    return out


@dataclass
class SparePosition:
    spare: str
    site: str
    approval: Optional[str]
    approval_known_day: Optional[int]
    deliverable: bool
    blockers: list[str] = field(default_factory=list)
    committed_chain: Optional[str] = None


def spare_positions(view: KnowledgeView,
                    chains: list[ReplacementChain]) -> list[SparePosition]:
    """共池备件在每个站点的可兑现状态。"""
    out: list[SparePosition] = []
    # 实物被任一未闭环链占用即不可再分配（同一实物跨站点）
    committed: dict[str, ReplacementChain] = {}
    for c in chains:
        if c.is_open:
            committed[c.spare] = c
    for (spare, site), approval in sorted(view.approvals.items()):
        blockers: list[str] = []
        chain = committed.get(spare)
        if chain is not None:
            blockers.append(
                f"实物已被未闭环链 {chain.chain_id} 占用"
                f"（{chain.site} 站，件 {chain.returned_part}）"
            )
        if approval.approval != "approved":
            blockers.append(
                f"互换许可状态 {approval.approval}，复验放行前不可用"
            )
        deliverable = not blockers
        out.append(SparePosition(
            spare=spare, site=site,
            approval=approval.approval,
            approval_known_day=approval.known_day,
            deliverable=deliverable,
            blockers=blockers,
            committed_chain=chain.chain_id if chain else None,
        ))
    return out


def revalidation_notes(chains: list[ReplacementChain]) -> list[dict]:
    """未确认计划按最新证据的重核结果（确认调出的锁定不动）。"""
    notes = []
    for c in chains:
        if c.state in {"requested", "reserved"}:
            notes.append({
                "chain": c.chain_id,
                "spare": c.spare,
                "site": c.site,
                "returned_part": c.returned_part,
                "state": c.state,
                "still_supported": c.eligibility in {"pending", "eligible"},
                "reasons": c.eligibility_reasons,
            })
    return notes
