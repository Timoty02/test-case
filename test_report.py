"""Тесты: python -m unittest -v"""

import tempfile
import unittest
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import report

DATA = Path(__file__).resolve().parent / "data"

C_HEAD = "contract_id,contractor,amount,signed_at\n"
W_HEAD = "work_id,contract_id,title,price,status,completed_at,cancelled_at\n"
P_HEAD = "payment_id,contract_id,work_id,purpose,amount,status,created_at,paid_at,refunded_at\n"


def make(contracts: str, works: str, payments: str) -> report.Dataset:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "contracts.csv").write_text(C_HEAD + contracts, encoding="utf-8")
    (tmp / "works.csv").write_text(W_HEAD + works, encoding="utf-8")
    (tmp / "payments.csv").write_text(P_HEAD + payments, encoding="utf-8")
    return report.load(tmp)


def by_contract(ds, on):
    return {r.contract.contract_id: r for r in report.build_report(ds, on)}


def work_paid(rep):
    return {l.work.work_id: l.paid for l in rep.works}


class RealDataOn1011(unittest.TestCase):
    """Отчёт на 10.11.2026 по данным из data/."""

    @classmethod
    def setUpClass(cls):
        cls.r = by_contract(report.load(DATA), date(2026, 11, 10))

    def test_d1_works(self):
        rep = self.r["D-1"]
        self.assertEqual(work_paid(rep), {"W-11": D("100000.00"), "W-12": D("100000.00"),
                                          "W-13": D("0.00"), "W-14": D("100000.00"),
                                          "W-15": D("15000.00"), "W-16": D("0.00")})
        self.assertEqual({l.work.work_id: l.state for l in rep.works}["W-13"], "cancelled")

    def test_d1_totals(self):
        rep = self.r["D-1"]
        self.assertEqual(rep.paid, D("315000.00"))       # P-1 + P-3, P-2 возвращён
        self.assertEqual(rep.queued, D("150000.00"))     # P-4
        self.assertEqual(rep.due, D("285000.00"))        # от суммы договора
        self.assertEqual(rep.due_by_works, D("185000.00"))
        self.assertEqual(rep.unallocated, D("0.00"))

    def test_d2(self):
        rep = self.r["D-2"]
        # W-23 принята 11.11 — на 10.11 ещё запланирована; P-8 (12.11) не учитывается.
        self.assertEqual({l.work.work_id: l.state for l in rep.works}["W-23"], "planned")
        self.assertEqual(rep.paid, D("75000.00"))
        self.assertEqual(rep.queued, D("0.00"))
        self.assertEqual(rep.due, D("15000.00"))
        self.assertEqual(work_paid(rep)["W-23"], D("25000.00"))

    def test_paid_sums_reconcile(self):
        for rep in self.r.values():
            self.assertEqual(sum(work_paid(rep).values()) + rep.unallocated, rep.paid)


class Dates(unittest.TestCase):
    def setUp(self):
        self.ds = report.load(DATA)

    def test_refund_window_counts_as_paid(self):
        rep = by_contract(self.ds, date(2026, 9, 5))["D-1"]
        self.assertEqual(rep.paid, D("350000.00"))
        rep = by_contract(self.ds, date(2026, 9, 10))["D-1"]
        self.assertEqual(rep.paid, D("300000.00"))

    def test_overpayment_after_p8(self):
        rep = by_contract(self.ds, date(2026, 11, 12))["D-2"]
        self.assertEqual(rep.paid, D("95000.00"))
        self.assertEqual(rep.unallocated, D("5000.00"))
        self.assertEqual(rep.due, D("-5000.00"))
        self.assertTrue(any("ПЕРЕПЛАТА" in w for w in rep.warnings))

    def test_before_everything(self):
        rep = by_contract(self.ds, date(2026, 1, 1))["D-1"]
        self.assertEqual((rep.paid, rep.queued), (D("0.00"), D("0.00")))

    def test_cancel_after_report_date_is_planned(self):
        rep = by_contract(self.ds, date(2026, 10, 11))["D-1"]
        self.assertEqual({l.work.work_id: l.state for l in rep.works}["W-13"], "planned")


class Allocation(unittest.TestCase):
    def test_done_works_by_completion_date_first(self):
        ds = make("C,X,300.00,2026-01-01\n",
                  "A,C,a,100.00,planned,,\n"
                  "B,C,b,100.00,done,2026-02-10,\n"
                  "Z,C,z,100.00,done,2026-02-01,\n",
                  "P1,C,,adv,150.00,paid,2026-01-02,2026-01-02,\n")
        rep = by_contract(ds, date(2026, 3, 1))["C"]
        self.assertEqual(work_paid(rep), {"A": D("0.00"), "B": D("50.00"), "Z": D("100.00")})

    def test_linked_to_cancelled_goes_to_pool(self):
        ds = make("C,X,200.00,2026-01-01\n",
                  "A,C,a,100.00,cancelled,,2026-01-05\n"
                  "B,C,b,100.00,done,2026-01-10,\n",
                  "P1,C,A,x,60.00,paid,2026-01-02,2026-01-02,\n")
        rep = by_contract(ds, date(2026, 2, 1))["C"]
        self.assertEqual(work_paid(rep), {"A": D("0.00"), "B": D("60.00")})
        self.assertTrue(any("отменена" in w for w in rep.warnings))

    def test_linked_excess_goes_to_pool(self):
        ds = make("C,X,200.00,2026-01-01\n",
                  "A,C,a,100.00,done,2026-01-05,\n"
                  "B,C,b,100.00,planned,,\n",
                  "P1,C,A,x,130.00,paid,2026-01-02,2026-01-02,\n")
        rep = by_contract(ds, date(2026, 2, 1))["C"]
        self.assertEqual(work_paid(rep), {"A": D("100.00"), "B": D("30.00")})

    def test_kopecks_exact(self):
        ds = make("C,X,0.30,2026-01-01\n",
                  "A,C,a,0.10,done,2026-01-05,\nB,C,b,0.20,done,2026-01-06,\n",
                  "P1,C,,x,0.10,paid,2026-01-02,2026-01-02,\n"
                  "P2,C,,x,0.20,paid,2026-01-02,2026-01-02,\n")
        rep = by_contract(ds, date(2026, 2, 1))["C"]
        self.assertEqual(rep.paid, D("0.30"))
        self.assertEqual(rep.due, D("0.00"))
        self.assertEqual(work_paid(rep), {"A": D("0.10"), "B": D("0.20")})

    def test_queued_linked_shown_on_work(self):
        ds = make("C,X,100.00,2026-01-01\n", "A,C,a,100.00,planned,,\n",
                  "P1,C,A,x,40.00,queued,2026-01-02,,\n")
        rep = by_contract(ds, date(2026, 2, 1))["C"]
        self.assertEqual(rep.works[0].queued, D("40.00"))
        self.assertEqual(rep.paid, D("0.00"))


class Validation(unittest.TestCase):
    def test_three_decimals_rejected(self):
        with self.assertRaises(report.DataError):
            make("C,X,1.001,2026-01-01\n", "", "")

    def test_work_from_other_contract(self):
        with self.assertRaises(report.DataError):
            make("C,X,1.00,2026-01-01\nE,Y,1.00,2026-01-01\n", "A,C,a,1.00,planned,,\n",
                 "P1,E,A,x,1.00,paid,2026-01-02,2026-01-02,\n")

    def test_unknown_contract(self):
        with self.assertRaises(report.DataError):
            make("C,X,1.00,2026-01-01\n", "A,Q,a,1.00,planned,,\n", "")

    def test_fmt(self):
        self.assertEqual(report.fmt(D("1234567.05")), "1 234 567,05")
        self.assertEqual(report.fmt(D("-5000.00")), "-5 000,00")


if __name__ == "__main__":
    unittest.main()
