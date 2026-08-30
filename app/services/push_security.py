from __future__ import annotations

import base64
import binascii
import ipaddress
import re
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec


MAX_PUSH_ENDPOINT_LENGTH = 2_048
MAX_P256DH_LENGTH = 256
MAX_AUTH_LENGTH = 128
_BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
AddressResolver = Callable[[str, int], Iterable[str]]


class UnsafePushEndpointError(ValueError):
    """Raised without echoing an endpoint that failed the SSRF policy."""


def _default_resolver(hostname: str, port: int) -> Iterable[str]:
    results = socket.getaddrinfo(
        hostname,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    return {str(result[4][0]).split("%", 1)[0] for result in results}


def _is_global_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_private
    )


class PushEndpointPolicy:
    """Validate push endpoints structurally and against all resolved addresses."""

    def __init__(self, resolver: AddressResolver | None = None) -> None:
        self._resolver = resolver or _default_resolver

    def validate(self, endpoint: str) -> str:
        candidate = endpoint.strip()
        if (
            not candidate
            or len(candidate) > MAX_PUSH_ENDPOINT_LENGTH
            or candidate != endpoint
            or any(ord(character) < 33 for character in candidate)
            or "\\" in candidate
        ):
            raise UnsafePushEndpointError("push endpoint is not allowed")
        try:
            parsed = urlsplit(candidate)
        except ValueError as exc:
            raise UnsafePushEndpointError("push endpoint is not allowed") from exc
        try:
            port = parsed.port
        except ValueError as exc:
            raise UnsafePushEndpointError("push endpoint is not allowed") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port not in {None, 443}
        ):
            raise UnsafePushEndpointError("push endpoint is not allowed")

        hostname = parsed.hostname.rstrip(".").casefold()
        if not hostname or len(hostname) > 253 or "%" in hostname:
            raise UnsafePushEndpointError("push endpoint is not allowed")
        try:
            direct_address = ipaddress.ip_address(hostname)
        except ValueError:
            try:
                ascii_hostname = hostname.encode("idna").decode("ascii")
            except UnicodeError as exc:
                raise UnsafePushEndpointError("push endpoint is not allowed") from exc
            labels = ascii_hostname.split(".")
            if any(
                not label
                or len(label) > 63
                or re.fullmatch(r"[A-Za-z0-9-]+", label) is None
                or label.startswith("-")
                or label.endswith("-")
                for label in labels
            ):
                raise UnsafePushEndpointError("push endpoint is not allowed")
            try:
                addresses = tuple(self._resolver(ascii_hostname, 443))
            except (OSError, socket.gaierror) as exc:
                raise UnsafePushEndpointError("push endpoint is not allowed") from exc
            if not addresses or not all(
                _is_global_address(value) for value in addresses
            ):
                raise UnsafePushEndpointError("push endpoint is not allowed")
        else:
            normalized = direct_address
            if isinstance(normalized, ipaddress.IPv6Address) and normalized.ipv4_mapped:
                normalized = normalized.ipv4_mapped
            if not _is_global_address(str(normalized)):
                raise UnsafePushEndpointError("push endpoint is not allowed")
        return candidate


def _decode_base64url(value: str) -> bytes:
    if not value or not _BASE64URL_PATTERN.fullmatch(value):
        raise ValueError("invalid push subscription")
    try:
        return base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid push subscription") from exc


def validate_subscription_keys(p256dh: str, auth: str) -> tuple[str, str]:
    p256dh_value = p256dh.strip()
    auth_value = auth.strip()
    if (
        p256dh_value != p256dh
        or auth_value != auth
        or len(p256dh_value) > MAX_P256DH_LENGTH
        or len(auth_value) > MAX_AUTH_LENGTH
    ):
        raise ValueError("invalid push subscription")
    decoded_public = _decode_base64url(p256dh_value)
    decoded_auth = _decode_base64url(auth_value)
    if (
        len(decoded_public) != 65
        or decoded_public[0] != 4
        or len(decoded_auth) != 16
    ):
        raise ValueError("invalid push subscription")
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(),
            decoded_public,
        )
    except ValueError as exc:
        raise ValueError("invalid push subscription") from exc
    return p256dh_value, auth_value
