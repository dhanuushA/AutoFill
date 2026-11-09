"""HTML-fixture checks for relationship and age verification.

These load real DOM in headless Chrome. They do not open Pointcare.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright


_REL_BUTTONS = ("Child", "Stepchild", "Grandchild", "Foster Child", "Other")


def _person_form_html(*, selected: str = "", hint: str = "", age_selected: bool = False) -> str:
    chips = []
    for label in _REL_BUTTONS:
        cls = "active" if label == selected else ""
        chips.append(
            f'<button type="button" class="{cls}" '
            f'onclick="this.classList.add(\'active\'); '
            f"var h=document.getElementById('rel-hint'); if(h) h.remove();\""
            f">{label}</button>"
        )
    age_cls = "active" if age_selected else ""
    hint_html = f'<p id="rel-hint">{hint}</p>' if hint else ""
    return f"""
    <section style="padding:12px">
      <p>Relationship to me</p>
      <div>{''.join(chips)}{hint_html}</div>
      <p>Age</p>
      <button type="button" id="age" class="{age_cls}">(21-63) Adult</button>
    </section>
    """


def _spouse_form_html(*, selected: str = "") -> str:
    buttons = []
    for label in ("Spouse", "Partner"):
        cls = "active" if label == selected else ""
        buttons.append(
            f'<button type="button" class="{cls}" '
            f"onclick=\"this.classList.add('active')\">{label}</button>"
        )
    return f"""
    <section style="padding:12px">
      <p>Are they your spouse or your partner?</p>
      <div>{''.join(buttons)}</div>
      <button type="button" id="age" class="active">(21-63) Adult</button>
    </section>
    """


class RelationshipDomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            channel="chrome", headless=True
        )
        self._page = await self._browser.new_page()

    async def asyncTearDown(self) -> None:
        await self._browser.close()
        await self._playwright.stop()

    async def test_plain_text_please_select_is_a_validation_hint(self) -> None:
        from pc_liveagent.live_form import _visible_validation_hints

        await self._page.set_content(
            _person_form_html(hint="Please select an option")
        )
        hints = (await _visible_validation_hints(self._page)).lower()
        self.assertIn("please select an option", hints)
        self.assertNotIn("role=alert", _person_form_html(hint="Please select an option"))

    async def test_unmarked_click_does_not_count_as_selected(self) -> None:
        from pc_liveagent.live_form import _ensure_relationship_choice_selected

        await self._page.set_content(
            """
            <section style="padding:12px">
              <p>Relationship to me</p>
              <button type="button" id="gc">Grandchild</button>
              <p>Please select an option</p>
            </section>
            """
        )
        await self._page.evaluate("() => { window.__clicks = 0; }")
        await self._page.eval_on_selector(
            "#gc",
            "el => el.addEventListener('click', () => { window.__clicks += 1; })",
        )
        with self.assertRaises(RuntimeError) as ctx:
            await _ensure_relationship_choice_selected(self._page, "Grandchild")
        self.assertIn("couldn't select", str(ctx.exception).lower())
        clicks = await self._page.evaluate("() => window.__clicks")
        self.assertEqual(clicks, 1)

    async def test_spouse_click_without_aria_succeeds_when_prompt_clears(self) -> None:
        from pc_liveagent.live_form import _ensure_relationship_choice_selected

        await self._page.set_content(
            """
            <section style="padding:12px">
              <p>Are they your spouse or your partner?</p>
              <button type="button" id="sp"
                onclick="var h=document.getElementById('rel-hint'); if(h) h.remove();">Spouse</button>
              <p id="rel-hint">Please select an option</p>
            </section>
            """
        )
        await self._page.evaluate("() => { window.__clicks = 0; }")
        await self._page.eval_on_selector(
            "#sp",
            "el => el.addEventListener('click', () => { window.__clicks += 1; })",
        )
        await _ensure_relationship_choice_selected(self._page, "Spouse")
        clicks = await self._page.evaluate("() => window.__clicks")
        self.assertEqual(clicks, 1)
        hint = await self._page.evaluate(
            "() => document.getElementById('rel-hint')"
        )
        self.assertIsNone(hint)

    async def test_new_person_form_clears_both_name_inputs(self) -> None:
        from pc_liveagent.live_form import _clear_new_person_name_fields

        await self._page.set_content(
            """
            <label>First Name <input id="first" value="Pat" /></label>
            <label>Last Name <input id="last" value="Lee" /></label>
            """
        )
        await _clear_new_person_name_fields(self._page)
        first, last = await self._page.evaluate(
            "() => [document.querySelector('#first').value, document.querySelector('#last').value]"
        )
        self.assertEqual(first, "")
        self.assertEqual(last, "")

    async def test_active_class_without_aria_is_not_clicked_again(self) -> None:
        from pc_liveagent.live_form import _ensure_relationship_choice_selected

        await self._page.set_content(_person_form_html(selected="Stepchild"))
        await self._page.evaluate("() => { window.__clicks = 0; }")
        await self._page.evaluate(
            """() => {
              document.querySelectorAll('button').forEach((el) => {
                el.addEventListener('click', () => { window.__clicks += 1; });
              });
            }"""
        )
        await _ensure_relationship_choice_selected(self._page, "Stepchild")
        clicks = await self._page.evaluate("() => window.__clicks")
        self.assertEqual(clicks, 0)

    async def test_each_relationship_chip_selects_once(self) -> None:
        from pc_liveagent.live_form import _ensure_relationship_choice_selected

        for label in _REL_BUTTONS:
            await self._page.set_content(_person_form_html())
            await _ensure_relationship_choice_selected(self._page, label)
            selected = await self._page.evaluate(
                """(label) => {
                  const btn = [...document.querySelectorAll('button')]
                    .find((el) => (el.innerText || '').trim() === label);
                  return !!(btn && btn.classList.contains('active'));
                }""",
                label,
            )
            self.assertTrue(selected, label)

    async def test_spouse_and_partner_select_once(self) -> None:
        from pc_liveagent.live_form import _ensure_relationship_choice_selected

        for label in ("Spouse", "Partner"):
            await self._page.set_content(_spouse_form_html())
            await _ensure_relationship_choice_selected(self._page, label)
            selected = await self._page.evaluate(
                """(label) => {
                  const btn = [...document.querySelectorAll('button')]
                    .find((el) => (el.innerText || '').trim() === label);
                  return btn.classList.contains('active');
                }""",
                label,
            )
            self.assertTrue(selected, label)

    async def test_confirm_yes_blocks_when_page_still_requires_relationship(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _submit_eligibility_answer

        await self._page.set_content(
            _person_form_html(hint="Please select an option", age_selected=True)
        )
        session = LiveFormSession(
            call_id="dom-confirm-block",
            url="https://example.test",
            elig_step="person_confirm",
            after_person="disabled",
        )
        session.filled.update(
            {
                "person_first": "Sam",
                "person_last": "Lee",
                "person_rel": "Foster Child",
                "person_age": "(21-63) Adult",
            }
        )
        cont = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="person_form"),
            ),
            patch("pc_liveagent.live_form._mark"),
            patch("pc_liveagent.live_form._click_continue_to", cont),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await _submit_eligibility_answer(
                    "dom-confirm-block", session, self._page, "Yes", 0.0
                )
        self.assertIn("relationship", str(ctx.exception).lower())
        cont.assert_not_awaited()
        self.assertEqual(session.elig_step, "person_confirm")
        clicks = await self._page.evaluate(
            """() => [...document.querySelectorAll('button')]
              .filter((el) => el.classList.contains('active'))
              .map((el) => (el.innerText || '').trim())"""
        )
        self.assertNotIn("Foster Child", clicks)

    async def test_confirm_yes_continues_when_chip_is_visually_selected(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _submit_eligibility_answer

        await self._page.set_content(
            _person_form_html(selected="Grandchild", age_selected=True)
        )
        session = LiveFormSession(
            call_id="dom-confirm-ok",
            url="https://example.test",
            elig_step="person_confirm",
            after_person="disabled",
        )
        session.filled.update(
            {
                "person_first": "Sam",
                "person_last": "Lee",
                "person_rel": "Grandchild",
                "person_age": "(21-63) Adult",
            }
        )
        cont = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="person_form"),
            ),
            patch("pc_liveagent.live_form._mark"),
            patch("pc_liveagent.live_form._click_continue_to", cont),
        ):
            result = await _submit_eligibility_answer(
                "dom-confirm-ok", session, self._page, "Yes", 0.0
            )
        cont.assert_awaited()
        self.assertTrue(result.get("ok"))
        self.assertEqual(session.elig_step, "disabled")

    async def test_age_chip_is_not_clicked_when_state_is_uncertain(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _assert_member_form_ready_before_continue

        await self._page.set_content(_person_form_html(selected="Child", age_selected=False))
        await self._page.evaluate("() => { window.__ageClicks = 0; }")
        await self._page.eval_on_selector(
            "#age",
            "el => el.addEventListener('click', () => { window.__ageClicks += 1; })",
        )
        session = LiveFormSession(
            call_id="dom-age",
            url="https://example.test",
            elig_step="person_confirm",
        )
        session.filled.update(
            {
                "person_rel": "Child",
                "person_age": "(21-63) Adult",
            }
        )
        await _assert_member_form_ready_before_continue(session, self._page, "person")
        clicks = await self._page.evaluate("() => window.__ageClicks")
        self.assertEqual(clicks, 0)

    async def test_selected_age_without_aria_is_not_clicked(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _assert_member_form_ready_before_continue

        await self._page.set_content(
            _spouse_form_html(selected="Partner")
        )
        await self._page.evaluate("() => { window.__ageClicks = 0; }")
        await self._page.eval_on_selector(
            "#age",
            "el => el.addEventListener('click', () => { window.__ageClicks += 1; })",
        )
        session = LiveFormSession(
            call_id="dom-age-ok",
            url="https://example.test",
            elig_step="spouse_confirm",
        )
        session.filled.update(
            {
                "spouse_role": "Partner",
                "spouse_age": "(21-63) Adult",
            }
        )
        await _assert_member_form_ready_before_continue(session, self._page, "spouse")
        clicks = await self._page.evaluate("() => window.__ageClicks")
        self.assertEqual(clicks, 0)

    async def test_confirm_continues_when_chip_has_no_marker_and_no_prompt(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _submit_eligibility_answer

        await self._page.set_content(
            """
            <section style="padding:12px">
              <p>Are they your spouse or your partner?</p>
              <button type="button">Spouse</button>
              <button type="button" id="age" class="active">(21-63) Adult</button>
            </section>
            """
        )
        session = LiveFormSession(
            call_id="dom-confirm-quiet",
            url="https://example.test",
            elig_step="spouse_confirm",
        )
        session.filled.update(
            {"spouse_role": "Spouse", "spouse_age": "(21-63) Adult"}
        )
        cont = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="spouse_form"),
            ),
            patch("pc_liveagent.live_form._mark"),
            patch("pc_liveagent.live_form._click_continue_to", cont),
        ):
            result = await _submit_eligibility_answer(
                "dom-confirm-quiet", session, self._page, "Yes", 0.0
            )
        cont.assert_awaited()
        self.assertTrue(result.get("ok"))

    async def test_saved_age_without_marker_confirms_without_click(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _assert_member_form_ready_before_continue

        await self._page.set_content(
            """
            <section style="padding:12px">
              <p>Are they your spouse or your partner?</p>
              <button type="button" class="active">Spouse</button>
              <button type="button" id="age">(21-63) Adult</button>
            </section>
            """
        )
        await self._page.evaluate("() => { window.__ageClicks = 0; }")
        await self._page.eval_on_selector(
            "#age",
            "el => el.addEventListener('click', () => { window.__ageClicks += 1; })",
        )
        session = LiveFormSession(
            call_id="dom-age-quiet",
            url="https://example.test",
            elig_step="spouse_confirm",
        )
        session.filled.update(
            {"spouse_role": "Spouse", "spouse_age": "(21-63) Adult"}
        )
        await _assert_member_form_ready_before_continue(session, self._page, "spouse")
        clicks = await self._page.evaluate("() => window.__ageClicks")
        self.assertEqual(clicks, 0)

    async def test_required_age_text_blocks_continue_without_click(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _assert_member_form_ready_before_continue

        await self._page.set_content(
            _person_form_html(selected="Child", hint="Please select an option")
        )
        await self._page.evaluate("() => { window.__ageClicks = 0; }")
        await self._page.eval_on_selector(
            "#age",
            "el => el.addEventListener('click', () => { window.__ageClicks += 1; })",
        )
        session = LiveFormSession(
            call_id="dom-age-required",
            url="https://example.test",
            elig_step="person_confirm",
        )
        session.filled.update(
            {"person_rel": "Child", "person_age": "(21-63) Adult"}
        )
        with self.assertRaises(RuntimeError) as ctx:
            await _assert_member_form_ready_before_continue(session, self._page, "person")
        self.assertTrue(str(ctx.exception))
        clicks = await self._page.evaluate("() => window.__ageClicks")
        self.assertEqual(clicks, 0)

    async def test_missing_saved_age_blocks_continue(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _assert_member_form_ready_before_continue

        await self._page.set_content(_person_form_html(selected="Child"))
        session = LiveFormSession(
            call_id="dom-age-missing",
            url="https://example.test",
            elig_step="person_confirm",
        )
        session.filled["person_rel"] = "Child"
        with self.assertRaises(RuntimeError) as ctx:
            await _assert_member_form_ready_before_continue(session, self._page, "person")
        self.assertIn("age", str(ctx.exception).lower())

    async def test_new_person_form_clears_delayed_autofill_only_once(self) -> None:
        import asyncio

        from pc_liveagent.live_form import LiveFormSession, _clear_new_person_name_fields

        await self._page.set_content(
            """
            <div style="display:none">
              <label>First Name <input id="hidden-first" value="Hidden" /></label>
              <label>Last Name <input id="hidden-last" value="Person" /></label>
            </div>
            <section style="padding:12px">
              <label>First Name <input id="first" value="Pat" /></label>
              <label>Last Name <input id="last" value="Lee" /></label>
            </section>
            """
        )
        session = LiveFormSession(
            call_id="dom-names",
            url="https://example.test",
            elig_step="spouse_first",
        )

        async def late_fill() -> None:
            await asyncio.sleep(0.25)
            await self._page.evaluate(
                "() => { const el = document.querySelector('#first'); if (el) el.value = 'Old'; }"
            )

        task = asyncio.create_task(late_fill())
        await _clear_new_person_name_fields(self._page, session, kind="spouse")
        await task
        visible = await self._page.evaluate(
            "() => [document.querySelector('#first').value, document.querySelector('#last').value]"
        )
        hidden = await self._page.evaluate(
            "() => [document.querySelector('#hidden-first').value, document.querySelector('#hidden-last').value]"
        )
        self.assertEqual(visible, ["", ""])
        self.assertEqual(hidden, ["Hidden", "Person"])
        await self._page.evaluate(
            "() => { document.querySelector('#first').value = 'Pat'; document.querySelector('#last').value = 'Lee'; }"
        )
        await _clear_new_person_name_fields(self._page, session, kind="spouse")
        again = await self._page.evaluate(
            "() => [document.querySelector('#first').value, document.querySelector('#last').value]"
        )
        self.assertEqual(again, ["Pat", "Lee"])
        session.filled["spouse_first"] = "Alex"
        session.filled.pop("name_fields_reset_for", None)
        await _clear_new_person_name_fields(self._page, session, kind="spouse")
        kept = await self._page.evaluate("() => document.querySelector('#first').value")
        self.assertEqual(kept, "Pat")

    async def test_relationship_options_exclude_household_chips(self) -> None:
        from pc_liveagent.live_form import (
            LiveFormSession,
            _relationship_speech_for_page,
            _visible_relationship_option_labels,
        )

        await self._page.set_content(
            """
            <nav>
              <button type="button">Your household</button>
              <button type="button">Agent You</button>
              <button type="button">john Partner</button>
            </nav>
            <section style="padding:12px">
              <p>Relationship to me</p>
              <div>
                <button type="button">Child</button>
                <button type="button">Stepchild</button>
                <button type="button">Grandchild</button>
                <button type="button">Foster Child</button>
                <button type="button">Other</button>
              </div>
            </section>
            """
        )
        labels = await _visible_relationship_option_labels(self._page)
        self.assertEqual(
            labels,
            ["Child", "Stepchild", "Grandchild", "Foster Child", "Other"],
        )
        session = LiveFormSession(call_id="rel-chips", url="https://example.test")
        speech = await _relationship_speech_for_page(session, self._page, "person_rel")
        low = speech.lower()
        self.assertNotIn("john partner", low)
        self.assertNotIn("agent you", low)
        self.assertNotIn("your household", low)
        for option in ("child", "stepchild", "grandchild", "foster child", "other"):
            self.assertIn(option, low)

        await self._page.set_content(
            """
            <nav>
              <button type="button">Agent You</button>
              <button type="button">john Partner</button>
            </nav>
            <section style="padding:12px">
              <p>Are they your spouse or your partner?</p>
              <div>
                <button type="button">Spouse</button>
                <button type="button">Partner</button>
              </div>
            </section>
            """
        )
        spouse = await _visible_relationship_option_labels(self._page)
        self.assertEqual(spouse, ["Spouse", "Partner"])

    async def test_support_options_keep_long_labels_without_household(self) -> None:
        from pc_liveagent.live_form import (
            _SUPPORT_CANONICAL,
            _visible_support_option_labels,
        )

        student, lives, disabled, relative, no = _SUPPORT_CANONICAL
        blob = " ".join(_SUPPORT_CANONICAL)
        await self._page.set_content(
            f"""
            <nav>
              <button type="button">Your household</button>
              <button type="button">Agent You</button>
              <button type="button">john Partner</button>
            </nav>
            <section style="padding:12px">
              <p>Does someone provide more than half of your annual financial support?</p>
              <div class="options">{blob}</div>
            </section>
            """
        )
        labels = await _visible_support_option_labels(self._page)
        self.assertEqual(labels, [student, lives, disabled, relative, no])
        joined = " ".join(labels).lower()
        self.assertNotIn("john partner", joined)
        self.assertNotIn("agent you", joined)
        self.assertNotIn("your household", joined)

    async def test_special_members_are_separate_from_household_bar(self) -> None:
        from pc_liveagent.live_form import (
            _click_special_person_member,
            _discover_special_person_members,
        )

        await self._page.set_content(
            """
            <nav>
              <div>Your household <span>Agent</span> <span>You</span>
                <span>john</span> <span>Partner</span></div>
            </nav>
            <section>
              <p>Someone is blind</p>
              <p>Who does this apply to?</p>
              <div>
                <button id="agent" type="button" role="checkbox" aria-checked="false"
                  onclick="this.setAttribute('aria-checked','true')"></button>
                <span>Agent</span><span>You</span>
              </div>
              <div>
                <button id="john" type="button" role="checkbox" aria-checked="false"
                  onclick="this.setAttribute('aria-checked','true')"></button>
                <span>john</span><span>Partner</span>
              </div>
            </section>
            """
        )
        discovered = await _discover_special_person_members(
            self._page, "Someone is blind"
        )
        labels = [member["label"] for member in discovered["members"]]
        self.assertEqual(labels, ["Agent", "john"])
        self.assertNotIn("Your household", labels)
        self.assertNotIn("Partner", labels)
        clicked = await _click_special_person_member(
            self._page, "john Partner", condition="Someone is blind"
        )
        self.assertTrue(clicked)
        state = await self._page.evaluate(
            """() => ({
              john: document.querySelector('#john').getAttribute('aria-checked'),
              agent: document.querySelector('#agent').getAttribute('aria-checked'),
            })"""
        )
        self.assertEqual(state["john"], "true")
        self.assertEqual(state["agent"], "false")

    async def test_citizenship_radios_stay_inside_each_member_card(self) -> None:
        from pc_liveagent.app_stage import (
            _scrape_immigration_members,
            _set_member_immigration_status,
            _verify_member_immigration_status,
        )

        def card(name: str, group: str) -> str:
            options = [
                ("citizen", "U.S. Citizen", True),
                ("national", "U.S. National", False),
                (
                    "status",
                    "Satisfactory immigration status (green card, etc.)",
                    False,
                ),
                ("skip", "Prefer not to answer", False),
            ]
            rows = []
            for value, label, checked in options:
                mark = " checked" if checked else ""
                rows.append(
                    f'<label style="display:block">'
                    f'<input type="radio" name="{group}" value="{value}"{mark} '
                    f'onclick="window.clicks.push(\'{group}:{value}\')">{label}</label>'
                )
            return (
                f'<article style="display:block;margin:8px">'
                f"<div>{name}</div>{''.join(rows)}</article>"
            )

        await self._page.set_content(
            """
            <section style="padding:12px">
              <h1>Are Agent and john all U.S. Citizens?</h1>
              <button type="button" id="yes">Yes — Everyone listed is a U.S. Citizen</button>
              <button type="button" id="no">No — Some or all are not U.S. Citizens</button>
              <div id="cards"></div>
            </section>
            <script>window.clicks = [];</script>
            """
        )
        await self._page.evaluate(
            """(html) => { document.querySelector('#cards').innerHTML = html; }""",
            card("Agent", "agent") + card("john", "john") + card("Don", "don"),
        )
        discovered = await _scrape_immigration_members(self._page)
        self.assertEqual([row["name"] for row in discovered], ["Agent", "john", "Don"])
        self.assertTrue(
            all(row["selected"] == "U.S. Citizen" for row in discovered)
        )
        clicked, click_reason = await _set_member_immigration_status(
            self._page,
            "john",
            "Satisfactory immigration status (green card, etc.)",
        )
        self.assertTrue(clicked)
        self.assertEqual(click_reason, "selected")
        verified, verify_reason = await _verify_member_immigration_status(
            self._page,
            "john",
            "Satisfactory immigration status (green card, etc.)",
        )
        self.assertTrue(verified)
        self.assertEqual(verify_reason, "verified")
        state = await self._page.evaluate(
            """() => ({
              clicks: window.clicks,
              agent: document.querySelector('input[name=agent][value=citizen]').checked,
              johnCitizen: document.querySelector('input[name=john][value=citizen]').checked,
              johnStatus: document.querySelector('input[name=john][value=status]').checked,
              don: document.querySelector('input[name=don][value=citizen]').checked,
            })"""
        )
        self.assertEqual(state["clicks"], ["john:status"])
        self.assertTrue(state["agent"])
        self.assertFalse(state["johnCitizen"])
        self.assertTrue(state["johnStatus"])
        self.assertTrue(state["don"])
        # Already selected for this member: accept without clicking/toggling.
        confirmed, confirm_reason = await _set_member_immigration_status(
            self._page, "Don", "U.S. Citizen"
        )
        self.assertTrue(confirmed)
        self.assertEqual(confirm_reason, "already_selected")
        clicks_after = await self._page.evaluate("() => window.clicks.slice()")
        self.assertEqual(clicks_after, ["john:status"])
        still_citizen = await self._page.evaluate(
            "() => document.querySelector('input[name=don][value=citizen]').checked"
        )
        self.assertTrue(still_citizen)

    async def test_immigration_us_vs_u_s_punctuation_and_wrong_member(self) -> None:
        from pc_liveagent.app_stage import (
            _set_member_immigration_status,
            _verify_member_immigration_status,
        )

        await self._page.set_content(
            """
            <section style="padding:12px">
              <article>
                <div>Agent T.</div>
                <label><input type="radio" name="agent" value="c" checked
                  onclick="window.clicks.push('agent:c')">US Citizen</label>
                <label><input type="radio" name="agent" value="n"
                  onclick="window.clicks.push('agent:n')">US National</label>
                <label><input type="radio" name="agent" value="s"
                  onclick="window.clicks.push('agent:s')">Satisfactory immigration status</label>
                <label><input type="radio" name="agent" value="p"
                  onclick="window.clicks.push('agent:p')">Prefer not to answer</label>
              </article>
              <article>
                <div>john D.</div>
                <label><input type="radio" name="john" value="c" checked
                  onclick="window.clicks.push('john:c')">US Citizen</label>
                <label><input type="radio" name="john" value="n"
                  onclick="window.clicks.push('john:n')">US National</label>
                <label><input type="radio" name="john" value="s"
                  onclick="window.clicks.push('john:s')">Satisfactory immigration status</label>
                <label><input type="radio" name="john" value="p"
                  onclick="window.clicks.push('john:p')">Prefer not to answer</label>
              </article>
            </section>
            <script>window.clicks = [];</script>
            """
        )
        # Preselected US Citizen matches spoken U.S. Citizen — no click.
        ok, reason = await _set_member_immigration_status(
            self._page, "Agent T", "U.S. Citizen"
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "already_selected")
        clicks = await self._page.evaluate("() => window.clicks.slice()")
        self.assertEqual(clicks, [])

        ok, reason = await _set_member_immigration_status(
            self._page, "Agent T.", "U.S. National"
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "selected")
        verified, _ = await _verify_member_immigration_status(
            self._page, "Agent T", "US National"
        )
        self.assertTrue(verified)
        state = await self._page.evaluate(
            """() => ({
              clicks: window.clicks.slice(),
              agentN: document.querySelector('input[name=agent][value=n]').checked,
              johnC: document.querySelector('input[name=john][value=c]').checked,
              johnN: document.querySelector('input[name=john][value=n]').checked,
            })"""
        )
        self.assertEqual(state["clicks"], ["agent:n"])
        self.assertTrue(state["agentN"])
        self.assertTrue(state["johnC"])
        self.assertFalse(state["johnN"])

        missing, missing_reason = await _set_member_immigration_status(
            self._page, "Nobody", "U.S. National"
        )
        self.assertFalse(missing)
        self.assertEqual(missing_reason, "member_not_found")

        # Ambiguous duplicate names must not pick either card.
        await self._page.set_content(
            """
            <section>
              <article>
                <div>John D.</div>
                <label><input type="radio" name="j1" value="n">U.S. National</label>
                <label><input type="radio" name="j1" value="c" checked>U.S. Citizen</label>
              </article>
              <article>
                <div>John D.</div>
                <label><input type="radio" name="j2" value="n">U.S. National</label>
                <label><input type="radio" name="j2" value="c" checked>U.S. Citizen</label>
              </article>
            </section>
            """
        )
        amb, amb_reason = await _set_member_immigration_status(
            self._page, "John D.", "U.S. National"
        )
        self.assertFalse(amb)
        self.assertEqual(amb_reason, "multiple_member_matches")

    async def test_immigration_delayed_aria_radio_and_no_forced_checked(self) -> None:
        from pc_liveagent.app_stage import (
            _set_member_immigration_status,
            _verify_member_immigration_status,
        )

        # Custom role=radio: aria-checked flips only after a short delay.
        await self._page.set_content(
            """
            <section style="padding:12px">
              <article>
                <div>Agent T.</div>
                <div role="radio" aria-checked="true" id="c"
                  onclick="window.pick(this)">U.S. Citizen</div>
                <div role="radio" aria-checked="false" id="n"
                  onclick="window.pick(this)">U.S. National</div>
                <div role="radio" aria-checked="false" id="p"
                  onclick="window.pick(this)">Prefer not to answer</div>
              </article>
            </section>
            <script>
              window.clicks = [];
              window.pick = (el) => {
                window.clicks.push(el.id);
                const group = el.parentElement.querySelectorAll('[role=radio]');
                setTimeout(() => {
                  group.forEach((node) => {
                    node.setAttribute('aria-checked', node === el ? 'true' : 'false');
                  });
                }, 120);
              };
            </script>
            """
        )
        ok, reason = await _set_member_immigration_status(
            self._page, "Agent T.", "U.S. National"
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "selected")
        verified, _ = await _verify_member_immigration_status(
            self._page, "Agent T.", "U.S. National"
        )
        self.assertTrue(verified)
        state = await self._page.evaluate(
            """() => ({
              clicks: window.clicks.slice(),
              n: document.getElementById('n').getAttribute('aria-checked'),
              c: document.getElementById('c').getAttribute('aria-checked'),
            })"""
        )
        self.assertEqual(state["clicks"], ["n"])
        self.assertEqual(state["n"], "true")
        self.assertEqual(state["c"], "false")

        # Click is ignored; selection never becomes active. Success must not
        # come from assigning element.checked / aria-checked in the setter.
        await self._page.set_content(
            """
            <section style="padding:12px">
              <article>
                <div>Agent T.</div>
                <label>
                  <input type="radio" name="agent" value="c" checked>U.S. Citizen
                </label>
                <label>
                  <input id="stuck" type="radio" name="agent" value="n"
                    onclick="event.preventDefault(); event.stopImmediatePropagation(); window.clicks.push('blocked'); return false;">
                  U.S. National
                </label>
              </article>
            </section>
            <script>window.clicks = [];</script>
            """
        )
        failed, fail_reason = await _set_member_immigration_status(
            self._page, "Agent T.", "U.S. National"
        )
        self.assertFalse(failed)
        self.assertIn(
            fail_reason,
            {"selection_timeout", "state_not_reflected", "click_failed"},
        )
        still_off = await self._page.evaluate(
            "() => !document.getElementById('stuck').checked"
        )
        self.assertTrue(still_off)
        # Manual DOM mutation alone is not how the setter confirms success:
        # after a forced property write without a working interaction, verify
        # may see the property, but the setter must not claim selected when
        # the live interaction never activated the control.
        await self._page.evaluate(
            "() => { document.getElementById('stuck').checked = true; }"
        )
        # Re-run set for Prefer not — missing option — and ensure the prior
        # forced National check did not invent a confirmed setter success path
        # for a blocked control (blocked path already returned false above).
        missing, missing_reason = await _set_member_immigration_status(
            self._page, "Agent T.", "Prefer not to answer"
        )
        self.assertFalse(missing)
        self.assertEqual(missing_reason, "status_option_not_found")

    async def test_immigration_next_inspect_states_and_delayed_enable(self) -> None:
        from pc_liveagent.app_stage import (
            _click_enabled_application_next,
            _inspect_application_next,
            _wait_immigration_next_ready,
        )

        await self._page.set_content(
            """
            <div class="Mui-disabled" style="padding:8px">
              <button type="button" id="next" disabled>Next</button>
            </div>
            """
        )
        info = await _inspect_application_next(self._page)
        self.assertEqual(info["status"], "next_disabled")
        self.assertTrue(info.get("native_disabled"))

        await self._page.evaluate(
            """() => {
              const btn = document.getElementById('next');
              setTimeout(() => { btn.disabled = false; }, 200);
            }"""
        )
        ready = await _wait_immigration_next_ready(
            self._page, polls=20, interval_ms=50
        )
        self.assertEqual(ready["status"], "next_enabled")

        await self._page.set_content("<section><p>No actions</p></section>")
        missing = await _inspect_application_next(self._page)
        self.assertEqual(missing["status"], "next_not_found")

        await self._page.set_content(
            """
            <button type="button" id="hidden" style="display:none">Next</button>
            """
        )
        hidden = await _inspect_application_next(self._page)
        self.assertEqual(hidden["status"], "next_hidden")

        # Ancestor disabled class must not mark an enabled Next as disabled.
        await self._page.set_content(
            """
            <div class="disabled Mui-disabled" style="padding:8px">
              <button type="button" id="ok">Next</button>
            </div>
            """
        )
        ancestor = await _inspect_application_next(self._page)
        self.assertEqual(ancestor["status"], "next_enabled")

        # Misleading class substrings must not block the yellow footer Next.
        # Old token-split logic treated "disabled" inside other names as disabled.
        await self._page.set_content(
            """
            <button type="button" id="stale" disabled style="position:fixed;top:8px">Next</button>
            <button type="button" id="hidden-dup" style="display:none">Next</button>
            <footer style="position:fixed;bottom:0;left:0;right:0;padding:12px;background:#fff">
              <button type="button" id="footer"
                class="MuiButton-disableElevation next-disabled-theme"
                style="background:#f5c518">Next</button>
            </footer>
            <script>window.clicks=[]; document.getElementById('footer')
              .addEventListener('click', () => window.clicks.push('footer'));</script>
            """
        )
        footer = await _inspect_application_next(self._page)
        self.assertEqual(footer["status"], "next_enabled")
        self.assertFalse(footer.get("native_disabled"))
        self.assertEqual(footer.get("aria_disabled") or "", "")
        self.assertTrue(await _click_enabled_application_next(self._page))
        clicks = await self._page.evaluate("() => window.clicks.slice()")
        self.assertEqual(clicks, ["footer"])
        # Second click must still target the enabled footer, not the stale top Next.
        await self._page.evaluate("() => { window.clicks = []; }")
        again = await _inspect_application_next(self._page)
        self.assertEqual(again["status"], "next_enabled")

    async def test_immigration_click_exception_is_click_failed(self) -> None:
        from pc_liveagent.app_stage import _set_member_immigration_status

        await self._page.set_content(
            """
            <section>
              <article>
                <div>Agent T.</div>
                <label><input type="radio" name="a" value="c" checked>U.S. Citizen</label>
                <label><input type="radio" name="a" value="n">U.S. National</label>
              </article>
            </section>
            """
        )

        class _Boom:
            async def count(self) -> int:
                return 1

            async def check(self, *args, **kwargs):
                raise RuntimeError("boom-check")

            async def click(self, *args, **kwargs):
                raise RuntimeError("boom-click")

            @property
            def first(self):
                return self

        real_locator = self._page.locator

        def _fake_locator(sel, *args, **kwargs):
            if "data-pc-imm" in str(sel):
                return _Boom()
            return real_locator(sel, *args, **kwargs)

        with patch.object(self._page, "locator", side_effect=_fake_locator):
            ok, reason = await _set_member_immigration_status(
                self._page, "Agent T.", "U.S. National"
            )
        self.assertFalse(ok)
        self.assertEqual(reason, "click_failed")

    async def test_next_ignores_decoy_and_ancestor_disabled_class(self) -> None:
        from pc_liveagent.app_stage import (
            _click_enabled_application_next,
            _next_button_enabled,
            _visible_form_validation,
        )

        await self._page.set_content(
            """
            <div class="disabled" style="padding:8px">
              <button type="button" id="decoy" disabled
                onclick="window.clicks.push('decoy')">Next</button>
            </div>
            <div style="height:240px"></div>
            <button type="button" id="real" class="undisabled"
              onclick="window.clicks.push('real')">Next</button>
            <div role="alert">Select a status for each person.</div>
            <script>window.clicks = [];</script>
            """
        )
        self.assertTrue(await _next_button_enabled(self._page))
        self.assertIn("Select a status", await _visible_form_validation(self._page))
        self.assertTrue(await _click_enabled_application_next(self._page))
        clicks = await self._page.evaluate("() => window.clicks.slice()")
        self.assertEqual(clicks, ["real"])

        await self._page.set_content(
            """
            <button type="button" id="only" disabled class="disabled">Next</button>
            """
        )
        self.assertFalse(await _next_button_enabled(self._page))

    async def test_coverage_checkboxes_follow_the_named_member(self) -> None:
        from pc_liveagent.app_stage import (
            _scrape_coverage_members,
            _set_member_insurance_checked,
            _verify_coverage_member_checked,
        )

        await self._page.set_content(
            """
            <section style="padding:12px">
              <h1>Coverage Statuses</h1>
              <p>Do Agent and john have health insurance or health coverage?</p>
              <button type="button" id="yes">Yes — Someone has coverage</button>
              <button type="button" id="no">No — No one has coverage</button>
              <div>
                <h2>Who has coverage?</h2>
                <label style="display:block">
                  <input id="agent" type="checkbox"
                    onclick="window.clicks.push('agent')"> Agent
                </label>
                <label style="display:block">
                  <input id="john" type="checkbox"
                    onclick="window.clicks.push('john')"> john
                </label>
                <label style="display:block">
                  <input id="don" type="checkbox" checked
                    onclick="window.clicks.push('don')"> Don
                </label>
              </div>
              <button type="button" id="next" disabled>Next</button>
            </section>
            <script>window.clicks = [];</script>
            """
        )
        discovered = await _scrape_coverage_members(self._page)
        self.assertEqual(
            [row["name"] for row in discovered],
            ["Agent", "john", "Don"],
        )
        self.assertEqual(
            [row["checked"] for row in discovered],
            [False, False, True],
        )
        self.assertTrue(await _set_member_insurance_checked(self._page, "john", True))
        self.assertTrue(await _verify_coverage_member_checked(self._page, "john"))
        self.assertFalse(await _verify_coverage_member_checked(self._page, "Agent"))
        self.assertTrue(await _set_member_insurance_checked(self._page, "Agent", True))
        self.assertTrue(await _set_member_insurance_checked(self._page, "Don", True))
        state = await self._page.evaluate(
            """() => ({
              clicks: window.clicks,
              agent: document.querySelector('#agent').checked,
              john: document.querySelector('#john').checked,
              don: document.querySelector('#don').checked,
            })"""
        )
        self.assertEqual(state["clicks"], ["john", "agent"])
        self.assertTrue(state["agent"])
        self.assertTrue(state["john"])
        self.assertTrue(state["don"])

    async def test_coverage_john_d_and_agent_t_custom_and_delayed(self) -> None:
        """Native + custom ARIA rows; delayed select; already-on; wrong-member guard."""
        from pc_liveagent.app_stage import (
            _ensure_member_coverage_expanded,
            _match_named_members,
            _member_coverage_types_visible,
            _set_member_coverage_type,
            _set_member_insurance_checked,
            _verify_coverage_member_checked,
            _verify_member_coverage_types,
        )

        await self._page.set_content(
            """
            <section>
              <h2>Who has coverage?</h2>
              <article>
                <input id="agent" type="checkbox"
                  style="opacity:0;position:absolute">
                <label class="member-label" for="agent" style="display:none">Agent T.</label>
                <button role="button" aria-expanded="false"
                  aria-controls="agent-plans"
                  onclick="this.setAttribute('aria-expanded','true');
                    this.dataset.state='expanded';
                    document.querySelector('#agent-plans').style.display='block'">
                  <h3>Agent T.</h3>
                </button>
                <div id="agent-plans" style="display:none">
                  <p>What type of coverage?</p>
                  <label><input id="agent-medicare" type="checkbox"
                    onchange="document.querySelector('#agent').checked=this.checked">
                    Medicare</label>
                </div>
              </article>
              <article>
                <div class="MuiFormControlLabel-root" style="display:flex;gap:8px;align-items:center">
                  <span id="john" role="checkbox" aria-checked="false" tabindex="0"
                    style="display:inline-block;width:24px;height:24px;border:1px solid #333;background:#fff"
                    onclick="(function(el){
                      if (el.dataset.pending) return;
                      el.dataset.pending = '1';
                      setTimeout(function(){
                        el.setAttribute('aria-checked','true');
                        el.classList.add('Mui-checked');
                        delete el.dataset.pending;
                      }, 250);
                    })(this)">
                  </span>
                  <span>john D.</span>
                </div>
              </article>
              <article>
                <label style="display:block">
                  <input id="don" type="checkbox" checked> Don D.
                </label>
              </article>
            </section>
            """
        )
        available = ["Agent T.", "john D.", "Don D."]
        hits, clarify = _match_named_members("I said John d.", available)
        self.assertIsNone(clarify)
        self.assertEqual(hits, ["john D."])
        hits_t, clarify_t = _match_named_members("Agent t.", available)
        self.assertIsNone(clarify_t)
        self.assertEqual(hits_t, ["Agent T."])

        self.assertTrue(
            await _set_member_insurance_checked(self._page, "john D.", True)
        )
        self.assertTrue(
            await _verify_coverage_member_checked(self._page, "john D.")
        )
        self.assertFalse(
            await _verify_coverage_member_checked(self._page, "Agent T.")
        )
        # john's expanded branch must not make Agent appear expanded.
        self.assertFalse(
            await _member_coverage_types_visible(self._page, "Agent T.")
        )
        self.assertTrue(
            await _ensure_member_coverage_expanded(self._page, "Agent T.")
        )
        self.assertTrue(
            await _member_coverage_types_visible(self._page, "Agent T.")
        )
        self.assertTrue(
            await _set_member_coverage_type(
                self._page, "Agent T.", "Medicare"
            )
        )
        self.assertTrue(
            await _verify_member_coverage_types(
                self._page, "Agent T.", ["Medicare"]
            )
        )
        # Already selected — must not toggle Don off.
        self.assertTrue(
            await _set_member_insurance_checked(self._page, "Don D.", True)
        )
        state = await self._page.evaluate(
            """() => ({
              agent: document.querySelector('#agent').checked,
              john: document.querySelector('#john').getAttribute('aria-checked'),
              don: document.querySelector('#don').checked,
            })"""
        )
        # The derived member status becomes checked only after selecting a plan.
        self.assertTrue(state["agent"])
        self.assertEqual(state["john"], "true")
        self.assertTrue(state["don"])

    async def test_dob_save_stays_on_the_named_member(self) -> None:
        from pc_liveagent.app_stage import (
            _click_member_add_dob,
            _fill_and_save_dob,
            _scrape_dob_cards,
            _verify_member_dob,
        )

        await self._page.set_content(
            """
            <h1>Date of birth</h1>
            <article id="agent" style="width:280px;height:160px;display:block">
              <div style="height:24px">Agent</div>
              <button type="button" id="agent-add" onclick="openEditor('agent')">Add</button>
              <div id="agent-saved"></div>
              <p id="agent-error"></p>
              <div id="agent-editor" style="display:none">
                <div style="display:flex;gap:8px">
                  <input id="agent-mm" placeholder="MM" maxlength="2">
                  <input id="agent-dd" placeholder="DD" maxlength="2">
                  <input id="agent-yyyy" placeholder="YYYY" maxlength="4">
                </div>
                <button type="button" onclick="saveDob('agent')">Save</button>
                <button type="button">Cancel</button>
              </div>
            </article>
            <article id="john" style="width:280px;height:160px;display:block">
              <div style="height:24px">john</div>
              <div id="john-saved">01/02/1990</div>
              <button type="button" id="john-edit" onclick="openEditor('john')">
                <svg width="14" height="14" viewBox="0 0 24 24">
                  <path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174"></path>
                  <path d="m15 5 4 4"></path>
                </svg>
              </button>
              <p id="john-error"></p>
              <div id="john-editor" style="display:none">
                <div style="display:flex;gap:8px">
                  <input id="john-mm" placeholder="MM" maxlength="2">
                  <input id="john-dd" placeholder="DD" maxlength="2">
                  <input id="john-yyyy" placeholder="YYYY" maxlength="4">
                </div>
                <button type="button" onclick="saveDob('john')">Save</button>
                <button type="button">Cancel</button>
              </div>
            </article>
            <script>
              window.opened = [];
              function openEditor(id) {
                window.opened.push(id);
                document.getElementById(id + '-editor').style.display = 'block';
                const add = document.getElementById(id + '-add');
                if (add) add.style.display = 'none';
              }
              function saveDob(id) {
                const mm = document.getElementById(id + '-mm').value;
                const dd = document.getElementById(id + '-dd').value;
                const yyyy = document.getElementById(id + '-yyyy').value;
                const error = document.getElementById(id + '-error');
                if (document.body.dataset.fail === '1' || !mm || !dd || !yyyy) {
                  error.textContent = 'Please enter a valid date';
                  return;
                }
                error.textContent = '';
                document.getElementById(id + '-saved').textContent =
                  mm + '/' + dd + '/' + yyyy;
                document.getElementById(id + '-editor').style.display = 'none';
              }
            </script>
            """
        )
        self.assertTrue(await _click_member_add_dob(self._page, "Agent"))
        opened = await self._page.evaluate("() => window.opened.slice()")
        self.assertEqual(opened, ["agent"])
        self.assertTrue(
            await _fill_and_save_dob(self._page, "03/15/2001", "Agent")
        )
        cards = await _scrape_dob_cards(self._page)
        by_name = {card["name"]: card for card in cards}
        self.assertEqual(by_name["Agent"]["dob"], "03/15/2001")
        self.assertFalse(by_name["Agent"]["needs_add"])
        self.assertEqual(by_name["john"]["dob"], "01/02/1990")
        self.assertTrue(await _verify_member_dob(self._page, "Agent", "03/15/2001"))
        self.assertFalse(await _verify_member_dob(self._page, "john", "03/15/2001"))

        await self._page.evaluate("() => { document.body.dataset.fail = '1'; }")
        self.assertTrue(await _click_member_add_dob(self._page, "john", edit=True))
        self.assertFalse(
            await _fill_and_save_dob(self._page, "04/04/1991", "john")
        )
        john_saved = await self._page.locator("#john-saved").inner_text()
        self.assertEqual(john_saved.strip(), "01/02/1990")
        agent_saved = await self._page.locator("#agent-saved").inner_text()
        self.assertEqual(agent_saved.strip(), "03/15/2001")

    async def test_address_edit_stays_on_the_named_member(self) -> None:
        from pc_liveagent.app_stage import (
            _clear_member_address_selection,
            _click_member_address_action,
            _commit_member_address,
            _handle_address_step,
            _open_member_address_editor,
            _scrape_address_cards,
            _set_member_no_address,
        )
        from pc_liveagent.live_form import LiveFormSession

        await self._page.set_content(
            """
            <h1>Home address</h1>
            <p>Mailing address</p>
            <article id="agent" style="width:320px;display:block">
              <div style="display:flex;justify-content:space-between;align-items:center">
                <div>Agent</div>
                <button type="button" id="agent-edit" style="width:36px;height:36px;padding:0;border:0;background:transparent">
                  <svg width="20" height="20" viewBox="0 0 24 24"><path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L2.828 17.185a2 2 0 0 0-.578 1.153l-.34 2.724a1 1 0 0 0 1.177 1.177l2.724-.34a2 2 0 0 0 1.153-.578z"></path></svg>
                </button>
              </div>
              <div>Home address</div>
              <div id="agent-home">10 First St</div>
              <div>Mailing address</div>
              <div id="agent-mail">Same mailing address</div>
              <div id="agent-form" style="display:none">
                <label>Home Address</label>
                <div>
                  <input id="agent-home-input">
                  <button type="button" id="agent-home-clear" style="width:24px;height:24px;padding:0;border:0;background:transparent">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
                  </button>
                </div>
                <label>Apartment, unit, building</label>
                <input id="agent-home-unit">
                <label>Mailing Address</label>
                <div>
                  <input id="agent-mail-input">
                  <button type="button" id="agent-mail-clear" style="width:24px;height:24px;padding:0;border:0;background:transparent">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
                  </button>
                </div>
                <label>Apartment, unit, building, or PO Box</label>
                <input id="agent-mail-unit">
                <label><input id="agent-no-home" type="checkbox"> No Home Address</label>
                <label><input id="agent-no-mail" type="checkbox"> No Mailing Address</label>
                <button type="button" id="agent-cancel">Cancel</button>
                <button type="button" id="agent-save">Save</button>
              </div>
            </article>
            <article id="john" style="width:320px;display:block">
              <div>john</div>
              <div>Home address</div>
              <div id="john-home"></div>
              <div>Mailing address</div>
              <div id="john-mail">200 Oak Ave</div>
              <button type="button" id="john-add">Add</button>
              <div id="john-form" style="display:none">
                <label>Home Address</label>
                <div>
                  <input id="john-home-input">
                  <button type="button" id="john-home-clear" style="width:24px;height:24px;padding:0;border:0;background:transparent">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
                  </button>
                </div>
                <label>Apartment, unit, building</label>
                <input id="john-home-unit">
                <label>Mailing Address</label>
                <div>
                  <input id="john-mail-input">
                  <button type="button" id="john-mail-clear" style="width:24px;height:24px;padding:0;border:0;background:transparent">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
                  </button>
                </div>
                <label>Apartment, unit, building, or PO Box</label>
                <input id="john-mail-unit">
                <label><input id="john-no-home" type="checkbox"> No Home Address</label>
                <label><input id="john-no-mail" type="checkbox"> No Mailing Address</label>
                <p id="john-error"></p>
                <button type="button" id="john-cancel">Cancel</button>
                <button type="button" id="john-save">Save</button>
              </div>
            </article>
            <div id="suggestions" style="display:none">
              <div class="pac-item" role="option">999 Other Rd, Austin, TX 78701</div>
              <div class="pac-item" role="option">123 Main St, San Jose, CA 95112</div>
              <div class="pac-item" role="option">Newton Ave, Seattle, WA</div>
              <div class="pac-item" role="option">Newton Street Seattle, WA, USA</div>
            </div>
            <button type="button" id="next" disabled>Next</button>
            <script>
              window.clicks = [];
              function openCard(id) {
                window.clicks.push('open-' + id);
                document.getElementById(id + '-form').style.display = 'block';
                const opener = document.getElementById(id + '-edit') || document.getElementById(id + '-add');
                if (opener) opener.style.display = 'none';
              }
              document.getElementById('agent-edit').onclick = () => openCard('agent');
              document.getElementById('john-add').onclick = () => openCard('john');
              function wireClear(inputId, clearId) {
                document.getElementById(clearId).onclick = () => {
                  window.clicks.push('clear-' + inputId);
                  document.getElementById(inputId).value = '';
                };
              }
              wireClear('agent-home-input', 'agent-home-clear');
              wireClear('agent-mail-input', 'agent-mail-clear');
              wireClear('john-home-input', 'john-home-clear');
              wireClear('john-mail-input', 'john-mail-clear');
              // Pre-selected mailing chip style value so X must be clicked first.
              document.getElementById('agent-edit').addEventListener('click', () => {
                setTimeout(() => {
                  document.getElementById('agent-mail-input').value = 'Highway 50, Fallon, NV, USA';
                }, 0);
              });
              function showSuggestions(input) {
                const box = document.getElementById('suggestions');
                box.style.display = 'block';
                box.dataset.target = input.id;
              }
              for (const id of ['agent-home-input','agent-mail-input','john-home-input','john-mail-input']) {
                document.getElementById(id).addEventListener('input', (event) => showSuggestions(event.target));
              }
              for (const item of document.querySelectorAll('.pac-item')) {
                item.style.display = 'block';
                item.style.height = '24px';
                item.onclick = () => {
                  window.clicks.push('suggestion');
                  const id = document.getElementById('suggestions').dataset.target;
                  document.getElementById(id).value = item.innerText;
                  document.getElementById('suggestions').style.display = 'none';
                };
              }
              function saveCard(id) {
                window.clicks.push('save-' + id);
                const error = document.getElementById(id + '-error');
                if (document.body.dataset.fail === '1') {
                  if (error) error.textContent = 'Please enter a valid address';
                  return;
                }
                if (error) error.textContent = '';
                const home = document.getElementById(id + '-home-input').value;
                const mail = document.getElementById(id + '-mail-input').value;
                const unit = document.getElementById(id + '-home-unit').value;
                if (home) document.getElementById(id + '-home').textContent = unit ? home + ' unit ' + unit : home;
                if (mail) document.getElementById(id + '-mail').textContent = mail;
                document.getElementById(id + '-form').style.display = 'none';
              }
              document.getElementById('john-save').onclick = () => saveCard('john');
              document.getElementById('agent-save').onclick = () => saveCard('agent');
              document.getElementById('agent-cancel').onclick = () => {
                window.clicks.push('cancel-agent');
                document.getElementById('agent-home-input').value = '';
                document.getElementById('agent-form').style.display = 'none';
                document.getElementById('agent-edit').style.display = 'inline';
              };
            </script>
            """
        )
        found = await _scrape_address_cards(self._page)
        by_name = {card["name"]: card for card in found}
        self.assertEqual(by_name["Agent"]["home"], "10 First St")
        self.assertTrue(by_name["Agent"]["mailing_same"])
        self.assertFalse(by_name["Agent"]["needs_add"])
        self.assertTrue(by_name["john"]["needs_home"])
        self.assertFalse(by_name["john"]["needs_mailing"])

        await self._page.evaluate(
            """() => {
              openCard('john');
              document.getElementById('john-home-input').value = 'Old Home Rd';
            }"""
        )
        cleared = await _clear_member_address_selection(self._page, "john", "home")
        self.assertTrue(cleared.get("home"))
        self.assertEqual(
            await self._page.locator("#john-home-input").input_value(), ""
        )
        self.assertEqual(
            await self._page.locator("#john-mail-input").input_value(), ""
        )
        clicks = await self._page.evaluate("() => window.clicks.slice()")
        self.assertIn("clear-john-home-input", clicks)
        await self._page.evaluate(
            """() => {
              document.getElementById('john-form').style.display = 'none';
              document.getElementById('john-add').style.display = 'inline';
              window.clicks = [];
            }"""
        )

        saved = await _commit_member_address(
            self._page, "john", "home", "123 Main St", "4B"
        )
        self.assertTrue(saved.get("ok"), saved.get("error"))
        clicks = await self._page.evaluate("() => window.clicks.slice()")
        self.assertIn("open-john", clicks)
        self.assertNotIn("open-agent", clicks)
        self.assertIn("clear-john-home-input", clicks)
        self.assertIn("suggestion", clicks)
        # The matching suggestion is the San Jose row, not the first Austin row.
        self.assertIn("San Jose", (await self._page.locator("#john-home").inner_text()))
        self.assertEqual(
            (await self._page.locator("#john-home").inner_text()).strip(),
            "123 Main St, San Jose, CA 95112 unit 4B",
        )
        self.assertEqual(
            (await self._page.locator("#agent-home").inner_text()).strip(),
            "10 First St",
        )
        self.assertEqual(
            (await self._page.locator("#agent-mail").inner_text()).strip(),
            "Same mailing address",
        )
        self.assertEqual(await self._page.locator("#john-home-unit").input_value(), "4B")
        self.assertNotEqual(
            await self._page.locator("#john-home-unit").input_value(),
            "123 Main St",
        )

        opened_agent = await _open_member_address_editor(self._page, "Agent")
        self.assertTrue(opened_agent.get("ok"), opened_agent)
        self.assertTrue(opened_agent.get("icon_only"), opened_agent)
        await self._page.locator("#agent-home-input").fill("999 Other Rd")
        session = LiveFormSession(
            call_id="addr-dom",
            url="https://example.test",
            app_step="app_address_review",
        )
        session.filled["app_address_current"] = "Agent"
        session.filled["app_address_snapshot"] = (
            '{"name":"Agent","home":"10 First St","mailing":"Same mailing address"}'
        )
        cancelled = await _handle_address_step(
            "addr-dom", session, self._page, "cancel", "cancel", "app_address_review"
        )
        self.assertTrue(cancelled.get("ok"))
        self.assertEqual(
            (await self._page.locator("#agent-home").inner_text()).strip(),
            "10 First St",
        )

        await self._page.evaluate(
            """() => {
              document.body.dataset.fail = '1';
              document.getElementById('john-add').style.display = 'inline';
              document.getElementById('john-form').style.display = 'none';
            }"""
        )
        failed = await _commit_member_address(
            self._page, "john", "mailing", "123 Main St"
        )
        self.assertFalse(failed.get("ok"))
        self.assertEqual(failed.get("error"), "address_save_failed")
        self.assertEqual(
            (await self._page.locator("#agent-home").inner_text()).strip(),
            "10 First St",
        )

        await self._page.evaluate(
            """() => {
              document.body.dataset.fail = '';
              document.getElementById('john-add').style.display = 'inline';
              document.getElementById('john-form').style.display = 'none';
              window.clicks = [];
            }"""
        )
        newton = await _commit_member_address(
            self._page, "Agent", "home", "Newton Street, Seattle"
        )
        self.assertTrue(newton.get("ok"), newton.get("error"))
        self.assertIn(
            "Newton Street Seattle",
            (await self._page.locator("#agent-home").inner_text()),
        )
        self.assertEqual(
            (await self._page.locator("#john-mail").inner_text()).strip(),
            "200 Oak Ave",
        )
        newton_clicks = await self._page.evaluate("() => window.clicks.slice()")
        self.assertIn("open-agent", newton_clicks)
        self.assertNotIn("open-john", newton_clicks)

        await self._page.evaluate(
            """() => {
              document.getElementById('john-add').style.display = 'inline';
              document.getElementById('john-form').style.display = 'none';
            }"""
        )
        opened_john = await _open_member_address_editor(self._page, "john")
        self.assertTrue(opened_john.get("ok"), opened_john)
        self.assertTrue(await _set_member_no_address(self._page, "john", "mailing"))
        self.assertTrue(await self._page.locator("#john-no-mail").is_checked())
        self.assertFalse(await self._page.locator("#agent-no-mail").is_checked())
        self.assertTrue(await _click_member_address_action(self._page, "john", "save"))
        self.assertNotIn(
            "next",
            await self._page.evaluate("() => window.clicks.slice()"),
        )


    async def test_marital_cards_scope_pencil_radio_save_and_same_value(self) -> None:
        from pc_liveagent.app_stage import (
            _click_marital_action,
            _marital_target_card,
            _open_marital_editor,
            _scrape_marital_cards,
            _set_member_marital_status,
        )

        await self._page.set_content(
            """
            <h2>Current Marital Statuses</h2>
            <p>We've set marital statuses based on your household relationships.</p>
            <div id="cards"></div>
            <script>
              window.maritalClicks = [];
              const people = [
                ['Agent T.', 'Married'],
                ['john D.', 'Married'],
                ['John D.', 'Never Married'],
              ];
              const options = [
                'Never Married', 'Married', 'Separated', 'Divorced', 'Widowed',
                'Registered Domestic Partner', 'Common Law Marriage'
              ];
              function renderCard(index) {
                const [name, saved] = people[index];
                const card = document.createElement('article');
                card.id = 'card-' + index;
                card.style.cssText = 'width:330px;padding:12px;margin:8px;border:1px solid #ccc';
                card.innerHTML = `
                  <div class="header"><span>${name}</span>
                    <button class="pencil" style="width:36px;height:36px">
                      <svg><path d="M21.174 6.812a1 1 0 0 0-3.986-3.987"></path></svg>
                    </button>
                    <button class="cancel" style="display:none">Cancel</button>
                    <button class="save" disabled style="display:none">Save</button>
                  </div>
                  <div class="saved">${saved}</div>
                  <div class="form" style="display:none"></div>`;
                const form = card.querySelector('.form');
                for (const label of options) {
                  const row = document.createElement('label');
                  row.style.display = 'block';
                  row.innerHTML = `<div role="radio" aria-checked="${label === saved}"></div><span>${label}</span>`;
                  const radio = row.querySelector('[role=radio]');
                  radio.style.cssText = 'display:inline-block;width:16px;height:16px';
                  radio.onclick = () => {
                    for (const other of form.querySelectorAll('[role=radio]')) {
                      other.setAttribute('aria-checked', String(other === radio));
                    }
                    card.querySelector('.save').disabled = label === people[index][1];
                    card.dataset.draft = label;
                  };
                  form.appendChild(row);
                }
                const open = () => {
                  window.maritalClicks.push('open-' + index);
                  card.querySelector('.pencil').style.display = 'none';
                  card.querySelector('.saved').style.display = 'none';
                  card.querySelector('.cancel').style.display = 'inline';
                  card.querySelector('.save').style.display = 'inline';
                  card.querySelector('.form').style.display = 'block';
                };
                const close = () => {
                  card.querySelector('.pencil').style.display = 'inline';
                  card.querySelector('.saved').style.display = 'block';
                  card.querySelector('.cancel').style.display = 'none';
                  card.querySelector('.save').style.display = 'none';
                  card.querySelector('.form').style.display = 'none';
                };
                card.querySelector('.pencil').onclick = open;
                card.querySelector('.cancel').onclick = () => {
                  window.maritalClicks.push('cancel-' + index);
                  delete card.dataset.draft;
                  close();
                };
                card.querySelector('.save').onclick = () => {
                  window.maritalClicks.push('save-' + index);
                  people[index][1] = card.dataset.draft;
                  card.querySelector('.saved').textContent = people[index][1];
                  delete card.dataset.draft;
                  close();
                };
                return card;
              }
              people.forEach((_person, index) => {
                document.getElementById('cards').appendChild(renderCard(index));
              });
            </script>
            """
        )

        cards = await _scrape_marital_cards(self._page)
        self.assertEqual(
            [(row["name"], row["status"]) for row in cards],
            [
                ("Agent T.", "Married"),
                ("john D.", "Married"),
                ("John D.", "Never Married"),
            ],
        )
        target, clarify = _marital_target_card("John D", cards)
        self.assertIsNone(target)
        self.assertIn("2 people", clarify or "")
        target, clarify = _marital_target_card("second John D", cards)
        self.assertIsNone(clarify)
        self.assertEqual(target["index"], 2)

        same = await _set_member_marital_status(self._page, 0, "Married")
        self.assertTrue(same.get("ok"), same)
        self.assertFalse(same.get("changed"))
        clicks = await self._page.evaluate("() => window.maritalClicks.slice()")
        self.assertIn("open-0", clicks)
        self.assertIn("cancel-0", clicks)
        self.assertNotIn("save-0", clicks)

        changed = await _set_member_marital_status(self._page, 1, "Divorced")
        self.assertTrue(changed.get("ok"), changed)
        self.assertTrue(changed.get("changed"))
        self.assertEqual(changed["card"]["status"], "Divorced")
        clicks = await self._page.evaluate("() => window.maritalClicks.slice()")
        self.assertIn("save-1", clicks)

        opened = await _open_marital_editor(self._page, 2)
        self.assertTrue(opened.get("ok"), opened)
        self.assertTrue(await _click_marital_action(self._page, 2, "cancel"))

    async def test_circumstance_cards_select_condition_and_members(self) -> None:
        from pc_liveagent.app_stage import (
            _discover_circumstance_members,
            _select_circumstance_checkbox,
            _set_circumstance_member,
        )

        await self._page.set_content(
            """
            <h2>Other Statuses</h2>
            <p>Please select any situations that apply to household members.</p>
            <div id="cards"></div>
            <script>
              window.circClicks = [];
              const conditions = [
                'Needs help with daily activities',
                'Veteran or military member',
                'None of the above',
              ];
              const people = ['Agent T.', 'john D.', 'John D.'];
              function renderCard(label) {
                const card = document.createElement('article');
                card.style.cssText = 'width:420px;padding:12px;margin:8px;border:1px solid #ccc';
                card.dataset.checked = '0';
                const header = document.createElement('label');
                header.innerHTML = `<input type="checkbox"> ${label}`;
                const box = header.querySelector('input');
                const who = document.createElement('div');
                who.style.display = 'none';
                who.innerHTML = '<div>Who does this apply to?</div>';
                people.forEach((name, index) => {
                  const row = document.createElement('label');
                  row.style.display = 'block';
                  row.innerHTML = `<input type="checkbox" data-name="${name}" data-i="${index}"> ${name}`;
                  row.querySelector('input').onclick = (ev) => {
                    window.circClicks.push(label + ':' + name + ':' + index);
                  };
                  who.appendChild(row);
                });
                box.onchange = () => {
                  window.circClicks.push('cond:' + label);
                  who.style.display = box.checked ? 'block' : 'none';
                };
                card.appendChild(header);
                card.appendChild(who);
                return card;
              }
              conditions.forEach((label) => {
                document.getElementById('cards').appendChild(renderCard(label));
              });
            </script>
            """
        )

        selected = await _select_circumstance_checkbox(
            self._page, "Needs help with daily activities"
        )
        self.assertTrue(selected.get("ok"), selected)
        members = await _discover_circumstance_members(
            self._page, "Needs help with daily activities"
        )
        self.assertEqual(
            [row["label"] for row in members],
            ["Agent T.", "john D.", "John D."],
        )
        self.assertTrue(
            await _set_circumstance_member(
                self._page, "john D.", "Needs help with daily activities", dup_index=0
            )
        )
        self.assertTrue(
            await _set_circumstance_member(
                self._page, "John D.", "Needs help with daily activities", dup_index=1
            )
        )
        clicks = await self._page.evaluate("() => window.circClicks.slice()")
        self.assertIn("cond:Needs help with daily activities", clicks)
        self.assertTrue(
            any(item.startswith("Needs help with daily activities:john D.:1") for item in clicks)
        )
        self.assertTrue(
            any(item.startswith("Needs help with daily activities:John D.:2") for item in clicks)
        )

    async def test_childcare_rows_scope_yes_no_and_same_value(self) -> None:
        from pc_liveagent.app_stage import (
            _childcare_target_card,
            _scrape_childcare_cards,
            _set_member_childcare,
        )

        await self._page.set_content(
            """
            <h2>Do the following household members live with and care for any
            children under the age of 19?</h2>
            <div id="rows"></div>
            <script>
              window.childcareClicks = [];
              const people = [
                ['Agent T.', 'Yes'],
                ['john D.', 'Yes'],
                ['John D.', 'No'],
              ];
              function renderRow(index) {
                const [name, saved] = people[index];
                const row = document.createElement('article');
                row.id = 'row-' + index;
                row.style.cssText = 'width:420px;padding:12px;margin:8px;border:1px solid #ccc;display:flex;justify-content:space-between;align-items:center';
                row.innerHTML = `
                  <span>${name}</span>
                  <div>
                    <label><div role="radio" data-val="Yes" aria-checked="${saved === 'Yes'}"></div> Yes</label>
                    <label><div role="radio" data-val="No" aria-checked="${saved === 'No'}"></div> No</label>
                  </div>`;
                for (const radio of row.querySelectorAll('[role=radio]')) {
                  radio.style.cssText = 'display:inline-block;width:16px;height:16px';
                  radio.onclick = () => {
                    window.childcareClicks.push(radio.dataset.val + '-' + index);
                    for (const other of row.querySelectorAll('[role=radio]')) {
                      other.setAttribute('aria-checked', String(other === radio));
                    }
                    people[index][1] = radio.dataset.val;
                  };
                }
                return row;
              }
              people.forEach((_person, index) => {
                document.getElementById('rows').appendChild(renderRow(index));
              });
            </script>
            """
        )

        cards = await _scrape_childcare_cards(self._page)
        self.assertEqual(
            [(row["name"], row["answer"]) for row in cards],
            [
                ("Agent T.", "Yes"),
                ("john D.", "Yes"),
                ("John D.", "No"),
            ],
        )
        target, clarify = _childcare_target_card("John D", cards)
        self.assertIsNone(target)
        self.assertIn("2 people", clarify or "")
        target, clarify = _childcare_target_card("second John D", cards)
        self.assertIsNone(clarify)
        self.assertEqual(target["index"], 2)

        same = await _set_member_childcare(self._page, 0, "Yes")
        self.assertTrue(same.get("ok"), same)
        self.assertFalse(same.get("changed"))
        clicks = await self._page.evaluate("() => window.childcareClicks.slice()")
        self.assertEqual(clicks, [])

        changed = await _set_member_childcare(self._page, 1, "No")
        self.assertTrue(changed.get("ok"), changed)
        self.assertTrue(changed.get("changed"))
        self.assertEqual(changed["card"]["answer"], "No")
        clicks = await self._page.evaluate("() => window.childcareClicks.slice()")
        self.assertIn("No-1", clicks)

    async def test_citizenship_cards_ignore_ancestor_disabled_class(self) -> None:
        from pc_liveagent.app_stage import _click_yes_no, _stage_e_card_state

        await self._page.set_content(
            """
            <section class="Mui-disabled" style="padding:16px">
              <div style="padding:12px">Yes — Everyone listed is a U.S. Citizen</div>
              <div id="no-card" style="padding:12px">No — Some or all are not U.S. Citizens</div>
              <div>Prefer not to answer</div>
              <div>U.S. National</div>
              <div>Satisfactory immigration status</div>
            </section>
            """
        )
        await self._page.evaluate(
            """() => {
              const el = document.getElementById('no-card');
              el.addEventListener('click', () => { el.dataset.clicked = '1'; });
            }"""
        )
        state = await _stage_e_card_state(
            self._page, "No — Some or all are not U.S. Citizens"
        )
        self.assertTrue(state.get("found"))
        self.assertFalse(state.get("disabled"))
        with (
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(
                    return_value=[
                        "Yes — Everyone listed is a U.S. Citizen",
                        "No — Some or all are not U.S. Citizens",
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage.immigration_member_radios_visible",
                new=AsyncMock(return_value=True),
            ),
        ):
            selected = await _click_yes_no(self._page, False)
        self.assertIn("not U.S. Citizens", selected)
        clicked = await self._page.evaluate(
            "() => document.getElementById('no-card').dataset.clicked || ''"
        )
        self.assertEqual(clicked, "1")

    async def test_citizenship_card_self_disabled_is_not_clicked(self) -> None:
        from pc_liveagent.app_stage import _click_yes_no, _stage_e_card_state

        await self._page.set_content(
            """
            <section style="padding:16px">
              <div id="yes-card" aria-disabled="true" style="padding:12px">Yes — Everyone listed is a U.S. Citizen</div>
              <div id="no-card" style="padding:12px">No — Some or all are not U.S. Citizens</div>
            </section>
            """
        )
        await self._page.evaluate(
            """() => {
              for (const id of ['yes-card', 'no-card']) {
                const el = document.getElementById(id);
                el.addEventListener('click', () => { el.dataset.clicked = '1'; });
              }
            }"""
        )
        yes_state = await _stage_e_card_state(
            self._page, "Yes — Everyone listed is a U.S. Citizen"
        )
        self.assertTrue(yes_state.get("disabled"))
        with (
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(
                    return_value=[
                        "Yes — Everyone listed is a U.S. Citizen",
                        "No — Some or all are not U.S. Citizens",
                    ]
                ),
            ),
        ):
            with self.assertRaises(RuntimeError):
                await _click_yes_no(self._page, True)
        clicked = await self._page.evaluate(
            "() => document.getElementById('yes-card').dataset.clicked || ''"
        )
        self.assertEqual(clicked, "")

if __name__ == "__main__":
    unittest.main()
