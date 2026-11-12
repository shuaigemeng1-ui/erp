from decimal import Decimal
from itertools import combinations

from app.modules.catalog.aggregates import aggregate_product, aggregate_sales_status, aggregate_value


def test_all_sales_status_combinations():
    values = ("on_sale", "discontinued", "trial", "clearance")
    for size in range(1, 5):
        for statuses in combinations(values, size):
            expected = statuses[0] if size == 1 else (
                "partial_clearance" if "clearance" in statuses else
                "partial_discontinued" if "discontinued" in statuses else "on_sale"
            )
            assert aggregate_sales_status([{"sales_status": value} for value in statuses]) == expected


def test_cost_and_weight_are_independent_and_archived_is_excluded():
    result = aggregate_product([
        {"sales_status": "on_sale", "cost_amount": Decimal("2"), "guidance_cost": Decimal("3"), "average_cost": None, "weight_g": 100},
        {"sales_status": "discontinued", "cost_amount": Decimal("2.0000"), "guidance_cost": Decimal("4"), "average_cost": None, "weight_g": 110},
        {"status": "archived", "sales_status": "clearance", "cost_amount": Decimal("999"), "weight_g": 999},
    ])
    assert result["sales_status"] == "partial_discontinued"
    assert result["cost_summary"]["current"]["value"] == Decimal("2")
    assert result["cost_summary"]["guidance"]["state"] == "multiple"
    assert result["cost_summary"]["average"]["state"] == "missing"
    assert result["weight_summary"]["state"] == "multiple"


def test_missing_is_not_zero_and_cost_access_does_not_touch_cost():
    assert aggregate_value([Decimal("0"), Decimal("0.0000")])["value"] == 0
    assert aggregate_value([None, Decimal("0")])["state"] == "partial_missing"
    assert aggregate_value([None, Decimal("0"), Decimal("1")]) == {"state": "multiple", "value": None, "has_missing": True}

    class WithoutCost:
        status = "active"
        sales_status = "trial"
        weight_g = Decimal("1")

        @property
        def cost_amount(self):
            raise AssertionError("成本列不得读取")

    assert "cost_summary" not in aggregate_product([WithoutCost()], include_cost=False)
