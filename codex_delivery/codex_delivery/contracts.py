"""Pure shared validation/errors: no Store, database, service or adapter imports.

Store/settings re-export these exact objects for existing callers. Standalone
backup adapters may use this module without importing the delivery application.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class DeliveryError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def reference(value: str) -> str:
    if not isinstance(value, str) or not REF.fullmatch(value):
        raise DeliveryError("reference_invalid")
    return value


def fixed_endpoint(value):
    """Canonical HTTPS authority only; forbid aliases and implicit port drift."""
    try:
        if type(value) is not str or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError()
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
                or port is not None and not 1 <= port <= 65535
                or parsed.hostname != parsed.hostname.lower() or '%' in parsed.netloc
                or parsed.netloc.endswith(':') or value != 'https://' + parsed.netloc.lower()):
            raise ValueError()
        return value
    except Exception:
        raise DeliveryError('fnos_file_config_invalid') from None
