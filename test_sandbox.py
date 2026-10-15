"""Offline contracts for the actual distributed sandbox support scripts."""
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tarfile
import types
import urllib.error
import urllib.request

import pytest

SUPPORT = Path(__file__).parent / 'sandbox'
APP = SUPPORT / 'scripts/apps.py'
BOOTSTRAP = SUPPORT / 'bootstrap.sh'
REVISION = 'a' * 40


@pytest.fixture
def app(tmp_path, monkeypatch):
    module = types.ModuleType('recipe_under_test')
    module.__file__ = str(APP)
    exec(compile(APP.read_text(), module.__file__, 'exec'), module.__dict__)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.setattr(module, 'WORKSPACE', workspace)

    def forbidden(url):
        pytest.fail(f'unexpected recipe download: {url}')

    monkeypatch.setattr(module, 'fetch', forbidden)
    yield module
    for record in tmp_path.rglob('process.json'):
        metadata = json.loads(record.read_text())
        if metadata.get('start') and module.identity(metadata['pid']) == metadata['start']:
            try:
                os.killpg(metadata['pid'], signal.SIGTERM)
                os.waitpid(metadata['pid'], 0)
            except ProcessLookupError:
                pass


def available_port():
    for port in range(8100, 9999):
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', port))
            except OSError:
                continue
            return port
    pytest.fail('no free recipe test port')


def bootstrap(*args, cache, environment=None):
    guard = cache.parent / 'network-guard'
    guard.mkdir(exist_ok=True)
    (guard / 'sitecustomize.py').write_text(
        'import urllib.request\n'
        'def forbidden(*args, **kwargs):\n'
        '    raise RuntimeError("unexpected bootstrap HTTP request")\n'
        'urllib.request.urlopen = forbidden\n'
    )
    return subprocess.run(['sh', str(BOOTSTRAP), *map(str, args)], text=True,
                          capture_output=True, env={**os.environ, 'XDG_CACHE_HOME': str(cache),
                                                    'PYTHONPATH': str(guard),
                                                    **(environment or {})}, timeout=20)


def test_exact_local_snapshot_and_cache_tampering(tmp_path):
    cache = tmp_path / 'cache'
    digest = bootstrap('--hash', SUPPORT, cache=cache)
    assert digest.returncode == 0, digest.stderr
    result = bootstrap(REVISION, '--seed', SUPPORT, '--sha256', digest.stdout.strip(), cache=cache)
    assert result.returncode == 0, result.stderr
    snapshot = Path(result.stdout.strip())
    assert snapshot == cache / 'lathe' / REVISION
    assert {p.relative_to(snapshot): p.read_bytes() for p in snapshot.rglob('*') if p.is_file()} == {
        p.relative_to(SUPPORT): p.read_bytes() for p in SUPPORT.rglob('*') if p.is_file()}
    # Reuse works without seed and does not attempt network acquisition.
    reused = bootstrap(REVISION, cache=cache)
    assert reused.returncode == 0, reused.stderr
    (snapshot / 'scripts/apps.py').write_text('tampered')
    changed = bootstrap(REVISION, cache=cache)
    assert changed.returncode != 0
    assert 'cached sandbox support snapshot changed' in changed.stderr


def test_source_digest_is_regular_file_content_not_metadata(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'z').write_bytes(b'last')
    (source / 'a').mkdir()
    (source / 'a/first').write_bytes(b'first\x00bytes')
    expected = hashlib.sha256()
    for name, content in [('a/first', b'first\x00bytes'), ('z', b'last')]:
        expected.update(name.encode() + b'\x00' + hashlib.sha256(content).digest())
    first = bootstrap('--hash', source, cache=tmp_path / 'cache')
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == expected.hexdigest()
    (source / 'empty-directory').mkdir()
    (source / 'z').chmod(0o755)
    os.utime(source / 'z', (1, 1))
    second = bootstrap('--hash', source, cache=tmp_path / 'cache')
    assert second.returncode == 0, second.stderr
    assert second.stdout == first.stdout


@pytest.mark.parametrize('revision', ['main', 'latest', '../outside', 'a' * 39])
def test_bootstrap_rejects_mutable_or_invalid_revision(tmp_path, revision):
    result = bootstrap(revision, '--seed', SUPPORT, cache=tmp_path)
    assert result.returncode != 0
    assert 'full 40-character' in result.stderr
    assert not (tmp_path / 'lathe').exists()


def test_bootstrap_layout_digest_and_link_guards(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'app.py').write_text('not a complete recipe')
    result = bootstrap(REVISION, '--seed', source, cache=tmp_path / 'cache')
    assert result.returncode != 0
    assert 'expected app/bootstrap/skill layout' in result.stderr
    result = bootstrap(REVISION, '--seed', SUPPORT, '--sha256', '0' * 64, cache=tmp_path / 'cache')
    assert result.returncode != 0
    assert 'digest mismatch' in result.stderr
    (source / 'link').symlink_to(APP)
    result = bootstrap('--hash', source, cache=tmp_path / 'cache')
    assert result.returncode != 0
    assert 'cannot contain symlinks' in result.stderr


@pytest.mark.parametrize('unsafe', [False, True])
def test_bootstrap_actual_archive_path_with_fixture_transport(tmp_path, unsafe):
    archive_path = tmp_path / 'source.tar.gz'
    with tarfile.open(archive_path, 'w:gz') as archive:
        for path in sorted(SUPPORT.rglob('*')):
            if path.is_file():
                archive.add(path, arcname='repo/sandbox/' + path.relative_to(SUPPORT).as_posix())
        if unsafe:
            member = tarfile.TarInfo('repo/sandbox/../../escaped')
            archive.addfile(member, io.BytesIO())
    # Replace only the subprocess's network transport, not bootstrap logic.
    (tmp_path / 'sitecustomize.py').write_text(
        'import io, os, urllib.request\n'
        'def fixture(request, **kwargs):\n'
        f'    assert request.full_url == "https://api.github.com/repos/rndmcnlly/lathe/tarball/{REVISION}"\n'
        '    return io.BytesIO(open(os.environ["FIXTURE_ARCHIVE"], "rb").read())\n'
        'urllib.request.urlopen = fixture\n'
    )
    result = bootstrap(REVISION, cache=tmp_path / 'cache',
                       environment={'PYTHONPATH': str(tmp_path), 'FIXTURE_ARCHIVE': str(archive_path)})
    if unsafe:
        assert result.returncode != 0 and 'unsafe sandbox archive' in result.stderr
        assert not (tmp_path / 'cache/lathe' / REVISION).exists()
    else:
        assert result.returncode == 0, result.stderr
        snapshot = Path(result.stdout.strip())
        assert {p.relative_to(snapshot): p.read_bytes() for p in snapshot.rglob('*') if p.is_file()} == {
            p.relative_to(SUPPORT): p.read_bytes() for p in SUPPORT.rglob('*') if p.is_file()}


def test_root_resolution_requires_real_workspace_directory(app, tmp_path):
    root = app.WORKSPACE / 'project with spaces'
    root.mkdir()
    assert app.root_path(str(root)) == root
    for value in ['relative', str(tmp_path), str(root / 'missing')]:
        with pytest.raises((ValueError, FileNotFoundError)):
            app.root_path(value)
    (root / 'escape').symlink_to(tmp_path)
    with pytest.raises(ValueError, match='within'):
        app.root_path(str(root / 'escape'))


def test_cli_rejects_root_before_install_or_state_creation(tmp_path):
    result = subprocess.run([sys.executable, str(APP), 'dufs', '--root', 'relative',
                             '--state', str(tmp_path / 'state')], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'absolute path' in result.stderr
    assert not (tmp_path / 'state').exists()


@pytest.mark.parametrize('options,message', [(['--port', '2999'], 'port must be'),
                                           (['--port', '10000'], 'port must be'),
                                           (['--timeout', '0'], 'timeout must be'),
                                           (['--state', 'relative'], 'state must be')])
def test_cli_option_guards_before_service_io(app, monkeypatch, capsys, options, message):
    monkeypatch.setattr(sys, 'argv', ['app.py', 'site', '--root', str(app.WORKSPACE), *options])
    monkeypatch.setattr(app, 'ensure', lambda *args: pytest.fail('service I/O before validation'))
    with pytest.raises(SystemExit) as error:
        app.main()
    assert error.value.code == 1
    assert message in capsys.readouterr().err


def test_real_site_readiness_reuse_root_conflict_and_recovery(app, tmp_path):
    root = app.WORKSPACE
    (root / 'index.html').write_text('recipe served these actual bytes')
    state, port = tmp_path / 'state', available_port()
    first = app.ensure('site', root, port, state, 5)
    assert first['ready'] and first['status'] == 'started'
    assert first['expose'] == f'http:{port}'
    assert Path(first['log']).is_file()
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/') as response:
        assert response.read() == b'recipe served these actual bytes'
    reused = app.ensure('site', root, port, state, 5)
    assert reused['pid'] == first['pid'] and reused['status'] == 'reused'
    other = root / 'other'
    other.mkdir()
    with pytest.raises(ValueError, match='another recipe/root'):
        app.ensure('site', other, port, state, 5)
    os.killpg(first['pid'], signal.SIGTERM)
    # Reap this direct child so both Linux /proc and macOS ps see a dead process.
    os.waitpid(first['pid'], 0)
    recovered = app.ensure('site', root, port, state, 5)
    assert recovered['status'] == 'started' and recovered['pid'] != first['pid']


@pytest.mark.parametrize('kind', ['file', 'index'])
def test_real_static_server_denies_outside_root_symlinks(app, tmp_path, kind):
    secret = tmp_path / 'secret'
    secret.write_text('must not leak')
    root = app.WORKSPACE
    name = 'index.html' if kind == 'index' else 'leak'
    (root / name).symlink_to(secret)
    port = available_port()
    app.ensure('site', root, port, tmp_path / 'state', 5)
    path = '/' if kind == 'index' else '/leak'
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(f'http://127.0.0.1:{port}{path}')
    assert error.value.code == 403


def test_untracked_listener_is_not_reused(app, tmp_path):
    port = available_port()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', port))
        sock.listen()
        with pytest.raises(ValueError, match='untracked'):
            app.ensure('site', app.WORKSPACE, port, tmp_path / 'state', 1)


@pytest.mark.parametrize('mode', ['exit', 'timeout'])
def test_startup_failure_is_logged_and_not_left_running(app, tmp_path, monkeypatch, mode):
    script = tmp_path / 'fixture.py'
    script.write_text('print("startup fixture", flush=True)\n' +
                      ('raise SystemExit(3)\n' if mode == 'exit' else 'import time; time.sleep(30)\n'))
    monkeypatch.setattr(app, 'command', lambda *args: [sys.executable, str(script)])
    state, port = tmp_path / 'state', available_port()
    with pytest.raises(ValueError, match='inspect'):
        app.ensure('site', app.WORKSPACE, port, state, 0.5)
    metadata = json.loads((state / 'services' / str(port) / 'process.json').read_text())
    os.waitpid(metadata['pid'], 0) if mode == 'timeout' else None
    assert app.identity(metadata['pid']) is None
    assert 'startup fixture' in (state / 'services' / str(port) / 'log').read_text()


def release(data, name='dufs-v1-x86_64-unknown-linux-musl.tar.gz', repo='sigoden/dufs'):
    url = f'https://github.com/{repo}/releases/download/v1/{name}'
    return {'tag_name': 'v1', 'url': f'https://api.github.com/repos/{repo}/releases/1',
            'assets': [{'name': name, 'browser_download_url': url,
                        'digest': 'sha256:' + hashlib.sha256(data).hexdigest()}]}


def archive_bytes(name, content=b'#!/bin/sh\necho fixture\n', link=None):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        member = tarfile.TarInfo(name)
        if link:
            member.type, member.linkname = tarfile.SYMTYPE, link
            archive.addfile(member)
        else:
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return stream.getvalue()


def test_shipped_installer_verifies_installs_and_reuses(app, tmp_path, monkeypatch):
    data = archive_bytes('dufs')
    document = release(data)
    calls = []

    def fetch(url):
        calls.append(url)
        if url == 'https://api.github.com/repos/sigoden/dufs/releases/latest':
            return json.dumps(document).encode()
        assert url == document['assets'][0]['browser_download_url']
        return data

    monkeypatch.setattr(app, 'fetch', fetch)
    monkeypatch.setattr(app.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(app.platform, 'machine', lambda: 'x86_64')
    binary = app.install('dufs', tmp_path)
    assert subprocess.check_output([str(binary)], text=True).strip() == 'fixture'
    assert json.loads((binary.parent / 'release.json').read_text())['tag'] == 'v1'
    assert app.install('dufs', tmp_path) == binary
    assert len(calls) == 2


@pytest.mark.parametrize('app_name', ['ttyd', 'code-server'])
def test_other_installers_use_verified_release_assets(app, tmp_path, monkeypatch, app_name):
    content = b'#!/bin/sh\necho fixture\n'
    if app_name == 'ttyd':
        name, data = 'ttyd.x86_64', content
    else:
        name = 'code-server-1-linux-amd64.tar.gz'
        data = archive_bytes('code-server-1-linux-amd64/bin/code-server', content)
    repo = app.REPOS[app_name]
    document = release(data, name=name, repo=repo)
    urls = {f'https://api.github.com/repos/{repo}/releases/latest': json.dumps(document).encode(),
            document['assets'][0]['browser_download_url']: data}
    if app_name == 'ttyd':
        sums = release(data, name='SHA256SUMS', repo=repo)['assets'][0]
        document['assets'].append(sums)
        urls[f'https://api.github.com/repos/{repo}/releases/latest'] = json.dumps(document).encode()
        urls[sums['browser_download_url']] = (hashlib.sha256(data).hexdigest() + '  ' + name).encode()
    monkeypatch.setattr(app, 'fetch', lambda url: urls[url])
    monkeypatch.setattr(app.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(app.platform, 'machine', lambda: 'x86_64')
    binary = app.install(app_name, tmp_path)
    assert subprocess.check_output([str(binary)], text=True).strip() == 'fixture'


@pytest.mark.parametrize('fault', ['digest', 'url', 'missing', 'duplicate'])
def test_release_integrity_guards(app, monkeypatch, fault):
    data = b'asset'
    document = release(data)
    asset = document['assets'][0]
    if fault == 'digest':
        asset['digest'] = 'sha256:' + '0' * 64
    elif fault == 'url':
        asset['browser_download_url'] = 'https://example.org/untrusted'
    elif fault == 'missing':
        document['assets'] = []
    else:
        document['assets'].append(asset.copy())
    monkeypatch.setattr(app, 'fetch', lambda url: data)
    with pytest.raises(ValueError):
        selected = app.release_asset(document, 'sigoden/dufs', asset['name'])
        app.verified_bytes(selected)


@pytest.mark.parametrize('name,link', [('../escape', None), ('/absolute', None), ('link', '../../escape')])
def test_archive_traversal_and_symlink_guards(app, tmp_path, name, link):
    stage = tmp_path / 'unpack'
    stage.mkdir()
    with pytest.raises(ValueError, match='archive'):
        app.unpack(archive_bytes(name, link=link), stage)
    assert not (tmp_path / 'escape').exists()
