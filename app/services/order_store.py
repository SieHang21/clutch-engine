"""
Clutch (ক্ল্যাচ) - Order Repository & SQLite Database Persistence
Thread-safe order repository supporting pagination, multi-criteria filtering,
search across phone numbers / tracking codes / consignment IDs, and atomic state updates
backed by SQLite / SQLModel.
"""

from typing import List, Optional, Tuple, Dict, Any
from datetime import datetime
from sqlmodel import Session, select

from app.models.order import Order, OrderStatus, DeliveryZone
import app.db.session as db_session


class OrderStore:
    """
    SQLModel/SQLite-backed repository for storing and querying Orders in clutch.db.
    Maintains thread-safety and session context management while fully preserving
    pagination, multi-field search, and state transitions.
    """

    def __init__(self, session: Optional[Session] = None):
        self._session = session

    def _get_session(self) -> Session:
        if self._session is not None:
            return self._session
        return db_session.SessionLocal()

    def save(self, order: Order) -> Order:
        """Insert or update an order in SQLite."""
        session = self._get_session()
        should_close = (self._session is None)
        try:
            merged = session.merge(order)
            session.commit()
            session.refresh(merged)
            for f in type(order).model_fields:
                setattr(order, f, getattr(merged, f))
            return order
        finally:
            if should_close:
                session.close()

    def get_by_id(self, order_id: str) -> Optional[Order]:
        """Fetch an order by its unique order_id from SQLite."""
        session = self._get_session()
        should_close = (self._session is None)
        try:
            return session.get(Order, order_id)
        finally:
            if should_close:
                session.close()

    def list_orders(
        self,
        status: Optional[OrderStatus] = None,
        search: Optional[str] = None,
        zone: Optional[DeliveryZone] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[Order], int]:
        """
        Query orders with status filtering, multi-field search, and pagination from SQLite.
        """
        session = self._get_session()
        should_close = (self._session is None)
        try:
            statement = select(Order)
            if status is not None:
                statement = statement.where(Order.status == status)
            if zone is not None:
                statement = statement.where(Order.delivery_zone == zone)

            matched: List[Order] = list(session.exec(statement).all())

            # Filter by search query across phone, order_id, name, consignment_id, tracking_code
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

            # Sort descending by creation date (newest first)
            matched.sort(
                key=lambda o: o.created_at if o.created_at else datetime.min,
                reverse=True,
            )

            total_count = len(matched)
            page = max(1, page)
            page_size = max(1, page_size)
            start_idx = (page - 1) * page_size
            end_idx = start_idx + page_size
            paged_results = matched[start_idx:end_idx]

            return paged_results, total_count
        finally:
            if should_close:
                session.close()

    def delete(self, order_id: str) -> bool:
        """Remove an order from SQLite."""
        session = self._get_session()
        should_close = (self._session is None)
        try:
            order = session.get(Order, order_id)
            if order:
                session.delete(order)
                session.commit()
                return True
            return False
        finally:
            if should_close:
                session.close()

    def clear(self) -> None:
        """Clear all orders from SQLite table (primarily for test fixtures)."""
        session = self._get_session()
        should_close = (self._session is None)
        try:
            orders = session.exec(select(Order)).all()
            for o in orders:
                session.delete(o)
            session.commit()
        finally:
            if should_close:
                session.close()

    def count(self) -> int:
        """Get total number of orders in SQLite database."""
        session = self._get_session()
        should_close = (self._session is None)
        try:
            return len(session.exec(select(Order)).all())
        finally:
            if should_close:
                session.close()


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
