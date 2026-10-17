"""Focused destination-refinement and real-POI charging-estimate scenarios."""

import pytest

from src.vivi.navigation.destinations import region_for
from tests.support.long_term_memory import services as services
from tests.support.navigation_refinement import DestinationMaps as DestinationMaps


@pytest.mark.parametrize("query", ["TP.HCM", "TP. HCM", "TPHCM", "trung tâm thành phố Hồ Chí Minh"])
def test_hcm_abbreviations_and_explicit_center_are_regions(query):
    assert region_for(query)["name"] == "Hồ Chí Minh"
    assert region_for("Sport House Tp Hcm") is None
