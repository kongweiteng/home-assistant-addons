"""Prevent the observed Supervisor base-image fallback from returning."""
from pathlib import Path
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]
ADDON=ROOT/'ha_artifact_observer'
BASES={
    'amd64':'ghcr.io/home-assistant/amd64-base-debian@sha256:84023298f975360b4350fe76118a1a5b117ff8f7ca5249d6398381659051aba3',
    'arm64':'ghcr.io/home-assistant/aarch64-base-debian@sha256:4ecdb87bbf2e24c220140aedefdc521f7e3c499bc116c886f432261dc247d2f8',
}


class ArtifactObserverBuildContractTests(unittest.TestCase):
    def instructions(self):
        return [line.strip() for line in (ADDON/'Dockerfile').read_text().splitlines()
                if line.strip() and not line.lstrip().startswith('#')]

    def test_Supervisor_default_BUILD_FROM_cannot_replace_pinned_Debian(self):
        self.assertFalse((ADDON/'build.yaml').exists())
        instructions=self.instructions()
        self.assertFalse(any('BUILD_FROM' in line for line in instructions))
        stages={}
        for line in instructions:
            match=re.fullmatch(r'FROM (\S+) AS (base-[a-z0-9]+)',line)
            if match:stages[match[2]]=match[1]
        self.assertEqual(stages,{'base-'+arch:image for arch,image in BASES.items()})
        self.assertIn('Acquire::https::Timeout=15',(ADDON/'Dockerfile').read_text())

    def test_actual_Docker_architecture_selects_only_reviewed_base_stage(self):
        instructions=self.instructions()
        self.assertIn('ARG TARGETARCH',instructions)
        final=[line for line in instructions if line.startswith('FROM ')][-1]
        self.assertEqual(final,'FROM base-${TARGETARCH} AS final')
        available={'base-'+arch for arch in BASES}
        for arch in ('amd64','arm64'):
            self.assertIn(final.split()[1].replace('${TARGETARCH}',arch),available)
        for unsupported in ('aarch64','arm','386',''):
            self.assertNotIn(final.split()[1].replace('${TARGETARCH}',unsupported),available)

    def test_public_metadata_and_runtime_version_match_corrected_build(self):
        config=(ADDON/'config.yaml').read_text()
        self.assertIn('version: "0.0.5"',config)
        self.assertIn('arch: [amd64, aarch64]',config)
        self.assertIn('docker_api: false',config)
        self.assertIn('hassio_api: false',config)
        self.assertIn('__version__=\'0.0.5\'',(ADDON/'ha_artifact_observer/__init__.py').read_text())
        self.assertIn('HAArtifactObserver/0.0.5',(ADDON/'ha_artifact_observer/api.py').read_text())


if __name__=='__main__':unittest.main()
