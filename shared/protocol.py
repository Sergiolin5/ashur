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
    reason: Optional[str] = None          # why this mode, in Spanish ("batería al 22%")
    modules: Optional[list[str]] = None   # sent on the first beat after start, so new modules are known
    code_version: Optional[str] = None    # first beat: which code this agent runs
    fresh_start: Optional[bool] = None    # first beat: whatever the core thinks it was running is gone
    cpu_pct: Optional[float] = Field(default=None, ge=0, le=100)
    ram_pct: Optional[float] = Field(default=None, ge=0, le=100)
    gpu_pct: Optional[float] = Field(default=None, ge=0, le=100)
    load_pct: Optional[float] = None
    free_gpu_mem_gb: Optional[float] = None
    battery_pct: Optional[float] = None
    on_power: Optional[bool] = None


class NewTask(BaseModel):
    module: str
    payload: dict[str, Any] = {}
    weight: Weight = Weight.LIGHT
    target: Optional[str] = None      # only this node may take it


class InventoryRow(BaseModel):
    path: str = Field(max_length=4096)
    size: Optional[int] = None
    kind: str = "file"                # file, folder_summary, sensitive
    modified: Optional[float] = None
    hash: Optional[str] = None
    runnable: bool = False


class InventoryBatch(BaseModel):
    reset: bool = False               # first batch of a scan clears the node's old rows
    rows: list[InventoryRow] = Field(default_factory=list, max_length=5000)


class Task(BaseModel):
    id: int
    module: str
    payload: dict[str, Any]
    weight: Weight
    status: TaskStatus
    node_id: Optional[str] = None


class TaskResult(BaseModel):
    ok: bool
    requeue: bool = False                 # paused (yield, restart): put it back, it will resume
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
    code_version: Optional[str] = None
    code_label: Optional[str] = None      # the core version that shipped that code, e.g. "0.11.0"
    reason: Optional[str] = None
    cpu_pct: Optional[float] = None       # None = no reading (offline, or not readable on that OS)
    ram_pct: Optional[float] = None
    gpu_pct: Optional[float] = None
