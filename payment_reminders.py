import argparse
import logging
import os
import re
import smtplib
import sqlite3
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from zoneinfo import ZoneInfo

from resident_store import ALERT_DAYS, Resident, ResidentStore
from payment_schedule import payment_deadline


LOGGER = logging.getLogger(__name__)
VENEZUELA = ZoneInfo("America/Caracas")
SEND_HOUR = 8
RESIDENCE_NAMES = (
    "PENSIÓN QUINTA CARMEN LUISA C.A.",
    "RESIDENCIAS LAS ACACIAS C.A.",
)
EMAIL_PATTERN = re.compile(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+")


@dataclass(frozen=True)
class MailConfig:
    sender: str
    password: str = field(repr=False)

    @classmethod
    def from_environment(cls) -> "MailConfig":
        sender = os.environ.get("REMINDER_SMTP_USER", "farfanlirio16@gmail.com").strip()
        password = os.environ.get("REMINDER_SMTP_PASSWORD", "").strip()
        if not EMAIL_PATTERN.fullmatch(sender) or not sender.lower().endswith("@gmail.com"):
            raise ValueError("REMINDER_SMTP_USER debe ser una dirección de Gmail válida.")
        if not password:
            raise ValueError(
                "Falta REMINDER_SMTP_PASSWORD: configura la contraseña de aplicación "
                "de Gmail como secreto del Codespace. No uses la contraseña normal."
            )
        return cls(sender, password)


def build_reminder(resident: Resident, today: datetime, config: MailConfig) -> EmailMessage:
    recipient = resident.values.get("email", "").strip()
    if not recipient.isascii() or not EMAIL_PATTERN.fullmatch(recipient):
        raise ValueError("La residente no tiene un correo electrónico válido.")
    name = " ".join(resident.values["student_name"].split())
    residence = RESIDENCE_NAMES[resident.residence]
    due = resident.next_payment
    local_date = today.astimezone(VENEZUELA).date()
    if due.day == 1 and due <= local_date <= payment_deadline(due):
        deadline = f"está pendiente; plazo del 1 al 5 de {due:%m/%Y}"
    elif due < local_date:
        deadline = f"venció el {due:%d/%m/%Y}"
    elif due == local_date:
        deadline = f"vence hoy {due:%d/%m/%Y}"
    else:
        deadline = f"vence el próximo {due:%d/%m/%Y}"
    message = EmailMessage()
    message["From"] = formataddr((residence, config.sender))
    message["To"] = recipient
    message["Subject"] = "Recordatorio de pago de mensualidad de habitación"
    message["Date"] = today
    message["Message-ID"] = make_msgid(domain="gmail.com")
    message.set_content(
        f"Hola {name}, te saludamos de {residence}. "
        f"Te recordamos que tu pago de MENSUALIDAD DE HABITACIÓN {deadline}. "
        "EVITA PAGOS POR MORA. ¡Muchas gracias!"
    )
    return message


def send_reminder(message: EmailMessage, config: MailConfig) -> None:
    with smtplib.SMTP_SSL(
        "smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30,
    ) as smtp:
        smtp.login(config.sender, config.password)
        refused = smtp.send_message(message)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


def run_reminders(
    store: ResidentStore, now: datetime, config: MailConfig | None = None,
    *, dry_run: bool = True,
) -> dict[str, int]:
    if now.tzinfo is None:
        raise ValueError("La fecha del proceso debe incluir zona horaria.")
    local_now = now.astimezone(VENEZUELA)
    counts = {"eligible": 0, "sent": 0, "failed": 0, "unknown": 0, "skipped": 0}
    if local_now.hour < SEND_HOUR:
        return counts
    if not dry_run and config is None:
        raise ValueError("Se requiere configurar Gmail antes de enviar recordatorios.")
    today = local_now.date()
    for residence in (0, 1):
        for resident in store.list_residents(residence):
            if (resident.next_payment - today).days > ALERT_DAYS:
                continue
            counts["eligible"] += 1
            if dry_run:
                continue
            if config is None:
                raise ValueError("Se requiere configurar Gmail.")
            if not store.claim_payment_reminder(resident, today, local_now.isoformat()):
                counts["skipped"] += 1
                continue
            try:
                message = build_reminder(resident, local_now, config)
            except ValueError:
                store.finish_payment_reminder(
                    resident.id, resident.next_payment, today, "failed", local_now.isoformat(),
                )
                counts["failed"] += 1
                LOGGER.error(
                    "Recordatorio no enviado: correo inválido en ingreso %s, residencia %s.",
                    resident.id, residence,
                )
                continue
            try:
                send_reminder(message, config)
            except (smtplib.SMTPException, OSError) as error:
                store.finish_payment_reminder(
                    resident.id, resident.next_payment, today, "unknown", local_now.isoformat(),
                )
                counts["unknown"] += 1
                LOGGER.error(
                    "No se confirmó el recordatorio del ingreso %s, residencia %s (%s). "
                    "No se reintentará hoy para evitar duplicados.",
                    resident.id, residence, type(error).__name__,
                )
            else:
                store.finish_payment_reminder(
                    resident.id, resident.next_payment, today, "sent", local_now.isoformat(),
                )
                counts["sent"] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Recordatorios diarios de mensualidad a las 08:00 de Venezuela.")
    parser.add_argument("--send", action="store_true", help="Habilitar el envío real por Gmail.")
    parser.add_argument("--watch", action="store_true", help="Mantener el proceso activo; revisar cada minuto.")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if arguments.watch and not arguments.send:
        parser.error("--watch requiere --send. Sin opciones solo se simula, sin enviar.")
    try:
        config = MailConfig.from_environment() if arguments.send else None
        store = ResidentStore()
        while True:
            counts = run_reminders(
                store, datetime.now(VENEZUELA), config, dry_run=not arguments.send,
            )
            LOGGER.info("%s: %s", "Envío" if arguments.send else "Simulación sin correos", counts)
            if not arguments.watch:
                return 1 if counts["failed"] or counts["unknown"] else 0
            time.sleep(60)
    except (ValueError, sqlite3.Error, OSError) as error:
        LOGGER.error("El proceso de recordatorios se detuvo (%s): %s", type(error).__name__, error)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
