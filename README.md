# AutoPay
## About the Project

AutoPay is a demo sportswear storefront built to be shopped by an **AI buyer agent**, not just a human. It pairs an agent-readable product catalog with a conversational checkout experience: a buyer agent collects a budget and preferences, searches the catalog, builds a cart, and checks out through a Razorpay-based payment flow that is cryptographically gated by a short-lived permission token — so no AI agent can ever authorize a payment beyond what was explicitly agreed to.

## Key Features

- **Agent-readable catalog API** — `/products`, `/products/search`, `/products/{id}` are self-documented at `/` so an AI agent can discover how to shop without external docs.
- **Conversational buyer agent (`/chat`)** — Groq-powered chat agent that collects a budget, searches the catalog, manages a cart, recommends accessories, applies coupons, and negotiates discounts via tool calls, with budget/inventory rules enforced in code (not trusted to the LLM).
- **Full price breakdown** — subtotal, coupon discount, negotiated discount, CGST/SGST, delivery fee, and final total, computed server-side at checkout.
- **Bounded permission tokens** — a successful checkout issues a short-lived (2-minute) token scoped to an exact amount, so a late or mismatched payment can't be recorded as paid.
- **Razorpay integration** — test-mode order creation, checkout widget, signature verification, and payment-failure/token-expiry handling.
- **Session audit trail** — every budget change, search, cart action, coupon, negotiation, and checkout event is logged per session and shown live in the UI.

## Tech Stack

**Backend**
- Python 3.10+
- FastAPI (API framework)
- Uvicorn (ASGI server)
- Groq SDK (LLM-powered buyer agent)
- Razorpay Python SDK (payments)
- Pydantic (request/response validation)
- python-dotenv (environment config)

**Frontend**
- React 19
- Vite (build tool & dev server)
- ESLint (linting)
- Razorpay Checkout JS SDK

## Project Structure
```
AutoPay/
├── backend/
│   ├── __init__.py           
│   ├── main.py
│   ├── agent.py
│   ├── build_catalog.py
│   ├── products.json
│   ├── accessories.json
│   ├── requirements.txt
│   ├── .env.example
│   └── .env
├── frontend/
│   ├── public/
│   │   ├── favicon.svg
│   │   └── icons.svg
│   ├── src/
│   │   ├── assets/
│   │   │   ├── hero.png
│   │   │   ├── react.svg
│   │   │   └── vite.svg
│   │   ├── App.jsx
│   │   ├── App.css
│   │   ├── main.jsx
│   │   └── index.css
│   ├── index.html
│   ├── eslint.config.js
│   ├── .gitignore
│   ├── package.json
│   ├── package-lock.json
│   └── vite.config.js
└── README.md
```

## Prerequisites

- Python 3.10+
- Node.js 18+
- [Groq API key](https://console.groq.com/keys) (free tier works)
- [Razorpay](https://dashboard.razorpay.com/) test-mode key id/secret (optional — the app falls back to a direct test-mode flow without real Razorpay credentials)

## Setup

### 1. Backend

```bash
cd backend
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # then fill in GROQ_API_KEY (and Razorpay keys if you have them)
```

Generate the product catalog (only needed once, or after editing the source data in `build_catalog.py`):

```bash
python build_catalog.py
```

Run the API **from the project root** (not from inside `backend/`), since `main.py` imports the agent module as `backend.agent`:

```bash
cd ..
uvicorn backend.main:app --reload --port 8000
```

The API will be live at `http://127.0.0.1:8000` — visit `/` for the agent-readable manifest, or `/docs` for interactive Swagger docs.

### 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

The app will be available at `http://localhost:5173` and expects the backend at `http://127.0.0.1:8000` (see `API_BASE` in `src/App.jsx`).

## Environment Variables (`backend/.env`)

| Variable | Required | Description |
|---|---|---|
| `GROQ_API_KEY` | Yes | Powers the conversational buyer agent. Without it, `/chat` returns a static message telling you to set it. |
| `RAZORPAY_KEY_ID` | No | Defaults to Razorpay's public test key if unset. |
| `RAZORPAY_KEY_SECRET` | No | Without a real secret, order creation and signature verification are skipped and the app proceeds in a direct test-mode path. |
