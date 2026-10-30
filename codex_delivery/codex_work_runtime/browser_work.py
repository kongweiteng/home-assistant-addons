"""Fixed public-browser intent under the existing WorkExecutor journal.

The host formats validated browser results; it never executes the task program.
Canonical task and grant resolvers are trusted composition ports, not tools.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Callable

from .browser_adapter import AuthorizedPublicBrowser
from .errors import WorkRuntimeError
from .public_browser import _url
from .qemu_work import _host_boundary
from .work_execution import MIB, WorkRequest, WorkRuntime, _checkpoint_tree, _private_dir, _write_new


def _fail(code, *, uncertain=False):
    return WorkRuntimeError(code, 'Browser task rejected by fixed policy.', uncertain=uncertain)


def browser_intent(raw: str) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        if not isinstance(raw, str) or not 1 <= len(raw.encode()) <= 24000:
            raise ValueError()
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (not isinstance(value, dict) or set(value) != {'version', 'url', 'exact_urls', 'screenshot'}
                or type(value['version']) is not int or value['version'] != 1
                or type(value['screenshot']) is not bool or not isinstance(value['exact_urls'], list)
                or not 1 <= len(value['exact_urls']) <= 64):
            raise ValueError()
        urls = [_url(url) for url in value['exact_urls']]
        if len(set(urls)) != len(urls) or sum(map(len, urls)) > 32768 or _url(value['url']) not in urls:
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError, WorkRuntimeError):
        raise _fail('browser_work_intent_invalid') from None


class BrowserWorkBackend:
    def __init__(self, runtimes: dict[str, WorkRuntime], browsers: dict,
                 task_reader: Callable[[WorkRequest], dict], grant_resolver: Callable):
        if not runtimes or set(runtimes) != set(browsers) or not callable(task_reader):
            raise _fail('browser_work_configuration_invalid')
        for key, runtime in runtimes.items():
            runtime.validate()
            if runtime.kind != 'browser-qemu-v1' or runtime.runtime_ref != key:
                raise _fail('browser_work_configuration_invalid')
        self.runtimes, self.browsers, self.task_reader = dict(runtimes), dict(browsers), task_reader
        self.policy = next(iter(browsers.values())).policy
        if any(browser.policy != self.policy for browser in browsers.values()):
            raise _fail('browser_work_configuration_invalid')
        self.assets = {key: browser.asset for key, browser in browsers.items()}
        self.authorized = {key: AuthorizedPublicBrowser(browser, grant_resolver) for key, browser in browsers.items()}
        self.reserve_bytes = max(browser.reserve_bytes for browser in browsers.values())

    def validate_request(self, request: WorkRequest) -> None:
        runtime = self.runtimes.get(request.runtime_ref)
        if runtime is None or request.profile != 'browser' or request.profile not in runtime.profiles:
            raise _fail('work_runtime_not_registered')
        if (request.limits.memory_bytes != 2048*MIB or request.limits.cpu_millis != 2000
                or request.limits.workspace_bytes > 8*MIB or request.limits.max_files < 2):
            raise _fail('browser_work_budget_invalid')
        _host_boundary(self.policy)

    def _qualify(self, asset) -> None:
        for browser in self.browsers.values():
            if browser.asset is asset:
                browser._qualify(asset)
                return
        raise _fail('browser_work_configuration_invalid')

    def run(self, request: WorkRequest, *, claim_ref: str, program: Path, inputs: Path,
            restore: Path, destination: Path, created, cancelled, now):
        self.validate_request(request)
        raw = program.read_text(encoding='utf-8')
        intent = browser_intent(raw)
        if any(inputs.iterdir()) or any(restore.iterdir()):
            raise _fail('browser_work_private_input_rejected')
        task = self.task_reader(request)
        try:
            spec = json.loads(task['work_json'])
            expected_outputs = ['report.txt', *(['page.png'] if intent['screenshot'] else [])]
            if (task['owner_ref'] != request.owner_ref or task['task_id'] != request.task_ref
                    or task['workspace_ref'] != request.workspace_ref or task['revision'] != request.input_revision
                    or task['lease_ref'] != request.lease_id or task['lease_epoch'] != request.assignment_epoch
                    or task['state'] != 'running' or task['control_pending'] is not None
                    or dt.datetime.fromisoformat(task['lease_expires_at']) != request.expires_at
                    or spec['profile'] != 'browser' or spec['runtime_id'] != request.runtime_ref
                    or spec['program'] != raw or spec['input_refs']
                    or set(spec['output_names']) != set(expected_outputs)):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise _fail('browser_work_assignment_invalid') from None

        def stopped_reason():
            if now() >= request.expires_at:
                return 'lease_expired'
            if cancelled():
                return 'cancelled'
            return None

        created('browser:' + claim_ref)
        reason = stopped_reason()
        result = None
        if reason is None:
            try:
                result = self.authorized[request.runtime_ref].browse(task, intent['exact_urls'], intent['url'],
                    screenshot=intent['screenshot'], cancelled=lambda: stopped_reason() is not None)
            except WorkRuntimeError as error:
                # The browser contract guarantees confirmed dual-process stop
                # before any known error. Uncertain cleanup remains unsettled.
                if error.uncertain:
                    raise
                reason = stopped_reason()
                if reason is None and error.code in {'browser_timeout', 'browser_grant_expired'}:
                    reason = 'timeout'
        reason = reason or stopped_reason()
        _private_dir(destination)
        if reason is None and result is not None:
            report = result.title + '\n' + result.url + '\n\n' + result.text + '\n'
            _write_new(destination / 'report.txt', report.encode('utf-8'))
            if intent['screenshot']:
                if not isinstance(result.screenshot_png, bytes):
                    raise _fail('browser_work_result_invalid')
                _write_new(destination / 'page.png', result.screenshot_png)
        elif reason is None:
            _write_new(destination / 'report.txt', b'Public browsing failed under the fixed policy.\n')
        tree = _checkpoint_tree(destination, request.limits)
        return {'checkpoint_written': True, 'files': tree['files'], 'exit_code': 0 if result is not None and reason is None else 1,
                'stop_reason': reason or 'finished'}
