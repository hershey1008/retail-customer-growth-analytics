import pandas as pd

from src.pipeline import build_cohort, prepare_transactions, score_rfm, weighted_retention


def sample_raw() -> pd.DataFrame:
    rows = [
        ["100001", "A", "Alpha", 2, "2011-01-01 10:00", 10.0, 1, "United Kingdom"],
        ["100002", "B", "Beta", 1, "2011-02-01 10:00", 20.0, 1, "United Kingdom"],
        ["100003", "A", "Alpha", 3, "2011-03-01 10:00", 10.0, 2, "France"],
        ["C10003", "A", "Alpha", -1, "2011-03-02 10:00", 10.0, 2, "France"],
        ["100004", "C", "Gamma", 1, "2011-04-01 10:00", 5.0, None, "France"],
    ]
    rows.append(rows[0].copy())
    return pd.DataFrame(
        rows,
        columns=["InvoiceNo", "StockCode", "Description", "Quantity", "InvoiceDate", "UnitPrice", "CustomerID", "Country"],
    )


def test_prepare_transactions_tracks_duplicates_and_cancellations():
    frame, audit = prepare_transactions(sample_raw())
    assert len(frame) == 5
    assert audit["exact_duplicate_rows"] == 1
    assert audit["cancelled_invoice_rows"] == 1
    assert audit["valid_sales_rows"] == 4
    assert audit["identified_customer_sales_rows"] == 3


def test_rfm_assigns_every_customer():
    frame, _ = prepare_transactions(sample_raw())
    sales = frame.loc[
        ~frame["is_cancelled"]
        & frame["quantity"].gt(0)
        & frame["unit_price"].gt(0)
        & frame["customer_id"].notna()
    ]
    rfm = score_rfm(sales)
    assert set(rfm["customer_id"]) == {"1", "2"}
    assert rfm["segment"].notna().all()
    assert rfm[["r_score", "f_score", "m_score"]].min().min() >= 1
    assert rfm[["r_score", "f_score", "m_score"]].max().max() <= 5


def test_cohort_month_zero_is_full_retention():
    frame, _ = prepare_transactions(sample_raw())
    sales = frame.loc[
        ~frame["is_cancelled"]
        & frame["quantity"].gt(0)
        & frame["unit_price"].gt(0)
        & frame["customer_id"].notna()
    ]
    _, retention = build_cohort(sales)
    assert (retention[0] == 1.0).all()


def test_weighted_retention_includes_zero_return_cohorts():
    counts = pd.DataFrame(
        {0: [100, 100, 100, 100], 1: [20, 0, 0, 0], 2: [10, 0, 0, 0]},
        index=["2011-01", "2011-02", "2011-03", "2011-04"],
    )
    assert weighted_retention(counts, 2) == 0.05
