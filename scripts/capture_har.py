#!/usr/bin/env python3
"""
capture_har.py — turn a recorded HAR file into a readable "request catalogue".

Phase 0 recon tool for Tennis-Bot. You record a real booking session with your
browser's DevTools (Network tab -> "Save all as HAR with content"); this script
parses that HAR and produces a human-readable summary of the API calls that
matter, so we can design the provider adapters.

SAFETY
------
HAR files contain secrets: session cookies, Authorization tokens, CSRF tokens,
and anything typed into forms (incl. card data if you didn't stop early). This
script REDACTS those by default in its output. It never transmits anything —
it only reads a local file and writes a local summary.

Stdlib only — no install needed. Requires Python 3.9+.

USAGE
-----
    python3 scripts/capture_har.py recon/hyde-park-recon.har
    python3 scripts/capture_har.py recon/*.har
    python3 scripts/capture_har.py recon/paddington-recon.har --out recon/out/paddington.md

    # Show secrets in the clear (NOT recommended; never commit the result):
    python3 scripts/capture_har.py recon/foo.har --no-redact

By default a Markdown summary is written next to the HAR under recon/out/,
and a short overview is printed to the terminal.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

# ──────────────────────────────────────────────────────────────────────────────
# What counts as sensitive. Matched case-insensitively as a substring.
# ──────────────────────────────────────────────────────────────────────────────
SENSITIVE_HEADERS = {
    "cookie", "set-cookie", "authorization", "proxy-authorization",
    "x-csrf-token", "x-xsrf-token", "x-auth-token", "x-api-key", "api-key",
}
# Substrings that mark a header/field name as sensitive.
SENSITIVE_NAME_HINTS = (
    "token", "secret", "password", "passwd", "auth", "session", "cookie",
    "csrf", "xsrf", "card", "cvv", "cvc", "pan", "cardnumber", "securitycode",
    "apikey", "api_key", "bearer", "signature",
)

# Only these MIME types are worth dumping as bodies.
INTERESTING_BODY_TYPES = ("json", "x-www-form-urlencoded", "text", "xml")

# Asset extensions / types we don't care about for API recon.
BORING_EXTENSIONS = (
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".ico", ".map", ".mp4", ".avif",
)
BORING_TYPE_HINTS = ("image/", "font/", "text/css", "javascript")

MAX_BODY_CHARS = 4000  # truncate long bodies in the summary


def is_sensitive_name(name: str) -> bool:
    n = name.lower()
    if n in SENSITIVE_HEADERS:
        return True
    return any(hint in n for hint in SENSITIVE_NAME_HINTS)


def redact_value(value: str, redact: bool) -> str:
    if not redact:
        return value
    if value is None:
        return ""
    v = str(value)
    if len(v) <= 8:
        return "***REDACTED***"
    return f"***REDACTED*** (len={len(v)}, starts='{v[:4]}…')"


def looks_boring(url: str, mime: str) -> bool:
    path = urlparse(url).path.lower()
    if any(path.endswith(ext) for ext in BORING_EXTENSIONS):
        return True
    m = (mime or "").lower()
    if any(hint in m for hint in BORING_TYPE_HINTS):
        return True
    return False


def body_is_interesting(mime: str) -> bool:
    m = (mime or "").lower()
    return any(t in m for t in INTERESTING_BODY_TYPES)


def fmt_headers(headers: list[dict], redact: bool) -> list[str]:
    lines = []
    for h in headers:
        name = h.get("name", "")
        if name.startswith(":"):  # HTTP/2 pseudo-headers — skip noise
            continue
        val = h.get("value", "")
        if is_sensitive_name(name):
            val = redact_value(val, redact)
        lines.append(f"      {name}: {val}")
    return lines


def fmt_query(params: list[dict], redact: bool) -> list[str]:
    lines = []
    for p in params:
        name = p.get("name", "")
        val = p.get("value", "")
        if is_sensitive_name(name):
            val = redact_value(val, redact)
        lines.append(f"      {name} = {val}")
    return lines


def redact_json_body(text: str, redact: bool) -> str:
    """If body is JSON, redact sensitive keys recursively; else return as-is."""
    if not redact:
        return text
    try:
        data = json.loads(text)
    except Exception:
        return text  # not JSON; leave to the generic truncation/printing

    def walk(obj):
        if isinstance(obj, dict):
            return {
                k: ("***REDACTED***" if is_sensitive_name(str(k)) else walk(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [walk(i) for i in obj]
        return obj

    try:
        return json.dumps(walk(data), indent=2)[:MAX_BODY_CHARS]
    except Exception:
        return text[:MAX_BODY_CHARS]


def fmt_post_body(post: dict, redact: bool) -> list[str]:
    if not post:
        return []
    mime = post.get("mimeType", "")
    lines = [f"      (mimeType: {mime})"]

    # urlencoded form params come pre-parsed in HAR
    params = post.get("params")
    if params:
        for p in params:
            name = p.get("name", "")
            val = p.get("value", "")
            if is_sensitive_name(name):
                val = redact_value(val, redact)
            lines.append(f"      {name} = {val}")
        return lines

    text = post.get("text", "")
    if not text:
        return lines
    if not body_is_interesting(mime):
        lines.append("      <non-text body omitted>")
        return lines
    body = redact_json_body(text, redact)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n      …<truncated>"
    for ln in body.splitlines():
        lines.append(f"      {ln}")
    return lines


def fmt_response(resp: dict, redact: bool) -> list[str]:
    status = resp.get("status", "?")
    status_text = resp.get("statusText", "")
    content = resp.get("content", {}) or {}
    mime = content.get("mimeType", "")
    size = content.get("size", "?")
    lines = [f"    <- {status} {status_text}  ({mime}, {size} bytes)"]
    text = content.get("text", "")
    if text and body_is_interesting(mime):
        body = redact_json_body(text, redact)
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS] + "\n      …<truncated>"
        lines.append("    response body:")
        for ln in body.splitlines():
            lines.append(f"      {ln}")
    return lines


def parse_har(path: Path, redact: bool) -> tuple[str, dict]:
    """Return (markdown_summary, stats)."""
    raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    entries = raw.get("log", {}).get("entries", [])

    out: list[str] = []
    out.append(f"# Recon catalogue — {path.name}")
    out.append("")
    out.append(f"> Redaction: {'ON (safe to review)' if redact else 'OFF — CONTAINS SECRETS'}")
    out.append(f"> Total HAR entries: {len(entries)}")
    out.append("")

    hosts: dict[str, int] = {}
    kept = 0
    skipped = 0

    api_lines: list[str] = []

    for e in entries:
        req = e.get("request", {})
        resp = e.get("response", {})
        url = req.get("url", "")
        method = req.get("method", "")
        resp_mime = (resp.get("content", {}) or {}).get("mimeType", "")

        if looks_boring(url, resp_mime):
            skipped += 1
            continue

        kept += 1
        host = urlparse(url).netloc
        hosts[host] = hosts.get(host, 0) + 1

        api_lines.append("")
        api_lines.append("─" * 78)
        api_lines.append(f"### {method} {url}")
        api_lines.append("")

        query = req.get("queryString", [])
        if query:
            api_lines.append("    query params:")
            api_lines.extend(fmt_query(query, redact))

        headers = req.get("headers", [])
        if headers:
            api_lines.append("    request headers:")
            api_lines.extend(fmt_headers(headers, redact))

        post = req.get("postData")
        if post:
            api_lines.append("    request body:")
            api_lines.extend(fmt_post_body(post, redact))

        api_lines.extend(fmt_response(resp, redact))

    # Overview section
    out.append("## Hosts seen (API-ish requests only)")
    out.append("")
    for host, count in sorted(hosts.items(), key=lambda x: -x[1]):
        out.append(f"- `{host}` — {count} request(s)")
    out.append("")
    out.append(f"Kept {kept} API-ish request(s); skipped {skipped} asset(s).")
    out.append("")
    out.append("## Requests")
    out.extend(api_lines)
    out.append("")

    stats = {"entries": len(entries), "kept": kept, "skipped": skipped, "hosts": hosts}
    return "\n".join(out), stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse a HAR into a recon catalogue.")
    ap.add_argument("har", nargs="+", help="HAR file(s) or glob(s), e.g. recon/*.har")
    ap.add_argument("--out", help="Output .md path (single input only). "
                                  "Default: recon/out/<name>.md")
    ap.add_argument("--no-redact", action="store_true",
                    help="Do NOT redact secrets (dangerous; never commit result).")
    args = ap.parse_args()

    redact = not args.no_redact

    # Expand globs (shell may not on all setups)
    paths: list[Path] = []
    for pattern in args.har:
        matched = [Path(p) for p in glob.glob(pattern)]
        paths.extend(matched if matched else [Path(pattern)])

    paths = [p for p in paths if p.suffix.lower() == ".har" and p.exists()]
    if not paths:
        print("No .har files found. Did you save the recording? "
              "(DevTools Network -> 'Save all as HAR with content')", file=sys.stderr)
        return 1

    if args.out and len(paths) > 1:
        print("--out only works with a single input HAR.", file=sys.stderr)
        return 1

    for path in paths:
        try:
            summary, stats = parse_har(path, redact)
        except Exception as exc:  # noqa: BLE001
            print(f"[!] Failed to parse {path}: {exc}", file=sys.stderr)
            continue

        if args.out:
            out_path = Path(args.out)
        else:
            out_dir = path.parent / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / (path.stem + ".md")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(summary, encoding="utf-8")

        print(f"✓ {path.name}: {stats['kept']} API request(s) across "
              f"{len(stats['hosts'])} host(s) -> {out_path}")
        for host, count in sorted(stats["hosts"].items(), key=lambda x: -x[1])[:6]:
            print(f"    {host}  ({count})")

    if redact:
        print("\nRedaction was ON. Safe to skim, but still don't commit recon/out/.")
    else:
        print("\n⚠  Redaction was OFF — output contains live secrets. Handle carefully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
