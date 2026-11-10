"""Text POINTCARE_URL when Option C callers need the PointCare link."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from . import config
from .retell_call import normalize_phone

logger = logging.getLogger(__name__)

_TWILIO_API = "https://api.twilio.com/2010-04-01"
_SMS_LIMIT = 1500


def pointcare_link_url() -> str:
    return (getattr(config, "POINTCARE_URL", "") or "").strip()


def _sms_from_number() -> str:
    return (
        (getattr(config, "MEDICAL_RENEWAL_SMS_COLLECT_NUMBER", "") or "").strip()
        or (getattr(config, "MEDICAL_RENEWAL_TOLL_FREE_NUMBER", "") or "").strip()
        or (getattr(config, "TWILIO_CARECALL_FROM_NUMBER", "") or "").strip()
        or (getattr(config, "TWILIO_PHONE_NUMBER", "") or "").strip()
    )


def build_pointcare_link_sms(*, name: str = "", link: str = "") -> str:
    who = (name or "").strip() or "there"
    url = (link or pointcare_link_url()).strip()
    return (
        f"Hi {who}, here is your PointCare link from AI Medicaid Assist: {url}\n\n"
        "Open it to continue registration."
    )


def send_pointcare_link_sms(
    *,
    phone_number: str,
    name: str = "",
    resend: bool = False,
) -> dict[str, Any]:
    formatted = normalize_phone(phone_number) or str(phone_number or "").strip()
    if not formatted:
        return {
            "ok": False,
            "error": "Missing phone number for PointCare link SMS.",
            "status_code": 400,
        }

    url = pointcare_link_url()
    if not url:
        return {
            "ok": False,
            "error": "POINTCARE_URL is not configured in .env.",
            "status_code": 500,
        }

    sid = (getattr(config, "TWILIO_ACCOUNT_SID", "") or "").strip()
    token = (getattr(config, "TWILIO_AUTH_TOKEN", "") or "").strip()
    from_number = _sms_from_number()
    if not sid or not token or not from_number:
        return {
            "ok": False,
            "error": "Twilio SMS is not configured for the PointCare link.",
            "status_code": 503,
            "link": url,
        }

    body = build_pointcare_link_sms(name=name, link=url)
    if resend:
        body = "Resending your PointCare link.\n\n" + body

    message_sid = ""
    try:
        for i in range(0, len(body), _SMS_LIMIT):
            chunk = body[i : i + _SMS_LIMIT]
            resp = httpx.post(
                f"{_TWILIO_API}/Accounts/{sid}/Messages.json",
                auth=(sid, token),
                data={"To": formatted, "From": from_number, "Body": chunk},
                timeout=25,
            )
            payload = {}
            try:
                payload = resp.json()
            except Exception:
                payload = {}
            if resp.status_code >= 400:
                err = str(
                    payload.get("message")
                    or payload.get("error_message")
                    or resp.text
                    or "Twilio rejected the SMS."
                )[:400]
                logger.warning(
                    "pointcare_link_sms_failed phone=%s err=%s", formatted, err
                )
                return {
                    "ok": False,
                    "error": err,
                    "status_code": 502,
                    "link": url,
                }
            message_sid = str(payload.get("sid") or message_sid)
    except Exception as exc:
        logger.exception("pointcare_link_sms_exception phone=%s", formatted)
        return {
            "ok": False,
            "error": str(exc)[:400],
            "status_code": 502,
            "link": url,
        }

    logger.info(
        "pointcare_link_sms_sent phone=%s from=%s url=%s sid=%s resend=%s",
        formatted,
        from_number,
        url,
        message_sid,
        resend,
    )
    return {
        "ok": True,
        "sid": message_sid,
        "from_number": from_number,
        "phone_number": formatted,
        "body": body,
        "link": url,
        "resend": bool(resend),
        "message": "PointCare link SMS sent.",
    }
