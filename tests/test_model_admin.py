from __future__ import annotations
import json
import threading
import urllib.error
import urllib.request
import pytest
from visual_agent.pacer_model import PacerModelServer
from visual_agent.model_admin import management_token


@pytest.fixture
def admin(tmp_path):
    config={"workspace_root":str(tmp_path),"auth_token":"provider-secret","allowed_roots":[str(tmp_path)]}
    server=PacerModelServer(("127.0.0.1",0),config)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield f"http://127.0.0.1:{server.server_port}",management_token(config)
    server.shutdown();server.server_close();thread.join(timeout=3)


def call(url,token,path,body=None):
    return urllib.request.urlopen(urllib.request.Request(url+path,data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"}),timeout=5)


def test_management_token_is_separate_from_provider_auth(admin):
    url,token=admin
    assert token!="provider-secret"
    with call(url,token,"/api/model-config") as response:
        assert "provider-secret" not in response.read().decode()
    with pytest.raises(urllib.error.HTTPError) as error:
        call(url,token,"/v1/messages",{"model":"pacer","messages":[]})
    assert error.value.code==401


def test_save_profiles_does_not_echo_key(admin):
    url,token=admin
    with call(url,token,"/api/model-config",{"workflow":"gemini4","providers":{"gemini4":{"api_key":"hidden-secret"}}}) as response:
        text=response.read().decode()
    assert "hidden-secret" not in text
    assert json.loads(text)["profiles"]["workflow"]=="gemini4"


def test_bundled_extension_is_v2_and_has_no_embedded_password(admin):
    import io,zipfile
    url,token=admin
    with call(url,token,"/api/extension") as response: data=response.read()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert json.loads(archive.read('manifest.json'))['version']=='2.0.3'
        assert 'sk-gemini4-vps-direct' not in archive.read('popup.html').decode()
