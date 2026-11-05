"""
Unit Tests for Clutch Phase 3:
Order Lifecycle State Machine, Steadfast Courier API Engine, and PDF Invoice Generation
Covers:
  - Order state machine transitions (linear path, hold/cancel branches)
  - Invalid state jumps & terminal state protection (InvalidStateTransitionError)
  - Phone validation & Bengali numeral normalization on Order model
  - Steadfast Courier API client (mock sandbox mode, payload serialization, tracking status)
  - PDF Invoice Generator (ReportLab A4 layout, Code128 barcode, non-empty %PDF- verification)
"""

import os
import pytest
from pydantic import ValidationError

from app.models.order import (
    Order,
    OrderItem,
    OrderStatus,
    DeliveryZone,
    InvalidStateTransitionError,
    DELIVERY_ZONE_FEES,
)
from app.services.courier_steadfast import (
    SteadfastCourierService,
    SteadfastAPIError,
)
from app.services.invoice_generator import (
    generate_invoice_pdf,
    DEFAULT_INVOICE_DIR,
)


# =========================================================================
# 1. Order Model & Phone Validation Tests
# =========================================================================
class TestOrderModelValidation:
    """Tests for Order schema validation and normalization."""

    def test_valid_order_creation(self):
        """Standard valid order with 11-digit BD mobile number."""
        order = Order(
            recipient_name="Rahim Khan",
            recipient_phone="01712345678",
            recipient_address="House 12, Road 5, Mirpur 10, Dhaka",
            delivery_zone=DeliveryZone.CORE,
            delivery_fee=70.0,
            cod_amount=1570.0,
            items=[
                OrderItem(sku="PJB-BLK-L", name="Black Panjabi", qty=1, price=1500.0),
            ],
        )
        assert order.recipient_phone == "01712345678"
        assert order.status == OrderStatus.PENDING_CONFIRMATION
        assert order.delivery_zone == DeliveryZone.CORE
        assert order.delivery_fee == 70.0
        assert order.cod_amount == 1570.0
        assert order.calculate_items_subtotal() == 1500.0
        assert order.calculate_expected_total() == 1570.0

    @pytest.mark.parametrize(
        "dirty_phone, expected_clean",
        [
            ("+8801712345678", "01712345678"),
            ("8801812345678", "01812345678"),
            ("01912-345678", "01912345678"),
            ("+88-01312345678", "01312345678"),
            ("০১৭১২-৩৪৫৬৭৮", "01712345678"),
            ("+৮৮০১৮১২৩৪৫৬৭৮", "01812345678"),
        ],
    )
    def test_phone_normalization_during_order_creation(self, dirty_phone, expected_clean):
        """Dirty phone strings with country prefixes and Bengali numerals normalize automatically."""
        order = Order(
            recipient_name="Tanvir",
            recipient_phone=dirty_phone,
            recipient_address="Dhanmondi, Dhaka",
            cod_amount=500.0,
        )
        assert order.recipient_phone == expected_clean

    @pytest.mark.parametrize(
        "invalid_phone",
        [
            "01012345678",   # Invalid operator 010
            "01112345678",   # Discontinued Citycell 011
            "01212345678",   # Invalid operator 012
            "017123456",     # Too short
            "017123456789",  # Too long (12 digits)
            "invalid_text",  # Non-numeric
            "",              # Empty string
        ],
    )
    def test_invalid_phone_rejected(self, invalid_phone):
        """Invalid phone formats must fail Pydantic validation."""
        with pytest.raises(ValidationError):
            Order(
                recipient_name="Test",
                recipient_phone=invalid_phone,
                recipient_address="Mirpur, Dhaka",
                cod_amount=500.0,
            )

    @pytest.mark.parametrize(
        "zone_input, expected_enum",
        [
            ("CORE", DeliveryZone.CORE),
            ("Inside Dhaka", DeliveryZone.CORE),
            ("Dhaka Core", DeliveryZone.CORE),
            ("SUBURB", DeliveryZone.SUBURB),
            ("Suburbs", DeliveryZone.SUBURB),
            ("Dhaka Suburbs", DeliveryZone.SUBURB),
            ("OUTSIDE", DeliveryZone.OUTSIDE),
            ("Outside Dhaka", DeliveryZone.OUTSIDE),
        ],
    )
    def test_flexible_delivery_zone_parsing(self, zone_input, expected_enum):
        """Flexible delivery zone strings map to DeliveryZone enum."""
        order = Order(
            recipient_name="Test",
            recipient_phone="01712345678",
            recipient_address="Address",
            delivery_zone=zone_input,
            cod_amount=500.0,
        )
        assert order.delivery_zone == expected_enum


# =========================================================================
# 2. State Machine Transition Tests
# =========================================================================
class TestOrderStateMachine:
    """Tests for order lifecycle state transitions and illegal leap prevention."""

    def test_full_linear_lifecycle(self):
        """PENDING_CONFIRMATION -> PROCESSING -> COURIER_DISPATCHED -> DELIVERED."""
        order = Order(
            recipient_name="Customer A",
            recipient_phone="01712345678",
            recipient_address="Mirpur 10",
            cod_amount=1000.0,
        )
        assert order.status == OrderStatus.PENDING_CONFIRMATION

        # 1. Confirm order
        order.transition_to(OrderStatus.PROCESSING)
        assert order.status == OrderStatus.PROCESSING
        assert order.updated_at is not None

        # 2. Dispatch with courier
        order.transition_to(OrderStatus.COURIER_DISPATCHED)
        assert order.status == OrderStatus.COURIER_DISPATCHED

        # 3. Successful customer delivery
        order.transition_to(OrderStatus.DELIVERED)
        assert order.status == OrderStatus.DELIVERED

    def test_admin_held_branch_and_recovery(self):
        """Order flagged for review (ADMIN_HELD) can be unheld back to PROCESSING or PENDING."""
        order = Order(
            recipient_name="Suspicious Customer",
            recipient_phone="01712345678",
            recipient_address="Unknown Area",
            cod_amount=1000.0,
        )
        # Hold order
        order.transition_to(OrderStatus.ADMIN_HELD)
        assert order.status == OrderStatus.ADMIN_HELD

        # Admin resolves issue and moves to PROCESSING
        order.transition_to(OrderStatus.PROCESSING)
        assert order.status == OrderStatus.PROCESSING

    def test_cancellation_branches(self):
        """Cancellation is valid from PENDING_CONFIRMATION, PROCESSING, or COURIER_DISPATCHED."""
        # Cancel from PENDING_CONFIRMATION
        o1 = Order(recipient_name="A", recipient_phone="01712345678", recipient_address="Dhaka", cod_amount=500.0)
        o1.transition_to(OrderStatus.CANCELLED)
        assert o1.status == OrderStatus.CANCELLED

        # Cancel from PROCESSING
        o2 = Order(recipient_name="B", recipient_phone="01712345678", recipient_address="Dhaka", cod_amount=500.0)
        o2.transition_to(OrderStatus.PROCESSING)
        o2.transition_to(OrderStatus.CANCELLED)
        assert o2.status == OrderStatus.CANCELLED

        # Cancel/Return from COURIER_DISPATCHED
        o3 = Order(recipient_name="C", recipient_phone="01712345678", recipient_address="Dhaka", cod_amount=500.0)
        o3.transition_to(OrderStatus.PROCESSING)
        o3.transition_to(OrderStatus.COURIER_DISPATCHED)
        o3.transition_to(OrderStatus.CANCELLED)
        assert o3.status == OrderStatus.CANCELLED

    def test_invalid_leap_from_pending_to_delivered(self):
        """Jumping from PENDING_CONFIRMATION directly to DELIVERED is prohibited."""
        order = Order(recipient_name="A", recipient_phone="01712345678", recipient_address="Dhaka", cod_amount=500.0)
        with pytest.raises(InvalidStateTransitionError):
            order.transition_to(OrderStatus.DELIVERED)

    def test_invalid_leap_from_pending_to_dispatched(self):
        """Jumping from PENDING_CONFIRMATION directly to COURIER_DISPATCHED is prohibited."""
        order = Order(recipient_name="A", recipient_phone="01712345678", recipient_address="Dhaka", cod_amount=500.0)
        with pytest.raises(InvalidStateTransitionError):
            order.transition_to(OrderStatus.COURIER_DISPATCHED)

    def test_delivered_terminal_state_cannot_mutate(self):
        """Once DELIVERED, no further state transitions are permitted."""
        order = Order(recipient_name="A", recipient_phone="01712345678", recipient_address="Dhaka", cod_amount=500.0)
        order.transition_to(OrderStatus.PROCESSING)
        order.transition_to(OrderStatus.COURIER_DISPATCHED)
        order.transition_to(OrderStatus.DELIVERED)

        # Attempt to reopen or cancel delivered order
        with pytest.raises(InvalidStateTransitionError):
            order.transition_to(OrderStatus.PROCESSING)

        with pytest.raises(InvalidStateTransitionError):
            order.transition_to(OrderStatus.CANCELLED)

    def test_cancelled_terminal_state_cannot_mutate(self):
        """Once CANCELLED, resurrecting the order is prohibited."""
        order = Order(recipient_name="A", recipient_phone="01712345678", recipient_address="Dhaka", cod_amount=500.0)
        order.transition_to(OrderStatus.CANCELLED)

        with pytest.raises(InvalidStateTransitionError):
            order.transition_to(OrderStatus.PROCESSING)

        with pytest.raises(InvalidStateTransitionError):
            order.transition_to(OrderStatus.COURIER_DISPATCHED)


# =========================================================================
# 3. Steadfast Courier Service Tests
# =========================================================================
class TestSteadfastCourierService:
    """Tests for Steadfast Courier API client (sandbox mock & payload mapping)."""

    @pytest.mark.anyio
    async def test_sandbox_mock_order_creation(self):
        """In mock/sandbox mode, create_order assigns consignment_id & tracking_code and advances status."""
        service = SteadfastCourierService(is_mock=True)
        order = Order(
            recipient_name="Rahim Khan",
            recipient_phone="01712345678",
            recipient_address="House 5, Road 2, Dhanmondi, Dhaka",
            cod_amount=1200.0,
            status=OrderStatus.PROCESSING,
        )

        response = await service.create_order(order)

        # Response verification
        assert response["status"] == 200
        assert "consignment" in response
        consignment = response["consignment"]
        assert consignment["recipient_phone"] == "01712345678"
        assert consignment["cod_amount"] == 1200.0

        # Model update verification
        assert order.consignment_id is not None
        assert order.consignment_id.startswith("SF-CID-")
        assert order.tracking_code is not None
        assert order.tracking_code.startswith("SF")
        assert order.status == OrderStatus.COURIER_DISPATCHED

    @pytest.mark.anyio
    async def test_auto_sandbox_on_test_credentials(self):
        """Credentials like 'test' or 'sandbox' automatically activate mock mode."""
        service = SteadfastCourierService(api_key="test", secret_key="test")
        assert service.is_mock is True

        order = Order(
            recipient_name="Karim",
            recipient_phone="01812345678",
            recipient_address="Uttara Sector 3, Dhaka",
            cod_amount=850.0,
            status=OrderStatus.PENDING_CONFIRMATION,
        )
        res = await service.create_order(order)
        assert res["status"] == 200
        assert order.status == OrderStatus.COURIER_DISPATCHED

    def test_serialize_order_payload(self):
        """Verify exact Steadfast API payload structure."""
        service = SteadfastCourierService(is_mock=True)
        order = Order(
            order_id="ORD-TEST-001",
            recipient_name="Tanvir Ahmed",
            recipient_phone="01912345678",
            recipient_address="Savar, Dhaka",
            cod_amount=1350.50,
        )
        payload = service.serialize_order_payload(order, note="Fragile")
        assert payload == {
            "invoice": "ORD-TEST-001",
            "recipient_name": "Tanvir Ahmed",
            "recipient_phone": "01912345678",
            "recipient_address": "Savar, Dhaka",
            "cod_amount": 1350.50,
            "note": "Fragile",
        }

    @pytest.mark.anyio
    async def test_get_delivery_status(self):
        """Verify tracking status polling."""
        service = SteadfastCourierService(is_mock=True)
        res = await service.get_delivery_status("SF-CID-998877")
        assert res["status"] == 200
        assert res["delivery_status"] == "in_review"
        assert res["consignment_id"] == "SF-CID-998877"

    @pytest.mark.anyio
    async def test_get_delivery_status_empty_cid_raises_error(self):
        """Empty consignment_id raises ValueError."""
        service = SteadfastCourierService(is_mock=True)
        with pytest.raises(ValueError):
            await service.get_delivery_status("")


# =========================================================================
# 4. ReportLab PDF Invoice Generator Tests
# =========================================================================
class TestInvoiceGenerator:
    """Tests for PDF invoice generation with Code128 barcode."""

    def test_successful_pdf_invoice_generation(self, tmp_path):
        """Generate a complete PDF invoice and verify it is created, non-empty, and has valid PDF header."""
        out_dir = str(tmp_path / "invoices")

        order = Order(
            order_id="ORD-INV-2026-01",
            recipient_name="Shafiqul Islam",
            recipient_phone="01711223344",
            recipient_address="Flat 4B, House 15, Road 8, Dhanmondi, Dhaka",
            delivery_zone=DeliveryZone.CORE,
            delivery_fee=70.0,
            cod_amount=3270.0,
            consignment_id="SF-CID-443322",
            tracking_code="SF88776655",
            status=OrderStatus.COURIER_DISPATCHED,
            items=[
                OrderItem(sku="HOOD-BLK-L", name="Cyber-Magenta Street Hoodie", qty=1, price=1800.0),
                OrderItem(sku="TEE-WHT-M", name="Graphic Streetwear T-Shirt", qty=2, price=700.0),
            ],
        )

        pdf_path = generate_invoice_pdf(
            order,
            output_dir=out_dir,
            brand_name="Clutch Apparel",
        )

        # 1. Verify file exists on disk
        assert os.path.exists(pdf_path)
        assert os.path.isabs(pdf_path)

        # 2. Verify non-empty and realistic file size (> 2KB)
        file_size = os.path.getsize(pdf_path)
        assert file_size > 2000, f"PDF file size too small: {file_size} bytes"

        # 3. Verify valid PDF magic header (%PDF-)
        with open(pdf_path, "rb") as f:
            header = f.read(5)
            assert header == b"%PDF-", "File is not a valid PDF document"

        # 4. Verify order model invoice_url updated
        assert order.invoice_url == pdf_path

    def test_invoice_generation_with_suburb_zone(self, tmp_path):
        """Invoice generation with Suburb zone charge (100 BDT) and single item."""
        out_dir = str(tmp_path / "invoices")

        order = Order(
            order_id="ORD-SAVAR-99",
            recipient_name="Anika Rahman",
            recipient_phone="01899887766",
            recipient_address="Savar Bazar Road, Savar",
            delivery_zone=DeliveryZone.SUBURB,
            delivery_fee=100.0,
            cod_amount=1300.0,
            items=[
                OrderItem(sku="JEANS-SLIM-32", name="Slim Fit Stretch Jeans", qty=1, price=1200.0),
            ],
        )

        pdf_path = generate_invoice_pdf(order, output_dir=out_dir)
        assert os.path.exists(pdf_path)
        assert os.path.getsize(pdf_path) > 1500

    def test_invoice_generation_fallback_items(self, tmp_path):
        """Invoice generation when items list is empty (generic parcel item fallback)."""
        out_dir = str(tmp_path / "invoices")

        order = Order(
            order_id="ORD-NOITEMS-01",
            recipient_name="Customer",
            recipient_phone="01700112233",
            recipient_address="Uttara, Dhaka",
            delivery_zone=DeliveryZone.CORE,
            delivery_fee=70.0,
            cod_amount=1070.0,
            items=[],
        )

        pdf_path = generate_invoice_pdf(order, output_dir=out_dir)
        assert os.path.exists(pdf_path)
        assert os.path.getsize(pdf_path) > 1500

