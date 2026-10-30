"""Closed tools authenticated by a service bearer and trusted signed origin.

Only a compatible configured Controller may issue origins. Model arguments
cannot select identity, qualify a runtime or enable a backend.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import hmac
from typing import Any, Callable

from .journal import InputJournal, WRITES
from .origin import OriginError, verify_origin
from .store import DeliveryError, SHANGHAI, canonical, reference

MAX_REQUEST = 65536
MAX_RESPONSE = 131072


def string(limit: int = 128, *, values: tuple[str, ...] = ()) -> dict[str, Any]:
    result: dict[str, Any] = {'type': 'string', 'minLength': 1, 'maxLength': limit}
    if values:
        result['enum'] = list(values)
    return result


def integer(minimum: int = 1, maximum: int = 2**31 - 1) -> dict[str, Any]:
    return {'type': 'integer', 'minimum': minimum, 'maximum': maximum}


def closed(fields: dict[str, Any], required: tuple[str, ...] | None = None) -> dict[str, Any]:
    return {'type': 'object', 'properties': fields,
            'required': list(fields) if required is None else list(required), 'additionalProperties': False}


PAGE = {'offset': integer(0), 'limit': integer(1, 100)}
GROUP_MEMBER = closed({
    **{key: string() for key in ('member_ref', 'project_ref', 'task_ref', 'release_ref',
                                  'authorization_ref', 'deployment_ref', 'target_ref')},
    'task_revision': integer(), 'version': string(64), 'release_digest': string(71),
    'goal': string(16, values=('published', 'deployed')),
}, ('member_ref', 'project_ref', 'task_ref', 'task_revision', 'release_ref',
    'version', 'release_digest', 'goal'))
GROUP_EDGE = closed({'before': string(), 'after': string()})
INPUTS = {
    'capabilities': closed({}),
    'work_submit': closed({'requirement': string(16000),
        'profile': string(16, values=('document', 'data', 'code', 'browser', 'preview')),
        'runtime_id': string(), 'program': string(24000),
        'output_names': {'type': 'array', 'items': string(120), 'maxItems': 4},
        'input_refs': {'type': 'array', 'items': string(), 'maxItems': 10},
        'project_ref': string(), 'expected_project_revision': integer()},
        ('requirement', 'profile', 'runtime_id', 'program', 'output_names', 'input_refs')),
    'work_status': closed({'task_ref': string()}),
    'work_prepare': closed({'task_ref': string(), 'expected_revision': integer(), 'program': string(24000),
        'output_names': {'type': 'array', 'items': string(120), 'maxItems': 4},
        'input_refs': {'type': 'array', 'items': string(), 'maxItems': 10}}),
    'work_revise': closed({'task_ref': string(), 'expected_revision': integer(), 'supplement': string(16000)}),
    'work_control': closed({'task_ref': string(), 'expected_revision': integer(),
        'action': string(16, values=('pause', 'resume', 'cancel'))}),
    'software_bind': closed({'task_ref': string(), 'expected_revision': integer(),
        'code_task_ref': string(), 'expected_code_revision': integer(), 'release_ref': string(),
        'version': string(64), 'publish_alias': string(32)},
        ('task_ref', 'expected_revision', 'code_task_ref', 'expected_code_revision', 'release_ref', 'version')),
    'software_status': closed({'task_ref': string()}),
    'group_create': closed({
        'members': {'type': 'array', 'items': GROUP_MEMBER, 'minItems': 2, 'maxItems': 32},
        'edges': {'type': 'array', 'items': GROUP_EDGE, 'maxItems': 128},
        'acceptance_ref': string(),
    }),
    'group_status': closed({'group_ref': string()}),
    'group_list': closed(PAGE, ()),
    'group_events': closed({'group_ref': string(), 'after': integer(0),
                            'limit': integer(1, 100)}, ('group_ref',)),
    'group_control': closed({'group_ref': string(), 'expected_revision': integer(),
                            'action': string(16, values=('pause', 'resume', 'cancel'))}),
    'projects': closed(PAGE, ()), 'repositories': closed(PAGE, ()), 'list': closed(PAGE, ()),
    'status': closed({'project_ref': string(), 'task_ref': string()}),
    'events': closed({'project_ref': string(), 'task_ref': string(), 'after': integer(0), 'limit': integer(1, 100)}, ('project_ref',)),
    'operation_status': closed({'operation_ref': string()}),
    'plan': closed({'project_ref': string(), 'expected_project_revision': integer(),
                    'requirement': string(16000), 'mode': string(16, values=('design', 'develop', 'deliver', 'deploy'))}),
    'revise': closed({'project_ref': string(), 'task_ref': string(), 'expected_revision': integer(), 'supplement': string(16000)}),
    'control': closed({'project_ref': string(), 'task_ref': string(), 'expected_revision': integer(),
                      'action': string(16, values=('pause', 'resume', 'cancel'))}),
    'project_control': closed({'project_ref': string(), 'expected_revision': integer(),
                              'action': string(16, values=('pause', 'resume', 'archive'))}),
}
ITEM = closed({
    **{k: string() for k in ('project_ref', 'task_ref', 'repo_ref', 'alias', 'event_ref', 'kind', 'workspace_ref', 'runtime_id',
                            'request_ref', 'binding_ref', 'release_ref', 'code_task_ref')},
    'name': string(100), 'state': string(32), 'phase': string(32), 'mode': string(16),
    'revision': integer(), 'project_revision': integer(), 'sequence': integer(),
    'created_at': string(64), 'updated_at': string(64), 'durability': string(32),
    'execution_enabled': {'type': 'boolean'},
    'profile': string(16), 'task_scope': string(16), 'input_revision': integer(),
    'control_pending': string(16), 'available': {'type': 'boolean'},
    'verified_at': string(64), 'reason': string(128),
}, ())
GROUP_MEMBER_STATUS = closed({
    **GROUP_MEMBER['properties'],
    'state': string(32), 'reason': string(128),
    'authorization_state': string(32), 'historical_success': {'type': 'boolean'},
    'dependency_blocked': {'type': 'boolean'},
    'fact_count': integer(0), 'facts_truncated': {'type': 'boolean'},
    'facts': {'type': 'array', 'items': closed({
        'event_ref': string(), 'kind': string(64), 'digest': string(71),
        'publication_ref': string(),
    }, ('event_ref', 'kind', 'digest')), 'maxItems': 3},
    'published': {'type': 'boolean'}, 'deployed': {'type': 'boolean'},
}, ('member_ref', 'project_ref', 'task_ref', 'task_revision', 'release_ref',
    'version', 'release_digest', 'goal'))
GROUP_STATUS = closed({
    'group_ref': string(), 'revision': integer(), 'control': string(32),
    'state': string(32), 'acceptance_ref': string(),
    'durability': string(16, values=('replicated', 'local_only')),
    'observation_ref': string(),
    **{key: {'type': 'boolean'} for key in ('requires_reconciliation',
        'recovery_read_only', 'combination_accepted')},
    'members': {'type': 'array', 'items': GROUP_MEMBER_STATUS, 'maxItems': 32},
    'edges': {'type': 'array', 'items': GROUP_EDGE, 'maxItems': 128},
    'created_at': string(64), 'updated_at': string(64),
}, ('group_ref', 'revision'))
OUTPUT = closed({
    'status': string(32, values=('ok', 'accepted', 'rejected', 'durability_pending')),
    'execution_enabled': {'type': 'boolean'}, 'code': string(128), 'durability': string(32),
    **{k: string() for k in ('project_ref', 'task_ref', 'operation_ref', 'event_ref', 'request_ref', 'group_ref')},
    'revision': integer(), 'total': integer(0), 'has_more': {'type': 'boolean'},
    'next_offset': integer(0), 'next_after': integer(0),
    'items': {'type': 'array', 'items': ITEM, 'maxItems': 100}, 'item': ITEM,
    'group': GROUP_STATUS,
    'groups': {'type': 'array', 'items': GROUP_STATUS, 'maxItems': 100},
}, ('status', 'execution_enabled'))


def validate(value: Any, schema: dict[str, Any]) -> None:
    """Only the fixed contract's closed objects, arrays, strings and scalars."""
    kind = schema['type']
    if kind == 'object':
        if (not isinstance(value, dict) or set(value) - schema['properties'].keys()
                or set(schema['required']) - value.keys()):
            raise DeliveryError('arguments_invalid')
        for key, child in value.items():
            validate(child, schema['properties'][key])
    elif kind == 'array':
        if not isinstance(value, list) or len(value) > schema['maxItems']:
            raise DeliveryError('arguments_invalid')
        for child in value:
            validate(child, schema['items'])
    elif kind == 'string':
        if (not isinstance(value, str) or not schema.get('minLength', 0) <= len(value) <= schema['maxLength']
                or '\x00' in value or ('enum' in schema and value not in schema['enum'])):
            raise DeliveryError('arguments_invalid')
    elif kind == 'integer':
        if type(value) is not int or not schema['minimum'] <= value <= schema['maximum']:
            raise DeliveryError('arguments_invalid')
    elif kind == 'boolean':
        if type(value) is not bool:
            raise DeliveryError('arguments_invalid')
    else:
        raise DeliveryError('contract_invalid')


def manifest() -> dict[str, Any]:
    """Candidate schemas only; installing a manifest does not enable origin support."""
    return {'version': 1, 'tools': [
        {'name': name, 'display_name': 'Delivery ' + name,
         'description': ('保存交付元数据，离机确认前不得称为已受理。' if name in WRITES else '查询当前 owner 的交付元数据。')
                        + ' 通用任务需另行具备合格执行节点；查询和入队不代表执行、Git推送或部署授权。',
         'risk_type': 'write' if name in WRITES else 'read_only', 'trusted_origin': True,
         'inputSchema': schema, 'outputSchema': OUTPUT}
        for name, schema in INPUTS.items()]}


class DeliveryProvider:
    def __init__(self, journal: InputJournal, *, bearer: str, origin_key: bytes,
                 allowed_actors: frozenset[str], clock: Callable[[], datetime] | None = None,
                 capabilities: Callable[[], list[dict[str, Any]]] | None = None):
        if (not isinstance(bearer, str) or not 32 <= len(bearer) <= 4096
                or any(not 33 <= ord(c) <= 126 for c in bearer)
                or not isinstance(origin_key, bytes) or len(origin_key) < 32
                or not isinstance(allowed_actors, frozenset) or not allowed_actors):
            raise DeliveryError('provider_config_invalid')
        for actor in allowed_actors:
            reference(actor)
        # Store only a digest for HTTP service authentication.
        self._bearer_digest = hashlib.sha256(bearer.encode()).digest()
        self._origin_key = origin_key
        self.allowed_actors = allowed_actors
        self.journal = journal
        self.store = journal.store
        self.clock = clock or (lambda: datetime.now(SHANGHAI))
        self.capabilities = capabilities or (lambda: [])
        self.software_management = None  # Trusted lifecycle only; no options flag.
        self.groups = None

    def authenticated(self, authorization: str) -> bool:
        if not isinstance(authorization, str) or not authorization.startswith('Bearer ') or len(authorization) > 4103:
            return False
        return hmac.compare_digest(self._bearer_digest, hashlib.sha256(authorization[7:].encode()).digest())

    def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Called after HTTP authentication; origin remains mandatory for reads too."""
        try:
            if (not isinstance(payload, dict) or set(payload) != {'version', 'name', 'arguments', 'context', 'origin'}
                    or type(payload['version']) is not int or payload['version'] != 1):
                raise DeliveryError('request_invalid')
            name, args, context = payload['name'], payload['arguments'], payload['context']
            if not isinstance(name, str) or name not in INPUTS:
                raise DeliveryError('tool_unavailable')
            validate(args, INPUTS[name])
            validate(context, closed({k: string() for k in ('job_hash', 'turn_hash', 'idempotency_key')}))
            verified = verify_origin(self._origin_key, payload['origin'], name=name, arguments=args,
                                     checked_at=self.clock(), allowed_actors=self.allowed_actors)
            if name in {'software_bind', 'software_status'} and self.software_management is None:
                raise DeliveryError('tool_unavailable')
            if name in WRITES:
                result = self.journal.submit(name, args, verified)
            else:
                result = self._read(verified['actor_ref'], name, args)
            validate(result, OUTPUT)
            if len(canonical(result).encode()) > MAX_RESPONSE:
                raise DeliveryError('response_too_large')
            return result
        except OriginError:
            return {'status': 'rejected', 'code': 'origin_invalid', 'execution_enabled': False}
        except DeliveryError as exc:
            return {'status': 'rejected', 'code': exc.code, 'execution_enabled': False}
        except Exception:
            # Never echo untrusted payloads or adapter/SQLite diagnostic strings.
            return {'status': 'rejected', 'code': 'provider_unavailable', 'execution_enabled': False}

    @staticmethod
    def _item(row: dict[str, Any]) -> dict[str, Any]:
        fields = {'project_id': 'project_ref', 'task_id': 'task_ref', 'repo_id': 'repo_ref',
                  'event_id': 'event_ref', 'event_ref': 'event_ref', 'alias': 'alias', 'name': 'name', 'state': 'state',
                  'phase': 'phase', 'mode': 'mode', 'revision': 'revision', 'project_revision': 'project_revision',
                  'sequence': 'sequence', 'kind': 'kind', 'created_at': 'created_at', 'updated_at': 'updated_at',
                  'durability': 'durability', 'status': 'state'}
        return {fields[key]: value for key, value in row.items() if key in fields and value is not None}

    def _read(self, owner: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name.startswith('group_'):
            return self._read_group(owner, name, args)
        if name == 'software_status':
            return self.software_management.status(owner, args['task_ref'])
        if name == 'operation_status':
            return self.journal.status(owner, args['operation_ref'])
        result: dict[str, Any] = {'status': 'ok', 'execution_enabled': False}
        if name == 'capabilities':
            result['items'] = self.capabilities()
            result['execution_enabled'] = (not self.store.recovery_mode and self.store.replica is not None
                and any(item.get('available') is True for item in result['items']))
            return result
        if name == 'work_status':
            # Validated result evidence is delivered on the background channel;
            # status exposes only the closed metadata DTO.
            item = self.store.work_status(owner, args['task_ref'])
            item.pop('result', None)
            result['item'] = item
            return result
        if name in {'projects', 'repositories', 'list'}:
            kind = {'projects': 'projects', 'repositories': 'repositories', 'list': 'tasks'}[name]
            page = self.store.catalog(owner, kind=kind, **args)
            result.update(items=[self._item(row) for row in page['items']], total=page['total'], has_more=page['has_more'])
            if page['next_offset'] is not None:
                result['next_offset'] = page['next_offset']
            return result
        if name == 'events':
            page = self.store.events(owner, project_id=args['project_ref'], task_id=args.get('task_ref'),
                                     after=args.get('after', 0), limit=args.get('limit', 20))
            result.update(items=[self._item(row) for row in page['items']], total=page['total'],
                          has_more=page['has_more'], next_after=page['next_after'])
            return result
        task = self.store.task(owner, args['project_ref'], args['task_ref'])
        result['item'] = self._item(task)
        # A catalog/query observes local state, not an implicit durability claim.
        with closing(self.store._connect()) as c:
            row = c.execute('SELECT d.state FROM events e JOIN durability d USING(event_id) WHERE e.owner_ref=? AND e.task_id=? ORDER BY e.sequence DESC LIMIT 1',
                            (owner, args['task_ref'])).fetchone()
        result['durability'] = 'replicated' if row and row[0] == 'confirmed' else 'local_only'
        return result

    @staticmethod
    def _group_item(row: dict[str, Any], *, detail: bool = True) -> dict[str, Any]:
        # Expose a closed metadata projection, never raw event/request bodies.
        result = {key: value for key, value in row.items()
                  if key in GROUP_STATUS['properties'] and key not in {'members', 'edges'}}
        if detail and 'members' in row:
            result['members'] = [{key: value for key, value in member.items()
                if key in GROUP_MEMBER_STATUS['properties'] and value is not None}
                for member in row['members']]
            for member in result['members']:
                if 'facts' in member:
                    allowed = GROUP_MEMBER_STATUS['properties']['facts']['items']['properties']
                    member['fact_count'] = len(member['facts'])
                    member['facts_truncated'] = len(member['facts']) > 3
                    member['facts'] = [{key: value for key, value in fact.items() if key in allowed}
                                       for fact in member['facts'][:3]]
        if detail and 'edges' in row:
            result['edges'] = [{key: edge[key] for key in ('before', 'after')}
                               for edge in row['edges']]
        return result

    def _read_group(self, owner: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.groups is None:
            from .delivery_groups import DeliveryGroups
            self.groups = DeliveryGroups(self.store)
        result: dict[str, Any] = {'status': 'ok', 'execution_enabled': False}
        if name == 'group_status':
            result['group'] = self._group_item(self.groups.status(owner, args['group_ref']))
        elif name == 'group_list':
            page = self.groups.list(owner, **args)
            result.update(groups=[self._group_item(row, detail=False) for row in page['items']],
                total=page['total'], has_more=page['has_more'])
            if page.get('next_offset') is not None:
                result['next_offset'] = page['next_offset']
        elif name == 'group_events':
            page = self.groups.events(owner, args['group_ref'],
                after=args.get('after', 0), limit=args.get('limit', 20))
            items = [self._item(row) for row in page['items']]
            for item in items:
                item['durability'] = 'replicated' if item.get('durability') == 'confirmed' else 'local_only'
            result.update(items=items,
                total=page['total'], has_more=page['has_more'], next_after=page['next_after'])
        else:
            raise DeliveryError('tool_unavailable')
        return result
