#!/usr/bin/env python3
"""Build script for ScanHound v2.0.

Steps:
  1. Build Python backend sidecar with PyInstaller
  2. Copy sidecar into Tauri's sidecar directory
  3. Build Tauri app (which builds frontend + bundles everything)

Usage:
  python scripts/build.py          # Full build
  python scripts/build.py --backend-only  # Just the Python sidecar
  python scripts/build.py --skip-backend  # Just the Tauri app (assumes sidecar already built)
"""

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
SRC_TAURI = FRONTEND / "src-tauri"
SIDECAR_DIR = SRC_TAURI / "binaries"

# Tauri expects sidecar binaries named: <name>-<target-triple>[.exe]
TARGET_TRIPLE = {
    ("Windows", "AMD64"): "x86_64-pc-windows-msvc",
    ("Windows", "x86"): "i686-pc-windows-msvc",
    ("Darwin", "x86_64"): "x86_64-apple-darwin",
    ("Darwin", "arm64"): "aarch64-apple-darwin",
    ("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("Linux", "aarch64"): "aarch64-unknown-linux-gnu",
}


def get_triple():
    key = (platform.system(), platform.machine())
    triple = TARGET_TRIPLE.get(key)
    if not triple:
        print(f"Warning: Unknown platform {key}, using rustc to detect target triple")
        result = subprocess.run(["rustc", "-vV"], capture_output=True, text=True)
        for line in result.stdout.splitlines():
            if line.startswith("host:"):
                return line.split(":")[1].strip()
        sys.exit(f"Cannot determine target triple for {key}")
    return triple


def build_backend():
    """Build the Python backend sidecar with PyInstaller."""
    print("=" * 60)
    print("Building Python backend sidecar...")
    print("=" * 60)

    spec = ROOT / "scanhound-backend.spec"
    if not spec.exists():
        sys.exit(f"PyInstaller spec not found: {spec}")

    subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(spec), "--noconfirm"],
        cwd=str(ROOT),
        check=True,
    )

    print("Backend sidecar built successfully.")


def copy_sidecar():
    """Copy built sidecar into Tauri's expected location."""
    triple = get_triple()
    ext = ".exe" if platform.system() == "Windows" else ""
    sidecar_name = f"scanhound-backend-{triple}{ext}"

    SIDECAR_DIR.mkdir(parents=True, exist_ok=True)

    # PyInstaller output
    dist_dir = ROOT / "dist" / "scanhound-backend"
    if not dist_dir.exists():
        sys.exit(f"Sidecar dist not found: {dist_dir}. Run build_backend first.")

    dest = SIDECAR_DIR / sidecar_name
    src_exe = dist_dir / f"scanhound-backend{ext}"

    if src_exe.exists():
        shutil.copy2(src_exe, dest)
        print(f"Copied sidecar to {dest}")
    else:
        sys.exit(f"Sidecar executable not found: {src_exe}")

    # Also copy the entire dist folder contents for runtime deps
    runtime_dest = SIDECAR_DIR / "scanhound-backend-runtime"
    if runtime_dest.exists():
        shutil.rmtree(runtime_dest)
    shutil.copytree(dist_dir, runtime_dest)
    print(f"Copied runtime dependencies to {runtime_dest}")


def build_tauri():
    """Build the Tauri desktop app."""
    print("=" * 60)
    print("Building Tauri app...")
    print("=" * 60)

    subprocess.run(
        ["npx", "tauri", "build"],
        cwd=str(FRONTEND),
        check=True,
        shell=True,
    )
    print("Tauri app built successfully.")


def main():
    parser = argparse.ArgumentParser(description="Build ScanHound v2.0")
    parser.add_argument("--backend-only", action="store_true", help="Only build the Python sidecar")
    parser.add_argument("--skip-backend", action="store_true", help="Skip Python sidecar, only build Tauri")
    args = parser.parse_args()

    if args.backend_only:
        build_backend()
        copy_sidecar()
        return

    if not args.skip_backend:
        build_backend()
        copy_sidecar()

    build_tauri()

    print()
    print("=" * 60)
    print("Build complete!")
    print(f"Output: {SRC_TAURI / 'target' / 'release' / 'bundle'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
