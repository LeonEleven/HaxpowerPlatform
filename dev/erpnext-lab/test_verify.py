"""Regression checks for lab verification; requires no Docker or application DB."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import verify
from site_checks import TOOLS_CHECK


class VerificationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='haxpower-verify-', dir='/tmp')
        self.addCleanup(self.directory.cleanup)
        self.lab = Path(self.directory.name)
        self.lab.chmod(0o700)
        (self.lab / 'secrets').mkdir(mode=0o700)
        (self.lab / 'secrets/mariadb-root-password').write_text('A' * 48)
        source = Path(__file__).parent / 'versions.lock.json'
        (self.lab / 'versions.lock.json').write_bytes(source.read_bytes())
        self.lock = json.loads(source.read_text())

    def invoke(self, extra, config=None):
        args = ['verify.py', '--lab-root', str(self.lab), '--config-only', *extra]
        response = subprocess.CompletedProcess([], 0, json.dumps(config), '')
        with mock.patch.object(sys, 'argv', args), mock.patch.object(subprocess, 'run', return_value=response) as runner:
            verify.main()
        return runner

    def config(self):
        # Only fields evaluated before the unsafe-service checks are needed.
        return {
            'name': 'haxpower-erpnext-v16-lab',
            'services': {
                'mariadb': {'image': self.lock['images']['mariadb']['reference']},
                'frappe': {}, 'redis-cache': {}, 'redis-queue': {},
            },
            'networks': {'backend': {'internal': True}}, 'volumes': {},
        }

    def test_published_database_port_is_rejected(self):
        config = self.config()
        config['services']['mariadb']['ports'] = [{'host_ip': '0.0.0.0', 'published': '3306'}]
        with self.assertRaises(verify.ValidationError):
            self.invoke([], config)

    def test_changed_image_is_rejected(self):
        config = self.config()
        config['services']['mariadb']['image'] = 'mariadb:latest'
        with self.assertRaises(verify.ValidationError):
            self.invoke([], config)

    def test_noninternal_backend_is_rejected(self):
        config = self.config()
        config['networks']['backend']['internal'] = False
        with self.assertRaises(verify.ValidationError):
            self.invoke([], config)

    def test_history_cannot_be_overwritten(self):
        target = self.lab / 'bootstrap-checks.json'
        target.write_text('historical result')
        with self.assertRaisesRegex(verify.ValidationError, 'already exists'):
            self.invoke(['--output', str(target)])
        self.assertEqual(target.read_text(), 'historical result')

    def test_output_cannot_escape_with_dotdot(self):
        with self.assertRaisesRegex(verify.ValidationError, 'inside the lab'):
            self.invoke(['--output', str(self.lab / '../escaped.json')])

    def test_output_symlink_is_rejected(self):
        target = self.lab / 'linked.json'
        target.symlink_to(self.lab / 'another.json')
        with self.assertRaisesRegex(verify.ValidationError, 'symlink'):
            self.invoke(['--output', str(target)])

    def test_base_tools_allow_an_existing_bench(self):
        values = {'python': 'Python 3.14.10', 'node': 'v24.22.3', 'yarn': '1.22.22',
                  'npm': '11.19.1', 'bench': '5.31.1', 'git': 'commit', 'id': '1000'}
        def output(command, **kwargs):
            return values[command[0]] + '\n'
        captured = io.StringIO()
        with mock.patch.object(subprocess, 'check_output', side_effect=output), \
             mock.patch('os.path.isdir', return_value=True), \
             mock.patch.object(sys, 'version_info', (3, 14, 10)), \
             contextlib.redirect_stdout(captured):
            exec(TOOLS_CHECK, {})
        self.assertTrue(json.loads(captured.getvalue())['bench_workspace_exists'])


if __name__ == '__main__':
    unittest.main()
