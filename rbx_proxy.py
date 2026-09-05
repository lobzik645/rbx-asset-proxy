"""
Roblox Asset Proxy (rbx-proxy)
A specialized lightweight local proxy for bypassing Roblox texture and asset blocks.
"""

import asyncio
import ctypes
import json
import os
import re
import signal
import socket
import ssl
import sys
import time
import urllib.request
import winreg

try:
    import msvcrt
    HAS_MSVCRT = True
except ImportError:
    HAS_MSVCRT = False

# Configuration defaults
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8989
PAC_PATH = "/proxy.pac"

# List of domains to intercept and route through DoH + DPI bypass
ROBLOX_DOMAINS = [
    "rbxcdn.com",
    "roblox.com",
    "arkoselabs.com"
]

# Windows Internet Settings Registry Key
REG_SETTINGS_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_REFRESH = 37


class WindowsProxyManager:
    """Manages Windows system PAC settings cleanly and restores them on exit."""

    def __init__(self, pac_url: str):
        self.pac_url = pac_url
        self.original_pac = None
        self.had_pac = False
        self.is_set = False

    def enable(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_SETTINGS_PATH, 0, winreg.KEY_READ) as key:
                try:
                    self.original_pac, _ = winreg.QueryValueEx(key, "AutoConfigURL")
                    self.had_pac = True
                except FileNotFoundError:
                    self.original_pac = None
                    self.had_pac = False

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_SETTINGS_PATH, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "AutoConfigURL", 0, winreg.REG_SZ, self.pac_url)

            self._refresh_wininet()
            self.is_set = True
            print(f"[SYSTEM] Windows PAC proxy enabled -> {self.pac_url}")
        except Exception as e:
            print(f"[WARN] Failed to set Windows PAC proxy: {e}")

    def disable(self):
        if not self.is_set:
            return
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_SETTINGS_PATH, 0, winreg.KEY_SET_VALUE) as key:
                if self.had_pac and self.original_pac:
                    winreg.SetValueEx(key, "AutoConfigURL", 0, winreg.REG_SZ, self.original_pac)
                else:
                    try:
                        winreg.DeleteValue(key, "AutoConfigURL")
                    except FileNotFoundError:
                        pass

            self._refresh_wininet()
            self.is_set = False
            print("[SYSTEM] Windows PAC proxy restored to original settings.")
        except Exception as e:
            print(f"[WARN] Failed to restore Windows proxy settings: {e}")

    def _refresh_wininet(self):
        try:
            ctypes.windll.wininet.InternetSetOptionW(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
            ctypes.windll.wininet.InternetSetOptionW(0, INTERNET_OPTION_REFRESH, 0, 0)
        except Exception:
            pass


class DoHResolver:
    """DNS-over-HTTPS resolver bypassing ISP DNS blocks and GeoIP restrictions."""

    def __init__(self):
        self.cache = {}  # domain -> (list_of_ips, expire_timestamp)
        self.ssl_ctx = ssl.create_default_context()

    async def resolve(self, domain: str) -> list[str]:
        now = time.time()
        if domain in self.cache:
            ips, exp = self.cache[domain]
            if now < exp and ips:
                return ips

        ips = await self._query_doh(domain)
        if not ips:
            # Fallback to local socket resolution
            try:
                loop = asyncio.get_running_loop()
                infos = await loop.getaddrinfo(domain, 443, family=socket.AF_INET)
                ips = [item[4][0] for item in infos]
            except Exception:
                ips = []

        if ips:
            self.cache[domain] = (ips, now + 300)  # 5 min cache
        return ips

    async def _query_doh(self, domain: str) -> list[str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._sync_query_doh, domain)

    def _sync_query_doh(self, domain: str) -> list[str]:
        # Query Google DoH with edns_client_subnet=0.0.0.0/0 to prevent Roblox GeoDNS from returning NXDOMAIN to RU IPs
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
                        # Extract type 1 (A records)
                        a_records = [ans["data"] for ans in answers if ans.get("type") == 1]
                        if a_records:
                            return a_records
                        # If only CNAME returned, find target recursively
                        cnames = [ans["data"].rstrip(".") for ans in answers if ans.get("type") == 5]
                        for cname in cnames:
                            nested_ips = self._sync_query_doh(cname)
                            if nested_ips:
                                return nested_ips
            except Exception:
                continue

        return []


class RobloxProxyServer:
    """Async HTTP / HTTPS CONNECT Proxy server for Roblox assets."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, enable_dpi_bypass=True):
        self.host = host
        self.port = port
        self.enable_dpi_bypass = enable_dpi_bypass
        self.resolver = DoHResolver()
        self.pac_manager = WindowsProxyManager(f"http://{host}:{port}{PAC_PATH}")
        self.stats = {"assets_proxied": 0, "bytes_transferred": 0}
        self.server = None
        self._is_running = True

    def is_roblox_domain(self, host: str) -> bool:
        host_clean = host.split(":")[0].lower()
        return any(host_clean == d or host_clean.endswith("." + d) for d in ROBLOX_DOMAINS)

    def generate_pac_script(self) -> str:
        return f"""// Roblox Asset Proxy Auto-Configuration (PAC) Script
function FindProxyForURL(url, host) {{
    // Only Roblox asset CDNs and APIs are routed through the proxy
    if (
        shExpMatch(host, "*.rbxcdn.com") ||
        shExpMatch(host, "*.roblox.com") ||
        host === "rbxcdn.com" ||
        host === "roblox.com" ||
        shExpMatch(host, "*.arkoselabs.com")
    ) {{
        return "PROXY {self.host}:{self.port}; DIRECT";
    }}
    // Everything else (gameplay UDP, browser, Discord, Steam) goes directly!
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

        # Resolve IP via DoH
        if is_rbx:
            ips = await self.resolver.resolve(dest_host)
            connect_host = ips[0] if ips else dest_host
        else:
            connect_host = dest_host

        # Connect to destination
        remote_reader = None
        remote_writer = None
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

        # Handle TLS handshake with DPI bypass if enabled for Roblox domains
        if is_rbx and self.enable_dpi_bypass:
            try:
                first_chunk = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                if first_chunk:
                    if len(first_chunk) > 5 and first_chunk[0] == 0x16:
                        # Split ClientHello into two TCP segments (TCP fragmentation)
                        remote_writer.write(first_chunk[:2])
                        await remote_writer.drain()
                        await asyncio.sleep(0.015)
                        remote_writer.write(first_chunk[2:])
                        await remote_writer.drain()
                    else:
                        remote_writer.write(first_chunk)
                        await remote_writer.drain()
            except Exception:
                pass

        # Tunnel bidirectional data
        self.stats["assets_proxied"] += 1
        t0 = time.time()
        bytes_transferred = await self.bidirectional_pipe(reader, writer, remote_reader, remote_writer)
        duration = time.time() - t0

        if is_rbx:
            kb = bytes_transferred / 1024
            print(f"[{time.strftime('%H:%M:%S')}] [ASSET] {dest_host:<26} -> {kb:6.1f} KB ({duration:.2f}s)")

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

            await self.bidirectional_pipe(reader, writer, remote_reader, remote_writer)
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

    async def key_listener(self):
        """Allows pressing 'q' or 'Q' to quit gracefully."""
        if not HAS_MSVCRT:
            return
        while self._is_running:
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch in (b'q', b'Q', b'\x03'):
                    print("\n[INFO] 'Q' pressed. Exiting...")
                    self.stop()
                    break
            await asyncio.sleep(0.2)

    async def start(self):
        self.server = await asyncio.start_server(self.handle_client, self.host, self.port)
        self.pac_manager.enable()
        self.print_banner()

        key_task = asyncio.create_task(self.key_listener())
        try:
            async with self.server:
                await self.server.serve_forever()
        finally:
            key_task.cancel()

    def stop(self):
        if not self._is_running:
            return
        self._is_running = False
        print("\n[STOPPING] Shutting down Roblox Asset Proxy...")
        self.pac_manager.disable()
        if self.server:
            self.server.close()

    def print_banner(self):
        os.system("cls" if os.name == "nt" else "clear")
        banner = f"""
==================================================================
           ROBLOX ASSET PROXY (rbx-proxy) v1.0
==================================================================
 [STATUS]  Proxy is ACTIVE and RUNNING!
 [LISTEN]  http://{self.host}:{self.port}
 [PAC URL] http://{self.host}:{self.port}{PAC_PATH}
 [MODE]    DoH (Google/Cloudflare ECS Bypass) + TLS Packet Splitting
 [SCOPE]   *.rbxcdn.com, *.roblox.com (Zero game UDP ping impact!)
------------------------------------------------------------------
 Press [Q] or [Ctrl+C] at any time to safely stop and restore proxy.
==================================================================
[LOGS] Live requests:
"""
        print(banner)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Roblox Asset Proxy (rbx-proxy)")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Proxy host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Proxy port (default: 8989)")
    parser.add_argument("--no-dpi", action="store_true", help="Disable TLS ClientHello splitting")
    parser.add_argument("--no-pac", action="store_true", help="Do not automatically configure Windows PAC proxy")
    args = parser.parse_args()

    proxy = RobloxProxyServer(
        host=args.host,
        port=args.port,
        enable_dpi_bypass=not args.no_dpi
    )
    if args.no_pac:
        proxy.pac_manager.enable = lambda: None
        proxy.pac_manager.disable = lambda: None

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(proxy.start())
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        proxy.stop()
        loop.close()
        print("[EXIT] Successfully stopped. Have fun in Roblox!")


if __name__ == "__main__":
    main()
