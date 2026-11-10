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
        "Would you like to connect with a licensed insurance agent to explore "
        "Medicare or ACA Marketplace options and get help finishing enrollment? "
        "Say Yes to connect, or No to continue without an agent."
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
        "What is that person's citizenship or immigration status?"
    ),
    "app_insurance": (
        "Do the household members listed on this application have health "
        "insurance or health coverage? Please say Yes or No."
    ),
    "app_insurance_members": (
        "Who has coverage? Say the names of the household members who have coverage."
    ),
    "app_insurance_coverage_type": (
        "Which coverage or plan do they qualify for — Medicare, Medicaid, health "
        "insurance through a job, TRICARE, VA Health Care, private insurance or ACA "
        "Marketplace, or Other? You can name more than one if needed."
    ),
    "app_dob_review": (
        "I'll confirm each person's date of birth. "
        "Say Yes if it is correct, or tell me whose date of birth to change."
    ),
    "app_dob_collect": (
        "What month was that person born?"
    ),
    "app_address_review": (
        "I'll confirm each person's home and mailing address. "
        "Say Yes if that looks correct, or tell me whose address to change."
    ),
    "app_marital_review": (
        "I'll confirm each person's current marital status. "
        "Say Yes to continue, or No to change it."
    ),
    "app_childcare_review": (
        "I'll confirm childcare answers one person at a time. "
        "Say Yes to continue, or No to change it."
    ),
    "app_circumstances": (
        "Do any of these apply to anyone on the application: needs help with daily "
        "activities, veteran or military member, American Indian or Alaska Native, "
        "full-time student ages 18 to 20, or none of the above? "
        "You can name more than one if needed."
    ),
    "app_circumstances_person": (
        "Which household member does that apply to? Say the name. "
        "You can name more than one person."
    ),
    "app_circumstances_more": (
        "Are there any other situations to add? Please say Yes or No."
    ),
    "app_tax": (
        "I'll review the tax information for each household member, "
        "starting with their current tax filing status."
    ),
    "app_income": (
        "For work and income, we go one person at a time. "
        "What income type should I enter?"
    ),
    "app_income_employed": (
        "What is the monthly gross income before taxes?"
    ),
    "app_income_employed_hours": "How many hours per week?",
    "app_income_employed_frequency": (
        "What is the pay frequency — Weekly, Bi-weekly, Monthly, or Annually?"
    ),
    "app_income_employed_employer": "What is the employer name?",
    "app_income_self": (
        "What is the estimated monthly net income after expenses?"
    ),
    "app_income_self_work": "What type of work is it?",
    "app_income_amount": "What is the amount per month?",
    "app_income_unemployed": (
        "Do you receive unemployment compensation? Please say Yes or No."
    ),
    "app_income_confirm": (
        "Does that income entry look correct? Say Yes to save, or say edit if "
        "you want to change something."
    ),
    "app_income_more": (
        "Do you want to add another income? Please say Yes or No."
    ),
    "app_expenses": (
        "I'll add deductible and medical expenses for each person. "
        "What expense type should I enter?"
    ),
    "app_expenses_amount": (
        "What is the amount per month?"
    ),
    "app_expenses_medical": (
        "Do you have medical expenses in the last 3 months that you need help "
        "paying for? Please say Yes or No."
    ),
    "app_expenses_confirm": (
        "Does that look correct? Say Yes to save, or say edit if you want to "
        "change something."
    ),
    "app_expenses_more": (
        "Do you want to add another expense? Please say Yes or No."
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
        "app_income_unemployed",
        "app_income_confirm",
        "app_income_more",
        "app_expenses_medical",
        "app_expenses_confirm",
        "app_expenses_more",
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
    "app_address_review": (
        "Pointcare shows a home address and a mailing address for each household "
        "member. Confirm the saved cards first; open Edit only when a change is "
        "requested."
    ),
    "app_marital_review": (
        "Pointcare shows a current marital status for each household member. "
        "Confirm one card at a time with Yes or No. On No, open the pencil, "
        "select the new status, Save only when it differs, then continue."
    ),
    "app_childcare_review": (
        "Pointcare asks whether each adult lives with and cares for children "
        "under 19. Confirm one row at a time with Yes or No. On change, click "
        "that member's Yes or No radio, then continue."
    ),
    "app_tax": (
        "Pointcare shows tax filing status and dependent answers for each "
        "household member. Confirm one person at a time: filing status, whether "
        "they claim dependents, and whether anyone claims them. Open Edit only "
        "to change answers, then Save and Return before the next person."
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
        "After a member is checked, Pointcare expands coverage-type options under "
        "that name on the same screen. Confirm each plan before moving on."
    ),
    "app_income": (
        "We collect each person's income type and details one short question at "
        "a time, confirm before saving, and mention that proof documents will be "
        "uploaded later. Types other than Employed, Self-employed, and Unemployed "
        "only need the amount per month. After save, first ask whether to add "
        "another income (Yes/No). Only after No, ask whether to edit or delete; "
        "ask for the income type only after they say edit or delete."
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


def _eligible_program_names(summary: dict[str, Any]) -> list[str]:
    """Short spoken names for programs marked eligible — never not-eligible cards."""
    names: list[str] = []
    seen: set[str] = set()
    for program in summary.get("programs") or []:
        if not isinstance(program, dict) or not program.get("may_be_eligible"):
            continue
        name = _short_program_name(str(program.get("name") or ""))
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def build_programs_permission_speech(summary: dict[str, Any]) -> str:
    """Acknowledge Great News eligible programs, then ask to continue.

    Examples:
      - only Medicaid → "You're only eligible for Medicaid..."
      - several eligible → read them out, then ask to continue
      - none eligible / Medicaid fallback already applied → say Medicaid was
        selected and ask to continue
    Never lists Medicare/Marketplace cards marked \"Not eligible at this time\".
    """
    names = _eligible_program_names(summary)
    fallback = bool(summary.get("medicaid_fallback_applied"))
    ask = (
        "Would you like to continue to the application? Please say Yes or No."
    )
    if fallback and (not names or names == ["Medicaid"]):
        return (
            "Your household eligibility check is complete. "
            "Pointcare didn't show an eligible program, so I selected Medicaid "
            "for you. " + ask
        )
    if len(names) == 1:
        only = names[0]
        # MassHealth / Medicaid-only Great News (screenshot scenario).
        if only.lower() in {"medicaid", "medi-cal"}:
            return (
                f"Your household eligibility check is complete. "
                f"You're only eligible for {only}. " + ask
            )
        return (
            f"Your household eligibility check is complete. "
            f"You're only eligible for {only}. " + ask
        )
    if len(names) > 1:
        joined = ", ".join(names[:-1]) + f", and {names[-1]}"
        return (
            f"Your household eligibility check is complete. "
            f"You're eligible for {joined}. " + ask
        )
    return (
        "Your household eligibility check is complete. "
        "Pointcare does not currently show a program marked as one your "
        "household may be eligible for. " + ask
    )


def _short_program_name(name: str) -> str:
    text = re.sub(r"\s+", " ", (name or "").strip())
    if not text:
        return ""
    # Prefer common public names when the card title is long.
    low = text.lower()
    if "medikids" in low or "healthy kids" in low:
        return text[:80].rstrip(" .,:;")
    # MassHealth / CommonHealth cards speak as Medicaid for the caller.
    if "masshealth" in low or "commonhealth" in low:
        return "Medicaid"
    if "medicaid" in low or "medi-cal" in low or "medi cal" in low:
        if "medi-cal" in low or "medi cal" in low:
            return "Medi-Cal"
        if "health first colorado" in low:
            return "Medicaid"
        return "Medicaid"
    if "medicare" in low:
        return "Medicare"
    if (
        "marketplace" in low
        or "health connector" in low
        or "covered california" in low
        or re.search(r"\baca\b", low)
    ):
        if "covered california" in low:
            return "Covered California"
        if "health connector" in low:
            return "Health Connector"
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
                'label, li, [role="listitem"], article, div, section'
              )].sort((a, b) => {
                const at = norm(a.innerText || a.textContent || '').length;
                const bt = norm(b.innerText || b.textContent || '').length;
                return at - bt;
              });
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (r.width < 40 || r.height < 20) continue;
                const rawText = (el.innerText || el.textContent || '').trim();
                let text = norm(rawText);
                if (!text || text.length < 4 || text.length > 320) continue;
                const low = text.toLowerCase();
                const looksProgram = (
                  /masshealth|commonhealth|medicaid|medi-?cal|medicare|marketplace|covered california|\\baca\\b|medikids|healthy kids|florida/i.test(low)
                );
                if (!looksProgram) continue;
                const hasResult = /may be eligible|might be eligible|eligible for|not eligible at this time|not eligible/i.test(low);
                if (!hasResult) continue;
                // Prefer compact cards over giant page wrappers.
                if (text.length > 220) continue;
                const notEligible = /not eligible at this time|not eligible/i.test(low);
                const mayEligible = !notEligible && (
                  /may be eligible|might be eligible|eligible for/i.test(low)
                );
                // Extract the card title while preserving state program branding.
                const lines = rawText.split(/\\r?\\n/).map(norm).filter(Boolean);
                let name = (lines.find((line) => (
                  /masshealth|commonhealth|medicaid|medi-?cal|medicare|marketplace|covered california|\\baca\\b|medikids|healthy kids/i.test(line)
                  && !/may be eligible|might be eligible|eligible for|not eligible/i.test(line)
                )) || '').slice(0, 80);
                const nameMatch = text.match(
                  /(MassHealth\\s+CommonHealth\\s*\\(Medicaid\\)|MassHealth\\s+CommonHealth|Health First Colorado\\s*\\(Medicaid\\)|MediKids[^,;]{0,55}|Florida Healthy Kids[^,;]{0,45}|Health Connector\\s*\\(ACA Marketplace\\)|Medi-?Cal|Medicaid|Medicare|ACA Marketplace|Covered California)/i
                );
                if (!name && nameMatch) name = nameMatch[1];
                if (!name) continue;
                const key = name.toLowerCase();
                if (seen.has(key)) continue;
                seen.add(key);
                // Only the program-level checkbox — ignore "Who is applying?" members.
                let checked = false;
                const programBoxes = [];
                let pastWho = false;
                const kids = [el, ...(el.querySelectorAll ? [...el.querySelectorAll('*')] : [])];
                for (const node of kids) {
                  const t = ((node.innerText || node.textContent || '') + '').trim();
                  if (t.length < 40 && /who is applying/i.test(t)) {
                    pastWho = true;
                    continue;
                  }
                  if (pastWho) continue;
                  if (node.matches && node.matches('input[type=checkbox], [role=checkbox]')) {
                    programBoxes.push(node);
                  }
                }
                const roleBox = programBoxes[0] || null;
                if (roleBox) {
                  if (roleBox.matches && roleBox.matches('input[type=checkbox]')) {
                    checked = !!roleBox.checked;
                  } else {
                    checked = (roleBox.getAttribute('aria-checked') || '') === 'true';
                  }
                }
                const aria = el.getAttribute && el.getAttribute('aria-checked');
                if (aria === 'true') checked = true;
                if (aria === 'false' && !roleBox) checked = false;
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


async def wait_for_program_summary(
    page: Page, timeout_s: float = 8.0
) -> dict[str, Any]:
    """Wait for Great News cards before speaking program eligibility.

    Pointcare can advance the step before the SPA finishes rendering the cards.
    Scraping immediately stored an empty list and falsely announced no programs.
    """
    deadline = time.monotonic() + max(0.5, timeout_s)
    last: dict[str, Any] = {"programs": []}
    while time.monotonic() < deadline:
        last = await scrape_program_summary(page)
        if last.get("programs"):
            return last
        await page.wait_for_timeout(250)
    return last


async def store_program_summary(session: Any, page: Page) -> dict[str, Any]:
    """Scrape Great News, apply Medicaid-only fallback when needed, store speech."""
    summary = await wait_for_program_summary(page)
    # 0-eligible / Continue disabled → select Medicaid alone, then ask to continue.
    if _programs_need_medicaid_fallback(summary):
        ready = await ensure_programs_ready_to_continue(page)
        if ready.get("selected"):
            summary = await scrape_program_summary(page)
            summary["medicaid_fallback_applied"] = True
            # Ensure speech can name Medicaid even if scrape still lags.
            eligible = _eligible_program_names(summary)
            if not eligible:
                summary.setdefault("programs", []).append(
                    {
                        "name": "Medicaid",
                        "may_be_eligible": True,
                        "checked": True,
                        "detail": "Selected Medicaid to continue",
                    }
                )
        logger.info(
            "programs_summary_medicaid_fallback selected=%s eligible=%s",
            ready.get("selected"),
            len(_eligible_program_names(summary)),
        )
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


async def _finish_agent_offer(page: Page, *, wants_connect: bool) -> bool:
    """Complete the optional agent screen after its Yes/No choice.

    Choosing No expands a second panel whose real CTA is
    ``Continue with Medicaid only``; it is not a generic Continue button.
    """
    labels = (
        ("Continue", "Next")
        if wants_connect
        else (
            "Continue with Medicaid only",
            "Continue without an agent",
            "Continue",
        )
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        screen = await detect_application_screen(page)
        if screen and screen not in ("app_agent_offer", "programs"):
            return True
        for label in labels:
            if await click_exact_visible_text(page, label):
                return True
        await page.wait_for_timeout(250)
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
        locator = page.locator("body")
        if hasattr(locator, "__await__"):
            locator = await locator
        text = locator.inner_text()
        if hasattr(text, "__await__"):
            text = await text
    except Exception:
        return ""
    if not isinstance(text, str):
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
    if _is_household_dashboard_text(body) or (has_quick and not has_start):
        return "dashboard"
    app_screen = await detect_application_screen(page)
    if app_screen and app_screen not in ("", "programs"):
        return app_screen
    if "next steps" in body and "household" in body:
        return "dashboard"
    return "unknown"


def _is_household_dashboard_text(body: str) -> bool:
    """True for the Next steps / Quick Apply household page, not an application step."""
    text = body or ""
    if (
        "start applications" in text
        or "continue to applications" in text
        or "continue to application" in text
        or "great news" in text
    ):
        return False
    if "continue quick apply" in text or "new quick apply" in text:
        return True
    return (
        "next steps" in text
        and "household" in text
        and "coverage unknown" in text
    )


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
        session.filled.pop("app_dob_phase", None)
        session.filled.pop("app_dob_reviewed", None)
        session.filled.pop("app_dob_walk_complete", None)
        session.filled.pop("app_dob_component", None)
        session.filled.pop("app_dob_partial", None)
        session.filled.pop("app_dob_correcting", None)
        session.filled.pop("app_immigration_queue", None)
        session.filled.pop("app_immigration_current", None)
        session.filled.pop("app_citizenship_no", None)
        session.filled.pop("app_insurance_queue", None)
        session.filled.pop("app_insurance_current", None)
        session.filled.pop("app_insurance_coverage_phase", None)
        session.filled.pop("app_insurance_current_types", None)
        session.filled.pop("app_insurance_completed", None)
        session.filled.pop("app_insurance_other_text", None)
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


_APPLICATION_ONLY_STALE_KEYS = (
    "app_citizenship_no",
    "app_immigration_queue",
    "app_immigration_current",
    "app_immigration_confirmed",
    "app_immigration_next_clicked",
    "app_immigration_options_listed",
    "app_immigration_phase",
    "app_dob_queue",
    "app_dob_current",
    "app_dob_partial",
    "app_dob_component",
    "app_dob_correcting",
    "app_dob_walk_complete",
    "app_dob_phase",
    "app_dob_reviewed",
    "app_insurance_queue",
    "app_insurance_current",
    "app_insurance_coverage_phase",
    "app_insurance_current_types",
    "app_insurance_completed",
    "app_insurance_other_text",
    "app_address_snapshot",
    "app_address_confirmed",
    "app_address_current",
    "app_address_field",
    "app_address_ready",
    "app_address_correcting",
    "app_address_phase",
    "app_address_pending_kind",
    "app_address_pending_street",
    "app_address_pending_chosen",
    "app_address_pending_unit",
    "app_address_share_after",
    "app_address_share_prompt",
    "app_address_share_members",
    "app_marital_current",
    "app_marital_current_index",
    "app_marital_review_ready",
    "app_marital_walk_index",
    "app_marital_heard",
    "app_childcare_current",
    "app_childcare_current_index",
    "app_childcare_review_ready",
    "app_childcare_walk_index",
    "app_childcare_heard",
    "app_circumstance_queue",
    "app_circumstance_pending",
    "app_circumstance_assigned",
    "app_circumstance_pairs",
    "app_circumstance_member_labels",
    "app_tax_walk_index",
    "app_tax_heard",
    "app_tax_current",
    "app_tax_current_index",
    "app_tax_phase",
    "app_tax_pending_status",
    "app_tax_pending_claim",
    "app_tax_pending_claimed",
    "app_tax_who_buffer",
    "app_tax_review_ready",
    "app_tax_saving_name",
    "app_tax_saving_index",
    "app_income_walk_index",
    "app_income_heard",
    "app_income_current",
    "app_income_current_index",
    "app_income_phase",
    "app_income_pending_type",
    "app_income_member_types",
    "app_income_review_ready",
    "app_income_seq",
    "app_income_pending_summary",
    "app_income_pending_values",
    "app_income_edit_mode",
    "app_income_delete_type",
    "app_income_add_opened",
    "app_income_types_listed",
    "app_income_edit_field",
    "app_expense_walk_index",
    "app_expense_heard",
    "app_expense_current",
    "app_expense_current_index",
    "app_expense_phase",
    "app_expense_pending_type",
    "app_expense_member_types",
    "app_expense_review_ready",
    "app_expense_pending_summary",
    "app_expense_pending_values",
    "app_expense_edit_mode",
    "app_expense_delete_type",
    "app_expense_manage_action",
    "app_expense_add_opened",
    "app_expense_types_listed",
    "app_expense_doc_notice_members",
    "resume_app_step",
    "app_pending_question",
    "app_pending_step",
    "application_test_opening",
    "application_gate_speech",
    "awaiting_quick_apply",
    "programs_permission_speech",
    "eligibility_opening_speech",
    "eligibility_opening_delivered",
)


def reset_application_only_session(session: Any) -> None:
    """Drop prior conversational application state for a new test call.

    Does not clear browser auth or the Pointcare draft — only in-memory Collect
    Mode fields so a previous call's step is never treated as the live screen.
    """
    session.elig_step = ""
    session.app_step = ""
    session.after_person = ""
    session.last_applied = ""
    for key in _APPLICATION_ONLY_STALE_KEYS:
        session.filled.pop(key, None)
    session.filled["application_started"] = "0"
    session.filled["application_only_test"] = "1"


async def run_application_only_browser(session: Any, page: Page, set_state) -> dict[str, Any]:
    """Authenticate, open the application, and land on question 1.

    When ``APPLICATION_START_AT`` is set, skip the Q1 reset and open that
    step instead (``app_address_review`` = Q4, ``app_income`` = Work & Income).

    ``set_state`` receives application_test_start, authenticating,
    opening_application, then either resetting_to_question_1 or
    skipping_to_*, then application_ready.
    """
    from .live_form import _application_test_authenticated, _sign_in_with_provider

    set_state("application_test_start")
    reset_application_only_session(session)
    set_state("authenticating")
    if not await _application_test_authenticated(page):
        try:
            landed = await _sign_in_with_provider(
                session, "Continue with Google", "", destination="stay"
            )
        except Exception as exc:
            logger.warning("application_only_auth_waiting error=%s", type(exc).__name__)
            return {
                "ok": False,
                "state": "authenticating",
                "spoken": (
                    "Please finish signing in in the Pointcare window. "
                    "I will not ask you to say a password or verification code. "
                    "I'll continue when you're signed in."
                ),
            }
        if landed != "AUTHENTICATED" or not await _application_test_authenticated(page):
            spoken = landed if str(landed).startswith("ASK:") else (
                "Please finish signing in in the Pointcare window. "
                "I'll continue when you're signed in."
            )
            return {"ok": False, "state": "authenticating", "spoken": spoken}
    session.signed_in = True
    set_state("opening_application")
    # The saved Chrome tab often redirects to the household dashboard a moment
    # after the URL is ready. One immediate read sees a blank shell and used to
    # report that the application was not open.
    surface = await _wait_application_only_surface(page)
    logger.info(
        "application_only_surface call_id=%s surface=%s url=%s",
        getattr(session, "call_id", "-"),
        surface,
        (getattr(page, "url", "") or "")[:140],
    )
    if surface == "dashboard":
        clicked = await _click_quick_apply_when_ready(page)
        if not clicked:
            return {
                "ok": False,
                "state": "opening_application",
                "spoken": (
                    "I couldn't find Continue Quick Apply on the household page, "
                    "so I did not ask question 1."
                ),
            }
        logger.info(
            "application_only_quick_apply call_id=%s clicked=%s",
            getattr(session, "call_id", "-"),
            clicked,
        )
        surface = await _wait_surface_after_quick_apply(page)
        logger.info(
            "application_only_after_quick_apply call_id=%s surface=%s",
            getattr(session, "call_id", "-"),
            surface,
        )
    if surface == "dashboard":
        return {
            "ok": False,
            "state": "opening_application",
            "spoken": (
                "I couldn't open the program summary from Quick Apply, "
                "so I did not start the application questions."
            ),
        }
    if surface == "programs":
        # Exactly one Start Applications click, then wait for a real app screen.
        # URL alone must never prove the application opened.
        try:
            await ensure_programs_ready_to_continue(page)
            clicked = await click_start_applications(page)
        except RuntimeError as exc:
            logger.warning(
                "application_only_start_applications_missing call_id=%s error=%s",
                getattr(session, "call_id", "-"),
                exc,
            )
            return {
                "ok": False,
                "state": "opening_application",
                "spoken": (
                    "I can see the program summary, but I couldn't click "
                    "Start Applications, so I did not ask question 1."
                ),
            }
        logger.info(
            "application_only_start_applications call_id=%s clicked=%s",
            getattr(session, "call_id", "-"),
            clicked,
        )
        started = await wait_for_application_started(page)
        if not started:
            live = await detect_application_screen(page)
            logger.warning(
                "application_only_start_applications_no_transition call_id=%s "
                "screen=%s",
                getattr(session, "call_id", "-"),
                live or "-",
            )
            return {
                "ok": False,
                "state": "opening_application",
                "spoken": (
                    "I clicked Start Applications, but the first application "
                    "question did not open, so I did not continue."
                ),
            }
    # Mid-flow drafts (for example question 11) resume as app_*. Always Back
    # to question 1 before speaking — never leave the browser on the prior page.
    # Exception: APPLICATION_START_AT skips ahead for targeted question tests.
    screen = await detect_application_screen(page)
    if not (screen or "").startswith("app_") and str(surface).startswith("app_"):
        screen = surface
    if not screen or not str(screen).startswith("app_"):
        logger.info(
            "application_only_no_app_screen call_id=%s surface=%s screen=%s",
            getattr(session, "call_id", "-"),
            surface,
            screen or "-",
        )
        return {
            "ok": False,
            "state": "opening_application",
            "spoken": (
                "The application screen is not open yet, "
                "so I did not ask question 1."
            ),
        }
    from . import config as _cfg

    start_at = str(getattr(_cfg, "APPLICATION_START_AT", "") or "").strip()
    if start_at == "app_address_review":
        return await _land_application_only_at_address(
            session, page, set_state, from_screen=str(screen)
        )
    if start_at == "app_income":
        return await _land_application_only_at_income(
            session, page, set_state, from_screen=str(screen)
        )
    set_state("resetting_to_question_1")
    logger.info(
        "application_only_resetting call_id=%s from_screen=%s",
        getattr(session, "call_id", "-"),
        screen,
    )
    try:
        await restart_application_to_first_question(page)
    except RuntimeError as exc:
        live = await detect_application_screen(page)
        logger.warning(
            "application_only_reset_blocked error=%s live_screen=%s",
            exc,
            live or "-",
        )
        return {
            "ok": False,
            "state": "resetting_to_question_1",
            "spoken": (
                "I couldn't safely return this application to question 1. "
                + str(exc)
            ),
        }
    verified = await _verified_application_question_one(page)
    if not verified:
        live = await detect_application_screen(page)
        body = (await _page_body_lower(page))[:220]
        logger.warning(
            "application_only_q1_unverified call_id=%s screen=%s body=%r",
            getattr(session, "call_id", "-"),
            live or "-",
            body,
        )
        return {
            "ok": False,
            "state": "resetting_to_question_1",
            "spoken": (
                "Question 1 is not the screen that is open, "
                "so I did not continue the test."
            ),
        }
    # Fresh conversational state after the browser is confirmed on Q1.
    reset_application_only_session(session)
    session.signed_in = True
    session.welcome_done = True
    session.app_step = verified
    session.filled["application_started"] = "1"
    session.filled["resume_app_step"] = verified
    question = app_question_for_step(verified, session)
    _set_pending_question(session, verified, question)
    set_state("application_ready")
    return {
        "ok": True,
        "state": "application_ready",
        "step": verified,
        "spoken": question,
    }


def _application_origin(page: Page) -> str:
    """Live Pointcare origin, falling back to production."""
    raw = str(getattr(page, "url", "") or "").strip()
    origin = "https://my.pointcare.com"
    if raw.startswith("http"):
        try:
            from urllib.parse import urlparse

            parsed = urlparse(raw)
            if parsed.scheme and parsed.netloc:
                origin = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            pass
    return origin


def _application_work_and_income_url(page: Page) -> str:
    """Build the Work & Income URL from the live origin when possible."""
    return f"{_application_origin(page)}/application/work-and-income"


def _application_address_url(page: Page) -> str:
    """Build the Address (Q4) URL from the live origin when possible."""
    return f"{_application_origin(page)}/application/address"


async def _land_application_only_at_address(
    session: Any,
    page: Page,
    set_state,
    *,
    from_screen: str = "",
) -> dict[str, Any]:
    """Skip Q1–Q3 and open Address for application-only Q4 testing.

    Requires a Pointcare draft that can open Address (earlier questions already
    completed in the browser). Does not change Q1–Q3 Collect Mode code.
    """
    set_state("skipping_to_address")
    logger.info(
        "application_only_skip_to_address call_id=%s from_screen=%s",
        getattr(session, "call_id", "-"),
        from_screen or "-",
    )
    reset_application_only_session(session)

    already = False
    try:
        live = await detect_application_screen(page)
        already = live == "app_address_review"
    except Exception:
        already = False
    if not already:
        target = _application_address_url(page)
        try:
            await page.goto(target, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(700)
        except Exception as exc:
            logger.warning(
                "application_only_address_goto_failed call_id=%s error=%s",
                getattr(session, "call_id", "-"),
                type(exc).__name__,
            )
            return {
                "ok": False,
                "state": "skipping_to_address",
                "spoken": (
                    "I couldn't open the address page for this test. "
                    "Leave the Pointcare draft on Address, or clear "
                    "POINTCARE_APPLICATION_START_AT to start at question 1."
                ),
            }

    verified = False
    for _ in range(8):
        live = await detect_application_screen(page)
        if live == "app_address_review":
            verified = True
            break
        await page.wait_for_timeout(350)
    if not verified:
        live = await detect_application_screen(page)
        logger.warning(
            "application_only_address_unverified call_id=%s screen=%s",
            getattr(session, "call_id", "-"),
            live or "-",
        )
        return {
            "ok": False,
            "state": "skipping_to_address",
            "spoken": (
                "Address is not the screen that is open, so I did not continue "
                "the Q4 test. Finish date of birth in the browser first, or "
                "clear POINTCARE_APPLICATION_START_AT to start at question 1."
            ),
        }

    step, speech = await _prepare_address_review_speech(session, page)
    session.signed_in = True
    session.welcome_done = True
    session.app_step = step or "app_address_review"
    session.filled["application_started"] = "1"
    session.filled["resume_app_step"] = session.app_step
    session.filled["application_start_at"] = "app_address_review"
    _set_pending_question(session, session.app_step, speech)
    set_state("application_ready")
    logger.info(
        "application_only_address_ready call_id=%s step=%s",
        getattr(session, "call_id", "-"),
        session.app_step,
    )
    return {
        "ok": True,
        "state": "application_ready",
        "step": session.app_step,
        "spoken": speech,
    }


async def _land_application_only_at_income(
    session: Any,
    page: Page,
    set_state,
    *,
    from_screen: str = "",
) -> dict[str, Any]:
    """Skip Q1–Q8 and open Work & Income for application-only Q9 testing.

    Requires a Pointcare draft that can open Work & Income (Q1–Q8 already
    completed in the browser). Does not modify tax / Q8 Collect Mode code.
    """
    from .income_stage import prepare_income_speech, probe_income_screen

    set_state("skipping_to_income")
    logger.info(
        "application_only_skip_to_income call_id=%s from_screen=%s",
        getattr(session, "call_id", "-"),
        from_screen or "-",
    )
    reset_application_only_session(session)

    already = False
    try:
        probe = await probe_income_screen(page)
        already = bool(probe.get("is_income"))
    except Exception:
        already = False
    if not already:
        live = await detect_application_screen(page)
        already = live == "app_income"
    if not already:
        target = _application_work_and_income_url(page)
        try:
            await page.goto(target, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(700)
        except Exception as exc:
            logger.warning(
                "application_only_income_goto_failed call_id=%s error=%s",
                getattr(session, "call_id", "-"),
                type(exc).__name__,
            )
            return {
                "ok": False,
                "state": "skipping_to_income",
                "spoken": (
                    "I couldn't open the work and income page for this test. "
                    "Leave the Pointcare draft on Work & Income, or clear "
                    "POINTCARE_APPLICATION_START_AT to start at question 1."
                ),
            }

    verified = False
    for _ in range(8):
        live = await detect_application_screen(page)
        if live == "app_income":
            verified = True
            break
        try:
            probe = await probe_income_screen(page)
        except Exception:
            probe = {}
        if probe.get("is_income"):
            verified = True
            break
        await page.wait_for_timeout(350)
    if not verified:
        live = await detect_application_screen(page)
        logger.warning(
            "application_only_income_unverified call_id=%s screen=%s",
            getattr(session, "call_id", "-"),
            live or "-",
        )
        return {
            "ok": False,
            "state": "skipping_to_income",
            "spoken": (
                "Work and income is not the screen that is open, so I did not "
                "continue the Q9 test. Finish tax in the browser first, or clear "
                "POINTCARE_APPLICATION_START_AT to start at question 1."
            ),
        }

    step, speech = await prepare_income_speech(session, page)
    session.signed_in = True
    session.welcome_done = True
    session.app_step = step or "app_income"
    session.filled["application_started"] = "1"
    session.filled["resume_app_step"] = session.app_step
    session.filled["application_start_at"] = "app_income"
    _set_pending_question(session, session.app_step, speech)
    set_state("application_ready")
    logger.info(
        "application_only_income_ready call_id=%s step=%s",
        getattr(session, "call_id", "-"),
        session.app_step,
    )
    return {
        "ok": True,
        "state": "application_ready",
        "step": session.app_step,
        "spoken": speech,
    }


def build_start_confirmation(summary: dict[str, Any]) -> str:
    """Explain eligible programs on the page, then ask before Start Applications.

    Do not announce programs marked not eligible.
    """
    return build_programs_permission_speech(summary)


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


async def _wait_application_only_surface(page: Page, timeout_s: float = 15.0) -> str:
    """Poll until the saved dashboard, program summary, or an application step shows."""
    deadline = time.monotonic() + timeout_s
    last = "unknown"
    while time.monotonic() < deadline:
        last = await classify_saved_household_page(page)
        if last in ("dashboard", "programs") or str(last).startswith("app_"):
            return last
        await page.wait_for_timeout(400)
    return last


async def _click_quick_apply_when_ready(page: Page, timeout_s: float = 8.0) -> str:
    """Click Continue Quick Apply once the control is actually in the page."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        clicked = await click_quick_apply(page)
        if clicked:
            return clicked
        await page.wait_for_timeout(400)
    return ""


async def _wait_surface_after_quick_apply(page: Page, timeout_s: float = 15.0) -> str:
    """Wait until Quick Apply lands on programs or an application step.

    Do not treat a transitional ``unknown`` page as success — that used to
    abort before Back-to-question-1 when Continue Quick Apply resumed a
    mid-flow draft (for example question 11).
    """
    deadline = time.monotonic() + timeout_s
    last = "unknown"
    while time.monotonic() < deadline:
        await page.wait_for_timeout(400)
        last = await classify_saved_household_page(page)
        if last == "programs" or str(last).startswith("app_"):
            return last
    return last


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
            session.filled.pop("app_insurance_coverage_phase", None)
            session.filled.pop("app_insurance_current_types", None)
            session.filled.pop("app_insurance_completed", None)
            session.filled.pop("app_insurance_other_text", None)
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
        await ensure_programs_ready_to_continue(page)
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
    await ensure_programs_ready_to_continue(page)
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
    next_step = await wait_for_verified_first_stage_e_step(page, timeout_ms=12000)
    if not next_step:
        screen = await detect_application_screen(page)
        if screen in _FIRST_STAGE_E_STEPS:
            next_step = screen
    if not next_step:
        logger.error(
            "stage_e_first_question_missing call_id=%s clicked=%s",
            call_id,
            clicked,
        )
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "eligibility_answer",
            "value": answer,
            "error": "stage_e_first_question_missing",
            "spoken": (
                "ASK: I've opened the application, but the first question is "
                "still loading. Please say next when you're ready."
            ),
            "message": "Application started but first question not verified",
            "step": "confirm_start_application",
            "phase": "eligibility",
        }
    session.app_step = next_step
    first_question = await _first_stage_e_question_speech(page, session, next_step)
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
        "Started application",
        spoken_override=intro,
    )


def _programs_need_medicaid_fallback(summary: dict[str, Any]) -> bool:
    """True when Great News has no usable eligible selection yet.

    Pointcare can show \"John may be eligible…\" on MassHealth while the headline
    still says \"0 eligible programs\" and Continue is disabled. Force MassHealth
    only in that empty/disabled case. When programs are already eligible /
    selected, leave the page alone — the caller's Yes is enough.
    """
    programs = [
        p for p in (summary.get("programs") or []) if isinstance(p, dict)
    ]
    body = str(summary.get("body_sample") or "")
    eligible = [p for p in programs if p.get("may_be_eligible")]
    checked_eligible = [p for p in eligible if p.get("checked")]
    if checked_eligible:
        return False
    if re.search(r"\b0\s+eligible\s+programs?\b|found\s+0\s+eligible", body, re.I):
        return True
    if re.search(r"applying\s+to\s+0\s+programs?", body, re.I):
        return True
    if not eligible:
        return True
    # Eligible program cards are present — do not force MassHealth.
    return False


async def select_masshealth_commonhealth_program(page: Page) -> dict[str, Any]:
    """Check only MassHealth CommonHealth (Medicaid); leave Medicare/ACA unchecked.

    Also checks Who-is-applying members under that one card. Explicitly unchecks
    other program cards so Continue applies to Medicaid only.
    """
    try:
        result = await page.evaluate(
            """() => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const low = (s) => norm(s).toLowerCase();
              const present = (el) => {
                if (!el) return false;
                let cur = el;
                while (cur && cur.nodeType === 1) {
                  const st = window.getComputedStyle(cur);
                  if (st.display === 'none' || st.visibility === 'hidden') return false;
                  cur = cur.parentElement;
                }
                return true;
              };
              const isChecked = (box) => {
                if (!box) return false;
                if (box.matches && box.matches('input[type=checkbox]')) {
                  return !!box.checked;
                }
                return (box.getAttribute('aria-checked') || '').toLowerCase() === 'true';
              };
              const setChecked = (box, want) => {
                if (!box || !present(box)) return false;
                const already = isChecked(box);
                if (already === want) return true;
                try {
                  box.dispatchEvent(new MouseEvent('click', {
                    bubbles: true, cancelable: true, view: window,
                  }));
                } catch (e) {}
                try { box.click(); } catch (e2) {}
                if (typeof box.onclick === 'function') {
                  try { box.onclick(); } catch (e3) {}
                }
                if (box.matches && box.matches('input[type=checkbox]')) {
                  box.checked = !!want;
                  box.dispatchEvent(new Event('input', { bubbles: true }));
                  box.dispatchEvent(new Event('change', { bubbles: true }));
                }
                if (box.getAttribute && box.getAttribute('role') === 'checkbox') {
                  box.setAttribute('aria-checked', want ? 'true' : 'false');
                }
                return isChecked(box) === want || true;
              };
              const titleLines = (el) =>
                ((el.innerText || el.textContent || '') + '')
                  .split(/\\r?\\n/)
                  .map(norm)
                  .filter(Boolean);
              const programKind = (text) => {
                const t = low(text);
                if (/masshealth|commonhealth/.test(t) || (
                  /\\bmedicaid\\b|medi-?cal|health first colorado/.test(t)
                  && !/medicare|marketplace|health connector|covered california/.test(t)
                )) {
                  return 'medicaid';
                }
                if (/\\bmedicare\\b/.test(t) && !/medicaid|commonhealth|masshealth/.test(t)) {
                  return 'medicare';
                }
                if (/marketplace|health connector|covered california|\\baca\\b/.test(t)
                    && !/medicaid|commonhealth|masshealth/.test(t)) {
                  return 'marketplace';
                }
                return '';
              };
              const programCheckbox = (card) => {
                if (card.matches && card.matches('[role=checkbox]') && present(card)) {
                  return card;
                }
                const boxes = [];
                let pastWho = false;
                const nodes = [card, ...card.querySelectorAll('*')];
                for (const node of nodes) {
                  const t = ((node.innerText || node.textContent || '') + '').trim();
                  if (t.length < 40 && /who is applying/i.test(t)) {
                    pastWho = true;
                    continue;
                  }
                  if (pastWho) continue;
                  if (!(node.matches && node.matches(
                    'input[type=checkbox], [role=checkbox]'
                  ))) continue;
                  if (!present(node)) continue;
                  boxes.push(node);
                }
                if (!boxes.length) return null;
                const roleFirst = boxes.find(
                  (b) => b.getAttribute && b.getAttribute('role') === 'checkbox'
                );
                return roleFirst || boxes[0];
              };
              // Single-program cards only — reject wrappers that list multiple programs.
              const isSingleProgramCard = (el) => {
                if (!present(el)) return false;
                const text = norm(el.innerText || el.textContent || '');
                if (text.length < 8 || text.length > 520) return false;
                const kinds = new Set();
                for (const line of titleLines(el)) {
                  if (line.length > 90) continue;
                  const kind = programKind(line);
                  if (kind) kinds.add(kind);
                }
                // Also catch cards whose body only has one program phrase.
                if (!kinds.size) {
                  const kind = programKind(text);
                  if (kind) kinds.add(kind);
                }
                return kinds.size === 1;
              };
              const allCards = [...document.querySelectorAll(
                'section, article, li, div'
              )].filter((el) => isSingleProgramCard(el) && !!programCheckbox(el));
              // Smallest single-program card wins (avoid outer wrappers).
              const byKind = (want) => allCards
                .filter((el) => {
                  const lines = titleLines(el);
                  const hit = lines.find((line) => programKind(line) === want)
                    || (programKind(el.innerText || '') === want ? (el.innerText || '') : '');
                  return !!hit;
                })
                .sort((a, b) =>
                  norm(a.innerText || '').length - norm(b.innerText || '').length
                );
              const medicaidCards = byKind('medicaid').filter((el) => {
                const t = low(el.innerText || '');
                // Prefer MassHealth CommonHealth when present.
                return /masshealth|commonhealth|medicaid|medi-?cal|health first colorado/.test(t);
              });
              medicaidCards.sort((a, b) => {
                const ta = low(a.innerText || '');
                const tb = low(b.innerText || '');
                const sa = /masshealth\\s+commonhealth|commonhealth/.test(ta) ? 0
                  : /masshealth/.test(ta) ? 1 : 2;
                const sb = /masshealth\\s+commonhealth|commonhealth/.test(tb) ? 0
                  : /masshealth/.test(tb) ? 1 : 2;
                if (sa !== sb) return sa - sb;
                return norm(a.innerText || '').length - norm(b.innerText || '').length;
              });
              if (!medicaidCards.length) {
                return { ok: false, error: 'masshealth_card_not_found' };
              }
              const card = medicaidCards[0];
              const title = norm(
                titleLines(card).find((line) => programKind(line) === 'medicaid')
                || titleLines(card)[0]
                || ''
              ).slice(0, 80);
              const box = programCheckbox(card);
              const before = isChecked(box);
              setChecked(box, true);

              // Who is applying? — only inside THIS medicaid card (never page-wide).
              let membersChecked = 0;
              const nodes = [...card.querySelectorAll(
                'label, li, div, [role="checkbox"], input[type=checkbox]'
              )];
              let inWho = false;
              for (const node of nodes) {
                const t = low(node.innerText || node.textContent || '');
                if (/who is applying/.test(t)) {
                  inWho = true;
                  continue;
                }
                if (!inWho) continue;
                if (/continue to applications|start applications|return to dashboard|applying to \\d/.test(t)) {
                  break;
                }
                const memberBox = node.matches && (
                  node.matches('input[type=checkbox]') || node.matches('[role=checkbox]')
                ) ? node : (node.querySelector && (
                  node.querySelector('input[type=checkbox], [role=checkbox]')
                ));
                if (!memberBox || !present(memberBox)) continue;
                if (memberBox === box) continue;
                if (setChecked(memberBox, true)) membersChecked += 1;
              }

              // Force Medicare / Marketplace program cards OFF.
              let uncheckedOthers = 0;
              for (const kind of ['medicare', 'marketplace']) {
                for (const other of byKind(kind)) {
                  // Skip if this node is inside the medicaid card we selected.
                  if (card.contains(other) || other.contains(card)) continue;
                  const otherBox = programCheckbox(other);
                  if (!otherBox || otherBox === box) continue;
                  if (isChecked(otherBox)) {
                    setChecked(otherBox, false);
                    uncheckedOthers += 1;
                  }
                }
              }

              return {
                ok: true,
                name: title,
                was_checked: before,
                now_checked: isChecked(box),
                box_id: box.id || '',
                box_aria: box.getAttribute ? (box.getAttribute('aria-checked') || '') : '',
                members_checked: membersChecked,
                unchecked_others: uncheckedOthers,
              };
            }"""
        )
    except Exception as exc:
        logger.exception("select_masshealth_commonhealth_program failed")
        return {"ok": False, "error": str(exc)[:200]}
    if not isinstance(result, dict):
        return {"ok": False, "error": "invalid_select_result"}
    # Playwright click fallback: only the MassHealth card checkbox.
    if result.get("ok") and not result.get("now_checked"):
        try:
            loc = page.get_by_text(
                re.compile(
                    r"MassHealth\s+CommonHealth\s*\(Medicaid\)|MassHealth\s+CommonHealth",
                    re.I,
                )
            ).first
            card = loc.locator(
                "xpath=ancestor::*[self::section or self::article or self::li][1]"
            )
            box = card.locator('[role="checkbox"], input[type="checkbox"]').first
            if await box.count() > 0:
                await box.click(timeout=2500, force=True)
                result["playwright_click"] = True
        except Exception:
            logger.info("masshealth playwright click fallback skipped", exc_info=True)
    try:
        await page.wait_for_timeout(400)
    except Exception:
        pass
    return result


async def ensure_programs_ready_to_continue(page: Page) -> dict[str, Any]:
    """Before Continue/Start: select MassHealth only when nothing is eligible.

    If Pointcare already shows eligible program(s), leave the UI alone and let
    the caller's Yes drive Continue / Start Applications.
    """
    summary = await scrape_program_summary(page)
    eligible = [
        p
        for p in (summary.get("programs") or [])
        if isinstance(p, dict) and p.get("may_be_eligible")
    ]
    if not _programs_need_medicaid_fallback(summary):
        reason = "already_selected" if any(
            p.get("checked") for p in eligible
        ) else "eligible_present"
        logger.info(
            "programs_ready reason=%s eligible=%s",
            reason,
            len(eligible),
        )
        return {
            "ok": True,
            "selected": False,
            "reason": reason,
            "eligible_count": len(eligible),
        }

    selected = await select_masshealth_commonhealth_program(page)
    logger.info(
        "programs_medicaid_fallback ok=%s name=%r members=%s error=%s",
        selected.get("ok"),
        str(selected.get("name") or "")[:60],
        selected.get("members_checked"),
        selected.get("error") or "",
    )
    # Card expand reveals "Who is applying?" — pass again for member boxes.
    if selected.get("ok"):
        try:
            await page.wait_for_timeout(350)
            again = await select_masshealth_commonhealth_program(page)
            if again.get("ok"):
                selected = again
        except Exception:
            pass
    return {
        "ok": bool(selected.get("ok")),
        "selected": bool(selected.get("ok")),
        "reason": "medicaid_fallback",
        "detail": selected,
        "eligible_count": len(eligible),
    }


async def click_start_applications(page: Page) -> str:
    """Click the visible, enabled Start Applications control exactly once.

    Prefers an enabled role=button / role=link match, then exact visible text
    (Pointcare's yellow CTA is often a div). Does not click a second time.
    """
    from .live_form import _click_cta

    labeled = (
        (re.compile(r"^\s*Start Applications\s*$", re.I), "Start Applications"),
        (re.compile(r"^\s*Start Application\s*$", re.I), "Start Application"),
        (
            re.compile(r"^\s*Continue to Applications\s*$", re.I),
            "Continue to Applications",
        ),
        (
            re.compile(r"^\s*Continue to Application\s*$", re.I),
            "Continue to Application",
        ),
    )
    for pattern, label in labeled:
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=pattern)
                count = await loc.count()
            except Exception:
                continue
            for i in range(count):
                btn = loc.nth(i)
                try:
                    if not await btn.is_visible():
                        continue
                    try:
                        if await btn.is_disabled():
                            continue
                    except Exception:
                        pass
                    aria = (
                        await btn.get_attribute("aria-disabled") or ""
                    ).strip().lower()
                    if aria in ("true", "1"):
                        continue
                    await btn.click(timeout=4000)
                    await page.wait_for_timeout(500)
                    return label
                except Exception:
                    continue
    for _pattern, label in labeled:
        if await click_exact_visible_text(page, label):
            await page.wait_for_timeout(500)
            return label
    try:
        await _click_cta(page, "Start Applications", attempts=1)
        await page.wait_for_timeout(500)
        return "Start Applications"
    except Exception as exc:
        raise RuntimeError(
            "Could not find Start Applications on the program summary screen: "
            + str(exc)
        ) from exc


async def wait_for_application_started(page: Page, *, timeout_ms: int = 20000) -> bool:
    """True when Pointcare left the Great News summary for an application step.

    Do not treat an ``/application`` or ``/apply`` URL alone as success — the
    program-summary page often already uses those paths, which used to mark
    Start Applications "done" while Great News was still on screen.
    """
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        screen = await detect_application_screen(page)
        if (screen or "").startswith("app_"):
            return True
        if screen == "programs":
            await page.wait_for_timeout(350)
            continue
        body = await _page_body_lower(page)
        if "great news" in body or (
            "start applications" in body
            and ("may be eligible" in body or "not eligible" in body)
        ):
            await page.wait_for_timeout(350)
            continue
        # Require a real first-question cue. Bare "Step 1 of N" while loading
        # used to mark success too early, then defaulted speech to citizenship
        # while the licensed-agent offer was still painting.
        if "start applications" not in body and (
            "licensed agent" in body
            or "clearguide" in body
            or (
                "connect me" in body
                and ("medicare" in body or "marketplace" in body or "agent" in body)
            )
            or "u.s. citizen" in body
            or "us citizen" in body
            or "application eligibility" in body
        ):
            return True
        await page.wait_for_timeout(350)
    return False


async def detect_application_screen(page: Page) -> str:
    """Map the visible application UI to an app_* step key.

    Never invent ``app_citizenship`` from the URL alone — unknown application
    pages return ``\"\"`` so resume can report a recoverable error.
    """
    tax_probe: dict[str, Any] = {}
    income_probe: dict[str, Any] = {}
    expense_probe: dict[str, Any] = {}
    try:
        from .tax_stage import probe_tax_information_screen

        tax_probe = await probe_tax_information_screen(page)
    except Exception:
        tax_probe = {}
    try:
        from .income_stage import probe_income_screen

        income_probe = await probe_income_screen(page)
    except Exception:
        income_probe = {}
    try:
        from .expense_stage import probe_expense_screen

        expense_probe = await probe_expense_screen(page)
    except Exception:
        expense_probe = {}
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
    if _is_household_dashboard_text(body):
        return ""
    # Agent offer is often the first Stage E screen. Prefer it over citizenship
    # chrome that can linger in the SPA shell while this question paints.
    if (
        "licensed agent" in body
        or "clearguide" in body
        or (
            "connect me" in body
            and ("medicare" in body or "marketplace" in body or "agent" in body)
        )
        or (
            "insurance agent" in body
            and ("medicare" in body or "marketplace" in body or "enrollment" in body)
        )
    ):
        return "app_agent_offer"
    # Later Stage E questions often still mention "U.S. Citizen" in household
    # chrome. Match the active question first so citizenship Yes→Next cannot be
    # mis-read as still-on-citizenship (and then Next again skips insurance).
    if (
        "who has coverage" in body
        or "coverage statuses" in body
        or "someone has coverage" in body
        or "no one has coverage" in body
        or "nobody has coverage" in body
        or "no one has health" in body
        or "health insurance" in body
        or (
            "insurance" in body
            and ("anyone" in body or "coverage" in body or "have health" in body)
        )
    ):
        return "app_insurance"
    if (
        "date of birth" in body
        or re.search(r"\bdob\b", body)
        or "add date of birth" in body
        or "needs date of birth" in body
    ):
        return "app_dob_review"
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
    if "home address" in body or "mailing address" in body:
        # Review screens may already show saved addresses without Add/pencil.
        return "app_address_review"
    has_childcare = "children under 19" in body or "caring for children" in body
    has_marital = (
        "current marital statuses" in body
        or (
            "marital status" in body
            and (
                "never married" in body
                or "registered domestic partner" in body
                or "common law marriage" in body
            )
        )
    )
    # Prefer childcare when both markers appear — SPA transitions often leave
    # marital radio labels in the body after Next has already moved on.
    if has_childcare:
        return "app_childcare_review"
    if has_marital:
        return "app_marital_review"
    income_body_cues = (
        "work & income" in body
        or "work and income" in body
        or "add income" in body
        or "add another income" in body
        or "income type" in body
        or "select income type" in body
        or (
            "employed" in body
            and "self-employed" in body
            and "none" in body
        )
    )
    # Prefer Work & Income over stale tax chrome after tax → Next.
    if income_probe.get("is_income") or income_body_cues:
        logger.info(
            "detect_screen=app_income reason=%s heading=%r adds=%s cards=%s",
            income_probe.get("reason") or "body_text",
            income_probe.get("heading"),
            income_probe.get("add_count"),
            income_probe.get("card_count"),
        )
        return "app_income"
    if income_probe.get("loading"):
        logger.info(
            "detect_screen=loading expected_hint=app_income heading=%r snippet=%r",
            income_probe.get("heading"),
            (income_probe.get("snippet") or "")[:120],
        )
        return ""
    # Tax Information before circumstances: SPA transitions often leave
    # "veteran" / "daily activities" labels in the DOM while Tax is visible.
    if tax_probe.get("is_tax"):
        logger.info(
            "detect_screen=app_tax reason=%s heading=%r edits=%s statuses=%s",
            tax_probe.get("reason"),
            tax_probe.get("heading"),
            tax_probe.get("edit_count"),
            tax_probe.get("status_hits"),
        )
        return "app_tax"
    if tax_probe.get("loading"):
        logger.info(
            "detect_screen=loading expected_hint=app_tax heading=%r snippet=%r",
            tax_probe.get("heading"),
            (tax_probe.get("snippet") or "")[:120],
        )
        return ""
    tax_body_cues = (
        "tax information" in body
        or "filing status" in body
        or "head of household" in body
        or "not filing" in body
        or ("tax" in body and "dependent" in body)
        or (
            "tax" in body
            and "edit" in body
            and (
                "single" in body
                or "married filing" in body
                or "household" in body
            )
        )
    )
    has_circumstances = (
        "other statuses" in body
        or "daily activities" in body
        or "alaska native" in body
        or "american indian" in body
        or ("veteran" in body and ("student 18" in body or "military" in body))
    )
    # Prefer tax when page shows tax cues — ignore leftover circumstances labels.
    if tax_body_cues:
        logger.info(
            "detect_screen=app_tax reason=body_text_fallback snippet=%r",
            body[:160],
        )
        return "app_tax"
    if has_circumstances:
        return "app_circumstances"
    if expense_probe.get("is_expense") or (
        "household expense" in body
        or "add deductible and medical expense" in body
        or "add an expense" in body
        or "add expense" in body
        or (
            "student loan" in body
            and ("childcare" in body or "child care" in body)
            and ("expense" in body)
        )
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


def _set_income_seq(session: Any, data: dict[str, Any] | None) -> None:
    """Compat wrapper — income sequence state lives in income_stage."""
    from .income_stage import _set_income_seq as _set_seq

    _set_seq(session, data)


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
    # Persist a standalone day such as "twenty two" or "22nd". The previous
    # parser asked for the missing month/year but discarded this accepted day,
    # so the following answer could never complete the date.
    if day is None and not month_match and not year_match:
        day_only = re.fullmatch(
            r"(?:date|day)?\s*(?:is\s+)?(?:the\s+)?"
            r"([a-z0-9 -]+?)(?:st|nd|rd|th)?",
            lowered,
        )
        if day_only:
            candidate = _dob_word_number(day_only.group(1).strip())
            if candidate is not None and 1 <= candidate <= 31:
                day = candidate
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
        and day is None
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


async def _verified_application_question_one(page: Page) -> str:
    """Return the first Stage E step only when detect and page text agree.

    Never trusts URL or a previous call's ``app_step``. Citizenship requires
    citizen wording; agent-offer requires licensed-agent / ClearGuide cues.
    """
    body = await _page_body_lower(page)
    # Prefer agent-offer cues even when household chrome still mentions citizens.
    if body and (
        "licensed agent" in body
        or "clearguide" in body
        or "connect me with" in body
        or (
            "connect me" in body
            and ("medicare" in body or "marketplace" in body or "agent" in body)
        )
    ):
        return "app_agent_offer"
    screen = await detect_application_screen(page)
    if screen not in _FIRST_STAGE_E_STEPS:
        return ""
    if not body:
        # Scrape failed; detect already mapped a first-step key from prior reads.
        return screen
    if screen == "app_citizenship":
        if "citizen" not in body:
            return ""
        # Loading spinner / Step 1 chrome alone is not enough when agent copy
        # has not painted yet — wait_for_verified_* will poll again.
        return screen
    if screen == "app_agent_offer":
        if not (
            "licensed agent" in body
            or "clearguide" in body
            or "connect me" in body
        ):
            return ""
        return screen
    return ""


async def wait_for_verified_first_stage_e_step(
    page: Page, *, timeout_ms: int = 12000
) -> str:
    """Wait until the first real Stage E question is visible and verified.

    Avoids the latest-call bug: Start Applications lands on licensed-agent
    offer, but a premature default to ``app_citizenship`` spoke the wrong
    question and only recovered after the caller talked again.
    """
    deadline = time.monotonic() + max(0.5, timeout_ms / 1000.0)
    last = ""
    while time.monotonic() < deadline:
        step = await _verified_application_question_one(page)
        if step in _FIRST_STAGE_E_STEPS:
            if step != last:
                logger.info("stage_e_first_question_verified step=%s", step)
            return step
        last = step or last
        try:
            await page.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)
    final = await _verified_application_question_one(page)
    logger.warning(
        "stage_e_first_question_unverified last=%s final=%s",
        last or "-",
        final or "-",
    )
    return final if final in _FIRST_STAGE_E_STEPS else ""


async def _first_stage_e_question_speech(page: Page, session: Any, step: str) -> str:
    """Spoken first Stage E question matching the live screen."""
    if step == "app_agent_offer":
        return app_question_for_step(step, session)
    if step == "app_citizenship":
        body = await _page_body_lower(page)
        # Prefer the household wording when the form asks about all members.
        if body and (
            "household members" in body
            or "everyone listed" in body
            or re.search(r"\ball\b.{0,40}\bcitizen", body)
        ):
            return APP_QUESTIONS["app_citizenship"]
        return _citizenship_question(
            await _citizenship_household_names(page, session)
        )
    return app_question_for_step(step, session)


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


async def _application_back_available(page: Page) -> bool:
    """True when a visible, enabled application Back control is present."""
    try:
        backs = page.get_by_role("button", name=re.compile(r"^\s*Back\s*$", re.I))
        count = await backs.count()
    except Exception:
        return False
    for i in range(count):
        btn = backs.nth(i)
        try:
            if not await btn.is_visible():
                continue
            try:
                if await btn.is_disabled():
                    continue
            except Exception:
                pass
            aria = (await btn.get_attribute("aria-disabled") or "").strip().lower()
            if aria in ("true", "1"):
                continue
            return True
        except Exception:
            continue
    return False


async def _click_application_back(page: Page) -> bool:
    """Click a visible enabled Back control on the application. Returns True if clicked."""
    try:
        backs = page.get_by_role("button", name=re.compile(r"^\s*Back\s*$", re.I))
        count = await backs.count()
        for i in range(count):
            btn = backs.nth(i)
            try:
                if not await btn.is_visible():
                    continue
                try:
                    if await btn.is_disabled():
                        continue
                except Exception:
                    pass
                aria = (await btn.get_attribute("aria-disabled") or "").strip().lower()
                if aria in ("true", "1"):
                    continue
                await btn.click(timeout=4000)
                await page.wait_for_timeout(450)
                return True
            except Exception:
                continue
    except Exception:
        pass
    try:
        from .live_form import _click_cta

        await _click_cta(page, "Back", attempts=1)
        await page.wait_for_timeout(450)
        return True
    except Exception:
        return False


async def _left_application_surface(page: Page) -> bool:
    """True when Back left the in-progress application (programs/dashboard)."""
    body = await _page_body_lower(page)
    if not body:
        return False
    if _is_household_dashboard_text(body):
        return True
    if "great news" in body:
        return True
    if "start applications" in body and (
        "not eligible" in body or "may be eligible" in body or "applying to" in body
    ):
        return True
    if "continue to applications" in body and "eligible" in body:
        return True
    return False


async def _application_body_signature(page: Page) -> str:
    """Compact body fingerprint so Back progress is visible on unrecognized screens."""
    body = await _page_body_lower(page)
    if not body:
        return ""
    return f"{len(body)}:{body[:240]}"


async def restart_application_to_first_question(page: Page) -> str:
    """Navigate the live application back to the first Stage E question.

    Uses Pointcare's Back control only — does not create a new draft, clear
    auth, or erase household eligibility. Returns the verified first-step key
    (``app_agent_offer`` or ``app_citizenship``). Raises RuntimeError when the
    UI cannot safely reach question 1.

    Unrecognized mid-flow screens (``detect`` returns empty) are common on
    Pointcare wording we have not mapped yet — keep clicking Back through those
    instead of aborting as if the application closed.
    """
    screen = await detect_application_screen(page)
    if screen in _FIRST_STAGE_E_STEPS:
        logger.info("stage_e_already_at_first_question screen=%s", screen)
        return screen
    if not (screen or "").startswith("app_"):
        if await _left_application_surface(page) or not await _application_back_available(
            page
        ):
            raise RuntimeError(
                "APPLICATION_TEST: expected an in-progress application screen to "
                f"restart from question 1, got {screen!r}."
            )

    unchanged_streak = 0
    for attempt in range(40):
        screen = await detect_application_screen(page)
        if screen in _FIRST_STAGE_E_STEPS:
            logger.info(
                "stage_e_restarted_to_first attempt=%s screen=%s",
                attempt,
                screen,
            )
            return screen
        if screen in ("programs", "dashboard"):
            raise RuntimeError(
                "APPLICATION_TEST: navigated away from the application before "
                "reaching question 1. Pointcare has no safe restart-to-step-1 "
                "control here without risking a duplicate draft. Open the "
                "existing application to step 1 manually, or leave the draft "
                f"on the first question. last_screen={screen!r}"
            )
        if not (screen or "").startswith("app_") and await _left_application_surface(
            page
        ):
            raise RuntimeError(
                "APPLICATION_TEST: navigated away from the application before "
                "reaching question 1. Pointcare has no safe restart-to-step-1 "
                "control here without risking a duplicate draft. Open the "
                "existing application to step 1 manually, or leave the draft "
                f"on the first question. last_screen={screen!r}"
            )
        before_screen = screen
        before_sig = await _application_body_signature(page)
        clicked = await _click_application_back(page)
        if not clicked:
            raise RuntimeError(
                "APPLICATION_TEST: cannot reach question 1 from "
                f"{before_screen or 'unrecognized'}; Back control is missing or "
                "inactive. Leave the in-progress application on the first question "
                "(licensed agent or citizenship) before the next test call."
            )
        # Wait for the screen or body text to change after Back.
        moved = False
        for _ in range(20):
            await page.wait_for_timeout(250)
            now_screen = await detect_application_screen(page)
            if now_screen != before_screen and (
                now_screen or before_screen
            ):
                moved = True
                break
            if await _application_body_signature(page) != before_sig:
                moved = True
                break
        logger.info(
            "stage_e_restart_back attempt=%s from=%s moved=%s",
            attempt,
            before_screen or "unrecognized",
            moved,
        )
        if not moved:
            unchanged_streak += 1
            if unchanged_streak >= 3:
                raise RuntimeError(
                    "APPLICATION_TEST: cannot reach question 1 from "
                    f"{before_screen or 'unrecognized'}; Back did not change the "
                    "screen. Leave the in-progress application on the first "
                    "question (licensed agent or citizenship) before the next "
                    "test call."
                )
        else:
            unchanged_streak = 0

    final = await detect_application_screen(page)
    raise RuntimeError(
        "APPLICATION_TEST: too many Back clicks without reaching question 1 "
        f"(still on {final or 'unrecognized'!r})."
    )


def _clean_member_display_name(name: str) -> str:
    """Strip UI chrome from a member label and make it safe to speak.

    Pointcare's Back control was being scraped as the first "member" name, so
    the agent said "For Back..." instead of "For Admin...".
    """
    label = re.sub(r"\s+", " ", (name or "").strip())
    # Pointcare marks the applicant as "Admin P. (you)" — never speak "you".
    label = re.sub(r"\s*\(\s*you\s*\)\s*", " ", label, flags=re.I)
    # Nav chrome is often glued onto the first card (Back sits under Admin P.).
    label = re.sub(
        r"\s+\b(add|edit|pencil|save|next|continue|back|cancel|exit|previous)\b\.?",
        "",
        label,
        flags=re.I,
    )
    label = re.sub(r"\s+", " ", label).strip(" ,.-")
    if not label:
        return "that person"
    if re.fullmatch(
        r"(back|next|continue|cancel|save|edit|add|exit|previous|done|close)",
        label,
        flags=re.I,
    ):
        return "that person"
    # Drop trailing initial period for clearer TTS: "John D." → "John D".
    label = re.sub(r"\b([A-Za-z])\.\s*$", r"\1", label)
    parts = label.split()
    # Applicant card: say "Admin" (not "Admin P") — clearer on the phone.
    if parts and parts[0].lower() == "admin":
        return "Admin"
    return label


_MONTH_SPEECH: dict[int, str] = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}

_DOB_COMPONENTS = frozenset({"month", "day", "year", "confirm", "fix_which"})


def _dob_component(session: Any) -> str:
    raw = str(session.filled.get("app_dob_component") or "month").strip().lower()
    return raw if raw in _DOB_COMPONENTS else "month"


def _set_dob_component(session: Any, component: str) -> None:
    session.filled["app_dob_component"] = (
        component if component in _DOB_COMPONENTS else "month"
    )


def _clear_dob_component(session: Any) -> None:
    session.filled.pop("app_dob_component", None)


def _dob_month_question(name: str) -> str:
    return f"What month was {_clean_member_display_name(name)} born?"


def _dob_day_question(_name: str = "") -> str:
    return "What day of the month were they born?"


def _dob_year_question(_name: str = "") -> str:
    return "What year were they born?"


def _dob_confirm_question(name: str, month: int, day: int, year: int) -> str:
    label = _MONTH_SPEECH.get(int(month), str(month))
    return (
        f"I have {label} {int(day)}, {int(year)}, for "
        f"{_clean_member_display_name(name)}. Is that correct?"
    )


def _dob_fix_which_question() -> str:
    return "Which part should I change — the month, day, or year?"


def _dob_spoken_from_slash(dob: str) -> str | None:
    """Turn ``MM/DD/YYYY`` into a natural phrase like ``October 22, 2003``."""
    parts = _dob_parts(dob)
    if not parts:
        return None
    month_s, day_s, year_s = parts
    label = _MONTH_SPEECH.get(int(month_s), month_s)
    return f"{label} {int(day_s)}, {year_s}"


def _dob_review_question(name: str, dob: str) -> str:
    """Ask the caller to confirm a DOB already saved on the Pointcare card."""
    spoken = _dob_spoken_from_slash(dob) or (dob or "").strip()
    label = _clean_member_display_name(name)
    return f"{label}'s date of birth is {spoken}. Is that correct?"


def _dob_ask_who_question() -> str:
    return "Whose date of birth should I change? Please say the name."


def _dob_reviewed_names(session: Any) -> set[str]:
    raw = session.filled.get("app_dob_reviewed") or ""
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except Exception:
        return set()
    if not isinstance(data, list):
        return set()
    return {str(x).strip().lower() for x in data if str(x).strip()}


def _mark_dob_reviewed(session: Any, name: str) -> None:
    key = (name or "").strip().lower()
    if not key:
        return
    names = _dob_reviewed_names(session)
    names.add(key)
    session.filled["app_dob_reviewed"] = json.dumps(sorted(names))


def _clear_dob_reviewed(session: Any) -> None:
    session.filled.pop("app_dob_reviewed", None)


def _dob_phase(session: Any) -> str:
    return str(session.filled.get("app_dob_phase") or "").strip().lower()


def _set_dob_phase(session: Any, phase: str) -> None:
    if phase:
        session.filled["app_dob_phase"] = phase
    else:
        session.filled.pop("app_dob_phase", None)


def _dob_card_by_name(
    cards: list[dict[str, Any]], name: str
) -> dict[str, Any] | None:
    want = (name or "").strip().lower()
    if not want:
        return None
    for card in cards:
        if str(card.get("name") or "").strip().lower() == want:
            return card
    return None


def _dob_explicit_confirm(text: str) -> bool:
    """True for an explicit Yes / that's right — not soft 'sure'."""
    from .live_form import _is_yes

    raw = (text or "").strip()
    lowered = raw.lower()
    return bool(
        (
            re.match(r"^(yes|yeah|yep|yup|yea)\b", raw, re.I)
            and _is_yes(text)
        )
        or re.search(
            r"\b(that(?:'s| is)? right|that(?:'s| is)? correct|looks good)\b",
            lowered,
        )
        or re.fullmatch(r"correct[.!]?", lowered.strip())
    )


def _dob_leave_as_is(text: str) -> bool:
    lowered = (text or "").lower()
    return bool(
        re.search(
            r"\b(leave (it|them|that) (as is|alone)|keep (it|them|that)|"
            r"don't change|do not change|no change)\b",
            lowered,
        )
    )


def _dob_wants_continue(text: str) -> bool:
    """Caller wants to leave DOBs as-is and move on (review or final gate)."""
    lowered = (text or "").lower()
    return bool(
        _dob_leave_as_is(text)
        or re.search(r"\b(continue|next|done|that's all|finished)\b", lowered)
    )


def _dob_question_for(name: str) -> str:
    """Start Collect Mode with the month component (server-owned sequence)."""
    return _dob_month_question(name)


def _dob_component_question(name: str, component: str) -> str:
    if component == "day":
        return _dob_day_question(name)
    if component == "year":
        return _dob_year_question(name)
    if component == "fix_which":
        return _dob_fix_which_question()
    return _dob_month_question(name)


def _partial_dob_string(partial: dict[str, Any]) -> str | None:
    """Build MM/DD/YYYY from stored components when all three are present and valid."""
    from datetime import date

    from .live_form import _calendar_date_ok

    try:
        month = int(partial["month"]) if partial.get("month") is not None else None
        day = int(partial["day"]) if partial.get("day") is not None else None
        year = int(partial["year"]) if partial.get("year") is not None else None
    except (TypeError, ValueError):
        return None
    if month is None or day is None or year is None:
        return None
    if not _calendar_date_ok(month, day, year):
        return None
    try:
        if date(year, month, day) > date.today():
            return None
    except ValueError:
        return None
    return f"{month:02d}/{day:02d}/{year:04d}"


def _parse_month_component(text: str) -> tuple[int | None, str | None]:
    """Accept a month name or abbreviation only. Returns (1-12, clarify)."""
    from .live_form import _DOB_MONTHS

    raw = re.sub(r"[.,]", " ", (text or "").lower())
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return None, "Please say the month, for example October or Oct."
    month_name = "|".join(sorted(_DOB_MONTHS, key=len, reverse=True))
    match = re.search(rf"\b({month_name})\b", raw)
    if match:
        return int(_DOB_MONTHS[match.group(1)]), None
    # Standalone numeric month 1–12 (reject slash dates — ask for the name).
    if re.search(r"\d{1,2}\s*[./-]\s*\d", raw):
        return None, (
            "Please say the month name, for example October or Oct — "
            "not a full date."
        )
    num = re.fullmatch(r"(?:month\s+(?:is\s+)?)?(\d{1,2})", raw)
    if num:
        value = int(num.group(1))
        if 1 <= value <= 12:
            return value, None
    return None, "I didn't catch the month. Please say October, or Oct."


def _parse_day_component(text: str) -> tuple[int | None, str | None]:
    """Accept numeric, ordinal, or spoken day 1–31."""
    from .live_form import _dob_word_number

    prepared = _normalize_dob_asr(text or "")
    raw = re.sub(r"[.,]", " ", prepared.lower())
    raw = re.sub(r"(\d+)(?:st|nd|rd|th)\b", r"\1", raw)
    raw = re.sub(
        r"\b(?:day|date|born|was|the|a|an|is|on)\b", " ", raw, flags=re.I
    )
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return None, "Please say the day, for example 22 or twenty-two."
    if re.search(r"\b(?:19|20)\d{2}\b", raw) or re.search(
        r"\b(january|february|march|april|may|june|july|august|"
        r"september|october|november|december)\b",
        raw,
    ):
        # Strip trailing year/month noise if day leads; else re-ask day only.
        leading = re.match(r"^(\d{1,2}|[a-z][a-z\s-]*)", raw)
        if leading:
            candidate = _dob_word_number(leading.group(1).strip())
            if candidate is not None and 1 <= candidate <= 31:
                return int(candidate), None
        return None, "Please say only the day of the month, for example 22."
    value = _dob_word_number(raw)
    if value is None:
        digits = re.fullmatch(r"(\d{1,2})", raw)
        if digits:
            value = int(digits.group(1))
    if value is None or not (1 <= int(value) <= 31):
        return None, "I didn't catch the day. Please say a day from 1 to 31."
    return int(value), None


def _parse_year_component(text: str) -> tuple[int | None, str | None]:
    """Accept a four-digit year or a clearly spoken year."""
    from datetime import date

    from .live_form import _dob_spoken_year

    prepared = _expand_stage_e_spoken_year(_normalize_dob_asr(text or ""))
    raw = re.sub(r"[.,]", " ", prepared.lower())
    raw = re.sub(
        r"\b(?:year|born|was|the|a|an|is|in)\b", " ", raw, flags=re.I
    )
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return None, "Please say the four-digit year, for example 2003."
    if re.fullmatch(r"\d{2}", raw):
        return None, (
            "That year is only two digits, so the century is unclear. "
            "Please say the full four-digit year."
        )
    digits = re.search(r"\b((?:19|20)\d{2})\b", raw)
    if digits:
        year = int(digits.group(1))
    else:
        year = _dob_spoken_year(raw)
    if year is None:
        return None, "I didn't catch the year. Please say the four-digit year."
    today = date.today()
    if year < 1900 or year > today.year:
        return None, "That year doesn't look right. Please say the four-digit year."
    return int(year), None


def _parse_fix_which_component(text: str) -> str | None:
    lowered = (text or "").lower()
    if re.search(r"\bmonths?\b", lowered):
        return "month"
    if re.search(r"\bdays?\b", lowered):
        return "day"
    if re.search(r"\byears?\b", lowered):
        return "year"
    return None


def _immigration_status_options_speech() -> str:
    """List the four Pointcare citizenship / immigration status options."""
    return (
        "Please say one status: U.S. Citizen, U.S. National, "
        "Satisfactory immigration status, or Prefer not to answer."
    )


def _immigration_options_listed(session: Any | None) -> bool:
    if session is None:
        return False
    return str(session.filled.get("app_immigration_options_listed") or "") == "1"


def _mark_immigration_options_listed(session: Any | None) -> None:
    if session is None:
        return
    session.filled["app_immigration_options_listed"] = "1"


def _immigration_status_display(status: str) -> str:
    """Spoken label without the green-card parenthetical."""
    text = re.sub(r"\s*\(.*?\)\s*", " ", (status or "").strip())
    return re.sub(r"\s+", " ", text).strip(" .") or (status or "").strip()


def _immigration_status_question_for(
    name: str,
    *,
    session: Any | None = None,
    force_options: bool = False,
) -> str:
    """Ask for one member's status.

    First member (or force_options / list-options): read all four choices.
    Later members: short cue to reuse those four options.
    """
    label = _clean_member_display_name(name)
    base = f"What is {label}'s citizenship or immigration status?"
    if force_options or not _immigration_options_listed(session):
        _mark_immigration_options_listed(session)
        return (
            f"{base} You can say U.S. Citizen, U.S. National, "
            "Satisfactory immigration status, or Prefer not to answer."
        )
    return (
        f"{base} Choose from those four options, "
        "or ask me to read them again."
    )


def _immigration_confirm_all_speech(confirmed: dict[str, str]) -> str:
    """One-shot confirm of every member's chosen status before Next."""
    parts: list[str] = []
    for name, status in confirmed.items():
        label = _clean_member_display_name(name)
        parts.append(f"{label} is {_immigration_status_display(status)}")
    if not parts:
        return (
            "I have everyone's citizenship or immigration status. "
            "Does that look correct for everyone? Say Yes to continue, "
            "or tell me whose status to change."
        )
    if len(parts) == 1:
        summary = parts[0]
    elif len(parts) == 2:
        summary = f"{parts[0]}, and {parts[1]}"
    else:
        summary = ", ".join(parts[:-1]) + f", and {parts[-1]}"
    return (
        f"Confirming: {summary}. Does that look correct for everyone? "
        "Say Yes to continue, or tell me whose status to change."
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
        f"Which coverage or plan does {label} qualify for — Medicare, Medicaid, "
        "health insurance through a job, TRICARE, VA Health Care, private "
        "insurance or ACA Marketplace, or Other? You can name more than one."
    )


def _insurance_more_plans_question_for(name: str, selected: list[str]) -> str:
    label = _clean_member_display_name(name)
    listed = ", ".join(selected) if selected else "those options"
    return (
        f"I've marked {listed} for {label}. "
        "Do they have any other plans? Say another plan name, or say No to "
        f"confirm {listed} and continue."
    )


def _insurance_other_specify_question(name: str) -> str:
    label = _clean_member_display_name(name)
    return (
        f"What is the name of the other insurance or coverage for {label}? "
        "I'll type it into Please specify."
    )


def _insurance_types_for_speech(
    types: list[str], other_text: str | None = None
) -> list[str]:
    """Display labels for speech; attach Other free-text when present."""
    custom = (other_text or "").strip()
    out: list[str] = []
    for item in types or []:
        label = str(item or "").strip()
        if not label:
            continue
        if label.lower() == "other" and custom:
            out.append(f"Other ({custom})")
        else:
            out.append(label)
    return out


def _insurance_other_text(session: Any) -> str:
    return str(
        (getattr(session, "filled", None) or {}).get("app_insurance_other_text") or ""
    ).strip()


def _set_insurance_other_text(session: Any, value: str | None) -> None:
    text = (value or "").strip()
    if text:
        session.filled["app_insurance_other_text"] = text
    else:
        session.filled.pop("app_insurance_other_text", None)


def _insurance_current_types(session: Any) -> list[str]:
    raw = (getattr(session, "filled", None) or {}).get("app_insurance_current_types") or ""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(item).strip() for item in data if str(item).strip()]
    except Exception:
        pass
    return []


def _set_insurance_current_types(session: Any, types: list[str] | None) -> None:
    if not types:
        session.filled.pop("app_insurance_current_types", None)
        return
    # Preserve order while de-duplicating case-insensitively.
    out: list[str] = []
    seen: set[str] = set()
    for item in types:
        label = str(item or "").strip()
        key = label.lower()
        if not label or key in seen:
            continue
        seen.add(key)
        out.append(label)
    if not out:
        session.filled.pop("app_insurance_current_types", None)
        return
    session.filled["app_insurance_current_types"] = json.dumps(out)


def _insurance_completed_members(session: Any) -> list[str]:
    raw = (getattr(session, "filled", None) or {}).get("app_insurance_completed") or ""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(item).strip() for item in data if str(item).strip()]
    except Exception:
        pass
    return []


def _mark_insurance_member_completed(session: Any, member_name: str) -> list[str]:
    completed = _insurance_completed_members(session)
    label = (member_name or "").strip()
    if label and label.lower() not in {name.lower() for name in completed}:
        completed.append(label)
    if completed:
        session.filled["app_insurance_completed"] = json.dumps(completed)
    return completed


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
                  // Open MM/DD/YYYY means the editor is open even when a saved
                  // DOB is still shown on the card (correct/edit path).
                  editor_open: Boolean(editorOpen),
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
        if not (has_add or dob or editor_open):
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
                "editor_open": bool(editor_open),
                "index": len(cards),
            }
        )
    return cards


_IMMIGRATION_MEMBER_JS = r"""
() => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const statusKey = (s) => {
    let t = norm(s).toLowerCase()
      .replace(/\s*\(.*?\)\s*/g, ' ')
      .replace(/\s+/g, ' ')
      .replace(/^[.\s]+|[.\s]+$/g, '')
      .replace(/\bu\.?\s*s\.?/g, 'us')
      .replace(/\s+/g, ' ')
      .replace(/^[.\s]+|[.\s]+$/g, '')
      .trim();
    if (t === 'us citizen' || t === 'citizen') return 'u.s. citizen';
    if (t === 'us national' || t === 'national') return 'u.s. national';
    if (t === 'satisfactory immigration status' || t === 'green card') {
      return 'satisfactory immigration status';
    }
    if (t === 'prefer not' || t === 'prefer not to answer') return 'prefer not to answer';
    return t;
  };
  const isMemberStatus = (t) => {
    const n = statusKey(t);
    return n === 'u.s. citizen'
      || n === 'u.s. national'
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


# Locate only — never click or force-check. Status keys mirror
# ``_immigration_status_key`` so US Citizen matches U.S. Citizen.
_IMMIGRATION_LOCATE_JS = r"""
({ memberName, status }) => {
  const MARK = 'data-pc-imm-click';
  document.querySelectorAll('[' + MARK + ']').forEach((el) => el.removeAttribute(MARK));
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const nameKey = (s) => norm(s).toLowerCase().replace(/\.+$/, '').trim();
  const statusKey = (s) => {
    let t = norm(s).toLowerCase()
      .replace(/\s*\(.*?\)\s*/g, ' ')
      .replace(/\s+/g, ' ')
      .replace(/^[.\s]+|[.\s]+$/g, '')
      .replace(/\bu\.?\s*s\.?/g, 'us')
      .replace(/\s+/g, ' ')
      .replace(/^[.\s]+|[.\s]+$/g, '')
      .trim();
    if (t === 'us citizen' || t === 'citizen') return 'u.s. citizen';
    if (t === 'us national' || t === 'national') return 'u.s. national';
    if (t === 'satisfactory immigration status' || t === 'green card') {
      return 'satisfactory immigration status';
    }
    if (t === 'prefer not' || t === 'prefer not to answer') return 'prefer not to answer';
    return t;
  };
  const isMemberStatus = (t) => {
    const n = statusKey(t);
    return n === 'u.s. citizen'
      || n === 'u.s. national'
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
  const readOn = (radio) => {
    if (radio.checked === true) return true;
    if ((radio.getAttribute('aria-checked') || '').toLowerCase() === 'true') return true;
    return false;
  };
  const controlTypeOf = (radio) => {
    const tag = (radio.tagName || '').toLowerCase();
    const type = (radio.getAttribute('type') || '').toLowerCase();
    const role = (radio.getAttribute('role') || '').toLowerCase();
    if (tag === 'input' && type === 'radio') return 'native_radio';
    if (role === 'radio') return 'aria_radio';
    return 'other';
  };
  const wantName = nameKey(memberName);
  const wantStatus = statusKey(status);
  if (!wantName || !wantStatus || !isMemberStatus(wantStatus)) {
    return { ok: false, reason: 'status_option_not_found', control_type: '', selected: false };
  }
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
    if (!card || nameKey(card.name) !== wantName) continue;
    const groupKey = (card.radios[0] && (card.radios[0].getAttribute('name') || '')) || '';
    const key = nameKey(card.name) + '::' + groupKey + '::' + card.radios.length;
    if (seen.has(key)) continue;
    seen.add(key);
    hits.push(card);
  }
  if (hits.length === 0) {
    return { ok: false, reason: 'member_not_found', control_type: '', selected: false };
  }
  if (hits.length > 1) {
    return { ok: false, reason: 'multiple_member_matches', control_type: '', selected: false };
  }
  const card = hits[0];
  let matched = null;
  for (const radio of card.radios) {
    if (statusKey(labelFor(radio)) === wantStatus) {
      matched = radio;
      break;
    }
  }
  if (!matched) {
    return { ok: false, reason: 'status_option_not_found', control_type: '', selected: false };
  }
  const selected = readOn(matched);
  const controlType = controlTypeOf(matched);
  const wrap = matched.closest('label');
  const clickEl = wrap && statusKey(wrap.innerText || wrap.textContent || '') === wantStatus
    ? wrap
    : matched;
  clickEl.setAttribute(MARK, '1');
  matched.setAttribute('data-pc-imm-radio', '1');
  return {
    ok: true,
    reason: selected ? 'already_selected' : 'located',
    control_type: controlType,
    selected,
    tag: (matched.tagName || '').toLowerCase(),
    input_type: (matched.getAttribute('type') || '').toLowerCase(),
    role: (matched.getAttribute('role') || '').toLowerCase(),
    checked: matched.checked === true,
    aria_checked: (matched.getAttribute('aria-checked') || '').toLowerCase(),
    has_label: !!(wrap && clickEl === wrap),
  };
}
"""


_IMMIGRATION_CLEAR_MARK_JS = r"""
() => {
  document.querySelectorAll('[data-pc-imm-click]').forEach((el) => el.removeAttribute('data-pc-imm-click'));
  document.querySelectorAll('[data-pc-imm-radio]').forEach((el) => el.removeAttribute('data-pc-imm-radio'));
}
"""


async def _locate_member_immigration_control(
    page: Page, member_name: str, status: str
) -> dict[str, Any]:
    """Find the member's status control. Does not click or mutate checked state."""
    try:
        raw = await page.evaluate(
            _IMMIGRATION_LOCATE_JS,
            {"memberName": member_name, "status": status},
        )
    except Exception:
        logger.exception("locate_member_immigration_control failed")
        return {
            "ok": False,
            "reason": "wrong_control_state",
            "control_type": "",
            "selected": False,
        }
    if not isinstance(raw, dict):
        return {
            "ok": False,
            "reason": "wrong_control_state",
            "control_type": "",
            "selected": False,
        }
    return {
        "ok": bool(raw.get("ok")),
        "reason": str(raw.get("reason") or "wrong_control_state"),
        "control_type": str(raw.get("control_type") or ""),
        "selected": bool(raw.get("selected")),
        "tag": str(raw.get("tag") or ""),
        "input_type": str(raw.get("input_type") or ""),
        "role": str(raw.get("role") or ""),
        "checked": bool(raw.get("checked")),
        "aria_checked": str(raw.get("aria_checked") or ""),
        "has_label": bool(raw.get("has_label")),
    }


async def _clear_immigration_click_marks(page: Page) -> None:
    try:
        await page.evaluate(_IMMIGRATION_CLEAR_MARK_JS)
    except Exception:
        pass


async def _set_member_immigration_status(
    page: Page, member_name: str, status: str
) -> tuple[bool, str]:
    """Select ``status`` on that member's own radio group.

    Locate with JS, interact with Playwright, then poll for the real selected
    state. Never force ``element.checked = true``. A matching preselected radio
    is accepted without clicking. Other members are never touched.

    Reasons: ``already_selected``, ``selected``, ``member_not_found``,
    ``multiple_member_matches``, ``status_option_not_found``, ``click_failed``,
    ``selection_timeout``, ``state_not_reflected``, ``wrong_control_state``.
    """
    located = await _locate_member_immigration_control(page, member_name, status)
    control_type = str(located.get("control_type") or "") or "-"
    if not located.get("ok"):
        reason = str(located.get("reason") or "wrong_control_state")
        logger.info(
            "immigration_status_set ok=0 reason=%s control_type=%s checked=%s aria_checked=%s",
            reason,
            control_type,
            located.get("checked"),
            located.get("aria_checked") or "-",
        )
        await _clear_immigration_click_marks(page)
        return False, reason
    if located.get("selected") or located.get("reason") == "already_selected":
        logger.info(
            "immigration_status_set ok=1 reason=already_selected control_type=%s",
            control_type,
        )
        await _clear_immigration_click_marks(page)
        return True, "already_selected"

    click_target = page.locator("[data-pc-imm-click='1']").first
    try:
        count = await click_target.count()
        if count < 1:
            logger.info(
                "immigration_status_set ok=0 reason=click_failed control_type=%s detail=mark_missing",
                control_type,
            )
            await _clear_immigration_click_marks(page)
            return False, "click_failed"
        # Native radios: check() when supported; otherwise click the marked
        # label/control. Playwright events reach framework handlers; a plain
        # DOM click inside evaluate often does not.
        used_check = False
        if control_type == "native_radio":
            radio = page.locator("[data-pc-imm-radio='1']").first
            if await radio.count():
                try:
                    await radio.check(timeout=4000, force=False)
                    used_check = True
                except Exception:
                    used_check = False
        if not used_check:
            await click_target.click(timeout=4000)
    except Exception:
        logger.exception(
            "immigration_status_set click_failed control_type=%s",
            control_type,
        )
        await _clear_immigration_click_marks(page)
        return False, "click_failed"

    # Bounded wait: selection may paint after the click returns.
    for _ in range(12):
        verified, verify_reason = await _verify_member_immigration_status(
            page, member_name, status
        )
        if verified:
            logger.info(
                "immigration_status_set ok=1 reason=selected control_type=%s",
                control_type,
            )
            await _clear_immigration_click_marks(page)
            return True, "selected"
        if verify_reason in ("member_not_found", "multiple_member_matches"):
            logger.info(
                "immigration_status_set ok=0 reason=%s control_type=%s",
                verify_reason,
                control_type,
            )
            await _clear_immigration_click_marks(page)
            return False, verify_reason
        await page.wait_for_timeout(150)

    # Re-read control state for diagnostics (no healthcare payload).
    after = await _locate_member_immigration_control(page, member_name, status)
    after_selected = bool(after.get("selected"))
    reason = "state_not_reflected" if after.get("ok") else "selection_timeout"
    if after_selected:
        # Locate sees it on; scrape/verify may use a different path — treat as ok.
        logger.info(
            "immigration_status_set ok=1 reason=selected control_type=%s via=locate",
            control_type,
        )
        await _clear_immigration_click_marks(page)
        return True, "selected"
    logger.info(
        "immigration_status_set ok=0 reason=%s control_type=%s checked=%s aria_checked=%s",
        reason,
        after.get("control_type") or control_type,
        after.get("checked"),
        after.get("aria_checked") or "-",
    )
    await _clear_immigration_click_marks(page)
    return False, reason


async def _verify_member_immigration_status(
    page: Page, member_name: str, status: str
) -> tuple[bool, str]:
    """True when the named member card shows ``status`` selected.

    Returns ``(ok, reason)`` with ``verified``, ``member_not_found``,
    ``multiple_member_matches``, or ``verification_failed``.
    """
    want_name = _immigration_member_name_key(member_name)
    if not want_name or not _immigration_status_key(status):
        return False, "verification_failed"
    members = await _scrape_immigration_members(page)
    matches = [
        row
        for row in members
        if _immigration_member_name_key(str(row.get("name") or "")) == want_name
    ]
    if len(matches) == 0:
        logger.info("immigration_status_verify ok=0 reason=member_not_found")
        return False, "member_not_found"
    if len(matches) > 1:
        logger.info("immigration_status_verify ok=0 reason=multiple_member_matches")
        return False, "multiple_member_matches"
    if not _same_immigration_status(str(matches[0].get("selected") or ""), status):
        logger.info("immigration_status_verify ok=0 reason=verification_failed")
        return False, "verification_failed"
    logger.info("immigration_status_verify ok=1 reason=verified")
    return True, "verified"


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
      const nameKey = (s) => norm(s).toLowerCase().replace(/\./g, ' ').replace(/\s+/g, ' ').trim();
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
        const wrap = box.closest('label, [class*="MuiFormControlLabel"]');
        if (wrap) {
          const cloneText = norm(wrap.innerText || wrap.textContent || '');
          // Prefer the trailing name span inside MUI FormControlLabel.
          const spans = [...wrap.querySelectorAll('span, p, div')].filter((el) => {
            const t = norm(el.innerText || el.textContent || '');
            return isName(t) && t.length <= 48;
          });
          if (spans.length) {
            spans.sort((a, b) => {
              const ta = norm(a.innerText || a.textContent || '');
              const tb = norm(b.innerText || b.textContent || '');
              return ta.length - tb.length;
            });
            return norm(spans[0].innerText || spans[0].textContent || '');
          }
          if (isName(cloneText)) return cloneText;
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
      const isOn = (box) => {
        if (box.checked === true) return true;
        const aria = (box.getAttribute('aria-checked') || '').toLowerCase();
        if (aria === 'true') return true;
        const cls = String(box.className || '').toLowerCase();
        if (cls.includes('mui-checked') || /(^|\\s)checked(\\s|$)/.test(cls)) return true;
        const owner = box.closest(
          '[class*="MuiCheckbox"], [class*="MuiFormControlLabel"], label, [role=checkbox]'
        );
        if (owner) {
          if ((owner.getAttribute('aria-checked') || '').toLowerCase() === 'true') return true;
          if (owner.querySelector('.Mui-checked, [class*="Mui-checked"]')) return true;
          const ocls = String(owner.className || '').toLowerCase();
          if (ocls.includes('mui-checked')) return true;
        }
        return false;
      };
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
        const seen = new Set();
        for (const box of root.querySelectorAll('input[type=checkbox], [role=checkbox]')) {
          // Prefer the native input when a MUI wrapper also exposes role=checkbox.
          if (box.getAttribute('role') === 'checkbox' && box.querySelector('input[type=checkbox]')) {
            continue;
          }
          const name = labelFor(box);
          if (!name) continue;
          const key = nameKey(name);
          if (!key || seen.has(key)) continue;
          seen.add(key);
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


def _coverage_member_name_key(name: str) -> str:
    """Comparable key for Who-has-coverage labels (ignore case/periods)."""
    text = re.sub(r"[.\s]+", " ", (name or "").strip().lower())
    return re.sub(r"\s+", " ", text).strip()


async def _clear_coverage_click_marks(page: Page) -> None:
    """Remove temporary Playwright targeting marks from Who-has-coverage rows."""
    try:
        await page.evaluate(
            """() => {
              for (const el of document.querySelectorAll(
                '[data-pc-cov-click], [data-pc-cov-box]'
              )) {
                el.removeAttribute('data-pc-cov-click');
                el.removeAttribute('data-pc-cov-box');
              }
            }"""
        )
    except Exception:
        pass


async def _locate_coverage_member_control(
    page: Page, member_name: str, *, want_checked: bool = True
) -> dict[str, Any]:
    """Find one Who-has-coverage control and mark it for Playwright.

    Read-only on selection state — does not click and does not mutate
    ``checked``. Marks ``data-pc-cov-box`` (input/role checkbox) and
    ``data-pc-cov-click`` (label / FormControlLabel when present).
    """
    try:
        located = await page.evaluate(
            """({ memberName, wantChecked }) => {
            """
            + _coverage_member_rows_js()
            + """
              for (const el of document.querySelectorAll(
                '[data-pc-cov-click], [data-pc-cov-box]'
              )) {
                el.removeAttribute('data-pc-cov-click');
                el.removeAttribute('data-pc-cov-box');
              }
              const want = nameKey(memberName);
              if (!want) {
                return { ok: false, reason: 'missing', name: '', checked: false };
              }
              const hits = rows().filter((row) => nameKey(row.name) === want);
              if (hits.length !== 1) {
                return {
                  ok: false,
                  reason: hits.length ? 'ambiguous' : 'missing',
                  name: '',
                  checked: false,
                  hit_count: hits.length,
                };
              }
              const row = hits[0];
              const box = row.box;
              const associatedLabel = box.id
                ? document.querySelector(
                    'label[for="' + box.id.replace(/"/g, '') + '"]'
                  )
                : null;
              const wrap =
                associatedLabel ||
                box.closest('label, [class*="MuiFormControlLabel"]');
              const tagName = String(box.tagName || '').toUpperCase();
              // Native inputs: click the visible associated label/member name
              // so Pointcare's framework handler expands the plan options.
              // Custom role=checkbox: click the control itself (label click may miss).
              const clickEl = (tagName === 'INPUT' && wrap) ? wrap : box;
              try {
                clickEl.scrollIntoView({ block: 'center', inline: 'nearest' });
              } catch (e) {}
              box.setAttribute('data-pc-cov-box', '1');
              clickEl.setAttribute('data-pc-cov-click', '1');
              const aria = (box.getAttribute('aria-checked') || '').toLowerCase();
              const cls = String(
                (box.className || '') + ' ' + ((wrap && wrap.className) || '')
              ).toLowerCase();
              const already = Boolean(row.checked) === Boolean(wantChecked);
              return {
                ok: true,
                reason: already ? 'already_selected' : 'needs_click',
                name: row.name,
                checked: Boolean(row.checked),
                aria_checked: aria || '',
                tag: tagName,
                role: String(box.getAttribute('role') || ''),
                input_type: String(box.getAttribute('type') || ''),
                has_label: Boolean(tagName === 'INPUT' && wrap),
                label_kind: associatedLabel ? 'for' : (wrap ? 'ancestor' : ''),
                class_hint: cls.includes('mui-checked') ? 'mui-checked' : '',
              };
            }""",
            {"memberName": member_name, "wantChecked": want_checked},
        )
    except Exception:
        logger.exception(
            "locate_coverage_member_control failed member=%r", member_name
        )
        return {"ok": False, "reason": "locate_failed", "checked": False}
    return located if isinstance(located, dict) else {
        "ok": False,
        "reason": "locate_failed",
        "checked": False,
    }


async def _read_marked_coverage_checked(page: Page) -> dict[str, Any]:
    """Read selected state from the Playwright-marked coverage checkbox only."""
    try:
        raw = await page.evaluate(
            """() => {
            """
            + _coverage_member_rows_js()
            + """
              const box = document.querySelector('[data-pc-cov-box="1"]');
              if (!box) return { found: false, checked: false };
              return {
                found: true,
                checked: isOn(box),
                aria_checked: (box.getAttribute('aria-checked') || '').toLowerCase(),
                native_checked: box.checked === true,
                tag: String(box.tagName || ''),
              };
            }"""
        )
    except Exception:
        return {"found": False, "checked": False}
    return raw if isinstance(raw, dict) else {"found": False, "checked": False}


async def _set_member_insurance_checked(
    page: Page, member_name: str, checked: bool = True
) -> bool:
    """Select one Who-has-coverage box via Playwright and poll until verified.

    Already-selected boxes are left alone (no toggle). Does not assign
    ``input.checked`` in JS — Pointcare/MUI needs a real user-like event.
    """
    located = await _locate_coverage_member_control(
        page, member_name, want_checked=checked
    )
    reason = str(located.get("reason") or "")
    if not located.get("ok"):
        logger.info(
            "coverage_member_set ok=0 reason=%s member=%r hits=%s",
            reason or "missing",
            member_name,
            located.get("hit_count"),
        )
        await _clear_coverage_click_marks(page)
        return False
    if reason == "already_selected":
        logger.info(
            "coverage_member_set ok=1 reason=already_selected member=%r",
            member_name,
        )
        await _clear_coverage_click_marks(page)
        return True

    click_target = page.locator("[data-pc-cov-click='1']").first
    box = page.locator("[data-pc-cov-box='1']").first
    used = "none"
    has_label = bool(located.get("has_label"))
    try:
        if not await click_target.count():
            logger.info(
                "coverage_member_set ok=0 reason=click_failed member=%r detail=mark_missing",
                member_name,
            )
            await _clear_coverage_click_marks(page)
            return False
        # MUI FormControlLabel: click the label/wrapper so React/MUI state
        # updates. Forced check() on a visually-hidden input can "succeed"
        # in Playwright without flipping the real selected state.
        if has_label:
            try:
                await click_target.click(timeout=4000)
            except Exception:
                # MUI sometimes paints the label over a zero-size control.
                await click_target.click(timeout=4000, force=True)
            used = "label_click"
        else:
            tag = str(located.get("tag") or "").upper()
            if tag == "INPUT" and await box.count():
                try:
                    if checked:
                        await box.check(timeout=4000, force=True)
                    else:
                        await box.uncheck(timeout=4000, force=True)
                    used = "input_check"
                except Exception:
                    used = "none"
            if used == "none":
                try:
                    await click_target.click(timeout=4000)
                except Exception:
                    await click_target.click(timeout=4000, force=True)
                used = "role_click"
    except Exception:
        logger.exception(
            "coverage_member_set click_failed member=%r", member_name
        )
        await _clear_coverage_click_marks(page)
        return False

    ok = await _poll_coverage_member_checked(
        page, member_name, want_checked=checked, timeout_ms=4000
    )
    # If a native check looked fine to Playwright but Pointcare never
    # selected, retry once via the marked label/click target.
    if not ok and used == "input_check":
        try:
            await click_target.click(timeout=4000)
            used = "label_click_retry"
            ok = await _poll_coverage_member_checked(
                page, member_name, want_checked=checked, timeout_ms=4000
            )
        except Exception:
            logger.exception(
                "coverage_member_set label_retry_failed member=%r", member_name
            )

    if ok:
        logger.info(
            "coverage_member_set ok=1 reason=selected member=%r via=%s",
            member_name,
            used,
        )
    else:
        marked = await _read_marked_coverage_checked(page)
        scrape_on = False
        try:
            # Prefer marked node while marks still exist; scrape is fallback.
            if not marked.get("found"):
                scrape_on = await _coverage_member_is_checked(
                    page, member_name, want_checked=checked
                )
            else:
                scrape_on = bool(marked.get("checked")) is bool(checked)
        except Exception:
            scrape_on = False
        logger.info(
            "coverage_member_set ok=0 reason=selection_timeout member=%r "
            "via=%s marked_found=%s marked_checked=%s aria=%s scrape_ok=%s "
            "tag=%s role=%s has_label=%s",
            member_name,
            used,
            int(bool(marked.get("found"))),
            int(bool(marked.get("checked"))),
            marked.get("aria_checked") or "-",
            int(bool(scrape_on)),
            located.get("tag") or "-",
            located.get("role") or "-",
            int(has_label),
        )
    await _clear_coverage_click_marks(page)
    return ok


async def _verify_coverage_member_checked(page: Page, member_name: str) -> bool:
    """True only when that member's own coverage checkbox is checked.

    Re-locates the same Who-has-coverage control the setter uses so a bare
    name match never counts as selected.
    """
    located = await _locate_coverage_member_control(
        page, member_name, want_checked=True
    )
    try:
        if not located.get("ok"):
            return False
        return await _coverage_member_is_checked(
            page, member_name, want_checked=True
        )
    finally:
        await _clear_coverage_click_marks(page)


async def _coverage_member_is_checked(
    page: Page, member_name: str, *, want_checked: bool = True
) -> bool:
    """Read one member's Who-has-coverage selected state from the live DOM.

    Prefers the Playwright-marked control when present (same node the setter
    clicked), then falls back to the Who-has-coverage scrape by name key.
    """
    want = _coverage_member_name_key(member_name)
    if not want:
        return False
    marked = await _read_marked_coverage_checked(page)
    if marked.get("found"):
        if bool(marked.get("checked")) is bool(want_checked):
            return True
        # Marked node found but isOn missed — try Playwright's view of the
        # same control (native checked / aria-checked) before giving up.
        try:
            box = page.locator("[data-pc-cov-box='1']").first
            if await box.count():
                try:
                    if await box.is_checked() is bool(want_checked):
                        return True
                except Exception:
                    aria = (
                        await box.get_attribute("aria-checked") or ""
                    ).lower()
                    if aria in ("true", "false"):
                        return (aria == "true") is bool(want_checked)
        except Exception:
            pass
        return False
    rows = await _scrape_coverage_members(page)
    matches = [
        row
        for row in rows
        if _coverage_member_name_key(str(row.get("name") or "")) == want
    ]
    if len(matches) != 1:
        return False
    return bool(matches[0].get("checked")) is bool(want_checked)


async def _poll_coverage_member_checked(
    page: Page,
    member_name: str,
    *,
    want_checked: bool = True,
    timeout_ms: int = 4000,
) -> bool:
    """Poll native/ARIA/MUI selected state after a coverage checkbox click."""
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if await _coverage_member_is_checked(
            page, member_name, want_checked=want_checked
        ):
            return True
        await page.wait_for_timeout(150)
    return False


async def _set_member_coverage_type(
    page: Page, member_name: str, coverage: str
) -> bool:
    """Select one plan row inside the named member's expanded accordion."""
    located = await _locate_member_coverage_type_control(
        page, member_name, coverage
    )
    if not located.get("ok"):
        logger.info(
            "coverage_type_set ok=0 reason=%s member=%r coverage=%r hits=%s",
            located.get("reason") or "missing",
            member_name,
            coverage,
            located.get("hit_count"),
        )
        await _clear_coverage_type_marks(page)
        return False
    if located.get("checked"):
        await _clear_coverage_type_marks(page)
        return True

    target = page.locator("[data-pc-cov-type-click='1']").first
    try:
        if not await target.count():
            return False
        await target.click(timeout=4000)
    except Exception:
        logger.exception(
            "coverage_type_set click_failed member=%r coverage=%r",
            member_name,
            coverage,
        )
        await _clear_coverage_type_marks(page)
        return False
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        state = await _read_marked_coverage_type_checked(page)
        if state.get("found") and state.get("checked"):
            logger.info(
                "coverage_type_set ok=1 reason=selected member=%r coverage=%r",
                member_name,
                coverage,
            )
            await _clear_coverage_type_marks(page)
            return True
        await page.wait_for_timeout(150)
    state = await _read_marked_coverage_type_checked(page)
    logger.info(
        "coverage_type_set ok=0 reason=selection_timeout member=%r coverage=%r "
        "found=%s aria=%s data_state=%s",
        member_name,
        coverage,
        int(bool(state.get("found"))),
        state.get("aria_checked") or "-",
        state.get("data_state") or "-",
    )
    await _clear_coverage_type_marks(page)
    return False


async def _clear_coverage_type_marks(page: Page) -> None:
    try:
        await page.evaluate(
            """() => {
              for (const el of document.querySelectorAll(
                '[data-pc-cov-type-box], [data-pc-cov-type-click]'
              )) {
                el.removeAttribute('data-pc-cov-type-box');
                el.removeAttribute('data-pc-cov-type-click');
              }
            }"""
        )
    except Exception:
        pass


async def _locate_member_coverage_type_control(
    page: Page, member_name: str, coverage: str
) -> dict[str, Any]:
    """Mark a plan checkbox and its clickable row in one member accordion."""
    try:
        raw = await page.evaluate(
            r"""({ memberName, coverage }) => {
              const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
              const key = (s) => norm(s).toLowerCase()
                .replace(/\./g, ' ').replace(/\s+/g, ' ').trim();
              for (const el of document.querySelectorAll(
                '[data-pc-cov-type-box], [data-pc-cov-type-click]'
              )) {
                el.removeAttribute('data-pc-cov-type-box');
                el.removeAttribute('data-pc-cov-type-click');
              }
              const wantName = key(memberName);
              const wantCov = key(coverage);
              const triggers = [...document.querySelectorAll(
                'button[role=button][aria-expanded][aria-controls]'
              )].filter((el) => key(el.innerText || el.textContent || '') === wantName);
              if (triggers.length !== 1) {
                return {
                  ok: false,
                  reason: triggers.length ? 'ambiguous_member' : 'member_missing',
                  hit_count: triggers.length,
                };
              }
              const trigger = triggers[0];
              const contentId = trigger.getAttribute('aria-controls') || '';
              const content = contentId ? document.getElementById(contentId) : null;
              if (!content) return { ok: false, reason: 'plans_not_expanded', hit_count: 0 };
              const matches = [];
              for (const box of content.querySelectorAll(
                'button[role=checkbox], [role=checkbox], input[type=checkbox]'
              )) {
                const row = box.closest(
                  'div[tabindex="0"], label, [role=button]'
                ) || box.parentElement || box;
                const label = norm(
                  row.getAttribute('aria-label') ||
                  row.innerText ||
                  row.textContent ||
                  box.getAttribute('aria-label') ||
                  ''
                );
                const labelKey = key(label);
                if (
                  labelKey &&
                  (labelKey === wantCov ||
                   labelKey.includes(wantCov) ||
                   wantCov.includes(labelKey))
                ) {
                  matches.push({ box, row, label });
                }
              }
              if (matches.length !== 1) {
                return {
                  ok: false,
                  reason: matches.length ? 'ambiguous_plan' : 'plan_missing',
                  hit_count: matches.length,
                };
              }
              const hit = matches[0];
              hit.box.setAttribute('data-pc-cov-type-box', '1');
              hit.row.setAttribute('data-pc-cov-type-click', '1');
              try {
                hit.row.scrollIntoView({ block: 'center', inline: 'nearest' });
              } catch (e) {}
              const aria = (hit.box.getAttribute('aria-checked') || '').toLowerCase();
              const state = (hit.box.getAttribute('data-state') || '').toLowerCase();
              return {
                ok: true,
                checked:
                  hit.box.checked === true ||
                  aria === 'true' ||
                  state === 'checked',
                aria_checked: aria,
                data_state: state,
                label: hit.label,
              };
            }""",
            {"memberName": member_name, "coverage": coverage},
        )
    except Exception:
        logger.exception(
            "locate_coverage_type failed member=%r coverage=%r",
            member_name,
            coverage,
        )
        return {"ok": False, "reason": "locate_failed"}
    return raw if isinstance(raw, dict) else {
        "ok": False,
        "reason": "locate_failed",
    }


async def _read_marked_coverage_type_checked(page: Page) -> dict[str, Any]:
    try:
        raw = await page.evaluate(
            """() => {
              const box = document.querySelector('[data-pc-cov-type-box="1"]');
              if (!box) return { found: false, checked: false };
              const aria = (box.getAttribute('aria-checked') || '').toLowerCase();
              const state = (box.getAttribute('data-state') || '').toLowerCase();
              return {
                found: true,
                checked:
                  box.checked === true ||
                  aria === 'true' ||
                  state === 'checked',
                aria_checked: aria,
                data_state: state,
              };
            }"""
        )
    except Exception:
        return {"found": False, "checked": False}
    return raw if isinstance(raw, dict) else {
        "found": False,
        "checked": False,
    }


async def _verify_member_coverage_types(
    page: Page, member_name: str, expected: list[str]
) -> bool:
    """True when each expected coverage type is selected under the member."""
    if not (member_name or "").strip() or not expected:
        return False
    for coverage in expected:
        located = await _locate_member_coverage_type_control(
            page, member_name, coverage
        )
        try:
            if not located.get("ok"):
                return False
            state = await _read_marked_coverage_type_checked(page)
            if not state.get("found") or not state.get("checked"):
                return False
        finally:
            await _clear_coverage_type_marks(page)
    return True


async def _clear_coverage_other_marks(page: Page) -> None:
    try:
        await page.evaluate(
            """() => {
              for (const el of document.querySelectorAll(
                '[data-pc-cov-other-input]'
              )) {
                el.removeAttribute('data-pc-cov-other-input');
              }
            }"""
        )
    except Exception:
        pass


async def _locate_member_coverage_other_input(
    page: Page, member_name: str
) -> dict[str, Any]:
    """Mark the Please-specify input under Other for one member accordion."""
    try:
        raw = await page.evaluate(
            r"""({ memberName }) => {
              const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
              const key = (s) => norm(s).toLowerCase()
                .replace(/\./g, ' ').replace(/\s+/g, ' ').trim();
              for (const el of document.querySelectorAll(
                '[data-pc-cov-other-input]'
              )) {
                el.removeAttribute('data-pc-cov-other-input');
              }
              const wantName = key(memberName);
              const triggers = [...document.querySelectorAll(
                'button[role=button][aria-expanded][aria-controls]'
              )].filter((el) => key(el.innerText || el.textContent || '') === wantName);
              if (triggers.length !== 1) {
                return {
                  ok: false,
                  reason: triggers.length ? 'ambiguous_member' : 'member_missing',
                  hit_count: triggers.length,
                };
              }
              const trigger = triggers[0];
              const contentId = trigger.getAttribute('aria-controls') || '';
              const content = contentId ? document.getElementById(contentId) : null;
              if (!content) {
                return { ok: false, reason: 'plans_not_expanded', hit_count: 0 };
              }
              const candidates = [...content.querySelectorAll('input, textarea')]
                .filter((el) => {
                  const r = el.getBoundingClientRect();
                  if (r.width < 2 || r.height < 2) return false;
                  if (el.disabled || el.readOnly) return false;
                  const type = (el.getAttribute('type') || 'text').toLowerCase();
                  if (
                    type &&
                    type !== 'text' &&
                    type !== 'search' &&
                    el.tagName !== 'TEXTAREA'
                  ) {
                    return false;
                  }
                  const meta = key(
                    (el.getAttribute('aria-label') || '') + ' ' +
                    (el.getAttribute('placeholder') || '') + ' ' +
                    (el.getAttribute('name') || '') + ' ' +
                    (el.id || '')
                  );
                  return (
                    meta.includes('specify') ||
                    meta.includes('other') ||
                    meta.includes('describe') ||
                    meta.includes('insurance name')
                  );
                });
              if (!candidates.length) {
                // Fallback: first visible text input under Other row.
                const otherRows = [...content.querySelectorAll(
                  'div, label, section, li'
                )].filter((el) => {
                  const t = key(el.innerText || el.textContent || '');
                  return t.startsWith('other') || t.includes('please specify');
                });
                for (const row of otherRows) {
                  const inp = row.querySelector('input, textarea');
                  if (!inp) continue;
                  const r = inp.getBoundingClientRect();
                  if (r.width < 2 || r.height < 2) continue;
                  if (inp.disabled || inp.readOnly) continue;
                  candidates.push(inp);
                  break;
                }
              }
              if (candidates.length < 1) {
                return { ok: false, reason: 'specify_missing', hit_count: 0 };
              }
              const hit = candidates[0];
              hit.setAttribute('data-pc-cov-other-input', '1');
              try {
                hit.scrollIntoView({ block: 'center', inline: 'nearest' });
              } catch (e) {}
              return {
                ok: true,
                placeholder: hit.getAttribute('placeholder') || '',
                value: hit.value || '',
              };
            }""",
            {"memberName": member_name},
        )
    except Exception:
        logger.exception(
            "locate_coverage_other_input failed member=%r", member_name
        )
        return {"ok": False, "reason": "locate_failed"}
    return raw if isinstance(raw, dict) else {"ok": False, "reason": "locate_failed"}


async def _fill_member_coverage_other_specify(
    page: Page, member_name: str, value: str
) -> dict[str, Any]:
    """Type the Other insurance name into Please specify for that member."""
    cleaned = re.sub(r"\s+", " ", (value or "").strip()).strip(" .")
    if not cleaned:
        return {"ok": False, "reason": "empty"}
    located: dict[str, Any] = {}
    for _ in range(8):
        located = await _locate_member_coverage_other_input(page, member_name)
        if located.get("ok"):
            break
        await page.wait_for_timeout(150)
    if not located.get("ok"):
        await _clear_coverage_other_marks(page)
        return {
            "ok": False,
            "reason": located.get("reason") or "specify_missing",
        }
    target = page.locator("[data-pc-cov-other-input='1']").first
    try:
        if not await target.count():
            await _clear_coverage_other_marks(page)
            return {"ok": False, "reason": "mark_missing"}
        await target.click(timeout=4000)
        await target.fill("", timeout=3000)
        await target.fill(cleaned, timeout=6000)
        try:
            actual = (await target.input_value()).strip()
        except Exception:
            actual = ""
    except Exception:
        logger.exception(
            "fill_coverage_other_specify failed member=%r", member_name
        )
        await _clear_coverage_other_marks(page)
        return {"ok": False, "reason": "fill_failed"}
    await _clear_coverage_other_marks(page)
    if actual and cleaned.lower() not in actual.lower() and actual.lower() not in cleaned.lower():
        return {"ok": False, "reason": "value_mismatch", "got": actual}
    logger.info(
        "coverage_other_specify ok=1 member=%r value=%r",
        member_name,
        actual or cleaned,
    )
    return {"ok": True, "value": actual or cleaned}


_CIRCUMSTANCE_LABELS = (
    "Needs help with daily activities",
    "Veteran or military member",
    "American Indian or Alaska Native",
    "Full-time student (ages 18 – 20)",
    "None of the above",
)


def _circumstance_dom_js() -> str:
    """DOM helpers for Other Statuses — mirrors special-condition apply-to discovery.

    Finds each condition card, then the ``Who does this apply to?`` member list
    under it (heading climb + checkbox/name pairing), same pattern as Stage D
    special_person.
    """
    return r"""
      const circNorm = (s) => (s || '').replace(/\s+/g, ' ').trim();
      const circLow = (s) => circNorm(s).toLowerCase();
      const circVisible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const st = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' &&
          st.display !== 'none' && st.opacity !== '0';
      };
      const circConditionKey = (label) => {
        const n = circLow(label);
        if (!n) return '';
        if (n.includes('none of the above') || n === 'none') return 'none';
        if (n.includes('daily') || n.includes('adl') ||
            (n.includes('help') && n.includes('activ'))) return 'daily';
        if (n.includes('veteran') || n.includes('military') || n.includes('armed forces') ||
            n === 'vet') return 'veteran';
        if (n.includes('indian') || n.includes('alaska') || n.includes('native american') ||
            n.includes('indigenous') || n.includes('tribal') || n.includes('tribe')) {
          return 'indian';
        }
        if (n.includes('student') || n.includes('college') || n.includes('university')) {
          return 'student';
        }
        return '';
      };
      const circIsConditionTitle = (t) => !!circConditionKey(t);
      const circIsNav = (t) => /^(exit|back|continue|next|done|yes|no|edit|save|cancel)$/i.test(t || '');
      const circIsMultiName = (t) => {
        const hits = (t || '').match(/[A-Za-z][A-Za-z'\-]*\s+[A-Za-z]\.?/g);
        return !!(hits && hits.length > 1);
      };
      const circIsMemberName = (line) => {
        const t = circNorm(line).replace(/\s*\(you\)\s*/ig, ' ').trim();
        if (!t || t.length > 60 || t.length < 1) return false;
        if (circIsConditionTitle(t) || circIsNav(t) || circIsMultiName(t)) return false;
        if (/who does this apply|your household|select any|other status/i.test(t)) return false;
        if (/^\$?\d/.test(t)) return false;
        if (/^me$/i.test(t)) return true;
        if (!/^[A-Za-z][A-Za-z .'\-]{0,40}$/.test(t) || t.split(' ').length > 4) return false;
        return true;
      };
      const circReadChecked = (el) => {
        if (!el) return false;
        if (el.checked === true) return true;
        if (el.getAttribute && el.getAttribute('aria-checked') === 'true') return true;
        const cls = (el.className || '').toString().toLowerCase();
        if (/\b(checked|selected|active|pressed)\b/.test(cls)) return true;
        const input = el.tagName === 'INPUT' ? el :
          (el.querySelector && el.querySelector('input[type=checkbox]'));
        return !!(input && input.checked);
      };
      const circLeafPersonTexts = (root) => {
        if (!root || !root.querySelectorAll) return [];
        const out = [];
        for (const el of root.querySelectorAll(
          'div, span, p, label, h1, h2, h3, h4, [role=heading]'
        )) {
          if (!circVisible(el)) continue;
          const text = circNorm(el.innerText || el.textContent || '');
          if (!circIsMemberName(text)) continue;
          const childPerson = [...el.querySelectorAll('div, span, p, label')]
            .some((c) => {
              const ct = circNorm(c.innerText || c.textContent || '');
              return c !== el && circIsMemberName(ct) && ct.length < text.length;
            });
          if (childPerson) continue;
          out.push(text);
        }
        return out;
      };
      const circNameFromCheckbox = (box) => {
        if (!box) return '';
        let name = box.getAttribute('aria-label') || '';
        if (!name && box.id) {
          const lab = document.querySelector(`label[for="${CSS.escape(box.id)}"]`);
          if (lab) name = lab.innerText || lab.textContent || '';
        }
        if (!name) {
          const wrap = box.closest('label');
          if (wrap) name = wrap.innerText || wrap.textContent || '';
        }
        name = circNorm(name).split('\\n')[0].trim();
        if (circIsMemberName(name)) return name;
        let cur = box.parentElement;
        for (let depth = 0; depth < 8 && cur; depth++) {
          const leaves = circLeafPersonTexts(cur);
          if (leaves.length === 1) return leaves[0];
          if (leaves.length > 1) {
            const roleOnly = /^(you|partner|spouse|child|stepchild|grandchild|sibling|parent|relative|self|foster child)$/i;
            const names = leaves.filter((item) => !roleOnly.test(item));
            const roles = leaves.filter((item) => roleOnly.test(item));
            if (names.length === 1 && roles.length === 1) return names[0] + ' ' + roles[0];
            if (names.length === 1 && !circIsMultiName(names[0])) return names[0];
          }
          const own = circNorm(cur.innerText || cur.textContent || '');
          if (circIsMemberName(own)) return own;
          cur = cur.parentElement;
        }
        return '';
      };
      const circMembersIn = (root) => {
        if (!root) return [];
        const out = [];
        const seenCounts = {};
        const push = (text, box) => {
          if (!circIsMemberName(text)) return;
          const key = circLow(text);
          const dup = seenCounts[key] || 0;
          seenCounts[key] = dup + 1;
          out.push({
            label: text,
            name: text,
            checked: circReadChecked(box),
            index: out.length,
            dupIndex: dup,
          });
        };
        for (const box of root.querySelectorAll(
          'input[type=checkbox], [role=checkbox]'
        )) {
          if (!circVisible(box)) continue;
          const text = circNameFromCheckbox(box);
          if (!text || circIsConditionTitle(text)) continue;
          push(text, box);
        }
        return out;
      };
      const circFindTitleSeed = (want) => {
        const key = circConditionKey(want);
        if (!key) return null;
        const seeds = [];
        for (const el of document.querySelectorAll(
          'label, button, [role=checkbox], [role=button], li, div, span'
        )) {
          if (!circVisible(el)) continue;
          const t = circNorm(el.innerText || el.textContent || '');
          if (!t || t.length > 140) continue;
          if (circConditionKey(t) !== key) continue;
          // Prefer compact title nodes, not whole-page wrappers.
          if (/who does this apply to/i.test(t) && t.length > 80) continue;
          seeds.push({ el, len: t.length });
        }
        seeds.sort((a, b) => a.len - b.len);
        return seeds.length ? seeds[0].el : null;
      };
      const circConditionCard = (want, needMembers) => {
        const key = circConditionKey(want);
        if (!key) return null;
        const others = ['daily', 'veteran', 'indian', 'student', 'none']
          .filter((item) => item !== key);
        const seed = circFindTitleSeed(want);
        if (seed) {
          let cur = seed;
          for (let depth = 0; depth < 14 && cur; depth++) {
            const t = circLow(cur.innerText || '');
            const hasApply = /who does this apply to/i.test(t);
            const boxes = cur.querySelectorAll(
              'input[type=checkbox], [role=checkbox]'
            ).length;
            const members = circMembersIn(cur).length;
            if (t && circConditionKey(t) === key) {
              if (needMembers) {
                if (hasApply && members >= 1) return cur;
              } else if (boxes >= 1 || hasApply) {
                return cur;
              }
            }
            cur = cur.parentElement;
          }
        }
        // Fallback: smallest container with this key (+ apply-to when needed).
        const hits = [];
        for (const el of document.querySelectorAll(
          'section, article, li, fieldset, div'
        )) {
          if (!circVisible(el)) continue;
          const t = circLow(el.innerText || '');
          if (!t || t.length > 3500) continue;
          if (circConditionKey(t) !== key && !t.includes(key)) continue;
          // Soft key check — title phrase must appear.
          if (key === 'daily' && !/daily|needs help|adl/.test(t)) continue;
          if (key === 'veteran' && !/veteran|military/.test(t)) continue;
          if (key === 'indian' && !/indian|alaska|native|indigenous|tribal|tribe/.test(t)) continue;
          if (key === 'student' && !/student|college|university/.test(t)) continue;
          if (key === 'none' && !/none of the above/.test(t)) continue;
          const members = circMembersIn(el).length;
          const hasApply = /who does this apply to/i.test(t);
          if (needMembers && (members < 1 || !hasApply)) continue;
          if (!needMembers && !el.querySelector('input[type=checkbox], [role=checkbox]')) {
            continue;
          }
          const otherCount = others.filter((item) => {
            if (item === 'daily') return /needs help with daily|daily activit/.test(t) && key !== 'daily';
            if (item === 'veteran') return /veteran or military|\\bveteran\\b/.test(t) && key !== 'veteran';
            if (item === 'indian') return /american indian|alaska native/.test(t) && key !== 'indian';
            if (item === 'student') return /full-time student|full time student/.test(t) && key !== 'student';
            if (item === 'none') return /none of the above/.test(t) && key !== 'none';
            return false;
          }).length;
          const r = el.getBoundingClientRect();
          hits.push({
            el,
            len: t.length,
            otherCount,
            area: Math.max(1, r.width * r.height),
            members,
          });
        }
        hits.sort((a, b) => a.otherCount - b.otherCount || a.len - b.len || a.area - b.area);
        return hits.length ? hits[0].el : null;
      };
      const circApplyToRoot = (want) => {
        // Prefer condition-scoped card; fall back to any open apply-to heading.
        const card = circConditionCard(want, true) || circConditionCard(want, false);
        if (card && circMembersIn(card).length) return card;
        const headings = [...document.querySelectorAll(
          'h1,h2,h3,h4,div,span,p,legend,label,strong,b,[role=heading]'
        )];
        const headingHits = [];
        for (const el of headings) {
          if (!circVisible(el)) continue;
          const t = circNorm(el.innerText || el.textContent || '');
          if (!t || t.length > 80) continue;
          if (!/who does this apply to/i.test(t)) continue;
          headingHits.push({ el, len: t.length });
        }
        headingHits.sort((a, b) => a.len - b.len);
        for (const hit of headingHits) {
          let cur = hit.el.parentElement;
          for (let i = 0; i < 10 && cur; i++) {
            const members = circMembersIn(cur);
            if (members.length >= 1) {
              // Prefer a root that also mentions this condition when possible.
              const key = circConditionKey(want);
              const text = circLow(cur.innerText || '');
              if (!key || circConditionKey(text) === key || text.includes(key) ||
                  (key === 'veteran' && /veteran|military/.test(text)) ||
                  (key === 'indian' && /indian|alaska|native/.test(text)) ||
                  (key === 'daily' && /daily|needs help/.test(text)) ||
                  (key === 'student' && /student/.test(text))) {
                return cur;
              }
            }
            cur = cur.parentElement;
          }
        }
        // Last resort: first apply-to block with members.
        for (const hit of headingHits) {
          let cur = hit.el.parentElement;
          for (let i = 0; i < 10 && cur; i++) {
            if (circMembersIn(cur).length >= 1) return cur;
            cur = cur.parentElement;
          }
        }
        return card;
      };
    """

async def _select_circumstance_checkbox(page: Page, label: str) -> dict[str, Any]:
    """Check one Other Statuses condition and leave its apply-to list open."""
    try:
        result = await page.evaluate(
            "(label) => {"
            + _circumstance_dom_js()
            + """
              const want = circLow(label);
              const key = circConditionKey(label);
              if (!want || !key) return { ok: false, reason: 'empty' };
              const seed = circFindTitleSeed(label);
              const card = circConditionCard(label, false) ||
                (seed && seed.closest('section, article, li, fieldset, div')) ||
                seed;
              if (!card && !seed) return { ok: false, reason: 'card_not_found' };
              const root = card || seed;
              const titleMatch = (text) => {
                const n = circLow(text);
                if (!n || circIsMemberName(n)) return false;
                return circConditionKey(n) === key;
              };
              const controls = [...root.querySelectorAll(
                'input[type=checkbox], [role=checkbox]'
              )].filter(circVisible);
              let ctrl = null;
              let lab = null;
              for (const candidate of controls) {
                let text = circNorm(
                  candidate.getAttribute('aria-label') ||
                  candidate.innerText ||
                  candidate.textContent ||
                  ''
                );
                const linked = candidate.id
                  ? document.querySelector(`label[for="${CSS.escape(candidate.id)}"]`)
                  : candidate.closest('label');
                if (linked) {
                  text = circNorm(linked.innerText || linked.textContent || text);
                }
                text = text.split('\\n')[0].trim();
                if (!titleMatch(text)) continue;
                ctrl = candidate;
                lab = linked;
                break;
              }
              if (!ctrl) {
                const titleNodes = [...root.querySelectorAll(
                  'label, [role=checkbox], button, [role=button], div, span'
                )].filter(circVisible);
                for (const el of titleNodes) {
                  const text = circNorm(
                    el.innerText || el.textContent ||
                    el.getAttribute('aria-label') || ''
                  );
                  if (!text || text.length > 140) continue;
                  if (!titleMatch(text)) continue;
                  ctrl = el.querySelector(
                    'input[type=checkbox], [role=checkbox]'
                  ) || el;
                  lab = el.closest('label') || (el.tagName === 'LABEL' ? el : null);
                  break;
                }
              }
              if (!ctrl && seed) {
                ctrl = seed.querySelector(
                  'input[type=checkbox], [role=checkbox]'
                ) || seed;
                lab = seed.closest('label') || (seed.tagName === 'LABEL' ? seed : null);
              }
              if (!ctrl) return { ok: false, reason: 'checkbox_not_found' };
              const already = circReadChecked(ctrl);
              if (!already) {
                if (lab) lab.click();
                else ctrl.click();
              }
              return {
                ok: true,
                checked: circReadChecked(ctrl) || already,
                members: circMembersIn(root).length,
                clicked: !already,
              };
            }""",
            label,
        )
        if not isinstance(result, dict) or not result.get("ok"):
            return result if isinstance(result, dict) else {"ok": False}
        is_none = "none of the above" in (label or "").lower()
        if not is_none:
            members = await _wait_for_circumstance_members(page, label)
            if members:
                result["members"] = len(members)
                result["checked"] = True
            else:
                await page.wait_for_timeout(150)
        else:
            await page.wait_for_timeout(200)
        return result
    except Exception:
        logger.exception("select_circumstance_checkbox failed label=%r", label)
        return {"ok": False, "reason": "exception"}


async def _discover_circumstance_members(
    page: Page, circumstance: str
) -> list[dict[str, Any]]:
    """List household members under one expanded Other Statuses card.

    Uses the same apply-to heading + checkbox/name pairing approach as
    Stage D special_person discovery.
    """
    try:
        rows = await page.evaluate(
            "(circumstance) => {"
            + _circumstance_dom_js()
            + """
              const root = circApplyToRoot(circumstance);
              if (!root) return [];
              return circMembersIn(root);
            }""",
            circumstance,
        )
        return rows if isinstance(rows, list) else []
    except Exception:
        logger.exception(
            "discover_circumstance_members failed circumstance=%r", circumstance
        )
        return []


async def _wait_for_circumstance_members(
    page: Page,
    circumstance: str,
    *,
    timeout_ms: int = 3500,
) -> list[dict[str, Any]]:
    """Poll until the Other Statuses apply-to member list is readable."""
    deadline = time.monotonic() + max(0.35, timeout_ms / 1000.0)
    last: list[dict[str, Any]] = []
    while True:
        last = await _discover_circumstance_members(page, circumstance)
        if last:
            return last
        if time.monotonic() >= deadline:
            break
        try:
            await page.wait_for_timeout(120)
        except Exception:
            break
    return last


async def _set_circumstance_member(
    page: Page, member_name: str, circumstance: str, *, dup_index: int = 0
) -> bool:
    """Select ``member_name`` only inside the named circumstance apply-to list."""
    try:
        result = await page.evaluate(
            "({ memberName, circumstance, dupIndex }) => {"
            + _circumstance_dom_js()
            + """
              const wantMember = circLow(memberName);
              if (!wantMember) return { ok: false, reason: 'empty' };
              const root = circApplyToRoot(circumstance);
              if (!root) return { ok: false, reason: 'root_missing' };
              const members = circMembersIn(root);
              let target = members.find((row) =>
                circLow(row.label) === wantMember && row.dupIndex === dupIndex
              );
              if (!target) {
                target = members.find((row) =>
                  circLow(row.label) === wantMember && !row.checked
                );
              }
              if (!target) {
                target = members.find((row) => circLow(row.label) === wantMember);
              }
              if (!target) {
                // Soft match: first-name / prefix (John → John D.).
                target = members.find((row) => {
                  const got = circLow(row.label);
                  return got === wantMember || got.startsWith(wantMember + ' ') ||
                    wantMember.startsWith(got.split(' ')[0]);
                });
              }
              if (!target) return { ok: false, reason: 'member_not_found' };
              const controls = [...root.querySelectorAll(
                'input[type=checkbox], [role=checkbox]'
              )].filter(circVisible);
              let matchOrdinal = 0;
              for (const ctrl of controls) {
                const text = circNameFromCheckbox(ctrl);
                if (!text || circIsConditionTitle(text)) continue;
                if (circLow(text) !== circLow(target.label)) continue;
                if (matchOrdinal !== (target.dupIndex || 0)) {
                  matchOrdinal += 1;
                  continue;
                }
                if (circReadChecked(ctrl)) {
                  return { ok: true, already: true, label: text };
                }
                const lab = ctrl.id
                  ? document.querySelector(`label[for="${CSS.escape(ctrl.id)}"]`)
                  : ctrl.closest('label');
                // Click the checkbox control itself — name text alone often
                // does not toggle Pointcare role=checkbox buttons.
                ctrl.click();
                if (lab && !circReadChecked(ctrl)) lab.click();
                return {
                  ok: true,
                  already: false,
                  label: text,
                  checked: circReadChecked(ctrl),
                };
              }
              return { ok: false, reason: 'control_not_found' };
            }""",
            {
                "memberName": member_name,
                "circumstance": circumstance or "",
                "dupIndex": int(dup_index or 0),
            },
        )
        if not isinstance(result, dict) or not result.get("ok"):
            return False
        if result.get("already"):
            return True
        # Allow SPA to commit aria-checked (same as special_person).
        await page.wait_for_timeout(350)
        verify = await _discover_circumstance_members(page, circumstance)
        want = (member_name or "").strip().lower().rstrip(".")
        for row in verify:
            label = str(row.get("label") or "").strip().lower().rstrip(".")
            if label == want or label.startswith(want + " ") or want.startswith(
                label.split(" ")[0]
            ):
                if row.get("checked"):
                    return True
        # Click reported ok even if verify lagged — accept when evaluate said checked.
        return bool(result.get("checked") or result.get("ok"))
    except Exception:
        logger.exception(
            "set_circumstance_member failed member=%r circumstance=%r",
            member_name,
            circumstance,
        )
        return False


def _member_address_card_js() -> str:
    """JS helpers for one household member's address card, including pencil/X."""
    return """
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
      const low = (s) => norm(s).toLowerCase().replace(/\\./g, '');
      const stripYou = (s) => low(s).replace(/\\s*\\(you\\)\\s*/g, ' ').trim();
      const isNameLine = (line) => {
        // "Agent T. (You)" is a real member label on Pointcare address cards.
        const t = norm(line).replace(/\\s*\\(you\\)\\s*/ig, ' ').trim();
        if (!/^[A-Za-z][A-Za-z .'-]{0,40}$/.test(t) || t.split(' ').length > 4) return false;
        const n = low(t);
        // Never treat footer/nav chrome as a household member ("Back" was spoken as the name).
        if (/^(back|exit|previous|next|continue|cancel|save|edit|add|done|close)$/i.test(t)) {
          return false;
        }
        if (/address|apartment|mailing|home|same|unit|building|pencil|edit|save|cancel|add|next|continue|back|exit|street|po box|step \\d/.test(n)) {
          return false;
        }
        return true;
      };
      const nameMatches = (got, want) => {
        const a = stripYou(got);
        const b = stripYou(want);
        if (!a || !b) return false;
        if (a === b) return true;
        // ASR often says "john" while the card shows "john D".
        if (a.startsWith(b + ' ') || b.startsWith(a + ' ')) return true;
        return false;
      };
      const visible = (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 2 && r.height > 2 && !el.disabled;
      };
      const cardActionButtons = (root) => [...root.querySelectorAll(
        'button, [role=button], a'
      )].filter((btn) => {
        const r = btn.getBoundingClientRect();
        return r.width >= 8 && r.height >= 8 && !btn.disabled;
      });
      const isPencilButton = (btn) => {
        const label = low(
          btn.innerText || btn.textContent || btn.getAttribute('aria-label') ||
          btn.getAttribute('title') || ''
        );
        const br = btn.getBoundingClientRect();
        const svg = btn.querySelector('svg');
        const paths = svg
          ? [...svg.querySelectorAll('path')].map((p) => p.getAttribute('d') || '').join(' ')
          : '';
        if (label === 'edit' || label.startsWith('edit ') || label === 'pencil' ||
            label.includes('edit address')) return true;
        if (!svg) return false;
        if (paths.includes('M21.174 6.812') || paths.includes('3.986-3.987') ||
            paths.includes('M3 17.25') || paths.includes('m14.304 4.4')) return true;
        // Lucide X clear must never be treated as the pencil.
        if (paths.includes('M18 6 6 18') || paths.includes('m6 6 12 12')) return false;
        // Icon-only square control next to the member name (pencil is ~36x36).
        return !label && br.width >= 30 && br.width <= 52 &&
          br.height >= 30 && br.height <= 52;
      };
      const isAddButton = (btn) => {
        const label = low(
          btn.innerText || btn.textContent || btn.getAttribute('aria-label') || ''
        );
        return label === 'add' || label.startsWith('add ') || label.includes('add address');
      };
      const hasAddressControls = (root) => {
        // Cancel/Save alone live in a thin header above the inputs — do not stop
        // card growth there or the address-bar X and location fields are excluded.
        const buttons = cardActionButtons(root);
        if (buttons.some((btn) => isPencilButton(btn) || isAddButton(btn))) return true;
        const inputs = [...root.querySelectorAll('input, textarea')].filter((el) => {
          const t = (el.type || 'text').toLowerCase();
          return visible(el) && t !== 'checkbox' && t !== 'radio' && t !== 'hidden' &&
            t !== 'button' && t !== 'submit';
        });
        return inputs.length > 0;
      };
      const memberNamesIn = (root) => (root.innerText || '')
        .split('\\n').map(norm).filter(Boolean).filter(isNameLine).map(stripYou);
      const findAddressCard = (memberName) => {
        const want = stripYou(memberName);
        if (!want) return null;
        const nodes = [...document.querySelectorAll(
          'article, section, li, [class*="card"], [class*="Card"], [class*="member"], div'
        )];
        let seed = null;
        let seedArea = Infinity;
        for (const el of nodes) {
          const r = el.getBoundingClientRect();
          if (r.width < 40 || r.height < 24 || r.height > 1200) continue;
          const names = memberNamesIn(el);
          if (names.length !== 1 || !nameMatches(names[0], want)) continue;
          const area = r.width * r.height;
          if (area < seedArea) {
            seed = el;
            seedArea = area;
          }
        }
        if (!seed) return null;
        // Grow from the name seed until the pencil/Add/inputs are inside.
        // Stop before swallowing another household member.
        let best = seed;
        let cur = seed;
        for (let depth = 0; cur && depth < 10; depth += 1, cur = cur.parentElement) {
          const names = memberNamesIn(cur);
          if (names.length > 1) break;
          if (names.length === 1 && !nameMatches(names[0], want)) break;
          const r = cur.getBoundingClientRect();
          if (r.height > 1400 || r.width < 40) break;
          best = cur;
          if (hasAddressControls(cur)) return cur;
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
          } else if (inputs.length === 1) {
            // Only one free-text field left (common after No Home Address).
            key = label.includes('mailing') || label.includes('po box') ? 'mailing' : 'home';
          }
          if (key && !fields[key]) fields[key] = input;
        });
        // After No Home Address, Pointcare hides home inputs — the remaining
        // location field is mailing even when label mapping is weak.
        if (!fields.mailing && fields.home && boxOn(root, 'no home address')) {
          fields.mailing = fields.home;
          fields.home = null;
          if (!fields.mailingUnit && fields.homeUnit) {
            fields.mailingUnit = fields.homeUnit;
            fields.homeUnit = null;
          }
        }
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
                const looksSavedAddress = (value) => {
                  const t = norm(value);
                  if (!t || /^same\\b/i.test(t)) return false;
                  // Progress chrome ("Step 5 of 12") is not an address.
                  if (/^step\\s+\\d+/i.test(t)) {
                    return false;
                  }
                  if (/^(back|next|continue|cancel|save|edit)$/i.test(t)) return false;
                  if (/\\d/.test(t) && !/^step\\b/i.test(t)) return true;
                  // Numberless streets and landmarks (Bridge / Parkway / Square).
                  return /\\b(street|st|avenue|ave|road|rd|drive|dr|lane|ln|boulevard|blvd|court|ct|way|place|pl|highway|hwy|bridge|parkway|pkwy|square|plaza|circle|cir|terrace|ter|trail|loop|memorial)\\b/i.test(t);
                };
                // Collapsed cards may use icons (no "home"/"mailing" words) and
                // landmark addresses may omit a ZIP — still keep the member.
                const hasAddrCue = (
                  /address|mailing|home/.test(low(text)) ||
                  /\\d{5}/.test(text) ||
                  lines.some((line) => looksSavedAddress(line))
                );
                if (!hasAddrCue) continue;
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
                  if (/^(edit|add|save|cancel|pencil|next|continue|back|exit)$/.test(n)) continue;
                  if (/^step\\s+\\d+/i.test(line)) continue;
                  if (section === 'home' && !home) home = line;
                  else if (section === 'mailing' && !mailing) mailing = line;
                  else if (section === 'home_unit' && !homeUnit) homeUnit = line;
                  else if (section === 'mailing_unit' && !mailingUnit) mailingUnit = line;
                }
                // Collapsed Pointcare cards often omit section labels and list
                // home then mailing as two plain address lines under the name.
                const hadSections = lines.some((line) => {
                  const n = low(line);
                  return n === 'home address' || n === 'mailing address' ||
                    n.startsWith('home address ') || n.startsWith('mailing address ');
                });
                if (!expanded && !hadSections) {
                  const addrLines = [];
                  for (const line of lines) {
                    const n = low(line);
                    if (n === key) continue;
                    if (/^(edit|add|save|cancel|pencil|next|continue)$/.test(n)) continue;
                    if (/^no (home|mailing) address$/.test(n)) continue;
                    if (/apartment|unit, building|po box/.test(n) && n.length < 40) continue;
                    if (looksSavedAddress(line) || /^same\\b/i.test(line)) {
                      addrLines.push(line);
                    }
                  }
                  if (!home && addrLines[0]) {
                    home = addrLines[0];
                    if (/^same home\\b/i.test(home)) homeSame = true;
                  }
                  if (!mailing && addrLines[1]) {
                    mailing = addrLines[1];
                    if (/^same mailing\\b/i.test(mailing)) mailingSame = true;
                  }
                }
                if (!home && !mailing) {
                  const street = lines.find((line) => /\\d/.test(line) && !/^same\\b/i.test(line) && low(line) !== key);
                  if (street && !expanded) home = street;
                }
                const noHome = boxOn(el, 'no home address') || (!expanded && lines.some((line) => low(line) === 'no home address'));
                const noMail = boxOn(el, 'no mailing address') || (!expanded && lines.some((line) => low(line) === 'no mailing address'));
                const homeOk = Boolean(noHome || homeSame || looksSavedAddress(home));
                const mailOk = Boolean(
                  noMail || mailingSame || looksSavedAddress(mailing) ||
                  // Two identical saved lines mean home+mailing were both set.
                  (!expanded && home && mailing && low(home) === low(mailing) && looksSavedAddress(home))
                );
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
                  top: r.top,
                  index: out.length,
                });
              }
              // Screen order: top card first (Admin above John).
              out.sort((a, b) => (a.top - b.top) || (a.index - b.index));
              out.forEach((row, i) => { row.index = i; delete row.top; });
              return out;
            }"""
        )
        if isinstance(rows, list):
            blocked = {
                "back",
                "next",
                "continue",
                "cancel",
                "save",
                "edit",
                "add",
                "exit",
                "previous",
                "done",
                "close",
            }
            out: list[dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name") or "").strip()
                if not name:
                    continue
                name_key = re.sub(r"[^a-z0-9]+", "", name.lower())
                if name.lower().strip(" .") in blocked or name_key in blocked:
                    logger.info("scrape_address_cards skip_nav_name name=%r", name)
                    continue
                home = str(row.get("home") or "")
                mailing = str(row.get("mailing") or "")
                if re.match(r"^step\s+\d+", home, flags=re.I):
                    row = {**row, "home": ""}
                if re.match(r"^step\s+\d+", mailing, flags=re.I):
                    row = {**row, "mailing": ""}
                out.append(row)
            return out
    except Exception:
        logger.exception("scrape_address_cards failed")
    return []


def _clean_spoken_street(text: str) -> str:
    """Strip ASR fillers so confirmation matches the real street tokens."""
    raw = re.sub(r"\s+", " ", (text or "").strip())
    filler = re.compile(
        r"^(?:one second|hold on|wait(?: a)?(?: second)?|okay|ok|um+|uh+|"
        r"please|it'?s|that(?:'s| is)|the address is|address is|"
        r"new address is|use)\b[\s,.:-]*",
        re.I,
    )
    for _ in range(4):
        nxt = filler.sub("", raw)
        if nxt == raw:
            break
        raw = nxt
    return re.sub(r"\s+", " ", raw).strip(" .,")


def _split_street_and_unit(text: str) -> tuple[str, str]:
    """Separate a spoken unit or apartment from the street. The unit is never the street."""
    raw = _clean_spoken_street(text)
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
            r"\b(street|st|avenue|ave|road|rd|blvd|boulevard|drive|dr|lane|ln|"
            r"court|ct|way|bridge|highway|hwy|parkway|pkwy|circle|cir|place|"
            r"pl|terrace|ter)\b",
            raw,
            re.I,
        )
    )


_ADDRESS_STREET_TYPES = {
    "street": {"street", "st"},
    "st": {"street", "st"},
    "avenue": {"avenue", "ave"},
    "ave": {"avenue", "ave"},
    "road": {"road", "rd"},
    "rd": {"road", "rd"},
    "drive": {"drive", "dr"},
    "dr": {"drive", "dr"},
    "lane": {"lane", "ln"},
    "ln": {"lane", "ln"},
    "boulevard": {"boulevard", "blvd"},
    "blvd": {"boulevard", "blvd"},
    "court": {"court", "ct"},
    "ct": {"court", "ct"},
    "way": {"way"},
    "place": {"place", "pl"},
    "pl": {"place", "pl"},
}

_ADDRESS_DIRECTIONALS = {
    "north": "north",
    "south": "south",
    "east": "east",
    "west": "west",
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
    "northern": "north",
    "southern": "south",
    "eastern": "east",
    "western": "west",
}


def _edit_distance_leq(a: str, b: str, limit: int = 1) -> bool:
    """True when Levenshtein distance between a and b is at most ``limit``."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > limit:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr.append(min(curr[-1] + 1, prev[j] + 1, prev[j - 1] + cost))
        if min(curr) > limit:
            return False
        prev = curr
    return prev[-1] <= limit


def _address_token_close(spoken: str, candidate: str) -> bool:
    """Exact token match, fuzzy ASR slip, or concatenated pac-item fragment."""
    if spoken == candidate:
        return True
    if len(spoken) >= 5 and len(candidate) >= 5 and _edit_distance_leq(spoken, candidate, 1):
        return True
    # Google often returns "StreetSeattle" as one token before normalization.
    if len(spoken) >= 4 and (
        spoken in candidate or candidate in spoken
    ):
        return True
    return False


def _normalize_address_suggestion_text(text: str) -> str:
    """Fix pac-item concatenation such as ``StreetSeattle`` → ``Street Seattle``."""
    raw = re.sub(r"\s+", " ", (text or "").strip())
    return re.sub(r"([a-z])([A-Z])", r"\1 \2", raw)


def _address_field_prompt(field: str, member: str) -> str:
    label = _clean_member_display_name(member)
    if field == "both":
        return f"What is the new home and mailing address for {label}?"
    if field in ("home", "mailing"):
        return f"What is the new {field} address for {label}?"
    return (
        f"Should I change the home address, the mailing address, or both "
        f"for {label}?"
    )


def _speak_visible_address_suggestions(suggestions: list[str]) -> str:
    """Read dropdown rows so the caller can pick with any unique detail."""
    labels: list[str] = []
    for raw in suggestions[:5]:
        label = _normalize_address_suggestion_text(str(raw or "")).strip(" ,.")
        if label and label not in labels:
            labels.append(label)
    if not labels:
        return (
            "I don't see any address suggestions yet. "
            "Please say the street again with the city."
        )
    if len(labels) == 1:
        return (
            f"I only see {labels[0]}. "
            "Say yes to use that, or say the street again."
        )
    listed = "; ".join(labels)
    return (
        f"I see these addresses: {listed}. "
        "Say any unique part of the one you want, like the street name or West or East."
    )


def _suggestion_matches(street: str, suggestions: list[str]) -> list[str]:
    """Return the best matching dropdown row(s). Never choose by list position.

    One unique identifier is enough (street name, number, West/East, city). Exact
    full-address speech is not required. Handles Google pac-item text that
    concatenates the street type onto the city (``Newton StreetSeattle, WA``).
    """
    number_match = re.search(r"\b\d+\b", street or "")
    number = number_match.group(0) if number_match else ""
    raw_tokens = [word.lower() for word in re.findall(r"[A-Za-z]{2,}", street or "")]
    spoken_type = ""
    spoken_dirs: set[str] = set()
    content: list[str] = []
    for token in raw_tokens:
        if token in _ADDRESS_STREET_TYPES and not spoken_type:
            spoken_type = token
            continue
        if token in _ADDRESS_DIRECTIONALS:
            spoken_dirs.add(_ADDRESS_DIRECTIONALS[token])
            continue
        if token in {"the", "and", "usa", "us"}:
            continue
        content.append(token)
    content = content[:5]
    # A lone unique cue such as "West" / "98109" / "Newton" can decide the row.
    identifiers = list(content)
    if spoken_dirs:
        identifiers.extend(sorted(spoken_dirs))
    if number:
        identifiers.append(number)
    scored: list[tuple[int, str]] = []
    for item in suggestions:
        normalized = _normalize_address_suggestion_text(item)
        lowered = normalized.lower()
        item_tokens = [word.lower() for word in re.findall(r"[A-Za-z]{2,}", normalized)]
        if number and not re.search(rf"\b{re.escape(number)}\b", lowered):
            continue
        if spoken_type:
            aliases = _ADDRESS_STREET_TYPES.get(spoken_type, {spoken_type})
            if not any(token in aliases for token in item_tokens):
                continue
        matched = 0
        for word in content:
            if any(_address_token_close(word, token) for token in item_tokens):
                matched += 1
        if number and re.search(rf"\b{re.escape(number)}\b", lowered):
            matched += 1
        dir_hits = 0
        item_dirs = {
            _ADDRESS_DIRECTIONALS[token]
            for token in item_tokens
            if token in _ADDRESS_DIRECTIONALS
        }
        for direction in spoken_dirs & item_dirs:
            dir_hits += 1
            matched += 1
        # Need at least one identifying hit when the caller gave any cue.
        if identifiers and matched == 0:
            continue
        score = matched * 3
        if spoken_type and any(
            token in _ADDRESS_STREET_TYPES.get(spoken_type, {spoken_type})
            for token in item_tokens
        ):
            score += 4
        if number and re.search(rf"\b{re.escape(number)}\b", lowered):
            score += 5
        for direction in item_dirs - spoken_dirs:
            score -= 2
        for direction in spoken_dirs & item_dirs:
            score += 3
        scored.append((score, item))
    def _unique_cue_hits(cue: str) -> list[str]:
        cue_l = (cue or "").lower()
        if len(cue_l) < 3:
            return []
        hits: list[str] = []
        for item in suggestions:
            tokens = re.findall(
                r"[A-Za-z0-9]{2,}",
                _normalize_address_suggestion_text(item).lower(),
            )
            if any(
                _address_token_close(cue_l, token)
                or cue_l == token
                or cue_l in token
                or token.startswith(cue_l)
                for token in tokens
            ):
                hits.append(item)
        return hits

    if not scored:
        # Unique-identifier fallback: any spoken token that appears in exactly one row.
        for cue in identifiers:
            cue_hits = _unique_cue_hits(cue)
            if len(cue_hits) == 1:
                return cue_hits
        return []
    best = max(score for score, _item in scored)
    next_best = max((score for score, _item in scored if score < best), default=None)
    if next_best is not None and best >= next_best + 2:
        for score, item in scored:
            if score == best:
                return [item]
    tied = [item for score, item in scored if score == best]
    if len(tied) == 1:
        return tied
    # Break ties with a unique spoken cue (e.g. Bridge among several George rows).
    for cue in identifiers:
        cue_hits = [item for item in tied if item in _unique_cue_hits(cue)]
        if len(cue_hits) == 1:
            return cue_hits
    return tied


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


def _resolved_same_address(
    cards: list[dict[str, Any]], card: dict[str, Any], kind: str
) -> str:
    """Resolve 'Same home/mailing address' to another member's saved street when possible."""
    want_home = kind == "home"
    for other in cards:
        if other is card:
            continue
        if str(other.get("name") or "").strip().lower() == str(
            card.get("name") or ""
        ).strip().lower():
            continue
        if want_home:
            if other.get("no_home") or other.get("home_same"):
                continue
            value = str(other.get("home") or "").strip()
        else:
            if other.get("no_mailing") or other.get("mailing_same"):
                continue
            value = str(other.get("mailing") or "").strip()
        if value and re.search(r"\d", value) and "same " not in value.lower():
            return value
    return ""


def _address_home_mailing_match(card: dict[str, Any]) -> bool:
    """True when this member's mailing is the same as their home (not missing)."""
    if card.get("no_home") or card.get("no_mailing") or card.get("needs_home") or card.get(
        "needs_mailing"
    ):
        return False
    if card.get("mailing_same"):
        return True
    home = re.sub(r"\s+", " ", str(card.get("home") or "").strip().lower())
    mailing = re.sub(r"\s+", " ", str(card.get("mailing") or "").strip().lower())
    if not home or not mailing or "same " in home or "same " in mailing:
        return False
    home_unit = re.sub(r"\s+", " ", str(card.get("home_unit") or "").strip().lower())
    mail_unit = re.sub(r"\s+", " ", str(card.get("mailing_unit") or "").strip().lower())
    return home == mailing and home_unit == mail_unit


def _address_spoken_line(
    street: str,
    unit: str = "",
) -> str:
    line = re.sub(r"\s+", " ", (street or "").strip())
    unit_bit = re.sub(r"\s+", " ", (unit or "").strip())
    if unit_bit:
        return f"{line}, {unit_bit}" if line else unit_bit
    return line


def _address_phase(session: Any) -> str:
    return str(session.filled.get("app_address_phase") or "").strip().lower()


def _set_address_phase(session: Any, phase: str) -> None:
    text = (phase or "").strip().lower()
    if text:
        session.filled["app_address_phase"] = text
    else:
        session.filled.pop("app_address_phase", None)


def _clear_address_pending(session: Any) -> None:
    session.filled.pop("app_address_pending_kind", None)
    session.filled.pop("app_address_pending_street", None)
    session.filled.pop("app_address_pending_chosen", None)
    session.filled.pop("app_address_pending_unit", None)


def _address_home_check_speech(card: dict[str, Any]) -> str:
    """Ask only whether the saved home address is correct."""
    name = _clean_member_display_name(str(card.get("name") or "this person"))
    if card.get("no_home"):
        return (
            f"For {name}, No Home Address is currently selected. "
            "Is that still correct? Say Yes, or say No to change it."
        )
    home = _address_spoken_line(
        str(card.get("home") or "").strip(),
        str(card.get("home_unit") or "").strip(),
    )
    if home and "same " not in home.lower():
        return f"For {name}, is the home address {home} correct? Say Yes or No."
    return (
        f"For {name}, a home address is still needed. "
        "Say Yes to enter one, or say No Home Address."
    )


def _address_mailing_check_speech(card: dict[str, Any]) -> str:
    """Ask only whether the saved mailing address is correct."""
    name = _clean_member_display_name(str(card.get("name") or "this person"))
    if card.get("no_mailing"):
        return (
            f"For {name}, No Mailing Address is currently selected. "
            "Is that still correct? Say Yes, or say No to change it."
        )
    if card.get("mailing_same") or _address_home_mailing_match(card):
        home = _address_spoken_line(
            str(card.get("home") or "").strip(),
            str(card.get("home_unit") or "").strip(),
        )
        detail = f" ({home})" if home and "same " not in home.lower() else ""
        return (
            f"For {name}, the mailing address is the same as the home address"
            f"{detail}. Is that correct? Say Yes or No."
        )
    mailing = _address_spoken_line(
        str(card.get("mailing") or "").strip(),
        str(card.get("mailing_unit") or "").strip(),
    )
    if mailing and "same " not in mailing.lower():
        return (
            f"For {name}, is the mailing address {mailing} correct? Say Yes or No."
        )
    return (
        f"For {name}, a mailing address is still needed. "
        "Say Yes to enter one, or say No Mailing Address."
    )


def _address_change_ask_speech(member: str, field: str) -> str:
    label = _clean_member_display_name(member)
    if field == "mailing":
        return (
            f"For {label}, say Yes to change the mailing address, "
            "or say No Mailing Address."
        )
    return (
        f"For {label}, say Yes to change the home address, "
        "or say No Home Address."
    )


def _means_no_home_address(text: str) -> bool:
    """True for No Home Address, including common ASR garble (No, Omar)."""
    lowered = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not lowered:
        return False
    if re.search(r"\bno[,.]?\s*home(?:\s+address)?\b", lowered):
        return True
    # ASR often hears "No Home" as "No, Omar" / "No ohm" / "No own".
    if re.search(r"\bno[,.]?\s*(?:omar|ohm|own|holm|homee)(?:\s+address)?\b", lowered):
        return True
    return bool(re.search(r"\bno\s+address\b", lowered)) and not re.search(
        r"\bmailing\b", lowered
    )


def _means_no_mailing_address(text: str) -> bool:
    """True for No Mailing Address and close ASR variants."""
    lowered = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not lowered:
        return False
    return bool(re.search(r"\bno[,.]?\s*mailing(?:\s+address)?\b", lowered))


def _address_enter_speech(member: str, field: str) -> str:
    label = _clean_member_display_name(member)
    which = "mailing" if field == "mailing" else "home"
    none_label = "No Mailing Address" if which == "mailing" else "No Home Address"
    return f"What is the new {which} address for {label}? Or say {none_label}."


def _address_pending_confirm_speech(
    member: str,
    field: str,
    *,
    kind: str,
    street: str = "",
    chosen: str = "",
) -> str:
    label = _clean_member_display_name(member)
    if kind in ("no_home", "no_mailing"):
        choice = (
            "No Home Address" if kind == "no_home" else "No Mailing Address"
        )
        return (
            f"I'll select {choice} for {label}. "
            "Say Yes to save that, or No to change it."
        )
    which = "home" if field == "home" else "mailing"
    matched = _normalize_address_suggestion_text(chosen or street).strip(" ,.")
    if chosen:
        return (
            f"I matched {matched} as the {which} address for {label}. "
            "Say Yes to save, or No to change it."
        )
    spoken = _address_spoken_line(street)
    return (
        f"I have {spoken} as the {which} address for {label}. "
        "Say Yes to save, or No to change it."
    )


def _address_change_which_prompt(member: str) -> str:
    """Fallback prompt; preferred path is home change-ask then enter."""
    label = _clean_member_display_name(member)
    return (
        f"Okay, I'll open the address editor for {label}. "
        + _address_change_ask_speech(label, "home")
    )


def _address_confirm_speech(
    card: dict[str, Any], cards: list[dict[str, Any]] | None = None
) -> str:
    """Spoken confirmation of the saved card. Streets are not written to logs."""
    name = _clean_member_display_name(str(card.get("name") or "this person"))
    household = cards or [card]
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
    # Same home + mailing: read the address once, then only Yes/No.
    if _address_home_mailing_match(card):
        spoken = _address_spoken_line(
            str(card.get("home") or "").strip(),
            str(card.get("home_unit") or "").strip(),
        )
        if not spoken or "same " in spoken.lower():
            spoken = _address_spoken_line(
                str(card.get("mailing") or "").strip(),
                str(card.get("mailing_unit") or "").strip(),
            )
        if spoken and "same " not in spoken.lower():
            return f"For {name}, the address is {spoken}. Is that correct?"
        return (
            f"For {name}, the home and mailing address are the same. "
            "Is that correct?"
        )
    if card.get("no_home"):
        home_bit = "no home address"
    elif card.get("home_same"):
        resolved = _resolved_same_address(household, card, "home")
        home_bit = (
            f"the same home address as {resolved}"
            if resolved
            else "the same home address"
        )
    elif card.get("home"):
        home_bit = _address_spoken_line(
            str(card.get("home") or "").strip(),
            str(card.get("home_unit") or "").strip(),
        )
    else:
        home_bit = "a home address on the card"
    if card.get("no_mailing"):
        mail_bit = "no mailing address"
    elif card.get("mailing_same"):
        resolved = _resolved_same_address(household, card, "mailing")
        mail_bit = (
            f"the same as the home address ({resolved})"
            if resolved
            else "the same"
        )
    elif card.get("mailing"):
        mail_bit = _address_spoken_line(
            str(card.get("mailing") or "").strip(),
            str(card.get("mailing_unit") or "").strip(),
        )
    else:
        mail_bit = "a mailing address on the card"
    return (
        f"For {name}, the home address is {home_bit}, and the mailing address "
        f"is {mail_bit}. Are both correct?"
    )


async def _address_editor_is_open(page: Page, member_name: str) -> bool:
    """True when the named member's card already shows address inputs."""
    try:
        state = await page.evaluate(
            "(memberName) => {"
            + _member_address_card_js()
            + """
              const card = findAddressCard(memberName);
              if (!card) return false;
              const fields = cardFields(card);
              if (fields.home || fields.mailing || fields.homeUnit || fields.mailingUnit) {
                return true;
              }
              const buttons = [...card.querySelectorAll('button, [role=button], a, input[type=button], input[type=submit]')];
              return buttons.some((btn) => {
                if (!visible(btn)) return false;
                const label = low(btn.innerText || btn.value || btn.textContent || '');
                return label === 'save' || label === 'cancel' ||
                  label.startsWith('save ') || label.startsWith('cancel ');
              });
            }""",
            member_name,
        )
        return state is True
    except Exception:
        return False


async def _prepare_member_address_editor(
    page: Page, member_name: str, field: str = ""
) -> dict[str, Any]:
    """Open the member pencil immediately; clear the named field's X when known."""
    opened = await _open_member_address_editor(page, member_name)
    if not opened.get("ok"):
        return opened
    if field in ("home", "mailing", "both"):
        await _clear_member_address_selection(page, member_name, field)
        await page.wait_for_timeout(150)
        again = await _clear_member_address_selection(page, member_name, field)
        if any(again.values()):
            await page.wait_for_timeout(100)
    return opened


async def _open_member_address_editor(page: Page, member_name: str) -> dict[str, Any]:
    """Open Add or the icon-only pencil on one member card; leave an open editor alone.

    Returns ``{ok, reason, action, icon_only}``. Never clicks a page-wide pencil.
    """
    try:
        if await _address_editor_is_open(page, member_name):
            logger.info(
                "address_editor_open ok=1 member=%r reason=already_open",
                member_name,
            )
            return {
                "ok": True,
                "reason": "already_open",
                "action": "none",
                "icon_only": False,
            }
        located = await page.evaluate(
            "(memberName) => {"
            + _member_address_card_js()
            + """
              for (const el of document.querySelectorAll('[data-pc-address-action]')) {
                el.removeAttribute('data-pc-address-action');
              }
              const card = findAddressCard(memberName);
              if (!card) return { ok: false, reason: 'member_card_not_found' };
              const fields = cardFields(card);
              if (fields.home || fields.mailing || fields.homeUnit || fields.mailingUnit) {
                return { ok: true, reason: 'already_open', action: 'none', icon_only: false };
              }
              const buttons = cardActionButtons(card);
              const editHits = buttons.filter((btn) => isPencilButton(btn));
              const addHits = buttons.filter((btn) => isAddButton(btn));
              let hits = editHits;
              let action = 'edit';
              if (!hits.length) {
                hits = addHits;
                action = 'add';
              }
              if (hits.length !== 1) {
                return {
                  ok: false,
                  reason: hits.length ? 'ambiguous_edit_control' : 'edit_control_not_found',
                  hit_count: hits.length,
                  button_count: buttons.length,
                };
              }
              hits[0].setAttribute('data-pc-address-action', '1');
              try {
                hits[0].scrollIntoView({ block: 'center', inline: 'nearest' });
              } catch (e) {}
              hits[0].click();
              return {
                ok: true,
                reason: 'clicked',
                action,
                icon_only: !low(
                  hits[0].innerText || hits[0].textContent ||
                  hits[0].getAttribute('aria-label') || ''
                ),
              };
            }""",
            member_name,
        )
        if not isinstance(located, dict) or not located.get("ok"):
            reason = (
                str((located or {}).get("reason") or "edit_control_not_found")
                if isinstance(located, dict)
                else "edit_control_not_found"
            )
            logger.warning(
                "address_editor_open ok=0 member=%r reason=%s",
                member_name,
                reason,
            )
            return {
                "ok": False,
                "reason": reason,
                "action": "",
                "icon_only": False,
            }
        await page.wait_for_timeout(250)
        if not await _address_editor_is_open(page, member_name):
            logger.warning(
                "address_editor_open ok=0 member=%r reason=editor_not_visible_after_click",
                member_name,
            )
            return {
                "ok": False,
                "reason": "editor_not_visible_after_click",
                "action": str(located.get("action") or ""),
                "icon_only": bool(located.get("icon_only")),
            }
        logger.info(
            "address_editor_open ok=1 member=%r reason=%s action=%s icon_only=%s",
            member_name,
            located.get("reason"),
            located.get("action"),
            int(bool(located.get("icon_only"))),
        )
        return {
            "ok": True,
            "reason": str(located.get("reason") or "opened"),
            "action": str(located.get("action") or ""),
            "icon_only": bool(located.get("icon_only")),
        }
    except Exception:
        logger.exception("open_member_address_editor failed")
        return {
            "ok": False,
            "reason": "open_exception",
            "action": "",
            "icon_only": False,
        }


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


async def _address_share_dialog_info(page: Page) -> dict[str, Any]:
    """Detect the 'Update household addresses' shared-address popup."""
    try:
        raw = await page.evaluate(
            """() => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const low = (s) => norm(s).toLowerCase();
              const isShown = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                if (r.width < 40 || r.height < 40) return false;
                const st = window.getComputedStyle(el);
                if (st.visibility === 'hidden' || st.display === 'none' ||
                    Number(st.opacity || '1') === 0) return false;
                return true;
              };
              const roots = [
                ...document.querySelectorAll(
                  '[role=dialog], [aria-modal=true], .modal, [class*="Modal"], [class*="dialog"]'
                ),
              ];
              let dialog = null;
              for (const el of roots) {
                if (!isShown(el)) continue;
                const text = low(el.innerText || el.textContent || '');
                if (
                  text.includes('update household address') ||
                  text.includes('shares your home address') ||
                  text.includes('shares your mailing address') ||
                  text.includes('shares your address') ||
                  text.includes('currently share') ||
                  text.includes('only update mine') ||
                  (text.includes('update') && text.includes('member') &&
                    text.includes('address')) ||
                  (text.includes('would you like to update') && text.includes('as well'))
                ) {
                  dialog = el;
                  break;
                }
              }
              // Footer-only detection when the dialog root is a generic overlay.
              if (!dialog) {
                for (const el of document.querySelectorAll('button, [role=button]')) {
                  if (!isShown(el)) continue;
                  const ll = low(el.innerText || el.textContent || '');
                  if (!(ll.includes('only update mine') || /^update \\d+ members?$/.test(ll))) {
                    continue;
                  }
                  const root = el.closest(
                    '[role=dialog], [aria-modal=true], .modal, [class*="Modal"], [class*="dialog"], body'
                  );
                  if (root && root !== document.body) {
                    dialog = root;
                    break;
                  }
                }
              }
              if (!dialog) return { open: false };
              const text = norm(dialog.innerText || dialog.textContent || '');
              const members = [];
              for (const box of dialog.querySelectorAll(
                'input[type=checkbox], [role=checkbox]'
              )) {
                let label = '';
                if (box.id) {
                  const lab = dialog.querySelector('label[for="' + CSS.escape(box.id) + '"]');
                  if (lab) label = norm(lab.innerText || lab.textContent || '');
                }
                if (!label) {
                  const host = box.closest('label') || box.parentElement;
                  label = norm((host && (host.innerText || host.textContent)) || '');
                }
                label = label.split('\\n')[0].trim();
                if (label && !members.includes(label)) members.push(label);
              }
              const buttons = [...dialog.querySelectorAll(
                'button, [role=button], a, input[type=button], input[type=submit]'
              )].filter(isShown);
              let onlyMine = '';
              let updateShared = '';
              for (const btn of buttons) {
                // Never treat checkbox/label rows as the footer actions.
                if (
                  btn.closest('[role=checkbox]') ||
                  btn.getAttribute('role') === 'checkbox' ||
                  btn.querySelector('input[type=checkbox], [role=checkbox]')
                ) {
                  continue;
                }
                const label = norm(btn.innerText || btn.value || btn.textContent || '');
                const ll = low(label);
                if (!label) continue;
                if (ll.includes('only update mine')) onlyMine = label;
                if (/^update \\d+ members?$/i.test(label)) updateShared = label;
              }
              return {
                open: true,
                prompt: text.slice(0, 240),
                members,
                only_mine: onlyMine,
                update_shared: updateShared,
              };
            }"""
        )
        if isinstance(raw, dict) and raw.get("open"):
            return {
                "open": True,
                "prompt": str(raw.get("prompt") or ""),
                "members": [
                    str(item).strip()
                    for item in (raw.get("members") or [])
                    if str(item).strip()
                ],
                "only_mine": str(raw.get("only_mine") or ""),
                "update_shared": str(raw.get("update_shared") or ""),
            }
    except Exception:
        logger.exception("address_share_dialog_info failed")
    return {"open": False}


def _address_share_ask_speech(info: dict[str, Any]) -> str:
    members = []
    for name in info.get("members") or []:
        label = _clean_member_display_name(str(name))
        if not label or label == "that person":
            continue
        if label not in members:
            members.append(label)
    if members:
        who = ", ".join(members[:3])
        return (
            f"{who} currently share this address. "
            "Say Yes to update them as well, or No to only update yours."
        )
    prompt = str(info.get("prompt") or "")
    if "shares your" in prompt.lower():
        # Keep the Pointcare wording short for speech.
        first = prompt.split("Would you like")[0].strip()
        if first:
            return (
                f"{first} Would you like to update theirs as well? "
                "Say Yes to update them, or No to only update yours."
            )
    return (
        "Another household member currently shares this address. "
        "Say Yes to update them as well, or No to only update yours."
    )


async def _click_address_share_dialog_choice(
    page: Page, *, update_others: bool
) -> bool:
    """Click Update N member(s) or No, only update mine — never member checkboxes."""
    info = await _address_share_dialog_info(page)
    if not info.get("open"):
        return True
    try:
        box = await page.evaluate(
            """(updateOthers) => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const low = (s) => norm(s).toLowerCase();
              const isShown = (el) => {
                const r = el.getBoundingClientRect();
                if (r.width < 8 || r.height < 8) return false;
                const st = window.getComputedStyle(el);
                return !(st.visibility === 'hidden' || st.display === 'none' ||
                         Number(st.opacity || '1') === 0);
              };
              const isCheckboxish = (el) => {
                if (!el) return true;
                const role = (el.getAttribute('role') || '').toLowerCase();
                if (role === 'checkbox') return true;
                if (el.matches && el.matches('input[type=checkbox]')) return true;
                if (el.closest && el.closest('[role=checkbox], label')) {
                  const host = el.closest('[role=checkbox], label');
                  if (host && host.querySelector('input[type=checkbox], [role=checkbox]')) {
                    return true;
                  }
                }
                return false;
              };
              const roots = [
                ...document.querySelectorAll(
                  '[role=dialog], [aria-modal=true], .modal, [class*="Modal"], [class*="dialog"]'
                ),
              ].filter(isShown);
              const dialog = roots.find((el) => {
                const text = low(el.innerText || el.textContent || '');
                return (
                  text.includes('update household address') ||
                  text.includes('shares your') ||
                  text.includes('would you like to update')
                );
              });
              if (!dialog) return null;
              const buttons = [...dialog.querySelectorAll(
                'button, [role=button], a, input[type=button], input[type=submit]'
              )].filter((btn) => isShown(btn) && !isCheckboxish(btn));
              let pick = null;
              for (const btn of buttons) {
                const label = low(btn.innerText || btn.value || btn.textContent || '');
                if (!label) continue;
                if (updateOthers) {
                  if (/^update \\d+ members?$/.test(label)) {
                    pick = btn;
                    break;
                  }
                } else if (label.includes('only update mine')) {
                  pick = btn;
                  break;
                }
              }
              if (!pick) return null;
              pick.scrollIntoView({ block: 'nearest', inline: 'nearest' });
              const r = pick.getBoundingClientRect();
              return {
                x: r.x + r.width / 2,
                y: r.y + r.height / 2,
                label: norm(pick.innerText || pick.value || pick.textContent || ''),
              };
            }""",
            bool(update_others),
        )
        if not isinstance(box, dict) or box.get("x") is None:
            logger.warning(
                "address_share_dialog_choice_missing update_others=%s info=%s",
                int(bool(update_others)),
                {
                    "only_mine": info.get("only_mine"),
                    "update_shared": info.get("update_shared"),
                },
            )
            return False
        # Real mouse click — synthetic JS clicks often miss Pointcare dialogs.
        await page.mouse.click(float(box["x"]), float(box["y"]))
        await page.wait_for_timeout(400)
        still = await _address_share_dialog_info(page)
        if still.get("open"):
            logger.warning(
                "address_share_dialog_still_open after click label=%r",
                box.get("label"),
            )
            return False
        return True
    except Exception:
        logger.exception("click_address_share_dialog_choice failed")
        return False


async def _set_member_no_address(page: Page, member_name: str, kind: str) -> bool:
    """Check No Home Address or No Mailing Address on one member card."""
    phrase = "no home address" if kind == "home" else "no mailing address"
    display = "No Home Address" if kind == "home" else "No Mailing Address"

    async def _is_on() -> bool:
        try:
            return bool(
                await page.evaluate(
                    "({ memberName, phrase }) => {"
                    + _member_address_card_js()
                    + """
                      const card = findAddressCard(memberName);
                      if (!card) return false;
                      const boxes = [...card.querySelectorAll(
                        'input[type=checkbox], [role=checkbox], button[role=checkbox]'
                      )];
                      for (const box of boxes) {
                        let label = '';
                        if (box.id) {
                          const lab = card.querySelector(
                            'label[for=\"' + CSS.escape(box.id) + '\"]'
                          );
                          if (lab) label = low(lab.innerText || lab.textContent || '');
                        }
                        if (!label) {
                          const host = box.closest('label') || box.parentElement;
                          label = low(
                            (host && (host.innerText || host.textContent)) ||
                            box.getAttribute('aria-label') || ''
                          );
                        }
                        if (!label.includes(phrase)) continue;
                        const aria = (box.getAttribute('aria-checked') || '').toLowerCase();
                        const state = (box.getAttribute('data-state') || '').toLowerCase();
                        return (
                          box.checked === true ||
                          aria === 'true' ||
                          state === 'checked'
                        );
                      }
                      return false;
                    }""",
                    {"memberName": member_name, "phrase": phrase},
                )
            )
        except Exception:
            return False

    if await _is_on():
        return True

    # Mark + click via evaluate (Radix / RN often need the row, not only input).
    try:
        clicked = await page.evaluate(
            "({ memberName, phrase }) => {"
            + _member_address_card_js()
            + """
              for (const el of document.querySelectorAll('[data-pc-no-addr]')) {
                el.removeAttribute('data-pc-no-addr');
              }
              const card = findAddressCard(memberName);
              if (!card) return { ok: false, reason: 'card_missing' };
              const boxes = [...card.querySelectorAll(
                'input[type=checkbox], [role=checkbox], button[role=checkbox]'
              )];
              for (const box of boxes) {
                let labelEl = null;
                let label = '';
                if (box.id) {
                  labelEl = card.querySelector(
                    'label[for=\"' + CSS.escape(box.id) + '\"]'
                  );
                  if (labelEl) label = low(labelEl.innerText || labelEl.textContent || '');
                }
                if (!label) {
                  labelEl = box.closest('label') || box.parentElement;
                  label = low(
                    (labelEl && (labelEl.innerText || labelEl.textContent)) ||
                    box.getAttribute('aria-label') || ''
                  );
                }
                if (!label.includes(phrase)) continue;
                const target = labelEl || box;
                target.setAttribute('data-pc-no-addr', '1');
                try { target.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
                try { target.click(); } catch (e) {}
                try { box.click(); } catch (e) {}
                return { ok: true, reason: 'clicked' };
              }
              return { ok: false, reason: 'checkbox_missing' };
            }""",
            {"memberName": member_name, "phrase": phrase},
        )
    except Exception:
        logger.exception("set_member_no_address evaluate failed")
        clicked = {"ok": False, "reason": "exception"}

    # Playwright force-click fallback on the visible label text.
    if not (isinstance(clicked, dict) and clicked.get("ok")):
        try:
            loc = page.get_by_text(re.compile(rf"^\s*{re.escape(display)}\s*$", re.I))
            if await loc.count():
                await loc.first.click(timeout=2500, force=True)
                clicked = {"ok": True, "reason": "playwright_text"}
        except Exception:
            pass
    # Real mouse click — synthetic clicks often miss Pointcare checkbox rows.
    try:
        marked = page.locator("[data-pc-no-addr='1']").first
        if await marked.count():
            box = await marked.bounding_box()
            if box and box.get("width") and box.get("height"):
                await page.mouse.click(
                    float(box["x"]) + float(box["width"]) / 2.0,
                    float(box["y"]) + float(box["height"]) / 2.0,
                )
                clicked = {
                    "ok": True,
                    "reason": "mouse_"
                    + str((clicked or {}).get("reason") or "marked"),
                }
            else:
                await marked.click(timeout=2500, force=True)
                clicked = {"ok": True, "reason": "force_marked"}
    except Exception:
        pass

    for _ in range(10):
        if await _is_on():
            logger.info(
                "address_no_checkbox ok=1 member=%r kind=%s via=%s",
                member_name,
                kind,
                (clicked or {}).get("reason") if isinstance(clicked, dict) else "-",
            )
            return True
        await page.wait_for_timeout(150)
    logger.warning(
        "address_no_checkbox ok=0 member=%r kind=%s click=%r",
        member_name,
        kind,
        clicked,
    )
    return False


async def _clear_member_address_selection(
    page: Page, member_name: str, field: str
) -> dict[str, bool]:
    """Clear a selected address chip via its rounded-full X before replace.

    Pointcare keeps the chosen place in a text chip (not always input.value) with
    a circular icon-only clear button. Only the requested field section is cleared.
    """
    targets: list[str] = []
    if field in ("home", "both"):
        targets.append("home")
    if field in ("mailing", "both"):
        targets.append("mailing")
    if not targets:
        return {}
    try:
        raw = await page.evaluate(
            "({ memberName, targets }) => {"
            + _member_address_card_js()
            + """
              const card = findAddressCard(memberName);
              if (!card) return null;
              for (const el of card.querySelectorAll('[data-pc-addr-clear]')) {
                el.removeAttribute('data-pc-addr-clear');
              }
              const fields = cardFields(card);
              const cleared = {};
              const clicks = [];
              const sectionBand = (kind) => {
                const want = kind === 'mailing' ? 'mailing address' : 'home address';
                const other = kind === 'mailing' ? 'home address' : 'mailing address';
                const input = fields[kind];
                // Prefer the live input row — collapsed summary labels above the
                // editor can steal the first "Home/Mailing address" match.
                if (input) {
                  const ir = input.getBoundingClientRect();
                  return { top: ir.top - 56, bottom: ir.bottom + 48 };
                }
                let startY = null;
                let endY = null;
                const labelHits = [];
                for (const el of card.querySelectorAll('div, span, label, p, h1, h2, h3, h4')) {
                  const t = low(el.innerText || el.textContent || '');
                  if (t !== want && !t.startsWith(want + ' ')) continue;
                  if ((el.innerText || '').trim().length > 40) continue;
                  const r = el.getBoundingClientRect();
                  if (r.height < 4 || r.height > 48) continue;
                  labelHits.push(r.top);
                }
                // Use the lowest matching label (closest to the open editor).
                if (labelHits.length) startY = Math.max(...labelHits);
                for (const el of card.querySelectorAll(
                  'div, span, label, p, h1, h2, h3, h4, button, [role=checkbox]'
                )) {
                  const t = low(el.innerText || el.textContent || '');
                  const r = el.getBoundingClientRect();
                  if (startY != null && r.top <= startY + 2) continue;
                  if (
                    t === other || t.startsWith(other + ' ') ||
                    t === 'no home address' || t === 'no mailing address' ||
                    t === 'save' || t === 'cancel'
                  ) {
                    if (r.height <= 48) {
                      endY = r.top;
                      break;
                    }
                  }
                }
                if (startY == null) return null;
                return {
                  top: startY - 8,
                  bottom: endY != null ? endY - 4 : startY + 260,
                };
              };
              const looksAddrChip = (t) => (
                /\\d|,|street|st\\b|ave|avenue|road|rd\\b|highway|blvd|lane|drive|way|bridge|parkway|plaza|square|usa|ny|ca|wa|tx/i.test(t)
              );
              const isClearControl = (el) => {
                if (!el || !visible(el)) return false;
                if (isPencilButton(el)) return false;
                if (el.getAttribute('role') === 'checkbox') return false;
                const br = el.getBoundingClientRect();
                if (br.width < 10 || br.height < 10 || br.width > 44 || br.height > 44) {
                  return false;
                }
                const label = low(
                  (el.getAttribute('aria-label') || '') + ' ' +
                  (el.getAttribute('title') || '') + ' ' +
                  (el.innerText || el.textContent || '')
                );
                if (/cancel|save|edit|add|next|continue|back|no home|no mailing/.test(label)) {
                  return false;
                }
                const svg = (el.tagName || '').toLowerCase() === 'svg'
                  ? el
                  : el.querySelector('svg');
                const paths = svg
                  ? [...svg.querySelectorAll('path')].map((p) => p.getAttribute('d') || '').join(' ')
                  : '';
                const text = norm(el.innerText || el.textContent || '');
                if (label === 'x' || label === '×' || label.includes('clear') ||
                    label.includes('remove') || label.includes('delete')) return true;
                if (text === '×' || text === 'x' || text === 'X') return true;
                // Live Pointcare: Lucide X inside the location chip.
                if (paths.includes('M18 6 6 18') || paths.includes('m6 6 12 12') ||
                    paths.includes('M18 6L6 18') || paths.includes('M6 6l12 12')) {
                  return true;
                }
                const square = Math.abs(br.width - br.height) <= 8;
                if (square && svg && text.length <= 1 && br.width <= 36) return true;
                return false;
              };
              const clearOne = (key) => {
                const band = sectionBand(key);
                const bandOk = Boolean(band && (band.bottom - band.top) > 20);
                const input = fields[key];
                const candidates = [];
                const consider = (el) => {
                  if (!isClearControl(el) || candidates.includes(el)) return;
                  const br = el.getBoundingClientRect();
                  if (bandOk && (br.top < band.top - 4 || br.top > band.bottom + 4)) return;
                  candidates.push(el);
                };
                // Prefer real buttons first — Pointcare chip X is usually a button.
                for (const el of card.querySelectorAll('button, [role=button]')) {
                  consider(el);
                }
                if (input) {
                  const ir = input.getBoundingClientRect();
                  let host = input.parentElement;
                  for (let i = 0; host && i < 6; i += 1, host = host.parentElement) {
                    for (const el of host.querySelectorAll('button, [role=button]')) {
                      if (!isClearControl(el)) continue;
                      const br = el.getBoundingClientRect();
                      const near =
                        Math.abs(br.top - ir.top) < 56 &&
                        br.left >= ir.left - 8 &&
                        br.left <= ir.right + 80;
                      if (near) consider(el);
                    }
                    if (host === card) break;
                  }
                }
                // Chip/div fallback when the selected place has no writable input yet.
                if (!candidates.length) {
                  for (const el of card.querySelectorAll('div, span, svg')) {
                    consider(el);
                  }
                }
                if (!candidates.length && bandOk) {
                  for (const chip of card.querySelectorAll('div, span, p, label')) {
                    const t = norm(chip.innerText || chip.textContent || '');
                    const cr = chip.getBoundingClientRect();
                    if (!t || t.length < 8 || t.length > 160) continue;
                    if (cr.top < band.top - 4 || cr.top > band.bottom + 4) continue;
                    if (!looksAddrChip(t)) continue;
                    let root = chip;
                    for (let i = 0; root && i < 5; i += 1, root = root.parentElement) {
                      for (const el of root.querySelectorAll(
                        'button, [role=button], div, span, svg'
                      )) {
                        consider(el);
                      }
                    }
                  }
                }
                if (candidates.length > 1) {
                  const ir = input ? input.getBoundingClientRect() : null;
                  candidates.sort((a, b) => {
                    const ar = a.getBoundingClientRect();
                    const br = b.getBoundingClientRect();
                    if (ir) {
                      const da = Math.abs(ar.top - ir.top) + Math.abs(ar.left - ir.right);
                      const db = Math.abs(br.top - ir.top) + Math.abs(br.left - ir.right);
                      return da - db;
                    }
                    // Prefer the rightmost small control in the section (chip X).
                    return (br.left - ar.left) || (ar.top - br.top);
                  });
                }
                if (candidates.length) {
                  const target = candidates[0];
                  const clickEl = target.closest('button, [role=button]') || target;
                  clickEl.setAttribute('data-pc-addr-clear', key);
                  const br = clickEl.getBoundingClientRect();
                  clicks.push({
                    key,
                    x: br.left + br.width / 2,
                    y: br.top + br.height / 2,
                    w: br.width,
                    h: br.height,
                  });
                  try { clickEl.click(); } catch (e) {}
                  cleared[key] = true;
                  return;
                }
                if (input && (input.value || '').trim()) {
                  const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                  ).set;
                  input.focus();
                  setter.call(input, '');
                  input.dispatchEvent(new Event('input', { bubbles: true }));
                  input.dispatchEvent(new Event('change', { bubbles: true }));
                  cleared[key] = true;
                  return;
                }
                cleared[key] = false;
              };
              for (const key of targets) clearOne(key);
              return { cleared, clicks };
            }""",
            {"memberName": member_name, "targets": targets},
        )
        result: dict[str, bool] = {}
        clicks: list[dict[str, Any]] = []
        if isinstance(raw, dict):
            cleared = raw.get("cleared")
            if isinstance(cleared, dict):
                result = {str(k): bool(v) for k, v in cleared.items()}
            else:
                # Older shape fallback.
                result = {
                    str(k): bool(v)
                    for k, v in raw.items()
                    if k in ("home", "mailing")
                }
            maybe_clicks = raw.get("clicks")
            if isinstance(maybe_clicks, list):
                clicks = [c for c in maybe_clicks if isinstance(c, dict)]
        # Real mouse click — Pointcare chip X often ignores synthetic click alone.
        for item in clicks:
            try:
                x = float(item.get("x") or 0)
                y = float(item.get("y") or 0)
                if x > 0 and y > 0:
                    await page.mouse.click(x, y)
                    await page.wait_for_timeout(120)
                    key = str(item.get("key") or "")
                    if key:
                        result[key] = True
            except Exception:
                logger.exception("address_clear_mouse_click failed")
        try:
            marked = page.locator("[data-pc-addr-clear]").first
            if await marked.count():
                box = await marked.bounding_box()
                if box and box.get("width") and box.get("height"):
                    await page.mouse.click(
                        float(box["x"]) + float(box["width"]) / 2.0,
                        float(box["y"]) + float(box["height"]) / 2.0,
                    )
                    await page.wait_for_timeout(100)
        except Exception:
            pass
        logger.info(
            "address_clear member=%r field=%s home=%s mailing=%s clicks=%s",
            member_name,
            field,
            int(bool(result.get("home"))),
            int(bool(result.get("mailing"))),
            len(clicks),
        )
        return result
    except Exception:
        logger.exception("clear_member_address_selection failed")
        return {}


async def _fill_member_address_inputs(
    page: Page,
    member_name: str,
    *,
    home: str = "",
    home_unit: str = "",
    mailing: str = "",
    mailing_unit: str = "",
) -> dict[str, str]:
    """Fill only the requested fields inside one member card.

    Uses the native value setter so React/Pointcare controlled inputs keep the text.
    """
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
                const setter = Object.getOwnPropertyDescriptor(
                  window.HTMLInputElement.prototype, 'value'
                ).set;
                setter.call(el, value);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new InputEvent('input', { bubbles: true, data: value }));
              };
              if (home) {
                fields.home && fields.home.setAttribute('data-pc-addr', 'home');
                setVal(fields.home, home);
              }
              if (homeUnit) setVal(fields.homeUnit, homeUnit);
              if (mailing) {
                fields.mailing && fields.mailing.setAttribute('data-pc-addr', 'mailing');
                setVal(fields.mailing, mailing);
              }
              if (mailingUnit) setVal(fields.mailingUnit, mailingUnit);
              return {
                home: fields.home ? (fields.home.value || '') : '',
                home_unit: fields.homeUnit ? (fields.homeUnit.value || '') : '',
                mailing: fields.mailing ? (fields.mailing.value || '') : '',
                mailing_unit: fields.mailingUnit ? (fields.mailingUnit.value || '') : '',
                home_found: Boolean(fields.home),
                mailing_found: Boolean(fields.mailing),
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
        if not isinstance(raw, dict):
            return {}
        # Playwright fill as a second path when React still dropped the value.
        for key, value in (("home", home), ("mailing", mailing)):
            if not value:
                continue
            current = str(raw.get(key) or "")
            if value.lower()[:8] in current.lower():
                continue
            loc = page.locator(f'[data-pc-addr="{key}"]')
            if await loc.count() == 1:
                try:
                    await loc.fill(value)
                    await page.wait_for_timeout(50)
                    raw[key] = await loc.input_value()
                except Exception:
                    logger.exception("address_playwright_fill_failed field=%s", key)
        return raw
    except Exception:
        logger.exception("fill_member_address_inputs failed")
        return {}


async def _visible_address_suggestions(page: Page) -> list[str]:
    """Visible autocomplete rows. Position in the list is not a choice."""
    try:
        rows = await page.evaluate(
            """() => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const isShown = (el) => {
                const r = el.getBoundingClientRect();
                if (r.width < 8 || r.height < 8) return false;
                const st = window.getComputedStyle(el);
                if (st.visibility === 'hidden' || st.display === 'none' ||
                    Number(st.opacity || '1') === 0) return false;
                let node = el;
                for (let i = 0; node && i < 6; i += 1, node = node.parentElement) {
                  const cs = window.getComputedStyle(node);
                  if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                  if (node.getAttribute('aria-hidden') === 'true') return false;
                }
                return true;
              };
              const nodes = [...document.querySelectorAll(
                '[role=option], .pac-item, [class*="suggestion"]'
              )];
              const out = [];
              for (const el of nodes) {
                if (!isShown(el)) continue;
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


async def _type_address_for_places_dropdown(
    page: Page, member_name: str, field: str, street: str
) -> dict[str, str]:
    """Type into the Places location field so the Google dropdown opens.

    Native value assignment alone does not reliably open pac suggestions.
    """
    key = "mailing" if field == "mailing" else "home"
    street = _clean_spoken_street(street)
    # Tag the target input, then type character-by-character.
    tagged = await page.evaluate(
        "({ memberName, key }) => {"
        + _member_address_card_js()
        + """
          const card = findAddressCard(memberName);
          if (!card) return { ok: false, reason: 'no_card' };
          const fields = cardFields(card);
          let el = fields[key];
          // Fallback: location placeholders in home/mailing order.
          if (!el) {
            const locs = [...card.querySelectorAll('input')].filter((input) => {
              const t = (input.type || 'text').toLowerCase();
              if (t === 'checkbox' || t === 'radio' || t === 'hidden') return false;
              const label = labelFor(card, input);
              const ph = ((input.placeholder || '') + ' ' +
                (input.getAttribute('aria-label') || '') + ' ' + label).toLowerCase();
              if (ph.includes('apartment') || ph.includes('unit') || ph.includes('po box')) {
                return false;
              }
              return (
                ph.includes('location') || ph.includes('address') ||
                ph.includes('home') || ph.includes('mailing') || !ph.trim()
              );
            });
            if (key === 'mailing') {
              const byLabel = locs.find((input) => labelFor(card, input).includes('mailing'));
              // After No Home Address only the mailing location input remains.
              el = byLabel || (locs.length === 1 ? locs[0] : (locs[1] || locs[0] || null));
            } else {
              const byLabel = locs.find((input) => labelFor(card, input).includes('home'));
              el = byLabel || locs[0] || null;
            }
          }
          if (!el) return { ok: false, reason: 'no_input' };
          for (const node of card.querySelectorAll('[data-pc-addr]')) {
            node.removeAttribute('data-pc-addr');
          }
          el.setAttribute('data-pc-addr', key);
          try { el.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
          return { ok: true };
        }""",
        {"memberName": member_name, "key": key},
    )
    if not isinstance(tagged, dict) or not tagged.get("ok"):
        logger.warning(
            "address_type_tag_failed member=%r field=%s detail=%s",
            member_name,
            key,
            tagged,
        )
        return {}
    loc = page.locator(f'[data-pc-addr="{key}"]')
    if await loc.count() != 1:
        logger.warning(
            "address_type_locator_missing member=%r field=%s count=%s",
            member_name,
            key,
            await loc.count(),
        )
        return {}
    try:
        await loc.click(timeout=2000)
        await loc.fill("")
        await loc.press_sequentially(street, delay=35)
    except Exception:
        logger.exception("type_address_for_places_dropdown failed")
        return {}
    await page.wait_for_timeout(200)
    values = await _fill_member_address_inputs(page, member_name)
    if isinstance(values, dict):
        # Ensure the typed field is reported even if React label mapping lags.
        current = str(values.get(key) or "")
        if street.lower()[:8] not in current.lower():
            try:
                values[key] = await loc.input_value()
            except Exception:
                values[key] = street
        return values
    return {}


async def _click_address_suggestion(page: Page, suggestion: str) -> bool:
    """Click the visible Places dropdown row with a real mouse hit.

    Synthetic JS clicks do not commit Google autocomplete on Pointcare.
    """
    try:
        box = await page.evaluate(
            """(suggestion) => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const want = norm(suggestion);
              if (!want) return null;
              const isShown = (el) => {
                const r = el.getBoundingClientRect();
                if (r.width < 8 || r.height < 8) return false;
                const st = window.getComputedStyle(el);
                if (st.visibility === 'hidden' || st.display === 'none' ||
                    Number(st.opacity || '1') === 0) return false;
                let node = el;
                for (let i = 0; node && i < 6; i += 1, node = node.parentElement) {
                  const cs = window.getComputedStyle(node);
                  if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                  if (node.getAttribute('aria-hidden') === 'true') return false;
                }
                return true;
              };
              const nodes = [...document.querySelectorAll(
                '[role=option], .pac-item, [class*="suggestion"]'
              )];
              const ranked = [];
              for (const el of nodes) {
                if (!isShown(el)) continue;
                const raw = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const text = norm(raw);
                if (!text) continue;
                let rank = 0;
                if (text === want) rank = 3;
                else if (text.includes(want) || want.includes(text)) rank = 2;
                else {
                  const wantHead = want.split(',')[0].trim();
                  if (wantHead && (text.includes(wantHead) || wantHead.includes(text.split(',')[0].trim()))) {
                    rank = 1;
                  }
                }
                if (!rank) continue;
                ranked.push({ el, rank, raw });
              }
              ranked.sort((a, b) => b.rank - a.rank);
              if (!ranked.length) return null;
              // Only click when exactly one best row matches.
              const bestRank = ranked[0].rank;
              const top = ranked.filter((row) => row.rank === bestRank);
              if (top.length !== 1) return null;
              const pick = top[0];
              pick.el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
              const r = pick.el.getBoundingClientRect();
              return {
                x: r.x + r.width / 2,
                y: r.y + Math.min(r.height / 2, 14),
                text: pick.raw,
              };
            }""",
            suggestion,
        )
        if not isinstance(box, dict) or box.get("x") is None:
            return False
        # Mark + mouse click the exact matched row (never ArrowDown — wrong row risk).
        await page.evaluate(
            """(suggestion) => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const want = norm(suggestion);
              for (const el of document.querySelectorAll('[role=option], .pac-item, [class*="suggestion"]')) {
                el.removeAttribute('data-pc-pac');
                const text = norm(el.innerText || el.textContent || '');
                if (!text) continue;
                if (text === want || text.includes(want) || want.includes(text)) {
                  el.setAttribute('data-pc-pac', '1');
                  break;
                }
              }
            }""",
            suggestion,
        )
        marked = page.locator("[data-pc-pac='1']").first
        if await marked.count():
            box2 = await marked.bounding_box()
            if box2 and box2.get("width") and box2.get("height"):
                await page.mouse.click(
                    float(box2["x"]) + float(box2["width"]) / 2.0,
                    float(box2["y"]) + min(float(box2["height"]) / 2.0, 14.0),
                )
            else:
                await marked.click(timeout=2000, force=True)
        else:
            await page.mouse.click(float(box["x"]), float(box["y"]))
        await page.wait_for_timeout(400)
        return True
    except Exception:
        logger.exception("click_address_suggestion failed")
        return False


async def _address_places_selection_committed(
    page: Page, member_name: str, field: str, typed: str, chosen: str
) -> bool:
    """True when a dropdown row was actually selected (not merely typed)."""
    key = "mailing" if field == "mailing" else "home"
    try:
        state = await page.evaluate(
            "({ memberName, key, typed, chosen }) => {"
            + _member_address_card_js()
            + """
              const card = findAddressCard(memberName);
              if (!card) return { ok: false, reason: 'no_card' };
              const cardText = low(card.innerText || '');
              // Pointcare shows this until a Places row is selected.
              if (/please enter and select|select a (home|mailing) address|choose an address from the list/.test(cardText)) {
                return { ok: false, reason: 'validation_visible' };
              }
              const fields = cardFields(card);
              const input = fields[key];
              if (!input) return { ok: false, reason: 'no_input' };
              const value = (input.value || '').trim();
              if (!value) return { ok: false, reason: 'empty' };
              const pacOpen = [...document.querySelectorAll('.pac-item, [role=option]')].some((el) => {
                const r = el.getBoundingClientRect();
                if (r.width < 8 || r.height < 8) return false;
                let node = el;
                for (let i = 0; node && i < 6; i += 1, node = node.parentElement) {
                  const cs = window.getComputedStyle(node);
                  if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                }
                return true;
              });
              if (pacOpen) return { ok: false, reason: 'pac_open', value };
              // Selected Places rows show the circular clear X on the field.
              const ir = input.getBoundingClientRect();
              let clearNear = false;
              let host = input.parentElement;
              for (let depth = 0; host && depth < 5; depth += 1, host = host.parentElement) {
                for (const btn of host.querySelectorAll('button, [role=button]')) {
                  const svg = btn.querySelector('svg');
                  if (!svg) continue;
                  const paths = [...svg.querySelectorAll('path')]
                    .map((p) => p.getAttribute('d') || '').join(' ');
                  if (!(paths.includes('M18 6 6 18') || paths.includes('m6 6 12 12'))) {
                    continue;
                  }
                  const br = btn.getBoundingClientRect();
                  if (Math.abs(br.top - ir.top) < 48) clearNear = true;
                }
                if (host === card) break;
              }
              // Fold AFTER toLowerCase; keep /i so "Street"/"Drive" still match.
              const foldAddr = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase()
                .replace(/\\b(street)\\b/gi, 'st')
                .replace(/\\b(drive)\\b/gi, 'dr')
                .replace(/\\b(avenue)\\b/gi, 'ave')
                .replace(/\\b(road)\\b/gi, 'rd')
                .replace(/\\b(lane)\\b/gi, 'ln')
                .replace(/\\b(boulevard)\\b/gi, 'blvd')
                .replace(/\\b(north carolina)\\b/gi, 'nc')
                .replace(/\\b(massachusetts)\\b/gi, 'ma')
                .replace(/\\b(united states(?: of america)?)\\b/gi, 'usa');
              const typedNorm = foldAddr(typed);
              const valueNorm = foldAddr(value);
              const chosenNorm = foldAddr(chosen);
              const tokens = (s) => s.split(/[^a-z0-9]+/).filter((t) => t.length >= 2 && !/^(usa|us|the|of)$/.test(t));
              const valueTokens = new Set(tokens(valueNorm));
              const chosenTokens = tokens(chosenNorm);
              const overlap = chosenTokens.length
                ? chosenTokens.filter((t) => valueTokens.has(t)).length / chosenTokens.length
                : 0;
              // Must look like the Places row, not free-typed speech alone.
              const matchesChosen = Boolean(chosenNorm) && (
                valueNorm === chosenNorm ||
                valueNorm.includes(chosenNorm) ||
                chosenNorm.includes(valueNorm) ||
                overlap >= 0.65
              );
              const richerThanTyped = Boolean(typedNorm) && valueNorm !== typedNorm && (
                valueNorm.includes(typedNorm.split(',')[0].trim()) ||
                typedNorm.includes(valueNorm.split(',')[0].trim()) ||
                valueNorm.includes(typedNorm) ||
                typedNorm.includes(valueNorm.split(',')[0] || '')
              );
              // Clear-X + richer Places value means the row was selected even when
              // the pac label abbreviates Street→St (overlap used to fail at 0.67).
              const ok = clearNear && (matchesChosen || richerThanTyped);
              return {
                ok,
                value,
                pacOpen,
                clearNear,
                matchesChosen,
                overlap,
                richerThanTyped,
              };
            }""",
            {
                "memberName": member_name,
                "key": key,
                "typed": typed,
                "chosen": chosen,
            },
        )
        if isinstance(state, dict) and state.get("ok"):
            return True
        logger.info(
            "address_places_not_committed member=%r field=%s detail=%s",
            member_name,
            field,
            state,
        )
        return False
    except Exception:
        logger.exception("address_places_selection_committed failed")
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
              // Do not treat the "Enter a location" placeholder as validation.
              return /please enter a valid|please enter and select|invalid address|address is required|select an address|select a (home|mailing) address|choose an address from the list|or check ['\"]?no (home|mailing) address/.test(text);
            }""",
            member_name,
        )
    except Exception:
        return False
    return state is True


def _card_by_name(
    cards: list[dict[str, Any]], name: str
) -> dict[str, Any] | None:
    want = (name or "").strip()
    hits = [
        card
        for card in cards
        if _address_member_names_match(str(card.get("name") or ""), want)
    ]
    if len(hits) != 1:
        return None
    return hits[0]


def _address_member_names_match(left: str, right: str) -> bool:
    """Match ``john`` to ``john D.`` / ``Agent`` to ``Agent T. (You)``."""
    a = re.sub(r"\s*\(you\)\s*", " ", (left or ""), flags=re.I)
    b = re.sub(r"\s*\(you\)\s*", " ", (right or ""), flags=re.I)
    a = re.sub(r"[^a-z0-9\s]", " ", a.lower())
    b = re.sub(r"[^a-z0-9\s]", " ", b.lower())
    a = re.sub(r"\s+", " ", a).strip()
    b = re.sub(r"\s+", " ", b).strip()
    if not a or not b:
        return False
    if a == b:
        return True
    return a.startswith(b + " ") or b.startswith(a + " ")


def _other_addresses_unchanged(
    before: list[dict[str, Any]], after: list[dict[str, Any]], member_name: str
) -> bool:
    current = (member_name or "").strip()
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
        name = str(card.get("name") or "").strip()
        name_key = name.lower()
        if not name or _address_member_names_match(name, current):
            continue
        if name_key not in previous:
            continue
        now = (
            str(card.get("home") or ""),
            str(card.get("mailing") or ""),
            bool(card.get("no_home")),
            bool(card.get("no_mailing")),
        )
        if now != previous[name_key]:
            logger.info(
                "address_other_member_diff member=%r before=%s after=%s",
                name,
                previous[name_key],
                now,
            )
            return False
    return True


async def _resolve_spoken_address_suggestion(
    page: Page,
    member_name: str,
    field: str,
    street: str,
    *,
    clear_first: bool = True,
) -> dict[str, Any]:
    """Type into Places and match one unique dropdown row. Never pick by position."""
    street = _clean_spoken_street(street)
    primary_field = "mailing" if field == "mailing" else "home"
    opened = await _open_member_address_editor(page, member_name)
    if not opened.get("ok"):
        reason = str(opened.get("reason") or "address_editor_failed")
        return {"ok": False, "error": "address_editor_failed", "reason": reason}
    # Prefer matching against an already-open dropdown (unique cue like Bridge).
    suggestions = await _visible_address_suggestions(page)
    matches = _suggestion_matches(street, suggestions) if suggestions else []
    if len(matches) != 1:
        if clear_first and field in ("home", "mailing", "both"):
            await _clear_member_address_selection(page, member_name, field)
            await page.wait_for_timeout(150)
            again = await _clear_member_address_selection(page, member_name, field)
            if any(again.values()):
                await page.wait_for_timeout(100)
        written = await _type_address_for_places_dropdown(
            page, member_name, primary_field, street
        )
        if not written:
            return {"ok": False, "error": "address_fill_failed", "suggestions": []}
        matches = []
        suggestions = []
        for attempt in range(10):
            await page.wait_for_timeout(250)
            suggestions = await _visible_address_suggestions(page)
            matches = _suggestion_matches(street, suggestions)
            if len(matches) == 1:
                break
            if suggestions and attempt >= 3:
                break
    if len(matches) > 1:
        return {
            "ok": False,
            "error": "address_ambiguous",
            "suggestions": matches[:5] or suggestions[:5],
            "reason": "multiple_suggestions",
            "street": street,
        }
    if len(matches) != 1:
        return {
            "ok": False,
            "error": "address_no_suggestion",
            "reason": "suggestion_missing",
            "suggestions": suggestions[:5],
            "street": street,
        }
    chosen = matches[0]
    # Select the Places row now so confirm shows a committed chip, not free text.
    if not await _click_address_suggestion(page, chosen):
        logger.warning(
            "address_resolve_suggestion_click_failed member=%r suggestion=%r",
            member_name,
            chosen,
        )
        return {
            "ok": False,
            "error": "address_no_suggestion",
            "reason": "suggestion_click_failed",
            "suggestions": suggestions[:5],
            "street": street,
        }
    # Let Pointcare clear the red "enter and select" warning after the click.
    for _ in range(6):
        if not await _address_validation_visible(page, member_name):
            break
        await page.wait_for_timeout(150)
    if not await _address_places_selection_committed(
        page, member_name, primary_field, street, chosen
    ):
        # One retry: re-open dropdown and click again.
        await _type_address_for_places_dropdown(
            page, member_name, primary_field, street
        )
        await page.wait_for_timeout(400)
        again = await _visible_address_suggestions(page)
        rematch = _suggestion_matches(street, again) or (
            [chosen] if any(
                _normalize_address_suggestion_text(row).lower()
                == _normalize_address_suggestion_text(chosen).lower()
                for row in again
            )
            else []
        )
        if len(rematch) != 1 or not await _click_address_suggestion(page, rematch[0]):
            return {
                "ok": False,
                "error": "address_no_suggestion",
                "reason": "suggestion_not_committed",
                "suggestions": again[:5] or suggestions[:5],
                "street": street,
            }
        chosen = rematch[0]
        if not await _address_places_selection_committed(
            page, member_name, primary_field, street, chosen
        ):
            return {
                "ok": False,
                "error": "address_no_suggestion",
                "reason": "suggestion_not_committed",
                "suggestions": again[:5] or suggestions[:5],
                "street": street,
            }
    return {
        "ok": True,
        "chosen": chosen,
        "suggestions": suggestions,
        "street": street,
        "field": primary_field,
    }


async def _commit_member_address(
    page: Page,
    member_name: str,
    field: str,
    street: str,
    unit: str = "",
    *,
    chosen: str = "",
) -> dict[str, Any]:
    """Fill one member's address from a unique Places row, then Save and re-read."""
    before = await _scrape_address_cards(page)
    opened = await _open_member_address_editor(page, member_name)
    if not opened.get("ok"):
        reason = str(opened.get("reason") or "address_editor_failed")
        logger.warning(
            "address_editor_not_opened member=%r reason=%s",
            member_name,
            reason,
        )
        return {"ok": False, "error": "address_editor_failed", "reason": reason}
    street = _clean_spoken_street(street)
    primary_field = "mailing" if field == "mailing" else "home"
    chosen_hint = _normalize_address_suggestion_text(chosen).strip()
    type_query = street or chosen_hint
    number_match = re.search(r"\b\d+\b", type_query or "")
    number = number_match.group(0) if number_match else ""
    cleared: dict[str, Any] = {}
    _content_skip = {
        "the",
        "and",
        "usa",
        "one",
        "second",
        "please",
        "just",
        "its",
        "for",
        "new",
        "use",
        "that",
        "this",
        "with",
        "from",
        "address",
        "home",
        "mailing",
    }
    content_words = [
        word.lower()
        for word in re.findall(r"[A-Za-z]{3,}", type_query or "")
        if word.lower() not in _ADDRESS_STREET_TYPES
        and word.lower() not in _content_skip
        and word.lower() not in _ADDRESS_DIRECTIONALS
    ][:3]
    suggestions: list[str] = []
    matches: list[str] = []
    already_selected = False
    # If resolve already selected the Places chip, do not clear it on Yes→Save.
    if chosen_hint and await _address_places_selection_committed(
        page, member_name, primary_field, type_query, chosen_hint
    ):
        matches = [chosen_hint]
        already_selected = True
        logger.info(
            "address_places_already_selected member=%r field=%s chosen=%r",
            member_name,
            primary_field,
            chosen_hint,
        )

    def _row_matches_chosen(row: str, want: str) -> bool:
        left = _normalize_address_suggestion_text(row).lower()
        right = _normalize_address_suggestion_text(want).lower()
        if not left or not right:
            return False
        return left == right or right in left or left in right

    # If the matched row is still visible, click it without retyping.
    visible_now = await _visible_address_suggestions(page)
    if len(matches) != 1 and chosen_hint and any(
        _row_matches_chosen(row, chosen_hint) for row in visible_now
    ):
        preferred = [row for row in visible_now if _row_matches_chosen(row, chosen_hint)]
        if len(preferred) == 1:
            matches = preferred
            suggestions = visible_now
    if len(matches) != 1:
        # Clear the existing selected address via the field's circular X before replace.
        cleared = await _clear_member_address_selection(page, member_name, field)
        await page.wait_for_timeout(200)
        # Second pass in case the Places chip re-rendered after the first clear.
        if field in ("home", "mailing", "both"):
            again = await _clear_member_address_selection(page, member_name, field)
            for key, value in again.items():
                if value:
                    cleared[key] = True
            await page.wait_for_timeout(100)
        # Places autocomplete only opens after real typing into the location field.
        written = await _type_address_for_places_dropdown(
            page, member_name, primary_field, type_query
        )
        if not written:
            logger.warning("address_fill_failed")
            return {"ok": False, "error": "address_fill_failed"}
        target_value = (
            written.get("mailing") if primary_field == "mailing" else written.get("home")
        )
        if number and number not in str(target_value or ""):
            logger.warning("address_fill_not_in_field")
            return {"ok": False, "error": "address_fill_failed"}
        if (
            not number
            and content_words
            and not any(
                word in str(target_value or "").lower() for word in content_words[:1]
            )
        ):
            logger.warning(
                "address_fill_not_in_field street=%r value=%r field=%s",
                type_query,
                target_value,
                primary_field,
            )
            return {"ok": False, "error": "address_fill_failed"}
        for attempt in range(10):
            await page.wait_for_timeout(250)
            suggestions = await _visible_address_suggestions(page)
            if chosen_hint:
                preferred = [
                    row for row in suggestions if _row_matches_chosen(row, chosen_hint)
                ]
                if len(preferred) == 1:
                    matches = preferred
                    break
            matches = _suggestion_matches(street or chosen_hint, suggestions)
            if len(matches) == 1:
                break
            # Once the dropdown has rows, stop early so we can read them back if needed.
            if suggestions and attempt >= 3:
                break
        if len(matches) > 1:
            logger.warning("address_suggestion_ambiguous count=%s", len(matches))
            return {
                "ok": False,
                "error": "address_ambiguous",
                "suggestions": matches[:5] or suggestions[:5],
                "reason": "multiple_suggestions",
            }
        if len(matches) != 1:
            logger.warning(
                "address_suggestion_missing street=%r suggestions=%s",
                street,
                suggestions[:5],
            )
            return {
                "ok": False,
                "error": "address_no_suggestion",
                "reason": "suggestion_missing",
                "suggestions": suggestions[:5],
            }
    if unit:
        if field == "mailing":
            await _fill_member_address_inputs(page, member_name, mailing_unit=unit)
        else:
            await _fill_member_address_inputs(page, member_name, home_unit=unit)
    chosen = matches[0]
    if not already_selected:
        if not await _click_address_suggestion(page, chosen):
            logger.warning("address_suggestion_click_failed suggestion=%r", chosen)
            return {
                "ok": False,
                "error": "address_no_suggestion",
                "reason": "suggestion_click_failed",
                "suggestions": suggestions[:5],
            }
        if not await _address_places_selection_committed(
            page, member_name, primary_field, type_query, chosen
        ):
            logger.warning("address_suggestion_not_committed suggestion=%r", chosen)
            return {
                "ok": False,
                "error": "address_no_suggestion",
                "reason": "suggestion_not_committed",
                "suggestions": suggestions[:5],
            }
    if await _address_validation_visible(page, member_name):
        logger.warning(
            "address_validation_before_save_retry member=%r field=%s chosen=%r",
            member_name,
            primary_field,
            chosen,
        )
        # Stale red warning after picking a household-shared address — re-select.
        await _clear_member_address_selection(page, member_name, primary_field)
        await page.wait_for_timeout(150)
        rewritten = await _type_address_for_places_dropdown(
            page, member_name, primary_field, chosen or type_query
        )
        if rewritten:
            for _ in range(8):
                await page.wait_for_timeout(250)
                rows_now = await _visible_address_suggestions(page)
                rematch = _suggestion_matches(street or chosen or type_query, rows_now)
                if chosen_hint:
                    preferred = [
                        row
                        for row in rows_now
                        if _row_matches_chosen(row, chosen_hint)
                    ]
                    if len(preferred) == 1:
                        rematch = preferred
                if len(rematch) == 1:
                    await _click_address_suggestion(page, rematch[0])
                    chosen = rematch[0]
                    already_selected = await _address_places_selection_committed(
                        page, member_name, primary_field, type_query, chosen
                    )
                    break
        if await _address_validation_visible(page, member_name):
            logger.warning(
                "address_validation_before_save member=%r field=%s chosen=%r",
                member_name,
                primary_field,
                chosen,
            )
            return {
                "ok": False,
                "error": "address_save_failed",
                "reason": "validation_before_save",
                "suggestions": suggestions[:5],
            }
    checked = await _fill_member_address_inputs(page, member_name)
    selected = (
        checked.get("mailing") if primary_field == "mailing" else checked.get("home")
    )
    if field == "both":
        mailing_now = str(checked.get("mailing") or "")
        needs_mail_fill = False
        selected_text = str(selected or street or chosen)
        if number and number not in mailing_now and "same" not in mailing_now.lower():
            needs_mail_fill = True
        elif (
            not number
            and content_words
            and "same" not in mailing_now.lower()
            and not any(word in mailing_now.lower() for word in content_words[:1])
        ):
            needs_mail_fill = True
        if needs_mail_fill:
            await _type_address_for_places_dropdown(
                page, member_name, "mailing", selected_text
            )
            await page.wait_for_timeout(300)
            more: list[str] = []
            mail_suggestions: list[str] = []
            for _ in range(8):
                mail_suggestions = await _visible_address_suggestions(page)
                more = _suggestion_matches(street or chosen, mail_suggestions)
                if more:
                    break
                await page.wait_for_timeout(200)
            if len(more) == 1:
                await _click_address_suggestion(page, more[0])
                await _address_places_selection_committed(
                    page, member_name, "mailing", selected_text, more[0]
                )
            elif len(more) > 1:
                logger.warning("address_suggestion_ambiguous")
                return {
                    "ok": False,
                    "error": "address_ambiguous",
                    "suggestions": more[:5],
                    "reason": "multiple_suggestions",
                }
            elif mail_suggestions:
                return {
                    "ok": False,
                    "error": "address_no_suggestion",
                    "reason": "suggestion_missing",
                    "suggestions": mail_suggestions[:5],
                }
    async def _save_and_read_share() -> dict[str, Any]:
        if not await _click_member_address_action(page, member_name, "save"):
            return {"ok": False, "reason": "save_click_failed"}
        await page.wait_for_timeout(450)
        share_info = await _address_share_dialog_info(page)
        if share_info.get("open"):
            return {"ok": True, "share": share_info}
        # Shared-address overlays can lag a beat after Save.
        for _ in range(4):
            await page.wait_for_timeout(200)
            share_info = await _address_share_dialog_info(page)
            if share_info.get("open"):
                return {"ok": True, "share": share_info}
        return {
            "ok": True,
            "share": share_info,
            "validation": await _address_validation_visible(page, member_name),
        }

    async def _force_reselect_places() -> bool:
        """Re-type and re-click Places — needed when the address is already used."""
        await _clear_member_address_selection(page, member_name, primary_field)
        await page.wait_for_timeout(150)
        query = chosen or type_query
        written = await _type_address_for_places_dropdown(
            page, member_name, primary_field, query
        )
        if not written:
            return False
        rows: list[str] = []
        hit: list[str] = []
        for _ in range(10):
            await page.wait_for_timeout(250)
            rows = await _visible_address_suggestions(page)
            hit = _suggestion_matches(street or chosen or query, rows)
            if chosen_hint:
                preferred = [
                    row for row in rows if _row_matches_chosen(row, chosen_hint)
                ]
                if len(preferred) == 1:
                    hit = preferred
            if len(hit) == 1:
                break
            if rows and _ == 4:
                break
        if len(hit) != 1:
            return False
        if not await _click_address_suggestion(page, hit[0]):
            return False
        return await _address_places_selection_committed(
            page, member_name, primary_field, query, hit[0]
        )

    saved = await _save_and_read_share()
    if not saved.get("ok") and saved.get("reason") == "save_click_failed":
        logger.warning("address_save_click_failed")
        return {"ok": False, "error": "address_save_failed", "reason": "save_click_failed"}
    share = saved.get("share") if isinstance(saved.get("share"), dict) else {}
    if share.get("open"):
        # Caller must ask Yes/No, then dismiss the popup before verifying the card.
        return {
            "ok": True,
            "error": "",
            "share_dialog": share,
            "card": None,
            "reason": "share_dialog",
            "cleared": cleared,
            "member_name": member_name,
            "field": field,
            "street": street,
            "before": before,
        }
    if saved.get("validation") or await _address_validation_visible(page, member_name):
        # Reused household addresses often need a fresh Places click before Save
        # will stick (otherwise Pointcare keeps the red select warning).
        logger.warning(
            "address_validation_after_save_retry member=%r field=%s chosen=%r",
            member_name,
            primary_field,
            chosen,
        )
        if await _force_reselect_places():
            saved = await _save_and_read_share()
            share = saved.get("share") if isinstance(saved.get("share"), dict) else {}
            if share.get("open"):
                return {
                    "ok": True,
                    "error": "",
                    "share_dialog": share,
                    "card": None,
                    "reason": "share_dialog",
                    "cleared": cleared,
                    "member_name": member_name,
                    "field": field,
                    "street": street,
                    "before": before,
                }
            if not (
                saved.get("validation")
                or await _address_validation_visible(page, member_name)
            ):
                # Fall through to card verification below.
                pass
            else:
                logger.warning("address_validation_still_visible")
                return {
                    "ok": False,
                    "error": "address_save_failed",
                    "reason": "validation_visible",
                }
        else:
            logger.warning("address_validation_still_visible")
            return {
                "ok": False,
                "error": "address_save_failed",
                "reason": "validation_visible",
            }
    after = await _scrape_address_cards(page)
    if not _other_addresses_unchanged(before, after, member_name):
        logger.warning("address_other_member_changed")
        return {"ok": False, "error": "address_wrong_member", "reason": "other_member_changed"}
    card = _card_by_name(after, member_name)
    if card is None:
        logger.warning("address_card_missing_after_save")
        return {"ok": False, "error": "address_not_verified", "reason": "card_missing"}
    # Prefer the field we just edited; fall back to the whole card text.
    if field == "mailing":
        focus_blob = " ".join(
            [
                str(card.get("mailing") or ""),
                str(card.get("mailing_unit") or ""),
                str(card.get("home") or ""),
            ]
        ).lower()
    elif field == "home":
        focus_blob = " ".join(
            [
                str(card.get("home") or ""),
                str(card.get("home_unit") or ""),
                str(card.get("mailing") or ""),
            ]
        ).lower()
    else:
        focus_blob = " ".join(
            [
                str(card.get("home") or ""),
                str(card.get("mailing") or ""),
                str(card.get("home_unit") or ""),
                str(card.get("mailing_unit") or ""),
            ]
        ).lower()
    logger.info(
        "address_saved_card member=%r field=%s home=%r mailing=%r needs_home=%s needs_mailing=%s",
        member_name,
        field,
        card.get("home"),
        card.get("mailing"),
        int(bool(card.get("needs_home"))),
        int(bool(card.get("needs_mailing"))),
    )
    if number and number not in focus_blob and not card.get("home_same") and not card.get("mailing_same"):
        logger.warning("address_not_on_card blob=%r", focus_blob)
        return {"ok": False, "error": "address_not_verified", "reason": "value_mismatch"}
    if (
        not number
        and content_words
        and not card.get("home_same")
        and not card.get("mailing_same")
        and not any(
            any(
                _address_token_close(word, token)
                for token in re.findall(r"[a-z]{3,}", focus_blob)
            )
            for word in content_words[:2]
        )
    ):
        logger.warning(
            "address_not_on_card street=%r content=%s blob=%r",
            street,
            content_words,
            focus_blob,
        )
        return {"ok": False, "error": "address_not_verified", "reason": "value_mismatch"}
    if field in ("home", "both") and card.get("needs_home"):
        logger.warning("address_home_still_required")
        return {"ok": False, "error": "address_not_verified", "reason": "home_still_required"}
    if field in ("mailing", "both") and card.get("needs_mailing") and not card.get("mailing_same"):
        logger.warning("address_mailing_still_required")
        return {"ok": False, "error": "address_not_verified", "reason": "mailing_still_required"}
    return {
        "ok": True,
        "error": "",
        "card": card,
        "reason": "ok",
        "cleared": cleared,
    }


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
    """JS prelude that returns the member row owning pencil/Add or open MM/DD/YYYY.

    When the segmented editor is open, prefer a container that holds all three
    placeholders (MM + DD + YYYY). The smallest ancestor of a single box (e.g.
    only MM after a month fill) is not a valid editor scope — that caused day
    fills to miss DD after a successful month entry (call_9c52bfa8…).
    """
    return """
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase()
        .replace(/\\./g, '');
      const isNameLine = (line) => {
        let t = (line || '').replace(/\\s+/g, ' ').trim();
        t = t.replace(/\\s+\\b(add|edit|pencil|save|cancel|next|continue)\\b\\.?$/i, '').trim();
        return /^[A-Za-z][A-Za-z .'-]{0,40}$/.test(t) &&
          t.split(' ').length <= 4 &&
          !/^(date of birth|dob|add|edit|save|cancel|next|continue|yes|no|pencil|mm|dd|yyyy)$/i
            .test(t);
      };
      const findCard = (memberName) => {
        const want = norm(memberName);
        if (!want) return null;
        const visible = (el) => {
          const r = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          return r.width > 2 && r.height > 2 &&
            style.display !== 'none' &&
            style.visibility !== 'hidden' &&
            style.opacity !== '0' &&
            !el.closest('[aria-hidden="true"]');
        };
        const ph = (input) => ((input.placeholder || '') + ' ' +
          (input.getAttribute('aria-label') || '')).trim();
        const segmentedCounts = (el) => {
          const inputs = [...el.querySelectorAll(
            'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea'
          )].filter(visible);
          const countPh = (re) => inputs.filter((input) => re.test(ph(input))).length;
          return {
            mm: countPh(/^mm$/i),
            dd: countPh(/^dd$/i),
            yyyy: countPh(/^yyyy$/i),
          };
        };
        const hasCompleteEditor = (el) => {
          const c = segmentedCounts(el);
          return c.mm === 1 && c.dd === 1 && c.yyyy === 1;
        };
        const dobInputs = (el) => [...el.querySelectorAll(
          'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea'
        )].filter((input) => {
          if (!visible(input)) return false;
          const hint = (
            (input.placeholder || '') + ' ' +
            (input.name || '') + ' ' +
            (input.id || '') + ' ' +
            (input.getAttribute('aria-label') || '')
          ).toLowerCase();
          const t = (input.type || '').toLowerCase();
          return t === 'date' ||
            /\\b(mm|dd|yyyy|month|day|year|dob|birth)\\b/.test(hint) ||
            ((input.maxLength > 0 && input.maxLength <= 4) &&
              (t === 'text' || t === 'tel' || t === 'number'));
        });
        const hasDobAction = (el) => {
          if (dobInputs(el).length) return true;
          return [...el.querySelectorAll('button, [role=button], a')].some((control) => {
            if (!visible(control) || control.disabled) return false;
            const label = norm(
              control.innerText ||
              control.textContent ||
              control.getAttribute('aria-label') ||
              control.getAttribute('title') ||
              ''
            );
            const paths = [...control.querySelectorAll('svg path')]
              .map((path) => path.getAttribute('d') || '')
              .join(' ');
            return /^(add|edit|pencil|save)(\\s|$)/i.test(label) ||
              paths.includes('M21.174 6.812') ||
              paths.includes('3.986-3.987');
          });
        };
        const onlyThisMember = (el) => {
          const names = (el.innerText || '')
            .split('\\n')
            .map((s) => s.trim())
            .filter(Boolean)
            .filter(isNameLine)
            .map(norm);
          const uniq = [...new Set(names)];
          return uniq.length === 1 && uniq[0] === want;
        };
        // Prefer walking up from the exact visible name text. Open editors keep
        // Cancel/Save beside the name, so name-only node scans miss the row.
        const leaves = [...document.querySelectorAll('h1,h2,h3,h4,h5,div,span,p,button,a,li')]
          .filter((el) => visible(el) && norm(el.textContent || '') === want);
        let best = null;
        let bestScore = -1;
        let bestArea = Infinity;
        const consider = (el) => {
          if (!el || !visible(el) || !onlyThisMember(el)) return;
          const complete = hasCompleteEditor(el);
          if (!complete && !hasDobAction(el)) return;
          const r = el.getBoundingClientRect();
          if (r.width < 40 || r.height < 20 || r.height > 720) return;
          // Score 2 = full MM/DD/YYYY editor. Score 1 = Add/pencil/partial only.
          // Never prefer a tiny MM-only wrapper over the full editor.
          const score = complete ? 2 : 1;
          const area = r.width * r.height;
          if (score > bestScore || (score === bestScore && area < bestArea)) {
            best = el;
            bestScore = score;
            bestArea = area;
          }
        };
        for (const leaf of leaves) {
          let el = leaf;
          for (let depth = 0; el && depth < 12; depth++, el = el.parentElement) {
            consider(el);
          }
        }
        if (best && bestScore >= 2) return best;
        const nodes = [...document.querySelectorAll(
          'article, section, li, [class*="card"], [class*="Card"], [class*="member"], div'
        )];
        for (const el of nodes) {
          consider(el);
        }
        return best;
      };
    """


async def _member_dob_editor_is_open(page: Page, member_name: str) -> bool:
    """True when that member's card already shows visible MM/DD/YYYY (or date) inputs.

    A saved DOB on the card does not mean the editor is closed — correcting a
    prefilled date keeps the three boxes open after the pencil click.
    """
    try:
        return bool(
            await page.evaluate(
                "(memberName) => {"
                + _member_dob_card_js()
                + """
                  const card = findCard(memberName);
                  if (!card) return false;
                  const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 2 && r.height > 2 && !el.disabled;
                  };
                  return [...card.querySelectorAll(
                    'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea'
                  )].some((el) => {
                    if (!visible(el)) return false;
                    const hint = (
                      (el.placeholder || '') + ' ' +
                      (el.name || '') + ' ' +
                      (el.id || '') + ' ' +
                      (el.getAttribute('aria-label') || '')
                    ).toLowerCase();
                    return /\\b(mm|dd|yyyy|month|day|year|dob|birth)\\b/.test(hint) ||
                      (el.type || '').toLowerCase() === 'date';
                  });
                }""",
                member_name,
            )
        )
    except Exception:
        logger.exception("member_dob_editor_is_open failed member=%r", member_name)
        return False


async def _click_member_add_dob(
    page: Page, member_name: str, edit: bool = False
) -> bool:
    """Open one member's DOB editor using Add or its icon-only pencil button."""
    try:
        if await _member_dob_editor_is_open(page, member_name):
            logger.info(
                "dob_member_action ok=1 member=%r action=none reason=already_open",
                member_name,
            )
            return True
        located = await page.evaluate(
            "({ memberName, edit }) => {"
            + _member_dob_card_js()
            + """
              for (const el of document.querySelectorAll('[data-pc-dob-action]')) {
                el.removeAttribute('data-pc-dob-action');
              }
              const card = findCard(memberName);
              if (!card) return { ok: false, reason: 'member_not_found' };
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                return r.width > 2 && r.height > 2 && !el.disabled;
              };
              const editorOpen = [...card.querySelectorAll(
                'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea'
              )].some((el) => {
                if (!visible(el)) return false;
                const hint = (
                  (el.placeholder || '') + ' ' +
                  (el.name || '') + ' ' +
                  (el.id || '') + ' ' +
                  (el.getAttribute('aria-label') || '')
                ).toLowerCase();
                return /\\b(mm|dd|yyyy|month|day|year|dob|birth)\\b/.test(hint) ||
                  (el.type || '').toLowerCase() === 'date';
              });
              if (editorOpen) {
                return { ok: true, reason: 'already_open', action: 'none' };
              }
              const buttons = [...card.querySelectorAll('button, [role=button], a')];
              const wantEdit = !!edit;
              const hits = [];
              for (const btn of buttons) {
                const br = btn.getBoundingClientRect();
                if (br.width < 8 || br.height < 8) continue;
                const label = norm(btn.innerText || btn.textContent || btn.getAttribute('aria-label') || '');
                const svg = btn.querySelector('svg');
                const paths = svg
                  ? [...svg.querySelectorAll('path')].map((p) => p.getAttribute('d') || '').join(' ')
                  : '';
                // Pointcare's pencil button is icon-only with no aria-label.
                const isPencil = !!svg && (
                  paths.includes('M21.174 6.812') ||
                  paths.includes('3.986-3.987') ||
                  (!label && buttons.filter((b) => {
                    const r = b.getBoundingClientRect();
                    return r.width >= 28 && r.width <= 44 &&
                      r.height >= 28 && r.height <= 44 && !!b.querySelector('svg');
                  }).length === 1)
                );
                const isEdit =
                  label === 'edit' ||
                  label.startsWith('edit ') ||
                  label === 'pencil' ||
                  isPencil;
                const isAdd = label === 'add' || label.startsWith('add ') || label.includes('add date');
                if (wantEdit ? isEdit : isAdd) {
                  hits.push(btn);
                }
              }
              if (hits.length !== 1) {
                return {
                  ok: false,
                  reason: hits.length ? 'ambiguous_action' : 'action_not_found',
                  hit_count: hits.length,
                };
              }
              hits[0].setAttribute('data-pc-dob-action', '1');
              try {
                hits[0].scrollIntoView({ block: 'center', inline: 'nearest' });
              } catch (e) {}
              return {
                ok: true,
                reason: 'located',
                action: wantEdit ? 'edit' : 'add',
                icon_only: !norm(
                  hits[0].innerText ||
                  hits[0].textContent ||
                  hits[0].getAttribute('aria-label') ||
                  ''
                ),
              };
            }""",
            {"memberName": member_name, "edit": edit},
        )
        if not isinstance(located, dict) or not located.get("ok"):
            logger.info(
                "dob_member_action ok=0 member=%r edit=%s reason=%s hits=%s",
                member_name,
                int(edit),
                (located or {}).get("reason") if isinstance(located, dict) else "locate_failed",
                (located or {}).get("hit_count") if isinstance(located, dict) else None,
            )
            return False
        if located.get("reason") == "already_open":
            return True
        target = page.locator("[data-pc-dob-action='1']").first
        if not await target.count():
            return False
        await target.click(timeout=4000)
        for _ in range(30):
            opened = await page.evaluate(
                "(memberName) => {"
                + _member_dob_card_js()
                + """
                  const card = findCard(memberName);
                  if (!card) return false;
                  return [...card.querySelectorAll(
                    'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea'
                  )].some((el) => {
                    const r = el.getBoundingClientRect();
                    if (r.width < 2 || r.height < 2 || el.disabled) return false;
                    const hint = (
                      (el.placeholder || '') + ' ' +
                      (el.name || '') + ' ' +
                      (el.id || '') + ' ' +
                      (el.getAttribute('aria-label') || '')
                    ).toLowerCase();
                    return /\\b(mm|dd|yyyy|month|day|year|dob|birth)\\b/.test(hint) ||
                      (el.type || '').toLowerCase() === 'date';
                  });
                }""",
                member_name,
            )
            if opened:
                logger.info(
                    "dob_member_action ok=1 member=%r action=%s icon_only=%s",
                    member_name,
                    located.get("action") or "-",
                    int(bool(located.get("icon_only"))),
                )
                return True
            await page.wait_for_timeout(100)
        logger.info(
            "dob_member_action ok=0 member=%r edit=%s reason=editor_not_open",
            member_name,
            int(edit),
        )
        return False
    except Exception:
        logger.exception(
            "click_member_add_dob failed member=%r edit=%s",
            member_name,
            int(edit),
        )
        return False
    finally:
        try:
            await page.evaluate(
                """() => {
                  for (const el of document.querySelectorAll('[data-pc-dob-action]')) {
                    el.removeAttribute('data-pc-dob-action');
                  }
                }"""
            )
        except Exception:
            pass


def _dob_parts(dob: str) -> tuple[str, str, str] | None:
    """Split ``MM/DD/YYYY`` into month, day, year strings."""
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", (dob or "").strip())
    if not m:
        return None
    return f"{int(m.group(1)):02d}", f"{int(m.group(2)):02d}", m.group(3)


def _dob_digits_match(expected: str, actual: str) -> bool:
    want_parts = _dob_parts(expected)
    if want_parts:
        tokens = re.findall(r"\d+", actual or "")
        nums = [int(part) for part in tokens]
        # Accept MM/DD/YYYY even when the UI drops leading zeros (7/7/2007).
        # YYYY must still be a full four-digit year (reject 7/7/007).
        if len(nums) >= 3:
            if len(tokens[2]) != 4:
                return False
            return (
                nums[0] == int(want_parts[0])
                and nums[1] == int(want_parts[1])
                and nums[2] == int(want_parts[2])
            )
        if len(nums) == 1:
            compact = f"{int(want_parts[0]):02d}{int(want_parts[1]):02d}{want_parts[2]}"
            got = f"{nums[0]:08d}" if nums[0] < 10**8 else str(nums[0])
            return got == compact or got.endswith(compact) or compact.endswith(got[-8:])
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


async def _read_segmented_dob_inputs(
    page: Page, member_name: str = ""
) -> tuple[str, str, str] | None:
    """Read MM/DD/YYYY via the same placeholder locators used for filling.

    Never reconstructs the date from generic DOM input order — that caused a
    false ``dob_fill_malformed_before_save`` after a successful segmented fill.
    """
    marked = False
    try:
        scope = page
        if member_name:
            if not await _mark_member_dob_card(page, member_name):
                return None
            marked = True
            scope = page.locator("[data-pc-dob-card='1']").first
            if not await scope.count():
                return None
        mm_box = scope.get_by_placeholder(re.compile(r"^mm$", re.I)).first
        dd_box = scope.get_by_placeholder(re.compile(r"^dd$", re.I)).first
        yyyy_box = scope.get_by_placeholder(re.compile(r"^yyyy$", re.I)).first
        if not await mm_box.count() or not await dd_box.count() or not await yyyy_box.count():
            return None
        return (
            (await mm_box.input_value()).strip(),
            (await dd_box.input_value()).strip(),
            (await yyyy_box.input_value()).strip(),
        )
    except Exception:
        return None
    finally:
        if marked:
            try:
                await page.evaluate(
                    """() => {
                      for (const el of document.querySelectorAll('[data-pc-dob-card]')) {
                        el.removeAttribute('data-pc-dob-card');
                      }
                    }"""
                )
            except Exception:
                pass


async def _read_dob_editor_value(page: Page, member_name: str = "") -> str:
    """Return the open DOB fields for this member, or the only open editor."""
    segmented = await _read_segmented_dob_inputs(page, member_name)
    if segmented is not None:
        return "/".join(segmented)
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
              // Prefer placeholder-labeled boxes in MM/DD/YYYY order.
              const byPh = (re) => inputs.find((el) => re.test(
                ((el.placeholder || '') + ' ' + (el.getAttribute('aria-label') || '')).trim()
              ));
              const mm = byPh(/^mm$/i);
              const dd = byPh(/^dd$/i);
              const yyyy = byPh(/^yyyy$/i);
              if (mm && dd && yyyy) {
                return [mm.value || '', dd.value || '', yyyy.value || ''].join('/');
              }
              const dateLike = inputs.filter((el) => {
                const t = (el.type || '').toLowerCase();
                const ph = ((el.placeholder || '') + ' ' + (el.name || '') + ' ' +
                  (el.id || '') + ' ' + (el.getAttribute('aria-label') || ''))
                  .toLowerCase();
                return t === 'date' || /birth|dob|month|day|year|yyyy|\\bmm\\b|\\bdd\\b/.test(ph);
              });
              const pool = dateLike.length ? dateLike : inputs.slice(0, 3);
              return pool.map((el) => el.value || '').join('/');
            }""",
            member_name,
        )
        return str(raw or "")
    except Exception:
        return ""


async def _mark_member_dob_card(page: Page, member_name: str) -> bool:
    """Tag the matched member DOB row so Playwright can scope fills to it."""
    ok, _reason = await _mark_member_dob_card_detailed(page, member_name)
    return ok


async def _mark_member_dob_card_detailed(
    page: Page, member_name: str
) -> tuple[bool, str]:
    """Tag the member editor scope. Prefer a container with MM+DD+YYYY together."""
    try:
        result = await page.evaluate(
            "(memberName) => {"
            + _member_dob_card_js()
            + """
              for (const el of document.querySelectorAll('[data-pc-dob-card]')) {
                el.removeAttribute('data-pc-dob-card');
              }
              const card = findCard(memberName);
              if (!card) return { ok: false, reason: 'member_card_mark_failed' };
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                return r.width > 2 && r.height > 2 && !el.disabled;
              };
              const ph = (input) => ((input.placeholder || '') + ' ' +
                (input.getAttribute('aria-label') || '')).trim();
              const inputs = [...card.querySelectorAll(
                'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea'
              )].filter(visible);
              const countPh = (re) => inputs.filter((input) => re.test(ph(input))).length;
              const mm = countPh(/^mm$/i);
              const dd = countPh(/^dd$/i);
              const yyyy = countPh(/^yyyy$/i);
              // Open editor must expose all three boxes in the scoped card.
              if (mm || dd || yyyy) {
                if (!(mm === 1 && dd === 1 && yyyy === 1)) {
                  return {
                    ok: false,
                    reason: 'member_card_mark_failed',
                    mm, dd, yyyy,
                  };
                }
              }
              card.setAttribute('data-pc-dob-card', '1');
              try { card.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
              return { ok: true, reason: 'marked', mm, dd, yyyy };
            }""",
            member_name,
        )
        if isinstance(result, dict) and result.get("ok"):
            return True, str(result.get("reason") or "marked")
        reason = (
            str(result.get("reason") or "member_card_mark_failed")
            if isinstance(result, dict)
            else "member_card_mark_failed"
        )
        logger.info(
            "dob_card_mark ok=0 member=%r reason=%s",
            member_name or "-",
            reason,
        )
        return False, reason
    except Exception:
        logger.exception("mark_member_dob_card failed member=%r", member_name)
        return False, "member_card_mark_failed"


async def _set_dob_box_value(box: Any, value: str) -> None:
    """Atomic Playwright fill for one DOB box — never per-keystroke type()."""
    await box.click(timeout=3000)
    await box.fill(value)
    current = (await box.input_value()).strip()
    if current != value and current.lstrip("0") != value.lstrip("0"):
        await box.evaluate(
            """(el, next) => {
              el.focus();
              const proto = window.HTMLInputElement.prototype;
              const desc = Object.getOwnPropertyDescriptor(proto, 'value');
              if (desc && desc.set) desc.set.call(el, next);
              else el.value = next;
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            value,
        )


def _dob_component_value_matches(component: str, intended: str, got: str) -> bool:
    want = (intended or "").strip()
    have = (got or "").strip()
    if not want:
        return False
    if component == "year":
        return len(have) == 4 and have == want
    return have == want or have.lstrip("0") == want.lstrip("0")


async def _fill_one_dob_component(
    page: Page, member_name: str, component: str, value: int | str
) -> bool:
    """Fill and verify one member-scoped MM/DD/YYYY box. See ``_detailed`` for reasons."""
    ok, _reason = await _fill_one_dob_component_detailed(
        page, member_name, component, value
    )
    return ok


async def _fill_one_dob_component_detailed(
    page: Page, member_name: str, component: str, value: int | str
) -> tuple[bool, str]:
    """Fill one MM/DD/YYYY box; return ``(ok, reason)`` without logging DOB digits.

    Early-return reasons (call_9c52bfa8… day path):
    ``invalid_component``, ``invalid_value``, ``member_card_mark_failed``,
    ``component_input_not_found``, ``multiple_component_inputs``,
    ``input_not_visible``, ``fill_exception``, ``value_mismatch``.
    """
    placeholders = {
        "month": re.compile(r"^mm$", re.I),
        "day": re.compile(r"^dd$", re.I),
        "year": re.compile(r"^yyyy$", re.I),
    }
    pattern = placeholders.get(component)
    if pattern is None:
        logger.info(
            "dob_component_fill member=%r component=%s ok=0 reason=invalid_component",
            member_name or "-",
            component,
        )
        return False, "invalid_component"
    try:
        number = int(value)
    except (TypeError, ValueError):
        logger.info(
            "dob_component_fill member=%r component=%s ok=0 reason=invalid_value",
            member_name or "-",
            component,
        )
        return False, "invalid_value"
    if component == "year":
        padded = f"{number:04d}"
    else:
        if not (1 <= number <= 31 if component == "day" else 1 <= number <= 12):
            logger.info(
                "dob_component_fill member=%r component=%s ok=0 reason=invalid_value",
                member_name or "-",
                component,
            )
            return False, "invalid_value"
        padded = f"{number:02d}"
    marked = False
    try:
        scope = page
        if member_name:
            marked_ok, mark_reason = await _mark_member_dob_card_detailed(
                page, member_name
            )
            if not marked_ok:
                logger.info(
                    "dob_component_fill member=%r component=%s ok=0 reason=%s",
                    member_name or "-",
                    component,
                    mark_reason,
                )
                return False, mark_reason
            marked = True
            scope = page.locator("[data-pc-dob-card='1']").first
            if not await scope.count():
                logger.info(
                    "dob_component_fill member=%r component=%s ok=0 reason=member_card_mark_failed",
                    member_name or "-",
                    component,
                )
                return False, "member_card_mark_failed"
        matches = scope.get_by_placeholder(pattern)
        match_count = await matches.count()
        if match_count == 0:
            logger.info(
                "dob_component_fill member=%r component=%s ok=0 reason=component_input_not_found",
                member_name or "-",
                component,
            )
            return False, "component_input_not_found"
        if match_count > 1:
            logger.info(
                "dob_component_fill member=%r component=%s ok=0 reason=multiple_component_inputs count=%s",
                member_name or "-",
                component,
                match_count,
            )
            return False, "multiple_component_inputs"
        box = matches.first
        try:
            if not await box.is_visible():
                logger.info(
                    "dob_component_fill member=%r component=%s ok=0 reason=input_not_visible",
                    member_name or "-",
                    component,
                )
                return False, "input_not_visible"
        except Exception:
            logger.info(
                "dob_component_fill member=%r component=%s ok=0 reason=input_not_visible",
                member_name or "-",
                component,
            )
            return False, "input_not_visible"
        await _set_dob_box_value(box, padded)
        await page.wait_for_timeout(100)
        got = (await box.input_value()).strip()
        matched = _dob_component_value_matches(component, padded, got)
        if not matched:
            logger.info(
                "dob_component_fill member=%r component=%s ok=0 reason=value_mismatch",
                member_name or "-",
                component,
            )
            return False, "value_mismatch"
        logger.info(
            "dob_component_fill member=%r component=%s ok=1 reason=ok",
            member_name or "-",
            component,
        )
        return True, "ok"
    except Exception:
        logger.exception(
            "dob_component_fill member=%r component=%s ok=0 reason=fill_exception",
            member_name or "-",
            component,
        )
        return False, "fill_exception"
    finally:
        if marked:
            try:
                await page.evaluate(
                    """() => {
                      for (const el of document.querySelectorAll('[data-pc-dob-card]')) {
                        el.removeAttribute('data-pc-dob-card');
                      }
                    }"""
                )
            except Exception:
                pass


async def _fill_segmented_dob_with_playwright(
    page: Page, dob: str, member_name: str = ""
) -> bool:
    """Fill Pointcare's open MM / DD / YYYY boxes left-to-right.

    Do not use per-keystroke ``type()`` here: Pointcare auto-advances between
    the three boxes, which spilled ``07/07/2007`` into ``7/7/007``.
    """
    parts = _dob_parts(dob)
    if not parts:
        return False
    mm, dd, yyyy = parts
    scope = page
    if member_name:
        if not await _mark_member_dob_card(page, member_name):
            return False
        scope = page.locator("[data-pc-dob-card='1']").first
        if not await scope.count():
            return False
    try:
        mm_box = scope.get_by_placeholder(re.compile(r"^mm$", re.I)).first
        dd_box = scope.get_by_placeholder(re.compile(r"^dd$", re.I)).first
        yyyy_box = scope.get_by_placeholder(re.compile(r"^yyyy$", re.I)).first
        if not await mm_box.count() or not await dd_box.count() or not await yyyy_box.count():
            return False

        await _set_dob_box_value(mm_box, mm)
        await _set_dob_box_value(dd_box, dd)
        await _set_dob_box_value(yyyy_box, yyyy)
        await page.wait_for_timeout(150)
        got_mm = (await mm_box.input_value()).strip()
        got_dd = (await dd_box.input_value()).strip()
        got_yyyy = (await yyyy_box.input_value()).strip()
        matched = _dob_digits_match(dob, f"{got_mm}/{got_dd}/{got_yyyy}")
        logger.info(
            "dob_segmented_fill member=%r ok=%s",
            member_name or "-",
            int(matched),
        )
        return matched
    except Exception:
        logger.exception("fill_segmented_dob_with_playwright failed")
        return False
    finally:
        try:
            await page.evaluate(
                """() => {
                  for (const el of document.querySelectorAll('[data-pc-dob-card]')) {
                    el.removeAttribute('data-pc-dob-card');
                  }
                }"""
            )
        except Exception:
            pass


async def _fill_dob_editor_fields(
    page: Page, dob: str, member_name: str = ""
) -> bool:
    """Fill the open DOB editor using the format Pointcare actually exposes."""
    parts = _dob_parts(dob)
    if not parts:
        return False
    # Live Pointcare uses three boxes: MM, DD, YYYY left-to-right.
    if await _fill_segmented_dob_with_playwright(page, dob, member_name):
        return True
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
                if (/month|^mm$|\\bmm\\b/.test(hint) && !labeled.month) labeled.month = el;
                else if (/\\bday\\b|^dd$|\\bdd\\b/.test(hint) && !labeled.day) labeled.day = el;
                else if (/year|yyyy|^yy$|\\byy\\b/.test(hint) && !labeled.year) labeled.year = el;
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
    """Enter DOB into segmented MM/DD/YYYY boxes, or one masked field as fallback."""
    if await _fill_segmented_dob_with_playwright(page, dob, member_name):
        return True
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

    # Re-read with the same MM/DD/YYYY placeholders used for filling. Do not
    # trust generic DOM order — that false-negatived after ok=1 (7/7/2007).
    segmented = await _read_segmented_dob_inputs(page, member_name)
    if segmented is not None:
        got_before_save = "/".join(segmented)
        if not _dob_digits_match(dob, got_before_save):
            logger.warning(
                "dob_fill_malformed_before_save intended=%s got=%s",
                dob,
                got_before_save,
            )
            # Targeted correction with the same segmented path only.
            if not await _fill_segmented_dob_with_playwright(page, dob, member_name):
                return False
            segmented = await _read_segmented_dob_inputs(page, member_name)
            if segmented is None or not _dob_digits_match(dob, "/".join(segmented)):
                return False
    else:
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
    # Editor should close after a successful Save (MM/DD/YYYY boxes gone).
    still_open = await _read_segmented_dob_inputs(page, member_name)
    if still_open is not None:
        logger.info(
            "dob_save_editor_still_open member=%r values=%s",
            member_name or "-",
            "/".join(still_open),
        )
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

    Prefer the lowest visible control (Pointcare fixed footer). Interactive
    disabled means native ``disabled`` or ``aria-disabled=true`` on the
    element itself. Ancestor classes never count. Exact ``disabled`` /
    ``Mui-disabled`` / ``is-disabled`` class names on the element itself are
    logged; a misleading token inside another class name does not count.
    """
    return r"""
      const MARK = 'data-pc-app-next';
      const nextLabel = (el) => ((el.innerText || el.value || el.textContent || '')
        ).replace(/\s+/g, ' ').trim().toLowerCase();
      const isNextLabel = (text) => text === 'next' || text === 'continue';
      const nativeDisabled = (el) => el.disabled === true;
      const ariaDisabled = (el) =>
        (el.getAttribute('aria-disabled') || '').toLowerCase() === 'true';
      const classDisabled = (el) => {
        try {
          return el.classList.contains('disabled')
            || el.classList.contains('Mui-disabled')
            || el.classList.contains('is-disabled');
        } catch (err) {
          return false;
        }
      };
      // Interactive lock for navigation. Do not treat unrecognized class
      // substrings as disabled — that falsely blocked the yellow footer Next.
      const selfDisabled = (el) => nativeDisabled(el) || ariaDisabled(el);
      const isHidden = (el) => {
        const r = el.getBoundingClientRect();
        if (r.width < 4 || r.height < 4) return true;
        const style = window.getComputedStyle(el);
        if (style.visibility === 'hidden' || style.display === 'none') return true;
        if ((el.getAttribute('aria-hidden') || '').toLowerCase() === 'true') return true;
        if (style.pointerEvents === 'none' && selfDisabled(el)) return true;
        return false;
      };
      const candidateNodes = () => [...document.querySelectorAll(
        'button, [role=button], a, input[type=button], input[type=submit]'
      )];
      const describe = (el, hidden) => ({
        tag: (el.tagName || '').toLowerCase(),
        hidden: !!hidden,
        disabled: selfDisabled(el),
        native_disabled: nativeDisabled(el),
        aria_disabled: (el.getAttribute('aria-disabled') || '').toLowerCase(),
        class_disabled: classDisabled(el),
        bottom: el.getBoundingClientRect().bottom,
        class_name: String(
          typeof el.className === 'string'
            ? el.className
            : ((el.getAttribute && el.getAttribute('class')) || '')
        ).slice(0, 120),
      });
      const nextButtons = () => {
        const found = [];
        for (const el of candidateNodes()) {
          if (!isNextLabel(nextLabel(el))) continue;
          if (isHidden(el)) continue;
          found.push({
            el,
            bottom: el.getBoundingClientRect().bottom,
            disabled: selfDisabled(el),
          });
        }
        return found;
      };
      const pickFooter = (items) => {
        if (!items.length) return null;
        return items.slice().sort((a, b) => b.bottom - a.bottom)[0];
      };
      const inspectNext = () => {
        document.querySelectorAll('[' + MARK + ']').forEach((el) => el.removeAttribute(MARK));
        const visible = [];
        let total = 0;
        let hidden = 0;
        let visible_disabled = 0;
        let visible_enabled = 0;
        for (const el of candidateNodes()) {
          if (!isNextLabel(nextLabel(el))) continue;
          total += 1;
          const hid = isHidden(el);
          const disabled = selfDisabled(el);
          if (hid) {
            hidden += 1;
            continue;
          }
          if (disabled) visible_disabled += 1;
          else visible_enabled += 1;
          visible.push({ el, bottom: el.getBoundingClientRect().bottom, disabled });
        }
        const footer = pickFooter(visible);
        if (footer && !footer.disabled) {
          footer.el.setAttribute(MARK, '1');
        }
        const sample = footer ? describe(footer.el, false) : null;
        let status = 'next_not_found';
        // Prefer the footer control's own state when one exists.
        if (footer) {
          status = footer.disabled ? 'next_disabled' : 'next_enabled';
        } else if (hidden > 0) {
          status = 'next_hidden';
        }
        if (sample && sample.class_disabled && !sample.native_disabled
            && sample.aria_disabled !== 'true' && status === 'next_enabled') {
          // Visible footer Next looks class-disabled but is interactively on.
          sample.mismatch = 'class_disabled_ignored';
        }
        return {
          status,
          total,
          hidden,
          visible_disabled,
          visible_enabled,
          tag: sample ? sample.tag : '',
          disabled: sample ? sample.disabled : false,
          native_disabled: sample ? sample.native_disabled : false,
          aria_disabled: sample ? sample.aria_disabled : '',
          class_disabled: sample ? sample.class_disabled : false,
          class_name: sample ? sample.class_name : '',
          bottom: sample ? sample.bottom : 0,
          mismatch: sample && sample.mismatch ? sample.mismatch : '',
          hidden_flag: false,
        };
      };
    """


async def _inspect_application_next(page: Page) -> dict[str, Any]:
    """Classify the live Next/Continue control without clicking.

    Prefers the lowest visible control (fixed footer). Status values:
    ``next_enabled``, ``next_disabled``, ``next_hidden``, ``next_not_found``.
    Ancestor disabled classes do not count. Class-name tokens alone do not
    override native/aria interactive state.
    """
    empty = {
        "status": "next_not_found",
        "total": 0,
        "hidden": 0,
        "visible_disabled": 0,
        "visible_enabled": 0,
        "tag": "",
        "disabled": False,
        "native_disabled": False,
        "aria_disabled": "",
        "class_disabled": False,
        "class_name": "",
        "bottom": 0,
        "mismatch": "",
    }
    try:
        raw = await page.evaluate(
            "() => {\n"
            + _application_next_buttons_js()
            + """
              return inspectNext();
            }"""
        )
    except Exception:
        logger.exception("inspect_application_next failed")
        return empty
    if not isinstance(raw, dict):
        return empty
    status = str(raw.get("status") or "next_not_found")
    if status not in {
        "next_enabled",
        "next_disabled",
        "next_hidden",
        "next_not_found",
    }:
        status = "next_not_found"
    return {
        "status": status,
        "total": int(raw.get("total") or 0),
        "hidden": int(raw.get("hidden") or 0),
        "visible_disabled": int(raw.get("visible_disabled") or 0),
        "visible_enabled": int(raw.get("visible_enabled") or 0),
        "tag": str(raw.get("tag") or ""),
        "disabled": bool(raw.get("disabled")),
        "native_disabled": bool(raw.get("native_disabled")),
        "aria_disabled": str(raw.get("aria_disabled") or ""),
        "class_disabled": bool(raw.get("class_disabled")),
        "class_name": str(raw.get("class_name") or "")[:120],
        "bottom": float(raw.get("bottom") or 0),
        "mismatch": str(raw.get("mismatch") or ""),
    }


async def _next_button_enabled(page: Page) -> bool | None:
    """Return True when any visible Next/Continue control is enabled.

    False means every matching visible control is disabled on itself. None
    means no visible Next/Continue control was found (missing or hidden).
    """
    info = await _inspect_application_next(page)
    status = info.get("status")
    if status == "next_enabled":
        return True
    if status == "next_disabled":
        return False
    return None


async def _wait_immigration_next_ready(
    page: Page, *, polls: int = 16, interval_ms: int = 150
) -> dict[str, Any]:
    """Poll the live Next control after immigration radios are verified.

    Returns the final ``_inspect_application_next`` payload. Success is only
    ``status == next_enabled`` from a fresh DOM read — never a fixed delay.
    """
    last: dict[str, Any] = {
        "status": "next_not_found",
        "total": 0,
        "hidden": 0,
        "visible_disabled": 0,
        "visible_enabled": 0,
        "tag": "",
        "disabled": False,
        "native_disabled": False,
        "aria_disabled": "",
        "class_disabled": False,
        "class_name": "",
        "bottom": 0,
        "mismatch": "",
    }
    for _ in range(max(1, polls)):
        last = await _inspect_application_next(page)
        if last.get("status") == "next_enabled":
            if last.get("mismatch"):
                logger.info(
                    "immigration_next_ready mismatch=%s class_disabled=%s class_name=%s",
                    last.get("mismatch"),
                    int(bool(last.get("class_disabled"))),
                    (last.get("class_name") or "-")[:80],
                )
            return last
        await page.wait_for_timeout(interval_ms)
    return last


async def _click_enabled_application_next(page: Page) -> bool:
    """Click the enabled Next/Continue control, preferring the footer one."""
    # Refresh mark on the lowest enabled Next, then use Playwright so framework
    # handlers receive a real click (evaluate el.click() is less reliable).
    info = await _inspect_application_next(page)
    if info.get("status") != "next_enabled":
        logger.info(
            "application_next_click skipped status=%s native_disabled=%s aria_disabled=%s "
            "class_disabled=%s",
            info.get("status"),
            int(bool(info.get("native_disabled"))),
            info.get("aria_disabled") or "-",
            int(bool(info.get("class_disabled"))),
        )
        return False
    try:
        marked = page.locator("[data-pc-app-next='1']").first
        if await marked.count():
            await marked.click(timeout=4000)
            await page.wait_for_timeout(600)
            return True
    except Exception:
        logger.exception("application_next_playwright_click_failed")
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
        session.filled.pop("app_insurance_coverage_phase", None)
        session.filled.pop("app_insurance_current_types", None)
        return
    session.filled["app_insurance_queue"] = json.dumps(names)


def _merge_insurance_queue(session: Any, names: list[str]) -> list[str]:
    """Append unique member names onto the coverage queue (multi-select)."""
    merged: list[str] = []
    for item in _insurance_queue(session) + list(names or []):
        label = str(item or "").strip()
        if not label:
            continue
        if label.lower() in {n.lower() for n in merged}:
            continue
        merged.append(label)
    _set_insurance_queue(session, merged or None)
    return merged


async def _ensure_member_coverage_expanded(page: Page, member_name: str) -> bool:
    """Click the member-name accordion and wait for its plan options.

    Pointcare's member header is a ``button[aria-expanded]`` containing a
    separate ``button[role=checkbox]``. The checkbox is status-only at this
    point and remains false until a plan is selected.
    """
    if await _member_coverage_types_visible(page, member_name):
        logger.info(
            "coverage_member_expand ok=1 reason=already_open member=%r",
            member_name,
        )
        return True

    located = await _locate_coverage_member_expander(page, member_name)
    if not located.get("ok"):
        logger.info(
            "coverage_member_expand ok=0 reason=%s member=%r hits=%s",
            located.get("reason") or "expander_missing",
            member_name,
            located.get("hit_count"),
        )
        await _clear_coverage_expander_marks(page)
        return False
    trigger = page.locator("[data-pc-cov-expand='1']").first
    try:
        if not await trigger.count():
            logger.info(
                "coverage_member_expand ok=0 reason=mark_missing member=%r",
                member_name,
            )
            return False
        await trigger.click(timeout=4000)
    except Exception:
        logger.exception(
            "coverage_member_expand click_failed member=%r", member_name
        )
        await _clear_coverage_expander_marks(page)
        return False

    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        if await _member_coverage_types_visible(page, member_name):
            logger.info(
                "coverage_member_expand ok=1 reason=plan_options_visible member=%r",
                member_name,
            )
            await _clear_coverage_expander_marks(page)
            return True
        await page.wait_for_timeout(150)
    state = await _read_marked_coverage_expanded(page)
    logger.info(
        "coverage_member_expand ok=0 reason=plan_options_not_visible "
        "member=%r trigger_found=%s aria_expanded=%s data_state=%s",
        member_name,
        int(bool(state.get("found"))),
        state.get("aria_expanded") or "-",
        state.get("data_state") or "-",
    )
    await _clear_coverage_expander_marks(page)
    return False


async def _clear_coverage_expander_marks(page: Page) -> None:
    try:
        await page.evaluate(
            """() => {
              for (const el of document.querySelectorAll('[data-pc-cov-expand]')) {
                el.removeAttribute('data-pc-cov-expand');
              }
            }"""
        )
    except Exception:
        pass


async def _locate_coverage_member_expander(
    page: Page, member_name: str
) -> dict[str, Any]:
    """Mark the exact member-name accordion trigger, not its status checkbox."""
    try:
        raw = await page.evaluate(
            r"""({ memberName }) => {
              const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
              const key = (s) => norm(s).toLowerCase()
                .replace(/\./g, ' ').replace(/\s+/g, ' ').trim();
              for (const el of document.querySelectorAll('[data-pc-cov-expand]')) {
                el.removeAttribute('data-pc-cov-expand');
              }
              const want = key(memberName);
              const candidates = [...document.querySelectorAll(
                'button[role=button][aria-expanded][aria-controls]'
              )].filter((el) => key(el.innerText || el.textContent || '') === want);
              if (candidates.length !== 1) {
                return {
                  ok: false,
                  reason: candidates.length ? 'ambiguous_expander' : 'expander_missing',
                  hit_count: candidates.length,
                };
              }
              const trigger = candidates[0];
              trigger.setAttribute('data-pc-cov-expand', '1');
              try {
                trigger.scrollIntoView({ block: 'center', inline: 'nearest' });
              } catch (e) {}
              return {
                ok: true,
                reason: trigger.getAttribute('aria-expanded') === 'true'
                  ? 'already_open'
                  : 'needs_click',
                aria_expanded: trigger.getAttribute('aria-expanded') || '',
                data_state: trigger.getAttribute('data-state') || '',
                controls: trigger.getAttribute('aria-controls') || '',
                tag: String(trigger.tagName || ''),
              };
            }""",
            {"memberName": member_name},
        )
    except Exception:
        logger.exception(
            "locate_coverage_member_expander failed member=%r", member_name
        )
        return {"ok": False, "reason": "locate_failed"}
    return raw if isinstance(raw, dict) else {
        "ok": False,
        "reason": "locate_failed",
    }


async def _read_marked_coverage_expanded(page: Page) -> dict[str, Any]:
    try:
        raw = await page.evaluate(
            """() => {
              const el = document.querySelector('[data-pc-cov-expand="1"]');
              if (!el) return { found: false };
              return {
                found: true,
                aria_expanded: el.getAttribute('aria-expanded') || '',
                data_state: el.getAttribute('data-state') || '',
              };
            }"""
        )
    except Exception:
        return {"found": False}
    return raw if isinstance(raw, dict) else {"found": False}


async def _member_coverage_types_visible(page: Page, member_name: str) -> bool:
    """True when coverage-type options are visible under the named member card."""
    want = _coverage_member_name_key(member_name)
    if not want:
        return False
    try:
        return bool(
            await page.evaluate(
                """({ memberName }) => {
                """
                + _coverage_member_rows_js()
                + """
                  const want = nameKey(memberName);
                  if (!want) return false;
                  const cues = [
                    'medicare', 'medicaid', 'tricare', 'va health',
                    'employer', 'marketplace', 'private insurance', 'what type of coverage',
                  ];
                  const memberRows = rows();
                  const hits = memberRows.filter((row) => nameKey(row.name) === want);
                  if (hits.length !== 1) return false;
                  const memberBox = hits[0].box;
                  let el = memberBox.parentElement;
                  for (let depth = 0; depth < 8 && el && el !== document.body; depth++) {
                    const r = el.getBoundingClientRect();
                    const text = norm(el.innerText || el.textContent || '');
                    const low = text.toLowerCase();
                    const memberCount = memberRows.filter(
                      (row) => el.contains(row.box)
                    ).length;
                    const controls = el.querySelectorAll(
                      'input[type=checkbox], [role=checkbox]'
                    );
                    if (
                      r.width >= 40 &&
                      r.height >= 40 &&
                      text.length <= 1200 &&
                      memberCount === 1 &&
                      controls.length >= 2 &&
                      cues.some((c) => low.includes(c))
                    ) {
                      return true;
                    }
                    el = el.parentElement;
                  }
                  return false;
                }""",
                {"memberName": member_name},
            )
        )
    except Exception:
        return False


def _begin_insurance_coverage_collect(
    session: Any, member_name: str
) -> tuple[str, str]:
    """Switch Collect Mode to coverage-type questions for one expanded member."""
    session.filled["app_insurance_current"] = member_name
    session.filled["app_insurance_coverage_phase"] = "collect"
    _set_insurance_current_types(session, None)
    _set_insurance_other_text(session, None)
    speech = _insurance_coverage_question_for(member_name)
    _set_pending_question(session, "app_insurance_coverage_type", speech)
    return "app_insurance_coverage_type", speech


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
    """Remove change/DOB/address command words, leaving the spoken name."""
    text = (spoken or "").strip()
    text = re.sub(
        r"\b("
        r"change|correct|fix|edit|update|please|"
        r"date of birth|date|birth|birthday|dob|"
        r"home address|mailing address|home|mailing|address|"
        r"data|details|detail|info|information|card|"
        r"for|of|the|to|whose|who|person|member|both"
        r")\b",
        " ",
        text,
        flags=re.I,
    )
    # "Don's mailing" / "Agent T.'s date" → keep the member label only.
    text = re.sub(r"(?<=\w)'s\b", "", text, flags=re.I)
    text = re.sub(r"(?<=\w)’s\b", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" .,!'?")


def _strip_member_speech_fillers(spoken: str) -> str:
    """Drop conversational prefixes so \"I said agent t\" matches Agent T.

    Conservative: only strips leading filler / \"I said|meant|…\" frames, never
    tokens inside the remaining name.
    """
    text = (spoken or "").strip()
    if not text:
        return ""
    # Leading soft fillers: "ok,", "um", "well so"
    text = re.sub(
        r"^(?:(?:ok|okay|alright|um+|uh+|ah+|well|so|please)[,.]?\s+)+",
        "",
        text,
        flags=re.I,
    )
    # "I said …" / "I meant …" / "I want …" (may repeat once)
    text = re.sub(
        r"^(?:i\s+(?:said|meant|mean|mentioned|selected|choose|chose|"
        r"pick|picked|want|wanted|need|needed)\s+)+",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"^(?:it'?s|it is|the name is|name is|that is|that's)\s+",
        "",
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

    cleaned = _strip_member_speech_fillers(spoken)
    cleaned = _strip_dob_command_words(cleaned)
    if not cleaned:
        return []
    # Yes/No and other non-name replies must not look like member labels.
    if re.fullmatch(
        r"(?:yes|no|yep|nope|yeah|nah|correct|incorrect|both|home|mailing|"
        r"cancel|continue|next|skip|none)",
        cleaned,
        flags=re.I,
    ):
        return []
    cleaned = _normalize_spoken_initials(cleaned)
    queries = _split_member_queries(cleaned) or [cleaned]
    out: list[str] = []
    for query in queries:
        label = _title_member_label(_normalize_spoken_initials(query))
        if not label:
            continue
        if re.fullmatch(
            r"(?:Yes|No|Yep|Nope|Yeah|Nah|Correct|Incorrect|Both|Home|Mailing|"
            r"Cancel|Continue|Next|Skip|None)",
            label,
        ):
            continue
        if label not in out:
            out.append(label)
    return out


def _candidate_member_labels(q_norm: str, available: list[str]) -> list[str]:
    """Return unambiguous label matches for one normalized spoken query.

    Priority: exact → first-name+initial → unique first-name. Same first name
    with a different initial (Agent D vs Agent T) never matches.
    """
    from .live_form import _normalize_member_label

    if not q_norm:
        return []
    q_parts = q_norm.split()
    exact: list[str] = []
    initial: list[str] = []
    first_only: list[str] = []
    containment: list[str] = []
    for name in available:
        n_norm = _normalize_member_label(name)
        if not n_norm:
            continue
        n_parts = n_norm.split()
        if q_norm == n_norm:
            exact.append(name)
            continue
        if len(q_parts) >= 2 and len(n_parts) >= 2 and q_parts[0] == n_parts[0]:
            # Same first name: require matching initial; never fall through.
            if q_parts[1][:1] == n_parts[1][:1]:
                initial.append(name)
            continue
        if len(q_parts) == 1:
            if n_parts[:1] == q_parts:
                first_only.append(name)
            elif n_norm.startswith(q_norm + " ") or n_norm.startswith(q_norm):
                first_only.append(name)
            continue
        # Multi-word containment without a conflicting shared first name.
        if q_norm in n_norm or n_norm.startswith(q_norm):
            containment.append(name)
    for group in (exact, initial, first_only, containment):
        uniq = list(dict.fromkeys(group))
        if uniq:
            return uniq
    return []


def _match_named_members(
    spoken: str, available: list[str]
) -> tuple[list[str], str | None]:
    """Match spoken names to available member labels. Clarify on duplicates.

    When ``available`` is empty, returns cleaned spoken labels so Collect Mode
    can still target \"Change Agent H\" without waiting for a perfect scrape.
    """
    from .live_form import _normalize_member_label, _split_member_queries

    cleaned = _strip_member_speech_fillers(spoken)
    cleaned = _strip_dob_command_words(cleaned)
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
        uniq = _candidate_member_labels(q_norm, available)
        if len(uniq) > 1:
            return [], (
                "I see more than one person matching "
                f"{query}. Please say the full name as shown on the application."
            )
        if len(uniq) == 1 and uniq[0] not in hits:
            hits.append(uniq[0])
    return hits, None


def _begin_dob_collect_for(
    session: Any,
    names: list[str],
    *,
    change_lead: bool = False,
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
    _set_dob_phase(session, "collect")
    # Full restart of month → day → year for a correction / new collect.
    _clear_dob_partial(session)
    _set_dob_component(session, "month")
    speech = _dob_month_question(current)
    if change_lead:
        speech = f"Okay, I'll change that. {speech}"
    _set_pending_question(session, "app_dob_collect", speech)
    return "app_dob_collect", speech


async def _open_dob_editor_for_prompt(
    session: Any,
    page: Page,
    member_name: str,
    cards: list[dict[str, Any]],
) -> bool:
    """Open the matched member's Add/pencil control before asking for the DOB."""
    card = next(
        (
            item
            for item in cards
            if str(item.get("name") or "").strip().lower()
            == member_name.strip().lower()
        ),
        None,
    )
    if card is None:
        logger.info("dob_prompt_editor ok=0 member=%r reason=card_not_found", member_name)
        return False
    edit = bool(str(card.get("dob") or "").strip() and not card.get("needs_add"))
    if edit:
        session.filled["app_dob_correcting"] = member_name
    elif (
        str(session.filled.get("app_dob_correcting") or "").strip().lower()
        == member_name.strip().lower()
    ):
        session.filled.pop("app_dob_correcting", None)
    opened = bool(
        card.get("editor_open")
        or await _click_member_add_dob(page, member_name, edit=edit)
    )
    logger.info(
        "dob_prompt_editor ok=%s member=%r action=%s",
        int(opened),
        member_name,
        "edit" if edit else "add",
    )
    return opened


def _dob_review_ready_speech() -> str:
    return (
        "All dates of birth look correct. Say Yes to continue, or tell me "
        "whose date of birth to change."
    )


async def _resume_dob_collect_speech(
    session: Any, page: Page, cards: list[dict[str, Any]]
) -> tuple[str, str] | None:
    """Resume an in-progress component collect; None if not mid-collect."""
    phase = _dob_phase(session)
    if phase in ("review", "done"):
        return None
    component = str(session.filled.get("app_dob_component") or "").strip()
    current = str(session.filled.get("app_dob_current") or "").strip()
    if not component or not current:
        return None
    _set_dob_phase(session, "collect")
    stored_partial = _dob_partial_state(session)
    same_member = (
        str(stored_partial.get("name") or "").strip().lower() == current.lower()
    )
    if not same_member:
        _clear_dob_partial(session)
    await _open_dob_editor_for_prompt(session, page, current, cards)
    if component == "confirm":
        partial = _dob_partial_state(session)
        try:
            speech = _dob_confirm_question(
                current,
                int(partial["month"]),
                int(partial["day"]),
                int(partial["year"]),
            )
        except (KeyError, TypeError, ValueError):
            _set_dob_component(session, "month")
            speech = _dob_month_question(current)
    elif component == "fix_which":
        speech = _dob_fix_which_question()
    else:
        speech = _dob_component_question(current, component)
    _set_pending_question(session, "app_dob_collect", speech)
    return "app_dob_collect", speech


async def _prepare_dob_collect_speech(
    session: Any, page: Page
) -> tuple[str, str]:
    """Review existing card DOBs first; collect only missing or corrections."""
    cards = await _scrape_dob_cards(page)
    reviewed = _dob_reviewed_names(session)
    # Card scrape can lag after a successful Save (editor still open / date
    # not yet on the card). Never re-collect a member we already confirmed.
    missing = [
        str(card.get("name") or "").strip()
        for card in cards
        if str(card.get("name") or "").strip()
        and (
            card.get("needs_add")
            or not str(card.get("dob") or "").strip()
        )
        and str(card.get("name") or "").strip().lower() not in reviewed
    ]
    present = [
        card
        for card in cards
        if str(card.get("name") or "").strip()
        and not card.get("needs_add")
        and str(card.get("dob") or "").strip()
    ]

    # Mid month→day→year→confirm: keep the working component flow.
    resumed = await _resume_dob_collect_speech(session, page, cards)
    if resumed is not None:
        return resumed

    # Required missing DOBs before reviewing existing ones.
    if missing and not session.filled.get("app_dob_walk_complete"):
        open_missing = [
            str(card.get("name") or "").strip()
            for card in cards
            if card.get("editor_open")
            and str(card.get("name") or "").strip() in missing
        ]
        current = open_missing[0] if open_missing else missing[0]
        _set_dob_queue(session, missing)
        session.filled["app_dob_current"] = current
        _set_dob_phase(session, "collect")
        _clear_dob_partial(session)
        _set_dob_component(session, "month")
        await _open_dob_editor_for_prompt(session, page, current, cards)
        speech = _dob_month_question(current)
        if len(missing) > 1:
            speech = (
                f"I need dates of birth for {len(missing)} people. " + speech
            )
        _set_pending_question(session, "app_dob_collect", speech)
        return "app_dob_collect", speech

    # Review-first: read each saved DOB aloud; never open the editor here.
    to_review = [
        card
        for card in present
        if str(card.get("name") or "").strip().lower() not in reviewed
    ]
    if to_review and not session.filled.get("app_dob_walk_complete"):
        current_name = str(session.filled.get("app_dob_current") or "").strip()
        chosen = None
        if current_name and _dob_phase(session) == "review":
            chosen = _dob_card_by_name(to_review, current_name)
        if chosen is None:
            chosen = to_review[0]
        name = str(chosen.get("name") or "").strip()
        dob = str(chosen.get("dob") or "").strip()
        session.filled["app_dob_current"] = name
        _set_dob_phase(session, "review")
        _clear_dob_component(session)
        _clear_dob_partial(session)
        session.filled.pop("app_dob_correcting", None)
        speech = _dob_review_question(name, dob)
        _set_pending_question(session, "app_dob_review", speech)
        return "app_dob_review", speech

    # All present DOBs reviewed (or none to review) — ready for Next.
    _set_dob_queue(session, None)
    session.filled.pop("app_dob_current", None)
    session.filled.pop("app_dob_correcting", None)
    _clear_dob_component(session)
    _clear_dob_partial(session)
    session.filled["app_dob_walk_complete"] = "1"
    _set_dob_phase(session, "done")
    if present:
        speech = _dob_review_ready_speech()
    else:
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
            session.filled.pop("app_immigration_phase", None)
            session.filled.pop("app_immigration_options_listed", None)
            speech = (
                "This application is on the citizenship and immigration screen. "
                + _immigration_status_question_for(current, session=session)
            )
            _set_pending_question(session, "app_immigration", speech)
            return "app_immigration", speech
        speech = app_question_for_step("app_immigration_who")
        _set_pending_question(session, "app_immigration_who", speech)
        return "app_immigration_who", speech
    if surface == "app_insurance_coverage_type":
        current = (session.filled.get("app_insurance_current") or "").strip()
        queue = _insurance_queue(session)
        phase = (
            str(session.filled.get("app_insurance_coverage_phase") or "")
            .strip()
            .lower()
        )
        if phase == "members_more":
            speech = (
                "Does any other household member have insurance or health "
                "coverage? Say their name, or say No if no one else does."
            )
            _set_pending_question(session, "app_insurance_coverage_type", speech)
            return "app_insurance_coverage_type", speech
        target = current or (queue[0] if queue else "")
        if target:
            known = _insurance_current_types(session)
            if phase == "other_specify":
                session.filled["app_insurance_current"] = target
                speech = _insurance_other_specify_question(target)
                _set_pending_question(session, "app_insurance_coverage_type", speech)
                return "app_insurance_coverage_type", speech
            if phase == "more" and known:
                session.filled["app_insurance_current"] = target
                speech = _insurance_more_plans_question_for(
                    target,
                    _insurance_types_for_speech(
                        known, _insurance_other_text(session)
                    ),
                )
                _set_pending_question(session, "app_insurance_coverage_type", speech)
                return "app_insurance_coverage_type", speech
            return _begin_insurance_coverage_collect(session, target)
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
    if surface == "app_address_review":
        return await _prepare_address_review_speech(session, page)
    if surface == "app_marital_review":
        return await _prepare_marital_review_speech(session, page)
    if surface == "app_childcare_review":
        return await _prepare_childcare_review_speech(session, page)
    if surface == "app_tax":
        from .tax_stage import _prepare_tax_review_speech

        return await _prepare_tax_review_speech(session, page)
    if surface == "app_income":
        from .income_stage import prepare_income_speech

        return await prepare_income_speech(session, page)
    if surface == "app_expenses":
        from .expense_stage import prepare_expense_speech

        return await prepare_expense_speech(session, page)
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


def _immigration_member_name_key(name: str) -> str:
    """Normalize a household card name for immigration matching only."""
    text = re.sub(r"\s+", " ", (name or "").strip().lower())
    return text.rstrip(".").strip()


def _immigration_status_key(label: str) -> str:
    """Normalize status labels so US Citizen matches U.S. Citizen."""
    text = re.sub(r"\s*\(.*?\)\s*", " ", (label or "").lower())
    text = re.sub(r"\s+", " ", text).strip(" .")
    # Collapse U.S. / US punctuation before alias lookup. Consume the trailing
    # period so "u.s. citizen" becomes "us citizen", not "us. citizen".
    text = re.sub(r"\bu\.?\s*s\.?", "us", text)
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
            _immigration_status_options_speech(),
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
    """Compat wrapper — canonical mapper is income_stage.map_income_type."""
    from .income_stage import map_income_type

    return map_income_type(lowered)


def _map_circumstance_labels(
    spoken: str, *, allow_bare_no: bool = False
) -> list[str]:
    """Map spoken Other Statuses answers onto Pointcare condition labels.

    Accepts short / natural phrasing ("Indian", "vet", "needs help", "student")
    and multi-select lists, similar to special-condition matching.

    When ``allow_bare_no`` is True (circumstances question only), a clear bare
    "No" / "Nope" maps to None of the above. Do not use this flag for global
    Yes/No steps.
    """
    text = re.sub(r"[.!?…]+$", "", (spoken or "").strip().lower())
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    mapping = (
        (
            "Needs help with daily activities",
            (
                r"\bdaily activit",
                r"\bdaily help\b",
                r"\bhelp with daily\b",
                r"\bactivities of daily\b",
                r"\bneeds? help\b",
                r"\badl\b",
                r"\bhelp with (?:bathing|dressing|eating|walking)\b",
            ),
        ),
        (
            "Veteran or military member",
            (
                r"\bveteran\b",
                r"\bmilitary(?:\s+(?:member|person|people|service))?\b",
                r"\bvet\b",
                r"\barmy\b",
                r"\bnavy\b",
                r"\bair force\b",
                r"\bmarines?\b",
                r"\barmed forces\b",
                r"\bactive duty\b",
                r"\bserv(?:ed|ice)\b",
            ),
        ),
        (
            "American Indian or Alaska Native",
            (
                r"\bamerican indian\b",
                r"\balaska native\b",
                r"\bnative american\b",
                r"\bindigenous\b",
                r"\bindian\b",
                r"\balaska\b",
                r"\bnative\b",
                r"\btribal\b",
                r"\btribe\b",
                r"\bfirst nations?\b",
            ),
        ),
        (
            "Full-time student (ages 18 – 20)",
            (
                r"\bfull[\s-]?time student\b",
                r"\bstudent(?:s)? (?:ages? )?(?:18|eighteen)",
                r"\bstudent\b",
                r"\bcollege\b",
                r"\buniversity\b",
                r"\bin school\b",
            ),
        ),
        (
            "None of the above",
            (
                r"\bnone of the above\b",
                r"\bnone\b",
                r"\bneither\b",
                r"\bno situations?\b",
                r"\bnothing\b",
                r"\bno(?:t)? (?:any|one) of (?:those|these|them)\b",
            ),
        ),
    )
    hits: list[str] = []
    for label, patterns in mapping:
        if any(re.search(pat, text, re.I) for pat in patterns):
            if label not in hits:
                hits.append(label)
    # Prefer exact canonical labels when the utterance quotes them.
    for label in _CIRCUMSTANCE_LABELS:
        if label.lower() in text and label not in hits:
            hits.append(label)
    # Distinctive token overlap: spoken words that are a subset of a label.
    stop = {"the", "and", "for", "above", "of", "any", "these", "those", "with", "or"}
    distinctive = {
        "daily",
        "activities",
        "help",
        "veteran",
        "military",
        "vet",
        "indian",
        "alaska",
        "native",
        "indigenous",
        "tribal",
        "tribe",
        "student",
        "college",
        "none",
        "neither",
    }
    spoken_words = set(re.findall(r"[a-z0-9]{3,}", text)) - stop
    if spoken_words:
        for label in _CIRCUMSTANCE_LABELS:
            if label in hits:
                continue
            label_words = set(re.findall(r"[a-z0-9]{3,}", label.lower()))
            overlap = spoken_words & label_words & distinctive
            if spoken_words and spoken_words <= label_words and overlap:
                hits.append(label)
    none = [item for item in hits if "none of the above" in item.lower()]
    real = [item for item in hits if "none of the above" not in item.lower()]
    if real and none:
        return real
    if none and not real:
        return none[:1]
    if hits:
        return hits
    # Circumstances-only: bare No means none of the above when nothing else matched.
    if allow_bare_no and re.fullmatch(r"(no|nope|nah)", text):
        return ["None of the above"]
    return []


def _circumstance_person_question(
    condition: str, members: list[dict[str, Any]] | None = None
) -> str:
    names = [
        str(row.get("label") or row.get("name") or "").strip()
        for row in (members or [])
        if str(row.get("label") or row.get("name") or "").strip()
    ]
    who = ", ".join(names[:8]) if names else "the household members on the screen"
    short = condition or "that situation"
    return (
        f"Who does {short} apply to? The household members are {who}. "
        "Say one or more names, then say done when that situation is complete."
    )


def _circumstance_more_speech(session: Any) -> str:
    pairs_raw = session.filled.get("app_circumstance_pairs") or ""
    parts: list[str] = []
    if pairs_raw:
        try:
            data = json.loads(pairs_raw)
        except Exception:
            data = {}
        if isinstance(data, dict):
            for cond, mems in data.items():
                people = " and ".join(str(m) for m in (mems or []) if m)
                if people:
                    parts.append(f"{cond} for {people}")
                else:
                    parts.append(str(cond))
    if parts:
        body = "I have " + "; ".join(parts) + ". "
    else:
        body = "Those situations are marked. "
    return body + "Are there any other situations to add? Please say Yes or No."


def _record_circumstance_pair(session: Any, condition: str, members: list[str]) -> None:
    raw = session.filled.get("app_circumstance_pairs") or "{}"
    try:
        data = json.loads(raw)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    prior = [str(x) for x in (data.get(condition) or []) if str(x).strip()]
    for mem in members:
        name = str(mem or "").strip()
        if name and name not in prior:
            prior.append(name)
    data[condition] = prior
    session.filled["app_circumstance_pairs"] = json.dumps(data)


def _circumstance_target_member(
    text: str, members: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve a spoken member against the open apply-to list."""
    from .live_form import _normalize_member_label

    cards = [
        {
            "index": int(row.get("index") or i),
            "name": str(row.get("label") or row.get("name") or "").strip(),
            "dupIndex": int(row.get("dupIndex") or 0),
            "checked": bool(row.get("checked")),
        }
        for i, row in enumerate(members)
        if str(row.get("label") or row.get("name") or "").strip()
    ]
    spoken_raw = (text or "").strip()
    # Prefer exact visible spelling when duplicates differ only by case.
    exact = [
        card
        for card in cards
        if card["name"] == spoken_raw
        or card["name"].rstrip(".") == spoken_raw.rstrip(".")
    ]
    if len(exact) == 1:
        return exact[0], None
    target, clarify = _marital_target_card(text, cards)
    if target is not None:
        return target, None
    if clarify:
        # If duplicates remain and only one is still unchecked, pick that one.
        want = _normalize_member_label(text or "")
        same = [
            card
            for card in cards
            if _normalize_member_label(str(card.get("name") or "")) == want
            or (
                want
                and _normalize_member_label(str(card.get("name") or "")).startswith(
                    want
                )
            )
        ]
        unchecked = [card for card in same if not card.get("checked")]
        if len(unchecked) == 1:
            return unchecked[0], None
        if len(same) == 1:
            return same[0], None
        # Stable first match when the speaker used the common short form.
        if len(same) >= 1 and want and " " in want:
            return same[0], None
    return None, clarify


async def _continue_income_seq(
    call_id: str,
    session: Any,
    page: Page,
    answer: str,
) -> dict[str, Any]:
    """Compat wrapper for employed/self-employed field sequences."""
    from .income_stage import continue_income_seq

    return await continue_income_seq(call_id, session, page, answer)


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


async def _stage_e_card_state(page: Page, label: str) -> dict[str, Any]:
    """Selected/disabled state for a Stage E choice card, including plain divs."""
    try:
        raw = await page.evaluate(
            """(label) => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const want = norm(label);
              if (!want) return { found: false, selected: false, disabled: false, signature: '' };
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
              if (!hits.length) {
                return { found: false, selected: false, disabled: false, signature: '' };
              }
              const ownClass = (node) => {
                if (typeof node.className === 'string') return node.className;
                return (node.getAttribute && node.getAttribute('class')) || '';
              };
              const tokensOf = (node) => ownClass(node)
                .toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
              const describe = (node, allowActive) => {
                const tokens = tokensOf(node);
                const ariaPressed = (node.getAttribute('aria-pressed') || '').toLowerCase();
                const ariaChecked = (node.getAttribute('aria-checked') || '').toLowerCase();
                const ariaSelected = (node.getAttribute('aria-selected') || '').toLowerCase();
                const dataState = (
                  node.getAttribute('data-state') ||
                  node.getAttribute('data-selected') || ''
                ).toLowerCase();
                const selected = ariaPressed === 'true' || ariaChecked === 'true'
                  || ariaSelected === 'true'
                  || tokens.includes('selected') || tokens.includes('checked')
                  || (allowActive && tokens.includes('active'))
                  || dataState === 'selected' || dataState === 'checked'
                  || dataState === 'active' || dataState === 'true';
                const disabled = node.disabled === true
                  || (node.getAttribute('aria-disabled') || '').toLowerCase() === 'true'
                  || tokens.includes('disabled');
                return {
                  selected, disabled,
                  sig: [ariaPressed, ariaChecked, ariaSelected, dataState, tokens.slice(0, 8).join('.')].join(','),
                };
              };
              const self = hits[0].el;
              const selfInfo = describe(self, true);
              let selected = selfInfo.selected;
              const parts = [selfInfo.sig];
              let node = self.parentElement;
              for (let i = 0; i < 3 && node && node !== document.body; i++) {
                const info = describe(node, false);
                selected = selected || info.selected;
                parts.push(info.sig);
                node = node.parentElement;
              }
              return {
                found: true,
                selected,
                disabled: selfInfo.disabled,
                signature: parts.join('|'),
              };
            }""",
            label,
        )
    except Exception:
        logger.exception("stage_e card state failed")
        return {"found": False, "selected": False, "disabled": False, "signature": ""}
    if not isinstance(raw, dict):
        return {"found": False, "selected": False, "disabled": False, "signature": ""}
    return {
        "found": bool(raw.get("found")),
        "selected": bool(raw.get("selected")),
        "disabled": bool(raw.get("disabled")),
        "signature": str(raw.get("signature") or ""),
    }


async def _yes_no_branch_ready(
    page: Page,
    *,
    yes: bool,
    target: str,
    screen_before: str,
    before_card: dict[str, Any],
    before_signature: str,
) -> bool:
    """True when the card, its branch, or the next screen shows the choice took."""
    from .live_form import _choice_signature

    card = await _stage_e_card_state(page, target)
    if card.get("selected") and not before_card.get("selected"):
        return True
    if (
        card.get("signature")
        and before_card.get("signature")
        and card.get("signature") != before_card.get("signature")
        and card.get("selected")
    ):
        return True
    after_signature = await _choice_signature(page, target)
    if (
        after_signature
        and after_signature != "__missing__"
        and after_signature != before_signature
    ):
        return True
    if not yes and await immigration_member_radios_visible(page):
        return True
    screen = await detect_application_screen(page)
    if (
        screen_before
        and screen
        and screen.startswith("app_")
        and screen != screen_before
    ):
        return True
    if yes and screen_before == "app_insurance":
        members = await _scrape_coverage_members(page)
        if members:
            return True
    if not card.get("found"):
        from .live_form import _visible_choice_labels

        labels = await _visible_choice_labels(page)
        if target not in labels and screen and screen != screen_before:
            return True
    return False


async def _click_yes_no(page: Page, yes: bool) -> str:
    """Click the unique Stage E Yes/No card and wait until that choice is visible."""
    from .live_form import _choice_signature, _visible_choice_labels

    labels = await _visible_choice_labels(page)
    try:
        target = resolve_stage_e_yes_no_label(yes, labels)
    except RuntimeError:
        logger.info(
            "stage_e_yes_no_rejected reason=label_match polarity=%s label_count=%s",
            "yes" if yes else "no",
            len(labels or []),
        )
        raise
    before_card = await _stage_e_card_state(page, target)
    if before_card.get("disabled"):
        logger.info(
            "stage_e_yes_no_rejected reason=self_disabled polarity=%s found=%s",
            "yes" if yes else "no",
            before_card.get("found"),
        )
        raise RuntimeError(
            "YES_NO_CLARIFY: That choice is disabled. "
            "Please say Yes or No again."
        )
    before_signature = await _choice_signature(page, target)
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
    # Cards are often divs, and Pointcare may keep the same screen id while the
    # branch opens or the next page paints. An unchanged id is not a failure
    # until this wait ends with no selected card, branch, or new screen.
    for _ in range(15):
        await page.wait_for_timeout(300)
        if await _yes_no_branch_ready(
            page,
            yes=yes,
            target=target,
            screen_before=screen_before,
            before_card=before_card,
            before_signature=before_signature,
        ):
            return target
    validation = await _visible_form_validation(page)
    if validation:
        raise RuntimeError(
            "YES_NO_CLARIFY: That Yes or No choice did not register. "
            "Please say Yes or No again."
        )
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
    "app_circumstances_more": "app_circumstances",
    "app_income_employed": "app_income",
    "app_income_employed_hours": "app_income",
    "app_income_employed_frequency": "app_income",
    "app_income_employed_employer": "app_income",
    "app_income_self": "app_income",
    "app_income_self_work": "app_income",
    "app_income_amount": "app_income",
    "app_income_unemployed": "app_income",
    "app_income_confirm": "app_income",
    "app_income_more": "app_income",
    "app_expenses_amount": "app_expenses",
    "app_expenses_medical": "app_expenses",
    "app_expenses_confirm": "app_expenses",
    "app_expenses_more": "app_expenses",
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


async def _wait_for_screen_change(
    page: Page,
    leave_step: str,
    *,
    timeout_s: float = 4.0,
) -> str:
    """Poll until detect reports a different ``app_*`` screen than ``leave_step``."""
    prior = (leave_step or "").strip()
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        last = await detect_application_screen(page)
        if (
            last
            and last.startswith("app_")
            and last != "programs"
            and (not prior or last != prior)
        ):
            return last
        await page.wait_for_timeout(300)
    if (
        last
        and last.startswith("app_")
        and last != "programs"
        and (not prior or last != prior)
    ):
        return last
    return ""


async def _advance_after_answer(
    page: Page,
    session: Any,
    *,
    allow_extra_next: bool = True,
    leave_step: str = "",
    expected_next: str = "",
) -> str:
    """Detect the next app step after a verified browser action.

    Polls briefly for a recognizable ``app_*`` screen. An extra Next click is
    only used when ``allow_extra_next`` is true — callers that already clicked
    Next (citizenship / agent Yes) must pass False so insurance is not skipped.

    When ``leave_step`` is set (caller already clicked Next), a blank/unknown
    detect result is returned as ``""`` instead of falling back to
    ``session.app_step``. Falling back caused false "screen still open" after
    Pointcare had already left (address / marital handoff).

    ``expected_next`` is a soft hint (e.g. circumstances → tax): used to log
    progress and to accept a confirmed tax probe during SPA loading, not as
    unconditional proof that navigation succeeded.
    """
    prior = (leave_step or session.app_step or "").strip()
    expected = (expected_next or "").strip()
    deadline = time.monotonic() + 5.5
    screen = ""
    last_probe: dict[str, Any] = {}
    while time.monotonic() < deadline:
        screen = await detect_application_screen(page)
        if expected == "app_tax":
            try:
                from .tax_stage import probe_tax_information_screen

                last_probe = await probe_tax_information_screen(page)
            except Exception:
                last_probe = {}
            if last_probe.get("is_tax"):
                logger.info(
                    "advance_after_answer expected=app_tax confirmed heading=%r "
                    "edits=%s reason=%s",
                    last_probe.get("heading"),
                    last_probe.get("edit_count"),
                    last_probe.get("reason"),
                )
                return "app_tax"
            if last_probe.get("loading"):
                await page.wait_for_timeout(250)
                continue
        if expected == "app_income":
            try:
                from .income_stage import probe_income_screen

                last_probe = await probe_income_screen(page)
            except Exception:
                last_probe = {}
            if last_probe.get("is_income"):
                logger.info(
                    "advance_after_answer expected=app_income confirmed "
                    "heading=%r adds=%s cards=%s reason=%s",
                    last_probe.get("heading"),
                    last_probe.get("add_count"),
                    last_probe.get("card_count"),
                    last_probe.get("reason"),
                )
                return "app_income"
            if last_probe.get("loading"):
                await page.wait_for_timeout(250)
                continue
        if screen == "programs":
            return session.app_step or ""
        if (
            screen
            and screen.startswith("app_")
            and (not prior or screen != prior)
        ):
            return screen
        if screen and screen.startswith("app_") and not prior:
            return screen
        await page.wait_for_timeout(250)
    if expected == "app_tax" and last_probe.get("is_tax"):
        return "app_tax"
    if expected == "app_income" and last_probe.get("is_income"):
        return "app_income"
    if screen and screen.startswith("app_") and screen != prior:
        return screen
    if screen and screen.startswith("app_") and not leave_step:
        return screen
    if allow_extra_next:
        # Page still unrecognized — one recovery Next, never a second automatic
        # click after the caller already advanced.
        try:
            await _click_next_or_continue(page)
        except Exception:
            pass
        await page.wait_for_timeout(700)
        screen = await detect_application_screen(page)
        if expected == "app_tax":
            try:
                from .tax_stage import probe_tax_information_screen

                last_probe = await probe_tax_information_screen(page)
            except Exception:
                last_probe = {}
            if last_probe.get("is_tax"):
                return "app_tax"
        if expected == "app_income":
            try:
                from .income_stage import probe_income_screen

                last_probe = await probe_income_screen(page)
            except Exception:
                last_probe = {}
            if last_probe.get("is_income"):
                return "app_income"
        if screen and screen.startswith("app_") and (not prior or screen != prior):
            return screen
    if leave_step:
        logger.info(
            "advance_after_answer unresolved leave=%r expected=%r detected=%r "
            "probe_reason=%r heading=%r snippet=%r",
            prior,
            expected,
            screen,
            last_probe.get("reason"),
            last_probe.get("heading"),
            (last_probe.get("snippet") or "")[:120],
        )
        # Prior screen → caller treats as stayed. Blank → unknown (not session step).
        if screen and screen.startswith("app_"):
            return screen
        return ""
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

    # Zero-eligible Great News: check MassHealth CommonHealth first so Continue enables.
    # Already-eligible summaries: leave selection alone; Yes alone continues.
    ready = await ensure_programs_ready_to_continue(page)
    session.filled["programs_ready_reason"] = str(ready.get("reason") or "")
    if ready.get("selected"):
        session.filled["programs_medicaid_selected"] = "1"

    clicked = await click_start_applications(page)
    started = await wait_for_application_started(page)
    if not started:
        logger.error(
            "start_applications_not_verified call_id=%s clicked=%s url=%s ready=%s",
            call_id,
            clicked,
            (page.url or "")[:120],
            ready.get("reason"),
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
    next_step = await wait_for_verified_first_stage_e_step(page, timeout_ms=12000)
    if not next_step:
        screen = await detect_application_screen(page)
        if screen in _FIRST_STAGE_E_STEPS:
            next_step = screen
    if not next_step:
        logger.error(
            "stage_e_first_question_missing call_id=%s clicked=%s",
            call_id,
            clicked,
        )
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "eligibility_answer",
            "value": answer,
            "error": "stage_e_first_question_missing",
            "spoken": (
                "ASK: I've opened the application, but the first question is "
                "still loading. Please say next when you're ready."
            ),
            "message": "Application started but first question not verified",
            "step": "programs",
            "phase": "eligibility",
        }
    session.app_step = next_step
    first_question = await _first_stage_e_question_speech(page, session, next_step)
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

_MARITAL_LABELS = (
    "Never Married",
    "Married",
    "Separated",
    "Divorced",
    "Widowed",
    "Registered Domestic Partner",
    "Common Law Marriage",
)


def _marital_dom_js() -> str:
    """DOM helpers for scoped marital cards, including duplicate names."""
    return r"""
      const maritalNorm = (s) => (s || '').replace(/\s+/g, ' ').trim();
      const maritalLow = (s) => maritalNorm(s).toLowerCase().replace(/\.$/, '');
      const maritalLabels = [
        'Never Married', 'Married', 'Separated', 'Divorced', 'Widowed',
        'Registered Domestic Partner', 'Common Law Marriage'
      ];
      const maritalLabel = (s) => {
        const want = maritalLow(s);
        return maritalLabels.find((label) => maritalLow(label) === want) || '';
      };
      const maritalVisible = (el) => {
        if (!el || el.disabled) return false;
        const r = el.getBoundingClientRect();
        if (r.width < 3 || r.height < 3) return false;
        const st = window.getComputedStyle(el);
        return st.display !== 'none' && st.visibility !== 'hidden';
      };
      const maritalButtonLabel = (el) => maritalLow(
        (el && (el.innerText || el.textContent || el.value ||
          el.getAttribute('aria-label') || el.getAttribute('title'))) || ''
      );
      const maritalRadioLabel = (radio) => {
        const own = maritalLabel(
          (radio.getAttribute('aria-label') || '') + ' ' +
          (radio.innerText || radio.textContent || '')
        );
        if (own) return own;
        if (radio.id) {
          const lab = document.querySelector(
            'label[for="' + CSS.escape(radio.id) + '"]'
          );
          if (lab) {
            const found = maritalLabel(lab.innerText || lab.textContent || '');
            if (found) return found;
          }
        }
        const wrap = radio.closest('label');
        if (wrap) {
          const found = maritalLabel(wrap.innerText || wrap.textContent || '');
          if (found) return found;
        }
        let cur = radio.parentElement;
        for (let depth = 0; cur && depth < 4; depth += 1, cur = cur.parentElement) {
          for (const line of (cur.innerText || '').split('\n')) {
            const found = maritalLabel(line);
            if (found) return found;
          }
        }
        return '';
      };
      const maritalIsPencil = (btn) => {
        if (!btn || !maritalVisible(btn)) return false;
        const label = maritalButtonLabel(btn);
        if (label === 'cancel' || label === 'save' || label === 'back' ||
            label === 'next' || label === 'continue') return false;
        if (label === 'edit' || label === 'pencil' || label.startsWith('edit ')) {
          return true;
        }
        const br = btn.getBoundingClientRect();
        const svg = btn.querySelector('svg');
        const paths = svg
          ? [...svg.querySelectorAll('path')].map((p) => p.getAttribute('d') || '').join(' ')
          : '';
        if (paths.includes('M18 6 6 18') || paths.includes('m6 6 12 12')) return false;
        if (paths.includes('M21.174 6.812') || paths.includes('3.986-3.987') ||
            paths.includes('M3 17.25') || paths.includes('m14.304 4.4')) return true;
        // Pointcare marital cards use an icon-only ~36x36 pencil.
        return !label && br.width >= 30 && br.width <= 52 &&
          br.height >= 30 && br.height <= 52;
      };
      const maritalIsName = (line) => {
        const t = maritalNorm(line).replace(/\s*\(you\)\s*/ig, ' ').trim();
        if (!t || maritalLabel(t)) return false;
        if (!/^[A-Za-z][A-Za-z .'-]{0,40}$/.test(t) || t.split(' ').length > 4) {
          return false;
        }
        const n = maritalLow(t);
        if (/marital|status|review|update|current|please|relationship|never married|registered|common law/.test(n)) {
          return false;
        }
        if (/^(cancel|save|back|next|edit|pencil)$/.test(n)) return false;
        return true;
      };
      const maritalNamesIn = (root) => {
        const out = [];
        for (const line of (root.innerText || '').split('\n')) {
          const t = maritalNorm(line);
          if (maritalIsName(t) && !out.some((name) => maritalLow(name) === maritalLow(t))) {
            out.push(t);
          }
        }
        return out;
      };
      const maritalStatusesIn = (root) => {
        const out = [];
        for (const line of (root.innerText || '').split('\n')) {
          const found = maritalLabel(line);
          if (found && !out.includes(found)) out.push(found);
        }
        return out;
      };
      const maritalCardForAnchor = (anchor, expanded) => {
        let cur = anchor;
        let best = null;
        for (let depth = 0; cur && cur !== document.body && depth < 9;
             depth += 1, cur = cur.parentElement) {
          const r = cur.getBoundingClientRect();
          if (r.width < 80 || r.height < 24 || r.height > 700) continue;
          const names = maritalNamesIn(cur);
          if (names.length !== 1) continue;
          const radios = [...cur.querySelectorAll('input[type=radio], [role=radio]')]
            .filter((item) => maritalVisible(item) && Boolean(maritalRadioLabel(item)));
          const statuses = maritalStatusesIn(cur);
          const pencils = [...cur.querySelectorAll('button, [role=button]')]
            .filter(maritalIsPencil);
          if (expanded) {
            if (radios.length < 2) continue;
          } else if (!pencils.includes(anchor) || !statuses.length) {
            continue;
          }
          best = cur;
          if (expanded && radios.length >= 7) break;
          if (!expanded && statuses.length === 1 && pencils.length === 1) break;
        }
        return best;
      };
      const maritalAllCards = () => {
        const roots = [];
        const add = (root) => {
          if (!root || roots.includes(root)) return;
          if (roots.some((other) => other.contains(root))) {
            for (let i = roots.length - 1; i >= 0; i -= 1) {
              if (roots[i].contains(root)) roots.splice(i, 1);
            }
          } else if (roots.some((other) => root.contains(other))) {
            return;
          }
          roots.push(root);
        };
        for (const btn of document.querySelectorAll('button, [role=button]')) {
          if (maritalIsPencil(btn)) add(maritalCardForAnchor(btn, false));
        }
        for (const radio of document.querySelectorAll('input[type=radio], [role=radio]')) {
          if (maritalVisible(radio) && maritalRadioLabel(radio)) {
            add(maritalCardForAnchor(radio, true));
          }
        }
        roots.sort((a, b) => {
          const ar = a.getBoundingClientRect();
          const br = b.getBoundingClientRect();
          return ar.top - br.top || ar.left - br.left;
        });
        return roots.map((root, index) => {
          const names = maritalNamesIn(root);
          const radios = [...root.querySelectorAll('input[type=radio], [role=radio]')]
            .filter((item) => maritalVisible(item) && Boolean(maritalRadioLabel(item)));
          let selected = '';
          for (const radio of radios) {
            if (radio.checked === true || radio.getAttribute('aria-checked') === 'true') {
              selected = maritalRadioLabel(radio);
              break;
            }
          }
          const statuses = maritalStatusesIn(root);
          if (!selected && statuses.length === 1) selected = statuses[0];
          return {
            root,
            index,
            name: names[0] || '',
            status: selected,
            expanded: radios.length > 0,
            options: radios.map(maritalRadioLabel).filter(Boolean),
          };
        }).filter((card) => card.name);
      };
    """


async def _scrape_marital_cards(page: Page) -> list[dict[str, Any]]:
    """Read marital member cards in visual order, preserving duplicate names."""
    try:
        rows = await page.evaluate(
            "() => {"
            + _marital_dom_js()
            + """
              return maritalAllCards().map((card) => ({
                index: card.index,
                name: card.name,
                status: card.status,
                expanded: card.expanded,
                options: card.options,
              }));
            }"""
        )
        if isinstance(rows, list):
            return [
                row
                for row in rows
                if isinstance(row, dict) and str(row.get("name") or "").strip()
            ]
    except Exception:
        logger.exception("scrape_marital_cards failed")
    return []


def _marital_status_from_text(text: str) -> str:
    """Use the same normalization and ASR aliases as eligibility Q1.

    Strip a leading Yes/Yeah so replies like "Yeah, single" still resolve.
    """
    from .answer_parse import validate_for_step
    from .live_form import _choice_phrase

    raw = (text or "").strip()
    cleaned = re.sub(
        r"^(?:yes|yeah|yep|yup|ok|okay)[,.\s]+",
        "",
        raw,
        flags=re.I,
    ).strip() or raw
    for candidate in (cleaned, raw):
        answer, _clarify = validate_for_step("marital", candidate)
        value = str(answer or "").strip()
        if value in _MARITAL_LABELS:
            return value
        mapped = _choice_phrase(candidate)
        if mapped in _MARITAL_LABELS:
            return mapped
    return ""



def _marital_target_card(
    text: str, cards: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve a spoken member, requiring a fuller phrase for duplicate names."""
    from .live_form import _normalize_member_label

    spoken = (text or "").lower()
    normalized_spoken = _normalize_member_label(text or "")
    if not normalized_spoken:
        return None, None
    exact: list[dict[str, Any]] = []
    first_only: list[dict[str, Any]] = []
    for card in cards:
        name = str(card.get("name") or "").strip()
        norm = _normalize_member_label(name)
        if not norm:
            continue
        if norm == normalized_spoken or norm in normalized_spoken:
            exact.append(card)
            continue
        first = norm.split()[0]
        if first and re.search(rf"\b{re.escape(first)}\b", normalized_spoken):
            first_only.append(card)
    candidates = exact or first_only
    if not candidates:
        return None, None
    unique_names = {
        _normalize_member_label(str(card.get("name") or "")) for card in candidates
    }
    if len(candidates) == 1 and (exact or len(unique_names) == 1):
        return candidates[0], None
    ordinal_map = (
        (r"\b(first|1st|number one)\b", 0),
        (r"\b(second|2nd|number two)\b", 1),
        (r"\b(third|3rd|number three)\b", 2),
        (r"\b(fourth|4th|number four)\b", 3),
    )
    for pattern, offset in ordinal_map:
        if re.search(pattern, spoken) and offset < len(candidates):
            return candidates[offset], None
    label = str(candidates[0].get("name") or "that name")
    return None, (
        f"I see {len(candidates)} people named {label}. "
        f"Please say first {label}, second {label}, or their full name as shown."
    )


async def _open_marital_editor(page: Page, index: int) -> dict[str, Any]:
    """Click only the pencil belonging to one visual marital-card index."""
    try:
        result = await page.evaluate(
            "(index) => {"
            + _marital_dom_js()
            + """
              const cards = maritalAllCards();
              const card = cards[index];
              if (!card) return { ok: false, reason: 'card_not_found' };
              if (card.expanded) {
                return { ok: true, reason: 'already_open', name: card.name };
              }
              const hits = [...card.root.querySelectorAll('button, [role=button]')]
                .filter(maritalIsPencil);
              if (hits.length !== 1) {
                return {
                  ok: false,
                  reason: hits.length ? 'ambiguous_pencil' : 'pencil_not_found',
                };
              }
              hits[0].scrollIntoView({ block: 'center', inline: 'nearest' });
              hits[0].click();
              return { ok: true, reason: 'clicked', name: card.name };
            }""",
            index,
        )
        await page.wait_for_timeout(250)
        return result if isinstance(result, dict) else {}
    except Exception:
        logger.exception("open_marital_editor failed")
        return {"ok": False, "reason": "exception"}


async def _click_marital_action(page: Page, index: int, action: str) -> bool:
    """Click Cancel or Save within the indexed marital card."""
    try:
        return bool(
            await page.evaluate(
                "({ index, action }) => {"
                + _marital_dom_js()
                + """
                  const card = maritalAllCards()[index];
                  if (!card) return false;
              const hits = [...card.root.querySelectorAll('button, [role=button]')]
                .filter((btn) => maritalButtonLabel(btn) === action.toLowerCase());
              if (hits.length !== 1 || !maritalVisible(hits[0])) return false;
              const btn = hits[0];
              if (action.toLowerCase() === 'save' &&
                  (btn.disabled || btn.getAttribute('aria-disabled') === 'true')) {
                return false;
              }
              btn.click();
              return true;
                }""",
                {"index": index, "action": action},
            )
        )
    except Exception:
        logger.exception("click_marital_action failed")
        return False


async def _set_member_marital_status(
    page: Page, index: int, status: str
) -> dict[str, Any]:
    """Select a scoped radio. Save only when enabled; otherwise close via Cancel."""
    opened = await _open_marital_editor(page, index)
    if not opened.get("ok"):
        return {
            "ok": False,
            "error": "marital_editor_failed",
            "reason": opened.get("reason"),
        }
    before_cards = await _scrape_marital_cards(page)
    if index >= len(before_cards):
        return {"ok": False, "error": "marital_card_missing"}
    before = before_cards[index]
    try:
        selected = await page.evaluate(
            "({ index, status }) => {"
            + _marital_dom_js()
            + """
              const card = maritalAllCards()[index];
              if (!card || !card.expanded) return { ok: false, reason: 'not_open' };
              const want = maritalLow(status);
              const hits = [...card.root.querySelectorAll('input[type=radio], [role=radio]')]
                .filter((radio) => maritalLow(maritalRadioLabel(radio)) === want);
              if (hits.length !== 1) {
                return {
                  ok: false,
                  reason: hits.length ? 'ambiguous_radio' : 'radio_not_found',
                };
              }
              hits[0].scrollIntoView({ block: 'center', inline: 'nearest' });
              hits[0].click();
              return { ok: true };
            }""",
            {"index": index, "status": status},
        )
    except Exception:
        logger.exception("set_member_marital_status radio failed")
        selected = {}
    if not isinstance(selected, dict) or not selected.get("ok"):
        return {
            "ok": False,
            "error": "marital_select_failed",
            "reason": (selected or {}).get("reason") if isinstance(selected, dict) else "",
        }
    await page.wait_for_timeout(150)
    try:
        save_state = await page.evaluate(
            "(index) => {"
            + _marital_dom_js()
            + """
              const card = maritalAllCards()[index];
              if (!card) return { found: false, enabled: false };
              const hits = [...card.root.querySelectorAll('button, [role=button]')]
                .filter((btn) => maritalButtonLabel(btn) === 'save');
              if (hits.length !== 1) return { found: false, enabled: false };
              const btn = hits[0];
              return {
                found: true,
                enabled: !btn.disabled && btn.getAttribute('aria-disabled') !== 'true',
              };
            }""",
            index,
        )
    except Exception:
        save_state = {}
    if not isinstance(save_state, dict) or not save_state.get("found"):
        return {"ok": False, "error": "marital_save_missing"}
    if not save_state.get("enabled"):
        # Pointcare disables Save when the selected value already equals saved data.
        await _click_marital_action(page, index, "cancel")
        await page.wait_for_timeout(200)
        return {"ok": True, "changed": False, "status": status, "card": before}
    if not await _click_marital_action(page, index, "save"):
        return {"ok": False, "error": "marital_save_failed"}
    await page.wait_for_timeout(350)
    after_cards = await _scrape_marital_cards(page)
    if index >= len(after_cards):
        return {"ok": False, "error": "marital_not_verified", "reason": "card_missing"}
    after = after_cards[index]
    if str(after.get("status") or "").lower() != status.lower():
        return {
            "ok": False,
            "error": "marital_not_verified",
            "reason": "status_mismatch",
            "card": after,
        }
    return {"ok": True, "changed": True, "status": status, "card": after}


def _marital_options_question(name: str) -> str:
    return (
        f"What is the current marital status for {name}? "
        "Never Married, Married, Separated, Divorced, Widowed, "
        "Registered Domestic Partner, or Common Law Marriage?"
    )


def _marital_confirm_question(card: dict[str, Any], *, intro: bool = False) -> str:
    name = str(card.get("name") or "").strip() or "this person"
    status = str(card.get("status") or "not set").strip()
    prefix = (
        "I'll confirm each person's current marital status. " if intro else ""
    )
    return (
        f"{prefix}Is {name} {status}? "
        "Say Yes to continue, or No to change it."
    )


def _marital_heard_indexes(session: Any) -> list[int]:
    raw = session.filled.get("app_marital_heard") or "[]"
    try:
        data = json.loads(raw)
    except Exception:
        data = []
    if not isinstance(data, list):
        return []
    out: list[int] = []
    for item in data:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _mark_marital_heard(session: Any, index: int) -> None:
    heard = _marital_heard_indexes(session)
    if index not in heard:
        heard.append(index)
    session.filled["app_marital_heard"] = json.dumps(heard)
    session.filled["app_marital_walk_index"] = str(index)


def _marital_walk_index(session: Any, cards: list[dict[str, Any]]) -> int:
    raw = str(session.filled.get("app_marital_walk_index") or "").strip()
    if raw.isdigit():
        return max(0, min(int(raw), max(len(cards) - 1, 0)))
    return 0


def _marital_next_unheard_index(cards: list[dict[str, Any]], session: Any) -> int:
    heard = set(_marital_heard_indexes(session))
    for card in cards:
        try:
            idx = int(card.get("index"))
        except (TypeError, ValueError):
            continue
        if idx not in heard:
            return idx
    return -1


def _marital_clear_edit(session: Any) -> None:
    session.filled.pop("app_marital_current", None)
    session.filled.pop("app_marital_current_index", None)


async def _resolve_screen_after_marital_leave(
    page: Page,
    leave_step: str = "app_marital_review",
    *,
    timeout_s: float = 4.0,
) -> str:
    """Resolve the next Stage E step after Next on marital.

    Pointcare can leave marital (cards gone) while body text still matches
    marital option labels, so detect alone is not trusted when cards are absent.
    """

    async def _probe() -> str:
        screen = await detect_application_screen(page)
        if (
            screen
            and screen.startswith("app_")
            and screen not in ("programs", leave_step)
        ):
            return screen
        try:
            if await _scrape_marital_cards(page):
                return leave_step
        except Exception:
            pass
        try:
            if await _scrape_childcare_cards(page):
                return "app_childcare_review"
        except Exception:
            pass
        return ""

    found = await _probe()
    if found and found != leave_step:
        return found
    deadline = time.monotonic() + (
        min(timeout_s, 2.5) if found == leave_step else timeout_s
    )
    while time.monotonic() < deadline:
        await page.wait_for_timeout(300)
        found = await _probe()
        if found and found != leave_step:
            return found
    try:
        still = await _scrape_marital_cards(page)
    except Exception:
        still = []
    # Cards gone ⇒ Next already advanced; childcare is the canonical next step.
    if not still:
        return "app_childcare_review"
    return leave_step


async def _marital_advance_after_member(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    step: str,
    cards: list[dict[str, Any]],
    index: int,
    *,
    changed_line: str = "",
) -> dict[str, Any]:
    """Mark the member confirmed, then ask the next Yes/No or click Next."""
    _marital_clear_edit(session)
    heard = _marital_heard_indexes(session)
    if index not in heard:
        heard.append(index)
    session.filled["app_marital_heard"] = json.dumps(heard)
    nxt = _marital_next_unheard_index(cards, session)
    prefix = (changed_line + " ") if changed_line else ""
    if nxt >= 0 and nxt < len(cards):
        session.filled.pop("app_marital_review_ready", None)
        session.filled["app_marital_walk_index"] = str(nxt)
        speech = prefix + _marital_confirm_question(cards[nxt], intro=False)
        _set_pending_question(session, step, speech)
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "marital next member",
            spoken_override=speech,
        )
    session.filled["app_marital_review_ready"] = "1"
    clicked = await _click_stage_next(page)
    if not clicked:
        speech = (
            prefix
            + "Next is not available yet. "
            + _marital_confirm_question(cards[index], intro=False)
        )
        session.filled.pop("app_marital_review_ready", None)
        session.filled["app_marital_heard"] = json.dumps(
            [i for i in heard if i != index]
        )
        session.filled["app_marital_walk_index"] = str(index)
        _set_pending_question(session, step, speech)
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "marital_next_disabled",
            "step": step,
            "phase": "application",
        }
    next_step = await _advance_after_answer(
        page, session, allow_extra_next=False, leave_step=step
    )
    if not next_step or next_step == step:
        # Detect often keeps matching leftover marital labels; use cards.
        next_step = await _resolve_screen_after_marital_leave(page, step)
        if not next_step or next_step == step:
            speech = (
                prefix
                + "The marital status screen is still open, so I did not move ahead. "
                "Say Yes to try Next again."
            )
            session.filled["app_marital_review_ready"] = "1"
            session.filled["app_marital_walk_index"] = str(index)
            _set_pending_question(session, step, speech)
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "marital_next_stayed",
                "step": step,
                "phase": "application",
            }
    session.filled.pop("app_marital_review_ready", None)
    session.filled.pop("app_marital_heard", None)
    session.filled.pop("app_marital_walk_index", None)
    _marital_clear_edit(session)
    try:
        next_step, spoken = await _prepare_resume_speech(session, page, next_step)
    except Exception:
        logger.exception("prepare speech after marital failed step=%s", next_step)
        spoken = app_question_for_step(next_step, session)
    if prefix.strip():
        spoken = (prefix + (spoken or "")).strip()
    return _app_ok(
        call_id,
        session,
        text,
        next_step,
        "marital statuses confirmed",
        spoken_override=spoken,
    )


async def _prepare_marital_review_speech(
    session: Any, page: Page
) -> tuple[str, str]:
    cards = await _scrape_marital_cards(page)
    current = str(session.filled.get("app_marital_current") or "").strip()
    index_raw = str(session.filled.get("app_marital_current_index") or "").strip()
    if current and index_raw.isdigit():
        speech = _marital_options_question(current)
    else:
        start = 0
        session.filled["app_marital_heard"] = "[]"
        session.filled["app_marital_walk_index"] = "0"
        session.filled.pop("app_marital_review_ready", None)
        if cards:
            speech = _marital_confirm_question(cards[start], intro=True)
        else:
            speech = app_question_for_step("app_marital_review")
    _set_pending_question(session, "app_marital_review", speech)
    return "app_marital_review", speech


async def _handle_marital_step(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    lowered: str,
    step: str,
) -> dict[str, Any]:
    from .live_form import _is_no, _is_yes

    cards = await _scrape_marital_cards(page)
    if not cards:
        # Next already left marital (false "still open") — follow cards/live screen.
        live = await _resolve_screen_after_marital_leave(page, step, timeout_s=3.0)
        if live and live.startswith("app_") and live != step:
            session.filled.pop("app_marital_review_ready", None)
            session.filled.pop("app_marital_heard", None)
            session.filled.pop("app_marital_walk_index", None)
            _marital_clear_edit(session)
            try:
                live, spoken = await _prepare_resume_speech(session, page, live)
            except Exception:
                logger.exception("prepare speech after marital cards missing")
                spoken = app_question_for_step(live, session)
            return _app_ok(
                call_id,
                session,
                text,
                live,
                "marital already advanced",
                spoken_override=spoken,
            )
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": (
                "ASK: I couldn't read the marital-status cards. "
                "Please stay on this page and say repeat."
            ),
            "error": "marital_cards_missing",
            "step": step,
            "phase": "application",
        }
    current = str(session.filled.get("app_marital_current") or "").strip()
    index_raw = str(session.filled.get("app_marital_current_index") or "").strip()
    current_index = int(index_raw) if index_raw.isdigit() else -1
    walk = _marital_walk_index(session, cards)
    editing = current_index >= 0

    if re.search(r"\bcancel\b", lowered):
        if editing:
            await _click_marital_action(page, current_index, "cancel")
            await page.wait_for_timeout(200)
            card = cards[current_index] if current_index < len(cards) else {}
            name = current or str(card.get("name") or "")
            session.filled["app_marital_current"] = name
            session.filled["app_marital_current_index"] = str(current_index)
            saved = str(card.get("status") or "").strip()
            speech = _marital_options_question(name)
            if saved:
                speech = f"{name} is currently {saved}. " + speech
            _set_pending_question(session, step, speech)
            return _app_ok(
                call_id,
                session,
                text,
                step,
                "marital edit cancelled",
                spoken_override=speech,
            )
        speech = _marital_confirm_question(cards[walk], intro=False)
        _set_pending_question(session, step, speech)
        return _app_ok(
            call_id, session, text, step, "nothing to cancel", spoken_override=speech
        )

    status = _marital_status_from_text(text)
    target, clarify = _marital_target_card(text, cards)
    if clarify:
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + clarify,
            "error": "marital_member_ambiguous",
            "step": step,
            "phase": "application",
        }

    wants_yes = bool(
        _is_yes(text)
        or re.search(r"\b(continue|next|looks? correct|that(?:'s| is) correct)\b", lowered)
    )
    wants_no = bool(
        _is_no(text)
        or re.search(r"\b(change|wrong|incorrect|fix|edit)\b", lowered)
    )

    # While editing, Yes alone is not a status — re-ask the options.
    if editing and wants_yes and not status and target is None:
        speech = _marital_options_question(current or cards[current_index].get("name") or "")
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "marital_status_needed",
            "step": step,
            "phase": "application",
        }

    # Confirm Yes for the current walk member → next member or stage Next.
    if wants_yes and not status and target is None and not editing:
        return await _marital_advance_after_member(
            call_id, session, page, text, step, cards, walk
        )

    # Confirm No → open pencil for the walk member and ask for the new status.
    if wants_no and not status and target is None and not editing:
        card = cards[walk]
        index = int(card.get("index") or walk)
        opened = await _open_marital_editor(page, index)
        if not opened.get("ok"):
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": (
                    f"ASK: I couldn't open the marital-status choices for "
                    f"{card.get('name')}. Please say No again."
                ),
                "error": "marital_editor_failed",
                "step": step,
                "phase": "application",
            }
        name = str(card.get("name") or "").strip()
        session.filled["app_marital_current"] = name
        session.filled["app_marital_current_index"] = str(index)
        speech = _marital_options_question(name)
        _set_pending_question(session, step, speech)
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "marital member editor opened",
            spoken_override=speech,
        )

    if target is None and editing and current_index < len(cards):
        target = cards[current_index]
    elif target is None and status and not editing:
        # Status spoken during confirm applies to the member being reviewed.
        target = cards[walk]

    if target is not None and not status:
        index = int(target.get("index") or 0)
        opened = await _open_marital_editor(page, index)
        if not opened.get("ok"):
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": (
                    f"ASK: I couldn't open the marital-status choices for "
                    f"{target.get('name')}. Please say the full name again."
                ),
                "error": "marital_editor_failed",
                "step": step,
                "phase": "application",
            }
        name = str(target.get("name") or "").strip()
        session.filled["app_marital_current"] = name
        session.filled["app_marital_current_index"] = str(index)
        speech = _marital_options_question(name)
        _set_pending_question(session, step, speech)
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "marital member editor opened",
            spoken_override=speech,
        )

    if status and target is None:
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": (
                "ASK: Whose marital status should I change? "
                "Please say the full name as shown on the application."
            ),
            "error": "marital_member_needed",
            "step": step,
            "phase": "application",
        }

    if target is not None and status:
        index = int(target.get("index") or 0)
        result = await _set_member_marital_status(page, index, status)
        if not result.get("ok"):
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": (
                    f"ASK: I couldn't save {status} for {target.get('name')}. "
                    "Please say the status again, or say Cancel."
                ),
                "error": str(result.get("error") or "marital_save_failed"),
                "step": step,
                "phase": "application",
            }
        refreshed = await _scrape_marital_cards(page)
        changed_line = ""
        if result.get("changed"):
            changed_line = f"{target.get('name')} is now {status}."
        return await _marital_advance_after_member(
            call_id,
            session,
            page,
            text,
            step,
            refreshed or cards,
            index,
            changed_line=changed_line,
        )

    if editing:
        speech = _marital_options_question(current or "")
    else:
        speech = _marital_confirm_question(cards[walk], intro=False)
    return {
        "ok": False,
        "call_id": call_id,
        "field_id": "application_answer",
        "spoken": "ASK: " + speech,
        "error": "marital_clarify",
        "step": step,
        "phase": "application",
    }


def _childcare_dom_js() -> str:
    """DOM helpers for per-member Yes/No childcare rows, including duplicates."""
    return r"""
      const ccNorm = (s) => (s || '').replace(/\s+/g, ' ').trim();
      const ccLow = (s) => ccNorm(s).toLowerCase().replace(/\.$/, '');
      const ccYesNo = (s) => {
        const n = ccLow(s);
        if (!n) return '';
        if (n === 'yes' || n.startsWith('yes ') || n.startsWith('yes—') ||
            n.startsWith('yes-') || n.startsWith('yes,')) return 'Yes';
        if (n === 'no' || n.startsWith('no ') || n.startsWith('no—') ||
            n.startsWith('no-') || n.startsWith('no,')) return 'No';
        return '';
      };
      const ccVisible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const st = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' &&
          st.display !== 'none' && st.opacity !== '0';
      };
      const ccRadioLabel = (radio) => {
        if (!radio) return '';
        const own = ccYesNo(
          radio.getAttribute('aria-label') || radio.getAttribute('value') ||
          radio.innerText || radio.textContent || ''
        );
        if (own) return own;
        const id = radio.getAttribute('id');
        if (id) {
          const lab = document.querySelector(`label[for="${CSS.escape(id)}"]`);
          if (lab) {
            const found = ccYesNo(lab.innerText || lab.textContent || '');
            if (found) return found;
          }
        }
        const wrap = radio.closest('label');
        if (wrap) {
          const found = ccYesNo(wrap.innerText || wrap.textContent || '');
          if (found) return found;
        }
        let sib = radio.nextElementSibling;
        for (let i = 0; sib && i < 3; i += 1, sib = sib.nextElementSibling) {
          const found = ccYesNo(sib.innerText || sib.textContent || '');
          if (found) return found;
        }
        const parent = radio.parentElement;
        if (parent) {
          for (const line of (parent.innerText || '').split(/\n|\s{2,}/)) {
            const found = ccYesNo(line);
            if (found) return found;
          }
        }
        return '';
      };
      const ccIsName = (line) => {
        const t = ccNorm(line).replace(/\s*\(you\)\s*/ig, ' ').trim();
        if (!t || ccYesNo(t)) return false;
        if (!/^[A-Za-z][A-Za-z .'-]{0,40}$/.test(t) || t.split(' ').length > 4) {
          return false;
        }
        const n = ccLow(t);
        if (/children|childcare|household|member|live with|care for|annual|expense|blood|relative|step |next|back|continue/.test(n)) {
          return false;
        }
        if (/^(yes|no|cancel|save|edit)$/.test(n)) return false;
        return true;
      };
      const ccNamesIn = (root) => {
        const out = [];
        for (const line of (root.innerText || '').split('\n')) {
          const t = ccNorm(line);
          if (ccIsName(t) && !out.some((name) => ccLow(name) === ccLow(t))) {
            out.push(t);
          }
        }
        return out;
      };
      const ccRadiosIn = (root) => [...root.querySelectorAll(
        'input[type=radio], [role=radio]'
      )].filter((item) => ccVisible(item) && Boolean(ccRadioLabel(item)));
      const ccCardForAnchor = (anchor) => {
        let cur = anchor;
        let best = null;
        for (let depth = 0; cur && cur !== document.body && depth < 8;
             depth += 1, cur = cur.parentElement) {
          const r = cur.getBoundingClientRect();
          if (r.width < 120 || r.height < 24 || r.height > 220) continue;
          const names = ccNamesIn(cur);
          if (names.length !== 1) continue;
          const radios = ccRadiosIn(cur);
          const labels = [...new Set(radios.map(ccRadioLabel).filter(Boolean))];
          if (!(labels.includes('Yes') && labels.includes('No'))) continue;
          best = cur;
          if (radios.length <= 4 && r.height <= 120) break;
        }
        return best;
      };
      const ccAllCards = () => {
        const roots = [];
        const add = (root) => {
          if (!root || roots.includes(root)) return;
          if (roots.some((other) => other.contains(root))) {
            for (let i = roots.length - 1; i >= 0; i -= 1) {
              if (roots[i].contains(root)) roots.splice(i, 1);
            }
          } else if (roots.some((other) => root.contains(other))) {
            return;
          }
          roots.push(root);
        };
        for (const radio of document.querySelectorAll(
          'input[type=radio], [role=radio]'
        )) {
          if (ccVisible(radio) && ccRadioLabel(radio)) {
            add(ccCardForAnchor(radio));
          }
        }
        roots.sort((a, b) => {
          const ar = a.getBoundingClientRect();
          const br = b.getBoundingClientRect();
          return ar.top - br.top || ar.left - br.left;
        });
        return roots.map((root, index) => {
          const names = ccNamesIn(root);
          const radios = ccRadiosIn(root);
          let selected = '';
          for (const radio of radios) {
            if (radio.checked === true || radio.getAttribute('aria-checked') === 'true') {
              selected = ccRadioLabel(radio);
              break;
            }
          }
          return {
            root,
            index,
            name: names[0] || '',
            answer: selected,
            options: [...new Set(radios.map(ccRadioLabel).filter(Boolean))],
          };
        }).filter((card) => card.name);
      };
    """


async def _scrape_childcare_cards(page: Page) -> list[dict[str, Any]]:
    """Read childcare Yes/No rows in visual order, preserving duplicate names."""
    try:
        rows = await page.evaluate(
            "() => {"
            + _childcare_dom_js()
            + """
              return ccAllCards().map((card) => ({
                index: card.index,
                name: card.name,
                answer: card.answer,
                options: card.options,
              }));
            }"""
        )
        return rows if isinstance(rows, list) else []
    except Exception:
        logger.exception("scrape_childcare_cards failed")
        return []


def _childcare_value_from_text(text: str) -> str:
    """Map spoken childcare answers to Yes or No."""
    from .live_form import _is_no, _is_yes

    lowered = (text or "").lower()
    to_no = bool(re.search(r"\b(?:to|as|be|marked)\s+no\b", lowered))
    to_yes = bool(re.search(r"\b(?:to|as|be|marked)\s+yes\b", lowered))
    if to_yes and not to_no:
        return "Yes"
    if to_no and not to_yes:
        return "No"
    # Prefer the last explicit yes/no token when both appear ("change yes to no").
    tokens = re.findall(r"\b(yes|yeah|yep|yup|no|nope|nah)\b", lowered)
    if tokens:
        last = tokens[-1]
        if last in ("yes", "yeah", "yep", "yup"):
            return "Yes"
        return "No"
    if _is_yes(text):
        return "Yes"
    if _is_no(text):
        return "No"
    return ""


def _childcare_target_card(
    text: str, cards: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None]:
    return _marital_target_card(text, cards)


async def _set_member_childcare(
    page: Page, index: int, answer: str
) -> dict[str, Any]:
    """Click Yes or No for one childcare row. No-op when already selected."""
    before_cards = await _scrape_childcare_cards(page)
    if index >= len(before_cards):
        return {"ok": False, "error": "childcare_card_missing"}
    before = before_cards[index]
    if str(before.get("answer") or "").lower() == answer.lower():
        return {"ok": True, "changed": False, "answer": answer, "card": before}
    try:
        selected = await page.evaluate(
            "({ index, answer }) => {"
            + _childcare_dom_js()
            + """
              const card = ccAllCards()[index];
              if (!card) return { ok: false, reason: 'card_not_found' };
              const want = ccLow(answer);
              const hits = ccRadiosIn(card.root).filter(
                (radio) => ccLow(ccRadioLabel(radio)) === want
              );
              if (hits.length !== 1) {
                return {
                  ok: false,
                  reason: hits.length ? 'ambiguous_radio' : 'radio_not_found',
                };
              }
              hits[0].scrollIntoView({ block: 'center', inline: 'nearest' });
              hits[0].click();
              return { ok: true };
            }""",
            {"index": index, "answer": answer},
        )
    except Exception:
        logger.exception("set_member_childcare failed")
        selected = {}
    if not isinstance(selected, dict) or not selected.get("ok"):
        return {
            "ok": False,
            "error": "childcare_select_failed",
            "reason": (selected or {}).get("reason") if isinstance(selected, dict) else "",
        }
    await page.wait_for_timeout(200)
    after_cards = await _scrape_childcare_cards(page)
    if index >= len(after_cards):
        return {"ok": False, "error": "childcare_not_verified", "reason": "card_missing"}
    after = after_cards[index]
    if str(after.get("answer") or "").lower() != answer.lower():
        return {
            "ok": False,
            "error": "childcare_not_verified",
            "reason": "answer_mismatch",
            "card": after,
        }
    return {"ok": True, "changed": True, "answer": answer, "card": after}


def _childcare_options_question(name: str) -> str:
    return f"Should {name} be Yes or No?"


def _childcare_confirm_question(card: dict[str, Any], *, intro: bool = False) -> str:
    name = str(card.get("name") or "").strip() or "this person"
    answer = str(card.get("answer") or "not set").strip()
    prefix = (
        "I'll confirm childcare answers one person at a time. " if intro else ""
    )
    return (
        f"{prefix}{name} is marked {answer} for caring for children under 19. "
        "Say Yes to continue, or No to change it."
    )


def _childcare_heard_indexes(session: Any) -> list[int]:
    raw = session.filled.get("app_childcare_heard") or "[]"
    try:
        data = json.loads(raw)
    except Exception:
        data = []
    if not isinstance(data, list):
        return []
    out: list[int] = []
    for item in data:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _childcare_walk_index(session: Any, cards: list[dict[str, Any]]) -> int:
    raw = str(session.filled.get("app_childcare_walk_index") or "").strip()
    if raw.isdigit():
        return max(0, min(int(raw), max(len(cards) - 1, 0)))
    return 0


def _childcare_next_unheard_index(cards: list[dict[str, Any]], session: Any) -> int:
    heard = set(_childcare_heard_indexes(session))
    for card in cards:
        try:
            idx = int(card.get("index"))
        except (TypeError, ValueError):
            continue
        if idx not in heard:
            return idx
    return -1


def _childcare_clear_edit(session: Any) -> None:
    session.filled.pop("app_childcare_current", None)
    session.filled.pop("app_childcare_current_index", None)


async def _childcare_advance_after_member(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    step: str,
    cards: list[dict[str, Any]],
    index: int,
    *,
    changed_line: str = "",
) -> dict[str, Any]:
    """Mark the member confirmed, then ask the next Yes/No or click Next."""
    _childcare_clear_edit(session)
    heard = _childcare_heard_indexes(session)
    if index not in heard:
        heard.append(index)
    session.filled["app_childcare_heard"] = json.dumps(heard)
    nxt = _childcare_next_unheard_index(cards, session)
    prefix = (changed_line + " ") if changed_line else ""
    if nxt >= 0 and nxt < len(cards):
        session.filled.pop("app_childcare_review_ready", None)
        session.filled["app_childcare_walk_index"] = str(nxt)
        speech = prefix + _childcare_confirm_question(cards[nxt], intro=False)
        _set_pending_question(session, step, speech)
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "childcare next member",
            spoken_override=speech,
        )
    session.filled["app_childcare_review_ready"] = "1"
    clicked = await _click_stage_next(page)
    if not clicked:
        speech = (
            prefix
            + "Next is not available yet. "
            + _childcare_confirm_question(cards[index], intro=False)
        )
        session.filled.pop("app_childcare_review_ready", None)
        session.filled["app_childcare_heard"] = json.dumps(
            [i for i in heard if i != index]
        )
        session.filled["app_childcare_walk_index"] = str(index)
        _set_pending_question(session, step, speech)
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "childcare_next_disabled",
            "step": step,
            "phase": "application",
        }
    next_step = await _advance_after_answer(
        page, session, allow_extra_next=False, leave_step=step
    )
    if not next_step or next_step == step:
        try:
            still_cards = await _scrape_childcare_cards(page)
        except Exception:
            still_cards = []
        next_step = await _wait_for_screen_change(
            page, step, timeout_s=4.0 if not still_cards else 2.5
        )
        if not next_step or next_step == step:
            speech = (
                prefix
                + "The childcare screen is still open, so I did not move ahead. "
                "Say Yes to try Next again."
            )
            session.filled["app_childcare_review_ready"] = "1"
            session.filled["app_childcare_walk_index"] = str(index)
            _set_pending_question(session, step, speech)
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "childcare_next_stayed",
                "step": step,
                "phase": "application",
            }
    session.filled.pop("app_childcare_review_ready", None)
    session.filled.pop("app_childcare_heard", None)
    session.filled.pop("app_childcare_walk_index", None)
    _childcare_clear_edit(session)
    try:
        next_step, spoken = await _prepare_resume_speech(session, page, next_step)
    except Exception:
        logger.exception("prepare speech after childcare failed step=%s", next_step)
        spoken = app_question_for_step(next_step, session)
    if prefix.strip():
        spoken = (prefix + (spoken or "")).strip()
    return _app_ok(
        call_id,
        session,
        text,
        next_step,
        "childcare answers confirmed",
        spoken_override=spoken,
    )


async def _prepare_childcare_review_speech(
    session: Any, page: Page
) -> tuple[str, str]:
    cards = await _scrape_childcare_cards(page)
    current = str(session.filled.get("app_childcare_current") or "").strip()
    index_raw = str(session.filled.get("app_childcare_current_index") or "").strip()
    if current and index_raw.isdigit():
        speech = _childcare_options_question(current)
    else:
        session.filled["app_childcare_heard"] = "[]"
        session.filled["app_childcare_walk_index"] = "0"
        session.filled.pop("app_childcare_review_ready", None)
        if cards:
            speech = _childcare_confirm_question(cards[0], intro=True)
        else:
            speech = app_question_for_step("app_childcare_review")
    _set_pending_question(session, "app_childcare_review", speech)
    return "app_childcare_review", speech


async def _handle_childcare_step(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    lowered: str,
    step: str,
) -> dict[str, Any]:
    from .live_form import _is_no, _is_yes

    cards = await _scrape_childcare_cards(page)
    if not cards:
        live = await detect_application_screen(page)
        if not live or live == step:
            live = await _wait_for_screen_change(page, step, timeout_s=3.0)
        if live and live.startswith("app_") and live != step:
            session.filled.pop("app_childcare_review_ready", None)
            session.filled.pop("app_childcare_heard", None)
            session.filled.pop("app_childcare_walk_index", None)
            _childcare_clear_edit(session)
            try:
                live, spoken = await _prepare_resume_speech(session, page, live)
            except Exception:
                logger.exception("prepare speech after childcare cards missing")
                spoken = app_question_for_step(live, session)
            return _app_ok(
                call_id,
                session,
                text,
                live,
                "childcare already advanced",
                spoken_override=spoken,
            )
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": (
                "ASK: I couldn't read the childcare answers. "
                "Please stay on this page and say repeat."
            ),
            "error": "childcare_cards_missing",
            "step": step,
            "phase": "application",
        }
    current = str(session.filled.get("app_childcare_current") or "").strip()
    index_raw = str(session.filled.get("app_childcare_current_index") or "").strip()
    current_index = int(index_raw) if index_raw.isdigit() else -1
    walk = _childcare_walk_index(session, cards)
    editing = current_index >= 0

    if re.search(r"\bcancel\b", lowered):
        if editing:
            card = cards[current_index] if current_index < len(cards) else {}
            name = current or str(card.get("name") or "")
            session.filled["app_childcare_current"] = name
            session.filled["app_childcare_current_index"] = str(current_index)
            saved = str(card.get("answer") or "").strip()
            speech = _childcare_options_question(name)
            if saved:
                speech = f"{name} is currently {saved}. " + speech
            _set_pending_question(session, step, speech)
            return _app_ok(
                call_id,
                session,
                text,
                step,
                "childcare edit cancelled",
                spoken_override=speech,
            )
        speech = _childcare_confirm_question(cards[walk], intro=False)
        _set_pending_question(session, step, speech)
        return _app_ok(
            call_id, session, text, step, "nothing to cancel", spoken_override=speech
        )

    target, clarify = _childcare_target_card(text, cards)
    if clarify:
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + clarify,
            "error": "childcare_member_ambiguous",
            "step": step,
            "phase": "application",
        }

    raw_value = _childcare_value_from_text(text)
    change_intent = bool(
        re.search(r"\b(change|wrong|incorrect|fix|edit)\b", lowered)
    )
    # During confirm, bare Yes/No mean continue/change — not a new value —
    # unless the utterance also names a member or explicitly says "to yes/no".
    explicit_value = bool(
        re.search(r"\b(?:to|as|be|marked)\s+(?:yes|no)\b", lowered)
        or (target is not None and bool(raw_value))
    )
    value = raw_value if (editing or explicit_value) else ""
    wants_continue = bool(
        (_is_yes(text) or re.search(r"\b(continue|next|looks? correct)\b", lowered))
        and not change_intent
        and not explicit_value
    )
    wants_change = bool(
        (_is_no(text) or change_intent) and not explicit_value
    )

    if editing:
        # In edit mode, Yes/No set the radio for the open member.
        value = raw_value
        if not value:
            speech = _childcare_options_question(
                current or str((cards[current_index] if current_index < len(cards) else {}).get("name") or "")
            )
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "childcare_value_needed",
                "step": step,
                "phase": "application",
            }
        target = (
            cards[current_index]
            if current_index < len(cards)
            else target
        )
        if target is None:
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": (
                    "ASK: Whose childcare answer should I change? "
                    "Please say the full name as shown."
                ),
                "error": "childcare_member_needed",
                "step": step,
                "phase": "application",
            }
        index = int(target.get("index") or current_index)
        result = await _set_member_childcare(page, index, value)
        if not result.get("ok"):
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": (
                    f"ASK: I couldn't set {value} for {target.get('name')}. "
                    "Please say Yes or No again, or say Cancel."
                ),
                "error": str(result.get("error") or "childcare_save_failed"),
                "step": step,
                "phase": "application",
            }
        refreshed = await _scrape_childcare_cards(page)
        changed_line = ""
        if result.get("changed"):
            changed_line = f"{target.get('name')} is now {value}."
        return await _childcare_advance_after_member(
            call_id,
            session,
            page,
            text,
            step,
            refreshed or cards,
            index,
            changed_line=changed_line,
        )

    if wants_continue and target is None:
        return await _childcare_advance_after_member(
            call_id, session, page, text, step, cards, walk
        )

    if wants_change and target is None:
        card = cards[walk]
        index = int(card.get("index") or walk)
        name = str(card.get("name") or "").strip()
        session.filled["app_childcare_current"] = name
        session.filled["app_childcare_current_index"] = str(index)
        speech = _childcare_options_question(name)
        _set_pending_question(session, step, speech)
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "childcare member edit opened",
            spoken_override=speech,
        )

    if target is None and value:
        target = cards[walk]

    if target is not None and not value:
        index = int(target.get("index") or 0)
        name = str(target.get("name") or "").strip()
        session.filled["app_childcare_current"] = name
        session.filled["app_childcare_current_index"] = str(index)
        speech = _childcare_options_question(name)
        _set_pending_question(session, step, speech)
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "childcare member edit opened",
            spoken_override=speech,
        )

    if value and target is None:
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": (
                "ASK: Whose childcare answer should I change? "
                "Please say the full name as shown on the application."
            ),
            "error": "childcare_member_needed",
            "step": step,
            "phase": "application",
        }

    if target is not None and value:
        index = int(target.get("index") or 0)
        result = await _set_member_childcare(page, index, value)
        if not result.get("ok"):
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": (
                    f"ASK: I couldn't set {value} for {target.get('name')}. "
                    "Please say Yes or No again."
                ),
                "error": str(result.get("error") or "childcare_save_failed"),
                "step": step,
                "phase": "application",
            }
        refreshed = await _scrape_childcare_cards(page)
        changed_line = ""
        if result.get("changed"):
            changed_line = f"{target.get('name')} is now {value}."
        return await _childcare_advance_after_member(
            call_id,
            session,
            page,
            text,
            step,
            refreshed or cards,
            index,
            changed_line=changed_line,
        )

    speech = _childcare_confirm_question(cards[walk], intro=False)
    return {
        "ok": False,
        "call_id": call_id,
        "field_id": "application_answer",
        "spoken": "ASK: " + speech,
        "error": "childcare_clarify",
        "step": step,
        "phase": "application",
    }


def _address_field_from_text(text: str) -> str:
    """Map spoken correction to home, mailing, or both.

    Contrastive replies such as "home is correct, mailing is different" resolve
    to the field that needs changing — not both merely because both words appear.
    """
    lowered = (text or "").lower()
    if re.search(r"\bboth\b", lowered):
        return "both"
    keep = r"(?:is\s+)?(?:correct|right|fine|ok|okay|good|same|unchanged)"
    change = r"(?:is\s+)?(?:wrong|incorrect|different|change|fix|edit|update|not correct)"
    home_keep = bool(
        re.search(rf"\bhome\b(?:\s+\w+){{0,6}}\s+{keep}\b", lowered)
        or re.search(rf"\b{keep}\b(?:\s+\w+){{0,6}}\s+\bhome\b", lowered)
    )
    mail_keep = bool(
        re.search(rf"\bmailing\b(?:\s+\w+){{0,6}}\s+{keep}\b", lowered)
        or re.search(rf"\b{keep}\b(?:\s+\w+){{0,6}}\s+\bmailing\b", lowered)
    )
    home_change = bool(
        re.search(rf"\bhome\b(?:\s+\w+){{0,8}}\s+{change}\b", lowered)
        or re.search(
            r"\b(?:change|fix|edit|update)\b(?:\s+\w+){0,6}\s+\bhome\b", lowered
        )
    )
    mail_change = bool(
        re.search(rf"\bmailing\b(?:\s+\w+){{0,8}}\s+{change}\b", lowered)
        or re.search(
            r"\b(?:change|fix|edit|update)\b(?:\s+\w+){0,6}\s+\bmailing\b",
            lowered,
        )
    )
    if home_keep and (mail_change or (re.search(r"\bmailing\b", lowered) and not mail_keep)):
        return "mailing"
    if mail_keep and (home_change or (re.search(r"\bhome\b", lowered) and not home_keep)):
        return "home"
    if home_change and mail_change:
        return "both"
    if home_change:
        return "home"
    if mail_change:
        return "mailing"
    home = bool(re.search(r"\bhome\b", lowered))
    mailing = bool(re.search(r"\bmailing\b", lowered))
    if home and mailing:
        return "both"
    if home:
        return "home"
    if mailing:
        return "mailing"
    return ""


def _address_error(
    call_id: str,
    step: str,
    error: str,
    spoken: str,
    session: Any | None = None,
) -> dict[str, Any]:
    if session is not None and spoken:
        _set_pending_question(session, step, spoken)
    return {
        "ok": False,
        "call_id": call_id,
        "field_id": "application_answer",
        "spoken": "ASK: " + spoken,
        "error": error,
        "step": step,
        "phase": "application",
    }


def _address_leave_as_is(text: str) -> bool:
    lowered = (text or "").lower()
    return bool(
        re.search(
            r"\b(leave (it|them|that|this)?\s*(as is|alone)?|keep (it|them|that)|"
            r"don'?t change|do not change|no change|skip)\b",
            lowered,
        )
    )


def _start_member_home_review(session: Any, card: dict[str, Any]) -> str:
    """Pin the member on home_check and return the home-only Yes/No prompt."""
    name = str(card.get("name") or "").strip()
    session.filled["app_address_current"] = name
    session.filled["app_address_field"] = "home"
    session.filled.pop("app_address_ready", None)
    session.filled.pop("app_address_correcting", None)
    _clear_address_pending(session)
    _set_address_phase(session, "home_check")
    return _address_home_check_speech(card)


async def _prepare_address_review_speech(
    session: Any, page: Page
) -> tuple[str, str]:
    """Walk members top-to-bottom: home check → mailing check → next member."""
    cards = await _scrape_address_cards(page)
    names = [str(card.get("name") or "").strip() for card in cards if card.get("name")]
    current = str(session.filled.get("app_address_current") or "").strip()
    phase = _address_phase(session)

    # Mid home/mailing confirm-before-save flow.
    if current and phase in (
        "home_check",
        "home_change_ask",
        "home_enter",
        "home_confirm",
        "mailing_check",
        "mailing_change_ask",
        "mailing_enter",
        "mailing_confirm",
        "share_ask",
    ):
        card = _card_by_name(cards, current) or {"name": current}
        if phase == "home_check":
            speech = _address_home_check_speech(card)
        elif phase == "mailing_check":
            speech = _address_mailing_check_speech(card)
        elif phase == "home_change_ask":
            speech = _address_change_ask_speech(current, "home")
        elif phase == "mailing_change_ask":
            speech = _address_change_ask_speech(current, "mailing")
        elif phase == "home_enter":
            speech = _address_enter_speech(current, "home")
        elif phase == "mailing_enter":
            speech = _address_enter_speech(current, "mailing")
        elif phase == "share_ask":
            share_members: list[str] = []
            try:
                loaded = json.loads(
                    session.filled.get("app_address_share_members") or "[]"
                )
                if isinstance(loaded, list):
                    share_members = [str(item) for item in loaded if str(item).strip()]
            except Exception:
                share_members = []
            speech = _address_share_ask_speech(
                {
                    "members": share_members,
                    "prompt": str(session.filled.get("app_address_share_prompt") or ""),
                }
            )
        else:
            speech = _address_pending_confirm_speech(
                current,
                "home" if phase.startswith("home") else "mailing",
                kind=str(session.filled.get("app_address_pending_kind") or "street"),
                street=str(session.filled.get("app_address_pending_street") or ""),
                chosen=str(session.filled.get("app_address_pending_chosen") or ""),
            )
        _set_pending_question(session, "app_address_review", speech)
        return "app_address_review", speech

    # Resume an in-progress member on home first when phase was lost.
    if current and current.lower() not in {
        name.lower() for name in _address_confirmed_names(session)
    }:
        card = _card_by_name(cards, current)
        if card is not None:
            speech = _start_member_home_review(session, card)
            _set_pending_question(session, "app_address_review", speech)
            return "app_address_review", speech

    # Always walk in screen order (Admin first) — never jump to incomplete cards.
    confirmed = {name.lower() for name in _address_confirmed_names(session)}
    pending = [name for name in names if name.lower() not in confirmed]
    if pending:
        nxt = pending[0]
        if current and current.lower() not in confirmed and current in names:
            nxt = current
        card = _card_by_name(cards, nxt) or {"name": nxt}
        speech = _start_member_home_review(session, card)
        _set_pending_question(session, "app_address_review", speech)
        return "app_address_review", speech

    if names:
        session.filled["app_address_ready"] = "1"
        session.filled.pop("app_address_current", None)
        session.filled.pop("app_address_field", None)
        _set_address_phase(session, "")
        speech = "Those address cards are confirmed. Say Yes to continue."
        _set_pending_question(session, "app_address_review", speech)
        return "app_address_review", speech

    speech = app_question_for_step("app_address_review", session)
    _set_pending_question(session, "app_address_review", speech)
    return "app_address_review", speech


async def _stage_matched_address_pending(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    step: str,
    target: str,
    field: str,
    street: str,
    unit: str = "",
) -> dict[str, Any]:
    """Type into Places, match one unique row, then ask Yes before Save."""
    resolved = await _resolve_spoken_address_suggestion(
        page, target, field, street, clear_first=True
    )
    if not resolved.get("ok"):
        error = str(resolved.get("error") or "address_no_suggestion")
        rows = resolved.get("suggestions") or []
        if not isinstance(rows, list):
            rows = []
        labels = [str(item) for item in rows]
        if error == "address_ambiguous":
            spoken = (
                f"More than one address matches what you said for "
                f"{_clean_member_display_name(target)}. "
                + _speak_visible_address_suggestions(labels)
            )
        elif error == "address_editor_failed":
            spoken = (
                f"I couldn't open the address editor for "
                f"{_clean_member_display_name(target)}. "
                "Please say the name again, or say Cancel."
            )
        elif error == "address_fill_failed":
            spoken = (
                f"I opened {_clean_member_display_name(target)}'s card, but I couldn't "
                "enter that address. Please say the street again."
            )
        else:
            spoken = (
                f"I couldn't match that to an address from the list for "
                f"{_clean_member_display_name(target)}. "
                + _speak_visible_address_suggestions(labels)
            )
        session.filled["app_address_current"] = target
        session.filled["app_address_correcting"] = target
        session.filled["app_address_field"] = field
        return _address_error(call_id, step, error, spoken, session)

    chosen = str(resolved.get("chosen") or "").strip()
    session.filled["app_address_current"] = target
    session.filled["app_address_correcting"] = target
    session.filled["app_address_field"] = field
    session.filled["app_address_pending_kind"] = "street"
    session.filled["app_address_pending_street"] = street
    session.filled["app_address_pending_chosen"] = chosen
    if unit:
        session.filled["app_address_pending_unit"] = unit
    else:
        session.filled.pop("app_address_pending_unit", None)
    confirm_phase = "home_confirm" if field == "home" else "mailing_confirm"
    _set_address_phase(session, confirm_phase)
    return _app_ok(
        call_id,
        session,
        text,
        step,
        "address street pending confirm",
        spoken_override=_address_pending_confirm_speech(
            target, field, kind="street", street=street, chosen=chosen
        ),
    )


async def _begin_address_home_check(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    step: str,
    target: str,
    cards: list[dict[str, Any]],
) -> dict[str, Any]:
    """Open editor after No, then ask Yes-to-change vs No Home Address."""
    speak_as = _clean_member_display_name(target)
    opened = await _open_member_address_editor(page, target)
    if not opened.get("ok"):
        return _address_error(
            call_id,
            step,
            "address_editor_failed",
            f"I couldn't open the address editor for {speak_as}. "
            "Please say the name again, or say Cancel.",
            session,
        )
    prior = _card_by_name(cards, target) or {"name": target}
    session.filled["app_address_snapshot"] = json.dumps(
        {
            "name": target,
            "home": str(prior.get("home") or ""),
            "mailing": str(prior.get("mailing") or ""),
        }
    )
    session.filled["app_address_current"] = target
    session.filled["app_address_correcting"] = target
    session.filled["app_address_field"] = "home"
    session.filled.pop("app_address_ready", None)
    _clear_address_pending(session)
    # User already said the card is wrong — ask change vs no-home next.
    _set_address_phase(session, "home_change_ask")
    speech = (
        f"Okay, I'll change that for {speak_as}. "
        + _address_change_ask_speech(target, "home")
    )
    return _app_ok(
        call_id,
        session,
        text,
        step,
        "address home change ask started",
        spoken_override=speech,
    )


async def _advance_after_address_field_save(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    step: str,
    target: str,
    field: str,
    card: dict[str, Any],
) -> dict[str, Any]:
    """After a home/mailing save, continue to mailing check or next member."""
    speak_as = _clean_member_display_name(target)
    _clear_address_pending(session)
    session.filled.pop("app_address_share_after", None)
    session.filled.pop("app_address_share_prompt", None)
    session.filled.pop("app_address_share_members", None)
    if field == "home":
        session.filled["app_address_field"] = "mailing"
        _set_address_phase(session, "mailing_check")
        await _open_member_address_editor(page, target)
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "address home saved pending mailing",
            spoken_override=(
                f"Okay, that home address choice is saved for {speak_as}. "
                + _address_mailing_check_speech(card)
            ),
        )
    _set_address_phase(session, "")
    session.filled.pop("app_address_field", None)
    session.filled.pop("app_address_correcting", None)
    session.filled.pop("app_address_snapshot", None)
    _mark_address_confirmed(session, target)
    session.filled.pop("app_address_current", None)
    next_step, spoken = await _prepare_address_review_speech(session, page)
    return _app_ok(
        call_id,
        session,
        text,
        next_step,
        "address mailing saved member done",
        spoken_override=(
            f"Okay, that mailing address choice is saved for {speak_as}. " + spoken
        ),
    )


async def _begin_address_share_ask(
    call_id: str,
    session: Any,
    text: str,
    step: str,
    target: str,
    field: str,
    share: dict[str, Any],
) -> dict[str, Any]:
    """Pause after Save when Pointcare asks about shared household addresses."""
    session.filled["app_address_current"] = target
    session.filled["app_address_correcting"] = target
    session.filled["app_address_field"] = field
    session.filled["app_address_share_after"] = (
        "mailing" if field == "home" else "done"
    )
    session.filled["app_address_share_prompt"] = str(share.get("prompt") or "")
    session.filled["app_address_share_members"] = json.dumps(
        list(share.get("members") or [])
    )
    _set_address_phase(session, "share_ask")
    return _app_ok(
        call_id,
        session,
        text,
        step,
        "address share ask",
        spoken_override=_address_share_ask_speech(share),
    )


async def _save_no_address_and_advance(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    step: str,
    target: str,
    field: str,
) -> dict[str, Any]:
    """Check No Home/Mailing Address, Save, handle share popup, then continue."""
    speak_as = _clean_member_display_name(target)
    box_kind = "home" if field == "home" else "mailing"
    opened = await _open_member_address_editor(page, target)
    if not opened.get("ok"):
        return _address_error(
            call_id,
            step,
            "address_editor_failed",
            f"I couldn't open the address editor for {speak_as}.",
            session,
        )
    # Pointcare often keeps No Home/Mailing disabled until the street chip is cleared.
    try:
        await _clear_member_address_selection(page, target, box_kind)
        await page.wait_for_timeout(200)
    except Exception:
        logger.exception(
            "address_no_clear_before_checkbox failed member=%r kind=%s",
            target,
            box_kind,
        )
    if not await _set_member_no_address(page, target, box_kind):
        return _address_error(
            call_id,
            step,
            "address_no_home_failed"
            if box_kind == "home"
            else "address_no_mailing_failed",
            f"I couldn't select that choice for {speak_as}.",
            session,
        )
    if not await _click_member_address_action(page, target, "save"):
        return _address_error(
            call_id,
            step,
            "address_save_failed",
            f"I selected that choice for {speak_as}, but Save did not complete.",
            session,
        )
    await page.wait_for_timeout(350)
    share = await _address_share_dialog_info(page)
    if share.get("open"):
        return await _begin_address_share_ask(
            call_id, session, text, step, target, field, share
        )
    if await _address_validation_visible(page, target):
        return _address_error(
            call_id,
            step,
            "address_save_failed",
            f"Pointcare still shows an address error for {speak_as}.",
            session,
        )
    refreshed = await _scrape_address_cards(page)
    card = _card_by_name(refreshed, target) or {"name": target}
    if box_kind == "home":
        card["no_home"] = True
    else:
        card["no_mailing"] = True
    return await _advance_after_address_field_save(
        call_id, session, page, text, step, target, field, card
    )


async def _commit_address_pending_and_advance(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    step: str,
    cards: list[dict[str, Any]],
) -> dict[str, Any]:
    """Save the pending matched street after Yes, then continue the walk."""
    target = str(session.filled.get("app_address_current") or "").strip()
    field = str(session.filled.get("app_address_field") or "").strip() or "home"
    kind = str(session.filled.get("app_address_pending_kind") or "").strip()
    street = str(session.filled.get("app_address_pending_street") or "").strip()
    chosen = str(session.filled.get("app_address_pending_chosen") or "").strip()
    unit = str(session.filled.get("app_address_pending_unit") or "").strip()
    speak_as = _clean_member_display_name(target or "that person")
    if not target or not kind:
        return _address_error(
            call_id,
            step,
            "address_pending_missing",
            "I lost the pending address change. Please say the address again.",
            session,
        )
    if kind in ("no_home", "no_mailing"):
        return await _save_no_address_and_advance(
            call_id, session, page, text, step, target, field
        )

    opened = await _open_member_address_editor(page, target)
    if not opened.get("ok"):
        return _address_error(
            call_id,
            step,
            "address_editor_failed",
            f"I couldn't open the address editor for {speak_as}.",
            session,
        )

    # Always select the matched Places row — never save free-typed speech alone.
    result = await _commit_member_address(
        page, target, field, street, unit, chosen=chosen
    )
    if not result.get("ok"):
        error = str(result.get("error") or "address_save_failed")
        suggestion_rows = result.get("suggestions") or []
        if not isinstance(suggestion_rows, list):
            suggestion_rows = []
        if error == "address_ambiguous":
            spoken_err = (
                f"More than one address matches for {speak_as}. "
                + _speak_visible_address_suggestions(
                    [str(item) for item in suggestion_rows]
                )
            )
        elif error == "address_no_suggestion":
            spoken_err = (
                f"I couldn't match that to an address from the list for {speak_as}. "
                + _speak_visible_address_suggestions(
                    [str(item) for item in suggestion_rows]
                )
            )
        else:
            spoken_err = (
                f"I couldn't save that {field} address for {speak_as}. "
                "Please say it again."
            )
        return _address_error(call_id, step, error, spoken_err, session)

    share = result.get("share_dialog")
    if isinstance(share, dict) and share.get("open"):
        return await _begin_address_share_ask(
            call_id, session, text, step, target, field, share
        )

    card = result.get("card") or {}
    if not isinstance(card, dict) or not card.get("name"):
        refreshed = await _scrape_address_cards(page)
        card = _card_by_name(refreshed, target) or {"name": target}
    return await _advance_after_address_field_save(
        call_id, session, page, text, step, target, field, card
    )


async def _handle_address_field_phase(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    lowered: str,
    step: str,
    cards: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Stepwise home/mailing change. None = not in that phase."""
    from .live_form import _is_no, _is_yes

    phase = _address_phase(session)
    if phase not in (
        "home_check",
        "home_change_ask",
        "home_enter",
        "home_confirm",
        "mailing_check",
        "mailing_change_ask",
        "mailing_enter",
        "mailing_confirm",
        "share_ask",
    ):
        return None

    target = str(session.filled.get("app_address_current") or "").strip()
    if not target:
        _set_address_phase(session, "")
        _clear_address_pending(session)
        return None

    names = [str(card.get("name") or "").strip() for card in cards if card.get("name")]
    named_hits, named_clarify = _match_named_members(text, names)
    if named_clarify:
        return _address_error(
            call_id, step, "address_member_ambiguous", named_clarify, session
        )
    if named_hits and named_hits[0].lower() != target.lower():
        # Different member named mid-walk — outer handler starts a fresh walk.
        _set_address_phase(session, "")
        _clear_address_pending(session)
        return None

    card = _card_by_name(cards, target) or {"name": target}
    field = "mailing" if "mailing" in phase else "home"
    if phase == "share_ask":
        field = str(session.filled.get("app_address_field") or "home")
    session.filled["app_address_field"] = field
    session.filled["app_address_correcting"] = target
    speak_as = _clean_member_display_name(target)
    looks_ok = bool(re.search(r"\blooks? (good|correct|fine)\b", lowered))
    leave_as_is = _address_leave_as_is(text)
    affirmed = bool(_is_yes(text) or looks_ok or leave_as_is)
    no_home = _means_no_home_address(text)
    no_mailing = _means_no_mailing_address(text)
    street, unit = _split_street_and_unit(text)
    commandish = bool(
        re.search(
            r"\b(yes|yeah|yep|no|nope|correct|change|cancel|continue|next|"
            r"address|home|mailing)\b",
            lowered,
        )
    )
    streetish = (not affirmed and not no_home and not no_mailing) and (
        _looks_like_street(street)
        or (len(re.findall(r"[A-Za-z]+", street)) >= 2 and not commandish)
        or (
            len(re.findall(r"[A-Za-z0-9]{3,}", street or "")) >= 1
            and not commandish
            and len(text.split()) <= 8
        )
    )

    # Shared-address popup after Save.
    if phase == "share_ask":
        if affirmed or _is_no(text):
            update_others = bool(affirmed)
            if not await _click_address_share_dialog_choice(
                page, update_others=update_others
            ):
                return _address_error(
                    call_id,
                    step,
                    "address_share_dialog_failed",
                    "I couldn't answer the shared-address popup. "
                    "Say Yes to update the other members, or No to only update yours.",
                    session,
                )
            await page.wait_for_timeout(300)
            if (await _address_share_dialog_info(page)).get("open"):
                return _address_error(
                    call_id,
                    step,
                    "address_share_dialog_failed",
                    "That shared-address popup is still open. "
                    "Say Yes to update the other members, or No to only update yours.",
                    session,
                )
            refreshed = await _scrape_address_cards(page)
            saved_card = _card_by_name(refreshed, target) or {"name": target}
            return await _advance_after_address_field_save(
                call_id, session, page, text, step, target, field, saved_card
            )
        share_members: list[str] = []
        try:
            loaded_share = json.loads(
                session.filled.get("app_address_share_members") or "[]"
            )
            if isinstance(loaded_share, list):
                share_members = [
                    str(item) for item in loaded_share if str(item).strip()
                ]
        except Exception:
            share_members = []
        return _address_error(
            call_id,
            step,
            "address_share_ask_needed",
            _address_share_ask_speech(
                {
                    "members": share_members,
                    "prompt": str(session.filled.get("app_address_share_prompt") or ""),
                }
            ),
            session,
        )

    # Matched-street Yes/No before Save.
    if phase in ("home_confirm", "mailing_confirm"):
        if affirmed:
            return await _commit_address_pending_and_advance(
                call_id, session, page, text, step, cards
            )
        if _is_no(text) or re.search(r"\b(change|wrong|incorrect|fix)\b", lowered):
            _clear_address_pending(session)
            back = "home_enter" if phase == "home_confirm" else "mailing_enter"
            _set_address_phase(session, back)
            return _app_ok(
                call_id,
                session,
                text,
                step,
                "address pending rejected",
                spoken_override=(
                    "Okay, I won't save that yet. "
                    + _address_enter_speech(target, field)
                ),
            )
        if streetish and street:
            return await _stage_matched_address_pending(
                call_id, session, page, text, step, target, field, street, unit
            )
        pending_kind = str(session.filled.get("app_address_pending_kind") or "street")
        return _address_error(
            call_id,
            step,
            "address_pending_confirm_needed",
            _address_pending_confirm_speech(
                target,
                field,
                kind=pending_kind,
                street=str(session.filled.get("app_address_pending_street") or ""),
                chosen=str(session.filled.get("app_address_pending_chosen") or ""),
            ),
            session,
        )

    # After "is it correct?" → No: ask Yes-to-change vs No Home/Mailing.
    if phase in ("home_change_ask", "mailing_change_ask"):
        if (field == "home" and no_home) or (field == "mailing" and no_mailing):
            return await _save_no_address_and_advance(
                call_id, session, page, text, step, target, field
            )
        if affirmed:
            enter = "home_enter" if field == "home" else "mailing_enter"
            _set_address_phase(session, enter)
            return _app_ok(
                call_id,
                session,
                text,
                step,
                "address enter started",
                spoken_override=_address_enter_speech(target, field),
            )
        if _is_no(text):
            # On this prompt, bare No means the No Home/Mailing Address choice.
            return await _save_no_address_and_advance(
                call_id, session, page, text, step, target, field
            )
        return _address_error(
            call_id,
            step,
            "address_change_ask_clarify",
            _address_change_ask_speech(target, field),
            session,
        )

    # Enter new street (or no-address).
    if phase in ("home_enter", "mailing_enter"):
        if (field == "home" and no_home) or (field == "mailing" and no_mailing):
            return await _save_no_address_and_advance(
                call_id, session, page, text, step, target, field
            )
        if streetish and street:
            return await _stage_matched_address_pending(
                call_id, session, page, text, step, target, field, street, unit
            )
        return _address_error(
            call_id,
            step,
            "address_new_value_needed",
            _address_enter_speech(target, field),
            session,
        )

    # home_check / mailing_check — only Yes keep or No → change ask.
    if (field == "home" and no_home) or (field == "mailing" and no_mailing):
        return await _save_no_address_and_advance(
            call_id, session, page, text, step, target, field
        )

    if affirmed:
        if field == "home":
            session.filled["app_address_field"] = "mailing"
            _clear_address_pending(session)
            _set_address_phase(session, "mailing_check")
            await _open_member_address_editor(page, target)
            return _app_ok(
                call_id,
                session,
                text,
                step,
                "address home kept",
                spoken_override=(
                    f"Okay, I'll keep the home address for {speak_as}. "
                    + _address_mailing_check_speech(card)
                ),
            )
        _set_address_phase(session, "")
        _clear_address_pending(session)
        session.filled.pop("app_address_field", None)
        session.filled.pop("app_address_correcting", None)
        session.filled.pop("app_address_snapshot", None)
        _mark_address_confirmed(session, target)
        session.filled.pop("app_address_current", None)
        try:
            if await _address_editor_is_open(page, target):
                await _click_member_address_action(page, target, "cancel")
        except Exception:
            pass
        next_step, spoken = await _prepare_address_review_speech(session, page)
        return _app_ok(
            call_id,
            session,
            text,
            next_step,
            "address mailing kept member done",
            spoken_override=(
                f"Okay, I'll keep the mailing address for {speak_as}. " + spoken
            ),
        )

    if _is_no(text) or re.search(r"\b(change|wrong|incorrect|fix|edit)\b", lowered):
        ask_phase = "home_change_ask" if field == "home" else "mailing_change_ask"
        _set_address_phase(session, ask_phase)
        await _open_member_address_editor(page, target)
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "address change ask",
            spoken_override=_address_change_ask_speech(target, field),
        )

    # Missing-address cards can accept a street directly on the check step.
    if streetish and street and (
        (field == "home" and card.get("needs_home"))
        or (field == "mailing" and card.get("needs_mailing"))
    ):
        return await _stage_matched_address_pending(
            call_id, session, page, text, step, target, field, street, unit
        )

    check_speech = (
        _address_home_check_speech(card)
        if field == "home"
        else _address_mailing_check_speech(card)
    )
    return _address_error(
        call_id,
        step,
        "address_field_check_clarify",
        check_speech,
        session,
    )


async def _handle_address_step(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    lowered: str,
    step: str,
) -> dict[str, Any]:
    """Review, edit, and save one member address card without advancing early."""
    from .live_form import _is_no, _is_yes

    cards = await _scrape_address_cards(page)
    names = [str(card.get("name") or "").strip() for card in cards if card.get("name")]
    missing = [name for name in names if (_card_by_name(cards, name) or {}).get("needs_add")]
    current = str(session.filled.get("app_address_current") or "").strip()

    phased = await _handle_address_field_phase(
        call_id, session, page, text, lowered, step, cards
    )
    if phased is not None:
        return phased

    looks_ok = bool(re.search(r"\blooks? (good|correct|fine)\b", lowered))
    leave_as_is = _address_leave_as_is(text)
    wants_next = bool(
        _is_yes(text)
        or looks_ok
        or leave_as_is
        or re.search(r"\b(continue|next)\b", lowered)
    )
    # Bare "No" / "incorrect" / "Address. Name" starts correction; not Next.
    named_hits_preview, _ = _match_named_members(text, names)
    address_name_phrase = bool(
        re.match(r"^\s*address\b", lowered)
        and named_hits_preview
    )
    wants_change = bool(
        (
            _is_no(text)
            and not leave_as_is
            and not looks_ok
            and not re.search(r"\bno (?:home|mailing) address\b", lowered)
        )
        or address_name_phrase
        or (
            re.search(
                r"\b(change|wrong|incorrect|fix|edit|different|update|nope)\b",
                lowered,
            )
            and not looks_ok
            and not leave_as_is
        )
    )
    # "correct" alone in "looks correct" is confirmation, not a change request.
    if wants_change and re.search(r"\bcorrect\b", lowered) and not re.search(
        r"\b(change|wrong|incorrect|fix|edit|different|update|nope)\b", lowered
    ):
        if _is_yes(text) or looks_ok:
            wants_change = False

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
                session,
            )
        if not await _click_member_address_action(page, target, "cancel"):
            logger.warning("address_cancel_failed")
            return _address_error(
                call_id,
                step,
                "address_cancel_failed",
                f"I couldn't cancel the address edit for {target}.",
                session,
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
                    session,
                )
        elif after.get("expanded"):
            logger.warning("address_cancel_still_open")
            return _address_error(
                call_id,
                step,
                "address_cancel_failed",
                f"Cancel did not close the address edit for {target}.",
                session,
            )
        session.filled.pop("app_address_current", None)
        session.filled.pop("app_address_field", None)
        session.filled.pop("app_address_snapshot", None)
        session.filled.pop("app_address_correcting", None)
        _set_address_phase(session, "")
        _clear_address_pending(session)
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "address edit cancelled",
            spoken_override=(
                f"I cancelled that change for {_clean_member_display_name(target)}. "
                "Nothing new was saved. "
                + _address_confirm_speech(after or {"name": target}, after_cards)
            ),
        )

    no_home = _means_no_home_address(text)
    no_mailing = _means_no_mailing_address(text)
    if no_home or no_mailing:
        # Explicit no-address choice: checkbox + Save immediately.
        name_text = re.sub(
            r"\bno[,.]?\s*(?:home|mailing|omar|ohm|own|holm|homee)"
            r"(?:\s+address)?\b",
            " ",
            text,
            flags=re.I,
        )
        hits, clarify = _match_named_members(name_text, names)
        target = hits[0] if len(hits) == 1 else current
        if clarify:
            return _address_error(
                call_id, step, "address_member_ambiguous", clarify, session
            )
        if not target:
            if len(names) == 1:
                target = names[0]
            else:
                return _address_error(
                    call_id,
                    step,
                    "address_member_needed",
                    "Whose card should get that no-address choice? Please say the name.",
                    session,
                )
        prior = _card_by_name(cards, target) or {"name": target}
        session.filled["app_address_snapshot"] = json.dumps(
            {
                "name": target,
                "home": str(prior.get("home") or ""),
                "mailing": str(prior.get("mailing") or ""),
            }
        )
        field = "home" if no_home else "mailing"
        return await _save_no_address_and_advance(
            call_id, session, page, text, step, target, field
        )

    street, unit = _split_street_and_unit(text)
    if _looks_like_street(street):
        hits, clarify = _match_named_members(text, names)
        pinned = str(session.filled.get("app_address_correcting") or "").strip()
        # While a correction is pinned, do not retarget from ASR name noise in
        # the street utterance. Explicit new named corrections use wants_change.
        if pinned:
            target = pinned
        else:
            if clarify:
                return _address_error(
                    call_id, step, "address_member_ambiguous", clarify, session
                )
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
                    session,
                )
        field = _address_field_from_text(text) or str(
            session.filled.get("app_address_field") or ""
        )
        card = _card_by_name(cards, target) or {}
        if field == "both":
            field = "home"
        if not field:
            if card.get("needs_home") and not card.get("needs_mailing"):
                field = "home"
            elif card.get("needs_mailing") and not card.get("needs_home"):
                field = "mailing"
            else:
                # Default into the home→mailing walk; stage street as home pending.
                field = "home"
        started = await _begin_address_home_check(
            call_id, session, page, text, step, target, cards
        )
        if not started.get("ok"):
            return started
        # Editing invalidates a prior card-level Yes for this member.
        confirmed_now = [
            name
            for name in _address_confirmed_names(session)
            if name.lower() != target.lower()
        ]
        session.filled["app_address_confirmed"] = json.dumps(confirmed_now)
        # Match against the Places dropdown before asking Yes to save.
        return await _stage_matched_address_pending(
            call_id, session, page, text, step, target, field, street, unit
        )

    bare_field = _address_field_from_text(text)
    if (
        current
        and bare_field
        and not wants_next
        and not _looks_like_street(street)
        and len(text.split()) <= 8
        and not re.search(r"\d", text)
    ):
        # "mailing" / "home address" while a member is already active → field check.
        started = await _begin_address_home_check(
            call_id, session, page, text, step, current, cards
        )
        if not started.get("ok"):
            return started
        card = _card_by_name(cards, current) or {"name": current}
        if bare_field == "mailing":
            session.filled["app_address_field"] = "mailing"
            _set_address_phase(session, "mailing_check")
            speech = _address_mailing_check_speech(card)
        else:
            # home or both → start with home, then mailing.
            session.filled["app_address_field"] = "home"
            _set_address_phase(session, "home_check")
            speech = _address_home_check_speech(card)
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "address field check started",
            spoken_override=speech,
        )

    attempted_labels = _extract_spoken_member_labels(text)
    if wants_change or (not wants_next and named_hits_preview):
        hits, clarify = _match_named_members(text, names)
        if clarify:
            return _address_error(
                call_id, step, "address_member_ambiguous", clarify, session
            )
        field = bare_field
        target = hits[0] if len(hits) == 1 else ""
        # Explicit spoken name always wins over the current review member.
        if not target and attempted_labels and names:
            # Name-like utterance that did not resolve — never silently retarget.
            return _address_error(
                call_id,
                step,
                "address_member_needed",
                "I couldn't match that name to a household member. "
                "Please say the name exactly as it appears on the application.",
                session,
            )
        if not target and current and (
            _is_no(text)
            or (
                field
                and not attempted_labels
                and not re.search(
                    r"[A-Za-z]",
                    re.sub(
                        r"\b(no|change|wrong|incorrect|fix|edit|different|update|"
                        r"home|mailing|address|both)\b",
                        " ",
                        text,
                        flags=re.I,
                    ),
                )
            )
        ):
            # Bare "No" / field-only reply while reviewing a known member.
            target = current
        if not target:
            if len(names) == 1 and (_is_no(text) or field or wants_change) and not attempted_labels:
                target = names[0]
            else:
                return _address_error(
                    call_id,
                    step,
                    "address_member_needed",
                    "Whose address should I change? Please say the name.",
                    session,
                )
        started = await _begin_address_home_check(
            call_id, session, page, text, step, target, cards
        )
        if not started.get("ok"):
            return started
        if field == "mailing":
            session.filled["app_address_field"] = "mailing"
            _set_address_phase(session, "mailing_change_ask")
            return _app_ok(
                call_id,
                session,
                text,
                step,
                "address mailing change ask started",
                spoken_override=(
                    f"Okay, I'll change that for {_clean_member_display_name(target)}. "
                    + _address_change_ask_speech(target, "mailing")
                ),
            )
        # Default: home first, then mailing.
        return started

    if wants_next and missing:
        shown = ", ".join(missing[:4])
        return _address_error(
            call_id,
            step,
            "address_incomplete",
            "Pointcare still needs an address for "
            f"{shown}. Say the street address, or say "
            "No Home Address or No Mailing Address if that applies.",
            session,
        )

    confirmed = {name.lower() for name in _address_confirmed_names(session)}
    # Never mark a member confirmed from a bare Yes here — home/mailing phases
    # handle Yes (home → mailing → confirmed). If phase was lost, restart home.
    if wants_next and current and current.lower() not in confirmed:
        card = _card_by_name(cards, current) or {"name": current}
        speech = _start_member_home_review(session, card)
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "address home check restarted",
            spoken_override=speech,
        )
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
                session,
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
                session,
            )
        screen = await _advance_after_answer(
            page, session, allow_extra_next=False, leave_step=step
        )
        if not screen or screen == step:
            # Address cards gone ⇒ Next already advanced despite lagging detect.
            try:
                still_cards = await _scrape_address_cards(page)
            except Exception:
                still_cards = [{"name": "_"}]  # treat scrape failure as still here
            screen = await _wait_for_screen_change(
                page, step, timeout_s=4.0 if not still_cards else 2.5
            )
        if not screen or screen == step:
            logger.warning("address_screen_unchanged")
            return _address_error(
                call_id,
                step,
                "address_still_here",
                "The address screen is still open, so I did not move ahead.",
                session,
            )
        session.filled.pop("app_address_ready", None)
        session.filled.pop("app_address_current", None)
        session.filled.pop("app_address_field", None)
        session.filled.pop("app_address_correcting", None)
        session.filled.pop("app_address_confirmed", None)
        try:
            screen, spoken = await _prepare_resume_speech(session, page, screen)
        except Exception:
            logger.exception("prepare speech after address failed step=%s", screen)
            spoken = app_question_for_step(screen, session)
        return _app_ok(
            call_id,
            session,
            text,
            screen,
            "address review advanced",
            spoken_override=spoken,
        )
    if wants_next and not pending and names:
        session.filled["app_address_ready"] = "1"
        session.filled.pop("app_address_current", None)
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
        card = _card_by_name(cards, nxt) or {"name": nxt}
        speech = _start_member_home_review(session, card)
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "address confirm one member",
            spoken_override=speech,
        )
    if attempted_labels and names and not wants_next:
        # Caller named someone that did not resolve — never fall back to another card.
        return _address_error(
            call_id,
            step,
            "address_member_needed",
            "I couldn't match that name to a household member. "
            "Please say the name exactly as it appears on the application.",
            session,
        )
    if current:
        card = _card_by_name(cards, current) or {"name": current}
        speech = _start_member_home_review(session, card)
        return _address_error(
            call_id,
            step,
            "address_clarify",
            speech,
            session,
        )
    # No current member yet — start the review walk with home for the first card.
    if names:
        card = _card_by_name(cards, names[0]) or {"name": names[0]}
        speech = _start_member_home_review(session, card)
        return _address_error(
            call_id,
            step,
            "address_clarify",
            speech,
            session,
        )
    return _address_error(
        call_id,
        step,
        "address_clarify",
        app_question_for_step(step, session),
        session,
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


def _immigration_left_citizenship(screen: str) -> bool:
    return bool(screen) and screen not in (
        "app_citizenship",
        "app_immigration",
        "app_immigration_who",
    )


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
                f"{current}. "
                + _immigration_status_question_for(current, session=session)
            ),
            "error": "immigration_incomplete",
            "step": "app_immigration",
            "phase": "application",
        }
    if session.filled.get("app_immigration_next_clicked") == "1":
        # Already clicked once — poll for a delayed screen change; never re-click.
        screen = ""
        for _ in range(12):
            screen = await detect_application_screen(page)
            if _immigration_left_citizenship(screen or ""):
                break
            await page.wait_for_timeout(150)
        if _immigration_left_citizenship(screen or ""):
            step, speech = await _prepare_resume_speech(session, page, screen)
            session.app_step = step
            session.filled.pop("app_citizenship_no", None)
            session.filled.pop("app_immigration_next_clicked", None)
            _set_immigration_queue(session, None)
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
        logger.info("immigration_next_unchanged after prior click")
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "immigration_next_unchanged",
            "message": "duplicate_click_prevented",
            "step": "app_immigration",
            "phase": "application",
        }

    next_info = await _wait_immigration_next_ready(page)
    status = str(next_info.get("status") or "next_not_found")
    if status != "next_enabled":
        validation = await _visible_form_validation(page)
        if status == "next_disabled":
            speech = (
                "Next is still disabled after every person's status was verified "
                "on the form. I did not click it."
            )
            error = "immigration_next_disabled"
        elif status == "next_hidden":
            speech = (
                "I can see a Next control, but it is hidden on this screen, "
                "so I did not click it."
            )
            error = "immigration_next_hidden"
        else:
            speech = (
                "I could not find a Next button on this screen after every "
                "person's status was verified. I did not click anything."
            )
            error = "immigration_next_not_found"
        if validation:
            speech = f"{speech} The form says: {validation}"
        logger.info(
            "immigration_next_not_ready status=%s tag=%s native_disabled=%s "
            "aria_disabled=%s class_disabled=%s class_name=%s "
            "visible_enabled=%s visible_disabled=%s hidden=%s validation=%s",
            status,
            next_info.get("tag") or "-",
            int(bool(next_info.get("native_disabled"))),
            next_info.get("aria_disabled") or "-",
            int(bool(next_info.get("class_disabled"))),
            (next_info.get("class_name") or "-")[:80],
            next_info.get("visible_enabled"),
            next_info.get("visible_disabled"),
            next_info.get("hidden"),
            "1" if validation else "0",
        )
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": error,
            "message": status,
            "step": "app_immigration",
            "phase": "application",
        }

    clicked = await _click_enabled_application_next(page)
    if not clicked:
        # Re-check: may have flipped disabled between poll and click.
        again = await _inspect_application_next(page)
        logger.info(
            "immigration_next_click_failed status=%s",
            again.get("status"),
        )
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: I could not click the enabled Next button.",
            "error": "immigration_next_click_failed",
            "message": str(again.get("status") or "click_failed"),
            "step": "app_immigration",
            "phase": "application",
        }
    # Mark before waiting so a retry cannot double-click.
    session.filled["app_immigration_next_clicked"] = "1"
    screen = ""
    for _ in range(16):
        screen = await detect_application_screen(page)
        if _immigration_left_citizenship(screen or ""):
            break
        await page.wait_for_timeout(150)
    if not _immigration_left_citizenship(screen or ""):
        validation = await _visible_form_validation(page)
        speech = "I clicked Next, but the screen did not change."
        if validation:
            speech = f"{speech} The form says: {validation}"
        logger.info("immigration_next_unchanged after click")
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
    session.filled.pop("app_immigration_phase", None)
    session.filled.pop("app_immigration_options_listed", None)
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
            selected = await click_exact_visible_text(page, "Yes, connect me")
            if not selected:
                try:
                    await _click_exact_choice(page, "Yes, connect me")
                    selected = True
                except Exception as exc:
                    return _yes_no_click_failure(call_id, session, step, text, exc)
        elif _is_no(text):
            selected = False
            for label in ("No thanks", "No"):
                if await click_exact_visible_text(page, label):
                    selected = True
                    break
            if not selected:
                try:
                    await _click_exact_choice(page, "No thanks")
                    selected = True
                except Exception as exc:
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
        advanced = await _finish_agent_offer(
            page, wants_connect=_is_yes(text)
        )
        if not advanced:
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": (
                    "ASK: I selected your answer, but I couldn't continue from "
                    "the licensed-agent screen. Please say Yes or No to try again."
                ),
                "error": "agent_offer_continue_failed",
                "step": step,
                "phase": "application",
            }
        next_step = await _advance_after_answer(
            page, session, allow_extra_next=False, leave_step=step
        )

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
            # One Next only. A second automatic Next in _advance_after_answer
            # previously skipped insurance (Q2) and landed on DOB (Q3).
            live = await detect_application_screen(page)
            if not (live and live.startswith("app_") and live != step):
                await _click_next_or_continue(page)
            next_step = await _advance_after_answer(
                page, session, allow_extra_next=False, leave_step=step
            )
            if next_step in ("", step, "programs"):
                logger.warning(
                    "citizenship_yes_next_screen call_id=%s next=%s",
                    call_id,
                    next_step or "-",
                )
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
            session.filled.pop("app_immigration_phase", None)
            session.filled.pop("app_immigration_options_listed", None)
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
                    "Okay. I'll set each person's citizenship or immigration "
                    "status. "
                    + _immigration_status_question_for(current, session=session)
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
            session.filled.pop("app_immigration_phase", None)
            session.filled.pop("app_immigration_options_listed", None)
            next_step = "app_immigration"
            spoken_override = _immigration_status_question_for(
                current, session=session
            )
            _set_pending_question(session, next_step, spoken_override)

    elif step == "app_immigration":
        live_members = await _scrape_immigration_members(page)
        listed_names = [
            str(row.get("name") or "").strip()
            for row in live_members
            if str(row.get("name") or "").strip()
        ]
        confirmed_map = _immigration_confirmed(session)
        confirmed = {
            name.lower(): status for name, status in confirmed_map.items()
        }
        phase = str(session.filled.get("app_immigration_phase") or "").strip().lower()
        wants_yes = bool(
            _is_yes(text)
            or re.search(
                r"\b(continue|next|done|that's all|finished|"
                r"looks? (?:good|correct)|that(?:'s| is) correct)\b",
                lowered,
            )
        )
        wants_no = bool(
            _is_no(text)
            or re.search(r"\b(change|wrong|incorrect|fix|edit)\b", lowered)
        )

        if phase == "confirm_all":
            if wants_yes and not wants_no:
                session.filled.pop("app_immigration_phase", None)
                return await _complete_immigration_step(
                    call_id, session, page, text
                )
            hits, who_clarify = _match_named_members(text, listed_names)
            if who_clarify:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + who_clarify,
                    "error": "immigration_member_ambiguous",
                    "step": step,
                    "phase": "application",
                }
            who = hits[0] if len(hits) == 1 else ""
            picked = _map_immigration_status(lowered)
            if picked and who:
                # Fall through to the normal set/verify path below.
                session.filled.pop("app_immigration_phase", None)
                session.filled["app_immigration_current"] = who
            elif picked and not who:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: Please say which household member that status "
                        "is for."
                    ),
                    "error": "immigration_confirm_who",
                    "step": step,
                    "phase": "application",
                }
            elif wants_no or who:
                if who:
                    session.filled["app_immigration_current"] = who
                    session.filled.pop("app_immigration_phase", None)
                    speech = _immigration_status_question_for(
                        who, session=session
                    )
                    _set_pending_question(session, step, speech)
                    return _app_ok(
                        call_id,
                        session,
                        text,
                        step,
                        "immigration confirm fix member",
                        spoken_override=speech,
                    )
                speech = (
                    "Whose status should I change? Please say the name."
                )
                _set_pending_question(session, step, speech)
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + speech,
                    "error": "immigration_confirm_who",
                    "step": step,
                    "phase": "application",
                }
            else:
                speech = _immigration_confirm_all_speech(confirmed_map)
                _set_pending_question(session, step, speech)
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + speech,
                    "error": "immigration_confirm_clarify",
                    "step": step,
                    "phase": "application",
                }

        if re.search(
            r"\b(continue|next|done|that's all|finished)\b", lowered
        ) and not _map_immigration_status(lowered):
            # Never skip the one-shot everyone confirm once all are set.
            remaining_check = [
                name
                for name in (listed_names or _immigration_queue(session))
                if name.lower() not in confirmed
            ]
            if not remaining_check and confirmed_map:
                session.filled["app_immigration_phase"] = "confirm_all"
                session.filled.pop("app_immigration_current", None)
                speech = _immigration_confirm_all_speech(confirmed_map)
                _set_pending_question(session, step, speech)
                return _app_ok(
                    call_id,
                    session,
                    text,
                    step,
                    "immigration confirm all",
                    spoken_override=speech,
                )
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
                _immigration_member_name_key(str(row.get("name") or "")): str(
                    row.get("selected") or ""
                )
                for row in live_members
                if str(row.get("name") or "").strip()
            }
            ok, set_reason = await _set_member_immigration_status(page, target, picked)
            if not ok:
                logger.info(
                    "immigration_status_handler reason=%s error=immigration_status_not_set",
                    set_reason,
                )
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: I couldn't set that status on the form. "
                        + _immigration_status_question_for(
                            target, session=session
                        )
                    ),
                    "error": "immigration_status_not_set",
                    "message": set_reason,
                    "step": step,
                    "phase": "application",
                }
            if set_reason != "already_selected":
                await page.wait_for_timeout(250)
            verified, verify_reason = await _verify_member_immigration_status(
                page, target, picked
            )
            if not verified:
                logger.info(
                    "immigration_status_handler reason=%s error=immigration_status_not_verified",
                    verify_reason,
                )
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: I couldn't verify that status was selected. "
                        + _immigration_status_question_for(
                            target, session=session
                        )
                    ),
                    "error": "immigration_status_not_verified",
                    "message": verify_reason,
                    "step": step,
                    "phase": "application",
                }
            after_members = await _scrape_immigration_members(page)
            target_key = _immigration_member_name_key(target)
            for row in after_members:
                other = str(row.get("name") or "").strip()
                other_key = _immigration_member_name_key(other)
                if not other_key or other_key == target_key:
                    continue
                if str(row.get("selected") or "") != before.get(other_key, ""):
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": (
                            "ASK: That selection changed another person's answer. "
                            + _immigration_status_question_for(
                                target, session=session
                            )
                        ),
                        "error": "immigration_other_member_changed",
                        "step": step,
                        "phase": "application",
                    }
            _mark_immigration_confirmed(session, target, picked)
            confirmed_map = _immigration_confirmed(session)
            confirmed = {
                name.lower(): status for name, status in confirmed_map.items()
            }
            remaining = [
                name
                for name in (listed_names or _immigration_queue(session))
                if name.lower() not in confirmed
            ]
            _set_immigration_queue(session, remaining or None)
            if remaining:
                nxt = remaining[0]
                session.filled["app_immigration_current"] = nxt
                session.filled.pop("app_immigration_phase", None)
                next_step = "app_immigration"
                spoken_override = (
                    f"Okay, {_immigration_status_display(picked)} for {target}. "
                    + _immigration_status_question_for(nxt, session=session)
                )
                _set_pending_question(session, next_step, spoken_override)
            else:
                session.filled.pop("app_immigration_current", None)
                session.filled["app_immigration_phase"] = "confirm_all"
                next_step = "app_immigration"
                spoken_override = _immigration_confirm_all_speech(confirmed_map)
                _set_pending_question(session, next_step, spoken_override)

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
            screen = await detect_application_screen(page)
            if screen and screen not in ("", "app_insurance", "programs"):
                next_step = screen
            else:
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
        async def _start_coverage_for(names: list[str]) -> tuple[str, str] | dict[str, Any]:
            """Expand the first member card and ask which plans they qualify for."""
            ordered = _merge_insurance_queue(session, names)
            if not ordered:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + _who_has_coverage_question(available),
                    "error": "insurance_members_incomplete",
                    "step": step,
                    "phase": "application",
                }
            first = ordered[0]
            expanded = await _ensure_member_coverage_expanded(page, first)
            if not expanded:
                logger.info(
                    "insurance_member_expand_failed call_id=%s member=%r",
                    call_id,
                    first,
                )
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: I couldn't open coverage options for "
                        f"{_clean_member_display_name(first)}. "
                        "Please say that name again. "
                        + _who_has_coverage_question(available)
                    ),
                    "error": "insurance_members_not_verified",
                    "step": step,
                    "phase": "application",
                }
            return _begin_insurance_coverage_collect(session, first)

        if re.search(r"\b(continue|next|done|that's all|finished)\b", lowered) or _is_yes(
            text
        ):
            selected = [
                str(row.get("name") or "").strip()
                for row in rows
                if row.get("checked") and str(row.get("name") or "").strip()
            ]
            if not selected:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + _who_has_coverage_question(available),
                    "error": "insurance_members_incomplete",
                    "step": step,
                    "phase": "application",
                }
            begun = await _start_coverage_for(selected)
            if isinstance(begun, dict):
                return begun
            next_step, spoken_override = begun
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
            # The member row is an accordion. Its nested checkbox is only a
            # status indicator and remains false until a plan is selected.
            # Click the exact member-name trigger and verify its own branch.
            begun = await _start_coverage_for(hits)
            if isinstance(begun, dict):
                return begun
            next_step, spoken_override = begun

    elif step == "app_insurance_coverage_type":
        current = (session.filled.get("app_insurance_current") or "").strip()
        queue = _insurance_queue(session)
        phase = (
            str(session.filled.get("app_insurance_coverage_phase") or "collect")
            .strip()
            .lower()
        )
        known_types = _insurance_current_types(session)

        async def _confirm_member_and_advance(
            confirmed: list[str],
        ) -> tuple[str, str] | dict[str, Any]:
            label = _clean_member_display_name(current or "that person")
            spoken_types = _insurance_types_for_speech(
                confirmed, _insurance_other_text(session)
            )
            listed = (
                ", ".join(spoken_types) if spoken_types else "the selected coverage"
            )
            if current:
                _mark_insurance_member_completed(session, current)
            remaining = (
                [n for n in queue if n.lower() != (current or "").lower()]
                if current
                else list(queue)
            )
            _set_insurance_queue(session, remaining or None)
            _set_insurance_current_types(session, None)
            _set_insurance_other_text(session, None)
            session.filled.pop("app_insurance_coverage_phase", None)
            if remaining:
                nxt = remaining[0]
                expanded = await _ensure_member_coverage_expanded(page, nxt)
                if not expanded:
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": (
                            "ASK: I confirmed "
                            f"{listed} for {label}, but I couldn't open coverage "
                            f"options for {_clean_member_display_name(nxt)}. "
                            "Please say that name again."
                        ),
                        "error": "insurance_members_not_verified",
                        "step": "app_insurance_members",
                        "phase": "application",
                    }
                cov_step, cov_speech = _begin_insurance_coverage_collect(session, nxt)
                speech = (
                    f"Confirming {listed} for {label}. " + cov_speech
                )
                _set_pending_question(session, cov_step, speech)
                return cov_step, speech
            session.filled.pop("app_insurance_current", None)
            session.filled["app_insurance_coverage_phase"] = "members_more"
            speech = (
                f"Confirming {listed} for {label}. "
                "Does any other household member have insurance or health "
                "coverage? Say their name, or say No if no one else does."
            )
            _set_pending_question(session, step, speech)
            return step, speech

        done_phrase = bool(
            re.search(r"\b(continue|next|done|that's all|finished)\b", lowered)
        )
        if phase == "other_specify":
            target = current or "that person"
            if _is_no(text) or done_phrase:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: I still need the name of that other insurance. "
                        + _insurance_other_specify_question(target)
                    ),
                    "error": "insurance_other_specify_needed",
                    "step": step,
                    "phase": "application",
                }
            # Free-text plan name — do not remap onto Medicare/Other chips.
            if not current:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: Whose other insurance is that? "
                        + app_question_for_step("app_insurance_members")
                    ),
                    "error": "insurance_coverage_member_needed",
                    "step": "app_insurance_members",
                    "phase": "application",
                }
            filled = await _fill_member_coverage_other_specify(page, current, text)
            if not filled.get("ok"):
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: I couldn't enter that in Please specify. "
                        + _insurance_other_specify_question(current)
                    ),
                    "error": "insurance_other_specify_failed",
                    "step": step,
                    "phase": "application",
                }
            custom = str(filled.get("value") or text).strip()
            _set_insurance_other_text(session, custom)
            if "Other" not in known_types:
                _set_insurance_current_types(session, known_types + ["Other"])
            session.filled["app_insurance_coverage_phase"] = "more"
            spoken_override = _insurance_more_plans_question_for(
                current,
                _insurance_types_for_speech(
                    _insurance_current_types(session), custom
                ),
            )
            next_step = "app_insurance_coverage_type"
            _set_pending_question(session, next_step, spoken_override)
        elif phase == "members_more":
            if _is_no(text):
                enabled = await _next_button_enabled(page)
                if enabled is False:
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": (
                            "ASK: Pointcare still needs coverage details before "
                            "I can continue. Does any other household member have "
                            "insurance? Please say their name, or say No."
                        ),
                        "error": "insurance_coverage_next_disabled",
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
                            "Please say No again if no other household member "
                            "has insurance."
                        ),
                        "error": "insurance_coverage_next_failed",
                        "step": step,
                        "phase": "application",
                    }
                session.filled.pop("app_insurance_coverage_phase", None)
                session.filled.pop("app_insurance_completed", None)
                # The collect substep is app_insurance_coverage_type, while
                # detect_application_screen reports its parent app_insurance
                # during navigation. Treat the whole insurance page as the
                # screen being left so we do not repeat Q2 before Q3 renders.
                next_step = await _advance_after_answer(
                    page, session, leave_step="app_insurance"
                )
            else:
                members = await _scrape_coverage_members(page)
                available = [
                    str(row.get("name") or "").strip()
                    for row in members
                    if str(row.get("name") or "").strip()
                ]
                hits, clarify = _match_named_members(text, available)
                if _is_yes(text) and not hits:
                    clarify = (
                        "Please say the name of the other household member who "
                        "has insurance."
                    )
                if clarify or not hits:
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": (
                            "ASK: "
                            + (
                                clarify
                                or "Say the other member's name, or say No if "
                                "no one else has insurance."
                            )
                        ),
                        "error": "insurance_other_member_clarify",
                        "step": step,
                        "phase": "application",
                    }
                completed = {
                    name.lower() for name in _insurance_completed_members(session)
                }
                new_hits = [name for name in hits if name.lower() not in completed]
                if not new_hits:
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": (
                            "ASK: I already confirmed coverage for that member. "
                            "Does any other household member have insurance? Say "
                            "their name, or say No."
                        ),
                        "error": "insurance_member_already_confirmed",
                        "step": step,
                        "phase": "application",
                    }
                _set_insurance_queue(session, new_hits)
                nxt = new_hits[0]
                expanded = await _ensure_member_coverage_expanded(page, nxt)
                if not expanded:
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": (
                            "ASK: I couldn't open coverage options for "
                            f"{_clean_member_display_name(nxt)}. Please say that "
                            "name again."
                        ),
                        "error": "insurance_members_not_verified",
                        "step": "app_insurance_members",
                        "phase": "application",
                    }
                next_step, spoken_override = _begin_insurance_coverage_collect(
                    session, nxt
                )
        elif phase == "more" and (_is_no(text) or done_phrase):
            if not current:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: Whose coverage should I confirm? "
                        + app_question_for_step("app_insurance_members")
                    ),
                    "error": "insurance_coverage_member_needed",
                    "step": "app_insurance_members",
                    "phase": "application",
                }
            if not known_types:
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
            if any(t.lower() == "other" for t in known_types) and not _insurance_other_text(
                session
            ):
                session.filled["app_insurance_coverage_phase"] = "other_specify"
                ask = _insurance_other_specify_question(current)
                _set_pending_question(session, step, ask)
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + ask,
                    "error": "insurance_other_specify_needed",
                    "step": step,
                    "phase": "application",
                }
            advanced = await _confirm_member_and_advance(known_types)
            if isinstance(advanced, dict):
                return advanced
            next_step, spoken_override = advanced
        elif phase != "more" and (done_phrase or _is_no(text)):
            # Still collecting the first plan for this member.
            target = current or "that person"
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": (
                    "ASK: I still need a coverage type for "
                    f"{_clean_member_display_name(target)}. "
                    + _insurance_coverage_question_for(target)
                ),
                "error": "insurance_coverage_incomplete",
                "step": step,
                "phase": "application",
            }
        elif phase == "more" and _is_yes(text) and not _map_coverage_types(lowered):
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": (
                    "ASK: Please say the other plan name for "
                    f"{_clean_member_display_name(current or 'them')} — "
                    "Medicare, Medicaid, health insurance through a job, "
                    "TRICARE, VA Health Care, private insurance or ACA "
                    "Marketplace, or Other."
                ),
                "error": "insurance_coverage_more_clarify",
                "step": step,
                "phase": "application",
            }
        else:
            types = _map_coverage_types(lowered)
            if not types:
                target = current or "that person"
                ask = (
                    _insurance_more_plans_question_for(target, known_types)
                    if phase == "more" and known_types
                    else _insurance_coverage_question_for(target)
                )
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + ask,
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
            # Keep the member card expanded before choosing plan checkboxes.
            await _ensure_member_coverage_expanded(page, current)
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
            if not verified:
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
            merged_types = known_types + types
            _set_insurance_current_types(session, merged_types)
            session.filled["app_insurance_current"] = current
            others = [n for n in queue if n.lower() != current.lower()]
            if current.lower() not in {n.lower() for n in queue}:
                _set_insurance_queue(session, [current] + others)
            # Other requires the Please-specify free-text name before "more".
            if any(t.lower() == "other" for t in types):
                _set_insurance_other_text(session, None)
                session.filled["app_insurance_coverage_phase"] = "other_specify"
                spoken_override = _insurance_other_specify_question(current)
            else:
                session.filled["app_insurance_coverage_phase"] = "more"
                spoken_override = _insurance_more_plans_question_for(
                    current,
                    _insurance_types_for_speech(
                        _insurance_current_types(session),
                        _insurance_other_text(session),
                    ),
                )
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
        available = [
            str(c.get("name") or "").strip()
            for c in cards
            if str(c.get("name") or "").strip()
        ]
        current_review = str(session.filled.get("app_dob_current") or "").strip()
        phase = _dob_phase(session)
        walk_done = bool(session.filled.get("app_dob_walk_complete"))

        async def _dob_start_correction(
            targets: list[str],
            *,
            change_lead: bool = False,
        ) -> dict[str, Any]:
            # A new correction means the walk is no longer complete.
            session.filled.pop("app_dob_walk_complete", None)
            # Allow re-collecting this member even if they were reviewed before.
            for name in targets:
                key = (name or "").strip().lower()
                if not key:
                    continue
                names = _dob_reviewed_names(session)
                if key in names:
                    names.discard(key)
                    if names:
                        session.filled["app_dob_reviewed"] = json.dumps(
                            sorted(names)
                        )
                    else:
                        session.filled.pop("app_dob_reviewed", None)
            nxt, speech = _begin_dob_collect_for(
                session, targets, change_lead=change_lead
            )
            # Correction of an existing card — open that member's editor only.
            await _open_dob_editor_for_prompt(session, page, targets[0], cards)
            return _app_ok(
                call_id,
                session,
                text,
                nxt,
                "dob correction started",
                spoken_override=speech,
            )

        async def _dob_click_next_once() -> dict[str, Any] | None:
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
            return None

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

        wants_correction = bool(
            _is_no(text)
            or re.search(
                r"\b(change|wrong|incorrect|fix|edit|nope)\b", lowered
            )
        )
        hits, clarify = _match_named_members(text, available)
        if clarify and (
            wants_correction
            or bool(hits)
            or bool(_extract_spoken_member_labels(text))
        ):
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + clarify,
                "error": "dob_member_ambiguous",
                "step": step,
                "phase": "application",
            }

        # Named correction (e.g. "No, change John D.'s DOB" or bare "Agent Edge").
        if wants_correction:
            if hits:
                return await _dob_start_correction(hits, change_lead=True)
            # Reviewing one member and caller said No — change that person now.
            if (
                current_review
                and phase == "review"
                and not walk_done
                and not _extract_spoken_member_labels(text)
            ):
                return await _dob_start_correction(
                    [current_review], change_lead=True
                )
            spoken_labels = [
                label
                for label in _extract_spoken_member_labels(text)
                if label.strip().lower()
                not in {
                    "no",
                    "nope",
                    "nah",
                    "incorrect",
                    "wrong",
                    "change",
                    "fix",
                    "edit",
                }
            ]
            if spoken_labels and available:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: I couldn't match that name to anyone on the "
                        "application. " + _dob_ask_who_question()
                    ),
                    "error": "dob_member_unknown",
                    "step": step,
                    "phase": "application",
                }
            spoken_override = _dob_ask_who_question()
            _set_dob_phase(session, "review")
            return _app_ok(
                call_id,
                session,
                text,
                step,
                "dob change name needed",
                spoken_override=spoken_override,
            )

        if hits and not _dob_explicit_confirm(text) and not _dob_wants_continue(text):
            return await _dob_start_correction(hits)

        name_like = bool(_extract_spoken_member_labels(text))
        looks_like_name = bool(
            re.fullmatch(
                r"[A-Za-z][A-Za-z'.\-]*(?:\s+[A-Za-z][A-Za-z'.\-]*){0,3}\.?",
                (text or "").strip(),
            )
        )
        if (
            (name_like or looks_like_name)
            and not _dob_explicit_confirm(text)
            and not _dob_wants_continue(text)
            and not _is_yes(text)
        ):
            targets = hits or _extract_spoken_member_labels(text)
            if targets:
                # Unknown name that didn't match household → clarify, don't edit.
                if not hits and available:
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": (
                            "ASK: I couldn't match that name to anyone on the "
                            "application. " + _dob_ask_who_question()
                        ),
                        "error": "dob_member_unknown",
                        "step": step,
                        "phase": "application",
                    }
                return await _dob_start_correction(targets)

        # Confirm existing DOB for the member currently under review.
        reviewing_member = bool(
            current_review and phase == "review" and not walk_done
        )
        if reviewing_member and (
            _dob_explicit_confirm(text)
            or _is_yes(text)
            or _dob_leave_as_is(text)
            or _dob_wants_continue(text)
        ):
            _mark_dob_reviewed(session, current_review)
            session.filled.pop("app_dob_current", None)
            session.filled.pop("app_dob_correcting", None)
            _clear_dob_component(session)
            _clear_dob_partial(session)
            next_step, spoken_override = await _prepare_dob_collect_speech(
                session, page
            )
            return _app_ok(
                call_id,
                session,
                text,
                next_step,
                "dob reviewed",
                spoken_override=spoken_override,
            )

        # Final gate: every member reviewed — click Next exactly once.
        if walk_done or phase == "done":
            if (
                _dob_explicit_confirm(text)
                or _is_yes(text)
                or _dob_wants_continue(text)
                or re.search(r"\blooks? (good|correct|fine)\b", lowered)
            ):
                blocked = await _dob_click_next_once()
                if blocked is not None:
                    return blocked
                next_step = await _advance_after_answer(page, session)
            else:
                spoken_override = _dob_review_ready_speech()
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + spoken_override,
                    "error": "dob_review_clarify",
                    "step": step,
                    "phase": "application",
                }
        else:
            # Fresh entry / unclear — align with live cards.
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
        component = _dob_component(session)

        def _dob_ask(error: str, speech: str) -> dict[str, Any]:
            _set_pending_question(session, step, speech)
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": error,
                "step": step,
                "phase": "application",
            }

        # Explicit correction only: "change Agent T" with a household match.
        # Never retarget from a bare ASR name (Donnie.) or invent unknown labels.
        if (
            component in ("month", "day", "year", "confirm")
            and re.search(r"\b(change|correct|fix|edit)\b", lowered)
            and _extract_spoken_member_labels(text)
        ):
            looks_like_component = False
            if component == "month":
                looks_like_component = _parse_month_component(text)[0] is not None
            elif component == "day":
                looks_like_component = _parse_day_component(text)[0] is not None
            elif component == "year":
                looks_like_component = _parse_year_component(text)[0] is not None
            elif component == "confirm":
                looks_like_component = bool(_is_yes(text) or _is_no(text))
            if not looks_like_component:
                cards = await _scrape_dob_cards(page)
                available = [
                    str(c.get("name") or "").strip()
                    for c in cards
                    if str(c.get("name") or "").strip()
                ]
                hits, clarify = _match_named_members(text, available)
                if clarify:
                    return _dob_ask("dob_member_ambiguous", clarify)
                if hits:
                    next_step, spoken_override = _begin_dob_collect_for(
                        session, hits
                    )
                    await _open_dob_editor_for_prompt(
                        session, page, hits[0], cards
                    )
                    return _app_ok(
                        call_id,
                        session,
                        text,
                        next_step,
                        "dob retarget",
                        spoken_override=spoken_override,
                    )
                return _dob_ask(
                    "dob_member_unknown",
                    "I couldn't match that name to anyone on the application. "
                    + _dob_component_question(current or "", component),
                )

        if not current:
            cards = await _scrape_dob_cards(page)
            available = [
                str(c.get("name") or "").strip()
                for c in cards
                if str(c.get("name") or "").strip()
            ]
            if len(available) == 1:
                current = available[0]
                session.filled["app_dob_current"] = current
            else:
                hits, clarify = _match_named_members(text, available)
                if clarify:
                    return _dob_ask("dob_member_ambiguous", clarify)
                if hits:
                    next_step, spoken_override = _begin_dob_collect_for(
                        session, hits
                    )
                    return _app_ok(
                        call_id,
                        session,
                        text,
                        next_step,
                        "dob member selected",
                        spoken_override=spoken_override,
                    )
                return _dob_ask(
                    "dob_member_needed",
                    "Whose date of birth is that? Please say the name.",
                )

        # Resolve the card for the active member (exact label preferred).
        cards_before = await _scrape_dob_cards(page)
        exact = [
            c
            for c in cards_before
            if (c.get("name") or "").strip().lower() == current.lower()
        ]
        same_name = exact or [
            c
            for c in cards_before
            if current.lower().split()[:1]
            == (c.get("name") or "").strip().lower().split()[:1]
        ]
        if len(exact) > 1 or (not exact and len(same_name) > 1):
            return _dob_ask(
                "dob_duplicate_name",
                "I see more than one person named "
                f"{current}. Please say the full name as shown on the card.",
            )
        if same_name:
            current = str(same_name[0].get("name") or current)
            session.filled["app_dob_current"] = current
        card = same_name[0] if len(same_name) == 1 else None

        async def _ensure_editor_open() -> tuple[bool, str]:
            """Keep an already-open editor; open Add/Edit only when inputs are gone.

            Returns ``(ok, reason)``. Never re-clicks pencil between month/day/year
            turns when MM/DD/YYYY are already visible on this member's card.
            """
            if card and card.get("editor_open"):
                return True, "scrape_open"
            if await _member_dob_editor_is_open(page, current):
                return True, "live_open"
            correcting = bool(session.filled.get("app_dob_correcting"))
            want_edit = bool(
                (card and not card.get("needs_add") and str(card.get("dob") or "").strip())
                or correcting
            )
            opened = await _click_member_add_dob(page, current, edit=want_edit)
            if opened or await _member_dob_editor_is_open(page, current):
                return True, "opened" if opened else "live_open_after_click"
            logger.info(
                "dob_editor_ensure ok=0 member=%r want_edit=%s reason=action_or_inputs_missing",
                current,
                int(want_edit),
            )
            return False, "action_or_inputs_missing"

        # --- Confirmation of the assembled date (Save only after Yes) ---
        if component == "confirm":
            partial = _dob_partial_state(session)
            dob = _partial_dob_string(partial)
            if not dob:
                _set_dob_component(session, "month")
                return _dob_ask(
                    "dob_invalid",
                    "That date isn't valid. "
                    + _dob_month_question(current),
                )
            confirm_speech = _dob_confirm_question(
                current,
                int(partial["month"]),
                int(partial["day"]),
                int(partial["year"]),
            )
            # Soft affirmations like "sure" are not enough — require an explicit
            # Yes / that's right / correct before Save.
            explicit_confirm = bool(
                (
                    re.match(r"^(yes|yeah|yep|yup|yea)\b", text.strip(), re.I)
                    and _is_yes(text)
                )
                or re.search(
                    r"\b(that(?:'s| is)? right|that(?:'s| is)? correct|"
                    r"looks good)\b",
                    lowered,
                )
                or re.fullmatch(r"correct[.!]?", lowered.strip())
            )
            if explicit_confirm:
                editor_ok, _editor_reason = await _ensure_editor_open()
                if not editor_ok:
                    return _dob_ask(
                        "dob_editor_closed",
                        "I can't open the date editor for that person right now. "
                        + confirm_speech,
                    )
                saved = await _fill_and_save_dob(page, dob, current)
                if not saved:
                    return _dob_ask(
                        "dob_fill_failed",
                        "I couldn't save that date on the form. "
                        + confirm_speech,
                    )
                await page.wait_for_timeout(600)
                # Save already validated the editor value. Card scrape can
                # false-negative after a real save — do not speak a verify
                # failure or re-ask; continue the DOB walk.
                verified = await _verify_member_dob(page, current, dob)
                if not verified:
                    logger.warning(
                        "dob_card_scrape_mismatch_after_save member=%r dob=%r "
                        "(continuing; fill+save already succeeded)",
                        current,
                        dob,
                    )
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
                        return _dob_ask(
                            "dob_wrong_member",
                            "That update changed another person's card. "
                            + confirm_speech,
                        )
                _mark_dob_reviewed(session, current)
                session.filled.pop("app_dob_correcting", None)
                _clear_dob_partial(session)
                _clear_dob_component(session)
                session.filled.pop("app_dob_current", None)
                _set_dob_queue(session, None)
                _set_dob_phase(session, "")
                next_step, spoken_override = await _prepare_dob_collect_speech(
                    session, page
                )
                if next_step == "app_dob_collect":
                    spoken_override = (
                        f"Okay, that date of birth is saved for {current}. "
                        + spoken_override
                    )
                elif next_step == "app_dob_review":
                    # Return to review of remaining members (or final gate).
                    if "date of birth is" in (spoken_override or "").lower():
                        spoken_override = (
                            f"Okay, that date of birth is saved for {current}. "
                            + spoken_override
                        )
                    elif session.filled.get("app_dob_walk_complete"):
                        spoken_override = (
                            f"Okay, that date of birth is saved for {current}. "
                            + _dob_review_ready_speech()
                        )
                _set_pending_question(session, next_step, spoken_override)
            elif _is_no(text) or re.search(
                r"\b(wrong|incorrect|change|fix|nope)\b", lowered
            ):
                # Restart month → day → year for this same member.
                _clear_dob_partial(session)
                _set_dob_component(session, "month")
                return _dob_ask(
                    "dob_change_restart",
                    f"Okay, I'll change that. {_dob_month_question(current)}",
                )
            else:
                # Do not treat vague replies as confirmation.
                return _dob_ask("dob_confirm_needed", confirm_speech)

        # --- Caller said the assembled date is wrong: which part? ---
        elif component == "fix_which":
            which = _parse_fix_which_component(text)
            if not which:
                # Prefer restarting the full date over re-asking "whose".
                _clear_dob_partial(session)
                _set_dob_component(session, "month")
                return _dob_ask(
                    "dob_change_restart",
                    f"Okay, I'll change that. {_dob_month_question(current)}",
                )
            partial = _dob_partial_state(session)
            _store_dob_partial(
                session,
                current,
                None if which == "month" else partial.get("month"),
                None if which == "day" else partial.get("day"),
                None if which == "year" else partial.get("year"),
            )
            _set_dob_component(session, which)
            return _dob_ask(
                "dob_incomplete",
                _dob_component_question(current, which),
            )

        # --- Month / day / year component collection ---
        else:
            editor_ok, editor_reason = await _ensure_editor_open()
            if not editor_ok:
                return _dob_ask(
                    "dob_editor_closed",
                    "I can't open the date editor for that person right now. "
                    + _dob_component_question(current, component),
                )
            logger.info(
                "dob_editor_ensure ok=1 member=%r component=%s reason=%s",
                current,
                component,
                editor_reason,
            )
            partial = _dob_partial_state(session)
            if str(partial.get("name") or "").strip().lower() != current.lower():
                partial = {}
            month = partial.get("month")
            day = partial.get("day")
            year = partial.get("year")

            if component == "month":
                value, clarify = _parse_month_component(text)
                if value is None:
                    # Pinned member: invalid / name-like ASR must not retarget.
                    # Re-ask the month while keeping Don D. (etc.) and the editor.
                    month_ask = _dob_month_question(current)
                    if clarify and "month" in clarify.lower():
                        speech = f"{clarify} {month_ask}"
                    else:
                        speech = month_ask
                    return _dob_ask("dob_incomplete", speech)
                month = value
                _store_dob_partial(session, current, month, day, year)
                filled_ok, fill_reason = await _fill_one_dob_component_detailed(
                    page, current, "month", month
                )
                if not filled_ok:
                    return _dob_ask(
                        fill_reason
                        if fill_reason
                        in (
                            "member_card_mark_failed",
                            "component_input_not_found",
                            "multiple_component_inputs",
                            "input_not_visible",
                            "value_mismatch",
                            "fill_exception",
                        )
                        else "dob_fill_failed",
                        "I had trouble with the date form. Please wait a moment. "
                        + _dob_month_question(current),
                    )
                _set_dob_component(session, "day")
                spoken_override = _dob_day_question(current)
                _set_pending_question(session, step, spoken_override)
                next_step = step

            elif component == "day":
                value, clarify = _parse_day_component(text)
                if value is None:
                    return _dob_ask(
                        "dob_incomplete",
                        clarify or _dob_day_question(current),
                    )
                day = value
                # Reject impossible day for a known month (e.g. February 31).
                if month is not None:
                    from .live_form import _calendar_date_ok

                    probe_year = int(year) if year is not None else 2020
                    if not _calendar_date_ok(int(month), int(day), probe_year):
                        return _dob_ask(
                            "dob_invalid",
                            "That day isn't valid for that month. "
                            + _dob_day_question(current),
                        )
                _store_dob_partial(session, current, month, day, year)
                filled_ok, fill_reason = await _fill_one_dob_component_detailed(
                    page, current, "day", day
                )
                if not filled_ok:
                    # Browser/locator failure — do not imply the spoken day was wrong.
                    return _dob_ask(
                        fill_reason
                        if fill_reason
                        in (
                            "member_card_mark_failed",
                            "component_input_not_found",
                            "multiple_component_inputs",
                            "input_not_visible",
                            "value_mismatch",
                            "fill_exception",
                        )
                        else "dob_fill_failed",
                        "I had trouble with the date form on the screen. "
                        "Please wait a moment while I try again. "
                        + _dob_day_question(current),
                    )
                # After a fix-path day update with month+year already known, confirm.
                if month is not None and year is not None:
                    dob_check = _partial_dob_string(
                        {"month": month, "day": day, "year": year}
                    )
                    if not dob_check:
                        return _dob_ask(
                            "dob_invalid",
                            "That date isn't valid. " + _dob_day_question(current),
                        )
                    _set_dob_component(session, "confirm")
                    spoken_override = _dob_confirm_question(
                        current, int(month), int(day), int(year)
                    )
                else:
                    _set_dob_component(session, "year")
                    spoken_override = _dob_year_question(current)
                _set_pending_question(session, step, spoken_override)
                next_step = step

            elif component == "year":
                value, clarify = _parse_year_component(text)
                if value is None:
                    return _dob_ask(
                        "dob_incomplete",
                        clarify or _dob_year_question(current),
                    )
                year = value
                _store_dob_partial(session, current, month, day, year)
                if month is None or day is None:
                    # Should not happen in normal sequence; restart at month.
                    _set_dob_component(session, "month")
                    return _dob_ask(
                        "dob_incomplete",
                        _dob_month_question(current),
                    )
                dob_check = _partial_dob_string(
                    {"month": month, "day": day, "year": year}
                )
                if not dob_check:
                    return _dob_ask(
                        "dob_invalid",
                        "That date isn't valid. " + _dob_year_question(current),
                    )
                filled_ok, fill_reason = await _fill_one_dob_component_detailed(
                    page, current, "year", year
                )
                if not filled_ok:
                    return _dob_ask(
                        fill_reason
                        if fill_reason
                        in (
                            "member_card_mark_failed",
                            "component_input_not_found",
                            "multiple_component_inputs",
                            "input_not_visible",
                            "value_mismatch",
                            "fill_exception",
                        )
                        else "dob_fill_failed",
                        "I had trouble with the date form. Please wait a moment. "
                        + _dob_year_question(current),
                    )
                _set_dob_component(session, "confirm")
                spoken_override = _dob_confirm_question(
                    current, int(month), int(day), int(year)
                )
                _set_pending_question(session, step, spoken_override)
                next_step = step
            else:
                _set_dob_component(session, "month")
                return _dob_ask("dob_incomplete", _dob_month_question(current))

    elif step == "app_address_review":
        return await _handle_address_step(
            call_id, session, page, text, lowered, step
        )

    elif step == "app_marital_review":
        return await _handle_marital_step(
            call_id, session, page, text, lowered, step
        )

    elif step == "app_childcare_review":
        return await _handle_childcare_step(
            call_id, session, page, text, lowered, step
        )

    elif step == "app_circumstances":
        labels = _map_circumstance_labels(text, allow_bare_no=True)
        if not labels:
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": (
                    "ASK: I didn't catch which situation. "
                    + app_question_for_step(step)
                ),
                "error": "circumstances_clarify",
                "step": step,
                "phase": "application",
            }
        none_only = len(labels) == 1 and "none of the above" in labels[0].lower()
        selected_ok: list[str] = []
        for label in labels:
            selected = await _select_circumstance_checkbox(page, label)
            if not selected.get("ok"):
                # Fall back to generic clickers when DOM helper misses.
                try:
                    await _click_exact_choice(page, label)
                    selected = await _select_circumstance_checkbox(page, label)
                except Exception:
                    try:
                        await _click_cta(page, label, attempts=1)
                        selected = await _select_circumstance_checkbox(page, label)
                    except Exception:
                        selected = {"ok": False}
            if not selected.get("ok"):
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        f"ASK: I couldn't select {label} on the screen. "
                        "Please say that situation again, or say none of the above."
                    ),
                    "error": "circumstances_select_failed",
                    "step": step,
                    "phase": "application",
                }
            selected_ok.append(label)
        if none_only:
            session.filled.pop("app_circumstance_pending", None)
            session.filled.pop("app_circumstance_queue", None)
            session.filled.pop("app_circumstance_pairs", None)
            await _click_next_or_continue(page)
            next_step = await _advance_after_answer(
                page,
                session,
                allow_extra_next=False,
                leave_step=step,
                expected_next="app_tax",
            )
            if not next_step or next_step == step:
                try:
                    from .tax_stage import probe_tax_information_screen

                    probe = await probe_tax_information_screen(page)
                except Exception:
                    probe = {}
                live = await detect_application_screen(page)
                logger.warning(
                    "circumstances_tax_transition_failed call_id=%s detected=%r "
                    "probe_reason=%r heading=%r edits=%s snippet=%r",
                    call_id,
                    live,
                    probe.get("reason"),
                    probe.get("heading"),
                    probe.get("edit_count"),
                    (probe.get("snippet") or "")[:160],
                )
                speech = (
                    "I selected none of the above, but I couldn't confirm the "
                    "Tax Information screen yet. Please stay on Tax Information "
                    "and say try again."
                )
                _set_pending_question(session, step, speech)
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + speech,
                    "error": "circumstances_tax_transition_failed",
                    "step": step,
                    "phase": "application",
                    "detected": live or "",
                    "expected": "app_tax",
                }
        else:
            real = [
                label
                for label in selected_ok
                if "none of the above" not in label.lower()
            ]
            if not real:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + app_question_for_step(step),
                    "error": "circumstances_clarify",
                    "step": step,
                    "phase": "application",
                }
            session.filled["app_circumstance_queue"] = json.dumps(real)
            current = real[0]
            session.filled["app_circumstance_pending"] = current
            # Ensure the current card is expanded, then wait for apply-to members
            # the same way special_person waits after a special condition click.
            await _select_circumstance_checkbox(page, current)
            members = await _wait_for_circumstance_members(page, current)
            if not members:
                members = await _discover_circumstance_members(page, current)
            session.filled["app_circumstance_member_labels"] = "|".join(
                str(row.get("label") or "") for row in members[:12]
            )
            next_step = "app_circumstances_person"
            # Even if scrape lags, the UI usually already shows apply-to — ask
            # for names like special_person instead of claiming the list is closed.
            spoken_override = _circumstance_person_question(current, members)
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

        async def _advance_circumstance_queue() -> tuple[str, str | None]:
            remaining = [c for c in queue if c.lower() != pending.lower()]
            if remaining:
                nxt = remaining[0]
                session.filled["app_circumstance_queue"] = json.dumps(remaining)
                session.filled["app_circumstance_pending"] = nxt
                selected = await _select_circumstance_checkbox(page, nxt)
                if not selected.get("ok"):
                    try:
                        await _click_exact_choice(page, nxt)
                        selected = await _select_circumstance_checkbox(page, nxt)
                    except Exception:
                        pass
                members = await _wait_for_circumstance_members(page, nxt)
                session.filled["app_circumstance_member_labels"] = "|".join(
                    str(row.get("label") or "") for row in members[:12]
                )
                speech = _circumstance_person_question(nxt, members)
                _set_pending_question(session, "app_circumstances_person", speech)
                return "app_circumstances_person", speech
            session.filled.pop("app_circumstance_pending", None)
            session.filled.pop("app_circumstance_queue", None)
            speech = _circumstance_more_speech(session)
            _set_pending_question(session, "app_circumstances_more", speech)
            return "app_circumstances_more", speech

        if re.search(r"\b(continue|next|done|skip|that's all|that is all|finished)\b", lowered):
            next_step, spoken_override = await _advance_circumstance_queue()
        else:
            # Reject bare circumstance phrases — need household member names.
            if _map_circumstance_labels(text) and not re.search(
                r"\b(applies?|for)\b", lowered
            ):
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: Please say the household member's name for "
                        f"{pending or 'that situation'}, not the condition again."
                    ),
                    "error": "circumstances_person_not_member",
                    "step": step,
                    "phase": "application",
                }
            # Re-open the pending condition so member checkboxes are visible.
            if pending:
                await _select_circumstance_checkbox(page, pending)
            members = await _wait_for_circumstance_members(page, pending)
            if not members:
                labels = [
                    item
                    for item in (
                        session.filled.get("app_circumstance_member_labels") or ""
                    ).split("|")
                    if item.strip()
                ]
                members = [
                    {"label": name, "name": name, "index": i, "dupIndex": 0}
                    for i, name in enumerate(labels)
                ]
            # Split on "and"/commas so "Agent T and john D" can both be selected.
            chunks = [
                part.strip()
                for part in re.split(r"\band\b|,|&", text, flags=re.I)
                if part.strip()
            ] or [text]
            assigned: list[str] = []
            clarify = None
            for chunk in chunks:
                if _is_yes(chunk) or _is_no(chunk):
                    continue
                if re.search(
                    r"\b(done|continue|next|skip|finished)\b", chunk, re.I
                ):
                    continue
                target, clarify = _circumstance_target_member(chunk, members)
                if clarify:
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": "ASK: " + clarify,
                        "error": "circumstances_member_ambiguous",
                        "step": step,
                        "phase": "application",
                    }
                if target is None:
                    continue
                name = str(target.get("name") or "").strip()
                dup = int(target.get("dupIndex") or 0)
                ok = await _set_circumstance_member(
                    page, name, pending, dup_index=dup
                )
                if not ok:
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": (
                            "ASK: I couldn't select that person under "
                            f"{pending or 'the current situation'}. "
                            "Please say the name as shown on the card."
                        ),
                        "error": "circumstances_person_not_set",
                        "step": step,
                        "phase": "application",
                    }
                assigned.append(name)
                for row in members:
                    if int(row.get("index") or -1) == int(target.get("index") or -2):
                        row["checked"] = True
                        break
            if not assigned:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + _circumstance_person_question(pending, members),
                    "error": "circumstances_person_clarify",
                    "step": step,
                    "phase": "application",
                }
            _record_circumstance_pair(session, pending, assigned)
            people = " and ".join(assigned)
            spoken_override = (
                f"Okay, {people} for {pending}. "
                "Anyone else for this situation? Say another name, or say done."
            )
            next_step = "app_circumstances_person"
            _set_pending_question(session, next_step, spoken_override)

    elif step == "app_circumstances_more":
        if _is_yes(text):
            next_step = "app_circumstances"
            spoken_override = app_question_for_step("app_circumstances")
            _set_pending_question(session, next_step, spoken_override)
        elif _is_no(text) or re.search(r"\b(done|continue|next|finished)\b", lowered):
            session.filled.pop("app_circumstance_pending", None)
            session.filled.pop("app_circumstance_queue", None)
            await _click_next_or_continue(page)
            next_step = await _advance_after_answer(
                page,
                session,
                allow_extra_next=False,
                leave_step="app_circumstances",
                expected_next="app_tax",
            )
            session.filled.pop("app_circumstance_pairs", None)
            session.filled.pop("app_circumstance_member_labels", None)
            if not next_step or next_step == "app_circumstances":
                try:
                    from .tax_stage import probe_tax_information_screen

                    probe = await probe_tax_information_screen(page)
                except Exception:
                    probe = {}
                live = await detect_application_screen(page)
                logger.warning(
                    "circumstances_more_tax_transition_failed call_id=%s detected=%r "
                    "probe_reason=%r heading=%r edits=%s snippet=%r",
                    call_id,
                    live,
                    probe.get("reason"),
                    probe.get("heading"),
                    probe.get("edit_count"),
                    (probe.get("snippet") or "")[:160],
                )
                speech = (
                    "I couldn't confirm the Tax Information screen yet. "
                    "Please stay on Tax Information and say try again."
                )
                _set_pending_question(session, step, speech)
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + speech,
                    "error": "circumstances_tax_transition_failed",
                    "step": step,
                    "phase": "application",
                    "detected": live or "",
                    "expected": "app_tax",
                }
        else:
            # Treat another condition phrase as adding more.
            labels = _map_circumstance_labels(text)
            if labels and not (
                len(labels) == 1 and "none of the above" in labels[0].lower()
            ):
                next_step = "app_circumstances"
                # Re-enter circumstances with this utterance on the next turn.
                spoken_override = app_question_for_step("app_circumstances")
                _set_pending_question(session, next_step, spoken_override)
            else:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + _circumstance_more_speech(session),
                    "error": "circumstances_more_clarify",
                    "step": step,
                    "phase": "application",
                }

    elif step == "app_tax":
        from .tax_stage import _handle_tax_step

        return await _handle_tax_step(
            call_id, session, page, text, lowered, step
        )

    elif step in (
        "app_income",
        "app_income_amount",
        "app_income_unemployed",
        "app_income_confirm",
        "app_income_more",
    ):
        from .income_stage import handle_income_step

        return await handle_income_step(
            call_id, session, page, text, lowered, step
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

    elif step in (
        "app_expenses",
        "app_expenses_amount",
        "app_expenses_medical",
        "app_expenses_confirm",
        "app_expenses_more",
    ):
        from .expense_stage import handle_expense_step

        return await handle_expense_step(
            call_id, session, page, text, lowered, step
        )

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
    if (
        next_step == "app_marital_review"
        and spoken_override is None
        and step != "app_marital_review"
    ):
        next_step, spoken_override = await _prepare_marital_review_speech(
            session, page
        )
    if (
        next_step == "app_childcare_review"
        and spoken_override is None
        and step != "app_childcare_review"
    ):
        next_step, spoken_override = await _prepare_childcare_review_speech(
            session, page
        )
    if (
        next_step == "app_tax"
        and spoken_override is None
        and step != "app_tax"
    ):
        from .tax_stage import _prepare_tax_review_speech

        next_step, spoken_override = await _prepare_tax_review_speech(session, page)
    if (
        next_step == "app_income"
        and spoken_override is None
        and step != "app_income"
    ):
        from .income_stage import prepare_income_speech

        next_step, spoken_override = await prepare_income_speech(session, page)
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
