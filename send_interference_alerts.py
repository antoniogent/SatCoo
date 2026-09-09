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
import io
import base64
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
    Ritorna [{user_id, email, satellite_name, beam_name}, ...] SOLO per gli
    utenti con piano pro_plus (i soli con diritto agli alert email).
    """
    profiles_res = supabase.table("profiles").select("user_id, email, plan").eq("plan", "pro_plus").execute()
    pro_plus_emails = {row["user_id"]: row["email"] for row in profiles_res.data}
    if not pro_plus_emails:
        return []

    sats_res = supabase.table("monitored_satellites").select("user_id, satellite_name, beam_name").execute()
    return [
        {
            "user_id": row["user_id"],
            "email": pro_plus_emails[row["user_id"]],
            "satellite_name": row["satellite_name"],
            "beam_name": row["beam_name"],
        }
        for row in sats_res.data
        if row["user_id"] in pro_plus_emails
    ]


def get_beam_frequency_range(satellite_name: str, beam_name: str):
    """
    Recupera f_min/f_max SOLO del beam monitorato (non di tutto il filing),
    stessa logica usata dalla ricerca manuale in app.py. None se non trovato.
    """
    query = """
    SELECT MIN(f.freq_min) AS f_min, MAX(f.freq_max) AS f_max
    FROM tbl_com_el c
    JOIN tbl_grp g ON c.ntc_id = g.ntc_id
    JOIN tbl_freq f ON g.grp_id = f.grp_id
    WHERE c.sat_name = %(sat_name)s AND g.beam_name = %(beam_name)s;
    """
    df = pd.read_sql(query, con=engine, params={"sat_name": satellite_name, "beam_name": beam_name})
    if df.empty or pd.isnull(df["f_min"].iloc[0]) or pd.isnull(df["f_max"].iloc[0]):
        return None
    return float(df["f_min"].iloc[0]), float(df["f_max"].iloc[0])


def find_new_interferers(satellite_name: str, f_min: float, f_max: float, since_wic: int, up_to_wic: int) -> pd.DataFrame:
    """
    Interferenti SOLO tra le pubblicazioni nuove (since_wic, up_to_wic],
    escludendo il satellite monitorato stesso. Stesse colonne dello
    screening manuale in app.py, così il CSV allegato all'email è
    direttamente utile (non solo un elenco di nomi).
    """
    query = """
    SELECT
        c.sat_name,
        MAX(c.adm) AS "ADM",
        MAX(c.wic_no) AS "BR IFIC",
        MAX(c.ssn_ref) AS "Pub Type",
        ROUND(AVG(f.freq_mhz)::numeric, 2) AS "Freq MHz",
        MIN(f.freq_min) AS "Freq min",
        MAX(f.freq_max) AS "Freq max",
        MAX(f.bdwdth) AS "Max BW MHz",
        MAX(b.gain) AS "Max Gain dBi",
        MAX(g.eirp_nom) AS "Max EIRP dBW"
    FROM tbl_freq f
    JOIN tbl_grp g ON f.grp_id = g.grp_id
    JOIN tbl_com_el c ON g.ntc_id = c.ntc_id
    LEFT JOIN (
        SELECT ntc_id, beam_name, MAX(gain) AS gain
        FROM tbl_s_beam GROUP BY ntc_id, beam_name
    ) b ON (g.ntc_id = b.ntc_id AND g.beam_name = b.beam_name)
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
    return df


def send_alert_email(to_email: str, satellite_name: str, beam_name: str, interferers_df: pd.DataFrame, wic_no: int):
    count = len(interferers_df)
    target_label = f"{satellite_name} (beam {beam_name})"

    if count == 0:
        body_html = f"""
        <p>Ciao,</p>
        <p>Abbiamo controllato la pubblicazione BR IFIC <strong>{wic_no}</strong>
        per il beam che monitori, <strong>{target_label}</strong>:
        nessun nuovo potenziale interferente rilevato.</p>
        <p><a href="{APP_BASE_URL}">Accedi a SatCoo</a> per un'analisi completa in qualsiasi momento.</p>
        """
        subject = f"✅ Nessun nuovo interferente per {target_label} (BR IFIC {wic_no})"
    else:
        interferers_html = "".join(f"<li>{name}</li>" for name in interferers_df["sat_name"])
        body_html = f"""
        <p>Ciao,</p>
        <p>La pubblicazione BR IFIC <strong>{wic_no}</strong> contiene {count}
        nuovo/i potenziale/i interferente/i per il beam che monitori,
        <strong>{target_label}</strong>:</p>
        <ul>{interferers_html}</ul>
        <p>Il dettaglio completo è nel CSV allegato a questa email.</p>
        <p><a href="{APP_BASE_URL}">Accedi a SatCoo</a> per l'analisi completa.</p>
        """
        subject = f"⚠️ {count} nuovo/i interferente/i per {target_label} (BR IFIC {wic_no})"

    payload = {
        "from": ALERT_FROM_EMAIL,
        "to": [to_email],
        "subject": subject,
        "html": body_html,
    }

    if count > 0:
        csv_bytes = interferers_df.to_csv(index=False).encode("utf-8")
        payload["attachments"] = [{
            "filename": f"interferenti_{satellite_name}_{beam_name}_WIC{wic_no}.csv",
            "content": base64.b64encode(csv_bytes).decode("ascii"),
        }]

    response = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json=payload,
        timeout=15,
    )
    if response.status_code >= 400:
        logger.error(f"Errore invio email a {to_email}: {response.status_code} {response.text}")
    else:
        logger.info(f"Email inviata a {to_email} per {satellite_name} ({count} interferenti)")


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
        freq_range = get_beam_frequency_range(entry["satellite_name"], entry["beam_name"])
        if freq_range is None:
            logger.warning(f"Beam non trovato nel DB: {entry['satellite_name']} / {entry['beam_name']}")
            continue
        f_min, f_max = freq_range
        interferers_df = find_new_interferers(entry["satellite_name"], f_min, f_max, last_alerted, latest_wic)
        send_alert_email(entry["email"], entry["satellite_name"], entry["beam_name"], interferers_df, latest_wic)

    set_last_alerted_wic(latest_wic)
    logger.info(f"Checkpoint aggiornato a WIC {latest_wic}.")


if __name__ == "__main__":
    main()
