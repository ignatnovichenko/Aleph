"""Numeric conditions on an explicitly selected project price list."""

import asyncio
import csv
from decimal import Decimal, InvalidOperation
import logging
import time
from . import project_sources

log = logging.getLogger(__name__)


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS price_rules(task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            source_id INTEGER NOT NULL REFERENCES project_sources(id) ON DELETE CASCADE, label TEXT NOT NULL,
            col INTEGER NOT NULL, percent INTEGER NOT NULL, previous TEXT NOT NULL, sequence INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS source_checks(source_id INTEGER PRIMARY KEY REFERENCES project_sources(id) ON DELETE CASCADE,
            checked REAL NOT NULL DEFAULT 0);
    """)


def amount(content, label, column):
    for separator in ("\t", ";", "|", ","):
        prices = []
        for cells in csv.reader(content.splitlines(), delimiter=separator):
            if column > len(cells) or not any(v.strip().casefold() == label.casefold() for v in cells):
                continue
            try:
                number = Decimal(
                    cells[column - 1].strip().replace(" ", "").replace("\xa0", "").replace(",", ".")
                )
            except InvalidOperation:
                continue
            if number.is_finite() and 0 < number < 10**15:
                prices.append(number)
        if len(prices) == 1:
            return prices[0]
        if prices:
            break
    raise ValueError("Нужна уникальная строка товара и столбец с числовой ценой.")


def create(store, uid, project, args, key):
    source = project_sources._row(store, uid, project, args["source_id"], content=True)
    column, threshold = args["column"], args["percent"]
    if not (1 <= column <= 50 and 0 < threshold <= 1000 and 0 < len(args["label"].strip()) <= 200):
        raise ValueError("Проверь товар, столбец и процент.")
    val = amount(source["content"], args["label"], column)
    task = store.create_task(
        uid,
        args["title"],
        notes=f"{source['title']} · {args['label']} · >{threshold}%",
        project_id=project,
        request_key=key,
        source="price",
    )
    store.db.execute(
        "INSERT OR IGNORE INTO price_rules(task_id,source_id,label,col,percent,previous) VALUES(?,?,?,?,?,?)",
        (task["id"], source["id"], args["label"], column, threshold, str(val)),
    )
    return {
        "task": task,
        "baseline": str(val),
        "source": source["title"],
        "updates": "linked" if source["url"] else "upload_same_filename",
    }


def observe(store, source_id):
    from .store import timestamp

    rows = store.db.execute(
        """SELECT r.*,t.user_id,s.content,s.url,s.title,u.language_code FROM price_rules r
        JOIN tasks t ON r.task_id=t.id JOIN project_sources s ON s.id=r.source_id JOIN users u ON u.id=t.user_id
        WHERE t.status='open' AND r.source_id=?""",
        (source_id,),
    ).fetchall()
    for row in rows:
        try:
            current = amount(row["content"], row["label"], row["col"])
        except ValueError:
            continue
        previous = Decimal(row["previous"])
        if previous == current:
            continue
        db = store.db
        db.execute("SAVEPOINT price_change")
        try:
            if (current - previous) * 100 > previous * row["percent"]:
                label = "Price increased" if row["language_code"].startswith("en") else "Цена выросла"
                text = f"{label} · {row['label']}\n{previous} → {current}\n{row['title']} · {timestamp()[:10]}\n{row['url']}".strip()
                db.execute(
                    "INSERT OR IGNORE INTO deliveries(key,user_id,kind,task_id,body,available_at) VALUES(?,?,'price',?,?,?)",
                    (
                        f"price:{row['task_id']}:{row['sequence']}",
                        row["user_id"],
                        row["task_id"],
                        text,
                        timestamp(),
                    ),
                )
            db.execute(
                "UPDATE price_rules SET previous=?,sequence=sequence+1 WHERE task_id=?",
                (str(current), row["task_id"]),
            )
        except BaseException:
            db.execute("ROLLBACK TO price_change")
            raise
        finally:
            db.execute("RELEASE price_change")


async def refresh_sources(store):
    while True:
        # Poll sources without push only when a user enabled contextual updates or a price rule.
        row = store.db.execute(
            """SELECT s.id,s.project_id,s.user_id FROM project_sources s JOIN users u ON u.id=s.user_id
            LEFT JOIN source_checks c ON c.source_id=s.id WHERE s.url!='' AND coalesce(c.checked,0)<?
            AND ((u.proactive AND u.context_sources AND u.focus!='' AND u.initiative!='quiet' AND s.kind IN ('drive','notion'))
            OR EXISTS(SELECT 1 FROM price_rules r JOIN tasks t ON t.id=r.task_id WHERE r.source_id=s.id AND t.status='open'))
            ORDER BY coalesce(c.checked,0) LIMIT 1""",
            (time.time() - 900,),
        ).fetchone()
        if row:
            store.db.execute("INSERT OR REPLACE INTO source_checks VALUES(?,?)", (row["id"], time.time()))
            try:
                await project_sources.refresh(store, row["user_id"], row["project_id"], row["id"])
            except Exception as error:
                log.info("Project sync postponed (%s)", type(error).__name__)
        await asyncio.sleep(1 if row else 15)
