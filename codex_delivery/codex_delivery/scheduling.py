"""Read-only project turns recovered from this Runner's confirmed Work claims.

This orders attempts only. The original dispatch lock, admission checks and
claim CAS remain the sole authority to execute; no scheduler cursor is written.
"""
from collections import deque
from datetime import datetime, timedelta
import json

from .durability import digest
from .store import DeliveryError, SHANGHAI, canonical, reference

WAIT_PROMOTION_SECONDS = 300


def _fail():
    raise DeliveryError('scheduling_history_invalid') from None


def _bucket(task):
    owner = reference(task['owner_ref'])
    if task['task_scope'] == 'owner_work' and task['project_id'] is None:
        return owner, 'owner_work', ''
    if task['task_scope'] == 'project' and task['project_id'] is not None:
        return owner, 'project', reference(task['project_id'])
    _fail()


def _instant(value):
    instant = datetime.fromisoformat(value)
    if instant.utcoffset() is None:
        _fail()
    return instant


def confirmed_turns(connection, runner_ref):
    """Latest original confirmed claim per owner/project, for one Runner.

    Work claim events do not carry project_id in their result. The original
    owner/task link joins the canonical task's immutable project/scope instead;
    neither current revision nor a later runtime result replaces that claim.
    """
    reference(runner_ref)
    rows = connection.execute("""
        WITH history AS (
            SELECT e.*, d.state AS turn_durability, t.task_id AS linked_task_id,
                   t.project_id AS bucket_project_id, t.task_scope, t.mode AS task_mode,
                   ROW_NUMBER() OVER (
                       PARTITION BY e.owner_ref,t.task_scope,t.project_id ORDER BY e.sequence DESC
                   ) AS turn_rank
            FROM events e LEFT JOIN durability d USING(event_id)
            LEFT JOIN tasks t ON t.task_id=e.task_id AND t.owner_ref=e.owner_ref
            WHERE e.kind='work.claim'
              AND json_extract(e.body_json,'$.arguments.runner_ref')=?
              AND (d.state='confirmed' OR d.event_id IS NULL)
        )
        SELECT history.*, c.owner_ref AS command_owner, c.argument_digest, c.result_json,
               (SELECT COUNT(*) FROM commands aliases WHERE aliases.event_id=history.event_id) AS command_count
        FROM history LEFT JOIN commands c ON c.event_id=history.event_id
        WHERE turn_rank=1 OR linked_task_id IS NULL OR turn_durability IS NULL
        ORDER BY sequence
        """, (runner_ref,)).fetchall()
    turns = {}
    try:
        for row in rows:
            if (row['command_count'] != 1 or row['command_owner'] != row['owner_ref']
                    or row['linked_task_id'] is None or row['task_mode'] != 'work'
                    or row['turn_durability'] != 'confirmed'):
                _fail()
            body = json.loads(row['body_json'])
            result = json.loads(row['result_json'])
            arguments = body['arguments']
            if (canonical(body) != row['body_json']
                    or digest(row['body_json'].encode()) != row['event_digest']
                    or body['event_id'] != row['event_id'] or body['owner_ref'] != row['owner_ref']
                    or body['kind'] != 'work.claim' or body['created_at'] != row['created_at']
                    or body['previous_digest'] != row['previous_digest']
                    or canonical(result) != row['result_json']
                    or body['result'] != result or result['task_id'] != row['task_id']
                    or arguments['task_id'] != row['task_id']
                    or arguments['runner_ref'] != runner_ref or result['runner_ref'] != runner_ref
                    or type(result['revision']) is not int or result['revision'] != arguments['expected_revision']
                    or result['state'] != 'running'
                    or result['execution_enabled'] is not True
                    or row['argument_digest'] != digest(canonical(dict(kind='work.claim', arguments=arguments)).encode())):
                _fail()
            key = _bucket(dict(owner_ref=row['owner_ref'], task_scope=row['task_scope'],
                               project_id=row['bucket_project_id']))
            turns[key] = row['sequence'], _instant(row['created_at'])
    except (KeyError, TypeError, ValueError, OverflowError):
        _fail()
    return turns


def ordered_tasks(tasks, turns, *, checked_at=None):
    """FIFO within each bucket, interleaved by least recently confirmed service.

    A bucket waiting 300 seconds since max(head creation, last confirmed claim)
    is promoted over younger buckets. Promotion is capped at one level; all
    promoted buckets still rotate by confirmed claim sequence, never by how many
    old tasks they accumulated. This is a finite promotion threshold, not a
    promise about task completion time or eligibility.
    """
    checked_at = datetime.now(SHANGHAI) if checked_at is None else checked_at
    if not isinstance(checked_at, datetime) or checked_at.utcoffset() is None:
        _fail()
    buckets = {}
    try:
        for row in tasks:
            task = dict(row)
            buckets.setdefault(_bucket(task), []).append(task)
        for rows in buckets.values():
            rows.sort(key=lambda row: (_instant(row['created_at']), row['task_id']))

        def order(key):
            head = buckets[key][0]
            created = _instant(head['created_at'])
            sequence, served_at = turns.get(key, (0, created))
            waiting_since = max(created, served_at)
            promoted = checked_at - waiting_since >= timedelta(seconds=WAIT_PROMOTION_SECONDS)
            return (not promoted, sequence, created, head['task_id'], key)

        queues = [deque(buckets[key]) for key in sorted(buckets, key=order)]
        result = []
        while queues:
            remaining = []
            for queue in queues:
                result.append(queue.popleft())
                if queue:
                    remaining.append(queue)
            queues = remaining
        return result
    except (KeyError, TypeError, ValueError, OverflowError):
        _fail()
