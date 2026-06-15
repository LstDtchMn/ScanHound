"""Entry point for packaged backend API (PyInstaller sidecar)."""
import argparse
import os
import secrets
import uvicorn


def main():
    parser = argparse.ArgumentParser(description="ScanHound API Server")
    parser.add_argument("--port", type=int, default=9721)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-auth", action="store_true", help="Disable auth nonce (dev mode)")
    args = parser.parse_args()

    # Generate auth nonce for Tauri sidecar mode.
    # In dev mode (--no-auth), skip so the browser can access the API directly.
    if args.no_auth:
        os.environ["SCANHOUND_AUTH_NONCE"] = ""
    elif not os.environ.get("SCANHOUND_AUTH_NONCE"):
        nonce = secrets.token_urlsafe(32)
        os.environ["SCANHOUND_AUTH_NONCE"] = nonce
        print(f"SCANHOUND_AUTH_NONCE={nonce}", flush=True)

    from backend.api.main import create_app
    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
