"""
Script di invio alert email per gli utenti PRO PLUS.

Va eseguito UNA VOLTA dopo che il database ITU è stato aggiornato con una
nuova pubblicazione BR IFIC — cioè subito dopo il tuo script di ingestione
automatica che già gira ogni 2 settimane. Non fa parte dei container
Streamlit/webhook: è pensato per girare come comando singolo (via cron),
per non toccare nulla del codice di aggiornamento del database che già
funziona.

Cosa fa, in ordine:
1. Legge il numero di pubblicazione (WIC) più recente nel database ITU.
2. Lo confronta con l'ultimo WIC per cui abbiamo già mandato alert
   (checkpoint salvato su Supabase, tabella alert_state).
3. Se non ci sono pubblicazioni nuove rispetto all'ultima volta, esce
   senza fare nulla (nessuna email duplicata).
4. Se ci sono, per ogni satellite monitorato da un utente PRO PLUS (solo
   PRO PLUS: i PRO possono salvare un satellite ma non ricevono email),
   cerca interferenti SOLO nelle pubblicazioni nuove — non rifà lo
   screening su tutto lo storico — e se ne trova, manda un'email con
   l'elenco tramite Resend.
5. Aggiorna il checkpoint su Supabase.

Uso:
    python send_interference_alerts.py

Variabili d'ambiente richieste (in aggiunta a quelle già usate da app.py):
    DATABASE_URL, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY  (già presenti)
    RESEND_API_KEY       -> API key del tuo account Resend
    ALERT_FROM_EMAIL     -> mittente, es. alerts@tuodominio.it
                            (finché non hai un dominio verificato su Resend,
                            puoi usare il dominio di test che Resend fornisce,
                            valido solo per email a te stesso)
    APP_BASE_URL          -> già presente, usato per il link nell'email
"""

import os
import logging
import requests
import pandas as pd
from sqlalchemy import create_engine
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("satcoo-alerts")

DATABASE_URL = os.environ["DATABASE_URL"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
ALERT_FROM_EMAIL = os.environ.get("ALERT_FROM_EMAIL", "alerts@satcoo.example.com")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8501")

engine = create_engine(DATABASE_URL)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def get_latest_wic() -> int:
    df = pd.read_sql("SELECT MAX(wic_no) as max_wic FROM tbl_com_el;", con=engine)
    if df.empty or pd.isnull(df["max_wic"].iloc[0]):
        return 0
    return int(df["max_wic"].iloc[0])


def get_last_alerted_wic() -> int:
    res = supabase.table("alert_state").select("last_alerted_wic").eq("id", 1).single().execute()
    return res.data["last_alerted_wic"] if res.data else 0


def set_last_alerted_wic(wic: int):
    supabase.table("alert_state").update({"last_alerted_wic": wic}).eq("id", 1).execute()


def get_pro_plus_monitored_satellites():
    """
    Ritorna [{user_id, email, satellite_name}, ...] SOLO per gli utenti con
    piano pro_plus (i soli con diritto agli alert email; i PRO possono
    salvare un satellite ma non compaiono qui).
    """
    profiles_res = supabase.table("profiles").select("user_id, email, plan").eq("plan", "pro_plus").execute()
    pro_plus_emails = {row["user_id"]: row["email"] for row in profiles_res.data}
    if not pro_plus_emails:
        return []

    sats_res = supabase.table("monitored_satellites").select("user_id, satellite_name").execute()
    return [
        {"user_id": row["user_id"], "email": pro_plus_emails[row["user_id"]], "satellite_name": row["satellite_name"]}
        for row in sats_res.data
        if row["user_id"] in pro_plus_emails
    ]


def get_satellite_frequency_range(satellite_name: str):
    """Recupera f_min/f_max del satellite monitorato. None se non trovato."""
    query = """
    SELECT MIN(f.freq_min) AS f_min, MAX(f.freq_max) AS f_max
    FROM tbl_com_el c
    JOIN tbl_grp g ON c.ntc_id = g.ntc_id
    JOIN tbl_freq f ON g.grp_id = f.grp_id
    WHERE c.sat_name = %(sat_name)s;
    """
    df = pd.read_sql(query, con=engine, params={"sat_name": satellite_name})
    if df.empty or pd.isnull(df["f_min"].iloc[0]) or pd.isnull(df["f_max"].iloc[0]):
        return None
    return float(df["f_min"].iloc[0]), float(df["f_max"].iloc[0])


def find_new_interferers(satellite_name: str, f_min: float, f_max: float, since_wic: int, up_to_wic: int):
    """
    Interferenti SOLO tra le pubblicazioni nuove (since_wic, up_to_wic],
    escludendo il satellite monitorato stesso. Stessa logica di
    sovrapposizione in frequenza dello screening manuale in app.py, ma
    ristretta al solo intervallo di WIC appena pubblicato.
    """
    query = """
    SELECT DISTINCT c.sat_name, MAX(c.wic_no) AS wic_no
    FROM tbl_freq f
    JOIN tbl_grp g ON f.grp_id = g.grp_id
    JOIN tbl_com_el c ON g.ntc_id = c.ntc_id
    WHERE f.freq_min <= %(f_max)s
       AND f.freq_max >= %(f_min)s
       AND c.wic_no > %(since_wic)s
       AND c.wic_no <= %(up_to_wic)s
       AND c.sat_name != %(sat_name)s
    GROUP BY c.sat_name
    ORDER BY MAX(c.wic_no) DESC;
    """
    df = pd.read_sql(query, con=engine, params={
        "f_min": f_min, "f_max": f_max,
        "since_wic": since_wic, "up_to_wic": up_to_wic,
        "sat_name": satellite_name,
    })
    return df["sat_name"].tolist()


def send_alert_email(to_email: str, satellite_name: str, interferers: list, wic_no: int):
    interferers_html = "".join(f"<li>{name}</li>" for name in interferers)
    html_body = f"""
    <p>Ciao,</p>
    <p>La pubblicazione BR IFIC <strong>{wic_no}</strong> contiene {len(interferers)}
    nuovo/i potenziale/i interferente/i per il satellite che monitori,
    <strong>{satellite_name}</strong>:</p>
    <ul>{interferers_html}</ul>
    <p><a href="{APP_BASE_URL}">Accedi a SatCoo</a> per l'analisi completa.</p>
    """
    response = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={
            "from": ALERT_FROM_EMAIL,
            "to": [to_email],
            "subject": f"⚠️ Nuovi interferenti per {satellite_name} (BR IFIC {wic_no})",
            "html": html_body,
        },
        timeout=15,
    )
    if response.status_code >= 400:
        logger.error(f"Errore invio email a {to_email}: {response.status_code} {response.text}")
    else:
        logger.info(f"Email inviata a {to_email} per {satellite_name} ({len(interferers)} interferenti)")


def main():
    latest_wic = get_latest_wic()
    last_alerted = get_last_alerted_wic()

    if latest_wic <= last_alerted:
        logger.info(f"Nessuna nuova pubblicazione (ultimo WIC noto: {latest_wic}). Niente da fare.")
        return

    logger.info(f"Nuove pubblicazioni trovate: da WIC {last_alerted + 1} a {latest_wic}.")

    monitored = get_pro_plus_monitored_satellites()
    logger.info(f"{len(monitored)} satelliti monitorati da utenti PRO PLUS.")

    for entry in monitored:
        freq_range = get_satellite_frequency_range(entry["satellite_name"])
        if freq_range is None:
            logger.warning(f"Satellite non trovato nel DB: {entry['satellite_name']}")
            continue
        f_min, f_max = freq_range
        interferers = find_new_interferers(entry["satellite_name"], f_min, f_max, last_alerted, latest_wic)
        if interferers:
            send_alert_email(entry["email"], entry["satellite_name"], interferers, latest_wic)

    set_last_alerted_wic(latest_wic)
    logger.info(f"Checkpoint aggiornato a WIC {latest_wic}.")


if __name__ == "__main__":
    main()
