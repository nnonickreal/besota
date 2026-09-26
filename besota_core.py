#!/usr/bin/env python3
"""
besota_core.py
==============
Core BESOTA OTA flashing protocol implementation for BES-based Bluetooth
audio chips (Soundcore / Nothing Ear / etc).

This module contains ONLY transport + protocol logic - no UI code at all.
It is meant to be imported both by a GUI front-end (besota_gui.py) and,
optionally, from a simple CLI wrapper.

Two wire protocol variants are supported (`PROTOCOL_V1` / `PROTOCOL_V2`):

    BESOTA V1  - MTU 656. Chunk size is negotiated from the device's
                 metadata reply. A real 0x8B flow-control ACK is required
                 after every data chunk before the next one is sent.

    BESOTA V1.1  - MTU 512. Fixed 512-byte chunks. The device does not send
                 a per-chunk ACK in this mode, so chunks are sent back to
                 back (matches the original reference implementation).

Both variants share the exact same ERASE packet layout - only the 4-byte
little-endian OTA_BOOT address embedded in it (and therefore the CRC32
appended at its end) changes between offsets (0x18000, 0x20000, custom...).
"""

from __future__ import annotations

import platform
import re
import socket
import struct
import threading
import time
import zlib
from pathlib import Path
from typing import Callable, Optional

try:
    import bluetooth  # pip install pybluez
except ImportError:
    bluetooth = None  # allow importing this module without pybluez (e.g. for docs)


# ─────────────────────────────────────────────────────────── platform ──────

IS_WINDOWS = platform.system() == "Windows"

if IS_WINDOWS:
    # native Windows bluetooth socket constants (bypasses pybluez's broken
    # connect() implementation, which is incompatible with Python 3.10+)
    AF_BTH = 32
    BTHPROTO_RFCOMM = 3

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

# ─────────────────────────────────────────────────────────── constants ─────

MAGIC = b"BEST"
BESOTA_SERVICE_UUID = "66666666-6666-6666-6666-666666666666"

# protocol commands
CMD_HS_REQ      = 0x8E   # -> 0x8F  handshake
CMD_HS_RESP     = 0x8F
CMD_INIT1_REQ   = 0x90   # -> 0x91  prepare
CMD_INIT1_RESP  = 0x91
CMD_INIT2_REQ   = 0x8C   # -> 0x8D  session
CMD_INIT2_RESP  = 0x8D
CMD_META_REQ    = 0x80   # -> 0x81  file metadata (size + crc)
CMD_META_RESP   = 0x81
CMD_ERASE_REQ   = 0x86   # -> 0x87  erase flash
CMD_ERASE_RESP  = 0x87
CMD_DATA        = 0x85   # data chunk
CMD_FLOW_CTRL   = 0x8B   # per-chunk ack from device
CMD_CRC_REQ     = 0x82   # -> 0x83  block crc checkpoint
CMD_CRC_RESP    = 0x83
CMD_APPLY       = 0x88   # -> 0x84  commit
CMD_APPLY_RESP  = 0x84
CMD_FINISH      = 0x92   # -> 0x93  reboot / disconnect
CMD_FINISH_RESP = 0x93

CHECKPOINT_N    = 32
RECV_TIMEOUT    = 15.0
ERASE_TIMEOUT   = 60.0
DEFAULT_SCAN_TO = 3

BES_MAGIC     = b"\xff\xff\xff\xff"
BES_MAGIC_ALT = b"\x1c\xec\x57\xbe"

OTA_BOOT_CHECK_SIZE = 100

# well-known OTA_BOOT offsets shown in the GUI dropdown
KNOWN_OTA_BOOT_OFFSETS = {
    "0x18000": 0x18000,
    "0x20000": 0x20000,
}


# ─────────────────────────────────────────────────────────── exceptions ────

class FlashError(Exception):
    """Base class for all recoverable flashing errors."""


class FirmwareError(FlashError):
    """Raised when the firmware file itself is invalid or unsupported."""


class ProtocolError(FlashError):
    """Raised when the device replies with something unexpected."""


class FlashAborted(FlashError):
    """Raised when the user cancels an in-progress operation."""


# ─────────────────────────────────────────────────────────── helpers ───────

def crc32(data: bytes) -> int:
    """Standard zlib CRC32 (init 0xFFFFFFFF) - used for framing / checkpoints."""
    return zlib.crc32(data) & 0xFFFF_FFFF


def crc32_bes(data: bytes, init: int = 0) -> int:
    """CRC32 with a caller-supplied init value - used for the in-image CRC tag."""
    return zlib.crc32(data, init & 0xFFFF_FFFF) & 0xFFFF_FFFF


def parse_hex_address(text: str) -> int:
    """Parse a user-supplied hex address string such as '0x18000' or '18000'."""
    s = text.strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if not re.fullmatch(r"[0-9a-f]+", s or ""):
        raise ValueError(f"'{text}' is not a valid hex address")
    value = int(s, 16)
    if not (0 <= value <= 0xFFFFFFFF):
        raise ValueError("address out of range (must fit in 32 bits)")
    return value


# ──────────────────────────────────────────────── firmware sanity checks ───

def check_firmware_header(
    data: bytearray,
    confirm: Optional[Callable[[str], bool]] = None,
) -> bytearray:
    """
    Validate the first 4 bytes of the firmware image.

    - FF FF FF FF   : normal, nothing to do.
    - 1C EC 57 BE   : known alternate marker; ask the caller (via the
                      `confirm` callback) whether to patch it to FF FF FF FF.
    - anything else : raise FirmwareError.
    """
    header = bytes(data[:4])

    if header == BES_MAGIC:
        return data

    if header == BES_MAGIC_ALT:
        should_patch = True
        if confirm is not None:
            should_patch = confirm(
                "Firmware starts with 1C EC 57 BE instead of FF FF FF FF.\n"
                "Replace the header with FF FF FF FF before flashing?"
            )
        if should_patch:
            data[0:4] = BES_MAGIC
        return data

    raise FirmwareError("File does not start with FF FF FF FF - wrong file?")


def check_not_ota_boot_image(data: bytes, ota_boot_addr: int) -> None:
    """
    Look at OTA_BOOT_CHECK_SIZE bytes right before `ota_boot_addr`. If they
    are all 0xFF, this is almost certainly a raw ota_boot-style image that
    still has its OTA_CODE_OFFSET header attached and must never be flashed
    directly - raise a FirmwareError instead.
    """
    start = ota_boot_addr - OTA_BOOT_CHECK_SIZE
    if start < 0 or len(data) < ota_boot_addr:
        return  # not enough data to check, skip silently

    region = data[start:ota_boot_addr]
    if all(b == 0xFF for b in region):
        raise FirmwareError(
            "This looks like an OTA_BOOT image! Strip the OTA_CODE_OFFSET "
            f"bytes (here: 0x{ota_boot_addr:X}) from the head of the image "
            "before flashing."
        )


# ──────────────────────────────────────────────── crc patch logic ──────────

def find_crc_info(data: bytes) -> Optional[dict]:
    """
    Search for the 'CRC32_OF_IMAGE=0x<hex>' marker in the firmware binary.
    BES always places this metadata near the tail of the image, so we
    search from the end (rfind).
    """
    marker = b"CRC32_OF_IMAGE=0x"
    pos = data.rfind(marker)
    if pos == -1:
        return None

    crc_hex_start = pos + len(marker)
    crc_bytes = data[crc_hex_start: crc_hex_start + 8]
    if len(crc_bytes) < 8:
        return None

    try:
        stored_crc = int(crc_bytes, 16)
    except ValueError:
        return None

    return {"marker_pos": pos, "crc_hex_start": crc_hex_start, "stored_crc": stored_crc}


def patch_firmware_crc(data: bytes, log: Optional[Callable[[str], None]] = None) -> bytes:
    """
    Locate CRC32_OF_IMAGE=0x<hex> in the firmware tail, recompute the CRC
    using the BES algorithm (crc32_bes, init=0) over data[0 .. crc_hex_start],
    and patch the ASCII hex value in place if it does not already match.
    """
    log = log or (lambda _msg: None)
    info = find_crc_info(data)
    if info is None:
        log("[!] CRC32_OF_IMAGE marker not found - skipping CRC patch.")
        return data

    crc_hex_start = info["crc_hex_start"]
    stored_crc = info["stored_crc"]

    region = data[:crc_hex_start]
    calc_crc = crc32_bes(region, init=0)

    log("Firmware CRC check:")
    log(f"  marker @ 0x{info['marker_pos']:08X}")
    log(f"  stored CRC : {stored_crc:08X}")
    log(f"  computed   : {calc_crc:08X}")

    if calc_crc == stored_crc:
        log("  CRC OK, no patch needed.")
        return data

    new_crc_ascii = f"{calc_crc:08X}".encode("ascii")
    buf = bytearray(data)
    buf[crc_hex_start: crc_hex_start + 8] = new_crc_ascii
    log(f"  patched CRC: {stored_crc:08X} -> {calc_crc:08X}")
    return bytes(buf)


def prepare_firmware(
    path: str,
    ota_boot_addr: int,
    confirm: Optional[Callable[[str], bool]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> bytes:
    """
    Full firmware-preparation pipeline: load file, validate header, make
    sure it's not a raw ota_boot image, patch the in-image CRC if needed.
    Returns the ready-to-flash firmware bytes.
    """
    log = log or (lambda _msg: None)
    p = Path(path)
    if not p.exists():
        raise FirmwareError(f"File not found: {path}")

    data = bytearray(p.read_bytes())
    log(f"Loaded {p.name} ({len(data):,} bytes)")

    data = check_firmware_header(data, confirm=confirm)
    check_not_ota_boot_image(bytes(data), ota_boot_addr)
    data = bytearray(patch_firmware_crc(bytes(data), log=log))

    return bytes(data)


# ──────────────────────────────────────────────── erase payload builder ────

def build_erase_payload(ota_boot_addr: int) -> bytes:
    """
    Build the ERASE command payload for an arbitrary OTA_BOOT offset.

    Layout (88-byte body + 4-byte CRC32 = 92 bytes total):
        [0:4]   body length, always 0x58 (88) as little-endian uint32
        [4:8]   target OTA_BOOT address, little-endian uint32
        [8:88]  zero padding
        [88:92] CRC32 (standard zlib crc32) of body[0:88]

    Common to both BESOTA V1 and V2 - only the embedded address (and
    therefore the resulting CRC) changes between OTA_BOOT offsets.
    """
    body = struct.pack("<I", 0x58) + struct.pack("<I", ota_boot_addr) + b"\x00" * 80
    assert len(body) == 88
    crc = crc32(body)
    return body + struct.pack("<I", crc)


# ──────────────────────────────────────────────── device discovery ─────────

def scan_devices_by_name(name_filter: str, duration: int = DEFAULT_SCAN_TO):
    """Scan for nearby Bluetooth devices whose name contains `name_filter`."""
    if bluetooth is None:
        raise FlashError("pybluez is not installed - cannot scan for devices.")
    devices = bluetooth.discover_devices(duration=duration, lookup_names=True, flush_cache=True)
    nf = name_filter.lower()
    return [(addr, name) for addr, name in devices if name and nf in name.lower()]


def find_besota_port(address: str) -> int:
    """SDP lookup for the BESOTA RFCOMM service on the target device."""
    if bluetooth is None:
        raise FlashError("pybluez is not installed - cannot perform SDP lookup.")
    services = bluetooth.find_service(uuid=BESOTA_SERVICE_UUID, address=address)
    if not services:
        raise FlashError("BESOTA service (uuid 66666666-...) not found on this device.")
    return services[0]["port"]


# ──────────────────────────────────────────────── bluetooth transport ──────

def make_socket() -> socket.socket:
    """
    Create a raw RFCOMM socket.

    On Windows we bypass pybluez's own connect() (broken on Python 3.10+)
    and talk to the native Winsock Bluetooth API directly via stdlib socket.
    """
    if IS_WINDOWS:
        return socket.socket(AF_BTH, socket.SOCK_STREAM, BTHPROTO_RFCOMM)
    if bluetooth is None:
        raise FlashError("pybluez is not installed - cannot create an RFCOMM socket.")
    return bluetooth.BluetoothSocket(bluetooth.RFCOMM)


def connect_device(address: str) -> socket.socket:
    """Resolve the BESOTA RFCOMM port via SDP and connect to it."""
    port = find_besota_port(address)
    sock = make_socket()
    try:
        sock.connect((address, port))
    except OSError as e:
        raise FlashError(f"Connection failed: {e}") from e
    sock.settimeout(RECV_TIMEOUT)
    return sock


def disconnect_device(sock: Optional[socket.socket]) -> None:
    if sock is None:
        return
    try:
        sock.close()
    except Exception:
        pass


def _check_abort(abort_event: Optional[threading.Event]) -> None:
    if abort_event is not None and abort_event.is_set():
        raise FlashAborted("Aborted by user.")


def recv_packet(
    sock: socket.socket,
    cmd: int,
    min_len: int,
    timeout: float = RECV_TIMEOUT,
    abort_event: Optional[threading.Event] = None,
) -> bytes:
    """Receive bytes until a packet starting with `cmd` and >= min_len is seen."""
    buf = b""
    deadline = time.monotonic() + timeout
    sock.settimeout(0.3)
    try:
        while time.monotonic() < deadline:
            _check_abort(abort_event)
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ProtocolError("Device closed the connection.")
                buf += chunk
                idx = buf.find(bytes([cmd]))
                if idx != -1:
                    buf = buf[idx:]
                    if len(buf) >= min_len:
                        return buf
            except socket.timeout:
                pass
    finally:
        sock.settimeout(RECV_TIMEOUT)

    raise ProtocolError(f"No 0x{cmd:02X} response in {timeout:.0f}s (buf={buf.hex() or 'empty'})")


def _wait_flow_ctrl_real(
    sock: socket.socket, pkt_idx: int, abort_event: Optional[threading.Event] = None
) -> None:
    """BESOTA V1: wait for the real 0x8B flow-control ACK after each chunk."""
    deadline = time.monotonic() + RECV_TIMEOUT
    buf = b""
    sock.settimeout(0.3)
    try:
        while time.monotonic() < deadline:
            _check_abort(abort_event)
            try:
                data = sock.recv(32)
                if data:
                    buf += data
                    if CMD_FLOW_CTRL in buf:
                        return
            except socket.timeout:
                pass
    finally:
        sock.settimeout(RECV_TIMEOUT)

    raise ProtocolError(f"No flow-control ACK (0x8B) after chunk #{pkt_idx}")


def _wait_flow_ctrl_noop(
    sock: socket.socket, pkt_idx: int, abort_event: Optional[threading.Event] = None
) -> None:
    """BESOTA V2: device does not send a per-chunk ACK, nothing to wait for."""
    return


# ──────────────────────────────────────────────── protocol definitions ─────

class Protocol:
    """Describes the differences between BESOTA wire protocol variants."""

    def __init__(
        self,
        key: str,
        label: str,
        description: str,
        default_chunk: int,
        default_ota_addr: int,
        parse_meta_response: Callable[[bytes, int], int],
        wait_flow_ctrl: Callable[[socket.socket, int, Optional[threading.Event]], None],
    ):
        self.key = key
        self.label = label
        self.description = description
        self.default_chunk = default_chunk
        self.default_ota_addr = default_ota_addr
        self.parse_meta_response = parse_meta_response
        self.wait_flow_ctrl = wait_flow_ctrl


def _parse_meta_v1(resp: bytes, default_chunk: int) -> int:
    """BESOTA V1: status word + negotiated MTU is embedded in the response."""
    if len(resp) < 7:
        raise ProtocolError("Metadata response too short.")
    status = struct.unpack_from("<H", resp, 5)[0]
    if status != 0x0001:
        raise ProtocolError(f"Device rejected firmware (status=0x{status:04X}).")

    chunk_size = default_chunk
    for offset in (9, 7):
        if len(resp) >= offset + 2:
            mtu = struct.unpack_from("<H", resp, offset)[0]
            if 128 <= mtu <= 65535:
                chunk_size = mtu - 16
                break
    return chunk_size


def _parse_meta_v2(resp: bytes, default_chunk: int) -> int:
    """BESOTA V2: only the magic is checked, chunk size is always fixed."""
    if resp[1:5] != MAGIC:
        raise ProtocolError(f"Device rejected metadata (magic mismatch: {resp[:8].hex()})")
    return default_chunk


PROTOCOL_V1 = Protocol(
    key="v1",
    label="BESOTA V1  (MTU 656)",
    description=(
        "Chunk size is negotiated from the device's metadata reply "
        "(MTU 656 by default). Waits for a real 0x8B flow-control ACK "
        "after every data chunk before sending the next one."
    ),
    default_chunk=656,
    default_ota_addr=0x18000,
    parse_meta_response=_parse_meta_v1,
    wait_flow_ctrl=_wait_flow_ctrl_real,
)

PROTOCOL_V2 = Protocol(
    key="v2",
    label="BESOTA V2  (MTU 512)",
    description=(
        "Fixed 512-byte data chunks. The device does not send a "
        "per-chunk ACK in this mode, so chunks are sent back-to-back."
    ),
    default_chunk=512,
    default_ota_addr=0x20000,
    parse_meta_response=_parse_meta_v2,
    wait_flow_ctrl=_wait_flow_ctrl_noop,
)

PROTOCOLS = {p.key: p for p in (PROTOCOL_V1, PROTOCOL_V2)}


# ──────────────────────────────────────────────────────── protocol stages ──

def stage_handshake_and_init(sock: socket.socket, abort_event=None) -> None:
    sock.sendall(bytes([CMD_HS_REQ]) + MAGIC)
    recv_packet(sock, CMD_HS_RESP, min_len=9, abort_event=abort_event)

    sock.sendall(bytes([CMD_INIT1_REQ, 0x00]))
    recv_packet(sock, CMD_INIT1_RESP, min_len=2, abort_event=abort_event)

    payload_8c = bytes.fromhex("00" * 32 + "010203040496cefd")
    sock.sendall(bytes([CMD_INIT2_REQ]) + MAGIC + payload_8c)
    recv_packet(sock, CMD_INIT2_RESP, min_len=10, abort_event=abort_event)


def stage_metadata(
    sock: socket.socket, protocol: Protocol, file_size: int, file_crc: int, abort_event=None
) -> int:
    req = (
        bytes([CMD_META_REQ]) + MAGIC
        + struct.pack("<I", file_size)
        + struct.pack("<I", file_crc)
    )
    sock.sendall(req)
    resp = recv_packet(sock, CMD_META_RESP, min_len=7, abort_event=abort_event)
    return protocol.parse_meta_response(resp, protocol.default_chunk)


def stage_erase(sock: socket.socket, ota_boot_addr: int, abort_event=None) -> None:
    payload = build_erase_payload(ota_boot_addr)
    sock.sendall(bytes([CMD_ERASE_REQ]) + payload)
    resp = recv_packet(sock, CMD_ERASE_RESP, min_len=2, timeout=ERASE_TIMEOUT, abort_event=abort_event)
    if not (len(resp) >= 2 and resp[1] == 0x01):
        raise ProtocolError(f"Unexpected erase response: {resp[:8].hex()}")


def _crc_checkpoint(sock: socket.socket, crc_value: int, chk_num: int, abort_event=None) -> None:
    sock.sendall(bytes([CMD_CRC_REQ]) + MAGIC + struct.pack("<I", crc_value))
    resp = recv_packet(sock, CMD_CRC_RESP, min_len=2, timeout=10.0, abort_event=abort_event)
    if len(resp) < 2 or resp[1] != 0x01:
        raise ProtocolError(f"CRC mismatch at checkpoint #{chk_num}!")


def stage_transfer(
    sock: socket.socket,
    protocol: Protocol,
    firmware: bytes,
    chunk_size: int,
    progress: Optional[Callable[[int, int, float, float], None]] = None,
    abort_event=None,
) -> None:
    total = len(firmware)
    offset = 0
    pkt_idx = 0
    block_start = 0
    t0 = time.monotonic()

    while offset < total:
        _check_abort(abort_event)

        chunk = firmware[offset: offset + chunk_size]
        sock.sendall(bytes([CMD_DATA]) + chunk)
        protocol.wait_flow_ctrl(sock, pkt_idx, abort_event)

        offset += len(chunk)
        pkt_idx += 1

        if progress is not None:
            elapsed = time.monotonic() - t0 or 0.001
            speed = offset / 1024 / elapsed
            eta = (total - offset) / (offset / elapsed) if offset else 0
            progress(offset, total, speed, eta)

        if pkt_idx % CHECKPOINT_N == 0:
            _crc_checkpoint(sock, crc32(firmware[block_start:offset]), pkt_idx // CHECKPOINT_N, abort_event)
            block_start = offset

    if block_start < total:
        _crc_checkpoint(sock, crc32(firmware[block_start:]), pkt_idx // CHECKPOINT_N + 1, abort_event)


def stage_apply(sock: socket.socket, abort_event=None) -> None:
    sock.sendall(bytes([CMD_APPLY]))
    recv_packet(sock, CMD_APPLY_RESP, min_len=2, abort_event=abort_event)

    sock.sendall(bytes([CMD_FINISH]) + MAGIC)
    try:
        recv_packet(sock, CMD_FINISH_RESP, min_len=2, timeout=5.0, abort_event=abort_event)
    except ProtocolError:
        # the device often reboots immediately and drops the connection
        # before the FINISH response makes it back - that's fine.
        pass


# ──────────────────────────────────────────────────────── top-level flash ──

def run_flash(
    sock: socket.socket,
    firmware: bytes,
    protocol: Protocol,
    ota_boot_addr: int,
    log: Callable[[str], None],
    progress: Optional[Callable[[int, int, float, float], None]] = None,
    abort_event: Optional[threading.Event] = None,
) -> None:
    """
    Run the full flashing pipeline over an already-connected RFCOMM socket.
    Raises FlashError (or a subclass) on any failure.
    """
    file_size = len(firmware)
    file_crc = crc32(firmware)

    log(f"Using protocol: {protocol.label}")
    log(f"OTA_BOOT address: 0x{ota_boot_addr:X}")
    log(f"Firmware size: {file_size:,} bytes, CRC32: 0x{file_crc:08X}")

    log("[1/5] Handshake & initialization...")
    stage_handshake_and_init(sock, abort_event)
    log("      OK")

    log("[2/5] Sending metadata...")
    chunk_size = stage_metadata(sock, protocol, file_size, file_crc, abort_event)
    log(f"      OK (chunk size: {chunk_size} bytes)")

    log("[3/5] Erasing flash...")
    stage_erase(sock, ota_boot_addr, abort_event)
    log("      OK")

    log(f"[4/5] Transferring {file_size:,} bytes...")
    stage_transfer(sock, protocol, firmware, chunk_size, progress, abort_event)
    log("      transfer complete")

    log("[5/5] Finalizing...")
    stage_apply(sock, abort_event)
    log("      OK - device is applying the update and will reboot.")


# ───────────────────────────────────────────────────────────── cli ─────────

def _cli_progress(offset: int, total: int, speed: float, eta: float) -> None:
    """Simple terminal progress bar for the CLI."""
    import sys

    pct = (offset / total * 100) if total else 0.0
    bar_len = 30
    filled = int(bar_len * offset // total) if total else 0
    bar = "=" * filled + "-" * (bar_len - filled)
    sys.stdout.write(
        f"\r[{bar}] {pct:5.1f}% ({offset / 1024:,.1f}/{total / 1024:,.1f} KB) "
        f"{speed:,.1f} KB/s ETA {eta:,.0f}s"
    )
    sys.stdout.flush()
    if offset >= total:
        sys.stdout.write("\n")
        sys.stdout.flush()


def main() -> None:
    """CLI entry point for flashing and device discovery."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="besota_core.py",
        description="BESOTA flashing tool for BES-based Bluetooth audio devices.",
        epilog=(
            "examples:\n"
            "  python besota_core.py --scan soundcore\n"
            "  python besota_core.py 11:22:33:44:55:66 firmware.bin -p v1 -o 0x18000\n"
            "  python besota_core.py 11:22:33:44:55:66 firmware.bin -p v2 -y\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "address",
        nargs="?",
        help="Target device Bluetooth MAC address (e.g. AA:BB:CC:DD:EE:FF).",
    )
    parser.add_argument(
        "firmware",
        nargs="?",
        help="Path to the .bin firmware image to flash.",
    )
    parser.add_argument(
        "-s", "--scan",
        metavar="NAME",
        help="Scan for nearby Bluetooth devices matching NAME filter and exit.",
    )
    parser.add_argument(
        "--scan-timeout",
        type=int,
        default=DEFAULT_SCAN_TO,
        metavar="SEC",
        help=f"Scan duration in seconds (default: {DEFAULT_SCAN_TO}).",
    )
    parser.add_argument(
        "-p", "--protocol",
        choices=["v1", "v2"],
        default=None,
        help="Protocol variant ('v1' MTU 656 flow-controlled, 'v2' MTU 512 unacked). Default: v1.",
    )
    parser.add_argument(
        "-o", "--offset",
        metavar="HEX",
        help="OTA_BOOT flash address offset (e.g. 0x18000, 0x20000). Defaults to protocol preset.",
    )
    parser.add_argument(
        "-y", "--yes",
        action="store_true",
        help="Automatically confirm header patches without interactive prompting.",
    )

    # Show help and exit cleanly when invoked without any arguments
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()

    if args.scan:
        print(f"Scanning for devices matching '{args.scan}' ({args.scan_timeout}s)...")
        try:
            results = scan_devices_by_name(args.scan, duration=args.scan_timeout)
        except FlashError as e:
            print(f"[!] Scan failed: {e}", file=sys.stderr)
            sys.exit(1)

        if not results:
            print("No matching devices found.")
        else:
            print(f"Found {len(results)} device(s):")
            for addr, name in results:
                print(f"  {addr}  {name}")
        sys.exit(0)

    if not args.address or not args.firmware:
        parser.print_help(sys.stderr)
        print("\nError: both 'address' and 'firmware' are required unless --scan is used.", file=sys.stderr)
        sys.exit(1)

    if not MAC_RE.match(args.address):
        print(f"[!] Invalid MAC address format: '{args.address}'", file=sys.stderr)
        sys.exit(1)

    proto_key = args.protocol or "v1"
    protocol = PROTOCOLS[proto_key]

    if args.offset:
        try:
            ota_addr = parse_hex_address(args.offset)
        except ValueError as e:
            print(f"[!] Invalid offset: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        ota_addr = protocol.default_ota_addr

    def confirm_cb(msg: str) -> bool:
        if args.yes:
            return True
        print(f"\n[?] {msg}")
        ans = input("Proceed? [y/N]: ").strip().lower()
        return ans in ("y", "yes")

    try:
        firmware_bytes = prepare_firmware(
            args.firmware,
            ota_addr,
            confirm=confirm_cb,
            log=print,
        )
    except FlashError as e:
        print(f"[!] Firmware error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to {args.address}...")
    try:
        sock = connect_device(args.address)
    except FlashError as e:
        print(f"[!] Connection failed: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        run_flash(
            sock=sock,
            firmware=firmware_bytes,
            protocol=protocol,
            ota_boot_addr=ota_addr,
            log=print,
            progress=_cli_progress,
        )
    except FlashError as e:
        print(f"\n[!] Flashing failed: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[!] Flashing aborted by user.", file=sys.stderr)
        sys.exit(130)
    finally:
        disconnect_device(sock)


if __name__ == "__main__":
    main()