# AFCProject Automated Filament Changer Software
#
# Creality CFS (Color Filament System) wire protocol.
#
# Pure-Python helpers with no Klipper imports so they can be unit tested on
# their own. The transport (serial port + Klipper reactor) lives in AFC_CFS.py.
#
# Protocol knowledge comes from the community reverse-engineering work of:
#   - tomaspakosta/k1c-cfs-klipper   (hardware-validated client, PROTOCOL.md)
#   - ityshchenko/klipper-cfs        (CRC8/framing + real captured traffic)
#   - gitstonelabs/creality-cfs-klipper, FrederickAlt's K1 CFS docs,
#     fake-name/cfs-reverse-engineering, Jacob10383/k2-plus-custom-firmware
# All GPL-3.0, same as this project.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import struct
from typing import List, NamedTuple, Optional

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
