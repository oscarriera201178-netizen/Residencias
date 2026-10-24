from datetime import date, datetime
from zoneinfo import ZoneInfo


def billing_today() -> date:
    return datetime.now(ZoneInfo("America/Caracas")).date()


def next_month_start(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def payment_deadline(due: date) -> date:
    return due.replace(day=5) if due.day == 1 else due


def late_payment_days(due: date, paid_on: date) -> int:
    return max(0, (paid_on - payment_deadline(due)).days)
