import os
import math
import pandas as pd
import streamlit as st
from sqlalchemy import create_engine
from stripe_manager import show_pricing_modal
from supabase import create_client, Client
import stripe 

from dotenv import load_dotenv

load_dotenv()

# 1. IMPORTA le funzioni dal file analytics.py
from analytics import (
    track_page_view, 
    track_search_executed, 
    track_pro_click, 
    track_checkout_completed,
    track_event  # Funzione generica per login/logout/altri eventi custom
)

# Inserisci le chiavi API Stripe (se le stai usando)
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY") # Inserisci la tua Secret Key di Stripe

PRICE_PAY_PER_VIEW = os.environ.get("PRICE_PAY_PER_VIEW")
PRICE_PRO_MONTHLY = os.environ.get("PRICE_PRO_MONTHLY")
PRICE_PLUS_MONTHLY = os.environ.get("PRICE_PLUS_MONTHLY")

def create_checkout_session(price_id, mode, user_id, report_id=None, plan_tier="pro"):
    try:
        # Recupera l'URL base dalle variabili d'ambiente (con fallback per lo sviluppo locale)
        base_url = os.environ.get("APP_BASE_URL", "http://localhost:8501")
        
        session = stripe.checkout.Session.create(
            line_items=[{"price": price_id, "quantity": 1}],
            mode=mode,
            success_url=f"{base_url}/?status=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}/?status=cancel",
            client_reference_id=str(user_id),
            metadata={
                "user_id": str(user_id),
                "plan_tier": plan_tier,  # Passato al webhook per aggiornare Supabase/PostHog
                "report_id": str(report_id) if report_id else "all"
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

SUPABASE_URL = "https://botdbymjqewmrixxqtmn.supabase.co"
SUPABASE_KEY = "sb_publishable_9X5DeC5n92Nm1ib4bsLTYA_pwdiohEG"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Inizializzazione variabili di sessione per il routing
if "page" not in st.session_state:
    st.session_state.page = "main"

if "user_authenticated" not in st.session_state:
    st.session_state.user_authenticated = False

if "user_email" not in st.session_state:
    st.session_state.user_email = None

# Recupera o assegna un ID utente per le metriche
user_id = st.session_state.get("user_id", st.session_state.get("user_email", "anonymous_user"))

# =========================================================================
# ANALYTICS: TRACCIAMENTO RITORNO DA STRIPE CHECKOUT
# =========================================================================
query_params = st.query_params
if "session_id" in query_params and "checkout_tracked" not in st.session_state:
    stripe_session_id = query_params["session_id"]
    track_checkout_completed(
        user_id=user_id,
        plan_tier=st.session_state.get("user_plan", "pro"),
        amount=0.0 # Valore popolabile dai metadata di Stripe
    )
    st.session_state["checkout_tracked"] = True
    st.balloons()
    st.success("🎉 Payment successful! Your account features have been unlocked.")

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
        
        if st.button("🚪 Sign Out", type="secondary"):
            track_event("signout_completed", {"email": user_email})
            st.session_state.user_authenticated = False
            st.session_state.user_email = None
            st.session_state.user_id = None
            st.session_state.page = "main"
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

# =======================================================================
# AUTHENTICATION CONFIGURATION & PAGES
# =======================================================================
# 1. Sign In
def render_sign_in_page():
    user_plan = st.session_state.get("user_plan", "free")
    track_page_view(user_id=user_id, page_name="Sign In", user_plan=user_plan)
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
                    st.session_state.user_id = res.user.id
                    st.session_state.page = "main" 
                    
                    # Tracciamento Login Successo
                    track_event("login_completed", {"user_id": res.user.id, "email": res.user.email})
                    
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

# 2. Sign Up
def render_sign_up_page():
    user_plan = st.session_state.get("user_plan", "free")
    track_page_view(user_id=user_id, page_name="Sign Up", user_plan=user_plan)
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
                    track_event("signup_completed", {"email": email})
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

# 3. Recupero Password
def render_reset_password_page():
    user_plan = st.session_state.get("user_plan", "free")
    track_page_view(user_id=user_id, page_name="Reset Password", user_plan=user_plan)
    st.title("🔑 Reset Password")
    st.info("Enter your email address and we'll send you a password reset link.")

    with st.form("reset_form"):
        email = st.text_input("Email Address")
        submit = st.form_submit_button("Send Reset Link")

        if submit:
            if email:
                try:
                    supabase.auth.reset_password_email(
                        email,
                        options={"redirect_to": "http://IP_DEL_TUO_SERVER:8501"}
                    )
                    track_event("password_reset_requested", {"email": email})
                    st.success("If the email is registered, a password reset link has been sent!")
                except Exception as e:
                    st.error(f"Error sending reset email: {e}")
            else:
                st.error("Please enter your email address.")

    if st.button("⬅️ Back to Sign In"):
        st.session_state.page = "signin"
        st.rerun()

# =========================================================================
# MAIN APP ROUTING & HEADER
# =========================================================================
if st.session_state.page == "signin":
    render_sign_in_page()
    st.stop()
elif st.session_state.page == "signup":
    render_sign_up_page()
    st.stop()
elif st.session_state.page == "reset":
    render_reset_password_page()
    st.stop()

# Tracciamento vista della Pagina Principale
user_plan = st.session_state.get("user_plan", "free")

track_page_view(user_id=user_id, page_name="Main Dashboard", user_plan=user_plan)
# =========================================================================
# DATABASE CONNECTION MANAGEMENT
# =========================================================================
DB_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql://itu_admin:supersecretpassword@itu_postgres:5432/itu_spectrum_db"
)

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

st.sidebar.header("⚙️ Target Satellite Configuration")

is_published = st.sidebar.radio(
    "Has your satellite filing already been published?",
    ["Yes (Search Existing Filing)", "No (Manual Entry)"]
)

target_f_min = None
target_f_max = None
target_wic_no = None
selected_sat = None

# ==========================================
# STEP 3: GESTIONE TARGET PLANO PRO PLUS
# ==========================================
user_plan = st.session_state.get("user_plan", "free")

track_page_view(user_id=user_id, page_name="Main Dashboard", user_plan=user_plan)

if user_plan == "pro_plus":
    st.sidebar.markdown("---")
    st.sidebar.markdown("### 🔔 PRO PLUS plan")
    st.sidebar.caption("Monitoring on future BR IFIC")
    
    current_target = st.session_state.get("monitored_sat", "")
    
    new_target = st.sidebar.text_input(
        "Satellite Target (Max 1):", 
        value=current_target,
        placeholder="Es. USA-LUNARSAT-1",
        help="Riceverai notifiche automatiche via email ad ogni nuova BR IFIC se ci sono interferenze su questo filing."
    )
    
    if st.sidebar.button("💾 Save Target", type="primary"):
        if new_target.strip():
            st.session_state["monitored_sat"] = new_target.strip()
            
            # Tracciamento configurazione Target Monitoring
            track_event("target_monitoring_updated", {
                "user_id": user_id, 
                "target_satellite": new_target.strip()
            })
            
            st.sidebar.success(f"Target saved: **{new_target.strip()}**")
        else:
            st.sidebar.warning("Insert a valid satellite name.")

# =========================================================================
# INPUT LOGIC
# =========================================================================
if "Yes" in is_published:
    st.sidebar.subheader("🔍 Search Filing")
    sat_search_name = st.sidebar.text_input("Satellite name as reported in filing", value="IRIDE")
    
    if sat_search_name:
        query_sat = """
        SELECT DISTINCT c.sat_name
        FROM tbl_com_el c
        WHERE c.sat_name ILIKE %(sat_name)s;
        """
        try:
            sat_results = pd.read_sql(query_sat, con=engine, params={'sat_name': f"%{sat_search_name}%"})
            
            if not sat_results.empty:
                selected_sat = st.sidebar.selectbox("Select Found Notice", sat_results['sat_name'].tolist())
                
                query_beams = """
                SELECT DISTINCT g.beam_name
                FROM tbl_com_el c
                JOIN tbl_grp g ON c.ntc_id = g.ntc_id
                WHERE c.sat_name = %(sat_name)s AND g.beam_name IS NOT NULL
                ORDER BY g.beam_name;
                """
                beam_results = pd.read_sql(query_beams, con=engine, params={'sat_name': selected_sat})
                
                if not beam_results.empty:
                    selected_beam = st.sidebar.selectbox("Select Beam", beam_results['beam_name'].tolist())
                    
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
        if target_f_min is None or target_f_max is None or target_wic_no is None:
            st.error("Invalid parameters or target satellite not selected.")
        else:
            # ANALYTICS: Tracciamento dell'evento di ricerca
            track_search_executed(
                user_id=user_id,
                query_type="interference_screening",
                search_params={
                    "is_published": is_published,
                    "target_satellite": selected_sat if "Yes" in is_published else "Manual Entry",
                    "f_min_mhz": target_f_min,
                    "f_max_mhz": target_f_max,
                    "base_wic_no": target_wic_no
                },
                user_plan=user_plan
            )

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

            query_interferers = f"""
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
                
                    st.subheader("📊 Analysis Results")
                
                    if total_count == 0:
                        st.success("🎉 No potential interferers found for the given parameters!")
                    else:
                        preview_count = max(1, math.ceil(total_count * 0.05))
                        df_preview = df_results.head(preview_count)
                    
                        col1, col2, col3 = st.columns(3)
                        col1.metric("Unique Interfering Satellites", total_count)
                        col2.metric("Preview Records (Freemium)", preview_count)
                        col3.metric("Analyzed Frequency Range", f"{target_f_min} - {target_f_max} MHz")
                    
                        st.write(f"### 👁️ Free Preview ({preview_count} of {total_count} satellites)")
                        st.dataframe(df_preview, use_container_width=True)
                    
                        st.divider()
                        st.warning(f"🔒 **{total_count - preview_count} remaining satellites are hidden.**")

                        # Contenitore Paywall con le schede dei prezzi
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
            
                            current_report_id = f"ANALYSIS_{target_f_min}_{target_f_max}_MHz"
            
                            col1, col2, col3 = st.columns(3)

                            with col1:
                                st.markdown("#### Single Analysis")
                                st.markdown("### €49")
                                st.caption("One Time Payment")
                                st.markdown("* Results Report \n* Export PDF/Excel")
                                url = create_checkout_session(PRICE_PAY_PER_VIEW, "payment", user_id, current_report_id)
                                if url:
                                    if st.link_button("👉 Pay €49", url, use_container_width=True, type="primary"):
                                        # ANALYTICS: Tracciamento del click sul piano Pay Per View
                                        track_pro_click(
                                            user_id=user_id, 
                                            feature_gate="single_report", 
                                            source_location="pricing_card_49",
                                            user_plan=user_plan
                                        )

                            with col2:
                                st.markdown("#### PRO Monthly")
                                st.markdown("### €199 /m")
                                st.caption("Subscription")
                                st.markdown("* No Limits Access\n* No Limits Exports")
                                url = create_checkout_session(PRICE_PRO_MONTHLY, "subscription", user_id)
                                if url:
                                    if st.link_button("👉 Subscribe for €199", url, use_container_width=True, type="primary"):
                                        # ANALYTICS: Tracciamento del click sul piano PRO
                                        track_pro_click(
                                            user_id=user_id, 
                                            feature_gate="pro_subscription", 
                                            source_location="pricing_card_199",
                                            user_plan=user_plan
                                        )

                            with col3:
                                st.markdown("#### PRO PLUS")
                                st.markdown("### €249 /m")
                                st.caption("Subscription")
                                st.markdown("* All included in PRO\n* **Alert BR IFIC on Target Satellite**")
                                url = create_checkout_session(PRICE_PLUS_MONTHLY, "subscription", user_id)
                                if url:
                                    if st.link_button("👉 Subscribe for €249", url, use_container_width=True, type="primary"):
                                        # ANALYTICS: Tracciamento del click sul piano PRO PLUS
                                        track_pro_click(
                                            user_id=user_id, 
                                            feature_gate="single_report", 
                                            source_location="pro_plus_subscription",
                                            user_plan=user_plan
                                        )
                except Exception as eval_err:
                    st.error(f"Failed to execute interference query: {eval_err}")