"""A simulated node: speaks the real protocol, but its device is imaginary.

It talks through any httpx-compatible client, so the same class drives an
in-process core (deterministic replay) or, later, a real core over the tailnet.
"""
from dataclasses import dataclass, field
from typing import Optional

from shared.protocol import Mode


@dataclass
class FakeNode:
    client: object                    # httpx.Client or fastapi TestClient
    device_id: str
    enroll_key: str
    persona: str = "sim"
    os: str = "linux"
    modules: list[str] = field(default_factory=lambda: ["inventory"])
    task_seconds: float = 30.0        # how long each task "takes" on this device
    fail_rate_every: int = 0          # fail every Nth task (0 = never)

    token: Optional[str] = None
    mode: Mode = Mode.FULL
    alive: bool = False
    current: Optional[dict] = None
    started_at: float = 0.0
    done: int = 0
    failed: int = 0

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def join(self) -> None:
        r = self.client.post(
            "/nodes/register",
            headers={"X-Enroll-Key": self.enroll_key},
            json={"device_id": self.device_id, "persona": self.persona, "os": self.os,
                  "modules": self.modules, "simulated": True},
        )
        r.raise_for_status()
        self.token = r.json()["token"]
        self.alive, self.mode, self.current = True, Mode.FULL, None

    def vanish(self) -> None:
        """The device sleeps or loses power: it just stops talking."""
        self.alive = False
        self.current = None

    def set_mode(self, mode: str) -> None:
        self.mode = Mode(mode)

    def tick(self, now: float) -> None:
        """One heartbeat interval: beat, finish work if due, pull new work."""
        if not self.alive:
            return
        self.client.post(f"/nodes/{self.device_id}/heartbeat", headers=self._auth(),
                         json={"mode": self.mode.value}).raise_for_status()

        if self.current and now - self.started_at >= self.task_seconds:
            n = self.done + self.failed + 1
            ok = not (self.fail_rate_every and n % self.fail_rate_every == 0)
            r = self.client.post(f"/tasks/{self.current['id']}/result", headers=self._auth(),
                                 json={"ok": ok, "result": {"by": self.device_id},
                                       "error": None if ok else "simulated failure"})
            if r.status_code == 200:
                self.done += ok
                self.failed += (not ok)
            self.current = None  # 409/404 means the core already rerouted it

        if self.current is None:
            r = self.client.get(f"/nodes/{self.device_id}/tasks/next", headers=self._auth())
            r.raise_for_status()
            if r.json():
                self.current, self.started_at = r.json(), now
