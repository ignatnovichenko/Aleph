"""Gmail push notifications are wakeups. Only the owner's Gmail API supplies matching messages."""

import asyncio
import base64
from email.utils import parseaddr
import hmac
import json
import logging
import os
import re
import time

from aiohttp import web
from . import connections

BASE = "/gmail/v1/users/me"
log = logging.getLogger(__name__)


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS gmail_streams (
            uid INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, email TEXT NOT NULL,
            cursor INTEGER NOT NULL, target INTEGER NOT NULL, expires REAL NOT NULL, retry REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS gmail_rules (
            task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            sender TEXT NOT NULL, subject TEXT NOT NULL, since INTEGER NOT NULL, matched TEXT);
    """)


async def subscribe(store, session, uid):
    topic = os.getenv("ALEPH_GMAIL_TOPIC", "")
    if (
        not re.fullmatch(r"projects/[\w.-]+/topics/[\w.~%+-]+", topic)
        or len(connections.setting("ALEPH_GMAIL_PUSH_SECRET")) < 32
    ):
        raise ValueError("На сервере ещё не настроены push-уведомления Gmail.")
    epoch = store.db.execute(
        "SELECT account_epoch FROM connections WHERE user_id=? AND provider='gmail' AND state='connected'",
        (uid,),
    ).fetchone()
    if not epoch:
        raise ValueError("Подключи Gmail в настройках.")
    profile = await connections.call(store, session, uid, "gmail", "GET", BASE + "/profile")
    data = await connections.call(
        store,
        session,
        uid,
        "gmail",
        "POST",
        BASE + "/watch",
        json={"topicName": topic, "labelIds": ["INBOX"], "labelFilterBehavior": "INCLUDE"},
    )
    if not store.db.execute(
        "SELECT 1 FROM connections WHERE user_id=? AND provider='gmail' AND state='connected' AND account_epoch=?",
        (uid, epoch[0]),
    ).fetchone():
        raise ValueError("Аккаунт Gmail изменился.")
    cursor = int(profile["historyId"])
    store.db.execute(
        """INSERT INTO gmail_streams(uid,email,cursor,target,expires) VALUES(?,?,?,?,?)
        ON CONFLICT(uid) DO UPDATE SET expires=excluded.expires,target=max(target,excluded.target)
        WHERE email=excluded.email""",
        (
            uid,
            profile["emailAddress"].casefold(),
            cursor,
            int(data["historyId"]),
            int(data["expiration"]) / 1000,
        ),
    )


async def watch(store, session, uid, project, args, key):
    sender, subject = (str(args.get(k, "")).strip().casefold() for k in ("sender", "subject"))
    if sender and not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", sender):
        raise ValueError("Укажи точный адрес отправителя.")
    if not (sender or subject) or len(subject) > 200 or len(sender) > 254:
        raise ValueError("Нужен отправитель или фрагмент темы письма.")
    previous = store.db.execute(
        "SELECT id FROM tasks WHERE user_id=? AND request_key=?", (uid, key)
    ).fetchone()
    if previous:
        return store.get_task(uid, previous[0])
    if connections.statuses(store, uid)["gmail"] != "connected":
        raise ValueError("Сначала подключи Gmail в настройках.")
    since = int(time.time() * 1000)
    await subscribe(store, session, uid)
    db = store.db
    db.execute("SAVEPOINT add_mail_rule")
    try:
        task = store.create_task(
            uid,
            args["title"],
            notes="Gmail · " + " · ".join(filter(None, [sender, subject])),
            request_key=key,
            project_id=project,
            source="gmail",
        )
        db.execute(
            "INSERT OR IGNORE INTO gmail_rules VALUES(?,?,?,?,NULL)", (task["id"], sender, subject, since)
        )
    except BaseException:
        db.execute("ROLLBACK TO add_mail_rule")
        raise
    finally:
        db.execute("RELEASE add_mail_rule")
    return task


def install(app, store):
    async def receive(request):
        secret = connections.setting("ALEPH_GMAIL_PUSH_SECRET")
        if len(secret) < 32 or not hmac.compare_digest(
            request.query.get("token", "").encode(), secret.encode()
        ):
            raise web.HTTPForbidden()
        try:
            packet = await request.json()
            event = json.loads(base64.b64decode(packet["message"]["data"], validate=True))
            cursor = int(event["historyId"])
            if not 0 < cursor < 2**63 or not isinstance(event["emailAddress"], str):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest() from None
        store.db.execute(
            "UPDATE gmail_streams SET target=max(target,?) WHERE email=?",
            (cursor, event["emailAddress"].casefold()),
        )
        return web.Response(status=204)

    app.router.add_post("/events/gmail", receive)


def match(store, uid, msg):
    from .store import timestamp

    if "INBOX" not in msg.get("labelIds", []):
        return
    headers = {h["name"].lower(): h["value"] for h in msg["payload"]["headers"]}
    sender = parseaddr(headers.get("from", ""))[1].casefold()
    subject = headers.get("subject", "")
    db = store.db
    rules = db.execute(
        """SELECT r.*,t.title,u.language_code FROM gmail_rules r JOIN tasks t ON t.id=r.task_id
        JOIN users u ON u.id=t.user_id WHERE t.user_id=? AND t.status='open'
        AND matched IS NULL AND since<=?""",
        (uid, int(msg["internalDate"])),
    ).fetchall()
    for rule in rules:
        if (rule["sender"] and sender != rule["sender"]) or rule["subject"] not in subject.casefold():
            continue
        tid = rule["task_id"]
        prefix = "Email arrived" if rule["language_code"].startswith("en") else "Пришло письмо"
        body = f"{prefix} · {sender}\n{subject[:350]}\n\n{rule['title']}"
        db.execute("SAVEPOINT mail_match")
        try:
            db.execute("UPDATE gmail_rules SET matched=? WHERE task_id=?", (msg["id"], tid))
            db.execute("UPDATE tasks SET status='done',updated_at=? WHERE id=?", (timestamp(), tid))
            db.execute(
                "INSERT OR IGNORE INTO deliveries(key,user_id,kind,task_id,body,available_at) VALUES(?,?,'mail',?,?,?)",
                (f"mail:{tid}:{rule['since']}", uid, tid, body, timestamp()),
            )
        except BaseException:
            db.execute("ROLLBACK TO mail_match")
            raise
        finally:
            db.execute("RELEASE mail_match")


async def sync(store, session, row):
    uid = row["uid"]

    async def get(path, params=None):
        return await connections.call(store, session, uid, "gmail", "GET", BASE + path, params=params)

    params = {"startHistoryId": str(row["cursor"]), "historyTypes": "messageAdded"}
    verified = row["cursor"]
    listing = "/history"
    while True:
        try:
            page = await get(listing, params)
        except connections.ConnectionError as error:
            if listing != "/history" or error.status != 404:
                raise
            since = store.db.execute(
                """SELECT min(r.since) FROM gmail_rules r JOIN tasks t ON t.id=r.task_id
                WHERE t.user_id=? AND t.status='open' AND r.matched IS NULL""",
                (uid,),
            ).fetchone()[0]
            if since is None:
                break
            verified = int((await get("/profile"))["historyId"])
            listing, params = "/messages", {"q": f"in:inbox after:{since // 1000 - 1}"}
            continue
        if listing == "/history":
            verified = int(page["historyId"])
        ids = (
            [m["id"] for m in page.get("messages", [])]
            if listing == "/messages"
            else [
                a["message"]["id"]
                for h in page.get("history", [])
                for a in h.get("messagesAdded", [])
                if "INBOX" in a["message"].get("labelIds", ["INBOX"])
            ]
        )
        for mid in dict.fromkeys(ids):
            try:
                msg = await get(
                    "/messages/" + mid,
                    [("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject")],
                )
            except connections.ConnectionError as error:
                if error.status == 404:
                    continue
                raise
            if not store.db.execute(
                "SELECT 1 FROM connections WHERE user_id=? AND provider='gmail' AND account_epoch=? AND state='connected'",
                (uid, row["account_epoch"]),
            ).fetchone():
                return
            match(store, uid, msg)
        if not page.get("nextPageToken"):
            break
        params["pageToken"] = page["nextPageToken"]
    store.db.execute(
        "UPDATE gmail_streams SET cursor=max(cursor,?),target=CASE WHEN target=? THEN ? ELSE target END WHERE uid=? AND email=?",
        (verified, row["target"], verified, uid, row["email"]),
    )


async def run(store, session):
    while True:
        rows = store.db.execute(
            """SELECT s.*,c.account_epoch FROM gmail_streams s JOIN connections c ON c.user_id=s.uid
            AND c.provider='gmail' AND c.state='connected' WHERE retry<=? AND (target>cursor OR expires<?)
            AND EXISTS(SELECT 1 FROM tasks t JOIN gmail_rules r ON r.task_id=t.id WHERE t.user_id=s.uid
            AND t.status='open' AND r.matched IS NULL)""",
            (time.time(), time.time() + 86400),
        ).fetchall()
        for row in rows:
            try:
                if row["expires"] < time.time() + 86400:
                    await subscribe(store, session, row["uid"])
                if row["target"] > row["cursor"]:
                    await sync(store, session, row)
            except Exception as exc:
                log.warning("Mail stream postponed (%s)", type(exc).__name__)
                store.db.execute(
                    "UPDATE gmail_streams SET retry=? WHERE uid=?", (time.time() + 60, row["uid"])
                )
        await asyncio.sleep(2)


def reset(store, uid):
    store.db.execute("DELETE FROM deliveries WHERE user_id=? AND kind='mail' AND status<>'sent'", (uid,))
    store.db.execute(
        """UPDATE tasks SET status='done' WHERE user_id=? AND id IN (SELECT task_id FROM gmail_rules)""",
        (uid,),
    )
    store.db.execute(
        "DELETE FROM gmail_rules WHERE task_id IN (SELECT id FROM tasks WHERE user_id=?)", (uid,)
    )
    store.db.execute("DELETE FROM gmail_streams WHERE uid=?", (uid,))
