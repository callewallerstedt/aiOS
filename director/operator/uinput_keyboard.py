"""Persistent, local-only virtual keyboard for the Director operator.

Chromium ignores XTest keyboard events even though it accepts the same X11
mouse path. A kernel uinput device is indistinguishable from a physical
keyboard to applications. This small root service owns only /dev/uinput and a
Unix socket whose owner is the Director user; it never accepts shell commands
or text, only bounded numeric key events.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import pathlib
import socket
import struct
import time
from typing import Any

EV_SYN = 0
EV_KEY = 1
SYN_REPORT = 0
BUS_USB = 0x03
# Linux shares the EV_KEY namespace with mouse/gamepad buttons starting at
# 0x100. Advertising every EV_KEY code made udev classify this device as both
# a mouse and a keyboard, so desktop clients did not treat it like a normal
# keyboard. The standard keyboard range is sufficient for every exposed key.
KEYBOARD_KEY_MAX = 0xFF
UINPUT_MAX_NAME_SIZE = 80

_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_NONE = 0
_IOC_WRITE = 1


def _ioc(direction: int, kind: str, number: int, size: int = 0) -> int:
    return ((direction << _IOC_DIRSHIFT) | (ord(kind) << _IOC_TYPESHIFT)
            | (number << _IOC_NRSHIFT) | (size << _IOC_SIZESHIFT))


UI_DEV_CREATE = _ioc(_IOC_NONE, "U", 1)
UI_DEV_DESTROY = _ioc(_IOC_NONE, "U", 2)
UI_DEV_SETUP = _ioc(_IOC_WRITE, "U", 3, struct.calcsize("HHHH80sI"))
UI_SET_EVBIT = _ioc(_IOC_WRITE, "U", 100, struct.calcsize("i"))
UI_SET_KEYBIT = _ioc(_IOC_WRITE, "U", 101, struct.calcsize("i"))


def open_keyboard() -> int:
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    for code in range(1, KEYBOARD_KEY_MAX + 1):
        fcntl.ioctl(fd, UI_SET_KEYBIT, code)
    name = b"aiOS Director virtual keyboard".ljust(UINPUT_MAX_NAME_SIZE, b"\0")
    setup = struct.pack("HHHH80sI", BUS_USB, 0x1209, 0xA105, 1, name, 0)
    fcntl.ioctl(fd, UI_DEV_SETUP, setup)
    fcntl.ioctl(fd, UI_DEV_CREATE)
    # Let udev and the desktop enumerate the device before accepting commands.
    time.sleep(1.0)
    return fd


def emit(fd: int, event_type: int, code: int, value: int) -> None:
    # Stamp transitions like a physical evdev device. Device-level diagnostics
    # accept a zero timeval, but desktop clients are entitled to use event time
    # for ordering and freshness decisions.
    now = time.time()
    seconds = int(now)
    microseconds = int((now - seconds) * 1_000_000)
    os.write(fd, struct.pack(
        "llHHi", seconds, microseconds, event_type, code, value))


def perform(fd: int, payload: dict[str, Any]) -> dict[str, Any]:
    raw_events = payload.get("events") or []
    if not isinstance(raw_events, list) or not 1 <= len(raw_events) <= 256:
        raise ValueError("events must contain 1 to 256 key transitions")
    delay = max(0.0, min(float(payload.get("delay_ms") or 20) / 1000.0, 0.25))
    events: list[tuple[int, int]] = []
    for raw in raw_events:
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise ValueError("each event must be [key_code, value]")
        code, value = int(raw[0]), int(raw[1])
        if not 1 <= code <= KEYBOARD_KEY_MAX or value not in (0, 1, 2):
            raise ValueError("invalid key transition")
        events.append((code, value))
    for code, value in events:
        emit(fd, EV_KEY, code, value)
        emit(fd, EV_SYN, SYN_REPORT, 0)
        if delay:
            time.sleep(delay)
    return {"ok": True, "events": len(events)}


def serve(socket_path: pathlib.Path, owner_uid: int, owner_gid: int) -> None:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    keyboard = open_keyboard()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
        os.chown(socket_path, owner_uid, owner_gid)
        os.chmod(socket_path, 0o600)
        server.listen(8)
        while True:
            client, _ = server.accept()
            with client:
                try:
                    peer = struct.unpack("3i", client.getsockopt(
                        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
                    if peer[1] != owner_uid:
                        raise PermissionError("untrusted socket peer")
                    raw = b""
                    while b"\n" not in raw and len(raw) <= 65536:
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        raw += chunk
                    payload = json.loads(raw.split(b"\n", 1)[0] or b"{}")
                    result = perform(keyboard, payload)
                except Exception as exc:
                    result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                client.sendall(json.dumps(result, ensure_ascii=True).encode("ascii") + b"\n")
    finally:
        server.close()
        try:
            fcntl.ioctl(keyboard, UI_DEV_DESTROY)
        finally:
            os.close(keyboard)
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--uid", required=True, type=int)
    parser.add_argument("--gid", required=True, type=int)
    args = parser.parse_args()
    serve(pathlib.Path(args.socket), args.uid, args.gid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
