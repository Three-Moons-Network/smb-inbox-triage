#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build the Lambda deployment package for AWS (arm64 / python3.12).

Creates .build/package/ containing:
  - pydantic + pydantic-core  (manylinux2014_aarch64 binary wheel — arm64 compatible)
  - httpx + dependencies      (pure Python)
  - Contents of src/          (application source code)

Terraform's archive_file data source then zips .build/package/ → .build/lambda.zip
and tracks the hash for Lambda redeploys.

Usage (called automatically by Terraform null_resource, or manually):
    python scripts/build_lambda.py
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys

# Resolve project root relative to this script's location
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_DIR = os.path.join(ROOT, ".build", "package")
SRC_DIR = os.path.join(ROOT, "src")


def _force_remove(func, path, exc_info) -> None:  # noqa: ANN001
    """
    Error handler for shutil.rmtree on Windows.

    Python writes __pycache__ .pyc files with read-only bits set, causing
    PermissionError (WinError 5) when rmtree tries to delete them.  Clearing
    the write bit and retrying is the standard workaround.
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


def pip(*args: str) -> None:
    """Run a pip command using the current Python interpreter."""
    result = subprocess.run(
        [sys.executable, "-m", "pip"] + list(args),
        check=False,
    )
    if result.returncode != 0:
        print(f"pip command failed (exit {result.returncode}): pip {' '.join(args)}", file=sys.stderr)
        sys.exit(result.returncode)


def main() -> None:
    print(f"[build_lambda] Building Lambda package -> {PACKAGE_DIR}")

    # ── Clean and recreate build directory ────────────────────────────────────
    # onexc handler is required on Windows: Python sets __pycache__ dirs
    # read-only after writing .pyc files, which causes PermissionError (WinError 5)
    # on the plain rmtree call.  _force_remove clears the bit and retries.
    if os.path.exists(PACKAGE_DIR):
        shutil.rmtree(PACKAGE_DIR, onexc=_force_remove)
    os.makedirs(PACKAGE_DIR, exist_ok=True)

    # ── Install pydantic with the arm64 manylinux wheel ───────────────────────
    #
    # pydantic v2 includes pydantic-core, a Rust extension compiled for the
    # target architecture.  Lambda runs on arm64 (aarch64).  Installing from
    # the developer's Windows/macOS/x86-Linux machine would yield the WRONG
    # native binary, so we request the manylinux2014_aarch64 wheel explicitly.
    #
    # --only-binary=:all:  — refuse to build from source (prevents mismatches)
    # --platform           — fetch the aarch64 wheel regardless of host arch
    # --python-version 3.12 / --implementation cp — match Lambda runtime
    print("[build_lambda] Installing pydantic (arm64 wheel)...")
    pip(
        "install", "pydantic>=2.7,<3",
        "--platform", "manylinux2014_aarch64",
        "--target", PACKAGE_DIR,
        "--implementation", "cp",
        "--python-version", "3.12",
        "--only-binary=:all:",
        "--upgrade",
        "--quiet",
    )

    # ── Install httpx (pure Python — no platform restriction needed) ──────────
    #
    # httpx and all its transitive deps (httpcore, certifi, h11, anyio, etc.)
    # are pure Python.  We still target PACKAGE_DIR so they land next to pydantic.
    print("[build_lambda] Installing httpx...")
    pip(
        "install", "httpx>=0.27,<1",
        "--target", PACKAGE_DIR,
        "--upgrade",
        "--quiet",
    )

    # ── Install OpenTelemetry SDK + OTLP HTTP exporter ────────────────────────
    #
    # These packages send traces and logs from Lambda to the Datadog Lambda
    # Extension (listening locally on HTTP port 4318), which then forwards them
    # to Datadog's API over HTTPS/443.
    #
    # opentelemetry-sdk       — pure Python (none-any wheel)
    # opentelemetry-exporter-otlp-proto-http — pure Python, but pulls in protobuf
    #
    # protobuf >= 4.x ships a manylinux2014_aarch64 binary wheel.  Using
    # --platform here ensures pip fetches the arm64 binary wheel (not the x86
    # wheel from the developer's machine).  Pure-Python packages tagged
    # none-any are always selected regardless of --platform.
    print("[build_lambda] Installing opentelemetry-sdk + OTLP HTTP exporter (arm64 wheels)...")
    pip(
        "install",
        "opentelemetry-sdk>=1.24,<2",
        "opentelemetry-exporter-otlp-proto-http>=1.24,<2",
        "--platform", "manylinux2014_aarch64",
        "--target", PACKAGE_DIR,
        "--implementation", "cp",
        "--python-version", "3.12",
        "--only-binary=:all:",
        "--upgrade",
        "--quiet",
    )

    # boto3 / botocore are already present in the Lambda Python 3.12 runtime;
    # including them would bloat the package and risk version conflicts.

    # ── Copy application source code ──────────────────────────────────────────
    #
    # archive_file uses source_dir = ".build/package", which means the zip root
    # is the package directory itself.  Source files must be at the top level of
    # .build/package/ so Lambda can import them without any path prefix.
    print("[build_lambda] Copying src/ into package...")
    for item in os.listdir(SRC_DIR):
        src = os.path.join(SRC_DIR, item)
        dst = os.path.join(PACKAGE_DIR, item)
        if os.path.isdir(src):
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)

    print("[build_lambda] Done — package ready for Terraform to zip.")


if __name__ == "__main__":
    main()
