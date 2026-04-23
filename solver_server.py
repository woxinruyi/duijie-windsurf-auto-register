#!/usr/bin/env python3
"""
Standalone Turnstile solver service.

POST /solve
{
  "site_url": "https://windsurf.com/billing/individual?plan=9",
  "sitekey": "",
  "browser_path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "timeout": 90,
  "headless": true
}

GET /health -> {"ok": true}
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from windsurf_auth_replay import (
    WorkflowError,
    env_bool,
    env_int,
    env_str,
    load_dotenv,
    solve_turnstile_token_with_options,
)


def parse_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


class SolverHandler(BaseHTTPRequestHandler):
    server_version = "TurnstileSolver/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[solver] {self.address_string()} - {fmt % args}")

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send_json(404, {"error": "not found"})
            return
        self._send_json(200, {"ok": True})

    def do_POST(self) -> None:
        if self.path != "/solve":
            self._send_json(404, {"error": "not found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "invalid content length"})
            return

        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            data = json.loads(raw_body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid json body"})
            return
        if not isinstance(data, dict):
            self._send_json(400, {"error": "json body must be an object"})
            return

        site_url = str(
            data.get("site_url")
            or env_str("WINDSURF_TURNSTILE_SITE_URL", "https://windsurf.com/billing/individual?plan=9")
        )
        sitekey = str(data.get("sitekey") or env_str("WINDSURF_TURNSTILE_SITEKEY"))
        browser_path = str(data.get("browser_path") or env_str("TURNSTILE_BROWSER_PATH"))
        timeout = data.get("timeout")
        try:
            timeout_value = int(timeout) if timeout is not None else env_int("TURNSTILE_TIMEOUT", 90)
        except (TypeError, ValueError):
            self._send_json(400, {"error": "timeout must be an integer"})
            return
        headless = parse_bool(data.get("headless"), env_bool("TURNSTILE_HEADLESS", True))

        try:
            token = solve_turnstile_token_with_options(
                site_url=site_url,
                sitekey=sitekey,
                browser_path=browser_path,
                timeout=max(5, timeout_value),
                headless=headless,
            )
        except WorkflowError as exc:
            self._send_json(500, {"error": str(exc)})
            return
        except Exception as exc:
            self._send_json(500, {"error": f"unexpected error: {exc}"})
            return

        self._send_json(200, {"token": token})


if __name__ == "__main__":
    load_dotenv()
    host = env_str("TURNSTILE_SOLVER_HOST", "127.0.0.1")
    port = env_int("TURNSTILE_SOLVER_PORT", 3000)
    with ThreadingHTTPServer((host, port), SolverHandler) as httpd:
        print(f"[*] Turnstile solver listening on http://{host}:{port}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[!] user interrupted")
