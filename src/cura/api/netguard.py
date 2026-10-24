"""Outbound HTTP that a caller can point somewhere: website verification and webhook delivery.

Both take a URL that came, one way or another, from a user. Without care, that is a request forgery
primitive: "verify my website at http://169.254.169.254/" reads cloud credentials, a webhook to
http://localhost:5432 probes the database. Every outbound request therefore goes through `fetch`:

1. **https only**, default port only (443), no credentials in the URL.
2. **Host allow-list**: when the caller says which host is allowed (the facility's own website), any
   other host is refused before anything is resolved.
3. **Resolve, then check every address**: private, loopback, link-local, multicast, reserved,
   unspecified and carrier-grade NAT ranges are refused (IPv4 and IPv6, including IPv4-mapped IPv6).
   One bad address in the answer refuses the whole name.
4. **Pin**: the connection goes to the address that was checked, not to a second lookup (no DNS
   rebinding between check and use). TLS still verifies the certificate against the hostname (SNI).
5. **No redirects** to another host; a same-host redirect is re-checked from step 1, at most 3 hops.
6. **5 s** total timeout, and at most `max_bytes` of body are read (4 KB for verification).
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

TIMEOUT_S = 5.0
MAX_REDIRECTS = 3
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class Blocked(Exception):
    """The request was refused before or during the connection. The message is safe to show."""


@dataclass
class Fetched:
    status: int
    body: bytes
    url: str
    headers: dict[str, str]


def _resolve(host: str) -> list[str]:  # replaced in tests
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def _transport() -> httpx.BaseTransport | None:  # replaced in tests
    return None


def is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.version == 4 and ip in _CGNAT:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def normalise_host(host: str | None) -> str | None:
    if not host:
        return None
    host = host.strip().lower().rstrip(".")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def check_url(url: str, *, allowed_host: str | None = None) -> tuple[str, str]:
    """Static checks (no network). Returns (host, path_and_query)."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise Blocked("Only https URLs are allowed.")
    if parts.username or parts.password:
        raise Blocked("Credentials in the URL are not allowed.")
    if parts.port not in (None, 443):
        raise Blocked("Only the default https port (443) is allowed.")
    host = normalise_host(parts.hostname)
    if not host or "." not in host:
        raise Blocked("The URL needs a public hostname.")
    try:
        ipaddress.ip_address(host.strip("[]"))
        raise Blocked("Use a hostname, not an IP address.")
    except ValueError:
        pass
    if host in ("localhost",) or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        raise Blocked("Internal hostnames are not allowed.")
    if allowed_host is not None and host != normalise_host(allowed_host):
        raise Blocked(f"Only the facility's own host ({allowed_host}) may be fetched.")
    path = parts.path or "/"
    return host, path + (f"?{parts.query}" if parts.query else "")


def resolve_public(host: str) -> str:
    try:
        addresses = _resolve(host)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise Blocked(f"{host} does not resolve.") from exc
    if not addresses:
        raise Blocked(f"{host} does not resolve.")
    bad = [a for a in addresses if not is_public(a)]
    if bad:
        raise Blocked(f"{host} resolves to a private or reserved address; refused.")
    return addresses[0]


def fetch(
    url: str,
    *,
    method: str = "GET",
    allowed_host: str | None = None,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
    max_bytes: int = 4096,
    timeout: float = TIMEOUT_S,
) -> Fetched:
    deadline = time.monotonic() + timeout
    for _hop in range(MAX_REDIRECTS + 1):
        host, target = check_url(url, allowed_host=allowed_host)
        address = resolve_public(host)
        literal = f"[{address}]" if ":" in address else address
        remaining = max(0.5, deadline - time.monotonic())
        req_headers = {"Host": host, "User-Agent": "Cura/1.0 (+https://cura.jo)", **(headers or {})}
        with httpx.Client(
            transport=_transport(), timeout=httpx.Timeout(remaining), follow_redirects=False, trust_env=False
        ) as client:
            request = client.build_request(
                method,
                f"https://{literal}{target}",
                headers=req_headers,
                content=content,
                extensions={"sni_hostname": host},
            )
            try:
                response = client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                raise Blocked("The server did not answer within 5 seconds.") from exc
            except httpx.HTTPError as exc:
                raise Blocked(f"Could not connect to {host}.") from exc
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise Blocked("Redirect without a location.")
                    nxt = urljoin(f"https://{host}{target}", location)
                    if normalise_host(urlsplit(nxt).hostname) != host:
                        raise Blocked("Redirects to another host are not followed.")
                    url, allowed_host = nxt, host
                    if response.status_code == 303:
                        method, content = "GET", None
                    continue
                body = b""
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) >= max_bytes:
                        body = body[:max_bytes]
                        break
                    if time.monotonic() > deadline:
                        raise Blocked("The server did not answer within 5 seconds.")
                return Fetched(response.status_code, body, f"https://{host}{target}", dict(response.headers))
            finally:
                response.close()
    raise Blocked("Too many redirects.")
