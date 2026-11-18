"""Recurring private briefings. A routine reads a bounded data source at delivery time."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
GROUPS = {"daily": DAYS, "weekdays": DAYS[:5], "weekends": DAYS[5:]}
UTC = timezone.utc


def day_names(value):
    if not isinstance(value, str) or len(value) > 50:
        raise ValueError("Укажи дни недели.")
    value = value.strip().lower()
    if value in GROUPS:
        return GROUPS[value]
    chosen = {part.strip() for part in value.split(",")}
    if not chosen or chosen - set(DAYS):
        raise ValueError("Дни недели: mon, tue, wed, thu, fri, sat, sun, weekdays, weekends или daily.")
    return tuple(day for day in DAYS if day in chosen)


def recent_occurrence(routine, now, zone):
    tz = ZoneInfo(zone)
    today = now.astimezone(tz).date()
    clock = time.fromisoformat(routine["time"])
    since = datetime.fromisoformat(routine["active_since"]).replace(second=0, microsecond=0)
    for day in (today, today - timedelta(days=1)):
        # Only a routine intentionally deferred by overnight quiet hours can
        # cross midnight. A Friday forecast must not arrive on Saturday merely
        # because the worker restarted after Friday's scheduled time.
        if day != today and not (
            routine["quiet_start"] > routine["quiet_end"] and routine["time"] >= routine["quiet_start"]
        ):
            continue
        if DAYS[day.weekday()] not in day_names(routine["days"]):
            continue
        instant = datetime.combine(day, clock, tzinfo=tz).astimezone(UTC)
        # Each local date has at most one occurrence, including clock changes.
        if since <= instant <= now and now - instant < timedelta(hours=24):
            return day, instant
    return None


def queue_routines(store, now):
    from .store import timestamp
    now_s = timestamp(now)
    rows = store.db.execute("""SELECT r.*,u.timezone,u.quiet_start,u.quiet_end FROM routines r JOIN users u ON r.user_id=u.id
        WHERE r.active=1 AND r.deleted=0 AND u.proactive=1 AND COALESCE(u.blocked_until,'')<=?""", (now_s,))
    for row in rows:
        occurrence = recent_occurrence(row, now, row["timezone"])
        if occurrence is None:
            continue
        day, due = occurrence
        key = f"routine:{row['id']}:{day}"
        store.db.execute("""INSERT OR IGNORE INTO deliveries(key,user_id,kind,routine_id,due_at,body,available_at)
            VALUES(?,?,'routine',?,?,'',?)""", (key,row["user_id"],row["id"],timestamp(due),now_s))


def eligible(store, item, now):
    from .scheduler import quiet
    row = store.db.execute("""SELECT r.*,u.proactive,u.timezone,u.quiet_start,u.quiet_end,u.language_code
        FROM routines r JOIN users u ON u.id=r.user_id WHERE r.id=? AND r.user_id=?""",
                           (item["routine_id"],item["user_id"])).fetchone()
    if not row or row["deleted"] or not row["active"] or not row["proactive"]:
        return None
    occurrence = recent_occurrence(row, now, row["timezone"])
    if not occurrence or not item["key"].endswith(":" + str(occurrence[0])):
        return None
    clock = now.astimezone(ZoneInfo(row["timezone"])).strftime("%H:%M")
    return "" if quiet(clock,row["quiet_start"],row["quiet_end"]) else row


class SourceUnavailable(Exception):
    pass


def number(value):
    if type(value) not in (int, float):
        raise SourceUnavailable
    return round(value)


async def format_routine(store, routine, now, weather):
    from .scheduler import russian
    ru = russian(routine)
    uid = routine["user_id"]
    title = routine["title"]
    kind = routine["kind"]
    if kind == "reminder":
        return ("Напоминаю: " if ru else "Reminder: ") + title
    if kind == "weather":
        result = await weather(routine["city"], routine["country_code"])
        try:
            data = result["forecast"]
            current = number(data["current"]["temperature_2m"])
            low = number(data["daily"]["temperature_2m_min"][0])
            high = number(data["daily"]["temperature_2m_max"][0])
            precipitation = data["daily"].get("precipitation_probability_max", [None])[0]
        except (KeyError, TypeError, IndexError, OverflowError, ValueError) as error:
            raise SourceUnavailable from error
        city = routine["city"]
        if ru:
            result = f"{city} · сейчас {current} °C. Сегодня от {low} до {high} °C."
            if isinstance(precipitation,(int,float)):
                result += f" Вероятность осадков до {round(precipitation)}%."
        else:
            result = f"{city} · currently {current} °C. Today's range is {low} to {high} °C."
            if isinstance(precipitation,(int,float)):
                result += f" Chance of precipitation up to {round(precipitation)}%."
        return result + "\nhttps://open-meteo.com/"
    if kind == "news":
        sources = store.list_feeds(uid)
        if not sources:
            return ("Для этой подборки нужен источник. Напиши, например, «подключи NASA»." if ru else
                    "This brief needs a source. Try “subscribe to NASA”.")
        articles = []
        for source in store.feed_items(uid):
            fresh = now - datetime.fromisoformat(source["refreshed_at"]) <= timedelta(hours=1)
            if fresh and source["items"]:
                top = source["items"][0]
                articles.append(f"{source['title']} — {top['title']}" + ("\n" + top["url"] if top["url"] else ""))
        if not articles:
            raise SourceUnavailable
        return title + "\n\n" + "\n\n".join(articles[:5])
    tz = ZoneInfo(routine["timezone"])
    today = now.astimezone(tz).date()
    result = []
    for task in store.list_tasks(uid, include_done=False, project_id=routine["project_id"]):
        if task["due_at"]:
            due = datetime.fromisoformat(task["due_at"]).astimezone(tz)
            if due.date() > today:
                continue
            prefix = due.strftime("%H:%M") if due.date() == today else ("Просрочено" if ru else "Overdue")
            result.append(f"• {prefix} — {task['title']}")
        else:
            result.append("• " + task["title"])
        if len(result) == 6:
            break
    return title + "\n\n" + ("\n".join(result) or ("На сегодня задач нет." if ru else "Your schedule is clear today."))
