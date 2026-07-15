from appliance_admin.models import IPCRequest, WifiConnectRequest


def test_request_has_id():
    request = IPCRequest(action="network.status")
    assert request.id


def test_wifi_password_validation():
    request = WifiConnectRequest(ssid="ShopWiFi", password="password123")
    assert request.ssid == "ShopWiFi"
