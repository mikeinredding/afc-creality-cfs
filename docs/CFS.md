# Creality CFS support (experimental)

Drives a Creality CFS box from AFC on mainline Klipper / Kalico, without
Creality's closed `box.py` / `box_wrapper.so`. Target setup: K1 / K1 Max with the
CFS upgrade kit, running [Simple AF](https://github.com/pellcorp/creality).

**Status:** the wire protocol is validated against real captured traffic, and the
load/unload sequences follow steps that
[tomaspakosta/k1c-cfs-klipper](https://github.com/tomaspakosta/k1c-cfs-klipper)
confirmed on hardware. The AFC integration itself has been tested against a
simulated box on Klipper's real reactor, **not yet on a real printer**. Treat
the first sessions as bring-up: stay at the printer and follow the test plan
below in order.

## How it works

| File | Role |
|---|---|
| `extras/AFC_CFS_protocol.py` | Framing, CRC8, frame parser, command constants. No Klipper imports. |
| `extras/AFC_CFS.py` | `[AFC_CFS <name>]` unit. Owns the serial link, polls the box, mirrors slot presence into lanes. |
| `extras/AFC_CFS_lane.py` | `[AFC_CFS_lane <name>]` lane. `AFC_CFS_TOOL_LOAD` / `AFC_CFS_TOOL_UNLOAD` run as AFC's `custom_load_cmd` / `custom_unload_cmd`. |
| `templates/AFC_CFS_1.cfg` | Example config for one box. |

The box connects over its built-in CH340 USB-RS485 adapter at 230400 baud.
All serial I/O is non-blocking on Klipper's reactor (`register_fd` + completions),
so a slow or silent box cannot stall heater or MCU timers. An earlier community
draft that used blocking reads tripped a false `verify_heater` shutdown mid-load.

**Presence:** the unit polls `GET_FILAMENT_SENSOR_STATE` every `poll_interval` and
sets each lane's prep/load state from its slot bit. Pulling a spool mid-print
pauses, or hands off to the lane's `runout_lane` if one is set.

**Load** (per attempt):
`SET_PRE_LOADING CLOSE` → `SET_BOX_MODE IDLE` → `CTRL_CONNECTION_MOTOR_ACTION EXTRUDE`
→ `TIGHTEN_UP on` → `EXTRUDE_PROCESS` stage 0, 4 → poll stage 5 until the toolhead
sensor trips (aborts on timeout or a stalled measuring wheel) → extruder prime
10 mm → stage 6 → prime 5 mm → stage 7 → `SET_BOX_MODE PRINT <slot>` → tighten off,
connection stop, `IDLE` (clears the latched error the box shows after a load) →
push the rest of `tool_stn`.

**Unload:** `AFC_CUT` (verified against `cutter_sensor_pin` if set) → park / form
tip per AFC settings → `RETRUDE_PROCESS <slot>` stages 0, 1 → retract the extruder
in steps (`unload_retract_steps`), checking in with the box (`RETRUDE_PROCESS 0,0`)
before each one → wait for the toolhead sensor to clear.

## Setup on a K1 Max with Simple AF

1. Install AFC with the steps in Simple AF's FAQ,
   [How to install AFC on Simple AF](https://pellcorp.github.io/creality-wiki/faq/#how-to-install-afc-on-simple-af).
   Clone into `/usr/data`, not `/root`. This works with AFCProject's `main` branch
   (what that FAQ installs) and with newer AFC `DEV` builds; CI tests both.
   AFC's installer has no CFS option; see
   [AFC installer choices for a CFS](#afc-installer-choices-for-a-cfs) below.
2. Install this repo, also under `/usr/data`:
   ```sh
   cd /usr/data
   git clone https://github.com/mikeinredding/afc-creality-cfs.git
   cd afc-creality-cfs && ./install.sh -t
   ```
   It finds Klipper at `/usr/data/klipper`, links the CFS files into its extras,
   and copies `AFC_CFS_1.cfg` into `/usr/data/printer_data/config/AFC/`.
3. Make sure nothing else owns the box: no `box.cfg` include, no `[box]` section,
   no Creality `box_wrapper`. Simple AF's install (and the
   [K1 firmware downgrade](https://pellcorp.github.io/creality-wiki/cfs_downgrade/)
   + factory reset it may require) already removes Creality's CFS software. That's
   expected: this project replaces it. The CFS box's own firmware is not touched;
   `AFC_CFS_STATUS` shows its version, and please report it if it differs from what
   others have tested.
4. Edit `AFC/AFC_CFS_1.cfg`:
   - `serial:` — `ls /dev/serial/by-id/`, pick the `1a86` device.
   - `cutter_sensor_pin:` — if your kit's cutter switch is on a Klipper pin.

   And in `AFC/AFC_Hardware.cfg`, under `[AFC_extruder extruder]` (not in the CFS
   file: AFC loads `AFC/*.cfg` alphabetically, so `AFC_Hardware.cfg` would override it):
   - `pin_tool_start:` — the CFS kit's toolhead filament sensor. Simple AF deletes
     `[filament_switch_sensor filament_sensor_2]` on install; take its `switch_pin`
     from your stock config backup.
   - `tool_stn:` — sensor-to-nozzle distance (start around 60 and measure),
     `tool_stn_unload: 0`, `tool_load_speed: 10`.
5. In `AFC_Macro_Vars.cfg`, set `variable_pin_loc_xy` to **your** measured cutter
   lever position. Measure it by jogging from the console in small steps until the
   lever is pressed; don't copy someone else's numbers. The community has seen it
   move after the kit was re-seated.
6. In GrumpyScreen's `grumpyscreen.ini` set `[mmu] backend: afc` to get the lane panel.

## AFC installer choices for a CFS

AFC's `install-afc.sh` only knows units it drives itself (BoxTurtle, NightOwl, …).
Install it as a 4-lane BoxTurtle to get AFC's shared files, then remove the BoxTurtle
unit file. In the **I. Install New System** menu:

| Option | Set to | Why |
|---|---|---|
| T. Installation Type | `BoxTurtle (4-Lane)` (default) | Simplest unit file to remove afterwards |
| 1. Add AFC includes | `True` | Adds `[include AFC/*.cfg]` to printer.cfg |
| 2. Tip-forming | `False` | The CFS kit cuts |
| 3. Toolhead cutter | `True` | Uses `AFC_CUT` with your lever position |
| 4. Hub cutter | `False` | |
| 5. Kick macro | `False` | Needs a kick target the K1 doesn't have |
| 6. Park macro | `True` | Park over the purge chute before loads |
| 7. Poop macro | `True` | Purges after a colour change |
| 8. Wipe macro | `False` until you configure a brush position | |
| 9. Toolhead sensor or ramming | `Sensor` | The CFS kit has a toolhead sensor |
| A. Toolhead sensor pin | the CFS kit's sensor pin | Written to `AFC_Hardware.cfg` |
| B. Buffer type | `None` | The CFS buffer is inside the box |
| C. BoxTurtle Name | leave `Turtle_1` | |

Then **I** to install, and **Q** to quit. Afterwards, before restarting Klipper:

```sh
cd /usr/data/printer_data/config/AFC
rm AFC_Turtle_1.cfg            # BoxTurtle unit: its [mcu Turtle_1] would stop Klipper starting
```

`mcu/AFC_Lite.cfg` can stay; it was only loaded from that file. Then run this repo's
`./install.sh -t` and continue with step 3 above.

## Test plan — do these in order, at the printer

Each step should pass before the next. `AFC_CFS_RAW` is available for poking at the
protocol directly.

1. **Link, read-only.** `AFC_CFS_STATUS UNIT=CFS_1`. Expect a version string,
   correct "filament present" slots, and `timeouts 0`. Pull a spool: within
   `poll_interval` the lane shows empty.
2. **Toolhead sensor.** `QUERY_ENDSTOPS` / AFC status with filament pushed in by
   hand above the extruder; `pin_tool_start` must read triggered.
3. **Cutter alone.** Heat, load filament manually, run `AFC_CUT`. The lever must
   actuate and the filament must be cut.
4. **Load slot A.** Nozzle hot, parked over the chute: `AFC_CFS_TOOL_LOAD LANE=lane1`.
   Watch for filament at the nozzle. Check `AFC_CFS_STATUS` afterwards shows
   connected slot `A` and box state `OK`.
5. **Unload slot A.** `AFC_CFS_TOOL_UNLOAD LANE=lane1`. Filament should be cut,
   pulled out of the gears, and reeled back into the box.
6. **Slot switch.** Load B, unload, load D. The community draft could only ever load
   slot A until the stage 6/7 + `PRINT` mode steps were added; this checks they work.
7. **Through AFC.** `CHANGE_TOOL LANE=lane2`, then `T0`/`T1` from the console.
8. **A short two-colour print**, then a runout test (pull a spool mid-print).

If a load fails, the lane goes to error and AFC pauses. `AFC_CFS_STATUS` shows the
box state; the lane's `cfs_last_load` status field records stage 5 polls, the last
measuring-wheel reading and the stage 6/7 replies.

Before each step, drop a marker in the trace so it's easy to find afterwards:
`AFC_CFS_TRACE UNIT=CFS_1 MARK="step 4 load A"`.

## Recording a trace

With `trace_file` set (the template turns it on), the unit records a JSON-lines file
of everything it does with the box:

- every frame sent and received, with reply latency, plus timeouts, CRC errors and
  replies nobody asked for
- connection changes and the box's firmware version
- each load: start, every attempt, stage 5 time / polls / measuring-wheel reading,
  stage 6/7 replies, end result and total time
- each unload: cut (and whether the blade released), every retract step with the
  box's reply and toolhead sensor, end result and time
- spool insert/remove, runouts, failures, print start/stop with the file name
- the effective settings, recorded at every Klipper start

Idle status polls are left out unless `trace_polls: True`, so the file only grows
when something happens. It rotates at `trace_max_size` (5 MB) keeping
`trace_backups` old copies. Writes happen on a background thread, so tracing doesn't
affect Klipper's timing.

`AFC_CFS_TRACE UNIT=CFS_1` reports where it's recording; `ENABLE=0` / `ENABLE=1`
pauses and resumes; `MARK="text"` adds a note. `AFC_CFS_STATUS` shows it too.

To look at it, download `cfs_trace.jsonl` from the Logs section of Mainsail/Fluidd,
or summarize it on the printer:

```sh
python3 /usr/data/afc-creality-cfs/tools/cfs_trace_summary.py /usr/data/printer_data/logs/cfs_trace.jsonl
python3 .../cfs_trace_summary.py cfs_trace.jsonl --since "2026-10-22 19:00"
python3 .../cfs_trace_summary.py cfs_trace.jsonl --events fail,load_end   # raw events
```

The summary gives load/unload success per slot, timings, stage 7 replies, retries,
reply latency per command, non-OK box replies, timeouts, and a timeline of failures
and markers. When reporting a problem, attach the trace file(s) and `klippy.log`.

## Known unknowns

- `stage7_arg` defaults to 3 (from an independent K2 implementation); the K1C
  testing used 2. If stage 7 returns `PARAMS_ERR`, try `stage7_arg: 2`.
- The stock handoff retries stage 7 with measuring-wheel verification; this
  implementation does a single handoff and relies on `load_attempts`.
- Multiple boxes on one bus (`box_addr` 2-4) are untested.
- `eject_lane` (reel a lane back with nothing in the toolhead) is untested.

## Credits

Protocol knowledge from tomaspakosta/k1c-cfs-klipper, ityshchenko/klipper-cfs,
gitstonelabs/creality-cfs-klipper, FrederickAlt's K1 CFS docs,
fake-name/cfs-reverse-engineering and Jacob10383/k2-plus-custom-firmware. All GPL-3.0.

## Tests

```
./run-tests.sh                              # unit tests
KLIPPER_PATH=~/klipper ./run-tests.sh       # plus real reactor + simulated box
```
