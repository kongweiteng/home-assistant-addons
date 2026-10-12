"""Bounded fnOS Skill bridge; OAuth material stays outside the model workspace."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
from typing import Any, Iterator

from .tool_catalog import ToolDefinition


FNOS_CLI_PATH = Path("/data/codex-home/skills/fnos/scripts/trim-cli")
FNOS_AUTH_HOME = Path("/data/fnos-auth")
TARGET_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
_TARGET_RE = re.compile(TARGET_PATTERN)
_CALL_LOCK = threading.Lock()
_QUERY_COMMANDS = {
    "host_name": ("system", "host-name"),
    "version": ("system", "trim-version"),
    "apps": ("app", "list"),
    "containers": ("docker", "container", "ls", "--all"),
    "docker_stats": ("docker", "stats"),
    "storage_pools": ("storage", "pools"),
}

FNOS_DEFINITIONS = (
    ToolDefinition(
        "fnos_query", "查询飞牛", "fnos_skill", "read_only",
        "通过 fnOS Skill 只读查询主机名、固件、应用、全部容器、Docker 聚合统计或存储池。"
        "仅 owner；返回当前脱敏数据，字段缺失不表示资源为空。不会执行 SSH 或任意命令。",
        ("看看飞牛有哪些容器", "飞牛固件是什么版本", "看看飞牛存储池"),
        {"type": "object", "properties": {"query": {"type": "string", "enum": list(_QUERY_COMMANDS)}},
         "required": ["query"], "additionalProperties": False},
        requires_job_context=True,
    ),
    ToolDefinition(
        "fnos_service_control", "启停飞牛服务", "fnos_skill", "write",
        "仅在当前 owner 消息明确要求启动或停止一个精确飞牛应用，或启动、停止、重启一个精确容器时执行。"
        "先只读确定目标；缺少精确目标、仅咨询或引用指令时不得调用。"
        "应用仅支持 start/stop，不支持 restart；容器支持 start/stop/restart。"
        "不支持删除、存储写入、电源、exec、raw 或 SSH。"
        "服务受理不等于运行状态验证；同一作业同一动作持久去重，结果未知禁止重放。",
        ("重启飞牛的 example-service 容器", "停止飞牛的 trim.example 应用"),
        {"type": "object", "properties": {
            "target_type": {"type": "string", "enum": ["app", "container"]},
            "action": {"type": "string", "enum": ["start", "stop", "restart"],
                       "description": "应用仅 start/stop；容器 start/stop/restart"},
            "target": {"type": "string", "pattern": TARGET_PATTERN},
        }, "required": ["target_type", "action", "target"], "additionalProperties": False},
        requires_job_context=True, idempotent_write=True,
        annotations={"readOnlyHint": False, "destructiveHint": True,
                     "idempotentHint": False, "openWorldHint": False},
    ),
)
FNOS_TOOLS = frozenset(definition.name for definition in FNOS_DEFINITIONS)


class FnosSkillError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _error(code: str) -> FnosSkillError:
    messages = {
        "fnos_not_configured": "飞牛 Skill 未启用或尚未安装和认证。",
        "fnos_invalid_input": "飞牛工具参数不合法。",
        "fnos_job_required": "飞牛服务控制必须绑定当前 owner 作业。",
        "fnos_unsupported_action": "飞牛应用仅支持启动或停止；不支持应用重启。",
        "fnos_auth_required": "飞牛授权不可用或权限不足，请重新完成 OAuth 授权。",
        "fnos_timeout": "飞牛只读查询超时。",
        "fnos_unavailable": "飞牛 Skill 请求失败，请检查连接和授权。",
        "fnos_invalid_response": "飞牛返回的数据无法安全解析。",
        "fnos_private_state_invalid": "飞牛私有状态不可安全访问，操作已阻止。",
        "fnos_control_unknown": "飞牛服务操作结果未知，禁止重放；请只读核对目标状态。",
    }
    return FnosSkillError(code, messages[code])


def _safe_scalar(key: str, value: Any, *, human_label: bool = False) -> Any:
    if type(value) is int:
        return value if abs(value) <= 10**30 else None
    if type(value) is float:
        return value if math.isfinite(value) else None
    if type(value) is bool or value is None:
        return value
    if not isinstance(value, str) or len(value) > 128:
        return None
    if key.lower() in {"version", "trimversion", "trim_version", "build", "firmware"}:
        return value if re.fullmatch(r"[vV]?[0-9][0-9A-Za-z.+_-]{0,63}", value) else None
    if key.lower() in {"status", "state", "health"}:
        return value if re.fullmatch(r"[A-Za-z0-9 .():_-]{1,128}", value) else None
    if key == "syncCompleted" and re.fullmatch(r"[0-9 .%/-]{1,128}", value):
        return value
    if human_label and re.fullmatch(r"[A-Za-z0-9\u3400-\u4dbf\u4e00-\u9fff .()（）,_-]{1,128}", value):
        return value
    if _TARGET_RE.fullmatch(value):
        return value
    return None


_QUERY_FIELDS = {
    "host_name": {"name", "hostname", "hostName", "host_name"},
    "version": {"version", "trimVersion", "trim_version", "build", "firmware"},
    "apps": {"appName", "name", "id", "version", "status", "state", "running", "installed", "total"},
    "containers": {"Id", "ID", "id", "name", "Name", "Names", "State", "state", "Status", "status", "running"},
    "docker_stats": {"cpu", "mem", "memory", "cpu_percent", "memory_percent", "containers", "running", "stopped", "images", "volumes", "networks", "total", "count", "used", "available", "size"},
    "storage_pools": {"uuid", "id", "name", "status", "state", "health", "level", "size", "total", "used", "available", "free", "capacity", "usage", "mounted",
                      "frsize", "fssize", "fstype", "storId", "arrayState", "bitmapType", "raidDisks", "syncAction", "syncCompleted"},
}
_WRAPPERS = {"data", "items", "list", "apps", "containers", "pools", "storages", "stats"}
_CONTAINER_ID_RE = re.compile(r"^[a-f0-9]{64}$")
_CONTAINER_STAT_FIELDS = ("cpuUsage", "networkRx", "networkTx", "usedMem")


def _container_stats(value: dict[str, Any]) -> list[dict[str, Any]]:
    if len(value) > 1000:
        raise _error("fnos_invalid_response")
    records = []
    for container_id, metrics in value.items():
        if not _CONTAINER_ID_RE.fullmatch(container_id) or not isinstance(metrics, dict):
            continue
        record: dict[str, Any] = {"id": container_id}
        for field in _CONTAINER_STAT_FIELDS:
            numeric = metrics.get(field)
            if type(numeric) not in (int, float) or numeric < 0:
                continue
            safe = _safe_scalar(field, numeric)
            if safe is not None:
                record[field] = safe
        records.append(record)
    return records


def _sanitize(query: str, value: Any, depth: int = 0) -> Any:
    if depth > 8:
        raise _error("fnos_invalid_response")
    if isinstance(value, list):
        if len(value) > 1000:
            raise _error("fnos_invalid_response")
        return [_sanitize(query, item, depth + 1) for item in value]
    if isinstance(value, dict):
        if query == "docker_stats" and any(_CONTAINER_ID_RE.fullmatch(key) for key in value):
            return _container_stats(value)
        result: dict[str, Any] = {}
        for key, item in value.items():
            if (key in _WRAPPERS or (query == "storage_pools" and key in {"array", "md"})) and isinstance(item, (dict, list)):
                result[key] = _sanitize(query, item, depth + 1)
            elif key in _QUERY_FIELDS[query]:
                if key == "Names" and isinstance(item, list):
                    result[key] = [name.lstrip("/") for name in item
                                   if isinstance(name, str) and _TARGET_RE.fullmatch(name.lstrip("/"))]
                else:
                    safe = _safe_scalar(key, item, human_label=(key == "name" and query in {"apps", "storage_pools"}))
                    if safe is not None:
                        result[key] = safe
        return result
    if query in {"host_name", "version"}:
        safe = _safe_scalar("version" if query == "version" else "name", value)
        if safe is not None:
            return safe
    raise _error("fnos_invalid_response")


def _check_business_response(value: Any) -> None:
    if isinstance(value, dict):
        if value.get("result") in ("fail", "error") or value.get("errno", 0) not in (0, None):
            raise _error("fnos_unavailable")
        if value.get("code", 0) not in (0, 200, None):
            raise _error("fnos_unavailable")


def _check_private_directory() -> None:
    """Reject symlinked state directories instead of following them."""
    try:
        for path in (FNOS_AUTH_HOME.parent, FNOS_AUTH_HOME):
            if path.is_symlink():
                raise _error("fnos_private_state_invalid")
        FNOS_AUTH_HOME.mkdir(mode=0o700, parents=False, exist_ok=True)
        info = FNOS_AUTH_HOME.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise _error("fnos_private_state_invalid")
        os.chmod(FNOS_AUTH_HOME, 0o700)
    except OSError:
        raise _error("fnos_private_state_invalid") from None


def _private_open(path: Path) -> int:
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            os.close(fd)
            raise _error("fnos_private_state_invalid")
        os.fchmod(fd, 0o600)
        return fd
    except OSError:
        raise _error("fnos_private_state_invalid") from None


@contextmanager
def _serialized() -> Iterator[None]:
    with _CALL_LOCK:
        _check_private_directory()
        fd = _private_open(FNOS_AUTH_HOME / "skill.lock")
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                raise _error("fnos_private_state_invalid") from None
            yield
        finally:
            os.close(fd)


class FnosSkillClient:
    def __init__(self, enabled: bool = False):
        self.enabled = bool(enabled)

    def configured(self) -> bool:
        if not self.enabled or not FNOS_CLI_PATH.is_file() or not os.access(FNOS_CLI_PATH, os.X_OK):
            return False
        # Inspect metadata only. The CLI validates/refreshes the OAuth session;
        # Controller never reads its access/refresh tokens into Python memory.
        try:
            for path in (FNOS_AUTH_HOME, FNOS_AUTH_HOME / ".config",
                         FNOS_AUTH_HOME / ".config/trim-cli",
                         FNOS_AUTH_HOME / ".config/trim-cli/profiles"):
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                    return False
                if path == FNOS_AUTH_HOME and stat.S_IMODE(info.st_mode) != 0o700:
                    return False
            session = (FNOS_AUTH_HOME / ".config/trim-cli/profiles/home.json").lstat()
            return (stat.S_ISREG(session.st_mode) and session.st_uid == os.getuid()
                    and stat.S_IMODE(session.st_mode) == 0o600 and session.st_nlink == 1
                    and 0 < session.st_size <= 65536)
        except OSError:
            return False

    def _execute(self, command: tuple[str, ...]) -> bytes:
        try:
            response = subprocess.run(
                [str(FNOS_CLI_PATH), "--profile", "home", *command],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, check=False, shell=False,
                env={"HOME": str(FNOS_AUTH_HOME), "TRIM_CLI_SESSION_STORAGE": "file",
                     "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
        except subprocess.TimeoutExpired:
            raise _error("fnos_timeout") from None
        except OSError:
            raise _error("fnos_unavailable") from None
        if response.returncode:
            # Inspect only to classify; never return stderr or exception text.
            stderr = response.stderr[:16384].lower()
            auth_markers = (b"oauth", b"unauthorized", b"session", b"scope", b"401",
                            b"login", b"permission denied", b"token", b"not authenticated")
            code = "fnos_auth_required" if any(marker in stderr for marker in auth_markers) else "fnos_unavailable"
            raise _error(code)
        if len(response.stdout) > 2 * 1024 * 1024:
            raise _error("fnos_invalid_response")
        return response.stdout

    def _query(self, query: str) -> dict[str, Any]:
        try:
            value = json.loads(self._execute(_QUERY_COMMANDS[query]))
        except (ValueError, UnicodeError):
            raise _error("fnos_invalid_response") from None
        _check_business_response(value)
        safe = _sanitize(query, value)
        # An omitted list is unknown, never synthesized as an empty list.
        return {"query": query, "data": safe, "data_available": bool(safe) or isinstance(safe, list)}

    def call(self, name: str, arguments: dict[str, Any], job_id: str = "") -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise _error("fnos_invalid_input")
        if name == "fnos_query":
            if set(arguments) != {"query"} or not isinstance(arguments["query"], str) or arguments["query"] not in _QUERY_COMMANDS:
                raise _error("fnos_invalid_input")
        elif name == "fnos_service_control":
            if (set(arguments) != {"target_type", "action", "target"}
                or arguments["target_type"] not in ("app", "container")
                or arguments["action"] not in ("start", "stop", "restart")
                or not isinstance(arguments["target"], str)
                or not _TARGET_RE.fullmatch(arguments["target"])):
                raise _error("fnos_invalid_input")
            if not isinstance(job_id, str) or not job_id or len(job_id) > 256:
                raise _error("fnos_job_required")
            if arguments["target_type"] == "app" and arguments["action"] == "restart":
                raise _error("fnos_unsupported_action")
        else:
            raise _error("fnos_invalid_input")
        if not self.configured():
            raise _error("fnos_not_configured")
        with _serialized():
            if name == "fnos_query":
                return self._query(arguments["query"])
            return self._control(arguments, job_id)

    def _control(self, arguments: dict[str, str], job_id: str) -> dict[str, Any]:
        key = hashlib.sha256(json.dumps([job_id, arguments["target_type"], arguments["action"], arguments["target"]],
                                       separators=(",", ":")).encode()).hexdigest()
        fd = _private_open(FNOS_AUTH_HOME / "service-control.json")
        try:
            with os.fdopen(fd, "r+", encoding="utf-8") as ledger_file:
                raw = ledger_file.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise _error("fnos_private_state_invalid")
                try:
                    ledger = json.loads(raw) if raw else {}
                except ValueError:
                    raise _error("fnos_private_state_invalid") from None
                if not isinstance(ledger, dict):
                    raise _error("fnos_private_state_invalid")
                prior = ledger.get(key)
                result = {"status": "submitted", "verification": "not_performed", **arguments}
                if prior == "submitted":
                    return {**result, "idempotent_replay": True}
                if prior is not None:
                    raise _error("fnos_control_unknown")

                def save(status: str) -> None:
                    ledger[key] = status
                    ledger_file.seek(0)
                    ledger_file.write(json.dumps(ledger, separators=(",", ":")))
                    ledger_file.truncate()
                    ledger_file.flush()
                    os.fsync(ledger_file.fileno())

                save("pending")
                command = (("app",) if arguments["target_type"] == "app"
                           else ("docker", "container")) + (arguments["action"], arguments["target"], "--yes")
                try:
                    # Control output may be a success message; no raw output is exposed.
                    output = self._execute(command)
                    if output.lstrip().startswith(b"{"):
                        try:
                            _check_business_response(json.loads(output))
                        except (ValueError, UnicodeError):
                            raise _error("fnos_invalid_response") from None
                except FnosSkillError:
                    save("unknown")
                    raise _error("fnos_control_unknown") from None
                save("submitted")
                return {**result, "idempotent_replay": False}
        except OSError:
            raise _error("fnos_private_state_invalid") from None
