"""Install Aleph's Russian/English CPU voice runtime (Linux x86-64, Python 3.12+)."""

import hashlib
import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MODEL = "8410bbfe508a9b84a967324ecf3caf5f655326c9"
CODE = "5a9d9a04df559499b9c12715ebe1780b0f7d588c"
BASE = f"https://huggingface.co/notmax123/BlueV3-onnx/resolve/{MODEL}"
ROOT = Path(os.environ.get("ALEPH_BLUETTS_HOME", Path.home() / ".local/share/aleph/bluetts"))
WHEEL = "frontend/polyg2p-0.1.0-cp39-abi3-manylinux_2_34_x86_64.whl"


def fetch(path, sha=None):
    target = ROOT / path
    if sha and target.is_file():
        with target.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() == sha:
                return
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        subprocess.run(
            [
                "curl",
                "-fsSL",
                "--retry",
                "3",
                "--max-time",
                "300",
                "-o",
                str(temporary),
                f"{BASE}/{path}?download=true",
            ],
            check=True,
        )
        with temporary.open("rb") as stream:
            if sha and hashlib.file_digest(stream, "sha256").hexdigest() != sha:
                raise ValueError(f"Checksum mismatch for {path}")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    fetch("MANIFEST.json")
    manifest = json.loads((ROOT / "MANIFEST.json").read_text())
    assets = {item["path"]: item["sha256"] for item in manifest["files"]}
    files = [
        "onnx/" + name
        for name in (
            "tts.json",
            "vocab.json",
            "stats.npz",
            "uncond.npz",
            "text_encoder.onnx",
            "duration_predictor_style.onnx",
            "vector_estimator.onnx",
            "vocoder.onnx",
        )
    ]
    files.extend([WHEEL, "voices/david.json"])
    files.extend(name for name in assets if name.startswith("frontend/polyg2p_data/"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda path: fetch(path, assets[path]), files))
    for name in ("LICENSES.md", "LICENSE.OpenRAIL-M"):
        fetch(name)
    interpreter = ROOT / "venv/bin/python"
    if not interpreter.exists():
        subprocess.run(["uv", "venv", "--python", "3.12", str(ROOT / "venv")], check=True)
    install = ["uv", "pip", "install", "--python", str(interpreter)]
    subprocess.run([*install, "onnxruntime>=1.27,<1.30", "numpy", "num2words", str(ROOT / WHEEL)], check=True)
    subprocess.run([*install, "--no-deps", f"git+https://github.com/maxmelichov/BlueTTS@{CODE}"], check=True)


if __name__ == "__main__":
    main()
