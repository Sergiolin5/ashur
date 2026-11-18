"""0.67.0: a node's configuration edited from the panel, carried by the heartbeat, kept in node.remote.yaml."""
import yaml

from node.agent import REMOTE_FILE, Agent
from node.policy import Policy
from shared import remote_config
from tests.test_node import A, ENROLL, core, make_agent  # noqa: F401


def enrolled(c, tmp_path):
    agent = make_agent(c, tmp_path)
    agent.load_modules()
    agent.register(ENROLL)
    return agent


def test_validation_keeps_out_what_reaches_the_core():
    import pytest
    for bad in ({"core_url": "http://otro"}, {"device_id": "x"}, {"token": "t"}, {"thresholds": {"battery_off_pct": 150}},
                {"thresholds": {"no_existe": 1}}, {"yield_policy": "turbo"}, {"modules": ["no-existe"]},
                {"llm_model": "qwen; rm -rf /"}):
        with pytest.raises(ValueError):
            remote_config.validate(bad)
    ok = remote_config.validate({"llm_model": "qwen2.5:7b", "modules": ["llm", "inventory", "llm"],
                                 "thresholds": {"battery_off_pct": 40}})
    assert ok == {"llm_model": "qwen2.5:7b", "modules": ["inventory", "llm"], "thresholds": {"battery_off_pct": 40}}


def test_panel_sets_the_node_applies_it_without_restarting_and_reports_it(core, tmp_path):
    c, _ = core
    agent = enrolled(c, tmp_path)
    agent.tick()
    r = c.put("/api/nodes/ryzen9/config", headers=A,
              json={"values": {"llm_model": "qwen2.5:7b", "yield_policy": "portable",
                               "thresholds": {"battery_off_pct": 45}}})
    assert r.status_code == 200 and r.json()["aplicada"] is False
    assert c.get("/api/nodes/ryzen9/config", headers=A).json()["aplicada"] is False
    agent.tick()                                                  # the reply brings it; the agent applies it
    assert agent.cfg["llm_model"] == "qwen2.5:7b" and agent.policy.name == "portable"
    assert agent.policy.t.battery_off_pct == 45 and agent.base_cfg.get("llm_model") is None
    saved = yaml.safe_load((tmp_path / "state" / REMOTE_FILE).read_text(encoding="utf-8"))
    assert saved["values"]["llm_model"] == "qwen2.5:7b" and saved["rev"] == agent.config_rev
    agent.tick()                                                  # the next beat says which revision it runs
    got = c.get("/api/nodes/ryzen9/config", headers=A).json()
    assert got["aplicada"] is True and got["applied_rev"] == got["rev"]
    again = Agent(dict(agent.base_cfg), client=c, policy=Policy("always_full"), state=tmp_path / "state")
    assert again.cfg["llm_model"] == "qwen2.5:7b" and again.config_rev == agent.config_rev   # a restart keeps it


def test_modules_changed_from_the_panel_are_loaded_and_announced(core, tmp_path):
    c, _ = core
    agent = enrolled(c, tmp_path)
    agent.tick()
    c.put("/api/nodes/ryzen9/config", headers=A, json={"values": {"modules": ["inventory", "mudanza"]}})
    agent.tick()
    assert "mudanza" in agent.cfg["modules"] and agent.announced is False
    agent.tick()
    node = c.app.state.db.get_node("ryzen9")
    assert "mudanza" in node["modules"]


def test_clearing_goes_back_to_node_yaml(core, tmp_path):
    c, _ = core
    agent = enrolled(c, tmp_path)
    c.put("/api/nodes/ryzen9/config", headers=A, json={"values": {"llm_model": "qwen2.5:7b"}})
    agent.tick()
    assert agent.cfg["llm_model"] == "qwen2.5:7b"
    c.put("/api/nodes/ryzen9/config", headers=A, json={"values": {}})
    agent.tick()
    assert "llm_model" not in agent.cfg and agent.cfg["modules"] == ["inventory"]


def test_bad_values_are_refused_by_the_panel(core, tmp_path):
    c, _ = core
    enrolled(c, tmp_path)
    r = c.put("/api/nodes/ryzen9/config", headers=A, json={"values": {"core_url": "http://evil"}})
    assert r.status_code == 422 and "No se pueden cambiar en remoto: core_url." in r.text
    assert c.put("/api/nodes/nadie/config", headers=A, json={"values": {}}).status_code == 404
    assert c.put("/api/nodes/ryzen9/config", json={"values": {}}).status_code in (401, 403)


def test_the_panel_has_the_configure_box(core):
    c, _ = core
    html = c.get("/").text
    assert "placeConfigs()" in html and 'api/nodes/" + encodeURIComponent(id) + "/config' in html
    assert "Volver a su node.yaml" in html
