from visual_agent.workbuddy_bridge import WorkBuddyGatewayClient, WorkBuddyGatewayInfo


def test_gateway_requests_include_csrf_marker_and_authentication():
    client = WorkBuddyGatewayClient(WorkBuddyGatewayInfo(port=61386, password='test-only'))
    headers = {key.lower(): value for key, value in client._headers().items()}
    assert headers.get('x-codebuddy-request'), 'WorkBuddy requires its request marker before checking authentication'
    assert headers['authorization'] == 'Bearer test-only'
    assert headers['content-type'] == 'application/json'


def test_request_marker_does_not_depend_on_credentials():
    client = WorkBuddyGatewayClient(WorkBuddyGatewayInfo(port=61386, password=''))
    headers = {key.lower(): value for key, value in client._headers().items()}
    assert headers.get('x-codebuddy-request')
    assert 'authorization' not in headers
