"""
Clutch (ক্ল্যাচ) - Security Layer
HMAC-SHA256 Webhook Verification and Cryptographic Utilities
"""

import hmac
import hashlib
from typing import Union, Optional


def verify_meta_signature(
    payload_bytes: Union[bytes, str, None],
    signature_header: Optional[str],
    secret: Union[str, bytes, None]
) -> bool:
    """
    Validate incoming Meta (Facebook Graph API) webhook signature against the merchant's App Secret.
    
    Header format: `X-Hub-Signature-256: sha256=<64-hex-characters>`
    
    Security:
      - Enforces HMAC-SHA256 verification.
      - Uses constant-time `hmac.compare_digest` to mitigate timing attacks.
      - Gracefully handles malformed, missing, or spoofed headers without throwing exceptions.
    
    Returns:
        True if the signature is valid, False otherwise.
    """
    if payload_bytes is None or not signature_header or not secret:
        return False

    if isinstance(payload_bytes, str):
        payload_bytes = payload_bytes.encode("utf-8")

    if isinstance(secret, str):
        secret = secret.encode("utf-8")

    # Meta signature header must start with "sha256="
    prefix = "sha256="
    if not signature_header.startswith(prefix):
        return False

    extracted_sig = signature_header[len(prefix):].strip()
    if not extracted_sig:
        return False

    # Compute expected HMAC-SHA256
    computed_sig = hmac.new(secret, payload_bytes, hashlib.sha256).hexdigest()

    # Compare in constant time
    return hmac.compare_digest(computed_sig, extracted_sig)

