from __future__ import annotations

import ipaddress
import os
from collections.abc import Iterable


PROXY_FAKE_IP_NETWORKS = (
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("fdfe:dcba:9876::/48"),
)


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def contains_proxy_fake_ip(
    addresses: Iterable[ipaddress.IPv4Address | ipaddress.IPv6Address],
) -> bool:
    return any(
        any(address in network for network in PROXY_FAKE_IP_NETWORKS)
        for address in addresses
    )


def address_is_allowed(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    allow_fake_ip_dns: bool,
) -> bool:
    if address.is_global:
        return True
    return allow_fake_ip_dns and any(
        address in network for network in PROXY_FAKE_IP_NETWORKS
    )
