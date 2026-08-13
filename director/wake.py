"""Wake-on-LAN for the house Windows PC.

Director runs on the always-on Linux box, on the same LAN as calle-windows.
The phone never sends the packet itself — it asks Director, and Director
broadcasts a magic packet to the Ethernet NIC (Wake on Magic Packet and
Shutdown Wake-On-Lan are both enabled on that adapter).
"""
from __future__ import annotations

import asyncio
import os
import re
import socket
import time
from typing import Any

from . import config

# Realtek Gaming 2.5GbE on calle-windows. WOL is enabled on this NIC.
HOUSE_MAC = "30:C5:99:D0:0D:4A"
HOUSE_BROADCAST = "192.168.0.255"
HOUSE_IP = "192.168.0.83"

_MAC_HEX = re.compile(r"[^0-9A-Fa-f]")
_PROBE_TTL = 8.0
_probe_cache: dict[str, Any] = {"ip": "", "at": 0.0, "online": False}


def parse_mac(raw: str) -> bytes:
    hexed = _MAC_HEX.sub("", str(raw or ""))
    if len(hexed) != 12:
        raise ValueError("need a 6-byte MAC address")
    return bytes.fromhex(hexed)


def format_mac(mac: bytes) -> str:
    return ":".join(f"{byte:02X}" for byte in mac)


def magic_packet(mac: bytes) -> bytes:
    return b"\xff" * 6 + mac * 16


def _wake_cfg(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = settings if settings is not None else config.load_settings()
    return dict(cfg.get("wake") or {})


def remember(payload: dict[str, Any]) -> dict[str, str]:
    """Keep the last LAN identity the Windows client reported."""
    patch: dict[str, str] = {}
    mac_raw = str(payload.get("mac") or "").strip()
    if mac_raw:
        patch["mac"] = format_mac(parse_mac(mac_raw))
    ip = str(payload.get("ip") or "").strip()
    if ip:
        patch["ip"] = ip
    broadcast = str(payload.get("broadcast") or "").strip()
    if broadcast:
        patch["broadcast"] = broadcast
    if patch:
        config.update_settings({"wake": patch})
    return patch


def windows_machine(machines: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in machines:
        platform = str(row.get("platform") or "").lower()
        name = str(row.get("name") or "").lower()
        if "windows" in platform or "windows" in name:
            return row
    return machines[0] if machines else None


def status(*, machines: list[dict[str, Any]],
           settings: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = _wake_cfg(settings)
    try:
        parse_mac(str(cfg.get("mac") or ""))
        available = True
    except ValueError:
        available = False
    machine = windows_machine(machines)
    connected = bool(machine and machine.get("online"))
    return {
        "available": available,
        "online": connected,
        "connected": connected,
        "can_power_off": connected,
        "name": str((machine or {}).get("name") or "PC"),
    }


async def _probe_host(ip: str) -> bool:
    """Check the house PC independently of the Director client connection."""
    args = (["ping", "-n", "1", "-w", "700", ip] if os.name == "nt" else
            ["ping", "-c", "1", "-W", "1", ip])
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            if await asyncio.wait_for(proc.wait(), timeout=1.5) == 0:
                return True
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
    except OSError:
        pass

    # ICMP can be disabled on Windows. These common Windows services provide
    # a second signal without requiring credentials.
    async def port_open(port: int) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=0.25)
            del reader
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    return any(await asyncio.gather(*(port_open(port) for port in (135, 445, 3389))))


async def status_with_probe(*, machines: list[dict[str, Any]],
                            settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return power state, probing the LAN when the bridge is disconnected."""
    result = status(machines=machines, settings=settings)
    if result["connected"]:
        return result
    cfg = _wake_cfg(settings)
    ip = str(cfg.get("ip") or "").strip()
    if not ip:
        return result
    now = time.monotonic()
    if (_probe_cache["ip"] != ip or
            now - float(_probe_cache["at"] or 0.0) >= _PROBE_TTL):
        _probe_cache.update(ip=ip, at=now, online=await _probe_host(ip))
    result["online"] = bool(_probe_cache["online"])
    result["reachable"] = result["online"]
    return result


def destinations(settings: dict[str, Any] | None = None) -> list[str]:
    cfg = _wake_cfg(settings)
    hosts: list[str] = []
    for host in (
        "255.255.255.255",
        str(cfg.get("broadcast") or HOUSE_BROADCAST).strip(),
        str(cfg.get("ip") or "").strip(),
    ):
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def send(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = _wake_cfg(settings)
    try:
        mac = parse_mac(str(cfg.get("mac") or ""))
    except ValueError:
        return {"ok": False, "error": "no MAC address configured for this PC"}
    packet = magic_packet(mac)
    sent: list[str] = []
    errors: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.4)
        for host in destinations(settings):
            for port in (9, 7):
                target = f"{host}:{port}"
                try:
                    sock.sendto(packet, (host, port))
                    sent.append(target)
                except OSError as exc:
                    errors.append(f"{target}: {exc}")
    finally:
        sock.close()
    if not sent:
        return {
            "ok": False,
            "error": errors[0] if errors else "could not send a magic packet",
        }
    return {"ok": True, "sent": sent, "mac": format_mac(mac)}
