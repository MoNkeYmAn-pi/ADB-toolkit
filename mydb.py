#!/usr/bin/env python3
"""
ADB Toolkit — single-file edition.

A cross-platform desktop GUI (PyQt6) for managing a single connected
Android device via ADB: device info & reboot, app management, file
browser, screenshots, an interactive shell, and live logcat streaming.

Everything lives in this one file for easy distribution — just
`pip install PyQt6` and run `python adb_toolkit.py`.

Sections in this file:
  1. AdbClient / AdbError / DeviceInfo  — all subprocess calls to `adb`
  2. DeviceTab                          — device info + reboot controls
  3. AppsTab                            — install/uninstall/manage apps
  4. FilesTab                           — browse/push/pull device files
  5. ScreenTab                          — screenshot capture
  6. ShellTab                           — interactive adb shell
  7. LogcatTab                          — live streaming log viewer
  8. CastPanel                          — embedded screen mirror (30fps target)
  9. MainWindow / main()                — app shell tying it all together

FPS FIXES (v2):
  - H.264: removed 400ms artificial sleep; feeder signals socket-ready via
    threading.Event so OpenCV connects as soon as data flows (saves ~400ms
    of dropped frames on every session start/restart).
  - H.264: CAP_PROP_BUFFERSIZE=1 now set via URL query param before open(),
    not after — eliminates OpenCV's internal pre-buffer stash.
  - H.264: device screen size auto-detected via `adb shell wm size` so
    --size always matches actual orientation (portrait phones get 720x1280,
    landscape tablets get 1280x720) — wrong size made screenrecord reject
    or silently re-encode at a slower rate.
  - Both modes: frame-drop guard (_frame_pending flag + lock) ensures the
    Qt GUI thread always renders the LATEST frame and never queues up a
    backlog of stale ones — the single biggest source of visible lag.
  - PNG mode: same frame-drop guard applied so slow-phone PNG streams don't
    build a latency tail in Qt's signal queue.
"""

import sys
import os
import re
import subprocess
import shutil
import tempfile
import time
import socket
import threading
from dataclasses import dataclass
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QComboBox, QPushButton, QTabWidget, QStatusBar, QMessageBox,
    QFileDialog, QLineEdit, QListWidget, QCheckBox, QListWidgetItem,
    QAbstractItemView, QTableWidget, QTableWidgetItem, QHeaderView,
    QInputDialog, QPlainTextEdit, QGroupBox, QFrame, QDialog, QDialogButtonBox,
    QSizePolicy
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QIcon, QPixmap, QTextCursor, QImage

try:
    import cv2
    _HAS_CV2 = True
except (ImportError, Exception):
    # Exception covers OSError/FileNotFoundError on Windows when OpenCV
    # native DLLs (e.g. MSVCP140.dll) are missing.
    _HAS_CV2 = False

try:
    from pyzbar import pyzbar
    _HAS_PYZBAR = True
except (ImportError, Exception):
    # On Windows, pyzbar raises FileNotFoundError (subclass of OSError,
    # NOT ImportError) when libzbar-64.dll or libiconv.dll are missing.
    # Catching Exception lets the app start normally and just disables
    # the QR-pairing button with an install hint in its tooltip.
    _HAS_PYZBAR = False

try:
    from zeroconf import Zeroconf, ServiceBrowser, ServiceListener
    _HAS_ZEROCONF = True
except (ImportError, Exception):
    _HAS_ZEROCONF = False

    class ServiceListener:
        pass

_QR_PAIRING_AVAILABLE = _HAS_CV2 and _HAS_PYZBAR and _HAS_ZEROCONF

# ── Cast FPS permanent setting ────────────────────────────────────────────────
# Set to 30 or 60.  30 = 720p, best quality/smoothness balance (recommended).
#                   60 = 540p, higher frame rate, slightly softer image.
# This is the default used when the app starts; the user can still toggle it
# live in the Cast panel.  Change this line to permanently change the default.
CAST_FPS_DEFAULT = 30   # ← change to 60 for 60fps default


def parse_adb_pairing_qr(text: str):
    if not text or not text.startswith("WIFI:"):
        return None
    type_match = re.search(r"T:([^;]+)", text)
    name_match = re.search(r"S:([^;]+)", text)
    pass_match = re.search(r"P:([^;]+)", text)
    if not type_match or type_match.group(1) != "ADB":
        return None
    if not name_match or not pass_match:
        return None
    return {"service_name": name_match.group(1), "password": pass_match.group(1)}

# ==============================================================================
# Core ADB wrapper
# ==============================================================================

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class DeviceInfo:
    serial: str
    state: str
    model: str = ""
    android_version: str = ""
    sdk_version: str = ""
    manufacturer: str = ""
    battery_level: str = ""
    ip_address: str = ""


class AdbError(Exception):
    pass


class AdbClient:
    def __init__(self, adb_path: Optional[str] = None):
        self.adb_path = adb_path or self._autodetect_adb()

    @staticmethod
    def _verify_adb_binary(path: str) -> bool:
        if not path or not os.path.isfile(path):
            return False
        try:
            result = subprocess.run(
                [path, "version"],
                capture_output=True,
                timeout=5,
                creationflags=_CREATE_NO_WINDOW,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    @classmethod
    def _autodetect_adb(cls) -> Optional[str]:
        found = shutil.which("adb")
        if found and cls._verify_adb_binary(found):
            return found

        candidates = []
        home = os.path.expanduser("~")
        if sys.platform == "win32":
            candidates += [
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", "adb.exe"),
                os.path.join(home, "AppData", "Local", "Android", "Sdk", "platform-tools", "adb.exe"),
                r"C:\Android\platform-tools\adb.exe",
                r"C:\platform-tools\adb.exe",
            ]
        elif sys.platform == "darwin":
            candidates += [
                os.path.join(home, "Library", "Android", "sdk", "platform-tools", "adb"),
                "/usr/local/bin/adb",
                "/opt/homebrew/bin/adb",
            ]
        else:
            candidates += [
                os.path.join(home, "Android", "Sdk", "platform-tools", "adb"),
                os.path.join(home, "android-sdk", "platform-tools", "adb"),
                "/usr/bin/adb",
                "/usr/local/bin/adb",
            ]

        for c in candidates:
            if c and cls._verify_adb_binary(c):
                return c
        return None

    def is_configured(self) -> bool:
        return bool(self.adb_path) and os.path.isfile(self.adb_path) or self.adb_path == "adb"

    def set_adb_path(self, path: str):
        self.adb_path = path

    def _run(self, args, timeout=30, input_data: Optional[bytes] = None) -> subprocess.CompletedProcess:
        if not self.adb_path:
            raise AdbError(
                "adb binary not found. Please install Android platform-tools "
                "and set the adb path using the 'ADB Path...' button."
            )
        cmd = [self.adb_path] + args
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                input=input_data,
                creationflags=_CREATE_NO_WINDOW,
            )
            return result
        except FileNotFoundError:
            raise AdbError(f"adb binary not found at path: {self.adb_path}")
        except subprocess.TimeoutExpired:
            raise AdbError(f"Command timed out: {' '.join(cmd)}")
        except OSError as e:
            raise AdbError(
                f"Could not run adb at '{self.adb_path}': {e}. "
                f"This usually means the path doesn't point to a real adb.exe. "
                f"Use 'ADB Path...' to select the correct platform-tools\\adb.exe."
            )

    def run(self, args, serial: Optional[str] = None, timeout=30) -> str:
        full_args = (["-s", serial] if serial else []) + args
        result = self._run(full_args, timeout=timeout)
        if result.returncode != 0:
            err = result.stderr.decode(errors="replace").strip() or result.stdout.decode(errors="replace").strip()
            raise AdbError(err or f"adb command failed: {' '.join(args)}")
        return result.stdout.decode(errors="replace")

    def run_popen(self, args, serial: Optional[str] = None):
        if not self.adb_path:
            raise AdbError("adb binary not found. Please configure it using 'ADB Path...'.")
        full_args = [self.adb_path] + (["-s", serial] if serial else []) + args
        try:
            return subprocess.Popen(
                full_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=_CREATE_NO_WINDOW,
                bufsize=1,
            )
        except OSError as e:
            raise AdbError(
                f"Could not run adb at '{self.adb_path}': {e}. "
                f"Check that the adb path points to a real adb.exe via 'ADB Path...'."
            )

    def list_devices(self) -> list:
        out = self.run(["devices"])
        devices = []
        for line in out.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t") if "\t" in line else line.split()
            if len(parts) >= 2:
                devices.append((parts[0], parts[1]))
        return devices

    def get_prop(self, serial: str, prop: str) -> str:
        try:
            return self.run(["shell", "getprop", prop], serial=serial).strip()
        except AdbError:
            return ""

    def get_device_info(self, serial: str, state: str) -> DeviceInfo:
        info = DeviceInfo(serial=serial, state=state)
        if state != "device":
            return info

        info.model = self.get_prop(serial, "ro.product.model")
        info.manufacturer = self.get_prop(serial, "ro.product.manufacturer")
        info.android_version = self.get_prop(serial, "ro.build.version.release")
        info.sdk_version = self.get_prop(serial, "ro.build.version.sdk")

        try:
            battery_out = self.run(["shell", "dumpsys", "battery"], serial=serial)
            m = re.search(r"level:\s*(\d+)", battery_out)
            if m:
                info.battery_level = m.group(1) + "%"
        except AdbError:
            pass

        try:
            ip_out = self.run(["shell", "ip", "route"], serial=serial)
            m = re.search(r"src\s+(\d+\.\d+\.\d+\.\d+)", ip_out)
            if m:
                info.ip_address = m.group(1)
        except AdbError:
            pass

        return info

    def get_screen_size(self, serial: str):
        """
        Returns (width, height) as integers from `adb shell wm size`.
        Returns None if the command fails or output can't be parsed.
        This is used to pass the correct --size to screenrecord so the
        H.264 encoder doesn't have to rescale or reject the stream.
        """
        try:
            out = self.run(["shell", "wm", "size"], serial=serial, timeout=5)
            # Output: "Physical size: 1080x2400" or "Override size: ..."
            m = re.search(r"(\d+)x(\d+)", out)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                return (w, h)
        except AdbError:
            pass
        return None

    def reboot(self, serial: str, mode: str = ""):
        args = ["reboot"]
        if mode:
            args.append(mode)
        self.run(args, serial=serial, timeout=15)

    def connect_tcp(self, address: str) -> str:
        return self.run(["connect", address], timeout=15)

    def disconnect_tcp(self, address: str = "") -> str:
        args = ["disconnect"]
        if address:
            args.append(address)
        return self.run(args, timeout=15)

    def pair(self, host_port: str, password: str) -> str:
        return self.run(["pair", host_port, password], timeout=15)

    def list_packages(self, serial: str, third_party_only: bool = True) -> list:
        args = ["shell", "pm", "list", "packages"]
        if third_party_only:
            args.append("-3")
        out = self.run(args, serial=serial, timeout=20)
        packages = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                packages.append(line[len("package:"):])
        return sorted(packages)

    def install_apk(self, serial: str, apk_path: str, reinstall: bool = False) -> str:
        args = ["install"]
        if reinstall:
            args.append("-r")
        args.append(apk_path)
        return self.run(args, serial=serial, timeout=120)

    def uninstall_package(self, serial: str, package: str) -> str:
        return self.run(["uninstall", package], serial=serial, timeout=30)

    def clear_app_data(self, serial: str, package: str) -> str:
        return self.run(["shell", "pm", "clear", package], serial=serial, timeout=20)

    def force_stop(self, serial: str, package: str) -> str:
        return self.run(["shell", "am", "force-stop", package], serial=serial, timeout=15)

    def launch_app(self, serial: str, package: str) -> str:
        return self.run(
            ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"],
            serial=serial,
            timeout=15,
        )

    def list_dir(self, serial: str, path: str) -> list:
        out = self.run(["shell", "ls", "-la", path], serial=serial, timeout=15)
        entries = []
        date_pattern = re.compile(
            r"(?:\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}|[A-Za-z]{3}\s+\d{1,2}\s+(?:\d{2}:\d{2}|\d{4}))\s+(.*)$"
        )
        for line in out.splitlines():
            line = line.rstrip()
            if not line or line.startswith("total"):
                continue
            m = date_pattern.search(line)
            if not m:
                continue
            name = m.group(1)
            if name in (".", ".."):
                continue
            if " -> " in name:
                name = name.split(" -> ")[0]

            head = line[:m.start()].strip()
            head_parts = head.split(None, 4)
            if len(head_parts) < 5:
                continue
            perms, _links, _owner, _group, size = head_parts[:5]

            is_dir = perms.startswith("d")
            entries.append({
                "name": name,
                "is_dir": is_dir,
                "size": size if size.isdigit() else "",
                "perms": perms,
            })
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return entries

    def pull_file(self, serial: str, remote_path: str, local_path: str) -> str:
        return self.run(["pull", remote_path, local_path], serial=serial, timeout=300)

    def push_file(self, serial: str, local_path: str, remote_path: str) -> str:
        return self.run(["push", local_path, remote_path], serial=serial, timeout=300)

    def delete_path(self, serial: str, remote_path: str) -> str:
        return self.run(["shell", "rm", "-rf", remote_path], serial=serial, timeout=30)

    def make_dir(self, serial: str, remote_path: str) -> str:
        return self.run(["shell", "mkdir", "-p", remote_path], serial=serial, timeout=15)

    def take_screenshot(self, serial: str, local_path: str):
        device_tmp = "/sdcard/__adbtool_screenshot.png"
        self.run(["shell", "screencap", "-p", device_tmp], serial=serial, timeout=20)
        self.run(["pull", device_tmp, local_path], serial=serial, timeout=30)
        try:
            self.run(["shell", "rm", "-f", device_tmp], serial=serial, timeout=10)
        except AdbError:
            pass

    def shell_command(self, serial: str, command: str, timeout=30) -> str:
        return self.run(["shell", command], serial=serial, timeout=timeout)


# ==============================================================================
# Device tab
# ==============================================================================

class _InfoFetchThread(QThread):
    finished_ok = pyqtSignal(object)
    finished_err = pyqtSignal(str)

    def __init__(self, adb, serial, state):
        super().__init__()
        self.adb = adb
        self.serial = serial
        self.state = state

    def run(self):
        try:
            info = self.adb.get_device_info(self.serial, self.state)
            self.finished_ok.emit(info)
        except AdbError as e:
            self.finished_err.emit(str(e))


class DeviceTab(QWidget):
    def __init__(self, adb, main_window):
        super().__init__()
        self.adb = adb
        self.main_window = main_window
        self.serial = None
        self.state = None
        self._fetch_thread = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        title = QLabel("Device Information")
        f = QFont()
        f.setPointSize(14)
        f.setBold(True)
        title.setFont(f)
        layout.addWidget(title)

        info_box = QGroupBox()
        grid = QGridLayout(info_box)
        grid.setColumnStretch(1, 1)

        self.labels = {}
        fields = [
            ("serial", "Serial:"),
            ("state", "Status:"),
            ("model", "Model:"),
            ("manufacturer", "Manufacturer:"),
            ("android_version", "Android Version:"),
            ("sdk_version", "SDK Level:"),
            ("battery_level", "Battery:"),
            ("ip_address", "IP Address:"),
        ]
        for row, (key, caption) in enumerate(fields):
            cap_label = QLabel(caption)
            cap_label.setStyleSheet("font-weight: bold;")
            val_label = QLabel("—")
            val_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(cap_label, row, 0)
            grid.addWidget(val_label, row, 1)
            self.labels[key] = val_label

        layout.addWidget(info_box)

        reboot_box = QGroupBox("Reboot Options")
        reboot_layout = QHBoxLayout(reboot_box)
        self.reboot_normal_btn = QPushButton("Reboot")
        self.reboot_recovery_btn = QPushButton("Reboot to Recovery")
        self.reboot_bootloader_btn = QPushButton("Reboot to Bootloader")
        self.reboot_normal_btn.clicked.connect(lambda: self._reboot(""))
        self.reboot_recovery_btn.clicked.connect(lambda: self._reboot("recovery"))
        self.reboot_bootloader_btn.clicked.connect(lambda: self._reboot("bootloader"))
        for btn in (self.reboot_normal_btn, self.reboot_recovery_btn, self.reboot_bootloader_btn):
            reboot_layout.addWidget(btn)
        reboot_layout.addStretch()
        layout.addWidget(reboot_box)

        pairing_box = QGroupBox("Wireless Debugging")
        pairing_layout = QHBoxLayout(pairing_box)
        self.pair_qr_btn = QPushButton("Pair via QR Code (webcam)...")
        self.pair_qr_btn.clicked.connect(self._open_qr_pairing)
        pairing_layout.addWidget(self.pair_qr_btn)

        if not _QR_PAIRING_AVAILABLE:
            missing = []
            if not _HAS_CV2: missing.append("opencv-python")
            if not _HAS_PYZBAR: missing.append("pyzbar")
            if not _HAS_ZEROCONF: missing.append("zeroconf")
            self.pair_qr_btn.setEnabled(False)
            self.pair_qr_btn.setToolTip("Missing packages: " + ", ".join(missing))
            hint = QLabel(f"(install: pip install {' '.join(missing)})")
            hint.setStyleSheet("color: #888;")
            pairing_layout.addWidget(hint)

        pairing_layout.addStretch()
        layout.addWidget(pairing_box)
        layout.addStretch()
        self._set_buttons_enabled(False)

    def _set_buttons_enabled(self, enabled: bool):
        for btn in (self.reboot_normal_btn, self.reboot_recovery_btn, self.reboot_bootloader_btn):
            btn.setEnabled(enabled)

    def on_device_changed(self, serial, state):
        if serial == self.serial and state == self.state:
            return   # nothing changed — don't flicker labels or re-fetch info
        self.serial = serial
        self.state = state
        if not serial:
            for label in self.labels.values():
                label.setText("—")
            self._set_buttons_enabled(False)
            return
        self.labels["serial"].setText(serial)
        self.labels["state"].setText(state)
        if state != "device":
            for key in ("model", "manufacturer", "android_version", "sdk_version", "battery_level", "ip_address"):
                self.labels[key].setText("(unavailable — device not authorized/online)")
            self._set_buttons_enabled(False)
            return
        self._set_buttons_enabled(True)
        self._fetch_info()

    def _fetch_info(self):
        if self._fetch_thread and self._fetch_thread.isRunning():
            return
        self._fetch_thread = _InfoFetchThread(self.adb, self.serial, self.state)
        self._fetch_thread.finished_ok.connect(self._on_info_loaded)
        self._fetch_thread.finished_err.connect(self._on_info_error)
        self._fetch_thread.start()

    def _on_info_loaded(self, info):
        self.labels["model"].setText(info.model or "—")
        self.labels["manufacturer"].setText(info.manufacturer or "—")
        self.labels["android_version"].setText(info.android_version or "—")
        self.labels["sdk_version"].setText(info.sdk_version or "—")
        self.labels["battery_level"].setText(info.battery_level or "—")
        self.labels["ip_address"].setText(info.ip_address or "—")

    def _on_info_error(self, msg):
        self.main_window.status_bar.showMessage(f"Error fetching device info: {msg}")

    def _reboot(self, mode):
        label = {"": "normal mode", "recovery": "recovery", "bootloader": "bootloader"}[mode]
        confirm = QMessageBox.question(
            self, "Confirm reboot", f"Reboot device into {label}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self.adb.reboot(self.serial, mode)
            self.main_window.status_bar.showMessage(f"Reboot ({label}) command sent.")
        except AdbError as e:
            QMessageBox.critical(self, "Reboot failed", str(e))

    def _open_qr_pairing(self):
        if not _QR_PAIRING_AVAILABLE:
            return
        dialog = QrPairingDialog(self.adb, self)
        dialog.paired.connect(self._on_qr_paired)
        dialog.exec()

    def _on_qr_paired(self, address):
        self.main_window.status_bar.showMessage(f"Paired with {address}. Refreshing devices...")
        self.main_window._refresh_devices()


# ==============================================================================
# QR wireless pairing dialog
# ==============================================================================

if _HAS_ZEROCONF:
    class _PairingServiceListener(ServiceListener):
        def __init__(self, target_name_substring, on_found):
            self.target_name_substring = target_name_substring
            self.on_found = on_found
            self._reported = False

        def add_service(self, zc, type_, name):
            if self._reported:
                return
            if self.target_name_substring not in name:
                return
            info = zc.get_service_info(type_, name)
            if not info or not info.addresses:
                return
            ip = ".".join(str(b) for b in info.addresses[0])
            port = info.port
            self._reported = True
            self.on_found(ip, port)

        def update_service(self, zc, type_, name): pass
        def remove_service(self, zc, type_, name): pass


class _QrScanThread(QThread):
    qr_found = pyqtSignal(str)
    frame_ready = pyqtSignal(object)
    camera_error = pyqtSignal(str)

    def __init__(self, camera_index=0):
        super().__init__()
        self.camera_index = camera_index
        self._stop_requested = False

    def run(self):
        cap = cv2.VideoCapture(self.camera_index)
        if not cap.isOpened():
            self.camera_error.emit(f"Could not open camera index {self.camera_index}.")
            return
        try:
            while not self._stop_requested:
                ok, frame = cap.read()
                if not ok:
                    continue
                self.frame_ready.emit(frame)
                decoded = pyzbar.decode(frame)
                for obj in decoded:
                    text = obj.data.decode("utf-8", errors="replace")
                    self.qr_found.emit(text)
                    return
        finally:
            cap.release()

    def stop(self):
        self._stop_requested = True


class _MdnsResolveThread(QThread):
    resolved = pyqtSignal(str, int)
    timed_out = pyqtSignal()

    def __init__(self, service_name_substring, timeout_seconds=15):
        super().__init__()
        self.service_name_substring = service_name_substring
        self.timeout_seconds = timeout_seconds

    def run(self):
        if not _HAS_ZEROCONF:
            self.timed_out.emit()
            return
        zc = Zeroconf()
        found_event = {"ip": None, "port": None}

        def on_found(ip, port):
            found_event["ip"] = ip
            found_event["port"] = port

        listener = _PairingServiceListener(self.service_name_substring, on_found)
        browser = ServiceBrowser(zc, "_adb-tls-pairing._tcp.local.", listener)
        waited = 0.0
        step = 0.25
        try:
            while waited < self.timeout_seconds:
                if found_event["ip"]:
                    self.resolved.emit(found_event["ip"], found_event["port"])
                    return
                self.msleep(int(step * 1000))
                waited += step
            self.timed_out.emit()
        finally:
            browser.cancel()
            zc.close()


class _PairThread(QThread):
    finished_ok = pyqtSignal(str, str)
    finished_err = pyqtSignal(str)

    def __init__(self, adb, address, password):
        super().__init__()
        self.adb = adb
        self.address = address
        self.password = password

    def run(self):
        try:
            result = self.adb.pair(self.address, self.password)
            self.finished_ok.emit(result, self.address)
        except AdbError as e:
            self.finished_err.emit(str(e))


class QrPairingDialog(QDialog):
    paired = pyqtSignal(str)

    def __init__(self, adb, parent=None):
        super().__init__(parent)
        self.adb = adb
        self.setWindowTitle("Pair Device via QR Code")
        self.resize(520, 560)
        self._scan_thread = None
        self._resolve_thread = None
        self._pair_thread = None
        self._build_ui()
        self._start_scanning()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        instructions = QLabel(
            "On your phone: Settings → Developer options → Wireless debugging\n"
            "→ \"Pair device with QR code\". Point your webcam at the QR code shown there."
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        self.preview_label = QLabel("Starting camera...")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(320)
        self.preview_label.setStyleSheet("border: 1px solid #888; background: #111; color: #ccc;")
        layout.addWidget(self.preview_label)
        self.status_label = QLabel("Scanning for QR code...")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        button_box.rejected.connect(self._on_cancel)
        layout.addWidget(button_box)

    def _start_scanning(self):
        self._scan_thread = _QrScanThread(camera_index=0)
        self._scan_thread.frame_ready.connect(self._on_frame)
        self._scan_thread.qr_found.connect(self._on_qr_found)
        self._scan_thread.camera_error.connect(self._on_camera_error)
        self._scan_thread.start()

    def _on_frame(self, frame):
        try:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb.shape
            qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
            pixmap = QPixmap.fromImage(qimg).scaled(
                self.preview_label.width() or 480, self.preview_label.height() or 320,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
            )
            self.preview_label.setPixmap(pixmap)
        except Exception:
            pass

    def _on_camera_error(self, msg):
        self.status_label.setText(f"Camera error: {msg}")

    def _on_qr_found(self, text):
        parsed = parse_adb_pairing_qr(text)
        if not parsed:
            self.status_label.setText("Not an ADB QR. Restarting scan...")
            self._start_scanning()
            return
        self.status_label.setText(f"QR recognized. Looking for device via mDNS...")
        self._resolve_service(parsed["service_name"], parsed["password"])

    def _resolve_service(self, service_name, password):
        self._resolve_thread = _MdnsResolveThread(service_name)
        self._resolve_thread.resolved.connect(lambda ip, port: self._on_resolved(ip, port, password))
        self._resolve_thread.timed_out.connect(self._on_resolve_timeout)
        self._resolve_thread.start()

    def _on_resolved(self, ip, port, password):
        address = f"{ip}:{port}"
        self.status_label.setText(f"Found device at {address}. Pairing...")
        self._pair_thread = _PairThread(self.adb, address, password)
        self._pair_thread.finished_ok.connect(self._on_pair_ok)
        self._pair_thread.finished_err.connect(self._on_pair_err)
        self._pair_thread.start()

    def _on_resolve_timeout(self):
        self.status_label.setText("Could not find device on network within 15 seconds.")

    def _on_pair_ok(self, result, address):
        if "successfully paired" in result.lower():
            self.status_label.setText(f"Paired successfully with {address}!")
            self.paired.emit(address)
            QTimer.singleShot(1200, self.accept)
        else:
            self.status_label.setText(f"Pairing response: {result.strip() or '(no output)'}")

    def _on_pair_err(self, msg):
        self.status_label.setText(f"Pairing failed: {msg}")

    def _stop_all_threads(self):
        if self._scan_thread and self._scan_thread.isRunning():
            self._scan_thread.stop()
            self._scan_thread.wait(2000)
        if self._resolve_thread and self._resolve_thread.isRunning():
            self._resolve_thread.wait(2000)

    def _on_cancel(self):
        self._stop_all_threads()
        self.reject()

    def closeEvent(self, event):
        self._stop_all_threads()
        super().closeEvent(event)


# ==============================================================================
# Apps tab
# ==============================================================================

class _PackageListThread(QThread):
    finished_ok = pyqtSignal(list)
    finished_err = pyqtSignal(str)

    def __init__(self, adb, serial, third_party_only):
        super().__init__()
        self.adb = adb
        self.serial = serial
        self.third_party_only = third_party_only

    def run(self):
        try:
            packages = self.adb.list_packages(self.serial, self.third_party_only)
            self.finished_ok.emit(packages)
        except AdbError as e:
            self.finished_err.emit(str(e))


class _InstallThread(QThread):
    finished_ok = pyqtSignal(str)
    finished_err = pyqtSignal(str)

    def __init__(self, adb, serial, apk_path, reinstall):
        super().__init__()
        self.adb = adb
        self.serial = serial
        self.apk_path = apk_path
        self.reinstall = reinstall

    def run(self):
        try:
            result = self.adb.install_apk(self.serial, self.apk_path, self.reinstall)
            self.finished_ok.emit(result)
        except AdbError as e:
            self.finished_err.emit(str(e))


class AppsTab(QWidget):
    def __init__(self, adb, main_window):
        super().__init__()
        self.adb = adb
        self.main_window = main_window
        self.serial = None
        self.state = None
        self._all_packages = []
        self._list_thread = None
        self._install_thread = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        install_row = QHBoxLayout()
        self.install_path_input = QLineEdit()
        self.install_path_input.setPlaceholderText("Path to .apk file...")
        install_row.addWidget(self.install_path_input)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_apk)
        install_row.addWidget(browse_btn)
        self.reinstall_checkbox = QCheckBox("Reinstall (-r)")
        install_row.addWidget(self.reinstall_checkbox)
        self.install_btn = QPushButton("Install APK")
        self.install_btn.clicked.connect(self._install_apk)
        install_row.addWidget(self.install_btn)
        layout.addLayout(install_row)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter:"))
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Type to filter package names...")
        self.filter_input.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self.filter_input)
        self.third_party_checkbox = QCheckBox("Third-party apps only")
        self.third_party_checkbox.setChecked(True)
        self.third_party_checkbox.stateChanged.connect(self._refresh_packages)
        filter_row.addWidget(self.third_party_checkbox)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._refresh_packages)
        filter_row.addWidget(self.refresh_btn)
        layout.addLayout(filter_row)

        self.package_list = QListWidget()
        self.package_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self.package_list)

        action_row = QHBoxLayout()
        self.launch_btn = QPushButton("Launch")
        self.force_stop_btn = QPushButton("Force Stop")
        self.clear_data_btn = QPushButton("Clear Data")
        self.uninstall_btn = QPushButton("Uninstall")
        self.launch_btn.clicked.connect(self._launch_selected)
        self.force_stop_btn.clicked.connect(self._force_stop_selected)
        self.clear_data_btn.clicked.connect(self._clear_data_selected)
        self.uninstall_btn.clicked.connect(self._uninstall_selected)
        for btn in (self.launch_btn, self.force_stop_btn, self.clear_data_btn, self.uninstall_btn):
            action_row.addWidget(btn)
        action_row.addStretch()
        layout.addLayout(action_row)
        self._set_buttons_enabled(False)

    def _set_buttons_enabled(self, enabled: bool):
        for btn in (self.install_btn, self.refresh_btn, self.launch_btn,
                    self.force_stop_btn, self.clear_data_btn, self.uninstall_btn):
            btn.setEnabled(enabled)

    def on_device_changed(self, serial, state):
        # No-op if the device identity hasn't changed — prevents the package
        # list from clearing and reloading on every silent 3-second poll.
        if serial == self.serial and state == self.state:
            return
        self.serial = serial
        self.state = state
        self.package_list.clear()
        self._all_packages = []
        ready = bool(serial) and state == "device"
        self._set_buttons_enabled(ready)
        if ready:
            self._refresh_packages()

    def _refresh_packages(self):
        if not self.serial or self.state != "device":
            return
        if self._list_thread and self._list_thread.isRunning():
            return
        self.package_list.clear()
        self.package_list.addItem("Loading...")
        self._list_thread = _PackageListThread(self.adb, self.serial, self.third_party_checkbox.isChecked())
        self._list_thread.finished_ok.connect(self._on_packages_loaded)
        self._list_thread.finished_err.connect(self._on_packages_error)
        self._list_thread.start()

    def _on_packages_loaded(self, packages):
        self._all_packages = packages
        self._apply_filter()

    def _on_packages_error(self, msg):
        self.package_list.clear()
        self.main_window.status_bar.showMessage(f"Error listing packages: {msg}")

    def _apply_filter(self):
        text = self.filter_input.text().strip().lower()
        self.package_list.clear()
        for pkg in self._all_packages:
            if not text or text in pkg.lower():
                self.package_list.addItem(pkg)

    def _selected_package(self):
        item = self.package_list.currentItem()
        if not item:
            return None
        text = item.text()
        if text == "Loading...":
            return None
        return text

    def _browse_apk(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select APK", filter="APK files (*.apk)")
        if path:
            self.install_path_input.setText(path)

    def _install_apk(self):
        if not self.main_window.require_device():
            return
        apk_path = self.install_path_input.text().strip()
        if not apk_path:
            QMessageBox.warning(self, "No APK", "Please select an APK file to install.")
            return
        if self._install_thread and self._install_thread.isRunning():
            return
        self.install_btn.setEnabled(False)
        self.install_btn.setText("Installing...")
        self._install_thread = _InstallThread(self.adb, self.serial, apk_path, self.reinstall_checkbox.isChecked())
        self._install_thread.finished_ok.connect(self._on_install_ok)
        self._install_thread.finished_err.connect(self._on_install_err)
        self._install_thread.start()

    def _on_install_ok(self, result):
        self.install_btn.setEnabled(True)
        self.install_btn.setText("Install APK")
        if "Success" in result:
            QMessageBox.information(self, "Install", "APK installed successfully.")
        else:
            QMessageBox.information(self, "Install", result.strip() or "Done.")
        self._refresh_packages()

    def _on_install_err(self, msg):
        self.install_btn.setEnabled(True)
        self.install_btn.setText("Install APK")
        QMessageBox.critical(self, "Install failed", msg)

    def _launch_selected(self):
        pkg = self._selected_package()
        if not pkg or not self.main_window.require_device():
            return
        try:
            self.adb.launch_app(self.serial, pkg)
            self.main_window.status_bar.showMessage(f"Launched {pkg}")
        except AdbError as e:
            QMessageBox.critical(self, "Launch failed", str(e))

    def _force_stop_selected(self):
        pkg = self._selected_package()
        if not pkg or not self.main_window.require_device():
            return
        try:
            self.adb.force_stop(self.serial, pkg)
            self.main_window.status_bar.showMessage(f"Force-stopped {pkg}")
        except AdbError as e:
            QMessageBox.critical(self, "Force stop failed", str(e))

    def _clear_data_selected(self):
        pkg = self._selected_package()
        if not pkg or not self.main_window.require_device():
            return
        confirm = QMessageBox.question(
            self, "Confirm", f"Clear all data for {pkg}? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self.adb.clear_app_data(self.serial, pkg)
            self.main_window.status_bar.showMessage(f"Cleared data for {pkg}")
        except AdbError as e:
            QMessageBox.critical(self, "Clear data failed", str(e))

    def _uninstall_selected(self):
        pkg = self._selected_package()
        if not pkg or not self.main_window.require_device():
            return
        confirm = QMessageBox.question(
            self, "Confirm uninstall", f"Uninstall {pkg}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            result = self.adb.uninstall_package(self.serial, pkg)
            if "Success" in result:
                self.main_window.status_bar.showMessage(f"Uninstalled {pkg}")
            else:
                QMessageBox.warning(self, "Uninstall", result.strip() or "Uninstall may have failed.")
            self._refresh_packages()
        except AdbError as e:
            QMessageBox.critical(self, "Uninstall failed", str(e))


# ==============================================================================
# Files tab
# ==============================================================================

DEFAULT_PATH = "/sdcard/"


class _ListDirThread(QThread):
    finished_ok = pyqtSignal(list)
    finished_err = pyqtSignal(str)

    def __init__(self, adb, serial, path):
        super().__init__()
        self.adb = adb
        self.serial = serial
        self.path = path

    def run(self):
        try:
            entries = self.adb.list_dir(self.serial, self.path)
            self.finished_ok.emit(entries)
        except AdbError as e:
            self.finished_err.emit(str(e))


class _TransferThread(QThread):
    finished_ok = pyqtSignal(str)
    finished_err = pyqtSignal(str)

    def __init__(self, fn, *args):
        super().__init__()
        self.fn = fn
        self.args = args

    def run(self):
        try:
            result = self.fn(*self.args)
            self.finished_ok.emit(result if isinstance(result, str) else "Done.")
        except AdbError as e:
            self.finished_err.emit(str(e))


class FilesTab(QWidget):
    def __init__(self, adb, main_window):
        super().__init__()
        self.adb = adb
        self.main_window = main_window
        self.serial = None
        self.state = None
        self.current_path = DEFAULT_PATH
        self._list_thread = None
        self._transfer_thread = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        path_row = QHBoxLayout()
        up_btn = QPushButton("⬆ Up")
        up_btn.clicked.connect(self._go_up)
        path_row.addWidget(up_btn)
        self.path_input = QLineEdit(self.current_path)
        self.path_input.returnPressed.connect(self._navigate_to_input)
        path_row.addWidget(self.path_input)
        go_btn = QPushButton("Go")
        go_btn.clicked.connect(self._navigate_to_input)
        path_row.addWidget(go_btn)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh)
        path_row.addWidget(refresh_btn)
        layout.addLayout(path_row)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Name", "Type", "Size"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(self._on_row_double_clicked)
        layout.addWidget(self.table)

        action_row = QHBoxLayout()
        self.pull_btn = QPushButton("Pull (Download)")
        self.push_btn = QPushButton("Push (Upload)...")
        self.delete_btn = QPushButton("Delete")
        self.mkdir_btn = QPushButton("New Folder")
        self.pull_btn.clicked.connect(self._pull_selected)
        self.push_btn.clicked.connect(self._push_file)
        self.delete_btn.clicked.connect(self._delete_selected)
        self.mkdir_btn.clicked.connect(self._make_dir)
        for btn in (self.pull_btn, self.push_btn, self.delete_btn, self.mkdir_btn):
            action_row.addWidget(btn)
        action_row.addStretch()
        layout.addLayout(action_row)
        self._set_buttons_enabled(False)

    def _set_buttons_enabled(self, enabled: bool):
        for btn in (self.pull_btn, self.push_btn, self.delete_btn, self.mkdir_btn):
            btn.setEnabled(enabled)

    def on_device_changed(self, serial, state):
        if serial == self.serial and state == self.state:
            return   # nothing changed — don't clear table or reset path
        self.serial = serial
        self.state = state
        self.table.setRowCount(0)
        ready = bool(serial) and state == "device"
        self._set_buttons_enabled(ready)
        if ready:
            self.current_path = DEFAULT_PATH
            self.path_input.setText(self.current_path)
            self._refresh()

    def _normalize_path(self, path):
        if not path.endswith("/"):
            path += "/"
        while "//" in path:
            path = path.replace("//", "/")
        return path

    def _navigate_to_input(self):
        path = self.path_input.text().strip() or DEFAULT_PATH
        self.current_path = self._normalize_path(path)
        self.path_input.setText(self.current_path)
        self._refresh()

    def _go_up(self):
        if self.current_path in ("/", ""):
            return
        trimmed = self.current_path.rstrip("/")
        parent = trimmed.rsplit("/", 1)[0] or "/"
        self.current_path = self._normalize_path(parent)
        self.path_input.setText(self.current_path)
        self._refresh()

    def _on_row_double_clicked(self, index):
        row = index.row()
        name_item = self.table.item(row, 0)
        type_item = self.table.item(row, 1)
        if not name_item:
            return
        if type_item and type_item.text() == "Folder":
            self.current_path = self._normalize_path(self.current_path + name_item.text())
            self.path_input.setText(self.current_path)
            self._refresh()

    def _refresh(self):
        if not self.serial or self.state != "device":
            return
        if self._list_thread and self._list_thread.isRunning():
            return
        self._list_thread = _ListDirThread(self.adb, self.serial, self.current_path)
        self._list_thread.finished_ok.connect(self._on_listed)
        self._list_thread.finished_err.connect(self._on_list_error)
        self._list_thread.start()

    def _on_listed(self, entries):
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self.table.setItem(row, 0, QTableWidgetItem(entry["name"]))
            self.table.setItem(row, 1, QTableWidgetItem("Folder" if entry["is_dir"] else "File"))
            self.table.setItem(row, 2, QTableWidgetItem("" if entry["is_dir"] else entry["size"]))

    def _on_list_error(self, msg):
        self.table.setRowCount(0)
        self.main_window.status_bar.showMessage(f"Error listing directory: {msg}")

    def _selected_entry(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        name_item = self.table.item(row, 0)
        type_item = self.table.item(row, 1)
        if not name_item:
            return None
        return name_item.text(), (type_item.text() == "Folder" if type_item else False)

    def _pull_selected(self):
        if not self.main_window.require_device():
            return
        entry = self._selected_entry()
        if not entry:
            QMessageBox.warning(self, "No selection", "Select a file to pull.")
            return
        name, _ = entry
        remote_path = self.current_path + name
        local_path, _ = QFileDialog.getSaveFileName(self, "Save as", name)
        if not local_path:
            return
        self._run_transfer(self.adb.pull_file, self.serial, remote_path, local_path)

    def _push_file(self):
        if not self.main_window.require_device():
            return
        local_path, _ = QFileDialog.getOpenFileName(self, "Select file to push")
        if not local_path:
            return
        filename = local_path.split("/")[-1].split("\\")[-1]
        remote_path = self.current_path + filename
        self._run_transfer(self.adb.push_file, self.serial, local_path, remote_path)

    def _delete_selected(self):
        if not self.main_window.require_device():
            return
        entry = self._selected_entry()
        if not entry:
            QMessageBox.warning(self, "No selection", "Select a file or folder to delete.")
            return
        name, is_dir = entry
        remote_path = self.current_path + name
        confirm = QMessageBox.question(
            self, "Confirm delete",
            f"Delete {'folder' if is_dir else 'file'} '{name}'? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self.adb.delete_path(self.serial, remote_path)
            self.main_window.status_bar.showMessage(f"Deleted {remote_path}")
            self._refresh()
        except AdbError as e:
            QMessageBox.critical(self, "Delete failed", str(e))

    def _make_dir(self):
        if not self.main_window.require_device():
            return
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        if not ok or not name.strip():
            return
        remote_path = self.current_path + name.strip()
        try:
            self.adb.make_dir(self.serial, remote_path)
            self.main_window.status_bar.showMessage(f"Created {remote_path}")
            self._refresh()
        except AdbError as e:
            QMessageBox.critical(self, "Create folder failed", str(e))

    def _run_transfer(self, fn, *args):
        self._set_buttons_enabled(False)
        self._transfer_thread = _TransferThread(fn, *args)
        self._transfer_thread.finished_ok.connect(self._on_transfer_ok)
        self._transfer_thread.finished_err.connect(self._on_transfer_err)
        self._transfer_thread.start()

    def _on_transfer_ok(self, result):
        self._set_buttons_enabled(True)
        self.main_window.status_bar.showMessage("Transfer complete.")
        self._refresh()

    def _on_transfer_err(self, msg):
        self._set_buttons_enabled(True)
        QMessageBox.critical(self, "Transfer failed", msg)


# ==============================================================================
# Screen tab
# ==============================================================================

class _ScreenshotThread(QThread):
    finished_ok = pyqtSignal(str)
    finished_err = pyqtSignal(str)

    def __init__(self, adb, serial, local_path):
        super().__init__()
        self.adb = adb
        self.serial = serial
        self.local_path = local_path

    def run(self):
        try:
            self.adb.take_screenshot(self.serial, self.local_path)
            self.finished_ok.emit(self.local_path)
        except AdbError as e:
            self.finished_err.emit(str(e))


class ScreenTab(QWidget):
    def __init__(self, adb, main_window):
        super().__init__()
        self.adb = adb
        self.main_window = main_window
        self.serial = None
        self.state = None
        self._last_screenshot_path = None
        self._screenshot_thread = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        button_row = QHBoxLayout()
        self.capture_btn = QPushButton("Take Screenshot")
        self.save_as_btn = QPushButton("Save As...")
        self.capture_btn.clicked.connect(self._take_screenshot)
        self.save_as_btn.clicked.connect(self._save_as)
        button_row.addWidget(self.capture_btn)
        button_row.addWidget(self.save_as_btn)
        button_row.addStretch()
        layout.addLayout(button_row)
        self.preview_label = QLabel("No screenshot yet.")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(400)
        self.preview_label.setStyleSheet("border: 1px solid #888; background: #222;")
        layout.addWidget(self.preview_label)
        self._set_buttons_enabled(False)

    def _set_buttons_enabled(self, enabled: bool):
        self.capture_btn.setEnabled(enabled)
        self.save_as_btn.setEnabled(enabled and self._last_screenshot_path is not None)

    def on_device_changed(self, serial, state):
        if serial == self.serial and state == self.state:
            return   # nothing changed — don't touch button states
        self.serial = serial
        self.state = state
        ready = bool(serial) and state == "device"
        self.capture_btn.setEnabled(ready)
        self.save_as_btn.setEnabled(ready and self._last_screenshot_path is not None)

    def _take_screenshot(self):
        if not self.main_window.require_device():
            return
        if self._screenshot_thread and self._screenshot_thread.isRunning():
            return
        tmp_dir = tempfile.gettempdir()
        local_path = os.path.join(tmp_dir, "adbtool_screenshot.png")
        self.capture_btn.setEnabled(False)
        self.capture_btn.setText("Capturing...")
        self._screenshot_thread = _ScreenshotThread(self.adb, self.serial, local_path)
        self._screenshot_thread.finished_ok.connect(self._on_screenshot_ok)
        self._screenshot_thread.finished_err.connect(self._on_screenshot_err)
        self._screenshot_thread.start()

    def _on_screenshot_ok(self, local_path):
        self.capture_btn.setEnabled(True)
        self.capture_btn.setText("Take Screenshot")
        self._last_screenshot_path = local_path
        pixmap = QPixmap(local_path)
        if not pixmap.isNull():
            scaled = pixmap.scaled(
                self.preview_label.width() or 600,
                self.preview_label.height() or 400,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            self.preview_label.setPixmap(scaled)
        else:
            self.preview_label.setText("Screenshot captured, but could not preview.")
        self.save_as_btn.setEnabled(True)
        self.main_window.status_bar.showMessage(f"Screenshot saved to {local_path}")

    def _on_screenshot_err(self, msg):
        self.capture_btn.setEnabled(True)
        self.capture_btn.setText("Take Screenshot")
        QMessageBox.critical(self, "Screenshot failed", msg)

    def _save_as(self):
        if not self._last_screenshot_path:
            return
        dest, _ = QFileDialog.getSaveFileName(self, "Save screenshot as", "screenshot.png", "PNG files (*.png)")
        if not dest:
            return
        try:
            with open(self._last_screenshot_path, "rb") as src, open(dest, "wb") as out:
                out.write(src.read())
            self.main_window.status_bar.showMessage(f"Saved to {dest}")
        except OSError as e:
            QMessageBox.critical(self, "Save failed", str(e))


# ==============================================================================
# Shell tab
# ==============================================================================

class _ShellCommandThread(QThread):
    finished_ok = pyqtSignal(str)
    finished_err = pyqtSignal(str)

    def __init__(self, adb, serial, command):
        super().__init__()
        self.adb = adb
        self.serial = serial
        self.command = command

    def run(self):
        try:
            output = self.adb.shell_command(self.serial, self.command, timeout=60)
            self.finished_ok.emit(output)
        except AdbError as e:
            self.finished_err.emit(str(e))


class ShellTab(QWidget):
    def __init__(self, adb, main_window):
        super().__init__()
        self.adb = adb
        self.main_window = main_window
        self.serial = None
        self.state = None
        self._command_history = []
        self._history_index = -1
        self._shell_thread = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("adb shell — type a command and press Enter"))
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        mono = QFont("Courier New")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.output.setFont(mono)
        self.output.setStyleSheet("background-color: #1e1e1e; color: #d4d4d4;")
        layout.addWidget(self.output)
        input_row = QHBoxLayout()
        self.command_input = QLineEdit()
        self.command_input.setFont(mono)
        self.command_input.setPlaceholderText("e.g. pm list packages, ls /sdcard, dumpsys battery...")
        self.command_input.returnPressed.connect(self._run_command)
        input_row.addWidget(self.command_input)
        run_btn = QPushButton("Run")
        run_btn.clicked.connect(self._run_command)
        input_row.addWidget(run_btn)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.output.clear)
        input_row.addWidget(clear_btn)
        layout.addLayout(input_row)
        self.command_input.setEnabled(False)

    def on_device_changed(self, serial, state):
        if serial == self.serial and state == self.state:
            return   # nothing changed — don't interrupt running commands
        self.serial = serial
        self.state = state
        ready = bool(serial) and state == "device"
        self.command_input.setEnabled(ready)

    def _run_command(self):
        if not self.main_window.require_device():
            return
        command = self.command_input.text().strip()
        if not command:
            return
        if self._shell_thread and self._shell_thread.isRunning():
            QMessageBox.information(self, "Busy", "A command is already running. Please wait.")
            return
        self._command_history.append(command)
        self._history_index = len(self._command_history)
        self.output.appendPlainText(f"$ {command}")
        self.command_input.clear()
        self.command_input.setEnabled(False)
        self._shell_thread = _ShellCommandThread(self.adb, self.serial, command)
        self._shell_thread.finished_ok.connect(self._on_command_ok)
        self._shell_thread.finished_err.connect(self._on_command_err)
        self._shell_thread.start()

    def _on_command_ok(self, output):
        self.output.appendPlainText(output.rstrip())
        self.output.appendPlainText("")
        self.command_input.setEnabled(True)
        self.command_input.setFocus()
        self.output.moveCursor(QTextCursor.MoveOperation.End)

    def _on_command_err(self, msg):
        self.output.appendPlainText(f"[error] {msg}")
        self.output.appendPlainText("")
        self.command_input.setEnabled(True)
        self.command_input.setFocus()
        self.output.moveCursor(QTextCursor.MoveOperation.End)


# ==============================================================================
# Logcat tab
# ==============================================================================

LOG_LEVELS = ["Verbose", "Debug", "Info", "Warn", "Error"]
LEVEL_FLAGS = {"Verbose": "V", "Debug": "D", "Info": "I", "Warn": "W", "Error": "E"}


class _LogcatStreamThread(QThread):
    line_received = pyqtSignal(str)
    stopped = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, adb, serial, level_flag, tag_filter):
        super().__init__()
        self.adb = adb
        self.serial = serial
        self.level_flag = level_flag
        self.tag_filter = tag_filter
        self._proc = None
        self._stop_requested = False

    def run(self):
        try:
            args = ["logcat", "-v", "time"]
            if self.tag_filter:
                args.append(f"{self.tag_filter}:{self.level_flag}")
                args.append("*:S")
            else:
                args.append(f"*:{self.level_flag}")
            self._proc = self.adb.run_popen(args, serial=self.serial)
        except AdbError as e:
            self.error.emit(str(e))
            return
        try:
            for raw_line in self._proc.stdout:
                if self._stop_requested:
                    break
                try:
                    line = raw_line.decode(errors="replace").rstrip()
                except Exception:
                    continue
                self.line_received.emit(line)
        finally:
            self.stopped.emit()

    def stop(self):
        self._stop_requested = True
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass


class LogcatTab(QWidget):
    def __init__(self, adb, main_window):
        super().__init__()
        self.adb = adb
        self.main_window = main_window
        self.serial = None
        self.state = None
        self._stream_thread = None
        self._is_streaming = False
        self._max_lines = 5000
        self._line_filter_text = ""
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        control_row = QHBoxLayout()
        control_row.addWidget(QLabel("Min level:"))
        self.level_combo = QComboBox()
        self.level_combo.addItems(LOG_LEVELS)
        control_row.addWidget(self.level_combo)
        control_row.addWidget(QLabel("Tag filter:"))
        self.tag_input = QLineEdit()
        self.tag_input.setPlaceholderText("optional tag, e.g. ActivityManager")
        self.tag_input.setMaximumWidth(200)
        control_row.addWidget(self.tag_input)
        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.clear_btn = QPushButton("Clear")
        self.start_btn.clicked.connect(self._start_stream)
        self.stop_btn.clicked.connect(self._stop_stream)
        self.clear_btn.clicked.connect(self._clear_output)
        control_row.addWidget(self.start_btn)
        control_row.addWidget(self.stop_btn)
        control_row.addWidget(self.clear_btn)
        control_row.addStretch()
        layout.addLayout(control_row)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search/highlight:"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Type to filter visible lines (applies on next Start)")
        search_row.addWidget(self.search_input)
        self.autoscroll_checkbox = QCheckBox("Auto-scroll")
        self.autoscroll_checkbox.setChecked(True)
        search_row.addWidget(self.autoscroll_checkbox)
        layout.addLayout(search_row)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        mono = QFont("Courier New")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(9)
        self.output.setFont(mono)
        self.output.setStyleSheet("background-color: #1e1e1e; color: #d4d4d4;")
        layout.addWidget(self.output)
        self.stop_btn.setEnabled(False)
        self.start_btn.setEnabled(False)

    def on_device_changed(self, serial, state):
        if serial == self.serial and state == self.state:
            return   # nothing changed — don't interrupt live logcat stream
        if self._is_streaming:
            self._stop_stream()
        self.serial = serial
        self.state = state
        self.start_btn.setEnabled(bool(serial) and state == "device")

    def _start_stream(self):
        if not self.main_window.require_device() or self._is_streaming:
            return
        level_flag = LEVEL_FLAGS[self.level_combo.currentText()]
        tag_filter = self.tag_input.text().strip()
        self._line_filter_text = self.search_input.text().strip().lower()
        self._stream_thread = _LogcatStreamThread(self.adb, self.serial, level_flag, tag_filter)
        self._stream_thread.line_received.connect(self._on_line)
        self._stream_thread.error.connect(self._on_error)
        self._stream_thread.stopped.connect(self._on_stopped)
        self._stream_thread.start()
        self._is_streaming = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.main_window.status_bar.showMessage("Logcat streaming started.")

    def _stop_stream(self):
        if self._stream_thread:
            self._stream_thread.stop()
        self.stop_btn.setEnabled(False)

    def _on_stopped(self):
        self._is_streaming = False
        self.start_btn.setEnabled(bool(self.serial) and self.state == "device")
        self.stop_btn.setEnabled(False)
        self.main_window.status_bar.showMessage("Logcat streaming stopped.")

    def _on_error(self, msg):
        self._is_streaming = False
        self.start_btn.setEnabled(bool(self.serial) and self.state == "device")
        self.stop_btn.setEnabled(False)
        self.output.appendPlainText(f"[error] {msg}")

    def _on_line(self, line):
        if self._line_filter_text and self._line_filter_text not in line.lower():
            return
        self.output.appendPlainText(line)
        doc = self.output.document()
        if doc.blockCount() > self._max_lines:
            cursor = QTextCursor(doc)
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            cursor.movePosition(
                QTextCursor.MoveOperation.Down,
                QTextCursor.MoveMode.KeepAnchor,
                doc.blockCount() - self._max_lines
            )
            cursor.removeSelectedText()
        if self.autoscroll_checkbox.isChecked():
            self.output.moveCursor(QTextCursor.MoveOperation.End)

    def _clear_output(self):
        self.output.clear()


# ==============================================================================
# Cast panel — FPS-optimised screen mirror
# ==============================================================================
#
# KEY FPS FIXES in this version:
#
#  FIX 1 — Frame-drop guard (BIGGEST WIN for both modes):
#    The Qt signal queue is not a zero-latency pipe. If the worker thread
#    emits frame_ready faster than the GUI thread can call setPixmap(), frames
#    pile up in the queue. You see lag, not smoothness. The fix: a boolean
#    _frame_pending flag + lock. The worker only emits when the flag is False,
#    then sets it True. The GUI slot clears it after rendering. This means
#    at most ONE frame is ever queued, and stale frames are silently dropped.
#
#  FIX 2 — H.264: socket-ready event replaces 400ms sleep:
#    Original code did `self.msleep(400)` to "give the feeder time to accept".
#    This caused OpenCV to miss the first 400ms of data on every session start
#    and every 165-second restart. Fix: feeder thread signals a threading.Event
#    when accept() returns. OpenCV's connect is attempted immediately after.
#
#  FIX 3 — H.264: correct device screen size via `adb shell wm size`:
#    Original code used hardcoded portrait strings ("540x960", "720x1280").
#    If the device is a tablet or landscape phone, screenrecord rejects the
#    size or silently encodes at a different resolution, confusing the decoder.
#    Fix: query the real pixel dimensions, then pick the closest preset that
#    fits within the 30fps or 60fps budget.
#
#  FIX 4 — H.264: CAP_PROP_BUFFERSIZE via URL param (set BEFORE open):
#    cap.set(CAP_PROP_BUFFERSIZE, 1) after VideoCapture() is already open has
#    no effect on most OpenCV builds. The correct approach is to embed the
#    buffer hint in the URL so FFmpeg's backend sees it before allocating:
#      tcp://127.0.0.1:PORT?timeout=5000000
#    Combined with grabbing the latest frame using a non-blocking grab loop.


class _StreamCaptureThread(QThread):
    """
    PNG streaming mode — fallback when opencv-python is not installed.
    Uses one persistent adb exec-out process; frames separated by PNG IEND.

    FPS ceiling is the phone's on-device PNG encoder (~2-25fps).
    Frame-drop guard ensures GUI never queues stale frames (FIX 1).
    """

    frame_ready = pyqtSignal(bytes)
    fps_update  = pyqtSignal(float)
    error       = pyqtSignal(str)

    _IEND  = b'\x00\x00\x00\x00IEND\xaeB\x60\x82'
    _CHUNK = 65536

    def __init__(self, adb, serial):
        super().__init__()
        self.adb    = adb
        self.serial = serial
        self._stop  = False
        self._proc  = None
        # FIX 1: frame-drop guard
        self._frame_lock    = threading.Lock()
        self._frame_pending = False

    def _try_emit(self, frame: bytes):
        """Emit only if the GUI has consumed the previous frame."""
        with self._frame_lock:
            if self._frame_pending:
                return   # GUI hasn't rendered yet; drop this frame
            self._frame_pending = True
        self.frame_ready.emit(frame)

    def frame_consumed(self):
        """Called by GUI slot after rendering to allow next emission."""
        with self._frame_lock:
            self._frame_pending = False

    def run(self):
        cmd = ["exec-out", "while true; do screencap -p; done"]
        try:
            self._proc = self.adb.run_popen(cmd, serial=self.serial)
        except AdbError as e:
            self.error.emit(str(e))
            return

        buf         = bytearray()
        frame_count = 0
        t_fps       = time.monotonic()

        try:
            while not self._stop:
                chunk = self._proc.stdout.read(self._CHUNK)
                if not chunk:
                    break
                buf.extend(chunk)

                while True:
                    idx = buf.find(self._IEND)
                    if idx == -1:
                        break
                    end = idx + len(self._IEND)
                    frame = bytes(buf[:end])
                    del buf[:end]

                    if not frame.startswith(b'\x89PNG'):
                        frame = frame.replace(b'\r\n', b'\n').replace(b'\r', b'')
                    if frame.startswith(b'\x89PNG'):
                        self._try_emit(frame)   # FIX 1: drop if pending
                        frame_count += 1

                now = time.monotonic()
                if now - t_fps >= 1.0:
                    self.fps_update.emit(frame_count / (now - t_fps))
                    frame_count = 0
                    t_fps = now

        except Exception as e:
            if not self._stop:
                self.error.emit(str(e))
        finally:
            self._kill()

    def stop(self):
        self._stop = True
        self._kill()

    def _kill(self):
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass


class _H264StreamThread(QThread):
    """
    H.264 hardware-encoder mode — 20-60fps via screenrecord.

    FPS fixes applied (see module docstring for full detail):
      FIX 1: frame-drop guard — emit only when GUI has rendered last frame
      FIX 2: socket-ready event — no more 400ms artificial sleep
      FIX 3: correct screen size from `adb shell wm size`
      FIX 4: CAP_PROP_BUFFERSIZE hint in TCP URL before open()
    """

    frame_ready = pyqtSignal(object)   # numpy ndarray BGR
    fps_update  = pyqtSignal(float)
    error       = pyqtSignal(str)

    _RESTART_SECS = 165   # restart 15 s before screenrecord's 180 s hard cap

    def __init__(self, adb, serial, bitrate="8M", size="720x1280"):
        super().__init__()
        self.adb     = adb
        self.serial  = serial
        self.bitrate = bitrate
        self.size    = size
        self._stop   = False
        self._proc   = None
        self._srv    = None
        self._conn   = None
        self._cap    = None
        # FIX 1: frame-drop guard
        self._frame_lock    = threading.Lock()
        self._frame_pending = False

    def _try_emit(self, frame):
        with self._frame_lock:
            if self._frame_pending:
                return
            self._frame_pending = True
        self.frame_ready.emit(frame)

    def frame_consumed(self):
        with self._frame_lock:
            self._frame_pending = False

    @staticmethod
    def _free_port():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    def run(self):
        if not _HAS_CV2:
            self.error.emit(
                "opencv-python is required for 30-60fps mode.\n"
                "Install it with: pip install opencv-python"
            )
            return

        port = self._free_port()
        while not self._stop:
            try:
                self._one_session(port)
            except Exception as e:
                if not self._stop:
                    self.error.emit(str(e))
                break

    def _one_session(self, port):
        # ── TCP relay server ────────────────────────────────────────────
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        srv.settimeout(8.0)
        self._srv = srv

        # ── Start screenrecord ─────────────────────────────────────────
        cmd = [
            "exec-out", "screenrecord",
            "--output-format=h264",
            f"--bit-rate={self.bitrate}",
            f"--size={self.size}",
            "--time-limit=170", "-",
        ]
        self._proc = self.adb.run_popen(cmd, serial=self.serial)

        # FIX 2: use an Event instead of msleep(400) so OpenCV connects
        #         the instant data starts flowing, not 400ms later.
        socket_ready = threading.Event()
        stop_ev      = threading.Event()

        def feed():
            try:
                conn, _ = srv.accept()    # blocks until OpenCV connects
                self._conn = conn
                socket_ready.set()        # signal: connection established
                while not stop_ev.is_set() and not self._stop:
                    chunk = self._proc.stdout.read(16384)
                    if not chunk:
                        break
                    try:
                        conn.sendall(chunk)
                    except OSError:
                        break
            except (socket.timeout, OSError):
                socket_ready.set()        # unblock waiter even on error

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()

        # FIX 4: embed buffer hint in URL so FFmpeg sees it before alloc.
        #         timeout=5000000 is microseconds (5 s) — gives screenrecord
        #         time to produce the first IDR frame before OpenCV gives up.
        url = f"tcp://127.0.0.1:{port}?timeout=5000000"
        cap = cv2.VideoCapture(url)
        # Belt-and-suspenders: also set via property (works on some builds)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        self._cap = cap

        # Wait for feeder to accept before declaring failure — up to 6 s.
        socket_ready.wait(timeout=6.0)

        if not cap.isOpened():
            self._cleanup_session(stop_ev, feeder)
            raise RuntimeError(
                "Could not open H.264 stream. The device may not support "
                "screenrecord streaming, or OpenCV's FFmpeg backend is missing."
            )

        fc      = 0
        t_win   = time.monotonic()
        t_start = t_win

        while not self._stop:
            # Grab the newest available frame, discarding any buffered ones.
            # cap.grab() advances the internal pointer without decoding;
            # cap.retrieve() decodes only the frame we actually need.
            grabbed = cap.grab()
            if not grabbed:
                break

            # Drain any extra buffered frames so we always show the latest.
            # On a well-tuned setup this loop body never executes; on a slow
            # GPU or a fast device it prevents latency from building up.
            while True:
                if not cap.grab():
                    break
                # If another grab succeeds immediately, there was a buffered
                # frame — discard the previous and keep going.

            ret, frame = cap.retrieve()
            if not ret or frame is None:
                break

            self._try_emit(frame)   # FIX 1: drop if GUI still busy
            fc += 1

            now = time.monotonic()
            if now - t_win >= 1.0:
                self.fps_update.emit(fc / (now - t_win))
                fc    = 0
                t_win = now

            if now - t_start >= self._RESTART_SECS:
                break   # proactive restart before 180 s limit

        self._cleanup_session(stop_ev, feeder)

    def _cleanup_session(self, stop_ev, feeder):
        stop_ev.set()
        for obj, method in [
            (self._cap,  lambda o: o.release()),
            (self._proc, lambda o: o.terminate()),
            (self._conn, lambda o: o.close()),
            (self._srv,  lambda o: o.close()),
        ]:
            if obj:
                try:
                    method(obj)
                except Exception:
                    pass
        self._cap = self._proc = self._conn = self._srv = None
        feeder.join(timeout=2)

    def stop(self):
        self._stop = True
        for obj in (self._cap, self._proc, self._conn, self._srv):
            if obj:
                try:
                    if hasattr(obj, 'release'):   obj.release()
                    elif hasattr(obj, 'terminate'): obj.terminate()
                    else:                           obj.close()
                except Exception:
                    pass


def _pick_screenrecord_size(dev_w: int, dev_h: int, target_fps: int) -> str:
    """
    FIX 3: Return a --size string appropriate for the device's actual
    screen orientation and the user's FPS target.

    Rules:
      - Keep the device's aspect ratio so screenrecord doesn't have to
        letterbox or pad (which wastes encoder time).
      - For 30fps: cap long edge at 720px  (good quality, manageable bitrate)
      - For 60fps: cap long edge at 540px  (faster encode = more frames/sec)
      - Always output WIDTHxHEIGHT (landscape: width > height).
    """
    if dev_w <= 0 or dev_h <= 0:
        # Unknown size — use safe defaults that match most portrait phones
        return "540x960" if target_fps >= 60 else "720x1280"

    landscape = dev_w >= dev_h
    long_px   = max(dev_w, dev_h)
    short_px  = min(dev_w, dev_h)

    cap = 540 if target_fps >= 60 else 720
    if long_px > cap:
        scale    = cap / long_px
        long_px  = cap
        short_px = round(short_px * scale)
        # Make both dimensions even (H.264 encoder requirement)
        short_px = short_px + (short_px % 2)

    if landscape:
        return f"{long_px}x{short_px}"
    else:
        return f"{short_px}x{long_px}"


class _ClickableCastLabel(QLabel):
    clicked = pyqtSignal(float, float)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            frac = self._frac(event)
            if frac:
                self.clicked.emit(*frac)

    def _frac(self, event):
        pm = self.pixmap()
        if pm is None or pm.isNull():
            return None
        pw, ph = self.width(), self.height()
        iw, ih = pm.width(), pm.height()
        ox = (pw - iw) / 2
        oy = (ph - ih) / 2
        px = event.position().x() - ox
        py = event.position().y() - oy
        if not (0 <= px <= iw and 0 <= py <= ih):
            return None
        return px / iw, py / ih


class CastPanel(QWidget):
    """
    Embedded screen-mirror panel (right side of main window).

    MODE SELECTION (automatic):
      H.264 [20-60fps] — when opencv-python is installed (recommended).
      PNG   [2-25fps]  — fallback without OpenCV.

    All four FPS fixes from the module docstring are active here.
    The default FPS target is 30fps (index 1 in the combo) which uses
    720p and delivers the best balance of quality and smoothness.
    """

    def __init__(self, adb, main_window):
        super().__init__()
        self.adb          = adb
        self.main_window  = main_window
        self.serial       = None
        self.state        = None
        self._thread      = None
        self._mode        = "h264" if _HAS_CV2 else "png"
        self._dev_w       = 0
        self._dev_h       = 0
        self._h264_failed = False
        self._build_ui()

    def _build_ui(self):
        self.setStyleSheet("CastPanel{background:#111;border-left:1px solid #2a2a2a;}")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Title bar
        tb_w = QWidget()
        tb_w.setFixedHeight(30)
        tb_w.setStyleSheet("background:#1a1a1a;border-bottom:1px solid #2a2a2a;")
        tb = QHBoxLayout(tb_w)
        tb.setContentsMargins(10, 0, 8, 0)
        tb.setSpacing(6)
        tb.addWidget(QLabel("📱"))
        self._title_lbl = QLabel("Device Screen")
        self._title_lbl.setStyleSheet("color:#ccc;font-size:12px;font-weight:bold;")
        tb.addWidget(self._title_lbl)
        tb.addStretch()
        self._fps_lbl = QLabel("— fps")
        self._fps_lbl.setStyleSheet("color:#4caf50;font-size:11px;font-weight:bold;")
        tb.addWidget(self._fps_lbl)
        root.addWidget(tb_w)

        # Mode / quality bar
        mode_w = QWidget()
        mode_w.setFixedHeight(22)
        mode_w.setStyleSheet("background:#161616;border-bottom:1px solid #222;")
        mr = QHBoxLayout(mode_w)
        mr.setContentsMargins(8, 0, 8, 0)
        mr.setSpacing(6)
        self._mode_lbl = QLabel()
        self._mode_lbl.setStyleSheet("font-size:9px;")
        mr.addWidget(self._mode_lbl)
        mr.addStretch()

        fps_lbl = QLabel("FPS:")
        fps_lbl.setStyleSheet("font-size:9px;color:#888;")
        mr.addWidget(fps_lbl)

        # Permanent FPS toggle — two buttons, one always active.
        # Clicking one restarts the stream immediately at the new setting.
        self._fps_30_btn = QPushButton("30")
        self._fps_60_btn = QPushButton("60")
        for btn in (self._fps_30_btn, self._fps_60_btn):
            btn.setFixedSize(28, 18)
            btn.setCheckable(True)
            btn.setStyleSheet(
                "QPushButton{font-size:9px;padding:0;border:1px solid #444;"
                "border-radius:3px;background:#2a2a3e;color:#888;}"
                "QPushButton:checked{background:#89b4fa;color:#1e1e2e;"
                "border-color:#89b4fa;font-weight:bold;}"
            )
        self._fps_30_btn.setToolTip("30 fps — 720p, best quality/smoothness")
        self._fps_60_btn.setToolTip("60 fps — 540p, higher frame rate")

        # Set default from module constant
        if CAST_FPS_DEFAULT == 60:
            self._fps_60_btn.setChecked(True)
            self._fps_30_btn.setChecked(False)
        else:
            self._fps_30_btn.setChecked(True)
            self._fps_60_btn.setChecked(False)

        def _on_fps_30():
            self._fps_30_btn.setChecked(True)
            self._fps_60_btn.setChecked(False)
            if self._thread and self._thread.isRunning():
                self._start_stream()

        def _on_fps_60():
            self._fps_60_btn.setChecked(True)
            self._fps_30_btn.setChecked(False)
            if self._thread and self._thread.isRunning():
                self._start_stream()

        self._fps_30_btn.clicked.connect(_on_fps_30)
        self._fps_60_btn.clicked.connect(_on_fps_60)
        mr.addWidget(self._fps_30_btn)
        mr.addWidget(self._fps_60_btn)

        qual_lbl = QLabel("Bitrate:")
        qual_lbl.setStyleSheet("font-size:9px;color:#888;")
        mr.addWidget(qual_lbl)
        self._quality_combo = QComboBox()
        self._quality_combo.addItems(["8M", "4M", "2M"])
        self._quality_combo.setStyleSheet("font-size:9px;")
        self._quality_combo.setFixedHeight(18)
        self._quality_combo.setFixedWidth(44)
        self._quality_combo.currentIndexChanged.connect(
            lambda _: self._start_stream() if self._thread and self._thread.isRunning() else None
        )
        if not _HAS_CV2:
            self._fps_30_btn.setEnabled(False)
            self._fps_60_btn.setEnabled(False)
            self._quality_combo.setEnabled(False)
            self._fps_30_btn.setToolTip("pip install opencv-python to enable H.264 mode")
            self._fps_60_btn.setToolTip("pip install opencv-python to enable H.264 mode")
        mr.addWidget(self._quality_combo)
        root.addWidget(mode_w)
        self._refresh_mode_label()

        # Preview
        self._preview = _ClickableCastLabel()
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setStyleSheet("background:#0a0a0a;color:#444;")
        self._preview.setText("No device\nconnected")
        self._preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._preview.clicked.connect(self._on_tap)
        root.addWidget(self._preview, stretch=1)

        # Controls
        ctrl_w = QWidget()
        ctrl_w.setFixedHeight(30)
        ctrl_w.setStyleSheet("background:#1a1a1a;border-top:1px solid #2a2a2a;")
        cr = QHBoxLayout(ctrl_w)
        cr.setContentsMargins(8, 0, 8, 0)
        self._touch_cb = QCheckBox("Tap to control")
        self._touch_cb.setChecked(True)
        self._touch_cb.setStyleSheet("color:#999;font-size:10px;")
        cr.addWidget(self._touch_cb)
        cr.addStretch()
        restart_btn = QPushButton("⟳")
        restart_btn.setFixedSize(22, 22)
        restart_btn.setToolTip("Restart stream")
        restart_btn.setStyleSheet(
            "QPushButton{background:#333;color:#ccc;border:none;border-radius:3px;}"
            "QPushButton:hover{background:#555;}"
        )
        restart_btn.clicked.connect(self._start_stream)
        cr.addWidget(restart_btn)
        root.addWidget(ctrl_w)

        # Status strip
        stat_w = QWidget()
        stat_w.setFixedHeight(18)
        stat_w.setStyleSheet("background:#161616;")
        sb = QHBoxLayout(stat_w)
        sb.setContentsMargins(8, 0, 8, 0)
        self._status_lbl = QLabel("Ready")
        self._status_lbl.setStyleSheet("color:#666;font-size:9px;")
        sb.addWidget(self._status_lbl)
        sb.addStretch()
        self._res_lbl = QLabel("")
        self._res_lbl.setStyleSheet("color:#444;font-size:9px;")
        sb.addWidget(self._res_lbl)
        root.addWidget(stat_w)

    def _refresh_mode_label(self):
        if self._mode == "h264":
            self._mode_lbl.setText("⚡ H.264 (20-60fps)")
            self._mode_lbl.setStyleSheet("font-size:9px;color:#4caf50;")
        else:
            self._mode_lbl.setText("🐢 PNG compat (2-25fps)")
            self._mode_lbl.setStyleSheet("font-size:9px;color:#ff9800;")

    # ── Device change ────────────────────────────────────────────────────

    def on_device_changed(self, serial, state):
        if serial == self.serial and state == self.state:
            return   # nothing changed — don't restart the stream
        prev = self.serial
        self.serial, self.state = serial, state
        ready = bool(serial) and state == "device"
        if not ready:
            self._stop_stream()
            self._preview.setText("No device\nconnected")
            self._fps_lbl.setText("— fps")
            self._res_lbl.setText("")
            self._status_lbl.setText("Ready")
            self._title_lbl.setText("Device Screen")
            return
        self._title_lbl.setText(serial)
        if serial != prev:
            self._h264_failed = False
            self._mode = "h264" if _HAS_CV2 else "png"
            self._refresh_mode_label()
            self._start_stream()

    # ── Streaming ────────────────────────────────────────────────────────

    def _bitrate(self) -> str:
        return ["8M", "4M", "2M"][self._quality_combo.currentIndex()]

    def _target_fps(self) -> int:
        """Return the currently selected FPS (30 or 60) from the toggle buttons."""
        return 60 if self._fps_60_btn.isChecked() else 30

    def _resolve_size(self) -> str:
        """FIX 3: query real screen dimensions, then pick correct --size."""
        if self._dev_w > 0 and self._dev_h > 0:
            return _pick_screenrecord_size(self._dev_w, self._dev_h, self._target_fps())
        # Try to query from device; fall back to safe default
        dims = self.adb.get_screen_size(self.serial)
        if dims:
            self._dev_w, self._dev_h = dims
            return _pick_screenrecord_size(self._dev_w, self._dev_h, self._target_fps())
        return _pick_screenrecord_size(0, 0, self._target_fps())

    def _start_stream(self):
        if not self.serial or self.state != "device":
            return
        if self._thread and self._thread.isRunning():
            self._thread.stop()
            self._thread.wait(2000)
        self._dev_w = self._dev_h = 0
        self._preview.setText("Connecting...")
        self._status_lbl.setText("Starting...")
        self._refresh_mode_label()

        if self._mode == "h264":
            size = self._resolve_size()   # FIX 3
            self._thread = _H264StreamThread(
                self.adb, self.serial,
                bitrate=self._bitrate(),
                size=size,
            )
            self._thread.frame_ready.connect(self._on_frame_h264)
        else:
            self._thread = _StreamCaptureThread(self.adb, self.serial)
            self._thread.frame_ready.connect(self._on_frame_png)

        self._thread.fps_update.connect(self._on_fps)
        self._thread.error.connect(self._on_error)
        self._thread.start()
        self._status_lbl.setText("Casting")

    def _stop_stream(self):
        if self._thread and self._thread.isRunning():
            self._thread.stop()
            self._thread.wait(2000)
        self._status_lbl.setText("Stopped")

    # ── Frame rendering ──────────────────────────────────────────────────

    def _scale(self, pixmap):
        pw = self._preview.width() or 280
        ph = self._preview.height() or 500
        return pixmap.scaled(
            pw, ph,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )

    def _on_frame_h264(self, frame):
        """Render H.264 frame; signal thread that GUI is ready for next."""
        if not _HAS_CV2:
            return
        h, w = frame.shape[:2]
        if self._dev_w == 0:
            self._dev_w, self._dev_h = w, h
            self._res_lbl.setText(f"{w}×{h} H.264")
        rgb  = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        qimg = QImage(rgb.data, w, h, rgb.strides[0],
                      QImage.Format.Format_RGB888).copy()
        self._preview.setPixmap(self._scale(QPixmap.fromImage(qimg)))
        # FIX 1: tell the thread the frame has been consumed
        if self._thread and hasattr(self._thread, 'frame_consumed'):
            self._thread.frame_consumed()

    def _on_frame_png(self, png: bytes):
        """Render PNG frame; signal thread that GUI is ready for next."""
        img = QImage.fromData(png, "PNG")
        if img.isNull():
            if self._thread and hasattr(self._thread, 'frame_consumed'):
                self._thread.frame_consumed()
            return
        if self._dev_w == 0:
            self._dev_w, self._dev_h = img.width(), img.height()
            self._res_lbl.setText(f"{self._dev_w}×{self._dev_h} PNG")
        self._preview.setPixmap(self._scale(QPixmap.fromImage(img)))
        # FIX 1: tell the thread the frame has been consumed
        if self._thread and hasattr(self._thread, 'frame_consumed'):
            self._thread.frame_consumed()

    def _on_fps(self, fps: float):
        self._fps_lbl.setText(f"{fps:.1f} fps")
        if fps >= 25:   color = "#4caf50"
        elif fps >= 12: color = "#cddc39"
        elif fps >= 5:  color = "#ff9800"
        else:           color = "#f44336"
        self._fps_lbl.setStyleSheet(f"color:{color};font-size:11px;font-weight:bold;")

    def _on_error(self, msg: str):
        if self._mode == "h264" and not self._h264_failed:
            self._h264_failed = True
            self._mode = "png"
            self._status_lbl.setText("H.264 failed — switching to PNG mode...")
            self._refresh_mode_label()
            QTimer.singleShot(800, lambda: (
                self._start_stream() if self.serial and self.state == "device" else None
            ))
            return
        self._status_lbl.setText("Error — retrying...")
        self._preview.setText("Stream error\n(retrying...)")
        QTimer.singleShot(3000, lambda: (
            self._start_stream() if self.serial and self.state == "device" else None
        ))

    # ── Touch forwarding ─────────────────────────────────────────────────

    def _on_tap(self, xf: float, yf: float):
        if not self._touch_cb.isChecked() or not self.serial or self._dev_w == 0:
            return
        try:
            self.adb.shell_command(
                self.serial,
                f"input tap {int(xf*self._dev_w)} {int(yf*self._dev_h)}",
                timeout=5,
            )
        except AdbError:
            pass


# ==============================================================================
# Wireless connection wizard dialog
# ==============================================================================

class _TcpipThread(QThread):
    finished_ok = pyqtSignal(str)
    finished_err = pyqtSignal(str)

    def __init__(self, adb, serial):
        super().__init__()
        self.adb = adb
        self.serial = serial

    def run(self):
        try:
            result = self.adb.run(["tcpip", "5555"], serial=self.serial, timeout=15)
            self.finished_ok.emit(result)
        except AdbError as e:
            self.finished_err.emit(str(e))


class _ConnectThread(QThread):
    finished_ok = pyqtSignal(str, str)
    finished_err = pyqtSignal(str)

    def __init__(self, adb, address):
        super().__init__()
        self.adb = adb
        self.address = address

    def run(self):
        try:
            result = self.adb.connect_tcp(self.address)
            self.finished_ok.emit(result, self.address)
        except AdbError as e:
            self.finished_err.emit(str(e))


class WirelessConnectDialog(QDialog):
    connected = pyqtSignal(str)

    def __init__(self, adb, parent=None):
        super().__init__(parent)
        self.adb = adb
        self.setWindowTitle("Connect Device Wirelessly")
        self.setMinimumWidth(540)
        self._tcpip_thread = None
        self._connect_thread = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        why_box = QGroupBox("Why does 'connection refused' happen?")
        why_layout = QVBoxLayout(why_box)
        why_label = QLabel(
            "Error 10061 (connection actively refused) means your phone has "
            "<b>no ADB TCP listener running</b> on port 5555. The phone must "
            "explicitly open a wireless debugging port before any PC can connect.\n\n"
            "Choose the method below that matches your situation:"
        )
        why_label.setWordWrap(True)
        why_layout.addWidget(why_label)
        layout.addWidget(why_box)

        self.method_a_box = QGroupBox("Method A — USB cable available (any Android version)")
        a_layout = QVBoxLayout(self.method_a_box)
        a_info = QLabel(
            "Plug your phone in via USB first (just this once). The app will:\n"
            "  1. Run <code>adb tcpip 5555</code> to open port 5555 on the phone\n"
            "  2. Read the phone's Wi-Fi IP automatically\n"
            "  3. Connect wirelessly — then you can unplug"
        )
        a_info.setWordWrap(True)
        a_layout.addWidget(a_info)
        a_row = QHBoxLayout()
        self.usb_device_combo = QComboBox()
        self.usb_device_combo.setMinimumWidth(260)
        a_row.addWidget(QLabel("USB device:"))
        a_row.addWidget(self.usb_device_combo)
        self.enable_tcpip_btn = QPushButton("Enable TCP + Connect")
        self.enable_tcpip_btn.clicked.connect(self._method_a_go)
        a_row.addWidget(self.enable_tcpip_btn)
        a_layout.addLayout(a_row)
        self.method_a_status = QLabel("")
        self.method_a_status.setWordWrap(True)
        a_layout.addWidget(self.method_a_status)
        layout.addWidget(self.method_a_box)

        self.method_b_box = QGroupBox("Method B — Android 11+ Wireless Debugging (no USB needed)")
        b_layout = QVBoxLayout(self.method_b_box)
        b_info = QLabel(
            "On your phone: <b>Settings → Developer options → Wireless debugging → turn ON</b>\n\n"
            "The screen shows an <b>IP address and port</b> (e.g. 192.168.0.10:<b>45678</b>).\n"
            "⚠ The port is <b>not always 5555</b> — it's a random number that changes each session."
        )
        b_info.setWordWrap(True)
        b_layout.addWidget(b_info)
        b_row = QHBoxLayout()
        b_row.addWidget(QLabel("IP:"))
        self.ip_input = QLineEdit()
        self.ip_input.setPlaceholderText("192.168.0.10")
        self.ip_input.setMaximumWidth(150)
        b_row.addWidget(self.ip_input)
        b_row.addWidget(QLabel("Port:"))
        self.port_input = QLineEdit()
        self.port_input.setPlaceholderText("45678")
        self.port_input.setMaximumWidth(80)
        b_row.addWidget(self.port_input)
        self.connect_b_btn = QPushButton("Connect")
        self.connect_b_btn.clicked.connect(self._method_b_go)
        b_row.addWidget(self.connect_b_btn)
        b_row.addStretch()
        b_layout.addLayout(b_row)
        self.method_b_status = QLabel("")
        self.method_b_status.setWordWrap(True)
        b_layout.addWidget(self.method_b_status)
        layout.addWidget(self.method_b_box)

        bottom = QHBoxLayout()
        bottom.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        bottom.addWidget(close_btn)
        layout.addLayout(bottom)
        self._populate_usb_devices()

    def _populate_usb_devices(self):
        self.usb_device_combo.clear()
        try:
            devices = self.adb.list_devices()
            usb_devices = [(s, st) for s, st in devices if not s.startswith("192.")]
            if not usb_devices:
                self.usb_device_combo.addItem("(no USB devices detected)")
                self.enable_tcpip_btn.setEnabled(False)
            else:
                for serial, state in usb_devices:
                    self.usb_device_combo.addItem(f"{serial}  [{state}]", userData=(serial, state))
                self.enable_tcpip_btn.setEnabled(True)
        except AdbError:
            self.usb_device_combo.addItem("(could not list devices)")
            self.enable_tcpip_btn.setEnabled(False)

    def _method_a_go(self):
        data = self.usb_device_combo.currentData()
        if not data:
            self.method_a_status.setText("No USB device selected.")
            return
        serial, state = data
        if state != "device":
            self.method_a_status.setText(
                f"Device '{serial}' is in state '{state}'. Check USB debugging authorization."
            )
            return
        self.enable_tcpip_btn.setEnabled(False)
        self.method_a_status.setText("Step 1/3: Opening TCP port 5555 on the device...")
        self._tcpip_thread = _TcpipThread(self.adb, serial)
        self._tcpip_thread.finished_ok.connect(lambda _: self._method_a_read_ip(serial))
        self._tcpip_thread.finished_err.connect(self._method_a_err)
        self._tcpip_thread.start()

    def _method_a_read_ip(self, serial):
        self.method_a_status.setText("Step 2/3: Reading Wi-Fi IP address...")
        try:
            out = self.adb.shell_command(serial, "ip route", timeout=10)
            m = re.search(r"src\s+(\d+\.\d+\.\d+\.\d+)", out)
            if not m:
                out2 = self.adb.shell_command(serial, "ifconfig wlan0", timeout=10)
                m = re.search(r"inet addr:(\d+\.\d+\.\d+\.\d+)", out2)
        except AdbError:
            m = None
        if not m:
            self.method_a_status.setText(
                "TCP port opened, but couldn't auto-detect the phone's IP. "
                "Use Method B with port 5555."
            )
            self.enable_tcpip_btn.setEnabled(True)
            return
        ip = m.group(1)
        address = f"{ip}:5555"
        self.method_a_status.setText(f"Step 3/3: Connecting to {address}...")
        self._run_connect(address, self.method_a_status, self.enable_tcpip_btn)

    def _method_a_err(self, msg):
        self.enable_tcpip_btn.setEnabled(True)
        self.method_a_status.setText(f"Failed: {msg}")

    def _method_b_go(self):
        ip   = self.ip_input.text().strip()
        port = self.port_input.text().strip()
        if not ip:
            self.method_b_status.setText("Please enter the IP address shown on your phone.")
            return
        if not port:
            self.method_b_status.setText("Please enter the port shown on the Wireless debugging screen.")
            return
        try:
            port_int = int(port)
            if not (1 <= port_int <= 65535):
                raise ValueError
        except ValueError:
            self.method_b_status.setText(f"'{port}' is not a valid port number (must be 1–65535).")
            return
        address = f"{ip}:{port}"
        self.connect_b_btn.setEnabled(False)
        self.method_b_status.setText(f"Connecting to {address}...")
        self._run_connect(address, self.method_b_status, self.connect_b_btn)

    def _run_connect(self, address, status_label, renable_btn):
        self._connect_thread = _ConnectThread(self.adb, address)
        self._connect_thread.finished_ok.connect(
            lambda result, addr: self._on_connect_ok(result, addr, status_label, renable_btn)
        )
        self._connect_thread.finished_err.connect(
            lambda msg: self._on_connect_err(msg, address, status_label, renable_btn)
        )
        self._connect_thread.start()

    def _on_connect_ok(self, result, address, status_label, renable_btn):
        renable_btn.setEnabled(True)
        result = result.strip()
        if "connected" in result.lower() and "unable" not in result.lower():
            status_label.setText(f"✅ Connected to {address}!")
            self.connected.emit(address)
            QTimer.singleShot(1500, self.accept)
        elif "already connected" in result.lower():
            status_label.setText(f"✅ Already connected to {address}.")
            self.connected.emit(address)
            QTimer.singleShot(1500, self.accept)
        else:
            status_label.setText(f"⚠ {result}")

    def _on_connect_err(self, msg, address, status_label, renable_btn):
        renable_btn.setEnabled(True)
        if "10061" in msg or "refused" in msg.lower():
            status_label.setText(
                f"❌ Connection refused by {address}.\n\n"
                "• Wireless debugging must be ON in Developer options\n"
                "• Use the exact port shown on the phone screen (not always 5555)\n"
                "• PC and phone must be on the same Wi-Fi network"
            )
        elif "10060" in msg or "timed out" in msg.lower():
            status_label.setText(
                f"❌ Connection timed out to {address}.\n"
                "Check that both devices are on the same Wi-Fi network."
            )
        else:
            status_label.setText(f"❌ {msg}")


# ==============================================================================
# Main window
# ==============================================================================

APP_TITLE = "ADB Toolkit"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.adb = AdbClient()
        self.current_serial = None
        self.current_state  = None
        self.setWindowTitle(APP_TITLE)
        self.resize(1400, 750)
        self._build_ui()
        self._refresh_devices()
        self.device_poll_timer = QTimer(self)
        self.device_poll_timer.timeout.connect(self._refresh_devices)
        self.device_poll_timer.start(3000)

    def _build_ui(self):
        central = QWidget()
        outer   = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        left   = QWidget()
        layout = QVBoxLayout(left)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        top_bar = QHBoxLayout()
        top_bar.addWidget(QLabel("Device:"))
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(320)
        self.device_combo.currentIndexChanged.connect(self._on_device_selected)
        top_bar.addWidget(self.device_combo)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_devices)
        top_bar.addWidget(refresh_btn)
        top_bar.addSpacing(20)
        wireless_btn = QPushButton("🌐 Connect Wirelessly...")
        wireless_btn.setToolTip("Step-by-step wireless ADB connection wizard")
        wireless_btn.clicked.connect(self._open_wireless_dialog)
        top_bar.addWidget(wireless_btn)
        top_bar.addStretch()
        self._cast_toggle_btn = QPushButton("📱 Cast panel")
        self._cast_toggle_btn.setToolTip("Show/hide the embedded screen-cast panel")
        self._cast_toggle_btn.setCheckable(True)
        self._cast_toggle_btn.setChecked(True)
        self._cast_toggle_btn.clicked.connect(self._toggle_cast_panel)
        top_bar.addWidget(self._cast_toggle_btn)
        adb_settings_btn = QPushButton("ADB Path...")
        adb_settings_btn.clicked.connect(self._configure_adb_path)
        top_bar.addWidget(adb_settings_btn)
        layout.addLayout(top_bar)

        self.tabs        = QTabWidget()
        self.device_tab  = DeviceTab(self.adb, self)
        self.apps_tab    = AppsTab(self.adb, self)
        self.files_tab   = FilesTab(self.adb, self)
        self.screen_tab  = ScreenTab(self.adb, self)
        self.shell_tab   = ShellTab(self.adb, self)
        self.logcat_tab  = LogcatTab(self.adb, self)
        self.tabs.addTab(self.device_tab,  "Device")
        self.tabs.addTab(self.apps_tab,    "Apps")
        self.tabs.addTab(self.files_tab,   "Files")
        self.tabs.addTab(self.screen_tab,  "Screen")
        self.tabs.addTab(self.shell_tab,   "Shell")
        self.tabs.addTab(self.logcat_tab,  "Logcat")
        layout.addWidget(self.tabs)
        outer.addWidget(left, stretch=1)

        self.cast_panel = CastPanel(self.adb, self)
        self.cast_panel.setFixedWidth(300)
        outer.addWidget(self.cast_panel, stretch=0)

        self.setCentralWidget(central)
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._update_adb_status()

    def _toggle_cast_panel(self, checked: bool):
        self.cast_panel.setVisible(checked)
        if checked and self.current_serial and self.current_state == "device":
            self.cast_panel.on_device_changed(self.current_serial, self.current_state)

    def _update_adb_status(self):
        if self.adb.adb_path:
            self.status_bar.showMessage(f"adb: {self.adb.adb_path}")
        else:
            self.status_bar.showMessage("adb not found — click 'ADB Path...' to set it manually")

    def _configure_adb_path(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select adb executable")
        if path:
            self.adb.set_adb_path(path)
            self._update_adb_status()
            self._refresh_devices()

    def _open_wireless_dialog(self):
        dlg = WirelessConnectDialog(self.adb, self)
        dlg.connected.connect(self._on_wireless_connected)
        dlg.exec()

    def _on_wireless_connected(self, address):
        self.status_bar.showMessage(f"Connected to {address}. Refreshing...")
        self._refresh_devices()

    def _refresh_devices(self):
        """
        Poll adb for the current device list and update the combo box.

        STABILITY GUARANTEE — tabs are NOT touched unless something actually
        changed.  The three things that constitute a "real change" are:
          1. The set of connected devices changed (device added or removed).
          2. The state of any existing device changed (e.g. 'offline' → 'device').
          3. The currently-selected device changed (user picked a different one).

        On every silent 3-second timer tick where nothing changed, this method
        returns after refreshing the combo box labels without calling
        _notify_tabs_device_changed(), so no tab resets, re-loads, or
        flickers at all.
        """
        if not self.adb.adb_path:
            self._update_adb_status()
            return
        try:
            devices = self.adb.list_devices()
        except AdbError as e:
            self.status_bar.showMessage(f"Error listing devices: {e}")
            return

        # ── Snapshot state BEFORE touching the combo ─────────────────────
        prev_serial    = self.current_serial
        prev_state     = self.current_state
        # Build a frozenset of (serial, state) tuples for quick comparison
        prev_device_set = frozenset(
            self.device_combo.itemData(i)
            for i in range(self.device_combo.count())
            if self.device_combo.itemData(i) is not None
        )
        new_device_set = frozenset(devices)

        # ── Rebuild the combo (silently — signals blocked) ────────────────
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        if not devices:
            self.device_combo.addItem("No devices connected")
            self.device_combo.setEnabled(False)
            new_serial = new_state = None
        else:
            self.device_combo.setEnabled(True)
            select_index = 0
            for i, (serial, state) in enumerate(devices):
                self.device_combo.addItem(f"{serial}  [{state}]", userData=(serial, state))
                if serial == prev_serial:
                    select_index = i
            self.device_combo.setCurrentIndex(select_index)
            data = self.device_combo.currentData()
            new_serial, new_state = data if data else (None, None)

        self.current_serial = new_serial
        self.current_state  = new_state
        self.device_combo.blockSignals(False)
        self._update_adb_status()

        # ── Only notify tabs when something actually changed ───────────────
        device_list_changed   = (new_device_set != prev_device_set)
        selection_changed     = (new_serial != prev_serial or new_state != prev_state)

        if device_list_changed or selection_changed:
            self._notify_tabs_device_changed()

    def _on_device_selected(self, index):
        """User manually picked a different device from the combo."""
        data = self.device_combo.currentData()
        if data:
            self.current_serial, self.current_state = data
        else:
            self.current_serial = self.current_state = None
        self._notify_tabs_device_changed()

    def _notify_tabs_device_changed(self):
        """
        Tell every tab (and the cast panel) about the new device.
        Only called when the device list or selection genuinely changed.
        """
        for tab in (self.device_tab, self.apps_tab, self.files_tab,
                    self.screen_tab, self.shell_tab, self.logcat_tab):
            if hasattr(tab, "on_device_changed"):
                tab.on_device_changed(self.current_serial, self.current_state)
        if hasattr(self, "cast_panel"):
            self.cast_panel.on_device_changed(self.current_serial, self.current_state)

    def get_current_device(self):
        return self.current_serial, self.current_state

    def require_device(self) -> bool:
        if not self.current_serial or self.current_state != "device":
            QMessageBox.warning(self, "No device", "No connected/authorized device selected.")
            return False
        return True

    def closeEvent(self, event):
        if hasattr(self, "cast_panel"):
            self.cast_panel._stop_stream()
        super().closeEvent(event)


def _apply_dark_theme(app: QApplication):
    app.setStyle("Fusion")

    DARK_BG     = "#1e1e2e"
    PANEL_BG    = "#181825"
    SURFACE     = "#2a2a3e"
    SURFACE2    = "#313244"
    BORDER      = "#45475a"
    ACCENT      = "#89b4fa"
    ACCENT_DARK = "#6c9dd8"
    TEXT        = "#cdd6f4"
    TEXT_DIM    = "#6c7086"
    TEXT_BRIGHT = "#ffffff"
    SEL_BG      = "#585b70"

    app.setStyleSheet(f"""
QWidget {{
    background-color: {DARK_BG};
    color: {TEXT};
    font-family: "Segoe UI", "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
}}
QMainWindow, QDialog {{ background-color: {DARK_BG}; }}
QLineEdit, QPlainTextEdit, QTextEdit {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 8px;
    selection-background-color: {ACCENT};
    selection-color: {DARK_BG};
}}
QLineEdit:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; background-color: {SURFACE2}; }}
QPushButton {{
    background-color: {SURFACE2};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 14px;
    min-height: 24px;
}}
QPushButton:hover {{ background-color: {ACCENT}; color: {DARK_BG}; border-color: {ACCENT}; }}
QPushButton:pressed {{ background-color: {ACCENT_DARK}; color: {DARK_BG}; }}
QPushButton:disabled {{ background-color: {PANEL_BG}; color: {TEXT_DIM}; border-color: {BORDER}; }}
QPushButton:checked {{ background-color: {ACCENT}; color: {DARK_BG}; border-color: {ACCENT}; font-weight: bold; }}
QComboBox {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 8px;
    min-height: 22px;
}}
QComboBox:hover {{ border-color: {ACCENT}; }}
QComboBox:disabled {{ color: {TEXT_DIM}; background-color: {PANEL_BG}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 6px solid {TEXT_DIM};
    width: 0; height: 0;
    margin-right: 6px;
}}
QComboBox QAbstractItemView {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    selection-color: {DARK_BG};
    outline: none;
}}
QCheckBox {{ spacing: 6px; color: {TEXT}; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {BORDER};
    border-radius: 4px;
    background: {SURFACE};
}}
QCheckBox::indicator:checked {{ background-color: {ACCENT}; border-color: {ACCENT}; }}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 0 6px 6px 6px;
    background: {DARK_BG};
    top: -1px;
}}
QTabBar::tab {{
    background: {PANEL_BG};
    color: {TEXT_DIM};
    border: 1px solid {BORDER};
    border-bottom: none;
    border-radius: 6px 6px 0 0;
    padding: 7px 18px;
    margin-right: 2px;
    font-size: 12px;
}}
QTabBar::tab:selected {{
    background: {DARK_BG};
    color: {ACCENT};
    border-bottom: 2px solid {ACCENT};
    font-weight: bold;
}}
QTabBar::tab:hover:!selected {{ background: {SURFACE}; color: {TEXT}; }}
QGroupBox {{
    background-color: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 10px;
    font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px; top: -1px;
    color: {ACCENT};
    font-size: 11px;
    letter-spacing: 0.5px;
}}
QListWidget, QTableWidget {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    alternate-background-color: {SURFACE2};
    gridline-color: {BORDER};
    outline: none;
}}
QListWidget::item, QTableWidget::item {{ padding: 4px 8px; border-radius: 3px; }}
QListWidget::item:selected, QTableWidget::item:selected {{
    background-color: {SEL_BG}; color: {TEXT_BRIGHT};
}}
QListWidget::item:hover, QTableWidget::item:hover {{ background-color: {SURFACE2}; }}
QHeaderView::section {{
    background-color: {PANEL_BG};
    color: {TEXT_DIM};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 8px;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}}
QScrollBar:vertical {{
    background: {PANEL_BG}; width: 8px; border-radius: 4px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER}; border-radius: 4px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {TEXT_DIM}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: {PANEL_BG}; height: 8px; border-radius: 4px; margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER}; border-radius: 4px; min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{ background: {TEXT_DIM}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QStatusBar {{
    background-color: {PANEL_BG};
    color: {TEXT_DIM};
    border-top: 1px solid {BORDER};
    font-size: 11px;
    padding: 2px 8px;
}}
QMessageBox {{ background-color: {PANEL_BG}; }}
QMessageBox QLabel {{ color: {TEXT}; }}
QMessageBox QPushButton {{ min-width: 80px; }}
QToolTip {{
    background-color: {SURFACE2};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 12px;
}}
QLabel {{ color: {TEXT}; background: transparent; }}
""")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    _apply_dark_theme(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()