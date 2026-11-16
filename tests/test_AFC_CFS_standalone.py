"""Tests that only run in the afc-creality-cfs repo, not in AFC's.

- cfs_trace_summary.py, the trace summarizer in tools/.
- The config templates' macros (templates/AFC_CFS_*.cfg).
- The CFS unit on Klipper's real reactor against a simulated box
  (tests/cfs_reactor_harness.py and tests/cfs_sim.py), skipped unless
  KLIPPER_PATH points at a Klipper (or Kalico) checkout.
"""
from __future__ import annotations

import ast
import configparser
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys

import jinja2
import pytest


# ============================================================================
# cfs_trace_summary.py (tools/)
# ============================================================================

_ROOT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..")
TOOL = os.path.join(_ROOT, "tools", "cfs_trace_summary.py")
TOOL = TOOL if os.path.isfile(TOOL) else None
summary = None
if TOOL is not None:
    _spec = importlib.util.spec_from_file_location("cfs_trace_summary", TOOL)
    summary = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(summary)
needs_summary = pytest.mark.skipif(summary is None, reason="cfs_trace_summary.py not found")


@needs_summary
class TestTraceSummarySummarize:
    def test_summary_reports_failures_and_link_problems(self):
        t = 1_760_000_000.0
        events = [
            {"ts": t, "ev": "trace_start", "settings": {"stage7_arg": 3, "load_attempts": 2,
                                                        "stage5_timeout": 60, "stage5_stall_time": 8}},
            {"ts": t + 1, "ev": "online", "version": "113100008730633225CMPN"},
            {"ts": t + 2, "ev": "load_start", "lane": "lane2", "slot": "B"},
            {"ts": t + 3, "ev": "tx", "fn": "EXTRUDE_PROCESS"},
            {"ts": t + 3.2, "ev": "rx", "fn": "EXTRUDE_PROCESS", "st": "EXTRUDE_ERR8", "ms": 40.0},
            {"ts": t + 4, "ev": "timeout", "fn": "EXTRUDE_PROCESS", "attempt": 1},
            {"ts": t + 9, "ev": "stage5_result", "lane": "lane2", "slot": "B", "reached": False,
             "attempt": 1, "detail": "measuring wheel stalled at 310.0mm", "seconds": 7.0},
            {"ts": t + 20, "ev": "load_end", "lane": "lane2", "slot": "B", "ok": False,
             "attempts": 2, "seconds": 18.0},
            {"ts": t + 20, "ev": "fail", "lane": "lane2", "message": "load failed after 2 attempt(s)"},
            {"ts": t + 21, "ev": "mark", "text": "swapped spool"},
        ]
        buf = io.StringIO()
        summary.summarize(events, out=buf)
        text = buf.getvalue()
        assert "Box firmware: 113100008730633225CMPN" in text
        assert "Loads: 0 ok, 1 failed" in text
        assert "measuring wheel stalled" in text
        assert "EXTRUDE_ERR8" in text
        assert "timeouts by command: EXTRUDE_PROCESS x1" in text
        assert "swapped spool" in text

    def test_summary_shows_box_state_during_the_handoff(self):
        t = 1_760_000_000.0
        events = [
            {"ts": t, "ev": "load_start", "lane": "lane1", "slot": "A"},
            {"ts": t + 1, "ev": "box_state", "lane": "lane1", "point": "prime1", "status": "OK"},
            {"ts": t + 2, "ev": "load_start", "lane": "lane1", "slot": "A"},
            {"ts": t + 10, "ev": "box_state", "lane": "lane1", "point": "toolhead", "status": "OK"},
            {"ts": t + 27.5, "ev": "box_state", "lane": "lane1", "point": "prime1",
             "status": "EXTRUDE_ERR8"},
            {"ts": t + 40, "ev": "box_state", "lane": "lane1", "point": "toolhead", "status": "OK"},
        ]
        buf = io.StringIO()
        summary.summarize(events, out=buf)
        lines = [ln.split(": ", 1)[1] for ln in buf.getvalue().splitlines()
                 if ln.startswith("    ") and "lane1:" in ln]
        # A new attempt starts at load_start or at each "toolhead" read
        assert lines == ["prime1 OK (+0.0s)",
                         "toolhead OK (+0.0s), prime1 EXTRUDE_ERR8 (+17.5s)",
                         "toolhead OK (+0.0s)"]


@needs_summary
class TestTraceSummaryMain:
    def test_summary_cli_filters_events(self, tmp_path, capsys):
        path = tmp_path / "t.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in [
            {"ts": 1.0, "ev": "tx", "fn": "A"},
            {"ts": 2.0, "ev": "fail", "message": "boom"},
        ]) + "\n{truncated", encoding="utf-8")
        assert summary.main([str(path), "--events", "fail"]) == 0
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1 and json.loads(out[0])["message"] == "boom"


# ============================================================================
# Klipper's real reactor (tests/cfs_reactor_harness.py)
#
# Runs the CFS unit and lanes against a simulated CFS box on Klipper's real
# reactor. Each scenario runs in its own subprocess because conftest mocks
# chelper and the real reactor needs it. Skipped unless KLIPPER_PATH points at
# a Klipper (or Kalico) checkout, e.g. KLIPPER_PATH=~/klipper ./run-tests.sh --unit
# ============================================================================

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "cfs_reactor_harness.py")
# KLIPPER_PATH, or a Klipper clone at the repository root (where AFC's klippy
# integration tests expect it)
KLIPPER_PATH = os.environ.get("KLIPPER_PATH", "") or os.path.join(HERE, "..", "klipper")

SCENARIOS = [
    "connect_addressed",
    "connect_discovery",
    "retry_and_offline",
    "garbage_resync",
    "poll_presence",
    "load_unload",
    "slot_switch",
    "stall_fails_cleanly",
    "empty_slot",
    "offline",
    "trace_recording",
    "slow_stage0",
    "late_reply_resync",
    "eject_part_fed",
    "unload_still_gripped",
    "box_boots_late",
    "offline_prep_then_online",
    "two_boxes_by_uid",
    "two_boxes_load_while_polling",
    "stage5_reply_lost",
    "rfid_insert_reads_tag",
    "rfid_no_tag_box_busy",
    "rfid_box_silent_longer",
    "rfid_spoolman_id",
    "rfid_load_waits_for_tag_read",
    "rfid_read_at_startup",
    "rfid_startup_off_by_default",
    "rfid_not_during_print",
    "wrong_box_at_address",
    "single_box_without_uid",
]


@pytest.mark.skipif(not os.path.isfile(os.path.join(KLIPPER_PATH, "klippy", "reactor.py")),
                    reason="no Klipper checkout (set KLIPPER_PATH or clone Klipper to ./klipper)")
class TestAfcCFSOnRealReactor:
    @pytest.mark.parametrize("scenario", SCENARIOS)
    def test_scenario(self, scenario):
        env = dict(os.environ)
        env["KLIPPER_PATH"] = KLIPPER_PATH
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
        proc = subprocess.run([sys.executable, HARNESS, scenario], capture_output=True,
                              text=True, timeout=120, env=env)
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        assert "exception" not in result, result["exception"]
        failed = {k: v for k, v in result["checks"].items() if not v}
        assert not failed, "failed checks {} (info: {})".format(list(failed), result["info"])


# ============================================================================
# Config template macros (templates/, incl. the K1 park/leave-purge macros)
# ============================================================================

TEMPLATES = sorted((pathlib.Path(__file__).resolve().parent.parent / "templates")
                   .glob("AFC_CFS_*.cfg"))


def _macros():
    for path in TEMPLATES:
        cp = configparser.RawConfigParser(strict=False, inline_comment_prefixes=None,
                                          comment_prefixes=("#", ";"))
        cp.read(path)
        for section in cp.sections():
            if section.startswith("gcode_macro ") and cp.has_option(section, "gcode"):
                yield pytest.param(cp.get(section, "gcode"), id="{}:{}".format(path.name, section))


class TestAfcCFSTemplates:
    @pytest.mark.parametrize("gcode", list(_macros()))
    def test_macro_compiles(self, gcode):
        # Klipper's environment: "{" "}" for expressions
        env = jinja2.Environment("{%", "%}", "{", "}")
        env.parse(gcode)

    def test_found_templates(self):
        ids = [p.id for p in _macros()]
        assert "AFC_CFS_K1.cfg:gcode_macro AFC_CFS_PARK" in ids
        assert "AFC_CFS_K1.cfg:gcode_macro AFC_CFS_LEAVE_PURGE" in ids


def _render_leave_purge(y=230.0, homed="xyz", next_lane=None, current_load=None):
    """Render AFC_CFS_LEAVE_PURGE from AFC_CFS_K1.cfg as Klipper would and return its lines."""
    path = next(p for p in TEMPLATES if p.name == "AFC_CFS_K1.cfg")
    cp = configparser.RawConfigParser(strict=False, inline_comment_prefixes=None,
                                      comment_prefixes=("#", ";"))
    cp.read(path)
    section = "gcode_macro AFC_CFS_LEAVE_PURGE"
    env = jinja2.Environment("{%", "%}", "{", "}")
    template = env.from_string(cp.get(section, "gcode"))
    printer = {
        "gcode_macro AFC_CFS_PARK": {"safe_y": 215.0, "travel_speed": 250},
        "AFC": {"next_lane": next_lane, "current_load": current_load},
        "toolhead": {"homed_axes": homed, "position": {"y": y}},
    }
    text = template.render(printer=printer, params={}, settle_ms=1000)
    return [line.strip() for line in text.splitlines() if line.strip()]


def _render_park(x=50.0, y=230.0, z=0.2):
    """Render AFC_CFS_PARK from AFC_CFS_K1.cfg with its default variables and return its lines."""
    path = next(p for p in TEMPLATES if p.name == "AFC_CFS_K1.cfg")
    cp = configparser.RawConfigParser(strict=False, inline_comment_prefixes=None,
                                      comment_prefixes=("#", ";"))
    cp.read(path)
    section = "gcode_macro AFC_CFS_PARK"
    # Klipper parses macro variables with ast.literal_eval
    variables = {k[len("variable_"):]: ast.literal_eval(v.split("#")[0].strip())
                 for k, v in cp.items(section) if k.startswith("variable_")}
    env = jinja2.Environment("{%", "%}", "{", "}")
    template = env.from_string(cp.get(section, "gcode"))
    printer = {"gcode_move": {"gcode_position": {"x": x, "y": y, "z": z}},
               "toolhead": {"axis_maximum": {"z": 300.0}}}
    text = template.render(printer=printer, params={}, **variables)
    return [line.strip() for line in text.splitlines() if line.strip()]


class TestAfcCFSParkMacro:
    def test_parks_from_behind_safe_y(self):
        assert _render_park() == ["G90", "AFC_DISABLE_SKEW", "G1 Z30.0 F900",
                                  "G1 Y215.0 F15000", "G1 X148.0 F15000",
                                  "G1 Y225.3 F15000", "M400", "AFC_ENABLE_SKEW"]

    def test_already_high_and_in_front(self):
        assert _render_park(y=200.0, z=50.0) == ["G90", "AFC_DISABLE_SKEW", "G1 X148.0 F15000",
                                                 "G1 Y225.3 F15000", "M400", "AFC_ENABLE_SKEW"]

    def test_already_lined_up_in_x(self):
        assert _render_park(x=148.0, z=50.0) == ["G90", "AFC_DISABLE_SKEW", "G1 Y225.3 F15000",
                                                 "M400", "AFC_ENABLE_SKEW"]


class TestAfcCFSLeavePurgeMacro:
    LEAVES = ["SAVE_GCODE_STATE NAME=_afc_cfs_leave", "G90", "AFC_DISABLE_SKEW",
              "G1 Y215.0 F15000", "M400", "AFC_ENABLE_SKEW", "G4 P1000",
              "RESTORE_GCODE_STATE NAME=_afc_cfs_leave"]

    def test_leaves_the_chute(self):
        assert _render_leave_purge() == self.LEAVES

    def test_already_in_front(self):
        assert _render_leave_purge(y=200.0) == []

    def test_not_homed(self):
        assert _render_leave_purge(homed="xz") == []

    def test_stays_between_unload_and_load_of_a_change(self):
        # After the unload the old lane is still (or no longer) current; the load comes next
        assert _render_leave_purge(next_lane="lane2", current_load="lane1") == []
        assert _render_leave_purge(next_lane="lane2", current_load=None) == []

    def test_leaves_after_the_load_of_a_change(self):
        assert _render_leave_purge(next_lane="lane2", current_load="lane2") == self.LEAVES
