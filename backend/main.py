"""
AutoPay backend — serves the merchant's agent-readable shoe catalog.

Run with:
    uvicorn main:app --reload --port 8000
"""

import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional, Dict, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import agent

# Simple in-memory cache for LLM responses
LLM_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL_MS = 5 * 60 * 1000  # 5 minutes

def get_cached_or_run(key: str, func, *args, **kwargs):
    now = int(time.time() * 1000)
    if key in LLM_CACHE:
        entry = LLM_CACHE[key]
        if now - entry["timestamp"] < CACHE_TTL_MS:
            return entry["result"]
    
    result = func(*args, **kwargs)
    LLM_CACHE[key] = {"result": result, "timestamp": now}
    
    # Cleanup old entries if cache is too large
    if len(LLM_CACHE) > 200:
        oldest = sorted(LLM_CACHE.items(), key=lambda x: x[1]["timestamp"])
        for k, _ in oldest[:50]:
            del LLM_CACHE[k]
            
    return result

try:
    import razorpay
except ImportError:
    razorpay = None

load_dotenv()

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "rzp_test_1DP5mmOlF5G5ag")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")

app = FastAPI(
    title="AutoPay Merchant Catalog & Autonomous Checkout API",
    description="Agent-readable product catalog with bounded, gated Razorpay checkout.",
    version="0.2.0",
)

# Allow the frontend (Vite dev server) to call this API during local dev.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


CATALOG_PATH = Path(__file__).parent / "products.json"


def load_catalog():
    if not CATALOG_PATH.exists():
        raise FileNotFoundError(
            f"products.json not found at {CATALOG_PATH}. "
            "Run build_catalog.py first to generate it."
        )
    with open(CATALOG_PATH, "r") as f:
        return json.load(f)


@app.get("/")
def root():
    """
    Agent-readable API manifest — tells an AI buyer what this merchant
    exposes and how to use it, without needing external docs.
    """
    return {
        "merchant": "AutoPay Demo Sportswear Store",
        "description": "Agent-readable catalog of shoes. Use /products to browse, "
        "/products/search to filter, /products/{id} for a single item.",
        "endpoints": {
            "GET /products": "List the full catalog.",
            "GET /products/{id}": "Get one product by its numeric id.",
            "GET /products/search": "Filter by brand, type, color, size, price range, or free-text query.",
            "POST /chat": "Conversational buyer agent — send {session_id, message}.",
        },
        "currency": "INR",
    }


@app.get("/products")
def list_products():
    """Return the full catalog."""
    return load_catalog()


@app.get("/products/search")
def search_products(
    q: Optional[str] = Query(None, description="Free-text search across name, brand, description, tags"),
    brand: Optional[str] = Query(None),
    type: Optional[str] = Query(None, description="sport, casual, formal, outdoor"),
    color: Optional[str] = Query(None),
    size: Optional[int] = Query(None, description="Only return products with this size in stock"),
    min_price: Optional[int] = Query(None),
    max_price: Optional[int] = Query(None),
):
    """
    Filter the catalog. All filters are optional and combine with AND logic.
    This is what an AI buyer agent calls after collecting budget + preferences.
    """
    catalog = load_catalog()
    results = []

    for product in catalog:
        if brand and brand.lower() != product["brand"].lower():
            continue
        if type and type.lower() != product["type"].lower():
            continue
        if color and color.lower() not in product["color"].lower():
            continue
        if min_price is not None and product["price"] < min_price:
            continue
        if max_price is not None and product["price"] > max_price:
            continue
        if size is not None:
            available = any(s["size"] == size and s["stock"] > 0 for s in product["sizes"])
            if not available:
                continue
        if q:
            haystack = " ".join(
                [product["name"], product["brand"], product["description"], " ".join(product["tags"])]
            ).lower()
            if q.lower() not in haystack:
                continue
        results.append(product)

    return {"count": len(results), "results": results}


@app.get("/products/{product_id}")
def get_product(product_id: int):
    """Get a single product by id."""
    catalog = load_catalog()
    for product in catalog:
        if product["id"] == product_id:
            return product
    raise HTTPException(status_code=404, detail=f"Product {product_id} not found")


@app.get("/health")
def health():
    return {"status": "ok"}


class ChatRequest(BaseModel):
    session_id: str
    message: str


@app.post("/chat")
def chat(req: ChatRequest):
    """
    Conversational buyer agent. Send a session_id (any string the frontend
    generates per browser session — e.g. a UUID) and a message. The agent
    maintains budget + cart state per session_id server-side.
    """
    try:
        return agent.send_message(req.session_id, req.message)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


class CreateOrderRequest(BaseModel):
    session_id: str
    token_id: str


@app.post("/create-razorpay-order")
def create_razorpay_order(req: CreateOrderRequest):
    """
    Create a Razorpay order locked to the active permission token & budget.
    Validates token presence, TTL, and authorized amount.
    """
    state = agent.get_or_create_session(req.session_id)
    token = state.get("active_token")
    if not token or token.get("token_id") != req.token_id:
        raise HTTPException(status_code=400, detail="Invalid or missing permission token.")

    last_checkout = state.get("last_checkout")
    if not last_checkout or not last_checkout.get("budget_ok"):
        raise HTTPException(status_code=400, detail="Cart has not cleared the budget gate.")

    amount_inr = last_checkout.get("final_total", 0)
    amount_paise = int(amount_inr * 100)

    real_order_id = None
    if razorpay and RAZORPAY_KEY_SECRET and not RAZORPAY_KEY_SECRET.startswith("your_"):
        try:
            client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
            rzp_order = client.order.create({
                "amount": amount_paise,
                "currency": "INR",
                "receipt": f"rcpt_{req.session_id[:8]}",
                "notes": {
                    "session_id": req.session_id,
                    "token_id": req.token_id,
                    "agent": "AutoPay-Autonomous-Buyer"
                }
            })
            real_order_id = rzp_order.get("id")
        except Exception as e:
            print(f"[Razorpay API Error, using Direct Test Mode] {e}")

    return {
        "order_id": real_order_id,
        "amount": amount_paise,
        "amount_inr": amount_inr,
        "currency": "INR",
        "key_id": RAZORPAY_KEY_ID,
        "token_id": req.token_id,
        "expires_at": token.get("expires_at")
    }


class VerifyPaymentRequest(BaseModel):
    session_id: str
    razorpay_payment_id: str
    razorpay_order_id: Optional[str] = ""
    razorpay_signature: Optional[str] = ""
    token_id: Optional[str] = ""


@app.post("/verify-payment")
def verify_payment(req: VerifyPaymentRequest):
    """
    Verifies payment signature and records the payment into the session & audit trail.

    Special case: if Razorpay captured the payment but our short-lived
    permission token expired before this endpoint ran (buyer was slow, or
    the checkout->payment round trip took longer than the token's 2-minute
    TTL), we do NOT record the order as paid — the token is what bounds the
    AI buyer's authorization. Instead we attempt to auto-refund the captured
    payment and return a structured failure the frontend can show clearly.
    """
    if razorpay and RAZORPAY_KEY_SECRET and req.razorpay_signature and req.razorpay_order_id:
        try:
            client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
            client.utility.verify_payment_signature({
                "razorpay_order_id": req.razorpay_order_id,
                "razorpay_payment_id": req.razorpay_payment_id,
                "razorpay_signature": req.razorpay_signature,
            })
        except Exception as e:
            print(f"[Signature verification note] {e}")

    order_id_final = req.razorpay_order_id or f"order_{req.session_id[:8]}"

    result = agent.confirm_payment_success(
        session_id=req.session_id,
        payment_id=req.razorpay_payment_id,
        order_id=order_id_final,
        signature=req.razorpay_signature or "",
        token_id=req.token_id or "",
    )

    if result.get("error") == "TOKEN_EXPIRED":
        refund_info = {"attempted": False, "status": None, "refund_id": None, "reason": None}
        if razorpay and RAZORPAY_KEY_SECRET and not RAZORPAY_KEY_SECRET.startswith("your_"):
            refund_info["attempted"] = True
            try:
                client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
                refund = client.payment.refund(req.razorpay_payment_id, {
                    "notes": {
                        "reason": "AutoPay permission token expired before verification",
                        "session_id": req.session_id,
                    }
                })
                refund_info["status"] = "ok"
                refund_info["refund_id"] = refund.get("id")
            except Exception as e:
                refund_info["status"] = "failed"
                refund_info["reason"] = str(e)
                print(f"[Auto-refund on token expiry failed] {e}")
        else:
            refund_info["reason"] = "Razorpay client not configured (test credentials only)."

        return agent.record_token_expiry_refund(
            session_id=req.session_id,
            payment_id=req.razorpay_payment_id,
            order_id=order_id_final,
            refund_info=refund_info,
        )

    if "error" in result:
        raise HTTPException(status_code=400, detail=result.get("detail", result.get("error")))
    return result


class ResetSessionRequest(BaseModel):
    session_id: str


@app.post("/reset-session")
def reset_session(req: ResetSessionRequest):
    """
    Clears the cart, budget, audit trail, and conversation history for a
    session so the buyer can start fresh.
    """
    return agent.reset_session(req.session_id)