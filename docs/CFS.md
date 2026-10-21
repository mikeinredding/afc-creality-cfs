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
   - `pin_tool_start:` — the CFS kit's toolhead filament sensor. Simple AF deletes
     `[filament_switch_sensor filament_sensor_2]` on install; take its `switch_pin`
     from your stock config backup.
   - `cutter_sensor_pin:` — if your kit's cutter switch is on a Klipper pin.
5. In `AFC_Macro_Vars.cfg`, set `variable_pin_loc_xy` to **your** measured cutter
   lever position. Measure it by jogging from the console in small steps until the
   lever is pressed; don't copy someone else's numbers. The community has seen it
   move after the kit was re-seated.
6. In GrumpyScreen's `grumpyscreen.ini` set `[mmu] backend: afc` to get the lane panel.

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
measuring-wheel reading and the stage 6/7 replies. Set `protocol_debug: True` to log
every frame.

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
