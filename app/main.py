"""
Clutch (ক্ল্যাচ) - Automated F-Commerce Order Management Engine
Main FastAPI Application Entrypoint
"""

import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, Header, Depends
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.models.order import Order
from app.db.session import init_db, get_db
from app.api.webhooks import (
    router as webhooks_router,
    verify_meta_webhook,
    receive_meta_webhook,
)
from app.api.dashboard import router as dashboard_router, MOCK_BENGALI_ORDERS

# Ensure directories exist
os.makedirs("invoices", exist_ok=True)
os.makedirs("app/templates", exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Automated table creation and engine initialization hook on startup."""
    init_db()
    yield


app = FastAPI(
    title="Clutch Engine",
    description="Automated F-Commerce Order Management Engine for Bangladesh",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files for invoices
app.mount("/invoices", StaticFiles(directory="invoices"), name="invoices")

# Setup Jinja2 HTML templates
templates = Jinja2Templates(directory="app/templates")


# -------------------------------------------------------------------------
# Core & System Routes
# -------------------------------------------------------------------------
@app.get("/", tags=["system"])
def read_root():
    return {
        "service": "Clutch Engine",
        "status": "online",
        "docs_url": "/docs",
        "dashboard_url": "/dashboard",
        "version": "1.0.0",
    }


@app.get("/health", tags=["system"])
def health_check():
    return {"status": "ok", "service": "clutch-engine"}


@app.get("/dashboard", response_class=HTMLResponse, tags=["merchant-ui"])
async def get_dashboard(request: Request, db: Session = Depends(get_db)):
    """Serve the Clutch Merchant Command Dashboard."""
    # Seed sample orders into SQLite database if empty
    existing_count = len(db.exec(select(Order)).all())
    if existing_count == 0:
        for item in MOCK_BENGALI_ORDERS:
            if not db.get(Order, item["order_id"]):
                db.add(Order(**item))
        db.commit()

    return templates.TemplateResponse(request=request, name="dashboard.html")


# -------------------------------------------------------------------------
# Primary API Router Registrations (Registered exactly ONCE)
# -------------------------------------------------------------------------
# Webhooks Router
app.include_router(webhooks_router, prefix="/api/webhooks")

# Merchant Dashboard & Orders Router
app.include_router(dashboard_router, prefix="/api/orders")


# -------------------------------------------------------------------------
# Backward Compatibility Route for tests hitting /webhooks
# -------------------------------------------------------------------------
@app.api_route("/webhooks", methods=["GET", "POST"], include_in_schema=False)
async def legacy_webhooks_endpoint(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
    db: Session = Depends(get_db),
):
    if request.method == "GET":
        return await verify_meta_webhook(
            hub_mode=request.query_params.get("hub.mode"),
            hub_verify_token=request.query_params.get("hub.verify_token"),
            hub_challenge=request.query_params.get("hub.challenge"),
        )
    return await receive_meta_webhook(request=request, x_hub_signature_256=x_hub_signature_256, db=db)
