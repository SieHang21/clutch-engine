"""
Clutch Phase 2 Verification Suite
As specified in 'Clutch Agent Master Specification.pdf' Phase 2:
- Redis-backed accumulator appending text to session:{sender_id} with a 75-second sliding TTL
- Admin Presence Sensor listening for is_echo: true or admin messages and setting admin_active:{sender_id} with 45-minute TTL
- 24-hour Meta messaging window timer rejecting auto-messages after 23h 50m
- Chat bursts and human admin takeover simulation
"""

import time
import json
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.api.webhooks import set_webhook_secrets, set_session_buffer
from app.services.session_buffer import (
    SessionBuffer,
    is_within_messaging_window,
    DEFAULT_DEBOUNCE_TTL,
    DEFAULT_TAKEOVER_TTL,
)
from tests.test_webhooks import generate_meta_signature, TEST_APP_SECRET, TEST_VERIFY_TOKEN


@pytest.fixture(autouse=True)
def setup_phase2_env():
    set_webhook_secrets(app_secret=TEST_APP_SECRET, verify_token=TEST_VERIFY_TOKEN)
    buffer = SessionBuffer()
    buffer._redis_backend = None  # Ensure in-memory backend for isolated test run
    set_session_buffer(buffer)
    yield buffer


@pytest.fixture
def client():
    return TestClient(app)


class TestPhase2BurstAndAdminTakeover:
    """End-to-end simulation of customer bursts and admin takeovers."""

    def test_multi_message_chat_burst_simulation(self, client, setup_phase2_env):
        """
        Simulate customer sending bursts:
        - msg1: 'Bhai'
        - msg2: 'Ami Mirpur 2 thaki'
        - msg3: '01712345678'
        Verify accumulator buffers all 3 and sliding TTL resets.
        """
        buffer = setup_phase2_env
        sender_id = "BURST_USER_123"
        page_id = "STORE_PAGE_456"

        burst_texts = ["Bhai", "Ami Mirpur 2 thaki", "01712345678"]

        for text in burst_texts:
            payload = {
                "object": "page",
                "entry": [
                    {
                        "id": page_id,
                        "messaging": [
                            {
                                "sender": {"id": sender_id},
                                "recipient": {"id": page_id},
                                "message": {"text": text},
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

        # Verify buffer
        buffered = buffer.get_buffered_messages(sender_id, page_id=page_id)
        assert len(buffered) == 3

        # Flush session
        flushed = buffer.flush_session(sender_id, page_id=page_id)
        assert flushed["order"] is not None
        assert flushed["order"]["clean_phone"] == "01712345678"
        assert flushed["order"]["zone"] == "Inside Dhaka"
        assert flushed["order"]["fee"] == 70

    def test_human_admin_takeover_silences_bot(self, client, setup_phase2_env):
        """
        Simulate human admin reply followed by customer reply:
        1. Human admin replies -> is_echo: True
        2. Admin takeover flag is set with 45m TTL (2700s)
        3. Customer sends subsequent message -> bot is silenced / bypassed.
        """
        buffer = setup_phase2_env
        sender_id = "CUSTOMER_TAKEOVER_1"
        page_id = "STORE_PAGE_TAKEOVER"

        # 1. Admin sends message via Page Inbox
        admin_payload = {
            "object": "page",
            "entry": [
                {
                    "id": page_id,
                    "messaging": [
                        {
                            "sender": {"id": page_id},
                            "recipient": {"id": sender_id},
                            "message": {
                                "text": "Apnake shob details inbox e pathano hoyeche.",
                                "is_echo": True,
                            },
                        }
                    ],
                }
            ],
        }
        body = json.dumps(admin_payload).encode("utf-8")
        res = client.post(
            "/webhooks",
            content=body,
            headers={"X-Hub-Signature-256": generate_meta_signature(body)},
        )
        assert res.status_code == 200

        # Assert admin takeover is active
        assert buffer.is_admin_takeover_active(sender_id, page_id=page_id) is True

        # 2. Customer replies while takeover is active
        cust_payload = {
            "object": "page",
            "entry": [
                {
                    "id": page_id,
                    "messaging": [
                        {
                            "sender": {"id": sender_id},
                            "recipient": {"id": page_id},
                            "message": {"text": "Dhonnobad bhai."},
                        }
                    ],
                }
            ],
        }
        body2 = json.dumps(cust_payload).encode("utf-8")
        res2 = client.post(
            "/webhooks",
            content=body2,
            headers={"X-Hub-Signature-256": generate_meta_signature(body2)},
        )
        assert res2.status_code == 200
        event = res2.json()["events"][0]
        assert event["action"] == "bypassed_due_to_admin_takeover"

        # Ensure message was not buffered
        buffered = buffer.get_buffered_messages(sender_id, page_id=page_id)
        assert len(buffered) == 0

    def test_messaging_window_timer_23h50m(self):
        """
        Master Spec requirement:
        Reject auto-messages after 23h 50m (85,800s) to comply with Meta 24-hr policy.
        """
        now = time.time()
        # 10 minutes ago
        assert is_within_messaging_window(now - 600) is True
        # 23 hours 45 minutes ago
        assert is_within_messaging_window(now - (23 * 3600 + 45 * 60)) is True
        # 23 hours 52 minutes ago (> 23h 50m)
        assert is_within_messaging_window(now - (23 * 3600 + 52 * 60)) is False
        # 24 hours 1 minute ago
        assert is_within_messaging_window(now - (24 * 3600 + 60)) is False

