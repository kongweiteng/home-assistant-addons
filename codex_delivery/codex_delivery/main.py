"""Explicit service entry point; model-generated programs never run here."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import signal
import sys
import threading

from .provider import manifest
from .service import DeliveryService, ServiceConfig
from .store import DeliveryError
from .production_lifecycle import ProductionLifecycle
from .software_registration import load_registry_plan, load_repository_plan, load_git_plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-config', action='store_true')
    parser.add_argument('--print-manifest', action='store_true')
    args = parser.parse_args()
    if args.print_manifest:
        print(json.dumps(manifest(), ensure_ascii=False, separators=(',', ':')))
        return 0
    lifecycle = ProductionLifecycle()
    execution = None
    try:
        config = ServiceConfig.load(Path(os.environ.get('CODEX_DELIVERY_OPTIONS_FILE', '/data/options.json')))
        # Read the runtime-only observation credential while still root. It is
        # never passed to a guest or read from an execution/model option.
        from .preview_observation import RuntimeObservationPlan
        reserved_credentials = [config.extension_bearer, config.origin_key.decode()]
        if config.execution_settings is not None:
            reserved_credentials.extend((config.execution_settings.runner_bearer,
                                         config.execution_settings.callback_bearer))
        observation_plan = RuntimeObservationPlan.load(
            forbidden_credentials=tuple(reserved_credentials))
        policy = None
        fnos_plan = None
        state_root = Path(os.environ.get('CODEX_DELIVERY_STATE_ROOT', '/data/state'))
        if config.file_replica_settings is not None:
            from .bootstrap import STATE_ROOT, prepare_and_drop
            from .fnos_composition import FnosBootPlan
            if state_root != STATE_ROOT:
                raise DeliveryError('fnos_composition_invalid')
            reserved = [config.extension_bearer, config.origin_key.decode()]
            if config.execution_settings is not None:
                reserved.extend((config.execution_settings.runner_bearer,
                                 config.execution_settings.callback_bearer))
            fnos_plan = FnosBootPlan.load(config.file_replica_settings,
                failure_domain=config.replica_domain, reserved_secrets=reserved)
        if config.execution_settings is not None:
            from .bootstrap import STATE_ROOT, prepare_and_drop, prepare_resources
            from .execution_policy import ExecutionPolicy, digest
            from .execution_composition import QualifiedExecution
            # No environment variable or options path selects executable code
            # or assets. Images without the fixed policy cannot enable work.
            policy = ExecutionPolicy.load()
            if (state_root != STATE_ROOT or (config.replica_settings is None and fnos_plan is None)
                    or digest(policy.registry_policy()) != config.execution_settings.policy_digest):
                raise DeliveryError('execution_not_qualified')
        # Fixed authority data is preloaded while root. It cannot start clients,
        # qualify sources or select code from options during configuration check.
        from .release_authority_composition import ReleaseAuthorityBootPlan
        release_reserved = list(reserved_credentials)
        runtime_key = getattr(observation_plan, '_key', None)
        if type(runtime_key) is str:
            release_reserved.append(runtime_key)
        fnos_key = getattr(fnos_plan, 'encryption_key', None)
        if type(fnos_key) is bytes:
            release_reserved.extend((fnos_key, base64.b64encode(fnos_key).decode('ascii'), fnos_key.hex()))
        release_plan = ReleaseAuthorityBootPlan.load(forbidden_credentials=tuple(release_reserved))
        # Each newer registration rejects every already loaded credential;
        # plans remain data-only until privilege drop and config-check returns.
        build_reserved = list(release_reserved)
        replica_key = getattr(config.replica_settings, 'key', None)
        if type(replica_key) is bytes:
            build_reserved.extend((replica_key, base64.b64encode(replica_key).decode('ascii'), replica_key.hex()))
        if release_plan is not None:
            target_token = getattr(release_plan, '_target_token', None)
            if type(target_token) is str:
                build_reserved.append(target_token)
            principals = getattr(release_plan, 'principals', ())
            for principal in principals if type(principals) is tuple else ():
                build_reserved.append(principal.token)
        registry_plan = load_registry_plan(forbidden_credentials=tuple(build_reserved))
        if registry_plan is not None:
            build_reserved.extend(token for _, token in registry_plan._tokens)
        repository_plan = load_repository_plan(forbidden_credentials=tuple(build_reserved))
        repository_token = getattr(repository_plan, '_token', None)
        if type(repository_token) in {str, bytes}:
            build_reserved.append(repository_token)
        git_plan = load_git_plan(forbidden_credentials=tuple(build_reserved))
        if (repository_plan is None) != (registry_plan is None):
            raise DeliveryError('repository_boot_unavailable')
        if repository_plan is not None and (policy is None or state_root != STATE_ROOT
                or repository_plan.service_uid != policy.host.uid
                or registry_plan.service_uid != policy.host.uid):
            raise DeliveryError('repository_boot_unavailable')
        if git_plan is not None and (repository_plan is None or git_plan.service_uid != repository_plan.service_uid):
            raise DeliveryError('repository_boot_unavailable')
        if observation_plan is not None and policy is None:
            raise DeliveryError('preview_runtime_observation_unavailable')
        if args.check_config:
            print('{"config_valid":true,"execution_enabled":false}')
            return 0
        if policy is not None or fnos_plan is not None:
            uid = policy.host.uid if policy is not None else fnos_plan.service_uid
            if fnos_plan is not None and fnos_plan.service_uid != uid:
                raise DeliveryError('fnos_composition_invalid')
            state_root = prepare_and_drop(uid)
        if policy is not None:
            prepare_resources(policy.host)
            if repository_plan is not None:
                lifecycle.prepare_repository(repository_plan, registry_plan, git_plan=git_plan)
            execution = QualifiedExecution(policy, config.execution_settings, state_root,
                repository_factory=lifecycle.repository_factory)
            lifecycle.execution = execution
        release_factory = release_plan.create if release_plan else None
        if release_plan is not None and lifecycle.repository_factory is not None:
            factory = lifecycle.repository_factory
            release_factory = lambda store: release_plan.create(store,
                release_reader=factory.release_reader, source_qualifier=factory.source_qualifier)
        service = DeliveryService(config, state_root,
            coordinator_factory=execution, capabilities=execution.capabilities if execution else None,
            fnos_replica_factory=fnos_plan.create if fnos_plan else None,
            release_authority_factory=release_factory)
        lifecycle.service = service
        if repository_plan is not None:
            lifecycle.attach_repository(state_root)
            lifecycle.attach_software_management()
        if observation_plan is not None:
            service.server.preview_observation = observation_plan.create()
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda _number, _frame: stop.set())
        lifecycle.start()
        stop.wait()
        lifecycle.close(timeout=150 if execution else 5)
        return 0
    except DeliveryError as exc:
        # Never include raw config, headers, response bodies or exception text.
        safe = exc.code if exc.code in {'service_config_invalid', 'execution_not_qualified',
                'schema_upgrade_required', 'schema_version_unsupported', 'storage_permissions_invalid',
                'storage_path_invalid', 'database_corrupt', 'service_shutdown_incomplete',
                'replica_config_invalid', 'execution_policy_invalid', 'execution_settings_invalid',
                'execution_policy_mismatch', 'execution_bootstrap_invalid',
                'execution_resource_bootstrap_invalid', 'fnos_composition_invalid',
                'fnos_file_config_invalid', 'fnos_qualification_expired',
                'preview_runtime_observation_unavailable', 'release_authority_registration_invalid',
                'repository_boot_unavailable', 'repository_boot_invalid',
                'repository_registration_invalid', 'registry_build_registration_invalid',
                'repository_git_registration_invalid', 'software_management_unavailable',
                'registry_build_unavailable'} else 'service_unavailable'
        print(json.dumps({'status': 'failed', 'code': safe}), file=sys.stderr)
        return 1
    except Exception:
        print('{"status":"failed","code":"service_unavailable"}', file=sys.stderr)
        return 1
    finally:
        try:
            lifecycle.close(timeout=150 if execution else 5)
        except Exception:
            pass


if __name__ == '__main__':
    raise SystemExit(main())
