"""TLS certificate management — generates self-signed certs and validates provided ones."""
from __future__ import annotations
import ipaddress
import logging
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

logger = logging.getLogger("anvil.tls")


def _build_san_list(extra_sans: List[str]) -> list:
    """Build a SAN list from the local hostname/IP plus any configured extras."""
    from cryptography import x509

    hostname = socket.getfqdn()
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        local_ip = "127.0.0.1"

    san_list = [
        x509.DNSName("localhost"),
        x509.DNSName(hostname),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
    ]
    try:
        san_list.append(x509.IPAddress(ipaddress.IPv4Address(local_ip)))
    except ValueError:
        pass

    for entry in extra_sans:
        entry = entry.strip()
        if not entry:
            continue
        try:
            san_list.append(x509.IPAddress(ipaddress.ip_address(entry)))
        except ValueError:
            san_list.append(x509.DNSName(entry))

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for s in san_list:
        key = repr(s)
        if key not in seen:
            seen.add(key)
            deduped.append(s)
    return deduped


def generate_self_signed_cert(cert_path: str, key_path: str, extra_sans: List[str] | None = None) -> None:
    """Generate a 4096-bit RSA self-signed certificate valid for 10 years."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    hostname = socket.getfqdn()
    san_list = _build_san_list(extra_sans or [])

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Anvil"),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san_list), critical=False)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=True, data_encipherment=False,
                key_agreement=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    key_p = Path(key_path)
    cert_p = Path(cert_path)
    key_p.parent.mkdir(parents=True, exist_ok=True)

    # Make existing files writable before overwriting (they may be 0o400/0o444)
    if key_p.exists():
        key_p.chmod(0o600)
    if cert_p.exists():
        cert_p.chmod(0o644)

    with open(key_path, "wb") as f:
        f.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    key_p.chmod(0o400)

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    cert_p.chmod(0o444)

    dns_names = [s.value for s in san_list if hasattr(s, "value")]
    logger.info("Self-signed TLS cert generated: %s (SANs: %s)", cert_path, ", ".join(dns_names))


def _cert_covers_sans(cert_path: str, required_sans: List[str]) -> bool:
    """Return True if the cert's SAN covers all required_sans entries."""
    from cryptography import x509
    from cryptography.x509 import DNSName, IPAddress as CertIP

    try:
        cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        covered_dns = {n.value.lower() for n in san_ext.value if isinstance(n, DNSName)}
        covered_ips = {str(n.value) for n in san_ext.value if isinstance(n, CertIP)}
    except Exception:
        return False

    for entry in required_sans:
        entry = entry.strip()
        if not entry:
            continue
        try:
            ipaddress.ip_address(entry)
            if entry not in covered_ips:
                return False
        except ValueError:
            if entry.lower() not in covered_dns:
                return False
    return True


def ensure_tls_cert(cert_path: str, key_path: str, extra_sans: List[str] | None = None) -> None:
    """Generate cert if it doesn't exist, is expiring within 30 days, or is missing required SANs."""
    from cryptography import x509

    cert_p = Path(cert_path)
    key_p = Path(key_path)
    extra_sans = extra_sans or []

    if cert_p.exists() and key_p.exists():
        try:
            cert = x509.load_pem_x509_certificate(cert_p.read_bytes())
            days_left = (cert.not_valid_after_utc - datetime.now(timezone.utc)).days
            if days_left <= 30:
                logger.warning("TLS cert expires in %d days — regenerating.", days_left)
            elif extra_sans and not _cert_covers_sans(cert_path, extra_sans):
                logger.warning(
                    "TLS cert does not cover configured extra_sans %s — regenerating.", extra_sans
                )
            else:
                logger.info("TLS cert valid for %d more days.", days_left)
                return
        except Exception as exc:
            logger.warning("Could not parse existing cert (%s) — regenerating.", exc)

    generate_self_signed_cert(cert_path, key_path, extra_sans)
