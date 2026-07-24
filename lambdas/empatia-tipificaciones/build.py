#!/usr/bin/env python
"""Build the Lambda deployment package with Linux-compatible dependencies.

`cryptography` ships native extensions, so we must fetch the manylinux
(Amazon Linux) wheels rather than whatever matches the build host. This runs on
any OS with Python + pip and produces ``build/`` (the dependencies plus
``main.py``) and ``function.zip`` -- with ``main.py`` at the ZIP ROOT, which is
what the Lambda runtime needs to import module ``main``.

Usage:
    python build.py
    aws lambda update-function-code --function-name <fn> --zip-file fileb://function.zip
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
ZIP_BASE = os.path.join(HERE, "function")  # -> function.zip
PYTHON_VERSION = "3.12"
ABI = "cp312"
PLATFORM = "manylinux2014_x86_64"  # Lambda x86_64 runtime


def main():
    shutil.rmtree(BUILD, ignore_errors=True)
    os.makedirs(BUILD)
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
            BUILD,
            "-r",
            os.path.join(HERE, "requirements.txt"),
        ]
    )
    shutil.copy(os.path.join(HERE, "main.py"), os.path.join(BUILD, "main.py"))

    # Zip the CONTENTS of build/ so main.py sits at the archive root (root_dir).
    if os.path.exists(ZIP_BASE + ".zip"):
        os.remove(ZIP_BASE + ".zip")
    shutil.make_archive(ZIP_BASE, "zip", root_dir=BUILD)
    print(f"Built {ZIP_BASE}.zip (main.py at root) and contents in {BUILD}")


if __name__ == "__main__":
    main()
