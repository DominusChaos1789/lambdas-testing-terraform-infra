#!/usr/bin/env python
"""Build the `cryptography` Lambda LAYER (layer.zip).

A Python layer must place packages under a top-level ``python/`` directory in
the zip; Lambda adds that to ``sys.path``. We fetch the manylinux (Amazon Linux,
x86_64, cp312) wheels so the native extensions load on the Lambda runtime.

This keeps the FUNCTION code a single ``main.py`` (paste/upload in the console),
with ``cryptography`` supplied by the layer.

Usage:
    python build_layer.py
    aws lambda publish-layer-version --layer-name empatia-cryptography \
      --zip-file fileb://layer.zip \
      --compatible-runtimes python3.12 --compatible-architectures x86_64 \
      --region us-east-2
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LAYER = os.path.join(HERE, "layer")
PY_DIR = os.path.join(LAYER, "python")  # required layer structure
ZIP_BASE = os.path.join(HERE, "layer")  # -> layer.zip
PYTHON_VERSION = "3.12"
ABI = "cp312"
PLATFORM = "manylinux2014_x86_64"  # must match the function architecture (x86_64)


def main():
    shutil.rmtree(LAYER, ignore_errors=True)
    os.makedirs(PY_DIR)
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--platform",
            PLATFORM,
            "--python-version",
            PYTHON_VERSION,
            "--implementation",
            "cp",
            "--abi",
            ABI,
            "--only-binary=:all:",
            "--target",
            PY_DIR,
            "-r",
            os.path.join(HERE, "requirements.txt"),
        ]
    )
    if os.path.exists(ZIP_BASE + ".zip"):
        os.remove(ZIP_BASE + ".zip")
    shutil.make_archive(ZIP_BASE, "zip", root_dir=LAYER)
    print(f"Built {ZIP_BASE}.zip  (python/ at root)")


if __name__ == "__main__":
    main()
