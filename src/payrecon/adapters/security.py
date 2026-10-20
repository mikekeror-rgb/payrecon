"""Secrets handling: encrypting the payer's number at rest, and Daraja's SecurityCredential."""
from __future__ import annotations

import base64
import re

_MSISDN = re.compile(r"^254[17]\d{8}$")


def normalize_msisdn(raw: str) -> str:
    """07XXXXXXXX, 7XXXXXXXX, +2547XXXXXXXX and 2547XXXXXXXX all become 2547XXXXXXXX (Safaricom 07/01 ranges).
    Raises ValueError on anything else, before anything is sent."""
    s = re.sub(r"[\s+\-]", "", str(raw))
    if re.fullmatch(r"0[17]\d{8}", s):
        s = "254" + s[1:]
    elif re.fullmatch(r"[17]\d{8}", s):
        s = "254" + s
    if not _MSISDN.match(s):
        raise ValueError("not a valid Safaricom number (expected 2547XXXXXXXX)")
    return s


class PiiBox:
    """Seals the full phone number for storage. With a Fernet key (PAYRECON_PII_KEY) it is encrypted;
    plaintext storage is allowed only when explicitly requested (sandbox)."""

    def __init__(self, key: str | None, *, allow_plaintext: bool = False):
        self._fernet = None
        if key:
            try:
                from cryptography.fernet import Fernet
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("install cryptography: pip install 'payrecon[live]'") from e
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        elif not allow_plaintext:
            raise RuntimeError("PAYRECON_PII_KEY is not set; refusing to store phone numbers in plaintext "
                               "(generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\")")

    def seal(self, value: str) -> str:
        if self._fernet:
            return "enc1:" + self._fernet.encrypt(value.encode()).decode()
        return "plain:" + value

    def open(self, sealed: str) -> str:
        if sealed.startswith("enc1:"):
            if not self._fernet:
                raise RuntimeError("stored value is encrypted but no PAYRECON_PII_KEY is configured")
            return self._fernet.decrypt(sealed[5:].encode()).decode()
        if sealed.startswith("plain:"):
            return sealed[6:]
        raise ValueError("unrecognised sealed value")


def security_credential(initiator_password: str, cert_pem: bytes) -> str:
    """Daraja SecurityCredential: the initiator password encrypted with M-Pesa's public certificate
    (RSA, PKCS#1 v1.5) and base64-encoded. Written from public docs and SDK conventions; not verified live."""
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import padding

    cert = x509.load_pem_x509_certificate(cert_pem)
    ciphertext = cert.public_key().encrypt(initiator_password.encode(), padding.PKCS1v15())
    return base64.b64encode(ciphertext).decode()
