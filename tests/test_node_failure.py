"""Bug 1 (2026-11-17): a node that fails at the transport level (Ollama ReadTimeout on the Legion Go) must not stop the
work. The node is set aside for a while and the unit goes to another device; the queue is never paused."""
import json
import threading

from tests.test_encargos import TWO, A, FakeLLM, approve, fast_local, order, run_all, runner_app, seed  # noqa: F401


def prefer(c, node_id: str, seconds: float) -> None:
    """The node's model last ran fully on its GPU in `seconds`: best_llm_node picks the fastest such device."""
    c.db.set_setting(f"llm_perf:{node_id}", json.dumps(
        {"share": 1.0, "model": "qwen2.5:7b", "seconds": seconds, "at": c.clock(), "gpus": []}))


def test_failing_preferred_node_does_not_stop_the_encargo(tmp_path, fast_local):
    c = runner_app(tmp_path)
    eid, rid = order(c, seed(c, TWO))
    approve(c, rid)
    legion = FakeLLM(c, lambda p, n: RuntimeError("ReadTimeout: timed out"), device="legion-go")
    ryzen = FakeLLM(c, lambda p, n: "- idea", device="ryzen9")
    prefer(c, "legion-go", 1.0)
    prefer(c, "ryzen9", 5.0)
    try:
        run_all(c)
    finally:
        legion.close()
        ryzen.close()
    e = c.db.encargo_get(eid)
    assert e["status"] == "kept", e["error"]
    assert e["done"] == 2 and e["devices"] == {"ryzen9": 2}
    assert len(legion.payloads) == 1                           # set aside after its first transport failure
    assert c.get("/api/overview", headers=A).json()["queue_paused"] is False
    degraded = [ev for ev in c.db.log_rows(100) if ev["event"] == "node_degraded"]
    assert len(degraded) == 1 and degraded[0]["node_id"] == "legion-go"


def test_node_dropped_by_hand_mid_unit_requeues_and_another_finishes(tmp_path, fast_local):
    """Pulling the plug on the Legion Go while it holds a unit: the sweep marks it offline and puts the unit back in the
    queue, the Ryzen 9 answers it, and the queue is never paused."""
    c = runner_app(tmp_path)
    eid, rid = order(c, seed(c, TWO))
    approve(c, rid)
    state = {"held": 0}
    release = threading.Event()

    def unplugged(payload, n):
        state["held"] += 1
        legion.stop.set()                                       # stops polling and never beats again
        c.clock.now += 60                                       # past OFFLINE_AFTER_S: the next sweep notices
        c.post("/nodes/ryzen9/heartbeat", headers=ryzen.h, json={"mode": "full"})   # the Ryzen 9 keeps beating
        release.wait(30)                                        # holds the unit, silent, until the test ends
        return RuntimeError("too late")

    legion = FakeLLM(c, unplugged, device="legion-go")
    prefer(c, "legion-go", 1.0)
    prefer(c, "ryzen9", 5.0)
    ryzen = FakeLLM(c, lambda p, n: "- idea", device="ryzen9")
    try:
        run_all(c)
    finally:
        release.set()
        ryzen.close()
    e = c.db.encargo_get(eid)
    assert e["status"] == "kept", e["error"]
    assert e["done"] == 2 and e["devices"] == {"ryzen9": 2} and state["held"] == 1
    assert c.db.get_node("legion-go")["mode"] == "offline"
    assert c.get("/api/overview", headers=A).json()["queue_paused"] is False


def test_degraded_node_comes_back_after_its_time(tmp_path, fast_local):
    c = runner_app(tmp_path)
    eid, rid = order(c, seed(c, TWO))
    approve(c, rid)
    fails = {"n": 0}

    def flaky(p, n):
        if fails["n"] == 0:
            fails["n"] += 1
            return RuntimeError("ConnectError: [Errno 111] Connection refused")
        return "- idea"

    legion = FakeLLM(c, flaky, device="legion-go")
    prefer(c, "legion-go", 1.0)
    try:
        assert c.step() is False                                # its only device is set aside: the unit waits, no try used
        e = c.db.encargo_get(eid)
        assert e["status"] == "running" and e["error"] is None and e["done"] == 0
        assert c.app.state.is_degraded("legion-go")
        c.clock.now += 301
        c.post("/nodes/legion-go/heartbeat", headers=legion.h, json={"mode": "full"})
        assert not c.app.state.is_degraded("legion-go")
        run_all(c)
    finally:
        legion.close()
    e = c.db.encargo_get(eid)
    assert e["status"] == "kept" and e["devices"] == {"legion-go": 2}


def test_content_errors_still_use_up_the_tries(tmp_path, fast_local):
    """Only transport errors set a device aside: a model error on the text keeps the old three tries."""
    c = runner_app(tmp_path)
    eid, rid = order(c, seed(c, TWO))
    approve(c, rid)
    node = FakeLLM(c, lambda p, n: RuntimeError("model produced invalid output"), device="ryzen9")
    try:
        c.step()
    finally:
        node.close()
    e = c.db.encargo_get(eid)
    assert e["status"] == "failed" and e["error"] == "Un trozo falló 3 veces en ryzen9."
    assert not c.app.state.is_degraded("ryzen9")


# --- Bug 2 (2026-11-17): the chat reads the same snapshot as the status view, with the heartbeat threshold -----------

from tests.test_brain import make as brain_app, resp, text, tool        # noqa: E402
from tests.test_mcp import tool_text, tools_app                           # noqa: E402


def chat_network(c, fake) -> dict:
    """What the chat model is given when it asks for the devices' state."""
    c.post("/api/chat", headers=A, json={"message": "¿cómo están mis equipos?"})
    return json.loads(fake.calls[1]["messages"][-1]["content"][0]["content"])


def beat(c, device_id, modules=("llm",), **hb):
    tok = c.post("/nodes/register", headers={"X-Enroll-Key": "e"},
                 json={"device_id": device_id, "os": "windows", "modules": list(modules)}).json()["token"]
    c.post(f"/nodes/{device_id}/heartbeat", headers={"Authorization": f"Bearer {tok}"}, json={"mode": "full", **hb})


def test_chat_says_sin_senal_and_sin_datos_like_the_view(tmp_path):
    script = [resp([tool("network_status", {})], stop="tool_use"), resp([text("ok")])]
    c, fake, clock = brain_app(tmp_path, script)
    beat(c, "ryzen9", cpu_pct=12.0, ram_pct=30.0)
    beat(c, "legion-go")
    clock.now += 60                                           # both silent past OFFLINE_AFTER_S
    beat(c, "ryzen9", cpu_pct=12.0, ram_pct=30.0)             # the Ryzen 9 beats again; the Legion Go does not
    net = {d["id"]: d for d in chat_network(c, fake)["dispositivos"]}
    assert net["ryzen9"]["estado"] == "ryzen9: en línea, CPU 12%, RAM 30%"
    assert net["ryzen9"]["gpu"] == "sin datos" and net["ryzen9"]["bateria"] == "sin datos"
    assert net["legion-go"]["estado"] == "legion-go: sin señal"
    assert net["legion-go"]["cpu"] == net["legion-go"]["ram"] == net["legion-go"]["gpu"] == "sin datos"


def test_heartbeat_threshold_applies_even_when_the_sweep_has_not_run(tmp_path):
    """A status read while another sweep holds its lock used to show a silent node as it was at its last beat."""
    script = [resp([tool("network_status", {})], stop="tool_use"), resp([text("ok")])]
    c, fake, clock = brain_app(tmp_path, script)
    beat(c, "ryzen9", cpu_pct=12.0)
    clock.now += 60
    lock = c.app.state.sweep_lock
    assert lock.acquire(blocking=False)
    try:
        net = {d["id"]: d for d in chat_network(c, fake)["dispositivos"]}
    finally:
        lock.release()
    assert net["ryzen9"]["estado"] == "ryzen9: sin señal" and net["ryzen9"]["cpu"] == "sin datos"


def test_chat_and_connector_print_the_same_line_per_device(tmp_path):
    c, _ = tools_app(tmp_path)
    c.tailnet.post("/nodes/register", headers={"X-Enroll-Key": "e"}, json={"device_id": "mac-m1", "os": "macos", "modules": []})
    estado = tool_text(c, "ashur_estado")[0].splitlines()
    net = c.app.state.network_for_brain()["dispositivos"]
    assert [d["estado"] for d in net] == [line for line in estado if line.split(":")[0] in {d["id"] for d in net}]


def test_local_and_connector_prompts_forbid_invented_figures(tmp_path):
    from core.brain import NET_RULE
    c, _, _ = brain_app(tmp_path, [])
    beat(c, "ryzen9")
    brain = c.app.state.brain
    for prompt in (brain._local_system(), brain.connector_system()):
        assert NET_RULE in prompt and '"estado": "ryzen9: en línea"' in prompt


def test_a_unit_that_fails_on_a_device_retries_on_another(tmp_path, fast_local):
    """2026-11-18: the Legion Go answered every research turn with an Ollama rejection (RuntimeError, not transport),
    and each retry went back to it (still the preferred device) while the Ryzen 9 sat free."""
    c = runner_app(tmp_path)
    eid, rid = order(c, seed(c, TWO))
    approve(c, rid)
    legion = FakeLLM(c, lambda p, n: RuntimeError("Ollama respondió 400 en /api/chat: no tools"), device="legion-go")
    ryzen = FakeLLM(c, lambda p, n: "- idea", device="ryzen9")
    prefer(c, "legion-go", 1.0)
    prefer(c, "ryzen9", 5.0)
    try:
        run_all(c)
    finally:
        legion.close()
        ryzen.close()
    e = c.db.encargo_get(eid)
    assert e["status"] == "kept", e["error"]
    assert e["devices"] == {"ryzen9": 2} and len(legion.payloads) == 2      # one failed try per unit, then elsewhere
    assert not c.app.state.is_degraded("legion-go")                       # a model error does not set it aside


def test_with_one_device_a_unit_still_retries_on_it(tmp_path, fast_local):
    c = runner_app(tmp_path)
    eid, rid = order(c, seed(c, TWO))
    approve(c, rid)
    node = FakeLLM(c, lambda p, n: RuntimeError("modelo raro") if n == 1 else "- idea", device="ryzen9")
    try:
        run_all(c)
    finally:
        node.close()
    assert c.db.encargo_get(eid)["status"] == "kept" and len(node.payloads) == 3
