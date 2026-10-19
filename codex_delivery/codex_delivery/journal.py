"""Durable local admission and a metadata-only background worker.

Admission never calls a replica or holds the metadata writer lock. A queued
entry is local_only and is not business acceptance. Only the worker may call
the fixed metadata methods below. This is not a Runner or a deployment queue.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import fcntl
import hashlib
import json
import os
import sqlite3
from typing import Any, Iterator

from .store import DeliveryError, DeliveryStore, canonical, now, reference

WRITES = frozenset({'plan', 'revise', 'control', 'project_control'})
# Only errors known to precede the metadata commit are terminal rejections.
# Snapshot/storage/commit failures can follow a successful local transaction.
REJECTIONS = frozenset({'reference_invalid', 'text_invalid', 'mode_invalid',
                        'not_found', 'revision_conflict', 'project_inactive',
                        'task_terminal', 'action_invalid', 'transition_invalid',
                        'catalog_conflict', 'idempotency_conflict',
                        'repository_set_missing', 'tool_unavailable'})


class InputJournal:
    def __init__(self, store: DeliveryStore, *, capacity: int = 128):
        if type(capacity) is not int or not 1 <= capacity <= 1024:
            raise DeliveryError('journal_capacity_invalid')
        self.store = store
        self.capacity = capacity

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        # Deliberately independent of the long replica handshake writer lock.
        # SQLite still serializes all local commits with synchronous=FULL.
        with closing(self.store._connect()) as c:
            c.execute('PRAGMA busy_timeout=250')
            try:
                c.execute('BEGIN IMMEDIATE')
                yield c
                c.execute('COMMIT')
            except sqlite3.OperationalError as exc:
                if c.in_transaction:
                    c.execute('ROLLBACK')
                raise DeliveryError('journal_unavailable') from exc
            except BaseException:
                if c.in_transaction:
                    c.execute('ROLLBACK')
                raise

    def submit(self, name: str, arguments: dict[str, Any], origin: dict[str, Any]) -> dict[str, Any]:
        """Trusted provider only: origin must already have been authenticated."""
        if self.store.recovery_mode:
            raise DeliveryError('recovery_read_only')
        if name not in WRITES:
            raise DeliveryError('tool_unavailable')
        resource = arguments.get('task_ref', arguments['project_ref'])
        owner = origin['actor_ref']
        identity = {k: origin[k] for k in ('actor_ref', 'source_event_ref', 'input_revision')}
        identity.update(name=name, resource_ref=reference(resource))
        operation = 'OP-' + hashlib.sha256(canonical(identity).encode()).hexdigest()
        with self.transaction() as c:
            old = c.execute('SELECT * FROM input_journal WHERE operation_id=?', (operation,)).fetchone()
            if old is not None:
                if (old['call_digest'] != origin['call_digest'] or old['conversation_ref'] != origin['conversation_ref']
                        or json.loads(old['origin_json'])['input_digest'] != origin['input_digest']):
                    raise DeliveryError('idempotency_conflict')
                return self._public(old)
            if c.execute("SELECT count(*) FROM input_journal WHERE state NOT IN ('accepted','rejected')").fetchone()[0] >= self.capacity:
                raise DeliveryError('journal_full')
            timestamp = now()
            c.execute("INSERT INTO input_journal(operation_id,owner_ref,conversation_ref,source_event_ref,input_revision,name,resource_ref,call_digest,payload_json,origin_json,created_at,updated_at,state,result_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'queued','{}')",
                      (operation, owner, origin['conversation_ref'], origin['source_event_ref'], origin['input_revision'],
                       name, resource, origin['call_digest'], canonical(arguments), canonical(origin), timestamp, timestamp))
            return self._public(c.execute('SELECT * FROM input_journal WHERE operation_id=?', (operation,)).fetchone())

    def status(self, owner: str, operation: str) -> dict[str, Any]:
        with closing(self.store._connect()) as c:
            row = c.execute('SELECT * FROM input_journal WHERE owner_ref=? AND operation_id=?',
                            (reference(owner), reference(operation))).fetchone()
        if row is None:
            raise DeliveryError('not_found')
        return self._public(row)

    def _public(self, row: sqlite3.Row) -> dict[str, Any]:
        result = json.loads(row['result_json'])
        response: dict[str, Any] = {'operation_ref': row['operation_id'], 'execution_enabled': False}
        if row['state'] == 'accepted':
            response.update(status='accepted', durability='replicated')
        elif row['state'] == 'rejected':
            response.update(status='rejected', code=result['code'])
        else:
            response.update(status='durability_pending', durability='local_only',
                            code='recovery_read_only' if self.store.recovery_mode else
                            'durability_pending' if row['state'] == 'durability_pending' else 'awaiting_processing')
        for original, exposed in (('task_id', 'task_ref'), ('project_id', 'project_ref'),
                                  ('event_id', 'event_ref'), ('revision', 'revision')):
            if original in result:
                response[exposed] = result[original]
        return response


class MetadataWorker:
    """A single background writer, safe to retry only these local metadata calls."""
    def __init__(self, journal: InputJournal):
        self.journal = journal
        self.store = journal.store

    def run_once(self) -> bool:
        if self.store.recovery_mode:
            raise DeliveryError('recovery_read_only')
        path = self.store.path.with_suffix(self.store.path.suffix + '.metadata-worker.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_mode & 0o077:
                raise DeliveryError('storage_permissions_invalid')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DeliveryError('worker_busy') from exc
            with self.journal.transaction() as c:
                row = c.execute("SELECT * FROM input_journal WHERE state NOT IN ('accepted','rejected') ORDER BY sequence LIMIT 1").fetchone()
                if row is None:
                    return False
                c.execute("UPDATE input_journal SET state='running',updated_at=? WHERE operation_id=?", (now(), row['operation_id']))
            try:
                result = self._apply(row)
            except DeliveryError as exc:
                if exc.code in {'durability_pending', 'writer_busy', 'journal_unavailable'}:
                    # An older unconfirmed event cannot be skipped by a new input.
                    result = {'status': 'durability_pending'}
                    state = 'durability_pending'
                elif exc.code in REJECTIONS:
                    result = {'code': exc.code}
                    state = 'rejected'
                else:
                    raise DeliveryError('worker_result_unknown') from None
            except Exception:
                # A crash after commit is replayed under the same durable key.
                # Never expose exceptions, authenticated URLs or candidate text.
                raise DeliveryError('worker_result_unknown') from None
            else:
                state = 'accepted' if result['status'] == 'accepted' else 'durability_pending'
            with self.journal.transaction() as c:
                c.execute('UPDATE input_journal SET state=?,result_json=?,updated_at=? WHERE operation_id=?',
                          (state, canonical(result), now(), row['operation_id']))
            return True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _apply(self, row: sqlite3.Row) -> dict[str, Any]:
        args = json.loads(row['payload_json'])
        origin = json.loads(row['origin_json'])
        owner, key = row['owner_ref'], row['operation_id']
        name = row['name']
        project = args['project_ref']
        if name == 'plan':
            return self.store.plan(owner, key, project_id=project,
                                   expected_project_revision=args['expected_project_revision'],
                                   conversation_ref=origin['conversation_ref'], source_event_ref=origin['source_event_ref'],
                                   requirement=args['requirement'], mode=args['mode'])
        if name == 'revise':
            return self.store.revise(owner, key, project_id=project, task_id=args['task_ref'],
                                     expected_revision=args['expected_revision'], supplement=args['supplement'])
        if name == 'control':
            return self.store.control(owner, key, project_id=project, task_id=args['task_ref'],
                                      expected_revision=args['expected_revision'], action=args['action'])
        if name == 'project_control':
            return self.store.project_control(owner, key, project_id=project,
                                              expected_revision=args['expected_revision'], action=args['action'])
        raise DeliveryError('tool_unavailable')
