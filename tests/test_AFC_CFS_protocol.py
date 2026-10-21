"""Framing, CRC and parser tests for AFC_CFS_protocol.

The vectors are real traffic: the first group was captured with interceptty
from a stock printer talking to a CFS (via ityshchenko/klipper-cfs), the second
was captured live by tomaspakosta/k1c-cfs-klipper. If encode_frame() doesn't
reproduce them byte-for-byte, the framing is wrong.
"""
from __future__ import annotations

import struct

import pytest

from extras import AFC_CFS_protocol as P

CAPTURED = [
    (0x01, 0x00, 0xA3, b"", "f7010300a3dd"),
    (0x02, 0x00, 0xA3, b"", "f7020300a3dd"),
    (0x04, 0x00, 0xA3, b"", "f7040300a3dd"),
    (0x01, 0xFF, 0x04, bytes([0x00, 0x01]), "f70105ff04000190"),
    (0x01, 0xFF, 0x14, b"", "f70103ff1406"),
    (0x01, 0xFF, 0x0D, bytes([0x0F, 0x01]), "f70105ff0d0f0169"),
    (0x01, 0xFF, 0x0A, b"", "f70103ff0a5c"),
    (0xFE, 0x00, 0xA1, bytes([0xFE, 0xFE]), "f7fe0500a1fefef8"),
    (0x01, 0x00, 0xA2, b"", "f7010300a2da"),
    (0x01, 0xFF, 0x07, bytes([0x01]), "f70104ff07011f"),
    (0x01, 0xFF, 0x07, bytes([0x00]), "f70104ff070018"),
    (0x01, 0xFF, 0x0F, bytes([0x01]), "f70104ff0f01b7"),
    (0x01, 0xFF, 0x11, bytes([0x01, 0x00]), "f70105ff110100e0"),
]


@pytest.mark.parametrize("addr,status,fn,data,expected", CAPTURED)
def test_encode_matches_captured_traffic(addr, status, fn, data, expected):
    assert P.encode_frame(addr, status, fn, data).hex() == expected


@pytest.mark.parametrize("addr,status,fn,data,expected", CAPTURED)
def test_parser_round_trips_captured_traffic(addr, status, fn, data, expected):
    frames = P.FrameParser().feed(bytes.fromhex(expected))
    assert frames == [P.Frame(addr, status, fn, data)]


def test_parser_handles_split_chunks():
    parser = P.FrameParser()
    raw = bytes.fromhex("f70105ff04000190f70103ff1406")
    frames = []
    for i in range(len(raw)):
        frames += parser.feed(raw[i:i + 1])
    assert [f.fn for f in frames] == [0x04, 0x14]


def test_parser_resyncs_past_garbage_and_bad_crc():
    parser = P.FrameParser()
    good = P.encode_frame(1, 0, P.GET_BUFFER_STATE, b"\x00")
    bad = bytearray(P.encode_frame(1, 0, P.GET_BOX_STATE, b"\x01\x02"))
    bad[-1] ^= 0xFF
    frames = parser.feed(b"\x00\xf7\xff" + bytes(bad) + good)
    assert frames == [P.Frame(1, 0, P.GET_BUFFER_STATE, b"\x00")]
    assert parser.crc_errors == 1


def test_parser_rejects_absurd_length_header():
    parser = P.FrameParser()
    good = P.encode_frame(1, 0, P.GET_VERSION_SN, b"SN")
    assert parser.feed(b"\xf7\x01\xfe" + good) == [P.Frame(1, 0, P.GET_VERSION_SN, b"SN")]


def test_measuring_wheel_decode_matches_hardware_capture():
    # Stage-5 telemetry captured when filament reached the toolhead sensor
    assert P.decode_float_be(bytes.fromhex("c5afa071")) == pytest.approx(-5620.055, abs=1e-3)
    assert P.decode_float_be(b"\x00\x00") is None
    assert P.decode_float_be(struct.pack(">f", 12.5)) == 12.5


def test_slot_mapping():
    assert [P.slot_bit(i) for i in range(1, 5)] == [0x01, 0x02, 0x04, 0x08]
    assert [P.slot_letter(i) for i in range(1, 5)] == ["A", "B", "C", "D"]
    assert P.bits_to_slots(0b1010) == [2, 4]
    with pytest.raises(ValueError):
        P.slot_bit(5)


def test_status_names():
    assert P.status_name(None) == "NO_REPLY"
    assert P.status_name(P.ST_EXTRUDE_ERR8) == "EXTRUDE_ERR8"
    assert P.status_name(0x7E) == "0x7E"
