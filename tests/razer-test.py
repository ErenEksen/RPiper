#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later

import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

# Before importing rpiper.razer, which reads it at import time.
CONFIG = tempfile.mkdtemp()
os.environ["XDG_CONFIG_HOME"] = CONFIG

from evdev import ecodes  # noqa: E402
from gi.repository import GObject  # noqa: E402

from rpiper import razer  # noqa: E402
from rpiper.ratbagd import (  # noqa: E402
    RatbagDeviceError,
    RatbagdButton,
    RatbagdMacro,
    RatbagdUnavailableError,
)

Action = RatbagdButton.ActionType
Special = RatbagdButton.ActionSpecial

# GET replies measured on 1532:00b9 (protocol sheet, section 9).
STAGES = bytes.fromhex(
    "010205" "0101900190" "0000" "0204b004b0" "0000" "0306400640" "0000"
    "040c800c80" "0000" "0519001900" "0000"
)  # fmt: skip
BUTTONS = {id: bytes([0, id, 0, 0x01, 0x01, id]) for id in razer.BUTTON_IDS}
BUTTONS[0x39] = bytes([0, 0x39, 0, 0x0C, 0x01, 0x00])
BUTTONS[0x60] = bytes([0, 0x60, 0, 0x06, 0x01, 0x06])


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.fail_after = None
        self.buttons = {}  # (slot, id): what a write mapped, else BUTTONS

    def query(self, cls, cid, size, args=()):
        if cid & 0x80:
            reply = {
                (0x00, 0x81): b"\x01\x00",
                (0x04, 0x86): STAGES,
                (0x00, 0x85): b"\x01",
                (0x0F, 0x84): b"\x01\x01\xff",
            }.get((cls, cid)) or self.buttons.get(tuple(args[:2]), BUTTONS[args[1]])
            return reply.ljust(80, b"\0")
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise RatbagDeviceError("fake failure")
        self.sent.append((cls, cid, size, list(args)))
        if (cls, cid) == (0x02, 0x0C):
            self.buttons[tuple(args[:2])] = bytes([0, *args[1:]])
        return bytes(80)


class FakeHidraw:
    """Answers HIDIOCGFEATURE with scripted statuses echoing the last request."""

    def __init__(self, script):
        self.script = list(script)
        self.req = None
        self.sets = 0

    def ioctl(self, fd, request, buf):
        if request == razer.HIDIOCSFEATURE:
            self.req = bytearray(buf[1:])
            self.sets += 1
            return
        kind = self.script.pop(0)
        rep = bytearray(self.req)
        rep[0] = {"busy": 1, "ok": 2, "fail": 3, "timeout": 4, "unsupported": 5}.get(
            kind, 2
        )
        rep[8] = 0x42
        if kind == "other":
            rep[7] ^= 0x01
        rep[88] = razer.crc(rep)
        if kind == "badcrc":
            rep[88] ^= 0xFF
        buf[1:] = rep


class TransportTest(unittest.TestCase):
    def query(self, script):
        hidraw = FakeHidraw(script)
        with mock.patch.object(razer.fcntl, "ioctl", hidraw.ioctl), mock.patch.object(
            razer.time, "sleep"
        ):
            try:
                return razer.Transport(3).query(0x00, 0x85, 0x01), hidraw.sets
            except RatbagDeviceError:
                return None, hidraw.sets

    def test_crc(self):
        self.assertEqual(razer.build(0x04, 0x85, 0x07, [0x01])[88], 0x87)
        # Write B3 of the measured log.
        report = razer.build(0x02, 0x0C, 0x0A, [0x01, 0x60, 0x00, 0x06, 0x01, 0x07])
        b3 = "00 1f 00 00 00 0a 02 0c 01 60 00 06 01 07 00 00 00 00"
        self.assertEqual(report[:18], bytes.fromhex(b3))
        self.assertEqual(report[88], 0x0A ^ 0x02 ^ 0x0C ^ 0x60 ^ 0x06 ^ 0x07)

    def test_ok(self):
        reply, sets = self.query(["ok"])
        self.assertEqual((reply[0], len(reply), sets), (0x42, 80, 1))

    def test_busy_is_polled(self):
        self.assertEqual(self.query(["busy", "busy", "ok"])[1], 1)

    def test_resend(self):
        for bad in ("timeout", "other", "badcrc"):
            reply, sets = self.query([bad, "ok"])
            self.assertEqual((reply[0], sets), (0x42, 2), bad)

    def test_errors(self):
        self.assertEqual(self.query(["fail"]), (None, 1))
        self.assertEqual(self.query(["unsupported"]), (None, 1))
        self.assertEqual(self.query(["timeout"] * 5), (None, 5))
        self.assertEqual(self.query(["busy"] * 50), (None, 5))


class MappingTest(unittest.TestCase):
    def test_round_trip(self):
        macro = [(1, ecodes.KEY_A), (3, 50), (2, ecodes.KEY_A)]
        rows = [
            ((Action.NONE, 0), (0x00, [])),
            ((Action.BUTTON, 1), (0x01, [0x01])),
            ((Action.BUTTON, 5), (0x01, [0x05])),
            ((Action.SPECIAL, Special.WHEEL_UP), (0x01, [0x09])),
            ((Action.SPECIAL, Special.WHEEL_DOWN), (0x01, [0x0A])),
            ((Action.SPECIAL, Special.RESOLUTION_UP), (0x06, [0x01])),
            ((Action.SPECIAL, Special.RESOLUTION_DOWN), (0x06, [0x02])),
            ((Action.SPECIAL, Special.RESOLUTION_CYCLE_UP), (0x06, [0x06])),
            ((Action.SPECIAL, Special.RESOLUTION_CYCLE_DOWN), (0x06, [0x07])),
            ((Action.SPECIAL, Special.DOUBLECLICK), (0x0B, [0x01])),
            ((Action.SPECIAL, Special.SECOND_MODE), (0x0C, [0x00])),
            ((Action.KEY, ecodes.KEY_A), (0x02, [0x00, 0x04])),
            ((Action.KEY, ecodes.KEY_F13), (0x02, [0x00, 0x68])),
            ((Action.KEY, ecodes.KEY_LEFTCTRL), (0x02, [0x01, 0x00])),
            ((Action.KEY, ecodes.KEY_RIGHTMETA), (0x02, [0x80, 0x00])),
            ((Action.KEY, ecodes.KEY_MUTE), (0x0A, [0x00, 0xE2])),
            ((Action.KEY, ecodes.KEY_VOLUMEUP), (0x0A, [0x00, 0xE9])),
            ((Action.KEY, ecodes.KEY_VOLUMEDOWN), (0x0A, [0x00, 0xEA])),
            ((Action.KEY, ecodes.KEY_PLAYPAUSE), (0x0A, [0x00, 0xCD])),
            ((Action.KEY, ecodes.KEY_NEXTSONG), (0x0A, [0x00, 0xB5])),
            ((Action.KEY, ecodes.KEY_PREVIOUSSONG), (0x0A, [0x00, 0xB6])),
            ((Action.KEY, ecodes.KEY_STOPCD), (0x0A, [0x00, 0xB7])),
            ((Action.MACRO, macro), (0x0A, [0x0F, 0xFA])),
        ]
        macros = {3: [list(e) for e in macro]}
        for mapping, block in rows:
            fn, data = razer.encode(*mapping, 3)
            self.assertEqual((fn, data), block, mapping)
            data = bytes(data).ljust(5, b"\0")
            self.assertEqual(razer.decode(fn, data, 3, macros), mapping, mapping)

    def test_command(self):
        command = "notify-send 'a b'"
        fn, data = razer.encode(Action.COMMAND, command, 3)
        self.assertEqual((fn, data), (0x0A, [0x0F, 0xFA]))
        stored = [list(e) for e in razer.host_macro(Action.COMMAND, command)]
        self.assertEqual(stored, [[razer.COMMAND, command]])
        data = bytes(data).ljust(5, b"\0")
        mapping = razer.decode(fn, data, 3, {3: stored})
        self.assertEqual(mapping, (Action.COMMAND, command))

    def test_macro_all_buttons(self):
        macro = [(1, ecodes.KEY_A), (2, ecodes.KEY_A)]
        blocks = set()
        for i in range(len(razer.BUTTON_IDS)):
            fn, data = razer.encode(Action.MACRO, macro, i)
            self.assertEqual(fn, 0x0A)
            self.assertNotIn(data[0] << 8 | data[1], razer.DECODE_CONSUMER)
            blocks.add(bytes(data))
            data = bytes(data).ljust(5, b"\0")
            mapping = razer.decode(fn, data, i, {i: [list(e) for e in macro]})
            self.assertEqual(mapping, (Action.MACRO, macro))
            # Another button's spare usage, or no stored macro, is no macro.
            other = {(i + 1) % 9: [list(e) for e in macro]}
            self.assertEqual(razer.decode(fn, data, i, other), (razer.UNKNOWN, 0))
        self.assertEqual(len(blocks), 9)

    def test_decode_only(self):
        self.assertEqual(
            razer.decode(0x0C, b"\x01\0\0\0\0", 7, {}),
            (Action.SPECIAL, Special.SECOND_MODE),
        )
        # F16 on button 3 without a stored macro is just a key.
        self.assertEqual(
            razer.decode(0x02, b"\x00\x6b\0\0\0", 3, {}), (Action.KEY, ecodes.KEY_F16)
        )
        for fn, data in [(0x07, b"\x01"), (0x03, b"\x00\x01\x01"), (0x02, b"\x01\x04")]:
            self.assertEqual(
                razer.decode(fn, data.ljust(5, b"\0"), 0, {}), (razer.UNKNOWN, 0)
            )

    def test_unsupported(self):
        for mapping in [
            (Action.BUTTON, 6),
            (Action.SPECIAL, Special.PROFILE_CYCLE_UP),
            (Action.SPECIAL, Special.WHEEL_LEFT),
            (razer.UNKNOWN, 0),
        ]:
            with self.assertRaises(ValueError):
                razer.encode(*mapping, 0)
            self.assertIsNone(razer.host_macro(*mapping))
        with self.assertRaises(ValueError):
            razer.dpi_bytes(18050)

    def test_key_without_encoding(self):
        key = ecodes.KEY_BRIGHTNESSUP
        self.assertEqual(razer.encode(Action.KEY, key, 2), (0x0A, [0x0F, 0xF9]))
        self.assertEqual(razer.host_macro(Action.KEY, key), [(1, key), (2, key)])
        self.assertIsNone(razer.host_macro(Action.KEY, ecodes.KEY_A))
        self.assertIsNone(razer.host_macro(Action.KEY, ecodes.KEY_MUTE))

    def test_hid_table(self):
        self.assertEqual(razer.KEYCODE[0x04], ecodes.KEY_A)
        self.assertEqual(razer.USAGE[ecodes.KEY_A], 0x04)
        self.assertEqual(razer.KEYCODE[0x68], ecodes.KEY_F13)
        self.assertEqual(razer.USAGE[ecodes.KEY_F24], 0x73)
        self.assertEqual(razer.KEYCODE[0xE0], ecodes.KEY_LEFTCTRL)
        self.assertEqual(razer.USAGE[ecodes.KEY_LEFTCTRL], 0xE0)
        self.assertEqual(razer.USAGE[ecodes.KEY_DELETE], 0x4C)
        self.assertEqual(razer.KEYCODE[0x28], ecodes.KEY_ENTER)
        self.assertEqual(razer.KEYCODE[0x52], ecodes.KEY_UP)


class DeviceTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(os.path.join(CONFIG, "rpiper"), ignore_errors=True)
        self.t = FakeTransport()
        self.device = razer.RazerDevice(self.t)
        self.profile = self.device.profiles[0]
        self.resyncs = []
        self.device.connect("resync", lambda d: self.resyncs.append(d))
        patch = mock.patch.object(razer.subprocess, "Popen")
        self.popen = patch.start()
        self.addCleanup(patch.stop)

    def test_load(self):
        d, p = self.device, self.profile
        self.assertEqual(
            (d.name, d.model, d.firmware_version),
            ("Razer Basilisk V3 X HyperSpeed", "usb:1532:00b9:0", "1.0"),
        )
        self.assertIs(d.device_type, razer.RatbagDeviceType.MOUSE)
        self.assertIs(d.active_profile, p)
        self.assertEqual((len(p.buttons), len(p.resolutions), len(p.leds)), (9, 5, 1))
        self.assertEqual(
            [r.resolution for r in p.resolutions],
            [(400,), (1200,), (1600,), (3200,), (6400,)],
        )
        self.assertEqual([r.is_active for r in p.resolutions], [0, 1, 0, 0, 0])
        self.assertEqual(
            (p.report_rate, p.report_rates, p.angle_snapping),
            (1000, [125, 500, 1000], -1),
        )
        self.assertEqual(p.buttons[0].mapping, 1)
        self.assertEqual(p.buttons[5].special, Special.WHEEL_UP)
        self.assertEqual(p.buttons[7].special, Special.SECOND_MODE)
        self.assertEqual(p.buttons[8].special, Special.RESOLUTION_CYCLE_UP)
        self.assertEqual(
            (p.leds[0].brightness, p.leds[0].mode), (255, razer.Mode.CYCLE)
        )
        self.assertFalse(p.dirty)

    def test_commit(self):
        p = self.profile
        p.buttons[7].key = ecodes.KEY_F13
        p.resolutions[0].resolution = (800,)
        p.resolutions[4].set_disabled(True)
        p.resolutions[2].set_active()
        p.report_rate = 500
        self.assertTrue(p.dirty)
        self.assertEqual([r.is_active for r in p.resolutions], [0, 0, 1, 0, 0])
        self.device.commit()
        stages = [0x01, 0x03, 0x04]
        for i, dpi in enumerate([800, 1200, 1600, 3200]):
            stages += [i + 1, *dpi.to_bytes(2, "big") * 2, 0, 0]
        self.assertEqual(
            self.t.sent,
            [
                (0x02, 0x0C, 0x0A, [0x01, 0x39, 0x00, 0x02, 0x02, 0x00, 0x68]),
                (0x02, 0x0C, 0x0A, [0x00, 0x39, 0x00, 0x02, 0x02, 0x00, 0x68]),
                (0x04, 0x06, 0x26, stages),
                (0x04, 0x05, 0x07, [0x01, 0x06, 0x40, 0x06, 0x40]),
                (0x00, 0x05, 0x01, [0x02]),
            ],
        )
        self.assertFalse(p.dirty)
        self.assertEqual(self.resyncs, [])
        self.t.sent.clear()
        self.device.commit()
        self.assertEqual(self.t.sent, [])
        self.popen.assert_not_called()

    def test_failure_and_retry(self):
        p = self.profile
        p.buttons[3].disable()
        p.buttons[4].special = Special.DOUBLECLICK
        p.leds[0].brightness = 128
        self.t.fail_after = 3  # the second button's live slot fails
        self.device.commit()
        self.assertEqual(len(self.resyncs), 1)
        self.assertTrue(p.dirty)
        self.assertEqual(p.buttons[4].special, Special.DOUBLECLICK)
        self.t.sent.clear()
        self.t.fail_after = None
        self.device.commit()
        self.assertEqual(
            self.t.sent,
            [
                (0x02, 0x0C, 0x0A, [0x01, 0x05, 0x00, 0x0B, 0x01, 0x01]),
                (0x02, 0x0C, 0x0A, [0x00, 0x05, 0x00, 0x0B, 0x01, 0x01]),
                (0x0F, 0x04, 0x03, [0x01, 0x01, 128]),
            ],
        )
        self.assertFalse(p.dirty)

    def test_unsupported_mapping(self):
        p = self.profile
        p.buttons[2].special = Special.PROFILE_UP
        p.buttons[3].special = Special.BATTERY_LEVEL
        p.buttons[4].disable()
        p.report_rate = 125
        self.device.commit()
        # Only the unsupported buttons are reverted, the rest is applied.
        self.assertEqual((p.buttons[2].mapping, p.buttons[3].mapping), (3, 4))
        self.assertEqual(
            self.t.sent,
            [
                (0x02, 0x0C, 0x0A, [0x01, 0x05, 0x00, 0x00, 0x00]),
                (0x02, 0x0C, 0x0A, [0x00, 0x05, 0x00, 0x00, 0x00]),
                (0x00, 0x05, 0x01, [0x08]),
            ],
        )
        self.assertEqual(len(self.resyncs), 1)
        self.assertFalse(p.dirty)

    def test_key_without_encoding_uses_macro_file(self):
        self.profile.buttons[3].key = ecodes.KEY_BRIGHTNESSUP
        self.device.commit()
        self.assertEqual(
            self.t.sent[0], (0x02, 0x0C, 0x0A, [1, 4, 0, 0x0A, 2, 0x0F, 0xFA])
        )
        key = ecodes.KEY_BRIGHTNESSUP
        self.assertEqual(razer.load_macros(), {3: [[1, key], [2, key]]})
        self.assertEqual(self.resyncs, [])
        self.popen.assert_called_once()
        self.assertEqual(
            self.popen.call_args.args[0],
            ["systemctl", "--user", "enable", "--now", "rpiper-razer-macros.service"],
        )

    def test_file_error_blocks_device_write(self):
        p = self.profile
        p.buttons[3].macro = RatbagdMacro.from_ratbag([(1, ecodes.KEY_C)])
        p.leds[0].mode = razer.Mode.OFF
        with mock.patch.object(razer, "write_json", side_effect=OSError("full")):
            self.device.commit()
        self.assertEqual((self.t.sent, len(self.resyncs)), ([], 1))
        self.assertTrue(p.dirty)
        p.buttons[3]._saved = p.buttons[3]._props  # only the LED is left
        with mock.patch.object(razer, "write_json", side_effect=OSError("full")):
            self.device.commit()
        self.assertEqual((self.t.sent, len(self.resyncs)), ([], 2))
        self.device.commit()
        self.assertEqual(self.t.sent, [(0x0F, 0x02, 0x06, [1, 1, 0])])
        self.assertFalse(p.dirty)

    def test_macro_and_led_files(self):
        p = self.profile
        macro = RatbagdMacro.from_ratbag([(1, ecodes.KEY_B), (2, ecodes.KEY_B)])
        p.buttons[6].macro = macro
        led = p.leds[0]
        led.mode = razer.Mode.ON
        led.color = (1, 2, 3)
        self.device.commit()
        self.assertEqual(
            self.t.sent[0], (0x02, 0x0C, 0x0A, [1, 0x0A, 0, 0x0A, 0x02, 0x0F, 0xFD])
        )
        self.assertEqual(
            self.t.sent[-1], (0x0F, 0x02, 0x09, [1, 1, 1, 0, 0, 1, 1, 2, 3])
        )
        with open(razer.MACRO_FILE) as f:
            self.assertEqual(
                json.load(f),
                {"6": [[1, ecodes.KEY_B], [2, ecodes.KEY_B]]},
            )
        # A fresh load shows the macro and the LED state again.
        BUTTONS[0x0A] = bytes([0, 0x0A, 0, 0x0A, 0x02, 0x0F, 0xFD])
        try:
            p2 = razer.RazerDevice(FakeTransport()).profiles[0]
        finally:
            BUTTONS[0x0A] = bytes([0, 0x0A, 0, 0x01, 0x01, 0x0A])
        self.assertEqual(
            p2.buttons[6].macro.keys, [(1, ecodes.KEY_B), (2, ecodes.KEY_B)]
        )
        self.assertEqual(
            (p2.leds[0].mode, p2.leds[0].color), (razer.Mode.ON, (1, 2, 3))
        )
        # Mapping the button to something else drops its macro.
        self.assertEqual(self.popen.call_count, 1)
        p.buttons[6].special = Special.WHEEL_DOWN
        self.device.commit()
        with open(razer.MACRO_FILE) as f:
            self.assertEqual(json.load(f), {})
        self.assertEqual(self.popen.call_count, 1)

    def test_reassert(self):
        macro = [(1, ecodes.KEY_LEFTMETA), (1, ecodes.KEY_W), (2, ecodes.KEY_W)]
        razer.store_macro(8, macro)
        t = FakeTransport()  # the mouse dropped button 8 back to DPI cycle
        device = razer.RazerDevice(t)
        p = device.profiles[0]
        spare = [0x60, 0x00, 0x0A, 0x02, 0x0F, 0xFF]
        self.assertEqual(
            t.sent, [(0x02, 0x0C, 0x0A, [1, *spare]), (0x02, 0x0C, 0x0A, [0, *spare])]
        )
        self.assertEqual(p.buttons[8].macro.keys, macro)
        t.sent.clear()
        razer.reassert(t.query)  # intact: reads only
        self.assertEqual(t.sent, [])
        # Mapping it away in the UI drops the macro, so it is not rebound.
        p.buttons[8].special = Special.RESOLUTION_CYCLE_UP
        device.commit()
        self.assertEqual(razer.load_macros(), {})
        t.sent.clear()
        razer.reassert(t.query)
        self.assertEqual(t.sent, [])

    def test_command_button(self):
        p = self.profile
        p.buttons[8].command = "true"
        self.device.commit()
        self.assertEqual(razer.load_macros(), {8: [[razer.COMMAND, "true"]]})
        self.popen.assert_called_once()  # the macro service plays commands too
        t = FakeTransport()
        t.buttons.update(self.t.buttons)  # what the commit mapped
        device = razer.RazerDevice(t)
        self.assertEqual(t.sent, [])  # bound already, so reassert writes nothing
        self.assertEqual(device.profiles[0].buttons[8].command, "true")
        t.buttons.clear()  # the mouse dropped it: rebound like a macro
        razer.reassert(t.query)
        spare = [0x60, 0x00, 0x0A, 0x02, 0x0F, 0xFF]
        self.assertEqual(t.sent[-1], (0x02, 0x0C, 0x0A, [0, *spare]))
        p.buttons[8].special = Special.RESOLUTION_CYCLE_UP
        self.device.commit()
        self.assertEqual(razer.load_macros(), {})

    def test_reassert_waits_for_apply(self):
        razer.store_macro(8, [(1, ecodes.KEY_A), (2, ecodes.KEY_A)])
        t = FakeTransport()
        device = razer.RazerDevice(t)
        device.profiles[0].buttons[8].special = Special.RESOLUTION_CYCLE_UP
        t.buttons.clear()  # the mouse dropped button 8 again
        gui = threading.Thread(target=device.commit)

        def service_query(cls, cid, size, args=()):
            if not gui.is_alive() and gui.ident is None:
                gui.start()  # Apply right after the service read the file
                gui.join(0.2)
            return t.query(cls, cid, size, args)

        razer.reassert(service_query)
        gui.join()
        self.assertEqual(razer.load_macros(), {})
        dpi_cycle = bytes([0, 0x60, 0, 0x06, 0x01, 0x06])
        self.assertEqual([t.buttons[(s, 0x60)] for s in (1, 0)], [dpi_cycle] * 2)

    def test_service_start_failure_ignored(self):
        self.popen.side_effect = FileNotFoundError("systemctl")
        self.profile.buttons[3].key = ecodes.KEY_BRIGHTNESSUP
        self.device.commit()
        self.assertFalse(self.profile.dirty)
        self.assertEqual(self.resyncs, [])


class RazerRatbagdTest(unittest.TestCase):
    def test_without_ratbagd(self):
        device = razer.RazerDevice(FakeTransport())
        unavailable = mock.Mock(side_effect=RatbagdUnavailableError("no daemon"))
        with mock.patch.object(razer, "Ratbagd", unavailable):
            with mock.patch.object(razer, "razer_devices", return_value=[device]):
                self.assertEqual(razer.RazerRatbagd(2).devices, [device])
            with mock.patch.object(razer, "razer_devices", return_value=[]):
                with self.assertRaises(RatbagdUnavailableError):
                    razer.RazerRatbagd(2)

    def test_with_ratbagd(self):
        class FakeRatbagd(GObject.GObject):
            __gsignals__ = {
                "device-added": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
                "device-removed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
                "daemon-disappeared": (GObject.SignalFlags.RUN_FIRST, None, ()),
            }
            devices = GObject.Property(type=object)

        ratbagd = FakeRatbagd()
        ratbagd.devices = ["other mouse"]
        device = razer.RazerDevice(FakeTransport())
        with mock.patch.object(razer, "Ratbagd", return_value=ratbagd):
            with mock.patch.object(razer, "razer_devices", return_value=[device]):
                manager = razer.RazerRatbagd(2)
        self.assertEqual(manager.devices, ["other mouse", device])
        seen = []
        manager.connect("device-added", lambda m, d: seen.append(("added", d)))
        manager.connect("daemon-disappeared", lambda m: seen.append("gone"))
        manager.connect("notify::devices", lambda m, p: seen.append("notify"))
        ratbagd.emit("device-added", "new")
        ratbagd.emit("daemon-disappeared")
        ratbagd.devices = ["new"]
        self.assertEqual(seen, [("added", "new"), "gone", "notify"])


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        shutil.rmtree(CONFIG, ignore_errors=True)
