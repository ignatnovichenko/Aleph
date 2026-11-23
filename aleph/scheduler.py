"""Durable proactive Telegram delivery and shared feed cache."""
import asyncio
from datetime import datetime, timedelta
import json
import logging
from zoneinfo import ZoneInfo

import aiohttp

from .feeds import fetch_feed
from .routines import SourceUnavailable, eligible, format_routine, queue_routines
from . import initiative, reminders, saved_results
from .store import UTC, timestamp

log = logging.getLogger(__name__)


def quiet(clock, start, end):
    if start == end:
        return False
    if start < end:
        return start <= clock < end
    return clock >= start or clock < end


def russian(user):
    return not user["language_code"] or user["language_code"].lower().startswith(("ru", "uk", "be", "kk"))


def telegram_text(text):
    # Bot API limits use UTF-16 code units. Never split a surrogate pair.
    return text.encode("utf-16-le")[:8000].decode("utf-16-le", errors="ignore")


def digest_body(store, user, local):
    uid = user["id"]
    ru = russian(user)
    today = local.date()
    dated, undated = [], []
    for task in store.list_tasks(uid, include_done=False):
        if not task["due_at"]:
            undated.append(task)
            continue
        at = datetime.fromisoformat(task["due_at"]).astimezone(local.tzinfo)
        if at.date() <= today:
            label = at.strftime("%H:%M") if at.date() == today else ("просрочено" if ru else "overdue")
            dated.append(f"• {label} — {task['title']}")
    sections = []
    if dated:
        sections.append(("В плане на сегодня" if ru else "On your agenda") + "\n" + "\n".join(dated[:5]))
    elif undated:
        sections.append(("Можно начать с этой задачи" if ru else "One thing you could start with") +
                        "\n• " + undated[0]["title"])
    headlines = []
    for source in store.feed_items(uid):
        if source["items"] and local.astimezone(UTC) - datetime.fromisoformat(source["refreshed_at"]) < timedelta(hours=24):
            top = source["items"][0]
            headlines.append(f"{source['title']} — {top['title']}" + ("\n" + top["url"] if top["url"] else ""))
    if headlines:
        sections.append(("Из твоих источников" if ru else "From your feeds") + "\n" + "\n\n".join(headlines[:3]))
    if not sections:
        return ""
    greeting = "Вот твой обзор на сегодня." if ru else "Here's your daily brief."
    return telegram_text(greeting + "\n\n" + "\n\n".join(sections))


def queue_messages(store, now):
    queue_routines(store, now)
    db = store.db
    now_s = timestamp(now)
    due = db.execute("""SELECT t.id,t.user_id,t.remind_at AS due_at FROM tasks t JOIN users u ON u.id=t.user_id
        WHERE t.status='open' AND t.remind_at<=? AND COALESCE(u.blocked_until,'')<=? AND NOT EXISTS
        (SELECT 1 FROM deliveries WHERE key='task:'||t.id||':'||t.remind_at AND status<>'cancelled')
        ORDER BY t.remind_at LIMIT 100""", (now_s, now_s)).fetchall()
    for task in due:
        key = f"task:{task['id']}:{task['due_at']}"
        db.execute("""INSERT INTO deliveries(key,user_id,kind,task_id,due_at,body,available_at)
            VALUES(?,?,'reminder',?,?,'',?) ON CONFLICT(key) DO UPDATE SET
            status='pending',available_at=excluded.available_at WHERE deliveries.status='cancelled'""", (key, task["user_id"], task["id"], task["due_at"], now_s))

    users = db.execute("""SELECT * FROM users u WHERE proactive=1 AND daily_digest=1 AND COALESCE(blocked_until,'')<=?
            AND (EXISTS(SELECT 1 FROM tasks WHERE user_id=u.id AND status='open')
              OR EXISTS(SELECT 1 FROM feeds WHERE user_id=u.id))""", (now_s,)).fetchall()
    for user in users:
        local = now.astimezone(ZoneInfo(user["timezone"]))
        clock = local.strftime("%H:%M")
        if clock < user["digest_time"] or quiet(clock, user["quiet_start"], user["quiet_end"]):
            continue
        created = datetime.fromisoformat(user["created_at"]).astimezone(local.tzinfo)
        if created.date() == local.date() and created.strftime("%H:%M") > user["digest_time"]:
            continue
        key = f"digest:{user['id']}:{local.date()}"
        if db.execute("SELECT 1 FROM deliveries WHERE key=? AND status<>'cancelled'", (key,)).fetchone():
            continue
        body = digest_body(store, user, local)
        if body:
            db.execute("""INSERT INTO deliveries(key,user_id,kind,body,available_at)
                VALUES(?,?,'digest',?,?) ON CONFLICT(key) DO UPDATE SET
                status='pending',body=excluded.body,available_at=excluded.available_at,attempts=0
                WHERE deliveries.status='cancelled'""", (key, user["id"], body, now_s))


def delivery_text(store, item, now):
    user = store.db.execute("SELECT * FROM users WHERE id=?", (item["user_id"],)).fetchone()
    if item["kind"] == "price":
        return item["body"] if store.db.execute("SELECT 1 FROM tasks WHERE id=? AND status='open' AND user_id=?", (item["task_id"],user["id"])).fetchone() else None
    if item["kind"] == "mail":
        return item["body"] if store.db.execute("SELECT 1 FROM gmail_rules WHERE task_id=? AND matched IS NOT NULL", (item["task_id"],)).fetchone() else None
    if item["kind"] == "initiative":
        if not user["proactive"] or user["initiative"]=="quiet":
            return None
        local = now.astimezone(ZoneInfo(user["timezone"]))
        if quiet(local.strftime("%H:%M"), user["quiet_start"], user["quiet_end"]):
            return ""
        return next((body for tag,body in initiative.candidates(store,user,now) if item["key"]==f"initiative:{user['id']}:{tag}"), None)
    if item["kind"] == "digest":
        local = now.astimezone(ZoneInfo(user["timezone"]))
        if not user["proactive"] or not user["daily_digest"] or not item["key"].endswith(":" + str(local.date())):
            return None
        clock = local.strftime("%H:%M")
        if clock < user["digest_time"] or quiet(clock, user["quiet_start"], user["quiet_end"]):
            return ""
        return digest_body(store, user, local) or None
    try:
        task = store.get_task(item["user_id"], item["task_id"])
    except KeyError:
        return None
    if task["status"] != "open" or task["remind_at"] != item["due_at"]:
        return None
    prefix = "Напоминаю" if russian(user) else "Reminder"
    return telegram_text(f"{prefix}: {task['title']}" + ("\n\n" + task["notes"][:3200] if task["notes"] else ""))


async def deliver(store, session, token, *, now=None, weather=None):
    now = now or datetime.now(UTC)
    now_s = timestamp(now)
    db = store.db
    rows = db.execute("""SELECT * FROM (SELECT d.*, ROW_NUMBER() OVER (
            PARTITION BY d.user_id ORDER BY CASE d.kind WHEN 'reminder' THEN 0 ELSE 1 END,d.available_at,d.key
        ) AS user_order FROM deliveries d JOIN users u ON u.id=d.user_id
        WHERE d.status IN ('pending','sending') AND d.available_at<=? AND COALESCE(u.blocked_until,'')<=?)
        WHERE user_order=1 ORDER BY available_at LIMIT 40""", (now_s, now_s)).fetchall()
    for item in rows:
        uid, key = item["user_id"], item["key"]
        claimed = db.execute("""UPDATE deliveries SET status='sending',attempts=attempts+1,available_at=?
            WHERE key=? AND status IN ('pending','sending') AND available_at<=?""",
            (timestamp(now + timedelta(minutes=10)), key, now_s)).rowcount
        if not claimed:
            continue
        retry_after = min(3600, 15 * (2 ** min(item["attempts"], 8)))
        try:
            if item["kind"] == "routine":
                routine = eligible(store,item,now)
                text = routine
                if routine:
                    if routine["kind"] == "weather" and weather is None:
                        from .llm import Aleph
                        weather = Aleph(session, "").weather
                    try:
                        text = telegram_text(await format_routine(store,routine,now,weather))
                    except (SourceUnavailable,aiohttp.ClientError,TimeoutError,ValueError):
                        if item["attempts"] < 2:
                            raise SourceUnavailable from None
                        text = (("Не удалось обновить данные для «" + routine["title"] + "».") if russian(routine)
                                else ("I couldn't refresh the data for “" + routine["title"] + "”."))
                    # A user may pause/delete a routine during an external fetch.
                    current = eligible(store,item,now)
                    if not current:
                        text = current
            else:
                text = delivery_text(store, item, now)
            if text is None:
                db.execute("UPDATE deliveries SET status='cancelled' WHERE key=?", (key,))
                continue
            if not text:
                db.execute("""UPDATE deliveries SET status='pending',attempts=attempts-1,available_at=?
                    WHERE key=?""", (timestamp(now + timedelta(minutes=1)), key))
                continue
            params = {"chat_id": uid, "text": text, "link_preview_options": {"is_disabled": True}}
            if item["kind"] == "reminder":
                params["reply_markup"] = reminders.keyboard(item["task_id"], item["due_at"], russian(store.get_user(uid)))
            async with session.post(f"https://api.telegram.org/bot{token}/sendMessage", json=params, timeout=aiohttp.ClientTimeout(total=15), allow_redirects=False) as response:
                try:
                    data = await response.json(content_type=None)
                except (ValueError, UnicodeError):
                    data = {}
                if response.status == 200 and data.get("ok"):
                    db.execute("UPDATE deliveries SET status='sent' WHERE key=?", (key,))
                    store.add_history(uid, "assistant", text)
                    continue
                if response.status == 403 or (response.status == 400 and "chat not found" in str(data.get("description", "")).lower()):
                    db.execute("UPDATE users SET blocked_until=? WHERE id=?", (timestamp(now + timedelta(days=1)), uid))
                    retry_after = 86400
                if response.status == 429:
                    retry_after = max(retry_after, int(data.get("parameters", {}).get("retry_after", 60)))
        except SourceUnavailable:
            retry_after = 180
        except (aiohttp.ClientError, TimeoutError):
            pass
        db.execute("UPDATE deliveries SET status='pending',available_at=? WHERE key=?",
                   (timestamp(now + timedelta(seconds=retry_after)), key))


async def refresh_feeds(store, session, now):
    due = store.db.execute("""SELECT DISTINCT f.url FROM feeds f LEFT JOIN feed_cache c ON c.url=f.url
        WHERE c.refreshed_at IS NULL OR c.refreshed_at<?""", (timestamp(now - timedelta(minutes=30)),)).fetchall()

    async def update(url):
        try:
            items = await fetch_feed(session, url)
        except Exception as error:
            # aiohttp exceptions can include a URL. Log only the error type.
            log.info("RSS update postponed (%s)", type(error).__name__)
            return
        store.db.execute("""INSERT INTO feed_cache(url,items,refreshed_at) VALUES(?,?,?)
            ON CONFLICT(url) DO UPDATE SET items=excluded.items,refreshed_at=excluded.refreshed_at""",
            (url, json.dumps(items, ensure_ascii=False), timestamp(now)))

    await asyncio.gather(*(update(row["url"]) for row in due))


async def run_scheduler(store, session, token, *, weather=None):
    next_feeds = datetime.min.replace(tzinfo=UTC)
    while True:
        try:
            now = datetime.now(UTC)
            if now >= next_feeds:
                initiative.queue(store, now)
                saved_results.prune(store, now)
                await refresh_feeds(store, session, now)
                next_feeds = now + timedelta(minutes=5)
            queue_messages(store, now)
            await deliver(store, session, token, weather=weather)
        except (aiohttp.ClientError, TimeoutError) as error:
            log.warning("Scheduler waiting to retry (%s)", type(error).__name__)
        except Exception as error:
            log.error("Scheduler tick failed (%s)", type(error).__name__)
        await asyncio.sleep(5)
