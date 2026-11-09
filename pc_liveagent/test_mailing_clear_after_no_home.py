"""Mailing chip clear after No Home Address — must click X then type Places."""

from __future__ import annotations

import unittest

from playwright.async_api import async_playwright


class MailingClearAfterNoHomeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            channel="chrome", headless=True
        )
        self._page = await self._browser.new_page()

    async def asyncTearDown(self) -> None:
        await self._browser.close()
        await self._playwright.stop()

    async def test_clears_mailing_chip_then_types_places(self) -> None:
        from pc_liveagent.app_stage import (
            _clear_member_address_selection,
            _resolve_spoken_address_suggestion,
            _type_address_for_places_dropdown,
        )

        await self._page.set_content(
            """
            <article id="admin" style="width:360px;display:block;padding:8px">
              <div>Admin P. (You)</div>
              <button type="button" id="cancel">Cancel</button>
              <button type="button" id="save" disabled>Save</button>
              <!-- Collapsed summary labels that previously stole the section band -->
              <div>Home address</div>
              <div>Mailing address</div>
              <div id="form">
                <label>Mailing Address *</label>
                <div style="display:flex;align-items:center;gap:4px">
                  <input id="mail" value="George Washington Bridge, New York, NY, USA"
                    style="width:280px">
                  <button type="button" id="mail-clear"
                    style="width:24px;height:24px;padding:0;border:0;border-radius:9999px;background:#5a1a1a;color:#fff">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2">
                      <path d="M18 6 6 18"/><path d="m6 6 12 12"/>
                    </svg>
                  </button>
                </div>
                <label>Apt, Unit, Building, PO Box (optional)</label>
                <input id="mail-unit">
                <label><input id="no-home" type="checkbox" checked> No Home Address</label>
                <label><input id="no-mail" type="checkbox"> No Mailing Address</label>
              </div>
              <div id="suggestions" style="display:none">
                <div class="pac-item" role="option">Newton Street, Seattle, WA 98109, USA</div>
                <div class="pac-item" role="option">Newton Ave, Seattle, WA, USA</div>
              </div>
            </article>
            <script>
              window.clicks = [];
              document.getElementById('mail-clear').onclick = () => {
                window.clicks.push('clear-mail');
                document.getElementById('mail').value = '';
              };
              document.getElementById('mail').addEventListener('input', () => {
                document.getElementById('suggestions').style.display = 'block';
              });
              for (const item of document.querySelectorAll('.pac-item')) {
                item.onclick = () => {
                  window.clicks.push('suggestion:' + item.innerText);
                  document.getElementById('mail').value = item.innerText;
                  document.getElementById('suggestions').style.display = 'none';
                };
              }
            </script>
            """
        )

        cleared = await _clear_member_address_selection(
            self._page, "Admin P. (You)", "mailing"
        )
        self.assertTrue(cleared.get("mailing"), cleared)
        self.assertIn("clear-mail", await self._page.evaluate("() => window.clicks"))
        self.assertEqual(await self._page.locator("#mail").input_value(), "")

        written = await _type_address_for_places_dropdown(
            self._page, "Admin P. (You)", "mailing", "Newton Street, Seattle"
        )
        self.assertTrue(written.get("mailing"), written)
        self.assertIn("newton", (written.get("mailing") or "").lower())

        resolved = await _resolve_spoken_address_suggestion(
            self._page,
            "Admin P. (You)",
            "mailing",
            "Newton Street, Seattle",
            clear_first=True,
        )
        self.assertTrue(resolved.get("ok"), resolved)
        self.assertIn("Newton Street", str(resolved.get("chosen") or ""))


if __name__ == "__main__":
    unittest.main()
