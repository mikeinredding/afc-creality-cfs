# AFCProject Automated Filament Changer Software
#
# Creality CFS (Color Filament System) unit for AFC.
#
# The CFS is a closed box: it moves filament itself and reports its own
# sensors over RS-485 (a CH340 USB adapter is built into its cable). Klipper
# never drives a CFS motor directly. This unit:
#   - owns the serial link to one box (non-blocking, reactor-driven),
#   - polls the box and mirrors per-slot filament presence into AFC lanes,
#   - runs tool load/unload for its lanes. Lanes are plain [AFC_lane]
#     sections (unit: <name>:<1-4>); this unit sets their custom load/unload
#     commands, so Mainsail/Fluidd/GrumpyScreen see them as ordinary lanes.
#
# STATUS: experimental. Protocol framing is validated against real captured
# traffic; the load/unload sequences follow tomaspakosta/k1c-cfs-klipper's
# hardware-validated steps. This integration itself still needs supervised
# testing on a real printer -- see docs/CFS.md for the staged test plan.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import errno
import json
import os
import queue
import struct
import sys
import termios
import threading
import time
import traceback
from typing import TYPE_CHECKING, Any, Dict, List, NamedTuple, Optional

import configfile

CONFIG_ERROR = getattr(configfile, "error", Exception)

try:
    from extras.AFC_utils import ERROR_STR
except Exception:
    raise CONFIG_ERROR(
        "Error when trying to import AFC_utils.ERROR_STR\n{}".format(traceback.format_exc())
    )

try:
    from extras.AFC_unit import afcUnit
except Exception:
    raise CONFIG_ERROR(ERROR_STR.format(import_lib="AFC_unit", trace=traceback.format_exc()))

try:
    from extras.AFC_lane import AFCLaneState, AFCMoveWarning
except Exception:
    raise CONFIG_ERROR(ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc()))

try:
    from extras.AFC_respond import AFCprompt
except Exception:
    raise CONFIG_ERROR(ERROR_STR.format(import_lib="AFC_respond", trace=traceback.format_exc()))



# =========================================================================
# Wire protocol
#
# Framing, CRC8, function codes and payload helpers. Plain Python, no Klipper
# imports. The rest of this file refers to these as P.<name>.
#
# Protocol knowledge comes from the community reverse-engineering work of:
#   - tomaspakosta/k1c-cfs-klipper   (hardware-validated client, PROTOCOL.md)
#   - ityshchenko/klipper-cfs        (CRC8/framing + real captured traffic)
#   - gitstonelabs/creality-cfs-klipper, FrederickAlt's K1 CFS docs,
#     fake-name/cfs-reverse-engineering, Jacob10383/k2-plus-custom-firmware
# All GPL-3.0, same as this project.
# =========================================================================

DEFAULT_BAUD = 230400

FRAME_HEAD = 0xF7
# Request "status" byte: 0xFF for normal box functions, 0x00 for the
# auto-addressing family (0xA0-0xA3).
REQ_STATUS_NORMAL = 0xFF
REQ_STATUS_ADDRESSING = 0x00

BROADCAST_BOXES = 0xFE
BROADCAST_ALL = 0xFF

# Function codes (numbering validated against real hardware by tomaspakosta).
GET_RFID = 0x02
GET_REMAIN_LEN = 0x03
SET_BOX_MODE = 0x04
GET_BUFFER_STATE = 0x05
CTRL_CONNECTION_MOTOR_ACTION = 0x07
GET_FILAMENT_SENSOR_STATE = 0x08
GET_BOX_STATE = 0x0A
SET_PRE_LOADING = 0x0D
GET_MEASURING_WHEEL = 0x0E
TIGHTEN_UP_ENABLE = 0x0F
EXTRUDE_PROCESS = 0x10
RETRUDE_PROCESS = 0x11
GET_VERSION_SN = 0x14
GET_HARDWARE_STATUS = 0x15
MOVE_DISTANCE = 0x31
CMD_SET_SLAVE_ADDR = 0xA0
CMD_GET_SLAVE_INFO = 0xA1
CMD_ONLINE_CHECK = 0xA2
CMD_GET_ADDR_TABLE = 0xA3

FN_NAMES = {
    GET_RFID: "GET_RFID",
    GET_REMAIN_LEN: "GET_REMAIN_LEN",
    SET_BOX_MODE: "SET_BOX_MODE",
    GET_BUFFER_STATE: "GET_BUFFER_STATE",
    CTRL_CONNECTION_MOTOR_ACTION: "CTRL_CONNECTION_MOTOR_ACTION",
    GET_FILAMENT_SENSOR_STATE: "GET_FILAMENT_SENSOR_STATE",
    GET_BOX_STATE: "GET_BOX_STATE",
    SET_PRE_LOADING: "SET_PRE_LOADING",
    GET_MEASURING_WHEEL: "GET_MEASURING_WHEEL",
    TIGHTEN_UP_ENABLE: "TIGHTEN_UP_ENABLE",
    EXTRUDE_PROCESS: "EXTRUDE_PROCESS",
    RETRUDE_PROCESS: "RETRUDE_PROCESS",
    GET_VERSION_SN: "GET_VERSION_SN",
    GET_HARDWARE_STATUS: "GET_HARDWARE_STATUS",
    MOVE_DISTANCE: "MOVE_DISTANCE",
    CMD_SET_SLAVE_ADDR: "CMD_SET_SLAVE_ADDR",
    CMD_GET_SLAVE_INFO: "CMD_GET_SLAVE_INFO",
    CMD_ONLINE_CHECK: "CMD_ONLINE_CHECK",
    CMD_GET_ADDR_TABLE: "CMD_GET_ADDR_TABLE",
}

# Response status codes actually observed on hardware (not exhaustive).
ST_OK = 0x00
ST_PARAMS_ERR = 0x01
ST_CRC_ERR = 0x02
ST_STATE_ERR = 0x03
ST_LENGTH_ERR = 0x04
ST_EXTRUDE_ERR4 = 0x08
ST_EXTRUDE_ERR8 = 0x0C
ST_EXTRUDE_ERR10 = 0x0D
ST_RETRUDE_DONE = 0x12  # treated as "retrude finished" by the stock firmware's recovery path
ST_RETRUDE_ERR2 = 0x13
ST_RETRUDE_ERR7 = 0x1A
ST_UPDATE_STATE = 0x30  # informational, not an error

STATUS_NAMES = {
    ST_OK: "OK",
    ST_PARAMS_ERR: "PARAMS_ERR",
    ST_CRC_ERR: "CRC_ERR",
    ST_STATE_ERR: "STATE_ERR",
    ST_LENGTH_ERR: "LENGTH_ERR",
    ST_EXTRUDE_ERR4: "EXTRUDE_ERR4",
    ST_EXTRUDE_ERR8: "EXTRUDE_ERR8",
    ST_EXTRUDE_ERR10: "EXTRUDE_ERR10",
    ST_RETRUDE_DONE: "RETRUDE_DONE",
    ST_RETRUDE_ERR2: "RETRUDE_ERR2",
    ST_RETRUDE_ERR7: "RETRUDE_ERR7",
    ST_UPDATE_STATE: "UPDATE_STATE",
}

# SET_BOX_MODE mode byte
MODE_PRINT = 0x00
MODE_IDLE = 0x01

# CTRL_CONNECTION_MOTOR_ACTION action byte
CONN_STOP = 0x00
CONN_EXTRUDE = 0x01
CONN_RETRUDE = 0x02

# SET_PRE_LOADING action byte
# GET_BOX_STATE data[3]: 0x02 while a slot is in print mode (SET_BOX_MODE
# [slot][PRINT]), 0x00 in feed/change mode
BOX_STATE_PRINT_LOCKED = 0x02

PRELOAD_CLOSE = 0x00
PRELOAD_OPEN = 0x01
PRELOAD_RUN = 0x02
PRELOAD_TIGHT = 0x03

# GET_FILAMENT_SENSOR_STATE banks
SENSOR_BANK_MATERIAL = 0x00
SENSOR_BANK_CONNECTIONS = 0x01

# GET_BUFFER_STATE values
BUFFER_MIDDLE = 0x00
BUFFER_FULL = 0x01
BUFFER_EMPTY = 0x02

# Slot bits, physical left-to-right. AFC lane index 1..4 maps to A..D.
SLOT_BITS = (0x01, 0x02, 0x04, 0x08)
SLOT_LETTERS = ("A", "B", "C", "D")
SLOT_ALL = 0x0F

# Upper bound on a sane length byte. Real frames seen are < 0x20; anything
# huge means we've lost sync and should resync on the next 0xF7.
MAX_LENGTH = 0x80


class Frame(NamedTuple):
    addr: int
    status: int
    fn: int
    data: bytes

    @property
    def ok(self) -> bool:
        return self.status == ST_OK

    def status_name(self) -> str:
        return status_name(self.status)

    def hex(self) -> str:
        return encode_frame(self.addr, self.status, self.fn, self.data).hex()


def status_name(code: Optional[int]) -> str:
    if code is None:
        return "NO_REPLY"
    return STATUS_NAMES.get(code, "0x%02X" % code)


def fn_name(fn: int) -> str:
    return FN_NAMES.get(fn, "0x%02X" % fn)


def crc8(data: bytes) -> int:
    """CRC-8, polynomial 0x07, no init/xorout."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) if crc & 0x80 else (crc << 1)
            crc &= 0xFF
    return crc


def encode_frame(addr: int, status: int, fn: int, data: bytes = b"") -> bytes:
    """
    head(0xF7) addr len status fn data... crc8

    len counts status + fn + data + crc. The CRC covers len..data.
    """
    data = bytes(data)
    length = 3 + len(data)
    body = bytes([length, status & 0xFF, fn & 0xFF]) + data
    return bytes([FRAME_HEAD, addr & 0xFF]) + body + bytes([crc8(body)])


class FrameParser:
    """
    Incremental parser for the RX byte stream. Feed it whatever the serial
    port produced; it returns every complete, CRC-valid frame and silently
    resyncs past garbage. Bad-CRC frames are counted, not returned.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.crc_errors = 0
        self.discarded_bytes = 0

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, chunk: bytes) -> List[Frame]:
        self._buf.extend(chunk)
        frames: List[Frame] = []
        buf = self._buf
        while True:
            head = buf.find(FRAME_HEAD)
            if head < 0:
                self.discarded_bytes += len(buf)
                buf.clear()
                break
            if head > 0:
                self.discarded_bytes += head
                del buf[:head]
            if len(buf) < 3:
                break
            length = buf[2]
            if length < 3 or length > MAX_LENGTH:
                # Not a real header, skip this 0xF7 and look for the next one
                self.discarded_bytes += 1
                del buf[0]
                continue
            total = 3 + length
            if len(buf) < total:
                break
            raw = bytes(buf[:total])
            if crc8(raw[2:-1]) != raw[-1]:
                self.crc_errors += 1
                self.discarded_bytes += 1
                del buf[0]
                continue
            del buf[:total]
            frames.append(Frame(addr=raw[1], status=raw[3], fn=raw[4], data=raw[5:-1]))
        return frames


def decode_float_be(data: bytes) -> Optional[float]:
    """Measuring wheel / stage-5 telemetry: 4-byte big-endian IEEE-754 float."""
    if len(data) < 4:
        return None
    return struct.unpack(">f", bytes(data[:4]))[0]


def slot_bit(index: int) -> int:
    """AFC lane index (1..4) -> CFS slot bit."""
    if index < 1 or index > 4:
        raise ValueError("CFS slot index must be 1-4, got {}".format(index))
    return SLOT_BITS[index - 1]


def slot_letter(index: int) -> str:
    return SLOT_LETTERS[index - 1]


def bits_to_slots(mask: int) -> List[int]:
    """Bitmask -> list of 1-based slot indices."""
    return [i + 1 for i, bit in enumerate(SLOT_BITS) if mask & bit]


def decode_ascii(data: bytes) -> Optional[str]:
    try:
        return bytes(data).decode("ascii").strip("\x00")
    except UnicodeDecodeError:
        return None


# --- RFID tags (GET_RFID) ----------------------------------------------------
# The box answers GET_RFID [slot_bits] with ASCII "<letter>:<record>;" per
# slot, "<letter>:unknown;" when it has no tag for it. The record is Creality's
# 40-character tag format (community documented, verified on a real tag):
#   date 5 | vendor 4 | batch 2 | filament id 6 ('1' + 5-digit material code)
#   | color 7 ('0' + RRGGBB) | length 4 | serial 6 | reserved 6
# Jacob10383's K2 firmware treats an integer > 1 in "reserved" as a Spoolman
# spool ID; tag writers can put one there.

RFID_RECORD_LEN = 40

# Length code -> spool weight in grams
RFID_LENGTH_GRAMS = {"0330": 1000, "0247": 750, "0198": 600, "0165": 500, "0082": 250}

# Creality material codes -> AFC material. Only codes checked against a real
# tag or the published catalogue; add others with rfid_materials.
RFID_MATERIALS = {
    "00001": "PLA",      # Generic PLA
    "00002": "PLA",      # Generic PLA-Silk
    "01001": "PLA",      # Hyper PLA
}


def parse_rfid_reply(data: bytes) -> Dict[int, Optional[str]]:
    """GET_RFID reply -> {slot index (1-4): 40-char record, or None if unknown}."""
    text = decode_ascii(data) or ""
    result: Dict[int, Optional[str]] = {}
    for part in text.split(";"):
        part = part.strip()
        if len(part) < 2 or part[1] != ":" or part[0] not in SLOT_LETTERS:
            continue
        index = SLOT_LETTERS.index(part[0]) + 1
        record = part[2:]
        result[index] = record if len(record) == RFID_RECORD_LEN and record.isalnum() else None
    return result


def decode_rfid_record(record: str, materials: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Split a 40-character tag record into its fields."""
    if len(record) != RFID_RECORD_LEN:
        raise ValueError("RFID record must be {} characters".format(RFID_RECORD_LEN))
    table = dict(RFID_MATERIALS)
    table.update(materials or {})
    material_code = record[12:17]
    color = record[18:24].upper()
    reserved = record[34:40]
    spool_id = int(reserved) if reserved.isdigit() and int(reserved) > 1 else None
    return {
        "record": record,
        "date": record[0:5],
        "vendor": record[5:9],
        "batch": record[9:11],
        "material_code": material_code,
        "material": table.get(material_code),
        "color": color if all(c in "0123456789ABCDEF" for c in color) else None,
        "length_code": record[24:28],
        "weight": RFID_LENGTH_GRAMS.get(record[24:28]),
        "serial": record[28:34],
        "spool_id": spool_id,
    }


# The protocol names above, as P.<name>
P = sys.modules[__name__]

if TYPE_CHECKING:
    from gcode import GCodeCommand
    from extras.AFC_lane import AFCLane


class CFSTrace:
    """
    Optional JSON-lines trace of everything the unit does with the box: every
    frame sent and received (with reply latency), timeouts, connection
    changes, and load/unload/cut events with timings. Used to debug and tune
    the integration from real prints (summarize with tools/cfs_trace_summary.py).

    Writes happen on a background thread so a slow SD/eMMC write can never
    stall Klipper's reactor; the reactor side only appends to a bounded queue.
    The file rotates at max_bytes, keeping `backups` older copies.
    """

    QUEUE_SIZE = 20000

    def __init__(self, path: Optional[str], max_bytes: int, backups: int,
                 logger: Any) -> None:
        self.path = os.path.expanduser(path) if path else None
        self.max_bytes = max_bytes
        self.backups = backups
        self.logger = logger
        self.enabled = False
        self.dropped = 0
        self.written = 0
        self.last_error: Optional[str] = None
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=self.QUEUE_SIZE)
        self._thread: Optional[threading.Thread] = None
        self._t0 = time.monotonic()

    def start(self) -> bool:
        if not self.path:
            return False
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="cfs-trace", daemon=True)
            self._thread.start()
        self.enabled = True
        return True

    def stop(self, timeout: float = 2.0) -> None:
        self.enabled = False
        if self._thread is not None and self._thread.is_alive():
            try:
                self._queue.put(None, timeout=timeout)
            except queue.Full:
                pass
            self._thread.join(timeout)
        self._thread = None

    def event(self, ev: str, **fields: Any) -> None:
        """Record one event. Cheap and non-blocking; safe from the reactor."""
        if not self.enabled:
            return
        now = time.time()
        record = {"ts": round(now, 4), "t": round(time.monotonic() - self._t0, 4), "ev": ev}
        record.update(fields)
        try:
            line = json.dumps(record, separators=(",", ":"), default=str)
            self._queue.put_nowait(line)
        except queue.Full:
            self.dropped += 1
        except (TypeError, ValueError):
            self.dropped += 1

    # -- writer thread ----------------------------------------------------

    def _open(self):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        handle = open(self.path, "a", encoding="utf-8")
        self._size = os.path.getsize(self.path)
        return handle

    def _rotate(self, handle):
        handle.close()
        for i in range(self.backups, 0, -1):
            src = self.path if i == 1 else "{}.{}".format(self.path, i - 1)
            dst = "{}.{}".format(self.path, i)
            if os.path.exists(src):
                os.replace(src, dst)
        if self.backups == 0 and os.path.exists(self.path):
            os.remove(self.path)
        return self._open()

    def _run(self) -> None:
        try:
            handle = self._open()
        except OSError as e:
            self.last_error = "cannot open trace file: {}".format(e)
            self.enabled = False
            return
        try:
            while True:
                line = self._queue.get()
                if line is None:
                    break
                handle.write(line + "\n")
                self.written += 1
                self._size += len(line.encode("utf-8")) + 1
                if self._size >= self.max_bytes:
                    handle = self._rotate(handle)
                elif self._queue.empty():
                    handle.flush()
        except OSError as e:
            self.last_error = "trace write failed: {}".format(e)
            self.enabled = False
        finally:
            try:
                handle.flush()
                handle.close()
            except Exception:
                pass


class CFSTransport:
    """
    Reactor-safe request/response link to a CFS box.

    Nothing here blocks the reactor: the port is opened O_NONBLOCK, received
    bytes arrive through reactor.register_fd(), and request() waits on a
    reactor completion. It must be called from a greenlet that may pause
    (gcode handlers and timer callbacks both qualify).
    """

    def __init__(self, reactor: Any, path: str, baud: int, logger: Any,
                 name: str, debug: bool = False) -> None:
        self.reactor = reactor
        self.path = path
        self.baud = baud
        self.logger = logger
        self.name = name
        self.debug = debug

        self.fd: Optional[int] = None
        self._fd_handle = None
        self._parser = P.FrameParser()
        self._mutex = reactor.mutex()
        self._pending: Optional[Dict[str, Any]] = None
        self.last_error: Optional[str] = None
        self.tx_count = 0
        self.rx_count = 0
        self.timeouts = 0
        self.trace: Optional[CFSTrace] = None
        self._traced_crc_errors = 0
        # The box handles one command at a time and answers motor commands
        # only once the motion finishes. Requests that timed out are still
        # being processed; their replies arrive late. Count them and wait for
        # them before sending anything else, so nothing queues up behind a
        # busy box and a late reply can't be taken as the answer to a newer
        # request.
        self.resync_timeout = 15.0
        self._outstanding = 0
        self._drain: Optional[Any] = None

    def _trace(self, ev: str, **fields: Any) -> None:
        if self.trace is not None:
            self.trace.event(ev, **fields)

    # -- port management ---------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.fd is not None

    def open(self) -> bool:
        if self.fd is not None:
            return True
        baud_const = getattr(termios, "B{}".format(self.baud), None)
        if baud_const is None:
            self.last_error = "unsupported baud {}".format(self.baud)
            return False
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as e:
            self.last_error = "open {} failed: {}".format(self.path, e)
            self._trace("port_error", error=self.last_error)
            return False
        try:
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0                                    # iflag: raw
            attrs[1] = 0                                    # oflag: raw
            cflag = attrs[2]
            cflag &= ~(termios.PARENB | termios.CSTOPB | termios.CSIZE)
            cflag &= ~getattr(termios, "CRTSCTS", 0)
            cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[2] = cflag
            attrs[3] = 0                                    # lflag: raw
            attrs[4] = baud_const                           # ispeed
            attrs[5] = baud_const                           # ospeed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIOFLUSH)
        except termios.error as e:
            # A pty used for testing may reject some attributes; that's fine.
            self.logger.debug("{}: termios setup warning: {}".format(self.name, e))
        self.fd = fd
        self._parser.reset()
        self._fd_handle = self.reactor.register_fd(fd, self._on_readable)
        self.last_error = None
        self._trace("port_open", path=self.path, baud=self.baud)
        return True

    def close(self) -> None:
        if self._fd_handle is not None:
            try:
                self.reactor.unregister_fd(self._fd_handle)
            except Exception:
                pass
            self._fd_handle = None
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        pending = self._pending
        if pending is not None and not pending["completion"].test():
            pending["completion"].complete(None)
        self._outstanding = 0
        if self._drain is not None and not self._drain.test():
            self._drain.complete(False)

    # -- I/O ---------------------------------------------------------------

    def _on_readable(self, eventtime: float) -> None:
        if self.fd is None:
            return
        chunks = []
        while True:
            try:
                chunk = os.read(self.fd, 4096)
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                # Device went away (USB unplugged, box powered off)
                self.last_error = "read error: {}".format(e)
                self.logger.warning("{}: serial {}".format(self.name, self.last_error))
                self._trace("port_error", error=self.last_error)
                self.close()
                return
            if not chunk:
                break
            chunks.append(chunk)
        if not chunks:
            return
        for frame in self._parser.feed(b"".join(chunks)):
            self.rx_count += 1
            if self.debug:
                self.logger.debug("{}: RX {} {} {}".format(
                    self.name, P.fn_name(frame.fn), frame.status_name(), frame.data.hex()))
            pending = self._pending
            if pending is not None and frame.fn == pending["fn"] \
                    and (pending["addr"] == P.BROADCAST_BOXES or frame.addr == pending["addr"]) \
                    and not pending["completion"].test():
                if not pending["quiet"]:
                    self._trace("rx", fn=P.fn_name(frame.fn), addr=frame.addr,
                                st=frame.status_name(), data=frame.data.hex(),
                                ms=round((self.reactor.monotonic() - pending["sent"]) * 1000, 1))
                pending["completion"].complete(frame)
            elif self._outstanding > 0:
                # Reply to a request we had already given up on
                self._outstanding -= 1
                self._trace("late_reply", fn=P.fn_name(frame.fn), addr=frame.addr,
                            st=frame.status_name(), data=frame.data.hex(),
                            still_outstanding=self._outstanding)
                if self._outstanding == 0 and self._drain is not None \
                        and not self._drain.test():
                    self._drain.complete(True)
            else:
                self._trace("rx_unsolicited", fn=P.fn_name(frame.fn), addr=frame.addr,
                            st=frame.status_name(), data=frame.data.hex())
                if self.debug:
                    self.logger.debug("{}: unsolicited frame {}".format(self.name, frame.hex()))
        if self._parser.crc_errors != self._traced_crc_errors:
            self._trace("crc_error", total=self._parser.crc_errors,
                        new=self._parser.crc_errors - self._traced_crc_errors)
            self._traced_crc_errors = self._parser.crc_errors

    def _write_all(self, data: bytes) -> bool:
        view = memoryview(data)
        deadline = self.reactor.monotonic() + 0.5
        while view:
            if self.fd is None:
                return False
            try:
                written = os.write(self.fd, view)
                view = view[written:]
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    self.last_error = "write error: {}".format(e)
                    self.close()
                    return False
                now = self.reactor.monotonic()
                if now > deadline:
                    self.last_error = "write timeout"
                    return False
                self.reactor.pause(now + 0.002)
        return True

    def request(self, addr: int, fn: int, data: bytes = b"",
                status: int = P.REQ_STATUS_NORMAL, timeout: float = 1.0,
                retries: int = 1, motor: bool = False,
                quiet: bool = False) -> Optional[P.Frame]:
        """
        Send one request and wait (cooperatively) for the matching reply.
        Returns the reply frame, or None after all retries time out.

        motor=True marks a command the box only answers after a motion
        finishes. If one of those times out the box is still busy with it, so
        the next request first waits for its late reply. A non-motor request
        that times out is treated as lost (box gone, frame dropped): nothing
        to wait for.

        quiet=True leaves the tx/rx frames out of the trace (idle status
        polls); timeouts are always traced.
        """
        with self._mutex:
            for attempt in range(retries + 1):
                if self.fd is None and not self.open():
                    return None
                if self._outstanding:
                    self._wait_for_late_replies()
                frame = P.encode_frame(addr, status, fn, data)
                completion = self.reactor.completion()
                self._pending = {"fn": fn, "addr": addr, "quiet": quiet,
                                 "completion": completion,
                                 "sent": self.reactor.monotonic()}
                if self.debug:
                    self.logger.debug("{}: TX {} {}".format(self.name, P.fn_name(fn), frame.hex()))
                if not self._write_all(frame):
                    self._pending = None
                    self._trace("tx_failed", fn=P.fn_name(fn), error=self.last_error)
                    continue
                self.tx_count += 1
                self._pending["sent"] = self.reactor.monotonic()
                if not quiet:
                    self._trace("tx", fn=P.fn_name(fn), addr=addr, data=bytes(data).hex(),
                                attempt=attempt + 1)
                result = completion.wait(self.reactor.monotonic() + timeout)
                self._pending = None
                if result is not None:
                    return result
                self.timeouts += 1
                if motor:
                    self._outstanding += 1
                self._trace("timeout", fn=P.fn_name(fn), addr=addr, data=bytes(data).hex(),
                            attempt=attempt + 1, of=retries + 1, timeout_s=timeout)
                if self.debug:
                    self.logger.debug("{}: timeout {} (attempt {}/{})".format(
                        self.name, P.fn_name(fn), attempt + 1, retries + 1))
            return None

    def _wait_for_late_replies(self) -> None:
        """Block (cooperatively) until every timed-out request has been
        answered, or resync_timeout passes."""
        waiting = self._outstanding
        start = self.reactor.monotonic()
        self._drain = self.reactor.completion()
        done = self._drain.wait(start + self.resync_timeout)
        self._drain = None
        waited = round(self.reactor.monotonic() - start, 2)
        if done:
            self._trace("resync", waited_s=waited, late_replies=waiting)
        else:
            self._trace("resync_gave_up", waited_s=waited, still_outstanding=self._outstanding)
            self.logger.warning("{}: box never answered {} request(s); continuing".format(
                self.name, self._outstanding))
            self._outstanding = 0

    @property
    def busy(self) -> bool:
        return self._mutex.test()


class CFSBus:
    """
    One RS-485 chain: up to four CFS boxes daisy-chained behind one USB
    adapter (printer -> box 1 IN, box 1 OUT -> box 2 IN, ... -> buffer).
    Every [AFC_CFS] unit on the same serial port shares this bus and its
    transport, so requests to different boxes take turns on the wire.

    Boxes lose their address when they power off and answer broadcast
    discovery one at a time until addressed. Discovery gives each box the
    box_addr of the unit configured with its box_uid, so the lanes can't
    swap boxes when the discovery order changes.
    """

    _buses: Dict[str, "CFSBus"] = {}

    @classmethod
    def get(cls, reactor: Any, path: str, baud: int, logger: Any,
            debug: bool = False) -> "CFSBus":
        bus = cls._buses.get(path)
        # Klipper builds a new reactor on RESTART; don't reuse a stale bus
        if bus is None or bus.reactor is not reactor:
            bus = cls(reactor, path, baud, logger, debug)
            cls._buses[path] = bus
        elif bus.transport.baud != baud:
            raise ValueError("serial {}: every CFS on one chain must use the same baud".format(path))
        return bus

    def __init__(self, reactor: Any, path: str, baud: int, logger: Any, debug: bool) -> None:
        self.reactor = reactor
        self.logger = logger
        self.transport = CFSTransport(reactor, path, baud, logger,
                                      "CFS bus {}".format(path), debug=debug)
        self.units: List[Any] = []

    def register(self, unit: Any) -> None:
        for other in self.units:
            if other.box_addr == unit.box_addr:
                raise ValueError(
                    "CFS {} and {} are both box_addr {} on {}; give each box on a chain "
                    "its own box_addr (1-4)".format(other.name, unit.name, unit.box_addr,
                                                    self.transport.path))
            if unit.box_uid and other.box_uid == unit.box_uid:
                raise ValueError("CFS {} and {} have the same box_uid".format(other.name, unit.name))
        if self.units and (not unit.box_uid or not all(u.box_uid for u in self.units)):
            self.logger.warning(
                "CFS {}: more than one box on {} without box_uid set; boxes are then "
                "addressed in discovery order, which can change. Set box_uid on each "
                "(AFC_CFS_STATUS shows it).".format(unit.name, self.transport.path))
        self.units.append(unit)

    @property
    def operation_active(self) -> bool:
        """True while any box on this chain is in a load/unload sequence.
        Status polls of the other boxes must stay off the wire then: a poll
        squeezed in between a box's stage 5 polls can make that box miss
        the next one (seen with two boxes on a K1 Max)."""
        return any(u._operation_depth > 0 for u in self.units)

    def unit_for_uid(self, uid: bytes) -> Optional[Any]:
        for unit in self.units:
            if unit.box_uid and unit.box_uid == uid:
                return unit
        return None

    def discover(self, timeout: float = 1.5) -> List[tuple]:
        """
        Address every unaddressed box on the chain. Each GET_SLAVE_INFO
        broadcast is answered by one unaddressed box; giving it an address
        silences it, so repeat until nobody answers. Returns [(uid, addr)].
        """
        assigned: List[tuple] = []
        taken = set()
        for _ in range(4):
            reply = self.transport.request(P.BROADCAST_BOXES, P.CMD_GET_SLAVE_INFO,
                                           bytes([P.BROADCAST_BOXES, P.BROADCAST_BOXES]),
                                           status=P.REQ_STATUS_ADDRESSING,
                                           timeout=timeout, retries=1)
            if reply is None or len(reply.data) < 14:
                break
            uid = bytes(reply.data[2:14])
            unit = self.unit_for_uid(uid)
            if unit is None:
                # Single-box setups don't set box_uid: the one unit without a
                # uid that hasn't got a box yet takes it.
                free = [u for u in self.units if not u.box_uid and u.box_addr not in taken]
                unit = free[0] if free else None
            if unit is not None:
                addr = unit.box_addr
            else:
                used = {u.box_addr for u in self.units} | taken
                spare = [a for a in range(1, 5) if a not in used]
                if not spare:
                    self.logger.warning("CFS bus {}: no free address for box uid {}".format(
                        self.transport.path, uid.hex()))
                    break
                addr = spare[0]
                self.logger.warning(
                    "CFS bus {}: box uid {} isn't in any [AFC_CFS] section (box_uid); "
                    "parked at address {}".format(self.transport.path, uid.hex(), addr))
            self.transport.request(P.BROADCAST_BOXES, P.CMD_SET_SLAVE_ADDR,
                                   bytes([addr]) + uid, status=P.REQ_STATUS_ADDRESSING,
                                   timeout=timeout)
            taken.add(addr)
            assigned.append((uid, addr))
            self.logger.info("CFS bus {}: box uid {} -> address {}{}".format(
                self.transport.path, uid.hex(), addr,
                " ({})".format(unit.name) if unit is not None else ""))
        return assigned


def set_lane_sensors(lane, present: bool, connected_mask) -> None:
    """
    Map the box's sensors onto AFC's lane model for a virtual hub:
      - prep / loaded_to_hub: a spool is in the slot (the box keeps it
        staged, so it is always ready to load)
      - load sensor (_load_state): this slot's filament is past the merge,
        in the shared tube. AFC's virtual hub reads "occupied" when any
        lane's load sensor is set, so it must not follow spool presence.
    """
    lane.prep_state = present
    lane.loaded_to_hub = present
    lane._load_state = bool(connected_mask is not None
                            and connected_mask & P.slot_bit(lane.index))


# Stock tip-forming wiggle, (distance_mm, speed_mm_per_min). Nets about -0.5mm.
TIP_WIGGLE_STEPS = [(0.5, 600), (-5, 600), (2.5, 600), (-1.25, 600), (1.75, 600), (1, 60)]


class afcCFS(afcUnit):
    """
    One Creality CFS box. Lanes are plain [AFC_lane laneN] sections with
    unit: <name>:1..4 mapping to physical slots A..D (left to right).
    """

    CALIBRATION_UNSUPPORTED = "CFS lanes are fed by the box itself; there is nothing to calibrate."

    def __init__(self, config):
        super().__init__(config)
        self.type = config.get("type", "CFS")
        self.reactor = self.printer.get_reactor()
        self._load_cfs_config(config)

        if self.cutter_sensor_pin is not None:
            buttons = self.printer.load_object(config, "buttons")
            buttons.register_buttons([self.cutter_sensor_pin], self._cutter_callback)

        try:
            self._init_cfs_runtime()
        except ValueError as e:      # two units on one chain configured to clash
            raise config.error("[AFC_CFS {}]: {}".format(self.name, e))

        self.printer.register_event_handler("klippy:ready", self._cfs_handle_ready)
        self.printer.register_event_handler("klippy:disconnect", self._cfs_handle_disconnect)

        self.gcode.register_mux_command("AFC_CFS_STATUS", "UNIT", self.name,
                                        self.cmd_AFC_CFS_STATUS, desc=self.cmd_AFC_CFS_STATUS_help)
        self.gcode.register_mux_command("AFC_CFS_RECONNECT", "UNIT", self.name,
                                        self.cmd_AFC_CFS_RECONNECT, desc=self.cmd_AFC_CFS_RECONNECT_help)
        self.gcode.register_mux_command("AFC_CFS_RAW", "UNIT", self.name,
                                        self.cmd_AFC_CFS_RAW, desc=self.cmd_AFC_CFS_RAW_help)
        self.gcode.register_mux_command("AFC_CFS_TRACE", "UNIT", self.name,
                                        self.cmd_AFC_CFS_TRACE, desc=self.cmd_AFC_CFS_TRACE_help)

    def _load_cfs_config(self, config) -> None:
        """Read every CFS-specific option. Kept separate so tests can apply
        the real defaults without constructing a full AFC unit."""
        self.serial_path = config.get("serial")
        self.baud = config.getint("baud", P.DEFAULT_BAUD)
        self.box_addr = config.getint("box_addr", 1, minval=1, maxval=4)
        # 12-byte box ID (24 hex digits, AFC_CFS_STATUS shows it). Needed with
        # more than one box on a chain so each section always gets its box.
        uid = (config.get("box_uid", "") or "").replace(":", "").replace(" ", "").lower()
        if uid and (len(uid) != 24 or any(c not in "0123456789abcdef" for c in uid)):
            msg = "box_uid must be 24 hex digits, got '{}'".format(uid)
            raise config.error(msg) if hasattr(config, "error") else ValueError(msg)
        self.box_uid: Optional[bytes] = bytes.fromhex(uid) if uid else None
        self.poll_interval = config.getfloat("poll_interval", 1.0, minval=0.2)
        # Stock firmware sends CMD_ONLINE_CHECK every 1.5s (10s while printing);
        # without it the box shows its "no printer" light (red, cycling).
        self.heartbeat_interval = config.getfloat("heartbeat_interval", 1.5, minval=0.5)
        self.heartbeat_interval_printing = config.getfloat(
            "heartbeat_interval_printing", 10.0, minval=0.5)
        self.request_timeout = config.getfloat("request_timeout", 0.8, minval=0.05)
        # Motor commands (EXTRUDE/RETRUDE_PROCESS stages) are only answered once
        # the motion finishes. On a K1: EXTRUDE stage 0 ~3.7s, RETRUDE stage 1
        # (the full reel-back) ~12.4s; a K1 Max's longer tube takes longer.
        self.stage_timeout = config.getfloat("stage_timeout", 30.0, minval=1.0)
        # Generic RETRUDE_PROCESS check-ins during unload
        self.checkin_timeout = config.getfloat("checkin_timeout", 5.0, minval=0.2)
        # After a timeout, how long to wait for the box to finish the timed-out
        # command (its late reply) before sending anything else. Must cover the
        # slowest motor stage, so it is deliberately longer than stage_timeout.
        self.resync_timeout = config.getfloat("resync_timeout", 60.0, minval=1.0)
        self.offline_after = config.getint("offline_after_failures", 3, minval=1)
        self.reconnect_interval = config.getfloat("reconnect_interval", 5.0, minval=0.5)
        # The box starts answering a few seconds after power-on, often after
        # Klipper is already prepping lanes. PREP keeps trying this long.
        self.boot_wait = config.getfloat("boot_wait", 20.0, minval=0.)
        self.protocol_debug = config.getboolean("protocol_debug", False)

        # --- Trace recording (see docs/CFS.md, "Recording a trace") ---
        # JSON-lines record of box traffic and load/unload events. Off unless set.
        self.trace_file = config.get("trace_file", None)
        self.trace_max_size = config.getfloat("trace_max_size", 5.0, minval=0.1)   # MB
        self.trace_backups = config.getint("trace_backups", 1, minval=0, maxval=10)
        # Idle status polls run every poll_interval; leave them out of the trace
        # unless asked (presence changes are always recorded).
        self.trace_polls = config.getboolean("trace_polls", False)

        # --- Tool load sequence (EXTRUDE_PROCESS stages 0/4/5/6/7) ---
        self.load_attempts = config.getint("load_attempts", 2, minval=1)
        self.stage_settle_time = config.getfloat("stage_settle_time", 0.3, minval=0.)
        self.stage5_poll_interval = config.getfloat("stage5_poll_interval", 0.4, minval=0.05)
        # Stage 5 polls are answered at once (~20 ms) while the box feeds;
        # a missed one is just skipped, not waited for like a motor command
        self.stage5_reply_timeout = config.getfloat("stage5_reply_timeout", 2.0, minval=0.2)
        # Max time for the box to push filament from the spool to the toolhead sensor
        self.stage5_timeout = config.getfloat("stage5_timeout", 60.0, minval=5.)
        # Abort stage 5 early if the measuring wheel hasn't moved for this long
        self.stage5_stall_time = config.getfloat("stage5_stall_time", 8.0, minval=1.)
        self.stage5_stall_distance = config.getfloat("stage5_stall_distance", 2.0, minval=0.)
        # Toolhead-side prime moves interleaved with stages 6 and 7.
        # Stock firmware values: 10mm @ 35mm/min, then 5mm @ 10mm/min.
        self.prime1_length = config.getfloat("prime1_length", 10.0, minval=0.)
        self.prime1_speed = config.getfloat("prime1_speed", 35.0, above=0.)   # mm/min
        self.prime2_length = config.getfloat("prime2_length", 5.0, minval=0.)
        self.prime2_speed = config.getfloat("prime2_speed", 10.0, above=0.)   # mm/min
        self.stage7_arg = config.getint("stage7_arg", 3, minval=0, maxval=255)
        # Send SET_BOX_MODE [00][01] after the load to clear the EXTRUDE_ERR8 the
        # box latches. Those bytes are the feed/change-mode command and take the
        # slot back out of print mode, so the box stops feeding during the print.
        # Leave False; only for experiments.
        self.clear_mode_after_load = config.getboolean("clear_mode_after_load", False)

        # --- Tool unload sequence (cut, extruder retract, box reel-back) ---
        # Extruder retract steps after RETRUDE start, "distance:speed_mm_per_min",
        # pulling out of the gears before the box reels. 60mm total like stock's
        # Tn_retrude -60; the first 15mm slower while the cut end is still warm.
        self.unload_retract_steps = self._parse_steps(config.get(
            "unload_retract_steps", "15:600, 45:1800"))
        # Optional tip-forming wiggle before retracting. Not needed with a cutter.
        self.unload_tip_wiggle = config.getboolean("unload_tip_wiggle", False)
        self.unload_timeout = config.getfloat("unload_timeout", 30.0, minval=1.)
        self.cutter_wait_time = config.getfloat("cutter_wait_time", 3.0, minval=0.1)
        # On the K1 CFS kit the cutter switch only closes near the end of the
        # blade's travel, so seeing it fire during AFC_CUT proves the cut
        # completed. When True, a cut where it never fired is retried, then
        # fails the unload instead of retracting possibly uncut filament.
        self.cutter_require_trigger = config.getboolean("cutter_require_trigger", True)

        # Optional cutter switch on the CFS toolhead kit (Klipper MCU pin)
        self.cutter_sensor_pin = config.get("cutter_sensor_pin", None)

        # --- RFID tags (Creality format) ---
        # Read a spool's tag once when it goes into a slot and set the lane's
        # colour, material and weight (or its Spoolman spool, if the tag has
        # an ID). Never during a print. Off by default.
        self.rfid_read = config.getboolean("rfid_read", False)
        # Wait after the spool is detected, so the box finishes drawing it in
        self.rfid_insert_delay = config.getfloat("rfid_insert_delay", 10.0, minval=0.)
        # Turn the slot first (SET_PRE_LOADING RUN), like stock BOX_INFO_REFRESH.
        # With no tag the box can stay busy for about a minute.
        self.rfid_spin = config.getboolean("rfid_spin", True)
        self.rfid_busy_timeout = config.getfloat("rfid_busy_timeout", 90.0, minval=5.)
        self.rfid_read_attempts = config.getint("rfid_read_attempts", 3, minval=1)
        self.rfid_retry_interval = config.getfloat("rfid_retry_interval", 5.0, minval=0.)
        # Also read every full slot once after Klipper starts (like stock "read
        # RFID after Klipper starts"), for spools swapped while the printer was
        # off. Each slot is turned; an untagged one can keep the box busy for
        # about a minute. Off by default.
        self.rfid_read_at_startup = config.getboolean("rfid_read_at_startup", False)
        self.rfid_startup_delay = config.getfloat("rfid_startup_delay", 15.0, minval=0.)
        # Extra Creality material codes, "CODE=MATERIAL, ..." (e.g. 00003=PETG)
        self.rfid_materials = self._parse_materials(config.get("rfid_materials", ""))

    def _init_cfs_runtime(self) -> None:
        self.cutter_sensor_state = False
        self.cutter_trigger_count = 0
        self.bus = CFSBus.get(self.reactor, self.serial_path, self.baud, self.logger,
                              debug=self.protocol_debug)
        self.bus.register(self)
        self.transport = self.bus.transport
        self.transport.resync_timeout = max(self.transport.resync_timeout, self.resync_timeout)
        self.trace = CFSTrace(self.trace_file, int(self.trace_max_size * 1024 * 1024),
                              self.trace_backups, self.logger)
        # The wire trace goes to the first unit on the chain that records one
        if self.trace_file and (self.transport.trace is None or not self.transport.trace.enabled):
            self.transport.trace = self.trace
        self.seen_uid: Optional[bytes] = None
        # Set at connect for a box without its own cutter_sensor_pin
        self.cutter_source: Optional[Any] = None
        if self.trace_file:
            self.trace.start()
            self.trace.event("trace_start", unit=self.name, serial=self.serial_path,
                             box_addr=self.box_addr, settings=self._trace_settings())
        self._traced_mask: Optional[int] = None
        self._traced_print_state: Optional[str] = None

        # Box state, as last seen
        self.online = False
        self.version_sn: Optional[str] = None
        self.material_mask: Optional[int] = None
        self.connected_mask: Optional[int] = None
        self.buffer_state: Optional[int] = None
        self.box_status: Optional[int] = None
        self._consecutive_failures = 0
        self._last_reconnect_attempt = 0.0
        self._last_heartbeat = 0.0
        self._boot_wait_done = False
        self._operation_depth = 0
        self._quiet_requests = False
        self._poll_timer = None
        self._lane_commands = set()      # lanes whose AFC_CFS_* commands are registered
        # RFID: last tag read per lane, lanes waiting for a read, read in progress
        self.lane_tags: Dict[str, Optional[Dict[str, Any]]] = {}
        self._rfid_queue: Dict[str, float] = {}
        self._rfid_timer = None
        self._rfid_busy = False
        self._rfid_startup_queued = False

    @staticmethod
    def _parse_materials(value: str) -> Dict[str, str]:
        table = {}
        for item in (value or "").split(","):
            item = item.strip()
            if not item:
                continue
            code, sep, material = item.partition("=")
            code = code.strip()
            if not sep or len(code) != 5 or not code.isdigit() or not material.strip():
                raise CONFIG_ERROR(
                    "rfid_materials: expected 'CODE=MATERIAL' with a 5-digit code, got '{}'".format(item))
            table[code] = material.strip()
        return table

    @staticmethod
    def _parse_steps(value: str):
        steps = []
        for item in value.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                dist, speed = item.split(":")
                steps.append((abs(float(dist)), float(speed)))
            except ValueError:
                raise CONFIG_ERROR(
                    "unload_retract_steps: expected 'distance:speed' pairs, got '{}'".format(item))
        return steps

    # -- lifecycle ---------------------------------------------------------

    def handle_connect(self):
        super().handle_connect()     # AFC attaches this unit's lanes here
        self._setup_lanes()
        if not self.cutter_sensor_pin:
            # One toolhead cutter: boxes on the chain share whichever box has the pin
            self.cutter_source = next((u for u in self.bus.units if u.cutter_sensor_pin), None)
        self.logo = '<span class=success--text>CFS Ready</span>\n'
        self.logo_error = '<span class=error--text>CFS Not Ready</span>\n'

    def _cfs_handle_ready(self):
        self._poll_timer = self.reactor.register_timer(self._poll, self.reactor.NOW)

    def _cfs_handle_disconnect(self):
        if self._poll_timer is not None:
            self.reactor.unregister_timer(self._poll_timer)
            self._poll_timer = None
        if self._rfid_timer is not None:
            self.reactor.unregister_timer(self._rfid_timer)
            self._rfid_timer = None
        self.transport.close()
        self.trace.event("trace_stop", reason="klippy disconnect")
        self.trace.stop()

    def _cutter_callback(self, eventtime, state):
        state = bool(state)
        if state and not self.cutter_sensor_state:
            self.cutter_trigger_count += 1
        self.cutter_sensor_state = state
        self.trace.event("cutter_sensor", triggered=state, count=self.cutter_trigger_count)

    def _trace_settings(self) -> Dict[str, Any]:
        keys = ("poll_interval", "request_timeout", "load_attempts", "stage_settle_time",
                "stage_timeout", "checkin_timeout", "resync_timeout",
            "stage5_poll_interval", "stage5_reply_timeout", "stage5_timeout", "stage5_stall_time",
                "stage5_stall_distance", "prime1_length", "prime1_speed", "prime2_length",
                "prime2_speed", "stage7_arg", "clear_mode_after_load",
                "unload_retract_steps", "unload_tip_wiggle", "unload_timeout")
        return {k: getattr(self, k, None) for k in keys}

    def heartbeat(self) -> Optional[P.Frame]:
        """The stock host's periodic CMD_ONLINE_CHECK, which keeps the box's
        connection light happy."""
        reply = self.transport.request(self.box_addr, P.CMD_ONLINE_CHECK, b"",
                                       status=P.REQ_STATUS_ADDRESSING,
                                       timeout=self.request_timeout,
                                       quiet=self._quiet_requests)
        if reply is None:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.offline_after:
                self._set_offline("no reply to heartbeat")
        else:
            self._consecutive_failures = 0
        return reply

    def _is_printing(self) -> bool:
        printer = getattr(self, "printer", None)
        stats = printer.lookup_object("print_stats", None) if printer is not None else None
        if stats is None:
            return False
        try:
            return stats.get_status(self.reactor.monotonic()).get("state") == "printing"
        except Exception:
            return False

    def _trace_print_state(self) -> None:
        """Record print start/stop so trace events can be tied to a job."""
        printer = getattr(self, "printer", None)
        if printer is None or not self.trace.enabled:
            return
        stats = printer.lookup_object("print_stats", None)
        if stats is None:
            return
        try:
            status = stats.get_status(self.reactor.monotonic())
        except Exception:
            return
        state = status.get("state")
        if state != self._traced_print_state:
            self._traced_print_state = state
            self.trace.event("print_state", state=state, filename=status.get("filename"))

    # -- connection --------------------------------------------------------

    def connect(self) -> bool:
        """
        Open the chain's port and make sure this unit's box answers at
        box_addr. A box keeps its address while powered and then ignores
        broadcast discovery, so probe the address first; after a power cycle
        the boxes are unaddressed and get (re)addressed by discovery.
        """
        if not self.transport.open():
            self._set_offline(self.transport.last_error)
            return False

        reply = self._probe()
        if reply is None:
            self.logger.info("CFS {}: no reply at address {}, trying discovery".format(
                self.name, self.box_addr))
            found = self.bus.discover(timeout=max(self.request_timeout, 1.5))
            reply = self._probe() if found else None
            if reply is None:
                self._set_offline("box did not answer discovery" if not found
                                  else "box did not answer after addressing")
                return False
            for uid, addr in found:
                if addr == self.box_addr:
                    self.logger.info("CFS {}: addressed box uid {} at {}".format(
                        self.name, uid.hex(), addr))
        uid = bytes(reply.data[2:14]) if len(reply.data) >= 14 else None
        if uid is not None:
            self.seen_uid = uid
            if self.box_uid and uid != self.box_uid:
                msg = ("box at address {} is uid {}, expected {} (box_uid); power-cycle "
                       "the CFS chain so the boxes are addressed again".format(
                           self.box_addr, uid.hex(), self.box_uid.hex()))
                self.logger.error("CFS {}: {}".format(self.name, msg))
                self._set_offline(msg)
                return False
        return self._set_online()

    def _probe(self) -> Optional[P.Frame]:
        for status in (P.REQ_STATUS_ADDRESSING, P.REQ_STATUS_NORMAL):
            reply = self.transport.request(self.box_addr, P.CMD_ONLINE_CHECK, b"",
                                           status=status, timeout=self.request_timeout)
            if reply is not None:
                return reply
        return None

    def _set_online(self) -> bool:
        if not self.online:
            self.online = True
            self._consecutive_failures = 0
            reply = self.transport.request(self.box_addr, P.GET_VERSION_SN,
                                           timeout=self.request_timeout)
            if reply is not None:
                self.version_sn = P.decode_ascii(reply.data)
            self.trace.event("online", box_addr=self.box_addr, version=self.version_sn)
            self.logger.info("CFS {}: online (addr {}, version {})".format(
                self.name, self.box_addr, self.version_sn or "unknown"))
        return True

    def _set_offline(self, reason: Optional[str]) -> None:
        if self.online:
            self.logger.warning("CFS {}: offline ({})".format(self.name, reason))
            self.trace.event("offline", reason=reason)
        self.online = False

    # -- box requests (used by lanes) --------------------------------------

    def request(self, fn: int, data: bytes = b"", timeout: Optional[float] = None,
                retries: int = 1, motor: bool = False) -> Optional[P.Frame]:
        reply = self.transport.request(self.box_addr, fn, data,
                                       timeout=timeout or self.request_timeout,
                                       retries=retries, motor=motor,
                                       quiet=self._quiet_requests)
        if reply is None:
            # A box turning a slot to find a tag answers nothing for a while;
            # that isn't the box going away
            if not self._rfid_busy:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.offline_after:
                    self._set_offline("no reply to {}".format(P.fn_name(fn)))
        else:
            self._consecutive_failures = 0
            if not self.online:
                self._set_online()
        return reply

    def set_box_mode(self, mode: int, slot_bits: int = 0x00) -> Optional[P.Frame]:
        return self.request(P.SET_BOX_MODE, bytes([slot_bits, mode]))

    def connection_motor(self, action: int) -> Optional[P.Frame]:
        return self.request(P.CTRL_CONNECTION_MOTOR_ACTION, bytes([action]))

    def tighten_up(self, enable: bool) -> Optional[P.Frame]:
        return self.request(P.TIGHTEN_UP_ENABLE, bytes([0x01 if enable else 0x00]))

    def pre_loading(self, action: int, slot_bits: int = P.SLOT_ALL,
                    timeout: Optional[float] = None) -> Optional[P.Frame]:
        motor = action in (P.PRELOAD_RUN, P.PRELOAD_TIGHT)
        return self.request(P.SET_PRE_LOADING, bytes([slot_bits, action]),
                            timeout=timeout or (self.stage_timeout if motor else None),
                            motor=motor)

    def extrude_stage(self, slot_bits: int, stage: int, arg: int = 0x00,
                      timeout: Optional[float] = None) -> Optional[P.Frame]:
        """Motor command: the box answers once the stage's motion finishes.
        Stage 5 is the exception: a status poll answered at once while the
        box feeds, so a lost one costs stage5_reply_timeout, not a resync."""
        if stage == 0x05:
            return self.request(P.EXTRUDE_PROCESS, bytes([slot_bits, stage, arg]),
                                timeout=timeout or self.stage5_reply_timeout, retries=0)
        return self.request(P.EXTRUDE_PROCESS, bytes([slot_bits, stage, arg]),
                            timeout=timeout or self.stage_timeout, retries=0, motor=True)

    def retrude_stage(self, slot_bits: int, stage: int,
                      timeout: Optional[float] = None) -> Optional[P.Frame]:
        """Motor command; slot_bits 0 is the generic check-in used during unload."""
        if timeout is None:
            timeout = self.stage_timeout if slot_bits else self.checkin_timeout
        return self.request(P.RETRUDE_PROCESS, bytes([slot_bits, stage]),
                            timeout=timeout, retries=0, motor=bool(slot_bits))

    def read_material_mask(self) -> Optional[int]:
        reply = self.request(P.GET_FILAMENT_SENSOR_STATE, bytes([P.SENSOR_BANK_MATERIAL]))
        if reply is not None and reply.data:
            self.material_mask = reply.data[0] & P.SLOT_ALL
            return self.material_mask
        return None

    def read_connected_mask(self) -> Optional[int]:
        reply = self.request(P.GET_FILAMENT_SENSOR_STATE, bytes([P.SENSOR_BANK_CONNECTIONS]))
        if reply is not None and reply.data:
            self.connected_mask = reply.data[0] & P.SLOT_ALL
            return self.connected_mask
        return None

    def read_buffer_state(self) -> Optional[int]:
        reply = self.request(P.GET_BUFFER_STATE)
        if reply is not None and reply.data:
            self.buffer_state = reply.data[0]
            return self.buffer_state
        return None

    def read_measuring_wheel(self) -> Optional[float]:
        reply = self.request(P.GET_MEASURING_WHEEL, bytes([0x01]))
        if reply is not None:
            return P.decode_float_be(reply.data)
        return None

    def read_box_status(self) -> Optional[int]:
        reply = self.request(P.GET_BOX_STATE)
        if reply is not None:
            self.box_status = reply.status
            return reply.status
        return None

    def clear_latched_error(self) -> None:
        """A finished extrude can leave GET_BOX_STATE latched on EXTRUDE_ERR8
        even though it worked; a fresh SET_BOX_MODE(IDLE) clears it."""
        self.set_box_mode(P.MODE_IDLE)

    class _Operation:
        def __init__(self, unit):
            self.unit = unit

        def __enter__(self):
            # A tag read turns a slot and can keep the box (and the shared
            # line) busy for a minute; don't start a load/unload in the middle
            self.unit.wait_for_tag_reads()
            self.unit._operation_depth += 1
            return self.unit

        def __exit__(self, *exc):
            self.unit._operation_depth -= 1
            return False

    def wait_for_tag_reads(self) -> None:
        """Block (cooperatively) while a tag read runs on any box of this chain."""
        units = getattr(getattr(self, "bus", None), "units", None) or [self]
        if not any(getattr(u, "_rfid_busy", False) for u in units):
            return
        limit = (self.rfid_busy_timeout + self.resync_timeout
                 + self.rfid_read_attempts * (2.0 + self.rfid_retry_interval) + 10.0)
        start = self.reactor.monotonic()
        self.logger.info("CFS {}: waiting for an RFID tag read to finish".format(self.name))
        self.trace.event("wait_for_tag_read")
        while any(getattr(u, "_rfid_busy", False) for u in units):
            if self.reactor.monotonic() - start > limit:
                self.logger.warning("CFS {}: tag read still running after {:.0f}s; "
                                    "continuing".format(self.name, limit))
                break
            self.reactor.pause(self.reactor.monotonic() + 0.2)
        self.trace.event("tag_read_done", waited_s=round(self.reactor.monotonic() - start, 2))

    def operation(self):
        """Context manager that pauses background polling during a box sequence."""
        return self._Operation(self)

    # -- polling -----------------------------------------------------------

    def _poll(self, eventtime: float) -> float:
        if self.bus.operation_active or self.transport.busy:
            return eventtime + self.poll_interval

        if not self.online:
            if eventtime - self._last_reconnect_attempt >= self.reconnect_interval:
                self._last_reconnect_attempt = eventtime
                try:
                    self.connect()
                except Exception:
                    self.logger.debug("CFS {}: reconnect failed\n{}".format(
                        self.name, traceback.format_exc()))
            if not self.online:
                return self.reactor.monotonic() + self.poll_interval

        self._quiet_requests = not self.trace_polls
        try:
            interval = (self.heartbeat_interval_printing if self._is_printing()
                        else self.heartbeat_interval)
            if eventtime - self._last_heartbeat >= interval:
                self._last_heartbeat = eventtime
                self.heartbeat()
            mask = self.read_material_mask()
            if mask is not None:
                self._mirror_presence(mask)
            connected = self.read_connected_mask()
            if connected is not None:
                self._mirror_connections(connected)
            self.read_buffer_state()
            self._queue_startup_tag_reads()
        except Exception:
            self.logger.debug("CFS {}: poll failed\n{}".format(self.name, traceback.format_exc()))
        finally:
            self._quiet_requests = False
        self._trace_print_state()
        return self.reactor.monotonic() + self.poll_interval

    def _mirror_presence(self, mask: int) -> None:
        if mask != self._traced_mask:
            self.trace.event("presence", mask=mask,
                             slots="".join(P.slot_letter(i) for i in P.bits_to_slots(mask)),
                             previous=self._traced_mask)
            self._traced_mask = mask
        for lane in self.lanes.values():
            try:
                self.update_lane_presence(lane, bool(mask & P.slot_bit(lane.index)))
            except ValueError:
                pass

    def _mirror_connections(self, mask: int) -> None:
        for lane in self.lanes.values():
            lane._load_state = bool(mask & P.slot_bit(lane.index))

    # -- AFC unit hooks ----------------------------------------------------

    def system_Test(self, cur_lane: AFCLane, delay: float, assignTcmd: bool,
                    enable_movement: bool) -> bool:
        """Restore lane state during PREP from the box's own sensors."""
        if not self.online:
            self.connect()
        if not self.online and not self._boot_wait_done:
            self._wait_for_box()
        self._boot_wait_done = True
        if self.online and self.material_mask is None:
            self.read_material_mask()
        if self.online and self.connected_mask is None:
            self.read_connected_mask()

        if not self.online:
            # Unknown, not empty: the first poll after the box answers syncs
            # this lane quietly instead of treating it as a new spool.
            cur_lane._cfs_present_last = None
            msg = "<span class=error--text>CFS NOT RESPONDING</span>"
            self.lane_not_ready(cur_lane)
        else:
            present = bool(self.material_mask is not None
                           and self.material_mask & P.slot_bit(cur_lane.index))
            cur_lane._cfs_present_last = present
            msg = self.apply_lane_state(cur_lane, present)

        if assignTcmd:
            self.afc.function.TcmdAssign(cur_lane)
        cur_lane.send_lane_data()
        cur_lane.do_enable(False)
        # AFCProject main keeps lane.map as a string; newer AFC makes it a list
        # with map_to_string(). Support both.
        to_string = getattr(cur_lane, "map_to_string", None)
        tcmd = to_string() if callable(to_string) else str(cur_lane.map)
        self.logger.info("{lane} tool cmd: {tcmd:3} {msg}".format(
            lane=cur_lane.name, tcmd=tcmd, msg=msg))
        cur_lane.set_afc_prep_done()
        return self.online

    def ensure_print_mode(self, lane: AFCLane) -> None:
        """
        A lane AFC has loaded in the toolhead needs its slot in print mode, or
        the box won't feed during the print. A box that restarted (or was
        left in change mode) has lost that, so restore it at PREP.
        """
        if not self.online:
            return
        reply = self.request(P.GET_BOX_STATE)
        if reply is None or len(reply.data) < 4:
            return
        if reply.data[3] == P.BOX_STATE_PRINT_LOCKED:
            return
        bits = P.slot_bit(lane.index)
        self.set_box_mode(P.MODE_PRINT, bits)
        self.trace.event("print_mode_restored", lane=lane.name,
                         slot=P.slot_letter(lane.index), box_state=reply.data.hex())
        self.logger.info("CFS {}: slot {} is loaded in the toolhead; set print mode "
                         "again (box state was {})".format(
                             self.name, P.slot_letter(lane.index), reply.data.hex()))

    def _wait_for_box(self) -> None:
        """Keep trying to reach a box that is still booting, up to boot_wait."""
        if self.boot_wait <= 0:
            return
        self.logger.info("CFS {}: waiting up to {:.0f}s for the box to start".format(
            self.name, self.boot_wait))
        deadline = self.reactor.monotonic() + self.boot_wait
        while not self.online and self.reactor.monotonic() < deadline:
            self.reactor.pause(self.reactor.monotonic() + 1.0)
            self.connect()

    # =====================================================================
    # Lanes
    #
    # Lanes are plain [AFC_lane laneN] sections with unit: <cfs>:<1-4>. The box
    # owns the slot motor and sensors, so a lane has no prep/load pins and no
    # stepper: presence comes from the poll, and tool load/unload are this
    # unit's AFC_CFS_TOOL_LOAD / AFC_CFS_TOOL_UNLOAD, set as each lane's
    # custom_load_cmd / custom_unload_cmd at connect.
    #
    # Load follows the stock sequence tomaspakosta/k1c-cfs-klipper validated on
    # hardware: connection motor -> tighten -> EXTRUDE_PROCESS stages 0, 4, 5
    # (until the toolhead sensor trips) -> prime -> stage 6 -> prime -> stage
    # 7 -> mark slot PRINT. Unload cuts, runs RETRUDE start, retracts the
    # extruder, then RETRUDE finish reels the slot back.
    # =====================================================================

    cmd_AFC_CFS_TOOL_LOAD_help = "Load a CFS lane into the toolhead"
    cmd_AFC_CFS_TOOL_UNLOAD_help = "Unload a CFS lane from the toolhead back to the box"
    cmd_AFC_CFS_EJECT_help = "Reel a CFS lane's filament back into the box (box motors only)"
    cmd_AFC_CFS_READ_TAG_help = "Read a CFS lane's RFID tag and set its color/material"

    def _setup_lanes(self) -> None:
        """At connect, once AFC has attached this unit's lanes."""
        for lane in list(self.lanes.values()):
            self._setup_lane(lane)

    def _setup_lane(self, lane: AFCLane) -> None:
        where = "[AFC_lane {}]".format(lane.name)
        if getattr(lane, "prep", None) is not None or getattr(lane, "load", None) is not None:
            raise CONFIG_ERROR("{}: CFS lanes take filament presence from the box; "
                               "remove prep/load pins".format(where))
        if lane.index < 1 or lane.index > 4:
            raise CONFIG_ERROR("{}: unit must be '{}:<1-4>' (slot A-D), got index {}".format(
                where, self.name, lane.index))
        # No load switch: AFC defaults _load_state to True; the box's
        # connection sensor drives it instead (see set_lane_sensors)
        lane._load_state = False
        lane._cfs_present_last = None
        lane.last_load_report = {}
        if getattr(lane, "custom_load_cmd", None) is None:
            lane.custom_load_cmd = "AFC_CFS_TOOL_LOAD LANE={}".format(lane.name)
        if getattr(lane, "custom_unload_cmd", None) is None:
            lane.custom_unload_cmd = "AFC_CFS_TOOL_UNLOAD LANE={}".format(lane.name)
        if lane.name in self._lane_commands:
            return
        self._lane_commands.add(lane.name)
        for cmd, handler in (("AFC_CFS_TOOL_LOAD", self.cmd_AFC_CFS_TOOL_LOAD),
                             ("AFC_CFS_TOOL_UNLOAD", self.cmd_AFC_CFS_TOOL_UNLOAD),
                             ("AFC_CFS_EJECT", self.cmd_AFC_CFS_EJECT),
                             ("AFC_CFS_READ_TAG", self.cmd_AFC_CFS_READ_TAG)):
            self.gcode.register_mux_command(
                cmd, "LANE", lane.name,
                lambda gcmd, lane=lane, handler=handler: handler(lane, gcmd),
                desc=getattr(self, "cmd_{}_help".format(cmd)))

    # -- lane plumbing -------------------------------------------------------

    def _pause(self, seconds: float) -> None:
        if seconds > 0:
            self.reactor.pause(self.reactor.monotonic() + seconds)

    def _e_move(self, distance: float, speed_mm_min: float, label: str) -> None:
        """Extruder-only move that leaves the user's M82/M83 mode alone."""
        self.afc.move_e_pos(distance, speed_mm_min / 60.0, "CFS {}".format(label), wait_tool=True)

    @staticmethod
    def _toolhead_sensor(lane: AFCLane) -> bool:
        return bool(lane.get_toolhead_pre_sensor_state())

    def _cutter(self) -> Any:
        """The unit whose cutter switch to watch: the cutter is on the
        toolhead, so a box without cutter_sensor_pin uses the chain's."""
        return getattr(self, "cutter_source", None) or self

    def _cutter_engaged(self) -> bool:
        cutter = self._cutter()
        return bool(cutter.cutter_sensor_pin and cutter.cutter_sensor_state)

    def _lane_trace(self, lane: AFCLane, ev: str, **fields: Any) -> None:
        self.trace.event(ev, lane=lane.name, slot=P.slot_letter(lane.index), **fields)

    def _lane_fail(self, lane: AFCLane, message: str) -> None:
        self._lane_trace(lane, "fail", message=message, toolhead_sensor=self._toolhead_sensor(lane))
        self.logger.error("CFS {} (slot {}): {}".format(lane.name, P.slot_letter(lane.index), message))
        self.afc.error.handle_lane_failure(lane, "CFS {}: {}".format(lane.name, message))

    # -- presence from the box poll -------------------------------------------

    def update_lane_presence(self, lane: AFCLane, present: bool) -> None:
        """Called by the poll with this slot's material sensor state."""
        if present == getattr(lane, "_cfs_present_last", None):
            return
        previous = getattr(lane, "_cfs_present_last", None)
        lane._cfs_present_last = present
        lane.prep_state = present
        lane.loaded_to_hub = present
        slot = P.slot_letter(lane.index)

        if not getattr(lane, "_afc_prep_done", False):
            self.afc.save_vars()
            return
        if previous is None:
            # PREP ran while the box was offline: this is the first real
            # reading, not an insert or a runout.
            msg = self.apply_lane_state(lane, present)
            if not present and not lane.tool_loaded:
                lane.set_unloaded()
            self.logger.info("CFS {}: box online, slot {}: {}".format(lane.name, slot, msg))
            self.afc.save_vars()
            return

        if present:
            self.logger.info("CFS {}: spool detected in slot {}".format(lane.name, slot))
            if lane.status not in (AFCLaneState.LOADED, AFCLaneState.TOOLED,
                                   AFCLaneState.TOOL_LOADED):
                lane.set_loaded()
            self.on_filament_insert(lane)
        else:
            self.logger.info("CFS {}: slot {} is empty".format(lane.name, slot))
            self.on_filament_remove(lane)
            if lane.tool_loaded and self.afc.function.is_printing():
                self._lane_trace(lane, "runout", runout_lane=lane.runout_lane)
                fila = getattr(lane, "fila_load", None)
                if fila is not None and not fila.runout_helper.sensor_enabled:
                    self.logger.warning("CFS runout on {} ignored, sensor disabled".format(lane.name))
                elif lane.runout_lane is not None:
                    # The tail is still in the path; let AFC's infinite spool decide.
                    lane._perform_infinite_runout()
                else:
                    lane._perform_pause_runout()
            elif not lane.tool_loaded:
                lane.set_unloaded()
        self.afc.save_vars()

    # -- tool load -------------------------------------------------------------

    def _stage5_feed(self, lane: AFCLane) -> tuple:
        """
        Poll EXTRUDE_PROCESS stage 5 (the box motor runs while we poll) until
        the toolhead sensor trips. Stops early on timeout or when the measuring
        wheel stops moving.
        """
        bits = P.slot_bit(lane.index)
        start = self.reactor.monotonic()
        last_progress_time = start
        last_wheel: Optional[float] = None
        polls = 0
        while True:
            if self._toolhead_sensor(lane):
                return True, "toolhead sensor triggered after {} polls".format(polls)
            now = self.reactor.monotonic()
            if now - start > self.stage5_timeout:
                return False, "timed out after {:.0f}s without reaching the toolhead sensor".format(
                    now - start)
            reply = self.extrude_stage(bits, 0x05)
            polls += 1
            if reply is not None and not reply.ok and reply.status != P.ST_UPDATE_STATE:
                self.logger.debug("CFS {}: stage 5 status {}".format(lane.name, reply.status_name()))
            wheel = P.decode_float_be(reply.data) if reply is not None else None
            if wheel is not None:
                if last_wheel is None or abs(wheel - last_wheel) >= self.stage5_stall_distance:
                    last_wheel = wheel
                    last_progress_time = now
                elif now - last_progress_time > self.stage5_stall_time:
                    if self._toolhead_sensor(lane):
                        continue
                    return False, "measuring wheel stalled at {:.1f}mm".format(wheel)
            lane.last_load_report["stage5_polls"] = polls
            lane.last_load_report["wheel"] = wheel
            self._pause(self.stage5_poll_interval)

    def _box_cleanup(self) -> None:
        self.tighten_up(False)
        self.connection_motor(P.CONN_STOP)
        self.clear_latched_error()

    def _load_attempt(self, lane: AFCLane, attempt: int) -> tuple:
        bits = P.slot_bit(lane.index)
        settle = self.stage_settle_time

        self.pre_loading(P.PRELOAD_CLOSE)
        status = self.read_box_status()
        if status not in (None, P.ST_OK, P.ST_UPDATE_STATE):
            self.logger.info("CFS {}: box reported {} before load, clearing".format(
                lane.name, P.status_name(status)))
        self.set_box_mode(P.MODE_IDLE)
        self._pause(settle)

        reply = self.connection_motor(P.CONN_EXTRUDE)
        if reply is None:
            return False, "box did not answer CTRL_CONNECTION_MOTOR_ACTION"
        self._pause(0.5)
        self.tighten_up(True)
        self._pause(settle)

        for stage in (0x00, 0x04):
            reply = self.extrude_stage(bits, stage)
            if reply is None:
                self._box_cleanup()
                return False, "no reply to EXTRUDE_PROCESS stage {}".format(stage)
            if not reply.ok:
                self._box_cleanup()
                return False, "EXTRUDE_PROCESS stage {} returned {}".format(stage, reply.status_name())
            self._pause(settle)

        stage5_start = self.reactor.monotonic()
        reached, detail = self._stage5_feed(lane)
        self._lane_trace(lane, "stage5_result", reached=reached, detail=detail, attempt=attempt + 1,
                         polls=lane.last_load_report.get("stage5_polls"),
                         wheel=lane.last_load_report.get("wheel"),
                         seconds=round(self.reactor.monotonic() - stage5_start, 2))
        self.logger.info("CFS {}: stage 5 {}".format(lane.name, detail))
        if not reached:
            self._box_cleanup()
            # Pull the partial feed back so the next attempt starts clean
            self.retrude_stage(bits, 0x00)
            self._pause(0.5)
            self.retrude_stage(bits, 0x01)
            self.clear_latched_error()
            return False, detail

        # Handoff: the toolhead extruder grabs the filament while the box
        # finishes its stages.
        if self.prime1_length > 0:
            self._e_move(self.prime1_length, self.prime1_speed, "prime 1")
        reply = self.extrude_stage(bits, 0x06)
        lane.last_load_report["stage6"] = P.status_name(reply.status if reply else None)
        if self.prime2_length > 0:
            self._e_move(self.prime2_length, self.prime2_speed, "prime 2")
        reply = self.extrude_stage(bits, 0x07, self.stage7_arg)
        lane.last_load_report["stage7"] = P.status_name(reply.status if reply else None)
        if reply is None or not reply.ok:
            self.logger.warning("CFS {}: stage 7 returned {}".format(
                lane.name, lane.last_load_report["stage7"]))
        self._lane_trace(lane, "handoff", attempt=attempt + 1,
                         stage6=lane.last_load_report["stage6"],
                         stage7=lane.last_load_report["stage7"], stage7_arg=self.stage7_arg,
                         toolhead_sensor=self._toolhead_sensor(lane))

        # Mark this slot as the active PRINT slot: the box then runs its own
        # buffer feed loop for the print. Nothing may send SET_BOX_MODE
        # [00][01] (feed/change mode) after this until the next unload.
        self.set_box_mode(P.MODE_PRINT, bits)
        self.tighten_up(False)
        self.connection_motor(P.CONN_STOP)
        if self.clear_mode_after_load:
            self.clear_latched_error()

        if not self._toolhead_sensor(lane):
            return False, "toolhead sensor cleared after the handoff"
        return True, "loaded"

    def cmd_AFC_CFS_TOOL_LOAD(self, lane: AFCLane, gcmd: Any) -> None:
        """
        Load this CFS lane to the nozzle. AFC calls this through custom_load_cmd
        during a tool change; it can also be run by hand during bring-up.

        Usage
        -----
        `AFC_CFS_TOOL_LOAD LANE=<lane>`
        """
        self.wait_for_tag_reads()   # a tag read keeps the box busy; finish it first
        started = self.reactor.monotonic()
        slot = P.slot_letter(lane.index)
        self._lane_trace(lane, "load_start", toolhead_sensor=self._toolhead_sensor(lane))
        lane.select_lane()
        self.afc._check_extruder_temp(lane)
        lane.last_load_report = {"slot": slot}

        if not self.online and not self.connect():
            self._lane_fail(lane, "box is offline ({})".format(self.transport.last_error or "no reply"))
            return
        if self._cutter_engaged():
            self._lane_fail(lane, "cutter sensor is engaged; reset the cutter before loading")
            return
        if self._toolhead_sensor(lane):
            self._lane_fail(lane, "toolhead sensor already sees filament; unload the other lane first")
            return
        mask = self.read_material_mask()
        if mask is not None and not mask & P.slot_bit(lane.index):
            self._lane_fail(lane, "no filament in slot {}".format(slot))
            return

        if self.afc.park and self.afc.park_cmd:
            self.afc.gcode.run_script_from_command(
                "{} EXTRUDER={}".format(self.afc.park_cmd, lane.extruder_obj.name))

        attempts_used = 0
        detail = ""
        with self.operation():
            for attempt in range(self.load_attempts):
                attempts_used = attempt + 1
                self.logger.info("CFS {}: loading slot {}, try {}/{}".format(
                    lane.name, slot, attempt + 1, self.load_attempts))
                try:
                    ok, detail = self._load_attempt(lane, attempt)
                except Exception as e:
                    self._box_cleanup()
                    ok, detail = False, "error: {}".format(e)
                    self.logger.debug(traceback.format_exc())
                if ok:
                    break
                self._lane_trace(lane, "load_attempt_failed", attempt=attempt + 1, detail=detail,
                                 toolhead_sensor=self._toolhead_sensor(lane))
                self.logger.warning("CFS {}: load attempt {} failed: {}".format(
                    lane.name, attempt + 1, detail))
                if self._toolhead_sensor(lane):
                    # Filament is stuck in the toolhead; don't keep pushing.
                    break
            else:
                ok = False

        lane.last_load_report["result"] = detail
        if not ok:
            self._lane_trace(lane, "load_end", ok=False, detail=detail, attempts=attempts_used,
                             seconds=round(self.reactor.monotonic() - started, 2))
            self._lane_fail(lane, "load failed after {} attempt(s): {}".format(attempts_used, detail))
            return

        # The handoff already pushed prime1 + prime2 past the sensor.
        remaining = lane.extruder_obj.tool_stn - (self.prime1_length + self.prime2_length)
        if remaining > 0:
            self.afc.move_e_pos(remaining, lane.extruder_obj.tool_load_speed,
                                "CFS tool_stn", wait_tool=True)
        lane._load_state = True          # our filament now occupies the shared path
        self._mark_tool_state(lane, loaded=True)
        self._lane_trace(lane, "load_end", ok=True, detail=detail, attempts=attempts_used,
                         tool_stn_push=round(max(remaining, 0), 2),
                         seconds=round(self.reactor.monotonic() - started, 2))

    # -- tool unload -----------------------------------------------------------

    def _run_cutter_sequence(self, lane: AFCLane) -> Optional[str]:
        """
        Run AFC's cut macro. With a cutter switch configured, verify it: the
        switch must have fired during the cut (blade reached the end of its
        travel, if cutter_require_trigger) and be released afterwards. Retries
        the cut; returns None on success or the reason it failed.
        """
        if not self.afc.tool_cut:
            return None
        cutter = self._cutter()
        reason = None
        for attempt in range(max(self.load_attempts, 1) + 1):
            triggers_before = getattr(cutter, "cutter_trigger_count", 0)
            lane.extruder_obj.estats.increase_cut_total()
            self.afc.gcode.run_script_from_command(
                "{} EXTRUDER={}".format(self.afc.tool_cut_cmd, lane.extruder_obj.name))
            if not cutter.cutter_sensor_pin:
                self._lane_trace(lane, "cut", attempt=attempt + 1, verified=False)
                return None
            # Let the cut moves finish and the switch report arrive
            toolhead = getattr(self.afc, "toolhead", None)
            if toolhead is not None and hasattr(toolhead, "wait_moves"):
                toolhead.wait_moves()
            start = self.reactor.monotonic()
            self._pause(0.1)
            while self._cutter_engaged():
                if self.reactor.monotonic() - start > cutter.cutter_wait_time:
                    break
                self._pause(0.01)
            released = not self._cutter_engaged()
            fired = getattr(cutter, "cutter_trigger_count", 0) > triggers_before
            self._lane_trace(lane, "cut", attempt=attempt + 1, verified=True, fired=fired,
                             released=released, seconds=round(self.reactor.monotonic() - start, 2))
            if not released:
                reason = "cutter sensor did not release after cutting"
            elif not fired and cutter.cutter_require_trigger:
                reason = ("cutter switch never fired during the cut (blade did not reach "
                          "the end of its travel; check pin_loc_xy / cut_move_dist)")
            else:
                return None
            self.logger.warning("CFS {}: cut {} not confirmed: {}".format(
                lane.name, attempt + 1, reason))
        return reason

    def _run_post_cut_macros(self, lane: AFCLane) -> None:
        if self.afc.park and self.afc.park_cmd:
            self.afc.gcode.run_script_from_command(
                "{} EXTRUDER={}".format(self.afc.park_cmd, lane.extruder_obj.name))
        if self.afc.form_tip:
            if self.afc.form_tip_cmd == "AFC":
                self.printer.lookup_object("AFC_form_tip").tip_form()
            else:
                self.afc.gcode.run_script_from_command(self.afc.form_tip_cmd)

    def _retract_to_box(self, lane: AFCLane) -> tuple:
        """
        After the cut, stock order (Hi captures, confirmed on a K1):
          1. RETRUDE stage 0 (START) while the extruder gears still hold the
             filament: the box pulls the buffer to its empty limit. With the
             end already free it reels slack and fails RETRUDE_ERR2.
          2. Extruder retract out of the gears (unload_retract_steps); the
             now-empty buffer takes up the slack, so the gears don't click.
          3. RETRUDE stage 1 (FINISH): the box reels the slot back and
             answers when it has finished.
        """
        bits = P.slot_bit(lane.index)
        self.pre_loading(P.PRELOAD_CLOSE)
        self.set_box_mode(P.MODE_IDLE)
        self._pause(self.stage_settle_time)

        if self.unload_tip_wiggle:
            for dist, speed in TIP_WIGGLE_STEPS:
                self._e_move(dist, speed, "tip wiggle")

        started = self.reactor.monotonic()
        reply = self.retrude_stage(bits, 0x00)
        start_status = P.status_name(reply.status if reply else None)
        self._lane_trace(lane, "retrude_start", status=start_status,
                         seconds=round(self.reactor.monotonic() - started, 2),
                         toolhead_sensor=self._toolhead_sensor(lane))
        if reply is None:
            return False, "box did not answer RETRUDE start"
        if not reply.ok:
            # FINISH still works on its own (it recovered a failed unload on
            # the K1), so carry on rather than leave filament in the path.
            self.logger.warning("CFS {}: RETRUDE start returned {}".format(
                lane.name, start_status))

        for i, (dist, speed) in enumerate(self.unload_retract_steps):
            self._e_move(-dist, speed, "unload retract {}".format(i + 1))
            self._lane_trace(lane, "retract_step", step=i + 1, mm=dist, speed=speed,
                             toolhead_sensor=self._toolhead_sensor(lane))

        started = self.reactor.monotonic()
        reply = self.retrude_stage(bits, 0x01)
        finish_status = P.status_name(reply.status if reply else None)
        reel_seconds = round(self.reactor.monotonic() - started, 2)
        done = reply is not None and reply.ok
        self.clear_latched_error()
        self._lane_trace(lane, "reel", done=done, replies=[start_status, finish_status],
                         seconds=reel_seconds, toolhead_sensor=self._toolhead_sensor(lane))

        deadline = self.reactor.monotonic() + self.unload_timeout
        while self._toolhead_sensor(lane):
            if self.reactor.monotonic() > deadline:
                return False, ("toolhead sensor still triggered {:.0f}s after reel-back "
                               "(box start {}, finish {})".format(
                                   self.unload_timeout, start_status, finish_status))
            self._pause(0.5)
        if not done:
            return False, "box did not confirm reel-back (start {}, finish {})".format(
                start_status, finish_status)
        return True, "reeled back in {:.1f}s".format(reel_seconds)

    def cmd_AFC_CFS_TOOL_UNLOAD(self, lane: AFCLane, gcmd: Any) -> None:
        """
        Cut and unload this CFS lane back to the box. AFC calls this through
        custom_unload_cmd during a tool change.

        Usage
        -----
        `AFC_CFS_TOOL_UNLOAD LANE=<lane>`
        """
        self.wait_for_tag_reads()   # a tag read keeps the box busy; finish it first
        started = self.reactor.monotonic()
        self._lane_trace(lane, "unload_start", toolhead_sensor=self._toolhead_sensor(lane))
        lane.select_lane()
        self.afc._check_extruder_temp(lane)
        lane.disable_buffer()

        if not self.online and not self.connect():
            self._lane_fail(lane, "box is offline ({})".format(self.transport.last_error or "no reply"))
            return
        if self._cutter_engaged():
            self._lane_fail(lane, "cutter sensor is engaged before cutting; check the cutter lever")
            return
        cut_problem = self._run_cutter_sequence(lane)
        if cut_problem:
            self._lane_fail(lane, cut_problem)
            return
        self._run_post_cut_macros(lane)

        with self.operation():
            try:
                ok, detail = self._retract_to_box(lane)
            except Exception as e:
                self.clear_latched_error()
                ok, detail = False, "error: {}".format(e)
                self.logger.debug(traceback.format_exc())

        self.logger.info("CFS {}: unload {}".format(lane.name, detail))
        self._lane_trace(lane, "unload_end", ok=ok, detail=detail,
                         seconds=round(self.reactor.monotonic() - started, 2))
        if not ok:
            self._lane_fail(lane, "unload failed: {}".format(detail))
            return
        lane._load_state = False         # reeled back past the merge
        self._mark_tool_state(lane, loaded=False)

    def _mark_tool_state(self, lane: AFCLane, loaded: bool) -> None:
        """
        Record the tool state in AFC. AFC's own TOOL_LOAD/TOOL_UNLOAD flow
        does this after our custom command returns, but running
        AFC_CFS_TOOL_LOAD/UNLOAD directly would otherwise leave AFC thinking
        nothing (or the old lane) is in the toolhead. Setting it twice is
        harmless.
        """
        try:
            if loaded:
                lane.set_tool_loaded(normal_toolchange=True)
            else:
                lane.set_tool_unloaded(normal_toolchange=True)
            self.afc.save_vars()
        except Exception as e:
            self.logger.warning("CFS {}: could not record tool state: {}".format(lane.name, e))

    def cmd_AFC_CFS_EJECT(self, lane: AFCLane, gcmd: Any) -> None:
        """
        Reel this lane's filament back into the box using only the box
        motors. For filament that stopped short of the extruder gears (an
        interrupted load); filament in the toolhead needs AFC_CFS_TOOL_UNLOAD.

        Usage
        -----
        `AFC_CFS_EJECT LANE=<lane>`
        """
        self.wait_for_tag_reads()
        if not self.online and not self.connect():
            raise gcmd.error("CFS {}: box is offline".format(lane.name))
        ok, detail = self.reel_back(lane)
        gcmd.respond_info("CFS {} (slot {}) eject: {}".format(
            lane.name, P.slot_letter(lane.index), detail))
        if not ok:
            raise gcmd.error("CFS {}: eject did not finish cleanly: {}".format(lane.name, detail))

    def cmd_AFC_CFS_READ_TAG(self, lane: AFCLane, gcmd: Any) -> None:
        """
        Read this lane's RFID tag (Creality format) and set the lane's color,
        material and weight from it, or its Spoolman spool if the tag carries
        an ID. Not during a print or with the lane loaded. Without a tag the
        box can be busy for about a minute.

        Usage
        -----
        `AFC_CFS_READ_TAG LANE=<lane> [SPIN=<0|1>] [APPLY=<0|1>]`

        SPIN=0 skips turning the slot first; APPLY=0 only reports the tag.
        """
        slot = P.slot_letter(lane.index)
        if not self.online and not self.connect():
            raise gcmd.error("CFS {}: box is offline".format(lane.name))
        blocker = self.tag_read_blocker(lane)
        if blocker is not None:
            raise gcmd.error("CFS {}: can't read the tag now ({})".format(lane.name, blocker))
        spin = gcmd.get_int("SPIN", 1 if self.rfid_spin else 0, minval=0, maxval=1)
        tag = self.read_tag(lane, spin=bool(spin))
        if tag is None:
            gcmd.respond_info("CFS {} (slot {}): no tag found".format(lane.name, slot))
            return
        gcmd.respond_info("CFS {} (slot {}) tag: {}\n  record {}".format(
            lane.name, slot, self._tag_summary(tag), tag["record"]))
        if gcmd.get_int("APPLY", 1, minval=0, maxval=1):
            for cmd in self.apply_tag(lane, tag, self.afc.gcode.run_script_from_command):
                gcmd.respond_info("  " + cmd)

    # -- RFID tags ---------------------------------------------------------

    def on_filament_insert(self, lane: AFCLane):
        super().on_filament_insert(lane)
        if self.rfid_read:
            self.queue_tag_read(lane, self.rfid_insert_delay)

    def on_filament_remove(self, lane: AFCLane):
        super().on_filament_remove(lane)
        self._rfid_queue.pop(lane.name, None)
        self.lane_tags.pop(lane.name, None)

    def _queue_startup_tag_reads(self) -> None:
        """Once per Klipper start, after PREP: queue a tag read for every slot
        with a spool (they run one at a time, never during a print)."""
        if not self.rfid_read_at_startup or self._rfid_startup_queued:
            return
        if self.material_mask is None or not self.lanes or not all(
                getattr(lane, "_afc_prep_done", False) for lane in self.lanes.values()):
            return
        self._rfid_startup_queued = True
        for lane in self.lanes.values():
            if self.material_mask & P.slot_bit(lane.index):
                self.queue_tag_read(lane, self.rfid_startup_delay)

    def queue_tag_read(self, lane: AFCLane, delay: float = 0.) -> None:
        """Read this lane's tag once the box is free (never during a print)."""
        due = self.reactor.monotonic() + delay
        self._rfid_queue[lane.name] = due
        if self._rfid_timer is None:
            self._rfid_timer = self.reactor.register_timer(self._rfid_tick, due)
        else:
            self.reactor.update_timer(self._rfid_timer, min(self._rfid_queue.values()))

    def _job_active(self) -> bool:
        printer = getattr(self, "printer", None)
        stats = printer.lookup_object("print_stats", None) if printer is not None else None
        if stats is None:
            return False
        try:
            return stats.get_status(self.reactor.monotonic()).get("state") in ("printing", "paused")
        except Exception:
            return False

    def tag_read_blocker(self, lane: AFCLane) -> Optional[str]:
        """Why this lane's tag can't be read right now, or None."""
        if self._job_active():
            return "a print is running"
        if not self.online:
            return "box offline"
        if self.bus.operation_active or self.transport.busy:
            return "box busy"
        if getattr(lane, "tool_loaded", False) or (
                self.connected_mask is not None and self.connected_mask & P.slot_bit(lane.index)):
            return "lane is loaded"
        # Untested: turning one slot while another of this box is loaded
        # (that slot is in print mode). Wait until the box is unloaded.
        if self.connected_mask or any(getattr(l, "tool_loaded", False)
                                      for l in self.lanes.values()):
            return "another slot of this box is loaded"
        if self.material_mask is not None and not self.material_mask & P.slot_bit(lane.index):
            return "slot is empty"
        return None

    def _rfid_tick(self, eventtime: float) -> float:
        if not self._rfid_queue:
            self._rfid_timer = None
            return self.reactor.NEVER
        name, due = min(self._rfid_queue.items(), key=lambda kv: kv[1])
        if due > eventtime:
            return due
        lane = self.lanes.get(name)
        if lane is None:
            self._rfid_queue.pop(name, None)
            return eventtime
        blocker = self.tag_read_blocker(lane)
        if blocker in ("slot is empty", "lane is loaded"):
            self._rfid_queue.pop(name, None)
        elif blocker is not None:
            # Try again later (after the print, once the box is free)
            self._rfid_queue[name] = eventtime + 10.0
        else:
            self._rfid_queue.pop(name, None)
            try:
                tag = self.read_tag(lane)
                if tag is not None:
                    self.apply_tag(lane, tag, self.afc.gcode.run_script)
            except Exception:
                self.logger.warning("CFS {}: tag read failed\n{}".format(
                    lane.name, traceback.format_exc()))
        if not self._rfid_queue:
            self._rfid_timer = None
            return self.reactor.NEVER
        return min(self._rfid_queue.values())

    def read_tag(self, lane: AFCLane, spin: Optional[bool] = None) -> Optional[Dict[str, Any]]:
        """
        Ask the box for a slot's tag. Returns the decoded tag, or None if the
        box has none for it. Like stock BOX_INFO_REFRESH, the slot is turned
        first (SET_PRE_LOADING RUN) so the reader can see the tag.
        """
        spin = self.rfid_spin if spin is None else spin
        bits = P.slot_bit(lane.index)
        record = None
        replies = []
        try:
            with self.operation():
                self._rfid_busy = True
                if spin:
                    # Answered once the box is done; with no tag that can be
                    # about a minute
                    self.pre_loading(P.PRELOAD_RUN, bits, timeout=self.rfid_busy_timeout)
                for attempt in range(self.rfid_read_attempts):
                    reply = self.request(P.GET_RFID, bytes([bits]), timeout=2.0)
                    if reply is not None:
                        replies.append(P.decode_ascii(reply.data))
                        record = P.parse_rfid_reply(reply.data).get(lane.index)
                        if record:
                            break
                    if attempt + 1 < self.rfid_read_attempts:
                        self.reactor.pause(self.reactor.monotonic() + self.rfid_retry_interval)
                if spin:
                    self.pre_loading(P.PRELOAD_CLOSE)
        finally:
            self._rfid_busy = False
        tag = P.decode_rfid_record(record, self.rfid_materials) if record else None
        self.lane_tags[lane.name] = tag
        self.trace.event("rfid", lane=lane.name, slot=P.slot_letter(lane.index), spin=spin,
                         replies=replies, tag=tag)
        self.logger.info("CFS {}: slot {} tag: {}".format(
            lane.name, P.slot_letter(lane.index), self._tag_summary(tag)))
        return tag

    @staticmethod
    def _tag_summary(tag: Optional[Dict[str, Any]]) -> str:
        if tag is None:
            return "none"
        parts = [tag["material"] or "material code {}".format(tag["material_code"])]
        if tag["color"]:
            parts.append("#" + tag["color"])
        if tag["weight"]:
            parts.append("{} g".format(tag["weight"]))
        if tag["spool_id"]:
            parts.append("Spoolman spool {}".format(tag["spool_id"]))
        return ", ".join(parts)

    def apply_tag(self, lane: AFCLane, tag: Dict[str, Any], run_gcode) -> List[str]:
        """Set the lane from its tag with AFC's own commands. A Spoolman ID on
        the tag wins; a lane that already has a Spoolman spool is left alone."""
        cmds = []
        if tag.get("spool_id") and getattr(self.afc, "spoolman", None) is not None:
            cmds.append("SET_SPOOL_ID LANE={} SPOOL_ID={}".format(lane.name, tag["spool_id"]))
        elif getattr(lane, "spool_id", None):
            self.logger.info("CFS {}: lane has Spoolman spool {}; tag not applied".format(
                lane.name, lane.spool_id))
        else:
            if tag.get("color"):
                cmds.append("SET_COLOR LANE={} COLOR={}".format(lane.name, tag["color"]))
            if tag.get("material"):
                cmds.append("SET_MATERIAL LANE={} MATERIAL={}".format(lane.name, tag["material"]))
            elif tag.get("material_code"):
                self.logger.info(
                    "CFS {}: material code {} isn't known; add it to rfid_materials in "
                    "[AFC_CFS {}], e.g. rfid_materials: {}=PETG".format(
                        lane.name, tag["material_code"], self.name, tag["material_code"]))
            if tag.get("weight"):
                cmds.append("SET_WEIGHT LANE={} WEIGHT={}".format(lane.name, tag["weight"]))
        for cmd in cmds:
            run_gcode(cmd)
        return cmds

    def apply_lane_state(self, cur_lane: AFCLane, present: bool) -> str:
        """Set a lane's presence and LEDs from the box's sensor; returns a
        PREP-style status message."""
        cur_lane.prep_state = present
        set_lane_sensors(cur_lane, present, self.connected_mask)
        if not present:
            self.lane_not_ready(cur_lane)
            return "EMPTY READY FOR SPOOL"
        self.lane_loaded(cur_lane)
        msg = "FILAMENT PRESENT"
        if (cur_lane.tool_loaded
                and cur_lane.extruder_obj is not None
                and cur_lane.extruder_obj.lane_loaded == cur_lane.name):
            self.ensure_print_mode(cur_lane)
            if self.afc.current == cur_lane.name:
                self.lane_tool_loaded(cur_lane)
            else:
                self.lane_tool_loaded_idle(cur_lane)
            msg += " in ToolHead"
        return msg

    def prep_load(self, lane: AFCLane) -> None:
        # The box pre-loads a newly inserted spool on its own.
        return

    def prep_post_load(self, lane: AFCLane) -> None:
        return

    def eject_lane(self, lane: AFCLane) -> None:
        """AFC's eject hook: reel a lane back onto its spool."""
        ok, detail = self.reel_back(lane)
        log = self.logger.info if ok else self.logger.error
        log("CFS {}: eject {}: {}".format(self.name, lane.name, detail))

    def reel_back(self, lane: AFCLane) -> tuple:
        """
        Reel a slot's filament back into the box with the box motors only.
        Only valid while the filament is short of the extruder gears (e.g.
        after a load that stopped before the handoff); filament gripped by the
        extruder needs a full unload instead.
        Returns (ok, detail).
        """
        if lane.tool_loaded:
            return False, "lane is loaded in the toolhead; use AFC_CFS_TOOL_UNLOAD"
        bits = P.slot_bit(lane.index)
        self.trace.event("eject_start", lane=lane.name, slot=P.slot_letter(lane.index))
        started = self.reactor.monotonic()
        with self.operation():
            done, replies = self.reel_slot(bits)
        toolhead_clear = not lane.get_toolhead_pre_sensor_state()
        seconds = round(self.reactor.monotonic() - started, 2)
        ok = toolhead_clear and done
        if done:
            lane._load_state = False     # out of the shared path
        detail = "retrude stages {}, box {}, toolhead sensor {}".format(
            "/".join(replies), "finished" if done else "did not confirm",
            "clear" if toolhead_clear else "STILL TRIGGERED")
        self.trace.event("eject_end", lane=lane.name, ok=ok, detail=detail, seconds=seconds)
        return ok, detail

    def reel_slot(self, bits: int) -> tuple:
        """
        Reel a slot back into the box: pre-loading off, box idle, then
        RETRUDE_PROCESS stage 0 and stage 1. The caller holds operation().
        Stage 1 is the reel-back itself and is only answered once it has
        finished (12.4s for 1.5m on a K1), so its OK means "done"; generic
        check-ins just answer OK while idle and say nothing.
        Returns (done, [stage reply names]).
        """
        self.pre_loading(P.PRELOAD_CLOSE)
        self.set_box_mode(P.MODE_IDLE)
        self.reactor.pause(self.reactor.monotonic() + self.stage_settle_time)
        replies = []
        reply = self.retrude_stage(bits, 0x00)
        replies.append(P.status_name(reply.status if reply else None))
        # A failed start (RETRUDE_ERR2 when nothing holds the far end) doesn't
        # stop the finish from reeling the slot back, so always send it --
        # unless the box isn't answering at all.
        if reply is not None:
            self.reactor.pause(self.reactor.monotonic() + 0.3)
            reply = self.retrude_stage(bits, 0x01)
            replies.append(P.status_name(reply.status if reply else None))
        done = reply is not None and reply.ok and len(replies) == 2
        self.clear_latched_error()
        return done, replies

    def move_to_hub(self, lane, dist, dir, use_homing=True, speed_mode=None, assist_active=None):
        self.logger.debug("CFS {}: move_to_hub not supported, box handles feeding".format(self.name))
        return False, 0, AFCMoveWarning.ERROR

    def load_then_home(self, lane, distance, assist_active, endstop):
        self.logger.debug("CFS {}: load_then_home not supported, box handles feeding".format(self.name))
        return False, 0, AFCMoveWarning.ERROR

    def calibrate_lane(self, cur_lane, tol):
        return False, self.CALIBRATION_UNSUPPORTED, 0.0

    def calibrate_bowden(self, cur_lane, dis, tol):
        return False, self.CALIBRATION_UNSUPPORTED, 0.0

    def calibrate_td1(self, cur_lane, dis, tol):
        return False, self.CALIBRATION_UNSUPPORTED, 0.0

    def cmd_UNIT_CALIBRATION(self, gcmd):
        prompt = AFCprompt(gcmd, self.logger)
        prompt.create_custom_p("{} Calibration".format(self.name),
                               self.CALIBRATION_UNSUPPORTED, cancel=True)

    def cmd_UNIT_LANE_CALIBRATION(self, gcmd):
        self.cmd_UNIT_CALIBRATION(gcmd)

    def cmd_UNIT_BOW_CALIBRATION(self, gcmd):
        self.cmd_UNIT_CALIBRATION(gcmd)

    def cmd_AFC_UNIT_TD_ONE_CALIBRATION(self, gcmd):
        self.cmd_UNIT_CALIBRATION(gcmd)

    # -- gcode -------------------------------------------------------------

    cmd_AFC_CFS_STATUS_help = "Query a CFS box and report its state"

    def cmd_AFC_CFS_STATUS(self, gcmd: GCodeCommand) -> None:
        """
        Usage
        -----
        `AFC_CFS_STATUS UNIT=<unit>`
        """
        with self.operation():
            if not self.online:
                self.connect()
            if not self.online:
                gcmd.respond_info("CFS {}: offline ({})".format(
                    self.name, self.transport.last_error or "no reply"))
                return
            material = self.read_material_mask()
            connected = self.read_connected_mask()
            buffer_state = self.read_buffer_state()
            box_status = self.read_box_status()
            wheel = self.read_measuring_wheel()

        def slots(mask):
            if mask is None:
                return "?"
            return ",".join(P.slot_letter(i) for i in P.bits_to_slots(mask)) or "none"

        buffer_names = {P.BUFFER_MIDDLE: "middle", P.BUFFER_FULL: "full",
                        P.BUFFER_EMPTY: "empty"}
        lines = [
            "CFS {} @ {} addr {}".format(self.name, self.serial_path, self.box_addr),
            "  version: {}".format(self.version_sn or "unknown"),
            "  box uid: {}{}".format(self.seen_uid.hex() if self.seen_uid else "?",
                                    "" if not self.box_uid or self.seen_uid == self.box_uid
                                    else " (box_uid is {})".format(self.box_uid.hex())),
            "  box state: {}".format(P.status_name(box_status)),
            "  filament present: {}".format(slots(material)),
            "  connected slot: {}".format(slots(connected)),
            "  buffer: {}".format(buffer_names.get(buffer_state, P.status_name(buffer_state))),
            "  measuring wheel: {}".format("%.2f mm" % wheel if wheel is not None else "?"),
            "  cutter sensor: {}".format(
                ("triggered" if self.cutter_sensor_state else "open")
                if self.cutter_sensor_pin else "not configured"),
            "  link: tx {} rx {} timeouts {} crc errors {}".format(
                self.transport.tx_count, self.transport.rx_count,
                self.transport.timeouts, self.transport._parser.crc_errors),
            "  trace: {}".format(self._trace_summary()),
        ]
        for lane in self.lanes.values():
            line = "  {} -> slot {}: {}".format(
                lane.name, P.slot_letter(lane.index),
                "present" if material is not None and material & P.slot_bit(lane.index) else "empty")
            if lane.name in self.lane_tags:
                line += ", tag: {}".format(self._tag_summary(self.lane_tags[lane.name]))
            lines.append(line)
        gcmd.respond_info("\n".join(lines))

    cmd_AFC_CFS_RECONNECT_help = "Close and reopen the link to a CFS box"

    def cmd_AFC_CFS_RECONNECT(self, gcmd: GCodeCommand) -> None:
        with self.operation():
            if len(self.bus.units) == 1:      # other boxes on the chain keep the port
                self.transport.close()
            self.online = False
            ok = self.connect()
        gcmd.respond_info("CFS {}: {}".format(
            self.name, "online" if ok else "offline ({})".format(self.transport.last_error or "no reply")))

    def _trace_summary(self) -> str:
        if not self.trace_file:
            return "off (set trace_file to enable)"
        if not self.trace.enabled:
            return "paused ({})".format(self.trace.last_error or self.trace.path)
        extra = ", {} dropped".format(self.trace.dropped) if self.trace.dropped else ""
        return "recording to {} ({} events{})".format(self.trace.path, self.trace.written, extra)

    cmd_AFC_CFS_TRACE_help = "Control the CFS trace recording or add a marker to it"

    def cmd_AFC_CFS_TRACE(self, gcmd: GCodeCommand) -> None:
        """
        Pause/resume the trace or write a marker into it (e.g. before a test).

        Usage
        -----
        `AFC_CFS_TRACE UNIT=<unit> [ENABLE=<0|1>] [MARK=<text>]`

        Example
        -------
        ```
        AFC_CFS_TRACE UNIT=CFS_1 MARK="test plan step 4, slot A"
        ```
        """
        enable = gcmd.get_int("ENABLE", None, minval=0, maxval=1)
        mark = gcmd.get("MARK", None)
        if enable is not None:
            if enable:
                if not self.trace_file:
                    raise gcmd.error("CFS {}: set trace_file in [AFC_CFS {}] first".format(
                        self.name, self.name))
                self.trace.start()
                self.trace.event("trace_resume")
            else:
                self.trace.event("trace_pause")
                self.trace.enabled = False
        if mark is not None:
            self.trace.event("mark", text=mark)
        gcmd.respond_info("CFS {} trace: {}".format(self.name, self._trace_summary()))

    cmd_AFC_CFS_RAW_help = "Send one raw CFS frame (debugging only)"

    def cmd_AFC_CFS_RAW(self, gcmd: GCodeCommand) -> None:
        """
        Send a single raw request to the box and print the reply. For protocol
        bring-up only -- motor commands sent this way bypass all of AFC's safety.

        Usage
        -----
        `AFC_CFS_RAW UNIT=<unit> FN=<hex> [DATA=<hex>] [ADDR=<int>] [STATUS=<hex>] [TIMEOUT=<s>]`

        Example
        -------
        ```
        AFC_CFS_RAW UNIT=CFS_1 FN=08 DATA=00
        ```
        """
        fn = int(gcmd.get("FN"), 16)
        data = bytes.fromhex(gcmd.get("DATA", ""))
        addr = gcmd.get_int("ADDR", self.box_addr)
        status = int(gcmd.get("STATUS", "FF"), 16)
        timeout = gcmd.get_float("TIMEOUT", 2.0, above=0.)
        with self.operation():
            reply = self.transport.request(addr, fn, data, status=status, timeout=timeout, retries=0)
        tx = P.encode_frame(addr, status, fn, data).hex()
        if reply is None:
            gcmd.respond_info("TX {}\nRX (no reply)".format(tx))
        else:
            gcmd.respond_info("TX {}\nRX {} status={} data={}".format(
                tx, reply.hex(), reply.status_name(), reply.data.hex()))

    def get_status(self, eventtime=None):
        response = super().get_status(eventtime)
        response["cfs"] = {
            "online": self.online,
            "box_addr": self.box_addr,
            "version": self.version_sn,
            "material_mask": self.material_mask,
            "connected_mask": self.connected_mask,
            "buffer_state": self.buffer_state,
            "box_status": P.status_name(self.box_status) if self.box_status is not None else None,
            "cutter_sensor": self.cutter_sensor_state if self.cutter_sensor_pin else None,
            "trace": self.trace.path if self.trace.enabled else None,
            "lanes": {
                lane.name: {
                    "slot": P.slot_letter(lane.index),
                    "present": getattr(lane, "_cfs_present_last", None),
                    "last_load": dict(getattr(lane, "last_load_report", {}) or {}),
                    "tag": self.lane_tags.get(lane.name),
                } for lane in self.lanes.values()
            },
        }
        return response


def load_config_prefix(config):
    return afcCFS(config)
