"""Tests that only run in the afc-creality-cfs repo, not in AFC's.

- cfs_trace_summary.py, the trace summarizer in tools/.
- The CFS unit on Klipper's real reactor against a simulated box
  (tests/cfs_reactor_harness.py and tests/cfs_sim.py), skipped unless
  KLIPPER_PATH points at a Klipper (or Kalico) checkout.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys

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
    def test_buffer_changes_while_printing(self):
        t = 1_760_000_000.0
        def buf(dt, state, used, printing=True):
            return {"ts": t + dt, "ev": "buffer", "state": state, "printing": printing,
                    "filament_used": used}
        events = [
            buf(0, "full", 0.0, printing=False),      # before the print: ignored
            buf(10, "middle", 5.0),
            buf(70, "empty", 45.0),
            buf(75, "full", 47.5),
            buf(200, "middle", 0.0),                  # next print restarts at 0: no gap
        ]
        buf_out = io.StringIO()
        summary.summarize(events, out=buf_out)
        lines = buf_out.getvalue().splitlines()
        i = lines.index("Buffer while printing: 4 changes")
        assert lines[i + 1:i + 4] == [
            "  states: middle x2, empty x1, full x1",
            "  filament between changes (mm): min 2.5  avg 21.2  max 40.0",
            "  longest: 40 mm in 'middle' (60 s)",
        ]

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
