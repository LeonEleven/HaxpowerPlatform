#!/usr/bin/env python3
"""Read-only base, Bench or ERPNext-site verification, with immutable evidence."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
from zoneinfo import ZoneInfo
from site_checks import TOOLS_CHECK, SQL_CHECK, REJECT_CHECK, check_applications

class ValidationError(RuntimeError):
    pass

def require(condition, message):
    if not condition:
        raise ValidationError(message)

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lab-root', type=Path, default=Path.home() / 'labs/haxpower-erpnext-v16')
    parser.add_argument('--config-only', action='store_true')
    parser.add_argument('--phase', choices=('base', 'bench', 'site'), default='base')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    requested_lab = args.lab_root.expanduser().absolute()
    require(not any(p.is_symlink() for p in (requested_lab, *requested_lab.parents)), 'Lab path traverses a symlink')
    lab = requested_lab.resolve()
    source = Path(__file__).resolve().parent
    if lab == source.parents[1] or lab.is_relative_to(source.parents[1]):
        raise SystemExit('Lab must be outside the project')
    lock = json.loads((lab / 'versions.lock.json').read_text())
    require(lock == json.loads((source / 'versions.lock.json').read_text()), 'Version lock differs')
    secret_path = lab / 'secrets/mariadb-root-password'
    require(not secret_path.is_symlink(), 'Secret must not be a symlink')
    secret = secret_path.read_text().strip()
    require(re.fullmatch('[A-Za-z0-9_-]{32,}', secret), 'Invalid local secret format')
    compose = ['docker', 'compose', '--project-directory', str(lab), '-f', str(lab / 'compose.yaml')]
    errors = []
    report = {'checked_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'), 'phase': args.phase, 'configuration_only': args.config_only}
    redactions = [secret]
    site_config = lab / 'workspace/frappe-bench/sites/haxpower.localhost/site_config.json'
    if site_config.is_file():
        require(not any(p.is_symlink() for p in (site_config, *site_config.parents)), 'Site configuration path traverses a symlink')
        settings = json.loads(site_config.read_text())
        redactions.extend(str(settings[key]) for key in ('db_password', 'encryption_key', 'backup_encryption_key') if settings.get(key))
    target = None
    if args.output:
        requested = args.output.expanduser().absolute()
        require(not any(p.is_symlink() for p in (requested, *requested.parents)), 'Evidence path traverses a symlink')
        target = requested.resolve()
        require(target.is_relative_to(lab) and target != lab, 'Evidence must remain inside the lab')
        require(not target.exists(), 'Evidence already exists; use a new filename to preserve history')

    def run(command, *, stdin=None, timeout=40):
        process = subprocess.run(command, input=stdin, capture_output=True, text=True, timeout=timeout)
        output = (process.stdout + process.stderr).strip()
        for value in redactions:
            output = output.replace(value, '[REDACTED]')
        output = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b', '[REDACTED]', output)
        return (process.returncode, output)

    def check(name, command, *, stdin=None, expected=None, timeout=40):
        try:
            code, output = run(command, stdin=stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            code, output = (124, 'Timed out')
        passed = code == 0 and (expected is None or expected(output))
        report.setdefault('checks', {})[name] = {'passed': passed, 'exit_code': code, 'output': output[:5000]}
        if not passed:
            errors.append(name)
        return output
    code, output = run(compose + ['config', '--format', 'json'])
    require(code == 0, output)
    config = json.loads(output)
    require(config['name'] == 'haxpower-erpnext-v16-lab', "Lab safety check failed: config['name'] == 'haxpower-erpnext-v16-lab'")
    services = config['services']
    require(set(services) == {'frappe', 'mariadb', 'redis-cache', 'redis-queue'}, "Lab safety check failed: set(services) == {'frappe', 'mariadb', 'redis-cache', 'redis-queue'}")
    require(config['networks']['backend']['internal'] is True, "Lab safety check failed: config['networks']['backend']['internal'] is True")
    require(all((not item.get('external') for item in config['networks'].values())), "Lab safety check failed: all((not item.get('external') for item in config['networks'].values()))")
    require(all((not item.get('external') for item in config['volumes'].values())), "Lab safety check failed: all((not item.get('external') for item in config['volumes'].values()))")
    for name, service in services.items():
        image = 'bench' if name == 'frappe' else 'redis' if name.startswith('redis-') else 'mariadb'
        require(service['image'] == lock['images'][image]['reference'], f'{name}: image digest differs from lock')
        require(not service.get('ports') and (not service.get('privileged')), f'{name}: published ports or privileged mode are forbidden')
        require(service.get('network_mode') != 'host', f'{name}: host networking is forbidden')
        require(not service.get('cap_add'), f'{name}: extra capabilities are forbidden')
        require(not service.get('build'), f'{name}: use reviewed prebuilt images')
        require(service.get('restart', 'no') == 'no', f'{name}: automatic restart is not configured for this lab')
        require(not any(('PROXY' in key.upper() for key in service.get('environment', {}))), f'{name}: unreviewed proxy environment')
        if name != 'frappe':
            require(set(service['networks']) == {'backend'}, f'{name}: must only join the internal backend')
    frappe = services['frappe']
    require(frappe['command'] == ['sleep', 'infinity'] and (not frappe.get('entrypoint')), "Lab safety check failed: frappe['command'] == ['sleep', 'infinity'] and (not frappe.get('entrypoint'))")
    require(frappe['user'] == '1000:1000', "Lab safety check failed: frappe['user'] == '1000:1000'")
    require(not frappe.get('secrets'), "Lab safety check failed: not frappe.get('secrets')")
    require(len(frappe['volumes']) == 1, "Lab safety check failed: len(frappe['volumes']) == 1")
    mount = frappe['volumes'][0]
    require(mount['source'] == str(lab / 'workspace') and mount['target'] == '/workspace/development', "Lab safety check failed: mount['source'] == str(lab / 'workspace') and mount['target'] == '/workspace/development'")
    require(services['mariadb']['environment']['MARIADB_ROOT_PASSWORD_FILE'] == '/run/secrets/mariadb_root_password', "Lab safety check failed: services['mariadb']['environment']['MARIADB_ROOT_PASSWORD_FILE'] == '/run/secrets/mariadb_root_password'")
    require('MARIADB_ROOT_PASSWORD' not in services['mariadb']['environment'], "Lab safety check failed: 'MARIADB_ROOT_PASSWORD' not in services['mariadb']['environment']")
    require(Path(config['secrets']['mariadb_root_password']['file']).resolve() == lab / 'secrets/mariadb-root-password', "Lab safety check failed: Path(config['secrets']['mariadb_root_password']['file']).resolve() == lab / 'secrets/mariadb-root-password'")
    require(lab.stat().st_mode & 0o777 == 0o700, 'Lab directory must have mode 0700')
    require((lab / 'secrets').stat().st_mode & 0o777 == 0o700, 'Secrets directory must have mode 0700')
    dev = json.loads((lab / '.devcontainer/devcontainer.json').read_text())
    require(not dev.get('mounts'), "Lab safety check failed: not dev.get('mounts')")
    require(not any((key.endswith('Command') for key in dev if key != 'overrideCommand')), "Lab safety check failed: not any((key.endswith('Command') for key in dev if key != 'overrideCommand'))")
    require(dev['customizations']['vscode']['settings']['remote.localPortHost'] == 'localhost', "Lab safety check failed: dev['customizations']['vscode']['settings']['remote.localPortHost'] == 'localhost'")
    report['configuration'] = {'passed': True, 'services': sorted(services), 'images_pinned': True, 'published_ports': [], 'secret_in_environment': False, 'ssh_mounts': False, 'automatic_install_commands': False}
    if not args.config_only:
        ids = []
        container_ids = {}
        runtime = {}
        for service in services:
            code, container_id = run(compose + ['ps', '--all', '--quiet', service])
            require(code == 0 and container_id, f'Missing container: {service}')
            code, inspected = run(['docker', 'inspect', container_id])
            require(code == 0, 'Lab safety check failed: code == 0')
            item = json.loads(inspected)[0]
            ids.append(container_id)
            container_ids[service] = container_id
            state = item['State']
            health = state.get('Health', {}).get('Status')
            bindings = item['HostConfig'].get('PortBindings') or {}
            passed = state['Status'] == 'running' and health == 'healthy' and (not bindings)
            require(item['Config']['Labels']['com.docker.compose.project'] == config['name'], "Lab safety check failed: item['Config']['Labels']['com.docker.compose.project'] == config['name']")
            image_key = 'bench' if service == 'frappe' else 'redis' if service.startswith('redis-') else 'mariadb'
            require(item['Image'] == lock['images'][image_key]['image_id'], "Lab safety check failed: item['Image'] == lock['images'][image_key]['image_id']")
            require(not item['HostConfig']['Privileged'], "Lab safety check failed: not item['HostConfig']['Privileged']")
            if service == 'frappe':
                require(item['Config']['User'] == '1000:1000', "Lab safety check failed: item['Config']['User'] == '1000:1000'")
                require('ALL' in item['HostConfig']['CapDrop'], "Lab safety check failed: 'ALL' in item['HostConfig']['CapDrop']")
                require('no-new-privileges:true' in item['HostConfig']['SecurityOpt'], "Lab safety check failed: 'no-new-privileges:true' in item['HostConfig']['SecurityOpt']")
            require(all((not key.split('=', 1)[0].upper().endswith('PROXY') for key in item['Config']['Env'])), "Lab safety check failed: all((not key.split('=', 1)[0].upper().endswith('PROXY') for key in item['Config']['Env']))")
            runtime[service] = {'passed': passed, 'container': item['Name'].lstrip('/'), 'status': state['Status'], 'health': health, 'image_id': item['Image'], 'ports': bindings, 'memory_limit_bytes': item['HostConfig']['Memory'], 'mounts': [{'type': m['Type'], 'destination': m['Destination'], 'volume': m.get('Name')} for m in item['Mounts']]}
            if not passed:
                errors.append(service)
        report['containers'] = runtime
        networks = {}
        for name in ('backend', 'egress'):
            full_name = config['networks'][name]['name']
            code, output = run(['docker', 'network', 'inspect', full_name])
            require(code == 0, 'Lab safety check failed: code == 0')
            network = json.loads(output)[0]
            require(network['Labels']['com.docker.compose.project'] == config['name'], "Lab safety check failed: network['Labels']['com.docker.compose.project'] == config['name']")
            members = set(network.get('Containers', {}))
            require(network['Internal'] == (name == 'backend'), "Lab safety check failed: network['Internal'] == (name == 'backend')")
            expected = set(ids) if name == 'backend' else {container_ids['frappe']}
            require(members == expected, f'Unexpected containers on {full_name}')
            networks[name] = {'name': full_name, 'internal': network['Internal'], 'container_count': len(members)}
        report['networks'] = networks
        volume_names = []
        for volume in config['volumes'].values():
            code, output = run(['docker', 'volume', 'inspect', volume['name']])
            require(code == 0, 'Lab safety check failed: code == 0')
            item = json.loads(output)[0]
            require(item['Labels']['com.docker.compose.project'] == config['name'], "Lab safety check failed: item['Labels']['com.docker.compose.project'] == config['name']")
            volume_names.append(item['Name'])
        report['volumes'] = sorted(volume_names)
        exec_base = compose + ['exec', '-T', 'frappe']
        tools_script = TOOLS_CHECK
        check('development_tools', exec_base + ['python', '-c', tools_script])
        sql_script = SQL_CHECK
        check('mariadb_from_frappe', exec_base + ['python', '-c', sql_script], stdin=secret, expected=lambda out: out.startswith('11.8.') and 'utf8mb4' in out)
        reject_script = REJECT_CHECK
        check('mariadb_rejects_bad_password', exec_base + ['python', '-c', reject_script])
        for service in ('redis-cache', 'redis-queue'):
            check(service + '_from_frappe', exec_base + ['redis-cli', '-h', service, 'PING'], expected=lambda out: out == 'PONG')
            check(service + '_version', compose + ['exec', '-T', service, 'redis-server', '--version'], expected=lambda out: 'v=7.4.' in out)
        for name, url in {'github_https': 'https://github.com', 'pypi_https': 'https://pypi.org/simple/frappe-bench/', 'npm_https': 'https://registry.npmjs.org/frappe-ui'}.items():
            script = 'import urllib.request; r=urllib.request.urlopen(' + repr(url) + ',timeout=15); print(r.status); r.read(1024)'
            check(name, exec_base + ['python', '-c', script], expected=lambda out: out == '200', timeout=25)
        check('github_git', exec_base + ['git', '-c', 'http.connectTimeout=15', 'ls-remote', '--exit-code', 'https://github.com/frappe/frappe.git', 'refs/tags/v16.55.0'], expected=lambda out: lock['applications']['frappe']['commit'] in out, timeout=30)
        check('python_package_index', exec_base + ['python', '-m', 'pip', 'index', 'versions', 'frappe-bench', '--index-url', 'https://pypi.org/simple/', '--timeout', '15', '--retries', '0'], timeout=30)
        check('npm_registry', exec_base + ['npm', 'ping', '--registry=https://registry.npmjs.org', '--fetch-retries=0', '--fetch-timeout=15000'], timeout=25)
        check('yarn_registry', exec_base + ['yarn', 'info', 'frappe-ui', 'version', '--json', '--registry', 'https://registry.npmjs.org', '--network-timeout', '15000'], timeout=30)
        check('resource_usage', ['docker', 'stats', '--no-stream', '--format', '{{json .}}', *ids])
        logs = {}
        for service in services:
            code, output = run(compose + ['logs', '--no-color', '--tail', '80', service])
            if code:
                errors.append(service + '_logs')
            noteworthy = [line for line in output.splitlines() if re.search('error|warn|ready|initialized', line, re.I)]
            logs[service] = {'read_exit_code': code, 'noteworthy_lines': noteworthy}
        report['log_review'] = logs
        if args.phase in ('bench', 'site'):
            check_applications(check, compose, lock, args.phase)
    report['passed'] = not errors
    report['failed_checks'] = errors
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + '\n'
    if target:
        with target.open('x') as handle:
            handle.write(rendered)
    print(rendered)
    raise SystemExit(1 if errors else 0)
if __name__ == '__main__':
    try:
        main()
    except (ValidationError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({'passed': False, 'error': str(error)}))
        raise SystemExit(2)
