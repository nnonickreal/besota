#!/usr/bin/env python3

from __future__ import annotations
import socket, struct, zlib, sys, time, argparse, platform, re
from pathlib import Path

try:
    import bluetooth  # pip install pybluez -- used only for sdp lookup (and scanning on windows)
except ImportError:
    sys.exit("install pybluez first: pip install git+https://github.com/pybluez/pybluez.git")

# ─────────────────────────────────────────────────────────── platform ──────

IS_WINDOWS = platform.system() == "Windows"

# native windows bluetooth socket constants (bypasses pybluez's broken
# connect() implementation, which is incompatible with python 3.10+)
if IS_WINDOWS:
    AF_BTH          = 32
    BTHPROTO_RFCOMM = 3

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

# ─────────────────────────────────────────────────────────── constants ─────

MAGIC = b"BEST"

BESOTA_SERVICE_UUID = "66666666-6666-6666-6666-666666666666"

# commands
CMD_HS_REQ     = 0x8E   # -> 0x8f  handshake
CMD_HS_RESP    = 0x8F
CMD_INIT1_REQ  = 0x90   # -> 0x91  prepare
CMD_INIT1_RESP = 0x91
CMD_INIT2_REQ  = 0x8C   # -> 0x8d  session
CMD_INIT2_RESP = 0x8D
CMD_META_REQ   = 0x80   # -> 0x81  file metadata (size + crc)
CMD_META_RESP  = 0x81
CMD_ERASE_REQ  = 0x86   # -> 0x87  erase flash
CMD_ERASE_RESP = 0x87
CMD_DATA       = 0x85   # data chunk
CMD_FLOW_CTRL  = 0x8B   # per-chunk ack from device
CMD_CRC_REQ    = 0x82   # -> 0x83  block crc checkpoint
CMD_CRC_RESP   = 0x83
CMD_APPLY      = 0x88   # -> 0x84  commit
CMD_APPLY_RESP = 0x84
CMD_FINISH     = 0x92   # -> 0x93  reboot / disconnect
CMD_FINISH_RESP= 0x93

DEFAULT_CHUNK   = 656     # exact size from logs (mtu 672 - 16 header bytes)
CHECKPOINT_N    = 32
RECV_TIMEOUT    = 15.0
ERASE_TIMEOUT   = 60.0
DEFAULT_SCAN_TO = 8

BES_MAGIC     = b"\xff\xff\xff\xff"
BES_MAGIC_ALT = b"\x1c\xec\x57\xbe"

OTA_BOOT_CHECK_ADDR = 0x18000
OTA_BOOT_CHECK_SIZE = 100

# ─────────────────────────────────────────────────────────── helpers ─────────

def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFF_FFFF

def crc32_bes(data: bytes, init: int = 0) -> int:
    # crc32 with a non-standard init value — used internally by bes firmware
    crc = init & 0xFFFF_FFFF
    crc = zlib.crc32(data, crc) & 0xFFFF_FFFF
    return crc

# ──────────────────────────────────────────────── firmware sanity checks ─────

def check_firmware_header(data: bytearray) -> bytearray:
    """
    checks the first 4 bytes of the firmware.
    - ff ff ff ff: normal, nothing to do.
    - 1c ec 57 be: known alternate marker, ask whether to replace it.
    - anything else: error.
    """
    header = bytes(data[:4])

    if header == BES_MAGIC:
        return data

    if header == BES_MAGIC_ALT:
        answer = input(
            "firmware starts with 1c ec 57 be instead of ff ff ff ff. "
            "replace with ff ff ff ff? [y/n]: "
        ).strip().lower()
        if answer == "y":
            data[0:4] = BES_MAGIC
            print("header patched to ff ff ff ff.")
        return data

    print("\nerror: file does not start with ff ff ff ff, wrong file?")
    # if input("continue anyway? [y/n]: ").strip().lower() != "y":
    sys.exit(0)
    return data

def check_not_ota_boot_image(data: bytes) -> None:
    """
    looks at the 100 bytes right before address 0x18000. if all of them
    are 0xff, this is almost certainly an ota_boot-style image, which
    must never be flashed directly — abort with an error.
    """
    start = OTA_BOOT_CHECK_ADDR - OTA_BOOT_CHECK_SIZE
    if start < 0 or len(data) < OTA_BOOT_CHECK_ADDR:
        return  # not enough data to check, skip silently

    region = data[start:OTA_BOOT_CHECK_ADDR]
    if all(b == 0xFF for b in region):
        sys.exit("this is an ota_boot image! please delete the OTA_CODE_OFFSET bytes (0x18000 for bes2300) from the head of your image.")

# ──────────────────────────────────────────────── crc patch logic ────────────

def find_crc_info(data: bytes) -> dict | None:
    """
    searches for the crc32_of_image=0x<hex> string in the firmware binary.
    uses rfind() to search from the end of the file — bes always places
    metadata at the tail of the image.
    returns a dict with metadata or none if not found.
    """
    marker = b"CRC32_OF_IMAGE=0x"

    pos = data.rfind(marker)
    if pos == -1:
        return None

    crc_hex_start = pos + len(marker)
    crc_bytes = data[crc_hex_start : crc_hex_start + 8]
    if len(crc_bytes) < 8:
        return None

    try:
        stored_crc = int(crc_bytes, 16)
    except ValueError:
        return None

    return {
        "marker_pos":     pos,
        "crc_hex_start":  crc_hex_start,
        "crc_region_end": crc_hex_start,
        "stored_crc":     stored_crc,
        "crc_bytes_raw":  crc_bytes,
    }

def patch_firmware_crc(data: bytes, verbose: bool = False) -> bytes:
    """
    locates crc32_of_image=0x<hex> in the firmware tail, recalculates the
    crc using the bes algorithm (crc32_bes, init=0) over data[0..crc_hex_start],
    and writes the correct value back into the buffer.
    """
    info = find_crc_info(data)
    if info is None:
        if verbose:
            print("[!] crc32_of_image not found — skipping crc patch.")
        return data

    crc_hex_start = info["crc_hex_start"]
    region_end    = info["crc_region_end"]
    stored_crc    = info["stored_crc"]

    region   = data[:region_end]
    calc_crc = crc32_bes(region, init=0)

    if calc_crc == stored_crc:
        return data

    new_crc_ascii = f"{calc_crc:08X}".encode("ascii")
    buf = bytearray(data)
    buf[crc_hex_start : crc_hex_start + 8] = new_crc_ascii
    return bytes(buf)

# ──────────────────────────────────────────────── device discovery (windows) ─
# note: this path is only used on windows. discovery and sdp lookup go
# through pybluez, since those calls work fine even on newer python. only
# the actual rfcomm connect is broken on windows in pybluez, so that part
# uses a native stdlib socket instead (see the transport section below).

def scan_devices_by_name(name_filter: str, duration: int = DEFAULT_SCAN_TO):
    print(f"scanning for bluetooth devices matching \"{name_filter}\" ({duration}s)...")
    devices = bluetooth.discover_devices(duration=duration, lookup_names=True, flush_cache=True)
    nf = name_filter.lower()
    return [(addr, name) for addr, name in devices if name and nf in name.lower()]

def select_device_interactive(name_filter: str, duration: int = DEFAULT_SCAN_TO) -> str:
    matches = scan_devices_by_name(name_filter, duration)

    if not matches:
        sys.exit("no matching devices found.")

    if len(matches) == 1:
        addr, name = matches[0]
        print(f"found: {name} [{addr}]")
        return addr

    print("\nmultiple devices found:")
    for i, (addr, name) in enumerate(matches, 1):
        print(f"  {i}. {name}  [{addr}]")

    while True:
        choice = input(f"select a device (1-{len(matches)}): ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(matches):
                return matches[idx][0]
        except ValueError:
            pass
        print("invalid selection.")

# ──────────────────────────────────────────────── device address (linux) ─────
# no scanning on linux - it never reliably worked here. instead, just ask
# for the headphones' mac address directly and use it as-is.

def prompt_for_mac_address() -> str:
    while True:
        addr = input("enter the headphones' bluetooth mac address (xx:xx:xx:xx:xx:xx): ").strip()
        if MAC_RE.match(addr):
            return addr.upper()
        print("invalid mac address format.")

# ──────────────────────────────────────────────── sdp lookup ─────────────────

def find_besota_port(address: str) -> int:
    services = bluetooth.find_service(uuid=BESOTA_SERVICE_UUID, address=address)
    if not services:
        sys.exit("besota service (uuid 66666666-...) not found on this device.")
    return services[0]["port"]

# ──────────────────────────────────────────────── bluetooth transport ────────
# actual connection/io is done through a plain stdlib socket, not through
# pybluez's own connect()/send()/recv() wrappers, since those are broken on
# windows with modern python (removed c api format codes -> SystemError).

def make_socket() -> socket.socket:
    if IS_WINDOWS:
        try:
            return socket.socket(AF_BTH, socket.SOCK_STREAM, BTHPROTO_RFCOMM)
        except OSError as e:
            sys.exit(f"cannot create bluetooth socket: {e}")
    else:
        return bluetooth.BluetoothSocket(bluetooth.RFCOMM)

def bt_connect(sock: socket.socket, address: str, port: int) -> None:
    try:
        sock.connect((address, port))
    except OSError as e:
        sys.exit(f"connection failed: {e}")

def recv_packet(sock: socket.socket, cmd: int, min_len: int,
                timeout: float = RECV_TIMEOUT) -> bytes:
    buf      = b""
    deadline = time.monotonic() + timeout
    sock.settimeout(0.5)
    try:
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("device closed connection")
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

    raise TimeoutError(
        f"no {cmd:#04x} response in {timeout:.0f}s (buf={buf.hex() or 'empty'})"
    )

def wait_flow_ctrl(sock: socket.socket, pkt_idx: int) -> None:
    deadline = time.monotonic() + RECV_TIMEOUT
    buf      = b""
    sock.settimeout(0.3)
    try:
        while time.monotonic() < deadline:
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

    raise TimeoutError(f"no flow-control ack (0x8b) after chunk #{pkt_idx}")

# ──────────────────────────────────────────── protocol stages ────────────────

def stage_handshake_and_init(sock: socket.socket) -> None:
    sock.sendall(bytes([CMD_HS_REQ]) + MAGIC)
    recv_packet(sock, CMD_HS_RESP, min_len=9)

    sock.sendall(bytes([CMD_INIT1_REQ, 0x00]))
    recv_packet(sock, CMD_INIT1_RESP, min_len=2)

    payload_8c = bytes.fromhex("00"*32 + "010203040496cefd")
    sock.sendall(bytes([CMD_INIT2_REQ]) + MAGIC + payload_8c)
    recv_packet(sock, CMD_INIT2_RESP, min_len=10)

def stage_metadata(sock: socket.socket, file_size: int, file_crc: int) -> int:
    req = (
        bytes([CMD_META_REQ]) + MAGIC
        + struct.pack("<I", file_size)
        + struct.pack("<I", file_crc)
    )
    sock.sendall(req)

    resp   = recv_packet(sock, CMD_META_RESP, min_len=7)
    status = struct.unpack_from("<H", resp, 5)[0]
    if status != 0x0001:
        raise RuntimeError(f"device rejected firmware (status={status:#06x}).")

    chunk_size = DEFAULT_CHUNK
    for offset in (9, 7):
        if len(resp) >= offset + 2:
            mtu = struct.unpack_from("<H", resp, offset)[0]
            if 128 <= mtu <= 65535:
                chunk_size = mtu - 16
                break

    return chunk_size

def stage_erase(sock: socket.socket) -> None:
    # exact 93-byte erase payload from capture. targets ota_code_offset = 0x18000
    erase_payload = bytes.fromhex("5800000000800100" + "00"*80 + "cb5dee3f")
    sock.sendall(bytes([CMD_ERASE_REQ]) + erase_payload)

    resp = recv_packet(sock, CMD_ERASE_RESP, min_len=2, timeout=ERASE_TIMEOUT)
    if not (len(resp) >= 2 and resp[1] == 0x01):
        raise RuntimeError(f"unexpected erase response: {resp[:8].hex()}")

def _crc_checkpoint(sock: socket.socket, crc: int, chk_num: int) -> None:
    sock.sendall(bytes([CMD_CRC_REQ]) + MAGIC + struct.pack("<I", crc))
    resp = recv_packet(sock, CMD_CRC_RESP, min_len=2, timeout=10.0)
    if len(resp) < 2 or resp[1] != 0x01:
        raise RuntimeError(f"crc mismatch at checkpoint #{chk_num}!")

def stage_transfer(sock: socket.socket, firmware: bytes, chunk_size: int) -> None:
    total       = len(firmware)
    offset      = 0
    pkt_idx     = 0
    block_start = 0
    t0          = time.monotonic()

    while offset < total:
        chunk = firmware[offset : offset + chunk_size]
        sock.sendall(bytes([CMD_DATA]) + chunk)
        wait_flow_ctrl(sock, pkt_idx)

        offset  += len(chunk)
        pkt_idx += 1

        elapsed = time.monotonic() - t0 or 0.001
        speed   = offset / 1024 / elapsed
        eta     = (total - offset) / (offset / elapsed) if offset else 0
        pct     = offset / total * 100
        filled  = int(32 * offset / total)
        bar     = "█" * filled + "░" * (32 - filled)
        print(
            f"\r    [{bar}] {pct:5.1f}%  "
            f"{offset//1024}/{total//1024} kb  "
            f"{speed:.1f} kb/s  eta {int(eta)}s   ",
            end="", flush=True
        )

        if pkt_idx % CHECKPOINT_N == 0:
            _crc_checkpoint(
                sock,
                crc32(firmware[block_start:offset]),
                pkt_idx // CHECKPOINT_N
            )
            block_start = offset

    if block_start < total:
        _crc_checkpoint(
            sock,
            crc32(firmware[block_start:]),
            pkt_idx // CHECKPOINT_N + 1
        )

    print()  # newline after the progress bar

def stage_apply(sock: socket.socket) -> None:
    print("\nsending finalize packet...")

    sock.sendall(bytes([CMD_APPLY]))
    recv_packet(sock, CMD_APPLY_RESP, min_len=2)

    sock.sendall(bytes([CMD_FINISH]) + MAGIC)
    recv_packet(sock, CMD_FINISH_RESP, min_len=2)

# ──────────────────────────────────────────────────────────── main logic ─────

def flash(address: str, firmware_path: str) -> None:
    path = Path(firmware_path)
    if not path.exists():
        sys.exit(f"file not found: {firmware_path}")

    firmware = bytearray(path.read_bytes())

    firmware = check_firmware_header(firmware)
    check_not_ota_boot_image(firmware)

    firmware = patch_firmware_crc(bytes(firmware), verbose=False)

    file_size = len(firmware)
    file_crc  = crc32(firmware)

    port = find_besota_port(address)
    sock = make_socket()
    bt_connect(sock, address, port)

    try:
        sock.settimeout(RECV_TIMEOUT)
        print("connected\n")

        stage_handshake_and_init(sock)
        chunk_size = stage_metadata(sock, file_size, file_crc)
        stage_erase(sock)
        stage_transfer(sock, firmware, chunk_size)
        stage_apply(sock)

        print("\ndone!")

    except KeyboardInterrupt:
        print("\n\ninterrupted by user.")
    except (TimeoutError, ConnectionError, RuntimeError) as exc:
        print(f"\nfatal: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        try:
            sock.close()
        except Exception:
            pass

# ──────────────────────────────────────────────────────────── cli ────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="besota flasher — BES chips flasher build 1!",
        epilog="example: python besota.py firmware_patched.bin --address aa:bb:cc:dd:ee:ff"
    )
    ap.add_argument("firmware", help="path to .bin firmware file to flash")
    ap.add_argument("--name", help="bluetooth device name (or part of it) to scan for (windows only)")
    ap.add_argument("--address", help="bluetooth mac address to connect to (required on linux)")
    ap.add_argument("--scan-timeout", type=int, default=DEFAULT_SCAN_TO,
                    help=f"bluetooth scan duration in seconds, windows only (default: {DEFAULT_SCAN_TO})")
    args = ap.parse_args()

    if IS_WINDOWS:
        if args.address:
            target_address = args.address
        else:
            name = args.name or input("enter device name (or part of it) to search for: ").strip()
            if not name:
                sys.exit("device name cannot be empty.")
            target_address = select_device_interactive(name, args.scan_timeout)
    else:
        target_address = args.address or prompt_for_mac_address()

    flash(target_address, args.firmware)
