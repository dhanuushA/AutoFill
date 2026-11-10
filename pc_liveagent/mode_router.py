"""Same-call help-mode router after identity confirm.

Modes (single Retell Custom LLM call — no second dial):
  live_agent → cold transfer via response.transfer_number
  guide      → verbal PointCare coaching (ai_pointcare-style)
  ai_fill    → existing Collect Mode (language → sign-in → fill)

Classification is intent-based: callers rarely say the menu verbatim.
Heuristics score paraphrases first; Claude (if configured) resolves leftovers.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Literal

HelpMode = Literal["live_agent", "guide", "ai_fill"]

logger = logging.getLogger(__name__)

MODE_INTRO_AND_MENU = (
    "Thanks for confirming. I'm Kyle from Bay Area Community Health, "
    "calling to help with your PointCare registration and forms. "
    "There are three options. "
    "Option A — connect with a live agent. "
    "Option B — guide me through, while you fill it on your screen. "
    "Option C — AI fills the form for you when you answer questions. "
    "Which option do you want — A, B, or C?"
)

MODE_CLARIFY = (
    "No problem — I want to get this right. "
    "There are three options. "
    "Option A is connect with a live agent. "
    "Option B is guide me through. "
    "Option C is AI fills the form for you when you answer. "
    "Which option do you want — A, B, or C?"
)

LIVE_AGENT_CONNECTING = (
    "Okay — I'm connecting you with a live agent now. Please hold."
)

LIVE_AGENT_NO_NUMBER = (
    "I wasn't able to reach a live agent number right now. "
    "I can walk you through PointCare instead, or fill the form for you. "
    "Which would you like?"
)

GUIDE_START = (
    "Okay — I'll guide you through PointCare on your screen. "
    "Please open the PointCare page if you don't have it open yet, "
    "and say ready when you're there."
)

AI_FILL_ACK = (
    "Okay — I'll fill PointCare for you while we talk. "
)

GUIDE_STEPS: tuple[str, ...] = (
    GUIDE_START,
    "Great. On the sign-in screen, choose email, Google, or Apple — "
    "whichever you prefer — and tell me which one you picked.",
    "Okay. Continue with that sign-in on your screen. "
    "If email asks for a confirmation or code, enter it yourself — "
    "please don't read passwords or codes out loud. Say done when you're signed in.",
    "Next, create your password privately on the screen. "
    "Say done when the password step is finished.",
    "Now enter your phone number if asked, and complete any SMS code on your screen. "
    "Say done when that step is finished.",
    "Let's fill your profile one piece at a time — name, date of birth, address, "
    "phone, and email. Update what's needed, then say done.",
    "You'll see a consent form. PointCare may monitor coverage, respond on your behalf, "
    "submit renewals, and contact you about care. Review it, sign on your screen, "
    "and say done when you've signed.",
    "Optional fields like SSN or Medicaid ID can be skipped if you prefer. "
    "Finish or skip those, then say done.",
    "Take a quick look that your profile looks right. "
    "If everything looks good, say all set. "
    "If you want a live agent instead, just say so.",
)

GUIDE_DONE = (
    "You're all set with PointCare registration guidance. "
    "Thank you for your time. Goodbye."
)

# Soft signal weights — utterance can match several; highest score wins.
_LIVE_SIGNALS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\b(live\s*agent|live\s*person|real\s*person|real\s*human)\b", re.I), 8),
    (re.compile(r"\b(human\s*agent|human\s*being|actual\s*person|actual\s*human)\b", re.I), 8),
    (re.compile(r"\b(enrollment\s*(agent|team|specialist)|customer\s*service|call\s*center)\b", re.I), 7),
    (re.compile(r"\b(front\s*desk|representative|\brep\b|operator)\b", re.I), 6),
    (re.compile(r"\b(transfer(\s*me)?|put\s*me\s*through|patch\s*me|hand\s*me\s*off)\b", re.I), 7),
    (re.compile(r"\b(connect(\s*me)?|get\s*me\s*(someone|a\s*person|an?\s*agent))\b", re.I), 6),
    (re.compile(r"\b(talk|speak|speak\s*to|talk\s*to|speak\s*with|talk\s*with)\b.{0,24}\b(person|human|someone|agent|somebody)\b", re.I), 7),
    (re.compile(r"\b(someone\s*real|a\s*person|a\s*human|somebody|an?\s*agent)\b", re.I), 5),
    (re.compile(r"\b(the\s*)?(human|person|agent)(\s*one)?\b", re.I), 5),
    (re.compile(r"\b(get\s*(me\s*)?(a\s*)?(human|person|agent)|need\s*(a\s*)?(human|person|agent))\b", re.I), 7),
    (re.compile(r"\b(someone|somebody)\s*(please|now)?\b", re.I), 5),
    (re.compile(r"\b(not\s*(the\s*)?ai|don'?t\s*want\s*(the\s*)?ai|no\s*ai|hate\s*bots|not\s*a\s*robot)\b", re.I), 6),
    (re.compile(r"\b(i\s*want\s*(a\s*)?(person|human)|give\s*me\s*(a\s*)?(person|human))\b", re.I), 7),
    (re.compile(r"\b(go\s*with\s*option\s*(a|1|one)|option\s*a\s*works)\b", re.I), 9),
)

_GUIDE_SIGNALS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\b(guide(\s*me)?|walk\s*me\s*through|walkthrough|walk\s*through)\b", re.I), 8),
    (re.compile(r"\b(step\s*by\s*step|one\s*step\s*at\s*a\s*time)\b", re.I), 7),
    (re.compile(r"\b(show\s*me\s*how|tell\s*me\s*what\s*to\s*(do|click|type|press)|coach(\s*me)?)\b", re.I), 7),
    (re.compile(r"\b(help\s*me\s*(do|fill|complete|with)|i\s*need\s*help\s*(doing|filling|with))\b", re.I), 6),
    (re.compile(r"\b(i('ll|\s+will)?\s*(do|fill|complete)\s*it\s*(myself|my\s*self|on\s*my\s*own))\b", re.I), 8),
    (re.compile(r"\b(i\s*want\s*to\s*(do|fill)\s*it\s*(myself|my\s*self|on\s*my\s*own))\b", re.I), 8),
    (re.compile(r"\b(on\s*my\s*(own\s*)?screen|i('ll|\s+will)\s*type|i\s*can\s*type)\b", re.I), 6),
    (re.compile(r"\b(registration\s*help|pointcare\s*registration|help\s*me\s*register)\b", re.I), 6),
    (re.compile(r"\b(you\s*tell\s*me|just\s*tell\s*me|instruct\s*me|directions)\b", re.I), 5),
    (re.compile(r"\b(second\s*(one|option)|option\s*(b|2|two)|number\s*(two|2)|#\s*2)\b", re.I), 9),
    (re.compile(r"\b(go\s*with\s*option\s*(b|2|two)|option\s*b\s*works)\b", re.I), 9),
)

_AI_SIGNALS: tuple[tuple[re.Pattern[str], int], ...] = (
    # Do not match bare "ai" — "I don't want the AI" is live_agent.
    (re.compile(r"\b(ai\s*(fill|fills|filler)|artificial\s*intelligence)\b", re.I), 8),
    (re.compile(r"\b(use\s*(the\s*)?ai|with\s*(the\s*)?ai|let\s*(the\s*)?ai|have\s*(the\s*)?ai)\b", re.I), 7),
    (re.compile(r"\b(the\s*bot|the\s*robot|form\s*filler|live\s*form\s*filler|auto(\s*|-)?fill)\b", re.I), 8),
    (re.compile(r"\b(fill\s*it\s*(up\s*)?(for\s*me|instead)|fill\s*(it\s*)?up(\s*for\s*me)?|fill\s*(the\s*)?form\s*(for\s*me|up)?)\b", re.I), 8),
    (re.compile(r"\b(you\s*fill(\s*it)?|you\s*do\s*it(\s*for\s*me)?|you\s*handle\s*(it|the\s*form|everything))\b", re.I), 7),
    (re.compile(r"\b(fill\s*it\s*on\s*your\s*own|do\s*it\s*(for\s*me|yourself))\b", re.I), 8),
    (re.compile(r"\b(i\s*(don'?t|do\s*not)\s*want\s*to\s*(type|fill|click)|don'?t\s*want\s*to\s*do\s*it)\b", re.I), 6),
    (re.compile(r"\b(just\s*ask\s*me(\s*questions)?|i('ll|\s+will)\s*answer(\s*questions)?)\b", re.I), 6),
    (re.compile(r"\b(you\s*fill\s*(while|and)\s*i\s*answer|answer\s*questions)\b", re.I), 6),
    (re.compile(r"\b(fill\s*it\s*while\s*i\s*(talk|answer|speak)|while\s*i\s*(talk|answer))\b", re.I), 7),
    (re.compile(r"\b(third\s*(one|option)|option\s*(c|3|three)|number\s*(three|3)|#\s*3|last\s*(one|option))\b", re.I), 9),
    (re.compile(r"\b(go\s*with\s*option\s*(c|3|three)|option\s*c\s*works)\b", re.I), 9),
)

# Menu order spoken to the caller: A live, B guide, C ai_fill
# Require "option/letter/choice" before A/B/C so "I want a person" is not Option A.
_ORDINAL_LIVE = re.compile(
    r"\b("
    r"option\s*(a|1|one)(\s*works(\s*for\s*me)?)?|"
    r"(go\s*with|pick|choose|take|select|i('ll|\s+will)\s*(go\s*with|take|do))\s+option\s*(a|1|one)|"
    r"(letter|choice)\s*(a|1|one)|"
    r"first(\s*one)?|first\s*option|number\s*(one|1)|#\s*1|the\s*first"
    r")\b",
    re.I,
)
_ORDINAL_GUIDE = re.compile(
    r"\b("
    r"option\s*(b|2|two)(\s*works(\s*for\s*me)?)?|"
    r"(go\s*with|pick|choose|take|select|i('ll|\s+will)\s*(go\s*with|take|do))\s+option\s*(b|2|two)|"
    r"(letter|choice)\s*(b|2|two)|"
    r"second(\s*one)?|second\s*option|number\s*(two|2)|#\s*2|the\s*second"
    r")\b",
    re.I,
)
_ORDINAL_AI = re.compile(
    r"\b("
    r"option\s*(c|3|three)(\s*works(\s*for\s*me)?)?|"
    r"(go\s*with|pick|choose|take|select|i('ll|\s+will)\s*(go\s*with|take|do))\s+option\s*(c|3|three)|"
    r"(letter|choice)\s*(c|3|three)|"
    r"third(\s*one)?|third\s*option|number\s*(three|3)|#\s*3|"
    r"the\s*third|last(\s*one)?|last\s*option"
    r")\b",
    re.I,
)


def _score_signals(text: str, signals: tuple[tuple[re.Pattern[str], int], ...]) -> int:
    score = 0
    for pattern, weight in signals:
        if pattern.search(text):
            score += weight
    return score


def _normalize_choice_text(utterance: str) -> str:
    text = (utterance or "").strip().lower()
    text = text.replace("wanna", "want to").replace("gonna", "going to")
    text = text.replace("gotta", "got to").replace("dunno", "do not know")
    text = re.sub(r"[^\w\s'#]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def classify_help_mode(utterance: str) -> HelpMode | None:
    """Map natural / messy speech to live_agent | guide | ai_fill.

    Does not require menu-exact wording. Returns None when still ambiguous.
    """
    text = _normalize_choice_text(utterance)
    if not text or text in {"huh", "what", "sorry", "pardon", "hm", "hmm", "uh"}:
        return None

    # Bare ordinals / option letters relative to the spoken menu.
    if _ORDINAL_AI.search(text) and not _ORDINAL_LIVE.search(text) and not _ORDINAL_GUIDE.search(text):
        return "ai_fill"
    if _ORDINAL_GUIDE.search(text) and not _ORDINAL_LIVE.search(text) and not _ORDINAL_AI.search(text):
        return "guide"
    if _ORDINAL_LIVE.search(text) and not _ORDINAL_GUIDE.search(text) and not _ORDINAL_AI.search(text):
        return "live_agent"

    live = _score_signals(text, _LIVE_SIGNALS)
    guide = _score_signals(text, _GUIDE_SIGNALS)
    ai = _score_signals(text, _AI_SIGNALS)

    # Self-serve "I'll do it" without "for me" → guide, not AI.
    if re.search(r"\b(myself|my\s*self|on\s*my\s*own)\b", text):
        if not re.search(r"\b(for\s*me|you\s*fill)\b", text):
            guide = max(guide, 8)
            ai = max(0, ai - 4)

    # Rejecting AI / bot / computer → live agent (menu context: person vs AI).
    if re.search(
        r"\b(don'?t|do\s*not|no|not|hate|nah|nope)\b.{0,28}\b(ai|bot|robot|computer|machine|automated)\b",
        text,
    ) or re.search(r"\b(not\s*(the\s*)?(ai|bot|robot)|no\s*ai|hate\s*bots)\b", text):
        live = max(live, 8)
        ai = 0

    # "Just do it" / "go ahead and fill" without myself → AI
    if re.search(r"\b(just\s*(do|fill)|go\s*ahead\s*and\s*fill|take\s*care\s*of\s*it)\b", text):
        if not re.search(r"\b(myself|my\s*self|person|human|agent)\b", text):
            ai = max(ai, 6)

    scores = {"live_agent": live, "guide": guide, "ai_fill": ai}
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best_name, best = ranked[0]
    second = ranked[1][1]

    # Need a clear winner.
    if best <= 0:
        return None
    if best >= 5 and best >= second + 2:
        return best_name  # type: ignore[return-value]
    if best >= 7 and best > second:
        return best_name  # type: ignore[return-value]
    # Single signal that is strong enough alone.
    if best >= 6 and second == 0:
        return best_name  # type: ignore[return-value]
    return None


async def classify_help_mode_async(utterance: str) -> HelpMode | None:
    """Heuristic first; Claude fallback for messy / incomplete phrasing."""
    quick = classify_help_mode(utterance)
    if quick:
        return quick

    text = (utterance or "").strip()
    if not text:
        return None

    from . import config

    if not (getattr(config, "ANTHROPIC_API_KEY", "") or "").strip():
        return None

    started = time.perf_counter()
    system = (
        "You classify a caller's spoken choice after this menu: "
        "Option A = live_agent (connect with a live agent), "
        "Option B = guide (guide me through while they fill on their screen), "
        "Option C = ai_fill (AI fills the form while they answer questions). "
        "Reply with ONLY JSON: "
        '{"mode":"live_agent"|"guide"|"ai_fill"|null,"confidence":0-1}. '
        "Accept option letters, numbers, and paraphrases. "
        "'Option A works for me'→live_agent; 'I will go with option B'→guide; "
        "'option C'→ai_fill; 'uh a person'→live_agent; "
        "'just tell me what to click'→guide; 'you handle the form'→ai_fill. "
        "If truly unclear, mode=null."
    )
    user = json.dumps(
        {
            "menu": [
                "Option A / live_agent — connect with a live agent",
                "Option B / guide — guide me through",
                "Option C / ai_fill — AI fills the form when you answer",
            ],
            "utterance": text[:300],
        },
        ensure_ascii=False,
    )
    timeout_s = max(0.4, float(getattr(config, "ANTHROPIC_TIMEOUT_S", 1.5) or 1.5))
    min_conf = float(getattr(config, "ANTHROPIC_MIN_CONFIDENCE", 0.72) or 0.72)
    model = (config.ANTHROPIC_MODEL or "claude-haiku-5-5").strip()

    try:
        from anthropic import AsyncAnthropic
        import asyncio

        client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

        async def _call() -> Any:
            kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": 80,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            try:
                kwargs["output_config"] = {
                    "effort": (getattr(config, "ANTHROPIC_EFFORT", "low") or "low")
                }
                kwargs["thinking"] = {"type": "disabled"}
                return await client.messages.create(**kwargs)
            except TypeError:
                kwargs.pop("output_config", None)
                kwargs.pop("thinking", None)
                return await client.messages.create(**kwargs)
            except Exception as first_exc:
                msg = str(first_exc).lower()
                if "effort" in msg or "thinking" in msg or "output_config" in msg:
                    kwargs.pop("output_config", None)
                    kwargs.pop("thinking", None)
                    return await client.messages.create(**kwargs)
                raise

        resp = await asyncio.wait_for(_call(), timeout=timeout_s)
        raw = ""
        for block in getattr(resp, "content", None) or []:
            raw += getattr(block, "text", "") or ""
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I).strip()
        data = json.loads(raw)
        mode = (data.get("mode") or "").strip()
        conf = float(data.get("confidence") or 0)
        latency = (time.perf_counter() - started) * 1000
        if mode in {"live_agent", "guide", "ai_fill"} and conf >= min_conf:
            logger.info(
                "mode_classify_claude mode=%s conf=%.2f latency_ms=%.0f utter=%r",
                mode,
                conf,
                latency,
                text[:80],
            )
            return mode  # type: ignore[return-value]
        logger.info(
            "mode_classify_claude rejected mode=%r conf=%.2f latency_ms=%.0f",
            mode,
            conf,
            latency,
        )
    except Exception:
        logger.info(
            "mode_classify_claude failed utter=%r",
            text[:80],
            exc_info=True,
        )
    return None


def guide_speech_for_step(step_index: int) -> str:
    if step_index < 0:
        step_index = 0
    if step_index >= len(GUIDE_STEPS):
        return GUIDE_DONE
    return GUIDE_STEPS[step_index]


def guide_wants_advance(utterance: str) -> bool:
    text = _normalize_choice_text(utterance)
    if not text:
        return False
    # Switching modes is not an advance.
    if classify_help_mode(utterance) in {"live_agent", "ai_fill"}:
        return False
    if re.search(
        r"\b("
        r"done|ready|finished|complete|completed|all\s*set|okay|ok|yes|"
        r"yep|yeah|yup|next|continue|go\s*ahead|signed\s*in|signed|"
        r"i\s*did|i\s*have|that('?s|\s+is)\s*it|got\s*it|mm\s*hmm|uh\s*huh"
        r")\b",
        text,
    ):
        return True
    return False


def guide_wants_live_agent(utterance: str) -> bool:
    return classify_help_mode(utterance) == "live_agent"


def guide_wants_ai_fill(utterance: str) -> bool:
    return classify_help_mode(utterance) == "ai_fill"
