"""Trusted fixed routing; a task cannot select a backend or substitute assets."""
from .errors import WorkRuntimeError


class WorkBackendRouter:
    def __init__(self, backends):
        self.runtimes, self.assets, self._routes = {}, {}, {}
        if not backends:
            raise WorkRuntimeError('work_backend_invalid', 'No qualified backend.')
        self.policy = backends[0].policy
        self.reserve_bytes = 0
        for backend in backends:
            if backend.policy != self.policy or set(self.runtimes) & set(backend.runtimes):
                raise WorkRuntimeError('work_backend_invalid', 'Conflicting fixed backend.')
            self.runtimes.update(backend.runtimes)
            self.assets.update(backend.assets)
            self._routes.update({key: backend for key in backend.runtimes})
            self.reserve_bytes = max(self.reserve_bytes, backend.reserve_bytes)

    def validate_request(self, request):
        backend = self._routes.get(request.runtime_ref)
        if backend is None:
            raise WorkRuntimeError('work_runtime_not_registered', 'Fixed runtime unavailable.')
        backend.validate_request(request)

    def _qualify(self, asset):
        for key, value in self.assets.items():
            if value is asset:
                return self._routes[key]._qualify(asset)
        raise WorkRuntimeError('work_backend_invalid', 'Fixed asset unavailable.')

    def run(self, request, **kwargs):
        self.validate_request(request)
        return self._routes[request.runtime_ref].run(request, **kwargs)

    def prepare_restore(self, request, **kwargs):
        self.validate_request(request)
        prepare = getattr(self._routes[request.runtime_ref], 'prepare_restore', None)
        return prepare(request, **kwargs) if callable(prepare) else kwargs['source']
