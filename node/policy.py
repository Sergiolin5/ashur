"""Yield policies: how much a device offers Ashur right now.

Sergio always wins on his own machines. The decision functions are pure, so
they are unit-tested on any OS; only the signal readers are Windows-specific.
"""
import logging
import platform
import time
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


def explain(s: Signals, t: Thresholds, mode: Mode, portable: bool) -> str:
    """Why the policy chose this mode, in Spanish, for the panel and `ashur dispositivos`."""
    if mode == Mode.OFF:
        if portable and s.on_battery and s.battery_pct is not None and s.battery_pct < t.battery_off_pct:
            return f"con batería al {s.battery_pct:.0f}%: pausado hasta conectarlo"
        if s.fullscreen:
            return "hay una app en pantalla completa"
        return f"otros programas usan el {s.other_load_pct:.0f}% del procesador"
    if mode == Mode.LIGHT:
        if portable and s.on_battery and s.idle_s is not None and s.idle_s >= t.idle_for_full_s:
            return "libre pero con batería: solo trabajo ligero"
        if s.idle_s is None:
            return "no puedo leer cuánto lleva sin uso"
        return f"en uso (sin tocar hace {s.idle_s / 60:.0f} de {t.idle_for_full_s / 60:.0f} min)"
    return "libre"


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


def parse_mac_idle_ns(ioreg_output: str) -> Optional[float]:
    """HIDIdleTime from `ioreg -c IOHIDSystem`, in nanoseconds -> seconds."""
    for line in ioreg_output.splitlines():
        if '"HIDIdleTime"' in line:
            try:
                return int(line.rsplit("=", 1)[1].strip()) / 1e9
            except ValueError:
                return None
    return None


def _mac_idle_s() -> Optional[float]:
    import subprocess
    out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], capture_output=True,
                         text=True, timeout=5).stdout
    return parse_mac_idle_ns(out)


class SignalReader:
    def __init__(self):
        import psutil
        self.psutil = psutil
        self.me = psutil.Process()
        self.windows = platform.system() == "Windows"
        self.mac = platform.system() == "Darwin"
        self._sampled = False
        psutil.cpu_percent(None)
        self._own_cpu()

    def _helpers(self) -> list:
        """Processes that work for Ashur without being its children: Ollama runs as its own
        service, and its answers must not count as Sergio's load (that paused the Mac)."""
        found = []
        try:
            for p in self.psutil.process_iter(["name"]):       # psutil reuses these objects between calls
                if (p.info.get("name") or "").lower().startswith("ollama"):
                    found.append(p)
        except self.psutil.Error:
            pass
        return found

    def _own_cpu(self) -> float:
        total = 0.0
        try:
            mine = [self.me, *self.me.children(recursive=True)]
        except self.psutil.Error:
            mine = [self.me]
        seen = {p.pid for p in mine}
        for p in [*mine, *[h for h in self._helpers() if h.pid not in seen]]:
            try:
                total += p.cpu_percent(None)
            except self.psutil.Error:
                pass
        return total / (self.psutil.cpu_count() or 1)

    def read(self) -> Signals:
        if not self._sampled:
            # cpu_percent compares with the previous call: right after start that gap is ~0 s
            # and the reading is noise (a self-test once saw "off" from it). Take a real sample.
            time.sleep(1.0)
            self._sampled = True
        idle, fullscreen = None, False
        if self.windows:
            try:
                idle, fullscreen = _windows_idle_s(), _windows_fullscreen()
            except Exception as e:  # never let a signal crash the agent
                log.warning("could not read Windows signals: %s", e)
        elif self.mac:
            try:
                idle = _mac_idle_s()           # fullscreen is not detected on macOS yet
            except Exception as e:
                log.warning("could not read macOS idle time: %s", e)
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
        self.reason = ""

    def mode(self) -> Mode:
        if self.name == "always_full":
            self.reason = ""
            return Mode.FULL
        s = self.reader.read()
        portable = self.name == "portable"
        mode = decide_portable(s, self.t) if portable else decide_shared(s, self.t)
        self.reason = explain(s, self.t, mode, portable)
        return mode
