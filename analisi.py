import os
import pandas as pd
from sqlalchemy import create_engine

DB_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql://itu_admin:supersecretpassword@itu_postgres:5432/itu_spectrum_db"
)

def run_analysis():
    print("🔌 Connessione al database PostgreSQL...")
    try:
        engine = create_engine(DB_URL)
        with engine.connect() as connection:
            print("✅ Connessione al DB riuscita!")
    except Exception as e:
        print(f"❌ Errore di connessione: {e}")
        return

    output_filename = "Analisi_API_A_WIC.xlsx"

    # Query 1: Totale API/A per WIC usando ssn_ref direttamente in tbl_com_el
    query_by_wic = """
    SELECT 
        c.wic_no AS wic,
        COUNT(DISTINCT c.ntc_id) AS totale_api_a
    FROM tbl_com_el c
    WHERE UPPER(COALESCE(c.ssn_ref, '')) LIKE 'API/A%%'
    GROUP BY c.wic_no
    ORDER BY c.wic_no DESC;
    """
    
    # Query 2: API/A per WIC e Nazione (adm)
    query_by_wic_adm = """
    SELECT 
        c.wic_no AS wic,
        c.adm AS nazione,
        COUNT(DISTINCT c.ntc_id) AS totale_api_a
    FROM tbl_com_el c
    WHERE UPPER(COALESCE(c.ssn_ref, '')) LIKE 'API/A%%'
    GROUP BY c.wic_no, c.adm
    ORDER BY c.wic_no DESC, totale_api_a DESC;
    """

    # Query 3: Totale complessivo per Nazione (adm)
    query_by_adm = """
    SELECT 
        c.adm AS nazione,
        COUNT(DISTINCT c.ntc_id) AS totale_api_a
    FROM tbl_com_el c
    WHERE UPPER(COALESCE(c.ssn_ref, '')) LIKE 'API/A%%'
    GROUP BY c.adm
    ORDER BY totale_api_a DESC;
    """

    try:
        df_wic = pd.read_sql(query_by_wic, con=engine)
        df_wic_adm = pd.read_sql(query_by_wic_adm, con=engine)
        df_adm = pd.read_sql(query_by_adm, con=engine)

        if not df_wic_adm.empty:
            df_pivot = df_wic_adm.pivot(index='nazione', columns='wic', values='totale_api_a').fillna(0).astype(int)
        else:
            df_pivot = pd.DataFrame()

        print(f"💾 Scrittura file Excel '{output_filename}'...")
        with pd.ExcelWriter(output_filename, engine='openpyxl') as writer:
            df_wic.to_excel(writer, sheet_name='Totale per WIC', index=False)
            df_wic_adm.to_excel(writer, sheet_name='Per WIC e Nazione', index=False)
            df_adm.to_excel(writer, sheet_name='Totale per Nazione', index=False)
            if not df_pivot.empty:
                df_pivot.to_excel(writer, sheet_name='Pivot Nazione vs WIC')

        totale = df_wic['totale_api_a'].sum() if not df_wic.empty else 0
        print(f"🎉 Trovati {totale} record API/A totali in tbl_com_el!")
        print(f"📁 File generato con successo: {output_filename}")

    except Exception as e:
        print(f"❌ Errore durante l'analisi: {e}")

if __name__ == "__main__":
    run_analysis()
