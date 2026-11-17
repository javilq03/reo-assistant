from datetime import datetime

from reo import clock, startup


def test_device_clock_and_spanish_names():
    t = datetime(2026, 11, 17, 9, 46).timestamp()
    dt, src = clock.now({"clock_mode": "device"}, clock=lambda: t)
    assert src == "device" and clock.describe(dt, "en") == "Tuesday 17 November 2026, 09:46"
    assert clock.describe(dt, "es") == "martes 17 de noviembre de 2026, 09:46"
    n = clock.note({}, clock=lambda: t)
    assert "Tuesday 17 November 2026, 09:46" in n and "martes 17 de noviembre" in n and "WEB RESULTS" in n


def test_manual_clock_keeps_counting_from_when_it_was_set():
    s = {"clock_mode": "manual", "clock_manual": "2026-12-24T20:00", "clock_set_at": 1000.0}
    dt, src = clock.now(s, clock=lambda: 1000.0 + 90 * 60)
    assert src == "manual" and dt == datetime(2026, 12, 24, 21, 30)
    assert clock.now({"clock_mode": "manual", "clock_manual": "garbage"})[1] == "device"      # nothing valid chosen: fall back to the PC


class FakeReg:
    HKEY_CURRENT_USER, KEY_READ, KEY_SET_VALUE, REG_SZ = 1, 2, 4, 1
    def __init__(self): self.vals = {}
    def OpenKey(self, *a): return self
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def SetValueEx(self, k, name, _r, _t, v): self.vals[name] = v
    def QueryValueEx(self, k, name):
        if name not in self.vals: raise FileNotFoundError(name)
        return self.vals[name], 1
    def DeleteValue(self, k, name):
        if name not in self.vals: raise FileNotFoundError(name)
        del self.vals[name]


def test_startup_registration_is_written_verified_and_removed():
    r = FakeReg()
    assert not startup.is_enabled(r)
    assert startup.set_enabled(True, r, cmd='"C:\\Reo\\Reo.exe" --startup') == (True, "")
    assert startup.is_enabled(r) and r.vals["Reo"].endswith("--startup")
    assert startup.set_enabled(False, r) == (True, "") and not startup.is_enabled(r)
    assert startup.set_enabled(False, r) == (True, "")                                       # already off is fine
    assert "run.py" in startup.command() or "--startup" in startup.command()
