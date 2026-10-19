#!/usr/bin/env python3
"""
simple_mobi_ebook_server.py - Simple Mobi Ebook Server

A tiny, dependency-free HTTP server that serves .mobi files from a folder
you choose (searched recursively, including all subfolders). Handy for
downloading them onto a Kindle 4 (or similar old Kindle without wireless
Send-to-Kindle) using its "Experimental > Basic Web Browser" - or onto
any other device with a plain web browser. The index page lists every
.mobi file found, showing its path relative to the chosen folder (e.g.
"Sci-Fi/Dune.mobi") so you can tell which subfolder each one came from,
plus a search box to filter it.

If Amazon's kindlegen is available, .epub files are listed too and get
converted to .mobi on demand the first time someone picks them (the
result is cached, so later downloads are instant).

Everything is served under a secret token in the URL path (e.g.
http://host:8000/k7x2m9qp4wza/), so the server can be exposed to the
internet - say, for a Kindle on a phone's hotspot - without strangers
being able to browse it. Requests without the token get a plain 404.

Usage:
    python3 simple_mobi_ebook_server.py -f /path/to/books -p 8000
    python3 simple_mobi_ebook_server.py -f /path/to/books --token mysecret \\
        --kindlegen ~/kindlegen/kindlegen

On a Kindle 4:
    1. Connect the Kindle to the same Wi-Fi network as this computer
       (or to a phone hotspot, if the server is reachable from outside).
    2. Open Menu -> Experimental -> Basic Web Browser (naming may vary
       slightly by firmware version).
    3. Type the address this script prints, e.g.
       http://192.168.1.23:8000/k7x2m9qp4wza/ - and bookmark it.
    4. Tap/select a book title. The Kindle should download it and offer
       to open/save it. If it doesn't show up on the Home screen right
       away, try Home -> Menu -> "Sync and Check for Items", or restart.

No external dependencies - standard library only (kindlegen is optional).
"""

import argparse
import hashlib
import hmac
import html
import os
import re
import secrets
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import threading
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, quote, urlsplit, parse_qs

MOBI_MIME = "application/x-mobipocket-ebook"
CHUNK_SIZE = 64 * 1024
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,}$")
# 12 chars of [a-z0-9] is ~62 bits - far beyond guessing over the network,
# yet short enough to type once on the Kindle's keyboard and bookmark.
TOKEN_ALPHABET = string.ascii_lowercase + string.digits
TOKEN_LENGTH = 12
# URL prefix for .epub books served as converted .mobi, e.g.
# "/_convert/Sci-Fi/Dune.epub.mobi". The URL must end in ".mobi": the
# Kindle's download manager goes by the extension and won't fetch .epub.
CONVERT_PREFIX = "/_convert/"
CONVERT_TIMEOUT = 600  # seconds; kindlegen on a huge, image-heavy book is slow
WAIT_REFRESH = 5  # seconds between "still converting" page reloads
DROPPED = (ConnectionResetError, BrokenPipeError, TimeoutError)
# Idle seconds before a connection is closed. Without it, a client that
# connects and never sends anything ties up a thread forever - and enough
# of those make the server stop answering. It's a per-read/write limit,
# so a long, slow download is fine as long as data keeps flowing.
CONNECTION_TIMEOUT = 60
# Control characters (ASCII and C1) escaped as \xNN before logging, so a
# request line can't smuggle terminal escape sequences into the console.
CONTROL_CHARS = {c: f"\\x{c:02x}" for c in [*range(0x20), *range(0x7F, 0xA0)]}


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


def list_books(folder, include_epub):
    """Recursively find .mobi (and, if `include_epub`, .epub) files under
    `folder`, returning paths relative to it (using '/' as separator, for
    use in URLs). An .epub with a .mobi of the same name next to it is
    skipped - the ready-made .mobi wins, no point converting."""
    exts = (".mobi", ".epub") if include_epub else (".mobi",)
    results = []
    for root, _dirs, files in os.walk(folder):
        mobi_stems = {f[:-5].lower() for f in files if f.lower().endswith(".mobi")}
        for f in files:
            lower = f.lower()
            if not lower.endswith(exts):
                continue
            if lower.endswith(".epub") and lower[:-5] in mobi_stems:
                continue
            full = os.path.join(root, f)
            rel = os.path.relpath(full, folder).replace(os.sep, "/")
            results.append(rel)
    results.sort(key=str.lower)
    return results


def ascii_fold(text):
    """Transliterate to plain ASCII: accents stripped, Polish l-with-stroke
    turned into 'l'. Characters with no ASCII equivalent (other alphabets,
    emoji, ...) are dropped."""
    # Polish letters that don't decompose under NFKD (l-with-stroke has
    # no accent to strip - it's a distinct base letter) get a manual
    # mapping; everything else (a-ogonek, c-acute, z-dot, etc.) is
    # handled generically by NFKD + stripping combining marks below.
    text = text.replace("ł", "l").replace("Ł", "L")
    decomposed = unicodedata.normalize("NFKD", text)
    return decomposed.encode("ascii", "ignore").decode("ascii")


def matches_query(rel, query):
    """True if every word of `query` occurs in `rel`, ignoring case and
    accents - typing Polish letters on the Kindle's on-screen keyboard is
    a pain, so "zolkiewski" should find "Żółkiewski"."""
    haystack = ascii_fold(rel).lower()
    return all(word in haystack for word in ascii_fold(query).lower().split())


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
    # Control characters left after folding become '_'.
    ascii_name = "".join(c if 32 <= ord(c) < 127 else "_" for c in ascii_fold(filename))
    ascii_name = ascii_name.replace('"', "_").replace("\\", "_").strip() or "book.mobi"
    return f'attachment; filename="{ascii_name}"'


class EpubConverter:
    """Converts .epub files to .mobi with kindlegen in background threads,
    caching the results in `cache_dir`. The cache key includes the epub's
    mtime and size, so an edited epub gets converted afresh."""

    def __init__(self, kindlegen, cache_dir):
        self.kindlegen = kindlegen
        self.cache_dir = cache_dir
        self._lock = threading.Lock()
        self._running = set()  # cache paths currently being converted
        self._failed = {}  # cache path -> error message, shown once

    def _cache_path(self, epub_path):
        st = os.stat(epub_path)
        key = f"{epub_path}\0{st.st_mtime_ns}\0{st.st_size}"
        digest = hashlib.sha1(key.encode("utf-8", "surrogateescape")).hexdigest()
        return os.path.join(self.cache_dir, digest + ".mobi")

    def status(self, epub_path):
        """Return ("ready", mobi_path), ("converting", None) or
        ("failed", message), starting a conversion if there's neither a
        cached result nor one already running. A conversion runs only
        once even when requests pile up - and they do: the Kindle's
        browser and its download manager each request the same URL."""
        dest = self._cache_path(epub_path)
        with self._lock:
            if dest in self._running:
                return "converting", None
            if os.path.isfile(dest):
                return "ready", dest
            if dest in self._failed:
                # Report the failure once; the next visit tries again.
                return "failed", self._failed.pop(dest)
            self._running.add(dest)
        threading.Thread(target=self._convert, args=(epub_path, dest), daemon=True).start()
        return "converting", None

    def _convert(self, epub_path, dest):
        name = os.path.basename(epub_path)
        sys.stderr.write(f"converting {name} ...\n")
        error = None
        try:
            # kindlegen writes its output next to its input and has trouble
            # with non-ASCII file names, so it works on a plainly named
            # copy in a scratch directory; the result is moved into place
            # atomically, so a half-written .mobi is never served.
            with tempfile.TemporaryDirectory(dir=self.cache_dir) as tmp:
                src = os.path.join(tmp, "book.epub")
                out = os.path.join(tmp, "book.mobi")
                shutil.copyfile(epub_path, src)
                proc = subprocess.run(
                    [self.kindlegen, src, "-dont_append_source", "-o", "book.mobi"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    timeout=CONVERT_TIMEOUT,
                )
                # Exit code 1 means "built, but with warnings" - normal for
                # real-world epubs. Only 2 (or no output file) is a failure.
                if proc.returncode in (0, 1) and os.path.isfile(out):
                    os.replace(out, dest)
                else:
                    log = proc.stdout.decode("utf-8", "replace").splitlines()
                    errors = [line for line in log if "Error" in line]
                    error = "\n".join(errors[-5:] or log[-5:]) or f"kindlegen exited with code {proc.returncode}"
        except subprocess.TimeoutExpired:
            error = f"kindlegen took longer than {CONVERT_TIMEOUT} s and was stopped"
        except OSError as e:
            error = str(e)
        with self._lock:
            self._running.discard(dest)
            if error:
                self._failed[dest] = error
        sys.stderr.write(f"conversion of {name} {'FAILED: ' + error if error else 'done'}\n")


def make_handler(folder, token, converter):
    real_folder = os.path.realpath(folder)
    # Prefix for every link on our pages: "/<token>", or "" without one.
    base = f"/{token}" if token else ""

    def resolve(rel_path, ext):
        """Map a URL path (already unquoted) to a real file under `folder`
        ending in `ext`, or None. Path-traversal guard: resolve symlinks
        and ".." and make sure the result still lives inside `folder`.
        Subfolders are allowed (that's the point), "../" escapes are not."""
        real_path = os.path.realpath(os.path.join(folder, rel_path))
        valid = (
            real_path.startswith(real_folder + os.sep)
            and os.path.isfile(real_path)
            and rel_path.lower().endswith(ext)
        )
        return real_path if valid else None

    class MobiHandler(BaseHTTPRequestHandler):
        timeout = CONNECTION_TIMEOUT

        def send_response(self, code, message=None):
            # Same as the base class, minus the "Server: BaseHTTP/x.y
            # Python/x.y.z" header - no need to tell every scanner on the
            # internet what's running here.
            self.log_request(code)
            self.send_response_only(code, message)
            self.send_header("Date", self.date_time_string())

        def _send_html(self, title, content, head=""):
            body = (
                f"<html><head><title>{html.escape(title)}</title>{head}</head><body>"
                + content
                + "</body></html>"
            )
            encoded = body.encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(encoded)
            except DROPPED as e:
                sys.stderr.write(
                    f"{self.client_address[0]} - connection dropped while sending page "
                    f"({e.__class__.__name__})\n"
                )

        def _send_index(self, query):
            books = list_books(folder, include_epub=converter is not None)
            total = len(books)
            if query:
                books = [rel for rel in books if matches_query(rel, query)]
            rows = []
            for rel in books:
                path = os.path.join(folder, *rel.split("/"))
                size_mb = os.path.getsize(path) / (1024 * 1024)
                safe_name = html.escape(rel)
                if rel.lower().endswith(".epub"):
                    href = base + CONVERT_PREFIX + quote(rel) + ".mobi"
                    info = f"EPUB, {size_mb:.1f} MB"
                else:
                    href = f"{base}/{quote(rel)}"
                    info = f"{size_mb:.1f} MB"
                rows.append(f'<li><a href="{href}">{safe_name}</a> &nbsp;({info})</li>')

            kinds = ".mobi / .epub" if converter else ".mobi"
            content = (
                f"<h1>Available books ({kinds})</h1>"
                f'<form method="get" action="{base}/">'
                f'<input type="text" name="q" value="{html.escape(query, quote=True)}"> '
                '<input type="submit" value="Search"></form>'
            )
            if query:
                content += (
                    f"<p>{len(books)} of {total} books match "
                    f"&quot;{html.escape(query)}&quot; - <a href=\"{base}/\">show all</a></p>"
                )
            if rows:
                content += "<ul>" + "".join(rows) + "</ul>"
            else:
                content += "<p>No matching books.</p>" if query else "<p>No books found.</p>"
            content += f"<p>Folder: {html.escape(folder)}</p>"
            self._send_html("Simple Mobi Ebook Server", content)

        def _send_file(self, real_path, download_name):
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
            except DROPPED as e:
                # The Kindle's browser/download manager closed or reset the
                # connection mid-transfer - it happens, especially on flaky
                # Wi-Fi, and it usually just retries (often with a Range
                # header, handled above). Log one short line and move on
                # instead of crashing the request thread with a traceback.
                sys.stderr.write(
                    f"{self.client_address[0]} - connection dropped while sending "
                    f"{download_name} ({e.__class__.__name__})\n"
                )

        def _send_mobi(self, rel_path):
            real_path = resolve(rel_path, ".mobi")
            if real_path is None:
                self.send_error(404, "File not found")
                return
            self._send_file(real_path, os.path.basename(real_path))

        def _send_converted(self, rel_path):
            # rel_path is "<path of the epub>.mobi", e.g. "Sci-Fi/Dune.epub.mobi".
            epub_rel = rel_path[:-5] if rel_path.lower().endswith(".mobi") else ""
            epub_path = resolve(epub_rel, ".epub")
            if converter is None or epub_path is None:
                self.send_error(404, "File not found")
                return

            title = os.path.basename(epub_path)
            state, result = converter.status(epub_path)
            if state == "ready":
                self._send_file(result, title[:-5] + ".mobi")
            elif state == "converting":
                # The Kindle 4's browser honors meta refresh: it keeps
                # reloading this URL until the .mobi is ready, then hands
                # the response to its download manager like any other book.
                self._send_html(
                    "Converting...",
                    f"<h1>Converting {html.escape(title)}</h1>"
                    f"<p>This page reloads every {WAIT_REFRESH} seconds and the "
                    "download starts by itself once the book is ready - usually "
                    "within a minute.</p>"
                    f'<p><a href="{html.escape(self.path, quote=True)}">Reload now</a>'
                    f' &nbsp; <a href="{base}/">Back to the list</a></p>',
                    head=f'<meta http-equiv="refresh" content="{WAIT_REFRESH}">',
                )
            else:
                self._send_html(
                    "Conversion failed",
                    f"<h1>Could not convert {html.escape(title)}</h1>"
                    f"<pre>{html.escape(result)}</pre>"
                    f'<p><a href="{html.escape(self.path, quote=True)}">Try again</a>'
                    f' &nbsp; <a href="{base}/">Back to the list</a></p>',
                )

        def do_GET(self):
            url = urlsplit(self.path)
            path = url.path
            if token:
                # Everything lives under "/<token>/"; anything else is a 404,
                # so the server looks empty to whoever stumbles upon it.
                # (self.path is latin-1 decoded by http.server, so the
                # encode below can't fail on odd bytes.)
                first, slash, rest = path[1:].partition("/")
                if not hmac.compare_digest(first.encode("latin-1"), token.encode("ascii")):
                    self.send_error(404, "File not found")
                    return
                if not slash:
                    self.send_response(301)
                    self.send_header("Location", f"{base}/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                path = "/" + rest

            if path == "/":
                query = parse_qs(url.query).get("q", [""])[0].strip()
                self._send_index(query)
            elif path.startswith(CONVERT_PREFIX):
                self._send_converted(unquote(path[len(CONVERT_PREFIX):]))
            else:
                self._send_mobi(unquote(path.lstrip("/")))

        def log_message(self, fmt, *args):
            message = (fmt % args).translate(CONTROL_CHARS)
            sys.stderr.write("%s - %s\n" % (self.address_string(), message))

    return MobiHandler


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Same as ThreadingHTTPServer, but doesn't dump a full traceback to
    the console when a client drops the connection mid-request (the
    per-request try/except above catches most of these, but this is a
    safety net for anything that slips through, e.g. in the library's
    own post-request socket flush)."""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, DROPPED):
            sys.stderr.write(f"{client_address[0]} - connection dropped ({exc.__class__.__name__})\n")
        else:
            super().handle_error(request, client_address)


def default_cache_dir():
    xdg = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(xdg, "simple-mobi-ebook-server")


def main():
    parser = argparse.ArgumentParser(
        description="Simple Mobi Ebook Server - serve .mobi files over HTTP "
                     "(e.g. for downloading onto a Kindle 4's experimental browser)."
    )
    parser.add_argument(
        "-f", "--folder", default=".",
        help="Folder containing .mobi/.epub files (default: current directory)",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=8000,
        help="Port to listen on (default: 8000)",
    )
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="Address to bind to (default: 0.0.0.0, i.e. whole LAN)",
    )
    parser.add_argument(
        "--token",
        help="Secret URL path prefix that guards the whole server - letters, "
             "digits, '-' and '_', at least 8 characters (default: a random "
             "one, different on every start)",
    )
    parser.add_argument(
        "--no-token", action="store_true",
        help="Serve without a token, at http://host:port/ - for a trusted LAN only",
    )
    parser.add_argument(
        "--kindlegen",
        help="Path to Amazon's kindlegen, used to convert .epub to .mobi on "
             "demand (default: 'kindlegen' from PATH; without it .epub files "
             "aren't listed)",
    )
    parser.add_argument(
        "--cache-dir", default=default_cache_dir(),
        help="Where converted books are kept (default: %(default)s)",
    )
    args = parser.parse_args()

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        print(f"Error: folder does not exist: {folder}", file=sys.stderr)
        sys.exit(1)

    if args.no_token:
        token = None
    elif args.token:
        if not TOKEN_RE.match(args.token):
            print("Error: --token must be at least 8 characters of letters, "
                  "digits, '-' and '_'", file=sys.stderr)
            sys.exit(1)
        token = args.token
    else:
        token = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))

    kindlegen = os.path.expanduser(args.kindlegen) if args.kindlegen else shutil.which("kindlegen")
    converter = None
    if kindlegen:
        if not (os.path.isfile(kindlegen) and os.access(kindlegen, os.X_OK)):
            print(f"Error: kindlegen not found or not executable: {kindlegen}", file=sys.stderr)
            sys.exit(1)
        cache_dir = os.path.abspath(os.path.expanduser(args.cache_dir))
        os.makedirs(cache_dir, exist_ok=True)
        converter = EpubConverter(kindlegen, cache_dir)

    handler = make_handler(folder, token, converter)
    server = QuietThreadingHTTPServer((args.host, args.port), handler)

    ip = get_local_ip()
    print(f"Folder:  {folder}")
    if converter:
        print(f"EPUB:    converted on demand with {kindlegen}")
        print(f"Cache:   {converter.cache_dir}")
    else:
        print("EPUB:    not served (no kindlegen - see --kindlegen)")
    print(f"Server:  http://{ip}:{args.port}/{token + '/' if token else ''}")
    if token and not args.token:
        print(f"         (random token - start with --token {token} to keep this address)")
    print("On the Kindle: type the address above into Basic Web Browser (Experimental).")
    print("Stop with Ctrl+C")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
