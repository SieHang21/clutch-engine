"""
Clutch Phase 1 Verification Test Suite
As specified in 'Clutch Agent Master Specification.pdf' Phase 1:
- normalize_bd_phone(text) converting Bengali numerals (০-৯) to ASCII digits and extracting 11-digit numbers (013-019)
- resolve_zone(address) classifying addresses into Inside Dhaka (৳70), Suburbs (৳100), or Outside Dhaka (৳130)
- verify_meta_signature(payload_bytes, signature_header, secret) validating X-Hub-Signature-256 using HMAC-SHA256
- 10+ dirty Banglish test strings simulating real-world customer DMs
"""

import hmac
import hashlib
import pytest
from app.core.sanitizer import (
    normalize_bd_phone,
    resolve_zone,
    detect_multi_number_conflict,
    extract_clean_phone,
    FEE_DHAKA_CORE,
    FEE_SUBURBS,
    FEE_OUTSIDE_DHAKA,
)
from app.core.security import verify_meta_signature


# =========================================================================
# 10 Real-world Dirty Banglish Test Strings
# =========================================================================
DIRTY_BANGLISH_TEST_CASES = [
    {
        "id": 1,
        "raw_text": "Bhai 2 ta black hoodie lagbe. Call me ০১৭-১২৩৪৫৬৭৮. Address: House 12, Road 5, Mirpur-10, Dhaka",
        "expected_phones": ["01712345678"],
        "expected_zone": "Inside Dhaka",
        "expected_fee": 70,
        "has_conflict": False,
    },
    {
        "id": 2,
        "raw_text": "Amar phone +8801812-345678, Uttara Sector 7 road 3. Cash on delivery te pathan.",
        "expected_phones": ["01812345678"],
        "expected_zone": "Inside Dhaka",
        "expected_fee": 70,
        "has_conflict": False,
    },
    {
        "id": 3,
        "raw_text": "Ami Savar thaki, Savar bus stand er pashe ashben. Mobile: ০১৭১১-২২৩৩৪৪",
        "expected_phones": ["01711223344"],
        "expected_zone": "Suburbs",
        "expected_fee": 100,
        "has_conflict": False,
    },
    {
        "id": 4,
        "raw_text": "Gazipur chowrasta shibbari mor. Number: +88-01912345678, taka advance pathano ache",
        "expected_phones": ["01912345678"],
        "expected_zone": "Suburbs",
        "expected_fee": 100,
        "has_conflict": False,
    },
    {
        "id": 5,
        "raw_text": "Address: Tongi Bazar road, Tongi. Phone: 01612345678. Shighroi pathaben",
        "expected_phones": ["01612345678"],
        "expected_zone": "Suburbs",
        "expected_fee": 100,
        "has_conflict": False,
    },
    {
        "id": 6,
        "raw_text": "Chittagong GEC er mor, Nasirabad. Phone: 01512345678. Tk 1500 confirm delivery",
        "expected_phones": ["01512345678"],
        "expected_zone": "Outside Dhaka",
        "expected_fee": 130,
        "has_conflict": False,
    },
    {
        "id": 7,
        "raw_text": "Sylhet Zindabazar points, phone: ০১৩১২৩৪৫৬৭৮। Red colour shirt ta pathan bhai",
        "expected_phones": ["01312345678"],
        "expected_zone": "Outside Dhaka",
        "expected_fee": 130,
        "has_conflict": False,
    },
    {
        "id": 8,
        "raw_text": "Dhanmondi 27, flat 4B. Call 01712345678 or 01898765432. Konta active paben call diyab",
        "expected_phones": ["01712345678", "01898765432"],
        "expected_zone": "Inside Dhaka",
        "expected_fee": 70,
        "has_conflict": True,
    },
    {
        "id": 9,
        "raw_text": "Keraniganj babubazar bridge er niche. Ph: (+880) 1412345678. Size L",
        "expected_phones": ["01412345678"],
        "expected_zone": "Suburbs",
        "expected_fee": 100,
        "has_conflict": False,
    },
    {
        "id": 10,
        "raw_text": "Bogra satmatha, Bogura sadar. ০১৭০০-১১২২৩৩. delivery charge koto bolen?",
        "expected_phones": ["01700112233"],
        "expected_zone": "Outside Dhaka",
        "expected_fee": 130,
        "has_conflict": False,
    },
]


class TestPhase1BanglishIngestion:
    """Test 10 dirty Banglish messages verifying phone extraction and zone classification."""

    @pytest.mark.parametrize("case", DIRTY_BANGLISH_TEST_CASES, ids=[f"Case-{c['id']}" for c in DIRTY_BANGLISH_TEST_CASES])
    def test_dirty_banglish_order_normalization(self, case):
        raw_text = case["raw_text"]
        expected_phones = case["expected_phones"]
        expected_zone = case["expected_zone"]
        expected_fee = case["expected_fee"]
        expected_conflict = case["has_conflict"]

        # 1. Phone extraction
        phones = normalize_bd_phone(raw_text)
        assert phones == expected_phones, f"Phone extraction mismatch for case {case['id']}"

        # 2. Multi-number conflict detection
        conflict = detect_multi_number_conflict(raw_text)
        assert conflict == expected_conflict, f"Conflict detection mismatch for case {case['id']}"

        # 3. Zone resolution
        zone_res = resolve_zone(raw_text)
        assert zone_res == expected_zone, f"Zone mismatch for case {case['id']}"
        assert zone_res.fee == expected_fee, f"Fee mismatch for case {case['id']}"


# =========================================================================
# Meta Webhook HMAC-SHA256 Security Tests
# =========================================================================
class TestMetaHMACSecurity:
    """Test Meta Graph Webhook HMAC-SHA256 signature verification."""

    def test_valid_meta_signature(self):
        secret = "clutch_webhook_secret_key_12345"
        payload = b'{"object":"page","entry":[{"id":"1029384756","time":1700000000}]}'
        expected_digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        signature_header = f"sha256={expected_digest}"

        assert verify_meta_signature(payload, signature_header, secret) is True

    def test_tampered_payload_rejected(self):
        secret = "clutch_webhook_secret_key_12345"
        payload = b'{"object":"page","entry":[{"id":"1029384756","time":1700000000}]}'
        tampered_payload = b'{"object":"page","entry":[{"id":"HACKED","time":1700000000}]}'
        expected_digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        signature_header = f"sha256={expected_digest}"

        assert verify_meta_signature(tampered_payload, signature_header, secret) is False

    def test_invalid_secret_rejected(self):
        secret = "correct_secret"
        wrong_secret = "wrong_secret"
        payload = b'{"test":"data"}'
        expected_digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        signature_header = f"sha256={expected_digest}"

        assert verify_meta_signature(payload, signature_header, wrong_secret) is False

    def test_malformed_signature_header_rejected(self):
        secret = "secret"
        payload = b'{"test":"data"}'

        # Missing sha256= prefix
        assert verify_meta_signature(payload, "invalid_header", secret) is False
        # None header
        assert verify_meta_signature(payload, None, secret) is False
        # Empty header
        assert verify_meta_signature(payload, "", secret) is False
        # Empty secret
        assert verify_meta_signature(payload, "sha256=abcdef", "") is False

    def test_string_and_bytes_payload_compatibility(self):
        secret = "app_secret"
        payload_str = '{"message":"hello world"}'
        expected_digest = hmac.new(secret.encode(), payload_str.encode("utf-8"), hashlib.sha256).hexdigest()
        header = f"sha256={expected_digest}"

        # Passing string payload directly
        assert verify_meta_signature(payload_str, header, secret) is True
        # Passing bytes payload
        assert verify_meta_signature(payload_str.encode("utf-8"), header, secret) is True

