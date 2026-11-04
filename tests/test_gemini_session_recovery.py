import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vps_gemini4_gateway'))
import gemini4_service as gateway


def account():
    return gateway.Account('test', 'test@example.invalid', 'test-cookie', 'test-ts')


def landing_page(monkeypatch, transport, html, status=200):
    @asynccontextmanager
    async def context(value):
        yield value

    async def text():
        return html

    response = SimpleNamespace(status=status, status_code=status, url='https://gemini.google.com/app',
                               text=html if transport == 'curl' else text)
    get = AsyncMock(return_value=response) if transport == 'curl' else Mock(return_value=context(response))
    client = SimpleNamespace(get=get)
    monkeypatch.setattr(gateway, 'HAS_CURL_CFFI', transport == 'curl')
    monkeypatch.setattr(gateway, 'CurlCookies', lambda: SimpleNamespace(set=lambda *a, **k: None))
    monkeypatch.setattr(gateway, 'CurlAsyncSession', lambda **kwargs: context(client))
    return client, get


@pytest.mark.parametrize('transport', ['curl', 'aiohttp'])
def test_missing_token_is_unverified_and_preserves_subscription(monkeypatch, transport):
    client, _ = landing_page(monkeypatch, transport, '<html>New page layout</html>')
    acc = account()
    previous = acc.tier, acc.is_pro, acc.plan
    assert asyncio.run(gateway.fetch_snlm0e(client, acc)) is None
    assert acc.get_status() == 'session_unverified'
    assert acc.is_available() is False
    assert (acc.tier, acc.is_pro, acc.plan) == previous


@pytest.mark.parametrize('transport', ['curl', 'aiohttp'])
def test_valid_token_with_json_whitespace_recovers_account(monkeypatch, transport):
    html = '"cfb2h": "build", "FdrFJe": "sid", "SNlM0e" : "fresh-token"'
    client, get = landing_page(monkeypatch, transport, html)
    acc = account()
    # A stale cached ticket must never resurrect an explicitly failed session.
    acc.snlm0e, acc.build_label, acc.session_id = 'old-ticket', 'build', 'sid'
    acc.last_snlm_time = time.time()
    acc.mark_expired('previous explicit login failure')
    assert asyncio.run(gateway.fetch_snlm0e(client, acc)) == 'fresh-token'
    assert get.call_count == 1
    assert acc.get_status() == 'active'


@pytest.mark.parametrize('transport', ['curl', 'aiohttp'])
def test_empty_token_cannot_make_account_available(monkeypatch, transport):
    client, _ = landing_page(monkeypatch, transport, '"SNlM0e":""')
    acc = account()
    assert not asyncio.run(gateway.fetch_snlm0e(client, acc))
    assert not acc.is_available()


def test_failed_session_probe_cannot_relabel_paid_account_as_free(monkeypatch):
    monkeypatch.setattr(gateway, 'fetch_snlm0e', AsyncMock(return_value=None))
    discovery = AsyncMock()
    monkeypatch.setattr(gateway, 'query_otaq7b_async', discovery)
    acc = account()
    previous = acc.tier, acc.is_pro, acc.plan
    result = asyncio.run(gateway.detect_account_tier_async(object(), acc))
    assert (acc.tier, acc.is_pro, acc.plan) == previous
    assert result == (acc.email, acc.tier, acc.plan)
    discovery.assert_not_awaited()


def test_refreshing_login_does_not_clear_model_cooldown_diagnostics(monkeypatch):
    client, _ = landing_page(monkeypatch, 'curl', '"SNlM0e":"valid-token"')
    acc = account()
    acc.mark_cooling_down(300, 'Google silently downgraded the requested model')
    old_reason, old_error = acc.status_reason, acc.last_error
    assert asyncio.run(gateway.fetch_snlm0e(client, acc)) == 'valid-token'
    assert acc.get_status() == 'cooling_down'
    assert (acc.status_reason, acc.last_error) == (old_reason, old_error)


def test_recovery_rechecks_failed_sessions_without_model_generation(monkeypatch):
    expired, unverified, disabled, healthy = [account() for _ in range(4)]
    expired.mark_expired('login required')
    unverified.mark_session_unverified('page format unknown')
    disabled.is_disabled = True
    monkeypatch.setattr(gateway, 'pool', SimpleNamespace(accounts=[expired, unverified, disabled, healthy]))
    @asynccontextmanager
    async def session():
        yield object()
    fetch = AsyncMock(return_value='fresh-ticket')
    generation = Mock(side_effect=AssertionError('recovery must not generate model output'))
    monkeypatch.setattr(gateway, 'get_aiohttp_session', session)
    monkeypatch.setattr(gateway, 'fetch_snlm0e', fetch)
    monkeypatch.setattr(gateway, 'call_gemini_web', generation)
    asyncio.run(gateway.refresh_pool_sessions())
    assert [call.args[1] for call in fetch.await_args_list] == [expired, unverified, healthy]
    generation.assert_not_called()


@pytest.mark.parametrize('changed', [True, False])
def test_cookie_rotation_reads_live_session_jar_and_requires_a_real_change(monkeypatch, changed):
    class Cookies:
        def __init__(self):
            self.jar=[]
        def set(self,name,value,**kwargs):
            self.jar.append(SimpleNamespace(name=name,value=value))
    live=Cookies()
    live.set('__Secure-1PSID','must-not-replace-the-account')
    live.set('__Secure-1PSIDTS','fresh-ts' if changed else 'test-ts')
    live.set('__Secure-1PSIDCC','fresh-cc' if changed else 'old-cc')
    @asynccontextmanager
    async def session(**kwargs):
        # A distinct jar, matching the installed curl_cffi library's behavior.
        assert kwargs['cookies'] is not live
        yield SimpleNamespace(cookies=live,post=AsyncMock(return_value=SimpleNamespace(status_code=200)))
    monkeypatch.setattr(gateway,'CurlCookies',Cookies)
    monkeypatch.setattr(gateway,'CurlAsyncSession',session)
    acc=account()
    acc.psidcc='old-cc'
    assert asyncio.run(gateway.rotate_account_cookies(acc)) is changed
    assert acc.psid=='test-cookie'
    assert acc.psidts==('fresh-ts' if changed else 'test-ts')
    assert acc.psidcc==('fresh-cc' if changed else 'old-cc')


def test_metadata_transport_failure_cannot_overwrite_verified_entitlement(monkeypatch):
    monkeypatch.setattr(gateway,'fetch_snlm0e',AsyncMock(return_value='ticket'))
    monkeypatch.setattr(gateway,'query_otaq7b_async',AsyncMock(return_value={'discovery_succeeded':False,'models':{}}))
    acc=account()
    acc.models={'argon':{'id':'verified-model'}}
    previous=acc.tier,acc.is_pro,acc.capacity,acc.plan,dict(acc.models)
    asyncio.run(gateway.detect_account_tier_async(object(),acc))
    assert (acc.tier,acc.is_pro,acc.capacity,acc.plan,acc.models)==previous


@pytest.mark.parametrize('model', ['', '3.6 Flash'])
def test_gemini4_probe_cannot_reuse_old_model_identity_or_fall_back_to_flash(monkeypatch,model):
    acc=account()
    acc.verified_model_name='4 Argon'
    @asynccontextmanager
    async def session():yield object()
    monkeypatch.setattr(gateway,'get_aiohttp_session',session)
    monkeypatch.setattr(gateway,'detect_account_tier_async',AsyncMock())
    async def generate(target,prompt,requested_model):
        assert target.verified_model_name==''
        assert requested_model=='gemini-4-argon'
        target.verified_model_name=model
        yield 'Pong'
    monkeypatch.setattr(gateway,'call_gemini_web',generate)
    valid,detail,_=asyncio.run(gateway.test_account_ping(acc))
    assert valid is False
    assert 'not verified' in detail


@pytest.mark.parametrize('model', [None,'3.1 Pro','4 Argon'])
def test_stream_must_observe_the_requested_flagship_identity(monkeypatch,model):
    inner=[None]*43
    inner[4]=[['candidate',['Pong']]]
    inner[42]=model
    import json
    async def lines():
        yield json.dumps([['wrb.fr','rpc',json.dumps(inner)]]).encode()
    response=SimpleNamespace(status_code=200,aiter_lines=lines)
    @asynccontextmanager
    async def session(**kwargs):
        yield SimpleNamespace(post=AsyncMock(return_value=response))
    monkeypatch.setattr(gateway,'HAS_CURL_CFFI',True)
    monkeypatch.setattr(gateway,'CurlCookies',lambda:SimpleNamespace(set=lambda *a,**k:None))
    monkeypatch.setattr(gateway,'CurlAsyncSession',session)
    monkeypatch.setattr(gateway,'fetch_snlm0e',AsyncMock(return_value='ticket'))
    acc=account()
    async def collect():
        return [text async for text in gateway.call_gemini_web(acc,'ping',requested_model='gemini-4')]
    if model=='4 Argon':
        assert asyncio.run(collect())==['Pong']
        assert acc.success_requests==1
    else:
        with pytest.raises(RuntimeError,match='identity was not observed'):
            asyncio.run(collect())
        assert acc.success_requests==0
