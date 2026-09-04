"""bot.mdns_advertise's start()/stop() — the mDNS/DNS-SD advertisement
Android's NsdDiscoveryClient looks for. Fakes zeroconf.Zeroconf/ServiceInfo
directly (never touches a real multicast socket) and bot.network_info's
detection, since both already have their own dedicated coverage — this
only needs to prove start()/stop() wire them together correctly and never
raise, matching the module's own "best-effort, always degrade gracefully"
contract.
"""

from __future__ import annotations

from bot import mdns_advertise


def _reset():
    mdns_advertise._zeroconf = None
    mdns_advertise._service_info = None


def _fake_addresses(monkeypatch, lan=None, tailscale=None):
    from bot import network_info

    monkeypatch.setattr(network_info, "detect_addresses", lambda: {"lan": lan, "tailscale": tailscale})


class _FakeServiceInfo:
    def __init__(self, service_type, name, addresses, port):
        self.service_type = service_type
        self.name = name
        self.addresses = addresses
        self.port = port


class _FakeZeroconf:
    instances: list["_FakeZeroconf"] = []

    def __init__(self):
        self.registered = []
        self.unregistered = []
        self.closed = False
        _FakeZeroconf.instances.append(self)

    def register_service(self, info):
        self.registered.append(info)

    def unregister_service(self, info):
        self.unregistered.append(info)

    def close(self):
        self.closed = True


def _patch_zeroconf(monkeypatch):
    import zeroconf

    _FakeZeroconf.instances = []
    monkeypatch.setattr(zeroconf, "Zeroconf", _FakeZeroconf)
    monkeypatch.setattr(zeroconf, "ServiceInfo", _FakeServiceInfo)


def test_start_registers_service_with_detected_lan_address(monkeypatch):
    _reset()
    _fake_addresses(monkeypatch, lan="192.168.1.50", tailscale="100.101.98.77")
    _patch_zeroconf(monkeypatch)

    mdns_advertise.start(port=9999)

    assert len(_FakeZeroconf.instances) == 1
    zc = _FakeZeroconf.instances[0]
    assert len(zc.registered) == 1
    assert zc.registered[0].port == 9999
    mdns_advertise.stop()


def test_start_skips_when_no_lan_address_detected(monkeypatch):
    _reset()
    _fake_addresses(monkeypatch, lan=None, tailscale="100.101.98.77")
    _patch_zeroconf(monkeypatch)

    mdns_advertise.start(port=9999)

    assert _FakeZeroconf.instances == []


def test_start_is_a_noop_when_already_running(monkeypatch):
    _reset()
    _fake_addresses(monkeypatch, lan="192.168.1.50")
    _patch_zeroconf(monkeypatch)

    mdns_advertise.start(port=9999)
    mdns_advertise.start(port=9999)

    assert len(_FakeZeroconf.instances) == 1
    mdns_advertise.stop()


def test_stop_unregisters_and_closes(monkeypatch):
    _reset()
    _fake_addresses(monkeypatch, lan="192.168.1.50")
    _patch_zeroconf(monkeypatch)
    mdns_advertise.start(port=9999)
    zc = _FakeZeroconf.instances[0]

    mdns_advertise.stop()

    assert zc.unregistered == zc.registered
    assert zc.closed is True


def test_stop_before_start_does_nothing(monkeypatch):
    _reset()
    mdns_advertise.stop()  # must not raise


def test_start_never_raises_when_zeroconf_explodes(monkeypatch):
    _reset()
    _fake_addresses(monkeypatch, lan="192.168.1.50")

    import zeroconf

    def _boom():
        raise OSError("no multicast support")

    monkeypatch.setattr(zeroconf, "Zeroconf", _boom)

    mdns_advertise.start(port=9999)  # must not raise

    assert mdns_advertise._zeroconf is None


def test_stop_never_raises_when_unregister_fails(monkeypatch):
    _reset()
    _fake_addresses(monkeypatch, lan="192.168.1.50")
    _patch_zeroconf(monkeypatch)
    mdns_advertise.start(port=9999)
    zc = _FakeZeroconf.instances[0]

    def _boom(info):
        raise OSError("socket already closed")

    monkeypatch.setattr(zc, "unregister_service", _boom)

    mdns_advertise.stop()  # must not raise
