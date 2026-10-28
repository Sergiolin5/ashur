"""Replay a churn scenario against an in-process core, in virtual time.

    python -m sim.scenario sim/scenarios/evening_churn.yaml

A 10-minute scenario runs in about a second and gives the same result every
time, so device churn can be replayed exactly (the system paper's testbed).
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from core.app import create_app
from shared.protocol import HEARTBEAT_INTERVAL_S
from sim.fake_node import FakeNode

ENROLL, ADMIN = "sim-enroll", "sim-admin"


class VirtualClock:
    def __init__(self, start: float = 1_000_000.0):
        self.start = self.now = start

    def __call__(self) -> float:
        return self.now


def run(scenario: dict, db_path: str) -> dict:
    clock = VirtualClock()
    app = create_app(db_path, ENROLL, ADMIN, clock=clock, sweep_every_s=None)
    client = TestClient(app)
    admin = {"X-Admin-Key": ADMIN}

    tick = scenario.get("tick_s", HEARTBEAT_INTERVAL_S)
    duration = scenario["duration_s"]

    nodes = {}
    events = []  # (at, order, callable)
    for spec in scenario["nodes"]:
        node = FakeNode(client=client, device_id=spec["id"], enroll_key=ENROLL,
                        persona=spec.get("persona", spec["id"]), os=spec.get("os", "linux"),
                        modules=spec.get("modules", ["inventory"]),
                        task_seconds=spec.get("task_seconds", 30),
                        fail_rate_every=spec.get("fail_every", 0))
        nodes[node.device_id] = node
        for ev in spec.get("timeline", []):
            act = {"join": lambda n=node, e=ev: n.join(),
                   "vanish": lambda n=node, e=ev: n.vanish(),
                   "mode": lambda n=node, e=ev: n.set_mode(e["mode"])}[ev["do"]]
            events.append((ev["at"], 0, act))
    for batch in scenario.get("tasks", []):
        def queue(b=batch):
            for _ in range(b.get("count", 1)):
                client.post("/tasks", headers=admin, json={
                    "module": b["module"], "weight": b.get("weight", "light"),
                    "payload": b.get("payload", {})}).raise_for_status()
        events.append((batch["at"], 1, queue))
    events.sort(key=lambda e: (e[0], e[1]))

    t = 0
    while t <= duration:
        clock.now = clock.start + t
        while events and events[0][0] <= t:
            events.pop(0)[2]()
        for node in nodes.values():
            node.tick(clock.now)
        t += tick

    status = client.get("/status", headers=admin).json()
    log = list(reversed(client.get("/log", headers=admin, params={"limit": 10_000}).json()))
    for row in log:
        row["t"] = round(row.pop("ts") - clock.start, 1)
    requeued = sum(len(r["detail"].get("requeued_tasks", [])) for r in log)
    client.close()
    app.state.db.close()          # Windows cannot delete an open database file
    return {
        "scenario": scenario.get("name", "unnamed"),
        "tasks": status["tasks"],
        "requeued_by_churn": requeued,
        "per_node": {n.device_id: {"done": n.done, "failed": n.failed} for n in nodes.values()},
        "final_modes": {n["device_id"]: n["mode"] for n in status["nodes"]},
        "log": log,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("scenario", type=Path)
    p.add_argument("--log-out", type=Path, help="write the session log as JSONL (paper data)")
    args = p.parse_args(argv)
    scenario = yaml.safe_load(args.scenario.read_text())
    with tempfile.TemporaryDirectory() as d:
        out = run(scenario, str(Path(d) / "sim.db"))

    print(f"Scenario: {out['scenario']}")
    print(f"Tasks:    {out['tasks']}   requeued by churn: {out['requeued_by_churn']}")
    for nid, c in out["per_node"].items():
        print(f"  {nid:<16} done {c['done']:>3}  failed {c['failed']:>2}  final {out['final_modes'][nid]}")
    print("\nTimeline (joins, leaves, mode changes):")
    for row in out["log"]:
        if row["event"] in {"register", "reregister", "offline", "back_online", "mode_change", "revoked"}:
            print(f"  t={row['t']:>6}s  {row['node_id']:<16} {row['event']:<12} {json.dumps(row['detail'])}")
    if args.log_out:
        args.log_out.write_text("\n".join(json.dumps(r) for r in out["log"]) + "\n")
        print(f"\nSession log written to {args.log_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
