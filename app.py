"""
Et al. — local PDF library manager.
Entry point: starts the FastAPI server in a thread and opens the native window.
"""
import threading
import time
import socket
import webview
import uvicorn

from server import app, set_window_ref

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
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


class JSAPI:
    """Exposed to JS via window.pywebview.api — used for native folder picker."""

    def pick_folder(self) -> str | None:
        win = webview.windows[0]
        result = win.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        return result[0]


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
    webview.start()


if __name__ == "__main__":
    main()
