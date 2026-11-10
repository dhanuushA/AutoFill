"""Text MEDICAL_RENEWAL_VIDEO_URL when the caller chooses Option A."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from . import config
from .retell_call import normalize_phone

logger = logging.getLogger(__name__)

_TWILIO_API = "https://api.twilio.com/2010-04-01"
_SMS_LIMIT = 1500


def renewal_video_url() -> str:
    return (getattr(config, "MEDICAL_RENEWAL_VIDEO_URL", "") or "").strip()


def _sms_from_number() -> str:
    return (
        (getattr(config, "MEDICAL_RENEWAL_SMS_COLLECT_NUMBER", "") or "").strip()
        or (getattr(config, "MEDICAL_RENEWAL_TOLL_FREE_NUMBER", "") or "").strip()
        or (getattr(config, "TWILIO_CARECALL_FROM_NUMBER", "") or "").strip()
        or (getattr(config, "TWILIO_PHONE_NUMBER", "") or "").strip()
    )


def build_renewal_video_sms(*, name: str = "", video_url: str = "") -> str:
    who = (name or "").strip() or "there"
    url = (video_url or renewal_video_url()).strip()
    return (
        f"Hi {who}, here is your how-to video from AI Medicaid Assist: {url}\n\n"
        "Open the link to watch how to complete your renewal."
    )


def send_renewal_video_sms(*, phone_number: str, name: str = "") -> dict[str, Any]:
    formatted = normalize_phone(phone_number) or str(phone_number or "").strip()
    if not formatted:
        return {
            "ok": False,
            "error": "Missing phone number for video SMS.",
            "status_code": 400,
        }

    url = renewal_video_url()
    if not url:
        return {
            "ok": False,
            "error": "MEDICAL_RENEWAL_VIDEO_URL is not configured in .env.",
            "status_code": 500,
        }

    sid = (getattr(config, "TWILIO_ACCOUNT_SID", "") or "").strip()
    token = (getattr(config, "TWILIO_AUTH_TOKEN", "") or "").strip()
    from_number = _sms_from_number()
    if not sid or not token or not from_number:
        return {
            "ok": False,
            "error": "Twilio SMS is not configured for the video link.",
            "status_code": 503,
            "video_url": url,
        }

    body = build_renewal_video_sms(name=name, video_url=url)
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
                    "formfiller_video_sms_failed phone=%s err=%s", formatted, err
                )
                return {
                    "ok": False,
                    "error": err,
                    "status_code": 502,
                    "video_url": url,
                }
            message_sid = str(payload.get("sid") or message_sid)
    except Exception as exc:
        logger.exception("formfiller_video_sms_exception phone=%s", formatted)
        return {
            "ok": False,
            "error": str(exc)[:400],
            "status_code": 502,
            "video_url": url,
        }

    logger.info(
        "formfiller_video_sms_sent phone=%s from=%s url=%s sid=%s",
        formatted,
        from_number,
        url,
        message_sid,
    )
    return {
        "ok": True,
        "sid": message_sid,
        "from_number": from_number,
        "phone_number": formatted,
        "body": body,
        "video_url": url,
        "message": "Renewal video SMS sent.",
    }
