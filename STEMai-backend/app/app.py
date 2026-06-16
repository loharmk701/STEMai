import sys
import os
import asyncio
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.storage import init_db

from app.core.database import engine
from app.models.user import User
User.metadata.create_all(bind=engine)
# ---------------------------------------------------
# Windows event loop fix
# ---------------------------------------------------
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# ---------------------------------------------------
# Load environment variables
# ---------------------------------------------------
load_dotenv()

# ---------------------------------------------------
# Env config
# ---------------------------------------------------
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
SITE_URL = os.getenv("SITE_URL", "https://stembotix-ai.vercel.app").strip()

# Model routing envs
# ── FIX: google/gemini-2.0-flash-exp:free returns 404 on OpenRouter (model retired).
#         Updated default FREE_MODEL to a working free-tier model.
#         If you set FREE_MODEL in your .env, make sure it is a model that actually
#         exists on https://openrouter.ai/models — verify before deploying.
FREE_MODEL    = os.getenv("FREE_MODEL",    "google/gemini-2.5-pro-exp-03-25:free").strip()
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "openai/gpt-4o-mini").strip()
VISION_MODEL  = os.getenv("VISION_MODEL",  "openai/gpt-4o-mini").strip()
PREMIUM_MODEL = os.getenv("PREMIUM_MODEL", "anthropic/claude-3.5-sonnet").strip()

# Backward-compatible existing var
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL).strip()

# App env
APP_NAME = os.getenv("APP_NAME", "STEMbotix backend").strip()
APP_ENV  = os.getenv("APP_ENV",  "development").strip()
DEBUG    = os.getenv("DEBUG",    "true").lower() == "true"

# CORS
ALLOW_ORIGINS_RAW = os.getenv("ALLOW_ORIGINS", "*").strip()
if ALLOW_ORIGINS_RAW == "*":
    ALLOW_ORIGINS = ["*"]
else:
    ALLOW_ORIGINS = [x.strip() for x in ALLOW_ORIGINS_RAW.split(",") if x.strip()]

# ---------------------------------------------------
# Lifespan
# ---------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n================ STEMbotix Startup ================")
    print(f"Environment        : {APP_ENV}")
    print(f"Debug              : {DEBUG}")
    print(f"Site URL           : {SITE_URL}")
    print(f"Default model      : {DEFAULT_MODEL}")
    print(f"Vision model       : {VISION_MODEL}")
    print(f"Free model         : {FREE_MODEL}")
    print(f"Premium model      : {PREMIUM_MODEL}")
    print(f"OpenRouter model   : {OPENROUTER_MODEL}")
    print(f"API key loaded     : {'YES' if OPENROUTER_API_KEY else 'NO'}")

    try:
        init_db()
        print("✅ Database initialized")
    except Exception as e:
        print(f"❌ Database init failed: {e}")
        raise

    print("✅ App startup complete")
    print("===================================================\n")

    yield

    print("\n🛑 App shutting down\n")


# ---------------------------------------------------
# Create app
# ---------------------------------------------------
app = FastAPI(
    title="STEMbotix AI Backend",
    version="1.0.0",
    lifespan=lifespan
)

# ---------------------------------------------------
# OpenRouter Key Interceptor & CORS Middleware
# ---------------------------------------------------
from starlette.middleware.base import BaseHTTPMiddleware
from app.core.context import openrouter_key_var

class OpenRouterKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        key = request.headers.get("X-OpenRouter-Key")
        token = None
        if key:
            token = openrouter_key_var.set(key)
        try:
            response = await call_next(request)
            return response
        finally:
            if token:
                openrouter_key_var.reset(token)

app.add_middleware(OpenRouterKeyMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------
# Import routers
# ---------------------------------------------------
from app.api.routes import (
    auth,
    blockzie,
    chat,
    voice,
    simulator,
    iot,
    firmware,
    agents,
    blockzie_generate,
    programming_lab,
    esp32_simulator
)

# ---------------------------------------------------
# Register routers
# ---------------------------------------------------
app.include_router(blockzie.router)
app.include_router(auth.router)
app.include_router(chat.router, prefix="/api")
app.include_router(voice.router, prefix="/api")
app.include_router(simulator.router, prefix="/api")
app.include_router(iot.router, prefix="/api")
app.include_router(firmware.router, prefix="/api")
app.include_router(agents.router, prefix="/api")
app.include_router(blockzie_generate.router)
app.include_router(blockzie_generate.router, prefix="/api")
app.include_router(programming_lab.router)        # prefix="/api/lab" built into router
app.include_router(esp32_simulator.router)         # prefix-less, routes use /api/sim_ai etc.

# ---------------------------------------------------
# Root / Health
# ---------------------------------------------------
@app.get("/")
async def root():
    return {
        "ok": True,
        "service": APP_NAME,
        "env": APP_ENV
    }


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": APP_NAME,
        "auth": "enabled",
        "database": "initialized"
    }

@app.get("/debug/auth-test")
async def auth_test():
    """Debug endpoint to verify auth endpoints are accessible"""
    return {
        "ok": True,
        "message": "Auth endpoints are accessible",
        "endpoints": {
            "register": "/auth/register (POST)",
            "login": "/auth/login (POST)",
            "me": "/auth/me (GET with Bearer token)"
        }
    }

@app.get("/api/health")
async def api_health():
    return {
        "ok": True,
        "service": APP_NAME,
        "env": APP_ENV,
        "models": {
            "free":    FREE_MODEL,
            "default": DEFAULT_MODEL,
            "vision":  VISION_MODEL,
            "premium": PREMIUM_MODEL
        },
        "openrouter_key_loaded": bool(OPENROUTER_API_KEY)
    }


@app.get("/config")
async def config_info():
    """
    Safe config info endpoint.
    Does NOT expose secret key.
    """
    return {
        "ok": True,
        "app_name": APP_NAME,
        "env": APP_ENV,
        "site_url": SITE_URL,
        "models": {
            "free":             FREE_MODEL,
            "default":          DEFAULT_MODEL,
            "vision":           VISION_MODEL,
            "premium":          PREMIUM_MODEL,
            "openrouter_model": OPENROUTER_MODEL
        },
        "cors": ALLOW_ORIGINS,
        "openrouter_key_loaded": bool(OPENROUTER_API_KEY)
    }
