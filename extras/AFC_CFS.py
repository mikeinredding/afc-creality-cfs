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
import os
import termios
import traceback
from typing import TYPE_CHECKING, Any, Dict, Optional

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
                    and not pending["completion"].test():
                pending["completion"].complete(frame)
            elif self.debug:
                self.logger.debug("{}: unsolicited frame {}".format(self.name, frame.hex()))

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
                retries: int = 1) -> Optional[P.Frame]:
        """
        Send one request and wait (cooperatively) for the matching reply.
        Returns the reply frame, or None after all retries time out.
        """
        with self._mutex:
            for attempt in range(retries + 1):
                if self.fd is None and not self.open():
                    return None
                frame = P.encode_frame(addr, status, fn, data)
                completion = self.reactor.completion()
                self._pending = {"fn": fn, "completion": completion}
                if self.debug:
                    self.logger.debug("{}: TX {} {}".format(self.name, P.fn_name(fn), frame.hex()))
                if not self._write_all(frame):
                    self._pending = None
                    continue
                self.tx_count += 1
                result = completion.wait(self.reactor.monotonic() + timeout)
                self._pending = None
                if result is not None:
                    return result
                self.timeouts += 1
                if self.debug:
                    self.logger.debug("{}: timeout {} (attempt {}/{})".format(
                        self.name, P.fn_name(fn), attempt + 1, retries + 1))
            return None

    @property
    def busy(self) -> bool:
        return self._mutex.test()


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

        self._init_cfs_runtime()

        self.printer.register_event_handler("klippy:ready", self._cfs_handle_ready)
        self.printer.register_event_handler("klippy:disconnect", self._cfs_handle_disconnect)

        self.gcode.register_mux_command("AFC_CFS_STATUS", "UNIT", self.name,
                                        self.cmd_AFC_CFS_STATUS, desc=self.cmd_AFC_CFS_STATUS_help)
        self.gcode.register_mux_command("AFC_CFS_RECONNECT", "UNIT", self.name,
                                        self.cmd_AFC_CFS_RECONNECT, desc=self.cmd_AFC_CFS_RECONNECT_help)
        self.gcode.register_mux_command("AFC_CFS_RAW", "UNIT", self.name,
                                        self.cmd_AFC_CFS_RAW, desc=self.cmd_AFC_CFS_RAW_help)

    def _load_cfs_config(self, config) -> None:
        """Read every CFS-specific option. Kept separate so tests can apply
        the real defaults without constructing a full AFC unit."""
        self.serial_path = config.get("serial")
        self.baud = config.getint("baud", P.DEFAULT_BAUD)
        self.box_addr = config.getint("box_addr", 1, minval=1, maxval=4)
        self.poll_interval = config.getfloat("poll_interval", 1.0, minval=0.2)
        self.request_timeout = config.getfloat("request_timeout", 0.8, minval=0.05)
        self.offline_after = config.getint("offline_after_failures", 3, minval=1)
        self.reconnect_interval = config.getfloat("reconnect_interval", 5.0, minval=0.5)
        self.protocol_debug = config.getboolean("protocol_debug", False)

        # --- Tool load sequence (EXTRUDE_PROCESS stages 0/4/5/6/7) ---
        self.load_attempts = config.getint("load_attempts", 2, minval=1)
        self.stage_settle_time = config.getfloat("stage_settle_time", 0.3, minval=0.)
        self.stage5_poll_interval = config.getfloat("stage5_poll_interval", 0.4, minval=0.05)
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
        self.clear_mode_after_load = config.getboolean("clear_mode_after_load", True)

        # --- Tool unload sequence (cut, RETRUDE_PROCESS, chunked retract) ---
        # Extruder retract steps after the cut, "distance:speed_mm_per_min",
        # with a box check-in before each step. Stock: 6 x 15mm, 90 then 500 mm/min.
        self.unload_retract_steps = self._parse_steps(config.get(
            "unload_retract_steps", "15:90, 15:90, 15:500, 15:500, 15:500, 15:500"))
        # Optional tip-forming wiggle before retracting. Not needed with a cutter.
        self.unload_tip_wiggle = config.getboolean("unload_tip_wiggle", False)
        self.unload_timeout = config.getfloat("unload_timeout", 30.0, minval=1.)
        self.cutter_wait_time = config.getfloat("cutter_wait_time", 3.0, minval=0.1)

        # Optional cutter switch on the CFS toolhead kit (Klipper MCU pin)
        self.cutter_sensor_pin = config.get("cutter_sensor_pin", None)

    def _init_cfs_runtime(self) -> None:
        self.cutter_sensor_state = False
        self.transport = CFSTransport(self.reactor, self.serial_path, self.baud,
                                      self.logger, "CFS {}".format(self.name),
                                      debug=self.protocol_debug)

        # Box state, as last seen
        self.online = False
        self.version_sn: Optional[str] = None
        self.material_mask: Optional[int] = None
        self.connected_mask: Optional[int] = None
        self.buffer_state: Optional[int] = None
        self.box_status: Optional[int] = None
        self._consecutive_failures = 0
        self._last_reconnect_attempt = 0.0
        self._operation_depth = 0
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
        self.logo = '<span class=success--text>CFS Ready</span>\n'
        self.logo_error = '<span class=error--text>CFS Not Ready</span>\n'

    def _cfs_handle_ready(self):
        self._poll_timer = self.reactor.register_timer(self._poll, self.reactor.NOW)

    def _cfs_handle_disconnect(self):
        if self._poll_timer is not None:
            self.reactor.unregister_timer(self._poll_timer)
            self._poll_timer = None
        self.transport.close()

    def _cutter_callback(self, eventtime, state):
        self.cutter_sensor_state = bool(state)

    # -- connection --------------------------------------------------------

    def connect(self) -> bool:
        """
        Open the port and make sure the box answers at box_addr.

        A CFS remembers its RS-485 address across power cycles and stops
        answering broadcast discovery once addressed, so probe the configured
        address first and only fall back to discovery for a fresh box.
        """
        if not self.transport.open():
            self._set_offline(self.transport.last_error)
            return False

        for status in (P.REQ_STATUS_ADDRESSING, P.REQ_STATUS_NORMAL):
            reply = self.transport.request(self.box_addr, P.CMD_ONLINE_CHECK, b"",
                                           status=status, timeout=self.request_timeout)
            if reply is not None:
                return self._set_online()

        self.logger.info("CFS {}: no reply at address {}, trying discovery".format(
            self.name, self.box_addr))
        reply = self.transport.request(P.BROADCAST_BOXES, P.CMD_GET_SLAVE_INFO,
                                       bytes([P.BROADCAST_BOXES, P.BROADCAST_BOXES]),
                                       status=P.REQ_STATUS_ADDRESSING,
                                       timeout=max(self.request_timeout, 1.5), retries=2)
        if reply is None or len(reply.data) < 14:
            self._set_offline("box did not answer discovery")
            return False
        uid = bytes(reply.data[2:14])
        self.transport.request(P.BROADCAST_BOXES, P.CMD_SET_SLAVE_ADDR,
                               bytes([self.box_addr]) + uid,
                               status=P.REQ_STATUS_ADDRESSING, timeout=1.5)
        reply = self.transport.request(self.box_addr, P.CMD_ONLINE_CHECK, b"",
                                       status=P.REQ_STATUS_ADDRESSING,
                                       timeout=self.request_timeout)
        if reply is None:
            self._set_offline("box did not answer after addressing")
            return False
        self.logger.info("CFS {}: addressed box uid {} at {}".format(
            self.name, uid.hex(), self.box_addr))
        return self._set_online()

    def _set_online(self) -> bool:
        if not self.online:
            self.online = True
            self._consecutive_failures = 0
            reply = self.transport.request(self.box_addr, P.GET_VERSION_SN,
                                           timeout=self.request_timeout)
            if reply is not None:
                self.version_sn = P.decode_ascii(reply.data)
            self.logger.info("CFS {}: online (addr {}, version {})".format(
                self.name, self.box_addr, self.version_sn or "unknown"))
        return True

    def _set_offline(self, reason: Optional[str]) -> None:
        if self.online:
            self.logger.warning("CFS {}: offline ({})".format(self.name, reason))
        self.online = False

    # -- box requests (used by lanes) --------------------------------------

    def request(self, fn: int, data: bytes = b"", timeout: Optional[float] = None,
                retries: int = 1) -> Optional[P.Frame]:
        reply = self.transport.request(self.box_addr, fn, data,
                                       timeout=timeout or self.request_timeout,
                                       retries=retries)
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
        return self.request(P.SET_PRE_LOADING, bytes([slot_bits, action]), timeout=timeout)

    def extrude_stage(self, slot_bits: int, stage: int, arg: int = 0x00,
                      timeout: Optional[float] = None) -> Optional[P.Frame]:
        return self.request(P.EXTRUDE_PROCESS, bytes([slot_bits, stage, arg]),
                            timeout=timeout, retries=0)

    def retrude_stage(self, slot_bits: int, stage: int,
                      timeout: Optional[float] = None) -> Optional[P.Frame]:
        return self.request(P.RETRUDE_PROCESS, bytes([slot_bits, stage]),
                            timeout=timeout, retries=0)

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
        if self._operation_depth > 0 or self.transport.busy:
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

        try:
            mask = self.read_material_mask()
            if mask is not None:
                self._mirror_presence(mask)
            self.read_buffer_state()
        except Exception:
            self.logger.debug("CFS {}: poll failed\n{}".format(self.name, traceback.format_exc()))
        return self.reactor.monotonic() + self.poll_interval

    def _mirror_presence(self, mask: int) -> None:
        for lane in self.lanes.values():
            update = getattr(lane, "cfs_update_presence", None)
            if update is None:
                continue
            try:
                update(bool(mask & P.slot_bit(lane.index)))
            except ValueError:
                pass

    # -- AFC unit hooks ----------------------------------------------------

    def system_Test(self, cur_lane: AFCLane, delay: float, assignTcmd: bool,
                    enable_movement: bool) -> bool:
        """Restore lane state during PREP from the box's own sensors."""
        if not self.online:
            self.connect()
        if self.online and self.material_mask is None:
            self.read_material_mask()

        present = bool(self.material_mask is not None
                       and self.material_mask & P.slot_bit(cur_lane.index))
        cur_lane.prep_state = present
        cur_lane._load_state = present
        if hasattr(cur_lane, "_cfs_present_last"):
            cur_lane._cfs_present_last = present

        if not self.online:
            msg = "<span class=error--text>CFS NOT RESPONDING</span>"
            self.lane_not_ready(cur_lane)
        elif not present:
            msg = "EMPTY READY FOR SPOOL"
            self.lane_not_ready(cur_lane)
        else:
            msg = "FILAMENT PRESENT"
            self.lane_loaded(cur_lane)
            if (cur_lane.tool_loaded
                    and cur_lane.extruder_obj is not None
                    and cur_lane.extruder_obj.lane_loaded == cur_lane.name):
                if self.afc.current == cur_lane.name:
                    self.lane_tool_loaded(cur_lane)
                else:
                    self.lane_tool_loaded_idle(cur_lane)
                msg += " in ToolHead"

        if assignTcmd:
            self.afc.function.TcmdAssign(cur_lane)
        cur_lane.send_lane_data()
        cur_lane.do_enable(False)
        self.logger.info("{lane} tool cmd: {tcmd:3} {msg}".format(
            lane=cur_lane.name, tcmd=cur_lane.map_to_string(), msg=msg))
        cur_lane.set_afc_prep_done()
        return self.online

    def prep_load(self, lane: AFCLane) -> None:
        # The box pre-loads a newly inserted spool on its own.
        return

    def prep_post_load(self, lane: AFCLane) -> None:
        return

    def eject_lane(self, lane: AFCLane) -> None:
        """Reel a lane's filament back onto its spool (only valid when not in the toolhead)."""
        if lane.tool_loaded:
            self.logger.error("CFS {}: unload {} from the toolhead before ejecting".format(
                self.name, lane.name))
            return
        bits = P.slot_bit(lane.index)
        with self.operation():
            self.pre_loading(P.PRELOAD_CLOSE)
            self.set_box_mode(P.MODE_IDLE)
            for stage in (0x00, 0x01):
                reply = self.retrude_stage(bits, stage, timeout=5.0)
                self.logger.debug("CFS {}: eject {} retrude stage {} -> {}".format(
                    self.name, lane.name, stage, P.status_name(reply.status if reply else None)))
                self.reactor.pause(self.reactor.monotonic() + 0.5)
            self.clear_latched_error()

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
        ]
        for lane in self.lanes.values():
            lines.append("  {} -> slot {}: {}".format(
                lane.name, P.slot_letter(lane.index),
                "present" if material is not None and material & P.slot_bit(lane.index) else "empty"))
        gcmd.respond_info("\n".join(lines))

    cmd_AFC_CFS_RECONNECT_help = "Close and reopen the link to a CFS box"

    def cmd_AFC_CFS_RECONNECT(self, gcmd: GCodeCommand) -> None:
        with self.operation():
            self.transport.close()
            self.online = False
            ok = self.connect()
        gcmd.respond_info("CFS {}: {}".format(
            self.name, "online" if ok else "offline ({})".format(self.transport.last_error or "no reply")))

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
        }
        return response


def load_config_prefix(config):
    return afcCFS(config)
