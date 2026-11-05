"""
Clutch (ক্ল্যাচ) - Automated F-Commerce Order Management Engine
Main FastAPI Application Entrypoint
"""

import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.models.order import Order
from app.api.webhooks import router as webhooks_router
from app.api.dashboard import router as dashboard_router, MOCK_BENGALI_ORDERS, get_order_store

# Ensure directories exist
os.makedirs("invoices", exist_ok=True)
os.makedirs("app/templates", exist_ok=True)

app = FastAPI(
    title="Clutch Engine",
    description="Automated F-Commerce Order Management Engine for Bangladesh",
    version="1.0.0",
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


@app.get("/")
def read_root():
    return {
        "service": "Clutch Engine",
        "status": "online",
        "docs_url": "/docs",
        "dashboard_url": "/dashboard",
        "version": "1.0.0",
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    """Serve the Clutch Merchant Command Dashboard."""
    # Seed sample orders if store is empty
    store = get_order_store()
    if store.count() == 0:
        for item in MOCK_BENGALI_ORDERS:
            if not store.get_by_id(item["order_id"]):
                store.save(Order(**item))

    return templates.TemplateResponse(request=request, name="dashboard.html")

# Mount Webhooks router at root and /api/webhooks
app.include_router(webhooks_router, prefix="/webhooks")
app.include_router(webhooks_router, prefix="/api/webhooks")

# Mount Merchant Dashboard API router at /api
app.include_router(dashboard_router, prefix="/api")


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "clutch-engine"}

