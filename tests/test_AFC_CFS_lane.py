"""Unit tests for AFC_CFS_lane decision logic (runout routing, refusals).
Box I/O is mocked here; tests/test_AFC_CFS_reactor.py covers it for real."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from extras import AFC_CFS_protocol as P
from extras.AFC_CFS import afcCFS
from extras.AFC_CFS_lane import AFCCFSLane
from extras.AFC_lane import AFCLaneState
from tests.conftest import MockAFC, MockLogger, MockReactor


class AdvancingReactor(MockReactor):
    """MockReactor whose pause() moves the clock, so timeout loops end."""

    def pause(self, until):
        self._monotonic = max(self._monotonic, until)
        return until


def _make_lane(index=1, printing=False, tool_loaded=False, runout_lane=None):
    lane = AFCCFSLane.__new__(AFCCFSLane)
    afc = MockAFC()
    afc.tool_cut = True
    afc.tool_cut_cmd = "AFC_CUT"
    afc.park = False
    afc.form_tip = False
    afc._check_extruder_temp = MagicMock(return_value=False)
    afc.error.handle_lane_failure = MagicMock()
    afc.gcode.run_script_from_command = MagicMock()
    afc.save_vars = MagicMock()
    afc.function.is_printing = MagicMock(return_value=printing)

    unit = MagicMock()
    unit.online = True
    unit.cutter_sensor_pin = None
    unit.cutter_sensor_state = False
    unit.load_attempts = 2
    unit.cutter_wait_time = 0.01
    unit.cutter_trigger_count = 0
    unit.cutter_require_trigger = True
    unit.read_material_mask = MagicMock(return_value=0b1111)

    lane.name = "lane{}".format(index)
    lane.index = index
    lane.afc = afc
    lane.unit_obj = unit
    lane.reactor = AdvancingReactor()
    lane.logger = MockLogger()
    lane.extruder_obj = MagicMock()
    lane.extruder_obj.name = "extruder"
    lane.extruder_obj.tool_start = "pin"
    lane.extruder_obj.tool_start_state = False
    lane.select_lane = MagicMock()
    lane.disable_buffer = MagicMock()
    lane.set_loaded = MagicMock()
    lane.set_unloaded = MagicMock()
    lane._perform_pause_runout = MagicMock()
    lane._perform_infinite_runout = MagicMock()
    lane._afc_prep_done = True
    lane._cfs_present_last = True
    lane.status = AFCLaneState.LOADED
    lane.tool_loaded = tool_loaded
    lane.runout_lane = runout_lane
    lane.last_load_report = {}
    return lane


class TestPresence:
    def test_first_report_only_mirrors_state(self):
        lane = _make_lane()
        lane._cfs_present_last = None
        lane.cfs_update_presence(True)
        assert lane.prep_state is True and lane.loaded_to_hub is True
        lane.set_loaded.assert_not_called()

    def test_unchanged_state_is_ignored(self):
        lane = _make_lane()
        lane.cfs_update_presence(True)
        lane.afc.save_vars.assert_not_called()

    def test_insert_marks_lane_loaded(self):
        lane = _make_lane()
        lane._cfs_present_last = False
        lane.status = AFCLaneState.NONE
        lane.cfs_update_presence(True)
        lane.set_loaded.assert_called_once()
        lane.unit_obj.on_filament_insert.assert_called_once_with(lane)

    def test_removal_when_idle_unloads_lane(self):
        lane = _make_lane()
        lane.cfs_update_presence(False)
        lane.set_unloaded.assert_called_once()
        lane._perform_pause_runout.assert_not_called()

    def test_runout_while_printing_pauses(self):
        lane = _make_lane(printing=True, tool_loaded=True)
        lane.cfs_update_presence(False)
        lane._perform_pause_runout.assert_called_once()
        lane.set_unloaded.assert_not_called()

    def test_runout_with_runout_lane_uses_infinite_spool(self):
        lane = _make_lane(printing=True, tool_loaded=True, runout_lane="lane2")
        lane.cfs_update_presence(False)
        lane._perform_infinite_runout.assert_called_once()
        lane._perform_pause_runout.assert_not_called()

    def test_runout_ignored_when_sensor_disabled(self):
        lane = _make_lane(printing=True, tool_loaded=True)
        lane.fila_load = MagicMock()
        lane.fila_load.runout_helper.sensor_enabled = False
        lane.cfs_update_presence(False)
        lane._perform_pause_runout.assert_not_called()
        lane._perform_infinite_runout.assert_not_called()


class TestLoadRefusals:
    def test_refuses_when_toolhead_already_loaded(self):
        lane = _make_lane()
        lane.extruder_obj.tool_start_state = True
        lane.cmd_AFC_CFS_TOOL_LOAD(None)
        msg = lane.afc.error.handle_lane_failure.call_args.args[1]
        assert "already sees filament" in msg
        lane.unit_obj.extrude_stage.assert_not_called()

    def test_refuses_when_cutter_engaged(self):
        lane = _make_lane()
        lane.unit_obj.cutter_sensor_pin = "PA1"
        lane.unit_obj.cutter_sensor_state = True
        lane.cmd_AFC_CFS_TOOL_LOAD(None)
        assert "cutter" in lane.afc.error.handle_lane_failure.call_args.args[1]

    def test_refuses_empty_slot(self):
        lane = _make_lane(index=3)
        lane.unit_obj.read_material_mask = MagicMock(return_value=0b0011)
        lane.cmd_AFC_CFS_TOOL_LOAD(None)
        assert "no filament in slot C" in lane.afc.error.handle_lane_failure.call_args.args[1]

    def test_refuses_when_box_offline(self):
        lane = _make_lane()
        lane.unit_obj.online = False
        lane.unit_obj.connect = MagicMock(return_value=False)
        lane.unit_obj.transport.last_error = "open failed"
        lane.cmd_AFC_CFS_TOOL_LOAD(None)
        assert "offline" in lane.afc.error.handle_lane_failure.call_args.args[1]


class TestUnloadCutter:
    def test_fails_when_cutter_never_releases(self):
        lane = _make_lane(tool_loaded=True)
        lane.unit_obj.cutter_sensor_pin = "PA1"
        calls = {"n": 0}

        def cut(script):
            calls["n"] += 1
            lane.unit_obj.cutter_sensor_state = True   # blade stays down

        lane.afc.gcode.run_script_from_command = MagicMock(side_effect=cut)
        lane.cmd_AFC_CFS_TOOL_UNLOAD(None)
        assert "did not release" in lane.afc.error.handle_lane_failure.call_args.args[1]
        lane.unit_obj.retrude_stage.assert_not_called()
        lane.unit_obj.reel_slot.assert_not_called()

    def test_no_cutter_switch_trusts_macro(self):
        lane = _make_lane(tool_loaded=True)
        assert lane._run_cutter_sequence() is None
        lane.afc.gcode.run_script_from_command.assert_called_once_with("AFC_CUT EXTRUDER=extruder")

    def _switch_lane(self, fires=True, require=True):
        lane = _make_lane(tool_loaded=True)
        unit = lane.unit_obj
        unit.cutter_sensor_pin = "nozzle_mcu:PA8"
        unit.cutter_require_trigger = require
        cuts = {"n": 0}

        def cut(script):
            cuts["n"] += 1
            if fires:       # blade reached end of travel and came back
                unit.cutter_trigger_count += 1
            unit.cutter_sensor_state = False

        lane.afc.gcode.run_script_from_command = MagicMock(side_effect=cut)
        return lane, cuts

    def test_switch_fired_and_released_is_a_good_cut(self):
        lane, cuts = self._switch_lane(fires=True)
        assert lane._run_cutter_sequence() is None
        assert cuts["n"] == 1
        lane.afc.toolhead.wait_moves.assert_called()

    def test_switch_never_fired_retries_then_fails(self):
        lane, cuts = self._switch_lane(fires=False)
        reason = lane._run_cutter_sequence()
        assert "never fired" in reason
        assert cuts["n"] == lane.unit_obj.load_attempts + 1

    def test_unload_stops_before_retract_when_cut_unconfirmed(self):
        lane, _ = self._switch_lane(fires=False)
        lane.cmd_AFC_CFS_TOOL_UNLOAD(None)
        assert "never fired" in lane.afc.error.handle_lane_failure.call_args.args[1]
        lane.unit_obj.retrude_stage.assert_not_called()
        lane.unit_obj.reel_slot.assert_not_called()

    def test_trigger_not_required_accepts_release_only(self):
        lane, cuts = self._switch_lane(fires=False, require=False)
        assert lane._run_cutter_sequence() is None
        assert cuts["n"] == 1


class TestUnitConfig:
    def test_parse_retract_steps(self):
        assert afcCFS._parse_steps("15:90, 15:500,") == [(15.0, 90.0), (15.0, 500.0)]
        assert afcCFS._parse_steps("-15:90") == [(15.0, 90.0)]

    def test_parse_retract_steps_rejects_garbage(self):
        with pytest.raises(Exception):
            afcCFS._parse_steps("15-90")


def test_slot_properties():
    lane = _make_lane(index=4)
    assert lane.slot_bits == P.SLOT_BITS[3]
    assert lane.slot_name == "D"


class TestSystemTest:
    """PREP-time lane restore. Must work on AFCProject main (lane.map is a
    string, no map_to_string) and on newer AFC (map list + map_to_string)."""

    def _unit(self, mask=0b0001, online=True):
        unit = afcCFS.__new__(afcCFS)
        unit.name = "CFS_1"
        unit.logger = MagicMock()
        unit.afc = MockAFC()
        unit.afc.current = None
        unit.online = online
        unit.material_mask = mask
        unit.connect = MagicMock(return_value=online)
        unit.read_material_mask = MagicMock(return_value=mask)
        unit.connected_mask = 0
        unit.read_connected_mask = MagicMock(return_value=0)
        unit.lane_loaded = MagicMock()
        unit.lane_not_ready = MagicMock()
        unit.lane_tool_loaded = MagicMock()
        unit.lane_tool_loaded_idle = MagicMock()
        unit.boot_wait = 0.0
        unit._boot_wait_done = False
        unit.reactor = AdvancingReactor()
        return unit

    def _lane(self, index=1, with_map_to_string=True):
        lane = MagicMock()
        lane.name = "lane{}".format(index)
        lane.index = index
        lane.tool_loaded = False
        lane.map = "T0" if not with_map_to_string else ["T0"]
        if with_map_to_string:
            lane.map_to_string = MagicMock(return_value="T0")
        else:
            del lane.map_to_string
        return lane

    @pytest.mark.parametrize("with_map_to_string", [True, False])
    def test_present_slot_marks_loaded(self, with_map_to_string):
        unit = self._unit(mask=0b0001)
        lane = self._lane(1, with_map_to_string)
        assert unit.system_Test(lane, 0, True, False) is True
        assert lane.prep_state is True and lane.loaded_to_hub is True
        # Present but not in the shared path: AFC's virtual hub must read clear
        assert lane._load_state is False
        unit.lane_loaded.assert_called_once_with(lane)
        lane.set_afc_prep_done.assert_called_once()
        assert "lane1 tool cmd: T0  FILAMENT PRESENT" in unit.logger.info.call_args.args[0]

    def test_connected_slot_sets_load_sensor(self):
        unit = self._unit(mask=0b0011)
        unit.connected_mask = 0b0010
        lane1, lane2 = self._lane(1), self._lane(2)
        unit.system_Test(lane1, 0, False, False)
        unit.system_Test(lane2, 0, False, False)
        assert lane1._load_state is False and lane2._load_state is True
        assert lane1.loaded_to_hub is True and lane2.loaded_to_hub is True

    def test_empty_slot_marks_not_ready(self):
        unit = self._unit(mask=0b0001)
        lane = self._lane(2, with_map_to_string=False)
        unit.system_Test(lane, 0, False, False)
        assert lane.prep_state is False
        unit.lane_not_ready.assert_called_once_with(lane)

    def test_offline_box_reports_not_responding(self):
        unit = self._unit(online=False)
        unit.material_mask = None
        lane = self._lane(1)
        assert unit.system_Test(lane, 0, False, False) is False
        assert "NOT RESPONDING" in unit.logger.info.call_args.args[0]
        lane.set_afc_prep_done.assert_called_once()
        # Unknown rather than empty, so the first poll doesn't see an insert
        assert lane._cfs_present_last is None

    def test_waits_for_booting_box_once(self):
        unit = self._unit(online=False)
        unit.material_mask = None
        unit.boot_wait = 10.0
        attempts = {"n": 0}

        def connect():
            attempts["n"] += 1
            if attempts["n"] >= 4:          # answers on the 4th try
                unit.online = True
                unit.material_mask = 0b0001
            return unit.online

        unit.connect = MagicMock(side_effect=connect)
        assert unit.system_Test(self._lane(1), 0, False, False) is True
        assert attempts["n"] == 4
        unit.lane_loaded.assert_called_once()

    def test_boot_wait_only_for_first_lane(self):
        unit = self._unit(online=False)
        unit.material_mask = None
        unit.boot_wait = 3.0
        unit.system_Test(self._lane(1), 0, False, False)
        first = unit.connect.call_count
        unit.system_Test(self._lane(2), 0, False, False)
        assert unit.connect.call_count == first + 1


class TestFirstReadingAfterOfflinePrep:
    def test_first_reading_is_not_an_insert(self):
        lane = _make_lane(tool_loaded=True)
        lane._cfs_present_last = None
        lane.unit_obj.apply_lane_state = MagicMock(return_value="FILAMENT PRESENT in ToolHead")
        lane.cfs_update_presence(True)
        lane.unit_obj.apply_lane_state.assert_called_once_with(lane, True)
        lane.unit_obj.on_filament_insert.assert_not_called()
        lane.set_loaded.assert_not_called()
        assert lane._cfs_present_last is True

    def test_first_reading_empty_is_not_a_runout(self):
        lane = _make_lane(printing=True, tool_loaded=False)
        lane._cfs_present_last = None
        lane.unit_obj.apply_lane_state = MagicMock(return_value="EMPTY READY FOR SPOOL")
        lane.cfs_update_presence(False)
        lane.unit_obj.on_filament_remove.assert_not_called()
        lane.set_unloaded.assert_called_once()


class TestVirtualHubGate:
    """AFC's TOOL_LOAD only proceeds when `lane.load_state and not hub.state`.
    With switch_pin: virtual, hub.state is any(lane.raw_load_state) and
    lane.load_state is lane.loaded_to_hub. Check that with AFC's real code."""

    def _hub(self, lanes):
        from extras.AFC_hub import afc_hub
        hub = afc_hub.__new__(afc_hub)
        hub.switch_pin = "virtual"
        hub._state = False
        hub.lanes = {l.name: l for l in lanes}
        return hub

    def _real_lane(self, index, hub):
        from extras.AFC_CFS import set_lane_sensors
        lane = AFCCFSLane.__new__(AFCCFSLane)
        lane.name = "lane{}".format(index)
        lane.index = index
        lane.hub_obj = hub
        return lane, set_lane_sensors

    def test_all_spools_present_hub_clear_and_lane_ready(self):
        hub = self._hub([])
        lanes = []
        for i in (1, 2, 3, 4):
            lane, set_sensors = self._real_lane(i, hub)
            set_sensors(lane, True, 0b0000)
            lanes.append(lane)
        hub.lanes = {l.name: l for l in lanes}
        assert hub.state is False
        assert all(l.load_state for l in lanes)

    def test_connected_slot_occupies_hub(self):
        hub = self._hub([])
        lanes = []
        for i in (1, 2):
            lane, set_sensors = self._real_lane(i, hub)
            set_sensors(lane, True, 0b0001)
            lanes.append(lane)
        hub.lanes = {l.name: l for l in lanes}
        assert hub.state is True

    def test_empty_slot_not_ready(self):
        hub = self._hub([])
        lane, set_sensors = self._real_lane(1, hub)
        set_sensors(lane, False, 0)
        hub.lanes = {lane.name: lane}
        assert lane.load_state is False
