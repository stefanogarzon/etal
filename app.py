"""
Et al. — local PDF library manager.
Entry point: starts the FastAPI server in a thread and opens the native window.
"""
import os
import socket
import subprocess
import sys
import threading
import time

import uvicorn
import webview

from server import app, db, library_root, set_window_ref

HOST = "127.0.0.1"
PORT = 8765


def wait_for_port(host: str, port: int, timeout: float = 5.0) -> None:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"Server did not start on {host}:{port}")


def run_server() -> None:
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


class JSAPI:
    """Exposed to JS via window.pywebview.api."""

    def pick_folder(self) -> str | None:
        win = webview.windows[0]
        result = win.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        return result[0]

    def open_external(self, article_id: int) -> bool:
        """Open an article's PDF in the OS default viewer."""
        with db() as conn:
            row = conn.execute(
                "SELECT topic, filename FROM articles WHERE id = ?", (article_id,)
            ).fetchone()
        if not row:
            return False
        path = library_root() / row["topic"] / row["filename"]
        if not path.exists():
            return False
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        elif sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True

    def share_pdf(self, article_id: int) -> bool:
        """Open the native macOS share sheet (Mail / Messages / AirDrop /
        installed share extensions) for an article's PDF."""
        with db() as conn:
            row = conn.execute(
                "SELECT topic, filename FROM articles WHERE id = ?", (article_id,)
            ).fetchone()
        if not row:
            return False
        path = library_root() / row["topic"] / row["filename"]
        if not path.exists():
            return False

        if sys.platform == "darwin":
            try:
                from Foundation import NSURL, NSOperationQueue
                from AppKit import (
                    NSApp, NSSharingServicePicker, NSMakeRect, NSMinYEdge,
                )
                url = NSURL.fileURLWithPath_(str(path))

                def show_sheet() -> None:
                    picker = NSSharingServicePicker.alloc().initWithItems_([url])
                    window = NSApp.keyWindow() or NSApp.mainWindow()
                    if not window:
                        return
                    view = window.contentView()
                    bounds = view.bounds()
                    rect = NSMakeRect(
                        bounds.size.width - 80, bounds.size.height - 40, 1, 1,
                    )
                    picker.showRelativeToRect_ofView_preferredEdge_(
                        rect, view, NSMinYEdge,
                    )

                NSOperationQueue.mainQueue().addOperationWithBlock_(show_sheet)
                return True
            except Exception:
                # Last-resort fallback: open Mail with the PDF attached
                subprocess.Popen(["open", "-a", "Mail", str(path)])
                return True
        elif sys.platform == "win32":
            # No native share sheet — reveal the file so user can attach
            subprocess.Popen(["explorer", "/select,", str(path)])
            return True
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
            return True


def main() -> None:
    threading.Thread(target=run_server, daemon=True).start()
    wait_for_port(HOST, PORT)

    api = JSAPI()
    window = webview.create_window(
        title="Et al.",
        url=f"http://{HOST}:{PORT}/",
        js_api=api,
        width=1400,
        height=900,
        min_size=(1000, 700),
    )
    set_window_ref(window)
    webview.start(debug=True)


if __name__ == "__main__":
    main()
