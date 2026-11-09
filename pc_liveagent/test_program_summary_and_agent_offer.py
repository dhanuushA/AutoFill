"""Regression coverage for the eligibility → application transition."""

from __future__ import annotations

import unittest

from playwright.async_api import async_playwright


class ProgramSummaryAndAgentOfferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            channel="chrome", headless=True
        )
        self._page = await self._browser.new_page()

    async def asyncTearDown(self) -> None:
        await self._browser.close()
        await self._playwright.stop()

    async def test_program_cards_read_only_eligible_programs(self) -> None:
        from pc_liveagent.app_stage import (
            build_programs_permission_speech,
            scrape_program_summary,
        )

        await self._page.set_content(
            """
            <h1>Great News!</h1>
            <p>We found 0 eligible programs for all 2 household members.</p>
            <section class="program-card">
              <div role="checkbox" aria-checked="true"></div>
              <div>Health First Colorado (Medicaid)</div>
              <div>Admin and John may be eligible for Health First Colorado</div>
            </section>
            <section class="program-card">
              <div role="checkbox" aria-checked="true"></div>
              <div>Medicare</div>
              <div>Not eligible at this time</div>
            </section>
            <section class="program-card">
              <div role="checkbox" aria-checked="false"></div>
              <div>Health Connector (ACA Marketplace)</div>
              <div>Not eligible at this time</div>
            </section>
            <button>Continue to Applications</button>
            """
        )

        summary = await scrape_program_summary(self._page)
        speech = build_programs_permission_speech(summary)

        self.assertEqual(len(summary["programs"]), 3)
        self.assertIn("Health First Colorado", speech)
        self.assertNotIn("eligible for Medicare", speech)
        self.assertNotIn("eligible for Health Connector", speech)

    async def test_program_scrape_waits_for_delayed_cards(self) -> None:
        from pc_liveagent.app_stage import wait_for_program_summary

        await self._page.set_content(
            """
            <h1>Loading results</h1>
            <script>
              setTimeout(() => {
                document.body.innerHTML = `
                  <h1>Great News!</h1>
                  <section>
                    <div role="checkbox" aria-checked="true"></div>
                    <div>Health First Colorado (Medicaid)</div>
                    <div>Admin may be eligible for Health First Colorado</div>
                  </section>
                  <button>Continue to Applications</button>`;
              }, 350);
            </script>
            """
        )

        summary = await wait_for_program_summary(self._page, timeout_s=2.0)
        self.assertEqual(len(summary["programs"]), 1)
        self.assertIn("Health First Colorado", summary["programs"][0]["name"])

    async def test_no_agent_clicks_continue_with_medicaid_only(self) -> None:
        from pc_liveagent.app_stage import (
            _finish_agent_offer,
            click_exact_visible_text,
            detect_application_screen,
        )

        await self._page.set_content(
            """
            <h1>Connect with a licensed insurance agent</h1>
            <button id="no">No thanks</button>
            <script>
              document.getElementById('no').onclick = () => {
                document.body.innerHTML = `
                  <h2>Select "Yes, connect me" to get the most from Pointcare</h2>
                  <button id="medicaid">Continue with Medicaid only</button>`;
                document.getElementById('medicaid').onclick = () => {
                  document.body.innerHTML = `
                    <h1>Step 1 of 13</h1>
                    <p>Are all household members U.S. Citizens?</p>`;
                };
              };
            </script>
            """
        )

        self.assertTrue(await click_exact_visible_text(self._page, "No thanks"))
        self.assertTrue(
            await _finish_agent_offer(self._page, wants_connect=False)
        )
        self.assertEqual(
            await detect_application_screen(self._page), "app_citizenship"
        )


if __name__ == "__main__":
    unittest.main()
