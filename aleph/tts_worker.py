"""Render one voice-note segment in the isolated BlueTTS CPU runtime."""

import os
import sys
import wave
from pathlib import Path

import numpy as np
from blue_onnx import BlueTTS


def main():
    root, destination, language = sys.argv[1:]
    bundle = Path(root)
    os.environ["POLYG2P_DATA"] = str(bundle / "frontend/polyg2p_data")
    model = BlueTTS(
        str(bundle / "onnx"),
        str(bundle / "voices/david.json"),
        providers=["CPUExecutionProvider"],
        precision="fp32",
    )
    audio, rate = model.synthesize(sys.stdin.buffer.read().decode("utf-8"), lang=language)
    if not audio.size or not np.isfinite(audio).all() or np.abs(audio).max() < 0.001:
        raise ValueError("Empty voice note")
    with wave.open(destination, "wb") as out:
        out.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        out.writeframes((audio * 32767).astype("<i2").tobytes())


if __name__ == "__main__":
    main()
