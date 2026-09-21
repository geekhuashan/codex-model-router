"""Configuration tests use temporary homes and never read real credentials."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import tomlkit

from codex_model_router import configure


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / 'router'
        self.home = self.root / 'codex'
        self.home.mkdir()
        self.model = {
            'slug': 'native-model', 'display_name': 'Native',
            'use_responses_lite': True, 'service_tiers': ['fast'],
            'prefer_websockets': True, 'supports_search_tool': True,
            'web_search_tool_type': 'text', 'apply_patch_tool_type': 'freeform',
            'available_access_programs': {'sample': {'enabled': True}},
            'future_schema': {'preserve': [1, 2]},
        }
        (self.home / 'models_cache.json').write_text(json.dumps({
            'models': [self.model], 'identity': 'synthetic-private-marker',
        }))

    def run_cli(self, command, *extra, success=True):
        args = [command, '--state-dir', str(self.state)]
        if command != 'add':
            args += ['--codex-home', str(self.home)]
        args += list(extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if success:
                self.assertEqual(configure.main(args), 0)
            else:
                with self.assertRaises(SystemExit) as raised:
                    configure.main(args)
                self.assertNotEqual(raised.exception.code, 0)
        return out.getvalue() + err.getvalue()

    def add_model(self, *extra, success=True):
        return self.run_cli('add', '--name', 'example', '--model', 'model-id',
                            '--label', 'Label', '--base-url', 'https://api.example.com/v1',
                            '--api-key-env', 'EXAMPLE_API_KEY', '--template', 'native-model',
                            *extra, success=success)

    def test_init_and_add_preserve_schema_without_identity_or_keys(self):
        output = self.run_cli('init')
        config = configure.read_json(self.state / 'router.json')
        self.assertNotIn(config['local_token'], output)
        self.assertEqual(config['routes'], {})
        self.assertEqual(config['native_models'], ['native-model'])
        self.assertEqual((self.state / 'router.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(configure.read_json(self.state / 'models.json'), {'models': [self.model]})
        self.add_model()
        route = configure.read_json(self.state / 'router.json')['routes']['example/model-id']
        self.assertEqual(route['kind'], 'responses')
        self.assertEqual(route['credential'], {'kind': 'env', 'key': 'EXAMPLE_API_KEY'})
        added = configure.read_json(self.state / 'models.json')['models'][1]
        self.assertFalse(added['supports_search_tool'])
        self.assertFalse(added['use_responses_lite'])
        self.assertFalse(added['prefer_websockets'])
        self.assertEqual(added['service_tiers'], [])
        for key in ('future_schema', 'available_access_programs', 'web_search_tool_type', 'apply_patch_tool_type'):
            self.assertEqual(added[key], self.model[key])

    def test_invalid_add_leaves_state_unchanged(self):
        self.run_cli('init')
        for extra in [('--template', 'missing'), ('--api-key-env', 'BAD-KEY'),
                      ('--base-url', 'https://user:password@example.com/v1'),
                      ('--base-url', 'https://example.com/v1?secret=value'),
                      ('--base-url', 'https://example.com/v1#fragment'),
                      ('--base-url', 'ftp://example.com'),
                      ('--base-url', 'http://example.com/v1')]:
            before = (self.state / 'router.json').read_bytes()
            self.add_model(*extra, success=False)
            self.assertEqual((self.state / 'router.json').read_bytes(), before)
        self.add_model()
        self.add_model(success=False)
        self.run_cli('init', success=False)

    def test_enable_restore_preserves_other_edits_and_auth(self):
        self.run_cli('init')
        config = self.home / 'config.toml'
        config.write_text('# original\nmodel_provider = "old"\nmodel = "native-model"\n\n[features]\nexample = true\n')
        auth = self.home / 'auth.json'
        auth.write_text('synthetic auth fixture')
        self.run_cli('enable')
        enabled = tomlkit.parse(config.read_text())
        self.assertEqual(enabled['model_provider'], 'openai')
        self.assertTrue(enabled['openai_base_url'].startswith('http://127.0.0.1:18791/'))
        self.assertEqual(enabled['model_catalog_json'], str(self.state / 'models.json'))
        enabled['model'] = 'another-model'
        enabled['features']['concurrent'] = True
        config.write_text(tomlkit.dumps(enabled))
        self.run_cli('restore')
        restored = tomlkit.parse(config.read_text())
        self.assertEqual(restored['model_provider'], 'old')
        self.assertNotIn('openai_base_url', restored)
        self.assertNotIn('model_catalog_json', restored)
        self.assertEqual(restored['model'], 'another-model')
        self.assertTrue(restored['features']['concurrent'])
        self.assertEqual(auth.read_text(), 'synthetic auth fixture')
        self.assertIn('# original', config.read_text())

    def test_restore_refuses_external_edit_to_installed_field(self):
        self.run_cli('init')
        self.run_cli('enable')
        config = self.home / 'config.toml'
        edited = tomlkit.parse(config.read_text())
        edited['openai_base_url'] = 'https://example.com/v1'
        config.write_text(tomlkit.dumps(edited))
        before = config.read_bytes()
        self.run_cli('restore', success=False)
        self.assertEqual(config.read_bytes(), before)
        self.assertTrue((self.state / 'installation.json').exists())

    def test_restore_existing_root_values(self):
        self.run_cli('init')
        config = self.home / 'config.toml'
        original = {'model_provider': 'old', 'openai_base_url': 'https://example.com/v1',
                    'model_catalog_json': '/synthetic/catalog.json'}
        config.write_text(tomlkit.dumps(original))
        self.run_cli('enable')
        self.run_cli('restore')
        self.assertEqual(tomlkit.parse(config.read_text()).unwrap(), original)


if __name__ == '__main__':
    unittest.main()
