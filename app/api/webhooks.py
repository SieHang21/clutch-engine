"""
Clutch (ক্ল্যাচ) - Meta Graph Webhook Ingestion Router
Handles Meta Messenger & Instagram Direct webhooks:
- GET: Webhook verification challenge (hub.mode, hub.verify_token, hub.challenge)
- POST: Real-time event notifications with strict HMAC-SHA256 signature verification
- Admin Takeover Sensor: Intercepts is_echo or page admin replies to mute automated responses for 45 minutes
- Session Accumulator: Buffers customer bursts with sliding 75-second TTL
"""

import json
import os
import logging
from typing import Optional, Dict, Any

from fastapi import APIRouter, Request, Header, HTTPException, Query, Response, status
from fastapi.responses import PlainTextResponse

from app.core.security import verify_meta_signature
from app.services.session_buffer import default_session_buffer, SessionBuffer
from app.models.order import Order, OrderItem, OrderStatus, DeliveryZone
from app.services.order_store import get_order_store
from app.services.customer_notifier import default_customer_notifier

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

# Configuration state
_custom_app_secret: Optional[str] = None
_custom_verify_token: Optional[str] = None
_active_session_buffer: SessionBuffer = default_session_buffer


def set_webhook_secrets(app_secret: Optional[str] = None, verify_token: Optional[str] = None) -> None:
    """Override webhook secrets for tests or runtime tenant configuration."""
    global _custom_app_secret, _custom_verify_token
    if app_secret is not None:
        _custom_app_secret = app_secret
    if verify_token is not None:
        _custom_verify_token = verify_token


def set_session_buffer(buffer: SessionBuffer) -> None:
    """Inject a custom session buffer (e.g. isolated test buffer)."""
    global _active_session_buffer
    _active_session_buffer = buffer


def get_session_buffer() -> SessionBuffer:
    """Get the active session buffer."""
    return _active_session_buffer


def get_app_secret() -> str:
    """Get the configured Meta App Secret."""
    if _custom_app_secret:
        return _custom_app_secret
    return os.getenv("META_APP_SECRET", os.getenv("CLUTCH_APP_SECRET", "clutch_dev_secret"))


def get_verify_token() -> str:
    """Get the configured Meta Webhook Verification Token."""
    if _custom_verify_token:
        return _custom_verify_token
    return os.getenv("META_VERIFY_TOKEN", os.getenv("CLUTCH_VERIFY_TOKEN", "clutch_verify_token"))


# -------------------------------------------------------------------------
# GET: Hub Verification Challenge Handlers
# -------------------------------------------------------------------------
@router.get("")
@router.get("/")
@router.get("/webhooks")
async def verify_meta_webhook(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
):
    """
    Handle Meta Webhook verification challenge (GET).
    Meta calls this endpoint when setting up the webhook in Facebook App Dashboard.
    Validates hub.mode == "subscribe" and hub.verify_token. Returns hub.challenge with 200 OK.
    """
    expected_token = get_verify_token()

    if hub_mode == "subscribe" and hub_verify_token == expected_token:
        logger.info("Meta webhook verification challenge succeeded.")
        # Meta requires returning the challenge as raw text
        return PlainTextResponse(content=hub_challenge or "", status_code=status.HTTP_200_OK)

    logger.warning(
        "Meta webhook verification failed: hub.mode=%s, token_match=%s",
        hub_mode,
        hub_verify_token == expected_token,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Verification token mismatch or invalid hub.mode",
    )


# -------------------------------------------------------------------------
# POST: Webhook Event Ingestion Handlers
# -------------------------------------------------------------------------
@router.post("")
@router.post("/")
@router.post("/webhooks")
async def receive_meta_webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
):
    """
    Handle incoming Meta Webhook event notifications (POST).
    Enforces HMAC-SHA256 signature verification over raw payload bytes.
    Detects Admin Takeovers and buffers customer message bursts.
    """
    raw_body: bytes = await request.body()
    app_secret: str = get_app_secret()

    # 1. Enforce Zero-Trust HMAC-SHA256 Signature Verification
    if not x_hub_signature_256 or not verify_meta_signature(raw_body, x_hub_signature_256, app_secret):
        logger.warning("Rejected webhook payload: invalid or missing X-Hub-Signature-256")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid HMAC-SHA256 signature",
        )

    # 2. Parse JSON payload
    try:
        payload: Dict[str, Any] = json.loads(raw_body.decode("utf-8"))
    except Exception as e:
        logger.error("Failed to parse JSON from verified webhook payload: %s", e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed JSON payload",
        )

    buffer = get_session_buffer()
    processed_events = []

    # 3. Process Meta Entry Objects
    entries = payload.get("entry", [])
    for entry in entries:
        page_id = str(entry.get("id", ""))
        messaging_events = entry.get("messaging", [])

        for event in messaging_events:
            sender = event.get("sender", {})
            recipient = event.get("recipient", {})
            sender_id = str(sender.get("id", ""))
            recipient_id = str(recipient.get("id", ""))
            message = event.get("message", {})

            if not message:
                continue

            is_echo = message.get("is_echo", False)
            message_text = message.get("text", "")

            # -----------------------------------------------------------------
            # Admin Takeover Sensor
            # -----------------------------------------------------------------
            # When a page admin replies manually (via Page Inbox / Business Suite),
            # Meta sends an event with is_echo: True, where sender is page_id and
            # recipient is the customer user_id.
            if is_echo or sender_id == page_id:
                target_customer_id = recipient_id if recipient_id != page_id else sender_id
                logger.info(
                    "Admin Takeover detected for customer %s on page %s. Muting bot for 45 minutes.",
                    target_customer_id,
                    page_id,
                )
                buffer.set_admin_takeover(target_customer_id, page_id=page_id, ttl=2700)
                processed_events.append({
                    "type": "admin_takeover",
                    "customer_id": target_customer_id,
                    "page_id": page_id,
                    "action": "muted_for_45m",
                })
                continue

            # -----------------------------------------------------------------
            # Customer Message Processing
            # -----------------------------------------------------------------
            # If the admin takeover sensor is active for this customer, suppress bot replies
            if buffer.is_admin_takeover_active(sender_id, page_id=page_id):
                logger.info(
                    "Customer %s sent message, but admin takeover is currently active. Bypassing bot parsing.",
                    sender_id,
                )
                processed_events.append({
                    "type": "customer_message",
                    "sender_id": sender_id,
                    "page_id": page_id,
                    "action": "bypassed_due_to_admin_takeover",
                })
                continue

            # Forward message to session accumulator buffer (75s sliding TTL)
            if message_text:
                buffer_result = buffer.add_message(
                    sender_id=sender_id,
                    text=message_text,
                    page_id=page_id,
                )

                created_order_id = None
                if buffer_result.get("is_complete") and buffer_result.get("order"):
                    order_info = buffer_result["order"]
                    clean_phone = order_info.get("clean_phone")

                    # If valid unambiguous phone was extracted, generate order in PENDING_CONFIRMATION
                    if clean_phone and not order_info.get("has_phone_conflict"):
                        zone_str = order_info.get("zone", "CORE")
                        try:
                            parsed_zone = DeliveryZone(zone_str)
                        except ValueError:
                            parsed_zone = DeliveryZone.CORE

                        fee_val = float(order_info.get("fee", 70.0))
                        combined_addr = buffer_result.get("combined_text", "")

                        order_obj = Order(
                            recipient_name=f"Customer {sender_id[-4:] if len(sender_id) >= 4 else sender_id}",
                            recipient_phone=clean_phone,
                            recipient_address=combined_addr,
                            delivery_zone=parsed_zone,
                            delivery_fee=fee_val,
                            cod_amount=round(1200.0 + fee_val, 2),
                            items=[
                                OrderItem(sku="CLUTCH-APP-01", name="F-Commerce Apparel", qty=1, price=1200.0)
                            ],
                            status=OrderStatus.PENDING_CONFIRMATION,
                        )

                        get_order_store().save(order_obj)
                        created_order_id = order_obj.order_id

                        # Dispatch automated confirmation notification
                        try:
                            await default_customer_notifier.send_messenger_confirmation(
                                order_obj,
                                recipient_id=sender_id,
                            )
                        except Exception as notif_err:
                            logger.warning("Could not dispatch customer notification: %s", notif_err)

                processed_events.append({
                    "type": "customer_message",
                    "sender_id": sender_id,
                    "page_id": page_id,
                    "action": "buffered",
                    "message_count": buffer_result["message_count"],
                    "is_complete": buffer_result["is_complete"],
                    "order_id": created_order_id,
                })

    # Return HTTP 200 OK to acknowledge receipt to Meta Graph API
    return {
        "status": "EVENT_RECEIVED",
        "processed_count": len(processed_events),
        "events": processed_events,
    }

