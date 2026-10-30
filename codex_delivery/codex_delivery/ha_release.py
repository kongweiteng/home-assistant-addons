"""Fixed HA release coordination, not a second task database or authority.

The coordinator holds a Manager/Broker port, never a Supervisor credential.
SupervisorReleaseAdapter is instantiated ONLY inside the trusted Manager/Broker
domain. No service route or production authorization is enabled by importing
this candidate module. Unknown external outcomes are fenced and only observed.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
import fcntl
import hashlib
from http.client import HTTPException
import json
import os
import re
import sqlite3
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .store import DeliveryError, DeliveryStore, SHANGHAI, canonical, reference, _verify_release_schema

SHA = re.compile(r"sha256:[a-f0-9]{64}\Z")
SLUG = re.compile(r"[a-z0-9][a-z0-9_]{0,63}\Z")
VERSION = re.compile(r"[0-9][A-Za-z0-9.+_-]{0,63}\Z")
MAX_RESPONSE = 1024 * 1024
CONTROL_COMPONENTS = ("codex_controller", "codex_delivery", "weixin_gateway", "codex_runner_relay",
    "ha_operations_broker", "ha_manager_executor", "codex_dev_runner")


class HAReleaseError(DeliveryError):
    def __init__(self, code: str, *, unknown: bool = False):
        super().__init__(code)
        self.unknown = unknown


def sha(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def _hash(value: Any) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise HAReleaseError("release_digest_invalid")
    return value


def _time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise HAReleaseError("release_time_invalid")
    return value.astimezone(SHANGHAI).isoformat()


@dataclass(frozen=True)
class HARelease:
    release_ref: str
    version: str
    source_digest: str
    artifact_identity: dict[str, str]
    permissions_digest: str
    data_compatible: bool = True
    schema_version: int = 2

    def document(self) -> dict[str, Any]:
        reference(self.release_ref)
        if not isinstance(self.version, str) or not VERSION.fullmatch(self.version):
            raise HAReleaseError("release_version_invalid")
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise HAReleaseError('release_schema_unsupported')
        artifact_identity(self.artifact_identity)
        for value in (self.source_digest, self.permissions_digest):
            _hash(value)
        if type(self.data_compatible) is not bool:
            raise HAReleaseError("release_compatibility_invalid")
        return asdict(self)

    @property
    def digest(self) -> str:
        return sha(self.document())


@dataclass(frozen=True)
class HATarget:
    target_ref: str
    instance_ref: str
    project_ref: str
    slug: str
    allowed_releases: frozenset[str]
    rollback_release_digest: str | None = None
    canary_no_business_data: bool = False
    require_runtime_identity: bool = True

    def validate(self) -> None:
        for value in (self.target_ref, self.instance_ref, self.project_ref):
            reference(value)
        if (not isinstance(self.slug, str) or not SLUG.fullmatch(self.slug) or self.slug in {"self", "core", "supervisor"}
                or any(self.slug == component or self.slug.endswith("_" + component) for component in CONTROL_COMPONENTS)):
            raise HAReleaseError("release_target_invalid")
        if not self.allowed_releases or len(self.allowed_releases) > 16:
            raise HAReleaseError("release_registry_invalid")

        for value in self.allowed_releases:
            _hash(value)
        if self.rollback_release_digest is not None:
            _hash(self.rollback_release_digest)
        if type(self.canary_no_business_data) is not bool or self.require_runtime_identity is not True:
            raise HAReleaseError("release_registry_invalid")

    def document(self) -> dict[str, Any]:
        self.validate()
        return {**asdict(self), "allowed_releases": sorted(self.allowed_releases)}


@dataclass(frozen=True)
class HAReleaseRequest:
    deployment_ref: str
    owner_ref: str
    task_ref: str
    task_revision: int
    project_ref: str
    target_ref: str
    release_digest: str
    baseline_etag: str
    action: str
    grant_ref: str
    deadline: datetime
    schema_version: int = 2

    def document(self) -> dict[str, Any]:
        for value in (self.deployment_ref, self.owner_ref, self.task_ref, self.project_ref,
                self.target_ref, self.grant_ref):
            reference(value)
        if type(self.schema_version) is not int or self.schema_version!=2:raise HAReleaseError('release_schema_unsupported')
        _hash(self.release_digest); _hash(self.baseline_etag)
        if type(self.task_revision) is not int or not 1 <= self.task_revision <= 2**53 - 1:
            raise HAReleaseError("release_revision_invalid")
        if self.action not in {"install", "update"}:
            raise HAReleaseError("release_action_invalid")
        return {**asdict(self), "deadline": _time(self.deadline)}


def baseline_etag(observation: Mapping[str, Any]) -> str:
    """Include config/permissions hashes; never persist raw Add-on options."""
    required = {"slug", "installed", "version", "state", "config_digest", "permissions_digest", "docker_image_id"}
    if not required.issubset(observation) or type(observation["installed"]) is not bool:
        raise HAReleaseError("release_baseline_invalid")
    _hash(observation["config_digest"]); _hash(observation["permissions_digest"])
    if observation['installed'] and artifact_identity(observation['docker_image_id'])['kind'] != 'docker_image_id':
        raise HAReleaseError('release_baseline_invalid')
    if not observation['installed'] and observation['docker_image_id'] is not None:
        raise HAReleaseError('release_baseline_invalid')
    return sha({key: observation[key] for key in sorted(required)})


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class FixedSupervisorHTTP:
    """Transport reused from an enrolled Broker Supervisor client, in its domain.

    No public path/body method is exposed to models. The fixed adapter below
    selects every path and payload. Fixture loopback access is explicit.
    """
    def __init__(self, broker_supervisor: Any, *, fixture_loopback: bool = False):
        parsed = urlsplit(broker_supervisor._base_url)
        allowed = parsed.hostname == "supervisor" or (fixture_loopback and parsed.hostname == "127.0.0.1")
        if (not allowed or parsed.scheme != "http" or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise HAReleaseError("supervisor_domain_invalid")
        self._base = broker_supervisor._base_url.rstrip("/")
        self._token = broker_supervisor._token
        self._timeout = broker_supervisor._timeout_seconds
        if not self._token or not 0 < self._timeout <= 30:
            raise HAReleaseError("supervisor_transport_invalid")
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        # Defense in depth even though only trusted adapter code calls this.
        if method not in {"GET", "POST"} or not re.fullmatch(
                r"/(?:addons/[a-z0-9_]+/(?:info|update|start|stop|rebuild)|store/(?:reload|addons/[a-z0-9_]+(?:/install)?)|backups/(?:new/partial|[a-zA-Z0-9_-]+/info))", path):
            raise HAReleaseError("supervisor_action_invalid")
        request = Request(self._base + path, method=method,
            data=canonical(body or {}).encode() if method == "POST" else None,
            headers={"Authorization": "Bearer " + self._token, "Accept": "application/json",
                "Content-Type": "application/json", "User-Agent": "ha-manager-release-candidate/1"})
        try:
            deadline = time.monotonic() + self._timeout
            with self._opener.open(request, timeout=self._timeout) as response:
                chunks = []
                size = 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("response_deadline")
                    connection = response.fp.raw._sock
                    connection.settimeout(remaining)
                    block = response.read1(min(64 * 1024, MAX_RESPONSE + 1 - size))
                    if not block:
                        break
                    chunks.append(block)
                    size += len(block)
                    if size > MAX_RESPONSE:
                        raise ValueError("response_size")
                    if response.isclosed():
                        break
                raw = b"".join(chunks)
            if len(raw) > MAX_RESPONSE:
                raise ValueError("response_size")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("result") != "ok":
                raise ValueError("response_shape")
            data = payload.get("data", {})
            if data is None:
                data = {}
            if not isinstance(data, dict):
                raise ValueError("response_data")
            return data
        except (HTTPError, URLError, HTTPException, EOFError, TimeoutError, OSError, ValueError, AttributeError) as exc:
            raise HAReleaseError("supervisor_result_unknown" if method == "POST" else "supervisor_observation_unavailable",
                unknown=method == "POST") from exc


class SupervisorReleaseAdapter:
    """Concrete fixed Supervisor actions, instantiated only in Manager/Broker.

    Old-source restore/rebuild is unqualified for cross-version rollback and is
    refused. No qualified backup restore or uninstall action exists here.
    """
    def __init__(self, http: FixedSupervisorHTTP, *, instance_ref: str,
            artifact_evidence: Callable[[HATarget, HARelease], Mapping[str, Any]],
            backup_verifier: Callable[[str, Mapping[str, Any], HATarget, str], Mapping[str, Any]],
            health_probe: Callable[[HATarget, HARelease], Mapping[str, Any]],
            restore_canary_source: Callable[[HATarget, HARelease], Mapping[str, Any]] | None = None,
            empty_target_backup: Callable[[HATarget, str, str], Mapping[str, Any]] | None = None,
            runtime_image_observer: Callable[[HATarget], Mapping[str, Any]] | None = None):
        self.http = http
        self.instance_ref = reference(instance_ref)
        self.artifact_evidence = artifact_evidence
        self.backup_verifier = backup_verifier
        self.health_probe = health_probe
        self.restore_canary_source = restore_canary_source
        self.empty_target_backup = empty_target_backup
        self.runtime_image_observer = runtime_image_observer

    def _runtime(self, target):
        if self.runtime_image_observer is None:
            raise HAReleaseError('release_runtime_observer_unavailable')
        return runtime_artifact_fact(dict(self.runtime_image_observer(target)), target_ref=target.target_ref,
            instance_ref=target.instance_ref, slug=target.slug)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        result = self.http.request("POST", path, body)
        selected = {key: result[key] for key in ("slug", "job_id") if key in result}
        if any(not isinstance(value, str) or len(value) > 128 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value)
                for value in selected.values()):
            raise HAReleaseError("release_result_unknown", unknown=True)
        return selected

    def observe(self, target: HATarget) -> dict[str, Any]:
        if target.instance_ref != self.instance_ref:
            raise HAReleaseError("release_target_mismatch")
        store = self.http.request("GET", f"/store/addons/{target.slug}")
        if store.get("slug") != target.slug or type(store.get("installed")) is not bool:
            raise HAReleaseError("release_target_mismatch")
        data = self.http.request("GET", f"/addons/{target.slug}/info") if store["installed"] else store
        if data.get("slug") != target.slug:
            raise HAReleaseError("release_target_mismatch")
        permissions = {key: data.get(key) for key in ("hassio_role", "hassio_api", "homeassistant_api",
            "host_network", "host_pid", "host_ipc", "full_access", "privileged", "docker_api", "ingress")}
        configuration = {key: data.get(key) for key in ("options", "boot", "watchdog", "auto_update", "network")}
        return {"slug": target.slug, "installed": store["installed"], "version": data.get("version"),
            "version_latest": store.get("version_latest"), "state": data.get("state", "not_installed"),
            "available": store.get("available") is True, "config_digest": sha(configuration),
            "permissions_digest": sha(permissions),
            "docker_image_id": self._runtime(target)['docker_image_id'] if store['installed'] else None}

    def qualify(self, target: HATarget, release: HARelease, *, rollback: bool = False) -> dict[str, Any]:
        if target.instance_ref != self.instance_ref:
            raise HAReleaseError("release_target_mismatch")
        proof = dict(self.artifact_evidence(target, release))
        expected = {"release_digest": release.digest, "source_digest": release.source_digest,
            "schema_version": 2, "artifact_identity": release.artifact_identity, "permissions_digest": release.permissions_digest,
            "source_immutable": True, "artifact_verified": True}
        if any(proof.get(key) != value for key, value in expected.items()):
            raise HAReleaseError("release_artifact_unverified")
        if rollback and (not target.canary_no_business_data or self.restore_canary_source is None):
            raise HAReleaseError("rollback_capability_unavailable")
        return expected

    def perform(self, step: str, target: HATarget, release: HARelease, *,
            operation_ref: str, baseline: str, action: str) -> dict[str, Any]:
        if target.instance_ref != self.instance_ref:
            raise HAReleaseError("release_target_mismatch")
        if step == "backup":
            if action == "install":
                if not target.canary_no_business_data or self.empty_target_backup is None:
                    raise HAReleaseError("backup_capability_unavailable")
                return dict(self.empty_target_backup(target, operation_ref, baseline))
            return self._post("/backups/new/partial", {"name": operation_ref,
                "addons": [target.slug], "homeassistant": False, "folders": [], "background": False})
        if step in {"install", "update"}:
            self.qualify(target, release)
            observation = self.observe(target)
            if observation["version_latest"] != release.version or not observation["available"]:
                raise HAReleaseError("release_store_drift")
            if observation["installed"] != (step == "update"):
                raise HAReleaseError("release_installation_drift")
            path = f"/store/addons/{target.slug}/install" if step == "install" else f"/addons/{target.slug}/update"
            # Supervisor 2026.07.5 source: install accepts background only;
            # update accepts background/backup, NEVER a requested version.
            return self._post(path, {"background": False})
        if step == "restore_source":
            # Supervisor 2026.10.4 rebuild requires installed.version ==
            # store.version. Restoring old source cannot downgrade an installed
            # newer version. A separately qualified backup restore is required.
            raise HAReleaseError("rollback_adapter_unqualified")
        if step in {"reload", "rebuild"}:
            raise HAReleaseError("rollback_adapter_unqualified")
        if step in {"start", "stop"}:
            return self._post(f"/addons/{target.slug}/{step}", {})
        raise HAReleaseError("release_action_invalid")

    def verify_backup(self, target: HATarget, result: Mapping[str, Any], baseline: str, *,
            current: datetime, action: str) -> dict[str, Any]:
        slug = result.get("slug")
        if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", slug):
            raise HAReleaseError("backup_identity_unavailable")
        info = self.http.request("GET", f"/backups/{slug}/info") if action == "update" else dict(result)
        if action == "update":
            covered = info.get("addons", [])
            if info.get("slug") != slug or not any(isinstance(item, dict) and item.get("slug") == target.slug for item in covered):
                raise HAReleaseError("backup_scope_invalid")
        proof = dict(self.backup_verifier(slug, info, target, baseline))
        # Reuse the existing Broker's completed/readable/off-device evidence
        # validator. This adapter runs in that domain, not the Delivery image.
        from ha_operations_broker.operations_broker.contract import validate_backup_evidence, ContractError
        try:
            validated = validate_backup_evidence(proof, now=current)
        except ContractError as exc:
            raise HAReleaseError(exc.code) from exc
        if validated["scope"] != "addon" or validated["baseline"] != baseline:
            raise HAReleaseError("backup_baseline_mismatch")
        return validated

    def health(self, target: HATarget, release: HARelease) -> dict[str, Any]:
        observed = self.observe(target)
        evidence = dict(self.health_probe(target, release))
        healthy = (observed["installed"] and observed["state"] == "started" and observed["version"] == release.version
            and evidence.get("healthy") is True and evidence.get("version") == release.version
            and evidence.get("source_digest") == release.source_digest)
        runtime_identity = artifact_matches(release.artifact_identity, self._runtime(target))
        if not runtime_identity:
            healthy = False
        return {"healthy": bool(healthy), "version": observed["version"], "state": observed["state"],
            "config_digest": observed["config_digest"], "permissions_digest": observed["permissions_digest"],
            "source_verified": evidence.get("source_digest") == release.source_digest,
            "schema_version": 2, "artifact_identity": release.artifact_identity if runtime_identity else None,
            "runtime_identity_observed": runtime_identity}


class HAReleaseCoordinator:
    """Persistent operation/step journal in the one existing Delivery SQLite."""
    def __init__(self, store: DeliveryStore, *, targets: Mapping[str, HATarget],
            releases: Mapping[str, HARelease], manager: Any,
            authorize: Callable[[HAReleaseRequest, str, str], bool],
            clock: Callable[[], datetime] | None = None):
        self.store = store
        self.targets, self.releases = dict(targets), dict(releases)
        self.manager, self.authorize = manager, authorize
        self.clock = clock or (lambda: datetime.now(SHANGHAI))
        for key, target in self.targets.items():
            target.validate()
            if key != target.target_ref:
                raise HAReleaseError("release_registry_invalid")
        for key, release in self.releases.items():
            if key != release.digest:
                raise HAReleaseError("release_registry_invalid")
        with closing(self.store._connect()) as c:
            _verify_release_schema(c)

    @contextmanager
    def _lock(self, target: HATarget):
        resource = target.instance_ref + ":" + target.slug
        name = self.store.path.name + ".ha-release-" + hashlib.sha256(resource.encode()).hexdigest() + ".lock"
        fd = os.open(self.store.path.parent / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_mode & 0o077:
                raise HAReleaseError("storage_permissions_invalid")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise HAReleaseError("release_target_busy") from exc
            yield
        finally:
            os.close(fd)

    def _configuration(self, request: HAReleaseRequest):
        request.document()
        target = self.targets.get(request.target_ref)
        release = self.releases.get(request.release_digest)
        if (target is None or release is None or target.project_ref != request.project_ref
                or release.digest not in target.allowed_releases or not release.data_compatible):
            raise HAReleaseError("release_not_enrolled")
        return target, release

    def _bound_digest(self, request: HAReleaseRequest) -> str:
        target, _ = self._configuration(request)
        return sha({"request": request.document(), "target_policy": target.document()})

    def _gate(self, request: HAReleaseRequest, step: str) -> None:
        if self.store.recovery_mode:
            raise HAReleaseError("recovery_read_only")
        if request.deadline <= self.clock():
            raise HAReleaseError("release_authorization_expired")
        with closing(self.store._connect()) as c:
            task = c.execute("SELECT t.*,p.state AS project_state,p.revision AS current_project_revision FROM tasks t JOIN projects p ON p.project_id=t.project_id WHERE t.task_id=? AND t.owner_ref=? AND t.project_id=?",
                (request.task_ref, request.owner_ref, request.project_ref)).fetchone()
            if task is None or task["mode"] != "deploy" or task["task_scope"] != "project":
                raise HAReleaseError("release_task_scope_invalid")
            if (task["revision"] != request.task_revision or task["project_revision"] != task["current_project_revision"]
                    or task["project_state"] != "active" or task["state"] not in {"queued", "running"} or task["control_pending"]):
                raise HAReleaseError("release_task_changed")
            if c.execute("SELECT recovery_required FROM runtime_guard WHERE singleton=1").fetchone()[0]:
                raise HAReleaseError("recovery_read_only")
            if c.execute("SELECT 1 FROM durability WHERE state='pending' LIMIT 1").fetchone():
                raise HAReleaseError("durability_pending")
        scope = sha({"bound_request_digest": self._bound_digest(request), "step": step})
        try:
            allowed = self.authorize(request, step, scope)
        except Exception:
            raise HAReleaseError("release_authorization_unavailable") from None
        if allowed is not True:
            raise HAReleaseError("release_authorization_revoked")

    def _change(self, request: HAReleaseRequest, suffix: str, apply: Callable) -> None:
        key = "HA-" + hashlib.sha256((request.deployment_ref + ":" + suffix).encode()).hexdigest()
        result = self.store._mutation(request.owner_ref, key, "ha_release_" + suffix.replace(":", "_"),
            {"request_digest": self._bound_digest(request), "step": suffix}, apply)
        if result.get("status") != "accepted":
            raise HAReleaseError("durability_pending")

    def _register(self, request: HAReleaseRequest, target: HATarget):
        def apply(c, timestamp):
            if c.execute("SELECT 1 FROM ha_release_locks WHERE resource_ref=?", (target.instance_ref + ":" + target.slug,)).fetchone():
                raise HAReleaseError("release_target_busy")
            c.execute("INSERT INTO ha_release_operations VALUES(?,?,?,?,?,?,?,'running','{}',?,?)",
                (request.deployment_ref, request.owner_ref, request.task_ref, request.project_ref, target.target_ref,
                 self._bound_digest(request), canonical(request.document()), timestamp, timestamp))
            c.execute("INSERT INTO ha_release_locks VALUES(?,?)", (target.instance_ref + ":" + target.slug, request.deployment_ref))
            return {"task_id": request.task_ref, "project_id": request.project_ref, "deployment_ref": request.deployment_ref}
        self._change(request, "register", apply)

    def _step(self, request: HAReleaseRequest, target: HATarget, release: HARelease, step: str) -> dict[str, Any]:
        self._gate(request, step)
        operation = "HS-" + hashlib.sha256((request.deployment_ref + ":" + step).encode()).hexdigest()
        def claim(c, timestamp):
            c.execute("INSERT INTO ha_release_steps VALUES(?,?,?,'claimed','{}',?,?)",
                (request.deployment_ref, step, operation, timestamp, timestamp))
            return {"task_id": request.task_ref, "project_id": request.project_ref, "step_ref": operation}
        self._change(request, step + ":claim", claim)
        # Recheck revocation/deadline after the replica handshake, at the actual
        # external-write boundary. A persisted claim is never blindly retried.
        self._gate(request, step)
        try:
            result = self.manager.perform(step, target, release, operation_ref=operation,
                baseline=request.baseline_etag, action=request.action)
            if not isinstance(result, dict) or len(canonical(result).encode()) > 64 * 1024:
                raise HAReleaseError("release_result_unknown", unknown=True)
        except BaseException as exc:
            self._local_state(request, "recovery_required", {"code": "release_result_unknown", "step": step}, step=step)
            if isinstance(exc, HAReleaseError) and not exc.unknown:
                raise
            raise HAReleaseError("release_result_unknown", unknown=True) from None
        def settle(c, timestamp):
            c.execute("UPDATE ha_release_steps SET state='known',result_json=?,updated_at=? WHERE deployment_ref=? AND step=?",
                (canonical(result), timestamp, request.deployment_ref, step))
            return {"task_id": request.task_ref, "project_id": request.project_ref, "step_ref": operation}
        self._change(request, step + ":result", settle)
        return result

    def _local_state(self, request: HAReleaseRequest, state: str, evidence: dict[str, Any], *,
            step: str | None = None, unlock: bool = False):
        with self.store._transaction() as c:
            stamp = _time(self.clock())
            c.execute("UPDATE ha_release_operations SET state=?,evidence_json=?,updated_at=? WHERE deployment_ref=?",
                (state, canonical(evidence), stamp, request.deployment_ref))
            if step:
                c.execute("UPDATE ha_release_steps SET state='unknown',updated_at=? WHERE deployment_ref=? AND step=?",
                    (stamp, request.deployment_ref, step))
            if unlock:
                c.execute("DELETE FROM ha_release_locks WHERE deployment_ref=?", (request.deployment_ref,))

    def _evidence(self, request: HAReleaseRequest, label: str, value: dict[str, Any]):
        def apply(c, timestamp):
            old = json.loads(c.execute("SELECT evidence_json FROM ha_release_operations WHERE deployment_ref=?",
                (request.deployment_ref,)).fetchone()[0])
            old[label] = value
            c.execute("UPDATE ha_release_operations SET evidence_json=?,updated_at=? WHERE deployment_ref=?",
                (canonical(old), timestamp, request.deployment_ref))
            return {"task_id": request.task_ref, "project_id": request.project_ref, "evidence_ref": label}
        self._change(request, "evidence:" + label, apply)

    def _finish(self, request: HAReleaseRequest, state: str, evidence: dict[str, Any], *, unlock: bool):
        def apply(c, timestamp):
            c.execute("UPDATE ha_release_operations SET state=?,evidence_json=?,updated_at=? WHERE deployment_ref=?",
                (state, canonical(evidence), timestamp, request.deployment_ref))
            if unlock:
                c.execute("DELETE FROM ha_release_locks WHERE deployment_ref=?", (request.deployment_ref,))
            return {"task_id": request.task_ref, "project_id": request.project_ref, "deployment_ref": request.deployment_ref}
        try:
            self._change(request, "finish", apply)
        except BaseException:
            self._local_state(request, "recovery_required", {"code": "release_completion_unconfirmed"})
            target, _ = self._configuration(request)
            with self.store._transaction() as c:
                c.execute("INSERT OR IGNORE INTO ha_release_locks VALUES(?,?)",
                    (target.instance_ref + ":" + target.slug, request.deployment_ref))
            raise

    def _health(self, target: HATarget, release: HARelease) -> dict[str, Any]:
        for attempt in range(3):
            result = self.manager.health(target, release)
            if result.get("healthy") is True:
                return result
            if attempt < 2:
                time.sleep(.1)
        return result

    def status(self, owner_ref: str, deployment_ref: str) -> dict[str, Any]:
        reference(owner_ref); reference(deployment_ref)
        with closing(self.store._connect()) as c:
            row = c.execute("SELECT * FROM ha_release_operations WHERE deployment_ref=? AND owner_ref=?",
                (deployment_ref, owner_ref)).fetchone()
            if row is None:
                raise HAReleaseError("not_found")
            frozen=json.loads(row['request_json'])
            if type(frozen.get('schema_version')) is not int or frozen['schema_version']!=2:raise HAReleaseError('release_schema_unsupported')
            steps = c.execute("SELECT step,operation_ref,state,result_json FROM ha_release_steps WHERE deployment_ref=? ORDER BY created_at,step",
                (deployment_ref,)).fetchall()
        return {"deployment_ref": deployment_ref, "task_ref": row["task_ref"], "target_ref": row["target_ref"],
            "state": row["state"], "evidence": json.loads(row["evidence_json"]),
            "steps": [{"step": s["step"], "operation_ref": s["operation_ref"], "state": s["state"],
                "result": json.loads(s["result_json"])} for s in steps], "updated_at": row["updated_at"]}

    def reconcile(self, request: HAReleaseRequest) -> dict[str, Any]:
        """Only bounded read observations. Never POST or release an unknown lock."""
        target, release = self._configuration(request)
        result = self.status(request.owner_ref, request.deployment_ref)
        with closing(self.store._connect()) as c:
            row = c.execute("SELECT request_digest FROM ha_release_operations WHERE deployment_ref=?", (request.deployment_ref,)).fetchone()
        if row[0] != self._bound_digest(request):
            raise HAReleaseError("release_request_conflict")
        try:
            result["observation"] = self.manager.observe(target)
            result["health_observation"] = self.manager.health(target, release)
        except Exception:
            result["observation_code"] = "supervisor_observation_unavailable"
        result["automatic_replay_allowed"] = False
        return result

    def recover(self) -> list[dict[str, Any]]:
        """A startup fence, only while no coordinator owns the resource lock."""
        with closing(self.store._connect()) as c:
            rows = c.execute("SELECT request_json,request_digest FROM ha_release_operations WHERE state='running'").fetchall()
        current=[row for row in rows if type(json.loads(row[0]).get('schema_version')) is int and json.loads(row[0])['schema_version']==2]
        for row in current:
            body = json.loads(row[0]); body["deadline"] = datetime.fromisoformat(body["deadline"])
            request = HAReleaseRequest(**body)
            if row[1] != self._bound_digest(request):
                raise HAReleaseError("release_registry_drift")
            target, _ = self._configuration(request)
            with self._lock(target):
                self._local_state(request, "recovery_required", {"code": "worker_interrupted"})
                with self.store._transaction() as c:
                    c.execute("UPDATE ha_release_steps SET state='unknown' WHERE deployment_ref=? AND state='claimed'", (request.deployment_ref,))
        return [self.status(json.loads(row[0])["owner_ref"], json.loads(row[0])["deployment_ref"]) for row in current]

    def run(self, request: HAReleaseRequest) -> dict[str, Any]:
        target, release = self._configuration(request)
        with closing(self.store._connect()) as c:
            frozen=c.execute('SELECT request_json FROM ha_release_operations WHERE deployment_ref=?',(request.deployment_ref,)).fetchone()
        if frozen:
            prior=json.loads(frozen[0])
            if type(prior.get('schema_version')) is not int or prior['schema_version']!=2:raise HAReleaseError('release_schema_unsupported')
        with self._lock(target):
            with closing(self.store._connect()) as c:
                previous = c.execute("SELECT request_digest,state FROM ha_release_operations WHERE deployment_ref=?", (request.deployment_ref,)).fetchone()
            if previous:
                if previous["request_digest"] != self._bound_digest(request):
                    raise HAReleaseError("release_request_conflict")
                if previous["state"] == "running":
                    self._local_state(request, "recovery_required", {"code": "worker_interrupted"})
                return self.reconcile(request) if previous["state"] in {"running", "recovery_required"} else self.status(request.owner_ref, request.deployment_ref)
            self._gate(request, "register")
            self._register(request, target)
            applied = False
            try:
                observed = self.manager.observe(target)
                if baseline_etag(observed) != request.baseline_etag:
                    raise HAReleaseError("release_baseline_drift")
                if observed["installed"] != (request.action == "update"):
                    raise HAReleaseError("release_installation_drift")
                self.manager.qualify(target, release)
                old = self.releases.get(target.rollback_release_digest)
                if request.action == "update":
                    if (old is None or old.version != observed["version"] or not old.data_compatible
                            or old.permissions_digest != observed["permissions_digest"]):
                        raise HAReleaseError("rollback_release_unavailable")
                    self.manager.qualify(target, old, rollback=True)
                self._evidence(request, "preflight", observed)
                backup = self._step(request, target, release, "backup")
                backup_proof = self.manager.verify_backup(target, backup, request.baseline_etag,
                    current=self.clock(), action=request.action)
                self._evidence(request, "backup", backup_proof)
                if baseline_etag(self.manager.observe(target)) != request.baseline_etag:
                    raise HAReleaseError("release_baseline_drift")
                self._step(request, target, release, request.action)
                applied = True
                if request.action == "install":
                    self._step(request, target, release, "start")
                health = self._health(target, release)
                if (health["version"] != release.version or health["permissions_digest"] != release.permissions_digest
                        or (request.action == "update" and health["config_digest"] != observed["config_digest"])):
                    raise HAReleaseError("release_postflight_drift")
                if health["healthy"]:
                    self._finish(request, "completed", {"release_digest": release.digest,
                        "backup": backup_proof, "health": health, "backup_restored": False}, unlock=True)
                elif request.action == "install":
                    self._step(request, target, release, "stop")
                    self._finish(request, "failed_stopped", {"code": "release_health_failed", "data_preserved": True,
                        "backup": backup_proof, "backup_restored": False}, unlock=True)
                else:
                    assert old is not None
                    self._finish(request, "recovery_required",
                        {"code": "rollback_adapter_unqualified", "cause": "release_health_failed",
                         "original_deployment_succeeded": False, "rollback_release_digest": old.digest,
                         "health": health, "backup": backup_proof, "backup_restored": False}, unlock=False)
            except HAReleaseError as exc:
                current = self.status(request.owner_ref, request.deployment_ref)
                if current["state"] != "recovery_required":
                    pending = any(s["state"] in {"claimed", "unknown"} for s in current["steps"])
                    changed_service = applied or any(s["step"] in {"install", "update", "start", "stop", "restore_source", "reload", "rebuild"}
                        for s in current["steps"])
                    self._local_state(request, "recovery_required" if changed_service or pending else "blocked",
                        {"code": exc.code}, unlock=not changed_service and not pending)
            except BaseException:
                self._local_state(request, "recovery_required", {"code": "release_result_unknown"})
                raise
            return self.status(request.owner_ref, request.deployment_ref)


def _artifact_fail():
    raise HAReleaseError('release_artifact_identity_invalid')


def artifact_identity(value):
    """Closed identity union: an ImageID never satisfies an OCI identity."""
    if not isinstance(value, dict):
        _artifact_fail()
    kind = value.get('kind')
    expected = {'kind', 'digest'} if kind == 'docker_image_id' else {'kind', 'repository', 'digest'}
    if not isinstance(kind,str) or kind not in {'docker_image_id', 'oci_repo_digest'} or set(value) != expected:
        _artifact_fail()
    if not isinstance(value['digest'], str) or re.fullmatch(r'sha256:[a-f0-9]{64}', value['digest']) is None:
        _artifact_fail()
    if kind == 'oci_repo_digest' and (not isinstance(value['repository'], str)
            or len(value['repository']) > 440 or re.fullmatch(
                r'[a-z0-9][a-z0-9._-]*(?::[0-9]{1,5})?(?:/[a-z0-9][a-z0-9._-]*)+', value['repository']) is None):
        _artifact_fail()
    return dict(value)


def artifact_matches(expected, fact):
    expected = artifact_identity(expected)
    return (expected == fact['docker_image_id'] if expected['kind'] == 'docker_image_id'
            else expected in fact['oci_repo_digests'])


def runtime_artifact_fact(value, *, target_ref, instance_ref, slug):
    fields = {'version','target_ref','instance_ref','slug','container_id',
              'docker_image_id','oci_repo_digests','running','observed_at'}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value['version']) is not int or value['version'] != 1
            or value['target_ref'] != target_ref or value['instance_ref'] != instance_ref or value['slug'] != slug
            or not isinstance(value['container_id'], str) or re.fullmatch(r'[a-f0-9]{64}', value['container_id']) is None
            or value['running'] is not True):
        _artifact_fail()
    image = artifact_identity(value['docker_image_id'])
    if image['kind'] != 'docker_image_id':
        _artifact_fail()
    repositories = value['oci_repo_digests']
    if not isinstance(repositories, list) or len(repositories) > 64:
        _artifact_fail()
    seen = set()
    for item in repositories:
        item = artifact_identity(item)
        if item['kind'] != 'oci_repo_digest' or (item['repository'], item['digest']) in seen:
            _artifact_fail()
        seen.add((item['repository'], item['digest']))
    try:
        observed = datetime.fromisoformat(value['observed_at'])
        if observed.utcoffset() is None or not value['observed_at'].endswith('+08:00'):
            _artifact_fail()
    except (TypeError, ValueError):
        _artifact_fail()
    return value
