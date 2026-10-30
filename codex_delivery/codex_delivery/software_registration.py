"""Lightweight fixed registration selection for the metadata-only image.

Absent and exact disabled registrations never import optional build libraries.
An existing file still requires the same root/no-follow/private provenance as
other production registrations. No path, module or executable comes from options.
"""
from pathlib import Path

from .release_authority_composition import _file
from .store import DeliveryError
from .target_observation_client import decode

REGISTRY_REGISTRATION = Path('/etc/codex-delivery/registry-build/registration.json')
REPOSITORY_REGISTRATION = Path('/etc/codex-delivery/repository/registration.json')
GIT_REGISTRATION = Path('/etc/codex-delivery/repository-git/registration.json')


def _enabled(path, error_code):
    try:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        raw = _file(path, private=True, optional=True)
        if raw is None:
            return False
        value = decode(raw)
        return not (type(value) is dict and set(value) == {'version', 'enabled'}
            and type(value['version']) is int and value['version'] == 1 and value['enabled'] is False)
    except Exception:
        raise DeliveryError(error_code) from None


def load_registry_plan(*, forbidden_credentials):
    if not _enabled(REGISTRY_REGISTRATION, 'registry_build_registration_invalid'):
        return None
    from .registry_build_composition import RegistryBuildBootPlan
    return RegistryBuildBootPlan.load(forbidden_credentials=forbidden_credentials)


def load_repository_plan(*, forbidden_credentials):
    if not _enabled(REPOSITORY_REGISTRATION, 'repository_registration_invalid'):
        return None
    from .repository_boot import RepositoryBootPlan
    return RepositoryBootPlan.load(forbidden_credentials=forbidden_credentials)


def load_git_plan(*, forbidden_credentials):
    if not _enabled(GIT_REGISTRATION, 'repository_git_registration_invalid'):
        return None
    from .repository_git_auth import GitAuthBootPlan
    return GitAuthBootPlan.load(forbidden_credentials=forbidden_credentials)
