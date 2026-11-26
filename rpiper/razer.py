# SPDX-License-Identifier: GPL-2.0-or-later

"""Talks to a Razer Basilisk V3 X HyperSpeed (1532:00b9) directly over hidraw
and presents it through the Ratbagd* classes, so the UI needs no changes."""

import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time

from evdev import ecodes
from gi.repository import GLib, GObject

from .ratbagd import (
    Ratbagd,
    RatbagdButton,
    RatbagdDevice,
    RatbagdIncompatibleError,
    RatbagdLed,
    RatbagdProfile,
    RatbagdResolution,
    RatbagdUnavailableError,
    RatbagDeviceError,
    RatbagDeviceType,
    RatbagError,
)
from .razermacros import COMMAND, MACRO_FILE, find_hidraw, load_macros, spare_usage

LED_FILE = os.path.join(os.path.dirname(MACRO_FILE), "razer-led.json")

LEN = 90
TID = 0x1F
HIDIOCSFEATURE = (3 << 30) | ((LEN + 1) << 16) | (ord("H") << 8) | 0x06
HIDIOCGFEATURE = (3 << 30) | ((LEN + 1) << 16) | (ord("H") << 8) | 0x07
BUSY, OK, FAIL, TIMEOUT, UNSUPPORTED = 1, 2, 3, 4, 5

# Piper button index -> device button id.
BUTTON_IDS = [0x01, 0x02, 0x03, 0x04, 0x05, 0x09, 0x0A, 0x39, 0x60]
RATES = {0x01: 1000, 0x02: 500, 0x08: 125}
RATE_CODES = {rate: code for code, rate in RATES.items()}

Action = RatbagdButton.ActionType
Special = RatbagdButton.ActionSpecial
Mode = RatbagdLed.Mode
UNKNOWN = 1000  # no ActionType, so Piper labels the button "Unknown"

SPECIALS = {
    Special.WHEEL_UP: (0x01, [0x09]),
    Special.WHEEL_DOWN: (0x01, [0x0A]),
    Special.RESOLUTION_UP: (0x06, [0x01]),
    Special.RESOLUTION_DOWN: (0x06, [0x02]),
    Special.RESOLUTION_CYCLE_UP: (0x06, [0x06]),
    Special.RESOLUTION_CYCLE_DOWN: (0x06, [0x07]),
    Special.DOUBLECLICK: (0x0B, [0x01]),
    # HyperShift; this mouse stores data 00, other models 01.
    Special.SECOND_MODE: (0x0C, [0x00]),
}
DECODE_SPECIAL = {(fn, data[0]): s for s, (fn, data) in SPECIALS.items()}
DECODE_SPECIAL[(0x0C, 0x01)] = Special.SECOND_MODE

CONSUMER = {
    ecodes.KEY_MUTE: 0xE2,
    ecodes.KEY_VOLUMEUP: 0xE9,
    ecodes.KEY_VOLUMEDOWN: 0xEA,
    ecodes.KEY_PLAYPAUSE: 0xCD,
    ecodes.KEY_NEXTSONG: 0xB5,
    ecodes.KEY_PREVIOUSSONG: 0xB6,
    ecodes.KEY_STOPCD: 0xB7,
}
DECODE_CONSUMER = {usage: code for code, usage in CONSUMER.items()}

# HID keyboard usage -> evdev code, usages 0x00-0x9f of the kernel's
# hid_keyboard[] table (drivers/hid/hid-input.c), 0 = unmapped.
HID_KEYBOARD = [
    0, 0, 0, 0, 30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38,
    50, 49, 24, 25, 16, 19, 31, 20, 22, 47, 17, 45, 21, 44, 2, 3,
    4, 5, 6, 7, 8, 9, 10, 11, 28, 1, 14, 15, 57, 12, 13, 26,
    27, 43, 43, 39, 40, 41, 51, 52, 53, 58, 59, 60, 61, 62, 63, 64,
    65, 66, 67, 68, 87, 88, 99, 70, 119, 110, 102, 104, 111, 107, 109, 106,
    105, 108, 103, 69, 98, 55, 74, 78, 96, 79, 80, 81, 75, 76, 77, 71,
    72, 73, 82, 83, 86, 127, 116, 117, 183, 184, 185, 186, 187, 188, 189, 190,
    191, 192, 193, 194, 134, 138, 130, 132, 128, 129, 131, 137, 133, 135, 136, 113,
    115, 114, 0, 0, 0, 121, 0, 89, 93, 124, 92, 94, 95, 0, 0, 0,
    122, 123, 90, 91, 85, 0, 0, 0, 0, 0, 0, 0, 111, 0, 0, 0,
]  # fmt: skip
KEYCODE = {usage: code for usage, code in enumerate(HID_KEYBOARD) if code}
# Modifiers 0xe0-0xe7: LeftCtrl LeftShift LeftAlt LeftMeta, then the right ones.
KEYCODE.update(zip(range(0xE0, 0xE8), [29, 42, 56, 125, 97, 54, 100, 126]))
USAGE = {}
for usage, code in sorted(KEYCODE.items()):
    USAGE.setdefault(code, usage)  # the first usage wins, e.g. 0x4c for Delete


def crc(report):
    c = 0
    for b in report[2:88]:
        c ^= b
    return c


def build(cls, cid, size, args=()):
    report = bytearray(LEN)
    report[1] = TID
    report[5] = size
    report[6] = cls
    report[7] = cid
    report[8 : 8 + len(args)] = bytes(args)
    report[88] = crc(report)
    return report


class Transport:
    def __init__(self, fd):
        self.fd = fd

    def query(self, cls, cid, size, args=()):
        """Sends one report and returns the 80 argument bytes of the reply."""
        req = build(cls, cid, size, args)
        for _ in range(5):
            fcntl.ioctl(self.fd, HIDIOCSFEATURE, bytes(1) + req)
            for _ in range(10):
                time.sleep(0.03)
                buf = bytearray(LEN + 1)
                fcntl.ioctl(self.fd, HIDIOCGFEATURE, buf)
                rep = buf[1:]
                if rep[0] not in (0, BUSY):
                    break
            else:
                continue
            if (rep[1], rep[6], rep[7]) != (TID, cls, cid) or rep[88] != crc(rep):
                continue  # someone else's reply or a corrupt one: resend
            if rep[0] == OK:
                return bytes(rep[8:88])
            if rep[0] != TIMEOUT:  # the receiver says timeout while the mouse sleeps
                raise RatbagDeviceError(f"{cls:02x}/{cid:02x} failed, status {rep[0]}")
        raise RatbagDeviceError(f"{cls:02x}/{cid:02x}: no answer, mouse asleep or off")


def encode(action_type, value, index):
    """Returns the device function (class, data) of a Piper mapping."""
    if action_type == Action.NONE:
        return 0x00, []
    if action_type == Action.BUTTON and 1 <= value <= 5:
        return 0x01, [value]
    if action_type == Action.SPECIAL and value in SPECIALS:
        return SPECIALS[value]
    if action_type == Action.KEY and value in CONSUMER:
        return 0x0A, [CONSUMER[value] >> 8, CONSUMER[value] & 0xFF]
    if action_type == Action.KEY and value in USAGE:
        usage = USAGE[value]
        if usage >= 0xE0:  # a lone modifier goes into the mask byte
            return 0x02, [1 << (usage - 0xE0), 0x00]
        return 0x02, [0x00, usage]
    if action_type in (Action.KEY, Action.MACRO, Action.COMMAND):
        # No macro memory on the mouse: it sends a spare consumer usage and
        # razermacros plays host_macro() stored under the button index.
        return 0x0A, list(spare_usage(index).to_bytes(2, "big"))
    raise ValueError(f"button {index}: unsupported mapping {action_type} {value}")


def host_macro(action_type, value):
    """Returns the sequence razermacros plays for a mapping that encode() binds
    to spare_usage(index), or None if the mouse handles the mapping itself."""
    if action_type == Action.MACRO:
        return value
    if action_type == Action.COMMAND:
        return [(COMMAND, value)]
    if action_type == Action.KEY and value not in CONSUMER and value not in USAGE:
        return [(1, value), (2, value)]
    return None


def store_macro(index, macro):
    """Stores the macro of a button, or removes it if macro is None."""
    macros = load_macros()
    if macro is not None:
        macros[index] = [list(e) for e in macro]
    elif macros.pop(index, None) is None:
        return
    write_json(MACRO_FILE, macros)


@contextlib.contextmanager
def macro_lock():
    """Holds the macro file and the button writes that follow it still against
    the other process (RPiper or the macro service) until the block ends."""
    path = os.path.dirname(MACRO_FILE)
    os.makedirs(path, exist_ok=True)
    fd = os.open(path, os.O_RDONLY)  # the file itself is replaced on each write
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def reassert(q):
    """Rebinds each button with a stored macro that the mouse no longer maps to
    its spare usage (it dropped the mapping); reads first, so an intact button
    costs no write."""
    with macro_lock():  # else an Apply in between would be undone
        macros = load_macros()
        for index in macros.keys() & range(len(BUTTON_IDS)):
            fn, data = encode(Action.MACRO, macros[index], index)
            for slot in (0x01, 0x00):  # the stored profile, then the live one
                args = [slot, BUTTON_IDS[index], 0x00]
                a = q(0x02, 0x8C, 0x0A, args)
                if a[3] != fn or list(a[5 : 5 + len(data)]) != data:
                    q(0x02, 0x0C, 0x0A, [*args, fn, len(data), *data])


def decode(fn, data, index, macros):
    """Returns the Piper mapping (action type, value) of a device function."""
    mods, usage = data[0], data[1]
    macro = macros.get(index)
    if fn == 0x00:
        return Action.NONE, 0
    if fn == 0x01 and 1 <= data[0] <= 5:
        return Action.BUTTON, data[0]
    if (fn, data[0]) in DECODE_SPECIAL:
        return Action.SPECIAL, DECODE_SPECIAL[(fn, data[0])]
    if fn == 0x0A and mods << 8 | usage == spare_usage(index) and macro is not None:
        if len(macro) == 1 and macro[0][0] == COMMAND:
            return Action.COMMAND, macro[0][1]
        return Action.MACRO, [tuple(e) for e in macro]
    if fn == 0x02 and mods == 0 and usage in KEYCODE:
        return Action.KEY, KEYCODE[usage]
    if fn == 0x02 and usage == 0 and mods in [1 << i for i in range(8)]:
        return Action.KEY, KEYCODE[0xE0 + mods.bit_length() - 1]
    if fn == 0x0A and (mods << 8 | usage) in DECODE_CONSUMER:
        return Action.KEY, DECODE_CONSUMER[mods << 8 | usage]
    return UNKNOWN, 0


def led_effect(mode, color):
    """Returns (size, args) of the scroll wheel effect report."""
    if mode == Mode.OFF:
        return 0x06, [0x01, 0x01, 0x00]
    if mode == Mode.CYCLE:
        return 0x06, [0x01, 0x01, 0x03]
    if mode == Mode.ON:
        return 0x09, [0x01, 0x01, 0x01, 0x00, 0x00, 0x01, *color]
    if mode == Mode.BREATHING:
        return 0x09, [0x01, 0x01, 0x02, 0x01, 0x00, 0x01, *color]
    raise ValueError(f"unsupported LED mode {mode}")


def dpi_bytes(dpi):
    if not 100 <= dpi <= 18000:
        raise ValueError(f"resolution {dpi} out of range")
    return list(dpi.to_bytes(2, "big") * 2)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


class _Local:
    """Replaces the DBus primitives of the Ratbagd* classes with a local
    property dict, so their getters, setters and signals work unchanged."""

    _object_path = None
    __eq__ = object.__eq__
    __hash__ = object.__hash__

    def _get_dbus_property(self, property):
        return self._props.get(property)

    def _set_dbus_property(self, property, type, value, readwrite=True):
        value = GLib.Variant(type, value).unpack()
        self._props[property] = value
        # What ratbagd would echo back in PropertiesChanged.
        changed = {property: value, "IsDirty": True} if readwrite else {property: value}
        self._on_properties_changed(None, changed, [])

    def _dbus_call(self, method, type="", *value):
        return getattr(self, "_" + method.lower())()


class RazerResolution(_Local, RatbagdResolution):
    def __init__(self, index, dpi, active, disabled, siblings):
        GObject.GObject.__init__(self)
        self._props = {
            "Index": index,
            "Resolution": dpi,
            "Resolutions": list(range(100, 18001, 50)),
            "Capabilities": [RatbagdResolution.CAP_DISABLE],
            "IsActive": active,
            "IsDisabled": disabled,
        }
        self._saved = dict(self._props)
        self._active = active
        self._default = False
        self._disabled = disabled
        self._resolution = (dpi,)
        self._siblings = siblings

    def _setactive(self):
        for r in self._siblings:
            if r is not self:
                r._set_dbus_property("IsActive", "b", False, readwrite=False)


class RazerButton(_Local, RatbagdButton):
    def __init__(self, index, mapping):
        GObject.GObject.__init__(self)
        self._props = {"Index": index, "Mapping": mapping, "ActionTypes": list(Action)}
        self._saved = dict(self._props)


class RazerLed(_Local, RatbagdLed):
    def __init__(self, brightness, mode, color):
        GObject.GObject.__init__(self)
        self._props = {
            "Index": 0,
            "Mode": mode,
            "Modes": list(Mode),
            "Color": color,
            "ColorDepth": RatbagdLed.ColorDepth.RGB_888,
            "Brightness": brightness,
            "EffectDuration": 1000,
        }
        self._saved = dict(self._props)
        self._mode = mode
        self._color = color
        self._brightness = brightness
        self._effect_duration = 1000


class RazerProfile(_Local, RatbagdProfile):
    def __init__(self, rate, resolutions, buttons, leds):
        GObject.GObject.__init__(self)
        self._props = {
            "Index": 0,
            "Name": "",
            "Capabilities": [],
            "ReportRate": rate,
            "ReportRates": [125, 500, 1000],
            "Debounces": [],
        }
        self._saved = dict(self._props)
        self._active = True
        self._angle_snapping = -1
        self._debounce = -1
        self._dirty = False
        self._disabled = False
        self._report_rate = rate
        self._resolutions = resolutions
        self._buttons = buttons
        self._leds = leds
        self._subscribe_dirty(resolutions + buttons + leds)

    def _setactive(self):
        pass  # the only profile is always active


class RazerDevice(_Local, RatbagdDevice):
    def __init__(self, transport):
        GObject.GObject.__init__(self)
        self._transport = transport
        q = transport.query
        fw = q(0x00, 0x81, 0x02)
        self._props = {
            "Name": "Razer Basilisk V3 X HyperSpeed",
            "Model": "usb:1532:00b9:0",
            "DeviceType": RatbagDeviceType.MOUSE,
            "FirmwareVersion": f"{fw[0]}.{fw[1]}",
        }
        self._id = self._props["Model"]

        a = q(0x04, 0x86, 0x26, [0x01])
        stages = [a[3 + 7 * i : 10 + 7 * i] for i in range(min(a[2], 5))]
        # The active byte is the id of the active stage, not its position.
        active = next((i for i, s in enumerate(stages) if s[0] == a[1]), 0)
        resolutions = []
        for i in range(5):
            dpi = stages[i][1] << 8 | stages[i][2] if i < len(stages) else 800
            resolutions.append(
                RazerResolution(i, dpi, i == active, i >= len(stages), resolutions)
            )

        rate = RATES.get(q(0x00, 0x85, 0x01)[0], 0)

        reassert(q)  # the mouse may have dropped a macro button since
        macros = load_macros()
        buttons = []
        for i, id in enumerate(BUTTON_IDS):
            a = q(0x02, 0x8C, 0x0A, [0x00, id, 0x00])
            buttons.append(RazerButton(i, decode(a[3], a[5:10], i, macros)))

        brightness = q(0x0F, 0x84, 0x03, [0x01, 0x01])[2]
        # The mouse cannot report its effect, so show the last one applied.
        try:
            with open(LED_FILE) as f:
                led = json.load(f)
            mode, (r, g, b) = Mode(led["mode"]), led["color"]
        except (OSError, ValueError, KeyError, TypeError):
            mode, (r, g, b) = Mode.CYCLE, (0, 255, 0)
        leds = [RazerLed(brightness, mode, (r, g, b))]

        self._profiles = [RazerProfile(rate, resolutions, buttons, leds)]

    def _commit(self):
        # shortcut: each report blocks the UI for ~50 ms, a full apply about a
        # second; move the writes to a thread if that becomes noticeable
        profile = self._profiles[0]
        q = self._transport.query
        rejected = False
        try:
            for b in profile.buttons:
                if b._props == b._saved:
                    continue
                try:
                    fn, data = encode(*b._props["Mapping"], b.index)
                except ValueError as e:
                    # Undo only this button, so the "reset" banner is true for it
                    # and the other pending changes still get applied.
                    print(f"Razer: {e}", file=sys.stderr)
                    b._props = dict(b._saved)
                    b._on_properties_changed(None, {"Mapping": b._saved["Mapping"]}, [])
                    rejected = True
                    continue
                # The macro must exist before the button sends its key, and a
                # stale one would be rewritten by reassert(), so update it first.
                macro = host_macro(*b._props["Mapping"])
                with macro_lock():
                    store_macro(b.index, macro)
                    for slot in (0x01, 0x00):  # the stored profile, then the live one
                        args = [slot, BUTTON_IDS[b.index], 0x00, fn, len(data), *data]
                        q(0x02, 0x0C, 0x0A, args)
                b._saved = dict(b._props)

            resolutions = profile.resolutions
            if any(r._props != r._saved for r in resolutions):
                enabled = [r for r in resolutions if not r.is_disabled]
                active = [r for r in enabled if r.is_active]
                if not active:
                    raise ValueError("the active resolution is disabled")
                args = [0x01, enabled.index(active[0]) + 1, len(enabled)]
                for i, r in enumerate(enabled):
                    args += [i + 1, *dpi_bytes(r._props["Resolution"]), 0, 0]
                q(0x04, 0x06, 0x26, args)
                q(0x04, 0x05, 0x07, [0x01, *dpi_bytes(active[0]._props["Resolution"])])
                for r in resolutions:
                    r._saved = dict(r._props)

            rate = profile._props["ReportRate"]
            if rate != profile._saved["ReportRate"]:
                if rate not in RATE_CODES:
                    raise ValueError(f"unsupported report rate {rate}")
                q(0x00, 0x05, 0x01, [RATE_CODES[rate]])
                profile._saved["ReportRate"] = rate

            led = profile.leds[0]
            mode, color = led._props["Mode"], led._props["Color"]
            if (mode, color) != (led._saved["Mode"], led._saved["Color"]):
                size, args = led_effect(mode, color)
                write_json(LED_FILE, {"mode": mode, "color": color})
                q(0x0F, 0x02, size, args)
                led._saved.update(Mode=mode, Color=color)
            brightness = led._props["Brightness"]
            if brightness != led._saved["Brightness"]:
                q(0x0F, 0x04, 0x03, [0x01, 0x01, brightness])
                led._saved["Brightness"] = brightness
        except (OSError, RatbagError, ValueError) as e:
            # The edits stay dirty, so Apply can be retried.
            print(f"Razer: apply failed: {e}", file=sys.stderr)
            self.emit("resync")
            return
        profile._on_properties_changed(None, {"IsDirty": False}, [])
        if load_macros():
            start_macro_service()
        if rejected:
            self.emit("resync")


def start_macro_service():
    """Starts the macro player without waiting; a missing unit (e.g. when run
    from the build directory) only means macros are not played."""
    cmd = ["systemctl", "--user", "enable", "--now", "rpiper-razer-macros.service"]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def razer_devices():
    """Returns the Razer mice that answer; prints why one is left out."""
    # shortcut: no Razer hotplug, restart Piper; add a sysfs poll if needed
    path = find_hidraw(0)
    if path is None:
        return []
    try:
        fd = os.open(path, os.O_RDWR)
    except PermissionError:
        print(f"Razer: no access to {path}, the udev rule is missing", file=sys.stderr)
        return []
    except OSError as e:
        print(f"Razer: {path}: {e}", file=sys.stderr)
        return []
    try:
        return [RazerDevice(Transport(fd))]
    except (OSError, RatbagError) as e:
        print(f"Razer: {path} left out: {e}", file=sys.stderr)
        os.close(fd)
        return []


class RazerRatbagd(GObject.GObject):
    """Ratbagd's devices plus the Razer mice handled here."""

    __gsignals__ = {
        "device-added": (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_PYOBJECT,)),
        "device-removed": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (GObject.TYPE_PYOBJECT,),
        ),
        "daemon-disappeared": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, api_version):
        super().__init__()
        self._razer = razer_devices()
        self._ratbagd = None
        try:
            self._ratbagd = Ratbagd(api_version)
        except (RatbagdUnavailableError, RatbagdIncompatibleError):
            if not self._razer:
                raise
            return
        for signal in ("device-added", "device-removed", "daemon-disappeared"):
            self._ratbagd.connect(
                signal, lambda _, *args, s=signal: self.emit(s, *args)
            )
        self._ratbagd.connect("notify::devices", lambda *_: self.notify("devices"))

    @GObject.Property
    def devices(self):
        return (self._ratbagd.devices if self._ratbagd else []) + self._razer
