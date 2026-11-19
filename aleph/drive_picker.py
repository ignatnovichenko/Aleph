"""Short-lived browser handoff for Google Picker with the Drive-file scope."""
import hashlib
import json
import os
import re
import secrets
import time

from . import connections


def settings():
    app_id = os.getenv("ALEPH_GOOGLE_PROJECT_NUMBER", "")
    api_key = os.getenv("ALEPH_GOOGLE_PICKER_KEY", "")
    if not re.fullmatch(r"[0-9]{5,20}", app_id) or not re.fullmatch(r"[A-Za-z0-9_-]{15,120}", api_key):
        raise connections.ConnectionError("Google Picker ещё не настроен на сервере.")
    return app_id, api_key


def start(store, uid):
    config = connections.configured("drive")
    settings()
    if connections.statuses(store, uid)["drive"] != "connected":
        raise connections.ConnectionError("Сначала подключи Google Drive.")
    ticket = secrets.token_urlsafe(32)
    key = hashlib.sha256(ticket.encode()).hexdigest()
    store.db.execute("DELETE FROM connection_requests WHERE expires<=?", (time.time(),))
    store.db.execute("""INSERT INTO connection_requests(hash,user_id,provider,expires,verifier)
        VALUES(?,?,'drive-picker',?,'')
    """, (key, uid, time.time() + 180))
    return f"{config.origin}/oauth/drive/picker?ticket={ticket}"


async def page(store, session, ticket):
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket):
        raise connections.ConnectionError("Ссылка истекла.")
    app_id, key = settings()
    digest = hashlib.sha256(ticket.encode()).hexdigest()
    row = store.db.execute("""DELETE FROM connection_requests WHERE provider='drive-picker' AND hash=? AND expires>?
        RETURNING user_id
    """, (digest, time.time())).fetchone()
    if not row:
        raise connections.ConnectionError("Ссылка истекла. Открой выбор файлов из Алефа заново.")
    token = await connections.access(store, session, row[0], "drive")
    nonce = secrets.token_urlsafe(18)
    options = json.dumps({"appId": app_id, "apiKey": key, "token": token}).replace("<", "\\u003c")
    html = f'''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Aleph · Google Drive</title>
<main><h1>Файлы Google Drive</h1><p id="status" role="status">Загружаю Google Drive…</p>
<button id="pick" type="button" disabled>Выбрать файлы</button></main>
<script nonce="{nonce}">
const options = {options};
const select = document.getElementById("pick");
const status = document.getElementById("status");
function loadPicker() {{
  gapi.load("picker", () => {{
    status.textContent = "Выбери файлы, к которым хочешь дать доступ Алефу.";
    select.disabled = false;
    select.onclick = () => {{
      new google.picker.PickerBuilder()
        .setAppId(options.appId).setOAuthToken(options.token).setDeveloperKey(options.apiKey)
        .setOrigin(window.location.origin).addView(new google.picker.DocsView())
        .enableFeature(google.picker.Feature.MULTISELECT_ENABLED)
        .setCallback(result => {{
          if (result.action === google.picker.Action.PICKED) {{
            status.textContent = "Доступ выдан. Вернись в Telegram и попроси Алефа открыть нужный файл.";
          }}
        }}).build().setVisible(true);
    }};
  }});
}}
</script><script nonce="{nonce}" src="https://apis.google.com/js/api.js" ></script><script nonce="{nonce}">loadPicker()</script></html>'''
    policy = (f"default-src 'none'; script-src 'nonce-{nonce}' https://apis.google.com https://*.gstatic.com; "
              "frame-src https://docs.google.com https://drive.google.com https://accounts.google.com; "
              "connect-src https://*.google.com https://www.googleapis.com; style-src 'unsafe-inline'; "
              "img-src https://*.google.com https://*.gstatic.com data:; frame-ancestors 'none'")
    return html, policy
