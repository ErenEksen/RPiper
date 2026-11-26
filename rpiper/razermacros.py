# SPDX-License-Identifier: GPL-2.0-or-later

"""Replays macros for a Razer Basilisk V3 X HyperSpeed. A button with a macro
is mapped on the device to the consumer usage spare_usage(button index); this
service reads that press from hidraw and plays the macro through uinput.

hidraw, not evdev: other programs may hold exclusive grabs on the mouse's
evdev nodes, which hides their events from every other reader."""

import glob
import json
import os
import select
import subprocess
import sys
import time

from evdev import UInput, UInputError, ecodes

MACRO_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "rpiper",
    "razer-macros.json",
)
# Above the logical maximum (0x023c) of the mouse's consumer report, so the
# kernel drops the usage and no input event reaches applications, while hidraw
# still carries the raw report. 0x0fff (index 8) is the one measured on the
# mouse; 0x0ff7-0x0ffe rely on the same range check.
SPARE_BASE = 0x0FF7
BUTTONS = 9
COMMAND = 4  # macro entry type of a shell command
CONSUMER_INTERFACE = 1
CONSUMER_REPORT = 0x02  # [0x02, usage lo, usage hi, 13 padding bytes]
# shortcut: polls the mouse, so a mapping it dropped while the receiver stayed
# plugged in comes back up to a minute late; use a receiver "mouse connected"
# notification instead if one is found in the protocol
RECHECK_SECONDS = 60


def spare_usage(index):
    return SPARE_BASE + index


def spare_index(usage):
    """Returns the button index of a spare usage, else None."""
    index = usage - SPARE_BASE
    return index if 0 <= index < BUTTONS else None


def find_hidraw(interface):
    """Returns the hidraw node of the mouse's USB interface, or None."""
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        dev = os.path.realpath(node + "/device")
        name, parent = os.path.basename(dev), os.path.dirname(dev)
        if name.startswith("0003:1532:00B9.") and parent.endswith(f":1.{interface}"):
            return "/dev/" + os.path.basename(node)
    return None


def load_macros():
    """Returns {button index: [[type, value], ...]}; {} if the file is unusable.
    Types: 1 = key press, 2 = key release, 3 = wait in ms, 4 = shell command."""
    try:
        with open(MACRO_FILE) as f:
            return {int(k): v for k, v in json.load(f).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def run(command):
    """Starts a shell command without waiting for it. A transient user unit, not
    a child: systemd reaps it, gives it the session environment, and it is not
    killed with this service's cgroup on a restart."""
    shell = os.environ.get("SHELL") or "/bin/sh"
    cmd = ["systemd-run", "--user", "--no-block", "--collect", "--quiet"]
    try:
        # systemd-run only queues the unit, so this returns without the command.
        subprocess.run([*cmd, "--", shell, "-c", command], timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"command {command!r} not started: {e}", file=sys.stderr)


def play(ui, macro):
    for type, value in macro:
        if type == 3:
            time.sleep(value / 1000)
        elif type == COMMAND:
            run(value)
        else:
            ui.write(ecodes.EV_KEY, value, 1 if type == 1 else 0)
            ui.syn()


def handle(ui, report, held):
    """Plays the macro of a newly pressed spare usage; returns the usage held
    now, so a repeated report of a held button does not play again."""
    if len(report) < 3 or report[0] != CONSUMER_REPORT:
        return held  # keyboard or pointer report of the same interface
    usage = report[1] | report[2] << 8
    index = spare_index(usage)
    if usage != held and index is not None:
        macro = load_macros().get(index)  # the file may have changed since the start
        if macro:
            play(ui, macro)
    return usage


def main():
    from .razer import RatbagDeviceError, Transport, reassert  # razer imports us

    path, control = find_hidraw(CONSUMER_INTERFACE), find_hidraw(0)
    if path is None or control is None:
        print("Razer Basilisk V3 X HyperSpeed not found", file=sys.stderr)
        return 1
    try:
        fd = os.open(path, os.O_RDONLY)
        q = Transport(os.open(control, os.O_RDWR)).query
        keys = [c for c in ecodes.KEY if 0 < c <= ecodes.KEY_MAX]
        ui = UInput({ecodes.EV_KEY: keys}, name="RPiper Razer macros")
        held, ready = 0, []
        while True:
            if not ready:  # at start, then after each quiet minute
                try:
                    reassert(q)
                except RatbagDeviceError:
                    pass  # mouse asleep or off: the next check retries
            ready = select.select([fd], [], [], RECHECK_SECONDS)[0]
            if ready:
                if not (report := os.read(fd, 64)):
                    break
                held = handle(ui, report, held)
        print(f"{path}: device gone", file=sys.stderr)
    except OSError as e:
        print(f"{e.filename or path}: {e.strerror}", file=sys.stderr)
    except UInputError as e:
        print(f"uinput: {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
