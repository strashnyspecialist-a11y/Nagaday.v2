"""
check_and_notify.py

Runs on a schedule via GitHub Actions. Each run:
  1. Asks Supabase for reminders that are due (datetime <= now, not completed, not yet notified)
  2. Sends a Telegram message for each
  3. If the reminder repeats, advances it to its next occurrence and clears notified_at.
     If it doesn't repeat, marks notified_at so it won't fire again.

Requires these environment variables (set as GitHub Actions secrets):
  SUPABASE_URL, SUPABASE_ANON_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

The workflow also sets TZ=Europe/Warsaw so that datetime.now()/local date math
below matches Warsaw wall-clock time — the same assumption index.html makes
about the browser's local timezone.

Uses only the Python standard library (urllib), so no `pip install` step is
needed in the workflow.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

if not all([SUPABASE_URL, SUPABASE_ANON_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    print(
        "Відсутні необхідні змінні середовища: SUPABASE_URL, SUPABASE_ANON_KEY, "
        "TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID",
        file=sys.stderr,
    )
    sys.exit(1)

DAY_ORDER = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]  # matches JS Date.getDay()


def sb_headers(extra=None):
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        headers.update(extra)
    return headers


def http_request(url, method="GET", headers=None, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return e.code, raw


def fetch_due_reminders():
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    url = (
        f"{SUPABASE_URL}/rest/v1/reminders?select=*&completed=eq.false"
        f"&notified_at=is.null&datetime=lte.{urllib.parse.quote(now_iso)}"
    )
    status, body = http_request(url, headers=sb_headers())
    if status != 200:
        raise RuntimeError(f"Supabase fetch failed: {status} {body}")
    return body


def patch_reminder(reminder_id, patch):
    url = f"{SUPABASE_URL}/rest/v1/reminders?id=eq.{reminder_id}"
    status, body = http_request(
        url,
        method="PATCH",
        headers=sb_headers({"Prefer": "return=minimal"}),
        body=patch,
    )
    if status not in (200, 204):
        raise RuntimeError(f"Supabase patch failed: {status} {body}")


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    status, body = http_request(
        url,
        method="POST",
        headers={"Content-Type": "application/json"},
        body={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
    )
    if status != 200:
        raise RuntimeError(f"Telegram send failed: {status} {body}")


def escape_html(s):
    if not s:
        return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_message(r):
    lines = [f"🔔 <b>{escape_html(r.get('title'))}</b>"]
    if r.get("note"):
        lines.append(escape_html(r["note"]))
    return "\n".join(lines)


def parse_iso(s):
    # Supabase returns ISO timestamps like 2026-09-15T08:00:00+00:00 or with Z
    s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def add_months(d, months):
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(
        d.day,
        [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
         31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1],
    )
    return d.replace(year=year, month=month, day=day)


def weekday_sun(d):
    # Python's weekday(): Mon=0..Sun=6. We want Sun=0..Sat=6 (like JS getDay()).
    return (d.weekday() + 1) % 7


def add_occurrence(from_date, rec):
    """Mirrors addOccurrence() in index.html / check-reminders.mjs."""
    interval = max(1, int(rec.get("interval") or 1))
    rtype = rec.get("type")
    days = rec.get("days") or []

    if rtype == "daily":
        return from_date + timedelta(days=interval)
    elif rtype == "weekly":
        if days:
            for i in range(1, 9):
                cand = from_date + timedelta(days=i)
                if DAY_ORDER[weekday_sun(cand)] in days:
                    return cand
            return from_date + timedelta(days=7 * interval)
        return from_date + timedelta(days=7 * interval)
    elif rtype == "monthly":
        return add_months(from_date, interval)
    elif rtype == "yearly":
        try:
            return from_date.replace(year=from_date.year + interval)
        except ValueError:
            # Feb 29 on a non-leap target year
            return from_date.replace(year=from_date.year + interval, day=28)
    return from_date


def main():
    try:
        due = fetch_due_reminders()
    except Exception as e:
        print(f"Помилка отримання нагадувань з Supabase: {e}", file=sys.stderr)
        sys.exit(1)

    if not due:
        print("Немає нагадувань, що настали.")
        return

    now = datetime.now(timezone.utc)

    for r in due:
        try:
            send_telegram_message(format_message(r))
            print(f"Надіслано в Telegram: {r.get('title')}")
        except Exception as e:
            print(f'Не вдалося надіслати "{r.get("title")}": {e}', file=sys.stderr)
            # Don't touch the row if sending failed — try again next run.
            continue

        rec = {
            "type": r.get("recurrence_type") or "none",
            "interval": r.get("recurrence_interval") or 1,
            "days": r.get("recurrence_days") or [],
        }

        try:
            if rec["type"] != "none":
                current = parse_iso(r["datetime"])
                nxt = add_occurrence(current, rec)
                guard = 0
                while nxt <= now and guard < 2000:
                    nxt = add_occurrence(nxt, rec)
                    guard += 1
                patch_reminder(r["id"], {"datetime": nxt.isoformat(), "notified_at": None})
            else:
                patch_reminder(r["id"], {"notified_at": now.isoformat()})
        except Exception as e:
            print(f'Не вдалося оновити рядок "{r.get("title")}" після надсилання: {e}', file=sys.stderr)


if __name__ == "__main__":
    main()
