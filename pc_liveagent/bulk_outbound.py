"""Parse patient lists from CSV/Excel and place concurrent outbound calls."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
from typing import Any

from . import config
from .call_turn import register_outbound_caller
from .form_bridge import on_call_started
from .retell_call import normalize_phone, place_form_filler_call

logger = logging.getLogger(__name__)

_HEADER_ALIASES = {
    "first_name": {
        "first_name",
        "firstname",
        "first",
        "given_name",
        "givenname",
        "fname",
    },
    "last_name": {
        "last_name",
        "lastname",
        "last",
        "family_name",
        "familyname",
        "surname",
        "lname",
    },
    "name": {
        "name",
        "full_name",
        "fullname",
        "patient_name",
        "patient",
        "member_name",
        "member",
    },
    "phone": {
        "phone",
        "phone_number",
        "phonenumber",
        "mobile",
        "mobile_number",
        "cell",
        "cellphone",
        "contact",
        "contact_number",
        "tel",
        "telephone",
    },
    "form_name": {
        "form_name",
        "formname",
        "reason",
        "reason_for_call",
        "call_reason",
        "notes",
    },
}


def _norm_header(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[\s\-]+", "_", text)
    text = re.sub(r"[^\w]", "", text)
    return text


def _map_headers(headers: list[str]) -> dict[str, int]:
    mapped: dict[str, int] = {}
    for idx, raw in enumerate(headers):
        key = _norm_header(raw)
        if not key:
            continue
        for field, aliases in _HEADER_ALIASES.items():
            if key in aliases and field not in mapped:
                mapped[field] = idx
                break
    return mapped


def _cell(row: list[Any], idx: int | None) -> str:
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    value = row[idx]
    if value is None:
        return ""
    return str(value).strip()


def _split_name(full: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", (full or "").strip()) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _patient_from_row(
    row: list[Any],
    cols: dict[str, int],
    *,
    default_form_name: str = "",
    row_number: int,
) -> dict[str, Any] | None:
    first = _cell(row, cols.get("first_name"))
    last = _cell(row, cols.get("last_name"))
    full = _cell(row, cols.get("name"))
    phone_raw = _cell(row, cols.get("phone"))
    form_name = _cell(row, cols.get("form_name")) or default_form_name

    if not first and not last and full:
        first, last = _split_name(full)
    name = " ".join(p for p in (first, last) if p).strip() or full.strip()
    phone = normalize_phone(phone_raw)
    digits = re.sub(r"\D", "", phone)
    if not phone or len(digits) < 10:
        return {
            "ok": False,
            "row": row_number,
            "name": name,
            "phone_number": phone_raw,
            "form_name": form_name,
            "error": "Missing or invalid phone number (need at least 10 digits).",
        }
    if not name:
        return {
            "ok": False,
            "row": row_number,
            "name": "",
            "phone_number": phone,
            "form_name": form_name,
            "error": "Missing patient name.",
        }
    return {
        "ok": True,
        "row": row_number,
        "first_name": first,
        "last_name": last,
        "name": name,
        "phone_number": phone,
        "form_name": form_name or "AI Medicaid Assist",
        "error": "",
    }


def parse_csv_bytes(data: bytes, *, default_form_name: str = "") -> dict[str, Any]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = [list(r) for r in reader]
    return _parse_table(rows, default_form_name=default_form_name, source="csv")


def parse_xlsx_bytes(data: bytes, *, default_form_name: str = "") -> dict[str, Any]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("openpyxl is required to parse Excel files.") from exc
    wb = load_workbook(filename=io.BytesIO(data), read_only=True, data_only=True)
    try:
        ws = wb.active
        rows: list[list[Any]] = []
        for row in ws.iter_rows(values_only=True):
            rows.append(["" if c is None else c for c in row])
    finally:
        wb.close()
    return _parse_table(rows, default_form_name=default_form_name, source="xlsx")


def parse_upload(
    filename: str,
    data: bytes,
    *,
    default_form_name: str = "",
) -> dict[str, Any]:
    name = (filename or "").lower().strip()
    if name.endswith(".csv") or name.endswith(".txt"):
        return parse_csv_bytes(data, default_form_name=default_form_name)
    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        return parse_xlsx_bytes(data, default_form_name=default_form_name)
    # Sniff: zip/xlsx vs text
    if data[:2] == b"PK":
        return parse_xlsx_bytes(data, default_form_name=default_form_name)
    return parse_csv_bytes(data, default_form_name=default_form_name)


def _parse_table(
    rows: list[list[Any]],
    *,
    default_form_name: str = "",
    source: str,
) -> dict[str, Any]:
    nonempty = [r for r in rows if any(str(c or "").strip() for c in r)]
    if not nonempty:
        return {
            "ok": False,
            "source": source,
            "patients": [],
            "errors": ["File is empty."],
            "error": "File is empty.",
        }

    header_row = nonempty[0]
    cols = _map_headers([str(c or "") for c in header_row])
    has_header = bool(cols.get("phone") is not None or cols.get("name") is not None)
    data_rows = nonempty[1:] if has_header else nonempty

    if not has_header:
        # Fallback: name, phone [, reason] or first, last, phone
        sample = nonempty[0]
        if len(sample) >= 3:
            cols = {"first_name": 0, "last_name": 1, "phone": 2}
            if len(sample) >= 4:
                cols["form_name"] = 3
        elif len(sample) >= 2:
            cols = {"name": 0, "phone": 1}
        else:
            return {
                "ok": False,
                "source": source,
                "patients": [],
                "errors": [
                    "Could not detect columns. Include headers like "
                    "first_name, last_name, phone (or name, phone)."
                ],
                "error": "Could not detect columns.",
            }
        data_rows = nonempty

    if "phone" not in cols:
        return {
            "ok": False,
            "source": source,
            "patients": [],
            "errors": [
                "Missing a phone column. Use headers such as phone, phone_number, or mobile."
            ],
            "error": "Missing phone column.",
        }

    patients: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, row in enumerate(data_rows, start=2 if has_header else 1):
        if not any(str(c or "").strip() for c in row):
            continue
        item = _patient_from_row(
            row, cols, default_form_name=default_form_name, row_number=i
        )
        if item is None:
            continue
        patients.append(item)
        if not item.get("ok"):
            errors.append(f"Row {i}: {item.get('error')}")

    valid = [p for p in patients if p.get("ok")]
    return {
        "ok": bool(valid),
        "source": source,
        "headers_detected": sorted(cols.keys()),
        "patients": patients,
        "valid_count": len(valid),
        "error_count": len(patients) - len(valid),
        "errors": errors[:50],
        "error": "" if valid else (errors[0] if errors else "No valid patients found."),
    }


def patients_from_payload(
    items: list[dict[str, Any]],
    *,
    default_form_name: str = "",
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, raw in enumerate(items or [], start=1):
        if not isinstance(raw, dict):
            out.append(
                {
                    "ok": False,
                    "row": i,
                    "name": "",
                    "phone_number": "",
                    "form_name": default_form_name,
                    "error": "Invalid patient entry.",
                }
            )
            continue
        first = str(raw.get("first_name") or "").strip()
        last = str(raw.get("last_name") or "").strip()
        full = str(raw.get("name") or "").strip()
        if not first and not last and full:
            first, last = _split_name(full)
        name = " ".join(p for p in (first, last) if p).strip() or full
        phone = normalize_phone(str(raw.get("phone_number") or raw.get("phone") or ""))
        form_name = (
            str(raw.get("form_name") or "").strip()
            or default_form_name
            or "AI Medicaid Assist"
        )
        digits = re.sub(r"\D", "", phone)
        if not phone or len(digits) < 10:
            out.append(
                {
                    "ok": False,
                    "row": i,
                    "name": name,
                    "phone_number": str(raw.get("phone_number") or raw.get("phone") or ""),
                    "form_name": form_name,
                    "error": "Missing or invalid phone number.",
                }
            )
            continue
        if not name:
            out.append(
                {
                    "ok": False,
                    "row": i,
                    "name": "",
                    "phone_number": phone,
                    "form_name": form_name,
                    "error": "Missing patient name.",
                }
            )
            continue
        out.append(
            {
                "ok": True,
                "row": i,
                "first_name": first,
                "last_name": last,
                "name": name,
                "phone_number": phone,
                "form_name": form_name,
                "error": "",
            }
        )
    return out


async def place_bulk_calls(
    *,
    patients: list[dict[str, Any]],
    max_concurrent: int = 20,
    agent_number: str = "",
    form_url: str = "",
    agent_id: str = "",
    chrome_url_builder,
) -> dict[str, Any]:
    valid = [p for p in patients if p.get("ok")]
    skipped = [p for p in patients if not p.get("ok")]
    limit = max(1, min(int(max_concurrent or 20), 20))
    form_url = (form_url or "").strip() or config.POINTCARE_URL
    sem = asyncio.Semaphore(limit)
    results: list[dict[str, Any]] = []

    async def _one(patient: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            placed = await asyncio.to_thread(
                place_form_filler_call,
                phone_number=str(patient["phone_number"]),
                name=str(patient.get("name") or ""),
                form_name=str(patient.get("form_name") or "AI Medicaid Assist"),
                agent_id=agent_id or "",
            )
            entry = {
                "row": patient.get("row"),
                "name": patient.get("name") or "",
                "phone_number": patient.get("phone_number") or "",
                "form_name": patient.get("form_name") or "",
                "ok": bool(placed.get("ok")),
                "call_id": str(placed.get("call_id") or ""),
                "status": placed.get("status") or "",
                "from_number": placed.get("from_number") or "",
                "error": placed.get("error") or "",
                "chrome_url": "",
            }
            call_id = entry["call_id"]
            if entry["ok"] and call_id:
                register_outbound_caller(
                    call_id,
                    str(patient.get("name") or ""),
                    agent_number=agent_number or "",
                    form_url=form_url,
                    phone_number=str(patient.get("phone_number") or ""),
                )
                chrome_link = chrome_url_builder(call_id)
                entry["chrome_url"] = chrome_link
                try:
                    started = await on_call_started(call_id, form_url, open_browser=False)
                    if isinstance(started, dict) and started.get("error"):
                        entry["browser_error"] = str(started.get("error") or "")[:200]
                except Exception as exc:
                    logger.exception("bulk on_call_started failed for %s", call_id)
                    entry["browser_error"] = str(exc)[:200]
            return entry

    if valid:
        results = await asyncio.gather(*[_one(p) for p in valid])

    placed_ok = [r for r in results if r.get("ok")]
    placed_fail = [r for r in results if not r.get("ok")]
    return {
        "ok": bool(placed_ok),
        "max_concurrent": limit,
        "requested": len(patients),
        "attempted": len(valid),
        "placed": len(placed_ok),
        "failed": len(placed_fail),
        "skipped": len(skipped),
        "results": list(results),
        "skipped_rows": skipped,
    }
