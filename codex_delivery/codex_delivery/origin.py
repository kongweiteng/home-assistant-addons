"""Versioned authenticated origin primitive, pending Controller integration.

Signing is a trusted Controller-side operation, outside model arguments. The
provider verifies source ownership but must still enforce project/task grants.
A verified owner input is not a grant to push or deploy.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import hmac
import re
from typing import Any

from .durability import digest
from .store import SHANGHAI, canonical, reference

FIELDS = frozenset({'version', 'audience', 'actor_ref', 'conversation_ref', 'source_event_ref',
                    'input_revision', 'input_digest', 'call_digest', 'issued_at', 'expires_at', 'signature'})
DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')


class OriginError(ValueError):
    pass


def _key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) < 32:
        raise OriginError('origin_key_invalid')


def _instant(instant: datetime) -> datetime:
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise OriginError('origin_time_invalid')
    return instant.astimezone(SHANGHAI)


def _signature(key: bytes, body: dict[str, Any]) -> str:
    return hmac.new(key, b'delivery-origin-v1\x00' + canonical(body).encode(), hashlib.sha256).hexdigest()


def issue_origin(key: bytes, *, actor_ref: str, conversation_ref: str, source_event_ref: str,
                 input_revision: int, input_digest: str, name: str, arguments: dict[str, Any],
                 issued_at: datetime, ttl_seconds: int = 60, audience: str = 'delivery') -> dict[str, Any]:
    """Internal issuer primitive, never expose as a model tool or public HTTP API."""
    _key(key)
    for value in (actor_ref, conversation_ref, source_event_ref, audience):
        reference(value)
    if (type(input_revision) is not int or input_revision < 1 or type(ttl_seconds) is not int
            or not 1 <= ttl_seconds <= 60 or not isinstance(input_digest, str)
            or not DIGEST.fullmatch(input_digest)):
        raise OriginError('origin_input_invalid')
    instant = _instant(issued_at)
    body = {'version': 1, 'audience': audience, 'actor_ref': actor_ref,
            'conversation_ref': conversation_ref, 'source_event_ref': source_event_ref,
            'input_revision': input_revision, 'input_digest': input_digest,
            'call_digest': digest(canonical({'name': name, 'arguments': arguments}).encode()),
            'issued_at': instant.isoformat(), 'expires_at': (instant + timedelta(seconds=ttl_seconds)).isoformat()}
    return {**body, 'signature': _signature(key, body)}


def verify_origin(key: bytes, origin: dict[str, Any], *, name: str, arguments: dict[str, Any],
                  checked_at: datetime, allowed_actors: frozenset[str],
                  audience: str = 'delivery') -> dict[str, Any]:
    """Return verified metadata only. Duplicate events are handled by durable store."""
    _key(key)
    try:
        if not isinstance(origin, dict) or set(origin) != FIELDS:
            raise ValueError
        body = {k: v for k, v in origin.items() if k != 'signature'}
        if not isinstance(origin['signature'], str) or not hmac.compare_digest(_signature(key, body), origin['signature']):
            raise ValueError
        if (type(origin['version']) is not int or origin['version'] != 1
                or origin['audience'] != audience or origin['actor_ref'] not in allowed_actors
                or type(origin['input_revision']) is not int or origin['input_revision'] < 1
                or not DIGEST.fullmatch(origin['input_digest'])
                or origin['call_digest'] != digest(canonical({'name': name, 'arguments': arguments}).encode())):
            raise ValueError
        for k in ('actor_ref', 'conversation_ref', 'source_event_ref', 'audience'):
            reference(origin[k])
        issued = _instant(datetime.fromisoformat(origin['issued_at']))
        expires = _instant(datetime.fromisoformat(origin['expires_at']))
        checked = _instant(checked_at)
        if not 0 < (expires - issued).total_seconds() <= 60 or issued > checked + timedelta(seconds=5) or expires <= checked:
            raise ValueError
        return body
    except (ValueError, TypeError, KeyError) as exc:
        raise OriginError('origin_invalid') from exc
