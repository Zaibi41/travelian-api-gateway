"""Run actual deployment scripts in temporary directories with mocked OS commands."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
OLD = 'a' * 40
NEW = 'b' * 40 + '-123-1'


@pytest.fixture
def deployment(tmp_path):
    app = tmp_path / 'app'
    (app / 'shared').mkdir(parents=True)
    (app / 'shared/.env').touch()
    (app / 'releases').mkdir()
    commands = tmp_path / 'bin'
    commands.mkdir()
    scripts = {}
    for name in ('deploy', 'rollback'):
        target = tmp_path / f'{name}.sh'
        target.write_text((ROOT / 'deploy' / f'{name}.sh').read_text().replace('/opt/travelian-api', str(app)))
        scripts[name] = target
    mocks = {
        'sudo': 'echo "$*" >> "$APP_TEST/commands"\n[[ "${FAIL_STOP:-}" != 1 || "$*" != *stop* ]]',
        'curl': '[[ "$(readlink "$APP_TEST/current")" != *"${UNHEALTHY:-no-match}"* ]] && printf \'{"status":"ok"}\'',
        'sleep': 'exit 0',
    }
    for name, body in mocks.items():
        path = commands / name
        path.write_text('#!/bin/bash\n' + body + '\n')
        path.chmod(0o755)
    env = {**os.environ, 'PATH': f'{commands}:{os.environ["PATH"]}', 'APP_TEST': str(app)}

    def release(name, healthy=False):
        path = app / 'releases' / name
        (path / '.venv/bin').mkdir(parents=True)
        (path / 'app').mkdir()
        (path / 'app/main.py').touch()
        (path / 'requirements.txt').touch()
        (path / 'requirements.lock').touch()
        python = path / '.venv/bin/python'
        python.write_text('#!/bin/bash\n[[ "${FAIL_INSTALL:-}" != 1 || "$*" != *"pip install"* ]]\n')
        python.chmod(0o755)
        if healthy:
            (path / '.deployment-success').touch()
        return path

    def run(name, target=NEW, **extra):
        return subprocess.run(['bash', str(scripts[name]), target], env={**env, **extra}, capture_output=True, text=True)

    return app, release, run


def test_first_deployment_and_active_release_protection(deployment):
    app, release, run = deployment
    target = release(NEW)
    assert run('deploy').returncode == 0
    assert (app / 'current').resolve() == target
    assert (target / '.deployment-success').is_file()
    assert run('deploy').returncode != 0


def test_failed_first_deployment_and_retry(deployment):
    app, release, run = deployment
    release(NEW)
    assert run('deploy', UNHEALTHY=NEW).returncode != 0
    assert not (app / 'current').is_symlink()
    assert 'stop' in (app / 'commands').read_text()
    retry = 'b' * 40 + '-123-2'
    target = release(retry)
    assert run('deploy', retry).returncode == 0
    assert (app / 'current').resolve() == target


def test_failed_deployment_restores_previous_release(deployment):
    app, release, run = deployment
    previous = release(OLD, healthy=True)
    (app / 'current').symlink_to(previous)
    release(NEW)
    result = run('deploy', UNHEALTHY=NEW)
    assert result.returncode != 0
    assert 'Rollback successful' in result.stdout
    assert (app / 'current').resolve() == previous


def test_dependency_failure_never_activates(deployment):
    app, release, run = deployment
    previous = release(OLD, healthy=True)
    (app / 'current').symlink_to(previous)
    release(NEW)
    assert run('deploy', FAIL_INSTALL='1').returncode != 0
    assert (app / 'current').resolve() == previous
    assert not (app / 'commands').exists()


def test_manual_rollback_and_failed_rollback_restoration(deployment):
    app, release, run = deployment
    previous = release(OLD, healthy=True)
    current = release(NEW, healthy=True)
    (app / 'current').symlink_to(current)
    result = run('rollback', OLD, UNHEALTHY=OLD)
    assert result.returncode != 0
    assert (app / 'current').resolve() == current
    assert run('rollback', OLD).returncode == 0
    assert (app / 'current').resolve() == previous
    assert run('rollback', OLD, UNHEALTHY=OLD).returncode != 0


def test_incomplete_release_is_not_rollback_target(deployment):
    app, release, run = deployment
    release(OLD)
    (app / 'current').symlink_to(release(NEW, healthy=True))
    assert run('rollback', OLD).returncode != 0


def test_lock_excludes_both_scripts(deployment):
    import fcntl
    app, release, run = deployment
    release(OLD, healthy=True)
    release(NEW)
    with (app / 'shared/deployment.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for script in ('deploy', 'rollback'):
            result = run(script, OLD)
            assert result.returncode != 0
            assert 'Another deployment or rollback' in result.stdout


def test_stop_failure_is_reported(deployment):
    app, release, run = deployment
    release(NEW)
    result = run('deploy', UNHEALTHY=NEW, FAIL_STOP='1')
    assert result.returncode != 0
    assert 'CRITICAL: Failed to stop' in result.stdout


@pytest.mark.parametrize('name,kind,allowed', [
    ('app/main.py', 'file', True),
    ('../outside', 'file', False),
    ('/tmp/outside', 'file', False),
    ('link', 'symlink', False),
    ('device', 'device', False),
])
def test_workflow_archive_validation(tmp_path, name, kind, allowed):
    import io
    import sys
    import tarfile
    import textwrap
    workflow = (ROOT / '.github/workflows/deploy-production.yml').read_text()
    validator = workflow.split("python3.12 -c '\n", 1)[1].split("\n          ' \"${SHA}.tar.gz\"", 1)[0]
    archive = tmp_path / 'release.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        member = tarfile.TarInfo(name)
        if kind == 'symlink':
            member.type = tarfile.SYMTYPE
            member.linkname = '/tmp/outside'
        elif kind == 'device':
            member.type = tarfile.CHRTYPE
        tar.addfile(member, io.BytesIO(b''))
    result = subprocess.run([sys.executable, '-c', textwrap.dedent(validator), str(archive)], capture_output=True)
    assert (result.returncode == 0) is allowed


def test_checksum_rejects_corrupted_archive(tmp_path):
    import hashlib
    archive = tmp_path / 'release.tar.gz'
    archive.write_bytes(b'tested archive')
    checksum = tmp_path / 'release.tar.gz.sha256'
    checksum.write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + '  release.tar.gz\n')
    def verify():
        return subprocess.run(['sha256sum', '-c', checksum.name], cwd=tmp_path, capture_output=True).returncode
    assert verify() == 0
    archive.write_bytes(b'corrupted archive')
    assert verify() != 0


def test_missing_environment_file_never_activates(deployment):
    app, release, run = deployment
    release(NEW)
    (app / 'shared/.env').unlink()
    result = run('deploy')
    assert result.returncode != 0
    assert 'environment file is missing' in result.stdout
    assert not (app / 'current').is_symlink()
