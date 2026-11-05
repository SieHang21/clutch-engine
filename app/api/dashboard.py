"""
Clutch (ক্ল্যাচ) - Merchant Live Dashboard & Order Management API
Administrative endpoints for:
- Paginated order query with status filters & multi-field search (phone, consignment, tracking code)
- Manual order review and field override (zone, fee, COD total, address)
- Single-tap automated courier dispatch (Steadfast Courier API consignment + PDF invoice generation)
- Administrative order hold (ADMIN_HELD) and customer postback handling
"""

from typing import List, Optional, Dict, Any
import logging
from math import ceil

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.models.order import (
    Order,
    OrderStatus,
    DeliveryZone,
    DELIVERY_ZONE_FEES,
    InvalidStateTransitionError,
)
from app.services.order_store import default_order_store, OrderStore
from app.services.courier_steadfast import default_courier_service, SteadfastCourierService
from app.services.invoice_generator import generate_invoice_pdf
from app.services.customer_notifier import default_customer_notifier, CustomerNotifierService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["dashboard"])

# Module service references (injectable for testing)
_active_order_store: OrderStore = default_order_store
_active_courier_service: SteadfastCourierService = default_courier_service
_active_notifier: CustomerNotifierService = default_customer_notifier


def get_order_store() -> OrderStore:
    return _active_order_store


def set_order_store(store: OrderStore) -> None:
    global _active_order_store
    _active_order_store = store


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
