#!/bin/sh
# Run with sh; Python 3 is the only prerequisite, including for HTTPS downloads.
exec python3 - "$@" <<'PY'
import argparse
import fcntl
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import urllib.request


def tree_hash(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('sandbox support snapshots cannot contain symlinks')
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            digest.update(relative.encode() + b'\0' + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


parser = argparse.ArgumentParser(description='Install an exact Lathe sandbox support snapshot, never main/latest.')
parser.add_argument('revision', nargs='?')
parser.add_argument('--sha256', help='Optional sandbox source-tree SHA-256, not an archive hash')
parser.add_argument('--seed', type=Path, help='Exact local sandbox directory, for unpublished trials')
parser.add_argument('--hash', type=Path, help='Print the reproducible source-tree SHA-256')
args = parser.parse_args()
try:
    if args.hash:
        print(tree_hash(args.hash.resolve(strict=True)))
        raise SystemExit(0)
    if not re.fullmatch('[0-9a-f]{40}', args.revision or ''):
        parser.error('revision must be a full 40-character Git commit, not a branch or latest')
    if args.sha256 and not re.fullmatch('[0-9a-f]{64}', args.sha256):
        parser.error('sha256 must be the exact sandbox source-tree digest')
    base = Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache'))) / 'lathe'
    destination = base / args.revision
    destination.parent.mkdir(parents=True, exist_ok=True)
    receipt = base / f'.{args.revision}.sha256'
    with (base / f'.{args.revision}.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.exists():
            actual = tree_hash(destination)
            if not receipt.exists() or actual != receipt.read_text().strip() or (args.sha256 and actual != args.sha256):
                raise ValueError('cached sandbox support snapshot changed; remove it explicitly before retrying')
        else:
            with tempfile.TemporaryDirectory(prefix='.sandbox-', dir=destination.parent) as temporary:
                staged = Path(temporary) / 'sandbox'
                if args.seed:
                    source = args.seed.resolve(strict=True)
                    if args.sha256 and tree_hash(source) != args.sha256:
                        raise ValueError('local sandbox source digest mismatch')
                    shutil.copytree(source, staged, symlinks=True)
                else:
                    url = f'https://api.github.com/repos/rndmcnlly/lathe/tarball/{args.revision}'
                    request = urllib.request.Request(url, headers={'User-Agent': 'lathe-sandbox'})
                    with urllib.request.urlopen(request, timeout=120) as response:
                        archive = response.read()
                    staged.mkdir()
                    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
                        for member in tar:
                            parts = member.name.split('/')
                            if len(parts) < 3 or parts[1] != 'sandbox':
                                continue
                            relative = Path(*parts[2:])
                            if relative.is_absolute() or '..' in relative.parts:
                                raise ValueError('unsafe sandbox archive path')
                            target = staged / relative
                            if member.isdir():
                                target.mkdir(parents=True, exist_ok=True)
                            elif member.isfile():
                                target.parent.mkdir(parents=True, exist_ok=True)
                                target.write_bytes(tar.extractfile(member).read())
                            else:
                                raise ValueError('sandbox archive contains a link or special file')
                actual = tree_hash(staged)
                if args.sha256 and actual != args.sha256:
                    raise ValueError('sandbox source digest mismatch')
                required = ['README.md', 'scripts/apps.py', 'bootstrap.sh', 'skills/dufs/SKILL.md',
                            'skills/ttyd/SKILL.md', 'skills/code-server/SKILL.md', 'skills/site/SKILL.md']
                if not all((staged / name).is_file() for name in required):
                    raise ValueError('sandbox snapshot lacks expected app/bootstrap/skill layout')
                staged.rename(destination)
                receipt.write_text(actual + '\n')
    print(destination)
except (OSError, ValueError, tarfile.TarError) as error:
    parser.exit(1, f'sandbox bootstrap: {error}\n')
PY
