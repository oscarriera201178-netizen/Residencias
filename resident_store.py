import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Callable, Generator, TypedDict

import db
from deposit_receipt import (
    SERVICE_CONCEPTS, generate_deposit_receipt,
    generate_monthly_receipt, generate_refund_receipt,
)
from resident_format import format_identity, format_phone, normalize_invoice_number
from vat_report import VatReport, calculate_vat_report
from payment_schedule import late_payment_days, next_month_start, payment_deadline


DATABASE_PATH = Path(os.environ.get("RESIDENCIAS_DATA_DIR", Path(__file__).parent / "data")) / "residencias.sqlite3"
ALERT_DAYS = 0
BANKS = ("Banco de Venezuela", "Banco del Tesoro", "Fondo Común")


def _validate_payment_currency(
    currency: str, received_amount: Decimal, payment_method: str | None,
    exchange_rate: Decimal | None, exchange_rate_date: date | None,
    amount_usd: Decimal | None, receiving_bank: str | None,
) -> Decimal | None:
    if currency == "VES":
        if receiving_bank not in BANKS:
            raise ValueError("Selecciona el banco que recibió los bolívares.")
        if payment_method is not None or exchange_rate is None or exchange_rate_date is None:
            raise ValueError("El pago en bolívares requiere la tasa y fecha publicadas por el BCV.")
        try:
            rate = Decimal(exchange_rate)
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError("La tasa BCV debe ser un número válido.") from error
        if not rate.is_finite() or rate <= 0:
            raise ValueError("La tasa BCV debe ser mayor que cero.")
        if amount_usd is not None and received_amount != bolivars_from_dollars(amount_usd, rate):
            raise ValueError("El monto en bolívares no coincide con dólares por tasa BCV.")
        return rate
    if currency == "USD":
        if receiving_bank is not None:
            raise ValueError("Los pagos en dólares no llevan banco receptor en bolívares.")
        if payment_method not in {"Efectivo", "Binance", "Zelle"}:
            raise ValueError("Indica si el pago en dólares fue en efectivo, Binance o Zelle.")
        if exchange_rate is not None or exchange_rate_date is not None:
            raise ValueError("Los pagos en dólares no llevan tasa BCV.")
        if amount_usd is not None:
            raise ValueError("La conversión desde dólares solo aplica a pagos en bolívares.")
        return None
    raise ValueError("Selecciona Bolívares o Dólares como moneda del pago.")


def calculate_expense_vat(total: Decimal) -> Decimal:
    return (total * Decimal("16") / Decimal("116")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    )


def bolivars_from_dollars(amount_usd: Decimal, rate: Decimal) -> Decimal:
    if (
        not amount_usd.is_finite() or amount_usd <= 0
        or not rate.is_finite() or rate <= 0
    ):
        raise ValueError("El monto en dólares y la tasa BCV deben ser mayores que cero.")
    try:
        if amount_usd != amount_usd.quantize(Decimal("0.01")):
            raise ValueError("El monto en dólares admite como máximo dos decimales.")
        result = (amount_usd * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as error:
        raise ValueError("El monto o la tasa excede el tamaño permitido.") from error
    if result <= 0:
        raise ValueError("El monto calculado debe ser de al menos Bs. 0,01.")
    return result


class Deposit(TypedDict):
    paid_on: str
    currency: str | None
    amount: str | None
    payment_method: str | None


class DepositRefund(TypedDict):
    refunded_on: str
    currency: str
    deposit_amount: str
    deduction: str
    deduction_reason: str
    amount: str
    keys_returned: bool
    inspection_ok: bool
    payment_method: str
    receipt_number: str
    receipt_pdf: bytes


class ReceivedPayment(TypedDict):
    record_id: int
    invoice_number: str | None
    student_name: str
    student_id: str
    room_number: str | None
    bed_number: str | None
    paid_on: str
    kind: str
    currency: str | None
    amount: str | None
    exchange_rate: str | None
    exchange_rate_date: str | None
    payment_method: str | None
    receiving_bank: str | None


class OpeningBalance(TypedDict):
    as_of: str
    currency: str
    payment_method: str
    amount: str
    description: str


class Expense(TypedDict):
    id: int
    concept: str
    paid_on: str
    registered_on: str | None
    currency: str
    payment_method: str
    amount: str
    invoice_number: str | None
    vat: str


class AccountBalance(TypedDict):
    currency: str
    payment_method: str
    amount: Decimal


def calculate_age(birth_date: date, today: date) -> str:
    if birth_date > today:
        raise ValueError("La fecha de nacimiento no puede ser futura.")
    years = today.year - birth_date.year - (
        (today.month, today.day) < (birth_date.month, birth_date.day)
    )
    return f"{years} AÑOS"


def normalize_admission_values(values: dict[str, str]) -> dict[str, str]:
    normalized = {
        field: value.strip().lower() if field == "email" else value.strip().upper()
        for field, value in values.items()
    }
    for field in ("student_id", "representative_id"):
        if field in normalized:
            normalized[field] = format_identity(normalized[field])
    for field in (
        "student_mobile", "student_landline", "emergency_phone",
        "mother_phone", "mother_landline", "father_phone", "father_landline",
        "emergency_contacts",
    ):
        if field in normalized:
            normalized[field] = format_phone(normalized[field])
    return normalized


@dataclass(frozen=True)
class Resident:
    id: int
    residence: int
    values: dict[str, str]
    documents: list[bool]
    admission_date: date
    next_payment: date
    discharged_on: date | None
    discharge_reason: str | None


def payment_status(due: date, today: date) -> str:
    if due.day == 1 and due <= today <= payment_deadline(due):
        return "Pago pendiente (plazo del 1 al 5)"
    remaining = (due - today).days
    if remaining < 0:
        return "Vencido"
    if remaining == 0:
        return "Vence hoy"
    return "Al día"


class ResidentStore:
    def __init__(self, path: Path = DATABASE_PATH):
        self.authorize_write: Callable[[], None] | None = None
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS residents (
                    id INTEGER PRIMARY KEY,
                    residence INTEGER NOT NULL CHECK (residence IN (0, 1)),
                    identity TEXT NOT NULL,
                    values_json TEXT NOT NULL,
                    documents_json TEXT NOT NULL,
                    pdf BLOB NOT NULL,
                    photo BLOB,
                    admission_date TEXT NOT NULL,
                    next_payment TEXT NOT NULL,
                    discharged_on TEXT,
                    discharge_reason TEXT,
                    calendar_billing INTEGER NOT NULL DEFAULT 1
                );
                CREATE UNIQUE INDEX IF NOT EXISTS active_resident_identity
                    ON residents (residence, identity)
                    WHERE discharged_on IS NULL;
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY,
                    resident_id INTEGER NOT NULL REFERENCES residents(id),
                    due_date TEXT NOT NULL,
                    paid_on TEXT NOT NULL,
                    UNIQUE (resident_id, due_date)
                );
                CREATE TABLE IF NOT EXISTS deposits (
                    resident_id INTEGER PRIMARY KEY REFERENCES residents(id),
                    paid_on TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipt_counters (
                    residence INTEGER NOT NULL CHECK (residence IN (0, 1)),
                    kind TEXT NOT NULL CHECK (kind IN ('deposit', 'monthly', 'refund')),
                    year INTEGER NOT NULL,
                    last_number INTEGER NOT NULL,
                    PRIMARY KEY (residence, kind, year)
                );
                CREATE TABLE IF NOT EXISTS deposit_refunds (
                    resident_id INTEGER PRIMARY KEY REFERENCES deposits(resident_id),
                    refunded_on TEXT NOT NULL,
                    currency TEXT NOT NULL CHECK (currency = 'USD'),
                    deposit_amount TEXT NOT NULL,
                    deduction TEXT NOT NULL,
                    deduction_reason TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    keys_returned INTEGER NOT NULL CHECK (keys_returned IN (0, 1)),
                    inspection_ok INTEGER NOT NULL CHECK (inspection_ok IN (0, 1)),
                    payment_method TEXT NOT NULL,
                    receipt_number TEXT NOT NULL,
                    receipt_pdf BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS opening_balances (
                    residence INTEGER NOT NULL CHECK (residence IN (0, 1)),
                    as_of TEXT NOT NULL,
                    currency TEXT NOT NULL CHECK (currency IN ('USD', 'VES')),
                    payment_method TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    description TEXT NOT NULL,
                    PRIMARY KEY (residence, currency, payment_method)
                );
                CREATE TABLE IF NOT EXISTS expenses (
                    id INTEGER PRIMARY KEY,
                    residence INTEGER NOT NULL CHECK (residence IN (0, 1)),
                    concept TEXT NOT NULL,
                    paid_on TEXT NOT NULL,
                    currency TEXT NOT NULL CHECK (currency IN ('USD', 'VES')),
                    payment_method TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    invoice_number TEXT,
                    vat TEXT NOT NULL,
                    registered_on TEXT
                );
                CREATE INDEX IF NOT EXISTS expenses_residence_date
                    ON expenses (residence, paid_on);
                CREATE TABLE IF NOT EXISTS vat_payments (
                    residence INTEGER NOT NULL CHECK (residence IN (0, 1)),
                    month TEXT NOT NULL,
                    expense_id INTEGER NOT NULL UNIQUE REFERENCES expenses(id),
                    reference TEXT NOT NULL,
                    debit TEXT NOT NULL,
                    credit TEXT NOT NULL,
                    PRIMARY KEY (residence, month)
                );
                CREATE TABLE IF NOT EXISTS service_payments (
                    id INTEGER PRIMARY KEY,
                    resident_id INTEGER NOT NULL REFERENCES residents(id),
                    concept TEXT NOT NULL,
                    paid_on TEXT NOT NULL,
                    currency TEXT NOT NULL CHECK (currency IN ('USD', 'VES')),
                    amount TEXT NOT NULL,
                    payment_method TEXT,
                    exchange_rate TEXT,
                    exchange_rate_date TEXT,
                    amount_usd TEXT,
                    receiving_bank TEXT,
                    invoice_number TEXT,
                    receipt_number TEXT NOT NULL,
                    receipt_pdf BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payment_reminders (
                    resident_id INTEGER NOT NULL REFERENCES residents(id) ON DELETE CASCADE,
                    due_date TEXT NOT NULL,
                    reminder_date TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('sending', 'sent', 'failed', 'unknown')),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (resident_id, reminder_date)
                );
                """
            )
            expense_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(expenses)")
            }
            if "registered_on" not in expense_columns:
                connection.execute("ALTER TABLE expenses ADD COLUMN registered_on TEXT")
            counter_schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'receipt_counters'"
            ).fetchone()["sql"]
            if "'refund'" not in counter_schema:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("ALTER TABLE receipt_counters RENAME TO old_receipt_counters")
                connection.execute(
                    "CREATE TABLE receipt_counters ("
                    "residence INTEGER NOT NULL CHECK (residence IN (0, 1)), "
                    "kind TEXT NOT NULL CHECK (kind IN ('deposit', 'monthly', 'refund')), "
                    "year INTEGER NOT NULL, last_number INTEGER NOT NULL, "
                    "PRIMARY KEY (residence, kind, year))"
                )
                connection.execute(
                    "INSERT INTO receipt_counters SELECT * FROM old_receipt_counters"
                )
                connection.execute("DROP TABLE old_receipt_counters")
            payment_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(payments)")
            }
            for column in (
                "currency", "amount", "payment_method", "exchange_rate",
                "exchange_rate_date", "amount_usd",
                "monthly_amount_usd", "late_fee_usd", "late_days",
                "receiving_bank",
            ):
                if column not in payment_columns:
                    connection.execute(f"ALTER TABLE payments ADD COLUMN {column} TEXT")
            if "receipt_pdf" not in payment_columns:
                connection.execute("ALTER TABLE payments ADD COLUMN receipt_pdf BLOB")
            deposit_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(deposits)")
            }
            for column in ("currency", "amount", "payment_method"):
                if column not in deposit_columns:
                    connection.execute(f"ALTER TABLE deposits ADD COLUMN {column} TEXT")
            if "receipt_pdf" not in deposit_columns:
                connection.execute("ALTER TABLE deposits ADD COLUMN receipt_pdf BLOB")
            for table, columns in (
                ("payments", payment_columns), ("deposits", deposit_columns),
            ):
                if "receipt_number" not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN receipt_number TEXT")
                if "invoice_number" not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN invoice_number TEXT")
            resident_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(residents)")
            }
            if "calendar_billing" not in resident_columns:
                connection.execute(
                    "ALTER TABLE residents ADD COLUMN calendar_billing INTEGER NOT NULL DEFAULT 0"
                )
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            legacy_residents = connection.execute(
                "SELECT r.id, r.admission_date, r.discharged_on, MAX(p.due_date) AS last_due "
                "FROM residents r LEFT JOIN payments p ON p.resident_id = r.id "
                "WHERE r.calendar_billing = 0 GROUP BY r.id"
            ).fetchall()
            for resident in legacy_residents:
                if resident["discharged_on"] is None:
                    due = (
                        next_month_start(date.fromisoformat(resident["last_due"]))
                        if resident["last_due"] else date.fromisoformat(resident["admission_date"])
                    )
                    connection.execute(
                        "UPDATE residents SET next_payment = ? WHERE id = ?",
                        (due.isoformat(), resident["id"]),
                    )
                connection.execute(
                    "UPDATE residents SET calendar_billing = 1 WHERE id = ?", (resident["id"],)
                )

    def _require_write(self) -> None:
        if self.authorize_write is not None:
            self.authorize_write()

    @staticmethod
    def _next_receipt_number(
        connection: sqlite3.Connection, residence: int, kind: str
    ) -> str:
        year = date.today().year
        connection.execute(
            "INSERT INTO receipt_counters (residence, kind, year, last_number) "
            "VALUES (?, ?, ?, 1) ON CONFLICT (residence, kind, year) "
            "DO UPDATE SET last_number = last_number + 1",
            (residence, kind, year),
        )
        row = connection.execute(
            "SELECT last_number FROM receipt_counters "
            "WHERE residence = ? AND kind = ? AND year = ?",
            (residence, kind, year),
        ).fetchone()
        return f"{row['last_number']:05d}-{year}"

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        connection = db.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def add(
        self,
        residence: int,
        values: dict[str, str],
        documents: list[bool],
        pdf: bytes,
        photo: bytes | None,
    ) -> int:
        self._require_write()
        values = normalize_admission_values(values)
        missing_assignment = [
            label for field, label in (("room_number", "Habitación"), ("bed_number", "Cama"))
            if not values.get(field)
        ]
        if missing_assignment:
            raise ValueError("Completa estos campos obligatorios: " + ", ".join(missing_assignment))
        admission = datetime.strptime(values["admission_date"], "%d/%m/%Y").date()
        identity = "".join(
            character for character in values["student_id"].upper()
            if character.isalnum()
        )
        if not identity:
            raise ValueError("La cédula debe contener letras o números.")
        if not values["student_name"].strip() or not pdf:
            raise ValueError("El nombre y la planilla son obligatorios.")
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM residents WHERE residence = ? AND identity = ? "
                "AND discharged_on IS NULL",
                (residence, identity),
            ).fetchone():
                raise ValueError("Ya existe una residente activa con esta cédula.")
            cursor = connection.execute(
                """
                INSERT INTO residents (
                    residence, identity, values_json, documents_json, pdf, photo,
                    admission_date, next_payment, calendar_billing
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    residence, identity, json.dumps(values, ensure_ascii=False),
                    json.dumps(documents), pdf, photo, admission.isoformat(),
                    admission.isoformat(),
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("No se obtuvo el identificador del ingreso guardado.")
            return cursor.lastrowid

    def list_residents(self, residence: int, inactive: bool = False) -> list[Resident]:
        condition = "IS NOT NULL" if inactive else "IS NULL"
        order = "discharged_on DESC, id DESC" if inactive else "next_payment, id"
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, residence, values_json, documents_json, admission_date, "
                "next_payment, discharged_on, discharge_reason FROM residents "
                f"WHERE residence = ? AND discharged_on {condition} "
                f"ORDER BY {order}",
                (residence,),
            ).fetchall()
        return [
            Resident(
                id=row["id"], residence=row["residence"],
                values=json.loads(row["values_json"]),
                documents=json.loads(row["documents_json"]),
                admission_date=date.fromisoformat(row["admission_date"]),
                next_payment=date.fromisoformat(row["next_payment"]),
                discharged_on=(
                    date.fromisoformat(row["discharged_on"])
                    if row["discharged_on"] else None
                ),
                discharge_reason=row["discharge_reason"],
            )
            for row in rows
        ]

    def get_pdf(self, resident_id: int, residence: int) -> bytes:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT pdf FROM residents WHERE id = ? AND residence = ?",
                (resident_id, residence),
            ).fetchone()
        if row is None:
            raise ValueError("No se encontró la planilla de esta residente.")
        return bytes(row["pdf"])

    def can_delete_admission(self, resident_id: int, residence: int) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM residents r WHERE r.id = ? AND r.residence = ? "
                "AND r.discharged_on IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM deposits d WHERE d.resident_id = r.id) "
                "AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.resident_id = r.id) "
                "AND NOT EXISTS (SELECT 1 FROM service_payments s WHERE s.resident_id = r.id)",
                (resident_id, residence),
            ).fetchone() is not None

    def claim_payment_reminder(
        self, resident: Resident, reminder_date: date, updated_at: str,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO payment_reminders "
                "(resident_id, due_date, reminder_date, status, updated_at) "
                "SELECT id, next_payment, ?, 'sending', ? FROM residents "
                "WHERE id = ? AND residence = ? AND discharged_on IS NULL "
                "AND next_payment = ? AND COALESCE(json_extract(values_json, '$.email'), '') = ? "
                "ON CONFLICT (resident_id, reminder_date) DO NOTHING",
                (
                    reminder_date.isoformat(), updated_at, resident.id, resident.residence,
                    resident.next_payment.isoformat(),
                    resident.values.get("email", ""),
                ),
            )
            return cursor.rowcount == 1

    def finish_payment_reminder(
        self, resident_id: int, due_date: date, reminder_date: date,
        status: str, updated_at: str,
    ) -> None:
        if status not in {"sent", "failed", "unknown"}:
            raise ValueError("Estado de recordatorio no válido.")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE payment_reminders SET status = ?, updated_at = ? "
                "WHERE resident_id = ? AND due_date = ? AND reminder_date = ? "
                "AND status = 'sending'",
                (status, updated_at, resident_id, due_date.isoformat(), reminder_date.isoformat()),
            )
            if cursor.rowcount != 1:
                raise ValueError("No se encontró el envío pendiente del recordatorio.")

    def reminder_counts(self, residence: int, reminder_date: date) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT m.status, COUNT(*) AS count FROM payment_reminders m "
                "JOIN residents r ON r.id = m.resident_id "
                "WHERE r.residence = ? AND m.reminder_date = ? GROUP BY m.status",
                (residence, reminder_date.isoformat()),
            ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def delete_admission(self, resident_id: int, residence: int) -> None:
        self._require_write()
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM residents WHERE id = ? AND residence = ? "
                "AND discharged_on IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM deposits WHERE resident_id = residents.id) "
                "AND NOT EXISTS (SELECT 1 FROM payments WHERE resident_id = residents.id) "
                "AND NOT EXISTS (SELECT 1 FROM service_payments WHERE resident_id = residents.id)",
                (resident_id, residence),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    "No se puede eliminar el ingreso: tiene pagos "
                    "registradas, está de baja o ya no está disponible. Actualiza la lista."
                )

    def record_payment(
        self,
        resident_id: int,
        residence: int,
        expected_due: date,
        paid_on: date,
        currency: str,
        amount: Decimal,
        payment_method: str | None = None,
        exchange_rate: Decimal | None = None,
        exchange_rate_date: date | None = None,
        amount_usd: Decimal | None = None,
        *,
        monthly_amount_usd: Decimal | None = None,
        late_fee_usd: Decimal = Decimal("0"),
        receiving_bank: str | None = None,
        invoice_number: str = "",
    ) -> None:
        self._require_write()
        invoice_number = normalize_invoice_number(invoice_number)
        try:
            received_amount = Decimal(amount)
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError("La cantidad recibida debe ser un número válido.") from error
        if not received_amount.is_finite() or received_amount <= 0:
            raise ValueError("La cantidad recibida debe ser mayor que cero.")
        late_days = late_payment_days(expected_due, paid_on)
        try:
            fee = Decimal(late_fee_usd)
            if (
                not fee.is_finite() or fee < 0
                or fee != fee.quantize(Decimal("0.01"))
            ):
                raise ValueError("La mora debe ser un monto no negativo con máximo dos decimales.")
        except (InvalidOperation, TypeError) as error:
            raise ValueError("El monto de mora no es válido.") from error
        if fee > 0 and late_days == 0:
            raise ValueError("No se puede cobrar mora antes de finalizar el plazo de pago.")
        base = monthly_amount_usd
        if base is None:
            if currency == "USD":
                base = received_amount - fee
            elif amount_usd is not None:
                base = amount_usd - fee
            elif fee:
                raise ValueError("La mora en bolívares requiere el monto base en dólares.")
        if base is not None:
            try:
                if (
                    not base.is_finite() or base <= 0
                    or base != base.quantize(Decimal("0.01"))
                ):
                    raise ValueError("La mensualidad debe ser positiva con máximo dos decimales.")
            except InvalidOperation as error:
                raise ValueError("El monto de la mensualidad excede el tamaño permitido.") from error
            total_usd = base + fee
            if currency == "USD" and received_amount != total_usd:
                raise ValueError("El total recibido no coincide con mensualidad más mora.")
            if currency == "VES" and amount_usd != total_usd:
                raise ValueError("El total en dólares no coincide con mensualidad más mora.")

        rate = _validate_payment_currency(
            currency, received_amount, payment_method, exchange_rate,
            exchange_rate_date, amount_usd, receiving_bank,
        )

        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE residents SET next_payment = ? WHERE id = ? "
                "AND residence = ? AND discharged_on IS NULL AND next_payment = ?",
                (
                    next_month_start(expected_due).isoformat(), resident_id,
                    residence, expected_due.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    "El registro cambió o la residente está de baja. Actualiza la lista."
                )
            resident = connection.execute(
                "SELECT values_json FROM residents WHERE id = ?", (resident_id,)
            ).fetchone()
            receipt_number = self._next_receipt_number(connection, residence, "monthly")
            receipt = generate_monthly_receipt(
                resident_id, residence, json.loads(resident["values_json"]),
                expected_due, paid_on, received_amount, currency,
                payment_method, rate, exchange_rate_date,
                amount_usd=amount_usd,
                monthly_amount_usd=base, late_fee_usd=fee,
                receiving_bank=receiving_bank,
                receipt_number=receipt_number,
                invoice_number=invoice_number,
            )
            connection.execute(
                "INSERT INTO payments (resident_id, due_date, paid_on, currency, "
                "amount, payment_method, exchange_rate, exchange_rate_date, receipt_pdf, "
                "amount_usd, receipt_number, monthly_amount_usd, late_fee_usd, late_days, receiving_bank, "
                "invoice_number) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    resident_id, expected_due.isoformat(), paid_on.isoformat(),
                    currency, str(received_amount), payment_method,
                    str(rate) if rate is not None else None,
                    exchange_rate_date.isoformat() if exchange_rate_date else None,
                    receipt,
                    str(amount_usd) if amount_usd is not None else None,
                    receipt_number,
                    str(base) if base is not None else None, str(fee), str(late_days),
                    receiving_bank,
                    invoice_number or None,
                ),
            )

    def record_service_payment(
        self, resident_id: int, residence: int, concept: str, paid_on: date,
        currency: str, amount: Decimal, *,
        payment_method: str | None = None,
        exchange_rate: Decimal | None = None,
        exchange_rate_date: date | None = None,
        amount_usd: Decimal | None = None,
        receiving_bank: str | None = None,
        invoice_number: str = "",
    ) -> None:
        self._require_write()
        if concept not in SERVICE_CONCEPTS:
            raise ValueError("Selecciona un concepto de servicio válido.")
        if paid_on > date.today():
            raise ValueError("La fecha del pago no puede ser futura.")
        invoice_number = normalize_invoice_number(invoice_number)
        try:
            received_amount = Decimal(amount)
            if (
                not received_amount.is_finite() or received_amount <= 0
                or received_amount != received_amount.quantize(Decimal("0.01"))
            ):
                raise ValueError("El monto debe ser positivo con máximo dos decimales.")
        except (InvalidOperation, TypeError) as error:
            raise ValueError("Ingresa una cantidad recibida válida.") from error
        rate = _validate_payment_currency(
            currency, received_amount, payment_method, exchange_rate,
            exchange_rate_date, amount_usd, receiving_bank,
        )
        if currency == "VES" and amount_usd is None:
            raise ValueError("El servicio en bolívares requiere el monto base en dólares.")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            resident = connection.execute(
                "SELECT values_json FROM residents WHERE id = ? AND residence = ? "
                "AND discharged_on IS NULL", (resident_id, residence),
            ).fetchone()
            if resident is None:
                raise ValueError("No se encontró una residente activa en esta residencia.")
            receipt_number = self._next_receipt_number(connection, residence, "monthly")
            receipt = generate_monthly_receipt(
                resident_id, residence, json.loads(resident["values_json"]),
                paid_on, paid_on, received_amount, currency,
                payment_method, rate, exchange_rate_date, amount_usd=amount_usd,
                receiving_bank=receiving_bank, invoice_number=invoice_number,
                receipt_number=receipt_number, service_concept=concept,
            )
            connection.execute(
                "INSERT INTO service_payments (resident_id, concept, paid_on, currency, amount, "
                "payment_method, exchange_rate, exchange_rate_date, amount_usd, receiving_bank, "
                "invoice_number, receipt_number, receipt_pdf) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    resident_id, concept, paid_on.isoformat(), currency, str(received_amount),
                    payment_method, str(rate) if rate is not None else None,
                    exchange_rate_date.isoformat() if exchange_rate_date else None,
                    str(amount_usd) if amount_usd is not None else None, receiving_bank,
                    invoice_number or None, receipt_number, receipt,
                ),
            )

    def service_payment_history(self, resident_id: int, residence: int) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                "SELECT s.* FROM service_payments s JOIN residents r ON r.id = s.resident_id "
                "WHERE r.id = ? AND r.residence = ? ORDER BY s.paid_on, s.id",
                (resident_id, residence),
            ).fetchall()

    def get_payment_receipt(
        self, resident_id: int, residence: int, due_date: date
    ) -> bytes | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT p.receipt_pdf FROM payments p "
                "JOIN residents r ON r.id = p.resident_id "
                "WHERE r.id = ? AND r.residence = ? AND p.due_date = ?",
                (resident_id, residence, due_date.isoformat()),
            ).fetchone()
        return bytes(row["receipt_pdf"]) if row and row["receipt_pdf"] else None

    def payment_history(self, resident_id: int, residence: int) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT p.due_date, p.paid_on, p.currency, p.amount, "
                "p.payment_method, p.exchange_rate, p.exchange_rate_date, p.amount_usd, "
                "p.monthly_amount_usd, p.late_fee_usd, p.late_days, p.receiving_bank FROM payments p "
                "JOIN residents r ON r.id = p.resident_id "
                "WHERE r.id = ? AND r.residence = ? ORDER BY p.due_date",
                (resident_id, residence),
            ).fetchall()
        return [dict(row) for row in rows]

    def received_payments(self, residence: int, through: date) -> list[ReceivedPayment]:
        end = date(through.year + (through.month == 12), through.month % 12 + 1, 1)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT r.values_json, p.paid_on, p.currency, p.amount, "
                "p.exchange_rate, p.exchange_rate_date, p.payment_method, "
                "p.receiving_bank, 'Mensualidad' AS kind, "
                "p.id AS record_id, p.invoice_number "
                "FROM payments p JOIN residents r ON r.id = p.resident_id "
                "WHERE r.residence = ? AND p.paid_on < ? "
                "UNION ALL "
                "SELECT r.values_json, d.paid_on, d.currency, d.amount, "
                "NULL, NULL, d.payment_method, NULL, 'Depósito de ingreso', "
                "d.resident_id, d.invoice_number "
                "FROM deposits d JOIN residents r ON r.id = d.resident_id "
                "WHERE r.residence = ? AND d.paid_on < ? "
                "UNION ALL "
                "SELECT r.values_json, s.paid_on, s.currency, s.amount, s.exchange_rate, "
                "s.exchange_rate_date, s.payment_method, s.receiving_bank, "
                "'Otros servicios: ' || s.concept, s.id, s.invoice_number "
                "FROM service_payments s JOIN residents r ON r.id = s.resident_id "
                "WHERE r.residence = ? AND s.paid_on < ? "
                "ORDER BY paid_on",
                (residence, end.isoformat(), residence, end.isoformat(), residence, end.isoformat()),
            ).fetchall()
        result: list[ReceivedPayment] = []
        for row in rows:
            values = json.loads(row["values_json"])
            result.append({
                "record_id": row["record_id"], "invoice_number": row["invoice_number"],
                "student_name": values["student_name"], "student_id": values["student_id"],
                "room_number": values.get("room_number"), "bed_number": values.get("bed_number"),
                "paid_on": row["paid_on"], "kind": row["kind"], "currency": row["currency"],
                "amount": row["amount"], "exchange_rate": row["exchange_rate"],
                "exchange_rate_date": row["exchange_rate_date"],
                "payment_method": row["payment_method"],
                "receiving_bank": row["receiving_bank"],
            })
        return result

    def set_invoice_number(
        self, residence: int, kind: str, record_id: int, invoice_number: str
    ) -> None:
        self._require_write()
        tables = {
            "Mensualidad": ("payments", "id"),
            "Depósito de ingreso": ("deposits", "resident_id"),
            **{
                f"Otros servicios: {concept}": ("service_payments", "id")
                for concept in SERVICE_CONCEPTS
            },
        }
        if kind not in tables:
            raise ValueError("Selecciona un pago de mensualidad, depósito o servicio válido.")
        number = normalize_invoice_number(invoice_number)
        table, identity = tables[kind]
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE {table} SET invoice_number = ? WHERE {identity} = ? "
                "AND resident_id IN (SELECT id FROM residents WHERE residence = ?)",
                (number or None, record_id, residence),
            )
            if cursor.rowcount != 1:
                raise ValueError("No se encontró el pago en esta residencia.")

    def record_opening_balance(
        self, residence: int, as_of: date, currency: str,
        payment_method: str, amount: Decimal, description: str,
    ) -> None:
        self._require_write()
        if as_of > date.today():
            raise ValueError("La fecha del saldo inicial no puede ser futura.")
        methods = {"Efectivo", "Zelle", "Binance"} if currency == "USD" else set(BANKS)
        if currency not in {"USD", "VES"} or payment_method not in methods:
            raise ValueError("Indica una moneda y un medio válidos para el saldo inicial.")
        try:
            valid = (
                amount.is_finite() and amount > 0
                and amount == amount.quantize(Decimal("0.01"))
            )
        except InvalidOperation as error:
            raise ValueError("El saldo inicial excede el tamaño permitido.") from error
        if not valid or not description.strip():
            raise ValueError("El saldo inicial debe ser positivo, con dos decimales y descripción.")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO opening_balances "
                "(residence, as_of, currency, payment_method, amount, description) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (residence, as_of.isoformat(), currency, payment_method,
                 str(amount), description.strip()),
            )

    def opening_balances(self, residence: int, through: date) -> list[OpeningBalance]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT as_of, currency, payment_method, amount, description "
                "FROM opening_balances WHERE residence = ? AND as_of <= ? "
                "ORDER BY as_of, currency, payment_method",
                (residence, through.isoformat()),
            ).fetchall()
        return [
            OpeningBalance(
                as_of=row["as_of"], currency=row["currency"],
                payment_method=row["payment_method"], amount=row["amount"],
                description=row["description"],
            )
            for row in rows
        ]

    @staticmethod
    def _account_movements(connection: sqlite3.Connection, residence: int) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT as_of AS paid_on, currency, payment_method, amount, 1 AS direction "
            "FROM opening_balances WHERE residence = ? "
            "UNION ALL "
            "SELECT p.paid_on, p.currency, "
            "CASE WHEN p.currency = 'VES' THEN p.receiving_bank ELSE p.payment_method END, "
            "p.amount, 1 FROM payments p JOIN residents r ON r.id = p.resident_id "
            "WHERE r.residence = ? "
            "UNION ALL "
            "SELECT d.paid_on, d.currency, d.payment_method, d.amount, 1 "
            "FROM deposits d JOIN residents r ON r.id = d.resident_id WHERE r.residence = ? "
            "UNION ALL "
            "SELECT s.paid_on, s.currency, "
            "CASE WHEN s.currency = 'VES' THEN s.receiving_bank ELSE s.payment_method END, "
            "s.amount, 1 FROM service_payments s JOIN residents r ON r.id = s.resident_id "
            "WHERE r.residence = ? "
            "UNION ALL "
            "SELECT d.refunded_on, d.currency, d.payment_method, d.amount, -1 "
            "FROM deposit_refunds d JOIN residents r ON r.id = d.resident_id "
            "WHERE r.residence = ? "
            "UNION ALL "
            "SELECT paid_on, currency, payment_method, amount, -1 FROM expenses "
            "WHERE residence = ? ORDER BY paid_on",
            (residence,) * 6,
        ).fetchall()

    @staticmethod
    def _movement_amount(row: sqlite3.Row) -> Decimal:
        if row["amount"] is None:
            return Decimal("0")
        try:
            amount = Decimal(row["amount"])
        except InvalidOperation as error:
            raise ValueError("Hay un movimiento con un importe no válido; revisa los registros.") from error
        if not amount.is_finite() or amount < 0:
            raise ValueError("Hay un movimiento con un importe no válido; revisa los registros.")
        return amount * row["direction"]

    def account_balances(self, residence: int, through: date) -> list[AccountBalance]:
        totals = {
            **{("VES", bank): Decimal("0") for bank in BANKS},
            **{("USD", method): Decimal("0") for method in ("Efectivo", "Binance", "Zelle")},
        }
        with self._connect() as connection:
            for row in self._account_movements(connection, residence):
                key = (row["currency"], row["payment_method"])
                if key in totals and row["paid_on"] <= through.isoformat():
                    totals[key] += self._movement_amount(row)
        return [
            AccountBalance(currency=currency, payment_method=method, amount=amount)
            for (currency, method), amount in totals.items()
        ]

    def expenses(self, residence: int, through: date) -> list[Expense]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, concept, paid_on, currency, payment_method, amount, "
                "invoice_number, vat, registered_on FROM expenses "
                "WHERE residence = ? AND paid_on <= ? ORDER BY paid_on DESC, id DESC",
                (residence, through.isoformat()),
            ).fetchall()
        return [Expense(**dict(row)) for row in rows]

    def record_expense(
        self, residence: int, concept: str, paid_on: date, currency: str,
        payment_method: str, amount: Decimal, *, with_invoice: bool = False,
        invoice_number: str = "",
    ) -> int:
        self._require_write()
        methods = BANKS if currency == "VES" else ("Efectivo", "Binance", "Zelle")
        if residence not in (0, 1) or currency not in {"VES", "USD"} or payment_method not in methods:
            raise ValueError("Selecciona una residencia, moneda y banco o medio de pago válidos.")
        if not concept.strip() or paid_on > date.today():
            raise ValueError("Indica el concepto y una fecha de pago que no sea futura.")
        if currency == "USD" and with_invoice:
            raise ValueError("Los gastos pagados en dólares no pueden registrarse con factura.")
        try:
            valid = amount.is_finite() and amount > 0 and amount == amount.quantize(Decimal("0.01"))
        except InvalidOperation as error:
            raise ValueError("El total pagado excede el tamaño permitido.") from error
        if not valid:
            raise ValueError("El total pagado debe ser positivo, con máximo dos decimales.")
        number = invoice_number.strip()
        if with_invoice and (not number or len(number) > 40):
            raise ValueError("Indica un número de factura de hasta 40 caracteres.")
        if not with_invoice and number:
            raise ValueError("Un gasto sin factura no puede tener número de factura.")
        vat = calculate_expense_vat(amount) if with_invoice else Decimal("0")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._insert_expense(
                connection, residence, concept.strip(), paid_on, currency,
                payment_method, amount, number, vat,
            )

    def _insert_expense(
        self, connection: sqlite3.Connection, residence: int, concept: str,
        paid_on: date, currency: str, payment_method: str, amount: Decimal,
        invoice_number: str, vat: Decimal,
    ) -> int:
        daily: dict[str, Decimal] = {paid_on.isoformat(): Decimal("0")}
        for row in self._account_movements(connection, residence):
            if row["currency"] == currency and row["payment_method"] == payment_method:
                day = row["paid_on"]
                daily[day] = daily.get(day, Decimal("0")) + self._movement_amount(row)
        balance = Decimal("0")
        for day, movement in sorted(daily.items()):
            balance += movement
            if day >= paid_on.isoformat() and balance < amount:
                raise ValueError(
                    "Saldo insuficiente en el banco o medio seleccionado. "
                    "El gasto no puede dejar saldo negativo en su fecha ni en fechas posteriores."
                )
        cursor = connection.execute(
            "INSERT INTO expenses (residence, concept, paid_on, currency, payment_method, "
            "amount, invoice_number, vat, registered_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (residence, concept, paid_on.isoformat(), currency, payment_method,
             str(amount), invoice_number or None, str(vat), date.today().isoformat()),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("No se obtuvo el identificador del gasto guardado.")
        return cursor.lastrowid

    @staticmethod
    def _vat_report(connection: sqlite3.Connection, residence: int, month: date) -> VatReport:
        start = month.replace(day=1)
        end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
        rows = connection.execute(
            "SELECT paid_on, kind, invoice_number, concept, amount, vat FROM ("
            "SELECT p.paid_on, 'Cobro' AS kind, p.invoice_number, "
            "'Mensualidad' AS concept, p.amount, NULL AS vat, p.currency, r.residence "
            "FROM payments p JOIN residents r ON r.id = p.resident_id "
            "UNION ALL "
            "SELECT p.paid_on, 'Cobro', p.invoice_number, 'Depósito de ingreso', "
            "p.amount, NULL, p.currency, r.residence "
            "FROM deposits p JOIN residents r ON r.id = p.resident_id "
            "UNION ALL "
            "SELECT p.paid_on, 'Cobro', p.invoice_number, p.concept, "
            "p.amount, NULL, p.currency, r.residence "
            "FROM service_payments p JOIN residents r ON r.id = p.resident_id "
            "UNION ALL "
            "SELECT paid_on, 'Gasto', invoice_number, concept, amount, vat, currency, residence "
            "FROM expenses) WHERE residence = ? AND currency = 'VES' "
            "AND TRIM(COALESCE(invoice_number, '')) != '' "
            "AND paid_on >= ? AND paid_on < ? AND paid_on <= ? "
            "ORDER BY paid_on, kind, invoice_number",
            (residence, start.isoformat(), end.isoformat(), date.today().isoformat()),
        ).fetchall()
        return calculate_vat_report([dict(row) for row in rows])

    def vat_report(self, residence: int, month: date) -> VatReport:
        with self._connect() as connection:
            return self._vat_report(connection, residence, month)

    def vat_payment(self, residence: int, month: date) -> dict[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT v.reference, v.debit, v.credit, e.paid_on, e.payment_method, e.amount "
                "FROM vat_payments v JOIN expenses e ON e.id = v.expense_id "
                "WHERE v.residence = ? AND v.month = ?",
                (residence, month.replace(day=1).isoformat()),
            ).fetchone()
        return dict(row) if row else None

    def record_vat_payment(
        self, residence: int, month: date, paid_on: date, bank: str,
        reference: str, expected_amount: Decimal,
    ) -> None:
        self._require_write()
        month = month.replace(day=1)
        if (
            residence not in (0, 1) or bank not in BANKS or not reference.strip()
            or paid_on > date.today() or paid_on < month
        ):
            raise ValueError("Indica residencia, banco, referencia y fecha de pago válidos.")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM vat_payments WHERE residence = ? AND month = ?",
                (residence, month.isoformat()),
            ).fetchone():
                raise ValueError("El pago del IVA de este mes ya está registrado.")
            report = self._vat_report(connection, residence, month)
            if report.net <= 0:
                raise ValueError("No hay IVA pendiente de pago en este mes.")
            if report.net != expected_amount:
                raise ValueError("El IVA del mes cambió. Actualiza el informe antes de pagar.")
            expense_id = self._insert_expense(
                connection, residence, f"Pago de IVA (16%) - {month:%m/%Y}",
                paid_on, "VES", bank, report.net, "", Decimal("0"),
            )
            connection.execute(
                "INSERT INTO vat_payments (residence, month, expense_id, reference, debit, credit) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (residence, month.isoformat(), expense_id, reference.strip(),
                 str(report.debit), str(report.credit)),
            )

    def deposit_date(self, resident_id: int, residence: int) -> date | None:
        deposit = self.deposit_details(resident_id, residence)
        return date.fromisoformat(deposit["paid_on"]) if deposit else None

    def deposit_details(
        self, resident_id: int, residence: int
    ) -> Deposit | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT d.paid_on, d.currency, d.amount, d.payment_method FROM deposits d "
                "JOIN residents r ON r.id = d.resident_id "
                "WHERE r.id = ? AND r.residence = ?",
                (resident_id, residence),
            ).fetchone()
        return (
            Deposit(
                paid_on=row["paid_on"],
                currency=row["currency"],
                amount=row["amount"],
                payment_method=row["payment_method"],
            )
            if row else None
        )

    def record_deposit(
        self, resident_id: int, residence: int, paid_on: date, payment_method: str,
        amount: Decimal,
    ) -> None:
        self._require_write()
        if payment_method not in {"Efectivo", "Binance", "Zelle"}:
            raise ValueError("Selecciona efectivo, Binance o Zelle para el depósito.")
        try:
            received_amount = Decimal(amount)
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError("El monto del depósito debe ser un número válido.") from error
        if not received_amount.is_finite() or received_amount <= 0:
            raise ValueError("El monto del depósito debe ser mayor que cero.")
        if paid_on > date.today():
            raise ValueError("La fecha del depósito no puede ser futura.")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            resident = connection.execute(
                "SELECT values_json FROM residents "
                "WHERE id = ? AND residence = ? AND discharged_on IS NULL",
                (resident_id, residence),
            ).fetchone()
            if resident is None:
                raise ValueError("No se encontró una residente activa en esta residencia.")
            if connection.execute(
                "SELECT 1 FROM deposits WHERE resident_id = ?", (resident_id,)
            ).fetchone():
                raise ValueError("El depósito único ya está registrado.")
            receipt_number = self._next_receipt_number(connection, residence, "deposit")
            receipt = generate_deposit_receipt(
                resident_id, residence, json.loads(resident["values_json"]),
                paid_on, received_amount, payment_method,
                receipt_number=receipt_number,
            )
            connection.execute(
                "INSERT INTO deposits (resident_id, paid_on, currency, amount, "
                "payment_method, receipt_pdf, receipt_number) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    resident_id, paid_on.isoformat(), "USD", str(received_amount),
                    payment_method, receipt, receipt_number,
                ),
            )

    def get_deposit_receipt(self, resident_id: int, residence: int) -> bytes | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT d.receipt_pdf FROM deposits d "
                "JOIN residents r ON r.id = d.resident_id "
                "WHERE r.id = ? AND r.residence = ?",
                (resident_id, residence),
            ).fetchone()
        return bytes(row["receipt_pdf"]) if row and row["receipt_pdf"] else None

    def receipt_number(
        self, resident_id: int, residence: int, due_date: date | None = None
    ) -> str | None:
        with self._connect() as connection:
            if due_date is None:
                row = connection.execute(
                    "SELECT d.receipt_number FROM deposits d "
                    "JOIN residents r ON r.id = d.resident_id "
                    "WHERE r.id = ? AND r.residence = ?",
                    (resident_id, residence),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT p.receipt_number FROM payments p "
                    "JOIN residents r ON r.id = p.resident_id "
                    "WHERE r.id = ? AND r.residence = ? AND p.due_date = ?",
                    (resident_id, residence, due_date.isoformat()),
                ).fetchone()
        return row["receipt_number"] if row else None

    def upgrade_legacy_receipts(self) -> int:
        self._require_write()
        updated = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for table, kind, identity in (
                ("deposits", "deposit", "resident_id"),
                ("payments", "monthly", "id"),
            ):
                rows = connection.execute(
                    f"SELECT t.*, r.residence, r.values_json FROM {table} t "
                    "JOIN residents r ON r.id = t.resident_id "
                    "WHERE t.receipt_pdf IS NOT NULL AND t.receipt_number IS NULL "
                    f"ORDER BY t.paid_on, t.{identity}"
                ).fetchall()
                for row in rows:
                    number = self._next_receipt_number(connection, row["residence"], kind)
                    values = json.loads(row["values_json"])
                    if kind == "deposit":
                        pdf = generate_deposit_receipt(
                            row["resident_id"], row["residence"], values,
                            date.fromisoformat(row["paid_on"]), Decimal(row["amount"]),
                            row["payment_method"], receipt_number=number,
                        )
                    else:
                        pdf = generate_monthly_receipt(
                            row["resident_id"], row["residence"], values,
                            date.fromisoformat(row["due_date"]),
                            date.fromisoformat(row["paid_on"]), Decimal(row["amount"]),
                            row["currency"], row["payment_method"],
                            Decimal(row["exchange_rate"]) if row["exchange_rate"] else None,
                            date.fromisoformat(row["exchange_rate_date"])
                            if row["exchange_rate_date"] else None,
                            amount_usd=Decimal(row["amount_usd"]) if row["amount_usd"] else None,
                            monthly_amount_usd=Decimal(row["monthly_amount_usd"])
                            if row["monthly_amount_usd"] else None,
                            late_fee_usd=Decimal(row["late_fee_usd"] or "0"),
                            receiving_bank=row["receiving_bank"],
                            receipt_number=number,
                        )
                    connection.execute(
                        f"UPDATE {table} SET receipt_pdf = ?, receipt_number = ? "
                        f"WHERE {identity} = ?",
                        (pdf, number, row[identity]),
                    )
                    updated += 1
        return updated

    def regenerate_deposit_receipt(self, resident_id: int, residence: int) -> None:
        self._require_write()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT d.paid_on, d.currency, d.amount, d.payment_method, "
                "d.receipt_number, r.values_json "
                "FROM deposits d JOIN residents r ON r.id = d.resident_id "
                "WHERE r.id = ? AND r.residence = ? AND r.discharged_on IS NULL",
                (resident_id, residence),
            ).fetchone()
            if row is None:
                raise ValueError("No se encontró un depósito de una residente activa.")
            if (
                row["currency"] != "USD"
                or not row["amount"]
                or row["payment_method"] not in {"Efectivo", "Binance", "Zelle"}
            ):
                raise ValueError(
                    "Este depósito antiguo no tiene moneda, monto o forma de pago "
                    "completos. No se puede generar un recibo sin esos datos."
                )
            amount = Decimal(row["amount"])
            if not amount.is_finite() or amount <= 0:
                raise ValueError("El monto guardado del depósito no es válido.")
            number = (
                row["receipt_number"]
                or self._next_receipt_number(connection, residence, "deposit")
            )
            receipt = generate_deposit_receipt(
                resident_id, residence, json.loads(row["values_json"]),
                date.fromisoformat(row["paid_on"]), amount, row["payment_method"],
                receipt_number=number,
            )
            connection.execute(
                "UPDATE deposits SET receipt_pdf = ?, receipt_number = ? WHERE resident_id = ?",
                (receipt, number, resident_id),
            )

    def deposit_refund(self, resident_id: int, residence: int) -> DepositRefund | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT f.* FROM deposit_refunds f JOIN residents r ON r.id = f.resident_id "
                "WHERE r.id = ? AND r.residence = ?", (resident_id, residence),
            ).fetchone()
        if row is None:
            return None
        return DepositRefund(
            refunded_on=row["refunded_on"], currency=row["currency"],
            deposit_amount=row["deposit_amount"], deduction=row["deduction"],
            deduction_reason=row["deduction_reason"], amount=row["amount"],
            keys_returned=bool(row["keys_returned"]), inspection_ok=bool(row["inspection_ok"]),
            payment_method=row["payment_method"], receipt_number=row["receipt_number"],
            receipt_pdf=bytes(row["receipt_pdf"]),
        )

    def discharge(
        self, resident_id: int, residence: int, discharged_on: date, reason: str,
        *, keys_returned: bool | None = None, inspection_ok: bool | None = None,
        deduction: Decimal = Decimal("0"), deduction_reason: str = "",
        refund_method: str | None = None, refund_confirmed: bool = False,
    ) -> None:
        self._require_write()
        if not reason.strip():
            raise ValueError("Indica el motivo de la baja.")
        if discharged_on > date.today():
            raise ValueError("La fecha de baja no puede ser futura.")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            resident = connection.execute(
                "SELECT values_json FROM residents WHERE id = ? AND residence = ? "
                "AND discharged_on IS NULL", (resident_id, residence),
            ).fetchone()
            if resident is None:
                raise ValueError("La residente ya está de baja o no existe.")
            deposit = connection.execute(
                "SELECT * FROM deposits WHERE resident_id = ?", (resident_id,),
            ).fetchone()
            if deposit is not None:
                if deposit["currency"] != "USD" or not deposit["amount"]:
                    raise ValueError(
                        "El depósito anterior no tiene monto o moneda completos. "
                        "Completa esos datos antes de registrar la devolución y la baja."
                    )
                try:
                    original = Decimal(deposit["amount"])
                    discount = Decimal(deduction)
                    valid = (
                        original.is_finite() and original > 0
                        and original == original.quantize(Decimal("0.01"))
                        and discount.is_finite() and 0 <= discount <= original
                        and discount == discount.quantize(Decimal("0.01"))
                    )
                except (InvalidOperation, TypeError, ValueError) as error:
                    raise ValueError("El monto del depósito o del descuento no es válido.") from error
                if not valid:
                    raise ValueError(
                        "El descuento debe estar entre cero y el depósito, con máximo dos decimales."
                    )
                if type(keys_returned) is not bool or type(inspection_ok) is not bool:
                    raise ValueError("Verifica la entrega de llaves y el estado de la habitación.")
                if not keys_returned or not inspection_ok:
                    if discount <= 0:
                        raise ValueError(
                            "Si faltan llaves o hay novedades, indica el descuento de la administración."
                        )
                if discount > 0 and not deduction_reason.strip():
                    raise ValueError("Indica el motivo del descuento.")
                if discount == 0 and deduction_reason.strip():
                    raise ValueError("Hay un motivo de descuento, pero el descuento es cero.")
                if refund_method not in {"Efectivo", "Binance", "Zelle"}:
                    raise ValueError("Selecciona efectivo, Binance o Zelle para la devolución.")
                if not refund_confirmed:
                    raise ValueError("Confirma la entrega del depósito y el descuento aplicado.")
                if discharged_on < date.fromisoformat(deposit["paid_on"]):
                    raise ValueError("La devolución no puede ser anterior al pago del depósito.")
                number = self._next_receipt_number(connection, residence, "refund")
                pdf = generate_refund_receipt(
                    residence, json.loads(resident["values_json"]), discharged_on,
                    original, discount, deduction_reason.strip(), keys_returned,
                    inspection_ok, refund_method, receipt_number=number,
                )
                connection.execute(
                    "INSERT INTO deposit_refunds (resident_id, refunded_on, currency, "
                    "deposit_amount, deduction, deduction_reason, amount, keys_returned, "
                    "inspection_ok, payment_method, receipt_number, receipt_pdf) "
                    "VALUES (?, ?, 'USD', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (resident_id, discharged_on.isoformat(), str(original), str(discount),
                     deduction_reason.strip(), str(original - discount), keys_returned,
                     inspection_ok, refund_method, number, pdf),
                )
            cursor = connection.execute(
                "UPDATE residents SET discharged_on = ?, discharge_reason = ? "
                "WHERE id = ? AND residence = ? AND discharged_on IS NULL",
                (discharged_on.isoformat(), reason.strip(), resident_id, residence),
            )
            if cursor.rowcount != 1:
                raise ValueError("La residente ya está de baja o no existe.")
