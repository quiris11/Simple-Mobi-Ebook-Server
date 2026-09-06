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
- **Resumable downloads** — supports HTTP `Range` requests (`206 Partial Content`),
  which old download managers (including the Kindle's) rely on.
- **Robust to flaky connections** — a dropped/reset connection is logged
  as a single line instead of crashing the server or spamming a traceback.
- **Minimal, e-ink-friendly HTML** — no CSS/JS, so it renders fast on slow,
  old browsers.
- **Basic path-traversal protection** — only files under the chosen folder,
  ending in `.mobi`, can be served.

## Requirements

- Python 3.8 or newer. Nothing else — no `pip install` needed.

## Usage

```bash
python3 simple_mobi_ebook_server.py -f /path/to/books -p 8000
```

| Option | Default | Description |
|---|---|---|
| `-f`, `--folder` | current directory | Folder to search for `.mobi` files (recursively) |
| `-p`, `--port` | `8000` | Port to listen on |
| `--host` | `0.0.0.0` | Address to bind to (`0.0.0.0` = whole LAN) |

The script prints the URL to use, e.g.:

```
Folder:  /home/you/books
Server:  http://192.168.1.23:8000/
```

## Downloading a book onto a Kindle 4

1. Connect the Kindle to the same Wi-Fi network as the computer running the server.
2. Open **Menu → Experimental → Basic Web Browser** (naming may vary slightly by firmware version).
3. Type in the address the script printed, e.g. `http://192.168.1.23:8000/`.
4. Tap a book title. The Kindle should download it and offer to open/save it.
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

## Security notes

- There is **no authentication**. Anyone on the same network as the server
  can browse and download the files. Fine for a home LAN, not something to
  expose to the open internet as-is.
- The server only serves files ending in `.mobi` that live under the folder
  you point it at; it will refuse `../` traversal attempts.

## Trademark notice

"Kindle" and "Amazon" are trademarks of Amazon.com, Inc. or its affiliates.
This project is not affiliated with, sponsored by, or endorsed by Amazon;
it's an independent tool that happens to be useful for Kindle owners.

## License

Not yet chosen — pick one that fits (e.g. MIT or Apache-2.0) before
publishing, and I can generate the `LICENSE` file for you if you tell me
which.
