"""Offline interlock and manual Chrome profile launcher. Standard library only."""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

ROOT = Path.home() / ".config/aleph/browser"


def state(root, change=None):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / "control.sqlite", timeout=2, isolation_level=None)) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS interlock (id INTEGER PRIMARY KEY, epoch INTEGER, paused INTEGER)"
        )
        db.execute("INSERT OR IGNORE INTO interlock VALUES (1,0,0)")
        if change is not None:
            db.execute("UPDATE interlock SET epoch=epoch+1,paused=? WHERE id=1", (int(change),))
        return db.execute("SELECT epoch,paused FROM interlock WHERE id=1").fetchone()


def activity_lock(root):
    file = (root / "activity.lock").open("a")
    try:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        file.close()
        return None
    return file


def chrome_binary():
    paths = [
        os.getenv("ALEPH_CHROME_BINARY"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        str(Path(os.getenv("PROGRAMFILES", "/nonexistent")) / "Google/Chrome/Application/chrome.exe"),
        str(Path(os.getenv("LOCALAPPDATA", "/nonexistent")) / "Google/Chrome/Application/chrome.exe"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    return next((str(p) for p in paths if p and Path(p).is_file()), None)


def pairing_key(config):
    return hashlib.sha256((config["server"] + "\0" + config["token"]).encode()).hexdigest()[:24]


def profile(root, pairing):
    target = root / "profiles" / pairing
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("browser-stop", "browser-resume", "browser-login"))
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/aleph/computer.json")
    args = parser.parse_args()
    command = args.command
    if command == "browser-login":
        binary = chrome_binary()
        if not binary:
            sys.exit("Install Google Chrome first.")
        # Manual login owns the profile exclusively. The worker cannot launch it concurrently.
        if not state(ROOT)[1]:
            sys.exit("Run browser-stop first. Sign in, close the Chrome window, then run browser-resume.")
        try:
            pairing = pairing_key(json.loads(args.config.read_text()))
        except (OSError, ValueError, KeyError):
            sys.exit("Connect this computer in Aleph Settings first.")
        subprocess.run(
            [
                binary,
                "--user-data-dir=" + str(profile(ROOT, pairing)),
                "--no-first-run",
                "--new-window",
                "about:blank",
            ],
            check=True,
        )
        return
    state(ROOT, command == "browser-stop")
    if command == "browser-stop":
        # Return success only when the old browser instance has finished cleanup.
        deadline = time.monotonic() + 8
        while (lock := activity_lock(ROOT)) is None:
            if time.monotonic() > deadline:
                sys.exit("New steps blocked. The active Chrome process has not confirmed shutdown yet.")
            time.sleep(0.04)
        lock.close()
    print(
        "Chrome automation blocked locally."
        if command == "browser-stop"
        else "New browser tasks enabled locally."
    )


if __name__ == "__main__":
    main()
