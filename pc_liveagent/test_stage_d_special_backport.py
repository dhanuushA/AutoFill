"""Focused coverage for the Stage D special-condition backport."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from pc_liveagent.live_form import LiveFormSession


class StageDSpecialBackportTests(unittest.IsolatedAsyncioTestCase):
    async def test_begin_eligibility_clears_prior_special_state(self) -> None:
        from pc_liveagent.live_form import _begin_eligibility

        session = LiveFormSession(call_id="fresh", url="https://example.test")
        session.filled["special_conditions"] = "Someone is blind"
        session.filled["special_assignment_pairs"] = "Someone is blind>>>Agent T."
        session.last_step_answer = ("special", "blind")
        session.last_result = {"ok": True}
        with (
            patch("pc_liveagent.config.APPLICATION_ONLY_TEST_MODE", False),
            patch("pc_liveagent.config.APPLICATION_AFTER_SIGNIN", False),
            patch("pc_liveagent.config.APPLICATION_TEST_MODE", False),
            patch(
                "pc_liveagent.live_form._open_household_eligibility",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.live_form._reset_eligibility_to_first_question",
                new=AsyncMock(return_value=True),
            ),
        ):
            spoken = await _begin_eligibility(session, AsyncMock())

        self.assertTrue(spoken.startswith("ASK: "))
        self.assertNotIn("special_conditions", session.filled)
        self.assertNotIn("special_assignment_pairs", session.filled)
        self.assertIsNone(session.last_step_answer)
        self.assertIsNone(session.last_result)

    async def test_stale_special_ui_clear_uses_exact_control(self) -> None:
        from pc_liveagent.live_form import _clear_stale_special_condition_checkboxes

        states = {
            "Someone is blind": [
                {"found": True, "checked": True, "source": "input"},
                {"found": True, "checked": False, "source": "input"},
            ]
        }

        async def state(_page, label):
            queue = states.get(label)
            if not queue:
                return {"found": True, "checked": False, "source": "input"}
            return queue.pop(0)

        click = AsyncMock()
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._special_condition_checkbox_state",
                new=AsyncMock(side_effect=state),
            ),
            patch(
                "pc_liveagent.live_form._click_special_condition_control",
                new=click,
            ),
        ):
            cleared = await _clear_stale_special_condition_checkboxes(page)

        self.assertEqual(cleared, 1)
        click.assert_awaited_once_with(page, "Someone is blind")

    def test_baby_count_and_named_special_more_are_deterministic(self) -> None:
        from pc_liveagent.answer_parse import is_fast_path_resolve, resolve_answer

        babies = resolve_answer("babies_count", "we are expecting triplets")
        self.assertEqual(babies.answer, "3")
        self.assertTrue(is_fast_path_resolve(babies))

        condition = resolve_answer("special_more", "Disabled")
        self.assertEqual(condition.answer, "Disabled")
        self.assertEqual(condition.detail, "special_more_condition")
        self.assertTrue(is_fast_path_resolve(condition))

    async def test_pregnant_member_opens_details_and_asks_member_count(self) -> None:
        from pc_liveagent.live_form import _submit_eligibility_answer

        session = LiveFormSession(
            call_id="pregnancy",
            url="https://example.test",
            elig_step="special_person",
        )
        session.filled["special_conditions"] = "Someone is pregnant"
        session.filled["special_active_conditions"] = "Someone is pregnant"
        session.filled["special_person_queue"] = "Someone is pregnant"

        async def assign(_page, _answer, current):
            current.filled["special_person_assigned"] = "Agent T."
            current.filled["special_last_assigned_labels"] = "Agent T."

        page = AsyncMock()
        open_details = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                new=AsyncMock(return_value="special"),
            ),
            patch(
                "pc_liveagent.live_form._assign_special_person_member",
                new=AsyncMock(side_effect=assign),
            ),
            patch(
                "pc_liveagent.live_form._pregnancy_details_state",
                new=AsyncMock(return_value={"open": False}),
            ),
            patch(
                "pc_liveagent.live_form._open_pregnancy_details_for_member",
                new=open_details,
            ),
        ):
            result = await _submit_eligibility_answer(
                session.call_id, session, page, "Agent T.", 0.0
            )

        self.assertEqual(result.get("step"), "babies_count")
        open_details.assert_awaited_once_with(page, "Agent T.")
        self.assertIn("Agent T.", result.get("spoken") or "")


if __name__ == "__main__":
    unittest.main()
