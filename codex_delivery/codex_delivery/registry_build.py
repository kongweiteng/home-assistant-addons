"""Actual fixed OCI -> same archive -> signed Registry publication bridge.

Only execute writes. All projections and observation reconcile original facts;
none calls a mutating port. No signing secret lives in this service.
"""
from contextlib import closing
from dataclasses import asdict
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from .durability import digest
from .object_archive import ArchiveReceipt, ObjectArchive, MAX_BYTES
from .oci_builder import FixedOCIBuilder, _inputs, _encoded, _json, _write, _sync, _deadline
from .repo_manager import RepoManager, _private, _read, _sha
from .repository_execution import BuildObservation, _confirmed
from .store import DeliveryError, canonical
from ha_artifact_registry.ha_artifact_registry.image import verify_archive


def fail():
    raise DeliveryError('registry_build_unavailable') from None


def hashed(value):
    return digest(_encoded(value))


def grant_ref(request_digest):
    return 'BG-' + _sha(request_digest)[7:]


def _existing_directory(root, directory):
    for path in (root, directory):
        if any(parent.is_symlink() for parent in (path, *path.parents)): fail()
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077: fail()


class BuildEvidenceProjection:
    """Independent same-Store projection; reads real source and artifact replicas."""
    def __init__(self, manager, archive, builder, root):
        if (not isinstance(manager, RepoManager) or not isinstance(archive, ObjectArchive)
                or not isinstance(builder, FixedOCIBuilder) or archive.replica is not manager.store.replica
                or builder.recipe.profile != 'ha-canary-python-v1'): fail()
        self.manager, self.store, self.archive, self.builder = manager, manager.store, archive, builder
        self.root = Path(root)
        if not self.root.is_absolute() or '..' in self.root.parts: fail()

    def _request(self, request_digest, deadline):
        _deadline(deadline); _sha(request_digest)
        if self.store.recovery_mode: fail()
        self.store.verify_event_chain()
        with closing(self.store._connect()) as c:
            if c.execute('SELECT recovery_required FROM runtime_guard WHERE singleton=1').fetchone()[0]: fail()
            if c.execute("SELECT 1 FROM durability WHERE state='pending' LIMIT 1").fetchone(): fail()
            rows = c.execute("SELECT c.owner_ref,c.idempotency_key FROM commands c JOIN events e USING(event_id) "
                "WHERE e.kind='software.build.claim' AND json_extract(c.result_json,'$.request_digest')=?",
                (request_digest,)).fetchall()
        if len(rows) != 1: fail()
        record = _confirmed(self.store, rows[0]['owner_ref'], rows[0]['idempotency_key'], 'software.build.claim')
        if record is None: fail()
        request = record['result']['request']
        if (hashed(request) != request_digest or record['result']['request_digest'] != request_digest
                or request['owner_ref'] != rows[0]['owner_ref']
                or record['result']['task_id'] != request['task_ref']
                or record['arguments'] != dict(binding_ref=request['binding_ref'], request_digest=request_digest)
                or rows[0]['idempotency_key'] != 'software-build-claim:' + request['binding_ref']): fail()
        # The claim belongs to the original software task, not its code Work.
        # Re-read the confirmed binding and both current scopes before any
        # replica read. An immutable claim alone is not current authorization.
        with closing(self.store._connect()) as c:
            bindings = c.execute("SELECT c.idempotency_key FROM commands c JOIN events e USING(event_id) "
                "WHERE c.owner_ref=? AND e.kind='software.bind' "
                "AND json_extract(c.result_json,'$.binding_ref')=?",
                (request['owner_ref'], request['binding_ref'])).fetchall()
        if len(bindings) != 1: fail()
        original = _confirmed(self.store, request['owner_ref'], bindings[0]['idempotency_key'], 'software.bind')
        if original is None: fail()
        binding = original['result']['binding']
        if (original['result']['binding_ref'] != request['binding_ref']
                or original['result']['task_id'] != request['task_ref']
                or 'SB-' + hashed(binding)[7:] != request['binding_ref']
                or any(request[key] != binding[key] for key in ('owner_ref', 'task_ref', 'revision',
                    'project_ref', 'release_ref', 'version', 'target_ref', 'permissions_digest'))
                or any(original['arguments'][key] != binding[key] for key in ('task_ref', 'revision',
                    'code_task_ref', 'code_revision', 'release_ref', 'version', 'target_ref',
                    'permissions_digest', 'publish_targets'))
                or type(request['revision']) is not int
                or type(binding['revision']) is not int or type(binding['code_revision']) is not int): fail()
        owner, project = request['owner_ref'], request['project_ref']
        task = self.store.task(owner, project, request['task_ref'])
        code = self.store.task(owner, project, binding['code_task_ref'])
        repositories = sorted(task['repositories'], key=lambda row: row['repo_id'])
        frozen = binding['repositories']
        if (task['revision'] != binding['revision'] or task['mode'] not in {'develop', 'deliver', 'deploy'}
                or task['state'] not in {'queued', 'running'} or task['control_pending']
                or code['revision'] != binding['code_revision'] or code['mode'] != 'work'
                or code['state'] not in {'queued', 'running', 'completed'} or code['control_pending']
                or digest(code['work_json'].encode()) != binding['code_spec_digest']
                or json.loads(code['work_json'])['profile'] != 'code'
                or not repositories or len(repositories) > 32
                or repositories != sorted(code['repositories'], key=lambda row: row['repo_id'])
                or any(row['access'] != 'write' or row['paths_json'] != '[]' for row in repositories)
                or [row['repo_id'] for row in repositories] != [item['repo_id'] for item in frozen]
                or [item['repo_id'] for item in request['candidates']] != [item['repo_id'] for item in frozen]
                or any(any(candidate[key] != value for key, value in source.items())
                    for candidate, source in zip(request['candidates'], frozen))
                or (task['mode'] == 'develop' and binding['publish_targets'])
                or (task['mode'] in {'deliver', 'deploy'}
                    and set(binding['publish_targets']) != {row['repo_id'] for row in repositories})): fail()
        with closing(self.store._connect()) as c:
            if self.store._project(c, owner, project)['state'] != 'active': fail()
        _deadline(deadline)
        return request

    def _restore(self, scope, receipt, deadline):
        # Temporary restore is deliberate; no persistent archive/build record is made.
        _deadline(deadline)
        with tempfile.TemporaryDirectory(prefix='codex-build-read-') as tmp:
            # macOS's system temporary root may contain the /var alias; use
            # the canonical freshly created private directory, not an alias.
            target = Path(tmp).resolve() / 'readback'
            self.archive.restore(target, scope=scope, receipt=ArchiveReceipt.from_document(receipt), deadline=deadline)
            files = {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob('*') if p.is_file()}
        _deadline(deadline)
        return files

    def input(self, request_digest, *, deadline):
        request = self._request(request_digest, deadline)
        original = _confirmed(self.store, request['owner_ref'], 'software-archive:' + request['binding_ref'], 'software.archive')
        if original is None: fail()
        record = original['result']
        expected_scope = dict(owner_ref=request['owner_ref'], task_ref=request['task_ref'],
                              revision=request['revision'], checkpoint_ref=request['binding_ref'])
        if (record['scope'] != expected_scope or record['archive'] != request['archive']
                or record['source_digest'] != request['source_digest']): fail()
        files = self._restore(record['scope'], record['archive'], deadline)
        inventory = [dict(name=name, size_bytes=len(raw), sha256=digest(raw)) for name, raw in sorted(files.items())]
        if inventory != record['files']: fail()
        _inputs(request, files, self.builder.recipe)
        policies = {}
        for candidate in request['candidates']:
            repo = candidate['repo_id']
            context, _ = self.manager._context(request['owner_ref'], request['task_ref'], request['revision'], repo, write=True)
            if context['policy_digest'] != candidate['policy_digest'] or repo in policies: fail()
            with closing(self.store._connect()) as c:
                stored = self.manager._candidate(c, request['owner_ref'], candidate['candidate_ref'], context)
            if self.manager._validate_candidate(context, stored) != candidate['validation_digest']: fail()
            policies[repo] = context['policy_digest']
        # Replica and validation reads can outlive a control/revision change.
        # Fence both tasks again; an earlier code-Work scope is not a lease.
        if self._request(request_digest, deadline) != request: fail()
        _deadline(deadline)
        return dict(version=1, request=request, request_digest=request_digest, repo_policies=policies,
                    recipe=asdict(self.builder.recipe), recipe_digest=hashed(asdict(self.builder.recipe)),
                    source_evidence_digest=hashed(dict(request_digest=request_digest, archive=record['archive'], files=inventory)))

    def _directory(self, request_digest):
        return self.root / _sha(request_digest)[7:]

    def artifact(self, request_digest, *, deadline):
        inputs = self.input(request_digest, deadline=deadline)
        request = inputs['request']; directory = self._directory(request_digest)
        _existing_directory(self.root, directory)
        receipt = _json(_read(directory / 'archive.json', 32768))
        expected_scope = dict(owner_ref=request['owner_ref'], task_ref=request['task_ref'], revision=request['revision'],
                              checkpoint_ref=grant_ref(request_digest))
        if set(receipt) != {'scope', 'receipt'} or receipt['scope'] != expected_scope: fail()
        restored = self._restore(receipt['scope'], receipt['receipt'], deadline)
        evidence = self.builder.observe(request, deadline=deadline)
        expected = {'artifact.tar': _read(self.builder._directory(request) / 'artifact.tar', MAX_BYTES),
                    'build.json': _encoded(asdict(evidence)), 'request.json': _encoded(request),
                    'recipe.json': _encoded(asdict(self.builder.recipe))}
        if restored != expected: fail()
        graph = verify_archive(restored['artifact.tar'], artifact_digest=evidence.artifact_blob_digest,
            architecture=self.builder.recipe.architecture, manifest_digest=evidence.image_manifest_digest,
            config_digest=evidence.image_config_digest, deadline=deadline)
        _deadline(deadline)
        return dict(inputs=inputs, artifact=asdict(evidence), artifact_size=len(restored['artifact.tar']),
                    archive_receipt=receipt['receipt'], archive_receipt_digest=hashed(receipt['receipt']),
                    graph_digest=graph.graph_digest)


class RegistryBuildBridge:
    def __init__(self, projection, *, delegation_ref, issuer, publisher, observer):
        if not isinstance(projection, BuildEvidenceProjection): fail()
        self.projection = projection
        self.builder, self.archive = projection.builder, projection.archive
        self.delegation_ref, self.issuer, self.publisher, self.observer = delegation_ref, issuer, publisher, observer
        binding = observer.binding
        recipe = self.builder.recipe
        if (binding.target_ref != recipe.target_ref or binding.architecture != recipe.architecture
                or binding.recipe_digest != hashed(asdict(recipe)) or binding.permissions_digest != recipe.permissions_digest
                or binding.qualification_digest != recipe.qualification_digest): fail()

    def _phase(self, directory, name, body):
        _write(directory / (name + '.json'), _encoded(body)); _sync(directory)

    def execute(self, request, files, *, deadline):
        _deadline(deadline)
        request_digest = hashed(request)
        inputs = self.projection.input(request_digest, deadline=deadline)
        if inputs['request'] != request: fail()
        _inputs(request, files, self.builder.recipe)
        # Exact source bytes, not only guest-supplied digests.
        original = _confirmed(self.projection.store, request['owner_ref'],
                              'software-archive:' + request['binding_ref'], 'software.archive')['result']
        if files != self.projection._restore(original['scope'], original['archive'], deadline): fail()
        _private(self.projection.root)
        directory = self.projection._directory(request_digest)
        try: directory.mkdir(mode=0o700)
        except FileExistsError: fail()
        _sync(self.projection.root)
        self._phase(directory, 'reserve-claim', dict(delegation_ref=self.delegation_ref, request_digest=request_digest))
        reservation = self.issuer.reserve(self.delegation_ref, request_digest, deadline=deadline)
        if (type(reservation) is not dict or reservation.get('request_digest') != request_digest
                or reservation.get('delegation_ref') != self.delegation_ref
                or reservation.get('input_digest') != hashed(inputs) or reservation.get('state') != 'reserved'
                or type(reservation.get('max_artifact_bytes')) is not int
                or not 1 <= reservation['max_artifact_bytes'] <= MAX_BYTES - 65536): fail()
        self._phase(directory, 'reservation', reservation)
        _deadline(deadline)
        evidence = self.builder.build(request, files, deadline=deadline)
        if self.builder.observe(request, deadline=deadline) != evidence: fail()
        artifact = _read(self.builder._directory(request) / 'artifact.tar', MAX_BYTES)
        if len(artifact) > reservation['max_artifact_bytes']: fail()
        data = {'artifact.tar': artifact, 'build.json': _encoded(asdict(evidence)),
                'request.json': _encoded(request), 'recipe.json': _encoded(asdict(self.builder.recipe))}
        if sum(map(len, data.values())) > MAX_BYTES: fail()
        expected = [dict(name=name, size_bytes=len(raw), sha256=digest(raw)) for name, raw in sorted(data.items())]
        scope = dict(owner_ref=request['owner_ref'], task_ref=request['task_ref'], revision=request['revision'],
                     checkpoint_ref=grant_ref(request_digest))
        self._phase(directory, 'archive-claim', dict(scope=scope, files=expected))
        _deadline(deadline)
        with tempfile.TemporaryDirectory(prefix='.seal-', dir=self.projection.root) as tmp:
            for name, raw in data.items(): _write(Path(tmp) / name, raw)
            receipt = self.archive.save(tmp, scope=scope, expected_files=expected)
        self._phase(directory, 'archive', dict(scope=scope, receipt=receipt.document()))
        _deadline(deadline)
        self.projection.artifact(request_digest, deadline=deadline)  # Real off-machine read before issue.
        self._phase(directory, 'issue-claim', dict(request_digest=request_digest))
        envelope = self.issuer.issue(self.delegation_ref, request_digest, deadline=deadline)
        self._check_envelope(request_digest, envelope, deadline)
        self._phase(directory, 'grant', envelope)
        self._phase(directory, 'register-claim', dict(grant_ref=grant_ref(request_digest)))
        self.publisher.register(envelope, deadline=deadline)
        if self.observer.observe_grant(grant_ref(request_digest), deadline=deadline) != envelope: fail()
        self.observer.bind(envelope)
        self._phase(directory, 'publish-claim', dict(grant_ref=grant_ref(request_digest)))
        self.publisher.publish(grant_ref(request_digest), artifact, deadline=deadline)
        return self.observe(request, deadline=deadline)

    def _check_envelope(self, request_digest, envelope, deadline):
        # bind independently verifies signature against fixed public key/binding.
        self.observer.bind(envelope)
        fact = self.projection.artifact(request_digest, deadline=deadline)
        inputs, build = fact['inputs'], fact['artifact']; request = inputs['request']
        grant = envelope['grant']
        expected = dict(grant_ref=grant_ref(request_digest), request_digest=request_digest,
            image_version=request['version'], source_digest=request['source_digest'],
            source_evidence_digest=inputs['source_evidence_digest'], recipe_digest=build['recipe_digest'],
            artifact_digest=build['artifact_blob_digest'], manifest_digest=build['image_manifest_digest'],
            config_digest=build['image_config_digest'], graph_digest=fact['graph_digest'],
            context_digest=build['context_digest'], archive_receipt_digest=fact['archive_receipt_digest'])
        if any(grant.get(key) != value for key, value in expected.items()): fail()
        return fact

    def observe(self, request, *, deadline):
        _deadline(deadline)
        request_digest = hashed(request)
        directory = self.projection._directory(request_digest)
        _existing_directory(self.projection.root, directory)
        reservation = _json(_read(directory / 'reservation.json', 32768))
        if reservation.get('delegation_ref') != self.delegation_ref or reservation.get('request_digest') != request_digest: fail()
        envelope = self.issuer.observe(self.delegation_ref, request_digest, deadline=deadline)
        fact = self._check_envelope(request_digest, envelope, deadline)
        # If a response was retained it must equal the original independently read grant.
        if (directory / 'grant.json').exists() and _json(_read(directory / 'grant.json', 32768)) != envelope: fail()
        if self.observer.observe_grant(grant_ref(request_digest), deadline=deadline) != envelope: fail()
        actual = self.observer.observe(grant_ref(request_digest), deadline=deadline)
        grant = envelope['grant']
        for key in ('grant_ref', 'target_ref', 'request_digest', 'source_evidence_digest', 'source_digest',
                    'recipe_digest', 'context_digest', 'archive_receipt_digest', 'graph_digest'):
            if actual.get(key) != grant[key]: fail()
        _deadline(deadline)
        return BuildObservation(request_digest, grant_ref(request_digest), actual['artifact_identity'],
            grant['permissions_digest'], grant['qualification_digest'],
            hashed(dict(artifact=fact, grant=envelope, registry=actual)))
