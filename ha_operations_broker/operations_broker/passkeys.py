"""Yubico FIDO2 adapter for exact-origin Passkey verification."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from fido2.server import Fido2Server
from fido2.webauthn import (
    AttestationConveyancePreference,
    AttestedCredentialData,
    AuthenticationResponse,
    PublicKeyCredentialRpEntity,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)


def validate_webauthn_configuration(
    rp_id: str, allowed_origins: Sequence[str]
) -> tuple[str, frozenset[str]]:
    normalized_rp = rp_id.strip().lower() if isinstance(rp_id, str) else ""
    if (
        not normalized_rp
        or len(normalized_rp) > 253
        or normalized_rp.startswith(".")
        or normalized_rp.endswith(".")
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-." for character in normalized_rp)
    ):
        raise ValueError("WebAuthn RP ID is invalid")
    origins: set[str] = set()
    for origin in allowed_origins:
        if not isinstance(origin, str):
            raise ValueError("WebAuthn origin is invalid")
        parsed = urlsplit(origin.strip())
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("WebAuthn origins must be exact HTTPS origins")
        hostname = parsed.hostname.lower()
        if hostname != normalized_rp and not hostname.endswith(f".{normalized_rp}"):
            raise ValueError("WebAuthn origin is outside the RP ID")
        port = f":{parsed.port}" if parsed.port is not None else ""
        origins.add(f"https://{hostname}{port}")
    if not origins:
        raise ValueError("At least one WebAuthn origin is required")
    return normalized_rp, frozenset(origins)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _jsonable(value[key]) for key in value}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    return value


class Fido2PasskeyBackend:
    """Small adapter around audited FIDO2/WebAuthn verification primitives."""

    def __init__(self, *, rp_id: str, allowed_origins: Sequence[str]):
        self.rp_id, self.allowed_origins = validate_webauthn_configuration(
            rp_id, allowed_origins
        )
        self.server = Fido2Server(
            PublicKeyCredentialRpEntity(
                id=self.rp_id,
                name="Home Assistant Operations Broker",
            ),
            attestation=AttestationConveyancePreference.NONE,
            verify_origin=lambda origin: origin in self.allowed_origins,
        )

    def registration_begin(
        self, *, user_handle: bytes, existing_credentials: list[bytes]
    ) -> tuple[dict[str, Any], Any]:
        credentials = [AttestedCredentialData(value) for value in existing_credentials]
        options, state = self.server.register_begin(
            {
                "id": user_handle,
                "name": f"ha-operator-{user_handle.hex()[:12]}",
                "displayName": "Home Assistant operator",
            },
            credentials=credentials,
            resident_key_requirement=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return _jsonable(options), state

    def registration_complete(self, *, state: Any, response: Any) -> dict[str, Any]:
        auth_data = self.server.register_complete(state, response)
        credential = auth_data.credential_data
        if credential is None:
            raise ValueError("Registration did not return credential data")
        return {
            "credential_id": credential.credential_id,
            "credential_data": bytes(credential),
            "sign_count": auth_data.counter,
        }

    def authentication_begin(
        self, *, credentials: list[bytes]
    ) -> tuple[dict[str, Any], Any]:
        parsed = [AttestedCredentialData(value) for value in credentials]
        options, state = self.server.authenticate_begin(
            parsed,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return _jsonable(options), state

    def authentication_complete(
        self, *, state: Any, credentials: list[bytes], response: Any
    ) -> dict[str, Any]:
        parsed = [AttestedCredentialData(value) for value in credentials]
        credential = self.server.authenticate_complete(state, parsed, response)
        authentication = AuthenticationResponse.from_dict(response)
        return {
            "credential_id": credential.credential_id,
            "sign_count": authentication.response.authenticator_data.counter,
        }
