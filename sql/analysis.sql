-- SQLite analytical layer for the UCI Online Retail project.
-- The pipeline loads a deduplicated `transaction_lines` table, runs this file,
-- and reconciles the SQL KPIs against the Pandas calculation.

DROP VIEW IF EXISTS valid_sales_lines;
CREATE VIEW valid_sales_lines AS
SELECT
    invoice_no,
    stock_code,
    description,
    quantity,
    invoice_date,
    unit_price,
    customer_id,
    country,
    line_revenue
FROM transaction_lines
WHERE is_cancelled = 0
  AND quantity > 0
  AND unit_price > 0;

DROP VIEW IF EXISTS customer_sales_lines;
CREATE VIEW customer_sales_lines AS
SELECT *
FROM valid_sales_lines
WHERE customer_id IS NOT NULL AND customer_id <> '';

-- Executive KPIs
SELECT
    ROUND(SUM(line_revenue), 2) AS gross_revenue_gbp,
    COUNT(DISTINCT invoice_no) AS orders,
    COUNT(DISTINCT customer_id) AS identified_customers,
    ROUND(SUM(line_revenue) / COUNT(DISTINCT invoice_no), 2) AS avg_order_value_gbp
FROM valid_sales_lines;

-- Monthly trend (the final month is a partial month in the source data)
SELECT
    SUBSTR(invoice_date, 1, 7) AS month,
    ROUND(SUM(line_revenue), 2) AS gross_revenue_gbp,
    COUNT(DISTINCT invoice_no) AS orders,
    COUNT(DISTINCT customer_id) AS identified_customers
FROM valid_sales_lines
GROUP BY month
ORDER BY month;

-- Country performance excluding the dominant home market
SELECT
    country,
    ROUND(SUM(line_revenue), 2) AS gross_revenue_gbp,
    COUNT(DISTINCT invoice_no) AS orders,
    COUNT(DISTINCT customer_id) AS identified_customers
FROM valid_sales_lines
WHERE country <> 'United Kingdom'
GROUP BY country
ORDER BY gross_revenue_gbp DESC;

-- Product performance
SELECT
    stock_code,
    MAX(description) AS description,
    SUM(quantity) AS units,
    ROUND(SUM(line_revenue), 2) AS gross_revenue_gbp,
    COUNT(DISTINCT invoice_no) AS orders
FROM valid_sales_lines
GROUP BY stock_code
ORDER BY gross_revenue_gbp DESC;
