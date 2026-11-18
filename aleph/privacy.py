"""Local account erasure initiated from the owner's Telegram conversation."""
import time


def privacy_reply(store, uid, text, update_id):
    command = text.strip().casefold()
    request = command in {"/delete", "удали мои данные", "удали все мои данные", "delete my data"}
    confirm = command in {"удалить мои данные навсегда", "permanently delete my data"}
    if not (request or confirm):
        return None
    ru = command[0] not in {"d", "p"} and (not command.startswith("/") or store.get_user(uid)["language_code"] in {"", "ru", "be", "uk"})
    db = store.db
    db.execute("CREATE TABLE IF NOT EXISTS erasure_requests (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, expires INTEGER NOT NULL)")
    if request:
        db.execute("INSERT OR REPLACE INTO erasure_requests VALUES(?,?)", (uid, int(time.time()) + 600))
        return ("Удалить проекты, план, расписания, память и историю Алефа? Если да, в течение 10 минут отправь «удалить мои данные навсегда»."
                if ru else "Erase your Aleph projects, plan, routines, memory and history? To confirm, send “permanently delete my data” within 10 minutes.")
    row = db.execute("SELECT expires FROM erasure_requests WHERE user_id=?", (uid,)).fetchone()
    if not row or row[0] < time.time():
        return ("Сначала напиши «удали мои данные»." if ru else "First send “delete my data”.")
    db.execute("BEGIN IMMEDIATE")
    try:
        # Revoke queued work before removing the records it refers to. Keep the
        # confirmation in the outbox just long enough for Telegram to receive it.
        db.execute("DELETE FROM bot_inbox WHERE uid=? AND id<>?", (uid, update_id))
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name in ("deliveries", "routine_runs", "routines", "history", "memories", "feeds", "tasks", "erasure_requests"):
            if name in tables:
                db.execute(f'DELETE FROM "{name}" WHERE user_id=?', (uid,))
        db.execute("DELETE FROM users WHERE id=?", (uid,))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return ("Данные удалены из Алефа. Историей этого чата ты управляешь в Telegram." if ru
            else "Your data has been erased from Aleph. You can manage this chat’s history in Telegram.")
