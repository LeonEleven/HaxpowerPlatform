"""Read-only checks executed inside the lab; never print site configuration."""

import re
from pathlib import Path

TOOLS_CHECK = """
import json, os, subprocess, sys
commands = {
    'python': ['python', '--version'], 'node': ['node', '--version'],
    'yarn': ['yarn', '--version'], 'npm': ['npm', '--version'],
    'bench': ['bench', '--version'], 'pip': ['python', '-m', 'pip', '--version'],
    'bench_commit': ['git', '-C', '/home/frappe/.bench', 'rev-parse', 'HEAD'],
    'uid': ['id', '-u'],
}
result = {key: subprocess.check_output(cmd, text=True).strip() for key, cmd in commands.items()}
if sys.version_info[:3] != (3, 14, 10):
    raise RuntimeError('Expected Python 3.14.10')
if not result['node'].startswith('v24.') or not result['yarn'].startswith('1.22.'):
    raise RuntimeError('Expected Node 24 and Yarn 1.22')
if result['uid'] != '1000':
    raise RuntimeError('Expected development UID 1000')
result['bench_workspace_exists'] = os.path.isdir('/workspace/development/frappe-bench')
print(json.dumps(result))
"""

SQL_CHECK = """
import subprocess, sys, tempfile
password = sys.stdin.read().strip()
with tempfile.NamedTemporaryFile(mode='w', prefix='haxpower-db-', suffix='.cnf') as config:
    config.write('[client]\\npassword=' + password + '\\n')
    config.flush()
    query = "SELECT VERSION(), @@character_set_server, @@collation_server, @@innodb_snapshot_isolation; SHOW SESSION STATUS LIKE 'Ssl_cipher';"
    result = subprocess.run(['mariadb', '--defaults-extra-file=' + config.name,
        '--protocol=tcp', '--host=mariadb', '--user=root', '--batch',
        '--skip-column-names', '--execute', query], capture_output=True, text=True, timeout=20)
    print(result.stdout, end='')
    print(result.stderr, end='', file=sys.stderr)
    sys.exit(result.returncode)
"""

REJECT_CHECK = """
import subprocess, tempfile
for password in ('deliberately-invalid-lab-password', ''):
    with tempfile.NamedTemporaryFile(mode='w', prefix='haxpower-auth-', suffix='.cnf') as config:
        config.write('[client]\\npassword=' + password + '\\n')
        config.flush()
        result = subprocess.run(['mariadb', '--defaults-extra-file=' + config.name,
            '--protocol=tcp', '--host=mariadb', '--user=root', '--execute', 'SELECT 1'],
            capture_output=True, text=True, timeout=15)
        if result.returncode == 0 or '1045' not in result.stderr:
            raise RuntimeError('Expected access denied for invalid/empty password')
print('Invalid and empty passwords both rejected with access denied (1045).')
"""

BENCH_CHECK = """
import hashlib, importlib, json, os, pathlib, subprocess, sys
root = pathlib.Path('/workspace/development/frappe-bench')
if sys.version_info[:3] != (3, 14, 10):
    raise RuntimeError('Bench virtualenv must use Python 3.14.10')
for folder in (root, root/'sites', root/'apps', root/'env'):
    if folder.stat().st_uid != 1000 or not os.access(folder, os.W_OK):
        raise RuntimeError('Unexpected Bench ownership or write permissions')
result = {}
for name, expected in EXPECTED.items():
    path = root/'apps'/name
    commit = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = subprocess.check_output(['git', '-C', str(path), 'status', '--porcelain'], text=True).strip()
    if commit != expected['commit']:
        raise RuntimeError('Application commit mismatch: ' + name)
    if dirty:
        raise RuntimeError('Core checkout has local changes: ' + name)
    if hashlib.sha256((path/'pyproject.toml').read_bytes()).hexdigest() != expected['pyproject_sha256']:
        raise RuntimeError('Dependency file differs from lock: ' + name)
    version = importlib.import_module(name).__version__
    if version != expected['tag'].removeprefix('v'):
        raise RuntimeError('Application version mismatch: ' + name)
    result[name] = {'version': version, 'commit': commit, 'core_clean': True}
settings = json.loads((root/'sites/common_site_config.json').read_text())
for key, value in {'db_host': 'mariadb', 'redis_cache': 'redis://redis-cache:6379',
                   'redis_queue': 'redis://redis-queue:6379'}.items():
    if settings.get(key) != value:
        raise RuntimeError('Unexpected Bench service setting: ' + key)
result['python'] = sys.version.split()[0]
result['ownership_ok'] = True
result['service_configuration_ok'] = True
print(json.dumps(result))
"""

SITE_CHECK = """
import json, os, pathlib
import frappe
from redis import Redis
root = pathlib.Path('/workspace/development/frappe-bench')
site = root/'sites'/'haxpower.localhost'
if not site.is_dir() or site.stat().st_uid != 1000:
    raise RuntimeError('Test site missing or owned by unexpected user')
os.chdir(root/'sites')
frappe.init(site='haxpower.localhost', sites_path='.')
try:
    frappe.connect()
    apps = frappe.get_installed_apps()
    if 'frappe' not in apps or 'erpnext' not in apps or 'haxpower_platform' in apps:
        raise RuntimeError('Unexpected installed application set')
    if frappe.db.sql('SELECT 1')[0][0] != 1:
        raise RuntimeError('Site database connection failed')
    if not frappe.db.exists('User', 'Administrator'):
        raise RuntimeError('Administrator is missing')
    for key in ('redis_cache', 'redis_queue'):
        if not Redis.from_url(frappe.conf[key]).ping():
            raise RuntimeError('Site Redis connection failed: ' + key)
    print(json.dumps({'site': 'haxpower.localhost', 'installed_apps': apps,
                     'database_connected': True, 'administrator_exists': True,
                     'cache_connected': True, 'queue_connected': True}))
finally:
    frappe.destroy()
"""

HTTP_CHECK = Path(__file__).with_name('asset_checks.py').read_text()


def check_applications(check, compose, lock, phase):
    """Reuse the audited container selection; add checks only when requested."""
    commands = compose + ['exec', '-T', '--workdir', '/workspace/development/frappe-bench', 'frappe']
    expected = 'EXPECTED = ' + repr(lock['applications']) + '\n'
    check('bench_applications', commands + ['./env/bin/python', '-c', expected + BENCH_CHECK])
    if phase == 'site':
        check('site_database_and_redis', commands + ['./env/bin/python', '-c', SITE_CHECK])
        check('site_http_and_api', commands + ['./env/bin/python', '-c', HTTP_CHECK])
        check('bench_workers', commands + ['bench', 'doctor'],
              expected=lambda output: re.search(r'Workers online:\s*[1-9]\d*', output) is not None,
              timeout=60)
