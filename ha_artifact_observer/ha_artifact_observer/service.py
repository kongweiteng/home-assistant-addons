"""Administrator-only composition. No discovery, mutation or release grants."""
from datetime import datetime
import hmac
import threading
import time

from .contract import Binding,Principal,TZ,fail,ref,stamp,validate_fact


class ObserverService:
    def __init__(self,*,inspector=None,bindings=(),principals=(),enabled=False,clock=None,monotonic=time.monotonic):
        self.inspector,self.enabled=inspector,enabled
        self.clock=clock or (lambda:datetime.now(TZ));self.monotonic=monotonic
        if any(not isinstance(item,Binding) for item in bindings) or any(not isinstance(item,Principal) for item in principals):
            fail('observer_registration_invalid')
        self.bindings={item.target_ref:item for item in bindings};self.principals=tuple(principals)
        if len(self.bindings)!=len(bindings) or len({p.principal_ref for p in principals})!=len(principals):fail('observer_registration_invalid')
        tokens=[];containers=set();slugs=set()
        for binding in bindings:
            identity=(binding.instance_ref,binding.container_name)
            slug_identity=(binding.instance_ref,binding.slug)
            if identity in containers or slug_identity in slugs:fail('observer_registration_invalid')
            containers.add(identity);slugs.add(slug_identity)
        # One local Docker daemon belongs to exactly one registered instance.
        if len({b.instance_ref for b in bindings})>1:fail('observer_registration_invalid')
        for principal in principals:
            if not principal.target_refs<=set(self.bindings) or any(hmac.compare_digest(principal.token,t) for t in tokens):fail('observer_registration_invalid')
            tokens.append(principal.token)
        self._capacity=threading.BoundedSemaphore(2)

    def observe(self,target_ref,authorization):
        if not self.enabled:fail('observer_disabled')
        if self.inspector is None or not self.bindings or not self.principals:fail('observer_authority_unavailable')
        if not isinstance(authorization,str):fail('observer_not_authorized')
        authenticated=[p for p in self.principals if hmac.compare_digest(authorization,'Bearer '+p.token)]
        if len(authenticated)!=1:fail('observer_not_authorized')
        target=ref(target_ref);principal=authenticated[0]
        if target not in principal.target_refs or target not in self.bindings:fail('observer_target_denied')
        if not self._capacity.acquire(blocking=False):fail('observer_busy')
        try:
            binding=self.bindings[target]
            fact=self.inspector.inspect(binding,deadline=self.monotonic()+5)
            # Closed trusted-port output; no original Docker dictionary returns.
            validate_fact(fact)
            return {'version':1,'target_ref':binding.target_ref,'instance_ref':binding.instance_ref,'slug':binding.slug,
                    **fact,'observed_at':stamp(self.clock())}
        finally:self._capacity.release()
