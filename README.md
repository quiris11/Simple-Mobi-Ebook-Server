# Simple Mobi Ebook Server

A tiny, dependency-free HTTP server that serves `.mobi` files from a folder
(recursively, including all subfolders) so you can download them straight
onto an old Kindle — or any device with a plain web browser — without
needing USB, Calibre, or a wireless Send-to-Kindle account.

It was built specifically to work around the **Kindle 4**'s lack of
wireless book transfer, using its `Experimental > Basic Web Browser`.

## Features

- **Zero dependencies** — standard library only, just Python 3.
- **Recursive folder scan** — organize your books in subfolders however you like.
- **Search box** — filters the list by title/path, ignoring case and accents
  (`zolkiewski` finds `Żółkiewski`), with no JavaScript needed.
- **EPUB → MOBI on demand** — with Amazon's `kindlegen` available, `.epub`
  files are listed too and converted the first time you pick one; the result
  is cached, so later downloads are instant.
- **Secret token in the URL** — everything is served under
  `http://host:port/<token>/`, so you can expose the server to the internet
  (e.g. for a Kindle on your phone's hotspot) without strangers browsing it.
- **Resumable downloads** — supports HTTP `Range` requests (`206 Partial Content`),
  which old download managers (including the Kindle's) rely on.
- **Robust to flaky connections** — a dropped/reset connection is logged
  as a single line instead of crashing the server or spamming a traceback.
- **Minimal, e-ink-friendly HTML** — no CSS/JS, so it renders fast on slow,
  old browsers.
- **Basic path-traversal protection** — only `.mobi`/`.epub` files under the
  chosen folder can be served.

## Requirements

- Python 3.8 or newer. Nothing else — no `pip install` needed.
- Optional: Amazon's `kindlegen` (Linux build 2.9 works fine on 64-bit
  systems) for serving `.epub` files.

## Usage

```bash
python3 simple_mobi_ebook_server.py -f /path/to/books -p 8000
python3 simple_mobi_ebook_server.py -f /path/to/books --token k7x2m9qp4wza \
    --kindlegen ~/kindlegen/kindlegen
```

| Option | Default | Description |
|---|---|---|
| `-f`, `--folder` | current directory | Folder to search for `.mobi` files (recursively) |
| `-p`, `--port` | `8000` | Port to listen on |
| `--host` | `0.0.0.0` | Address to bind to (`0.0.0.0` = whole LAN) |
| `--token` | random | Secret URL prefix (letters, digits, `-`, `_`, min. 8 chars). Without it a new random one is generated on every start |
| `--no-token` | off | Serve at `http://host:port/` with no token — trusted LAN only |
| `--kindlegen` | `kindlegen` from `PATH` | Path to kindlegen; without it `.epub` files aren't listed |
| `--cache-dir` | `~/.cache/simple-mobi-ebook-server` | Where converted books are kept |

The script prints the URL to use, e.g.:

```
Folder:  /home/you/books
EPUB:    converted on demand with /home/you/kindlegen/kindlegen
Cache:   /home/you/.cache/simple-mobi-ebook-server
Server:  http://192.168.1.23:8000/k7x2m9qp4wza/
```

Pass a fixed `--token` so the address stays the same between restarts,
and bookmark it on the Kindle — that way you only type it once.

## Downloading a book onto a Kindle 4

1. Connect the Kindle to the same Wi-Fi network as the computer running the server.
2. Open **Menu → Experimental → Basic Web Browser** (naming may vary slightly by firmware version).
3. Type in the address the script printed, e.g. `http://192.168.1.23:8000/`.
4. Tap a book title. For an `.epub` you'll first see a "Converting…" page
   that reloads itself every few seconds; the download starts on its own
   once the book is ready. The Kindle should download it and offer to open/save it.
   If it doesn't show up on the Home screen right away, try
   **Home → Menu → "Sync and Check for Items"**, or restart the device.

### A quirk you'll probably see in the logs

```
... connection dropped while sending Some Book.mobi (ConnectionResetError)
... "GET /Some Book.mobi HTTP/1.1" 200 -
```

This is normal on the Kindle 4 and not a bug. Its browser makes an initial
request to sniff the content type; once it sees this isn't HTML, it hands
the URL off to its internal download manager and drops its own "preview"
connection (the reset you see in the log). The download manager then opens
a fresh connection and completes the transfer — which is the request you
see right after. The file downloads correctly either way.

## Using it away from home (phone hotspot)

Connect the Kindle to your phone's hotspot and make the server reachable
from the internet: forward the port on your home router to the computer
running the server, or run it on a VPS. If your home connection has no
public IP (CGNAT), use a tunnel or a VPS instead. Keep the token on; it's
what keeps the server private.

## Security notes

- Access control is the **secret token in the URL**. Requests without it
  get a plain `404`, so the server looks empty to scanners. The token is
  compared in constant time; a random 12-character one is ~62 bits, far
  beyond guessing.
- It's **plain HTTP**: the Kindle 4's browser can't do modern HTTPS. The
  token travels unencrypted and can be seen by anyone who can watch the
  traffic (your mobile carrier, or someone on an open Wi-Fi). Over your own
  hotspot that's a reasonable trade-off for a personal library. If the
  token leaks, just start the server with a new one.
- The token also guards the EPUB converter, so strangers can't make your
  machine run kindlegen.
- The server only serves `.mobi` (and convertible `.epub`) files that live
  under the folder you point it at; it will refuse `../` traversal attempts.

## Trademark notice

"Kindle" and "Amazon" are trademarks of Amazon.com, Inc. or its affiliates.
This project is not affiliated with, sponsored by, or endorsed by Amazon;
it's an independent tool that happens to be useful for Kindle owners.

## License

This is free and unencumbered software released into the public domain
under [The Unlicense](https://unlicense.org). See `LICENSE` for the full text.