"""Dedicated application-only test mode. Mocks only — no live Pointcare."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pc_liveagent.live_form import LiveFormSession


class ApplicationOnlyFlagTests(unittest.TestCase):
    def test_flag_defaults_off_and_is_independent(self) -> None:
        from pc_liveagent.config import (
            application_after_signin_from_env,
            application_only_test_mode_from_env,
            application_start_at_from_env,
            application_test_mode_from_env,
        )

        self.assertFalse(application_only_test_mode_from_env({}))
        self.assertFalse(
            application_only_test_mode_from_env(
                {"POINTCARE_APPLICATION_TEST_MODE": "true"}
            )
        )
        self.assertFalse(
            application_only_test_mode_from_env(
                {"POINTCARE_APPLICATION_AFTER_SIGNIN": "true"}
            )
        )
        self.assertTrue(
            application_only_test_mode_from_env(
                {"POINTCARE_APPLICATION_ONLY_TEST_MODE": "true"}
            )
        )
        self.assertFalse(
            application_test_mode_from_env(
                {"POINTCARE_APPLICATION_ONLY_TEST_MODE": "true"}
            )
        )
        self.assertFalse(
            application_after_signin_from_env(
                {"POINTCARE_APPLICATION_ONLY_TEST_MODE": "true"}
            )
        )
        self.assertFalse(
            application_only_test_mode_from_env(
                {
                    "POINTCARE_APPLICATION_ONLY_TEST_MODE": "true",
                    "APP_ENV": "production",
                }
            )
        )
        self.assertEqual(application_start_at_from_env({}), "")
        self.assertEqual(
            application_start_at_from_env(
                {"POINTCARE_APPLICATION_START_AT": "app_income"}
            ),
            "app_income",
        )
        self.assertEqual(
            application_start_at_from_env({"POINTCARE_APPLICATION_START_AT": "q9"}),
            "app_income",
        )
        self.assertEqual(
            application_start_at_from_env({"POINTCARE_APPLICATION_START_AT": "q4"}),
            "app_address_review",
        )
        self.assertEqual(
            application_start_at_from_env(
                {"POINTCARE_APPLICATION_START_AT": "app_address_review"}
            ),
            "app_address_review",
        )
        self.assertEqual(
            application_start_at_from_env({"POINTCARE_APPLICATION_START_AT": "q1"}),
            "",
        )

    def test_local_env_enables_the_flag_without_changing_the_default(self) -> None:
        from pc_liveagent.config import application_only_test_mode_from_env

        text = Path(__file__).resolve().parents[1].joinpath(".env").read_text(
            encoding="utf-8"
        )
        self.assertRegex(
            text, r"(?m)^POINTCARE_APPLICATION_ONLY_TEST_MODE=true\s*$"
        )
        self.assertRegex(
            text,
            r"(?m)^POINTCARE_APPLICATION_START_AT=app_address_review\s*$",
        )
        self.assertFalse(application_only_test_mode_from_env({}))


class ApplicationOnlyStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Default Q1 start — individual tests opt into START_AT=app_income.
        self._start_at_patch = patch("pc_liveagent.config.APPLICATION_START_AT", "")
        self._start_at_patch.start()
        self.addCleanup(self._start_at_patch.stop)

    def _session(self, call_id: str = "only-1") -> LiveFormSession:
        session = LiveFormSession(
            call_id=call_id, url="https://my.pointcare.com/home", app_step="app_dob_review"
        )
        session.elig_step = "marital"
        session.filled["app_citizenship_no"] = "1"
        session.filled["app_immigration_confirmed"] = '{"Agent T.": "U.S. Citizen"}'
        session.page = AsyncMock()
        return session

    async def test_opening_skips_welcome_and_schedules_setup(self) -> None:
        from pc_liveagent.call_turn import begin_speech, drop_controller, get_controller

        call_id = "only-open"
        await drop_controller(call_id)
        with (
            patch("pc_liveagent.config.APPLICATION_ONLY_TEST_MODE", True),
            patch("pc_liveagent.config.APPLICATION_TEST_MODE", False),
            patch("pc_liveagent.call_turn._schedule_application_only") as schedule,
        ):
            speech = await begin_speech(call_id)
        ctrl = await get_controller(call_id)
        self.assertEqual(speech, "")
        self.assertNotIn("let's start", speech.lower())
        self.assertEqual(ctrl.last_speech, "")
        self.assertEqual(ctrl.application_only_state, "application_test_start")
        self.assertNotEqual(ctrl.phase, "language")
        schedule.assert_called_once_with(call_id)
        await drop_controller(call_id)

    async def test_normal_mode_still_opens_with_the_greeting(self) -> None:
        from pc_liveagent.call_turn import LIVE_FILL_GREETING, begin_speech, drop_controller

        call_id = "full-open"
        await drop_controller(call_id)
        with (
            patch("pc_liveagent.config.APPLICATION_ONLY_TEST_MODE", False),
            patch("pc_liveagent.config.APPLICATION_TEST_MODE", False),
            patch("pc_liveagent.call_turn._schedule_application_only") as schedule,
        ):
            speech = await begin_speech(call_id)
        self.assertEqual(speech, LIVE_FILL_GREETING)
        schedule.assert_not_called()
        await drop_controller(call_id)

    async def test_answers_wait_until_application_ready(self) -> None:
        from pc_liveagent.call_turn import drop_controller, get_controller, process_user_turn

        call_id = "only-wait"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "authenticating"
        ctrl.application_only_state = "authenticating"
        ctrl.last_speech = ""
        with (
            patch("pc_liveagent.config.APPLICATION_ONLY_TEST_MODE", True),
            patch(
                "pc_liveagent.call_turn.submit_field",
                new=AsyncMock(side_effect=AssertionError("must not answer during login")),
            ),
        ):
            result = await process_user_turn(call_id, "Yes, everyone is a citizen")
        self.assertEqual(result.detail, "application_only_not_ready")
        self.assertEqual(result.speech, "")
        self.assertEqual(ctrl.phase, "authenticating")
        await drop_controller(call_id)

    async def test_answers_run_only_after_application_ready(self) -> None:
        from pc_liveagent.call_turn import drop_controller, get_controller, process_user_turn

        call_id = "only-ready"
        await drop_controller(call_id)
        ctrl = await get_controller(call_id)
        ctrl.phase = "application"
        ctrl.application_only_state = "application_ready"
        session = LiveFormSession(
            call_id=call_id,
            url="https://example.test",
            app_step="app_citizenship",
        )
        submitted: list[str] = []

        async def fake_session(_cid: str):
            return session

        async def fake_submit(_cid: str, _field: str, text: str, **_kwargs):
            submitted.append(text)
            return {
                "ok": True,
                "spoken": "Are they all U.S. Citizens?",
                "step": "app_citizenship",
            }

        with (
            patch("pc_liveagent.config.APPLICATION_ONLY_TEST_MODE", True),
            patch(
                "pc_liveagent.call_turn.get_session",
                new=AsyncMock(side_effect=fake_session),
            ),
            patch("pc_liveagent.call_turn.submit_field", new=AsyncMock(side_effect=fake_submit)),
        ):
            result = await process_user_turn(call_id, "No")
            task_map = __import__(
                "pc_liveagent.call_turn", fromlist=["_app_browser_tasks"]
            )._app_browser_tasks
            task = task_map.get(call_id)
            if task is not None:
                await task
        self.assertNotEqual(result.detail, "application_only_not_ready")
        self.assertEqual(submitted, ["No"])
        await drop_controller(call_id)

    async def test_startup_task_does_not_overlap(self) -> None:
        from pc_liveagent.call_turn import (
            _application_only_tasks,
            _schedule_application_only,
        )

        call_id = "only-race"
        started = 0
        release = asyncio.Event()

        async def hang(_cid: str, _url: str = ""):
            nonlocal started
            started += 1
            await release.wait()
            return LiveFormSession(call_id=_cid, url="https://example.test")

        with patch("pc_liveagent.live_form.start_live_form", new=AsyncMock(side_effect=hang)):
            _schedule_application_only(call_id)
            _schedule_application_only(call_id)
            await asyncio.sleep(0)
            self.assertEqual(started, 1)
        task = _application_only_tasks.get(call_id)
        if task is not None:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        _application_only_tasks.pop(call_id, None)

    async def test_reuses_authenticated_session_and_resets_to_question_1(self) -> None:
        from pc_liveagent.app_stage import run_application_only_browser

        session = self._session()
        states: list[str] = []
        sign_in = AsyncMock(side_effect=AssertionError("must not sign in again"))
        with (
            patch(
                "pc_liveagent.live_form._application_test_authenticated",
                new=AsyncMock(return_value=True),
            ),
            patch("pc_liveagent.live_form._sign_in_with_provider", sign_in),
            patch(
                "pc_liveagent.app_stage.classify_saved_household_page",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(side_effect=["app_dob_review", "app_citizenship"]),
            ),
            patch(
                "pc_liveagent.app_stage.restart_application_to_first_question",
                new=AsyncMock(return_value="app_citizenship"),
            ) as restart,
            patch(
                "pc_liveagent.app_stage.click_quick_apply",
                new=AsyncMock(side_effect=AssertionError("draft is already open")),
            ),
        ):
            result = await run_application_only_browser(
                session, session.page, states.append
            )
        sign_in.assert_not_awaited()
        restart.assert_awaited_once()
        self.assertEqual(
            states,
            [
                "application_test_start",
                "authenticating",
                "opening_application",
                "resetting_to_question_1",
                "application_ready",
            ],
        )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_citizenship")
        self.assertNotIn("let's start", str(result.get("spoken") or "").lower())
        self.assertEqual(session.app_step, "app_citizenship")
        self.assertEqual(session.elig_step, "")
        self.assertIsNone(session.filled.get("app_citizenship_no"))
        self.assertIsNone(session.filled.get("app_immigration_confirmed"))

    async def test_start_at_income_skips_question_1_reset(self) -> None:
        from pc_liveagent.app_stage import run_application_only_browser

        session = self._session("only-q9")
        session.page.url = "https://my.pointcare.com/application/tax-information"
        states: list[str] = []
        with (
            patch("pc_liveagent.config.APPLICATION_START_AT", "app_income"),
            patch(
                "pc_liveagent.live_form._application_test_authenticated",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage.classify_saved_household_page",
                new=AsyncMock(return_value="app_tax"),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(
                    # 1) surface check, 2) already-on-income check, 3) post-goto verify
                    side_effect=["app_tax", "app_tax", "app_income"]
                ),
            ),
            patch(
                "pc_liveagent.app_stage.restart_application_to_first_question",
                new=AsyncMock(
                    side_effect=AssertionError("must not reset to Q1 when skipping")
                ),
            ) as restart,
            patch(
                "pc_liveagent.app_stage.click_quick_apply",
                new=AsyncMock(side_effect=AssertionError("draft is already open")),
            ),
            patch(
                "pc_liveagent.income_stage.probe_income_screen",
                new=AsyncMock(
                    side_effect=[
                        {"is_income": False},
                        {"is_income": True, "heading": "Work & Income"},
                    ]
                ),
            ),
            patch(
                "pc_liveagent.income_stage.prepare_income_speech",
                new=AsyncMock(
                    return_value=(
                        "app_income",
                        "I'll add work and income for each person. "
                        "What income type should I enter for Admin P.?",
                    )
                ),
            ) as prepare,
        ):
            session.page.goto = AsyncMock()
            session.page.wait_for_timeout = AsyncMock()
            result = await run_application_only_browser(
                session, session.page, states.append
            )
        restart.assert_not_awaited()
        prepare.assert_awaited_once()
        session.page.goto.assert_awaited()
        self.assertIn("skipping_to_income", states)
        self.assertIn("application_ready", states)
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_income")
        self.assertEqual(session.app_step, "app_income")
        self.assertIn("work and income", str(result.get("spoken") or "").lower())
        self.assertEqual(session.filled.get("application_start_at"), "app_income")

    async def test_start_at_address_skips_question_1_reset(self) -> None:
        from pc_liveagent.app_stage import run_application_only_browser

        session = self._session("only-q4")
        session.page.url = "https://my.pointcare.com/application/date-of-birth"
        states: list[str] = []
        with (
            patch("pc_liveagent.config.APPLICATION_START_AT", "app_address_review"),
            patch(
                "pc_liveagent.live_form._application_test_authenticated",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage.classify_saved_household_page",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(
                    # 1) surface check, 2) already-on-address check, 3) post-goto verify
                    side_effect=[
                        "app_dob_review",
                        "app_dob_review",
                        "app_address_review",
                    ]
                ),
            ),
            patch(
                "pc_liveagent.app_stage.restart_application_to_first_question",
                new=AsyncMock(
                    side_effect=AssertionError("must not reset to Q1 when skipping")
                ),
            ) as restart,
            patch(
                "pc_liveagent.app_stage.click_quick_apply",
                new=AsyncMock(side_effect=AssertionError("draft is already open")),
            ),
            patch(
                "pc_liveagent.app_stage._prepare_address_review_speech",
                new=AsyncMock(
                    return_value=(
                        "app_address_review",
                        "For Admin P., home address is 1 Main St. "
                        "Mailing is the same. Is that correct?",
                    )
                ),
            ) as prepare,
        ):
            session.page.goto = AsyncMock()
            session.page.wait_for_timeout = AsyncMock()
            result = await run_application_only_browser(
                session, session.page, states.append
            )
        restart.assert_not_awaited()
        prepare.assert_awaited_once()
        session.page.goto.assert_awaited()
        self.assertIn("skipping_to_address", states)
        self.assertIn("application_ready", states)
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_address_review")
        self.assertEqual(session.app_step, "app_address_review")
        self.assertIn("home address", str(result.get("spoken") or "").lower())
        self.assertEqual(
            session.filled.get("application_start_at"), "app_address_review"
        )

    async def test_household_dashboard_is_not_treated_as_an_application_step(self) -> None:
        from pc_liveagent.app_stage import (
            classify_saved_household_page,
            detect_application_screen,
        )

        class _Body:
            async def inner_text(self) -> str:
                return (
                    "Next steps "
                    "Continue the application process and get covered in 15min or less "
                    "Continue Quick Apply "
                    "New Quick Apply "
                    "Household Recheck Eligibility Add Member "
                    "Agent T. Age 25 Coverage unknown "
                    "john D. Adult Coverage unknown "
                    "Don D. Youth Coverage unknown"
                )

        page = AsyncMock()
        page.url = "https://my.pointcare.com/"
        page.locator = lambda _selector: _Body()
        self.assertEqual(await detect_application_screen(page), "")
        self.assertEqual(await classify_saved_household_page(page), "dashboard")

    async def test_late_dashboard_clicks_continue_quick_apply_before_question_1(self) -> None:
        from pc_liveagent.app_stage import run_application_only_browser

        session = self._session("only-dashboard")
        states: list[str] = []
        click = AsyncMock(return_value="Continue Quick Apply")
        with (
            patch(
                "pc_liveagent.live_form._application_test_authenticated",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage.classify_saved_household_page",
                new=AsyncMock(side_effect=["unknown", "unknown", "dashboard"]),
            ),
            patch("pc_liveagent.app_stage.click_quick_apply", click),
            patch(
                "pc_liveagent.app_stage._wait_surface_after_quick_apply",
                new=AsyncMock(return_value="programs"),
            ),
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
                new=AsyncMock(side_effect=["app_dob_review", "app_citizenship"]),
            ),
            patch(
                "pc_liveagent.app_stage.restart_application_to_first_question",
                new=AsyncMock(return_value="app_citizenship"),
            ),
        ):
            result = await run_application_only_browser(
                session, session.page, states.append
            )
        click.assert_awaited()
        start.assert_awaited_once()
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_citizenship")
        self.assertNotIn("not open", str(result.get("spoken") or "").lower())
        self.assertEqual(states[-1], "application_ready")

    async def test_second_call_starts_at_question_1_again(self) -> None:
        from pc_liveagent.app_stage import run_application_only_browser

        session = self._session("only-retry")
        session.app_step = "app_address_review"
        with (
            patch(
                "pc_liveagent.live_form._application_test_authenticated",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage.classify_saved_household_page",
                new=AsyncMock(return_value="app_address_review"),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(side_effect=["app_address_review", "app_agent_offer"]),
            ),
            patch(
                "pc_liveagent.app_stage.restart_application_to_first_question",
                new=AsyncMock(return_value="app_agent_offer"),
            ) as restart,
        ):
            result = await run_application_only_browser(session, session.page, lambda _s: None)
        restart.assert_awaited_once()
        self.assertEqual(result.get("step"), "app_agent_offer")
        self.assertEqual(session.app_step, "app_agent_offer")

    async def test_wait_for_started_ignores_application_url_while_great_news(
        self,
    ) -> None:
        """Great News often uses /application URLs — that must not count as started."""
        from pc_liveagent.app_stage import wait_for_application_started

        page = AsyncMock()
        page.url = "https://my.pointcare.com/application/programs"
        page.wait_for_timeout = AsyncMock()
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="programs"),
            ),
            patch(
                "pc_liveagent.app_stage._page_body_lower",
                new=AsyncMock(
                    return_value=(
                        "great news! we found 1 eligible programs. "
                        "start applications"
                    )
                ),
            ),
        ):
            started = await wait_for_application_started(page, timeout_ms=800)
        self.assertFalse(started)

    async def test_great_news_clicks_start_applications_then_question_1(
        self,
    ) -> None:
        """Quick Apply → Great News must click Start Applications before Q1."""
        from pc_liveagent.app_stage import run_application_only_browser

        session = self._session("only-great-news")
        states: list[str] = []
        start = AsyncMock(return_value="Start Applications")
        restart = AsyncMock(return_value="app_citizenship")
        with (
            patch(
                "pc_liveagent.live_form._application_test_authenticated",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage.classify_saved_household_page",
                new=AsyncMock(return_value="dashboard"),
            ),
            patch(
                "pc_liveagent.app_stage.click_quick_apply",
                new=AsyncMock(return_value="Quick Apply"),
            ),
            patch(
                "pc_liveagent.app_stage._wait_surface_after_quick_apply",
                new=AsyncMock(return_value="programs"),
            ),
            patch(
                "pc_liveagent.app_stage.click_start_applications",
                start,
            ),
            patch(
                "pc_liveagent.app_stage.wait_for_application_started",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(
                    side_effect=["app_citizenship", "app_citizenship"]
                ),
            ),
            patch(
                "pc_liveagent.app_stage.restart_application_to_first_question",
                restart,
            ),
        ):
            result = await run_application_only_browser(
                session, session.page, states.append
            )
        start.assert_awaited_once()
        restart.assert_awaited_once()
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_citizenship")
        self.assertEqual(states[-1], "application_ready")
        self.assertNotIn("not open", str(result.get("spoken") or "").lower())

    async def test_already_on_question_1_becomes_ready_without_back(
        self,
    ) -> None:
        from pc_liveagent.app_stage import run_application_only_browser

        session = self._session("only-q1")
        states: list[str] = []
        restart = AsyncMock(return_value="app_citizenship")
        with (
            patch(
                "pc_liveagent.live_form._application_test_authenticated",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage.classify_saved_household_page",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_citizenship"),
            ),
            patch(
                "pc_liveagent.app_stage._page_body_lower",
                new=AsyncMock(
                    return_value="are all household members u.s. citizens?"
                ),
            ),
            patch(
                "pc_liveagent.app_stage.restart_application_to_first_question",
                restart,
            ),
            patch(
                "pc_liveagent.app_stage.click_start_applications",
                new=AsyncMock(side_effect=AssertionError("already on Q1")),
            ),
            patch(
                "pc_liveagent.app_stage.click_quick_apply",
                new=AsyncMock(side_effect=AssertionError("already on Q1")),
            ),
        ):
            result = await run_application_only_browser(
                session, session.page, states.append
            )
        restart.assert_awaited_once()
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_citizenship")
        self.assertIn("resetting_to_question_1", states)
        self.assertEqual(states[-1], "application_ready")
        self.assertIn("citizen", str(result.get("spoken") or "").lower())

    async def test_start_applications_waits_through_delayed_transition(
        self,
    ) -> None:
        from pc_liveagent.app_stage import wait_for_application_started

        page = AsyncMock()
        page.url = "https://my.pointcare.com/application/programs"
        page.wait_for_timeout = AsyncMock()
        detect = AsyncMock(
            side_effect=["programs", "programs", "app_citizenship"]
        )
        with (
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                detect,
            ),
            patch(
                "pc_liveagent.app_stage._page_body_lower",
                new=AsyncMock(
                    side_effect=[
                        "great news! start applications may be eligible",
                        "great news! start applications may be eligible",
                        "are all household members u.s. citizens?",
                    ]
                ),
            ),
        ):
            started = await wait_for_application_started(page, timeout_ms=5000)
        self.assertTrue(started)
        self.assertGreaterEqual(detect.await_count, 3)

    async def test_unverified_question_1_stays_unready(
        self,
    ) -> None:
        from pc_liveagent.app_stage import run_application_only_browser

        session = self._session("only-unverified")
        with (
            patch(
                "pc_liveagent.live_form._application_test_authenticated",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage.classify_saved_household_page",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage.restart_application_to_first_question",
                new=AsyncMock(return_value="app_dob_review"),
            ),
            patch(
                "pc_liveagent.app_stage._verified_application_question_one",
                new=AsyncMock(return_value=""),
            ),
        ):
            result = await run_application_only_browser(
                session, session.page, lambda _s: None
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("state"), "resetting_to_question_1")
        self.assertIn("question 1", str(result.get("spoken") or "").lower())
        self.assertEqual(session.app_step, "")
        self.assertEqual(session.filled.get("application_started"), "0")

    async def test_quick_apply_mid_flow_resets_to_question_1_not_stuck_on_q11(
        self,
    ) -> None:
        """Continue Quick Apply may reopen question 11; always Back to Q1."""
        from pc_liveagent.app_stage import (
            _wait_surface_after_quick_apply,
            run_application_only_browser,
        )

        session = self._session("only-q11")
        states: list[str] = []
        page = session.page
        page.wait_for_timeout = AsyncMock()
        # Transitional unknown must not abort before the mid-flow app screen.
        classify = AsyncMock(
            side_effect=["unknown", "unknown", "app_circumstances"]
        )
        with patch(
            "pc_liveagent.app_stage.classify_saved_household_page", classify
        ):
            surface = await _wait_surface_after_quick_apply(page, timeout_s=5.0)
        self.assertEqual(surface, "app_circumstances")
        self.assertGreaterEqual(classify.await_count, 3)

        restart = AsyncMock(return_value="app_citizenship")
        with (
            patch(
                "pc_liveagent.live_form._application_test_authenticated",
                new=AsyncMock(return_value=True),
            ),
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
                new=AsyncMock(return_value="app_circumstances"),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(
                    side_effect=["app_circumstances", "app_citizenship"]
                ),
            ),
            patch(
                "pc_liveagent.app_stage.restart_application_to_first_question",
                restart,
            ),
            patch(
                "pc_liveagent.app_stage.click_start_applications",
                new=AsyncMock(side_effect=AssertionError("must not Start Applications")),
            ),
        ):
            result = await run_application_only_browser(
                session, page, states.append
            )
        restart.assert_awaited_once()
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("step"), "app_citizenship")
        self.assertIn("resetting_to_question_1", states)
        self.assertEqual(states[-1], "application_ready")
        self.assertNotIn("not open", str(result.get("spoken") or "").lower())

    async def test_unauthenticated_waits_for_google_without_starting_questions(self) -> None:
        from pc_liveagent.app_stage import run_application_only_browser

        session = self._session("only-auth")
        states: list[str] = []
        sign_in = AsyncMock(return_value="AUTHENTICATED")
        authed = AsyncMock(side_effect=[False, True])
        with (
            patch("pc_liveagent.live_form._application_test_authenticated", authed),
            patch("pc_liveagent.live_form._sign_in_with_provider", sign_in),
            patch(
                "pc_liveagent.app_stage.classify_saved_household_page",
                new=AsyncMock(return_value="programs"),
            ),
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
            patch(
                "pc_liveagent.app_stage.restart_application_to_first_question",
                new=AsyncMock(return_value="app_citizenship"),
            ),
        ):
            result = await run_application_only_browser(
                session, session.page, states.append
            )
        self.assertEqual(sign_in.await_args.kwargs["destination"], "stay")
        self.assertEqual(sign_in.await_args.args[1], "Continue with Google")
        start.assert_awaited_once()
        self.assertTrue(result.get("ok"))
        self.assertIn("authenticating", states)
        self.assertEqual(states[-1], "application_ready")
        self.assertEqual(session.app_step, "app_citizenship")

    async def test_login_failure_does_not_open_question_1(self) -> None:
        from pc_liveagent.app_stage import run_application_only_browser

        session = self._session("only-mfa")
        with (
            patch(
                "pc_liveagent.live_form._application_test_authenticated",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pc_liveagent.live_form._sign_in_with_provider",
                new=AsyncMock(side_effect=RuntimeError("Google sign-in has not finished")),
            ),
            patch(
                "pc_liveagent.app_stage.click_start_applications",
                new=AsyncMock(side_effect=AssertionError("must not open the application")),
            ),
        ):
            result = await run_application_only_browser(
                session, session.page, lambda _s: None
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("state"), "authenticating")
        self.assertIn("signed in", str(result.get("spoken") or "").lower())
        self.assertEqual(session.app_step, "")

    async def test_unsafe_reset_reports_a_blocker(self) -> None:
        from pc_liveagent.app_stage import run_application_only_browser

        session = self._session("only-block")
        with (
            patch(
                "pc_liveagent.live_form._application_test_authenticated",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pc_liveagent.app_stage.classify_saved_household_page",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage.detect_application_screen",
                new=AsyncMock(return_value="app_insurance"),
            ),
            patch(
                "pc_liveagent.app_stage.restart_application_to_first_question",
                new=AsyncMock(
                    side_effect=RuntimeError(
                        "APPLICATION_TEST: Back control is missing"
                    )
                ),
            ),
        ):
            result = await run_application_only_browser(
                session, session.page, lambda _s: None
            )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("state"), "resetting_to_question_1")
        self.assertIn("question 1", str(result.get("spoken") or "").lower())
        self.assertEqual(session.app_step, "")

    async def test_saved_session_does_not_start_stage_d(self) -> None:
        from pc_liveagent.live_form import promote_signed_in_to_eligibility

        with (
            patch("pc_liveagent.config.APPLICATION_ONLY_TEST_MODE", True),
            patch(
                "pc_liveagent.live_form._begin_eligibility",
                new=AsyncMock(side_effect=AssertionError("must not start Stage D")),
            ),
        ):
            speech = await promote_signed_in_to_eligibility("saved-session")
        self.assertIsNone(speech)

    async def test_provider_stay_does_not_start_stage_d(self) -> None:
        from pc_liveagent.live_form import _land_after_provider_auth

        session = self._session("only-land")
        page = AsyncMock()
        with patch(
            "pc_liveagent.live_form._begin_eligibility",
            new=AsyncMock(side_effect=AssertionError("must not start Stage D")),
        ):
            landed = await _land_after_provider_auth(session, page, "stay")
        self.assertEqual(landed, "AUTHENTICATED")
        self.assertTrue(session.signed_in)


if __name__ == "__main__":
    unittest.main()
