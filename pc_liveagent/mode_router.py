"""Same-call help-mode router after identity confirm.

Modes (single Retell Custom LLM call — no second dial):
  video      → text MEDICAL_RENEWAL_VIDEO_URL how-to link
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

from .guide_prompt import (
    GUIDE_ASK_TEXT_LINK,
    GUIDE_HAVE_LINK,
    GUIDE_LINK_CHECK,
    GUIDE_OPENING,
    GUIDE_RESEND_LINK,
    GUIDE_SENT_LINK,
)

HelpMode = Literal["video", "live_agent", "guide", "ai_fill"]

logger = logging.getLogger(__name__)

RENEWAL_STATUS_ASK = (
    "Thanks for confirming. I'm Kyle from AI Medicaid Assist. "
    "Have you already completed your Medi-Cal renewal?"
)

RENEWAL_STATUS_CLARIFY = (
    "Just to confirm — have you already completed your Medi-Cal renewal? "
    "Please say yes or no."
)

RENEWAL_ALREADY_DONE = (
    "Okay, great — thank you for taking care of that. "
    "We appreciate your time. Have a wonderful day. Goodbye."
)

MODE_INTRO_AND_MENU = (
    "No problem — I can help you with that. There are four options. "
    "Option A — I'll text you a video link showing how to do it. "
    "Option B — connect with a live agent. "
    "Option C — guide me through, while you fill it on your screen. "
    "Option D — AI fills the form for you when you answer questions. "
    "Which option are you comfortable with?"
)

MODE_CLARIFY = (
    "No problem — I want to get this right. There are four options. "
    "Option A — I'll text you a video link showing how to do it. "
    "Option B — connect with a live agent. "
    "Option C — guide me through, while you fill it on your screen. "
    "Option D — AI fills the form for you when you answer questions. "
    "Which option are you comfortable with?"
)

VIDEO_SENT = (
    "I've just texted you the video link. "
    "You can watch it whenever you're ready. "
    "Thank you for your time. Goodbye."
)

VIDEO_SEND_FAILED = (
    "I wasn't able to text that video link just now. "
    "I can connect you with a live agent, guide you through on your screen, "
    "or fill the form for you. Which of those are you most comfortable with?"
)

LIVE_AGENT_CONNECTING = (
    "Okay — I'm connecting you with a live agent now. Please hold."
)

LIVE_AGENT_NO_NUMBER = (
    "I wasn't able to reach a live agent number right now. "
    "I can walk you through PointCare instead, or fill the form for you. "
    "Which would you like?"
)

GUIDE_START = GUIDE_OPENING

AI_FILL_ACK = (
    "Okay — I'll fill PointCare for you while we talk. "
)

# Language ask only — do not repeat the Kyle intro (already spoken at call start).
LANGUAGE_ONLY_ASK = (
    "Is it okay to continue in English, or would you prefer another language?"
)

# Fallback script if Claude coaching is unavailable (Stage A onward).
GUIDE_STEPS: tuple[str, ...] = (
    "Tap the yellow button at the bottom that says Get Started.",
    "Tap Continue.",
    "This screen is about what frustrates you most about coverage. "
    "Tap whichever fits, then tap Continue.",
    "Tap Create My Profile.",
    "On Sign in or Create an Account, tap Continue with Google, Continue with Apple, "
    "or type any email and tap Continue. Tell me when the next page is open.",
    "Continue privately on your screen. Don't read passwords or codes out loud. "
    "Say done when you're signed in.",
    "Fill in your first name.",
    "Now fill in your last name.",
    "Now fill in your date of birth: month, then day, then the four-digit year.",
    "Start typing your street address, then pick it from the list that pops up.",
    "Type your phone number, all 10 digits. Say done when that box is filled.",
    "Check the email, then tap Next.",
    "Review the consent page, tap Add Signature, sign, and tap Save. Say done when signed.",
    "On coverage lookup, type SSN or Medicaid ID yourself or tap Skip. "
    "Tell me when the next page is open.",
    "Tap the yellow button that says Check Household Eligibility.",
    "Continue one screen at a time. Say done after each page until you reach "
    "You're All Set, then say yes if you want to submit.",
)

GUIDE_DONE = (
    "You're all set with PointCare registration guidance. "
    "Thank you for your time. Goodbye."
)

GUIDE_SMS_FAILED = (
    "I wasn't able to text the PointCare link just now. "
    "You can open the PointCare page on your phone if you have it, "
    "or I can connect you with a live agent. What would you like to do?"
)

# Soft signal weights — utterance can match several; highest score wins.
_VIDEO_SIGNALS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\b(video(\s*link)?|how[\s-]*to\s*video|send\s*(me\s*)?(the\s*)?(video|link)|text\s*(me\s*)?(the\s*)?(video|link))\b", re.I), 9),
    (re.compile(r"\b(sms|text\s*me|send\s*it|watch\s*(the\s*)?video|video\s*showing)\b", re.I), 7),
    (re.compile(r"\b(option\s*(a|1|one)|first\s*(one|option)|number\s*(one|1)|#\s*1)\b", re.I), 9),
    (re.compile(r"\b(go\s*with\s*option\s*(a|1|one)|option\s*a\s*works)\b", re.I), 9),
)

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
    (re.compile(r"\b(go\s*with\s*option\s*(b|2|two)|option\s*b\s*works)\b", re.I), 9),
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
    (re.compile(r"\b(third\s*(one|option)|option\s*(c|3|three)|number\s*(three|3)|#\s*3)\b", re.I), 9),
    (re.compile(r"\b(go\s*with\s*option\s*(c|3|three)|option\s*c\s*works)\b", re.I), 9),
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
    (re.compile(r"\b(fourth\s*(one|option)|option\s*(d|4|four)|number\s*(four|4)|#\s*4|last\s*(one|option))\b", re.I), 9),
    (re.compile(r"\b(go\s*with\s*option\s*(d|4|four)|option\s*d\s*works)\b", re.I), 9),
)

# Menu order spoken to the caller: A video, B live, C guide, D ai_fill
_ORDINAL_VIDEO = re.compile(
    r"\b("
    r"option\s*(a|1|one)(\s*works(\s*for\s*me)?)?|"
    r"(go\s*with|pick|choose|take|select|i('ll|\s+will)\s*(go\s*with|take|do))\s+option\s*(a|1|one)|"
    r"(letter|choice)\s*(a|1|one)|"
    r"first(\s*one)?|first\s*option|number\s*(one|1)|#\s*1|the\s*first"
    r")\b",
    re.I,
)
_ORDINAL_LIVE = re.compile(
    r"\b("
    r"option\s*(b|2|two)(\s*works(\s*for\s*me)?)?|"
    r"(go\s*with|pick|choose|take|select|i('ll|\s+will)\s*(go\s*with|take|do))\s+option\s*(b|2|two)|"
    r"(letter|choice)\s*(b|2|two)|"
    r"second(\s*one)?|second\s*option|number\s*(two|2)|#\s*2|the\s*second"
    r")\b",
    re.I,
)
_ORDINAL_GUIDE = re.compile(
    r"\b("
    r"option\s*(c|3|three)(\s*works(\s*for\s*me)?)?|"
    r"(go\s*with|pick|choose|take|select|i('ll|\s+will)\s*(go\s*with|take|do))\s+option\s*(c|3|three)|"
    r"(letter|choice)\s*(c|3|three)|"
    r"third(\s*one)?|third\s*option|number\s*(three|3)|#\s*3|the\s*third"
    r")\b",
    re.I,
)
_ORDINAL_AI = re.compile(
    r"\b("
    r"option\s*(d|4|four)(\s*works(\s*for\s*me)?)?|"
    r"(go\s*with|pick|choose|take|select|i('ll|\s+will)\s*(go\s*with|take|do))\s+option\s*(d|4|four)|"
    r"(letter|choice)\s*(d|4|four)|"
    r"fourth(\s*one)?|fourth\s*option|number\s*(four|4)|#\s*4|"
    r"the\s*fourth|last(\s*one)?|last\s*option"
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
    """Map natural / messy speech to video | live_agent | guide | ai_fill.

    Does not require menu-exact wording. Returns None when still ambiguous.
    """
    text = _normalize_choice_text(utterance)
    if not text or text in {"huh", "what", "sorry", "pardon", "hm", "hmm", "uh"}:
        return None

    hits = {
        "video": bool(_ORDINAL_VIDEO.search(text)),
        "live_agent": bool(_ORDINAL_LIVE.search(text)),
        "guide": bool(_ORDINAL_GUIDE.search(text)),
        "ai_fill": bool(_ORDINAL_AI.search(text)),
    }
    ordinal_hits = [name for name, matched in hits.items() if matched]
    if len(ordinal_hits) == 1:
        return ordinal_hits[0]  # type: ignore[return-value]

    video = _score_signals(text, _VIDEO_SIGNALS)
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
        if not re.search(r"\b(myself|my\s*self|person|human|agent|video)\b", text):
            ai = max(ai, 6)

    scores = {"video": video, "live_agent": live, "guide": guide, "ai_fill": ai}
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
        "You classify a caller's spoken choice after this help menu: "
        "video = text a how-to video link; "
        "live_agent = connect with a live agent; "
        "guide = guide them through while they fill on their screen; "
        "ai_fill = AI fills the form while they answer questions. "
        "Reply with ONLY JSON: "
        '{"mode":"video"|"live_agent"|"guide"|"ai_fill"|null,"confidence":0-1}. '
        "Accept option letters (A/B/C/D), numbers, option names, and full sentences. "
        "'Option A' or 'send the video'→video; "
        "'Option B' or 'a live agent'→live_agent; "
        "'Option C' or 'guide me through'→guide; "
        "'Option D' or 'AI fills the form'→ai_fill; "
        "'uh a person'→live_agent; 'just tell me what to click'→guide; "
        "'you handle the form'→ai_fill. If truly unclear, mode=null."
    )
    user = json.dumps(
        {
            "menu": [
                "video — text a how-to video link",
                "live_agent — connect with a live agent",
                "guide — guide me through on my screen",
                "ai_fill — AI fills the form when I answer",
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
        if mode in {"video", "live_agent", "guide", "ai_fill"} and conf >= min_conf:
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
    if classify_help_mode(utterance) in {"video", "live_agent", "ai_fill"}:
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
