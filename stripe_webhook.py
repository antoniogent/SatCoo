import os
import stripe
from flask import Flask, request, jsonify
from supabase import create_client, Client
from posthog import Posthog
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Configurazione Stripe
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

# Configurazione Supabase
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# Configurazione PostHog SDK
POSTHOG_API_KEY = os.environ.get("POSTHOG_API_KEY")
posthog = Posthog(
    project_api_key=POSTHOG_API_KEY,
    host=os.environ.get("POSTHOG_HOST", "https://eu.posthog.com")
)


@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        return jsonify({"error": "Invalid signature or payload"}), 400

    event_type = event["type"]

    # 1. Pagamento Completato (Checkout Session)
    if event_type == "checkout.session.completed":
        session = event["data"]["object"]

        # Recupera dati dalla sessione e dai metadata
        metadata = session.get("metadata", {})
        user_id = metadata.get("user_id") or session.get("client_reference_id")
        customer_email = session.get("customer_details", {}).get("email")
        plan_tier = metadata.get("plan_tier", "pro")  # es. 'pro', 'pro_plus', 'single_report'

        # Recupera importo e valuta per le metriche finanziarie
        amount_total = session.get("amount_total", 0) / 100.0  # da centesimi a EUR/USD
        currency = session.get("currency", "eur")

        # --- A. Aggiornamento Supabase ---
        if user_id:
            supabase.table("profiles").update({"role": plan_tier}).eq("id", user_id).execute()
        elif customer_email:
            supabase.table("profiles").update({"role": plan_tier}).eq("email", customer_email).execute()

        # --- B. Tracciamento PostHog ---
        distinct_id = user_id or customer_email or "anonymous_checkout"
        posthog.capture(
            distinct_id=distinct_id,
            event="checkout_completed",
            properties={
                "amount": amount_total,
                "currency": currency,
                "plan_tier": plan_tier,
                "stripe_customer_id": session.get("customer"),
                "stripe_subscription_id": session.get("subscription"),
                "payment_status": session.get("payment_status")
            },
            set={
                "user_plan": plan_tier,
                "is_paying_customer": True
            }
        )
        posthog.flush()

    # 2. Cancellazione Abbonamento
    elif event_type == "customer.subscription.deleted":
        subscription = event["data"]["object"]
        metadata = subscription.get("metadata", {})
        user_id = metadata.get("user_id")

        if user_id:
            # Ripristina il ruolo a 'free' su Supabase
            supabase.table("profiles").update({"role": "free"}).eq("id", user_id).execute()

            # Aggiorna lo stato dell'utente su PostHog
            posthog.capture(
                distinct_id=user_id,
                event="subscription_cancelled",
                properties={
                    "stripe_subscription_id": subscription.get("id")
                },
                set={
                    "user_plan": "free",
                    "is_paying_customer": False
                }
            )
            posthog.flush()

    return jsonify({"status": "success"}), 200


if __name__ == "__main__":
    app.run(port=4242)