from pathlib import Path

import yaml

from sim.scenario import run

SCENARIOS = Path(__file__).parent.parent / "sim" / "scenarios"


def test_evening_churn_is_deterministic(tmp_path):
    s = yaml.safe_load((SCENARIOS / "evening_churn.yaml").read_text())
    a = run(s, str(tmp_path / "a.db"))
    b = run(s, str(tmp_path / "b.db"))
    assert a["per_node"] == b["per_node"]
    assert [(r["t"], r["event"]) for r in a["log"]] == [(r["t"], r["event"]) for r in b["log"]]


def test_evening_churn_loses_no_task(tmp_path):
    s = yaml.safe_load((SCENARIOS / "evening_churn.yaml").read_text())
    out = run(s, str(tmp_path / "s.db"))
    queued = sum(t["count"] for t in s["tasks"])
    assert sum(out["tasks"].values()) == queued
    assert out["tasks"].get("done", 0) == queued        # churn delays work, never drops it
    assert out["requeued_by_churn"] >= 1                  # the vanishings did hit running tasks
    assert out["final_modes"]["sim-legion-go"] == "offline"


def test_all_devices_gone_keeps_work_queued(tmp_path):
    s = {"name": "blackout", "duration_s": 300, "tasks": [{"at": 0, "module": "inventory", "count": 3}],
         "nodes": [{"id": "only", "task_seconds": 600,
                    "timeline": [{"at": 0, "do": "join"}, {"at": 30, "do": "vanish"}]}]}
    out = run(s, str(tmp_path / "s.db"))
    assert out["tasks"] == {"queued": 3}                  # nothing lost, nothing pretended done
