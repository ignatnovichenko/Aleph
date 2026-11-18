"""Public links for Telegram's signed Mini App."""
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def mini_app_url():
    url = os.getenv("MINI_APP_URL", "").strip()
    if not url:
        default = Path.home() / ".config/telegram-agent/mini-app-url"
        path = Path(os.getenv("MINI_APP_URL_FILE") or default)
        if path.is_file():
            url = path.read_text().strip()
    if url:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError("MINI_APP_URL must be a public HTTPS URL")
    return url


def widget_url(widget_id):
    try:
        url = mini_app_url()
    except (ValueError, OSError):
        return ""
    if not url:
        return ""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/widget.html",
                       f"id={widget_id}", ""))
