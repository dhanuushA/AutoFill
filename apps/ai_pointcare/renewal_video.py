"""Medi-Cal renewal video SMS used by the voice-call tool.

When the caller chooses Option A, texts ``MEDICAL_RENEWAL_VIDEO_URL`` from ``.env``.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from django.conf import settings

from apps.ai_caller.retell import normalize_phone
from apps.ai_sms.services import send_twilio_sms
from apps.ai_pointcare.flow_definition import ORG_NAME

logger = logging.getLogger(__name__)


def renewal_video_url() -> str:
    """Exact URL from MEDICAL_RENEWAL_VIDEO_URL in .env / Django settings."""
    return (getattr(settings, "MEDICAL_RENEWAL_VIDEO_URL", "") or "").strip()


def renewal_video_sms_from_number() -> str:
    return (
        getattr(settings, "MEDICAL_RENEWAL_SMS_COLLECT_NUMBER", "")
        or getattr(settings, "MEDICAL_RENEWAL_TOLL_FREE_NUMBER", "")
        or getattr(settings, "TWILIO_CARECALL_FROM_NUMBER", "")
        or getattr(settings, "TWILIO_PHONE_NUMBER", "")
        or ""
    ).strip()


def build_renewal_video_sms(*, name: str = "", video_url: str = "") -> str:
    who = (name or "").strip() or "there"
    url = (video_url or renewal_video_url()).strip()
    return (
        f"Hi {who}, here is your Medi-Cal renewal how-to video from {ORG_NAME}: "
        f"{url}\n\n"
        "Open the link to watch how to complete your renewal."
    )


def send_renewal_video_sms(*, phone_number: str, name: str = "") -> Dict[str, Any]:
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

    from_number = renewal_video_sms_from_number()
    body = build_renewal_video_sms(name=name, video_url=url)
    message_sid, err = send_twilio_sms(formatted, body, from_number)
    if err:
        logger.warning(
            "renewal_video_sms_failed phone=%s url=%s err=%s",
            formatted,
            url,
            err,
        )
        return {
            "ok": False,
            "error": err,
            "status_code": 502,
            "video_url": url,
        }
    logger.info(
        "renewal_video_sms_sent phone=%s from=%s url=%s sid=%s",
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
