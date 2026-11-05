"""
Clutch (ক্ল্যাচ) - Customer Notification & Interactive Dispatch Service
Outbound confirmation dispatcher for WhatsApp Cloud API and Meta Messenger Graph API:
- Generates bilingual order confirmation summaries (English + Bengali)
- Constructs single-tap quick-reply button templates (Confirm / Modify Address / Cancel)
- Handles incoming customer postbacks to trigger automated courier dispatch, hold, or cancellation
- Fully supports mock / sandbox mode for offline testing and staging
"""

from typing import Dict, Any, Optional, List
import os
import uuid
import logging
import httpx

from app.models.order import Order, OrderStatus, DeliveryZone
from app.services.order_store import default_order_store, OrderStore
from app.services.courier_steadfast import default_courier_service, SteadfastCourierService
from app.services.invoice_generator import generate_invoice_pdf

logger = logging.getLogger(__name__)

# Action Payload Prefixes
ACTION_CONFIRM = "CONFIRM_ORDER"
ACTION_MODIFY = "MODIFY_ADDRESS"
ACTION_CANCEL = "CANCEL_ORDER"


def format_bilingual_order_summary(order: Order, brand_name: str = "Clutch Fashion") -> str:
    """
    Format a customer-facing bilingual order summary (English + Bengali).
    Includes item breakdown, delivery destination, courier partner, COD total, and tracking info.
    """
    zone_label = {
        DeliveryZone.CORE: "Dhaka Core / ঢাকা সিটি (৳70)",
        DeliveryZone.SUBURB: "Suburbs / ঢাকা উপশহর (৳100)",
        DeliveryZone.OUTSIDE: "Outside Dhaka / ঢাকার বাইরে (৳130)",
    }.get(order.delivery_zone, str(order.delivery_zone.value))

    # Items section
    if order.items:
        item_lines = [f"• {it.name} (Qty: {it.qty}) - ৳{it.total:.2f}" for it in order.items]
        items_text = "\n".join(item_lines)
    else:
        items_text = "• 1x Parcel Item(s) / পার্সেল পণ্য"

    tracking_section = ""
    if order.tracking_code:
        tracking_section = (
            f"\n🔍 Tracking Code / ট্র্যাকিং কোড: {order.tracking_code}\n"
            f"Tracking Link: https://steadfast.com.bd/tracking/{order.tracking_code}\n"
        )

    summary = (
        f"📦 {brand_name} - Order Confirmation / অর্ডার নিশ্চিতকরণ\n"
        f"----------------------------------------\n"
        f"Order ID / অর্ডার নং: {order.order_id}\n\n"
        f"📋 Items / পণ্য তালিকা:\n"
        f"{items_text}\n\n"
        f"📍 Delivery Address / ডেলিভারি ঠিকানা:\n"
        f"{order.recipient_address}\n"
        f"Zone: {zone_label}\n\n"
        f"🚚 Courier / কুরিয়ার: Steadfast Courier\n"
        f"💵 Total COD / সর্বমোট ক্যাশ অন ডেলিভারি: ৳{order.cod_amount:.2f}\n"
        f"{tracking_section}\n"
        f"Please tap below to confirm or modify your order.\n"
        f"অর্ডারটি নিশ্চিত বা পরিবর্তন করতে নিচের বাটনে চাপ দিন:"
    )
    return summary


class CustomerNotifierService:
    """
    Dispatcher managing bilingual interactive notifications for WhatsApp and Meta Messenger.
    """

    def __init__(
        self,
        whatsapp_token: Optional[str] = None,
        whatsapp_phone_number_id: Optional[str] = None,
        messenger_page_token: Optional[str] = None,
        order_store: Optional[OrderStore] = None,
        courier_service: Optional[SteadfastCourierService] = None,
        is_mock: Optional[bool] = None,
    ):
        self.whatsapp_token = whatsapp_token or os.getenv("WHATSAPP_CLOUD_API_TOKEN", "")
        self.phone_number_id = whatsapp_phone_number_id or os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
        self.messenger_token = messenger_page_token or os.getenv("META_PAGE_ACCESS_TOKEN", "")
        self.order_store = order_store or default_order_store
        self.courier_service = courier_service or default_courier_service

        # Mock mode enabled if tokens missing or in test mode
        if is_mock is not None:
            self.is_mock = is_mock
        else:
            self.is_mock = not (self.whatsapp_token or self.messenger_token)

        self.sent_notifications: List[Dict[str, Any]] = []

    def build_whatsapp_interactive_payload(self, order: Order, recipient_phone: Optional[str] = None) -> Dict[str, Any]:
        """
        Build WhatsApp Cloud API interactive button payload.
        WhatsApp limits interactive buttons to max 3 buttons with title length <= 20 chars.
        """
        phone = recipient_phone or order.recipient_phone
        # Format international 880 prefix
        clean_phone = phone.lstrip("+")
        if clean_phone.startswith("01"):
            clean_phone = "88" + clean_phone

        body_text = format_bilingual_order_summary(order)

        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": clean_phone,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body_text},
                "action": {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {
                                "id": f"{ACTION_CONFIRM}:{order.order_id}",
                                "title": "Confirm Order",
                            },
                        },
                        {
                            "type": "reply",
                            "reply": {
                                "id": f"{ACTION_MODIFY}:{order.order_id}",
                                "title": "Modify Address",
                            },
                        },
                        {
                            "type": "reply",
                            "reply": {
                                "id": f"{ACTION_CANCEL}:{order.order_id}",
                                "title": "Cancel",
                            },
                        },
                    ]
                },
            },
        }

    def build_messenger_quick_reply_payload(self, order: Order, recipient_id: str) -> Dict[str, Any]:
        """
        Build Meta Messenger Graph API Quick Reply message payload.
        """
        body_text = format_bilingual_order_summary(order)

        return {
            "recipient": {"id": str(recipient_id)},
            "messaging_type": "RESPONSE",
            "message": {
                "text": body_text,
                "quick_replies": [
                    {
                        "content_type": "text",
                        "title": "Confirm Order",
                        "payload": f"{ACTION_CONFIRM}:{order.order_id}",
                    },
                    {
                        "content_type": "text",
                        "title": "Modify Address",
                        "payload": f"{ACTION_MODIFY}:{order.order_id}",
                    },
                    {
                        "content_type": "text",
                        "title": "Cancel",
                        "payload": f"{ACTION_CANCEL}:{order.order_id}",
                    },
                ],
            },
        }

    async def send_whatsapp_confirmation(self, order: Order, recipient_phone: Optional[str] = None) -> Dict[str, Any]:
        """Dispatch WhatsApp interactive confirmation message."""
        payload = self.build_whatsapp_interactive_payload(order, recipient_phone=recipient_phone)

        if self.is_mock:
            mock_id = f"wamid.{uuid.uuid4().hex}"
            record = {
                "channel": "whatsapp",
                "order_id": order.order_id,
                "recipient": payload["to"],
                "message_id": mock_id,
                "payload": payload,
                "status": "sent",
            }
            self.sent_notifications.append(record)
            logger.info("Mock WhatsApp confirmation dispatched for order %s to %s", order.order_id, payload["to"])
            return {"status": "success", "message_id": mock_id, "mock": True}

        url = f"https://graph.facebook.com/v19.0/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.whatsapp_token}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(url, json=payload, headers=headers)
            res.raise_for_status()
            data = res.json()
            return {"status": "success", "response": data}

    async def send_messenger_confirmation(self, order: Order, recipient_id: str) -> Dict[str, Any]:
        """Dispatch Meta Messenger quick reply confirmation."""
        payload = self.build_messenger_quick_reply_payload(order, recipient_id=recipient_id)

        if self.is_mock:
            mock_id = f"mid.{uuid.uuid4().hex}"
            record = {
                "channel": "messenger",
                "order_id": order.order_id,
                "recipient": recipient_id,
                "message_id": mock_id,
                "payload": payload,
                "status": "sent",
            }
            self.sent_notifications.append(record)
            logger.info("Mock Messenger confirmation dispatched for order %s to %s", order.order_id, recipient_id)
            return {"status": "success", "message_id": mock_id, "mock": True}

        url = "https://graph.facebook.com/v19.0/me/messages"
        params = {"access_token": self.messenger_token}

        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(url, params=params, json=payload)
            res.raise_for_status()
            data = res.json()
            return {"status": "success", "response": data}

    async def handle_postback(self, postback_payload: str) -> Dict[str, Any]:
        """
        Process customer single-tap button postbacks:
        - CONFIRM_ORDER:{order_id} -> Automatically triggers courier dispatch + invoice generation
        - MODIFY_ADDRESS:{order_id} -> Flags order as ADMIN_HELD for review
        - CANCEL_ORDER:{order_id} -> Cancels the order
        """
        if not postback_payload or ":" not in postback_payload:
            raise ValueError(f"Malformed postback payload: '{postback_payload}'")

        action, order_id = postback_payload.split(":", 1)
        action = action.strip().upper()
        order_id = order_id.strip()

        order = self.order_store.get_by_id(order_id)
        if not order:
            raise ValueError(f"Order '{order_id}' not found")

        # 1. Action: Confirm Order -> Dispatch to Steadfast & Generate Invoice
        if action == ACTION_CONFIRM:
            logger.info("Customer confirmed order %s via single-tap postback.", order_id)
            # Advance to PROCESSING if currently PENDING
            if order.can_transition_to(OrderStatus.PROCESSING):
                order.transition_to(OrderStatus.PROCESSING)
            elif order.status == OrderStatus.ADMIN_HELD and order.can_transition_to(OrderStatus.PROCESSING):
                order.transition_to(OrderStatus.PROCESSING)

            # Dispatch with Steadfast
            courier_res = await self.courier_service.create_order(order)

            # Generate PDF Invoice
            invoice_path = generate_invoice_pdf(order)

            # Persist updated state
            self.order_store.save(order)

            return {
                "action": ACTION_CONFIRM,
                "order_id": order.order_id,
                "status": order.status.value,
                "consignment_id": order.consignment_id,
                "tracking_code": order.tracking_code,
                "invoice_url": invoice_path,
                "courier_response": courier_res,
            }

        # 2. Action: Modify Address -> Hold for Admin Review
        elif action == ACTION_MODIFY:
            logger.info("Customer requested address modification for order %s.", order_id)
            if order.can_transition_to(OrderStatus.ADMIN_HELD):
                order.transition_to(OrderStatus.ADMIN_HELD)
            self.order_store.save(order)

            return {
                "action": ACTION_MODIFY,
                "order_id": order.order_id,
                "status": order.status.value,
                "message": "Order placed on ADMIN_HELD for customer address modification",
            }

        # 3. Action: Cancel Order
        elif action == ACTION_CANCEL:
            logger.info("Customer cancelled order %s via single-tap postback.", order_id)
            if order.can_transition_to(OrderStatus.CANCELLED):
                order.transition_to(OrderStatus.CANCELLED)
            self.order_store.save(order)

            return {
                "action": ACTION_CANCEL,
                "order_id": order.order_id,
                "status": order.status.value,
                "message": "Order cancelled by customer",
            }

        else:
            raise ValueError(f"Unknown postback action: '{action}'")


# Default customer notifier service
default_customer_notifier = CustomerNotifierService()

