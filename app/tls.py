"""Fetch the expiry date of a public TLS certificate using only the standard library.

We only read the peer's *public* certificate. No private keys are involved or stored.
"""

from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass
from datetime import UTC, date, datetime

DEFAULT_PORT = 443


class TLSProbeError(Exception):
    pass


@dataclass(frozen=True)
class TLSCertInfo:
    hostname: str
    port: int
    not_after: date
    not_before: date | None
    issuer: str | None
    subject: str | None
    verified: bool


def parse_hostport(raw: str, default_port: int = DEFAULT_PORT) -> tuple[str, int]:
    """'api.example.com:443' -> ('api.example.com', 443); 'api.example.com' -> (..., 443)."""
    raw = raw.strip()
    if not raw:
        raise TLSProbeError("hostname is empty")
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    raw = raw.split("/", 1)[0]
    host, sep, port_s = raw.rpartition(":")
    if not sep:
        return raw, default_port
    if not host:
        raise TLSProbeError(f"invalid host:port '{raw}'")
    try:
        port = int(port_s)
    except ValueError as exc:
        raise TLSProbeError(f"invalid port in '{raw}'") from exc
    if not 1 <= port <= 65535:
        raise TLSProbeError(f"port out of range in '{raw}'")
    return host, port


def normalize_hostport(raw: str) -> str:
    host, port = parse_hostport(raw)
    return f"{host.lower()}:{port}"


def parse_cert_time(value: str) -> datetime:
    """Parse the OpenSSL text format ssl.getpeercert() returns, e.g. 'Jun  1 12:00:00 2027 GMT'."""
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(value), tz=UTC)


def _rdn_to_str(rdns) -> str:
    parts = []
    for rdn in rdns:
        for key, val in rdn:
            parts.append(f"{key}={val}")
    return ", ".join(parts)


def parse_peercert(cert: dict, hostname: str, port: int, *, verified: bool = True) -> TLSCertInfo:
    """Build TLSCertInfo from the dict returned by SSLSocket.getpeercert()."""
    if not cert or "notAfter" not in cert:
        raise TLSProbeError("peer certificate has no notAfter field")
    not_after = parse_cert_time(cert["notAfter"]).date()
    not_before = parse_cert_time(cert["notBefore"]).date() if cert.get("notBefore") else None
    issuer = _rdn_to_str(cert.get("issuer", ())) or None
    subject = _rdn_to_str(cert.get("subject", ())) or None
    return TLSCertInfo(hostname, port, not_after, not_before, issuer, subject, verified)


def _parse_der(der: bytes, hostname: str, port: int) -> TLSCertInfo:
    """Fallback for self-signed / untrusted chains: parse DER with `cryptography` if installed."""
    try:
        from cryptography import x509
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise TLSProbeError("certificate chain is not trusted and 'cryptography' is not installed to parse it") from exc
    c = x509.load_der_x509_certificate(der)
    return TLSCertInfo(
        hostname,
        port,
        c.not_valid_after_utc.date(),
        c.not_valid_before_utc.date(),
        c.issuer.rfc4514_string(),
        c.subject.rfc4514_string(),
        verified=False,
    )


def fetch_certificate(hostport: str, timeout: float = 10.0) -> TLSCertInfo:
    """Open a TLS connection to host:port (SNI = host) and return the leaf certificate's dates."""
    host, port = parse_hostport(hostport)
    ctx = ssl.create_default_context()
    try:
        with (
            socket.create_connection((host, port), timeout=timeout) as sock,
            ctx.wrap_socket(sock, server_hostname=host) as ssock,
        ):
            return parse_peercert(ssock.getpeercert(), host, port, verified=True)
    except ssl.SSLCertVerificationError:
        # Untrusted chain (self-signed, internal CA, expired). Still useful to know the expiry.
        ctx_unverified = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx_unverified.check_hostname = False
        ctx_unverified.verify_mode = ssl.CERT_NONE
        try:
            with (
                socket.create_connection((host, port), timeout=timeout) as sock,
                ctx_unverified.wrap_socket(sock, server_hostname=host) as ssock,
            ):
                der = ssock.getpeercert(binary_form=True)
        except (OSError, ssl.SSLError) as exc:
            raise TLSProbeError(f"TLS handshake failed for {host}:{port}: {exc}") from exc
        if not der:
            raise TLSProbeError(f"no certificate returned by {host}:{port}") from None
        return _parse_der(der, host, port)
    except socket.gaierror as exc:
        raise TLSProbeError(f"cannot resolve {host}: {exc}") from exc
    except TimeoutError as exc:
        raise TLSProbeError(f"connection to {host}:{port} timed out") from exc
    except (OSError, ssl.SSLError) as exc:
        raise TLSProbeError(f"connection to {host}:{port} failed: {exc}") from exc
