"""Places must be selected from the dropdown — typed text alone must not save."""

from __future__ import annotations

import unittest

from playwright.async_api import async_playwright


class AddressPlacesCommitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            channel="chrome", headless=True
        )
        self._page = await self._browser.new_page()

    async def asyncTearDown(self) -> None:
        await self._browser.close()
        await self._playwright.stop()

    async def test_typed_without_select_is_not_committed_and_validation_blocks(self) -> None:
        from pc_liveagent.app_stage import (
            _address_places_selection_committed,
            _address_validation_visible,
            _click_address_suggestion,
        )

        await self._page.set_content(
            """
            <article id="john" style="width:360px;padding:8px">
              <div>John D.</div>
              <label>Mailing Address *</label>
              <div>
                <input id="mail" value="Radiator Springs Dr, North Carolina 28761, USA">
                <button type="button" id="mail-clear"
                  style="width:24px;height:24px;padding:0;border:0">
                  <svg width="14" height="14" viewBox="0 0 24 24">
                    <path d="M18 6 6 18"></path><path d="m6 6 12 12"></path>
                  </svg>
                </button>
              </div>
              <p id="err" style="color:red">
                Please enter and select a mailing address, or check "No Mailing Address".
              </p>
              <label><input type="checkbox" checked> No Home Address</label>
              <label><input type="checkbox"> No Mailing Address</label>
              <div id="suggestions" style="display:none">
                <div class="pac-item" role="option">Radiator Springs Drive, Nebo, NC, USA</div>
              </div>
              <button type="button">Cancel</button>
              <button type="button">Save</button>
            </article>
            <script>
              window.selected = false;
              document.querySelector('.pac-item').onclick = () => {
                window.selected = true;
                document.getElementById('err').style.display = 'none';
                document.getElementById('mail').value = 'Radiator Springs Drive, Nebo, NC, USA';
                document.getElementById('suggestions').style.display = 'none';
              };
            </script>
            """
        )

        self.assertTrue(
            await _address_validation_visible(self._page, "John D.")
        )
        self.assertFalse(
            await _address_places_selection_committed(
                self._page,
                "John D.",
                "mailing",
                "Radiator Springs",
                "Radiator Springs Drive, Nebo, NC, USA",
            )
        )

        await self._page.evaluate(
            "() => { document.getElementById('suggestions').style.display = 'block'; }"
        )
        self.assertTrue(
            await _click_address_suggestion(
                self._page, "Radiator Springs Drive, Nebo, NC, USA"
            )
        )
        self.assertTrue(await self._page.evaluate("() => window.selected"))
        self.assertFalse(await _address_validation_visible(self._page, "John D."))
        self.assertTrue(
            await _address_places_selection_committed(
                self._page,
                "John D.",
                "mailing",
                "Radiator Springs",
                "Radiator Springs Drive, Nebo, NC, USA",
            )
        )

    async def test_street_vs_st_places_row_is_committed(self) -> None:
        """Google value uses St while pac label keeps Street — must still commit."""
        from pc_liveagent.app_stage import _address_places_selection_committed

        await self._page.set_content(
            """
            <article style="width:360px;padding:8px">
              <div>Admin</div>
              <label>Home Address *</label>
              <div>
                <input id="home" value="India St, Boston, MA, USA">
                <button type="button" style="width:24px;height:24px;padding:0;border:0">
                  <svg width="14" height="14" viewBox="0 0 24 24">
                    <path d="M18 6 6 18"></path><path d="m6 6 12 12"></path>
                  </svg>
                </button>
              </div>
              <label><input type="checkbox"> No Home Address</label>
              <label><input type="checkbox"> No Mailing Address</label>
              <button type="button">Cancel</button>
              <button type="button">Save</button>
            </article>
            """
        )
        self.assertTrue(
            await _address_places_selection_committed(
                self._page,
                "Admin",
                "home",
                "India Street Boston",
                "India Street, Boston, MA, USA",
            )
        )


if __name__ == "__main__":
    unittest.main()
