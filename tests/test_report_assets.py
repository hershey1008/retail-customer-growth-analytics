from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_every_generated_figure_is_used_in_a_report():
    report_text = "\n".join(
        (ROOT / "reports" / report_name).read_text(encoding="utf-8")
        for report_name in ("analysis_report.md", "experiment_plan.md")
    )
    figure_names = {
        path.name for path in (ROOT / "reports" / "figures").glob("*.png")
    }

    assert figure_names
    assert all(f"figures/{figure_name}" in report_text for figure_name in figure_names)
