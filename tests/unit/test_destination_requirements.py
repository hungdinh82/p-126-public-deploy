from pathlib import Path

from src.vivi.dialogue.contracts import ActionDraft
from src.vivi.navigation.energy import load_stations

ROOT = Path(__file__).parents[2]


def test_plan_missing_destination_does_not_fabricate_previous_result():
    from src.vivi.capabilities.registry import REGISTRY

    args, message = REGISTRY["navigation.plan"].validate(ActionDraft(capability="navigation.plan"))
    assert args is None and message
    assert load_stations(ROOT / "data/energy/charging-stations.json") == []
