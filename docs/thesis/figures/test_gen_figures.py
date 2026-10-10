"""Geometry checks for connectors in gen_figures.py that must touch their shapes."""

from gen_figures import DIAMOND_W, LEFT_X, RIGHT_X, safe_failure_branch


def test_safe_failure_branch_starts_on_the_left_corner_of_the_safety_diamond():
    """The Tidak line of 'Seluruh langkah keselamatan berhasil?' leaves from the diamond's left corner."""
    points = safe_failure_branch(safe_y=7.0, stay_y=8.3, stay_h=0.45)
    assert points[0] == (RIGHT_X - DIAMOND_W / 2, 7.0)


def test_safe_failure_branch_ends_on_top_of_the_stay_active_box():
    """The same line ends on the top edge of 'Tetap aktif' in the left column."""
    points = safe_failure_branch(safe_y=7.0, stay_y=8.3, stay_h=0.45)
    assert points[-1] == (LEFT_X, 8.3 - 0.45 / 2)


def test_feedback_label_names_both_traversals_of_the_arrow():
    """III-1: the arrow is traversed after Scenario B (warm-up, retested in C) and after cycle 1 (final design)."""
    from gen_figures import FEEDBACK_LABEL
    assert "setelah Skenario B" in FEEDBACK_LABEL
    assert "setelah siklus 1" in FEEDBACK_LABEL


def test_cycle_one_note_dates_the_three_validator_set():
    """The cycle-1 chain had four signers until height 325 and a three-member set from height 793."""
    from gen_figures import CYCLE_NOTES
    assert "sejak ketinggian 793" in CYCLE_NOTES[0][0]
