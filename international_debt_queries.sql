-- =====================================================================

USE international_debt_db;

-- Total external debt per country per year (2000-2024)
CREATE OR REPLACE VIEW v_total_debt AS
SELECT country_code,
       short_name   AS country,
       region,
       income_group,
       year,
       debt_value   AS debt_usd
FROM international_debt_datamart
WHERE series_code = 'DT.DOD.DECT.CD'
  AND is_aggregate = 0;

-- Total external debt split into its 8 non-overlapping components
CREATE OR REPLACE VIEW v_debt_components AS
SELECT country_code,
       short_name     AS country,
       region,
       income_group,
       series_code,
       indicator_name AS indicator,
       year,
       debt_value     AS debt_usd
FROM international_debt_datamart
WHERE series_code IN ('DT.DOD.MLAT.CD', 'DT.DOD.BLAT.CD', 'DT.DOD.PBND.CD', 'DT.DOD.PCBK.CD',
                      'DT.DOD.PROP.CD', 'DT.DOD.DPNG.CD', 'DT.DOD.DSTC.CD', 'DT.DOD.DIMF.CD')
  AND is_aggregate = 0;


-- Q1. Retrieve all distinct country names from the dataset.
SELECT DISTINCT c.short_name AS country, c.country_name AS official_name
FROM debt_fact f
JOIN country_dim c ON f.country_code = c.country_code
WHERE c.is_aggregate = 0
ORDER BY country;

-- Q2. Count the total number of countries available.
SELECT COUNT(*)                AS total_entities,
       SUM(is_aggregate = 0)   AS countries,
       SUM(is_aggregate = 1)   AS regional_income_aggregates
FROM country_dim;

-- Q3. Find the total number of indicators present.
SELECT (SELECT COUNT(*) FROM series_dim)                   AS indicators_defined,
       (SELECT COUNT(DISTINCT series_code) FROM debt_fact) AS indicators_with_data;

-- Q4. Display the first 10 records of the dataset.
SELECT country_name, indicator_name, year, debt_value, unit
FROM international_debt_datamart
ORDER BY country_code, series_code, year
LIMIT 10;

-- Q5. Calculate the total global debt (2024).
SELECT year,
       SUM(debt_usd)                     AS total_global_debt_usd,
       ROUND(SUM(debt_usd) / 1e12, 2)    AS total_global_debt_trillion
FROM v_total_debt
WHERE year = 2024
GROUP BY year;

-- Q6. List all unique indicator names.
SELECT DISTINCT s.indicator_name, s.indicator_category, s.unit
FROM debt_fact f
JOIN series_dim s ON f.series_code = s.series_code
ORDER BY s.indicator_category, s.indicator_name;

-- Q7. Find the number of records for each country.
SELECT c.short_name AS country, COUNT(*) AS records
FROM debt_fact f
JOIN country_dim c ON f.country_code = c.country_code
WHERE c.is_aggregate = 0
GROUP BY c.short_name
ORDER BY records DESC;

-- Q8. Display all records where debt is greater than 1 billion USD.
SELECT country, region, year, debt_usd
FROM v_total_debt
WHERE debt_usd > 1e9
ORDER BY year DESC, debt_usd DESC;

-- Q9. Find the minimum, maximum, and average debt values (2024, across countries).
SELECT MIN(debt_usd)            AS min_debt_usd,
       MAX(debt_usd)            AS max_debt_usd,
       ROUND(AVG(debt_usd), 2)  AS avg_debt_usd
FROM v_total_debt
WHERE year = 2024;

-- Q10. Count total number of records in the dataset.
SELECT COUNT(*) AS total_records FROM debt_fact;

-- Q11. Find the total debt for each country (2024).
SELECT country, region, income_group, debt_usd AS total_debt_usd
FROM v_total_debt
WHERE year = 2024
ORDER BY total_debt_usd DESC;

-- Q12. Display the top 10 countries with the highest total debt (2024).
SELECT country, region, ROUND(debt_usd / 1e9, 2) AS total_debt_billion
FROM v_total_debt
WHERE year = 2024
ORDER BY debt_usd DESC
LIMIT 10;

-- Q13. Find the average debt per country (average yearly debt, 2000-2024).
SELECT country,
       ROUND(AVG(debt_usd) / 1e9, 2) AS avg_debt_billion,
       COUNT(*)                      AS years_of_data
FROM v_total_debt
GROUP BY country
ORDER BY avg_debt_billion DESC;

-- Q14. Calculate total debt for each indicator (2024).
SELECT indicator, ROUND(SUM(debt_usd) / 1e9, 2) AS total_debt_billion
FROM v_debt_components
WHERE year = 2024
GROUP BY indicator
ORDER BY total_debt_billion DESC;

-- Q15. Identify the indicator contributing the highest total debt (2024).
SELECT indicator, ROUND(SUM(debt_usd) / 1e9, 2) AS total_debt_billion
FROM v_debt_components
WHERE year = 2024
GROUP BY indicator
ORDER BY total_debt_billion DESC
LIMIT 1;

-- Q16. Find the country with the lowest total debt (2024).
SELECT country, region, debt_usd AS total_debt_usd
FROM v_total_debt
WHERE year = 2024
ORDER BY debt_usd ASC
LIMIT 1;

-- Q17. Calculate total debt for each country and indicator combination (2024).
SELECT country, indicator, debt_usd
FROM v_debt_components
WHERE year = 2024
ORDER BY country, debt_usd DESC;

-- Q18. Count how many indicators each country has (indicators with data).
SELECT c.short_name AS country, COUNT(DISTINCT f.series_code) AS indicator_count
FROM debt_fact f
JOIN country_dim c ON f.country_code = c.country_code
WHERE c.is_aggregate = 0
GROUP BY c.short_name
ORDER BY indicator_count DESC;

-- Q19. Display countries whose total debt is above the global average (2024).
WITH country_debt AS (
    SELECT country, debt_usd FROM v_total_debt WHERE year = 2024
)
SELECT country,
       ROUND(debt_usd / 1e9, 2)                                     AS total_debt_billion,
       ROUND((SELECT AVG(debt_usd) FROM country_debt) / 1e9, 2)     AS global_avg_billion
FROM country_debt
WHERE debt_usd > (SELECT AVG(debt_usd) FROM country_debt)
ORDER BY debt_usd DESC;

-- Q20. Rank countries based on total debt, highest to lowest (2024).
SELECT country,
       ROUND(debt_usd / 1e9, 2)                    AS total_debt_billion,
       RANK()       OVER (ORDER BY debt_usd DESC)  AS debt_rank,
       DENSE_RANK() OVER (ORDER BY debt_usd DESC)  AS dense_debt_rank
FROM v_total_debt
WHERE year = 2024
ORDER BY debt_rank;

-- Q21. Find the top 5 indicators contributing most to global debt (2024).
SELECT indicator,
       ROUND(SUM(debt_usd) / 1e9, 2)                                AS total_debt_billion,
       ROUND(100 * SUM(debt_usd) / SUM(SUM(debt_usd)) OVER (), 2)   AS pct_of_global_debt
FROM v_debt_components
WHERE year = 2024
GROUP BY indicator
ORDER BY total_debt_billion DESC
LIMIT 5;

-- Q22. Calculate percentage contribution of each country to total global debt (2024).
SELECT country,
       ROUND(debt_usd / 1e9, 2)                           AS total_debt_billion,
       ROUND(100 * debt_usd / SUM(debt_usd) OVER (), 2)   AS pct_of_global_debt
FROM v_total_debt
WHERE year = 2024
ORDER BY pct_of_global_debt DESC;

-- Q23. Identify the top 3 countries for each indicator based on debt (2024).
SELECT indicator, debt_rank, country, total_debt_billion
FROM (
    SELECT indicator,
           country,
           ROUND(debt_usd / 1e9, 2)                                          AS total_debt_billion,
           ROW_NUMBER() OVER (PARTITION BY indicator ORDER BY debt_usd DESC) AS debt_rank
    FROM v_debt_components
    WHERE year = 2024
) ranked
WHERE debt_rank <= 3
ORDER BY indicator, debt_rank;

-- Q24. Find the difference between maximum and minimum debt for each country (2000-2024).
SELECT country,
       ROUND(MIN(debt_usd) / 1e9, 2)                    AS min_debt_billion,
       ROUND(MAX(debt_usd) / 1e9, 2)                    AS max_debt_billion,
       ROUND((MAX(debt_usd) - MIN(debt_usd)) / 1e9, 2)  AS debt_range_billion
FROM v_total_debt
GROUP BY country
ORDER BY debt_range_billion DESC;

-- Q25. Create a view for the top 10 countries with highest debt (2024).
CREATE OR REPLACE VIEW v_top10_debt_countries AS
SELECT country, region, income_group, debt_usd AS total_debt_usd
FROM v_total_debt
WHERE year = 2024
ORDER BY debt_usd DESC
LIMIT 10;

SELECT * FROM v_top10_debt_countries;

-- Q26. Categorize countries into High / Medium / Low debt (2024).
--      High >= 100 bn US$, Medium 10-100 bn US$, Low < 10 bn US$.
SELECT country,
       ROUND(debt_usd / 1e9, 2) AS total_debt_billion,
       CASE
           WHEN debt_usd >= 1e11 THEN 'High Debt'
           WHEN debt_usd >= 1e10 THEN 'Medium Debt'
           ELSE 'Low Debt'
       END AS debt_category
FROM v_total_debt
WHERE year = 2024
ORDER BY debt_usd DESC;

-- Q27. Use window functions to calculate cumulative debt per country (running total by year).
SELECT country,
       year,
       ROUND(debt_usd / 1e9, 2) AS debt_billion,
       ROUND(SUM(debt_usd) OVER (PARTITION BY country ORDER BY year
                                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) / 1e9, 2) AS cumulative_debt_billion
FROM v_total_debt
ORDER BY country, year;

-- Q28. Find indicators where average debt is higher than overall average debt (2024).
SELECT indicator, ROUND(AVG(debt_usd) / 1e9, 2) AS avg_debt_billion
FROM v_debt_components
WHERE year = 2024
GROUP BY indicator
HAVING AVG(debt_usd) > (SELECT AVG(debt_usd) FROM v_debt_components WHERE year = 2024)
ORDER BY avg_debt_billion DESC;

-- Q29. Identify countries contributing more than 5% of global debt (2024).
SELECT country, total_debt_billion, pct_of_global_debt
FROM (
    SELECT country,
           ROUND(debt_usd / 1e9, 2)                           AS total_debt_billion,
           ROUND(100 * debt_usd / SUM(debt_usd) OVER (), 2)   AS pct_of_global_debt
    FROM v_total_debt
    WHERE year = 2024
) shares
WHERE pct_of_global_debt > 5
ORDER BY pct_of_global_debt DESC;

-- Q30. Find the most dominant indicator (highest contribution) for each country (2024).
SELECT country, indicator AS dominant_indicator, debt_billion, pct_of_country_debt
FROM (
    SELECT country,
           indicator,
           ROUND(debt_usd / 1e9, 2)                                               AS debt_billion,
           ROUND(100 * debt_usd / SUM(debt_usd) OVER (PARTITION BY country), 2)   AS pct_of_country_debt,
           ROW_NUMBER() OVER (PARTITION BY country ORDER BY debt_usd DESC)        AS rn
    FROM v_debt_components
    WHERE year = 2024
) ranked
WHERE rn = 1
ORDER BY debt_billion DESC;
