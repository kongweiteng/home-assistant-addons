"""Trusted replica boundary. A local directory is never an off-device receipt.

No production adapter is installed by this module. Adapters must provide bounded,
authenticated, encrypted, immutable writes with read-after-write verification.
The configured failure-domain identity is an administrative trust decision.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Protocol


def digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class Snapshot:
    event_id: str
    content: bytes
    sha256: str


@dataclass(frozen=True)
class ReplicaReceipt:
    event_id: str
    sha256: str
    failure_domain: str
    object_ref: str


class Replica(Protocol):
    def save(self, snapshot: Snapshot) -> ReplicaReceipt:
        """Idempotent verified write; timeout/unknown must be reconciled by adapter."""
        ...


def verify_receipt(snapshot: Snapshot, receipt: ReplicaReceipt, *,
                   local_domain: str, replica_domain: str) -> None:
    if (not replica_domain or replica_domain == local_domain
            or receipt.failure_domain != replica_domain
            or receipt.event_id != snapshot.event_id
            or receipt.sha256 != snapshot.sha256
            or not receipt.object_ref or len(receipt.object_ref) > 200):
        raise ValueError("replica_receipt_invalid")
