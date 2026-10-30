"""Canonical preview assignment gates; no task/lease issuer or HTTP inputs."""
from contextlib import closing
from datetime import datetime
import json

from .store import DeliveryError, SHANGHAI


class CanonicalPreviewAuthority:
    def __init__(self, store, runner_ref, identity, stop_identity):
        self.store, self.runner_ref = store, runner_ref
        self.identity, self.stop_identity = identity, stop_identity

    def open(self, request):
        row = self.store.work_task(request.owner_ref, request.task_ref)
        spec = json.loads(row['work_json'])
        if (request.profile != 'preview' or spec['profile'] != 'preview'
                or spec['runtime_id'] != request.runtime_ref
                or row['revision'] != request.input_revision
                or row['workspace_ref'] != request.workspace_ref
                or row['assigned_runner_ref'] != self.runner_ref
                or row['lease_ref'] != request.lease_id or row['lease_epoch'] != request.assignment_epoch
                or datetime.fromisoformat(row['lease_expires_at']) != request.expires_at
                or not self.store.lease_current(request.owner_ref, request.task_ref,
                    request.lease_id, request.assignment_epoch, runner_ref=self.runner_ref)):
            raise DeliveryError('preview_assignment_invalid')
        self.identity(self.runner_ref)
        return PreviewAssignment(self, row)


class PreviewAssignment:
    """Ephemeral immutable original scope; restart cannot adopt an old session."""
    def __init__(self, authority, row):
        self.authority, self.original = authority, dict(row)

    def _row(self, lease):
        a, original = self.authority, self.original
        if (a.store.recovery_mode or lease.owner_ref != original['owner_ref']
                or lease.task_ref != original['task_id']
                or lease.task_revision != original['revision']
                or lease.input_revision != original['input_revision']
                or lease.lease_ref != original['lease_ref'] or lease.lease_epoch != original['lease_epoch']
                or lease.expires_at > datetime.fromisoformat(original['lease_expires_at'])):
            raise DeliveryError('preview_assignment_invalid')
        lease.validate(datetime.now(SHANGHAI))
        with closing(a.store._connect()) as c:
            c.execute('PRAGMA busy_timeout=50')
            c.execute('BEGIN')
            row = a.store._work(c, original['owner_ref'], original['task_id'])
        if (row['assigned_runner_ref'] != a.runner_ref or row['lease_ref'] != lease.lease_ref
                or row['lease_epoch'] != lease.lease_epoch or row['workspace_ref'] != original['workspace_ref']
                or row['state'] != 'running' or row['phase'] != 'execution'
                or row['lease_expires_at'] != original['lease_expires_at']
                or row['revision'] < original['revision']):
            raise DeliveryError('preview_assignment_invalid')
        return row

    def admit(self, lease):
        try:
            row = self._row(lease)
            self.authority.identity(self.authority.runner_ref)
            return (row['revision'] == self.original['revision']
                and row['input_revision'] == self.original['input_revision']
                and self.authority.store.lease_current(lease.owner_ref, lease.task_ref,
                    lease.lease_ref, lease.lease_epoch, runner_ref=self.authority.runner_ref))
        except Exception:
            return False

    def retire(self, lease):
        try:
            row = self._row(lease)
            identity = self.authority.stop_identity(self.authority.runner_ref, lease.task_ref)
            if (not isinstance(identity, dict) or identity.get('runner_ref') != self.authority.runner_ref
                    or identity.get('registered') is not True or identity.get('enabled') is not True
                    or identity.get('state') not in {'idle', 'busy'}):
                return False
            # A locally durable pending control may only stop the original
            # assignment. It cannot authorize new pixels, input or execution.
            return (row['control_pending'] in {'pause', 'cancel', 'revise'}
                or (row['control_pending'] is None and row['revision'] == self.original['revision']
                    and row['input_revision'] == self.original['input_revision']))
        except Exception:
            return False
