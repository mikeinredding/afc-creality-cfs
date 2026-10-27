# AFCProject Automated Filament Changer Software
#
# Creality CFS (Color Filament System) unit for AFC.
#
# The CFS is a closed box: it moves filament itself and reports its own
# sensors over RS-485 (a CH340 USB adapter is built into its cable). Klipper
# never drives a CFS motor directly. This unit:
#   - owns the serial link to one box (non-blocking, reactor-driven),
#   - polls the box and mirrors per-slot filament presence into AFC lanes,
#   - exposes the box operations that AFC_CFS_lane uses for tool load/unload.
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
import termios
import threading
import time
import traceback
from typing import TYPE_CHECKING, Any, Dict, List, Optional

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
    from extras.AFC_lane import AFCMoveWarning
except Exception:
    raise CONFIG_ERROR(ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc()))

try:
    from extras.AFC_respond import AFCprompt
except Exception:
    raise CONFIG_ERROR(ERROR_STR.format(import_lib="AFC_respond", trace=traceback.format_exc()))

try:
    from extras import AFC_CFS_protocol as P
except Exception:
    raise CONFIG_ERROR(ERROR_STR.format(import_lib="AFC_CFS_protocol", trace=traceback.format_exc()))

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


class afcCFS(afcUnit):
    """
    One Creality CFS box. Lanes are [AFC_CFS_lane laneN] with unit: <name>:1..4
    mapping to physical slots A..D (left to right).
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
        super().handle_connect()
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
            self.unit._operation_depth += 1
            return self.unit

        def __exit__(self, *exc):
            self.unit._operation_depth -= 1
            return False

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
            update = getattr(lane, "cfs_update_presence", None)
            if update is None:
                continue
            try:
                update(bool(mask & P.slot_bit(lane.index)))
            except ValueError:
                pass

    def _mirror_connections(self, mask: int) -> None:
        for lane in self.lanes.values():
            if hasattr(lane, "cfs_update_presence"):
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
            if hasattr(cur_lane, "_cfs_present_last"):
                cur_lane._cfs_present_last = None
            msg = "<span class=error--text>CFS NOT RESPONDING</span>"
            self.lane_not_ready(cur_lane)
        else:
            present = bool(self.material_mask is not None
                           and self.material_mask & P.slot_bit(cur_lane.index))
            if hasattr(cur_lane, "_cfs_present_last"):
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
            lines.append("  {} -> slot {}: {}".format(
                lane.name, P.slot_letter(lane.index),
                "present" if material is not None and material & P.slot_bit(lane.index) else "empty"))
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
        }
        return response


def load_config_prefix(config):
    return afcCFS(config)
