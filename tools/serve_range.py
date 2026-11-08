#!/usr/bin/env python3
"""Range-capable static file server for download pages.

Why this exists instead of `python3 -m http.server`:
  the stdlib handler ignores `Range`, so any interrupted tab re-downloads a
  100 MB installer from byte 0. On a phone that means the download never lands.
  It also sends no ETag / Cache-Control, so every visit re-transfers the page.

Compatibility: stdlib only, Python 3.8+ (sandbox and Windows both hit).
Contract with the deployer: bind 0.0.0.0 on $PORT, then serve `--root`.
"""

import argparse
import gzip
import hashlib
import os
import re
import sys
import threading
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

CHUNK = 64 * 1024

# Windows raises ConnectionAbortedError (WinError 10053) where POSIX raises
# ConnectionResetError. A user cancelling a resume hits this on every attempt,
# so it must stay silent — otherwise the log fills with tracebacks during the
# exact scenario Range support exists for.
CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

# Extension -> cache lifetime. Immutable artifacts get a year; the page itself
# must revalidate, otherwise a new version ships and nobody's browser notices.
CACHE_RULES = {
    ".html": "no-cache",
    ".htm": "no-cache",
    ".json": "no-cache",
    ".exe": "public, max-age=31536000, immutable",
    ".apk": "public, max-age=31536000, immutable",
    ".zip": "public, max-age=31536000, immutable",
    ".7z": "public, max-age=31536000, immutable",
    ".dmg": "public, max-age=31536000, immutable",
    ".cer": "public, max-age=604800",
    ".png": "public, max-age=604800",
    ".jpg": "public, max-age=604800",
    ".webp": "public, max-age=604800",
    ".svg": "public, max-age=604800",
    ".ico": "public, max-age=604800",
    ".woff2": "public, max-age=31536000, immutable",
}

COMPRESSIBLE = {
    ".html", ".htm", ".css", ".js", ".json", ".svg", ".txt", ".xml", ".map",
}

MIME = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".cer": "application/x-x509-ca-cert",
    ".exe": "application/octet-stream",
    ".apk": "application/vnd.android.package-archive",
    ".zip": "application/zip",
    ".7z": "application/x-7z-compressed",
    ".dmg": "application/x-apple-diskimage",
    ".md": "text/markdown; charset=utf-8",
}

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def gzip_bytes(raw):
    out = gzip.compress(raw, compresslevel=6)
    # Below ~1 KB the 18-byte header eats the gain and JSON/HTML shrink to nothing.
    return out if len(out) < len(raw) else raw


class Handler(BaseHTTPRequestHandler):
    server_version = "serve-static/1.0"
    protocol_version = "HTTP/1.1"
    root = "."

    def log_message(self, fmt, *args):
        sys.stderr.write("[serve] %s %s\n" % (self.address_string(), fmt % args))

    def handle(self):
        # The client can also vanish while the request line is still being read,
        # which blows up inside socketserver before any handler method runs.
        # Catching here keeps a cancelled download from printing a traceback.
        try:
            super().handle()
        except CLIENT_GONE:
            pass

    # ---------- path resolution ----------

    def _resolve(self):
        path = unquote(urlparse(self.path).path)
        if path.endswith("/"):
            path += "index.html"
        rel = os.path.normpath(path).lstrip("\\/")
        full = os.path.abspath(os.path.join(self.root, rel))
        # Reject traversal: the resolved path must stay under root.
        if not (full == self.root or full.startswith(self.root + os.sep)):
            return None
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        return full

    # ---------- validators ----------

    def _etag(self, st):
        return '"%x-%x"' % (int(st.st_mtime), st.st_size)

    def _not_modified(self, st, etag):
        inm = self.headers.get("If-None-Match")
        if inm and etag in [t.strip() for t in inm.split(",")]:
            return True
        ims = self.headers.get("If-Modified-Since")
        if ims:
            try:
                return int(st.st_mtime) <= int(parsedate_to_datetime(ims).timestamp())
            except (TypeError, ValueError):
                return False
        return False

    # ---------- verbs ----------

    def do_HEAD(self):
        self._serve(False)

    def do_GET(self):
        self._serve(True)

    def _serve(self, send_body):
        full = self._resolve()
        if full is None or not os.path.isfile(full):
            self._plain(404, "404 Not Found")
            return

        try:
            st = os.stat(full)
        except OSError:
            self._plain(404, "404 Not Found")
            return

        ext = os.path.splitext(full)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        etag = self._etag(st)
        cache = CACHE_RULES.get(ext, "public, max-age=3600")

        if self._not_modified(st, etag):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache)
            self.end_headers()
            return

        range_header = self.headers.get("Range")
        m = RANGE_RE.match(range_header.strip()) if range_header else None
        start, end = 0, st.st_size - 1
        partial = False
        if m and st.st_size:
            first, last = m.group(1), m.group(2)
            if first == "" and last == "":
                m = None
            else:
                partial = True
                if first == "":
                    # Suffix form: bytes=-500 means the last 500 bytes.
                    start = max(0, st.st_size - int(last))
                    end = st.st_size - 1
                else:
                    start = int(first)
                    end = int(last) if last else st.st_size - 1
                end = min(end, st.st_size - 1)
                if start > end or start >= st.st_size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % st.st_size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

        body = None
        use_gzip = False
        if not partial and ext in COMPRESSIBLE and st.st_size <= 8 * 1024 * 1024:
            accepts = self.headers.get("Accept-Encoding", "")
            if "gzip" in accepts.lower():
                with open(full, "rb") as fh:
                    raw = fh.read()
                gz = gzip_bytes(raw)
                if gz is not raw:
                    body = gz
                    use_gzip = True

        length = len(body) if body is not None else (end - start + 1)

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", formatdate(st.st_mtime, usegmt=True))
        self.send_header("Cache-Control", cache)
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, st.st_size))
        self.end_headers()

        if not send_body:
            return

        if body is not None:
            self.wfile.write(body)
            return

        remaining = length
        with open(full, "rb") as fh:
            fh.seek(start)
            while remaining > 0:
                chunk = fh.read(min(CHUNK, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except CLIENT_GONE:
                    return
                remaining -= len(chunk)

    def _plain(self, code, text):
        payload = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "3000")))
    ap.add_argument("--bind", default="0.0.0.0")
    args = ap.parse_args()

    Handler.root = os.path.abspath(args.root)
    httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    httpd.daemon_threads = True
    sys.stderr.write("[serve] root=%s port=%d pid=%d\n" % (Handler.root, args.port, os.getpid()))
    sys.stderr.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
