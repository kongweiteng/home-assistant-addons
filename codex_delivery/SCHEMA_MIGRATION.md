# Delivery schema v5

This is a local database migration and candidate adapter correction. It does not
enable deployment, supply Supervisor credentials, qualify a backup restore, or
change a running Home Assistant instance.

## Scope and explicit upgrade

Schema v5 owns `ha_release_operations`, `ha_release_locks`, and
`ha_release_steps` in the existing Delivery SQLite database. A fresh database
installs them with the task, journal, and repository schemas in one transaction.
`HAReleaseCoordinator` only verifies those definitions; constructing it never
creates or repairs tables.

Opening v1, v2, v3, or v4 requires the administrator's explicit `upgrade_schema`
option. Without that option, the Store returns `schema_upgrade_required` and
does not migrate or create an upgrade backup. Unsupported versions are rejected
without changing their version number. Existing v4 task/repository corruption
is rejected rather than repaired as part of the v5 migration.

Before changing a supported older schema, the Store holds its writer lock,
checks SQLite integrity and foreign keys, and creates a new SQLite online backup
beside the source. Its name contains the source version and a random backup ID;
the file is created with mode `0600`, checked for integrity/foreign keys/version,
then fsynced together with its parent directory. `schema_backup` identifies this
pre-upgrade database. It is never overwritten or automatically removed.

Migration DDL, schema verification, `quick_check`, foreign-key checks, and the
version change share one transaction. Any failure rolls back that transaction;
the pre-upgrade backup remains available. Foreign-key enforcement is restored
on every exit. The existing task/repository migration preserves task children,
events, inputs, unknown outcomes, and resource locks.

## Adopting v4 release candidates

The previous candidate created release tables during Coordinator construction
without changing `user_version`. A v4 database may therefore have none, some,
or all of these tables.

During explicit migration, missing tables are installed. Existing candidate
tables are retained in place, including every operation, persistent lock,
claimed/unknown step and evidence row. Their DDL must match the frozen candidate
definitions, including column order, primary keys, unique constraints and
foreign keys; insignificant whitespace and SQL keyword case are accepted.
Additional custom release triggers or indexes are rejected. A weakened or
otherwise changed definition returns `schema_release_invalid` and rolls back
new DDL instead of blessing, rebuilding, or deleting the existing table.

An already-v5 database must contain all required schema objects. Missing or
altered release objects are rejected even when `upgrade_schema` is supplied;
opening a current database is not a repair operation.

## Offline recovery

Replica restore checks authenticated snapshot bytes, source schema version,
SQLite integrity and foreign keys before accepting the snapshot. Older
snapshots require the same explicit migration option. Restore writes into a
fresh private namespace, preserves the pre-upgrade image, and keeps
`recovery_required` set. Migration does not release a target lock, replay a
Supervisor action, acknowledge a notification, or resume execution. Operators
must establish one writer and reconcile external outcomes separately before
clearing recovery gates. Keep the source and pre-upgrade backup until that
reconciliation is complete; do not attempt an in-place downgrade.

## Release adapter limit

Store observations use the canonical `GET /store/addons/{registered_slug}`
endpoint. The local fixture rejects the old extra `/info` suffix.

Supervisor 2026.10.4, commit
`d92eb0e373a304c1e6a66dcef66585854a2002d1`, requires the installed version to
equal Store metadata before rebuilding an app. Restoring old source and
reloading Store metadata cannot downgrade an installed newer version. The old
`restore_source`, `reload`, and `rebuild` compensation adapter therefore refuses
with `rollback_adapter_unqualified` before callbacks or HTTP writes.

When an applied update has a known health failure, Delivery records
`recovery_required` with `rollback_adapter_unqualified`, preserves the verified
backup evidence and resource lock, and reports `backup_restored: false`. It does
not claim `rolled_back`, retry an update, or invoke the invalid compensation.
Reconciliation remains observational. A separately authorized and qualified
backup-restore executor is still required for actual cross-version/data recovery.

Official contract:
[Supervisor rebuild version check](https://github.com/home-assistant/supervisor/blob/d92eb0e373a304c1e6a66dcef66585854a2002d1/supervisor/apps/manager.py#L342).
