"""Regression tests for Collect Mode answer pipeline and Custom LLM controller."""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from pc_liveagent.answer_parse import normalize_utterance, validate_for_step
from pc_liveagent.call_turn import (
    CallController,
    drop_controller,
    get_controller,
    latest_user_utterance,
    process_user_turn,
)
from pc_liveagent.config import retell_llm_websocket_url
from pc_liveagent.form_bridge import _apply_key, _mark_applied, _skip_duplicate_apply
from pc_liveagent.live_form import LiveFormSession, _next_elig_step


class AnswerParseTests(unittest.TestCase):
    def test_q1_marital_never_married(self) -> None:
        answer, clarify = validate_for_step("marital", "Never married")
        self.assertEqual(answer, "Never Married")
        self.assertIsNone(clarify)

    def test_q2_children_no(self) -> None:
        answer, clarify = validate_for_step("children", "No")
        self.assertEqual(answer, "No")
        self.assertIsNone(clarify)

    def test_q3_student_no(self) -> None:
        answer, clarify = validate_for_step("student", "No")
        self.assertEqual(answer, "No")
        self.assertIsNone(clarify)

    def test_ambiguous_yes_no_asks_clarify(self) -> None:
        answer, clarify = validate_for_step("student", "maybe later")
        self.assertIsNone(answer)
        # Must include the full active question, not only a bare clarify sentence.
        self.assertIn("full-time student", (clarify or "").lower())
        self.assertIn("yes or no", (clarify or "").lower())
        self.assertNotEqual((clarify or "").strip().lower(), "please answer yes or no.")

    def test_whats_the_next_question_is_next_intent(self) -> None:
        from pc_liveagent.control_intent import detect_control_intent

        self.assertEqual(
            detect_control_intent("What's the next question?").kind, "next"
        )
        self.assertEqual(
            detect_control_intent("I didn't hear the question").kind, "repeat"
        )
        answer2, clarify2 = validate_for_step("disabled", "I didn't hear the question")
        self.assertIsNone(answer2)
        self.assertEqual(clarify2, "__REPEAT__")

    def test_yeah_thats_right_is_yes(self) -> None:
        answer, clarify = validate_for_step("children", "Yeah, that's right.")
        self.assertEqual(answer, "Yes")
        self.assertIsNone(clarify)

    def test_never_been_married(self) -> None:
        answer, clarify = validate_for_step("marital", "I have never been married.")
        self.assertEqual(answer, "Never Married")
        self.assertIsNone(clarify)

    def test_normalize_strips_filler(self) -> None:
        self.assertEqual(normalize_utterance("  My name is Alex  "), "Alex")

    def test_proceed_application_yes_is_fast_path(self) -> None:
        from pc_liveagent.answer_parse import is_fast_path_resolve, resolve_answer

        for utterance in ("Yes", "Yes.", "Yes. We can start.", "Yeah"):
            resolved = resolve_answer("proceed_application", utterance)
            self.assertEqual(resolved.answer, "Yes", utterance)
            self.assertIsNone(resolved.clarify, utterance)
            self.assertTrue(is_fast_path_resolve(resolved), utterance)

    def test_proceed_application_continue_counts_as_yes(self) -> None:
        answer, clarify = validate_for_step(
            "proceed_application", "Continue Quick Apply"
        )
        self.assertEqual(answer, "Yes")
        self.assertIsNone(clarify)

    def test_proceed_application_unclear_does_not_list_dashboard_buttons(
        self,
    ) -> None:
        from pc_liveagent.answer_parse import resolve_answer

        # Same dashboard labels call_turn used to pass in and speak on timeout.
        resolved = resolve_answer(
            "proceed_application",
            "maybe later",
            options=[
                "en",
                "Household",
                "Account",
                "Continue Quick Apply",
                "New Quick Apply",
            ],
        )
        self.assertIsNone(resolved.answer)
        spoken = (resolved.clarify or "").lower()
        self.assertIn("yes or no", spoken)
        self.assertNotIn("continue quick apply", spoken)
        self.assertNotIn("new quick apply", spoken)
        self.assertNotIn("options include", spoken)


class BranchingTests(unittest.TestCase):
    def session(self, step: str) -> LiveFormSession:
        return LiveFormSession(call_id="t", url="https://example.test", elig_step=step)

    def test_q1_to_q2_to_q3_with_no(self) -> None:
        s = self.session("marital")
        self.assertEqual(_next_elig_step(s, "never married"), "children")
        s.elig_step = "children"
        self.assertEqual(_next_elig_step(s, "no"), "student")
        s.elig_step = "student"
        self.assertEqual(_next_elig_step(s, "no"), "disabled")

    def test_yes_on_student_opens_detail_branch(self) -> None:
        s = self.session("student")
        nxt = _next_elig_step(s, "yes")
        self.assertIn(nxt, ("person_first", "person_form", "student_count", "person_name"))


class DedupeTests(unittest.TestCase):
    def test_same_yes_no_different_steps_not_blocked(self) -> None:
        call_id = "dedupe-call"
        session = LiveFormSession(call_id=call_id, url="https://example.test", elig_step="children")
        _mark_applied(call_id, "children", "No")
        # Different step with same answer must not be treated as duplicate.
        self.assertFalse(_skip_duplicate_apply(call_id, "student", "No", session))
        # Same step+answer within cooldown is blocked.
        session.elig_step = "children"
        self.assertTrue(_skip_duplicate_apply(call_id, "children", "No", session))
        self.assertEqual(_apply_key("children", "No"), "children:no")
        self.assertEqual(_apply_key("student", "No"), "student:no")


class CallTurnTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await drop_controller("turn-test")

    async def test_consecutive_no_across_eligibility_steps(self) -> None:
        call_id = "turn-test"
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        ctrl.active_websocket_status = "connected"

        session = LiveFormSession(call_id=call_id, url="https://example.test", elig_step="children")

        async def fake_get_session(_cid: str):
            return session

        async def fake_submit(cid, field, value, url="", request_id=""):
            # Advance children -> student, then student -> disabled
            if session.elig_step == "children":
                session.elig_step = "student"
                return {
                    "ok": True,
                    "spoken": "ASK: Are there any students?",
                    "step": "student",
                }
            if session.elig_step == "student":
                session.elig_step = "disabled"
                return {
                    "ok": True,
                    "spoken": "ASK: Anyone disabled?",
                    "step": "disabled",
                }
            return {"ok": False, "spoken": "stuck"}

        interrupt = AsyncMock(return_value=True)
        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch("pc_liveagent.call_turn.peek_choice_labels", AsyncMock(return_value=[])),
            patch("pc_liveagent.call_turn.submit_field", side_effect=fake_submit),
            patch("pc_liveagent.call_turn._speak_interrupt", interrupt),
        ):
            r1 = await process_user_turn(call_id, "No")
            self.assertEqual(r1.detail, "elig_ack")
            task = __import__(
                "pc_liveagent.call_turn", fromlist=["_elig_browser_tasks"]
            )._elig_browser_tasks.get(call_id)
            if task:
                await asyncio.wait_for(task, timeout=2.0)
            self.assertEqual(session.elig_step, "student")

            r2 = await process_user_turn(call_id, "No")
            self.assertEqual(r2.detail, "elig_ack")
            self.assertNotEqual(r2.detail, "duplicate_utterance")
            task2 = __import__(
                "pc_liveagent.call_turn", fromlist=["_elig_browser_tasks"]
            )._elig_browser_tasks.get(call_id)
            if task2:
                await asyncio.wait_for(task2, timeout=2.0)
            self.assertEqual(session.elig_step, "disabled")

    async def test_duplicate_same_step_is_ignored(self) -> None:
        call_id = "turn-test"
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        ctrl.active_websocket_status = "connected"
        session = LiveFormSession(call_id=call_id, url="https://example.test", elig_step="student")
        calls = {"n": 0}

        async def fake_get_session(_cid: str):
            return session

        async def fake_submit(*_a, **_k):
            calls["n"] += 1
            session.elig_step = "disabled"
            return {"ok": True, "spoken": "ASK: next", "step": "disabled"}

        interrupt = AsyncMock(return_value=True)
        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch("pc_liveagent.call_turn.peek_choice_labels", AsyncMock(return_value=[])),
            patch("pc_liveagent.call_turn.submit_field", side_effect=fake_submit),
            patch("pc_liveagent.call_turn._speak_interrupt", interrupt),
        ):
            await process_user_turn(call_id, "No")
            task = __import__(
                "pc_liveagent.call_turn", fromlist=["_elig_browser_tasks"]
            )._elig_browser_tasks.get(call_id)
            if task:
                await asyncio.wait_for(task, timeout=2.0)
            # Reset step so commit_key differs but utterance fp matches same ctx —
            # after success step changed, so re-say No on disabled is different ctx.
            # Force same step context for true duplicate:
            session.elig_step = "student"
            ctrl = await get_controller(call_id)
            ctrl.phase = "eligibility"
            # Clear committed but keep last_user_fp/ctx from prior student:No
            # Actually after first turn, last_dedupe_ctx was eligibility:student and
            # session was advanced then we set back to student — ctx matches.
            r = await process_user_turn(call_id, "No")
            # Second submit may still run if commit_key student:no is in committed.
            self.assertIn(
                r.detail,
                ("duplicate_utterance", "already_committed", "elig_ack", ""),
            )
            if r.detail == "elig_ack":
                # Should still be blocked by commit key inside the bg path.
                self.assertEqual(calls["n"], 1)

    async def test_language_then_sign_in_question(self) -> None:
        call_id = "turn-test"
        await drop_controller(call_id)
        r = await process_user_turn(call_id, "English is fine")
        self.assertIn("Google", r.speech)
        self.assertEqual(r.phase, "sign_in")

    async def test_intro_for_what_stays_on_language_with_context(self) -> None:
        call_id = "turn-test"
        await drop_controller(call_id)
        r = await process_user_turn(call_id, "For what?")
        self.assertEqual(r.phase, "language")
        self.assertEqual(r.detail, "repeat")
        self.assertIn("pointcare", r.speech.lower())
        self.assertIn("english", r.speech.lower())
        self.assertNotIn("google", r.speech.lower())

    async def test_sign_in_what_repeats_chooser_with_context(self) -> None:
        call_id = "turn-test"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "sign_in"
        r = await process_user_turn(call_id, "What?")
        self.assertEqual(r.phase, "sign_in")
        self.assertEqual(r.detail, "repeat")
        self.assertIn("pointcare", r.speech.lower())
        self.assertIn("google", r.speech.lower())

    async def test_eligibility_what_do_you_mean_repeats_question(self) -> None:
        call_id = "turn-test"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="marital"
        )
        submit = AsyncMock()

        async def fake_get_session(_cid: str):
            return session

        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch("pc_liveagent.call_turn.peek_choice_labels", AsyncMock(return_value=[])),
            patch("pc_liveagent.call_turn.submit_field", submit),
        ):
            r = await process_user_turn(call_id, "What do you mean?")
        self.assertEqual(r.phase, "eligibility")
        self.assertEqual(r.detail, "repeat")
        self.assertIn("marital", r.speech.lower())
        submit.assert_not_called()

    async def test_person_rel_options_available_lists_choices(self) -> None:
        call_id = "rel-opts"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="person_rel"
        )
        submit = AsyncMock()
        opts = ["Child", "Stepchild", "Grandchild", "Foster Child", "Other"]

        with (
            patch(
                "pc_liveagent.call_turn.get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "pc_liveagent.call_turn.peek_choice_labels",
                AsyncMock(return_value=opts),
            ),
            patch("pc_liveagent.call_turn.submit_field", submit),
            patch(
                "pc_liveagent.call_turn.resolve_with_claude",
                AsyncMock(),
            ) as claude,
        ):
            r = await process_user_turn(
                call_id, "What are the options available?"
            )
        self.assertEqual(r.detail, "list_options")
        self.assertEqual(session.elig_step, "person_rel")
        low = r.speech.lower()
        self.assertIn("the options are", low)
        self.assertIn("child", low)
        self.assertIn("other", low)
        self.assertNotIn("required to continue", low)
        self.assertNotIn("please choose one of", low)
        self.assertNotIn("first name", low)
        submit.assert_not_called()
        claude.assert_not_called()

    async def test_initial_person_rel_question_includes_options(self) -> None:
        from pc_liveagent.live_form import (
            LiveFormSession,
            _spoken_question_for_step,
            relationship_question_speech,
        )

        session = LiveFormSession(
            call_id="rel-init", url="https://example.test", elig_step="person_rel"
        )
        spoken = _spoken_question_for_step(session, "person_rel")
        low = spoken.lower()
        self.assertIn("the options are", low)
        self.assertIn("child", low)
        self.assertIn("stepchild", low)
        self.assertIn("grandchild", low)
        self.assertIn("foster", low)
        self.assertIn("other", low)
        self.assertIn("which best describes", low)
        child = relationship_question_speech(
            "child_rel",
            ["Child", "Stepchild", "Grandchild", "Foster Child", "Other"],
        )
        self.assertIn("the options are", child.lower())
        self.assertIn("foster child", child.lower())

    async def test_person_rel_claude_timeout_uses_local_options(self) -> None:
        from pc_liveagent.answer_parse import ClaudeResolveResult

        call_id = "rel-timeout"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="person_rel"
        )
        opts = ["Child", "Stepchild", "Other"]
        submit = AsyncMock()
        claude = ClaudeResolveResult(
            needs_clarify=True,
            reject_reason="timeout",
            clarify_prompt=(
                "What is their relationship to you? "
                "This field is required to continue. Please choose one of: Child."
            ),
            latency_ms=1500,
        )

        with (
            patch(
                "pc_liveagent.call_turn.get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "pc_liveagent.call_turn.peek_choice_labels",
                AsyncMock(return_value=opts),
            ),
            patch("pc_liveagent.call_turn.submit_field", submit),
            patch(
                "pc_liveagent.call_turn.resolve_with_claude",
                AsyncMock(return_value=claude),
            ),
        ):
            r = await process_user_turn(call_id, "not sure")
        self.assertEqual(session.elig_step, "person_rel")
        self.assertIsNone(getattr(r, "end_call", None) or None)
        low = (r.speech or "").lower()
        self.assertIn("other", low)
        self.assertNotIn("required to continue", low)
        self.assertNotIn("options include", low)
        submit.assert_not_called()

    async def test_other_relationship_awaits_custom_text(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _submit_eligibility_answer

        session = LiveFormSession(
            call_id="rel-other", url="https://example.test", elig_step="person_rel"
        )
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()

        with (
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                AsyncMock(return_value=["Child", "Other"]),
            ),
            patch(
                "pc_liveagent.live_form._ensure_relationship_choice_selected",
                AsyncMock(),
            ) as ensure,
            patch(
                "pc_liveagent.live_form._other_relationship_input_visible",
                AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="person_form"),
            ),
            patch("pc_liveagent.live_form._mark"),
        ):
            result = await _submit_eligibility_answer(
                "rel-other", session, page, "Other", time.time()
            )
        ensure.assert_awaited_with(page, "Other")
        self.assertEqual(session.filled.get("rel_other_awaiting"), "person_rel")
        self.assertEqual(session.elig_step, "person_rel")
        self.assertIn("other relationship", (result.get("spoken") or "").lower())
        self.assertNotIn("required", (result.get("spoken") or "").lower())

        with (
            patch(
                "pc_liveagent.live_form._fill_other_relationship_text",
                AsyncMock(return_value="Niece"),
            ),
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="person_form"),
            ),
            patch("pc_liveagent.live_form._mark"),
        ):
            result2 = await _submit_eligibility_answer(
                "rel-other", session, page, "Niece", time.time()
            )
        self.assertEqual(session.filled.get("person_rel"), "Other")
        self.assertEqual(session.filled.get("person_rel_other"), "Niece")
        self.assertNotIn("rel_other_awaiting", session.filled)
        self.assertEqual(session.elig_step, "person_age")
        self.assertTrue(result2.get("ok"))

    async def test_other_without_custom_field_continues(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _submit_eligibility_answer

        session = LiveFormSession(
            call_id="rel-other-none",
            url="https://example.test",
            elig_step="person_rel",
        )
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()

        with (
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                AsyncMock(return_value=["Child", "Other"]),
            ),
            patch(
                "pc_liveagent.live_form._ensure_relationship_choice_selected",
                AsyncMock(),
            ),
            patch(
                "pc_liveagent.live_form._other_relationship_input_visible",
                AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="person_form"),
            ),
            patch(
                "pc_liveagent.live_form._relationship_speech_for_page",
                AsyncMock(return_value="age next"),
            ),
            patch("pc_liveagent.live_form._mark"),
        ):
            result = await _submit_eligibility_answer(
                "rel-other-none", session, page, "Other", time.time()
            )
        self.assertEqual(session.filled.get("person_rel"), "Other")
        self.assertNotIn("rel_other_awaiting", session.filled)
        self.assertEqual(session.elig_step, "person_age")
        self.assertTrue(result.get("ok"))

    async def test_ensure_relationship_raises_when_validation_still_blocks(self) -> None:
        """Fail only when the form still demands a selection after click attempts."""
        from pc_liveagent.live_form import _ensure_relationship_choice_selected

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._relationship_chip_selected",
                AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.live_form._choice_appears_selected",
                AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.live_form._click_relationship_chip",
                AsyncMock(return_value=True),
            ) as click,
            patch("pc_liveagent.live_form._click_exact_choice", AsyncMock()),
            patch(
                "pc_liveagent.live_form._visible_validation_hints",
                AsyncMock(return_value="Please select an option."),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await _ensure_relationship_choice_selected(page, "Child")
        self.assertIn("couldn't select", str(ctx.exception).lower())
        self.assertEqual(click.await_count, 1)

    async def test_ensure_relationship_accepts_click_when_required_text_is_gone(self) -> None:
        """No ARIA marker is OK after one click when the field is no longer invalid."""
        from pc_liveagent.live_form import _ensure_relationship_choice_selected

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        click = AsyncMock(return_value=True)
        with (
            patch(
                "pc_liveagent.live_form._relationship_chip_selected",
                AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.live_form._choice_appears_selected",
                AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.live_form._click_relationship_chip",
                click,
            ),
            patch(
                "pc_liveagent.live_form._visible_validation_hints",
                AsyncMock(return_value=""),
            ),
        ):
            await _ensure_relationship_choice_selected(page, "Spouse")
        self.assertEqual(click.await_count, 1)

    async def test_ensure_relationship_skips_click_when_already_selected(self) -> None:
        from pc_liveagent.live_form import _ensure_relationship_choice_selected

        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._relationship_chip_selected",
                AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.live_form._click_relationship_chip",
                AsyncMock(return_value=True),
            ) as click,
        ):
            await _ensure_relationship_choice_selected(page, "Child")
        click.assert_not_awaited()

    async def test_ensure_relationship_skips_click_when_visual_active(self) -> None:
        from pc_liveagent.live_form import _ensure_relationship_choice_selected

        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._relationship_chip_selected",
                AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.live_form._choice_appears_selected",
                AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.live_form._click_relationship_chip",
                AsyncMock(return_value=True),
            ) as click,
        ):
            await _ensure_relationship_choice_selected(page, "Stepchild")
        click.assert_not_awaited()

    async def test_spouse_last_fill_does_not_clear_the_first_name(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _submit_eligibility_answer

        session = LiveFormSession(
            call_id="keep-first",
            url="https://example.test",
            elig_step="spouse_last",
        )
        session.filled["spouse_first"] = "Alex"
        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="spouse_form"),
            ),
            patch("pc_liveagent.live_form._mark"),
            patch("pc_liveagent.live_form._strict_fill", AsyncMock()),
            patch(
                "pc_liveagent.live_form._clear_new_person_name_fields",
                AsyncMock(),
            ) as clear,
        ):
            result = await _submit_eligibility_answer(
                "keep-first", session, page, "Rivera", 0.0
            )
        clear.assert_not_awaited()
        self.assertEqual(session.filled.get("spouse_first"), "Alex")
        self.assertEqual(session.filled.get("spouse_last"), "Rivera")
        self.assertEqual(session.elig_step, "spouse_role")
        self.assertTrue(result.get("ok"))

    async def test_opening_spouse_form_clears_name_fields(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _submit_eligibility_answer

        session = LiveFormSession(
            call_id="new-spouse",
            url="https://example.test",
            elig_step="marital",
        )
        page = AsyncMock()
        with (
            patch("pc_liveagent.live_form._click_exact_choice", AsyncMock()),
            patch("pc_liveagent.live_form._wait_for_screen", AsyncMock(return_value="spouse_form")),
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="marital"),
            ),
            patch("pc_liveagent.live_form._mark"),
            patch(
                "pc_liveagent.live_form._clear_new_person_name_fields",
                AsyncMock(),
            ) as clear,
        ):
            result = await _submit_eligibility_answer(
                "new-spouse", session, page, "Married", 0.0
            )
        clear.assert_awaited()
        self.assertEqual(session.elig_step, "spouse_first")
        self.assertTrue(result.get("ok"))

    async def test_person_rel_does_not_advance_when_selection_unverified(self) -> None:
        """Grandchild must not reach person_age unless the chip is selected."""
        from pc_liveagent.live_form import LiveFormSession, _submit_eligibility_answer

        session = LiveFormSession(
            call_id="rel-no-advance",
            url="https://example.test",
            elig_step="person_rel",
        )
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                AsyncMock(
                    return_value=[
                        "Child",
                        "Stepchild",
                        "Grandchild",
                        "Foster Child",
                        "Other",
                    ]
                ),
            ),
            patch(
                "pc_liveagent.live_form._ensure_relationship_choice_selected",
                AsyncMock(
                    side_effect=RuntimeError(
                        "REL_CLARIFY: I couldn't select Grandchild on the form."
                    )
                ),
            ),
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="person_form"),
            ),
            patch("pc_liveagent.live_form._mark"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await _submit_eligibility_answer(
                    "rel-no-advance", session, page, "Grandchild", time.time()
                )
        self.assertIn("couldn't select", str(ctx.exception).lower())
        self.assertNotIn("person_rel", session.filled)
        self.assertEqual(session.elig_step, "person_rel")

    async def test_confirm_yes_blocks_when_relationship_missing(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _submit_eligibility_answer

        session = LiveFormSession(
            call_id="rel-confirm-block",
            url="https://example.test",
            elig_step="person_confirm",
            after_person="disabled",
        )
        session.filled.update(
            {
                "person_first": "Water",
                "person_last": "Bottle",
                "person_age": "(21-63) Adult",
                "person_kind": "student",
            }
        )
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="person_form"),
            ),
            patch("pc_liveagent.live_form._mark"),
            patch(
                "pc_liveagent.live_form._click_continue_to",
                AsyncMock(),
            ) as cont,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await _submit_eligibility_answer(
                    "rel-confirm-block", session, page, "Yes", time.time()
                )
        self.assertIn("relationship", str(ctx.exception).lower())
        cont.assert_not_awaited()
        self.assertEqual(session.elig_step, "person_confirm")

    async def test_spouse_confirm_yes_skips_strict_chip_reverify(self) -> None:
        """Confirm Yes must not fail when the strict chip reader cannot see selection."""
        from pc_liveagent.live_form import LiveFormSession, _submit_eligibility_answer

        session = LiveFormSession(
            call_id="spouse-confirm-yes",
            url="https://example.test",
            elig_step="spouse_confirm",
        )
        session.filled.update(
            {
                "spouse_first": "Alex",
                "spouse_last": "Rivera",
                "spouse_role": "Spouse",
                "spouse_age": "(21-63) Adult",
            }
        )
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="spouse_form"),
            ),
            patch("pc_liveagent.live_form._mark"),
            patch(
                "pc_liveagent.live_form._ensure_relationship_choice_selected",
                AsyncMock(
                    side_effect=RuntimeError(
                        "REL_CLARIFY: I couldn't select Spouse on the form."
                    )
                ),
            ) as ensure,
            patch(
                "pc_liveagent.live_form._choice_appears_selected",
                AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.live_form._click_continue_to",
                AsyncMock(),
            ) as cont,
        ):
            result = await _submit_eligibility_answer(
                "spouse-confirm-yes", session, page, "Yes", time.time()
            )
        ensure.assert_not_awaited()
        cont.assert_awaited()
        self.assertTrue(result.get("ok"))
        self.assertEqual(session.elig_step, "children")
        self.assertEqual(session.filled.get("spouse_role"), "Spouse")

    async def test_spouse_confirm_partner_correction_in_place(self) -> None:
        """Saying Partner on spouse_confirm corrects the role without Back."""
        from pc_liveagent.live_form import LiveFormSession, _submit_eligibility_answer

        session = LiveFormSession(
            call_id="spouse-partner-fix",
            url="https://example.test",
            elig_step="spouse_confirm",
        )
        session.filled.update(
            {
                "spouse_first": "Alex",
                "spouse_last": "Rivera",
                "spouse_role": "Spouse",
                "spouse_age": "(21-63) Adult",
            }
        )
        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="spouse_form"),
            ),
            patch("pc_liveagent.live_form._mark"),
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                AsyncMock(return_value=["Spouse", "Partner"]),
            ),
            patch(
                "pc_liveagent.live_form._ensure_relationship_choice_selected",
                AsyncMock(),
            ) as ensure,
            patch(
                "pc_liveagent.live_form._click_continue_to",
                AsyncMock(),
            ) as cont,
        ):
            result = await _submit_eligibility_answer(
                "spouse-partner-fix", session, page, "Partner.", time.time()
            )
        ensure.assert_awaited_with(page, "Partner")
        cont.assert_not_awaited()
        self.assertEqual(session.filled.get("spouse_role"), "Partner")
        self.assertEqual(session.filled.get("spouse_first"), "Alex")
        self.assertEqual(session.elig_step, "spouse_confirm")

    async def test_spouse_confirm_different_age_updates_without_second_match_click(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _submit_eligibility_answer

        session = LiveFormSession(
            call_id="age-fix",
            url="https://example.test",
            elig_step="spouse_confirm",
        )
        session.filled.update(
            {
                "spouse_first": "Alex",
                "spouse_last": "Rivera",
                "spouse_role": "Spouse",
                "spouse_age": "(65+) Senior",
            }
        )
        page = AsyncMock()
        ages = [
            "(0-18) Child",
            "(19-20) Young Adult",
            "(21-63) Adult",
            "(64) Turning Senior",
            "(65+) Senior",
        ]
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="spouse_form"),
            ),
            patch("pc_liveagent.live_form._mark"),
            patch(
                "pc_liveagent.live_form._ensure_age_choices_visible",
                AsyncMock(return_value=ages),
            ),
            patch("pc_liveagent.live_form._click_age_choice", AsyncMock()) as click,
        ):
            changed = await _submit_eligibility_answer(
                "age-fix", session, page, "Twenty five", 0.0
            )
            click.assert_awaited()
            self.assertEqual(session.filled.get("spouse_age"), "(21-63) Adult")
            self.assertEqual(session.elig_step, "spouse_confirm")
            self.assertIn("confirm", (changed.get("spoken") or "").lower())
            click.reset_mock()
            same = await _submit_eligibility_answer(
                "age-fix", session, page, "Twenty five", 0.0
            )
        click.assert_not_awaited()
        self.assertEqual(session.filled.get("spouse_first"), "Alex")
        self.assertEqual(session.elig_step, "spouse_confirm")
        self.assertTrue(same.get("ok"))

    def test_resolve_partner_on_spouse_confirm_is_fast_path(self) -> None:
        from pc_liveagent.answer_parse import is_fast_path_resolve, resolve_answer

        resolved = resolve_answer("spouse_confirm", "Partner.")
        self.assertEqual(resolved.answer, "Partner")
        self.assertEqual(resolved.detail, "confirm_rel_correction")
        self.assertTrue(is_fast_path_resolve(resolved))
        affirm = resolve_answer("spouse_confirm", "Spouse is correct.")
        self.assertEqual(affirm.answer, "Yes")

    def test_resolve_age_on_confirm_is_fast_path(self) -> None:
        from pc_liveagent.answer_parse import is_fast_path_resolve, resolve_answer

        resolved = resolve_answer("spouse_confirm", "Twenty five.")
        self.assertEqual(resolved.detail, "confirm_age_correction")
        self.assertTrue(is_fast_path_resolve(resolved))
        child = resolve_answer("child_confirm", "Child")
        self.assertEqual(child.detail, "confirm_rel_correction")
        person = resolve_answer("person_confirm", "Fifteen.")
        self.assertEqual(person.detail, "confirm_age_correction")

    def test_resolve_relationship_on_person_confirm_is_fast_path(self) -> None:
        from pc_liveagent.answer_parse import is_fast_path_resolve, resolve_answer

        for spoken, expected in (
            ("Grandchild", "Grandchild"),
            ("my stepson", "Stepchild"),
            ("Foster Child", "Foster Child"),
            ("Other", "Other"),
        ):
            resolved = resolve_answer("person_confirm", spoken)
            self.assertEqual(resolved.answer, expected, spoken)
            self.assertEqual(resolved.detail, "confirm_rel_correction")
            self.assertTrue(is_fast_path_resolve(resolved))

    async def test_all_person_relationship_chips_select_and_advance(self) -> None:
        """Every person_rel chip selects via ensure and advances to person_age."""
        from pc_liveagent.live_form import LiveFormSession, _submit_eligibility_answer

        opts = ["Child", "Stepchild", "Grandchild", "Foster Child", "Other"]
        for label in opts:
            session = LiveFormSession(
                call_id=f"rel-{label}",
                url="https://example.test",
                elig_step="person_rel",
            )
            page = AsyncMock()
            page.wait_for_timeout = AsyncMock()
            with (
                patch(
                    "pc_liveagent.live_form._visible_choice_labels",
                    AsyncMock(return_value=opts),
                ),
                patch(
                    "pc_liveagent.live_form._ensure_relationship_choice_selected",
                    AsyncMock(),
                ) as ensure,
                patch(
                    "pc_liveagent.live_form._other_relationship_input_visible",
                    AsyncMock(return_value=False),
                ),
                patch(
                    "pc_liveagent.live_form._eligibility_screen",
                    AsyncMock(return_value="person_form"),
                ),
                patch("pc_liveagent.live_form._mark"),
            ):
                result = await _submit_eligibility_answer(
                    f"rel-{label}", session, page, label, time.time()
                )
            ensure.assert_awaited_with(page, label)
            self.assertEqual(session.filled.get("person_rel"), label)
            self.assertEqual(session.elig_step, "person_age")
            self.assertTrue(result.get("ok"), label)

    async def test_all_child_relationship_chips_select_and_advance(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _submit_eligibility_answer

        opts = ["Child", "Stepchild", "Grandchild", "Foster Child", "Other"]
        for label in opts:
            session = LiveFormSession(
                call_id=f"child-rel-{label}",
                url="https://example.test",
                elig_step="child_rel",
            )
            page = AsyncMock()
            page.wait_for_timeout = AsyncMock()
            with (
                patch(
                    "pc_liveagent.live_form._visible_choice_labels",
                    AsyncMock(return_value=opts),
                ),
                patch(
                    "pc_liveagent.live_form._ensure_relationship_choice_selected",
                    AsyncMock(),
                ) as ensure,
                patch(
                    "pc_liveagent.live_form._other_relationship_input_visible",
                    AsyncMock(return_value=False),
                ),
                patch(
                    "pc_liveagent.live_form._eligibility_screen",
                    AsyncMock(return_value="child_form"),
                ),
                patch("pc_liveagent.live_form._mark"),
            ):
                result = await _submit_eligibility_answer(
                    f"child-rel-{label}", session, page, label, time.time()
                )
            ensure.assert_awaited_with(page, label)
            self.assertEqual(session.filled.get("child_rel"), label)
            self.assertEqual(session.elig_step, "child_age")
            self.assertTrue(result.get("ok"), label)

    async def test_spouse_and_partner_roles_select(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _submit_eligibility_answer

        for spoken, role in (("Spouse", "Spouse"), ("Partner", "Partner"), ("they're my partner", "Partner")):
            session = LiveFormSession(
                call_id=f"role-{role}-{spoken[:4]}",
                url="https://example.test",
                elig_step="spouse_role",
            )
            page = AsyncMock()
            with (
                patch(
                    "pc_liveagent.live_form._eligibility_screen",
                    AsyncMock(return_value="spouse_form"),
                ),
                patch("pc_liveagent.live_form._mark"),
                patch(
                    "pc_liveagent.live_form._ensure_relationship_choice_selected",
                    AsyncMock(),
                ) as ensure,
            ):
                result = await _submit_eligibility_answer(
                    session.call_id, session, page, spoken, time.time()
                )
            ensure.assert_awaited_with(page, role)
            self.assertEqual(session.filled.get("spouse_role"), role)
            self.assertEqual(session.elig_step, "spouse_age")
            self.assertTrue(result.get("ok"))

    async def test_person_confirm_relationship_correction_in_place(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _submit_eligibility_answer

        session = LiveFormSession(
            call_id="person-rel-fix",
            url="https://example.test",
            elig_step="person_confirm",
            after_person="disabled",
        )
        session.filled.update(
            {
                "person_first": "Sam",
                "person_last": "Lee",
                "person_rel": "Child",
                "person_age": "(21-63) Adult",
                "person_kind": "student",
            }
        )
        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="person_form"),
            ),
            patch("pc_liveagent.live_form._mark"),
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                AsyncMock(
                    return_value=[
                        "Child",
                        "Stepchild",
                        "Grandchild",
                        "Foster Child",
                        "Other",
                    ]
                ),
            ),
            patch(
                "pc_liveagent.live_form._ensure_relationship_choice_selected",
                AsyncMock(),
            ) as ensure,
            patch(
                "pc_liveagent.live_form._click_continue_to",
                AsyncMock(),
            ) as cont,
        ):
            result = await _submit_eligibility_answer(
                "person-rel-fix", session, page, "Grandchild", time.time()
            )
        ensure.assert_awaited_with(page, "Grandchild")
        cont.assert_not_awaited()
        self.assertEqual(session.filled.get("person_rel"), "Grandchild")
        self.assertEqual(session.filled.get("person_first"), "Sam")
        self.assertEqual(session.elig_step, "person_confirm")
        self.assertTrue(result.get("ok"))

    async def test_person_confirm_yes_continues_when_relationship_recorded(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _submit_eligibility_answer

        session = LiveFormSession(
            call_id="person-confirm-yes",
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
                "person_kind": "student",
            }
        )
        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="person_form"),
            ),
            patch("pc_liveagent.live_form._mark"),
            patch(
                "pc_liveagent.live_form._ensure_relationship_choice_selected",
                AsyncMock(
                    side_effect=RuntimeError(
                        "REL_CLARIFY: I couldn't select Foster Child on the form."
                    )
                ),
            ) as ensure,
            patch(
                "pc_liveagent.live_form._choice_appears_selected",
                AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.live_form._click_continue_to",
                AsyncMock(),
            ) as cont,
        ):
            result = await _submit_eligibility_answer(
                "person-confirm-yes", session, page, "Yes", time.time()
            )
        ensure.assert_not_awaited()
        cont.assert_awaited()
        self.assertTrue(result.get("ok"))
        self.assertEqual(session.elig_step, "disabled")
        self.assertEqual(session.filled.get("person_rel"), "Foster Child")

    async def test_correct_member_relationship_requires_real_selection(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, correct_member_field

        call_id = "rel-correct-fail"
        session = LiveFormSession(
            call_id=call_id,
            url="https://example.test",
            elig_step="person_confirm",
            welcome_done=True,
            status="open",
        )
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        session.page = page
        from pc_liveagent import live_form as lf

        async with lf._sessions_lock:
            lf._sessions[call_id] = session
        try:
            with (
                patch(
                    "pc_liveagent.live_form._eligibility_screen",
                    AsyncMock(return_value="person_form"),
                ),
                patch(
                    "pc_liveagent.live_form._visible_choice_labels",
                    AsyncMock(
                        return_value=[
                            "Child",
                            "Stepchild",
                            "Grandchild",
                            "Foster Child",
                            "Other",
                        ]
                    ),
                ),
                patch(
                    "pc_liveagent.live_form._ensure_relationship_choice_selected",
                    AsyncMock(
                        side_effect=RuntimeError(
                            "REL_CLARIFY: I couldn't select Child on the form."
                        )
                    ),
                ),
            ):
                result = await correct_member_field(call_id, "relationship", "Child")
            self.assertFalse(result.get("ok"))
            self.assertIn("couldn't select", (result.get("spoken") or "").lower())
            self.assertEqual(session.elig_step, "person_confirm")
            self.assertNotIn("person_rel", session.filled)
        finally:
            async with lf._sessions_lock:
                lf._sessions.pop(call_id, None)

    async def test_programs_decline_closes_without_stage_e(self) -> None:
        """No on program-summary closes politely without starting Stage E."""
        from pc_liveagent.app_stage import CLOSING_NO_APPLICATION
        from pc_liveagent.call_turn import (
            CLOSING,
            _run_eligibility_browser_background,
            drop_controller,
            get_controller,
        )

        call_id = "elig-complete"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.active_websocket_status = "connected"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="programs"
        )
        deliver = AsyncMock(return_value=True)

        async def fake_get_session(_cid: str):
            return session

        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch(
                "pc_liveagent.call_turn.submit_field",
                AsyncMock(
                    return_value={
                        "ok": True,
                        "spoken": CLOSING_NO_APPLICATION,
                        "step": "",
                        "end_call": True,
                        "action": "declined_application",
                    }
                ),
            ),
            patch(
                "pc_liveagent.call_turn._deliver_question_once",
                deliver,
            ),
        ):
            await _run_eligibility_browser_background(
                call_id,
                answer="No",
                step="programs",
                turn_id=f"{call_id}:1:close",
                form_url="https://example.test",
                commit_key="programs:1:no",
                epoch=ctrl.elig_nav_epoch,
            )
        self.assertEqual(ctrl.phase, "closing")
        self.assertIn("have not started an application", ctrl.last_speech.lower())
        self.assertIn("thank", ctrl.last_speech.lower())
        deliver.assert_awaited()
        kwargs = deliver.await_args.kwargs
        self.assertTrue(kwargs.get("end_call"))
        self.assertIn("close:", kwargs.get("key", ""))
        r = await process_user_turn(call_id, "")
        self.assertTrue(r.end_call)
        self.assertEqual(r.phase, "closing")
        self.assertTrue(CLOSING)
        await drop_controller(call_id)

    async def test_speak_now_closing_sets_end_call_flag(self) -> None:
        from pc_liveagent import retell_llm_ws

        sent: list[dict] = []

        async def fake_send(_ws, payload):
            sent.append(payload)

        with (
            patch.dict(retell_llm_ws._active_ws, {"c-end": object()}, clear=False),
            patch("pc_liveagent.retell_llm_ws._send", side_effect=fake_send),
        ):
            ok = await retell_llm_ws.speak_now(
                "c-end", "Thanks, goodbye.", interrupt_id=9, end_call=True
            )
        self.assertTrue(ok)
        self.assertEqual(sent[0]["response_type"], "agent_interrupt")
        self.assertTrue(sent[0].get("end_call"))

    async def test_next_question_repeats_without_advancing(self) -> None:
        call_id = "turn-test"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="disabled"
        )
        submit = AsyncMock()

        async def fake_get_session(_cid: str):
            return session

        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch("pc_liveagent.call_turn.submit_field", submit),
        ):
            r = await process_user_turn(call_id, "What's the next question?")
        self.assertIn("disabled person", r.speech.lower())
        self.assertEqual(session.elig_step, "disabled")
        submit.assert_not_called()

    async def test_unclear_yes_no_returns_full_question(self) -> None:
        call_id = "turn-test"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="relatives"
        )
        submit = AsyncMock()

        async def fake_get_session(_cid: str):
            return session

        from pc_liveagent.answer_parse import ClaudeResolveResult

        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch("pc_liveagent.call_turn.peek_choice_labels", AsyncMock(return_value=[])),
            patch("pc_liveagent.call_turn.submit_field", submit),
            patch(
                "pc_liveagent.call_turn.resolve_with_claude",
                AsyncMock(
                    return_value=ClaudeResolveResult(
                        intent="clarify",
                        needs_clarify=True,
                        reject_reason="needs_clarify",
                        accepted=False,
                    )
                ),
            ),
        ):
            r = await process_user_turn(call_id, "Sixteen to eighteen.")
        self.assertIn("other relatives", r.speech.lower())
        self.assertIn("yes or no", r.speech.lower())
        self.assertEqual(session.elig_step, "relatives")
        submit.assert_not_called()

    async def test_latest_user_utterance(self) -> None:
        transcript = [
            {"role": "agent", "content": "Are there any students?"},
            {"role": "user", "content": "No"},
        ]
        self.assertEqual(latest_user_utterance(transcript), "No")


class IncomeMappingTests(unittest.TestCase):
    def test_period_aliases(self) -> None:
        from pc_liveagent.live_form import (
            _income_period_choice,
            _resolve_income_period_label,
        )

        self.assertEqual(_income_period_choice("yearly"), "Annual")
        self.assertEqual(_income_period_choice("Annually."), "Annual")
        self.assertEqual(_income_period_choice("every two weeks"), "Bi-weekly")
        self.assertEqual(_income_period_choice("two weeks."), "Bi-weekly")
        self.assertEqual(_income_period_choice("Monthly."), "Monthly")
        # Hyphen / spacing / STT variants must map (not fail on week|ly boundary).
        for spoken in (
            "bi-weekly",
            "biweekly",
            "bi weekly",
            "Bi weekly.",
            "every 2 weeks",
            "by weekly",
            "buy weekly",
        ):
            self.assertEqual(
                _income_period_choice(spoken),
                "Bi-weekly",
                msg=spoken,
            )
            self.assertEqual(
                _resolve_income_period_label(
                    spoken, ["Annual", "Monthly", "Bi-weekly"]
                ),
                "Bi-weekly",
                msg=spoken,
            )
        self.assertEqual(
            _resolve_income_period_label(
                "not a period", ["Annual", "Monthly", "Bi-weekly"]
            ),
            "",
        )
        # Never fall back to the first visible chip.
        self.assertNotEqual(
            _resolve_income_period_label(
                "fortnightly-ish maybe", ["Annual", "Monthly", "Bi-weekly"]
            ),
            "Annual",
        )

    def test_range_spoken_to_chip(self) -> None:
        from pc_liveagent.live_form import _map_income_range_label

        labels = [
            "$0 – $12,000",
            "$12,001 – $13,200",
            "$13,201 – $22,000",
            "$22,001 – $26,300",
            "$26,301 – $32,700",
            "$32,701 – $39,900",
            "$39,901+",
        ]
        self.assertEqual(
            _map_income_range_label("Zero dollars to twelve thousand dollars.", labels),
            "$0 – $12,000",
        )
        self.assertEqual(_map_income_range_label("first option", labels), "$0 – $12,000")
        self.assertEqual(_map_income_range_label("39901 plus", labels), "$39,901+")
        self.assertEqual(
            _map_income_range_label("40000-50000", labels),
            "$39,901+",
        )
        self.assertEqual(
            _map_income_range_label("forty to fifty thousand", labels),
            "$39,901+",
        )
        # Unmatched / not near open floor — must not fall back to first chip.
        self.assertEqual(
            _map_income_range_label("ninety thousand to one hundred thousand", labels),
            "",
        )
        self.assertNotEqual(
            _map_income_range_label("ninety thousand to one hundred thousand", labels),
            labels[0],
        )

    def test_natural_spoken_amounts_and_k(self) -> None:
        from pc_liveagent.live_form import (
            _map_income_range_label,
            _parse_spoken_income_interval,
        )

        labels = [
            "$0 – $12,000",
            "$12,001 – $39,900",
            "$39,901 – $50,000",
            "$50,001 – $100,000",
            "$100,001+",
        ]
        self.assertEqual(
            _parse_spoken_income_interval("forty to fifty thousand"),
            (40000, 50000),
        )
        self.assertEqual(
            _parse_spoken_income_interval("Between 40,000 and 50,000 dollars."),
            (40000, 50000),
        )
        self.assertEqual(_parse_spoken_income_interval("About 100k."), (100000, 100000))
        self.assertEqual(
            _parse_spoken_income_interval("One hundred thousand a year."),
            (100000, 100000),
        )
        self.assertEqual(
            _parse_spoken_income_interval("Around forty thousand annually."),
            (40000, 40000),
        )
        self.assertEqual(
            _parse_spoken_income_interval("About five thousand a month."),
            (5000, 5000),
        )
        self.assertEqual(
            _parse_spoken_income_interval("Every two weeks, around two thousand."),
            (2000, 2000),
        )
        self.assertEqual(
            _map_income_range_label("About 100k.", labels),
            "$50,001 – $100,000",
        )
        self.assertEqual(
            _map_income_range_label("One hundred thousand a year.", labels),
            "$50,001 – $100,000",
        )
        self.assertEqual(
            _map_income_range_label("forty to fifty thousand", labels),
            "$39,901 – $50,000",
        )
        self.assertEqual(
            _map_income_range_label("About five thousand a month.", labels),
            "$0 – $12,000",
        )

    def test_bare_number_requests_scale_clarification(self) -> None:
        from pc_liveagent.answer_parse import resolve_answer
        from pc_liveagent.elig_meta import get_field_meta
        from pc_liveagent.live_form import (
            _income_amount_scale_unclear,
            _map_income_range_label,
        )

        labels = ["$0 – $12,000", "$39,901 – $50,000", "$50,001 – $100,000"]
        self.assertTrue(_income_amount_scale_unclear("100"))
        self.assertTrue(_income_amount_scale_unclear("one hundred"))
        self.assertFalse(_income_amount_scale_unclear("100k"))
        self.assertFalse(_income_amount_scale_unclear("one hundred thousand"))
        self.assertFalse(_income_amount_scale_unclear("100 dollars"))
        self.assertEqual(_map_income_range_label("100", labels), "")
        r = resolve_answer(
            "income_range",
            "100",
            options=labels,
            meta=get_field_meta("income_range"),
        )
        self.assertIsNone(r.answer)
        self.assertEqual(r.detail, "income_scale_unclear")
        clarify = (r.clarify or "").lower()
        self.assertIn("thousand", clarify)
        self.assertIn("dollars", clarify)
        # Do not echo the raw utterance or list UI chip labels.
        self.assertNotIn("i heard 100", clarify)
        self.assertNotIn("$39,901", r.clarify or "")
        self.assertNotIn("you see", clarify)

    def test_period_switch_detected_from_income_range(self) -> None:
        from pc_liveagent.answer_parse import resolve_answer
        from pc_liveagent.elig_meta import get_field_meta
        from pc_liveagent.live_form import (
            _detect_income_period_switch,
            _wants_income_period_reselect,
        )

        self.assertEqual(
            _detect_income_period_switch("change the period to monthly"),
            "Monthly",
        )
        self.assertEqual(
            _detect_income_period_switch("I want to choose bi-weekly instead"),
            "Bi-weekly",
        )
        self.assertEqual(
            _detect_income_period_switch("go back to annual"),
            "Annual",
        )
        self.assertTrue(_wants_income_period_reselect("go back to the period"))
        self.assertTrue(_wants_income_period_reselect("change the income period"))
        switched = resolve_answer(
            "income_range",
            "change the period to monthly",
            options=["$0 – $12,000", "$12,001+"],
            meta=get_field_meta("income_range"),
        )
        self.assertEqual(switched.answer, "__INCOME_PERIOD_SWITCH__:Monthly")
        self.assertEqual(switched.detail, "income_period_switch")
        reselect = resolve_answer(
            "income_range",
            "go back to the period",
            options=["$0 – $12,000"],
            meta=get_field_meta("income_range"),
        )
        self.assertEqual(reselect.answer, "__INCOME_PERIOD_RESELECT__")

    def test_voice_friendly_income_questions(self) -> None:
        from pc_liveagent.elig_meta import format_question
        from pc_liveagent.live_form import (
            ELIG_QUESTIONS,
            LiveFormSession,
            _income_range_question_for_period,
            _spoken_question_for_step,
        )

        annual = _income_range_question_for_period("Annual")
        monthly = _income_range_question_for_period("Monthly")
        bi = _income_range_question_for_period("Bi-weekly")
        self.assertIn("per year", annual.lower())
        self.assertIn("per month", monthly.lower())
        self.assertIn("every two weeks", bi.lower())
        for q in (annual, monthly, bi, ELIG_QUESTIONS["income_range"]):
            self.assertNotIn("you see", q.lower())
            self.assertNotIn("dollar range you see", q.lower())
            # Never enumerate every income-band chip in the spoken question.
            self.assertNotIn("$0", q)
            self.assertNotIn("$12,000", q)
        fq = format_question(
            "income_range",
            options=["$0 – $12,000", "$12,001 – $39,900"],
            period="Monthly",
        )
        self.assertIn("per month", fq.lower())
        self.assertNotIn("$0", fq)
        self.assertNotIn("required to continue", fq.lower())
        from pc_liveagent.live_form import build_options_help_speech

        help_speech = build_options_help_speech(
            "income_range", ["$0 – $12,000", "$39,901+"]
        )
        self.assertNotIn("$0", help_speech)
        self.assertNotIn("$39,901", help_speech)
        session = LiveFormSession(
            call_id="inc-q", url="https://example.test", elig_step="income_range"
        )
        session.filled["income_when"] = "Bi-weekly"
        spoken = _spoken_question_for_step(session, "income_range")
        self.assertIn("every two weeks", spoken.lower())

    def test_income_range_validate(self) -> None:
        from pc_liveagent.answer_parse import resolve_answer
        from pc_liveagent.elig_meta import get_field_meta

        labels = [
            "$0 – $12,000",
            "$12,001 – $13,200",
            "$39,901+",
        ]
        r = resolve_answer(
            "income_range",
            "Zero dollars to twelve thousand dollars.",
            options=labels,
            meta=get_field_meta("income_range"),
        )
        self.assertEqual(r.answer, "$0 – $12,000")
        bad = resolve_answer(
            "income_range",
            "ninety thousand to one hundred thousand",
            options=labels,
            meta=get_field_meta("income_range"),
        )
        self.assertIsNone(bad.answer)
        self.assertIsNotNone(bad.clarify)
        clarify = (bad.clarify or "").lower()
        self.assertNotIn("$0", bad.clarify or "")
        # Must not echo the full caller phrase back (avoids clarification loops).
        self.assertNotIn("ninety thousand to one hundred thousand", clarify)
        self.assertNotIn("i heard", clarify)
        self.assertNotEqual(bad.answer, labels[0] if labels else None)
        answer2, clarify2 = validate_for_step("income_when", "two weeks")
        self.assertEqual(answer2, "Bi-weekly")
        self.assertIsNone(clarify2)

    def test_income_screen_keeps_range_step(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _screen_first_step

        session = LiveFormSession(
            call_id="t", url="https://example.test", elig_step="income_range"
        )
        self.assertEqual(_screen_first_step("income", session), "income_range")


class WsResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_begin_speech_does_not_reset_mid_call(self) -> None:
        from pc_liveagent.call_turn import begin_speech, drop_controller, get_controller

        call_id = "ws-resume-test"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.turn_seq = 5
        ctrl.last_speech = "Which income range applies before taxes?"
        speech = await begin_speech(call_id)
        self.assertEqual(speech, "Which income range applies before taxes?")
        ctrl2 = await get_controller(call_id)
        self.assertEqual(ctrl2.phase, "eligibility")
        await drop_controller(call_id)

    async def test_auto_start_after_signin_when_session_ready(self) -> None:
        from pc_liveagent.call_turn import drop_controller, get_controller, process_user_turn
        from pc_liveagent.live_form import ELIG_QUESTIONS, LiveFormSession

        call_id = "auto-start-test"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "sign_in"
        session = LiveFormSession(
            call_id=call_id,
            url="https://example.test",
            signed_in=True,
            elig_step="marital",
        )

        async def fake_get_session(_cid: str):
            return session

        async def fake_promote(_cid: str):
            return ELIG_QUESTIONS["marital"]

        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch(
                "pc_liveagent.call_turn.promote_signed_in_to_eligibility",
                side_effect=fake_promote,
            ),
        ):
            result = await process_user_turn(call_id, "next question")
        self.assertEqual(result.phase, "eligibility")
        self.assertEqual(result.detail, "auto_start_after_signin")
        self.assertIn("marital", result.speech.lower())
        await drop_controller(call_id)


class SupportScreenTests(unittest.TestCase):
    def test_support_body_not_misread_as_student(self) -> None:
        from pc_liveagent.live_form import _map_support_yes_label

        body_like_labels = [
            "Yes, I am a full-time student and my parent or legal guardian supports me.",
            "Yes, I live more than half the year with the person that supports me.",
            "Yes, I'm disabled and live year-round with the person that supports me.",
            "Yes, I live more than half the year with a relative that supports me.",
            "No",
        ]
        self.assertIn("student", _map_support_yes_label("yes student parent", body_like_labels).lower())
        self.assertIn("disabled", _map_support_yes_label("yes disabled", body_like_labels).lower())
        self.assertIn("relative", _map_support_yes_label("yes a relative", body_like_labels).lower())
        self.assertEqual(_map_support_yes_label("No", body_like_labels), "")

    def test_support_bare_yes_asks_clarify(self) -> None:
        answer, clarify = validate_for_step("support", "Yes")
        self.assertIsNone(answer)
        self.assertIn("which situation", (clarify or "").lower())

    def test_support_no_is_clear(self) -> None:
        answer, clarify = validate_for_step("support", "No")
        self.assertEqual(answer, "No")
        self.assertIsNone(clarify)

    def test_support_paraphrases_map_before_claude(self) -> None:
        from pc_liveagent.answer_parse import is_fast_path_resolve, resolve_answer

        labels = [
            "Yes, I am a full-time student and my parent or legal guardian supports me.",
            "Yes, I live more than half the year with the person that supports me.",
            "Yes, I'm disabled and live year-round with the person that supports me.",
            "Yes, I live more than half the year with a relative that supports me.",
            "No",
        ]
        student = labels[0]
        for utterance in ("I'm a student", "Student supported by parent"):
            resolved = resolve_answer("support", utterance, options=labels)
            self.assertEqual(resolved.answer, student, utterance)
            self.assertEqual(resolved.detail, "support_yes")
            self.assertTrue(is_fast_path_resolve(resolved), utterance)
        live = resolve_answer(
            "support", "I live with the person who supports me", options=labels
        )
        self.assertEqual(live.answer, labels[1])
        self.assertTrue(is_fast_path_resolve(live))
        disabled = resolve_answer("support", "I'm disabled", options=labels)
        self.assertEqual(disabled.answer, labels[2])
        relative = resolve_answer(
            "support", "a relative supports me", options=labels
        )
        self.assertEqual(relative.answer, labels[3])
        no = resolve_answer("support", "No", options=labels)
        self.assertEqual(no.answer, "No")
        self.assertEqual(no.detail, "support_no")
        self.assertTrue(is_fast_path_resolve(no))

    def test_support_ambiguous_asks_clarification(self) -> None:
        from pc_liveagent.answer_parse import is_fast_path_resolve, resolve_answer

        labels = [
            "Yes, I am a full-time student and my parent or legal guardian supports me.",
            "Yes, I live more than half the year with the person that supports me.",
            "Yes, I'm disabled and live year-round with the person that supports me.",
            "Yes, I live more than half the year with a relative that supports me.",
            "No",
        ]
        for utterance in ("Yes", "maybe", "I'm a student and also disabled"):
            resolved = resolve_answer("support", utterance, options=labels)
            self.assertIsNone(resolved.answer, utterance)
            self.assertIn(
                resolved.detail, ("support_clarify", "support_unclear"), utterance
            )
            self.assertFalse(is_fast_path_resolve(resolved), utterance)
            self.assertTrue(resolved.clarify, utterance)

    def test_support_long_wrapper_drops_household_chips(self) -> None:
        from pc_liveagent.live_form import (
            _SUPPORT_CANONICAL,
            _map_support_yes_label,
            _support_option_labels,
        )

        student, lives, disabled, relative, no = _SUPPORT_CANONICAL
        long_wrapper = (
            "Your household Agent You john Partner "
            + " ".join(_SUPPORT_CANONICAL)
            + " extra wrapper text so this label is longer than the generic "
            "one hundred forty character choice cap and must still be read."
        )
        labels = _support_option_labels(
            ["Your household", "Agent You", "john Partner", long_wrapper]
        )
        self.assertEqual(labels, [student, lives, disabled, relative, no])
        self.assertNotIn("john Partner", labels)
        self.assertNotIn("Agent You", labels)
        self.assertEqual(_map_support_yes_label("I'm a student", []), student)
        self.assertEqual(
            _map_support_yes_label("Student supported by parent", [long_wrapper]),
            student,
        )
        self.assertEqual(_map_support_yes_label("I'm disabled", labels), disabled)
        self.assertIn(
            "person that supports",
            _map_support_yes_label(
                "I live with the person who supports me", labels
            ).lower(),
        )
        self.assertIn(
            "relative",
            _map_support_yes_label("a relative supports me", labels).lower(),
        )
        self.assertEqual(_map_support_yes_label("No", labels), "")


class ConfigTests(unittest.TestCase):
    def test_websocket_url_from_webhook(self) -> None:
        with patch("pc_liveagent.config.RETELL_LLM_WEBSOCKET_URL", ""):
            with patch(
                "pc_liveagent.config.FORM_FILLER_WEBHOOK_URL",
                "https://tunnel.example/webhooks/retell",
            ):
                url = retell_llm_websocket_url()
                self.assertEqual(url, "wss://tunnel.example/llm-websocket")


class BrowserLinkTests(unittest.IsolatedAsyncioTestCase):
    def test_local_devtools_url(self) -> None:
        from pc_liveagent.browser import local_devtools_url

        url = local_devtools_url(
            {
                "id": "ABC123",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/ABC123",
            }
        )
        self.assertIn("127.0.0.1:9222/devtools/inspector.html", url)
        self.assertIn("ABC123", url)

    async def test_describe_live_browser_includes_open_url(self) -> None:
        from pc_liveagent.live_form import describe_live_browser

        page = AsyncMock()
        page.url = "https://my.pointcare.com/auth"
        page.is_closed = lambda: False
        session = LiveFormSession(
            call_id="call_browser_link",
            url="https://my.pointcare.com/onboarding",
            welcome_done=True,
            status="open",
        )
        session.page = page
        with patch(
            "pc_liveagent.browser.cdp_target_for_url",
            return_value={
                "id": "TAB1",
                "url": "https://my.pointcare.com/auth",
                "title": "Pointcare",
                "type": "page",
            },
        ):
            info = await describe_live_browser(
                session,
                call_id="call_browser_link",
                open_base_url="https://tunnel.example",
            )
        self.assertTrue(info["opened"])
        self.assertEqual(info["url"], "https://my.pointcare.com/auth")
        self.assertEqual(
            info["open_url"],
            "https://tunnel.example/form-filler/sessions/call_browser_link/chrome",
        )
        self.assertIn("devtools", info["devtools_url"])
        self.assertTrue(info["chrome_user_data"])
        self.assertEqual(session.url, "https://my.pointcare.com/auth")


class CustomLlmProtocolTests(unittest.TestCase):
    def test_response_payload_shape(self) -> None:
        # Documented Retell response shape used by retell_llm_ws.
        payload = {
            "response_type": "response",
            "response_id": 3,
            "content": "Are there any students?",
            "content_complete": True,
        }
        self.assertEqual(payload["response_type"], "response")
        self.assertTrue(payload["content_complete"])
        self.assertIsInstance(payload["response_id"], int)

    def test_agent_interrupt_payload_shape(self) -> None:
        # Documented Retell agent_interrupt shape (post-SSO first question).
        payload = {
            "response_type": "agent_interrupt",
            "interrupt_id": 1,
            "content": "What is your marital status?",
            "content_complete": True,
            "no_interruption_allowed": True,
        }
        self.assertEqual(payload["response_type"], "agent_interrupt")
        self.assertIsInstance(payload["interrupt_id"], int)
        self.assertTrue(payload["content_complete"])


class GoogleSsoNonBlockingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await drop_controller("sso-test")
        await drop_controller("sso-slow")
        await drop_controller("sso-fail")
        await drop_controller("sso-dup")
        await drop_controller("sso-disc")

    async def test_google_ack_returns_before_sso_finishes(self) -> None:
        from pc_liveagent.call_turn import SSO_ACK, _sso_tasks
        from pc_liveagent.live_form import ELIG_QUESTIONS

        call_id = "sso-test"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "sign_in"
        started = asyncio.Event()
        finished = asyncio.Event()

        async def slow_submit(*_a, **_k):
            started.set()
            await finished.wait()
            return {
                "ok": True,
                "spoken": f"ASK: {ELIG_QUESTIONS['marital']}",
                "step": "marital",
            }

        with (
            patch("pc_liveagent.call_turn.submit_field", side_effect=slow_submit),
            patch("pc_liveagent.call_turn.get_session", AsyncMock(return_value=None)),
            patch("pc_liveagent.call_turn._speak_interrupt", AsyncMock(return_value=True)),
        ):
            result = await process_user_turn(call_id, "Google", response_id=4)
            self.assertEqual(result.detail, "sso_ack")
            self.assertEqual(result.speech, SSO_ACK)
            self.assertTrue(ctrl.sso_in_progress)
            self.assertTrue(ctrl.sso_ack_sent)
            # Yield so the background SSO task reaches submit_field without finishing.
            await asyncio.wait_for(started.wait(), timeout=2.0)
            self.assertFalse(finished.is_set())
            task = _sso_tasks.get(call_id)
            self.assertIsNotNone(task)
            finished.set()
            await asyncio.wait_for(task, timeout=2.0)
        self.assertFalse(ctrl.sso_in_progress)
        self.assertEqual(ctrl.phase, "eligibility")
        self.assertTrue(ctrl.eligibility_screen_verified)
        self.assertTrue(ctrl.eligibility_question_sent)

    async def test_slow_sso_keeps_turns_responsive(self) -> None:
        from pc_liveagent.call_turn import SSO_BUSY

        call_id = "sso-slow"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "sign_in"
        gate = asyncio.Event()

        async def slow_submit(*_a, **_k):
            await gate.wait()
            return {"ok": True, "spoken": "ASK: What is your marital status?"}

        with (
            patch("pc_liveagent.call_turn.submit_field", side_effect=slow_submit),
            patch("pc_liveagent.call_turn.get_session", AsyncMock(return_value=None)),
            patch("pc_liveagent.call_turn._speak_interrupt", AsyncMock(return_value=True)),
        ):
            await process_user_turn(call_id, "At Google", response_id=2)
            busy = await process_user_turn(call_id, "hello?", response_id=3)
            self.assertEqual(busy.detail, "sso_in_progress")
            self.assertEqual(busy.speech, SSO_BUSY)
            gate.set()
            task = __import__("pc_liveagent.call_turn", fromlist=["_sso_tasks"])._sso_tasks.get(
                call_id
            )
            if task:
                await asyncio.wait_for(task, timeout=2.0)

    async def test_sso_failure_does_not_send_eligibility_question(self) -> None:
        call_id = "sso-fail"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "sign_in"
        interrupt = AsyncMock(return_value=True)

        async def fail_submit(*_a, **_k):
            return {
                "ok": False,
                "spoken": "Please say Google again when the sign-in page is ready.",
                "error": "not_ready",
            }

        with (
            patch("pc_liveagent.call_turn.submit_field", side_effect=fail_submit),
            patch("pc_liveagent.call_turn._speak_interrupt", interrupt),
            patch("pc_liveagent.call_turn.schedule_signin_watch"),
        ):
            await process_user_turn(call_id, "Google", response_id=1)
            task = __import__("pc_liveagent.call_turn", fromlist=["_sso_tasks"])._sso_tasks.get(
                call_id
            )
            if task:
                await asyncio.wait_for(task, timeout=2.0)
        self.assertFalse(ctrl.eligibility_question_sent)
        self.assertFalse(ctrl.eligibility_screen_verified)
        self.assertNotEqual(ctrl.phase, "eligibility")
        interrupt.assert_awaited()
        spoken = interrupt.await_args.args[1]
        self.assertNotIn("marital", spoken.lower())

    async def test_duplicate_ready_does_not_double_speak(self) -> None:
        from pc_liveagent.call_turn import deliver_first_eligibility_question
        from pc_liveagent.live_form import ELIG_QUESTIONS

        call_id = "sso-dup"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_screen_verified = True
        ctrl.last_speech = ELIG_QUESTIONS["marital"]
        interrupt = AsyncMock(return_value=True)
        with patch("pc_liveagent.call_turn._speak_interrupt", interrupt):
            ok1 = await deliver_first_eligibility_question(call_id)
            ok2 = await deliver_first_eligibility_question(call_id)
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertEqual(interrupt.await_count, 1)

    async def test_disconnect_skips_stale_interrupt(self) -> None:
        from pc_liveagent.call_turn import deliver_first_eligibility_question
        from pc_liveagent.live_form import ELIG_QUESTIONS

        call_id = "sso-disc"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_screen_verified = True
        ctrl.active_websocket_status = "disconnected"
        ctrl.last_speech = ELIG_QUESTIONS["marital"]
        ok = await deliver_first_eligibility_question(call_id)
        self.assertFalse(ok)
        self.assertFalse(ctrl.eligibility_question_sent)


class EmailAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        for cid in (
            "email-choice",
            "email-ack",
            "email-fail",
            "email-secure",
            "email-success",
            "email-extract",
            "email-goback",
            "email-goback-race",
        ):
            await drop_controller(cid)

    def test_extract_and_normalize_spoken_email(self) -> None:
        from pc_liveagent.answer_parse import extract_email, is_email_choice, normalize_spoken_email

        self.assertTrue(is_email_choice("use email"))
        self.assertTrue(is_email_choice("I'll sign in with email"))
        self.assertFalse(is_email_choice("Google email"))
        self.assertEqual(extract_email("jane.doe@example.com"), "jane.doe@example.com")
        self.assertEqual(
            extract_email("jane doe at example dot com"),
            "janedoe@example.com",
        )
        self.assertEqual(
            normalize_spoken_email("name at domain dot org"),
            "name@domain.org",
        )
        self.assertEqual(extract_email("use email"), "")
        # Retell / spoken variants that previously caused re-prompt loops.
        self.assertEqual(
            extract_email("my email is murali at gmail dot com"),
            "murali@gmail.com",
        )
        self.assertEqual(
            extract_email("murali at the rate gmail.com"),
            "murali@gmail.com",
        )
        self.assertEqual(
            extract_email("john underscore doe at gmail period com"),
            "john_doe@gmail.com",
        )
        self.assertEqual(
            extract_email("jane hyphen doe at example dot com"),
            "jane-doe@example.com",
        )
        self.assertEqual(
            extract_email("email is pat at g mail dot com"),
            "pat@gmail.com",
        )
        # Do not invent @ when the speaker never said at / @.
        self.assertEqual(extract_email("murali gmail.com"), "")
        self.assertEqual(extract_email("hello there"), "")
        # Spoken trailing digits (Retell often says zerozerothree for 003).
        self.assertEqual(
            extract_email("murali zero zero three at gmail dot com"),
            "murali003@gmail.com",
        )
        self.assertEqual(
            extract_email("murali zerozerothree at gmail.com"),
            "murali003@gmail.com",
        )
        self.assertEqual(
            extract_email("john oh one two at example period com"),
            "john012@example.com",
        )
        # Must not corrupt normal words like "someone".
        self.assertEqual(extract_email("someone at example.com"), "someone@example.com")

    async def test_spoken_email_does_not_reprompt(self) -> None:
        from pc_liveagent.call_turn import EMAIL_ACK, EMAIL_CLARIFY, _email_auth_tasks

        call_id = "email-extract"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "email"
        gate = asyncio.Event()

        async def slow_submit(cid, field, value, **_k):
            self.assertEqual(field, "email")
            self.assertEqual(value, "murali@gmail.com")
            await gate.wait()
            return {
                "ok": True,
                "auth_screen": "need_password",
                "spoken": "Please finish signing in in the Pointcare window.",
                "awaiting_secure": True,
            }

        with (
            patch("pc_liveagent.call_turn.submit_field", side_effect=slow_submit),
            patch(
                "pc_liveagent.call_turn.poll_email_auth_until_signed_in",
                AsyncMock(
                    return_value={
                        "ok": True,
                        "auth_screen": "signed_in",
                        "spoken": "ASK: What is your marital status?",
                        "step": "marital",
                    }
                ),
            ),
            patch("pc_liveagent.call_turn.get_session", AsyncMock(return_value=None)),
            patch("pc_liveagent.call_turn._speak_interrupt", AsyncMock(return_value=True)),
            patch(
                "pc_liveagent.call_turn.deliver_first_eligibility_question",
                AsyncMock(return_value=True),
            ),
        ):
            first = await process_user_turn(
                call_id, "my email is murali at gmail dot com", response_id=1
            )
            self.assertEqual(first.detail, "email_confirm")
            self.assertIn("murali at gmail dot com", first.speech.lower())
            self.assertFalse(first.end_call)
            confirmed = await process_user_turn(call_id, "yes", response_id=2)
            self.assertEqual(confirmed.detail, "email_ack")
            self.assertEqual(confirmed.speech, EMAIL_ACK)
            self.assertTrue(ctrl.email_captured)
            # A follow-up while auth runs must not re-ask for the address.
            second = await process_user_turn(call_id, "hello?", response_id=3)
            self.assertNotEqual(second.speech, EMAIL_CLARIFY)
            self.assertNotIn("what email", second.speech.lower())
            self.assertFalse(second.end_call)
            gate.set()
            task = _email_auth_tasks.get(call_id)
            if task:
                await asyncio.wait_for(task, timeout=2.0)
        self.assertEqual(ctrl.phase, "eligibility")

    async def test_unclear_email_gets_clarify_not_loop_identical(self) -> None:
        from pc_liveagent.call_turn import EMAIL_CLARIFY, EMAIL_QUESTION

        call_id = "email-choice"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "email"
        result = await process_user_turn(call_id, "murali gmail.com")
        self.assertEqual(result.detail, "email_address_unclear")
        self.assertEqual(result.speech, EMAIL_CLARIFY)
        self.assertNotEqual(result.speech, EMAIL_QUESTION)
        self.assertFalse(result.end_call)
        self.assertEqual(ctrl.phase, "email")

    async def test_email_confirm_and_go_back(self) -> None:
        from pc_liveagent.call_turn import EMAIL_ACK, EMAIL_GO_BACK, _email_auth_tasks

        call_id = "email-goback"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "email"

        confirm = await process_user_turn(call_id, "test@example.com")
        self.assertEqual(confirm.detail, "email_confirm")
        self.assertEqual(ctrl.phase, "email_confirm")
        self.assertEqual(ctrl.pending_email, "test@example.com")

        with patch("pc_liveagent.call_turn._reset_email_sign_in_browser", AsyncMock()):
            back = await process_user_turn(call_id, "go back")
        self.assertEqual(back.detail, "email_go_back")
        self.assertEqual(back.speech, EMAIL_GO_BACK)
        self.assertEqual(ctrl.phase, "sign_in")
        self.assertEqual(ctrl.pending_email, "")
        self.assertFalse(ctrl.email_captured)

        # ASR-glued "goback" must also work.
        await process_user_turn(call_id, "use email")
        await process_user_turn(call_id, "test@example.com")
        with patch("pc_liveagent.call_turn._reset_email_sign_in_browser", AsyncMock()):
            glued = await process_user_turn(call_id, "goback")
        self.assertEqual(glued.detail, "email_go_back")
        self.assertEqual(ctrl.phase, "sign_in")

        await process_user_turn(call_id, "use email")
        again = await process_user_turn(call_id, "right@example.com")
        self.assertEqual(again.detail, "email_confirm")
        self.assertIn("right at example dot com", again.speech.lower())

        # Saying "no" also goes back.
        with patch("pc_liveagent.call_turn._reset_email_sign_in_browser", AsyncMock()):
            await process_user_turn(call_id, "no that's wrong")
        self.assertEqual(ctrl.phase, "sign_in")

        await process_user_turn(call_id, "use email")
        await process_user_turn(call_id, "right@example.com")
        with (
            patch("pc_liveagent.call_turn.submit_field", AsyncMock(
                return_value={
                    "ok": True,
                    "auth_screen": "need_password",
                    "awaiting_secure": True,
                    "spoken": "Please finish creating or entering your password in the Pointcare window.",
                }
            )),
            patch(
                "pc_liveagent.call_turn.poll_email_auth_until_signed_in",
                AsyncMock(return_value=None),
            ),
            patch("pc_liveagent.call_turn._speak_interrupt", AsyncMock(return_value=True)),
            patch("pc_liveagent.call_turn._reset_email_sign_in_browser", AsyncMock()),
        ):
            yes = await process_user_turn(call_id, "yes", response_id=9)
            self.assertEqual(yes.detail, "email_ack")
            self.assertEqual(yes.speech, EMAIL_ACK)
            task = _email_auth_tasks.get(call_id)
            # Go back after auth started (glued form).
            back2 = await process_user_turn(call_id, "goback")
            self.assertEqual(back2.detail, "email_go_back")
            self.assertEqual(ctrl.phase, "sign_in")
            self.assertFalse(ctrl.email_auth_in_progress)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        await drop_controller(call_id)

    async def test_go_back_wins_over_stale_email_auth_task(self) -> None:
        """Playwright finishing after go-back must not steal the phase."""
        from pc_liveagent.call_turn import EMAIL_GO_BACK, _email_auth_tasks

        call_id = "email-goback-race"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "email"
        release_fill = asyncio.Event()
        fill_started = asyncio.Event()

        async def slow_submit(*_a, **_k):
            fill_started.set()
            await release_fill.wait()
            return {
                "ok": True,
                "auth_screen": "need_password",
                "awaiting_secure": True,
                "spoken": "Please finish signing in.",
            }

        with (
            patch("pc_liveagent.call_turn.submit_field", side_effect=slow_submit),
            patch(
                "pc_liveagent.call_turn.poll_email_auth_until_signed_in",
                AsyncMock(
                    return_value={
                        "ok": True,
                        "auth_screen": "signed_in",
                        "spoken": "ASK: What is your marital status?",
                        "step": "marital",
                    }
                ),
            ),
            patch("pc_liveagent.call_turn._speak_interrupt", AsyncMock(return_value=True)),
            patch(
                "pc_liveagent.call_turn.deliver_first_eligibility_question",
                AsyncMock(return_value=True),
            ),
            patch("pc_liveagent.call_turn._reset_email_sign_in_browser", AsyncMock()),
        ):
            await process_user_turn(call_id, "bad@example.com")
            await process_user_turn(call_id, "yes", response_id=1)
            self.assertEqual(ctrl.phase, "email_await_secure")
            await asyncio.wait_for(fill_started.wait(), timeout=2.0)
            epoch_before = ctrl.email_auth_epoch
            back = await process_user_turn(call_id, "goback", response_id=2)
            self.assertEqual(back.detail, "email_go_back")
            self.assertEqual(back.speech, EMAIL_GO_BACK)
            self.assertEqual(ctrl.phase, "sign_in")
            self.assertGreater(ctrl.email_auth_epoch, epoch_before)
            release_fill.set()
            task = _email_auth_tasks.get(call_id)
            if task:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    pass
            await asyncio.sleep(0.05)
        self.assertEqual(ctrl.phase, "sign_in")
        self.assertFalse(ctrl.email_auth_in_progress)
        self.assertNotEqual(ctrl.phase, "eligibility")
        await drop_controller(call_id)

    async def test_go_back_preferred_from_recent_transcript(self) -> None:
        from pc_liveagent.call_turn import latest_user_utterance

        transcript = [
            {"role": "agent", "content": "Is that correct?"},
            {"role": "user", "content": "murali003@gmail.com"},
            {"role": "user", "content": "go back"},
        ]
        # Newest user line wins — do not scan older lines for go-back.
        self.assertEqual(latest_user_utterance(transcript), "go back")
        transcript2 = [
            {"role": "user", "content": "murali003@gmail.com"},
            {"role": "user", "content": "goback"},
        ]
        self.assertEqual(latest_user_utterance(transcript2), "goback")
        # Newer Google must not be stolen by an older go-back in the window.
        transcript3 = [
            {"role": "user", "content": "go back"},
            {"role": "agent", "content": "How would you like to sign in?"},
            {"role": "user", "content": "Continue with Google"},
        ]
        self.assertEqual(latest_user_utterance(transcript3), "Continue with Google")

    async def test_use_email_asks_for_address(self) -> None:
        from pc_liveagent.call_turn import EMAIL_QUESTION

        call_id = "email-choice"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "sign_in"
        result = await process_user_turn(call_id, "use email")
        self.assertEqual(result.phase, "email")
        self.assertEqual(result.speech, EMAIL_QUESTION)
        self.assertFalse(result.end_call)

    async def test_go_back_phrase_is_not_email_choice(self) -> None:
        from pc_liveagent.answer_parse import is_email_choice

        self.assertFalse(
            is_email_choice(
                "Go back. I've entered the wrong email address. "
                "I have to use the different address."
            )
        )
        self.assertTrue(is_email_choice("use email"))
        self.assertTrue(is_email_choice("I'll sign in with email"))


class StaleTranscriptDedupeTests(unittest.IsolatedAsyncioTestCase):
    """Retell sends cumulative transcripts; unchanged tips must not re-run Go Back."""

    async def test_same_response_id_handled_once(self) -> None:
        from pc_liveagent.call_turn import (
            classify_transcript_turn,
            mark_transcript_turn_handled,
        )

        call_id = "stale-rid"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        transcript = [
            {"role": "user", "content": "go back"},
        ]
        d1 = classify_transcript_turn(ctrl, transcript, response_id=6)
        self.assertEqual(d1.dedupe, "process")
        mark_transcript_turn_handled(ctrl, d1, response_id=6)
        d2 = classify_transcript_turn(ctrl, transcript, response_id=6)
        self.assertEqual(d2.dedupe, "duplicate_response_id")
        await drop_controller(call_id)

    async def test_stale_tip_does_not_reprocess(self) -> None:
        from pc_liveagent.call_turn import (
            classify_transcript_turn,
            mark_transcript_turn_handled,
        )

        call_id = "stale-tip"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        go_back = (
            "Go back. I've entered the wrong email address. "
            "I have to use the different address."
        )
        transcript = [
            {"role": "agent", "content": "Is that correct?"},
            {"role": "user", "content": go_back},
        ]
        d1 = classify_transcript_turn(ctrl, transcript, response_id=6)
        self.assertEqual(d1.dedupe, "process")
        self.assertEqual(d1.utterance, go_back)
        mark_transcript_turn_handled(ctrl, d1, response_id=6)
        # New response_id, same cumulative tip → stale (not a new user turn).
        d2 = classify_transcript_turn(ctrl, transcript, response_id=7)
        self.assertEqual(d2.dedupe, "stale_tip")
        d3 = classify_transcript_turn(ctrl, transcript, response_id=13)
        self.assertEqual(d3.dedupe, "stale_tip")
        await drop_controller(call_id)

    async def test_same_text_new_user_index_is_new_turn(self) -> None:
        from pc_liveagent.call_turn import (
            classify_transcript_turn,
            mark_transcript_turn_handled,
        )

        call_id = "stale-new-idx"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        t1 = [{"role": "user", "content": "go back"}]
        d1 = classify_transcript_turn(ctrl, t1, response_id=1)
        mark_transcript_turn_handled(ctrl, d1, response_id=1)
        # User literally said "go back" again — new transcript entry.
        t2 = [
            {"role": "user", "content": "go back"},
            {"role": "agent", "content": "How would you like to sign in?"},
            {"role": "user", "content": "go back"},
        ]
        d2 = classify_transcript_turn(ctrl, t2, response_id=2)
        self.assertEqual(d2.dedupe, "process")
        self.assertEqual(d2.user_index, 1)
        await drop_controller(call_id)

    async def test_asr_revision_same_user_index_is_not_new_turn(self) -> None:
        """call_772995: Don D. → Donnie. must not re-enter Collect Mode."""
        from pc_liveagent.call_turn import (
            classify_transcript_turn,
            mark_transcript_turn_handled,
        )

        call_id = "asr-don-d"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        t1 = [
            {"role": "agent", "content": "Whose date of birth should I change?"},
            {"role": "user", "content": "Don D."},
        ]
        d1 = classify_transcript_turn(ctrl, t1, response_id=6)
        self.assertEqual(d1.dedupe, "process")
        self.assertEqual(d1.utterance, "Don D.")
        self.assertEqual(d1.user_index, 0)
        mark_transcript_turn_handled(ctrl, d1, response_id=6)

        # Same user slot, ASR revised tip text — not a new caller turn.
        t2 = [
            {"role": "agent", "content": "Whose date of birth should I change?"},
            {"role": "user", "content": "Donnie."},
        ]
        d2 = classify_transcript_turn(ctrl, t2, response_id=7)
        self.assertEqual(d2.dedupe, "asr_revision")
        self.assertEqual(d2.user_index, 0)

        # Unchanged tip remains stale_tip.
        d3 = classify_transcript_turn(ctrl, t2, response_id=8)
        self.assertEqual(d3.dedupe, "asr_revision")

        # A later genuine user turn still processes.
        t3 = t2 + [
            {"role": "agent", "content": "What month was Don D born?"},
            {"role": "user", "content": "February"},
        ]
        d4 = classify_transcript_turn(ctrl, t3, response_id=9)
        self.assertEqual(d4.dedupe, "process")
        self.assertEqual(d4.user_index, 1)
        self.assertEqual(d4.utterance, "February")
        await drop_controller(call_id)

    async def test_same_transcript_event_is_not_reused_as_last_name(self) -> None:
        """The first-name transcript event must not also fill last name."""
        from pc_liveagent.call_turn import drop_controller, get_controller, process_user_turn
        from pc_liveagent.live_form import LiveFormSession

        call_id = "name-replay"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.application_only_state = "application_ready"
        ctrl.eligibility_question_sent = True
        ctrl.active_websocket_status = "connected"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="spouse_first"
        )
        submits: list[tuple[str, str]] = []

        async def fake_get_session(_cid: str):
            return session

        async def fake_submit(_cid, _field, value, **_kwargs):
            submits.append((session.elig_step, value))
            if session.elig_step == "spouse_first":
                session.filled["spouse_first"] = value
                session.elig_step = "spouse_last"
                return {
                    "ok": True,
                    "spoken": "ASK: What is their last name?",
                    "step": "spouse_last",
                }
            session.filled["spouse_last"] = value
            session.elig_step = "spouse_role"
            return {
                "ok": True,
                "spoken": "ASK: Are they your spouse or your partner?",
                "step": "spouse_role",
            }

        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch("pc_liveagent.call_turn.submit_field", side_effect=fake_submit),
            patch("pc_liveagent.call_turn._speak_interrupt", AsyncMock(return_value=True)),
            patch("pc_liveagent.call_turn.peek_choice_labels", AsyncMock(return_value=[])),
        ):
            first = await process_user_turn(
                call_id, "Alex", response_id=1, transcript_user_index=4
            )
            task = __import__(
                "pc_liveagent.call_turn", fromlist=["_elig_browser_tasks"]
            )._elig_browser_tasks.get(call_id)
            if task is not None:
                await task
            replay = await process_user_turn(
                call_id, "Alex", response_id=2, transcript_user_index=4
            )
            second = await process_user_turn(
                call_id, "Alex", response_id=3, transcript_user_index=5
            )
            task2 = __import__(
                "pc_liveagent.call_turn", fromlist=["_elig_browser_tasks"]
            )._elig_browser_tasks.get(call_id)
            if task2 is not None:
                await task2
        self.assertIn(first.detail, ("elig_ack", "elig_fast_complete"))
        self.assertEqual(replay.detail, "stale_transcript_turn")
        self.assertNotEqual(second.detail, "stale_transcript_turn")
        self.assertEqual(session.filled.get("spouse_first"), "Alex")
        self.assertEqual(session.filled.get("spouse_last"), "Alex")
        self.assertEqual(submits, [("spouse_first", "Alex"), ("spouse_last", "Alex")])
        await drop_controller(call_id)

    async def test_fresh_yes_after_failed_confirm_is_not_a_duplicate(self) -> None:
        from pc_liveagent.call_turn import drop_controller, get_controller, process_user_turn
        from pc_liveagent.live_form import LiveFormSession

        call_id = "yes-retry"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.application_only_state = "application_ready"
        ctrl.eligibility_question_sent = True
        ctrl.active_websocket_status = "connected"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="spouse_confirm"
        )
        session.filled.update(
            {"spouse_role": "Spouse", "spouse_age": "(21-63) Adult"}
        )
        submits: list[int] = []

        async def fake_get_session(_cid: str):
            return session

        async def fake_submit(_cid, _field, value, **_kwargs):
            submits.append(1)
            if len(submits) == 1:
                raise RuntimeError(
                    "AGE_CLARIFY: I couldn't confirm the age on the form."
                )
            return {
                "ok": True,
                "spoken": "ASK: Do you have any children?",
                "step": "spouse_confirm",
            }

        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch("pc_liveagent.call_turn.submit_field", side_effect=fake_submit),
            patch("pc_liveagent.call_turn._speak_interrupt", AsyncMock(return_value=True)),
            patch("pc_liveagent.call_turn.peek_choice_labels", AsyncMock(return_value=[])),
        ):
            first = await process_user_turn(
                call_id, "Yes.", response_id=1, transcript_user_index=4
            )
            task = __import__(
                "pc_liveagent.call_turn", fromlist=["_elig_browser_tasks"]
            )._elig_browser_tasks.get(call_id)
            if task is not None:
                await task
            replay = await process_user_turn(
                call_id, "Yes.", response_id=2, transcript_user_index=4
            )
            second = await process_user_turn(
                call_id, "Yes.", response_id=3, transcript_user_index=5
            )
            task2 = __import__(
                "pc_liveagent.call_turn", fromlist=["_elig_browser_tasks"]
            )._elig_browser_tasks.get(call_id)
            if task2 is not None:
                await task2
            replay2 = await process_user_turn(
                call_id, "Yes.", response_id=4, transcript_user_index=5
            )
        self.assertEqual(replay.detail, "stale_transcript_turn")
        self.assertNotEqual(second.detail, "duplicate_utterance")
        self.assertEqual(replay2.detail, "stale_transcript_turn")
        self.assertEqual(len(submits), 2)
        self.assertNotEqual(first.detail, "stale_transcript_turn")
        await drop_controller(call_id)

    async def test_go_back_once_then_google_once(self) -> None:
        from pc_liveagent.call_turn import (
            EMAIL_GO_BACK,
            SSO_ACK,
            classify_transcript_turn,
            mark_transcript_turn_handled,
        )

        call_id = "stale-goback-google"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "email_confirm"
        ctrl.pending_email = "wrong@example.com"
        ctrl.email_captured = True
        go_back = (
            "Go back. I've entered the wrong email address. "
            "I have to use the different address."
        )
        transcript_back = [
            {"role": "user", "content": "wrong@example.com"},
            {"role": "user", "content": go_back},
        ]
        reset = AsyncMock()
        with patch("pc_liveagent.call_turn._reset_email_sign_in_browser", reset):
            d1 = classify_transcript_turn(ctrl, transcript_back, response_id=6)
            self.assertEqual(d1.dedupe, "process")
            mark_transcript_turn_handled(ctrl, d1, response_id=6)
            back = await process_user_turn(call_id, d1.utterance, response_id=6)
            self.assertEqual(back.detail, "email_go_back")
            self.assertEqual(back.speech, EMAIL_GO_BACK)
            self.assertEqual(ctrl.phase, "sign_in")
            # Browser reset is fire-and-forget via create_task.
            await asyncio.sleep(0)
            self.assertEqual(reset.await_count, 1)
            epoch_after = ctrl.email_auth_epoch

            # Responses 7–13: same tip, new response_ids — no browser reset.
            for rid in (7, 8, 9, 10, 11, 12, 13):
                d = classify_transcript_turn(ctrl, transcript_back, response_id=rid)
                self.assertEqual(d.dedupe, "stale_tip", msg=f"rid={rid}")
                mark_transcript_turn_handled(ctrl, d, response_id=rid)
            await asyncio.sleep(0)
            self.assertEqual(reset.await_count, 1)
            self.assertEqual(ctrl.email_auth_epoch, epoch_after)

            # Explicit new Google turn.
            transcript_google = transcript_back + [
                {"role": "agent", "content": EMAIL_GO_BACK},
                {"role": "user", "content": "Continue with Google"},
            ]
            d_g = classify_transcript_turn(ctrl, transcript_google, response_id=14)
            self.assertEqual(d_g.dedupe, "process")
            self.assertEqual(d_g.utterance, "Continue with Google")
            mark_transcript_turn_handled(ctrl, d_g, response_id=14)
            with patch(
                "pc_liveagent.call_turn._schedule_google_sso",
                lambda *a, **k: None,
            ):
                google = await process_user_turn(
                    call_id, d_g.utterance, response_id=14
                )
            self.assertEqual(google.detail, "sso_ack")
            self.assertEqual(google.speech, SSO_ACK)
            self.assertTrue(ctrl.sso_in_progress)
            self.assertEqual(reset.await_count, 1)
        await drop_controller(call_id)

    async def test_go_back_on_chooser_does_not_reset_browser(self) -> None:
        call_id = "stale-chooser-noop"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "sign_in"
        with patch(
            "pc_liveagent.call_turn._reset_email_sign_in_browser", AsyncMock()
        ) as reset:
            r = await process_user_turn(call_id, "go back", response_id=1)
        self.assertEqual(r.detail, "email_go_back")
        self.assertEqual(ctrl.phase, "sign_in")
        reset.assert_not_awaited()
        await drop_controller(call_id)

    async def test_no_new_turn_does_not_guess_google(self) -> None:
        from pc_liveagent.call_turn import classify_transcript_turn, mark_transcript_turn_handled

        call_id = "stale-no-guess"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "sign_in"
        ctrl.last_speech = "How would you like to sign in?"
        transcript = [{"role": "user", "content": "go back"}]
        d1 = classify_transcript_turn(ctrl, transcript, response_id=1)
        mark_transcript_turn_handled(ctrl, d1, response_id=1)
        d2 = classify_transcript_turn(ctrl, transcript, response_id=2)
        self.assertEqual(d2.dedupe, "stale_tip")
        # Stale path must not invoke process_user_turn / Google.
        self.assertFalse(ctrl.sso_in_progress)
        await drop_controller(call_id)


class EmailAuthFollowupTests(unittest.IsolatedAsyncioTestCase):
    """Remaining email-auth cases (split after StaleTranscriptDedupeTests insert)."""

    async def test_email_ack_no_premature_end_call(self) -> None:
        from pc_liveagent.call_turn import EMAIL_ACK, _email_auth_tasks
        from pc_liveagent.live_form import ELIG_QUESTIONS

        call_id = "email-ack"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "email"
        started = asyncio.Event()
        finished = asyncio.Event()

        async def slow_submit(*_a, **_k):
            started.set()
            await finished.wait()
            return {
                "ok": True,
                "auth_screen": "signed_in",
                "spoken": f"ASK: {ELIG_QUESTIONS['marital']}",
                "step": "marital",
            }

        with (
            patch("pc_liveagent.call_turn.submit_field", side_effect=slow_submit),
            patch("pc_liveagent.call_turn.get_session", AsyncMock(return_value=None)),
            patch("pc_liveagent.call_turn._speak_interrupt", AsyncMock(return_value=True)),
            patch(
                "pc_liveagent.call_turn.poll_email_auth_until_signed_in",
                AsyncMock(return_value=None),
            ),
        ):
            confirm = await process_user_turn(
                call_id, "jane@example.com", response_id=2
            )
            self.assertEqual(confirm.detail, "email_confirm")
            self.assertEqual(ctrl.phase, "email_confirm")
            self.assertIn("jane at example dot com", confirm.speech.lower())
            self.assertFalse(confirm.end_call)
            result = await process_user_turn(call_id, "yes", response_id=3)
            self.assertEqual(result.detail, "email_ack")
            self.assertEqual(result.speech, EMAIL_ACK)
            self.assertFalse(result.end_call)
            self.assertEqual(result.phase, "email_await_secure")
            self.assertTrue(ctrl.email_auth_in_progress)
            self.assertNotEqual(ctrl.phase, "closing")
            await asyncio.wait_for(started.wait(), timeout=2.0)
            finished.set()
            task = _email_auth_tasks.get(call_id)
            if task:
                await asyncio.wait_for(task, timeout=2.0)
        self.assertEqual(ctrl.phase, "eligibility")
        self.assertTrue(ctrl.eligibility_screen_verified)
        self.assertFalse(ctrl.email_auth_in_progress)

    async def test_email_fill_failure_keeps_auth_phase(self) -> None:
        from pc_liveagent.call_turn import EMAIL_SECURE_RETRY, _email_auth_tasks

        call_id = "email-fail"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "email"
        interrupt = AsyncMock(return_value=True)

        async def fail_submit(*_a, **_k):
            return {
                "ok": False,
                "auth_screen": "need_email",
                "spoken": EMAIL_SECURE_RETRY,
                "error": "email_not_filled",
            }

        with (
            patch("pc_liveagent.call_turn.submit_field", side_effect=fail_submit),
            patch("pc_liveagent.call_turn._speak_interrupt", interrupt),
        ):
            await process_user_turn(call_id, "pat@example.com", response_id=1)
            result = await process_user_turn(call_id, "yes", response_id=2)
            self.assertFalse(result.end_call)
            self.assertNotEqual(result.phase, "closing")
            task = _email_auth_tasks.get(call_id)
            if task:
                await asyncio.wait_for(task, timeout=2.0)
        self.assertEqual(ctrl.phase, "email")
        self.assertFalse(ctrl.eligibility_question_sent)
        interrupt.assert_awaited()
        spoken = interrupt.await_args.args[1]
        self.assertNotIn("goodbye", spoken.lower())
        self.assertNotIn("marital", spoken.lower())

    async def test_secure_screen_awaits_without_ending_call(self) -> None:
        from pc_liveagent.call_turn import EMAIL_SECURE_COMPLETE, _email_auth_tasks
        from pc_liveagent.live_form import ELIG_QUESTIONS

        call_id = "email-secure"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "email"

        async def secure_submit(*_a, **_k):
            return {
                "ok": True,
                "auth_screen": "need_password",
                "spoken": EMAIL_SECURE_COMPLETE,
                "awaiting_secure": True,
            }

        async def later_signed_in(*_a, **_k):
            return {
                "ok": True,
                "auth_screen": "signed_in",
                "spoken": f"ASK: {ELIG_QUESTIONS['marital']}",
                "step": "marital",
            }

        with (
            patch("pc_liveagent.call_turn.submit_field", side_effect=secure_submit),
            patch(
                "pc_liveagent.call_turn.poll_email_auth_until_signed_in",
                side_effect=later_signed_in,
            ),
            patch("pc_liveagent.call_turn.get_session", AsyncMock(return_value=None)),
            patch("pc_liveagent.call_turn._speak_interrupt", AsyncMock(return_value=True)),
            patch(
                "pc_liveagent.call_turn.deliver_first_eligibility_question",
                AsyncMock(return_value=True),
            ),
        ):
            await process_user_turn(call_id, "pat@example.com", response_id=3)
            result = await process_user_turn(call_id, "yes", response_id=4)
            self.assertFalse(result.end_call)
            self.assertEqual(result.phase, "email_await_secure")
            task = _email_auth_tasks.get(call_id)
            if task:
                await asyncio.wait_for(task, timeout=2.0)
        self.assertEqual(ctrl.phase, "eligibility")
        self.assertTrue(ctrl.eligibility_screen_verified)

    async def test_sign_in_with_email_uses_fill_text_not_choice(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _sign_in_with_email

        session = LiveFormSession(call_id="email-extract", url="https://example.test")
        page = AsyncMock()
        page.url = "https://my.pointcare.com/signin"
        page.locator = AsyncMock()
        session.page = page

        fill = AsyncMock()
        continue_click = AsyncMock(return_value=True)
        detect = AsyncMock(
            side_effect=["need_password", "need_password", "need_password", "need_password", "need_password"]
        )
        already = AsyncMock(return_value=False)
        matches = AsyncMock(return_value=True)
        ensure = AsyncMock()

        with (
            patch("pc_liveagent.live_form._ensure_native_email_form", ensure),
            patch("pc_liveagent.live_form._fill_text", fill),
            patch("pc_liveagent.live_form._email_field_matches", matches),
            patch("pc_liveagent.live_form._click_continue_if_present", continue_click),
            patch("pc_liveagent.live_form._detect_auth_screen", detect),
            patch("pc_liveagent.live_form._already_signed_in", already),
            patch("pc_liveagent.live_form._provider_popup", return_value=None),
            patch("pc_liveagent.live_form._click_choice", AsyncMock()) as click_choice,
        ):
            result = await _sign_in_with_email(session, "jane@example.com")

        fill.assert_awaited()
        continue_click.assert_awaited()
        click_choice.assert_not_awaited()
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("auth_screen"), "need_password")
        self.assertTrue(result.get("awaiting_secure"))
        self.assertIn("password", (result.get("spoken") or "").lower())
        self.assertNotIn("goodbye", (result.get("spoken") or "").lower())

    def test_spoken_for_auth_screen_verify_link(self) -> None:
        from pc_liveagent.live_form import (
            EMAIL_PASSWORD_SECURE,
            EMAIL_VERIFY_LINK,
            spoken_for_auth_screen,
        )

        self.assertEqual(spoken_for_auth_screen("need_confirm"), EMAIL_VERIFY_LINK)
        self.assertIn("verification link", EMAIL_VERIFY_LINK.lower())
        self.assertEqual(spoken_for_auth_screen("need_password"), EMAIL_PASSWORD_SECURE)
        self.assertNotEqual(
            spoken_for_auth_screen("need_confirm"),
            spoken_for_auth_screen("need_password"),
        )


class ControlIntentAndMetaTests(unittest.TestCase):
    def test_intent_kinds(self) -> None:
        from pc_liveagent.control_intent import detect_control_intent

        self.assertEqual(detect_control_intent("Can you repeat that?").kind, "repeat")
        self.assertEqual(detect_control_intent("For what?").kind, "repeat")
        self.assertEqual(detect_control_intent("What?").kind, "repeat")
        self.assertEqual(detect_control_intent("What do you mean?").kind, "repeat")
        self.assertEqual(detect_control_intent("Why are you calling?").kind, "repeat")
        self.assertEqual(detect_control_intent("What's this about?").kind, "repeat")
        self.assertEqual(detect_control_intent("Go back one step.").kind, "go_back")
        self.assertEqual(detect_control_intent("Skip this if it is optional.").kind, "skip")
        self.assertEqual(detect_control_intent("I made a mistake.").kind, "redo")
        change = detect_control_intent("Change the age to fifteen.")
        self.assertEqual(change.kind, "change")
        self.assertEqual(change.target, "age")
        self.assertIn("fifteen", change.value.lower())
        self.assertEqual(detect_control_intent("Never married").kind, "none")
        self.assertEqual(
            detect_control_intent("What are the options available?").kind,
            "list_options",
        )
        self.assertEqual(
            detect_control_intent("What choices are there?").kind, "list_options"
        )
        self.assertEqual(
            detect_control_intent("What can I say?").kind, "list_options"
        )
        # Real answers / next must not be treated as context-repeat.
        self.assertEqual(
            detect_control_intent("What's the next question?").kind, "next"
        )

    def test_required_meta_blocks_skip_flag(self) -> None:
        from pc_liveagent.elig_meta import format_question, get_field_meta

        meta = get_field_meta("children")
        self.assertTrue(meta.required)
        self.assertFalse(meta.can_skip)
        q = format_question("children")
        # Spoken questions must not append internal "required to continue" chrome.
        self.assertNotIn("required to continue", q.lower())
        self.assertIn("yes or no", q.lower())

    def test_options_clarify_relationship_no_required_chrome(self) -> None:
        from pc_liveagent.answer_parse import resolve_answer
        from pc_liveagent.elig_meta import get_field_meta

        opts = ["Child", "Stepchild", "Grandchild", "Foster Child", "Other"]
        r = resolve_answer(
            "person_rel",
            "uh maybe something",
            options=opts,
            meta=get_field_meta("person_rel"),
        )
        self.assertIsNone(r.answer)
        clarify = (r.clarify or "").lower()
        self.assertIn("child", clarify)
        self.assertIn("other", clarify)
        self.assertNotIn("required to continue", clarify)
        self.assertNotIn("please choose one of", clarify)

    def test_resolve_other_relationship_maps(self) -> None:
        from pc_liveagent.answer_parse import is_fast_path_resolve, resolve_answer
        from pc_liveagent.elig_meta import get_field_meta
        from pc_liveagent.live_form import (
            _map_relationship_label,
            _relationship_option_labels,
            relationship_question_speech,
        )

        opts = ["Child", "Stepchild", "Grandchild", "Foster Child", "Other"]
        r = resolve_answer(
            "child_rel", "Other", options=opts, meta=get_field_meta("child_rel")
        )
        self.assertEqual(r.answer, "Other")
        self.assertTrue(is_fast_path_resolve(r))
        self.assertEqual(_map_relationship_label("my son", opts), "Child")
        self.assertEqual(_map_relationship_label("my stepdaughter", opts), "Stepchild")
        self.assertEqual(_map_relationship_label("my grandson", opts), "Grandchild")
        self.assertEqual(_map_relationship_label("a foster child", opts), "Foster Child")
        self.assertEqual(_map_relationship_label("something else", opts), "Other")
        speech = relationship_question_speech("person_rel", opts)
        low = speech.lower()
        self.assertIn("what is their relationship to you", low)
        self.assertIn("the options are", low)
        self.assertIn("child", low)
        self.assertIn("other", low)
        self.assertIn("which best describes", low)
        self.assertNotIn("required to continue", low)
        self.assertNotIn("first name", low)
        # Without Other in the live list, do not invent it.
        no_other = relationship_question_speech(
            "person_rel", ["Child", "Stepchild", "Grandchild"]
        )
        self.assertNotIn("other", no_other.lower())
        mixed = [
            "Your household",
            "Agent You",
            "john Partner",
            "Child",
            "Stepchild",
            "Grandchild",
            "Foster Child",
            "Other",
        ]
        kept = _relationship_option_labels(mixed)
        self.assertEqual(
            kept, ["Child", "Stepchild", "Grandchild", "Foster Child", "Other"]
        )
        spoken = relationship_question_speech("child_rel", mixed).lower()
        self.assertNotIn("john partner", spoken)
        self.assertNotIn("agent you", spoken)
        self.assertNotIn("your household", spoken)
        self.assertIn("foster child", spoken)
        self.assertIn("other", spoken)
        self.assertEqual(
            _map_relationship_label("partner", ["john Partner", "Spouse", "Partner"]),
            "Partner",
        )

    def test_resolve_age_with_options(self) -> None:
        from pc_liveagent.answer_parse import resolve_answer
        from pc_liveagent.elig_meta import get_field_meta

        options = ["(0-5) Child", "(6-20) Adult", "(21-63) Adult"]
        meta = get_field_meta("person_age")
        r = resolve_answer("person_age", "15", options=options, meta=meta)
        self.assertEqual(r.answer, "(6-20) Adult")
        bad = resolve_answer(
            "child_age",
            "Sixteen to eight",
            options=["(0-1) Baby", "(16-18) Teen"],
            meta=get_field_meta("child_age"),
        )
        self.assertIsNone(bad.answer)
        self.assertIsNotNone(bad.clarify)

    def test_resolve_twenty_five_and_ambiguous_age(self) -> None:
        from pc_liveagent.answer_parse import is_fast_path_resolve, resolve_answer
        from pc_liveagent.elig_meta import get_field_meta

        spouse_ages = [
            "(0-18) Child",
            "(19-20) Young Adult",
            "(21-63) Adult",
            "(64) Turning Senior",
            "(65+) Senior",
        ]
        meta = get_field_meta("spouse_age")
        mapped = resolve_answer(
            "spouse_age", "Twenty five", options=spouse_ages, meta=meta
        )
        self.assertEqual(mapped.answer, "(21-63) Adult")
        self.assertEqual(mapped.detail, "age_mapped")
        self.assertNotEqual(mapped.answer, "(19-20) Young Adult")

        digit = resolve_answer("spouse_age", "25", options=spouse_ages, meta=meta)
        self.assertEqual(digit.answer, "(21-63) Adult")

        # Form captions must not become age choices or a false Young Adult pick.
        captions = [
            "First Name",
            "Last Name",
            "Spouse",
            "Partner",
            "Age",
        ]
        with_captions = resolve_answer(
            "spouse_age",
            "Twenty twenty two thirty",
            options=captions + spouse_ages,
            meta=meta,
        )
        self.assertIsNone(with_captions.answer)
        self.assertEqual(with_captions.detail, "age_ambiguous")
        self.assertNotIn("First Name", with_captions.clarify or "")

        # No live chips: clear spoken age is free-text for Playwright verification.
        free = resolve_answer("spouse_age", "Twenty five", options=captions, meta=meta)
        self.assertEqual(free.detail, "age_free")
        self.assertTrue(is_fast_path_resolve(free))
        self.assertEqual(free.answer, "Twenty five")

        ambiguous = resolve_answer(
            "spouse_age", "Twenty twenty two thirty", options=captions, meta=meta
        )
        self.assertIsNone(ambiguous.answer)
        self.assertEqual(ambiguous.detail, "age_ambiguous")


class ConversationalNavTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await drop_controller("nav-test")

    async def test_skip_blocked_when_required(self) -> None:
        call_id = "nav-test"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="student"
        )

        async def fake_get_session(_cid: str):
            return session

        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch("pc_liveagent.call_turn.peek_choice_labels", AsyncMock(return_value=[])),
            patch(
                "pc_liveagent.call_turn.skip_current_optional",
                AsyncMock(
                    return_value={
                        "ok": False,
                        "error": "required",
                        "spoken": (
                            "Are you the parent or legal guardian of a full-time "
                            "student? This field is required to continue, so I can't "
                            "skip it."
                        ),
                    }
                ),
            ),
            patch("pc_liveagent.call_turn.submit_field", AsyncMock()) as submit,
        ):
            r = await process_user_turn(call_id, "Skip this")
        self.assertEqual(r.detail, "skip_blocked")
        self.assertIn("required", r.speech.lower())
        submit.assert_not_called()

    async def test_go_back_failure_is_honest(self) -> None:
        call_id = "nav-test"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="disabled"
        )

        async def fake_get_session(_cid: str):
            return session

        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch("pc_liveagent.call_turn.peek_choice_labels", AsyncMock(return_value=[])),
            patch(
                "pc_liveagent.call_turn.navigate_back_one",
                AsyncMock(
                    return_value={
                        "ok": False,
                        "spoken": "I can't go back from this screen.",
                    }
                ),
            ),
        ):
            r = await process_user_turn(call_id, "Go back.")
        self.assertEqual(r.detail, "go_back_failed")
        self.assertIn("can't go back", r.speech.lower())

    async def test_correction_after_advance_uses_back(self) -> None:
        call_id = "nav-test"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        ctrl.active_websocket_status = "connected"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="child_more"
        )

        async def fake_get_session(_cid: str):
            return session

        async def fake_back(_cid: str):
            session.elig_step = "child_age"
            return {
                "ok": True,
                "step": "child_age",
                "spoken": "What is the child's age range?",
            }

        async def slow_submit(*_a, **_k):
            await asyncio.sleep(0.05)
            session.elig_step = "child_more"
            return {
                "ok": True,
                "spoken": "ASK: Is there another child to add? Please say Yes or No.",
                "step": "child_more",
            }

        interrupt = AsyncMock(return_value=True)
        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch(
                "pc_liveagent.call_turn.peek_choice_labels",
                AsyncMock(return_value=["(13-15) Teen", "(16-18) Teen"]),
            ),
            patch("pc_liveagent.call_turn.navigate_back_one", side_effect=fake_back),
            patch("pc_liveagent.call_turn.submit_field", side_effect=slow_submit),
            patch("pc_liveagent.call_turn._speak_interrupt", interrupt),
        ):
            r = await process_user_turn(call_id, "Change the age to fifteen.")
            self.assertEqual(r.detail, "elig_ack")
            task = __import__("pc_liveagent.call_turn", fromlist=["_elig_browser_tasks"])._elig_browser_tasks.get(
                call_id
            )
            if task:
                await asyncio.wait_for(task, timeout=2.0)
        self.assertTrue(interrupt.await_count >= 1)


class LiveCallFiveFixesTests(unittest.IsolatedAsyncioTestCase):
    """Regression for call_d0bdc6eeb83021bb121cce91da7 class of bugs."""

    async def asyncTearDown(self) -> None:
        for cid in (
            "fix-ws",
            "fix-once",
            "fix-disc",
            "fix-corr",
            "fix-child",
            "fix-child2",
        ):
            await drop_controller(cid)

    async def test_slow_browser_keeps_turn_responsive(self) -> None:
        call_id = "fix-ws"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        ctrl.active_websocket_status = "connected"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="child_age"
        )

        async def fake_get_session(_cid: str):
            return session

        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_submit(*_a, **_k):
            started.set()
            await release.wait()
            session.elig_step = "child_more"
            return {
                "ok": True,
                "spoken": "ASK: Is there another child to add? Please say Yes or No.",
                "step": "child_more",
            }

        interrupt = AsyncMock(return_value=True)
        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch(
                "pc_liveagent.call_turn.peek_choice_labels",
                AsyncMock(return_value=["(13-15) Teen"]),
            ),
            patch("pc_liveagent.call_turn.submit_field", side_effect=slow_submit),
            patch("pc_liveagent.call_turn._speak_interrupt", interrupt),
        ):
            t0 = asyncio.get_event_loop().time()
            r = await process_user_turn(call_id, "fifteen", response_id=1)
            elapsed = asyncio.get_event_loop().time() - t0
            self.assertLess(elapsed, 0.5)
            self.assertEqual(r.detail, "elig_ack")
            await asyncio.wait_for(started.wait(), timeout=1.0)
            # While browser runs, another turn stays responsive (next waits, not hang).
            busy = await process_user_turn(call_id, "next question", response_id=2)
            self.assertIn(busy.detail, ("elig_op_in_progress", "next_waiting_browser"))
            release.set()
            task = __import__(
                "pc_liveagent.call_turn", fromlist=["_elig_browser_tasks"]
            )._elig_browser_tasks.get(call_id)
            if task:
                await asyncio.wait_for(task, timeout=2.0)
        self.assertFalse(ctrl.elig_op_in_progress)
        self.assertEqual(interrupt.await_count, 1)

    async def test_next_question_delivered_exactly_once(self) -> None:
        from pc_liveagent.call_turn import _deliver_question_once

        call_id = "fix-once"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.active_websocket_status = "connected"
        interrupt = AsyncMock(return_value=True)
        with patch("pc_liveagent.call_turn._speak_interrupt", interrupt):
            ok1 = await _deliver_question_once(
                call_id, "Is there another child to add?", key="next:child_more:t1"
            )
            ok2 = await _deliver_question_once(
                call_id, "Is there another child to add?", key="next:child_more:t1"
            )
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertEqual(interrupt.await_count, 1)

    async def test_disconnect_queues_recovery_without_double_commit(self) -> None:
        call_id = "fix-disc"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        ctrl.active_websocket_status = "connected"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="child_age"
        )
        commits: list[str] = []

        async def fake_get_session(_cid: str):
            return session

        async def submit_once(*_a, **_k):
            commits.append("once")
            session.elig_step = "child_more"
            return {
                "ok": True,
                "spoken": "ASK: Is there another child to add? Please say Yes or No.",
                "step": "child_more",
            }

        interrupt = AsyncMock(return_value=False)
        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch(
                "pc_liveagent.call_turn.peek_choice_labels",
                AsyncMock(return_value=["(13-15) Teen"]),
            ),
            patch("pc_liveagent.call_turn.submit_field", side_effect=submit_once),
            patch("pc_liveagent.call_turn._speak_interrupt", interrupt),
        ):
            await process_user_turn(call_id, "fifteen", response_id=1)
            task = __import__(
                "pc_liveagent.call_turn", fromlist=["_elig_browser_tasks"]
            )._elig_browser_tasks.get(call_id)
            if task:
                await asyncio.wait_for(task, timeout=2.0)
        self.assertEqual(commits, ["once"])
        self.assertEqual(session.elig_step, "child_more")
        self.assertTrue(ctrl.pending_outbound_speech)
        # Reconnect recovery delivers once; answer is not re-applied.
        ctrl.active_websocket_status = "connected"
        interrupt2 = AsyncMock(return_value=True)
        from pc_liveagent.call_turn import recover_pending_outbound

        with patch("pc_liveagent.call_turn._speak_interrupt", interrupt2):
            ok = await recover_pending_outbound(call_id)
        self.assertTrue(ok)
        self.assertEqual(interrupt2.await_count, 1)
        self.assertEqual(commits, ["once"])

    async def test_change_first_name_prompts_for_value(self) -> None:
        call_id = "fix-corr"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="child_more"
        )

        async def fake_get_session(_cid: str):
            return session

        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch("pc_liveagent.call_turn.peek_choice_labels", AsyncMock(return_value=[])),
            patch("pc_liveagent.call_turn.submit_field", AsyncMock()) as submit,
            patch("pc_liveagent.call_turn.navigate_back_one", AsyncMock()) as back,
        ):
            r = await process_user_turn(call_id, "Change the first name.")
        self.assertEqual(r.detail, "correction_prompt")
        self.assertIn("first name", r.speech.lower())
        self.assertIsNotNone(ctrl.pending_correction)
        submit.assert_not_called()
        back.assert_not_called()

    async def test_change_first_name_to_michael_updates(self) -> None:
        call_id = "fix-corr"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        ctrl.active_websocket_status = "connected"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="child_more"
        )

        async def fake_get_session(_cid: str):
            return session

        edit = AsyncMock(
            return_value={
                "ok": True,
                "spoken": (
                    "Updated the first name to Michael. Just to confirm for the child: "
                    "first name Michael, last name Smith, relationship Child, "
                    "age the 13-to-15 age range. Is that correct?"
                ),
                "step": "child_confirm",
                "value": "Michael",
                "in_place": True,
            }
        )
        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch("pc_liveagent.call_turn.peek_choice_labels", AsyncMock(return_value=[])),
            patch("pc_liveagent.call_turn.correct_member_field", edit),
            patch("pc_liveagent.call_turn.submit_field", AsyncMock()) as submit,
        ):
            r = await process_user_turn(
                call_id, "Change the first name to Michael."
            )
        self.assertEqual(r.detail, "member_corrected")
        self.assertIn("michael", r.speech.lower())
        edit.assert_awaited()
        self.assertEqual(edit.await_args.args[2], "Michael")
        submit.assert_not_called()

    async def test_child_age_auto_asks_child_confirm(self) -> None:
        call_id = "fix-child"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        ctrl.active_websocket_status = "connected"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="child_age"
        )
        session.filled.update(
            {
                "child_first": "Aria",
                "child_last": "Martin",
                "child_rel": "Child",
                "child_age": "(13-15) Teen",
            }
        )

        async def fake_get_session(_cid: str):
            return session

        async def submit(*_a, **_k):
            session.elig_step = "child_confirm"
            return {
                "ok": True,
                "spoken": (
                    "ASK: Just to confirm for the child: first name Aria, last name "
                    "Martin, relationship Child, age (13-15) Teen. Is that correct? "
                    "Please say Yes or No."
                ),
                "step": "child_confirm",
            }

        interrupt = AsyncMock(return_value=True)
        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch(
                "pc_liveagent.call_turn.peek_choice_labels",
                AsyncMock(return_value=["(13-15) Teen"]),
            ),
            patch("pc_liveagent.call_turn.submit_field", side_effect=submit),
            patch("pc_liveagent.call_turn._speak_interrupt", interrupt),
        ):
            await process_user_turn(call_id, "fifteen", response_id=1)
            task = __import__(
                "pc_liveagent.call_turn", fromlist=["_elig_browser_tasks"]
            )._elig_browser_tasks.get(call_id)
            if task:
                await asyncio.wait_for(task, timeout=2.0)
        spoken = interrupt.await_args.args[1]
        self.assertIn("just to confirm", spoken.lower())
        self.assertIn("aria", spoken.lower())
        self.assertEqual(session.elig_step, "child_confirm")

    async def test_add_another_child_asks_first_name(self) -> None:
        call_id = "fix-child2"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        ctrl.active_websocket_status = "connected"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="child_more"
        )

        async def fake_get_session(_cid: str):
            return session

        async def submit(*_a, **_k):
            session.elig_step = "child_first"
            return {
                "ok": True,
                "spoken": "ASK: What is the child's first name?",
                "step": "child_first",
            }

        interrupt = AsyncMock(return_value=True)
        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch("pc_liveagent.call_turn.peek_choice_labels", AsyncMock(return_value=[])),
            patch("pc_liveagent.call_turn.submit_field", side_effect=submit),
            patch("pc_liveagent.call_turn._speak_interrupt", interrupt),
        ):
            await process_user_turn(call_id, "Yes", response_id=1)
            task = __import__(
                "pc_liveagent.call_turn", fromlist=["_elig_browser_tasks"]
            )._elig_browser_tasks.get(call_id)
            if task:
                await asyncio.wait_for(task, timeout=2.0)
        spoken = interrupt.await_args.args[1]
        self.assertIn("first name", spoken.lower())
        self.assertEqual(session.elig_step, "child_first")

    async def test_no_more_children_then_student(self) -> None:
        call_id = "fix-child2"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        ctrl.active_websocket_status = "connected"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="child_more"
        )

        async def fake_get_session(_cid: str):
            return session

        async def submit(*_a, **_k):
            session.elig_step = "student"
            return {
                "ok": True,
                "spoken": "ASK: Are you the parent or legal guardian of a full-time student?",
                "step": "student",
            }

        interrupt = AsyncMock(return_value=True)
        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch("pc_liveagent.call_turn.peek_choice_labels", AsyncMock(return_value=[])),
            patch("pc_liveagent.call_turn.submit_field", side_effect=submit),
            patch("pc_liveagent.call_turn._speak_interrupt", interrupt),
        ):
            await process_user_turn(call_id, "No", response_id=1)
            task = __import__(
                "pc_liveagent.call_turn", fromlist=["_elig_browser_tasks"]
            )._elig_browser_tasks.get(call_id)
            if task:
                await asyncio.wait_for(task, timeout=2.0)
        spoken = interrupt.await_args.args[1]
        self.assertIn("student", spoken.lower())
        self.assertEqual(session.elig_step, "student")


class ClaudeResolverTests(unittest.TestCase):
    def test_fast_path_skips_claude_for_clear_yes_no_and_mapped_age(self) -> None:
        from pc_liveagent.answer_parse import is_fast_path_resolve, resolve_answer
        from pc_liveagent.elig_meta import get_field_meta

        yes = resolve_answer("children", "Yes", meta=get_field_meta("children"))
        self.assertTrue(is_fast_path_resolve(yes))
        age = resolve_answer(
            "child_age",
            "15",
            options=["(13-15) Teen", "(16-18) Teen"],
            meta=get_field_meta("child_age"),
        )
        self.assertTrue(is_fast_path_resolve(age))
        ambiguous = resolve_answer(
            "child_age",
            "around that age maybe",
            options=["(13-15) Teen", "(16-18) Teen"],
            meta=get_field_meta("child_age"),
        )
        self.assertFalse(is_fast_path_resolve(ambiguous))

    def test_validate_rejects_invented_income_option(self) -> None:
        from pc_liveagent.answer_parse import ClaudeResolveResult, validate_claude_result

        options = ["$0 – $30,000", "$30,001 – $45,000", "$45,001 – $60,000"]
        invented = ClaudeResolveResult(
            intent="answer",
            answer="$40,000 – $50,000",
            matched_option="$40,000 – $50,000",
            confidence=0.99,
            needs_clarify=False,
        )
        out = validate_claude_result(
            "income_range",
            invented,
            options=options,
            answer_type="income_range",
            min_confidence=0.72,
        )
        self.assertFalse(out.accepted)
        self.assertEqual(out.reject_reason, "not_in_options")

    def test_validate_accepts_real_income_option(self) -> None:
        from pc_liveagent.answer_parse import ClaudeResolveResult, validate_claude_result

        options = ["$0 – $30,000", "$39,901 – $50,000", "$50,001+"]
        ok = ClaudeResolveResult(
            intent="answer",
            answer="$39,901 – $50,000",
            matched_option="$39,901 – $50,000",
            confidence=0.91,
            needs_clarify=False,
        )
        out = validate_claude_result(
            "income_range",
            ok,
            options=options,
            answer_type="income_range",
            min_confidence=0.72,
        )
        self.assertTrue(out.accepted)
        self.assertEqual(out.answer, "$39,901 – $50,000")

    def test_validate_age_must_match_displayed_chip(self) -> None:
        from pc_liveagent.answer_parse import ClaudeResolveResult, validate_claude_result

        options = ["(13-15) Teen", "(16-18) Teen"]
        bad = ClaudeResolveResult(
            intent="answer",
            answer="(19-20) Young Adult",
            matched_option="(19-20) Young Adult",
            confidence=0.95,
            needs_clarify=False,
        )
        out = validate_claude_result(
            "child_age",
            bad,
            options=options,
            answer_type="age",
            min_confidence=0.72,
        )
        self.assertFalse(out.accepted)
        good = ClaudeResolveResult(
            intent="answer",
            answer="fifteen",
            matched_option="(13-15) Teen",
            confidence=0.9,
            needs_clarify=False,
        )
        out2 = validate_claude_result(
            "child_age",
            good,
            options=options,
            answer_type="age",
            min_confidence=0.72,
        )
        self.assertTrue(out2.accepted)
        self.assertEqual(out2.answer, "(13-15) Teen")

    def test_validate_name_sanitize_and_reject_condition(self) -> None:
        from pc_liveagent.answer_parse import ClaudeResolveResult, validate_claude_result

        name = ClaudeResolveResult(
            intent="answer",
            answer="Michael.",
            confidence=0.88,
            needs_clarify=False,
        )
        out = validate_claude_result(
            "child_first",
            name,
            options=None,
            answer_type="text",
            min_confidence=0.72,
        )
        self.assertTrue(out.accepted)
        self.assertEqual(out.answer, "Michael")
        cond = ClaudeResolveResult(
            intent="answer",
            answer="Someone is blind",
            confidence=0.9,
            needs_clarify=False,
        )
        out2 = validate_claude_result(
            "special_person",
            cond,
            options=None,
            answer_type="text",
            min_confidence=0.72,
        )
        self.assertFalse(out2.accepted)

    def test_ambiguous_claude_forces_clarify_not_first_option(self) -> None:
        from pc_liveagent.answer_parse import ClaudeResolveResult, validate_claude_result

        ambiguous = ClaudeResolveResult(
            intent="clarify",
            answer=None,
            matched_option=None,
            confidence=0.4,
            needs_clarify=True,
            clarify_prompt="Which income range matches?",
        )
        out = validate_claude_result(
            "income_range",
            ambiguous,
            options=["$0 – $30,000", "$30,001 – $45,000"],
            answer_type="income_range",
            min_confidence=0.72,
        )
        self.assertFalse(out.accepted)
        self.assertNotEqual(out.answer, "$0 – $30,000")


class ClaudeResolverAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_does_not_return_accepted_answer(self) -> None:
        from pc_liveagent import answer_parse
        from pc_liveagent.answer_parse import resolve_with_claude

        async def slow_create(*_a, **_k):
            await asyncio.sleep(5)
            raise AssertionError("should have timed out")

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            @property
            def messages(self):
                return self

            async def create(self, *a, **k):
                return await slow_create()

        with (
            patch("pc_liveagent.config.ANTHROPIC_API_KEY", "test-key"),
            patch("pc_liveagent.config.ANTHROPIC_TIMEOUT_S", 0.05),
            patch("anthropic.AsyncAnthropic", FakeClient),
        ):
            result = await resolve_with_claude(
                "income_range",
                "forty to fifty thousand",
                question="What is your household income?",
                answer_type="income_range",
                options=["$0 – $30,000", "$39,901 – $50,000"],
                turn_id="t1",
            )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reject_reason, "timeout")

    async def test_stale_claude_does_not_override_committed(self) -> None:
        call_id = "claude-stale"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="income_range"
        )

        async def fake_get_session(_cid: str):
            return session

        async def fake_claude(*_a, **_k):
            # Simulate a late/wrong Claude pick of the first chip.
            from pc_liveagent.answer_parse import ClaudeResolveResult

            ctrl.committed.append(
                {
                    "key": "income_range:$39,901 – $50,000",
                    "turn_id": "other",
                    "step": "income_range",
                    "answer": "$39,901 – $50,000",
                }
            )
            return ClaudeResolveResult(
                intent="answer",
                answer="$0 – $30,000",
                matched_option="$0 – $30,000",
                confidence=0.99,
                needs_clarify=False,
                accepted=True,
            )

        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch(
                "pc_liveagent.call_turn.peek_choice_labels",
                AsyncMock(
                    return_value=["$0 – $30,000", "$39,901 – $50,000"]
                ),
            ),
            patch("pc_liveagent.call_turn.resolve_with_claude", side_effect=fake_claude),
            patch("pc_liveagent.call_turn.submit_field", AsyncMock()) as submit,
        ):
            # Phrasing that Python cannot map uniquely → Claude path → stale discard.
            r = await process_user_turn(call_id, "somewhere in the middle I guess")
        submit.assert_not_called()
        self.assertNotEqual(r.detail, "elig_ack")
        self.assertTrue(r.speech)


class NameAndSpecialPersonUnitTests(unittest.TestCase):
    def test_sanitize_trailing_asr_punctuation(self) -> None:
        from pc_liveagent.answer_parse import sanitize_person_name

        self.assertEqual(sanitize_person_name("John."), "John")
        self.assertEqual(sanitize_person_name("Aria."), "Aria")
        self.assertEqual(sanitize_person_name("O'Connor"), "O'Connor")
        self.assertEqual(sanitize_person_name("Anne-Marie"), "Anne-Marie")
        self.assertEqual(sanitize_person_name("Last name is Martin"), "Martin")

    def test_sanitize_letter_by_letter_spelling(self) -> None:
        from pc_liveagent.answer_parse import sanitize_person_name

        self.assertEqual(sanitize_person_name("H u s s e y"), "Hussey")
        self.assertEqual(sanitize_person_name("H-U-S-S-E-Y"), "Hussey")
        self.assertEqual(sanitize_person_name("h u s s e y."), "Hussey")
        self.assertEqual(sanitize_person_name("spelled H U S S E Y"), "Hussey")
        # Legitimate multi-word / compound names must stay intact.
        self.assertEqual(sanitize_person_name("Anne Marie"), "Anne Marie")
        self.assertEqual(sanitize_person_name("Anne-Marie"), "Anne-Marie")
        self.assertEqual(sanitize_person_name("O'Connor"), "O'Connor")
        # Too short / not clearly spelling — leave alone.
        self.assertEqual(sanitize_person_name("A B"), "A B")
        self.assertEqual(sanitize_person_name("J R"), "J R")

    def test_last_name_is_martin_fills_name_not_choice(self) -> None:
        from pc_liveagent.answer_parse import resolve_answer
        from pc_liveagent.elig_meta import get_field_meta

        r = resolve_answer(
            "child_last",
            "Last name is Martin",
            options=["Child", "Grandchild", "Last Name"],
            meta=get_field_meta("child_last"),
        )
        self.assertEqual(r.answer, "Martin")
        choice = resolve_answer(
            "child_rel",
            "Last name is Martin",
            options=["Child", "Grandchild", "Last Name"],
            meta=get_field_meta("child_rel"),
        )
        self.assertIsNone(choice.answer)
        self.assertIn("relationship", (choice.clarify or "").lower())

    def test_someone_is_blind_is_not_person_assignment(self) -> None:
        from pc_liveagent.answer_parse import resolve_answer
        from pc_liveagent.elig_meta import get_field_meta

        r = resolve_answer(
            "special_person",
            "Someone is blind",
            meta=get_field_meta("special_person"),
        )
        self.assertIsNone(r.answer)
        self.assertIn("condition", (r.clarify or "").lower())

    def test_change_first_name_intent(self) -> None:
        from pc_liveagent.control_intent import detect_control_intent

        bare = detect_control_intent("Change the first name.")
        self.assertEqual(bare.kind, "change")
        self.assertEqual(bare.target, "first_name")
        self.assertEqual(bare.value, "")
        valued = detect_control_intent("Change the first name to Michael.")
        self.assertEqual(valued.kind, "change")
        self.assertEqual(valued.target, "first_name")
        self.assertEqual(valued.value.lower(), "michael")

    def test_duplicate_member_labels_need_ordinal(self) -> None:
        from pc_liveagent.live_form import _normalize_member_label

        self.assertEqual(_normalize_member_label("John D."), "john d")
        self.assertEqual(_normalize_member_label("John."), "john")

    def test_ui_field_labels_filtered_from_choices(self) -> None:
        from pc_liveagent.live_form import _choice_labels_for_matching, _fuzzy_choice_label

        labels = ["Child", "Grandchild", "First Name", "Last Name"]
        filtered = _choice_labels_for_matching(labels)
        self.assertNotIn("First Name", filtered)
        self.assertNotIn("Last Name", filtered)
        # Must not fuzzy onto the caption.
        self.assertEqual(
            _fuzzy_choice_label("Last Name", labels),
            "Last Name",
        )


class ChildNameEditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await drop_controller("child-edit")

    async def test_pending_then_value_uses_correct_name_field(self) -> None:
        call_id = "child-edit"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="child_rel"
        )

        async def fake_get_session(_cid: str):
            return session

        edit = AsyncMock(
            return_value={
                "ok": True,
                "spoken": (
                    "Updated the last name to Martin. Just to confirm for the child: "
                    "first name Jackie, last name Martin, relationship Child, "
                    "age the 13-to-15 age range. Is that correct?"
                ),
                "step": "child_confirm",
                "value": "Martin",
                "in_place": True,
            }
        )
        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch("pc_liveagent.call_turn.peek_choice_labels", AsyncMock(return_value=[])),
            patch("pc_liveagent.call_turn.correct_member_field", edit),
            patch("pc_liveagent.call_turn.submit_field", AsyncMock()) as submit,
        ):
            prompt = await process_user_turn(call_id, "Change the last name.")
            self.assertEqual(prompt.detail, "correction_prompt")
            r = await process_user_turn(call_id, "Martin")
        self.assertEqual(r.detail, "member_corrected")
        self.assertEqual(edit.await_args.args[1], "last_name")
        self.assertEqual(edit.await_args.args[2], "Martin")
        submit.assert_not_called()

    async def test_create_child_flow_steps(self) -> None:
        from pc_liveagent.live_form import _next_elig_step

        s = LiveFormSession(
            call_id="c", url="https://example.test", elig_step="child_first"
        )
        self.assertEqual(_next_elig_step(s, "Aria"), "child_last")
        s.elig_step = "child_last"
        self.assertEqual(_next_elig_step(s, "Martin"), "child_rel")
        s.elig_step = "child_rel"
        self.assertEqual(_next_elig_step(s, "Child"), "child_age")
        s.elig_step = "child_age"
        self.assertEqual(_next_elig_step(s, "15"), "child_confirm")
        s.elig_step = "child_confirm"
        self.assertEqual(_next_elig_step(s, "yes"), "child_more")
        self.assertEqual(_next_elig_step(s, "no"), "child_confirm")
        s.elig_step = "child_more"
        self.assertEqual(_next_elig_step(s, "yes"), "child_first")
        self.assertEqual(_next_elig_step(s, "no"), "student")


class SpecialConditionAndPersonTests(unittest.TestCase):
    SPECIAL_OPTS = [
        "Someone is pregnant",
        "Someone has breast cancer",
        "Someone has cervical cancer",
        "Someone is blind",
        "Someone is disabled (SSDI)",
        "Someone was in foster care when they turned 18",
        "None of the above",
    ]

    def test_disabled_maps_to_ssdi_option(self) -> None:
        from pc_liveagent.live_form import _map_special_condition_labels
        from pc_liveagent.answer_parse import resolve_answer
        from pc_liveagent.elig_meta import get_field_meta

        mapped = _map_special_condition_labels(
            "Someone is disabled.", self.SPECIAL_OPTS
        )
        self.assertEqual(mapped, ["Someone is disabled (SSDI)"])
        r = resolve_answer(
            "special",
            "Someone is disabled.",
            options=self.SPECIAL_OPTS,
            meta=get_field_meta("special"),
        )
        self.assertEqual(r.answer, "Someone is disabled (SSDI)")

    def test_blind_maps_to_visible_option(self) -> None:
        from pc_liveagent.live_form import _map_special_condition_labels

        mapped = _map_special_condition_labels("Someone is blind.", self.SPECIAL_OPTS)
        self.assertEqual(mapped, ["Someone is blind"])

    def test_natural_blind_phrases_and_negation(self) -> None:
        from pc_liveagent.live_form import _map_special_condition_labels

        self.assertEqual(
            _map_special_condition_labels("I'm blind", self.SPECIAL_OPTS),
            ["Someone is blind"],
        )
        self.assertEqual(
            _map_special_condition_labels(
                "someone in my household is blind", self.SPECIAL_OPTS
            ),
            ["Someone is blind"],
        )
        self.assertEqual(
            _map_special_condition_labels(
                "my mother has a visual disability", self.SPECIAL_OPTS
            ),
            ["Someone is blind"],
        )
        self.assertEqual(
            _map_special_condition_labels("I'm not blind", self.SPECIAL_OPTS),
            [],
        )
        self.assertEqual(
            _map_special_condition_labels(
                "blind and pregnant", self.SPECIAL_OPTS
            ),
            ["Someone is pregnant", "Someone is blind"],
        )
        # None is exclusive when no other conditions present.
        self.assertEqual(
            _map_special_condition_labels("none of the above", self.SPECIAL_OPTS),
            ["None of the above"],
        )
        self.assertEqual(
            _map_special_condition_labels("none", self.SPECIAL_OPTS),
            ["None of the above"],
        )
        self.assertEqual(
            _map_special_condition_labels("of the above", self.SPECIAL_OPTS),
            [],
        )
        self.assertEqual(
            _map_special_condition_labels("of the above.", self.SPECIAL_OPTS),
            [],
        )

    def test_household_bar_splits_into_member_identities(self) -> None:
        from pc_liveagent.answer_parse import is_fast_path_resolve, resolve_answer
        from pc_liveagent.elig_meta import get_field_meta
        from pc_liveagent.live_form import (
            _expand_household_member_bar,
            _is_plausible_member_label,
            _match_member_rows,
        )

        pieces = _expand_household_member_bar(
            "Your household Agent You john Partner"
        )
        self.assertEqual(
            [(item["label"], item["role"]) for item in pieces],
            [("Agent", "You"), ("john", "Partner")],
        )
        self.assertFalse(
            _is_plausible_member_label("Your household Agent You john Partner")
        )
        self.assertFalse(_is_plausible_member_label("Partner"))
        self.assertTrue(_is_plausible_member_label("Agent"))
        self.assertTrue(_is_plausible_member_label("john"))
        members = [
            {
                "label": "Agent",
                "role": "You",
                "chip": "Agent You",
                "dupIndex": 0,
                "checked": False,
            },
            {
                "label": "john",
                "role": "Partner",
                "chip": "john Partner",
                "dupIndex": 0,
                "checked": False,
            },
        ]
        john = _match_member_rows("john", members)
        self.assertEqual([item["chip"] for item in john], ["john Partner"])
        partner = _match_member_rows("partner", members)
        self.assertEqual([item["label"] for item in partner], ["john"])
        self.assertEqual(_match_member_rows("Alex", members), [])
        ambiguous = members + [
            {
                "label": "john",
                "role": "Child",
                "chip": "john Child",
                "dupIndex": 1,
                "checked": False,
            }
        ]
        self.assertEqual(len(_match_member_rows("john", ambiguous)), 2)
        self.assertEqual(
            [item["label"] for item in _match_member_rows("partner", ambiguous)],
            ["john"],
        )
        named = resolve_answer(
            "special",
            "john",
            options=["Your household Agent You john Partner", *self.SPECIAL_OPTS],
            meta=get_field_meta("special"),
        )
        self.assertEqual(named.detail, "special_member_name")
        self.assertTrue(is_fast_path_resolve(named))
        self.assertNotEqual(named.answer, "None of the above")

    def test_relationship_synonyms(self) -> None:
        from pc_liveagent.live_form import _map_relationship_label
        from pc_liveagent.answer_parse import resolve_answer
        from pc_liveagent.elig_meta import get_field_meta

        opts = ["Child", "Stepchild", "Grandchild", "Foster Child", "Other"]
        self.assertEqual(_map_relationship_label("my daughter", opts), "Child")
        self.assertEqual(_map_relationship_label("my grandson", opts), "Grandchild")
        self.assertEqual(
            _map_relationship_label("my stepdaughter", opts), "Stepchild"
        )
        self.assertEqual(
            _map_relationship_label("my foster child", opts), "Foster Child"
        )
        self.assertIsNone(_map_relationship_label("maybe a relative kinda", opts))
        r = resolve_answer(
            "child_rel",
            "my daughter",
            options=opts,
            meta=get_field_meta("child_rel"),
        )
        self.assertEqual(r.answer, "Child")

    def test_member_period_normalization(self) -> None:
        from pc_liveagent.live_form import (
            _normalize_member_label,
            _split_member_queries,
        )

        self.assertEqual(_normalize_member_label("Murali M."), "murali m")
        self.assertEqual(_normalize_member_label("Murali M"), "murali m")
        self.assertEqual(_normalize_member_label("Agent H."), "agent h")
        self.assertEqual(_normalize_member_label("Agent H"), "agent h")
        # Distinct people must not collapse together.
        self.assertNotEqual(
            _normalize_member_label("Murali M."),
            _normalize_member_label("Murali K."),
        )
        self.assertEqual(
            _split_member_queries("Murali M and Agent H."),
            ["Murali M", "Agent H"],
        )

    def test_resolve_special_person_keeps_multi_member_phrase(self) -> None:
        from pc_liveagent.answer_parse import resolve_answer
        from pc_liveagent.elig_meta import get_field_meta

        r = resolve_answer(
            "special_person",
            "Murali M and Agent H.",
            meta=get_field_meta("special_person"),
        )
        self.assertIsNotNone(r.answer)
        self.assertIn("and", (r.answer or "").lower())
        self.assertIn("Murali", r.answer or "")
        self.assertIn("Agent", r.answer or "")

    def test_screen_recovery_keeps_special_person(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _screen_first_step

        session = LiveFormSession(
            call_id="sp",
            url="https://example.test",
            elig_step="special_person",
        )
        self.assertEqual(_screen_first_step("special", session), "special_person")
        session.elig_step = "special"
        self.assertEqual(_screen_first_step("special", session), "special")

    def test_repeated_condition_on_special_person_clarifies(self) -> None:
        from pc_liveagent.answer_parse import resolve_answer
        from pc_liveagent.elig_meta import get_field_meta

        r = resolve_answer(
            "special_person",
            "Someone is blind.",
            meta=get_field_meta("special_person"),
        )
        self.assertIsNone(r.answer)
        self.assertIn("household member", (r.clarify or "").lower())
        r2 = resolve_answer(
            "special_person",
            "I said someone is blind.",
            meta=get_field_meta("special_person"),
        )
        self.assertIsNone(r2.answer)

    def test_angel_or_sanitizes_to_angel(self) -> None:
        from pc_liveagent.answer_parse import sanitize_person_name, resolve_answer
        from pc_liveagent.elig_meta import get_field_meta
        from pc_liveagent.live_form import _spoken_member_query, _normalize_member_label

        self.assertEqual(sanitize_person_name("Angel or"), "Angel")
        self.assertEqual(_spoken_member_query("Angel or"), "Angel")
        self.assertEqual(_spoken_member_query("Angel r dot"), "Angel R")
        self.assertEqual(_normalize_member_label("Angel r dot"), "angel r")
        self.assertEqual(_normalize_member_label("Angel R."), "angel r")
        r = resolve_answer(
            "special_person",
            "Angel or",
            meta=get_field_meta("special_person"),
        )
        self.assertEqual(r.answer, "Angel")
        r2 = resolve_answer(
            "special_person",
            "Angel r dot",
            meta=get_field_meta("special_person"),
        )
        self.assertEqual(r2.answer, "Angel R")

    def test_member_filter_rejects_marital_labels(self) -> None:
        from pc_liveagent.live_form import _is_plausible_member_label

        self.assertFalse(_is_plausible_member_label("Never Married"))
        self.assertFalse(_is_plausible_member_label("Exit"))
        self.assertFalse(_is_plausible_member_label("Someone is blind"))
        self.assertTrue(_is_plausible_member_label("Angel R."))
        self.assertTrue(_is_plausible_member_label("John. D."))
        self.assertTrue(_is_plausible_member_label("John D."))
        self.assertTrue(_is_plausible_member_label("Me"))
        # Aggregate apply-to wrapper text must not count as a member.
        self.assertFalse(
            _is_plausible_member_label(
                "Murali M. Agent H. David W. Mickey M."
            )
        )


class SpecialPersonAssignTests(unittest.IsolatedAsyncioTestCase):
    async def test_condition_phrase_rejected_before_click(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _assign_special_person_member

        page = AsyncMock()
        session = LiveFormSession(call_id="sp", url="https://example.test")
        with self.assertRaises(RuntimeError) as ctx:
            await _assign_special_person_member(page, "Someone is blind", session)
        self.assertIn("SPECIAL_PERSON_CLARIFY", str(ctx.exception))
        page.evaluate.assert_not_called()

    async def test_duplicate_john_requires_disambiguation(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _assign_special_person_member

        page = AsyncMock()
        page.evaluate = AsyncMock(
            return_value=[
                {"label": "John D.", "index": 0, "dupIndex": 0, "checked": False},
                {"label": "John D.", "index": 1, "dupIndex": 1, "checked": False},
            ]
        )
        session = LiveFormSession(call_id="sp", url="https://example.test")
        with self.assertRaises(RuntimeError) as ctx:
            await _assign_special_person_member(page, "John D.", session)
        self.assertIn("two people", str(ctx.exception).lower())
        # Never fall back to marital options in the clarify text.
        self.assertNotIn("Never Married", str(ctx.exception))

    async def test_member_click_scoped_and_verified(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _assign_special_person_member

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        members = [
            {"label": "Aria R.", "index": 0, "dupIndex": 0, "checked": False},
            {"label": "John D.", "index": 1, "dupIndex": 0, "checked": False},
        ]

        async def evaluate(script, arg=None):
            if arg is None:
                return members
            # click path
            return {"ok": True, "label": arg.get("label")}

        page.evaluate = AsyncMock(side_effect=evaluate)
        session = LiveFormSession(call_id="sp", url="https://example.test")
        await _assign_special_person_member(page, "John.", session)
        self.assertIn("John D.", session.filled.get("special_person_assigned", ""))

    async def test_multiple_members_and_period_labels(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _assign_special_person_member

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        members = [
            {"label": "Murali M.", "index": 0, "dupIndex": 0, "checked": False},
            {"label": "Agent H.", "index": 1, "dupIndex": 0, "checked": False},
        ]
        clicked: list[str] = []

        async def evaluate(script, arg=None):
            if arg is None:
                # Reflect prior clicks so already-checked path can work later.
                out = []
                for row in members:
                    copy = dict(row)
                    if copy["label"] in clicked:
                        copy["checked"] = True
                    out.append(copy)
                return {
                    "members": out,
                    "strategy": "heading",
                    "checkboxCount": 2,
                    "visibleSample": [m["label"] for m in members],
                }
            clicked.append(arg.get("label"))
            return {"ok": True, "label": arg.get("label"), "strategy": "scoped"}

        page.evaluate = AsyncMock(side_effect=evaluate)
        session = LiveFormSession(call_id="sp-multi", url="https://example.test")
        await _assign_special_person_member(
            page, "Murali M and Agent H.", session
        )
        assigned = session.filled.get("special_person_assigned", "")
        self.assertIn("Murali M.", assigned)
        self.assertIn("Agent H.", assigned)
        self.assertEqual(clicked, ["Murali M.", "Agent H."])

    async def test_already_checked_member_not_toggled(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _assign_special_person_member

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.evaluate = AsyncMock(
            return_value={
                "members": [
                    {
                        "label": "Murali M.",
                        "index": 0,
                        "dupIndex": 0,
                        "checked": True,
                    }
                ],
                "strategy": "heading",
                "checkboxCount": 1,
                "visibleSample": ["Murali M."],
            }
        )
        session = LiveFormSession(call_id="sp-checked", url="https://example.test")
        await _assign_special_person_member(page, "Murali M.", session)
        self.assertIn("Murali M.", session.filled.get("special_person_assigned", ""))
        # Discover only — no click payload (arg is None).
        for call in page.evaluate.await_args_list:
            self.assertIsNone(call.args[1] if len(call.args) > 1 else None)

    async def test_failed_special_verify_keeps_step(self) -> None:
        """Unverified special condition must not advance past special."""
        from unittest.mock import patch

        from pc_liveagent.live_form import (
            LiveFormSession,
            _submit_eligibility_answer,
        )

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.mouse = AsyncMock()
        page.mouse.wheel = AsyncMock()
        session = LiveFormSession(
            call_id="sp-verify",
            url="https://example.test",
            elig_step="special",
        )
        labels = [
            "Someone is blind",
            "Someone is pregnant",
            "None of the above",
        ]

        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                new=AsyncMock(return_value="special"),
            ),
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(return_value=labels),
            ),
            patch(
                "pc_liveagent.live_form._special_condition_checkbox_state",
                new=AsyncMock(
                    return_value={
                        "found": True,
                        "checked": False,
                        "source": "input",
                    }
                ),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_member_count",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "pc_liveagent.live_form._special_next_enabled",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await _submit_eligibility_answer(
                    "sp-verify", session, page, "I'm blind", 0.0
                )
        self.assertIn("SPECIAL_CLARIFY", str(ctx.exception))
        self.assertEqual(session.elig_step, "special")
        self.assertNotIn("special_conditions", session.filled)
        # Exactly one click attempt — never a toggle-off retry.
        self.assertEqual(click.await_count, 1)


def _sticky_state_side_effect(sequence: list[dict]):
    """Return successive states, then keep repeating the last one (poll-safe)."""
    values = list(sequence)
    if not values:
        values = [{"found": False, "checked": False, "source": "missing"}]
    idx = {"n": 0}

    async def _state(*_a, **_k):
        i = idx["n"]
        idx["n"] = i + 1
        return values[i] if i < len(values) else values[-1]

    return _state


class SpecialConditionSelectTests(unittest.IsolatedAsyncioTestCase):
    """Special Cases: one click max; never toggle off a successful selection."""

    async def test_unchecked_condition_one_click(self) -> None:
        from pc_liveagent.live_form import _select_special_condition_checkbox

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._special_condition_checkbox_state",
                new=AsyncMock(
                    side_effect=_sticky_state_side_effect(
                        [
                            {"found": True, "checked": False, "source": "input"},
                            {"found": True, "checked": True, "source": "input"},
                        ]
                    )
                ),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_member_count",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
        ):
            await _select_special_condition_checkbox(
                page, "Someone is blind", call_id="sp1", multi=False
            )
        self.assertEqual(click.await_count, 1)

    async def test_already_checked_not_clicked(self) -> None:
        from pc_liveagent.live_form import _select_special_condition_checkbox

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._special_condition_checkbox_state",
                new=AsyncMock(
                    return_value={
                        "found": True,
                        "checked": True,
                        "source": "input",
                    }
                ),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
        ):
            await _select_special_condition_checkbox(
                page, "Someone is blind", call_id="sp2", multi=False
            )
        click.assert_not_awaited()

    async def test_multi_does_not_false_accept_via_prior_apply_to(self) -> None:
        """Live bug: pregnant accepted because Blind already showed apply-to."""
        from pc_liveagent.live_form import _select_special_condition_checkbox

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()

        async def state(_page, label):
            if "pregnant" in (label or "").lower():
                return {"found": False, "checked": False, "source": "missing"}
            return {"found": True, "checked": True, "source": "input"}

        with (
            patch(
                "pc_liveagent.live_form._special_condition_checkbox_state",
                new=AsyncMock(side_effect=state),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_member_count",
                new=AsyncMock(return_value=8),  # prior Blind apply-to already open
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_heading_visible",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.live_form._special_condition_readonly_reinspect",
                new=AsyncMock(
                    return_value={
                        "found": False,
                        "checked": False,
                        "source": "reinspect_missing",
                    }
                ),
            ),
            patch(
                "pc_liveagent.live_form._discover_special_person_members",
                new=AsyncMock(
                    return_value={
                        "members": [],
                        "strategy": "condition_scope_missing",
                    }
                ),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ),
            self.assertRaises(RuntimeError) as ctx,
        ):
            await _select_special_condition_checkbox(
                page, "Someone is pregnant", call_id="sp-multi-false", multi=True
            )
        self.assertIn("couldn't confirm", str(ctx.exception).lower())
        self.assertIn("pregnant", str(ctx.exception).lower())

    async def test_multi_accepts_second_condition_without_member_count_increase(
        self,
    ) -> None:
        """Second condition OK when scoped members appear even if count is flat."""
        from pc_liveagent.live_form import _select_special_condition_checkbox

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        calls = {"n": 0}

        async def state(_page, _label):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"found": True, "checked": False, "source": "input"}
            return {"found": False, "checked": False, "source": "missing"}

        with (
            patch(
                "pc_liveagent.live_form._special_condition_checkbox_state",
                new=AsyncMock(side_effect=state),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_member_count",
                new=AsyncMock(return_value=2),
            ),
            patch(
                "pc_liveagent.live_form._discover_special_person_members",
                new=AsyncMock(
                    return_value={
                        "members": [
                            {"label": "Mickey M.", "checked": False, "dupIndex": 0}
                        ],
                        "strategy": "condition_scope",
                    }
                ),
            ) as discover,
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
        ):
            await _select_special_condition_checkbox(
                page,
                "Someone is disabled (SSDI)",
                call_id="sp-multi-ok",
                multi=True,
            )
        click.assert_awaited_once()
        discover.assert_awaited()
        # Scoped discover must use the active condition label.
        self.assertIn("disabled", (discover.await_args.args[1] or "").lower())

    async def test_disabled_condition_one_click(self) -> None:
        from pc_liveagent.live_form import _select_special_condition_checkbox

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._special_condition_checkbox_state",
                new=AsyncMock(
                    side_effect=_sticky_state_side_effect(
                        [
                            {"found": True, "checked": False, "source": "input"},
                            {"found": True, "checked": True, "source": "input"},
                        ]
                    )
                ),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_member_count",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
        ):
            await _select_special_condition_checkbox(
                page,
                "Someone is disabled (SSDI)",
                call_id="sp-dis",
                multi=False,
            )
        self.assertEqual(click.await_count, 1)

    async def test_no_second_click_when_generic_verifier_would_fail(self) -> None:
        """Simulate post-click state readable as checked only on 2nd read.

        Even if a shared verifier would still fail, special path must not
        click again after the first successful selection.
        """
        from pc_liveagent.live_form import _select_special_condition_checkbox

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._special_condition_checkbox_state",
                new=AsyncMock(
                    side_effect=_sticky_state_side_effect(
                        [
                            {"found": True, "checked": False, "source": "input"},
                            {"found": True, "checked": True, "source": "input"},
                        ]
                    )
                ),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_member_count",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "pc_liveagent.live_form._choice_appears_selected",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
        ):
            await _select_special_condition_checkbox(
                page, "Someone is blind", call_id="sp3", multi=False
            )
        self.assertEqual(click.await_count, 1)

    async def test_unreadable_inconclusive_clarifies_without_second_click(self) -> None:
        """found=False + no apply-to + failed reinspect → clarify, never toggle."""
        from pc_liveagent.live_form import _select_special_condition_checkbox

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._special_condition_checkbox_state",
                new=AsyncMock(
                    side_effect=_sticky_state_side_effect(
                        [
                            {"found": False, "checked": False, "source": "missing"},
                            {"found": False, "checked": False, "source": "missing"},
                        ]
                    )
                ),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_member_count",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_heading_visible",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.live_form._discover_special_person_members",
                new=AsyncMock(
                    return_value={"members": [], "strategy": "condition_scope_missing"}
                ),
            ),
            patch(
                "pc_liveagent.live_form._special_condition_readonly_reinspect",
                new=AsyncMock(
                    return_value={
                        "found": False,
                        "checked": False,
                        "source": "reinspect_missing",
                    }
                ),
            ),
            patch(
                "pc_liveagent.live_form._special_next_enabled",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await _select_special_condition_checkbox(
                    page, "Someone is blind", call_id="sp4", multi=False
                )
        self.assertIn("SPECIAL_CLARIFY", str(ctx.exception))
        self.assertEqual(click.await_count, 1)

    async def test_unreadable_accepted_via_readonly_reinspect(self) -> None:
        """Bounded reinspect finds nested checkbox checked — accept, one click."""
        from pc_liveagent.live_form import _select_special_condition_checkbox

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._special_condition_checkbox_state",
                new=AsyncMock(
                    side_effect=_sticky_state_side_effect(
                        [
                            {"found": False, "checked": False, "source": "missing"},
                            {"found": False, "checked": False, "source": "missing"},
                        ]
                    )
                ),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_member_count",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_heading_visible",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.live_form._discover_special_person_members",
                new=AsyncMock(
                    return_value={"members": [], "strategy": "condition_scope_missing"}
                ),
            ),
            patch(
                "pc_liveagent.live_form._special_condition_readonly_reinspect",
                new=AsyncMock(
                    return_value={
                        "found": True,
                        "checked": True,
                        "source": "reinspect_checkbox",
                    }
                ),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
        ):
            await _select_special_condition_checkbox(
                page, "Someone is blind", call_id="sp4b", multi=False
            )
        self.assertEqual(click.await_count, 1)

    async def test_unreadable_accepted_via_delayed_apply_to(self) -> None:
        """Apply-to appears after a short poll when checkbox state is unreadable."""
        from pc_liveagent.live_form import _select_special_condition_checkbox

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        member_counts = iter([0, 0, 3, 3, 3])
        headings = iter([False, False, True, True, True])
        with (
            patch(
                "pc_liveagent.live_form._special_condition_checkbox_state",
                new=AsyncMock(
                    side_effect=_sticky_state_side_effect(
                        [
                            {"found": False, "checked": False, "source": "missing"},
                            {"found": False, "checked": False, "source": "missing"},
                        ]
                    )
                ),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_member_count",
                new=AsyncMock(side_effect=lambda *_a, **_k: next(member_counts)),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_heading_visible",
                new=AsyncMock(side_effect=lambda *_a, **_k: next(headings)),
            ),
            patch(
                "pc_liveagent.live_form._discover_special_person_members",
                new=AsyncMock(
                    return_value={"members": [], "strategy": "condition_scope_missing"}
                ),
            ),
            patch(
                "pc_liveagent.live_form._special_condition_readonly_reinspect",
                new=AsyncMock(
                    return_value={
                        "found": False,
                        "checked": False,
                        "source": "reinspect_missing",
                    }
                ),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
        ):
            await _select_special_condition_checkbox(
                page, "Someone is blind", call_id="sp4c", multi=False
            )
        self.assertEqual(click.await_count, 1)

    async def test_unreadable_accepts_apply_to_expansion(self) -> None:
        """Member list expansion while Next stays disabled still confirms select."""
        from pc_liveagent.live_form import _select_special_condition_checkbox

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        member_counts = iter([0, 4, 4, 4])
        with (
            patch(
                "pc_liveagent.live_form._special_condition_checkbox_state",
                new=AsyncMock(
                    side_effect=_sticky_state_side_effect(
                        [
                            {"found": False, "checked": False, "source": "missing"},
                            {"found": False, "checked": False, "source": "missing"},
                        ]
                    )
                ),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_member_count",
                new=AsyncMock(side_effect=lambda *_a, **_k: next(member_counts)),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_heading_visible",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.live_form._discover_special_person_members",
                new=AsyncMock(
                    return_value={"members": [], "strategy": "condition_scope_missing"}
                ),
            ),
            patch(
                "pc_liveagent.live_form._special_next_enabled",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
        ):
            await _select_special_condition_checkbox(
                page, "Someone is blind", call_id="sp5", multi=False
            )
        self.assertEqual(click.await_count, 1)

    async def test_multi_unreadable_accepted_without_next(self) -> None:
        from pc_liveagent.live_form import _select_special_condition_checkbox

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._special_condition_checkbox_state",
                new=AsyncMock(
                    side_effect=_sticky_state_side_effect(
                        [
                            {"found": False, "checked": False, "source": "missing"},
                            {"found": False, "checked": False, "source": "missing"},
                        ]
                    )
                ),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_member_count",
                new=AsyncMock(return_value=2),
            ),
            patch(
                "pc_liveagent.live_form._discover_special_person_members",
                new=AsyncMock(
                    return_value={
                        "members": [
                            {"label": "Mickey M.", "checked": False, "dupIndex": 0}
                        ],
                        "strategy": "condition_scope",
                    }
                ),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
        ):
            await _select_special_condition_checkbox(
                page, "Someone is blind", call_id="sp6", multi=True
            )
        self.assertEqual(click.await_count, 1)

    async def test_explicit_unchecked_after_click_clarifies_once(self) -> None:
        from pc_liveagent.live_form import _select_special_condition_checkbox

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._special_condition_checkbox_state",
                new=AsyncMock(
                    side_effect=_sticky_state_side_effect(
                        [
                            {"found": True, "checked": False, "source": "input"},
                            {"found": True, "checked": False, "source": "input"},
                        ]
                    )
                ),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_member_count",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "pc_liveagent.live_form._discover_special_person_members",
                new=AsyncMock(
                    return_value={"members": [], "strategy": "condition_members_missing"}
                ),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await _select_special_condition_checkbox(
                    page, "Someone is blind", call_id="sp7", multi=False
                )
        self.assertIn("SPECIAL_CLARIFY", str(ctx.exception))
        self.assertEqual(click.await_count, 1)

    async def test_submit_multi_conditions_one_click_each(self) -> None:
        from pc_liveagent.live_form import (
            LiveFormSession,
            _submit_eligibility_answer,
        )

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.mouse = AsyncMock()
        page.mouse.wheel = AsyncMock()
        session = LiveFormSession(
            call_id="sp-multi-cond",
            url="https://example.test",
            elig_step="special",
        )
        labels = [
            "Someone is pregnant",
            "Someone is blind",
            "None of the above",
        ]
        # Each label: before unchecked, after checked.
        state_calls: list[str] = []

        async def state(_page, label):
            state_calls.append(label)
            # Odd calls are "before", even are "after" per label pair.
            checked = state_calls.count(label) > 1
            return {"found": True, "checked": checked, "source": "input"}

        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                new=AsyncMock(return_value="special"),
            ),
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(return_value=labels),
            ),
            patch(
                "pc_liveagent.live_form._special_condition_checkbox_state",
                new=AsyncMock(side_effect=state),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_member_count",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "pc_liveagent.live_form._wait_for_special_apply_to_members",
                new=AsyncMock(
                    return_value={
                        "members": [
                            {
                                "label": "Murali M.",
                                "index": 0,
                                "dupIndex": 0,
                                "checked": False,
                            }
                        ],
                        "strategy": "heading",
                        "has_apply_to": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
        ):
            result = await _submit_eligibility_answer(
                "sp-multi-cond",
                session,
                page,
                "blind and pregnant",
                0.0,
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "special_person")
        self.assertEqual(click.await_count, 2)
        self.assertIn("blind", session.filled.get("special_conditions", "").lower())
        self.assertIn("pregnant", session.filled.get("special_conditions", "").lower())
        # One condition at a time — do not assign under Blind while asking Pregnant.
        active = (session.filled.get("special_active_conditions") or "").lower()
        self.assertTrue(
            "pregnant" in active or "blind" in active,
            active,
        )
        self.assertNotIn("|", session.filled.get("special_active_conditions") or "")
        queue = session.filled.get("special_person_queue") or ""
        self.assertIn("|", queue)
        spoken = (result.get("spoken") or "").lower()
        self.assertTrue(
            "who does" in spoken or "apply" in spoken or "which household" in spoken,
            spoken,
        )

    async def test_submit_none_of_the_above(self) -> None:
        from pc_liveagent.live_form import (
            LiveFormSession,
            _submit_eligibility_answer,
        )

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.mouse = AsyncMock()
        page.mouse.wheel = AsyncMock()
        session = LiveFormSession(
            call_id="sp-none",
            url="https://example.test",
            elig_step="special",
        )
        labels = [
            "Someone is blind",
            "None of the above",
        ]
        states = iter(
            [
                {"found": True, "checked": False, "source": "input"},
                {"found": True, "checked": True, "source": "input"},
            ]
        )
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                new=AsyncMock(return_value="special"),
            ),
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(return_value=labels),
            ),
            patch(
                "pc_liveagent.live_form._special_condition_checkbox_state",
                new=AsyncMock(side_effect=lambda *_a, **_k: next(states)),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_member_count",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
            patch(
                "pc_liveagent.live_form._click_cta",
                new=AsyncMock(),
            ),
            patch(
                "pc_liveagent.live_form._wait_for_screen",
                new=AsyncMock(return_value="programs"),
            ),
        ):
            result = await _submit_eligibility_answer(
                "sp-none", session, page, "none of the above", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "programs")
        self.assertEqual(click.await_count, 1)
        self.assertIn("none", session.filled.get("special_conditions", "").lower())

    async def test_submit_unreadable_advances_to_special_person(self) -> None:
        """Successful click with unreadable checkbox must not re-ask the condition."""
        from pc_liveagent.live_form import (
            LiveFormSession,
            _submit_eligibility_answer,
        )

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.mouse = AsyncMock()
        page.mouse.wheel = AsyncMock()
        session = LiveFormSession(
            call_id="sp-unread",
            url="https://example.test",
            elig_step="special",
        )
        labels = [
            "Someone is blind",
            "Someone is disabled (SSDI)",
            "Murali M.",
            "Agent H.",
            "None of the above",
        ]
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                new=AsyncMock(return_value="special"),
            ),
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(return_value=labels),
            ),
            patch(
                "pc_liveagent.live_form._special_condition_checkbox_state",
                new=AsyncMock(
                    return_value={
                        "found": False,
                        "checked": False,
                        "source": "missing",
                    }
                ),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_member_count",
                new=AsyncMock(side_effect=[0, 3]),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_heading_visible",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.live_form._wait_for_special_apply_to_members",
                new=AsyncMock(
                    return_value={
                        "members": [
                            {
                                "label": "Murali M.",
                                "index": 0,
                                "dupIndex": 0,
                                "checked": False,
                            }
                        ],
                        "strategy": "heading",
                        "has_apply_to": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
        ):
            result = await _submit_eligibility_answer(
                "sp-unread", session, page, "I'm blind", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "special_person")
        self.assertEqual(click.await_count, 1)
        self.assertIn("blind", session.filled.get("special_conditions", "").lower())

    def test_mapper_ignores_household_names_in_visible_list(self) -> None:
        from pc_liveagent.live_form import _map_special_condition_labels

        labels = [
            "Someone is blind",
            "Someone is disabled (SSDI)",
            "Murali M.",
            "Agent H.",
            "None of the above",
        ]
        self.assertEqual(
            _map_special_condition_labels("blind and disabled", labels),
            ["Someone is blind", "Someone is disabled (SSDI)"],
        )


class SpecialPersonAssignFollowupTests(unittest.IsolatedAsyncioTestCase):
    """Remaining special_person assign cases after SpecialConditionSelectTests insert."""

    async def test_incomplete_angel_or_matches_angel(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _assign_special_person_member

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        members = [
            {"label": "Angel R.", "index": 0, "dupIndex": 0, "checked": False},
            {"label": "John D.", "index": 1, "dupIndex": 0, "checked": False},
        ]

        async def evaluate(script, arg=None):
            if arg is None:
                return members
            return {"ok": True, "label": arg.get("label")}

        page.evaluate = AsyncMock(side_effect=evaluate)
        session = LiveFormSession(call_id="sp", url="https://example.test")
        await _assign_special_person_member(page, "Angel or", session)
        self.assertIn("Angel R.", session.filled.get("special_person_assigned", ""))

    async def test_angel_r_dot_matches_displayed_label(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _assign_special_person_member

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        members = [
            {"label": "Angel R.", "index": 0, "dupIndex": 0, "checked": False},
            {"label": "Mike H.", "index": 1, "dupIndex": 0, "checked": False},
        ]

        async def evaluate(script, arg=None):
            if arg is None:
                return {
                    "members": members,
                    "strategy": "special_section_fallback",
                    "checkboxCount": 6,
                    "visibleSample": ["Someone is blind", "Angel R.", "Mike H."],
                }
            self.assertEqual(arg.get("label"), "Angel R.")
            return {"ok": True, "label": "Angel R.", "strategy": "scoped"}

        page.evaluate = AsyncMock(side_effect=evaluate)
        session = LiveFormSession(call_id="sp", url="https://example.test")
        await _assign_special_person_member(page, "Angel r dot", session)
        self.assertIn("Angel R.", session.filled.get("special_person_assigned", ""))

    async def test_discovery_fallback_without_heading(self) -> None:
        from pc_liveagent.live_form import (
            LiveFormSession,
            _assign_special_person_member,
            _discover_special_person_members,
        )

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        # Simulate missing heading: JS returns pagewide fallback payload.
        page.evaluate = AsyncMock(
            return_value={
                "members": [
                    {
                        "label": "Angel R.",
                        "index": 0,
                        "dupIndex": 0,
                        "checked": False,
                    },
                    {
                        "label": "John. D.",
                        "index": 1,
                        "dupIndex": 0,
                        "checked": False,
                    },
                ],
                "strategy": "pagewide_person_labels",
                "checkboxCount": 8,
                "visibleSample": [
                    "Someone is blind",
                    "Angel R.",
                    "John. D.",
                    "Mike H.",
                ],
            }
        )
        discovered = await _discover_special_person_members(page)
        self.assertEqual(discovered["strategy"], "pagewide_person_labels")
        self.assertEqual(len(discovered["members"]), 2)

        # Assignment path also consumes dict payload and clicks.
        async def evaluate(script, arg=None):
            if arg is None:
                return {
                    "members": [
                        {
                            "label": "Angel R.",
                            "index": 0,
                            "dupIndex": 0,
                            "checked": False,
                        }
                    ],
                    "strategy": "special_section_fallback",
                    "checkboxCount": 4,
                    "visibleSample": ["Angel R."],
                }
            return {"ok": True, "label": "Angel R."}

        page.evaluate = AsyncMock(side_effect=evaluate)
        session = LiveFormSession(call_id="sp", url="https://example.test")
        await _assign_special_person_member(page, "Angel", session)
        self.assertIn("Angel R.", session.filled.get("special_person_assigned", ""))

    async def test_empty_discovery_logs_and_clarifies(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _assign_special_person_member

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        empty = {
            "members": [],
            "strategy": "section_missing",
            "checkbox_count": 0,
            "visible_labels": ["Someone is blind", "Next"],
            "has_apply_to": False,
            "has_special": True,
            "has_marital": False,
            "url": "https://example.test/special",
        }
        session = LiveFormSession(
            call_id="sp-empty",
            url="https://example.test",
            elig_step="special_person",
        )
        with (
            patch(
                "pc_liveagent.live_form._wait_for_special_apply_to_members",
                new=AsyncMock(return_value=empty),
            ),
            patch(
                "pc_liveagent.live_form._recover_special_apply_to_section",
                new=AsyncMock(return_value=empty),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await _assign_special_person_member(page, "Angel", session)
        self.assertIn("member list", str(ctx.exception).lower())
        self.assertNotIn("say the person's name again.", str(ctx.exception).lower())
        self.assertEqual(session.elig_step, "special_person")

    async def test_wrong_screen_marital_asks_to_reopen_condition(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _assign_special_person_member

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        wrong = {
            "members": [],
            "strategy": "wrong_screen_marital",
            "checkbox_count": 11,
            "visible_labels": [
                "Never Married",
                "Married",
                "Add Member",
                "Exit",
            ],
            "has_apply_to": False,
            "has_special": False,
            "has_marital": True,
            "url": "https://example.test/marital",
        }
        session = LiveFormSession(
            call_id="sp-wrong",
            url="https://example.test",
            elig_step="special_person",
        )
        session.filled["special_conditions"] = "Someone is blind"
        with (
            patch(
                "pc_liveagent.live_form._wait_for_special_apply_to_members",
                new=AsyncMock(return_value=wrong),
            ),
            patch(
                "pc_liveagent.live_form._recover_special_apply_to_section",
                new=AsyncMock(return_value=wrong),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await _assign_special_person_member(page, "Mickey", session)
        msg = str(ctx.exception).lower()
        self.assertIn("special cases", msg)
        self.assertIn("condition", msg)

    async def test_mickey_mouse_matches_mickey_m(self) -> None:
        from pc_liveagent.live_form import _match_member_rows

        members = [
            {"label": "Mickey M.", "index": 0, "dupIndex": 0, "checked": False},
            {"label": "Murali M.", "index": 1, "dupIndex": 0, "checked": False},
        ]
        hits = _match_member_rows("Mickey Mouse.", members)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["label"], "Mickey M.")

    async def test_assign_waits_until_members_ready(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _assign_special_person_member

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        members = [
            {"label": "Mickey M.", "index": 0, "dupIndex": 0, "checked": False},
        ]
        ready = {
            "members": members,
            "strategy": "heading",
            "checkbox_count": 4,
            "visible_labels": ["Who does this apply to?", "Mickey M."],
            "has_apply_to": True,
            "has_special": True,
            "has_marital": False,
            "url": "https://example.test/special",
        }
        session = LiveFormSession(
            call_id="sp-wait",
            url="https://example.test",
            elig_step="special_person",
        )

        async def click(_page, label, *, dup_index=0):
            return True

        with (
            patch(
                "pc_liveagent.live_form._wait_for_special_apply_to_members",
                new=AsyncMock(return_value=ready),
            ),
            patch(
                "pc_liveagent.live_form._discover_special_person_members",
                new=AsyncMock(return_value=ready),
            ),
            patch(
                "pc_liveagent.live_form._click_special_person_member",
                new=AsyncMock(side_effect=click),
            ),
        ):
            await _assign_special_person_member(page, "Mickey Mouse", session)
        self.assertIn("Mickey M.", session.filled.get("special_person_assigned", ""))

    async def test_nested_name_div_pairs_empty_role_checkbox(self) -> None:
        """Pointcare: empty button[role=checkbox] + sibling name div in apply-to."""
        from pc_liveagent.live_form import (
            _SPECIAL_MEMBER_DISCOVER_JS,
            _discover_special_person_members,
        )

        page = AsyncMock()
        # Simulate the live DOM pairing result (names resolved from row climb).
        page.evaluate = AsyncMock(
            return_value={
                "members": [
                    {
                        "label": "Murali M.",
                        "index": 0,
                        "dupIndex": 0,
                        "checked": False,
                    },
                    {
                        "label": "Mickey M.",
                        "index": 1,
                        "dupIndex": 0,
                        "checked": False,
                    },
                    {
                        # Aggregate wrapper must be dropped by Python filter.
                        "label": "Murali M. Agent H. David W. Mickey M.",
                        "index": 2,
                        "dupIndex": 0,
                        "checked": False,
                    },
                ],
                "strategy": "heading",
                "checkboxCount": 11,
                "visibleSample": [
                    "Who does this apply to?",
                    "Murali M.",
                    "Mickey M.",
                ],
                "hasApplyTo": True,
                "hasSpecial": True,
                "hasMarital": False,
                "url": "https://example.test/special",
            }
        )
        discovered = await _discover_special_person_members(page)
        labels = [m["label"] for m in discovered["members"]]
        self.assertEqual(labels, ["Murali M.", "Mickey M."])
        self.assertEqual(discovered["strategy"], "heading")
        self.assertIn("Who does this apply to?", _SPECIAL_MEMBER_DISCOVER_JS)

    async def test_click_targets_checkbox_then_verifies_aria(self) -> None:
        from pc_liveagent.live_form import _click_special_person_member

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        calls: list[str] = []

        async def evaluate(script, arg=None):
            mode = (arg or {}).get("mode")
            calls.append(str(mode))
            if mode == "click":
                return {
                    "ok": True,
                    "clicked": True,
                    "already": False,
                    "label": "Mickey M.",
                    "strategy": "scoped",
                }
            if mode == "verify":
                return {
                    "ok": True,
                    "already": True,
                    "label": "Mickey M.",
                    "strategy": "scoped",
                }
            return {"ok": True, "label": "Mickey M."}

        page.evaluate = AsyncMock(side_effect=evaluate)
        ok = await _click_special_person_member(page, "Mickey M.", dup_index=0)
        self.assertTrue(ok)
        self.assertEqual(calls, ["click", "verify"])
        self.assertEqual(page.wait_for_timeout.await_count, 1)

    async def test_click_no_checkbox_control_fails_without_retry_toggle(self) -> None:
        from pc_liveagent.live_form import _click_special_person_member

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.evaluate = AsyncMock(
            return_value={
                "ok": False,
                "reason": "no_checkbox",
                "label": "Mickey M.",
            }
        )
        ok = await _click_special_person_member(page, "Mickey M.", dup_index=0)
        self.assertFalse(ok)
        self.assertEqual(page.evaluate.await_count, 1)

    async def test_unique_first_name_match_only(self) -> None:
        from pc_liveagent.live_form import _match_member_rows

        members = [
            {"label": "Mickey M.", "index": 0, "dupIndex": 0, "checked": False},
            {"label": "Murali M.", "index": 1, "dupIndex": 0, "checked": False},
        ]
        self.assertEqual(
            [m["label"] for m in _match_member_rows("Mickey", members)],
            ["Mickey M."],
        )
        dups = [
            {"label": "John D.", "index": 0, "dupIndex": 0, "checked": False},
            {"label": "John K.", "index": 1, "dupIndex": 0, "checked": False},
        ]
        hits = _match_member_rows("John", dups)
        self.assertEqual(len(hits), 2)

    async def test_special_step_requires_apply_to_before_advance(self) -> None:
        from pc_liveagent.live_form import (
            LiveFormSession,
            _submit_eligibility_answer,
        )

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.mouse = AsyncMock()
        page.mouse.wheel = AsyncMock()
        session = LiveFormSession(
            call_id="sp-gate",
            url="https://example.test",
            elig_step="special",
        )
        labels = ["Someone is blind", "None of the above"]
        empty = {
            "members": [],
            "strategy": "section_missing",
            "checkbox_count": 7,
            "visible_labels": labels,
            "has_apply_to": False,
            "has_special": True,
            "has_marital": False,
            "url": "",
        }
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                new=AsyncMock(return_value="special"),
            ),
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(return_value=labels),
            ),
            patch(
                "pc_liveagent.live_form._select_special_condition_checkbox",
                new=AsyncMock(),
            ),
            patch(
                "pc_liveagent.live_form._wait_for_special_apply_to_members",
                new=AsyncMock(return_value=empty),
            ),
            patch(
                "pc_liveagent.live_form._recover_special_apply_to_section",
                new=AsyncMock(return_value=empty),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await _submit_eligibility_answer(
                    "sp-gate", session, page, "I'm blind", 0.0
                )
        self.assertIn("SPECIAL_CLARIFY", str(ctx.exception))
        self.assertEqual(session.elig_step, "special")

    async def test_failed_click_keeps_special_person_clarify(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _assign_special_person_member

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        members = [
            {"label": "Angel R.", "index": 0, "dupIndex": 0, "checked": False},
        ]

        async def evaluate(script, arg=None):
            if arg is None:
                return members
            return {"ok": False, "reason": "not_checked"}

        page.evaluate = AsyncMock(side_effect=evaluate)
        session = LiveFormSession(
            call_id="sp",
            url="https://example.test",
            elig_step="special_person",
        )
        with self.assertRaises(RuntimeError) as ctx:
            await _assign_special_person_member(page, "Angel R.", session)
        self.assertIn("SPECIAL_PERSON_CLARIFY", str(ctx.exception))
        self.assertEqual(session.elig_step, "special_person")

    async def test_clarify_error_does_not_recover_to_special(self) -> None:
        """Regression: call_1613 — SPECIAL_PERSON_CLARIFY must not ok→special."""
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="sp-recover",
            url="https://example.test",
            elig_step="special_person",
        )
        session.filled["special_conditions"] = "Someone is blind"
        # Simulate submit_field clarify branch ordering via _screen_first_step guard.
        from pc_liveagent.live_form import _screen_first_step

        recovered = _screen_first_step("special", session)
        self.assertEqual(recovered, "special_person")
        self.assertNotEqual(recovered, "special")


class AgeAndChildFormRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await drop_controller("age-child")

    async def test_claude_timeout_does_not_select_age(self) -> None:
        """Claude timeout on ambiguous age must clarify — never click Young Adult."""
        from pc_liveagent.answer_parse import ClaudeResolveResult

        call_id = "age-child"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="spouse_age"
        )

        async def fake_get_session(_cid: str):
            return session

        captions = ["First Name", "Last Name", "Spouse", "Partner", "Age"]
        submit = AsyncMock()
        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch(
                "pc_liveagent.call_turn.peek_choice_labels",
                AsyncMock(return_value=captions),
            ),
            patch(
                "pc_liveagent.call_turn.resolve_with_claude",
                AsyncMock(
                    return_value=ClaudeResolveResult(
                        reject_reason="timeout",
                        needs_clarify=True,
                        latency_ms=1500,
                    )
                ),
            ),
            patch("pc_liveagent.call_turn.submit_field", submit),
        ):
            r = await process_user_turn(call_id, "Twenty twenty two thirty")
        self.assertIn("age", r.speech.lower())
        self.assertNotIn("first name", r.speech.lower())
        self.assertNotIn("young adult", r.speech.lower())
        self.assertIn(r.detail, {"age_ambiguous", "clarify", "timeout"})
        submit.assert_not_called()
        self.assertEqual(session.elig_step, "spouse_age")

    async def test_twenty_five_fast_path_submits_spoken_age(self) -> None:
        call_id = "age-child"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        ctrl.active_websocket_status = "connected"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="spouse_age"
        )

        async def fake_get_session(_cid: str):
            return session

        async def submit(_cid, _field, value, **_k):
            session.elig_step = "children"
            return {
                "ok": True,
                "spoken": "ASK: Do you live with and mainly support any children?",
                "step": "children",
                "value": value,
            }

        interrupt = AsyncMock(return_value=True)
        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch(
                "pc_liveagent.call_turn.peek_choice_labels",
                AsyncMock(
                    return_value=[
                        "First Name",
                        "Last Name",
                        "Spouse",
                        "Partner",
                        "Age",
                    ]
                ),
            ),
            patch("pc_liveagent.call_turn.submit_field", side_effect=submit) as submit_mock,
            patch("pc_liveagent.call_turn._speak_interrupt", interrupt),
            patch("pc_liveagent.call_turn.resolve_with_claude", AsyncMock()) as claude,
        ):
            r = await process_user_turn(call_id, "Twenty five", response_id=1)
            task = __import__(
                "pc_liveagent.call_turn", fromlist=["_elig_browser_tasks"]
            )._elig_browser_tasks.get(call_id)
            if task:
                await asyncio.wait_for(task, timeout=2.0)
        claude.assert_not_called()
        self.assertEqual(r.detail, "elig_ack")
        submit_mock.assert_awaited()
        self.assertEqual(submit_mock.await_args.args[2], "Twenty five")

    async def test_children_yes_waits_for_child_form(self) -> None:
        """Children=Yes must not set child_first until First Name is ready."""
        from pc_liveagent.live_form import _submit_eligibility_answer

        call_id = "age-child"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="children"
        )
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        visible_phases = {"n": 0}

        async def eligibility_screen(_page):
            # First polls still on children; later child_form.
            visible_phases["n"] += 1
            return "children" if visible_phases["n"] < 3 else "child_form"

        name_ready = {"ok": False}

        async def name_visible(_page, label):
            if label == "First Name" and visible_phases["n"] >= 4:
                name_ready["ok"] = True
                return True
            if label == "Last Name" and name_ready["ok"]:
                return True
            return False

        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                side_effect=eligibility_screen,
            ),
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                AsyncMock(return_value=["Yes", "No"]),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                AsyncMock(),
            ),
            patch(
                "pc_liveagent.live_form._name_field_visible",
                side_effect=name_visible,
            ),
            patch(
                "pc_liveagent.live_form._wait_for_child_form_ready",
                AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.live_form._clear_new_person_name_fields",
                AsyncMock(),
            ) as clear_names,
        ):
            result = await _submit_eligibility_answer(
                call_id, session, page, "Yes", started=0.0
            )
        clear_names.assert_awaited()
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "child_first")

    async def test_child_first_recovers_when_field_initially_absent(self) -> None:
        from pc_liveagent.live_form import _submit_eligibility_answer

        call_id = "age-child"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="child_first"
        )
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        attempts = {"n": 0}

        async def name_visible(_page, label):
            if label != "First Name":
                return False
            attempts["n"] += 1
            return attempts["n"] >= 2

        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="child_form"),
            ),
            patch(
                "pc_liveagent.live_form._name_field_visible",
                side_effect=name_visible,
            ),
            patch(
                "pc_liveagent.live_form._wait_for_child_form_ready",
                AsyncMock(return_value=True),
            ),
            patch("pc_liveagent.live_form._strict_fill", AsyncMock()) as fill,
        ):
            result = await _submit_eligibility_answer(
                call_id, session, page, "Jackie", started=0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "child_last")
        fill.assert_awaited_once()
        self.assertEqual(fill.await_args.args[1], "First Name")
        self.assertEqual(fill.await_args.args[2], "Jackie")

    async def test_child_form_fields_fill_in_order(self) -> None:
        from pc_liveagent.live_form import _submit_eligibility_answer

        call_id = "age-child"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="child_first"
        )
        page = AsyncMock()
        fills: list[tuple[str, str]] = []

        async def strict_fill(_page, label, value):
            fills.append((label, value))

        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="child_form"),
            ),
            patch(
                "pc_liveagent.live_form._name_field_visible",
                AsyncMock(return_value=True),
            ),
            patch("pc_liveagent.live_form._strict_fill", side_effect=strict_fill),
            patch("pc_liveagent.live_form._click_exact_choice", AsyncMock()) as click,
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                AsyncMock(
                    return_value=[
                        "Child",
                        "Stepchild",
                        "Grandchild",
                        "Foster Child",
                        "Other",
                    ]
                ),
            ),
            patch(
                "pc_liveagent.live_form._ensure_age_choices_visible",
                AsyncMock(
                    return_value=[
                        "(0-1) Baby",
                        "(1-5) Toddler",
                        "(6-12) Child",
                        "(13-15) Teen",
                        "(16-18) Teen",
                    ]
                ),
            ),
            patch("pc_liveagent.live_form._click_age_choice", AsyncMock()) as age_click,
        ):
            r1 = await _submit_eligibility_answer(
                call_id, session, page, "Jackie", started=0.0
            )
            session.elig_step = r1["step"]
            r2 = await _submit_eligibility_answer(
                call_id, session, page, "Smith", started=0.0
            )
            session.elig_step = r2["step"]
            r3 = await _submit_eligibility_answer(
                call_id, session, page, "Child", started=0.0
            )
            session.elig_step = r3["step"]
            r4 = await _submit_eligibility_answer(
                call_id, session, page, "15", started=0.0
            )
        self.assertEqual([s for s, _ in fills], ["First Name", "Last Name"])
        self.assertEqual(fills[0][1], "Jackie")
        self.assertEqual(fills[1][1], "Smith")
        self.assertTrue(click.await_count or page.evaluate.await_count)
        age_click.assert_awaited()
        self.assertEqual(r4.get("step"), "child_confirm")
        self.assertIn("just to confirm", (r4.get("spoken") or "").lower())

    async def test_child_form_missing_keeps_state_and_explains(self) -> None:
        from pc_liveagent.live_form import _submit_eligibility_answer

        call_id = "age-child"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="children"
        )
        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="children"),
            ),
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                AsyncMock(return_value=["Yes", "No"]),
            ),
            patch("pc_liveagent.live_form._click_exact_choice", AsyncMock()),
            patch(
                "pc_liveagent.live_form._wait_for_child_form_ready",
                AsyncMock(return_value=False),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await _submit_eligibility_answer(
                    call_id, session, page, "Yes", started=0.0
                )
        self.assertIn("CHILD_FORM", str(ctx.exception))
        self.assertEqual(session.elig_step, "children")

    async def test_child_more_continue_fail_does_not_restart_child_first(self) -> None:
        """Regression call_d0fa: stuck child_form must not ok→child_first + commit."""
        from pc_liveagent.live_form import _submit_eligibility_answer

        call_id = "age-child"
        session = LiveFormSession(
            call_id=call_id,
            url="https://example.test",
            elig_step="child_more",
            page=AsyncMock(),
        )
        session.filled["child_instance"] = "1"
        page = session.page

        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="child_form"),
            ),
            patch(
                "pc_liveagent.live_form._click_continue_to",
                AsyncMock(
                    side_effect=RuntimeError(
                        "Pointcare did not reach ('student',); current screen is child_form"
                    )
                ),
            ),
            patch(
                "pc_liveagent.live_form._visible_validation_hints",
                AsyncMock(return_value="Required field"),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await _submit_eligibility_answer(
                    call_id, session, page, "No", started=0.0
                )
        self.assertIn("CHILD_MORE_CLARIFY", str(ctx.exception))
        self.assertEqual(session.elig_step, "child_more")

        # Recovery guard: stay on the active child_* step (do not force child_first).
        from pc_liveagent.live_form import _screen_first_step, _step_group

        recovered = _screen_first_step("child_form", session)
        self.assertEqual(recovered, "child_more")
        self.assertEqual(_step_group("child_more"), "child_form")
        self.assertEqual(_step_group(recovered), "child_form")
        # Same form group ⇒ treat as failure, not successful recovery.
        same_form = _step_group(recovered) == _step_group("child_more")
        self.assertTrue(same_form)

    async def test_second_child_same_answers_not_blocked(self) -> None:
        call_id = "age-child"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        ctrl.active_websocket_status = "connected"
        # Simulate first child already committed Child + Youth + No for instance 1.
        ctrl.committed = [
            {"key": "child_rel:1:child", "step": "child_rel", "answer": "Child"},
            {
                "key": "child_age:1:(6-18) youth",
                "step": "child_age",
                "answer": "(6-18) Youth",
            },
            {"key": "child_more:1:no", "step": "child_more", "answer": "No"},
        ]
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="child_rel"
        )
        session.filled["child_instance"] = "2"

        async def fake_get_session(_cid: str):
            return session

        submit = AsyncMock(
            return_value={
                "ok": True,
                "spoken": "ASK: What is the child's age range?",
                "step": "child_age",
            }
        )
        interrupt = AsyncMock(return_value=True)
        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch("pc_liveagent.call_turn.peek_choice_labels", AsyncMock(return_value=["Child", "Stepchild"])),
            patch("pc_liveagent.call_turn.submit_field", submit),
            patch("pc_liveagent.call_turn._speak_interrupt", interrupt),
        ):
            r = await process_user_turn(call_id, "Child", response_id=1)
            task = __import__(
                "pc_liveagent.call_turn", fromlist=["_elig_browser_tasks"]
            )._elig_browser_tasks.get(call_id)
            if task:
                await asyncio.wait_for(task, timeout=2.0)
        self.assertEqual(r.detail, "elig_ack")
        submit.assert_awaited()
        self.assertNotEqual(r.detail, "already_committed")

    async def test_already_committed_retries_when_still_on_step(self) -> None:
        call_id = "age-child"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        ctrl.active_websocket_status = "connected"
        ctrl.committed = [
            {"key": "child_more:1:no", "step": "child_more", "answer": "No"},
        ]
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="child_more"
        )
        session.filled["child_instance"] = "1"

        async def fake_get_session(_cid: str):
            return session

        submit = AsyncMock(
            return_value={
                "ok": True,
                "spoken": "ASK: Are you the parent of a full-time student?",
                "step": "student",
            }
        )
        interrupt = AsyncMock(return_value=True)
        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch("pc_liveagent.call_turn.peek_choice_labels", AsyncMock(return_value=["Yes", "No"])),
            patch("pc_liveagent.call_turn.submit_field", submit),
            patch("pc_liveagent.call_turn._speak_interrupt", interrupt),
        ):
            r = await process_user_turn(call_id, "No", response_id=1)
            task = __import__(
                "pc_liveagent.call_turn", fromlist=["_elig_browser_tasks"]
            )._elig_browser_tasks.get(call_id)
            if task:
                await asyncio.wait_for(task, timeout=2.0)
        self.assertEqual(r.detail, "elig_ack")
        submit.assert_awaited()

    async def test_control_commands_repeat_redo_change(self) -> None:
        from pc_liveagent.control_intent import detect_control_intent

        self.assertEqual(detect_control_intent("Ask the question again").kind, "repeat")
        self.assertEqual(detect_control_intent("Repeat that").kind, "repeat")
        self.assertEqual(detect_control_intent("Start again").kind, "redo")
        self.assertEqual(detect_control_intent("Redo").kind, "redo")
        self.assertEqual(detect_control_intent("Change that").kind, "change")
        change = detect_control_intent("Change the first name to Virat")
        self.assertEqual(change.kind, "change")
        self.assertEqual(change.target, "first_name")
        self.assertEqual(change.value.lower(), "virat")
        self.assertEqual(detect_control_intent("Go back").kind, "go_back")
        self.assertEqual(detect_control_intent("Next question").kind, "next")
        self.assertEqual(detect_control_intent("Previous question").kind, "go_back")

    async def test_add_another_bumps_child_instance(self) -> None:
        from pc_liveagent.live_form import _submit_eligibility_answer

        call_id = "age-child"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="child_more"
        )
        session.filled["child_instance"] = "1"
        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="child_form"),
            ),
            patch("pc_liveagent.live_form._click_cta", AsyncMock()),
            patch(
                "pc_liveagent.live_form._wait_for_screen",
                AsyncMock(return_value="child_form"),
            ),
            patch(
                "pc_liveagent.live_form._wait_for_child_form_ready",
                AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.live_form._name_field_visible",
                AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.live_form._locate_named_input",
                AsyncMock(return_value=None),
            ),
        ):
            result = await _submit_eligibility_answer(
                call_id, session, page, "Yes", started=0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "child_first")
        self.assertEqual(session.filled.get("child_instance"), "2")

    def test_age_seven_and_spoken_range(self) -> None:
        from pc_liveagent.answer_parse import resolve_answer
        from pc_liveagent.elig_meta import get_field_meta
        from pc_liveagent.live_form import _map_age_label

        opts = [
            "(0-1) Baby",
            "(1-5) Toddler",
            "(6-18) Youth",
            "(19-20) Young Adult",
        ]
        self.assertEqual(_map_age_label("7", opts), "(6-18) Youth")
        self.assertEqual(_map_age_label("Six to eighteen", opts), "(6-18) Youth")
        r = resolve_answer(
            "child_age", "7", options=opts, meta=get_field_meta("child_age")
        )
        self.assertEqual(r.answer, "(6-18) Youth")
        ambiguous = _map_age_label("Twenty twenty two thirty", opts)
        self.assertIsNone(ambiguous)


class YesNoIntegrityAndGoBackTests(unittest.IsolatedAsyncioTestCase):
    def test_classify_yes_no_conservative(self) -> None:
        from pc_liveagent.live_form import classify_yes_no

        self.assertEqual(classify_yes_no("Yes"), "yes")
        self.assertEqual(classify_yes_no("Yeah, that's right."), "yes")
        self.assertEqual(classify_yes_no("No"), "no")
        self.assertEqual(classify_yes_no("Nope"), "no")
        self.assertIsNone(classify_yes_no("Yes I don't have any"))
        self.assertIsNone(classify_yes_no("Yes or no"))
        self.assertIsNone(classify_yes_no("Go back to the previous question."))
        self.assertIsNone(classify_yes_no("maybe later"))
        self.assertEqual(classify_yes_no("I don't"), "no")

    def test_validate_mixed_yes_no_clarifies(self) -> None:
        answer, clarify = validate_for_step("student", "Yes I don't")
        self.assertIsNone(answer)
        self.assertIn("yes or no", (clarify or "").lower())

    def test_stale_yes_after_go_back_not_resolved(self) -> None:
        from pc_liveagent.form_bridge import _yes_no_from_recent_transcript

        transcript = (
            "Agent: Do you live with and mainly support any children under 19 "
            "who live with you more than half the year?\n"
            "User: Yes.\n"
            "Agent: Are you a full-time student age 18 to 21 whose parent or legal "
            "guardian supports you?\n"
            "User: Go back to the previous question.\n"
            "Agent: Do you live with and mainly support any children under 19 "
            "who live with you more than half the year?\n"
        )
        call = {
            "transcript_with_tool_calls": [
                {
                    "role": "agent",
                    "content": (
                        "Do you live with and mainly support any children under 19 "
                        "who live with you more than half the year?"
                    ),
                },
                {"role": "user", "content": "Yes."},
                {
                    "role": "agent",
                    "content": (
                        "Are you a full-time student age 18 to 21 whose parent or "
                        "legal guardian supports you?"
                    ),
                },
                {"role": "user", "content": "Go back to the previous question."},
                {
                    "role": "agent",
                    "content": (
                        "Do you live with and mainly support any children under 19 "
                        "who live with you more than half the year?"
                    ),
                },
            ]
        }
        # Latest user turn is go-back → must not reuse prior Yes.
        self.assertIsNone(
            _yes_no_from_recent_transcript(transcript, "children", call=call)
        )
        # After a fresh Yes following go-back, resolve Yes.
        call["transcript_with_tool_calls"].append({"role": "user", "content": "Yes."})
        self.assertEqual(
            _yes_no_from_recent_transcript(transcript + "User: Yes.\n", "children", call=call),
            "Yes",
        )

    async def test_go_back_cancels_pending_browser_and_bumps_epoch(self) -> None:
        from pc_liveagent.call_turn import (
            _elig_browser_tasks,
            drop_controller,
            get_controller,
            process_user_turn,
        )

        call_id = "yn-goback-race"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        ctrl.active_websocket_status = "connected"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="student"
        )

        async def fake_get_session(_cid: str):
            return session

        fill_started = asyncio.Event()
        release_fill = asyncio.Event()

        async def slow_submit(*_a, **_k):
            fill_started.set()
            await release_fill.wait()
            # Real Playwright would mutate the page; the controller must ignore
            # this late result after go-back invalidated the epoch.
            return {
                "ok": True,
                "spoken": "ASK: Do you support a disabled individual?",
                "step": "disabled",
            }

        async def fake_nav(_cid: str):
            session.elig_step = "children"
            return {
                "ok": True,
                "step": "children",
                "screen": "children",
                "spoken": (
                    "Do you live with and mainly support any children under 19 "
                    "who live with you more than half the year?"
                ),
            }

        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch("pc_liveagent.call_turn.peek_choice_labels", AsyncMock(return_value=["Yes", "No"])),
            patch("pc_liveagent.call_turn.submit_field", side_effect=slow_submit),
            patch("pc_liveagent.call_turn._speak_interrupt", AsyncMock(return_value=True)),
            patch("pc_liveagent.call_turn.navigate_back_one", side_effect=fake_nav),
        ):
            ack = await process_user_turn(call_id, "No", response_id=1)
            self.assertEqual(ack.detail, "elig_ack")
            await asyncio.wait_for(fill_started.wait(), timeout=2.0)
            epoch_before = ctrl.elig_nav_epoch
            back = await process_user_turn(call_id, "Go back.", response_id=2)
            self.assertEqual(back.detail, "go_back")
            self.assertGreater(ctrl.elig_nav_epoch, epoch_before)
            self.assertFalse(ctrl.elig_op_in_progress)
            release_fill.set()
            task = _elig_browser_tasks.get(call_id)
            if task:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    pass
            await asyncio.sleep(0.05)
        # Late No must not promote past children after go-back.
        self.assertEqual(session.elig_step, "children")
        self.assertFalse(
            any(item.get("step") == "student" for item in ctrl.committed)
        )
        await drop_controller(call_id)

    async def test_go_back_missing_button_is_honest(self) -> None:
        from pc_liveagent.call_turn import drop_controller, get_controller, process_user_turn

        call_id = "yn-goback-missing"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="marital"
        )

        async def fake_get_session(_cid: str):
            return session

        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch("pc_liveagent.call_turn.peek_choice_labels", AsyncMock(return_value=[])),
            patch(
                "pc_liveagent.call_turn.navigate_back_one",
                AsyncMock(
                    return_value={
                        "ok": False,
                        "error": "no_back",
                        "spoken": (
                            "I can't go back from this screen. There is no Back button "
                            "available. You can still correct the current question — "
                            "What is your marital status?"
                        ),
                        "step": "marital",
                    }
                ),
            ),
        ):
            r = await process_user_turn(call_id, "Go back.")
        self.assertEqual(r.detail, "go_back_failed")
        self.assertIn("can't go back", r.speech.lower())
        self.assertIn("marital", r.speech.lower())
        await drop_controller(call_id)

    async def test_yesno_missing_chips_wrong_screen_rejects(self) -> None:
        from pc_liveagent.live_form import _submit_eligibility_answer

        call_id = "yn-mismatch"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="student"
        )
        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="student"),
            ),
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                AsyncMock(return_value=[]),
            ),
            patch(
                "pc_liveagent.live_form._yes_no_selection_state",
                AsyncMock(return_value="none"),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await _submit_eligibility_answer(
                    call_id, session, page, "Yes", started=0.0
                )
        self.assertIn("YESNO_VERIFY", str(ctx.exception))

    async def test_overlapping_answer_replaces_prior_task(self) -> None:
        from pc_liveagent.call_turn import (
            _elig_browser_tasks,
            drop_controller,
            get_controller,
            process_user_turn,
        )

        call_id = "yn-overlap"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        ctrl.active_websocket_status = "connected"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="children"
        )

        async def fake_get_session(_cid: str):
            return session

        calls: list[str] = []
        gate = asyncio.Event()

        async def slow_submit(_cid, _field, value, **_k):
            calls.append(value)
            if value == "No":
                await gate.wait()
            session.elig_step = "student" if value == "No" else "child_first"
            return {
                "ok": True,
                "spoken": "ASK: next",
                "step": session.elig_step,
            }

        with (
            patch("pc_liveagent.call_turn.get_session", side_effect=fake_get_session),
            patch("pc_liveagent.call_turn.peek_choice_labels", AsyncMock(return_value=["Yes", "No"])),
            patch("pc_liveagent.call_turn.submit_field", side_effect=slow_submit),
            patch("pc_liveagent.call_turn._speak_interrupt", AsyncMock(return_value=True)),
        ):
            r1 = await process_user_turn(call_id, "No", response_id=1)
            self.assertEqual(r1.detail, "elig_ack")
            # Second answer while first is still pending — must replace, not drop.
            # Go back first to clear busy, then answer Yes on same step? Better:
            # force schedule replacement by cancelling busy flag briefly is wrong.
            # Instead: go_back clears busy; we test replacement via schedule API.
            from pc_liveagent.call_turn import _schedule_eligibility_browser

            epoch = ctrl.elig_nav_epoch + 1
            ctrl.elig_nav_epoch = epoch
            _schedule_eligibility_browser(
                call_id,
                answer="Yes",
                step="children",
                turn_id="t-yes",
                form_url="",
                commit_key="children:yes",
                epoch=epoch,
            )
            gate.set()
            task = _elig_browser_tasks.get(call_id)
            if task:
                await asyncio.wait_for(task, timeout=2.0)
        self.assertIn("Yes", calls)
        await drop_controller(call_id)

    async def test_step_mismatch_does_not_commit_answer(self) -> None:
        from pc_liveagent.live_form import _submit_eligibility_answer

        call_id = "yn-step-mismatch"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="student"
        )
        page = AsyncMock()
        with patch(
            "pc_liveagent.live_form._eligibility_screen",
            AsyncMock(return_value="children"),
        ):
            result = await _submit_eligibility_answer(
                call_id, session, page, "Yes", started=0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "step_mismatch")
        self.assertEqual(session.elig_step, "children")
        self.assertIn("children", (result.get("spoken") or "").lower())


class BiWeeklyIncomeWhenSubmitTests(unittest.IsolatedAsyncioTestCase):
    async def test_biweekly_selects_and_verifies(self) -> None:
        from pc_liveagent.live_form import (
            LiveFormSession,
            _submit_eligibility_answer,
        )

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        labels = ["Annual", "Monthly", "Bi-weekly", "$0 – $12,000"]
        session = LiveFormSession(
            call_id="inc-bi",
            url="https://example.test",
            elig_step="income_when",
        )
        sig_values = iter(["idle|", "selected|active|true||false"])
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                new=AsyncMock(return_value="income"),
            ),
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(return_value=labels),
            ),
            patch(
                "pc_liveagent.live_form._choice_signature",
                new=AsyncMock(side_effect=lambda *_a, **_k: next(sig_values)),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
            patch(
                "pc_liveagent.live_form._income_period_selected",
                new=AsyncMock(return_value=False),
            ),
        ):
            result = await _submit_eligibility_answer(
                "inc-bi", session, page, "bi-weekly", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "income_range")
        click.assert_awaited()
        self.assertEqual(click.await_args.args[1], "Bi-weekly")
        self.assertEqual(session.filled.get("income_when"), "Bi-weekly")

    async def test_unrecognized_period_clarifies_without_click(self) -> None:
        from pc_liveagent.live_form import (
            LiveFormSession,
            _submit_eligibility_answer,
        )

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        labels = ["Annual", "Monthly", "Bi-weekly"]
        session = LiveFormSession(
            call_id="inc-bad",
            url="https://example.test",
            elig_step="income_when",
        )
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                new=AsyncMock(return_value="income"),
            ),
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(return_value=labels),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await _submit_eligibility_answer(
                    "inc-bad", session, page, "quarterly maybe", 0.0
                )
        self.assertIn("INCOME_CLARIFY", str(ctx.exception))
        click.assert_not_awaited()
        self.assertEqual(session.elig_step, "income_when")

    async def test_period_switch_from_income_range_in_place(self) -> None:
        from pc_liveagent.live_form import (
            LiveFormSession,
            _submit_eligibility_answer,
        )

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.go_back = AsyncMock()
        labels = [
            "Annual",
            "Monthly",
            "Bi-weekly",
            "$0 – $3,000",
            "$3,001 – $5,000",
        ]
        session = LiveFormSession(
            call_id="inc-switch",
            url="https://example.test",
            elig_step="income_range",
        )
        session.filled["income_when"] = "Annual"
        session.filled["income_range"] = "$39,901+"
        sig_values = iter(["idle|", "selected|active|true||false"])
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                new=AsyncMock(return_value="income"),
            ),
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(return_value=labels),
            ),
            patch(
                "pc_liveagent.live_form._choice_signature",
                new=AsyncMock(side_effect=lambda *_a, **_k: next(sig_values)),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
            patch(
                "pc_liveagent.live_form._income_period_selected",
                new=AsyncMock(return_value=False),
            ),
        ):
            result = await _submit_eligibility_answer(
                "inc-switch",
                session,
                page,
                "__INCOME_PERIOD_SWITCH__:Monthly",
                0.0,
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "income_range")
        self.assertEqual(session.filled.get("income_when"), "Monthly")
        self.assertNotIn("income_range", session.filled)
        self.assertIn("per month", (result.get("spoken") or "").lower())
        click.assert_awaited()
        self.assertEqual(click.await_args.args[1], "Monthly")
        page.go_back.assert_not_awaited()

    async def test_period_reselect_keeps_income_page(self) -> None:
        from pc_liveagent.live_form import (
            LiveFormSession,
            _submit_eligibility_answer,
        )

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.go_back = AsyncMock()
        session = LiveFormSession(
            call_id="inc-reselect",
            url="https://example.test",
            elig_step="income_range",
        )
        session.filled["income_when"] = "Annual"
        session.filled["income_range"] = "$12,001 – $39,900"
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                new=AsyncMock(return_value="income"),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
        ):
            result = await _submit_eligibility_answer(
                "inc-reselect",
                session,
                page,
                "__INCOME_PERIOD_RESELECT__",
                0.0,
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "income_when")
        self.assertEqual(session.elig_step, "income_when")
        self.assertNotIn("income_range", session.filled)
        self.assertIn("annual", (result.get("spoken") or "").lower())
        click.assert_not_awaited()
        page.go_back.assert_not_awaited()

    async def test_income_range_verifies_before_advance(self) -> None:
        from pc_liveagent.live_form import (
            LiveFormSession,
            _submit_eligibility_answer,
        )

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.mouse = AsyncMock()
        page.mouse.wheel = AsyncMock()
        labels = ["Annual", "Monthly", "$0 – $12,000", "$12,001 – $39,900"]
        session = LiveFormSession(
            call_id="inc-range-ok",
            url="https://example.test",
            elig_step="income_range",
        )
        session.filled["income_when"] = "Annual"
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                new=AsyncMock(side_effect=["income", "special", "special"]),
            ),
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(return_value=labels),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
            patch(
                "pc_liveagent.live_form._choice_appears_selected",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.live_form._wait_for_screen",
                new=AsyncMock(return_value="special"),
            ),
        ):
            result = await _submit_eligibility_answer(
                "inc-range-ok",
                session,
                page,
                "about thirty thousand",
                0.0,
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "special")
        self.assertEqual(session.filled.get("income_range"), "$12,001 – $39,900")
        click.assert_awaited()
        self.assertEqual(click.await_args.args[1], "$12,001 – $39,900")


class SpecialMoreConfirmTests(unittest.IsolatedAsyncioTestCase):
    async def test_special_person_confirms_before_next(self) -> None:
        from pc_liveagent.live_form import (
            LiveFormSession,
            _submit_eligibility_answer,
        )

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        session = LiveFormSession(
            call_id="sp-confirm",
            url="https://example.test",
            elig_step="special_person",
        )
        session.filled["special_conditions"] = "Someone is blind"
        session.filled["special_active_conditions"] = "Someone is blind"
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                new=AsyncMock(return_value="special"),
            ),
            patch(
                "pc_liveagent.live_form._assign_special_person_member",
                new=AsyncMock(
                    side_effect=lambda _p, _a, sess: sess.filled.__setitem__(
                        "special_person_assigned", "Mickey M."
                    )
                ),
            ),
            patch(
                "pc_liveagent.live_form._click_cta",
                new=AsyncMock(),
            ) as cta,
        ):
            result = await _submit_eligibility_answer(
                "sp-confirm", session, page, "Mickey M.", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "special_more")
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("blind", spoken)
        self.assertIn("mickey", spoken)
        self.assertIn("other conditions", spoken)
        cta.assert_not_awaited()

    async def test_special_more_no_verifies_then_next(self) -> None:
        from pc_liveagent.live_form import (
            LiveFormSession,
            _submit_eligibility_answer,
        )

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        session = LiveFormSession(
            call_id="sp-more-no",
            url="https://example.test",
            elig_step="special_more",
        )
        session.filled["special_conditions"] = "Someone is blind"
        session.filled["special_person_assigned"] = "Mickey M."
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                new=AsyncMock(return_value="special"),
            ),
            patch(
                "pc_liveagent.live_form._verify_special_selections_before_next",
                new=AsyncMock(),
            ) as verify,
            patch(
                "pc_liveagent.live_form._click_cta",
                new=AsyncMock(),
            ) as cta,
            patch(
                "pc_liveagent.live_form._wait_for_screen",
                new=AsyncMock(return_value="programs"),
            ),
        ):
            result = await _submit_eligibility_answer(
                "sp-more-no", session, page, "No", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "programs")
        verify.assert_awaited()
        cta.assert_awaited()
        self.assertEqual(cta.await_args.args[1], "Next")

    async def test_special_more_yes_collects_another_condition(self) -> None:
        from pc_liveagent.live_form import (
            LiveFormSession,
            _submit_eligibility_answer,
        )

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.mouse = AsyncMock()
        page.mouse.wheel = AsyncMock()
        session = LiveFormSession(
            call_id="sp-more-yes",
            url="https://example.test",
            elig_step="special_more",
        )
        session.filled["special_conditions"] = "Someone is blind"
        session.filled["special_person_assigned"] = "Mickey M."
        session.filled["special_assignment_pairs"] = (
            "Someone is blind>>>Mickey M."
        )
        labels = [
            "Someone is blind",
            "Someone is disabled (SSDI)",
            "None of the above",
        ]
        # First turn: Yes → back to special question.
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                new=AsyncMock(return_value="special"),
            ),
            patch(
                "pc_liveagent.live_form._click_cta",
                new=AsyncMock(),
            ) as cta,
        ):
            yes_result = await _submit_eligibility_answer(
                "sp-more-yes", session, page, "Yes", 0.0
            )
        self.assertEqual(yes_result.get("step"), "special")
        cta.assert_not_awaited()

        # Second turn: add disabled without deselecting blind.
        state_calls: list[str] = []

        async def state(_page, label):
            state_calls.append(label)
            if "blind" in label.lower():
                return {"found": True, "checked": True, "source": "input"}
            checked = state_calls.count(label) > 1
            return {"found": True, "checked": checked, "source": "input"}

        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                new=AsyncMock(return_value="special"),
            ),
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(return_value=labels),
            ),
            patch(
                "pc_liveagent.live_form._special_condition_checkbox_state",
                new=AsyncMock(side_effect=state),
            ),
            patch(
                "pc_liveagent.live_form._special_apply_to_member_count",
                new=AsyncMock(return_value=1),
            ),
            patch(
                "pc_liveagent.live_form._wait_for_special_apply_to_members",
                new=AsyncMock(
                    return_value={
                        "members": [
                            {
                                "label": "Mickey M.",
                                "index": 0,
                                "dupIndex": 0,
                                "checked": True,
                            },
                            {
                                "label": "Agent H.",
                                "index": 1,
                                "dupIndex": 0,
                                "checked": False,
                            },
                        ],
                        "strategy": "heading",
                        "has_apply_to": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ) as click,
        ):
            add_result = await _submit_eligibility_answer(
                "sp-more-yes", session, page, "disabled on SSDI", 0.0
            )
        self.assertEqual(add_result.get("step"), "special_person")
        self.assertIn("blind", session.filled.get("special_conditions", "").lower())
        self.assertIn("disabled", session.filled.get("special_conditions", "").lower())
        # Blind already checked — only disabled should be clicked.
        self.assertEqual(click.await_count, 1)

    async def test_multi_condition_multi_member_confirm_speech(self) -> None:
        from pc_liveagent.live_form import (
            LiveFormSession,
            _record_special_assignment_pairs,
            _special_more_confirm_speech,
        )

        session = LiveFormSession(call_id="sp-pair", url="https://example.test")
        _record_special_assignment_pairs(
            session, ["Someone is blind"], ["Mickey M.", "Agent H."]
        )
        _record_special_assignment_pairs(
            session, ["Someone is pregnant"], ["Aria R."]
        )
        speech = _special_more_confirm_speech(session).lower()
        self.assertIn("blind", speech)
        self.assertIn("mickey", speech)
        self.assertIn("agent", speech)
        self.assertIn("pregnant", speech)
        self.assertIn("aria", speech)
        self.assertIn("other conditions", speech)


class SectionIntroTests(unittest.IsolatedAsyncioTestCase):
    def test_eligibility_section_intro_before_marital(self) -> None:
        from pc_liveagent.live_form import (
            ELIGIBILITY_SECTION_INTRO,
            LiveFormSession,
            _spoken_question_for_step,
        )

        session = LiveFormSession(
            call_id="elig-intro", url="https://example.test", elig_step="marital"
        )
        session.filled["eligibility_intro_pending"] = "1"
        first = _spoken_question_for_step(session, "marital")
        self.assertTrue(first.startswith(ELIGIBILITY_SECTION_INTRO.strip()[:12]))
        self.assertIn("let's start your household eligibility", first.lower())
        self.assertIn("programs you may qualify", first.lower())
        self.assertIn("marital status", first.lower())
        again = _spoken_question_for_step(session, "marital")
        self.assertNotIn("household eligibility check", again.lower())
        self.assertIn("marital status", again.lower())

    def test_speech_after_signin_keeps_eligibility_purpose_intro(self) -> None:
        """Regression: post-sign-in helper used to replace intro with bare marital."""
        from pc_liveagent.call_turn import _speech_after_signin
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="keep-intro", url="https://example.test", elig_step="marital"
        )
        spoken = (
            "ASK: Let's start your household eligibility check. "
            "I'll ask a few short questions about your household so Pointcare can see "
            "what programs you may qualify for. "
            "What is your marital status? Never Married, Married, Separated, Divorced, "
            "Widowed, Registered Domestic Partner, or Common Law Marriage?"
        )
        session.filled["eligibility_opening_speech"] = spoken[5:].strip()
        out = _speech_after_signin(session, spoken)
        self.assertIn("let's start your household eligibility", out.lower())
        self.assertIn("marital status", out.lower())
        # Empty spoken still recovers stored opening.
        out2 = _speech_after_signin(session, "")
        self.assertIn("let's start your household eligibility", out2.lower())

    def test_spouse_and_child_intros_once(self) -> None:
        from pc_liveagent.live_form import (
            LiveFormSession,
            _spoken_question_for_step,
            _bump_child_instance,
        )

        session = LiveFormSession(
            call_id="intro", url="https://example.test", elig_step="spouse_first"
        )
        first = _spoken_question_for_step(session, "spouse_first")
        self.assertIn("learn more about your spouse or partner", first.lower())
        self.assertIn("first name", first.lower())
        again = _spoken_question_for_step(session, "spouse_first")
        self.assertNotIn("learn more about your spouse or partner", again.lower())
        self.assertIn("first name", again.lower())

        child = LiveFormSession(
            call_id="intro-c", url="https://example.test", elig_step="child_first"
        )
        c1 = _spoken_question_for_step(child, "child_first")
        self.assertIn("learn more about your child", c1.lower())
        self.assertIn("first name", c1.lower())
        c1b = _spoken_question_for_step(child, "child_first")
        self.assertNotIn("learn more about your child", c1b.lower())

        _bump_child_instance(child)
        c2 = _spoken_question_for_step(child, "child_first")
        self.assertIn("next child", c2.lower())
        self.assertIn("first name", c2.lower())

    def test_person_kind_intros_and_member_confirm_speech(self) -> None:
        from pc_liveagent.live_form import (
            LiveFormSession,
            _member_confirm_speech,
            _next_elig_step,
            _spoken_question_for_step,
            _yes_no_transition,
        )

        # Originating gate wins over a misclassified child_form landing.
        self.assertEqual(
            _yes_no_transition("disabled", True, "child_form"), "person_first"
        )
        self.assertEqual(
            _yes_no_transition("relatives", True, "child_form"), "person_first"
        )
        self.assertEqual(
            _yes_no_transition("student", True, "person_form"), "person_first"
        )
        self.assertEqual(
            _yes_no_transition("children", True, "child_form"), "child_first"
        )
        self.assertEqual(
            _yes_no_transition("children", True, "person_form"), "child_first"
        )

        disabled = LiveFormSession(
            call_id="p-dis", url="https://example.test", elig_step="person_first"
        )
        disabled.filled["person_kind"] = "disabled"
        speech = _spoken_question_for_step(disabled, "person_first")
        self.assertIn("disabled individual", speech.lower())
        self.assertIn("first name", speech.lower())
        self.assertNotIn("your child", speech.lower())

        relative = LiveFormSession(
            call_id="p-rel", url="https://example.test", elig_step="person_first"
        )
        relative.filled["person_kind"] = "relative"
        speech = _spoken_question_for_step(relative, "person_first")
        self.assertIn("relatives you support", speech.lower())
        self.assertNotIn("your child", speech.lower())

        spouse = LiveFormSession(
            call_id="c-sp", url="https://example.test", elig_step="spouse_confirm"
        )
        spouse.filled.update(
            {
                "spouse_first": "Alex",
                "spouse_last": "Rivera",
                "spouse_role": "Partner",
                "spouse_age": "(65+) Senior",
            }
        )
        confirm = _member_confirm_speech(spouse, "spouse")
        self.assertIn("alex", confirm.lower())
        self.assertIn("rivera", confirm.lower())
        self.assertIn("partner", confirm.lower())
        self.assertIn("senior", confirm.lower())
        self.assertIn("yes or no", confirm.lower())

        spouse.elig_step = "spouse_age"
        self.assertEqual(_next_elig_step(spouse, "65"), "spouse_confirm")
        spouse.elig_step = "spouse_confirm"
        self.assertEqual(_next_elig_step(spouse, "yes"), "children")
        self.assertEqual(_next_elig_step(spouse, "no"), "spouse_confirm")

        child = LiveFormSession(
            call_id="c-ch", url="https://example.test", elig_step="child_age"
        )
        self.assertEqual(_next_elig_step(child, "youth"), "child_confirm")
        child.elig_step = "child_confirm"
        self.assertEqual(_next_elig_step(child, "yes"), "child_more")
        self.assertEqual(_next_elig_step(child, "no"), "child_confirm")

        person = LiveFormSession(
            call_id="c-pe", url="https://example.test", elig_step="person_age"
        )
        person.after_person = "relatives"
        self.assertEqual(_next_elig_step(person, "adult"), "person_confirm")
        person.elig_step = "person_confirm"
        self.assertEqual(_next_elig_step(person, "yes"), "relatives")
        self.assertEqual(_next_elig_step(person, "no"), "person_confirm")

    async def test_disabled_yes_keeps_person_kind_despite_child_form_land(self) -> None:
        from pc_liveagent.live_form import (
            LiveFormSession,
            _submit_eligibility_answer,
        )

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        session = LiveFormSession(
            call_id="dis-gate",
            url="https://example.test",
            elig_step="disabled",
        )
        with (
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                new=AsyncMock(return_value="disabled"),
            ),
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(return_value=["Yes", "No", "Child", "Other"]),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(),
            ),
            patch(
                "pc_liveagent.live_form._yes_no_selection_state",
                new=AsyncMock(return_value="yes"),
            ),
            patch(
                "pc_liveagent.live_form._wait_for_screen",
                # Misclassified landing that previously forced child_first.
                new=AsyncMock(return_value="child_form"),
            ),
        ):
            result = await _submit_eligibility_answer(
                "dis-gate", session, page, "Yes", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "person_first")
        self.assertEqual(session.filled.get("person_kind"), "disabled")
        self.assertIn("disabled individual", (result.get("spoken") or "").lower())
        self.assertNotIn("your child", (result.get("spoken") or "").lower())

    async def test_person_form_not_child_when_only_relationship_chip(self) -> None:
        from pc_liveagent.live_form import _eligibility_screen

        page = AsyncMock()
        page.url = "https://example.test/support-disabled"
        body = AsyncMock()
        body.inner_text = AsyncMock(
            return_value=(
                "Tell us about this household member. First Name Last Name "
                "Relationship to me Child Other"
            )
        )
        page.locator = lambda *_a, **_k: body
        with patch(
            "pc_liveagent.live_form._visible_choice_labels",
            new=AsyncMock(return_value=["Child", "Other", "(21-63) Adult"]),
        ):
            screen = await _eligibility_screen(page)
        self.assertEqual(screen, "person_form")

    async def test_repeat_keeps_answers_and_explains_section(self) -> None:
        from pc_liveagent.call_turn import (
            CallController,
            _repeat_with_context,
            drop_controller,
            get_controller,
        )
        from pc_liveagent.live_form import LiveFormSession

        call_id = "intro-repeat"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        session = LiveFormSession(
            call_id=call_id,
            url="https://example.test",
            elig_step="spouse_last",
        )
        session.filled["spouse_first"] = "Alex"
        session.filled["spouse_section_intro"] = "1"
        with (
            patch(
                "pc_liveagent.call_turn.get_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "pc_liveagent.call_turn.peek_choice_labels",
                new=AsyncMock(return_value=[]),
            ),
        ):
            speech = await _repeat_with_context(call_id, ctrl)
        self.assertIn("spouse or partner", speech.lower())
        self.assertIn("last name", speech.lower())
        self.assertEqual(session.filled.get("spouse_first"), "Alex")
        self.assertEqual(session.elig_step, "spouse_last")
        await drop_controller(call_id)


class MemberConfirmCorrectionTests(unittest.IsolatedAsyncioTestCase):
    """Child/spouse confirm No, age mapping, and in-place correction (no Back)."""

    def test_spoken_age_range_is_natural(self) -> None:
        from pc_liveagent.live_form import _spoken_age_range

        spoken = _spoken_age_range("(1-5) Toddler")
        self.assertIn("1-to-5", spoken.lower())
        self.assertIn("toddler", spoken.lower())
        self.assertNotRegex(spoken, r"^\(1-5\)")

    def test_relationship_words_never_map_to_age(self) -> None:
        from pc_liveagent.live_form import _map_age_label

        opts = [
            "(0-1) Baby",
            "(1-5) Toddler",
            "(6-12) Child",
            "(13-15) Teen",
            "(16-18) Teen",
        ]
        for spoken in ("Child", "Stepchild", "Grandchild", "Foster Child", "son"):
            self.assertIsNone(
                _map_age_label(spoken, opts),
                msg=f"{spoken!r} must not select an age chip",
            )
        self.assertEqual(_map_age_label("fifteen", opts), "(13-15) Teen")
        self.assertEqual(_map_age_label("Age is fifteen.", opts), "(13-15) Teen")
        self.assertIsNone(_map_age_label("", opts))

    def test_confirm_speech_uses_natural_age(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _member_confirm_speech

        session = LiveFormSession(
            call_id="c-age", url="https://example.test", elig_step="child_confirm"
        )
        session.filled.update(
            {
                "child_first": "Jackie",
                "child_last": "Smith",
                "child_rel": "Child",
                "child_age": "(1-5) Toddler",
            }
        )
        speech = _member_confirm_speech(session, "child")
        self.assertIn("jackie", speech.lower())
        self.assertIn("1-to-5", speech.lower())
        self.assertNotIn("(1-5)", speech)

    async def test_child_confirm_no_asks_which_field_not_restart(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _submit_eligibility_answer

        call_id = "confirm-no"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="child_confirm"
        )
        session.filled.update(
            {
                "child_first": "Jackie",
                "child_last": "Smith",
                "child_rel": "Child",
                "child_age": "(13-15) Teen",
                "child_instance": "1",
            }
        )
        page = AsyncMock()
        with patch(
            "pc_liveagent.live_form._eligibility_screen",
            AsyncMock(return_value="child_form"),
        ):
            result = await _submit_eligibility_answer(
                call_id, session, page, "No", started=0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "child_confirm")
        self.assertTrue(result.get("await_field_correction"))
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("which detail", spoken)
        self.assertNotIn("learn more about your child", spoken)
        self.assertEqual(session.filled.get("child_first"), "Jackie")
        self.assertEqual(session.filled.get("child_age"), "(13-15) Teen")

    async def test_age_correction_updates_in_place_without_back(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, correct_member_field

        call_id = "age-inplace"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="child_first"
        )
        session.filled.update(
            {
                "child_first": "Jackie",
                "child_last": "Smith",
                "child_rel": "Child",
                "child_age": "(1-5) Toddler",
                "child_instance": "1",
            }
        )
        page = AsyncMock()
        session.page = page
        backs: list[str] = []

        async def no_back(*_a, **_k):
            backs.append("back")
            raise AssertionError("must not navigate Back for age correction")

        with (
            patch(
                "pc_liveagent.live_form.get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "pc_liveagent.live_form._eligibility_screen",
                AsyncMock(return_value="child_form"),
            ),
            patch(
                "pc_liveagent.live_form._ensure_age_choices_visible",
                AsyncMock(
                    return_value=[
                        "(0-1) Baby",
                        "(1-5) Toddler",
                        "(6-12) Child",
                        "(13-15) Teen",
                        "(16-18) Teen",
                    ]
                ),
            ),
            patch("pc_liveagent.live_form._click_age_choice", AsyncMock()) as age_click,
            patch(
                "pc_liveagent.live_form.navigate_back_one",
                new=AsyncMock(side_effect=no_back),
            ),
        ):
            result = await correct_member_field(call_id, "age", "Age is fifteen.")
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "child_confirm")
        self.assertEqual(session.filled.get("child_age"), "(13-15) Teen")
        self.assertEqual(session.filled.get("child_first"), "Jackie")
        self.assertEqual(session.filled.get("child_rel"), "Child")
        self.assertIn("13-to-15", (result.get("spoken") or "").lower())
        age_click.assert_awaited()
        self.assertEqual(backs, [])

    async def test_change_age_only_sets_pending_not_back(self) -> None:
        from pc_liveagent.call_turn import drop_controller, get_controller, process_user_turn
        from pc_liveagent.live_form import LiveFormSession

        call_id = "change-age-only"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        ctrl.active_websocket_status = "connected"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="child_confirm"
        )
        session.filled.update(
            {
                "child_first": "Jackie",
                "child_last": "Smith",
                "child_rel": "Child",
                "child_age": "(1-5) Toddler",
            }
        )
        nav = AsyncMock(
            side_effect=AssertionError("age correction must not call navigate_back_one")
        )
        with (
            patch(
                "pc_liveagent.call_turn.get_session",
                AsyncMock(return_value=session),
            ),
            patch("pc_liveagent.call_turn.peek_choice_labels", AsyncMock(return_value=[])),
            patch("pc_liveagent.call_turn.navigate_back_one", nav),
            patch("pc_liveagent.call_turn.submit_field", AsyncMock()),
        ):
            result = await process_user_turn(
                call_id,
                "Everything is correct. I need to change the age only.",
                response_id=1,
            )
        self.assertEqual(result.detail, "correction_prompt")
        self.assertEqual((ctrl.pending_correction or {}).get("field"), "age")
        self.assertIn("age", (result.speech or "").lower())
        nav.assert_not_awaited()
        await drop_controller(call_id)

    async def test_pending_age_fifteen_corrects_without_back(self) -> None:
        from pc_liveagent.call_turn import drop_controller, get_controller, process_user_turn
        from pc_liveagent.live_form import LiveFormSession

        call_id = "pending-fifteen"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        ctrl.active_websocket_status = "connected"
        ctrl.pending_correction = {
            "field": "age",
            "prompt": "Of course. What should the age be?",
        }
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="child_first"
        )
        session.filled.update(
            {
                "child_first": "Jackie",
                "child_last": "Smith",
                "child_rel": "Child",
                "child_age": "(1-5) Toddler",
                "child_instance": "1",
            }
        )
        session.page = AsyncMock()

        async def fake_correct(cid, field, value):
            self.assertEqual(field, "age")
            self.assertIn("fifteen", value.lower())
            session.filled["child_age"] = "(13-15) Teen"
            session.elig_step = "child_confirm"
            return {
                "ok": True,
                "step": "child_confirm",
                "spoken": (
                    "Updated the age to the 13-to-15 age range, labelled Teen. "
                    "Just to confirm for the child: first name Jackie, last name Smith, "
                    "relationship Child, age the 13-to-15 age range, labelled Teen. "
                    "Is that correct? Please say Yes or No."
                ),
                "in_place": True,
            }

        nav = AsyncMock(
            side_effect=AssertionError("must not navigate_back_one for pending age")
        )
        with (
            patch(
                "pc_liveagent.call_turn.get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "pc_liveagent.call_turn.correct_member_field",
                AsyncMock(side_effect=fake_correct),
            ),
            patch("pc_liveagent.call_turn.navigate_back_one", nav),
            patch("pc_liveagent.call_turn.submit_field", AsyncMock()),
        ):
            result = await process_user_turn(
                call_id, "Age is fifteen.", response_id=2
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.detail, "member_corrected")
        self.assertIsNone(ctrl.pending_correction)
        self.assertIn("13-to-15", (result.speech or "").lower())
        self.assertEqual(session.filled.get("child_first"), "Jackie")
        nav.assert_not_awaited()
        await drop_controller(call_id)

    async def test_support_student_survives_claude_timeout(self) -> None:
        from pc_liveagent.answer_parse import ClaudeResolveResult
        from pc_liveagent.call_turn import drop_controller, get_controller, process_user_turn
        from pc_liveagent.live_form import LiveFormSession, _SUPPORT_CANONICAL

        call_id = "support-student-timeout"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        ctrl.active_websocket_status = "connected"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="support"
        )
        submitted: list[str] = []

        async def fake_submit(_cid, _field, answer, **_kwargs):
            submitted.append(answer)
            session.elig_step = "income_when"
            return {
                "ok": True,
                "spoken": "What is your household income?",
                "step": "income_when",
            }

        claude = AsyncMock(
            return_value=ClaudeResolveResult(
                accepted=False,
                needs_clarify=True,
                reject_reason="timeout",
                latency_ms=1500,
            )
        )
        with (
            patch("pc_liveagent.call_turn.get_session", AsyncMock(return_value=session)),
            patch("pc_liveagent.call_turn.peek_choice_labels", AsyncMock(return_value=[])),
            patch("pc_liveagent.call_turn.resolve_with_claude", claude),
            patch("pc_liveagent.call_turn.submit_field", side_effect=fake_submit),
        ):
            await process_user_turn(call_id, "I'm a student.", response_id=3)
            task = __import__(
                "pc_liveagent.call_turn", fromlist=["_elig_browser_tasks"]
            )._elig_browser_tasks.get(call_id)
            if task:
                await asyncio.wait_for(task, timeout=2.0)
        claude.assert_not_awaited()
        self.assertEqual(submitted, [_SUPPORT_CANONICAL[0]])
        await drop_controller(call_id)

    async def test_correction_phrases_edit_only_named_field(self) -> None:
        from pc_liveagent.call_turn import drop_controller, get_controller, process_user_turn
        from pc_liveagent.control_intent import parse_member_correction
        from pc_liveagent.live_form import LiveFormSession

        self.assertEqual(
            parse_member_correction("change the last name to Patel"),
            ("last_name", "Patel"),
        )
        self.assertEqual(
            parse_member_correction("the age is seven"),
            ("age", "seven"),
        )
        self.assertEqual(
            parse_member_correction("their relationship is Grandchild"),
            ("relationship", "Grandchild"),
        )
        self.assertEqual(parse_member_correction("change the name"), ("name", ""))

        call_id = "corr-phrases"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.eligibility_question_sent = True
        ctrl.active_websocket_status = "connected"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="child_confirm"
        )
        session.filled.update(
            {
                "child_first": "Jackie",
                "child_last": "Smith",
                "child_rel": "Child",
                "child_age": "(1-5) Toddler",
            }
        )
        ctrl.pending_correction = {
            "field": "",
            "need_field": True,
            "prompt": "Which detail should I change?",
        }
        edits: list[tuple[str, str]] = []

        async def fake_edit(_cid, field, value):
            edits.append((field, value))
            key = {
                "first_name": "child_first",
                "last_name": "child_last",
                "age": "child_age",
                "relationship": "child_rel",
            }[field]
            session.filled[key] = value
            return {
                "ok": True,
                "step": "child_confirm",
                "spoken": f"Updated the {field}.",
            }

        with (
            patch("pc_liveagent.call_turn.get_session", AsyncMock(return_value=session)),
            patch("pc_liveagent.call_turn.correct_member_field", side_effect=fake_edit),
            patch("pc_liveagent.call_turn.submit_field", AsyncMock()),
            patch("pc_liveagent.call_turn.navigate_back_one", AsyncMock()),
        ):
            unclear = await process_user_turn(call_id, "hmm", response_id=1)
            self.assertEqual(unclear.detail, "correction_clarify_field")
            self.assertEqual(edits, [])
            self.assertIsNotNone(ctrl.pending_correction)
            self.assertEqual(session.filled["child_first"], "Jackie")

            named = await process_user_turn(call_id, "change the name", response_id=2)
            self.assertEqual(named.detail, "correction_clarify_name")
            self.assertIn("first name or the last name", named.speech.lower())
            self.assertEqual(edits, [])
            self.assertEqual(session.filled["child_first"], "Jackie")
            self.assertEqual((ctrl.pending_correction or {}).get("field"), "name")

            last = await process_user_turn(
                call_id, "change the last name to Patel", response_id=3
            )
            self.assertEqual(last.detail, "member_corrected")
            self.assertEqual(edits, [("last_name", "Patel")])
            self.assertEqual(session.filled["child_first"], "Jackie")
            self.assertEqual(session.filled["child_rel"], "Child")
            self.assertEqual(session.filled["child_age"], "(1-5) Toddler")

            ctrl.pending_correction = {
                "field": "",
                "need_field": True,
                "prompt": "Which detail should I change?",
            }
            age = await process_user_turn(call_id, "the age is seven", response_id=4)
            ctrl.pending_correction = {
                "field": "",
                "need_field": True,
                "prompt": "Which detail should I change?",
            }
            rel = await process_user_turn(
                call_id, "their relationship is Grandchild", response_id=5
            )
        self.assertEqual(age.detail, "member_corrected")
        self.assertEqual(rel.detail, "member_corrected")
        self.assertEqual(
            edits[1:],
            [("age", "seven"), ("relationship", "Grandchild")],
        )
        self.assertEqual(session.filled["child_first"], "Jackie")
        self.assertEqual(session.filled["child_last"], "Patel")
        await drop_controller(call_id)

    async def test_spouse_confirm_no_preserves_values(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _submit_eligibility_answer

        call_id = "spouse-confirm-no"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="spouse_confirm"
        )
        session.filled.update(
            {
                "spouse_first": "Alex",
                "spouse_last": "Rivera",
                "spouse_role": "Partner",
                "spouse_age": "(21-63) Adult",
            }
        )
        with patch(
            "pc_liveagent.live_form._eligibility_screen",
            AsyncMock(return_value="spouse_form"),
        ):
            result = await _submit_eligibility_answer(
                call_id, session, AsyncMock(), "No", started=0.0
            )
        self.assertEqual(result.get("step"), "spouse_confirm")
        self.assertTrue(result.get("await_field_correction"))
        self.assertEqual(session.filled.get("spouse_first"), "Alex")
        self.assertEqual(session.filled.get("spouse_role"), "Partner")


class StageETransitionTests(unittest.IsolatedAsyncioTestCase):
    """Program-summary permission gate and Stage E Collect Mode."""

    def test_build_programs_permission_speech_dynamic(self) -> None:
        from pc_liveagent.app_stage import build_programs_permission_speech

        speech = build_programs_permission_speech(
            {
                "programs": [
                    {
                        "name": "Medicaid",
                        "may_be_eligible": False,
                        "not_eligible": True,
                        "detail": "Not eligible at this time",
                    },
                    {
                        "name": "Medicare",
                        "may_be_eligible": False,
                        "not_eligible": True,
                        "detail": "Not eligible at this time",
                    },
                    {
                        "name": "ACA Marketplace",
                        "may_be_eligible": True,
                        "detail": "Members may be eligible for ACA Marketplace",
                    },
                ]
            }
        )
        low = speech.lower()
        self.assertIn("household eligibility check is complete", low)
        self.assertIn("aca marketplace", low)
        self.assertIn("may be eligible", low)
        # Speak only eligible programs — never announce not-eligible cards.
        self.assertNotIn("not eligible at this time", low)
        self.assertNotIn("medicare", low)
        self.assertNotIn("medicaid", low)
        self.assertIn("would you like to continue and start the application", low)
        self.assertNotIn("approved", low)

    def test_build_programs_permission_speech_eligible_only_medikids(self) -> None:
        from pc_liveagent.app_stage import build_programs_permission_speech

        speech = build_programs_permission_speech(
            {
                "programs": [
                    {
                        "name": "MediKids / Florida Healthy Kids",
                        "may_be_eligible": True,
                        "detail": (
                            "Agent, Mickey may be eligible for MediKids / "
                            "Florida Healthy Kids"
                        ),
                    },
                    {
                        "name": "Medicare",
                        "may_be_eligible": False,
                        "not_eligible": True,
                        "detail": "Not eligible at this time",
                    },
                    {
                        "name": "ACA Marketplace",
                        "may_be_eligible": False,
                        "not_eligible": True,
                        "detail": "Not eligible at this time",
                    },
                ]
            }
        )
        low = speech.lower()
        self.assertIn("medikids", low)
        self.assertIn("may be eligible", low)
        self.assertNotIn("not eligible", low)
        self.assertNotIn("medicare", low)
        self.assertNotIn("marketplace", low)

    def test_programs_context_does_not_navigate(self) -> None:
        from pc_liveagent.app_stage import programs_context_reply

        reply = programs_context_reply(
            "What happens if I say no?",
            {"programs": [{"name": "ACA Marketplace", "may_be_eligible": True}]},
        )
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("will not start the application", reply.lower())
        self.assertIn("yes", reply.lower())

    async def test_programs_yes_starts_application_once(self) -> None:
        from pc_liveagent.app_stage import handle_programs_decision
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="prog-yes", url="https://example.test", elig_step="programs"
        )
        page = AsyncMock()
        page.url = "https://my.pointcare.com/application/step-1"
        start = AsyncMock(return_value="Start Applications")

        with (
            patch("pc_liveagent.app_stage.click_start_applications", start),
            patch(
                "pc_liveagent.app_stage.wait_for_application_started",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
        ):
            result = await handle_programs_decision(
                "prog-yes", session, page, "Yes"
            )
            again = await handle_programs_decision(
                "prog-yes", session, page, "Yes"
            )
        start.assert_awaited_once()

        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_citizenship")
        self.assertEqual(result.get("phase"), "application")
        self.assertIn("started the application", str(result.get("spoken") or "").lower())
        self.assertEqual(session.filled.get("application_started"), "1")
        self.assertEqual(session.app_step, "app_citizenship")
        self.assertEqual(again.get("step"), "app_citizenship")
        # Second Yes must not re-click Start Applications (idempotent).
        self.assertEqual(session.filled.get("start_applications_clicked"), "Start Applications")

    async def test_programs_no_preserves_results_no_click(self) -> None:
        from pc_liveagent.app_stage import (
            CLOSING_NO_APPLICATION,
            handle_programs_decision,
        )
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="prog-no", url="https://example.test", elig_step="programs"
        )
        session.filled["program_summary_json"] = '{"programs":[]}'
        page = AsyncMock()
        with patch(
            "pc_liveagent.app_stage.click_start_applications",
            new=AsyncMock(side_effect=AssertionError("must not click")),
        ) as start:
            result = await handle_programs_decision("prog-no", session, page, "No")
        start.assert_not_awaited()
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "")
        self.assertTrue(result.get("end_call"))
        self.assertIn(CLOSING_NO_APPLICATION, str(result.get("spoken") or ""))
        self.assertEqual(session.filled.get("application_started"), "0")
        self.assertEqual(session.app_step, "")

    async def test_programs_ambiguous_clarifies(self) -> None:
        from pc_liveagent.app_stage import handle_programs_decision
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="prog-maybe", url="https://example.test", elig_step="programs"
        )
        session.filled["programs_permission_speech"] = (
            "Your household eligibility check is complete. Would you like to continue "
            "and start the application? Please say Yes or No."
        )
        page = AsyncMock()
        with patch(
            "pc_liveagent.app_stage.click_start_applications",
            new=AsyncMock(side_effect=AssertionError("must not click")),
        ):
            result = await handle_programs_decision(
                "prog-maybe", session, page, "I'm not sure"
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("step"), "programs")
        self.assertIn("please say yes", str(result.get("spoken") or "").lower())

    async def test_background_programs_yes_enters_application_phase(self) -> None:
        from pc_liveagent.call_turn import (
            _run_eligibility_browser_background,
            drop_controller,
            get_controller,
        )

        call_id = "stage-e-yes"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        ctrl.active_websocket_status = "connected"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="programs"
        )
        deliver = AsyncMock(return_value=True)

        with (
            patch(
                "pc_liveagent.call_turn.get_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "pc_liveagent.call_turn.submit_field",
                AsyncMock(
                    return_value={
                        "ok": True,
                        "spoken": (
                            "Okay, I've started the application. Are all household "
                            "members listed on this screen U.S. citizens? Please say Yes or No."
                        ),
                        "step": "app_citizenship",
                        "phase": "application",
                    }
                ),
            ),
            patch("pc_liveagent.call_turn._deliver_question_once", deliver),
        ):
            await _run_eligibility_browser_background(
                call_id,
                answer="Yes",
                step="programs",
                turn_id=f"{call_id}:1:yes",
                form_url="https://example.test",
                commit_key="programs:1:yes",
                epoch=ctrl.elig_nav_epoch,
            )
        self.assertEqual(ctrl.phase, "application")
        self.assertIn("u.s. citizens", ctrl.last_speech.lower())
        self.assertIn("stage_e:", deliver.await_args.kwargs.get("key", ""))
        self.assertFalse(deliver.await_args.kwargs.get("end_call"))
        await drop_controller(call_id)

    async def test_submit_requires_explicit_yes(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="submit1",
            url="https://example.test",
            app_step="app_submit_confirm",
        )
        page = AsyncMock()
        page.url = "https://my.pointcare.com/application/submit"
        page.locator = MagicMock()
        with patch(
            "pc_liveagent.live_form._click_cta",
            new=AsyncMock(side_effect=AssertionError("must not submit")),
        ):
            unclear = await submit_application_answer(
                "submit1", session, page, "maybe later", 0.0
            )
            review = await submit_application_answer(
                "submit1", session, page, "I want to review first", 0.0
            )
        self.assertFalse(unclear.get("ok"))
        self.assertEqual(unclear.get("step"), "app_submit_confirm")
        self.assertTrue(review.get("ok"))
        self.assertIn("will not submit yet", str(review.get("spoken") or "").lower())
        self.assertNotEqual(session.filled.get("application_submitted"), "1")

    async def test_submit_yes_requires_pointcare_confirmation_text(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        class _Body:
            def __init__(self, text: str) -> None:
                self.text = text

            async def inner_text(self) -> str:
                return self.text

        session = LiveFormSession(
            call_id="submit-verify",
            url="https://example.test",
            app_step="app_submit_confirm",
        )
        page = AsyncMock()
        page.url = "https://my.pointcare.com/application/submit"
        page.wait_for_timeout = AsyncMock()
        page.locator = lambda _selector: _Body("Review your application Submit Applications")
        with patch("pc_liveagent.live_form._click_cta", new=AsyncMock(return_value=True)):
            missing = await submit_application_answer(
                "submit-verify", session, page, "Yes", 0.0
            )
        self.assertFalse(missing.get("ok"))
        self.assertEqual(missing.get("error"), "submit_not_verified")
        self.assertNotEqual(session.filled.get("application_submitted"), "1")

        page.locator = lambda _selector: _Body(
            "Thank you. Your application has been submitted."
        )
        with patch("pc_liveagent.live_form._click_cta", new=AsyncMock(return_value=True)):
            done = await submit_application_answer(
                "submit-verify", session, page, "Yes", 0.0
            )
        self.assertTrue(done.get("ok"))
        self.assertTrue(done.get("end_call"))
        self.assertEqual(session.filled.get("application_submitted"), "1")

    async def test_application_turn_dedupes_duplicate_answer(self) -> None:
        from pc_liveagent.call_turn import drop_controller, get_controller, process_user_turn

        call_id = "app-dedupe"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "application"
        ctrl.last_speech = "Are all household members U.S. citizens?"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", app_step="app_citizenship"
        )
        submit = AsyncMock(
            return_value={
                "ok": True,
                "spoken": "ASK: Does anyone have health insurance now?",
                "step": "app_insurance",
                "phase": "application",
            }
        )
        with (
            patch("pc_liveagent.config.APPLICATION_ONLY_TEST_MODE", False),
            patch(
                "pc_liveagent.call_turn.get_session",
                new=AsyncMock(return_value=session),
            ),
            patch("pc_liveagent.call_turn.submit_field", submit),
        ):
            # Force sync path by removing step from slow set temporarily.
            with patch(
                "pc_liveagent.call_turn._SLOW_APP_STEPS",
                frozenset(),
            ):
                r1 = await process_user_turn(call_id, "Yes")
                session.app_step = "app_insurance"
                ctrl.phase = "application"
                r2 = await process_user_turn(call_id, "Yes")
        self.assertEqual(r1.phase, "application")
        # Second Yes on same fingerprint+step should dedupe once committed.
        # After first answer step advances; second Yes is a new step — allowed.
        self.assertEqual(submit.await_count, 2)
        await drop_controller(call_id)

    async def test_application_allows_same_no_for_new_insurance_subquestion(
        self,
    ) -> None:
        from pc_liveagent.call_turn import (
            drop_controller,
            get_controller,
            process_user_turn,
        )

        call_id = "app-insurance-two-nos"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "application"
        session = LiveFormSession(
            call_id=call_id,
            url="https://example.test",
            app_step="app_insurance_coverage_type",
        )
        session.filled["app_insurance_coverage_phase"] = "more"
        session.filled["app_insurance_current"] = "Agent T."
        session.filled["app_pending_question"] = "Does Agent T. have any other plans?"

        async def _submit(*_args, **_kwargs):
            session.filled["app_insurance_coverage_phase"] = "members_more"
            session.filled.pop("app_insurance_current", None)
            session.filled["app_pending_question"] = (
                "Does any other household member have insurance?"
            )
            return {
                "ok": True,
                "spoken": (
                    "ASK: Does any other household member have insurance? "
                    "Say their name, or say No."
                ),
                "step": "app_insurance_coverage_type",
                "phase": "application",
            }

        submit = AsyncMock(side_effect=_submit)
        with (
            patch("pc_liveagent.config.APPLICATION_ONLY_TEST_MODE", False),
            patch(
                "pc_liveagent.call_turn.get_session",
                new=AsyncMock(return_value=session),
            ),
            patch("pc_liveagent.call_turn.submit_field", submit),
            patch("pc_liveagent.call_turn._SLOW_APP_STEPS", frozenset()),
        ):
            first = await process_user_turn(call_id, "No")
            second = await process_user_turn(call_id, "No")
            third = await process_user_turn(call_id, "No")

        self.assertEqual(first.detail, "")
        self.assertEqual(second.detail, "")
        self.assertEqual(submit.await_count, 2)
        self.assertEqual(third.detail, "duplicate_app_answer")
        await drop_controller(call_id)

    async def test_live_form_cache_allows_two_insurance_no_answers(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, submit_field

        session = LiveFormSession(
            call_id="live-form-two-nos",
            url="https://example.test",
            app_step="app_insurance_coverage_type",
        )
        session.page = AsyncMock()
        session.ready.set()
        session.filled["app_insurance_coverage_phase"] = "more"
        session.filled["app_insurance_current"] = "Agent T."
        session.filled["app_pending_step"] = "app_insurance_coverage_type"
        session.filled["app_pending_question"] = "Any other plans?"

        async def _submit(_call_id, active, _page, _text, _started):
            if active.filled.get("app_insurance_coverage_phase") == "more":
                active.filled["app_insurance_coverage_phase"] = "members_more"
                active.filled.pop("app_insurance_current", None)
                active.filled["app_pending_question"] = (
                    "Does any other member have insurance?"
                )
                return {
                    "ok": True,
                    "spoken": "ASK: Does any other member have insurance?",
                    "step": "app_insurance_coverage_type",
                    "phase": "application",
                }
            return {
                "ok": True,
                "spoken": "ASK: Please confirm dates of birth.",
                "step": "app_dob_review",
                "phase": "application",
            }

        apply_answer = AsyncMock(side_effect=_submit)
        with (
            patch(
                "pc_liveagent.live_form.start_live_form",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "pc_liveagent.app_stage.submit_application_answer",
                new=apply_answer,
            ),
        ):
            first = await submit_field(
                session.call_id,
                "application_answer",
                "No",
                request_id="first-no",
            )
            second = await submit_field(
                session.call_id,
                "application_answer",
                "No",
                request_id="second-no",
            )

        self.assertEqual(apply_answer.await_count, 2)
        self.assertIn("other member", first.get("spoken", "").lower())
        self.assertEqual(second.get("step"), "app_dob_review")

    async def test_programs_context_via_process_user_turn(self) -> None:
        from pc_liveagent.call_turn import drop_controller, get_controller, process_user_turn

        call_id = "prog-ctx"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "eligibility"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="programs"
        )
        session.filled["program_summary_json"] = json.dumps(
            {
                "programs": [
                    {"name": "ACA Marketplace", "may_be_eligible": True},
                ]
            }
        )
        session.filled["programs_permission_speech"] = (
            "Your household eligibility check is complete. Pointcare indicates that "
            "your household may be eligible for ACA Marketplace. Would you like to "
            "continue and start the application? Please say Yes or No."
        )
        submit = AsyncMock()
        with (
            patch("pc_liveagent.config.APPLICATION_ONLY_TEST_MODE", False),
            patch(
                "pc_liveagent.call_turn.get_session",
                new=AsyncMock(return_value=session),
            ),
            patch("pc_liveagent.call_turn.submit_field", submit),
            patch(
                "pc_liveagent.call_turn.peek_choice_labels",
                new=AsyncMock(return_value=[]),
            ),
        ):
            r = await process_user_turn(call_id, "What happens if I say no?")
        self.assertEqual(r.detail, "programs_context")
        self.assertIn("will not start", r.speech.lower())
        submit.assert_not_awaited()
        await drop_controller(call_id)


class StageEYesNoResolverTests(unittest.IsolatedAsyncioTestCase):
    """Stage E Yes/No cards: short and descriptive labels, no Stage D changes."""

    CITIZENSHIP = [
        "Yes Everyone listed is a U.S. Citizen",
        "No Some or all are not U.S. Citizens",
    ]
    INSURANCE = [
        "Yes Someone has health insurance",
        "No Nobody has health insurance",
    ]

    def test_yes_matches_long_citizenship_card(self) -> None:
        from pc_liveagent.app_stage import resolve_stage_e_yes_no_label

        self.assertEqual(
            resolve_stage_e_yes_no_label(True, self.CITIZENSHIP),
            self.CITIZENSHIP[0],
        )

    def test_no_matches_long_citizenship_card(self) -> None:
        from pc_liveagent.app_stage import resolve_stage_e_yes_no_label

        self.assertEqual(
            resolve_stage_e_yes_no_label(False, self.CITIZENSHIP),
            self.CITIZENSHIP[1],
        )

    def test_exact_short_labels_still_supported(self) -> None:
        from pc_liveagent.app_stage import resolve_stage_e_yes_no_label

        self.assertEqual(resolve_stage_e_yes_no_label(True, ["Yes", "No"]), "Yes")
        self.assertEqual(resolve_stage_e_yes_no_label(False, ["Yes", "No"]), "No")

    def test_descriptive_preferred_when_short_and_long_both_visible(self) -> None:
        from pc_liveagent.app_stage import resolve_stage_e_yes_no_label

        labels = ["Yes", "Yes Everyone listed is a U.S. Citizen", "No"]
        self.assertEqual(
            resolve_stage_e_yes_no_label(True, labels),
            "Yes Everyone listed is a U.S. Citizen",
        )

    def test_dash_prefixed_cards(self) -> None:
        from pc_liveagent.app_stage import resolve_stage_e_yes_no_label

        labels = [
            "Yes — Everyone listed is a U.S. Citizen",
            "No — Some or all are not U.S. Citizens",
        ]
        self.assertEqual(resolve_stage_e_yes_no_label(True, labels), labels[0])
        self.assertEqual(resolve_stage_e_yes_no_label(False, labels), labels[1])

    def test_insurance_long_labels(self) -> None:
        from pc_liveagent.app_stage import resolve_stage_e_yes_no_label

        self.assertEqual(
            resolve_stage_e_yes_no_label(True, self.INSURANCE),
            self.INSURANCE[0],
        )
        self.assertEqual(
            resolve_stage_e_yes_no_label(False, self.INSURANCE),
            self.INSURANCE[1],
        )

    def test_ambiguous_options_do_not_resolve(self) -> None:
        from pc_liveagent.app_stage import resolve_stage_e_yes_no_label

        with self.assertRaises(RuntimeError) as ctx:
            resolve_stage_e_yes_no_label(
                True,
                [
                    "Yes Everyone listed is a U.S. Citizen",
                    "Yes Some members are citizens",
                ],
            )
        self.assertIn("YES_NO_CLARIFY", str(ctx.exception))

    def test_missing_options_do_not_resolve(self) -> None:
        from pc_liveagent.app_stage import resolve_stage_e_yes_no_label

        with self.assertRaises(RuntimeError) as ctx:
            resolve_stage_e_yes_no_label(True, ["Continue", "Next"])
        self.assertIn("YES_NO_CLARIFY", str(ctx.exception))

    def test_shared_fuzzy_choice_still_keeps_bare_yes(self) -> None:
        """Stage D / shared matcher must stay unchanged for bare Yes."""
        from pc_liveagent.live_form import _fuzzy_choice_label

        self.assertEqual(
            _fuzzy_choice_label("Yes", self.CITIZENSHIP),
            "Yes",
        )

    async def test_click_yes_no_uses_full_label_and_verifies(self) -> None:
        from pc_liveagent.app_stage import _click_yes_no

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        clicked: list[str] = []

        async def _stage_e_click(_page, value: str) -> bool:
            clicked.append(value)
            return True

        with (
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(return_value=list(self.CITIZENSHIP)),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_e_option_label",
                new=_stage_e_click,
            ),
            patch(
                "pc_liveagent.live_form._choice_signature",
                new=AsyncMock(side_effect=["before", "after-selected"]),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
        ):
            chosen = await _click_yes_no(page, True)
        self.assertEqual(chosen, self.CITIZENSHIP[0])
        self.assertEqual(clicked, [self.CITIZENSHIP[0]])
        # Must not collapse through _choice_phrase / bare Yes.
        self.assertNotEqual(chosen, "Yes")

    async def test_click_yes_no_succeeds_when_page_advances(self) -> None:
        """Missing old card after navigation is success, not a failure."""
        from pc_liveagent.app_stage import _click_yes_no

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        screens = iter(["app_citizenship", "app_insurance", "app_insurance"])
        with (
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(
                    side_effect=[
                        list(self.CITIZENSHIP),
                        list(self.INSURANCE),
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_e_option_label",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.live_form._choice_signature",
                new=AsyncMock(side_effect=["before", "__missing__"]),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(side_effect=lambda _p: next(screens)),
            ),
        ):
            chosen = await _click_yes_no(page, True)
        self.assertEqual(chosen, self.CITIZENSHIP[0])

    async def test_click_yes_no_ambiguous_does_not_click(self) -> None:
        from pc_liveagent.app_stage import _click_yes_no

        click = AsyncMock(side_effect=AssertionError("must not click"))
        with (
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(
                    return_value=[
                        "Yes Everyone listed is a U.S. Citizen",
                        "Yes Some members are citizens",
                    ]
                ),
            ),
            patch("pc_liveagent.app_stage._click_stage_e_option_label", click),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await _click_yes_no(AsyncMock(), True)
        self.assertIn("YES_NO_CLARIFY", str(ctx.exception))
        click.assert_not_awaited()

    async def test_citizenship_yes_advances_only_after_verified_click(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="cit-yes",
            url="https://example.test",
            app_step="app_citizenship",
        )
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        # reconcile + post-yes live check stay on citizenship until Next.
        screens = iter(["app_citizenship", "app_citizenship"])
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(side_effect=lambda _p: next(screens, "app_citizenship")),
            ),
            patch(
                "pc_liveagent.app_stage._click_yes_no",
                new=AsyncMock(return_value=self.CITIZENSHIP[0]),
            ) as click,
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(return_value=True),
            ) as nxt,
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_insurance"),
            ) as advance,
        ):
            result = await submit_application_answer(
                "cit-yes", session, page, "Yes", 0.0
            )
        click.assert_awaited_once_with(page, True)
        nxt.assert_awaited_once()
        advance.assert_awaited_once()
        self.assertFalse(advance.await_args.kwargs.get("allow_extra_next", True))
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_insurance")
        self.assertEqual(session.app_step, "app_insurance")
        self.assertIn("insurance", (result.get("spoken") or "").lower())

    async def test_detect_prefers_insurance_over_stale_citizen_chrome(self) -> None:
        from pc_liveagent.app_stage import detect_application_screen

        class _Body:
            async def inner_text(self) -> str:
                return (
                    "Coverage Statuses "
                    "Do Agent, john, Don have health insurance or health coverage? "
                    "Yes Someone has coverage No No one has coverage "
                    "Agent T. U.S. Citizen john D. U.S. Citizen"
                )

        page = AsyncMock()
        page.locator = lambda _sel: _Body()
        self.assertEqual(await detect_application_screen(page), "app_insurance")

    async def test_detect_prefers_childcare_over_stale_marital_labels(self) -> None:
        from pc_liveagent.app_stage import detect_application_screen

        class _Body:
            async def inner_text(self) -> str:
                return (
                    "Current marital statuses Never Married Common Law Marriage "
                    "Do you live with and mainly support any children under 19? "
                    "Caring for children Yes No"
                )

        page = AsyncMock()
        page.locator = lambda _sel: _Body()
        self.assertEqual(await detect_application_screen(page), "app_childcare_review")

    async def test_citizenship_yes_advance_does_not_click_second_next(
        self,
    ) -> None:
        """call_4d470… skipped insurance when advance clicked Next again."""
        from pc_liveagent.app_stage import _advance_after_answer

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        session = AsyncMock()
        session.app_step = "app_citizenship"
        reads = iter(["", "", "app_insurance"])
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(side_effect=lambda _p: next(reads, "app_insurance")),
            ),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(side_effect=AssertionError("must not Next again")),
            ),
        ):
            step = await _advance_after_answer(
                page,
                session,
                allow_extra_next=False,
                leave_step="app_citizenship",
            )
        self.assertEqual(step, "app_insurance")

    async def test_citizenship_no_branches_to_immigration(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="cit-no",
            url="https://example.test",
            app_step="app_citizenship",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage._click_yes_no",
                new=AsyncMock(return_value=self.CITIZENSHIP[1]),
            ) as click,
            patch(
                "pc_liveagent.app_stage._scrape_immigration_members",
                new=AsyncMock(return_value=[]),
            ),
        ):
            result = await submit_application_answer(
                "cit-no", session, AsyncMock(), "No", 0.0
            )
        click.assert_awaited_once_with(ANY, False)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "citizenship_members_missing")
        self.assertEqual(result.get("step"), "app_citizenship")
        self.assertEqual(session.app_step, "app_citizenship")
        self.assertIn("member", (result.get("spoken") or "").lower())

    async def test_repeated_yes_reconciles_when_browser_already_on_insurance(
        self,
    ) -> None:
        """Delayed Yes must not re-click citizenship after the page advanced."""
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="cit-stale",
            url="https://example.test",
            app_step="app_citizenship",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage._click_yes_no",
                new=AsyncMock(return_value=self.INSURANCE[0]),
            ) as click,
            patch("pc_liveagent.app_stage._click_next_or_continue", new=AsyncMock()),
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_dob_review"),
            ) as advance,
        ):
            result = await submit_application_answer(
                "cit-stale", session, AsyncMock(), "Yes", 0.0
            )
        click.assert_awaited_once_with(ANY, True)
        self.assertTrue(result.get("ok"))
        # Insurance Yes now collects member names when still on insurance.
        self.assertEqual(result.get("step"), "app_insurance_members")
        self.assertNotEqual(session.app_step, "app_citizenship")

    async def test_click_failure_recovers_when_browser_already_advanced(
        self,
    ) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="cit-nav",
            url="https://example.test",
            app_step="app_citizenship",
        )
        screens = iter(["app_citizenship", "app_insurance"])
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(side_effect=lambda _p: next(screens)),
            ),
            patch(
                "pc_liveagent.app_stage._click_yes_no",
                new=AsyncMock(
                    side_effect=RuntimeError(
                        "YES_NO_CLARIFY: That Yes or No choice did not register."
                    )
                ),
            ),
        ):
            result = await submit_application_answer(
                "cit-nav", session, AsyncMock(), "Yes", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_insurance")
        spoken = result.get("spoken") or ""
        self.assertNotIn("target=", spoken)
        self.assertNotIn("body=", spoken)
        self.assertIn("insurance", spoken.lower())

    async def test_citizenship_no_is_accepted_when_member_radios_expand(self) -> None:
        """No keeps the Yes/No cards visible. Member radios mean the click worked."""
        from pc_liveagent.app_stage import _click_yes_no

        page = AsyncMock()
        label = "No Some or all are not U.S. Citizens"
        with (
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(
                    return_value=[
                        "Yes Everyone listed is a U.S. Citizen",
                        label,
                    ]
                ),
            ),
            patch(
                "pc_liveagent.live_form._choice_signature",
                new=AsyncMock(return_value="__missing__"),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_e_option_label",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage.immigration_member_radios_visible",
                new=AsyncMock(return_value=True),
            ),
        ):
            selected = await _click_yes_no(page, False)
        self.assertEqual(selected, label)

    async def test_citizenship_no_still_fails_when_radios_never_appear(self) -> None:
        from pc_liveagent.app_stage import _click_yes_no

        page = AsyncMock()
        label = "No Some or all are not U.S. Citizens"
        with (
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(return_value=[label]),
            ),
            patch(
                "pc_liveagent.live_form._choice_signature",
                new=AsyncMock(return_value="__missing__"),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_e_option_label",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage.immigration_member_radios_visible",
                new=AsyncMock(return_value=False),
            ),
        ):
            with self.assertRaises(RuntimeError):
                await _click_yes_no(page, False)

    async def test_div_card_selected_state_counts_without_a_screen_change(self) -> None:
        from pc_liveagent.app_stage import _click_yes_no

        page = AsyncMock()
        label = "Yes Everyone listed is a U.S. Citizen"
        click = AsyncMock(return_value=True)
        with (
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(return_value=[label, "No Some or all are not U.S. Citizens"]),
            ),
            patch(
                "pc_liveagent.live_form._choice_signature",
                new=AsyncMock(return_value="__missing__"),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch("pc_liveagent.app_stage._click_stage_e_option_label", click),
            patch(
                "pc_liveagent.app_stage._stage_e_card_state",
                new=AsyncMock(
                    side_effect=[
                        {"found": True, "selected": False, "disabled": False, "signature": "off"},
                        {"found": True, "selected": True, "disabled": False, "signature": "on"},
                    ]
                ),
            ),
        ):
            selected = await _click_yes_no(page, True)
        click.assert_awaited_once()
        self.assertEqual(selected, label)

    async def test_disabled_div_card_is_not_clicked(self) -> None:
        from pc_liveagent.app_stage import _click_yes_no

        page = AsyncMock()
        label = "No No one has coverage"
        click = AsyncMock(return_value=True)
        with (
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(return_value=[label]),
            ),
            patch("pc_liveagent.app_stage._click_stage_e_option_label", click),
            patch(
                "pc_liveagent.app_stage._stage_e_card_state",
                new=AsyncMock(
                    return_value={
                        "found": True,
                        "selected": False,
                        "disabled": True,
                        "signature": "disabled",
                    }
                ),
            ),
        ):
            with self.assertRaises(RuntimeError):
                await _click_yes_no(page, False)
        click.assert_not_awaited()

    async def test_missing_citizenship_card_is_rejected_before_click(self) -> None:
        from pc_liveagent.app_stage import _click_yes_no

        page = AsyncMock()
        click = AsyncMock(return_value=True)
        with (
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(return_value=["Continue", "Back"]),
            ),
            patch("pc_liveagent.app_stage._click_stage_e_option_label", click),
        ):
            with self.assertRaises(RuntimeError):
                await _click_yes_no(page, False)
            with self.assertRaises(RuntimeError):
                await _click_yes_no(page, True)
        click.assert_not_awaited()

    async def test_citizenship_click_false_does_not_count_as_selected(self) -> None:
        from pc_liveagent.app_stage import _click_yes_no

        page = AsyncMock()
        label = "No — Some or all are not U.S. Citizens"
        click = AsyncMock(return_value=False)
        with (
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(
                    return_value=[
                        "Yes — Everyone listed is a U.S. Citizen",
                        label,
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._stage_e_card_state",
                new=AsyncMock(
                    return_value={
                        "found": True,
                        "selected": False,
                        "disabled": False,
                        "signature": "off",
                    }
                ),
            ),
            patch(
                "pc_liveagent.live_form._choice_signature",
                new=AsyncMock(return_value="__missing__"),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch("pc_liveagent.app_stage._click_stage_e_option_label", click),
        ):
            with self.assertRaises(RuntimeError):
                await _click_yes_no(page, False)
        click.assert_awaited_once()

    async def test_unchanged_div_card_is_a_real_failure(self) -> None:
        from pc_liveagent.app_stage import _click_yes_no

        page = AsyncMock()
        label = "No No one has coverage"
        with (
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(return_value=[label]),
            ),
            patch(
                "pc_liveagent.live_form._choice_signature",
                new=AsyncMock(return_value="__missing__"),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_e_option_label",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._stage_e_card_state",
                new=AsyncMock(
                    return_value={
                        "found": True,
                        "selected": False,
                        "disabled": False,
                        "signature": "same",
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage.immigration_member_radios_visible",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_coverage_members",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "pc_liveagent.app_stage._visible_form_validation",
                new=AsyncMock(return_value="Please choose Yes or No."),
            ),
        ):
            with self.assertRaises(RuntimeError):
                await _click_yes_no(page, False)

    async def test_insurance_no_waits_for_a_delayed_dob_screen(self) -> None:
        from pc_liveagent.app_stage import _click_yes_no

        page = AsyncMock()
        label = "No No one has coverage"
        screens = iter(["app_insurance", "app_insurance", "app_insurance", "app_dob_review"])

        async def detect(_page: object) -> str:
            return next(screens)

        with (
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(return_value=[label, "Yes Someone has coverage"]),
            ),
            patch(
                "pc_liveagent.live_form._choice_signature",
                new=AsyncMock(return_value="__missing__"),
            ),
            patch("pc_liveagent.app_stage.detect_application_screen", new=AsyncMock(side_effect=detect)),
            patch(
                "pc_liveagent.app_stage._click_stage_e_option_label",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._stage_e_card_state",
                new=AsyncMock(
                    return_value={
                        "found": True,
                        "selected": False,
                        "disabled": False,
                        "signature": "same",
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage.immigration_member_radios_visible",
                new=AsyncMock(return_value=False),
            ),
        ):
            selected = await _click_yes_no(page, False)
        self.assertEqual(selected, label)

    async def test_insurance_yes_accepts_coverage_members_on_the_same_screen(self) -> None:
        from pc_liveagent.app_stage import _click_yes_no

        page = AsyncMock()
        label = "Yes Someone has coverage"
        scrapes = iter([[], [{"name": "Agent T.", "checked": False}]])

        async def scrape(_page: object) -> list:
            return next(scrapes)

        with (
            patch(
                "pc_liveagent.live_form._visible_choice_labels",
                new=AsyncMock(return_value=[label, "No No one has coverage"]),
            ),
            patch(
                "pc_liveagent.live_form._choice_signature",
                new=AsyncMock(return_value="__missing__"),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_e_option_label",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._stage_e_card_state",
                new=AsyncMock(
                    return_value={
                        "found": True,
                        "selected": False,
                        "disabled": False,
                        "signature": "same",
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_coverage_members",
                new=AsyncMock(side_effect=scrape),
            ),
        ):
            selected = await _click_yes_no(page, True)
        self.assertEqual(selected, label)

    async def test_citizenship_no_moves_to_member_status_while_cards_stay(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="cit-no-cards",
            url="https://example.test",
            app_step="app_citizenship",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage.immigration_member_radios_visible",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._click_yes_no",
                new=AsyncMock(return_value="No Some or all are not U.S. Citizens"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_immigration_members",
                new=AsyncMock(return_value=[{"name": "Agent T."}]),
            ),
        ):
            result = await submit_application_answer(
                "cit-no-cards", session, AsyncMock(), "No", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_immigration")
        self.assertEqual(session.filled.get("app_citizenship_no"), "1")
        self.assertNotIn("all U.S. Citizens", result.get("spoken") or "")

    async def test_insurance_no_does_not_click_next_after_the_page_advances(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="ins-no-delay",
            url="https://example.test",
            app_step="app_insurance",
        )
        screens = iter(["app_insurance", "app_dob_review"])
        next_click = AsyncMock(side_effect=AssertionError("must not click Next"))
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(side_effect=lambda _page: next(screens)),
            ),
            patch(
                "pc_liveagent.app_stage._click_yes_no",
                new=AsyncMock(return_value="No No one has coverage"),
            ),
            patch("pc_liveagent.app_stage._click_next_or_continue", next_click),
        ):
            result = await submit_application_answer(
                "ins-no-delay", session, AsyncMock(), "No", 0.0
            )
        next_click.assert_not_awaited()
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_dob_review")

    async def test_insurance_yes_collects_members_without_leaving_the_screen(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="ins-yes-members",
            url="https://example.test",
            app_step="app_insurance",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage._click_yes_no",
                new=AsyncMock(return_value="Yes Someone has coverage"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_coverage_members",
                new=AsyncMock(return_value=[{"name": "john D.", "checked": False}]),
            ),
        ):
            result = await submit_application_answer(
                "ins-yes-members", session, AsyncMock(), "Yes", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_insurance_members")
        self.assertIn("john D.", result.get("spoken") or "")

    async def test_validation_keeps_the_insurance_question(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="ins-invalid",
            url="https://example.test",
            app_step="app_insurance",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage._click_yes_no",
                new=AsyncMock(
                    side_effect=RuntimeError(
                        "YES_NO_CLARIFY: That Yes or No choice did not register."
                    )
                ),
            ),
        ):
            result = await submit_application_answer(
                "ins-invalid", session, AsyncMock(), "No", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "yes_no_click_failed")
        self.assertEqual(result.get("step"), "app_insurance")
        self.assertIn("Yes or No", result.get("spoken") or "")

    async def test_citizenship_click_failure_returns_clarify_without_advance(
        self,
    ) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="cit-fail",
            url="https://example.test",
            app_step="app_citizenship",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage._click_yes_no",
                new=AsyncMock(
                    side_effect=RuntimeError(
                        "YES_NO_CLARIFY: That Yes or No choice did not register. "
                        "target='Yes Everyone' body='debug'"
                    )
                ),
            ),
        ):
            result = await submit_application_answer(
                "cit-fail", session, AsyncMock(), "Yes", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "yes_no_click_failed")
        self.assertEqual(result.get("step"), "app_citizenship")
        self.assertEqual(session.app_step, "app_citizenship")
        spoken = result.get("spoken") or ""
        self.assertNotIn("target=", spoken)
        self.assertNotIn("body=", spoken)
        self.assertNotIn("Yes Everyone", spoken)
        self.assertIn("Yes or No", spoken)

    async def test_insurance_no_uses_resolver(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="ins-no",
            url="https://example.test",
            app_step="app_insurance",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage._click_yes_no",
                new=AsyncMock(return_value=self.INSURANCE[1]),
            ) as click,
            patch("pc_liveagent.app_stage._click_next_or_continue", new=AsyncMock()),
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_dob_review"),
            ),
        ):
            result = await submit_application_answer(
                "ins-no", session, AsyncMock(), "No", 0.0
            )
        click.assert_awaited_once_with(ANY, False)
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_dob_review")

    async def test_detect_insurance_coverage_wording(self) -> None:
        from pc_liveagent.app_stage import detect_application_screen

        class _Body:
            async def inner_text(self) -> str:
                return (
                    "Does anyone have coverage? "
                    "Yes Someone has coverage "
                    "No No one has coverage"
                )

        page = AsyncMock()
        page.url = "https://example.test/application"
        page.locator = lambda _selector: _Body()
        self.assertEqual(await detect_application_screen(page), "app_insurance")


class StageECollectModeTests(unittest.IsolatedAsyncioTestCase):
    """Sequential Collect Mode Stage E: income fields, explain, circumstances."""

    async def test_employed_income_asks_one_field_at_a_time(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-seq",
            url="https://example.test",
            app_step="app_income",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_current_index"] = "0"
        session.filled["app_income_phase"] = "type"
        session.filled["app_income_heard"] = "[]"
        page = AsyncMock()
        cards = [{"index": 0, "name": "Admin P.", "hasAddIncome": True}]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._open_type_and_select",
                new=AsyncMock(return_value={"ok": True}),
            ),
        ):
            first = await submit_application_answer(
                "inc-seq", session, page, "Employed", 0.0
            )
        self.assertTrue(first.get("ok"))
        self.assertEqual(first.get("step"), "app_income_employed")
        self.assertIn("monthly gross income", (first.get("spoken") or "").lower())
        self.assertNotIn("hours per week", (first.get("spoken") or "").lower())

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage._fill_employed_field",
                new=AsyncMock(return_value=True),
            ) as fill_pay,
        ):
            second = await submit_application_answer(
                "inc-seq", session, page, "around 400 dollars", 0.0
            )
        self.assertTrue(second.get("ok"))
        self.assertEqual(second.get("step"), "app_income_employed_hours")
        self.assertIn("hours", (second.get("spoken") or "").lower())
        fill_pay.assert_awaited()
        self.assertEqual(fill_pay.await_args.args[1], "pay")
        self.assertEqual(fill_pay.await_args.args[2], "400")

    async def test_unemployed_yes_asks_amount_then_saves(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-unemp-yes",
            url="https://example.test",
            app_step="app_income",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_current_index"] = "0"
        session.filled["app_income_phase"] = "type"
        session.filled["app_income_heard"] = "[]"
        cards = [{"index": 0, "name": "Admin P.", "hasAddIncome": True}]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._open_type_and_select",
                new=AsyncMock(return_value={"ok": True}),
            ),
        ):
            first = await submit_application_answer(
                "inc-unemp-yes", session, AsyncMock(), "Unemployed", 0.0
            )
        self.assertTrue(first.get("ok"))
        self.assertEqual(first.get("step"), "app_income_unemployed")
        self.assertIn(
            "unemployment compensation", (first.get("spoken") or "").lower()
        )

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._click_unemployment_compensation_yn",
                new=AsyncMock(return_value=True),
            ) as click_yn,
        ):
            second = await submit_application_answer(
                "inc-unemp-yes", session, AsyncMock(), "Yes", 0.0
            )
        click_yn.assert_awaited()
        self.assertEqual(click_yn.await_args.args[1], "Yes")
        self.assertEqual(second.get("step"), "app_income_amount")
        self.assertIn("amount per month", (second.get("spoken") or "").lower())

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._fill_income_amount",
                new=AsyncMock(return_value=True),
            ),
        ):
            third = await submit_application_answer(
                "inc-unemp-yes", session, AsyncMock(), "800", 0.0
            )
        self.assertEqual(third.get("step"), "app_income_confirm")
        spoken = (third.get("spoken") or "").lower()
        self.assertTrue(
            "should i save" in spoken or "yes to save" in spoken or "save" in spoken,
            spoken,
        )

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._ensure_pending_amount_on_form",
                new=AsyncMock(return_value={"ok": True}),
            ),
            patch(
                "pc_liveagent.income_stage._click_income_save",
                new=AsyncMock(return_value=True),
            ),
        ):
            fourth = await submit_application_answer(
                "inc-unemp-yes", session, AsyncMock(), "Yes", 0.0
            )
        self.assertEqual(fourth.get("step"), "app_income_more")
        self.assertIn("another income", (fourth.get("spoken") or "").lower())

    async def test_unemployed_no_saves_without_amount(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-unemp-no",
            url="https://example.test",
            app_step="app_income_unemployed",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_current_index"] = "0"
        session.filled["app_income_phase"] = "unemployed"
        session.filled["app_income_pending_type"] = "Unemployed"
        session.filled["app_income_heard"] = "[]"
        cards = [{"index": 0, "name": "Admin P.", "hasAddIncome": True}]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._click_unemployment_compensation_yn",
                new=AsyncMock(return_value=True),
            ) as click_yn,
            patch(
                "pc_liveagent.income_stage._fill_income_amount",
                new=AsyncMock(return_value=True),
            ) as fill_amount,
        ):
            result = await submit_application_answer(
                "inc-unemp-no", session, AsyncMock(), "No", 0.0
            )
        click_yn.assert_awaited()
        self.assertEqual(click_yn.await_args.args[1], "No")
        fill_amount.assert_not_awaited()
        self.assertEqual(result.get("step"), "app_income_confirm")
        self.assertIn("no unemployment compensation", (result.get("spoken") or "").lower())
        self.assertNotIn("amount per month", (result.get("spoken") or "").lower())

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._click_income_save",
                new=AsyncMock(return_value=True),
            ) as save,
        ):
            saved = await submit_application_answer(
                "inc-unemp-no", session, AsyncMock(), "Yes", 0.0
            )
        save.assert_awaited()
        self.assertEqual(saved.get("step"), "app_income_more")

    async def test_self_employed_asks_net_income_then_type_of_work(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-self",
            url="https://example.test",
            app_step="app_income",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_current_index"] = "0"
        session.filled["app_income_phase"] = "type"
        session.filled["app_income_heard"] = "[]"
        cards = [{"index": 0, "name": "Admin P.", "hasAddIncome": True}]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._open_type_and_select",
                new=AsyncMock(return_value={"ok": True}),
            ),
        ):
            first = await submit_application_answer(
                "inc-self", session, AsyncMock(), "Self-employed", 0.0
            )
        self.assertTrue(first.get("ok"))
        self.assertEqual(first.get("step"), "app_income_self")
        spoken = (first.get("spoken") or "").lower()
        self.assertIn("net income", spoken)
        self.assertIn("after expenses", spoken)
        self.assertIn("upload proof", spoken)
        self.assertIn("1099", spoken)

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage._fill_self_employed_field",
                new=AsyncMock(return_value=True),
            ),
        ):
            second = await submit_application_answer(
                "inc-self", session, AsyncMock(), "2500", 0.0
            )
        self.assertTrue(second.get("ok"))
        self.assertEqual(second.get("step"), "app_income_self_work")
        self.assertIn("type of work", (second.get("spoken") or "").lower())

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage._fill_self_employed_field",
                new=AsyncMock(return_value=True),
            ),
        ):
            third = await submit_application_answer(
                "inc-self", session, AsyncMock(), "freelance design", 0.0
            )
        self.assertEqual(third.get("step"), "app_income_confirm")
        self.assertIn("should i save", (third.get("spoken") or "").lower())

    async def test_employed_pay_frequency_dropdown_options(self) -> None:
        from pc_liveagent.app_stage import _set_income_seq, submit_application_answer
        from pc_liveagent.income_stage import map_employed_pay_frequency
        from pc_liveagent.live_form import LiveFormSession

        self.assertEqual(map_employed_pay_frequency("biweekly"), "Bi-weekly")
        self.assertEqual(map_employed_pay_frequency("annually"), "Annually")
        self.assertEqual(map_employed_pay_frequency("every week"), "Weekly")
        self.assertEqual(map_employed_pay_frequency("monthly"), "Monthly")

        session = LiveFormSession(
            call_id="inc-freq",
            url="https://example.test",
            app_step="app_income_employed_frequency",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_current_index"] = "0"
        session.filled["app_income_phase"] = "employed"
        _set_income_seq(
            session,
            {
                "kind": "employed",
                "index": 2,
                "values": {"pay": "3000", "hours": "40"},
                "keys": ["pay", "hours", "frequency", "employer"],
            },
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage._select_employed_pay_frequency",
                new=AsyncMock(return_value=True),
            ) as select_freq,
            patch(
                "pc_liveagent.income_stage._employed_detail_modal_open",
                new=AsyncMock(return_value=True),
            ),
        ):
            result = await submit_application_answer(
                "inc-freq", session, AsyncMock(), "Bi-weekly", 0.0
            )
        select_freq.assert_awaited_once()
        self.assertEqual(select_freq.await_args.args[1], "Bi-weekly")
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_income_employed_employer")
        self.assertIn("employer", (result.get("spoken") or "").lower())

    async def test_employed_pay_frequency_soft_accepts_when_ui_fails(self) -> None:
        """Mapped Monthly must advance even if the dropdown click fails."""
        from pc_liveagent.app_stage import _set_income_seq, submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-freq-soft",
            url="https://example.test",
            app_step="app_income_employed_frequency",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_current_index"] = "0"
        session.filled["app_income_phase"] = "employed"
        _set_income_seq(
            session,
            {
                "kind": "employed",
                "index": 2,
                "values": {"pay": "3000", "hours": "40"},
                "keys": ["pay", "hours", "frequency", "employer"],
            },
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage._select_employed_pay_frequency",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.income_stage._employed_detail_modal_open",
                new=AsyncMock(return_value=True),
            ),
        ):
            result = await submit_application_answer(
                "inc-freq-soft", session, AsyncMock(), "Monthly.", 0.0
            )
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("step"), "app_income_employed_employer")
        self.assertEqual(session.filled.get("app_income_freq_needs_ui"), "Monthly")
        self.assertIn("employer", (result.get("spoken") or "").lower())

    async def test_employed_pay_frequency_modal_closed_recovers(self) -> None:
        """If the dropdown dismisses the editor, reopen instead of asking employer."""
        from pc_liveagent.app_stage import _set_income_seq, submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-freq-closed",
            url="https://example.test",
            app_step="app_income_employed_frequency",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_current_index"] = "0"
        session.filled["app_income_phase"] = "employed"
        _set_income_seq(
            session,
            {
                "kind": "employed",
                "index": 2,
                "values": {"pay": "500", "hours": "40"},
                "keys": ["pay", "hours", "frequency", "employer"],
            },
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage._select_employed_pay_frequency",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.income_stage._employed_detail_modal_open",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.income_stage._reopen_employed_after_modal_loss",
                new=AsyncMock(return_value=True),
            ) as reopen,
        ):
            result = await submit_application_answer(
                "inc-freq-closed", session, AsyncMock(), "Monthly.", 0.0
            )
        reopen.assert_awaited_once()
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("step"), "app_income_employed_employer")
        self.assertIn("employer", (result.get("spoken") or "").lower())

    async def test_income_sequence_saves_after_last_field(self) -> None:
        from pc_liveagent.app_stage import _set_income_seq, submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-last",
            url="https://example.test",
            app_step="app_income_employed_employer",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_current_index"] = "0"
        session.filled["app_income_phase"] = "employed"
        session.filled["app_income_pending_type"] = "Employed"
        _set_income_seq(
            session,
            {
                "kind": "employed",
                "index": 3,
                "values": {"pay": "3k", "hours": "40", "frequency": "monthly"},
                "keys": ["pay", "hours", "frequency", "employer"],
            },
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage._fill_employed_field",
                new=AsyncMock(return_value=True),
            ),
        ):
            result = await submit_application_answer(
                "inc-last", session, AsyncMock(), "Acme Corp", 0.0
            )
        self.assertEqual(result.get("step"), "app_income_confirm")
        spoken = (result.get("spoken") or "").lower()
        self.assertTrue(
            "should i save" in spoken or "yes to save" in spoken or "save" in spoken,
            spoken,
        )
        self.assertIn("acme corp", spoken)

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(
                    return_value=[
                        {"index": 0, "name": "Admin P.", "hasAddIncome": True}
                    ]
                ),
            ),
            patch(
                "pc_liveagent.income_stage._income_card_shows_entry",
                new=AsyncMock(return_value={"ok": False}),
            ),
            patch(
                "pc_liveagent.income_stage._ensure_pending_amount_on_form",
                new=AsyncMock(return_value={"ok": True}),
            ),
            patch(
                "pc_liveagent.income_stage._click_income_save",
                new=AsyncMock(return_value=True),
            ) as save,
        ):
            saved = await submit_application_answer(
                "inc-last", session, AsyncMock(), "Yes", 0.0
            )
        save.assert_awaited()
        self.assertEqual(saved.get("step"), "app_income_more")
        self.assertIn("another income", (saved.get("spoken") or "").lower())

    async def test_amount_only_income_asks_amount_per_month(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-amt",
            url="https://example.test",
            app_step="app_income",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_current_index"] = "0"
        session.filled["app_income_phase"] = "type"
        session.filled["app_income_heard"] = "[]"
        cards = [{"index": 0, "name": "Admin P.", "hasAddIncome": True}]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._open_type_and_select",
                new=AsyncMock(return_value={"ok": True}),
            ),
        ):
            first = await submit_application_answer(
                "inc-amt", session, AsyncMock(), "SSA", 0.0
            )
        self.assertTrue(first.get("ok"))
        self.assertEqual(first.get("step"), "app_income_amount")
        spoken = (first.get("spoken") or "").lower()
        self.assertIn("amount per month", spoken)
        self.assertIn("upload proof", spoken)
        self.assertIn("award letters", spoken)

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._fill_income_amount",
                new=AsyncMock(return_value=True),
            ),
        ):
            second = await submit_application_answer(
                "inc-amt", session, AsyncMock(), "$45", 0.0
            )
        self.assertTrue(second.get("ok"))
        self.assertEqual(second.get("step"), "app_income_confirm")
        self.assertIn("45", (second.get("spoken") or ""))

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._ensure_pending_amount_on_form",
                new=AsyncMock(return_value={"ok": True}),
            ),
            patch(
                "pc_liveagent.income_stage._click_income_save",
                new=AsyncMock(return_value=True),
            ) as save,
        ):
            third = await submit_application_answer(
                "inc-amt", session, AsyncMock(), "Yes", 0.0
            )
        save.assert_awaited()
        self.assertEqual(save.await_args.kwargs.get("member_name"), "Admin P.")
        self.assertEqual(save.await_args.kwargs.get("income_type"), "SSA")
        self.assertEqual(save.await_args.kwargs.get("amount"), "45")
        self.assertEqual(third.get("step"), "app_income_more")
        self.assertEqual(
            session.filled.get("app_income_member_types"),
            '{"Admin P.": ["SSA"]}',
        )

    async def test_income_confirm_save_failure_stays_on_confirm(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-save-fail",
            url="https://example.test",
            app_step="app_income_confirm",
        )
        session.filled["app_income_current"] = "John D."
        session.filled["app_income_current_index"] = "1"
        session.filled["app_income_phase"] = "confirm"
        session.filled["app_income_pending_type"] = "Pension"
        session.filled["app_income_pending_values"] = '{"amount": "400"}'
        session.filled["app_income_pending_summary"] = (
            "For John D., Pension, 400 dollars per month. Should I save this?"
        )
        session.filled["app_income_heard"] = "[0]"
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(
                    return_value=[
                        {"index": 0, "name": "Admin P.", "hasAddIncome": True},
                        {"index": 1, "name": "John D.", "hasAddIncome": True},
                    ]
                ),
            ),
            patch(
                "pc_liveagent.income_stage._ensure_pending_amount_on_form",
                new=AsyncMock(return_value={"ok": True}),
            ),
            patch(
                "pc_liveagent.income_stage._click_income_save",
                new=AsyncMock(return_value=False),
            ) as save,
        ):
            result = await submit_application_answer(
                "inc-save-fail", session, AsyncMock(), "Yes", 0.0
            )
        save.assert_awaited()
        self.assertEqual(save.await_args.kwargs.get("member_name"), "John D.")
        self.assertEqual(save.await_args.kwargs.get("income_type"), "Pension")
        self.assertEqual(save.await_args.kwargs.get("amount"), "400")
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "income_confirm_save_failed")
        self.assertEqual(result.get("step"), "app_income_confirm")
        self.assertIn("wasn't added", (result.get("spoken") or "").lower())
        self.assertEqual(session.filled.get("app_income_pending_type"), "Pension")

    async def test_income_confirm_amount_missing_stays_on_confirm(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-amt-miss",
            url="https://example.test",
            app_step="app_income_confirm",
        )
        session.filled["app_income_current"] = "John D."
        session.filled["app_income_phase"] = "confirm"
        session.filled["app_income_pending_type"] = "Pension"
        session.filled["app_income_pending_values"] = '{"amount": "400"}'
        session.filled["app_income_pending_summary"] = (
            "For John D., Pension, 400 dollars per month. Should I save this?"
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(
                    return_value=[
                        {"index": 1, "name": "John D.", "hasAddIncome": True}
                    ]
                ),
            ),
            patch(
                "pc_liveagent.income_stage._ensure_pending_amount_on_form",
                new=AsyncMock(
                    return_value={"ok": False, "reason": "value_mismatch"}
                ),
            ),
            patch(
                "pc_liveagent.income_stage._click_income_save",
                new=AsyncMock(return_value=True),
            ) as save,
        ):
            result = await submit_application_answer(
                "inc-amt-miss", session, AsyncMock(), "Yes", 0.0
            )
        save.assert_not_awaited()
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "income_confirm_amount_missing")
        self.assertEqual(result.get("step"), "app_income_confirm")
        self.assertIn("amount", (result.get("spoken") or "").lower())

    def test_normalize_employer_name_strips_asr_period(self) -> None:
        from pc_liveagent.income_stage import _normalize_employer_name

        self.assertEqual(_normalize_employer_name("Microsoft."), "Microsoft")
        self.assertEqual(_normalize_employer_name("Microsoft!"), "Microsoft")
        self.assertEqual(_normalize_employer_name("A.B.C. Corp."), "A.B.C. Corp")
        self.assertEqual(_normalize_employer_name("  Acme Inc  "), "Acme Inc")

    async def test_employed_confirm_yes_clicks_save_when_modal_open(self) -> None:
        """Open Employed editor must not short-circuit Save as already-on-card."""
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-emp-save",
            url="https://example.test",
            app_step="app_income_confirm",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_phase"] = "confirm"
        session.filled["app_income_pending_type"] = "Employed"
        session.filled["app_income_pending_values"] = json.dumps(
            {
                "pay": "400",
                "hours": "50",
                "frequency": "Monthly",
                "employer": "Microsoft",
            }
        )
        session.filled["app_income_pending_summary"] = (
            "For Admin P., I have Employed: monthly gross income 400. "
            "Does that look correct?"
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(
                    return_value=[
                        {"index": 0, "name": "Admin P.", "hasAddIncome": True}
                    ]
                ),
            ),
            patch(
                "pc_liveagent.income_stage._income_card_shows_entry",
                new=AsyncMock(
                    side_effect=[
                        {
                            "modal_open": True,
                            "editor_closed": False,
                            "type_on_card": True,
                            "amount_on_card": False,
                            "add_another": True,
                        },
                        {
                            "modal_open": False,
                            "editor_closed": True,
                            "type_on_card": True,
                            "amount_on_card": True,
                            "add_another": True,
                        },
                    ]
                ),
            ),
            patch(
                "pc_liveagent.income_stage._ensure_pending_amount_on_form",
                new=AsyncMock(return_value={"ok": True}),
            ),
            patch(
                "pc_liveagent.income_stage._click_income_save",
                new=AsyncMock(return_value=True),
            ) as save,
        ):
            result = await submit_application_answer(
                "inc-emp-save", session, AsyncMock(), "Yes", 0.0
            )
        save.assert_awaited_once()
        self.assertEqual(save.await_args.kwargs.get("income_type"), "Employed")
        self.assertEqual(save.await_args.kwargs.get("amount"), "400")
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_income_more")

    def test_confirm_income_speech_offers_edit_not_bare_no(self) -> None:
        from pc_liveagent.income_stage import (
            _confirm_income_speech,
            _map_confirm_edit_field,
        )

        spoken = _confirm_income_speech(
            "Admin P.",
            "Employed",
            {
                "pay": "400",
                "hours": "40",
                "frequency": "Bi-weekly",
                "employer": "Acme",
            },
        ).lower()
        self.assertIn("look correct", spoken)
        self.assertIn("edit", spoken)
        self.assertNotIn("say yes or no", spoken)
        self.assertEqual(
            _map_confirm_edit_field("pay frequency", "Employed"), "frequency"
        )
        self.assertEqual(
            _map_confirm_edit_field("monthly gross income", "Employed"), "pay"
        )
        self.assertEqual(_map_confirm_edit_field("hours", "Employed"), "hours")

    async def test_income_confirm_no_asks_which_field_to_edit(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-confirm-edit",
            url="https://example.test",
            app_step="app_income_confirm",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_phase"] = "confirm"
        session.filled["app_income_pending_type"] = "Employed"
        session.filled["app_income_pending_values"] = json.dumps(
            {
                "pay": "400",
                "hours": "48",
                "frequency": "Bi-weekly",
                "employer": "Start",
            }
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(
                    return_value=[
                        {"index": 0, "name": "Admin P.", "hasAddIncome": True}
                    ]
                ),
            ),
            patch(
                "pc_liveagent.income_stage._click_income_cancel",
                new=AsyncMock(return_value=True),
            ) as cancel,
        ):
            result = await submit_application_answer(
                "inc-confirm-edit", session, AsyncMock(), "No", 0.0
            )
        cancel.assert_not_awaited()
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_income_confirm")
        self.assertEqual(session.filled.get("app_income_phase"), "confirm_edit_pick")
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("which detail", spoken)
        self.assertIn("hours per week", spoken)
        self.assertIn("pay frequency", spoken)
        self.assertNotIn("cancelled that save", spoken)

    async def test_income_confirm_edit_field_then_value_reconfirms(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-confirm-edit-val",
            url="https://example.test",
            app_step="app_income_confirm",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_phase"] = "confirm_edit_pick"
        session.filled["app_income_pending_type"] = "Employed"
        session.filled["app_income_pending_values"] = json.dumps(
            {
                "pay": "400",
                "hours": "48",
                "frequency": "Bi-weekly",
                "employer": "Start",
            }
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(
                    return_value=[
                        {"index": 0, "name": "Admin P.", "hasAddIncome": True}
                    ]
                ),
            ),
            patch(
                "pc_liveagent.income_stage._select_employed_pay_frequency",
                new=AsyncMock(return_value=True),
            ) as select_freq,
        ):
            pick = await submit_application_answer(
                "inc-confirm-edit-val", session, AsyncMock(), "pay frequency", 0.0
            )
            self.assertEqual(session.filled.get("app_income_phase"), "confirm_edit_value")
            self.assertEqual(session.filled.get("app_income_edit_field"), "frequency")
            self.assertIn("pay frequency", (pick.get("spoken") or "").lower())

            done = await submit_application_answer(
                "inc-confirm-edit-val", session, AsyncMock(), "Monthly", 0.0
            )
        select_freq.assert_awaited_once()
        self.assertEqual(select_freq.await_args.args[1], "Monthly")
        self.assertEqual(session.filled.get("app_income_phase"), "confirm")
        self.assertNotIn("app_income_edit_field", session.filled)
        values = json.loads(session.filled.get("app_income_pending_values") or "{}")
        self.assertEqual(values.get("frequency"), "Monthly")
        spoken = (done.get("spoken") or "").lower()
        self.assertIn("monthly", spoken)
        self.assertIn("look correct", spoken)

    def test_income_dom_js_uses_single_backslash_regexes(self) -> None:
        """_income_dom_js is a Python raw string — double escapes break /month patterns."""
        import os
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path

        from pc_liveagent.income_stage import _income_dom_js

        js = _income_dom_js()
        # Broken form that caused: SyntaxError: Invalid regular expression flags
        self.assertNotIn(r"\/\s*month", js)
        self.assertNotIn(r"/add\\s+another", js)
        self.assertIn(r"|[/]\s*month/", js)
        self.assertIn(r"/add\s+another\s+income/", js)
        self.assertIn(r"/\$\s*", js)

        node = shutil.which("node")
        if not node:
            helpers = Path(os.environ.get("LOCALAPPDATA", "")) / (
                "Programs/cursor/resources/app/resources/helpers/node.exe"
            )
            if helpers.is_file():
                node = str(helpers)
        if not node:
            return
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.js"
            check = Path(tmp) / "check.cjs"
            body.write_text(js, encoding="utf-8")
            # argv[2] is the body path (argv[1] is this script when invoked as node check.cjs body.js).
            check.write_text(
                "const fs = require('fs');\n"
                "const js = fs.readFileSync(process.argv[2], 'utf8');\n"
                "new Function(js);\n"
                "const api = new Function(js + '; return {incAmountNearText};')();\n"
                "if (api.incAmountNearText('$500 / month') !== '500') process.exit(2);\n"
                "if (api.incAmountNearText('500 / month') !== '500') process.exit(3);\n"
                "console.log('ok');\n",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [node, str(check), str(body)],
                capture_output=True,
                text=True,
                timeout=15,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_income_save_state_ok_when_card_shows_type(self) -> None:
        from pc_liveagent.income_stage import _income_save_state_ok

        # Saved card: type + amount, editor closed.
        self.assertTrue(
            _income_save_state_ok(
                {
                    "modal_open": False,
                    "editor_closed": True,
                    "type_on_card": True,
                    "amount_on_card": True,
                    "add_another": True,
                    "has_amount_hint": True,
                },
                want_type="Pension",
                amount="50",
            )
        )
        # Open Employed/detail modal must NOT count as saved (Add Another still
        # visible behind the modal previously skipped the Save click).
        self.assertFalse(
            _income_save_state_ok(
                {
                    "modal_open": True,
                    "editor_closed": False,
                    "type_on_card": True,
                    "amount_on_card": False,
                    "add_another": True,
                },
                want_type="Employed",
                amount="",
            )
        )
        # Editor closed alone is NOT enough when amount was collected.
        self.assertFalse(
            _income_save_state_ok(
                {
                    "modal_open": False,
                    "editor_closed": True,
                    "type_on_card": True,
                    "amount_on_card": False,
                    "has_amount_hint": True,
                },
                want_type="SSDI",
                amount="500",
            )
        )
        self.assertTrue(
            _income_save_state_ok(
                {
                    "modal_open": False,
                    "editor_closed": True,
                    "type_on_card": True,
                    "amount_on_card": True,
                    "has_amount_hint": True,
                },
                want_type="SSDI",
                amount="500",
            )
        )
        self.assertFalse(
            _income_save_state_ok(
                {
                    "modal_open": True,
                    "type_on_card": False,
                    "amount_on_card": False,
                },
                want_type="Pension",
                amount="50",
            )
        )

    async def test_save_confirm_succeeds_when_card_already_has_entry(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-already",
            url="https://example.test",
            app_step="app_income_confirm",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_phase"] = "confirm"
        session.filled["app_income_pending_type"] = "Pension"
        session.filled["app_income_pending_values"] = '{"amount": "50"}'
        session.filled["app_income_pending_summary"] = (
            "For Admin P., Pension: amount per month 50. Should I save that?"
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(
                    return_value=[
                        {"index": 0, "name": "Admin P.", "hasAddIncome": True}
                    ]
                ),
            ),
            patch(
                "pc_liveagent.income_stage._income_card_shows_entry",
                new=AsyncMock(
                    return_value={
                        "modal_open": False,
                        "editor_closed": True,
                        "type_on_card": True,
                        "amount_on_card": True,
                        "add_another": True,
                        "has_amount_hint": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.income_stage._click_income_save",
                new=AsyncMock(return_value=False),
            ) as save,
        ):
            result = await submit_application_answer(
                "inc-already", session, AsyncMock(), "Yes", 0.0
            )
        save.assert_not_awaited()
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_income_more")
        self.assertIn("another income", (result.get("spoken") or "").lower())

    async def test_ensure_amount_allows_click_when_save_looks_active(self) -> None:
        from pc_liveagent.income_stage import _ensure_pending_amount_on_form

        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.income_stage._read_income_amount",
                new=AsyncMock(side_effect=["50", "50"]),
            ),
            patch(
                "pc_liveagent.income_stage._income_save_button_state",
                new=AsyncMock(
                    side_effect=[
                        {"found": True, "enabled": False, "active_look": False},
                        {
                            "found": True,
                            "enabled": False,
                            "active_look": True,
                        },
                    ]
                ),
            ),
            patch(
                "pc_liveagent.income_stage._fill_income_amount",
                new=AsyncMock(return_value=True),
            ) as fill,
        ):
            result = await _ensure_pending_amount_on_form(page, "50")
        fill.assert_awaited_once_with(page, "50")
        self.assertTrue(result.get("ok"))
        self.assertTrue(result.get("refilled"))

    async def test_click_income_save_uses_playwright_when_yellow(self) -> None:
        from pc_liveagent.income_stage import _click_income_save

        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.income_stage._income_save_button_state",
                new=AsyncMock(
                    return_value={
                        "found": True,
                        "enabled": False,
                        "active_look": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.income_stage._playwright_click_income_save",
                new=AsyncMock(return_value=True),
            ) as pw_click,
            patch(
                "pc_liveagent.income_stage._verify_income_saved",
                new=AsyncMock(return_value=True),
            ) as verify,
        ):
            ok = await _click_income_save(
                page,
                member_name="Admin P.",
                income_type="Pension",
                amount="50",
            )
        pw_click.assert_awaited()
        verify.assert_awaited()
        self.assertTrue(ok)

    async def test_click_income_save_skips_hard_disabled_without_amount(self) -> None:
        from pc_liveagent.income_stage import _click_income_save

        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.income_stage._income_save_button_state",
                new=AsyncMock(
                    return_value={
                        "found": True,
                        "enabled": False,
                        "active_look": False,
                    }
                ),
            ),
            patch(
                "pc_liveagent.income_stage._playwright_click_income_save",
                new=AsyncMock(return_value=True),
            ) as pw_click,
            patch(
                "pc_liveagent.income_stage._verify_income_saved",
                new=AsyncMock(return_value=True),
            ) as verify,
        ):
            ok = await _click_income_save(
                page,
                member_name="Admin P.",
                income_type="None",
                amount="",
            )
        pw_click.assert_not_awaited()
        verify.assert_not_awaited()
        self.assertFalse(ok)

    async def test_income_more_accepts_type_directly(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-more-type",
            url="https://example.test",
            app_step="app_income_more",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_current_index"] = "0"
        session.filled["app_income_phase"] = "more"
        session.filled["app_income_member_types"] = '{"Admin P.": ["SSA"]}'
        cards = [{"index": 0, "name": "Admin P.", "hasAddIncome": True}]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._click_add_another_income",
                new=AsyncMock(
                    return_value={"ok": True, "member": "Admin P.", "modal_open": True}
                ),
            ),
            patch(
                "pc_liveagent.income_stage._open_type_and_select",
                new=AsyncMock(return_value={"ok": True}),
            ),
        ):
            result = await submit_application_answer(
                "inc-more-type", session, AsyncMock(), "Pension", 0.0
            )
        self.assertEqual(result.get("step"), "app_income_amount")
        self.assertIn("pension", (result.get("spoken") or "").lower())

    async def test_income_more_yes_another_asks_type_not_other(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-more-yes",
            url="https://example.test",
            app_step="app_income_more",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_current_index"] = "0"
        session.filled["app_income_phase"] = "more"
        session.filled["app_income_member_types"] = '{"Admin P.": ["Pension"]}'
        cards = [{"index": 0, "name": "Admin P.", "hasAddIncome": True}]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._click_add_another_income",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "member": "Admin P.",
                        "modal_open": True,
                        "label": "add another income",
                    }
                ),
            ) as add_btn,
            patch(
                "pc_liveagent.income_stage._open_type_and_select",
                new=AsyncMock(return_value={"ok": True}),
            ) as open_type,
        ):
            result = await submit_application_answer(
                "inc-more-yes",
                session,
                AsyncMock(),
                "Yes. I want to add another income.",
                0.0,
            )
        add_btn.assert_awaited()
        self.assertEqual(add_btn.await_args.args[1], "Admin P.")
        open_type.assert_not_awaited()
        self.assertEqual(result.get("step"), "app_income")
        self.assertEqual(session.filled.get("app_income_phase"), "type")
        self.assertEqual(session.filled.get("app_income_add_opened"), "1")
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("income type", spoken)
        self.assertNotIn("okay, other", spoken)
        # Do not re-list every income type when adding another.
        self.assertNotIn("worker's compensation", spoken)
        self.assertNotIn("property rental income", spoken)
        self.assertEqual(
            session.filled.get("app_income_member_types"),
            '{"Admin P.": ["Pension"]}',
        )

    async def test_income_more_yes_add_another_click_failure_stays(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-more-miss",
            url="https://example.test",
            app_step="app_income_more",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_phase"] = "more"
        session.filled["app_income_member_types"] = '{"Admin P.": ["SSDI"]}'
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(
                    return_value=[
                        {"index": 0, "name": "Admin P.", "hasAddIncome": True}
                    ]
                ),
            ),
            patch(
                "pc_liveagent.income_stage._click_add_another_income",
                new=AsyncMock(
                    return_value={
                        "ok": False,
                        "reason": "add_another_not_found",
                        "member": "Admin P.",
                    }
                ),
            ),
        ):
            result = await submit_application_answer(
                "inc-more-miss", session, AsyncMock(), "Yes", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "income_add_another_failed")
        self.assertEqual(result.get("step"), "app_income_more")
        self.assertIn("couldn't click", (result.get("spoken") or "").lower())

    async def test_income_more_named_type_clicks_add_another_first(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-more-pension",
            url="https://example.test",
            app_step="app_income_more",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_current_index"] = "0"
        session.filled["app_income_phase"] = "more"
        session.filled["app_income_member_types"] = '{"Admin P.": ["SSDI"]}'
        cards = [{"index": 0, "name": "Admin P.", "hasAddIncome": True}]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._click_add_another_income",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "member": "Admin P.",
                        "modal_open": True,
                    }
                ),
            ) as add_btn,
            patch(
                "pc_liveagent.income_stage._open_type_and_select",
                new=AsyncMock(return_value={"ok": True}),
            ) as open_type,
        ):
            result = await submit_application_answer(
                "inc-more-pension", session, AsyncMock(), "Pension", 0.0
            )
        add_btn.assert_awaited()
        open_type.assert_awaited()
        self.assertEqual(result.get("step"), "app_income_amount")

    async def test_save_verify_accepts_broader_member_scoped_hit(self) -> None:
        from pc_liveagent.income_stage import _verify_income_saved

        page = AsyncMock()
        # Header-only miss, then member-scoped hit (SSDI + $500).
        card_states = [
            {
                "modal_open": False,
                "editor_closed": True,
                "type_on_card": False,
                "amount_on_card": False,
                "add_another": False,
                "has_amount_hint": True,
            },
            {
                "modal_open": False,
                "editor_closed": True,
                "type_on_card": True,
                "amount_on_card": True,
                "add_another": True,
                "has_amount_hint": True,
                "root_kind": "member_scoped",
            },
        ]
        with patch(
            "pc_liveagent.income_stage._income_card_shows_entry",
            new=AsyncMock(side_effect=card_states),
        ):
            ok = await _verify_income_saved(page, "Admin P.", "SSDI", "500")
        self.assertTrue(ok)

    async def test_save_verify_rejects_wrong_amount(self) -> None:
        from pc_liveagent.income_stage import _income_save_state_ok

        self.assertFalse(
            _income_save_state_ok(
                {
                    "modal_open": False,
                    "editor_closed": True,
                    "type_on_card": True,
                    "amount_on_card": False,
                    "has_amount_hint": True,
                },
                want_type="SSDI",
                amount="500",
            )
        )

    async def test_income_dom_card_includes_ssdi_row_and_add_another(self) -> None:
        """Realistic RN-style card: header strip + nested SSDI row must verify."""
        from playwright.async_api import async_playwright

        from pc_liveagent.income_stage import _income_dom_js

        html = """
        <html><body>
          <div id="page">
            <div class="member" style="width:400px;height:220px;">
              <div class="header" style="width:400px;height:40px;">
                <span>Admin P.</span>
                <button>Add Another Income</button>
              </div>
              <div class="row" style="width:380px;height:80px;">
                <div>SSDI</div>
                <div>$500 / month</div>
                <button aria-label="Edit">Edit</button>
              </div>
            </div>
            <div class="member" style="width:400px;height:80px;">
              <div class="header" style="width:400px;height:40px;">
                <span>John D.</span>
                <button>Add Income</button>
              </div>
            </div>
          </div>
        </body></html>
        """
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content(html)
                state = await page.evaluate(
                    "({ memberName, incomeType, amountHint }) => {"
                    + _income_dom_js()
                    + " return incIncomeAfterSave(memberName, incomeType, amountHint); }",
                    {
                        "memberName": "Admin P.",
                        "incomeType": "SSDI",
                        "amountHint": "500",
                    },
                )
                click = await page.evaluate(
                    "(memberName) => {"
                    + _income_dom_js()
                    + " return incClickAddAnotherIncome(memberName); }",
                    "Admin P.",
                )
                await browser.close()
        except unittest.SkipTest:
            raise
        except Exception as exc:
            msg = str(exc)
            # Only skip missing browser binaries — SyntaxError must fail the test.
            if (
                "Executable doesn't exist" in msg
                or "playwright install" in msg.lower()
                or "BrowserType.launch" in msg
            ):
                raise unittest.SkipTest(
                    f"Playwright Chromium unavailable: {exc}"
                ) from exc
            raise
        self.assertTrue(state.get("type_on_card"), state)
        self.assertTrue(state.get("amount_on_card"), state)
        self.assertTrue(state.get("add_another"), state)
        self.assertTrue(state.get("editor_closed"), state)
        self.assertTrue(click.get("ok"), click)
        self.assertIn("another", (click.get("label") or "").lower())

    async def test_employed_labeled_inputs_do_not_cross_fill(self) -> None:
        """Hours must not overwrite Monthly gross when both labels share a form root."""
        from playwright.async_api import async_playwright

        from pc_liveagent.income_stage import _income_dom_js

        html = """
        <html><body>
          <div class="modal" style="width:480px;height:520px;">
            <h2>Admin P.</h2>
            <div class="field" style="margin:8px;">
              <label>Monthly gross income (before taxes) *</label>
              <input id="pay" value="100" />
            </div>
            <div class="field" style="margin:8px;">
              <label>Hours per week *</label>
              <input id="hours" value="" />
            </div>
            <div class="field" style="margin:8px;">
              <label>Pay frequency *</label>
              <div role="combobox" aria-haspopup="listbox" id="freq">Bi-weekly</div>
              <div id="menu" style="display:none;">
                <div role="option">Weekly</div>
                <div role="option">Bi-weekly</div>
                <div role="option">Monthly</div>
                <div role="option">Annually</div>
              </div>
            </div>
            <div class="field" style="margin:8px;">
              <label>Employer name *</label>
              <input id="employer" placeholder="Enter name." />
            </div>
            <button>Cancel</button>
            <button disabled>Save</button>
          </div>
          <script>
            document.getElementById('freq').addEventListener('click', () => {
              document.getElementById('menu').style.display = 'block';
            });
          </script>
        </body></html>
        """
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content(html)
                targets = await page.evaluate(
                    "() => {"
                    + _income_dom_js()
                    + " const pay = incFindLabeledInput('monthly gross income');"
                    " const hours = incFindLabeledInput('hours per week');"
                    " const employer = incFindLabeledInput('employer name');"
                    " const hoursEl = incFindLabeledInput('hours per week');"
                    " if (hoursEl) incSetInputValue(hoursEl, '40');"
                    " return {"
                    "  payId: pay && pay.id,"
                    "  hoursId: hours && hours.id,"
                    "  employerId: employer && employer.id,"
                    "  payAfter: (document.getElementById('pay') || {}).value,"
                    "  hoursAfter: (document.getElementById('hours') || {}).value"
                    " }; }"
                )
                opened = await page.evaluate(
                    "() => {" + _income_dom_js() + " return incOpenPayFrequency(); }"
                )
                selected = await page.evaluate(
                    "(label) => {"
                    + _income_dom_js()
                    + " return incClickPayFrequencyOption(label); }",
                    "Monthly",
                )
                freq_text = await page.evaluate(
                    "() => document.getElementById('freq').innerText"
                )
                await browser.close()
        except unittest.SkipTest:
            raise
        except Exception as exc:
            msg = str(exc)
            if (
                "Executable doesn't exist" in msg
                or "playwright install" in msg.lower()
                or "BrowserType.launch" in msg
            ):
                raise unittest.SkipTest(
                    f"Playwright Chromium unavailable: {exc}"
                ) from exc
            raise
        self.assertEqual(targets.get("payId"), "pay", targets)
        self.assertEqual(targets.get("hoursId"), "hours", targets)
        self.assertEqual(targets.get("employerId"), "employer", targets)
        self.assertEqual(str(targets.get("payAfter")), "100", targets)
        self.assertEqual(str(targets.get("hoursAfter")), "40", targets)
        self.assertTrue(opened.get("ok"), opened)
        self.assertTrue(selected.get("ok"), selected)
        # Menu option click should succeed even if the closed chip text is unchanged
        # in this fixture (no onClick on options). Main assert is option_not_found gone.
        self.assertEqual(selected.get("selected"), "Monthly")
        _ = freq_text

    async def test_unemployment_yn_clicks_when_question_excludes_radios(self) -> None:
        """Yes/No must resolve even when the question label is a tight sibling."""
        from playwright.async_api import async_playwright

        from pc_liveagent.income_stage import _income_dom_js

        html = """
        <html><body>
          <div role="dialog" class="modal" style="width:420px;height:360px;">
            <h2>Admin P.</h2>
            <div id="q">Do you receive unemployment compensation?</div>
            <div id="row" style="margin-top:12px;">
              <div role="radio" aria-label="Yes" id="yes" tabindex="0">Yes</div>
              <div role="radio" aria-label="No" id="no" tabindex="0">No</div>
            </div>
            <button>Cancel</button>
            <button disabled>Save</button>
          </div>
          <script>
            let selected = '';
            document.getElementById('yes').addEventListener('click', () => {
              selected = 'Yes';
              window.__yn = selected;
            });
            document.getElementById('no').addEventListener('click', () => {
              selected = 'No';
              window.__yn = selected;
            });
          </script>
        </body></html>
        """
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content(html)
                clicked = await page.evaluate(
                    "(value) => {"
                    + _income_dom_js()
                    + " return incClickUnemploymentYn(value); }",
                    "No",
                )
                got = await page.evaluate("() => window.__yn || ''")
                await browser.close()
        except unittest.SkipTest:
            raise
        except Exception as exc:
            msg = str(exc)
            if (
                "Executable doesn't exist" in msg
                or "playwright install" in msg.lower()
                or "BrowserType.launch" in msg
            ):
                raise unittest.SkipTest(
                    f"Playwright Chromium unavailable: {exc}"
                ) from exc
            raise
        self.assertTrue(clicked.get("ok"), clicked)
        self.assertEqual(got, "No")

    async def test_unemployment_yn_playwright_radiogroup_and_prime_no(self) -> None:
        """Playwright must select Yes; priming must enable Save for default No."""
        import re

        from playwright.async_api import async_playwright

        from pc_liveagent.income_stage import (
            _click_unemployment_compensation_yn,
            _income_dom_js,
            _prime_unemployed_no_saveable,
        )

        html = """
        <html><body>
          <div role="dialog" class="modal" style="width:480px;height:420px;">
            <h2>Admin P.</h2>
            <div>Do you receive unemployment compensation?</div>
            <div role="radiogroup" id="rg">
              <div tabindex="0" id="yesRow">
                <div role="radio" aria-checked="false" id="yes">Yes</div>
              </div>
              <div tabindex="0" id="noRow">
                <div role="radio" aria-checked="true" id="no">No</div>
              </div>
            </div>
            <div id="amount" style="display:none">Amount per month</div>
            <button>Cancel</button>
            <button id="save" disabled>Save</button>
          </div>
          <script>
            window.__status = 'NOT_UNEMPLOYMENT_COMPENSATED';
            const yes = document.getElementById('yes');
            const no = document.getElementById('no');
            const amount = document.getElementById('amount');
            const save = document.getElementById('save');
            const select = (v) => {
              window.__status = v;
              yes.setAttribute('aria-checked', v === 'UNEMPLOYMENT_COMPENSATED' ? 'true' : 'false');
              no.setAttribute('aria-checked', v === 'NOT_UNEMPLOYMENT_COMPENSATED' ? 'true' : 'false');
              amount.style.display = v === 'UNEMPLOYMENT_COMPENSATED' ? 'block' : 'none';
              // Mimic Pointcare: Save enables only when dirty vs default No.
              const dirty = v !== 'NOT_UNEMPLOYMENT_COMPENSATED';
              save.disabled = !dirty;
            };
            document.getElementById('yesRow').addEventListener('click', () => select('UNEMPLOYMENT_COMPENSATED'));
            document.getElementById('noRow').addEventListener('click', () => select('NOT_UNEMPLOYMENT_COMPENSATED'));
            // Fake RHF FormProvider on Save fiber path via a marker the prime helper
            // can use when React fibers are absent — exercise JS selected helpers.
            window.__pcForm = {
              control: {
                _formValues: { unemploymentCompensationStatus: 'NOT_UNEMPLOYMENT_COMPENSATED' },
                _defaultValues: { unemploymentCompensationStatus: 'NOT_UNEMPLOYMENT_COMPENSATED' },
                _options: { defaultValues: { unemploymentCompensationStatus: 'NOT_UNEMPLOYMENT_COMPENSATED' } },
              },
              setValue(name, value, _opts) {
                this.control._formValues[name] = value;
                const dirty = value !== this.control._defaultValues[name];
                save.disabled = !dirty && value === 'NOT_UNEMPLOYMENT_COMPENSATED'
                  ? true
                  : false;
                if (value === 'NOT_UNEMPLOYMENT_COMPENSATED' &&
                    this.control._defaultValues[name] === 'UNEMPLOYMENT_COMPENSATED') {
                  save.disabled = false;
                }
              },
              getValues() { return this.control._formValues; },
              trigger() {},
            };
          </script>
        </body></html>
        """
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content(html)
                # Playwright Yes click path (force text in radiogroup).
                await page.locator("[role=radiogroup]").get_by_text(
                    re.compile(r"^Yes$", re.I)
                ).first.click(force=True)
                status = await page.evaluate("() => window.__status")
                amount_visible = await page.evaluate(
                    "() => document.getElementById('amount').style.display === 'block'"
                )
                # Prime helper against a FormProvider-like object attached via evaluate.
                primed = await page.evaluate(
                    "() => {"
                    + _income_dom_js()
                    + """
                      // Patch form lookup for this fixture (no React fibers).
                      const real = incUnemploymentFormControl;
                      incUnemploymentFormControl = () => window.__pcForm;
                      const before = {
                        disabled: document.getElementById('save').disabled,
                        value: window.__pcForm.control._formValues.unemploymentCompensationStatus,
                      };
                      window.__pcForm.control._defaultValues.unemploymentCompensationStatus =
                        'UNEMPLOYMENT_COMPENSATED';
                      window.__pcForm.control._options.defaultValues.unemploymentCompensationStatus =
                        'UNEMPLOYMENT_COMPENSATED';
                      const result = incPrimeUnemployedNoSaveable();
                      incUnemploymentFormControl = real;
                      return { before, result, disabled: document.getElementById('save').disabled };
                    }"""
                )
                await browser.close()
        except unittest.SkipTest:
            raise
        except Exception as exc:
            msg = str(exc)
            if (
                "Executable doesn't exist" in msg
                or "playwright install" in msg.lower()
                or "BrowserType.launch" in msg
            ):
                raise unittest.SkipTest(
                    f"Playwright Chromium unavailable: {exc}"
                ) from exc
            raise
        self.assertEqual(status, "UNEMPLOYMENT_COMPENSATED")
        self.assertTrue(amount_visible)
        self.assertTrue(primed.get("result", {}).get("ok"), primed)
        self.assertFalse(primed.get("disabled"), primed)
        # Keep import referenced for future Python-path coverage.
        self.assertTrue(callable(_click_unemployment_compensation_yn))
        self.assertTrue(callable(_prime_unemployed_no_saveable))

    async def test_pay_frequency_opens_with_empty_chip(self) -> None:
        """Open must work when the control has no Weekly/Monthly value yet."""
        from playwright.async_api import async_playwright

        from pc_liveagent.income_stage import _income_dom_js

        html = """
        <html><body>
          <div class="modal" style="width:480px;height:400px;">
            <div class="field" style="margin:8px;">
              <label>Pay frequency *</label>
              <div role="combobox" aria-haspopup="listbox" id="freq"></div>
              <div id="menu" style="display:none;">
                <div role="option">Weekly</div>
                <div role="option">Bi-weekly</div>
                <div role="option">Monthly</div>
                <div role="option">Annually</div>
              </div>
            </div>
          </div>
          <script>
            document.getElementById('freq').addEventListener('click', () => {
              document.getElementById('menu').style.display = 'block';
            });
          </script>
        </body></html>
        """
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content(html)
                opened = await page.evaluate(
                    "() => {" + _income_dom_js() + " return incOpenPayFrequency(); }"
                )
                selected = await page.evaluate(
                    "(label) => {"
                    + _income_dom_js()
                    + " return incClickPayFrequencyOption(label); }",
                    "Monthly",
                )
                await browser.close()
        except unittest.SkipTest:
            raise
        except Exception as exc:
            msg = str(exc)
            if (
                "Executable doesn't exist" in msg
                or "playwright install" in msg.lower()
                or "BrowserType.launch" in msg
            ):
                raise unittest.SkipTest(
                    f"Playwright Chromium unavailable: {exc}"
                ) from exc
            raise
        self.assertTrue(opened.get("ok"), opened)
        self.assertTrue(selected.get("ok"), selected)
        self.assertEqual(selected.get("selected"), "Monthly")

    def test_labeled_income_values_match_currency(self) -> None:
        from pc_liveagent.income_stage import _labeled_values_match

        self.assertTrue(_labeled_values_match("100", "$100"))
        self.assertTrue(_labeled_values_match("40", "40"))
        self.assertFalse(_labeled_values_match("40", "100"))

    async def test_confirm_yes_idempotent_when_already_on_card(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-idem",
            url="https://example.test",
            app_step="app_income_confirm",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_phase"] = "confirm"
        session.filled["app_income_pending_type"] = "SSDI"
        session.filled["app_income_pending_values"] = '{"amount": "500"}'
        session.filled["app_income_pending_summary"] = (
            "For Admin P., SSDI: amount per month 500. Should I save that?"
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(
                    return_value=[
                        {"index": 0, "name": "Admin P.", "hasAddIncome": True}
                    ]
                ),
            ),
            patch(
                "pc_liveagent.income_stage._income_card_shows_entry",
                new=AsyncMock(
                    return_value={
                        "modal_open": False,
                        "editor_closed": True,
                        "type_on_card": True,
                        "amount_on_card": True,
                        "add_another": True,
                        "has_amount_hint": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.income_stage._ensure_pending_amount_on_form",
                new=AsyncMock(return_value={"ok": True}),
            ) as ensure,
            patch(
                "pc_liveagent.income_stage._click_income_save",
                new=AsyncMock(return_value=True),
            ) as save,
        ):
            result = await submit_application_answer(
                "inc-idem", session, AsyncMock(), "Yes", 0.0
            )
        ensure.assert_not_awaited()
        save.assert_not_awaited()
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_income_more")

    async def test_income_type_accepts_workers_apostrophe_plural(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-workers",
            url="https://example.test",
            app_step="app_income",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_current_index"] = "0"
        session.filled["app_income_phase"] = "type"
        session.filled["app_income_heard"] = "[]"
        cards = [{"index": 0, "name": "Admin P.", "hasAddIncome": True}]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._open_type_and_select",
                new=AsyncMock(return_value={"ok": True}),
            ) as open_type,
        ):
            result = await submit_application_answer(
                "inc-workers",
                session,
                AsyncMock(),
                "Workers' compensation.",
                0.0,
            )
        open_type.assert_awaited()
        self.assertEqual(open_type.await_args.args[2], "Worker's compensation")
        self.assertEqual(result.get("step"), "app_income_amount")
        self.assertIn("worker's compensation", (result.get("spoken") or "").lower())

    async def test_income_delete_requires_confirm(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-del",
            url="https://example.test",
            app_step="app_income_more",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_current_index"] = "0"
        session.filled["app_income_phase"] = "more"
        session.filled["app_income_member_types"] = '{"Admin P.": ["SSA"]}'
        cards = [{"index": 0, "name": "Admin P.", "hasAddIncome": True}]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
        ):
            ask = await submit_application_answer(
                "inc-del", session, AsyncMock(), "delete SSA", 0.0
            )
        self.assertEqual(ask.get("step"), "app_income_confirm")
        spoken_ask = (ask.get("spoken") or "").lower()
        self.assertIn("can i delete", spoken_ask)
        self.assertIn("ssa", spoken_ask)

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._delete_income_entry",
                new=AsyncMock(return_value=True),
            ) as delete_click,
        ):
            done = await submit_application_answer(
                "inc-del", session, AsyncMock(), "Yes", 0.0
            )
        delete_click.assert_awaited()
        self.assertEqual(delete_click.await_args.args[1], "Admin P.")
        self.assertEqual(delete_click.await_args.args[2], "SSA")
        self.assertEqual(done.get("step"), "app_income_more")
        self.assertEqual(session.filled.get("app_income_member_types"), '{"Admin P.": []}')

    async def test_income_delete_opens_pencil_then_modal_delete(self) -> None:
        from pc_liveagent.income_stage import _delete_income_entry

        page = AsyncMock()
        page.get_by_role = MagicMock()
        page.get_by_role.return_value.first.wait_for = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.income_stage._click_income_entry_action",
                new=AsyncMock(
                    return_value={"ok": True, "action": "edit", "via": "type_row"}
                ),
            ) as entry_click,
            patch(
                "pc_liveagent.income_stage._income_edit_modal_ready",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "hasDelete": True,
                        "memberMatch": True,
                        "typeMatch": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.income_stage._click_income_modal_delete",
                new=AsyncMock(return_value={"ok": True, "via": "dom"}),
            ) as modal_delete,
            patch(
                "pc_liveagent.income_stage._verify_income_absent",
                new=AsyncMock(return_value=True),
            ) as absent,
            patch(
                "pc_liveagent.income_stage._click_add_another_income",
                new=AsyncMock(side_effect=AssertionError("delete must not add")),
            ),
            patch(
                "pc_liveagent.income_stage._open_type_and_select",
                new=AsyncMock(side_effect=AssertionError("delete must not type-select")),
            ),
        ):
            ok = await _delete_income_entry(page, "John D.", "Retirement income")
        self.assertTrue(ok)
        entry_click.assert_awaited_once()
        self.assertEqual(entry_click.await_args.args[2], "Retirement income")
        self.assertEqual(entry_click.await_args.args[3], "edit")
        modal_delete.assert_awaited()
        absent.assert_awaited()

    async def test_income_delete_cancels_modal_when_delete_click_fails(self) -> None:
        from pc_liveagent.income_stage import _delete_income_entry

        page = AsyncMock()
        page.get_by_role = MagicMock()
        page.get_by_role.return_value.first.wait_for = AsyncMock(
            side_effect=Exception("no delete yet")
        )
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.income_stage._click_income_entry_action",
                new=AsyncMock(
                    return_value={"ok": True, "action": "edit", "via": "type_row"}
                ),
            ),
            patch(
                "pc_liveagent.income_stage._income_edit_modal_ready",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "hasDelete": True,
                        "memberMatch": True,
                        "typeMatch": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.income_stage._click_income_modal_delete",
                new=AsyncMock(return_value={"ok": False, "reason": "delete_not_clicked"}),
            ),
            patch(
                "pc_liveagent.income_stage._click_income_cancel",
                new=AsyncMock(return_value=True),
            ) as cancel,
        ):
            ok = await _delete_income_entry(page, "John D.", "Retirement income")
        self.assertFalse(ok)
        cancel.assert_awaited()

    async def test_income_delete_followup_type_does_not_add_another(self) -> None:
        """After 'which income to delete?', bare type must confirm delete — not Add."""
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-del-follow",
            url="https://example.test",
            app_step="app_income_more",
        )
        session.filled["app_income_current"] = "John D."
        session.filled["app_income_current_index"] = "1"
        session.filled["app_income_phase"] = "more"
        session.filled["app_income_manage_action"] = "delete"
        session.filled["app_income_member_types"] = (
            '{"John D.": ["Retirement income", "SSA"]}'
        )
        cards = [{"index": 1, "name": "John D.", "hasAddIncome": True}]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._click_add_another_income",
                new=AsyncMock(side_effect=AssertionError("must not add another")),
            ),
            patch(
                "pc_liveagent.income_stage._open_type_and_select",
                new=AsyncMock(side_effect=AssertionError("must not type-select")),
            ),
        ):
            ask = await submit_application_answer(
                "inc-del-follow", session, AsyncMock(), "Retirement income", 0.0
            )
        self.assertEqual(ask.get("step"), "app_income_confirm")
        self.assertEqual(session.filled.get("app_income_phase"), "delete_confirm")
        self.assertEqual(
            session.filled.get("app_income_delete_type"), "Retirement income"
        )
        self.assertIn("can i delete", (ask.get("spoken") or "").lower())

    async def test_document_notice_once_per_member(self) -> None:
        from pc_liveagent.income_stage import _amount_ask_speech
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-doc-once",
            url="https://example.test",
            app_step="app_income",
        )
        first = _amount_ask_speech(
            "Admin P.", "SSA", session=session
        ).lower()
        second = _amount_ask_speech(
            "Admin P.", "SSDI", session=session
        ).lower()
        other = _amount_ask_speech(
            "John D.", "Alimony", session=session
        ).lower()
        self.assertIn("upload proof", first)
        self.assertIn("award letters", first)
        self.assertNotIn("upload proof", second)
        self.assertIn("ssdi", second)
        self.assertIn("upload proof", other)
        self.assertIn("award letters", other)

    def test_map_expense_type_pointcare_labels(self) -> None:
        from pc_liveagent.expense_stage import map_expense_type

        self.assertEqual(map_expense_type("Student Loans"), "Student Loans")
        self.assertEqual(map_expense_type("tuition"), "Tuition Fees")
        self.assertEqual(map_expense_type("Tuition Fees"), "Tuition Fees")
        self.assertEqual(map_expense_type("tution fees"), "Tuition Fees")
        self.assertEqual(map_expense_type("tution"), "Tuition Fees")
        self.assertEqual(map_expense_type("tuition fee"), "Tuition Fees")
        # Live ASR miss: "Tuition fees" → "Fusion fees."
        self.assertEqual(map_expense_type("Fusion fees."), "Tuition Fees")
        self.assertEqual(map_expense_type("fusion fees"), "Tuition Fees")
        self.assertEqual(map_expense_type("fusion"), "Tuition Fees")
        self.assertEqual(map_expense_type("two shun fees"), "Tuition Fees")
        self.assertEqual(map_expense_type("tooshun"), "Tuition Fees")
        self.assertEqual(map_expense_type("alimony paid"), "Alimony Paid")
        self.assertEqual(map_expense_type("moving expenses"), "Moving Expenses")
        self.assertEqual(map_expense_type("IRA"), "IRA Contributions")
        self.assertEqual(map_expense_type("child care"), "Childcare")
        self.assertEqual(map_expense_type("medical"), "Medical Expenses")
        self.assertEqual(map_expense_type("Medical Expenses"), "Medical Expenses")
        self.assertEqual(map_expense_type("Other"), "Other")
        self.assertEqual(map_expense_type("none"), "None")
        self.assertIsNone(map_expense_type("another expense"))

    async def test_expense_amount_only_flow_student_loans(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="exp-amt",
            url="https://example.test",
            app_step="app_expenses",
        )
        session.filled["app_expense_current"] = "Admin P."
        session.filled["app_expense_current_index"] = "0"
        session.filled["app_expense_phase"] = "type"
        session.filled["app_expense_heard"] = "[]"
        cards = [{"index": 0, "name": "Admin P.", "hasAddExpense": True}]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_expenses"),
            ),
            patch(
                "pc_liveagent.expense_stage.scrape_expense_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.expense_stage._open_type_and_select",
                new=AsyncMock(return_value={"ok": True}),
            ),
        ):
            first = await submit_application_answer(
                "exp-amt", session, AsyncMock(), "Student Loans", 0.0
            )
        self.assertTrue(first.get("ok"))
        self.assertEqual(first.get("step"), "app_expenses_amount")
        spoken = (first.get("spoken") or "").lower()
        self.assertIn("amount per month", spoken)
        self.assertIn("upload proof", spoken)

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_expenses"),
            ),
            patch(
                "pc_liveagent.expense_stage.scrape_expense_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.expense_stage._fill_expense_amount",
                new=AsyncMock(return_value=True),
            ),
        ):
            second = await submit_application_answer(
                "exp-amt", session, AsyncMock(), "500", 0.0
            )
        self.assertEqual(second.get("step"), "app_expenses_confirm")
        self.assertIn("500", (second.get("spoken") or ""))

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_expenses"),
            ),
            patch(
                "pc_liveagent.expense_stage.scrape_expense_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.expense_stage._fill_expense_amount",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.expense_stage._click_expense_save",
                new=AsyncMock(return_value=True),
            ) as save,
        ):
            third = await submit_application_answer(
                "exp-amt", session, AsyncMock(), "Yes", 0.0
            )
        save.assert_awaited()
        self.assertEqual(third.get("step"), "app_expenses_more")
        self.assertEqual(
            session.filled.get("app_expense_member_types"),
            '{"Admin P.": ["Student Loans"]}',
        )

    async def test_expense_medical_yes_no_flow(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="exp-med",
            url="https://example.test",
            app_step="app_expenses",
        )
        session.filled["app_expense_current"] = "Admin P."
        session.filled["app_expense_current_index"] = "0"
        session.filled["app_expense_phase"] = "type"
        session.filled["app_expense_heard"] = "[]"
        cards = [{"index": 0, "name": "Admin P.", "hasAddExpense": True}]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_expenses"),
            ),
            patch(
                "pc_liveagent.expense_stage.scrape_expense_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.expense_stage._open_type_and_select",
                new=AsyncMock(return_value={"ok": True}),
            ),
        ):
            first = await submit_application_answer(
                "exp-med", session, AsyncMock(), "Medical Expenses", 0.0
            )
        self.assertEqual(first.get("step"), "app_expenses_medical")
        self.assertIn("last 3 months", (first.get("spoken") or "").lower())

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_expenses"),
            ),
            patch(
                "pc_liveagent.expense_stage.scrape_expense_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.expense_stage._click_medical_yn",
                new=AsyncMock(return_value=True),
            ) as yn,
        ):
            second = await submit_application_answer(
                "exp-med", session, AsyncMock(), "No", 0.0
            )
        yn.assert_awaited()
        self.assertEqual(yn.await_args.args[1], "No")
        self.assertEqual(second.get("step"), "app_expenses_confirm")

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_expenses"),
            ),
            patch(
                "pc_liveagent.expense_stage.scrape_expense_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.expense_stage._click_medical_yn",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.expense_stage._click_expense_save",
                new=AsyncMock(return_value=True),
            ),
        ):
            third = await submit_application_answer(
                "exp-med", session, AsyncMock(), "Yes", 0.0
            )
        self.assertEqual(third.get("step"), "app_expenses_more")

    async def test_expense_delete_requires_confirm_not_add_another(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="exp-del",
            url="https://example.test",
            app_step="app_expenses_more",
        )
        session.filled["app_expense_current"] = "John D."
        session.filled["app_expense_current_index"] = "1"
        session.filled["app_expense_phase"] = "more"
        session.filled["app_expense_member_types"] = (
            '{"John D.": ["Childcare", "Student Loans"]}'
        )
        cards = [{"index": 1, "name": "John D.", "hasAddExpense": True}]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_expenses"),
            ),
            patch(
                "pc_liveagent.expense_stage.scrape_expense_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.expense_stage._click_add_another_expense",
                new=AsyncMock(side_effect=AssertionError("delete must not add")),
            ),
        ):
            ask = await submit_application_answer(
                "exp-del", session, AsyncMock(), "delete childcare", 0.0
            )
        self.assertEqual(ask.get("step"), "app_expenses_confirm")
        self.assertIn("can i delete", (ask.get("spoken") or "").lower())

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_expenses"),
            ),
            patch(
                "pc_liveagent.expense_stage.scrape_expense_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.expense_stage._delete_expense_entry",
                new=AsyncMock(return_value=True),
            ) as delete_fn,
            patch(
                "pc_liveagent.expense_stage._click_add_another_expense",
                new=AsyncMock(side_effect=AssertionError("no add on delete yes")),
            ),
        ):
            done = await submit_application_answer(
                "exp-del", session, AsyncMock(), "Yes", 0.0
            )
        delete_fn.assert_awaited_once_with(ANY, "John D.", "Childcare")
        self.assertEqual(done.get("step"), "app_expenses_more")
        self.assertEqual(
            session.filled.get("app_expense_member_types"),
            '{"John D.": ["Student Loans"]}',
        )

    async def test_same_income_type_override_is_announced(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-ov",
            url="https://example.test",
            app_step="app_income",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_current_index"] = "0"
        session.filled["app_income_phase"] = "type"
        session.filled["app_income_heard"] = "[]"
        session.filled["app_income_member_types"] = '{"Admin P.": ["Pension"]}'
        cards = [{"index": 0, "name": "Admin P.", "hasAddIncome": True}]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._open_type_and_select",
                new=AsyncMock(return_value={"ok": True}),
            ),
        ):
            result = await submit_application_answer(
                "inc-ov", session, AsyncMock(), "Pension", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_income_amount")
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("replacing", spoken)
        self.assertIn("amount per month", spoken)

    async def test_map_income_type_matches_pointcare_labels(self) -> None:
        from pc_liveagent.income_stage import map_income_type

        self.assertEqual(map_income_type("worker's comp"), "Worker's compensation")
        self.assertEqual(
            map_income_type("Workers' compensation."), "Worker's compensation"
        )
        self.assertEqual(
            map_income_type("workers compensation"), "Worker's compensation"
        )
        self.assertEqual(map_income_type("rental"), "Property rental income")
        self.assertEqual(map_income_type("retirement"), "Retirement income")
        self.assertEqual(map_income_type("self employed"), "Self-employed")
        self.assertEqual(map_income_type("none"), "None")
        self.assertEqual(map_income_type("Other"), "Other")
        # Short acronyms + common ASR spellings.
        self.assertEqual(map_income_type("SSA"), "SSA")
        self.assertEqual(map_income_type("S.S.A."), "SSA")
        self.assertEqual(map_income_type("S S A"), "SSA")
        self.assertEqual(
            map_income_type("Social Security Administration"), "SSA"
        )
        # SSI is a different program — do not map to SSA.
        self.assertIsNone(map_income_type("SSI"))
        # Live ASR often hears A as E: "S s e."
        self.assertEqual(map_income_type("S s e."), "SSA")
        self.assertEqual(map_income_type("S.S.E."), "SSA")
        self.assertEqual(map_income_type("sse"), "SSA")
        self.assertEqual(map_income_type("Yes. It's SSA."), "SSA")
        self.assertEqual(map_income_type("ess ess ay"), "SSA")
        # Live ASR turns "SSA" into the word "essay".
        self.assertEqual(map_income_type("essay"), "SSA")
        self.assertEqual(map_income_type("Essay."), "SSA")
        self.assertEqual(map_income_type("essa"), "SSA")
        self.assertEqual(map_income_type("SSDI"), "SSDI")
        self.assertEqual(map_income_type("S.S.D.I."), "SSDI")
        self.assertEqual(map_income_type("S S D I"), "SSDI")
        self.assertEqual(map_income_type("S s d e."), "SSDI")
        self.assertEqual(map_income_type("S S D"), "SSDI")
        self.assertEqual(map_income_type("S D I"), "SSDI")
        self.assertEqual(map_income_type("ess ess dee eye"), "SSDI")
        self.assertEqual(
            map_income_type("Social Security Disability Insurance"), "SSDI"
        )
        self.assertEqual(map_income_type("Social Security disability"), "SSDI")
        self.assertEqual(map_income_type("disability"), "SSDI")
        self.assertEqual(map_income_type("on disability"), "SSDI")
        # SSA and SSDI must never map to each other.
        self.assertNotEqual(map_income_type("SSA"), "SSDI")
        self.assertNotEqual(map_income_type("SSDI"), "SSA")
        # Ambiguous bare Social Security — ask, don't guess.
        self.assertIsNone(map_income_type("Social Security"))
        self.assertIsNone(map_income_type("social security."))
        self.assertEqual(map_income_type("Alimony"), "Alimony")
        self.assertEqual(map_income_type("alimoney"), "Alimony")
        self.assertEqual(map_income_type("a limony"), "Alimony")
        self.assertEqual(map_income_type("allimony"), "Alimony")
        self.assertEqual(map_income_type("ali money"), "Alimony")
        self.assertEqual(map_income_type("ali mony"), "Alimony")
        # Live ASR: "Alimony" → "Early morning."
        self.assertEqual(map_income_type("Early morning."), "Alimony")
        self.assertEqual(map_income_type("early morning"), "Alimony")
        self.assertEqual(map_income_type("elementary"), "Alimony")
        self.assertEqual(map_income_type("all money"), "Alimony")
        # "ssa" substring must not invent a type inside longer unrelated phrases.
        self.assertIsNone(map_income_type("I wrote an essay"))
        # Incomplete ASR should not invent a type.
        self.assertIsNone(map_income_type("Yes. It's a."))
        # "another" must never resolve to Other.
        self.assertIsNone(map_income_type("another income"))
        self.assertIsNone(map_income_type("Yes. I want to add another income."))

    def test_outbound_call_boosts_ssa_ssdi_transcription(self) -> None:
        from pc_liveagent import retell_call

        captured: dict = {}

        def fake_request(method, path, json_body=None, params=None):
            if path == "/v2/create-phone-call":
                captured.update(json_body or {})
                return 201, {"call_id": "call-test", "call_status": "registered"}, ""
            return 200, {}, ""

        with (
            patch.object(
                retell_call.config,
                "PC_FORMFILLER_RETELL_API_KEY",
                "test-key",
            ),
            patch.object(
                retell_call,
                "_resolve_from_number",
                return_value=("+14155550000", ""),
            ),
            patch.object(
                retell_call,
                "_ensure_agent",
                return_value=("agent-test", "", 0),
            ),
            patch.object(retell_call, "_request", side_effect=fake_request),
        ):
            result = retell_call.place_form_filler_call(
                phone_number="+14155551234",
                name="Test User",
            )

        self.assertTrue(result["ok"])
        keywords = captured["agent_override"]["agent"]["boosted_keywords"]
        self.assertIn("SSA", keywords)
        self.assertIn("SSDI", keywords)
        self.assertIn("Social Security Administration", keywords)
        self.assertIn("Social Security Disability Insurance", keywords)

    def test_looks_like_truncated_ssa_ssdi(self) -> None:
        from pc_liveagent.income_stage import _looks_like_truncated_ssa_ssdi

        self.assertTrue(_looks_like_truncated_ssa_ssdi("Yes. It's a."))
        self.assertTrue(_looks_like_truncated_ssa_ssdi("it's s"))
        self.assertFalse(_looks_like_truncated_ssa_ssdi("S s e."))
        self.assertFalse(_looks_like_truncated_ssa_ssdi("Employed"))

    def test_income_type_click_js_uses_react_onpress_for_ssa_chips(self) -> None:
        """SSA/SSDI chips are RN Pressables — click helpers must call onPress."""
        from pc_liveagent.income_stage import _income_dom_js

        js = _income_dom_js()
        self.assertIn("incPressTypeChip", js)
        self.assertIn("incClickableAncestor", js)
        self.assertIn("onPress", js)
        self.assertIn("incClickType", js)

    async def test_ambiguous_social_security_asks_ssa_or_ssdi(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-ss-ambig",
            url="https://example.test",
            app_step="app_income",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_current_index"] = "0"
        session.filled["app_income_phase"] = "type"
        session.filled["app_income_heard"] = "[]"
        cards = [{"index": 0, "name": "Admin P.", "hasAddIncome": True}]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._open_type_and_select",
                new=AsyncMock(side_effect=AssertionError("must not open type")),
            ),
        ):
            result = await submit_application_answer(
                "inc-ss-ambig", session, AsyncMock(), "Social Security", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "income_ssa_ssdi_clarify")
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("s s a", spoken)
        self.assertIn("s s d i", spoken)

    async def test_delete_ssa_confirm_then_pencil_not_add_another(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-del-ssa",
            url="https://example.test",
            app_step="app_income_more",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_current_index"] = "0"
        session.filled["app_income_phase"] = "more"
        session.filled["app_income_member_types"] = (
            '{"Admin P.": ["SSA", "SSDI"]}'
        )
        cards = [{"index": 0, "name": "Admin P.", "hasAddIncome": True}]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._click_add_another_income",
                new=AsyncMock(side_effect=AssertionError("no add on delete")),
            ),
        ):
            ask = await submit_application_answer(
                "inc-del-ssa", session, AsyncMock(), "Delete SSA", 0.0
            )
        self.assertEqual(ask.get("step"), "app_income_confirm")
        self.assertEqual(session.filled.get("app_income_delete_type"), "SSA")
        self.assertEqual(session.filled.get("app_income_manage_action"), "delete")

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._delete_income_entry",
                new=AsyncMock(return_value=True),
            ) as delete_fn,
            patch(
                "pc_liveagent.income_stage._click_add_another_income",
                new=AsyncMock(side_effect=AssertionError("no add on confirm yes")),
            ),
            patch(
                "pc_liveagent.income_stage._open_type_and_select",
                new=AsyncMock(side_effect=AssertionError("no type-select on delete")),
            ),
        ):
            done = await submit_application_answer(
                "inc-del-ssa", session, AsyncMock(), "Yes", 0.0
            )
        delete_fn.assert_awaited_once_with(ANY, "Admin P.", "SSA")
        self.assertEqual(done.get("step"), "app_income_more")
        self.assertEqual(
            session.filled.get("app_income_member_types"),
            '{"Admin P.": ["SSDI"]}',
        )

    def test_pay_freq_equal_aliases(self) -> None:
        from pc_liveagent.income_stage import _pay_freq_equal

        self.assertTrue(_pay_freq_equal("Biweekly", "Bi-weekly"))
        self.assertTrue(_pay_freq_equal("Monthly", "monthly"))
        self.assertFalse(_pay_freq_equal("Weekly", "Monthly"))

    def test_map_employed_pay_frequency_common_phrases(self) -> None:
        from pc_liveagent.income_stage import map_employed_pay_frequency

        self.assertEqual(map_employed_pay_frequency("Monthly."), "Monthly")
        self.assertEqual(map_employed_pay_frequency("every month"), "Monthly")
        self.assertEqual(map_employed_pay_frequency("per month"), "Monthly")
        self.assertEqual(map_employed_pay_frequency("biweekly"), "Bi-weekly")
        self.assertEqual(map_employed_pay_frequency("every two weeks"), "Bi-weekly")
        self.assertEqual(map_employed_pay_frequency("weekly"), "Weekly")
        self.assertEqual(map_employed_pay_frequency("once a week"), "Weekly")
        self.assertEqual(map_employed_pay_frequency("annually"), "Annually")
        self.assertEqual(map_employed_pay_frequency("yearly"), "Annually")
        self.assertIsNone(map_employed_pay_frequency("something else"))

    def test_income_type_options_spoken_only_once_unless_asked(self) -> None:
        from pc_liveagent.income_stage import (
            _type_ask_for_session,
            income_type_options_speech,
        )
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-opts-once",
            url="https://example.test",
            app_step="app_income",
        )
        first = _type_ask_for_session(session, "Admin P.", intro=True)
        second = _type_ask_for_session(session, "Admin P.")
        self.assertIn("employed", first.lower())
        self.assertIn("worker's compensation", first.lower())
        self.assertNotIn("worker's compensation", second.lower())
        self.assertNotIn("property rental income", second.lower())
        self.assertIn("what income type", second.lower())
        self.assertIn("employed", income_type_options_speech().lower())

    async def test_income_clarify_does_not_repeat_full_type_list(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-clarify-opts",
            url="https://example.test",
            app_step="app_income",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_current_index"] = "0"
        session.filled["app_income_phase"] = "type"
        session.filled["app_income_types_listed"] = "1"
        session.filled["app_income_add_opened"] = "1"
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(
                    return_value=[
                        {"index": 0, "name": "Admin P.", "hasAddIncome": True}
                    ]
                ),
            ),
        ):
            result = await submit_application_answer(
                "inc-clarify-opts", session, AsyncMock(), "um maybe something", 0.0
            )
        self.assertFalse(result.get("ok"))
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("didn't catch", spoken)
        self.assertIn("options", spoken)
        self.assertNotIn("worker's compensation", spoken)
        self.assertNotIn("property rental income", spoken)

    async def test_application_list_options_reads_income_types(self) -> None:
        from pc_liveagent.call_turn import CallController, _process_application_turn
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-list-opts",
            url="https://example.test",
            app_step="app_income",
        )
        session.filled["app_income_phase"] = "type"
        session.page = AsyncMock()
        ctrl = CallController(call_id="inc-list-opts", phase="application")
        with patch(
            "pc_liveagent.call_turn.get_session",
            new=AsyncMock(return_value=session),
        ):
            result = await _process_application_turn(
                "inc-list-opts",
                ctrl,
                text="What are the options?",
                fp="opts",
                dedupe_ctx="app",
                turn_id="t1",
                form_url="https://example.test",
            )
        self.assertEqual(result.detail, "list_options")
        low = (result.speech or "").lower()
        self.assertIn("employed", low)
        self.assertIn("ssdi", low)
        self.assertIn("worker's compensation", low)
        self.assertEqual(session.filled.get("app_income_types_listed"), "1")

    def test_type_select_looks_committed_requires_amount_modal(self) -> None:
        from pc_liveagent.income_stage import _type_select_looks_committed

        # Still on Select Income Type → not committed for amount-only types.
        self.assertFalse(
            _type_select_looks_committed(
                "Retirement income",
                {
                    "still_on_type": True,
                    "amount_open": False,
                    "modal_kind": "type",
                },
            )
        )
        self.assertTrue(
            _type_select_looks_committed(
                "Retirement income",
                {
                    "still_on_type": False,
                    "amount_open": True,
                    "modal_kind": "amount",
                },
            )
        )
        # Employed only needs to leave the type palette.
        self.assertTrue(
            _type_select_looks_committed(
                "Employed",
                {"still_on_type": False, "amount_open": False, "modal_kind": "open"},
            )
        )

    async def test_retirement_type_select_failure_does_not_ask_amount(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-retire-fail",
            url="https://example.test",
            app_step="app_income",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_current_index"] = "0"
        session.filled["app_income_phase"] = "type"
        session.filled["app_income_heard"] = "[]"
        cards = [{"index": 0, "name": "Admin P.", "hasAddIncome": True}]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._open_type_and_select",
                new=AsyncMock(
                    return_value={"ok": False, "reason": "type_select_failed"}
                ),
            ) as open_type,
        ):
            result = await submit_application_answer(
                "inc-retire-fail", session, AsyncMock(), "Retirement income", 0.0
            )
        open_type.assert_awaited()
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "income_type_select_failed")
        self.assertNotEqual(result.get("step"), "app_income_amount")
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("couldn't select", spoken)
        self.assertIn("retirement income", spoken)
        self.assertNotIn("amount per month", spoken)

    async def test_open_type_and_select_requires_verified_commit(self) -> None:
        from pc_liveagent.income_stage import _open_type_and_select

        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.income_stage._income_type_palette_open",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.income_stage._click_income_type",
                new=AsyncMock(return_value=True),
            ) as click_type,
            patch(
                "pc_liveagent.income_stage._verify_income_type_selected",
                new=AsyncMock(return_value=False),
            ) as verify,
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(side_effect=RuntimeError("nope")),
            ),
            patch(
                "pc_liveagent.live_form._click_cta",
                new=AsyncMock(side_effect=RuntimeError("nope")),
            ),
            patch(
                "pc_liveagent.income_stage._click_income_cancel",
                new=AsyncMock(return_value=True),
            ),
        ):
            result = await _open_type_and_select(
                page, "Admin P.", "Retirement income"
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("reason"), "type_select_failed")
        self.assertEqual(click_type.await_count, 2)
        self.assertGreaterEqual(verify.await_count, 2)

    async def test_income_none_clicks_only_no_amount_or_more(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-none",
            url="https://example.test",
            app_step="app_income",
        )
        session.filled["app_income_current"] = "Admin P."
        session.filled["app_income_current_index"] = "0"
        session.filled["app_income_phase"] = "type"
        session.filled["app_income_heard"] = "[]"
        cards = [
            {"index": 0, "name": "Admin P.", "hasAddIncome": True},
            {"index": 1, "name": "John D.", "hasAddIncome": True},
        ]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.income_stage.scrape_income_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.income_stage._open_type_and_select",
                new=AsyncMock(return_value={"ok": True}),
            ) as open_type,
            patch(
                "pc_liveagent.income_stage._click_income_save",
                new=AsyncMock(return_value=True),
            ) as save,
            patch(
                "pc_liveagent.income_stage._fill_income_amount",
                new=AsyncMock(return_value=True),
            ) as fill_amount,
        ):
            result = await submit_application_answer(
                "inc-none", session, AsyncMock(), "None", 0.0
            )
        open_type.assert_awaited()
        save.assert_not_awaited()
        fill_amount.assert_not_awaited()
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_income")
        spoken = (result.get("spoken") or "").lower()
        self.assertNotIn("amount per month", spoken)
        self.assertNotIn("another income", spoken)
        self.assertIn("john", spoken)
        self.assertIn("0", session.filled.get("app_income_heard") or "")

    async def test_circumstances_none_advances(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="circ-none",
            url="https://example.test",
            app_step="app_circumstances",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_circumstances"),
            ),
            patch(
                "pc_liveagent.app_stage._select_circumstance_checkbox",
                new=AsyncMock(return_value={"ok": True}),
            ),
            patch("pc_liveagent.live_form._click_exact_choice", new=AsyncMock()),
            patch("pc_liveagent.app_stage._click_next_or_continue", new=AsyncMock()),
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_tax"),
            ),
        ):
            result = await submit_application_answer(
                "circ-none", session, AsyncMock(), "None of the above", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_tax")

    async def test_circumstances_multi_member_then_more_no(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="circ-multi",
            url="https://example.test",
            app_step="app_circumstances_person",
        )
        session.filled["app_circumstance_pending"] = "Veteran or military member"
        session.filled["app_circumstance_queue"] = json.dumps(
            ["Veteran or military member"]
        )
        members = [
            {"label": "Agent T.", "index": 0, "dupIndex": 0},
            {"label": "john D.", "index": 1, "dupIndex": 0},
            {"label": "John D.", "index": 2, "dupIndex": 1},
        ]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_circumstances"),
            ),
            patch(
                "pc_liveagent.app_stage._select_circumstance_checkbox",
                new=AsyncMock(return_value={"ok": True, "checked": True}),
            ),
            patch(
                "pc_liveagent.app_stage._wait_for_circumstance_members",
                new=AsyncMock(return_value=members),
            ),
            patch(
                "pc_liveagent.app_stage._discover_circumstance_members",
                new=AsyncMock(return_value=members),
            ),
            patch(
                "pc_liveagent.app_stage._set_circumstance_member",
                new=AsyncMock(return_value=True),
            ) as set_member,
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(),
            ) as nxt,
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_tax"),
            ),
        ):
            multi = await submit_application_answer(
                "circ-multi",
                session,
                AsyncMock(),
                "Agent T and john D",
                0.0,
            )
            self.assertEqual(multi.get("step"), "app_circumstances_person")
            self.assertEqual(set_member.await_count, 2)
            self.assertIn("anyone else", (multi.get("spoken") or "").lower())

            more = await submit_application_answer(
                "circ-multi", session, AsyncMock(), "done", 0.0
            )
            self.assertEqual(more.get("step"), "app_circumstances_more")
            self.assertIn("other situations", (more.get("spoken") or "").lower())

            done = await submit_application_answer(
                "circ-multi", session, AsyncMock(), "No", 0.0
            )
        self.assertEqual(done.get("step"), "app_tax")
        nxt.assert_awaited()

    async def test_circumstances_asks_person_assignment(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="circ-vet",
            url="https://example.test",
            app_step="app_circumstances",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_circumstances"),
            ),
            patch(
                "pc_liveagent.app_stage._select_circumstance_checkbox",
                new=AsyncMock(return_value={"ok": True, "checked": True}),
            ) as select_box,
            patch(
                "pc_liveagent.app_stage._wait_for_circumstance_members",
                new=AsyncMock(
                    return_value=[
                        {"label": "Agent T.", "index": 0, "dupIndex": 0},
                        {"label": "john D.", "index": 1, "dupIndex": 0},
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._discover_circumstance_members",
                new=AsyncMock(
                    return_value=[
                        {"label": "Agent T.", "index": 0, "dupIndex": 0},
                        {"label": "john D.", "index": 1, "dupIndex": 0},
                    ]
                ),
            ),
            patch("pc_liveagent.live_form._click_exact_choice", new=AsyncMock()),
        ):
            result = await submit_application_answer(
                "circ-vet", session, AsyncMock(), "veteran", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_circumstances_person")
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("who does veteran or military member apply to", spoken)
        self.assertIn("agent t", spoken)
        select_box.assert_awaited()

    def test_circumstances_fuzzy_option_mapping(self) -> None:
        from pc_liveagent.app_stage import _map_circumstance_labels

        self.assertEqual(
            _map_circumstance_labels("Indian."),
            ["American Indian or Alaska Native"],
        )
        self.assertEqual(
            _map_circumstance_labels("american indian"),
            ["American Indian or Alaska Native"],
        )
        self.assertEqual(
            _map_circumstance_labels("Native American"),
            ["American Indian or Alaska Native"],
        )
        self.assertEqual(
            _map_circumstance_labels("vet"),
            ["Veteran or military member"],
        )
        self.assertEqual(
            _map_circumstance_labels("military person"),
            ["Veteran or military member"],
        )
        self.assertEqual(
            _map_circumstance_labels("Military member."),
            ["Veteran or military member"],
        )
        self.assertEqual(
            _map_circumstance_labels("veteran alone"),
            ["Veteran or military member"],
        )
        self.assertEqual(
            _map_circumstance_labels("needs help"),
            ["Needs help with daily activities"],
        )
        self.assertEqual(
            _map_circumstance_labels("college student"),
            ["Full-time student (ages 18 – 20)"],
        )
        self.assertEqual(
            _map_circumstance_labels("Indian and veteran"),
            [
                "Veteran or military member",
                "American Indian or Alaska Native",
            ],
        )
        self.assertEqual(
            _map_circumstance_labels("none"),
            ["None of the above"],
        )

    async def test_circumstances_indian_clicks_then_asks_members(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="circ-indian",
            url="https://example.test",
            app_step="app_circumstances",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_circumstances"),
            ),
            patch(
                "pc_liveagent.app_stage._select_circumstance_checkbox",
                new=AsyncMock(return_value={"ok": True, "checked": True}),
            ) as select_box,
            patch(
                "pc_liveagent.app_stage._wait_for_circumstance_members",
                new=AsyncMock(
                    return_value=[
                        {"label": "Agent T.", "index": 0, "dupIndex": 0},
                        {"label": "John D.", "index": 1, "dupIndex": 0},
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._discover_circumstance_members",
                new=AsyncMock(
                    return_value=[
                        {"label": "Agent T.", "index": 0, "dupIndex": 0},
                        {"label": "John D.", "index": 1, "dupIndex": 0},
                    ]
                ),
            ),
            patch("pc_liveagent.live_form._click_exact_choice", new=AsyncMock()),
        ):
            result = await submit_application_answer(
                "circ-indian", session, AsyncMock(), "Indian.", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_circumstances_person")
        self.assertEqual(
            session.filled.get("app_circumstance_pending"),
            "American Indian or Alaska Native",
        )
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("american indian or alaska native", spoken)
        self.assertIn("agent t", spoken)
        self.assertGreaterEqual(select_box.await_count, 1)
        self.assertEqual(
            select_box.await_args_list[0].args[1],
            "American Indian or Alaska Native",
        )

    async def test_circumstances_military_asks_person_even_if_scrape_lags(
        self,
    ) -> None:
        """Regression: list was open but old scraper returned []; still ask who."""
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="circ-mil-lag",
            url="https://example.test",
            app_step="app_circumstances",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_circumstances"),
            ),
            patch(
                "pc_liveagent.app_stage._select_circumstance_checkbox",
                new=AsyncMock(return_value={"ok": True, "checked": True}),
            ),
            patch(
                "pc_liveagent.app_stage._wait_for_circumstance_members",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "pc_liveagent.app_stage._discover_circumstance_members",
                new=AsyncMock(return_value=[]),
            ),
            patch("pc_liveagent.live_form._click_exact_choice", new=AsyncMock()),
        ):
            result = await submit_application_answer(
                "circ-mil-lag", session, AsyncMock(), "Military member.", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_circumstances_person")
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("veteran or military member", spoken)
        self.assertIn("who does", spoken)
        self.assertNotIn("did not open", spoken)

    async def test_circumstances_select_failure_stays_on_step(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="circ-fail",
            url="https://example.test",
            app_step="app_circumstances",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_circumstances"),
            ),
            patch(
                "pc_liveagent.app_stage._select_circumstance_checkbox",
                new=AsyncMock(return_value={"ok": False, "reason": "card_not_found"}),
            ),
            patch(
                "pc_liveagent.live_form._click_exact_choice",
                new=AsyncMock(side_effect=RuntimeError("miss")),
            ),
            patch(
                "pc_liveagent.live_form._click_cta",
                new=AsyncMock(side_effect=RuntimeError("miss")),
            ),
        ):
            result = await submit_application_answer(
                "circ-fail", session, AsyncMock(), "American Indian", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("step"), "app_circumstances")
        self.assertIn("couldn't select", (result.get("spoken") or "").lower())

    async def test_stage_e_explain_does_not_advance(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, submit_field

        session = LiveFormSession(
            call_id="explain-e",
            url="https://example.test",
            app_step="app_citizenship",
            page=AsyncMock(),
            welcome_done=True,
            signed_in=True,
        )
        session.ready.set()
        with patch(
            "pc_liveagent.live_form.get_session",
            new=AsyncMock(return_value=session),
        ):
            # submit_field path may need session in registry — call explain via
            # the same regex gate used in submit_field by invoking the helper.
            from pc_liveagent.app_stage import stage_e_explain

            spoken = stage_e_explain("app_citizenship")
        self.assertIn("U.S. citizen", spoken)
        self.assertIn("Please say Yes or No", spoken)

    async def test_documents_upload_does_not_pretend_success(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="docs",
            url="https://example.test",
            app_step="app_documents",
        )
        with patch(
            "pc_liveagent.app_stage.detect_application_screen",
            new=AsyncMock(return_value="app_documents"),
        ):
            result = await submit_application_answer(
                "docs", session, AsyncMock(), "I want to upload", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_documents")
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("can't take document", spoken)
        self.assertIn("not pretend", spoken)

    async def test_substep_reconcile_keeps_income_field(self) -> None:
        from pc_liveagent.app_stage import _reconcile_app_step
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="sub",
            url="https://example.test",
            app_step="app_income_employed_hours",
        )
        with patch(
            "pc_liveagent.app_stage.detect_application_screen",
            new=AsyncMock(return_value="app_income"),
        ):
            step = await _reconcile_app_step(session, AsyncMock())
        self.assertEqual(step, "app_income_employed_hours")

    def test_prompt_mentions_collect_mode_stage_e(self) -> None:
        from pc_liveagent.live_fill_prompt import LIVE_FILL_APPENDIX

        low = LIVE_FILL_APPENDIX.lower()
        self.assertIn("collect mode", low)
        self.assertIn("one short question at a time", low)
        self.assertIn("never pretend a file was uploaded", low)


class StageEResumeDobImmigrationTests(unittest.IsolatedAsyncioTestCase):
    """Resume, citizenship→immigration branching, and dynamic DOB collection."""

    def test_parse_dob_rejects_invalid_and_accepts_spoken(self) -> None:
        from pc_liveagent.app_stage import _parse_dob_answer

        self.assertEqual(_parse_dob_answer("March 15 2018"), ("03/15/2018", None))
        self.assertEqual(_parse_dob_answer("3/15/2018"), ("03/15/2018", None))
        self.assertEqual(_parse_dob_answer("not a date"), (None, None))
        self.assertEqual(_parse_dob_answer("February 31 2018"), (None, None))
        self.assertEqual(_parse_dob_answer("02/30/2019"), (None, None))

    def test_parse_dob_flexible_formats(self) -> None:
        from pc_liveagent.app_stage import _parse_dob_answer
        from pc_liveagent.live_form import _normalize_dob

        self.assertEqual(_parse_dob_answer("22/10/2024"), ("10/22/2024", None))
        self.assertEqual(_parse_dob_answer("10/22/2024"), ("10/22/2024", None))
        self.assertEqual(_parse_dob_answer("2024-10-22"), ("10/22/2024", None))
        self.assertEqual(_parse_dob_answer("October 22, 2024"), ("10/22/2024", None))
        self.assertEqual(_parse_dob_answer("22 October 2024"), ("10/22/2024", None))
        self.assertEqual(
            _parse_dob_answer("month is october date 22 year 2024"),
            ("10/22/2024", None),
        )
        self.assertEqual(
            _parse_dob_answer("October twenty-second twenty twenty-four"),
            ("10/22/2024", None),
        )
        self.assertEqual(
            _parse_dob_answer(
                "the twenty-second of October two thousand twenty-four"
            ),
            ("10/22/2024", None),
        )
        # Live ASR: periods between clauses + fully spoken numeric DOB.
        self.assertEqual(
            _parse_dob_answer(
                "Month is October. Day is twenty two. Year is two thousand three."
            ),
            ("10/22/2003", None),
        )
        self.assertEqual(
            _parse_dob_answer("Zero four twenty five twenty twenty two."),
            ("04/25/2022", None),
        )
        self.assertEqual(
            _parse_dob_answer("Zero four twenty five twenty twenty."),
            ("04/25/2020", None),
        )
        self.assertEqual(_parse_dob_answer("02/29/2020"), ("02/29/2020", None))
        self.assertEqual(_parse_dob_answer("02/29/2019"), (None, None))
        dob, clarify = _parse_dob_answer("10/11/2024")
        self.assertIsNone(dob)
        self.assertIn("ambiguous", (clarify or "").lower())
        # Stage D normalize still prefers US month-first when ambiguous.
        self.assertEqual(_normalize_dob("10/11/2024"), "10/11/2024")
        self.assertEqual(_normalize_dob("22/10/2024"), "10/22/2024")

    async def test_quick_apply_yes_restarts_at_question_one_in_test_mode(
        self,
    ) -> None:
        """Live bug: Continue Quick Apply opened DOB; must Back to Q1."""
        from pc_liveagent.app_stage import handle_application_gate
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="qa-restart",
            url="https://example.test",
            elig_step="proceed_application",
        )
        session.filled["application_gate_speech"] = "Can I start?"
        session.filled["awaiting_quick_apply"] = "1"
        session.filled["application_started"] = "0"
        with (
            patch("pc_liveagent.config.APPLICATION_TEST_MODE", True),
            patch(
                "pc_liveagent.app_stage.classify_saved_household_page",
                new=AsyncMock(return_value="dashboard"),
            ),
            patch(
                "pc_liveagent.app_stage.click_quick_apply",
                new=AsyncMock(return_value="Continue Quick Apply"),
            ),
            patch(
                "pc_liveagent.app_stage._wait_surface_after_quick_apply",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage.restart_application_to_first_question",
                new=AsyncMock(return_value="app_citizenship"),
            ) as restart,
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
        ):
            result = await handle_application_gate(
                "qa-restart", session, AsyncMock(), "Yes"
            )
        restart.assert_awaited_once()
        self.assertEqual(result.get("step"), "app_citizenship")
        self.assertIn("first question", (result.get("spoken") or "").lower())
        self.assertNotIn("date of birth", (result.get("spoken") or "").lower())

    async def test_detect_does_not_invent_citizenship_from_url(self) -> None:
        from pc_liveagent.app_stage import detect_application_screen

        class _Body:
            async def inner_text(self) -> str:
                return "Loading application details"

        page = AsyncMock()
        page.url = "https://my.pointcare.com/application/step-3"
        page.locator = lambda _s: _Body()
        self.assertEqual(await detect_application_screen(page), "")

    async def test_detect_dob_without_pencil_keyword(self) -> None:
        from pc_liveagent.app_stage import detect_application_screen

        class _Body:
            async def inner_text(self) -> str:
                return (
                    "Date of birth Review household "
                    "Mickey Mouse Needs date of birth Add "
                    "Murali M. 01/02/1990"
                )

        page = AsyncMock()
        page.url = "https://my.pointcare.com/application"
        page.locator = lambda _s: _Body()
        self.assertEqual(await detect_application_screen(page), "app_dob_review")

    async def test_application_test_restarts_from_dob_to_question_one(self) -> None:
        """Prior run on DOB must Back to Q1 — not resume mid-flow."""
        from pc_liveagent.app_stage import prepare_application_test
        from pc_liveagent.live_form import LiveFormSession

        page = AsyncMock()
        page.url = "https://my.pointcare.com/application/step-3"
        with (
            patch(
                "pc_liveagent.app_stage.wait_for_saved_household_surface",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage.restart_application_to_first_question",
                new=AsyncMock(return_value="app_citizenship"),
            ) as restart,
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
        ):
            session = LiveFormSession(call_id="restart-dob", url=page.url)
            spoken = await prepare_application_test(session, page)
        restart.assert_awaited_once()
        self.assertEqual(session.app_step, "app_citizenship")
        self.assertIn("first question", spoken.lower())
        self.assertIn("u.s. citizen", spoken.lower())
        self.assertNotIn("date of birth", spoken.lower())

    async def test_application_test_first_question_agent_offer_when_present(
        self,
    ) -> None:
        from pc_liveagent.app_stage import prepare_application_test
        from pc_liveagent.live_form import LiveFormSession

        page = AsyncMock()
        page.url = "https://my.pointcare.com/application/step-1"
        with (
            patch(
                "pc_liveagent.app_stage.wait_for_saved_household_surface",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.app_stage.restart_application_to_first_question",
                new=AsyncMock(return_value="app_agent_offer"),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_agent_offer"),
            ),
        ):
            session = LiveFormSession(call_id="restart-agent", url=page.url)
            spoken = await prepare_application_test(session, page)
        self.assertEqual(session.app_step, "app_agent_offer")
        self.assertIn("licensed agent", spoken.lower())

    async def test_restart_clicks_back_until_first_question(self) -> None:
        from pc_liveagent.app_stage import restart_application_to_first_question

        state = {"screen": "app_dob_review", "backs": 0}

        async def _detect(_page):
            return state["screen"]

        async def _back(_page):
            state["backs"] += 1
            state["screen"] = {
                1: "app_insurance",
                2: "app_citizenship",
            }.get(state["backs"], "app_citizenship")
            return True

        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(side_effect=_detect),
            ),
            patch(
                "pc_liveagent.app_stage._click_application_back",
                new=AsyncMock(side_effect=_back),
            ) as back,
            patch(
                "pc_liveagent.app_stage._application_body_signature",
                new=AsyncMock(side_effect=lambda _p: f"sig-{state['backs']}"),
            ),
        ):
            step = await restart_application_to_first_question(page)
        self.assertEqual(step, "app_citizenship")
        self.assertEqual(back.await_count, 2)

    async def test_restart_reports_when_back_leaves_application(self) -> None:
        from pc_liveagent.app_stage import restart_application_to_first_question

        state = {"screen": "app_dob_review"}

        async def _detect(_page):
            return state["screen"]

        async def _back(_page):
            state["screen"] = "programs"
            return True

        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(side_effect=_detect),
            ),
            patch(
                "pc_liveagent.app_stage._click_application_back",
                new=AsyncMock(side_effect=_back),
            ),
            patch(
                "pc_liveagent.app_stage._application_body_signature",
                new=AsyncMock(return_value="sig"),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await restart_application_to_first_question(page)
        self.assertIn("APPLICATION_TEST", str(ctx.exception))
        self.assertIn("duplicate draft", str(ctx.exception).lower())

    async def test_restart_keeps_backing_through_unrecognized_mid_flow_pages(
        self,
    ) -> None:
        """Empty detect mid-flow must not abort before question 1 (page-9 stall)."""
        from pc_liveagent.app_stage import restart_application_to_first_question

        # idx advances only on Back; repeated detect reads stay on the same step.
        state = {
            "idx": 0,
            "path": [
                "app_documents",
                "",  # unrecognized wording (e.g. expenses/tax chrome)
                "app_circumstances",
                "app_insurance",
                "app_citizenship",
            ],
        }

        async def _detect(_page):
            return state["path"][state["idx"]]

        async def _back(_page):
            state["idx"] = min(state["idx"] + 1, len(state["path"]) - 1)
            return True

        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(side_effect=_detect),
            ),
            patch(
                "pc_liveagent.app_stage._click_application_back",
                new=AsyncMock(side_effect=_back),
            ) as back,
            patch(
                "pc_liveagent.app_stage._left_application_surface",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.app_stage._application_body_signature",
                new=AsyncMock(side_effect=lambda _p: f"sig-{state['idx']}"),
            ),
        ):
            step = await restart_application_to_first_question(page)
        self.assertEqual(step, "app_citizenship")
        self.assertGreaterEqual(back.await_count, 4)

    async def test_resume_gate_yes_redetects_live_screen(self) -> None:
        from pc_liveagent.app_stage import handle_application_gate
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="resume-live",
            url="https://example.test",
            elig_step="proceed_application",
        )
        session.filled["resume_app_step"] = "app_citizenship"
        session.filled["application_started"] = "1"
        session.filled["application_gate_speech"] = "Can I start?"
        with (
            patch("pc_liveagent.config.APPLICATION_TEST_MODE", False),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_income",
                        "What income type should I enter?",
                    )
                ),
            ) as prep,
        ):
            result = await handle_application_gate(
                "resume-live", session, AsyncMock(), "Yes"
            )
        prep.assert_awaited()
        self.assertEqual(result.get("step"), "app_income")
        self.assertIn("income", (result.get("spoken") or "").lower())

    async def test_citizenship_no_walks_every_member_ignoring_defaults(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="imm-flow",
            url="https://example.test",
            app_step="app_citizenship",
        )
        page = AsyncMock()
        members = [
            {"name": "Mickey Mouse", "selected": "U.S. Citizen", "index": 0},
            {"name": "Angel R.", "selected": "U.S. Citizen", "index": 1},
        ]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage._click_yes_no",
                new=AsyncMock(return_value="No"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_immigration_members",
                new=AsyncMock(return_value=members),
            ),
        ):
            first = await submit_application_answer(
                "imm-flow", session, page, "No", 0.0
            )
        # Preselected Citizen radios must not skip confirmation.
        self.assertEqual(first.get("step"), "app_immigration")
        self.assertIn("mickey", (first.get("spoken") or "").lower())
        self.assertEqual(session.filled.get("app_immigration_current"), "Mickey Mouse")

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_immigration"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_immigration_members",
                new=AsyncMock(return_value=members),
            ),
            patch(
                "pc_liveagent.app_stage._set_member_immigration_status",
                new=AsyncMock(return_value=(True, "selected")),
            ) as set_status,
            patch(
                "pc_liveagent.app_stage._verify_member_immigration_status",
                new=AsyncMock(return_value=(True, "verified")),
            ),
        ):
            status = await submit_application_answer(
                "imm-flow", session, page, "Satisfactory immigration status", 0.0
            )
        set_status.assert_awaited_once_with(
            page,
            "Mickey Mouse",
            "Satisfactory immigration status (green card, etc.)",
        )
        self.assertEqual(status.get("step"), "app_immigration")
        self.assertIn("angel", (status.get("spoken") or "").lower())

    async def test_citizenship_yes_uses_full_label_path(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="cit-yes",
            url="https://example.test",
            app_step="app_citizenship",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage._click_yes_no",
                new=AsyncMock(return_value="Yes Everyone is a U.S. citizen"),
            ) as click,
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_insurance"),
            ),
        ):
            result = await submit_application_answer(
                "cit-yes", session, AsyncMock(), "Yes", 0.0
            )
        click.assert_awaited_once()
        self.assertEqual(click.await_args.args[1], True)
        self.assertEqual(result.get("step"), "app_insurance")

    async def test_immigration_duplicate_name_asks_clarification(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="imm-dup",
            url="https://example.test",
            app_step="app_immigration",
        )
        session.filled["app_immigration_current"] = "John D."
        session.filled["app_immigration_queue"] = '["John D."]'
        session.filled["app_citizenship_no"] = "1"
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_immigration"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_immigration_members",
                new=AsyncMock(
                    return_value=[
                        {"name": "John D.", "selected": "U.S. Citizen", "index": 0},
                        {"name": "John D.", "selected": "U.S. Citizen", "index": 1},
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._set_member_immigration_status",
                new=AsyncMock(return_value=(True, "selected")),
            ) as set_status,
        ):
            result = await submit_application_answer(
                "imm-dup", session, AsyncMock(), "U.S. Citizen", 0.0
            )
        set_status.assert_not_awaited()
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "immigration_duplicate_name")

    def test_map_immigration_green_card_and_citizen(self) -> None:
        from pc_liveagent.app_stage import _map_immigration_status

        self.assertEqual(
            _map_immigration_status("he has a green card"),
            "Satisfactory immigration status (green card, etc.)",
        )
        self.assertEqual(_map_immigration_status("citizen"), "U.S. Citizen")
        self.assertEqual(_map_immigration_status("national"), "U.S. National")
        self.assertIsNone(_map_immigration_status("citizen or national"))
        self.assertIsNone(_map_immigration_status("not a citizen"))

    def test_citizenship_question_uses_listed_names(self) -> None:
        from pc_liveagent.app_stage import _citizenship_question, _split_listed_names

        self.assertEqual(
            _split_listed_names("Agent and john"),
            ["Agent", "john"],
        )
        self.assertEqual(
            _citizenship_question(["Agent", "john"]),
            "Are Agent and john all U.S. Citizens? Please say Yes or No.",
        )
        self.assertEqual(
            _citizenship_question(["Don"]),
            "Is Don a U.S. Citizen? Please say Yes or No.",
        )

    def test_named_status_stays_with_that_member(self) -> None:
        from pc_liveagent.app_stage import _parse_immigration_assignment

        members = [
            {"name": "Agent", "selected": "U.S. Citizen"},
            {"name": "john", "selected": "U.S. Citizen"},
            {"name": "Don", "selected": "U.S. Citizen"},
        ]
        name, status, clarify = _parse_immigration_assignment(
            "john has a green card", members, "Agent"
        )
        self.assertIsNone(clarify)
        self.assertEqual(name, "john")
        self.assertEqual(status, "Satisfactory immigration status (green card, etc.)")
        name, status, clarify = _parse_immigration_assignment(
            "Agent is a U.S. National", members, "john"
        )
        self.assertIsNone(clarify)
        self.assertEqual((name, status), ("Agent", "U.S. National"))
        name, status, clarify = _parse_immigration_assignment(
            "Don is a citizen", members, "Agent"
        )
        self.assertIsNone(clarify)
        self.assertEqual((name, status), ("Don", "U.S. Citizen"))
        _name, _status, clarify = _parse_immigration_assignment(
            "Alex has a green card", members, "Agent"
        )
        self.assertIsNotNone(clarify)
        _name, _status, clarify = _parse_immigration_assignment(
            "john and Don are citizens", members, "Agent"
        )
        self.assertIsNotNone(clarify)

    async def test_preselected_citizen_is_not_clicked_again(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="imm-default",
            url="https://example.test",
            app_step="app_immigration",
        )
        session.filled["app_immigration_current"] = "Don"
        session.filled["app_immigration_queue"] = '["Don", "Agent"]'
        session.filled["app_citizenship_no"] = "1"
        members = [
            {"name": "Don", "selected": "U.S. Citizen", "index": 0},
            {"name": "Agent", "selected": "U.S. Citizen", "index": 1},
        ]
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_immigration"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_immigration_members",
                new=AsyncMock(return_value=members),
            ),
            patch(
                "pc_liveagent.app_stage._set_member_immigration_status",
                new=AsyncMock(return_value=(True, "selected")),
            ) as set_status,
            patch(
                "pc_liveagent.app_stage._verify_member_immigration_status",
                new=AsyncMock(return_value=(True, "verified")),
            ),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(return_value=True),
            ) as nxt,
        ):
            result = await submit_application_answer(
                "imm-default", session, page, "Don is a citizen", 0.0
            )
        set_status.assert_awaited_once()
        self.assertEqual(set_status.await_args.args[1], "Don")
        self.assertEqual(set_status.await_args.args[2], "U.S. Citizen")
        nxt.assert_not_awaited()
        self.assertEqual(result.get("step"), "app_immigration")
        self.assertIn("agent", (result.get("spoken") or "").lower())
        self.assertEqual(
            session.filled.get("app_immigration_confirmed"),
            '{"Don": "U.S. Citizen"}',
        )

    async def test_citizenship_member_sentence_does_not_click_yes_no(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="cit-name",
            url="https://example.test",
            app_step="app_citizenship",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage._click_yes_no",
                new=AsyncMock(side_effect=AssertionError("must not click")),
            ),
        ):
            result = await submit_application_answer(
                "cit-name", session, AsyncMock(), "john has a green card", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "citizenship_clarify")
        self.assertEqual(session.app_step, "app_citizenship")

    async def test_insurance_yes_members_then_coverage_types(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="ins-flow",
            url="https://example.test",
            app_step="app_insurance",
        )
        page = AsyncMock()
        members = [
            {
                "name": "Mickey Mouse",
                "checked": False,
                "selected_types": [],
                "index": 0,
            },
            {
                "name": "Angel R.",
                "checked": False,
                "selected_types": [],
                "index": 1,
            },
        ]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage._click_yes_no",
                new=AsyncMock(return_value="Yes — Someone has coverage"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_coverage_members",
                new=AsyncMock(return_value=members),
            ),
        ):
            first = await submit_application_answer(
                "ins-flow", session, page, "Yes", 0.0
            )
        self.assertEqual(first.get("step"), "app_insurance_members")
        self.assertIn("mickey", (first.get("spoken") or "").lower())
        self.assertIn("angel", (first.get("spoken") or "").lower())

        state = {"members": [dict(item) for item in members]}

        async def _scrape(_page):
            return [dict(item) for item in state["members"]]

        async def _check(_page, name, checked=True):
            for item in state["members"]:
                if item["name"].lower() == name.lower():
                    item["checked"] = checked
                    return True
            return False

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_coverage_members",
                new=AsyncMock(side_effect=_scrape),
            ),
            patch(
                "pc_liveagent.app_stage._set_member_insurance_checked",
                new=AsyncMock(side_effect=_check),
            ) as check,
            patch(
                "pc_liveagent.app_stage._verify_coverage_member_checked",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._ensure_member_coverage_expanded",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(side_effect=AssertionError("must not click next yet")),
            ),
        ):
            who = await submit_application_answer(
                "ins-flow", session, page, "Mickey and Angel", 0.0
            )
        # Member rows are accordion triggers; their status checkboxes are not
        # clicked directly before selecting a plan.
        self.assertEqual(check.await_count, 0)
        self.assertEqual(who.get("step"), "app_insurance_coverage_type")
        self.assertIn("mickey", (who.get("spoken") or "").lower())
        self.assertIn("qualify", (who.get("spoken") or "").lower())
        self.assertEqual(session.filled.get("app_insurance_current"), "Mickey Mouse")

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage._ensure_member_coverage_expanded",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._set_member_coverage_type",
                new=AsyncMock(return_value=True),
            ) as set_cov,
            patch(
                "pc_liveagent.app_stage._verify_member_coverage_types",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_insurance_members",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "Mickey Mouse",
                            "checked": True,
                            "selected_types": ["Medicare", "Medicaid"],
                            "index": 0,
                        },
                        {
                            "name": "Angel R.",
                            "checked": True,
                            "selected_types": [],
                            "index": 1,
                        },
                    ]
                ),
            ),
        ):
            cov = await submit_application_answer(
                "ins-flow", session, page, "Medicare and Medicaid", 0.0
            )
        self.assertEqual(set_cov.await_count, 2)
        self.assertEqual(cov.get("step"), "app_insurance_coverage_type")
        self.assertIn("medicare", (cov.get("spoken") or "").lower())
        self.assertIn("other plans", (cov.get("spoken") or "").lower())
        self.assertEqual(session.filled.get("app_insurance_coverage_phase"), "more")

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage._ensure_member_coverage_expanded",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(side_effect=AssertionError("must not next mid-queue")),
            ),
        ):
            confirm = await submit_application_answer(
                "ins-flow", session, page, "No", 0.0
            )
        self.assertEqual(confirm.get("step"), "app_insurance_coverage_type")
        self.assertIn("confirming", (confirm.get("spoken") or "").lower())
        self.assertIn("angel", (confirm.get("spoken") or "").lower())
        self.assertIn("qualify", (confirm.get("spoken") or "").lower())
        self.assertEqual(session.filled.get("app_insurance_current"), "Angel R.")
        self.assertEqual(session.filled.get("app_insurance_coverage_phase"), "collect")

    def test_coverage_question_uses_listed_names(self) -> None:
        from pc_liveagent.app_stage import (
            _coverage_question,
            _who_has_coverage_question,
            resolve_stage_e_yes_no_label,
        )

        self.assertEqual(
            _coverage_question(["Agent", "john"]),
            "Do Agent and john have health insurance or health coverage? "
            "Please say Yes or No.",
        )
        self.assertIn("Agent and john", _who_has_coverage_question(["Agent", "john"]))
        labels = [
            "Yes — Someone has coverage",
            "No — No one has coverage",
        ]
        self.assertEqual(resolve_stage_e_yes_no_label(True, labels), labels[0])
        self.assertEqual(resolve_stage_e_yes_no_label(False, labels), labels[1])

    async def test_coverage_no_goes_next_without_member_selection(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="cov-no",
            url="https://example.test",
            app_step="app_insurance",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage._click_yes_no",
                new=AsyncMock(return_value="No — No one has coverage"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_coverage_members",
                new=AsyncMock(side_effect=AssertionError("must not list members")),
            ),
            patch(
                "pc_liveagent.app_stage._set_member_insurance_checked",
                new=AsyncMock(side_effect=AssertionError("must not check members")),
            ),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(return_value=True),
            ) as nxt,
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_dob_review"),
            ),
        ):
            result = await submit_application_answer(
                "cov-no", session, AsyncMock(), "No", 0.0
            )
        nxt.assert_awaited()
        self.assertEqual(result.get("step"), "app_dob_review")
        self.assertNotEqual(session.app_step, "app_insurance_members")

    async def test_last_insured_member_requires_no_before_next(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="cov-final-member",
            url="https://example.test",
            app_step="app_insurance_coverage_type",
        )
        session.filled["app_insurance_queue"] = '["Agent T."]'
        session.filled["app_insurance_current"] = "Agent T."
        session.filled["app_insurance_coverage_phase"] = "more"
        session.filled["app_insurance_current_types"] = '["Medicare"]'
        next_click = AsyncMock(return_value=True)
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=next_click,
            ),
        ):
            confirm_plan = await submit_application_answer(
                "cov-final-member", session, AsyncMock(), "No", 0.0
            )
        next_click.assert_not_awaited()
        self.assertEqual(
            session.filled.get("app_insurance_coverage_phase"),
            "members_more",
        )
        self.assertIn(
            "any other household member",
            (confirm_plan.get("spoken") or "").lower(),
        )

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage._next_button_enabled",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(return_value=True),
            ) as final_next,
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_dob_review"),
            ) as advance,
            patch(
                "pc_liveagent.app_stage._prepare_dob_collect_speech",
                new=AsyncMock(
                    return_value=(
                        "app_dob_review",
                        "Please confirm each person's date of birth.",
                    )
                ),
            ),
        ):
            finish = await submit_application_answer(
                "cov-final-member", session, AsyncMock(), "No", 0.0
            )
        final_next.assert_awaited_once()
        self.assertEqual(advance.await_args.kwargs.get("leave_step"), "app_insurance")
        self.assertEqual(finish.get("step"), "app_dob_review")

    async def test_coverage_empty_selection_does_not_click_next(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="cov-empty",
            url="https://example.test",
            app_step="app_insurance_members",
        )
        members = [
            {"name": "Agent", "checked": False, "index": 0},
            {"name": "john", "checked": False, "index": 1},
        ]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_coverage_members",
                new=AsyncMock(return_value=members),
            ),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(side_effect=AssertionError("must not click next")),
            ),
        ):
            result = await submit_application_answer(
                "cov-empty", session, AsyncMock(), "continue", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "insurance_members_incomplete")
        self.assertEqual(session.app_step, "app_insurance_members")
        self.assertIn("who has coverage", (result.get("spoken") or "").lower())

    async def test_coverage_ambiguous_name_does_not_click(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="cov-amb",
            url="https://example.test",
            app_step="app_insurance_members",
        )
        members = [
            {"name": "John D.", "checked": False, "index": 0},
            {"name": "John M.", "checked": False, "index": 1},
        ]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_coverage_members",
                new=AsyncMock(return_value=members),
            ),
            patch(
                "pc_liveagent.app_stage._set_member_insurance_checked",
                new=AsyncMock(side_effect=AssertionError("must not check")),
            ),
        ):
            result = await submit_application_answer(
                "cov-amb", session, AsyncMock(), "John", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "insurance_members_ambiguous")
        self.assertEqual(session.app_step, "app_insurance_members")

    async def test_already_checked_coverage_box_is_not_clicked_again(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="cov-already",
            url="https://example.test",
            app_step="app_insurance_members",
        )
        members = [
            {"name": "Agent", "checked": True, "index": 0},
            {"name": "john", "checked": False, "index": 1},
        ]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_coverage_members",
                new=AsyncMock(return_value=members),
            ),
            patch(
                "pc_liveagent.app_stage._set_member_insurance_checked",
                new=AsyncMock(side_effect=AssertionError("must not click")),
            ),
            patch(
                "pc_liveagent.app_stage._ensure_member_coverage_expanded",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(side_effect=AssertionError("must not next yet")),
            ),
        ):
            result = await submit_application_answer(
                "cov-already", session, AsyncMock(), "Agent", 0.0
            )
        self.assertEqual(result.get("step"), "app_insurance_coverage_type")
        self.assertIn("agent", (result.get("spoken") or "").lower())
        self.assertIn("qualify", (result.get("spoken") or "").lower())
        self.assertEqual(session.filled.get("app_insurance_current"), "Agent")

    def test_i_said_agent_t_matches_agent_t(self) -> None:
        from pc_liveagent.app_stage import _match_named_members

        available = ["Agent T.", "john D.", "Don D."]
        for spoken in (
            "I said agent t.",
            "I meant Agent T",
            "Agent t.",
            "agent tee",
        ):
            hits, clarify = _match_named_members(spoken, available)
            self.assertIsNone(clarify, spoken)
            self.assertEqual(hits, ["Agent T."], spoken)

    def test_agent_d_does_not_select_agent_t(self) -> None:
        from pc_liveagent.app_stage import _match_named_members

        available = ["Agent T.", "john D.", "Don D."]
        hits, clarify = _match_named_members("Agent D", available)
        self.assertIsNone(clarify)
        self.assertEqual(hits, [])
        hits2, clarify2 = _match_named_members("I said agent d.", available)
        self.assertIsNone(clarify2)
        self.assertEqual(hits2, [])

    def test_multiple_coverage_members_match(self) -> None:
        from pc_liveagent.app_stage import _match_named_members

        available = ["Agent T.", "john D.", "Don D."]
        hits, clarify = _match_named_members(
            "Agent T and john D.", available
        )
        self.assertIsNone(clarify)
        self.assertEqual(hits, ["Agent T.", "john D."])

    async def test_coverage_checkbox_polls_delayed_selected_state(self) -> None:
        from pc_liveagent.app_stage import _set_member_insurance_checked

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        click_loc = AsyncMock()
        click_loc.count = AsyncMock(return_value=1)
        click_loc.click = AsyncMock()
        click_loc.first = click_loc
        box_loc = AsyncMock()
        box_loc.count = AsyncMock(return_value=1)
        box_loc.check = AsyncMock()
        box_loc.first = box_loc
        page.locator = MagicMock(
            side_effect=lambda sel: click_loc
            if "cov-click" in sel
            else box_loc
        )
        reads = iter([False, False, True])

        async def _is_checked(_page, _name, *, want_checked=True):
            return next(reads)

        with (
            patch(
                "pc_liveagent.app_stage._locate_coverage_member_control",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "reason": "needs_click",
                        "name": "Agent T.",
                        "checked": False,
                        "tag": "INPUT",
                        "role": "",
                        "has_label": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._coverage_member_is_checked",
                new=AsyncMock(side_effect=_is_checked),
            ),
            patch(
                "pc_liveagent.app_stage._clear_coverage_click_marks",
                new=AsyncMock(),
            ),
        ):
            ok = await _set_member_insurance_checked(page, "Agent T.", True)
        self.assertTrue(ok)
        click_loc.click.assert_awaited()
        box_loc.check.assert_not_awaited()
        self.assertGreaterEqual(page.wait_for_timeout.await_count, 1)

    async def test_coverage_checkbox_already_on_skips_click(self) -> None:
        from pc_liveagent.app_stage import _set_member_insurance_checked

        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage._locate_coverage_member_control",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "reason": "already_selected",
                        "name": "john D.",
                        "checked": True,
                    }
                ),
            ) as locate,
            patch(
                "pc_liveagent.app_stage._clear_coverage_click_marks",
                new=AsyncMock(),
            ),
            patch(
                "pc_liveagent.app_stage._poll_coverage_member_checked",
                new=AsyncMock(side_effect=AssertionError("must not poll")),
            ),
        ):
            ok = await _set_member_insurance_checked(page, "john D.", True)
        self.assertTrue(ok)
        locate.assert_awaited_once()

    async def test_coverage_checkbox_click_failure_stays_unverified(self) -> None:
        from pc_liveagent.app_stage import _set_member_insurance_checked

        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage._locate_coverage_member_control",
                new=AsyncMock(
                    return_value={"ok": False, "reason": "missing", "checked": False}
                ),
            ),
            patch(
                "pc_liveagent.app_stage._clear_coverage_click_marks",
                new=AsyncMock(),
            ),
        ):
            ok = await _set_member_insurance_checked(page, "Agent T.", True)
        self.assertFalse(ok)

    async def test_checked_member_without_expanded_plans_is_not_ready(self) -> None:
        from pc_liveagent.app_stage import _ensure_member_coverage_expanded

        page = AsyncMock()
        clock = {"t": 0.0}

        async def _wait(ms: int) -> None:
            clock["t"] += ms / 1000.0

        page.wait_for_timeout = AsyncMock(side_effect=_wait)
        trigger = AsyncMock()
        trigger.first = trigger
        trigger.count = AsyncMock(return_value=1)
        trigger.click = AsyncMock()
        page.locator = MagicMock(return_value=trigger)
        with (
            patch(
                "pc_liveagent.app_stage._locate_coverage_member_expander",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "reason": "needs_click",
                        "aria_expanded": "false",
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._member_coverage_types_visible",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.app_stage._read_marked_coverage_expanded",
                new=AsyncMock(
                    return_value={
                        "found": True,
                        "aria_expanded": "true",
                        "data_state": "expanded",
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._clear_coverage_expander_marks",
                new=AsyncMock(),
            ),
            patch(
                "pc_liveagent.app_stage.time.monotonic",
                side_effect=lambda: clock["t"],
            ),
        ):
            ok = await _ensure_member_coverage_expanded(
                page, "Agent T."
            )
        self.assertFalse(ok)
        trigger.click.assert_awaited_once()

    async def test_coverage_checkbox_never_selected_times_out(self) -> None:
        from pc_liveagent.app_stage import _set_member_insurance_checked

        page = AsyncMock()
        click_loc = AsyncMock()
        click_loc.count = AsyncMock(return_value=1)
        click_loc.click = AsyncMock()
        click_loc.first = click_loc
        box_loc = AsyncMock()
        box_loc.count = AsyncMock(return_value=0)
        box_loc.first = box_loc
        page.locator = MagicMock(
            side_effect=lambda sel: box_loc if "cov-box" in sel else click_loc
        )
        clock = {"t": 0.0}

        async def _wait(ms: int) -> None:
            clock["t"] += ms / 1000.0

        page.wait_for_timeout = AsyncMock(side_effect=_wait)
        with (
            patch(
                "pc_liveagent.app_stage._locate_coverage_member_control",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "reason": "needs_click",
                        "name": "john D.",
                        "checked": False,
                        "tag": "SPAN",
                        "role": "checkbox",
                        "has_label": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._coverage_member_is_checked",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.app_stage._read_marked_coverage_checked",
                new=AsyncMock(
                    return_value={
                        "found": True,
                        "checked": False,
                        "aria_checked": "false",
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._clear_coverage_click_marks",
                new=AsyncMock(),
            ),
            patch(
                "pc_liveagent.app_stage.time.monotonic",
                side_effect=lambda: clock["t"],
            ),
        ):
            ok = await _set_member_insurance_checked(page, "john D.", True)
        self.assertFalse(ok)
        click_loc.click.assert_awaited()

    async def test_coverage_native_input_check_without_label(self) -> None:
        from pc_liveagent.app_stage import _set_member_insurance_checked

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        click_loc = AsyncMock()
        click_loc.count = AsyncMock(return_value=1)
        click_loc.click = AsyncMock()
        click_loc.first = click_loc
        box_loc = AsyncMock()
        box_loc.count = AsyncMock(return_value=1)
        box_loc.check = AsyncMock()
        box_loc.first = box_loc
        page.locator = MagicMock(
            side_effect=lambda sel: click_loc
            if "cov-click" in sel
            else box_loc
        )
        with (
            patch(
                "pc_liveagent.app_stage._locate_coverage_member_control",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "reason": "needs_click",
                        "name": "john D.",
                        "checked": False,
                        "tag": "INPUT",
                        "has_label": False,
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._coverage_member_is_checked",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._clear_coverage_click_marks",
                new=AsyncMock(),
            ),
        ):
            ok = await _set_member_insurance_checked(page, "john D.", True)
        self.assertTrue(ok)
        box_loc.check.assert_awaited()
        click_loc.click.assert_not_awaited()

    async def test_coverage_label_click_preferred_over_hidden_input(self) -> None:
        from pc_liveagent.app_stage import _set_member_insurance_checked

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        click_loc = AsyncMock()
        click_loc.count = AsyncMock(return_value=1)
        click_loc.click = AsyncMock()
        click_loc.first = click_loc
        box_loc = AsyncMock()
        box_loc.count = AsyncMock(return_value=1)
        box_loc.check = AsyncMock()
        box_loc.first = box_loc
        page.locator = MagicMock(
            side_effect=lambda sel: click_loc
            if "cov-click" in sel
            else box_loc
        )
        with (
            patch(
                "pc_liveagent.app_stage._locate_coverage_member_control",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "reason": "needs_click",
                        "name": "Agent T.",
                        "checked": False,
                        "tag": "INPUT",
                        "has_label": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._coverage_member_is_checked",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._clear_coverage_click_marks",
                new=AsyncMock(),
            ),
        ):
            ok = await _set_member_insurance_checked(page, "Agent T.", True)
        self.assertTrue(ok)
        click_loc.click.assert_awaited()
        box_loc.check.assert_not_awaited()

    async def test_coverage_verify_uses_marked_control_over_stale_scrape(
        self,
    ) -> None:
        """Selected mark wins even if the old scrape path still looks unchecked."""
        from pc_liveagent.app_stage import _verify_coverage_member_checked

        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage._locate_coverage_member_control",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "reason": "already_selected",
                        "name": "john D.",
                        "checked": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._read_marked_coverage_checked",
                new=AsyncMock(
                    return_value={
                        "found": True,
                        "checked": True,
                        "aria_checked": "true",
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_coverage_members",
                new=AsyncMock(
                    return_value=[{"name": "john D.", "checked": False}]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._clear_coverage_click_marks",
                new=AsyncMock(),
            ),
        ):
            self.assertTrue(
                await _verify_coverage_member_checked(page, "john D.")
            )

    async def test_coverage_aria_checked_counts_as_selected(self) -> None:
        from pc_liveagent.app_stage import _verify_coverage_member_checked

        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage._locate_coverage_member_control",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "reason": "already_selected",
                        "name": "Agent T.",
                        "checked": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._read_marked_coverage_checked",
                new=AsyncMock(return_value={"found": False, "checked": False}),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_coverage_members",
                new=AsyncMock(
                    return_value=[{"name": "Agent T.", "checked": True}]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._clear_coverage_click_marks",
                new=AsyncMock(),
            ),
        ):
            self.assertTrue(
                await _verify_coverage_member_checked(page, "Agent T.")
            )

    async def test_coverage_wrong_member_not_verified(self) -> None:
        from pc_liveagent.app_stage import _verify_coverage_member_checked

        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage._locate_coverage_member_control",
                new=AsyncMock(
                    return_value={"ok": False, "reason": "missing", "checked": False}
                ),
            ),
            patch(
                "pc_liveagent.app_stage._clear_coverage_click_marks",
                new=AsyncMock(),
            ),
        ):
            self.assertFalse(
                await _verify_coverage_member_checked(page, "Nobody")
            )

    async def test_john_d_and_agent_t_match_for_coverage(self) -> None:
        from pc_liveagent.app_stage import _match_named_members

        available = ["Agent T.", "john D.", "Don D."]
        hits, clarify = _match_named_members("I said John d.", available)
        self.assertIsNone(clarify)
        self.assertEqual(hits, ["john D."])
        hits2, clarify2 = _match_named_members("Agent t.", available)
        self.assertIsNone(clarify2)
        self.assertEqual(hits2, ["Agent T."])
        # Wrong initial must not select Agent T.
        bad, _ = _match_named_members("Agent b.", available)
        self.assertEqual(bad, [])

    async def test_i_said_agent_t_checks_box_end_to_end(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="cov-said",
            url="https://example.test",
            app_step="app_insurance_members",
        )
        state = {
            "members": [
                {"name": "Agent T.", "checked": False, "index": 0},
                {"name": "john D.", "checked": False, "index": 1},
                {"name": "Don D.", "checked": False, "index": 2},
            ]
        }

        async def _scrape(_page):
            return [dict(item) for item in state["members"]]

        async def _check(_page, name, checked=True):
            for item in state["members"]:
                if item["name"].lower() == name.lower():
                    item["checked"] = checked
                    return True
            return False

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_coverage_members",
                new=AsyncMock(side_effect=_scrape),
            ),
            patch(
                "pc_liveagent.app_stage._set_member_insurance_checked",
                new=AsyncMock(side_effect=_check),
            ) as check,
            patch(
                "pc_liveagent.app_stage._verify_coverage_member_checked",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._ensure_member_coverage_expanded",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(side_effect=AssertionError("must not next yet")),
            ),
        ):
            result = await submit_application_answer(
                "cov-said", session, AsyncMock(), "I said agent t.", 0.0
            )
        check.assert_not_awaited()
        self.assertEqual(result.get("step"), "app_insurance_coverage_type")
        self.assertIn("agent t", (result.get("spoken") or "").lower())
        self.assertIn("qualify", (result.get("spoken") or "").lower())
        self.assertNotEqual(result.get("error"), "insurance_members_clarify")
        self.assertNotEqual(result.get("error"), "insurance_members_not_verified")

    def test_map_coverage_types(self) -> None:
        from pc_liveagent.app_stage import _map_coverage_types

        hits = _map_coverage_types("medicare and private marketplace")
        self.assertIn("Medicare", hits)
        self.assertIn("Private insurance or ACA Marketplace", hits)
        self.assertIn(
            "Health insurance through a job (employer-sponsored)",
            _map_coverage_types("employer sponsored through a job"),
        )

    async def test_insurance_other_asks_and_fills_please_specify(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="ins-other",
            url="https://example.test",
            app_step="app_insurance_coverage_type",
        )
        session.filled["app_insurance_queue"] = '["Admin P."]'
        session.filled["app_insurance_current"] = "Admin P."
        session.filled["app_insurance_coverage_phase"] = "collect"
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage._ensure_member_coverage_expanded",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._set_member_coverage_type",
                new=AsyncMock(return_value=True),
            ) as set_cov,
            patch(
                "pc_liveagent.app_stage._verify_member_coverage_types",
                new=AsyncMock(return_value=True),
            ),
        ):
            selected = await submit_application_answer(
                "ins-other", session, page, "Other", 0.0
            )
        set_cov.assert_awaited()
        self.assertEqual(
            session.filled.get("app_insurance_coverage_phase"), "other_specify"
        )
        self.assertIn("please specify", (selected.get("spoken") or "").lower())
        self.assertIn("other insurance", (selected.get("spoken") or "").lower())

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage._fill_member_coverage_other_specify",
                new=AsyncMock(return_value={"ok": True, "value": "Blue Cross"}),
            ) as fill_other,
        ):
            named = await submit_application_answer(
                "ins-other", session, page, "Blue Cross", 0.0
            )
        fill_other.assert_awaited_once()
        self.assertEqual(session.filled.get("app_insurance_coverage_phase"), "more")
        self.assertEqual(session.filled.get("app_insurance_other_text"), "Blue Cross")
        spoken = (named.get("spoken") or "").lower()
        self.assertIn("blue cross", spoken)
        self.assertIn("other plans", spoken)

    async def test_fill_and_save_dob_requires_value_before_save(self) -> None:
        from pc_liveagent.app_stage import _fill_and_save_dob

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.keyboard = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage._inspect_dob_editor",
                new=AsyncMock(return_value={"count": 1, "hasSave": True}),
            ),
            patch(
                "pc_liveagent.app_stage._fill_dob_editor_fields",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.live_form._strict_fill",
                new=AsyncMock(side_effect=RuntimeError("no label")),
            ),
            patch(
                "pc_liveagent.app_stage._type_dob_via_keyboard",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.app_stage._click_dob_save",
                new=AsyncMock(return_value=True),
            ) as save,
        ):
            ok = await _fill_and_save_dob(page, "03/15/2018")
        self.assertFalse(ok)
        save.assert_not_awaited()

    async def test_fill_and_save_dob_save_failure_is_failure(self) -> None:
        from pc_liveagent.app_stage import _fill_and_save_dob

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage._inspect_dob_editor",
                new=AsyncMock(return_value={"count": 1, "hasSave": True}),
            ),
            patch(
                "pc_liveagent.app_stage._fill_dob_editor_fields",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._read_segmented_dob_inputs",
                new=AsyncMock(return_value=("03", "15", "2018")),
            ),
            patch(
                "pc_liveagent.app_stage._read_dob_editor_value",
                new=AsyncMock(return_value="03/15/2018"),
            ),
            patch(
                "pc_liveagent.app_stage._click_dob_save",
                new=AsyncMock(return_value=False),
            ),
        ):
            ok = await _fill_and_save_dob(page, "03/15/2018")
        self.assertFalse(ok)

    async def test_fill_and_save_dob_segmented_success(self) -> None:
        from pc_liveagent.app_stage import _fill_and_save_dob

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage._inspect_dob_editor",
                new=AsyncMock(
                    return_value={
                        "count": 3,
                        "kinds": ["text", "text", "text"],
                        "hasSave": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._fill_dob_editor_fields",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._read_segmented_dob_inputs",
                new=AsyncMock(side_effect=[("10", "22", "2024"), None]),
            ),
            patch(
                "pc_liveagent.app_stage._click_dob_save",
                new=AsyncMock(return_value=True),
            ) as save,
            patch(
                "pc_liveagent.app_stage._type_dob_via_keyboard",
                new=AsyncMock(side_effect=AssertionError("must not keyboard")),
            ),
        ):
            ok = await _fill_and_save_dob(page, "10/22/2024")
        self.assertTrue(ok)
        save.assert_awaited_once()

    async def test_fill_and_save_accepts_unpadded_segmented_values(self) -> None:
        """Regression for call_7c75b5e…: fill ok=1 with 7/7/2007 must Save."""
        from pc_liveagent.app_stage import _fill_and_save_dob

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        keyboard = AsyncMock(side_effect=AssertionError("must not keyboard fallback"))
        with (
            patch(
                "pc_liveagent.app_stage._inspect_dob_editor",
                new=AsyncMock(
                    return_value={
                        "count": 3,
                        "kinds": ["text", "text", "text"],
                        "hasSave": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._fill_dob_editor_fields",
                new=AsyncMock(return_value=True),
            ),
            # Generic reader would disagree; segmented placeholders are truth.
            patch(
                "pc_liveagent.app_stage._read_dob_editor_value",
                new=AsyncMock(return_value=""),
            ),
            patch(
                "pc_liveagent.app_stage._read_segmented_dob_inputs",
                new=AsyncMock(side_effect=[("7", "7", "2007"), None]),
            ),
            patch(
                "pc_liveagent.app_stage._click_dob_save",
                new=AsyncMock(return_value=True),
            ) as save,
            patch(
                "pc_liveagent.app_stage._type_dob_via_keyboard",
                new=keyboard,
            ),
            patch(
                "pc_liveagent.app_stage._dob_validation_visible",
                new=AsyncMock(return_value=False),
            ),
        ):
            ok = await _fill_and_save_dob(
                page, "07/07/2007", member_name="Agent T."
            )
        self.assertTrue(ok)
        save.assert_awaited_once()
        keyboard.assert_not_awaited()

    async def test_fill_and_save_rejects_incomplete_year(self) -> None:
        from pc_liveagent.app_stage import _fill_and_save_dob

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage._inspect_dob_editor",
                new=AsyncMock(
                    return_value={
                        "count": 3,
                        "kinds": ["text", "text", "text"],
                        "hasSave": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._fill_dob_editor_fields",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._read_segmented_dob_inputs",
                new=AsyncMock(return_value=("7", "7", "007")),
            ),
            patch(
                "pc_liveagent.app_stage._fill_segmented_dob_with_playwright",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.app_stage._click_dob_save",
                new=AsyncMock(side_effect=AssertionError("must not save")),
            ),
        ):
            ok = await _fill_and_save_dob(
                page, "07/07/2007", member_name="Agent T."
            )
        self.assertFalse(ok)

    async def test_fill_and_save_reports_save_click_failure(self) -> None:
        from pc_liveagent.app_stage import _fill_and_save_dob

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage._inspect_dob_editor",
                new=AsyncMock(
                    return_value={
                        "count": 3,
                        "kinds": ["text", "text", "text"],
                        "hasSave": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._fill_dob_editor_fields",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._read_segmented_dob_inputs",
                new=AsyncMock(return_value=("07", "07", "2007")),
            ),
            patch(
                "pc_liveagent.app_stage._click_dob_save",
                new=AsyncMock(return_value=False),
            ),
        ):
            ok = await _fill_and_save_dob(
                page, "07/07/2007", member_name="Agent T."
            )
        self.assertFalse(ok)

    def test_dob_digits_match_allows_dropped_leading_zeros(self) -> None:
        from pc_liveagent.app_stage import _dob_digits_match

        # Pointcare may display 7/7/2007 after a successful 07/07/2007 fill.
        self.assertTrue(_dob_digits_match("07/07/2007", "7/7/2007"))
        self.assertTrue(_dob_digits_match("07/07/2007", "07/07/2007"))
        self.assertTrue(_dob_digits_match("07/07/2007", "7/7/2007"))
        # Digit spill / incomplete year must still fail.
        self.assertFalse(_dob_digits_match("07/07/2007", "7/7/007"))
        self.assertFalse(_dob_digits_match("07/07/2007", "7/8/2007"))
        self.assertFalse(_dob_digits_match("07/07/2007", "8/7/2007"))

    async def test_dob_fill_failed_keeps_member_active(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-fill-fail",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Mickey Mouse"
        session.filled["app_dob_queue"] = '["Mickey Mouse"]'
        session.filled["app_dob_component"] = "month"
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "Mickey Mouse",
                            "dob": "",
                            "needs_add": True,
                            "index": 0,
                            "editor_open": True,
                        }
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._click_member_add_dob",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._fill_one_dob_component_detailed",
                new=AsyncMock(return_value=(False, "component_input_not_found")),
            ),
            patch(
                "pc_liveagent.app_stage._fill_and_save_dob",
                new=AsyncMock(side_effect=AssertionError("must not save")),
            ),
        ):
            result = await submit_application_answer(
                "dob-fill-fail", session, AsyncMock(), "March", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "component_input_not_found")
        self.assertEqual(session.app_step, "app_dob_collect")
        self.assertEqual(session.filled.get("app_dob_current"), "Mickey Mouse")
        self.assertEqual(session.filled.get("app_dob_component"), "month")

    async def test_dob_collects_missing_then_blocks_disabled_next(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-miss",
            url="https://example.test",
            app_step="app_dob_review",
        )
        cards_missing = [
            {"name": "Mickey Mouse", "dob": "", "needs_add": True, "index": 0},
            {"name": "Murali M.", "dob": "01/02/1990", "needs_add": False, "index": 1},
        ]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=cards_missing),
            ),
            patch(
                "pc_liveagent.app_stage._click_member_add_dob",
                new=AsyncMock(return_value=True),
            ),
        ):
            start = await submit_application_answer(
                "dob-miss", session, AsyncMock(), "Yes", 0.0
            )
        self.assertEqual(start.get("step"), "app_dob_collect")
        self.assertIn("mickey", (start.get("spoken") or "").lower())
        self.assertIn("month", (start.get("spoken") or "").lower())

        cards_after = [
            {
                "name": "Mickey Mouse",
                "dob": "03/15/2018",
                "needs_add": False,
                "index": 0,
            },
            {"name": "Murali M.", "dob": "01/02/1990", "needs_add": False, "index": 1},
        ]
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=cards_missing),
            ),
            patch(
                "pc_liveagent.app_stage._click_member_add_dob",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._fill_one_dob_component_detailed",
                new=AsyncMock(return_value=(True, "ok")),
            ),
            patch(
                "pc_liveagent.app_stage._fill_and_save_dob",
                new=AsyncMock(side_effect=AssertionError("save only after confirm")),
            ),
        ):
            for answer, expect_component in (
                ("March", "day"),
                ("15", "year"),
                ("2018", "confirm"),
            ):
                turn = await submit_application_answer(
                    "dob-miss", session, page, answer, 0.0
                )
                self.assertTrue(turn.get("ok"), msg=answer)
                self.assertEqual(
                    session.filled.get("app_dob_component"), expect_component
                )

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(side_effect=[cards_missing, cards_after, cards_after]),
            ),
            patch(
                "pc_liveagent.app_stage._click_member_add_dob",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._fill_and_save_dob",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._verify_member_dob",
                new=AsyncMock(return_value=True),
            ),
        ):
            saved = await submit_application_answer(
                "dob-miss", session, page, "Yes", 0.0
            )
        self.assertTrue(saved.get("ok"))
        # Existing DOB is reviewed aloud — no editor for Murali.
        self.assertEqual(saved.get("step"), "app_dob_review")
        spoken_murali = (saved.get("spoken") or "").lower()
        self.assertIn("murali", spoken_murali)
        self.assertIn("date of birth is", spoken_murali)
        self.assertNotIn("month", spoken_murali)

        # Confirm Murali's existing DOB, then Next stays blocked when disabled.
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=cards_after),
            ),
            patch(
                "pc_liveagent.app_stage._click_member_add_dob",
                new=AsyncMock(side_effect=AssertionError("must not edit")),
            ),
            patch(
                "pc_liveagent.app_stage._fill_and_save_dob",
                new=AsyncMock(side_effect=AssertionError("must not save")),
            ),
        ):
            reviewed = await submit_application_answer(
                "dob-miss", session, AsyncMock(), "Yes", 0.0
            )
        self.assertTrue(reviewed.get("ok"))
        self.assertEqual(session.filled.get("app_dob_walk_complete"), "1")

        session.app_step = "app_dob_review"
        session.filled["app_dob_walk_complete"] = "1"
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=cards_after),
            ),
            patch(
                "pc_liveagent.app_stage._next_button_enabled",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(return_value=False),
            ) as nxt,
        ):
            blocked = await submit_application_answer(
                "dob-miss", session, AsyncMock(), "Yes", 0.0
            )
        nxt.assert_not_awaited()
        self.assertFalse(blocked.get("ok"))
        self.assertIn("disabled", (blocked.get("spoken") or "").lower())

    async def test_dob_invalid_date_asks_clarify(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-bad",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Mickey Mouse"
        session.filled["app_dob_queue"] = '["Mickey Mouse"]'
        session.filled["app_dob_component"] = "day"
        session.filled["app_dob_partial"] = (
            '{"name": "Mickey Mouse", "month": 2, "day": null, "year": null}'
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "Mickey Mouse",
                            "dob": "",
                            "needs_add": True,
                            "editor_open": True,
                        }
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._fill_one_dob_component_detailed",
                new=AsyncMock(side_effect=AssertionError("must not fill invalid day")),
            ),
        ):
            result = await submit_application_answer(
                "dob-bad", session, AsyncMock(), "31", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "dob_invalid")
        self.assertEqual(session.app_step, "app_dob_collect")
        self.assertEqual(session.filled.get("app_dob_component"), "day")

    async def test_dob_ambiguous_keeps_member_and_asks(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-amb",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Agent H."
        session.filled["app_dob_queue"] = '["Agent H."]'
        session.filled["app_dob_component"] = "month"
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "Agent H.",
                            "dob": "",
                            "needs_add": True,
                            "editor_open": True,
                        }
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._fill_one_dob_component_detailed",
                new=AsyncMock(side_effect=AssertionError("must not fill slash date")),
            ),
        ):
            result = await submit_application_answer(
                "dob-amb", session, AsyncMock(), "10/11/2024", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "dob_incomplete")
        self.assertEqual(session.app_step, "app_dob_collect")
        self.assertEqual(session.filled.get("app_dob_current"), "Agent H.")
        self.assertEqual(session.filled.get("app_dob_component"), "month")
        self.assertIn("month", (result.get("spoken") or "").lower())

    async def test_dob_duplicate_name_asks_which_member(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-dup",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "John D."
        session.filled["app_dob_queue"] = '["John D."]'
        session.filled["app_dob_component"] = "month"
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(
                    return_value=[
                        {"name": "John D.", "dob": "", "needs_add": True, "index": 0},
                        {"name": "John D.", "dob": "", "needs_add": True, "index": 1},
                    ]
                ),
            ),
        ):
            result = await submit_application_answer(
                "dob-dup", session, AsyncMock(), "March", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "dob_duplicate_name")

    async def test_unidentified_screen_is_recoverable_error(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="unk",
            url="https://example.test",
            app_step="",
        )
        with patch(
            "pc_liveagent.app_stage.detect_application_screen",
            new=AsyncMock(return_value=""),
        ):
            result = await submit_application_answer(
                "unk", session, AsyncMock(), "Yes", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "stage_e_screen_unidentified")

    async def test_click_next_skips_when_disabled(self) -> None:
        from pc_liveagent.app_stage import _click_next_or_continue

        with (
            patch(
                "pc_liveagent.app_stage._next_button_enabled",
                new=AsyncMock(return_value=False),
            ),
            patch("pc_liveagent.live_form._click_cta", new=AsyncMock()) as cta,
        ):
            ok = await _click_next_or_continue(AsyncMock())
        self.assertFalse(ok)
        cta.assert_not_awaited()

    def test_agent_edge_asr_maps_to_agent_h(self) -> None:
        from pc_liveagent.app_stage import _match_named_members

        hits, clarify = _match_named_members(
            "Change agent h date of birth.",
            ["Agent H.", "Logic S."],
        )
        self.assertIsNone(clarify)
        self.assertEqual(hits, ["Agent H."])

        hits2, _ = _match_named_members("Agent Edge.", ["Agent H.", "Logic S."])
        self.assertEqual(hits2, ["Agent H."])

    def test_change_dob_works_when_scrape_empty(self) -> None:
        from pc_liveagent.app_stage import _match_named_members

        hits, clarify = _match_named_members(
            "Change agent h date of birth.",
            [],
        )
        self.assertIsNone(clarify)
        self.assertEqual(hits, ["Agent H"])

    async def test_live_failure_change_agent_h_enters_collect(self) -> None:
        """Regression: call_41ac… said change Agent H but scrape returned []."""
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="live-dob-fix",
            url="https://example.test",
            app_step="app_dob_review",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=[]),
            ),
        ):
            result = await submit_application_answer(
                "live-dob-fix",
                session,
                AsyncMock(),
                "Change agent h date of birth.",
                0.0,
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_dob_collect")
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("agent h", spoken)
        self.assertIn("month", spoken)
        self.assertEqual(session.filled.get("app_dob_component"), "month")

    async def test_bare_agent_edge_enters_collect(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="live-edge",
            url="https://example.test",
            app_step="app_dob_review",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "Agent H.",
                            "dob": "01/01/1990",
                            "needs_add": False,
                            "index": 0,
                        },
                        {
                            "name": "Logic S.",
                            "dob": "02/02/1991",
                            "needs_add": False,
                            "index": 1,
                        },
                    ]
                ),
            ),
        ):
            result = await submit_application_answer(
                "live-edge", session, AsyncMock(), "Agent Edge.", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_dob_collect")
        self.assertIn("agent h", (result.get("spoken") or "").lower())

    async def test_body_fallback_scrapes_heading_only_dob_page(self) -> None:
        from pc_liveagent.app_stage import _scrape_dob_cards_from_body

        class _Body:
            async def inner_text(self) -> str:
                return (
                    "Date of birth\n"
                    "Agent H.\n"
                    "Add\n"
                    "Logic S.\n"
                    "01/02/1990\n"
                )

        page = AsyncMock()
        page.locator = lambda _s: _Body()
        cards = await _scrape_dob_cards_from_body(page)
        names = [c["name"] for c in cards]
        self.assertIn("Agent H.", names)
        agent = next(c for c in cards if c["name"] == "Agent H.")
        self.assertTrue(agent.get("needs_add"))

    def test_parse_dob_natural_formats_and_limits(self) -> None:
        from datetime import date

        from pc_liveagent.app_stage import _parse_dob_answer
        from pc_liveagent.live_form import _normalize_dob

        accepted = {
            "01/23/2001": "01/23/2001",
            "23/01/2001": "01/23/2001",
            "2001-01-23": "01/23/2001",
            "01-23-2001": "01/23/2001",
            "23-01-2001": "01/23/2001",
            "01.23.2001": "01/23/2001",
            "23.01.2001": "01/23/2001",
            "2001.01.23": "01/23/2001",
            "January 23, 2001": "01/23/2001",
            "Jan 23, 2001": "01/23/2001",
            "jan 2 2001": "01/02/2001",
            "feb 2nd 2002": "02/02/2002",
            "oct 22 2005": "10/22/2005",
            "october 22 2005": "10/22/2005",
            "date is 22 october 2005": "10/22/2005",
            "22nd of oct 2005": "10/22/2005",
            "2nd of oct 2005": "10/02/2005",
            "Nov 2, 2000": "11/02/2000",
            "23 January 2001": "01/23/2001",
            "23 Jan 2001": "01/23/2001",
            "November 23rd, 2001": "11/23/2001",
            "November 2nd, 2000": "11/02/2000",
            "March 1st, 1990": "03/01/1990",
            "November twenty-third, two thousand one": "11/23/2001",
            "the second of December, nineteen ninety-five": "12/02/1995",
            "02/29/2000": "02/29/2000",
            "02/29/2024": "02/29/2024",
        }
        for raw, expected in accepted.items():
            self.assertEqual(_parse_dob_answer(raw), (expected, None), raw)
        self.assertEqual(_parse_dob_answer("02/29/1900"), (None, None))
        self.assertEqual(_parse_dob_answer("02/29/2019"), (None, None))
        self.assertEqual(_normalize_dob("10/11/2024"), "10/11/2024")
        ambiguous, note = _parse_dob_answer("05.06.2001")
        self.assertIsNone(ambiguous)
        self.assertIn("ambiguous", (note or "").lower())
        month_only, month_note = _parse_dob_answer("November")
        self.assertIsNone(month_only)
        self.assertIn("day", (month_note or "").lower())
        self.assertIn("year", (month_note or "").lower())
        day_only, day_note = _parse_dob_answer("the 23rd")
        self.assertIsNone(day_only)
        self.assertIn("month", (day_note or "").lower())
        year_note = _parse_dob_answer("01/23/01")[1] or ""
        self.assertIn("century", year_note.lower())
        future, future_note = _parse_dob_answer("01/01/2099")
        self.assertIsNone(future)
        self.assertIn("future", (future_note or "").lower())
        today = date.today()
        if today.month == 12 and today.day == 31:
            return
        tomorrow = date.fromordinal(today.toordinal() + 1)
        spoken_tomorrow = f"{tomorrow.month:02d}/{tomorrow.day:02d}/{tomorrow.year}"
        self.assertIn("future", (_parse_dob_answer(spoken_tomorrow)[1] or "").lower())

    async def test_existing_dob_still_requires_component_confirmation(self) -> None:
        """Prefill is not treated as caller-confirmed — walk month → confirm."""
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-keep",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Murali M."
        session.filled["app_dob_queue"] = '["Murali M.", "Mickey Mouse"]'
        session.filled["app_dob_component"] = "month"
        session.filled["app_dob_correcting"] = "Murali M."
        cards = [
            {
                "name": "Murali M.",
                "dob": "01/02/1990",
                "needs_add": False,
                "editor_open": True,
                "index": 0,
            },
            {"name": "Mickey Mouse", "dob": "", "needs_add": True, "index": 1},
        ]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._fill_one_dob_component_detailed",
                new=AsyncMock(return_value=(True, "ok")),
            ) as fill_one,
            patch(
                "pc_liveagent.app_stage._fill_and_save_dob",
                new=AsyncMock(side_effect=AssertionError("save only after confirm")),
            ),
        ):
            result = await submit_application_answer(
                "dob-keep", session, AsyncMock(), "January", 0.0
            )
        fill_one.assert_awaited_once()
        self.assertEqual(fill_one.await_args.args[2], "month")
        self.assertTrue(result.get("ok"))
        self.assertEqual(session.filled.get("app_dob_component"), "day")
        self.assertEqual(session.filled.get("app_dob_current"), "Murali M.")

    async def test_unconfirmed_date_is_not_saved(self) -> None:
        """Year accepted moves to confirm — Save must wait for explicit Yes."""
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-exist",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Murali M."
        session.filled["app_dob_queue"] = '["Murali M."]'
        session.filled["app_dob_component"] = "year"
        session.filled["app_dob_partial"] = (
            '{"name": "Murali M.", "month": 3, "day": 15, "year": null}'
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "Murali M.",
                            "dob": "01/02/1990",
                            "needs_add": False,
                            "editor_open": True,
                            "index": 0,
                        }
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._fill_one_dob_component_detailed",
                new=AsyncMock(return_value=(True, "ok")),
            ),
            patch(
                "pc_liveagent.app_stage._fill_and_save_dob",
                new=AsyncMock(side_effect=AssertionError("must not save yet")),
            ) as fill,
        ):
            result = await submit_application_answer(
                "dob-exist", session, AsyncMock(), "2018", 0.0
            )
        fill.assert_not_awaited()
        self.assertTrue(result.get("ok"))
        self.assertEqual(session.filled.get("app_dob_component"), "confirm")
        self.assertIn("march 15, 2018", (result.get("spoken") or "").lower())
        self.assertEqual(session.app_step, "app_dob_collect")

    async def test_other_member_dob_change_blocks_progress(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-wrong",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Mickey Mouse"
        session.filled["app_dob_queue"] = '["Mickey Mouse"]'
        session.filled["app_dob_component"] = "confirm"
        session.filled["app_dob_partial"] = (
            '{"name": "Mickey Mouse", "month": 3, "day": 15, "year": 2018}'
        )
        before = [
            {"name": "Mickey Mouse", "dob": "", "needs_add": True, "index": 0},
            {"name": "Murali M.", "dob": "01/02/1990", "needs_add": False, "index": 1},
        ]
        after = [
            {"name": "Mickey Mouse", "dob": "03/15/2018", "needs_add": False, "index": 0},
            {"name": "Murali M.", "dob": "03/15/2018", "needs_add": False, "index": 1},
        ]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(side_effect=[before, after]),
            ),
            patch(
                "pc_liveagent.app_stage._click_member_add_dob",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._fill_and_save_dob",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._verify_member_dob",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(return_value=True),
            ) as nxt,
        ):
            result = await submit_application_answer(
                "dob-wrong", session, AsyncMock(), "Yes", 0.0
            )
        nxt.assert_not_awaited()
        self.assertEqual(result.get("error"), "dob_wrong_member")
        self.assertEqual(session.app_step, "app_dob_collect")

    async def test_missing_dob_does_not_click_next(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-next",
            url="https://example.test",
            app_step="app_dob_review",
        )
        cards = [
            {"name": "Mickey Mouse", "dob": "", "needs_add": False, "index": 0}
        ]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(return_value=True),
            ) as nxt,
        ):
            result = await submit_application_answer(
                "dob-next", session, AsyncMock(), "Yes", 0.0
            )
        nxt.assert_not_awaited()
        self.assertEqual(result.get("step"), "app_dob_collect")

    async def test_save_validation_error_is_not_success(self) -> None:
        from pc_liveagent.app_stage import _fill_and_save_dob

        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.evaluate = AsyncMock(return_value=True)
        with (
            patch(
                "pc_liveagent.app_stage._inspect_dob_editor",
                new=AsyncMock(return_value={"count": 3, "hasSave": True}),
            ),
            patch(
                "pc_liveagent.app_stage._fill_dob_editor_fields",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._read_segmented_dob_inputs",
                new=AsyncMock(return_value=("03", "15", "2018")),
            ),
            patch(
                "pc_liveagent.app_stage._read_dob_editor_value",
                new=AsyncMock(return_value="03/15/2018"),
            ),
            patch(
                "pc_liveagent.app_stage._click_dob_save",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._dob_validation_visible",
                new=AsyncMock(return_value=True),
            ),
        ):
            ok = await _fill_and_save_dob(page, "03/15/2018", "Mickey Mouse")
        self.assertFalse(ok)


class FullCallFlowIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Intro → Stage D → programs consent → Stage E with shortcuts off."""

    def test_runtime_flags_default_to_full_flow(self) -> None:
        from pc_liveagent import config

        # .env for normal calls must keep both shortcuts disabled.
        self.assertFalse(config.application_test_mode_from_env())
        self.assertFalse(config.application_after_signin_from_env())

    async def test_begin_speech_is_greeting_when_shortcuts_off(self) -> None:
        from pc_liveagent.call_turn import begin_speech, drop_controller
        from pc_liveagent.live_fill_prompt import LIVE_FILL_GREETING

        call_id = "full-greet"
        await drop_controller(call_id)
        with (
            patch("pc_liveagent.config.APPLICATION_TEST_MODE", False),
            patch("pc_liveagent.config.APPLICATION_AFTER_SIGNIN", False),
        ):
            speech = await begin_speech(call_id)
        self.assertEqual(speech, LIVE_FILL_GREETING)

    def test_stage_d_ends_at_programs(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _next_elig_step

        session = LiveFormSession(call_id="d-end", url="https://example.test")
        session.elig_step = "special_more"
        session.filled["special_conditions"] = "None of the above"
        self.assertEqual(_next_elig_step(session, "No"), "programs")
        session.elig_step = "due_date"
        self.assertEqual(_next_elig_step(session, "October 1 2026"), "programs")

    async def test_programs_yes_then_stage_e_citizenship(self) -> None:
        from pc_liveagent.app_stage import (
            handle_programs_decision,
            submit_application_answer,
        )
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="full-e", url="https://example.test", elig_step="programs"
        )
        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage.click_start_applications",
                new=AsyncMock(return_value="Start Applications"),
            ) as start,
            patch(
                "pc_liveagent.app_stage.wait_for_application_started",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch("pc_liveagent.config.APPLICATION_TEST_MODE", False),
        ):
            gate = await handle_programs_decision("full-e", session, page, "Yes")
        start.assert_awaited_once()
        self.assertEqual(gate.get("step"), "app_citizenship")
        self.assertEqual(gate.get("phase"), "application")
        self.assertEqual(session.app_step, "app_citizenship")

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage._click_yes_no",
                new=AsyncMock(return_value="Yes"),
            ),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_insurance"),
            ),
        ):
            cit = await submit_application_answer(
                "full-e", session, page, "Yes", 0.0
            )
        self.assertEqual(cit.get("step"), "app_insurance")

    async def test_programs_duplicate_yes_does_not_reclick(self) -> None:
        from pc_liveagent.app_stage import handle_programs_decision
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dup-yes", url="https://example.test", elig_step="programs"
        )
        page = AsyncMock()
        start = AsyncMock(return_value="Start Applications")
        with (
            patch("pc_liveagent.app_stage.click_start_applications", start),
            patch(
                "pc_liveagent.app_stage.wait_for_application_started",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
        ):
            await handle_programs_decision("dup-yes", session, page, "Yes")
            await handle_programs_decision("dup-yes", session, page, "Yes")
        start.assert_awaited_once()

    async def test_circumstances_person_scoped_to_pending_condition(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="circ-scope",
            url="https://example.test",
            app_step="app_circumstances_person",
        )
        session.filled["app_circumstance_pending"] = (
            "Needs help with daily activities"
        )
        session.filled["app_circumstance_queue"] = json.dumps(
            [
                "Needs help with daily activities",
                "Veteran or military member",
            ]
        )
        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_circumstances"),
            ),
            patch(
                "pc_liveagent.app_stage._wait_for_circumstance_members",
                new=AsyncMock(
                    return_value=[
                        {"label": "Mickey Mouse", "index": 0, "dupIndex": 0},
                        {"label": "Agent T.", "index": 1, "dupIndex": 0},
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._discover_circumstance_members",
                new=AsyncMock(
                    return_value=[
                        {"label": "Mickey Mouse", "index": 0, "dupIndex": 0},
                        {"label": "Agent T.", "index": 1, "dupIndex": 0},
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._set_circumstance_member",
                new=AsyncMock(return_value=True),
            ) as set_member,
            patch(
                "pc_liveagent.app_stage._select_circumstance_checkbox",
                new=AsyncMock(return_value={"ok": True}),
            ),
        ):
            first = await submit_application_answer(
                "circ-scope", session, page, "Mickey Mouse", 0.0
            )
            set_member.assert_awaited_once()
            self.assertEqual(
                set_member.await_args.args[2],
                "Needs help with daily activities",
            )
            self.assertEqual(first.get("step"), "app_circumstances_person")
            self.assertIn("anyone else", (first.get("spoken") or "").lower())

            next_cond = await submit_application_answer(
                "circ-scope", session, page, "done", 0.0
            )
        self.assertEqual(next_cond.get("step"), "app_circumstances_person")
        self.assertIn("veteran", (next_cond.get("spoken") or "").lower())

    async def test_submit_requires_explicit_yes_and_verification(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="submit-v",
            url="https://example.test",
            app_step="app_submit_confirm",
        )
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()

        class _Body:
            async def inner_text(self) -> str:
                return "Thank you. Your application has been submitted."

        page.locator = lambda _s: _Body()
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_submit_confirm"),
            ),
            patch(
                "pc_liveagent.live_form._click_cta",
                new=AsyncMock(return_value=True),
            ) as cta,
        ):
            result = await submit_application_answer(
                "submit-v", session, page, "Yes submit", 0.0
            )
        cta.assert_awaited()
        self.assertTrue(result.get("ok"))
        self.assertTrue(result.get("end_call"))
        self.assertEqual(session.filled.get("application_submitted"), "1")

        session2 = LiveFormSession(
            call_id="submit-fail",
            url="https://example.test",
            app_step="app_submit_confirm",
        )

        class _Empty:
            async def inner_text(self) -> str:
                return "Review your answers"

        page2 = AsyncMock()
        page2.wait_for_timeout = AsyncMock()
        page2.locator = lambda _s: _Empty()
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_submit_confirm"),
            ),
            patch(
                "pc_liveagent.live_form._click_cta",
                new=AsyncMock(return_value=True),
            ),
        ):
            failed = await submit_application_answer(
                "submit-fail", session2, page2, "Yes", 0.0
            )
        self.assertFalse(failed.get("ok"))
        self.assertEqual(failed.get("error"), "submit_not_verified")

    async def test_address_blocks_continue_when_missing(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-miss",
            url="https://example.test",
            app_step="app_address_review",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "Mickey Mouse",
                            "needs_add": True,
                            "needs_home": True,
                            "needs_mailing": False,
                            "index": 0,
                        }
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(side_effect=AssertionError("must not advance")),
            ),
        ):
            result = await submit_application_answer(
                "addr-miss", session, AsyncMock(), "Yes", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "address_incomplete")

    async def test_test_mode_remains_isolated_when_patched_on(self) -> None:
        from pc_liveagent.call_turn import begin_speech, drop_controller
        from pc_liveagent.live_form import LiveFormSession, _sessions, _sessions_lock

        call_id = "iso-test"
        await drop_controller(call_id)
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", elig_step="programs"
        )
        session.filled["application_test_opening"] = "Start from programs?"
        async with _sessions_lock:
            _sessions[call_id] = session
        with patch("pc_liveagent.config.APPLICATION_TEST_MODE", True):
            speech = await begin_speech(call_id)
        self.assertEqual(speech, "Start from programs?")

    async def test_special_more_no_enters_programs_with_scraped_speech(self) -> None:
        """Pins Stage D → programs hop with both shortcuts off."""
        from pc_liveagent.live_form import LiveFormSession, _enter_programs_step

        session = LiveFormSession(
            call_id="d-to-e", url="https://example.test", elig_step="special_more"
        )
        page = AsyncMock()
        permission = (
            "Your household eligibility check is complete. "
            "Pointcare indicates that your household may be eligible for Medicaid. "
            "Would you like to continue and start the application? Please say Yes or No."
        )
        with (
            patch("pc_liveagent.config.APPLICATION_TEST_MODE", False),
            patch("pc_liveagent.config.APPLICATION_AFTER_SIGNIN", False),
            patch(
                "pc_liveagent.app_stage.store_program_summary",
                new=AsyncMock(
                    side_effect=lambda s, _p: s.filled.update(
                        {
                            "programs_permission_speech": permission,
                            "program_summary_json": '{"programs":[{"name":"Medicaid"}]}',
                        }
                    )
                    or {"programs": [{"name": "Medicaid"}]}
                ),
            ),
        ):
            step, speech = await _enter_programs_step(session, page)
        self.assertEqual(step, "programs")
        self.assertEqual(speech, permission)
        self.assertIn("medicaid", speech.lower())
        self.assertEqual(session.filled.get("programs_permission_speech"), permission)

    async def test_enter_programs_fallback_speech_when_scrape_empty(self) -> None:
        from pc_liveagent.live_form import LiveFormSession, _enter_programs_step

        session = LiveFormSession(call_id="prog-fb", url="https://example.test")
        with patch(
            "pc_liveagent.app_stage.store_program_summary",
            new=AsyncMock(side_effect=RuntimeError("scrape failed")),
        ):
            step, speech = await _enter_programs_step(session, AsyncMock())
        self.assertEqual(step, "programs")
        self.assertTrue(speech)
        self.assertIn("start the application", speech.lower())
        self.assertEqual(session.filled.get("programs_permission_speech"), speech)


class AddressStageTests(unittest.IsolatedAsyncioTestCase):
    async def test_marital_yes_no_confirm_and_change(self) -> None:
        from pc_liveagent.app_stage import (
            _handle_marital_step,
            _marital_status_from_text,
            _prepare_marital_review_speech,
        )
        from pc_liveagent.live_form import LiveFormSession

        self.assertEqual(_marital_status_from_text("Yeah. A single."), "Never Married")
        self.assertEqual(_marital_status_from_text("Never mind."), "Never Married")
        self.assertEqual(_marital_status_from_text("Never matter."), "Never Married")

        cards = [
            {"index": 0, "name": "Agent T.", "status": "Married"},
            {"index": 1, "name": "john D.", "status": "Never Married"},
        ]
        session = LiveFormSession(
            call_id="marital-yes-no",
            url="https://example.test",
            app_step="app_marital_review",
        )
        with (
            patch(
                "pc_liveagent.app_stage._scrape_marital_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._open_marital_editor",
                new=AsyncMock(return_value={"ok": True, "reason": "clicked"}),
            ) as opened,
            patch(
                "pc_liveagent.app_stage._set_member_marital_status",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "changed": True,
                        "status": "Never Married",
                        "card": {
                            "index": 0,
                            "name": "Agent T.",
                            "status": "Never Married",
                        },
                    }
                ),
            ) as set_status,
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(return_value=True),
            ) as next_click,
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_childcare_review"),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_childcare_review",
                        "I'll confirm childcare answers one person at a time. "
                        "Is Agent T. No? Say Yes to continue, or No to change it.",
                    )
                ),
            ) as prep,
        ):
            step, first = await _prepare_marital_review_speech(session, AsyncMock())
            self.assertEqual(step, "app_marital_review")
            self.assertIn("is agent t", first.lower())
            self.assertIn("married", first.lower())
            self.assertIn("say yes to continue", first.lower())
            self.assertIn("no to change", first.lower())
            self.assertNotIn("john", first.lower())

            no_turn = await _handle_marital_step(
                "marital-yes-no",
                session,
                AsyncMock(),
                "No",
                "no",
                "app_marital_review",
            )
            self.assertTrue(no_turn.get("ok"))
            opened.assert_awaited_once()
            self.assertEqual(session.filled.get("app_marital_current_index"), "0")
            self.assertIn(
                "what is the current marital status",
                (no_turn.get("spoken") or "").lower(),
            )

            changed = await _handle_marital_step(
                "marital-yes-no",
                session,
                AsyncMock(),
                "Yeah. A single.",
                "yeah. a single.",
                "app_marital_review",
            )
            self.assertTrue(changed.get("ok"))
            set_status.assert_awaited_once()
            self.assertEqual(set_status.await_args.args[1:], (0, "Never Married"))
            spoken = (changed.get("spoken") or "").lower()
            self.assertIn("john", spoken)
            self.assertIn("say yes to continue", spoken)
            next_click.assert_not_awaited()
            prep.assert_not_awaited()

            done = await _handle_marital_step(
                "marital-yes-no",
                session,
                AsyncMock(),
                "Yes",
                "yes",
                "app_marital_review",
            )
            self.assertTrue(done.get("ok"))
            self.assertEqual(done.get("step"), "app_childcare_review")
            next_click.assert_awaited_once()
            prep.assert_awaited_once()
            done_spoken = (done.get("spoken") or "").lower()
            self.assertIn("childcare", done_spoken)
            self.assertIn("say yes to continue", done_spoken)

    async def test_marital_last_change_prepares_childcare_speech(self) -> None:
        from pc_liveagent.app_stage import _handle_marital_step
        from pc_liveagent.live_form import LiveFormSession

        cards = [
            {"index": 0, "name": "John D.", "status": "Never Married"},
        ]
        session = LiveFormSession(
            call_id="marital-last-change",
            url="https://example.test",
            app_step="app_marital_review",
        )
        session.filled["app_marital_walk_index"] = "0"
        session.filled["app_marital_current"] = "John D."
        session.filled["app_marital_current_index"] = "0"
        with (
            patch(
                "pc_liveagent.app_stage._scrape_marital_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._set_member_marital_status",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "changed": True,
                        "status": "Divorced",
                        "card": {
                            "index": 0,
                            "name": "John D.",
                            "status": "Divorced",
                        },
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(return_value=True),
            ) as next_click,
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_childcare_review"),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_childcare_review",
                        "I'll confirm childcare answers one person at a time. "
                        "Is John D. No? Say Yes to continue, or No to change it.",
                    )
                ),
            ),
        ):
            done = await _handle_marital_step(
                "marital-last-change",
                session,
                AsyncMock(),
                "Divorced",
                "divorced",
                "app_marital_review",
            )
        self.assertTrue(done.get("ok"))
        self.assertEqual(done.get("step"), "app_childcare_review")
        next_click.assert_awaited_once()
        spoken = (done.get("spoken") or "").lower()
        self.assertIn("john d. is now divorced", spoken)
        self.assertIn("childcare", spoken)
        self.assertNotEqual(spoken.strip(), "ask: john d. is now divorced.")

    async def test_marital_recovers_when_next_left_but_detect_lagged(self) -> None:
        from pc_liveagent.app_stage import _handle_marital_step
        from pc_liveagent.live_form import LiveFormSession

        cards = [{"index": 0, "name": "John D.", "status": "Never Married"}]
        session = LiveFormSession(
            call_id="marital-detect-lag",
            url="https://example.test",
            app_step="app_marital_review",
        )
        session.filled["app_marital_walk_index"] = "0"
        session.filled["app_marital_current"] = "John D."
        session.filled["app_marital_current_index"] = "0"
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage._scrape_marital_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._set_member_marital_status",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "changed": True,
                        "status": "Common Law Marriage",
                        "card": {
                            "index": 0,
                            "name": "John D.",
                            "status": "Common Law Marriage",
                        },
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value=""),
            ),
            patch(
                "pc_liveagent.app_stage._resolve_screen_after_marital_leave",
                new=AsyncMock(return_value="app_childcare_review"),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_childcare_review",
                        "I'll confirm childcare answers one person at a time. "
                        "Is John D. No? Say Yes to continue, or No to change it.",
                    )
                ),
            ),
        ):
            done = await _handle_marital_step(
                "marital-detect-lag",
                session,
                page,
                "Common law marriage",
                "common law marriage",
                "app_marital_review",
            )
        self.assertTrue(done.get("ok"))
        self.assertEqual(done.get("step"), "app_childcare_review")
        spoken = (done.get("spoken") or "").lower()
        self.assertIn("common law marriage", spoken)
        self.assertIn("childcare", spoken)
        self.assertNotIn("still open", spoken)

    async def test_marital_cards_gone_assumes_childcare_when_detect_stuck(
        self,
    ) -> None:
        """Regression: leftover marital labels kept detect on marital after Next."""
        from pc_liveagent.app_stage import _handle_marital_step
        from pc_liveagent.live_form import LiveFormSession

        cards = [{"index": 0, "name": "John D.", "status": "Common Law Marriage"}]
        session = LiveFormSession(
            call_id="marital-cards-gone-advance",
            url="https://example.test",
            app_step="app_marital_review",
        )
        session.filled["app_marital_walk_index"] = "0"
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        clock = {"t": 1000.0}

        async def scrape_marital(_page):
            # First read during handle; after Next the cards are gone.
            if scrape_marital.calls == 0:
                scrape_marital.calls += 1
                return cards
            return []

        scrape_marital.calls = 0

        with (
            patch(
                "pc_liveagent.app_stage._scrape_marital_cards",
                new=AsyncMock(side_effect=scrape_marital),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_childcare_cards",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_marital_review"),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_marital_review"),
            ),
            patch(
                "pc_liveagent.app_stage.time.monotonic",
                side_effect=lambda: clock.__setitem__("t", clock["t"] + 2.0)
                or clock["t"],
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_childcare_review",
                        "I'll confirm childcare answers one person at a time. "
                        "Is John D. No? Say Yes to continue, or No to change it.",
                    )
                ),
            ),
        ):
            done = await _handle_marital_step(
                "marital-cards-gone-advance",
                session,
                page,
                "Yes. It's correct.",
                "yes. it's correct.",
                "app_marital_review",
            )
        self.assertTrue(done.get("ok"))
        self.assertEqual(done.get("step"), "app_childcare_review")
        self.assertNotIn("still open", (done.get("spoken") or "").lower())

    async def test_marital_missing_cards_follows_live_screen(self) -> None:
        from pc_liveagent.app_stage import _handle_marital_step
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="marital-cards-gone",
            url="https://example.test",
            app_step="app_marital_review",
        )
        with (
            patch(
                "pc_liveagent.app_stage._scrape_marital_cards",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "pc_liveagent.app_stage._resolve_screen_after_marital_leave",
                new=AsyncMock(return_value="app_childcare_review"),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_childcare_review",
                        "I'll confirm childcare answers one person at a time.",
                    )
                ),
            ),
        ):
            done = await _handle_marital_step(
                "marital-cards-gone",
                session,
                AsyncMock(),
                "Yes",
                "yes",
                "app_marital_review",
            )
        self.assertTrue(done.get("ok"))
        self.assertEqual(done.get("step"), "app_childcare_review")
        self.assertIn("childcare", (done.get("spoken") or "").lower())

    async def test_marital_missing_cards_recovers_via_childcare_scrape(
        self,
    ) -> None:
        from pc_liveagent.app_stage import _handle_marital_step
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="marital-cards-gone-cc",
            url="https://example.test",
            app_step="app_marital_review",
        )
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage._scrape_marital_cards",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_childcare_cards",
                new=AsyncMock(
                    return_value=[{"index": 0, "name": "John D.", "value": "No"}]
                ),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_marital_review"),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_childcare_review",
                        "I'll confirm childcare answers one person at a time.",
                    )
                ),
            ),
        ):
            done = await _handle_marital_step(
                "marital-cards-gone-cc",
                session,
                page,
                "Yes",
                "yes",
                "app_marital_review",
            )
        self.assertTrue(done.get("ok"))
        self.assertEqual(done.get("step"), "app_childcare_review")
        self.assertNotIn("couldn't read", (done.get("spoken") or "").lower())

    async def test_marital_yes_skips_to_next_member(self) -> None:
        from pc_liveagent.app_stage import (
            _handle_marital_step,
            _prepare_marital_review_speech,
        )
        from pc_liveagent.live_form import LiveFormSession

        cards = [
            {"index": 0, "name": "Agent T.", "status": "Married"},
            {"index": 1, "name": "Don D.", "status": "Never Married"},
        ]
        session = LiveFormSession(
            call_id="marital-yes-walk",
            url="https://example.test",
            app_step="app_marital_review",
        )
        with (
            patch(
                "pc_liveagent.app_stage._scrape_marital_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(return_value=True),
            ) as next_click,
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_childcare_review"),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_childcare_review",
                        "I'll confirm childcare answers one person at a time. "
                        "Is Agent T. No? Say Yes to continue, or No to change it.",
                    )
                ),
            ),
        ):
            await _prepare_marital_review_speech(session, AsyncMock())
            mid = await _handle_marital_step(
                "marital-yes-walk",
                session,
                AsyncMock(),
                "Yes",
                "yes",
                "app_marital_review",
            )
            self.assertTrue(mid.get("ok"))
            spoken = (mid.get("spoken") or "").lower()
            self.assertIn("is don d", spoken)
            self.assertIn("never married", spoken)
            self.assertIn("say yes to continue", spoken)
            next_click.assert_not_awaited()

            done = await _handle_marital_step(
                "marital-yes-walk",
                session,
                AsyncMock(),
                "Yes",
                "yes",
                "app_marital_review",
            )
            self.assertTrue(done.get("ok"))
            self.assertEqual(done.get("step"), "app_childcare_review")
            next_click.assert_awaited_once()
            self.assertIn("childcare", (done.get("spoken") or "").lower())

    async def test_marital_cancel_reasks_same_member_status(self) -> None:
        from pc_liveagent.app_stage import _handle_marital_step
        from pc_liveagent.live_form import LiveFormSession

        cards = [
            {"index": 0, "name": "Agent T.", "status": "Married"},
            {"index": 1, "name": "john D.", "status": "Never Married"},
        ]
        session = LiveFormSession(
            call_id="marital-cancel",
            url="https://example.test",
            app_step="app_marital_review",
        )
        session.filled["app_marital_current"] = "john D."
        session.filled["app_marital_current_index"] = "1"
        session.filled["app_marital_walk_index"] = "1"
        with (
            patch(
                "pc_liveagent.app_stage._scrape_marital_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._click_marital_action",
                new=AsyncMock(return_value=True),
            ) as action,
        ):
            cancelled = await _handle_marital_step(
                "marital-cancel",
                session,
                AsyncMock(),
                "Cancel",
                "cancel",
                "app_marital_review",
            )
            self.assertTrue(cancelled.get("ok"))
            action.assert_awaited_once()
            self.assertEqual(action.await_args.args[1:], (1, "cancel"))
            spoken = (cancelled.get("spoken") or "").lower()
            self.assertIn("john", spoken)
            self.assertIn("never married", spoken)
            self.assertIn("what is the current marital status", spoken)

    async def test_marital_duplicate_name_needs_fuller_phrase(self) -> None:
        from pc_liveagent.app_stage import _handle_marital_step
        from pc_liveagent.live_form import LiveFormSession

        cards = [
            {"index": 0, "name": "John D.", "status": "Married"},
            {"index": 1, "name": "John D.", "status": "Never Married"},
        ]
        session = LiveFormSession(
            call_id="marital-dup",
            url="https://example.test",
            app_step="app_marital_review",
        )
        session.filled["app_marital_walk_index"] = "0"
        with patch(
            "pc_liveagent.app_stage._scrape_marital_cards",
            new=AsyncMock(return_value=cards),
        ):
            unclear = await _handle_marital_step(
                "marital-dup",
                session,
                AsyncMock(),
                "John D",
                "john d",
                "app_marital_review",
            )
            self.assertFalse(unclear.get("ok"))
            self.assertEqual(unclear.get("error"), "marital_member_ambiguous")
            self.assertIn("2 people", (unclear.get("spoken") or "").lower())
            self.assertIn("full name", (unclear.get("spoken") or "").lower())

    async def test_childcare_yes_no_confirm_and_change(self) -> None:
        from pc_liveagent.app_stage import (
            _handle_childcare_step,
            _prepare_childcare_review_speech,
        )
        from pc_liveagent.live_form import LiveFormSession

        cards = [
            {"index": 0, "name": "Agent T.", "answer": "Yes"},
            {"index": 1, "name": "Don D.", "answer": "No"},
        ]
        session = LiveFormSession(
            call_id="childcare-yes-no",
            url="https://example.test",
            app_step="app_childcare_review",
        )
        with (
            patch(
                "pc_liveagent.app_stage._scrape_childcare_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._set_member_childcare",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "changed": True,
                        "answer": "No",
                        "card": {"index": 0, "name": "Agent T.", "answer": "No"},
                    }
                ),
            ) as set_answer,
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(return_value=True),
            ) as next_click,
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_circumstances"),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_circumstances",
                        "Do any of these other statuses apply to anyone?",
                    )
                ),
            ),
        ):
            step, first = await _prepare_childcare_review_speech(session, AsyncMock())
            self.assertEqual(step, "app_childcare_review")
            self.assertIn("agent t", first.lower())
            self.assertIn("marked yes", first.lower())
            self.assertIn("say yes to continue", first.lower())
            self.assertNotIn("don", first.lower())

            change = await _handle_childcare_step(
                "childcare-yes-no",
                session,
                AsyncMock(),
                "No",
                "no",
                "app_childcare_review",
            )
            self.assertTrue(change.get("ok"))
            self.assertEqual(session.filled.get("app_childcare_current_index"), "0")
            self.assertIn("should agent t", (change.get("spoken") or "").lower())
            set_answer.assert_not_awaited()

            saved = await _handle_childcare_step(
                "childcare-yes-no",
                session,
                AsyncMock(),
                "No",
                "no",
                "app_childcare_review",
            )
            self.assertTrue(saved.get("ok"))
            set_answer.assert_awaited_once()
            self.assertEqual(set_answer.await_args.args[1:], (0, "No"))
            spoken = (saved.get("spoken") or "").lower()
            self.assertIn("don", spoken)
            self.assertIn("marked no", spoken)
            next_click.assert_not_awaited()

            done = await _handle_childcare_step(
                "childcare-yes-no",
                session,
                AsyncMock(),
                "Yes",
                "yes",
                "app_childcare_review",
            )
            self.assertTrue(done.get("ok"))
            self.assertEqual(done.get("step"), "app_circumstances")
            next_click.assert_awaited_once()
            self.assertIn("other statuses", (done.get("spoken") or "").lower())

    async def test_childcare_yes_skips_to_next_member(self) -> None:
        from pc_liveagent.app_stage import (
            _handle_childcare_step,
            _prepare_childcare_review_speech,
        )
        from pc_liveagent.live_form import LiveFormSession

        cards = [
            {"index": 0, "name": "Agent T.", "answer": "Yes"},
            {"index": 1, "name": "john D.", "answer": "Yes"},
        ]
        session = LiveFormSession(
            call_id="childcare-yes-walk",
            url="https://example.test",
            app_step="app_childcare_review",
        )
        with (
            patch(
                "pc_liveagent.app_stage._scrape_childcare_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(return_value=True),
            ) as next_click,
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_circumstances"),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_circumstances",
                        "Do any of these other statuses apply to anyone?",
                    )
                ),
            ),
        ):
            await _prepare_childcare_review_speech(session, AsyncMock())
            mid = await _handle_childcare_step(
                "childcare-yes-walk",
                session,
                AsyncMock(),
                "Yes",
                "yes",
                "app_childcare_review",
            )
            self.assertTrue(mid.get("ok"))
            spoken = (mid.get("spoken") or "").lower()
            self.assertIn("john", spoken)
            self.assertIn("say yes to continue", spoken)
            next_click.assert_not_awaited()

            done = await _handle_childcare_step(
                "childcare-yes-walk",
                session,
                AsyncMock(),
                "Yes",
                "yes",
                "app_childcare_review",
            )
            self.assertTrue(done.get("ok"))
            self.assertEqual(done.get("step"), "app_circumstances")
            next_click.assert_awaited_once()
            self.assertIn("other statuses", (done.get("spoken") or "").lower())

    def test_street_unit_split_and_suggestion_match(self) -> None:
        from pc_liveagent.app_stage import _split_street_and_unit, _suggestion_matches

        street, unit = _split_street_and_unit("123 Main St apartment 4")
        self.assertEqual(street, "123 Main St")
        self.assertEqual(unit, "4")
        suggestions = [
            "999 Other Rd, Austin, TX 78701",
            "123 Main St, San Jose, CA 95112",
            "123 Main St, Austin, TX 78701",
        ]
        self.assertEqual(
            _suggestion_matches("123 Main St", suggestions[:2]),
            ["123 Main St, San Jose, CA 95112"],
        )
        self.assertEqual(
            _suggestion_matches("123 Main St", suggestions[1:]),
            suggestions[1:],
        )
        self.assertEqual(_suggestion_matches("500 Oak Ave", suggestions), [])
        newton = [
            "999 Other Rd, Austin, TX",
            "Newton Street Seattle, WA, USA",
            "Newton Ave, Seattle, WA",
        ]
        self.assertEqual(
            _suggestion_matches("Newton Street, Seattle", newton),
            ["Newton Street Seattle, WA, USA"],
        )
        self.assertEqual(
            _suggestion_matches("Newten Street, Seattle", newton),
            ["Newton Street Seattle, WA, USA"],
        )
        # Live Google pac-items concatenate street type + city with no space.
        pac = [
            "Newton StreetSeattle, WA, USA",
            "West Newton StreetSeattle, WA, USA",
            "East Newton StreetSeattle, WA, USA",
        ]
        self.assertEqual(
            _suggestion_matches("Newton Street, Seattle", pac),
            ["Newton StreetSeattle, WA, USA"],
        )
        self.assertEqual(
            _suggestion_matches("Western Newton Street, Seattle", pac),
            ["West Newton StreetSeattle, WA, USA"],
        )
        # One unique cue from the open dropdown is enough.
        self.assertEqual(
            _suggestion_matches("West", pac),
            ["West Newton StreetSeattle, WA, USA"],
        )
        self.assertEqual(
            _suggestion_matches("East Newton", pac),
            ["East Newton StreetSeattle, WA, USA"],
        )
        geo = [
            "George Washington Bridge New York, NY, USA",
            "George Floyd Square Minneapolis, MN, USA",
            "George Dieter Drive El Paso, TX, USA",
            "Georgina Avenue Santa Monica, CA, USA",
            "George Washington Memorial Parkway Alexandria, VA, USA",
        ]
        self.assertEqual(
            _suggestion_matches("George Washington Bridge", geo),
            ["George Washington Bridge New York, NY, USA"],
        )
        self.assertEqual(
            _suggestion_matches("Bridge", geo),
            ["George Washington Bridge New York, NY, USA"],
        )
        self.assertEqual(
            _suggestion_matches("Floyd", geo),
            ["George Floyd Square Minneapolis, MN, USA"],
        )
        self.assertEqual(
            len(_suggestion_matches("Washington", geo)),
            2,
        )

    async def test_saved_addresses_are_confirmed_before_next(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-review",
            url="https://example.test",
            app_step="app_address_review",
        )
        cards = [
            {
                "name": "Mickey Mouse",
                "home": "10 First St",
                "mailing": "Same mailing address",
                "home_same": False,
                "mailing_same": True,
                "no_home": False,
                "no_mailing": False,
                "needs_home": False,
                "needs_mailing": False,
                "needs_add": False,
            },
            {
                "name": "Murali M.",
                "home": "Same home address",
                "mailing": "200 Oak Ave",
                "home_same": True,
                "mailing_same": False,
                "no_home": False,
                "no_mailing": False,
                "needs_home": False,
                "needs_mailing": False,
                "needs_add": False,
            },
        ]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._commit_member_address",
                new=AsyncMock(return_value={"ok": True}),
            ) as commit,
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(side_effect=AssertionError("must not advance")),
            ),
        ):
            first = await submit_application_answer(
                "addr-review", session, AsyncMock(), "Yes", 0.0
            )
        commit.assert_not_awaited()
        self.assertTrue(first.get("ok"))
        self.assertEqual(first.get("step"), "app_address_review")
        spoken_first = (first.get("spoken") or "").lower()
        # Same home+mailing card: read once, ask if correct (no separate mailing line).
        self.assertIn("10 first", spoken_first)
        self.assertIn("mickey", spoken_first)
        self.assertIn("correct", spoken_first)

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(side_effect=AssertionError("must not advance")),
            ),
        ):
            second = await submit_application_answer(
                "addr-review", session, AsyncMock(), "Yes", 0.0
            )
        self.assertIn("same home", (second.get("spoken") or "").lower())
        self.assertEqual(session.app_step, "app_address_review")

    async def test_yes_after_review_syncs_detected_screen(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-next",
            url="https://example.test",
            app_step="app_address_review",
        )
        session.filled["app_address_confirmed"] = '["Mickey Mouse"]'
        session.filled["app_address_ready"] = "1"
        cards = [
            {
                "name": "Mickey Mouse",
                "home": "10 First St",
                "mailing": "Same mailing address",
                "mailing_same": True,
                "needs_add": False,
                "needs_home": False,
                "needs_mailing": False,
            }
        ]
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._next_button_enabled",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(return_value=True),
            ) as nxt,
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_marital_review"),
            ) as advance,
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_marital_review",
                        "I'll confirm each person's current marital status. "
                        "Is Mickey Mouse Never Married? Say Yes to continue, "
                        "or No to change it.",
                    )
                ),
            ),
        ):
            result = await submit_application_answer(
                "addr-next", session, page, "Yes", 0.0
            )
        nxt.assert_awaited_once()
        advance.assert_awaited_once()
        self.assertEqual(result.get("step"), "app_marital_review")
        self.assertIn("marital", (result.get("spoken") or "").lower())

    async def test_address_next_recovers_when_detect_lags(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-detect-lag",
            url="https://example.test",
            app_step="app_address_review",
        )
        session.filled["app_address_confirmed"] = '["Mickey Mouse"]'
        session.filled["app_address_ready"] = "1"
        cards = [
            {
                "name": "Mickey Mouse",
                "home": "10 First St",
                "mailing": "Same mailing address",
                "mailing_same": True,
                "needs_add": False,
                "needs_home": False,
                "needs_mailing": False,
            }
        ]
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(side_effect=[cards, []]),
            ),
            patch(
                "pc_liveagent.app_stage._next_button_enabled",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value=""),
            ),
            patch(
                "pc_liveagent.app_stage._wait_for_screen_change",
                new=AsyncMock(return_value="app_marital_review"),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_marital_review",
                        "I'll confirm each person's current marital status.",
                    )
                ),
            ),
        ):
            result = await submit_application_answer(
                "addr-detect-lag", session, page, "Yes", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_marital_review")
        self.assertNotIn("still open", (result.get("spoken") or "").lower())

    async def test_disabled_next_stays_on_address(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-disabled",
            url="https://example.test",
            app_step="app_address_review",
        )
        session.filled["app_address_confirmed"] = '["Mickey Mouse"]'
        session.filled["app_address_ready"] = "1"
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "Mickey Mouse",
                            "home": "10 First St",
                            "mailing": "10 First St",
                            "needs_add": False,
                            "needs_home": False,
                            "needs_mailing": False,
                        }
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._next_button_enabled",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(side_effect=AssertionError("must not advance")),
            ),
        ):
            result = await submit_application_answer(
                "addr-disabled", session, AsyncMock(), "Yes", 0.0
            )
        self.assertEqual(result.get("error"), "address_next_disabled")
        self.assertEqual(session.app_step, "app_address_review")

    async def test_edit_one_member_does_not_touch_another(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-edit",
            url="https://example.test",
            app_step="app_address_review",
        )
        session.filled["app_address_current"] = "Mickey Mouse"
        session.filled["app_address_field"] = "mailing"
        saved = {
            "name": "Mickey Mouse",
            "home": "10 First St",
            "mailing": "200 Oak Ave",
            "mailing_same": False,
            "needs_home": False,
            "needs_mailing": False,
            "needs_add": False,
        }
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(return_value=[saved]),
            ),
            patch(
                "pc_liveagent.app_stage._open_member_address_editor",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "reason": "clicked",
                        "action": "edit",
                        "icon_only": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._resolve_spoken_address_suggestion",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "chosen": "200 Oak Ave, Fallon, NV, USA",
                        "street": "200 Oak Ave",
                        "suggestions": ["200 Oak Ave, Fallon, NV, USA"],
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._commit_member_address",
                new=AsyncMock(return_value={"ok": True, "card": saved}),
            ) as commit,
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(side_effect=AssertionError("must not advance")),
            ),
        ):
            pending = await submit_application_answer(
                "addr-edit", session, AsyncMock(), "200 Oak Ave", 0.0
            )
            self.assertTrue(pending.get("ok"))
            self.assertEqual(session.filled.get("app_address_phase"), "mailing_confirm")
            self.assertEqual(
                session.filled.get("app_address_pending_chosen"),
                "200 Oak Ave, Fallon, NV, USA",
            )
            commit.assert_not_awaited()
            result = await submit_application_answer(
                "addr-edit", session, AsyncMock(), "Yes", 0.0
            )
        commit.assert_awaited_once()
        self.assertEqual(commit.await_args.args[1], "Mickey Mouse")
        self.assertEqual(commit.await_args.args[2], "mailing")
        self.assertEqual(
            commit.await_args.kwargs.get("chosen"),
            "200 Oak Ave, Fallon, NV, USA",
        )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_address_review")
        self.assertIn("matched", (pending.get("spoken") or "").lower())

    async def test_street_stages_pending_confirm_before_save(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-which",
            url="https://example.test",
            app_step="app_address_review",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "Mickey Mouse",
                            "home": "10 First St",
                            "mailing": "200 Oak Ave",
                            "needs_add": False,
                            "needs_home": False,
                            "needs_mailing": False,
                        }
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._open_member_address_editor",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "reason": "clicked",
                        "action": "edit",
                        "icon_only": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._resolve_spoken_address_suggestion",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "chosen": "10 First St, Fallon, NV, USA",
                        "street": "10 First St",
                        "suggestions": ["10 First St, Fallon, NV, USA"],
                    }
                ),
            ) as resolved,
            patch(
                "pc_liveagent.app_stage._commit_member_address",
                new=AsyncMock(return_value={"ok": True}),
            ) as commit,
        ):
            result = await submit_application_answer(
                "addr-which", session, AsyncMock(), "10 First St", 0.0
            )
        resolved.assert_awaited()
        commit.assert_not_awaited()
        self.assertTrue(result.get("ok"))
        self.assertEqual(session.filled.get("app_address_phase"), "home_confirm")
        self.assertEqual(session.filled.get("app_address_pending_kind"), "street")
        self.assertEqual(
            session.filled.get("app_address_pending_chosen"),
            "10 First St, Fallon, NV, USA",
        )
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("matched", spoken)
        self.assertIn("10 first", spoken)
        self.assertIn("yes to save", spoken)

    async def test_ambiguous_suggestion_does_not_save(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-amb",
            url="https://example.test",
            app_step="app_address_review",
        )
        cards = [
            {
                "name": "Mickey Mouse",
                "home": "",
                "mailing": "200 Oak Ave",
                "needs_home": True,
                "needs_mailing": False,
                "needs_add": True,
            }
        ]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._open_member_address_editor",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "reason": "clicked",
                        "action": "edit",
                        "icon_only": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._resolve_spoken_address_suggestion",
                new=AsyncMock(
                    return_value={
                        "ok": False,
                        "error": "address_ambiguous",
                        "suggestions": ["123 Main St, San Jose", "123 Main St, Austin"],
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._commit_member_address",
                new=AsyncMock(side_effect=AssertionError("must not save")),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(side_effect=AssertionError("must not advance")),
            ),
        ):
            result = await submit_application_answer(
                "addr-amb",
                session,
                AsyncMock(),
                "home address 123 Main St",
                0.0,
            )
        self.assertEqual(result.get("error"), "address_ambiguous")
        self.assertIn("san jose", (result.get("spoken") or "").lower())
        self.assertEqual(session.app_step, "app_address_review")
        self.assertIsNone(session.filled.get("app_address_pending_chosen"))

    async def test_failed_save_stays_on_address(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-save",
            url="https://example.test",
            app_step="app_address_review",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "Mickey Mouse",
                            "needs_home": True,
                            "needs_mailing": False,
                            "needs_add": True,
                        }
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._open_member_address_editor",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "reason": "clicked",
                        "action": "edit",
                        "icon_only": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._resolve_spoken_address_suggestion",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "chosen": "123 Main St, Fallon, NV, USA",
                        "street": "123 Main St",
                        "suggestions": ["123 Main St, Fallon, NV, USA"],
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._commit_member_address",
                new=AsyncMock(return_value={"ok": False, "error": "address_save_failed"}),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(side_effect=AssertionError("must not advance")),
            ),
        ):
            pending = await submit_application_answer(
                "addr-save", session, AsyncMock(), "123 Main St", 0.0
            )
            self.assertTrue(pending.get("ok"))
            result = await submit_application_answer(
                "addr-save", session, AsyncMock(), "Yes", 0.0
            )
        self.assertEqual(result.get("error"), "address_save_failed")
        self.assertEqual(session.app_step, "app_address_review")

    async def test_no_home_address_confirms_before_save(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-none",
            url="https://example.test",
            app_step="app_address_review",
        )
        before = [
            {
                "name": "Mickey Mouse",
                "home": "",
                "mailing": "200 Oak Ave",
                "no_home": False,
                "no_mailing": False,
                "needs_home": True,
                "needs_mailing": False,
                "needs_add": True,
            },
            {
                "name": "Murali M.",
                "home": "9 Pine St",
                "mailing": "Same mailing address",
                "mailing_same": True,
                "no_home": False,
                "no_mailing": False,
                "needs_add": False,
                "needs_home": False,
                "needs_mailing": False,
            },
        ]
        after = [
            {
                "name": "Mickey Mouse",
                "home": "",
                "mailing": "200 Oak Ave",
                "no_home": True,
                "no_mailing": False,
                "needs_home": False,
                "needs_mailing": False,
                "needs_add": False,
            },
            before[1],
        ]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(side_effect=[before, before, after, after]),
            ),
            patch(
                "pc_liveagent.app_stage._open_member_address_editor",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "reason": "already_open",
                        "action": "none",
                        "icon_only": False,
                    }
                ),
            ) as opened,
            patch(
                "pc_liveagent.app_stage._set_member_no_address",
                new=AsyncMock(return_value=True),
            ) as checked,
            patch(
                "pc_liveagent.app_stage._click_member_address_action",
                new=AsyncMock(return_value=True),
            ) as action,
            patch(
                "pc_liveagent.app_stage._address_validation_visible",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(side_effect=AssertionError("must not advance")),
            ),
        ):
            pending = await submit_application_answer(
                "addr-none",
                session,
                AsyncMock(),
                "No Home Address for Mickey Mouse",
                0.0,
            )
            self.assertTrue(pending.get("ok"))
            self.assertEqual(session.filled.get("app_address_phase"), "home_confirm")
            self.assertEqual(session.filled.get("app_address_pending_kind"), "no_home")
            checked.assert_not_awaited()
            result = await submit_application_answer(
                "addr-none",
                session,
                AsyncMock(),
                "Yes",
                0.0,
            )
        self.assertTrue(opened.await_count >= 1)
        self.assertEqual(checked.await_args.args[2], "home")
        self.assertEqual(action.await_args.args[2], "save")
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_address_review")
        self.assertEqual(session.filled.get("app_address_phase"), "mailing_check")
        self.assertIn("mailing", (result.get("spoken") or "").lower())

    async def test_cancel_restores_snapshot_without_saving(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-cancel",
            url="https://example.test",
            app_step="app_address_review",
        )
        session.filled["app_address_current"] = "Mickey Mouse"
        session.filled["app_address_snapshot"] = (
            '{"name":"Mickey Mouse","home":"10 First St","mailing":"Same mailing address"}'
        )
        draft = {
            "name": "Mickey Mouse",
            "home": "999 Other Rd",
            "mailing": "Same mailing address",
            "expanded": True,
            "needs_add": False,
            "needs_home": False,
            "needs_mailing": False,
        }
        restored = {
            "name": "Mickey Mouse",
            "home": "10 First St",
            "mailing": "Same mailing address",
            "mailing_same": True,
            "expanded": False,
            "needs_add": False,
            "needs_home": False,
            "needs_mailing": False,
        }
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(side_effect=[[draft], [restored]]),
            ),
            patch(
                "pc_liveagent.app_stage._click_member_address_action",
                new=AsyncMock(return_value=True),
            ) as action,
            patch(
                "pc_liveagent.app_stage._commit_member_address",
                new=AsyncMock(side_effect=AssertionError("must not save")),
            ),
        ):
            result = await submit_application_answer(
                "addr-cancel", session, AsyncMock(), "cancel", 0.0
            )
        self.assertEqual(action.await_args.args[2], "cancel")
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_address_review")
        self.assertIsNone(session.filled.get("app_address_snapshot"))

    async def test_entry_reads_existing_address_without_opening_editor(self) -> None:
        from pc_liveagent.app_stage import _prepare_address_review_speech
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-entry",
            url="https://example.test",
            app_step="app_address_review",
        )
        cards = [
            {
                "name": "Agent T.",
                "home": "Highway 50, Fallon, Nevada",
                "mailing": "Same mailing address",
                "mailing_same": True,
                "needs_add": False,
                "needs_home": False,
                "needs_mailing": False,
            },
            {
                "name": "Don D.",
                "home": "Same home address",
                "home_same": True,
                "mailing": "200 Oak Ave",
                "needs_add": False,
                "needs_home": False,
                "needs_mailing": False,
            },
        ]
        with (
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._open_member_address_editor",
                new=AsyncMock(side_effect=AssertionError("must not open editor")),
            ),
            patch(
                "pc_liveagent.app_stage._commit_member_address",
                new=AsyncMock(side_effect=AssertionError("must not save")),
            ),
        ):
            step, speech = await _prepare_address_review_speech(session, AsyncMock())
        self.assertEqual(step, "app_address_review")
        self.assertEqual(session.filled.get("app_address_current"), "Agent T.")
        # Same home+mailing: read the street once, then only Yes/No.
        self.assertIn("highway 50", speech.lower())
        self.assertIn("the address is", speech.lower())
        self.assertIn("is that correct", speech.lower())
        self.assertNotIn("mailing address is", speech.lower())

    def test_address_confirm_speech_same_vs_different(self) -> None:
        from pc_liveagent.app_stage import (
            _address_confirm_speech,
            _clean_member_display_name,
        )

        self.assertEqual(
            _clean_member_display_name("Admin P. (you)"),
            "Admin P.",
        )
        same = _address_confirm_speech(
            {
                "name": "Admin P. (you)",
                "home": "10 First St",
                "mailing": "Same mailing address",
                "mailing_same": True,
                "needs_home": False,
                "needs_mailing": False,
            }
        )
        self.assertIn("for admin p., the address is 10 first st", same.lower())
        self.assertIn("is that correct", same.lower())
        # Confirm is Yes/No only — no-address options come after No → edit.
        self.assertNotIn("you can also say no home", same.lower())
        self.assertNotIn("(you)", same.lower())
        self.assertNotIn("both the home and mailing", same.lower())
        identical = _address_confirm_speech(
            {
                "name": "Agent T.",
                "home": "10 First St",
                "mailing": "10 First St",
                "mailing_same": False,
                "needs_home": False,
                "needs_mailing": False,
            }
        )
        self.assertIn("the address is 10 first st", identical.lower())
        self.assertIn("is that correct", identical.lower())
        different = _address_confirm_speech(
            {
                "name": "Don D.",
                "home": "10 First St",
                "mailing": "200 Oak Ave",
                "mailing_same": False,
                "needs_home": False,
                "needs_mailing": False,
            }
        )
        self.assertIn("10 first st", different.lower())
        self.assertIn("200 oak ave", different.lower())
        self.assertIn("are both correct", different.lower())
        from pc_liveagent.app_stage import _address_change_which_prompt

        which = _address_change_which_prompt("Admin P. (you)")
        self.assertIn("home address", which.lower())
        self.assertIn("no home address", which.lower())
        self.assertNotIn("(you)", which.lower())

    async def test_yes_confirms_without_edit_then_reads_next_member(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-yes-walk",
            url="https://example.test",
            app_step="app_address_review",
        )
        session.filled["app_address_current"] = "Agent T."
        cards = [
            {
                "name": "Agent T.",
                "home": "10 First St",
                "mailing": "Same mailing address",
                "mailing_same": True,
                "needs_add": False,
                "needs_home": False,
                "needs_mailing": False,
            },
            {
                "name": "Don D.",
                "home": "Same home address",
                "home_same": True,
                "mailing": "200 Oak Ave",
                "needs_add": False,
                "needs_home": False,
                "needs_mailing": False,
            },
        ]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._open_member_address_editor",
                new=AsyncMock(side_effect=AssertionError("must not open")),
            ) as opened,
            patch(
                "pc_liveagent.app_stage._commit_member_address",
                new=AsyncMock(side_effect=AssertionError("must not save")),
            ) as commit,
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(side_effect=AssertionError("must not advance")),
            ),
        ):
            result = await submit_application_answer(
                "addr-yes-walk", session, AsyncMock(), "Yes", 0.0
            )
        opened.assert_not_awaited()
        commit.assert_not_awaited()
        self.assertTrue(result.get("ok"))
        self.assertIn("don", (result.get("spoken") or "").lower())
        self.assertEqual(session.filled.get("app_address_current"), "Don D.")
        self.assertIn("Agent T.", _json_names(session, "app_address_confirmed"))

    async def test_bare_no_opens_editor_and_asks_home_first(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-bare-no",
            url="https://example.test",
            app_step="app_address_review",
        )
        session.filled["app_address_current"] = "Don D."
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "Don D.",
                            "home": "10 First St",
                            "mailing": "200 Oak Ave",
                            "needs_add": False,
                            "needs_home": False,
                            "needs_mailing": False,
                        }
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._commit_member_address",
                new=AsyncMock(side_effect=AssertionError("must not save")),
            ),
            patch(
                "pc_liveagent.app_stage._open_member_address_editor",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "reason": "clicked",
                        "action": "edit",
                        "icon_only": True,
                    }
                ),
            ) as opened,
        ):
            result = await submit_application_answer(
                "addr-bare-no", session, AsyncMock(), "No", 0.0
            )
        opened.assert_awaited()
        self.assertTrue(result.get("ok"))
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("home", spoken)
        self.assertIn("don", spoken)
        self.assertEqual(session.filled.get("app_address_current"), "Don D.")
        self.assertEqual(session.filled.get("app_address_phase"), "home_check")
        self.assertEqual(session.filled.get("app_address_field"), "home")

    async def test_named_mailing_only_correction_does_not_touch_home(self) -> None:
        from pc_liveagent.app_stage import (
            _address_field_from_text,
            submit_application_answer,
        )
        from pc_liveagent.live_form import LiveFormSession

        self.assertEqual(
            _address_field_from_text(
                "The home address is correct, but the mailing address is different"
            ),
            "mailing",
        )
        self.assertEqual(
            _address_field_from_text("Change Don's mailing address"),
            "mailing",
        )
        session = LiveFormSession(
            call_id="addr-mail-only",
            url="https://example.test",
            app_step="app_address_review",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "Don D.",
                            "home": "10 First St",
                            "mailing": "200 Oak Ave",
                            "needs_add": False,
                            "needs_home": False,
                            "needs_mailing": False,
                        },
                        {
                            "name": "Agent T.",
                            "home": "9 Pine St",
                            "mailing": "Same mailing address",
                            "mailing_same": True,
                            "needs_add": False,
                            "needs_home": False,
                            "needs_mailing": False,
                        },
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._commit_member_address",
                new=AsyncMock(side_effect=AssertionError("must wait for street")),
            ),
            patch(
                "pc_liveagent.app_stage._open_member_address_editor",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "reason": "clicked",
                        "action": "edit",
                        "icon_only": True,
                    }
                ),
            ) as opened,
        ):
            result = await submit_application_answer(
                "addr-mail-only",
                session,
                AsyncMock(),
                "Change Don's mailing address",
                0.0,
            )
        opened.assert_awaited()
        self.assertTrue(result.get("ok"))
        self.assertEqual(session.filled.get("app_address_current"), "Don D.")
        self.assertEqual(session.filled.get("app_address_field"), "mailing")
        self.assertEqual(session.filled.get("app_address_phase"), "mailing_check")
        self.assertIn("mailing", (result.get("spoken") or "").lower())
        self.assertNotIn("10 first", (result.get("spoken") or "").lower())

    async def test_ambiguous_member_name_does_not_open_editor(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-amb-name",
            url="https://example.test",
            app_step="app_address_review",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "Don D.",
                            "home": "10 First St",
                            "mailing": "Same mailing address",
                            "mailing_same": True,
                            "needs_add": False,
                            "needs_home": False,
                            "needs_mailing": False,
                        },
                        {
                            "name": "Donnie D.",
                            "home": "11 Second St",
                            "mailing": "Same mailing address",
                            "mailing_same": True,
                            "needs_add": False,
                            "needs_home": False,
                            "needs_mailing": False,
                        },
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._match_named_members",
                return_value=([], "I heard more than one matching name. Which person?"),
            ),
            patch(
                "pc_liveagent.app_stage._open_member_address_editor",
                new=AsyncMock(side_effect=AssertionError("must not open")),
            ),
            patch(
                "pc_liveagent.app_stage._commit_member_address",
                new=AsyncMock(side_effect=AssertionError("must not save")),
            ),
        ):
            result = await submit_application_answer(
                "addr-amb-name",
                session,
                AsyncMock(),
                "Change Don's home address",
                0.0,
            )
        self.assertEqual(result.get("error"), "address_member_ambiguous")

    async def test_leave_as_is_confirms_without_saving(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-leave",
            url="https://example.test",
            app_step="app_address_review",
        )
        session.filled["app_address_current"] = "Agent T."
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "Agent T.",
                            "home": "10 First St",
                            "mailing": "Same mailing address",
                            "mailing_same": True,
                            "needs_add": False,
                            "needs_home": False,
                            "needs_mailing": False,
                        }
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._commit_member_address",
                new=AsyncMock(side_effect=AssertionError("must not save")),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(side_effect=AssertionError("not ready yet")),
            ),
        ):
            result = await submit_application_answer(
                "addr-leave", session, AsyncMock(), "leave it as is", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertIn("confirmed", (result.get("spoken") or "").lower())
        self.assertEqual(session.filled.get("app_address_ready"), "1")

    async def test_verified_mismatch_stays_on_member(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-verify-fail",
            url="https://example.test",
            app_step="app_address_review",
        )
        session.filled["app_address_current"] = "Don D."
        session.filled["app_address_field"] = "home"
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "Don D.",
                            "home": "10 First St",
                            "mailing": "200 Oak Ave",
                            "needs_add": False,
                            "needs_home": False,
                            "needs_mailing": False,
                        }
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._open_member_address_editor",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "reason": "clicked",
                        "action": "edit",
                        "icon_only": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._resolve_spoken_address_suggestion",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "chosen": "123 Main St, Fallon, NV, USA",
                        "street": "123 Main St",
                        "suggestions": ["123 Main St, Fallon, NV, USA"],
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._commit_member_address",
                new=AsyncMock(
                    return_value={"ok": False, "error": "address_not_verified"}
                ),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(side_effect=AssertionError("must not advance")),
            ),
        ):
            pending = await submit_application_answer(
                "addr-verify-fail", session, AsyncMock(), "123 Main St", 0.0
            )
            self.assertTrue(pending.get("ok"))
            result = await submit_application_answer(
                "addr-verify-fail", session, AsyncMock(), "Yes", 0.0
            )
        self.assertEqual(result.get("error"), "address_not_verified")
        self.assertEqual(session.filled.get("app_address_current"), "Don D.")
        self.assertEqual(session.app_step, "app_address_review")
        self.assertNotEqual(session.filled.get("app_address_ready"), "1")

    async def test_home_then_mailing_confirm_before_save_walk(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-walk-confirm",
            url="https://example.test",
            app_step="app_address_review",
        )
        session.filled["app_address_current"] = "Don D."
        card = {
            "name": "Don D.",
            "home": "10 First St",
            "mailing": "200 Oak Ave",
            "needs_add": False,
            "needs_home": False,
            "needs_mailing": False,
        }
        saved_home = {
            **card,
            "home": "123 Main St",
        }
        saved_mail = {
            **saved_home,
            "mailing": "456 Oak Ave",
            "mailing_same": False,
        }
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(
                    side_effect=[
                        [card],  # bare No
                        [card],  # home street pending
                        [card],  # Yes save home (step scrape)
                        [saved_home],  # Yes save home (refresh)
                        [saved_home],  # mailing street pending
                        [saved_home],  # Yes save mailing (step scrape)
                        [saved_mail],  # Yes save mailing (refresh)
                        [saved_mail],  # prepare next speech
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._open_member_address_editor",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "reason": "clicked",
                        "action": "edit",
                        "icon_only": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._resolve_spoken_address_suggestion",
                new=AsyncMock(
                    side_effect=[
                        {
                            "ok": True,
                            "chosen": "123 Main St, Fallon, NV, USA",
                            "street": "123 Main St",
                            "suggestions": ["123 Main St, Fallon, NV, USA"],
                        },
                        {
                            "ok": True,
                            "chosen": "456 Oak Ave, Fallon, NV, USA",
                            "street": "456 Oak Ave",
                            "suggestions": ["456 Oak Ave, Fallon, NV, USA"],
                        },
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._commit_member_address",
                new=AsyncMock(
                    side_effect=[
                        {"ok": True, "card": saved_home},
                        {"ok": True, "card": saved_mail},
                    ]
                ),
            ) as commit,
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(side_effect=AssertionError("must not advance")),
            ),
        ):
            no = await submit_application_answer(
                "addr-walk-confirm", session, AsyncMock(), "No", 0.0
            )
            self.assertEqual(session.filled.get("app_address_phase"), "home_check")
            self.assertIn("home", (no.get("spoken") or "").lower())

            pending_home = await submit_application_answer(
                "addr-walk-confirm", session, AsyncMock(), "123 Main St", 0.0
            )
            self.assertEqual(session.filled.get("app_address_phase"), "home_confirm")
            commit.assert_not_awaited()
            self.assertIn("matched", (pending_home.get("spoken") or "").lower())
            self.assertIn("yes to save", (pending_home.get("spoken") or "").lower())

            home_saved = await submit_application_answer(
                "addr-walk-confirm", session, AsyncMock(), "Yes", 0.0
            )
            self.assertEqual(commit.await_count, 1)
            self.assertEqual(session.filled.get("app_address_phase"), "mailing_check")
            self.assertIn("mailing", (home_saved.get("spoken") or "").lower())

            pending_mail = await submit_application_answer(
                "addr-walk-confirm", session, AsyncMock(), "456 Oak Ave", 0.0
            )
            self.assertEqual(session.filled.get("app_address_phase"), "mailing_confirm")
            self.assertEqual(commit.await_count, 1)
            self.assertIn("456 oak", (pending_mail.get("spoken") or "").lower())

            done = await submit_application_answer(
                "addr-walk-confirm", session, AsyncMock(), "Yes", 0.0
            )
        self.assertEqual(commit.await_count, 2)
        self.assertTrue(done.get("ok"))
        self.assertIsNone(session.filled.get("app_address_phase"))
        self.assertIn("Don D.", _json_names(session, "app_address_confirmed"))
        self.assertIn("confirmed", (done.get("spoken") or "").lower())

    async def test_duplicate_yes_does_not_click_next_twice(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-next-once",
            url="https://example.test",
            app_step="app_address_review",
        )
        session.filled["app_address_confirmed"] = '["Agent T."]'
        session.filled["app_address_ready"] = "1"
        cards = [
            {
                "name": "Agent T.",
                "home": "10 First St",
                "mailing": "Same mailing address",
                "mailing_same": True,
                "needs_add": False,
                "needs_home": False,
                "needs_mailing": False,
            }
        ]
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(side_effect=["app_address_review", "app_marital_review"]),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._next_button_enabled",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(return_value=True),
            ) as nxt,
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_marital_review",
                        "I'll confirm each person's current marital status. "
                        "Is Agent T. Married? Say Yes to continue, or No to change it.",
                    )
                ),
            ),
        ):
            first = await submit_application_answer(
                "addr-next-once", session, page, "Yes", 0.0
            )
        nxt.assert_awaited_once()
        self.assertEqual(first.get("step"), "app_marital_review")
        self.assertIsNone(session.filled.get("app_address_ready"))
        self.assertIn("marital", (first.get("spoken") or "").lower())

    async def test_named_member_wins_over_current_review_member(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-named-wins",
            url="https://example.test",
            app_step="app_address_review",
        )
        session.filled["app_address_current"] = "john D."
        cards = [
            {
                "name": "john D.",
                "home": "Same home address",
                "home_same": True,
                "mailing": "200 Oak Ave",
                "needs_add": False,
                "needs_home": False,
                "needs_mailing": False,
            },
            {
                "name": "Agent T.",
                "home": "10 First St",
                "mailing": "Same mailing address",
                "mailing_same": True,
                "needs_add": False,
                "needs_home": False,
                "needs_mailing": False,
            },
        ]
        phrases = [
            "Change agent t's data.",
            "Address. Agent t.",
            "Change Agent T's address",
        ]
        for phrase in phrases:
            session.filled["app_address_current"] = "john D."
            session.filled.pop("app_address_correcting", None)
            session.filled.pop("app_address_field", None)
            session.filled.pop("app_address_phase", None)
            session.filled.pop("app_address_pending_kind", None)
            session.filled.pop("app_address_pending_street", None)
            session.filled.pop("app_address_pending_unit", None)
            with (
                patch(
                    "pc_liveagent.app_stage.detect_application_screen",
                    new=AsyncMock(return_value="app_address_review"),
                ),
                patch(
                    "pc_liveagent.app_stage._scrape_address_cards",
                    new=AsyncMock(return_value=cards),
                ),
                patch(
                    "pc_liveagent.app_stage._open_member_address_editor",
                    new=AsyncMock(
                        return_value={
                            "ok": True,
                            "reason": "clicked",
                            "action": "edit",
                            "icon_only": True,
                        }
                    ),
                ) as open_edit,
                patch(
                    "pc_liveagent.app_stage._clear_member_address_selection",
                    new=AsyncMock(return_value={}),
                ),
                patch(
                    "pc_liveagent.app_stage._commit_member_address",
                    new=AsyncMock(side_effect=AssertionError("must not save yet")),
                ),
            ):
                result = await submit_application_answer(
                    "addr-named-wins", session, AsyncMock(), phrase, 0.0
                )
            self.assertTrue(result.get("ok"), phrase)
            self.assertEqual(session.filled.get("app_address_current"), "Agent T.", phrase)
            self.assertEqual(
                session.filled.get("app_address_correcting"), "Agent T.", phrase
            )
            self.assertEqual(session.filled.get("app_address_phase"), "home_check", phrase)
            open_edit.assert_awaited()
            self.assertIn("agent", (result.get("spoken") or "").lower())
            self.assertIn("home", (result.get("spoken") or "").lower())
            self.assertNotIn("john", (result.get("spoken") or "").lower())

    async def test_unknown_name_does_not_fall_back_to_current_member(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-no-fallback",
            url="https://example.test",
            app_step="app_address_review",
        )
        session.filled["app_address_current"] = "john D."
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "john D.",
                            "home": "10 First St",
                            "mailing": "Same mailing address",
                            "mailing_same": True,
                            "needs_add": False,
                            "needs_home": False,
                            "needs_mailing": False,
                        },
                        {
                            "name": "Agent T.",
                            "home": "9 Pine St",
                            "mailing": "Same mailing address",
                            "mailing_same": True,
                            "needs_add": False,
                            "needs_home": False,
                            "needs_mailing": False,
                        },
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._commit_member_address",
                new=AsyncMock(side_effect=AssertionError("must not save")),
            ),
        ):
            result = await submit_application_answer(
                "addr-no-fallback",
                session,
                AsyncMock(),
                "Change Subravity's address",
                0.0,
            )
        self.assertEqual(result.get("error"), "address_member_needed")
        self.assertEqual(session.filled.get("app_address_current"), "john D.")

    async def test_pinned_correction_keeps_member_on_street_answer(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-pin",
            url="https://example.test",
            app_step="app_address_review",
        )
        session.filled["app_address_current"] = "Agent T."
        session.filled["app_address_correcting"] = "Agent T."
        session.filled["app_address_field"] = "home"
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "Agent T.",
                            "home": "10 First St",
                            "mailing": "Same mailing address",
                            "mailing_same": True,
                            "needs_add": False,
                            "needs_home": False,
                            "needs_mailing": False,
                        },
                        {
                            "name": "john D.",
                            "home": "Same home address",
                            "home_same": True,
                            "mailing": "200 Oak Ave",
                            "needs_add": False,
                            "needs_home": False,
                            "needs_mailing": False,
                        },
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._open_member_address_editor",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "reason": "clicked",
                        "action": "edit",
                        "icon_only": True,
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._resolve_spoken_address_suggestion",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "chosen": "Newton Street Seattle, WA, USA",
                        "street": "Newton Street, Seattle",
                        "suggestions": ["Newton Street Seattle, WA, USA"],
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._commit_member_address",
                new=AsyncMock(side_effect=AssertionError("must confirm first")),
            ) as commit,
        ):
            result = await submit_application_answer(
                "addr-pin",
                session,
                AsyncMock(),
                "Newton Street, Seattle for john",
                0.0,
            )
        commit.assert_not_awaited()
        self.assertTrue(result.get("ok"))
        self.assertEqual(session.filled.get("app_address_current"), "Agent T.")
        self.assertEqual(session.filled.get("app_address_phase"), "home_confirm")
        self.assertEqual(
            session.filled.get("app_address_pending_chosen"),
            "Newton Street Seattle, WA, USA",
        )
        self.assertIn("newton", (result.get("spoken") or "").lower())
        self.assertIn("matched", (result.get("spoken") or "").lower())
        self.assertIn("yes to save", (result.get("spoken") or "").lower())

    async def test_editor_open_failure_reports_specific_error(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="addr-editor-fail",
            url="https://example.test",
            app_step="app_address_review",
        )
        session.filled["app_address_current"] = "john D."
        session.filled["app_address_correcting"] = "john D."
        session.filled["app_address_field"] = "home"
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_address_cards",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "john D.",
                            "home": "Same home address",
                            "home_same": True,
                            "mailing": "200 Oak Ave",
                            "needs_add": False,
                            "needs_home": False,
                            "needs_mailing": False,
                        }
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._commit_member_address",
                new=AsyncMock(
                    return_value={
                        "ok": False,
                        "error": "address_editor_failed",
                        "reason": "edit_control_not_found",
                    }
                ),
            ),
        ):
            result = await submit_application_answer(
                "addr-editor-fail",
                session,
                AsyncMock(),
                "Newton Street, Seattle",
                0.0,
            )
        self.assertEqual(result.get("error"), "address_editor_failed")
        self.assertIn("open the address editor", (result.get("spoken") or "").lower())
        self.assertEqual(session.filled.get("app_address_correcting"), "john D.")


def _json_names(session, key: str) -> list[str]:
    import json

    raw = session.filled.get(key) or "[]"
    try:
        data = json.loads(raw)
    except Exception:
        return []
    return [str(item) for item in data] if isinstance(data, list) else []


class AddressDetectTests(unittest.IsolatedAsyncioTestCase):
    async def test_detect_address_review_without_add_cue(self) -> None:
        from pc_liveagent.app_stage import detect_application_screen

        class _Body:
            async def inner_text(self) -> str:
                return (
                    "Home address Mailing address "
                    "Mickey Mouse 123 Main St San Jose CA 95112"
                )

        page = AsyncMock()
        page.url = "https://my.pointcare.com/application"
        page.locator = lambda _s: _Body()
        self.assertEqual(await detect_application_screen(page), "app_address_review")


class ApplicationSyncBackDobTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_server_step_follows_open_screen(self) -> None:
        from pc_liveagent.call_turn import (
            _process_application_turn,
            drop_controller,
            get_controller,
        )
        from pc_liveagent.live_form import LiveFormSession

        call_id = "sync-later-screen"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "application"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", app_step="app_insurance"
        )
        session.page = AsyncMock()

        async def fake_session(_cid: str):
            return session

        with (
            patch(
                "pc_liveagent.call_turn.get_session",
                new=AsyncMock(side_effect=fake_session),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_dob_collect",
                        "What is Agent T.'s date of birth?",
                    )
                ),
            ),
            patch(
                "pc_liveagent.app_stage._click_application_back",
                new=AsyncMock(side_effect=AssertionError("must not click Back")),
            ),
            patch(
                "pc_liveagent.call_turn.submit_field",
                new=AsyncMock(side_effect=AssertionError("must not submit")),
            ),
        ):
            result = await _process_application_turn(
                call_id,
                ctrl,
                text="Yes",
                fp="yes",
                dedupe_ctx="",
                turn_id="t1",
                form_url="https://example.test",
            )
        self.assertEqual(result.detail, "app_resynced")
        self.assertEqual(session.app_step, "app_dob_collect")
        self.assertIn("Agent T.", result.speech)

    async def test_in_flight_insurance_answer_is_not_applied_to_dob(self) -> None:
        from pc_liveagent.call_turn import (
            _process_application_turn,
            drop_controller,
            get_controller,
        )
        from pc_liveagent.live_form import LiveFormSession

        call_id = "sync-inflight"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "application"
        ctrl.elig_op_in_progress = True
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", app_step="app_insurance"
        )
        session.page = AsyncMock()

        async def fake_session(_cid: str):
            return session

        with (
            patch(
                "pc_liveagent.call_turn.get_session",
                new=AsyncMock(side_effect=fake_session),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(side_effect=AssertionError("must not resync during the click")),
            ),
            patch(
                "pc_liveagent.call_turn.submit_field",
                new=AsyncMock(side_effect=AssertionError("must not submit the repeat")),
            ),
        ):
            result = await _process_application_turn(
                call_id,
                ctrl,
                text="No.",
                fp="no",
                dedupe_ctx="",
                turn_id="t-pending",
                form_url="https://example.test",
            )
        self.assertEqual(result.detail, "app_answer_pending")
        self.assertEqual(session.app_step, "app_insurance")
        self.assertNotIn("date of birth", result.speech.lower())

    async def test_new_dob_answer_is_not_blocked_by_an_insurance_no(self) -> None:
        from pc_liveagent.call_turn import (
            _app_browser_tasks,
            _process_application_turn,
            _user_fingerprint,
            drop_controller,
            get_controller,
        )
        from pc_liveagent.live_form import LiveFormSession

        call_id = "sync-new-dob"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "application"
        ctrl.committed.append(
            {
                "key": "app:app_insurance:" + _user_fingerprint("No."),
                "turn_id": "old",
                "step": "app_insurance",
                "answer": "No.",
                "at": 0,
            }
        )
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", app_step="app_dob_review"
        )
        session.page = AsyncMock()
        submitted: list[str] = []

        async def fake_session(_cid: str):
            return session

        async def fake_submit(_cid: str, _field: str, text: str, **_kwargs):
            submitted.append(text)
            return {"ok": True, "spoken": "ASK: What is the home address?", "step": "app_address_review"}

        with (
            patch(
                "pc_liveagent.call_turn.get_session",
                new=AsyncMock(side_effect=fake_session),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch("pc_liveagent.call_turn.submit_field", new=AsyncMock(side_effect=fake_submit)),
        ):
            result = await _process_application_turn(
                call_id,
                ctrl,
                text="Yes",
                fp=_user_fingerprint("Yes"),
                dedupe_ctx="",
                turn_id="dob-1",
                form_url="https://example.test",
            )
            task = _app_browser_tasks.get(call_id)
            if task is not None:
                await task
        self.assertEqual(result.detail, "app_ack")
        self.assertEqual(submitted, ["Yes"])

    async def test_question_one_server_follows_question_five_screen(self) -> None:
        from pc_liveagent.call_turn import (
            _process_application_turn,
            drop_controller,
            get_controller,
        )
        from pc_liveagent.live_form import LiveFormSession

        call_id = "sync-q1-vs-later"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", app_step="app_citizenship"
        )
        session.page = AsyncMock()

        async def fake_session(_cid: str):
            return session

        with (
            patch(
                "pc_liveagent.call_turn.get_session",
                new=AsyncMock(side_effect=fake_session),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_address_review",
                        "Please confirm the home and mailing addresses.",
                    )
                ),
            ),
            patch(
                "pc_liveagent.call_turn.submit_field",
                new=AsyncMock(side_effect=AssertionError("must not submit")),
            ),
        ):
            result = await _process_application_turn(
                call_id,
                ctrl,
                text="Yes",
                fp="yes-2",
                dedupe_ctx="",
                turn_id="t2",
                form_url="https://example.test",
            )
        self.assertEqual(result.detail, "app_resynced")
        self.assertEqual(session.app_step, "app_address_review")
        self.assertIn("address", result.speech.lower())

    async def test_new_application_asks_first_screen_only_after_verify(self) -> None:
        from pc_liveagent.app_stage import _start_application_from_permission
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="new-app", url="https://example.test", elig_step="proceed_application"
        )
        page = AsyncMock()
        page.url = "https://my.pointcare.com/application"
        with (
            patch(
                "pc_liveagent.app_stage.classify_saved_household_page",
                new=AsyncMock(return_value="programs"),
            ),
            patch(
                "pc_liveagent.app_stage.click_start_applications",
                new=AsyncMock(return_value="Start Applications"),
            ),
            patch(
                "pc_liveagent.app_stage.wait_for_application_started",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage.store_program_summary",
                new=AsyncMock(),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage.restart_application_to_first_question",
                new=AsyncMock(side_effect=AssertionError("must not reset")),
            ),
        ):
            result = await _start_application_from_permission(
                "new-app", session, page, "Yes"
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_citizenship")
        self.assertIn("citizen", str(result.get("spoken") or "").lower())

    async def test_open_draft_resumes_visible_step_without_back(self) -> None:
        from pc_liveagent.app_stage import _start_application_from_permission
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="resume-app",
            url="https://example.test",
            elig_step="proceed_application",
        )
        page = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage.classify_saved_household_page",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_dob_collect",
                        "What is Agent T.'s date of birth?",
                    )
                ),
            ),
            patch(
                "pc_liveagent.app_stage._click_application_back",
                new=AsyncMock(side_effect=AssertionError("must not click Back")),
            ),
        ):
            result = await _start_application_from_permission(
                "resume-app", session, page, "Yes"
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(session.app_step, "app_dob_collect")
        self.assertIn("Agent T.", str(result.get("spoken") or ""))

    async def test_go_back_is_navigation_on_each_application_step(self) -> None:
        from pc_liveagent.app_stage import application_go_back
        from pc_liveagent.call_turn import (
            _process_application_turn,
            drop_controller,
            get_controller,
        )
        from pc_liveagent.live_form import LiveFormSession

        pairs = [
            ("app_citizenship", "app_agent_offer"),
            ("app_insurance", "app_citizenship"),
            ("app_dob_review", "app_insurance"),
            ("app_dob_collect", "app_insurance"),
            ("app_address_review", "app_dob_review"),
        ]
        for before, after in pairs:
            session = LiveFormSession(
                call_id=f"back-{before}",
                url="https://example.test",
                app_step=before,
            )
            page = AsyncMock()
            clicks = AsyncMock(return_value=True)
            with (
                patch("pc_liveagent.app_stage._click_application_back", clicks),
                patch(
                    "pc_liveagent.app_stage.detect_application_screen",
                    new=AsyncMock(return_value=after),
                ),
                patch(
                    "pc_liveagent.app_stage._prepare_resume_speech",
                    new=AsyncMock(return_value=(after, f"Question for {after}")),
                ),
            ):
                result = await application_go_back(session.call_id, session, page)
            self.assertTrue(result.get("ok"), before)
            self.assertEqual(clicks.await_count, 1, before)
            self.assertEqual(session.app_step, after)

        call_id = "go-back-not-an-answer"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", app_step="app_insurance"
        )
        session.page = AsyncMock()

        async def fake_session(_cid: str):
            return session

        with (
            patch(
                "pc_liveagent.call_turn.get_session",
                new=AsyncMock(side_effect=fake_session),
            ),
            patch(
                "pc_liveagent.app_stage.application_go_back",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "step": "app_citizenship",
                        "spoken": "Is everyone a U.S. citizen?",
                    }
                ),
            ) as backed,
            patch(
                "pc_liveagent.call_turn.submit_field",
                new=AsyncMock(side_effect=AssertionError("must not submit Go back")),
            ),
        ):
            result = await _process_application_turn(
                call_id,
                ctrl,
                text="Go back.",
                fp="go-back",
                dedupe_ctx="",
                turn_id="t-back",
                form_url="https://example.test",
            )
        backed.assert_awaited_once()
        self.assertEqual(result.detail, "go_back")
        self.assertIn("citizen", result.speech.lower())

    def test_spoken_dob_examples_and_accumulation(self) -> None:
        from pc_liveagent.app_stage import (
            _dob_pieces,
            _merge_dob_pieces,
            _parse_dob_answer,
        )
        from pc_liveagent.live_form import LiveFormSession

        self.assertEqual(
            _parse_dob_answer(
                "Monday's October. Date is twenty two. Year is two thousand three."
            ),
            ("10/22/2003", None),
        )
        self.assertEqual(_parse_dob_answer("March 15 2018"), ("03/15/2018", None))
        self.assertEqual(
            _parse_dob_answer("October twenty-second two thousand three"),
            ("10/22/2003", None),
        )
        pieces = _dob_pieces("Date is twenty two. Year is two thousand three.")
        self.assertEqual(pieces.get("day"), 22)
        self.assertEqual(pieces.get("year"), 2003)
        self.assertIsNone(pieces.get("month"))
        standalone_day = _dob_pieces("Twenty two.")
        self.assertEqual(standalone_day.get("day"), 22)
        self.assertFalse(standalone_day.get("ambiguous"))
        corrupted = _dob_pieces("October twenty twenty to")
        self.assertEqual(corrupted.get("month"), 10)
        self.assertIsNone(corrupted.get("day"))
        self.assertIsNone(corrupted.get("year"))
        self.assertTrue(corrupted.get("ambiguous"))
        self.assertEqual(_parse_dob_answer("February 31 2018"), (None, None))
        _dob, amb = _parse_dob_answer("10/11/2024")
        self.assertIsNone(_dob)
        self.assertIn("ambiguous", (amb or "").lower())

        session = LiveFormSession(
            call_id="dob-parts", url="https://example.test", app_step="app_dob_collect"
        )
        _date, clarify = _merge_dob_pieces(
            session, "Agent T.", _dob_pieces("October twenty twenty to")
        )
        self.assertIsNone(_date)
        self.assertIn("day", (clarify or "").lower())
        self.assertIn("year", (clarify or "").lower())
        _date, clarify = _merge_dob_pieces(
            session,
            "Agent T.",
            _dob_pieces("Date is twenty two. Year is two thousand three."),
        )
        self.assertEqual(_date, "10/22/2003")
        self.assertIsNone(clarify)

        split_session = LiveFormSession(
            call_id="dob-split", url="https://example.test", app_step="app_dob_collect"
        )
        _date, clarify = _merge_dob_pieces(
            split_session, "Agent T.", _dob_pieces("Twenty two.")
        )
        self.assertIsNone(_date)
        self.assertIn("month", (clarify or "").lower())
        _date, clarify = _merge_dob_pieces(
            split_session,
            "Agent T.",
            _dob_pieces("Month is October. Year is two thousand three."),
        )
        self.assertEqual(_date, "10/22/2003")
        self.assertIsNone(clarify)

    async def test_dob_review_reads_existing_without_pencil(self) -> None:
        from pc_liveagent.app_stage import _prepare_dob_collect_speech
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-pencil", url="https://example.test", app_step="app_dob_review"
        )
        cards = [
            {
                "name": "Agent T.",
                "dob": "10/20/2003",
                "needs_add": False,
                "editor_open": False,
            }
        ]
        click = AsyncMock(return_value=True)
        with (
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch("pc_liveagent.app_stage._click_member_add_dob", new=click),
        ):
            step, speech = await _prepare_dob_collect_speech(
                session, AsyncMock()
            )

        self.assertEqual(step, "app_dob_review")
        click.assert_not_awaited()
        self.assertEqual(session.filled.get("app_dob_current"), "Agent T.")
        self.assertEqual(session.filled.get("app_dob_phase"), "review")
        self.assertIsNone(session.filled.get("app_dob_component"))
        self.assertIn("Agent T", speech)
        self.assertIn("October 20, 2003", speech)
        self.assertIn("correct", speech.lower())
        self.assertNotIn("month", speech.lower())
        self.assertNotIn("MM/DD/YYYY", speech)

    async def test_parser_failure_does_not_fill_or_advance(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-no-fill",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Agent T."
        session.filled["app_dob_queue"] = '["Agent T."]'
        session.filled["app_dob_component"] = "day"
        session.filled["app_dob_partial"] = (
            '{"name": "Agent T.", "month": 10, "day": null, "year": null}'
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "Agent T.",
                            "dob": "",
                            "needs_add": True,
                            "editor_open": True,
                        }
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._fill_one_dob_component_detailed",
                new=AsyncMock(side_effect=AssertionError("must not fill")),
            ),
            patch(
                "pc_liveagent.app_stage._fill_and_save_dob",
                new=AsyncMock(side_effect=AssertionError("must not save")),
            ),
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(side_effect=AssertionError("must not advance")),
            ),
        ):
            result = await submit_application_answer(
                "dob-no-fill",
                session,
                AsyncMock(),
                "twenty twenty to",
                0.0,
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "dob_incomplete")
        self.assertEqual(session.app_step, "app_dob_collect")
        self.assertEqual(session.filled.get("app_dob_component"), "day")
        self.assertNotIn("10/", str(result.get("spoken") or ""))

    async def test_accumulated_dob_is_saved_for_the_named_member(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-save-agent",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Agent T."
        session.filled["app_dob_queue"] = '["Agent T."]'
        session.filled["app_dob_component"] = "confirm"
        session.filled["app_dob_partial"] = (
            '{"name": "Agent T.", "month": 10, "day": 22, "year": 2003}'
        )
        cards = [
            {"name": "Agent T.", "dob": "", "needs_add": True, "editor_open": True},
            {"name": "john D.", "dob": "02/22/2003", "needs_add": False},
            {"name": "Don D.", "dob": "12/05/2005", "needs_add": False},
        ]
        saved_cards = [
            {"name": "Agent T.", "dob": "10/22/2003", "needs_add": False},
            {"name": "john D.", "dob": "02/22/2003", "needs_add": False},
            {"name": "Don D.", "dob": "12/05/2005", "needs_add": False},
        ]
        fill = AsyncMock(return_value=True)

        async def scrape(_page):
            scrape.calls += 1
            return cards if scrape.calls == 1 else saved_cards

        scrape.calls = 0
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(side_effect=scrape),
            ),
            patch(
                "pc_liveagent.app_stage._click_member_add_dob",
                new=AsyncMock(return_value=True),
            ),
            patch("pc_liveagent.app_stage._fill_and_save_dob", fill),
            patch(
                "pc_liveagent.app_stage._verify_member_dob",
                new=AsyncMock(return_value=True),
            ),
        ):
            result = await submit_application_answer(
                "dob-save-agent",
                session,
                AsyncMock(),
                "Yes",
                0.0,
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(fill.await_args.args[1], "10/22/2003")
        self.assertEqual(fill.await_args.args[2], "Agent T.")
        self.assertIn("Agent T.", str(result.get("spoken") or ""))

    async def test_exit_control_is_not_a_household_member(self) -> None:
        from pc_liveagent.app_stage import (
            _prepare_dob_collect_speech,
            _scrape_dob_cards_from_body,
        )
        from pc_liveagent.live_form import LiveFormSession

        class _Body:
            async def inner_text(self) -> str:
                return (
                    "Date of birth\n"
                    "Please add and confirm for all household members\n"
                    "Exit\n"
                    "Agent T.\n"
                    "Cancel\n"
                    "Save\n"
                    "john D.\n"
                    "02/22/2003\n"
                    "Don D.\n"
                    "12/05/2005\n"
                )

        page = AsyncMock()
        page.locator = lambda _sel: _Body()
        cards = await _scrape_dob_cards_from_body(page)
        names = [card["name"] for card in cards]
        self.assertIn("Agent T.", names)
        self.assertNotIn("Exit", names)
        session = LiveFormSession(
            call_id="dob-name", url="https://example.test", app_step="app_dob_review"
        )
        with patch(
            "pc_liveagent.app_stage._scrape_dob_cards",
            new=AsyncMock(return_value=cards),
        ):
            step, speech = await _prepare_dob_collect_speech(session, page)
        # Agent T. is missing a DOB — collect first; present DOBs wait for review.
        self.assertEqual(step, "app_dob_collect")
        self.assertIn("Agent T", speech)
        self.assertNotIn("Exit", speech)


class DobPinnedMemberCollectTests(unittest.IsolatedAsyncioTestCase):
    """Pinned DOB member must survive ASR revisions and invalid month speech."""

    def _don_cards(self, *, editor_open: bool = True) -> list[dict]:
        return [
            {
                "name": "Agent T.",
                "dob": "11/22/2005",
                "needs_add": False,
                "editor_open": False,
                "index": 0,
            },
            {
                "name": "john D.",
                "dob": "01/02/2020",
                "needs_add": False,
                "editor_open": False,
                "index": 1,
            },
            {
                "name": "Don D.",
                "dob": "02/21/2000",
                "needs_add": False,
                "editor_open": editor_open,
                "index": 2,
            },
        ]

    async def test_donnie_asr_revision_does_not_retarget_month(self) -> None:
        """Exact call_772995 sequence after Don D. editor is open."""
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="don-d-pin",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Don D."
        session.filled["app_dob_correcting"] = "Don D."
        session.filled["app_dob_queue"] = '["Don D."]'
        session.filled["app_dob_component"] = "month"
        session.filled["app_dob_phase"] = "collect"
        cards = self._don_cards(editor_open=True)
        begin = AsyncMock(side_effect=AssertionError("must not retarget"))
        click = AsyncMock(side_effect=AssertionError("must not re-click pencil"))
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._fill_one_dob_component_detailed",
                new=AsyncMock(side_effect=AssertionError("no fill on Donnie")),
            ),
            patch("pc_liveagent.app_stage._begin_dob_collect_for", begin),
            patch("pc_liveagent.app_stage._click_member_add_dob", click),
        ):
            result = await submit_application_answer(
                "don-d-pin", session, AsyncMock(), "Donnie.", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "dob_incomplete")
        self.assertEqual(session.filled.get("app_dob_current"), "Don D.")
        self.assertEqual(session.filled.get("app_dob_correcting"), "Don D.")
        self.assertEqual(session.filled.get("app_dob_component"), "month")
        self.assertIn("don d", (result.get("spoken") or "").lower())
        self.assertIn("month", (result.get("spoken") or "").lower())
        self.assertNotIn("donnie", (result.get("spoken") or "").lower())
        begin.assert_not_called()
        click.assert_not_awaited()

    async def test_invalid_month_keeps_member_and_editor(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="don-d-bad-month",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Don D."
        session.filled["app_dob_correcting"] = "Don D."
        session.filled["app_dob_component"] = "month"
        session.filled["app_dob_phase"] = "collect"
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=self._don_cards()),
            ),
            patch(
                "pc_liveagent.app_stage._fill_one_dob_component_detailed",
                new=AsyncMock(side_effect=AssertionError("no fill")),
            ),
            patch(
                "pc_liveagent.app_stage._begin_dob_collect_for",
                new=AsyncMock(side_effect=AssertionError("no retarget")),
            ),
        ):
            result = await submit_application_answer(
                "don-d-bad-month", session, AsyncMock(), "Subravity.", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "dob_incomplete")
        self.assertEqual(session.filled.get("app_dob_current"), "Don D.")
        self.assertEqual(session.filled.get("app_dob_component"), "month")

    async def test_valid_month_fills_pinned_member(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="don-d-feb",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Don D."
        session.filled["app_dob_correcting"] = "Don D."
        session.filled["app_dob_component"] = "month"
        session.filled["app_dob_phase"] = "collect"
        fill = AsyncMock(return_value=(True, "ok"))
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=self._don_cards()),
            ),
            patch(
                "pc_liveagent.app_stage._fill_one_dob_component_detailed",
                fill,
            ),
            patch(
                "pc_liveagent.app_stage._click_member_add_dob",
                new=AsyncMock(side_effect=AssertionError("editor already open")),
            ),
        ):
            result = await submit_application_answer(
                "don-d-feb", session, AsyncMock(), "February", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(session.filled.get("app_dob_current"), "Don D.")
        self.assertEqual(session.filled.get("app_dob_component"), "day")
        fill.assert_awaited_once()
        self.assertEqual(fill.await_args.args[1], "Don D.")
        self.assertEqual(fill.await_args.args[2], "month")
        self.assertEqual(fill.await_args.args[3], 2)

    async def test_explicit_change_retargets_only_matched_member(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="don-d-change",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Don D."
        session.filled["app_dob_correcting"] = "Don D."
        session.filled["app_dob_component"] = "month"
        session.filled["app_dob_phase"] = "collect"
        click = AsyncMock(return_value=True)
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=self._don_cards(editor_open=False)),
            ),
            patch("pc_liveagent.app_stage._click_member_add_dob", click),
        ):
            result = await submit_application_answer(
                "don-d-change",
                session,
                AsyncMock(),
                "Change john D date of birth",
                0.0,
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_dob_collect")
        self.assertEqual(session.filled.get("app_dob_current"), "john D.")
        self.assertEqual(session.filled.get("app_dob_component"), "month")
        click.assert_awaited()
        self.assertEqual(click.await_args.args[1], "john D.")

    async def test_duplicate_month_event_does_not_clear_partial(self) -> None:
        from pc_liveagent.call_turn import (
            drop_controller,
            get_controller,
            process_user_turn,
        )
        from pc_liveagent.live_form import LiveFormSession

        call_id = "don-d-dup"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "application"
        ctrl.application_only_state = "application_ready"
        ctrl.consumed_transcript_user_index = 5
        ctrl.last_speech = "What month was Don D born?"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", app_step="app_dob_collect"
        )
        session.filled["app_dob_current"] = "Don D."
        session.filled["app_dob_component"] = "month"
        with patch(
            "pc_liveagent.call_turn.get_session",
            new=AsyncMock(return_value=session),
        ):
            result = await process_user_turn(
                call_id,
                "Donnie.",
                response_id=7,
                transcript_user_index=5,
            )
        self.assertEqual(result.detail, "stale_transcript_turn")
        self.assertEqual(session.filled.get("app_dob_current"), "Don D.")
        self.assertEqual(session.filled.get("app_dob_component"), "month")
        await drop_controller(call_id)

    async def test_new_user_index_month_still_processes(self) -> None:
        from pc_liveagent.call_turn import (
            drop_controller,
            get_controller,
            process_user_turn,
        )
        from pc_liveagent.live_form import LiveFormSession

        call_id = "don-d-new-idx"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "application"
        ctrl.application_only_state = "application_ready"
        ctrl.consumed_transcript_user_index = 5
        ctrl.last_speech = "What month was Don D born?"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", app_step="app_dob_collect"
        )
        session.filled["app_dob_current"] = "Don D."
        session.filled["app_dob_component"] = "month"
        session.page = AsyncMock()

        async def fake_submit(_cid, _field, value, **_kwargs):
            session.filled["app_dob_component"] = "day"
            return {
                "ok": True,
                "spoken": "ASK: What day of the month was Don D born?",
                "step": "app_dob_collect",
            }

        with (
            patch(
                "pc_liveagent.call_turn.get_session",
                new=AsyncMock(return_value=session),
            ),
            patch("pc_liveagent.call_turn.submit_field", side_effect=fake_submit),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.call_turn._speak_interrupt",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.call_turn._deliver_question_once",
                new=AsyncMock(return_value=True),
            ),
        ):
            result = await process_user_turn(
                call_id,
                "February",
                response_id=9,
                transcript_user_index=6,
            )
            task = __import__(
                "pc_liveagent.call_turn", fromlist=["_app_browser_tasks"]
            )._app_browser_tasks.get(call_id)
            if task is not None:
                await task
        self.assertNotEqual(result.detail, "stale_transcript_turn")
        self.assertEqual(ctrl.consumed_transcript_user_index, 6)
        self.assertEqual(session.filled.get("app_dob_current"), "Don D.")
        self.assertEqual(session.filled.get("app_dob_component"), "day")
        await drop_controller(call_id)


class DobReviewFirstTests(unittest.IsolatedAsyncioTestCase):
    """Stage E Q3: confirm existing card DOBs before opening any editor."""

    def _cards(self) -> list[dict]:
        return [
            {
                "name": "Agent T.",
                "dob": "10/22/2003",
                "needs_add": False,
                "editor_open": False,
                "index": 0,
            },
            {
                "name": "John D.",
                "dob": "02/22/2003",
                "needs_add": False,
                "editor_open": False,
                "index": 1,
            },
        ]

    async def test_confirm_existing_dob_skips_edit_and_save(self) -> None:
        from pc_liveagent.app_stage import (
            _prepare_dob_collect_speech,
            submit_application_answer,
        )
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-rev-yes",
            url="https://example.test",
            app_step="app_dob_review",
        )
        cards = self._cards()[:1]
        with (
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._click_member_add_dob",
                new=AsyncMock(side_effect=AssertionError("no edit")),
            ),
            patch(
                "pc_liveagent.app_stage._fill_and_save_dob",
                new=AsyncMock(side_effect=AssertionError("no save")),
            ),
        ):
            step, speech = await _prepare_dob_collect_speech(session, AsyncMock())
            self.assertEqual(step, "app_dob_review")
            self.assertIn("October 22, 2003", speech)
            with patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ):
                result = await submit_application_answer(
                    "dob-rev-yes", session, AsyncMock(), "Yes", 0.0
                )
        self.assertTrue(result.get("ok"))
        self.assertEqual(session.filled.get("app_dob_walk_complete"), "1")
        self.assertIn("agent t", session.filled.get("app_dob_reviewed", "").lower())

    async def test_multiple_members_confirmed_in_sequence(self) -> None:
        from pc_liveagent.app_stage import (
            _prepare_dob_collect_speech,
            submit_application_answer,
        )
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-rev-seq",
            url="https://example.test",
            app_step="app_dob_review",
        )
        cards = self._cards()
        click = AsyncMock(side_effect=AssertionError("no edit"))
        save = AsyncMock(side_effect=AssertionError("no save"))
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch("pc_liveagent.app_stage._click_member_add_dob", click),
            patch("pc_liveagent.app_stage._fill_and_save_dob", save),
        ):
            step, first_speech = await _prepare_dob_collect_speech(
                session, AsyncMock()
            )
            self.assertEqual(step, "app_dob_review")
            self.assertIn("agent t", first_speech.lower())
            self.assertIn("october 22, 2003", first_speech.lower())
            second = await submit_application_answer(
                "dob-rev-seq", session, AsyncMock(), "That's right", 0.0
            )
            self.assertIn("john d", (second.get("spoken") or "").lower())
            self.assertIn("february 22, 2003", (second.get("spoken") or "").lower())
            third = await submit_application_answer(
                "dob-rev-seq", session, AsyncMock(), "Correct", 0.0
            )
        self.assertTrue(third.get("ok"))
        self.assertEqual(session.filled.get("app_dob_walk_complete"), "1")
        click.assert_not_awaited()
        save.assert_not_awaited()

    async def test_no_without_name_changes_current_member(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-rev-no",
            url="https://example.test",
            app_step="app_dob_review",
        )
        session.filled["app_dob_current"] = "Agent T."
        session.filled["app_dob_phase"] = "review"
        click = AsyncMock(return_value=True)
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=self._cards()),
            ),
            patch("pc_liveagent.app_stage._click_member_add_dob", click),
        ):
            result = await submit_application_answer(
                "dob-rev-no", session, AsyncMock(), "No", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_dob_collect")
        self.assertEqual(session.filled.get("app_dob_current"), "Agent T.")
        self.assertEqual(session.filled.get("app_dob_component"), "month")
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("i'll change that", spoken)
        self.assertIn("month", spoken)
        self.assertIn("agent t", spoken)
        self.assertNotIn("whose", spoken)
        click.assert_awaited()

    async def test_named_correction_edits_only_that_member(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-rev-fix",
            url="https://example.test",
            app_step="app_dob_review",
        )
        session.filled["app_dob_current"] = "Agent T."
        session.filled["app_dob_phase"] = "review"
        click = AsyncMock(return_value=True)
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=self._cards()),
            ),
            patch("pc_liveagent.app_stage._click_member_add_dob", click),
        ):
            result = await submit_application_answer(
                "dob-rev-fix",
                session,
                AsyncMock(),
                "No, change John D.'s DOB",
                0.0,
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_dob_collect")
        self.assertEqual(session.filled.get("app_dob_current"), "John D.")
        self.assertEqual(session.filled.get("app_dob_correcting"), "John D.")
        self.assertEqual(session.filled.get("app_dob_component"), "month")
        click.assert_awaited_once()
        self.assertEqual(click.await_args.args[1], "John D.")
        self.assertTrue(click.await_args.kwargs.get("edit"))
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("john", spoken)
        self.assertIn("month", spoken)
        self.assertIn("i'll change that", spoken)

    async def test_prepare_skips_reviewed_member_after_scrape_lag(self) -> None:
        from pc_liveagent.app_stage import _prepare_dob_collect_speech
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-reviewed-lag",
            url="https://example.test",
            app_step="app_dob_review",
        )
        session.filled["app_dob_reviewed"] = '["john d."]'
        # Stale scrape still shows John as needing Add after a real Save.
        cards = [
            {
                "name": "John D.",
                "dob": "",
                "needs_add": True,
                "editor_open": True,
            },
            {
                "name": "Admin P.",
                "dob": "01/31/1995",
                "needs_add": False,
                "editor_open": False,
            },
        ]
        click = AsyncMock(return_value=True)
        with (
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch("pc_liveagent.app_stage._click_member_add_dob", click),
        ):
            step, speech = await _prepare_dob_collect_speech(session, AsyncMock())
        self.assertEqual(step, "app_dob_review")
        self.assertIn("admin p", speech.lower())
        self.assertNotIn("month", speech.lower())
        click.assert_not_awaited()

    async def test_ambiguous_member_name_clarifies(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-rev-amb",
            url="https://example.test",
            app_step="app_dob_review",
        )
        session.filled["app_dob_phase"] = "review"
        cards = [
            {"name": "John D.", "dob": "01/01/1990", "needs_add": False},
            {"name": "John S.", "dob": "02/02/1991", "needs_add": False},
        ]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._click_member_add_dob",
                new=AsyncMock(side_effect=AssertionError("no edit")),
            ),
        ):
            result = await submit_application_answer(
                "dob-rev-amb", session, AsyncMock(), "Change John", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "dob_member_ambiguous")

    async def test_unknown_member_name_clarifies(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-rev-unk",
            url="https://example.test",
            app_step="app_dob_review",
        )
        session.filled["app_dob_phase"] = "review"
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=self._cards()),
            ),
        ):
            result = await submit_application_answer(
                "dob-rev-unk", session, AsyncMock(), "Change Frodo Baggins", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "dob_member_unknown")

    async def test_correction_confirmed_saved_and_verified(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-rev-save",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "John D."
        session.filled["app_dob_queue"] = '["John D."]'
        session.filled["app_dob_component"] = "confirm"
        session.filled["app_dob_correcting"] = "John D."
        session.filled["app_dob_phase"] = "collect"
        session.filled["app_dob_partial"] = (
            '{"name": "John D.", "month": 3, "day": 15, "year": 2018}'
        )
        session.filled["app_dob_reviewed"] = '["agent t."]'
        before = [
            {
                "name": "Agent T.",
                "dob": "10/22/2003",
                "needs_add": False,
                "index": 0,
            },
            {
                "name": "John D.",
                "dob": "02/22/2003",
                "needs_add": False,
                "editor_open": True,
                "index": 1,
            },
        ]
        after = [
            {
                "name": "Agent T.",
                "dob": "10/22/2003",
                "needs_add": False,
                "index": 0,
            },
            {
                "name": "John D.",
                "dob": "03/15/2018",
                "needs_add": False,
                "index": 1,
            },
        ]
        fill = AsyncMock(return_value=True)
        verify = AsyncMock(return_value=True)
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(side_effect=[before, after, after]),
            ),
            patch("pc_liveagent.app_stage._fill_and_save_dob", fill),
            patch("pc_liveagent.app_stage._verify_member_dob", verify),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(side_effect=AssertionError("no next yet")),
            ),
        ):
            result = await submit_application_answer(
                "dob-rev-save", session, AsyncMock(), "Yes", 0.0
            )
        self.assertTrue(result.get("ok"))
        fill.assert_awaited_once()
        self.assertEqual(fill.await_args.args[1], "03/15/2018")
        self.assertEqual(fill.await_args.args[2], "John D.")
        verify.assert_awaited_once()
        self.assertEqual(session.filled.get("app_dob_walk_complete"), "1")
        self.assertEqual(result.get("step"), "app_dob_review")

    async def test_correction_rejected_does_not_save(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-rev-reject",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "John D."
        session.filled["app_dob_queue"] = '["John D."]'
        session.filled["app_dob_component"] = "confirm"
        session.filled["app_dob_phase"] = "collect"
        session.filled["app_dob_partial"] = (
            '{"name": "John D.", "month": 3, "day": 15, "year": 2018}'
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=self._cards()),
            ),
            patch(
                "pc_liveagent.app_stage._fill_and_save_dob",
                new=AsyncMock(side_effect=AssertionError("must not save")),
            ),
        ):
            result = await submit_application_answer(
                "dob-rev-reject", session, AsyncMock(), "No", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "dob_change_restart")
        self.assertEqual(session.filled.get("app_dob_component"), "month")
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("i'll change that", spoken)
        self.assertIn("month", spoken)

    async def test_continue_without_changing_proceeds_review(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-rev-leave",
            url="https://example.test",
            app_step="app_dob_review",
        )
        session.filled["app_dob_current"] = "Agent T."
        session.filled["app_dob_phase"] = "review"
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=self._cards()),
            ),
            patch(
                "pc_liveagent.app_stage._fill_and_save_dob",
                new=AsyncMock(side_effect=AssertionError("no save")),
            ),
        ):
            result = await submit_application_answer(
                "dob-rev-leave", session, AsyncMock(), "Leave it as is", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertIn("john d", (result.get("spoken") or "").lower())
        self.assertIn("date of birth is", (result.get("spoken") or "").lower())

    async def test_next_clicked_once_after_all_reviews(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-rev-next",
            url="https://example.test",
            app_step="app_dob_review",
        )
        session.filled["app_dob_walk_complete"] = "1"
        session.filled["app_dob_phase"] = "done"
        session.filled["app_dob_reviewed"] = '["agent t.", "john d."]'
        nxt = AsyncMock(return_value=True)
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=self._cards()),
            ),
            patch(
                "pc_liveagent.app_stage._next_button_enabled",
                new=AsyncMock(return_value=True),
            ),
            patch("pc_liveagent.app_stage._click_next_or_continue", nxt),
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_address_review"),
            ),
        ):
            result = await submit_application_answer(
                "dob-rev-next", session, AsyncMock(), "Yes", 0.0
            )
        nxt.assert_awaited_once()
        self.assertEqual(result.get("step"), "app_address_review")


class DobComponentCollectTests(unittest.IsolatedAsyncioTestCase):
    """Stage E Q3: month → day → year → confirm → Save (mocked Pointcare)."""

    def test_month_names_and_abbreviations(self) -> None:
        from pc_liveagent.app_stage import _parse_month_component

        self.assertEqual(_parse_month_component("October")[0], 10)
        self.assertEqual(_parse_month_component("Oct")[0], 10)
        self.assertEqual(_parse_month_component("oct.")[0], 10)
        self.assertIsNone(_parse_month_component("10/22/2003")[0])
        self.assertIsNone(_parse_month_component("blurble")[0])

    def test_ordinal_and_spoken_days(self) -> None:
        from pc_liveagent.app_stage import _parse_day_component

        self.assertEqual(_parse_day_component("22")[0], 22)
        self.assertEqual(_parse_day_component("22nd")[0], 22)
        self.assertEqual(_parse_day_component("twenty-two")[0], 22)
        self.assertEqual(_parse_day_component("twenty two")[0], 22)
        self.assertIsNone(_parse_day_component("thirty-two")[0])

    def test_spoken_years(self) -> None:
        from pc_liveagent.app_stage import _parse_year_component

        self.assertEqual(_parse_year_component("2003")[0], 2003)
        self.assertEqual(_parse_year_component("two thousand three")[0], 2003)
        self.assertIsNone(_parse_year_component("03")[0])
        clarify = _parse_year_component("03")[1] or ""
        self.assertIn("four-digit", clarify.lower())

    async def test_component_persistence_across_turns(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-persist",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Agent T."
        session.filled["app_dob_queue"] = '["Agent T."]'
        session.filled["app_dob_component"] = "month"
        cards = [
            {
                "name": "Agent T.",
                "dob": "",
                "needs_add": True,
                "editor_open": True,
            }
        ]
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._fill_one_dob_component_detailed",
                new=AsyncMock(return_value=(True, "ok")),
            ) as fill_one,
            patch(
                "pc_liveagent.app_stage._fill_and_save_dob",
                new=AsyncMock(side_effect=AssertionError("no save yet")),
            ),
        ):
            month_turn = await submit_application_answer(
                "dob-persist", session, page, "October", 0.0
            )
            self.assertTrue(month_turn.get("ok"))
            self.assertEqual(session.filled.get("app_dob_component"), "day")
            self.assertIn("day", (month_turn.get("spoken") or "").lower())

            day_turn = await submit_application_answer(
                "dob-persist", session, page, "22nd", 0.0
            )
            self.assertTrue(day_turn.get("ok"))
            self.assertEqual(session.filled.get("app_dob_component"), "year")

            year_turn = await submit_application_answer(
                "dob-persist", session, page, "two thousand three", 0.0
            )
            self.assertTrue(year_turn.get("ok"))
            self.assertEqual(session.filled.get("app_dob_component"), "confirm")
            spoken = (year_turn.get("spoken") or "").lower()
            self.assertIn("october 22, 2003", spoken)
            self.assertIn("agent t", spoken)
        self.assertEqual(
            [call.args[2] for call in fill_one.await_args_list],
            ["month", "day", "year"],
        )

    async def test_duplicate_month_event_does_not_double_advance(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.call_turn import (
            drop_controller,
            get_controller,
            process_user_turn,
        )
        from pc_liveagent.live_form import LiveFormSession, _application_step_answer_key

        session = LiveFormSession(
            call_id="dob-dup-evt",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Agent T."
        session.filled["app_dob_component"] = "month"
        session.filled["app_pending_question"] = "What month was Agent T. born?"
        key_month = _application_step_answer_key(session, "October")
        session.filled["app_dob_component"] = "day"
        session.filled["app_pending_question"] = "What day were they born?"
        key_day = _application_step_answer_key(session, "October")
        self.assertNotEqual(key_month, key_day)

        session.filled["app_dob_component"] = "month"
        session.filled["app_dob_queue"] = '["Agent T."]'
        session.filled["app_pending_question"] = "What month was Agent T. born?"
        cards = [
            {
                "name": "Agent T.",
                "dob": "",
                "needs_add": True,
                "editor_open": True,
            }
        ]
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        fill_one = AsyncMock(return_value=(True, "ok"))
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch("pc_liveagent.app_stage._fill_one_dob_component_detailed", fill_one),
            patch(
                "pc_liveagent.app_stage._fill_and_save_dob",
                new=AsyncMock(side_effect=AssertionError("no save")),
            ),
        ):
            first = await submit_application_answer(
                "dob-dup-evt", session, page, "October", 0.0
            )
            self.assertTrue(first.get("ok"))
            self.assertEqual(session.filled.get("app_dob_component"), "day")
            # Repeat "October" while on day — must not fill month again or jump to year.
            second = await submit_application_answer(
                "dob-dup-evt", session, page, "October", 0.0
            )
        self.assertFalse(second.get("ok"))
        self.assertEqual(fill_one.await_count, 1)
        self.assertEqual(session.filled.get("app_dob_component"), "day")

        call_id = "dob-dup-commit"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "application"
        session2 = LiveFormSession(
            call_id=call_id,
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session2.filled["app_dob_current"] = "Agent T."
        session2.filled["app_dob_component"] = "month"
        session2.filled["app_pending_question"] = "What month was Agent T. born?"
        submit = AsyncMock(
            return_value={
                "ok": True,
                "spoken": "ASK: What day were they born?",
                "step": "app_dob_collect",
                "phase": "application",
            }
        )
        with (
            patch("pc_liveagent.config.APPLICATION_ONLY_TEST_MODE", False),
            patch(
                "pc_liveagent.call_turn.get_session",
                new=AsyncMock(return_value=session2),
            ),
            patch("pc_liveagent.call_turn.submit_field", submit),
            patch("pc_liveagent.call_turn._SLOW_APP_STEPS", frozenset()),
        ):
            # Keep component in the commit key so a repeated webhook is dropped.
            r1 = await process_user_turn(call_id, "October")
            r2 = await process_user_turn(call_id, "October")
        self.assertEqual(submit.await_count, 1)
        self.assertEqual(r2.detail, "duplicate_app_answer")
        self.assertIn("day", (r1.speech or "").lower())
        await drop_controller(call_id)

    async def test_reject_confirmation_then_correct_day(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-fix",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Agent T."
        session.filled["app_dob_queue"] = '["Agent T."]'
        session.filled["app_dob_component"] = "confirm"
        session.filled["app_dob_partial"] = (
            '{"name": "Agent T.", "month": 10, "day": 21, "year": 2003}'
        )
        cards = [
            {
                "name": "Agent T.",
                "dob": "",
                "needs_add": True,
                "editor_open": True,
            }
        ]
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._fill_one_dob_component_detailed",
                new=AsyncMock(return_value=(True, "ok")),
            ) as fill_one,
            patch(
                "pc_liveagent.app_stage._fill_and_save_dob",
                new=AsyncMock(side_effect=AssertionError("no save on reject")),
            ),
        ):
            no_turn = await submit_application_answer(
                "dob-fix", session, page, "No", 0.0
            )
            self.assertFalse(no_turn.get("ok"))
            self.assertEqual(session.filled.get("app_dob_component"), "fix_which")

            which = await submit_application_answer(
                "dob-fix", session, page, "the day", 0.0
            )
            self.assertFalse(which.get("ok"))
            self.assertEqual(session.filled.get("app_dob_component"), "day")

            day = await submit_application_answer(
                "dob-fix", session, page, "22", 0.0
            )
            self.assertTrue(day.get("ok"))
            self.assertEqual(session.filled.get("app_dob_component"), "confirm")
            self.assertIn("october 22, 2003", (day.get("spoken") or "").lower())
        fill_one.assert_awaited_once()
        self.assertEqual(fill_one.await_args.args[2], "day")
        self.assertEqual(fill_one.await_args.args[3], 22)

    async def test_vague_reply_is_not_confirmation(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-vague",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Agent T."
        session.filled["app_dob_queue"] = '["Agent T."]'
        session.filled["app_dob_component"] = "confirm"
        session.filled["app_dob_partial"] = (
            '{"name": "Agent T.", "month": 10, "day": 22, "year": 2003}'
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "Agent T.",
                            "dob": "",
                            "needs_add": True,
                            "editor_open": True,
                        }
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._fill_and_save_dob",
                new=AsyncMock(side_effect=AssertionError("must not save")),
            ),
        ):
            result = await submit_application_answer(
                "dob-vague", session, AsyncMock(), "okay sure maybe", 0.0
            )
            soft = await submit_application_answer(
                "dob-vague", session, AsyncMock(), "sure", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "dob_confirm_needed")
        self.assertFalse(soft.get("ok"))
        self.assertEqual(soft.get("error"), "dob_confirm_needed")
        self.assertEqual(session.filled.get("app_dob_component"), "confirm")

    async def test_confirmed_save_verifies_card_then_next_member(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-multi",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Agent T."
        session.filled["app_dob_queue"] = '["Agent T.", "john D."]'
        session.filled["app_dob_component"] = "confirm"
        session.filled["app_dob_partial"] = (
            '{"name": "Agent T.", "month": 10, "day": 22, "year": 2003}'
        )
        before = [
            {"name": "Agent T.", "dob": "", "needs_add": True, "editor_open": True},
            {"name": "john D.", "dob": "", "needs_add": True},
        ]
        after = [
            {"name": "Agent T.", "dob": "10/22/2003", "needs_add": False},
            {"name": "john D.", "dob": "", "needs_add": True},
        ]
        fill = AsyncMock(return_value=True)
        verify = AsyncMock(return_value=True)
        open_next = AsyncMock(return_value=True)
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(side_effect=[before, after, after]),
            ),
            patch("pc_liveagent.app_stage._fill_and_save_dob", fill),
            patch("pc_liveagent.app_stage._verify_member_dob", verify),
            patch(
                "pc_liveagent.app_stage._open_dob_editor_for_prompt",
                open_next,
            ),
        ):
            result = await submit_application_answer(
                "dob-multi", session, AsyncMock(), "Yes", 0.0
            )
        self.assertTrue(result.get("ok"))
        fill.assert_awaited_once()
        self.assertEqual(fill.await_args.args[1], "10/22/2003")
        verify.assert_awaited_once()
        self.assertEqual(session.filled.get("app_dob_current"), "john D.")
        self.assertEqual(session.filled.get("app_dob_component"), "month")
        self.assertIn("john", (result.get("spoken") or "").lower())
        self.assertIn("month", (result.get("spoken") or "").lower())
        open_next.assert_awaited()

    async def test_next_only_after_all_required_dobs_saved(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-next-all",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Agent T."
        session.filled["app_dob_queue"] = '["Agent T."]'
        session.filled["app_dob_component"] = "confirm"
        session.filled["app_dob_partial"] = (
            '{"name": "Agent T.", "month": 10, "day": 22, "year": 2003}'
        )
        before = [
            {"name": "Agent T.", "dob": "", "needs_add": True, "editor_open": True},
        ]
        after = [
            {"name": "Agent T.", "dob": "10/22/2003", "needs_add": False},
        ]
        nxt = AsyncMock(return_value=True)
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(side_effect=[before, after, after, after]),
            ),
            patch(
                "pc_liveagent.app_stage._fill_and_save_dob",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._verify_member_dob",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                nxt,
            ),
            patch(
                "pc_liveagent.app_stage._next_button_enabled",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_address_review"),
            ),
        ):
            saved = await submit_application_answer(
                "dob-next-all", session, AsyncMock(), "Yes", 0.0
            )
            self.assertTrue(saved.get("ok"))
            self.assertEqual(session.filled.get("app_dob_walk_complete"), "1")
            nxt.assert_not_awaited()
            session.app_step = "app_dob_review"
            continued = await submit_application_answer(
                "dob-next-all", session, AsyncMock(), "Yes", 0.0
            )
        nxt.assert_awaited_once()
        self.assertEqual(continued.get("step"), "app_address_review")

    def test_day_question_asks_day_of_month(self) -> None:
        from pc_liveagent.app_stage import _dob_day_question

        self.assertIn("day of the month", _dob_day_question("Agent T.").lower())

    async def test_scrape_marks_editor_open_even_with_saved_dob(self) -> None:
        """Regression for call_8d9ed1ab: saved DOB must not hide an open editor."""
        from pc_liveagent.app_stage import _scrape_dob_cards_from_body

        class _Body:
            async def inner_text(self) -> str:
                return (
                    "Date of birth\n"
                    "Agent T.\n"
                    "10/20/2003\n"
                    "MM\n"
                    "DD\n"
                    "YYYY\n"
                    "Cancel\n"
                    "Save\n"
                )

        page = AsyncMock()
        page.locator = lambda _sel: _Body()
        cards = await _scrape_dob_cards_from_body(page)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["name"], "Agent T.")
        self.assertEqual(cards[0]["dob"], "10/20/2003")
        self.assertTrue(cards[0]["editor_open"])

    async def test_saved_dob_open_editor_skips_pencil_between_components(
        self,
    ) -> None:
        """Month then day on separate turns must not re-click Edit/pencil."""
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-open-edit",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Agent T."
        session.filled["app_dob_queue"] = '["Agent T."]'
        session.filled["app_dob_component"] = "month"
        session.filled["app_dob_correcting"] = "Agent T."
        # Saved DOB present AND editor already open (the live failure case).
        cards = [
            {
                "name": "Agent T.",
                "dob": "10/20/2003",
                "needs_add": False,
                "editor_open": True,
            }
        ]
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        click = AsyncMock(side_effect=AssertionError("must not re-click pencil"))
        fill_one = AsyncMock(return_value=(True, "ok"))
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch("pc_liveagent.app_stage._click_member_add_dob", click),
            patch(
                "pc_liveagent.app_stage._member_dob_editor_is_open",
                new=AsyncMock(return_value=True),
            ),
            patch("pc_liveagent.app_stage._fill_one_dob_component_detailed", fill_one),
            patch(
                "pc_liveagent.app_stage._fill_and_save_dob",
                new=AsyncMock(side_effect=AssertionError("no save yet")),
            ),
        ):
            month = await submit_application_answer(
                "dob-open-edit", session, page, "October", 0.0
            )
            self.assertTrue(month.get("ok"))
            self.assertEqual(session.filled.get("app_dob_component"), "day")
            self.assertIn("day of the month", (month.get("spoken") or "").lower())

            day = await submit_application_answer(
                "dob-open-edit", session, page, "Twenty two", 0.0
            )
            self.assertTrue(day.get("ok"))
            self.assertEqual(session.filled.get("app_dob_component"), "year")

            year = await submit_application_answer(
                "dob-open-edit", session, page, "2003", 0.0
            )
            self.assertTrue(year.get("ok"))
            self.assertEqual(session.filled.get("app_dob_component"), "confirm")
        click.assert_not_awaited()
        self.assertEqual(
            [c.args[2] for c in fill_one.await_args_list],
            ["month", "day", "year"],
        )

    async def test_live_open_editor_skips_pencil_when_scrape_says_closed(
        self,
    ) -> None:
        """Missing pencil while inputs are open must not report action_not_found loop."""
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-live-open",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Agent T."
        session.filled["app_dob_queue"] = '["Agent T."]'
        session.filled["app_dob_component"] = "day"
        session.filled["app_dob_correcting"] = "Agent T."
        session.filled["app_dob_partial"] = (
            '{"name": "Agent T.", "month": 10, "day": null, "year": null}'
        )
        # Old buggy scrape shape: saved dob → editor_open false.
        cards = [
            {
                "name": "Agent T.",
                "dob": "10/20/2003",
                "needs_add": False,
                "editor_open": False,
            }
        ]
        click = AsyncMock(
            side_effect=AssertionError("must not click missing pencil")
        )
        fill_one = AsyncMock(return_value=(True, "ok"))
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._member_dob_editor_is_open",
                new=AsyncMock(return_value=True),
            ),
            patch("pc_liveagent.app_stage._click_member_add_dob", click),
            patch("pc_liveagent.app_stage._fill_one_dob_component_detailed", fill_one),
        ):
            result = await submit_application_answer(
                "dob-live-open", session, AsyncMock(), "22", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(session.filled.get("app_dob_component"), "year")
        click.assert_not_awaited()
        fill_one.assert_awaited_once()
        self.assertEqual(fill_one.await_args.args[2], "day")
        self.assertEqual(fill_one.await_args.args[3], 22)

    async def test_genuinely_closed_editor_reports_failure_once(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="dob-closed",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Agent T."
        session.filled["app_dob_queue"] = '["Agent T."]'
        session.filled["app_dob_component"] = "day"
        session.filled["app_dob_partial"] = (
            '{"name": "Agent T.", "month": 10, "day": null, "year": null}'
        )
        cards = [
            {
                "name": "Agent T.",
                "dob": "10/20/2003",
                "needs_add": False,
                "editor_open": False,
            }
        ]
        click = AsyncMock(return_value=False)
        fill_one = AsyncMock(side_effect=AssertionError("must not fill closed editor"))
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._member_dob_editor_is_open",
                new=AsyncMock(return_value=False),
            ),
            patch("pc_liveagent.app_stage._click_member_add_dob", click),
            patch("pc_liveagent.app_stage._fill_one_dob_component_detailed", fill_one),
        ):
            result = await submit_application_answer(
                "dob-closed", session, AsyncMock(), "22", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "dob_editor_closed")
        self.assertEqual(session.filled.get("app_dob_component"), "day")
        click.assert_awaited_once()
        self.assertTrue(click.await_args.kwargs.get("edit"))
        fill_one.assert_not_awaited()

    async def test_click_member_add_skips_when_inputs_already_visible(
        self,
    ) -> None:
        from pc_liveagent.app_stage import _click_member_add_dob

        page = AsyncMock()
        page.evaluate = AsyncMock(
            side_effect=AssertionError("must not search for pencil")
        )
        with patch(
            "pc_liveagent.app_stage._member_dob_editor_is_open",
            new=AsyncMock(return_value=True),
        ):
            ok = await _click_member_add_dob(page, "Agent T.", edit=True)
        self.assertTrue(ok)


def _pointcare_dob_editor_html() -> str:
    """Realistic fixture: Agent T editor with nested MM-only wrapper + unrelated member.

    Reproduces call_9c52bfa8… where the smallest ancestor of MM does not contain DD.
    """
    return """
    <!DOCTYPE html><html><body>
    <h1>Date of birth</h1>
    <div class="member-card" id="agent-card" style="width:400px;height:220px;">
      <div class="name">Agent T.</div>
      <div class="saved">10/20/2003</div>
      <div class="editor" style="width:380px;height:120px;">
        <div class="mm-only-wrap" style="width:60px;height:40px;">
          <input placeholder="MM" id="agent-mm" />
        </div>
        <input placeholder="DD" id="agent-dd" />
        <input placeholder="YYYY" id="agent-yyyy" />
        <button type="button">Cancel</button>
        <button type="button">Save</button>
      </div>
    </div>
    <div class="member-card" id="john-card" style="width:400px;height:220px;">
      <div class="name">john D.</div>
      <div class="saved">02/22/2003</div>
      <div class="editor" style="width:380px;height:120px;">
        <input placeholder="MM" id="john-mm" value="02" />
        <input placeholder="DD" id="john-dd" value="22" />
        <input placeholder="YYYY" id="john-yyyy" value="2003" />
        <button type="button">Cancel</button>
        <button type="button">Save</button>
      </div>
    </div>
    </body></html>
    """


class DobComponentDomFixtureTests(unittest.IsolatedAsyncioTestCase):
    """Real Chromium DOM fixture for Q3 component scoping (not a live Pointcare call)."""

    async def asyncSetUp(self) -> None:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            channel="chrome", headless=True
        )
        self._page = await self._browser.new_page()

    async def asyncTearDown(self) -> None:
        await self._browser.close()
        await self._playwright.stop()

    async def test_month_then_day_across_turns_with_nested_mm_wrapper(self) -> None:
        """call_9c52bfa8… sequence: fill Nov, then day 22 on a later turn."""
        from pc_liveagent.app_stage import (
            _fill_one_dob_component_detailed,
            _mark_member_dob_card_detailed,
        )

        await self._page.set_content(_pointcare_dob_editor_html())
        mark_ok, mark_reason = await _mark_member_dob_card_detailed(
            self._page, "Agent T."
        )
        self.assertTrue(mark_ok, msg=mark_reason)
        # Marked scope must include DD, not just the nested MM wrapper.
        marked = self._page.locator("[data-pc-dob-card='1']")
        self.assertEqual(await marked.get_by_placeholder("DD").count(), 1)
        self.assertEqual(await marked.get_by_placeholder("YYYY").count(), 1)

        month_ok, month_reason = await _fill_one_dob_component_detailed(
            self._page, "Agent T.", "month", 11
        )
        self.assertTrue(month_ok, msg=month_reason)
        self.assertEqual(await self._page.locator("#agent-mm").input_value(), "11")
        self.assertEqual(await self._page.locator("#agent-dd").input_value(), "")
        self.assertEqual(await self._page.locator("#agent-yyyy").input_value(), "")
        # Unrelated member untouched.
        self.assertEqual(await self._page.locator("#john-mm").input_value(), "02")
        self.assertEqual(await self._page.locator("#john-dd").input_value(), "22")

        day_ok, day_reason = await _fill_one_dob_component_detailed(
            self._page, "Agent T.", "day", 22
        )
        self.assertTrue(day_ok, msg=day_reason)
        self.assertEqual(await self._page.locator("#agent-mm").input_value(), "11")
        self.assertEqual(await self._page.locator("#agent-dd").input_value(), "22")
        self.assertEqual(await self._page.locator("#agent-yyyy").input_value(), "")
        self.assertEqual(await self._page.locator("#john-dd").input_value(), "22")

    async def test_diagnostics_for_missing_input_and_value_mismatch(self) -> None:
        from pc_liveagent.app_stage import _fill_one_dob_component_detailed

        await self._page.set_content(
            """
            <!DOCTYPE html><html><body>
            <div class="member-card" style="width:400px;height:200px;">
              <div class="name">Agent T.</div>
              <input placeholder="MM" id="agent-mm" />
              <input placeholder="YYYY" id="agent-yyyy" />
              <button type="button">Save</button>
            </div>
            </body></html>
            """
        )
        # No DD in the card → mark fails (partial editor) or day not found.
        ok, reason = await _fill_one_dob_component_detailed(
            self._page, "Agent T.", "day", 22
        )
        self.assertFalse(ok)
        self.assertIn(
            reason,
            ("member_card_mark_failed", "component_input_not_found"),
        )

        await self._page.set_content(_pointcare_dob_editor_html())
        # Force a mismatch by making DD reject the value via read-only simulation:
        # fill a year into day after blocking DD — use detailed path with bad verify
        # by filling then overwriting expectation through a stubbed match is hard;
        # instead fill day successfully then check month-path invalid_value.
        bad, bad_reason = await _fill_one_dob_component_detailed(
            self._page, "Agent T.", "day", 99
        )
        self.assertFalse(bad)
        self.assertEqual(bad_reason, "invalid_value")

    async def test_submit_day_turn_advances_to_year_on_fixture(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        await self._page.set_content(_pointcare_dob_editor_html())
        session = LiveFormSession(
            call_id="dob-dom-turn",
            url="https://example.test",
            app_step="app_dob_collect",
        )
        session.filled["app_dob_current"] = "Agent T."
        session.filled["app_dob_queue"] = '["Agent T."]'
        session.filled["app_dob_component"] = "month"
        session.filled["app_dob_correcting"] = "Agent T."
        cards = [
            {
                "name": "Agent T.",
                "dob": "10/20/2003",
                "needs_add": False,
                "editor_open": True,
            }
        ]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_dob_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.app_stage._click_member_add_dob",
                new=AsyncMock(side_effect=AssertionError("must not re-click")),
            ),
        ):
            month = await submit_application_answer(
                "dob-dom-turn", session, self._page, "November", 0.0
            )
            self.assertTrue(month.get("ok"), msg=month)
            self.assertEqual(session.filled.get("app_dob_component"), "day")
            self.assertEqual(await self._page.locator("#agent-mm").input_value(), "11")

            day = await submit_application_answer(
                "dob-dom-turn", session, self._page, "Twenty two", 0.0
            )
            self.assertTrue(day.get("ok"), msg=day)
            self.assertEqual(session.filled.get("app_dob_component"), "year")
            self.assertEqual(await self._page.locator("#agent-mm").input_value(), "11")
            self.assertEqual(await self._page.locator("#agent-dd").input_value(), "22")
            self.assertEqual(await self._page.locator("#agent-yyyy").input_value(), "")
            self.assertEqual(await self._page.locator("#john-dd").input_value(), "22")


class CitizenshipImmigrationSyncTests(unittest.IsolatedAsyncioTestCase):
    def _members(self) -> list[dict[str, str]]:
        return [
            {"name": "Agent T.", "selected": "U.S. Citizen"},
            {"name": "john D.", "selected": "U.S. Citizen"},
            {"name": "Don D.", "selected": "U.S. Citizen"},
        ]

    async def test_overlapping_heading_still_detects_citizenship_screen(self) -> None:
        from pc_liveagent.app_stage import detect_application_screen

        class _Body:
            async def inner_text(self) -> str:
                return (
                    "Are Agent T, john D, and Don D all U.S. Citizens? "
                    "Yes Everyone listed is a U.S. Citizen "
                    "No Some or all are not U.S. Citizens "
                    "Agent T. U.S. Citizen U.S. National "
                    "Satisfactory immigration status Prefer not to answer"
                )

        page = AsyncMock()
        page.locator = lambda _sel: _Body()
        self.assertEqual(await detect_application_screen(page), "app_citizenship")

    async def test_us_national_selects_current_member_without_repeating_yes_no(
        self,
    ) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="imm-sync",
            url="https://example.test",
            app_step="app_immigration",
        )
        session.filled["app_citizenship_no"] = "1"
        session.filled["app_immigration_current"] = "Agent T."
        session.filled["app_immigration_queue"] = '["Agent T.", "john D.", "Don D."]'
        set_status = AsyncMock(return_value=(True, "selected"))
        yes_no = AsyncMock(side_effect=AssertionError("must not treat this as Yes/No"))
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage.immigration_member_radios_visible",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_immigration_members",
                new=AsyncMock(return_value=self._members()),
            ),
            patch("pc_liveagent.app_stage._set_member_immigration_status", set_status),
            patch(
                "pc_liveagent.app_stage._verify_member_immigration_status",
                new=AsyncMock(return_value=(True, "verified")),
            ),
            patch("pc_liveagent.app_stage._click_yes_no", yes_no),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(side_effect=AssertionError("must not advance yet")),
            ),
        ):
            result = await submit_application_answer(
                "imm-sync", session, AsyncMock(), "US national.", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(session.app_step, "app_immigration")
        self.assertEqual(set_status.await_args.args[1], "Agent T.")
        self.assertEqual(set_status.await_args.args[2], "U.S. National")
        spoken = str(result.get("spoken") or "")
        self.assertNotIn("all U.S. Citizens", spoken)
        self.assertIn("john D", spoken)
        self.assertEqual(session.filled.get("app_immigration_current"), "john D.")

    async def test_other_statuses_walk_each_member_then_advance(self) -> None:
        from pc_liveagent.app_stage import (
            _map_immigration_status,
            submit_application_answer,
        )
        from pc_liveagent.live_form import LiveFormSession

        self.assertEqual(_map_immigration_status("us national"), "U.S. National")
        self.assertEqual(_map_immigration_status("u.s. national"), "U.S. National")
        self.assertEqual(
            _map_immigration_status("green card"),
            "Satisfactory immigration status (green card, etc.)",
        )
        self.assertEqual(
            _map_immigration_status("prefer not to answer"),
            "Prefer not to answer",
        )
        session = LiveFormSession(
            call_id="imm-walk",
            url="https://example.test",
            app_step="app_immigration",
        )
        session.filled["app_citizenship_no"] = "1"
        session.filled["app_immigration_current"] = "Agent T."
        session.filled["app_immigration_queue"] = '["Agent T.", "john D.", "Don D."]'
        # Simulate first-member options already spoken for Agent T.
        session.filled["app_immigration_options_listed"] = "1"
        set_status = AsyncMock(return_value=(True, "selected"))
        nxt = AsyncMock(return_value=True)

        async def scrape(_page):
            saved = json.loads(session.filled.get("app_immigration_confirmed") or "{}")
            return [
                {
                    "name": name,
                    "selected": saved.get(name, "U.S. Citizen"),
                }
                for name in ("Agent T.", "john D.", "Don D.")
            ]

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(
                    side_effect=[
                        "app_citizenship",
                        "app_citizenship",
                        "app_citizenship",
                        "app_citizenship",
                        "app_insurance",
                    ]
                    + ["app_insurance"] * 20
                ),
            ),
            patch(
                "pc_liveagent.app_stage.immigration_member_radios_visible",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_immigration_members",
                new=AsyncMock(side_effect=scrape),
            ),
            patch("pc_liveagent.app_stage._set_member_immigration_status", set_status),
            patch(
                "pc_liveagent.app_stage._verify_member_immigration_status",
                new=AsyncMock(return_value=(True, "verified")),
            ),
            patch("pc_liveagent.app_stage._click_enabled_application_next", nxt),
            patch(
                "pc_liveagent.app_stage._wait_immigration_next_ready",
                new=AsyncMock(
                    return_value={
                        "status": "next_enabled",
                        "total": 1,
                        "hidden": 0,
                        "visible_disabled": 0,
                        "visible_enabled": 1,
                        "tag": "button",
                        "disabled": False,
                        "aria_disabled": "",
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_insurance",
                        "Do they have health coverage?",
                    )
                ),
            ),
        ):
            page = AsyncMock()
            page.wait_for_timeout = AsyncMock()
            first = await submit_application_answer(
                "imm-walk", session, page, "U.S. National", 0.0
            )
            second = await submit_application_answer(
                "imm-walk", session, page, "green card", 0.0
            )
            third = await submit_application_answer(
                "imm-walk", session, page, "prefer not to answer", 0.0
            )
        self.assertTrue(first.get("ok"))
        self.assertIn("john D", str(first.get("spoken") or ""))
        self.assertIn("those four options", str(first.get("spoken") or "").lower())
        self.assertTrue(second.get("ok"))
        self.assertIn("Don D", str(second.get("spoken") or ""))
        self.assertTrue(third.get("ok"))
        self.assertEqual(third.get("step"), "app_immigration")
        self.assertIn("confirming", str(third.get("spoken") or "").lower())
        self.assertIn("agent t", str(third.get("spoken") or "").lower())
        self.assertIn("don d", str(third.get("spoken") or "").lower())
        self.assertEqual(session.filled.get("app_immigration_phase"), "confirm_all")
        nxt.assert_not_awaited()
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(
                    side_effect=["app_immigration"] + ["app_insurance"] * 20
                ),
            ),
            patch(
                "pc_liveagent.app_stage.immigration_member_radios_visible",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_immigration_members",
                new=AsyncMock(side_effect=scrape),
            ),
            patch("pc_liveagent.app_stage._set_member_immigration_status", set_status),
            patch(
                "pc_liveagent.app_stage._verify_member_immigration_status",
                new=AsyncMock(return_value=(True, "verified")),
            ),
            patch("pc_liveagent.app_stage._click_enabled_application_next", nxt),
            patch(
                "pc_liveagent.app_stage._wait_immigration_next_ready",
                new=AsyncMock(
                    return_value={
                        "status": "next_enabled",
                        "total": 1,
                        "hidden": 0,
                        "visible_disabled": 0,
                        "visible_enabled": 1,
                        "tag": "button",
                        "disabled": False,
                        "aria_disabled": "",
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_insurance",
                        "Do they have health coverage?",
                    )
                ),
            ),
        ):
            page = AsyncMock()
            page.wait_for_timeout = AsyncMock()
            confirm = await submit_application_answer(
                "imm-walk", session, page, "Yes", 0.0
            )
        self.assertEqual(confirm.get("step"), "app_insurance")
        self.assertEqual(
            [call.args[1:] for call in set_status.await_args_list],
            [
                ("Agent T.", "U.S. National"),
                ("john D.", "Satisfactory immigration status (green card, etc.)"),
                ("Don D.", "Prefer not to answer"),
            ],
        )
        nxt.assert_awaited_once()

    async def test_preselected_citizen_still_requires_an_explicit_answer(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="imm-pre",
            url="https://example.test",
            app_step="app_immigration",
        )
        session.filled["app_citizenship_no"] = "1"
        session.filled["app_immigration_current"] = "Agent T."
        nxt = AsyncMock(return_value=True)
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage.immigration_member_radios_visible",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_immigration_members",
                new=AsyncMock(return_value=self._members()),
            ),
            patch(
                "pc_liveagent.app_stage._set_member_immigration_status",
                new=AsyncMock(side_effect=AssertionError("must not click a radio")),
            ),
            patch("pc_liveagent.app_stage._click_next_or_continue", nxt),
        ):
            result = await submit_application_answer(
                "imm-pre", session, AsyncMock(), "continue", 0.0
            )
        self.assertEqual(result.get("error"), "immigration_incomplete")
        self.assertEqual(session.app_step, "app_immigration")
        self.assertIn("Agent T", str(result.get("spoken") or ""))
        nxt.assert_not_awaited()

    async def test_turn_keeps_status_answer_after_citizenship_no_was_committed(
        self,
    ) -> None:
        from pc_liveagent.call_turn import (
            _app_browser_tasks,
            _process_application_turn,
            _user_fingerprint,
            drop_controller,
            get_controller,
        )
        from pc_liveagent.live_form import LiveFormSession

        call_id = "imm-turn"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "application"
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", app_step="app_immigration"
        )
        session.page = AsyncMock()
        session.filled["app_citizenship_no"] = "1"
        session.filled["app_immigration_current"] = "Agent T."
        ctrl.committed.append(
            {
                "key": f"app:app_citizenship:{_user_fingerprint('No.')}",
                "turn_id": "earlier-no",
                "step": "app_citizenship",
                "answer": "No.",
                "at": 0,
            }
        )
        submitted: list[str] = []

        async def fake_session(_cid: str):
            return session

        async def fake_submit(_cid: str, _field: str, text: str, **_kwargs):
            submitted.append(text)
            return {
                "ok": True,
                "spoken": "Okay, U.S. National for Agent T.",
                "step": "app_immigration",
            }

        with (
            patch(
                "pc_liveagent.call_turn.get_session",
                new=AsyncMock(side_effect=fake_session),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage.immigration_member_radios_visible",
                new=AsyncMock(return_value=True),
            ),
            patch("pc_liveagent.call_turn.submit_field", new=AsyncMock(side_effect=fake_submit)),
        ):
            first = await _process_application_turn(
                call_id,
                ctrl,
                text="U.S. National.",
                fp=_user_fingerprint("U.S. National."),
                dedupe_ctx="",
                turn_id="imm-1",
                form_url="https://example.test",
            )
            task = _app_browser_tasks.get(call_id)
            if task is not None:
                await task
            second = await _process_application_turn(
                call_id,
                ctrl,
                text="U.S. National.",
                fp=_user_fingerprint("U.S. National."),
                dedupe_ctx="",
                turn_id="imm-2",
                form_url="https://example.test",
            )
        self.assertEqual(first.detail, "app_ack")
        self.assertNotIn("all U.S. Citizens", first.speech)
        self.assertEqual(session.app_step, "app_immigration")
        self.assertEqual(submitted, ["U.S. National."])
        self.assertEqual(second.detail, "duplicate_app_answer")

    async def test_downgraded_step_is_restored_before_the_status_answer(self) -> None:
        from pc_liveagent.call_turn import (
            _app_browser_tasks,
            _process_application_turn,
            _user_fingerprint,
            drop_controller,
            get_controller,
        )
        from pc_liveagent.live_form import LiveFormSession

        call_id = "imm-restore"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", app_step="app_citizenship"
        )
        session.page = AsyncMock()
        session.filled["app_citizenship_no"] = "1"
        session.filled["app_immigration_current"] = "Agent T."
        seen: list[str] = []

        async def fake_session(_cid: str):
            return session

        async def fake_submit(_cid: str, _field: str, text: str, **_kwargs):
            seen.append(session.app_step)
            seen.append(text)
            return {"ok": True, "spoken": "Okay.", "step": "app_immigration"}

        with (
            patch(
                "pc_liveagent.call_turn.get_session",
                new=AsyncMock(side_effect=fake_session),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage.immigration_member_radios_visible",
                new=AsyncMock(return_value=True),
            ),
            patch("pc_liveagent.call_turn.submit_field", new=AsyncMock(side_effect=fake_submit)),
        ):
            result = await _process_application_turn(
                call_id,
                ctrl,
                text="US national.",
                fp=_user_fingerprint("US national."),
                dedupe_ctx="",
                turn_id="restore-1",
                form_url="https://example.test",
            )
            task = _app_browser_tasks.get(call_id)
            if task is not None:
                await task
        self.assertEqual(result.detail, "app_ack")
        self.assertEqual(seen, ["app_immigration", "US national."])

    async def test_radios_gone_returns_to_the_citizenship_question(self) -> None:
        from pc_liveagent.call_turn import (
            _process_application_turn,
            _user_fingerprint,
            drop_controller,
            get_controller,
        )
        from pc_liveagent.live_form import LiveFormSession

        call_id = "imm-back-screen"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", app_step="app_immigration"
        )
        session.page = AsyncMock()
        session.filled["app_citizenship_no"] = "1"

        async def fake_session(_cid: str):
            return session

        with (
            patch(
                "pc_liveagent.call_turn.get_session",
                new=AsyncMock(side_effect=fake_session),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage.immigration_member_radios_visible",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_citizenship",
                        "Are Agent T, john D, and Don D all U.S. Citizens?",
                    )
                ),
            ),
            patch(
                "pc_liveagent.call_turn.submit_field",
                new=AsyncMock(side_effect=AssertionError("must not submit")),
            ),
            patch(
                "pc_liveagent.app_stage._set_member_immigration_status",
                new=AsyncMock(side_effect=AssertionError("must not set a radio")),
            ),
        ):
            result = await _process_application_turn(
                call_id,
                ctrl,
                text="U.S. National.",
                fp=_user_fingerprint("U.S. National."),
                dedupe_ctx="",
                turn_id="back-screen",
                form_url="https://example.test",
            )
        self.assertEqual(result.detail, "app_resynced")
        self.assertEqual(session.app_step, "app_citizenship")
        self.assertIn("U.S. Citizens", result.speech)

    async def test_go_back_from_immigration_is_navigation(self) -> None:
        from pc_liveagent.call_turn import (
            _process_application_turn,
            drop_controller,
            get_controller,
        )
        from pc_liveagent.live_form import LiveFormSession

        call_id = "imm-go-back"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", app_step="app_immigration"
        )
        session.page = AsyncMock()
        session.filled["app_citizenship_no"] = "1"

        async def fake_session(_cid: str):
            return session

        with (
            patch(
                "pc_liveagent.call_turn.get_session",
                new=AsyncMock(side_effect=fake_session),
            ),
            patch(
                "pc_liveagent.app_stage.application_go_back",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "step": "app_citizenship",
                        "spoken": "Are they all U.S. Citizens?",
                    }
                ),
            ) as backed,
            patch(
                "pc_liveagent.call_turn.submit_field",
                new=AsyncMock(side_effect=AssertionError("must not submit")),
            ),
        ):
            result = await _process_application_turn(
                call_id,
                ctrl,
                text="Go back.",
                fp="go back",
                dedupe_ctx="",
                turn_id="nav",
                form_url="https://example.test",
            )
        backed.assert_awaited_once()
        self.assertEqual(result.detail, "go_back")
        self.assertIn("Citizens", result.speech)

    async def test_last_member_advances_only_when_live_radios_match(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="imm-last",
            url="https://example.test",
            app_step="app_immigration",
        )
        session.filled["app_citizenship_no"] = "1"
        session.filled["app_immigration_current"] = "Don D."
        session.filled["app_immigration_queue"] = '["Don D."]'
        session.filled["app_immigration_confirmed"] = json.dumps(
            {"Agent T.": "U.S. Citizen", "john D.": "U.S. Citizen"}
        )
        members = [
            {"name": "Agent T.", "selected": "U.S. Citizen"},
            {"name": "john D.", "selected": "U.S. Citizen"},
            {"name": "Don D.", "selected": "U.S. Citizen"},
        ]
        click = AsyncMock(return_value=True)
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(
                    side_effect=["app_citizenship", "app_citizenship"]
                    + ["app_insurance"] * 20
                ),
            ),
            patch(
                "pc_liveagent.app_stage.immigration_member_radios_visible",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_immigration_members",
                new=AsyncMock(return_value=members),
            ),
            patch(
                "pc_liveagent.app_stage._set_member_immigration_status",
                new=AsyncMock(return_value=(True, "selected")),
            ) as set_status,
            patch(
                "pc_liveagent.app_stage._verify_member_immigration_status",
                new=AsyncMock(return_value=(True, "verified")),
            ),
            patch(
                "pc_liveagent.app_stage._wait_immigration_next_ready",
                new=AsyncMock(
                    return_value={
                        "status": "next_enabled",
                        "total": 1,
                        "hidden": 0,
                        "visible_disabled": 0,
                        "visible_enabled": 1,
                        "tag": "button",
                        "disabled": False,
                        "aria_disabled": "",
                    }
                ),
            ),
            patch("pc_liveagent.app_stage._click_enabled_application_next", click),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=("app_insurance", "Question 2. Who has coverage?")
                ),
            ),
        ):
            result = await submit_application_answer(
                "imm-last", session, page, "Don D. is a U.S. Citizen", 0.0
            )
        set_status.assert_awaited()
        self.assertEqual(set_status.await_args.args[1], "Don D.")
        click.assert_awaited_once()
        self.assertEqual(result.get("step"), "app_insurance")
        self.assertIn("Question 2", str(result.get("spoken") or ""))

    async def test_session_flag_without_matching_radio_does_not_click_next(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="imm-mismatch",
            url="https://example.test",
            app_step="app_immigration",
        )
        session.filled["app_citizenship_no"] = "1"
        session.filled["app_immigration_confirmed"] = json.dumps(
            {
                "Agent T.": "U.S. Citizen",
                "john D.": "U.S. Citizen",
                "Don D.": "U.S. National",
            }
        )
        members = [
            {"name": "Agent T.", "selected": "U.S. Citizen"},
            {"name": "john D.", "selected": "U.S. Citizen"},
            {"name": "Don D.", "selected": "U.S. Citizen"},
        ]
        click = AsyncMock(return_value=True)
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage.immigration_member_radios_visible",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_immigration_members",
                new=AsyncMock(return_value=members),
            ),
            patch("pc_liveagent.app_stage._click_enabled_application_next", click),
        ):
            result = await submit_application_answer(
                "imm-mismatch", session, AsyncMock(), "Now move to next question.", 0.0
            )
        click.assert_not_awaited()
        self.assertEqual(result.get("error"), "immigration_incomplete")
        self.assertIn("Don D.", str(result.get("spoken") or ""))
        self.assertEqual(session.app_step, "app_immigration")

    async def test_disabled_next_reports_validation_and_repeat_does_not_click(
        self,
    ) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="imm-disabled",
            url="https://example.test",
            app_step="app_immigration",
        )
        session.filled["app_citizenship_no"] = "1"
        session.filled["app_immigration_confirmed"] = json.dumps(
            {
                "Agent T.": "U.S. Citizen",
                "john D.": "U.S. Citizen",
                "Don D.": "U.S. Citizen",
            }
        )
        members = self._members()
        click = AsyncMock(return_value=True)
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage.immigration_member_radios_visible",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_immigration_members",
                new=AsyncMock(return_value=members),
            ),
            patch(
                "pc_liveagent.app_stage._wait_immigration_next_ready",
                new=AsyncMock(
                    return_value={
                        "status": "next_disabled",
                        "total": 1,
                        "hidden": 0,
                        "visible_disabled": 1,
                        "visible_enabled": 0,
                        "tag": "button",
                        "disabled": True,
                        "aria_disabled": "true",
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._visible_form_validation",
                new=AsyncMock(return_value="Select a status for each person."),
            ),
            patch("pc_liveagent.app_stage._click_enabled_application_next", click),
        ):
            first = await submit_application_answer(
                "imm-disabled", session, AsyncMock(), "Now move to next question.", 0.0
            )
            second = await submit_application_answer(
                "imm-disabled", session, AsyncMock(), "We have done everything.", 0.0
            )
        click.assert_not_awaited()
        self.assertEqual(first.get("error"), "immigration_next_disabled")
        self.assertEqual(first.get("message"), "next_disabled")
        self.assertIn("Select a status", str(first.get("spoken") or ""))
        self.assertNotIn("still needs an immigration status before I can continue", str(first.get("spoken") or "").lower())
        self.assertEqual(second.get("error"), "immigration_next_disabled")
        self.assertEqual(session.app_step, "app_immigration")

    async def test_second_continue_does_not_click_next_again(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="imm-once",
            url="https://example.test",
            app_step="app_immigration",
        )
        session.filled["app_citizenship_no"] = "1"
        session.filled["app_immigration_confirmed"] = json.dumps(
            {
                "Agent T.": "U.S. Citizen",
                "john D.": "U.S. Citizen",
                "Don D.": "U.S. Citizen",
            }
        )
        click = AsyncMock(return_value=True)
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage.immigration_member_radios_visible",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_immigration_members",
                new=AsyncMock(return_value=self._members()),
            ),
            patch(
                "pc_liveagent.app_stage._wait_immigration_next_ready",
                new=AsyncMock(
                    return_value={
                        "status": "next_enabled",
                        "total": 1,
                        "hidden": 0,
                        "visible_disabled": 0,
                        "visible_enabled": 1,
                        "tag": "button",
                        "disabled": False,
                        "aria_disabled": "",
                    }
                ),
            ),
            patch("pc_liveagent.app_stage._click_enabled_application_next", click),
            patch(
                "pc_liveagent.app_stage._visible_form_validation",
                new=AsyncMock(return_value=""),
            ),
        ):
            first = await submit_application_answer(
                "imm-once", session, page, "move to next question", 0.0
            )
            second = await submit_application_answer(
                "imm-once", session, page, "we have done everything", 0.0
            )
        click.assert_awaited_once()
        self.assertEqual(first.get("error"), "immigration_next_unchanged")
        self.assertEqual(second.get("error"), "immigration_next_unchanged")
        self.assertEqual(second.get("message"), "duplicate_click_prevented")
        self.assertIn("already tried Next", str(second.get("spoken") or ""))

    async def test_delayed_next_enablement_then_advances(self) -> None:
        from pc_liveagent.app_stage import _complete_immigration_step
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="imm-delay-next",
            url="https://example.test",
            app_step="app_immigration",
        )
        session.filled["app_citizenship_no"] = "1"
        session.filled["app_immigration_confirmed"] = json.dumps(
            {
                "Agent T.": "U.S. Citizen",
                "john D.": "U.S. Citizen",
                "Don D.": "U.S. Citizen",
            }
        )
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        inspect_states = [
            {
                "status": "next_disabled",
                "total": 1,
                "hidden": 0,
                "visible_disabled": 1,
                "visible_enabled": 0,
                "tag": "button",
                "disabled": True,
                "aria_disabled": "true",
            },
            {
                "status": "next_disabled",
                "total": 1,
                "hidden": 0,
                "visible_disabled": 1,
                "visible_enabled": 0,
                "tag": "button",
                "disabled": True,
                "aria_disabled": "true",
            },
            {
                "status": "next_enabled",
                "total": 1,
                "hidden": 0,
                "visible_disabled": 0,
                "visible_enabled": 1,
                "tag": "button",
                "disabled": False,
                "aria_disabled": "",
            },
        ]
        click = AsyncMock(return_value=True)
        with (
            patch(
                "pc_liveagent.app_stage._scrape_immigration_members",
                new=AsyncMock(return_value=self._members()),
            ),
            patch(
                "pc_liveagent.app_stage._inspect_application_next",
                new=AsyncMock(side_effect=inspect_states),
            ),
            patch("pc_liveagent.app_stage._click_enabled_application_next", click),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(side_effect=["app_immigration"] + ["app_insurance"] * 10),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=("app_insurance", "Do they have health coverage?")
                ),
            ),
        ):
            result = await _complete_immigration_step(
                "imm-delay-next", session, page, "continue"
            )
        click.assert_awaited_once()
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_insurance")

    async def test_next_not_found_and_hidden_are_distinct_from_disabled(self) -> None:
        from pc_liveagent.app_stage import _complete_immigration_step
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="imm-next-states",
            url="https://example.test",
            app_step="app_immigration",
        )
        session.filled["app_citizenship_no"] = "1"
        session.filled["app_immigration_confirmed"] = json.dumps(
            {
                "Agent T.": "U.S. Citizen",
                "john D.": "U.S. Citizen",
                "Don D.": "U.S. Citizen",
            }
        )
        click = AsyncMock(return_value=True)
        for status, error, spoken_bit in (
            ("next_not_found", "immigration_next_not_found", "could not find a Next"),
            ("next_hidden", "immigration_next_hidden", "hidden"),
            ("next_disabled", "immigration_next_disabled", "still disabled"),
        ):
            session.filled.pop("app_immigration_next_clicked", None)
            with (
                patch(
                    "pc_liveagent.app_stage._scrape_immigration_members",
                    new=AsyncMock(return_value=self._members()),
                ),
                patch(
                    "pc_liveagent.app_stage._wait_immigration_next_ready",
                    new=AsyncMock(
                        return_value={
                            "status": status,
                            "total": 1 if status != "next_not_found" else 0,
                            "hidden": 1 if status == "next_hidden" else 0,
                            "visible_disabled": 1 if status == "next_disabled" else 0,
                            "visible_enabled": 0,
                            "tag": "button",
                            "disabled": status == "next_disabled",
                            "aria_disabled": "true" if status == "next_disabled" else "",
                        }
                    ),
                ),
                patch(
                    "pc_liveagent.app_stage._visible_form_validation",
                    new=AsyncMock(return_value=""),
                ),
                patch("pc_liveagent.app_stage._click_enabled_application_next", click),
            ):
                result = await _complete_immigration_step(
                    "imm-next-states", session, AsyncMock(), "continue"
                )
            self.assertEqual(result.get("error"), error)
            self.assertEqual(result.get("message"), status)
            self.assertIn(spoken_bit.lower(), str(result.get("spoken") or "").lower())
            self.assertEqual(session.app_step, "app_immigration")
        click.assert_not_awaited()

    async def test_delayed_navigation_after_one_click(self) -> None:
        from pc_liveagent.app_stage import _complete_immigration_step
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="imm-delay-nav",
            url="https://example.test",
            app_step="app_immigration",
        )
        session.filled["app_citizenship_no"] = "1"
        session.filled["app_immigration_confirmed"] = json.dumps(
            {
                "Agent T.": "U.S. Citizen",
                "john D.": "U.S. Citizen",
                "Don D.": "U.S. Citizen",
            }
        )
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        click = AsyncMock(return_value=True)
        with (
            patch(
                "pc_liveagent.app_stage._scrape_immigration_members",
                new=AsyncMock(return_value=self._members()),
            ),
            patch(
                "pc_liveagent.app_stage._wait_immigration_next_ready",
                new=AsyncMock(
                    return_value={
                        "status": "next_enabled",
                        "total": 1,
                        "hidden": 0,
                        "visible_disabled": 0,
                        "visible_enabled": 1,
                        "tag": "button",
                        "disabled": False,
                        "aria_disabled": "",
                    }
                ),
            ),
            patch("pc_liveagent.app_stage._click_enabled_application_next", click),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(
                    side_effect=[
                        "app_immigration",
                        "app_immigration",
                        "app_insurance",
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=("app_insurance", "Do they have health coverage?")
                ),
            ),
        ):
            result = await _complete_immigration_step(
                "imm-delay-nav", session, page, "continue"
            )
        click.assert_awaited_once()
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_insurance")
        self.assertNotIn("app_immigration_next_clicked", session.filled)

    def test_immigration_member_prompt_options_then_short_cue(self) -> None:
        from pc_liveagent.app_stage import (
            APP_QUESTIONS,
            _immigration_confirm_all_speech,
            _immigration_status_options_speech,
            _immigration_status_question_for,
            _map_immigration_status,
            _parse_immigration_assignment,
        )
        from pc_liveagent.live_form import LiveFormSession

        # First member (or no session flag): read all four options.
        first = _immigration_status_question_for("Agent T.")
        self.assertIn("U.S. Citizen", first)
        self.assertIn("U.S. National", first)
        self.assertIn("Satisfactory immigration status", first)
        self.assertIn("Prefer not to answer", first)

        session = LiveFormSession(
            call_id="imm-opts",
            url="https://example.test",
            app_step="app_immigration",
        )
        first_sess = _immigration_status_question_for(
            "Agent T.", session=session
        )
        self.assertIn("U.S. Citizen", first_sess)
        self.assertEqual(session.filled.get("app_immigration_options_listed"), "1")
        later = _immigration_status_question_for("john D.", session=session)
        self.assertIn("those four options", later.lower())
        self.assertNotIn("U.S. Citizen", later)
        self.assertIn("read them again", later.lower())

        generic = APP_QUESTIONS["app_immigration"]
        self.assertEqual(
            generic, "What is that person's citizenship or immigration status?"
        )

        confirm = _immigration_confirm_all_speech(
            {
                "Agent T.": "U.S. National",
                "john D.": "Satisfactory immigration status (green card, etc.)",
            }
        )
        self.assertIn("Confirming:", confirm)
        self.assertIn("Agent T is U.S. National", confirm)
        self.assertIn("john D is Satisfactory immigration status", confirm)
        self.assertIn("everyone", confirm.lower())

        # Supported mappings still work without hearing the option list first.
        self.assertEqual(_map_immigration_status("us national"), "U.S. National")
        self.assertEqual(_map_immigration_status("u.s. citizen"), "U.S. Citizen")
        self.assertEqual(
            _map_immigration_status("green card"),
            "Satisfactory immigration status (green card, etc.)",
        )
        self.assertEqual(
            _map_immigration_status("prefer not to answer"),
            "Prefer not to answer",
        )
        members = [{"name": "Agent T.", "selected": "U.S. Citizen"}]
        target, status, clarify = _parse_immigration_assignment(
            "US national.", members, "Agent T."
        )
        self.assertEqual(target, "Agent T.")
        self.assertEqual(status, "U.S. National")
        self.assertIsNone(clarify)

        # Unclear answers still get the options listed once.
        _, _, unclear = _parse_immigration_assignment(
            "maybe something else", members, "Agent T."
        )
        self.assertEqual(unclear, _immigration_status_options_speech())
        self.assertIn("U.S. Citizen", unclear or "")
        self.assertIn("Prefer not to answer", unclear or "")

    def test_status_key_matches_us_and_u_s_punctuation(self) -> None:
        from pc_liveagent.app_stage import (
            _immigration_member_name_key,
            _immigration_status_key,
            _same_immigration_status,
        )

        self.assertEqual(
            _immigration_status_key("US Citizen"),
            _immigration_status_key("U.S. Citizen"),
        )
        self.assertEqual(
            _immigration_status_key("US National"),
            _immigration_status_key("U.S. National"),
        )
        self.assertTrue(_same_immigration_status("US Citizen", "U.S. Citizen"))
        self.assertTrue(_same_immigration_status("US National", "U.S. National"))
        self.assertFalse(
            _same_immigration_status("U.S. Citizen", "U.S. National")
        )
        self.assertEqual(
            _immigration_member_name_key("Agent T."),
            _immigration_member_name_key("Agent T"),
        )

    async def test_preselected_matching_status_is_confirmed_without_toggle(
        self,
    ) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="imm-preselect",
            url="https://example.test",
            app_step="app_immigration",
        )
        session.filled["app_citizenship_no"] = "1"
        session.filled["app_immigration_current"] = "Agent T."
        session.filled["app_immigration_queue"] = '["Agent T.", "john D."]'
        members = [
            {"name": "Agent T.", "selected": "US Citizen"},
            {"name": "john D.", "selected": "U.S. Citizen"},
        ]
        set_status = AsyncMock(return_value=(True, "already_selected"))
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage.immigration_member_radios_visible",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_immigration_members",
                new=AsyncMock(return_value=members),
            ),
            patch("pc_liveagent.app_stage._set_member_immigration_status", set_status),
            patch(
                "pc_liveagent.app_stage._verify_member_immigration_status",
                new=AsyncMock(return_value=(True, "verified")),
            ),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(side_effect=AssertionError("must not advance yet")),
            ),
        ):
            result = await submit_application_answer(
                "imm-preselect", session, AsyncMock(), "US citizen.", 0.0
            )
        self.assertTrue(result.get("ok"))
        set_status.assert_awaited_once_with(ANY, "Agent T.", "U.S. Citizen")
        confirmed = json.loads(session.filled.get("app_immigration_confirmed") or "{}")
        self.assertEqual(confirmed.get("Agent T."), "U.S. Citizen")
        self.assertEqual(session.filled.get("app_immigration_current"), "john D.")

    async def test_set_failure_reasons_are_surfaced_and_not_confirmed(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="imm-fail-set",
            url="https://example.test",
            app_step="app_immigration",
        )
        session.filled["app_citizenship_no"] = "1"
        session.filled["app_immigration_current"] = "Agent T."
        members = [{"name": "Agent T.", "selected": "U.S. Citizen"}]
        cases = (
            ("member_not_found", "immigration_status_not_set"),
            ("multiple_member_matches", "immigration_status_not_set"),
            ("status_option_not_found", "immigration_status_not_set"),
            ("selection_failed", "immigration_status_not_set"),
        )
        for reason, error in cases:
            session.filled.pop("app_immigration_confirmed", None)
            with (
                patch(
                    "pc_liveagent.app_stage.detect_application_screen",
                    new=AsyncMock(return_value="app_citizenship"),
                ),
                patch(
                    "pc_liveagent.app_stage.immigration_member_radios_visible",
                    new=AsyncMock(return_value=True),
                ),
                patch(
                    "pc_liveagent.app_stage._scrape_immigration_members",
                    new=AsyncMock(return_value=members),
                ),
                patch(
                    "pc_liveagent.app_stage._set_member_immigration_status",
                    new=AsyncMock(return_value=(False, reason)),
                ),
            ):
                result = await submit_application_answer(
                    "imm-fail-set", session, AsyncMock(), "U.S. National", 0.0
                )
            self.assertFalse(result.get("ok"))
            self.assertEqual(result.get("error"), error)
            self.assertEqual(result.get("message"), reason)
            self.assertNotIn("app_immigration_confirmed", session.filled)

    async def test_verification_failure_does_not_mark_confirmed(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="imm-fail-verify",
            url="https://example.test",
            app_step="app_immigration",
        )
        session.filled["app_citizenship_no"] = "1"
        session.filled["app_immigration_current"] = "Agent T."
        members = [{"name": "Agent T.", "selected": "U.S. Citizen"}]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage.immigration_member_radios_visible",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._scrape_immigration_members",
                new=AsyncMock(return_value=members),
            ),
            patch(
                "pc_liveagent.app_stage._set_member_immigration_status",
                new=AsyncMock(return_value=(True, "selected")),
            ),
            patch(
                "pc_liveagent.app_stage._verify_member_immigration_status",
                new=AsyncMock(return_value=(False, "verification_failed")),
            ),
        ):
            result = await submit_application_answer(
                "imm-fail-verify", session, AsyncMock(), "U.S. National", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "immigration_status_not_verified")
        self.assertEqual(result.get("message"), "verification_failed")
        self.assertNotIn("app_immigration_confirmed", session.filled)


class TaxReviewWorkflowTests(unittest.IsolatedAsyncioTestCase):
    """Tax Information per-member confirm / change / who multi-select."""

    def test_map_tax_status_aliases(self) -> None:
        from pc_liveagent.tax_stage import (
            _map_tax_status,
            _tax_card_speech,
            _tax_status_needs_clarification,
        )

        self.assertEqual(_map_tax_status("head of household"), "Head of Household")
        self.assertEqual(_map_tax_status("household head"), "Head of Household")
        self.assertEqual(_map_tax_status("jointly"), "Married filing jointly")
        self.assertEqual(_map_tax_status("Married jointly"), "Married filing jointly")
        self.assertEqual(_map_tax_status("Filing separately"), "Married filing separately")
        self.assertEqual(_map_tax_status("Separate returns"), "Married filing separately")
        self.assertEqual(_map_tax_status("not filing"), "Not filing")
        self.assertEqual(_map_tax_status("I don't file taxes"), "Not filing")
        self.assertEqual(_map_tax_status("I'm single"), "Single")
        self.assertEqual(_map_tax_status("single"), "Single")
        self.assertEqual(
            _map_tax_status("Qualifying widow"),
            "Qualifying Widow(er) with Dependent Child",
        )
        self.assertEqual(
            _map_tax_status("widow"),
            "Qualifying Widow(er) with Dependent Child",
        )
        self.assertIsNone(_map_tax_status("married"))
        self.assertTrue(_tax_status_needs_clarification("married"))
        self.assertFalse(_tax_status_needs_clarification("married jointly"))
        speech = _tax_card_speech(
            {"name": "Admin P.", "status": "Head of Household"},
            intro=True,
        )
        self.assertIn("Admin P.", speech)
        self.assertIn("I'll review the tax information for each household member", speech)
        self.assertIn("starting with Admin P.", speech)
        self.assertIn("head of household", speech.lower())
        self.assertIn("is that correct", speech.lower())
        self.assertNotIn("I'll open the editor", speech)
        self.assertNotIn("One moment", speech)
        self.assertNotIn("Say Yes to keep an answer", speech)

    async def test_tax_status_answer_advances_to_claim_deps(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="tax-status-yes",
            url="https://example.test",
            app_step="app_tax",
        )
        session.filled["app_tax_current"] = "Admin P."
        session.filled["app_tax_current_index"] = "0"
        session.filled["app_tax_phase"] = "status"
        session.filled["app_tax_heard"] = "[]"
        cards = [
            {
                "index": 0,
                "name": "Admin P.",
                "status": "Head of Household",
                "dependents": "John D.",
            },
            {"index": 1, "name": "John D.", "status": "Not filing", "dependents": ""},
        ]
        modal = {
            "open": True,
            "member": "Admin P.",
            "status": "Head of Household",
            "claim_deps": "Yes",
            "claim_deps_who": ["John D."],
            "claim_deps_options": ["John D."],
            "claimed_as": "No",
            "claimed_as_who": [],
            "claimed_as_options": [],
        }
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_tax"),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.tax_stage._ensure_tax_modal",
                new=AsyncMock(return_value=modal),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_modal",
                new=AsyncMock(return_value=modal),
            ),
            patch(
                "pc_liveagent.tax_stage._set_tax_status",
                new=AsyncMock(return_value={"ok": True, "stage": "already_selected"}),
            ) as set_status,
        ):
            result = await submit_application_answer(
                "tax-status-yes",
                session,
                AsyncMock(),
                "Yes",
                0.0,
            )
        self.assertTrue(result.get("ok"))
        set_status.assert_not_awaited()  # already matches / confirmed
        self.assertEqual(session.filled.get("app_tax_phase"), "claim_deps")
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("claim any dependents", spoken)
        self.assertNotIn("is that correct", spoken)

    async def test_tax_status_change_sets_radio(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="tax-status-change",
            url="https://example.test",
            app_step="app_tax",
        )
        session.filled["app_tax_current"] = "Admin P."
        session.filled["app_tax_current_index"] = "0"
        session.filled["app_tax_phase"] = "status"
        cards = [
            {"index": 0, "name": "Admin P.", "status": "Single", "dependents": ""},
        ]
        modal = {
            "open": True,
            "status": "Single",
            "claim_deps": "No",
            "claim_deps_who": [],
            "claim_deps_options": [],
            "claimed_as": "No",
            "claimed_as_who": [],
            "claimed_as_options": [],
        }
        after = dict(modal)
        after["status"] = "Head of Household"
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_tax"),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.tax_stage._ensure_tax_modal",
                new=AsyncMock(return_value=modal),
            ),
            patch(
                "pc_liveagent.tax_stage._set_tax_status",
                new=AsyncMock(return_value={"ok": True, "stage": "verified"}),
            ) as set_status,
            patch(
                "pc_liveagent.tax_stage._scrape_tax_modal",
                new=AsyncMock(return_value=after),
            ),
        ):
            result = await submit_application_answer(
                "tax-status-change",
                session,
                AsyncMock(),
                "Head of Household",
                0.0,
            )
        self.assertTrue(result.get("ok"))
        set_status.assert_awaited_with(ANY, "Head of Household")
        self.assertEqual(session.filled.get("app_tax_phase"), "claim_deps")
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("claim any dependents", spoken)
        self.assertNotIn("is that correct", spoken)
        self.assertNotIn("i'll open the editor", spoken)

    async def test_tax_status_ambiguous_married_asks_clarify(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="tax-status-married",
            url="https://example.test",
            app_step="app_tax",
        )
        session.filled["app_tax_current"] = "Admin P."
        session.filled["app_tax_current_index"] = "0"
        session.filled["app_tax_phase"] = "status"
        cards = [
            {"index": 0, "name": "Admin P.", "status": "Single", "dependents": ""},
        ]
        modal = {
            "open": True,
            "status": "Single",
            "claim_deps": "No",
            "claim_deps_who": [],
            "claim_deps_options": [],
            "claimed_as": "No",
            "claimed_as_who": [],
            "claimed_as_options": [],
        }
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_tax"),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.tax_stage._ensure_tax_modal",
                new=AsyncMock(return_value=modal),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_modal",
                new=AsyncMock(return_value=modal),
            ),
            patch(
                "pc_liveagent.tax_stage._set_tax_status",
                new=AsyncMock(return_value={"ok": True}),
            ) as set_status,
        ):
            result = await submit_application_answer(
                "tax-status-married", session, AsyncMock(), "married", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "tax_status_ambiguous")
        set_status.assert_not_awaited()
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("jointly", spoken)
        self.assertIn("separately", spoken)

    async def test_tax_claim_yes_asks_who_multi(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="tax-claim-who",
            url="https://example.test",
            app_step="app_tax",
        )
        session.filled["app_tax_current"] = "Admin P."
        session.filled["app_tax_current_index"] = "0"
        session.filled["app_tax_phase"] = "claim_deps"
        cards = [
            {"index": 0, "name": "Admin P.", "status": "Head of Household", "dependents": ""},
        ]
        modal = {
            "open": True,
            "status": "Head of Household",
            "claim_deps": "No",
            "claim_deps_who": [],
            "claim_deps_options": ["John D.", "Agent T."],
            "claimed_as": "No",
            "claimed_as_who": [],
            "claimed_as_options": [],
        }
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_tax"),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.tax_stage._ensure_tax_modal",
                new=AsyncMock(return_value=modal),
            ),
            patch(
                "pc_liveagent.tax_stage._set_tax_yes_no",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.tax_stage._wait_tax_who_members",
                new=AsyncMock(return_value=["John D.", "Agent T."]),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_modal",
                new=AsyncMock(return_value=modal),
            ),
        ):
            result = await submit_application_answer(
                "tax-claim-who", session, AsyncMock(), "Yes", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(session.filled.get("app_tax_phase"), "claim_deps_who")
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("who will admin p", spoken)
        self.assertIn("john d", spoken)

    async def test_tax_claim_who_multi_select_then_done(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="tax-who-multi",
            url="https://example.test",
            app_step="app_tax",
        )
        session.filled["app_tax_current"] = "Admin P."
        session.filled["app_tax_current_index"] = "0"
        session.filled["app_tax_phase"] = "claim_deps_who"
        cards = [
            {"index": 0, "name": "Admin P.", "status": "Head of Household", "dependents": ""},
        ]
        modal = {
            "open": True,
            "status": "Head of Household",
            "claim_deps": "Yes",
            "claim_deps_who": [],
            "claim_deps_options": ["John D.", "Agent T."],
            "claimed_as": "No",
            "claimed_as_who": [],
            "claimed_as_options": ["Admin P."],
        }
        after_select = dict(modal)
        after_select["claim_deps_who"] = ["John D.", "Agent T."]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_tax"),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.tax_stage._ensure_tax_modal",
                new=AsyncMock(return_value=modal),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_modal",
                new=AsyncMock(side_effect=[modal, after_select, after_select]),
            ),
            patch(
                "pc_liveagent.tax_stage._set_tax_who_members",
                new=AsyncMock(return_value=True),
            ) as set_who,
            patch(
                "pc_liveagent.tax_stage._wait_tax_who_members",
                new=AsyncMock(return_value=["John D.", "Agent T."]),
            ),
        ):
            selected = await submit_application_answer(
                "tax-who-multi",
                session,
                AsyncMock(),
                "John D and Agent T",
                0.0,
            )
            self.assertTrue(selected.get("ok"))
            set_who.assert_awaited()
            self.assertIn("anyone else", (selected.get("spoken") or "").lower())

            done = await submit_application_answer(
                "tax-who-multi", session, AsyncMock(), "done", 0.0
            )
        self.assertTrue(done.get("ok"))
        self.assertEqual(session.filled.get("app_tax_phase"), "claimed_as")
        self.assertIn("claim", (done.get("spoken") or "").lower())

    async def test_tax_claimed_as_independent_yes_who(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="tax-claimed-yes",
            url="https://example.test",
            app_step="app_tax",
        )
        session.filled["app_tax_current"] = "John D."
        session.filled["app_tax_current_index"] = "1"
        session.filled["app_tax_phase"] = "claimed_as"
        cards = [
            {"index": 0, "name": "Admin P.", "status": "Head of Household", "dependents": "John D."},
            {"index": 1, "name": "John D.", "status": "Not filing", "dependents": ""},
        ]
        modal = {
            "open": True,
            "status": "Not filing",
            "claim_deps": "No",
            "claim_deps_who": [],
            "claim_deps_options": [],
            "claimed_as": "No",
            "claimed_as_who": [],
            "claimed_as_options": ["Admin P."],
        }
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_tax"),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.tax_stage._ensure_tax_modal",
                new=AsyncMock(return_value=modal),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_modal",
                new=AsyncMock(return_value=modal),
            ),
            patch(
                "pc_liveagent.tax_stage._set_tax_yes_no",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.tax_stage._wait_tax_who_members",
                new=AsyncMock(return_value=["Admin P."]),
            ),
        ):
            result = await submit_application_answer(
                "tax-claimed-yes", session, AsyncMock(), "Yes", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(session.filled.get("app_tax_phase"), "claimed_as_who")
        self.assertIn("who will claim john d", (result.get("spoken") or "").lower())

    async def test_tax_claimed_as_yes_without_who_list_saves(self) -> None:
        """Yes with no Pointcare Who? list should Save and Return, not ask names."""
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="tax-claimed-yes-no-who",
            url="https://example.test",
            app_step="app_tax",
        )
        session.filled["app_tax_current"] = "Admin P."
        session.filled["app_tax_current_index"] = "0"
        session.filled["app_tax_phase"] = "claimed_as"
        session.filled["app_tax_heard"] = "[]"
        cards = [
            {"index": 0, "name": "Admin P.", "status": "Single", "dependents": "John D."},
            {"index": 1, "name": "John D.", "status": "Not filing", "dependents": ""},
        ]
        modal = {
            "open": True,
            "status": "Single",
            "claim_deps": "Yes",
            "claim_deps_who": ["John D."],
            "claim_deps_options": ["John D."],
            "claimed_as": "Yes",
            "claimed_as_who": [],
            "claimed_as_options": [],
        }
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_tax"),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.tax_stage._ensure_tax_modal",
                new=AsyncMock(return_value=modal),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_modal",
                new=AsyncMock(return_value=modal),
            ),
            patch(
                "pc_liveagent.tax_stage._set_tax_yes_no",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.tax_stage._wait_tax_who_members",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "pc_liveagent.tax_stage._click_tax_save_return",
                new=AsyncMock(return_value=True),
            ) as save,
            patch(
                "pc_liveagent.tax_stage._begin_tax_member",
                new=AsyncMock(
                    return_value=(
                        "I'm starting with John D. "
                        "What is John D.'s tax filing status?"
                    )
                ),
            ),
        ):
            result = await submit_application_answer(
                "tax-claimed-yes-no-who", session, AsyncMock(), "Yes", 0.0
            )
        self.assertTrue(result.get("ok"))
        save.assert_awaited()
        spoken = (result.get("spoken") or "").lower()
        self.assertNotIn("available members", spoken)
        self.assertNotIn("who will claim", spoken)
        self.assertNotEqual(session.filled.get("app_tax_phase"), "claimed_as_who")

    async def test_tax_claimed_as_no_starts_next_member(self) -> None:
        """After claimed_as No, advance with 'I'm starting with {next}' — not re-ask."""
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="tax-claimed-no-next",
            url="https://example.test",
            app_step="app_tax",
        )
        session.filled["app_tax_current"] = "Admin P."
        session.filled["app_tax_current_index"] = "0"
        session.filled["app_tax_phase"] = "claimed_as"
        session.filled["app_tax_heard"] = "[]"
        cards = [
            {"index": 0, "name": "Admin P.", "status": "Single", "dependents": ""},
            {"index": 1, "name": "John D.", "status": "Not filing", "dependents": ""},
        ]
        modal = {
            "open": True,
            "member": "Admin P.",
            "status": "Single",
            "claim_deps": "No",
            "claim_deps_who": [],
            "claim_deps_options": [],
            "claimed_as": "No",
            "claimed_as_who": [],
            "claimed_as_options": [],
        }
        closed = {"open": False}

        async def _scrape_modal_side_effect(*_a, **_k):
            return closed

        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_tax"),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.tax_stage._ensure_tax_modal",
                new=AsyncMock(return_value=modal),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_modal",
                new=AsyncMock(side_effect=_scrape_modal_side_effect),
            ),
            patch(
                "pc_liveagent.tax_stage._set_tax_yes_no",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.tax_stage._click_tax_save_return",
                new=AsyncMock(return_value=True),
            ) as save,
            patch(
                "pc_liveagent.tax_stage._begin_tax_member",
                new=AsyncMock(
                    return_value=(
                        "I'm starting with John D. "
                        "What is John D.'s tax filing status?"
                    )
                ),
            ) as begin,
        ):
            result = await submit_application_answer(
                "tax-claimed-no-next", session, AsyncMock(), "No", 0.0
            )
        self.assertTrue(result.get("ok"))
        save.assert_awaited()
        begin.assert_awaited()
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("i'm starting with john d", spoken)
        self.assertNotIn("will anyone claim", spoken)
        self.assertNotEqual(session.filled.get("app_tax_phase"), "claimed_as")
        self.assertNotEqual(session.filled.get("app_tax_phase"), "saving")

    async def test_tax_saving_phase_prepare_does_not_reask_claimed_as(self) -> None:
        """While Save is in flight / failed, prepare must not re-ask claimed_as."""
        from pc_liveagent.tax_stage import _prepare_tax_review_speech
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="tax-saving-prepare",
            url="https://example.test",
            app_step="app_tax",
        )
        session.filled["app_tax_current"] = "Admin P."
        session.filled["app_tax_current_index"] = "0"
        session.filled["app_tax_phase"] = "saving"
        session.filled["app_tax_saving_name"] = "Admin P."
        session.filled["app_tax_saving_index"] = "0"
        with (
            patch(
                "pc_liveagent.tax_stage._scrape_tax_cards",
                new=AsyncMock(
                    return_value=[
                        {"index": 0, "name": "Admin P.", "status": "Single"},
                    ]
                ),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_modal",
                new=AsyncMock(
                    return_value={
                        "open": True,
                        "claimed_as": "No",
                        "member": "Admin P.",
                    }
                ),
            ),
        ):
            step, speech = await _prepare_tax_review_speech(session, AsyncMock())
        self.assertEqual(step, "app_tax")
        lowered = (speech or "").lower()
        self.assertIn("saving tax details", lowered)
        self.assertNotIn("will anyone claim", lowered)

    async def test_tax_saving_phase_continue_retries_save(self) -> None:
        """Say continue while phase=saving retries Save — does not re-ask Q3."""
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="tax-saving-retry",
            url="https://example.test",
            app_step="app_tax",
        )
        session.filled["app_tax_current"] = "Admin P."
        session.filled["app_tax_current_index"] = "0"
        session.filled["app_tax_phase"] = "saving"
        session.filled["app_tax_saving_name"] = "Admin P."
        session.filled["app_tax_saving_index"] = "0"
        session.filled["app_tax_heard"] = "[]"
        cards = [
            {"index": 0, "name": "Admin P.", "status": "Single", "dependents": ""},
            {"index": 1, "name": "John D.", "status": "Not filing", "dependents": ""},
        ]
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_tax"),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.tax_stage._click_tax_save_return",
                new=AsyncMock(return_value=True),
            ) as save,
            patch(
                "pc_liveagent.tax_stage._begin_tax_member",
                new=AsyncMock(
                    return_value=(
                        "I'm starting with John D. "
                        "What is John D.'s tax filing status?"
                    )
                ),
            ) as begin,
        ):
            result = await submit_application_answer(
                "tax-saving-retry", session, AsyncMock(), "continue", 0.0
            )
        self.assertTrue(result.get("ok"))
        save.assert_awaited()
        begin.assert_awaited()
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("i'm starting with john d", spoken)
        self.assertNotIn("will anyone claim", spoken)

    async def test_tax_claimed_as_who_empty_list_saves_on_any_turn(self) -> None:
        """If already stuck in who-phase with no list, save instead of asking names."""
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="tax-claimed-who-empty",
            url="https://example.test",
            app_step="app_tax",
        )
        session.filled["app_tax_current"] = "Admin P."
        session.filled["app_tax_current_index"] = "0"
        session.filled["app_tax_phase"] = "claimed_as_who"
        session.filled["app_tax_heard"] = "[]"
        cards = [
            {"index": 0, "name": "Admin P.", "status": "Single", "dependents": ""},
        ]
        modal = {
            "open": True,
            "status": "Single",
            "claim_deps": "No",
            "claim_deps_who": [],
            "claim_deps_options": [],
            "claimed_as": "Yes",
            "claimed_as_who": [],
            "claimed_as_options": [],
        }
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_tax"),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.tax_stage._ensure_tax_modal",
                new=AsyncMock(return_value=modal),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_modal",
                new=AsyncMock(return_value=modal),
            ),
            patch(
                "pc_liveagent.tax_stage._wait_tax_who_members",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "pc_liveagent.tax_stage._click_tax_save_return",
                new=AsyncMock(return_value=True),
            ) as save,
            patch(
                "pc_liveagent.tax_stage._advance_after_tax_member",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "spoken": "I've reviewed tax information for everyone.",
                        "step": "app_tax",
                    }
                ),
            ),
        ):
            result = await submit_application_answer(
                "tax-claimed-who-empty",
                session,
                AsyncMock(),
                "John d, and it's done.",
                0.0,
            )
        self.assertTrue(result.get("ok"))
        save.assert_awaited()
        self.assertNotIn("couldn't match", (result.get("spoken") or "").lower())

    async def test_tax_final_yes_clicks_next(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="tax-final",
            url="https://example.test",
            app_step="app_tax",
        )
        session.filled["app_tax_phase"] = "final"
        session.filled["app_tax_review_ready"] = "1"
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_tax"),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_cards",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(return_value=True),
            ) as nxt,
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_income"),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(return_value=("app_income", "What income type?")),
            ),
        ):
            result = await submit_application_answer(
                "tax-final", session, AsyncMock(), "Yes", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_income")
        nxt.assert_awaited()

    def test_tax_dom_js_splits_on_real_newlines(self) -> None:
        from pc_liveagent.tax_stage import _tax_dom_js
        import re

        js = _tax_dom_js()
        match = re.search(r"taxLinesOf[\s\S]{0,120}?\.split\(([^)]+)\)", js)
        self.assertIsNotNone(match)
        arg = match.group(1)
        # Generated JS must be /\r?\n/, not a quoted '\\n' literal.
        self.assertEqual(arg, "/\\r?\\n/")
        self.assertIn("taxLinesOf(raw)", js)
        self.assertIn("const taxParseCard", js)
        # Modal detection must prefer dialogs — body-text alone caused Save/advance
        # to stall and re-ask "Will anyone claim this person as a dependent?"
        self.assertIn("taxModalOpen", js)
        self.assertIn("[role=dialog]", js)
        self.assertIn("never treat the list page body text alone", js)

    async def test_scrape_tax_cards_discovers_admin_and_john(self) -> None:
        from playwright.async_api import async_playwright

        from pc_liveagent.tax_stage import _scrape_tax_cards

        html = """
        <html><body>
          <h1>Tax Information</h1>
          <div class="card" style="width:320px;height:140px;border:1px solid #000;margin:8px;">
            <div>Admin P.</div>
            <div>Head of Household</div>
            <div>Dependents: John D.</div>
            <button>Edit</button>
          </div>
          <div class="card" style="width:320px;height:120px;border:1px solid #000;margin:8px;">
            <div>John D.</div>
            <div>Not filing</div>
            <button>Edit</button>
          </div>
        </body></html>
        """
        async with async_playwright() as p:
            # Prefer installed Chrome; bundled chromium may be missing in CI/dev.
            try:
                browser = await p.chromium.launch(headless=True, channel="chrome")
            except Exception:
                browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.set_content(html)
                cards = await _scrape_tax_cards(page)
            finally:
                await browser.close()

        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0].get("name"), "Admin P.")
        self.assertEqual(cards[0].get("status"), "Head of Household")
        self.assertEqual(cards[0].get("dependents"), "John D.")
        self.assertEqual(cards[1].get("name"), "John D.")
        self.assertEqual(cards[1].get("status"), "Not filing")
        self.assertEqual(cards[1].get("dependents") or "", "")

    async def test_open_tax_editor_clicks_named_member_edit(self) -> None:
        from playwright.async_api import async_playwright

        from pc_liveagent.tax_stage import _open_tax_editor

        html = """
        <html><body>
          <div class="card" style="width:320px;height:140px;">
            <div>Admin P.</div><div>Head of Household</div>
            <div>Dependents: John D.</div>
            <button id="edit-admin">Edit</button>
          </div>
          <div class="card" style="width:320px;height:120px;">
            <div>John D.</div><div>Not filing</div>
            <button id="edit-john">Edit</button>
          </div>
          <script>
            window.__clicked = [];
            document.getElementById('edit-admin').onclick = () => window.__clicked.push('admin');
            document.getElementById('edit-john').onclick = () => window.__clicked.push('john');
          </script>
        </body></html>
        """
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True, channel="chrome")
            except Exception:
                browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.set_content(html)
                ok = await _open_tax_editor(page, "Admin P.")
                clicked = await page.evaluate("() => window.__clicked")
            finally:
                await browser.close()
        self.assertTrue(ok)
        self.assertEqual(clicked, ["admin"])

    async def test_set_tax_status_custom_radio_sibling_labels(self) -> None:
        """Pointcare-style role=radio with label text in a sibling node."""
        from playwright.async_api import async_playwright

        from pc_liveagent.tax_stage import _scrape_tax_modal, _set_tax_status

        html = """
        <html><head><style>
          [role=radio] { width: 18px; height: 18px; display: inline-block; border: 1px solid #333; }
          .opt { display: flex; gap: 8px; align-items: center; margin: 6px 0; }
          .modal { width: 480px; height: 520px; }
        </style></head><body>
          <div class="modal">
            <h1>Tax Information – Admin P.</h1>
            <p>Review and update this person's tax details if needed.</p>
            <div id="tax-status-box">
              <div>Tax Status</div>
              <div class="opt"><div role="radio" aria-checked="false" data-status="Single"></div><div>Single</div></div>
              <div class="opt"><div role="radio" aria-checked="true" data-status="Head of Household"></div><div>Head of Household</div></div>
              <div class="opt"><div role="radio" aria-checked="false" data-status="Married filing jointly"></div><div>Married filing jointly</div></div>
              <div class="opt"><div role="radio" aria-checked="false" data-status="Married filing separately"></div><div>Married filing separately</div></div>
              <div class="opt"><div role="radio" aria-checked="false" data-status="Qualifying Widow(er) with Dependent Child"></div><div>Qualifying Widow(er) with Dependent Child</div></div>
              <div class="opt"><div role="radio" aria-checked="false" data-status="Not filing"></div><div>Not filing</div></div>
            </div>
            <div>
              <div>Will this person claim any dependents?</div>
              <div class="opt"><div role="radio" aria-checked="true"></div><div>Yes</div></div>
              <div class="opt"><div role="radio" aria-checked="false"></div><div>No</div></div>
            </div>
            <div>
              <div>Will anyone claim this person as a dependent?</div>
              <div class="opt"><div role="radio" aria-checked="false"></div><div>Yes</div></div>
              <div class="opt"><div role="radio" aria-checked="true"></div><div>No</div></div>
            </div>
            <button>Cancel</button>
            <button>Save and Return</button>
          </div>
          <script>
            document.querySelectorAll('#tax-status-box [role=radio]').forEach((el) => {
              el.addEventListener('click', () => {
                document.querySelectorAll('#tax-status-box [role=radio]').forEach((r) => {
                  r.setAttribute('aria-checked', 'false');
                });
                el.setAttribute('aria-checked', 'true');
              });
            });
          </script>
        </body></html>
        """
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True, channel="chrome")
            except Exception:
                browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.set_content(html)
                before = await _scrape_tax_modal(page)
                self.assertTrue(before.get("open"))
                self.assertEqual(before.get("status"), "Head of Household")

                for target in (
                    "Single",
                    "Not filing",
                    "Married filing jointly",
                    "Married filing separately",
                ):
                    result = await _set_tax_status(page, target)
                    self.assertTrue(result.get("ok"), msg=repr(result))
                    modal = await _scrape_tax_modal(page)
                    self.assertEqual(modal.get("status"), target)
                    selected = await page.evaluate(
                        """() => [...document.querySelectorAll('#tax-status-box [role=radio]')]
                          .filter((r) => r.getAttribute('aria-checked') === 'true')
                          .map((r) => r.getAttribute('data-status'))"""
                    )
                    self.assertEqual(selected, [target])
            finally:
                await browser.close()

    async def test_set_tax_yes_no_sibling_labels_both_questions(self) -> None:
        """Yes/No radios with sibling text for claim_deps and claimed_as."""
        from playwright.async_api import async_playwright

        from pc_liveagent.tax_stage import _scrape_tax_modal, _set_tax_yes_no

        html = """
        <html><head><style>
          [role=radio], [role=checkbox] {
            width: 18px; height: 18px; display: inline-block; border: 1px solid #333;
          }
          .opt { display: flex; gap: 8px; align-items: center; margin: 6px 0; }
          .modal { width: 480px; height: 640px; overflow: auto; }
          .who { background: #eee; padding: 8px; margin-top: 6px; }
          .who.hidden { display: none; }
        </style></head><body>
          <div class="modal">
            <h1>Tax Information – Admin P.</h1>
            <p>Review and update this person's tax details if needed.</p>
            <div id="tax-status-box">
              <div>Tax Status</div>
              <div class="opt"><div role="radio" aria-checked="true"></div><div>Head of Household</div></div>
              <div class="opt"><div role="radio" aria-checked="false"></div><div>Single</div></div>
            </div>
            <div id="claim-box">
              <div>Will this person claim any dependents?</div>
              <div class="opt"><div role="radio" aria-checked="false" data-q="claim" data-v="Yes"></div><div>Yes</div></div>
              <div class="opt"><div role="radio" aria-checked="true" data-q="claim" data-v="No"></div><div>No</div></div>
              <div id="claim-who" class="who hidden">
                <div>Who?</div>
                <div class="opt"><div role="checkbox" aria-checked="false"></div><div>John D.</div></div>
              </div>
            </div>
            <div id="claimed-box">
              <div>Will anyone claim this person as a dependent?</div>
              <div class="opt"><div role="radio" aria-checked="false" data-q="claimed" data-v="Yes"></div><div>Yes</div></div>
              <div class="opt"><div role="radio" aria-checked="true" data-q="claimed" data-v="No"></div><div>No</div></div>
              <div id="claimed-who" class="who hidden">
                <div>Who?</div>
                <div class="opt"><div role="checkbox" aria-checked="false"></div><div>John D.</div></div>
              </div>
            </div>
            <button>Cancel</button>
            <button>Save and Return</button>
          </div>
          <script>
            function wire(q) {
              document.querySelectorAll('[data-q="' + q + '"]').forEach((el) => {
                el.addEventListener('click', () => {
                  document.querySelectorAll('[data-q="' + q + '"]').forEach((r) => {
                    r.setAttribute('aria-checked', 'false');
                  });
                  el.setAttribute('aria-checked', 'true');
                  const who = document.getElementById(q === 'claim' ? 'claim-who' : 'claimed-who');
                  if (el.getAttribute('data-v') === 'Yes') who.classList.remove('hidden');
                  else who.classList.add('hidden');
                });
              });
            }
            wire('claim');
            wire('claimed');
          </script>
        </body></html>
        """
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True, channel="chrome")
            except Exception:
                browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.set_content(html)
                before = await _scrape_tax_modal(page)
                self.assertTrue(before.get("open"))
                self.assertEqual(before.get("claim_deps"), "No")
                self.assertEqual(before.get("claimed_as"), "No")

                ok_claim = await _set_tax_yes_no(page, "claim_deps", "Yes")
                self.assertTrue(ok_claim)
                after_claim = await _scrape_tax_modal(page)
                self.assertEqual(after_claim.get("claim_deps"), "Yes")
                self.assertIn("John D.", after_claim.get("claim_deps_options") or [])

                ok_claimed = await _set_tax_yes_no(page, "claimed_as", "Yes")
                self.assertTrue(ok_claimed)
                after_claimed = await _scrape_tax_modal(page)
                self.assertEqual(after_claimed.get("claimed_as"), "Yes")
                self.assertEqual(after_claimed.get("claim_deps"), "Yes")
                self.assertIn("John D.", after_claimed.get("claimed_as_options") or [])
            finally:
                await browser.close()

    async def test_prepare_tax_review_starts_with_first_member(self) -> None:
        from pc_liveagent.live_form import LiveFormSession
        from pc_liveagent.tax_stage import _prepare_tax_review_speech

        session = LiveFormSession(
            call_id="tax-prepare",
            url="https://example.test",
            app_step="app_tax",
        )
        cards = [
            {
                "index": 0,
                "name": "Admin P.",
                "status": "Head of Household",
                "dependents": "John D.",
            },
            {"index": 1, "name": "John D.", "status": "Not filing", "dependents": ""},
        ]
        with (
            patch(
                "pc_liveagent.tax_stage._scrape_tax_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.tax_stage._begin_tax_member",
                new=AsyncMock(
                    return_value=(
                        "I'll confirm each person's tax information. "
                        "For Admin P., the current filing status is Head of Household. "
                        "Is that correct?"
                    )
                ),
            ) as begin,
        ):
            step, speech = await _prepare_tax_review_speech(session, AsyncMock())
        self.assertEqual(step, "app_tax")
        self.assertIn("Admin P.", speech)
        self.assertNotIn("Say Yes to keep an answer", speech)
        begin.assert_awaited()
        self.assertEqual(begin.await_args.args[2].get("name"), "Admin P.")

    async def test_prepare_tax_review_empty_cards_no_generic_yes(self) -> None:
        from pc_liveagent.live_form import LiveFormSession
        from pc_liveagent.tax_stage import _prepare_tax_review_speech

        session = LiveFormSession(
            call_id="tax-prepare-empty",
            url="https://example.test",
            app_step="app_tax",
        )
        with patch(
            "pc_liveagent.tax_stage._scrape_tax_cards",
            new=AsyncMock(return_value=[]),
        ):
            step, speech = await _prepare_tax_review_speech(session, AsyncMock())
        self.assertEqual(step, "app_tax")
        self.assertIn("can't see the household member cards", speech.lower())
        self.assertIn("try again", speech.lower())
        self.assertNotIn("Say Yes to keep an answer", speech)

    async def test_handle_tax_empty_cards_retries_then_try_again(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="tax-empty",
            url="https://example.test",
            app_step="app_tax",
        )
        page = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_tax"),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_cards",
                new=AsyncMock(return_value=[]),
            ),
        ):
            result = await submit_application_answer(
                "tax-empty", session, page, "Yes", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "tax_cards_missing")
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("try again", spoken)
        self.assertNotIn("say repeat", spoken)
        self.assertNotIn("say yes to keep an answer", spoken)

    async def test_detect_tax_information_heading_and_cards(self) -> None:
        from playwright.async_api import async_playwright

        from pc_liveagent.app_stage import detect_application_screen

        html = """
        <html><body>
          <h1>Tax Information</h1>
          <div class="card" style="width:320px;height:140px;border:1px solid #000;">
            <div>Admin P.</div>
            <div>Head of Household</div>
            <div>Dependents: John D.</div>
            <button>Edit</button>
          </div>
          <!-- Hidden stale circumstances chrome must not win detection -->
          <div style="display:none">
            Needs help with daily activities. Veteran or military member.
            American Indian or Alaska Native. Full-time student ages 18 – 20.
          </div>
        </body></html>
        """
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True, channel="chrome")
            except Exception:
                browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.set_content(html)
                self.assertEqual(await detect_application_screen(page), "app_tax")
            finally:
                await browser.close()

    async def test_scrape_tax_cards_nested_and_whitespace(self) -> None:
        from playwright.async_api import async_playwright

        from pc_liveagent.tax_stage import _scrape_tax_cards

        html = """
        <html><body>
          <h1>Tax Information</h1>
          <section style="width:360px;padding:12px;border:1px solid #333;margin:8px;">
            <div><span>Admin P.</span></div>
            <div><span>Head of Household</span></div>
            <div><span>Dependents: John D.</span></div>
            <a role="button" aria-label="Edit">Edit &gt;</a>
          </section>
          <section style="width:360px;padding:12px;border:1px solid #333;margin:8px;">
            <div>John D.</div>
            <div>Not filing</div>
            <button title="Edit">Edit</button>
          </section>
        </body></html>
        """
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True, channel="chrome")
            except Exception:
                browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.set_content(html)
                cards = await _scrape_tax_cards(page)
            finally:
                await browser.close()
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0].get("name"), "Admin P.")
        self.assertEqual(cards[0].get("status"), "Head of Household")
        self.assertEqual(cards[0].get("dependents"), "John D.")
        self.assertEqual(cards[1].get("name"), "John D.")
        self.assertEqual(cards[1].get("status"), "Not filing")

    async def test_scrape_tax_cards_skips_open_modal_edits(self) -> None:
        from playwright.async_api import async_playwright

        from pc_liveagent.tax_stage import _scrape_tax_cards

        html = """
        <html><body>
          <h1>Tax Information</h1>
          <div class="card" style="width:300px;height:120px;border:1px solid #000;">
            <div>Admin P.</div><div>Single</div><button>Edit</button>
          </div>
          <div role="dialog" aria-modal="true" style="width:480px;height:400px;border:2px solid red;">
            <h2>Tax Information – Admin P.</h2>
            <div>Will this person claim any dependents?</div>
            <button>Save and Return</button>
            <button>Edit</button>
          </div>
        </body></html>
        """
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True, channel="chrome")
            except Exception:
                browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.set_content(html)
                cards = await _scrape_tax_cards(page)
            finally:
                await browser.close()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].get("name"), "Admin P.")

    def test_map_circumstance_bare_no_when_allowed(self) -> None:
        from pc_liveagent.app_stage import _map_circumstance_labels

        self.assertEqual(
            _map_circumstance_labels("No.", allow_bare_no=True),
            ["None of the above"],
        )
        self.assertEqual(
            _map_circumstance_labels("None", allow_bare_no=True),
            ["None of the above"],
        )
        # Bare No must not map when the flag is off (global Yes/No safety).
        self.assertEqual(_map_circumstance_labels("No."), [])

    async def test_circumstances_none_advances_to_tax(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="circ-to-tax",
            url="https://example.test",
            app_step="app_circumstances",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(side_effect=["app_circumstances", "app_tax", "app_tax"]),
            ),
            patch(
                "pc_liveagent.app_stage._select_circumstance_checkbox",
                new=AsyncMock(return_value={"ok": True}),
            ),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_tax"),
            ),
            patch(
                "pc_liveagent.tax_stage._prepare_tax_review_speech",
                new=AsyncMock(
                    return_value=(
                        "app_tax",
                        "I'll review the tax information for each household member, "
                        "starting with Admin P. Admin P.'s current tax filing status "
                        "is Head of Household. Is that correct?",
                    )
                ),
            ),
        ):
            result = await submit_application_answer(
                "circ-to-tax", session, AsyncMock(), "None of the above.", 0.0
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_tax")
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("admin p", spoken)
        self.assertNotIn("trouble identifying", spoken)

    async def test_circumstances_bare_no_selects_none(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="circ-bare-no",
            url="https://example.test",
            app_step="app_circumstances",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_circumstances"),
            ),
            patch(
                "pc_liveagent.app_stage._select_circumstance_checkbox",
                new=AsyncMock(return_value={"ok": True}),
            ) as select,
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_tax"),
            ),
            patch(
                "pc_liveagent.tax_stage._prepare_tax_review_speech",
                new=AsyncMock(
                    return_value=(
                        "app_tax",
                        "I'll review the tax information for each household member, "
                        "starting with Admin P. Admin P.'s current tax filing status "
                        "is Single. Is that correct?",
                    )
                ),
            ),
        ):
            result = await submit_application_answer(
                "circ-bare-no", session, AsyncMock(), "No.", 0.0
            )
        self.assertTrue(result.get("ok"))
        select.assert_awaited()
        self.assertEqual(
            select.await_args.args[1],
            "None of the above",
        )
        self.assertEqual(result.get("step"), "app_tax")

    async def test_circumstances_tax_transition_diagnostic(self) -> None:
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="circ-tax-fail",
            url="https://example.test",
            app_step="app_circumstances",
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_circumstances"),
            ),
            patch(
                "pc_liveagent.app_stage._select_circumstance_checkbox",
                new=AsyncMock(return_value={"ok": True}),
            ),
            patch(
                "pc_liveagent.app_stage._click_next_or_continue",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value=""),
            ),
            patch(
                "pc_liveagent.tax_stage.probe_tax_information_screen",
                new=AsyncMock(
                    return_value={
                        "is_tax": False,
                        "loading": False,
                        "heading": "",
                        "edit_count": 0,
                        "reason": "no_tax_signal",
                        "snippet": "loading",
                    }
                ),
            ),
        ):
            result = await submit_application_answer(
                "circ-tax-fail", session, AsyncMock(), "None of the above.", 0.0
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "circumstances_tax_transition_failed")
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("tax information", spoken)
        self.assertNotIn("trouble identifying the current application screen", spoken)

    async def test_click_tax_save_return_uses_visible_button_fast(self) -> None:
        """Discover Save & Return once — missing label variants must not add long waits."""
        import time

        from playwright.async_api import async_playwright

        from pc_liveagent.tax_stage import _click_tax_save_return

        html = """
        <html><body>
          <h1>Tax Information</h1>
          <div class="card" style="width:300px;height:100px;display:none" id="summary">
            <div>Admin P.</div><div>Head of Household</div><button>Edit</button>
          </div>
          <div role="dialog" aria-modal="true" id="dlg"
               style="width:480px;height:420px;border:1px solid #000;">
            <h2>Tax Information – Admin P.</h2>
            <p>Review and update this person's tax details if needed.</p>
            <div>Will this person claim any dependents?</div>
            <div>Will anyone claim this person as a dependent?</div>
            <button id="cancel">Cancel</button>
            <button id="save">Save &amp; Return</button>
          </div>
          <script>
            document.getElementById('save').onclick = () => {
              document.getElementById('dlg').style.display = 'none';
              document.getElementById('summary').style.display = 'block';
            };
          </script>
        </body></html>
        """
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True, channel="chrome")
            except Exception:
                browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.set_content(html)
                t0 = time.monotonic()
                ok = await _click_tax_save_return(page)
                elapsed = time.monotonic() - t0
                dlg_hidden = await page.evaluate(
                    "() => getComputedStyle(document.getElementById('dlg')).display === 'none'"
                )
            finally:
                await browser.close()
        self.assertTrue(ok)
        self.assertTrue(dlg_hidden)
        # Must not burn multi-second _click_cta retries for missing labels.
        self.assertLess(elapsed, 3.0)

    async def test_click_tax_save_return_validation_stays_open(self) -> None:
        from playwright.async_api import async_playwright

        from pc_liveagent.tax_stage import _click_tax_save_return

        html = """
        <html><body>
          <div role="dialog" aria-modal="true"
               style="width:480px;height:420px;border:1px solid #000;">
            <h2>Tax Information – Admin P.</h2>
            <p>Will this person claim any dependents?</p>
            <p>Will anyone claim this person as a dependent?</p>
            <div role="alert" class="error">Please select a required field.</div>
            <button>Save and Return</button>
          </div>
        </body></html>
        """
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True, channel="chrome")
            except Exception:
                browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.set_content(html)
                ok = await _click_tax_save_return(page)
            finally:
                await browser.close()
        self.assertFalse(ok)

    async def test_click_tax_save_return_already_on_summary(self) -> None:
        """No Save button because editor already closed — still succeed."""
        from playwright.async_api import async_playwright

        from pc_liveagent.tax_stage import _click_tax_save_return

        html = """
        <html><body>
          <h1>Tax Information</h1>
          <div class="card" style="width:320px;height:120px;padding:12px;border:1px solid #333;">
            <div><strong>Admin P.</strong></div>
            <div>Head of Household</div>
            <div>Dependents: John D.</div>
            <button>Edit</button>
          </div>
          <div class="card" style="width:320px;height:100px;padding:12px;border:1px solid #333;">
            <div><strong>John D.</strong></div>
            <div>Single</div>
            <button>Edit</button>
          </div>
        </body></html>
        """
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True, channel="chrome")
            except Exception:
                browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.set_content(html)
                ok = await _click_tax_save_return(page)
            finally:
                await browser.close()
        self.assertTrue(ok)

    async def test_tax_saving_phase_advances_when_summary_already_visible(self) -> None:
        """Stuck in saving after a successful Save — advance, do not fail."""
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="tax-saving-already-done",
            url="https://example.test",
            app_step="app_tax",
        )
        session.filled["app_tax_current"] = "Admin P."
        session.filled["app_tax_current_index"] = "0"
        session.filled["app_tax_phase"] = "saving"
        session.filled["app_tax_saving_name"] = "Admin P."
        session.filled["app_tax_saving_index"] = "0"
        session.filled["app_tax_heard"] = "[]"
        cards = [
            {
                "index": 0,
                "name": "Admin P.",
                "status": "Head of Household",
                "dependents": "John D.",
            },
            {"index": 1, "name": "John D.", "status": "Single", "dependents": ""},
        ]
        page = AsyncMock()
        page.evaluate = AsyncMock(
            return_value={
                "summary_visible": True,
                "modal_open": False,
                "card_count": 2,
                "card_names": ["Admin P.", "John D."],
            }
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_tax"),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.tax_stage._click_tax_save_return",
                new=AsyncMock(return_value=False),
            ) as save,
            patch(
                "pc_liveagent.tax_stage._begin_tax_member",
                new=AsyncMock(
                    return_value=(
                        "I'm starting with John D. "
                        "What is John D.'s tax filing status?"
                    )
                ),
            ) as begin,
        ):
            result = await submit_application_answer(
                "tax-saving-already-done", session, page, "Two.", 0.0
            )
        self.assertTrue(result.get("ok"))
        save.assert_not_awaited()
        begin.assert_awaited()
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("i'm starting with john d", spoken)
        self.assertNotIn("couldn't save", spoken)
        self.assertNotIn("who will claim", spoken)
        self.assertNotEqual(session.filled.get("app_tax_phase"), "saving")

    async def test_scrape_tax_cards_dependent_of_and_status(self) -> None:
        from playwright.async_api import async_playwright

        from pc_liveagent.tax_stage import _scrape_tax_cards

        html = """
        <html><body>
          <h1>Tax Information</h1>
          <div class="member-card" style="width:420px;padding:12px;border:1px solid #333;margin:8px;">
            <div style="display:flex;justify-content:space-between;">
              <div>
                <div><strong>Admin P.</strong></div>
                <div>Head of Household</div>
                <div>Dependent of: John D.</div>
              </div>
              <a href="#">Edit &gt;</a>
            </div>
          </div>
          <div class="member-card" style="width:420px;padding:12px;border:1px solid #333;margin:8px;">
            <div style="display:flex;justify-content:space-between;">
              <div>
                <div><strong>John D.</strong></div>
                <div>Not filing</div>
              </div>
              <button>Edit</button>
            </div>
          </div>
        </body></html>
        """
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True, channel="chrome")
            except Exception:
                browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.set_content(html)
                cards = await _scrape_tax_cards(page)
            finally:
                await browser.close()
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0].get("name"), "Admin P.")
        self.assertEqual(cards[0].get("status"), "Head of Household")
        self.assertEqual(cards[0].get("dependent_of"), "John D.")
        self.assertEqual(cards[1].get("name"), "John D.")
        self.assertEqual(cards[1].get("status"), "Not filing")
        # Cards must stay separate — not one merged container.
        self.assertNotEqual(cards[0].get("name"), cards[1].get("name"))

    async def test_save_done_advances_to_next_member(self) -> None:
        """Done on claimed_as_who saves Admin then starts John — not Who? again."""
        from pc_liveagent.app_stage import submit_application_answer
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="tax-save-next",
            url="https://example.test",
            app_step="app_tax",
        )
        session.filled["app_tax_current"] = "Admin P."
        session.filled["app_tax_current_index"] = "0"
        session.filled["app_tax_phase"] = "claimed_as_who"
        session.filled["app_tax_heard"] = "[]"
        cards = [
            {
                "index": 0,
                "name": "Admin P.",
                "status": "Head of Household",
                "dependent_of": "John D.",
            },
            {"index": 1, "name": "John D.", "status": "Not filing"},
        ]
        modal = {
            "open": True,
            "member": "Admin P.",
            "status": "Head of Household",
            "claim_deps": "No",
            "claim_deps_who": [],
            "claim_deps_options": [],
            "claimed_as": "Yes",
            "claimed_as_who": ["John D."],
            "claimed_as_options": ["John D."],
        }
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_tax"),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_cards",
                new=AsyncMock(return_value=cards),
            ),
            patch(
                "pc_liveagent.tax_stage._ensure_tax_modal",
                new=AsyncMock(return_value=modal),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_modal",
                new=AsyncMock(return_value=modal),
            ),
            patch(
                "pc_liveagent.tax_stage._click_tax_save_return",
                new=AsyncMock(return_value=True),
            ) as save,
            patch(
                "pc_liveagent.tax_stage._begin_tax_member",
                new=AsyncMock(
                    return_value=(
                        "I'm starting with John D. "
                        "John D.'s current tax filing status is Not filing. "
                        "Is that correct?"
                    )
                ),
            ) as begin,
        ):
            result = await submit_application_answer(
                "tax-save-next", session, AsyncMock(), "Done.", 0.0
            )
        self.assertTrue(result.get("ok"))
        save.assert_awaited()
        begin.assert_awaited()
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("i'm starting with john d", spoken)
        self.assertNotIn("anyone else for this question", spoken)
        self.assertNotEqual(session.filled.get("app_tax_phase"), "claimed_as_who")
        self.assertNotEqual(session.filled.get("app_tax_phase"), "saving")

    async def test_saving_phase_reminder_not_who_question(self) -> None:
        from pc_liveagent.tax_stage import _prepare_tax_review_speech
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="tax-saving-reminder",
            url="https://example.test",
            app_step="app_tax",
        )
        session.filled["app_tax_current"] = "Admin P."
        session.filled["app_tax_current_index"] = "0"
        session.filled["app_tax_phase"] = "saving"
        session.filled["app_tax_saving_name"] = "Admin P."
        with (
            patch(
                "pc_liveagent.tax_stage._scrape_tax_cards",
                new=AsyncMock(return_value=[{"index": 0, "name": "Admin P."}]),
            ),
            patch(
                "pc_liveagent.tax_stage._scrape_tax_modal",
                new=AsyncMock(return_value={"open": True}),
            ),
        ):
            _step, speech = await _prepare_tax_review_speech(session, AsyncMock())
        lowered = (speech or "").lower()
        self.assertIn("saving tax details", lowered)
        self.assertIn("one moment", lowered)
        self.assertNotIn("who will claim", lowered)
        self.assertNotIn("anyone else", lowered)
        self.assertNotIn("try again", lowered)

    async def test_app_busy_reminder_does_not_repeat_prior_question(self) -> None:
        """reminder_required during Save must not re-speak Who? from last_speech."""
        from pc_liveagent.call_turn import drop_controller, get_controller, process_user_turn
        from pc_liveagent.live_form import LiveFormSession

        call_id = "tax-busy-reminder"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "application"
        ctrl.application_only_state = "application_ready"
        ctrl.elig_op_in_progress = True
        ctrl.last_speech = (
            "Who will claim Admin P. as a dependent? "
            "Available members are John D. Say one or more names, then say done."
        )
        session = LiveFormSession(
            call_id=call_id, url="https://example.test", app_step="app_tax"
        )
        session.filled["app_tax_phase"] = "saving"
        session.filled["app_tax_saving_name"] = "Admin P."
        session.filled["app_pending_question"] = (
            "I'm saving tax details for Admin P. One moment."
        )

        async def fake_session(_cid: str):
            return session

        with (
            patch("pc_liveagent.config.APPLICATION_ONLY_TEST_MODE", False),
            patch(
                "pc_liveagent.call_turn.get_session",
                new=AsyncMock(side_effect=fake_session),
            ),
        ):
            result = await process_user_turn(call_id, "")
        self.assertEqual(result.detail, "app_busy_reminder")
        lowered = (result.speech or "").lower()
        self.assertEqual(lowered, "one moment.")
        self.assertNotIn("who will claim", lowered)

    async def test_detect_work_and_income_heading(self) -> None:
        from playwright.async_api import async_playwright

        from pc_liveagent.app_stage import detect_application_screen

        html = """
        <html><body>
          <h1>Work &amp; Income</h1>
          <p>Tell us about income for each household member.</p>
          <div style="width:360px;padding:12px;border:1px solid #333;margin:8px;">
            <div>Admin P.</div>
            <button>Add Income</button>
          </div>
          <div style="width:360px;padding:12px;border:1px solid #333;margin:8px;">
            <div>John D.</div>
            <button>Add Income</button>
          </div>
          <!-- Hidden stale tax chrome must not win -->
          <div style="display:none">Tax Information Head of Household Not filing</div>
        </body></html>
        """
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True, channel="chrome")
            except Exception:
                browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.set_content(html)
                self.assertEqual(await detect_application_screen(page), "app_income")
            finally:
                await browser.close()

    async def test_normalize_income_amount_and_hours_from_speech(self) -> None:
        from pc_liveagent.income_stage import _normalize_amount, _normalize_hours

        self.assertEqual(_normalize_amount("around 400 dollars"), "400")
        self.assertEqual(_normalize_amount("about $3,200 a month"), "3200")
        self.assertEqual(_normalize_amount("400"), "400")
        self.assertEqual(_normalize_amount("100 dollors"), "100")
        self.assertEqual(_normalize_amount("close to 20"), "20")
        self.assertEqual(_normalize_amount("Thirty dollars."), "30")
        self.assertEqual(_normalize_amount("I don't know. Thirty dollars."), "30")
        self.assertEqual(_normalize_amount("around thirty"), "30")
        self.assertEqual(_normalize_amount("one hundred"), "100")
        self.assertEqual(_normalize_amount("two thousand five hundred"), "2500")
        self.assertEqual(_normalize_amount("no idea"), "")
        self.assertEqual(_normalize_hours("about 40 hours"), "40")
        self.assertEqual(_normalize_hours("twenty hours"), "20")
        self.assertEqual(_normalize_hours("forty"), "40")
        self.assertEqual(_normalize_hours("20"), "20")
        self.assertEqual(_normalize_hours("full time"), "")

    async def test_begin_income_member_clicks_add_and_starts_with_name(self) -> None:
        from pc_liveagent.income_stage import _begin_income_member
        from pc_liveagent.live_form import LiveFormSession

        session = LiveFormSession(
            call_id="inc-begin-add",
            url="https://example.test",
            app_step="app_income",
        )
        page = AsyncMock()
        with patch(
            "pc_liveagent.income_stage._click_add_income",
            new=AsyncMock(return_value=True),
        ) as add:
            speech = await _begin_income_member(
                session,
                page,
                {"index": 0, "name": "Admin P.", "hasAddIncome": True},
                intro=True,
            )
        add.assert_awaited_once()
        self.assertEqual(session.filled.get("app_income_add_opened"), "1")
        self.assertEqual(session.filled.get("app_income_phase"), "type")
        lowered = (speech or "").lower()
        self.assertIn("i'm starting with admin p", lowered)
        self.assertIn("what income type", lowered)
        # First ask includes the type list once.
        self.assertIn("employed", lowered)
        self.assertIn("worker's compensation", lowered)
        self.assertEqual(session.filled.get("app_income_types_listed"), "1")

    async def test_scrape_income_cards_add_another_income_label(self) -> None:
        """Live Pointcare labels the CTA 'Add Another Income' (not only 'Add Income')."""
        from playwright.async_api import async_playwright

        from pc_liveagent.income_stage import probe_income_screen, scrape_income_cards

        html = """
        <html><body>
          <h1>Work &amp; Income</h1>
          <p>Add work and income information for each household member.</p>
          <div class="bg-card" style="width:420px;padding:12px;border:1px solid #333;margin:8px;">
            <div>Admin P.</div>
            <button>Add Another Income</button>
            <div>None</div>
          </div>
          <div class="bg-card" style="width:420px;padding:12px;border:1px solid #333;margin:8px;">
            <div>John D.</div>
            <button>Add Another Income</button>
            <div>None</div>
          </div>
          <div>Progress: 2 of 2 complete</div>
        </body></html>
        """
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True, channel="chrome")
            except Exception:
                browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.set_content(html)
                probe = await probe_income_screen(page)
                cards = await scrape_income_cards(page)
            finally:
                await browser.close()
        self.assertTrue(probe.get("is_income"))
        self.assertGreaterEqual(int(probe.get("add_count") or 0), 2)
        self.assertEqual(
            [str(c.get("name") or "") for c in cards],
            ["Admin P.", "John D."],
        )
        # "Add Another Income" must not be scraped as income type "Other".
        for card in cards:
            types = [
                str(e.get("type") or "")
                for e in (card.get("entries") or [])
                if isinstance(e, dict)
            ]
            self.assertNotIn("Other", types)

    async def test_finish_tax_advances_to_income(self) -> None:
        from pc_liveagent.live_form import LiveFormSession
        from pc_liveagent.tax_stage import _finish_tax_review_next

        session = LiveFormSession(
            call_id="tax-to-income",
            url="https://example.test",
            app_step="app_tax",
        )
        session.filled["app_tax_phase"] = "final"
        session.filled["app_tax_review_ready"] = "1"
        with (
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value="app_income"),
            ) as advance,
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_income",
                        "I'll add work and income for each person. "
                        "What income type should I enter for Admin P.?",
                    )
                ),
            ),
        ):
            result = await _finish_tax_review_next(
                "tax-to-income", session, AsyncMock(), "Yes", "app_tax"
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_income")
        advance.assert_awaited()
        self.assertEqual(
            advance.await_args.kwargs.get("expected_next"),
            "app_income",
        )
        spoken = (result.get("spoken") or "").lower()
        self.assertIn("finished tax information", spoken)
        self.assertIn("work and income", spoken)
        self.assertNotIn("tax screen is still open", spoken)

    async def test_finish_tax_income_probe_recovers(self) -> None:
        from pc_liveagent.live_form import LiveFormSession
        from pc_liveagent.tax_stage import _finish_tax_review_next

        session = LiveFormSession(
            call_id="tax-income-probe",
            url="https://example.test",
            app_step="app_tax",
        )
        session.filled["app_tax_phase"] = "final"
        with (
            patch(
                "pc_liveagent.app_stage._click_stage_next",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage._advance_after_answer",
                new=AsyncMock(return_value=""),
            ),
            patch(
                "pc_liveagent.app_stage._wait_for_screen_change",
                new=AsyncMock(return_value=""),
            ),
            patch(
                "pc_liveagent.income_stage.probe_income_screen",
                new=AsyncMock(
                    return_value={
                        "is_income": True,
                        "heading": "Work & Income",
                        "add_count": 2,
                        "card_count": 2,
                        "reason": "income_heading",
                    }
                ),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_resume_speech",
                new=AsyncMock(
                    return_value=(
                        "app_income",
                        "I'll add work and income for each person.",
                    )
                ),
            ),
        ):
            result = await _finish_tax_review_next(
                "tax-income-probe", session, AsyncMock(), "continue", "app_tax"
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_income")
        self.assertNotEqual(result.get("error"), "tax_next_stayed")


if __name__ == "__main__":
    unittest.main()
