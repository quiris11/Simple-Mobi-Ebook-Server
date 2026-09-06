#!/usr/bin/env python3
"""
simple_mobi_ebook_server.py - Simple Mobi Ebook Server

A tiny, dependency-free HTTP server that serves .mobi files from a folder
you choose (searched recursively, including all subfolders). Handy for
downloading them onto a Kindle 4 (or similar old Kindle without wireless
Send-to-Kindle) using its "Experimental > Basic Web Browser" - or onto
any other device with a plain web browser. The index page lists every
.mobi file found, showing its path relative to the chosen folder (e.g.
"Sci-Fi/Dune.mobi") so you can tell which subfolder each one came from.

Usage:
    python3 simple_mobi_ebook_server.py -f /path/to/books -p 8000

On a Kindle 4:
    1. Connect the Kindle to the same Wi-Fi network as this computer.
    2. Open Menu -> Experimental -> Basic Web Browser (naming may vary
       slightly by firmware version).
    3. Type the address this script prints, e.g. http://192.168.1.23:8000/
    4. Tap/select a book title. The Kindle should download it and offer
       to open/save it. If it doesn't show up on the Home screen right
       away, try Home -> Menu -> "Sync and Check for Items", or restart.

No external dependencies - standard library only.
"""

import argparse
import html
import os
import re
import shutil
import socket
import sys
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, quote

MOBI_MIME = "application/x-mobipocket-ebook"
CHUNK_SIZE = 64 * 1024
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def parse_byte_range(range_header, file_size):
    """Parse a single-range 'Range: bytes=start-end' header.
    Returns an inclusive (start, end) tuple, or None if there's no
    (valid) range to honor - callers should then serve the whole file.
    Kindle's download manager (like most) issues Range requests when
    resuming an interrupted download, so supporting this avoids corrupt
    re-downloads after a dropped connection."""
    if not range_header:
        return None
    m = RANGE_RE.match(range_header.strip())
    if not m:
        return None
    start_s, end_s = m.groups()
    if start_s == "" and end_s == "":
        return None
    if start_s == "":
        # Suffix range, e.g. "bytes=-500" -> last 500 bytes.
        length = int(end_s)
        if length <= 0:
            return None
        start, end = max(file_size - length, 0), file_size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s != "" else file_size - 1
    if start > end or start >= file_size:
        return None
    return start, min(end, file_size - 1)


def get_local_ip():
    """Best-effort LAN IP address, just so we can print a useful URL."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def list_mobi_files(folder):
    """Recursively find .mobi files under `folder`, returning paths
    relative to it (using '/' as separator, for use in URLs)."""
    results = []
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(".mobi"):
                full = os.path.join(root, f)
                rel = os.path.relpath(full, folder).replace(os.sep, "/")
                results.append(rel)
    results.sort(key=str.lower)
    return results


def content_disposition_header(filename):
    """Build a Content-Disposition value that's safe to hand to
    send_header(), which encodes as latin-1. Book titles routinely
    contain non-latin-1 characters (Polish 'l with stroke', 'z with
    dot', etc.), which would otherwise raise UnicodeEncodeError and
    kill the request thread.

    We deliberately send a single, plain ASCII `filename="..."` and
    nothing fancier. An earlier version of this also added the RFC 6266
    `filename*=UTF-8''...` extension so modern clients would see the
    exact original name - but the Kindle 4's "Basic Web Browser" download
    manager can't parse that two-parameter header and resets the
    connection as soon as it sees it. Plain ASCII is the one thing every
    HTTP client, however old, is guaranteed to handle, so accented
    letters are transliterated to their closest ASCII equivalent (Polish
    l-with-stroke becomes plain 'l', etc.) instead of being dropped."""
    # Polish letters that don't decompose under NFKD (l-with-stroke has
    # no accent to strip - it's a distinct base letter) get a manual
    # mapping; everything else (a-ogonek, c-acute, z-dot, etc.) is
    # handled generically by NFKD + stripping combining marks below.
    filename = filename.replace("\u0142", "l").replace("\u0141", "L")
    decomposed = unicodedata.normalize("NFKD", filename)
    ascii_name = decomposed.encode("ascii", "ignore").decode("ascii")
    # Anything still non-ASCII (other alphabets, emoji, ...) becomes '_'.
    ascii_name = "".join(c if 32 <= ord(c) < 127 else "_" for c in ascii_name)
    ascii_name = ascii_name.replace('"', "_").replace("\\", "_").strip() or "book.mobi"
    return f'attachment; filename="{ascii_name}"'


def make_handler(folder):
    class MobiHandler(BaseHTTPRequestHandler):
        server_version = "SimpleMobiEbookServer/1.0"

        def _send_index(self):
            files = list_mobi_files(folder)
            rows = []
            for rel in files:
                path = os.path.join(folder, *rel.split("/"))
                size_mb = os.path.getsize(path) / (1024 * 1024)
                safe_name = html.escape(rel)
                href = quote(rel)
                rows.append(
                    f'<li><a href="/{href}">{safe_name}</a> &nbsp;({size_mb:.1f} MB)</li>'
                )
            body = (
                "<html><head><title>Simple Mobi Ebook Server</title></head><body>"
                "<h1>Available books (.mobi)</h1>"
                + ("<ul>" + "".join(rows) + "</ul>" if rows else "<p>No .mobi files found.</p>")
                + f"<p>Folder: {html.escape(folder)}</p>"
                "</body></html>"
            )
            encoded = body.encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (ConnectionResetError, BrokenPipeError, TimeoutError) as e:
                sys.stderr.write(
                    f"{self.client_address[0]} - connection dropped while sending index "
                    f"({e.__class__.__name__})\n"
                )

        def _send_file(self, raw_path):
            # Path-traversal guard: resolve symlinks/".." and make sure
            # the result still lives inside `folder`. Subfolders are
            # allowed (that's the point), "../" escapes are not.
            rel_path = unquote(raw_path)
            candidate = os.path.normpath(os.path.join(folder, rel_path))
            real_folder = os.path.realpath(folder)
            real_path = os.path.realpath(candidate)
            valid = (
                real_path.startswith(real_folder + os.sep)
                and os.path.isfile(real_path)
                and rel_path.lower().endswith(".mobi")
            )
            if not valid:
                self.send_error(404, "File not found")
                return

            download_name = os.path.basename(real_path)
            size = os.path.getsize(real_path)
            byte_range = parse_byte_range(self.headers.get("Range"), size)

            try:
                with open(real_path, "rb") as f:
                    if byte_range:
                        start, end = byte_range
                        length = end - start + 1
                        self.send_response(206)
                        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                        self.send_header("Content-Length", str(length))
                        f.seek(start)
                    else:
                        length = size
                        self.send_response(200)
                        self.send_header("Content-Length", str(length))
                    self.send_header("Content-Type", MOBI_MIME)
                    self.send_header("Content-Disposition", content_disposition_header(download_name))
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()

                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(CHUNK_SIZE, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (ConnectionResetError, BrokenPipeError, TimeoutError) as e:
                # The Kindle's browser/download manager closed or reset the
                # connection mid-transfer - it happens, especially on flaky
                # Wi-Fi, and it usually just retries (often with a Range
                # header, handled above). Log one short line and move on
                # instead of crashing the request thread with a traceback.
                sys.stderr.write(
                    f"{self.client_address[0]} - connection dropped while sending "
                    f"{download_name} ({e.__class__.__name__})\n"
                )

        def do_GET(self):
            if self.path in ("/", ""):
                self._send_index()
            else:
                self._send_file(self.path.lstrip("/"))

        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    return MobiHandler


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Same as ThreadingHTTPServer, but doesn't dump a full traceback to
    the console when a client drops the connection mid-request (the
    per-request try/except above catches most of these, but this is a
    safety net for anything that slips through, e.g. in the library's
    own post-request socket flush)."""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, TimeoutError)):
            sys.stderr.write(f"{client_address[0]} - connection dropped ({exc.__class__.__name__})\n")
        else:
            super().handle_error(request, client_address)


def main():
    parser = argparse.ArgumentParser(
        description="Simple Mobi Ebook Server - serve .mobi files over HTTP "
                     "(e.g. for downloading onto a Kindle 4's experimental browser)."
    )
    parser.add_argument(
        "-f", "--folder", default=".",
        help="Folder containing .mobi files (default: current directory)",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=8000,
        help="Port to listen on (default: 8000)",
    )
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="Address to bind to (default: 0.0.0.0, i.e. whole LAN)",
    )
    args = parser.parse_args()

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        print(f"Error: folder does not exist: {folder}", file=sys.stderr)
        sys.exit(1)

    handler = make_handler(folder)
    server = QuietThreadingHTTPServer((args.host, args.port), handler)

    ip = get_local_ip()
    print(f"Folder:  {folder}")
    print(f"Server:  http://{ip}:{args.port}/")
    print("On the Kindle: type the address above into Basic Web Browser (Experimental).")
    print("Stop with Ctrl+C")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
