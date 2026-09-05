"""Pairing protocol v1: the format, and the rules about what may be encoded.

This module is the authoritative implementation of a cross-language contract -
Home Assistant produces pairing codes, the iOS app only consumes them - so what
is asserted here is what the Swift side has to accept.
"""

from __future__ import annotations

import base64
import json

import pytest

from custom_components.apple_health_sync.pairing import (
    PAIRING_VERSION,
    PairingError,
    PairingPayload,
    Reach,
    decode,
    encode,
    is_cleartext_allowed,
    is_usable_target,
)

URL = "https://hooks.nabu.casa/gAbCdEf"
TOKEN = "a-256-bit-token-in-spirit-if-not-in-length"


def payload_of(uri: str) -> dict:
    fragment = uri.split("#", 1)[1]
    return json.loads(base64.urlsafe_b64decode(fragment + "=" * (-len(fragment) % 4)))


# --- round trip --------------------------------------------------------------


def test_a_pairing_code_round_trips():
    result = decode(encode(URL, TOKEN, Reach.REMOTE))
    assert result == PairingPayload(
        url=URL, token=TOKEN, reach=Reach.REMOTE, version=PAIRING_VERSION
    )


@pytest.mark.parametrize("reach", list(Reach))
def test_every_reach_round_trips(reach):
    assert decode(encode(URL, TOKEN, reach)).reach is reach


def test_the_code_is_a_fragment_on_a_custom_scheme():
    """A fragment is not sent to a server if the code is pasted into a browser."""
    uri = encode(URL, TOKEN, Reach.REMOTE)
    assert uri.startswith("eigenpuls://pair#")
    assert "?" not in uri


def test_the_encoding_is_deterministic():
    """The Swift consumer is tested against a committed fixture of this output."""
    assert encode(URL, TOKEN, Reach.REMOTE) == encode(URL, TOKEN, Reach.REMOTE)


def test_the_payload_carries_the_version_and_nothing_unexpected():
    body = payload_of(encode(URL, TOKEN, Reach.REMOTE))
    assert body["v"] == PAIRING_VERSION
    # No device id, no account, no installation identifier. Each would need
    # justifying later and none is needed to POST to a URL with a token.
    assert set(body) == {"v", "url", "token", "reach"}


# --- what must be refused ----------------------------------------------------


def test_a_payload_without_a_version_is_rejected():
    body = {"url": URL, "token": TOKEN, "reach": "remote"}
    with pytest.raises(PairingError) as err:
        decode(_uri_for(body))
    assert err.value.reason == "missing_version"


def test_a_future_pairing_version_is_named_distinctly():
    """"Your app is too old" is actionable; "the code is broken" is not."""
    body = {"v": PAIRING_VERSION + 1, "url": URL, "token": TOKEN, "reach": "remote"}
    with pytest.raises(PairingError) as err:
        decode(_uri_for(body))
    assert err.value.reason == "unsupported_pairing_version"


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"v": 1, "token": TOKEN, "reach": "remote"}, "missing_url"),
        ({"v": 1, "url": URL, "reach": "remote"}, "missing_token"),
        ({"v": 1, "url": URL, "token": "", "reach": "remote"}, "missing_token"),
        ({"v": 1, "url": URL, "token": TOKEN}, "bad_reach"),
        ({"v": 1, "url": URL, "token": TOKEN, "reach": "somehow"}, "bad_reach"),
        ({"v": True, "url": URL, "token": TOKEN, "reach": "remote"}, "missing_version"),
    ],
)
def test_incomplete_payloads_are_rejected(body, reason):
    with pytest.raises(PairingError) as err:
        decode(_uri_for(body))
    assert err.value.reason == reason


@pytest.mark.parametrize(
    ("uri", "reason"),
    [
        ("https://pair#abc", "bad_scheme"),
        ("eigenpuls://something#abc", "bad_scheme"),
        ("eigenpuls://pair", "missing_payload"),
        ("eigenpuls://pair#not-base64-at-all!!", "malformed_payload"),
        ("eigenpuls://pair#" + base64.urlsafe_b64encode(b'"a string"').decode(),
         "malformed_payload"),
    ],
)
def test_malformed_codes_are_rejected(uri, reason):
    with pytest.raises(PairingError) as err:
        decode(uri)
    assert err.value.reason == reason


def test_encoding_refuses_a_target_the_phone_could_not_use():
    with pytest.raises(PairingError) as err:
        encode("http://example.com/api/webhook/x", TOKEN, Reach.REMOTE)
    assert err.value.reason == "unusable_url"


def test_encoding_refuses_an_empty_token():
    with pytest.raises(PairingError) as err:
        encode(URL, "", Reach.REMOTE)
    assert err.value.reason == "missing_token"


# --- the cleartext policy ----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.10:8123/api/webhook/x",
        "http://10.0.0.4:8123/api/webhook/x",
        "http://172.16.5.5:8123/api/webhook/x",
        "http://homeassistant.local:8123/api/webhook/x",
        "http://[fd00::1]:8123/api/webhook/x",
    ],
)
def test_cleartext_is_allowed_on_a_home_network(url):
    assert is_cleartext_allowed(url)
    assert is_usable_target(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/api/webhook/x",
        "http://93.184.216.34/api/webhook/x",
        # Reachable by Home Assistant, never by the phone.
        "http://127.0.0.1:8123/api/webhook/x",
        "http://localhost:8123/api/webhook/x",
        # No DHCP is not a home network, and this is the address family that
        # contaminated the P3 routing measurement.
        "http://169.254.10.1:8123/api/webhook/x",
        "http://[fe80::1]:8123/api/webhook/x",
        # A name this side cannot resolve the way the phone would.
        "http://my-ha-box:8123/api/webhook/x",
    ],
)
def test_cleartext_is_refused_everywhere_else(url):
    assert not is_cleartext_allowed(url)
    assert not is_usable_target(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/api/webhook/x",
        "https://192.168.1.10:8123/api/webhook/x",
        "https://hooks.nabu.casa/gAbCdEf",
    ],
)
def test_https_is_accepted_wherever_it_points(url):
    """The certificate is the phone's to validate, and it refuses self-signed."""
    assert is_usable_target(url)


@pytest.mark.parametrize(
    "url", ["ftp://192.168.1.10/x", "eigenpuls://pair", "", "not a url"]
)
def test_only_http_and_https_are_targets(url):
    assert not is_usable_target(url)


# --- the token must not leak -------------------------------------------------


def test_the_payload_never_renders_its_token():
    """A dataclass repr is how a secret reaches a log without anyone deciding to."""
    payload = PairingPayload(url=URL, token=TOKEN, reach=Reach.REMOTE)
    assert TOKEN not in repr(payload)
    assert TOKEN not in str(payload)
    assert "<redacted>" in repr(payload)


def test_errors_never_carry_the_payload():
    for bad in ("eigenpuls://pair#" + base64.urlsafe_b64encode(
        json.dumps({"v": 1, "url": URL, "token": TOKEN}).encode()
    ).decode(),):
        with pytest.raises(PairingError) as err:
            decode(bad)
        assert TOKEN not in str(err.value)


def _uri_for(body: dict) -> str:
    raw = json.dumps(body, separators=(",", ":")).encode()
    return "eigenpuls://pair#" + base64.urlsafe_b64encode(raw).decode().rstrip("=")
