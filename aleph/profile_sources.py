"""Opt-in, bounded personal context from each user's connected accounts."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import time
from . import integrations
from .initiative import keywords


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS profile_sources(uid INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        provider TEXT NOT NULL, focus TEXT NOT NULL, items TEXT NOT NULL, checked REAL NOT NULL, PRIMARY KEY(uid,provider))""")


def current(store, uid):
    rows = store.db.execute(
        """SELECT p.provider,p.items FROM profile_sources p JOIN users u ON u.id=p.uid
        JOIN connections c ON c.user_id=u.id AND c.provider=p.provider AND c.state='connected'
        WHERE u.id=? AND u.focus=p.focus AND u.context_sources AND p.checked>?""",
        (uid, time.time() - 86400),
    )
    return {row["provider"]: json.loads(row["items"]) for row in rows}


async def collect(store, session, uid, provider, focus):
    terms = keywords(focus)
    if not terms:
        return []
    # Keep search terms inside the service already authorized by the user.
    tokens = sorted(terms, key=lambda word: (-len(word), word))[:3]
    items = {}
    for term in tokens if provider != "calendar" else [""]:
        query = f'in:inbox newer_than:3d "{term}"' if provider == "gmail" else term
        args = {"provider": provider, "query": query}
        if provider == "calendar":
            now = datetime.now(timezone.utc)
            args.update(start=now.isoformat(), end=(now + timedelta(days=2)).isoformat())
        data = await integrations.read(store, session, uid, args)
        key = {"notion": "pages", "drive": "files", "calendar": "events", "gmail": "messages"}[provider]
        for row in data.get(key, [])[:8]:
            title = row.get("title") or row.get("name") or row.get("subject") or ""
            if provider == "calendar" and not keywords(title) & terms:
                continue
            ident = row.get("id") or row.get("url") or title
            if not title or ident in items:
                continue
            excerpt = row.get("snippet", "")
            if provider in ("notion", "drive") and not items:
                try:
                    detail = await integrations.read(
                        store, session, uid, {"provider": provider, "item": ident}
                    )
                    excerpt = detail.get("text", "")[:750]
                except ValueError:
                    pass
            items[ident] = {
                "title": title[:240],
                "url": row.get("url") or row.get("webViewLink") or "",
                "excerpt": excerpt[:750],
                "start": row.get("start", {}),
            }
            if len(items) >= 4:
                return list(items.values())
    return list(items.values())


async def run(store, session):
    while True:
        row = store.db.execute(
            """SELECT u.id,u.focus,c.provider,c.account_epoch FROM users u JOIN connections c ON c.user_id=u.id
            LEFT JOIN profile_sources p ON p.uid=u.id AND p.provider=c.provider WHERE u.context_sources AND u.focus!=''
            AND c.state='connected' AND (p.uid IS NULL OR p.focus!=u.focus OR p.checked<?)
            ORDER BY coalesce(p.checked,0) LIMIT 1""",
            (time.time() - 1800,),
        ).fetchone()
        if not row:
            await asyncio.sleep(12)
            continue
        uid, focus, provider, epoch = row
        store.db.execute(
            """INSERT INTO profile_sources VALUES(?,?,?,'[]',?) ON CONFLICT(uid,provider) DO UPDATE SET
            items=CASE WHEN focus=excluded.focus THEN items ELSE '[]' END,focus=excluded.focus,checked=excluded.checked""",
            (uid, provider, focus, time.time()),
        )
        try:
            items = await collect(store, session, uid, provider, focus)
        except Exception as error:
            import logging

            logging.getLogger(__name__).info("Personalization read postponed (%s)", type(error).__name__)
            items = []
        store.db.execute(
            "UPDATE profile_sources SET items=? WHERE uid=? AND provider=? AND focus=? AND EXISTS(SELECT 1 FROM connections WHERE user_id=? AND provider=? AND account_epoch=?)",
            (json.dumps(items, ensure_ascii=False), uid, provider, focus, uid, provider, epoch),
        )
        await asyncio.sleep(1)
