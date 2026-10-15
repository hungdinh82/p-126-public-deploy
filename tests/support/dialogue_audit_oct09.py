"""Shared component fixtures extracted from historical scenario modules."""

from src.vivi.text import normalize_text
from tests.support.long_term_memory import services as services
from tests.support.navigation_workflow import Maps


class AuditMaps(Maps):
    async def search(self, text, **kwargs):
        from src.vivi.navigation.vietmap import Place

        # Vendor queries keep Vietnamese spelling ("đà nẵng"); match on the normalized form.
        if normalize_text(text) == "da nang":
            # A city answers as an administrative result with its boundary, as VietMap does.
            return [
                Place(
                    ref_id="dn-test",
                    name="Đà Nẵng",
                    lat=16,
                    lng=108,
                    boundaries=[{"name": "Đà Nẵng", "full_name": "Thành phố Đà Nẵng"}],
                )
            ]
        if "trạm sạc" in text:
            return [Place(ref_id="charger-test", name="Trạm sạc VinFast TEST", lat=21, lng=105.9)]
        return await super().search(text, **kwargs)
