"""
Перевіряє всі нагадування в базі Supabase (таблиця "reminders") і шле
повідомлення в Telegram для тих, чий час настав.
Для повторюваних нагадувань після надсилання одразу розраховує наступне
спрацювання (та сама логіка, що й у index.html).

Запускається за розкладом через GitHub Actions
(див. .github/workflows/check.yml).

Потрібні змінні середовища (GitHub Secrets):
  SUPABASE_URL        — напр. https://xxxxx.supabase.co
  SUPABASE_ANON_KEY    — publishable/anon ключ (sb_publishable_...)
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  TIMEZONE             — напр. Europe/Kyiv (часовий пояс, у якому введені
                          дати нагадувань; за замовчуванням Europe/Kyiv)
"""

import os
import sys
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TIMEZONE = os.environ.get("TIMEZONE", "Europe/Warsaw")

TZ = ZoneInfo(TIMEZONE)

DAY_ORDER = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]


def http_request(url, method="GET", headers=None, body=None):
    headers = headers or {}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode("utf-8", errors="ignore")


def fetch_reminders():
    url = f"{SUPABASE_URL}/rest/v1/reminders?select=id,data"
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
    }
    status, data = http_request(url, headers=headers)
    if status != 200:
        print(f"Помилка завантаження нагадувань: {status} {data}", file=sys.stderr)
        return []
    return data


def patch_reminder_data(row_id, new_data):
    url = f"{SUPABASE_URL}/rest/v1/reminders?id=eq.{row_id}"
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    status, _ = http_request(url, method="PATCH", headers=headers, body={"data": new_data})
    if status not in (200, 204):
        print(f"Помилка оновлення нагадування {row_id}: {status}", file=sys.stderr)


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    body = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    headers = {"Content-Type": "application/json"}
    status, resp = http_request(url, method="POST", headers=headers, body=body)
    if status != 200:
        print(f"Помилка надсилання в Telegram: {status} {resp}", file=sys.stderr)
        return False
    return True


def parse_local_datetime(dt_str):
    """Формат з index.html: 'YYYY-MM-DDTHH:MM' — трактуємо як локальний
    час користувача (TIMEZONE), повертаємо aware datetime."""
    if not dt_str:
        return None
    try:
        naive = datetime.strptime(dt_str[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return naive.replace(tzinfo=TZ)


def to_local_input_string(dt_aware):
    local = dt_aware.astimezone(TZ)
    return local.strftime("%Y-%m-%dT%H:%M")


def add_occurrence(from_dt, recurrence):
    """Та сама логіка, що й addOccurrence() в index.html."""
    rec_type = recurrence.get("type", "none")
    interval = max(1, int(recurrence.get("interval") or 1))
    days = recurrence.get("days") or []

    if rec_type == "daily":
        return from_dt + timedelta(days=interval)

    if rec_type == "weekly":
        if days:
            for i in range(1, 9):
                cand = from_dt + timedelta(days=i)
                if DAY_ORDER[cand.weekday() == 6 and 0 or (cand.isoweekday() % 7)] in days:
                    pass
            # Простіше й надійніше: перевіряємо day-name напряму через strftime
            for i in range(1, 9):
                cand = from_dt + timedelta(days=i)
                day_name = DAY_ORDER[int(cand.strftime("%w"))]  # %w: 0=Sunday
                if day_name in days:
                    return cand
            return from_dt + timedelta(weeks=interval)
        return from_dt + timedelta(weeks=interval)

    if rec_type == "monthly":
        month = from_dt.month - 1 + interval
        year = from_dt.year + month // 12
        month = month % 12 + 1
        day = min(from_dt.day, 28)  # спрощено, уникаємо помилок з короткими місяцями
        try:
            return from_dt.replace(year=year, month=month, day=from_dt.day)
        except ValueError:
            return from_dt.replace(year=year, month=month, day=day)

    if rec_type == "yearly":
        try:
            return from_dt.replace(year=from_dt.year + interval)
        except ValueError:
            return from_dt.replace(year=from_dt.year + interval, day=28)

    return from_dt


def main():
    rows = fetch_reminders()
    now = datetime.now(TZ)

    for row in rows:
        row_id = row["id"]
        data = row.get("data") or {}

        if data.get("completed"):
            continue
        if data.get("notifiedAt"):
            continue

        due = parse_local_datetime(data.get("datetime"))
        if due is None or due > now:
            continue

        title = data.get("title", "Нагадування")
        note = data.get("note")
        text = f"🔔 {title}"
        if note:
            text += f"\n{note}"

        sent = send_telegram_message(text)
        if not sent:
            continue

        recurrence = data.get("recurrence") or {"type": "none"}
        if recurrence.get("type") != "none":
            next_due = add_occurrence(due, recurrence)
            data["datetime"] = to_local_input_string(next_due)
            data["notifiedAt"] = None
        else:
            data["notifiedAt"] = now.isoformat()

        patch_reminder_data(row_id, data)
        print(f"Надіслано нагадування #{row_id}: {title}")


if __name__ == "__main__":
    main()
