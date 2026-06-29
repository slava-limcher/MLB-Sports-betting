"""
BarBoards backend — FastAPI application.

Run with:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.bdl_client import bdl_client
from app.config import settings
from app import poller
from app.routes import router as api_router
from app.webhooks import router as webhooks_router
from app.ws import router as ws_router
from app import win_expectancy
from app.win_expectancy import load_table, _we_table

# ── Logging ─────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("barboards")


# ── Lifecycle ───────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting BarBoards backend...")
    await bdl_client.start()
    poller.start()
    logger.info("All systems online.")
    yield
    # Shutdown
    logger.info("Shutting down...")
    await poller.stop()
    await bdl_client.close()
    logger.info("Goodbye.")


# ── App ─────────────────────────────────────────

app = FastAPI(
    title="BarBoards Live Odds",
    description="Real-time betting odds and social engagement for sports bars",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — allow the React frontends
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routers
app.include_router(api_router)
app.include_router(webhooks_router)
app.include_router(ws_router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "bdl_headroom": bdl_client.headroom,
    }


# ── Serve the standalone frontend (play.html + image/font assets) ──
# So phones load the page AND reach the API from the SAME LAN origin —
# no file:// and no hardcoded localhost. mlb-betting/ is two levels up from app/.
# NOTE: the StaticFiles mount at "/" is added LAST so /api, /ws, /health, /webhooks
# (registered above) still win; it only catches play.html and the asset files.
FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent


@app.get("/")
async def _root():
    return RedirectResponse(url="/play.html")


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
