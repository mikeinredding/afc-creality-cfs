# Creality CFS support (in testing)

Drives a Creality CFS box from AFC on mainline Klipper / Kalico, without
Creality's closed `box.py` / `box_wrapper.so`. Target setup: K1 / K1 Max with the
CFS upgrade kit, running [Simple AF](https://github.com/pellcorp/creality).

**Status: currently in testing on a real K1 + CFS** (0.2 mm nozzle, Simple AF).
Loading, purging, the napkin-strip wipe, cutting, unloading, tool changes and
4-color prints from one box work; multiple boxes on one chain are implemented and
simulator-tested but not yet tried on hardware. The wire protocol follows real
captured traffic and the load/unload steps that
[tomaspakosta/k1c-cfs-klipper](https://github.com/tomaspakosta/k1c-cfs-klipper)
confirmed. On a new setup, still treat the first sessions as bring-up: stay at the
printer and follow the test plan below in order.

## How it works

| File | Role |
|---|---|
| `extras/AFC_CFS.py` | Everything, in one file: the wire protocol (framing, CRC8, parser, command constants), the `[AFC_CFS <name>]` unit (serial link, polling, presence, RFID) and the lane sequences. |
| `[AFC_lane <name>]` | Lanes are plain AFC lanes with `unit: <cfs>:<1-4>`, so Mainsail, Fluidd and GrumpyScreen show them like any other AFC lane. At connect the unit sets each lane's `custom_load_cmd` / `custom_unload_cmd` to its `AFC_CFS_TOOL_LOAD` / `AFC_CFS_TOOL_UNLOAD`. |
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
tip per AFC settings → box idle, pre-loading off → `RETRUDE_PROCESS <slot>` stage 0
(START) *while the extruder gears still hold the filament* — the box pulls the
buffer to its empty limit → retract the extruder (`unload_retract_steps`, 60 mm total
by default, like stock's `Tn_retrude -60`) into the now-slack buffer → stage 1
(FINISH): the box reels the slot back and answers when it has finished → toolhead
sensor must be clear. Pulling out of the gears before START makes the box reel slack
and fail with `RETRUDE_ERR2`; pulling while the buffer is full makes the gears click.
If START fails anyway, FINISH is still sent (it reels the slot back on its own).
On the K1-series CFS toolhead the cutter sits below the extruder gears, just
above the nozzle (~30 mm from the gears, Creality's `buffer_empty_len`), and the
toolhead sensor is above the gears. So after the cut the gears still hold the
filament: the extruder retract must clear them before the box can reel, and the
toolhead sensor only clears once the box pulls the tip past it. The stub below
the cut stays in the nozzle and is purged by the next load.
AFC is told the tool is loaded/unloaded even when `AFC_CFS_TOOL_LOAD`/`UNLOAD`
are run by hand.

## Upgrading from `[AFC_CFS_lane]` sections

Earlier versions had their own lane section, `[AFC_CFS_lane laneN]`, and three files
in Klipper's extras. Lanes are now plain AFC lanes and everything is in `AFC_CFS.py`.
After updating:

1. Run `./install.sh` again. It links the one file and removes the old links
   (`AFC_CFS_lane.py`, `AFC_CFS_protocol.py`).
2. In each `AFC_CFS_*.cfg`, change every `[AFC_CFS_lane laneN]` to `[AFC_lane laneN]`.
   Nothing else in the section changes (`unit: CFS_1:1`, `map:` and so on).
3. Restart Klipper.

Without step 2 Klipper stops with "Section 'afc_cfs_lane lane1' is not a valid config
section".

## Setup on a K1 / K1 Max with Simple AF

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
5. In `AFC_Macro_Vars.cfg`, set `variable_pin_loc_xy` (line 35) to **your** measured cutter
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

## K1-series starting values (from Creality's box.cfg)

A K1-series printer with the CFS kit has a `box.cfg` with Creality's positions. Translated to AFC
(verified against a K1's own `box.cfg`; the K1 travels to X 229 / Y 227.5 once Simple AF
removes Creality's `gcode_position_max` limits. On a K1 Max, check its `box.cfg` and
redo the cutter calibration):

| Creality `box.cfg` | Value | AFC setting |
|---|---|---|
| `[serial_485] serial` | `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0` | `[AFC_CFS] serial` |
| `switch_pin` | `nozzle_mcu:PA8` | `[AFC_CFS] cutter_sensor_pin` |
| `[box] filament_sensor` → `filament_sensor_2` | `^!nozzle_mcu:PA10` | `[AFC_extruder] pin_tool_start` |
| `cut_pos_x` | 42.0 | `_AFC_CUT_TIP_VARS` `pin_loc_xy` X |
| `pre_cut_pos_y` → `cut_pos_y` | 160 → 224.3 (moves toward +Y) | `cut_direction: "back"` |
| `cut_pos_y` − `cut_find_start_offset_y` | 224.3 − 6 = 218.3 | `pin_loc_xy` Y (estimate, calibrate below) |
| `cut_pos_y` | 224.3 | full cut = `pin_loc_y + cut_move_dist` → `cut_move_dist: 6.0` |
| `extrude_pos_x/y/z` | 148, 225.3, Z 30 | `AFC_CFS_PARK` `park_x/park_y/min_z`, `_AFC_POOP_VARS` `purge_loc_xy` |
| `box_need_clean_length` | 140 mm | `_AFC_POOP_VARS` `purge_length` |
| `Tn_extrude_velocity` | 360 mm/min | `purge_spd: 6` (mm/s) |
| `clean_left/right_pos` | X 153.5–178, Y 225.3 | wiper, if you enable `wipe` later: `brush_loc: 165.75, 225.3, -1`, `brush_width: 24.5` |

### Lines to change in AFC's own files

These options already exist in the files the AFC installer writes, so **edit the
line that is there** instead of adding a second copy: Klipper uses the *last*
copy of an option in a section, so a line added above the default gets
overridden by it. Line numbers are for a fresh install (AFC `main` and `DEV`,
October 2026); if you added lines near the top of a file, search for the option
name instead.

`AFC/AFC.cfg`, section `[AFC]`:

| Line | Option | Default | Set to |
|---|---|---|---|
| 17 | `default_material_temps` | `default: 235, …` | `default: 220, …` (stock `Tn_extrude_temp: 220`; used when a lane has no material set) |
| 84 | `tool_cut` | `True` | `True` |
| 86 | `tool_cut_cmd` | `AFC_CUT` | `AFC_CUT` |
| 89 | `park` | `True` | `True` |
| 90 | `park_cmd` | `AFC_PARK` | `AFC_CFS_PARK` |
| 93 | `poop` | `True` | `True` |
| 97 | `kick` | `True` | `False` |
| 101 | `wipe` | `True` | `True` |
| 102 | `wipe_cmd` | `AFC_BRUSH` | `AFC_CFS_PAD_WIPE` (see *Nozzle wipes* below) |
| 113 | `post_load_macro` | commented out | remove the `# `: `post_load_macro: AFC_CFS_LEAVE_PURGE` |
| 116 | `post_unload_macro` | commented out | remove the `# `: `post_unload_macro: AFC_CFS_LEAVE_PURGE` |

`AFC/AFC_Macro_Vars.cfg`:

| Line | Option | Default | K1 | K1 Max |
|---|---|---|---|---|
| | **`[gcode_macro _AFC_CUT_TIP_VARS]`** (line 26) | | | |
| 35 | `variable_pin_loc_xy` | `-99, -99` | `42, 218.5` | `38, 300` |
| 48 | `variable_cut_direction` | `"left"` | `"back"` | `"back"` |
| 52 | `variable_pin_park_dist` | `6.0` | `6.0` | `6.0` |
| 60 | `variable_cut_move_dist` | `8.5` | `6.5` | `7` |
| 86 | `variable_retract_length` | `20` | `0` | `0` |
| 92 | `variable_cut_count` | `2` | `1` | `1` |
| 101 | `variable_pushback_length` | `15` | `0` | `0` |
| | **`[gcode_macro _AFC_POOP_VARS]`** (line 159) | | | |
| 164 | `variable_purge_loc_xy` | `-99, -99` | `148, 225.3` | `185, 305.5` |
| 165 | `variable_purge_spd` | `6.5` | `6` | `6` |
| 166 | `variable_z_purge_move` | `True` | `False` | `False` |
| 191 | `variable_purge_length` | `72.111` | `140` | `140` |
| | **`[gcode_macro _AFC_PARK_VARS]`** (line 268) | | | |
| 272 | `variable_park_loc_xy` | `-99, -99` | `148, 225.3` | `185, 305.5` |

Notes on the cutter values: `pin_loc_xy` and `cut_move_dist` are measured, so
calibrate yours (see below); the full cut ends at `pin_loc_y + cut_move_dist`,
about 1 mm past the point where the switch fires. `retract_length` 20 can drag the
tip past the blade, and `pushback_length` must stay below `retract_length`.
`cut_count: 1` is Creality's `cut_run_count`. `z_purge_move: True` would drop to
Z 0.6 at the chute. `purge_length: 140` is stock; tune it down once colour changes
are clean. `park_loc_xy` isn't used with `AFC_CFS_PARK`, but setting it silences
AFC's warning.

`printer.cfg` on a K1 Max: `[stepper_y] position_max` (about line 99 in a Simple AF
`printer.cfg`, right after `[stepper_y]` near line 90; it moves with the probe you
installed) from `306` to `307.5` (see *K1 Max values* below). The K1 needs no change.

The two post macros matter: after a load AFC restores the saved position Z first, so
without them the bed rises to the saved height + `z_hop` with the toolhead still
over the chute and comes up into the purge hopper (seen on a K1), then the
toolhead leaves diagonally. `AFC_CFS_LEAVE_PURGE` moves straight forward to
`safe_y` first and waits for the hopper to spring back.

**Why `AFC_CFS_PARK`:** AFC_PARK can only raise Z relative to the current height. A
load near the bed (first load of a print, or right after homing) would reach the
purge chute at Z ~0–2, where the mechanism hits the bed. `AFC_CFS_PARK` (in the
template) raises Z to at least 30, never lowers it, then moves over the chute.
During tool changes AFC also lifts by `[AFC] z_hop` before the cut.

### Calibrating the cutter position

Creality finds the cutter each time by moving toward `cut_pos_y` and watching the
PA8 switch. Do the equivalent once by hand. Nothing hot, toolhead empty:

1. `G28`, then `G90` and `G1 X42 Y212 F6000` (6 mm in front of the estimated pin).
2. Step toward the back with `G1 Y214`, `G1 Y216`, `G1 Y217`, then 0.5 mm at a time,
   watching the cutter arm. Stop the moment the arm **touches** the pin. That Y is
   your `pin_loc_y`. Never go past Y 225.6 (`cut_pos_y` + Creality's 1.3 mm offset).
3. Keep stepping 0.5 mm at a time and run `AFC_CFS_STATUS UNIT=CFS_1` after each;
   note the Y where `cutter sensor` turns **triggered**. If it never changes, the pin
   polarity is wrong (try `!nozzle_mcu:PA8`).
4. Set `cut_move_dist` so the full cut lands about 1 mm past the trigger point
   (`pin_loc_y + cut_move_dist = trigger_y + 1`), then `G1 Y212` to back off.
5. Test with filament loaded by hand and the nozzle hot: `AFC_CUT`. If the filament
   isn't cut through, raise `cut_move_dist` by 0.3 mm at a time, up to trigger + 1.3 mm.

Measured on a K1 with the CFS kit: arm touches the pin at **Y 218.5**, switch triggers at
**Y 224.0** (Creality's `cut_pos_y` is 224.3). So the PA8 switch is an
**end-of-travel** switch, not a contact switch: it closes only when the blade has
nearly finished its stroke. The unload uses that. With `cutter_sensor_pin` set,
each cut must make the switch fire *and* release again; if it never fires, the cut
is retried and then the unload stops before retracting
(`cutter_require_trigger: True`, the default).

### K1 Max values (measured)

The Max's `box.cfg` differs from the K1's; these worked on a K1 Max with the CFS kit,
0.4 mm nozzle, BTT Eddy (`x_offset: -23`) and load cells for Z offset, box firmware
`11350000232175B625DGIB`:

| What | K1 Max | Notes |
|---|---|---|
| Cutter | `pin_loc_xy: 38, 300`, `cut_move_dist: 7` | arm touches the pin at Y300, the switch fires at Y306 |
| Y travel | `[stepper_y] position_max: 307.5` (printer.cfg, about line 99) | Simple AF sets 306, which ends the cut exactly at the trigger; 307.5 is Creality's own CFS value (`gcode_position_max: 295` kept normal moves lower) |
| Unload retract | `unload_retract_steps: 10:600, 10:1800` | stock `Tn_retrude: -20`; only 10 mm from the cutter to the gears (`buffer_empty_len`) |
| Park / purge | X185 Y305.5, Z30; `safe_y: 291.5` | `AFC_CFS_PARK`, `_AFC_POOP_VARS`, `_AFC_PARK_VARS` |
| Purge | `purge_spd: 6`, `purge_length: 140`, `tool_load_speed: 10` | 0.4 mm nozzle |
| Napkin pad | X71.5–111.5 at Y302.5 → `x_left: 75.5`, `x_right: 107.5`, `y: 302.5`, `safe_y: 291.5` | in `_AFC_CFS_PAD_VARS`; the template's `safe_y: 215` is the K1's |
| Reel-back | ~24 s | about twice the K1's 12.4 s (longer tube) |

Creality's wiper strip on the Max is at X187–210, Y305.5 (`clean_left/right_pos`).
With an Eddy 23 mm left of the nozzle, walk the toolhead slowly to the cutter, the
chute and both ends of the pad at Z30 before running anything there at speed.

## Multiple CFS units

Up to four boxes per chain. The RS-485 cable is daisy-chained (printer → adapter →
box 1 IN, box 1 OUT → box 2 IN, …); each box's tube goes to its own input of the 4-in-1
buffer. In the config:

- one `[AFC_CFS]` section per box, all with the **same `serial`** (they share the
  port and take turns on the bus), each with its own `box_addr` (1–4) and `box_uid`;
- all of them `hub: CFS_1` — the buffer is the one place every box's filament meets,
  so filament left there from any box blocks loading from any other;
- lanes `lane5`–`lane8` for box 2 and so on (`templates/AFC_CFS_2.cfg`).

Boxes forget their address when powered off and answer discovery one at a time, in
no guaranteed order. Discovery therefore gives each box the `box_addr` of the section
whose `box_uid` matches; a box at an address that belongs to another uid (e.g. left
addressed by stock firmware) is refused with a message until the chain is
power-cycled. A single box without `box_uid` works as before.

Hardware-untested so far: two boxes answering one discovery broadcast at once (stock
sends one broadcast per expected box, so they appear to take turns), and whether
a second box needs anything when the other box's slot is in the buffer.

## Nozzle wipes (K1 napkin strip)

Creality has K1 owners stick a "napkin strip" pad on the back edge of the build
plate; measured on a K1 it runs X60–96, front edge at Y223, with the nozzle over its middle at Y226 (the wipe line). Simple
AF's load cell wipe (`loadcells_wipe.cfg`) scrapes a random 30 mm line anywhere in the
middle third of X at Y222.5–224.5, which with the CFS kit can run past the pad into the
purge hopper (that collision skipped a belt on the K1 and the next cut missed).

`templates/AFC_CFS_wipe.cfg` fixes the location and adds a tool-change wipe:

- `_SAF_NOZZLE_WIPE` replaces Simple AF's start-of-print wipe (the `START_PRINT`
  hook): same heating, tapping (Simple AF's `_LOAD_CELL_WIPE_MEASURE`) and slow scrape,
  but always on the pad, approached and left from the front, lifting off forward
  instead of continuing sideways (toward the hopper one way, the cutter pin the other).
  Keep `[include loadcells_wipe.cfg]` in `printer.cfg` for the helpers.
- `AFC_CFS_PAD_WIPE` (AFC `wipe_cmd`) drags the nozzle across the pad after each purge
  at the pad height the start wipe measured (`wipe_height` above it), no taps.

Copy it to the AFC config folder, set the pad in `_AFC_CFS_PAD_VARS` if yours differs,
and test `_SAF_NOZZLE_WIPE EXTRUDER_TEMP=220 BED_TEMP=60` by hand first.

## Simple AF macros

Simple AF's installer rewrites `printer.cfg`, so put the `_SAF_…` hooks and overrides
below in your own custom macros file (for example `custom_macros.cfg`, included in
`printer.cfg` after Simple AF's includes), not in `printer.cfg` itself.

```ini
# Home Y first: X homes to the right along the back, through the purge chute,
# if the printer was switched off while parked there.
[gcode_macro _HOMING_PARAMS]
variable_home_y_before_x: True

# Load the print's first tool before START_PRINT's purge line, so the line
# comes out in the right colour. Needs INITIAL_TOOL on the slicer's START_PRINT line.
[gcode_macro _SAF_START_PRINT_BEFORE_LINE_PURGE]
description: Load the print's first tool before the purge line (AFC + CFS)
gcode:
  {% if params.INITIAL_TOOL is defined %}
    T{params.INITIAL_TOOL}
  {% endif %}
```

In OrcaSlicer: `START_PRINT EXTRUDER_TEMP=[nozzle_temperature_initial_layer]
BED_TEMP=[bed_temperature_initial_layer_single] INITIAL_TOOL=[initial_no_support_extruder]`
as the first line of the machine start G-code, and `T[next_extruder]` as the change
filament G-code (prime tower and ramming off, "Retraction when switching material" 0).

Keep `_SAF_NOZZLE_WIPE` only in `AFC/AFC_CFS_wipe.cfg`: it must load after Simple AF's
`loadcells_wipe.cfg`, which the `[include AFC/*.cfg]` at the end of `printer.cfg`
guarantees. Defining it again in the custom macros file would make whichever file
loads last win.

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

**Recovering from a failed load.** A load that stops before the extruder grabs the
filament can leave it partway up the tube or at the toolhead sensor. Reel it back
with the box alone, then clear AFC's error state:

```
AFC_CFS_EJECT LANE=lane1
RESET_FAILURE
```

`AFC_CFS_EJECT` only works while the filament is short of the extruder gears; once
the extruder has it, use `AFC_CFS_TOOL_UNLOAD`.

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

## RFID tags

Each CFS slot has an RFID reader. Creality RFID spools, and tags written with
community tools in the same format, carry the spool's material, color and size. With
`rfid_read: True` in an `[AFC_CFS]` section, the plugin reads a spool's tag once when
it goes into a slot, then sets the lane with AFC's own commands:

- `SET_COLOR`, `SET_MATERIAL` and `SET_WEIGHT` from the tag, so Mainsail, GrumpyScreen
  and Orca (through AFC's lane data) show the right filament;
- or `SET_SPOOL_ID`, if the tag carries a Spoolman spool ID (a number in the tag's
  reserved field, as Jacob10383's K2 firmware uses it) and AFC has Spoolman. AFC then
  takes color, material and weight from Spoolman.

A lane that already has a Spoolman spool is left alone. Nothing is read during a print
(a spool inserted mid-print is read after it ends), or while any slot of that box is
loaded to the toolhead (untested; the read waits until the box is unloaded).

AFC saves each lane's color, material and weight, so they survive a restart while
the spool stays in its slot. For spools swapped while the printer was off, set
`rfid_read_at_startup: True` (off by default): once Klipper has started and PREP is
done, every slot with a spool is read once, one after another (after
`rfid_startup_delay`, 15 s). Slots without a tag can keep the box busy for about a
minute each, so with several untagged spools this takes a few minutes; it waits
until no slot of the box is loaded and never runs during a print.

Read one by hand: `AFC_CFS_READ_TAG LANE=lane4`. `APPLY=0` only reports the tag;
`SPIN=0` skips turning the slot first. `AFC_CFS_STATUS` shows the last tag per lane.

**How the read works.** Like the stock `BOX_INFO_REFRESH` macro, the slot is turned
briefly (`SET_PRE_LOADING RUN`), then the box is asked for the tag (`GET_RFID`). On a
slot **without** a tag the box can stay busy and silent for about a minute after the
turn; the plugin waits for it (`rfid_busy_timeout`, 90 s) and doesn't mark the box
offline. Other boxes on the chain aren't polled meanwhile.

**Tag format** (40 characters, community documented; checked against a real tag):

| Field | Chars | Example | Meaning |
|---|---|---|---|
| date | 5 | `AB124` | date code |
| vendor | 4 | `0276` | Creality |
| batch | 2 | `A2` | |
| filament id | 6 | `100002` | `1` + 5-digit material code |
| color | 7 | `0E7A18E` | `0` + RRGGBB |
| length | 4 | `0330` | 1 kg (`0247` 750 g, `0198` 600 g, `0165` 500 g, `0082` 250 g) |
| serial | 6 | `000001` | |
| reserved | 6 | `000000` | a number > 1 here = Spoolman spool ID |

Material codes the plugin knows: `00001` Generic PLA, `00002` Generic PLA-Silk,
`01001` Hyper PLA (all set as PLA). For any other code the console names it; add it
with `rfid_materials: <code>=<MATERIAL>, ...`.

Options (`[AFC_CFS]`): `rfid_read` (False), `rfid_insert_delay` (10 s),
`rfid_read_at_startup` (False), `rfid_startup_delay` (15 s),
`rfid_spin` (True), `rfid_busy_timeout` (90 s), `rfid_read_attempts` (3),
`rfid_retry_interval` (5 s), `rfid_materials`.

If a tagged spool reads as no tag, the tag is probably on the side facing away from
the reader; turn the spool around. The box's remaining-length reply (`GET_REMAIN_LEN`)
came back as "not known" in testing, so remaining filament isn't used.

## Known unknowns

- **Motor commands block.** The box answers `EXTRUDE_PROCESS` / `RETRUDE_PROCESS`
  stages only when the motion finishes and handles one command at a time; on a K1,
  stage 0 took ~3.7 s and the `RETRUDE_PROCESS` stage-1 reel-back of 1.5 m took 12.4 s.
  Those use `stage_timeout` (30 s). Stage 1 answering OK is the "reel-back done" signal;
  generic check-ins only report idle. If one still times out,
  the next command waits for its late reply (`resync_timeout`) instead of queueing
  behind the busy box.

- `stage7_arg` defaults to 3 (from an independent K2 implementation); the K1C
  testing used 2. If stage 7 returns `PARAMS_ERR`, try `stage7_arg: 2`.
- The stock handoff retries stage 7 with measuring-wheel verification; this
  implementation does a single handoff and relies on `load_attempts`.
- Multiple boxes on one bus: two boxes tested on a K1 Max; three or four are
  simulator-tested only.
- RFID: tested with one tag written by a third-party tool; no Creality-made spool yet.
- `eject_lane` / `AFC_CFS_EJECT` works on a K1 (it reeled a cut, retracted lane
  back and recovered a failed unload).

## Credits

Protocol knowledge from tomaspakosta/k1c-cfs-klipper, ityshchenko/klipper-cfs,
gitstonelabs/creality-cfs-klipper, FrederickAlt's K1 CFS docs,
fake-name/cfs-reverse-engineering and Jacob10383/k2-plus-custom-firmware. All GPL-3.0.

## Tests

```
./run-tests.sh                              # unit tests
KLIPPER_PATH=~/klipper ./run-tests.sh       # plus real reactor + simulated box
```
