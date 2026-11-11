import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text
import warnings
import sys

# Suppress pandas warnings for cleaner output
warnings.filterwarnings('ignore')

# ==========================================
# 1. CONFIGURATION
# ==========================================
DB_USER = 'root'
DB_PASS = 'root'
DB_HOST = 'localhost'
DB_NAME = 'international_debt_db'

FILES = {
    'fact': 'IDS_ALLCountries_Data.csv',
    'country': 'IDS_CountryMetaData.csv',
    'series': 'IDS_SeriesMetaData.csv',
    'footnotes': 'IDS_FootNoteMetaData.csv',
    'country_series': 'Country-Series - Metadata.csv'
}

# The World Bank exports are Windows-1252, not UTF-8. Reading them as latin-1 turned curly
# quotes and dashes (bytes 0x80-0x9F) into invisible control characters, e.g. 'country\x92s'.
CSV_ENCODING = 'cp1252'

def read_csv_raw(file_path):
    """Read every column as text so values like '2020' are not turned into '2020.0'.
    Only truly empty cells become NaN ('NA' stays 'NA', e.g. Namibia's 2-alpha code)."""
    return pd.read_csv(file_path, dtype=str, encoding=CSV_ENCODING, keep_default_na=False, na_values=[''])

def clean_text_series(series):
    """Utility to strip whitespace and convert empty/whitespace strings to NaN/None."""
    if series is None:
        return series
    cleaned = series.astype(str).str.strip()
    cleaned = cleaned.replace(['nan', 'None', '', 'NaN'], np.nan)
    return cleaned

def extract_code(series):
    """Pull the code from labels like 'Name (with (nested) parens) (CODE)'.
    Takes the LAST parenthesis group. The old r'\\((.*?)\\)$' started at the FIRST '(' and
    returned fragments such as 'current US$) (BX.TRF.PWKR.CD.DT'."""
    return clean_text_series(series.astype(str).str.extract(r'\(([^()]*)\)\s*$')[0])

def derive_unit(indicator_name):
    """Unit of measure from the last parenthesis group of the indicator name."""
    name = str(indicator_name)
    groups = pd.Series([name]).str.findall(r'\(([^()]*)\)')[0]
    last = groups[-1] if groups else ''
    if 'US$' in last:
        return 'US$'
    if '%' in last:
        return '%'
    if 'years' in last:
        return 'years'
    if 'months' in name.lower():
        return 'months'
    return 'count'

def process_fact_data(file_path):
    """Loads and cleans the main wide-format fact data, dropping 2025-2032+ years."""
    print("  -> Loading and transposing fact data (Fact Table)...")

    df_wide = read_csv_raw(file_path)

    # Drop trailing summary / blank rows if any exist
    df_wide = df_wide.dropna(subset=['Country Code', 'Series Code'])

    # Drop future years (2025 onwards)
    years_to_drop = [str(year) for year in range(2025, 2040)]
    df_wide = df_wide.drop(columns=[col for col in years_to_drop if col in df_wide.columns], errors='ignore')

    # Strip whitespace from keys
    for col in ['Country Code', 'Counterpart-Area Code', 'Series Code']:
        if col in df_wide.columns:
            df_wide[col] = clean_text_series(df_wide[col])

    # Melt (Wide to Long)
    id_vars = ['Country Name', 'Country Code', 'Counterpart-Area Name',
               'Counterpart-Area Code', 'Series Name', 'Series Code']
    year_cols = [col for col in df_wide.columns if col not in id_vars]

    df_fact = pd.melt(df_wide, id_vars=id_vars, value_vars=year_cols,
                      var_name='year', value_name='debt_value')

    # Handle Nulls & Forward Fill inside gaps, flagging every value that was filled
    df_fact['debt_value'] = clean_text_series(df_fact['debt_value'])
    df_fact = df_fact.sort_values(by=['Country Code', 'Counterpart-Area Code', 'Series Code', 'year'])

    reported = df_fact['debt_value'].notna()
    df_fact['debt_value'] = df_fact.groupby(['Country Code', 'Counterpart-Area Code', 'Series Code'])['debt_value'].transform(
        lambda x: x.ffill(limit_area='inside')
    )
    df_fact['is_imputed'] = (~reported & df_fact['debt_value'].notna()).astype(int)

    stats = {'raw_reported': int(reported.sum()), 'imputed': int(df_fact['is_imputed'].sum())}

    # Drop remaining NaNs and convert types
    df_fact = df_fact.dropna(subset=['debt_value'])
    df_fact['year'] = pd.to_numeric(df_fact['year'], errors='coerce').astype(int)
    df_fact['debt_value'] = pd.to_numeric(df_fact['debt_value'].str.replace(',', '', regex=False), errors='coerce')
    non_numeric = int(df_fact['debt_value'].isna().sum())
    if non_numeric:
        print(f"  -> WARNING: {non_numeric} non-numeric debt values dropped.")
    df_fact = df_fact.dropna(subset=['debt_value'])

    # Drop duplicates
    before = len(df_fact)
    df_fact = df_fact.drop_duplicates(subset=['Country Code', 'Counterpart-Area Code', 'Series Code', 'year'], keep='first')
    stats['duplicates'] = before - len(df_fact)

    # Rename to snake_case
    df_fact.rename(columns={
        'Country Code': 'country_code',
        'Counterpart-Area Code': 'counterpart_code',
        'Series Code': 'series_code'
    }, inplace=True)

    # Filter valid counterpart codes
    df_fact = df_fact[df_fact['counterpart_code'].notna() & (df_fact['counterpart_code'] != '')]

    return df_fact[['country_code', 'counterpart_code', 'series_code', 'year', 'debt_value', 'is_imputed']], df_wide, stats

def process_metadata(df_wide_fact):
    """Loads, cleans, and retains all columns from all metadata files without data loss."""
    print("  -> Processing all metadata files (retaining all variables)...")

    # 1. Country Dimension (Retaining all relevant metadata columns)
    df_country = read_csv_raw(FILES['country'])
    df_country['Code'] = clean_text_series(df_country['Code'])

    country_cols_map = {
        'Code': 'country_code', 'Long Name': 'country_name', 'Short Name': 'short_name',
        'Table Name': 'table_name', 'Income Group': 'income_group', 'Region': 'region',
        'Lending category': 'lending_category', 'Other groups': 'other_groups',
        'Currency Unit': 'currency_unit', '2-alpha code': 'alpha_2_code', 'WB-2 code': 'wb_2_code',
        'Special Notes': 'special_notes', 'National accounts base year': 'national_accounts_base_year',
        'External debt Reporting status': 'external_debt_reporting_status',
        'System of trade': 'system_trade', 'Government Accounting concept': 'government_accounting_concept',
        'Latest population census': 'latest_population_census', 'Latest household survey': 'latest_household_survey',
        'National accounts reference year': 'national_accounts_reference_year',
        'System of National Accounts': 'system_of_national_accounts', 'SNA price valuation': 'sna_price_valuation',
        'PPP survey years': 'ppp_survey_years', 'Balance of Payments Manual in use': 'bop_manual_in_use',
        'IMF data dissemination standard': 'imf_data_dissemination_standard',
        'Source of most recent Income and expenditure data': 'source_income_expenditure_data',
        'Vital registration complete': 'vital_registration_complete',
        'Latest agricultural census': 'latest_agricultural_census', 'Latest industrial data': 'latest_industrial_data',
        'Latest trade data': 'latest_trade_data', 'Latest water withdrawal data': 'latest_water_withdrawal_data'
    }

    # Keep columns that exist in the dataframe
    existing_country_cols = {k: v for k, v in country_cols_map.items() if k in df_country.columns}
    df_country_clean = df_country[list(existing_country_cols.keys())].rename(columns=existing_country_cols)

    # Clean string columns, leave Other groups as null if missing
    for col in df_country_clean.columns:
        if col != 'country_code':
            df_country_clean[col] = clean_text_series(df_country_clean[col])

    df_country_clean = df_country_clean.dropna(subset=['country_code']).drop_duplicates(subset=['country_code'])

    # Regional / income-group aggregates (LMY, LMC, SSA, ...) have no Region. Their values are
    # sums of member countries, so they must be excluded from country rankings and global totals.
    df_country_clean['is_aggregate'] = df_country_clean['region'].isna().astype(int)
    print(f"  -> Flagged {df_country_clean['is_aggregate'].sum()} aggregate rows: "
          f"{', '.join(df_country_clean.loc[df_country_clean['is_aggregate'] == 1, 'country_code'])}")

    # 2. Series Dimension (Retaining all columns + dynamic handling for missing fact series)
    df_series = read_csv_raw(FILES['series'])
    df_series['Code'] = clean_text_series(df_series['Code'])

    series_cols_map = {
        'Code': 'series_code', 'License Type': 'license_type', 'Indicator Name': 'indicator_name', 'Short definition': 'short_definition',
        'Long definition': 'long_definition', 'Source': 'source', 'Topic': 'topic',
        'Dataset': 'dataset', 'Periodicity': 'periodicity', 'Aggregation method': 'aggregation_method',
        'Limitations and exceptions': 'limitations_and_exceptions', 'General comments': 'general_comments'
    }
    existing_series_cols = {k: v for k, v in series_cols_map.items() if k in df_series.columns}
    df_series_clean = df_series[list(existing_series_cols.keys())].rename(columns=existing_series_cols)

    for col in df_series_clean.columns:
        if col != 'series_code':
            df_series_clean[col] = clean_text_series(df_series_clean[col])

    df_series_clean = df_series_clean.dropna(subset=['series_code']).drop_duplicates(subset=['series_code'])

    # Check for any missing series codes in fact data and dynamically add them from fact 'Series Name'
    fact_series_unique = df_wide_fact[['Series Code', 'Series Name']].dropna().drop_duplicates()
    fact_series_unique['Series Code'] = clean_text_series(fact_series_unique['Series Code'])
    fact_series_unique['Series Name'] = clean_text_series(fact_series_unique['Series Name'])
    existing_codes = set(df_series_clean['series_code'])

    missing_series = fact_series_unique[~fact_series_unique['Series Code'].isin(existing_codes)]
    if not missing_series.empty:
        print(f"  -> Note: Found {len(missing_series)} series codes in fact data missing from series metadata. Dynamically registering them.")
        # Topic is taken from metadata series sharing the same measure segment (DT.DOD.* -> Debt outstanding)
        segment = df_series_clean['series_code'].str.split('.').str[:2].str.join('.')
        topic_by_segment = df_series_clean.groupby(segment)['topic'].agg(lambda t: t.mode().iat[0] if t.notna().any() else None)
        new_rows = []
        for _, row in missing_series.iterrows():
            new_rows.append({
                'series_code': row['Series Code'],
                'indicator_name': row['Series Name'],
                'short_definition': None,
                'long_definition': None,
                'source': 'World Bank (Extracted from Fact Data)',
                'topic': topic_by_segment.get('.'.join(row['Series Code'].split('.')[:2]), 'Unclassified / IMF Debt'),
                'dataset': 'International Debt Statistics',
                'periodicity': 'Annual',
                'aggregation_method': 'Sum'
            })
        df_series_clean = pd.concat([df_series_clean, pd.DataFrame(new_rows)], ignore_index=True)

    # The fact file names 8 series differently from the metadata file (e.g. 'Use of IMF credit
    # and SDR allocations' vs 'Use of IMF credit'). Keep both so neither source is lost.
    df_series_clean = df_series_clean.merge(
        fact_series_unique.drop_duplicates(subset=['Series Code']).rename(
            columns={'Series Code': 'series_code', 'Series Name': 'fact_series_name'}),
        on='series_code', how='left')

    # Analysis attributes derived from the series' own metadata
    df_series_clean['indicator_category'] = df_series_clean['topic'].str.split(':').str[-1].str.strip()
    df_series_clean['unit'] = df_series_clean['indicator_name'].fillna(df_series_clean['fact_series_name']).map(derive_unit)
    df_series_clean['is_total'] = (
        df_series_clean['indicator_name'].str.contains(', total (', regex=False, na=False)
        & (df_series_clean['unit'] == 'US$')
    ).astype(int)
    print(f"  -> Headline total series: {', '.join(df_series_clean.loc[df_series_clean['is_total'] == 1, 'series_code'])}")

    # 3. Counterpart Dimension
    df_cp_names = df_wide_fact[['Counterpart-Area Code', 'Counterpart-Area Name']].drop_duplicates()
    df_cp_names.rename(columns={'Counterpart-Area Code': 'counterpart_code', 'Counterpart-Area Name': 'counterpart_name'}, inplace=True)
    df_cp_names['counterpart_code'] = clean_text_series(df_cp_names['counterpart_code'])
    df_cp_names['counterpart_name'] = clean_text_series(df_cp_names['counterpart_name'])
    df_cp_names = df_cp_names[df_cp_names['counterpart_code'].notna() & (df_cp_names['counterpart_code'] != '')].drop_duplicates(subset=['counterpart_code'])

    # 4. Footnotes
    df_foot = read_csv_raw(FILES['footnotes'])
    if 'Country Code' in df_foot.columns and 'Series Code' in df_foot.columns:
        df_foot['country_code'] = extract_code(df_foot['Country Code'])
        df_foot['series_code'] = extract_code(df_foot['Series Code'])
        df_foot['year'] = pd.to_numeric(df_foot['Time Code'].str.extract(r'(\d{4})')[0], errors='coerce').astype('Int64')
        df_foot['description'] = clean_text_series(df_foot['Description'])
        df_foot_clean = df_foot[['country_code', 'series_code', 'year', 'description', 'Type']].dropna(subset=['country_code', 'series_code']).copy()
    else:
        df_foot_clean = pd.DataFrame(columns=['country_code', 'series_code', 'year', 'description', 'Type'])

    # 5. Country-Series Metadata (New table integration)
    df_cs = read_csv_raw(FILES['country_series'])
    if 'Country Code' in df_cs.columns and 'Series Code' in df_cs.columns:
        df_cs['country_code'] = extract_code(df_cs['Country Code'])
        df_cs['series_code'] = extract_code(df_cs['Series Code'])
        df_cs['description'] = clean_text_series(df_cs['Description'])
        df_cs_clean = df_cs[['country_code', 'series_code', 'description', 'Type']].dropna(subset=['country_code', 'series_code']).copy()
    else:
        df_cs_clean = pd.DataFrame(columns=['country_code', 'series_code', 'description', 'Type'])

    return df_country_clean, df_series_clean, df_cp_names, df_foot_clean, df_cs_clean

def setup_database_and_load(df_fact, df_country, df_series, df_cp_names, df_foot, df_cs, fact_stats):
    """Recreates the MySQL star schema and loads every table (drop + create + insert)."""

    # Connect to Server
    engine_server = create_engine(f'mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}/')
    try:
        with engine_server.connect() as conn:
            conn.execute(text(f"CREATE DATABASE IF NOT EXISTS {DB_NAME};"))
            conn.commit()
    except Exception as e:
        print(f"❌ Error connecting to MySQL Server: {e}")
        sys.exit(1)

    engine = create_engine(f'mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}/{DB_NAME}')

    print("  -> Recreating normalized star schema in MySQL...")
    schema_query = """
    SET FOREIGN_KEY_CHECKS = 0;
    DROP TABLE IF EXISTS international_debt_datamart;
    DROP TABLE IF EXISTS country_series_dim;
    DROP TABLE IF EXISTS footnotes;
    DROP TABLE IF EXISTS debt_fact;
    DROP TABLE IF EXISTS counterpart_dim;
    DROP TABLE IF EXISTS series_dim;
    DROP TABLE IF EXISTS country_dim;
    SET FOREIGN_KEY_CHECKS = 1;

    CREATE TABLE country_dim (
        country_code VARCHAR(10) PRIMARY KEY,
        country_name VARCHAR(255),
        short_name VARCHAR(255),
        table_name VARCHAR(255),
        income_group VARCHAR(100),
        region VARCHAR(100),
        lending_category VARCHAR(255),
        other_groups TEXT,
        currency_unit VARCHAR(100),
        alpha_2_code VARCHAR(10),
        wb_2_code VARCHAR(10),
        special_notes TEXT,
        national_accounts_base_year TEXT,
        external_debt_reporting_status VARCHAR(255),
        system_trade VARCHAR(100),
        government_accounting_concept VARCHAR(100),
        latest_population_census VARCHAR(255),
        latest_household_survey VARCHAR(255),
        national_accounts_reference_year VARCHAR(255),
        system_of_national_accounts VARCHAR(255),
        sna_price_valuation VARCHAR(255),
        ppp_survey_years VARCHAR(255),
        bop_manual_in_use VARCHAR(255),
        imf_data_dissemination_standard VARCHAR(255),
        source_income_expenditure_data VARCHAR(255),
        vital_registration_complete VARCHAR(255),
        latest_agricultural_census VARCHAR(255),
        latest_industrial_data VARCHAR(255),
        latest_trade_data VARCHAR(255),
        latest_water_withdrawal_data VARCHAR(255),
        is_aggregate TINYINT(1) NOT NULL
    );

    CREATE TABLE series_dim (
        series_code VARCHAR(50) PRIMARY KEY,
        license_type VARCHAR(100),
        indicator_name TEXT,
        short_definition TEXT,
        long_definition TEXT,
        source TEXT,
        topic TEXT,
        dataset VARCHAR(100),
        periodicity VARCHAR(50),
        aggregation_method VARCHAR(100),
        limitations_and_exceptions TEXT,
        general_comments TEXT,
        fact_series_name TEXT,
        indicator_category VARCHAR(100),
        unit VARCHAR(20),
        is_total TINYINT(1) NOT NULL
    );

    CREATE TABLE counterpart_dim (
        counterpart_code VARCHAR(10) PRIMARY KEY,
        counterpart_name VARCHAR(255)
    );

    CREATE TABLE debt_fact (
        fact_id INT AUTO_INCREMENT PRIMARY KEY,
        country_code VARCHAR(10),
        counterpart_code VARCHAR(10),
        series_code VARCHAR(50),
        year INT,
        debt_value DECIMAL(30, 9),
        is_imputed TINYINT(1) NOT NULL,
        FOREIGN KEY (country_code) REFERENCES country_dim(country_code),
        FOREIGN KEY (counterpart_code) REFERENCES counterpart_dim(counterpart_code),
        FOREIGN KEY (series_code) REFERENCES series_dim(series_code)
    );

    CREATE TABLE footnotes (
        footnote_id INT AUTO_INCREMENT PRIMARY KEY,
        country_code VARCHAR(100),
        series_code VARCHAR(100),
        year INT,
        description TEXT,
        type VARCHAR(50)
    );

    CREATE TABLE country_series_dim (
        cs_id INT AUTO_INCREMENT PRIMARY KEY,
        country_code VARCHAR(100),
        series_code VARCHAR(100),
        description TEXT,
        type VARCHAR(50)
    );
    """

    with engine.begin() as conn:
        for statement in schema_query.split(';'):
            if statement.strip():
                conn.execute(text(statement))

    # Tables were just recreated empty, so 'append' loads them in full
    print("  -> Loading data into tables...")
    df_country.to_sql('country_dim', con=engine, if_exists='append', index=False)
    df_series.to_sql('series_dim', con=engine, if_exists='append', index=False)
    df_cp_names.to_sql('counterpart_dim', con=engine, if_exists='append', index=False)
    df_fact.to_sql('debt_fact', con=engine, if_exists='append', index=False, chunksize=50000)

    # Footnote years stay NULL when the source has none (was 0 in v7)
    df_foot.to_sql('footnotes', con=engine, if_exists='append', index=False, chunksize=1000)
    df_cs.to_sql('country_series_dim', con=engine, if_exists='append', index=False)

    # Datamart built only from the base tables; LEFT JOINs keep every fact row
    print("  -> Creating international_debt_datamart from the base tables...")
    datamart_query = """
    CREATE TABLE international_debt_datamart AS
    SELECT
        c.country_code,
        c.country_name,
        c.region,
        c.income_group,
        c.other_groups,
        s.series_code,
        s.indicator_name,
        s.topic,
        s.source,
        cp.counterpart_name,
        f.year,
        f.debt_value,
        f.counterpart_code,
        c.short_name,
        c.lending_category,
        c.currency_unit,
        s.dataset,
        s.periodicity,
        s.aggregation_method,
        s.indicator_category,
        s.unit,
        s.is_total,
        c.is_aggregate,
        f.is_imputed
    FROM debt_fact f
    LEFT JOIN country_dim c ON f.country_code = c.country_code
    LEFT JOIN series_dim s ON f.series_code = s.series_code
    LEFT JOIN counterpart_dim cp ON f.counterpart_code = cp.counterpart_code;
    """
    with engine.begin() as conn:
        conn.execute(text(datamart_query))
        conn.execute(text("CREATE INDEX idx_dm_series_year ON international_debt_datamart (series_code, year)"))
        conn.execute(text("CREATE INDEX idx_dm_country ON international_debt_datamart (country_code)"))

    # Loss check: compare rows prepared in pandas vs rows now in MySQL
    expected = {'country_dim': len(df_country), 'series_dim': len(df_series), 'counterpart_dim': len(df_cp_names),
                'debt_fact': len(df_fact), 'footnotes': len(df_foot), 'country_series_dim': len(df_cs),
                'international_debt_datamart': len(df_fact)}
    print("\n--- ROW COUNT CHECK (pandas vs MySQL) ---")
    with engine.connect() as conn:
        for t, n in expected.items():
            got = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            print(f"  {t:30s} expected={n:>9,}  loaded={got:>9,}  {'OK' if n == got else '<-- MISMATCH'}")
        orph_f = conn.execute(text("SELECT COUNT(*) FROM footnotes f LEFT JOIN series_dim s ON f.series_code=s.series_code WHERE s.series_code IS NULL")).scalar()
        orph_c = conn.execute(text("SELECT COUNT(*) FROM country_series_dim f LEFT JOIN series_dim s ON f.series_code=s.series_code WHERE s.series_code IS NULL")).scalar()
        print(f"  footnotes with unknown series_code: {orph_f:,} | country_series_dim with unknown series_code: {orph_c:,}")

        # Source check: every reported CSV value plus every filled gap must be in MySQL
        reported = conn.execute(text("SELECT COUNT(*) FROM debt_fact WHERE is_imputed = 0")).scalar()
        imputed = conn.execute(text("SELECT COUNT(*) FROM debt_fact WHERE is_imputed = 1")).scalar()
        print("\n--- SOURCE CHECK (CSV vs MySQL) ---")
        print(f"  reported values  csv={fact_stats['raw_reported']:>9,}  loaded={reported:>9,}  "
              f"{'OK' if fact_stats['raw_reported'] - fact_stats['duplicates'] == reported else '<-- MISMATCH'}")
        print(f"  imputed values   ffill={fact_stats['imputed']:>9,}  loaded={imputed:>9,}  "
              f"{'OK' if fact_stats['imputed'] == imputed else '<-- MISMATCH'}")
        print(f"  duplicate rows dropped: {fact_stats['duplicates']:,}")
    return engine

def run_verification(engine):
    """Runs a verification query on the datamart."""
    print("\n--- VERIFICATION: Top 10 Countries by Total External Debt (2022) ---")
    query = """
    SELECT
        country_name,
        region,
        SUM(debt_value) AS total_debt_usd
    FROM international_debt_datamart
    WHERE series_code = 'DT.DOD.DECT.CD'
      AND counterpart_name = 'World'
      AND year = 2022
      AND is_aggregate = 0
    GROUP BY country_name, region
    ORDER BY total_debt_usd DESC
    LIMIT 10;
    """
    try:
        df_result = pd.read_sql(query, con=engine)
        print(df_result.to_string(index=False))
    except Exception as e:
        print(f"❌ Verification failed: {e}")

# ==========================================
# MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    print("🚀 Starting Refactored International Debt Pipeline (v8)...\n")

    try:
        df_fact, df_wide, fact_stats = process_fact_data(FILES['fact'])
        df_country, df_series, df_cp_names, df_foot, df_cs = process_metadata(df_wide)
    except FileNotFoundError as e:
        print(f"❌ Error: Could not find CSV file. {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Data Processing Error: {e}")
        sys.exit(1)

    print(f"✅ Data processing complete. Fact rows: {len(df_fact)}")

    print("\n🐬 Connecting to MySQL and loading data...")
    engine = setup_database_and_load(df_fact, df_country, df_series, df_cp_names, df_foot, df_cs, fact_stats)
    print("✅ Database setup and load complete.")

    run_verification(engine)

    print("\n🎉 Pipeline executed successfully!")
