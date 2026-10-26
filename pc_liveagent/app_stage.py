"""Stage E Application Collect Mode + program-summary permission gate.

Stage D lands on the Pointcare program-summary screen. We scrape what the page
actually shows, ask permission, then either start the application (Yes) or close
politely without clicking Start Applications (No).
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from playwright.async_api import Page

logger = logging.getLogger(__name__)

# Collect Mode questions — server owns order; live form may skip screens.
APP_QUESTIONS: dict[str, str] = {
    "app_agent_offer": (
        "Connecting with a licensed agent later is optional and not required to "
        "finish this application. Would you like that option? Please say Yes or No."
    ),
    "app_citizenship": (
        "Are the household members listed on this application all U.S. Citizens? "
        "Please say Yes or No."
    ),
    "app_immigration_who": (
        "Which household members are not U.S. citizens? "
        "Say the names, or say continue if everyone who needs a status is already set."
    ),
    "app_immigration": (
        "What is that person's citizenship or immigration status — "
        "U.S. Citizen, U.S. National, Satisfactory immigration status "
        "(green card, etc.), or Prefer not to answer?"
    ),
    "app_insurance": (
        "Do the household members listed on this application have health "
        "insurance or health coverage? Please say Yes or No."
    ),
    "app_insurance_members": (
        "Who has coverage? Say the names of the household members who have coverage."
    ),
    "app_insurance_coverage_type": (
        "What type of coverage do they have — Medicare, Medicaid, health insurance "
        "through a job, TRICARE, VA Health Care, private insurance or ACA Marketplace, "
        "or Other? You can name more than one if needed."
    ),
    "app_dob_review": (
        "Please confirm each person's date of birth looks correct. "
        "Say Yes to continue, or tell me whose date of birth to change."
    ),
    "app_dob_collect": (
        "What is that person's date of birth? Please say the month, day, and year."
    ),
    "app_address_review": (
        "Please confirm each person's home and mailing address looks correct. "
        "Say Yes to continue, or tell me who needs an address update."
    ),
    "app_marital_review": (
        "Please confirm the marital status already filled in looks correct. "
        "Say Yes to continue, or say change if something is wrong."
    ),
    "app_childcare_review": (
        "For each adult, confirm whether they care for children under 19. "
        "Say Yes if that looks correct, or tell me what to change."
    ),
    "app_circumstances": (
        "Do any of these apply to anyone on the application: needs help with daily "
        "activities, veteran or military, American Indian or Alaska Native, "
        "full-time student 18 to 20, or none of the above? "
        "You can name more than one if needed."
    ),
    "app_circumstances_person": (
        "Which household member does that circumstance apply to? Say the name."
    ),
    "app_tax": (
        "The tax filing status on this screen is already filled in. "
        "Does it look correct? Say Yes to continue, or say edit if you need to change it."
    ),
    "app_income": (
        "For work and income, we go one person at a time. What income type should I "
        "enter for the next person — Employed, Self-employed, Unemployed, SSA, SSDI, "
        "Pension, Alimony, Retirement, Rental, Worker's comp, Other, or None?"
    ),
    "app_income_employed": (
        "What is the monthly pay before taxes?"
    ),
    "app_income_employed_hours": "How many hours per week?",
    "app_income_employed_frequency": (
        "What is the pay frequency — weekly, bi-weekly, or monthly?"
    ),
    "app_income_employed_employer": "What is the employer name?",
    "app_income_self": (
        "What is the monthly income after expenses?"
    ),
    "app_income_self_work": "What type of work is it?",
    "app_income_more": (
        "Does that person have another income source to add? Please say Yes or No. "
        "If every person already has income or None, say continue."
    ),
    "app_expenses": (
        "Household expenses are optional. Would you like to add an expense such as "
        "student loans, childcare, or medical, or say skip to continue?"
    ),
    "app_expenses_amount": (
        "What amount should I enter for that expense?"
    ),
    "app_documents": (
        "Documents can be provided now or later. Say skip to continue without "
        "uploading now, or say upload if you want help with documents later."
    ),
    "app_submit_confirm": (
        "The application is ready for final review. After you submit, answers may "
        "no longer be editable. Would you like me to submit the application now? "
        "Please say Yes or No."
    ),
}

# Sequential Collect Mode sub-fields for employed / self-employed income.
_INCOME_EMPLOYED_SEQ = (
    ("pay", "app_income_employed", "What is the monthly pay before taxes?"),
    ("hours", "app_income_employed_hours", "How many hours per week?"),
    (
        "frequency",
        "app_income_employed_frequency",
        "What is the pay frequency — weekly, bi-weekly, or monthly?",
    ),
    ("employer", "app_income_employed_employer", "What is the employer name?"),
)
_INCOME_SELF_SEQ = (
    ("pay", "app_income_self", "What is the monthly income after expenses?"),
    ("work", "app_income_self_work", "What type of work is it?"),
)

YES_NO_APP_STEPS = frozenset(
    {
        "app_agent_offer",
        "app_citizenship",
        "app_insurance",
        "app_dob_review",
        "app_address_review",
        "app_marital_review",
        "app_childcare_review",
        "app_tax",
        "app_income_more",
        "app_submit_confirm",
    }
)

_STAGE_E_EXPLAIN: dict[str, str] = {
    "app_agent_offer": (
        "A licensed agent can help later if you want, but you do not need one to "
        "complete this application."
    ),
    "app_citizenship": (
        "Pointcare needs to know whether everyone listed is a U.S. citizen so it "
        "can choose the right immigration questions."
    ),
    "app_immigration_who": (
        "After you say not everyone is a U.S. citizen, Pointcare needs a "
        "citizenship or immigration status for each household member shown."
    ),
    "app_immigration": (
        "For each household member, Pointcare asks which citizenship or "
        "immigration status to record from the options on the form. "
        "Preselected radios are not treated as confirmed answers."
    ),
    "app_dob_collect": (
        "Pointcare needs a date of birth for each household member who does not "
        "already have one saved on this screen."
    ),
    "app_insurance": (
        "Pointcare asks whether anyone already has health coverage so benefits "
        "can be calculated correctly."
    ),
    "app_insurance_members": (
        "When someone has coverage, Pointcare asks which household members to "
        "mark, then what type of coverage each selected person has."
    ),
    "app_insurance_coverage_type": (
        "Each selected household member can have one or more coverage types "
        "from the options shown under their name."
    ),
    "app_income": (
        "We collect each person's income type and details one short question at "
        "a time so Pointcare can fill the work section accurately."
    ),
    "app_submit_confirm": (
        "After submit, Pointcare may lock the answers. Say Yes only when you are "
        "ready for me to submit."
    ),
}

CLOSING_NO_APPLICATION = (
    "Your household eligibility check is complete, and I have not started an "
    "application. You can log back into Pointcare later if you want to continue. "
    "Thank you for your time. Goodbye."
)

CLOSING_SUBMITTED = (
    "You're all done. Pointcare will follow up if they need anything. You can "
    "upload documents later by logging in with the same account you used today. "
    "For questions, email support@pointcare.com. Thank you for your time. Goodbye."
)

CLOSING_NOT_SUBMITTED = (
    "Okay — I have not submitted the application. Your progress is saved in "
    "Pointcare, and you can finish later by logging in. Thank you for your time. "
    "Goodbye."
)

PROCEED_QUESTION = (
    "Your household eligibility check is finished. "
    "Can I start the application now? Please say Yes to start, or No to end the call."
)

CLOSING_NO_PROCEED = (
    "Okay. I will not start the application. Your saved household information "
    "stays in Pointcare. Thank you for your time. Goodbye."
)

# Friendly caller speech when post-auth navigation fails — never speak diagnostics.
POST_AUTH_RETRY_SPEECH = (
    "You're signed in, and I can see your household dashboard. "
    "Your household eligibility check is finished. "
    "Can I start the application now? Please say Yes to start, or No to end the call."
)

GATE_STEPS = frozenset({"proceed_application", "confirm_start_application"})

PROGRAMS_CLARIFY = (
    "Please say Yes if you want me to start the application, or No if you do not "
    "want to start it now."
)


def build_programs_permission_speech(summary: dict[str, Any]) -> str:
    """Spoken permission ask from scraped program-summary data — never invent programs.

    Speak only programs that may be eligible. Do not list Medicare/Marketplace
    (or any other card) marked \"Not eligible at this time\".
    """
    eligible = [
        p
        for p in (summary.get("programs") or [])
        if isinstance(p, dict) and p.get("may_be_eligible")
    ]
    parts: list[str] = ["Your household eligibility check is complete."]
    if eligible:
        names = [_short_program_name(str(p.get("name") or "")) for p in eligible]
        names = [n for n in names if n]
        if len(names) == 1:
            parts.append(
                f"Pointcare indicates that your household may be eligible for {names[0]}."
            )
        elif names:
            joined = ", ".join(names[:-1]) + f", and {names[-1]}"
            parts.append(
                f"Pointcare indicates that your household may be eligible for {joined}."
            )
        details = []
        for p in eligible:
            detail = (p.get("detail") or "").strip()
            # Skip page chrome like "Not eligible at this time" if it leaked in.
            if re.search(r"not eligible", detail, re.I):
                continue
            if detail and len(detail) < 160:
                details.append(detail)
        if details and len(details) == 1:
            # Soften approval language if the page used strong wording.
            soft = re.sub(
                r"\b(are eligible|is eligible|approved)\b",
                "may be eligible",
                details[0],
                flags=re.I,
            )
            if soft.lower() not in " ".join(parts).lower():
                parts.append(soft if soft.endswith(".") else soft + ".")
        parts.append(
            "Would you like to continue and start the application? "
            "Please say Yes or No."
        )
    else:
        parts.append(
            "Pointcare does not currently show a program marked as one your "
            "household may be eligible for. "
            "Would you like to continue and start the application? Please say Yes or No."
        )
    return " ".join(parts)


def _short_program_name(name: str) -> str:
    text = re.sub(r"\s+", " ", (name or "").strip())
    if not text:
        return ""
    # Prefer common public names when the card title is long.
    low = text.lower()
    if "medikids" in low or "healthy kids" in low:
        return text[:80].rstrip(" .,:;")
    if "medicaid" in low or "medi-cal" in low or "medi cal" in low:
        if "medi-cal" in low or "medi cal" in low:
            return "Medi-Cal"
        return "Medicaid"
    if "medicare" in low:
        return "Medicare"
    if "marketplace" in low or "covered california" in low or "aca" in low:
        if "covered california" in low:
            return "Covered California"
        return "ACA Marketplace"
    # Keep short titles; trim long card text.
    return text[:48].rstrip(" .,:;")


def programs_context_reply(utterance: str, summary: dict[str, Any] | None) -> str | None:
    """Brief contextual answers on the program-summary decision — never navigate."""
    text = (utterance or "").strip().lower()
    if not text:
        return None
    summary = summary or {}
    if re.search(
        r"what happens if i (say )?no|if i (say )?no what|what if i (don'?t|do not)",
        text,
    ):
        return (
            "If you say no, I will not start the application. Your household "
            "eligibility results stay saved in Pointcare, and you can continue later "
            "by logging in. "
            + PROGRAMS_CLARIFY
        )
    if re.search(
        r"what (is|does) (this|that) program|what does that mean|which program|"
        r"what (am i|are we) (eligible|applying)|explain (the )?program",
        text,
    ):
        speech = build_programs_permission_speech(summary)
        return (
            "These are the programs Pointcare listed from your household eligibility "
            "answers — not an approval. "
            + speech
        )
    return None


async def scrape_program_summary(page: Page) -> dict[str, Any]:
    """Read program names / eligibility messages / Start Applications from the live page."""
    try:
        raw = await page.evaluate(
            """() => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const body = norm(document.body && document.body.innerText);
              const programs = [];
              const seen = new Set();
              const nodes = [...document.querySelectorAll(
                'label, li, [role="checkbox"], [role="listitem"], button, div, section'
              )];
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (r.width < 40 || r.height < 20) continue;
                let text = norm(el.innerText || el.textContent || '');
                if (!text || text.length < 4 || text.length > 320) continue;
                const low = text.toLowerCase();
                const looksProgram = (
                  /medicaid|medi-?cal|medicare|marketplace|covered california|\\baca\\b|medikids|healthy kids|florida/i.test(low)
                );
                if (!looksProgram) continue;
                // Prefer compact cards over giant page wrappers.
                if (text.length > 220) continue;
                const notEligible = /not eligible at this time|not eligible/i.test(low);
                const mayEligible = !notEligible && (
                  /may be eligible|might be eligible|eligible for/i.test(low)
                );
                // Extract a short name: first line or known keyword phrase.
                let name = text.split('\\n')[0].slice(0, 80);
                const nameMatch = text.match(
                  /(Medi-?Cal|Medicaid|Medicare|ACA Marketplace|Covered California)/i
                );
                if (nameMatch) name = nameMatch[1];
                const key = name.toLowerCase();
                if (seen.has(key)) continue;
                seen.add(key);
                let checked = false;
                const input = el.querySelector && el.querySelector('input[type=checkbox]');
                if (input) checked = !!input.checked;
                const aria = el.getAttribute && el.getAttribute('aria-checked');
                if (aria === 'true') checked = true;
                if (aria === 'false') checked = false;
                programs.push({
                  name,
                  detail: text.slice(0, 200),
                  may_be_eligible: mayEligible || (checked && !notEligible),
                  not_eligible: notEligible,
                  checked,
                });
                if (programs.length >= 8) break;
              }
              const hasStart = /start applications/i.test(body);
              const hasContinue = /continue to applications/i.test(body);
              const headline = /great news/i.test(body) ? 'Great News' : '';
              return {
                programs,
                has_start_applications: hasStart,
                has_continue_to_applications: hasContinue,
                headline,
                body_sample: body.slice(0, 500),
              };
            }"""
        )
    except Exception:
        logger.exception("scrape_program_summary failed")
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    programs = raw.get("programs") if isinstance(raw.get("programs"), list) else []
    return {
        "programs": programs,
        "has_start_applications": bool(raw.get("has_start_applications")),
        "has_continue_to_applications": bool(raw.get("has_continue_to_applications")),
        "headline": str(raw.get("headline") or ""),
        "body_sample": str(raw.get("body_sample") or "")[:500],
    }


def summary_from_session_filled(filled: dict[str, str]) -> dict[str, Any]:
    raw = (filled.get("program_summary_json") or "").strip()
    if not raw:
        return {"programs": []}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"programs": []}
    except Exception:
        return {"programs": []}


async def store_program_summary(session: Any, page: Page) -> dict[str, Any]:
    summary = await scrape_program_summary(page)
    session.filled["program_summary_json"] = json.dumps(summary)
    speech = build_programs_permission_speech(summary)
    session.filled["programs_permission_speech"] = speech
    return summary


async def click_exact_visible_text(page: Page, label: str) -> bool:
    """Click the smallest visible node whose text is exactly ``label``.

    Pointcare's yellow actions are often a div, not a <button>.
    """
    try:
        clicked = await page.evaluate(
            """(label) => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const want = norm(label);
              if (!want) return false;
              const nodes = [...document.querySelectorAll(
                'button, [role=button], a, div, span'
              )];
              const hits = [];
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (r.width < 8 || r.height < 8) continue;
                const text = norm(el.innerText || el.textContent || '');
                if (text !== want || text.length > 80) continue;
                hits.push({ el, area: r.width * r.height });
              }
              hits.sort((a, b) => a.area - b.area);
              if (!hits.length) return false;
              hits[0].el.click();
              return true;
            }""",
            label,
        )
        return bool(clicked)
    except Exception:
        logger.exception("click_exact_visible_text failed label=%s", label)
        return False


async def click_quick_apply(page: Page) -> str:
    """Click the dashboard Quick Apply control once.

    Prefers ``Continue Quick Apply`` over ``New Quick Apply`` / bare ``Quick Apply``.
    """
    for label in ("Continue Quick Apply", "Quick Apply", "New Quick Apply"):
        if await click_exact_visible_text(page, label):
            return label
    try:
        clicked = await page.evaluate(
            """() => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const nodes = [...document.querySelectorAll(
                'button, [role=button], a, div, span'
              )];
              const hits = [];
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (r.width < 8 || r.height < 8) continue;
                const text = norm(el.innerText || el.textContent || '');
                if (!text || text.length > 60) continue;
                if (!text.includes('quick apply')) continue;
                if (text.includes('recheck') || text.includes('add member')) continue;
                if (text.includes('household eligibility')) continue;
                hits.push({ el, text, len: text.length, area: r.width * r.height });
              }
              // Prefer Continue Quick Apply over New / bare Quick Apply.
              hits.sort((a, b) => {
                const rank = (t) =>
                  t.includes('continue') ? 0 : t === 'quick apply' ? 1 : 2;
                const d = rank(a.text) - rank(b.text);
                if (d) return d;
                return a.len - b.len || a.area - b.area;
              });
              if (!hits.length) return '';
              hits[0].el.click();
              return hits[0].text;
            }"""
        )
        return str(clicked or "").strip()
    except Exception:
        logger.exception("click_quick_apply failed")
        return ""


async def wait_for_saved_household_surface(page: Page, timeout_s: float = 12.0) -> str:
    """Wait briefly for Quick Apply / programs / application screens after sign-in."""
    deadline = time.monotonic() + timeout_s
    last = "unknown"
    waiting_for_buttons = False
    while time.monotonic() < deadline:
        last = await classify_saved_household_page(page)
        if last.startswith("app_") or last == "programs":
            return last
        body = await _page_body_lower(page)
        if "quick apply" in body:
            return "dashboard"
        if "next steps" in body and "household" in body:
            # Dashboard shell visible; Quick Apply buttons may still be rendering.
            waiting_for_buttons = True
            await page.wait_for_timeout(400)
            continue
        if last == "dashboard":
            waiting_for_buttons = True
            await page.wait_for_timeout(400)
            continue
        if not waiting_for_buttons:
            return last
        await page.wait_for_timeout(400)
    return last


async def _page_body_lower(page: Page) -> str:
    try:
        text = await page.locator("body").inner_text()
    except Exception:
        return ""
    return re.sub(r"\s+", " ", text or "").strip().lower()


async def classify_saved_household_page(page: Page) -> str:
    """Distinguish dashboard (Quick Apply) from Great News (Start/Continue Applications)."""
    body = await _page_body_lower(page)
    has_start = (
        "start applications" in body
        or "continue to applications" in body
        or "continue to application" in body
    )
    has_quick = "quick apply" in body
    has_news = "great news" in body
    if has_news or (
        has_start
        and (
            "not eligible" in body
            or "may be eligible" in body
            or "applying to" in body
        )
    ):
        return "programs"
    # Quick Apply is the dashboard. Check it before application-step keywords
    # so household cards are not mistaken for an in-progress application.
    if has_quick and not has_start:
        return "dashboard"
    app_screen = await detect_application_screen(page)
    if app_screen and app_screen not in ("", "programs"):
        return app_screen
    if "next steps" in body and "household" in body:
        return "dashboard"
    return "unknown"


def _arm_start_permission(session: Any, speech: str) -> str:
    """Ask once before any Quick Apply / Start Applications click."""
    session.elig_step = "proceed_application"
    session.app_step = ""
    session.signed_in = True
    session.welcome_done = True
    session.filled["application_gate"] = "proceed"
    session.filled["application_gate_speech"] = speech
    session.filled["application_test_opening"] = speech
    session.filled["awaiting_quick_apply"] = "1"
    session.filled["application_started"] = "0"
    return "ASK: " + speech


async def prepare_application_test(session: Any, page: Page) -> str:
    """Land on the saved household and ask before starting the application.

    Flow: sign-in → acknowledge eligibility finished → ask to start → Yes/No.
    Does not click Quick Apply until the caller says Yes.
    """
    surface = await wait_for_saved_household_surface(page)
    logger.info(
        "application_test_surface call_id=%s surface=%s url=%s",
        getattr(session, "call_id", "-"),
        surface,
        (page.url or "")[:140],
    )
    if surface == "dashboard":
        return _arm_start_permission(session, PROCEED_QUESTION)
    if surface == "programs":
        await store_program_summary(session, page)
        speech = (session.filled.get("programs_permission_speech") or "").strip()
        if not speech:
            speech = build_programs_permission_speech(
                summary_from_session_filled(session.filled)
            )
        # Still one Yes/No gate — programs speech already asks to start.
        session.elig_step = "programs"
        session.app_step = ""
        session.signed_in = True
        session.welcome_done = True
        session.filled["application_test_opening"] = speech
        session.filled["application_started"] = "0"
        session.filled.pop("awaiting_quick_apply", None)
        return "ASK: " + speech
    if surface.startswith("app_"):
        # Application-test default: restart at question 1 via Back, do not resume
        # mid-flow from a prior call. Browser must actually show the first step.
        first = await restart_application_to_first_question(page)
        verified = await detect_application_screen(page)
        if verified not in _FIRST_STAGE_E_STEPS:
            raise RuntimeError(
                "APPLICATION_TEST: restart reported "
                f"{first!r} but the visible screen is {verified!r}."
            )
        step = verified
        question = app_question_for_step(step)
        _set_pending_question(session, step, question)
        session.app_step = step
        session.elig_step = ""
        session.signed_in = True
        session.welcome_done = True
        session.filled["application_started"] = "1"
        session.filled["resume_app_step"] = step
        session.filled.pop("awaiting_quick_apply", None)
        # Clear mid-flow collect queues from a prior process if any.
        session.filled.pop("app_dob_queue", None)
        session.filled.pop("app_dob_current", None)
        session.filled.pop("app_immigration_queue", None)
        session.filled.pop("app_immigration_current", None)
        session.filled.pop("app_citizenship_no", None)
        session.filled.pop("app_insurance_queue", None)
        session.filled.pop("app_insurance_current", None)
        speech = (
            "Okay, I'll start the application from the first question. " + question
        )
        session.filled["application_test_opening"] = speech
        return "ASK: " + speech
    body = (await _page_body_lower(page))[:300]
    raw_url = (getattr(page, "url", "") or "").lower()
    if "/application" in raw_url:
        raise RuntimeError(
            "APPLICATION_TEST: an application page is open but the active Stage E "
            f"step could not be identified reliably. url={page.url} surface={surface} "
            f"body={body!r}"
        )
    raise RuntimeError(
        "APPLICATION_TEST: expected the saved household dashboard (Quick Apply), "
        "the Great News program summary (Start Applications), or an application "
        f"already in progress. url={page.url} surface={surface} body={body!r}"
    )


def build_start_confirmation(summary: dict[str, Any]) -> str:
    """Explain eligible programs on the page, then ask before Start Applications.

    Do not announce programs marked not eligible.
    """
    eligible = [
        p
        for p in (summary.get("programs") or [])
        if isinstance(p, dict) and p.get("may_be_eligible")
    ]
    parts: list[str] = []
    names = [_short_program_name(str(p.get("name") or "")) for p in eligible]
    names = [n for n in names if n]
    if len(names) == 1:
        parts.append(f"Pointcare shows your household may be eligible for {names[0]}.")
    elif names:
        joined = ", ".join(names[:-1]) + f", and {names[-1]}"
        parts.append(f"Pointcare shows your household may be eligible for {joined}.")
    else:
        parts.append(
            "Pointcare does not currently show a program marked as one your "
            "household may be eligible for."
        )
    parts.append(
        "Should I start the application for the eligible programs? "
        "Please say Yes to start, or No to stop."
    )
    return " ".join(parts)


async def begin_application_after_signin(session: Any, page: Page) -> str:
    """After sign-in, ask before starting the application. Never starts Stage D."""
    session.filled["signin_choice_done"] = "1"
    if session.elig_step in GATE_STEPS:
        speech = (session.filled.get("application_gate_speech") or PROCEED_QUESTION).strip()
        return "ASK: " + speech
    if session.app_step:
        speech = (
            "Your application is already in progress. "
            + app_question_for_step(session.app_step)
        )
        return "ASK: " + speech

    surface = await wait_for_saved_household_surface(page)
    logger.info(
        "application_after_signin_surface call_id=%s surface=%s url=%s",
        getattr(session, "call_id", "-"),
        surface,
        (page.url or "")[:140],
    )
    if surface == "dashboard":
        return _arm_start_permission(session, PROCEED_QUESTION)
    if surface == "programs":
        await store_program_summary(session, page)
        return _arm_start_permission(session, PROCEED_QUESTION)
    if surface.startswith("app_"):
        # Remember the live step; wait for Yes before speaking Stage E questions.
        session.filled["resume_app_step"] = surface
        session.filled["application_started"] = "1"
        session.filled.pop("awaiting_quick_apply", None)
        session.elig_step = "proceed_application"
        session.app_step = ""
        session.signed_in = True
        session.welcome_done = True
        session.filled["application_gate"] = "proceed"
        session.filled["application_gate_speech"] = PROCEED_QUESTION
        return "ASK: " + PROCEED_QUESTION
    body = (await _page_body_lower(page))[:300]
    raw_url = (getattr(page, "url", "") or "").lower()
    if "/application" in raw_url:
        raise RuntimeError(
            "APPLICATION_AFTER_SIGNIN: an application page is open but the active "
            f"Stage E step could not be identified. url={page.url} surface={surface} "
            f"body={body!r}"
        )
    raise RuntimeError(
        "APPLICATION_AFTER_SIGNIN: expected the saved household dashboard "
        "(Quick Apply), the program summary (Start Applications), or an "
        f"application already in progress. url={page.url} surface={surface} "
        f"body={body!r}"
    )


async def _wait_surface_after_quick_apply(page: Page, timeout_s: float = 15.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        await page.wait_for_timeout(400)
        surface = await classify_saved_household_page(page)
        if surface and surface != "dashboard":
            return surface
    return await classify_saved_household_page(page)


async def _start_application_from_permission(
    call_id: str,
    session: Any,
    page: Page,
    answer: str,
) -> dict[str, Any]:
    """Yes path: Continue Quick Apply (if needed) → Start Applications → Stage E."""
    surface = await classify_saved_household_page(page)
    if surface == "dashboard":
        clicked = await click_quick_apply(page)
        if not clicked:
            logger.error(
                "quick_apply_missing_after_yes call_id=%s url=%s",
                call_id,
                (page.url or "")[:120],
            )
            speech = (
                session.filled.get("application_gate_speech") or PROCEED_QUESTION
            ).strip()
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "eligibility_answer",
                "value": answer,
                "error": "quick_apply_missing",
                "spoken": "ASK: " + speech,
                "message": "Quick Apply control not found",
                "step": "proceed_application",
                "phase": "eligibility",
            }
        session.filled["quick_apply_clicked"] = clicked
        surface = await _wait_surface_after_quick_apply(page)
        logger.info(
            "quick_apply_after_yes call_id=%s clicked=%s surface=%s",
            call_id,
            clicked,
            surface,
        )
    session.filled.pop("awaiting_quick_apply", None)

    if surface.startswith("app_"):
        session.filled["application_started"] = "1"
        session.elig_step = ""
        # Application-test: never resume mid-flow after Quick Apply — Back to Q1.
        from . import config as _cfg

        if getattr(_cfg, "APPLICATION_TEST_MODE", False):
            first = await restart_application_to_first_question(page)
            verified = await detect_application_screen(page)
            if verified not in _FIRST_STAGE_E_STEPS:
                raise RuntimeError(
                    "APPLICATION_TEST: after Quick Apply, restart reported "
                    f"{first!r} but visible screen is {verified!r}."
                )
            step = verified
            question = app_question_for_step(step)
            _set_pending_question(session, step, question)
            session.app_step = step
            session.filled["resume_app_step"] = step
            session.filled.pop("app_dob_queue", None)
            session.filled.pop("app_dob_current", None)
            session.filled.pop("app_immigration_queue", None)
            session.filled.pop("app_immigration_current", None)
            session.filled.pop("app_citizenship_no", None)
            session.filled.pop("app_insurance_queue", None)
            session.filled.pop("app_insurance_current", None)
            intro = (
                "Okay, I'll start the application from the first question. "
                + question
            )
            return _app_ok(
                call_id,
                session,
                answer,
                step,
                "Restarted in-progress application at question 1",
                spoken_override=intro,
            )
        step, question = await _prepare_resume_speech(session, page, surface)
        session.app_step = step
        intro = "Okay, I've started the application. " + question
        return _app_ok(
            call_id,
            session,
            answer,
            step,
            "Opened in-progress application",
            spoken_override=intro,
        )

    if surface == "programs":
        await store_program_summary(session, page)
        clicked = await click_start_applications(page)
        started = await wait_for_application_started(page)
        if not started:
            logger.error(
                "start_applications_not_verified call_id=%s clicked=%s url=%s",
                call_id,
                clicked,
                (page.url or "")[:120],
            )
            speech = (
                session.filled.get("application_gate_speech") or PROCEED_QUESTION
            ).strip()
            session.elig_step = "proceed_application"
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "eligibility_answer",
                "value": answer,
                "error": "start_applications_failed",
                "spoken": (
                    "ASK: I couldn't confirm that the application started. "
                    "Please say Yes to try again, or No to end the call. "
                    + speech
                ),
                "message": "Start Applications click not verified",
                "step": "proceed_application",
                "phase": "eligibility",
            }
        session.filled["application_started"] = "1"
        session.filled["start_applications_clicked"] = clicked
        session.elig_step = ""
        screen = await detect_application_screen(page)
        if (
            not screen
            or screen == "programs"
            or not str(screen).startswith("app_")
        ):
            logger.error(
                "application_first_screen_unverified call_id=%s clicked=%s",
                call_id,
                clicked,
            )
            session.elig_step = "proceed_application"
            speech = (
                session.filled.get("application_gate_speech") or PROCEED_QUESTION
            ).strip()
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "eligibility_answer",
                "value": answer,
                "error": "application_screen_unverified",
                "spoken": (
                    "ASK: I couldn't confirm the first application screen yet. "
                    "Please say Yes to try again, or No to end the call. "
                    + speech
                ),
                "message": "Application screen was not verified",
                "step": "proceed_application",
                "phase": "eligibility",
            }
        if screen in _FIRST_STAGE_E_STEPS:
            next_step = screen
            question = app_question_for_step(next_step)
        else:
            next_step, question = await _prepare_resume_speech(
                session, page, screen
            )
        session.app_step = next_step
        intro = "Okay, I've started the application. " + question
        return _app_ok(
            call_id,
            session,
            answer,
            next_step,
            "Started application",
            spoken_override=intro,
        )

    speech = (session.filled.get("application_gate_speech") or PROCEED_QUESTION).strip()
    session.elig_step = "proceed_application"
    session.filled["awaiting_quick_apply"] = "1"
    return {
        "ok": False,
        "call_id": call_id,
        "field_id": "eligibility_answer",
        "value": answer,
        "error": "application_start_surface_missing",
        "spoken": "ASK: " + speech,
        "message": f"Unexpected surface after Yes: {surface}",
        "step": "proceed_application",
        "phase": "eligibility",
    }


async def handle_application_gate(
    call_id: str,
    session: Any,
    page: Page,
    answer: str,
) -> dict[str, Any]:
    """Proceed question, then a separate confirmation before Start Applications."""
    from .live_form import classify_yes_no

    step = session.elig_step or ""
    decision = classify_yes_no(answer)
    lowered = (answer or "").strip().lower()
    if decision is None and re.search(
        r"\b(start|continue|proceed|go ahead|quick apply)\b", lowered
    ):
        decision = "yes"
    if step == "proceed_application":
        if decision is None:
            speech = (session.filled.get("application_gate_speech") or PROCEED_QUESTION).strip()
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "eligibility_answer",
                "value": answer,
                "error": "proceed_clarify",
                "spoken": (
                    "ASK: Please say Yes to start the application, or No to end "
                    "the call. " + speech
                ),
                "message": "proceed clarify",
                "step": "proceed_application",
                "phase": "eligibility",
            }
        if decision == "no":
            session.elig_step = ""
            session.app_step = ""
            logger.info("application_proceed_declined call_id=%s", call_id)
            return {
                "ok": True,
                "call_id": call_id,
                "field_id": "eligibility_answer",
                "value": answer,
                "action": "declined_application",
                "message": "Caller declined to proceed",
                "spoken": "ASK: " + CLOSING_NO_PROCEED,
                "step": "",
                "phase": "closing",
                "end_call": True,
            }
        resume = (session.filled.get("resume_app_step") or "").strip()
        if resume or session.filled.get("application_started") == "1":
            from . import config as _cfg

            live = await detect_application_screen(page)
            if live and live.startswith("app_"):
                surface = live
            elif resume.startswith("app_"):
                surface = resume
            else:
                return _unidentified_screen_error(call_id, session, answer)
            # Application-test: Back to question 1 instead of resuming mid-flow.
            if getattr(_cfg, "APPLICATION_TEST_MODE", False):
                first = await restart_application_to_first_question(page)
                verified = await detect_application_screen(page)
                if verified not in _FIRST_STAGE_E_STEPS:
                    raise RuntimeError(
                        "APPLICATION_TEST: gate restart reported "
                        f"{first!r} but visible screen is {verified!r}."
                    )
                step = verified
                question = app_question_for_step(step)
                _set_pending_question(session, step, question)
                session.app_step = step
                session.elig_step = ""
                session.filled["application_started"] = "1"
                session.filled["resume_app_step"] = step
                session.filled.pop("awaiting_quick_apply", None)
                session.filled.pop("app_dob_queue", None)
                session.filled.pop("app_dob_current", None)
                intro = (
                    "Okay, I'll start the application from the first question. "
                    + question
                )
                return _app_ok(
                    call_id,
                    session,
                    answer,
                    step,
                    "Restarted in-progress application at question 1",
                    spoken_override=intro,
                )
            step, question = await _prepare_resume_speech(session, page, surface)
            session.app_step = step
            session.elig_step = ""
            session.filled["application_started"] = "1"
            session.filled["resume_app_step"] = step
            session.filled.pop("awaiting_quick_apply", None)
            intro = (
                "Okay, I'll continue the application that's already open. "
                + question
            )
            return _app_ok(
                call_id,
                session,
                answer,
                step,
                "Resumed in-progress application",
                spoken_override=intro,
            )
        # One Yes starts the application (Continue Quick Apply → Start Applications).
        return await _start_application_from_permission(
            call_id, session, page, answer
        )

    if step != "confirm_start_application":
        raise RuntimeError(f"APPLICATION_AFTER_SIGNIN: unexpected gate step {step!r}")

    if decision is None:
        speech = (session.filled.get("application_gate_speech") or "").strip()
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "eligibility_answer",
            "value": answer,
            "error": "confirm_clarify",
            "spoken": "ASK: Please say Yes to start the application, or No to stop. " + speech,
            "message": "start confirmation clarify",
            "step": "confirm_start_application",
            "phase": "eligibility",
        }
    if decision == "no":
        session.elig_step = ""
        session.app_step = ""
        session.filled["application_started"] = "0"
        return {
            "ok": True,
            "call_id": call_id,
            "field_id": "eligibility_answer",
            "value": answer,
            "action": "declined_application",
            "message": "Caller declined Start Applications",
            "spoken": "ASK: " + CLOSING_NO_PROCEED,
            "step": "",
            "phase": "closing",
            "end_call": True,
        }
    if session.filled.get("application_started") == "1" and session.app_step:
        return _app_ok(
            call_id,
            session,
            answer,
            session.app_step,
            "Application already started",
            spoken_override=app_question_for_step(session.app_step),
        )
    clicked = await click_start_applications(page)
    started = await wait_for_application_started(page)
    if not started:
        logger.error(
            "start_applications_not_verified call_id=%s clicked=%s url=%s",
            call_id,
            clicked,
            (page.url or "")[:120],
        )
        speech = (session.filled.get("application_gate_speech") or "").strip()
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "eligibility_answer",
            "value": answer,
            "error": "start_applications_failed",
            "spoken": (
                "ASK: I couldn't confirm that the application started. " + speech
            ),
            "message": "Start Applications click not verified",
            "step": "confirm_start_application",
            "phase": "eligibility",
        }
    session.filled["application_started"] = "1"
    session.filled["start_applications_clicked"] = clicked
    session.elig_step = ""
    screen = await detect_application_screen(page)
    next_step = screen if screen and screen != "programs" else "app_citizenship"
    session.app_step = next_step
    intro = "Okay, I've started the application. " + app_question_for_step(next_step)
    logger.info(
        "stage_e_started call_id=%s clicked=%s first_step=%s",
        call_id,
        clicked,
        next_step,
    )
    return _app_ok(
        call_id,
        session,
        answer,
        next_step,
        "Started application",
        spoken_override=intro,
    )


async def click_start_applications(page: Page) -> str:
    """Click the live Start Applications (or Continue to Applications) control once."""
    from .live_form import _click_cta

    labels = (
        "Start Applications",
        "Start Application",
        "Continue to Applications",
        "Continue to Application",
    )
    for label in labels:
        if await click_exact_visible_text(page, label):
            return label
    try:
        await _click_cta(page, "Start Applications", attempts=1)
        return "Start Applications"
    except Exception as exc:
        raise RuntimeError(
            "Could not find Start Applications on the program summary screen: "
            + str(exc)
        ) from exc


async def wait_for_application_started(page: Page, *, timeout_ms: int = 20000) -> bool:
    """True when Pointcare left the program-summary for an application step."""
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        screen = await detect_application_screen(page)
        if screen and screen != "programs":
            return True
        url = (page.url or "").lower()
        if any(
            part in url
            for part in ("/application", "/apply", "step-", "eligibility/application")
        ):
            # URL moved even if heading scrape is slow.
            if "program" not in url or "application" in url:
                return True
        try:
            body = (await page.locator("body").inner_text()).lower()
        except Exception:
            body = ""
        if "start applications" not in body and (
            "u.s. citizen" in body
            or "us citizen" in body
            or "application eligibility" in body
            or "step 1 of" in body
            or "licensed agent" in body
        ):
            return True
        await page.wait_for_timeout(350)
    return False


async def detect_application_screen(page: Page) -> str:
    """Map the visible application UI to an app_* step key.

    Never invent ``app_citizenship`` from the URL alone — unknown application
    pages return ``\"\"`` so resume can report a recoverable error.
    """
    try:
        body = re.sub(
            r"\s+", " ", (await page.locator("body").inner_text()).lower()
        )
    except Exception:
        return ""
    if "great news" in body or (
        "start applications" in body and ("not eligible" in body or "may be eligible" in body)
    ):
        return "programs"
    if "continue to applications" in body and "eligible" in body:
        return "programs"
    if "licensed agent" in body or "clearguide" in body or "connect me" in body:
        return "app_agent_offer"
    # Immigration detail (member radios) vs everyone-citizens Yes/No.
    has_immig_options = (
        "prefer not to answer" in body
        and (
            "satisfactory immigration" in body
            or "u.s. national" in body
            or "us national" in body
        )
    )
    has_citizen_question = bool(
        re.search(
            r"(are|is)\s+(all|everyone|every)\b.{0,40}\bcitizen",
            body,
        )
        or re.search(r"\ball\s+u\.?s\.?\s+citizens\b", body)
        or "everyone listed is a u.s. citizen" in body
        or "some or all are not u.s. citizens" in body
        or (
            "all household members" in body
            and "citizen" in body
        )
    )
    if has_immig_options and not has_citizen_question:
        return "app_immigration"
    if "u.s. citizen" in body or "us citizen" in body:
        if has_immig_options and has_citizen_question:
            # Yes/No still visible with expanded options — stay on citizenship
            # until No is verified; callers reconcile to immigration after No.
            return "app_citizenship"
        return "app_citizenship"
    if "immigration status" in body and has_immig_options:
        return "app_immigration"
    if (
        "health insurance" in body
        or ("insurance" in body and ("anyone" in body or "coverage" in body))
        or "someone has coverage" in body
        or "no one has coverage" in body
        or "nobody has coverage" in body
        or "no one has health" in body
    ):
        return "app_insurance"
    if (
        "date of birth" in body
        or re.search(r"\bdob\b", body)
        or "add date of birth" in body
        or "needs date of birth" in body
    ):
        return "app_dob_review"
    if "home address" in body or "mailing address" in body:
        # Review screens may already show saved addresses without Add/pencil.
        return "app_address_review"
    if "marital status" in body and ("pencil" in body or "check" in body):
        return "app_marital_review"
    if "children under 19" in body or "caring for children" in body:
        return "app_childcare_review"
    if (
        "daily activities" in body
        or "alaska native" in body
        or "american indian" in body
        or ("veteran" in body and "student 18" in body)
    ):
        return "app_circumstances"
    if "filing status" in body or "head of household" in body or (
        "tax" in body and "dependent" in body
    ):
        return "app_tax"
    if "add income" in body or ("income type" in body) or (
        "employed" in body and "self-employed" in body and "none" in body
    ):
        return "app_income"
    if "household expense" in body or (
        "student loan" in body and "childcare" in body and "optional" in body
    ):
        return "app_expenses"
    if "upload a file" in body or "take a photo" in body or "proof of" in body:
        return "app_documents"
    if (
        "you're all set" in body
        or "you are all set" in body
        or "submit applications" in body
        or "submit application" in body
    ):
        return "app_submit_confirm"
    if any(
        needle in body
        for needle in (
            "submitted",
            "thank you",
            "we received",
            "application has been",
            "all set",
        )
    ):
        # Post-submit confirmation — do not invent an earlier step.
        return ""
    return ""


async def immigration_member_radios_visible(page: Page) -> bool:
    """True when per-member immigration radios are on the page.

    The citizenship Yes/No cards often stay visible above those radios after No.
    """
    try:
        locator = page.locator("body")
        if hasattr(locator, "__await__"):
            locator = await locator
        raw = locator.inner_text()
        if hasattr(raw, "__await__"):
            raw = await raw
    except Exception:
        return False
    if not isinstance(raw, str):
        return False
    body = re.sub(r"\s+", " ", raw.lower())
    return "prefer not to answer" in body and (
        "satisfactory immigration" in body
        or "u.s. national" in body
        or "us national" in body
    )


def expanded_immigration_active(
    session: Any, current: str, live: str, radios_visible: bool
) -> bool:
    """Keep the per-member status step after No while those radios are visible."""
    if not radios_visible:
        return False
    filled = getattr(session, "filled", None) or {}
    if not str(filled.get("app_citizenship_no") or "").strip():
        return False
    if live not in ("app_citizenship", "app_immigration"):
        return False
    return current in (
        "app_citizenship",
        "app_immigration",
        "app_immigration_who",
    )


def app_question_for_step(step: str, session: Any | None = None) -> str:
    """Return the spoken prompt for ``step``, preferring a pending personalized line."""
    if session is not None:
        pending = (session.filled.get("app_pending_question") or "").strip()
        pending_step = (session.filled.get("app_pending_step") or "").strip()
        if pending and (not pending_step or pending_step == step):
            return pending
    return (APP_QUESTIONS.get(step) or "").strip() or (
        "Please answer the current application question."
    )


def _set_pending_question(session: Any, step: str, speech: str) -> None:
    text = (speech or "").strip()
    if text:
        session.filled["app_pending_question"] = text
        session.filled["app_pending_step"] = step
    else:
        session.filled.pop("app_pending_question", None)
        session.filled.pop("app_pending_step", None)


def stage_e_explain(step: str) -> str:
    """Brief why-this-question context without inventing options."""
    base = app_question_for_step(step)
    extra = (_STAGE_E_EXPLAIN.get(step) or "").strip()
    if extra:
        return f"{extra} {base}"
    return base


def _income_seq_state(session: Any) -> dict[str, Any]:
    raw = session.filled.get("app_income_seq") or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _set_income_seq(session: Any, data: dict[str, Any] | None) -> None:
    if not data:
        session.filled.pop("app_income_seq", None)
        return
    session.filled["app_income_seq"] = json.dumps(data)


async def _fill_next_visible_input(page: Page, value: str) -> bool:
    """Fill the next empty visible text/number input (income detail Collect Mode)."""
    try:
        filled = await page.evaluate(
            """(value) => {
              const want = (value || '').trim();
              if (!want) return false;
              const inputs = [...document.querySelectorAll(
                'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea'
              )].filter((el) => {
                const r = el.getBoundingClientRect();
                return r.width > 2 && r.height > 2 && !el.disabled && !el.readOnly;
              });
              for (const el of inputs) {
                if ((el.value || '').trim()) continue;
                el.focus();
                el.value = want;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
              }
              // Fallback: overwrite the first visible empty-looking field.
              if (inputs.length) {
                const el = inputs[0];
                el.focus();
                el.value = want;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
              }
              return false;
            }""",
            value,
        )
        return bool(filled)
    except Exception:
        logger.exception("fill_next_visible_input failed")
        return False


async def _scrape_review_summary(page: Page, step: str) -> str:
    """Best-effort short summary of saved review-screen values for confirmation."""
    body = await _page_body_lower(page)
    if not body:
        return ""
    # Keep summaries short — never dump the whole page.
    snippet = re.sub(r"\s+", " ", body)[:280]
    if step == "app_dob_review" and "date of birth" in snippet:
        return "I see date of birth details already on this screen. "
    if step == "app_address_review" and (
        "address" in snippet or "mailing" in snippet
    ):
        return "I see home or mailing address details already on this screen. "
    if step == "app_marital_review" and "marital" in snippet:
        return "I see a marital status already filled in. "
    if step == "app_childcare_review" and (
        "children" in snippet or "care" in snippet
    ):
        return "I see childcare answers already on this screen. "
    if step == "app_tax" and ("filing" in snippet or "tax" in snippet):
        return "I see a tax filing status already filled in. "
    return ""


def _expand_stage_e_spoken_year(text: str) -> str:
    """Turn 'nineteen ninety-five' into 1995. Does not touch 'twenty twenty-four'."""
    from .live_form import _DOB_CARDINAL

    tens = {
        "twenty": 20,
        "thirty": 30,
        "forty": 40,
        "fifty": 50,
        "sixty": 60,
        "seventy": 70,
        "eighty": 80,
        "ninety": 90,
    }

    def _repl(match: re.Match[str]) -> str:
        words = match.group(1).replace("-", " ").split()
        base = tens.get(words[0])
        if base is None:
            return match.group(0)
        if len(words) == 1:
            return str(1900 + base)
        ones = _DOB_CARDINAL.get(words[1])
        if ones is None or ones > 9:
            return match.group(0)
        return str(1900 + base + ones)

    return re.sub(
        r"\bnineteen\s+((?:ninety|eighty|seventy|sixty|fifty|forty|thirty|twenty)"
        r"(?:[-\s]+[a-z]+)?)",
        _repl,
        text,
        flags=re.I,
    )


def _partial_dob_clarify(text: str) -> str | None:
    """Ask only for the missing part of an incomplete date. None if it is not a date fragment."""
    from .live_form import _DOB_MONTHS

    raw = re.sub(r"[.,]", " ", (text or "").lower())
    raw = re.sub(r"(\d+)(?:st|nd|rd|th)\b", r"\1", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return None
    month_name = "|".join(sorted(_DOB_MONTHS, key=len, reverse=True))
    has_month = re.search(rf"\b({month_name})\b", raw) is not None
    has_year = re.search(r"\b(?:19|20)\d{2}\b", raw) is not None
    has_day = re.search(r"\b([1-9]|[12]\d|3[01])\b", raw) is not None
    only_day = re.fullmatch(
        r"(?:the\s+)?([1-9]|[12]\d|3[01]|first|second|third|fourth|fifth|"
        r"twenty(?:\s+[a-z]+)?)",
        raw,
    )
    if only_day and not has_month and not has_year:
        return "I have the day. Please say the month and the year."
    if has_month and not has_day and not has_year:
        return "I have the month. Please say the day and the year."
    if has_month and has_day and not has_year:
        return "Please say the year."
    if has_month and has_year and not has_day:
        return "Please say the day."
    if has_year and not has_month and not has_day:
        return "Please say the month and the day."
    return None


def _parse_dob_answer(text: str) -> tuple[str | None, str | None]:
    """Parse a DOB utterance for Stage E.

    Returns ``(mm_dd_yyyy, clarify)``. On success ``clarify`` is None. When the
    numeric date is ambiguous (both parts ≤12), ``clarify`` asks which order
    without guessing. Incomplete dates ask only for the missing part. A
    two-digit year is never expanded. Never logs the raw DOB value.
    """
    from datetime import date

    from .live_form import extract_dob_ymd

    raw = (text or "").strip()
    if not raw:
        return None, None
    if re.search(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2}\b", raw) and not re.search(
        r"\b(?:19|20)\d{2}\b", raw
    ):
        return None, (
            "That year is only two digits, so the century is unclear. "
            "Please say the full four-digit year."
        )
    prepared = _normalize_dob_asr(raw)
    prepared = _expand_stage_e_spoken_year(prepared)
    prepared = re.sub(r"(\d)\.(\d)", r"\1/\2", prepared)
    month, day, year, status = extract_dob_ymd(
        prepared, prefer_us_when_ambiguous=False
    )
    if status == "ambiguous":
        return None, (
            "That numeric date is ambiguous. Please say the month name, then "
            "the day, then the year — for example, October twenty-second, "
            "twenty twenty-four."
        )
    if status is None and month and day and year:
        try:
            parsed = date(year, month, day)
        except ValueError:
            return None, None
        if parsed > date.today():
            return None, (
                "That date is in the future. Please say the date of birth again."
            )
        return f"{month:02d}/{day:02d}/{year:04d}", None
    if status == "incomplete":
        missing = _partial_dob_clarify(raw)
        return None, missing
    return None, None


def _normalize_dob_asr(text: str) -> str:
    """Fix DOB-only hearing errors such as 'Monday's October' → 'month is October'."""
    from .live_form import _DOB_MONTHS

    month_name = "|".join(sorted(_DOB_MONTHS, key=len, reverse=True))
    return re.sub(
        rf"\bmondays?(?:'s|’s)?\b(?=\s+(?:the\s+)?(?:{month_name})\b)",
        "month is",
        text or "",
        flags=re.I,
    )


def _dob_pieces(text: str) -> dict[str, Any]:
    """Pull month, day, and year without guessing a corrupted remainder."""
    from .live_form import _DOB_MONTHS, _dob_spoken_year, _dob_word_number

    prepared = _normalize_dob_asr(text or "")
    dob, clarify = _parse_dob_answer(prepared)
    if dob:
        month_s, day_s, year_s = _dob_parts(dob) or ("", "", "")
        return {
            "month": int(month_s),
            "day": int(day_s),
            "year": int(year_s),
            "ambiguous": False,
            "clarify": None,
        }
    if clarify and "ambiguous" in clarify.lower():
        return {"month": None, "day": None, "year": None, "ambiguous": True, "clarify": clarify}
    if clarify and "future" in clarify.lower():
        return {"month": None, "day": None, "year": None, "ambiguous": False, "clarify": clarify}
    if not clarify:
        from .live_form import extract_dob_ymd

        _month, _day, _year, status = extract_dob_ymd(
            prepared, prefer_us_when_ambiguous=False
        )
        if status == "invalid":
            return {
                "month": None,
                "day": None,
                "year": None,
                "ambiguous": False,
                "clarify": None,
                "invalid": True,
            }
    lowered = re.sub(r"[.,]", " ", prepared.lower())
    lowered = re.sub(r"\s+", " ", lowered).strip()
    month_name = "|".join(sorted(_DOB_MONTHS, key=len, reverse=True))
    month = None
    month_match = re.search(rf"\b({month_name})\b", lowered)
    if month_match:
        month = _DOB_MONTHS[month_match.group(1)]
    day = None
    day_match = re.search(
        r"\b(?:date|day)(?:\s+is)?\s+([a-z0-9 -]+?)(?:\s+year\b|$)",
        lowered,
    )
    if day_match:
        day = _dob_word_number(day_match.group(1).strip())
    year = None
    year_match = re.search(r"\byear(?:\s+is)?\s+(.+)$", lowered)
    if year_match:
        year = _dob_spoken_year(year_match.group(1).strip())
    labeled = bool(day_match or year_match)
    remainder = lowered
    if month_match:
        remainder = remainder.replace(month_match.group(0), " ", 1)
    remainder = re.sub(
        r"\b(?:month|date|day|year|is|the|a|an)\b", " ", remainder
    )
    remainder = re.sub(r"\s+", " ", remainder).strip()
    unresolved = bool(
        remainder
        and not labeled
        and re.search(r"\b(twenty|thirty|two|to|too|thousand)\b", remainder)
    )
    return {
        "month": month,
        "day": None if unresolved else day,
        "year": None if unresolved else year,
        "ambiguous": unresolved,
        "clarify": clarify,
    }


def _dob_partial_state(session: Any) -> dict[str, Any]:
    raw = session.filled.get("app_dob_partial") or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _store_dob_partial(
    session: Any, name: str, month: int | None, day: int | None, year: int | None
) -> None:
    session.filled["app_dob_partial"] = json.dumps(
        {"name": name, "month": month, "day": day, "year": year}
    )


def _clear_dob_partial(session: Any) -> None:
    session.filled.pop("app_dob_partial", None)


def _merge_dob_pieces(
    session: Any, member_name: str, pieces: dict[str, Any]
) -> tuple[str | None, str | None]:
    """Combine this utterance with parts already accepted for the same member."""
    from datetime import date

    from .live_form import _calendar_date_ok

    if pieces.get("invalid"):
        return None, None
    if pieces.get("clarify") and not any(
        pieces.get(key) for key in ("month", "day", "year")
    ):
        return None, str(pieces.get("clarify"))
    stored = _dob_partial_state(session)
    same = str(stored.get("name") or "").strip().lower() == (member_name or "").strip().lower()
    month = pieces.get("month") or (stored.get("month") if same else None)
    day = pieces.get("day") or (stored.get("day") if same else None)
    year = pieces.get("year") or (stored.get("year") if same else None)
    if month and day and year:
        if not _calendar_date_ok(int(month), int(day), int(year)):
            return None, None
        try:
            parsed = date(int(year), int(month), int(day))
        except ValueError:
            return None, None
        if parsed > date.today():
            return None, "That date is in the future. Please say the date of birth again."
        _clear_dob_partial(session)
        return f"{int(month):02d}/{int(day):02d}/{int(year):04d}", None
    _store_dob_partial(
        session,
        member_name,
        int(month) if month else None,
        int(day) if day else None,
        int(year) if year else None,
    )
    missing: list[str] = []
    if not month:
        missing.append("month")
    if not day:
        missing.append("day")
    if not year:
        missing.append("year")
    if pieces.get("ambiguous") and "day" in missing and "year" in missing:
        return None, "I have the month. Please say the day and the year."
    if not missing:
        return None, None
    if missing == ["month"]:
        return None, "Please say the month."
    if missing == ["day"]:
        return None, "Please say the day."
    if missing == ["year"]:
        return None, "Please say the year."
    if missing == ["day", "year"]:
        return None, "I have the month. Please say the day and the year."
    if missing == ["month", "year"]:
        return None, "I have the day. Please say the month and the year."
    if missing == ["month", "day"]:
        return None, "I have the year. Please say the month and the day."
    return None, "I need a clear month, day, and year."


_FIRST_STAGE_E_STEPS = frozenset({"app_agent_offer", "app_citizenship"})


def _app_step_parent(step: str) -> str:
    """Pointcare screen that owns a Collect Mode sub-step."""
    return _APP_SUBSTEPS.get(step, step)


async def application_go_back(call_id: str, session: Any, page: Page) -> dict[str, Any]:
    """Click the visible application Back control once and follow the new screen."""
    before = (session.app_step or "").strip()
    clicked = await _click_application_back(page)
    if not clicked:
        logger.warning("application_back_missing")
        return {
            "ok": False,
            "error": "no_back",
            "step": before,
            "spoken": (
                "I can't go back from this screen. There is no Back button. "
                + app_question_for_step(before or "app_citizenship", session)
            ),
        }
    await page.wait_for_timeout(400)
    screen = await detect_application_screen(page)
    if not screen or screen == before:
        logger.warning("application_back_screen_unchanged")
        return {
            "ok": False,
            "error": "back_unchanged",
            "step": before,
            "spoken": (
                "The screen did not change, so I stayed on this question. "
                + app_question_for_step(before or "app_citizenship", session)
            ),
        }
    step, speech = await _prepare_resume_speech(session, page, screen)
    session.app_step = step
    logger.info(
        "application_back call_id=%s from=%s to=%s",
        call_id,
        before or "-",
        step,
    )
    return {"ok": True, "step": step, "spoken": speech}


async def _click_application_back(page: Page) -> bool:
    """Click a visible Back control on the application. Returns True if clicked."""
    try:
        back = page.get_by_role("button", name=re.compile(r"^\s*Back\s*$", re.I)).first
        if await back.count() and await back.is_visible():
            await back.click(timeout=4000)
            await page.wait_for_timeout(450)
            return True
    except Exception:
        pass
    try:
        from .live_form import _click_cta

        await _click_cta(page, "Back", attempts=1)
        await page.wait_for_timeout(450)
        return True
    except Exception:
        return False


async def restart_application_to_first_question(page: Page) -> str:
    """Navigate the live application back to the first Stage E question.

    Uses Pointcare's Back control only — does not create a new draft, clear
    auth, or erase household eligibility. Returns the verified first-step key
    (``app_agent_offer`` or ``app_citizenship``). Raises RuntimeError when the
    UI cannot safely reach question 1.
    """
    screen = await detect_application_screen(page)
    if screen in _FIRST_STAGE_E_STEPS:
        logger.info("stage_e_already_at_first_question screen=%s", screen)
        return screen
    if not (screen or "").startswith("app_"):
        raise RuntimeError(
            "APPLICATION_TEST: expected an in-progress application screen to "
            f"restart from question 1, got {screen!r}."
        )

    for attempt in range(24):
        screen = await detect_application_screen(page)
        if screen in _FIRST_STAGE_E_STEPS:
            logger.info(
                "stage_e_restarted_to_first attempt=%s screen=%s",
                attempt,
                screen,
            )
            return screen
        if screen in ("programs",) or screen == "dashboard" or not screen:
            raise RuntimeError(
                "APPLICATION_TEST: navigated away from the application before "
                "reaching question 1. Pointcare has no safe restart-to-step-1 "
                "control here without risking a duplicate draft. Open the "
                "existing application to step 1 manually, or leave the draft "
                f"on the first question. last_screen={screen!r}"
            )
        before = screen
        clicked = await _click_application_back(page)
        if not clicked:
            raise RuntimeError(
                "APPLICATION_TEST: cannot reach question 1 from "
                f"{before!r}; Back control is missing or inactive. "
                "Leave the in-progress application on the first question "
                "(licensed agent or citizenship) before the next test call."
            )
        # Wait for the screen to change after Back.
        for _ in range(16):
            await page.wait_for_timeout(250)
            now = await detect_application_screen(page)
            if now != before:
                break

    final = await detect_application_screen(page)
    raise RuntimeError(
        "APPLICATION_TEST: too many Back clicks without reaching question 1 "
        f"(still on {final!r})."
    )


def _clean_member_display_name(name: str) -> str:
    """Strip trailing UI labels like Add that scrape sometimes glues onto names."""
    label = re.sub(r"\s+", " ", (name or "").strip())
    label = re.sub(
        r"\s+\b(add|edit|pencil|save|next|continue)\b\.?$",
        "",
        label,
        flags=re.I,
    ).strip(" .")
    return label or "that person"


def _dob_question_for(name: str) -> str:
    label = _clean_member_display_name(name)
    return (
        f"What is {label}'s date of birth? Please say the month, day, and year."
    )


def _immigration_status_question_for(name: str) -> str:
    label = _clean_member_display_name(name)
    return (
        f"What is {label}'s citizenship or immigration status — "
        "U.S. Citizen, U.S. National, Satisfactory immigration status "
        "(green card, etc.), or Prefer not to answer?"
    )


_IMMIGRATION_STATUS_OPTIONS = (
    "U.S. Citizen",
    "U.S. National",
    "Satisfactory immigration status (green card, etc.)",
    "Prefer not to answer",
)


def _citizenship_question(names: list[str]) -> str:
    """Ask the citizenship Yes/No question using the people actually listed."""
    clean: list[str] = []
    seen: set[str] = set()
    for name in names:
        label = _clean_member_display_name(name)
        key = label.lower()
        if not label or key in seen or key in {"that person", "your household"}:
            continue
        seen.add(key)
        clean.append(label)
    if not clean:
        return APP_QUESTIONS["app_citizenship"]
    if len(clean) == 1:
        return f"Is {clean[0]} a U.S. Citizen? Please say Yes or No."
    if len(clean) == 2:
        who = f"{clean[0]} and {clean[1]}"
    else:
        who = ", ".join(clean[:-1]) + ", and " + clean[-1]
    return f"Are {who} all U.S. Citizens? Please say Yes or No."


def _split_listed_names(text: str) -> list[str]:
    raw = re.sub(r"\s+", " ", (text or "").strip())
    raw = re.sub(r"\s+and\s+", ", ", raw, flags=re.I)
    names: list[str] = []
    for part in raw.split(","):
        label = part.strip(" .?")
        if not label or len(label.split()) > 4:
            continue
        if re.search(
            r"citizen|immigration|national|everyone|household|yes|no",
            label,
            re.I,
        ):
            continue
        if re.match(r"^[A-Za-z][A-Za-z .'-]{0,40}$", label):
            names.append(label)
    return names


async def _citizenship_household_names(page: Page, session: Any) -> list[str]:
    """Names for the citizenship question, from the live page, then saved household data."""
    members = await _scrape_immigration_members(page)
    from_cards = [
        str(row.get("name") or "").strip()
        for row in members
        if str(row.get("name") or "").strip()
    ]
    if from_cards:
        return from_cards
    heading = ""
    try:
        heading = await page.evaluate(
            """() => {
              const text = ((document.body && document.body.innerText) || '')
                .replace(/\\s+/g, ' ');
              const match = text.match(
                /\\b(?:are|is)\\s+(.+?)\\s+all\\s+u\\.?s\\.?\\s+citizens\\b/i
              );
              return match ? match[1] : '';
            }"""
        )
    except Exception:
        heading = ""
    listed = _split_listed_names(str(heading or ""))
    if listed:
        return listed
    saved = [
        part.strip()
        for part in str((getattr(session, "filled", None) or {}).get("household_names") or "").split("|")
        if part.strip()
    ]
    return saved


def _join_member_names(names: list[str]) -> str:
    clean = [name.strip() for name in names if name and name.strip()]
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return f"{clean[0]} and {clean[1]}"
    return ", ".join(clean[:-1]) + ", and " + clean[-1]


def _coverage_question(names: list[str]) -> str:
    """Ask the coverage Yes/No question with the people actually listed."""
    who = _join_member_names(names)
    if not who:
        return APP_QUESTIONS["app_insurance"]
    if len([name for name in names if name and name.strip()]) == 1:
        return (
            f"Does {who} have health insurance or health coverage? "
            "Please say Yes or No."
        )
    return (
        f"Do {who} have health insurance or health coverage? "
        "Please say Yes or No."
    )


def _who_has_coverage_question(names: list[str]) -> str:
    who = _join_member_names(names)
    if not who:
        return (
            "Who has coverage? I don't see the member list yet. "
            "Please say Yes again if the list is not open."
        )
    return f"Who has coverage? The household members are {who}."


def _insurance_coverage_question_for(name: str) -> str:
    label = _clean_member_display_name(name)
    return (
        f"What type of coverage does {label} have — Medicare, Medicaid, "
        "health insurance through a job, TRICARE, VA Health Care, private "
        "insurance or ACA Marketplace, or Other? You can name more than one."
    )


def _map_coverage_types(lowered: str) -> list[str]:
    """Map spoken coverage phrases to visible Pointcare option labels."""
    mapping = (
        ("Medicare", ("medicare",)),
        ("Medicaid", ("medicaid", "medi-cal", "medical")),
        (
            "Health insurance through a job (employer-sponsored)",
            ("through a job", "employer", "job", "work insurance", "work coverage"),
        ),
        ("TRICARE", ("tricare", "tri-care", "military health")),
        (
            "VA Health Care Program",
            ("va health", "veterans", "v a health", "va care"),
        ),
        (
            "Private insurance or ACA Marketplace",
            (
                "private",
                "marketplace",
                "aca",
                "obama",
                "exchange",
            ),
        ),
        ("Other", ("other", "something else")),
    )
    hits: list[str] = []
    for label, cues in mapping:
        if any(cue in lowered for cue in cues):
            if label not in hits:
                hits.append(label)
    return hits


async def _scrape_dob_cards(page: Page) -> list[dict[str, Any]]:
    """Inspect household DOB cards: name, saved DOB, and whether Add is needed.

    Pointcare often puts \"Date of birth\" only in the page heading, while each
    member row shows a name plus Add or a saved date — so rows without the DOB
    phrase in-card must still be collected when the page is a DOB review.
    """
    try:
        rows = await page.evaluate(
            """() => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const bodyLow = norm(document.body && document.body.innerText || '')
                .toLowerCase();
              const pageIsDob =
                bodyLow.includes('date of birth') ||
                /\\bdob\\b/.test(bodyLow) ||
                bodyLow.includes('add date of birth');
              const cards = [];
              const nodes = [...document.querySelectorAll(
                '[class*="card"], [class*="Card"], [class*="member"], section, article, li, div'
              )];
              const seen = new Set();
              const isNameLine = (line) => {
                let t = (line || '').replace(/\\s+/g, ' ').trim();
                t = t.replace(/\\s+\\b(add|edit|pencil|save|next|continue)\\b\\.?$/i, '').trim();
                return /^[A-Za-z][A-Za-z .'-]{0,40}$/.test(t) &&
                  t.split(' ').length <= 4 &&
                  !/^(date of birth|dob|add|edit|save|cancel|back|exit|next|continue|yes|no|pencil|mm|dd|yyyy)$/i
                    .test(t);
              };
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (r.width < 40 || r.height < 20 || r.height > 420) continue;
                const lines = (el.innerText || '').split('\\n').map(norm).filter(Boolean);
                const text = lines.join(' ');
                if (!text || text.length > 420) continue;
                const low = text.toLowerCase();
                const hasDobCue =
                  low.includes('date of birth') ||
                  /\\bdob\\b/.test(low) ||
                  low.includes('add date') ||
                  low.includes('needs date');
                const hasAdd = /(?:^|\\n|\\s)add(?:\\s|$)/i.test(text) ||
                  [...el.querySelectorAll('button, [role=button], a')].some((b) => {
                    const t = norm(b.innerText || b.textContent || '').toLowerCase();
                    return t === 'add' || t.startsWith('add ');
                  });
                const dobMatch = text.match(
                  /(?:date of birth|dob)\\s*[:\\-]?\\s*(\\d{1,2}[\\/-]\\d{1,2}[\\/-]\\d{2,4})/i
                );
                const dobAlt = text.match(/\\b(\\d{1,2}[\\/-]\\d{1,2}[\\/-]\\d{4})\\b/);
                const dob = (dobMatch && dobMatch[1]) || (dobAlt && dobAlt[1]) || '';
                const editorOpen = [...el.querySelectorAll('input, textarea')].some((input) => {
                  const hint = norm(
                    (input.placeholder || '') + ' ' + (input.getAttribute('aria-label') || '')
                  ).toLowerCase();
                  const box = input.getBoundingClientRect();
                  return box.width > 2 && box.height > 2 && /\\b(mm|dd|yyyy|month|day|year)\\b/.test(hint);
                });
                // On a DOB page, name + Add/date/open editor is enough.
                if (!hasDobCue && !(pageIsDob && (hasAdd || dob || editorOpen))) continue;
                let name = '';
                for (const line of lines.slice(0, 5)) {
                  if (/date of birth|\\bdob\\b|add |needs |pencil|edit|save|next|continue/i
                    .test(line)) {
                    continue;
                  }
                  if (isNameLine(line)) {
                    name = line.replace(/\\s+\\b(add|edit|pencil|save|next|continue)\\b\\.?$/i, '').trim();
                    break;
                  }
                }
                if (!name) continue;
                const key = name.toLowerCase();
                if (seen.has(key)) continue;
                seen.add(key);
                const needsAdd = Boolean(
                  ((hasAdd || editorOpen) && !dob) ||
                  /needs date|add date of birth|missing/i.test(text)
                );
                cards.push({
                  name,
                  dob: dob || '',
                  needs_add: needsAdd,
                  editor_open: Boolean(editorOpen && !dob),
                  index: cards.length,
                });
              }
              return cards;
            }"""
        )
        if isinstance(rows, list):
            from .live_form import _is_plausible_member_label

            parsed = [
                row
                for row in rows
                if isinstance(row, dict)
                and row.get("name")
                and _is_plausible_member_label(str(row.get("name") or ""))
            ]
            if parsed:
                return parsed
            # Real empty DOM scrape — try body text before giving up.
            return await _scrape_dob_cards_from_body(page)
        return []
    except Exception:
        logger.exception("scrape_dob_cards failed")
        try:
            return await _scrape_dob_cards_from_body(page)
        except Exception:
            return []
    return []


async def _scrape_dob_cards_from_body(page: Page) -> list[dict[str, Any]]:
    """Parse DOB member rows from visible body text when card DOM scrape misses."""
    try:
        raw = await page.locator("body").inner_text()
    except Exception:
        return []
    if not isinstance(raw, str) or not raw.strip():
        return []
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln]
    body_low = " ".join(lines).lower()
    if not (
        "date of birth" in body_low
        or re.search(r"\bdob\b", body_low)
        or "add date" in body_low
    ):
        return []
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    name_re = re.compile(r"^[A-Za-z][A-Za-z .'-]{0,40}$")
    skip_re = re.compile(
        r"date of birth|\bdob\b|^add$|^edit$|^save$|^cancel$|^back$|^exit$|"
        r"^next$|^continue$|^mm$|^dd$|^yyyy$|please confirm|looks correct|household",
        re.I,
    )
    for i, line in enumerate(lines):
        if not name_re.match(line) or skip_re.search(line):
            continue
        if len(line.split()) > 4:
            continue
        # Only consume lines until the next member-looking name.
        chunk: list[str] = []
        for follow in lines[i + 1 : i + 4]:
            if name_re.match(follow) and not skip_re.search(follow) and len(
                follow.split()
            ) <= 4:
                break
            chunk.append(follow)
        window = " ".join(chunk)
        dob_match = re.search(
            r"(?:date of birth|dob)\s*[:\-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            window,
            re.I,
        )
        dob_alt = re.search(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{4})\b", window)
        dob = (dob_match.group(1) if dob_match else "") or (
            dob_alt.group(1) if dob_alt else ""
        )
        has_add = any(re.fullmatch(r"add", part.strip(), re.I) for part in chunk)
        editor_open = any(
            re.fullmatch(r"(cancel|save|mm|dd|yyyy)", part.strip(), re.I)
            for part in chunk
        )
        # A heading such as "Please add and confirm" is not an Add button.
        if not (has_add or dob or (editor_open and not dob)):
            continue
        from .live_form import _is_plausible_member_label

        if not _is_plausible_member_label(line):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        cards.append(
            {
                "name": line,
                "dob": dob,
                "needs_add": bool((has_add or editor_open) and not dob),
                "editor_open": bool(editor_open and not dob),
                "index": len(cards),
            }
        )
    return cards


_IMMIGRATION_MEMBER_JS = r"""
() => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const isMemberStatus = (t) => {
    const n = norm(t).toLowerCase().replace(/\s*\(.*?\)\s*/g, ' ').replace(/\s+/g, ' ').trim();
    return n === 'u.s. citizen' || n === 'us citizen'
      || n === 'u.s. national' || n === 'us national'
      || n === 'satisfactory immigration status'
      || n === 'prefer not to answer';
  };
  const labelFor = (radio) => {
    const own = norm(radio.getAttribute('aria-label') || radio.innerText || radio.textContent || '');
    if (isMemberStatus(own)) return own;
    if (radio.id) {
      const lab = document.querySelector('label[for="' + radio.id.replace(/"/g, '') + '"]');
      if (lab && isMemberStatus(lab.innerText || lab.textContent || '')) {
        return norm(lab.innerText || lab.textContent || '');
      }
    }
    const wrap = radio.closest('label');
    if (wrap && isMemberStatus(wrap.innerText || wrap.textContent || '')) {
      return norm(wrap.innerText || wrap.textContent || '');
    }
    let sib = radio.nextSibling;
    while (sib) {
      const t = norm(sib.textContent || '');
      if (isMemberStatus(t)) return t;
      if (t) break;
      sib = sib.nextSibling;
    }
    return '';
  };
  const isNameLine = (line) => {
    const t = norm(line);
    if (!t || isMemberStatus(t)) return false;
    if (/citizen|immigration|national|prefer not|satisfactory|everyone listed|some or all/i.test(t)) {
      return false;
    }
    if (/^(yes|no)\b/i.test(t)) return false;
    return /^[A-Za-z][A-Za-z .'-]{0,40}$/.test(t) && t.split(' ').length <= 4;
  };
  const namesIn = (el) => {
    const lines = (el.innerText || el.textContent || '').split('\n').map(norm).filter(Boolean);
    const found = [];
    for (const line of lines) {
      if (!isNameLine(line)) continue;
      if (!found.some((item) => item.toLowerCase() === line.toLowerCase())) found.push(line);
    }
    return found;
  };
  const radios = [...document.querySelectorAll('input[type=radio], [role=radio]')];
  const cards = [];
  const seen = new Set();
  for (const radio of radios) {
    if (!isMemberStatus(labelFor(radio))) continue;
    let cur = radio.parentElement;
    let card = null;
    for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++) {
      const group = [...cur.querySelectorAll('input[type=radio], [role=radio]')]
        .filter((item) => isMemberStatus(labelFor(item)));
      const names = namesIn(cur);
      const groupNames = new Set(group.map((item) => item.getAttribute('name') || ''));
      if (names.length === 1 && group.length >= 1 && group.length <= 6 && groupNames.size <= 1) {
        card = { el: cur, name: names[0], radios: group };
        break;
      }
      cur = cur.parentElement;
    }
    if (!card) continue;
    const key = card.name.toLowerCase() + ':' + card.radios.map((item) => labelFor(item)).join('|');
    if (seen.has(key)) continue;
    seen.add(key);
    let selected = '';
    for (const item of card.radios) {
      const on = item.checked === true || item.getAttribute('aria-checked') === 'true';
      if (on) {
        selected = labelFor(item);
        break;
      }
    }
    cards.push({
      name: card.name,
      selected,
      index: cards.length,
      options: card.radios.map((item) => labelFor(item)).filter(Boolean),
    });
  }
  return cards;
}
"""


async def _scrape_immigration_members(page: Page) -> list[dict[str, Any]]:
    """Read one radio group per household-member card. Preselected values are reported, not confirmed."""
    try:
        rows = await page.evaluate(_IMMIGRATION_MEMBER_JS)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict) and row.get("name")]
    except Exception:
        logger.exception("scrape_immigration_members failed")
    return []


async def _set_member_immigration_status(
    page: Page, member_name: str, status: str
) -> bool:
    """Select ``status`` on that member's own radio group.

    A value that is already selected still receives a change event so
    Pointcare can enable Next. If that click toggles the radio off, it is
    clicked again and the original status is restored. Other members are
    not touched.
    """
    try:
        ok = await page.evaluate(
            """({ memberName, status }) => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const base = (s) => norm(s).toLowerCase()
                .replace(/\\s*\\(.*?\\)\\s*/g, ' ').replace(/\\s+/g, ' ').trim();
              const isMemberStatus = (t) => {
                const n = base(t);
                return n === 'u.s. citizen' || n === 'us citizen'
                  || n === 'u.s. national' || n === 'us national'
                  || n === 'satisfactory immigration status'
                  || n === 'prefer not to answer';
              };
              const labelFor = (radio) => {
                const own = norm(radio.getAttribute('aria-label') || radio.innerText || radio.textContent || '');
                if (isMemberStatus(own)) return own;
                if (radio.id) {
                  const lab = document.querySelector('label[for="' + radio.id.replace(/"/g, '') + '"]');
                  if (lab && isMemberStatus(lab.innerText || lab.textContent || '')) {
                    return norm(lab.innerText || lab.textContent || '');
                  }
                }
                const wrap = radio.closest('label');
                if (wrap && isMemberStatus(wrap.innerText || wrap.textContent || '')) {
                  return norm(wrap.innerText || wrap.textContent || '');
                }
                let sib = radio.nextSibling;
                while (sib) {
                  const t = norm(sib.textContent || '');
                  if (isMemberStatus(t)) return t;
                  if (t) break;
                  sib = sib.nextSibling;
                }
                return '';
              };
              const isNameLine = (line) => {
                const t = norm(line);
                if (!t || isMemberStatus(t)) return false;
                if (/citizen|immigration|national|prefer not|satisfactory|everyone listed|some or all/i.test(t)) return false;
                if (/^(yes|no)\\b/i.test(t)) return false;
                return /^[A-Za-z][A-Za-z .'-]{0,40}$/.test(t) && t.split(' ').length <= 4;
              };
              const namesIn = (el) => {
                const lines = (el.innerText || el.textContent || '').split('\\n').map(norm).filter(Boolean);
                const found = [];
                for (const line of lines) {
                  if (!isNameLine(line)) continue;
                  if (!found.some((item) => item.toLowerCase() === line.toLowerCase())) found.push(line);
                }
                return found;
              };
              const wantName = base(memberName);
              const wantStatus = base(status);
              if (!wantName || !wantStatus) return false;
              const hits = [];
              const seen = new Set();
              for (const radio of document.querySelectorAll('input[type=radio], [role=radio]')) {
                if (!isMemberStatus(labelFor(radio))) continue;
                let cur = radio.parentElement;
                let card = null;
                for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++) {
                  const group = [...cur.querySelectorAll('input[type=radio], [role=radio]')]
                    .filter((item) => isMemberStatus(labelFor(item)));
                  const names = namesIn(cur);
                  const groupNames = new Set(group.map((item) => item.getAttribute('name') || ''));
                  if (names.length === 1 && group.length >= 1 && group.length <= 6 && groupNames.size <= 1) {
                    card = { el: cur, name: names[0], radios: group };
                    break;
                  }
                  cur = cur.parentElement;
                }
                if (!card || base(card.name) !== wantName) continue;
                const key = card.name.toLowerCase();
                if (seen.has(key)) continue;
                seen.add(key);
                hits.push(card);
              }
              if (hits.length !== 1) return false;
              const card = hits[0];
              const readOn = (radio) => radio.checked === true || radio.getAttribute('aria-checked') === 'true';
              const notify = (radio) => {
                radio.dispatchEvent(new Event('input', { bubbles: true }));
                radio.dispatchEvent(new Event('change', { bubbles: true }));
              };
              for (const radio of card.radios) {
                if (base(labelFor(radio)) !== wantStatus) continue;
                const wrap = radio.closest('label');
                const target = wrap && base(wrap.innerText || wrap.textContent || '') === wantStatus
                  ? wrap
                  : radio;
                if (readOn(radio)) {
                  target.click();
                  if (!readOn(radio)) target.click();
                  if (!readOn(radio) && radio.tagName === 'INPUT') {
                    radio.checked = true;
                    notify(radio);
                  } else {
                    notify(radio);
                  }
                  return readOn(radio);
                }
                target.click();
                if (!readOn(radio) && radio.tagName === 'INPUT') {
                  radio.checked = true;
                  notify(radio);
                }
                return readOn(radio);
              }
              return false;
            }""",
            {"memberName": member_name, "status": status},
        )
        return bool(ok)
    except Exception:
        logger.exception(
            "set_member_immigration_status failed member=%r status=%r",
            member_name,
            status,
        )
        return False


async def _verify_member_immigration_status(
    page: Page, member_name: str, status: str
) -> bool:
    """True when the named member card shows ``status`` selected."""
    want_name = (member_name or "").strip().lower()
    want_status = (status or "").strip().lower()
    if not want_name or not want_status:
        return False
    members = await _scrape_immigration_members(page)
    matches = [
        row
        for row in members
        if str(row.get("name") or "").strip().lower() == want_name
    ]
    if len(matches) != 1:
        return False
    return _same_immigration_status(str(matches[0].get("selected") or ""), status)


async def _scrape_insurance_members(page: Page) -> list[dict[str, Any]]:
    """Inspect expanded household-member coverage checkboxes and types."""
    try:
        rows = await page.evaluate(
            """() => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const coverageCues = [
                'medicare', 'medicaid', 'tricare', 'va health', 'private',
                'employer', 'through a job', 'marketplace', 'other',
              ];
              const out = [];
              const nodes = [...document.querySelectorAll(
                '[class*="card"], [class*="Card"], [class*="member"], section, article, li, div, fieldset, label'
              )];
              const seen = new Set();
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (r.width < 40 || r.height < 24 || r.height > 720) continue;
                const text = norm(el.innerText || el.textContent || '');
                if (!text || text.length > 700) continue;
                const low = text.toLowerCase();
                const hasCoverage = coverageCues.some((c) => low.includes(c));
                const hasCheckbox = !!el.querySelector(
                  'input[type=checkbox], [role=checkbox]'
                );
                if (!(hasCoverage || hasCheckbox)) continue;
                const lines = text.split('\\n').map(norm).filter(Boolean);
                let name = '';
                for (const line of lines.slice(0, 6)) {
                  const ll = line.toLowerCase();
                  if (
                    /medicare|medicaid|tricare|coverage|insurance|employer|private|other|va health|marketplace|yes|no/.test(ll)
                  ) {
                    continue;
                  }
                  if (/^[A-Za-z][A-Za-z .'-]{1,40}$/.test(line) && line.split(' ').length <= 4) {
                    name = line;
                    break;
                  }
                }
                if (!name) continue;
                const key = name.toLowerCase();
                if (seen.has(key)) continue;
                seen.add(key);
                let checked = false;
                const boxes = [...el.querySelectorAll(
                  'input[type=checkbox], [role=checkbox]'
                )];
                for (const box of boxes) {
                  if (
                    box.checked === true ||
                    box.getAttribute('aria-checked') === 'true' ||
                    (box.className || '').toLowerCase().includes('checked')
                  ) {
                    checked = true;
                    break;
                  }
                }
                const types = [];
                for (const cue of [
                  'Medicare', 'Medicaid',
                  'Health insurance through a job (employer-sponsored)',
                  'TRICARE', 'VA Health Care Program',
                  'Private insurance or ACA Marketplace', 'Other',
                ]) {
                  if (low.includes(cue.toLowerCase().slice(0, 12))) {
                    // detect selected coverage controls under this card
                  }
                }
                const selectedTypes = [];
                const controls = [...el.querySelectorAll(
                  'input[type=checkbox], [role=checkbox], label, button, [role=button]'
                )];
                for (const ctrl of controls) {
                  const label = norm(
                    ctrl.getAttribute('aria-label') ||
                    ctrl.innerText ||
                    ctrl.textContent ||
                    ''
                  );
                  if (!label || label.length > 90) continue;
                  const ll = label.toLowerCase();
                  const isCoverage = coverageCues.some((c) => ll.includes(c));
                  if (!isCoverage) continue;
                  const on =
                    ctrl.checked === true ||
                    ctrl.getAttribute('aria-checked') === 'true' ||
                    (ctrl.className || '').toLowerCase().includes('checked') ||
                    (ctrl.className || '').toLowerCase().includes('selected');
                  if (on) selectedTypes.push(label);
                }
                out.push({
                  name,
                  checked,
                  selected_types: selectedTypes,
                  index: out.length,
                });
              }
              return out;
            }"""
        )
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict) and row.get("name")]
    except Exception:
        logger.exception("scrape_insurance_members failed")
    return []


def _coverage_member_rows_js() -> str:
    """Shared finder for the Who-has-coverage checkbox rows. Not page-wide."""
    return r"""
      const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
      const isCoverageType = (t) => /medicare|medicaid|tricare|va health|employer|marketplace|private insurance|\bother\b/i.test(norm(t));
      const isName = (t) => {
        const n = norm(t);
        if (!n || isCoverageType(n)) return false;
        if (/coverage|insurance|citizen|yes|no|next|who has/i.test(n)) return false;
        return /^[A-Za-z][A-Za-z .'-]{0,40}$/.test(n) && n.split(' ').length <= 4;
      };
      const labelFor = (box) => {
        if (box.id) {
          const lab = document.querySelector('label[for="' + box.id.replace(/"/g, '') + '"]');
          if (lab && isName(lab.innerText || lab.textContent || '')) {
            return norm(lab.innerText || lab.textContent || '');
          }
        }
        const wrap = box.closest('label');
        if (wrap && isName(wrap.innerText || wrap.textContent || '')) {
          return norm(wrap.innerText || wrap.textContent || '');
        }
        const aria = norm(box.getAttribute('aria-label') || '');
        if (isName(aria)) return aria;
        let sib = box.nextSibling;
        while (sib) {
          const t = norm(sib.textContent || '');
          if (isName(t)) return t;
          if (t) break;
          sib = sib.nextSibling;
        }
        return '';
      };
      const isOn = (box) => box.checked === true || box.getAttribute('aria-checked') === 'true';
      const coverageRoot = () => {
        const nodes = [...document.querySelectorAll('h1,h2,h3,h4,p,div,span,legend,label,section')];
        const hits = [];
        for (const el of nodes) {
          const t = norm(el.innerText || el.textContent || '');
          if (!t || t.length > 80) continue;
          if (!/who has coverage/i.test(t)) continue;
          hits.push({ el, len: t.length });
        }
        hits.sort((a, b) => a.len - b.len);
        if (!hits.length) return null;
        let cur = hits[0].el;
        for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++) {
          if (cur.querySelector('input[type=checkbox], [role=checkbox]')) return cur;
          cur = cur.parentElement;
        }
        return null;
      };
      const rows = () => {
        const root = coverageRoot();
        if (!root) return [];
        const out = [];
        for (const box of root.querySelectorAll('input[type=checkbox], [role=checkbox]')) {
          const name = labelFor(box);
          if (!name) continue;
          out.push({ name, checked: isOn(box), box, index: out.length });
        }
        return out;
      };
    """


async def _scrape_coverage_members(page: Page) -> list[dict[str, Any]]:
    """Member checkboxes inside Who has coverage. Names come from those rows only."""
    try:
        rows = await page.evaluate(
            "() => {\n"
            + _coverage_member_rows_js()
            + """
              return rows().map((row) => ({
                name: row.name,
                checked: row.checked,
                selected_types: [],
                index: row.index,
              }));
            }"""
        )
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict) and row.get("name")]
    except Exception:
        logger.exception("scrape_coverage_members failed")
    return []


async def _coverage_household_names(page: Page, session: Any) -> list[str]:
    """Names for the coverage question, from the live list, then saved household data."""
    members = await _scrape_coverage_members(page)
    from_boxes = [
        str(row.get("name") or "").strip()
        for row in members
        if str(row.get("name") or "").strip()
    ]
    if from_boxes:
        return from_boxes
    heading = ""
    try:
        heading = await page.evaluate(
            """() => {
              const text = ((document.body && document.body.innerText) || '')
                .replace(/\\s+/g, ' ');
              const match = text.match(
                /\\b(?:do|does)\\s+(.+?)\\s+have\\s+health\\s+insurance\\s+or\\s+health\\s+coverage\\b/i
              );
              return match ? match[1] : '';
            }"""
        )
    except Exception:
        heading = ""
    listed = _split_listed_names(str(heading or ""))
    if listed:
        return listed
    return [
        part.strip()
        for part in str(
            (getattr(session, "filled", None) or {}).get("household_names") or ""
        ).split("|")
        if part.strip()
    ]


async def _set_member_insurance_checked(
    page: Page, member_name: str, checked: bool = True
) -> bool:
    """Check one Who-has-coverage box. A box that is already selected is not clicked.

    The return value is the checkbox state after the action, not the click itself.
    """
    try:
        ok = await page.evaluate(
            """({ memberName, wantChecked }) => {
            """
            + _coverage_member_rows_js()
            + """
              const want = norm(memberName).toLowerCase();
              if (!want) return false;
              const hits = rows().filter((row) => row.name.toLowerCase() === want);
              if (hits.length !== 1) return false;
              const row = hits[0];
              if (Boolean(row.checked) === Boolean(wantChecked)) return true;
              const box = row.box;
              const wrap = box.closest('label');
              (wrap || box).click();
              if (box.tagName === 'INPUT' && box.checked !== Boolean(wantChecked)) {
                box.checked = Boolean(wantChecked);
                box.dispatchEvent(new Event('input', { bubbles: true }));
                box.dispatchEvent(new Event('change', { bubbles: true }));
              }
              const now = box.checked === true || box.getAttribute('aria-checked') === 'true';
              return now === Boolean(wantChecked);
            }""",
            {"memberName": member_name, "wantChecked": checked},
        )
        return bool(ok)
    except Exception:
        logger.exception(
            "set_member_insurance_checked failed member=%r", member_name
        )
        return False


async def _verify_coverage_member_checked(page: Page, member_name: str) -> bool:
    """True only when that member's own coverage checkbox is checked."""
    want = (member_name or "").strip().lower()
    if not want:
        return False
    rows = await _scrape_coverage_members(page)
    matches = [
        row
        for row in rows
        if str(row.get("name") or "").strip().lower() == want
    ]
    if len(matches) != 1:
        return False
    return bool(matches[0].get("checked"))


async def _set_member_coverage_type(
    page: Page, member_name: str, coverage: str
) -> bool:
    """Select a coverage type only within the named member's insurance card."""
    try:
        ok = await page.evaluate(
            """({ memberName, coverage }) => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const wantName = norm(memberName);
              const wantCov = norm(coverage);
              if (!wantName || !wantCov) return false;
              const nodes = [...document.querySelectorAll(
                '[class*="card"], [class*="Card"], [class*="member"], section, article, li, div, fieldset'
              )];
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (r.width < 40 || r.height < 40) continue;
                const text = norm(el.innerText || el.textContent || '');
                if (!text.includes(wantName)) continue;
                if (!(
                  text.includes('medicare') ||
                  text.includes('medicaid') ||
                  text.includes('coverage') ||
                  text.includes('insurance') ||
                  text.includes('tricare')
                )) {
                  continue;
                }
                const controls = [...el.querySelectorAll(
                  'label, button, [role=button], [role=checkbox], input[type=checkbox], span, div'
                )];
                for (const ctrl of controls) {
                  const cr = ctrl.getBoundingClientRect();
                  if (cr.width < 6 || cr.height < 6) continue;
                  const label = norm(
                    ctrl.getAttribute('aria-label') ||
                    ctrl.innerText ||
                    ctrl.textContent ||
                    ''
                  );
                  if (!label || label.length > 100) continue;
                  if (!(label === wantCov || label.includes(wantCov) || wantCov.includes(label))) {
                    continue;
                  }
                  const on =
                    ctrl.checked === true ||
                    ctrl.getAttribute('aria-checked') === 'true' ||
                    (ctrl.className || '').toLowerCase().includes('checked') ||
                    (ctrl.className || '').toLowerCase().includes('selected');
                  // Never toggle an already-selected coverage off.
                  if (on) return true;
                  ctrl.click();
                  return true;
                }
              }
              return false;
            }""",
            {"memberName": member_name, "coverage": coverage},
        )
        return bool(ok)
    except Exception:
        logger.exception(
            "set_member_coverage_type failed member=%r coverage=%r",
            member_name,
            coverage,
        )
        return False


async def _verify_member_coverage_types(
    page: Page, member_name: str, expected: list[str]
) -> bool:
    """True when each expected coverage type is selected under the member."""
    want = (member_name or "").strip().lower()
    if not want or not expected:
        return False
    rows = await _scrape_insurance_members(page)
    for row in rows:
        name = str(row.get("name") or "").strip().lower()
        if not (name == want or want in name or name in want):
            continue
        selected = [
            str(t).strip().lower()
            for t in (row.get("selected_types") or [])
            if str(t).strip()
        ]
        for exp in expected:
            el = exp.strip().lower()
            if not any(el in s or s in el for s in selected):
                return False
        return True
    return False


async def _set_circumstance_member(
    page: Page, member_name: str, circumstance: str
) -> bool:
    """Select ``member_name`` only inside the named circumstance section.

    Prevents a Cervical-cancer member click from toggling Blind (or any other
    section that appears earlier on the page).
    """
    try:
        ok = await page.evaluate(
            """({ memberName, circumstance }) => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const wantMember = norm(memberName);
              const wantCond = norm(circumstance);
              if (!wantMember) return false;
              const nodes = [...document.querySelectorAll(
                '[class*="card"], [class*="Card"], section, article, fieldset, li, div'
              )];
              // Prefer the smallest card that contains the circumstance text.
              const candidates = [];
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (r.width < 40 || r.height < 30 || r.height > 900) continue;
                const text = norm(el.innerText || el.textContent || '');
                if (!text) continue;
                if (wantCond && !text.includes(wantCond.slice(0, Math.min(24, wantCond.length)))) {
                  // Also accept key tokens (veteran, student, indian, daily).
                  const tokens = wantCond.split(' ').filter((t) => t.length > 4);
                  if (!tokens.some((t) => text.includes(t))) continue;
                }
                if (!wantCond && !/apply|who does|member|household/.test(text)) continue;
                candidates.push({ el, area: r.width * r.height, text });
              }
              candidates.sort((a, b) => a.area - b.area);
              const scopes = candidates.length ? candidates : [{ el: document.body, area: 0, text: '' }];
              for (const scope of scopes) {
                const controls = [...scope.el.querySelectorAll(
                  'input[type=checkbox], [role=checkbox], label, button, [role=button], span, div'
                )];
                for (const ctrl of controls) {
                  const cr = ctrl.getBoundingClientRect();
                  if (cr.width < 4 || cr.height < 4) continue;
                  const label = norm(
                    ctrl.getAttribute('aria-label') ||
                    ctrl.innerText ||
                    ctrl.textContent ||
                    ''
                  );
                  if (!label || label.length > 60) continue;
                  if (!(
                    label === wantMember ||
                    label.includes(wantMember) ||
                    wantMember.includes(label)
                  )) {
                    continue;
                  }
                  // Avoid matching the circumstance title itself.
                  if (wantCond && (label.includes(wantCond) || wantCond.includes(label))) {
                    continue;
                  }
                  const on =
                    ctrl.checked === true ||
                    ctrl.getAttribute('aria-checked') === 'true' ||
                    (ctrl.className || '').toLowerCase().includes('checked') ||
                    (ctrl.className || '').toLowerCase().includes('selected');
                  if (on) return true;
                  ctrl.click();
                  return true;
                }
              }
              return false;
            }""",
            {"memberName": member_name, "circumstance": circumstance or ""},
        )
        return bool(ok)
    except Exception:
        logger.exception(
            "set_circumstance_member failed member=%r circumstance=%r",
            member_name,
            circumstance,
        )
        return False


def _member_address_card_js() -> str:
    """JS helpers for the smallest card that belongs to one household member."""
    return """
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
      const low = (s) => norm(s).toLowerCase().replace(/\\./g, '');
      const isNameLine = (line) => {
        const t = norm(line);
        if (!/^[A-Za-z][A-Za-z .'-]{0,40}$/.test(t) || t.split(' ').length > 4) return false;
        const n = low(t);
        if (/address|apartment|mailing|home|same|unit|building|pencil|edit|save|cancel|add|next|continue|street|po box/.test(n)) {
          return false;
        }
        return true;
      };
      const visible = (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 2 && r.height > 2 && !el.disabled;
      };
      const findAddressCard = (memberName) => {
        const want = low(memberName);
        if (!want) return null;
        const nodes = [...document.querySelectorAll(
          'article, section, li, [class*="card"], [class*="Card"], [class*="member"], div'
        )];
        let best = null;
        let bestArea = Infinity;
        for (const el of nodes) {
          const r = el.getBoundingClientRect();
          if (r.width < 40 || r.height < 24 || r.height > 900) continue;
          const lines = (el.innerText || '').split('\\n').map(norm).filter(Boolean);
          const names = lines.filter(isNameLine);
          if (names.length !== 1 || low(names[0]) !== want) continue;
          const area = r.width * r.height;
          if (area < bestArea) {
            best = el;
            bestArea = area;
          }
        }
        return best;
      };
      const labelFor = (root, input) => {
        if (input.id) {
          const lab = root.querySelector('label[for="' + CSS.escape(input.id) + '"]');
          if (lab) return low(lab.innerText || lab.textContent || '');
        }
        const wrap = input.closest('label');
        if (wrap) return low(wrap.innerText || wrap.textContent || '');
        const prev = input.previousElementSibling;
        if (prev) return low(prev.innerText || prev.textContent || '');
        return low(
          (input.getAttribute('aria-label') || '') + ' ' +
          (input.placeholder || '') + ' ' + (input.name || '')
        );
      };
      const cardFields = (root) => {
        const inputs = [...root.querySelectorAll('input, textarea')].filter((el) => {
          const t = (el.type || 'text').toLowerCase();
          return visible(el) && t !== 'checkbox' && t !== 'radio' && t !== 'hidden' && t !== 'button' && t !== 'submit';
        });
        const fields = { home: null, homeUnit: null, mailing: null, mailingUnit: null };
        inputs.forEach((input, index) => {
          const label = labelFor(root, input);
          let key = '';
          if (label.includes('po box') || (label.includes('apartment') && label.includes('mailing'))) {
            key = 'mailingUnit';
          } else if (label.includes('apartment') || label.includes('unit') || label.includes('building')) {
            key = fields.homeUnit ? 'mailingUnit' : 'homeUnit';
          } else if (label.includes('mailing')) {
            key = 'mailing';
          } else if (label.includes('home')) {
            key = 'home';
          } else if (inputs.length >= 4) {
            key = ['home', 'homeUnit', 'mailing', 'mailingUnit'][index] || '';
          } else if (inputs.length === 2) {
            key = index === 0 ? 'home' : 'mailing';
          }
          if (key && !fields[key]) fields[key] = input;
        });
        return fields;
      };
      const boxOn = (root, phrase) => {
        const boxes = [...root.querySelectorAll('input[type=checkbox], [role=checkbox]')];
        for (const box of boxes) {
          let label = '';
          if (box.id) {
            const lab = root.querySelector('label[for="' + CSS.escape(box.id) + '"]');
            if (lab) label = low(lab.innerText || lab.textContent || '');
          }
          if (!label) {
            const host = box.closest('label') || box.parentElement;
            label = low((host && (host.innerText || host.textContent)) || box.getAttribute('aria-label') || '');
          }
          if (!label.includes(phrase)) continue;
          return box.checked === true || box.getAttribute('aria-checked') === 'true';
        }
        return false;
      };
    """


async def _scrape_address_cards(page: Page) -> list[dict[str, Any]]:
    """Read each member card: saved lines, same-address text, and no-address boxes."""
    try:
        rows = await page.evaluate(
            "() => {"
            + _member_address_card_js()
            + """
              const out = [];
              const seen = new Set();
              const nodes = [...document.querySelectorAll(
                'article, section, li, [class*="card"], [class*="Card"], [class*="member"], div'
              )];
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (r.width < 40 || r.height < 24 || r.height > 900) continue;
                const lines = (el.innerText || '').split('\\n').map(norm).filter(Boolean);
                const names = lines.filter(isNameLine);
                if (names.length !== 1) continue;
                const name = names[0];
                const key = low(name);
                if (seen.has(key)) continue;
                const text = lines.join(' ');
                if (!/address|mailing|home/.test(low(text)) && !/\\d{5}/.test(text)) continue;
                const fields = cardFields(el);
                const expanded = Boolean(fields.home || fields.mailing || fields.homeUnit || fields.mailingUnit);
                let home = fields.home ? (fields.home.value || '') : '';
                let mailing = fields.mailing ? (fields.mailing.value || '') : '';
                let homeUnit = fields.homeUnit ? (fields.homeUnit.value || '') : '';
                let mailingUnit = fields.mailingUnit ? (fields.mailingUnit.value || '') : '';
                let homeSame = false;
                let mailingSame = false;
                let section = '';
                for (const line of lines) {
                  const n = low(line);
                  if (n === key) continue;
                  if (n === 'home address') { section = 'home'; continue; }
                  if (n === 'mailing address') { section = 'mailing'; continue; }
                  if (n.includes('po box') && n.includes('apartment')) { section = 'mailing_unit'; continue; }
                  if (n.includes('apartment') || n === 'unit' || n.includes('unit, building')) {
                    section = section === 'mailing' || section === 'mailing_unit' ? 'mailing_unit' : 'home_unit';
                    continue;
                  }
                  if (n === 'same home address') { homeSame = true; if (!home) home = line; continue; }
                  if (n === 'same mailing address') { mailingSame = true; if (!mailing) mailing = line; continue; }
                  if (n === 'no home address' || n === 'no mailing address') continue;
                  if (/^needs\b/.test(n)) continue;
                  if (/^(edit|add|save|cancel|pencil|next|continue)$/.test(n)) continue;
                  if (section === 'home' && !home) home = line;
                  else if (section === 'mailing' && !mailing) mailing = line;
                  else if (section === 'home_unit' && !homeUnit) homeUnit = line;
                  else if (section === 'mailing_unit' && !mailingUnit) mailingUnit = line;
                }
                if (!home && !mailing) {
                  const street = lines.find((line) => /\\d/.test(line) && !/^same\\b/i.test(line) && low(line) !== key);
                  if (street && !expanded) home = street;
                }
                const noHome = boxOn(el, 'no home address') || (!expanded && lines.some((line) => low(line) === 'no home address'));
                const noMail = boxOn(el, 'no mailing address') || (!expanded && lines.some((line) => low(line) === 'no mailing address'));
                const homeOk = Boolean(noHome || homeSame || /\\d/.test(home));
                const mailOk = Boolean(noMail || mailingSame || /\\d/.test(mailing));
                seen.add(key);
                out.push({
                  name,
                  home: home || '',
                  mailing: mailing || '',
                  home_unit: homeUnit || '',
                  mailing_unit: mailingUnit || '',
                  home_same: homeSame,
                  mailing_same: mailingSame,
                  no_home: noHome,
                  no_mailing: noMail,
                  expanded,
                  needs_home: !homeOk,
                  needs_mailing: !mailOk,
                  needs_add: !homeOk || !mailOk,
                  index: out.length,
                });
              }
              return out;
            }"""
        )
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict) and row.get("name")]
    except Exception:
        logger.exception("scrape_address_cards failed")
    return []


def _split_street_and_unit(text: str) -> tuple[str, str]:
    """Separate a spoken unit or apartment from the street. The unit is never the street."""
    raw = re.sub(r"\s+", " ", (text or "").strip())
    match = re.search(
        r"\b(?:apartment|apt|unit|suite|ste|#)\s*#?\s*([A-Za-z0-9-]+)\b",
        raw,
        re.I,
    )
    unit = ""
    if match:
        unit = match.group(1)
        raw = (raw[: match.start()] + " " + raw[match.end() :]).strip(" ,")
    return re.sub(r"\s+", " ", raw).strip(" ,"), unit


def _looks_like_street(text: str) -> bool:
    raw = text or ""
    if re.search(r"\bp\.?\s*o\.?\s*box\s*\d+\b", raw, re.I):
        return True
    if re.search(r"\b\d+\s+[A-Za-z]", raw):
        return True
    return bool(
        re.search(
            r"\b(street|avenue|ave|road|rd|blvd|boulevard|drive|dr|lane|ln|court|way)\b",
            raw,
            re.I,
        )
    )


def _suggestion_matches(street: str, suggestions: list[str]) -> list[str]:
    """Return suggestions that match the street. Never choose one by list position."""
    number_match = re.search(r"\b\d+\b", street or "")
    number = number_match.group(0) if number_match else ""
    ignored = {
        "street",
        "avenue",
        "road",
        "drive",
        "lane",
        "blvd",
        "boulevard",
        "court",
        "place",
        "the",
    }
    words = [
        word.lower()
        for word in re.findall(r"[A-Za-z]{3,}", street or "")
        if word.lower() not in ignored
    ][:3]
    hits: list[str] = []
    for item in suggestions:
        lowered = item.lower()
        if number and not re.search(rf"\b{re.escape(number)}\b", lowered):
            continue
        if words and not all(re.search(rf"\b{re.escape(word)}", lowered) for word in words):
            continue
        if item not in hits:
            hits.append(item)
    return hits


def _address_confirmed_names(session: Any) -> list[str]:
    raw = session.filled.get("app_address_confirmed") or ""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item).strip()]


def _mark_address_confirmed(session: Any, name: str) -> None:
    names = _address_confirmed_names(session)
    if name and name.lower() not in {item.lower() for item in names}:
        names.append(name)
    session.filled["app_address_confirmed"] = json.dumps(names)


def _address_confirm_speech(card: dict[str, Any]) -> str:
    """Spoken confirmation. The street is not written to the diagnostic log."""
    name = str(card.get("name") or "this person")
    if card.get("needs_home") and card.get("needs_mailing"):
        return (
            f"For {name}, Pointcare still needs a home address and a mailing address. "
            "Please say the home address, or say No Home Address."
        )
    if card.get("needs_home"):
        return (
            f"For {name}, Pointcare still needs a home address. "
            "Please say it, or say No Home Address."
        )
    if card.get("needs_mailing"):
        return (
            f"For {name}, the home address is on the card, and a mailing address is still needed. "
            "Please say the mailing address, or say No Mailing Address."
        )
    if card.get("no_home"):
        home_bit = "No Home Address"
    elif card.get("home_same"):
        home_bit = "the same home address"
    elif card.get("home"):
        home_bit = f"the home address {card.get('home')}"
    else:
        home_bit = "a home address on the card"
    if card.get("no_mailing"):
        mail_bit = "No Mailing Address"
    elif card.get("mailing_same"):
        mail_bit = "the same mailing address"
    elif card.get("mailing"):
        mail_bit = f"the mailing address {card.get('mailing')}"
    else:
        mail_bit = "a mailing address on the card"
    unit = str(card.get("home_unit") or "").strip()
    unit_bit = f" The home apartment or unit is {unit}." if unit else ""
    return (
        f"For {name}, I see {home_bit}, and {mail_bit}.{unit_bit} "
        "Does that look correct?"
    )


async def _open_member_address_editor(page: Page, member_name: str) -> bool:
    """Open Add or Edit on that member's card. An already open editor is left open."""
    try:
        ok = await page.evaluate(
            "(memberName) => {"
            + _member_address_card_js()
            + """
              const card = findAddressCard(memberName);
              if (!card) return false;
              const fields = cardFields(card);
              if (fields.home || fields.mailing || fields.homeUnit || fields.mailingUnit) {
                return true;
              }
              const buttons = [...card.querySelectorAll('button, [role=button], a')];
              const ranked = [];
              for (const btn of buttons) {
                if (!visible(btn)) continue;
                const label = low(btn.innerText || btn.textContent || btn.getAttribute('aria-label') || '');
                if (label === 'edit' || label === 'pencil' || label.startsWith('edit ')) ranked.push([0, btn]);
                else if (label === 'add' || label.startsWith('add ')) ranked.push([1, btn]);
              }
              ranked.sort((a, b) => a[0] - b[0]);
              if (!ranked.length) return false;
              ranked[0][1].click();
              return true;
            }""",
            member_name,
        )
        return bool(ok)
    except Exception:
        logger.exception("open_member_address_editor failed")
        return False


async def _click_member_address_action(
    page: Page, member_name: str, action: str
) -> bool:
    """Click Save or Cancel inside one member card."""
    try:
        ok = await page.evaluate(
            "({ memberName, action }) => {"
            + _member_address_card_js()
            + """
              const card = findAddressCard(memberName);
              if (!card) return false;
              const want = low(action);
              const buttons = [...card.querySelectorAll('button, [role=button], a, input[type=button], input[type=submit]')];
              for (const btn of buttons) {
                if (!visible(btn)) continue;
                const label = low(btn.innerText || btn.value || btn.textContent || '');
                if (label === want || label.startsWith(want + ' ')) {
                  btn.click();
                  return true;
                }
              }
              return false;
            }""",
            {"memberName": member_name, "action": action},
        )
        return bool(ok)
    except Exception:
        logger.exception("click_member_address_action failed")
        return False


async def _set_member_no_address(page: Page, member_name: str, kind: str) -> bool:
    """Check No Home Address or No Mailing Address on one member card, then re-read it."""
    phrase = "no home address" if kind == "home" else "no mailing address"
    try:
        ok = await page.evaluate(
            "({ memberName, phrase }) => {"
            + _member_address_card_js()
            + """
              const card = findAddressCard(memberName);
              if (!card) return false;
              const boxes = [...card.querySelectorAll('input[type=checkbox], [role=checkbox]')];
              for (const box of boxes) {
                let label = '';
                if (box.id) {
                  const lab = card.querySelector('label[for="' + CSS.escape(box.id) + '"]');
                  if (lab) label = low(lab.innerText || lab.textContent || '');
                }
                if (!label) {
                  const host = box.closest('label') || box.parentElement;
                  label = low((host && (host.innerText || host.textContent)) || box.getAttribute('aria-label') || '');
                }
                if (!label.includes(phrase)) continue;
                const on = box.checked === true || box.getAttribute('aria-checked') === 'true';
                if (!on) box.click();
                const now = box.checked === true || box.getAttribute('aria-checked') === 'true';
                return now;
              }
              return false;
            }""",
            {"memberName": member_name, "phrase": phrase},
        )
        return bool(ok)
    except Exception:
        logger.exception("set_member_no_address failed")
        return False


async def _fill_member_address_inputs(
    page: Page,
    member_name: str,
    *,
    home: str = "",
    home_unit: str = "",
    mailing: str = "",
    mailing_unit: str = "",
) -> dict[str, str]:
    """Fill only the requested fields inside one member card."""
    try:
        raw = await page.evaluate(
            "({ memberName, home, homeUnit, mailing, mailingUnit }) => {"
            + _member_address_card_js()
            + """
              const card = findAddressCard(memberName);
              if (!card) return null;
              const fields = cardFields(card);
              const setVal = (el, value) => {
                if (!el || !value) return;
                el.focus();
                el.value = value;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
              };
              setVal(fields.home, home);
              setVal(fields.homeUnit, homeUnit);
              setVal(fields.mailing, mailing);
              setVal(fields.mailingUnit, mailingUnit);
              return {
                home: fields.home ? (fields.home.value || '') : '',
                home_unit: fields.homeUnit ? (fields.homeUnit.value || '') : '',
                mailing: fields.mailing ? (fields.mailing.value || '') : '',
                mailing_unit: fields.mailingUnit ? (fields.mailingUnit.value || '') : '',
              };
            }""",
            {
                "memberName": member_name,
                "home": home,
                "homeUnit": home_unit,
                "mailing": mailing,
                "mailingUnit": mailing_unit,
            },
        )
        return raw if isinstance(raw, dict) else {}
    except Exception:
        logger.exception("fill_member_address_inputs failed")
        return {}


async def _visible_address_suggestions(page: Page) -> list[str]:
    """Visible autocomplete rows. Position in the list is not a choice."""
    try:
        rows = await page.evaluate(
            """() => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const nodes = [...document.querySelectorAll(
                '[role=option], .pac-item, [class*="suggestion"]'
              )];
              const out = [];
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (r.width < 8 || r.height < 8) continue;
                const text = norm(el.innerText || el.textContent || '');
                if (!text || text.length < 4) continue;
                if (/^powered by google$/i.test(text)) continue;
                if (!out.includes(text)) out.push(text);
              }
              return out;
            }"""
        )
        if isinstance(rows, list):
            return [str(item).strip() for item in rows if str(item).strip()]
    except Exception:
        logger.exception("visible_address_suggestions failed")
    return []


async def _click_address_suggestion(page: Page, suggestion: str) -> bool:
    """Click the suggestion whose text matches. Do not click the first row."""
    try:
        ok = await page.evaluate(
            """(suggestion) => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const want = norm(suggestion);
              if (!want) return false;
              const nodes = [...document.querySelectorAll(
                '[role=option], .pac-item, [class*="suggestion"]'
              )];
              const hits = [];
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (r.width < 8 || r.height < 8) continue;
                const text = norm(el.innerText || el.textContent || '');
                if (text === want) hits.push(el);
              }
              if (hits.length !== 1) return false;
              hits[0].click();
              return true;
            }""",
            suggestion,
        )
        return bool(ok)
    except Exception:
        logger.exception("click_address_suggestion failed")
        return False


async def _address_validation_visible(page: Page, member_name: str) -> bool:
    try:
        state = await page.evaluate(
            "(memberName) => {"
            + _member_address_card_js()
            + """
              const card = findAddressCard(memberName);
              if (!card) return false;
              const text = low(card.innerText || '');
              return /please enter|invalid address|address is required|select an address|choose an address|enter a valid/.test(text);
            }""",
            member_name,
        )
    except Exception:
        return False
    return state is True


def _card_by_name(
    cards: list[dict[str, Any]], name: str
) -> dict[str, Any] | None:
    want = (name or "").strip().lower()
    hits = [
        card
        for card in cards
        if str(card.get("name") or "").strip().lower() == want
    ]
    if len(hits) != 1:
        return None
    return hits[0]


def _other_addresses_unchanged(
    before: list[dict[str, Any]], after: list[dict[str, Any]], member_name: str
) -> bool:
    current = (member_name or "").strip().lower()
    previous = {
        str(card.get("name") or "").strip().lower(): (
            str(card.get("home") or ""),
            str(card.get("mailing") or ""),
            bool(card.get("no_home")),
            bool(card.get("no_mailing")),
        )
        for card in before
    }
    for card in after:
        name = str(card.get("name") or "").strip().lower()
        if not name or name == current or name not in previous:
            continue
        now = (
            str(card.get("home") or ""),
            str(card.get("mailing") or ""),
            bool(card.get("no_home")),
            bool(card.get("no_mailing")),
        )
        if now != previous[name]:
            return False
    return True


async def _commit_member_address(
    page: Page,
    member_name: str,
    field: str,
    street: str,
    unit: str = "",
) -> dict[str, Any]:
    """Fill one member's address, require a unique suggestion, then Save and re-read."""
    before = await _scrape_address_cards(page)
    if not await _open_member_address_editor(page, member_name):
        logger.warning("address_editor_not_opened")
        return {"ok": False, "error": "address_editor_failed"}
    await page.wait_for_timeout(200)
    home = street if field in ("home", "both") else ""
    mailing = street if field == "mailing" else ""
    home_unit = unit if field in ("home", "both") else ""
    mailing_unit = unit if field == "mailing" else ""
    written = await _fill_member_address_inputs(
        page,
        member_name,
        home=home,
        home_unit=home_unit,
        mailing=mailing,
        mailing_unit=mailing_unit,
    )
    if not written:
        logger.warning("address_fill_failed")
        return {"ok": False, "error": "address_fill_failed"}
    number_match = re.search(r"\b\d+\b", street or "")
    number = number_match.group(0) if number_match else ""
    target_value = written.get("mailing") if field == "mailing" else written.get("home")
    if number and number not in str(target_value or ""):
        logger.warning("address_fill_not_in_field")
        return {"ok": False, "error": "address_fill_failed"}
    if unit and field in ("home", "both") and str(written.get("home_unit") or "") != unit:
        logger.warning("address_unit_not_separate")
        return {"ok": False, "error": "address_fill_failed"}
    if unit and field == "mailing" and str(written.get("mailing_unit") or "") != unit:
        logger.warning("address_unit_not_separate")
        return {"ok": False, "error": "address_fill_failed"}
    await page.wait_for_timeout(400)
    suggestions = await _visible_address_suggestions(page)
    matches = _suggestion_matches(street, suggestions)
    if len(matches) > 1:
        logger.warning("address_suggestion_ambiguous")
        return {
            "ok": False,
            "error": "address_ambiguous",
            "suggestions": matches[:3],
        }
    if len(matches) != 1:
        logger.warning("address_suggestion_missing")
        return {"ok": False, "error": "address_no_suggestion"}
    if not await _click_address_suggestion(page, matches[0]):
        logger.warning("address_suggestion_click_failed")
        return {"ok": False, "error": "address_no_suggestion"}
    await page.wait_for_timeout(200)
    checked = await _fill_member_address_inputs(page, member_name)
    selected = checked.get("mailing") if field == "mailing" else checked.get("home")
    if number and number not in str(selected or ""):
        logger.warning("address_suggestion_not_committed")
        return {"ok": False, "error": "address_no_suggestion"}
    if field == "both":
        mailing_now = str(checked.get("mailing") or "")
        if number and number not in mailing_now and "same" not in mailing_now.lower():
            await _fill_member_address_inputs(page, member_name, mailing=str(selected or street))
            await page.wait_for_timeout(300)
            more = _suggestion_matches(street, await _visible_address_suggestions(page))
            if len(more) == 1:
                await _click_address_suggestion(page, more[0])
            elif len(more) > 1:
                logger.warning("address_suggestion_ambiguous")
                return {
                    "ok": False,
                    "error": "address_ambiguous",
                    "suggestions": more[:3],
                }
    if not await _click_member_address_action(page, member_name, "save"):
        logger.warning("address_save_click_failed")
        return {"ok": False, "error": "address_save_failed"}
    await page.wait_for_timeout(400)
    if await _address_validation_visible(page, member_name):
        logger.warning("address_validation_still_visible")
        return {"ok": False, "error": "address_save_failed"}
    after = await _scrape_address_cards(page)
    if not _other_addresses_unchanged(before, after, member_name):
        logger.warning("address_other_member_changed")
        return {"ok": False, "error": "address_wrong_member"}
    card = _card_by_name(after, member_name)
    if card is None:
        logger.warning("address_card_missing_after_save")
        return {"ok": False, "error": "address_not_verified"}
    blob = " ".join(
        [
            str(card.get("home") or ""),
            str(card.get("mailing") or ""),
            str(card.get("home_unit") or ""),
            str(card.get("mailing_unit") or ""),
        ]
    )
    if number and number not in blob and not card.get("home_same") and not card.get("mailing_same"):
        logger.warning("address_not_on_card")
        return {"ok": False, "error": "address_not_verified"}
    if field in ("home", "both") and card.get("needs_home"):
        logger.warning("address_home_still_required")
        return {"ok": False, "error": "address_not_verified"}
    if field in ("mailing", "both") and card.get("needs_mailing") and not card.get("mailing_same"):
        logger.warning("address_mailing_still_required")
        return {"ok": False, "error": "address_not_verified"}
    return {"ok": True, "error": "", "card": card}


async def _click_stage_next(page: Page) -> bool:
    """Click Next or Continue only. Do not click Save on an open address card."""
    from .live_form import _click_cta

    enabled = await _next_button_enabled(page)
    if enabled is False:
        logger.info("stage_e_next_disabled skip_click")
        return False
    for label in ("Next", "Continue"):
        try:
            await _click_cta(page, label, attempts=1)
            await page.wait_for_timeout(600)
            return True
        except Exception:
            continue
    return False


def _member_dob_card_js() -> str:
    """JS prelude that returns the smallest card whose only name line is the member."""
    return """
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase()
        .replace(/\\./g, '');
      const isNameLine = (line) => {
        let t = (line || '').replace(/\\s+/g, ' ').trim();
        t = t.replace(/\\s+\\b(add|edit|pencil|save|next|continue)\\b\\.?$/i, '').trim();
        return /^[A-Za-z][A-Za-z .'-]{0,40}$/.test(t) &&
          t.split(' ').length <= 4 &&
          !/^(date of birth|dob|add|edit|save|next|continue|yes|no|pencil|mm|dd|yyyy)$/i
            .test(t);
      };
      const findCard = (memberName) => {
        const want = norm(memberName);
        if (!want) return null;
        const nodes = [...document.querySelectorAll(
          'article, section, li, [class*="card"], [class*="Card"], [class*="member"], div'
        )];
        let best = null;
        let bestArea = Infinity;
        for (const el of nodes) {
          const r = el.getBoundingClientRect();
          if (r.width < 40 || r.height < 20 || r.height > 420) continue;
          const lines = (el.innerText || '').split('\\n').map((s) => s.trim()).filter(Boolean);
          const names = lines.filter(isNameLine);
          if (names.length !== 1 || norm(names[0]) !== want) continue;
          const area = r.width * r.height;
          if (area < bestArea) {
            best = el;
            bestArea = area;
          }
        }
        return best;
      };
    """


async def _click_member_add_dob(
    page: Page, member_name: str, edit: bool = False
) -> bool:
    """Click Add or Edit on that member's own card, never a parent that lists others."""
    try:
        ok = await page.evaluate(
            "({ memberName, edit }) => {"
            + _member_dob_card_js()
            + """
              const card = findCard(memberName);
              if (!card) return false;
              const buttons = [...card.querySelectorAll('button, [role=button], a')];
              const wantEdit = !!edit;
              for (const btn of buttons) {
                const br = btn.getBoundingClientRect();
                if (br.width < 8 || br.height < 8) continue;
                const label = norm(btn.innerText || btn.textContent || btn.getAttribute('aria-label') || '');
                const isEdit = label === 'edit' || label.startsWith('edit ') || label === 'pencil';
                const isAdd = label === 'add' || label.startsWith('add ') || label.includes('add date');
                if (wantEdit ? isEdit : isAdd) {
                  btn.click();
                  return true;
                }
              }
              return false;
            }""",
            {"memberName": member_name, "edit": edit},
        )
        return bool(ok)
    except Exception:
        logger.exception("click_member_add_dob failed")
        return False


def _dob_parts(dob: str) -> tuple[str, str, str] | None:
    """Split ``MM/DD/YYYY`` into month, day, year strings."""
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", (dob or "").strip())
    if not m:
        return None
    return f"{int(m.group(1)):02d}", f"{int(m.group(2)):02d}", m.group(3)


def _dob_digits_match(expected: str, actual: str) -> bool:
    want = re.sub(r"\D", "", expected or "")
    got = re.sub(r"\D", "", actual or "")
    if not want or not got:
        return False
    return got == want or got.endswith(want) or want.endswith(got)


async def _inspect_dob_editor(page: Page) -> dict[str, Any]:
    """Describe the open DOB editor controls (masked / date / segmented)."""
    try:
        info = await page.evaluate(
            """() => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                return r.width > 2 && r.height > 2 && !el.disabled;
              };
              const inputs = [...document.querySelectorAll(
                'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea'
              )].filter(visible);
              const dateLike = inputs.filter((el) => {
                const t = (el.type || '').toLowerCase();
                const ph = norm(el.placeholder || el.getAttribute('aria-label') || '');
                const name = norm(el.name || el.id || '');
                return (
                  t === 'date' ||
                  /birth|dob|mm\\/dd|month|day|year/i.test(ph + ' ' + name)
                );
              });
              const pool = dateLike.length ? dateLike : inputs;
              const values = pool.map((el) => ({
                type: (el.type || 'text').toLowerCase(),
                value: el.value || '',
                placeholder: el.placeholder || '',
                name: el.name || el.id || '',
                maxLength: el.maxLength || 0,
                inputMode: el.inputMode || '',
              }));
              const hasSave = [...document.querySelectorAll(
                'button, [role=button], a, input[type=button], input[type=submit]'
              )].some((el) => {
                if (!visible(el)) return false;
                const t = norm(el.innerText || el.value || el.textContent || '')
                  .toLowerCase();
                return t === 'save' || t.startsWith('save ') || t.includes('save and');
              });
              return {
                count: pool.length,
                values,
                hasSave,
                kinds: values.map((v) => v.type),
              };
            }"""
        )
        return info if isinstance(info, dict) else {}
    except Exception:
        logger.exception("inspect_dob_editor failed")
        return {}


async def _read_dob_editor_value(page: Page, member_name: str = "") -> str:
    """Return the open DOB fields for this member, or the only open editor."""
    try:
        raw = await page.evaluate(
            "(memberName) => {"
            + _member_dob_card_js()
            + """
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                return r.width > 2 && r.height > 2 && !el.disabled;
              };
              let inputs = [...document.querySelectorAll(
                'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea'
              )].filter(visible);
              if (memberName) {
                const card = findCard(memberName);
                if (!card) return '';
                inputs = inputs.filter((el) => card.contains(el));
              }
              const dateLike = inputs.filter((el) => {
                const t = (el.type || '').toLowerCase();
                const ph = ((el.placeholder || '') + ' ' + (el.name || '') + ' ' +
                  (el.id || '') + ' ' + (el.getAttribute('aria-label') || ''))
                  .toLowerCase();
                return t === 'date' || /birth|dob|month|day|year|yyyy|\\bmm\\b|\\bdd\\b/.test(ph);
              });
              const pool = dateLike.length ? dateLike : inputs.slice(0, 3);
              return pool.map((el) => el.value || '').join(' ');
            }""",
            member_name,
        )
        return str(raw or "")
    except Exception:
        return ""


async def _fill_dob_editor_fields(
    page: Page, dob: str, member_name: str = ""
) -> bool:
    """Fill the open DOB editor using the format Pointcare actually exposes."""
    parts = _dob_parts(dob)
    if not parts:
        return False
    mm, dd, yyyy = parts
    iso = f"{yyyy}-{mm}-{dd}"
    digits = f"{mm}{dd}{yyyy}"
    try:
        filled = await page.evaluate(
            "({ mm, dd, yyyy, iso, slash, digits, memberName }) => {"
            + _member_dob_card_js()
            + """
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                return r.width > 2 && r.height > 2 && !el.disabled && !el.readOnly;
              };
              const setVal = (el, value) => {
                el.focus();
                el.value = '';
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.value = value;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
              };
              let inputs = [...document.querySelectorAll(
                'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea'
              )].filter(visible);
              if (memberName) {
                const card = findCard(memberName);
                if (!card) return { ok: false, mode: 'no-card' };
                inputs = inputs.filter((el) => card.contains(el));
              }
              if (!inputs.length) return { ok: false, mode: 'none' };

              const dateInputs = inputs.filter((el) => (el.type || '').toLowerCase() === 'date');
              if (dateInputs.length === 1) {
                setVal(dateInputs[0], iso);
                return { ok: true, mode: 'html-date', value: dateInputs[0].value || '' };
              }

              const labeled = { month: null, day: null, year: null, combined: null };
              for (const el of inputs) {
                const hint = norm(
                  (el.placeholder || '') + ' ' + (el.name || '') + ' ' +
                  (el.id || '') + ' ' + (el.getAttribute('aria-label') || '') +
                  ' ' + (el.getAttribute('autocomplete') || '')
                );
                if (/month|mm\\b/.test(hint) && !labeled.month) labeled.month = el;
                else if (/\\bday\\b|dd\\b/.test(hint) && !labeled.day) labeled.day = el;
                else if (/year|yyyy|yy\\b/.test(hint) && !labeled.year) labeled.year = el;
                else if (/birth|dob|date/.test(hint) && !labeled.combined) labeled.combined = el;
              }
              if (labeled.month && labeled.day && labeled.year) {
                setVal(labeled.month, mm);
                setVal(labeled.day, dd);
                setVal(labeled.year, yyyy);
                return {
                  ok: true,
                  mode: 'segmented',
                  value: [labeled.month.value, labeled.day.value, labeled.year.value].join('/'),
                };
              }
              // Three short numeric fields without clear labels — treat as M/D/Y.
              const short = inputs.filter((el) => {
                const ml = el.maxLength || 0;
                const t = (el.type || '').toLowerCase();
                return (t === 'text' || t === 'tel' || t === 'number') && (ml > 0 && ml <= 4);
              });
              if (short.length >= 3) {
                setVal(short[0], mm);
                setVal(short[1], dd);
                setVal(short[2], yyyy);
                return {
                  ok: true,
                  mode: 'segmented-short',
                  value: [short[0].value, short[1].value, short[2].value].join('/'),
                };
              }
              const target = labeled.combined || inputs[0];
              setVal(target, slash);
              let got = target.value || '';
              if (!got || got.replace(/\\D/g, '').length < 8) {
                setVal(target, digits);
                got = target.value || '';
              }
              return { ok: true, mode: 'combined', value: got };
            }""",
            {
                "mm": mm,
                "dd": dd,
                "yyyy": yyyy,
                "iso": iso,
                "slash": f"{mm}/{dd}/{yyyy}",
                "digits": digits,
                "memberName": member_name,
            },
        )
        if not isinstance(filled, dict) or not filled.get("ok"):
            return False
        got = str(filled.get("value") or "")
        if _dob_digits_match(dob, got):
            return True
        # Masked fields often need sequential key entry after clear.
        return await _type_dob_via_keyboard(page, dob, member_name)
    except Exception:
        logger.exception("fill_dob_editor_fields failed")
        return False


async def _type_dob_via_keyboard(
    page: Page, dob: str, member_name: str = ""
) -> bool:
    """Enter DOB digits sequentially into the focused/visible masked field."""
    parts = _dob_parts(dob)
    if not parts:
        return False
    mm, dd, yyyy = parts
    digits = f"{mm}{dd}{yyyy}"
    try:
        focused = await page.evaluate(
            "(memberName) => {"
            + _member_dob_card_js()
            + """
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                return r.width > 2 && r.height > 2 && !el.disabled;
              };
              let inputs = [...document.querySelectorAll(
                'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea'
              )].filter(visible);
              if (memberName) {
                const card = findCard(memberName);
                if (!card) return false;
                inputs = inputs.filter((el) => card.contains(el));
              }
              const dateLike = inputs.filter((el) => {
                const t = (el.type || '').toLowerCase();
                const ph = ((el.placeholder || '') + ' ' + (el.name || '') + ' ' +
                  (el.id || '') + ' ' + (el.getAttribute('aria-label') || ''))
                  .toLowerCase();
                return t === 'date' || /birth|dob|mm\\/dd|date/.test(ph);
              });
              const el = dateLike[0] || inputs[0];
              if (!el) return false;
              el.focus();
              el.select && el.select();
              return true;
            }""",
            member_name,
        )
        if not focused:
            return False
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.type(digits, delay=40)
        await page.wait_for_timeout(150)
        got = await _read_dob_editor_value(page, member_name)
        return _dob_digits_match(dob, got)
    except Exception:
        logger.exception("type_dob_via_keyboard failed")
        return False


async def _click_dob_save(page: Page, member_name: str = "") -> bool:
    """Click Save on this member's editor. Failure is a real failure."""
    from .live_form import _click_cta

    if not member_name:
        for label in ("Save", "Save and Return", "Save & Return"):
            try:
                await _click_cta(page, label, attempts=1)
                return True
            except Exception:
                continue
    try:
        clicked = await page.evaluate(
            "(memberName) => {"
            + _member_dob_card_js()
            + """
              const nodes = [...document.querySelectorAll(
                'button, [role=button], a, input[type=button], input[type=submit]'
              )];
              const card = memberName ? findCard(memberName) : null;
              if (memberName && !card) return false;
              for (const el of nodes) {
                if (card && !card.contains(el)) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 4 || r.height < 4) continue;
                if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
                const t = norm(el.innerText || el.value || el.textContent || '');
                if (t === 'save' || t.startsWith('save ') || t.includes('save and')) {
                  el.click();
                  return true;
                }
              }
              return false;
            }""",
            member_name,
        )
        return bool(clicked)
    except Exception:
        logger.exception("click_dob_save failed")
        return False


async def _dob_validation_visible(page: Page, member_name: str = "") -> bool:
    """True only when this member's card still shows a date validation error."""
    try:
        state = await page.evaluate(
            "(memberName) => {"
            + _member_dob_card_js()
            + """
              const card = memberName ? findCard(memberName) : document.body;
              if (!card) return false;
              const text = (card.innerText || '').toLowerCase();
              return /invalid date|please enter|date is required|enter a valid/.test(text);
            }""",
            member_name,
        )
    except Exception:
        return False
    return state is True


async def _fill_and_save_dob(page: Page, dob: str, member_name: str = "") -> bool:
    """Fill the open DOB editor, verify the value, then Save.

    Never returns True merely because the editor opened. Save failure and a
    blank/malformed field are failures.
    """
    from .live_form import _strict_fill

    parts = _dob_parts(dob)
    if not parts:
        return False

    # Wait briefly for the editor controls after Add.
    editor: dict[str, Any] = {}
    for _ in range(8):
        editor = await _inspect_dob_editor(page)
        if int(editor.get("count") or 0) > 0:
            break
        await page.wait_for_timeout(150)
    if int(editor.get("count") or 0) <= 0:
        logger.warning("dob_editor_not_ready")
        return False

    filled = False
    # Prefer scoped editor fill over page-wide label lookup.
    filled = await _fill_dob_editor_fields(page, dob, member_name)
    if not filled and not member_name:
        try:
            await _strict_fill(page, "Date of birth", dob)
            got = await _read_dob_editor_value(page)
            filled = _dob_digits_match(dob, got)
        except Exception:
            filled = False
    if not filled:
        filled = await _type_dob_via_keyboard(page, dob, member_name)
    if not filled:
        logger.warning("dob_fill_value_missing_after_entry")
        return False

    got_before_save = await _read_dob_editor_value(page, member_name)
    if not _dob_digits_match(dob, got_before_save):
        if not await _type_dob_via_keyboard(page, dob, member_name):
            logger.warning("dob_fill_malformed_before_save")
            return False
        got_before_save = await _read_dob_editor_value(page, member_name)
        if not _dob_digits_match(dob, got_before_save):
            return False

    saved = await _click_dob_save(page, member_name)
    if not saved:
        logger.warning("dob_save_click_failed")
        return False
    await page.wait_for_timeout(500)
    if await _dob_validation_visible(page, member_name):
        logger.warning("dob_validation_still_visible")
        return False
    return True


def _dob_saved_match(expected: str, actual: str) -> bool:
    """True when two displayed dates are the same month, day, and year."""
    want = _dob_parts(expected)
    got = _dob_parts(actual)
    if want and got:
        return want == got
    want_digits = re.sub(r"\D", "", expected or "")
    got_digits = re.sub(r"\D", "", actual or "")
    return bool(want_digits) and want_digits == got_digits and len(want_digits) == 8


async def _verify_member_dob(page: Page, member_name: str, dob: str) -> bool:
    """True only when that member's own card shows this saved date."""
    cards = await _scrape_dob_cards(page)
    want = (member_name or "").strip().lower()
    matches = [
        card
        for card in cards
        if (card.get("name") or "").strip().lower() == want
    ]
    if len(matches) != 1:
        return False
    card = matches[0]
    if card.get("needs_add"):
        return False
    return _dob_saved_match(dob, str(card.get("dob") or ""))


def _application_next_buttons_js() -> str:
    """JS helpers that find the real Next/Continue control.

    A class is disabled only when this element itself has the disabled
    attribute, aria-disabled, or a disabled class token. An ancestor's class
    and a substring such as "disabledState" do not count. An earlier disabled
    Next does not hide a later enabled one.
    """
    return r"""
      const nextLabel = (el) => ((el.innerText || el.value || el.textContent || '')
        ).replace(/\s+/g, ' ').trim().toLowerCase();
      const ownClass = (el) => {
        if (typeof el.className === 'string') return el.className;
        return (el.getAttribute && el.getAttribute('class')) || '';
      };
      const selfDisabled = (el) => {
        if (el.disabled === true) return true;
        if (el.getAttribute('aria-disabled') === 'true') return true;
        const tokens = String(ownClass(el)).toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
        return tokens.some((token) => token === 'disabled');
      };
      const nextButtons = () => {
        const labels = ['next', 'continue'];
        const nodes = [...document.querySelectorAll(
          'button, [role=button], a, input[type=button], input[type=submit]'
        )];
        const found = [];
        for (const el of nodes) {
          if (!labels.includes(nextLabel(el))) continue;
          const r = el.getBoundingClientRect();
          if (r.width < 4 || r.height < 4) continue;
          found.push({ el, bottom: r.bottom, disabled: selfDisabled(el) });
        }
        return found;
      };
    """


async def _next_button_enabled(page: Page) -> bool | None:
    """Return True when any visible Next/Continue control is enabled.

    False means every matching control is disabled on itself. None means no
    visible Next/Continue control was found.
    """
    try:
        state = await page.evaluate(
            "() => {\n"
            + _application_next_buttons_js()
            + """
              const found = nextButtons();
              if (!found.length) return null;
              return found.some((item) => !item.disabled);
            }"""
        )
        if state is None:
            return None
        return bool(state)
    except Exception:
        return None


async def _click_enabled_application_next(page: Page) -> bool:
    """Click the enabled Next/Continue control, preferring the lowest one."""
    try:
        clicked = await page.evaluate(
            "() => {\n"
            + _application_next_buttons_js()
            + """
              const enabled = nextButtons().filter((item) => !item.disabled);
              if (!enabled.length) return false;
              enabled.sort((a, b) => b.bottom - a.bottom);
              enabled[0].el.click();
              return true;
            }"""
        )
        if not clicked:
            return False
        await page.wait_for_timeout(600)
        return True
    except Exception:
        logger.exception("application_next_click_failed")
        return False


async def _visible_form_validation(page: Page) -> str:
    """Short visible validation text, without logging the message body."""
    try:
        text = await page.evaluate(
            """() => {
              const nodes = [...document.querySelectorAll(
                '[role=alert], [aria-live=assertive], [aria-invalid=true]'
              )];
              const parts = [];
              for (const el of nodes) {
                const raw = (
                  el.innerText || el.textContent ||
                  el.getAttribute('aria-errormessage') || ''
                ).replace(/\\s+/g, ' ').trim();
                if (!raw || raw.length > 180 || parts.includes(raw)) continue;
                const box = el.getBoundingClientRect();
                if (box.width < 2 && box.height < 2 && el.getAttribute('aria-invalid') !== 'true') {
                  continue;
                }
                parts.push(raw);
              }
              return parts.slice(0, 2).join(' ');
            }"""
        )
    except Exception:
        return ""
    return text if isinstance(text, str) else ""


def _immigration_queue(session: Any) -> list[str]:
    raw = session.filled.get("app_immigration_queue") or ""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(item).strip() for item in data if str(item).strip()]
    except Exception:
        pass
    return []


def _set_immigration_queue(session: Any, names: list[str] | None) -> None:
    if not names:
        session.filled.pop("app_immigration_queue", None)
        session.filled.pop("app_immigration_current", None)
        return
    session.filled["app_immigration_queue"] = json.dumps(names)


def _insurance_queue(session: Any) -> list[str]:
    raw = session.filled.get("app_insurance_queue") or ""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(item).strip() for item in data if str(item).strip()]
    except Exception:
        pass
    return []


def _set_insurance_queue(session: Any, names: list[str] | None) -> None:
    if not names:
        session.filled.pop("app_insurance_queue", None)
        session.filled.pop("app_insurance_current", None)
        return
    session.filled["app_insurance_queue"] = json.dumps(names)


def _dob_queue(session: Any) -> list[str]:
    raw = session.filled.get("app_dob_queue") or ""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(item).strip() for item in data if str(item).strip()]
    except Exception:
        pass
    return []


def _set_dob_queue(session: Any, names: list[str] | None) -> None:
    if not names:
        session.filled.pop("app_dob_queue", None)
        session.filled.pop("app_dob_current", None)
        session.filled.pop("app_dob_correcting", None)
        return
    session.filled["app_dob_queue"] = json.dumps(names)


# ASR often hears single-letter initials as short words ("H" → "edge" / "aitch").
_INITIAL_ASR_ALIASES: dict[str, str] = {
    "ay": "a",
    "bee": "b",
    "be": "b",
    "see": "c",
    "sea": "c",
    "dee": "d",
    "ee": "e",
    "ef": "f",
    "eff": "f",
    "gee": "g",
    "aitch": "h",
    "aitch.": "h",
    "edge": "h",
    "age": "h",
    "eyech": "h",
    "eye": "i",
    "jay": "j",
    "kay": "k",
    "el": "l",
    "ell": "l",
    "em": "m",
    "en": "n",
    "oh": "o",
    "pee": "p",
    "cue": "q",
    "queue": "q",
    "are": "r",
    "ess": "s",
    "as": "s",
    "tee": "t",
    "you": "u",
    "vee": "v",
    "doubleyou": "w",
    "ex": "x",
    "why": "y",
    "zee": "z",
    "zed": "z",
}


def _strip_dob_command_words(spoken: str) -> str:
    """Remove change/date-of-birth command words, leaving the spoken name."""
    text = (spoken or "").strip()
    text = re.sub(
        r"\b("
        r"change|correct|fix|edit|update|please|"
        r"date of birth|date|birth|birthday|dob|"
        r"for|of|the|to|whose|who|person|member"
        r")\b",
        " ",
        text,
        flags=re.I,
    )
    return re.sub(r"\s+", " ", text).strip(" .,!?")


def _normalize_spoken_initials(text: str) -> str:
    """Map ASR initial spellings onto single letters (\"Agent Edge\" → \"Agent H\").

    Only rewrites the final token of a multi-word name so ordinary words are
    not corrupted (e.g. \"are\" / \"as\" mid-sentence).
    """
    from .live_form import _spoken_member_query

    cleaned = _spoken_member_query(text or "")
    parts = cleaned.split()
    if len(parts) >= 2:
        key = parts[-1].lower().rstrip(".")
        alias = _INITIAL_ASR_ALIASES.get(key)
        if alias:
            parts[-1] = alias.upper()
    return " ".join(parts).strip()


def _title_member_label(label: str) -> str:
    parts = (label or "").split()
    out: list[str] = []
    for part in parts:
        if len(part) == 1:
            out.append(part.upper())
        else:
            out.append(part[:1].upper() + part[1:])
    return " ".join(out).strip()


def _extract_spoken_member_labels(spoken: str) -> list[str]:
    """Best-effort member labels from speech when the DOM scrape is empty."""
    from .live_form import _split_member_queries

    cleaned = _strip_dob_command_words(spoken)
    if not cleaned:
        return []
    cleaned = _normalize_spoken_initials(cleaned)
    queries = _split_member_queries(cleaned) or [cleaned]
    out: list[str] = []
    for query in queries:
        label = _title_member_label(_normalize_spoken_initials(query))
        if label and label not in out:
            out.append(label)
    return out


def _match_named_members(
    spoken: str, available: list[str]
) -> tuple[list[str], str | None]:
    """Match spoken names to available member labels. Clarify on duplicates.

    When ``available`` is empty, returns cleaned spoken labels so Collect Mode
    can still target \"Change Agent H\" without waiting for a perfect scrape.
    """
    from .live_form import _normalize_member_label, _split_member_queries

    cleaned = _strip_dob_command_words(spoken)
    cleaned = _normalize_spoken_initials(cleaned)
    if not cleaned:
        return [], None
    queries = _split_member_queries(cleaned) or [cleaned]
    queries = [_normalize_spoken_initials(q) for q in queries if q.strip()]
    if not queries:
        return [], None

    if not available:
        # No DOM labels — still accept the spoken name as the correction target.
        return _extract_spoken_member_labels(spoken), None

    hits: list[str] = []
    for query in queries:
        q_norm = _normalize_member_label(query)
        if not q_norm:
            continue
        matches: list[str] = []
        for name in available:
            n_norm = _normalize_member_label(name)
            if not n_norm:
                continue
            if (
                q_norm == n_norm
                or q_norm in n_norm
                or n_norm.startswith(q_norm)
                or n_norm.startswith(q_norm.split()[0])
            ):
                # Prefer first-token + initial matches: "agent h" ↔ "agent h."
                q_parts = q_norm.split()
                n_parts = n_norm.split()
                if len(q_parts) >= 2 and len(n_parts) >= 2:
                    if q_parts[0] == n_parts[0] and q_parts[1][:1] == n_parts[1][:1]:
                        if name not in matches:
                            matches.append(name)
                        continue
                if name not in matches:
                    matches.append(name)
        # Tight pass: first name only when unique among available.
        if not matches and " " not in q_norm:
            first_hits = [
                name
                for name in available
                if _normalize_member_label(name).split()[:1] == [q_norm]
            ]
            matches = list(dict.fromkeys(first_hits))
        uniq = list(dict.fromkeys(matches))
        if len(uniq) > 1:
            return [], (
                "I see more than one person matching "
                f"{query}. Please say the full name as shown on the application."
            )
        if len(uniq) == 1 and uniq[0] not in hits:
            hits.append(uniq[0])
    return hits, None


def _begin_dob_collect_for(
    session: Any, names: list[str]
) -> tuple[str, str]:
    """Arm Collect Mode for the first named member and return (step, speech)."""
    cleaned = [n.strip() for n in names if (n or "").strip()]
    if not cleaned:
        speech = app_question_for_step("app_dob_review")
        return "app_dob_review", speech
    current = cleaned[0]
    _set_dob_queue(session, cleaned)
    session.filled["app_dob_current"] = current
    session.filled["app_dob_correcting"] = current
    stored_partial = _dob_partial_state(session)
    if str(stored_partial.get("name") or "").strip().lower() != current.lower():
        _clear_dob_partial(session)
    speech = _dob_question_for(current)
    _set_pending_question(session, "app_dob_collect", speech)
    return "app_dob_collect", speech


async def _prepare_dob_collect_speech(
    session: Any, page: Page
) -> tuple[str, str]:
    """Inspect DOB cards and return (step, spoken) for resume or entry."""
    cards = await _scrape_dob_cards(page)
    missing = [
        str(card.get("name") or "").strip()
        for card in cards
        if str(card.get("name") or "").strip()
        and (
            card.get("needs_add")
            or not str(card.get("dob") or "").strip()
        )
    ]
    present = [
        card
        for card in cards
        if not card.get("needs_add") and str(card.get("dob") or "").strip()
    ]
    if missing:
        _set_dob_queue(session, missing)
        open_names = [
            str(card.get("name") or "").strip()
            for card in cards
            if card.get("editor_open") and str(card.get("name") or "").strip() in missing
        ]
        current = open_names[0] if open_names else missing[0]
        session.filled["app_dob_current"] = current
        speech = _dob_question_for(current)
        if len(missing) > 1:
            speech = (
                f"I still need dates of birth for {len(missing)} people. " + speech
            )
        _set_pending_question(session, "app_dob_collect", speech)
        return "app_dob_collect", speech
    _set_dob_queue(session, None)
    if present:
        bits = []
        for card in present[:4]:
            bits.append(f"{card.get('name')} {card.get('dob')}")
        summary = "; ".join(bits)
        speech = (
            f"I see these dates of birth on the application: {summary}. "
            "Do they look correct? Say Yes to continue, or tell me whose "
            "date of birth to change."
        )
        _set_pending_question(session, "app_dob_review", speech)
        return "app_dob_review", speech
    speech = app_question_for_step("app_dob_review")
    _set_pending_question(session, "app_dob_review", speech)
    return "app_dob_review", speech


async def _prepare_resume_speech(
    session: Any, page: Page, surface: str
) -> tuple[str, str]:
    """Align resume step with live UI and return (step, spoken)."""
    if surface in ("app_dob_review", "app_dob_collect"):
        return await _prepare_dob_collect_speech(session, page)
    if surface in ("app_immigration", "app_immigration_who"):
        members = await _scrape_immigration_members(page)
        names = [
            str(m.get("name") or "").strip()
            for m in members
            if str(m.get("name") or "").strip()
        ]
        if names:
            # Do not trust preselected radios — walk every member.
            _set_immigration_queue(session, names)
            current = names[0]
            session.filled["app_immigration_current"] = current
            session.filled["app_citizenship_no"] = "1"
            speech = (
                "This application is on the citizenship and immigration screen. "
                + _immigration_status_question_for(current)
            )
            _set_pending_question(session, "app_immigration", speech)
            return "app_immigration", speech
        speech = app_question_for_step("app_immigration_who")
        _set_pending_question(session, "app_immigration_who", speech)
        return "app_immigration_who", speech
    if surface == "app_insurance_coverage_type":
        current = (session.filled.get("app_insurance_current") or "").strip()
        queue = _insurance_queue(session)
        target = current or (queue[0] if queue else "")
        if target:
            session.filled["app_insurance_current"] = target
            speech = _insurance_coverage_question_for(target)
            _set_pending_question(session, "app_insurance_coverage_type", speech)
            return "app_insurance_coverage_type", speech
        speech = app_question_for_step("app_insurance_coverage_type")
        _set_pending_question(session, "app_insurance_coverage_type", speech)
        return "app_insurance_coverage_type", speech
    if surface == "app_insurance_members":
        members = await _scrape_coverage_members(page)
        names = [
            str(row.get("name") or "").strip()
            for row in members
            if str(row.get("name") or "").strip()
        ]
        speech = _who_has_coverage_question(names)
        _set_pending_question(session, "app_insurance_members", speech)
        return "app_insurance_members", speech
    if surface == "app_insurance":
        names = await _coverage_household_names(page, session)
        speech = _coverage_question(names)
        _set_pending_question(session, "app_insurance", speech)
        return "app_insurance", speech
    if surface == "app_citizenship":
        names = await _citizenship_household_names(page, session)
        speech = _citizenship_question(names)
        _set_pending_question(session, "app_citizenship", speech)
        return "app_citizenship", speech
    speech = app_question_for_step(surface)
    _set_pending_question(session, surface, speech)
    return surface, speech


def _unidentified_screen_error(
    call_id: str,
    session: Any,
    answer: str = "",
) -> dict[str, Any]:
    return {
        "ok": False,
        "call_id": call_id,
        "field_id": "application_answer",
        "value": answer,
        "error": "stage_e_screen_unidentified",
        "spoken": (
            "ASK: I'm having trouble identifying the current application screen. "
            "Please stay on this page and say repeat in a moment so I can try again."
        ),
        "message": "Could not identify Stage E screen from live UI",
        "step": session.app_step or "",
        "phase": "application",
    }


def _immigration_status_key(label: str) -> str:
    text = re.sub(r"\s*\(.*?\)\s*", " ", (label or "").lower())
    text = re.sub(r"\s+", " ", text).strip(" .")
    aliases = {
        "us citizen": "u.s. citizen",
        "citizen": "u.s. citizen",
        "us national": "u.s. national",
        "national": "u.s. national",
        "satisfactory immigration status": "satisfactory immigration status",
        "green card": "satisfactory immigration status",
        "prefer not": "prefer not to answer",
        "prefer not to answer": "prefer not to answer",
    }
    return aliases.get(text, text)


def _same_immigration_status(left: str, right: str) -> bool:
    key = _immigration_status_key(left)
    return bool(key) and key == _immigration_status_key(right) and key in {
        "u.s. citizen",
        "u.s. national",
        "satisfactory immigration status",
        "prefer not to answer",
    }


def _map_immigration_status(lowered: str) -> str | None:
    """Map one spoken status onto one Pointcare option. Two statuses do not match."""
    hits: list[str] = []
    if "prefer not" in lowered:
        hits.append("Prefer not to answer")
    if (
        "satisfactory" in lowered
        or "green card" in lowered
        or "permanent resident" in lowered
        or re.search(r"\bvisa\b", lowered)
    ):
        hits.append("Satisfactory immigration status (green card, etc.)")
    if re.search(r"\bnational\b", lowered):
        hits.append("U.S. National")
    negated_citizen = bool(re.search(r"\bnot\b.{0,24}\bcitizen", lowered))
    if re.search(r"\bcitizen\b", lowered) and not negated_citizen:
        hits.append("U.S. Citizen")
    if len(hits) == 1:
        return hits[0]
    return None


def _immigration_name_residue(spoken: str) -> str:
    text = re.sub(
        r"\b("
        r"satisfactory immigration status(?:\s*\([^)]*\))?|"
        r"green cards?|permanent residents?|visas?|"
        r"prefer not to answer|prefer not|"
        r"u\.?s\.?\s+nationals?|nationals?|"
        r"u\.?s\.?\s+citizens?|citizens?|"
        r"immigration status|immigration"
        r")\b",
        " ",
        spoken or "",
        flags=re.I,
    )
    text = re.sub(
        r"\b(has|have|had|is|was|are|a|an|the|with|for|of|please|status)\b",
        " ",
        text,
        flags=re.I,
    )
    return re.sub(r"\s+", " ", text).strip(" .,!?")


def _immigration_confirmed(session: Any) -> dict[str, str]:
    raw = session.filled.get("app_immigration_confirmed") or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in data.items()
        if str(key).strip() and str(value).strip()
    }


def _mark_immigration_confirmed(session: Any, name: str, status: str) -> None:
    data = _immigration_confirmed(session)
    data[name] = status
    session.filled["app_immigration_confirmed"] = json.dumps(data)


def _parse_immigration_assignment(
    spoken: str,
    members: list[dict[str, Any]],
    current: str,
) -> tuple[str, str, str | None]:
    """Return ``(member, status, clarify)``. A clarify means nothing was clicked."""
    status = _map_immigration_status((spoken or "").lower())
    names = [
        str(row.get("name") or "").strip()
        for row in members
        if str(row.get("name") or "").strip()
    ]
    if not status:
        return (
            "",
            "",
            "Please say one status: U.S. Citizen, U.S. National, "
            "Satisfactory immigration status, or Prefer not to answer.",
        )
    residue = _immigration_name_residue(spoken)
    pronoun = residue.lower() in {
        "",
        "he",
        "she",
        "they",
        "him",
        "her",
        "them",
        "that person",
        "this person",
    }
    if pronoun:
        target = (current or "").strip()
        if not target:
            return "", "", "Please say which household member that status is for."
        same = [name for name in names if name.lower() == target.lower()]
        if len(same) > 1:
            return (
                "",
                "",
                f"I see more than one person named {target}. "
                "Please say the full name exactly as shown on the card.",
            )
        return target, status, None
    hits, clarify = _match_named_members(residue, names)
    if clarify:
        return "", "", clarify
    if len(hits) != 1:
        return (
            "",
            "",
            "Please say one household member at a time, using the name on their card.",
        )
    return hits[0], status, None


def _map_income_type(lowered: str) -> str | None:
    income_types = (
        "Self-employed",
        "Employed",
        "Unemployed",
        "SSA",
        "SSDI",
        "Pension",
        "Alimony",
        "Retirement",
        "Rental",
        "Worker's comp",
        "Other",
        "None",
    )
    if "worker" in lowered:
        return "Worker's comp"
    if "self" in lowered:
        return "Self-employed"
    for label in income_types:
        if label.lower() in lowered:
            return label
    return None


def _map_circumstance_labels(lowered: str) -> list[str]:
    mapping = (
        (
            "needs help with daily activities",
            ("daily activit", "daily help", "help with daily"),
        ),
        ("veteran or military", ("veteran", "military")),
        (
            "American Indian or Alaska Native",
            ("american indian", "alaska native", "native american"),
        ),
        ("full-time student", ("full-time student", "full time student", "student")),
        ("None of the above", ("none of the above", "none")),
    )
    hits: list[str] = []
    for label, cues in mapping:
        if any(cue in lowered for cue in cues):
            if label not in hits:
                hits.append(label)
    # Prefer explicit none alone.
    if "None of the above" in hits and len(hits) > 1:
        hits = [item for item in hits if item != "None of the above"]
    return hits


async def _start_income_seq(
    session: Any, kind: str
) -> tuple[str, str]:
    """Begin sequential income detail questions. Returns (step, spoken)."""
    seq = _INCOME_EMPLOYED_SEQ if kind == "employed" else _INCOME_SELF_SEQ
    first_key, first_step, first_q = seq[0]
    _set_income_seq(
        session,
        {"kind": kind, "index": 0, "values": {}, "keys": [item[0] for item in seq]},
    )
    return first_step, first_q


async def _continue_income_seq(
    call_id: str,
    session: Any,
    page: Page,
    answer: str,
) -> dict[str, Any]:
    """Store one income detail field, then ask the next or save."""
    from .live_form import _click_cta

    state = _income_seq_state(session)
    kind = str(state.get("kind") or "")
    if kind == "employed":
        seq = _INCOME_EMPLOYED_SEQ
    elif kind == "other":
        seq = (
            (
                "pay",
                "app_income_self",
                "What amount should I enter for that income?",
            ),
        )
    else:
        seq = _INCOME_SELF_SEQ
    index = int(state.get("index") or 0)
    values = dict(state.get("values") or {})
    if index < 0 or index >= len(seq):
        _set_income_seq(session, None)
        return _app_ok(
            call_id,
            session,
            answer,
            "app_income_more",
            "Income sequence reset",
            spoken_override=app_question_for_step("app_income_more"),
        )
    key, _step_id, _q = seq[index]
    text = (answer or "").strip()
    if not text:
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + app_question_for_step(session.app_step or _step_id),
            "error": "income_field_empty",
            "step": session.app_step or _step_id,
            "phase": "application",
        }
    values[key] = text
    filled = await _fill_next_visible_input(page, text)
    if not filled and key in ("pay", "hours"):
        # Numeric fields should land in an input; ask again if nothing took.
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": (
                "ASK: I couldn't enter that value on the form. "
                + app_question_for_step(session.app_step or _step_id)
            ),
            "error": "income_field_not_filled",
            "step": session.app_step or _step_id,
            "phase": "application",
        }
    next_index = index + 1
    if next_index < len(seq):
        nkey, nstep, nq = seq[next_index]
        _set_income_seq(
            session,
            {
                "kind": kind,
                "index": next_index,
                "values": values,
                "keys": [item[0] for item in seq],
            },
        )
        return _app_ok(
            call_id,
            session,
            answer,
            nstep,
            f"Collected income field {key}",
            spoken_override=nq,
        )
    # All fields collected — save and ask about another source.
    try:
        await _click_cta(page, "Save", attempts=2)
    except Exception:
        pass
    _set_income_seq(session, None)
    return _app_ok(
        call_id,
        session,
        answer,
        "app_income_more",
        "Saved income details",
        spoken_override=app_question_for_step("app_income_more"),
    )


def _app_ok(
    call_id: str,
    session: Any,
    answer: str,
    next_step: str,
    message: str,
    *,
    spoken_override: str | None = None,
    end_call: bool = False,
) -> dict[str, Any]:
    session.app_step = next_step
    if next_step:
        session.elig_step = session.elig_step  # leave Stage D marker alone if any
    if spoken_override is not None:
        spoken = spoken_override
    elif next_step:
        spoken = app_question_for_step(next_step, session)
    else:
        spoken = CLOSING_SUBMITTED if end_call else CLOSING_NOT_SUBMITTED
    if next_step and spoken:
        _set_pending_question(session, next_step, spoken)
    elif not next_step:
        _set_pending_question(session, "", "")
    return {
        "ok": True,
        "call_id": call_id,
        "field_id": "application_answer",
        "value": answer,
        "action": "verified",
        "message": message,
        "spoken": "ASK: " + spoken,
        "step": next_step,
        "phase": "application" if next_step else "closing",
        "end_call": end_call and not next_step,
    }


_STAGE_E_YES_LEAD = re.compile(r"^\s*yes(?:\b|[\s—–\-:,.])", re.I)
_STAGE_E_NO_LEAD = re.compile(r"^\s*no(?:\b|[\s—–\-:,.])", re.I)


def resolve_stage_e_yes_no_label(yes: bool, labels: list[str]) -> str:
    """Map spoken Yes/No onto one visible Stage E option, including long cards.

    Matches exact ``Yes`` / ``No`` and descriptive cards such as
    ``Yes Everyone listed is a U.S. Citizen``. Does not change Stage D matching.
    """
    polarity = "yes" if yes else "no"
    lead = _STAGE_E_YES_LEAD if yes else _STAGE_E_NO_LEAD
    hits: list[str] = []
    for raw in labels or []:
        text = re.sub(r"\s+", " ", str(raw or "").strip())
        if not text or len(text) > 140:
            continue
        lowered = text.lower()
        if lowered == polarity or lead.match(text):
            if text not in hits:
                hits.append(text)
    if not hits:
        raise RuntimeError(
            f"YES_NO_CLARIFY: I could not find a visible {polarity!r} option "
            "for this question. Please say Yes or No again."
        )
    if len(hits) == 1:
        return hits[0]
    # Bare Yes/No plus one descriptive card for the same polarity → use the card.
    descriptive = [item for item in hits if item.lower() != polarity]
    if len(descriptive) == 1:
        return descriptive[0]
    raise RuntimeError(
        f"YES_NO_CLARIFY: Several {polarity!r} options are visible. "
        "Please say the full choice, or say Yes or No again."
    )


async def _click_stage_e_option_label(page: Page, label: str) -> bool:
    """Click the control whose full visible text matches ``label``.

    Does not use ``_choice_phrase`` / ``_click_exact_choice``, so descriptive
    Stage E cards keep their full text (e.g. citizenship Yes cards).
    """
    try:
        clicked = await page.evaluate(
            """(label) => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const want = norm(label);
              if (!want) return false;
              const nodes = [...document.querySelectorAll(
                'button, [role=button], a, div, span, label'
              )];
              const hits = [];
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (r.width < 8 || r.height < 8) continue;
                const text = norm(el.innerText || el.textContent || '');
                if (text !== want || text.length > 160) continue;
                hits.push({ el, area: r.width * r.height });
              }
              hits.sort((a, b) => a.area - b.area);
              if (!hits.length) return false;
              hits[0].el.click();
              return true;
            }""",
            label,
        )
        if clicked:
            return True
    except Exception:
        logger.exception("stage_e option DOM click failed label=%r", label[:80])
    try:
        loc = page.get_by_text(label, exact=True).first
        if await loc.count() and await loc.is_visible():
            await loc.click(timeout=6000)
            return True
    except Exception:
        pass
    return False


async def _click_yes_no(page: Page, yes: bool) -> str:
    """Click the unique Stage E Yes/No option and verify it took effect."""
    from .live_form import _choice_signature, _visible_choice_labels

    labels = await _visible_choice_labels(page)
    target = resolve_stage_e_yes_no_label(yes, labels)
    before = await _choice_signature(page, target)
    screen_before = await detect_application_screen(page)
    logger.info(
        "stage_e_yes_no_click polarity=%s target=%r screen=%s",
        "yes" if yes else "no",
        target[:80],
        screen_before or "-",
    )
    clicked = await _click_stage_e_option_label(page, target)
    if not clicked:
        raise RuntimeError(
            "YES_NO_CLARIFY: I could not select that Yes or No option. "
            "Please say Yes or No again."
        )
    await page.wait_for_timeout(450)
    after = await _choice_signature(page, target)
    screen_after = await detect_application_screen(page)
    if screen_after and screen_before and screen_after != screen_before:
        return target
    if after != before and after != "__missing__":
        return target
    if after and after != "__missing__":
        return target
    # Successful navigation often removes the old card entirely.
    if after == "__missing__":
        await page.wait_for_timeout(500)
        screen_after = await detect_application_screen(page)
        if screen_after and screen_after != screen_before:
            return target
        new_labels = await _visible_choice_labels(page)
        if target not in new_labels:
            return target
    raise RuntimeError(
        "YES_NO_CLARIFY: That Yes or No choice did not register. "
        "Please say Yes or No again."
    )


def _yes_no_click_failure(
    call_id: str,
    session: Any,
    step: str,
    answer: str,
    exc: BaseException,
) -> dict[str, Any]:
    # Never speak raw targets, bodies, or exception dumps to the caller.
    _ = exc
    return {
        "ok": False,
        "call_id": call_id,
        "field_id": "application_answer",
        "value": answer,
        "error": "yes_no_click_failed",
        "spoken": (
            "ASK: Please say Yes or No again. " + app_question_for_step(step)
        ),
        "message": "yes_no_click_failed",
        "step": step,
        "phase": "application",
    }


# Backend-only Collect Mode sub-steps that share a parent Pointcare screen.
_APP_SUBSTEPS: dict[str, str] = {
    "app_immigration_who": "app_immigration",
    "app_insurance_members": "app_insurance",
    "app_insurance_coverage_type": "app_insurance",
    "app_dob_collect": "app_dob_review",
    "app_circumstances_person": "app_circumstances",
    "app_income_employed": "app_income",
    "app_income_employed_hours": "app_income",
    "app_income_employed_frequency": "app_income",
    "app_income_employed_employer": "app_income",
    "app_income_self": "app_income",
    "app_income_self_work": "app_income",
    "app_income_more": "app_income",
    "app_expenses_amount": "app_expenses",
}


async def _reconcile_app_step(session: Any, page: Page) -> str:
    """Align session.app_step with the visible Stage E screen before answering."""
    current = (session.app_step or "").strip()
    screen = await detect_application_screen(page)
    radios = False
    if screen in ("app_citizenship", "app_immigration") or current in (
        "app_citizenship",
        "app_immigration",
        "app_immigration_who",
    ):
        radios = await immigration_member_radios_visible(page)
    if expanded_immigration_active(session, current, screen, radios):
        # Yes/No heading can remain after No. That is not a return to question 1.
        if current in ("app_immigration", "app_immigration_who"):
            return current
        session.app_step = "app_immigration"
        logger.info(
            "application_immigration_restored call_id=%s from=%s",
            getattr(session, "call_id", "-"),
            current or "-",
        )
        return "app_immigration"
    if screen and screen.startswith("app_") and screen != "programs":
        parent = _APP_SUBSTEPS.get(current, "")
        # Keep Collect Mode sub-steps while still on the parent Pointcare screen.
        if parent and (screen == parent or screen == current):
            return current
        # Insurance member/coverage sub-steps stay while Yes expands the form.
        if current in (
            "app_insurance_members",
            "app_insurance_coverage_type",
        ) and screen in ("app_insurance", "app_insurance_members"):
            return current
        if current != screen:
            logger.info(
                "stage_e_reconcile call_id=%s from=%s to=%s",
                getattr(session, "call_id", "-"),
                current or "-",
                screen,
            )
            session.app_step = screen
        return session.app_step
    # Never invent citizenship when the UI cannot be identified.
    return current or screen or ""


async def _recover_if_browser_advanced(
    call_id: str,
    session: Any,
    page: Page,
    answer: str,
    expected_step: str,
) -> dict[str, Any] | None:
    """If Pointcare already left ``expected_step``, return the next question."""
    screen = await detect_application_screen(page)
    if (
        screen
        and screen.startswith("app_")
        and screen not in ("programs", expected_step)
    ):
        logger.info(
            "stage_e_yes_no_recovered_after_nav call_id=%s from=%s to=%s",
            call_id,
            expected_step,
            screen,
        )
        step, speech = await _prepare_resume_speech(session, page, screen)
        return _app_ok(
            call_id,
            session,
            answer,
            step,
            "Recovered after page advanced",
            spoken_override=speech,
        )
    return None


async def _click_next_or_continue(page: Page) -> bool:
    """Click Next/Continue only when enabled. Returns True if a click was attempted."""
    from .live_form import _click_cta

    enabled = await _next_button_enabled(page)
    if enabled is False:
        logger.info("stage_e_next_disabled skip_click")
        return False
    for label in ("Next", "Continue", "Save and Return", "Save"):
        try:
            await _click_cta(page, label, attempts=1)
            await page.wait_for_timeout(600)
            return True
        except Exception:
            continue
    return False


async def _advance_after_answer(page: Page, session: Any) -> str:
    """Detect the next app step after a verified browser action."""
    await page.wait_for_timeout(700)
    screen = await detect_application_screen(page)
    if screen == "programs":
        # Still on summary — unexpected mid Stage E.
        return session.app_step or ""
    if screen:
        return screen
    # Try Next once only when enabled and the page looks stuck.
    try:
        await _click_next_or_continue(page)
    except Exception:
        pass
    screen = await detect_application_screen(page)
    return screen or (session.app_step or "")


async def handle_programs_decision(
    call_id: str,
    session: Any,
    page: Page,
    answer: str,
) -> dict[str, Any]:
    """Yes → Start Applications + first Stage E question; No → polite close; else clarify."""
    from .live_form import classify_yes_no

    lowered = (answer or "").strip().lower()
    decision = classify_yes_no(answer)
    wants_start = decision == "yes" or bool(
        re.search(r"\b(continue|start|proceed|application)\b", lowered)
        and decision != "no"
    )
    # "start the application" without a clear No counts as Yes.
    if decision is None and re.search(
        r"\b(start (the )?application|continue to (the )?application)\b", lowered
    ):
        wants_start = True
    if decision is None and not wants_start:
        summary = summary_from_session_filled(session.filled)
        ctx = programs_context_reply(answer, summary)
        speech = ctx or (
            (session.filled.get("programs_permission_speech") or "").strip()
            or build_programs_permission_speech(summary)
        )
        if not ctx:
            speech = f"{PROGRAMS_CLARIFY} {speech}"
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "eligibility_answer",
            "value": answer,
            "error": "programs_clarify",
            "spoken": "ASK: " + speech,
            "message": "programs clarify",
            "step": "programs",
            "phase": "eligibility",
        }

    if not wants_start or decision == "no":
        # Do not click Start Applications or Back — leave eligibility results intact.
        session.filled["application_started"] = "0"
        session.app_step = ""
        session.elig_step = ""
        logger.info(
            "programs_declined call_id=%s leave_results=1",
            call_id,
        )
        return {
            "ok": True,
            "call_id": call_id,
            "field_id": "eligibility_answer",
            "value": answer,
            "action": "declined_application",
            "message": "Caller declined to start the application",
            "spoken": "ASK: " + CLOSING_NO_APPLICATION,
            "step": "",
            "phase": "closing",
            "end_call": True,
        }

    # Yes — start application once.
    if session.filled.get("application_started") == "1" and session.app_step:
        return _app_ok(
            call_id,
            session,
            answer,
            session.app_step,
            "Application already started",
            spoken_override=app_question_for_step(session.app_step),
        )

    clicked = await click_start_applications(page)
    started = await wait_for_application_started(page)
    if not started:
        logger.error(
            "start_applications_not_verified call_id=%s clicked=%s url=%s",
            call_id,
            clicked,
            (page.url or "")[:120],
        )
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "eligibility_answer",
            "value": answer,
            "error": "start_applications_failed",
            "spoken": (
                "ASK: I couldn't confirm that the application started. "
                + PROGRAMS_CLARIFY
            ),
            "message": "Start Applications click not verified",
            "step": "programs",
            "phase": "eligibility",
        }

    session.filled["application_started"] = "1"
    session.filled["start_applications_clicked"] = clicked
    session.elig_step = ""
    screen = await detect_application_screen(page)
    next_step = screen if screen and screen != "programs" else "app_citizenship"
    session.app_step = next_step
    if next_step == "app_citizenship":
        first_question = _citizenship_question(
            await _citizenship_household_names(page, session)
        )
    else:
        first_question = app_question_for_step(next_step)
    intro = "Okay, I've started the application. " + first_question
    logger.info(
        "stage_e_started call_id=%s clicked=%s first_step=%s",
        call_id,
        clicked,
        next_step,
    )
    return _app_ok(
        call_id,
        session,
        answer,
        next_step,
        f"Started application via {clicked}",
        spoken_override=intro,
    )


def _address_field_from_text(text: str) -> str:
    lowered = (text or "").lower()
    home = bool(re.search(r"\bhome\b", lowered))
    mailing = bool(re.search(r"\bmailing\b", lowered))
    both = bool(re.search(r"\bboth\b", lowered))
    if both or (home and mailing):
        return "both"
    if home:
        return "home"
    if mailing:
        return "mailing"
    return ""


def _address_error(
    call_id: str, step: str, error: str, spoken: str
) -> dict[str, Any]:
    return {
        "ok": False,
        "call_id": call_id,
        "field_id": "application_answer",
        "spoken": "ASK: " + spoken,
        "error": error,
        "step": step,
        "phase": "application",
    }


async def _handle_address_step(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    lowered: str,
    step: str,
) -> dict[str, Any]:
    """Review, edit, and save one member address card without advancing early."""
    from .live_form import _is_yes

    cards = await _scrape_address_cards(page)
    names = [str(card.get("name") or "").strip() for card in cards if card.get("name")]
    missing = [name for name in names if (_card_by_name(cards, name) or {}).get("needs_add")]
    current = str(session.filled.get("app_address_current") or "").strip()
    looks_ok = bool(re.search(r"\blooks? (good|correct|fine)\b", lowered))
    wants_next = bool(_is_yes(text) or looks_ok or re.search(r"\b(continue|next)\b", lowered))
    wants_change = bool(
        re.search(r"\b(change|wrong|fix|edit|different|update|correct)\b", lowered)
    ) and not looks_ok

    if re.search(r"\bcancel\b", lowered):
        target = current
        if not target and len(names) == 1:
            target = names[0]
        if not target:
            return _address_error(
                call_id,
                step,
                "address_member_needed",
                "Whose address change should I cancel?",
            )
        if not await _click_member_address_action(page, target, "cancel"):
            logger.warning("address_cancel_failed")
            return _address_error(
                call_id,
                step,
                "address_cancel_failed",
                f"I couldn't cancel the address edit for {target}.",
            )
        await page.wait_for_timeout(300)
        after_cards = await _scrape_address_cards(page)
        after = _card_by_name(after_cards, target) or {}
        snapshot: dict[str, Any] = {}
        try:
            loaded = json.loads(session.filled.get("app_address_snapshot") or "{}")
            if isinstance(loaded, dict):
                snapshot = loaded
        except Exception:
            snapshot = {}
        if snapshot and str(snapshot.get("name") or "").strip().lower() == target.lower():
            if (
                str(after.get("home") or "") != str(snapshot.get("home") or "")
                or str(after.get("mailing") or "") != str(snapshot.get("mailing") or "")
            ):
                logger.warning("address_cancel_not_restored")
                return _address_error(
                    call_id,
                    step,
                    "address_cancel_failed",
                    f"Cancel did not restore the saved address for {target}.",
                )
        elif after.get("expanded"):
            logger.warning("address_cancel_still_open")
            return _address_error(
                call_id,
                step,
                "address_cancel_failed",
                f"Cancel did not close the address edit for {target}.",
            )
        session.filled.pop("app_address_current", None)
        session.filled.pop("app_address_field", None)
        session.filled.pop("app_address_snapshot", None)
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "address edit cancelled",
            spoken_override=(
                f"I cancelled that change for {target}. Nothing new was saved. "
                + _address_confirm_speech(after or {"name": target})
            ),
        )

    no_home = bool(re.search(r"\bno home address\b", lowered))
    no_mailing = bool(re.search(r"\bno mailing address\b", lowered))
    if no_home or no_mailing:
        name_text = re.sub(
            r"\bno (?:home|mailing) address\b", " ", text, flags=re.I
        )
        hits, clarify = _match_named_members(name_text, names)
        target = hits[0] if len(hits) == 1 else current
        if clarify:
            return _address_error(call_id, step, "address_member_ambiguous", clarify)
        if not target:
            if len(names) == 1:
                target = names[0]
            else:
                return _address_error(
                    call_id,
                    step,
                    "address_member_needed",
                    "Whose card should get that no-address choice? Please say the name.",
                )
        prior = _card_by_name(cards, target) or {"name": target}
        session.filled["app_address_snapshot"] = json.dumps(
            {
                "name": target,
                "home": str(prior.get("home") or ""),
                "mailing": str(prior.get("mailing") or ""),
            }
        )
        if not await _open_member_address_editor(page, target):
            logger.warning("address_editor_not_opened")
            return _address_error(
                call_id,
                step,
                "address_no_home_failed" if no_home else "address_no_mailing_failed",
                f"I couldn't open the address card for {target}.",
            )
        kind = "home" if no_home else "mailing"
        if not await _set_member_no_address(page, target, kind):
            logger.warning("address_checkbox_failed")
            return _address_error(
                call_id,
                step,
                "address_no_home_failed" if no_home else "address_no_mailing_failed",
                f"I couldn't select that choice for {target}.",
            )
        if not await _click_member_address_action(page, target, "save"):
            logger.warning("address_save_click_failed")
            return _address_error(
                call_id,
                step,
                "address_save_failed",
                f"I selected that choice for {target}, but Save did not complete.",
            )
        await page.wait_for_timeout(300)
        if await _address_validation_visible(page, target):
            logger.warning("address_validation_still_visible")
            return _address_error(
                call_id,
                step,
                "address_save_failed",
                f"Pointcare still shows an address error for {target}.",
            )
        refreshed = await _scrape_address_cards(page)
        if not _other_addresses_unchanged(cards, refreshed, target):
            logger.warning("address_other_member_changed")
            return _address_error(
                call_id,
                step,
                "address_wrong_member",
                "That change affected another person's address card. I did not continue.",
            )
        card = _card_by_name(refreshed, target) or {}
        confirmed_flag = card.get("no_home") if no_home else card.get("no_mailing")
        if not confirmed_flag:
            logger.warning("address_checkbox_not_saved")
            return _address_error(
                call_id,
                step,
                "address_not_verified",
                f"I could not verify that choice was saved for {target}.",
            )
        session.filled["app_address_current"] = target
        session.filled.pop("app_address_snapshot", None)
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "address choice saved",
            spoken_override=_address_confirm_speech(card),
        )

    street, unit = _split_street_and_unit(text)
    if _looks_like_street(street):
        hits, clarify = _match_named_members(text, names)
        if clarify:
            return _address_error(call_id, step, "address_member_ambiguous", clarify)
        target = hits[0] if len(hits) == 1 else current
        if not target:
            needing = [
                name
                for name in names
                if (_card_by_name(cards, name) or {}).get("needs_add")
            ]
            if len(needing) == 1:
                target = needing[0]
            elif len(names) == 1:
                target = names[0]
            else:
                return _address_error(
                    call_id,
                    step,
                    "address_member_needed",
                    "Whose address is that? Please say the name.",
                )
        field = _address_field_from_text(text) or str(
            session.filled.get("app_address_field") or ""
        )
        card = _card_by_name(cards, target) or {}
        if not field:
            if card.get("needs_home") and not card.get("needs_mailing"):
                field = "home"
            elif card.get("needs_mailing") and not card.get("needs_home"):
                field = "mailing"
            else:
                session.filled["app_address_current"] = target
                return _address_error(
                    call_id,
                    step,
                    "address_field_needed",
                    f"Should I use that for {target}'s home address, mailing address, or both?",
                )
        prior = _card_by_name(cards, target) or {"name": target}
        session.filled["app_address_snapshot"] = json.dumps(
            {
                "name": target,
                "home": str(prior.get("home") or ""),
                "mailing": str(prior.get("mailing") or ""),
            }
        )
        result = await _commit_member_address(page, target, field, street, unit)
        if not result.get("ok"):
            error = str(result.get("error") or "address_save_failed")
            if error == "address_ambiguous":
                spoken = (
                    f"I see more than one match for {target}. "
                    "Please say the city or ZIP so I can choose the right one."
                )
            elif error == "address_no_suggestion":
                spoken = (
                    f"I didn't get a single matching address suggestion for {target}. "
                    "Please say the street again, or say No Home Address or No Mailing Address."
                )
            elif error == "address_wrong_member":
                spoken = "That update changed another person's address. I stayed on this question."
            else:
                spoken = f"I couldn't save that address for {target}. Please say it again."
            session.filled["app_address_current"] = target
            session.filled["app_address_field"] = field
            return _address_error(call_id, step, error, spoken)
        saved_card = result.get("card") or {}
        session.filled["app_address_current"] = target
        session.filled.pop("app_address_field", None)
        session.filled.pop("app_address_ready", None)
        session.filled.pop("app_address_snapshot", None)
        mailing_note = ""
        if field == "home":
            if saved_card.get("mailing_same"):
                mailing_note = " The mailing address still shows the same mailing address."
            elif saved_card.get("needs_mailing"):
                mailing_note = " The mailing address did not change with it. Say the mailing address if it should be different."
            else:
                mailing_note = " I left the mailing address as it is."
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "address saved",
            spoken_override=(
                f"Okay, that address is saved for {target}.{mailing_note} "
                + _address_confirm_speech(saved_card)
            ),
        )

    if wants_change or (not wants_next and _match_named_members(text, names)[0]):
        hits, clarify = _match_named_members(text, names)
        if clarify:
            return _address_error(call_id, step, "address_member_ambiguous", clarify)
        if not hits:
            return _address_error(
                call_id,
                step,
                "address_member_needed",
                "Whose address should I change? Please say the name.",
            )
        target = hits[0]
        field = _address_field_from_text(text)
        session.filled["app_address_current"] = target
        if field:
            session.filled["app_address_field"] = field
            prompt = f"What is the new {field} address for {target}?"
        else:
            session.filled.pop("app_address_field", None)
            prompt = (
                f"Should I change the home address, the mailing address, or both for {target}?"
            )
        return _address_error(call_id, step, "address_field_needed", prompt)

    if wants_next and missing:
        shown = ", ".join(missing[:4])
        return _address_error(
            call_id,
            step,
            "address_incomplete",
            "Pointcare still needs an address for "
            f"{shown}. Say the street address, or say "
            "No Home Address or No Mailing Address if that applies.",
        )

    confirmed = {name.lower() for name in _address_confirmed_names(session)}
    if wants_next and current and current.lower() not in confirmed:
        _mark_address_confirmed(session, current)
        confirmed.add(current.lower())
        session.filled.pop("app_address_current", None)
        current = ""
    pending = [name for name in names if name.lower() not in confirmed]
    ready = bool(session.filled.get("app_address_ready")) and not pending and not missing
    if wants_next and ready:
        enabled = await _next_button_enabled(page)
        if enabled is False:
            return _address_error(
                call_id,
                step,
                "address_next_disabled",
                "Next is still disabled — an address may still be missing. "
                + app_question_for_step(step, session),
            )
        clicked = await _click_stage_next(page)
        if not clicked:
            logger.warning("address_next_not_clicked")
            return _address_error(
                call_id,
                step,
                "address_next_disabled",
                "I can't continue yet because Next is not available. "
                + app_question_for_step(step, session),
            )
        screen = await detect_application_screen(page)
        if not screen or screen == "app_address_review":
            logger.warning("address_screen_unchanged")
            return _address_error(
                call_id,
                step,
                "address_still_here",
                "The address screen is still open, so I did not move ahead.",
            )
        session.filled.pop("app_address_ready", None)
        session.filled.pop("app_address_current", None)
        return _app_ok(
            call_id,
            session,
            text,
            screen,
            "address review advanced",
            spoken_override=app_question_for_step(screen, session),
        )
    if wants_next and not pending and names:
        session.filled["app_address_ready"] = "1"
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "addresses confirmed",
            spoken_override=(
                "Those address cards are confirmed. Say Yes to continue."
            ),
        )
    if wants_next and pending:
        nxt = pending[0]
        session.filled["app_address_current"] = nxt
        session.filled.pop("app_address_ready", None)
        card = _card_by_name(cards, nxt) or {"name": nxt}
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "address confirm one member",
            spoken_override=_address_confirm_speech(card),
        )
    if current:
        card = _card_by_name(cards, current) or {"name": current}
        return _address_error(call_id, step, "address_clarify", _address_confirm_speech(card))
    return _address_error(
        call_id,
        step,
        "address_clarify",
        app_question_for_step(step, session),
    )


def _immigration_unmet(members: list[dict[str, Any]], confirmed: dict[str, str]) -> list[str]:
    """Members whose visible radio does not match a caller-confirmed status."""
    confirmed_by_name = {
        str(key).strip().lower(): value for key, value in confirmed.items()
    }
    unmet: list[str] = []
    for row in members:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        expected = confirmed_by_name.get(name.lower(), "")
        live = str(row.get("selected") or "")
        if not expected or not _same_immigration_status(live, expected):
            unmet.append(name)
    return unmet


async def _complete_immigration_step(
    call_id: str,
    session: Any,
    page: Page,
    answer: str,
) -> dict[str, Any]:
    """Advance only when every live radio matches and Next itself is enabled."""
    members = await _scrape_immigration_members(page)
    if not members:
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": (
                "ASK: I can't see the household member status choices on this "
                "screen, so I did not click Next."
            ),
            "error": "immigration_members_missing",
            "step": "app_immigration",
            "phase": "application",
        }
    unmet = _immigration_unmet(members, _immigration_confirmed(session))
    if unmet:
        current = unmet[0]
        session.filled["app_immigration_current"] = current
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": (
                "ASK: I still need a status that is selected on the form for "
                f"{current}. " + _immigration_status_question_for(current)
            ),
            "error": "immigration_incomplete",
            "step": "app_immigration",
            "phase": "application",
        }
    if session.filled.get("app_immigration_next_clicked") == "1":
        screen = await detect_application_screen(page)
        if screen and screen not in (
            "app_citizenship",
            "app_immigration",
            "app_immigration_who",
        ):
            step, speech = await _prepare_resume_speech(session, page, screen)
            session.app_step = step
            return _app_ok(
                call_id,
                session,
                answer,
                step,
                "Immigration already advanced",
                spoken_override=speech,
            )
        validation = await _visible_form_validation(page)
        speech = "I already tried Next and this screen did not change."
        if validation:
            speech = f"{speech} The form says: {validation}"
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "immigration_next_unchanged",
            "step": "app_immigration",
            "phase": "application",
        }
    enabled = await _next_button_enabled(page)
    if enabled is not True:
        validation = await _visible_form_validation(page)
        speech = (
            "Next is still disabled after every person's status was verified "
            "on the form. I did not click it."
        )
        if validation:
            speech = f"{speech} The form says: {validation}"
        logger.info("immigration_next_still_disabled")
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "immigration_next_disabled",
            "step": "app_immigration",
            "phase": "application",
        }
    clicked = await _click_enabled_application_next(page)
    if not clicked:
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: I could not click the enabled Next button.",
            "error": "immigration_next_click_failed",
            "step": "app_immigration",
            "phase": "application",
        }
    session.filled["app_immigration_next_clicked"] = "1"
    screen = await detect_application_screen(page)
    if not screen or screen in (
        "app_citizenship",
        "app_immigration",
        "app_immigration_who",
    ):
        validation = await _visible_form_validation(page)
        speech = "I clicked Next, but the screen did not change."
        if validation:
            speech = f"{speech} The form says: {validation}"
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "immigration_next_unchanged",
            "step": "app_immigration",
            "phase": "application",
        }
    session.filled.pop("app_citizenship_no", None)
    session.filled.pop("app_immigration_next_clicked", None)
    _set_immigration_queue(session, None)
    step, speech = await _prepare_resume_speech(session, page, screen)
    session.app_step = step
    return _app_ok(
        call_id,
        session,
        answer,
        step,
        "Advanced after immigration statuses",
        spoken_override=speech,
    )


async def submit_application_answer(
    call_id: str,
    session: Any,
    page: Page,
    answer: str,
    started: float,
) -> dict[str, Any]:
    """Apply one Collect Mode Stage E answer and return the next question."""
    from .live_form import (
        _click_cta,
        _click_exact_choice,
        _is_no,
        _is_yes,
        _mark,
        classify_yes_no,
    )

    # Browser may already have advanced (e.g. delayed Yes after citizenship).
    step = await _reconcile_app_step(session, page)
    if step == "programs":
        return _unidentified_screen_error(call_id, session, answer)
    if not step:
        # Keep a prior Collect Mode sub-step only if already set; otherwise fail
        # closed instead of inventing citizenship.
        prior = (session.app_step or "").strip()
        if prior.startswith("app_"):
            step = prior
        else:
            return _unidentified_screen_error(call_id, session, answer)
    session.app_step = step
    text = (answer or "").strip()
    lowered = text.lower()
    _mark(call_id, "browser_action", started)

    # Contextual / unclear on yes-no steps — do not navigate.
    if step in YES_NO_APP_STEPS:
        decision = classify_yes_no(text)
        if decision is None and step == "app_submit_confirm":
            if re.search(r"\b(review|check|go back|change|correct)\b", lowered):
                return _app_ok(
                    call_id,
                    session,
                    text,
                    step,
                    "submit deferred for review",
                    spoken_override=(
                        "Okay, I will not submit yet. Tell me what you'd like to "
                        "review or change, or say Yes when you want me to submit."
                    ),
                )
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + app_question_for_step(step),
                "error": "submit_clarify",
                "step": step,
                "phase": "application",
            }

    next_step = step
    spoken_override: str | None = None
    end_call = False

    if step == "app_agent_offer":
        if _is_yes(text):
            try:
                await _click_exact_choice(page, "Yes, connect me")
            except Exception:
                try:
                    await _click_yes_no(page, True)
                except RuntimeError as exc:
                    return _yes_no_click_failure(call_id, session, step, text, exc)
        elif _is_no(text):
            clicked_no = False
            for label in ("No thanks", "No", "Continue without an agent"):
                try:
                    await _click_cta(page, label, attempts=1)
                    clicked_no = True
                    break
                except Exception:
                    continue
            if not clicked_no:
                try:
                    await _click_yes_no(page, False)
                except RuntimeError as exc:
                    return _yes_no_click_failure(call_id, session, step, text, exc)
        else:
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + app_question_for_step(step),
                "error": "agent_offer_clarify",
                "step": step,
                "phase": "application",
            }
        try:
            await _click_cta(page, "Continue", attempts=2)
        except Exception:
            await _click_next_or_continue(page)
        next_step = await _advance_after_answer(page, session)

    elif step == "app_citizenship":
        if _is_yes(text):
            session.filled.pop("app_citizenship_no", None)
            try:
                await _click_yes_no(page, True)
            except RuntimeError as exc:
                recovered = await _recover_if_browser_advanced(
                    call_id, session, page, text, step
                )
                if recovered is not None:
                    return recovered
                return _yes_no_click_failure(call_id, session, step, text, exc)
            await _click_next_or_continue(page)
            next_step = await _advance_after_answer(page, session)
        elif _is_no(text):
            try:
                await _click_yes_no(page, False)
            except RuntimeError as exc:
                recovered = await _recover_if_browser_advanced(
                    call_id, session, page, text, step
                )
                if recovered is not None:
                    return recovered
                return _yes_no_click_failure(call_id, session, step, text, exc)
            session.filled["app_citizenship_no"] = "1"
            session.filled.pop("app_immigration_confirmed", None)
            await page.wait_for_timeout(500)
            members = await _scrape_immigration_members(page)
            # Do not treat preselected U.S. Citizen radios as verified answers.
            # Walk every household member card Pointcare actually showed.
            names = [
                str(m.get("name") or "").strip()
                for m in members
                if str(m.get("name") or "").strip()
            ]
            lowered_names = [n.lower() for n in names]
            if not names:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: I selected No, but the individual household member "
                        "cards are not on the screen yet. Please say No again."
                    ),
                    "error": "citizenship_members_missing",
                    "step": step,
                    "phase": "application",
                }
            if len(set(lowered_names)) < len(lowered_names):
                next_step = "app_immigration_who"
                spoken_override = (
                    "I see more than one person with the same name on this "
                    "screen. Please say the full name as shown for each person "
                    "whose status I should set, one at a time."
                )
                _set_pending_question(session, next_step, spoken_override)
            else:
                _set_immigration_queue(session, names)
                current = names[0]
                session.filled["app_immigration_current"] = current
                next_step = "app_immigration"
                spoken_override = (
                    "Okay. I'll confirm each person's citizenship or immigration "
                    "status one at a time. "
                    + _immigration_status_question_for(current)
                )
                _set_pending_question(session, next_step, spoken_override)
        else:
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + app_question_for_step(step, session),
                "error": "citizenship_clarify",
                "step": step,
                "phase": "application",
            }

    elif step == "app_immigration_who":
        if re.search(r"\b(continue|next|done|that's all|finished)\b", lowered):
            enabled = await _next_button_enabled(page)
            if enabled is False:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: Pointcare still needs immigration status for one or "
                        "more people before I can continue. "
                        + app_question_for_step("app_immigration_who", session)
                    ),
                    "error": "immigration_incomplete",
                    "step": step,
                    "phase": "application",
                }
            clicked = await _click_next_or_continue(page)
            if not clicked and enabled is False:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: Next is still disabled. "
                        + app_question_for_step("app_immigration_who", session)
                    ),
                    "error": "immigration_next_disabled",
                    "step": step,
                    "phase": "application",
                }
            session.filled.pop("app_citizenship_no", None)
            _set_immigration_queue(session, None)
            next_step = await _advance_after_answer(page, session)
        else:
            members = await _scrape_immigration_members(page)
            available = [
                str(m.get("name") or "").strip()
                for m in members
                if str(m.get("name") or "").strip()
            ]
            if not available:
                # Fall back to free-form name tokens when scrape finds nothing.
                available = []
            hits, clarify = _match_named_members(text, available or [])
            if clarify:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + clarify,
                    "error": "immigration_who_ambiguous",
                    "step": step,
                    "phase": "application",
                }
            if not hits:
                # Accept spoken names even when scrape missed cards.
                tokens = [
                    tok
                    for tok in re.findall(r"[A-Za-z][A-Za-z'.-]{1,}", text)
                    if tok.lower()
                    not in {
                        "and",
                        "the",
                        "not",
                        "citizen",
                        "citizens",
                        "are",
                        "is",
                        "both",
                    }
                ]
                hits = tokens
            if not hits:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + app_question_for_step(step, session),
                    "error": "immigration_who_clarify",
                    "step": step,
                    "phase": "application",
                }
            _set_immigration_queue(session, hits)
            current = hits[0]
            session.filled["app_immigration_current"] = current
            next_step = "app_immigration"
            spoken_override = _immigration_status_question_for(current)
            _set_pending_question(session, next_step, spoken_override)

    elif step == "app_immigration":
        live_members = await _scrape_immigration_members(page)
        listed_names = [
            str(row.get("name") or "").strip()
            for row in live_members
            if str(row.get("name") or "").strip()
        ]
        confirmed = {
            name.lower(): status
            for name, status in _immigration_confirmed(session).items()
        }
        if re.search(
            r"\b(continue|next|done|that's all|finished)\b", lowered
        ):
            return await _complete_immigration_step(call_id, session, page, text)
        else:
            current = (session.filled.get("app_immigration_current") or "").strip()
            target, picked, clarify = _parse_immigration_assignment(
                text, live_members, current
            )
            if clarify:
                error = (
                    "immigration_duplicate_name"
                    if "more than one person" in clarify.lower()
                    else "immigration_clarify"
                )
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + clarify,
                    "error": error,
                    "step": step,
                    "phase": "application",
                }
            before = {
                str(row.get("name") or "").strip().lower(): str(
                    row.get("selected") or ""
                )
                for row in live_members
            }
            ok = await _set_member_immigration_status(page, target, picked)
            if not ok:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: I couldn't set that status on the form. "
                        + _immigration_status_question_for(target)
                    ),
                    "error": "immigration_status_not_set",
                    "step": step,
                    "phase": "application",
                }
            await page.wait_for_timeout(250)
            verified = await _verify_member_immigration_status(page, target, picked)
            if not verified:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: I couldn't verify that status was selected. "
                        + _immigration_status_question_for(target)
                    ),
                    "error": "immigration_status_not_verified",
                    "step": step,
                    "phase": "application",
                }
            after_members = await _scrape_immigration_members(page)
            for row in after_members:
                other = str(row.get("name") or "").strip()
                if other.lower() == target.lower():
                    continue
                if str(row.get("selected") or "") != before.get(other.lower(), ""):
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": (
                            "ASK: That selection changed another person's answer. "
                            + _immigration_status_question_for(target)
                        ),
                        "error": "immigration_other_member_changed",
                        "step": step,
                        "phase": "application",
                    }
            _mark_immigration_confirmed(session, target, picked)
            confirmed[target.lower()] = picked
            remaining = [
                name
                for name in (listed_names or _immigration_queue(session))
                if name.lower() not in confirmed
            ]
            _set_immigration_queue(session, remaining or None)
            if remaining:
                nxt = remaining[0]
                session.filled["app_immigration_current"] = nxt
                next_step = "app_immigration"
                spoken_override = (
                    f"Okay, {picked} for {target}. "
                    + _immigration_status_question_for(nxt)
                )
                _set_pending_question(session, next_step, spoken_override)
            else:
                session.filled.pop("app_immigration_current", None)
                return await _complete_immigration_step(
                    call_id, session, page, text
                )

    elif step == "app_insurance":
        if _is_yes(text):
            try:
                await _click_yes_no(page, True)
            except RuntimeError as exc:
                recovered = await _recover_if_browser_advanced(
                    call_id, session, page, text, step
                )
                if recovered is not None:
                    return recovered
                return _yes_no_click_failure(call_id, session, step, text, exc)
            await page.wait_for_timeout(400)
            screen = await detect_application_screen(page)
            if screen and screen not in ("", "app_insurance", "programs"):
                next_step = await _advance_after_answer(page, session)
            else:
                members = await _scrape_coverage_members(page)
                names = [
                    str(m.get("name") or "").strip()
                    for m in members
                    if str(m.get("name") or "").strip()
                ]
                next_step = "app_insurance_members"
                spoken_override = _who_has_coverage_question(names)
                _set_pending_question(session, next_step, spoken_override)
        elif _is_no(text):
            try:
                await _click_yes_no(page, False)
            except RuntimeError as exc:
                recovered = await _recover_if_browser_advanced(
                    call_id, session, page, text, step
                )
                if recovered is not None:
                    return recovered
                return _yes_no_click_failure(call_id, session, step, text, exc)
            await _click_next_or_continue(page)
            next_step = await _advance_after_answer(page, session)
        else:
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + app_question_for_step(step),
                "error": "insurance_clarify",
                "step": step,
                "phase": "application",
            }

    elif step == "app_insurance_members":
        rows = await _scrape_coverage_members(page)
        available = [
            str(row.get("name") or "").strip()
            for row in rows
            if str(row.get("name") or "").strip()
        ]
        before_checked = {
            str(row.get("name") or "").strip().lower(): bool(row.get("checked"))
            for row in rows
        }

        async def _finish_coverage_members() -> dict[str, Any] | None:
            confirmed = await _scrape_coverage_members(page)
            selected = [
                str(row.get("name") or "").strip()
                for row in confirmed
                if row.get("checked") and str(row.get("name") or "").strip()
            ]
            if not selected:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: "
                        + _who_has_coverage_question(available)
                    ),
                    "error": "insurance_members_incomplete",
                    "step": step,
                    "phase": "application",
                }
            enabled = await _next_button_enabled(page)
            if enabled is False:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: Next is still disabled. "
                        + _who_has_coverage_question(available)
                    ),
                    "error": "insurance_members_incomplete",
                    "step": step,
                    "phase": "application",
                }
            clicked = await _click_next_or_continue(page)
            if not clicked:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: I couldn't move to the next question yet. "
                        + _who_has_coverage_question(available)
                    ),
                    "error": "insurance_members_incomplete",
                    "step": step,
                    "phase": "application",
                }
            _set_insurance_queue(session, selected)
            return None

        if re.search(r"\b(continue|next|done|that's all|finished)\b", lowered) or _is_yes(
            text
        ):
            early = await _finish_coverage_members()
            if early is not None:
                # Yes with nobody selected stays on this question.
                if _is_yes(text) and not any(before_checked.values()):
                    early["spoken"] = "ASK: " + _who_has_coverage_question(available)
                    early["step"] = step
                return early
            next_step = await _advance_after_answer(page, session)
        else:
            if not available:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + _who_has_coverage_question([]),
                    "error": "insurance_members_clarify",
                    "step": step,
                    "phase": "application",
                }
            hits, clarify = _match_named_members(text, available)
            if clarify or not hits:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: "
                    + (
                        clarify
                        or _who_has_coverage_question(available)
                    ),
                    "error": "insurance_members_ambiguous"
                    if clarify
                    else "insurance_members_clarify",
                    "step": step,
                    "phase": "application",
                }
            for name in hits:
                same = [item for item in available if item.lower() == name.lower()]
                if len(same) > 1:
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": (
                            "ASK: I see more than one person named "
                            f"{name}. Please say the full name as shown."
                        ),
                        "error": "insurance_duplicate_name",
                        "step": step,
                        "phase": "application",
                    }
            for name in hits:
                if before_checked.get(name.lower()):
                    continue
                ok = await _set_member_insurance_checked(page, name, True)
                verified = await _verify_coverage_member_checked(page, name)
                if not ok or not verified:
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": (
                            "ASK: I couldn't confirm coverage for "
                            f"{_clean_member_display_name(name)}. "
                            + _who_has_coverage_question(available)
                        ),
                        "error": "insurance_members_not_verified",
                        "step": step,
                        "phase": "application",
                    }
            after = await _scrape_coverage_members(page)
            after_checked = {
                str(row.get("name") or "").strip().lower(): bool(row.get("checked"))
                for row in after
            }
            chosen = {name.lower() for name in hits}
            for name, was_on in before_checked.items():
                if name in chosen:
                    continue
                if after_checked.get(name, was_on) != was_on:
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": (
                            "ASK: That changed another person's coverage checkbox. "
                            + _who_has_coverage_question(available)
                        ),
                        "error": "insurance_other_member_changed",
                        "step": step,
                        "phase": "application",
                    }
            early = await _finish_coverage_members()
            if early is not None:
                return early
            next_step = await _advance_after_answer(page, session)

    elif step == "app_insurance_coverage_type":
        current = (session.filled.get("app_insurance_current") or "").strip()
        queue = _insurance_queue(session)
        if re.search(r"\b(continue|next|done|that's all|finished)\b", lowered):
            if current:
                # Require at least one coverage type before leaving this member.
                rows = await _scrape_insurance_members(page)
                row = next(
                    (
                        r
                        for r in rows
                        if (r.get("name") or "").strip().lower() == current.lower()
                    ),
                    None,
                )
                selected_types = list((row or {}).get("selected_types") or [])
                if not selected_types:
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": (
                            "ASK: I still need a coverage type for "
                            f"{_clean_member_display_name(current)}. "
                            + _insurance_coverage_question_for(current)
                        ),
                        "error": "insurance_coverage_incomplete",
                        "step": step,
                        "phase": "application",
                    }
            remaining = [n for n in queue if n.lower() != current.lower()] if current else queue
            _set_insurance_queue(session, remaining or None)
            if remaining:
                nxt = remaining[0]
                session.filled["app_insurance_current"] = nxt
                next_step = "app_insurance_coverage_type"
                spoken_override = _insurance_coverage_question_for(nxt)
                _set_pending_question(session, next_step, spoken_override)
            else:
                session.filled.pop("app_insurance_current", None)
                enabled = await _next_button_enabled(page)
                if enabled is False:
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": (
                            "ASK: Pointcare still needs coverage details before "
                            "I can continue. "
                            + app_question_for_step("app_insurance_members")
                        ),
                        "error": "insurance_coverage_next_disabled",
                        "step": step,
                        "phase": "application",
                    }
                await _click_next_or_continue(page)
                next_step = await _advance_after_answer(page, session)
        else:
            types = _map_coverage_types(lowered)
            if not types:
                target = current or "that person"
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + _insurance_coverage_question_for(target),
                    "error": "insurance_coverage_clarify",
                    "step": step,
                    "phase": "application",
                }
            if not current:
                if queue:
                    current = queue[0]
                    session.filled["app_insurance_current"] = current
                else:
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": (
                            "ASK: Whose coverage type is that? "
                            + app_question_for_step("app_insurance_members")
                        ),
                        "error": "insurance_coverage_member_needed",
                        "step": "app_insurance_members",
                        "phase": "application",
                    }
            set_ok = True
            for cov in types:
                ok = await _set_member_coverage_type(page, current, cov)
                if not ok:
                    set_ok = False
                    break
            if not set_ok:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: I couldn't select that coverage on the form. "
                        + _insurance_coverage_question_for(current)
                    ),
                    "error": "insurance_coverage_not_set",
                    "step": step,
                    "phase": "application",
                }
            await page.wait_for_timeout(250)
            verified = await _verify_member_coverage_types(page, current, types)
            # Soft-verify: scrape may miss selected_types; keep going if set_ok.
            if not verified:
                # Re-check by scraping; if still empty, ask once more.
                rows = await _scrape_insurance_members(page)
                row = next(
                    (
                        r
                        for r in rows
                        if (r.get("name") or "").strip().lower() == current.lower()
                    ),
                    None,
                )
                if row is not None and not list(row.get("selected_types") or []):
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": (
                            "ASK: I couldn't verify that coverage was selected. "
                            + _insurance_coverage_question_for(current)
                        ),
                        "error": "insurance_coverage_not_verified",
                        "step": step,
                        "phase": "application",
                    }
            # Stay on this member so the caller can add more coverage types.
            label_list = ", ".join(types)
            others = [n for n in queue if n.lower() != current.lower()]
            if others:
                spoken_override = (
                    f"Okay, {label_list} for {current}. "
                    "Say another coverage type for them if needed, or say "
                    "continue to move to the next person."
                )
            else:
                spoken_override = (
                    f"Okay, {label_list} for {current}. "
                    "Say another coverage type if needed, or say continue."
                )
            session.filled["app_insurance_current"] = current
            if current.lower() not in {n.lower() for n in queue}:
                _set_insurance_queue(session, [current] + others)
            next_step = "app_insurance_coverage_type"
            _set_pending_question(session, next_step, spoken_override)

    elif step == "app_dob_review":
        cards = await _scrape_dob_cards(page)
        missing = [
            str(c.get("name") or "").strip()
            for c in cards
            if str(c.get("name") or "").strip()
            and (c.get("needs_add") or not str(c.get("dob") or "").strip())
        ]
        if missing:
            # Required DOBs still missing — never treat Yes as permission to advance.
            dob_step, dob_speech = await _prepare_dob_collect_speech(session, page)
            return _app_ok(
                call_id,
                session,
                text,
                dob_step,
                "dob missing required",
                spoken_override=dob_speech,
            )
        if _is_yes(text) or re.search(
            r"\b(continue|looks? (good|correct|fine)|next)\b", lowered
        ):
            enabled = await _next_button_enabled(page)
            if enabled is False:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: Next is still disabled, so a required date of birth "
                        "may still be missing. "
                        + app_question_for_step("app_dob_review", session)
                    ),
                    "error": "dob_next_disabled",
                    "step": step,
                    "phase": "application",
                }
            clicked = await _click_next_or_continue(page)
            if not clicked and enabled is False:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: I can't continue yet because Next is disabled. "
                        + app_question_for_step("app_dob_review", session)
                    ),
                    "error": "dob_next_disabled",
                    "step": step,
                    "phase": "application",
                }
            next_step = await _advance_after_answer(page, session)
        elif _is_no(text) or re.search(
            r"\b(change|wrong|correct|fix|edit)\b", lowered
        ):
            available = [
                str(c.get("name") or "").strip()
                for c in cards
                if str(c.get("name") or "").strip()
            ]
            hits, clarify = _match_named_members(text, available)
            if clarify:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + clarify,
                    "error": "dob_member_ambiguous",
                    "step": step,
                    "phase": "application",
                }
            if hits:
                next_step, spoken_override = _begin_dob_collect_for(session, hits)
            else:
                spoken_override = (
                    "Okay — whose date of birth should I change? Say the name."
                )
                return _app_ok(
                    call_id,
                    session,
                    text,
                    step,
                    "dob change name needed",
                    spoken_override=spoken_override,
                )
        else:
            # Bare name like "Agent H" / "Agent Edge" while confirming DOBs.
            available = [
                str(c.get("name") or "").strip()
                for c in cards
                if str(c.get("name") or "").strip()
            ]
            hits, clarify = _match_named_members(text, available)
            if clarify:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + clarify,
                    "error": "dob_member_ambiguous",
                    "step": step,
                    "phase": "application",
                }
            name_like = bool(hits) or bool(_extract_spoken_member_labels(text))
            looks_like_name = bool(
                re.fullmatch(
                    r"[A-Za-z][A-Za-z'.\-]*(?:\s+[A-Za-z][A-Za-z'.\-]*){0,3}\.?",
                    (text or "").strip(),
                )
            )
            if name_like or looks_like_name:
                targets = hits or _extract_spoken_member_labels(text)
                if targets:
                    next_step, spoken_override = _begin_dob_collect_for(
                        session, targets
                    )
                else:
                    dob_step, dob_speech = await _prepare_dob_collect_speech(
                        session, page
                    )
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": "ASK: " + dob_speech,
                        "error": "dob_review_clarify",
                        "step": "app_dob_review",
                        "phase": "application",
                    }
            else:
                # Fresh entry / unclear — summarize or start collecting.
                dob_step, dob_speech = await _prepare_dob_collect_speech(
                    session, page
                )
                if dob_step == "app_dob_collect":
                    return _app_ok(
                        call_id,
                        session,
                        text,
                        dob_step,
                        "dob collect required",
                        spoken_override=dob_speech,
                    )
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + dob_speech,
                    "error": "dob_review_clarify",
                    "step": "app_dob_review",
                    "phase": "application",
                }

    elif step == "app_dob_collect":
        current = (session.filled.get("app_dob_current") or "").strip()
        queue = _dob_queue(session)
        # Allow "change Mickey" / name-only to retarget without inventing a date.
        parsed_early, _clarify_early = _parse_dob_answer(text)
        if re.search(r"\b(change|correct|fix|edit)\b", lowered) and not parsed_early:
            cards = await _scrape_dob_cards(page)
            available = [
                str(c.get("name") or "").strip()
                for c in cards
                if str(c.get("name") or "").strip()
            ]
            hits, clarify = _match_named_members(text, available)
            if clarify:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + clarify,
                    "error": "dob_member_ambiguous",
                    "step": step,
                    "phase": "application",
                }
            targets = hits or _extract_spoken_member_labels(text)
            if targets:
                next_step, spoken_override = _begin_dob_collect_for(session, targets)
                return _app_ok(
                    call_id,
                    session,
                    text,
                    next_step,
                    "dob retarget",
                    spoken_override=spoken_override,
                )
        dob, dob_clarify = _merge_dob_pieces(
            session, current or "", _dob_pieces(text)
        )
        if dob_clarify:
            clarify_low = dob_clarify.lower()
            if "future" in clarify_low:
                clarify_error = "dob_invalid"
            elif clarify_low.startswith("please say") or clarify_low.startswith("i have"):
                clarify_error = "dob_incomplete"
            else:
                clarify_error = "dob_ambiguous"
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + dob_clarify,
                "error": clarify_error,
                "step": step,
                "phase": "application",
            }
        if not dob:
            # Caller may have named another member instead of giving a date.
            alt_names = _extract_spoken_member_labels(text)
            looks_like_name = bool(
                re.fullmatch(
                    r"[A-Za-z][A-Za-z'.\-]*(?:\s+[A-Za-z][A-Za-z'.\-]*){0,3}\.?",
                    (text or "").strip(),
                )
            )
            if alt_names and looks_like_name:
                next_step, spoken_override = _begin_dob_collect_for(
                    session, alt_names
                )
            else:
                target = current or "that person"
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: I need a clear month, day, and year. "
                        + _dob_question_for(target)
                    ),
                    "error": "dob_invalid",
                    "step": step,
                    "phase": "application",
                }
        else:
            if not current:
                cards = await _scrape_dob_cards(page)
                available = [
                    str(c.get("name") or "").strip()
                    for c in cards
                    if c.get("needs_add") and str(c.get("name") or "").strip()
                ]
                if len(available) == 1:
                    current = available[0]
                    session.filled["app_dob_current"] = current
                else:
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": (
                            "ASK: Whose date of birth is that? Please say the name."
                        ),
                        "error": "dob_member_needed",
                        "step": step,
                        "phase": "application",
                    }
            cards_before = await _scrape_dob_cards(page)
            same_name = [
                c
                for c in cards_before
                if (c.get("name") or "").strip().lower() == current.lower()
                or current.lower().split()[:1]
                == (c.get("name") or "").strip().lower().split()[:1]
            ]
            # Prefer exact label; fall back to first-name matches carefully.
            exact = [
                c
                for c in cards_before
                if (c.get("name") or "").strip().lower() == current.lower()
            ]
            if exact:
                same_name = exact
            if len(same_name) > 1:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: I see more than one person named "
                        f"{current}. Please say the full name as shown on the card."
                    ),
                    "error": "dob_duplicate_name",
                    "step": step,
                    "phase": "application",
                }
            if same_name:
                current = str(same_name[0].get("name") or current)
                session.filled["app_dob_current"] = current
            card = same_name[0] if len(same_name) == 1 else None
            correcting = bool(session.filled.get("app_dob_correcting"))
            saved_already = bool(
                card
                and not card.get("needs_add")
                and _dob_saved_match(dob, str(card.get("dob") or ""))
            )
            if saved_already and not correcting:
                saved = True
            elif (
                card
                and not card.get("needs_add")
                and str(card.get("dob") or "").strip()
                and not correcting
            ):
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: That card already has a saved date of birth. "
                        f"Say change {current} if it should be corrected."
                    ),
                    "error": "dob_existing",
                    "step": step,
                    "phase": "application",
                }
            else:
                edit = bool(card and not card.get("needs_add") and correcting)
                if card is None or card.get("needs_add") or edit:
                    await _click_member_add_dob(page, current, edit=edit)
                saved = await _fill_and_save_dob(page, dob, current)
            if not saved:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: I couldn't enter that date on the form. "
                        + _dob_question_for(current)
                    ),
                    "error": "dob_fill_failed",
                    "step": step,
                    "phase": "application",
                }
            await page.wait_for_timeout(600)
            verified = await _verify_member_dob(page, current, dob)
            if not verified:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: I couldn't verify that date of birth was saved. "
                        + _dob_question_for(current)
                    ),
                    "error": "dob_not_verified",
                    "step": step,
                    "phase": "application",
                }
            remaining = [name for name in queue if name.lower() != current.lower()]
            cards_after = await _scrape_dob_cards(page)
            before_dobs = {
                str(c.get("name") or "").strip().lower(): str(c.get("dob") or "")
                for c in cards_before
            }
            for other in cards_after:
                other_name = str(other.get("name") or "").strip()
                if not other_name or other_name.lower() == current.lower():
                    continue
                previous = before_dobs.get(other_name.lower())
                if previous is None:
                    continue
                if str(other.get("dob") or "") != previous:
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": (
                            "ASK: That update changed another person's card. "
                            + _dob_question_for(current)
                        ),
                        "error": "dob_wrong_member",
                        "step": step,
                        "phase": "application",
                    }
            still_missing = [
                str(c.get("name") or "").strip()
                for c in cards_after
                if c.get("needs_add") and str(c.get("name") or "").strip()
            ]
            for name in still_missing:
                if name.lower() != current.lower() and name not in remaining:
                    remaining.append(name)
            session.filled.pop("app_dob_correcting", None)
            _clear_dob_partial(session)
            if remaining:
                _set_dob_queue(session, remaining)
                nxt = remaining[0]
                session.filled["app_dob_current"] = nxt
                next_step = "app_dob_collect"
                spoken_override = (
                    f"Okay, that date of birth is saved for {current}. "
                    + _dob_question_for(nxt)
                )
                _set_pending_question(session, next_step, spoken_override)
            else:
                _set_dob_queue(session, None)
                session.filled.pop("app_dob_current", None)
                next_step, spoken_override = await _prepare_dob_collect_speech(
                    session, page
                )

    elif step == "app_address_review":
        return await _handle_address_step(
            call_id, session, page, text, lowered, step
        )

    elif step in (
        "app_marital_review",
        "app_childcare_review",
    ):
        if _is_yes(text) or re.search(
            r"\b(continue|looks? (good|correct|fine)|next)\b", lowered
        ):
            await _click_next_or_continue(page)
            next_step = await _advance_after_answer(page, session)
        elif _is_no(text) or re.search(r"\b(change|wrong|correct|fix)\b", lowered):
            blurb = await _scrape_review_summary(page, step)
            return _app_ok(
                call_id,
                session,
                text,
                step,
                "change requested",
                spoken_override=(
                    blurb
                    + "Okay — tell me whose information to change and the new value, "
                    "or say Yes when it looks correct so I can continue."
                ),
            )
        else:
            blurb = await _scrape_review_summary(page, step)
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + blurb + app_question_for_step(step, session),
                "error": f"{step}_clarify",
                "step": step,
                "phase": "application",
            }

    elif step == "app_circumstances":
        labels = _map_circumstance_labels(lowered)
        if not labels:
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + app_question_for_step(step),
                "error": "circumstances_clarify",
                "step": step,
                "phase": "application",
            }
        for label in labels:
            try:
                await _click_exact_choice(page, label)
            except Exception:
                try:
                    await _click_cta(page, label, attempts=1)
                except Exception:
                    pass
        if labels == ["None of the above"]:
            await _click_next_or_continue(page)
            next_step = await _advance_after_answer(page, session)
        else:
            # Queue every selected circumstance that needs a person assignment.
            session.filled["app_circumstance_queue"] = json.dumps(labels)
            session.filled["app_circumstance_pending"] = labels[0]
            next_step = "app_circumstances_person"
            spoken_override = (
                f"Okay, {labels[0]}. "
                + f"Which household member does {labels[0]} apply to?"
            )
            _set_pending_question(session, next_step, spoken_override)

    elif step == "app_circumstances_person":
        pending = (session.filled.get("app_circumstance_pending") or "").strip()
        queue_raw = session.filled.get("app_circumstance_queue") or "[]"
        try:
            queue = [
                str(x).strip()
                for x in json.loads(queue_raw)
                if str(x).strip()
            ]
        except Exception:
            queue = [pending] if pending else []
        if re.search(r"\b(continue|next|done|skip)\b", lowered):
            await _click_next_or_continue(page)
            next_step = await _advance_after_answer(page, session)
            session.filled.pop("app_circumstance_pending", None)
            session.filled.pop("app_circumstance_queue", None)
        else:
            # Reject circumstance/condition phrases — need a household member.
            if _map_circumstance_labels(lowered) and not re.search(
                r"\b(applies?|for)\b", lowered
            ):
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: Please say the household member's name for "
                        f"{pending or 'that circumstance'}, not the condition again."
                    ),
                    "error": "circumstances_person_not_member",
                    "step": step,
                    "phase": "application",
                }
            member = ""
            for token in re.findall(
                r"[A-Za-z][A-Za-z'.\-]*(?:\s+[A-Za-z]\.?)?", text
            ):
                if token.lower() in {
                    "and",
                    "the",
                    "applies",
                    "to",
                    "for",
                    "this",
                    "that",
                }:
                    continue
                member = token.strip()
                if member:
                    break
            if not member:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: Which household member does "
                        f"{pending or 'that circumstance'} apply to?"
                    ),
                    "error": "circumstances_person_clarify",
                    "step": step,
                    "phase": "application",
                }
            # Scope the member click to the active circumstance card only —
            # never select Blind when Cervical cancer is the pending section.
            ok = await _set_circumstance_member(page, member, pending)
            if not ok:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: I couldn't select that person under "
                        f"{pending or 'the current circumstance'}. "
                        "Please say the name as shown on the card."
                    ),
                    "error": "circumstances_person_not_set",
                    "step": step,
                    "phase": "application",
                }
            remaining = [c for c in queue if c.lower() != pending.lower()]
            if remaining:
                session.filled["app_circumstance_queue"] = json.dumps(remaining)
                session.filled["app_circumstance_pending"] = remaining[0]
                next_step = "app_circumstances_person"
                spoken_override = (
                    f"Okay. Which household member does {remaining[0]} apply to?"
                )
                _set_pending_question(session, next_step, spoken_override)
            else:
                session.filled.pop("app_circumstance_pending", None)
                session.filled.pop("app_circumstance_queue", None)
                await _click_next_or_continue(page)
                next_step = await _advance_after_answer(page, session)

    elif step == "app_tax":
        screen = await detect_application_screen(page)
        if screen and screen != "app_tax" and screen.startswith("app_"):
            # Tax screen absent — reconcile and continue.
            next_step = screen
            spoken_override = app_question_for_step(next_step)
        elif re.search(r"\b(edit|change)\b", lowered) or (
            _is_no(text) and "correct" not in lowered
        ):
            try:
                await _click_cta(page, "Edit", attempts=2)
            except Exception:
                pass
            spoken_override = (
                "Okay, the tax edit screen should be open. Say the filing status "
                "or dependent change you need, then say continue when you're ready "
                "for me to save and return."
            )
            next_step = "app_tax"
        elif _is_yes(text) or re.search(
            r"\b(continue|looks? (good|correct|fine)|next|save)\b", lowered
        ):
            await _click_next_or_continue(page)
            next_step = await _advance_after_answer(page, session)
        else:
            blurb = await _scrape_review_summary(page, step)
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + blurb + app_question_for_step(step),
                "error": "tax_clarify",
                "step": step,
                "phase": "application",
            }

    elif step == "app_income":
        if re.search(r"\b(continue|next|done|everyone)\b", lowered):
            await _click_next_or_continue(page)
            next_step = await _advance_after_answer(page, session)
        else:
            picked = _map_income_type(lowered)
            if not picked:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + app_question_for_step(step),
                    "error": "income_clarify",
                    "step": step,
                    "phase": "application",
                }
            try:
                await _click_cta(page, "Add Income", attempts=1)
            except Exception:
                pass
            try:
                await _click_exact_choice(page, picked)
            except Exception:
                await _click_cta(page, picked, attempts=2)
            if picked == "Employed":
                next_step, spoken_override = await _start_income_seq(
                    session, "employed"
                )
            elif picked == "Self-employed":
                next_step, spoken_override = await _start_income_seq(session, "self")
            elif picked == "None":
                try:
                    await _click_cta(page, "Save", attempts=2)
                except Exception:
                    pass
                next_step = "app_income_more"
                spoken_override = app_question_for_step(next_step)
            else:
                _set_income_seq(
                    session,
                    {
                        "kind": "other",
                        "index": 0,
                        "values": {},
                        "keys": ["pay"],
                    },
                )
                next_step = "app_income_self"
                spoken_override = (
                    f"Okay, {picked}. What amount should I enter for that income?"
                )

    elif step in (
        "app_income_employed",
        "app_income_employed_hours",
        "app_income_employed_frequency",
        "app_income_employed_employer",
        "app_income_self",
        "app_income_self_work",
    ):
        return await _continue_income_seq(call_id, session, page, text)

    elif step == "app_income_more":
        if _is_yes(text):
            next_step = "app_income"
            spoken_override = app_question_for_step(next_step)
        else:
            await _click_next_or_continue(page)
            next_step = await _advance_after_answer(page, session)

    elif step == "app_expenses":
        if re.search(r"\b(skip|next|no|continue|none)\b", lowered) or _is_no(text):
            await _click_next_or_continue(page)
            next_step = await _advance_after_answer(page, session)
        else:
            expense_cues = (
                ("student loan", "Student loans"),
                ("childcare", "Childcare"),
                ("child care", "Childcare"),
                ("medical", "Medical"),
            )
            picked = None
            for cue, label in expense_cues:
                if cue in lowered:
                    picked = label
                    break
            if picked:
                try:
                    await _click_exact_choice(page, picked)
                except Exception:
                    try:
                        await _click_cta(page, picked, attempts=1)
                    except Exception:
                        pass
                session.filled["app_expense_pending"] = picked
                next_step = "app_expenses_amount"
                spoken_override = app_question_for_step(next_step)
            else:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + app_question_for_step(step),
                    "error": "expenses_clarify",
                    "step": step,
                    "phase": "application",
                }

    elif step == "app_expenses_amount":
        filled = await _fill_next_visible_input(page, text)
        if not filled:
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + app_question_for_step(step),
                "error": "expenses_amount_clarify",
                "step": step,
                "phase": "application",
            }
        try:
            await _click_cta(page, "Save", attempts=1)
        except Exception:
            pass
        session.filled.pop("app_expense_pending", None)
        await _click_next_or_continue(page)
        next_step = await _advance_after_answer(page, session)

    elif step == "app_documents":
        if re.search(r"\b(skip|next|later|continue|no)\b", lowered) or _is_no(text):
            await _click_next_or_continue(page)
            next_step = await _advance_after_answer(page, session)
        else:
            return _app_ok(
                call_id,
                session,
                text,
                step,
                "upload deferred",
                spoken_override=(
                    "I can't take document files over the phone. "
                    "Say skip to continue without uploading now. You can add "
                    "documents later by logging in with the same account — "
                    "I will not pretend a file was uploaded."
                ),
            )

    elif step == "app_submit_confirm":
        if _is_yes(text) or re.search(r"\b(submit|confirm)\b", lowered):
            clicked_submit = False
            for label in ("Submit Applications", "Submit Application", "Submit"):
                try:
                    await _click_cta(page, label, attempts=2)
                    clicked_submit = True
                    break
                except Exception:
                    continue
            if not clicked_submit:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + app_question_for_step(step),
                    "error": "submit_button_missing",
                    "step": step,
                    "phase": "application",
                }
            await page.wait_for_timeout(1200)
            body = ""
            try:
                body = (await page.locator("body").inner_text()).lower()
            except Exception:
                pass
            confirmed = any(
                needle in body
                for needle in (
                    "submitted",
                    "thank you",
                    "all set",
                    "application has been",
                    "we received",
                )
            )
            if not confirmed:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: I couldn't confirm that Pointcare accepted the submission. "
                        + app_question_for_step(step)
                    ),
                    "error": "submit_not_verified",
                    "step": step,
                    "phase": "application",
                }
            session.filled["application_submitted"] = "1"
            session.app_step = ""
            return {
                "ok": True,
                "call_id": call_id,
                "field_id": "application_answer",
                "value": text,
                "action": "submitted",
                "message": "Application submitted and verified",
                "spoken": "ASK: " + CLOSING_SUBMITTED,
                "step": "",
                "phase": "closing",
                "end_call": True,
            }
        if _is_no(text):
            session.app_step = ""
            return {
                "ok": True,
                "call_id": call_id,
                "field_id": "application_answer",
                "value": text,
                "action": "not_submitted",
                "message": "Caller declined final submit",
                "spoken": "ASK: " + CLOSING_NOT_SUBMITTED,
                "step": "",
                "phase": "closing",
                "end_call": True,
            }
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + app_question_for_step(step),
            "error": "submit_clarify",
            "step": step,
            "phase": "application",
        }
    else:
        # Unknown step — try Next and redetect.
        await _click_next_or_continue(page)
        next_step = await _advance_after_answer(page, session)

    if next_step == "programs":
        # Mid Stage E should not silently restart citizenship.
        next_step = step if step.startswith("app_") else ""
        if not next_step:
            return _unidentified_screen_error(call_id, session, answer)
    # Entering DOB review: inspect missing dates before asking a bare Yes.
    if (
        next_step == "app_dob_review"
        and spoken_override is None
        and step != "app_dob_review"
    ):
        next_step, spoken_override = await _prepare_dob_collect_speech(
            session, page
        )
    if not next_step:
        return _unidentified_screen_error(call_id, session, answer)
    _mark(call_id, "verified", started)
    session.history.append(
        {
            "field_id": "application_answer",
            "step": step,
            "value": answer,
            "action": "verified",
            "ok": True,
        }
    )
    result = _app_ok(
        call_id,
        session,
        answer,
        next_step,
        f"Applied application step {step}",
        spoken_override=spoken_override,
        end_call=end_call,
    )
    _mark(call_id, "next_question", started)
    _mark(call_id, "tool_response", started)
    return result
