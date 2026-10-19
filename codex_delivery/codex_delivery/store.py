"""Owner-scoped catalog and durable inputs, deliberately without execution rights.

Local commit precedes a bounded replica call; no DB transaction spans the network.
One pending mutation blocks subsequent writes until its immutable snapshot is
confirmed. Production replica enrollment, trusted-origin HTTP and execution are
separate gates. The caller must be a trusted service, never model arguments.
"""
from __future__ import annotations

from contextlib import contextmanager, closing
from dataclasses import asdict
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

from .durability import Replica, ReplicaReceipt, Snapshot, digest, verify_receipt

SHANGHAI = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = 1
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
ALIAS = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")


class DeliveryError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def now() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="microseconds")


def reference(value: str) -> str:
    if not isinstance(value, str) or not REF.fullmatch(value):
        raise DeliveryError("reference_invalid")
    return value


def text(value: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise DeliveryError("text_invalid")
    return value


def alias(value: str) -> str:
    if not isinstance(value, str) or not ALIAS.fullmatch(value):
        raise DeliveryError("alias_invalid")
    return value


def identifier(prefix: str) -> str:
    return prefix + "-" + os.urandom(16).hex()


class DeliveryStore:
    def __init__(self, path: Path, *, local_domain: str, replica_domain: str = "",
                 replica: Replica | None = None, recovery_mode: bool = False):
        self.path = Path(path)
        self.local_domain = reference(local_domain)
        self.replica_domain = reference(replica_domain) if replica_domain else ""
        self.replica = replica
        self.recovery_mode = recovery_mode
        self._lock = threading.RLock()
        self._writer_depth = 0
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise DeliveryError("storage_path_invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Do not make an existing shared directory private as a side effect.
        if self.path.parent.stat().st_mode & 0o077:
            raise DeliveryError("storage_permissions_invalid")
        if self.path.exists() and self.path.stat().st_mode & 0o077:
            raise DeliveryError("storage_permissions_invalid")
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        with closing(self._connect()) as c:
            version = c.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise DeliveryError("schema_version_unsupported")
            if version == 0:
                if c.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]:
                    raise DeliveryError("schema_unknown")
                c.executescript(Path(__file__).with_name("schema.sql").read_text())
            self.recovery_mode = recovery_mode or bool(c.execute("SELECT recovery_required FROM runtime_guard WHERE singleton=1").fetchone()[0])
            if c.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise DeliveryError("database_corrupt")
        self._fsync_parent()

    def _fsync_parent(self) -> None:
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, isolation_level=None, timeout=3)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=FULL")
        c.execute("PRAGMA trusted_schema=OFF")
        return c

    @contextmanager
    def _writer_guard(self) -> Iterator[None]:
        # Serialize the entire commit/snapshot/replica handshake across processes.
        # SQLite alone cannot serialize the period outside its transaction.
        with self._lock:
            if self._writer_depth:
                self._writer_depth += 1
                try:
                    yield
                finally:
                    self._writer_depth -= 1
                return
            lock_path = self.path.with_suffix(self.path.suffix + ".writer.lock")
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                if os.fstat(fd).st_mode & 0o077:
                    raise DeliveryError("storage_permissions_invalid")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise DeliveryError("writer_busy") from exc
                self._writer_depth = 1
                try:
                    yield
                finally:
                    self._writer_depth = 0
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._writer_guard(), closing(self._connect()) as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def _owned(self, c: sqlite3.Connection, table: str, id_column: str,
               owner: str, ref: str) -> dict[str, Any]:
        row = c.execute(f"SELECT * FROM {table} WHERE {id_column}=? AND owner_ref=?",
                        (reference(ref), reference(owner))).fetchone()
        if row is None:
            raise DeliveryError("not_found")
        return dict(row)

    def _project(self, c: sqlite3.Connection, owner: str, project_id: str,
                 expected: int | None = None) -> dict[str, Any]:
        row = self._owned(c, "projects", "project_id", owner, project_id)
        if expected is not None and (type(expected) is not int or row["revision"] != expected):
            raise DeliveryError("revision_conflict")
        return row

    def _mutation(self, owner: str, key: str, kind: str, arguments: dict[str, Any],
                  apply: Callable[[sqlite3.Connection, str], dict[str, Any]]) -> dict[str, Any]:
        reference(owner); reference(key)
        if self.recovery_mode:
            raise DeliveryError("recovery_read_only")
        argument_digest = digest(canonical({"kind": kind, "arguments": arguments}).encode())
        with self._writer_guard():
            with self._transaction() as c:
                old = c.execute("SELECT * FROM commands WHERE owner_ref=? AND idempotency_key=?",
                                (owner, key)).fetchone()
                if old:
                    if old["argument_digest"] != argument_digest:
                        raise DeliveryError("idempotency_conflict")
                    result = json.loads(old["result_json"])
                    event_id = old["event_id"]
                else:
                    if c.execute("SELECT 1 FROM durability WHERE state='pending' LIMIT 1").fetchone():
                        raise DeliveryError("durability_pending")
                    timestamp = now()
                    try:
                        result = apply(c, timestamp)
                    except sqlite3.IntegrityError as exc:
                        raise DeliveryError("catalog_conflict") from exc
                    event_id = identifier("EV")
                    previous = c.execute("SELECT event_digest FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
                    body = {"event_id": event_id, "owner_ref": owner,
                            "kind": kind, "arguments": arguments, "result": result,
                            "created_at": timestamp,
                            "previous_digest": previous[0] if previous else ""}
                    body_json = canonical(body)
                    c.execute("INSERT INTO events(event_id,owner_ref,project_id,task_id,kind,body_json,created_at,previous_digest,event_digest) VALUES (?,?,?,?,?,?,?,?,?)",
                              (event_id, owner, result.get("project_id"), result.get("task_id"), kind,
                               body_json, timestamp, body["previous_digest"], digest(body_json.encode())))
                    c.execute("INSERT INTO commands VALUES (?,?,?,?,?)",
                              (owner, key, argument_digest, event_id, canonical(result)))
                    c.execute("INSERT INTO durability VALUES (?,NULL,'pending',NULL)", (event_id,))
                    c.execute("INSERT INTO notification_outbox VALUES (?,'queued',NULL,?)", (event_id, timestamp))
            return {**result, **self.reconcile(event_id)}

    def snapshot(self, event_id: str) -> Snapshot:
        """Freeze bytes once, before attempting remote I/O; retry identical bytes.

        A timed-out verified upload may already exist remotely. Regenerating a DB
        snapshot under that event ID would destroy idempotent reconciliation.
        """
        reference(event_id)
        if self.recovery_mode:
            raise DeliveryError("recovery_read_only")
        with self._writer_guard(), closing(self._connect()) as c:
            row = c.execute("SELECT snapshot_digest FROM durability WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise DeliveryError("not_found")
            folder = self.path.parent / (self.path.name + ".snapshots")
            if folder.is_symlink():
                raise DeliveryError("storage_path_invalid")
            folder.mkdir(mode=0o700, exist_ok=True)
            self._fsync_parent()
            if folder.stat().st_mode & 0o077:
                raise DeliveryError("storage_permissions_invalid")
            path = folder / (event_id + ".sqlite3")
            if path.is_symlink():
                raise DeliveryError("storage_path_invalid")
            if not path.exists():
                if row["snapshot_digest"]:
                    raise DeliveryError("snapshot_missing")
                temporary = folder / (identifier("staging") + ".sqlite3")
                fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                with closing(sqlite3.connect(temporary)) as backup:
                    c.backup(backup)
                    # A file-backed destination can leave WAL mode completely;
                    # an in-memory PRAGMA does not rewrite a WAL image header.
                    backup.execute("PRAGMA journal_mode=DELETE")
                fd = os.open(temporary, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.rename(temporary, path)
                directory = os.open(folder, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            if path.stat().st_mode & 0o077:
                raise DeliveryError("storage_permissions_invalid")
            content = path.read_bytes()
            sha256 = digest(content)
            if row["snapshot_digest"] and row["snapshot_digest"] != sha256:
                raise DeliveryError("snapshot_digest_invalid")
            with self._transaction() as writer:
                writer.execute("UPDATE durability SET snapshot_digest=? WHERE event_id=? AND snapshot_digest IS NULL", (sha256, event_id))
            return Snapshot(event_id, content, sha256)

    def reconcile(self, event_id: str) -> dict[str, Any]:
        """Only the internal replica worker may call this; never trust a tool receipt."""
        with self._writer_guard():
            with closing(self._connect()) as c:
                row = c.execute("SELECT * FROM durability WHERE event_id=?", (reference(event_id),)).fetchone()
            if row is None:
                raise DeliveryError("not_found")
            if row["state"] == "confirmed":
                return {"status": "accepted", "event_id": event_id, "durability": "replicated"}
            if self.recovery_mode or self.replica is None or not self.replica_domain:
                return {"status": "durability_pending", "event_id": event_id, "durability": "local_only"}
            snapshot = self.snapshot(event_id)
            try:
                receipt = self.replica.save(snapshot)
                verify_receipt(snapshot, receipt, local_domain=self.local_domain,
                               replica_domain=self.replica_domain)
            except Exception:
                # Do not log adapter exceptions, which can contain authenticated URLs.
                return {"status": "durability_pending", "event_id": event_id, "durability": "local_only"}
            with self._transaction() as c:
                c.execute("UPDATE durability SET snapshot_digest=?,state='confirmed',receipt_json=? WHERE event_id=? AND state='pending'",
                          (snapshot.sha256, canonical(asdict(receipt)), event_id))
            return {"status": "accepted", "event_id": event_id, "durability": "replicated"}

    def enroll_repository(self, owner: str, key: str, *, repo_alias: str,
                          remote_identity: str) -> dict[str, Any]:
        alias(repo_alias)
        # Administrative provider identity, never a URL, credential, or arbitrary path.
        if (not isinstance(remote_identity, str)
                or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", remote_identity)
                or any(part in {".", ".."} for part in remote_identity.split(":")[-1].split("/"))):
            raise DeliveryError("remote_identity_invalid")
        def apply(c: sqlite3.Connection, timestamp: str) -> dict[str, Any]:
            repo_id = identifier("RP")
            c.execute("INSERT INTO repositories VALUES (?,?,?,?,1,'enrolled',?)",
                      (repo_id, owner, repo_alias, remote_identity, timestamp))
            return {"repo_id": repo_id, "revision": 1}
        return self._mutation(owner, key, "repository.enroll", {"repo_alias": repo_alias, "remote_identity": remote_identity}, apply)

    def create_project(self, owner: str, key: str, *, project_alias: str, name: str) -> dict[str, Any]:
        alias(project_alias); text(name, 100)
        def apply(c: sqlite3.Connection, timestamp: str) -> dict[str, Any]:
            project_id = identifier("PJ")
            c.execute("INSERT INTO projects VALUES (?,?,?,?,'proposed',1,?,?)",
                      (project_id, owner, project_alias, name, timestamp, timestamp))
            c.execute("INSERT INTO project_versions VALUES (?,1,?,?)",
                      (project_id, canonical({"name": name, "state": "proposed", "repositories": []}), timestamp))
            return {"project_id": project_id, "revision": 1}
        return self._mutation(owner, key, "project.create", {"alias": project_alias, "name": name}, apply)

    def _save_project_version(self, c: sqlite3.Connection, project: dict[str, Any],
                              timestamp: str, state: str | None = None) -> int:
        revision = project["revision"] + 1
        bindings = [dict(x) for x in c.execute("SELECT repo_id,access,paths_json FROM project_repositories WHERE project_id=? ORDER BY repo_id", (project["project_id"],))]
        next_state = state or project["state"]
        c.execute("UPDATE projects SET state=?,revision=?,updated_at=? WHERE project_id=?",
                  (next_state, revision, timestamp, project["project_id"]))
        c.execute("INSERT INTO project_versions VALUES (?,?,?,?)",
                  (project["project_id"], revision, canonical({"name": project["name"], "state": next_state, "repositories": bindings}), timestamp))
        return revision

    def bind_repository(self, owner: str, key: str, *, project_id: str, repo_id: str,
                        expected_revision: int, access: str = "read",
                        paths: tuple[str, ...] = ()) -> dict[str, Any]:
        if access not in {"read", "write"}:
            raise DeliveryError("access_invalid")
        if paths:
            # Path-limited export/commit service is not present yet. Never issue a full bundle.
            raise DeliveryError("path_isolation_unavailable")
        def apply(c: sqlite3.Connection, timestamp: str) -> dict[str, Any]:
            project = self._project(c, owner, project_id, expected_revision)
            repo = self._owned(c, "repositories", "repo_id", owner, repo_id)
            if project["state"] == "archived" or repo["status"] != "enrolled":
                raise DeliveryError("catalog_inactive")
            c.execute("INSERT INTO project_repositories VALUES (?,?,?,?,?) ON CONFLICT(project_id,repo_id) DO UPDATE SET access=excluded.access,paths_json=excluded.paths_json",
                      (owner, project_id, repo_id, access, "[]"))
            revision = self._save_project_version(c, project, timestamp)
            return {"project_id": project_id, "revision": revision}
        return self._mutation(owner, key, "project.bind", {"project_id": project_id, "repo_id": repo_id, "expected_revision": expected_revision, "access": access}, apply)

    def project_control(self, owner: str, key: str, *, project_id: str,
                        expected_revision: int, action: str) -> dict[str, Any]:
        transitions = {"activate": {"proposed": "active"}, "pause": {"active": "paused"},
                       "resume": {"paused": "active"},
                       "archive": {"proposed": "archived", "active": "archived", "paused": "archived"}}
        if action not in transitions:
            raise DeliveryError("action_invalid")
        def apply(c: sqlite3.Connection, timestamp: str) -> dict[str, Any]:
            project = self._project(c, owner, project_id, expected_revision)
            state = transitions[action].get(project["state"])
            if state is None:
                raise DeliveryError("transition_invalid")
            if state == "active" and not c.execute("SELECT 1 FROM project_repositories WHERE project_id=?", (project_id,)).fetchone():
                raise DeliveryError("repository_set_missing")
            revision = self._save_project_version(c, project, timestamp, state)
            # No runner exists in this foundation. Do not claim running work was stopped.
            return {"project_id": project_id, "revision": revision, "state": state}
        return self._mutation(owner, key, "project.control", {"project_id": project_id, "expected_revision": expected_revision, "action": action}, apply)

    def plan(self, owner: str, key: str, *, project_id: str, expected_project_revision: int,
             conversation_ref: str, source_event_ref: str, requirement: str,
             mode: str = "design") -> dict[str, Any]:
        reference(conversation_ref); reference(source_event_ref); text(requirement, 16000)
        if mode not in {"design", "develop", "deliver", "deploy"}:
            raise DeliveryError("mode_invalid")
        def apply(c: sqlite3.Connection, timestamp: str) -> dict[str, Any]:
            project = self._project(c, owner, project_id, expected_project_revision)
            if project["state"] in {"paused", "archived"} or (mode != "design" and project["state"] != "active"):
                raise DeliveryError("project_inactive")
            task_id = identifier("DL")
            c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,1,'planning','queued',?,?)",
                      (task_id, owner, project_id, conversation_ref, source_event_ref, mode,
                       requirement, project["revision"], timestamp, timestamp))
            c.execute("INSERT INTO task_repositories SELECT owner_ref,project_id,?,repo_id,access,paths_json FROM project_repositories WHERE project_id=?", (task_id, project_id))
            return {"project_id": project_id, "task_id": task_id, "revision": 1, "state": "queued", "execution_enabled": False}
        return self._mutation(owner, key, "task.plan", {"project_id": project_id, "expected_project_revision": expected_project_revision, "conversation_ref": conversation_ref, "source_event_ref": source_event_ref, "requirement": requirement, "mode": mode}, apply)

    def revise(self, owner: str, key: str, *, project_id: str, task_id: str,
               expected_revision: int, supplement: str) -> dict[str, Any]:
        text(supplement, 16000)
        def apply(c: sqlite3.Connection, timestamp: str) -> dict[str, Any]:
            task = self._owned(c, "tasks", "task_id", owner, task_id)
            if task["project_id"] != project_id:
                raise DeliveryError("not_found")
            if type(expected_revision) is not int or task["revision"] != expected_revision:
                raise DeliveryError("revision_conflict")
            if task["state"] == "cancelled":
                raise DeliveryError("task_terminal")
            self._project(c, owner, project_id)
            new_requirement = task["requirement"] + "\n\n" + supplement
            text(new_requirement, 64000)
            revision = task["revision"] + 1
            c.execute("UPDATE tasks SET requirement=?,revision=?,updated_at=? WHERE task_id=?",
                      (new_requirement, revision, timestamp, task_id))
            return {"project_id": project_id, "task_id": task_id, "revision": revision}
        return self._mutation(owner, key, "task.revise", {"project_id": project_id, "task_id": task_id, "expected_revision": expected_revision, "supplement": supplement}, apply)

    def control(self, owner: str, key: str, *, project_id: str, task_id: str,
                expected_revision: int, action: str) -> dict[str, Any]:
        transitions = {"pause": {"queued": "paused"}, "resume": {"paused": "queued"},
                       "cancel": {"queued": "cancelled", "paused": "cancelled"}}
        if action not in transitions:
            raise DeliveryError("action_invalid")
        def apply(c: sqlite3.Connection, timestamp: str) -> dict[str, Any]:
            task = self._owned(c, "tasks", "task_id", owner, task_id)
            if task["project_id"] != project_id:
                raise DeliveryError("not_found")
            if type(expected_revision) is not int or task["revision"] != expected_revision:
                raise DeliveryError("revision_conflict")
            state = transitions[action].get(task["state"])
            if state is None:
                raise DeliveryError("transition_invalid")
            project = self._project(c, owner, project_id)
            if action == "resume" and project["state"] != "active":
                raise DeliveryError("project_inactive")
            revision = task["revision"] + 1
            c.execute("UPDATE tasks SET state=?,revision=?,updated_at=? WHERE task_id=?",
                      (state, revision, timestamp, task_id))
            return {"project_id": project_id, "task_id": task_id, "revision": revision, "state": state}
        return self._mutation(owner, key, "task.control", {"project_id": project_id, "task_id": task_id,
                              "expected_revision": expected_revision, "action": action}, apply)

    def events(self, owner: str, *, project_id: str, task_id: str | None = None,
               after: int = 0, limit: int = 20) -> dict[str, Any]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise DeliveryError("pagination_invalid")
        with closing(self._connect()) as c:
            self._project(c, owner, project_id)
            if task_id is not None:
                task = self._owned(c, "tasks", "task_id", owner, task_id)
                if task["project_id"] != project_id:
                    raise DeliveryError("not_found")
            where = "e.owner_ref=? AND e.project_id=? AND e.sequence>?"
            values: list[Any] = [owner, project_id, after]
            if task_id:
                where += " AND e.task_id=?"; values.append(task_id)
            c.execute("BEGIN")
            total = c.execute("SELECT count(*) FROM events e WHERE " + where, values).fetchone()[0]
            rows = [dict(r) for r in c.execute("SELECT e.sequence,e.event_id,e.project_id,e.task_id,e.kind,e.created_at,d.state AS durability FROM events e JOIN durability d USING(event_id) WHERE " + where + " ORDER BY e.sequence LIMIT ?", [*values, limit])]
            c.execute("COMMIT")
        return {"items": rows, "total": total, "has_more": len(rows) < total,
                "next_after": rows[-1]["sequence"] if rows else after}

    def task(self, owner: str, project_id: str, task_id: str) -> dict[str, Any]:
        with closing(self._connect()) as c:
            task = self._owned(c, "tasks", "task_id", owner, task_id)
            if task["project_id"] != project_id:
                raise DeliveryError("not_found")
            task["repositories"] = [dict(x) for x in c.execute("SELECT repo_id,access,paths_json FROM task_repositories WHERE task_id=?", (task_id,))]
        return task

    def catalog(self, owner: str, *, kind: str, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        reference(owner)
        if kind not in {"projects", "repositories", "tasks"}:
            raise DeliveryError("catalog_invalid")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise DeliveryError("pagination_invalid")
        column = {"projects": "project_id", "repositories": "repo_id", "tasks": "task_id"}[kind]
        with closing(self._connect()) as c:
            c.execute("BEGIN")
            total = c.execute(f"SELECT count(*) FROM {kind} WHERE owner_ref=?", (owner,)).fetchone()[0]
            rows = [dict(r) for r in c.execute(f"SELECT * FROM {kind} WHERE owner_ref=? ORDER BY created_at,{column} LIMIT ? OFFSET ?", (owner, limit, offset))]
            c.execute("COMMIT")
        # Listing is a local point-in-time view. No hidden fetch or remote mutation.
        return {"items": rows, "total": total, "has_more": offset + len(rows) < total,
                "next_offset": offset + len(rows) if offset + len(rows) < total else None}

    def claim_notification(self) -> dict[str, Any] | None:
        """Durable before-send claim. Unknown is never automatically reclaimed."""
        if self.recovery_mode:
            raise DeliveryError("recovery_read_only")
        with self._transaction() as c:
            row = c.execute("SELECT e.* FROM notification_outbox o JOIN events e USING(event_id) JOIN durability d USING(event_id) WHERE o.state='queued' AND d.state='confirmed' ORDER BY e.sequence LIMIT 1").fetchone()
            if row is None:
                return None
            c.execute("UPDATE notification_outbox SET state='unknown',updated_at=? WHERE event_id=?", (now(), row["event_id"]))
            return {k: row[k] for k in ("event_id", "owner_ref", "project_id", "task_id", "kind", "created_at")}

    def finish_notification(self, event_id: str, *, evidence: str) -> None:
        if self.recovery_mode:
            raise DeliveryError("recovery_read_only")
        if evidence not in {"ha_webhook_accepted", "gateway_accepted", "gateway_sent"}:
            raise DeliveryError("channel_evidence_invalid")
        with self._transaction() as c:
            changed = c.execute("UPDATE notification_outbox SET state='accepted',channel_evidence=?,updated_at=? WHERE event_id=? AND state='unknown'", (evidence, now(), reference(event_id))).rowcount
            if changed != 1:
                raise DeliveryError("notification_claim_missing")

    def verify_event_chain(self) -> int:
        previous = ""; count = 0
        with closing(self._connect()) as c:
            for row in c.execute("SELECT * FROM events ORDER BY sequence"):
                body = json.loads(row["body_json"])
                if (row["previous_digest"] != previous or body["previous_digest"] != previous
                        or digest(row["body_json"].encode()) != row["event_digest"]
                        or any(body[k] != row[k] for k in ("event_id", "owner_ref", "kind", "created_at"))):
                    raise DeliveryError("event_chain_invalid")
                previous = row["event_digest"]; count += 1
        return count

    @classmethod
    def restore(cls, path: Path, snapshot: Snapshot, receipt: ReplicaReceipt, *,
                local_domain: str, replica_domain: str) -> "DeliveryStore":
        """Offline restore into a new private namespace; execution stays disabled.

        A restored outbox is frozen until an operator establishes one active
        writer and reconciles external outcomes. Old notices are never replayed.
        """
        verify_receipt(snapshot, receipt, local_domain=local_domain, replica_domain=replica_domain)
        if digest(snapshot.content) != snapshot.sha256:
            raise DeliveryError("snapshot_digest_invalid")
        if path.exists() or path.is_symlink() or not path.parent.is_dir() or path.parent.is_symlink():
            raise DeliveryError("restore_destination_invalid")
        if path.parent.stat().st_mode & 0o077:
            raise DeliveryError("storage_permissions_invalid")
        # Inspect the trusted replica bytes in memory before creating any file.
        with closing(sqlite3.connect(":memory:")) as c:
            c.deserialize(snapshot.content)
            c.execute("PRAGMA trusted_schema=OFF")
            if (c.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
                    or c.execute("PRAGMA quick_check").fetchone()[0] != "ok"
                    or c.execute("PRAGMA foreign_key_check").fetchone() is not None):
                raise DeliveryError("snapshot_database_invalid")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as target:
            target.write(snapshot.content); target.flush(); os.fsync(target.fileno())
        with closing(sqlite3.connect(path, isolation_level=None)) as c:
            c.execute("PRAGMA synchronous=FULL")
            c.execute("UPDATE runtime_guard SET recovery_required=1 WHERE singleton=1")
        restored = cls(path, local_domain=local_domain, replica_domain=replica_domain, recovery_mode=True)
        restored.verify_event_chain()
        return restored
