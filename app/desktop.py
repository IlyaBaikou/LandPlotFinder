from __future__ import annotations

import json
import logging
import os
import platform
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Mapping, Optional

import uvicorn

from app.config import load_settings
from app.web import create_app

APP_NAME = "LandPlotFinder"
DEFAULT_PORT = 8787
PORT_FILE = "desktop.json"
LOGGER = logging.getLogger(__name__)


def app_data_dir(
    system: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> Path:
    system = system or platform.system()
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    if system == "Windows":
        root = Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    elif system == "Darwin":
        root = home / "Library" / "Application Support"
    else:
        root = Path(environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return root / APP_NAME


def sqlite_database_url(path: Path) -> str:
    return f"sqlite:///{path.expanduser().resolve().as_posix()}"


def find_available_port(start: int = DEFAULT_PORT, attempts: int = 20) -> int:
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                candidate.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("Не удалось найти свободный локальный порт")


def is_landplotfinder_running(port: int, timeout: float = 0.6) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health",
            timeout=timeout,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return bool(payload.get("ok") and payload.get("service") == "land-plot-finder")
    except (OSError, ValueError, urllib.error.URLError):
        return False


def configure_desktop_environment(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["DATABASE_URL"] = sqlite_database_url(data_dir / "landplots.db")
    os.environ["WEB_HOST"] = "127.0.0.1"
    os.environ["LANDPLOTFINDER_DESKTOP"] = "1"
    os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")
    os.environ.pop("GOOGLE_SPREADSHEET_ID", None)
    os.environ.pop("GOOGLE_SERVICE_ACCOUNT_JSON", None)


def _configure_logging(data_dir: Path) -> None:
    handler = RotatingFileHandler(
        data_dir / "landplotfinder.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _stored_port(data_dir: Path) -> Optional[int]:
    try:
        value = json.loads((data_dir / PORT_FILE).read_text(encoding="utf-8"))
        port = int(value["port"])
        return port if 1 <= port <= 65535 else None
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _write_port(data_dir: Path, port: int) -> None:
    (data_dir / PORT_FILE).write_text(
        json.dumps({"port": port}),
        encoding="utf-8",
    )


class DesktopApplication:
    def __init__(self, data_dir: Path, port: int) -> None:
        import tkinter as tk

        self.tk = tk
        self.data_dir = data_dir
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.server: Optional[uvicorn.Server] = None
        self.server_thread: Optional[threading.Thread] = None
        self.closing = False

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("460x280")
        self.root.minsize(420, 260)
        self.root.configure(background="#f7f3e8")
        self.root.protocol("WM_DELETE_WINDOW", self.stop)
        self.status = tk.StringVar(value="Запускаем локальный сервис…")
        self._build_window()

    def _build_window(self) -> None:
        tk = self.tk
        title = tk.Label(
            self.root,
            text="LandPlotFinder",
            font=("Arial", 24, "bold"),
            foreground="#26362c",
            background="#f7f3e8",
        )
        title.pack(pady=(28, 8))
        subtitle = tk.Label(
            self.root,
            text="Поиск участков работает на этом компьютере",
            font=("Arial", 12),
            foreground="#58635b",
            background="#f7f3e8",
        )
        subtitle.pack(pady=(0, 18))
        status = tk.Label(
            self.root,
            textvariable=self.status,
            font=("Arial", 12),
            foreground="#58635b",
            background="#f7f3e8",
        )
        status.pack(pady=(0, 18))
        buttons = tk.Frame(self.root, background="#f7f3e8")
        buttons.pack()
        self.open_button = tk.Button(
            buttons,
            text="Открыть LandPlotFinder",
            command=self.open_browser,
            state=tk.DISABLED,
            font=("Arial", 12, "bold"),
            foreground="white",
            background="#48604f",
            activebackground="#34483b",
            activeforeground="white",
            padx=18,
            pady=10,
            relief=tk.FLAT,
        )
        self.open_button.pack(side=tk.LEFT, padx=6)
        stop_button = tk.Button(
            buttons,
            text="Остановить",
            command=self.stop,
            font=("Arial", 12),
            foreground="#26362c",
            background="#e9dfc7",
            activebackground="#d8c9a8",
            padx=14,
            pady=10,
            relief=tk.FLAT,
        )
        stop_button.pack(side=tk.LEFT, padx=6)

    def run(self) -> None:
        signal.signal(signal.SIGINT, self._request_stop)
        signal.signal(signal.SIGTERM, self._request_stop)
        settings = load_settings()
        application = create_app(settings)
        config = uvicorn.Config(
            application,
            host="127.0.0.1",
            port=self.port,
            log_level="info",
            access_log=False,
            loop="asyncio",
            http="h11",
            ws="none",
            log_config=None,
        )
        self.server = uvicorn.Server(config)
        self.server_thread = threading.Thread(
            target=self.server.run,
            name="landplotfinder-server",
            daemon=True,
        )
        self.server_thread.start()
        threading.Thread(target=self._wait_until_ready, daemon=True).start()
        self.root.mainloop()

    def _request_stop(self, *_: object) -> None:
        self.root.after(0, self.stop)

    def _wait_until_ready(self) -> None:
        for _ in range(120):
            if self.closing:
                return
            if is_landplotfinder_running(self.port):
                self.root.after(0, self._mark_ready)
                return
            if self.server_thread and not self.server_thread.is_alive():
                break
            time.sleep(0.25)
        self.root.after(0, self._mark_failed)

    def _mark_ready(self) -> None:
        if self.closing:
            return
        _write_port(self.data_dir, self.port)
        self.status.set("Готово. Окно управления можно свернуть.")
        self.open_button.configure(state=self.tk.NORMAL)
        self.open_browser()

    def _mark_failed(self) -> None:
        if self.closing:
            return
        self.status.set("Не удалось запустить. Подробности записаны в журнал.")

    def open_browser(self) -> None:
        webbrowser.open(self.url)

    def stop(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.status.set("Останавливаем…")
        self.root.update_idletasks()
        if self.server is not None:
            self.server.should_exit = True
        if self.server_thread is not None:
            self.server_thread.join(timeout=8)
        port_file = self.data_dir / PORT_FILE
        if _stored_port(self.data_dir) == self.port:
            port_file.unlink(missing_ok=True)
        self.root.destroy()


def main() -> int:
    configured_data_dir = os.getenv("LANDPLOTFINDER_DATA_DIR")
    data_dir = (
        Path(configured_data_dir).expanduser().resolve()
        if configured_data_dir
        else app_data_dir()
    )
    configure_desktop_environment(data_dir)
    _configure_logging(data_dir)

    existing_port = _stored_port(data_dir)
    if existing_port and is_landplotfinder_running(existing_port):
        webbrowser.open(f"http://127.0.0.1:{existing_port}")
        return 0

    try:
        port = find_available_port()
        DesktopApplication(data_dir, port).run()
        return 0
    except Exception:
        LOGGER.exception("Desktop application failed")
        try:
            from tkinter import messagebox

            messagebox.showerror(
                APP_NAME,
                "Не удалось запустить LandPlotFinder.\n"
                f"Журнал: {data_dir / 'landplotfinder.log'}",
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
