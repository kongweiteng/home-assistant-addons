"""Only two digest-addressed, read-only pre-release evidence projections."""
import hmac
import re
from .registry_build import fail

ROUTE=re.compile(r'/internal/v1/build-evidence/([a-f0-9]{64})/(input|artifact)\Z')


class BuildEvidenceRoutes:
    def __init__(self,projection,*,token,owner_refs):
        if (type(token) is not str or not 32<=len(token)<=256 or not token.isascii()
                or any(c.isspace() for c in token) or type(owner_refs) is not frozenset or not owner_refs):fail()
        self.projection,self._token,self.owners=projection,token,owner_refs

    def dispatch(self,method,path,authorization,*,deadline):
        if method!='GET' or type(authorization) is not str or not hmac.compare_digest(authorization,'Bearer '+self._token):fail()
        match=ROUTE.fullmatch(path)
        if match is None:fail()
        hashed,stage=match.groups();request_digest='sha256:'+hashed
        # Check owner before any off-machine read; no request may select a path.
        request=self.projection._request(request_digest,deadline)
        if request['owner_ref'] not in self.owners:fail()
        return getattr(self.projection,stage)(request_digest,deadline=deadline)
