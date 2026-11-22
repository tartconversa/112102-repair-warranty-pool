"""命令行入口：python -m app.cli <command>。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import clock, explain, importer, records, risk, scenarios, store, warranty

DEFAULT_DB = "data/app.db"
DEFAULT_SOURCES = "sources"


def _connect(args) -> store.sqlite3.Connection:
    return store.connect(args.db)


def _as_of(conn, requested: int | None) -> int:
    return requested if requested is not None else store.latest_known_day(conn)


# ---------- 导入 ----------

def cmd_import(args) -> int:
    conn = _connect(args)
    report = importer.import_sources(conn, args.sources, force=args.force)
    if report.skipped_unchanged:
        print(f"来源未变化，跳过 {len(report.skipped_unchanged)} 张表（--force 可强制重导）")
    for table, n in sorted(report.imported.items()):
        print(f"  导入 {table}: {n} 行")
    for w in report.warnings:
        print(f"  警告: {w}")
    if report.changed:
        scenarios.baseline_from_sources(conn)
        print("  已同步基线方案「基线（现行条款）」")
    return 0


# ---------- 风险 ----------

def cmd_risk(args) -> int:
    conn = _connect(args)
    as_of = _as_of(conn, args.as_of)
    view = clock.build_view(conn, as_of)
    recs = risk.classify_parts(view)
    print(f"分析时点：第 {as_of} 天（观察窗 {len(recs)} 件）\n")

    print("== 状态分桶（不使用送修顺序推定故障时间） ==")
    for status, n in risk.status_counts(recs).items():
        print(f"  {risk.STATUS_LABELS[status]}: {n}")

    print("\n== 批次分层事件率（每 1000 使用小时，95% CI） ==")
    for row in risk.strata_rates(recs, ("lot",)):
        hi = "∞" if row.ci_high == float("inf") else f"{row.ci_high:.3f}"
        print(f"  批次 {row.label}: 事件 {row.events}/{row.parts} 件, "
              f"暴露 {row.exposure_hours:.0f}h, 率 {row.rate_text} "
              f"CI[{row.ci_low:.3f}, {hi}]")

    print("\n== 站点 × 批次分层率 ==")
    for row in risk.strata_rates(recs, ("site", "lot")):
        print(f"  {row.label}: {row.events} 事件 / {row.exposure_hours:.0f}h "
              f"= {row.rate_per_1000h:.3f}/千小时")

    print("\n== L1 批次内供应商直接标化对比（消除站点检查强度差异） ==")
    for row in risk.standardized_vendor_rates(recs, "L1"):
        print(f"  {row['vendor']}: 朴素率 {row['crude_rate_per_1000h']:.3f}, "
              f"标化率 {row['adjusted_rate_per_1000h']:.3f} /千小时")

    print("\n== 站点检查覆盖（解释发现时差） ==")
    for row in risk.inspection_coverage(view, recs):
        print(f"  {row['site']}: 均检查 {row['mean_checks_per_part']} 次/件, "
              f"平均间隔 {row['mean_interval_days']} 天, "
              f"故障发现窗 {row['mean_detection_window_days']} 天")

    print("\n== 朴素停用计数口径（仅暴露偏差，不作结论） ==")
    for row in risk.naive_repair_ranking(recs):
        print(f"  {row['vendor']}: 停用 {row['withdrawn']}, 故障证据 "
              f"{row['events']}, 暴露 {row['exposure']:.0f}h")

    if args.parts:
        print("\n== 逐件记录 ==")
        for r in recs:
            print(f"  {r.part} [{r.vendor}/{r.lot or '未知'}@{r.site}] "
                  f"{risk.STATUS_LABELS[r.status]} 暴露 {r.exposure_hours:.1f}h")
            for n in r.notes:
                print(f"      · {n}")
    return 0


# ---------- 保修与备件 ----------

def cmd_warranty(args) -> int:
    conn = _connect(args)
    as_of = _as_of(conn, args.as_of)
    view = clock.build_view(conn, as_of)
    chains = warranty.build_chains(view)
    print(f"分析时点：第 {as_of} 天\n")

    print("== 替换交接链 ==")
    for c in chains:
        print(f"  {c.chain_id} {c.spare}@{c.site} 件 {c.returned_part} "
              f"条款 {c.term} 状态 {warranty.CHAIN_STATE_LABELS[c.state]}")
        print(f"      保修结论: {warranty.ELIGIBILITY_LABELS[c.eligibility]}"
              f"（确认锁定: {'是' if c.confirmed_locked else '否，刷新重核'}）")
        for reason in c.eligibility_reasons:
            print(f"      · {reason}")

    print("\n== 条款额度与费用敞口 ==")
    for p in warranty.term_positions(view, chains):
        t = p.term
        flag = " 【超额度】" if p.over_limit else ""
        print(f"  {t['term']} ({t['vendor']}/{t['lot']}@{t['supported_site']}): "
              f"未闭环 {p.advance_used}/{t['advance_limit']}, "
              f"剩余额度 {p.advance_remaining}, 可兑现保修 {p.eligible_credits}, "
              f"费用敞口 {p.cost_exposure}{flag}")

    print("\n== 共池备件可兑现状态 ==")
    for p in warranty.spare_positions(view, chains):
        mark = "可兑现" if p.deliverable else "不可兑现"
        print(f"  {p.spare}@{p.site} [{p.approval}] {mark}")
        for b in p.blockers:
            print(f"      · {b}")

    print("\n== 未确认计划重核 ==")
    notes = warranty.revalidation_notes(chains)
    if not notes:
        print("  无")
    for n in notes:
        verdict = "仍获支持" if n["still_supported"] else "证据不再支持"
        print(f"  {n['spare']}@{n['site']} 件 {n['returned_part']} "
              f"（{n['state']}）: {verdict}")
        for r in n["reasons"]:
            print(f"      · {r}")
    return 0


# ---------- 需求 ----------

def cmd_demand(args) -> int:
    conn = _connect(args)
    as_of = _as_of(conn, args.as_of)
    view = clock.build_view(conn, as_of)
    recs = risk.classify_parts(view)
    print(f"分析时点：第 {as_of} 天，展望 {args.horizon} 天，"
          f"用量倍数 {args.multiplier}\n")
    for p in risk.project_demand(recs, view, args.horizon, args.multiplier):
        print(f"  {p.site} 站 批次 {p.lot or '未知'}: 在役 {p.continuing_parts} 件"
              f"（含换站 {p.transferred_parts}）, "
              f"未来暴露 {p.future_exposure_hours:.0f}h, "
              f"预计故障 {p.expected_failures}, 计划拆换 {p.planned_removals}, "
              f"备件需求 {p.total_spares_needed}（p90 {p.demand_p90}）")
    return 0


# ---------- 方案 ----------

def cmd_scenario(args) -> int:
    conn = _connect(args)
    if args.scenario_action == "list":
        for s in scenarios.list_scenarios(conn):
            print(f"  {s['name']} | 展望 {s['horizon_days']} 天 | "
                  f"用量 ×{s['usage_multiplier']} | {s['description']}")
        return 0
    if args.scenario_action == "create":
        spec = json.loads(Path(args.file).read_text(encoding="utf-8"))
        scenarios.save_scenario(
            conn, spec["name"], spec["horizon_days"],
            spec["usage_multiplier"], spec["terms"],
            description=spec.get("description", ""),
        )
        print(f"已保存方案 {spec['name']}")
        return 0
    # eval / compare
    as_of = _as_of(conn, args.as_of)
    view = clock.build_view(conn, as_of)
    recs = risk.classify_parts(view)
    if args.scenario_action == "eval":
        names = [args.name]
    else:
        names = args.names or [s["name"] for s in scenarios.list_scenarios(conn)]
    results = scenarios.compare(conn, view, recs, names)
    for res in results:
        print(f"\n== 方案 {res.name}（{res.description}）==")
        print(f"  展望 {res.horizon_days} 天，用量 ×{res.usage_multiplier}")
        for row in res.rows:
            print(f"  {row.site}/{row.lot or '未知'}: 预计故障 "
                  f"{row.expected_failures}（覆盖 {row.covered_failures}/"
                  f"未覆盖 {row.uncovered_failures}），备件 {row.spares_needed}，"
                  f"成本 {row.expected_cost}，条款 {row.covering_terms or '无'}")
        t = res.totals
        print(f"  合计: 故障 {t['expected_failures']}, 可保修 "
              f"{t['covered_failures']}, 费用敞口 {t['expected_cost']}")
        for f in res.limit_flags:
            print(f"  额度预警: {f}")
    return 0


# ---------- 登记 ----------

def cmd_register_event(args) -> int:
    conn = _connect(args)
    records.register_event(
        conn, event=args.event, spare=args.spare, site=args.site,
        returned_part=args.part, term=args.term, action=args.action,
        day=args.day, note=args.note or "",
    )
    print(f"已登记 {args.event}: {args.action}")
    return 0


def cmd_register_approval(args) -> int:
    conn = _connect(args)
    records.register_approval(
        conn, spare=args.spare, site=args.site, approval=args.approval,
        known_day=args.day, note=args.note or "",
    )
    print(f"已登记复验放行 {args.spare}@{args.site} -> {args.approval}")
    return 0


# ---------- 解释 ----------

def cmd_explain(args) -> int:
    conn = _connect(args)
    diff = explain.diff_as_of(conn, args.t1, args.t2)
    print(f"第 {diff.earlier} 天 → 第 {diff.later} 天的变化依据\n")
    print("== 新到达证据 ==")
    for e in diff.new_evidence:
        print(f"  第 {e.day} 天 [{e.kind}] {e.label}")
    print("\n== 部件结论变化 ==")
    for c in diff.part_changes:
        print(f"  {c['part']}: {c['change']}")
    print("\n== 替换链/保修变化 ==")
    for c in diff.chain_changes:
        print(f"  {c['chain']}: {c['change']}；保修 {c['eligibility']}")
    print("\n== 备件可兑现变化 ==")
    for c in diff.spare_changes:
        print(f"  {c['spare']}: {c['change']}")
    print("\n== 目前仍可采取的措施 ==")
    for a in diff.actions_now or ["（无）"]:
        print(f"  · {a}")
    return 0


def cmd_web(args) -> int:
    from .web import serve
    serve(args.db, args.host, args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="app", description=__doc__)
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite 数据库路径")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("import", help="导入/刷新来源 CSV")
    sp.add_argument("--sources", default=DEFAULT_SOURCES)
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("risk", help="停用风险分析")
    sp.add_argument("--as-of", type=int, default=None)
    sp.add_argument("--parts", action="store_true", help="输出逐件记录")
    sp.set_defaults(func=cmd_risk)

    sp = sub.add_parser("warranty", help="保修资格、额度与共池备件")
    sp.add_argument("--as-of", type=int, default=None)
    sp.set_defaults(func=cmd_warranty)

    sp = sub.add_parser("demand", help="用量假设下的备件需求")
    sp.add_argument("--as-of", type=int, default=None)
    sp.add_argument("--horizon", type=int, default=28)
    sp.add_argument("--multiplier", type=float, default=1.0)
    sp.set_defaults(func=cmd_demand)

    sp = sub.add_parser("scenario", help="续约方案")
    ssub = sp.add_subparsers(dest="scenario_action", required=True)
    ssub.add_parser("list")
    c = ssub.add_parser("create")
    c.add_argument("file", help="方案 JSON 文件")
    e = ssub.add_parser("eval")
    e.add_argument("name")
    e.add_argument("--as-of", type=int, default=None)
    cp = ssub.add_parser("compare")
    cp.add_argument("names", nargs="*")
    cp.add_argument("--as-of", type=int, default=None)
    sp.set_defaults(func=cmd_scenario)

    sp = sub.add_parser("register-event", help="登记本地替换交接")
    sp.add_argument("--event", required=True)
    sp.add_argument("--spare", required=True)
    sp.add_argument("--site", required=True)
    sp.add_argument("--part", required=True)
    sp.add_argument("--term", default=None)
    sp.add_argument("--action", required=True,
                    choices=sorted(records.ACTION_PREREQ) + ["requested"])
    sp.add_argument("--day", type=int, required=True)
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_register_event)

    sp = sub.add_parser("register-approval", help="登记备件复验放行")
    sp.add_argument("--spare", required=True)
    sp.add_argument("--site", required=True)
    sp.add_argument("--approval", required=True, choices=sorted(store.VALID_APPROVALS))
    sp.add_argument("--day", type=int, required=True)
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_register_approval)

    sp = sub.add_parser("explain", help="对比两个时点的结论变化")
    sp.add_argument("t1", type=int)
    sp.add_argument("t2", type=int)
    sp.set_defaults(func=cmd_explain)

    sp = sub.add_parser("web", help="启动本地网页应用")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.set_defaults(func=cmd_web)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (importer.ImportError_, records.RegistrationError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except (KeyError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
