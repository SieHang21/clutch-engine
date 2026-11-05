"""
Unit and Integration Tests for Clutch Phase 4:
Merchant Live Dashboard, Manual Order Review/Override UI, and Customer Dispatch Notifications
Covers:
  - Dashboard order pagination (pages, total_pages, boundary checks)
  - Filtering by status (PENDING_CONFIRMATION, PROCESSING, COURIER_DISPATCHED, ADMIN_HELD)
  - Search across customer phone, consignment ID, tracking code, and order ID
  - Merchant manual overrides (zone, fee, COD total, address)
  - Administrative hold (ADMIN_HELD) and state transition integrity
  - Automated approval pipeline (Steadfast Courier dispatch + PDF invoice creation)
  - Bilingual notification summary generation (English + Bengali)
  - Interactive button payload validation for WhatsApp Cloud API & Meta Messenger
  - Customer single-tap postback handling (CONFIRM_ORDER, MODIFY_ADDRESS, CANCEL_ORDER)
"""

import os
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.order import (
    Order,
    OrderItem,
    OrderStatus,
    DeliveryZone,
)
from app.services.order_store import OrderStore
from app.api.dashboard import set_order_store
from app.services.courier_steadfast import SteadfastCourierService
from app.services.customer_notifier import (
    CustomerNotifierService,
    format_bilingual_order_summary,
    ACTION_CONFIRM,
    ACTION_MODIFY,
    ACTION_CANCEL,
)


@pytest.fixture
def isolated_order_store():
    """Isolated in-memory order repository for each test."""
    store = OrderStore()
    set_order_store(store)
    return store


@pytest.fixture
def mock_courier():
    return SteadfastCourierService(is_mock=True)


@pytest.fixture
def client(isolated_order_store):
    return TestClient(app)


def create_sample_order(
    order_id: str,
    name: str = "Test Customer",
    phone: str = "01711223344",
    address: str = "Mirpur 10, Dhaka",
    zone: DeliveryZone = DeliveryZone.CORE,
    fee: float = 70.0,
    cod: float = 1270.0,
    status: OrderStatus = OrderStatus.PENDING_CONFIRMATION,
    consignment_id: str = None,
    tracking_code: str = None,
) -> Order:
    return Order(
        order_id=order_id,
        recipient_name=name,
        recipient_phone=phone,
        recipient_address=address,
        delivery_zone=zone,
        delivery_fee=fee,
        cod_amount=cod,
        status=status,
        consignment_id=consignment_id,
        tracking_code=tracking_code,
        items=[
            OrderItem(sku="TEST-SKU-1", name="Panjabi Classic", qty=1, price=1200.0),
        ],
    )


# =========================================================================
# 1. Dashboard Order Query, Filter & Search Tests
# =========================================================================
class TestDashboardOrderQueries:
    """Tests for GET /api/orders pagination, status filtering, and multi-field search."""

    def test_pagination_and_total_pages(self, client, isolated_order_store):
        """Seed 25 orders and test pagination across page 1, 2, and 3."""
        for i in range(1, 26):
            order = create_sample_order(
                order_id=f"ORD-PAG-{i:03d}",
                phone=f"017100000{i:02d}",
            )
            isolated_order_store.save(order)

        # Page 1 (10 items)
        res1 = client.get("/api/orders", params={"page": 1, "page_size": 10})
        assert res1.status_code == 200
        data1 = res1.json()
        assert data1["total"] == 25
        assert data1["page"] == 1
        assert data1["page_size"] == 10
        assert data1["total_pages"] == 3
        assert len(data1["orders"]) == 10

        # Page 2 (10 items)
        res2 = client.get("/api/orders", params={"page": 2, "page_size": 10})
        assert res2.status_code == 200
        data2 = res2.json()
        assert len(data2["orders"]) == 10

        # Page 3 (remaining 5 items)
        res3 = client.get("/api/orders", params={"page": 3, "page_size": 10})
        assert res3.status_code == 200
        data3 = res3.json()
        assert len(data3["orders"]) == 5

    def test_status_filter(self, client, isolated_order_store):
        """Filter orders by specific lifecycle status."""
        isolated_order_store.save(create_sample_order("ORD-S1", status=OrderStatus.PENDING_CONFIRMATION))
        isolated_order_store.save(create_sample_order("ORD-S2", status=OrderStatus.PROCESSING))
        isolated_order_store.save(create_sample_order("ORD-S3", status=OrderStatus.COURIER_DISPATCHED))
        isolated_order_store.save(create_sample_order("ORD-S4", status=OrderStatus.ADMIN_HELD))

        # Query only ADMIN_HELD
        res = client.get("/api/orders", params={"status": "ADMIN_HELD"})
        assert res.status_code == 200
        data = res.json()
        assert data["total"] == 1
        assert data["orders"][0]["order_id"] == "ORD-S4"
        assert data["orders"][0]["status"] == "ADMIN_HELD"

        # Query only COURIER_DISPATCHED
        res_disp = client.get("/api/orders", params={"status": "COURIER_DISPATCHED"})
        assert res_disp.status_code == 200
        assert res_disp.json()["total"] == 1
        assert res_disp.json()["orders"][0]["order_id"] == "ORD-S3"

    def test_search_by_phone(self, client, isolated_order_store):
        """Search query matching recipient mobile number."""
        isolated_order_store.save(create_sample_order("ORD-P1", phone="01711998877"))
        isolated_order_store.save(create_sample_order("ORD-P2", phone="01822334455"))

        res = client.get("/api/orders", params={"search": "01711998877"})
        assert res.status_code == 200
        data = res.json()
        assert data["total"] == 1
        assert data["orders"][0]["recipient_phone"] == "01711998877"

    def test_search_by_consignment_and_tracking(self, client, isolated_order_store):
        """Search query matching consignment_id or tracking_code."""
        isolated_order_store.save(create_sample_order(
            "ORD-C1",
            consignment_id="SF-CID-889900",
            tracking_code="SF88990011",
            status=OrderStatus.COURIER_DISPATCHED,
        ))
        isolated_order_store.save(create_sample_order("ORD-C2", phone="01900000000"))

        # Search by consignment ID
        res_cid = client.get("/api/orders", params={"search": "SF-CID-889900"})
        assert res_cid.status_code == 200
        assert res_cid.json()["total"] == 1
        assert res_cid.json()["orders"][0]["order_id"] == "ORD-C1"

        # Search by tracking code substring
        res_track = client.get("/api/orders", params={"search": "88990011"})
        assert res_track.status_code == 200
        assert res_track.json()["total"] == 1


# =========================================================================
# 2. Manual Order Review Override & Hold Tests
# =========================================================================
class TestOrderReviewOverridesAndHold:
    """Tests for POST /api/orders/{id}/override and POST /api/orders/{id}/hold."""

    def test_manual_override_zone_and_fee(self, client, isolated_order_store):
        """Merchant changes delivery zone from CORE to SUBURB; delivery_fee automatically updates to ৳100."""
        order = create_sample_order(
            "ORD-OVR-01",
            zone=DeliveryZone.CORE,
            fee=70.0,
            cod=1270.0,
        )
        isolated_order_store.save(order)

        payload = {
            "delivery_zone": "SUBURB",
            "recipient_address": "Savar Bus Stand, Savar, Dhaka",
        }
        res = client.post(f"/api/orders/{order.order_id}/override", json=payload)
        assert res.status_code == 200
        updated = res.json()

        assert updated["delivery_zone"] == "SUBURB"
        assert updated["delivery_fee"] == 100.0  # Auto-updated suburb rate
        assert updated["recipient_address"] == "Savar Bus Stand, Savar, Dhaka"
        assert updated["cod_amount"] == 1300.0   # Recalculated (1200 items + 100 fee)

    def test_manual_override_explicit_custom_fee_and_cod(self, client, isolated_order_store):
        """Merchant applies promotional delivery discount (৳40) and custom COD amount."""
        order = create_sample_order("ORD-OVR-02", cod=1500.0)
        isolated_order_store.save(order)

        payload = {
            "delivery_fee": 40.0,
            "cod_amount": 1400.0,
        }
        res = client.post(f"/api/orders/{order.order_id}/override", json=payload)
        assert res.status_code == 200
        updated = res.json()
        assert updated["delivery_fee"] == 40.0
        assert updated["cod_amount"] == 1400.0

    def test_override_nonexistent_order_returns_404(self, client):
        """Overriding unknown order returns 404."""
        res = client.post("/api/orders/ORD-DOES-NOT-EXIST/override", json={"delivery_fee": 50.0})
        assert res.status_code == 404

    def test_override_terminal_state_rejected(self, client, isolated_order_store):
        """Editing an order that is already DELIVERED or CANCELLED is rejected with 400."""
        order = create_sample_order("ORD-TERM-01", status=OrderStatus.DELIVERED)
        isolated_order_store.save(order)

        res = client.post(f"/api/orders/{order.order_id}/override", json={"cod_amount": 999.0})
        assert res.status_code == 400
        assert "terminal state" in res.json()["detail"]

    def test_administrative_hold(self, client, isolated_order_store):
        """Place order on ADMIN_HELD with reason."""
        order = create_sample_order("ORD-HOLD-01", status=OrderStatus.PENDING_CONFIRMATION)
        isolated_order_store.save(order)

        res = client.post(
            f"/api/orders/{order.order_id}/hold",
            json={"reason": "Customer requested size verification"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["order"]["status"] == "ADMIN_HELD"
        assert data["reason"] == "Customer requested size verification"

        # Verify persisted
        reloaded = isolated_order_store.get_by_id(order.order_id)
        assert reloaded.status == OrderStatus.ADMIN_HELD


# =========================================================================
# 3. Order Approval & Automated Courier Dispatch Pipeline
# =========================================================================
class TestOrderApprovalPipeline:
    """Tests for POST /api/orders/{id}/approve dispatching to Steadfast & creating PDF invoice."""

    def test_successful_approval_and_dispatch(self, client, isolated_order_store):
        """Approving order triggers Steadfast consignment creation, invoice generation, and status dispatch."""
        order = create_sample_order(
            "ORD-APP-001",
            name="Mushfiqur Rahim",
            phone="01712345678",
            address="Road 10, Dhanmondi, Dhaka",
            status=OrderStatus.PENDING_CONFIRMATION,
        )
        isolated_order_store.save(order)

        res = client.post(f"/api/orders/{order.order_id}/approve")
        assert res.status_code == 200
        data = res.json()

        assert data["status"] == "success"
        assert data["order"]["status"] == "COURIER_DISPATCHED"
        assert data["consignment_id"] is not None
        assert data["consignment_id"].startswith("SF-CID-")
        assert data["tracking_code"] is not None
        assert data["tracking_code"].startswith("SF")
        assert data["invoice_url"] is not None
        assert os.path.exists(data["invoice_url"])

        # Clean up generated test invoice
        if os.path.exists(data["invoice_url"]):
            os.remove(data["invoice_url"])

    def test_approve_already_dispatched_order(self, client, isolated_order_store):
        """Approving an order that is already COURIER_DISPATCHED returns status already_dispatched idempotently."""
        order = create_sample_order(
            "ORD-APP-DISP",
            status=OrderStatus.COURIER_DISPATCHED,
            consignment_id="SF-CID-112233",
            tracking_code="SF11223344",
        )
        isolated_order_store.save(order)

        res = client.post(f"/api/orders/{order.order_id}/approve")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "already_dispatched"
        assert data["consignment_id"] == "SF-CID-112233"


# =========================================================================
# 4. Bilingual Notification Summary & Interactive Button Templates
# =========================================================================
class TestCustomerNotifierTemplates:
    """Tests for bilingual English+Bengali summaries and WhatsApp/Messenger button payloads."""

    def test_bilingual_order_summary_content(self):
        """Summary must contain English and Bengali labels, items, address, zone, and COD amount."""
        order = create_sample_order(
            "ORD-NOTIF-01",
            name="Zubair",
            phone="01812345678",
            address="Uttara Sector 4, Road 2",
            zone=DeliveryZone.CORE,
            cod=1270.0,
            tracking_code="SF55443322",
        )
        summary = format_bilingual_order_summary(order, brand_name="Clutch Style")

        # Bilingual indicators
        assert "Order Confirmation / অর্ডার নিশ্চিতকরণ" in summary
        assert "Items / পণ্য তালিকা:" in summary
        assert "Delivery Address / ডেলিভারি ঠিকানা:" in summary
        assert "Total COD / সর্বমোট ক্যাশ অন ডেলিভারি: ৳1270.00" in summary
        assert "Steadfast Courier" in summary
        assert "https://steadfast.com.bd/tracking/SF55443322" in summary

    def test_whatsapp_interactive_button_payload_structure(self):
        """Validate WhatsApp Cloud API interactive button specification."""
        notifier = CustomerNotifierService(is_mock=True)
        order = create_sample_order("ORD-WA-01", phone="01712345678")

        payload = notifier.build_whatsapp_interactive_payload(order)

        assert payload["messaging_product"] == "whatsapp"
        assert payload["to"] == "8801712345678"  # Internationalized
        assert payload["type"] == "interactive"

        interactive = payload["interactive"]
        assert interactive["type"] == "button"
        assert "body" in interactive
        assert "action" in interactive

        buttons = interactive["action"]["buttons"]
        assert len(buttons) == 3

        button_ids = [b["reply"]["id"] for b in buttons]
        assert f"{ACTION_CONFIRM}:{order.order_id}" in button_ids
        assert f"{ACTION_MODIFY}:{order.order_id}" in button_ids
        assert f"{ACTION_CANCEL}:{order.order_id}" in button_ids

    def test_messenger_quick_reply_payload_structure(self):
        """Validate Meta Messenger Graph API quick reply specification."""
        notifier = CustomerNotifierService(is_mock=True)
        order = create_sample_order("ORD-FB-01")

        payload = notifier.build_messenger_quick_reply_payload(order, recipient_id="FB_USER_9988")

        assert payload["recipient"]["id"] == "FB_USER_9988"
        message = payload["message"]
        quick_replies = message["quick_replies"]
        assert len(quick_replies) == 3

        reply_payloads = [qr["payload"] for qr in quick_replies]
        assert f"{ACTION_CONFIRM}:{order.order_id}" in reply_payloads
        assert f"{ACTION_MODIFY}:{order.order_id}" in reply_payloads
        assert f"{ACTION_CANCEL}:{order.order_id}" in reply_payloads


# =========================================================================
# 5. Customer Postback Execution Tests
# =========================================================================
class TestCustomerPostbackHandling:
    """Tests for single-tap customer button postbacks triggering approval, hold, or cancellation."""

    @pytest.mark.anyio
    async def test_confirm_postback_triggers_approval_and_courier_dispatch(self, isolated_order_store):
        """Tapping 'Confirm Order' automatically executes courier dispatch and generates invoice."""
        notifier = CustomerNotifierService(
            order_store=isolated_order_store,
            courier_service=SteadfastCourierService(is_mock=True),
            is_mock=True,
        )
        order = create_sample_order("ORD-POST-CONFIRM", status=OrderStatus.PENDING_CONFIRMATION)
        isolated_order_store.save(order)

        res = await notifier.handle_postback(f"{ACTION_CONFIRM}:{order.order_id}")
        assert res["action"] == ACTION_CONFIRM
        assert res["status"] == "COURIER_DISPATCHED"
        assert res["consignment_id"] is not None
        assert res["tracking_code"] is not None
        assert res["invoice_url"] is not None
        assert os.path.exists(res["invoice_url"])

        # Verify persisted state
        reloaded = isolated_order_store.get_by_id(order.order_id)
        assert reloaded.status == OrderStatus.COURIER_DISPATCHED
        assert reloaded.consignment_id == res["consignment_id"]

        if os.path.exists(res["invoice_url"]):
            os.remove(res["invoice_url"])

    @pytest.mark.anyio
    async def test_modify_address_postback_holds_order(self, isolated_order_store):
        """Tapping 'Modify Address' moves order to ADMIN_HELD for merchant intervention."""
        notifier = CustomerNotifierService(order_store=isolated_order_store, is_mock=True)
        order = create_sample_order("ORD-POST-MOD", status=OrderStatus.PENDING_CONFIRMATION)
        isolated_order_store.save(order)

        res = await notifier.handle_postback(f"{ACTION_MODIFY}:{order.order_id}")
        assert res["action"] == ACTION_MODIFY
        assert res["status"] == "ADMIN_HELD"

        reloaded = isolated_order_store.get_by_id(order.order_id)
        assert reloaded.status == OrderStatus.ADMIN_HELD

    @pytest.mark.anyio
    async def test_cancel_postback_cancels_order(self, isolated_order_store):
        """Tapping 'Cancel' marks order as CANCELLED."""
        notifier = CustomerNotifierService(order_store=isolated_order_store, is_mock=True)
        order = create_sample_order("ORD-POST-CANCEL", status=OrderStatus.PENDING_CONFIRMATION)
        isolated_order_store.save(order)

        res = await notifier.handle_postback(f"{ACTION_CANCEL}:{order.order_id}")
        assert res["action"] == ACTION_CANCEL
        assert res["status"] == "CANCELLED"

        reloaded = isolated_order_store.get_by_id(order.order_id)
        assert reloaded.status == OrderStatus.CANCELLED

    @pytest.mark.anyio
    async def test_malformed_postback_raises_error(self, isolated_order_store):
        """Malformed postback payload without colon delimiter raises ValueError."""
        notifier = CustomerNotifierService(order_store=isolated_order_store, is_mock=True)
        with pytest.raises(ValueError):
            await notifier.handle_postback("INVALID_PAYLOAD_WITHOUT_COLON")

