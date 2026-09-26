#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import sys
import threading

import webview
from webview.dom import DOMEventHandler

import besota_core as core


def resource_path(relative_path: str) -> str:
    """Resolve a path both in dev mode and inside a PyInstaller onefile exe."""
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


HTML_PATH = resource_path(os.path.join("web", "index.html"))


def js_string(value: str) -> str:
    """Safely encode a Python string as a JS string literal."""
    return json.dumps(value)

current_window: webview.Window | None = None

class Api:
    """Exposed to JavaScript as `pywebview.api`."""

    def __init__(self):
        self.sock = None
        self.abort_event = None
        self.firmware_path = None
        self._window = None

    def _bind_window(self, window: webview.Window) -> None:
        self._window = window

    def _eval(self, js: str) -> None:
        win = self._window or webview.active_window()
        if win is not None:
            try:
                win.evaluate_js(js)
            except Exception:
                pass  # window may already be closing

    # ---------------------------------------------------------- logging

    def log(self, message: str, cls: str = "l-info") -> None:
        self._eval(f"uiLog({js_string(message)}, {js_string(cls)})")

    def _progress_cb(self, offset: int, total: int, speed: float, eta: float) -> None:
        self._eval(f"setProgress({offset}, {total}, {speed}, {eta})")

    # ------------------------------------------------------------- files

    def pick_file(self):
        """Open a native file dialog and remember the selected firmware path."""
        win = webview.active_window()
        if win is None:
            return None
            
        result = win.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=("Firmware binary (*.bin)", "All files (*.*)"),
        )
        if not result:
            return None
            
        path = result[0]
        self.firmware_path = path
        return {
            "path": path,
            "name": os.path.basename(path),
            "size": os.path.getsize(path),
        }

    def set_firmware_by_path(self, path: str):
        if not path or not os.path.exists(path):
            return
        self.firmware_path = path
        info = {
            "path": path,
            "name": os.path.basename(path),
            "size": os.path.getsize(path),
        }
        self._eval(f"onFirmwareSelected({json.dumps(info)})")

    # -------------------------------------------------------------- scan

    def scan_devices(self, name: str, duration) -> bool:
        threading.Thread(
            target=self._scan_worker, args=(name, int(duration)), daemon=True
        ).start()
        return True

    def _scan_worker(self, name: str, duration: int) -> None:
        try:
            results = core.scan_devices_by_name(name, duration)
            payload = [{"address": addr, "name": dev_name} for addr, dev_name in results]
            self._eval(f"onScanResults({json.dumps(payload)})")
        except Exception as e:
            self._eval(f"onScanFailed({js_string(str(e))})")

    # -------------------------------------------------------- connection

    def connect(self, address: str) -> bool:
        threading.Thread(target=self._connect_worker, args=(address,), daemon=True).start()
        return True

    def _connect_worker(self, address: str) -> None:
        try:
            self.sock = core.connect_device(address)
            self._eval(f"onConnected({js_string(address)})")
        except Exception as e:
            self._eval(f"onConnectFailed({js_string(str(e))})")

    def disconnect(self) -> bool:
        core.disconnect_device(self.sock)
        self.sock = None
        return True

    # -------------------------------------------------------------- flash

    def start_flash(self, protocol_key: str, ota_addr_hex: str) -> bool:
        threading.Thread(
            target=self._flash_worker, args=(protocol_key, ota_addr_hex), daemon=True
        ).start()
        return True

    def _confirm_header_patch(self, question: str) -> bool:
        # No blocking modal dialog in the web UI - log it and auto-confirm.
        self.log(question, "l-info")
        self.log("Header will be patched automatically.", "l-info")
        return True

    def _flash_worker(self, protocol_key: str, ota_addr_hex: str) -> None:
        try:
            if not self.firmware_path:
                raise core.FirmwareError("No firmware file selected.")
            if self.sock is None:
                raise core.FlashError("Not connected to a device.")

            protocol = core.PROTOCOLS.get(protocol_key)
            if protocol is None:
                raise core.FlashError(f"Unknown protocol: {protocol_key}")

            ota_addr = core.parse_hex_address(ota_addr_hex)
            self.abort_event = threading.Event()

            firmware = core.prepare_firmware(
                self.firmware_path,
                ota_addr,
                confirm=self._confirm_header_patch,
                log=self.log,
            )
            core.run_flash(
                self.sock,
                firmware,
                protocol,
                ota_addr,
                log=self.log,
                progress=self._progress_cb,
                abort_event=self.abort_event,
            )
            self._eval("onFlashDone(null)")
        except (core.FlashAborted, core.FlashError) as e:
            self._eval(f"onFlashDone({js_string(str(e))})")
        except Exception as e:
            self._eval(f"onFlashDone({js_string('Unexpected error: ' + str(e))})")
        finally:
            core.disconnect_device(self.sock)
            self.sock = None

    def abort(self) -> bool:
        if self.abort_event is not None:
            self.abort_event.set()
        return True


def main() -> None:
    if sys.platform == "win32":
        import ctypes
        app_id = "nnonick.besota.flasher"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)

    api = Api()
    icon_file = resource_path("icon.ico")

    window = webview.create_window(
        "besota GUI",
        HTML_PATH,
        js_api=api,
        width=820,
        height=1000,
        min_size=(700, 800),
        background_color="#000000",
    )

    def setup_drag_and_drop():
        def on_drop(e):
            files = e.get("dataTransfer", {}).get("files", [])
            if not files:
                return
            file_path = files[0].get("pywebviewFullPath")
            if file_path:
                api.set_firmware_by_path(file_path)

        body = window.dom.get_element("body")
        if body:
            body.events.drop += DOMEventHandler(
                on_drop, prevent_default=True, stop_propagation=False
            )

    window.events.loaded += setup_drag_and_drop

    if os.path.exists(icon_file):
        webview.start(icon=icon_file)
    else:
        webview.start()


if __name__ == "__main__":
    main()