"""
Clutch (ক্ল্যাচ) - Order Repository & In-Memory Persistence
Thread-safe order repository supporting pagination, multi-criteria filtering,
search across phone numbers / tracking codes / consignment IDs, and atomic state updates.
"""

from typing import List, Optional, Tuple, Dict, Any
import threading
from datetime import datetime

from app.models.order import Order, OrderStatus, DeliveryZone


class OrderStore:
    """
    Thread-safe repository for storing and querying Orders.
    Acts as the operational persistence layer for the merchant dashboard and state machine.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # order_id -> Order
        self._orders: Dict[str, Order] = {}

    def save(self, order: Order) -> Order:
        """Insert or update an order."""
        with self._lock:
            self._orders[order.order_id] = order
            return order

    def get_by_id(self, order_id: str) -> Optional[Order]:
        """Fetch an order by its unique order_id."""
        with self._lock:
            return self._orders.get(order_id)

    def list_orders(
        self,
        status: Optional[OrderStatus] = None,
        search: Optional[str] = None,
        zone: Optional[DeliveryZone] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[Order], int]:
        """
        Query orders with status filtering, multi-field search, and pagination.
        
        Args:
            status: Optional OrderStatus filter
            search: Optional substring match on phone, consignment_id, tracking_code, order_id, or name
            zone: Optional DeliveryZone filter
            page: 1-indexed page number
            page_size: Number of records per page
            
        Returns:
            Tuple of (list of orders for the page, total matching count)
        """
        with self._lock:
            matched: List[Order] = list(self._orders.values())

        # 1. Filter by status
        if status is not None:
            matched = [o for o in matched if o.status == status]

        # 2. Filter by zone
        if zone is not None:
            matched = [o for o in matched if o.delivery_zone == zone]

        # 3. Filter by search query
        if search and search.strip():
            q = search.strip().lower()
            filtered = []
            for o in matched:
                phone_match = q in o.recipient_phone.lower()
                order_id_match = q in o.order_id.lower()
                name_match = q in o.recipient_name.lower()
                cid_match = bool(o.consignment_id and q in o.consignment_id.lower())
                track_match = bool(o.tracking_code and q in o.tracking_code.lower())

                if phone_match or order_id_match or name_match or cid_match or track_match:
                    filtered.append(o)
            matched = filtered

        # 4. Sort by creation date descending (newest first)
        matched.sort(
            key=lambda o: o.created_at if o.created_at else datetime.min,
            reverse=True,
        )

        total_count = len(matched)

        # 5. Paginate
        page = max(1, page)
        page_size = max(1, page_size)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paged_results = matched[start_idx:end_idx]

        return paged_results, total_count

    def delete(self, order_id: str) -> bool:
        """Remove an order from store."""
        with self._lock:
            return self._orders.pop(order_id, None) is not None

    def clear(self) -> None:
        """Clear all orders (used primarily in test fixtures)."""
        with self._lock:
            self._orders.clear()

    def count(self) -> int:
        """Get total number of orders in store."""
        with self._lock:
            return len(self._orders)


# Global default order repository instance
default_order_store = OrderStore()
_active_order_store: OrderStore = default_order_store


def get_order_store() -> OrderStore:
    """Get the active order repository."""
    return _active_order_store


def set_order_store(store: OrderStore) -> None:
    """Override the active order repository (e.g. for test isolation)."""
    global _active_order_store
    _active_order_store = store

