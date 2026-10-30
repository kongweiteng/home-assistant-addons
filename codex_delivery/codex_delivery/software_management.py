"""Owner-scoped metadata admission and the original software worker handoff.

Admission cannot fetch, export, execute, push or deploy. Only the existing
SoftwareDeliveryWorker processes a confirmed request and its one original claim.
After a claimed attempt, absence of the original bind is UNKNOWN, never replay.
"""
from contextlib import closing

from .durability import digest
from .repo_manager import _sha
from .repository_execution import SoftwareDeliveryPipeline, _confirmed, _deadline
from .release_authority import CanonicalReleaseAuthority
from .store import DeliveryError, canonical, reference


def fail(code='software_scope_invalid'):
    raise DeliveryError(code) from None


class SoftwareManagement:
    def __init__(self, pipeline, repository_plan, registry_plan):
        if not isinstance(pipeline, SoftwareDeliveryPipeline) or not callable(getattr(repository_plan, 'verify', None)):
            fail('software_management_unavailable')
        self.pipeline, self.store = pipeline, pipeline.store
        self.verify = repository_plan.verify
        self.owner, self.project = registry_plan.owner_ref, registry_plan.project_ref
        self.target = registry_plan.binding.target_ref
        self.permissions = registry_plan.binding.permissions_digest
        for value in (self.owner, self.project, self.target): reference(value)
        _sha(self.permissions)
        self.policies = dict(registry_plan.repo_policies)
        if not self.policies or len(self.policies) > 32: fail()
        for repo, sha in self.policies.items(): reference(repo); _sha(sha)

    def _scope(self, owner, args, *, authorize=True):
        required = {'task_ref', 'expected_revision', 'code_task_ref', 'expected_code_revision', 'release_ref', 'version'}
        if type(args) is not dict or not required <= set(args) or set(args)-required-{'publish_alias'}: fail()
        if owner != self.owner: fail()
        if authorize: self.verify()
        try:
            reference(args['release_ref'])
            task, code, repos = self.pipeline._tasks(owner, args['task_ref'], args['expected_revision'],
                args['code_task_ref'], args['expected_code_revision'])
            if task['project_id'] != self.project or code['state'] != 'queued' or set(repos) != set(self.policies): fail()
            publish = {}
            alias = args.get('publish_alias')
            if task['mode'] == 'develop':
                if alias is not None: fail()
            elif type(alias) is not str: fail()
            parameters = dict(task_ref=task['task_id'], revision=task['revision'],
                code_task_ref=code['task_id'], code_revision=code['revision'],
                release_ref=args['release_ref'], version=args['version'], target_ref=self.target,
                permissions_digest=self.permissions, publish_targets=publish)
            for repo in repos:
                context, policy = self.pipeline.manager._context(owner, task['task_id'], task['revision'], repo, write=True)
                if policy.sha256 != self.policies[repo]: fail()
                if alias is not None:
                    if alias not in dict(policy.publish_targets): fail()
                    publish[repo] = alias
            if authorize:
                for repo in repos:
                    context, _ = self.pipeline.manager._context(owner, task['task_id'], task['revision'], repo, write=True)
                    self.pipeline.manager._authorize('bind_code_task', dict(context, **parameters))
            CanonicalReleaseAuthority._validate_release(dict(schema_version=2, release_ref=args['release_ref'],
                version=args['version'], source_digest=digest(b''), artifact_identity={'kind':'docker_image_id', 'digest':digest(b'')},
                permissions_digest=self.permissions, qualification_digest=digest(b'')), args['release_ref'])
            return parameters
        except Exception:
            fail()

    def request(self, owner, key, args, origin):
        """Only Provider's verified origin; metadata mutation, no source reads."""
        if origin.get('actor_ref') != owner or owner != self.owner: fail()
        identity = {field: origin[field] for field in ('actor_ref', 'conversation_ref', 'source_event_ref',
            'input_revision', 'input_digest', 'call_digest')}
        arguments = dict(tool_arguments=args, origin=identity)
        with closing(self.store._connect()) as connection:
            old = connection.execute('SELECT 1 FROM commands WHERE owner_ref=? AND idempotency_key=?', (owner, key)).fetchone()
        if old:
            # Existing Store command checks the exact kind/arguments and only
            # reconciles its original immutable snapshot, never a source action.
            return self.store._mutation(owner, key, 'software.bind.request', arguments, lambda *_: fail())
        parameters = self._scope(owner, args)
        request_ref = 'SBR-' + digest(canonical(dict(owner=owner, key=key)).encode())[7:]
        binding_key = 'software-binding:' + request_ref[4:]
        def apply(connection, timestamp):
            if self._scope(owner, args, authorize=False) != parameters: fail()
            conflict = connection.execute("SELECT 1 FROM events WHERE owner_ref=? AND kind='software.bind.request' "
                "AND ((json_extract(body_json,'$.result.code_task_ref')=? AND json_extract(body_json,'$.result.code_revision')=?) "
                "OR json_extract(body_json,'$.result.parameters.release_ref')=?)",
                (owner, args['code_task_ref'], args['expected_code_revision'], args['release_ref'])).fetchone()
            if conflict: fail('software_request_conflict')
            return dict(task_id=args['task_ref'], project_id=self.project, request_ref=request_ref,
                code_task_ref=args['code_task_ref'], code_revision=args['expected_code_revision'],
                revision=args['expected_revision'], binding_key=binding_key, parameters=parameters)
        return self.store._mutation(owner, key, 'software.bind.request', arguments, apply)

    def _command(self, owner, key, kind):
        with closing(self.store._connect()) as connection:
            row = connection.execute('SELECT e.kind,d.state FROM commands c JOIN events e USING(event_id) '
                'JOIN durability d USING(event_id) WHERE c.owner_ref=? AND c.idempotency_key=?', (owner, key)).fetchone()
        if row is None: return None
        if row['kind'] != kind: fail()
        if row['state'] != 'confirmed': return False
        return _confirmed(self.store, owner, key, kind)

    def _finish(self, owner, request, state, *, deadline, binding=None):
        _deadline(deadline)
        result = dict(task_id=request['task_id'], request_ref=request['request_ref'], state=state)
        if binding is not None: result['binding_ref'] = binding['binding_ref']
        return self.store._mutation(owner, 'software-outcome:'+request['request_ref'], 'software.bind.outcome',
            dict(request_ref=request['request_ref'], state=state), lambda *_: result)

    def process_next(self, *, deadline):
        """Called solely by the existing software worker, under its busy lock."""
        _deadline(deadline)
        if self.store.recovery_mode: return False
        self.store.verify_event_chain()
        with closing(self.store._connect()) as connection:
            if connection.execute("SELECT 1 FROM durability WHERE state='pending' LIMIT 1").fetchone(): return False
            row = connection.execute("SELECT c.owner_ref,c.idempotency_key FROM commands c JOIN events e USING(event_id) "
                "JOIN durability d USING(event_id) WHERE e.kind='software.bind.request' AND d.state='confirmed' "
                "AND NOT EXISTS (SELECT 1 FROM events done WHERE done.owner_ref=e.owner_ref AND done.kind='software.bind.outcome' "
                "AND json_extract(done.body_json,'$.result.request_ref')=json_extract(e.body_json,'$.result.request_ref')) "
                "ORDER BY e.sequence LIMIT 1").fetchone()
        if row is None: return False
        owner = row['owner_ref']
        command = self._command(owner, row['idempotency_key'], 'software.bind.request')
        request, args = command['result'], command['arguments']['tool_arguments']
        bound = self._command(owner, request['binding_key'], 'software.bind')
        if bound is False: return False
        if bound:
            if bound['arguments'] != request['parameters']: fail()
            self._finish(owner, request, 'bound', deadline=deadline, binding=bound['result'])
            return True
        claim_key = 'software-bind-claim:' + request['request_ref']
        claim = self._command(owner, claim_key, 'software.bind.claim')
        if claim is False: return False
        if claim:
            self._finish(owner, request, 'binding_unknown', deadline=deadline)
            return True
        try:
            if self._scope(owner, args) != request['parameters']: fail()
        except Exception:
            self._finish(owner, request, 'binding_rejected', deadline=deadline)
            return True
        _deadline(deadline)
        claimed = self.store._mutation(owner, claim_key, 'software.bind.claim',
            dict(request_ref=request['request_ref'], parameters_digest=digest(canonical(request['parameters']).encode())),
            lambda *_: dict(task_id=request['task_id'], request_ref=request['request_ref']))
        if claimed['status'] != 'accepted': return True
        try:
            _deadline(deadline)
            self.pipeline.bind(owner, request['binding_key'], **request['parameters'], deadline=deadline)
        except Exception:
            pass  # The original claim is now the only authority: never retry.
        bound = self._command(owner, request['binding_key'], 'software.bind')
        if bound is False: return True
        if bound and bound['arguments'] != request['parameters']: fail()
        self._finish(owner, request, 'bound' if bound else 'binding_unknown', deadline=deadline,
            binding=bound['result'] if bound else None)
        return True

    def status(self, owner, task_ref):
        """Canonical local observations only; never sync, build or reconcile."""
        self.store.verify_event_chain()
        with closing(self.store._connect()) as connection:
            self.store._owned(connection, 'tasks', 'task_id', owner, task_ref)
            row = connection.execute("SELECT c.idempotency_key FROM commands c JOIN events e USING(event_id) "
                "WHERE e.owner_ref=? AND e.task_id=? AND e.kind='software.bind.request' ORDER BY e.sequence DESC LIMIT 1",
                (owner, task_ref)).fetchone()
        if row is None: raise DeliveryError('not_found')
        request_record = self._command(owner, row['idempotency_key'], 'software.bind.request')
        if not request_record:
            return dict(status='ok', execution_enabled=False, item=dict(task_ref=task_ref, state='durability_pending', durability='local_only'))
        request = request_record['result']
        item = dict(task_ref=task_ref, request_ref=request['request_ref'], code_task_ref=request['code_task_ref'],
            release_ref=request['parameters']['release_ref'], state='binding_pending', durability='replicated')
        outcome = self._command(owner, 'software-outcome:'+request['request_ref'], 'software.bind.outcome')
        if outcome:
            item['state'] = outcome['result']['state']
            if 'binding_ref' in outcome['result']:
                item['binding_ref'] = outcome['result']['binding_ref']
                release = self._command(owner, 'software-release:'+item['binding_ref'], 'software.release')
                if release: item['state'] = 'release_ready'
        return dict(status='ok', execution_enabled=False, item=item)
