"""
Runs AFC_CFS / AFC_CFS_lane against a simulated CFS box (tests/cfs_sim.py)
on Klipper's *real* reactor, so the serial I/O, completions, mutex and timer
interplay are exercised for real.

Run as a subprocess (tests/test_AFC_CFS_reactor.py does this) because the AFC
test conftest replaces chelper with a mock, and the real reactor needs the
real one:

    KLIPPER_PATH=~/klipper python3 tests/cfs_reactor_harness.py <scenario>

Prints one JSON object: {"checks": {name: bool}, "info": {...}}.
A reactor watchdog timer runs every 5ms during each scenario; "max_gap"
reports the longest the reactor was ever unable to service it.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from unittest.mock import MagicMock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIRMWARE = os.environ["KLIPPER_PATH"]
KLIPPY = os.path.join(FIRMWARE, "klippy")
sys.path.insert(0, REPO)

# Real reactor + chelper. Klipper keeps klippy/ as a flat module directory;
# Kalico makes it a package (klippy.reactor with relative imports).
if os.path.isfile(os.path.join(KLIPPY, "__init__.py")):
    # Register klippy as a bare package so its __init__ (which imports the
    # whole printer stack, pyserial included) doesn't run; we only need
    # klippy.reactor and its relative imports (chelper, util).
    import types  # noqa: E402
    pkg = types.ModuleType("klippy")
    pkg.__path__ = [KLIPPY]
    sys.modules["klippy"] = pkg
    from klippy import reactor as kreactor  # noqa: E402
else:
    sys.path.append(KLIPPY)
    import reactor as kreactor  # noqa: E402
import tests.conftest  # noqa: E402,F401  installs Klipper mocks for everything else
from tests.conftest import MockAFC  # noqa: E402
from tests.cfs_sim import FakeCFS  # noqa: E402
from extras import AFC_CFS_protocol as P  # noqa: E402
from extras.AFC_CFS import afcCFS  # noqa: E402
from extras.AFC_CFS_lane import AFCCFSLane  # noqa: E402

FAST = {
    "stage_settle_time": 0.02,
    "stage5_poll_interval": 0.03,
    "request_timeout": 0.5,
    "poll_interval": 0.2,
}


class Defaults:
    """Stands in for a Klipper config section: returns each option's real
    default unless overridden."""

    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)

    def getint(self, key, default=None, **kw):
        return int(self.values.get(key, default))

    def getfloat(self, key, default=None, **kw):
        return float(self.values.get(key, default))

    def getboolean(self, key, default=None):
        return bool(self.values.get(key, default))


class Logger:
    def __init__(self):
        self.lines = []

    def _log(self, level, msg):
        self.lines.append("{}: {}".format(level, msg))

    def debug(self, msg):
        self._log("debug", msg)

    def info(self, msg):
        self._log("info", msg)

    def warning(self, msg):
        self._log("warning", msg)

    def error(self, msg):
        self._log("error", msg)


class Watchdog:
    def __init__(self, r, period=0.005):
        self.r = r
        self.period = period
        self.max_gap = 0.0
        self.slow_ticks = 0      # ticks that arrived more than 50ms late
        self.last = None
        self.timer = r.register_timer(self._tick, r.NOW)

    def _tick(self, eventtime):
        now = self.r.monotonic()
        if self.last is not None:
            gap = now - self.last - self.period
            self.max_gap = max(self.max_gap, gap)
            if gap > 0.05:
                self.slow_ticks += 1
        self.last = now
        return now + self.period

    def stop(self):
        self.r.unregister_timer(self.timer)

    @property
    def never_stalled(self):
        """A blocking implementation stalls on every request (dozens of
        100ms+ gaps). A shared test machine adds the odd isolated spike, so
        allow two late ticks but nothing long."""
        return self.slow_ticks <= 2 and self.max_gap < 0.25


class FakeExtruder:
    def __init__(self, sim):
        self.sim = sim
        self.name = "extruder"
        self.tool_start = "pin"
        self.tool_stn = 50.0
        self.tool_load_speed = 25.0
        self.estats = MagicMock()
        self.moved = []
        self.lane_loaded = None

    @property
    def tool_start_state(self):
        return self.sim.toolhead_sensor


class Rig:
    def __init__(self, r, sim, **overrides):
        self.r = r
        self.sim = sim
        self.logger = Logger()

        afc = MockAFC()
        afc.tool_cut = True
        afc.tool_cut_cmd = "AFC_CUT"
        afc.park = False
        afc.park_cmd = None
        afc.form_tip = False
        afc._check_extruder_temp = MagicMock(return_value=False)
        afc.error.handle_lane_failure = MagicMock()
        afc.gcode.run_script_from_command = MagicMock()
        afc.save_vars = MagicMock()
        afc.function.is_printing = MagicMock(return_value=False)
        self.extruder = FakeExtruder(sim)

        def move_e_pos(distance, speed, label="", wait_tool=False):
            self.extruder.moved.append((round(distance, 3), label))
            sim.extruder_move(distance)
            # Scaled-down motion time so slow primes don't make tests slow
            r.pause(r.monotonic() + min(abs(distance) / max(speed, 0.1) * 0.01, 0.2))

        afc.move_e_pos = move_e_pos
        self.afc = afc

        unit = afcCFS.__new__(afcCFS)
        unit.reactor = r
        unit.logger = self.logger
        unit.name = "CFS_1"
        unit.lanes = {}
        unit.afc = afc
        values = dict(FAST)
        values.update(overrides)
        values.setdefault("serial", sim.path)
        unit._load_cfs_config(Defaults(values))
        unit._init_cfs_runtime()
        unit.on_filament_insert = MagicMock()
        unit.on_filament_remove = MagicMock()
        unit.lane_tool_loaded = MagicMock()      # LED handling, needs a full lane
        unit.lane_tool_unloaded = MagicMock()
        self.unit = unit

    def lane(self, index):
        lane = AFCCFSLane.__new__(AFCCFSLane)
        lane.name = "lane{}".format(index)
        lane.index = index
        lane.unit_obj = self.unit
        lane.afc = self.afc
        lane.reactor = self.r
        lane.logger = self.logger
        lane.extruder_obj = self.extruder
        lane.select_lane = MagicMock()
        lane.disable_buffer = MagicMock()
        lane.set_loaded = MagicMock()
        lane.set_unloaded = MagicMock()
        lane.loaded_to_hub = False
        lane.last_load_report = {}
        lane._cfs_present_last = None
        lane.status = None
        lane.tool_loaded = False
        lane.runout_lane = None
        lane.spool_id = None
        lane._afc_prep_done = True
        self.unit.lanes[lane.name] = lane
        return lane

    @property
    def failures(self):
        return [c.args[1] for c in self.afc.error.handle_lane_failure.call_args_list]

    def cut_calls(self):
        return [c.args[0] for c in self.afc.gcode.run_script_from_command.call_args_list
                if c.args[0].startswith("AFC_CUT")]


def wait(r, seconds):
    r.pause(r.monotonic() + seconds)


def is_subsequence(needle, haystack):
    it = iter(haystack)
    return all(any(x == y for y in it) for x in needle)


# -- scenarios ---------------------------------------------------------------

def sc_connect_addressed(r):
    sim = FakeCFS()
    rig = Rig(r, sim)
    ok = rig.unit.connect()
    sim.close()
    return {
        "online": ok and rig.unit.online,
        "version_read": rig.unit.version_sn == "SIMCFS-0001",
        "skipped_discovery": sim.count(P.CMD_GET_SLAVE_INFO) == 0,
    }, {}


def sc_connect_discovery(r):
    sim = FakeCFS(addressed=False)
    rig = Rig(r, sim)
    ok = rig.unit.connect()
    sim.close()
    return {
        "online": ok and rig.unit.online,
        "box_got_address": sim.addr == 1,
        "used_discovery": sim.count(P.CMD_GET_SLAVE_INFO) >= 1,
    }, {}


def sc_retry_and_offline(r):
    sim = FakeCFS()
    rig = Rig(r, sim, request_timeout=0.15)
    rig.unit.connect()
    sim.drop_replies = 1
    retried = rig.unit.read_buffer_state() == P.BUFFER_MIDDLE
    sim.drop_replies = 1000
    t0 = r.monotonic()
    for _ in range(rig.unit.offline_after):
        rig.unit.read_buffer_state()
    dead_box_seconds = r.monotonic() - t0
    went_offline = not rig.unit.online
    sim.drop_replies = 0
    recovered = rig.unit.read_material_mask() == sim.material_mask and rig.unit.online
    sim.close()
    return {
        "retry_recovers_dropped_reply": retried,
        "offline_after_failures": went_offline,
        "back_online_on_reply": recovered,
        # status queries to a dead box fail fast: no waiting for late replies
        "dead_box_fails_fast": dead_box_seconds < 3 * 2 * 0.15 + 1.0,
    }, {"timeouts": rig.unit.transport.timeouts, "dead_box_seconds": round(dead_box_seconds, 2)}


def sc_garbage_resync(r):
    sim = FakeCFS()
    sim.garbage_before_reply = b"\x00\xf7\xff\x13\xf7"
    rig = Rig(r, sim)
    rig.unit.connect()
    masks = [rig.unit.read_material_mask() for _ in range(5)]
    sim.close()
    return {"parses_through_garbage": all(m == sim.material_mask for m in masks)}, {
        "crc_errors": rig.unit.transport._parser.crc_errors}


def sc_poll_presence(r):
    sim = FakeCFS(material_mask=0b0011)
    rig = Rig(r, sim)
    lanes = [rig.lane(i) for i in (1, 2, 3)]
    rig.unit._cfs_handle_ready()
    wait(r, 0.6)
    initial = [l._cfs_present_last for l in lanes]
    sim.material_mask = 0b0110
    wait(r, 0.6)
    after = [l._cfs_present_last for l in lanes]
    rig.unit._cfs_handle_disconnect()
    sim.close()
    return {
        "initial_presence": initial == [True, True, False],
        "presence_follows_box": after == [False, True, True],
        "insert_marks_loaded": lanes[2].set_loaded.called,
        "insert_event_sent": rig.unit.on_filament_insert.called,
        "removal_marks_unloaded": lanes[0].set_unloaded.called,
    }, {"initial": initial, "after": after}


def _prep_lane(rig, lane):
    """Run the unit's PREP hook on a harness lane (AFC's bits mocked)."""
    lane.prep_state = None
    lane._load_state = None
    lane.map = "T{}".format(lane.index - 1)
    lane.send_lane_data = MagicMock()
    lane.do_enable = MagicMock()
    lane.set_afc_prep_done = MagicMock(side_effect=lambda: setattr(lane, "_afc_prep_done", True))
    return rig.unit.system_Test(lane, 0, False, False)


def sc_box_boots_late(r):
    """Box starts answering ~2s into PREP: the first lane waits for it and
    every lane preps normally."""
    sim = FakeCFS(material_mask=0b0011)
    sim.mute = True
    rig = Rig(r, sim, boot_wait=6.0, request_timeout=0.2)
    for name in ("lane_loaded", "lane_not_ready", "lane_tool_loaded", "lane_tool_loaded_idle"):
        setattr(rig.unit, name, MagicMock())
    rig.afc.current = None
    lanes = [rig.lane(i) for i in (1, 2, 3)]
    for l in lanes:
        l._afc_prep_done = False

    def unmute(eventtime):
        sim.mute = False
        return r.NEVER
    r.register_timer(unmute, r.monotonic() + 2.0)
    dog = Watchdog(r)
    started = r.monotonic()
    results = [_prep_lane(rig, l) for l in lanes]
    took = r.monotonic() - started
    dog.stop()
    sim.close()
    return {
        "all_lanes_online": results == [True, True, True],
        "presence_right": [l.prep_state for l in lanes] == [True, True, False],
        # one connect attempt to a silent box takes a few seconds itself
        "waited_for_box": 1.5 < took < 10.0,
        "reactor_never_stalled": dog.never_stalled,
    }, {"took": round(took, 2), "results": results}


def sc_offline_prep_then_online(r):
    """Box never answered during PREP; when it does, lanes sync quietly
    (no insert event) and a tool-loaded lane stays loaded."""
    sim = FakeCFS(material_mask=0b0011)
    sim.mute = True
    rig = Rig(r, sim, boot_wait=0.5, request_timeout=0.2, reconnect_interval=0.5,
              poll_interval=0.2)
    for name in ("lane_loaded", "lane_not_ready", "lane_tool_loaded", "lane_tool_loaded_idle"):
        setattr(rig.unit, name, MagicMock())
    rig.afc.current = None
    lanes = [rig.lane(i) for i in (1, 2, 3)]
    lanes[0].tool_loaded = True
    rig.extruder.lane_loaded = "lane1"
    for l in lanes:
        l._afc_prep_done = False
    results = [_prep_lane(rig, l) for l in lanes]
    rig.unit._cfs_handle_ready()
    sim.mute = False
    wait(r, 3.0)
    rig.unit._cfs_handle_disconnect()
    sim.close()
    return {
        "prep_saw_offline": results == [False, False, False],
        "came_online": rig.unit.online,
        "presence_synced": [l._cfs_present_last for l in lanes] == [True, True, False],
        "no_insert_event": not rig.unit.on_filament_insert.called,
        "tool_loaded_kept": lanes[0].tool_loaded and not lanes[0].set_unloaded.called,
        "tool_led_set": rig.unit.lane_tool_loaded_idle.called,
    }, {"results": results, "online": rig.unit.online,
        "presence": [l._cfs_present_last for l in lanes]}


def _load(rig, lane):
    lane.cmd_AFC_CFS_TOOL_LOAD(None)


def _unload(rig, lane):
    lane.cmd_AFC_CFS_TOOL_UNLOAD(None)


def sc_load_unload(r):
    # Deliberately slow box replies: the reactor must stay responsive anyway
    sim = FakeCFS(reply_delay=0.05, stage5_delay=0.25)
    rig = Rig(r, sim)
    lane = rig.lane(1)
    dog = Watchdog(r)
    rig.unit.connect()

    _load(rig, lane)
    loaded_sensor = sim.toolhead_sensor
    stages = list(sim.stages_seen)
    modes = [d for f, d in sim.log if f == P.SET_BOX_MODE]
    after_load_position = sim.position
    moves_after_load = list(rig.extruder.moved)
    afc_tooled_after_load = (lane.tool_loaded
                             and getattr(rig.extruder, "lane_loaded", None) == lane.name)
    tool_state_warnings = [m for m in rig.logger.lines
                           if "could not record tool state" in m]

    _unload(rig, lane)
    dog.stop()
    sim.close()
    return {
        "no_failures": rig.failures == [],
        "reached_toolhead": loaded_sensor,
        "stage_order": is_subsequence([0, 4, 5, 6, 7], stages),
        "marked_slot_print": bytes([0x01, P.MODE_PRINT]) in modes,
        # The box feeds during the print only while the slot stays in print mode
        "print_mode_kept_after_load": bool(modes) and modes[-1] == bytes([0x01, P.MODE_PRINT]),
        "cleared_latched_error": sim.latched_error == P.ST_OK,
        "primed_then_tool_stn": [m[0] for m in moves_after_load] == [10.0, 5.0, 35.0],
        "cut_before_unload": len(rig.cut_calls()) == 1,
        "toolhead_clear_after_unload": not sim.toolhead_sensor,
        "box_reeled_back": sim.position == 0.0,
        "extruder_released_before_reel": (len(sim.reel_positions) == 1
                                          and sim.reel_positions[0] < sim.grip_len),
        "afc_marked_tool_loaded": afc_tooled_after_load and not tool_state_warnings,
        "afc_marked_tool_unloaded": (not lane.tool_loaded
                                     and getattr(rig.extruder, "lane_loaded", None) is None),
        "connection_motor_stopped": sim.conn_action == P.CONN_STOP,
        "reactor_never_stalled": dog.never_stalled,
    }, {"max_gap_ms": round(dog.max_gap * 1000, 2), "stages": stages,
        "position_after_load": after_load_position, "failures": rig.failures,
        "report": lane.last_load_report, "tool_state_warnings": tool_state_warnings,
        "lane_loaded": getattr(rig.extruder, "lane_loaded", None)}


def sc_slot_switch(r):
    sim = FakeCFS(material_mask=0b0011)
    rig = Rig(r, sim)
    l1, l2 = rig.lane(1), rig.lane(2)
    rig.unit.connect()
    _load(rig, l1)
    first = sim.connected_mask
    _unload(rig, l1)
    _load(rig, l2)
    second = sim.connected_mask
    sim.close()
    return {
        "no_failures": rig.failures == [],
        "slot_a_connected": first == 0x01,
        "slot_b_connected": second == 0x02,
        "toolhead_loaded": sim.toolhead_sensor,
    }, {"failures": rig.failures}


def sc_stall_fails_cleanly(r):
    sim = FakeCFS(feed_per_poll=0.0)
    rig = Rig(r, sim, stage5_stall_time=0.3, load_attempts=2)
    lane = rig.lane(1)
    rig.unit.connect()
    _load(rig, lane)
    sim.close()
    return {
        "reported_failure": len(rig.failures) == 1 and "stalled" in rig.failures[0],
        "tried_twice": sim.stages_seen.count(0) == 2,
        "box_left_stopped": sim.conn_action == P.CONN_STOP and not sim.tighten,
        "extruder_untouched": rig.extruder.moved == [],
    }, {"failures": rig.failures}


def sc_empty_slot(r):
    sim = FakeCFS(material_mask=0b0011)
    rig = Rig(r, sim)
    lane = rig.lane(3)
    rig.unit.connect()
    _load(rig, lane)
    sim.close()
    return {
        "reported_empty": len(rig.failures) == 1 and "no filament" in rig.failures[0],
        "never_started_extrude": sim.count(P.EXTRUDE_PROCESS) == 0,
    }, {"failures": rig.failures}


def sc_offline(r):
    sim = FakeCFS()
    path = sim.path
    sim.close()
    rig = Rig(r, sim, serial="/dev/does-not-exist-cfs")
    lane = rig.lane(1)
    dog = Watchdog(r)
    _load(rig, lane)
    dog.stop()
    return {
        "reported_offline": len(rig.failures) == 1 and "offline" in rig.failures[0],
        "reactor_never_stalled": dog.never_stalled,
    }, {"failures": rig.failures, "unused_path": path}


def sc_trace_recording(r):
    import importlib.util
    import tempfile
    tool_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "tools",
                             "cfs_trace_summary.py")
    spec = importlib.util.spec_from_file_location("cfs_trace_summary", tool_path)
    summary = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(summary)

    tmp = tempfile.mkdtemp(prefix="cfs-trace-")
    path = os.path.join(tmp, "logs", "cfs_trace.jsonl")
    sim = FakeCFS(material_mask=0b0011)
    rig = Rig(r, sim, trace_file=path)
    lane = rig.lane(1)
    rig.lane(2)
    dog = Watchdog(r)
    rig.unit.connect()
    rig.unit._cfs_handle_ready()           # idle polling, should stay out of the trace
    wait(r, 0.7)
    sim.material_mask = 0b0111             # a spool goes in: recorded as presence
    wait(r, 0.5)
    rig.unit._cfs_handle_disconnect()       # stops polling and flushes the trace
    rig.unit.trace.start()                  # reopen to record the tool change
    _load(rig, lane)
    _unload(rig, lane)
    dog.stop()
    rig.unit.trace.stop()
    sim.close()

    events, files, bad = summary.load(path)
    kinds = [e["ev"] for e in events]
    load_end = [e for e in events if e["ev"] == "load_end"]
    polled_fns = {e["fn"] for e in events if e["ev"] == "tx"} & {"GET_BUFFER_STATE"}
    import io
    buf = io.StringIO()
    summary.summarize(events, out=buf)
    text = buf.getvalue()
    return {
        "file_written": len(files) == 1 and bad == 0 and len(events) > 20,
        "has_settings": any(e["ev"] == "trace_start" and e.get("settings") for e in events),
        "online_with_version": any(e["ev"] == "online" and e.get("version") == "SIMCFS-0001"
                                   for e in events),
        "idle_polls_not_traced": not polled_fns,
        "presence_recorded": any(e["ev"] == "presence" and e.get("slots") == "ABC"
                                 for e in events),
        "load_events": all(k in kinds for k in ("load_start", "stage5_result", "handoff",
                                                 "load_end")),
        "load_timed": bool(load_end) and load_end[0].get("ok") and load_end[0]["seconds"] > 0,
        "unload_events": all(k in kinds for k in ("unload_start", "cut", "retract_step",
                                                   "unload_end")),
        "replies_have_latency": all("ms" in e for e in events if e["ev"] == "rx"),
        "summary_reports_load": "Loads: 1 ok, 0 failed" in text and "Unloads: 1 ok" in text,
        "summary_reports_latency": "EXTRUDE_PROCESS" in text and "p95" in text,
        "reactor_never_stalled": dog.never_stalled,
    }, {"events": len(events), "max_gap_ms": round(dog.max_gap * 1000, 2),
        "summary": text}


def _read_trace(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def sc_slow_stage0(r):
    """Real K1 timing: stage 0 blocks ~3.7s and pushes filament forward."""
    sim = FakeCFS(stage_delays={0: 3.7}, stage0_feed=200.0)
    rig = Rig(r, sim)
    lane = rig.lane(1)
    dog = Watchdog(r)
    rig.unit.connect()
    _load(rig, lane)
    dog.stop()
    sim.close()
    return {
        "no_failures": rig.failures == [],
        "reached_toolhead": sim.toolhead_sensor,
        "no_timeouts": rig.unit.transport.timeouts == 0,
        "reactor_never_stalled": dog.never_stalled,
    }, {"failures": rig.failures, "timeouts": rig.unit.transport.timeouts,
        "max_gap_ms": round(dog.max_gap * 1000, 2)}


def sc_late_reply_resync(r):
    """A stage times out anyway: later commands must wait for its late reply
    instead of queueing behind it or being matched to it."""
    import tempfile
    path = os.path.join(tempfile.mkdtemp(prefix="cfs-resync-"), "t.jsonl")
    sim = FakeCFS(stage_delays={0: 2.5})
    rig = Rig(r, sim, stage_timeout=1.0, load_attempts=1, trace_file=path)
    lane = rig.lane(1)
    dog = Watchdog(r)
    rig.unit.connect()
    _load(rig, lane)
    dog.stop()
    rig.unit.trace.stop()
    sim.close()
    events = _read_trace(path)
    kinds = [e["ev"] for e in events]
    late = [e for e in events if e["ev"] == "late_reply"]
    after_timeout = kinds[kinds.index("timeout") + 1:] if "timeout" in kinds else []
    return {
        "only_stage0_timed_out": rig.unit.transport.timeouts == 1,
        "late_reply_recognised": len(late) == 1 and late[0]["fn"] == "EXTRUDE_PROCESS",
        "waited_before_next_command": bool(after_timeout) and after_timeout[0] in (
            "late_reply", "resync"),
        "nothing_left_outstanding": rig.unit.transport._outstanding == 0,
        "failure_reported": len(rig.failures) == 1 and "stage 0" in rig.failures[0],
        "box_left_stopped": sim.conn_action == P.CONN_STOP and not sim.tighten,
        "no_unsolicited_frames": "rx_unsolicited" not in kinds,
        "reactor_never_stalled": dog.never_stalled,
    }, {"kinds_after_timeout": after_timeout[:6], "failures": rig.failures}


def sc_eject_part_fed(r):
    """Filament stopped at the toolhead sensor, short of the gears (the state a
    failed load can leave): AFC_CFS_EJECT reels it back with the box alone."""
    sim = FakeCFS(reel_delay=2.0)   # stage 1 blocks until the reel-back finishes
    rig = Rig(r, sim)
    lane = rig.lane(1)
    rig.unit.connect()
    sim.position = 610.0            # past the sensor (600), short of the gears (620)
    before = sim.toolhead_sensor
    t0 = r.monotonic()
    ok, detail = rig.unit.reel_back(lane)
    seconds = r.monotonic() - t0
    sim.close()
    return {
        "started_at_sensor": before,
        "eject_ok": ok,
        "toolhead_clear": not sim.toolhead_sensor,
        "box_reeled_back": sim.position == 0.0,
        "latched_error_cleared": sim.latched_error == P.ST_OK,
        # done when stage 1 answers, not after waiting out check-ins
        "finishes_with_stage1": seconds < 4.0,
        "no_checkins_needed": sim.count(P.RETRUDE_PROCESS) == 2,
    }, {"detail": detail, "seconds": round(seconds, 2)}


def sc_unload_still_gripped(r):
    """Retract too short to leave the gears: the reel-back fails and the
    unload reports it instead of claiming success or hanging."""
    sim = FakeCFS()
    rig = Rig(r, sim, unload_retract_steps="5:600", unload_timeout=1.0)
    lane = rig.lane(1)
    dog = Watchdog(r)
    rig.unit.connect()
    _load(rig, lane)
    _unload(rig, lane)
    dog.stop()
    sim.close()
    return {
        "unload_failed": len(rig.failures) == 1,
        "reported_reel_problem": bool(rig.failures) and "unload failed" in rig.failures[0],
        "box_kept_filament_out": sim.position > 0.0,
        "afc_still_tool_loaded": lane.tool_loaded,
        "reactor_never_stalled": dog.never_stalled,
    }, {"failures": rig.failures, "max_gap_ms": round(dog.max_gap * 1000, 2),
        "reel_positions": sim.reel_positions}


SCENARIOS = {name[3:]: fn for name, fn in globals().items() if name.startswith("sc_")}


def run(name):
    r = kreactor.Reactor()
    out = {"checks": {}, "info": {}}

    def body(eventtime):
        try:
            checks, info = SCENARIOS[name](r)
            out["checks"] = checks
            out["info"] = info
        except Exception:
            out["exception"] = traceback.format_exc()
        finally:
            r.end()
        return r.NEVER

    r.register_timer(body, r.NOW)
    r.run()
    return out


if __name__ == "__main__":
    names = sys.argv[1:] or sorted(SCENARIOS)
    results = {n: run(n) for n in names}
    print(json.dumps(results if len(names) > 1 else results[names[0]], default=str))
