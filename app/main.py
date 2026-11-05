"""
Clutch (ক্ল্যাচ) - Automated F-Commerce Order Management Engine
Main FastAPI Application Entrypoint
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.webhooks import router as webhooks_router
from app.api.dashboard import router as dashboard_router

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


@app.get("/")
def read_root():
    return {
        "service": "Clutch Engine",
        "status": "online",
        "docs_url": "/docs",
        "version": "1.0.0",
    }

# Mount Webhooks router at root and /api/webhooks
app.include_router(webhooks_router, prefix="/webhooks")
app.include_router(webhooks_router, prefix="/api/webhooks")

# Mount Merchant Dashboard API router at /api
app.include_router(dashboard_router, prefix="/api")


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "clutch-engine"}

