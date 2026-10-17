"""Historical experiment feasibility, power planning, and assignment previews.

Nothing in this module claims a causal result. Historical outcomes are used only
to estimate baselines and determine whether a future randomized test is sized
well enough to answer its primary question.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from statistics import NormalDist
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


SNAPSHOT_DATE = pd.Timestamp("2011-09-10")
ALPHA = 0.05
POWER = 0.80
TARGET_EFFECT = 0.05


def required_binary_sample_per_arm(
    baseline_rate: float,
    absolute_lift: float = TARGET_EFFECT,
    alpha: float = ALPHA,
    power: float = POWER,
) -> int:
    """Approximate equal-arm sample size for a two-sided difference in rates."""
    if not 0 < baseline_rate < 1:
        raise ValueError("baseline_rate must be between 0 and 1")
    treatment_rate = baseline_rate + absolute_lift
    if not 0 < treatment_rate < 1:
        raise ValueError("baseline_rate + absolute_lift must be between 0 and 1")
    z_alpha = NormalDist().inv_cdf(1 - alpha / 2)
    z_power = NormalDist().inv_cdf(power)
    pooled = (baseline_rate + treatment_rate) / 2
    numerator = (
        z_alpha * math.sqrt(2 * pooled * (1 - pooled))
        + z_power
        * math.sqrt(
            baseline_rate * (1 - baseline_rate)
            + treatment_rate * (1 - treatment_rate)
        )
    ) ** 2
    return math.ceil(numerator / absolute_lift**2)


def achievable_binary_mde(
    baseline_rate: float,
    total_sample: int,
    alpha: float = ALPHA,
    power: float = POWER,
) -> float | None:
    """Return the smallest detectable absolute lift for the available pool."""
    if total_sample < 20 or not 0 < baseline_rate < 1:
        return None
    per_arm = total_sample // 2
    lower = 0.001
    upper = min(0.50, 0.999 - baseline_rate)
    if required_binary_sample_per_arm(baseline_rate, upper, alpha, power) > per_arm:
        return None
    for _ in range(50):
        middle = (lower + upper) / 2
        required = required_binary_sample_per_arm(baseline_rate, middle, alpha, power)
        if required <= per_arm:
            upper = middle
        else:
            lower = middle
    return upper


def pseudonymize_customer(customer_id: str) -> str:
    """Return a stable project-local pseudonym instead of exporting raw IDs."""
    payload = f"online-retail-experiment:{customer_id}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def stratified_assignment(
    subjects: pd.DataFrame,
    experiment_id: str,
    strata: list[str],
    waves: int = 1,
) -> pd.DataFrame:
    """Create a balanced, deterministic assignment preview within each stratum."""
    required = {"customer_id", *strata}
    missing = required - set(subjects.columns)
    if missing:
        raise ValueError(f"Assignment data is missing columns: {sorted(missing)}")
    frame = subjects[list(required)].drop_duplicates("customer_id").copy()
    frame["_hash"] = frame["customer_id"].map(
        lambda value: int(
            hashlib.sha256(f"{experiment_id}:{value}".encode("utf-8")).hexdigest()[:16],
            16,
        )
    )
    frame = frame.sort_values([*strata, "_hash", "customer_id"]).reset_index(drop=True)
    frame["_position"] = frame.groupby(strata, dropna=False).cumcount()
    frame["arm"] = np.where(frame["_position"].mod(2).eq(0), "control", "treatment")
    frame["wave"] = "holdout"
    treatment = frame["arm"].eq("treatment")
    frame.loc[treatment, "wave"] = frame.loc[treatment, "_hash"].map(
        lambda value: f"wave_{value % max(1, waves) + 1}"
    )
    frame["experiment_id"] = experiment_id
    frame["subject_id"] = frame["customer_id"].map(pseudonymize_customer)
    frame["stratum"] = frame[strata].astype(str).agg(" | ".join, axis=1)
    frame["simulation_only"] = True
    return frame[
        ["experiment_id", "subject_id", "stratum", "arm", "wave", "simulation_only"]
    ].sort_values(["stratum", "arm", "subject_id"])


def _future_customer_flags(
    customer_sales: pd.DataFrame,
    snapshot_date: pd.Timestamp,
) -> pd.DataFrame:
    post = customer_sales.loc[
        customer_sales["invoice_date"].ge(snapshot_date)
        & customer_sales["invoice_date"].lt(snapshot_date + pd.Timedelta(days=91))
    ].copy()
    post["within_30_days"] = post["invoice_date"].lt(
        snapshot_date + pd.Timedelta(days=30)
    )
    return (
        post.groupby("customer_id")
        .agg(
            reactivated_30=("within_30_days", "max"),
            retained_90=("invoice_no", "nunique"),
            revenue_90=("line_revenue", "sum"),
        )
        .assign(retained_90=lambda data: data["retained_90"].gt(0))
    )


def _lifecycle_baselines(customer_sales: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, float]]:
    invoice_dates = (
        customer_sales.groupby(["customer_id", "invoice_no"])
        .agg(invoice_date=("invoice_date", "min"), country=("country", "first"))
        .reset_index()
        .sort_values(["customer_id", "invoice_date", "invoice_no"])
    )
    invoice_dates["order_number"] = invoice_dates.groupby("customer_id").cumcount() + 1
    first = invoice_dates.loc[invoice_dates["order_number"].eq(1)].set_index("customer_id")
    second = (
        invoice_dates.loc[invoice_dates["order_number"].eq(2), ["customer_id", "invoice_date"]]
        .set_index("customer_id")["invoice_date"]
        .rename("second_purchase")
    )
    lifecycle = first[["invoice_date", "country"]].rename(
        columns={"invoice_date": "first_purchase"}
    )
    lifecycle = lifecycle.join(second)
    max_date = customer_sales["invoice_date"].max()
    lifecycle = lifecycle.loc[
        lifecycle["first_purchase"].ge("2011-01-01")
        & lifecycle["first_purchase"].le(max_date - pd.Timedelta(days=90))
    ].copy()
    lifecycle["days_to_second"] = (
        lifecycle["second_purchase"] - lifecycle["first_purchase"]
    ).dt.days
    for days in (30, 60, 90):
        lifecycle[f"repeat_{days}"] = lifecycle["days_to_second"].between(0, days)
    lifecycle["first_purchase_month"] = lifecycle["first_purchase"].dt.to_period("M").astype(str)
    lifecycle["market_group"] = np.where(
        lifecycle["country"].eq("United Kingdom"), "UK", "International"
    )
    baselines = {
        f"repeat_{days}_rate": float(lifecycle[f"repeat_{days}"].mean())
        for days in (30, 60, 90)
    }
    return lifecycle.reset_index(), baselines


def _overseas_market_table(
    pre_period: pd.DataFrame,
    future_flags: pd.DataFrame,
) -> pd.DataFrame:
    overseas = pre_period.loc[pre_period["country"].ne("United Kingdom")].copy()
    customer_market = (
        overseas.groupby(["country", "customer_id"])["line_revenue"].sum().rename("revenue")
    )
    market = (
        overseas.groupby("country")
        .agg(
            pre_revenue_gbp=("line_revenue", "sum"),
            pre_orders=("invoice_no", "nunique"),
            pre_customers=("customer_id", "nunique"),
        )
        .sort_values("pre_revenue_gbp", ascending=False)
        .head(8)
    )
    top_share = customer_market.groupby(level=0).apply(
        lambda values: values.nlargest(1).sum() / values.sum()
    )
    market["top_customer_revenue_share"] = top_share
    post_active = future_flags["retained_90"].rename("post_active")
    market_customer = customer_market.reset_index().join(post_active, on="customer_id")
    market_customer["post_active"] = market_customer["post_active"].fillna(False).astype(bool)
    repeats = market_customer.groupby("country")["post_active"].mean()
    market["historical_90d_repeat_rate"] = repeats
    market["recommended_mode"] = np.where(
        (market["pre_customers"] >= 30)
        & (market["top_customer_revenue_share"] <= 0.70),
        "customer_rct",
        "measurement_only",
    )
    return market.reset_index()


def _experiment_row(
    experiment_id: str,
    name: str,
    primary_metric: str,
    baseline_rate: float,
    eligible_units: int,
    target_lift: float = TARGET_EFFECT,
) -> dict[str, Any]:
    per_arm = required_binary_sample_per_arm(baseline_rate, target_lift)
    required_total = per_arm * 2
    mde = achievable_binary_mde(baseline_rate, eligible_units)
    return {
        "experiment_id": experiment_id,
        "experiment_name": name,
        "primary_metric": primary_metric,
        "historical_baseline_rate": baseline_rate,
        "eligible_units": eligible_units,
        "target_absolute_lift": target_lift,
        "required_sample_per_arm": per_arm,
        "required_total_sample": required_total,
        "achievable_mde": mde,
        "readiness": (
            "powered_with_current_pool"
            if eligible_units >= required_total
            else "pilot_only_underpowered"
        ),
    }


def design_experiments(analysis: dict[str, Any]) -> dict[str, Any]:
    """Create four leakage-safe historical baselines and future-test designs."""
    from src.pipeline import score_rfm

    customer_sales = analysis["customer_sales"].copy()
    pre_period = customer_sales.loc[customer_sales["invoice_date"].lt(SNAPSHOT_DATE)].copy()
    pre_rfm = score_rfm(pre_period, snapshot_date=SNAPSHOT_DATE)
    pre_rfm["market_group"] = np.where(
        pre_rfm["customer_id"].isin(
            pre_period.loc[pre_period["country"].ne("United Kingdom"), "customer_id"]
        ),
        "International",
        "UK",
    )
    future_flags = _future_customer_flags(customer_sales, SNAPSHOT_DATE)
    scored = pre_rfm.join(future_flags, on="customer_id")
    scored[["reactivated_30", "retained_90"]] = scored[
        ["reactivated_30", "retained_90"]
    ].fillna(False).astype(bool)
    scored["revenue_90"] = scored["revenue_90"].fillna(0.0)

    champions = scored.loc[scored["segment"].eq("Champions")].copy()
    at_risk = scored.loc[scored["segment"].eq("At Risk")].copy()
    lifecycle, lifecycle_baselines = _lifecycle_baselines(customer_sales)
    market = _overseas_market_table(pre_period, future_flags)
    rct_markets = market.loc[market["recommended_mode"].eq("customer_rct"), "country"]
    overseas_subjects = (
        pre_period.loc[pre_period["country"].isin(rct_markets)]
        .sort_values("invoice_date")
        .drop_duplicates("customer_id", keep="last")
        [["customer_id", "country"]]
    )
    # Existing customers enter one mutually exclusive layer. Champions and
    # At Risk experiments take routing priority; the overseas preview uses the
    # remaining market-eligible customers so simultaneous launches do not
    # contaminate one another.
    retention_ids = set(champions["customer_id"]).union(at_risk["customer_id"])
    overseas_subjects = overseas_subjects.loc[
        ~overseas_subjects["customer_id"].isin(retention_ids)
    ].copy()
    overseas_scored = overseas_subjects.join(future_flags, on="customer_id")
    overseas_scored["retained_90"] = overseas_scored["retained_90"].fillna(False).astype(bool)

    summary_rows = [
        _experiment_row(
            "champions_protection",
            "Champions 保护",
            "90 天购买留存率",
            float(champions["retained_90"].mean()),
            int(len(champions)),
        ),
        _experiment_row(
            "at_risk_reactivation",
            "At Risk 分批召回",
            "30 天重新购买率",
            float(at_risk["reactivated_30"].mean()),
            int(len(at_risk)),
        ),
        _experiment_row(
            "new_customer_lifecycle",
            "首购后 30/60/90 天运营",
            "90 天二次购买率",
            lifecycle_baselines["repeat_90_rate"],
            int(len(lifecycle)),
        ),
        _experiment_row(
            "overseas_market_validation",
            "海外头部市场验证",
            "90 天再次购买率",
            float(overseas_scored["retained_90"].mean()),
            int(len(overseas_scored)),
        ),
    ]
    summary = pd.DataFrame(summary_rows)

    assignments = pd.concat(
        [
            stratified_assignment(
                champions.assign(monetary_band=pd.qcut(champions["monetary"], 4, labels=False, duplicates="drop")),
                "champions_protection",
                ["market_group", "monetary_band"],
            ),
            stratified_assignment(
                at_risk.assign(recency_band=pd.qcut(at_risk["recency_days"], 3, labels=False, duplicates="drop")),
                "at_risk_reactivation",
                ["recency_band", "f_score"],
                waves=3,
            ),
            stratified_assignment(
                lifecycle,
                "new_customer_lifecycle",
                ["first_purchase_month", "market_group"],
            ),
            stratified_assignment(
                overseas_subjects,
                "overseas_market_validation",
                ["country"],
            ),
        ],
        ignore_index=True,
    )
    assignments["experiment_layer"] = assignments["experiment_id"].map(
        {
            "champions_protection": "existing_customer",
            "at_risk_reactivation": "existing_customer",
            "overseas_market_validation": "existing_customer",
            "new_customer_lifecycle": "new_customer",
        }
    )
    assignments = assignments[
        [
            "experiment_layer",
            "experiment_id",
            "subject_id",
            "stratum",
            "arm",
            "wave",
            "simulation_only",
        ]
    ]

    registry = [
        {
            "experiment_id": "champions_protection",
            "experiment_layer": "existing_customer",
            "hypothesis": "优先服务和新品权益能提高 Champions 的 90 天购买留存率。",
            "unit": "customer_id",
            "arms": ["control: existing service", "treatment: priority service + early access"],
            "randomization": "Within UK/international and monetary quartile strata, 1:1.",
            "primary_metric": "90-day purchase retention",
            "secondary_metrics": ["90-day revenue per customer", "order frequency"],
            "guardrails": ["cancellation rate", "gross-margin proxy when cost is available", "contact opt-out rate"],
            "duration_days": 90,
            "analysis": "Intent-to-treat; report absolute lift and 95% confidence interval.",
        },
        {
            "experiment_id": "at_risk_reactivation",
            "experiment_layer": "existing_customer",
            "hypothesis": "分三批发送差异化召回权益能提高 30 天重新购买率。",
            "unit": "customer_id",
            "arms": ["control: no recall", "treatment: recall, staggered across 3 weekly waves"],
            "randomization": "Within recency band and frequency score strata, 1:1; treatment is split into 3 operational waves.",
            "primary_metric": "30-day reactivation rate",
            "secondary_metrics": ["incremental revenue per assigned customer", "coupon redemption"],
            "guardrails": ["unsubscribe rate", "complaint rate", "discount cost"],
            "duration_days": 30,
            "analysis": "Concurrent holdout remains untouched through all waves; do not compare waves to different calendar baselines.",
        },
        {
            "experiment_id": "new_customer_lifecycle",
            "experiment_layer": "new_customer",
            "hypothesis": "首购后 30/60/90 天节奏化内容能提高 90 天二次购买率。",
            "unit": "customer_id",
            "arms": ["control: current onboarding", "treatment: day 30/60/90 lifecycle journey"],
            "randomization": "Within acquisition month and UK/international strata, 1:1 at first purchase.",
            "primary_metric": "90-day second-purchase rate",
            "secondary_metrics": ["30-day repeat", "60-day repeat", "days to second order"],
            "guardrails": ["message opt-out", "cancellation rate", "contact frequency cap"],
            "duration_days": 90,
            "analysis": "Assign once at first purchase; evaluate by original assignment even if messages fail.",
        },
        {
            "experiment_id": "overseas_market_validation",
            "experiment_layer": "existing_customer",
            "hypothesis": "本地化商品与履约信息能提高具备足够客户基数的海外市场 90 天再次购买率。",
            "unit": "customer_id",
            "arms": ["control: current storefront", "treatment: localized merchandising + delivery message"],
            "randomization": "Within each eligible country, 1:1; high-concentration markets remain measurement-only.",
            "primary_metric": "90-day repeat purchase rate",
            "secondary_metrics": ["revenue per customer", "orders per customer"],
            "guardrails": ["delivery lead time", "cancellation rate", "shipping subsidy per order"],
            "duration_days": 90,
            "analysis": "Country fixed effects; report pooled estimate and country-level heterogeneity only when adequately sized.",
        },
    ]
    return {
        "snapshot_date": SNAPSHOT_DATE.date().isoformat(),
        "summary": summary,
        "assignments": assignments,
        "lifecycle_baselines": lifecycle_baselines,
        "market_feasibility": market,
        "registry": registry,
        "disclaimer": "Historical baselines and simulated assignments only; no intervention was executed.",
    }


def save_experiment_outputs(design: dict[str, Any], root: Path) -> None:
    outputs = root / "outputs"
    experiments = root / "experiments"
    outputs.mkdir(parents=True, exist_ok=True)
    experiments.mkdir(parents=True, exist_ok=True)
    design["summary"].to_csv(outputs / "experiment_feasibility.csv", index=False)
    design["assignments"].to_csv(outputs / "experiment_assignment_preview.csv", index=False)
    design["market_feasibility"].to_csv(outputs / "overseas_market_feasibility.csv", index=False)
    baseline_payload = {
        "snapshot_date": design["snapshot_date"],
        "lifecycle_baselines": design["lifecycle_baselines"],
        "disclaimer": design["disclaimer"],
    }
    (outputs / "experiment_baselines.json").write_text(
        json.dumps(baseline_payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (experiments / "experiment_registry.json").write_text(
        json.dumps(design["registry"], indent=2, ensure_ascii=False), encoding="utf-8"
    )


def save_experiment_charts(design: dict[str, Any], figures_dir: Path) -> None:
    figures_dir.mkdir(parents=True, exist_ok=True)
    summary = design["summary"].copy()
    chart_labels = [
        "Champions protection",
        "At Risk reactivation",
        "30/60/90 lifecycle",
        "Overseas validation",
    ]
    positions = np.arange(len(summary))
    width = 0.36
    fig, axis = plt.subplots(figsize=(11, 5.5), constrained_layout=True)
    axis.barh(positions - width / 2, summary["eligible_units"], width, label="Historical eligible pool", color="#2f6f8f")
    axis.barh(positions + width / 2, summary["required_total_sample"], width, label="Required for +5pp", color="#d99b2b")
    axis.set_yticks(positions, labels=chart_labels)
    axis.set_xscale("log")
    axis.set_xlabel("Customers (log scale)")
    axis.set_title("Experiment feasibility: available pool vs required sample", loc="left", fontweight="bold")
    axis.legend(frameon=False)
    axis.grid(axis="x", alpha=0.2)
    fig.savefig(figures_dir / "06_experiment_feasibility.png", bbox_inches="tight", dpi=140)
    plt.close(fig)

    baselines = design["lifecycle_baselines"]
    days = [30, 60, 90]
    values = [baselines[f"repeat_{day}_rate"] for day in days]
    fig, axis = plt.subplots(figsize=(8, 4.8), constrained_layout=True)
    axis.plot(days, values, marker="o", linewidth=2.5, color="#2f6f8f")
    for day, value in zip(days, values):
        axis.text(day, value + 0.012, f"{value:.1%}", ha="center", color="#17324d")
    axis.set_xticks(days, labels=[f"Day {day}" for day in days])
    axis.set_ylim(0, max(values) * 1.25)
    axis.set_ylabel("Historical second-purchase rate")
    axis.set_title("New-customer lifecycle baseline (not a treatment result)", loc="left", fontweight="bold")
    axis.grid(axis="y", alpha=0.2)
    fig.savefig(figures_dir / "07_lifecycle_baseline.png", bbox_inches="tight", dpi=140)
    plt.close(fig)


def build_experiment_report(design: dict[str, Any], output_path: Path) -> None:
    summary = design["summary"].set_index("experiment_id")
    champions = summary.loc["champions_protection"]
    at_risk = summary.loc["at_risk_reactivation"]
    lifecycle = summary.loc["new_customer_lifecycle"]
    overseas = summary.loc["overseas_market_validation"]
    markets = design["market_feasibility"]
    rct_markets = ", ".join(
        markets.loc[markets["recommended_mode"].eq("customer_rct"), "country"].tolist()
    ) or "无"
    measurement_markets = ", ".join(
        markets.loc[markets["recommended_mode"].eq("measurement_only"), "country"].tolist()
    ) or "无"

    def row(record: pd.Series) -> str:
        mde = "不可稳定估计" if pd.isna(record["achievable_mde"]) else f"{record['achievable_mde']:.1%}"
        return (
            f"| {record['experiment_name']} | {record['historical_baseline_rate']:.1%} | "
            f"{int(record['eligible_units']):,} | {int(record['required_total_sample']):,} | "
            f"{mde} | {record['readiness']} |"
        )

    report = f"""# 后续实验设计与可行性报告

> **结论边界：** 本报告没有真实实验结果。它使用 {design['snapshot_date']} 前的数据定义人群，并只用之后的历史交易估计自然基线；分组文件是模拟预览，不能当作已触达名单或因果证据。

## 1. 统一实验原则

- 分析单位为客户；1:1 随机分组，在关键价值/市场分层内保持平衡。
- 主分析采用 Intent-to-Treat，报告绝对提升、95% 置信区间和分母。
- 显著性水平 5%、把握度 80%；样本量表以检测 **+5 个百分点**绝对提升为目标。
- 每个实验只设一个主指标；收入、订单频次作为次指标，取消、退订、投诉和补贴成本作为护栏。
- At Risk 分批执行仍保留同期控制组，不能把不同自然周直接互相比。
- 客户必须在同一实验层内互斥；现有客户层按 Champions → At Risk → 海外验证路由，新客生命周期使用独立层。

## 2. 历史基线与样本量

| 实验 | 历史基线 | 可用历史人群 | +5pp 所需总样本 | 当前样本可检测提升 | 可行性 |
|---|---:|---:|---:|---:|---|
{row(champions)}
{row(at_risk)}
{row(lifecycle)}
{row(overseas)}

`pilot_only_underpowered` 表示可以验证埋点、分流、触达和成本流程，但当前单期人群不足以对 +5pp 提升作稳定结论。解决方法是延长滚动纳入周期、扩大符合条件人群或接受只检测更大的提升；不能降低统计门槛后再宣布成功。

## 3. 四个实验

### 3.1 Champions 保护

- 假设：优先客服和新品权益提高 90 天购买留存率。
- 对照：现有服务；实验：优先服务 + 新品提前触达。
- 分层：英国/海外 × 历史消费金额四分位。
- 主指标：90 天是否再次购买；次指标：人均收入、订单频次。
- 护栏：取消率、退订率、权益成本；取得成本字段后增加增量毛利。

### 3.2 At Risk 分批召回

- 假设：差异化召回权益提高 30 天重新购买率。
- 先随机划出固定控制组；实验组再按哈希分为 3 个周批次，避免运营容量造成选择偏差。
- 分层：Recency 三档 × Frequency 得分。
- 主指标：30 天重新购买；护栏：退订、投诉和折扣成本。

### 3.3 首购后 30/60/90 天运营

- 历史自然二购率：30 天 {design['lifecycle_baselines']['repeat_30_rate']:.1%}、60 天 {design['lifecycle_baselines']['repeat_60_rate']:.1%}、90 天 {design['lifecycle_baselines']['repeat_90_rate']:.1%}。
- 客户完成首购时一次性随机；实验组进入 30/60/90 天旅程，对照组保持当前 onboarding。
- 主指标：90 天二次购买率；次指标：30/60 天二购、距二购天数。
- 不能只比较“收到消息的人”，发送失败和未打开者仍按原分组进入主分析。

### 3.4 海外头部市场验证

- 可进入客户级随机试验的市场：{rct_markets}。
- 仅做测量和运营准备、暂不单独宣称因果效果的高集中/小样本市场：{measurement_markets}。
- 在每个可测试国家内 1:1 分流，测试本地化商品陈列与履约信息；使用国家固定效应汇总。
- 上线前必须补齐运费补贴、交付时长、税费与成本，否则只能评估购买行为，不能评估利润。

## 4. 埋点与数据合同

每条实验事件至少包含：`experiment_id`、`subject_id`、`arm`、`assignment_time`、`eligibility_snapshot`、`message_sent`、`message_delivered`、`conversion_time`、`order_id`、`revenue`、`discount_cost`、`cancellation`。分组在触达前写入且不可覆盖；原始客户号应留在受控系统，分析层使用稳定伪匿名 ID。

## 5. 停止和决策规则

- 只为安全、投诉或严重数据故障提前停止；不要因中途看到正向波动提前宣布胜出。
- 达到预先设定样本与观察窗口后做一次主检验；多次查看需预先采用序贯校正。
- 推广条件：主指标置信区间排除 0，护栏无实质恶化，且增量价值覆盖权益与履约成本。
- 未显著不等于“完全无效”；应同时报告置信区间覆盖的可能效果范围。

## 6. 可复核产物

- [`outputs/experiment_feasibility.csv`](../outputs/experiment_feasibility.csv)：基线、样本量和 MDE。
- [`outputs/experiment_assignment_preview.csv`](../outputs/experiment_assignment_preview.csv)：哈希客户号的模拟分流预览。
- [`outputs/overseas_market_feasibility.csv`](../outputs/overseas_market_feasibility.csv)：海外市场集中度与模式门槛。
- [`experiments/experiment_registry.json`](../experiments/experiment_registry.json)：假设、分组、指标、护栏和分析规则。
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
