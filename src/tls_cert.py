"""Self-signed TLS certificate management for retina-gui.

The GUI is served at owl.local/retina.local, which are mDNS names and can
never get a publicly-trusted CA certificate (Let's Encrypt requires public
DNS validation). A self-signed cert is generated once and persisted so it
survives restarts/reboots — regenerating it on every boot would invalidate
the browser's "proceed anyway" exception each time.
"""

import datetime
import ipaddress
import os
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

CERT_HOSTNAMES = ('owl.local', 'retina.local', 'localhost')
CERT_IPS = ('127.0.0.1', '::1')
CERT_VALIDITY_DAYS = 3650


def ensure_self_signed_cert(data_dir):
    """Return (cert_path, key_path), generating a self-signed cert on first call."""
    tls_dir = os.path.join(data_dir, 'tls')
    cert_path = os.path.join(tls_dir, 'cert.pem')
    key_path = os.path.join(tls_dir, 'key.pem')

    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path

    os.makedirs(tls_dir, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, CERT_HOSTNAMES[0]),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    san = x509.SubjectAlternativeName(
        [x509.DNSName(host) for host in CERT_HOSTNAMES]
        + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in CERT_IPS]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=CERT_VALIDITY_DAYS))
        .add_extension(san, critical=False)
        .sign(key, hashes.SHA256())
    )

    key_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_bytes = cert.public_bytes(serialization.Encoding.PEM)

    _atomic_write(tls_dir, key_path, key_bytes, mode=0o600)
    _atomic_write(tls_dir, cert_path, cert_bytes, mode=0o644)

    return cert_path, key_path


def _atomic_write(dir_path, dest_path, data, mode):
    fd, tmp_path = tempfile.mkstemp(dir=dir_path)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
    os.chmod(tmp_path, mode)
    os.rename(tmp_path, dest_path)
