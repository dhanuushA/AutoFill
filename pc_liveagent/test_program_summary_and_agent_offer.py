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

    async def test_zero_eligible_selects_masshealth_commonhealth(self) -> None:
        from pc_liveagent.app_stage import (
            ensure_programs_ready_to_continue,
            scrape_program_summary,
        )

        await self._page.set_content(
            """
            <h1>Great News!</h1>
            <p>We found 0 eligible programs for all 2 household members.</p>
            <section id="mh" class="program-card">
              <div id="mh-box" role="checkbox" aria-checked="false"
                   style="width:20px;height:20px;border:1px solid #333;"
                   onclick="this.setAttribute('aria-checked','true');
                            document.getElementById('who').style.display='block';
                            document.getElementById('cta').disabled=false;
                            document.getElementById('count').textContent=
                              'Applying to 1 program for 2 members.';"></div>
              <div>MassHealth CommonHealth (Medicaid)</div>
              <div>John may be eligible for MassHealth CommonHealth</div>
              <div id="who" style="display:none">
                <div>Who is applying?</div>
                <label><input id="m1" type="checkbox" /> Admin PointCare</label>
                <label><input id="m2" type="checkbox" /> John David</label>
              </div>
            </section>
            <section id="medicare" class="program-card">
              <div id="medicare-box" role="checkbox" aria-checked="false"
                   style="width:20px;height:20px;border:1px solid #333;"
                   onclick="this.setAttribute('aria-checked',
                     this.getAttribute('aria-checked')==='true'?'false':'true');"></div>
              <div>Medicare</div>
              <div>Not eligible at this time</div>
            </section>
            <section id="aca" class="program-card">
              <div id="aca-box" role="checkbox" aria-checked="false"
                   style="width:20px;height:20px;border:1px solid #333;"
                   onclick="this.setAttribute('aria-checked',
                     this.getAttribute('aria-checked')==='true'?'false':'true');"></div>
              <div>Health Connector (ACA Marketplace)</div>
              <div>Not eligible at this time</div>
            </section>
            <p id="count">Applying to 0 programs for 0 members.</p>
            <button id="cta" disabled>Continue to Applications</button>
            """
        )

        summary = await scrape_program_summary(self._page)
        self.assertTrue(
            any("MassHealth" in str(p.get("name") or "") for p in summary["programs"])
        )
        ready = await ensure_programs_ready_to_continue(self._page)
        self.assertEqual(ready.get("reason"), "medicaid_fallback")
        self.assertTrue(ready.get("selected"))
        self.assertEqual(
            await self._page.get_attribute("#mh-box", "aria-checked"), "true"
        )
        self.assertEqual(
            await self._page.get_attribute("#medicare-box", "aria-checked"), "false"
        )
        self.assertEqual(
            await self._page.get_attribute("#aca-box", "aria-checked"), "false"
        )
        self.assertTrue(await self._page.is_checked("#m1"))
        self.assertTrue(await self._page.is_checked("#m2"))
        self.assertFalse(await self._page.is_disabled("#cta"))

    async def test_medicaid_fallback_unchecks_other_programs(self) -> None:
        from pc_liveagent.app_stage import select_masshealth_commonhealth_program

        await self._page.set_content(
            """
            <h1>Great News!</h1>
            <p>We found 0 eligible programs for all 2 household members.</p>
            <section id="mh" class="program-card">
              <div id="mh-box" role="checkbox" aria-checked="false"
                   style="width:20px;height:20px;"
                   onclick="this.setAttribute('aria-checked','true');"></div>
              <div>MassHealth CommonHealth (Medicaid)</div>
              <div>John may be eligible for MassHealth CommonHealth</div>
            </section>
            <section class="program-card">
              <div id="medicare-box" role="checkbox" aria-checked="true"
                   style="width:20px;height:20px;"
                   onclick="this.setAttribute('aria-checked',
                     this.getAttribute('aria-checked')==='true'?'false':'true');"></div>
              <div>Medicare</div>
              <div>Not eligible at this time</div>
            </section>
            <section class="program-card">
              <div id="aca-box" role="checkbox" aria-checked="true"
                   style="width:20px;height:20px;"
                   onclick="this.setAttribute('aria-checked',
                     this.getAttribute('aria-checked')==='true'?'false':'true');"></div>
              <div>Health Connector (ACA Marketplace)</div>
              <div>Not eligible at this time</div>
            </section>
            <button>Continue to Applications</button>
            """
        )
        result = await select_masshealth_commonhealth_program(self._page)
        self.assertTrue(result.get("ok"))
        self.assertEqual(
            await self._page.get_attribute("#mh-box", "aria-checked"), "true"
        )
        self.assertEqual(
            await self._page.get_attribute("#medicare-box", "aria-checked"), "false"
        )
        self.assertEqual(
            await self._page.get_attribute("#aca-box", "aria-checked"), "false"
        )
        self.assertGreaterEqual(int(result.get("unchecked_others") or 0), 1)

    async def test_eligible_programs_leave_selection_alone(self) -> None:
        from pc_liveagent.app_stage import ensure_programs_ready_to_continue

        await self._page.set_content(
            """
            <h1>Great News!</h1>
            <p>We found 1 eligible program for all 2 household members.</p>
            <section class="program-card">
              <div id="mh-box" role="checkbox" aria-checked="true"></div>
              <div>MassHealth CommonHealth (Medicaid)</div>
              <div>John may be eligible for MassHealth CommonHealth</div>
            </section>
            <p>Applying to 1 program for 1 member.</p>
            <button>Continue to Applications</button>
            """
        )
        ready = await ensure_programs_ready_to_continue(self._page)
        self.assertFalse(ready.get("selected"))
        self.assertIn(ready.get("reason"), ("already_selected", "eligible_present"))
        self.assertEqual(
            await self._page.get_attribute("#mh-box", "aria-checked"), "true"
        )


if __name__ == "__main__":
    unittest.main()
