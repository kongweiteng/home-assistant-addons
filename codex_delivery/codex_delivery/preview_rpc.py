"""Private Controller service RPC for an existing canonical pixel session.

Authentication belongs to DeliveryHTTPServer. No model tools use this API.
One Controller boot owns a VM's preview transport; replacing that boot cannot
adopt it after an unknown input. VM/task recovery remains WorkExecutor-owned.
"""
import re
import threading
import time

from .store import DeliveryError

MAX_PREVIEW_RESPONSE = 6*1024**2
PREFIX = '/internal/v1/work-preview/'


class PreviewRPC:
    def __init__(self, transport, allowed_actors):
        self.transport, self.allowed_actors = transport, frozenset(allowed_actors)
        self._lock = threading.Lock()
        self._binding = None

    def dispatch(self, action, value, *, deadline):
        keys = {'version','transport_epoch','request_id','budget_ms','owner_ref','task_ref'}
        keys |= {'scope'} if action in {'frame','action'} else set()
        keys |= {'event'} if action == 'action' else set()
        if (action not in {'scope','frame','action'} or not isinstance(value,dict) or set(value)!=keys
                or type(value['version']) is not int or value['version']!=1
                or not isinstance(value['transport_epoch'],str) or re.fullmatch(r'PT-[a-f0-9]{40}',value['transport_epoch']) is None
                or type(value['request_id']) is not int or not 1<=value['request_id']<=2**53-1
                or type(value['budget_ms']) is not int or not 1<=value['budget_ms']<=5000
                or not isinstance(value['owner_ref'],str) or value['owner_ref'] not in self.allowed_actors
                or not isinstance(value['task_ref'],str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',value['task_ref']) is None):
            raise DeliveryError('preview_request_invalid')
        deadline=min(deadline,time.monotonic()+value['budget_ms']/1000)
        if time.monotonic()>=deadline or not self._lock.acquire(blocking=False):
            raise DeliveryError('preview_busy')
        try:
            scope=self.transport.canonical_scope(value['owner_ref'],value['task_ref'],deadline=deadline)
            identity=tuple(scope[k] for k in ('owner_ref','task_ref','task_revision','input_revision',
                'lease_ref','lease_epoch','preview_nonce'))
            if self._binding is None or self._binding['identity']!=identity:
                if action!='scope':
                    raise DeliveryError('preview_scope_required')
                self._binding={'identity':identity,'epoch':value['transport_epoch'],'last_id':0,'frozen':False}
            binding=self._binding
            if (binding['epoch']!=value['transport_epoch'] or binding['frozen']
                    or value['request_id']<=binding['last_id']):
                raise DeliveryError('preview_transport_fenced')
            binding['last_id']=value['request_id']
            if action!='scope' and value['scope']!=scope:
                raise DeliveryError('preview_scope_changed')
            try:
                result=(scope if action=='scope' else self.transport.frame(scope,deadline=deadline)
                    if action=='frame' else self.transport.action(scope,value['event'],deadline=deadline))
                if time.monotonic()>=deadline:
                    raise DeliveryError('preview_deadline')
                if self.transport.canonical_scope(value['owner_ref'],value['task_ref'],deadline=deadline)!=scope:
                    raise DeliveryError('preview_scope_changed')
                return {'version':1,'request_id':value['request_id'],'result':result}
            except BaseException:
                binding['frozen']=True
                raise
        finally:
            self._lock.release()
