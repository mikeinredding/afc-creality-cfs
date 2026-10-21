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
        self.last = None
        self.timer = r.register_timer(self._tick, r.NOW)

    def _tick(self, eventtime):
        now = self.r.monotonic()
        if self.last is not None:
            self.max_gap = max(self.max_gap, now - self.last - self.period)
        self.last = now
        return now + self.period

    def stop(self):
        self.r.unregister_timer(self.timer)


class FakeExtruder:
    def __init__(self, sim):
        self.sim = sim
        self.name = "extruder"
        self.tool_start = "pin"
        self.tool_stn = 50.0
        self.tool_load_speed = 25.0
        self.estats = MagicMock()
        self.moved = []

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
    for _ in range(rig.unit.offline_after):
        rig.unit.read_buffer_state()
    went_offline = not rig.unit.online
    sim.drop_replies = 0
    recovered = rig.unit.read_material_mask() == sim.material_mask and rig.unit.online
    sim.close()
    return {
        "retry_recovers_dropped_reply": retried,
        "offline_after_failures": went_offline,
        "back_online_on_reply": recovered,
    }, {"timeouts": rig.unit.transport.timeouts}


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

    _unload(rig, lane)
    dog.stop()
    sim.close()
    return {
        "no_failures": rig.failures == [],
        "reached_toolhead": loaded_sensor,
        "stage_order": is_subsequence([0, 4, 5, 6, 7], stages),
        "marked_slot_print": bytes([0x01, P.MODE_PRINT]) in modes,
        "cleared_latched_error": sim.latched_error == P.ST_OK,
        "primed_then_tool_stn": [m[0] for m in moves_after_load] == [10.0, 5.0, 35.0],
        "cut_before_unload": len(rig.cut_calls()) == 1,
        "toolhead_clear_after_unload": not sim.toolhead_sensor,
        "box_reeled_back": sim.position == 0.0,
        "connection_motor_stopped": sim.conn_action == P.CONN_STOP,
        "reactor_never_stalled": dog.max_gap < 0.05,
    }, {"max_gap_ms": round(dog.max_gap * 1000, 2), "stages": stages,
        "position_after_load": after_load_position, "failures": rig.failures,
        "report": lane.last_load_report}


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
        "reactor_never_stalled": dog.max_gap < 0.05,
    }, {"failures": rig.failures, "unused_path": path}


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
