#!/usr/bin/env python3
"""
Kids Flashcard Web Server (Port 8090).
Serves static frontend, cropped flashcard images, MP3 audio with HTTP Range support,
and provides /api/catalog and /api/sync endpoints with background Google Drive auto-sync.
"""

import argparse
import json
import mimetypes
import os
import re
import threading
import time
import urllib.parse
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn

from sync_and_crop import APP_DIR, CATALOG_PATH, DATA_DIR, build_catalog

STATIC_DIR = os.path.join(APP_DIR, "static")
_sync_lock = threading.Lock()


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class FlashcardRequestHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Keep logs concise
        pass

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)

        if path == "/api/catalog":
            self._handle_get_catalog()
            return

        if path.startswith("/data/"):
            rel = path[len("/data/"):]
            full_path = os.path.normpath(os.path.join(DATA_DIR, rel))
            if not full_path.startswith(DATA_DIR) or not os.path.isfile(full_path):
                self.send_error(404, "File not found")
                return
            self._serve_file_with_range(full_path)
            return

        # Serve static frontend files
        if path == "/" or path == "":
            path = "/index.html"
        rel = path.lstrip("/")
        full_path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full_path.startswith(STATIC_DIR) or not os.path.isfile(full_path):
            self.send_error(404, "Not found")
            return
        self._serve_file_with_range(full_path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/sync":
            self._handle_post_sync()
            return
        self.send_error(404, "Endpoint not found")

    def _handle_get_catalog(self):
        if not os.path.exists(CATALOG_PATH):
            with _sync_lock:
                if not os.path.exists(CATALOG_PATH):
                    build_catalog(sync_from_drive=False)
        with open(CATALOG_PATH, "rb") as f:
            payload = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_post_sync(self):
        try:
            with _sync_lock:
                catalog = build_catalog(sync_from_drive=True)
            payload = json.dumps({"status": "ok", "catalog": catalog}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as e:
            err = json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

    def _serve_file_with_range(self, full_path):
        ctype, _ = mimetypes.guess_type(full_path)
        if not ctype:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"

        file_size = os.path.getsize(full_path)
        range_header = self.headers.get("Range")

        if range_header:
            m = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else file_size - 1
                end = min(end, file_size - 1)
                if start <= end and start < file_size:
                    length = end - start + 1
                    self.send_response(206)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                    self.send_header("Content-Length", str(length))
                    self.end_headers()
                    with open(full_path, "rb") as f:
                        f.seek(start)
                        self.wfile.write(f.read(length))
                    return

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        if "/data/cards/" in full_path or "/data/raw/" in full_path:
            self.send_header("Cache-Control", "public, max-age=86400")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(file_size))
        self.end_headers()
        with open(full_path, "rb") as f:
            self.wfile.write(f.read())


def background_drive_sync_loop(interval_sec=600):
    """Periodically syncs new files from Google Drive every 10 minutes."""
    while True:
        time.sleep(interval_sec)
        try:
            with _sync_lock:
                build_catalog(sync_from_drive=True)
        except Exception as e:
            print(f"[AutoSync] Warning: {e}")


def main():
    parser = argparse.ArgumentParser(description="Kids Flashcard Web Server")
    parser.add_argument("--port", type=int, default=8090, help="Port to listen on (default: 8090)")
    args = parser.parse_args()

    t = threading.Thread(target=background_drive_sync_loop, args=(600,), daemon=True)
    t.start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), FlashcardRequestHandler)
    print(f"Kids Flashcard Web Server listening on http://0.0.0.0:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
