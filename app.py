import os
import math
import pandas as pd
import streamlit as st
from sqlalchemy import create_engine

# =========================================================================
# PAGE CONFIGURATION
# =========================================================================
st.set_page_config(
    page_title="SatCoo | Interference Analyzer",
    page_icon="🛰️",
    layout="wide"
)

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

# =========================================================================
# INPUT LOGIC
# =========================================================================
if "Yes" in is_published:
    st.sidebar.subheader("🔍 Search Filing")
    sat_search_name = st.sidebar.text_input("Satellite name as reported in filing", value="USASAT")
    
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
                    **Target WIC (Latest API/A, CR/C, PART II-S):** {target_wic_no}  
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
    if target_f_min is None or target_f_max is None or target_wic_no is None:
        st.error("Invalid parameters or target satellite not selected.")
    else:
        if "Yes" in is_published:
            wic_condition = "c.wic_no > %(wic_no)s AND c.sat_name != %(selected_sat)s"
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
            MAX(c.adm) AS adm,
            MAX(c.wic_no) AS wic_no,                     
            ROUND(AVG(f.freq_mhz)::numeric, 2) AS freq_mhz,
            MIN(f.freq_min) AS freq_min,
            MAX(f.freq_max) AS freq_max,
            MAX(f.bdwdth) AS max_bw_mhz,           
            MAX(b.gain) AS max_gain_dbi,
            MAX(g.eirp_nom) AS max_eirp_dbw,
            MIN(o.min_perig_km) AS min_perig_km,
            MAX(o.max_perig_km) AS max_perig_km
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
        
        with st.spinner("Executing spatial and frequency overlap analysis..."):
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
                    
                    container_paywall = st.container(border=True)
                    with container_paywall:
                        st.markdown("### 👑 Upgrade to PRO to unlock full analysis")
                        st.markdown(f"""
                        Unlock the complete report to access:
                        * 📈 **All {total_count} unique satellite networks** without restrictions.
                        * 🔍 **Detailed beam-by-beam breakdown** with full EIRP and antenna pattern details.
                        * 📥 **Direct Excel export (.xlsx)** with pre-formatted filters.
                        * 🔔 **Automated email alerts** upon every new BR IFIC publication.
                        """)
                        st.button("💳 Purchase Full Report / Upgrade Pro", type="secondary", disabled=True)
            except Exception as eval_err:
                st.error(f"Failed to execute interference query: {eval_err}")
