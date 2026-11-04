import json
from unittest.mock import MagicMock, patch

import pytest

from visual_agent.command_verification import is_test_path, snapshot_verification_files
from visual_agent.context_profile import ContextProfileManager
from visual_agent.workbuddy_bridge import WorkBuddyGatewayClient, WorkBuddyGatewayInfo


def test_tamper_backups_do_not_become_new_exam_files(tmp_path):
    import subprocess
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    original = tmp_path / 'tests/test_case.py'
    original.parent.mkdir()
    original.write_text('def test_case():\n    assert 1 + 1 == 2\n')
    before = snapshot_verification_files(repo_root=tmp_path)
    backup = tmp_path / '.pacer/tamper_guard/blobs/tests/test_case.py'
    backup.parent.mkdir(parents=True)
    backup.write_bytes(original.read_bytes())
    assert snapshot_verification_files(repo_root=tmp_path) == before
    assert is_test_path('tests/test_case.py')
    assert not is_test_path('.pacer/tamper_guard/blobs/tests/test_case.py')


def test_profile_refresh_preserves_saved_protocol_decisions(tmp_path):
    manager = ContextProfileManager(tmp_path)
    profile = manager.get_or_create_profile()
    profile.architecture_overview = 'Saved protocol contract'
    profile.confirmed_decisions = ['Send the mandatory gateway request marker']
    manager.save_profile(profile)
    refreshed = manager.get_or_create_profile(focus_files=['worker.py'])
    assert refreshed.architecture_overview == 'Saved protocol contract'
    assert refreshed.confirmed_decisions == profile.confirmed_decisions
    assert 'Saved protocol contract' in refreshed.to_compact_markdown()
    replacement = manager.get_or_create_profile(architecture_overview='Explicit replacement')
    assert replacement.architecture_overview == 'Explicit replacement'


@pytest.mark.parametrize('mode', ['authentication_error', 'truncated', 'disconnect', 'structured_error'])
def test_failed_worker_streams_cannot_materialize_code(tmp_path, mode):
    client = WorkBuddyGatewayClient(WorkBuddyGatewayInfo(port=61386, password='test-only'))
    post = MagicMock()
    post.__enter__.return_value = post
    post.read.return_value = b'{"runId":"failed-run"}'
    code = '```python path=app.py\ndef repaired():\n    return 42\n```'

    def lines():
        if mode == 'authentication_error':
            yield b'data: {"status":"completed","content":{"markdown":"AuthenticationRequiredError: sign in"}}\n'
        elif mode == 'structured_error':
            yield b'event: error\n'
            yield b'data: {"error":"quota exhausted"}\n'
            yield b'event: done\n'
        else:
            yield ('data: ' + json.dumps({'delta': code}) + '\n').encode()
            if mode == 'disconnect':
                raise ConnectionResetError('stream disconnected')

    stream = MagicMock()
    stream.__enter__.return_value = stream
    stream.__iter__.side_effect = lines
    with patch('urllib.request.urlopen', side_effect=[post, stream]):
        result = client.create_run('repair app', cwd=tmp_path, target_files=['app.py'], apply_changes=True)
    assert result.success is False
    assert result.error
    assert result.written_files == []
    assert not (tmp_path / 'app.py').exists()
