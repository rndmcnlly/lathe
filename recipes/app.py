#!/usr/bin/env python3
"""Ordinary sandbox services. No OWUI imports, credentials, or preview registration."""
import argparse
import fcntl
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import platform
import re
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

WORKSPACE = Path('/home/daytona/workspace')
REPOS = {'dufs': 'sigoden/dufs', 'ttyd': 'tsl0922/ttyd', 'code-server': 'coder/code-server'}
PORTS = {'dufs': 5000, 'ttyd': 7681, 'code-server': 8080, 'site': 8000}


def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'lathe-recipes'})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def release_asset(release, repo, name):
    tag = release.get('tag_name', '')
    if not re.fullmatch(r'[A-Za-z0-9._-]+', tag):
        raise ValueError('invalid GitHub release tag')
    if not re.fullmatch(rf'https://api\.github\.com/repos/{re.escape(repo)}/releases/[1-9][0-9]*', release.get('url', '')):
        raise ValueError('release lacks a canonical GitHub release ID')
    matches = [a for a in release.get('assets', []) if a.get('name') == name]
    if len(matches) != 1:
        raise ValueError(f'expected exactly one release asset: {name}')
    asset = matches[0]
    if asset.get('browser_download_url') != f'https://github.com/{repo}/releases/download/{tag}/{name}':
        raise ValueError('release asset URL does not match release')
    return asset


def verified_bytes(asset, expected=None):
    digest = expected or asset.get('digest', '').removeprefix('sha256:')
    if not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('release asset has no SHA-256 digest')
    data = fetch(asset['browser_download_url'])
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError('release asset SHA-256 mismatch')
    return data


def unpack(data, directory):
    # Allow only contained symlinks: code-server ships internal Node module links.
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        members = archive.getmembers()
        for member in members:
            target = (directory / member.name).resolve()
            if not target.is_relative_to(directory.resolve()) or not (member.isfile() or member.isdir() or member.issym()):
                raise ValueError('unsafe release archive member')
            if member.issym() and not (target.parent / member.linkname).resolve().is_relative_to(directory.resolve()):
                raise ValueError('release archive symlink escapes installation')
        # Python's data filter also checks traversal through previously extracted links.
        archive.extractall(directory, members=members, filter='data')


def install(app, state):
    destination = state / 'bin' / app
    binary = destination / ('bin/code-server' if app == 'code-server' else app)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (destination.parent / f'.{app}.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if binary.is_file() and os.access(binary, os.X_OK):
            return binary
        if destination.exists():
            raise ValueError(f'incomplete installation: inspect and remove {destination}')
        if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'AMD64'):
            raise ValueError('binary recipes require x86_64 Linux; site needs only Python 3')
        repo = REPOS[app]
        release = json.loads(fetch(f'https://api.github.com/repos/{repo}/releases/latest'))
        tag = release.get('tag_name', '')
        if app == 'dufs':
            name = f'dufs-{tag}-x86_64-unknown-linux-musl.tar.gz'
        elif app == 'code-server':
            if not tag.startswith('v'):
                raise ValueError('invalid code-server version')
            name = f'code-server-{tag[1:]}-linux-amd64.tar.gz'
        else:
            name = 'ttyd.x86_64'
        asset = release_asset(release, repo, name)
        expected = None
        if app == 'ttyd':
            sums = fetch(release_asset(release, repo, 'SHA256SUMS')['browser_download_url']).decode()
            matches = [line.split()[0] for line in sums.splitlines()
                       if len(line.split()) == 2 and line.split()[1].lstrip('*') == name]
            if len(matches) != 1:
                raise ValueError('ttyd SHA256SUMS missing or ambiguous')
            expected = matches[0]
        data = verified_bytes(asset, expected)
        with tempfile.TemporaryDirectory(prefix=f'.{app}-', dir=destination.parent) as temporary:
            stage = Path(temporary)
            if app == 'ttyd':
                (stage / app).write_bytes(data)
                source = stage
            else:
                unpack(data, stage)
                source = stage / f'code-server-{tag[1:]}-linux-amd64' if app == 'code-server' else stage
            executable = source / ('bin/code-server' if app == 'code-server' else app)
            if not executable.is_file():
                raise ValueError('release archive lacks its executable')
            executable.chmod(0o755)
            (source / 'release.json').write_text(json.dumps({'tag': tag, 'asset': name, 'sha256': hashlib.sha256(data).hexdigest()}))
            source.rename(destination)
    return binary


def root_path(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('root must be an absolute path')
    path = path.resolve(strict=True)
    if not path.is_dir() or not path.is_relative_to(WORKSPACE.resolve()):
        raise ValueError(f'root must be an existing directory within {WORKSPACE}')
    return path


def command(app, root, port, state):
    if app == 'site':
        # Our handler rejects symlinks resolving outside the declared site root.
        return [sys.executable, str(Path(__file__).resolve()), '_serve', str(root), str(port)]
    binary = str(install(app, state))
    if app == 'dufs':
        return [binary, str(root), '--bind', '0.0.0.0', '--port', str(port),
                '--allow-upload', '--allow-delete', '--allow-search', '--allow-archive', '--allow-hash']
    if app == 'ttyd':
        return [binary, '-W', '-i', '0.0.0.0', '-p', str(port), '-w', str(root), '/bin/bash']
    return [binary, '--bind-addr', f'0.0.0.0:{port}', '--auth', 'none', str(root)]


def listener(port):
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def identity(pid):
    """Linux start ticks prevent PID reuse from being mistaken for our process."""
    if not Path('/proc').exists():
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True, text=True)
        return result.stdout.strip() or None
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except FileNotFoundError:
        return None


def process_command(pid):
    if Path('/proc').exists():
        return Path(f'/proc/{pid}/cmdline').read_bytes().hex()
    return subprocess.check_output(['ps', '-p', str(pid), '-o', 'command='], text=True).strip()


def ready(port):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=0.5)
    try:
        connection.request('GET', '/')
        return connection.getresponse().status < 500
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def ensure(app, root, port, state, timeout):
    directory = state / 'services' / str(port)
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / 'log'
    record = directory / 'process.json'
    with (directory / '.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = json.loads(record.read_text()) if record.exists() else None
        alive = old and old.get('start') is not None and identity(old['pid']) == old['start']
        if alive:
            if old['app'] != app or old['root'] != str(root):
                raise ValueError('port belongs to another recipe/root; choose another port or stop its process explicitly')
            if process_command(old['pid']) != old.get('observed_command'):
                raise ValueError('recorded service command changed; inspect it manually')
            pid, status = old['pid'], 'reused'
        else:
            if listener(port):
                raise ValueError('port occupied by an untracked process; refusing reuse')
            argv = command(app, root, port, state)
            with log.open('ab') as output:
                process = subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            pid, status = process.pid, 'started'
            record.write_text(json.dumps({'app': app, 'root': str(root), 'pid': pid,
                                          'start': identity(pid), 'command': argv}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if status == 'started' and process.poll() is not None:
                raise ValueError(f'service exited; inspect {log}')
            if ready(port):
                if status == 'started':
                    metadata = json.loads(record.read_text())
                    metadata['observed_command'] = process_command(pid)
                    record.write_text(json.dumps(metadata))
                return {'service': app, 'port': port, 'root': str(root), 'log': str(log),
                        'pid': pid, 'status': status, 'expose': f'http:{port}',
                        'access': 'private', 'ready': True}
            time.sleep(0.2)
        # Do not leave a slow unauthenticated terminal/IDE running after a failed call.
        if status == 'started' and process.poll() is None:
            os.killpg(pid, signal.SIGTERM)
        raise ValueError(f'service readiness timeout; inspect {log}')


def serve(root, port):
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    class ContainedFiles(SimpleHTTPRequestHandler):
        def send_head(self):
            translated = Path(self.translate_path(self.path))
            candidates = [translated]
            if translated.is_dir():
                candidates += [translated / name for name in ('index.html', 'index.htm')]
            if any(not path.resolve().is_relative_to(Path(root).resolve()) for path in candidates):
                self.send_error(403, 'Outside-root symlink denied')
                return None
            return super().send_head()

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=root, **kwargs)

    ThreadingHTTPServer(('0.0.0.0', int(port)), ContainedFiles).serve_forever()


def main():
    if len(sys.argv) == 4 and sys.argv[1] == '_serve':
        serve(sys.argv[2], sys.argv[3])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', choices=PORTS)
    parser.add_argument('--root', default=str(WORKSPACE))
    parser.add_argument('--port', type=int)
    parser.add_argument('--state', type=Path, default=Path('/tmp/lathe-apps'))
    parser.add_argument('--timeout', type=float, default=20)
    args = parser.parse_args()
    try:
        root = root_path(args.root)
        port = args.port if args.port is not None else PORTS[args.app]
        if not 3000 <= port <= 9999:
            raise ValueError('port must be between 3000 and 9999')
        if not args.state.is_absolute():
            raise ValueError('state must be an absolute path')
        if not 0 < args.timeout <= 120:
            raise ValueError('timeout must be positive and at most 120 seconds')
        print(json.dumps(ensure(args.app, root, port, args.state, args.timeout)), flush=True)
    except (OSError, ValueError, tarfile.TarError) as error:
        parser.exit(1, f'recipe: {error}\n')


if __name__ == '__main__':
    main()
