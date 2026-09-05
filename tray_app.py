"""
Roblox Asset Proxy (rbx-proxy) - System Tray Application
Runs quietly in Windows System Tray (notification area) with zero console window.
Provides dark-themed GUI settings, live traffic monitor, auto-start, and embedded 1-click DNS fix.
"""

import asyncio
import base64
import ctypes
import json
import os
import queue
import re
import socket
import ssl
import sys
import threading
import time
import urllib.request
import winreg

from PyQt6.QtCore import Qt, QTimer, QRect
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap, QBrush
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget
)

# Configuration defaults
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8989
PAC_PATH = "/proxy.pac"
APP_NAME = "RobloxProxy"
REG_RUN_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_SETTINGS_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_REFRESH = 37

ROBLOX_DOMAINS = [
    "rbxcdn.com",
    "roblox.com",
    "arkoselabs.com"
]

# Thread-safe queue for log events from proxy to GUI
log_queue = queue.Queue()


def is_hosts_dns_fixed() -> bool:
    """Checks if tr.rbxcdn.com is mapped in Windows hosts file."""
    try:
        hosts_path = os.path.expandvars(r"%WINDIR%\System32\drivers\etc\hosts")
        with open(hosts_path, "r", encoding="utf-8", errors="ignore") as f:
            return "tr.rbxcdn.com" in f.read()
    except Exception:
        return False


def apply_hosts_dns_fix():
    """Applies DNS fix via Base64-encoded elevated PowerShell command. Immune to quoting/path/encoding issues."""
    ps_code = """
$hosts = [System.IO.Path]::Combine($env:WINDIR, 'System32\\drivers\\etc\\hosts')
$lines = "`r`n# Roblox Asset CDN DNS Fix`r`n108.157.229.101 tr.rbxcdn.com`r`n108.157.229.27 tr.rbxcdn.com`r`n108.157.229.26 tr.rbxcdn.com`r`n108.157.229.100 tr.rbxcdn.com`r`n"
$current = [System.IO.File]::ReadAllText($hosts)
if (-not $current.Contains('tr.rbxcdn.com')) {
    [System.IO.File]::AppendAllText($hosts, $lines, [System.Text.Encoding]::UTF8)
    Write-Host "[OK] Added tr.rbxcdn.com to hosts successfully!" -ForegroundColor Green
} else {
    Write-Host "[OK] tr.rbxcdn.com is already present in hosts." -ForegroundColor Yellow
}
ipconfig /flushdns
Start-Sleep -Milliseconds 1200
"""
    b64 = base64.b64encode(ps_code.encode("utf-16le")).decode("ascii")
    ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        "powershell.exe",
        f"-NoProfile -EncodedCommand {b64}",
        None,
        1 # SW_SHOWNORMAL (displays quick window confirming [OK])
    )


class WindowsProxyManager:
    """Manages Windows Internet Settings (PAC and System Proxy) cleanly."""

    def __init__(self, host: str, port: int, use_system_proxy: bool = True):
        self.host = host
        self.port = port
        self.pac_url = f"http://{host}:{port}{PAC_PATH}"
        self.use_system_proxy = use_system_proxy

        self.original_pac = None
        self.original_proxy_enable = None
        self.original_proxy_server = None
        self.is_set = False

    def enable(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_SETTINGS_PATH, 0, winreg.KEY_READ) as key:
                try:
                    self.original_pac, _ = winreg.QueryValueEx(key, "AutoConfigURL")
                except FileNotFoundError:
                    self.original_pac = None
                try:
                    self.original_proxy_enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
                except FileNotFoundError:
                    self.original_proxy_enable = 0
                try:
                    self.original_proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
                except FileNotFoundError:
                    self.original_proxy_server = None

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_SETTINGS_PATH, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "AutoConfigURL", 0, winreg.REG_SZ, self.pac_url)

                if self.use_system_proxy:
                    winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
                    winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, f"{self.host}:{self.port}")
                    winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, "*.local;<local>;<-loopback>")

            self._refresh_wininet()
            self.is_set = True
        except Exception as e:
            print(f"[WARN] Failed to set Windows proxy: {e}")

    def disable(self):
        if not self.is_set:
            return
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_SETTINGS_PATH, 0, winreg.KEY_SET_VALUE) as key:
                if self.original_pac:
                    winreg.SetValueEx(key, "AutoConfigURL", 0, winreg.REG_SZ, self.original_pac)
                else:
                    try:
                        winreg.DeleteValue(key, "AutoConfigURL")
                    except FileNotFoundError:
                        pass

                if self.original_proxy_enable is not None:
                    winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, self.original_proxy_enable)
                else:
                    winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)

                if self.original_proxy_server:
                    winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, self.original_proxy_server)
                else:
                    try:
                        winreg.DeleteValue(key, "ProxyServer")
                    except FileNotFoundError:
                        pass

            self._refresh_wininet()
            self.is_set = False
        except Exception as e:
            print(f"[WARN] Failed to restore Windows proxy: {e}")

    def _refresh_wininet(self):
        try:
            ctypes.windll.wininet.InternetSetOptionW(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
            ctypes.windll.wininet.InternetSetOptionW(0, INTERNET_OPTION_REFRESH, 0, 0)
        except Exception:
            pass


class DoHResolver:
    """DNS-over-HTTPS resolver bypassing ISP DNS blocks and GeoIP restrictions."""

    def __init__(self):
        self.cache = {}
        self.ssl_ctx = ssl.create_default_context()

    async def resolve(self, domain: str) -> list[str]:
        now = time.time()
        if domain in self.cache:
            ips, exp = self.cache[domain]
            if now < exp and ips:
                return ips

        ips = await self._query_doh(domain)
        if not ips:
            try:
                loop = asyncio.get_running_loop()
                infos = await loop.getaddrinfo(domain, 443, family=socket.AF_INET)
                ips = [item[4][0] for item in infos]
            except Exception:
                ips = []

        if ips:
            self.cache[domain] = (ips, now + 300)
        return ips

    async def _query_doh(self, domain: str) -> list[str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._sync_query_doh, domain)

    def _sync_query_doh(self, domain: str) -> list[str]:
        endpoints = [
            (
                "8.8.8.8",
                f"https://8.8.8.8/resolve?name={domain}&type=A&edns_client_subnet=0.0.0.0/0",
                "dns.google"
            ),
            (
                "1.1.1.1",
                f"https://1.1.1.1/dns-query?name={domain}&type=A",
                "cloudflare-dns.com"
            )
        ]

        for ip_addr, url, host_header in endpoints:
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "Host": host_header,
                        "Accept": "application/dns-json",
                        "User-Agent": "RobloxProxy/1.0"
                    }
                )
                with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=3.5) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        answers = data.get("Answer", [])
                        a_records = [ans["data"] for ans in answers if ans.get("type") == 1]
                        if a_records:
                            return a_records
                        cnames = [ans["data"].rstrip(".") for ans in answers if ans.get("type") == 5]
                        for cname in cnames:
                            nested = self._sync_query_doh(cname)
                            if nested:
                                return nested
            except Exception:
                continue

        return []


class RobloxProxyServer:
    """Async HTTP / HTTPS CONNECT Proxy server."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, enable_dpi_bypass=False, use_system_proxy=True):
        self.host = host
        self.port = port
        self.enable_dpi_bypass = enable_dpi_bypass
        self.use_system_proxy = use_system_proxy
        self.resolver = DoHResolver()
        self.pac_manager = WindowsProxyManager(host, port, use_system_proxy)
        self.server = None
        self._is_running = False

    def is_roblox_domain(self, host: str) -> bool:
        host_clean = host.split(":")[0].lower()
        return any(host_clean == d or host_clean.endswith("." + d) for d in ROBLOX_DOMAINS)

    def generate_pac_script(self) -> str:
        return f"""// Roblox Asset Proxy Auto-Configuration (PAC) Script
function FindProxyForURL(url, host) {{
    if (
        shExpMatch(host, "*.rbxcdn.com") ||
        shExpMatch(host, "*.roblox.com") ||
        host === "rbxcdn.com" ||
        host === "roblox.com" ||
        shExpMatch(host, "*.arkoselabs.com")
    ) {{
        return "PROXY {self.host}:{self.port}; DIRECT";
    }}
    return "DIRECT";
}}
"""

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            initial_line = await reader.readline()
            if not initial_line:
                writer.close()
                await writer.wait_closed()
                return

            line_str = initial_line.decode("iso-8859-1", errors="ignore").strip()
            parts = line_str.split()
            if len(parts) < 2:
                writer.close()
                await writer.wait_closed()
                return

            method, target = parts[0].upper(), parts[1]

            # 1. Serve PAC Script
            if method == "GET" and (target == PAC_PATH or target.startswith(PAC_PATH + "?")):
                while True:
                    h = await reader.readline()
                    if not h or h == b"\r\n":
                        break
                pac_content = self.generate_pac_script().encode("utf-8")
                response = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/x-ns-proxy-autoconfig\r\n"
                    b"Content-Length: " + str(len(pac_content)).encode() + b"\r\n"
                    b"Connection: close\r\n\r\n" + pac_content
                )
                writer.write(response)
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            # 2. HTTPS CONNECT Tunnel
            if method == "CONNECT":
                await self.handle_connect(reader, writer, target)
                return

            # 3. Plain HTTP Proxy
            if method in ["GET", "POST", "HEAD", "OPTIONS"]:
                await self.handle_plain_http(reader, writer, method, target, initial_line)
                return

        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def handle_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: str):
        headers = []
        while True:
            line = await reader.readline()
            if not line or line == b"\r\n":
                break
            headers.append(line)

        if ":" in target:
            dest_host, dest_port_str = target.split(":", 1)
            dest_port = int(dest_port_str)
        else:
            dest_host = target
            dest_port = 443

        is_rbx = self.is_roblox_domain(dest_host)

        if is_rbx:
            ips = await self.resolver.resolve(dest_host)
            connect_host = ips[0] if ips else dest_host
        else:
            connect_host = dest_host

        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(connect_host, dest_port),
                timeout=5.0
            )
        except Exception:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            return

        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        # If safe DPI bypass is enabled, read exact full TLS record and split
        if is_rbx and self.enable_dpi_bypass:
            try:
                header = await asyncio.wait_for(reader.read(5), timeout=5.0)
                if len(header) == 5 and header[0] == 0x16:
                    rec_len = (header[3] << 8) | header[4]
                    payload = await asyncio.wait_for(reader.readexactly(rec_len), timeout=5.0)
                    full_packet = header + payload
                    remote_writer.write(full_packet[:2])
                    await remote_writer.drain()
                    await asyncio.sleep(0.015)
                    remote_writer.write(full_packet[2:])
                    await remote_writer.drain()
                elif header:
                    remote_writer.write(header)
                    await remote_writer.drain()
            except Exception:
                pass

        t0 = time.time()
        bytes_transferred = await self.bidirectional_pipe(reader, writer, remote_reader, remote_writer)
        duration = time.time() - t0

        if is_rbx and bytes_transferred > 0:
            log_queue.put((
                time.strftime("%H:%M:%S"),
                dest_host,
                f"{bytes_transferred / 1024:.1f} KB",
                f"{duration:.2f}s"
            ))

    async def handle_plain_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, method: str, target: str, initial_line: bytes):
        headers = []
        host = ""
        while True:
            line = await reader.readline()
            if not line or line == b"\r\n":
                break
            headers.append(line)
            if line.lower().startswith(b"host:"):
                host = line.split(b":", 1)[1].strip().decode("iso-8859-1")

        if not host and target.startswith("http://"):
            match = re.match(r"http://([^/]+)", target)
            if match:
                host = match.group(1)

        if not host:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            return

        port = 80
        if ":" in host:
            host_only, p_str = host.split(":", 1)
            port = int(p_str)
        else:
            host_only = host

        is_rbx = self.is_roblox_domain(host_only)
        if is_rbx:
            ips = await self.resolver.resolve(host_only)
            connect_host = ips[0] if ips else host_only
        else:
            connect_host = host_only

        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(connect_host, port),
                timeout=5.0
            )
            remote_writer.write(initial_line)
            for h in headers:
                remote_writer.write(h)
            remote_writer.write(b"\r\n")
            await remote_writer.drain()

            bytes_transferred = await self.bidirectional_pipe(reader, writer, remote_reader, remote_writer)
            if is_rbx and bytes_transferred > 0:
                log_queue.put((
                    time.strftime("%H:%M:%S"),
                    host_only,
                    f"{bytes_transferred / 1024:.1f} KB",
                    "HTTP"
                ))
        except Exception:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()

    async def bidirectional_pipe(self, r1: asyncio.StreamReader, w1: asyncio.StreamWriter,
                                 r2: asyncio.StreamReader, w2: asyncio.StreamWriter) -> int:
        total_bytes = 0

        async def pipe(reader, writer):
            nonlocal total_bytes
            try:
                while True:
                    data = await reader.read(16384)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
                    total_bytes += len(data)
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                except Exception:
                    pass

        await asyncio.gather(pipe(r1, w2), pipe(r2, w1))
        return total_bytes

    async def start(self):
        self.server = await asyncio.start_server(self.handle_client, self.host, self.port)
        self.pac_manager.enable()
        self._is_running = True
        async with self.server:
            await self.server.serve_forever()

    def stop(self):
        if not self._is_running:
            return
        self._is_running = False
        self.pac_manager.disable()
        if self.server:
            self.server.close()


class ProxyThread(threading.Thread):
    """Background thread running the asyncio proxy event loop."""

    def __init__(self, server: RobloxProxyServer):
        super().__init__(daemon=True)
        self.server = server
        self.loop = None

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.server.start())
        except Exception:
            pass

    def stop(self):
        if self.server:
            self.server.stop()
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)


class SettingsDialog(QDialog):
    """Modern dark-themed settings and live traffic monitor."""

    def __init__(self, proxy_server: RobloxProxyServer, parent=None):
        super().__init__(parent)
        self.proxy_server = proxy_server
        self.setWindowTitle("Roblox Asset Proxy — Настройки и Журнал")
        self.resize(650, 520)
        self.setStyleSheet("""
            QDialog {
                background-color: #1a1a1a;
                color: #ffffff;
                font-family: 'Segoe UI', sans-serif;
            }
            QLabel {
                color: #e0e0e0;
                font-size: 13px;
            }
            QTableWidget {
                background-color: #242424;
                border: 1px solid #333333;
                border-radius: 8px;
                color: #ffffff;
                gridline-color: #2e2e2e;
                font-size: 12px;
            }
            QTableWidget::item {
                padding: 6px;
            }
            QTableWidget::item:selected {
                background-color: #e6232d;
                color: #ffffff;
            }
            QHeaderView::section {
                background-color: #2b2b2b;
                color: #aaaaaa;
                padding: 6px;
                border: none;
                font-weight: bold;
                font-size: 11px;
            }
            QCheckBox {
                color: #ffffff;
                font-size: 13px;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 4px;
                border: 1px solid #555555;
                background-color: #2b2b2b;
            }
            QCheckBox::indicator:checked {
                background-color: #e6232d;
                border: 1px solid #e6232d;
            }
            QPushButton {
                background-color: #2d2d2d;
                color: #ffffff;
                border: 1px solid #3d3d3d;
                border-radius: 6px;
                padding: 7px 16px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #383838;
                border-color: #4a4a4a;
            }
            QPushButton:pressed {
                background-color: #e6232d;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        # Header with status
        header_layout = QHBoxLayout()
        title_label = QLabel("🚀 Roblox Asset Proxy")
        title_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #ffffff;")
        self.status_label = QLabel("🟢 Активен (127.0.0.1:8989)")
        self.status_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #4cd964;")
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        header_layout.addWidget(self.status_label)
        layout.addLayout(header_layout)

        # Embedded DNS Fix Button with live status
        self.btn_dns_fix = QPushButton()
        self.update_dns_button_state()
        self.btn_dns_fix.clicked.connect(self.on_click_dns_fix)
        layout.addWidget(self.btn_dns_fix)

        # Options layout
        options_layout = QVBoxLayout()
        options_layout.setSpacing(8)

        self.cb_system_proxy = QCheckBox("Полный системный прокси (рекомендуется для лаунчера и игры)")
        self.cb_system_proxy.setChecked(self.proxy_server.use_system_proxy)
        self.cb_system_proxy.stateChanged.connect(self.on_system_proxy_changed)
        options_layout.addWidget(self.cb_system_proxy)

        self.cb_dpi = QCheckBox("Фрагментация пакетов TLS (обход блокировок ТСПУ по SNI)")
        self.cb_dpi.setChecked(self.proxy_server.enable_dpi_bypass)
        self.cb_dpi.stateChanged.connect(self.on_dpi_changed)
        options_layout.addWidget(self.cb_dpi)

        self.cb_autostart = QCheckBox("Запускать автоматически вместе с Windows")
        self.cb_autostart.setChecked(self.is_autostart_enabled())
        self.cb_autostart.stateChanged.connect(self.on_autostart_changed)
        options_layout.addWidget(self.cb_autostart)

        layout.addLayout(options_layout)

        # Table label & counter
        table_header = QHBoxLayout()
        log_label = QLabel("Журнал загруженных ассетов:")
        log_label.setStyleSheet("font-weight: bold; color: #aaaaaa;")
        self.counter_label = QLabel("Загружено: 0 ассетов")
        self.counter_label.setStyleSheet("color: #888888; font-size: 12px;")
        table_header.addWidget(log_label)
        table_header.addStretch()
        table_header.addWidget(self.counter_label)
        layout.addLayout(table_header)

        # Table
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Время", "Домен / Хост", "Размер", "Скорость"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)

        # Bottom buttons
        btn_layout = QHBoxLayout()
        self.btn_clear = QPushButton("Очистить журнал")
        self.btn_clear.clicked.connect(self.clear_logs)
        btn_layout.addWidget(self.btn_clear)

        btn_layout.addStretch()

        self.btn_close = QPushButton("Свернуть в трей")
        self.btn_close.clicked.connect(self.hide)
        self.btn_close.setStyleSheet("background-color: #e6232d; border-color: #e6232d;")
        btn_layout.addWidget(self.btn_close)
        layout.addLayout(btn_layout)

        self.total_assets = 0

    def update_dns_button_state(self):
        if is_hosts_dns_fixed():
            self.btn_dns_fix.setText("✅ DNS-фикс применён (tr.rbxcdn.com активен)")
            self.btn_dns_fix.setStyleSheet("""
                QPushButton {
                    background-color: #2e7d32;
                    border: 1px solid #388e3c;
                    border-radius: 6px;
                    padding: 10px 16px;
                    font-size: 13px;
                    font-weight: bold;
                    color: #ffffff;
                }
            """)
            self.btn_dns_fix.setEnabled(False)
        else:
            self.btn_dns_fix.setText("⚡ Починить картинки и плейсы в лаунчере (1 клик)")
            self.btn_dns_fix.setStyleSheet("""
                QPushButton {
                    background-color: #0078d4;
                    border: 1px solid #005a9e;
                    border-radius: 6px;
                    padding: 10px 16px;
                    font-size: 13px;
                    font-weight: bold;
                    color: #ffffff;
                }
                QPushButton:hover {
                    background-color: #106ebe;
                }
            """)
            self.btn_dns_fix.setEnabled(True)

    def on_click_dns_fix(self):
        apply_hosts_dns_fix()
        for delay in [1000, 2000, 3000, 4500]:
            QTimer.singleShot(delay, self.update_dns_button_state)

    def add_log_entry(self, timestamp: str, host: str, size: str, speed: str):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(timestamp))
        self.table.setItem(row, 1, QTableWidgetItem(host))
        self.table.setItem(row, 2, QTableWidgetItem(size))
        self.table.setItem(row, 3, QTableWidgetItem(speed))
        self.table.scrollToBottom()

        self.total_assets += 1
        self.counter_label.setText(f"Загружено: {self.total_assets} ассетов")

    def clear_logs(self):
        self.table.setRowCount(0)
        self.total_assets = 0
        self.counter_label.setText("Загружено: 0 ассетов")

    def on_system_proxy_changed(self, state):
        enabled = (state == 2)
        self.proxy_server.use_system_proxy = enabled
        self.proxy_server.pac_manager.use_system_proxy = enabled
        if self.proxy_server._is_running:
            self.proxy_server.pac_manager.disable()
            self.proxy_server.pac_manager.enable()

    def on_dpi_changed(self, state):
        self.proxy_server.enable_dpi_bypass = (state == 2)

    def is_autostart_enabled(self) -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_PATH, 0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, APP_NAME)
                return True
        except FileNotFoundError:
            return False

    def on_autostart_changed(self, state):
        enabled = (state == 2)
        exe_path = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_PATH, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe_path}"')
                else:
                    try:
                        winreg.DeleteValue(key, APP_NAME)
                    except FileNotFoundError:
                        pass
        except Exception as e:
            print(f"[WARN] Failed to change autostart: {e}")


class TrayApp:
    """System Tray Application Controller."""

    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        self.proxy_server = RobloxProxyServer(
            host=DEFAULT_HOST,
            port=DEFAULT_PORT,
            enable_dpi_bypass=False,
            use_system_proxy=True
        )
        self.proxy_thread = ProxyThread(self.proxy_server)
        self.proxy_thread.start()

        self.settings_dialog = SettingsDialog(self.proxy_server)

        self.icon = self.create_icon()
        self.tray = QSystemTrayIcon(self.icon, self.app)
        self.tray.setToolTip("Roblox Asset Proxy — Работает (127.0.0.1:8989)")

        self.menu = QMenu()
        self.menu.setStyleSheet("""
            QMenu {
                background-color: #242424;
                color: #ffffff;
                border: 1px solid #333333;
                border-radius: 6px;
                padding: 4px;
                font-family: 'Segoe UI', sans-serif;
                font-size: 13px;
            }
            QMenu::item {
                padding: 6px 20px 6px 28px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #e6232d;
                color: #ffffff;
            }
            QMenu::separator {
                height: 1px;
                background-color: #333333;
                margin: 4px 8px;
            }
        """)

        self.action_toggle = QAction("🟢 Обход активен", self.menu)
        self.action_toggle.setCheckable(True)
        self.action_toggle.setChecked(True)
        self.action_toggle.triggered.connect(self.toggle_proxy)
        self.menu.addAction(self.action_toggle)

        self.action_dns_fix = QAction("⚡ Починить картинки лаунчера (DNS-фикс)", self.menu)
        self.action_dns_fix.triggered.connect(self.apply_dns_fix_from_menu)
        self.menu.addAction(self.action_dns_fix)

        self.menu.addSeparator()

        self.action_settings = QAction("⚙️ Настройки и журнал...", self.menu)
        self.action_settings.triggered.connect(self.open_settings)
        self.menu.addAction(self.action_settings)

        self.action_autostart = QAction("🚀 Автозапуск с Windows", self.menu)
        self.action_autostart.setCheckable(True)
        self.action_autostart.setChecked(self.settings_dialog.is_autostart_enabled())
        self.action_autostart.triggered.connect(self.toggle_autostart)
        self.menu.addAction(self.action_autostart)

        self.menu.addSeparator()

        self.action_quit = QAction("❌ Выход", self.menu)
        self.action_quit.triggered.connect(self.quit_app)
        self.menu.addAction(self.action_quit)

        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self.poll_logs)
        self.poll_timer.start(200)

        # Periodic check for DNS fix state
        self.dns_check_timer = QTimer()
        self.dns_check_timer.timeout.connect(self.settings_dialog.update_dns_button_state)
        self.dns_check_timer.start(2000)

    def apply_dns_fix_from_menu(self):
        apply_hosts_dns_fix()
        for delay in [1000, 2000, 3000, 4500]:
            QTimer.singleShot(delay, self.settings_dialog.update_dns_button_state)

    def create_icon(self) -> QIcon:
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
        if os.path.exists(icon_path):
            return QIcon(icon_path)

        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QBrush(QColor(230, 35, 45)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(2, 2, 28, 28, 6, 6)
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        painter.drawText(QRect(0, 0, 32, 32), Qt.AlignmentFlag.AlignCenter, "R")
        painter.end()
        return QIcon(pixmap)

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_settings()

    def open_settings(self):
        self.settings_dialog.update_dns_button_state()
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def toggle_proxy(self):
        if self.action_toggle.isChecked():
            self.proxy_server.pac_manager.enable()
            self.action_toggle.setText("🟢 Обход активен")
            self.settings_dialog.status_label.setText("🟢 Активен (127.0.0.1:8989)")
            self.settings_dialog.status_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #4cd964;")
            self.tray.setToolTip("Roblox Asset Proxy — Работает (127.0.0.1:8989)")
        else:
            self.proxy_server.pac_manager.disable()
            self.action_toggle.setText("🔴 Обход выключен")
            self.settings_dialog.status_label.setText("🔴 Остановлен")
            self.settings_dialog.status_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #ff3b30;")
            self.tray.setToolTip("Roblox Asset Proxy — Остановлен")

    def toggle_autostart(self):
        checked = self.action_autostart.isChecked()
        self.settings_dialog.cb_autostart.setChecked(checked)

    def poll_logs(self):
        while not log_queue.empty():
            try:
                timestamp, host, size, speed = log_queue.get_nowait()
                self.settings_dialog.add_log_entry(timestamp, host, size, speed)
            except queue.Empty:
                break

    def quit_app(self):
        self.proxy_thread.stop()
        self.tray.hide()
        self.app.quit()

    def run(self):
        return self.app.exec()


def main():
    app = TrayApp()
    sys.exit(app.run())


if __name__ == "__main__":
    main()
