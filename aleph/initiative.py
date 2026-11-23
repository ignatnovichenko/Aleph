"""Select actionable changes from the user's chosen project materials and deadlines."""

from datetime import timedelta
import hashlib
import re
from zoneinfo import ZoneInfo


def keywords(value):
    stop = {
        "сейчас",
        "нужно",
        "важно",
        "важные",
        "когда",
        "чтобы",
        "проект",
        "работать",
        "please",
        "about",
        "project",
        "with",
        "that",
    }
    return set(re.findall(r"[\w-]{3,}", value.casefold())) - stop


def candidates(store, user, now):
    from .store import timestamp

    uid = user["id"]
    focus = keywords(user["focus"])
    ru = not user["language_code"].startswith("en")
    if focus and user["context_sources"]:
        rows = store.db.execute(
            """SELECT id,title,content,url,updated_at FROM project_sources
            WHERE user_id=? AND updated_at>=? AND status IN ('ready','partial') ORDER BY updated_at DESC LIMIT 30""",
            (uid, timestamp(now - timedelta(days=1))),
        )
        for row in rows:
            terms = focus & keywords(row["title"] + " " + row["content"][:5000])
            if terms:
                label = (
                    "Обновился материал по твоему приоритету" if ru else "A source matches your current focus"
                )
                key = hashlib.sha256(row["content"].encode()).hexdigest()[:16]
                yield (
                    f"source:{row['id']}:{key}",
                    f"{label} «{', '.join(sorted(terms)[:3])}»\n{row['title']}\n{row['url']}",
                )
    if focus and user["context_sources"]:
        from . import profile_sources

        profile = profile_sources.current(store, uid)
        for provider in ("calendar", "gmail", "drive", "notion"):
            for item in profile.get(provider, []):
                summary = item["title"]
                if provider == "calendar":
                    when = item["start"].get("dateTime") or item["start"].get("date") or ""
                    if when[:10] < now.date().isoformat():
                        continue
                    summary += " · " + when
                key = hashlib.sha256(
                    (provider + summary + item["url"] + item["excerpt"]).encode()
                ).hexdigest()[:24]
                reason = "По твоему приоритету" if ru else "Related to your focus"
                yield (
                    key,
                    f"{reason} «{user['focus'][:100]}» · {provider.title()}\n{summary}\n{item['url']}".strip(),
                )
    rows = store.db.execute(
        """SELECT t.id,t.title,t.due_at,p.name FROM tasks t LEFT JOIN projects p ON p.id=t.project_id
        WHERE t.user_id=? AND t.status='open' AND t.remind_at IS NULL AND t.due_at>? AND t.due_at<=?
        AND NOT EXISTS(SELECT 1 FROM scheduled_reports WHERE task_id=t.id) ORDER BY t.due_at LIMIT 30""",
        (uid, timestamp(now), timestamp(now + timedelta(hours=24))),
    )
    for row in rows:
        related = focus & keywords(row["title"] + " " + (row["name"] or ""))
        if related or user["initiative"] == "active":
            from datetime import datetime

            when = (
                datetime.fromisoformat(row["due_at"])
                .astimezone(ZoneInfo(user["timezone"]))
                .strftime("%d.%m %H:%M")
            )
            label = "Приближается срок" if ru else "Deadline coming up"
            yield f"due:{row['id']}:{row['due_at']}", f"{label} · {when}\n{row['title']}"


def queue(store, now):
    from .scheduler import quiet
    from .store import timestamp

    db = store.db
    for user in db.execute("""SELECT * FROM users WHERE proactive AND initiative!='quiet'
        AND (focus!='' OR initiative='active')"""):
        uid = user["id"]
        if quiet(
            now.astimezone(ZoneInfo(user["timezone"])).strftime("%H:%M"),
            user["quiet_start"],
            user["quiet_end"],
        ):
            continue
        interval = timedelta(hours=4 if user["initiative"] == "active" else 20)
        if db.execute(
            """SELECT 1 FROM deliveries WHERE user_id=? AND kind='initiative' AND
            (status IN ('pending','sending') OR (status='sent' AND available_at>?))""",
            (uid, timestamp(now - interval)),
        ).fetchone():
            continue
        for tag, body in candidates(store, user, now):
            if db.execute(
                """INSERT OR IGNORE INTO deliveries(key,user_id,kind,body,available_at)
                VALUES(?,?,'initiative',?,?)""",
                (f"initiative:{uid}:{tag}", uid, body, timestamp(now)),
            ).rowcount:
                break
