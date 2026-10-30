"""Actual disposable Git and OCI bytes; no Engine or HA qualification."""
from dataclasses import replace
from io import BytesIO
import gzip
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_delivery.codex_delivery.durability import digest
from codex_delivery.codex_delivery.oci_builder import (
    ArtifactBuildEvidence, BuildRecipe, FixedOCIBuilder, CONFIG, LAYER, MANIFEST,
    _descriptor, _encoded, _tar,
)
from codex_delivery.codex_delivery.store import DeliveryError


def base_image(*, compressed=False, hostile=False):
    layer = _tar({'usr/bin/placeholder': (b'fixture only, not an executable runtime', 0o755)})
    stored = gzip.compress(layer, mtime=0) if compressed else layer
    config = _encoded(dict(architecture='amd64', os='linux',
        config=dict(User='0', Env=['SECRET=must-disappear'], Entrypoint=['/danger'], Cmd=['danger'],
                    OnBuild=['RUN must-not-execute'], Volumes={'/host': {}}),
        rootfs=dict(type='layers', diff_ids=[digest(layer)])))
    layer_desc = _descriptor(LAYER + ('+gzip' if compressed else ''), stored)
    if hostile: layer_desc['size'] += 1
    manifest = _encoded(dict(schemaVersion=2, mediaType=MANIFEST,
        config=_descriptor(CONFIG, config), layers=[layer_desc]))
    files = {'oci-layout': (_encoded({'imageLayoutVersion': '1.0.0'}), 0o644),
             'index.json': (_encoded(dict(schemaVersion=2, manifests=[_descriptor(MANIFEST, manifest)])), 0o644)}
    for raw in (stored, config, manifest): files['blobs/sha256/' + digest(raw)[7:]] = (raw, 0o644)
    return _tar(files), digest(manifest)


class OCIBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve(); self.repo = self.root / 'author'; self.repo.mkdir()
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': str(self.root), 'GIT_CONFIG_NOSYSTEM': '1',
                    'GIT_CONFIG_GLOBAL': '/dev/null', 'TZ': 'Asia/Shanghai'}
        self.git('init', '--initial-branch=main')
        self.git('config', 'user.name', 'Fixture'); self.git('config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'README').write_text('baseline')
        self.git('add', '.'); self.git('commit', '-m', 'baseline')
        self.baseline = self.git('rev-parse', 'HEAD').decode().strip()
        self.source = self.bundle('source')
        service = self.repo / 'service'; service.mkdir()
        self.marker = self.root / 'must-not-execute'
        (service / 'main.py').write_text('open(' + repr(str(self.marker)) + ', "w").write("bad")\n')
        (service / 'main.mjs').write_text('throw Error("source must never execute");\n')
        self.git('add', '.'); self.git('commit', '-m', 'candidate')
        self.candidate = self.git('rev-parse', 'HEAD').decode().strip()
        self.incoming = self.bundle('candidate')
        self.base, manifest_digest = base_image()
        self.recipe = BuildRecipe('python-service-v1', 'repository', 'service', 'amd64',
            digest(self.base), manifest_digest, digest(b'fixed permissions'), digest(b'fixture qualification'))
        self.refresh()

    def git(self, *args):
        return subprocess.run(['/usr/bin/git', *args], cwd=self.repo, env=self.env, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def bundle(self, name):
        path = self.root / (name + '.bundle'); self.git('bundle', 'create', str(path), '--all')
        return path.read_bytes()

    def refresh(self):
        source = dict(repo_id='repository', baseline_oid=self.baseline, source_bundle_digest=digest(self.source))
        candidate = dict(source, candidate_oid=self.candidate, bundle_sha256=digest(self.incoming))
        binding = dict(owner_ref='owner', task_ref='task', revision=1, project_ref='project', target_ref='target',
                       release_ref='release', version='0.1.0', permissions_digest=self.recipe.permissions_digest,
                       repositories=[source])
        manifest = dict(binding_ref='binding', binding=binding, candidates=[candidate])
        self.files = {'software.json': _encoded(manifest), 'repository-source.bundle': self.source,
                      'repository-candidate.bundle': self.incoming}
        self.request = {key: value for key, value in binding.items() if key != 'repositories'}
        self.request.update(binding_ref='binding', candidates=[candidate], source_digest=digest(self.files['software.json']),
                            archive={'fixture': 'original-sealed-archive-reference'})

    def builder(self, name='builds', **kwargs):
        return FixedOCIBuilder(self.root / name, kwargs.pop('recipe', self.recipe), kwargs.pop('base', self.base), **kwargs)

    def build(self, builder=None):
        return (builder or self.builder()).build(self.request, self.files, deadline=time.monotonic() + 30)

    def observe(self, builder=None):
        return (builder or self.builder()).observe(self.request, deadline=time.monotonic() + 30)

    def content(self, path):
        with (tarfile.open(fileobj=path) if isinstance(path, BytesIO) else tarfile.open(path)) as archive:
            return {item.name: archive.extractfile(item).read() for item in archive if item.isfile()}

    def test_actual_git_to_exact_oci_source_and_restart_observation(self):
        result = self.build()
        self.assertIs(type(result), ArtifactBuildEvidence)
        self.assertEqual(self.observe(), result)
        directory = self.builder()._directory(self.request)
        files = self.content(directory / 'artifact.tar')
        config = json.loads(files['blobs/sha256/' + result.image_config_digest[7:]])
        runtime = config['config']
        self.assertEqual(runtime['User'], '65532:65532')
        self.assertEqual(runtime['Entrypoint'][:3], ['/usr/bin/python3', '-I', '-c'])
        self.assertIn('sys.path.insert(0,"/app")', runtime['Entrypoint'][3])
        self.assertNotIn('OnBuild', runtime); self.assertNotIn('Volumes', runtime)
        self.assertEqual(runtime['Env'], ['PATH=/usr/bin:/bin', 'TZ=Asia/Shanghai'])
        manifest = json.loads(files['blobs/sha256/' + result.image_manifest_digest[7:]])
        layer = files['blobs/sha256/' + manifest['layers'][-1]['digest'][7:]]
        with tarfile.open(fileobj=BytesIO(layer)) as archive:
            self.assertEqual(set(archive.getnames()), {'app/main.py', 'app/main.mjs'})
            self.assertEqual(archive.extractfile('app/main.py').read(), (self.repo / 'service/main.py').read_bytes())
        self.assertFalse(self.marker.exists())
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in directory.iterdir()))

    def test_two_build_roots_produce_identical_artifact_and_evidence(self):
        self.assertEqual(self.build(self.builder('one')), self.build(self.builder('two')))

    def test_node_profile_uses_fixed_command(self):
        recipe = replace(self.recipe, profile='node-service-v1')
        result = self.build(self.builder(recipe=recipe))
        files = self.content(self.builder()._directory(self.request) / 'artifact.tar')
        config = json.loads(files['blobs/sha256/' + result.image_config_digest[7:]])
        self.assertEqual(config['config']['Entrypoint'], ['/usr/bin/node', '/app/main.mjs'])

    def test_canary_root_profile_requires_exact_target_and_fixed_supervisor_metadata(self):
        with self.assertRaises(DeliveryError):replace(self.recipe,profile='ha-canary-python-v1')
        recipe=replace(self.recipe,profile='ha-canary-python-v1',target_ref='target')
        result=self.build(self.builder(recipe=recipe))
        files=self.content(self.builder()._directory(self.request)/'artifact.tar')
        runtime=json.loads(files['blobs/sha256/'+result.image_config_digest[7:]])['config']
        self.assertEqual(runtime['User'],'0:0')
        self.assertEqual(runtime['Labels']['io.hass.version'],self.request['version'])
        self.assertEqual(runtime['Labels']['io.hass.arch'],'amd64')
        self.assertIn('CODEX_RELEASE_SOURCE_DIGEST='+self.request['source_digest'],runtime['Env'])
        with self.assertRaises(DeliveryError):self.build(self.builder('foreign',recipe=replace(recipe,target_ref='other')))

    def test_gzip_base_verified_and_preserved(self):
        base, manifest = base_image(compressed=True)
        recipe = replace(self.recipe, base_archive_digest=digest(base), base_manifest_digest=manifest)
        builder = self.builder(recipe=recipe, base=base)
        self.assertEqual(self.build(builder), self.observe(builder))

    def test_changed_bundle_and_unknown_files_fail_before_claim(self):
        for change in ({'repository-candidate.bundle': self.incoming + b'changed'}, {'unexpected': b'data'}):
            with self.subTest(change=list(change)):
                with self.assertRaises(DeliveryError):
                    self.builder().build(self.request, dict(self.files, **change), deadline=time.monotonic() + 30)
        self.assertFalse(self.builder().root.exists())

    def test_changed_request_binding_and_permissions_fail_closed(self):
        for field, value in (('version', 'foreign'), ('permissions_digest', digest(b'foreign')),
                             ('source_digest', digest(b'foreign')), ('owner_ref', 'foreign')):
            with self.subTest(field=field), self.assertRaises(DeliveryError):
                self.builder().build(dict(self.request, **{field: value}), self.files, deadline=time.monotonic() + 30)

    def test_broken_base_descriptor_size_rejected_even_if_archive_re_pinned(self):
        base, manifest = base_image(hostile=True)
        recipe = replace(self.recipe, base_archive_digest=digest(base), base_manifest_digest=manifest)
        with self.assertRaises(DeliveryError): self.build(self.builder(recipe=recipe, base=base))

    def test_base_architecture_and_expansion_limit_fail_closed(self):
        with self.assertRaises(DeliveryError): self.build(self.builder(recipe=replace(self.recipe, architecture='arm64')))
        with patch('codex_delivery.codex_delivery.oci_builder.MAX_EXPANDED', 1):
            with self.assertRaises(DeliveryError): self.build(self.builder('second'))

    def test_unexpected_base_blob_is_rejected(self):
        raw = self.content(BytesIO(self.base))
        raw['unregistered'] = b'foreign'
        base = _tar({name: (data, 0o644) for name, data in raw.items()})
        recipe = replace(self.recipe, base_archive_digest=digest(base))
        with self.assertRaises(DeliveryError): self.build(self.builder(recipe=recipe, base=base))

    def test_git_symlink_and_missing_entrypoint_rejected(self):
        (self.repo / 'service/main.py').unlink()
        (self.repo / 'service/main.py').symlink_to('/etc/passwd')
        self.git('add', '.'); self.git('commit', '-m', 'unsafe link')
        self.candidate = self.git('rev-parse', 'HEAD').decode().strip(); self.incoming = self.bundle('link'); self.refresh()
        with self.assertRaises(DeliveryError): self.build()
        (self.repo / 'service/main.py').unlink()
        self.git('add', '.'); self.git('commit', '-m', 'missing entry')
        self.candidate = self.git('rev-parse', 'HEAD').decode().strip(); self.incoming = self.bundle('missing'); self.refresh()
        with self.assertRaises(DeliveryError): self.build(self.builder('second'))

    def test_completed_claim_is_not_replayed(self):
        original = self.build()
        with self.assertRaisesRegex(DeliveryError, 'already_claimed'): self.build()
        self.assertEqual(self.observe(), original)

    def test_interruption_after_claim_has_no_automatic_rebuild(self):
        with patch('codex_delivery.codex_delivery.oci_builder._assemble', side_effect=RuntimeError('fixture crash')):
            with self.assertRaises(RuntimeError): self.build()
        with self.assertRaisesRegex(DeliveryError, 'already_claimed'): self.build()
        with self.assertRaises((DeliveryError, FileNotFoundError)): self.observe()

    def test_retained_artifact_receipt_and_source_drift_rejected(self):
        original = self.build(); directory = self.builder()._directory(self.request)
        for name in ('artifact.tar', 'receipt.json', 'repository-candidate.bundle'):
            path = directory / name; before = path.read_bytes(); path.write_bytes(before + b'changed')
            with self.subTest(name=name), self.assertRaises(DeliveryError): self.observe()
            path.write_bytes(before)
        self.assertEqual(self.observe(), original)

    def test_expired_deadline_does_not_create_claim(self):
        with self.assertRaisesRegex(DeliveryError, 'deadline'):
            self.builder().build(self.request, self.files, deadline=time.monotonic() - 1)
        self.assertFalse(self.builder().root.exists())

    def test_observer_never_calls_build_or_changes_evidence(self):
        original = self.build(); directory = self.builder()._directory(self.request)
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}
        with patch.object(FixedOCIBuilder, 'build', side_effect=AssertionError('must not execute')):
            self.assertEqual(self.observe(), original)
        after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}
        self.assertEqual(before, after)


if __name__ == '__main__': unittest.main()
