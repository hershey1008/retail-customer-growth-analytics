"""End-to-end cleaning, SQL analysis, customer analytics, and reporting pipeline."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "raw" / "online_retail.xlsx"
COLUMN_MAP = {
    "InvoiceNo": "invoice_no",
    "StockCode": "stock_code",
    "Description": "description",
    "Quantity": "quantity",
    "InvoiceDate": "invoice_date",
    "UnitPrice": "unit_price",
    "CustomerID": "customer_id",
    "Country": "country",
}


def prepare_transactions(raw: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Normalize the source workbook and return deduplicated transaction lines."""
    missing = set(COLUMN_MAP) - set(raw.columns)
    if missing:
        raise ValueError(f"Missing expected columns: {sorted(missing)}")

    audit: dict[str, Any] = {
        "source_rows": int(len(raw)),
        "source_columns": int(len(raw.columns)),
        "exact_duplicate_rows": int(raw.duplicated().sum()),
        "missing_customer_id_rows": int(raw["CustomerID"].isna().sum()),
        "missing_description_rows": int(raw["Description"].isna().sum()),
    }

    frame = raw[list(COLUMN_MAP)].drop_duplicates().rename(columns=COLUMN_MAP).copy()
    frame["invoice_no"] = frame["invoice_no"].astype("string").str.strip()
    frame["stock_code"] = frame["stock_code"].astype("string").str.strip()
    frame["description"] = frame["description"].astype("string").str.strip()
    frame["description"] = frame["description"].replace("", pd.NA)
    frame["quantity"] = pd.to_numeric(frame["quantity"], errors="coerce")
    frame["invoice_date"] = pd.to_datetime(frame["invoice_date"], errors="coerce")
    frame["unit_price"] = pd.to_numeric(frame["unit_price"], errors="coerce")
    customer_number = pd.to_numeric(frame["customer_id"], errors="coerce").astype("Int64")
    frame["customer_id"] = customer_number.astype("string")
    frame["country"] = frame["country"].astype("string").str.strip()
    frame["is_cancelled"] = (
        frame["invoice_no"].str.upper().str.startswith("C", na=False)
    )
    frame["line_revenue"] = frame["quantity"] * frame["unit_price"]

    invalid_typed = frame[["invoice_no", "quantity", "invoice_date", "unit_price"]].isna().any(axis=1)
    if invalid_typed.any():
        raise ValueError(f"{int(invalid_typed.sum())} rows failed required type conversion")

    valid_sales = (
        ~frame["is_cancelled"]
        & frame["quantity"].gt(0)
        & frame["unit_price"].gt(0)
    )
    identified_sales = valid_sales & frame["customer_id"].notna()
    audit.update(
        {
            "deduplicated_rows": int(len(frame)),
            "rows_removed_as_exact_duplicates": int(len(raw) - len(frame)),
            "cancelled_invoice_rows": int(frame["is_cancelled"].sum()),
            "nonpositive_quantity_rows": int(frame["quantity"].le(0).sum()),
            "nonpositive_price_rows": int(frame["unit_price"].le(0).sum()),
            "valid_sales_rows": int(valid_sales.sum()),
            "identified_customer_sales_rows": int(identified_sales.sum()),
            "date_min": frame["invoice_date"].min().isoformat(),
            "date_max": frame["invoice_date"].max().isoformat(),
        }
    )
    return frame, audit


def create_sqlite_layer(frame: pd.DataFrame, database_path: Path, sql_path: Path) -> dict[str, Any]:
    """Load the line table into SQLite and return independently calculated KPIs."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    if database_path.exists():
        database_path.unlink()

    sql_frame = frame.copy()
    sql_frame["invoice_date"] = sql_frame["invoice_date"].dt.strftime("%Y-%m-%d %H:%M:%S")
    for column in ["invoice_no", "stock_code", "description", "customer_id", "country"]:
        sql_frame[column] = sql_frame[column].astype(object).where(sql_frame[column].notna(), None)
    sql_frame["is_cancelled"] = sql_frame["is_cancelled"].astype("int8")

    with sqlite3.connect(database_path) as connection:
        sql_frame.to_sql("transaction_lines", connection, index=False, if_exists="replace", chunksize=20_000)
        connection.executescript(
            "CREATE INDEX idx_invoice ON transaction_lines(invoice_no);"
            "CREATE INDEX idx_customer ON transaction_lines(customer_id);"
            "CREATE INDEX idx_date ON transaction_lines(invoice_date);"
        )
        connection.executescript(sql_path.read_text(encoding="utf-8"))
        row = connection.execute(
            """
            SELECT SUM(line_revenue), COUNT(DISTINCT invoice_no),
                   COUNT(DISTINCT customer_id)
            FROM valid_sales_lines
            """
        ).fetchone()
    return {
        "gross_revenue_gbp": float(row[0]),
        "orders": int(row[1]),
        "identified_customers": int(row[2]),
    }


def score_rfm(
    customer_sales: pd.DataFrame,
    snapshot_date: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Build deterministic quintile RFM scores and business-readable segments."""
    if snapshot_date is None:
        snapshot_date = customer_sales["invoice_date"].max().normalize() + pd.Timedelta(days=1)
    else:
        snapshot_date = pd.Timestamp(snapshot_date)
    rfm = (
        customer_sales.groupby("customer_id")
        .agg(
            last_purchase=("invoice_date", "max"),
            frequency=("invoice_no", "nunique"),
            monetary=("line_revenue", "sum"),
        )
        .reset_index()
    )
    rfm["recency_days"] = (snapshot_date - rfm["last_purchase"].dt.normalize()).dt.days

    def quintile(series: pd.Series, reverse: bool = False) -> pd.Series:
        score = pd.qcut(series.rank(method="first"), 5, labels=False).astype(int) + 1
        return 6 - score if reverse else score

    rfm["r_score"] = quintile(rfm["recency_days"], reverse=True)
    rfm["f_score"] = quintile(rfm["frequency"])
    rfm["m_score"] = quintile(rfm["monetary"])
    r = rfm["r_score"]
    f = rfm["f_score"]
    conditions = [
        (r >= 4) & (f >= 4),
        f >= 4,
        (r >= 4) & (f >= 2),
        (r >= 4) & (f == 1),
        (r <= 2) & (f >= 3),
        (r <= 2) & (f <= 2),
    ]
    labels = [
        "Champions",
        "Loyal Customers",
        "Potential Loyalists",
        "New Customers",
        "At Risk",
        "Hibernating",
    ]
    rfm["segment"] = np.select(conditions, labels, default="Needs Attention")
    return rfm.sort_values(["segment", "monetary"], ascending=[True, False]).reset_index(drop=True)


def build_cohort(customer_sales: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return customer counts and retention rates by acquisition cohort."""
    activity = customer_sales[["customer_id", "invoice_date"]].copy()
    activity["activity_month"] = activity["invoice_date"].dt.to_period("M")
    activity = activity.drop_duplicates(["customer_id", "activity_month"])
    first_month = activity.groupby("customer_id")["activity_month"].min().rename("cohort_month")
    activity = activity.join(first_month, on="customer_id")
    activity["cohort_index"] = (
        (activity["activity_month"].dt.year - activity["cohort_month"].dt.year) * 12
        + activity["activity_month"].dt.month
        - activity["cohort_month"].dt.month
    )
    counts = (
        activity.groupby(["cohort_month", "cohort_index"])["customer_id"]
        .nunique()
        .unstack(fill_value=0)
        .sort_index()
        .astype(float)
    )
    counts = counts.loc[:, [column for column in counts.columns if column <= 12]]
    last_observed_month = activity["activity_month"].max()
    # Keep observed zeroes as 0, but mark future/unobservable cohort cells as
    # missing so the heatmap does not imply 0% retention outside the window.
    for cohort_month in counts.index:
        for month_index in counts.columns:
            if cohort_month + int(month_index) > last_observed_month:
                counts.loc[cohort_month, month_index] = np.nan
    retention = counts.div(counts[0], axis=0)
    counts.index = counts.index.astype(str)
    retention.index = retention.index.astype(str)
    return counts, retention


def weighted_retention(counts: pd.DataFrame, month: int) -> float | None:
    if month not in counts.columns:
        return None
    cohort_months = pd.PeriodIndex(counts.index, freq="M")
    # A cohort is eligible when the observation window has reached the target
    # month, even if that cohort has zero returning customers in that month.
    eligible_mask = cohort_months <= cohort_months.max() - month
    if not eligible_mask.any():
        return None
    return float(counts.loc[eligible_mask, month].sum() / counts.loc[eligible_mask, 0].sum())


def analyze(frame: pd.DataFrame) -> dict[str, Any]:
    """Create analysis tables and the explicit KPI dictionary."""
    valid = frame.loc[
        ~frame["is_cancelled"] & frame["quantity"].gt(0) & frame["unit_price"].gt(0)
    ].copy()
    customer_sales = valid.loc[valid["customer_id"].notna()].copy()

    invoices = (
        valid.groupby("invoice_no")
        .agg(
            invoice_date=("invoice_date", "min"),
            customer_id=("customer_id", "first"),
            country=("country", "first"),
            revenue=("line_revenue", "sum"),
            units=("quantity", "sum"),
        )
        .reset_index()
    )
    customer_orders = customer_sales.groupby("customer_id")["invoice_no"].nunique()
    customer_revenue = customer_sales.groupby("customer_id")["line_revenue"].sum().sort_values(ascending=False)
    top_customer_count = max(1, math.ceil(len(customer_revenue) * 0.10))
    cancellation_value = float(
        -frame.loc[frame["is_cancelled"] & frame["line_revenue"].lt(0), "line_revenue"].sum()
    )

    monthly = (
        valid.assign(month=valid["invoice_date"].dt.to_period("M").astype(str))
        .groupby("month")
        .agg(
            gross_revenue_gbp=("line_revenue", "sum"),
            orders=("invoice_no", "nunique"),
            identified_customers=("customer_id", "nunique"),
            units=("quantity", "sum"),
        )
        .reset_index()
    )
    cancelled_monthly = (
        frame.loc[frame["is_cancelled"]]
        .assign(month=lambda data: data["invoice_date"].dt.to_period("M").astype(str))
        .groupby("month")["invoice_no"]
        .nunique()
        .rename("cancelled_invoices")
    )
    monthly = monthly.join(cancelled_monthly, on="month").fillna({"cancelled_invoices": 0})
    monthly["cancelled_invoices"] = monthly["cancelled_invoices"].astype(int)

    rfm = score_rfm(customer_sales)
    segment_summary = (
        rfm.groupby("segment")
        .agg(
            customers=("customer_id", "nunique"),
            gross_revenue_gbp=("monetary", "sum"),
            median_recency_days=("recency_days", "median"),
            avg_orders=("frequency", "mean"),
        )
        .reset_index()
    )
    segment_summary["customer_share"] = segment_summary["customers"] / segment_summary["customers"].sum()
    segment_summary["revenue_share"] = segment_summary["gross_revenue_gbp"] / segment_summary["gross_revenue_gbp"].sum()
    segment_summary = segment_summary.sort_values("gross_revenue_gbp", ascending=False).reset_index(drop=True)

    cohort_counts, cohort_retention = build_cohort(customer_sales)
    country = (
        valid.groupby("country")
        .agg(
            gross_revenue_gbp=("line_revenue", "sum"),
            orders=("invoice_no", "nunique"),
            identified_customers=("customer_id", "nunique"),
            units=("quantity", "sum"),
        )
        .reset_index()
        .sort_values("gross_revenue_gbp", ascending=False)
    )
    country["avg_order_value_gbp"] = country["gross_revenue_gbp"] / country["orders"]
    product = (
        valid.groupby("stock_code")
        .agg(
            description=("description", lambda values: values.dropna().mode().iat[0] if not values.dropna().empty else "Unknown"),
            units=("quantity", "sum"),
            gross_revenue_gbp=("line_revenue", "sum"),
            orders=("invoice_no", "nunique"),
        )
        .reset_index()
        .sort_values("gross_revenue_gbp", ascending=False)
    )

    metrics = {
        "source_period_start": frame["invoice_date"].min().date().isoformat(),
        "source_period_end": frame["invoice_date"].max().date().isoformat(),
        "last_month_is_partial": True,
        "gross_revenue_gbp": float(valid["line_revenue"].sum()),
        "identified_customer_revenue_gbp": float(customer_sales["line_revenue"].sum()),
        "orders": int(valid["invoice_no"].nunique()),
        "identified_customers": int(customer_sales["customer_id"].nunique()),
        "avg_order_value_gbp": float(invoices["revenue"].mean()),
        "units_sold": int(valid["quantity"].sum()),
        "repeat_customer_rate": float(customer_orders.gt(1).mean()),
        "cancelled_invoices": int(frame.loc[frame["is_cancelled"], "invoice_no"].nunique()),
        "cancellation_value_gbp": cancellation_value,
        "identified_revenue_coverage": float(customer_sales["line_revenue"].sum() / valid["line_revenue"].sum()),
        "top_10pct_customer_revenue_share": float(customer_revenue.head(top_customer_count).sum() / customer_revenue.sum()),
        "non_uk_revenue_share": float(valid.loc[valid["country"].ne("United Kingdom"), "line_revenue"].sum() / valid["line_revenue"].sum()),
        "month_3_weighted_retention": weighted_retention(cohort_counts, 3),
        "month_6_weighted_retention": weighted_retention(cohort_counts, 6),
    }
    return {
        "valid": valid,
        "customer_sales": customer_sales,
        "monthly": monthly,
        "rfm": rfm,
        "segment_summary": segment_summary,
        "cohort_counts": cohort_counts,
        "cohort_retention": cohort_retention,
        "country": country,
        "product": product,
        "metrics": metrics,
        "customer_revenue": customer_revenue,
    }


def save_tables(analysis: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    analysis["monthly"].to_csv(output_dir / "monthly_metrics.csv", index=False)
    analysis["rfm"].to_csv(output_dir / "rfm_customer_scores.csv", index=False)
    analysis["segment_summary"].to_csv(output_dir / "rfm_segments.csv", index=False)
    analysis["cohort_counts"].to_csv(output_dir / "cohort_customer_counts.csv")
    analysis["cohort_retention"].to_csv(output_dir / "cohort_retention.csv")
    analysis["country"].to_csv(output_dir / "country_performance.csv", index=False)
    analysis["product"].to_csv(output_dir / "product_performance.csv", index=False)


def save_charts(analysis: dict[str, Any], figures_dir: Path) -> None:
    figures_dir.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"figure.dpi": 140, "axes.spines.top": False, "axes.spines.right": False})
    colors = {"navy": "#17324d", "blue": "#2f6f8f", "gold": "#d99b2b", "red": "#c75050"}

    monthly = analysis["monthly"]
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), constrained_layout=True)
    axes[0].plot(monthly["month"], monthly["gross_revenue_gbp"], marker="o", color=colors["blue"], linewidth=2)
    axes[0].fill_between(monthly["month"], monthly["gross_revenue_gbp"], alpha=0.12, color=colors["blue"])
    axes[0].set_title("Monthly gross sales revenue (final month is partial)", loc="left", fontweight="bold")
    axes[0].set_ylabel("GBP")
    axes[1].plot(monthly["month"], monthly["orders"], marker="o", color=colors["gold"], linewidth=2, label="Orders")
    axes[1].plot(monthly["month"], monthly["identified_customers"], marker="o", color=colors["navy"], linewidth=2, label="Customers")
    axes[1].set_title("Monthly orders and identified customers", loc="left", fontweight="bold")
    axes[1].legend(frameon=False)
    for axis in axes:
        axis.tick_params(axis="x", rotation=45)
        axis.grid(axis="y", alpha=0.2)
    fig.savefig(figures_dir / "01_monthly_trend.png", bbox_inches="tight")
    plt.close(fig)

    segments = analysis["segment_summary"].sort_values("gross_revenue_gbp")
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.5), constrained_layout=True)
    axes[0].barh(segments["segment"], segments["customers"], color=colors["blue"])
    axes[0].set_title("Customers by RFM segment", loc="left", fontweight="bold")
    axes[0].set_xlabel("Customers")
    axes[1].barh(segments["segment"], segments["gross_revenue_gbp"], color=colors["gold"])
    axes[1].set_title("Revenue by RFM segment", loc="left", fontweight="bold")
    axes[1].set_xlabel("GBP")
    fig.savefig(figures_dir / "02_rfm_segments.png", bbox_inches="tight")
    plt.close(fig)

    retention = analysis["cohort_retention"]
    display_columns = [column for column in retention.columns if column <= 12]
    matrix = retention[display_columns].to_numpy(dtype=float)
    fig, axis = plt.subplots(figsize=(12, 6.5), constrained_layout=True)
    image = axis.imshow(matrix, aspect="auto", cmap="Blues", vmin=0, vmax=1)
    axis.set_title("Monthly customer cohort retention (blank = not yet observable)", loc="left", fontweight="bold")
    axis.set_xlabel("Months since first purchase")
    axis.set_ylabel("Acquisition cohort")
    axis.set_xticks(range(len(display_columns)), labels=display_columns)
    axis.set_yticks(range(len(retention.index)), labels=retention.index)
    for row in range(matrix.shape[0]):
        for column in range(matrix.shape[1]):
            value = matrix[row, column]
            if not np.isnan(value):
                axis.text(column, row, f"{value:.0%}", ha="center", va="center", fontsize=7, color="white" if value > 0.55 else colors["navy"])
    fig.colorbar(image, ax=axis, label="Retention")
    fig.savefig(figures_dir / "03_cohort_retention.png", bbox_inches="tight")
    plt.close(fig)

    countries = analysis["country"].loc[lambda data: data["country"].ne("United Kingdom")].head(10).sort_values("gross_revenue_gbp")
    products = analysis["product"].head(10).sort_values("gross_revenue_gbp")
    fig, axes = plt.subplots(1, 2, figsize=(13, 6), constrained_layout=True)
    axes[0].barh(countries["country"], countries["gross_revenue_gbp"], color=colors["blue"])
    axes[0].set_title("Top non-UK markets", loc="left", fontweight="bold")
    axes[0].set_xlabel("Gross revenue (GBP)")
    labels = products["description"].str.slice(0, 28)
    axes[1].barh(labels, products["gross_revenue_gbp"], color=colors["gold"])
    axes[1].set_title("Top products by revenue", loc="left", fontweight="bold")
    axes[1].set_xlabel("Gross revenue (GBP)")
    fig.savefig(figures_dir / "04_markets_products.png", bbox_inches="tight")
    plt.close(fig)

    revenue = analysis["customer_revenue"].sort_values(ascending=False)
    cumulative = revenue.cumsum() / revenue.sum()
    customer_share = np.arange(1, len(revenue) + 1) / len(revenue)
    fig, axis = plt.subplots(figsize=(9, 5), constrained_layout=True)
    axis.plot(customer_share, cumulative, color=colors["red"], linewidth=2)
    axis.plot([0, 1], [0, 1], linestyle="--", color="#8b98a5", linewidth=1)
    axis.axvline(0.10, color=colors["gold"], linestyle=":")
    axis.set(title="Revenue concentration among identified customers", xlabel="Top customer share", ylabel="Cumulative revenue share")
    axis.set_xlim(0, 1)
    axis.set_ylim(0, 1)
    axis.grid(alpha=0.2)
    fig.savefig(figures_dir / "05_revenue_concentration.png", bbox_inches="tight")
    plt.close(fig)


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if pd.isna(value) else float(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def build_dashboard(analysis: dict[str, Any], output_path: Path) -> None:
    """Write a standalone interactive HTML dashboard with no external assets."""
    retention = analysis["cohort_retention"].copy()
    payload = {
        "metrics": analysis["metrics"],
        "monthly": analysis["monthly"].to_dict(orient="records"),
        "segments": analysis["segment_summary"].to_dict(orient="records"),
        "countries": analysis["country"].loc[lambda data: data["country"].ne("United Kingdom")].head(12).to_dict(orient="records"),
        "products": analysis["product"].head(12).to_dict(orient="records"),
        "experiments": analysis["experiments"]["summary"].to_dict(orient="records"),
        "lifecycleBaselines": analysis["experiments"]["lifecycle_baselines"],
        "cohort": {
            "rows": retention.index.tolist(),
            "columns": [int(column) for column in retention.columns],
            "values": retention.to_numpy().tolist(),
        },
    }
    data_json = json.dumps(json_safe(payload), ensure_ascii=False)
    template = (ROOT / "dashboard" / "template.html").read_text(encoding="utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(template.replace("__DASHBOARD_DATA__", data_json), encoding="utf-8")


def build_report(analysis: dict[str, Any], audit: dict[str, Any], output_path: Path) -> None:
    metrics = analysis["metrics"]
    segments = analysis["segment_summary"].set_index("segment")
    champions = segments.loc["Champions"] if "Champions" in segments.index else None
    at_risk = segments.loc["At Risk"] if "At Risk" in segments.index else None
    top_country = analysis["country"].loc[lambda data: data["country"].ne("United Kingdom")].iloc[0]
    top_product = analysis["product"].iloc[0]
    complete_months = analysis["monthly"].iloc[:-1]
    peak_month = complete_months.loc[complete_months["gross_revenue_gbp"].idxmax()]
    report = f"""# UCI Online Retail 客户价值与留存分析报告

## 1. 业务背景与问题

本项目把一家英国无实体店礼品零售商的真实交易流水，转化为可回答运营问题的分析资产。目标不是预测未来，而是回答：收入从哪里来、哪些客户值得差异化运营、客户是否持续复购、取消交易如何影响口径，以及海外市场和商品组合有哪些机会。

## 2. 数据与口径

- 数据源：UCI Online Retail，原始 {audit['source_rows']:,} 行，时间范围 {metrics['source_period_start']} 至 {metrics['source_period_end']}。
- 清洗：删除 {audit['exact_duplicate_rows']:,} 条完全重复行；销售口径排除取消单、非正数量和非正单价。
- 收入口径：`quantity × unit_price` 的销售毛额（GBP），不是利润，也不含成本、税费和运费。
- 客户分析：仅使用有 `CustomerID` 的有效销售；这些交易覆盖销售毛额的 {metrics['identified_revenue_coverage']:.1%}。
- 取消金额：按取消行负金额的绝对值估算，仅用于规模监控；未逐笔匹配原始订单，不能当作已确认退款额。
- 2011-12 只有前 9 天，月度比较时视为不完整月份，不与完整月份直接比较。

## 3. 核心结果

| 指标 | 结果 |
|---|---:|
| 有效销售毛额 | £{metrics['gross_revenue_gbp']:,.2f} |
| 有效订单 | {metrics['orders']:,} |
| 可识别客户 | {metrics['identified_customers']:,} |
| 平均订单金额 | £{metrics['avg_order_value_gbp']:,.2f} |
| 复购客户占比 | {metrics['repeat_customer_rate']:.1%} |
| 取消单数量 | {metrics['cancelled_invoices']:,} |
| 取消行金额绝对值 | £{metrics['cancellation_value_gbp']:,.2f} |
| Top 10% 客户收入占比 | {metrics['top_10pct_customer_revenue_share']:.1%} |
| 非英国收入占比 | {metrics['non_uk_revenue_share']:.1%} |
| 加权第 3 月留存 | {metrics['month_3_weighted_retention']:.1%} |
| 加权第 6 月留存 | {metrics['month_6_weighted_retention']:.1%} |

## 4. 图表解读

### 4.1 月度收入、订单与客户趋势

![图 1：月度销售趋势](figures/01_monthly_trend.png)

- 在完整月份中，{peak_month['month']} 的销售毛额最高，为 £{peak_month['gross_revenue_gbp']:,.2f}，对应 {int(peak_month['orders']):,} 个订单和 {int(peak_month['identified_customers']):,} 位可识别客户。9—11 月收入、订单和客户数同步走高，说明旺季增长不只是少数大额订单推动。
- 这张图只能证明时间上的共同变化。数据没有促销、流量和节假日活动字段，因此不能把增长归因于某次营销活动或单一季节因素。
- 2011-12 仅覆盖前 9 天，图中末点明显回落属于观察窗口不完整，不能与完整月份直接比较。

### 4.2 RFM 客户分层

![图 2：RFM 客户数与收入](figures/02_rfm_segments.png)

- Champions 占可识别客户的 {champions['customer_share'] if champions is not None else 0:.1%}，却贡献 {champions['revenue_share'] if champions is not None else 0:.1%} 的客户收入，是保护留存和服务体验的第一优先级。
- At Risk 与 Hibernating 客户池较大，但历史价值和近期活跃度不同，应将 At Risk 放入分批召回试验，而不是对所有沉睡客户统一发券。
- RFM 是基于最近购买、购买频次和累计消费的规则分层，不是响应概率模型；分群差异不能直接解释为某种运营动作的效果。

### 4.3 Cohort 月度留存

![图 3：客户 Cohort 月度留存](figures/03_cohort_retention.png)

- 每行代表一个首购月份，M0 固定为 100%；空白格表示该 Cohort 尚未走到相应观察月，并非留存率为 0。
- 加权第 3 月和第 6 月留存分别为 {metrics['month_3_weighted_retention']:.1%}、{metrics['month_6_weighted_retention']:.1%}。两个汇总值纳入的可观察 Cohort 不完全相同，不能据其大小直接判断留存自然回升或下降。
- 图中没有营销曝光或随机分组信息，只能作为历史基线和异常监控依据，不能当作触达带来的因果提升。

### 4.4 海外市场与头部行项目

![图 4：海外市场与头部行项目](figures/04_markets_products.png)

- {top_country['country']} 以 £{top_country['gross_revenue_gbp']:,.0f} 位居非英国市场首位，但海外市场是否值得扩张仍需结合运费、税费、交付时长和本地履约成本。
- 收入最高行项目为 `{top_product['description']}`（£{top_product['gross_revenue_gbp']:,.0f}），它属于邮费编码而非实体商品。商品榜在用于选品或备货前，必须先拆分邮费、手工调整等服务型编码。
- 横轴是销售毛额而非利润；图中排名不能直接代表市场盈利能力或商品毛利贡献。

### 4.5 客户收入集中度

![图 5：可识别客户收入集中度](figures/05_revenue_concentration.png)

- 曲线明显高于均等分布虚线，收入最高的 10% 可识别客户贡献 {metrics['top_10pct_customer_revenue_share']:.1%} 的客户销售额，说明高价值客户保护会影响较大的收入盘子。
- 高集中度同时意味着客户流失风险集中，应跟踪头部客户留存并设置服务护栏；但集中度本身不能证明提供权益后一定会提高收入。

## 5. 综合发现

1. **客户价值高度集中。** 收入最高的 10% 可识别客户贡献 {metrics['top_10pct_customer_revenue_share']:.1%} 的客户销售额，运营应优先保护高价值客户，同时监测过度依赖风险。
2. **RFM 分层可以直接转成运营队列。** Champions 有 {int(champions['customers']) if champions is not None else 0:,} 人，贡献 {champions['revenue_share'] if champions is not None else 0:.1%} 的客户收入；At Risk 有 {int(at_risk['customers']) if at_risk is not None else 0:,} 人，适合设置召回优先级。
3. **留存需要按 Cohort 持续追踪。** 加权第 3 月和第 6 月留存分别为 {metrics['month_3_weighted_retention']:.1%}、{metrics['month_6_weighted_retention']:.1%}；两者纳入的可观察 Cohort 不完全相同，不能把大小差异解释成自然回升或下降，也不能直接归因于营销动作。
4. **海外收入仍是补充市场。** 非英国市场贡献 {metrics['non_uk_revenue_share']:.1%} 的有效销售额；其中 {top_country['country']} 以 £{top_country['gross_revenue_gbp']:,.0f} 位居海外市场首位。
5. **头部行项目需要先做商品治理。** 收入最高行项目为 `{top_product['description']}`（£{top_product['gross_revenue_gbp']:,.0f}），但它是邮费编码而非实体商品；分析利润和备货前必须分离邮费、手工调整等服务型编码，并补充成本和库存数据。

## 6. 后续实验层

项目已把建议扩展为四个可复现实验设计：Champions 保护、At Risk 分批召回、首购后 30/60/90 天运营和海外头部市场验证。使用 {analysis['experiments']['snapshot_date']} 时间切分估计自然基线、样本量与可检测提升，并生成哈希化模拟分流预览；**这些不是已执行实验或因果结果**。完整方案见 [`reports/experiment_plan.md`](experiment_plan.md)。

## 7. 建议与下一步

- 为 Champions 建立服务保障和新品优先触达，对 At Risk 采用分批召回；以增量实验而不是活动后自然波动评估效果。
- 建立首购后 30/60/90 天自动化触达与 Cohort 看板，按获客月份追踪留存，避免只看累计复购率。
- 将取消单按商品、客户和月份建立原因码；当前数据没有取消原因，无法区分缺货、客户反悔、录入纠错等机制。
- 针对海外头部市场补充履约成本、税费、交付时长后再评估扩张；本项目只有销售额，没有利润字段。
- 后续如取得营销触达、流量、成本和库存数据，可增加漏斗、CAC/LTV、毛利与库存周转分析。

## 8. 证据与可复现材料

- SQL 定义：[`sql/analysis.sql`](../sql/analysis.sql)
- 交互式 Dashboard：[`dashboard/index.html`](../dashboard/index.html)
- 数据质量审计：[`outputs/data_quality.json`](../outputs/data_quality.json)
- 核心指标：[`outputs/kpis.json`](../outputs/kpis.json)
- Cohort、RFM、市场和商品明细：[`outputs/`](../outputs/)
- 实验可行性与分流预览：[`outputs/experiment_feasibility.csv`](../outputs/experiment_feasibility.csv)、[`outputs/experiment_assignment_preview.csv`](../outputs/experiment_assignment_preview.csv)
- 实验注册表：[`experiments/experiment_registry.json`](../experiments/experiment_registry.json)

## 9. 限制

这是公开历史交易数据的描述性分析。数据没有流量、营销触达、商品成本、库存、运费和取消原因，无法计算转化率、获客成本、真实利润，也不能把相关性写成因果结论。缺失客户编号的交易不进入 RFM/Cohort，客户相关结论只代表可识别客户。
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")


def run(input_path: Path = DEFAULT_INPUT) -> dict[str, Any]:
    if not input_path.exists():
        raise FileNotFoundError(f"Dataset not found: {input_path}. Run: python src/download_data.py")
    print(f"Reading {input_path}")
    raw = pd.read_excel(input_path, engine="openpyxl")
    frame, audit = prepare_transactions(raw)
    analysis = analyze(frame)
    from src.experiments import design_experiments

    analysis["experiments"] = design_experiments(analysis)

    sql_metrics = create_sqlite_layer(frame, ROOT / "build" / "online_retail.sqlite", ROOT / "sql" / "analysis.sql")
    metrics = analysis["metrics"]
    checks = {
        "expected_source_row_count": audit["source_rows"] == 541_909,
        "date_range_valid": metrics["source_period_start"] == "2010-12-01" and metrics["source_period_end"] == "2011-12-09",
        "sql_revenue_reconciled": math.isclose(sql_metrics["gross_revenue_gbp"], metrics["gross_revenue_gbp"], rel_tol=0, abs_tol=0.01),
        "sql_orders_reconciled": sql_metrics["orders"] == metrics["orders"],
        "sql_customers_reconciled": sql_metrics["identified_customers"] == metrics["identified_customers"],
        "all_customers_segmented": int(analysis["segment_summary"]["customers"].sum()) == metrics["identified_customers"],
        "cohort_month_zero_is_one": bool(np.allclose(analysis["cohort_retention"][0].dropna(), 1.0)),
        "positive_sales_metrics": metrics["gross_revenue_gbp"] > 0 and metrics["orders"] > 0,
        "four_experiments_registered": len(analysis["experiments"]["registry"]) == 4,
        "experiment_assignments_pseudonymized": bool(
            analysis["experiments"]["assignments"]["subject_id"]
            .astype(str)
            .str.fullmatch(r"[0-9a-f]{12}")
            .all()
        ),
        "experiment_power_inputs_valid": bool(
            analysis["experiments"]["summary"]["required_total_sample"].gt(0).all()
            and analysis["experiments"]["summary"]["historical_baseline_rate"]
            .between(0, 1, inclusive="neither")
            .all()
        ),
        "experiment_layers_mutually_exclusive": bool(
            ~analysis["experiments"]["assignments"].duplicated(
                ["experiment_layer", "subject_id"]
            ).any()
        ),
    }
    audit["validation_checks"] = checks
    audit["all_checks_passed"] = all(checks.values())
    if not audit["all_checks_passed"]:
        raise AssertionError(f"Validation failed: {checks}")

    output_dir = ROOT / "outputs"
    save_tables(analysis, output_dir)
    (output_dir / "kpis.json").write_text(json.dumps(json_safe(metrics), indent=2, ensure_ascii=False), encoding="utf-8")
    (output_dir / "data_quality.json").write_text(json.dumps(json_safe(audit), indent=2, ensure_ascii=False), encoding="utf-8")
    save_charts(analysis, ROOT / "reports" / "figures")
    from src.experiments import (
        build_experiment_report,
        save_experiment_charts,
        save_experiment_outputs,
    )

    save_experiment_outputs(analysis["experiments"], ROOT)
    save_experiment_charts(analysis["experiments"], ROOT / "reports" / "figures")
    build_experiment_report(
        analysis["experiments"], ROOT / "reports" / "experiment_plan.md"
    )
    build_dashboard(analysis, ROOT / "dashboard" / "index.html")
    build_report(analysis, audit, ROOT / "reports" / "analysis_report.md")
    print(json.dumps(json_safe(metrics), indent=2, ensure_ascii=False))
    print("All validation checks passed.")
    return {"metrics": metrics, "audit": audit}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    args = parser.parse_args()
    run(args.input)


if __name__ == "__main__":
    main()
