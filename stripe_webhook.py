import os
import logging
import stripe
from flask import Flask, request, jsonify
from supabase import create_client, Client
from dotenv import load_dotenv

# --- PostHog: disattivato per ora, riattivabile in futuro senza riscrivere
# la logica sotto. Basta scommentare le 3 righe di import/init qui sotto e
# le chiamate posthog.capture(...) più in basso, e aggiungere POSTHOG_API_KEY
# al .env.
# from posthog import Posthog
# posthog = Posthog(project_api_key=os.environ.get("POSTHOG_API_KEY"),
#                    host=os.environ.get("POSTHOG_HOST", "https://eu.posthog.com"))

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("satcoo-webhook")

app = Flask(__name__)

# --- Stripe ---
STRIPE_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

# --- Supabase (chiave admin: bypassa RLS, necessaria per scrivere per
# conto di qualunque utente da un servizio server-side) ---
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logger.warning(f"Webhook firma non valida: {e}")
        return jsonify({"error": "Invalid signature or payload"}), 400

    event_type = event["type"]
    data_object = event["data"]["object"]

    if event_type == "checkout.session.completed":
        handle_checkout_completed(data_object)
    elif event_type == "customer.subscription.deleted":
        handle_subscription_deleted(data_object)
    elif event_type == "customer.subscription.updated":
        handle_subscription_updated(data_object)
    else:
        logger.info(f"Evento ignorato (non gestito): {event_type}")

    return jsonify({"status": "success"}), 200


def handle_checkout_completed(session: dict):
    metadata = session.get("metadata", {}) or {}
    user_id = metadata.get("user_id") or session.get("client_reference_id")
    # NB: "plan_name" è la chiave usata da create_checkout_session() in app.py.
    # Valori possibili: "single" (Pay-per-view €49), "pro", "pro_plus".
    plan_name = metadata.get("plan_name")
    report_id = metadata.get("report_id")
    checkout_session_id = session.get("id")
    customer_id = session.get("customer")
    subscription_id = session.get("subscription")

    if not user_id or not plan_name:
        logger.error(f"checkout.session.completed senza user_id/plan_name nei metadata: {checkout_session_id}")
        return

    if plan_name == "single":
        # Pagamento one-time: sblocca SOLO questo report, non tocca profiles.plan
        try:
            supabase.table("purchases").upsert({
                "user_id": user_id,
                "report_id": report_id,
                "stripe_checkout_session_id": checkout_session_id,
            }, on_conflict="user_id,report_id").execute()
            logger.info(f"Purchase registrato: user={user_id} report={report_id}")
        except Exception as e:
            logger.error(f"Errore salvataggio purchase: {e}")

    elif plan_name in ("pro", "pro_plus"):
        # Abbonamento: aggiorna il piano persistente dell'utente
        try:
            supabase.table("profiles").update({
                "plan": plan_name,
                "stripe_customer_id": customer_id,
                "stripe_subscription_id": subscription_id,
            }).eq("user_id", user_id).execute()
            logger.info(f"Piano aggiornato: user={user_id} -> {plan_name}")
        except Exception as e:
            logger.error(f"Errore aggiornamento piano: {e}")
    else:
        logger.warning(f"plan_name sconosciuto nei metadata: {plan_name}")

    # posthog.capture(distinct_id=user_id, event="checkout_completed", properties={
    #     "amount": session.get("amount_total", 0) / 100.0,
    #     "currency": session.get("currency", "eur"),
    #     "plan_name": plan_name,
    # })
    # posthog.flush()


def handle_subscription_deleted(subscription: dict):
    """L'abbonamento è stato cancellato: l'utente torna a 'free'."""
    subscription_id = subscription.get("id")
    try:
        supabase.table("profiles").update({"plan": "free"}).eq(
            "stripe_subscription_id", subscription_id
        ).execute()
        logger.info(f"Abbonamento {subscription_id} terminato -> piano riportato a free")
    except Exception as e:
        logger.error(f"Errore downgrade a free: {e}")

    # posthog.capture(distinct_id=..., event="subscription_cancelled", ...)


def handle_subscription_updated(subscription: dict):
    """
    Copre gli stati non paganti (es. 'past_due', 'unpaid', 'canceled')
    senza aspettare l'evento 'deleted' formale. Stripe ritenta comunque
    il pagamento automaticamente per alcuni giorni prima di arrivare lì.
    """
    status = subscription.get("status")
    subscription_id = subscription.get("id")

    if status in ("past_due", "unpaid", "canceled", "incomplete_expired"):
        try:
            supabase.table("profiles").update({"plan": "free"}).eq(
                "stripe_subscription_id", subscription_id
            ).execute()
            logger.info(f"Abbonamento {subscription_id} in stato {status} -> piano riportato a free")
        except Exception as e:
            logger.error(f"Errore downgrade su subscription.updated: {e}")


if __name__ == "__main__":
    # Solo per test rapidi in locale. In produzione (Docker) si usa gunicorn,
    # vedi docker-compose.yml.
    app.run(host="0.0.0.0", port=8000)
