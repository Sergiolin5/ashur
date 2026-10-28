"""Yield policies: how much a device offers Ashur right now.

Sergio always wins on his own machines. The decision functions are pure, so
they are unit-tested on any OS; only the signal readers are Windows-specific.
"""
import logging
import platform
from dataclasses import dataclass
from typing import Optional

from shared.protocol import Mode

log = logging.getLogger("ashur.policy")


@dataclass
class Thresholds:
    idle_for_full_s: float = 600       # 10 minutes without keyboard or mouse
    other_load_off_pct: float = 70     # his load above this: Ashur backs off
    battery_off_pct: float = 30        # portable: below this on battery, nothing


@dataclass
class Signals:
    idle_s: Optional[float]            # None = cannot be read on this OS
    fullscreen: bool = False
    other_load_pct: float = 0.0
    on_battery: bool = False
    battery_pct: Optional[float] = None


def decide_shared(s: Signals, t: Thresholds) -> Mode:
    """The shared main machine (Ryzen 9)."""
    if s.fullscreen or s.other_load_pct > t.other_load_off_pct:
        return Mode.OFF
    if s.idle_s is not None and s.idle_s >= t.idle_for_full_s:
        return Mode.FULL
    return Mode.LIGHT                  # active, or idle time unknown: be polite


def decide_portable(s: Signals, t: Thresholds) -> Mode:
    """A battery device that is also used by hand (Legion Go)."""
    mode = decide_shared(s, t)
    if s.on_battery:
        if s.battery_pct is not None and s.battery_pct < t.battery_off_pct:
            return Mode.OFF
        if mode == Mode.FULL:
            return Mode.LIGHT          # heavy work only when plugged in
    return mode


# --- signal readers --------------------------------------------------------

def _windows_idle_s() -> Optional[float]:
    import ctypes
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return None
    now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
    return ((now - lii.dwTime) & 0xFFFFFFFF) / 1000.0


def _windows_fullscreen() -> bool:
    """True when the foreground window covers its whole monitor (a game, a video)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd or hwnd in (user32.GetDesktopWindow(), user32.GetShellWindow()):
        return False
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
        return False

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    mon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return False
    m = mi.rcMonitor
    return (rect.left <= m.left and rect.top <= m.top
            and rect.right >= m.right and rect.bottom >= m.bottom)


class SignalReader:
    def __init__(self):
        import psutil
        self.psutil = psutil
        self.me = psutil.Process()
        self.windows = platform.system() == "Windows"
        psutil.cpu_percent(None)
        self._own_cpu()

    def _own_cpu(self) -> float:
        total = 0.0
        for p in [self.me, *self.me.children(recursive=True)]:
            try:
                total += p.cpu_percent(None)
            except self.psutil.Error:
                pass
        return total / (self.psutil.cpu_count() or 1)

    def read(self) -> Signals:
        idle, fullscreen = None, False
        if self.windows:
            try:
                idle, fullscreen = _windows_idle_s(), _windows_fullscreen()
            except Exception as e:  # never let a signal crash the agent
                log.warning("could not read Windows signals: %s", e)
        other = max(0.0, self.psutil.cpu_percent(None) - self._own_cpu())
        batt = None
        try:
            batt = self.psutil.sensors_battery()
        except Exception:
            pass
        return Signals(idle_s=idle, fullscreen=fullscreen, other_load_pct=other,
                       on_battery=bool(batt and not batt.power_plugged),
                       battery_pct=batt.percent if batt else None)


class Policy:
    def __init__(self, name: str, thresholds: Optional[Thresholds] = None,
                 reader: Optional[SignalReader] = None):
        if name not in ("shared_main_machine", "portable", "always_full"):
            raise ValueError(f"unknown yield policy: {name}")
        self.name = name
        self.t = thresholds or Thresholds()
        self.reader = reader if reader is not None or name == "always_full" else SignalReader()

    def mode(self) -> Mode:
        if self.name == "always_full":
            return Mode.FULL
        s = self.reader.read()
        return decide_shared(s, self.t) if self.name == "shared_main_machine" else decide_portable(s, self.t)
