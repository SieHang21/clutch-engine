"""
Clutch (ক্ল্যাচ) - Order Domain Model & SQLite Database Schema
SQLModel table representation for order lifecycle, items, delivery zones, and strict transition enforcement.
Persisted in SQLite database (clutch.db) while preserving full Pydantic v2 validation semantics.
"""

from enum import Enum
from typing import List, Optional, Union, Dict, Any, Set
from datetime import datetime, timezone
import uuid
import re
import json

from pydantic import BaseModel, field_validator, ValidationError
from pydantic_core import PydanticCustomError, InitErrorDetails
from sqlmodel import SQLModel, Field
from sqlalchemy import Column
from sqlalchemy.types import TypeDecorator, Text


# -------------------------------------------------------------------------
# Exceptions
# -------------------------------------------------------------------------
class InvalidStateTransitionError(ValueError):
    """Raised when an order attempts an illegal lifecycle transition."""
    pass


# -------------------------------------------------------------------------
# Enums
# -------------------------------------------------------------------------
class OrderStatus(str, Enum):
    """Order lifecycle state machine states."""
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    PROCESSING = "PROCESSING"
    ADMIN_HELD = "ADMIN_HELD"
    COURIER_DISPATCHED = "COURIER_DISPATCHED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


class DeliveryZone(str, Enum):
    """3-tier delivery boundary zones."""
    CORE = "CORE"
    SUBURB = "SUBURB"
    OUTSIDE = "OUTSIDE"


# Explicit fee mapping per zone in BDT
DELIVERY_ZONE_FEES: Dict[DeliveryZone, float] = {
    DeliveryZone.CORE: 70.0,
    DeliveryZone.SUBURB: 100.0,
    DeliveryZone.OUTSIDE: 130.0,
}

# Strict state machine transition rules
VALID_TRANSITIONS: Dict[OrderStatus, Set[OrderStatus]] = {
    OrderStatus.PENDING_CONFIRMATION: {
        OrderStatus.PROCESSING,
        OrderStatus.ADMIN_HELD,
        OrderStatus.CANCELLED,
    },
    OrderStatus.PROCESSING: {
        OrderStatus.COURIER_DISPATCHED,
        OrderStatus.ADMIN_HELD,
        OrderStatus.CANCELLED,
    },
    OrderStatus.ADMIN_HELD: {
        OrderStatus.PENDING_CONFIRMATION,
        OrderStatus.PROCESSING,
        OrderStatus.CANCELLED,
    },
    OrderStatus.COURIER_DISPATCHED: {
        OrderStatus.DELIVERED,
        OrderStatus.CANCELLED,
        OrderStatus.ADMIN_HELD,
    },
    # Terminal states: no further transitions permitted
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELLED: set(),
}


# -------------------------------------------------------------------------
# Item Model
# -------------------------------------------------------------------------
class OrderItem(BaseModel):
    """Item breakdown in an order."""
    sku: str = Field(..., description="Stock Keeping Unit identifier")
    name: str = Field(..., description="Product title or description")
    qty: int = Field(default=1, gt=0, description="Quantity ordered (must be >= 1)")
    price: float = Field(..., ge=0.0, description="Unit price in BDT (must be >= 0)")

    @property
    def total(self) -> float:
        """Total line price."""
        return round(float(self.price * self.qty), 2)


class OrderItemListType(TypeDecorator):
    """SQLAlchemy TypeDecorator serializing List[OrderItem] to JSON Text in SQLite."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return "[]"
        if isinstance(value, list):
            dumped = [it.model_dump() if isinstance(it, BaseModel) else dict(it) for it in value]
            return json.dumps(dumped)
        return json.dumps(value)

    def process_result_value(self, value, dialect):
        if not value:
            return []
        try:
            data = json.loads(value)
            if isinstance(data, list):
                return [OrderItem(**it) if isinstance(it, dict) else it for it in data]
        except Exception:
            return []
        return []


# -------------------------------------------------------------------------
# Order Table & Domain Model
# -------------------------------------------------------------------------
class Order(SQLModel, table=True):
    """
    Core Order aggregate model and SQLite table schema.
    Stores recipient contact details, zone classification, items JSON,
    and enforces lifecycle state transitions.
    """
    __tablename__ = "orders"

    order_id: str = Field(
        default_factory=lambda: f"ORD-{uuid.uuid4().hex[:8].upper()}",
        primary_key=True,
        description="Unique order identifier"
    )
    recipient_name: str = Field(..., min_length=1, description="Recipient customer full name")
    recipient_phone: str = Field(..., description="Validated 11-digit Bangladeshi mobile number")
    recipient_address: str = Field(..., min_length=3, description="Full delivery destination address")
    delivery_zone: DeliveryZone = Field(default=DeliveryZone.CORE, description="CORE / SUBURB / OUTSIDE")
    delivery_fee: float = Field(default=70.0, ge=0.0, description="Delivery fee in BDT")
    cod_amount: float = Field(..., ge=0.0, description="Total Cash on Delivery collection amount in BDT")
    items: List[OrderItem] = Field(
        default_factory=list,
        sa_column=Column("items", OrderItemListType),
        description="Itemized product list"
    )
    consignment_id: Optional[str] = Field(default=None, description="Steadfast courier consignment ID")
    tracking_code: Optional[str] = Field(default=None, description="Steadfast courier tracking code")
    invoice_url: Optional[str] = Field(default=None, description="Filesystem path or URL to PDF invoice")
    status: OrderStatus = Field(default=OrderStatus.PENDING_CONFIRMATION, description="Lifecycle status")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Creation timestamp")
    updated_at: Optional[datetime] = Field(default=None, description="Last modification timestamp")

    # ---------------------------------------------------------------------
    # Normalizers & Validators
    # ---------------------------------------------------------------------
    @classmethod
    def validate_and_normalize_phone(cls, v: str) -> str:
        """
        Validate and normalize Bangladeshi mobile number:
        - Strips whitespace, hyphens, and +880/880 prefixes.
        - Must be exactly 11 digits starting with valid operator prefix (013, 014, 015, 016, 017, 018, 019).
        """
        if not v or not isinstance(v, str):
            raise ValueError("Recipient phone number is required")

        # Map Bengali numerals if present
        bn_map = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
        cleaned = v.translate(bn_map)
        cleaned = re.sub(r"[\s\-\+\(\)]", "", cleaned)

        # Handle +880 or 880 prefix
        if cleaned.startswith("880") and len(cleaned) == 13:
            cleaned = cleaned[2:]  # Keep leading 0: 88017... -> 017...
        elif cleaned.startswith("88") and len(cleaned) == 13:
            cleaned = cleaned[2:]

        # Validate standard 11 digits starting with 013-019
        if not re.match(r"^01[3-9]\d{8}$", cleaned):
            raise ValueError(
                f"Invalid Bangladeshi mobile number '{v}'. Must be 11 digits with valid operator prefix (013-019)."
            )

        return cleaned

    @classmethod
    def parse_delivery_zone(cls, v: Any) -> DeliveryZone:
        """Allow flexible zone naming (CORE, Inside Dhaka, Dhaka Core, Suburbs, etc.)."""
        if isinstance(v, DeliveryZone):
            return v
        if isinstance(v, str):
            normalized = v.strip().upper()
            if normalized in ("CORE", "DHAKA_CORE", "INSIDE DHAKA", "DHAKA CORE", "INSIDE_DHAKA"):
                return DeliveryZone.CORE
            if normalized in ("SUBURB", "SUBURBS", "DHAKA SUBURBS", "DHAKA_SUBURBS"):
                return DeliveryZone.SUBURB
            if normalized in ("OUTSIDE", "OUTSIDE DHAKA", "OUTSIDE_DHAKA"):
                return DeliveryZone.OUTSIDE
        raise ValueError(f"Unknown delivery zone: {v}. Must be CORE, SUBURB, or OUTSIDE.")

    @field_validator("recipient_phone")
    @classmethod
    def _validate_phone_pydantic(cls, v: str) -> str:
        return cls.validate_and_normalize_phone(v)

    @field_validator("delivery_zone", mode="before")
    @classmethod
    def _parse_zone_pydantic(cls, v: Any) -> DeliveryZone:
        return cls.parse_delivery_zone(v)

    def __init__(self, **data):
        if "recipient_phone" in data:
            try:
                data["recipient_phone"] = self.validate_and_normalize_phone(data["recipient_phone"])
            except ValueError as e:
                err = InitErrorDetails(
                    type=PydanticCustomError("value_error", str(e)),
                    loc=("recipient_phone",),
                    input=data.get("recipient_phone"),
                )
                raise ValidationError.from_exception_data(self.__class__.__name__, [err]) from e

        if "delivery_zone" in data:
            try:
                data["delivery_zone"] = self.parse_delivery_zone(data["delivery_zone"])
            except ValueError as e:
                err = InitErrorDetails(
                    type=PydanticCustomError("value_error", str(e)),
                    loc=("delivery_zone",),
                    input=data.get("delivery_zone"),
                )
                raise ValidationError.from_exception_data(self.__class__.__name__, [err]) from e

        if "items" in data and isinstance(data["items"], list):
            data["items"] = [
                OrderItem(**it) if isinstance(it, dict) else it
                for it in data["items"]
            ]
        super().__init__(**data)

    # ---------------------------------------------------------------------
    # State Machine Methods
    # ---------------------------------------------------------------------
    def can_transition_to(self, new_state: Union[OrderStatus, str]) -> bool:
        """Check whether transition to target state is legally permitted."""
        if isinstance(new_state, str):
            try:
                new_state = OrderStatus(new_state)
            except ValueError:
                return False
        allowed = VALID_TRANSITIONS.get(self.status, set())
        return new_state in allowed

    def transition_to(self, new_state: Union[OrderStatus, str]) -> "Order":
        """
        Transition order to a new lifecycle state.
        Raises InvalidStateTransitionError if the jump violates the state machine.
        """
        if isinstance(new_state, str):
            try:
                target_state = OrderStatus(new_state)
            except ValueError:
                raise InvalidStateTransitionError(f"Invalid order status string: '{new_state}'")
        else:
            target_state = new_state

        if not self.can_transition_to(target_state):
            raise InvalidStateTransitionError(
                f"Illegal state transition: Cannot move order {self.order_id} "
                f"from '{self.status.value}' to '{target_state.value}'."
            )

        self.status = target_state
        self.updated_at = datetime.now(timezone.utc)
        return self

    # ---------------------------------------------------------------------
    # Calculation Helpers
    # ---------------------------------------------------------------------
    def calculate_items_subtotal(self) -> float:
        """Compute the sum of line item totals."""
        return round(sum(item.total for item in self.items), 2)

    def calculate_expected_total(self) -> float:
        """Compute total COD amount (items subtotal + delivery fee)."""
        return round(self.calculate_items_subtotal() + float(self.delivery_fee), 2)
