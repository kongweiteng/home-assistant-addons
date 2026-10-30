"""Fixed internal Broker build routes. Unknown writes have no automatic retry."""
from .registry_build import fail
from ha_artifact_registry.ha_artifact_registry.http import _HTTP
from ha_artifact_registry.ha_artifact_registry.image import canonical, decode
from ha_artifact_registry.ha_artifact_registry.service import REF


class BuildIssuerClient(_HTTP):
    def _route(self, delegation_ref, request_digest, action):
        if (not isinstance(delegation_ref,str) or not REF.fullmatch(delegation_ref)
                or not isinstance(request_digest,str) or not request_digest.startswith('sha256:')
                or len(request_digest)!=71 or any(c not in '0123456789abcdef' for c in request_digest[7:])): fail()
        return '/internal/v1/build-delegations/'+delegation_ref+'/builds/'+request_digest[7:]+'/'+action

    def reserve(self,delegation_ref,request_digest,*,deadline):
        raw,_=self.call(self._route(delegation_ref,request_digest,'reserve'),deadline=deadline,limit=65536,
                        body=canonical({'version':1}),content_type='application/json')
        return decode(raw)

    def issue(self,delegation_ref,request_digest,*,deadline):
        raw,_=self.call(self._route(delegation_ref,request_digest,'issue'),deadline=deadline,limit=65536,
                        body=canonical({'version':1}),content_type='application/json')
        return decode(raw)

    def observe(self,delegation_ref,request_digest,*,deadline):
        raw,_=self.call(self._route(delegation_ref,request_digest,'grant'),deadline=deadline,limit=65536)
        return decode(raw)
