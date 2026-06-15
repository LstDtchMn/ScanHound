#!/usr/bin/env python3
"""Development server launcher for ScanHound v2.0.

Starts both the Python backend API (port 9721) and the Svelte dev server (port 5173).
Press Ctrl+C to stop both.

Usage:
  python scripts/dev.py
  python scripts/dev.py --backend-only    # Just the API server
  python scripts/dev.py --frontend-only   # Just the Svelte dev server
"""

import argparse
import os
import platform
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"


def kill_port(port: int) -> None:
    """Kill any process listening on the given port (best-effort)."""
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True
            )
            for line in result.stdout.splitlines():
                if f":{port} " in line and "LISTENING" in line:
                    pid = line.split()[-1]
                    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
        else:
            subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)
    except Exception:
        pass


def run_backend():
    """Start the FastAPI backend server."""
    return subprocess.Popen(
        [sys.executable, "-m", "backend.api", "--port", "9721", "--host", "127.0.0.1", "--no-auth"],
        cwd=str(ROOT),
    )


def run_frontend():
    """Start the Svelte dev server."""
    env = os.environ.copy()
    return subprocess.Popen(
        ["npm", "run", "dev", "--", "--port", "5174"],
        cwd=str(FRONTEND),
        shell=True,
        env=env,
    )


def main():
    parser = argparse.ArgumentParser(description="ScanHound v2.0 dev server")
    parser.add_argument("--backend-only", action="store_true")
    parser.add_argument("--frontend-only", action="store_true")
    args = parser.parse_args()

    procs = []

    try:
        if not args.frontend_only:
            print("Freeing port 9721...")
            kill_port(9721)
            print("Starting backend on http://127.0.0.1:9721 ...")
            procs.append(run_backend())

        if not args.backend_only:
            print("Freeing port 5174...")
            kill_port(5174)
            print("Starting frontend on http://localhost:5174 ...")
            procs.append(run_frontend())

        print("\nPress Ctrl+C to stop.\n")

        # Wait for any process to exit
        for p in procs:
            p.wait()

    except KeyboardInterrupt:
        print("\nShutting down...")
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
