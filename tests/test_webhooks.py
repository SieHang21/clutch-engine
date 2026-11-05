"""
Unit Tests for Clutch Phase 2: Webhooks, Meta Security & Session Accumulator
Covers:
  - GET verification challenge (hub.mode, hub.verify_token, hub.challenge)
  - POST HMAC-SHA256 signature verification (valid, invalid, tampered, missing)
  - Multi-message burst accumulation across sliding 75-second window
  - Completeness detection (phone + address) and automatic normalization
  - Admin Takeover Sensor (is_echo / page admin mute for 45 minutes)
  - 24-hour Meta messaging safety window (23h 50m cutoff)
"""

import hmac
import hashlib
import json
import time
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.api.webhooks import set_webhook_secrets, set_session_buffer
from app.services.session_buffer import (
    SessionBuffer,
    InMemorySessionBuffer,
    is_order_complete,
    is_within_messaging_window,
    DEFAULT_DEBOUNCE_TTL,
    DEFAULT_TAKEOVER_TTL,
)

TEST_APP_SECRET = "test_meta_app_secret_clutch_9988"
TEST_VERIFY_TOKEN = "test_meta_verify_token_123"


@pytest.fixture(autouse=True)
def setup_webhook_env():
    """Setup isolated test configuration for each test."""
    set_webhook_secrets(app_secret=TEST_APP_SECRET, verify_token=TEST_VERIFY_TOKEN)
    isolated_buffer = SessionBuffer()
    # Force in-memory backend for deterministic isolated unit tests
    isolated_buffer._redis_backend = None
    set_session_buffer(isolated_buffer)
    yield isolated_buffer


@pytest.fixture
def client():
    return TestClient(app)


def generate_meta_signature(payload_bytes: bytes, secret: str = TEST_APP_SECRET) -> str:
    """Helper to generate valid X-Hub-Signature-256 header."""
    digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# =========================================================================
# 1. GET Webhook Verification Tests
# =========================================================================
class TestWebhookVerificationGet:
    """Tests for Meta Webhook setup challenge (GET)."""

    def test_valid_verification_challenge(self, client):
        """Valid subscribe mode and matching verify_token must return the challenge with 200 OK."""
        challenge_code = "1158201444"
        params = {
            "hub.mode": "subscribe",
            "hub.verify_token": TEST_VERIFY_TOKEN,
            "hub.challenge": challenge_code,
        }
        response = client.get("/webhooks", params=params)
        assert response.status_code == 200
        assert response.text == challenge_code

    def test_valid_verification_challenge_api_prefix(self, client):
        """Also functions under /api/webhooks prefix."""
        challenge_code = "9988776655"
        params = {
            "hub.mode": "subscribe",
            "hub.verify_token": TEST_VERIFY_TOKEN,
            "hub.challenge": challenge_code,
        }
        response = client.get("/api/webhooks", params=params)
        assert response.status_code == 200
        assert response.text == challenge_code

    def test_invalid_verify_token_rejected(self, client):
        """Mismatched verification token must return 403 Forbidden."""
        params = {
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong_attacker_token",
            "hub.challenge": "1158201444",
        }
        response = client.get("/webhooks", params=params)
        assert response.status_code == 403
        assert "Verification token mismatch" in response.json()["detail"]

    def test_invalid_mode_rejected(self, client):
        """Non-subscribe hub.mode must return 403 Forbidden."""
        params = {
            "hub.mode": "publish",
            "hub.verify_token": TEST_VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        }
        response = client.get("/webhooks", params=params)
        assert response.status_code == 403

    def test_missing_params_rejected(self, client):
        """Empty query params must return 403 Forbidden."""
        response = client.get("/webhooks")
        assert response.status_code == 403


# =========================================================================
# 2. POST HMAC-SHA256 Signature Verification Tests
# =========================================================================
class TestWebhookSignaturePost:
    """Tests for zero-trust HMAC-SHA256 signature enforcement on incoming payloads."""

    def test_valid_signature_accepted(self, client):
        """Valid HMAC signature returns 200 OK."""
        payload = {
            "object": "page",
            "entry": [
                {
                    "id": "PAGE_101",
                    "time": 1700000000,
                    "messaging": [
                        {
                            "sender": {"id": "USER_202"},
                            "recipient": {"id": "PAGE_101"},
                            "message": {"mid": "mid.1", "text": "Hello Clutch"},
                        }
                    ],
                }
            ],
        }
        body_bytes = json.dumps(payload).encode("utf-8")
        headers = {
            "X-Hub-Signature-256": generate_meta_signature(body_bytes),
            "Content-Type": "application/json",
        }
        response = client.post("/webhooks", content=body_bytes, headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "EVENT_RECEIVED"
        assert data["processed_count"] == 1

    def test_invalid_signature_rejected_403(self, client):
        """Forged or mismatched signature must be rejected with 403 Forbidden."""
        body_bytes = b'{"object":"page","entry":[]}'
        headers = {
            "X-Hub-Signature-256": "sha256=0000000000000000000000000000000000000000000000000000000000000000",
            "Content-Type": "application/json",
        }
        response = client.post("/webhooks", content=body_bytes, headers=headers)
        assert response.status_code == 403
        assert "Invalid HMAC-SHA256 signature" in response.json()["detail"]

    def test_missing_signature_header_rejected_403(self, client):
        """Missing X-Hub-Signature-256 header must return 403 Forbidden."""
        body_bytes = b'{"object":"page","entry":[]}'
        headers = {"Content-Type": "application/json"}
        response = client.post("/webhooks", content=body_bytes, headers=headers)
        assert response.status_code == 403

    def test_tampered_payload_rejected_403(self, client):
        """Tampering with body bytes after signature generation must return 403."""
        original_body = b'{"action":"order","amount":100}'
        sig = generate_meta_signature(original_body)
        tampered_body = b'{"action":"order","amount":999}'

        headers = {
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        }
        response = client.post("/webhooks", content=tampered_body, headers=headers)
        assert response.status_code == 403

    def test_malformed_json_returns_400(self, client):
        """Valid signature over invalid JSON syntax returns 400 Bad Request."""
        malformed_bytes = b"not-a-valid-json-string{"
        headers = {
            "X-Hub-Signature-256": generate_meta_signature(malformed_bytes),
            "Content-Type": "application/json",
        }
        response = client.post("/webhooks", content=malformed_bytes, headers=headers)
        assert response.status_code == 400
        assert "Malformed JSON payload" in response.json()["detail"]


# =========================================================================
# 3. Session Accumulator & Burst Debouncing Tests
# =========================================================================
class TestSessionAccumulator:
    """Tests for multi-message burst buffering, sliding TTL, and delivery pattern detection."""

    def test_fragmented_burst_aggregation_and_order_parsing(self, client, setup_webhook_env):
        """
        Simulate real-world customer DM burst split into 4 fragmented messages:
        1: 'Bhai 1 ta black panjabi lagbe'
        2: 'Size XL'
        3: 'Phone: 01712-345678'
        4: 'Address: Dhanmondi 27, road 4, house 12, Dhaka'
        """
        buffer = setup_webhook_env
        page_id = "PAGE_BD_STORE"
        sender_id = "CUSTOMER_RAHIM"

        messages = [
            "Bhai 1 ta black panjabi lagbe",
            "Size XL",
            "Phone: 01712-345678",
            "Address: Dhanmondi 27, road 4, house 12, Dhaka",
        ]

        # Send messages sequentially through webhook
        for msg in messages:
            payload = {
                "object": "page",
                "entry": [
                    {
                        "id": page_id,
                        "messaging": [
                            {
                                "sender": {"id": sender_id},
                                "recipient": {"id": page_id},
                                "message": {"mid": f"mid.{time.time()}", "text": msg},
                            }
                        ],
                    }
                ],
            }
            body = json.dumps(payload).encode("utf-8")
            res = client.post(
                "/webhooks",
                content=body,
                headers={"X-Hub-Signature-256": generate_meta_signature(body)},
            )
            assert res.status_code == 200

        # Verify buffer aggregated all 4 messages
        buffered = buffer.get_buffered_messages(sender_id, page_id=page_id)
        assert len(buffered) == 4
        assert buffered == messages

        # Flush session (debounce timer expired) and verify order normalization
        flushed = buffer.flush_session(sender_id, page_id=page_id)
        assert flushed["message_count"] == 4
        assert flushed["flushed"] is True or "order" in flushed

        order = flushed["order"]
        assert order is not None
        assert order["clean_phone"] == "01712345678"
        assert order["zone"] == "Inside Dhaka"
        assert order["fee"] == 70
        assert order["has_phone_conflict"] is False
        assert order["requires_human_review"] is False

    def test_bengali_burst_with_suburb_resolution(self, setup_webhook_env):
        """Customer sends fragmented Bengali message burst from Savar."""
        buffer = setup_webhook_env
        sender_id = "CUSTOMER_BENGALI"
        page_id = "PAGE_01"

        buffer.add_message(sender_id, "ভাইয়া লাল শাড়ি অর্ডার করব", page_id=page_id)
        buffer.add_message(sender_id, "আমার মোবাইল নম্বর ০১৭১১-২২৩৩৪৪", page_id=page_id)
        res = buffer.add_message(sender_id, "সাভার বাস স্ট্যান্ড এর কাছে ডেলিভারি দিবেন", page_id=page_id)

        # Phone + Suburb area detected
        assert res["is_complete"] is True
        order = res["order"]
        assert order["clean_phone"] == "01711223344"
        assert order["zone"] == "Suburbs"
        assert order["fee"] == 100

    def test_multi_number_conflict_in_burst(self, setup_webhook_env):
        """Customer sends 2 conflicting phone numbers across consecutive messages."""
        buffer = setup_webhook_env
        sender_id = "CUSTOMER_CONFLICT"

        buffer.add_message(sender_id, "Call 01711111111")
        res = buffer.add_message(sender_id, "Delivery boy na pele 01822222222 e call diben. Mirpur 10")

        assert res["is_complete"] is True
        order = res["order"]
        assert order["has_phone_conflict"] is True
        assert order["clean_phone"] is None
        assert order["requires_human_review"] is True

    def test_completeness_checker(self):
        """Verify is_order_complete detects presence of both valid phone and recognized area."""
        assert is_order_complete("Order hoodie") is False
        assert is_order_complete("Phone 01712345678") is False  # Missing address
        assert is_order_complete("Mirpur 10, Dhaka") is False   # Missing phone
        assert is_order_complete("Phone 01712345678, Dhanmondi 27") is True


# =========================================================================
# 4. Admin Takeover Sensor Tests
# =========================================================================
class TestAdminTakeoverSensor:
    """Tests for human takeover suppression sensor (45-minute bot muting)."""

    def test_is_echo_triggers_admin_takeover(self, client, setup_webhook_env):
        """
        When a human page admin replies to a customer via Page Inbox,
        Meta emits an event with is_echo: True.
        The sensor must mute the bot for 45 minutes for that customer.
        """
        buffer = setup_webhook_env
        page_id = "PAGE_SHOP_ADMIN"
        customer_id = "CUSTOMER_USER_77"

        # Initially, admin takeover is NOT active
        assert buffer.is_admin_takeover_active(customer_id, page_id=page_id) is False

        # Simulate Meta is_echo webhook payload (sender = page_id, recipient = customer_id)
        admin_reply_payload = {
            "object": "page",
            "entry": [
                {
                    "id": page_id,
                    "messaging": [
                        {
                            "sender": {"id": page_id},
                            "recipient": {"id": customer_id},
                            "message": {
                                "mid": "mid.admin.reply",
                                "text": "Apnar order confirmation kora hoyeche.",
                                "is_echo": True,
                            },
                        }
                    ],
                }
            ],
        }
        body = json.dumps(admin_reply_payload).encode("utf-8")
        res = client.post(
            "/webhooks",
            content=body,
            headers={"X-Hub-Signature-256": generate_meta_signature(body)},
        )
        assert res.status_code == 200
        events = res.json()["events"]
        assert len(events) == 1
        assert events[0]["type"] == "admin_takeover"
        assert events[0]["action"] == "muted_for_45m"

        # Verify takeover key is now active
        assert buffer.is_admin_takeover_active(customer_id, page_id=page_id) is True

    def test_customer_message_bypassed_during_active_takeover(self, client, setup_webhook_env):
        """
        While admin takeover is active, incoming customer messages must bypass automated bot parsing.
        """
        buffer = setup_webhook_env
        page_id = "PAGE_SHOP_ADMIN"
        customer_id = "CUSTOMER_USER_88"

        # Manually set takeover active
        buffer.set_admin_takeover(customer_id, page_id=page_id, ttl=2700)
        assert buffer.is_admin_takeover_active(customer_id, page_id=page_id) is True

        # Customer sends a message
        customer_payload = {
            "object": "page",
            "entry": [
                {
                    "id": page_id,
                    "messaging": [
                        {
                            "sender": {"id": customer_id},
                            "recipient": {"id": page_id},
                            "message": {
                                "mid": "mid.customer.new",
                                "text": "Bhai discount hobe?",
                                "is_echo": False,
                            },
                        }
                    ],
                }
            ],
        }
        body = json.dumps(customer_payload).encode("utf-8")
        res = client.post(
            "/webhooks",
            content=body,
            headers={"X-Hub-Signature-256": generate_meta_signature(body)},
        )
        assert res.status_code == 200
        events = res.json()["events"]
        assert len(events) == 1
        assert events[0]["action"] == "bypassed_due_to_admin_takeover"

        # Verify message was NOT added to session buffer
        buffered = buffer.get_buffered_messages(customer_id, page_id=page_id)
        assert len(buffered) == 0

    def test_clearing_admin_takeover_restores_bot_processing(self, setup_webhook_env):
        """Clearing takeover allows customer messages to buffer normally again."""
        buffer = setup_webhook_env
        customer_id = "CUSTOMER_99"

        buffer.set_admin_takeover(customer_id, ttl=2700)
        assert buffer.is_admin_takeover_active(customer_id) is True

        buffer.clear_admin_takeover(customer_id)
        assert buffer.is_admin_takeover_active(customer_id) is False

        # Now buffer allows adding messages
        res = buffer.add_message(customer_id, "Hello")
        assert res["message_count"] == 1


# =========================================================================
# 5. Meta 24-Hour Messaging Window Safety Timer Tests
# =========================================================================
class TestMessagingWindowTimer:
    """Tests for the 23h 50m messaging window guard."""

    def test_recent_message_within_window(self):
        """A message from 5 minutes ago is well within the 24h window."""
        now = time.time()
        five_mins_ago = now - 300
        assert is_within_messaging_window(five_mins_ago) is True

    def test_message_at_23h_40m_is_within_window(self):
        """A message from 23 hours 40 minutes ago is still valid."""
        now = time.time()
        timestamp = now - (23 * 3600 + 40 * 60)
        assert is_within_messaging_window(timestamp) is True

    def test_message_at_23h_55m_is_outside_safety_window(self):
        """A message from 23 hours 55 minutes (> 23h 50m) must be rejected to prevent policy strikes."""
        now = time.time()
        timestamp = now - (23 * 3600 + 55 * 60)
        assert is_within_messaging_window(timestamp) is False

    def test_message_at_25h_is_expired(self):
        """A message from 25 hours ago is strictly expired."""
        now = time.time()
        timestamp = now - (25 * 3600)
        assert is_within_messaging_window(timestamp) is False

    def test_invalid_or_zero_timestamp(self):
        """Zero or None timestamp returns False."""
        assert is_within_messaging_window(0) is False

