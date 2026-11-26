#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later

import io
import json
import os
import tempfile
import unittest
from unittest import mock

from evdev import ecodes

from rpiper import razer, razermacros
from rpiper.ratbagd import RatbagDeviceError

# Raw hidraw reports measured on 1532:00b9 with a button bound to consumer
# usage 0x0fff (interface 1), and to keyboard usage 0x68 (interface 2).
PRESS = bytes.fromhex("02 ff 0f 00 00 00 00 00 00 00 00 00 00 00 00 00")
RELEASE = bytes.fromhex("02 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00")
BOOT_KEY = bytes.fromhex("00 00 68 00 00 00 00 00")
# Report id 1 (keyboard) and id 0x0a (pointer, X/Y at 5..8) of interface 1,
# laid out after its report descriptor.
KEY = bytes.fromhex("01 00 04 00 00 00 00 00 00 00 00 00 00 00 00 00")
MOTION = bytes.fromhex("0a 00 00 00 00 03 00 01 00")
VOLUME_UP = bytes.fromhex("02 e9 00 00 00 00 00 00 00 00 00 00 00 00 00 00")


class FakeUInput:
    def __init__(self):
        self.calls = []

    def write(self, type, code, value):
        self.calls.append(("write", type, code, value))

    def syn(self):
        self.calls.append(("syn",))


class RazerMacrosTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = os.path.join(d.name, "m.json")
        patch = mock.patch.object(razermacros, "MACRO_FILE", self.path)
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, macros):
        with open(self.path, "w") as f:
            json.dump(macros, f)

    def feed(self, reports):
        ui = FakeUInput()
        held = 0
        for report in reports:
            held = razermacros.handle(ui, report, held)
        return ui.calls

    def test_load_missing(self):
        self.assertEqual(razermacros.load_macros(), {})

    def test_load_invalid(self):
        with open(self.path, "w") as f:
            f.write("{")
        self.assertEqual(razermacros.load_macros(), {})

    def test_load(self):
        self.write({"8": [[1, 30], [3, 10], [2, 30]]})
        self.assertEqual(razermacros.load_macros(), {8: [[1, 30], [3, 10], [2, 30]]})

    def test_play(self):
        ui = FakeUInput()
        with mock.patch("time.sleep") as sleep:
            razermacros.play(ui, [[1, 30], [3, 250], [2, 30]])
        sleep.assert_called_once_with(0.25)
        K = ecodes.EV_KEY
        self.assertEqual(
            ui.calls,
            [("write", K, 30, 1), ("syn",), ("write", K, 30, 0), ("syn",)],
        )

    def test_spare_usage(self):
        usages = [razermacros.spare_usage(i) for i in range(9)]
        self.assertEqual(usages[8], 0x0FFF)
        self.assertEqual(len(set(usages)), 9)
        # Above the logical maximum of the consumer report.
        self.assertGreater(min(usages), 0x023C)
        self.assertEqual([razermacros.spare_index(u) for u in usages], list(range(9)))
        for usage in (0, 0xE9, 0x023C, 0x0FF6, 0x1000):
            self.assertIsNone(razermacros.spare_index(usage))

    def test_press_plays_once(self):
        self.write({"8": [[1, 30], [2, 30]], "0": [[1, 31], [2, 31]]})
        K = ecodes.EV_KEY
        once = [("write", K, 30, 1), ("syn",), ("write", K, 30, 0), ("syn",)]
        self.assertEqual(self.feed([PRESS]), once)
        self.assertEqual(self.feed([PRESS, PRESS, MOTION, PRESS, RELEASE]), once)
        self.assertEqual(self.feed([PRESS, RELEASE, PRESS, RELEASE]), once * 2)

    def test_other_reports(self):
        self.write({"8": [[1, 30], [2, 30]]})
        reports = [RELEASE, BOOT_KEY, KEY, MOTION, VOLUME_UP, RELEASE, b"", b"\x02"]
        self.assertEqual(self.feed(reports), [])

    def test_press_without_macro(self):
        self.write({"0": [[1, 30], [2, 30]]})
        self.assertEqual(self.feed([PRESS, RELEASE]), [])

    def test_macro_added_after_start(self):
        ui = FakeUInput()
        self.assertEqual(razermacros.handle(ui, RELEASE, 0), 0)
        self.write({"8": [[1, 30], [2, 30]]})
        self.assertEqual(razermacros.handle(ui, PRESS, 0), 0x0FFF)
        self.assertEqual(ui.calls[0], ("write", ecodes.EV_KEY, 30, 1))

    def test_no_device(self):
        with mock.patch.object(razermacros, "find_hidraw", return_value=None):
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(razermacros.main(), 1)
        self.assertEqual(err.getvalue().count("\n"), 1)

    def test_open_error(self):
        patch = mock.patch.object
        with patch(razermacros, "find_hidraw", return_value="/dev/hidraw99"):
            with patch(os, "open", side_effect=PermissionError(13, "denied")):
                with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                    self.assertEqual(razermacros.main(), 1)
        self.assertEqual(err.getvalue(), "/dev/hidraw99: denied\n")

    def test_unplug(self):
        self.write({"8": [[1, 30], [2, 30]]})
        ui = FakeUInput()
        reads = [PRESS, RELEASE, OSError(19, "No such device")]
        # Quiet, a report, quiet (mouse asleep), then two reports.
        selects = [[], [99], [], [99], [99]]
        asleep = RatbagDeviceError("asleep")
        patch = mock.patch.object
        with patch(razermacros, "find_hidraw", return_value="/dev/hidraw13"):
            with patch(os, "open", return_value=99), patch(os, "read") as read:
                read.side_effect = reads
                with patch(razermacros.select, "select") as select, patch(
                    razer, "reassert", side_effect=[None, None, asleep, None]
                ) as reassert:
                    select.side_effect = [(s, [], []) for s in selects]
                    with patch(razermacros, "UInput", return_value=ui) as uinput:
                        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                            self.assertEqual(razermacros.main(), 1)
        self.assertEqual(err.getvalue(), "/dev/hidraw13: No such device\n")
        # At start and after each quiet wait; a sleeping mouse is no error.
        self.assertEqual(reassert.call_count, 3)
        self.assertEqual(len(ui.calls), 4)
        keys = uinput.call_args.args[0][ecodes.EV_KEY]
        self.assertIn(ecodes.KEY_A, keys)
        self.assertNotIn(ecodes.BTN_LEFT, keys)


if __name__ == "__main__":
    unittest.main()
