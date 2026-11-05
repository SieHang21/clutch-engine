"""
Clutch (ক্ল্যাচ) - Steadfast Courier API Client
Async client for Bangladesh's Steadfast Courier API:
- Order consignment dispatch (`/create_order`)
- Real-time tracking status polling (`/status_by_cid`)
- Seamless Mock / Sandbox fallback for automated testing and credential-less development
"""

from typing import Dict, Any, Optional
import os
import uuid
import logging
import httpx

from app.models.order import Order, OrderStatus

logger = logging.getLogger(__name__)

DEFAULT_STEADFAST_BASE_URL = "https://portal.steadfast.com.bd/api/v1"


class SteadfastAPIError(Exception):
    """Raised when the Steadfast Courier API returns an error or fails."""
    def __init__(self, message: str, status_code: Optional[int] = None, response_data: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data or {}


class SteadfastCourierService:
    """
    Async client for dispatching parcels and polling delivery statuses via Steadfast Courier.
    Automatically enters Mock/Sandbox mode when credentials are missing or set to 'test'/'sandbox'.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        base_url: Optional[str] = None,
        is_mock: Optional[bool] = None,
        timeout: float = 15.0,
    ):
        self.api_key = api_key if api_key is not None else os.getenv("STEADFAST_API_KEY", "")
        self.secret_key = secret_key if secret_key is not None else os.getenv("STEADFAST_SECRET_KEY", "")
        self.base_url = (base_url or os.getenv("STEADFAST_BASE_URL", DEFAULT_STEADFAST_BASE_URL)).rstrip("/")
        self.timeout = timeout

        # Enable mock if explicitly requested or if credentials indicate sandbox/test
        if is_mock is not None:
            self.is_mock = is_mock
        else:
            self.is_mock = (
                not self.api_key
                or not self.secret_key
                or self.api_key.lower() in ("test", "sandbox", "mock")
                or self.secret_key.lower() in ("test", "sandbox", "mock")
            )

        if self.is_mock:
            logger.info("SteadfastCourierService initialized in MOCK/SANDBOX mode.")
        else:
            logger.info("SteadfastCourierService initialized in LIVE PRODUCTION mode.")

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Api-Key": self.api_key,
            "Secret-Key": self.secret_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def serialize_order_payload(self, order: Order, note: Optional[str] = None) -> Dict[str, Any]:
        """Format an Order domain model into Steadfast API's expected JSON payload."""
        return {
            "invoice": order.order_id,
            "recipient_name": order.recipient_name,
            "recipient_phone": order.recipient_phone,
            "recipient_address": order.recipient_address,
            "cod_amount": float(order.cod_amount),
            "note": note or f"Clutch Order {order.order_id}",
        }

    async def create_order(self, order: Order, note: Optional[str] = None) -> Dict[str, Any]:
        """
        Create a courier consignment for the order with Steadfast Courier.
        Updates order.consignment_id, order.tracking_code, and transitions status to COURIER_DISPATCHED.
        """
        payload = self.serialize_order_payload(order, note=note)

        # -----------------------------------------------------------------
        # Mock / Sandbox Dispatch
        # -----------------------------------------------------------------
        if self.is_mock:
            mock_cid = f"SF-CID-{uuid.uuid4().hex[:8].upper()}"
            mock_tracking = f"SF{uuid.uuid4().hex[:8].upper()}"

            order.consignment_id = mock_cid
            order.tracking_code = mock_tracking

            # Advance state machine if allowed
            if order.can_transition_to(OrderStatus.PROCESSING):
                order.transition_to(OrderStatus.PROCESSING)
            if order.can_transition_to(OrderStatus.COURIER_DISPATCHED):
                order.transition_to(OrderStatus.COURIER_DISPATCHED)

            response_data = {
                "status": 200,
                "message": "Order created successfully (sandbox mock)",
                "consignment": {
                    "consignment_id": mock_cid,
                    "invoice": order.order_id,
                    "tracking_code": mock_tracking,
                    "recipient_name": order.recipient_name,
                    "recipient_phone": order.recipient_phone,
                    "recipient_address": order.recipient_address,
                    "cod_amount": order.cod_amount,
                    "status": "in_review",
                },
            }
            logger.info("Mock Steadfast consignment generated: %s (%s)", mock_cid, mock_tracking)
            return response_data

        # -----------------------------------------------------------------
        # Production Live API Request
        # -----------------------------------------------------------------
        url = f"{self.base_url}/create_order"
        headers = self._get_headers()

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                res = await client.post(url, json=payload, headers=headers)

                if res.status_code != 200:
                    raise SteadfastAPIError(
                        f"Steadfast API error HTTP {res.status_code}: {res.text}",
                        status_code=res.status_code,
                    )

                data = res.json()
                consignment = data.get("consignment", {})
                cid = str(consignment.get("consignment_id", ""))
                tracking = str(consignment.get("tracking_code", ""))

                order.consignment_id = cid
                order.tracking_code = tracking

                # Transition state machine
                if order.can_transition_to(OrderStatus.PROCESSING):
                    order.transition_to(OrderStatus.PROCESSING)
                if order.can_transition_to(OrderStatus.COURIER_DISPATCHED):
                    order.transition_to(OrderStatus.COURIER_DISPATCHED)

                return data

        except httpx.RequestError as exc:
            logger.error("Network failure communicating with Steadfast Courier: %s", exc)
            raise SteadfastAPIError(f"Network error connecting to Steadfast: {str(exc)}")

    async def get_delivery_status(self, consignment_id: str) -> Dict[str, Any]:
        """
        Poll delivery tracking status by consignment ID.
        """
        if not consignment_id:
            raise ValueError("consignment_id is required")

        # Mock Mode
        if self.is_mock:
            return {
                "status": 200,
                "delivery_status": "in_review",
                "consignment_id": consignment_id,
                "mock": True,
            }

        # Live API
        url = f"{self.base_url}/status_by_cid/{consignment_id}"
        headers = self._get_headers()

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                res = await client.get(url, headers=headers)
                if res.status_code != 200:
                    raise SteadfastAPIError(
                        f"Failed to fetch status: HTTP {res.status_code}",
                        status_code=res.status_code,
                    )
                return res.json()
        except httpx.RequestError as exc:
            raise SteadfastAPIError(f"Network error polling tracking status: {str(exc)}")


# Default shared service instance
default_courier_service = SteadfastCourierService()

