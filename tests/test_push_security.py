from __future__ import annotations

import socket

import pytest

from app.services.push_security import (
    PushEndpointPolicy,
    UnsafePushEndpointError,
    validate_subscription_keys,
)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://push.example.test/send",
        "not a URL",
        "https://[::1",
        "https://user:password@push.example.test/send",
        "https://push.example.test/send#fragment",
        "https://push.example.test:8443/send",
        "https://localhost/send",
        "https://127.0.0.1/send",
        "https://10.0.0.1/send",
        "https://169.254.1.1/send",
        "https://100.64.0.1/send",
        "https://0.0.0.0/send",
        "https://224.0.0.1/send",
        "https://192.0.2.1/send",
        "https://[::1]/send",
        "https://[fc00::1]/send",
        "https://[fe80::1]/send",
        "https://[::ffff:127.0.0.1]/send",
        "https://[2606:4700:4700::1111%25eth0]/send",
        "https://push.example.test\\@127.0.0.1/send",
    ],
)
def test_endpoint_policy_rejects_unsafe_structures_and_direct_addresses(
    endpoint: str,
):
    policy = PushEndpointPolicy(lambda _hostname, _port: ("127.0.0.1",))

    with pytest.raises(UnsafePushEndpointError):
        policy.validate(endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://8.8.8.8/send",
        "https://[2606:4700:4700::1111]/send",
        "https://[::ffff:8.8.8.8]/send",
    ],
)
def test_endpoint_policy_accepts_global_direct_addresses(endpoint: str):
    policy = PushEndpointPolicy(lambda _hostname, _port: ())

    assert policy.validate(endpoint) == endpoint


def test_endpoint_policy_accepts_dns_only_when_every_answer_is_global():
    calls: list[tuple[str, int]] = []

    def resolver(hostname: str, port: int):
        calls.append((hostname, port))
        return ("8.8.8.8", "2606:4700:4700::1111")

    endpoint = "https://push.example.test/send?token=synthetic"

    assert PushEndpointPolicy(resolver).validate(endpoint) == endpoint
    assert calls == [("push.example.test", 443)]


def test_endpoint_policy_rejects_mixed_public_and_private_dns_answers():
    policy = PushEndpointPolicy(
        lambda _hostname, _port: ("8.8.8.8", "10.0.0.8")
    )

    with pytest.raises(UnsafePushEndpointError):
        policy.validate("https://push.example.test/send")


@pytest.mark.parametrize("answers", [(), ("not-an-address",)])
def test_endpoint_policy_rejects_empty_or_invalid_dns_answers(answers: tuple[str, ...]):
    policy = PushEndpointPolicy(lambda _hostname, _port: answers)

    with pytest.raises(UnsafePushEndpointError):
        policy.validate("https://push.example.test/send")


def test_endpoint_policy_rejects_dns_failure():
    def failing_resolver(_hostname: str, _port: int):
        raise socket.gaierror("synthetic DNS failure")

    with pytest.raises(UnsafePushEndpointError):
        PushEndpointPolicy(failing_resolver).validate(
            "https://push.example.test/send"
        )


def test_endpoint_policy_rejects_oversized_endpoint():
    endpoint = "https://push.example.test/" + "a" * 2_100

    with pytest.raises(UnsafePushEndpointError):
        PushEndpointPolicy(lambda _hostname, _port: ("8.8.8.8",)).validate(
            endpoint
        )


def test_subscription_key_validation_accepts_synthetic_browser_keys(
    subscription_keys: dict[str, str],
):
    assert validate_subscription_keys(
        subscription_keys["p256dh"],
        subscription_keys["auth"],
    ) == (subscription_keys["p256dh"], subscription_keys["auth"])


@pytest.mark.parametrize(
    ("p256dh", "auth"),
    [
        ("a" * 257, "a" * 22),
        ("a" * 87, "a" * 129),
        ("not+base64url", "a" * 22),
        ("a" * 87, "too-short"),
    ],
)
def test_subscription_key_validation_rejects_malformed_or_oversized_values(
    p256dh: str,
    auth: str,
):
    with pytest.raises(ValueError, match="invalid push subscription"):
        validate_subscription_keys(p256dh, auth)
