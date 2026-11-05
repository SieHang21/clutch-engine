"""
Clutch (ক্ল্যাচ) - Merchant Live Dashboard & Order Management API
Administrative endpoints for:
- Paginated order query with status filters & multi-field search (phone, consignment, tracking code)
- Manual order review and field override (zone, fee, COD total, address)
- Single-tap automated courier dispatch (Steadfast Courier API consignment + PDF invoice generation)
- Administrative order hold (ADMIN_HELD) and customer postback handling
"""

import os
import random
import uuid
from typing import List, Optional, Dict, Any
import logging
from math import ceil

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.models.order import (
    Order,
    OrderItem,
    OrderStatus,
    DeliveryZone,
    DELIVERY_ZONE_FEES,
    InvalidStateTransitionError,
)
from app.services.order_store import default_order_store, OrderStore, get_order_store, set_order_store
from app.services.courier_steadfast import default_courier_service, SteadfastCourierService
from app.services.invoice_generator import generate_invoice_pdf
from app.services.customer_notifier import default_customer_notifier, CustomerNotifierService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["dashboard"])

# Module service references (injectable for testing)
_active_courier_service: SteadfastCourierService = default_courier_service
_active_notifier: CustomerNotifierService = default_customer_notifier


def get_courier_service() -> SteadfastCourierService:
    return _active_courier_service


def set_courier_service(service: SteadfastCourierService) -> None:
    global _active_courier_service
    _active_courier_service = service


# -------------------------------------------------------------------------
# Request & Response Schemas
# -------------------------------------------------------------------------
class OrderOverrideRequest(BaseModel):
    """Schema for merchant manual order adjustments."""
    delivery_zone: Optional[DeliveryZone] = Field(None, description="Updated delivery zone (CORE/SUBURB/OUTSIDE)")
    delivery_fee: Optional[float] = Field(None, ge=0.0, description="Updated delivery fee in BDT")
    cod_amount: Optional[float] = Field(None, ge=0.0, description="Updated Cash on Delivery total in BDT")
    recipient_address: Optional[str] = Field(None, min_length=3, description="Corrected destination address")
    recipient_name: Optional[str] = Field(None, min_length=1, description="Corrected customer name")
    recipient_phone: Optional[str] = Field(None, description="Corrected 11-digit mobile number")


class OrderHoldRequest(BaseModel):
    """Schema for pausing automated processing."""
    reason: Optional[str] = Field(None, description="Reason for placing order on administrative hold")


class PaginatedOrdersResponse(BaseModel):
    """Paginated order list response."""
    orders: List[Order]
    total: int
    page: int
    page_size: int
    total_pages: int


class OrderApprovalResponse(BaseModel):
    """Response returned upon courier dispatch & invoice creation."""
    status: str
    message: str
    order: Order
    consignment_id: Optional[str]
    tracking_code: Optional[str]
    invoice_url: Optional[str]


# -------------------------------------------------------------------------
# Endpoints
# -------------------------------------------------------------------------
@router.get("", response_model=PaginatedOrdersResponse)
@router.get("/", response_model=PaginatedOrdersResponse)
async def list_orders(
    status_filter: Optional[OrderStatus] = Query(None, alias="status", description="Filter by OrderStatus"),
    search: Optional[str] = Query(None, description="Search by phone, order_id, consignment_id, tracking_code, or name"),
    zone: Optional[DeliveryZone] = Query(None, description="Filter by DeliveryZone"),
    page: int = Query(1, ge=1, description="1-indexed page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
):
    """
    Query orders with multi-criteria filtering, search, and pagination.
    """
    store = get_order_store()
    orders, total = store.list_orders(
        status=status_filter,
        search=search,
        zone=zone,
        page=page,
        page_size=page_size,
    )
    total_pages = ceil(total / page_size) if total > 0 else 1

    return PaginatedOrdersResponse(
        orders=orders,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


# -------------------------------------------------------------------------
# Mock Seed Data & Simulation Definitions
# -------------------------------------------------------------------------
MOCK_BENGALI_ORDERS = [
    {
        "order_id": "ORD-MIRPUR-01",
        "recipient_name": "তানভীর আহমেদ (Tanvir Ahmed)",
        "recipient_phone": "01712345678",
        "recipient_address": "বাড়ি ১২, রোড ৫, ব্লক সি, মিরপুর ১০, ঢাকা",
        "delivery_zone": DeliveryZone.CORE,
        "delivery_fee": 70.0,
        "cod_amount": 1370.0,
        "items": [
            OrderItem(sku="TSH-BLK-01", name="Premium Cotton Drop-Shoulder Tee (L)", qty=2, price=650.0),
        ],
        "status": OrderStatus.PENDING_CONFIRMATION,
    },
    {
        "order_id": "ORD-DHANMONDI-02",
        "recipient_name": "নুসরাত জাহান (Nusrat Jahan)",
        "recipient_phone": "01823456789",
        "recipient_address": "হাউস ৪২, রোড ২৭, ধানমন্ডি, ঢাকা",
        "delivery_zone": DeliveryZone.CORE,
        "delivery_fee": 70.0,
        "cod_amount": 1320.0,
        "items": [
            OrderItem(sku="KUR-FLR-02", name="Embroidered Linen Kurti (M)", qty=1, price=1250.0),
        ],
        "status": OrderStatus.PENDING_CONFIRMATION,
    },
    {
        "order_id": "ORD-SAVAR-03",
        "recipient_name": "রাকিবুল হাসান (Rakibul Hasan)",
        "recipient_phone": "01934567890",
        "recipient_address": "সিআরপি রোড, সাভার বাসস্ট্যান্ডের বিপরীতে, সাভার",
        "delivery_zone": DeliveryZone.SUBURB,
        "delivery_fee": 100.0,
        "cod_amount": 1650.0,
        "items": [
            OrderItem(sku="JNS-SLM-03", name="Vintage Slim Fit Denim Jeans (32)", qty=1, price=1550.0),
        ],
        "status": OrderStatus.PENDING_CONFIRMATION,
    },
    {
        "order_id": "ORD-CHITTAGONG-04",
        "recipient_name": "মেহেদী হাসান (Mehedi Hasan)",
        "recipient_phone": "01645678901",
        "recipient_address": "প্লট ৮, জিইসি মোড়, ওআর নিজাম রোড, নাসিরাবাদ, চট্টগ্রাম",
        "delivery_zone": DeliveryZone.OUTSIDE,
        "delivery_fee": 130.0,
        "cod_amount": 1230.0,
        "items": [
            OrderItem(sku="POLO-NVY-04", name="Pique Cotton Solid Polo (XL)", qty=2, price=550.0),
        ],
        "status": OrderStatus.PROCESSING,
    },
    {
        "order_id": "ORD-BANANI-05",
        "recipient_name": "ফারহানা করিম (Farhana Karim)",
        "recipient_phone": "01756789012",
        "recipient_address": "অ্যাপার্টমেন্ট ৪বি, রোড ১১, ব্লক ডি, বনানী, ঢাকা",
        "delivery_zone": DeliveryZone.CORE,
        "delivery_fee": 70.0,
        "cod_amount": 2570.0,
        "items": [
            OrderItem(sku="SRI-JAM-05", name="Dhakai Jamdani Silk Saree", qty=1, price=2500.0),
        ],
        "status": OrderStatus.COURIER_DISPATCHED,
        "consignment_id": "CID-STF-982141",
        "tracking_code": "STF-98214101",
    },
    {
        "order_id": "ORD-TONGI-06",
        "recipient_name": "সাদিয়া ইসলাম (Sadia Islam)",
        "recipient_phone": "01367890123",
        "recipient_address": "টঙ্গী স্টেশন রোড, গাজীপুর",
        "delivery_zone": DeliveryZone.SUBURB,
        "delivery_fee": 100.0,
        "cod_amount": 1900.0,
        "items": [
            OrderItem(sku="BAG-LTH-06", name="Handcrafted Leather Shoulder Bag", qty=1, price=1800.0),
        ],
        "status": OrderStatus.ADMIN_HELD,
    },
]

DYNAMIC_SIM_CUSTOMERS = [
    {
        "name": "আরিফুল ইসলাম (Ariful Islam)",
        "phone": "01788112233",
        "address": "বাড়ি ১৬, রোড ৪, সেক্টর ৭, উত্তরা, ঢাকা",
        "zone": DeliveryZone.CORE,
        "fee": 70.0,
        "items": [OrderItem(sku="PAN-SLK-07", name="Semi-fitted Cotton Panjabi (40)", qty=1, price=1450.0)],
        "cod": 1520.0,
    },
    {
        "name": "শারমিন আক্তার (Sharmin Akter)",
        "phone": "01944556677",
        "address": "আশুলিয়া বাজার, কাঠগড়া রোড, সাভার",
        "zone": DeliveryZone.SUBURB,
        "fee": 100.0,
        "items": [OrderItem(sku="HIJ-CHI-08", name="Chiffon Premium Hijab Combo (Pack of 3)", qty=1, price=890.0)],
        "cod": 990.0,
    },
    {
        "name": "জাহিদ হাসান (Zahid Hasan)",
        "phone": "01855667788",
        "address": "জিন্দাবাজার মোড়, কোর্ট রোড, সিলেট",
        "zone": DeliveryZone.OUTSIDE,
        "fee": 130.0,
        "items": [OrderItem(sku="HOO-WNT-09", name="Heavyweight Fleece Hoodie (Navy, L)", qty=1, price=1150.0)],
        "cod": 1280.0,
    },
    {
        "name": "মাহমুদা বেগম (Mahmuda Begum)",
        "phone": "01522334455",
        "address": "মধ্য বাড্ডা, প্রগতি সরণি, গুলশান লিংক রোড, ঢাকা",
        "zone": DeliveryZone.CORE,
        "fee": 70.0,
        "items": [OrderItem(sku="SHO-SNK-10", name="Casual Urban Sneakers (White, 42)", qty=1, price=1850.0)],
        "cod": 1920.0,
    },
    {
        "name": "কায়সার মাহমুদ (Kaysar Mahmud)",
        "phone": "01677889900",
        "address": "চৌরাস্তা মোড়, গাজীপুর সদর, গাজীপুর",
        "zone": DeliveryZone.SUBURB,
        "fee": 100.0,
        "items": [OrderItem(sku="WAT-DIG-11", name="Matte Black Chronograph Watch", qty=1, price=2200.0)],
        "cod": 2300.0,
    },
]


@router.get("/metrics")
async def get_dashboard_metrics():
    """Summary metrics for the merchant dashboard."""
    store = get_order_store()
    with store._lock:
        orders = list(store._orders.values())

    total_revenue = sum(
        o.cod_amount for o in orders
        if o.status in (OrderStatus.COURIER_DISPATCHED, OrderStatus.DELIVERED, OrderStatus.PROCESSING)
    )
    pending_count = sum(1 for o in orders if o.status == OrderStatus.PENDING_CONFIRMATION)
    dispatched_count = sum(1 for o in orders if o.status == OrderStatus.COURIER_DISPATCHED)
    held_count = sum(1 for o in orders if o.status == OrderStatus.ADMIN_HELD)
    processing_count = sum(1 for o in orders if o.status == OrderStatus.PROCESSING)
    delivered_count = sum(1 for o in orders if o.status == OrderStatus.DELIVERED)
    cancelled_count = sum(1 for o in orders if o.status == OrderStatus.CANCELLED)

    return {
        "total_revenue": round(total_revenue, 2),
        "pending_count": pending_count,
        "dispatched_count": dispatched_count,
        "held_count": held_count,
        "processing_count": processing_count,
        "delivered_count": delivered_count,
        "cancelled_count": cancelled_count,
        "total_count": len(orders),
    }


@router.post("/mock-seed")
async def seed_mock_orders(
    single: bool = Query(False, description="Generate a single simulated order rather than full demo set")
):
    """
    Seed sample Bengali F-Commerce orders for quick manual testing.
    - single=false: Seeds a full set of realistic Bangladeshi orders across all zones & statuses.
    - single=true: Simulates one incoming Bengali customer order.
    """
    store = get_order_store()

    if single:
        pick = random.choice(DYNAMIC_SIM_CUSTOMERS)
        unique_suffix = uuid.uuid4().hex[:6].upper()
        order = Order(
            order_id=f"ORD-SIM-{unique_suffix}",
            recipient_name=pick["name"],
            recipient_phone=pick["phone"],
            recipient_address=pick["address"],
            delivery_zone=pick["zone"],
            delivery_fee=pick["fee"],
            cod_amount=pick["cod"],
            items=pick["items"],
            status=OrderStatus.PENDING_CONFIRMATION,
        )
        store.save(order)
        return {
            "status": "success",
            "message": f"Simulated order '{order.order_id}' for {order.recipient_name} successfully created.",
            "order": order,
        }

    seeded_count = 0
    created_orders = []
    for item in MOCK_BENGALI_ORDERS:
        existing = store.get_by_id(item["order_id"])
        if not existing:
            order = Order(**item)
            store.save(order)
            created_orders.append(order)
            seeded_count += 1

    return {
        "status": "success",
        "message": f"Seeded {seeded_count} sample Bengali orders.",
        "count": seeded_count,
        "total_in_store": store.count(),
    }


@router.get("/{order_id}", response_model=Order)
async def get_order_by_id(order_id: str):
    """Fetch details of a single order."""
    store = get_order_store()
    order = store.get_by_id(order_id)
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order '{order_id}' not found",
        )
    return order


@router.post("", response_model=Order, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=Order, status_code=status.HTTP_201_CREATED)
async def create_order(order: Order):
    """Manually insert a new order."""
    store = get_order_store()
    store.save(order)
    return order


@router.post("/{order_id}/override", response_model=Order)
async def override_order(order_id: str, payload: OrderOverrideRequest):
    """
    Manual Order Review Override:
    Allows merchant to adjust zone, fee, COD total, or customer address before dispatch.
    """
    store = get_order_store()
    order = store.get_by_id(order_id)
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order '{order_id}' not found",
        )

    # Prevent editing delivered or cancelled orders
    if order.status in (OrderStatus.DELIVERED, OrderStatus.CANCELLED):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot override order in terminal state '{order.status.value}'",
        )

    # 1. Update Zone & Fee
    if payload.delivery_zone is not None:
        order.delivery_zone = payload.delivery_zone
        # Auto-update fee if not explicitly passed
        if payload.delivery_fee is None:
            order.delivery_fee = DELIVERY_ZONE_FEES.get(payload.delivery_zone, order.delivery_fee)

    if payload.delivery_fee is not None:
        order.delivery_fee = payload.delivery_fee

    # 2. Update COD Amount
    if payload.cod_amount is not None:
        order.cod_amount = payload.cod_amount
    elif (payload.delivery_fee is not None or payload.delivery_zone is not None) and order.items:
        # Recompute COD = items subtotal + delivery fee if fee or zone changed
        order.cod_amount = order.calculate_expected_total()

    # 3. Update Contact Details
    if payload.recipient_address is not None:
        order.recipient_address = payload.recipient_address

    if payload.recipient_name is not None:
        order.recipient_name = payload.recipient_name

    if payload.recipient_phone is not None:
        # Will trigger Pydantic validation if invalid
        order.recipient_phone = Order.validate_and_normalize_phone(payload.recipient_phone)

    # Save changes
    store.save(order)
    logger.info("Order %s updated via merchant override.", order_id)
    return order


@router.post("/{order_id}/approve", response_model=OrderApprovalResponse)
async def approve_and_dispatch_order(order_id: str):
    """
    Approve order review:
    1. Advances state from PENDING_CONFIRMATION / ADMIN_HELD to PROCESSING
    2. Dispatches consignment request to Steadfast Courier
    3. Generates the PDF Invoice with Code128 tracking barcode
    4. Transitions order status to COURIER_DISPATCHED
    """
    store = get_order_store()
    courier = get_courier_service()

    order = store.get_by_id(order_id)
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order '{order_id}' not found",
        )

    if order.status == OrderStatus.COURIER_DISPATCHED:
        return OrderApprovalResponse(
            status="already_dispatched",
            message="Order has already been dispatched to Steadfast Courier",
            order=order,
            consignment_id=order.consignment_id,
            tracking_code=order.tracking_code,
            invoice_url=order.invoice_url,
        )

    if order.status in (OrderStatus.DELIVERED, OrderStatus.CANCELLED):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot dispatch order in terminal state '{order.status.value}'",
        )

    # Advance state machine
    if order.status == OrderStatus.ADMIN_HELD and order.can_transition_to(OrderStatus.PROCESSING):
        order.transition_to(OrderStatus.PROCESSING)
    elif order.status == OrderStatus.PENDING_CONFIRMATION and order.can_transition_to(OrderStatus.PROCESSING):
        order.transition_to(OrderStatus.PROCESSING)

    # 1. Dispatch with Steadfast Courier
    try:
        courier_response = await courier.create_order(order)
    except Exception as e:
        logger.error("Steadfast Courier dispatch failed for order %s: %s", order_id, e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Courier dispatch failed: {str(e)}",
        )

    # 2. Generate PDF Invoice
    try:
        invoice_path = generate_invoice_pdf(order)
    except Exception as e:
        logger.error("Invoice generation failed for order %s: %s", order_id, e)
        invoice_path = None

    # Save final state in repository
    store.save(order)
    logger.info("Order %s approved and dispatched with Steadfast (CID: %s, Tracking: %s)",
                order.order_id, order.consignment_id, order.tracking_code)

    return OrderApprovalResponse(
        status="success",
        message="Order successfully approved, dispatched to Steadfast Courier, and invoice generated",
        order=order,
        consignment_id=order.consignment_id,
        tracking_code=order.tracking_code,
        invoice_url=invoice_path,
    )


@router.post("/{order_id}/hold", response_model=Dict[str, Any])
async def hold_order(order_id: str, payload: Optional[OrderHoldRequest] = None):
    """
    Pause automated processing and place order on ADMIN_HELD.
    """
    store = get_order_store()
    order = store.get_by_id(order_id)
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order '{order_id}' not found",
        )

    reason = payload.reason if payload else "Merchant manual hold"

    try:
        order.transition_to(OrderStatus.ADMIN_HELD)
    except InvalidStateTransitionError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    store.save(order)
    logger.info("Order %s placed on ADMIN_HELD. Reason: %s", order_id, reason)

    return {
        "status": "success",
        "message": "Order successfully placed on ADMIN_HELD",
        "order": order,
        "reason": reason,
    }


@router.post("/{order_id}/unhold", response_model=Dict[str, Any])
async def unhold_order(order_id: str):
    """
    Resume automated processing from ADMIN_HELD back to PENDING_CONFIRMATION.
    """
    store = get_order_store()
    order = store.get_by_id(order_id)
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order '{order_id}' not found",
        )

    if order.status != OrderStatus.ADMIN_HELD:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Order is in '{order.status.value}', not 'ADMIN_HELD'",
        )

    try:
        order.transition_to(OrderStatus.PENDING_CONFIRMATION)
    except InvalidStateTransitionError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    store.save(order)
    logger.info("Order %s resumed from ADMIN_HELD to PENDING_CONFIRMATION", order_id)
    return {
        "status": "success",
        "message": "Order resumed back to PENDING_CONFIRMATION",
        "order": order,
    }


@router.get("/{order_id}/invoice")
async def get_order_invoice(order_id: str):
    """
    Download or preview the generated PDF invoice for an order.
    Dynamically generates the invoice if not already present on disk.
    """
    store = get_order_store()
    order = store.get_by_id(order_id)
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order '{order_id}' not found",
        )

    # If invoice file exists on disk, return it directly
    if order.invoice_url and os.path.exists(order.invoice_url):
        return FileResponse(
            path=order.invoice_url,
            media_type="application/pdf",
            filename=f"invoice_{order.order_id}.pdf",
        )

    # Otherwise generate on the fly
    try:
        pdf_path = generate_invoice_pdf(order)
        order.invoice_url = pdf_path
        store.save(order)
        return FileResponse(
            path=pdf_path,
            media_type="application/pdf",
            filename=f"invoice_{order.order_id}.pdf",
        )
    except Exception as e:
        logger.error("Failed to generate invoice on the fly for %s: %s", order_id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not generate invoice PDF: {str(e)}",
        )


@router.post("/postbacks/handle", response_model=Dict[str, Any])
async def handle_customer_postback(payload: Dict[str, str]):
    """
    Webhook handler for customer button postbacks (WhatsApp / Messenger interactive buttons).
    """
    postback_data = payload.get("payload", "")
    if not postback_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing 'payload' field in postback request",
        )

    try:
        res = await _active_notifier.handle_postback(postback_data)
        return res
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
