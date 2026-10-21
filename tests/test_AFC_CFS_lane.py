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
        assert lane.prep_state is True and lane._load_state is True
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

    def test_no_cutter_switch_trusts_macro(self):
        lane = _make_lane(tool_loaded=True)
        assert lane._run_cutter_sequence() is True
        lane.afc.gcode.run_script_from_command.assert_called_once_with("AFC_CUT EXTRUDER=extruder")


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
