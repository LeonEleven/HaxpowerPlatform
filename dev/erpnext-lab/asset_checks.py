"""Verify actual login and Desk assets, not just manifest HTTP status codes.

The full probe runs inside the pinned Bench virtualenv. It never logs in or
prints HTML, boot data, cookies, site settings, or the Setup Wizard script.
"""

import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import unquote, urlsplit
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


class AssetError(RuntimeError):
    pass


class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'link' and 'stylesheet' in attrs.get('rel', '').lower().split():
            value, kind = attrs.get('href'), 'css'
        elif tag == 'script':
            value, kind = attrs.get('src'), 'js'
        else:
            return
        if value and urlsplit(value).path.startswith('/assets/'):
            self.assets[asset_path(value)] = kind


def asset_path(value):
    url = urlsplit(value)
    path = unquote(url.path)
    if url.username or url.password or (url.netloc and url.hostname not in ('localhost', 'haxpower.localhost')):
        raise AssetError('Asset points outside the local lab')
    if not path.startswith('/assets/') or any(part in ('.', '..') for part in path.split('/')):
        raise AssetError('Unsafe asset path')
    if url.scheme and url.scheme != 'http':
        raise AssetError('Unexpected asset URL scheme')
    if url.port is not None and url.port != 8000:
        raise AssetError('Unexpected asset URL port')
    return path


def inspect_asset(root, path, kind, status, content_type, body, final_path):
    path = asset_path(path)
    if status != 200 or final_path != path:
        raise AssetError('Asset failed or redirected: ' + path)
    mime = content_type.split(';', 1)[0].strip().lower()
    expected = {'css': {'text/css'}, 'js': {'text/javascript', 'application/javascript'}}
    if kind not in expected or mime not in expected[kind]:
        raise AssetError('Wrong asset MIME type: ' + path + ' (' + mime + ')')
    if not body or body.lstrip().lower().startswith((b'<!doctype html', b'<html')):
        raise AssetError('Empty or HTML asset response: ' + path)
    target = (root/'sites'/path.lstrip('/')).resolve()
    allowed = [root/'sites/assets', root/'apps/frappe/frappe/public', root/'apps/erpnext/erpnext/public']
    if not any(target.is_relative_to(directory) for directory in allowed):
        raise AssetError('Asset symlink leaves the approved public directories: ' + path)
    if not target.is_file():
        raise AssetError('Asset missing from disk: ' + path)
    digest = hashlib.sha256(body).hexdigest()
    if digest != hashlib.sha256(target.read_bytes()).hexdigest():
        raise AssetError('HTTP body differs from the built file: ' + path)
    return {'url': path, 'status': status, 'content_type': content_type,
            'bytes': len(body), 'sha256': digest, 'matches_disk': True,
            'prefix': body[:100].decode('utf-8', errors='replace')}


def desk_references(root):
    """Resolve the real Desk template hooks without accessing browser sessions."""
    import os
    import frappe
    from frappe.utils.jinja_globals import bundled_asset
    from frappe.desk.desk_page import get
    os.chdir(root/'sites')
    frappe.init(site='haxpower.localhost', sites_path='.')
    try:
        frappe.connect()
        assets = {}
        for key, kind in (('app_include_css', 'css'), ('app_include_js', 'js')):
            values = list(frappe.get_hooks(key, [])) + list(frappe.conf.get(key, []))
            assets.update({asset_path(bundled_asset(value)): kind for value in values})
        # This is a read-only Page loader, not a setup/fixture installation call.
        page = get('setup-wizard')
        script = page.get('script') or ''
        syntax = subprocess.run(['node', '--check'], input=script, text=True, capture_output=True, timeout=20)
        if not script or syntax.returncode:
            raise AssetError('Setup Wizard Page JavaScript is missing or invalid')
        metadata = {'source': 'Desk hooks and permitted Setup Wizard Page loader',
                    'script_bytes': len(script.encode()),
                    'script_sha256': hashlib.sha256(script.encode()).hexdigest(),
                    'javascript_syntax_ok': True,
                    'page_css_bytes': len((page.get('style') or '').encode()),
                    'authenticated_browser_tested': False}
        return assets, metadata
    finally:
        frappe.destroy()
        os.chdir(root)


def collect(root):
    opener = build_opener(ProxyHandler({}))
    def fetch(host, path):
        request = Request('http://127.0.0.1:8000'+path, headers={'Host': host})
        try:
            response = opener.open(request, timeout=20)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers.get('Content-Type', ''), response.read(), urlsplit(response.url).path

    desk, wizard = desk_references(root)
    manifest = json.loads((root/'sites/assets/assets.json').read_text())
    result = {'hosts': {}, 'setup_wizard': wizard, 'passed': True}
    for host in ('localhost', 'haxpower.localhost'):
        status, mime, html, final = fetch(host, '/login')
        if status != 200 or final != '/login' or not mime.startswith('text/html'):
            raise AssetError('Login page unavailable for ' + host)
        parser = References()
        parser.feed(html.decode('utf-8'))
        if not any(k == 'css' for k in parser.assets.values()) or not any(k == 'js' for k in parser.assets.values()):
            raise AssetError('Login HTML contains no expected CSS/JS references')
        resources = dict(parser.assets)
        resources.update(desk)
        rows = []
        for path, kind in sorted(resources.items()):
            rows.append(inspect_asset(root, path, kind, *fetch(host, path)))
        for key in ('login.bundle.css', 'website.bundle.css', 'desk.bundle.css', 'frappe-web.bundle.js'):
            if manifest.get(key) not in resources:
                raise AssetError('Rendered assets and manifest disagree: ' + key)
        api_status, api_mime, api_body, api_path = fetch(host, '/api/method/ping')
        if api_status != 200 or api_path != '/api/method/ping' or not api_mime.startswith('application/json') or json.loads(api_body).get('message') != 'pong':
            raise AssetError('API ping failed for ' + host)
        result['hosts'][host] = {'login': 200, 'api_ping': 'pong',
                                 'login_references': parser.assets,
                                 'desk_references': desk, 'assets': rows}
    return result


if __name__ == '__main__':
    result = collect(Path('/workspace/development/frappe-bench'))
    if '--full' not in sys.argv:
        result = {'passed': result['passed'], 'setup_wizard': result['setup_wizard'],
                  'hosts': {host: {'login': data['login'], 'api_ping': data['api_ping'],
                                   'asset_count': len(data['assets']),
                                   'css_js_mime_and_disk_content_match': True}
                            for host, data in result['hosts'].items()}}
    print(json.dumps(result, indent=2))
