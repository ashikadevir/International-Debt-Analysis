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
DB_USER = 'root'           # Update with your MySQL username
DB_PASS = 'password'       # Update with your MySQL password
DB_HOST = 'localhost'
DB_NAME = 'international_debt_db'

FILES = {
    'fact': 'IDS_ALLCountries_Data.csv',
    'country': 'IDS_CountryMetaData.csv',
    'series': 'IDS_SeriesMetaData.csv',
    'footnotes': 'IDS_FootNoteMetaData.csv',
    'country_series': 'Country-Series - Metadata.csv'
}

def process_fact_data(file_path):
    """Loads and cleans the main wide-format fact data."""
    print("  -> Loading and transposing fact data...")
    df_wide = pd.read_csv(file_path, dtype=str)
    
    # Drop future years
    years_to_drop = [str(year) for year in range(2025, 2034)]
    df_wide = df_wide.drop(columns=years_to_drop, errors='ignore')
    
    # Strip whitespace from keys
    for col in ['Country Code', 'Counterpart-Area Code', 'Series Code']:
        df_wide[col] = df_wide[col].str.strip()
        
    # Melt (Wide to Long)
    id_vars = ['Country Name', 'Country Code', 'Counterpart-Area Name', 
               'Counterpart-Area Code', 'Series Name', 'Series Code']
    year_cols = [col for col in df_wide.columns if col not in id_vars]
    
    df_fact = pd.melt(df_wide, id_vars=id_vars, value_vars=year_cols, 
                      var_name='year', value_name='debt_value')
    
    # Handle Nulls & Forward Fill
    df_fact['debt_value'] = df_fact['debt_value'].replace(r'^\s*$', np.nan, regex=True)
    df_fact = df_fact.sort_values(by=['Country Code', 'Counterpart-Area Code', 'Series Code', 'year'])
    
    # Forward fill ONLY inside gaps (limit_area='inside')
    df_fact['debt_value'] = df_fact.groupby(['Country Code', 'Counterpart-Area Code', 'Series Code'])['debt_value'].transform(
        lambda x: x.ffill(limit_area='inside')
    )
    
    # Drop remaining NaNs and convert types
    df_fact = df_fact.dropna(subset=['debt_value'])
    df_fact['year'] = df_fact['year'].astype(int)
    df_fact['debt_value'] = pd.to_numeric(df_fact['debt_value'].str.replace(',', '', regex=False), errors='coerce')
    df_fact = df_fact.dropna(subset=['debt_value'])
    
    # Drop duplicates
    df_fact = df_fact.drop_duplicates(subset=['Country Code', 'Counterpart-Area Code', 'Series Code', 'year'], keep='first')
    
    # Rename to snake_case
    df_fact.rename(columns={
        'Country Code': 'country_code',
        'Counterpart-Area Code': 'counterpart_code',
        'Series Code': 'series_code',
        'year': 'year',
        'debt_value': 'debt_value'
    }, inplace=True)
    
    return df_fact[['country_code', 'counterpart_code', 'series_code', 'year', 'debt_value']]

def process_metadata():
    """Loads and cleans the metadata files."""
    print("  -> Processing metadata files...")
    
    # 1. Country Dimension
    df_country = pd.read_csv(FILES['country'])
    df_country['Code'] = df_country['Code'].str.strip()
    df_country_clean = df_country[['Code', 'Long Name', 'Income Group', 'Region', 'Lending category', 'Currency Unit']].copy()
    df_country_clean.rename(columns={
        'Code': 'country_code', 'Long Name': 'country_name', 
        'Income Group': 'income_group', 'Region': 'region', 
        'Lending category': 'lending_category', 'Currency Unit': 'currency_unit'
    }, inplace=True)
    df_country_clean = df_country_clean.dropna(subset=['country_code']).drop_duplicates(subset=['country_code'])
    
    # 2. Series Dimension
    df_series = pd.read_csv(FILES['series'])
    df_series['Code'] = df_series['Code'].str.strip()
    df_series_clean = df_series[['Code', 'Indicator Name', 'Topic', 'Aggregation method']].copy()
    df_series_clean.rename(columns={
        'Code': 'series_code', 'Indicator Name': 'indicator_name', 
        'Topic': 'topic', 'Aggregation method': 'aggregation_method'
    }, inplace=True)
    df_series_clean = df_series_clean.dropna(subset=['series_code']).drop_duplicates(subset=['series_code'])
    
    # 3. Footnotes
    df_foot = pd.read_csv(FILES['footnotes'])
    df_foot[['Country Name', 'country_code']] = df_foot['Country Code'].str.rsplit(' (', n=1, expand=True)
    df_foot['country_code'] = df_foot['country_code'].str.replace(')', '', regex=False).str.strip()
    
    df_foot[['Series Name', 'series_code']] = df_foot['Series Code'].str.rsplit(' (', n=1, expand=True)
    df_foot['series_code'] = df_foot['series_code'].str.replace(')', '', regex=False).str.strip()
    
    df_foot['year'] = df_foot['Time Code'].str.extract(r'(\d{4})').astype('Int64')
    df_foot_clean = df_foot[['country_code', 'series_code', 'year', 'Description']].copy()
    df_foot_clean.rename(columns={'Description': 'footnote_description'}, inplace=True)
    
    # 4. Country-Series Metadata (Unused in database but processed as requested)
    df_cs = pd.read_csv(FILES['country_series'])
    df_cs[['Country Name', 'country_code']] = df_cs['Country Code'].str.rsplit(' (', n=1, expand=True)
    df_cs['country_code'] = df_cs['country_code'].str.replace(')', '', regex=False).str.strip()
    df_cs[['Series Name', 'series_code']] = df_cs['Series Code'].str.rsplit(' (', n=1, expand=True)
    df_cs['series_code'] = df_cs['series_code'].str.replace(')', '', regex=False).str.strip()
    df_cs_clean = df_cs[['country_code', 'series_code', 'Description']].copy()
    df_cs_clean.rename(columns={'Description': 'series_country_description'}, inplace=True)
    
    return df_country_clean, df_series_clean, df_foot_clean, df_cs_clean

def setup_database_and_load(df_fact, df_country, df_series, df_foot):
    """Creates MySQL tables and inserts the cleaned data."""
    
    # Create Counterpart Dimension from Fact Data
    df_counterpart = df_fact[['counterpart_code']].drop_duplicates()
    # Note: The fact file has counterpart names, but we can map them here if needed. 
    # For simplicity, we'll just use the code as the name if we don't have a separate dict.
    # Actually, let's pull the name from the original wide file just in case.
    df_wide = pd.read_csv(FILES['fact'], dtype=str)
    df_wide['Counterpart-Area Code'] = df_wide['Counterpart-Area Code'].str.strip()
    df_cp_names = df_wide[['Counterpart-Area Code', 'Counterpart-Area Name']].drop_duplicates()
    df_cp_names.rename(columns={'Counterpart-Area Code': 'counterpart_code', 'Counterpart-Area Name': 'counterpart_name'}, inplace=True)
    
    # Connect to Server (to create DB if it doesn't exist)
    engine_server = create_engine(f'mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}/')
    try:
        with engine_server.connect() as conn:
            conn.execute(text(f"CREATE DATABASE IF NOT EXISTS {DB_NAME};"))
            conn.commit()
    except Exception as e:
        print(f"❌ Error connecting to MySQL Server: {e}")
        sys.exit(1)
        
    # Connect to specific DB
    engine = create_engine(f'mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}/{DB_NAME}')
    
    # Drop and Create Tables
    print("  -> Recreating schema (snake_case)...")
    schema_query = """
    SET FOREIGN_KEY_CHECKS = 0;
    DROP TABLE IF EXISTS footnotes;
    DROP TABLE IF EXISTS debt_fact;
    DROP TABLE IF EXISTS counterpart_dim;
    DROP TABLE IF EXISTS series_dim;
    DROP TABLE IF EXISTS country_dim;
    DROP TABLE IF EXISTS international_debt_datamart;
    SET FOREIGN_KEY_CHECKS = 1;

    CREATE TABLE country_dim (
        country_code VARCHAR(10) PRIMARY KEY,
        country_name VARCHAR(255),
        income_group VARCHAR(100),
        region VARCHAR(100),
        lending_category VARCHAR(50),
        currency_unit VARCHAR(100)
    );

    CREATE TABLE series_dim (
        series_code VARCHAR(50) PRIMARY KEY,
        indicator_name TEXT,
        topic TEXT,
        aggregation_method VARCHAR(50)
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
        debt_value DECIMAL(30, 4),
        FOREIGN KEY (country_code) REFERENCES country_dim(country_code),
        FOREIGN KEY (counterpart_code) REFERENCES counterpart_dim(counterpart_code),
        FOREIGN KEY (series_code) REFERENCES series_dim(series_code)
    );

    CREATE TABLE footnotes (
        footnote_id INT AUTO_INCREMENT PRIMARY KEY,
        country_code VARCHAR(10),
        series_code VARCHAR(50),
        year INT,
        footnote_description TEXT
    );
    """
    
    with engine.begin() as conn:
        for statement in schema_query.split(';'):
            if statement.strip():
                conn.execute(text(statement))
                
    # Insert Data
    print("  -> Inserting data into normalized tables...")
    df_country.to_sql('country_dim', con=engine, if_exists='append', index=False)
    df_series.to_sql('series_dim', con=engine, if_exists='append', index=False)
    df_cp_names.to_sql('counterpart_dim', con=engine, if_exists='append', index=False)
    df_fact.to_sql('debt_fact', con=engine, if_exists='append', index=False)
    
    # Handle footnotes year NaN for SQL
    df_foot['year'] = df_foot['year'].fillna(0).astype(int)
    df_foot.to_sql('footnotes', con=engine, if_exists='append', index=False)
    
    # Create Datamart
    print("  -> Creating international_debt_datamart...")
    datamart_query = """
    CREATE TABLE international_debt_datamart AS
    SELECT 
        c.country_code,
        c.country_name,
        c.region,
        c.income_group,
        s.series_code,
        s.indicator_name,
        s.topic,
        cp.counterpart_name,
        f.year,
        f.debt_value
    FROM debt_fact f
    JOIN country_dim c ON f.country_code = c.country_code
    JOIN series_dim s ON f.series_code = s.series_code
    JOIN counterpart_dim cp ON f.counterpart_code = cp.counterpart_code;
    """
    with engine.begin() as conn:
        conn.execute(text(datamart_query))
        
    return engine

def run_verification(engine):
    """Runs a sample query on the datamart to verify success."""
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
    print("🚀 Starting International Debt Pipeline...\n")
    
    # 1. Process Data
    try:
        df_fact = process_fact_data(FILES['fact'])
        df_country, df_series, df_foot, df_cs = process_metadata()
    except FileNotFoundError as e:
        print(f"❌ Error: Could not find CSV file. {e}")
        sys.exit(1)
        
    print(f"✅ Data processing complete. Fact rows: {len(df_fact)}")
    
    # 2. Database Integration
    print("\n🐬 Connecting to MySQL and loading data...")
    engine = setup_database_and_load(df_fact, df_country, df_series, df_foot)
    print("✅ Database setup and data insertion complete.")
    
    # 3. Verification
    run_verification(engine)
    
    print("\n🎉 Pipeline executed successfully!")