"""Protocol messages shared by the core and every node.

Core and nodes import the same models, so the two sides cannot drift apart.
"""
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

HEARTBEAT_INTERVAL_S = 15
MISSED_BEATS_BEFORE_OFFLINE = 3
OFFLINE_AFTER_S = HEARTBEAT_INTERVAL_S * MISSED_BEATS_BEFORE_OFFLINE  # 45 s


class Mode(str, Enum):
    """What a node offers right now (see the yield policy)."""
    FULL = "full"      # heavy and light tasks
    LIGHT = "light"    # light tasks only
    BUSY = "busy"      # online, no new tasks
    OFF = "off"        # owner needs the machine; nothing new
    OFFLINE = "offline"  # set by the core only, after missed heartbeats


class Weight(str, Enum):
    LIGHT = "light"
    HEAVY = "heavy"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Resources(BaseModel):
    cpu_cores: Optional[int] = None
    ram_gb: Optional[float] = None
    gpu_mem_gb: Optional[float] = None
    battery: bool = False


class RegisterRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    persona: str = "unnamed"
    os: str
    modules: list[str] = []
    resources: Resources = Resources()
    simulated: bool = False


class RegisterResponse(BaseModel):
    device_id: str
    token: str
    heartbeat_interval_s: int = HEARTBEAT_INTERVAL_S


class Heartbeat(BaseModel):
    mode: Mode
    load_pct: Optional[float] = None
    free_gpu_mem_gb: Optional[float] = None
    battery_pct: Optional[float] = None
    on_power: Optional[bool] = None


class NewTask(BaseModel):
    module: str
    payload: dict[str, Any] = {}
    weight: Weight = Weight.LIGHT


class Task(BaseModel):
    id: int
    module: str
    payload: dict[str, Any]
    weight: Weight
    status: TaskStatus
    node_id: Optional[str] = None


class TaskResult(BaseModel):
    ok: bool
    result: dict[str, Any] = {}
    error: Optional[str] = None
    log_lines: list[str] = []


class NodeStatus(BaseModel):
    device_id: str
    persona: str
    os: str
    mode: Mode
    modules: list[str]
    last_seen_s_ago: Optional[float]
    simulated: bool
