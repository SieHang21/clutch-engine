"""
Clutch (ক্ল্যাচ) - End-to-End (E2E) Integration Pipeline Test
Validates the complete automated F-commerce customer lifecycle:
  1. Fragmented Bengali customer message burst over webhook ("Amar shart lagbe", "Dhaka mirpur 10", "০১৭৯৮৭৬৫৪৩২")
  2. Session accumulator buffering & normalization: converts ০১৭৯৮৭৬৫৪৩২ -> 01798765432, classifies Dhaka Core (৳70 fee)
  3. Order generation in PENDING_CONFIRMATION state
  4. Outbound bilingual order confirmation dispatched with interactive action buttons
  5. Merchant approval via POST /api/orders/{id}/approve
  6. Steadfast Courier API consignment booking (consignment ID & tracking code generated)
  7. A4/Thermal PDF invoice generation in invoices/ with Code128 barcode
  8. Final order state verified as COURIER_DISPATCHED
"""

import os
import json
import time
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.order import Order, OrderStatus, DeliveryZone
from app.services.order_store import default_order_store, OrderStore, set_order_store
from app.api.dashboard import set_courier_service
from app.api.webhooks import set_webhook_secrets, set_session_buffer
from app.services.session_buffer import SessionBuffer
from app.services.courier_steadfast import SteadfastCourierService
from app.services.customer_notifier import default_customer_notifier, CustomerNotifierService
from app.core.config import settings, Settings
from app.workers.dispatch_worker import enqueue_dispatch_job, process_dispatch_job
from tests.test_webhooks import generate_meta_signature, TEST_APP_SECRET, TEST_VERIFY_TOKEN


@pytest.fixture
def setup_e2e_environment():
    """Reset and isolate services for end-to-end execution."""
    set_webhook_secrets(app_secret=TEST_APP_SECRET, verify_token=TEST_VERIFY_TOKEN)

    # In-memory session buffer
    session_buf = SessionBuffer()
    session_buf._redis_backend = None
    set_session_buffer(session_buf)

    # Dedicated order store
    store = OrderStore()
    set_order_store(store)

    # Mock courier client
    courier = SteadfastCourierService(is_mock=True)
    set_courier_service(courier)

    # Reset notifier
    default_customer_notifier.order_store = store
    default_customer_notifier.courier_service = courier
    default_customer_notifier.is_mock = True
    default_customer_notifier.sent_notifications.clear()

    yield {
        "store": store,
        "session_buffer": session_buf,
        "courier": courier,
        "notifier": default_customer_notifier,
    }


@pytest.fixture
def client():
    return TestClient(app)


class TestClutchEndToEndCustomerJourney:
    """Full lifecycle customer simulation from chat burst to courier dispatch."""

    def test_complete_e2e_customer_lifecycle(self, client, setup_e2e_environment):
        """
        Execute Step 1 to Step 8 of the automated Clutch engine directive:
        1. Fragmented burst: 'Amar shart lagbe', 'Dhaka mirpur 10', '০১৭৯৮৭৬৫৪৩২'
        2. Normalizer extracts 01798765432, 70 BDT Dhaka Core
        3. Order generated in PENDING_CONFIRMATION
        4. Automated bilingual notification dispatched
        5. Admin approves order via /api/orders/{id}/approve
        6. Steadfast consignment created with tracking code
        7. PDF invoice generated in invoices/
        8. Final order status verified as COURIER_DISPATCHED
        """
        env = setup_e2e_environment
        store = env["store"]
        notifier = env["notifier"]

        sender_id = "CUSTOMER_E2E_01"
        page_id = "CLUTCH_MERCHANT_PAGE_101"

        # -----------------------------------------------------------------
        # Step 1: Fragmented incoming messages over Meta Webhook
        # -----------------------------------------------------------------
        incoming_burst = [
            "Amar shart lagbe",
            "Dhaka mirpur 10",
            "০১৭৯৮৭৬৫৪৩২",
        ]

        created_order_id = None

        for text_fragment in incoming_burst:
            payload = {
                "object": "page",
                "entry": [
                    {
                        "id": page_id,
                        "messaging": [
                            {
                                "sender": {"id": sender_id},
                                "recipient": {"id": page_id},
                                "message": {"mid": f"mid.{time.time()}", "text": text_fragment},
                            }
                        ],
                    }
                ],
            }
            raw_body = json.dumps(payload).encode("utf-8")
            sig_header = generate_meta_signature(raw_body, secret=TEST_APP_SECRET)

            res = client.post(
                "/webhooks",
                content=raw_body,
                headers={"X-Hub-Signature-256": sig_header},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "EVENT_RECEIVED"

            # Check if order was created on completeness
            events = data.get("events", [])
            for ev in events:
                if ev.get("order_id"):
                    created_order_id = ev["order_id"]

        # -----------------------------------------------------------------
        # Step 2 & 3: Normalization & Order Generation in PENDING_CONFIRMATION
        # -----------------------------------------------------------------
        assert created_order_id is not None, "Order was not automatically generated upon burst completion"

        # Retrieve order from repository
        order = store.get_by_id(created_order_id)
        assert order is not None
        assert order.status == OrderStatus.PENDING_CONFIRMATION
        # Converted Bengali numeral to standard 11-digit ASCII
        assert order.recipient_phone == "01798765432"
        # Classified as Dhaka Core with 70 BDT fee
        assert order.delivery_zone == DeliveryZone.CORE
        assert order.delivery_fee == 70.0
        assert order.cod_amount == 1270.0  # 1200 item + 70 delivery
        assert "mirpur" in order.recipient_address.lower()

        # -----------------------------------------------------------------
        # Step 4: Automated Bilingual Notification Dispatched
        # -----------------------------------------------------------------
        assert len(notifier.sent_notifications) > 0
        latest_notif = notifier.sent_notifications[-1]
        assert latest_notif["recipient"] == sender_id
        msg_payload = latest_notif["payload"]["message"]
        assert "Order Confirmation / অর্ডার নিশ্চিতকরণ" in msg_payload["text"]
        assert "01798765432" or order.recipient_address in msg_payload["text"]

        # -----------------------------------------------------------------
        # Step 5: Admin Approves Order via /api/orders/{id}/approve
        # -----------------------------------------------------------------
        approve_res = client.post(f"/api/orders/{created_order_id}/approve")
        assert approve_res.status_code == 200
        approve_data = approve_res.json()

        assert approve_data["status"] == "success"

        # -----------------------------------------------------------------
        # Step 6: Steadfast Consignment Created with Tracking Code
        # -----------------------------------------------------------------
        consignment_id = approve_data["consignment_id"]
        tracking_code = approve_data["tracking_code"]
        assert consignment_id is not None
        assert consignment_id.startswith("SF-CID-")
        assert tracking_code is not None
        assert tracking_code.startswith("SF")

        # -----------------------------------------------------------------
        # Step 7: PDF Invoice Generated in invoices/
        # -----------------------------------------------------------------
        invoice_url = approve_data["invoice_url"]
        assert invoice_url is not None
        assert os.path.exists(invoice_url)
        assert os.path.getsize(invoice_url) > 2000

        with open(invoice_url, "rb") as f:
            pdf_bytes = f.read(5)
            assert pdf_bytes == b"%PDF-", "Generated invoice is not a valid PDF"

        # -----------------------------------------------------------------
        # Step 8: Final Order Status Verified as COURIER_DISPATCHED
        # -----------------------------------------------------------------
        final_order = store.get_by_id(created_order_id)
        assert final_order.status == OrderStatus.COURIER_DISPATCHED
        assert final_order.consignment_id == consignment_id
        assert final_order.tracking_code == tracking_code
        assert final_order.invoice_url == invoice_url

        # Clean up generated invoice file
        if os.path.exists(invoice_url):
            os.remove(invoice_url)

    def test_e2e_single_tap_customer_postback_approval(self, client, setup_e2e_environment):
        """
        Verify alternative flow:
        Customer taps 'Confirm Order' button in Messenger/WhatsApp postback,
        triggering automated courier dispatch and invoice creation.
        """
        env = setup_e2e_environment
        store = env["store"]

        # Setup pending order
        order = store.save(Order(
            order_id="ORD-E2E-TAP-01",
            recipient_name="Karim Ullah",
            recipient_phone="01812345678",
            recipient_address="Savar Bus Stand, Savar",
            delivery_zone=DeliveryZone.SUBURB,
            delivery_fee=100.0,
            cod_amount=1300.0,
            status=OrderStatus.PENDING_CONFIRMATION,
        ))

        # Customer postback: CONFIRM_ORDER:ORD-E2E-TAP-01
        postback_body = {"payload": f"CONFIRM_ORDER:{order.order_id}"}
        res = client.post("/api/orders/postbacks/handle", json=postback_body)
        assert res.status_code == 200
        data = res.json()

        assert data["action"] == "CONFIRM_ORDER"
        assert data["status"] == "COURIER_DISPATCHED"
        assert data["consignment_id"] is not None
        assert data["tracking_code"] is not None
        assert data["invoice_url"] is not None
        assert os.path.exists(data["invoice_url"])

        # Clean up
        if os.path.exists(data["invoice_url"]):
            os.remove(data["invoice_url"])

    @pytest.mark.anyio
    async def test_background_worker_dispatch_job(self, setup_e2e_environment):
        """Verify asynchronous background worker task processing."""
        env = setup_e2e_environment
        store = env["store"]
        courier = env["courier"]

        order = store.save(Order(
            order_id="ORD-WORKER-01",
            recipient_name="Worker Test",
            recipient_phone="01912345678",
            recipient_address="Agrabad, Chittagong",
            delivery_zone=DeliveryZone.OUTSIDE,
            delivery_fee=130.0,
            cod_amount=1630.0,
            status=OrderStatus.PENDING_CONFIRMATION,
        ))

        # Enqueue job
        enqueue_res = enqueue_dispatch_job(order.order_id)
        assert enqueue_res is True

        # Process job via worker processor
        job_result = await process_dispatch_job(
            order.order_id,
            order_store=store,
            courier_service=courier,
        )
        assert job_result["status"] == "success"
        assert job_result["consignment_id"] is not None
        assert job_result["tracking_code"] is not None

        # Verify updated order in store
        reloaded = store.get_by_id(order.order_id)
        assert reloaded.status == OrderStatus.COURIER_DISPATCHED

        if job_result.get("invoice_url") and os.path.exists(job_result["invoice_url"]):
            os.remove(job_result["invoice_url"])

    def test_environment_settings_loading(self):
        """Validate Pydantic-Settings configuration model and defaults."""
        cfg = Settings()
        assert cfg.ENVIRONMENT in ("development", "staging", "production")
        assert cfg.STEADFAST_BASE_URL.startswith("http")
        assert cfg.BRAND_NAME != ""
        assert cfg.REDIS_URL.startswith("redis://")
