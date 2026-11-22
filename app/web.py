"""本地网页界面：仅依赖标准库 http.server，无外部资源。

页面：
  /                 总览（状态分桶、批次率、额度、敞口）
  /parts            逐件风险记录与证据说明
  /warranty         替换链、条款额度、共池备件、未确认计划重核
  /demand           用量假设与备件需求
  /scenarios        续约方案比较与方案维护
  /explain          两个时点的证据与建议变化
  /register         替换交接与复验放行登记
"""
from __future__ import annotations

import html
import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import clock, explain, importer, records, risk, scenarios, store, warranty

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2430;--muted:#5b6675;--line:#e3e7ee;
--accent:#1f5fa8;--bad:#b3261e;--warn:#9a6700;--ok:#1e7d43;}
*{box-sizing:border-box}
body{margin:0;font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
background:var(--bg);color:var(--ink);font-size:14px;line-height:1.55}
header{background:#10243e;color:#fff;padding:14px 24px;display:flex;
flex-wrap:wrap;align-items:center;gap:18px}
header h1{font-size:17px;margin:0;font-weight:650}
nav{display:flex;gap:4px;flex-wrap:wrap}
nav a{color:#cdd9ea;text-decoration:none;padding:5px 10px;border-radius:6px}
nav a:hover{background:#213c61;color:#fff}
main{max-width:1180px;margin:22px auto;padding:0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin-bottom:18px}
.card h2{font-size:15px;margin:0 0 12px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid var(--line);padding:7px 9px;text-align:left;
vertical-align:top}
th{color:var(--muted);font-weight:600;background:#fafbfc}
tr:hover td{background:#f8fafd}
.kpi{display:flex;gap:14px;flex-wrap:wrap}
.kpi .box{flex:1;min-width:150px;background:#fafbfc;border:1px solid var(--line);
border-radius:8px;padding:12px}
.kpi .num{font-size:22px;font-weight:700}
.kpi .lbl{color:var(--muted);font-size:12px}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;
white-space:nowrap}
.b-fail{background:#fbe3e1;color:var(--bad)}
.b-ok{background:#e2f4e9;color:var(--ok)}
.b-warn{background:#fdf0d9;color:var(--warn)}
.b-muted{background:#eef1f5;color:var(--muted)}
.muted{color:var(--muted)}
.small{font-size:12px}
form.inline{display:flex;gap:8px;flex-wrap:wrap;align-items:end}
label{font-size:12px;color:var(--muted);display:block;margin-bottom:3px}
input,select,button{font:inherit;padding:6px 9px;border:1px solid var(--line);
border-radius:6px;background:#fff}
button{background:var(--accent);color:#fff;border-color:var(--accent);cursor:pointer}
button.secondary{background:#fff;color:var(--ink)}
ul.reasons{margin:4px 0 0;padding-left:16px}
ul.reasons li{font-size:12px;color:var(--muted)}
.flash{padding:10px 14px;border-radius:8px;margin-bottom:16px}
.flash-ok{background:#e2f4e9;color:var(--ok)}
.flash-err{background:#fbe3e1;color:var(--bad)}
.tabs{display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap}
.tabs a{padding:6px 12px;border:1px solid var(--line);border-radius:6px;
text-decoration:none;color:var(--ink);background:#fff}
.tabs a.on{background:var(--accent);color:#fff;border-color:var(--accent)}
code{background:#eef1f5;padding:1px 5px;border-radius:4px}
"""

NAV = [
    ("/", "总览"), ("/parts", "部件风险"), ("/warranty", "保修与备件"),
    ("/demand", "备件需求"), ("/scenarios", "续约方案"),
    ("/explain", "时点对比"), ("/register", "交接登记"),
]


def esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=False)


def badge(text: str, kind: str) -> str:
    return f'<span class="badge b-{kind}">{esc(text)}</span>'


def page(title: str, body: str, as_of: int | None = None,
         flash: tuple[str, str] | None = None) -> bytes:
    nav = "".join(
        f'<a href="{u}">{esc(t)}</a>' for u, t in NAV
    )
    clock_bar = ""
    if as_of is not None:
        clock_bar = (
            '<form class="inline" method="get" action="/">'
            '<div><label>分析时点（第 N 天，含当天可知信息）</label>'
            f'<input type="number" name="as_of" value="{as_of}" min="0" style="width:110px">'
            '</div><button type="submit">刷新分析</button></form>'
        )
    import_form = (
        '<form method="post" action="/import">'
        '<label>从 sources/ 重新导入（本地登记保留）</label>'
        '<button class="secondary" type="submit">重新导入来源</button></form>'
    )
    flash_html = ""
    if flash:
        flash_html = f'<div class="flash flash-{flash[0]}">{esc(flash[1])}</div>'
    doc = f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 部件停用风险与备件调配</title>
<style>{CSS}</style></head><body>
<header><h1>低空服务部件：停用风险 · 保修 · 共池备件</h1><nav>{nav}</nav></header>
<main>{flash_html}
<div class="card"><div class="inline" style="display:flex;justify-content:space-between;
flex-wrap:wrap;gap:12px;align-items:end"><div>{clock_bar}</div><div>{import_form}</div></div></div>
{body}</main></body></html>"""
    return doc.encode("utf-8")


class AppState:
    def __init__(self, db_path: str):
        self.db_path = db_path


def _as_of(handler, conn) -> int:
    q = handler.query.get("as_of", [None])[0]
    if q:
        try:
            return max(0, int(q))
        except ValueError:
            pass
    return store.latest_known_day(conn)


def _badge_for_eligibility(elig: str) -> str:
    kind = {
        "eligible": "ok", "overdue_cost": "fail", "lot_uncovered": "fail",
        "site_unsupported": "fail", "lot_unattributed": "warn", "pending": "muted",
    }.get(elig, "muted")
    return badge(warranty.ELIGIBILITY_LABELS[elig], kind)


def _badge_for_status(status: str) -> str:
    kind = {
        "interval_failure": "fail", "unattributed": "warn",
        "in_service": "ok", "planned": "muted", "transfer": "muted",
    }.get(status, "muted")
    return badge(risk.STATUS_LABELS[status], kind)


# ---------- 页面渲染 ----------

def render_overview(handler, conn, as_of: int) -> str:
    view = clock.build_view(conn, as_of)
    recs = risk.classify_parts(view)
    counts = risk.status_counts(recs)
    lot_rows = risk.strata_rates(recs, ("lot",))
    chains = warranty.build_chains(view)
    positions = warranty.term_positions(view, chains)
    spares = warranty.spare_positions(view, chains)

    kpi = "".join(
        f'<div class="box"><div class="num">{v}</div>'
        f'<div class="lbl">{esc(risk.STATUS_LABELS[k])}</div></div>'
        for k, v in counts.items()
    )
    deliverable = sum(1 for s in spares if s.deliverable)
    kpi += (
        f'<div class="box"><div class="num">{deliverable}</div>'
        f'<div class="lbl">当前可兑现备件许可（站点×实物）</div></div>'
        f'<div class="box"><div class="num">{sum(p.cost_exposure for p in positions):.0f}</div>'
        f'<div class="lbl">费用敞口（合成货币单位）</div></div>'
    )

    lot_html = "".join(
        f"<tr><td>{esc(r.label)}</td><td>{r.parts}</td><td>{r.events}</td>"
        f"<td>{r.exposure_hours:.0f}</td><td>{r.rate_per_1000h:.3f}</td>"
        f"<td>{r.ci_low:.3f} – {'∞' if r.ci_high == float('inf') else f'{r.ci_high:.3f}'}</td></tr>"
        for r in lot_rows
    )
    cover = risk.inspection_coverage(view, recs)
    cover_html = "".join(
        f"<tr><td>{esc(r['site'])}</td><td>{r['parts']}</td>"
        f"<td>{r['mean_checks_per_part']}</td><td>{esc(r['mean_interval_days'])}</td>"
        f"<td>{r['failures_found']}</td><td>{esc(r['mean_detection_window_days'])}</td></tr>"
        for r in cover
    )
    term_html = "".join(
        f"<tr><td>{esc(p.term['term'])}</td>"
        f"<td>{esc(p.term['vendor'])}/{esc(p.term['lot'])}@{esc(p.term['supported_site'])}</td>"
        f"<td>{p.advance_used}/{p.term['advance_limit']}{' ' + badge('超额度','fail') if p.over_limit else ''}</td>"
        f"<td>{p.eligible_credits}</td><td>{p.overdue_costs:.0f}</td>"
        f"<td>{p.cost_exposure:.0f}</td></tr>"
        for p in positions
    )
    naive = risk.naive_repair_ranking(recs)
    naive_html = "".join(
        f"<tr><td>{esc(r['vendor'])}</td><td>{r['withdrawn']}</td>"
        f"<td>{r['events']}</td><td>{r['exposure']:.0f}</td>"
        f"<td>{1000 * r['events'] / r['exposure']:.3f}</td></tr>"
        for r in naive
    )
    std = risk.standardized_vendor_rates(recs, "L1")
    std_rows = []
    for r in std:
        per_site = ", ".join(
            f"{s} {v['rate'] * 1000:.3f}" for s, v in r["per_site"].items()
        )
        std_rows.append(
            f"<tr><td>{esc(r['vendor'])}</td>"
            f"<td>{r['crude_rate_per_1000h']:.3f}</td>"
            f"<td>{r['adjusted_rate_per_1000h']:.3f}</td>"
            f"<td>{esc(per_site)}</td></tr>"
        )
    std_html = "".join(std_rows)
    return f"""
<div class="card"><h2>时点 {as_of} 的风险与备件总览</h2>
<div class="kpi">{kpi}</div>
<p class="muted small" style="margin-top:10px">故障只承认在该时点之前可知的检查结论；
送修顺序不用于推定故障时间。批号未明的件单列“不能归属”。</p></div>
<div class="card"><h2>批次分层停用事件率（每 1000 使用小时）</h2>
<table><thead><tr><th>批次</th><th>件数</th><th>故障事件</th><th>暴露小时</th>
<th>事件率</th><th>95% CI</th></tr></thead><tbody>{lot_html}</tbody></table></div>
<div class="card"><h2>站点检查覆盖与发现时差</h2>
<table><thead><tr><th>站点</th><th>件数</th><th>均检查次数/件</th>
<th>平均检查间隔(天)</th><th>发现故障数</th><th>平均发现窗(天)</th></tr></thead>
<tbody>{cover_html}</tbody></table>
<p class="muted small">检查更密的站点更早确认磨损，但按使用小时归一后事件率可比；
发现窗只反映检查间隔，不代表真实故障发生时刻。</p></div>
<div class="card"><h2>L1 批次内供应商对比（站点暴露直接标化，剔除检查强度混杂）</h2>
<table><thead><tr><th>供应商</th><th>朴素事件率/千小时</th>
<th>标化事件率/千小时</th><th>各站点分层率/千小时</th></tr></thead>
<tbody>{std_html}</tbody></table>
<p class="muted small">标化率使用两站点在 L1 上的合计暴露作为共同权重；
若某供应商的件集中在检查更密的站点，朴素率会高估其风险。</p></div>
<div class="card"><h2>条款额度与费用敞口</h2>
<table><thead><tr><th>条款</th><th>覆盖</th><th>未闭环/额度</th>
<th>可兑现保修</th><th>逾期成本</th><th>费用敞口</th></tr></thead>
<tbody>{term_html}</tbody></table></div>
<div class="card"><h2>朴素停用计数（仅用于暴露指标偏差，不作续约结论）</h2>
<table><thead><tr><th>供应商</th><th>停用计数</th><th>有故障证据</th>
<th>暴露小时</th><th>证据事件率/千小时</th></tr></thead>
<tbody>{naive_html}</tbody></table></div>"""


def render_parts(handler, conn, as_of: int) -> str:
    view = clock.build_view(conn, as_of)
    recs = risk.classify_parts(view)
    rows = []
    for r in recs:
        notes = "".join(f"<li>{esc(n)}</li>" for n in r.notes)
        ev = (
            f"{r.event_hours_lower:.1f}–{r.event_hours_upper:.1f}h"
            if r.is_event else "—"
        )
        lot_badge = (
            badge(r.lot, "ok") if r.lot_source == "raw"
            else badge(r.lot or "未知", "warn")
        )
        rows.append(
            f"<tr><td>{esc(r.part)}</td><td>{esc(r.vendor)}</td>"
            f"<td>{lot_badge}<div class='small muted'>{esc(r.lot_source)}</div></td>"
            f"<td>{esc(r.site)}</td><td>{_badge_for_status(r.status)}</td>"
            f"<td>{r.exposure_hours:.1f}</td><td>{esc(ev)}</td>"
            f"<td>{r.n_checks}（间隔 {esc(r.mean_check_interval)} 天）</td>"
            f"<td><ul class='reasons'>{notes}</ul></td></tr>"
        )
    km = risk.kaplan_meier(recs)
    km_html = "".join(
        f"<tr><td>{p['hours']}</td><td>{p['at_risk_start']}</td>"
        f"<td>{p['events']}</td><td>{p['censored']}</td>"
        f"<td>{p['survival']:.4f}</td></tr>"
        for p in km if p["events"]
    )
    return f"""
<div class="card"><h2>逐件生存记录（{len(recs)} 件，时点 {as_of}）</h2>
<table><thead><tr><th>部件</th><th>供应商</th><th>批次归属</th><th>站点</th>
<th>状态</th><th>暴露/事件小时</th><th>故障区间</th><th>检查覆盖</th>
<th>证据与口径说明</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<div class="card"><h2>Kaplan–Meier 生存阶梯（使用小时尺度，仅列事件点）</h2>
<table><thead><tr><th>小时</th><th>在险</th><th>事件</th><th>删失</th>
<th>生存函数</th></tr></thead><tbody>{km_html}</tbody></table></div>"""


def render_warranty(handler, conn, as_of: int) -> str:
    view = clock.build_view(conn, as_of)
    chains = warranty.build_chains(view)
    chain_rows = []
    for c in chains:
        reasons = "".join(f"<li>{esc(x)}</li>" for x in c.eligibility_reasons)
        trail = " → ".join(
            f"{esc(e.action)}({e.day}{'·本地' if e.origin == 'local' else ''})"
            for e in c.events
        )
        chain_rows.append(
            f"<tr><td>{esc(c.chain_id)}</td><td>{esc(c.spare)}@{esc(c.site)}</td>"
            f"<td>{esc(c.returned_part)}<div class='small muted'>批次 {esc(c.part_lot)}</div></td>"
            f"<td>{esc(c.term)}</td>"
            f"<td>{badge(warranty.CHAIN_STATE_LABELS[c.state], 'muted' if c.is_open else 'ok')}</td>"
            f"<td>{_badge_for_eligibility(c.eligibility)}</td>"
            f"<td>{badge('确认锁定','warn') if c.confirmed_locked else badge('未确认·刷新重核','muted')}</td>"
            f"<td class='small'>{trail}<ul class='reasons'>{reasons}</ul></td></tr>"
        )
    positions = warranty.term_positions(view, chains)
    pos_rows = "".join(
        f"<tr><td>{esc(p.term['term'])}</td>"
        f"<td>{esc(p.term['vendor'])}/{esc(p.term['lot'])}@{esc(p.term['supported_site'])}</td>"
        f"<td>{p.term['return_days']} 天</td>"
        f"<td>{p.advance_used} / {p.term['advance_limit']}</td>"
        f"<td>{p.advance_remaining}</td><td>{p.eligible_credits}</td>"
        f"<td>{p.overdue_costs:.0f}</td><td>{p.cost_exposure:.0f}</td></tr>"
        for p in positions
    )
    spares = warranty.spare_positions(view, chains)
    spare_rows = "".join(
        f"<tr><td>{esc(s.spare)}@{esc(s.site)}</td>"
        f"<td>{badge(s.approval, 'ok' if s.approval == 'approved' else 'warn')}</td>"
        f"<td>{badge('可兑现','ok') if s.deliverable else badge('不可兑现','fail')}</td>"
        f"<td>{esc(s.committed_chain or '—')}</td>"
        f"<td><ul class='reasons'>{''.join(f'<li>{esc(b)}</li>' for b in s.blockers) or '—'}</ul></td></tr>"
        for s in spares
    )
    notes = warranty.revalidation_notes(chains)
    note_rows = "".join(
        f"<tr><td>{esc(n['spare'])}@{esc(n['site'])}</td>"
        f"<td>{esc(n['returned_part'])}</td><td>{esc(n['state'])}</td>"
        f"<td>{badge('仍获支持','ok') if n['still_supported'] else badge('不再支持','fail')}</td>"
        f"<td><ul class='reasons'>{''.join(f'<li>{esc(x)}</li>' for x in n['reasons'])}</ul></td></tr>"
        for n in notes
    ) or "<tr><td colspan='5' class='muted'>无未确认计划</td></tr>"
    return f"""
<div class="card"><h2>替换交接链（同一场换件的风险依据与覆盖决定）</h2>
<table><thead><tr><th>链</th><th>备件/站点</th><th>原件</th><th>条款</th>
<th>状态</th><th>保修结论</th><th>确认性质</th><th>事件轨迹与依据</th></tr></thead>
<tbody>{''.join(chain_rows)}</tbody></table></div>
<div class="card"><h2>条款额度（仅未闭环链占用先行额度）</h2>
<table><thead><tr><th>条款</th><th>覆盖</th><th>交回期限</th>
<th>未闭环/额度</th><th>剩余额度</th><th>可兑现保修</th>
<th>逾期成本</th><th>费用敞口</th></tr></thead><tbody>{pos_rows}</tbody></table></div>
<div class="card"><h2>共池备件可兑现状态</h2>
<table><thead><tr><th>备件/站点</th><th>互换许可</th><th>可兑现</th>
<th>占用链</th><th>阻碍</th></tr></thead><tbody>{spare_rows}</tbody></table></div>
<div class="card"><h2>未确认计划的最新证据重核</h2>
<table><thead><tr><th>备件/站点</th><th>原件</th><th>状态</th><th>结论</th>
<th>依据</th></tr></thead><tbody>{note_rows}</tbody></table>
<p class="small muted">已确认调出的链不会因刷新取回；requested/reserved 计划每次按最新
批号与许可重新核对。</p></div>"""


def render_demand(handler, conn, as_of: int) -> str:
    horizon = int(handler.query.get("horizon", ["28"])[0] or 28)
    multiplier = float(handler.query.get("mult", ["1.0"])[0] or 1.0)
    view = clock.build_view(conn, as_of)
    recs = risk.classify_parts(view)
    projections = risk.project_demand(recs, view, horizon, multiplier)
    rows = "".join(
        f"<tr><td>{esc(p.site)}</td><td>{esc(p.lot or '未知')}</td>"
        f"<td>{p.continuing_parts}（换站 {p.transferred_parts}）</td>"
        f"<td>{p.future_exposure_hours:.0f}</td>"
        f"<td>{p.expected_failures:.2f}</td><td>{p.planned_removals}</td>"
        f"<td>{p.total_spares_needed:.2f}</td><td>{p.demand_p90:.2f}</td></tr>"
        for p in projections
    )
    form = f"""<form method="get" action="/demand">
    <input type="hidden" name="as_of" value="{as_of}">
    <div style="display:flex;gap:8px;flex-wrap:wrap">
    <div><label>展望天数</label><input name="horizon" value="{horizon}" type="number" min="1"></div>
    <div><label>用量倍数</label><input name="mult" value="{multiplier}" type="number" step="0.1" min="0.1"></div>
    <button type="submit">重算</button></div></form>"""
    return f"""
<div class="card"><h2>用量假设（{form}）</h2>
<p class="small muted">仅时点上仍在役的件贡献未来暴露；计划拆换按确定性需求另计；
换站带走的件不占原站点需求。p90 = λ + 1.282√λ（泊松近似）。</p>
<table><thead><tr><th>站点</th><th>批次</th><th>在役件</th>
<th>未来暴露小时</th><th>预计故障 λ</th><th>计划拆换</th>
<th>备件需求(λ+计划)</th><th>p90 备货</th></tr></thead>
<tbody>{rows}</tbody></table></div>"""


def render_scenarios(handler, conn, as_of: int) -> str:
    names = [s["name"] for s in scenarios.list_scenarios(conn)]
    view = clock.build_view(conn, as_of)
    recs = risk.classify_parts(view)
    selected = handler.query.get("s", names)
    results = scenarios.compare(conn, view, recs, selected)
    placeholder = esc(
        '{"name":"方案A","horizon_days":28,"usage_multiplier":1.2,'
        '"description":"","terms":[{"term":"W1","vendor":"V1","lot":"L1",'
        '"supported_site":"north","return_days":5,"unit_cost":190,'
        '"advance_limit":2,"enabled":1}]}'
    )
    head = "".join(f"<th>{esc(r.name)}</th>" for r in results)
    metrics = [
        ("展望天数", lambda r: r.horizon_days),
        ("用量倍数", lambda r: r.usage_multiplier),
        ("预计停用（故障）", lambda r: r.totals["expected_failures"]),
        ("其中可保修", lambda r: r.totals["covered_failures"]),
        ("其中未覆盖", lambda r: r.totals["uncovered_failures"]),
        ("计划拆换", lambda r: r.totals["planned_removals"]),
        ("备件需求合计", lambda r: r.totals["spares_needed"]),
        ("费用敞口", lambda r: r.totals["expected_cost"]),
    ]
    body = "".join(
        f"<tr><td>{esc(label)}</td>" +
        "".join(f"<td>{fn(r)}</td>" for r in results) + "</tr>"
        for label, fn in metrics
    )
    flags = "".join(
        f"<li><b>{esc(r.name)}</b>：{esc(f)}</li>"
        for r in results for f in r.limit_flags
    ) or "<li class='muted'>无额度预警</li>"

    tabs = "".join(
        f'<a class="{"on" if n in selected else ""}" href="/scenarios?'
        f'as_of={as_of}&s={urllib.parse.quote(n)}">{esc(n)}</a>'
        for n in names
    )
    rows_html = ""
    for r in results:
        trows = "".join(
            f"<tr><td>{esc(x.site)}</td><td>{esc(x.lot or '未知')}</td>"
            f"<td>{esc(x.vendor_mix)}</td><td>{x.expected_failures:.2f}</td>"
            f"<td>{x.covered_failures:.2f}</td><td>{x.uncovered_failures:.2f}</td>"
            f"<td>{x.spares_needed:.2f}</td><td>{x.expected_cost:.0f}</td>"
            f"<td>{esc(', '.join(x.covering_terms) or '无')}</td></tr>"
            for x in r.rows
        )
        rows_html += (
            f'<div class="card"><h2>{esc(r.name)} · 站点批次明细</h2>'
            f'<table><thead><tr><th>站点</th><th>批次</th><th>供应商暴露构成</th>'
            f'<th>预计故障</th><th>可保修</th><th>未覆盖</th><th>备件需求</th>'
            f'<th>预计成本</th><th>覆盖条款</th></tr></thead>'
            f'<tbody>{trows}</tbody></table></div>'
        )
    return f"""
<div class="tabs">{tabs}</div>
<div class="card"><h2>方案并排比较（时点 {as_of}）</h2>
<table><thead><tr><th>指标</th>{head}</tr></thead><tbody>{body}</tbody></table>
<ul class="reasons" style="margin-top:10px">{flags}</ul></div>
{rows_html}
<div class="card"><h2>新建/覆盖方案（JSON）</h2>
<form method="post" action="/scenarios/create">
<input type="hidden" name="as_of" value="{as_of}">
<textarea name="spec" rows="10" style="width:100%;font-family:monospace"
placeholder='{placeholder}'></textarea>
<div style="margin-top:8px"><button type="submit">保存方案</button></div></form></div>"""


def render_explain(handler, conn, as_of: int) -> str:
    milestones = clock.knowledge_milestones(conn)
    t1 = int(handler.query.get("t1", [min(milestones, default=0)])[0])
    t2 = int(handler.query.get("t2", [as_of])[0])
    diff = explain.diff_as_of(conn, t1, t2)
    ev_rows = "".join(
        f"<tr><td>{e.day}</td><td>{esc(e.kind)}</td><td>{esc(e.label)}</td></tr>"
        for e in diff.new_evidence
    ) or "<tr><td colspan='3' class='muted'>两个时点之间没有新证据</td></tr>"
    part_rows = "".join(
        f"<tr><td>{esc(c['part'])}</td><td>{esc(c['before'])}</td>"
        f"<td>{esc(c['after'])}</td><td>{esc(c['change'])}</td></tr>"
        for c in diff.part_changes
    ) or "<tr><td colspan='4' class='muted'>无部件结论变化</td></tr>"
    chain_rows = "".join(
        f"<tr><td>{esc(c['chain'])}</td><td>{esc(c['change'])}</td>"
        f"<td>{esc(c['eligibility'])}</td></tr>"
        for c in diff.chain_changes
    ) or "<tr><td colspan='3' class='muted'>无替换链变化</td></tr>"
    spare_rows = "".join(
        f"<tr><td>{esc(c['spare'])}</td><td>{esc(c['change'])}</td></tr>"
        for c in diff.spare_changes
    ) or "<tr><td colspan='2' class='muted'>无备件状态变化</td></tr>"
    actions = "".join(f"<li>{esc(a)}</li>" for a in diff.actions_now) \
        or "<li class='muted'>当前没有待处理措施</li>"
    options1 = "".join(
        f'<option value="{d}" {"selected" if d == t1 else ""}>第 {d} 天</option>'
        for d in milestones
    )
    options2 = "".join(
        f'<option value="{d}" {"selected" if d == t2 else ""}>第 {d} 天</option>'
        for d in milestones
    )
    return f"""
<div class="card"><h2>选择两个时点</h2>
<form method="get" action="/explain" class="inline">
<div><label>较早时点 T1</label><select name="t1">{options1}</select></div>
<div><label>较晚时点 T2</label><select name="t2">{options2}</select></div>
<input type="hidden" name="as_of" value="{as_of}">
<button type="submit">对比并解释</button></form></div>
<div class="card"><h2>新到达证据（{t1} → {t2}）</h2>
<table><thead><tr><th>天</th><th>类型</th><th>证据</th></tr></thead>
<tbody>{ev_rows}</tbody></table></div>
<div class="card"><h2>部件结论变化及依据</h2>
<table><thead><tr><th>部件</th><th>T1 状态</th><th>T2 状态</th><th>变化说明</th></tr></thead>
<tbody>{part_rows}</tbody></table></div>
<div class="card"><h2>替换链与保修结论变化</h2>
<table><thead><tr><th>链</th><th>状态变化</th><th>保修结论</th></tr></thead>
<tbody>{chain_rows}</tbody></table></div>
<div class="card"><h2>共池备件可兑现变化</h2>
<table><thead><tr><th>备件</th><th>变化</th></tr></thead>
<tbody>{spare_rows}</tbody></table></div>
<div class="card"><h2>目前仍可采取的措施</h2><ul>{actions}</ul></div>"""


def render_register(handler, conn, as_of: int) -> str:
    view = clock.build_view(conn, as_of)
    chains = warranty.build_chains(view)
    audit_rows = "".join(
        f"<tr><td>{esc(a['chain'])}</td><td>{esc(a['spare'])}@{esc(a['site'])}</td>"
        f"<td>{esc(a['returned_part'])}</td><td>{esc(a['term'])}</td>"
        f"<td>{esc(warranty.CHAIN_STATE_LABELS.get(a['state'], a['state']))}</td>"
        f"<td>{badge('锁定' if a['confirmed_locked'] else '可重核','warn' if a['confirmed_locked'] else 'muted')}</td>"
        f"<td><ul class='reasons'>{''.join(f'<li>{esc(x)}</li>' for x in a['reasons'])}</ul></td></tr>"
        for a in records.open_chain_audit(conn, as_of)
    ) or "<tr><td colspan='7' class='muted'>无未闭环链</td></tr>"
    part_opts = "".join(
        f"<option value='{esc(p)}'>{esc(p)}</option>"
        for p in sorted(view.intervals)
    )
    spare_opts = "".join(
        f"<option value='{esc(s)}'>{esc(s)}</option>"
        for s in sorted({k[0] for k in view.approvals})
    )
    term_opts = "".join(
        f"<option value='{esc(t['term'])}'>{esc(t['term'])} "
        f"({esc(t['vendor'])}/{esc(t['lot'])}@{esc(t['supported_site'])})</option>"
        for t in view.terms
    )
    action_opts = "".join(
        f"<option>{esc(a)}</option>"
        for a in ["requested"] + sorted(records.ACTION_PREREQ)
    )
    return f"""
<div class="card"><h2>登记替换交接（本地记录，重导来源不会清空）</h2>
<form method="post" action="/register/event" class="inline">
<input type="hidden" name="as_of" value="{as_of}">
<div><label>事件编号</label><input name="event" required placeholder="Z6"></div>
<div><label>备件</label><select name="spare">{spare_opts}</select></div>
<div><label>站点</label><select name="site"><option>north</option><option>south</option></select></div>
<div><label>原件</label><select name="returned_part">{part_opts}</select></div>
<div><label>条款（可空）</label><select name="term"><option value="">—</option>{term_opts}</select></div>
<div><label>动作</label><select name="action">{action_opts}</select></div>
<div><label>第 N 天</label><input name="day" type="number" min="0" value="{as_of}"></div>
<div><label>备注</label><input name="note"></div>
<div><button type="submit">登记</button></div></form>
<p class="small muted">动作受状态机约束：不能跳过调出直接确认交回；已确认调出的链
不可撤销，只能推进。日期不能倒填到链上已确认事实之前。</p></div>
<div class="card"><h2>登记备件复验放行</h2>
<form method="post" action="/register/approval" class="inline">
<input type="hidden" name="as_of" value="{as_of}">
<div><label>备件</label><select name="spare">{spare_opts}</select></div>
<div><label>站点</label><select name="site"><option>north</option><option>south</option></select></div>
<div><label>复验结论</label><select name="approval"><option value="approved">approved</option>
<option value="inspection_required">inspection_required</option></select></div>
<div><label>第 N 天</label><input name="day" type="number" min="0" value="{as_of}"></div>
<div><label>备注</label><input name="note"></div>
<div><button type="submit">登记</button></div></form></div>
<div class="card"><h2>未闭环链审计（时点 {as_of}）</h2>
<table><thead><tr><th>链</th><th>备件/站点</th><th>原件</th><th>条款</th>
<th>状态</th><th>确认性质</th><th>依据</th></tr></thead>
<tbody>{audit_rows}</tbody></table></div>"""


class Handler(BaseHTTPRequestHandler):
    state: AppState

    def log_message(self, fmt, *args):  # 安静日志
        pass

    @property
    def query(self) -> dict:
        # parse_qsl：保留方括号等字符，不把 a[b] 解析成嵌套键
        parsed = urllib.parse.parse_qsl(
            urllib.parse.urlparse(self.path).query, keep_blank_values=True
        )
        out: dict[str, list[str]] = {}
        for k, v in parsed:
            out.setdefault(k, []).append(v)
        return out

    def _send(self, body: bytes, status: int = 200,
              redirect: str | None = None) -> None:
        if redirect:
            self.send_response(303)
            self.send_header("Location", redirect)
            self.end_headers()
            return
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        conn = store.connect(self.state.db_path)
        try:
            as_of = _as_of(self, conn)
            flash = None
            if "flash" in self.query:
                kind = self.query.get("kind", ["ok"])[0]
                flash = (kind if kind in {"ok", "err"} else "ok",
                         self.query["flash"][0])
            if path == "/":
                body = render_overview(self, conn, as_of)
            elif path == "/parts":
                body = render_parts(self, conn, as_of)
            elif path == "/warranty":
                body = render_warranty(self, conn, as_of)
            elif path == "/demand":
                body = render_demand(self, conn, as_of)
            elif path == "/scenarios":
                body = render_scenarios(self, conn, as_of)
            elif path == "/explain":
                body = render_explain(self, conn, as_of)
            elif path == "/register":
                body = render_register(self, conn, as_of)
            else:
                body = page("未找到", '<div class="card">页面不存在</div>', as_of)
                self._send(body, 404)
                return
            self._send(page(path, body, as_of, flash=flash))
        finally:
            conn.close()

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        form = self._read_form()
        as_of = form.get("as_of", "")
        suffix = f"?as_of={urllib.parse.quote(as_of)}" if as_of else ""
        conn = store.connect(self.state.db_path)
        try:
            flash = ("ok", "操作完成")
            if path == "/register/event":
                try:
                    records.register_event(
                        conn, event=form["event"], spare=form["spare"],
                        site=form["site"], returned_part=form["returned_part"],
                        term=form.get("term") or None, action=form["action"],
                        day=int(form["day"]), note=form.get("note", ""),
                    )
                    flash = ("ok", f"已登记交接事件 {form['event']}")
                except records.RegistrationError as exc:
                    flash = ("err", str(exc))
            elif path == "/register/approval":
                try:
                    records.register_approval(
                        conn, spare=form["spare"], site=form["site"],
                        approval=form["approval"], known_day=int(form["day"]),
                        note=form.get("note", ""),
                    )
                    flash = ("ok", f"已登记复验放行 {form['spare']}@{form['site']}")
                except records.RegistrationError as exc:
                    flash = ("err", str(exc))
            elif path == "/scenarios/create":
                try:
                    spec = json.loads(form["spec"])
                    scenarios.save_scenario(
                        conn, spec["name"], int(spec["horizon_days"]),
                        float(spec["usage_multiplier"]), spec["terms"],
                        description=spec.get("description", ""),
                    )
                    flash = ("ok", f"已保存方案 {spec['name']}")
                except (KeyError, ValueError, json.JSONDecodeError) as exc:
                    flash = ("err", f"方案 JSON 不合法: {exc}")
            elif path == "/import":
                report = importer.import_sources(conn, "sources")
                if report.changed:
                    scenarios.baseline_from_sources(conn)
                flash = ("ok", "导入完成：" + ", ".join(f"{k} {v} 行" for k, v in report.imported.items())
                         or "来源未变化")
            else:
                flash = ("err", "未知操作")
            # 闪存消息通过查询参数带回（本地单用户应用足够）
            sep = "&" if suffix else "?"
            if path == "/import":
                target = "/"
            elif path.startswith("/register"):
                target = "/register"
            else:
                target = "/scenarios"
            redirect = (
                f"{target}{suffix}{sep}flash="
                f"{urllib.parse.quote(flash[1])}&kind={flash[0]}"
            )
            self._send(b"", redirect=redirect)
        finally:
            conn.close()


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8000) -> None:
    handler = Handler
    handler.state = AppState(db_path)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"本地应用已启动：http://{host}:{port}/ （Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
