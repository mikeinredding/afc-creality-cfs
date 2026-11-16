# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Creality CFS: Creality's 4-slot filament box, up to four on one RS-485 chain."""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from installer.config_editor.blocks import section_names, set_key_in_section
from installer.config_editor.document import ConfigDocument
from installer.config_editor.edits import replace_text
from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.platform_core.fsops import safe_copy
from installer.unit_registry.base import BufferTarget, UnitType
from installer.unit_registry.options import (
    CFS_BOX_OPTIONS,
    ChoiceOption,
    NameOption,
    UnitOption,
)
from installer.unit_registry.selection import UnitSelection

FIRST_NAME = "CFS_1"
TEMPLATE = "AFC_CFS_1.cfg"
MAX_BOXES = 4                               # box_addr is 1-4
UID_PLACEHOLDER = "000000000000000000000000"

_SECTION = re.compile(r"^\[AFC_CFS ([^\]]+)\]\s*$")
_HEADER = re.compile(r"^\[")
_OPTION = re.compile(r"^([A-Za-z0-9_]+)\s*[:=]\s*(.*?)\s*$")
_LANE = re.compile(r"lane(\d+)$")
_UID = re.compile(r"[0-9a-f]{24}")


class CfsBox(NamedTuple):
    """An `[AFC_CFS]` section already in the AFC config directory."""

    name: str
    path: Path
    options: Dict[str, str]


def _strip_comment(value: str) -> str:
    """
    A config value without its trailing `#` comment.

    :param value: raw value text
    :return str: the value
    """
    return value.split("#", 1)[0].strip()


def find_cfs_boxes(cfg_dir: Path) -> List[CfsBox]:
    """
    Every `[AFC_CFS]` section in the AFC config directory, in file then line order.

    :param cfg_dir: AFC config directory
    :return List[CfsBox]: the boxes, with their uncommented options
    """
    boxes: List[CfsBox] = []
    for path in sorted(cfg_dir.glob("*.cfg")):
        try:
            doc = ConfigDocument.load(path)
        except OSError:
            continue
        current: Optional[CfsBox] = None
        for line in (raw.rstrip("\r\n") for raw in doc.lines):
            match = _SECTION.match(line)
            if match:
                current = CfsBox(match.group(1), path, {})
                boxes.append(current)
                continue
            if _HEADER.match(line):
                current = None
                continue
            option = _OPTION.match(line)
            if current is not None and option:
                current.options[option.group(1)] = _strip_comment(option.group(2))
    return boxes


def _box_addr(box: CfsBox) -> int:
    """
    The box_addr a section uses (1 when unset, like the unit does).

    :param box: the section
    :return int: the address, or 0 if it can't be read
    """
    try:
        return int(box.options.get("box_addr", "1"))
    except ValueError:
        return 0


def _next_lane_number(cfg_dir: Path) -> int:
    """
    The first lane number after every `[AFC_lane laneN]` already configured.

    :param cfg_dir: AFC config directory
    :return int: the number the new box's first lane gets
    """
    numbers = [
        int(match.group(1))
        for match in (_LANE.match(name) for name in
                      section_names(sorted(cfg_dir.glob("*.cfg")), "AFC_lane"))
        if match
    ]
    return max(numbers, default=0) + 1


def _lane_sort_key(name: str) -> Tuple[int, str]:
    """
    Sort `lane9` before `lane10`; names without a number go last.

    :param name: lane name
    :return Tuple[int, str]: the sort key
    """
    match = _LANE.match(name)
    return (int(match.group(1)), name) if match else (sys.maxsize, name)


def normalize_uid(text: str) -> Optional[str]:
    """
    A box uid as AFC_CFS_STATUS prints it, or None if it isn't one.

    :param text: what the user typed (colons and spaces are allowed)
    :return Optional[str]: 24 lower-case hex digits, or None
    """
    uid = text.replace(":", "").replace(" ", "").lower()
    return uid if _UID.fullmatch(uid) else None


def additional_box_config(name: str, first: CfsBox, box_addr: int, first_lane: int) -> str:
    """
    The config for another box on the first box's chain.

    It shares the first box's serial port, hub and extruder, takes the next free
    box_addr and lane numbers, and gets a placeholder box_uid: discovery then parks the
    new box and logs its real uid, which goes in its place.

    :param name: the new unit's name
    :param first: the chain's first box
    :param box_addr: the new box's address
    :param first_lane: number of the new box's first lane
    :return str: the file's text
    """
    serial = first.options.get("serial", "")
    hub = first.options.get("hub", first.name)
    extruder = first.options.get("extruder", "extruder")
    lanes = "".join(
        f"[AFC_lane lane{first_lane + slot}]\nunit: {name}:{slot + 1}\n" for slot in range(4)
    )
    return (
        f"# Another Creality CFS on the same chain as {first.name}: box 1 OUT -> box 2 IN.\n"
        "# Every box on a chain needs box_uid. Restart Klipper with the new box powered on:\n"
        "# AFC.log names its uid (\"isn't in any [AFC_CFS] section\"). Put it in box_uid\n"
        "# below, restart Klipper, then power-cycle the boxes once.\n"
        "\n"
        f"[AFC_CFS {name}]\n"
        f"serial: {serial}   # same port as {first.name}\n"
        f"box_addr: {box_addr}\n"
        f"box_uid: {UID_PLACEHOLDER}   # replace with this box's uid\n"
        f"hub: {hub}   # the shared 4-in-1 buffer\n"
        f"extruder: {extruder}\n"
        "\n"
        f"{lanes}"
    )


class Cfs(UnitType):
    """
    Creality CFS. Not an MCU: no board or motor type, no pins or steppers file.

    The box feeds until the toolhead sensor triggers, so it always uses a toolhead sensor
    (no ramming), and its 4-in-1 buffer is run by the box itself (no AFC buffer).
    Additional boxes join the first box's RS-485 chain and share its hub, so their file
    is written for the chain instead of being a renamed copy of the template.
    """

    display_name = "Creality CFS"
    key = "CFS"
    additional_name_prefix = "CFS"
    ramming_supported = False
    buffer_selectable = False

    def copy_files(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy the hardware file and the CFS unit file, then write the other boxes' files.

        With more than one box every box needs box_uid, so box 1 gets the placeholder too.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when a destination file already exists
        :param sel: the user's unit choices
        """
        templates = ctx.afc_path / "templates"
        cfg = ctx.afc_config_dir
        safe_copy(ctx, console, prompter, templates / "AFC_Hardware-AFC.cfg",
                  cfg / "AFC_Hardware.cfg")
        safe_copy(ctx, console, prompter, templates / TEMPLATE, cfg / TEMPLATE)
        if sel.cfs_num_boxes < 2:
            return
        first = next((box for box in find_cfs_boxes(cfg) if box.path == cfg / TEMPLATE), None)
        if first is None:
            console.print_msg(
                "WARNING",
                f"No [AFC_CFS] section in {cfg / TEMPLATE}; the other boxes were not set up.",
            )
            return
        if not first.options.get("box_uid"):
            set_key_in_section(first.path, f"[AFC_CFS {first.name}]", "box_uid",
                               UID_PLACEHOLDER)
        for box in range(2, sel.cfs_num_boxes + 1):
            name = f"{self.additional_name_prefix}_{box}"
            text = additional_box_config(name, first, box, 4 * (box - 1) + 1)
            self._write_generated(ctx, console, prompter, cfg / f"AFC_{name}.cfg", text)

    def _write_generated(self, ctx: Context, console: Console, prompter: Prompter,
                         dest: Path, text: str) -> None:
        """
        Write a generated config file, asking first if it exists (like every other unit).

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when the file already exists
        :param dest: the file to write
        :param text: its contents
        """
        with tempfile.TemporaryDirectory() as tmp:
            generated = Path(tmp) / dest.name
            generated.write_text(text)
            safe_copy(ctx, console, prompter, generated, dest)

    def install_additional(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Write the config for another box on the existing chain.

        With no CFS configured yet, the template is copied under the new name instead.
        Every box on a chain needs box_uid, so the first box's uid is asked for if it
        hasn't got one.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used for the first box's uid and an existing destination file
        :param sel: the user's unit choices
        """
        cfg = ctx.afc_config_dir
        name = sel.unit_name
        unit_file = cfg / f"AFC_{name}.cfg"
        boxes = find_cfs_boxes(cfg)
        if not boxes:
            safe_copy(ctx, console, prompter, ctx.afc_path / "templates" / TEMPLATE, unit_file)
            replace_text(unit_file, FIRST_NAME, name)
            return
        used = {_box_addr(box) for box in boxes}
        free = [addr for addr in range(1, MAX_BOXES + 1) if addr not in used]
        if not free:
            console.print_msg(
                "WARNING",
                f"A CFS chain takes at most {MAX_BOXES} boxes; {name} was not added.",
            )
            return
        first = boxes[0]
        if not first.options.get("box_uid"):
            self._ask_first_uid(console, prompter, first)
        text = additional_box_config(name, first, free[0], _next_lane_number(cfg))
        self._write_generated(ctx, console, prompter, unit_file, text)

    def _ask_first_uid(self, console: Console, prompter: Prompter, first: CfsBox) -> None:
        """
        Ask for the first box's uid and write it to its section.

        :param console: where messages go
        :param prompter: where the answer comes from
        :param first: the chain's first box
        """
        console.print_msg(
            "INFO",
            f"Every box on a CFS chain needs box_uid, and {first.name} has none yet. Run "
            f"AFC_CFS_STATUS UNIT={first.name} in the console and copy its box uid.",
        )
        answer = prompter.ask(f"Box uid for {first.name} (press enter to set it later): ")
        uid = normalize_uid(answer or "")
        if uid is None:
            console.print_msg(
                "WARNING",
                f"Set box_uid in [AFC_CFS {first.name}] in {first.path} before restarting "
                "Klipper; it refuses the config while a box on the chain has none.",
            )
            return
        set_key_in_section(first.path, f"[AFC_CFS {first.name}]", "box_uid", uid)

    def buffer_target(self, ctx: Context, sel: UnitSelection) -> BufferTarget:
        """
        The unit's own file and section (used to point it at the chosen extruder).

        :param ctx: installer context
        :param sel: the user's unit choices
        :return BufferTarget: the unit's file and section
        """
        if sel.is_additional_unit:
            name = sel.unit_name
            return BufferTarget(name, "AFC_CFS", ctx.afc_config_dir / f"AFC_{name}.cfg", name)
        return BufferTarget(FIRST_NAME, "AFC_CFS", ctx.afc_config_dir / TEMPLATE, FIRST_NAME)

    def message(self, ctx: Context, sel: UnitSelection) -> str:
        """
        Point the user at the serial port, toolhead settings and cutter switch.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return str: the message
        """
        return (
            f"\n- Set serial in the {ctx.afc_config_dir}/{TEMPLATE} file: run "
            "ls /dev/serial/by-id/ and use the 1a86 device.\n\n"
            "- Set cutter_sensor_pin there if the CFS kit's cutter switch is on a Klipper "
            "pin.\n\n"
            f"- In {ctx.afc_config_dir}/AFC_Hardware.cfg, set tool_stn to the toolhead "
            "sensor-to-nozzle distance and tool_stn_unload to 0.\n  "
        ) + self._uid_steps(sel)

    def _uid_steps(self, sel: UnitSelection) -> str:
        """
        How to fill in the box uids, one box at a time, when more than one box was set up.

        :param sel: the user's unit choices
        :return str: the steps, or an empty string for a single box
        """
        if sel.cfs_num_boxes < 2:
            return ""
        return (
            f"\n- {sel.cfs_num_boxes} boxes were set up (AFC_CFS_1.cfg to "
            f"AFC_CFS_{sel.cfs_num_boxes}.cfg). Each needs its box_uid in place of the "
            "000... placeholder.\n"
            "  Power on only box 1 and restart Klipper: AFC.log names its uid (\"isn't in any "
            "[AFC_CFS] section\"). Put it in AFC_CFS_1.cfg, power on box 2 as well, restart "
            "Klipper, and so on for each box. Power-cycle the boxes once at the end.\n  "
        )

    def additional_message(self, ctx: Context, sel: UnitSelection) -> Optional[str]:
        """
        Say how the new box joined the chain and what is left to do, read back from its file.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return Optional[str]: the text, or None for the generic note (a renamed template
            copy, with no chain yet)
        """
        name = sel.unit_name
        unit_file = ctx.afc_config_dir / f"AFC_{name}.cfg"
        if not unit_file.exists():
            return f"{name} was not added: a CFS chain takes at most {MAX_BOXES} boxes."
        box = next((b for b in find_cfs_boxes(ctx.afc_config_dir)
                    if b.name == name and b.path == unit_file), None)
        if (box is None
            or "box_addr" not in box.options):
            return None
        lanes = sorted(section_names([unit_file], "AFC_lane"), key=_lane_sort_key)
        lane_text = f" with lanes {lanes[0]} to {lanes[-1]}" if lanes else ""
        return (
            f"{name} created: it joins the CFS chain as box_addr {box.options['box_addr']}"
            f"{lane_text}, on the same serial port and hub as the first box.\n"
            f"Restart Klipper with the new box powered on. AFC.log names its uid (\"isn't in "
            f"any [AFC_CFS] section\"): put it in box_uid in {unit_file}, restart Klipper, "
            "then power-cycle the boxes once."
        )

    def install_options(self) -> List[UnitOption]:
        """
        How many boxes are on the chain (each gets its own unit file).

        :return List[UnitOption]: the options
        """
        return [ChoiceOption("CFS Box Count", "cfs_num_boxes", CFS_BOX_OPTIONS, cast=int)]

    def additional_options(self) -> List[UnitOption]:
        """
        The name option of the additional-unit screen.

        :return List[UnitOption]: the options
        """
        return [NameOption("CFS Name", additional_prompt=True)]

    def on_select(self, sel: UnitSelection) -> None:
        """
        CFS always uses a toolhead sensor, and the box runs its own buffer.

        :param sel: the user's unit choices, updated in place
        """
        sel.toolhead_sensor = "Sensor"
        sel.buffer_type = "None"
