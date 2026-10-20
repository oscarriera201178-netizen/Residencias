import os
import smtplib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from payment_reminders import (
    MailConfig, RESIDENCE_NAMES, VENEZUELA, build_reminder, main, run_reminders,
    send_reminder,
)
from resident_store import ResidentStore


class PaymentReminderTests(unittest.TestCase):
    def setUp(self):
        login = patch("portal_auth.require_login", return_value=SimpleNamespace(
            role="administrator", can_write=True, require_write=lambda: None,
        ))
        login.start()
        self.addCleanup(login.stop)
        area = patch("portal_auth.select_residence_area", return_value=(0, 1))
        area.start()
        self.addCleanup(area.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = ResidentStore(Path(directory.name) / "test.sqlite3")
        self.now = datetime(2026, 10, 20, 8, tzinfo=VENEZUELA)
        self.config = MailConfig("sender@gmail.com", "test-password-not-real")
        self.sender = patch("payment_reminders.send_reminder")
        self.send = self.sender.start()
        self.addCleanup(self.sender.stop)
        self.sequence = 0

    def add(self, days=0, residence=0, email="resident@example.com"):
        self.sequence += 1
        due = self.now.date() + timedelta(days=days)
        return self.store.add(
            residence,
            {
                "student_name": "Residente Ficticia",
                "student_id": f"TEST-{self.sequence}",
                "email": email,
                "admission_date": due.strftime("%d/%m/%Y"),
                "room_number": "1",
                "bed_number": "2",
            },
            [False] * 8, b"%PDF-test", None,
        )

    def test_thresholds_in_both_residences_and_inactive_exclusion(self):
        for residence in (0, 1):
            for days in (-60, -1, 0, 1, 2, 3, 4):
                self.add(days, residence)
            inactive = self.add(-1, residence)
            self.store.discharge(inactive, residence, self.now.date(), "Fin")
        result = run_reminders(self.store, self.now, self.config, dry_run=False)
        self.assertEqual(result["eligible"], 12)
        self.assertEqual(result["sent"], 12)
        self.assertEqual(self.send.call_count, 12)
        for message, _ in (call.args for call in self.send.call_args_list):
            self.assertEqual(message["To"], "resident@example.com")
            self.assertIsNone(message["Cc"])
            self.assertIsNone(message["Bcc"])
            self.assertFalse(message.is_multipart())

    def test_text_uses_resident_residence_and_correct_deadline(self):
        for residence in (0, 1):
            for days, wording in (
                (-1, "venció el 19/10/2026"),
                (0, "vence hoy 20/10/2026"),
                (3, "vence el próximo 23/10/2026"),
            ):
                self.add(days, residence)
                resident = self.store.list_residents(residence)[-1]
                message = build_reminder(resident, self.now, self.config)
                body = message.get_content()
                self.assertIn("Hola RESIDENTE FICTICIA", body)
                self.assertIn(RESIDENCE_NAMES[residence], body)
                self.assertNotIn(RESIDENCE_NAMES[1 - residence], body)
                self.assertIn(f"MENSUALIDAD DE HABITACIÓN {wording}", body)
                self.assertIn("EVITA PAGOS POR MORA. ¡Muchas gracias!", body)
                self.assertIn(self.config.sender, message["From"])

    def test_before_eight_does_not_send_or_claim_and_utc_is_converted(self):
        self.add()
        result = run_reminders(
            self.store, datetime(2026, 10, 20, 11, 59, tzinfo=timezone.utc),
            self.config, dry_run=False,
        )
        self.assertEqual(result["eligible"], 0)
        self.send.assert_not_called()
        self.assertEqual(self.store.reminder_counts(0, self.now.date()), {})
        result = run_reminders(
            self.store, datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
            self.config, dry_run=False,
        )
        self.assertEqual(result["sent"], 1)

    def test_once_daily_persists_across_restart_and_next_day_sends_again(self):
        self.add(-1)
        run_reminders(self.store, self.now, self.config, dry_run=False)
        restored = ResidentStore(self.store.path)
        result = run_reminders(restored, self.now, self.config, dry_run=False)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.send.call_count, 1)
        result = run_reminders(
            restored, self.now + timedelta(days=1), self.config, dry_run=False,
        )
        self.assertEqual(result["sent"], 1)
        self.assertEqual(self.send.call_count, 2)

    def test_concurrent_workers_claim_only_once(self):
        self.add()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda _: run_reminders(self.store, self.now, self.config, dry_run=False),
                range(2),
            ))
        self.assertEqual(sum(result["sent"] for result in results), 1)
        self.send.assert_called_once()

    def test_stale_candidates_are_rechecked_after_payment_and_discharge(self):
        resident_id = self.add()
        resident = self.store.list_residents(0)[0]
        self.store.record_payment(
            resident_id, 0, resident.next_payment, self.now.date(),
            "USD", Decimal("100"), payment_method="Zelle",
        )
        self.assertFalse(self.store.claim_payment_reminder(
            resident, self.now.date(), self.now.isoformat(),
        ))
        self.assertEqual(
            run_reminders(self.store, self.now, self.config, dry_run=False)["eligible"], 0,
        )
        resident_id = self.add(-1)
        resident = self.store.list_residents(0)[0]
        self.store.discharge(resident_id, 0, self.now.date(), "Fin")
        self.assertFalse(self.store.claim_payment_reminder(
            resident, self.now.date(), self.now.isoformat(),
        ))
        self.send.assert_not_called()

    def test_new_due_after_partial_catchup_does_not_send_twice_same_day(self):
        resident_id = self.add(-60)
        run_reminders(self.store, self.now, self.config, dry_run=False)
        due = self.store.list_residents(0)[0].next_payment
        self.store.record_payment(
            resident_id, 0, due, self.now.date(), "USD", Decimal("100"),
            payment_method="Zelle",
        )
        result = run_reminders(self.store, self.now, self.config, dry_run=False)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.send.call_count, 1)

    def test_missing_or_invalid_email_is_reported_without_success(self):
        for email in ("", "invalid", "a@example.com\nBcc: other@example.com"):
            self.add(email=email)
        with self.assertLogs("payment_reminders", level="ERROR") as logs:
            result = run_reminders(self.store, self.now, self.config, dry_run=False)
        self.assertEqual(result["failed"], 3)
        self.assertEqual(result["sent"], 0)
        self.send.assert_not_called()
        self.assertEqual(self.store.reminder_counts(0, self.now.date()), {"failed": 3})
        self.assertNotIn("example.com", " ".join(logs.output))

    def test_smtp_failure_is_visible_and_never_retried_same_day(self):
        self.add()
        self.send.side_effect = smtplib.SMTPAuthenticationError(535, b"credentials")
        with self.assertLogs("payment_reminders", level="ERROR") as logs:
            result = run_reminders(self.store, self.now, self.config, dry_run=False)
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(result["sent"], 0)
        self.assertNotIn("credentials", " ".join(logs.output))
        run_reminders(self.store, self.now, self.config, dry_run=False)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.store.reminder_counts(0, self.now.date()), {"unknown": 1})

    def test_interrupted_send_is_not_retried_and_deletion_cascades_ledger(self):
        resident_id = self.add()
        resident = self.store.list_residents(0)[0]
        self.assertTrue(self.store.claim_payment_reminder(
            resident, self.now.date(), self.now.isoformat(),
        ))
        result = run_reminders(self.store, self.now, self.config, dry_run=False)
        self.assertEqual(result["skipped"], 1)
        self.send.assert_not_called()
        self.store.delete_admission(resident_id, 0)
        with self.store._connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM payment_reminders"
            ).fetchone()[0], 0)

    def test_dry_run_does_not_send_or_modify_ledger(self):
        self.add()
        result = run_reminders(self.store, self.now)
        self.assertEqual(result["eligible"], 1)
        self.send.assert_not_called()
        self.assertEqual(self.store.reminder_counts(0, self.now.date()), {})

    def test_configuration_is_required_and_password_is_not_represented(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "REMINDER_SMTP_PASSWORD"):
                MailConfig.from_environment()
        with patch.dict(os.environ, {
            "REMINDER_SMTP_PASSWORD": "not-a-real-password",
        }, clear=True):
            config = MailConfig.from_environment()
            self.assertEqual(config.sender, "farfanlirio16@gmail.com")
            self.assertNotIn("not-a-real-password", repr(config))
        with self.assertRaisesRegex(ValueError, "configurar Gmail"):
            run_reminders(self.store, self.now, dry_run=False)
        with self.assertRaisesRegex(ValueError, "zona horaria"):
            run_reminders(self.store, datetime(2026, 10, 20, 8))

    def test_gmail_transport_requires_verified_tls_and_authentication(self):
        self.add()
        message = build_reminder(self.store.list_residents(0)[0], self.now, self.config)
        with patch("payment_reminders.smtplib.SMTP_SSL") as smtp:
            client = smtp.return_value.__enter__.return_value
            client.send_message.return_value = {}
            send_reminder(message, self.config)
            client.login.assert_called_once_with(self.config.sender, self.config.password)
            client.send_message.assert_called_once_with(message)
            args, kwargs = smtp.call_args
            self.assertEqual(args, ("smtp.gmail.com", 465))
            self.assertTrue(kwargs["context"].check_hostname)
            self.assertEqual(kwargs["timeout"], 30)

    def test_cli_refuses_real_sending_without_secret(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "sys.argv", ["payment_reminders.py", "--send"],
        ), self.assertLogs("payment_reminders", level="ERROR"):
            self.assertEqual(main(), 1)
        self.send.assert_not_called()

    def test_pending_view_reports_missing_configuration_and_failed_attempts(self):
        self.add(-1)
        resident = self.store.list_residents(0)[0]
        today = datetime.now(VENEZUELA).date()
        self.store.claim_payment_reminder(resident, today, self.now.isoformat())
        self.store.finish_payment_reminder(
            resident.id, resident.next_payment, today, "unknown", self.now.isoformat(),
        )
        with patch.dict(os.environ, {}, clear=True), patch(
            "resident_store.ResidentStore", return_value=self.store,
        ):
            app = AppTest.from_file(
                str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=30,
            ).run()
            app.button(key="residencia_action_0_2").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any(
            "REMINDER_SMTP_PASSWORD" in message.value for message in app.tabs[0].info
        ))
        self.assertTrue(any(
            "sin confirmación" in message.value for message in app.tabs[0].warning
        ))
        self.send.assert_not_called()

    def test_reminders_do_not_change_financial_records_or_due_dates(self):
        self.add()
        before = self.store.list_residents(0)
        run_reminders(self.store, self.now, self.config, dry_run=False)
        self.assertEqual(before, self.store.list_residents(0))
        self.assertEqual(self.store.received_payments(0, self.now.date()), [])
        self.assertEqual(self.store.payment_history(before[0].id, 0), [])
        self.assertIsNone(self.store.deposit_date(before[0].id, 0))


if __name__ == "__main__":
    unittest.main()
