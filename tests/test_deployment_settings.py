import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deployment/scripts/deploy-jobtracker-remote-k8s.sh'


class DeploymentSettingsTests(unittest.TestCase):
    def test_data_path_is_supplied_at_deploy_or_preserved_from_existing_deployment(self) -> None:
        for requested, existing, expected in (
            ('/srv/tracker-data', '/srv/old-data', '/srv/tracker-data'),
            ('', '/srv/existing-data', '/srv/existing-data'),
            ('', '', '/var/lib/jobtracker'),
        ):
            with self.subTest(requested=requested, existing=existing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for command, body in {
                    'docker': '#!/bin/sh\nexit 0\n',
                    'kubectl': '''#!/bin/sh
case "$*" in
  *"get deployment jobtracker"*) printf '%s' "$STUB_EXISTING_PATH" ;;
  *"apply -f -"*) cat > "$STUB_MANIFEST" ;;
esac
''',
                }.items():
                    executable = root / command
                    executable.write_text(body)
                    executable.chmod(0o755)
                environment = {**os.environ, 'PATH': f'{root}:/usr/bin:/bin',
                               'KUBECONFIG': str(root / 'kubeconfig'),
                               'STUB_EXISTING_PATH': existing, 'STUB_MANIFEST': str(root / 'applied.yaml')}
                environment.pop('JOBTRACKER_HTPASSWD', None)
                args = ['bash', str(SCRIPT), 'deploy', '--image-mode', 'registry', '--image-repository', 'example.invalid/test']
                if requested:
                    args += ['--data-dir', requested]
                result = subprocess.run(args, env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'path: {expected}', (root / 'applied.yaml').read_text())

    def test_invalid_data_path_is_rejected_before_external_commands(self) -> None:
        for path in ('relative', '/tmp/path|bad', '/tmp/path\nother'):
            result = subprocess.run(['bash', str(SCRIPT), '--data-dir', path], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('--data-dir must', result.stderr)

    def test_source_archive_uses_an_allowlist(self) -> None:
        script = SCRIPT.read_text()
        self.assertIn('-czf - Dockerfile .dockerignore nginx.conf src server deployment', script)
        self.assertNotIn('-czf - . |', script)
