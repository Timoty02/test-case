"""Отчёт по оплате работ и договоров на дату.

Запуск:
    python report.py --date 2026-11-10
    python report.py --date 2026-11-10 --data path/to/data

Решения, лежащие в основе расчёта, описаны в DECISIONS.md.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

ZERO = Decimal("0.00")


class DataError(Exception):
    """Ошибка во входных данных."""


# ---------------------------------------------------------------- модели

@dataclass(frozen=True)
class Contract:
    contract_id: str
    contractor: str
    amount: Decimal
    signed_at: date | None


@dataclass(frozen=True)
class Work:
    work_id: str
    contract_id: str
    title: str
    price: Decimal
    completed_at: date | None
    cancelled_at: date | None

    def state_on(self, on: date) -> str:
        if self.cancelled_at is not None and self.cancelled_at <= on:
            return "cancelled"
        if self.completed_at is not None and self.completed_at <= on:
            return "done"
        return "planned"


@dataclass(frozen=True)
class Payment:
    payment_id: str
    contract_id: str
    work_id: str | None
    purpose: str
    amount: Decimal
    status: str
    created_at: date | None
    paid_at: date | None
    refunded_at: date | None

    def state_on(self, on: date) -> str:
        """paid | refunded | queued | absent — состояние платежа на дату."""
        if self.paid_at is not None and self.paid_at <= on:
            if self.refunded_at is not None and self.refunded_at <= on:
                return "refunded"
            return "paid"
        if self.created_at is not None and self.created_at <= on:
            return "queued"
        return "absent"


# ---------------------------------------------------------------- чтение

def parse_money(value: str, where: str) -> Decimal:
    try:
        amount = Decimal(value.strip())
    except InvalidOperation:
        raise DataError(f"{where}: не число: {value!r}") from None
    if not amount.is_finite():
        raise DataError(f"{where}: не число: {value!r}")
    if amount.as_tuple().exponent < -2:
        raise DataError(f"{where}: больше двух знаков после точки: {value!r}")
    if amount < 0:
        raise DataError(f"{where}: отрицательная сумма: {value!r}")
    return amount.quantize(Decimal("0.01"))


def parse_date(value: str, where: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise DataError(f"{where}: неверная дата: {value!r}") from None


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise DataError(f"нет файла {path}")
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


@dataclass
class Dataset:
    contracts: dict[str, Contract]
    works: dict[str, Work]
    payments: list[Payment]


def load(data_dir: Path) -> Dataset:
    contracts: dict[str, Contract] = {}
    for i, r in enumerate(read_rows(data_dir / "contracts.csv"), start=2):
        where = f"contracts.csv:{i}"
        cid = r["contract_id"].strip()
        if cid in contracts:
            raise DataError(f"{where}: повтор договора {cid}")
        contracts[cid] = Contract(cid, r["contractor"].strip(),
                                  parse_money(r["amount"], where),
                                  parse_date(r["signed_at"], where))

    works: dict[str, Work] = {}
    for i, r in enumerate(read_rows(data_dir / "works.csv"), start=2):
        where = f"works.csv:{i}"
        wid = r["work_id"].strip()
        cid = r["contract_id"].strip()
        if wid in works:
            raise DataError(f"{where}: повтор работы {wid}")
        if cid not in contracts:
            raise DataError(f"{where}: работа {wid} ссылается на неизвестный договор {cid}")
        works[wid] = Work(wid, cid, r["title"].strip(),
                          parse_money(r["price"], where),
                          parse_date(r["completed_at"], where),
                          parse_date(r["cancelled_at"], where))

    payments: list[Payment] = []
    seen: set[str] = set()
    for i, r in enumerate(read_rows(data_dir / "payments.csv"), start=2):
        where = f"payments.csv:{i}"
        pid = r["payment_id"].strip()
        cid = r["contract_id"].strip()
        wid = (r["work_id"] or "").strip() or None
        if pid in seen:
            raise DataError(f"{where}: повтор платежа {pid}")
        seen.add(pid)
        if cid not in contracts:
            raise DataError(f"{where}: платёж {pid} ссылается на неизвестный договор {cid}")
        if wid is not None:
            if wid not in works:
                raise DataError(f"{where}: платёж {pid} ссылается на неизвестную работу {wid}")
            if works[wid].contract_id != cid:
                raise DataError(f"{where}: платёж {pid}: работа {wid} из другого договора")
        payments.append(Payment(pid, cid, wid, r["purpose"].strip(),
                                parse_money(r["amount"], where),
                                r["status"].strip(),
                                parse_date(r["created_at"], where),
                                parse_date(r["paid_at"], where),
                                parse_date(r["refunded_at"], where)))
    return Dataset(contracts, works, payments)


# ---------------------------------------------------------------- расчёт

@dataclass
class WorkLine:
    work: Work
    state: str
    paid: Decimal = ZERO
    queued: Decimal = ZERO

    @property
    def due(self) -> Decimal:
        """Сколько осталось оплатить по работе (по отменённой — ноль)."""
        return ZERO if self.state == "cancelled" else self.work.price - self.paid


@dataclass
class ContractReport:
    contract: Contract
    works: list[WorkLine]
    payments: list[tuple[Payment, str]]          # платёж и его состояние на дату
    allocations: list[tuple[str, str, Decimal]]  # (платёж, работа или «аванс», сумма)
    paid: Decimal = ZERO
    queued: Decimal = ZERO
    unallocated: Decimal = ZERO
    warnings: list[str] = field(default_factory=list)

    @property
    def works_price(self) -> Decimal:
        return sum((w.work.price for w in self.works if w.state != "cancelled"), ZERO)

    @property
    def cancelled_price(self) -> Decimal:
        return sum((w.work.price for w in self.works if w.state == "cancelled"), ZERO)

    @property
    def due_by_works(self) -> Decimal:
        return sum((w.due for w in self.works), ZERO)

    @property
    def due(self) -> Decimal:
        """Осталось оплатить по договору: от суммы договора (см. DECISIONS.md)."""
        return self.contract.amount - self.paid


def queue_order(lines: list[WorkLine]) -> list[WorkLine]:
    """Очередь гашения: принятые по дате приёмки, затем запланированные по номеру."""
    done = sorted((l for l in lines if l.state == "done"),
                  key=lambda l: (l.work.completed_at, l.work.work_id))
    planned = sorted((l for l in lines if l.state == "planned"),
                     key=lambda l: l.work.work_id)
    return done + planned


def build_contract(contract: Contract, ds: Dataset, on: date) -> ContractReport:
    lines = [WorkLine(w, w.state_on(on)) for w in ds.works.values()
             if w.contract_id == contract.contract_id]
    lines.sort(key=lambda l: l.work.work_id)
    by_id = {l.work.work_id: l for l in lines}

    pays = [(p, p.state_on(on)) for p in ds.payments if p.contract_id == contract.contract_id]
    pays.sort(key=lambda ps: (ps[0].paid_at or ps[0].created_at or date.max, ps[0].payment_id))

    rep = ContractReport(contract, lines, pays, [])
    pool: list[tuple[str, Decimal]] = []  # (платёж, сумма) для разнесения по очереди

    # 1. Привязанные платежи — на свою работу; излишек — в общий пул.
    for p, st in pays:
        if st == "paid":
            rep.paid += p.amount
        elif st == "queued":
            rep.queued += p.amount
            if p.work_id:
                by_id[p.work_id].queued += p.amount
        if st != "paid":
            continue
        if p.work_id is None:
            pool.append((p.payment_id, p.amount))
            continue
        line = by_id[p.work_id]
        room = ZERO if line.state == "cancelled" else line.work.price - line.paid
        take = min(room, p.amount)
        if take > 0:
            line.paid += take
            rep.allocations.append((p.payment_id, line.work.work_id, take))
        if p.amount > take:
            rest = p.amount - take
            pool.append((p.payment_id, rest))
            why = "отменена" if line.state == "cancelled" else "уже полностью оплачена"
            rep.warnings.append(
                f"Платёж {p.payment_id} привязан к работе {line.work.work_id}, которая {why}: "
                f"{fmt(rest)} перенесено в общие платежи по договору.")

    # 2. Непривязанные платежи и излишки — по очереди работ.
    order = queue_order(lines)
    for pid, amount in pool:
        for line in order:
            if amount == 0:
                break
            room = line.work.price - line.paid
            if room <= 0:
                continue
            take = min(room, amount)
            line.paid += take
            amount -= take
            rep.allocations.append((pid, line.work.work_id, take))
        if amount > 0:
            rep.unallocated += amount
            rep.allocations.append((pid, "аванс", amount))

    # 3. Предупреждения.
    a = contract.amount
    if rep.cancelled_price > 0:
        ids = ", ".join(l.work.work_id for l in lines if l.state == "cancelled")
        rep.warnings.append(
            f"Есть отменённые работы ({ids}) на {fmt(rep.cancelled_price)}. Остаток по договору "
            f"считается от суммы договора и больше суммы остатков по работам на "
            f"{fmt(rep.due - rep.due_by_works)}. Нужно подтвердить, уменьшена ли сумма договора.")
    elif rep.works_price != a:
        rep.warnings.append(
            f"Сумма цен работ {fmt(rep.works_price)} не равна сумме договора {fmt(a)}.")
    if rep.paid > a:
        rep.warnings.append(f"ПЕРЕПЛАТА: выплачено {fmt(rep.paid)} при сумме договора {fmt(a)}, "
                            f"сверх договора {fmt(rep.paid - a)}.")
    elif rep.paid + rep.queued > a:
        rep.warnings.append(
            f"После оплаты очереди будет переплата: выплачено {fmt(rep.paid)} + в очереди "
            f"{fmt(rep.queued)} больше суммы договора {fmt(a)} на {fmt(rep.paid + rep.queued - a)}.")
    if rep.unallocated > 0:
        rep.warnings.append(
            f"Выплачено больше, чем стоят все неотменённые работы: {fmt(rep.unallocated)} "
            f"не разнесено ни на одну работу.")
    for p, st in pays:
        if st == "refunded":
            rep.warnings.append(
                f"Платёж {p.payment_id} ({fmt(p.amount)}, «{p.purpose}») возвращён "
                f"{p.refunded_at:%d.%m.%Y}, в выплаченное не входит.")
        elif p.refunded_at is not None and st == "paid":
            rep.warnings.append(
                f"Платёж {p.payment_id} ({fmt(p.amount)}) будет возвращён "
                f"{p.refunded_at:%d.%m.%Y}, на дату отчёта считается выплаченным.")
    prepaid = [l for l in lines if l.state == "planned" and l.paid > 0]
    if prepaid:
        rep.warnings.append(
            "Аванс за ещё не принятые работы: "
            + ", ".join(f"{l.work.work_id} {fmt(l.paid)}" for l in prepaid) + ".")
    return rep


def build_report(ds: Dataset, on: date) -> list[ContractReport]:
    return [build_contract(c, ds, on) for c in sorted(ds.contracts.values(),
                                                      key=lambda c: c.contract_id)]


# ---------------------------------------------------------------- вывод

STATE_RU = {"done": "принята", "planned": "запланирована", "cancelled": "отменена",
            "paid": "выплачен", "queued": "в очереди", "refunded": "возвращён",
            "absent": "ещё не заведён"}


def fmt(x: Decimal) -> str:
    sign = "-" if x < 0 else ""
    whole, frac = f"{abs(x):.2f}".split(".")
    groups = []
    while whole:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    return f"{sign}{' '.join(groups)},{frac}"


def table(headers: list[str], rows: list[list[str]], right: set[int]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h)
              for i, h in enumerate(headers)]

    def line(cells: list[str]) -> str:
        return "  ".join(c.rjust(w) if i in right else c.ljust(w)
                         for i, (c, w) in enumerate(zip(cells, widths))).rstrip()

    out = [line(headers), "  ".join("-" * w for w in widths)]
    out += [line(r) for r in rows]
    return "\n".join(out)


def render(reports: list[ContractReport], on: date) -> str:
    out = [f"ОТЧЁТ ОБ ОПЛАТЕ НА {on:%d.%m.%Y} (включительно)", ""]
    for rep in reports:
        c = rep.contract
        out.append(f"=== Договор {c.contract_id} — {c.contractor}, сумма {fmt(c.amount)}")
        out.append("")
        rows = [[l.work.work_id, l.work.title, STATE_RU[l.state], fmt(l.work.price),
                 fmt(l.paid), fmt(l.due), fmt(l.queued) if l.queued else ""]
                for l in rep.works]
        out.append(table(["Работа", "Название", "Статус", "Цена", "Выплачено",
                          "Осталось", "В очереди"], rows, {3, 4, 5, 6}))
        out.append("")
        totals = [
            ("Сумма договора", c.amount),
            ("Цена неотменённых работ", rep.works_price),
            ("Выплачено всего", rep.paid),
            ("  из них разнесено по работам", rep.paid - rep.unallocated),
            ("  из них нераспределённый аванс", rep.unallocated),
            ("Осталось оплатить по договору", rep.due),
            ("Осталось оплатить по работам", rep.due_by_works),
            ("В очереди на оплату", rep.queued),
        ]
        out.append(table(["Итог по договору", "Сумма"],
                         [[k, fmt(v)] for k, v in totals], {1}))
        out.append("")
        rows = [[p.payment_id, p.purpose, p.work_id or "—", fmt(p.amount), STATE_RU[st],
                 f"{p.paid_at:%d.%m.%Y}" if p.paid_at and p.paid_at <= on else ""]
                for p, st in rep.payments]
        out.append("Платежи (состояние на дату отчёта):")
        out.append(table(["Платёж", "Назначение", "Работа", "Сумма", "На дату", "Оплачен"],
                         rows, {3}))
        if rep.allocations:
            out.append("")
            out.append("Как разнесены выплаты: " + "; ".join(
                f"{pid} → {wid} {fmt(amt)}" for pid, wid, amt in rep.allocations))
        if rep.warnings:
            out.append("")
            out.append("Обратить внимание:")
            out += [f"  ! {w}" for w in rep.warnings]
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Отчёт по оплате работ и договоров на дату.")
    ap.add_argument("--date", required=True, help="дата отчёта, ГГГГ-ММ-ДД (включительно)")
    ap.add_argument("--data", default=str(Path(__file__).resolve().parent / "data"),
                    help="папка с contracts.csv, works.csv, payments.csv")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = ap.parse_args(argv)
    try:
        on = date.fromisoformat(args.date)
    except ValueError:
        ap.error(f"неверная дата: {args.date!r}, нужен формат ГГГГ-ММ-ДД")
    try:
        ds = load(Path(args.data))
    except DataError as e:
        print(f"Ошибка в данных: {e}", file=sys.stderr)
        return 1
    print(render(build_report(ds, on), on))
    return 0


if __name__ == "__main__":
    sys.exit(main())
