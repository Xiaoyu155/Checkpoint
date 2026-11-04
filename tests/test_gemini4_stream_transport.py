from __future__ import annotations

import io
import json
from unittest.mock import patch
import urllib.error

from visual_agent.gemini4_vps_client import invoke_gemini4_vps


class Response(io.BytesIO):
    def __init__(self, payload, content_type='text/event-stream'):
        super().__init__(payload)
        self.headers = {'Content-Type': content_type}

    def getcode(self):
        return 200


def events(*data):
    return ''.join('data: ' + (item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)) + '\n\n'
                   for item in data).encode('utf-8')


def settings():
    return {'enabled': True, 'model': 'gemini-4', 'base_url': 'https://gateway.example/gemini4',
            'api_key': 'private-test-key', 'allow_ssh_fallback': False}


def test_stream_collects_utf8_and_requires_real_completion():
    body = events({'model': 'gemini-4', 'choices': [{'delta': {'content': '中文'}}]},
                  {'choices': [{'delta': {'content': ' OK'}, 'finish_reason': 'stop'}], 'usage': {'total_tokens': 73}}, '[DONE]')
    with patch('visual_agent.gemini4_vps_client.gemini4_settings', return_value=settings()), \
         patch('visual_agent.gemini4_vps_client.urllib.request.urlopen', return_value=Response(body)) as call:
        result = invoke_gemini4_vps('test', timeout_seconds=5)
    assert json.loads(call.call_args.args[0].data)['stream'] is True
    assert result.success and result.content == '中文 OK'
    assert result.tokens_used == 73


def test_broken_stream_does_not_accept_partial_code():
    body = events({'choices': [{'delta': {'content': 'partial code'}}]})
    with patch('visual_agent.gemini4_vps_client.gemini4_settings', return_value=settings()), \
         patch('visual_agent.gemini4_vps_client.urllib.request.urlopen', return_value=Response(body)):
        result = invoke_gemini4_vps('test', timeout_seconds=5)
    assert not result.success
    assert 'stream' in result.error.lower()


def test_upstream_error_after_content_is_not_success():
    body = events({'choices': [{'delta': {'content': 'partial'}}]}, {'error': {'message': 'Pool exhausted'}})
    with patch('visual_agent.gemini4_vps_client.gemini4_settings', return_value=settings()), \
         patch('visual_agent.gemini4_vps_client.urllib.request.urlopen', return_value=Response(body)):
        result = invoke_gemini4_vps('test', timeout_seconds=5)
    assert not result.success and 'Pool exhausted' in result.error


def test_timeout_is_reported_with_its_cause():
    with patch('visual_agent.gemini4_vps_client.gemini4_settings', return_value=settings()), \
         patch('visual_agent.gemini4_vps_client.urllib.request.urlopen', side_effect=TimeoutError('timed out')):
        result = invoke_gemini4_vps('test', timeout_seconds=5)
    assert not result.success and 'timed out' in result.error.lower()
    assert 'private-test-key' not in result.error


def test_http_errors_keep_the_upstream_status_and_detail():
    error = urllib.error.HTTPError('https://gateway.example', 503, 'Unavailable', {}, io.BytesIO(b'{"detail":"Pool exhausted"}'))
    with patch('visual_agent.gemini4_vps_client.gemini4_settings', return_value=settings()), \
         patch('visual_agent.gemini4_vps_client.urllib.request.urlopen', side_effect=error):
        result = invoke_gemini4_vps('test', timeout_seconds=5)
    assert not result.success and '503' in result.error and 'Pool exhausted' in result.error
