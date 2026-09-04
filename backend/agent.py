"""
AutoPay conversational buyer agent — powered by Groq.

Groq's SDK is OpenAI-compatible and does NOT auto-execute tool calls, so this
file runs the tool-call loop manually: send the conversation, check if the
model asked to call a tool, run it, feed the result back, repeat until the
model gives a plain text reply.

Budget enforcement, inventory checks, and the final price breakdown all
happen in code here — not narrated by the model.
"""

import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

API_KEY = os.environ.get("GROQ_API_KEY")
MODEL_NAME = "openai/gpt-oss-20b"
MAX_TOOL_ROUNDS = 9
TOKEN_TTL_SECONDS = 120  # 2 minutes validity




CATALOG_PATH = Path(__file__).parent / "products.json"
ACCESSORIES_PATH = Path(__file__).parent / "accessories.json"

COUPONS = {
    "WELCOME10": {"type": "percent", "value": 10, "min_cart": 0, "description": "10% off for new buyers"},
    "FLAT100": {"type": "flat", "value": 100, "min_cart": 2000, "description": "Flat ₹100 off on carts ₹2000+"},
    "LOYALTY5": {"type": "percent", "value": 5, "min_cart": 0, "description": "5% loyalty discount"},
}

FREE_DELIVERY_THRESHOLD = 1999
FLAT_DELIVERY_FEE = 99
MAX_STACKED_DISCOUNT_PCT = 30

# Read-only status checks — not Budget/Reasoning/Execution/Negotiation/
# Offers/Delivery events, so they're excluded from the audit log entirely.
AUDIT_EXCLUDED_TOOLS = {"get_cart_status", "get_addon_recommendations"}

SYSTEM_PROMPT = """You are the AutoPay AI shopping agent for a demo sportswear store.

Your job: help the buyer find shoes within their budget, and NEVER let the cart
exceed the budget — the add_to_cart tool enforces this in code, so if it refuses,
tell the buyer clearly why (which item, how much it would have exceeded by) and
suggest cheaper alternatives from the catalog.

Flow to follow:
1. If the buyer hasn't stated a budget yet, ask for one before showing products.
2. Ask about preferences (brand, type, color, size, price range) if not given.
3. Use search_products to find matches — never invent products that aren't in
   the catalog.

CRITICAL — how search_products filters actually work:
  - `brand` matches exactly ONE brand name. `query` is a single substring
    match against each product's name/brand/description/tags.
  - NEVER combine multiple brand names into one query or brand value (e.g.
    query="Adidas Puma Bata" will NEVER match anything — it looks for that
    literal phrase). If the buyer names multiple acceptable brands, call
    search_products once per brand (a separate tool call for each), then
    combine the results yourself when replying.
  - If a search returns zero results, do NOT immediately retry with a
    different color/attribute in a tight loop. First drop the LEAST
    important filter (usually color, then price) and try once more. If it's
    still empty after that, stop searching and tell the buyer plainly what
    you couldn't find, then ask them to relax one constraint (budget,
    brand, or color) — don't keep guessing silently.

4. When the buyer wants a shoe, call add_to_cart. Report the real result.

CRITICAL — resolving short replies:
After you show search results, the buyer will often reply briefly: a bare
size ("8"), a partial product name ("comfit"), a brand, or "add X". Do NOT
treat these as a new, unrelated search. Instead:
  - Match the reply against the product_id + name you already have from your
    most recent search_products tool result in this conversation.
  - If the buyer only gave a size and exactly one product is in play (or was
    just discussed), use that product's id with the given size and call
    add_to_cart immediately.
  - If the buyer named a product (even partially, e.g. "comfit" matching
    "Comfit Formal Slip-on"), use that product's id. If you don't yet have
    its size, ask for the size, then call add_to_cart on their next reply —
    do not call search_products again for a product you already found.
  - Only call search_products again if the buyer's reply clearly changes the
    criteria (different brand/type/color/price) or if none of the
    previously shown items match what they now said.
  - Never reply with the exact same product list and question twice in a
    row — if you catch yourself about to do that, call add_to_cart instead.
5. If budget remains after a primary pick, proactively call
   get_addon_recommendations and suggest one relevant accessory (socks,
   insoles, cleaning kit, etc.) that fits the remaining budget. If the buyer
   wants to add one, call add_addon_to_cart (NOT add_to_cart — accessories
   have no size).
6. If the buyer asks about deals, offers, or coupons, mention that WELCOME10
   (10% off), FLAT100 (₹100 off carts ₹2000+), and LOYALTY5 (5% off) exist,
   and call apply_coupon with whichever code they choose.
7. If the buyer asks to negotiate, haggle, or get a better price, call
   negotiate_discount. Report the real outcome — don't invent a discount
   percentage yourself.
8. When the buyer wants to check out or see the final price, call
   start_checkout. This re-verifies inventory and budget in code and
   returns the full breakdown (subtotal, discounts, CGST, SGST, delivery,
   final total). Present this breakdown clearly, item by item.
9. If start_checkout returns an inventory or budget failure, explain exactly
   why and suggest a fix (remove an item, apply a coupon, negotiate, or
   increase budget) — do not pretend it succeeded.
10. IMPORTANT: there is no payment-execution tool yet. After a successful
    start_checkout, tell the buyer the order is verified and ready, but do
    NOT claim that payment has actually been completed — that capability
    doesn't exist yet.

Use get_cart_status whenever the buyer asks what's in their cart or how much
budget is left. Be concise and conversational — a few sentences per turn, not
a wall of text. Always speak in INR (₹).
"""

client = Groq(api_key=API_KEY) if API_KEY else None

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "Search the shoe catalog. All arguments are optional filters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Free-text search across name, brand, description, tags."},
                    "brand": {"type": "string", "description": 'Exact brand name, e.g. "Nike".'},
                    "type": {"type": "string", "description": "Shoe type — sport, casual, formal, or outdoor."},
                    "color": {"type": "string", "description": "Substring match on color."},
                    "size": {"type": "integer", "description": "Only return products with this size in stock."},
                    "max_price": {"type": "integer", "description": "Maximum price in INR."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_budget",
            "description": "Set the buyer's total shopping budget in INR for this session.",
            "parameters": {
                "type": "object",
                "properties": {"amount": {"type": "integer", "description": "Budget amount in INR, must be positive."}},
                "required": ["amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_cart",
            "description": (
                "Add a SHOE to the cart in a chosen size. This ENFORCES the budget in "
                "code — it will refuse if the item would exceed budget or if the size is "
                "out of stock, regardless of what the conversation implied. Do NOT use this "
                "for accessories/add-ons (socks, insoles, etc.) — use add_addon_to_cart instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "integer", "description": "Numeric id from search_products."},
                    "size": {"type": "integer", "description": "The chosen shoe size."},
                },
                "required": ["product_id", "size"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_addon_to_cart",
            "description": (
                "Add an ACCESSORY/add-on (socks, insoles, cleaning kit, waterproof spray, "
                "spare laces, shoe horn) to the cart. Accessories have no size. This ENFORCES "
                "the budget in code, same as add_to_cart. Use the id from "
                "get_addon_recommendations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "accessory_id": {"type": "integer", "description": "Numeric id from get_addon_recommendations."},
                },
                "required": ["accessory_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_from_cart",
            "description": "Remove a product from the cart by its id.",
            "parameters": {
                "type": "object",
                "properties": {"product_id": {"type": "integer"}},
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_cart_status",
            "description": "Return the current cart contents, amount spent, and remaining budget.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_addon_recommendations",
            "description": "Return accessory add-ons (socks, insoles, cleaning kit, etc.) that fit within the remaining budget.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_coupon",
            "description": "Apply a coupon code to the cart. Valid codes: WELCOME10, FLAT100, LOYALTY5.",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "The coupon code, e.g. WELCOME10."}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "negotiate_discount",
            "description": (
                "Negotiate a discount with the seller. The seller's max offer scales with cart "
                "size and is decided by code, not by you. Only usable once per session."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "requested_discount_percent": {
                        "type": "number",
                        "description": "What the buyer is asking for, if they gave a number. Omit if they just asked generically for a discount.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_checkout",
            "description": (
                "Re-verify inventory and budget in code, then compute the final price breakdown "
                "(subtotal, discounts, CGST, SGST, delivery, final total). Call this when the buyer "
                "wants to check out or see the final price."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


_CATALOG_CACHE = None
_ACCESSORIES_CACHE = None


def _load_catalog():
    global _CATALOG_CACHE
    if _CATALOG_CACHE is None:
        with open(CATALOG_PATH, "r") as f:
            _CATALOG_CACHE = json.load(f)
    return _CATALOG_CACHE


def _load_accessories():
    global _ACCESSORIES_CACHE
    if _ACCESSORIES_CACHE is None:
        with open(ACCESSORIES_PATH, "r") as f:
            _ACCESSORIES_CACHE = json.load(f)
    return _ACCESSORIES_CACHE


def _gst_rate(price: float, category: str) -> float:
    """Simplified Indian GST slabs. Footwear <=1000 = 5%, >1000 = 18%.
    Add-ons taxed at a flat 12% for simplicity."""
    if category == "addon":
        return 0.12
    return 0.05 if price <= 1000 else 0.18


def _generate_permission_token(session_id: str, final_total: int) -> dict:
    token_id = f"tok_{secrets.token_hex(6)}"
    issued_at = int(time.time())
    expires_at = issued_at + TOKEN_TTL_SECONDS
    raw = f"{session_id}:{final_total}:{expires_at}:{token_id}"
    token_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return {
        "token_id": token_id,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "ttl_seconds": TOKEN_TTL_SECONDS,
        "token_hash": token_hash,
        "status": "ACTIVE",
        "authorized_amount": final_total,
    }



def make_tool_funcs(state: dict):
    """
    Build a name -> function dispatch dict, bound to a single session's
    mutable state. state shape:
        {"budget", "cart", "coupon", "negotiated_discount_pct", "audit", "messages"}
    """

    def search_products(query="", brand="", type="", color="", size=0, max_price=0):
        catalog = _load_catalog()
        results = []
        for p in catalog:
            if brand and brand.lower() != p["brand"].lower():
                continue
            if type and type.lower() != p["type"].lower():
                continue
            if color and color.lower() not in p["color"].lower():
                continue
            if max_price and p["price"] > max_price:
                continue
            if size:
                if not any(s["size"] == size and s["stock"] > 0 for s in p["sizes"]):
                    continue
            if query:
                haystack = " ".join(
                    [p["name"], p["brand"], p["description"], " ".join(p["tags"])]
                ).lower()
                if query.lower() not in haystack:
                    continue
            results.append(
                {
                    "id": p["id"],
                    "brand": p["brand"],
                    "name": p["name"],
                    "type": p["type"],
                    "color": p["color"],
                    "price": p["price"],
                    "sizes_in_stock": [s["size"] for s in p["sizes"] if s["stock"] > 0],
                    "description": p["description"],
                }
            )
        return {"count": len(results), "results": results[:5]}

    def set_budget(amount):
        amount = int(amount)
        if amount <= 0:
            return {"error": "Invalid budget — must be a positive number."}
        state["budget"] = amount
        return {"ok": True, "budget": amount}

    def add_to_cart(product_id, size):
        product_id = int(product_id)
        size = int(size)
        if state.get("budget") is None:
            return {"error": "No budget set yet — ask the buyer for their budget first."}

        catalog = _load_catalog()
        product = next((p for p in catalog if p["id"] == product_id), None)
        if not product:
            return {"error": f"No product with id {product_id} exists in the catalog."}

        size_entry = next((s for s in product["sizes"] if s["size"] == size), None)
        if not size_entry or size_entry["stock"] <= 0:
            available = [s["size"] for s in product["sizes"] if s["stock"] > 0]
            return {"error": f"Size {size} of {product['name']} is out of stock.", "available_sizes": available}

        spent = sum(item["price"] for item in state["cart"])
        if spent + product["price"] > state["budget"]:
            over = spent + product["price"] - state["budget"]
            return {
                "error": "BLOCKED_BY_MANDATE_LEDGER",
                "detail": (
                    f"Adding {product['name']} (₹{product['price']}) would exceed the "
                    f"₹{state['budget']} budget by ₹{over}. Not added."
                ),
            }

        state["cart"].append(
            {"id": product["id"], "name": product["name"], "price": product["price"], "size": size, "category": "shoe"}
        )
        remaining = state["budget"] - (spent + product["price"])
        return {
            "ok": True,
            "added": product["name"],
            "size": size,
            "price": product["price"],
            "remaining_budget": remaining,
        }

    def add_addon_to_cart(accessory_id):
        accessory_id = int(accessory_id)
        if state.get("budget") is None:
            return {"error": "No budget set yet — ask the buyer for their budget first."}

        accessories = _load_accessories()
        accessory = next((a for a in accessories if a["id"] == accessory_id), None)
        if not accessory:
            return {"error": f"No accessory with id {accessory_id} exists in the catalog."}

        spent = sum(item["price"] for item in state["cart"])
        if spent + accessory["price"] > state["budget"]:
            over = spent + accessory["price"] - state["budget"]
            return {
                "error": "BLOCKED_BY_MANDATE_LEDGER",
                "detail": (
                    f"Adding {accessory['name']} (₹{accessory['price']}) would exceed the "
                    f"₹{state['budget']} budget by ₹{over}. Not added."
                ),
            }

        state["cart"].append(
            {"id": accessory["id"], "name": accessory["name"], "price": accessory["price"], "category": "addon"}
        )
        remaining = state["budget"] - (spent + accessory["price"])
        return {
            "ok": True,
            "added": accessory["name"],
            "price": accessory["price"],
            "remaining_budget": remaining,
        }

    def remove_from_cart(product_id):
        product_id = int(product_id)
        before = len(state["cart"])
        state["cart"] = [item for item in state["cart"] if item["id"] != product_id]
        if len(state["cart"]) == before:
            return {"error": f"No item with id {product_id} was in the cart."}
        return {"ok": True}

    def get_cart_status():
        spent = sum(item["price"] for item in state["cart"])
        budget = state.get("budget")
        remaining = (budget - spent) if budget is not None else None
        return {"budget": budget, "spent": spent, "remaining": remaining, "cart": state["cart"]}

    def get_addon_recommendations():
        budget = state.get("budget")
        if budget is None:
            return {"error": "No budget set yet."}
        spent = sum(item["price"] for item in state["cart"])
        remaining = budget - spent
        addons = _load_accessories()
        affordable = [a for a in addons if a["price"] <= remaining]
        return {"remaining_budget": remaining, "recommendations": affordable}

    def apply_coupon(code):
        code = str(code).upper().strip()
        coupon = COUPONS.get(code)
        if not coupon:
            return {"error": f"Coupon '{code}' is not valid."}
        subtotal = sum(item["price"] for item in state["cart"])
        if subtotal < coupon["min_cart"]:
            return {
                "error": (
                    f"Coupon '{code}' requires a cart of at least ₹{coupon['min_cart']} "
                    f"(currently ₹{subtotal})."
                )
            }
        state["coupon"] = {"code": code, **coupon}
        return {"ok": True, "code": code, "description": coupon["description"]}

    def negotiate_discount(requested_discount_percent=0):
        requested_discount_percent = float(requested_discount_percent or 0)
        subtotal = sum(item["price"] for item in state["cart"])
        if subtotal <= 0:
            return {"error": "Cart is empty — add items before negotiating."}
        if state.get("negotiated_discount_pct") is not None:
            return {
                "error": "A negotiated discount has already been applied this session.",
                "current_discount_pct": state["negotiated_discount_pct"],
            }
        if subtotal >= 8000:
            max_offer = 10
        elif subtotal >= 5000:
            max_offer = 7
        elif subtotal >= 2000:
            max_offer = 4
        else:
            max_offer = 0
        if max_offer == 0:
            return {
                "ok": False,
                "offer_pct": 0,
                "message": "Cart total is too low to qualify for a negotiated discount right now.",
            }
        agreed = min(requested_discount_percent, max_offer) if requested_discount_percent > 0 else max_offer
        state["negotiated_discount_pct"] = agreed
        return {
            "ok": True,
            "requested_pct": requested_discount_percent,
            "max_offer_pct": max_offer,
            "agreed_pct": agreed,
        }

    def start_checkout():
        if not state["cart"]:
            return {"error": "Cart is empty — add items before checking out."}
        if state.get("budget") is None:
            return {"error": "No budget set yet."}

        catalog = _load_catalog()
        catalog_by_id = {p["id"]: p for p in catalog}
        accessories = _load_accessories()
        accessories_by_id = {a["id"]: a for a in accessories}

        inventory_issues = []
        items_detail = []
        for item in state["cart"]:
            if item.get("category") == "addon":
                accessory = accessories_by_id.get(item["id"])
                if not accessory:
                    inventory_issues.append(f"{item['name']} no longer exists in the catalog.")
                    continue
                items_detail.append(
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "size": None,
                        "price": item["price"],
                        "brand": "Accessory",
                        "type": "addon",
                        "category": "addon",
                    }
                )
                continue

            product = catalog_by_id.get(item["id"])
            if not product:
                inventory_issues.append(f"{item['name']} no longer exists in the catalog.")
                continue
            size_entry = next((s for s in product["sizes"] if s["size"] == item["size"]), None)
            if not size_entry or size_entry["stock"] <= 0:
                inventory_issues.append(f"{item['name']} (size {item['size']}) is now out of stock.")
            items_detail.append(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "size": item["size"],
                    "price": item["price"],
                    "brand": product["brand"],
                    "type": product["type"],
                    "category": "shoe",
                }
            )

        if inventory_issues:
            return {"error": "INVENTORY_CHECK_FAILED", "issues": inventory_issues}

        subtotal = sum(i["price"] for i in items_detail)

        flat_discount = 0
        percent_discount = 0.0
        coupon = state.get("coupon")
        if coupon:
            if coupon["type"] == "flat":
                flat_discount += coupon["value"]
            else:
                percent_discount += coupon["value"]
        negotiated_pct = state.get("negotiated_discount_pct") or 0
        percent_discount += negotiated_pct
        percent_discount = min(percent_discount, MAX_STACKED_DISCOUNT_PCT)

        percent_discount_amount = round(subtotal * percent_discount / 100)
        total_discount = flat_discount + percent_discount_amount
        discounted_subtotal = max(subtotal - total_discount, 0)

        cgst_total = 0.0
        sgst_total = 0.0
        for i in items_detail:
            item_share = (i["price"] / subtotal) * discounted_subtotal if subtotal else 0
            rate = _gst_rate(i["price"], i["category"])
            gst_amount = item_share * rate
            cgst_total += gst_amount / 2
            sgst_total += gst_amount / 2
        cgst_total = round(cgst_total)
        sgst_total = round(sgst_total)

        delivery_fee = 0 if discounted_subtotal >= FREE_DELIVERY_THRESHOLD else FLAT_DELIVERY_FEE
        final_total = discounted_subtotal + cgst_total + sgst_total + delivery_fee

        budget = state["budget"]
        budget_ok = final_total <= budget

        breakdown = {
            "items": items_detail,
            "subtotal": subtotal,
            "flat_discount": flat_discount,
            "percent_discount_pct": percent_discount,
            "percent_discount_amount": percent_discount_amount,
            "total_discount": total_discount,
            "discounted_subtotal": discounted_subtotal,
            "cgst": cgst_total,
            "sgst": sgst_total,
            "delivery_fee": delivery_fee,
            "final_total": final_total,
            "budget": budget,
            "budget_ok": budget_ok,
            "inventory_ok": True,
        }

        if not budget_ok:
            breakdown["error"] = "BUDGET_CHECK_FAILED_AT_CHECKOUT"
            breakdown["over_by"] = final_total - budget
            return breakdown

        token = _generate_permission_token(state.get("session_id", "default"), final_total)
        breakdown["token"] = token
        state["active_token"] = token
        state["last_checkout"] = breakdown
        return breakdown

    return {
        "search_products": search_products,
        "set_budget": set_budget,
        "add_to_cart": add_to_cart,
        "add_addon_to_cart": add_addon_to_cart,
        "remove_from_cart": remove_from_cart,
        "get_cart_status": get_cart_status,
        "get_addon_recommendations": get_addon_recommendations,
        "apply_coupon": apply_coupon,
        "negotiate_discount": negotiate_discount,
        "start_checkout": start_checkout,
    }


# In-memory per-session state. Fine for a hackathon demo; not persistent
# across backend restarts.
SESSIONS: dict[str, dict] = {}


def get_or_create_session(session_id: str):
    if session_id not in SESSIONS:
        SESSIONS[session_id] = {
            "session_id": session_id,
            "budget": None,
            "cart": [],
            "coupon": None,
            "negotiated_discount_pct": None,
            "last_checkout": None,
            "active_token": None,
            "payment": None,
            "audit": [],
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}],
        }
    return SESSIONS[session_id]


def _explain(fn_name: str, args: dict, result: dict) -> str:
    """Short, single-line reasoning for the audit log — action + why, nothing more."""
    if fn_name == "search_products":
        filters = ", ".join(f"{k}={v}" for k, v in args.items() if v) or "no filters"
        return f"Searched ({filters}) — {result.get('count', 0)} found."
    if fn_name == "set_budget":
        if "error" in result:
            return f"Rejected: {result['error']}"
        return f"Budget set to ₹{result.get('budget')}."
    if fn_name == "add_to_cart":
        if result.get("error") == "BLOCKED_BY_MANDATE_LEDGER":
            return f"BLOCKED — over budget, not added."
        if "error" in result:
            return f"Not added: {result['error']}"
        return f"Added {result.get('added')} (size {result.get('size')}) — ₹{result.get('remaining_budget')} left."
    if fn_name == "add_addon_to_cart":
        if result.get("error") == "BLOCKED_BY_MANDATE_LEDGER":
            return f"BLOCKED — over budget, not added."
        if "error" in result:
            return f"Not added: {result['error']}"
        return f"Added {result.get('added')} (add-on) — ₹{result.get('remaining_budget')} left."
    if fn_name == "remove_from_cart":
        if "error" in result:
            return result["error"]
        return "Removed from cart."
    if fn_name == "apply_coupon":
        if "error" in result:
            return f"Coupon rejected: {result['error']}"
        return f"Coupon {result['code']} applied."
    if fn_name == "negotiate_discount":
        if "error" in result:
            return f"Negotiation rejected: {result['error']}"
        if not result.get("ok"):
            return "Negotiation declined — cart too small."
        return f"Negotiated {result['agreed_pct']}% discount."
    if fn_name == "start_checkout":
        if result.get("error") == "INVENTORY_CHECK_FAILED":
            return "BLOCKED — inventory check failed."
        if result.get("error") == "BUDGET_CHECK_FAILED_AT_CHECKOUT":
            return f"BLOCKED — final total over budget by ₹{result['over_by']}."
        delivery_fee = result.get("delivery_fee", 0)
        delivery_note = "free delivery" if delivery_fee == 0 else f"₹{delivery_fee} delivery"
        token_info = result.get("token", {})
        token_str = f" · Token {token_info.get('token_id', '')} (2m TTL)" if token_info else ""

        return f"Verified — final total ₹{result.get('final_total')} ({delivery_note}), within budget{token_str}."

    if fn_name == "confirm_payment":
        if "error" in result:
            return f"Payment failed: {result['error']}"
        return f"Payment of ₹{result.get('amount')} authorized via Razorpay (ID: {result.get('payment_id')}). Order fulfilled."
    if fn_name == "payment_declined":
        reason = result.get("reason") or "declined by bank"
        return f"Payment {args.get('payment_id') or '(none)'} rejected by Razorpay/bank ({reason}). No charge made, cart preserved."
    if fn_name == "token_expired":
        return f"Security Gate: Permission token {args.get('token_id')} expired before authorization."
    if fn_name == "token_expired_after_payment":
        refund = result.get("refund", {})
        if refund.get("attempted") and refund.get("status") == "ok":
            return (
                f"Security Gate: Razorpay captured payment {args.get('payment_id')} but the "
                f"permission token had already expired. Order REJECTED — refund "
                f"{refund.get('refund_id')} auto-initiated."
            )
        if refund.get("attempted"):
            return (
                f"Security Gate: Razorpay captured payment {args.get('payment_id')} but the "
                f"permission token had already expired. Order REJECTED — automatic refund "
                f"attempt failed, needs manual reconciliation."
            )
        return (
            f"Security Gate: Payment {args.get('payment_id')} arrived after the permission "
            f"token expired. Order REJECTED, no refund attempted (test-mode payment, no real "
            f"funds captured)."
        )
    return f"Called {fn_name}."


def _log_audit(state: dict, fn_name: str, args: dict, result: dict) -> None:
    state.setdefault("audit", []).append(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "action": fn_name,
            "input": args,
            "result": result,
            "explanation": _explain(fn_name, args, result),
        }
    )


def _cart_summary(state: dict) -> dict:
    spent = sum(item["price"] for item in state["cart"])
    budget = state.get("budget")
    remaining = (budget - spent) if budget is not None else None
    return {
        "budget": budget,
        "spent": spent,
        "remaining": remaining,
        "cart": state["cart"],
        "audit": state.get("audit", []),
        "checkout": state.get("last_checkout"),
        "active_token": state.get("active_token"),
        "payment": state.get("payment"),
    }


def record_payment_failure(
    session_id: str,
    payment_id: str,
    order_id: str,
    error_code: str = "",
    error_description: str = "",
    error_reason: str = "",
) -> dict:
    """
    Called when Razorpay's own checkout widget reports payment.failed (e.g.
    the bank declined the card, insufficient funds in test mode, etc.).
    Unlike the token-expiry case, no money was ever captured here — nothing
    to refund — but it still needs to be a visible, explainable entry in the
    audit trail rather than silently vanishing when the buyer closes the
    Razorpay modal.
    """
    state = get_or_create_session(session_id)
    _log_audit(
        state,
        "payment_declined",
        {"payment_id": payment_id, "order_id": order_id, "error_code": error_code},
        {
            "error": "PAYMENT_DECLINED",
            "detail": error_description or "Payment was declined.",
            "reason": error_reason,
        },
    )
    return {
        "ok": False,
        "failure_reason": "PAYMENT_DECLINED",
        "detail": error_description or "Your payment was declined by the bank. No charge was made.",
        "error_code": error_code,
        "payment_id": payment_id,
        **_cart_summary(state),
    }


def record_token_expiry_refund(session_id: str, payment_id: str, order_id: str, refund_info: dict) -> dict:
    """
    Called when Razorpay actually captured a payment but our short-lived
    permission token had already expired by the time /verify-payment ran.
    This does NOT record the order as paid — the token is the buyer's bound
    authorization, and letting a late verification slip through would defeat
    the point of gating on it. Instead it logs the mismatch and the refund
    outcome (if a refund was attempted) so there's a clear, explainable
    audit trail of the failure.
    """
    state = get_or_create_session(session_id)
    _log_audit(
        state,
        "token_expired_after_payment",
        {"payment_id": payment_id, "order_id": order_id},
        {
            "error": "TOKEN_EXPIRED",
            "detail": (
                "Razorpay captured this payment, but the 2-minute permission "
                "token had already expired before verification. The order was "
                "NOT accepted."
            ),
            "refund": refund_info,
        },
    )
    return {
        "ok": False,
        "failure_reason": "TOKEN_EXPIRED",
        "detail": (
            "Your permission token expired before the payment could be verified in time. "
            "The order was not placed."
            + (
                f" A refund was automatically initiated (refund id {refund_info.get('refund_id')})."
                if refund_info.get("attempted") and refund_info.get("status") == "ok"
                else ""
            )
        ),
        "refund": refund_info,
        "payment_id": payment_id,
        **_cart_summary(state),
    }


def confirm_payment_success(session_id: str, payment_id: str, order_id: str, signature: str = "", token_id: str = "") -> dict:
    """Confirm a successful Razorpay payment, enforce token TTL, and log to the audit trail."""
    state = get_or_create_session(session_id)
    token = state.get("active_token")
    if not token:
        return {"error": "NO_ACTIVE_TOKEN", "detail": "No active checkout permission token found."}
    
    if token_id and token.get("token_id") != token_id:
        return {"error": "TOKEN_MISMATCH", "detail": "Permission token does not match active session token."}

    if time.time() > token.get("expires_at", 0):
        token["status"] = "EXPIRED"
        # NOTE: the caller (main.py /verify-payment) is responsible for
        # attempting a refund and logging via record_token_expiry_refund —
        # not logged here to avoid a duplicate audit entry.
        return {
            "error": "TOKEN_EXPIRED",
            "detail": "Permission token expired. Please re-run checkout to generate a fresh token.",
            **_cart_summary(state)
        }

    token["status"] = "REDEEMED"
    last_chk = state.get("last_checkout") or {}
    amount = last_chk.get("final_total", 0)

    # Compute estimated delivery date (5–7 business days from now)
    from datetime import timedelta
    today = datetime.now(timezone.utc)
    delivery_min = (today + timedelta(days=5)).strftime("%d %b %Y")
    delivery_max = (today + timedelta(days=7)).strftime("%d %b %Y")

    payment_data = {
        "payment_id": payment_id,
        "order_id": order_id,
        "signature": signature,
        "amount": amount,
        "status": "PAID",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "delivery_estimate": f"{delivery_min} – {delivery_max}",
        "items": last_chk.get("items", state.get("cart", [])),
        "subtotal": last_chk.get("subtotal", amount),
        "gst": last_chk.get("total_gst", 0),
        "delivery_fee": last_chk.get("delivery_fee", 0),
        "coupon_discount": last_chk.get("coupon_discount", 0),
        "volume_discount": last_chk.get("volume_discount", 0),
    }
    state["payment"] = payment_data

    _log_audit(
        state,
        "confirm_payment",
        {"payment_id": payment_id, "order_id": order_id, "token_id": token.get("token_id")},
        {"ok": True, "payment_id": payment_id, "amount": amount, "status": "PAID"}
    )
    return {"ok": True, "payment": payment_data, **_cart_summary(state)}



def reset_session(session_id: str) -> dict:
    """Reset the session cart, conversation, and audit trail for a fresh start."""
    if session_id in SESSIONS:
        SESSIONS[session_id] = {
            "session_id": session_id,
            "budget": None,
            "cart": [],
            "coupon": None,
            "negotiated_discount_pct": None,
            "last_checkout": None,
            "active_token": None,
            "payment": None,
            "audit": [],
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}],
        }
    return _cart_summary(get_or_create_session(session_id))




MAX_HISTORY_TURNS = 10  # keep only the most recent N user turns of chat history


def _trim_history(state):
    """
    Caps state["messages"] to the system prompt + the most recent
    MAX_HISTORY_TURNS user turns (each turn = the user message plus any
    assistant tool-calls/tool-results/reply that followed it).

    This is the fix for the latency issue we diagnosed earlier: without it,
    the ENTIRE conversation (including every tool call and tool result ever
    made) gets resent to Groq on every single message, so requests get
    slower and slower the longer a session runs. Trimming always cuts at a
    user-message boundary, never mid tool-call/tool-result pair, so the API
    never sees an orphaned tool response (which Groq/OpenAI-style APIs
    reject).

    Trade-off: preferences stated more than MAX_HISTORY_TURNS turns ago
    (e.g. "size 8" mentioned in turn 2 of a 15-turn session) can fall out of
    context. Live cart/budget/coupon state is unaffected since those live in
    `state` directly, not in chat history.
    """
    messages = state["messages"]
    if len(messages) <= 1:
        return
    system_msg = messages[0]
    user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(user_indices) <= MAX_HISTORY_TURNS:
        return
    cutoff_index = user_indices[-MAX_HISTORY_TURNS]
    state["messages"] = [system_msg] + messages[cutoff_index:]


def send_message(session_id: str, message: str) -> dict:
    if client is None:
        return {
            "reply": "GROQ_API_KEY is not set. Add it to backend/.env as GROQ_API_KEY=your-key-here",
            "budget": None,
            "spent": 0,
            "remaining": None,
            "cart": [],
            "audit": [],
            "checkout": None,
        }

    state = get_or_create_session(session_id)
    tool_funcs = make_tool_funcs(state)
    _trim_history(state)
    state["messages"].append({"role": "user", "content": message})

    reply_text = None
    seen_search_calls = set()  # dedupe identical search_products calls within this turn
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=state["messages"],
                tools=TOOLS_SCHEMA,
                tool_choice="auto",
                temperature=0.3,
                max_completion_tokens=1024,
            )
            msg = response.choices[0].message
            # Store as a plain dict (not the raw SDK object) so history is
            # always safely serializable on the next round-trip to the API.
            assistant_msg = msg.model_dump(exclude_unset=True) if hasattr(msg, "model_dump") else msg
            state["messages"].append(assistant_msg)

            if not msg.tool_calls:
                reply_text = msg.content
                break

            for tool_call in msg.tool_calls:
                fn_name = tool_call.function.name
                try:
                    args = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                # Loop-breaker: if the model repeats an identical
                # search_products call within this turn, don't re-scan the
                # catalog for the same guaranteed-empty/duplicate result —
                # nudge it to broaden filters or stop instead.
                if fn_name == "search_products":
                    call_key = tuple(sorted(args.items()))
                    if call_key in seen_search_calls:
                        result = {
                            "count": 0,
                            "results": [],
                            "note": (
                                "You already ran this exact search this turn with the same "
                                "filters. Repeating it will not produce new results. Drop the "
                                "least important filter and try once more, or stop and ask the "
                                "buyer to relax a constraint."
                            ),
                        }
                        state["messages"].append(
                            {"role": "tool", "content": json.dumps(result), "tool_call_id": tool_call.id}
                        )
                        continue
                    seen_search_calls.add(call_key)

                fn = tool_funcs.get(fn_name)
                result = fn(**args) if fn else {"error": f"Unknown tool {fn_name}"}
                if fn_name not in AUDIT_EXCLUDED_TOOLS:
                    _log_audit(state, fn_name, args, result)
                state["messages"].append(
                    {
                        "role": "tool",
                        "content": json.dumps(result),
                        "tool_call_id": tool_call.id,
                    }
                )
        else:
            reply_text = "I got stuck in a loop trying to complete that — could you rephrase?"

    except Exception as e:
        print(f"[agent] Groq API error: {type(e).__name__}: {e}")
        msg_str = str(e)
        if "rate_limit" in msg_str.lower() or "429" in msg_str:
            reply_text = (
                "I'm being rate-limited by the AI provider right now. "
                "Please wait a moment and try again."
            )
        else:
            reply_text = "I hit an error talking to the AI provider and couldn't complete that. Please try again."

    return {"reply": reply_text, **_cart_summary(state)}