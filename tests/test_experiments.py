import pandas as pd

from src.experiments import (
    achievable_binary_mde,
    pseudonymize_customer,
    required_binary_sample_per_arm,
    stratified_assignment,
)


def test_required_sample_falls_when_target_effect_grows():
    small_effect = required_binary_sample_per_arm(0.20, 0.03)
    large_effect = required_binary_sample_per_arm(0.20, 0.08)
    assert small_effect > large_effect > 0


def test_achievable_mde_falls_with_larger_pool():
    small_pool = achievable_binary_mde(0.20, 500)
    large_pool = achievable_binary_mde(0.20, 2_000)
    assert small_pool is not None
    assert large_pool is not None
    assert large_pool < small_pool


def test_assignment_is_balanced_within_strata_and_hides_raw_id():
    subjects = pd.DataFrame(
        {
            "customer_id": [str(value) for value in range(12)],
            "segment": ["A"] * 6 + ["B"] * 6,
        }
    )
    assignment = stratified_assignment(subjects, "demo", ["segment"], waves=3)
    balance = assignment.groupby(["stratum", "arm"]).size().unstack(fill_value=0)
    assert (balance["control"] - balance["treatment"]).abs().max() <= 1
    assert not set(subjects["customer_id"]).intersection(assignment["subject_id"])
    assert set(assignment.loc[assignment["arm"].eq("control"), "wave"]) == {"holdout"}


def test_pseudonym_is_stable():
    assert pseudonymize_customer("12345") == pseudonymize_customer("12345")
    assert pseudonymize_customer("12345") != pseudonymize_customer("12346")
