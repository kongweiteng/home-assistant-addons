"""Ephemeral loopback TLS protocol key, never a persisted account credential.

Engine must already classify loopback as insecure. This does not establish a
certificate identity/trust store and never changes daemon settings or a CA.
"""
from datetime import datetime,timedelta
import ipaddress
from pathlib import Path
import ssl
import tempfile

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .service import TZ,private,write


def ephemeral_context(runtime_root=Path('/run')):
    key=ec.generate_private_key(ec.SECP256R1())
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'codex-loopback-artifact-registry')])
    now=datetime.now(TZ)
    certificate=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
        .not_valid_after(now+timedelta(days=365)).add_extension(x509.SubjectAlternativeName([
            x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False).sign(key,hashes.SHA256()))
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version=ssl.TLSVersion.TLSv1_2
    # Python's API needs paths. The private files exist only until SSL loads
    # them and are unlinked before any listener can start; /run is volatile.
    with tempfile.TemporaryDirectory(prefix='.codex-registry-tls-',dir=runtime_root) as temporary:
        root=private(Path(temporary))
        write(root/'key.pem',key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        write(root/'cert.pem',certificate.public_bytes(serialization.Encoding.PEM))
        context.load_cert_chain(root/'cert.pem',root/'key.pem')
    return context
