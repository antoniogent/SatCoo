import os
import math
import time
import uuid
from urllib.parse import quote
import pandas as pd
import streamlit as st
from sqlalchemy import create_engine
from stripe_manager import show_pricing_modal
from supabase import create_client, Client
import stripe
import posthog
from dotenv import load_dotenv
from streamlit_cookies_controller import CookieController

# Carica le variabili da .env se presente (in produzione su OVH puoi anche
# impostarle come vere variabili d'ambiente di sistema/systemd: in quel
# caso load_dotenv() semplicemente non trova nulla da sovrascrivere).
load_dotenv()


# =========================================================================
# CONFIGURAZIONE DA VARIABILI D'AMBIENTE (mai chiavi hardcoded nel codice!)
# =========================================================================
# Sul server OVH, queste vengono lette dal tuo file .env (via systemd
# EnvironmentFile= o python-dotenv). In locale, puoi creare un .env con
# le stesse chiavi e caricarlo con `from dotenv import load_dotenv; load_dotenv()`
# prima di questo blocco.

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]  # NIENTE default: se manca, l'app deve fermarsi subito

PRICE_PAY_PER_VIEW = os.environ["PRICE_PAY_PER_VIEW"]   # €49 One-time
PRICE_PRO_MONTHLY = os.environ["PRICE_PRO_MONTHLY"]     # €199/mese
PRICE_PLUS_MONTHLY = os.environ["PRICE_PLUS_MONTHLY"]   # €249/mese

# Usato dal servizio webhook separato (non da questa app Streamlit) per
# verificare che le richieste arrivino davvero da Stripe. Lo leggiamo
# comunque qui per fallire subito e chiaramente se manca dal .env.
STRIPE_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]

# Dominio pubblico dell'app: in locale resta localhost, in produzione
# imposta APP_BASE_URL=https://tuodominio.it nel .env
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8501")

def create_checkout_session(price_id, mode, user_id, plan_name, report_id=None, sat_name=None, beam_name=None):
    try:
        base_url = APP_BASE_URL
        success_url = f"{base_url}/?status=success&session_id={{CHECKOUT_SESSION_ID}}"
        if sat_name:
            success_url += f"&sat={quote(sat_name)}"
        if beam_name:
            success_url += f"&beam={quote(beam_name)}"
        session = stripe.checkout.Session.create(
            line_items=[{"price": price_id, "quantity": 1}],
            mode=mode,
            success_url=success_url,
            cancel_url=f"{base_url}/?status=cancel",
            client_reference_id=str(user_id),
            metadata={
                "user_id": str(user_id),
                "report_id": str(report_id) if report_id else "all",
                # "single" = sblocca solo questo report (tabella purchases)
                # "pro" / "pro_plus" = aggiorna l'abbonamento (tabella profiles)
                "plan_name": plan_name,
            }
        )
        return session.url
    except Exception as e:
        st.error(f"Errore Checkout: {e}")
        return None

# 1. SET PAGE CONFIG DEVE ESSERE IN CIMA A TUTTO
st.set_page_config(
    page_title="SatCoo | Interference Analyzer",
    page_icon="🛰️",
    layout="wide"
)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]  # chiave admin: solo lato server, mai esposta al browser

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_user_plan(user_id: str) -> str:
    """
    Legge il piano dell'utente dalla tabella 'profiles' su Supabase.
    Se la tabella non esiste ancora o l'utente non ha un profilo,
    ricade in modo sicuro su 'free' (nessun accesso premium concesso di default).
    NB: quando creiamo la tabella profiles, questa funzione andrà verificata
    contro i nomi reali delle colonne.
    """
    try:
        res = supabase.table("profiles").select("plan").eq("user_id", user_id).single().execute()
        return res.data.get("plan", "free") if res.data else "free"
    except Exception:
        return "free"

def has_purchased_report(user_id, report_id: str) -> bool:
    """
    True se l'utente ha già pagato €49 (Single Analysis) per QUESTO specifico
    report (stessa combinazione target_f_min/target_f_max). Fallback sicuro
    a False se manca la tabella purchases o user_id non è disponibile.
    """
    if not user_id:
        return False
    try:
        res = (
            supabase.table("purchases")
            .select("id")
            .eq("user_id", user_id)
            .eq("report_id", report_id)
            .execute()
        )
        return len(res.data) > 0
    except Exception:
        return False


# Limite di beam monitorabili: solo PRO PLUS (i PRO non hanno gli alert
# email, quindi salvare un beam senza notifiche non avrebbe scopo in questo
# meccanismo). Usato sia qui per la UI, sia implicitamente dallo script di
# invio email che legge la stessa tabella.
PLAN_SATELLITE_LIMITS = {"pro_plus": 3}


def get_monitored_beams(user_id) -> list:
    """Elenco dei beam monitorati dall'utente: [{'satellite_name':..., 'beam_name':...}, ...]."""
    if not user_id:
        return []
    try:
        res = (
            supabase.table("monitored_satellites")
            .select("satellite_name, beam_name")
            .eq("user_id", user_id)
            .order("created_at")
            .execute()
        )
        return res.data
    except Exception:
        return []


def add_monitored_beam(user_id, satellite_name: str, beam_name: str, max_allowed: int, initial_wic: int):
    """
    Aggiunge un beam alla lista monitorata, rispettando il limite del piano.
    initial_wic è il WIC di pubblicazione del satellite al momento
    dell'aggiunta: da lì partirà la ricerca per il PRIMO alert email
    (non da un checkpoint globale condiviso con altri beam).
    """
    current = get_monitored_beams(user_id)
    if any(b["satellite_name"] == satellite_name and b["beam_name"] == beam_name for b in current):
        return False, "Questo beam è già nella tua lista monitorata."
    if len(current) >= max_allowed:
        return False, f"Hai raggiunto il limite di {max_allowed} beam monitorati per il tuo piano."
    try:
        supabase.table("monitored_satellites").insert(
            {
                "user_id": user_id,
                "satellite_name": satellite_name,
                "beam_name": beam_name,
                "last_checked_wic": initial_wic,
            }
        ).execute()
        return True, f"'{satellite_name}' (beam {beam_name}) aggiunto ai monitorati."
    except Exception as e:
        return False, f"Errore durante il salvataggio: {e}"


def remove_monitored_beam(user_id, satellite_name: str, beam_name: str) -> bool:
    try:
        supabase.table("monitored_satellites").delete().eq("user_id", user_id).eq(
            "satellite_name", satellite_name
        ).eq("beam_name", beam_name).execute()
        return True
    except Exception:
        return False


# Gestore dei cookie del browser: usato per far sopravvivere il login a un
# reload completo della pagina (es. dopo il redirect di ritorno da Stripe
# Checkout, che è un caricamento pagina nuovo agli occhi del browser, non
# un semplice "torna alla scheda" — senza questo, Streamlit perderebbe
# session_state e l'utente risulterebbe disconnesso subito dopo aver pagato).
cookie_controller = CookieController()

# --- PostHog: analytics prodotto (visite, ricerche, pagamenti) ---
POSTHOG_API_KEY = os.environ.get("POSTHOG_API_KEY")
POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://eu.posthog.com")
if POSTHOG_API_KEY:
    posthog.api_key = POSTHOG_API_KEY
    posthog.host = POSTHOG_HOST


def get_distinct_id() -> str:
    """
    Un identificativo stabile per collegare gli eventi alla stessa persona
    nel tempo. Se loggato, usa lo user_id vero (così colleghiamo le
    ricerche fatte da anonimo PRIMA del login, una volta che si registra,
    a patto che siano nella stessa sessione browser). Se non loggato, usa
    un id anonimo salvato in cookie, che sopravvive ai reload di pagina.
    """
    user_id = st.session_state.get("user_id")
    if user_id:
        return str(user_id)
    anon_id = cookie_controller.get("ph_anon_id")
    if not anon_id:
        anon_id = str(uuid.uuid4())
        cookie_controller.set("ph_anon_id", anon_id)
    return anon_id


def track_event(event_name: str, properties: dict = None):
    """Non fa nulla se POSTHOG_API_KEY non è configurata (fail-safe)."""
    if not POSTHOG_API_KEY:
        return
    try:
        posthog.capture(distinct_id=get_distinct_id(), event=event_name, properties=properties or {})
    except Exception:
        pass  # il tracciamento non deve mai far fallire l'app


# Inizializzazione variabili di sessione per il routing
if "page" not in st.session_state:
    st.session_state.page = "main"

if "user_authenticated" not in st.session_state:
    st.session_state.user_authenticated = False

if "user_email" not in st.session_state:
    st.session_state.user_email = None

# Ripristino sessione da cookie: se questa è una pagina "nuova" agli occhi
# di Streamlit (reload, o ritorno da Stripe Checkout) ma il browser ha
# ancora un cookie di sessione valido, ripristiniamo il login automaticamente
# invece di mostrare l'utente come disconnesso.
if not st.session_state.user_authenticated:
    _stored_access_token = cookie_controller.get("sb_access_token")
    _stored_refresh_token = cookie_controller.get("sb_refresh_token")
    if _stored_access_token and _stored_refresh_token:
        try:
            _restore_res = supabase.auth.set_session(_stored_access_token, _stored_refresh_token)
            st.session_state.user_authenticated = True
            st.session_state.user_email = _restore_res.user.email
            st.session_state.user_id = _restore_res.user.id
            st.session_state.user_plan = get_user_plan(_restore_res.user.id)
        except Exception:
            # Token scaduto o non più valido: puliamo i cookie, l'utente
            # dovrà rifare il login normalmente.
            cookie_controller.remove("sb_access_token")
            cookie_controller.remove("sb_refresh_token")

# =========================================================================
# CUSTOM CSS
# =========================================================================
st.markdown(
    """
    <style>
        [data-testid="stSidebar"] {
            min-width: 320px;
            max-width: 320px;
        }
        div[data-testid="stMetricValue"] > div {
            font-size: 1.8rem !important;
        }
        div[data-testid="stMetricLabel"] label, 
        div[data-testid="stMetricLabel"] p {
            font-size: 3rem !important;
        }
    </style>
    """,
    unsafe_allow_html=True
)

# Area Utente nella Sidebar
with st.sidebar:
    st.markdown("---")
    if st.session_state.get("user_authenticated", False):
        user_email = st.session_state.get("user_email", "Utente")
        st.success(f"👤 Signed In as: **{user_email}**")

        current_plan = st.session_state.get("user_plan", "free")
        plan_labels = {"pro": "🎖️ Piano: PRO", "pro_plus": "🎖️ Piano: PRO PLUS"}
        if current_plan in plan_labels:
            st.caption(plan_labels[current_plan])
        # Nota: chi ha solo comprato un Single Analysis (pay-per-view) non ha
        # un "piano" persistente — resta 'free' lato profiles, quindi qui
        # non compare nessun badge, come richiesto.

        if st.button("🚪 Sign Out", type="secondary"):
            st.session_state.user_authenticated = False
            st.session_state.user_email = None
            st.session_state.user_id = None
            st.session_state.user_plan = "free"
            st.session_state.page = "main"
            cookie_controller.remove("sb_access_token")
            cookie_controller.remove("sb_refresh_token")
            # Stessa pausa usata al login: il componente ha bisogno di un
            # istante per eseguire davvero la cancellazione nel browser
            # prima che il rerun ricarichi la pagina.
            time.sleep(0.5)
            st.rerun()
    else:
        st.info("🔒 Status: Not Signed In")
        col_login, col_signup = st.columns(2)
        with col_login:
            if st.button("Sign In"):
                st.session_state.page = "signin"
                st.rerun()
        with col_signup:
            if st.button("Sign Up"):
                st.session_state.page = "signup"
                st.rerun()
    st.markdown("---")

#=======================================================================
#AUTHETICATION CONFIGURATION
#======================================================================
# 1. Sign In (Solo Email e Password)
def render_sign_in_page():
    st.title("🔑 Sign In")
    st.info("Enter your credentials to access full analysis features.")

    with st.form("signin_form"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        submit = st.form_submit_button("Sign In")
        if submit:
            if email and password:
                try:
                    res = supabase.auth.sign_in_with_password({"email": email, "password": password})
                    st.session_state.user_authenticated = True
                    st.session_state.user_email = res.user.email
                    st.session_state.user_id = res.user.id  # FIX: prima non veniva mai salvato -> tutti i pagamenti finivano su user_id di default (1)
                    st.session_state.user_plan = get_user_plan(res.user.id)
                    # Collega l'id anonimo (usato per page_view prima del
                    # login) al vero user_id: senza questo, PostHog vede due
                    # "persone" diverse e i funnel restano vuoti.
                    if POSTHOG_API_KEY:
                        _anon_id = cookie_controller.get("ph_anon_id")
                        st.session_state["_debug_anon_id_at_login"] = _anon_id  # solo per debug temporaneo
                        if _anon_id:
                            try:
                                posthog.alias(previous_id=_anon_id, distinct_id=res.user.id)
                                print(f"[POSTHOG ALIAS OK] anon={_anon_id} -> user={res.user.id}", flush=True)
                            except Exception as e:
                                print(f"[POSTHOG ALIAS ERROR] anon={_anon_id} user={res.user.id} errore={e}", flush=True)
                        else:
                            print("[POSTHOG ALIAS SKIPPED] nessun ph_anon_id trovato nel cookie al momento del login", flush=True)
                    track_event("user_signed_in")
                    # Salva i token in un cookie così il login sopravvive a un
                    # reload completo della pagina (es. ritorno da Stripe).
                    cookie_controller.set("sb_access_token", res.session.access_token)
                    cookie_controller.set("sb_refresh_token", res.session.refresh_token)
                    # Piccola pausa: il componente ha bisogno di un istante per
                    # eseguire davvero il comando JS che scrive il cookie nel
                    # browser, prima che il rerun ricarichi la pagina.
                    time.sleep(0.5)
                    st.session_state.page = "main" 
                    st.success("Successfully logged in!") 
                    st.rerun()
                except Exception as e:
                    st.error("Invalid credentials or email not verified.")
            else:
                st.error("Please provide both email and password.")

    st.write("Don't have an account?")
    if st.button("Create a new account (Sign Up)"):
        st.session_state.page = "signup"
        st.rerun()

    if st.button("Forgot Password?"):
        st.session_state.page = "reset"
        st.rerun()  

    if st.button("⬅️ Back to Home"):
        st.session_state.page = "main"
        st.rerun()

# 2. Sign Up (Form Completo)
def render_sign_up_page():
    st.title("📝 Sign Up")
    st.info("Complete the registration form to create an account.")

    with st.form("signup_form"):
        nome = st.text_input("Full Name")
        email = st.text_input("Email Address")
        password = st.text_input("Password", type="password")
        confirm_password = st.text_input("Confirm Password", type="password")
        submit = st.form_submit_button("Sign Up")

        if submit:
            if email and password and password == confirm_password:
                try:
                    res = supabase.auth.sign_up({"email": email, "password": password})
                    if POSTHOG_API_KEY and res.user:
                        _anon_id = cookie_controller.get("ph_anon_id")
                        if _anon_id:
                            try:
                                posthog.alias(previous_id=_anon_id, distinct_id=res.user.id)
                            except Exception:
                                pass
                    track_event("user_signed_up")
                    st.success("Account created! Check your email to confirm your account.")
                except Exception as e:
                    st.error(f"Registration failed: {e}")
            elif password != confirm_password:
                st.error("Passwords do not match.")
            else:
                st.error("Please fill in all required fields.")

    st.write("Already have an account?")
    if st.button("Go to Sign In"):
        st.session_state.page = "signin"
        st.rerun()

    if st.button("⬅️ Back to Home"):
        st.session_state.page = "main"
        st.rerun()

#3. Recupero Password
def render_reset_password_page():
    st.title("🔑 Reset Password")
    st.info("Enter your email address and we'll send you a password reset link.")

    with st.form("reset_form"):
        email = st.text_input("Email Address")
        submit = st.form_submit_button("Send Reset Link")

        if submit:
            if email:
                try:
                    # Invia la mail di recupero da Supabase
                    supabase.auth.reset_password_email(
                        email,
                        options={"redirect_to": APP_BASE_URL}
                    )
                    st.success("If the email is registered, a password reset link has been sent!")
                except Exception as e:
                    st.error(f"Error sending reset email: {e}")
            else:
                st.error("Please enter your email address.")

    if st.button("⬅️ Back to Sign In"):
        st.session_state.page = "signin"
        st.rerun()


# =========================================================================
# DATABASE CONNECTION MANAGEMENT
# =========================================================================
# Nessun default con password hardcoded: se DATABASE_URL manca dal .env,
# meglio che l'app si fermi con un errore chiaro piuttosto che rischiare
# di usare credenziali in chiaro finite nel codice sorgente.
DB_URL = os.environ["DATABASE_URL"]

@st.cache_resource
def get_db_engine():
    return create_engine(DB_URL)

try:
    engine = get_db_engine()
    with engine.connect() as connection:
        pass
except Exception as e:
    st.error("🚨 **Database Connection Error**")
    st.warning("Could not reach the PostgreSQL container (`itu_postgres`). Please ensure the database service is running.")
    st.code(str(e))
    st.stop()

# =========================================================================
# HEADER & SIDEBAR
# =========================================================================
st.title("🛰️ SatCoo | Interference Analyzer")
st.caption("Spectrum Interference Screening for Satellite Coordination (based on ITU BR IFIC filings)")

if not st.session_state.get("_page_view_tracked", False):
    track_event("page_view")
    st.session_state["_page_view_tracked"] = True

st.sidebar.header("⚙️ Target Satellite Configuration")

# Ripristino della ricerca dopo il ritorno da Stripe Checkout: il redirect
# è un caricamento pagina completo agli occhi del browser, quindi senza
# questo l'utente dovrebbe rifare da capo ricerca satellite + selezione
# beam subito dopo aver pagato. I valori arrivano come query string
# nell'URL di ritorno (impostati in create_checkout_session).
_qp = st.query_params
if _qp.get("status") == "success" and not st.session_state.get("_restored_search_after_payment", False):
    if _qp.get("sat"):
        st.session_state["sat_search_name_widget"] = _qp.get("sat")
        st.session_state["selected_sat_widget"] = _qp.get("sat")
    if _qp.get("beam"):
        st.session_state["selected_beam_widget"] = _qp.get("beam")
    # Evita di riapplicare questi valori ad ogni rerun successivo, così
    # l'utente può comunque cercare liberamente qualcos'altro dopo.
    st.session_state["_restored_search_after_payment"] = True

is_published = st.sidebar.radio(
    "Has your satellite filing already been published?",
    ["Yes (Search Existing Filing)", "No (Manual Entry)"]
)

target_f_min = None
target_f_max = None
target_wic_no = None
selected_sat = None
selected_beam = None

# ==========================================
# STEP 3: SATELLITI/BEAM MONITORATI (solo PRO PLUS)
# ==========================================
# Recupera lo stato utente dalla sessione (default 'free')
user_plan = st.session_state.get("user_plan", "free")
user_id_for_monitoring = st.session_state.get("user_id")
max_monitored_beams = PLAN_SATELLITE_LIMITS.get(user_plan, 0)
monitored_beams = get_monitored_beams(user_id_for_monitoring) if max_monitored_beams else []

if max_monitored_beams:
    st.sidebar.markdown("---")
    st.sidebar.markdown(f"### 🔔 Beam monitorati ({len(monitored_beams)}/{max_monitored_beams})")
    st.sidebar.caption("Riceverai un'email automatica ad ogni nuova pubblicazione BR IFIC se emergono interferenze su questi beam. Per aggiungerne uno, cercalo qui sotto come per uno screening normale.")

    for b in monitored_beams:
        col_name, col_remove = st.sidebar.columns([4, 1])
        col_name.markdown(f"🛰️ {b['satellite_name']} — {b['beam_name']}")
        if col_remove.button("🗑️", key=f"remove_{b['satellite_name']}_{b['beam_name']}", help="Rimuovi"):
            remove_monitored_beam(user_id_for_monitoring, b["satellite_name"], b["beam_name"])
            st.rerun()


# =========================================================================
# INPUT LOGIC
# =========================================================================
if "Yes" in is_published:
    st.sidebar.subheader("🔍 Search Filing")
    sat_search_name = st.sidebar.text_input("Satellite name as reported in filing", value="IRIDE", key="sat_search_name_widget")
    
    if sat_search_name:
        query_sat = """
        SELECT DISTINCT c.sat_name
        FROM tbl_com_el c
        WHERE c.sat_name ILIKE %(sat_name)s;
        """
        try:
            sat_results = pd.read_sql(query_sat, con=engine, params={'sat_name': f"%{sat_search_name}%"})
            
            if not sat_results.empty:
                selected_sat = st.sidebar.selectbox("Select Found Notice", sat_results['sat_name'].tolist(), key="selected_sat_widget")
                
                query_beams = """
                SELECT DISTINCT g.beam_name
                FROM tbl_com_el c
                JOIN tbl_grp g ON c.ntc_id = g.ntc_id
                WHERE c.sat_name = %(sat_name)s AND g.beam_name IS NOT NULL
                ORDER BY g.beam_name;
                """
                beam_results = pd.read_sql(query_beams, con=engine, params={'sat_name': selected_sat})
                
                if not beam_results.empty:
                    selected_beam = st.sidebar.selectbox("Select Beam", beam_results['beam_name'].tolist(), key="selected_beam_widget")
                    
                    query_beam_details = """
                    SELECT 
                        MAX(c.wic_no) FILTER (
                            WHERE UPPER(COALESCE(c.ntc_type, '')) LIKE '%%API/A%%'
                               OR UPPER(COALESCE(c.ntc_type, '')) LIKE '%%CR/C%%'
                               OR UPPER(COALESCE(c.ntc_type, '')) LIKE '%%PART II%%'
                               OR UPPER(COALESCE(c.ntc_type, '')) LIKE '%%PART 2%%'
                               OR UPPER(COALESCE(c.ntc_type, '')) LIKE '%%A%%'
                        ) AS filtered_wic,
                        MAX(c.wic_no) AS fallback_wic,
                        MIN(f.freq_min) AS f_min,
                        MAX(f.freq_max) AS f_max,
                        AVG(f.freq_mhz) AS f_mhz,
                        MAX(f.bdwdth) AS bdwdth,
                        MAX(b.gain) AS gain_dbi,
                        MAX(g.eirp_nom) AS eirp_nom
                    FROM tbl_com_el c
                    JOIN tbl_grp g ON c.ntc_id = g.ntc_id
                    JOIN tbl_freq f ON g.grp_id = f.grp_id
                    LEFT JOIN tbl_s_beam b ON (g.ntc_id = b.ntc_id AND g.beam_name = b.beam_name)
                    WHERE c.sat_name = %(sat_name)s AND g.beam_name = %(beam_name)s;
                    """
                    beam_info = pd.read_sql(
                        query_beam_details, 
                        con=engine, 
                        params={'sat_name': selected_sat, 'beam_name': selected_beam}
                    ).iloc[0]
                    
                    target_wic_raw = beam_info['filtered_wic'] if pd.notnull(beam_info['filtered_wic']) else beam_info['fallback_wic']
                    target_wic_no = int(target_wic_raw) if pd.notnull(target_wic_raw) else 0
                    
                    target_f_min = float(beam_info['f_min']) if pd.notnull(beam_info['f_min']) else 0.0
                    target_f_max = float(beam_info['f_max']) if pd.notnull(beam_info['f_max']) else 0.0
                    
                    freq_mhz_val = round(float(beam_info['f_mhz']), 2) if pd.notnull(beam_info['f_mhz']) else "Not Available"
                    gain_val = beam_info['gain_dbi'] if pd.notnull(beam_info['gain_dbi']) else "Not Available"
                    bw_val = beam_info['bdwdth'] if pd.notnull(beam_info['bdwdth']) else "Not Available"
                    
                    st.sidebar.success(f"""
                    **Satellite Published in BR IFIC:** {target_wic_no}  
                    **Beam:** {selected_beam}  
                    **Antenna Gain:** {gain_val} dBi  
                    **Freq Min:** {target_f_min} MHz  
                    **Freq Max:** {target_f_max} MHz  
                    **Carrier Freq:** {freq_mhz_val} MHz  
                    **Bandwidth:** {bw_val} MHz
                    """)
                    st.sidebar.info(f"👉 Screening starts from **WIC {target_wic_no + 1}** onwards (excluding WIC {target_wic_no}).")

                    if max_monitored_beams:
                        already_monitored = any(
                            b["satellite_name"] == selected_sat and b["beam_name"] == selected_beam
                            for b in monitored_beams
                        )
                        if already_monitored:
                            st.sidebar.caption("🔔 Questo beam è già tra i monitorati.")
                        elif len(monitored_beams) >= max_monitored_beams:
                            st.sidebar.caption(f"Limite di {max_monitored_beams} beam monitorati raggiunto.")
                        else:
                            if st.sidebar.button("🔔 Aggiungi ai monitorati", key="add_to_monitored_btn"):
                                ok, msg = add_monitored_beam(
                                    user_id_for_monitoring, selected_sat, selected_beam, max_monitored_beams, target_wic_no
                                )
                                if ok:
                                    st.sidebar.success(msg)
                                    st.rerun()
                                else:
                                    st.sidebar.warning(msg)
                else:
                    st.sidebar.warning("No beams found for this satellite.")
            else:
                st.sidebar.warning("No satellite found with this name in the DB.")
        except Exception as query_err:
            st.sidebar.error(f"Error querying satellite database: {query_err}")

else:
    st.sidebar.subheader("📝 Manual Parameters")
    target_f_min = st.sidebar.number_input("Minimum Frequency (MHz)", value=2259.0, step=0.1)
    target_f_max = st.sidebar.number_input("Maximum Frequency (MHz)", value=2261.0, step=0.1)
    
    try:
        max_wic_df = pd.read_sql("SELECT MAX(wic_no) as max_wic FROM tbl_com_el;", con=engine)
        latest_wic = int(max_wic_df['max_wic'].dropna().iloc[0]) if not max_wic_df.empty else 3078
    except Exception:
        latest_wic = 3078
    
    target_wic_no = latest_wic
    st.sidebar.info(f"👉 Screening evaluates against the latest available publication: **WIC {target_wic_no}**")

# =========================================================================
# INTERFERENCE SEARCH EXECUTION
# =========================================================================
if st.button("🚀 Run Interference Screening", type="primary"):
# Controllo Registrazione Utente
    if not st.session_state.get("user_authenticated", False):
        st.session_state.page = "signin"
        st.rerun()
    else:
        # Se l'utente è autenticato, esegui l'analisi
        if target_f_min is None or target_f_max is None or target_wic_no is None:
            st.error("Invalid parameters or target satellite not selected.")
        else:
            if "Yes" in is_published:
                wic_condition = "c.wic_no > %(wic_no)s AND c.sat_name != %(selected_sat)s AND c.ssn_ref = 'API/A'"
                params_dict = {
                    'f_min': target_f_min,
                    'f_max': target_f_max,
                    'wic_no': target_wic_no,
                    'selected_sat': selected_sat
                }
            else:
                wic_condition = "c.wic_no >= %(wic_no)s"
                params_dict = {
                    'f_min': target_f_min,
                    'f_max': target_f_max,
                    'wic_no': target_wic_no
                }
            # Query raggruppata per singolo satellite senza total_overlapping_beams
            query_interferers = f"""
            SELECT
                c.sat_name,
                MAX(c.adm) AS "ADM",
                MAX(c.wic_no) AS "BR IFIC",
                MAX(c.ssn_ref) AS "Pub Type",                      
                MAX(g.polar_type) AS "Pol Type",
                ROUND(AVG(f.freq_mhz)::numeric, 2) AS "Freq MHz",
                MIN(f.freq_min) AS "Freq min",
                MAX(f.freq_max) AS "Freq max",
                MAX(f.bdwdth) AS "Max BW MHz",           
                MAX(b.gain) AS "Max Gain dBi",
                MAX(g.eirp_nom) AS "Max EIRP dBW",
                MIN(o.min_perig_km) AS "min Perigee km",
                MAX(o.max_perig_km) AS "Max Perigee km"
            FROM tbl_freq f
            JOIN tbl_grp g ON f.grp_id = g.grp_id
            JOIN tbl_com_el c ON g.ntc_id = c.ntc_id
            LEFT JOIN (
                SELECT ntc_id, MIN(perig_km) AS min_perig_km, MAX(perig_km) AS max_perig_km
                FROM tbl_orbit GROUP BY ntc_id
            ) o ON c.ntc_id = o.ntc_id
            LEFT JOIN (
                SELECT ntc_id, beam_name, MAX(gain) AS gain
                FROM tbl_s_beam GROUP BY ntc_id, beam_name
            ) b ON (g.ntc_id = b.ntc_id AND g.beam_name = b.beam_name)
            WHERE f.freq_min <= %(f_max)s 
               AND f.freq_max >= %(f_min)s
               AND {wic_condition}
            GROUP BY c.sat_name
            ORDER BY MAX(c.wic_no) DESC;
            """
        
            with st.spinner("Executing frequency overlap analysis..."):
                try:
                    df_results = pd.read_sql(query_interferers, con=engine, params=params_dict)
                    df_results = df_results.fillna("Not Available")
                
                    total_count = len(df_results)
                    track_event("search_performed", {
                        "satellite": selected_sat,
                        "beam": selected_beam,
                        "results_count": total_count,
                    })
                
                    st.subheader("📊 Analysis Results")
                
                    if total_count == 0:
                        st.success("🎉 No potential interferers found for the given parameters!")
                    else:
                        # FIX: prima chi pagava non vedeva mai la tabella completa,
                        # perché qui non si controllava mai il piano/acquisti dell'utente.
                        user_plan = st.session_state.get("user_plan", "free")
                        report_id_current = f"ANALYSIS_{target_f_min}_{target_f_max}_MHz"
                        is_unlocked = (
                            user_plan in ("pro", "pro_plus")
                            or has_purchased_report(st.session_state.get("user_id"), report_id_current)
                        )

                        preview_count = total_count if is_unlocked else max(1, math.ceil(total_count * 0.05))
                        df_preview = df_results.head(preview_count)
                    
                        col1, col2, col3 = st.columns(3)
                        col1.metric("Unique Interfering Satellites", total_count)
                        col2.metric("Unlocked Records" if is_unlocked else "Preview Records (Freemium)", preview_count)
                        col3.metric("Analyzed Frequency Range", f"{target_f_min} - {target_f_max} MHz")

                        if is_unlocked:
                            st.write(f"### ✅ Full Report ({total_count} satellites)")
                        else:
                            st.write(f"### 👁️ Free Preview ({preview_count} of {total_count} satellites)")
                        st.dataframe(df_preview, use_container_width=True)

                        if is_unlocked:
                            st.download_button(
                                "⬇️ Export CSV",
                                df_results.to_csv(index=False).encode("utf-8"),
                                file_name=f"interference_report_{target_f_min}_{target_f_max}MHz.csv",
                                mime="text/csv",
                            )

                    if total_count > 0 and not is_unlocked:
                        st.divider()
                        st.warning(f"🔒 **{total_count - preview_count} remaining satellites are hidden.**")

                        # Contenitore Paywall con le schede dei prezzi SEMPRE VISIBILI
                        container_paywall = st.container(border=True)
                        with container_paywall:
                            st.markdown("### 👑 Upgrade to PRO to unlock full analysis")
                            st.markdown(f"""
                            Unlock the complete report to access:
                            * 📈 **All {total_count} unique satellite networks** without restrictions.
                            * 🔍 **Detailed beam-by-beam breakdown** with full EIRP and antenna pattern details.
                            * 📊 **Direct Excel export (.xlsx)** with pre-formatted filters.
                            * 🔔 **Automated email alerts** upon every new BR IFIC publication.
                            """)
            
                            st.divider()
                            st.subheader("Select your plan to access full data:")
            
                            user_id = st.session_state.get("user_id")
                            current_report_id = f"ANALYSIS_{target_f_min}_{target_f_max}_MHz"

                            if not user_id:
                                st.error("⚠️ Impossibile identificare l'utente per il checkout. Prova a rifare il login.")
                                st.stop()
            
                            col1, col2, col3 = st.columns(3)

                            with col1:
                                st.markdown("#### Single Analysis")
                                st.markdown("### €49")
                                st.caption("One Time Payment")
                                st.markdown("* Results Report \n* Export PDF/Excel")
                                track_event("checkout_started", {"plan": "single"})
                                url = create_checkout_session(PRICE_PAY_PER_VIEW, "payment", user_id, "single", current_report_id, selected_sat, selected_beam)
                                if url:
                                    st.link_button("👉 Pay €49", url, use_container_width=True, type="primary")

                            with col2:
                                st.markdown("#### PRO Monthly")
                                st.markdown("### €199 /m")
                                st.caption("Subscription")
                                st.markdown("* No Limits Access\n* No Limits Exports")
                                track_event("checkout_started", {"plan": "pro"})
                                url = create_checkout_session(PRICE_PRO_MONTHLY, "subscription", user_id, "pro", sat_name=selected_sat, beam_name=selected_beam)
                                if url:
                                    st.link_button("👉 Subscribe for €199", url, use_container_width=True, type="primary")

                            with col3:
                                st.markdown("#### PRO PLUS")
                                st.markdown("### €249 /m")
                                st.caption("Subscription")
                                st.markdown("* All included in PRO\n* **Alert BR IFIC on Target Satellite**")
                                track_event("checkout_started", {"plan": "pro_plus"})
                                url = create_checkout_session(PRICE_PLUS_MONTHLY, "subscription", user_id, "pro_plus", sat_name=selected_sat, beam_name=selected_beam)
                                if url:
                                    st.link_button("👉 Subscribe for €249", url, use_container_width=True, type="primary")

                except Exception as eval_err:
                    st.error(f"Failed to execute interference query: {eval_err}")

# Router per il cambio pagina in fondo ad app.py
# Router con bypass attivo
if st.session_state.page == "signin":
    render_sign_in_page()
elif st.session_state.page == "signup":
    render_sign_up_page()
elif st.session_state.page == "reset":
    render_reset_password_page()
