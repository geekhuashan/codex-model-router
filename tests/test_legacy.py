import json
from types import SimpleNamespace

import pytest
import tomlkit

from codex_model_router.legacy import enable, main, restore


@pytest.fixture
def installation(tmp_path):
    home = tmp_path / 'codex'
    state = tmp_path / 'state'
    home.mkdir()
    state.mkdir()
    (home / 'config.toml').write_text('''model_provider = "old-provider"
model = "native-model"
[model_providers.old-provider]
name = "Old provider"
base_url = "https://example.com/v1"
env_key = "EXAMPLE_KEY"
experimental_bearer_token = "fake-secret-for-test"
[model_providers.old-provider.http_headers]
X-Example = "example"
[model_providers.other]
name = "Other"
base_url = "https://other.example.com/v1"
''')
    (state / 'router.json').write_text(json.dumps({'port': 18791, 'local_token': 'fake-local-token', 'routes': {}}))
    return SimpleNamespace(codex_home=home, state_dir=state, provider='old-provider')


def read(args):
    return tomlkit.parse((args.codex_home / 'config.toml').read_text())


def change(args, mutate):
    doc = read(args)
    mutate(doc)
    (args.codex_home / 'config.toml').write_text(tomlkit.dumps(doc))


def test_bridge_and_restore_preserve_other_changes(installation):
    args = installation
    before = read(args)
    enable(args)
    doc = read(args)
    assert doc['model_provider'] == 'openai'
    provider = doc['model_providers']['old-provider']
    assert set(provider) == {'name', 'base_url', 'wire_api', 'requires_openai_auth'}
    assert provider['requires_openai_auth'] is True
    assert provider['base_url'] == 'http://127.0.0.1:18791/fake-local-token/v1'
    assert doc['model_providers']['other'] == before['model_providers']['other']
    for path in list(args.state_dir.glob('legacy-*')) + [args.codex_home / 'config.toml']:
        assert path.stat().st_mode & 0o777 == 0o600
    change(args, lambda d: d.__setitem__('model', 'new-model'))
    restore(args)
    assert read(args)['model'] == 'new-model'
    assert read(args)['model_provider'] == 'old-provider'
    assert read(args)['model_providers'] == before['model_providers']


@pytest.mark.parametrize('reserved', ['openai', 'ollama', 'lmstudio', '../old'])
def test_reserved_or_invalid_ids_rejected(installation, reserved):
    installation.provider = reserved
    before = read(installation)
    with pytest.raises(ValueError):
        enable(installation)
    assert read(installation) == before


def test_dependent_credential_rejected(installation):
    args = installation
    path = args.state_dir / 'router.json'
    router = json.loads(path.read_text())
    router['routes'] = {'example/model': {'credential': {'kind': 'codex_provider', 'provider': args.provider}}}
    path.write_text(json.dumps(router))
    with pytest.raises(ValueError, match='Migrate'):
        enable(args)
    assert read(args)['model_provider'] == args.provider
    assert not (args.state_dir / 'legacy-old-provider.json').exists()


@pytest.mark.parametrize('field', ['root', 'table'])
def test_restore_refuses_external_changes(installation, field):
    args = installation
    enable(args)
    if field == 'root':
        change(args, lambda d: d.__setitem__('model_provider', 'other'))
    else:
        change(args, lambda d: d['model_providers'][args.provider].__setitem__('env_key', 'OTHER_KEY'))
    before = read(args)
    with pytest.raises(ValueError, match='changed externally'):
        restore(args)
    assert read(args) == before


def test_untouched_root_may_change(installation):
    args = installation
    change(args, lambda d: d.__setitem__('model_provider', 'other'))
    enable(args)
    change(args, lambda d: d.__setitem__('model_provider', 'openai'))
    restore(args)
    assert read(args)['model_provider'] == 'openai'


def test_cli_no_credentials_in_output_and_warns_restart(installation, capsys):
    args = installation
    flags = ['--state-dir', str(args.state_dir), '--codex-home', str(args.codex_home), '--provider', args.provider]
    assert main(['enable', *flags]) == 0
    output = capsys.readouterr()
    assert 'Restart' in output.out
    assert 'fake-' not in output.out + output.err
    with pytest.raises(SystemExit):
        main(['enable', *flags])
    output = capsys.readouterr()
    assert 'fake-' not in output.out + output.err
    assert main(['restore', *flags]) == 0


def test_restore_pending_receipt_after_config_write_failure(installation, monkeypatch):
    import codex_model_router.legacy as legacy
    args = installation
    original = read(args)
    real_write = legacy.write_private

    def fail_config(path, text):
        if path == args.codex_home / 'config.toml':
            raise OSError('simulated write failure')
        real_write(path, text)

    monkeypatch.setattr(legacy, 'write_private', fail_config)
    with pytest.raises(OSError):
        enable(args)
    assert read(args) == original
    restore(args)
    assert not (args.state_dir / 'legacy-old-provider.json').exists()
    assert read(args) == original
