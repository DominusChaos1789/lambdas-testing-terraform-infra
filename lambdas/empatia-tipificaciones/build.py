#!/usr/bin/env python
"""Build the Lambda deployment package with Linux-compatible dependencies.

`cryptography` ships native extensions, so we must fetch the manylinux
(Amazon Linux) wheels rather than whatever matches the build host. This runs on
any OS with Python + pip and produces ``build/`` containing the dependencies
plus ``main.py`` -- exactly what Terraform's archive_file zips.

Usage: python build.py
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
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
    print(f"Built deployment package contents in {BUILD}")


if __name__ == "__main__":
    main()
