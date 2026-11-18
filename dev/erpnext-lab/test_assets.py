"""Guard against static-resource false positives without Docker or a browser."""

from pathlib import Path
import tempfile
import unittest

from asset_checks import AssetError, References, asset_path, inspect_asset


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='haxpower-assets-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = '/assets/frappe/dist/css/login.bundle.TEST.css'
        self.file = self.root/'sites'/self.path.lstrip('/')
        self.file.parent.mkdir(parents=True)
        self.body = b'.page-card { width: 400px; padding: 24px; }'
        self.file.write_bytes(self.body)

    def check(self, **overrides):
        values = dict(root=self.root, path=self.path, kind='css', status=200,
                      content_type='text/css; charset=utf-8', body=self.body, final_path=self.path)
        values.update(overrides)
        return inspect_asset(**values)

    def test_actual_html_dependencies_are_collected(self):
        parser = References()
        parser.feed('<link rel="stylesheet" href="/assets/frappe/a.css">'
                    '<link rel="stylesheet" href="/assets/erpnext/b.css">'
                    '<script src="/assets/frappe/c.js"></script>'
                    '<link rel="shortcut icon" href="/assets/frappe/icon.svg">')
        self.assertEqual(parser.assets, {'/assets/frappe/a.css': 'css',
                                         '/assets/erpnext/b.css': 'css',
                                         '/assets/frappe/c.js': 'js'})

    def test_valid_css_requires_matching_file(self):
        self.assertTrue(self.check()['matches_disk'])

    def test_html_returned_with_200_is_rejected(self):
        with self.assertRaisesRegex(AssetError, 'MIME'):
            self.check(content_type='text/html', body=b'<!doctype html><html>Login</html>')

    def test_html_disguised_as_css_is_rejected(self):
        with self.assertRaisesRegex(AssetError, 'HTML'):
            self.check(body=b'<!doctype html><html>Login</html>')

    def test_redirect_to_login_is_rejected(self):
        with self.assertRaisesRegex(AssetError, 'redirected'):
            self.check(final_path='/login')

    def test_wrong_body_with_correct_mime_is_rejected(self):
        with self.assertRaisesRegex(AssetError, 'differs'):
            self.check(body=b'.wrong { color:red }')

    def test_missing_disk_file_is_rejected(self):
        self.file.unlink()
        with self.assertRaisesRegex(AssetError, 'missing'):
            self.check()

    def test_encoded_traversal_and_external_origins_are_rejected(self):
        for url in ('/assets/%2e%2e/private.css', 'http://example.com/assets/a.css',
                    'http://localhost:9000/assets/a.css'):
            with self.subTest(url=url), self.assertRaises(AssetError):
                asset_path(url)

    def test_public_symlink_cannot_read_private_file(self):
        private = self.root/'sites/haxpower.localhost/private.css'
        private.parent.mkdir(parents=True)
        private.write_bytes(self.body)
        linked = self.file.with_name(self.file.name.replace('TEST', 'linked'))
        linked.symlink_to(private)
        with self.assertRaisesRegex(AssetError, 'approved public'):
            self.check(path=self.path.replace('TEST', 'linked'),
                       final_path=self.path.replace('TEST', 'linked'))


if __name__ == '__main__':
    unittest.main()
