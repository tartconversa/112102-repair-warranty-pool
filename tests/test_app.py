"""应用层测试：使用临时目录中的数据库副本，不触碰仓库内 sources/。"""
import csv
import shutil
import tempfile
import unittest
from pathlib import Path

from app import clock, explain, importer, records, risk, scenarios, store, warranty

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "sources"


class AppTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.sources_dir = self.dir / "sources"
        shutil.copytree(SOURCES, self.sources_dir)
        self.db = self.dir / "app.db"
        self.conn = store.connect(self.db)
        self.report = importer.import_sources(self.conn, self.sources_dir)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def view(self, as_of: int) -> clock.KnowledgeView:
        return clock.build_view(self.conn, as_of)

    def records(self, as_of: int):
        return risk.classify_parts(self.view(as_of))


class ImportTests(AppTestBase):
    def test_idempotent_reimport(self):
        again = importer.import_sources(self.conn, self.sources_dir)
        self.assertEqual(again.imported, {})
        self.assertEqual(set(again.skipped_unchanged), set(importer.SOURCE_FILES))
        # 强制重导后行数不变
        forced = importer.import_sources(self.conn, self.sources_dir, force=True)
        self.assertEqual(forced.imported["service_intervals"], 42)

    def test_lot_blank_warning(self):
        self.assertTrue(
            any("P21" in w for w in self.report.warnings)
        )

    def test_bad_header_rejected_and_rolled_back(self):
        target = self.sources_dir / "checks.csv"
        rows = list(csv.DictReader(target.open(encoding="utf-8")))
        with target.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["part", "inspection_day",
                                                    "result", "WRONG"])
            writer.writeheader()
            for r in rows[:5]:
                writer.writerow({"part": r["part"],
                                 "inspection_day": r["inspection_day"],
                                 "result": r["result"], "WRONG": r["known_day"]})
        with self.assertRaises(importer.ImportError_):
            importer.import_sources(self.conn, self.sources_dir, force=True)
        # 回滚后旧数据仍可查询
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM checks").fetchone()[0],
            len(rows),
        )

    def test_dangling_reference_rejected(self):
        target = self.sources_dir / "checks.csv"
        with target.open("a", encoding="utf-8", newline="") as fh:
            fh.write("P999,7,clear,7\n")
        with self.assertRaises(importer.ImportError_):
            importer.import_sources(self.conn, self.sources_dir, force=True)


class ClockTests(AppTestBase):
    def test_lot_correction_only_visible_after_known_day(self):
        early = self.view(47).lots["P21"]
        self.assertIsNone(early.lot)
        self.assertEqual(early.source, "unattributed")
        later = self.view(48).lots["P21"]
        self.assertEqual(later.lot, "L1")
        self.assertEqual(later.source, "correction")

    def test_intervals_truncate_before_observed_to(self):
        # observed_to=42 的窗自第 1 天起逐步可见，但第 41 天尚未完整
        view41 = self.view(41)
        self.assertIn("P1", view41.intervals)
        self.assertFalse(view41.intervals["P1"].is_complete)
        # 未完整窗的结束原因不可知，按在役处理
        recs = {r.part: r for r in risk.classify_parts(view41)}
        self.assertEqual(recs["P1"].status, "in_service")
        view42 = self.view(42)
        self.assertTrue(view42.intervals["P1"].is_complete)

    def test_usage_proration(self):
        iv = self.view(55).intervals["P1"]  # 85h over days 1..42
        self.assertAlmostEqual(iv.usage_at(42), 85.0)
        self.assertEqual(iv.usage_at(1), 0.0)
        self.assertAlmostEqual(iv.usage_at(22), 85 * 21 / 41, places=4)


class RiskTests(AppTestBase):
    def test_all_events_are_l1_and_interval_censored(self):
        recs = {r.part: r for r in self.records(55)}
        events = [r for r in recs.values() if r.is_event]
        self.assertEqual(len(events), 10)
        self.assertTrue({r.lot for r in events} <= {"L1"})
        for r in events:
            self.assertIsNotNone(r.event_hours_lower)
            self.assertLessEqual(r.event_hours_lower, r.event_hours_upper)

    def test_status_buckets(self):
        counts = risk.status_counts(self.records(55))
        self.assertEqual(counts["interval_failure"], 10)
        self.assertEqual(counts["in_service"], 19)
        self.assertEqual(counts["planned"], 7)
        self.assertEqual(counts["transfer"], 6)  # P21 补录后从不能归属转为换站
        self.assertEqual(counts["unattributed"], 0)

    def test_unattributed_before_correction(self):
        recs = {r.part: r for r in self.records(47)}
        self.assertEqual(recs["P21"].status, "unattributed")
        self.assertIsNone(recs["P21"].lot)
        # 不能归属件不进入 L1 分层
        l1 = next(r for r in risk.strata_rates(self.records(47), ("lot",))
                  if r.key == ("L1",))
        self.assertEqual(l1.parts, 13)

    def test_withdrawn_without_evidence_is_unattributed(self):
        # 构造一个已停用但无故障结论的件
        self.conn.execute(
            "UPDATE checks SET result='clear' WHERE part='P3' AND result='failed'"
        )
        recs = {r.part: r for r in risk.classify_parts(self.view(55))}
        self.assertEqual(recs["P3"].status, "unattributed")
        self.assertFalse(recs["P3"].is_event)

    def test_no_fabricated_event_time_from_repair_order(self):
        # 事件时间只来自 failed 检查时刻（区间上沿），区间下沿来自上一次
        # clear 检查；送修/停用顺序不参与构造，且不产生单点伪精度
        recs = {r.part: r for r in self.records(55)}
        p12 = recs["P12"]  # 第 14 天 clear、第 21 天 failed
        iv = self.view(55).intervals["P12"]
        self.assertEqual(p12.event_hours_upper, iv.usage_at(21))
        self.assertEqual(p12.event_hours_lower, iv.usage_at(14))
        self.assertLess(p12.event_hours_lower, p12.event_hours_upper)
        # 没有故障证据的停用件不得产生事件小时
        for r in recs.values():
            if not r.is_event:
                self.assertIsNone(r.event_hours_upper)

    def test_l2_l3_zero_event_rate_with_upper_ci(self):
        rows = {r.key[0]: r for r in
                risk.strata_rates(self.records(55), ("lot",))}
        self.assertEqual(rows["L2"].events, 0)
        self.assertEqual(rows["L2"].rate_per_1000h, 0.0)
        self.assertGreater(rows["L2"].ci_high, 0.0)

    def test_inspection_coverage_shows_site_intensity_gap(self):
        cov = {r["site"]: r
               for r in risk.inspection_coverage(self.view(55), self.records(55))}
        self.assertLess(cov["north"]["mean_interval_days"],
                        cov["south"]["mean_interval_days"])

    def test_km_survival_between_0_and_1(self):
        table = risk.kaplan_meier(self.records(55))
        self.assertTrue(all(0 <= p["survival"] <= 1 for p in table))
        self.assertLess(table[-1]["survival"], 1.0)

    def test_demand_responds_to_usage_multiplier(self):
        low = risk.project_demand(self.records(55), self.view(55), 28, 0.5)
        high = risk.project_demand(self.records(55), self.view(55), 28, 2.0)
        self.assertLess(
            sum(p.expected_failures for p in low),
            sum(p.expected_failures for p in high),
        )

    def test_transfer_parts_remain_in_fleet_demand(self):
        proj = {(p.site, p.lot): p
                for p in risk.project_demand(self.records(55), self.view(55), 28, 1.0)}
        north_l1 = proj[("north", "L1")]
        self.assertGreaterEqual(north_l1.transferred_parts, 1)
        self.assertGreater(north_l1.future_exposure_hours, 0.0)


class LateEvidenceTests(AppTestBase):
    def _make_late_check(self):
        # 把 P3 的 failed 结论改为第 50 天才可知
        self.conn.execute(
            "UPDATE checks SET known_day=50 WHERE part='P3' AND result='failed'"
        )

    def test_late_failure_hidden_before_known_day(self):
        self._make_late_check()
        recs47 = {r.part: r for r in risk.classify_parts(self.view(47))}
        self.assertNotEqual(recs47["P3"].status, "interval_failure")
        recs50 = {r.part: r for r in risk.classify_parts(self.view(50))}
        self.assertEqual(recs50["P3"].status, "interval_failure")
        self.assertTrue(recs50["P3"].failure_late)

    def test_late_check_warning_on_import(self):
        path = self.sources_dir / "checks.csv"
        text = path.read_text(encoding="utf-8").replace(
            "P3,42,failed,42", "P3,42,failed,50")
        path.write_text(text, encoding="utf-8")
        report = importer.import_sources(self.conn, self.sources_dir, force=True)
        self.assertTrue(any("补交" in w for w in report.warnings))


class WarrantyTests(AppTestBase):
    def test_p12_chain_overdue_and_uncovered(self):
        chains = {c.chain_id: c for c in
                  warranty.build_chains(self.view(55))}
        c1 = next(c for c in chains.values()
                  if c.returned_part == "P12" and not c.is_open)
        self.assertEqual(c1.eligibility, "overdue_cost")
        self.assertEqual(c1.days_used, 10)
        self.assertEqual(c1.cost_exposure, 220.0)
        self.assertTrue(c1.confirmed_locked)

    def test_p21_attribution_flips_chain_eligibility(self):
        before = next(c for c in warranty.build_chains(self.view(47))
                      if c.returned_part == "P21")
        self.assertEqual(before.eligibility, "lot_unattributed")
        after = next(c for c in warranty.build_chains(self.view(55))
                     if c.returned_part == "P21")
        self.assertEqual(after.eligibility, "pending")  # 补录后覆盖匹配，待闭环

    def test_advance_limit_counts_only_open_chains(self):
        positions = {p.term["term"]: p
                     for p in warranty.term_positions(
                         self.view(47), warranty.build_chains(self.view(47)))}
        # 第 47 天 W3 有两条未闭环链（C1 调出、C2 待复验），超过额度 1
        self.assertTrue(positions["W3"].over_limit)
        self.assertEqual(positions["W3"].advance_used, 2)
        # 第 55 天 C1 闭环释放额度
        positions55 = {p.term["term"]: p
                       for p in warranty.term_positions(
                           self.view(55), warranty.build_chains(self.view(55)))}
        self.assertFalse(positions55["W3"].over_limit)
        self.assertEqual(positions55["W3"].advance_used, 1)

    def test_spare_pool_blocks_unapproved_and_committed(self):
        spares = {(s.spare, s.site): s for s in
                  warranty.spare_positions(self.view(47),
                                           warranty.build_chains(self.view(47)))}
        self.assertFalse(spares[("S2", "north")].deliverable)  # 待复验
        self.assertFalse(spares[("S1", "north")].deliverable)  # 实物被 C1 占用

    def test_local_approval_unblocks_after_reinspection(self):
        records.register_approval(
            self.conn, spare="S2", site="north", approval="approved",
            known_day=50, note="复验合格")
        spares = {(s.spare, s.site): s for s in
                  warranty.spare_positions(self.view(55),
                                           warranty.build_chains(self.view(55)))}
        # 许可已放行，但实物仍被 C2 占用
        self.assertEqual(spares[("S2", "north")].approval, "approved")
        self.assertFalse(spares[("S2", "north")].deliverable)
        self.assertTrue(any("占用" in b for b in spares[("S2", "north")].blockers))

    def test_confirmed_dispatch_is_locked_against_refresh(self):
        c1 = next(c for c in warranty.build_chains(self.view(55))
                  if c.returned_part == "P12")
        self.assertTrue(c1.confirmed_locked)
        # 即便补录凭证变化，已确认事实仍在（用更早日切视图确认事件不消失）
        self.assertTrue(
            any(e.action == "dispatched"
                for e in self.view(55).events
                if e.spare == "S1")
        )

    def test_dispatched_chain_mismatch_flagged_while_open(self):
        c2 = next(c for c in warranty.build_chains(self.view(47))
                  if c.returned_part == "P18")
        self.assertEqual(c2.eligibility, "lot_uncovered")
        self.assertTrue(c2.confirmed_locked)


class RegistrationTests(AppTestBase):
    def test_state_machine_prevents_skipping_dispatch(self):
        with self.assertRaises(records.RegistrationError):
            records.register_event(
                self.conn, event="Z9", spare="S3", site="north",
                returned_part="P21", term="W1", action="return_confirmed", day=56)

    def test_valid_chain_progression(self):
        records.register_event(
            self.conn, event="Z6", spare="S3", site="north",
            returned_part="P21", term="W1", action="dispatched", day=56)
        records.register_event(
            self.conn, event="Z7", spare="S3", site="north",
            returned_part="P21", term="W1", action="return_confirmed", day=60)
        chain = next(c for c in warranty.build_chains(self.view(60))
                     if c.returned_part == "P21")
        self.assertEqual(chain.state, "return_confirmed")
        self.assertEqual(chain.eligibility, "eligible")  # 5 天内交回、覆盖匹配

    def test_backdating_rejected(self):
        records.register_event(
            self.conn, event="Z6", spare="S3", site="north",
            returned_part="P21", term="W1", action="dispatched", day=56)
        with self.assertRaises(records.RegistrationError):
            records.register_event(
                self.conn, event="Z7", spare="S3", site="north",
                returned_part="P21", term="W1",
                action="awaiting_inspection", day=55)

    def test_local_records_survive_reimport(self):
        records.register_event(
            self.conn, event="Z6", spare="S3", site="north",
            returned_part="P21", term="W1", action="dispatched", day=56)
        importer.import_sources(self.conn, self.sources_dir, force=True)
        self.assertTrue(
            self.conn.execute(
                "SELECT 1 FROM local_events WHERE event='Z6'").fetchone()
        )


class ScenarioTests(AppTestBase):
    def setUp(self):
        super().setUp()
        self.baseline = scenarios.baseline_from_sources(self.conn)

    def test_baseline_build_and_persistence(self):
        names = [s["name"] for s in scenarios.list_scenarios(self.conn)]
        self.assertIn(self.baseline, names)

    def test_scenario_with_l1_cover_reduces_uncovered(self):
        terms = [dict(r) for r in self.conn.execute(
            "SELECT term, vendor, lot, supported_site, return_days, "
            "unit_cost, advance_limit FROM warranty_terms").fetchall()]
        terms += [
            {"term": "W4", "vendor": "V2", "lot": "L1", "supported_site": "north",
             "return_days": 7, "unit_cost": 200, "advance_limit": 2},
            {"term": "W5", "vendor": "V2", "lot": "L1", "supported_site": "south",
             "return_days": 7, "unit_cost": 200, "advance_limit": 2},
        ]
        scenarios.save_scenario(
            self.conn, "加保方案", 28, 1.0, terms, description="test")
        view = self.view(55)
        recs = risk.classify_parts(view)
        base = scenarios.evaluate(self.conn, view, recs, self.baseline)
        new = scenarios.evaluate(self.conn, view, recs, "加保方案")
        self.assertGreater(
            base.totals["uncovered_failures"],
            new.totals["uncovered_failures"],
        )
        self.assertGreater(
            new.totals["covered_failures"],
            base.totals["covered_failures"],
        )

    def test_limit_flag_when_demand_exceeds_advance(self):
        terms = [{
            "term": "WX", "vendor": "V1", "lot": "L1",
            "supported_site": "north", "return_days": 30,
            "unit_cost": 100, "advance_limit": 0,
        }]
        scenarios.save_scenario(self.conn, "零额度", 28, 3.0, terms)
        view = self.view(55)
        result = scenarios.evaluate(
            self.conn, view, risk.classify_parts(view), "零额度")
        self.assertTrue(
            any("先行额度" in f for f in result.limit_flags))


class ExplainTests(AppTestBase):
    def test_diff_traces_correction_and_chain_flip(self):
        diff = explain.diff_as_of(self.conn, 47, 55)
        kinds = {e.kind for e in diff.new_evidence}
        self.assertIn("实物批号补录", kinds)
        self.assertIn("替换交接", kinds)
        changed_parts = {c["part"]: c for c in diff.part_changes}
        self.assertIn("P21", changed_parts)
        chain_terms = [c["chain"] for c in diff.chain_changes]
        self.assertTrue(chain_terms)
        # 必须给出仍可采取的措施
        self.assertTrue(diff.actions_now)

    def test_diff_empty_when_no_news(self):
        diff = explain.diff_as_of(self.conn, 42, 42)
        self.assertEqual(diff.new_evidence, [])


if __name__ == "__main__":
    unittest.main()
