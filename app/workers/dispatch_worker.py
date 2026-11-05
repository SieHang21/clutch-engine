"""
Clutch (ক্ল্যাচ) - Background Dispatch & Invoice Worker
Lightweight asynchronous task queue consumer processing courier dispatch
and PDF invoice generation without blocking incoming webhook responses.
Supports Redis queue (BRPOP) with thread-safe in-process fallback.
"""

import os
import time
import json
import logging
import asyncio
from typing import Optional, Dict, Any, List

from app.models.order import Order, OrderStatus
from app.services.order_store import default_order_store, OrderStore
from app.services.courier_steadfast import default_courier_service, SteadfastCourierService
from app.services.invoice_generator import generate_invoice_pdf
from app.services.customer_notifier import default_customer_notifier, CustomerNotifierService

logger = logging.getLogger(__name__)

DISPATCH_QUEUE_KEY = "clutch:queue:dispatch"

# In-process queue for fallback when Redis is offline
_in_memory_task_queue: List[str] = []


def enqueue_dispatch_job(order_id: str, redis_url: Optional[str] = None) -> bool:
    """
    Enqueue an order_id for background courier dispatch and invoice generation.
    Pushes to Redis list if available, else stores in thread-safe memory queue.
    """
    url = redis_url or os.getenv("REDIS_URL")
    if url:
        try:
            import redis
            client = redis.Redis.from_url(url, socket_connect_timeout=0.5)
            client.lpush(DISPATCH_QUEUE_KEY, order_id)
            logger.info("Enqueued order %s to Redis queue '%s'", order_id, DISPATCH_QUEUE_KEY)
            return True
        except Exception as e:
            logger.warning("Redis enqueue failed (%s). Appending to in-memory queue.", e)

    # In-memory fallback
    _in_memory_task_queue.append(order_id)
    logger.info("Enqueued order %s to in-memory queue", order_id)
    return True


async def process_dispatch_job(
    order_id: str,
    order_store: Optional[OrderStore] = None,
    courier_service: Optional[SteadfastCourierService] = None,
    notifier_service: Optional[CustomerNotifierService] = None,
) -> Dict[str, Any]:
    """
    Execute background dispatch processing:
    1. Fetch order
    2. Advance state machine to PROCESSING -> COURIER_DISPATCHED
    3. Issue Steadfast Courier consignment
    4. Generate PDF invoice in invoices/
    5. Save order in repository
    6. Return summary
    """
    store = order_store or default_order_store
    courier = courier_service or default_courier_service
    notifier = notifier_service or default_customer_notifier

    order = store.get_by_id(order_id)
    if not order:
        logger.error("Worker error: Order '%s' not found in store.", order_id)
        return {"status": "error", "message": f"Order {order_id} not found"}

    if order.status == OrderStatus.COURIER_DISPATCHED:
        logger.info("Order %s is already dispatched. Skipping.", order_id)
        return {"status": "skipped", "order_id": order_id, "tracking_code": order.tracking_code}

    # Advance state machine
    if order.status == OrderStatus.ADMIN_HELD and order.can_transition_to(OrderStatus.PROCESSING):
        order.transition_to(OrderStatus.PROCESSING)
    elif order.status == OrderStatus.PENDING_CONFIRMATION and order.can_transition_to(OrderStatus.PROCESSING):
        order.transition_to(OrderStatus.PROCESSING)

    # 1. Dispatch with Steadfast
    try:
        courier_res = await courier.create_order(order)
    except Exception as e:
        logger.error("Steadfast dispatch failed in worker for order %s: %s", order_id, e)
        return {"status": "error", "order_id": order_id, "error": str(e)}

    # 2. Generate PDF Invoice
    try:
        invoice_path = generate_invoice_pdf(order)
    except Exception as e:
        logger.error("Invoice generation failed in worker for order %s: %s", order_id, e)
        invoice_path = None

    # Save state
    store.save(order)
    logger.info("Worker successfully dispatched order %s (Consignment: %s, Tracking: %s, Invoice: %s)",
                order_id, order.consignment_id, order.tracking_code, invoice_path)

    return {
        "status": "success",
        "order_id": order.order_id,
        "consignment_id": order.consignment_id,
        "tracking_code": order.tracking_code,
        "invoice_url": invoice_path,
        "courier_response": courier_res,
    }


def run_worker(
    redis_url: Optional[str] = None,
    stop_after_jobs: Optional[int] = None,
    order_store: Optional[OrderStore] = None,
    courier_service: Optional[SteadfastCourierService] = None,
):
    """
    Main worker consumer loop: listens to Redis queue or in-memory queue and executes jobs.
    """
    url = redis_url or os.getenv("REDIS_URL")
    jobs_processed = 0
    logger.info("Clutch background dispatch worker started...")

    client = None
    if url:
        try:
            import redis
            client = redis.Redis.from_url(url, socket_connect_timeout=1.0)
            client.ping()
        except Exception as e:
            logger.warning("Worker could not connect to Redis (%s). Using in-memory queue.", e)
            client = None

    while True:
        order_id = None

        if client is not None:
            try:
                # BRPOP blocks for up to 1 second
                res = client.brpop(DISPATCH_QUEUE_KEY, timeout=1)
                if res:
                    order_id = res[1].decode("utf-8") if isinstance(res[1], bytes) else str(res[1])
            except Exception as e:
                logger.error("Redis BRPOP error: %s", e)
                time.sleep(1)
        else:
            if _in_memory_task_queue:
                order_id = _in_memory_task_queue.pop(0)
            else:
                time.sleep(0.1)

        if order_id:
            logger.info("Worker picked up job for order: %s", order_id)
            asyncio.run(process_dispatch_job(
                order_id,
                order_store=order_store,
                courier_service=courier_service,
            ))
            jobs_processed += 1

            if stop_after_jobs and jobs_processed >= stop_after_jobs:
                logger.info("Worker reached target job limit (%d). Exiting.", jobs_processed)
                break


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run_worker()

