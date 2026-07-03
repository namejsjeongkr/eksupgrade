"""Validate EKS version upgrade path constraints."""

import pytest
from packaging.version import parse as parse_version


class TestUpgradePathValidation:
    """Single-minor-version upgrade constraint must be correctly detectable."""

    @pytest.mark.parametrize(
        "current,target",
        [
            ("1.32", "1.33"),
            ("1.33", "1.34"),
            ("1.34", "1.35"),
            ("1.31", "1.32"),
            ("1.26", "1.27"),
        ],
    )
    def test_sequential_upgrade_is_single_minor(self, current, target):
        """Valid upgrade: target is exactly one minor version ahead."""
        c = parse_version(current)
        t = parse_version(target)
        assert t.minor == c.minor + 1, f"{current} → {target} should differ by exactly 1 minor version"

    @pytest.mark.parametrize(
        "current,target",
        [
            ("1.32", "1.34"),
            ("1.32", "1.35"),
            ("1.30", "1.33"),
            ("1.21", "1.35"),
        ],
    )
    def test_multi_minor_jump_is_detectable(self, current, target):
        """Multi-minor jumps must be detectable (EKS rejects these)."""
        c = parse_version(current)
        t = parse_version(target)
        assert t.minor > c.minor + 1, f"{current} → {target} should be a multi-minor jump"

    def test_downgrade_not_valid(self):
        current = parse_version("1.33")
        target = parse_version("1.32")
        assert target < current


class TestPreviousMinor:
    """Rollback targets exactly one minor below the current version."""

    @pytest.mark.parametrize(
        "current,expected",
        [
            ("1.36", "1.35"),
            ("1.33", "1.32"),
            ("1.10", "1.9"),  # X.10 -> X.9 boundary must not break (no float math)
        ],
    )
    def test_previous_minor(self, current, expected):
        from eksupgrade.models.eks import _previous_minor

        assert _previous_minor(current) == expected
