"""应用层业务测试：份额守恒、并发请求、边界与更正。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from claimapp import emissions, ledger, portfolio, reports, statements
from claimapp.records import DataError, load_dataset
from claimapp.store import LedgerStore

ROOT = Path(__file__).resolve().parents[1]


class Base(unittest.TestCase):
    def setUp(self):
        self.data = load_dataset(ROOT)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def store(self, name="ledger.json"):
        return LedgerStore(Path(self.folder.name) / name)


class ShareTests(Base):
    def test_reserved_and_issued_cannot_exceed_capacity(self):
        store = self.store()
        store.reserve("K1", "U9", "O1-1", "B1", 8, "2025-02-02T08:00:00+08:00", self.data)
        with self.assertRaisesRegex(DataError, "超出可承诺量"):
            store.reserve("K2", "U10", "O1-1", "B1", 1, "2025-02-02T09:00:00+08:00", self.data)

    def test_capacity_is_per_output_and_unit(self):
        store = self.store()
        # O1-1 只剩 8 MWh；O1-2 的 182 t 不能拿来抵。
        store.reserve("K1", "U9", "O1-1", "B1", 8, "2025-02-02T08:00:00+08:00", self.data)
        with self.assertRaisesRegex(DataError, "超出可承诺量"):
            store.reserve("K2", "U10", "O1-1", "B1", 0.5, "2025-02-02T09:00:00+08:00", self.data)
        other = store.reserve("K3", "U11", "O1-2", "B1", 100, "2025-02-02T09:00:00+08:00", self.data)
        self.assertEqual(other["position"]["available"], "82")

    def test_release_only_unissued(self):
        store = self.store()
        store.reserve("K1", "U9", "O5-1", "B1", 10, "2025-02-02T08:00:00+08:00", self.data)
        result = store.release("K1", 4, "2025-02-03T08:00:00+08:00", self.data)
        self.assertEqual(result["released"], "4")
        self.assertEqual(result["after"]["available"], "54")
        with self.assertRaisesRegex(DataError, "已签发"):
            ledger.release(self.data, "C1", 1)

    def test_release_cannot_exceed_held(self):
        store = self.store()
        store.reserve("K1", "U9", "O5-1", "B1", 10, "2025-02-02T08:00:00+08:00", self.data)
        with self.assertRaisesRegex(DataError, "超过"):
            store.release("K1", 11, "2025-02-03T08:00:00+08:00", self.data)

    def test_issue_keeps_snapshot(self):
        store = self.store()
        store.reserve("K1", "U9", "O5-1", "B1", 10, "2025-02-02T08:00:00+08:00", self.data)
        result = store.issue("K1", "2025-02-03T08:00:00+08:00", self.data)
        self.assertEqual(result["statement"]["kind"], "issued")
        self.assertEqual(
            result["statement"]["evidence"]["vintage"], "2025-02-01T08:00:00+08:00"
        )
        saved = store.issue.__self__.state["claims"][0]
        self.assertEqual(saved["issued_at"], "2025-02-03T08:00:00+08:00")
        with self.assertRaisesRegex(DataError, "已签发"):
            store.release("K1", 1, "2025-02-04T08:00:00+08:00", self.data)


class ConcurrencyTests(Base):
    def test_concurrent_requests_do_not_both_get_everything(self):
        result = ledger.settle_incoming(self.data)
        granted = {row["request_id"]: int(row["granted"]) for row in result["requests"]}
        self.assertEqual(granted, {"Q1": 4, "Q2": 4})
        self.assertEqual(result["remaining_by_output"]["O1-1"], "0")

    def test_batch_never_exceeds_remaining(self):
        result = ledger.settle_incoming(self.data)
        self.assertLessEqual(sum(int(r["granted"]) for r in result["requests"]), 8)

    def test_simultaneous_requests_with_large_remainder(self):
        # O5-1 尚无占用：两个 100 的并发请求只能拿到 30。
        from dataclasses import replace
        from claimapp.records import IncomingRequest
        data = replace(
            self.data,
            incoming=(
                IncomingRequest("Z1", "O5-1", 100, "2025-02-02T08:00:00+08:00"),
                IncomingRequest("Z2", "O5-1", 100, "2025-02-02T08:00:00+08:00"),
            ),
        )
        result = ledger.settle_incoming(data)
        self.assertEqual([r["granted"] for r in result["requests"]], ["30", "30"])
        self.assertEqual(result["remaining_by_output"]["O5-1"], "0")

    def test_small_request_fulfilled_first_then_rest_split(self):
        from dataclasses import replace
        from claimapp.records import IncomingRequest
        data = replace(
            self.data,
            incoming=(
                IncomingRequest("Z1", "O5-1", 10, "2025-02-02T08:00:00+08:00"),
                IncomingRequest("Z2", "O5-1", 100, "2025-02-02T08:00:00+08:00"),
                IncomingRequest("Z3", "O5-1", 100, "2025-02-02T08:00:00+08:00"),
            ),
        )
        result = ledger.settle_incoming(data)
        granted = {r["request_id"]: r["granted"] for r in result["requests"]}
        self.assertEqual(granted, {"Z1": "10", "Z2": "25", "Z3": "25"})


class EmissionsTests(Base):
    def test_baseline_range_is_identified_not_fixed(self):
        extremes = emissions.baseline_extremes(self.data, "L1")
        self.assertLess(extremes["gross_min"], extremes["gross_max"])
        self.assertGreater(extremes["gross_min"], 0)
        self.assertNotEqual(extremes["gross_min_shares"], extremes["gross_max_shares"])

    def test_no_default_to_full_open_burning(self):
        extremes = emissions.baseline_extremes(self.data, "L1")
        self.assertGreater(extremes["gross_min"] / extremes["dry_t"], 50)
        self.assertNotIn(extremes["gross_min_shares"].get("decay"), (1.0,))

    def test_correction_changes_range(self):
        before = emissions.baseline_extremes(self.data, "L1", "2025-01-31T08:00:00+08:00")
        after = emissions.baseline_extremes(self.data, "L1")
        self.assertNotEqual(before["gross_min"], after["gross_min"])
        self.assertTrue(all(c.correction_ids == () for c in before["components"]))

    def test_boundary_units_and_weights(self):
        b1 = self.data.boundary("B1")
        b2 = self.data.boundary("B2")
        one = emissions.output_range(self.data, "O1-1", b1)
        one_v = emissions.output_range(self.data, "O1-1", b2)
        self.assertEqual(one["allocation_weight"], 0.4)
        self.assertEqual(one_v["allocation_weight"], 0.55)
        self.assertGreater(one_v["allocation_weight"], one["allocation_weight"])
        self.assertTrue(b2.include_biogenic)
        self.assertGreater(one_v["direct_kg"]["max"], one["direct_kg"]["max"])

    def test_system_boundary_uses_real_quantities(self):
        system = emissions.output_range(self.data, "O1-1", self.data.boundary("B3"))
        total = emissions.lot_total(self.data, "L1", self.data.boundary("B3"))
        self.assertIsNone(system["allocation_weight"])
        parts = sum(p["total_kg"]["max"] for p in total["outputs"])
        self.assertAlmostEqual(parts, total["total_kg"]["max"], places=6)

    def test_boundaries_not_comparable(self):
        b1 = self.data.boundary("B1")
        b3 = self.data.boundary("B3")
        verdict = emissions.comparability(
            b1, b3, emissions.evidence_label(self.data), emissions.evidence_label(self.data)
        )
        self.assertFalse(verdict["comparable"])
        self.assertTrue(any("时间范围" in r for r in verdict["reasons"]))
        self.assertTrue(any("分配约定" in r for r in verdict["reasons"]))

    def test_joint_uncertainty_is_not_sum_of_favourable_ends(self):
        result = portfolio.portfolio(self.data)
        self.assertTrue(result["by_boundary"])
        for row in result["by_boundary"]:
            self.assertGreaterEqual(row["joint_feasible_kg"]["min"], row["naive_sum_of_favourable_ends_kg"]["min"])
            self.assertLessEqual(row["joint_feasible_kg"]["max"], row["naive_sum_of_favourable_ends_kg"]["max"])

    def test_shared_group_factors_move_together(self):
        # L1、L2、L3 同属 G1：电网因子端点必须一致，不能分别取端。
        pieces = [
            __import__("claimapp.portfolio", fromlist=["_piece"])._piece(self.data, c, None)
            for c in self.data.claims
        ]
        l1 = next(p for p in pieces if p.lot_id == "L1")
        l2 = next(p for p in pieces if p.lot_id == "L2")
        self.assertEqual(l1.group, l2.group)


class CorrectionTests(Base):
    def test_issued_statement_never_rewritten(self):
        before = statements.statement_for(self.data, "C1")
        entries = statements.evolving_statements(self.data)
        c1 = next(e for e in entries if e["claim_id"] == "C1")
        after = statements.statement_for(self.data, "C1")
        self.assertEqual(before.calculation, after.calculation)
        self.assertEqual(c1["original_total_kg"], before.calculation["total_kg"])
        self.assertEqual(c1["assessment"], "affected")
        self.assertEqual(c1["required_action"], "supplement")

    def test_correction_lists_customers_and_deltas(self):
        trace = statements.trace_correction(self.data, "X1")
        self.assertEqual(trace["customers"], ["U1", "U2"])
        for item in trace["impact"]["affected_claims"]:
            self.assertIn("delta_kg", item)
            self.assertNotEqual(item["action"]["kind"], "none")

    def test_reservations_reassessed_on_correction(self):
        impact = statements.correction_impact(self.data, "X1")
        self.assertEqual([r["claim_id"] for r in impact["reservations_to_reassess"]], ["C2"])

    def test_unaffected_lot_keeps_range(self):
        before = emissions.baseline_extremes(self.data, "L2", "2025-01-31T08:00:00+08:00")
        after = emissions.baseline_extremes(self.data, "L2")
        self.assertEqual(before["gross_min"], after["gross_min"])

    def test_statement_available_throughout_chain(self):
        trace = statements.trace_statement(self.data, "C2")
        self.assertEqual(trace["feedstock"]["lot_id"], "L1")
        self.assertEqual(trace["output"]["output_id"], "O1-1")
        self.assertEqual(trace["boundary"]["allocation"], "value")
        self.assertEqual(trace["statement"]["share_basis"]["evidence"]["corrections_applied"], [])
        self.assertIn("X1", [c["correction_id"] for c in trace["related_corrections"]])


class ReportTests(Base):
    def test_lot_view_reports_unallocated(self):
        view = reports.lot_view(self.data, "L1")
        quantities = {u["output_id"]: u["available"] for u in view["unallocated"]}
        self.assertEqual(quantities["O1-1"], "8")
        self.assertEqual(quantities["O1-2"], "182")

    def test_inventory_flags_no_overcommitment(self):
        report = reports.inventory(self.data)
        self.assertEqual(report["overcommitments"], [])
        self.assertTrue(report["statements"])

    def test_cli_check_ok(self):
        from claimapp.__main__ import main
        self.assertEqual(main(["check", "--root", str(ROOT)]), 0)

    def test_ledger_round_trip(self):
        path = Path(self.folder.name) / "claims.json"
        from claimapp.__main__ import main
        self.assertEqual(
            main(
                [
                    "reserve", "--root", str(ROOT), "--ledger", str(path),
                    "--claim", "K9", "--customer", "U9", "--output", "O6-1",
                    "--boundary", "B1", "--quantity", "5",
                    "--at", "2025-03-01T08:00:00+08:00",
                ]
            ),
            0,
        )
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["claims"][0]["claim_id"], "K9")
        self.assertEqual(saved["claims"][0]["status"], "reserved")
        self.assertEqual(
            main(
                [
                    "issue", "--root", str(ROOT), "--ledger", str(path),
                    "--claim", "K9", "--at", "2025-03-02T08:00:00+08:00",
                ]
            ),
            0,
        )
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["claims"][0]["status"], "issued")


if __name__ == "__main__":
    unittest.main()
