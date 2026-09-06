# analytics.py
import os
import streamlit as st
from posthog import PostHog
from dotenv import load_dotenv

load_dotenv()

# --- 1. Inizializzazione Client PostHog ---
POSTHOG_API_KEY = os.environ.get("POSTHOG_API_KEY")
POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://eu.posthog.com")

# Istanza unica e corretta del client PostHog
posthog_client = PostHog(
    project_api_key=POSTHOG_API_KEY,
    host=POSTHOG_HOST,
    debug=True  # Sostituisce posthog.debug = True e mostra i log
)


# --- 2. Definizione delle Funzioni di Tracciamento ---
def track_event(event_name, properties=None, user_id="anonymous", user_plan="free"):
    if properties is None:
        properties = {}
    
    # Aggiunge/aggiorna le proprietà del profilo utente su PostHog
    user_properties = {
        "$set": {
            "user_plan": user_plan
        }
    }
    
    # Unisce le proprietà dell'evento con le Person Properties
    payload = {**properties, **user_properties}
    
    try:
        posthog_client.capture(
            distinct_id=user_id,
            event=event_name,
            properties=payload
        )
        posthog_client.flush()
    except Exception as e:
        print(f"Errore PostHog: {e}")


def track_page_view(user_id, page_name, user_plan="free"):
    track_event(
        event_name="$pageview",
        properties={"page_name": page_name},
        user_id=user_id,
        user_plan=user_plan
    )


def track_search_executed(user_id, query_type, search_params, user_plan="free"):
    track_event(
        event_name="search_executed",
        properties={"query_type": query_type, **search_params},
        user_id=user_id,
        user_plan=user_plan
    )


def track_pro_click(user_id, feature_gate, source_location, user_plan="free"):
    track_event(
        event_name="pro_click",
        properties={"feature_gate": feature_gate, "source_location": source_location},
        user_id=user_id,
        user_plan=user_plan
    )


def track_checkout_completed(user_id: str, plan_tier: str, amount: float, currency: str = "EUR"):
    """Da chiamare dopo la conferma di pagamento da Stripe."""
    try:
        posthog_client.capture(
            distinct_id=user_id,
            event="checkout_completed",
            properties={
                "plan_tier": plan_tier,
                "amount": amount,
                "currency": currency,
                "$set": {
                    "user_plan": plan_tier
                }
            }
        )
        posthog_client.flush()
    except Exception as e:
        print(f"Errore PostHog Checkout: {e}")