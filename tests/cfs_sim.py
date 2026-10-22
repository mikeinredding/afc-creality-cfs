"""
A simulated Creality CFS box on a pseudo-terminal, for exercising AFC_CFS
against Klipper's real reactor without hardware.

It speaks the real wire format (via AFC_CFS_protocol) and models just enough
physics to drive the load/unload sequences:

  position  mm of filament pushed from the slot toward the toolhead
  path_len  position at which the toolhead entry sensor triggers
  grip_len  position beyond which the extruder gears hold the filament
            (the box cannot reel it back until the extruder retracts)

Behaviour mirrors what tomaspakosta observed on real hardware: EXTRUDE_PROCESS
fails with EXTRUDE_ERR8 unless the connection motor was set to EXTRUDE first,
stage-5 replies carry a big-endian float measuring-wheel reading, and an
addressed box ignores broadcast discovery.
"""
from __future__ import annotations

import os
import pty
import select
import struct
import threading
import time
import tty

from extras import AFC_CFS_protocol as P


class FakeCFS:
    def __init__(self, material_mask=0b0011, addressed=True, addr=1,
                 path_len=600.0, grip_len=620.0, feed_per_poll=60.0,
                 reply_delay=0.02, stage5_delay=0.15, stage_delays=None,
                 stage0_feed=0.0, reel_delay=0.0):
        self.addr = addr if addressed else None
        self.uid = bytes(range(0x10, 0x1C))  # 12 bytes
        self.material_mask = material_mask
        self.path_len = path_len
        self.grip_len = grip_len
        self.feed_per_poll = feed_per_poll
        self.reply_delay = reply_delay
        self.stage5_delay = stage5_delay
        # EXTRUDE_PROCESS stage -> seconds the box spends before answering.
        # The real box blocks on motor stages (stage 0 took ~3.7s on a K1)
        # and handles one command at a time, so later commands queue.
        self.stage_delays = dict(stage_delays or {})
        self.stage0_feed = stage0_feed        # mm stage 0 pushes toward the toolhead
        self.reel_positions = []              # filament position at each stage-1 reel
        self.reel_delay = reel_delay          # seconds RETRUDE stage 1 blocks (K1: 12.4)

        self.position = 0.0
        self.wheel = 0.0
        self.conn_action = P.CONN_STOP
        self.tighten = False
        self.connected_mask = 0
        self.mode = P.MODE_IDLE
        self.stages_seen = []
        self.retruding = False
        self.latched_error = P.ST_OK
        self.log = []                 # (fn, data) of every request
        self.drop_replies = 0         # fault injection: swallow the next N replies
        self.garbage_before_reply = b""
        self.lock = threading.Lock()

        self.master, self.slave = pty.openpty()
        tty.setraw(self.master)
        self.path = os.ttyname(self.slave)
        self._parser = P.FrameParser()
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # -- harness-facing ----------------------------------------------------

    @property
    def toolhead_sensor(self) -> bool:
        return self.position >= self.path_len

    def extruder_move(self, distance: float) -> None:
        """Called when the (fake) printer moves its extruder."""
        with self.lock:
            if self.position >= self.path_len - 1.0:
                self.position = max(0.0, self.position + distance)

    def close(self):
        self._stop = True
        self._thread.join(timeout=1.0)
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def count(self, fn: int) -> int:
        return sum(1 for f, _ in self.log if f == fn)

    # -- box side ----------------------------------------------------------

    def _run(self):
        while not self._stop:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if not r:
                continue
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                return
            for frame in self._parser.feed(chunk):
                reply = self._handle(frame)
                if reply is None:
                    continue
                if self.drop_replies > 0:
                    self.drop_replies -= 1
                    continue
                delay = self.reply_delay
                if frame.fn == P.EXTRUDE_PROCESS and len(frame.data) > 1:
                    stage = frame.data[1]
                    if stage in self.stage_delays:
                        delay = self.stage_delays[stage]
                    elif stage == 5:
                        delay = self.stage5_delay
                elif frame.fn == P.RETRUDE_PROCESS and len(frame.data) > 1 \
                        and frame.data[0] and frame.data[1] == 1:
                    delay = self.reel_delay
                time.sleep(delay)
                os.write(self.master, self.garbage_before_reply + reply)

    def _reply(self, addr, status, fn, data=b""):
        return P.encode_frame(addr, status, fn, data)

    def _handle(self, f: P.Frame):
        self.log.append((f.fn, bytes(f.data)))
        # Addressing family
        if f.fn == P.CMD_GET_SLAVE_INFO:
            if f.addr != P.BROADCAST_BOXES or self.addr is not None:
                return None  # addressed boxes ignore broadcast discovery
            return self._reply(P.BROADCAST_BOXES, P.ST_OK, f.fn, b"\x00\x01" + self.uid)
        if f.fn == P.CMD_SET_SLAVE_ADDR:
            if bytes(f.data[1:13]) == self.uid:
                self.addr = f.data[0]
                return self._reply(P.BROADCAST_BOXES, P.ST_OK, f.fn, self.uid)
            return None
        if self.addr is None or f.addr != self.addr:
            return None
        a = self.addr

        with self.lock:
            if f.fn == P.CMD_ONLINE_CHECK:
                return self._reply(a, P.ST_OK, f.fn, self.uid)
            if f.fn == P.GET_VERSION_SN:
                return self._reply(a, P.ST_OK, f.fn, b"SIMCFS-0001")
            if f.fn == P.GET_FILAMENT_SENSOR_STATE:
                mask = self.material_mask if f.data[:1] == b"\x00" else self.connected_mask
                return self._reply(a, P.ST_OK, f.fn, bytes([mask]))
            if f.fn == P.GET_BUFFER_STATE:
                return self._reply(a, P.ST_OK, f.fn, bytes([P.BUFFER_MIDDLE]))
            if f.fn == P.GET_BOX_STATE:
                return self._reply(a, self.latched_error, f.fn, b"\x00\x00\x00\x00")
            if f.fn == P.GET_MEASURING_WHEEL:
                return self._reply(a, P.ST_OK, f.fn, struct.pack(">f", -self.wheel))
            if f.fn == P.SET_BOX_MODE:
                slot, mode = f.data[0], f.data[1]
                self.mode = mode
                if mode == P.MODE_PRINT and slot:
                    self.connected_mask = slot
                if mode == P.MODE_IDLE:
                    self.latched_error = P.ST_OK
                return self._reply(a, P.ST_OK, f.fn)
            if f.fn == P.CTRL_CONNECTION_MOTOR_ACTION:
                self.conn_action = f.data[0]
                return self._reply(a, P.ST_OK, f.fn)
            if f.fn == P.TIGHTEN_UP_ENABLE:
                self.tighten = bool(f.data[0])
                return self._reply(a, P.ST_OK, f.fn)
            if f.fn == P.SET_PRE_LOADING:
                return self._reply(a, P.ST_OK, f.fn)
            if f.fn == P.EXTRUDE_PROCESS:
                return self._extrude(a, f)
            if f.fn == P.RETRUDE_PROCESS:
                return self._retrude(a, f)
        return self._reply(a, P.ST_PARAMS_ERR, f.fn)

    def _extrude(self, a, f):
        if len(f.data) != 3:
            return self._reply(a, P.ST_PARAMS_ERR, f.fn)
        slot, stage = f.data[0], f.data[1]
        if self.conn_action != P.CONN_EXTRUDE:
            return self._reply(a, P.ST_EXTRUDE_ERR8, f.fn)
        if not slot & self.material_mask:
            return self._reply(a, P.ST_EXTRUDE_ERR10, f.fn)
        self.stages_seen.append(stage)
        if stage == 0:
            self.retruding = False
            self.position = min(self.position + self.stage0_feed, self.path_len)
        if stage == 5:
            if self.position < self.path_len:
                step = min(self.feed_per_poll, self.path_len - self.position)
                self.position += step
                self.wheel += step
            return self._reply(a, P.ST_OK, f.fn, struct.pack(">f", -self.wheel))
        if stage == 7:
            self.latched_error = P.ST_EXTRUDE_ERR8  # mimic the post-load latch
        return self._reply(a, P.ST_OK, f.fn)

    def _retrude(self, a, f):
        slot, stage = f.data[0], f.data[1]
        if slot:
            self.retruding = True
            # Stage 1 is the reel-back: the real box answers once it's done.
            # Filament the extruder still grips can't be reeled.
            if stage == 1:
                self.reel_positions.append(self.position)
                if self.position >= self.grip_len:
                    # Modelled: the reel motor stalls against the gears and the
                    # box reports a retrude error (real status code unconfirmed).
                    self.retruding = False
                    return self._reply(a, P.ST_RETRUDE_ERR2, f.fn)
                self.position = 0.0
                self.retruding = False
                self.connected_mask = 0
            return self._reply(a, P.ST_OK, f.fn)
        # Generic check-in [0x00, 0x00]: reel back what the extruder released
        if self.retruding and self.position < self.grip_len:
            self.position = max(0.0, self.position - 300.0)
            if self.position == 0.0:
                self.retruding = False
                self.connected_mask = 0
                return self._reply(a, P.ST_RETRUDE_DONE, f.fn)
            return self._reply(a, P.ST_OK, f.fn)
        if not self.retruding:
            return self._reply(a, P.ST_RETRUDE_DONE, f.fn)
        return self._reply(a, P.ST_OK, f.fn)
