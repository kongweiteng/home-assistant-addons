-- Connection pragmas are applied by DeliveryStore before the DDL transaction.
CREATE TABLE IF NOT EXISTS runtime_guard (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 recovery_required INTEGER NOT NULL CHECK(recovery_required IN (0,1))
);
INSERT OR IGNORE INTO runtime_guard VALUES (1,0);
CREATE TABLE IF NOT EXISTS repositories (
 repo_id TEXT PRIMARY KEY, owner_ref TEXT NOT NULL, alias TEXT NOT NULL,
 remote_identity TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL CHECK(revision>0),
 status TEXT NOT NULL CHECK(status IN ('enrolled','archived')), created_at TEXT NOT NULL,
 UNIQUE(owner_ref,alias), UNIQUE(owner_ref,repo_id)
);
CREATE TABLE IF NOT EXISTS projects (
 project_id TEXT PRIMARY KEY, owner_ref TEXT NOT NULL, alias TEXT NOT NULL,
 name TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('proposed','active','paused','archived')),
 revision INTEGER NOT NULL CHECK(revision>0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(owner_ref,alias), UNIQUE(owner_ref,project_id)
);
CREATE TABLE IF NOT EXISTS project_versions (
 project_id TEXT NOT NULL REFERENCES projects(project_id), revision INTEGER NOT NULL,
 config_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(project_id,revision)
);
CREATE TABLE IF NOT EXISTS project_repositories (
 owner_ref TEXT NOT NULL, project_id TEXT NOT NULL, repo_id TEXT NOT NULL,
 access TEXT NOT NULL CHECK(access IN ('read','write')), paths_json TEXT NOT NULL,
 PRIMARY KEY(project_id,repo_id),
 FOREIGN KEY(owner_ref,project_id) REFERENCES projects(owner_ref,project_id),
 FOREIGN KEY(owner_ref,repo_id) REFERENCES repositories(owner_ref,repo_id)
);
CREATE TABLE IF NOT EXISTS tasks (
 task_id TEXT PRIMARY KEY, owner_ref TEXT NOT NULL, project_id TEXT,
 conversation_ref TEXT NOT NULL, source_event_ref TEXT NOT NULL,
 mode TEXT NOT NULL CHECK(mode IN ('design','develop','deliver','deploy','work')),
 requirement TEXT NOT NULL, project_revision INTEGER,
 revision INTEGER NOT NULL CHECK(revision>0), phase TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('queued','running','waiting_input','paused','blocked','recovery_required','completed','failed','cancelled')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 task_scope TEXT NOT NULL DEFAULT 'project' CHECK(task_scope IN ('project','owner_work')),
 workspace_ref TEXT NOT NULL DEFAULT '', work_json TEXT NOT NULL DEFAULT '{}',
 input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
 current_source_event_ref TEXT NOT NULL DEFAULT '',
 lease_epoch INTEGER NOT NULL DEFAULT 0 CHECK(lease_epoch>=0),
 lease_ref TEXT, lease_expires_at TEXT, assigned_runner_ref TEXT, control_pending TEXT,
 runtime_result_json TEXT NOT NULL DEFAULT '{}',
 CHECK((task_scope='project' AND project_id IS NOT NULL) OR (task_scope='owner_work' AND project_id IS NULL)),
 UNIQUE(owner_ref,source_event_ref), UNIQUE(owner_ref,project_id,task_id),
 UNIQUE(owner_ref,task_id),
 FOREIGN KEY(owner_ref,project_id) REFERENCES projects(owner_ref,project_id),
 FOREIGN KEY(project_id,project_revision) REFERENCES project_versions(project_id,revision)
);
CREATE TABLE IF NOT EXISTS task_repositories (
 owner_ref TEXT NOT NULL, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
 repo_id TEXT NOT NULL, access TEXT NOT NULL, paths_json TEXT NOT NULL,
 PRIMARY KEY(task_id,repo_id),
 FOREIGN KEY(owner_ref,project_id,task_id) REFERENCES tasks(owner_ref,project_id,task_id),
 FOREIGN KEY(owner_ref,repo_id) REFERENCES repositories(owner_ref,repo_id)
);
CREATE TABLE IF NOT EXISTS events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 owner_ref TEXT NOT NULL, project_id TEXT, task_id TEXT,
 kind TEXT NOT NULL, body_json TEXT NOT NULL, created_at TEXT NOT NULL,
 previous_digest TEXT NOT NULL, event_digest TEXT NOT NULL UNIQUE,
 UNIQUE(owner_ref,event_id)
);
CREATE TABLE IF NOT EXISTS commands (
 owner_ref TEXT NOT NULL, idempotency_key TEXT NOT NULL, argument_digest TEXT NOT NULL,
 event_id TEXT NOT NULL REFERENCES events(event_id), result_json TEXT NOT NULL,
 PRIMARY KEY(owner_ref,idempotency_key)
);
CREATE TABLE IF NOT EXISTS durability (
 event_id TEXT PRIMARY KEY REFERENCES events(event_id), snapshot_digest TEXT,
 state TEXT NOT NULL CHECK(state IN ('pending','confirmed')), receipt_json TEXT
);
CREATE TABLE IF NOT EXISTS notification_outbox (
 event_id TEXT PRIMARY KEY REFERENCES events(event_id),
 state TEXT NOT NULL CHECK(state IN ('queued','unknown','accepted')),
 channel_evidence TEXT, updated_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
 BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
 BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS versions_no_update BEFORE UPDATE ON project_versions
 BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS versions_no_delete BEFORE DELETE ON project_versions
 BEGIN SELECT RAISE(ABORT,'append_only'); END;
-- provider journal v2
CREATE TABLE IF NOT EXISTS input_journal (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL UNIQUE,
 owner_ref TEXT NOT NULL, conversation_ref TEXT NOT NULL, source_event_ref TEXT NOT NULL,
 input_revision INTEGER NOT NULL CHECK(input_revision>0), name TEXT NOT NULL,
 resource_ref TEXT NOT NULL, call_digest TEXT NOT NULL, payload_json TEXT NOT NULL,
 origin_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('queued','running','durability_pending','accepted','rejected')),
 result_json TEXT NOT NULL,
 UNIQUE(owner_ref,source_event_ref,input_revision,name,resource_ref)
);
CREATE INDEX IF NOT EXISTS journal_pending ON input_journal(state,sequence);
CREATE TRIGGER IF NOT EXISTS journal_no_rewrite BEFORE UPDATE OF
 operation_id,owner_ref,conversation_ref,source_event_ref,input_revision,name,
 resource_ref,call_digest,payload_json,origin_json,created_at ON input_journal
 BEGIN SELECT RAISE(ABORT,'append_only_input'); END;
CREATE TRIGGER IF NOT EXISTS journal_no_delete BEFORE DELETE ON input_journal
 BEGIN SELECT RAISE(ABORT,'append_only_input'); END;
-- HA release journal v5
CREATE TABLE IF NOT EXISTS ha_release_operations(
 deployment_ref TEXT PRIMARY KEY,owner_ref TEXT NOT NULL,task_ref TEXT NOT NULL,
 project_ref TEXT NOT NULL,target_ref TEXT NOT NULL,request_digest TEXT NOT NULL,
 request_json TEXT NOT NULL,state TEXT NOT NULL,evidence_json TEXT NOT NULL,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ha_release_locks(
 resource_ref TEXT PRIMARY KEY,deployment_ref TEXT NOT NULL UNIQUE
 REFERENCES ha_release_operations(deployment_ref));
CREATE TABLE IF NOT EXISTS ha_release_steps(
 deployment_ref TEXT NOT NULL REFERENCES ha_release_operations(deployment_ref),
 step TEXT NOT NULL,operation_ref TEXT NOT NULL UNIQUE,state TEXT NOT NULL,
 result_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 PRIMARY KEY(deployment_ref,step));
-- schema version
-- Repository evidence is installed from repo_manager in this same transaction.
-- DeliveryStore validates every required schema before committing version 5.
PRAGMA user_version=5;
