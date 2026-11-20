"""Launch the same isolated Hermes worker on the server or a paired computer."""
import asyncio
import json
import os
from pathlib import Path
import tempfile

TURN_TIMEOUT = 265
WORKER = Path(__file__).with_name("hermes_worker.py")


def hermes_python():
    path = Path(os.environ.get("ALEPH_HERMES_PYTHON") or
                Path.home() / ".local/share/aleph/hermes/.venv/bin/python")
    if not path.is_absolute() or not os.access(path, os.X_OK):
        raise RuntimeError("Hermes Python runtime missing. Run deploy/install-hermes.sh or set ALEPH_HERMES_PYTHON.")
    return path


async def run_worker(python, turn):
    # A new Hermes home keeps environment credentials, plugins and cached state outside the turn.
    with tempfile.TemporaryDirectory(prefix="aleph-hermes-") as home:
        env = {"PATH": os.defpath, "LANG": "C.UTF-8", "HERMES_HOME": home,
               "NO_PROXY": "127.0.0.1,localhost", "PYTHONUNBUFFERED": "1"}
        process = await asyncio.create_subprocess_exec(
            str(python), str(WORKER), cwd=home, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(json.dumps(turn, ensure_ascii=False).encode()), TURN_TIMEOUT)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        try:
            packet = json.loads(output)
        except (ValueError, UnicodeError):
            packet = {}
        answer = packet.get("answer") if isinstance(packet, dict) else None
        if process.returncode or not isinstance(answer, str) or not answer.strip():
            return {"error_type": "agent_turn_failed"}
        return {"answer": answer.strip()[:14000]}
