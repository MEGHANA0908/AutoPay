import { useEffect, useRef, useState, useCallback } from "react";
import "./App.css";

const API_BASE = "http://127.0.0.1:8000";

function formatINR(amount) {
  if (amount === null || amount === undefined) return "—";
  return `₹${amount.toLocaleString("en-IN")}`;
}

function formatTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function formatCountdown(seconds) {
  if (seconds <= 0) return "EXPIRED";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
}

function auditLabel(action) {
  const labels = {
    search_products: "Search",
    set_budget: "Budget set",
    add_to_cart: "Add to cart",
    add_addon_to_cart: "Add-on",
    remove_from_cart: "Remove",
    apply_coupon: "Coupon",
    negotiate_discount: "Negotiation",
    start_checkout: "Checkout & Token",
    confirm_payment: "Payment Authorized",
    token_expired: "Security Alert",
  };
  return labels[action] || action;
}

const TYPE_COLORS = {
  sport: { bg: "#e7e9ff", fg: "#4c5fff" },
  casual: { bg: "#dcfaf1", fg: "#00a882" },
  formal: { bg: "#eceef5", fg: "#131b3e" },
  outdoor: { bg: "#fdf0dd", fg: "#c8790b" },
  addon: { bg: "#f3eefc", fg: "#7c4dcf" },
};

function initials(brand) {
  return (brand || "AP")
    .split(/\s+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

function getSessionId() {
  let id = sessionStorage.getItem("autopay_session_id");
  if (!id) {
    id = crypto.randomUUID();
    sessionStorage.setItem("autopay_session_id", id);
  }
  return id;
}


export default function App() {
  const [sessionId] = useState(getSessionId);
  const [messages, setMessages] = useState([
    {
      role: "agent",
      text:
        "👋 Welcome to AutoPay — your autonomous AI shopping agent on Razorpay! Tell me your budget and preferences (brand, size, color, shoe type) and I'll start finding options for you.",
    },
  ]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [cartState, setCartState] = useState({ budget: null, spent: 0, remaining: null, cart: [], audit: [] });
  const [checkout, setCheckout] = useState(null);
  const [slideIndex, setSlideIndex] = useState(0);
  const [error, setError] = useState(null);
  const [paying, setPaying] = useState(false);
  const [paymentSuccess, setPaymentSuccess] = useState(null);
  const [paymentFailure, setPaymentFailure] = useState(null);

  // Token TTL countdown
  const [tokenTimeLeft, setTokenTimeLeft] = useState(0);

  // Voice Agent State
  const [isListening, setIsListening] = useState(false);
  const [voiceEnabled, setVoiceEnabled] = useState(false);

  const threadEndRef = useRef(null);
  const auditEndRef = useRef(null);
  const inputRef = useRef(null);
  const recognitionRef = useRef(null);

  // Auto-scroll chat to bottom
  useEffect(() => {
    threadEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, sending]);

  // Auto-scroll audit log to bottom when new entries arrive
  useEffect(() => {
    auditEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [cartState.audit]);

  // Focus input
  useEffect(() => {
    if (!sending) {
      inputRef.current?.focus();
    }
  }, [sending]);



  // Token Countdown Timer
  useEffect(() => {
    if (!checkout?.token?.expires_at) {
      setTokenTimeLeft((prev) => (prev === 0 ? prev : 0));
      return;
    }

    const updateTimer = () => {
      const now = Math.floor(Date.now() / 1000);
      const remaining = Math.max(0, checkout.token.expires_at - now);
      setTokenTimeLeft(remaining);
    };

    updateTimer();
    const interval = setInterval(updateTimer, 1000);
    return () => clearInterval(interval);
  }, [checkout?.token]);

  // Text-To-Speech function
  const speakText = useCallback(
    (text) => {
      if (!voiceEnabled || !("speechSynthesis" in window)) return;
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.rate = 1.0;
      utterance.pitch = 1.0;
      window.speechSynthesis.speak(utterance);
    },
    [voiceEnabled]
  );

  // Initialize Speech Recognition
  useEffect(() => {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (SpeechRecognition) {
      const recognition = new SpeechRecognition();
      recognition.continuous = false;
      recognition.interimResults = false;
      recognition.lang = "en-IN";

      recognition.onresult = (event) => {
        const transcript = event.results[0][0].transcript;
        if (transcript) {
          sendMessage(transcript);
        }
      };

      recognition.onerror = (event) => {
        console.warn("Speech recognition error:", event.error);
        setIsListening(false);
      };

      recognition.onend = () => {
        setIsListening(false);
      };

      recognitionRef.current = recognition;
    }
  }, []);

  const toggleListening = () => {
    if (!recognitionRef.current) {
      alert("Speech recognition is not supported in this browser. Please use Chrome or Edge.");
      return;
    }
    if (isListening) {
      recognitionRef.current.stop();
      setIsListening(false);
    } else {
      try {
        recognitionRef.current.start();
        setIsListening(true);
      } catch (err) {
        console.error("Failed to start voice recognition:", err);
      }
    }
  };

  async function sendMessage(text) {
    const trimmed = text.trim();
    if (!trimmed || sending) return;

    setMessages((prev) => [...prev, { role: "user", text: trimmed }]);
    setInput("");
    setSending(true);
    setError(null);

    try {
      const res = await fetch(`${API_BASE}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, message: trimmed }),
      });
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}));
        throw new Error(errBody.detail || `Backend responded ${res.status}`);
      }
      const data = await res.json();
      setMessages((prev) => [...prev, { role: "agent", text: data.reply }]);
      setCartState({
        budget: data.budget,
        spent: data.spent,
        remaining: data.remaining,
        cart: data.cart,
        audit: data.audit || [],
      });
      if (data.checkout) {
        setCheckout(data.checkout);
        setSlideIndex(0);
      }

      // Voice read-out if enabled
      if (data.reply) {
        speakText(data.reply);
      }
    } catch (err) {
      console.error(err);
      setError(err.message || "Something went wrong talking to the agent.");
    } finally {
      setSending(false);
    }
  }

  function handleSubmit(e) {
    e.preventDefault();
    sendMessage(input);
  }

  // Handle Razorpay Payment Flow
  async function handleRazorpayPayment() {
    if (!checkout?.token?.token_id) {
      setError("No permission token found. Re-verify checkout first.");
      return;
    }
    if (tokenTimeLeft <= 0) {
      setError("Permission token has expired! Please request the agent to verify checkout again.");
      return;
    }

    setPaying(true);
    setError(null);

    try {
      // 1. Create order on backend locked to the token
      const orderRes = await fetch(`${API_BASE}/create-razorpay-order`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sessionId,
          token_id: checkout.token.token_id,
        }),
      });

      if (!orderRes.ok) {
        const errData = await orderRes.json().catch(() => ({}));
        throw new Error(errData.detail || "Failed to initialize Razorpay order.");
      }

      const orderData = await orderRes.json();

      // 2. Open Razorpay Checkout Modal
      if (!window.Razorpay) {
        throw new Error("Razorpay Checkout SDK is still loading. Please try again.");
      }

      const options = {
        key: orderData.key_id,
        amount: orderData.amount,
        currency: orderData.currency,
        name: "AutoPay Merchant Store",
        description: `Autonomous Footwear Purchase · Token ${orderData.token_id}`,
        ...(orderData.order_id ? { order_id: orderData.order_id } : {}),
        handler: async function (response) {
          try {
            // 3. Verify payment signature and record in agent audit log
            const verifyRes = await fetch(`${API_BASE}/verify-payment`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                session_id: sessionId,
                razorpay_payment_id: response.razorpay_payment_id,
                razorpay_order_id: response.razorpay_order_id || orderData.order_id || "",
                razorpay_signature: response.razorpay_signature || "",
                token_id: checkout.token.token_id,
              }),
            });


            const verifyData = await verifyRes.json();
            if (!verifyRes.ok) {
              throw new Error(verifyData.detail || "Payment verification failed.");
            }

            if (verifyData.ok === false && verifyData.failure_reason === "TOKEN_EXPIRED") {
              // Razorpay captured the payment, but our permission token had
              // already expired before we could verify it in time.
              setCartState((prev) => ({
                ...prev,
                audit: verifyData.audit || prev.audit,
              }));
              setPaymentFailure(verifyData);
              setCheckout(null);
              return;
            }

            setCartState((prev) => ({
              ...prev,
              audit: verifyData.audit || prev.audit,
            }));
            setPaymentSuccess(verifyData.payment);
            setCheckout(null);
          } catch (vErr) {
            console.error(vErr);
            setError(vErr.message);
          } finally {
            setPaying(false);
          }
        },
        prefill: {
          name: "AI Autonomous Buyer",
          email: "buyer@autopay.ai",
          contact: "9876543210",
        },
        notes: {
          token_id: checkout.token.token_id,
          session_id: sessionId,
        },
        theme: {
          color: "#0c2340",
        },
        modal: {
          ondismiss: function () {
            setPaying(false);
          },
        },
      };

      const rzpInstance = new window.Razorpay(options);

      // Genuine bank/card decline — Razorpay shows its own retry screen,
      // then fires this once the buyer gives up or exhausts retries. Log it
      // to our audit trail so it isn't silently lost.
      rzpInstance.on("payment.failed", async function (response) {
        const err = response.error || {};
        try {
          const failRes = await fetch(`${API_BASE}/payment-failed`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              session_id: sessionId,
              razorpay_payment_id: err.metadata?.payment_id || "",
              razorpay_order_id: err.metadata?.order_id || orderData.order_id || "",
              error_code: err.code || "",
              error_description: err.description || "",
              error_reason: err.reason || "",
            }),
          });
          const failData = await failRes.json().catch(() => ({}));
          setCartState((prev) => ({
            ...prev,
            audit: failData.audit || prev.audit,
          }));
          setPaymentFailure(
            failData.failure_reason
              ? failData
              : {
                  failure_reason: "PAYMENT_DECLINED",
                  detail: err.description || "Your payment was declined by the bank. No charge was made.",
                }
          );
        } catch (logErr) {
          console.error(logErr);
          setError(err.description || "Payment was declined by the bank.");
        } finally {
          setPaying(false);
          setCheckout(null);
        }
      });

      rzpInstance.open();
    } catch (payErr) {
      console.error(payErr);
      setError(payErr.message || "Payment initiation failed.");
      setPaying(false);
    }
  }

  // Handle Session Reset (Fresh Start)
  async function handleResetSession() {
    if (!window.confirm("Start a fresh shopping session? Cart and audit log will be cleared.")) {
      return;
    }
    try {
      const res = await fetch(`${API_BASE}/reset-session`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId }),
      });
      const data = await res.json();
      setCartState({
        budget: data.budget,
        spent: data.spent,
        remaining: data.remaining,
        cart: data.cart,
        audit: [],
      });
      setCheckout(null);
      setPaymentSuccess(null);
      setMessages([
        {
          role: "agent",
          text: "👋 Welcome to AutoPay — your autonomous AI shopping agent on Razorpay! Set your shopping budget and pick your preferences (brand, size, color, shoe type) below to start browsing, or just type what you're looking for.",
        },
      ]);
    } catch (rErr) {
      console.error(rErr);
    }
  }



  const pctUsed =
    cartState.budget && cartState.budget > 0
      ? Math.min(100, (cartState.spent / cartState.budget) * 100)
      : 0;
  const meterState = pctUsed >= 100 ? "over" : pctUsed >= 80 ? "warn" : "safe";

  return (
    <div className="app">
      <nav className="navbar">
        <div className="navbar__brand">
          <span className="navbar__mark">A</span>
          <span>AutoPay</span>
        </div>
        <span className="navbar__tagline">Autonomous AI Buyer · Razorpay Gated Checkout</span>
        <div className="navbar__actions">
          <button
            type="button"
            className={`navbar__btn-voice ${voiceEnabled ? "navbar__btn-voice--active" : ""}`}
            onClick={() => setVoiceEnabled((v) => !v)}
            title={voiceEnabled ? "Voice Output Active (Click to mute)" : "Enable Agent Voice Output"}
          >
            {voiceEnabled ? "🔊 Voice On" : "🔇 Voice Off"}
          </button>
          <button
            type="button"
            className="navbar__btn-reset"
            onClick={handleResetSession}
            title="Start a fresh conversation"
          >
            ↻ New Session
          </button>
        </div>
      </nav>

      <div className="chatlayout">
        <main className="chatthread">

          {messages.map((m, i) => (
            <div key={i} className={`bubble bubble--${m.role}`}>
              <span className="bubble__label">{m.role === "user" ? "You" : "AutoPay Agent"}</span>
              <p>{m.text}</p>
            </div>
          ))}
          {sending && (
            <div className="bubble bubble--agent bubble--typing">
              <span className="bubble__label">AutoPay Agent</span>
              <p>Reasoning &amp; checking catalog…</p>
            </div>
          )}
          {error && <p className="chatthread__error">⚠ {error}</p>}
          <div ref={threadEndRef} />
        </main>


        <aside className="rightcol">
          {/* Compact Budget Ledger directly above Cart */}
          <div className="budgetcard">
            <div className="budgetcard__head">
              <span className="budgetcard__title">
                {cartState.budget ? "Budget Ledger" : "No budget set"}
              </span>
              <span className={`budgetcard__badge budgetcard__badge--${meterState}`}>
                {cartState.budget ? `${formatINR(cartState.remaining)} left` : "Set budget"}
              </span>
            </div>
            <div className="budgetcard__track">
              <div
                className={`budgetcard__fill budgetcard__fill--${meterState}`}
                style={{ width: `${pctUsed}%` }}
              />
            </div>
            <div className="budgetcard__stats">
              <span>Budget: <strong>{formatINR(cartState.budget)}</strong></span>
              <span>Spent: <strong>{formatINR(cartState.spent)}</strong></span>
            </div>
          </div>

          <div className="cart">
            <h2>Cart · {cartState.cart.length}</h2>
            {cartState.cart.length === 0 ? (
              <p className="cart__empty">Nothing added yet.</p>
            ) : (
              <ul className="cart__list">
                {cartState.cart.map((item, idx) => (
                  <li key={idx}>
                    <span>
                      {item.name}
                      {item.size ? ` (UK ${item.size})` : ""}
                    </span>
                    <span className="cart__itemprice">{formatINR(item.price)}</span>
                  </li>
                ))}
              </ul>
            )}
            <div className="cart__total">
              <span>Total</span>
              <strong>{formatINR(cartState.spent)}</strong>
            </div>
          </div>

          <div className="auditpanel">
            <h2>Audit Trail · {cartState.audit.length}</h2>
            {cartState.audit.length === 0 ? (
              <p className="auditpanel__empty">No agent actions yet.</p>
            ) : (
              <div className="auditpanel__scroll">
                {cartState.audit.map((entry, i) => {
                  const blocked =
                    entry.explanation?.startsWith("BLOCKED") || entry.action === "token_expired";
                  const isPayment = entry.action === "confirm_payment";
                  return (
                    <div
                      key={i}
                      className={`auditentry ${blocked ? "auditentry--blocked" : ""} ${
                        isPayment ? "auditentry--payment" : ""
                      }`}
                    >
                      <div className="auditentry__head">
                        <span className="auditentry__action">{auditLabel(entry.action)}</span>
                        <span className="auditentry__time">{formatTime(entry.timestamp)}</span>
                      </div>
                      <p className="auditentry__explanation">{entry.explanation}</p>
                    </div>
                  );
                })}
                <div ref={auditEndRef} />
              </div>
            )}
          </div>
        </aside>
      </div>


      {paymentFailure && (
        <div className="successmodal">
          <div className="successmodal__content">
            <div className="failuremodal__icon">✕</div>
            <h2>
              {paymentFailure.failure_reason === "PAYMENT_DECLINED"
                ? "Payment Declined"
                : "Payment Not Accepted"}
            </h2>
            <p className="successmodal__desc">{paymentFailure.detail}</p>

            <div className="successmodal__receipt" style={{ textAlign: "left" }}>
              <div className="successmodal__section-title">
                {paymentFailure.failure_reason === "PAYMENT_DECLINED" ? "❌ Bank Response" : "🔒 Security Gate"}
              </div>
              <div className="receipt__row">
                <span>Reason</span>
                <span className="badge-failed">
                  {paymentFailure.failure_reason === "PAYMENT_DECLINED"
                    ? (paymentFailure.error_code || "DECLINED")
                    : "TOKEN EXPIRED"}
                </span>
              </div>
              {paymentFailure.payment_id && (
                <div className="receipt__row">
                  <span>Payment ID</span>
                  <code className="receipt__code">{paymentFailure.payment_id}</code>
                </div>
              )}
              {paymentFailure.failure_reason === "PAYMENT_DECLINED" ? (
                <div className="receipt__row">
                  <span>Charge</span>
                  <span>None — payment never captured</span>
                </div>
              ) : (
                <>
                  {paymentFailure.refund?.attempted && (
                    <div className="receipt__row">
                      <span>Refund</span>
                      <span className={paymentFailure.refund.status === "ok" ? "badge-paid" : "badge-failed"}>
                        {paymentFailure.refund.status === "ok"
                          ? `✓ ${paymentFailure.refund.refund_id || "Initiated"}`
                          : "Failed — needs manual check"}
                      </span>
                    </div>
                  )}
                  {!paymentFailure.refund?.attempted && (
                    <div className="receipt__row">
                      <span>Refund</span>
                      <span>Not needed (test mode)</span>
                    </div>
                  )}
                </>
              )}
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <button
                className="btn-primary"
                onClick={() => {
                  setPaymentFailure(null);
                  sendMessage("please start checkout again");
                }}
              >
                🔁 Retry Checkout
              </button>
              <button
                className="btn-primary"
                style={{ background: "transparent", color: "var(--ink-soft)", border: "1px solid var(--border)" }}
                onClick={() => setPaymentFailure(null)}
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {paymentSuccess && (
        <div className="successmodal">
          <div className="successmodal__content">
            {/* Header */}
            <div className="successmodal__icon">✓</div>
            <h2>Order Placed Successfully!</h2>
            <p className="successmodal__desc">
              AutoPay completed your autonomous purchase and Razorpay verified the payment. Your order is confirmed!
            </p>

            {/* Delivery Banner */}
            <div className="successmodal__delivery">
              <span className="successmodal__delivery-icon">🚚</span>
              <div>
                <div className="successmodal__delivery-label">Estimated Delivery</div>
                <div className="successmodal__delivery-date">
                  {paymentSuccess.delivery_estimate || "5–7 Business Days"}
                </div>
              </div>
            </div>

            {/* Items ordered */}
            {paymentSuccess.items && paymentSuccess.items.length > 0 && (
              <div className="successmodal__items">
                <div className="successmodal__section-title">🛍️ Items Ordered</div>
                {paymentSuccess.items.map((item, i) => (
                  <div key={i} className="successmodal__item">
                    <span className="successmodal__item-name">
                      {item.name}{item.size ? ` · UK ${item.size}` : ""}{item.brand ? ` · ${item.brand}` : ""}
                    </span>
                    <span className="successmodal__item-price">{formatINR(item.price)}</span>
                  </div>
                ))}
              </div>
            )}

            {/* Price Breakdown */}
            <div className="successmodal__receipt">
              <div className="successmodal__section-title">🧾 Bill Summary</div>
              {paymentSuccess.coupon_discount > 0 && (
                <div className="receipt__row receipt__row--discount">
                  <span>Coupon Discount</span>
                  <span>− {formatINR(paymentSuccess.coupon_discount)}</span>
                </div>
              )}
              {paymentSuccess.volume_discount > 0 && (
                <div className="receipt__row receipt__row--discount">
                  <span>Volume Discount</span>
                  <span>− {formatINR(paymentSuccess.volume_discount)}</span>
                </div>
              )}
              {paymentSuccess.gst > 0 && (
                <div className="receipt__row">
                  <span>GST</span>
                  <span>+ {formatINR(paymentSuccess.gst)}</span>
                </div>
              )}
              {paymentSuccess.delivery_fee > 0 ? (
                <div className="receipt__row">
                  <span>Delivery</span>
                  <span>+ {formatINR(paymentSuccess.delivery_fee)}</span>
                </div>
              ) : (
                <div className="receipt__row receipt__row--discount">
                  <span>Delivery</span>
                  <span>FREE ✓</span>
                </div>
              )}
              <div className="receipt__row receipt__row--total">
                <span>Total Paid</span>
                <strong>{formatINR(paymentSuccess.amount)}</strong>
              </div>
              <div className="receipt__row">
                <span>Payment ID</span>
                <code className="receipt__code">{paymentSuccess.payment_id}</code>
              </div>
              <div className="receipt__row">
                <span>Status</span>
                <span className="badge-paid">✓ VERIFIED · PAID</span>
              </div>
            </div>

            <button
              className="btn-primary"
              onClick={() => {
                setPaymentSuccess(null);
                handleResetSession();
              }}
            >
              🛍️ Start New Order
            </button>
          </div>
        </div>
      )}


      {checkout && (
        <div className="successmodal">
          <section className="checkoutpanel">
            <div className="checkoutpanel__header">
              <h2>
                {checkout.error ? "⛔ Checkout Blocked" : "✅ Checkout Verified & Gated"}
              </h2>
              <button className="checkoutpanel__close" onClick={() => setCheckout(null)} aria-label="Close">
                ×
              </button>
            </div>

          {checkout.token && (
            <div
              className={`tokenbadge ${
                tokenTimeLeft <= 0 ? "tokenbadge--expired" : tokenTimeLeft <= 20 ? "tokenbadge--warn" : ""
              }`}
            >
              <div className="tokenbadge__info">

                <span className="tokenbadge__tag">🔒 Permission Token:</span>
                <code>{checkout.token.token_id}</code>
              </div>
              <div className="tokenbadge__timer">
                <span>Validity:</span>
                <strong>{formatCountdown(tokenTimeLeft)}</strong>
              </div>
            </div>
          )}

          <div className="slideshow">
            {checkout.items?.length > 0 && (
              <>
                <div className="slideshow__stage">
                  <button
                    type="button"
                    className="slideshow__nav"
                    onClick={() => setSlideIndex((i) => (i - 1 + checkout.items.length) % checkout.items.length)}
                    disabled={checkout.items.length < 2}
                    aria-label="Previous item"
                  >
                    ‹
                  </button>

                  <div className="slideshow__card">
                    <div
                      className="slideshow__image"
                      style={{
                        background: (TYPE_COLORS[checkout.items[slideIndex]?.type] || TYPE_COLORS.sport).bg,
                      }}
                    >
                      <span
                        className="slideshow__initials"
                        style={{
                          color: (TYPE_COLORS[checkout.items[slideIndex]?.type] || TYPE_COLORS.sport).fg,
                        }}
                      >
                        {initials(checkout.items[slideIndex]?.brand)}
                      </span>
                    </div>
                    <div className="slideshow__info">
                      <span className="slideshow__brand">{checkout.items[slideIndex]?.brand}</span>
                      <h3>{checkout.items[slideIndex]?.name}</h3>
                      <p>
                        {checkout.items[slideIndex]?.size ? `Size ${checkout.items[slideIndex].size} · ` : ""}
                        {formatINR(checkout.items[slideIndex]?.price)}
                      </p>
                    </div>
                  </div>

                  <button
                    type="button"
                    className="slideshow__nav"
                    onClick={() => setSlideIndex((i) => (i + 1) % checkout.items.length)}
                    disabled={checkout.items.length < 2}
                    aria-label="Next item"
                  >
                    ›
                  </button>
                </div>
                <div className="slideshow__dots">
                  {checkout.items.map((_, i) => (
                    <span key={i} className={`slideshow__dot ${i === slideIndex ? "slideshow__dot--active" : ""}`} />
                  ))}
                </div>
              </>
            )}
          </div>

          {checkout.error === "INVENTORY_CHECK_FAILED" && (
            <p className="checkoutpanel__error">
              Inventory check failed: {checkout.issues?.join("; ")}
            </p>
          )}
          {checkout.error === "BUDGET_CHECK_FAILED_AT_CHECKOUT" && (
            <p className="checkoutpanel__error">
              Final total {formatINR(checkout.final_total)} exceeds your {formatINR(checkout.budget)} budget by{" "}
              {formatINR(checkout.over_by)}. Remove an item, apply a coupon, or negotiate a discount.
            </p>
          )}

          {!checkout.error && (
            <>
              <div className="breakdown">
                <div className="breakdown__row">
                  <span>Subtotal</span>
                  <span>{formatINR(checkout.subtotal)}</span>
                </div>
                {checkout.total_discount > 0 && (
                  <div className="breakdown__row breakdown__row--discount">
                    <span>Discount ({checkout.percent_discount_pct}%{checkout.flat_discount > 0 ? " + flat" : ""})</span>
                    <span>−{formatINR(checkout.total_discount)}</span>
                  </div>
                )}
                <div className="breakdown__row">
                  <span>CGST</span>
                  <span>{formatINR(checkout.cgst)}</span>
                </div>
                <div className="breakdown__row">
                  <span>SGST</span>
                  <span>{formatINR(checkout.sgst)}</span>
                </div>
                <div className="breakdown__row">
                  <span>Delivery</span>
                  <span>{checkout.delivery_fee === 0 ? "Free" : formatINR(checkout.delivery_fee)}</span>
                </div>
                <div className="breakdown__row breakdown__row--total">
                  <span>Final Total</span>
                  <span>{formatINR(checkout.final_total)}</span>
                </div>
              </div>

              <div className="checkoutpanel__actions">
                <button
                  type="button"
                  className="btn-razorpay"
                  onClick={handleRazorpayPayment}
                  disabled={paying || tokenTimeLeft <= 0}
                >
                  {paying ? (
                    "Processing Razorpay..."
                  ) : tokenTimeLeft <= 0 ? (
                    "⛔ Token Expired"
                  ) : (
                    <>
                      <span className="btn-razorpay__icon">⚡</span>
                      <span>Authorize & Pay {formatINR(checkout.final_total)} with Razorpay</span>
                    </>
                  )}
                </button>
              </div>
            </>
          )}
          </section>
        </div>
      )}

      <form className="composer" onSubmit={handleSubmit}>
        <div className="composer__row">

          <input
            ref={inputRef}
            type="text"
            placeholder={isListening ? "Listening... speak now" : "Tell the agent what you're looking for…"}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            disabled={sending}
            autoComplete="off"
            autoCorrect="off"
            spellCheck="false"
            data-lpignore="true"
            data-1p-ignore="true"
          />
          <button
            type="button"
            className={`composer__mic ${isListening ? "composer__mic--recording" : ""}`}
            onClick={toggleListening}
            title={isListening ? "Stop listening" : "Click to speak"}
            disabled={sending}
          >
            {isListening ? "🛑" : "🎤"}
          </button>
          <button type="submit" disabled={sending || !input.trim()}>
            Send
          </button>
        </div>
      </form>
    </div>
  );
}