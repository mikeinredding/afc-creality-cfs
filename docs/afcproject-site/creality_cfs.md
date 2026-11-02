!!! warning
    The CFS unit needs Klipper running on the printer with Creality's own CFS software removed.
    On a K1, K1C or K1 Max that means [Simple AF](https://pellcorp.github.io/creality-wiki/),
    which removes `box.cfg`, the `[box]` section and Creality's `box_wrapper` on install. The
    CFS box's own firmware is not changed.

The Creality CFS (Color Filament System) is a closed box: it moves the filament itself and
reports its own sensors over RS-485, through a CH340 USB adapter built into its cable. AFC
never drives a CFS motor directly. Instead the `AFC_CFS` unit talks to the box, mirrors each
slot's sensors into a normal AFC lane, and runs the box's own load and unload sequence when
AFC changes tools. Lanes are plain `[AFC_lane]` sections, so Mainsail, Fluidd, GrumpyScreen and
Orca see them like any other AFC lane.

Tested on a K1 with one box and a K1 Max with two boxes, both on Simple AF.

## Supports
- Up to four CFS boxes on one RS-485 chain (two tested on hardware)
- Loading, unloading and tool changes with the toolhead cutter, including multicolor prints
- Infinite runout to another lane, or pause, when a spool runs out mid-print
- Reading Creality-format RFID tags to set a lane's color, material and weight, or its
  [Spoolman](../features/spoolman.md) spool when the tag carries a spool ID (off by default)
- Verifying each cut with the CFS kit's cutter switch, and retrying a cut that didn't finish
- Ejecting a lane back into the box (`AFC_CFS_EJECT`)
- Restoring lane state after a restart, including a lane loaded in the toolhead
- More things can be found on the [features](../features.md) page

## Does Not Support
- Remaining filament length from the box (it reports "unknown")
- Creality's measuring-wheel retry during the stage 7 handoff; a failed load is retried with
  `load_attempts` instead
- Hub, bowden or TD-1 calibration: the box feeds until the toolhead sensor triggers, so there
  is no distance to calibrate (`UNIT_CALIBRATION` says so). Only `tool_stn` needs measuring.
- Three or four boxes on one chain has only been tested in simulation
- RFID has been tested with community-written tags; no Creality-made spool yet

## General Notes

- After installing or updating, **restart the Klipper service**
  (`/etc/init.d/S55klipper_service restart` on a K1). `FIRMWARE_RESTART` rereads the config but
  keeps the old Python code loaded; loads then fail with
  "Homing error occurred when trying to short move to hub sensor".
- Lanes are plain AFC lanes with `unit: <cfs name>:<1-4>` for slots A-D, left to right. Don't
  give them `prep` or `load` pins; the box reports presence itself.
- Every box's tube goes to its own input on the 4-in-1 buffer. That buffer is AFC's hub for
  every box (`hub: CFS_1` in each `[AFC_CFS]` section): filament left in it from any box blocks
  loading from any other.
- With more than one box, set `box_uid` on each. Without it the boxes are addressed in the
  order they answer, which can change between power-ups and swap your lanes.
  `AFC_CFS_STATUS UNIT=<name>` shows a box's uid.
- A load takes about a minute: heating, the box feeding to the toolhead sensor, then two short
  extruder pushes (`prime1`, `prime2`) while the box hands over; see *Load speed* below.
- Set `auto_home: True` in `[AFC]` so a tool change on an unhomed printer homes first instead
  of stopping with "Please home printer before doing a tool load".
- The box's lights: a pulsing white slot is the one loaded to the toolhead. Chasing red means
  the box thinks it has lost contact with the printer, which can happen to one box while
  another box on the chain is busy for a long time (e.g. reading an untagged RFID spool). It
  clears as soon as the printer next sends that box a command.
- Routine load and unload steps go to `AFC.log` only. Set `debug: True` in `[AFC]` to see them
  in the console as well.

### Installing
Copy `templates/AFC_CFS_1.cfg` into your AFC config folder (`AFC/` next to `printer.cfg`), and
`templates/AFC_CFS_2.cfg` for a second box. Then edit:

- `serial:` - run `ls /dev/serial/by-id/` and pick the `1a86` device. Every box on a chain uses
  the same `serial`.
- `cutter_sensor_pin:` - the CFS kit's cutter switch, if your kit has one on a Klipper pin
  (`nozzle_mcu:PA8` on a K1). Set it on one box only; the other boxes on the chain use it.
- In `AFC_Hardware.cfg`, `[AFC_extruder extruder]`: `pin_tool_start` is the CFS kit's toolhead
  filament sensor (`^!nozzle_mcu:PA10` on a K1), `tool_stn` the sensor-to-nozzle distance
  (start around 60 and measure), `tool_stn_unload: 0`, `tool_load_speed: 10`.

In `AFC.cfg`, `[AFC]`, edit the existing lines rather than adding new ones (Klipper uses the
last copy of an option):

| Option | Set to | Why |
|---|---|---|
| `default_material_temps` | `default: 220, ...` | Creality's purge temperature, for lanes with no material set |
| `tool_cut` / `tool_cut_cmd` | `True` / `AFC_CUT` | The CFS kit cuts at the toolhead |
| `park` / `park_cmd` | `True` / `AFC_CFS_PARK` | Parks over the purge chute at a safe height (see below) |
| `poop` | `True` | Purges after a color change |
| `kick` | `False` | The K1 has no kick target |
| `wipe` | `False` | Unless you set up a wipe macro of your own |
| `post_load_macro` | `AFC_CFS_LEAVE_PURGE` | Leaves the chute before AFC lowers Z |
| `post_unload_macro` | `AFC_CFS_LEAVE_PURGE` | Same, after an unload |
| `auto_home` | `True` | Homes before a tool change if needed |

`AFC_CFS_PARK` and `AFC_CFS_LEAVE_PURGE` are in the template. `AFC_PARK` can only raise Z
relative to the current height, so a load right after homing would reach the purge chute at
Z 0-2, where the mechanism hits the bed. `AFC_CFS_PARK` raises Z to at least 30 first.

### Starting values for K1-series printers
From Creality's own `box.cfg`, measured on a K1 and a K1 Max. Measure the cutter position on
your own printer (next section) instead of copying it.

| Setting | K1 | K1 Max |
|---|---|---|
| `_AFC_CUT_TIP_VARS` `pin_loc_xy` | `42, 218.5` | `38, 300` |
| `cut_direction` | `"back"` | `"back"` |
| `cut_move_dist` | `6.5` | `7` |
| `retract_length` / `pushback_length` | `0` / `0` | `0` / `0` |
| `cut_count` | `1` | `1` |
| `_AFC_POOP_VARS` `purge_loc_xy` | `148, 225.3` | `185, 305.5` |
| `purge_spd` / `purge_length` | `6` / `140` | `6` / `140` |
| `z_purge_move` | `False` | `False` |
| `AFC_CFS_PARK` `park_x`, `park_y`, `min_z` | `148`, `225.3`, `30` | `185`, `305.5`, `30` |
| `[AFC_CFS]` `unload_retract_steps` | default (`15:600, 45:1800`) | `10:600, 10:1800` |
| `[stepper_y] position_max` | no change | `307.5` |

On a K1 Max with a side-mounted probe, walk the toolhead slowly to the cutter and the chute at
Z 30 before running anything there at speed.

### Calibrating the cutter position
The CFS kit's cutter switch is an end-of-travel switch: it closes only when the blade has
nearly finished its stroke. With nothing hot and the toolhead empty:

1. `G28`, then `G90` and `G1 X42 Y212 F6000` (K1; 6 mm in front of the estimated pin).
1. Step toward the back, 0.5 mm at a time near the end, and stop the moment the cutter arm
   touches the pin. That Y is your `pin_loc_xy` Y.
1. Keep stepping 0.5 mm at a time and run `AFC_CFS_STATUS UNIT=CFS_1` after each. Note the Y
   where `cutter sensor` turns `triggered`. If it never does, try the inverted pin
   (`!nozzle_mcu:PA8`).
1. Set `cut_move_dist` so the full cut ends about 1 mm past the trigger point
   (`pin_loc_y + cut_move_dist = trigger_y + 1`), then `G1 Y212` to back off.
1. With filament loaded by hand and the nozzle hot, run `AFC_CUT`. If the filament isn't cut
   through, raise `cut_move_dist` 0.3 mm at a time.

With `cutter_sensor_pin` set, every cut must make the switch fire and release again. A cut
where it never fires is retried, then the unload stops before retracting possibly uncut
filament (`cutter_require_trigger: True`).

### Multiple CFS units
The RS-485 cable is daisy-chained: printer, adapter, box 1 IN, box 1 OUT, box 2 IN, and so on.
In the config, one `[AFC_CFS]` section per box, all with the same `serial`, each with its own
`box_addr` (1-4) and `box_uid`, and all with `hub: CFS_1`. Box 2's lanes are `lane5`-`lane8`
(`templates/AFC_CFS_2.cfg`).

To find each box's uid: with only box 1 connected, run `AFC_CFS_STATUS UNIT=CFS_1` and copy
its `box uid`. Connect box 2 and power-cycle both; the console names the new box's uid
("isn't in any [AFC_CFS] section"). Put it in box 2's `box_uid`, restart, and power-cycle the
boxes once more.

### RFID tags
Each slot has an RFID reader. With `rfid_read: True` in an `[AFC_CFS]` section, a spool's tag
is read once when it goes into a slot, and the lane is set with `SET_COLOR`, `SET_MATERIAL`
and `SET_WEIGHT`, or with `SET_SPOOL_ID` if the tag carries a Spoolman spool ID and AFC has
Spoolman. A lane that already has a Spoolman spool is left alone.

- Nothing is read during a print, or while any slot of that box is loaded to the toolhead;
  those reads wait.
- On a slot with no tag the box searches for up to about 90 seconds, turning its slots, and
  the other boxes on the chain wait meanwhile. Leave `rfid_read` off if you mostly use
  untagged spools.
- `rfid_read_at_startup: True` also reads every full slot once after Klipper starts, for
  spools swapped while the printer was off.
- Read one by hand with `AFC_CFS_READ_TAG LANE=lane4` (`APPLY=0` only reports it).
- Material codes known: `00001` Generic PLA, `00002` Generic PLA-Silk, `01001` Hyper PLA. The
  console names any other code; add it with `rfid_materials: <code>=<MATERIAL>, ...`.
- If a tagged spool reads as no tag, turn the spool around so the tag faces the reader.

### Load speed
The two pushes during the handoff are set in mm/min. `prime1_speed` (35) is Creality's stock
value. `prime2_speed` defaults to 600; Creality's stock firmware uses 10, which adds about
30 seconds to every load, and only runs that slow push as a fallback. If loads fail at the
handoff ("toolhead sensor cleared after the handoff"), set `prime2_speed: 10` in the
`[AFC_CFS]` section to go back to stock.

### Slicer setup
In OrcaSlicer, set up the printer as described on the [slicer setup](../installation/slicer-setup.md)
page, with prime tower and ramming off and "Retraction when switching material" at 0. On Simple
AF, add `INITIAL_TOOL=[initial_no_support_extruder]` to the `START_PRINT` line so the first
tool is loaded before the purge line.

### Troubleshooting
- `AFC_CFS_STATUS UNIT=<name>` shows the box's state: version, uid, which slots have filament,
  the buffer, the cutter switch, link counters and each lane's last tag.
- `AFC_CFS_RECONNECT UNIT=<name>` reopens the link to a box.
- `trace_file: <path>` in an `[AFC_CFS]` section records everything the unit sends to and gets
  from the box, with load and unload timings. `AFC_CFS_TRACE UNIT=<name> MARK="..."` adds a
  marker before a test, and `utilities/cfs_trace_summary.py <file>` summarizes a trace. Attach
  the trace when reporting a problem.
- `AFC_CFS_RAW` sends a single raw frame to the box. It's for protocol debugging only: motor
  commands sent this way bypass all of AFC's checks.
