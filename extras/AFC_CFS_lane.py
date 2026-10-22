# AFCProject Automated Filament Changer Software
#
# Lane for a Creality CFS slot. The box owns the slot motor and sensor, so this
# lane has no prep/load pins: presence comes from the unit's RS-485 poll, and
# tool load/unload run as custom commands that sequence the box and the
# toolhead extruder together.
#
# Load follows the stock sequence that tomaspakosta/k1c-cfs-klipper validated
# on hardware: connection motor -> tighten -> EXTRUDE_PROCESS stages 0, 4, 5
# (until the toolhead sensor trips) -> prime -> stage 6 -> prime -> stage 7 ->
# mark slot PRINT. Unload cuts, runs RETRUDE_PROCESS, then retracts the
# extruder in chunks with a box check-in before each chunk.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import traceback
from typing import Any, Optional

import configfile

CONFIG_ERROR = getattr(configfile, "error", Exception)

try:
    from extras.AFC_utils import ERROR_STR
except Exception:
    raise CONFIG_ERROR(
        "Error when trying to import AFC_utils.ERROR_STR\n{}".format(traceback.format_exc())
    )

try:
    from extras.AFC_lane import AFCLane, AFCLaneState, AFCMoveWarning
except Exception:
    raise CONFIG_ERROR(ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc()))

try:
    from extras import AFC_CFS_protocol as P
except Exception:
    raise CONFIG_ERROR(ERROR_STR.format(import_lib="AFC_CFS_protocol", trace=traceback.format_exc()))


# Stock tip-forming wiggle, (distance_mm, speed_mm_per_min). Nets about -0.5mm.
TIP_WIGGLE_STEPS = [(0.5, 600), (-5, 600), (2.5, 600), (-1.25, 600), (1.75, 600), (1, 60)]


class AFCCFSLane(AFCLane):
    cmd_AFC_CFS_TOOL_LOAD_help = "Load a CFS lane into the toolhead"
    cmd_AFC_CFS_TOOL_UNLOAD_help = "Unload a CFS lane from the toolhead back to the box"
    cmd_AFC_CFS_EJECT_help = "Reel a CFS lane's filament back into the box (box motors only)"

    def __init__(self, config):
        super().__init__(config)
        if self.prep is not None or self.load is not None:
            raise CONFIG_ERROR(
                "[{}]: CFS lanes take filament presence from the box; remove prep/load pins".format(
                    config.get_name()))
        if self.index < 1 or self.index > 4:
            raise CONFIG_ERROR(
                "[{}]: unit must be '<cfs_unit>:<1-4>' (slot A-D), got index {}".format(
                    config.get_name(), self.index))

        self._load_state = False
        self._cfs_present_last: Optional[bool] = None
        self.last_load_report: dict = {}

        if self.custom_load_cmd is None:
            self.custom_load_cmd = "AFC_CFS_TOOL_LOAD LANE={}".format(self.name)
        if self.custom_unload_cmd is None:
            self.custom_unload_cmd = "AFC_CFS_TOOL_UNLOAD LANE={}".format(self.name)

        self.afc.gcode.register_mux_command("AFC_CFS_TOOL_LOAD", "LANE", self.name,
                                            self.cmd_AFC_CFS_TOOL_LOAD,
                                            desc=self.cmd_AFC_CFS_TOOL_LOAD_help)
        self.afc.gcode.register_mux_command("AFC_CFS_TOOL_UNLOAD", "LANE", self.name,
                                            self.cmd_AFC_CFS_TOOL_UNLOAD,
                                            desc=self.cmd_AFC_CFS_TOOL_UNLOAD_help)
        self.afc.gcode.register_mux_command("AFC_CFS_EJECT", "LANE", self.name,
                                            self.cmd_AFC_CFS_EJECT,
                                            desc=self.cmd_AFC_CFS_EJECT_help)
        self._register_frontend_compat_aliases()

    # -- plumbing ------------------------------------------------------------

    def _register_frontend_compat_aliases(self) -> None:
        """Expose this lane as 'AFC_lane <name>' / 'AFC_stepper <name>' so
        Mainsail, Fluidd and GrumpyScreen find it like any other lane."""
        for alias in ("AFC_lane {}".format(self.name), "AFC_stepper {}".format(self.name)):
            if alias == self.fullname:
                continue
            existing = self.printer.lookup_object(alias, None)
            if existing not in (None, self):
                continue
            add_object = getattr(self.printer, "add_object", None)
            if callable(add_object):
                try:
                    add_object(alias, self)
                    continue
                except Exception:
                    pass
            for registry_name in ("_objects", "objects"):
                registry = getattr(self.printer, registry_name, None)
                if isinstance(registry, dict):
                    registry.setdefault(alias, self)
                    break

    @property
    def cfs(self):
        return self.unit_obj

    @property
    def slot_bits(self) -> int:
        return P.slot_bit(self.index)

    @property
    def slot_name(self) -> str:
        return P.slot_letter(self.index)

    def _pause(self, seconds: float) -> None:
        if seconds > 0:
            self.reactor.pause(self.reactor.monotonic() + seconds)

    def _e_move(self, distance: float, speed_mm_min: float, label: str) -> None:
        """Extruder-only move that leaves the user's M82/M83 mode alone."""
        self.afc.move_e_pos(distance, speed_mm_min / 60.0, "CFS {}".format(label), wait_tool=True)

    def _toolhead_sensor(self) -> bool:
        return bool(self.get_toolhead_pre_sensor_state())

    def _cutter_engaged(self) -> bool:
        unit = self.cfs
        return bool(unit.cutter_sensor_pin and unit.cutter_sensor_state)

    def _trace(self, ev: str, **fields: Any) -> None:
        trace = getattr(self.cfs, "trace", None)
        if trace is not None:
            trace.event(ev, lane=self.name, slot=self.slot_name, **fields)

    def _fail(self, message: str) -> None:
        self._trace("fail", message=message, toolhead_sensor=self._toolhead_sensor())
        self.logger.error("CFS {} (slot {}): {}".format(self.name, self.slot_name, message))
        self.afc.error.handle_lane_failure(self, "CFS {}: {}".format(self.name, message))

    # Lanes on a CFS never move filament through AFC's stepper paths
    def move(self, distance, speed, accel, assist_active=False, *args, **kwargs):
        self.logger.debug("CFS {}: direct lane moves are not supported".format(self.name))

    def move_advanced(self, distance, speed_mode, assist_active=None):
        self.logger.debug("CFS {}: direct lane moves are not supported".format(self.name))

    def move_to(self, distance, speed_mode, endstop=None, assist_active=None, use_homing=True):
        return False, 0, AFCMoveWarning.ERROR

    def do_enable(self, enable):
        return

    # -- presence from the box poll -------------------------------------------

    def cfs_update_presence(self, present: bool) -> None:
        """Called by the unit's poll with this slot's material sensor state."""
        if present == self._cfs_present_last:
            return
        previous = self._cfs_present_last
        self._cfs_present_last = present
        self.prep_state = present
        self.loaded_to_hub = present

        if not getattr(self, "_afc_prep_done", False):
            self.afc.save_vars()
            return
        if previous is None:
            # PREP ran while the box was offline: this is the first real
            # reading, not an insert or a runout.
            msg = self.unit_obj.apply_lane_state(self, present)
            if not present and not self.tool_loaded:
                self.set_unloaded()
            self.logger.info("CFS {}: box online, slot {}: {}".format(
                self.name, self.slot_name, msg))
            self.afc.save_vars()
            return

        if present:
            self.logger.info("CFS {}: spool detected in slot {}".format(self.name, self.slot_name))
            if self.status not in (AFCLaneState.LOADED, AFCLaneState.TOOLED,
                                   AFCLaneState.TOOL_LOADED):
                self.set_loaded()
            self.unit_obj.on_filament_insert(self)
        else:
            self.logger.info("CFS {}: slot {} is empty".format(self.name, self.slot_name))
            self.unit_obj.on_filament_remove(self)
            if self.tool_loaded and self.afc.function.is_printing():
                self._trace("runout", runout_lane=self.runout_lane)
                fila = getattr(self, "fila_load", None)
                if fila is not None and not fila.runout_helper.sensor_enabled:
                    self.logger.warning("CFS runout on {} ignored, sensor disabled".format(self.name))
                elif self.runout_lane is not None:
                    # The tail is still in the path; let AFC's infinite spool decide.
                    self._perform_infinite_runout()
                else:
                    self._perform_pause_runout()
            elif not self.tool_loaded:
                self.set_unloaded()
        self.afc.save_vars()

    # -- tool load -------------------------------------------------------------

    def _stage5_feed(self) -> tuple[bool, str]:
        """
        Poll EXTRUDE_PROCESS stage 5 (the box motor runs while we poll) until
        the toolhead sensor trips. Stops early on timeout or when the measuring
        wheel stops moving.
        """
        unit = self.cfs
        start = self.reactor.monotonic()
        last_progress_time = start
        last_wheel: Optional[float] = None
        polls = 0
        while True:
            if self._toolhead_sensor():
                return True, "toolhead sensor triggered after {} polls".format(polls)
            now = self.reactor.monotonic()
            if now - start > unit.stage5_timeout:
                return False, "timed out after {:.0f}s without reaching the toolhead sensor".format(
                    now - start)
            reply = unit.extrude_stage(self.slot_bits, 0x05)
            polls += 1
            if reply is not None and not reply.ok and reply.status != P.ST_UPDATE_STATE:
                self.logger.debug("CFS {}: stage 5 status {}".format(self.name, reply.status_name()))
            wheel = P.decode_float_be(reply.data) if reply is not None else None
            if wheel is not None:
                if last_wheel is None or abs(wheel - last_wheel) >= unit.stage5_stall_distance:
                    last_wheel = wheel
                    last_progress_time = now
                elif now - last_progress_time > unit.stage5_stall_time:
                    if self._toolhead_sensor():
                        continue
                    return False, "measuring wheel stalled at {:.1f}mm".format(wheel)
            self.last_load_report["stage5_polls"] = polls
            self.last_load_report["wheel"] = wheel
            self._pause(unit.stage5_poll_interval)

    def _box_cleanup(self) -> None:
        unit = self.cfs
        unit.tighten_up(False)
        unit.connection_motor(P.CONN_STOP)
        unit.clear_latched_error()

    def _load_attempt(self, attempt: int) -> tuple[bool, str]:
        unit = self.cfs
        bits = self.slot_bits
        settle = unit.stage_settle_time

        unit.pre_loading(P.PRELOAD_CLOSE)
        status = unit.read_box_status()
        if status not in (None, P.ST_OK, P.ST_UPDATE_STATE):
            self.logger.info("CFS {}: box reported {} before load, clearing".format(
                self.name, P.status_name(status)))
        unit.set_box_mode(P.MODE_IDLE)
        self._pause(settle)

        reply = unit.connection_motor(P.CONN_EXTRUDE)
        if reply is None:
            return False, "box did not answer CTRL_CONNECTION_MOTOR_ACTION"
        self._pause(0.5)
        unit.tighten_up(True)
        self._pause(settle)

        for stage in (0x00, 0x04):
            reply = unit.extrude_stage(bits, stage)
            if reply is None:
                self._box_cleanup()
                return False, "no reply to EXTRUDE_PROCESS stage {}".format(stage)
            if not reply.ok:
                self._box_cleanup()
                return False, "EXTRUDE_PROCESS stage {} returned {}".format(stage, reply.status_name())
            self._pause(settle)

        stage5_start = self.reactor.monotonic()
        reached, detail = self._stage5_feed()
        self._trace("stage5_result", reached=reached, detail=detail, attempt=attempt + 1,
                    polls=self.last_load_report.get("stage5_polls"),
                    wheel=self.last_load_report.get("wheel"),
                    seconds=round(self.reactor.monotonic() - stage5_start, 2))
        self.logger.info("CFS {}: stage 5 {}".format(self.name, detail))
        if not reached:
            self._box_cleanup()
            # Pull the partial feed back so the next attempt starts clean
            unit.retrude_stage(bits, 0x00)
            self._pause(0.5)
            unit.retrude_stage(bits, 0x01)
            unit.clear_latched_error()
            return False, detail

        # Handoff: the toolhead extruder grabs the filament while the box
        # finishes its stages.
        if unit.prime1_length > 0:
            self._e_move(unit.prime1_length, unit.prime1_speed, "prime 1")
        reply = unit.extrude_stage(bits, 0x06)
        self.last_load_report["stage6"] = P.status_name(reply.status if reply else None)
        if unit.prime2_length > 0:
            self._e_move(unit.prime2_length, unit.prime2_speed, "prime 2")
        reply = unit.extrude_stage(bits, 0x07, unit.stage7_arg)
        self.last_load_report["stage7"] = P.status_name(reply.status if reply else None)
        if reply is None or not reply.ok:
            self.logger.warning("CFS {}: stage 7 returned {}".format(
                self.name, self.last_load_report["stage7"]))
        self._trace("handoff", attempt=attempt + 1, stage6=self.last_load_report["stage6"],
                    stage7=self.last_load_report["stage7"], stage7_arg=unit.stage7_arg,
                    toolhead_sensor=self._toolhead_sensor())

        # Mark this slot as the active PRINT slot: the box then runs its own
        # buffer feed loop for the print. Nothing may send SET_BOX_MODE
        # [00][01] (feed/change mode) after this until the next unload.
        unit.set_box_mode(P.MODE_PRINT, bits)
        unit.tighten_up(False)
        unit.connection_motor(P.CONN_STOP)
        if unit.clear_mode_after_load:
            unit.clear_latched_error()

        if not self._toolhead_sensor():
            return False, "toolhead sensor cleared after the handoff"
        return True, "loaded"

    def cmd_AFC_CFS_TOOL_LOAD(self, gcmd: Any) -> None:
        """
        Load this CFS lane to the nozzle. AFC calls this through custom_load_cmd
        during a tool change; it can also be run by hand during bring-up.

        Usage
        -----
        `AFC_CFS_TOOL_LOAD LANE=<lane>`
        """
        unit = self.cfs
        started = self.reactor.monotonic()
        self._trace("load_start", toolhead_sensor=self._toolhead_sensor())
        self.select_lane()
        self.afc._check_extruder_temp(self)
        self.last_load_report = {"slot": self.slot_name}

        if not unit.online and not unit.connect():
            self._fail("box is offline ({})".format(unit.transport.last_error or "no reply"))
            return
        if self._cutter_engaged():
            self._fail("cutter sensor is engaged; reset the cutter before loading")
            return
        if self._toolhead_sensor():
            self._fail("toolhead sensor already sees filament; unload the other lane first")
            return
        mask = unit.read_material_mask()
        if mask is not None and not mask & self.slot_bits:
            self._fail("no filament in slot {}".format(self.slot_name))
            return

        if self.afc.park and self.afc.park_cmd:
            self.afc.gcode.run_script_from_command(
                "{} EXTRUDER={}".format(self.afc.park_cmd, self.extruder_obj.name))

        attempts_used = 0
        with unit.operation():
            for attempt in range(unit.load_attempts):
                attempts_used = attempt + 1
                self.logger.info("CFS {}: loading slot {}, try {}/{}".format(
                    self.name, self.slot_name, attempt + 1, unit.load_attempts))
                try:
                    ok, detail = self._load_attempt(attempt)
                except Exception as e:
                    self._box_cleanup()
                    ok, detail = False, "error: {}".format(e)
                    self.logger.debug(traceback.format_exc())
                if ok:
                    break
                self._trace("load_attempt_failed", attempt=attempt + 1, detail=detail,
                            toolhead_sensor=self._toolhead_sensor())
                self.logger.warning("CFS {}: load attempt {} failed: {}".format(
                    self.name, attempt + 1, detail))
                if self._toolhead_sensor():
                    # Filament is stuck in the toolhead; don't keep pushing.
                    break
            else:
                ok = False

        self.last_load_report["result"] = detail
        if not ok:
            self._trace("load_end", ok=False, detail=detail, attempts=attempts_used,
                        seconds=round(self.reactor.monotonic() - started, 2))
            self._fail("load failed after {} attempt(s): {}".format(attempts_used, detail))
            return

        # The handoff already pushed prime1 + prime2 past the sensor.
        remaining = self.extruder_obj.tool_stn - (unit.prime1_length + unit.prime2_length)
        if remaining > 0:
            self.afc.move_e_pos(remaining, self.extruder_obj.tool_load_speed,
                                "CFS tool_stn", wait_tool=True)
        self._load_state = True          # our filament now occupies the shared path
        self._mark_tool_state(loaded=True)
        self._trace("load_end", ok=True, detail=detail, attempts=attempts_used,
                    tool_stn_push=round(max(remaining, 0), 2),
                    seconds=round(self.reactor.monotonic() - started, 2))

    # -- tool unload -----------------------------------------------------------

    def _run_cutter_sequence(self) -> Optional[str]:
        """
        Run AFC's cut macro. With a cutter switch configured, verify it: the
        switch must have fired during the cut (blade reached the end of its
        travel, if cutter_require_trigger) and be released afterwards. Retries
        the cut; returns None on success or the reason it failed.
        """
        if not self.afc.tool_cut:
            return None
        unit = self.cfs
        reason = None
        for attempt in range(max(unit.load_attempts, 1) + 1):
            triggers_before = getattr(unit, "cutter_trigger_count", 0)
            self.extruder_obj.estats.increase_cut_total()
            self.afc.gcode.run_script_from_command(
                "{} EXTRUDER={}".format(self.afc.tool_cut_cmd, self.extruder_obj.name))
            if not unit.cutter_sensor_pin:
                self._trace("cut", attempt=attempt + 1, verified=False)
                return None
            # Let the cut moves finish and the switch report arrive
            toolhead = getattr(self.afc, "toolhead", None)
            if toolhead is not None and hasattr(toolhead, "wait_moves"):
                toolhead.wait_moves()
            start = self.reactor.monotonic()
            self._pause(0.1)
            while self._cutter_engaged():
                if self.reactor.monotonic() - start > unit.cutter_wait_time:
                    break
                self._pause(0.01)
            released = not self._cutter_engaged()
            fired = getattr(unit, "cutter_trigger_count", 0) > triggers_before
            self._trace("cut", attempt=attempt + 1, verified=True, fired=fired,
                        released=released, seconds=round(self.reactor.monotonic() - start, 2))
            if not released:
                reason = "cutter sensor did not release after cutting"
            elif not fired and unit.cutter_require_trigger:
                reason = ("cutter switch never fired during the cut (blade did not reach "
                          "the end of its travel; check pin_loc_xy / cut_move_dist)")
            else:
                return None
            self.logger.warning("CFS {}: cut {} not confirmed: {}".format(
                self.name, attempt + 1, reason))
        return reason

    def _run_post_cut_macros(self) -> None:
        if self.afc.park and self.afc.park_cmd:
            self.afc.gcode.run_script_from_command(
                "{} EXTRUDER={}".format(self.afc.park_cmd, self.extruder_obj.name))
        if self.afc.form_tip:
            if self.afc.form_tip_cmd == "AFC":
                self.printer.lookup_object("AFC_form_tip").tip_form()
            else:
                self.afc.gcode.run_script_from_command(self.afc.form_tip_cmd)

    def _retract_to_box(self) -> tuple[bool, str]:
        """
        After the cut: pull the filament out of the extruder gears first, then
        let the box reel the slot back. Asking the box to reel while the gears
        still hold the filament just stalls its motor.
        """
        unit = self.cfs
        # Keep the box from feeding while the extruder pulls back.
        unit.pre_loading(P.PRELOAD_CLOSE)
        unit.set_box_mode(P.MODE_IDLE)

        if unit.unload_tip_wiggle:
            for dist, speed in TIP_WIGGLE_STEPS:
                self._e_move(dist, speed, "tip wiggle")

        for i, (dist, speed) in enumerate(unit.unload_retract_steps):
            self._e_move(-dist, speed, "unload retract {}".format(i + 1))
            self._trace("retract_step", step=i + 1, mm=dist, speed=speed,
                        toolhead_sensor=self._toolhead_sensor())

        reel_started = self.reactor.monotonic()
        done, replies = unit.reel_slot(self.slot_bits)
        reel_seconds = round(self.reactor.monotonic() - reel_started, 2)
        self._trace("reel", done=done, replies=replies, seconds=reel_seconds,
                    toolhead_sensor=self._toolhead_sensor())

        # The box answers stage 1 when it's done, but give the sensor a moment.
        deadline = self.reactor.monotonic() + unit.unload_timeout
        while self._toolhead_sensor():
            if self.reactor.monotonic() > deadline:
                return False, ("toolhead sensor still triggered {:.0f}s after reel-back "
                               "(box stages {})".format(unit.unload_timeout, "/".join(replies)))
            self._pause(0.5)
        if not done:
            return False, "box did not confirm reel-back (stages {})".format("/".join(replies))
        return True, "reeled back in {:.1f}s".format(reel_seconds)

    def cmd_AFC_CFS_TOOL_UNLOAD(self, gcmd: Any) -> None:
        """
        Cut and unload this CFS lane back to the box. AFC calls this through
        custom_unload_cmd during a tool change.

        Usage
        -----
        `AFC_CFS_TOOL_UNLOAD LANE=<lane>`
        """
        unit = self.cfs
        started = self.reactor.monotonic()
        self._trace("unload_start", toolhead_sensor=self._toolhead_sensor())
        self.select_lane()
        self.afc._check_extruder_temp(self)
        self.disable_buffer()

        if not unit.online and not unit.connect():
            self._fail("box is offline ({})".format(unit.transport.last_error or "no reply"))
            return
        if self._cutter_engaged():
            self._fail("cutter sensor is engaged before cutting; check the cutter lever")
            return
        cut_problem = self._run_cutter_sequence()
        if cut_problem:
            self._fail(cut_problem)
            return
        self._run_post_cut_macros()

        with unit.operation():
            try:
                ok, detail = self._retract_to_box()
            except Exception as e:
                unit.clear_latched_error()
                ok, detail = False, "error: {}".format(e)
                self.logger.debug(traceback.format_exc())

        self.logger.info("CFS {}: unload {}".format(self.name, detail))
        self._trace("unload_end", ok=ok, detail=detail,
                    seconds=round(self.reactor.monotonic() - started, 2))
        if not ok:
            self._fail("unload failed: {}".format(detail))
            return
        self._load_state = False         # reeled back past the merge
        self._mark_tool_state(loaded=False)

    def _mark_tool_state(self, loaded: bool) -> None:
        """
        Record the tool state in AFC. AFC's own TOOL_LOAD/TOOL_UNLOAD flow
        does this after our custom command returns, but running
        AFC_CFS_TOOL_LOAD/UNLOAD directly would otherwise leave AFC thinking
        nothing (or the old lane) is in the toolhead. Setting it twice is
        harmless.
        """
        try:
            if loaded:
                self.set_tool_loaded(normal_toolchange=True)
            else:
                self.set_tool_unloaded(normal_toolchange=True)
            self.afc.save_vars()
        except Exception as e:
            self.logger.warning("CFS {}: could not record tool state: {}".format(
                self.name, e))

    def cmd_AFC_CFS_EJECT(self, gcmd: Any) -> None:
        """
        Reel this lane's filament back into the box using only the box
        motors. For filament that stopped short of the extruder gears (an
        interrupted load); filament in the toolhead needs AFC_CFS_TOOL_UNLOAD.

        Usage
        -----
        `AFC_CFS_EJECT LANE=<lane>`
        """
        unit = self.cfs
        if not unit.online and not unit.connect():
            raise gcmd.error("CFS {}: box is offline".format(self.name))
        ok, detail = unit.reel_back(self)
        gcmd.respond_info("CFS {} (slot {}) eject: {}".format(self.name, self.slot_name, detail))
        if not ok:
            raise gcmd.error("CFS {}: eject did not finish cleanly: {}".format(self.name, detail))

    def get_status(self, eventtime=None, save_to_file=False):
        response = super().get_status(eventtime=eventtime, save_to_file=save_to_file)
        if not response or save_to_file:
            return response
        response["cfs_slot"] = self.slot_name
        response["cfs_present"] = self._cfs_present_last
        response["cfs_last_load"] = dict(self.last_load_report)
        return response


def load_config_prefix(config):
    return AFCCFSLane(config)
