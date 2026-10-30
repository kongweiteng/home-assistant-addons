"""Trusted grant composition, separate from model programs and browser guests."""
from __future__ import annotations

from copy import deepcopy
from typing import Callable, Any

from .public_browser import PublicBrowserGrant, _fail, _url


class AuthorizedPublicBrowser:
    """The resolver must be the authenticated Controller callback.

    This adapter has no browser profile, grant cache, origin issuer or task
    database. Each invocation reacquires a grant for a canonical task snapshot.
    Only the fixed fetch broker receives the short-lived exact URL capability.
    """
    def __init__(self, browser, resolver: Callable[[dict[str, Any], list[str]], dict]):
        if not callable(resolver) or not callable(getattr(browser, 'browse', None)):
            raise _fail('browser_adapter_invalid')
        self.browser, self.resolver = browser, resolver

    def browse(self, task: dict, exact_urls: list[str], url: str, *, screenshot: bool = True,
               cancelled: Callable[[], bool] | None = None):
        try:
            snapshot = deepcopy(task)
            scope = {'source_event_ref': snapshot.get('current_source_event_ref') or snapshot['source_event_ref'],
                'actor_ref': snapshot['owner_ref'], 'conversation_ref': snapshot['conversation_ref'],
                'task_ref': snapshot['task_id'], 'input_revision': snapshot['input_revision']}
            if (type(screenshot) is not bool or not isinstance(exact_urls, list) or _url(url) not in exact_urls
                    or cancelled is not None and not callable(cancelled)):
                raise ValueError('request')
        except (KeyError, TypeError, ValueError):
            raise _fail('browser_adapter_invalid') from None
        urls = list(exact_urls)
        # No page or task code runs before trusted source/grant validation.
        response = self.resolver(snapshot, list(urls))
        grant = PublicBrowserGrant.from_callback_response(response, expected_scope=scope, exact_urls=urls)
        grant.require(url)
        if cancelled is not None:
            if cancelled():
                raise _fail('browser_cancelled')
            return self.browser.browse(grant, url, screenshot=screenshot, cancelled=cancelled)
        return self.browser.browse(grant, url, screenshot=screenshot)
