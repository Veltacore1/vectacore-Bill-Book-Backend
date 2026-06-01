#!/usr/bin/env python
"""Production smoke checks for a deployed VastraBook stack.

The checker intentionally uses only the Python standard library so it can run
from a release host, CI job, or Docker exec session without extra packages.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


DEFAULT_BACKEND_URL = "http://127.0.0.1:8001"
DEFAULT_FRONTEND_URL = "http://127.0.0.1:8080"
DEFAULT_REFRESH_COOKIE_NAME = "vastrabook_refresh"


@dataclass
class SmokeResult:
    name: str
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def build_url(base_url: str, path: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def headers_to_dict(headers: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in headers.items():
        existing = values.get(key)
        values[key] = f"{existing}\n{value}" if existing else value
    return values


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[redacted]" if key.lower() in {"access", "refresh", "token", "csrftoken"} else redact_sensitive(nested)
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def request_json(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None, timeout: float = 10) -> tuple[int, dict[str, str], dict[str, Any]]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        data = json.loads(raw) if raw else {}
        return response.status, headers_to_dict(response.headers), data


def request_text(url: str, *, timeout: float = 10) -> tuple[int, dict[str, str], str]:
    request = Request(url, headers={"Accept": "text/html,application/xhtml+xml"})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return response.status, headers_to_dict(response.headers), raw


def find_first_asset(frontend_url: str, html: str) -> str:
    for marker in ('src="', 'href="'):
        start = 0
        while True:
            index = html.find(marker, start)
            if index == -1:
                break
            value_start = index + len(marker)
            value_end = html.find('"', value_start)
            if value_end == -1:
                break
            candidate = html[value_start:value_end]
            if candidate.startswith("/assets/") and (candidate.endswith(".js") or candidate.endswith(".css")):
                return build_url(frontend_url, candidate)
            start = value_end + 1
    return ""


def header_value(headers: dict[str, str], name: str) -> str:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return ""


def require_header(headers: dict[str, str], name: str, expected: str) -> tuple[bool, str]:
    actual = header_value(headers, name)
    if expected.lower() in actual.lower():
        return True, actual
    return False, actual


def check_backend_health(backend_url: str, timeout: float) -> SmokeResult:
    url = build_url(backend_url, "/healthz")
    try:
        status, headers, data = request_json(url, timeout=timeout)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return SmokeResult("backend health", False, f"Backend health check failed: {exc}", {"url": url})

    ok = (
        status == 200
        and data.get("success") is True
        and data.get("status") == "ok"
        and data.get("database") == "ok"
    )
    if not ok:
        return SmokeResult("backend health", False, "Backend /healthz did not report ok database state.", {"url": url, "status": status, "body": data})

    header_checks = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
    }
    missing = {}
    for name, expected in header_checks.items():
        header_ok, actual = require_header(headers, name, expected)
        if not header_ok:
            missing[name] = actual
    if missing:
        return SmokeResult("backend health", False, "Backend security headers are missing or incorrect.", {"url": url, "headers": missing})

    return SmokeResult("backend health", True, "Backend health and security headers passed.", {"url": url})


def check_frontend_shell(frontend_url: str, timeout: float) -> tuple[SmokeResult, dict[str, str], str]:
    try:
        status, headers, html = request_text(frontend_url, timeout=timeout)
    except (HTTPError, URLError, TimeoutError) as exc:
        return SmokeResult("frontend shell", False, f"Frontend shell check failed: {exc}", {"url": frontend_url}), {}, ""

    has_root = 'id="root"' in html or "id='root'" in html
    has_entrypoint = "/assets/" in html or "/src/main" in html
    has_shell = status == 200 and has_root and has_entrypoint
    if not has_shell:
        return SmokeResult("frontend shell", False, "Frontend did not return the Vite app shell.", {"url": frontend_url, "status": status}), headers, html
    return SmokeResult("frontend shell", True, "Frontend app shell loaded.", {"url": frontend_url}), headers, html


def check_frontend_security(frontend_url: str, headers: dict[str, str], html: str, timeout: float) -> SmokeResult:
    required = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=()",
        "Content-Security-Policy": "default-src 'self'",
        "Cache-Control": "no-store",
    }
    missing = {}
    for name, expected in required.items():
        header_ok, actual = require_header(headers, name, expected)
        if not header_ok:
            missing[name] = actual
    if missing:
        return SmokeResult("frontend security headers", False, "Frontend shell security/cache headers are missing or incorrect.", {"headers": missing})

    asset_url = find_first_asset(frontend_url, html)
    if not asset_url:
        return SmokeResult("frontend security headers", False, "Could not find a frontend asset URL to verify immutable cache headers.")

    try:
        _, asset_headers, _ = request_text(asset_url, timeout=timeout)
    except (HTTPError, URLError, TimeoutError) as exc:
        return SmokeResult("frontend security headers", False, f"Could not fetch frontend asset: {exc}", {"assetUrl": asset_url})

    cache_ok, cache_header = require_header(asset_headers, "Cache-Control", "public, immutable")
    if not cache_ok:
        return SmokeResult("frontend security headers", False, "Frontend assets are not served with immutable cache headers.", {"assetUrl": asset_url, "cacheControl": cache_header})

    return SmokeResult("frontend security headers", True, "Frontend security and cache headers passed.", {"assetUrl": asset_url})


def check_demo_session(backend_url: str, api_prefix: str, demo_mobile: str, refresh_cookie_name: str, timeout: float) -> SmokeResult:
    url = build_url(backend_url, f"{api_prefix.rstrip('/')}/auth/demo-session")
    try:
        status, headers, data = request_json(url, method="POST", payload={"mobile": demo_mobile}, timeout=timeout)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return SmokeResult("demo session", False, f"Demo session check failed: {exc}", {"url": url})

    tokens = data.get("tokens") or {}
    set_cookie = header_value(headers, "Set-Cookie")
    has_cookie = f"{refresh_cookie_name}=" in set_cookie
    cookie_httponly = "httponly" in set_cookie.lower()
    if (
        status == 200
        and data.get("success") is True
        and tokens.get("access")
        and not tokens.get("refresh")
        and has_cookie
        and cookie_httponly
    ):
        return SmokeResult(
            "demo session",
            True,
            "Demo tenant session returned access JSON and HttpOnly refresh cookie.",
            {"url": url},
        )
    return SmokeResult(
        "demo session",
        False,
        "Demo tenant session did not match secure cookie auth expectations.",
        {
            "url": url,
            "status": status,
            "hasAccess": bool(tokens.get("access")),
            "hasRefreshInJson": bool(tokens.get("refresh")),
            "hasRefreshCookie": has_cookie,
            "refreshCookieHttpOnly": cookie_httponly,
            "body": redact_sensitive(data),
        },
    )


def run_smoke(args: argparse.Namespace) -> list[SmokeResult]:
    results = [check_backend_health(args.backend_url, args.timeout)]
    frontend_result, frontend_headers, frontend_html = check_frontend_shell(args.frontend_url, args.timeout)
    results.append(frontend_result)

    if frontend_result.ok and not args.skip_frontend_security:
        results.append(check_frontend_security(args.frontend_url, frontend_headers, frontend_html, args.timeout))

    if args.demo_mobile:
        results.append(check_demo_session(
            args.backend_url,
            args.api_prefix,
            args.demo_mobile,
            args.refresh_cookie_name,
            args.timeout,
        ))

    return results


def print_results(results: list[SmokeResult], *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps([result.__dict__ for result in results], indent=2, sort_keys=True))
        return

    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"[{status}] {result.name}: {result.message}")
        if result.details and not result.ok:
            print(json.dumps(result.details, indent=2, sort_keys=True))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-check a deployed VastraBook backend/frontend stack.")
    parser.add_argument("--backend-url", default=os.getenv("SMOKE_BACKEND_URL", DEFAULT_BACKEND_URL))
    parser.add_argument("--frontend-url", default=os.getenv("SMOKE_FRONTEND_URL", DEFAULT_FRONTEND_URL))
    parser.add_argument("--api-prefix", default=os.getenv("SMOKE_API_PREFIX", "/api/v1"))
    parser.add_argument("--demo-mobile", default=os.getenv("SMOKE_DEMO_MOBILE", ""))
    parser.add_argument("--refresh-cookie-name", default=os.getenv("AUTH_REFRESH_COOKIE_NAME", DEFAULT_REFRESH_COOKIE_NAME))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("SMOKE_TIMEOUT", "10")))
    parser.add_argument("--skip-frontend-security", action="store_true", help="Use for Vite/dev-server smoke checks. Production should not skip this.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON results.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    results = run_smoke(args)
    print_results(results, as_json=args.json)
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
