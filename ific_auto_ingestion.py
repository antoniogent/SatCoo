import os
import re
import sys
import zipfile
import requests
import subprocess
import shutil
import pandas as pd
from pathlib import Path
from sqlalchemy import create_engine, text

# Configurazione Ambiente e Database
DB_URL = os.getenv("DATABASE_URL", "postgresql://itu_admin:supersecretpassword@itu_postgres:5432/itu_spectrum_db")
BASE_DIR = Path("/app/itu_data")
DOWNLOAD_DIR = BASE_DIR / "downloads"
CSV_DIR = BASE_DIR / "csv_temp"

START_WIC = 2886
SWITCH_URL_WIC = 3037

TARGET_SSN_PATTERNS = ['API/A', 'CR/C', 'PART II-S']

TABLE_COLUMNS = {
    'notice': ['ntc_id', 'adm', 'sat_name', 'ntc_type', 'wic_no', 'ssn_ref'],
    'pub_ssn': ['ntc_id', 'ssn_ref', 'sec_code', 'pub_no'],
    'com_el': ['ntc_id', 'adm', 'sat_name', 'ntc_type', 'wic_no', 'ssn_ref'],
    'grp': ['ntc_id', 'grp_id', 'beam_name', 'eirp_nom', 'polar_type'],
    'freq': ['grp_id', 'seq_no', 'freq_mhz', 'freq_min', 'freq_max', 'bdwdth'],
    's_beam': ['ntc_id', 'beam_name', 'gain'],
    'emiss': ['grp_id', 'seq_no', 'design_emi', 'pep_max', 'pep_min'],
    'orbit': ['ntc_id', 'nbr_sat_pl', 'inclin_ang', 'apog_km', 'perig_km', 'op_ht_km']
}

def get_engine():
    return create_engine(DB_URL)

def init_db_and_refresh_view(engine):
    """Crea la struttura iniziale delle tabelle se assente e aggiorna la vista SQL."""
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tbl_notice (ntc_id BIGINT, adm TEXT, sat_name TEXT, ntc_type TEXT, wic_no BIGINT, ssn_ref TEXT);
            CREATE TABLE IF NOT EXISTS tbl_com_el (ntc_id BIGINT, adm TEXT, sat_name TEXT, ntc_type TEXT, wic_no BIGINT, ssn_ref TEXT);
            CREATE TABLE IF NOT EXISTS tbl_grp (ntc_id BIGINT, grp_id BIGINT, beam_name TEXT, eirp_nom DOUBLE PRECISION, polar_type TEXT);
            CREATE TABLE IF NOT EXISTS tbl_freq (grp_id BIGINT, seq_no BIGINT, freq_mhz DOUBLE PRECISION, freq_min DOUBLE PRECISION, freq_max DOUBLE PRECISION, bdwdth DOUBLE PRECISION);
            CREATE TABLE IF NOT EXISTS tbl_s_beam (ntc_id BIGINT, beam_name TEXT, gain DOUBLE PRECISION);
            CREATE TABLE IF NOT EXISTS tbl_emiss (grp_id BIGINT, seq_no BIGINT, design_emi TEXT, pep_max DOUBLE PRECISION, pep_min DOUBLE PRECISION);
            CREATE TABLE IF NOT EXISTS tbl_orbit (ntc_id BIGINT, nbr_sat_pl BIGINT, inclin_ang DOUBLE PRECISION, apog_km DOUBLE PRECISION, perig_km DOUBLE PRECISION, op_ht_km DOUBLE PRECISION);
        """))
        
        conn.execute(text("DROP VIEW IF EXISTS notice_summary CASCADE;"))
        conn.execute(text("""
            CREATE VIEW notice_summary AS
            SELECT 
                c.ntc_id,
                c.sat_name,
                c.adm,
                c.ssn_ref,
                c.wic_no,
                g.grp_id,
                g.beam_name,
                g.eirp_nom,
                g.polar_type,
                f.freq_mhz,
                f.freq_min,
                f.freq_max,
                f.bdwdth,
                b.gain AS gain_dbi
            FROM tbl_freq f
            JOIN tbl_grp g ON f.grp_id = g.grp_id
            JOIN tbl_com_el c ON g.ntc_id = c.ntc_id
            LEFT JOIN tbl_s_beam b ON (g.ntc_id = b.ntc_id AND g.beam_name = b.beam_name);
        """))

def get_imported_wics(engine):
    """Recupera le WIC già presenti nel database per saltare i download duplicati."""
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT DISTINCT wic_no FROM tbl_com_el WHERE wic_no IS NOT NULL;"))
            return set(row[0] for row in result.fetchall())
    except Exception:
        return set()

def build_ific_url(wic_no):
    """Gestisce il cambio di URL pattern a partire dalla WIC 3037."""
    if wic_no < SWITCH_URL_WIC:
        return f"https://www.itu.int/sns/converted-to-v10/ific{wic_no}.zip"
    else:
        return f"https://www.itu.int/sns/ific10/ific{wic_no}.zip"

def download_and_extract(wic_no, dest_dir):
    """Scarica il file ZIP, ne estrae il file MDB ed elimina subito lo ZIP."""
    url = build_ific_url(wic_no)
    zip_path = dest_dir / f"ific{wic_no}.zip"
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"\n📥 Download IFIC {wic_no} da: {url}")
    res = requests.get(url, stream=True, timeout=60)
    if res.status_code == 404:
        print(f"ℹ️ IFIC {wic_no} non trovata sul server ITU (404).")
        return None
    res.raise_for_status()

    with open(zip_path, 'wb') as f:
        for chunk in res.iter_content(chunk_size=8192):
            f.write(chunk)

    extracted_mdb = None
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        for name in zip_ref.namelist():
            if name.lower().endswith('.mdb'):
                zip_ref.extract(name, dest_dir)
                extracted_mdb = dest_dir / name
                break
    
    zip_path.unlink(missing_ok=True)
    return extracted_mdb

def export_mdb_to_csv(mdb_path, table_name, out_dir):
    """Esporta la tabella dal DB Access a CSV mediante mdb-export."""
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_file = out_dir / f"{table_name}.csv"
    with open(csv_file, 'w') as f:
        subprocess.run(['mdb-export', str(mdb_path), table_name], stdout=f, check=True)
    return csv_file

def process_single_mdb(mdb_path, current_wic, engine):
    """Elabora le tabelle MDB, applica i filtri e scrive in append su Postgres."""
    dfs = {}
    for table, cols in TABLE_COLUMNS.items():
        try:
            csv_path = export_mdb_to_csv(mdb_path, table, CSV_DIR)
            df = pd.read_csv(csv_path, low_memory=False)
            df.columns = df.columns.str.strip().str.lower()
            valid_cols = [c for c in cols if c in df.columns]
            dfs[table] = df[valid_cols].drop_duplicates()
        except Exception:
            pass

    # Elimina la cartella temporanea dei CSV estratti per questa WIC
    shutil.rmtree(CSV_DIR, ignore_errors=True)

    if 'pub_ssn' not in dfs or dfs['pub_ssn'].empty:
        print(f"⚠️ Tabella 'pub_ssn' mancante o vuota nella WIC {current_wic}.")
        return False

    df_ssn = dfs['pub_ssn'].copy()
    pattern_str = '|'.join([f"^{p}" for p in TARGET_SSN_PATTERNS])
    ssn_mask = df_ssn['ssn_ref'].astype(str).str.upper().str.contains(pattern_str, regex=True, na=False)
    
    df_ssn_filtered = df_ssn[ssn_mask]
    valid_ntc_ids = set(df_ssn_filtered['ntc_id'].dropna().unique())

    if not valid_ntc_ids:
        print(f"ℹ️ Nessun record rilevante (API/A, CR/C, PART II-S) nella WIC {current_wic}.")
        return True

    df_ssn_agg = df_ssn_filtered.groupby('ntc_id', as_index=False).agg({
        'ssn_ref': lambda x: ', '.join(map(str, x.dropna().unique()))
    })

    for t in ['com_el', 'notice']:
        if t in dfs:
            dfs[t] = dfs[t][dfs[t]['ntc_id'].isin(valid_ntc_ids)]
            dfs[t] = pd.merge(dfs[t], df_ssn_agg, on='ntc_id', how='left')
            dfs[t]['wic_no'] = current_wic

    for t in ['s_beam', 'orbit', 'grp']:
        if t in dfs:
            dfs[t] = dfs[t][dfs[t]['ntc_id'].isin(valid_ntc_ids)]

    valid_grp_ids = set(dfs['grp']['grp_id'].dropna().unique()) if 'grp' in dfs else set()
    for t in ['freq', 'emiss']:
        if t in dfs:
            dfs[t] = dfs[t][dfs[t]['grp_id'].isin(valid_grp_ids)]

    if 'freq' in dfs:
        dfs['freq']['freq_mhz'] = dfs['freq']['freq_mhz'].fillna(
            (dfs['freq']['freq_min'] + dfs['freq']['freq_max']) / 2.0
        )
        dfs['freq']['bdwdth'] = dfs['freq']['freq_max'] - dfs['freq']['freq_min']

    if all(k in dfs for k in ['grp', 's_beam', 'emiss']):
        beam_gain = dfs['s_beam'].groupby(['ntc_id', 'beam_name'], as_index=False)['gain'].max()
        emiss_pep = dfs['emiss'].groupby('grp_id', as_index=False)['pep_max'].max()
        grp_calc = dfs['grp'].merge(beam_gain, on=['ntc_id', 'beam_name'], how='left')
        grp_calc = grp_calc.merge(emiss_pep, on='grp_id', how='left')
        grp_calc['eirp_nom'] = grp_calc['eirp_nom'].fillna(grp_calc['pep_max'] + grp_calc['gain'])
        dfs['grp'] = grp_calc[TABLE_COLUMNS['grp']]

    # Caricamento in append su Postgres
    for table_name, df in dfs.items():
        if table_name == 'pub_ssn':
            continue
        df.to_sql(f"tbl_{table_name}", con=engine, if_exists='append', index=False)

    return True

def main():
    engine = get_engine()
    print("🛠️ Verifico e inizializzo lo schema del DB PostgreSQL...")
    init_db_and_refresh_view(engine)

    imported_wics = get_imported_wics(engine)
    current_wic = START_WIC
    max_missing_consecutive = 3
    missing_counter = 0

    print(f"🚀 Avvio procedura Ingestion BR IFIC da WIC: {START_WIC}")
    print(f"📊 WIC già memorizzate nel DB: {len(imported_wics)}")

    while missing_counter < max_missing_consecutive:
        if current_wic in imported_wics:
            print(f"⏩ WIC {current_wic} già presente a DB. Salto.")
            current_wic += 1
            missing_counter = 0
            continue

        mdb_path = download_and_extract(current_wic, DOWNLOAD_DIR)
        if not mdb_path:
            missing_counter += 1
            current_wic += 1
            continue

        missing_counter = 0
        print(f"⚙️ Caricamento ed elaborazione WIC {current_wic}...")
        success = process_single_mdb(mdb_path, current_wic, engine)
        
        # Pulizia del file .mdb e della cartella download per ogni ciclo
        mdb_path.unlink(missing_ok=True)
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

        if success:
            print(f"✅ WIC {current_wic} completata con successo!")
        
        current_wic += 1

    print("\n⚡ Aggiornamento e refresh finale della vista SQL 'notice_summary'...")
    init_db_and_refresh_view(engine)
    print("🏁 Ingestion automatica completata con successo!")

if __name__ == "__main__":
    main()
