"""Pointcare Work & Income (Step 9) per-member Collect Mode workflow.

Walks each household card, opens Add Income (or edit/delete an existing entry),
selects a type, collects details, speaks a document-proof notice, confirms the
values, then Saves. Amount-only types ask only amount per month. None is
click-only. Same type for a person overrides. After save, asks whether to add
another income (and can take the next type in the same turn).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from playwright.async_api import Page, expect

logger = logging.getLogger(__name__)

# Exact Pointcare Select Income Type labels (screenshot order).
_INCOME_TYPE_LABELS = (
    "Employed",
    "Self-employed",
    "Unemployed",
    "SSA",
    "SSDI",
    "Pension",
    "Alimony",
    "Retirement income",
    "Property rental income",
    "Worker's compensation",
    "Other",
    "None",
)

# Full follow-up question sets come later for these three.
_INCOME_DETAILED_TYPES = frozenset({"Employed", "Self-employed", "Unemployed"})

# Everything else (except None) is amount-per-month only.
_INCOME_AMOUNT_ONLY_TYPES = frozenset(
    {
        "SSA",
        "SSDI",
        "Pension",
        "Alimony",
        "Retirement income",
        "Property rental income",
        "Worker's compensation",
        "Other",
    }
)

_INCOME_FILLED_KEYS = (
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
    "app_income_manage_action",
    "app_income_types_listed",
    "app_income_edit_field",
    "app_income_doc_notice_members",
)

# Spoken once on the first type ask; again only when the caller asks for options.
_INCOME_TYPE_OPTIONS_SPEECH = (
    "You can say Employed, Self-employed, Unemployed, SSA, SSDI, Pension, "
    "Alimony, Retirement income, Property rental income, Worker's compensation, "
    "Other, or None."
)

_INCOME_PHASES = (
    "type",
    "amount",
    "more",
    "employed",
    "self",
    "unemployed",
    "confirm",
    "confirm_edit_pick",
    "confirm_edit_value",
    "delete_confirm",
    "final",
)

# Spoken labels for confirm-edit ("which detail should I change?").
_EMPLOYED_EDIT_FIELD_LABELS = {
    "pay": "monthly gross income",
    "hours": "hours per week",
    "frequency": "pay frequency",
    "employer": "employer name",
}
_SELF_EDIT_FIELD_LABELS = {
    "pay": "estimated monthly net income",
    "work": "type of work",
}


def _income_dom_js() -> str:
    """DOM helpers for Work & Income member cards and amount modal."""
    return r"""
      const incNorm = (s) => (s || '').replace(/\s+/g, ' ').trim();
      const incLow = (s) => incNorm(s).toLowerCase();
      const incVisible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const st = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' &&
          st.display !== 'none' && st.opacity !== '0';
      };
      const incIsPerson = (line) => {
        const t = incNorm(line).replace(/\s*\(you\)\s*/ig, ' ').trim();
        if (!t || t.length > 60) return false;
        // Pointcare uses both "Add Income" and "Add Another Income".
        if (/add(?:\s+another|\s+an)?\s+income|select income|amount per month|progress|work & income|work and income|next|back|continue|cancel|save|edit|remove/i.test(t)) {
          return false;
        }
        if (/^(yes|no|employed|self-employed|unemployed|ssa|ssdi|pension|alimony|other|none)$/i.test(t)) {
          return false;
        }
        return /^[A-Za-z][A-Za-z .'\-]{0,40}$/.test(t) && t.split(' ').length <= 4;
      };
      const incBtnText = (btn) => incLow(
        btn.innerText || btn.textContent ||
        btn.getAttribute('aria-label') || ''
      );
      const incHasAddIncomeCue = (s) =>
        /\badd(?:\s+another|\s+an)?\s+income\b/.test(incLow(s || ''));
      const incIsAddIncome = (btn) => {
        const t = incBtnText(btn);
        const aria = incLow(btn.getAttribute('aria-label') || '');
        return incHasAddIncomeCue(t) || incHasAddIncomeCue(aria);
      };
      const incIncomeTypeLabels = [
        'Employed', 'Self-employed', 'Unemployed', 'SSA', 'SSDI', 'Pension',
        'Alimony', 'Retirement income', 'Property rental income',
        "Worker's compensation", 'Other', 'None',
      ];
      const incMatchType = (line) => {
        const n = incLow(line);
        // Longest label first; never bare includes() — "another" matched "Other".
        const labels = [...incIncomeTypeLabels].sort(
          (a, b) => incLow(b).length - incLow(a).length
        );
        for (const label of labels) {
          const low = incLow(label);
          if (n === low) return label;
          if (n.startsWith(low + ' ') || n.endsWith(' ' + low)) return label;
          if (n.includes(' ' + low + ' ')) return label;
        }
        return '';
      };
      const incIsEditBtn = (btn) => {
        const t = incBtnText(btn);
        const aria = incLow(btn.getAttribute('aria-label') || '');
        const title = incLow(btn.getAttribute('title') || '');
        const testId = incLow(
          btn.getAttribute('data-testid') || btn.getAttribute('data-test-id') || ''
        );
        if (
          t === 'edit' || t.startsWith('edit ') || t.includes('pencil') ||
          aria.includes('edit') || aria.includes('pencil') ||
          title.includes('edit') || title.includes('pencil') ||
          testId.includes('edit') || testId.includes('pencil')
        ) {
          return true;
        }
        // Pointcare rows often use an icon-only pencil (empty label + svg).
        const bare = !t || t.length <= 1;
        if (!bare) return false;
        if (aria.includes('close') || aria.includes('cancel') ||
            aria.includes('dismiss') || aria.includes('delete') ||
            aria.includes('remove') || aria.includes('add')) {
          return false;
        }
        if (typeof incIsAddIncome === 'function' && incIsAddIncome(btn)) {
          return false;
        }
        return Boolean(btn.querySelector('svg, img, [class*="icon" i]'));
      };
      const incIsDeleteBtn = (btn) => {
        const t = incBtnText(btn);
        const aria = incLow(btn.getAttribute('aria-label') || '');
        return t === 'delete' || t === 'remove' || t.startsWith('delete') ||
          t.startsWith('remove') || aria.includes('delete') ||
          aria.includes('remove') || t.includes('trash');
      };
      const incAmountNearText = (blob) => {
        const t = String(blob || '');
        // Single JS escapes only (this blob is a Python raw string). Literal slash via [/].
        const m = t.match(/\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)/);
        if (m) return m[1].replace(/,/g, '');
        const m2 = t.match(/\b([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*[/]\s*month\b/i);
        if (m2) return m2[1].replace(/,/g, '');
        return '';
      };
      const incEntriesIn = (root) => {
        if (!root) return [];
        const out = [];
        const seen = {};
        const pushEntry = (typ, summary) => {
          if (!typ || typ === 'None') return;
          const key = incLow(typ);
          if (seen[key]) {
            // Enrich prior entry with amount if we learn it later.
            const prior = out.find((e) => incLow(e.type) === key);
            if (prior && !prior.amount) {
              const amt = incAmountNearText(summary);
              if (amt) {
                prior.amount = amt;
                prior.summary = (prior.summary || '') + ' ' + summary;
              }
            }
            return;
          }
          seen[key] = true;
          let editBtn = null;
          let delBtn = null;
          for (const btn of root.querySelectorAll('button, [role=button], a')) {
            if (!incVisible(btn)) continue;
            let cur = btn.parentElement;
            let near = false;
            for (let d = 0; cur && d < 6; d += 1, cur = cur.parentElement) {
              const ct = incLow(cur.innerText || '');
              if (ct.includes(key)) { near = true; break; }
            }
            if (!near) continue;
            if (!editBtn && incIsEditBtn(btn)) editBtn = btn;
            if (!delBtn && incIsDeleteBtn(btn)) delBtn = btn;
          }
          out.push({
            type: typ,
            summary: summary || typ,
            amount: incAmountNearText(summary),
            hasEdit: Boolean(editBtn),
            hasDelete: Boolean(delBtn),
            index: out.length,
          });
        };
        const text = incNorm(root.innerText || '');
        const lines = text.split(/\r?\n/).map(incNorm).filter(Boolean);
        for (let i = 0; i < lines.length; i += 1) {
          const line = lines[i];
          const typ = incMatchType(line);
          if (!typ || typ === 'None') continue;
          // Pair "SSDI" + next line "$500 / month".
          const next = lines[i + 1] || '';
          const summary = next && /month|\$|\d/.test(next)
            ? (line + ' ' + next)
            : line;
          pushEntry(typ, summary);
        }
        // Nested leaves (RN Web) — short nodes that are exact type labels.
        for (const child of root.querySelectorAll(
          'div, span, p, li, label, h1, h2, h3, h4'
        )) {
          if (!incVisible(child)) continue;
          const t = incNorm(child.innerText || child.textContent || '');
          if (!t || t.length > 60) continue;
          const typ = incMatchType(t);
          if (!typ || typ === 'None') continue;
          if (incLow(t) !== incLow(typ) && !incLow(t).startsWith(incLow(typ))) {
            continue;
          }
          let parentBlob = '';
          let cur = child.parentElement;
          for (let d = 0; cur && d < 4; d += 1, cur = cur.parentElement) {
            parentBlob = incNorm(cur.innerText || '');
            if (parentBlob.length > 20 && parentBlob.length < 400) break;
          }
          pushEntry(typ, parentBlob || t);
        }
        return out;
      };
      const incLinesFromEl = (el) => {
        // Prefer newline-split innerText; also collect short child leaves so
        // React Native Web cards (often one flattened line) still yield names.
        const raw = el.innerText || '';
        const lines = raw.split(/\r?\n/).map(incNorm).filter(Boolean);
        const seen = {};
        const out = [];
        const push = (s) => {
          const n = incNorm(s);
          if (!n || n.length > 80) return;
          const key = incLow(n);
          if (seen[key]) return;
          seen[key] = 1;
          out.push(n);
        };
        for (const line of lines) push(line);
        for (const child of el.querySelectorAll(
          'div, span, p, li, h1, h2, h3, h4, label'
        )) {
          if (!incVisible(child)) continue;
          const t = incNorm(child.innerText || child.textContent || '');
          if (!t || t.length > 80) continue;
          if ((child.children || []).length > 3 && t.length > 40) continue;
          push(t);
        }
        return out;
      };
      const incNameFromCardText = (text) => {
        const blob = incNorm(text || '');
        if (!blob) return '';
        // "Admin P. Add Another Income None" (no newlines).
        const m = blob.match(
          /^([A-Za-z][A-Za-z .'\-]{0,40}?)\s+Add(?:\s+Another|\s+An)?\s+Income\b/i
        );
        if (m && incIsPerson(m[1])) return incNorm(m[1]);
        return '';
      };
      const incAreaPenalty = (area) => {
        if (!area || area < 1) return 0;
        return Math.min(8, Math.floor(Math.log10(area)));
      };
      const incCardFromAddBtn = (btn) => {
        // Add-Income-anchored walk. Do NOT always pick the smallest ancestor —
        // the header-only root misses SSDI / $500 / month rows below.
        // Score: single member + entries/amount/Add Another, still one Add btn.
        const candidates = [];
        let cur = btn.parentElement;
        for (let depth = 0; cur && depth < 14; depth += 1, cur = cur.parentElement) {
          if (!incVisible(cur)) continue;
          const r = cur.getBoundingClientRect();
          const width = Math.round(r.width);
          const height = Math.round(r.height);
          if (width < 80 || height < 20 || height > 1100 || width > 1400) continue;
          const addsIn = [...cur.querySelectorAll(
            'button, [role=button], a'
          )].filter(incVisible).filter(incIsAddIncome);
          if (addsIn.length !== 1) continue;
          const lines = incLinesFromEl(cur);
          const people = lines.filter(incIsPerson);
          let name = people.length === 1 ? people[0] : '';
          if (!name) name = incNameFromCardText(cur.innerText || '');
          if (!name) continue;
          // Reject containers that clearly include a second household member.
          if (people.length > 1) continue;
          const entries = incEntriesIn(cur);
          const blob = incLow(cur.innerText || '');
          const hasAddAnother = /add\s+another\s+income/.test(blob);
          const hasMoney = /\$\s*\d|[/]\s*month/.test(blob);
          let score = 0;
          score += Math.min(entries.length, 4) * 40;
          if (hasAddAnother) score += 25;
          if (hasMoney) score += 20;
          // Prefer a moderately sized card over a tiny header strip.
          if (height >= 70) score += 15;
          if (height >= 120) score += 10;
          // Soft penalty for huge page-level wrappers.
          if (height > 700) score -= 20;
          score -= incAreaPenalty(width * height);
          candidates.push({
            name,
            area: width * height,
            height,
            el: cur,
            entries,
            score,
            hasAddAnother,
          });
        }
        if (!candidates.length) return null;
        candidates.sort((a, b) => b.score - a.score || a.area - b.area);
        return candidates[0];
      };
      const incFindCards = () => {
        const buttons = [...document.querySelectorAll(
          'button, [role=button], a'
        )].filter(incVisible).filter(incIsAddIncome);
        const cards = [];
        const seen = {};
        for (const btn of buttons) {
          const card = incCardFromAddBtn(btn);
          if (!card) continue;
          const key = incLow(card.name);
          if (seen[key] !== undefined) continue;
          seen[key] = cards.length;
          cards.push({
            index: cards.length,
            name: card.name,
            hasAddIncome: true,
            entries: card.entries || [],
          });
        }
        return cards;
      };
      const incPressEl = (el) => {
        if (!el) return false;
        try { el.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
        try {
          el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
          el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
          el.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
          el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
          el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        } catch (e) {
          try {
            el.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }));
            el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
            el.dispatchEvent(new MouseEvent('pointerup', { bubbles: true }));
            el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
          } catch (e2) {}
        }
        try { el.click(); } catch (e) {}
        return true;
      };
      const incRowForType = (card, wantType) => {
        // Prefer the tightest ancestor that contains the type label + one edit control.
        const leaves = [...card.querySelectorAll(
          'div, span, p, li, label, h1, h2, h3, h4'
        )].filter(incVisible);
        let best = null;
        let bestScore = -1;
        for (const leaf of leaves) {
          const raw = incNorm(leaf.innerText || leaf.textContent || '');
          if (!raw || raw.length > 80) continue;
          const matched = incMatchType(raw);
          if (!matched || incLow(matched) !== wantType) continue;
          if (incLow(raw) !== wantType && !incLow(raw).startsWith(wantType)) {
            continue;
          }
          let cur = leaf;
          for (let d = 0; cur && d < 8; d += 1, cur = cur.parentElement) {
            if (!incVisible(cur)) continue;
            const r = cur.getBoundingClientRect();
            if (r.height > 220 || r.width < 40) continue;
            const btns = [...cur.querySelectorAll(
              'button, [role=button], a'
            )].filter(incVisible);
            const editBtns = btns.filter(incIsEditBtn);
            const delBtns = btns.filter(incIsDeleteBtn);
            const addBtns = btns.filter(incIsAddIncome);
            if (addBtns.length) continue;
            if (!editBtns.length && !delBtns.length) continue;
            // Prefer compact rows with a single edit control near the type.
            let score = 0;
            score += editBtns.length ? 50 : 0;
            score += delBtns.length ? 20 : 0;
            score += Math.max(0, 40 - Math.floor(r.height / 4));
            if (score > bestScore) {
              bestScore = score;
              best = { el: cur, editBtns, delBtns, height: r.height };
            }
          }
        }
        return best;
      };
      const incClickEntryAction = (memberName, incomeType, action) => {
        const card = incFindCardEl(memberName);
        if (!card) return { ok: false, reason: 'card_not_found' };
        const wantType = incLow(incomeType);
        const wantAction = incLow(action);
        const row = incRowForType(card, wantType);
        if (row) {
          if (wantAction === 'edit' && row.editBtns.length) {
            incPressEl(row.editBtns[0]);
            return {
              ok: true,
              action: 'edit',
              via: 'type_row',
              wantType,
              rowHeight: row.height,
            };
          }
          if ((wantAction === 'delete' || wantAction === 'remove') &&
              row.delBtns.length) {
            incPressEl(row.delBtns[0]);
            return { ok: true, action: 'delete', via: 'type_row', wantType };
          }
          // Delete requested but only pencil exists on the row — open edit.
          if ((wantAction === 'delete' || wantAction === 'remove') &&
              row.editBtns.length) {
            incPressEl(row.editBtns[0]);
            return {
              ok: true,
              action: 'edit',
              via: 'type_row_pencil_for_delete',
              wantType,
              rowHeight: row.height,
            };
          }
        }
        // Legacy near-type walk (broader).
        const buttons = [...card.querySelectorAll(
          'button, [role=button], a'
        )].filter(incVisible);
        for (const btn of buttons) {
          let cur = btn.parentElement;
          let nearType = false;
          for (let d = 0; cur && d < 6; d += 1, cur = cur.parentElement) {
            const t = incLow(cur.innerText || '');
            if (t.includes(wantType)) { nearType = true; break; }
          }
          if (!nearType) continue;
          if (wantAction === 'edit' && incIsEditBtn(btn)) {
            incPressEl(btn);
            return { ok: true, action: 'edit', via: 'near_type', wantType };
          }
          if ((wantAction === 'delete' || wantAction === 'remove') &&
              incIsDeleteBtn(btn)) {
            incPressEl(btn);
            return { ok: true, action: 'delete', via: 'near_type', wantType };
          }
        }
        // Last resort for edit/delete: any icon-only pencil in the card that
        // sits beside a node whose text equals the income type.
        if (wantAction === 'edit' || wantAction === 'delete' ||
            wantAction === 'remove') {
          for (const btn of buttons) {
            if (!incIsEditBtn(btn)) continue;
            let cur = btn.parentElement;
            for (let d = 0; cur && d < 5; d += 1, cur = cur.parentElement) {
              const blob = incLow(cur.innerText || '');
              if (!blob.includes(wantType)) continue;
              // Avoid the whole member card (also contains Add Another Income).
              if (incHasAddIncomeCue(blob) && blob.length > 120) continue;
              incPressEl(btn);
              return {
                ok: true,
                action: 'edit',
                via: 'card_pencil_fallback',
                wantType,
              };
            }
          }
        }
        return {
          ok: false,
          reason: 'action_not_found',
          wantType,
          wantAction,
          hadRow: Boolean(row),
        };
      };
      const incFindCardEl = (memberName) => {
        const want = incLow(memberName);
        const cards = incFindCards();
        // Prefer live DOM from Add-anchor so click targets the right card.
        const buttons = [...document.querySelectorAll(
          'button, [role=button], a'
        )].filter(incVisible).filter(incIsAddIncome);
        for (const btn of buttons) {
          const card = incCardFromAddBtn(btn);
          if (!card) continue;
          const n = incLow(card.name);
          if (n === want || n.startsWith(want) || want.startsWith(n)) {
            return card.el;
          }
        }
        // Name-only fallback from prior scrape shape.
        for (const c of cards) {
          const n = incLow(c.name || '');
          if (n === want || n.startsWith(want) || want.startsWith(n)) {
            // Re-resolve element via Add buttons when only the name matched.
            for (const btn of buttons) {
              const card = incCardFromAddBtn(btn);
              if (card && incLow(card.name) === n) return card.el;
            }
          }
        }
        return null;
      };
      const incFindIncomeModalRoot = () => {
        const selectors = [
          '[role=dialog]', '[aria-modal=true]', 'dialog',
          '.modal', '.MuiDialog-root', '.ReactModal__Content',
        ].join(', ');
        const nodes = [...document.querySelectorAll(selectors)].filter(incVisible);
        const cues = [
          'amount per month', 'select income type', 'pay frequency',
          'unemployment compensation', 'employer name', 'net income',
          'type of work', 'hours per week',
        ];
        for (const el of nodes) {
          const t = incLow(el.innerText || '');
          if (!t || t.length > 8000) continue;
          if (cues.some((c) => t.includes(c))) return el;
        }
        for (const el of nodes) {
          const buttons = [...el.querySelectorAll(
            'button, [role=button], a'
          )].filter(incVisible);
          if (buttons.some((b) => incBtnText(b) === 'save')) return el;
        }
        return null;
      };
      const incModalOpen = () => {
        // Prefer a real dialog root. Do not use body-wide "amount per month"
        // — after Save the phrase can linger or match unrelated copy while the
        // editor is already gone, which made verify fail after a real save.
        const root = incFindIncomeModalRoot();
        if (root) {
          const t = incLow(root.innerText || '');
          const hasSave = [...root.querySelectorAll(
            'button, [role=button], a'
          )].filter(incVisible).some((b) => incBtnText(b) === 'save');
          const hasCancel = [...root.querySelectorAll(
            'button, [role=button], a'
          )].filter(incVisible).some((b) => {
            const x = incBtnText(b);
            return x === 'cancel' || x.startsWith('cancel ');
          });
          if (t.includes('select income type')) return 'type';
          if (t.includes('amount per month') || (hasSave && hasCancel)) {
            if (t.includes('amount per month')) return 'amount';
            return 'open';
          }
          if (hasSave && hasCancel) return 'open';
          return '';
        }
        // Fallback: visible amount field + Save still means the editor is up.
        const amt = (typeof incAmountInput === 'function') ? incAmountInput() : null;
        if (amt && incFindModalSaveButton && incFindModalSaveButton()) return 'amount';
        return '';
      };
      const incNormTypeKey = (s) => incLow(s).replace(/['’`]/g, '');
      const incScrollIntoModalView = (el) => {
        if (!el) return;
        try { el.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
        let cur = el.parentElement;
        for (let d = 0; cur && d < 10; d += 1, cur = cur.parentElement) {
          const st = window.getComputedStyle(cur);
          const oy = (st.overflowY || st.overflow || '');
          if (oy === 'auto' || oy === 'scroll' || cur.scrollHeight > cur.clientHeight + 8) {
            try {
              const er = el.getBoundingClientRect();
              const cr = cur.getBoundingClientRect();
              const delta = (er.top + er.height / 2) - (cr.top + cr.height / 2);
              cur.scrollTop += delta;
            } catch (e) {}
            break;
          }
        }
      };
      const incClickableAncestor = (el) => {
        // SSA/SSDI chips are often a text leaf inside RN Pressable / button.
        let cur = el;
        for (let d = 0; cur && d < 8; d += 1, cur = cur.parentElement) {
          const role = (cur.getAttribute('role') || '').toLowerCase();
          const tag = (cur.tagName || '').toLowerCase();
          if (tag === 'button' || tag === 'a' || role === 'button' ||
              role === 'option' || role === 'menuitem' || role === 'radio' ||
              role === 'listitem') {
            return cur;
          }
          try {
            if (cur.tabIndex >= 0) return cur;
            const st = window.getComputedStyle(cur);
            if (st && st.cursor === 'pointer') return cur;
          } catch (e) {}
        }
        return el;
      };
      const incFindTypeOption = (label) => {
        const want = incNormTypeKey(label);
        if (!want) return null;
        const root = incFindIncomeModalRoot() || document.body;
        const scoreNode = (el) => {
          const raw = incNorm(el.innerText || el.textContent || '');
          const aria = incNorm(el.getAttribute('aria-label') || '');
          const sources = [];
          if (raw && raw.length <= 80) sources.push(raw);
          if (aria && aria.length <= 80) sources.push(aria);
          if (!sources.length) return null;
          let matched = false;
          let matchLen = 80;
          for (const src of sources) {
            const lines = src.split(/\r?\n/).map(incNorm).filter(Boolean);
            const candidates = lines.length ? lines : [src];
            for (const line of candidates) {
              const got = incNormTypeKey(line);
              if (got === want) {
                matched = true;
                matchLen = Math.min(matchLen, line.length);
                break;
              }
            }
            if (matched) break;
          }
          if (!matched) return null;
          const role = (el.getAttribute('role') || '').toLowerCase();
          const tag = (el.tagName || '').toLowerCase();
          let score = 10;
          if (tag === 'button' || role === 'button') score += 40;
          if (role === 'option' || role === 'menuitem') score += 30;
          if (incVisible(el)) score += 20;
          // Prefer the tightest label node (SSA / SSDI chips).
          score -= Math.min(matchLen, 60);
          return score;
        };
        // Visible first, then off-screen chips inside the scrollable modal.
        let best = null;
        let bestScore = -Infinity;
        const selectors = 'button, [role=button], a, li, div, span, p, label, [role=option], [role=menuitem]';
        for (const visibleOnly of [true, false]) {
          const nodes = [...root.querySelectorAll(selectors)].filter(
            (el) => (visibleOnly ? incVisible(el) : true)
          );
          for (const el of nodes) {
            const score = scoreNode(el);
            if (score == null) continue;
            if (score > bestScore) {
              best = el;
              bestScore = score;
            }
          }
          if (best) break;
        }
        return best;
      };
      const incPressTypeChip = (el) => {
        // Local copy so type-click does not depend on later helper order.
        if (!el) return '';
        const synth = {
          preventDefault() {},
          stopPropagation() {},
          nativeEvent: { preventDefault() {}, stopPropagation() {} },
          target: el,
          currentTarget: el,
          type: 'click',
          bubbles: true,
          cancelable: true,
        };
        const fk = Object.keys(el).find((k) => k.startsWith('__reactFiber'));
        let fiber = fk ? el[fk] : null;
        for (let d = 0; fiber && d < 16; d += 1, fiber = fiber.return) {
          const p = fiber.memoizedProps || {};
          if (typeof p.onPress === 'function') {
            try { p.onPress(synth); return 'onPress'; } catch (e) {}
          }
        }
        const pk = Object.keys(el).find((k) => k.startsWith('__reactProps'));
        const props = pk ? el[pk] : null;
        if (props && typeof props.onClick === 'function') {
          try { props.onClick(synth); return 'onClick'; } catch (e) {}
        }
        try { el.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
        try {
          el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
          el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
          el.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
          el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
          el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        } catch (e) {
          try {
            el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
            el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
          } catch (e2) {}
        }
        try { el.click(); } catch (e) {}
        return 'dom';
      };
      const incClickType = (label) => {
        const want = incNormTypeKey(label);
        if (!want) return { ok: false, reason: 'empty_label' };
        const best = incFindTypeOption(label);
        if (!best) return { ok: false, reason: 'type_not_found', want };
        const target = incClickableAncestor(best);
        incScrollIntoModalView(target);
        try { target.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
        // RN Web Pressable chips (SSA / SSDI) need onPress — plain click often
        // returns ok without advancing to Amount per month.
        let via = incPressTypeChip(target);
        if (via === 'dom' && best !== target) {
          const via2 = incPressTypeChip(best);
          if (via2 !== 'dom') via = via2;
        }
        return { ok: true, selected: label, via, want };
      };
      const incTypeSelectResult = (label) => {
        const kind = incModalOpen();
        const stillType = kind === 'type' || (
          !kind && (() => {
            const body = incLow(document.body ? document.body.innerText : '');
            return body.includes('select income type');
          })()
        );
        const amountOpen = kind === 'amount';
        const optionStillVisible = Boolean(incFindTypeOption(label));
        return {
          modal_kind: kind || '',
          still_on_type: stillType,
          amount_open: amountOpen,
          option_still_visible: optionStillVisible,
        };
      };
      const incSetInputValue = (el, want) => {
        if (!el) return false;
        el.focus();
        const value = String(want || '');
        try {
          const proto = el.tagName === 'TEXTAREA'
            ? window.HTMLTextAreaElement.prototype
            : window.HTMLInputElement.prototype;
          const desc = Object.getOwnPropertyDescriptor(proto, 'value');
          if (desc && desc.set) desc.set.call(el, value);
          else el.value = value;
        } catch (e) {
          el.value = value;
        }
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        try {
          el.dispatchEvent(new InputEvent('input', { bubbles: true, data: value }));
        } catch (e) {}
        return true;
      };
      const incFindLabeledInput = (needle) => {
        // Pick the input whose *tightest* ancestor mentions the needle.
        // Walking DOM order and returning the first hit wrongly maps
        // "hours per week" onto Monthly gross when a shared form ancestor
        // contains every label.
        const want = incLow(needle);
        if (!want) return null;
        const inputs = [...document.querySelectorAll(
          'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea'
        )].filter(incVisible).filter((el) => !el.disabled && !el.readOnly);
        let best = null;
        let bestLen = Infinity;
        for (const el of inputs) {
          let cur = el.parentElement;
          for (let d = 0; cur && d < 8; d += 1, cur = cur.parentElement) {
            const t = incLow(cur.innerText || '');
            if (!t || t.length > 800) continue;
            if (!t.includes(want)) continue;
            if (t.length < bestLen) {
              best = el;
              bestLen = t.length;
            }
            break;
          }
        }
        return best;
      };
      const incReadLabeledInput = (needle) => {
        const el = incFindLabeledInput(needle);
        if (!el) return { ok: false, value: '', reason: 'not_found' };
        return { ok: true, value: String(el.value || '').trim() };
      };
      const incAmountInput = () => {
        const labeled = incFindLabeledInput('amount per month');
        if (labeled) return labeled;
        const root = incFindIncomeModalRoot() || document.body;
        return [...root.querySelectorAll(
          'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea'
        )].filter(incVisible).filter((el) => !el.disabled && !el.readOnly)[0] || null;
      };
      const incFillAmount = (value) => {
        const want = String(value || '').trim();
        if (!want) return { ok: false, reason: 'empty_value' };
        const target = incAmountInput();
        if (!target) return { ok: false, reason: 'amount_input_missing' };
        incSetInputValue(target, want);
        const got = String(target.value || '').trim();
        return { ok: Boolean(got), value: got, want };
      };
      const incFocusAmount = () => {
        const target = incAmountInput();
        if (!target) return { ok: false, reason: 'amount_input_missing' };
        try {
          [...document.querySelectorAll('[data-pc-income-amt]')]
            .forEach((el) => el.removeAttribute('data-pc-income-amt'));
        } catch (e) {}
        target.setAttribute('data-pc-income-amt', '1');
        try { target.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
        target.focus();
        try { target.select && target.select(); } catch (e) {}
        return { ok: true };
      };
      const incReadAmount = () => {
        const target = incAmountInput();
        if (!target) return { ok: false, value: '' };
        return { ok: true, value: String(target.value || '').trim() };
      };
      const incBlurAmount = () => {
        const target = incAmountInput();
        if (!target) return false;
        try {
          target.dispatchEvent(new Event('blur', { bubbles: true }));
          target.blur();
        } catch (e) {}
        return true;
      };
      const incControlDisabled = (el) => {
        // Strict DOM/ARIA only — RN Web yellow Saves often have opacity/class
        // noise that falsely looked disabled and blocked the click.
        if (!el) return true;
        if (el.disabled) return true;
        if (el.getAttribute('disabled') !== null) return true;
        const aria = (el.getAttribute('aria-disabled') || '').toLowerCase();
        if (aria === 'true') return true;
        return false;
      };
      const incSaveLooksActive = (el) => {
        if (!el || incControlDisabled(el)) return false;
        try {
          const st = window.getComputedStyle(el);
          const bg = (st.backgroundColor || '').toLowerCase();
          // Pointcare enabled Save is solid yellow / gold.
          const m = bg.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
          if (m) {
            const r = Number(m[1]); const g = Number(m[2]); const b = Number(m[3]);
            if (r > 180 && g > 140 && b < 120) return true;
          }
        } catch (e) {}
        return !incControlDisabled(el);
      };
      const incFindModalSaveButton = () => {
        const root = incFindIncomeModalRoot() || document.body;
        const nodes = [...root.querySelectorAll(
          'button, [role=button], a, div, span'
        )].filter(incVisible);
        const candidates = [];
        for (const el of nodes) {
          const t = incBtnText(el);
          if (t !== 'save') continue;
          const r = el.getBoundingClientRect();
          if (r.width < 20 || r.height < 16) continue;
          candidates.push(el);
        }
        if (!candidates.length) return null;
        // Prefer an enabled / yellow Save; then the lowest (footer) control.
        candidates.sort((a, b) => {
          const ae = incSaveLooksActive(a) ? 1 : 0;
          const be = incSaveLooksActive(b) ? 1 : 0;
          if (ae !== be) return be - ae;
          const ad = incControlDisabled(a) ? 0 : 1;
          const bd = incControlDisabled(b) ? 0 : 1;
          if (ad !== bd) return bd - ad;
          return b.getBoundingClientRect().top - a.getBoundingClientRect().top;
        });
        return candidates[0];
      };
      const incMarkModalSave = () => {
        const el = incFindModalSaveButton();
        try {
          [...document.querySelectorAll('[data-pc-income-save]')]
            .forEach((n) => n.removeAttribute('data-pc-income-save'));
        } catch (e) {}
        if (!el) return { found: false, enabled: false };
        el.setAttribute('data-pc-income-save', '1');
        const disabled = incControlDisabled(el);
        const r = el.getBoundingClientRect();
        return {
          found: true,
          enabled: !disabled,
          disabled,
          active_look: incSaveLooksActive(el),
          label: incBtnText(el),
          x: Math.round(r.left + r.width / 2),
          y: Math.round(r.top + r.height / 2),
        };
      };
      const incSaveButtonState = () => {
        const marked = incMarkModalSave();
        return marked;
      };
      const incFillLabeled = (needle, value) => {
        const want = String(value || '').trim();
        if (!want) return false;
        const target = incFindLabeledInput(needle);
        if (!target) return false;
        return incSetInputValue(target, want);
      };
      const incNormPayFreq = (s) => incLow(s || '').replace(/[\s\-]+/g, '');
      const incIsPayFreqValue = (s) => {
        const n = incNormPayFreq(s || '');
        return (
          n === 'weekly' || n === 'biweekly' || n === 'byweekly' ||
          n === 'monthly' || n === 'annually' || n === 'annual' || n === 'yearly'
        );
      };
      const incPayFreqMatch = (spoken, option) => {
        const a = incNormPayFreq(spoken);
        const b = incNormPayFreq(option);
        if (!a || !b) return false;
        if (a === b) return true;
        const groups = [
          ['weekly', 'week'],
          ['biweekly', 'byweekly', 'bweekly', 'fortnightly'],
          ['monthly', 'month'],
          ['annually', 'annual', 'yearly', 'year'],
        ];
        for (const g of groups) {
          if (g.includes(a) && g.includes(b)) return true;
        }
        return false;
      };
      const incClickPayFreqEl = (el) => {
        if (!el) return false;
        try { el.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
        try {
          el.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }));
          el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
          el.dispatchEvent(new MouseEvent('pointerup', { bubbles: true }));
          el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
        } catch (e) {}
        try { el.click(); } catch (e) { return false; }
        return true;
      };
      const incReadPayFrequency = () => {
        // Prefer native select near the label.
        const selects = [...document.querySelectorAll('select')].filter(incVisible);
        for (const sel of selects) {
          let cur = sel.parentElement;
          let near = false;
          for (let d = 0; cur && d < 7; d += 1, cur = cur.parentElement) {
            const t = incLow(cur.innerText || '');
            if (t.includes('pay frequency')) { near = true; break; }
          }
          if (!near && selects.length !== 1) continue;
          const opt = sel.options[sel.selectedIndex];
          const raw = incNorm(opt ? (opt.textContent || opt.value || '') : (sel.value || ''));
          if (raw) return { ok: true, value: raw, kind: 'native_select' };
        }
        const root = incPayFrequencyFieldRoot();
        const scope = root || document.body;
        let best = '';
        let bestLen = Infinity;
        for (const el of scope.querySelectorAll(
          'button, [role=combobox], [role=button], div, span, p, label'
        )) {
          if (!incVisible(el)) continue;
          const own = incNorm(el.innerText || el.textContent || '');
          if (!incIsPayFreqValue(own)) continue;
          if (own.length < bestLen) {
            best = own;
            bestLen = own.length;
          }
        }
        if (best) return { ok: true, value: best, kind: 'custom' };
        return { ok: false, value: '', reason: 'not_found' };
      };
      const incPayFrequencyFieldRoot = () => {
        let best = null;
        let bestLen = Infinity;
        for (const el of document.querySelectorAll(
          'div, label, section, fieldset, li, form'
        )) {
          if (!incVisible(el)) continue;
          const t = incLow(el.innerText || el.textContent || '');
          if (!t.includes('pay frequency')) continue;
          // Tightest visible row that mentions the label (value optional).
          const r = el.getBoundingClientRect();
          if (r.width < 40 || r.height < 12 || r.height > 520) continue;
          if (t.length > 220) continue;
          if (t.length < bestLen) {
            best = el;
            bestLen = t.length;
          }
        }
        return best;
      };
      const incFindPayFrequencyLabel = () => {
        const nodes = [...document.querySelectorAll(
          'label, div, span, p, legend, h1, h2, h3, h4'
        )].filter(incVisible);
        for (const el of nodes) {
          const own = incNorm(el.innerText || el.textContent || '');
          if (!own || own.length > 40) continue;
          if (/^pay\s*frequency\b/i.test(own)) return el;
        }
        return null;
      };
      const incOpenPayFrequency = () => {
        // Native <select> — no open step needed.
        const selects = [...document.querySelectorAll('select')].filter(incVisible);
        for (const sel of selects) {
          let cur = sel.parentElement;
          let near = false;
          for (let d = 0; cur && d < 7; d += 1, cur = cur.parentElement) {
            const t = incLow(cur.innerText || '');
            if (t.includes('pay frequency')) { near = true; break; }
          }
          if (near || selects.length === 1) {
            return { ok: true, kind: 'native_select' };
          }
        }
        const root = incPayFrequencyFieldRoot();
        const labelEl = incFindPayFrequencyLabel();
        const scope = root || (labelEl && labelEl.parentElement) || document.body;
        const candidates = [...scope.querySelectorAll(
          'button, [role=button], [role=combobox], [aria-haspopup=listbox], [aria-haspopup=menu], div, span, p'
        )].filter(incVisible);
        let best = null;
        let bestScore = -1;
        for (const el of candidates) {
          const role = (el.getAttribute('role') || '').toLowerCase();
          const aria = (el.getAttribute('aria-haspopup') || '').toLowerCase();
          const own = incNorm(el.innerText || el.textContent || '');
          const ownLow = incLow(own);
          let score = 0;
          if (role === 'combobox') score += 50;
          if (aria === 'listbox' || aria === 'menu' || aria === 'true') score += 40;
          if (incIsPayFreqValue(own)) score += 60;
          else if (
            ownLow.includes('weekly') || ownLow.includes('monthly') ||
            ownLow.includes('annually') || ownLow.includes('bi-weekly') ||
            ownLow.includes('select') || ownLow.includes('choose')
          ) {
            // Prefer short value / placeholder chips over huge wrappers.
            if (own.length <= 24) score += 35;
            else if (own.length <= 80) score += 10;
            else continue;
          } else if (role || aria) {
            // Empty ARIA control next to the label (no value yet).
            if (!own || own.length <= 2) score += 25;
            else continue;
          } else {
            continue;
          }
          if (root && root.contains(el)) score += 20;
          if (score > bestScore) {
            best = el;
            bestScore = score;
          }
        }
        // Chevron / icon control on the right of the field (Pointcare custom select).
        if (!best && scope) {
          const withSvg = [...scope.querySelectorAll('div, span, button')]
            .filter(incVisible)
            .filter((el) => el.querySelector('svg'));
          if (withSvg.length) best = withSvg[withSvg.length - 1];
        }
        // Do NOT click the whole field root / label parent — that can hit
        // Cancel and dismiss the Pointcare income modal.
        // Last resort: smallest visible frequency value chip anywhere.
        if (!best) {
          for (const el of document.querySelectorAll(
            'div, span, button, p, label'
          )) {
            if (!incVisible(el)) continue;
            const own = incNorm(el.innerText || el.textContent || '');
            if (!incIsPayFreqValue(own)) continue;
            if (!best || own.length < incNorm(best.innerText || '').length) {
              best = el;
            }
          }
        }
        if (!best) {
          return { ok: false, reason: 'trigger_not_found' };
        }
        if (!incClickPayFreqEl(best)) {
          return { ok: false, reason: 'click_failed' };
        }
        return { ok: true, kind: 'custom', label: incNorm(best.innerText || '') };
      };
      const incClickPayFrequencyOption = (label) => {
        const want = String(label || '').trim();
        if (!want) return { ok: false, reason: 'empty_label' };
        // Native <select> near Pay frequency.
        const selects = [...document.querySelectorAll('select')].filter(incVisible);
        for (const sel of selects) {
          let cur = sel.parentElement;
          let near = false;
          for (let d = 0; cur && d < 7; d += 1, cur = cur.parentElement) {
            const t = incLow(cur.innerText || '');
            if (t.includes('pay frequency')) { near = true; break; }
          }
          if (!near && selects.length !== 1) continue;
          const opts = [...sel.options];
          const hit = opts.find((o) =>
            incPayFreqMatch(want, o.textContent || o.value || '')
          );
          if (!hit) {
            return {
              ok: false,
              reason: 'option_missing',
              want,
              options: opts.map((o) => incNorm(o.textContent || o.value || '')),
            };
          }
          sel.value = hit.value;
          sel.dispatchEvent(new Event('input', { bubbles: true }));
          sel.dispatchEvent(new Event('change', { bubbles: true }));
          return { ok: true, selected: incNorm(hit.textContent || hit.value || label) };
        }
        let best = null;
        let bestLen = Infinity;
        const nodes = [...document.querySelectorAll(
          'li, [role=option], [role=menuitem], button, [role=button], div, span, p, a, label'
        )].filter(incVisible);
        for (const el of nodes) {
          const raw = incNorm(el.innerText || el.textContent || '');
          if (!raw || raw.length > 40) continue;
          const lines = raw.split(/\r?\n/).map(incNorm).filter(Boolean);
          const candidates = lines.length ? lines : [raw];
          for (const line of candidates) {
            if (!incPayFreqMatch(want, line)) continue;
            // Prefer role=option / list items from an open menu.
            const role = (el.getAttribute('role') || '').toLowerCase();
            const bonus = (
              role === 'option' || role === 'menuitem' || el.tagName === 'LI'
            ) ? -5 : 0;
            const len = line.length + bonus;
            if (len < bestLen) {
              best = el;
              bestLen = len;
            }
          }
        }
        if (!best) {
          return { ok: false, reason: 'option_not_found', want };
        }
        if (!incClickPayFreqEl(best)) {
          return { ok: false, reason: 'click_failed', want };
        }
        return { ok: true, selected: label };
      };
      const incSelectPayFrequency = (label) => {
        const opened = incOpenPayFrequency();
        if (!opened.ok && opened.kind !== 'native_select') {
          // Still try options in case the menu is already open.
        }
        return incClickPayFrequencyOption(label);
      };
      const incPressModalEl = (el) => {
        if (!el) return false;
        try { el.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
        // RN Web / Radix often ignore plain click — mirror Save's press sequence.
        try {
          el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
          el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
          el.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
          el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
          el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        } catch (e) {
          try {
            el.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }));
            el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
            el.dispatchEvent(new MouseEvent('pointerup', { bubbles: true }));
            el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
          } catch (e2) {}
        }
        try { el.click(); } catch (e) {}
        return true;
      };
      const incClickModalButton = (label) => {
        const want = incLow(label);
        if (want === 'save') {
          const el = incFindModalSaveButton();
          if (!el) return { ok: false, reason: 'not_found' };
          // Only hard-block when the control itself is truly disabled.
          if (incControlDisabled(el) && !incSaveLooksActive(el)) {
            return { ok: false, reason: 'disabled' };
          }
          incPressModalEl(el);
          return { ok: true, scoped: true, active_look: incSaveLooksActive(el) };
        }
        const root = incFindIncomeModalRoot();
        // Prefer income modal footer — page-level Save/Next must not win.
        const scopes = root ? [root, document.body] : [document.body];
        const matchBtn = (el) => {
          const t = incBtnText(el);
          const aria = incLow(el.getAttribute('aria-label') || '');
          return t === want || aria === want || t.startsWith(want + ' ') ||
            aria.startsWith(want + ' ') || (want === 'delete' && (
              t === 'remove' || aria === 'remove' || aria.includes('delete')
            ));
        };
        for (const scope of scopes) {
          const nodes = [...scope.querySelectorAll(
            'button, [role=button], a'
          )].filter(incVisible);
          let exact = null;
          for (const el of nodes) {
            if (matchBtn(el)) exact = el;
          }
          if (exact) {
            incPressModalEl(exact);
            return { ok: true, scoped: scope !== document.body || Boolean(root) };
          }
          // Only fall through to document.body when modal root had no match.
          if (root && scope === root) continue;
          break;
        }
        return { ok: false, reason: 'not_found', want };
      };
      const incDigits = (s) => String(s || '').replace(/[^0-9.]/g, '');
      const incCardHasType = (cardText, wantType) => {
        const t = incLow(cardText || '');
        const w = incLow(wantType || '');
        if (!t || !w) return false;
        if (t.includes(w)) return true;
        const compact = t.replace(/\s+/g, ' ');
        return compact.includes(w);
      };
      const incMemberScopedSearch = (memberName, incomeType, amountHint) => {
        // Broader search when the tight card root missed nested income rows.
        const wantName = incLow(memberName);
        const wantType = incLow(incomeType);
        const wantAmt = incDigits(amountHint);
        const out = {
          type_found: false,
          amount_found: false,
          add_another: false,
          source: 'none',
          root_kind: '',
        };
        if (!wantName) return out;
        // Prefer scored card element first.
        const cardEl = incFindCardEl(memberName);
        const consider = (el, kind) => {
          if (!el) return;
          const blob = incLow(el.innerText || '');
          if (!blob) return;
          // Must mention this member (avoid other household cards).
          if (!(blob.includes(wantName) || wantName.split(' ')[0] &&
              blob.includes(wantName.split(' ')[0]))) {
            // Allow when we already resolved cardEl for this member.
            if (kind !== 'card') return;
          }
          const entries = incEntriesIn(el);
          for (const e of entries) {
            if (wantType && incLow(e.type || '') === wantType) {
              out.type_found = true;
              if (wantAmt && (
                incDigits(e.amount || '').includes(wantAmt) ||
                incDigits(e.summary || '').includes(wantAmt)
              )) {
                out.amount_found = true;
              }
            }
          }
          if (!out.type_found && wantType && incCardHasType(blob, wantType)) {
            out.type_found = true;
          }
          if (wantAmt && incDigits(blob).includes(wantAmt) && out.type_found) {
            out.amount_found = true;
          }
          if (/add\s+another\s+income/.test(blob)) out.add_another = true;
          if (out.type_found) {
            out.source = kind;
            out.root_kind = kind;
          }
        };
        consider(cardEl, 'card');
        if (out.type_found && (!wantAmt || out.amount_found)) return out;
        // Walk containers that mention the member name and an income type.
        const nodes = [...document.querySelectorAll(
          'section, article, li, div'
        )].filter(incVisible);
        let best = null;
        let bestScore = -1;
        for (const el of nodes) {
          const blob = incLow(el.innerText || '');
          if (!blob || blob.length > 3500) continue;
          if (!blob.includes(wantName)) continue;
          // Avoid whole-page wrappers that also list other members heavily.
          const otherHints = (blob.match(/\badd(?:\s+another|\s+an)?\s+income\b/g) || []).length;
          if (otherHints > 1) continue;
          let score = 0;
          if (wantType && blob.includes(wantType)) score += 50;
          if (wantAmt && incDigits(blob).includes(wantAmt)) score += 30;
          if (/add\s+another\s+income/.test(blob)) score += 15;
          const r = el.getBoundingClientRect();
          if (r.height >= 80 && r.height < 700) score += 10;
          if (score > bestScore) {
            best = el;
            bestScore = score;
          }
        }
        if (best) consider(best, 'member_scoped');
        return out;
      };
      const incIncomeAfterSave = (memberName, incomeType, amountHint) => {
        const modalKind = incModalOpen();
        const saveBtn = incFindModalSaveButton();
        const amountField = incAmountInput();
        const editorOpen = Boolean(modalKind) && (
          Boolean(saveBtn) || Boolean(amountField) || modalKind === 'type'
        );
        const cards = incFindCards();
        const wantName = incLow(memberName);
        const wantType = incLow(incomeType);
        const wantAmt = incDigits(amountHint);
        let typeOnCard = false;
        let amountOnCard = false;
        let cardText = '';
        let addAnother = false;
        let rootKind = 'none';
        for (const c of cards) {
          const n = incLow(c.name || '');
          if (!(n === wantName || n.startsWith(wantName) || wantName.startsWith(n))) {
            continue;
          }
          const el = incFindCardEl(memberName);
          cardText = incLow(el ? (el.innerText || '') : '');
          addAnother = /add(?:\s+another|\s+an)?\s+income/.test(cardText) ||
            /add\s+another\s+income/.test(cardText);
          rootKind = 'card';
          for (const e of (c.entries || [])) {
            const typ = incLow(e.type || '');
            if (wantType && typ === wantType) {
              typeOnCard = true;
              const sum = incLow(e.summary || '');
              const amt = incDigits(e.amount || '');
              if (wantAmt && (
                amt.includes(wantAmt) ||
                incDigits(sum).includes(wantAmt) ||
                sum.includes(wantAmt)
              )) {
                amountOnCard = true;
              }
            }
          }
          // Live re-scan of the chosen element (entries on scrape may be stale).
          if (el) {
            for (const e of incEntriesIn(el)) {
              if (wantType && incLow(e.type || '') === wantType) {
                typeOnCard = true;
                if (wantAmt && (
                  incDigits(e.amount || '').includes(wantAmt) ||
                  incDigits(e.summary || '').includes(wantAmt)
                )) {
                  amountOnCard = true;
                }
              }
            }
          }
          if (!typeOnCard && incCardHasType(cardText, wantType)) {
            typeOnCard = true;
          }
          if (wantAmt && cardText && incDigits(cardText).includes(wantAmt) && typeOnCard) {
            amountOnCard = true;
          }
          break;
        }
        if (!typeOnCard || (wantAmt && !amountOnCard)) {
          const broad = incMemberScopedSearch(memberName, incomeType, amountHint);
          if (broad.type_found) {
            typeOnCard = true;
            rootKind = broad.root_kind || 'member_scoped';
          }
          if (broad.amount_found) amountOnCard = true;
          if (broad.add_another) addAnother = true;
        }
        return {
          modal_open: editorOpen,
          modal_kind: modalKind || '',
          type_on_card: typeOnCard,
          amount_on_card: amountOnCard,
          card_count: cards.length,
          has_amount_hint: Boolean(wantAmt),
          add_another: addAnother,
          editor_closed: !editorOpen,
          root_kind: rootKind,
          member: memberName || '',
          income_type: incomeType || '',
        };
      };
      const incClickAddAnotherIncome = (memberName) => {
        const card = incFindCardEl(memberName);
        if (!card) {
          return { ok: false, reason: 'card_not_found', member: memberName };
        }
        const buttons = [...card.querySelectorAll(
          'button, [role=button], a'
        )].filter(incVisible);
        let hit = null;
        const labels = [];
        for (const btn of buttons) {
          const t = incBtnText(btn);
          const aria = incLow(btn.getAttribute('aria-label') || '');
          labels.push(t || aria);
          if (
            t.includes('add another income') ||
            aria.includes('add another income') ||
            (incIsAddIncome(btn) && (t.includes('another') || aria.includes('another')))
          ) {
            hit = btn;
            break;
          }
        }
        // Fallback: any Add Income on this card (first after save still works).
        if (!hit) {
          for (const btn of buttons) {
            if (incIsAddIncome(btn)) { hit = btn; break; }
          }
        }
        if (!hit) {
          return {
            ok: false,
            reason: 'add_another_not_found',
            member: memberName,
            labels: labels.slice(0, 12),
          };
        }
        try { hit.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
        try {
          hit.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }));
          hit.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
          hit.dispatchEvent(new MouseEvent('pointerup', { bubbles: true }));
          hit.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
        } catch (e) {}
        hit.click();
        return {
          ok: true,
          member: memberName,
          label: incBtnText(hit),
        };
      };
      const incParseYn = (t) => {
        // Lowercase FIRST — stripping [^a-z] before lowercasing turns "Yes" into "es".
        const n = incLow(String(t || ''))
          .replace(/[^a-z\s]/g, ' ')
          .replace(/\s+/g, ' ')
          .trim();
        if (n === 'yes' || n.startsWith('yes ')) return 'yes';
        if (n === 'no' || n.startsWith('no ')) return 'no';
        return '';
      };
      const incSynthEvent = (el) => ({
        preventDefault() {},
        stopPropagation() {},
        nativeEvent: { preventDefault() {}, stopPropagation() {} },
        target: el,
        currentTarget: el,
        type: 'click',
        bubbles: true,
        cancelable: true,
      });
      const incReactClick = (el) => {
        if (!el) return false;
        const key = Object.keys(el).find((k) => k.startsWith('__reactProps'));
        const props = key ? el[key] : null;
        if (!props || typeof props.onClick !== 'function') return false;
        try {
          props.onClick(incSynthEvent(el));
          return true;
        } catch (e) {
          return false;
        }
      };
      const incReactPress = (el) => {
        // RN Web / Radix RadioItem commits via onPress on Pressable, not DOM click.
        if (!el) return false;
        const fk = Object.keys(el).find((k) => k.startsWith('__reactFiber'));
        let fiber = fk ? el[fk] : null;
        for (let d = 0; fiber && d < 14; d += 1, fiber = fiber.return) {
          const p = fiber.memoizedProps || {};
          if (typeof p.onPress === 'function') {
            try {
              p.onPress(incSynthEvent(el));
              return true;
            } catch (e) {}
          }
        }
        return false;
      };
      const incUnemploymentFormControl = () => {
        // Prefer FormProvider (has setValue/trigger). Controller-only fibers
        // expose control._formValues but cannot prime Save for default No.
        const seeds = [];
        const g = document.querySelector('[role=radiogroup]');
        if (g) seeds.push(g);
        const save = [...document.querySelectorAll('button, [role=button]')]
          .find((b) => /^\\s*save\\s*$/i.test((b.innerText || '').trim()));
        if (save) seeds.push(save);
        let controlOnly = null;
        for (const el of seeds) {
          const fk = Object.keys(el).find((k) => k.startsWith('__reactFiber'));
          let fiber = fk ? el[fk] : null;
          for (let d = 0; fiber && d < 70; d += 1, fiber = fiber.return) {
            const p = fiber.memoizedProps || {};
            if (typeof p.handleSubmit === 'function' && typeof p.setValue === 'function') {
              return p;
            }
            if (!controlOnly && p.control && p.control._formValues) {
              controlOnly = p;
            }
          }
        }
        return controlOnly;
      };
      const incUnemploymentFormValue = () => {
        const form = incUnemploymentFormControl();
        if (!form) return '';
        const vals = (form.control && form.control._formValues)
          || (typeof form.getValues === 'function' ? form.getValues() : null)
          || {};
        return String(vals.unemploymentCompensationStatus || '');
      };
      const incUnemploymentWantCode = (want) => (
        want === 'yes' ? 'UNEMPLOYMENT_COMPENSATED' : 'NOT_UNEMPLOYMENT_COMPENSATED'
      );
      const incUnemploymentYnSelected = (want) => {
        const code = incUnemploymentWantCode(want);
        const formVal = incUnemploymentFormValue();
        if (formVal && formVal === code) return true;
        if (want === 'yes' && /amount per month/i.test(document.body.innerText || '')) {
          return true;
        }
        for (const g of document.querySelectorAll('[role=radiogroup]')) {
          if (!incVisible(g)) continue;
          const t = incLow(g.innerText || '');
          if (!(t.includes('yes') && t.includes('no'))) continue;
          for (const r of g.querySelectorAll('[role=radio]')) {
            if ((r.getAttribute('aria-checked') || '') !== 'true') continue;
            const label = incParseYn(
              (r.parentElement && (r.parentElement.innerText || '')) ||
              r.getAttribute('aria-label') ||
              (r.innerText || '')
            );
            if (label === want) return true;
          }
        }
        return false;
      };
      const incSetUnemploymentViaRadix = (want) => {
        const code = incUnemploymentWantCode(want);
        const groups = [...document.querySelectorAll('[role=radiogroup]')].filter(incVisible);
        for (const g of groups) {
          const fk = Object.keys(g).find((k) => k.startsWith('__reactFiber'));
          let fiber = fk ? g[fk] : null;
          for (let d = 0; fiber && d < 40; d += 1, fiber = fiber.return) {
            const p = fiber.memoizedProps || {};
            if (typeof p.onValueChange === 'function') {
              try {
                p.onValueChange(code);
                return true;
              } catch (e) {}
            }
            if (p.field && typeof p.field.onChange === 'function') {
              try {
                p.field.onChange(code);
                return true;
              } catch (e) {}
            }
            if (typeof p.onChange === 'function' && p.name === 'unemploymentCompensationStatus') {
              try {
                p.onChange(code);
                return true;
              } catch (e) {}
            }
          }
        }
        const form = incUnemploymentFormControl();
        if (form && typeof form.setValue === 'function') {
          try {
            form.setValue('unemploymentCompensationStatus', code, {
              shouldDirty: true,
              shouldValidate: true,
              shouldTouch: true,
            });
            return true;
          } catch (e) {}
        }
        return false;
      };
      const incPrimeUnemployedNoSaveable = () => {
        // Pointcare defaults to NOT_UNEMPLOYMENT_COMPENSATED. Save stays disabled
        // until the form is dirty — selecting No again does nothing. Shift the
        // default so No becomes a dirty, valid value and Save enables.
        const form = incUnemploymentFormControl();
        if (!form || typeof form.setValue !== 'function') {
          return { ok: false, reason: 'no_form' };
        }
        try {
          const control = form.control;
          if (control && control._defaultValues) {
            control._defaultValues.unemploymentCompensationStatus =
              'UNEMPLOYMENT_COMPENSATED';
          }
          if (control && control._options && control._options.defaultValues) {
            control._options.defaultValues.unemploymentCompensationStatus =
              'UNEMPLOYMENT_COMPENSATED';
          }
          form.setValue(
            'unemploymentCompensationStatus',
            'NOT_UNEMPLOYMENT_COMPENSATED',
            { shouldDirty: true, shouldValidate: true, shouldTouch: true }
          );
          if (typeof form.trigger === 'function') {
            try { form.trigger('unemploymentCompensationStatus'); } catch (e) {}
          }
          const save = [...document.querySelectorAll('button, [role=button]')]
            .find((b) => /^\\s*save\\s*$/i.test((b.innerText || '').trim()));
          return {
            ok: true,
            value: incUnemploymentFormValue(),
            saveEnabled: !!(save && !save.disabled),
          };
        } catch (e) {
          return { ok: false, reason: String(e) };
        }
      };
      const incClickYnEl = (el) => {
        if (!el) return false;
        try { el.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
        // Prefer RN onPress (Radix RadioItem) — synthetic onClick alone often
        // flips aria without committing RHF.
        if (incReactPress(el)) return true;
        if (incReactClick(el)) return true;
        const radio = (el.getAttribute('role') === 'radio')
          ? el
          : (el.querySelector && el.querySelector('[role=radio]'));
        if (radio && radio !== el) {
          if (incReactPress(radio) || incReactClick(radio)) return true;
        }
        try {
          el.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }));
          el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
          el.dispatchEvent(new MouseEvent('pointerup', { bubbles: true }));
          el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
        } catch (e) {}
        try { el.click(); } catch (e) { return false; }
        return true;
      };
      const incClickUnemploymentYn = (value) => {
        const wantRaw = incLow(value);
        const want = (wantRaw === 'yes' || wantRaw.startsWith('yes '))
          ? 'yes'
          : ((wantRaw === 'no' || wantRaw.startsWith('no ')) ? 'no' : '');
        if (!want) return { ok: false, reason: 'empty_value' };
        // Pointcare: radiogroup of Yes/No. Values UNEMPLOYMENT_COMPENSATED /
        // NOT_UNEMPLOYMENT_COMPENSATED. Pressable row wraps role=radio circle.
        const groups = [...document.querySelectorAll('[role=radiogroup]')]
          .filter(incVisible)
          .filter((g) => {
            const t = incLow(g.innerText || '');
            return t.includes('yes') && t.includes('no');
          });
        let group = null;
        for (const g of groups) {
          let cur = g.parentElement;
          let near = false;
          for (let d = 0; cur && d < 8; d += 1, cur = cur.parentElement) {
            if (incLow(cur.innerText || '').includes('unemployment compensation')) {
              near = true;
              break;
            }
          }
          if (near || groups.length === 1) {
            group = g;
            break;
          }
        }
        if (!group && groups.length) group = groups[0];
        let via = '';
        if (group) {
          // Prefer the role=radio circle whose parent row text matches.
          for (const radio of [...group.querySelectorAll('[role=radio]')].filter(incVisible)) {
            const rowText = incNorm(
              (radio.parentElement && radio.parentElement.innerText) ||
              (radio.getAttribute('aria-label') || '') ||
              (radio.innerText || '')
            );
            if (incParseYn(rowText) !== want) continue;
            const row = radio.closest('[tabindex]') || radio.parentElement;
            if (incClickYnEl(radio) || (row && incClickYnEl(row))) {
              via = 'role_radio';
              break;
            }
          }
          if (!via) {
            for (const el of [...group.querySelectorAll('div')].filter(incVisible)) {
              if (el.tabIndex < 0) continue;
              const own = incNorm(el.innerText || el.textContent || '');
              if (incParseYn(own) !== want || own.length > 12) continue;
              if (incClickYnEl(el)) {
                via = 'radiogroup_row';
                break;
              }
            }
          }
        }
        if (!via) {
          const scope = group || document.body;
          let leaf = null;
          for (const el of [...scope.querySelectorAll('div, span, button, label, p')].filter(incVisible)) {
            const own = incNorm(el.innerText || el.textContent || '');
            if (incParseYn(own) !== want || own.length > 4) continue;
            leaf = el;
            break;
          }
          if (!leaf) {
            // Last resort: drive Radix/RHF directly.
            if (incSetUnemploymentViaRadix(want)) {
              const verifiedRadix = incUnemploymentYnSelected(want);
              return {
                ok: verifiedRadix,
                selected: value,
                via: 'radix_value',
                verified: verifiedRadix,
                formValue: incUnemploymentFormValue(),
              };
            }
            return { ok: false, reason: 'yn_not_found', want };
          }
          let clickEl = leaf;
          for (let cur = leaf, d = 0; cur && d < 6; d += 1, cur = cur.parentElement) {
            if (cur.tabIndex >= 0 || (cur.getAttribute('role') || '') === 'radio') {
              clickEl = cur;
              break;
            }
          }
          if (!incClickYnEl(clickEl)) {
            if (incSetUnemploymentViaRadix(want)) {
              const verifiedRadix = incUnemploymentYnSelected(want);
              return {
                ok: verifiedRadix,
                selected: value,
                via: 'radix_value',
                verified: verifiedRadix,
                formValue: incUnemploymentFormValue(),
              };
            }
            return { ok: false, reason: 'click_failed', want };
          }
          via = 'text_ancestor';
        }
        let verified = incUnemploymentYnSelected(want);
        if (!verified && incSetUnemploymentViaRadix(want)) {
          via = via + '+radix';
          verified = incUnemploymentYnSelected(want);
        }
        // Fixtures without RHF still count a successful DOM click as ok.
        const hasForm = !!incUnemploymentFormControl();
        return {
          ok: verified || !hasForm,
          selected: value,
          via,
          verified,
          formValue: incUnemploymentFormValue(),
        };
      };
    """


def _income_phase(session: Any) -> str:
    phase = str(session.filled.get("app_income_phase") or "").strip()
    return phase if phase in _INCOME_PHASES else "type"


def _set_income_phase(session: Any, phase: str) -> None:
    session.filled["app_income_phase"] = phase


def _income_heard(session: Any) -> list[int]:
    raw = session.filled.get("app_income_heard") or "[]"
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [int(x) for x in data]
    except Exception:
        pass
    return []


def _mark_income_heard(session: Any, index: int) -> None:
    heard = _income_heard(session)
    if index not in heard:
        heard.append(index)
    session.filled["app_income_heard"] = json.dumps(heard)


def _member_types_map(session: Any) -> dict[str, list[str]]:
    raw = session.filled.get("app_income_member_types") or "{}"
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            out: dict[str, list[str]] = {}
            for key, val in data.items():
                if isinstance(val, list):
                    out[str(key)] = [str(x) for x in val if str(x).strip()]
            return out
    except Exception:
        pass
    return {}


def _set_member_types_map(session: Any, data: dict[str, list[str]]) -> None:
    session.filled["app_income_member_types"] = json.dumps(data)


def _record_member_type(session: Any, member: str, income_type: str) -> bool:
    """Record type for member. Returns True when this overrides a prior entry."""
    key = (member or "").strip() or "_unknown"
    mapping = _member_types_map(session)
    existing = list(mapping.get(key) or [])
    overridden = income_type in existing
    if overridden:
        existing = [t for t in existing if t != income_type]
    existing.append(income_type)
    mapping[key] = existing
    _set_member_types_map(session, mapping)
    return overridden


def _clear_income_member_state(session: Any) -> None:
    for key in (
        "app_income_current",
        "app_income_current_index",
        "app_income_pending_type",
        "app_income_seq",
    ):
        session.filled.pop(key, None)
    _set_income_phase(session, "type")


def _clear_all_income_state(session: Any) -> None:
    for key in _INCOME_FILLED_KEYS:
        session.filled.pop(key, None)


def _normalize_ssa_ssdi_asr(spoken: str) -> str:
    """Collapse letter-by-letter / misheard SSA and SSDI ASR into tokens."""
    text = (spoken or "").strip().lower()
    if not text:
        return ""
    text = text.replace("\u2019", "'").replace("`", "'")
    text = re.sub(r"[.!?…,]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Spoken letter names: "ess ess ay", "ess ess dee eye".
    text = re.sub(
        r"\bess\s*ess\s*(?:dee|d)\s*(?:eye|i|ee|e)\b",
        "ssdi",
        text,
    )
    text = re.sub(
        r"\bess\s*(?:dee|d)\s*(?:eye|i|ee|e)\b",
        "ssdi",
        text,
    )
    text = re.sub(
        r"\bess\s*ess\s*(?:ay|a|eh|e)\b",
        "ssa",
        text,
    )
    # Spelled letters with optional dots/spaces. A↔E and I↔E are common.
    # SSDI first so "s s d i" is not eaten by an SSA rule.
    text = re.sub(r"\bs\W*s\W*d\W*[ie]\b", "ssdi", text)
    text = re.sub(r"\bs\W*s\W*d\b", "ssdi", text)  # truncated "S S D"
    text = re.sub(r"\bs\W*d\W*[ie]\b", "ssdi", text)  # "S D I" / "SDI"
    text = re.sub(r"\bs\W*s\W*[ae]\b", "ssa", text)  # "S S A" / "S S E"
    # Whole-utterance letter soup / live ASR: "essay" → SSA (not SSI→SSA).
    letters = re.sub(r"[^a-z]", "", text)
    if letters in {"ssa", "sse", "ssay", "essay", "essa", "esay", "esae"}:
        return "ssa"
    if letters in {"ssdi", "ssde", "ssd", "sdi", "ssdy", "sdee", "asdee", "essdee"}:
        return "ssdi"
    # Keep multi-word phrases intact (e.g. social security administration).
    return text


def _looks_like_ambiguous_social_security(spoken: str) -> bool:
    """Bare 'Social Security' without SSA vs SSDI disambiguators."""
    text = re.sub(r"[.!?…,]+", " ", (spoken or "").strip().lower())
    text = re.sub(r"\s+", " ", text).strip()
    if not text or not re.search(r"\bsocial\s+security\b", text):
        return False
    if re.search(
        r"\b(disability|insurance|administration|admin|ssdi|ssa)\b", text
    ):
        return False
    return True


def map_income_type(spoken: str) -> str | None:
    """Map spoken text onto a Pointcare income-type label."""
    raw_spoken = (spoken or "").strip()
    lowered = re.sub(r"[.!?…]+$", "", raw_spoken.lower())
    lowered = re.sub(r"\s+", " ", lowered).strip()
    # Normalize curly/straight apostrophes so Workers' / Worker's both match.
    lowered = lowered.replace("\u2019", "'").replace("`", "'")
    if not lowered:
        return None
    # Drop leading yes/it's fillers so "Yes. It's SSA" still maps.
    lowered = re.sub(
        r"^(?:yes|yeah|yep|yup|yea|ok|okay)(?:[.,!]+\s*|\s+)",
        "",
        lowered,
    ).strip()
    lowered = re.sub(r"^(?:it'?s|it\s+is|that'?s)\s+", "", lowered).strip()
    normalized = _normalize_ssa_ssdi_asr(lowered)
    lowered = normalized or lowered
    if not lowered:
        return None
    # Longer / more specific cues first.
    # "Workers' compensation" (plural + apostrophe) must not miss.
    if re.search(
        r"\bworkers?'?\s*(comp|compensation)\b", lowered
    ) or re.search(r"\bworker'?s?\s+comp", lowered):
        return "Worker's compensation"
    if re.search(r"\bproperty\s+rental\b", lowered) or re.search(
        r"\brental\s+income\b", lowered
    ) or re.search(r"\brental\b", lowered):
        return "Property rental income"
    if re.search(r"\bretirement\b", lowered):
        return "Retirement income"
    if re.search(r"\bself[-\s]?employ", lowered):
        return "Self-employed"
    if re.search(r"\bunemploy", lowered):
        return "Unemployed"
    if re.search(r"\bemploy", lowered):
        return "Employed"
    # SSDI before SSA — never collapse both into one option.
    if (
        re.search(r"\bssdi\b", lowered)
        or re.search(
            r"\bsocial\s+security\s+disability(?:\s+insurance)?\b", lowered
        )
        or re.search(r"\bdisability\s+(income|benefit|insurance)\b", lowered)
        or re.search(r"\bon\s+disability\b", lowered)
        # Short answers during type ask: "disability" / "disabled".
        or re.fullmatch(r"disability|disabled|ssd|sdi", lowered)
    ):
        logger.info(
            "map_income_type raw=%r normalized=%r -> SSDI",
            raw_spoken,
            lowered,
        )
        return "SSDI"
    if (
        re.search(r"\bssa\b", lowered)
        or re.search(r"\bsse\b", lowered)  # ASR: A heard as E
        or re.search(r"\bsocial\s+security\s+administration\b", lowered)
        # Whole-word "essay" is the live ASR spelling of SSA (not substring).
        or re.fullmatch(r"essay|essa|esay", lowered)
    ):
        logger.info(
            "map_income_type raw=%r normalized=%r -> SSA",
            raw_spoken,
            lowered,
        )
        return "SSA"
    # Bare "Social Security" is ambiguous (SSA vs SSDI) — do not guess.
    if _looks_like_ambiguous_social_security(lowered):
        logger.info(
            "map_income_type raw=%r normalized=%r ambiguous social security",
            raw_spoken,
            lowered,
        )
        return None
    # SSI is a different program — do not silently map to SSA.
    if "pension" in lowered:
        return "Pension"
    alimony_compact = re.sub(r"[\s\-]+", "", lowered)
    if (
        "alimony" in alimony_compact
        or "alimoney" in alimony_compact
        or "allimony" in alimony_compact
        or "alimani" in alimony_compact
        or "alameny" in alimony_compact
        or "alimoni" in alimony_compact
        # Live ASR: "Alimony" → "Early morning."
        or "earlymorning" in alimony_compact
        or "earlymony" in alimony_compact
        or "earlymoney" in alimony_compact
        or "elementary" in alimony_compact
        or "allmoney" in alimony_compact
        or "allofmoney" in alimony_compact
        or re.search(r"\ba\s+limony\b", lowered)
        or re.search(r"\bali\s*mon(?:ey|y|i)\b", lowered)
        or re.search(r"\bali\s+money\b", lowered)
        or re.search(r"\bearly\s+morn", lowered)
        or re.search(r"\ball\s+(?:of\s+)?money\b", lowered)
    ):
        return "Alimony"
    if re.search(r"\bnone\b", lowered) or "no income" in lowered:
        return "None"
    # Never treat the "other" inside "another" as income type Other.
    if re.search(r"(?<![a-z])other(?![a-z])", lowered) and not re.search(
        r"\banother\b", lowered
    ):
        return "Other"
    for label in _INCOME_TYPE_LABELS:
        low = label.lower()
        if low == "other":
            continue  # handled above — substring "other" matches "another"
        # Short labels (SSA/SSDI/None) need word boundaries — avoid matching
        # "ssa" as a substring inside unrelated longer words.
        if len(low) <= 4:
            if re.search(rf"\b{re.escape(low)}\b", lowered):
                return label
            continue
        if low in lowered:
            return label
    return None


def _looks_like_truncated_ssa_ssdi(spoken: str) -> bool:
    """True when ASR looks like a cut-off SSA/SSDI attempt (e.g. \"It's a.\")."""
    text = re.sub(r"[.!?…,]+", " ", (spoken or "").strip().lower())
    text = re.sub(r"\s+", " ", text).strip()
    if not text or len(text) > 28:
        return False
    if map_income_type(text):
        return False
    return bool(
        re.search(
            r"\b("
            r"it'?s\s+a|it'?s\s+s|it\s+is\s+a|"
            r"s\s*s$|ess\s*ess$|ess$|"
            r"social\s+sec|social$"
            r")\b",
            text,
        )
    )


def _strip_another_income_filler(spoken: str) -> str:
    """Remove yes/another-income phrasing so a real type can be mapped."""
    text = (spoken or "").strip()
    if not text:
        return ""
    cleaned = re.sub(
        r"(?i)\b("
        r"yes|yeah|yep|yup|yea|"
        r"i\s+want\s+to|i'?d\s+like\s+to|please|"
        r"add(?:\s+an)?|another|income(?:\s+type)?|"
        r"for\s+me|thanks|thank\s+you"
        r")\b",
        " ",
        text,
    )
    cleaned = re.sub(r"[.!?…,]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


_INCOME_ONES = {
    "zero": 0,
    "oh": 0,
    "o": 0,
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_INCOME_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fourty": 40,  # common ASR misspelling
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_INCOME_NUMBER_FILLER = (
    r"doll[ao]?rs?|bucks|usd|per\s+month|a\s+month|monthly|"
    r"hours?|hrs?|per\s+week|a\s+week|weekly|"
    r"about|around|approximately|roughly|almost|nearly|"
    r"close\s+to|kind\s+of|kinda|sort\s+of|like|maybe|"
    r"i\s+don'?t\s+know|don'?t\s+know|not\s+sure|no\s+idea|"
    r"umm+|uh+|hmm+|um|well|say|its|it's|is|the|of|and"
)


def _words_to_number(text: str) -> float | None:
    """Parse spoken number phrases into a numeric value.

    Handles: thirty, twenty five, one hundred, two thousand five hundred,
    a hundred twenty, etc. Returns None when no number words are present.
    """
    raw = re.sub(r"[^a-z\s\-]", " ", (text or "").lower())
    raw = raw.replace("-", " ")
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return None
    # Drop connector words that do not change scale.
    tokens = [
        t
        for t in raw.split()
        if t not in ("and", "a", "an", "the")
    ]
    if not tokens:
        return None
    total = 0
    current = 0
    saw_number = False
    for tok in tokens:
        if tok in _INCOME_ONES:
            current += _INCOME_ONES[tok]
            saw_number = True
        elif tok in _INCOME_TENS:
            current += _INCOME_TENS[tok]
            saw_number = True
        elif tok == "hundred":
            current = (current or 1) * 100
            saw_number = True
        elif tok == "thousand":
            total += (current or 1) * 1000
            current = 0
            saw_number = True
        elif tok in ("million", "point"):
            # Income fields rarely need these; stop rather than invent.
            return None
        else:
            # Unknown token in the middle — abandon word parse.
            return None
    if not saw_number:
        return None
    return float(total + current)


def _extract_spoken_number(spoken: str, *, as_int: bool = False) -> str:
    """Pull a numeric form value from spoken Q9 answers.

    Digits win when present (``around 30``, ``100 dollars``, ``close to 20``).
    Otherwise spoken words are parsed (``thirty dollars``, ``one hundred``).
    """
    text = (spoken or "").strip()
    if not text:
        return ""
    # Prefer explicit digits anywhere in the utterance first.
    digit_src = text.replace(",", "").replace("$", " ")
    digit_m = re.search(r"(\d+(?:\.\d{1,2})?)", digit_src)
    if digit_m:
        num = digit_m.group(1)
        if as_int and "." not in num:
            return str(int(num))
        if as_int and num.endswith(".0"):
            return str(int(float(num)))
        return num

    cleaned = re.sub(rf"(?i)\b({_INCOME_NUMBER_FILLER})\b", " ", text)
    cleaned = cleaned.replace(",", " ").replace("$", " ").replace(".", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return ""
    # Try the whole cleaned phrase, then each clause (ASR often adds hedges).
    candidates = [cleaned] + [
        part.strip()
        for part in re.split(r"[;]|,(?!\d)", cleaned)
        if part.strip()
    ]
    for part in candidates:
        value = _words_to_number(part)
        if value is None:
            continue
        if as_int or value == int(value):
            return str(int(value))
        # Keep up to two decimal places for money.
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return ""


def _normalize_amount(spoken: str) -> str:
    """Extract a form-friendly numeric amount from spoken currency."""
    return _extract_spoken_number(spoken, as_int=False)


def _normalize_hours(spoken: str) -> str:
    """Extract hours per week from speech (digits or words)."""
    return _extract_spoken_number(spoken, as_int=True)


def _document_notice(income_type: str) -> str:
    """Spoken proof-of-income notice (matches Pointcare info banners)."""
    typ = (income_type or "").strip()
    if typ == "Employed":
        examples = "paystubs or W-2 forms"
    elif typ == "Self-employed":
        examples = "invoices or 1099 forms"
    else:
        examples = "award letters or benefit statements"
    return (
        "We'll ask you to upload proof of this income later, so have the "
        f"relevant documents ready. This could include {examples}."
    )


def _doc_notice_members(session: Any | None) -> list[str]:
    if session is None:
        return []
    raw = session.filled.get("app_income_doc_notice_members") or "[]"
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        pass
    return []


def _doc_notice_said_for(session: Any | None, member_name: str) -> bool:
    key = (member_name or "").strip()
    if not key:
        return False
    return key in _doc_notice_members(session)


def _mark_doc_notice_said(session: Any | None, member_name: str) -> None:
    if session is None:
        return
    key = (member_name or "").strip()
    if not key:
        return
    members = _doc_notice_members(session)
    if key not in members:
        members.append(key)
    session.filled["app_income_doc_notice_members"] = json.dumps(members)


def _with_document_notice(
    speech: str,
    income_type: str,
    *,
    session: Any | None = None,
    member_name: str = "",
) -> str:
    """Prepend proof notice at most once per household member."""
    body = (speech or "").strip()
    who = (member_name or "").strip()
    if session is not None and who and _doc_notice_said_for(session, who):
        return body
    notice = _document_notice(income_type)
    if session is not None and who:
        _mark_doc_notice_said(session, who)
    if not body:
        return notice
    return f"{notice} {body}"


def income_type_options_speech() -> str:
    """Full Select Income Type list — only for first ask or explicit options help."""
    return _INCOME_TYPE_OPTIONS_SPEECH


def _income_types_listed(session: Any | None) -> bool:
    if session is None:
        return False
    return str(session.filled.get("app_income_types_listed") or "") == "1"


def _mark_income_types_listed(session: Any | None) -> None:
    if session is None:
        return
    session.filled["app_income_types_listed"] = "1"


def _type_ask_speech(
    name: str,
    *,
    intro: bool = False,
    existing: list[str] | None = None,
    include_options: bool = False,
) -> str:
    who = name or "this person"
    prefix = f"I'm starting with {who}. "
    if intro:
        prefix = (
            "I'll add work and income for each person. "
            + prefix
        )
    types = [t for t in (existing or []) if t and t != "None"]
    if types:
        listed = ", ".join(types[:6])
        prefix += (
            f"I already see {listed} for {who}. "
            "You can add another income type, or say edit or delete followed by "
            "the income type. "
        )
    ask = "What income type should I enter?"
    if include_options:
        ask = f"{ask} {income_type_options_speech()}"
    return prefix + ask


def _type_ask_for_session(
    session: Any | None,
    name: str,
    *,
    intro: bool = False,
    existing: list[str] | None = None,
    force_options: bool = False,
) -> str:
    """Ask for income type; list choices only once unless force_options."""
    include = bool(force_options) or not _income_types_listed(session)
    if include:
        _mark_income_types_listed(session)
    return _type_ask_speech(
        name,
        intro=intro,
        existing=existing,
        include_options=include,
    )


def _amount_ask_speech(
    name: str,
    income_type: str,
    *,
    override: bool = False,
    session: Any | None = None,
) -> str:
    who = name or "this person"
    lead = (
        f"Okay, replacing the previous {income_type} amount for {who}. "
        if override
        else f"Okay, {income_type} for {who}. "
    )
    return _with_document_notice(
        lead + "What is the amount per month?",
        income_type,
        session=session,
        member_name=who,
    )


def _more_ask_speech(name: str, existing: list[str] | None = None) -> str:
    who = name or "this person"
    types = [t for t in (existing or []) if t and t != "None"]
    manage = ""
    if types:
        listed = ", ".join(types[:6])
        manage = (
            f" You can also say edit or delete one of the existing incomes "
            f"({listed})."
        )
    return (
        f"Do you want to add another income for {who}? "
        f"If yes, tell me the income type.{manage} "
        "Say No if you're done with this person."
    )


def _unemployed_compensation_ask_speech(
    name: str, *, session: Any | None = None
) -> str:
    who = name or "this person"
    return _with_document_notice(
        f"For {who}, do you receive unemployment compensation? "
        "Please say Yes or No.",
        "Unemployed",
        session=session,
        member_name=who,
    )


def _unemployed_amount_ask_speech(name: str) -> str:
    who = name or "this person"
    return f"What is the amount per month of unemployment compensation for {who}?"


def _confirm_income_speech(
    name: str, income_type: str, values: dict[str, Any]
) -> str:
    """Speak collected values and ask for Yes before Save."""
    who = name or "this person"
    typ = income_type or "that income"
    parts: list[str] = []
    if typ == "Employed":
        if values.get("pay"):
            parts.append(f"monthly gross income {values['pay']}")
        if values.get("hours"):
            parts.append(f"{values['hours']} hours per week")
        if values.get("frequency"):
            parts.append(f"pay frequency {values['frequency']}")
        if values.get("employer"):
            parts.append(f"employer {values['employer']}")
    elif typ == "Self-employed":
        if values.get("pay"):
            parts.append(f"estimated monthly net income {values['pay']}")
        if values.get("work"):
            parts.append(f"type of work {values['work']}")
    elif typ == "Unemployed":
        comp = str(values.get("compensation") or "").strip()
        if comp.lower() == "no":
            parts.append("no unemployment compensation")
        else:
            parts.append("receives unemployment compensation")
            if values.get("amount"):
                parts.append(f"amount per month {values['amount']}")
    else:
        if values.get("amount") or values.get("pay"):
            parts.append(
                f"amount per month {values.get('amount') or values.get('pay')}"
            )
    detail = "; ".join(parts) if parts else "the details we entered"
    return (
        f"For {who}, I have {typ}: {detail}. "
        "Does that look correct? Say Yes to save, or say edit if you want to "
        "change something."
    )


def _confirm_edit_field_choices(
    income_type: str, values: dict[str, Any] | None = None
) -> list[tuple[str, str]]:
    """Return (field_key, spoken_label) pairs the caller can edit before save."""
    typ = (income_type or "").strip()
    vals = values or {}
    if typ == "Employed":
        keys = ("pay", "hours", "frequency", "employer")
        labels = _EMPLOYED_EDIT_FIELD_LABELS
    elif typ == "Self-employed":
        keys = ("pay", "work")
        labels = _SELF_EDIT_FIELD_LABELS
    elif typ == "Unemployed":
        out: list[tuple[str, str]] = [("compensation", "unemployment compensation yes or no")]
        if str(vals.get("compensation") or "").strip().lower() != "no":
            out.append(("amount", "amount per month"))
        return out
    else:
        return [("amount", "amount per month")]
    return [(k, labels[k]) for k in keys if k in labels]


def _confirm_edit_pick_speech(
    name: str, income_type: str, values: dict[str, Any] | None = None
) -> str:
    who = name or "this person"
    choices = _confirm_edit_field_choices(income_type, values)
    labels = [label for _key, label in choices]
    if not labels:
        return (
            f"Which detail for {who} should I change? "
            "Say cancel if you want to start this income over."
        )
    if len(labels) == 1:
        joined = labels[0]
    elif len(labels) == 2:
        joined = f"{labels[0]} or {labels[1]}"
    else:
        joined = ", ".join(labels[:-1]) + f", or {labels[-1]}"
    return (
        f"Okay — which detail for {who} should I change? "
        f"You can say {joined}. "
        "Or say cancel to discard this income entry."
    )


def _confirm_edit_value_ask(field_key: str, income_type: str) -> str:
    typ = (income_type or "").strip()
    if field_key == "frequency":
        return (
            "What is the pay frequency — Weekly, Bi-weekly, Monthly, or Annually?"
        )
    if field_key == "hours":
        return "How many hours per week?"
    if field_key == "employer":
        return "What is the employer name?"
    if field_key == "work":
        return "What type of work is it?"
    if field_key == "compensation":
        return (
            "Do you receive unemployment compensation? Please say Yes or No."
        )
    if field_key == "amount" or (
        field_key == "pay" and typ in ("Employed", "Self-employed", "")
    ):
        if typ == "Employed":
            return "What is the monthly gross income before taxes?"
        if typ == "Self-employed":
            return "What is the estimated monthly net income after expenses?"
        return "What is the amount per month?"
    if field_key == "pay":
        return "What amount should I enter?"
    return "What should I change that to?"


def _map_confirm_edit_field(
    spoken: str, income_type: str, values: dict[str, Any] | None = None
) -> str | None:
    """Map 'hours' / 'pay frequency' / etc. onto a pending-values field key."""
    text = re.sub(r"[.!?…]+$", "", (spoken or "").strip().lower())
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    choices = _confirm_edit_field_choices(income_type, values)
    allowed = {key for key, _label in choices}
    # Exact / synonym matches.
    if re.search(r"\b(pay\s*frequency|frequency|how\s+often)\b", text):
        if "frequency" in allowed:
            return "frequency"
    if re.search(r"\b(hours?|hour\s+per\s+week|hrs?)\b", text):
        if "hours" in allowed:
            return "hours"
    if re.search(r"\b(employer|company|business\s+name|work\s+place|workplace)\b", text):
        if "employer" in allowed:
            return "employer"
    if re.search(r"\b(type\s+of\s+work|kind\s+of\s+work|what\s+work)\b", text):
        if "work" in allowed:
            return "work"
    if re.search(r"\b(compensation|unemployment)\b", text):
        if "compensation" in allowed:
            return "compensation"
    if re.search(
        r"\b(gross|monthly\s+(?:gross\s+)?income|pay(?!\s*frequency)|salary|wage|amount|income)\b",
        text,
    ):
        if "pay" in allowed:
            return "pay"
        if "amount" in allowed:
            return "amount"
    # Label substring match.
    for key, label in choices:
        if label in text or text in label:
            return key
    return None


def _wants_confirm_edit(spoken: str) -> bool:
    text = (spoken or "").strip().lower()
    if not text:
        return False
    if re.search(
        r"\b(edit|change|correct|fix|update|wrong|incorrect|not\s+correct|"
        r"not\s+right|looks?\s+wrong)\b",
        text,
    ):
        return True
    return False


def _wants_confirm_cancel(spoken: str) -> bool:
    text = (spoken or "").strip().lower()
    return bool(
        re.search(
            r"\b(cancel|start\s+over|discard|never\s*mind|forget\s+it)\b",
            text,
        )
    )


def _delete_confirm_speech(name: str, income_type: str) -> str:
    who = name or "this person"
    label = (income_type or "that").strip() or "that"
    # Types like "Retirement income" already end with "income".
    if not re.search(r"\bincome\b", label, flags=re.I):
        label = f"{label} income"
    return f"Can I delete the {label} for {who}? Please say Yes or No."


def _member_existing_types(session: Any, name: str) -> list[str]:
    return list(_member_types_map(session).get((name or "").strip()) or [])


def _parse_edit_delete_intent(spoken: str) -> tuple[str, str] | None:
    """Return ('edit'|'delete', income_type) when the utterance asks to manage."""
    text = (spoken or "").strip()
    lowered = text.lower()
    if not text:
        return None
    action = ""
    if re.search(r"\b(delete|deleted|deleting|remove|removed|removing)\b", lowered):
        action = "delete"
    elif re.search(r"\bedit\b|\bchange\b|\bupdate\b", lowered):
        action = "edit"
    if not action:
        return None
    # Strip the action word so type mapping is cleaner.
    residue = re.sub(
        r"\b(please\s+)?(delete|deleted|deleting|remove|removed|removing|"
        r"edit|change|update)\b",
        " ",
        lowered,
        flags=re.I,
    )
    residue = re.sub(r"\b(the|income|for|entry|source)\b", " ", residue)
    picked = map_income_type(residue) or map_income_type(text)
    if not picked or picked == "None":
        return action, ""
    return action, picked


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


# Employed modal fields (Pointcare Work & Income).
_INCOME_EMPLOYED_SEQ = (
    (
        "pay",
        "app_income_employed",
        "What is the monthly gross income before taxes?",
    ),
    ("hours", "app_income_employed_hours", "How many hours per week?"),
    (
        "frequency",
        "app_income_employed_frequency",
        "What is the pay frequency — Weekly, Bi-weekly, Monthly, or Annually?",
    ),
    ("employer", "app_income_employed_employer", "What is the employer name?"),
)
# Self-employed modal fields (Pointcare Work & Income).
_INCOME_SELF_SEQ = (
    (
        "pay",
        "app_income_self",
        "What is the estimated monthly net income after expenses?",
    ),
    ("work", "app_income_self_work", "What type of work is it?"),
)

_EMPLOYED_PAY_FREQUENCY_LABELS = (
    "Weekly",
    "Bi-weekly",
    "Monthly",
    "Annually",
)

# Field label needles used to locate employed / self-employed modal inputs.
_EMPLOYED_FIELD_NEEDLES = {
    "pay": "monthly gross income",
    "hours": "hours per week",
    "employer": "employer name",
}
_SELF_FIELD_NEEDLES = {
    "pay": "estimated monthly net income",
    "work": "type of work",
}


def map_employed_pay_frequency(spoken: str) -> str | None:
    """Map spoken pay frequency onto Employed dropdown labels."""
    text = re.sub(r"[.!?…]+$", "", (spoken or "").strip().lower())
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    compact = re.sub(r"[\s\-]+", "", text)
    if (
        "biweekly" in compact
        or compact in {"byweekly", "buyweekly", "biweek", "bweekly"}
        or re.search(
            r"\b(?:bi\s*week(?:ly)?|every\s*(?:two|2)\s*weeks?|fortnight(?:ly)?)\b",
            text,
        )
    ):
        return "Bi-weekly"
    if re.search(
        r"\b(year(?:ly)?|annual(?:ly)?|once\s+a\s+year|per\s+year)\b", text
    ):
        return "Annually"
    if re.search(
        r"\b(month(?:ly)?|once\s+a\s+month|per\s+month|every\s+month)\b", text
    ):
        return "Monthly"
    if re.search(
        r"\b(week(?:ly)?|once\s+a\s+week|per\s+week|every\s+week)\b", text
    ):
        return "Weekly"
    for label in _EMPLOYED_PAY_FREQUENCY_LABELS:
        if label.lower() in text:
            return label
    return None


async def probe_income_screen(page: Page) -> dict[str, Any]:
    """Visible-DOM probe for Work & Income (ignores hidden stale nodes)."""
    try:
        raw = await page.evaluate(
            "() => {"
            + _income_dom_js()
            + """
              const headingEls = [...document.querySelectorAll(
                'h1, h2, h3, [role=heading]'
              )].filter(incVisible);
              const headings = headingEls
                .map((el) => incNorm(el.innerText || el.textContent || ''))
                .filter(Boolean);
              const headingLow = incLow(headings.join(' | '));
              const hasIncomeHeading = (
                /work\\s*&\\s*income|work and income|income information/.test(headingLow)
                || headings.some((h) => /\\bincome\\b/i.test(h) && !/tax/i.test(h))
              );
              const bodyVisible = incNorm(
                document.body ? (document.body.innerText || '') : ''
              );
              const bodyLow = incLow(bodyVisible);
              const addButtons = [...document.querySelectorAll(
                'button, [role=button], a'
              )].filter(incVisible).filter(incIsAddIncome);
              const cards = incFindCards();
              const loading = (
                bodyVisible.length < 40
                || (/loading|please wait/.test(bodyLow) && bodyVisible.length < 120)
              );
              const hasIncomePhrase = (
                /work\\s*&\\s*income|work and income/.test(bodyLow)
                || /add(?:\\s+another|\\s+an)?\\s+income/.test(bodyLow)
                || /select income type|income type/.test(bodyLow)
              );
              const hasTypePalette = (
                bodyLow.includes('employed')
                && bodyLow.includes('self-employed')
                && bodyLow.includes('none')
              );
              const isIncome = !!(
                !loading
                && (
                  hasIncomeHeading
                  || (hasIncomePhrase && (addButtons.length >= 1 || cards.length >= 1))
                  || addButtons.length >= 1
                  || cards.length >= 1
                  || hasTypePalette
                )
              );
              return {
                is_income: isIncome,
                loading: loading && !isIncome,
                heading: headings[0] || '',
                headings: headings.slice(0, 6),
                add_count: addButtons.length,
                card_count: cards.length,
                card_names: cards.map((c) => c.name).slice(0, 8),
                snippet: bodyVisible.slice(0, 200),
                reason: isIncome
                  ? (hasIncomeHeading
                      ? 'income_heading'
                      : (addButtons.length ? 'add_income_buttons' : 'income_phrase'))
                  : (loading ? 'loading' : 'no_income_signal'),
              };
            }"""
        )
        if isinstance(raw, dict):
            return raw
    except Exception:
        logger.exception("probe_income_screen failed")
    return {
        "is_income": False,
        "loading": False,
        "heading": "",
        "add_count": 0,
        "card_count": 0,
        "reason": "probe_failed",
        "snippet": "",
    }


async def scrape_income_cards(page: Page) -> list[dict[str, Any]]:
    """Read Work & Income member rows that expose Add Income."""
    try:
        raw = await page.evaluate(
            "() => {" + _income_dom_js() + " return incFindCards(); }"
        )
        if not isinstance(raw, list):
            return []
        rows = [r for r in raw if isinstance(r, dict) and str(r.get("name") or "").strip()]
        logger.info(
            "scrape_income_cards found=%s names=%s",
            len(rows),
            [str(r.get("name") or "") for r in rows],
        )
        return rows
    except Exception:
        logger.exception("scrape_income_cards failed")
        return []


async def scrape_income_cards_with_retry(
    page: Page, *, attempts: int = 3, delay_ms: int = 400
) -> list[dict[str, Any]]:
    """Retry card scrape briefly — SPA rows can lag after tax Next."""
    cards: list[dict[str, Any]] = []
    for attempt in range(max(1, attempts)):
        cards = await scrape_income_cards(page)
        if cards:
            return cards
        if attempt + 1 < attempts:
            await page.wait_for_timeout(delay_ms)
    return cards


def _card_entries(card: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(card, dict):
        return []
    raw = card.get("entries") or []
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict) and str(e.get("type") or "").strip()]


async def _click_income_entry_action(
    page: Page, member_name: str, income_type: str, action: str
) -> dict[str, Any]:
    """Click edit/pencil (or rare row delete) on a saved income row.

    Returns a result dict. Never opens Add Another Income / type select.
    """
    try:
        result = await page.evaluate(
            "({ memberName, incomeType, action }) => {"
            + _income_dom_js()
            + " return incClickEntryAction(memberName, incomeType, action); }",
            {
                "memberName": member_name,
                "incomeType": income_type,
                "action": action,
            },
        )
        out = result if isinstance(result, dict) else {"ok": False, "reason": "bad_result"}
        if out.get("ok"):
            await page.wait_for_timeout(400)
            logger.info(
                "click_income_entry_action ok member=%r type=%r action=%r via=%r",
                member_name,
                income_type,
                action,
                out.get("via"),
            )
            return out
        logger.warning(
            "click_income_entry_action failed member=%r type=%r action=%r result=%r",
            member_name,
            income_type,
            action,
            out,
        )
        return out
    except Exception:
        logger.exception(
            "click_income_entry_action failed member=%r type=%r action=%r",
            member_name,
            income_type,
            action,
        )
        return {"ok": False, "reason": "exception"}


async def _income_edit_modal_ready(
    page: Page, member_name: str = "", income_type: str = ""
) -> dict[str, Any]:
    """Confirm the income edit modal is open (Delete visible; optional identity)."""
    try:
        raw = await page.evaluate(
            """({ memberName, incomeType }) => {
              const low = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const visible = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const st = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' &&
                  st.display !== 'none' && st.opacity !== '0';
              };
              const nodes = [...document.querySelectorAll(
                'button, [role=button], a'
              )].filter(visible);
              let deleteBtn = null;
              for (const el of nodes) {
                const t = low(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
                if (t === 'delete' || t.startsWith('delete ')) { deleteBtn = el; break; }
              }
              if (!deleteBtn) return { ok: false, reason: 'delete_btn_missing' };
              const body = low(document.body && document.body.innerText);
              const wantMember = low(memberName);
              const wantType = low(incomeType);
              return {
                ok: true,
                hasDelete: true,
                memberMatch: !wantMember || body.includes(wantMember),
                typeMatch: !wantType || body.includes(wantType),
              };
            }""",
            {"memberName": member_name, "incomeType": income_type},
        )
        return raw if isinstance(raw, dict) else {"ok": False}
    except Exception:
        logger.exception("income_edit_modal_ready failed")
        return {"ok": False, "reason": "exception"}


async def _click_income_modal_delete(page: Page) -> dict[str, Any]:
    """Click Delete inside the open income edit modal (JS, then Playwright)."""
    result: dict[str, Any] | None = None
    try:
        result = await page.evaluate(
            "() => {"
            + _income_dom_js()
            + " return incClickModalButton('Delete'); }"
        )
        if isinstance(result, dict) and result.get("ok"):
            logger.info("click_income_modal_delete via=dom")
            return {"ok": True, "via": "dom"}
    except Exception:
        logger.exception("click_income_modal_delete dom failed")
        result = {"ok": False, "reason": "dom_exception"}
    # Playwright role click — RN Web sometimes ignores synthetic DOM events.
    try:
        loc = page.get_by_role("button", name=re.compile(r"^\s*delete\s*$", re.I))
        if await loc.count() < 1:
            loc = page.get_by_text(re.compile(r"^\s*delete\s*$", re.I))
        target = loc.first
        await expect(target).to_be_visible(timeout=2500)
        await target.scroll_into_view_if_needed(timeout=1500)
        await target.click(timeout=2500, force=True)
        logger.info("click_income_modal_delete via=playwright")
        return {"ok": True, "via": "playwright"}
    except Exception as exc:
        logger.warning(
            "click_income_modal_delete playwright failed err=%s dom=%r",
            exc,
            result,
        )
        return {
            "ok": False,
            "reason": "delete_not_clicked",
            "dom": result,
            "error": str(exc),
        }


async def _verify_income_absent(
    page: Page, member_name: str, income_type: str
) -> bool:
    """True when the member card no longer lists the income type."""
    want = (income_type or "").strip().lower()
    who = (member_name or "").strip().lower()
    if not want:
        return False
    for _ in range(10):
        cards = await scrape_income_cards(page)
        matched_member = False
        still_present = False
        for card in cards:
            name = str(card.get("name") or "").strip().lower()
            if who and not (
                name == who or name.startswith(who) or who.startswith(name)
            ):
                continue
            matched_member = True
            for entry in _card_entries(card):
                typ = str(entry.get("type") or "").strip().lower()
                if typ == want:
                    still_present = True
                    break
            break
        if matched_member and not still_present:
            logger.info(
                "income_delete_verified_absent member=%r type=%r",
                member_name,
                income_type,
            )
            return True
        # No cards yet / SPA lag — keep polling briefly.
        if not cards:
            await page.wait_for_timeout(250)
            continue
        if matched_member and still_present:
            await page.wait_for_timeout(250)
            continue
        # Member card missing from scrape — treat cautiously as not verified.
        await page.wait_for_timeout(250)
    logger.warning(
        "income_delete_verify_absent_failed member=%r type=%r",
        member_name,
        income_type,
    )
    return False


async def _delete_income_entry(
    page: Page, member_name: str, income_type: str
) -> bool:
    """Open the saved row via pencil/edit, then click Delete in the modal.

    Pointcare Work & Income rows expose an edit pencil only; Delete lives inside
    the edit modal footer. This must NEVER fall through to Add Another Income
    or type-select (that path is edit/add, not delete).
    """
    logger.info(
        "delete_income_entry start member=%r type=%r",
        member_name,
        income_type,
    )
    pencil = await _click_income_entry_action(
        page, member_name, income_type, "edit"
    )
    if not pencil.get("ok"):
        # Rare: row trash control, or JS opens pencil under action=delete.
        direct = await _click_income_entry_action(
            page, member_name, income_type, "delete"
        )
        if direct.get("ok") and direct.get("action") == "delete" and (
            direct.get("via") in ("type_row", "near_type")
        ):
            # True row-level delete control (no modal).
            ok_absent = await _verify_income_absent(page, member_name, income_type)
            return ok_absent
        if not direct.get("ok"):
            logger.warning(
                "delete_income_entry pencil not found member=%r type=%r pencil=%r direct=%r",
                member_name,
                income_type,
                pencil,
                direct,
            )
            return False
        # Pencil was opened via delete action — continue to modal Delete.
        pencil = direct

    # Wait for edit modal footer (Delete) before pressing it.
    try:
        await page.get_by_role(
            "button", name=re.compile(r"^\s*delete\s*$", re.I)
        ).first.wait_for(state="visible", timeout=3000)
    except Exception:
        await page.wait_for_timeout(600)

    modal = await _income_edit_modal_ready(page, member_name, income_type)
    logger.info(
        "delete_income_entry modal member=%r type=%r modal=%r pencil=%r",
        member_name,
        income_type,
        modal,
        pencil,
    )
    if not modal.get("ok"):
        logger.warning(
            "delete_income_entry modal not ready member=%r type=%r modal=%r",
            member_name,
            income_type,
            modal,
        )
        try:
            await _click_income_cancel(page)
        except Exception:
            pass
        return False
    if modal.get("memberMatch") is False or modal.get("typeMatch") is False:
        logger.warning(
            "delete_income_entry modal identity mismatch member=%r type=%r modal=%r",
            member_name,
            income_type,
            modal,
        )
        try:
            await _click_income_cancel(page)
        except Exception:
            pass
        return False

    clicked = await _click_income_modal_delete(page)
    if not clicked.get("ok"):
        logger.warning(
            "delete_income_entry modal Delete failed member=%r type=%r result=%r",
            member_name,
            income_type,
            clicked,
        )
        try:
            await _click_income_cancel(page)
        except Exception:
            logger.exception("delete_income_entry cancel after failed Delete")
        return False

    await page.wait_for_timeout(450)
    absent = await _verify_income_absent(page, member_name, income_type)
    if not absent:
        # Some SPAs keep the type briefly; accept Delete click if modal closed.
        logger.warning(
            "delete_income_entry post-verify absent soft-fail member=%r type=%r",
            member_name,
            income_type,
        )
    logger.info(
        "delete_income_entry done member=%r type=%r delete_via=%r absent=%s",
        member_name,
        income_type,
        clicked.get("via"),
        absent,
    )
    return True


def _set_pending_confirm(
    session: Any, income_type: str, values: dict[str, Any], summary: str
) -> None:
    session.filled["app_income_pending_type"] = income_type
    session.filled["app_income_pending_values"] = json.dumps(values)
    session.filled["app_income_pending_summary"] = summary
    _set_income_phase(session, "confirm")


def _pending_income_values(session: Any) -> dict[str, Any]:
    raw = session.filled.get("app_income_pending_values") or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _amount_digits(value: str) -> str:
    return re.sub(r"[^0-9.]", "", str(value or "").strip())


def _amounts_match(left: str, right: str) -> bool:
    a = _amount_digits(left)
    b = _amount_digits(right)
    if not a or not b:
        return False
    try:
        return abs(float(a) - float(b)) < 0.009
    except Exception:
        return a == b


async def _income_save_button_state(page: Page) -> dict[str, Any]:
    """Read whether the income modal Save control is enabled."""
    try:
        raw = await page.evaluate(
            "() => {" + _income_dom_js() + " return incSaveButtonState(); }"
        )
        return raw if isinstance(raw, dict) else {}
    except Exception:
        logger.exception("income_save_button_state failed")
        return {}


async def _ensure_pending_amount_on_form(
    page: Page, amount: str
) -> dict[str, Any]:
    """Re-type Amount per month until the value sticks and Save enables."""
    want = str(amount or "").strip()
    if not want:
        return {"ok": True, "skipped": True}
    current = await _read_income_amount(page)
    save_state = await _income_save_button_state(page)
    if _amounts_match(current, want) and save_state.get("enabled"):
        return {"ok": True, "already": True, "value": current}
    # DOM can show the digits while React still leaves Save disabled — re-type.
    filled = await _fill_income_amount(page, want)
    if not filled:
        return {"ok": False, "reason": "fill_failed", "value": current}
    again = await _read_income_amount(page)
    if not _amounts_match(again, want):
        return {"ok": False, "reason": "value_mismatch", "value": again, "want": want}
    save_state = await _income_save_button_state(page)
    # Yellow/active Save can still report aria quirks — do not block confirm;
    # the click path will attempt Playwright click + post-save verify.
    if save_state.get("found") and not save_state.get("enabled"):
        if save_state.get("active_look"):
            logger.info(
                "income_amount_ready_save_looks_active want=%r state=%r",
                want,
                save_state,
            )
            return {"ok": True, "refilled": True, "value": again, "save": save_state}
        logger.warning(
            "income_amount_filled_save_maybe_disabled want=%r value=%r state=%r",
            want,
            again,
            save_state,
        )
        # Still proceed — prior heuristic blocked real yellow Saves.
        return {
            "ok": True,
            "refilled": True,
            "value": again,
            "save": save_state,
            "save_uncertain": True,
        }
    return {"ok": True, "refilled": True, "value": again}


async def _finish_income_saved_ok(
    call_id: str,
    session: Any,
    text: str,
    name: str,
    pending: str,
    *,
    note: str = "Saved income after confirm",
) -> dict[str, Any]:
    from .app_stage import _app_ok, _set_pending_question

    if pending:
        _record_member_type(session, name, pending)
    for key in (
        "app_income_pending_type",
        "app_income_pending_values",
        "app_income_pending_summary",
        "app_income_edit_mode",
        "app_income_seq",
    ):
        session.filled.pop(key, None)
    _set_income_phase(session, "more")
    speech = _more_ask_speech(name, _member_existing_types(session, name))
    _set_pending_question(session, "app_income_more", speech)
    return _app_ok(
        call_id,
        session,
        text,
        "app_income_more",
        note,
        spoken_override=speech,
    )


async def _save_confirmed_income(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    name: str,
) -> dict[str, Any]:
    """Click Save after spoken confirmation, verify card, then ask another income."""
    from .app_stage import _set_pending_question

    pending = str(session.filled.get("app_income_pending_type") or "").strip()
    values = _pending_income_values(session)
    amount = _pending_verify_amount(values)
    summary = str(session.filled.get("app_income_pending_summary") or "").strip()

    # Prior Yes may have already saved — card shows Pension $50 / month.
    # Do not skip Save while the Employed/detail modal is still open.
    already = await _income_card_shows_entry(page, name, pending, amount)
    if _income_save_state_ok(already, want_type=pending, amount=amount):
        logger.info(
            "income_already_on_card_before_save member=%r type=%r state=%r",
            name,
            pending,
            already,
        )
        return await _finish_income_saved_ok(
            call_id,
            session,
            text,
            name,
            pending,
            note="Income already on card after confirm",
        )

    # Re-apply deferred pay frequency before Save so the modal can submit.
    freq = str(session.filled.get("app_income_freq_needs_ui") or "").strip()
    if freq and pending == "Employed":
        if await _select_employed_pay_frequency(page, freq):
            session.filled.pop("app_income_freq_needs_ui", None)
        else:
            logger.warning(
                "pay_frequency_still_unset_before_save want=%r", freq
            )

    if amount:
        ensured = await _ensure_pending_amount_on_form(page, amount)
        if not ensured.get("ok"):
            # Editor may already be closed because Save succeeded on a prior try.
            again = await _income_card_shows_entry(page, name, pending, amount)
            if _income_save_state_ok(again, want_type=pending, amount=amount):
                logger.info(
                    "income_on_card_after_amount_miss member=%r type=%r state=%r",
                    name,
                    pending,
                    again,
                )
                return await _finish_income_saved_ok(
                    call_id,
                    session,
                    text,
                    name,
                    pending,
                    note="Income found on card after amount miss",
                )
            logger.warning(
                "income_confirm_amount_not_on_form name=%r type=%r ensured=%r",
                name,
                pending,
                ensured,
            )
            reason = str(ensured.get("reason") or "")
            if reason == "save_still_disabled":
                speech = (
                    "The amount is on the screen, but Save is still disabled. "
                    + (summary or "Should I try saving again? Please say Yes or No.")
                )
                err = "income_confirm_save_disabled"
            else:
                speech = (
                    "I couldn't get that amount onto the form. "
                    + (summary or "Should I try saving again? Please say Yes or No.")
                )
                err = "income_confirm_amount_missing"
            _set_pending_question(session, "app_income_confirm", speech)
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": err,
                "step": "app_income_confirm",
                "phase": "application",
            }

    saved = await _click_income_save(
        page,
        member_name=name,
        income_type=pending,
        amount=amount,
    )
    if not saved:
        # Click/verify raced the UI — re-check the card before telling the user.
        await page.wait_for_timeout(400)
        final = await _income_card_shows_entry(page, name, pending, amount)
        if _income_save_state_ok(final, want_type=pending, amount=amount):
            logger.info(
                "income_on_card_after_verify_miss member=%r type=%r state=%r",
                name,
                pending,
                final,
            )
            return await _finish_income_saved_ok(
                call_id,
                session,
                text,
                name,
                pending,
                note="Income found on card after verify miss",
            )
        speech = (
            "Save didn't go through — the income wasn't added on the card. "
            + (summary or "Should I try saving again? Please say Yes or No.")
        )
        _set_pending_question(session, "app_income_confirm", speech)
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "income_confirm_save_failed",
            "step": "app_income_confirm",
            "phase": "application",
        }
    return await _finish_income_saved_ok(
        call_id, session, text, name, pending
    )


def _begin_confirm(
    call_id: str,
    session: Any,
    answer: str,
    name: str,
    income_type: str,
    values: dict[str, Any],
) -> dict[str, Any]:
    from .app_stage import _app_ok, _set_pending_question

    session.filled.pop("app_income_edit_field", None)
    summary = _confirm_income_speech(name, income_type, values)
    _set_pending_confirm(session, income_type, values, summary)
    _set_pending_question(session, "app_income_confirm", summary)
    return _app_ok(
        call_id,
        session,
        answer,
        "app_income_confirm",
        "Confirm income before save",
        spoken_override=summary,
    )


async def _apply_confirm_field_edit(
    page: Page,
    income_type: str,
    field_key: str,
    spoken_value: str,
) -> tuple[bool, str, str]:
    """Write one edited value onto the open income modal.

    Returns (ok, store_value, error_speech).
    """
    typ = (income_type or "").strip()
    text = (spoken_value or "").strip()
    if field_key == "frequency":
        freq = map_employed_pay_frequency(text)
        if not freq:
            return (
                False,
                "",
                "Please say Weekly, Bi-weekly, Monthly, or Annually for pay frequency.",
            )
        await _select_employed_pay_frequency(page, freq)
        # Always keep the mapped value; confirm/save retries the UI if needed.
        return True, freq, ""
    if field_key == "hours":
        hours = _normalize_hours(text)
        if not hours:
            return False, "", "Please say the hours per week as a number, like 20 or 40."
        ok = await _fill_employed_field(page, "hours", hours)
        if not ok:
            return False, "", "I couldn't enter the hours on the form. How many hours per week?"
        return True, hours, ""
    if field_key == "employer":
        name = _normalize_employer_name(text)
        if not name:
            return False, "", "What is the employer name?"
        ok = await _fill_employed_field(page, "employer", name)
        if not ok:
            return False, "", "I couldn't enter the employer name. What is the employer name?"
        return True, name, ""
    if field_key == "work":
        work = _normalize_employer_name(text)
        if not work:
            return False, "", "What type of work is it?"
        ok = await _fill_self_employed_field(page, "work", work)
        if not ok:
            return False, "", "I couldn't enter the type of work. What type of work is it?"
        return True, work, ""
    if field_key == "compensation":
        from .live_form import _is_no, _is_yes

        if _is_yes(text):
            yn = "Yes"
        elif _is_no(text):
            yn = "No"
        else:
            return False, "", "Do you receive unemployment compensation? Please say Yes or No."
        ok = await _click_unemployment_compensation_yn(page, yn)
        if not ok:
            return (
                False,
                "",
                "I couldn't set unemployment compensation. Please say Yes or No.",
            )
        return True, yn, ""
    if field_key in ("pay", "amount"):
        amount = _normalize_amount(text)
        if not amount:
            return (
                False,
                "",
                "Please say the amount as a number, like 400 or 3200.",
            )
        if typ == "Employed" and field_key == "pay":
            ok = await _fill_employed_field(page, "pay", amount)
        elif typ == "Self-employed" and field_key == "pay":
            ok = await _fill_self_employed_field(page, "pay", amount)
        else:
            ok = await _fill_income_amount(page, amount)
        if not ok:
            return False, "", "I couldn't enter that amount on the form. What is the amount?"
        return True, amount, ""
    return False, "", "I couldn't tell which detail to change."


async def _click_add_income(page: Page, member_name: str) -> bool:
    """Click Add Income on the named member card (not a random first match)."""
    try:
        ok = await page.evaluate(
            "(memberName) => {"
            + _income_dom_js()
            + """
              const card = incFindCardEl(memberName);
              if (!card) return false;
              const buttons = [...card.querySelectorAll(
                'button, [role=button], a'
              )].filter(incVisible).filter(incIsAddIncome);
              if (!buttons.length) return false;
              buttons[0].click();
              return true;
            }""",
            member_name,
        )
        if ok:
            await page.wait_for_timeout(400)
        return bool(ok)
    except Exception:
        logger.exception("click_add_income failed member=%r", member_name)
        return False


async def _click_add_another_income(page: Page, member_name: str) -> dict[str, Any]:
    """Click Add Another Income on this member's card and verify type modal opens."""
    try:
        clicked = await page.evaluate(
            "(memberName) => {"
            + _income_dom_js()
            + " return incClickAddAnotherIncome(memberName); }",
            member_name,
        )
    except Exception:
        logger.exception(
            "click_add_another_income evaluate failed member=%r", member_name
        )
        return {"ok": False, "reason": "evaluate_failed", "member": member_name}

    if not isinstance(clicked, dict) or not clicked.get("ok"):
        logger.warning(
            "click_add_another_income miss member=%r result=%r",
            member_name,
            clicked,
        )
        return clicked if isinstance(clicked, dict) else {
            "ok": False,
            "reason": "click_failed",
            "member": member_name,
        }

    await page.wait_for_timeout(350)
    opened = await _income_type_palette_open(page)
    if not opened:
        # Retry once with Playwright force click on Add Another Income.
        try:
            btn = page.get_by_role(
                "button", name=re.compile(r"Add\s+Another\s+Income", re.I)
            )
            if await btn.count():
                await btn.last.scroll_into_view_if_needed(timeout=2000)
                await btn.last.click(timeout=2500, force=True)
                await page.wait_for_timeout(400)
                opened = await _income_type_palette_open(page)
        except Exception:
            logger.exception(
                "click_add_another_income playwright retry failed member=%r",
                member_name,
            )

    logger.info(
        "click_add_another_income member=%r clicked=%s modal_open=%s label=%r",
        member_name,
        True,
        opened,
        (clicked or {}).get("label"),
    )
    if not opened:
        return {
            "ok": False,
            "reason": "type_modal_not_opened",
            "member": member_name,
            "clicked": True,
            "label": (clicked or {}).get("label"),
        }
    return {
        "ok": True,
        "member": member_name,
        "modal_open": True,
        "label": (clicked or {}).get("label"),
    }


async def _click_income_type(page: Page, label: str) -> bool:
    """Click a Select Income Type option (scrolls modal list into view first)."""
    want = (label or "").strip()
    if not want:
        return False
    try:
        result = await page.evaluate(
            "(label) => {" + _income_dom_js() + " return incClickType(label); }",
            want,
        )
        if isinstance(result, dict) and result.get("ok"):
            await page.wait_for_timeout(220)
            logger.info(
                "click_income_type ok label=%r via=%s",
                want,
                result.get("via") or "dom",
            )
            return True
        logger.info(
            "click_income_type dom miss label=%r result=%r trying playwright",
            want,
            result,
        )
        # Short chips (SSA / SSDI / None / Alimony) — Playwright force-click.
        pattern = re.compile(rf"^\s*{re.escape(want)}\s*$", re.I)
        try:
            for role_name in ("button", "option", "radio", "menuitem"):
                role = page.get_by_role(role_name, name=pattern)
                if await role.count():
                    try:
                        await role.first.scroll_into_view_if_needed(timeout=800)
                        await role.first.click(timeout=1500, force=True)
                        await page.wait_for_timeout(220)
                        logger.info(
                            "click_income_type ok label=%r via=playwright_role_%s",
                            want,
                            role_name,
                        )
                        return True
                    except Exception:
                        pass
            loc = page.get_by_text(pattern)
            count = await loc.count()
            for i in range(min(count, 8)):
                node = loc.nth(i)
                try:
                    await node.scroll_into_view_if_needed(timeout=800)
                    await node.click(timeout=1500, force=True)
                    await page.wait_for_timeout(220)
                    logger.info(
                        "click_income_type ok label=%r via=playwright_text",
                        want,
                    )
                    return True
                except Exception:
                    continue
        except Exception:
            pass
        logger.warning("click_income_type failed label=%r result=%r", want, result)
        return False
    except Exception:
        logger.exception("click_income_type failed label=%r", want)
        return False


async def _income_type_select_state(page: Page, label: str) -> dict[str, Any]:
    """Read whether the type palette is still open after a type click."""
    try:
        raw = await page.evaluate(
            "(label) => {"
            + _income_dom_js()
            + " return incTypeSelectResult(label); }",
            label,
        )
        return raw if isinstance(raw, dict) else {}
    except Exception:
        logger.exception("income_type_select_state failed label=%r", label)
        return {}


def _type_select_looks_committed(
    income_type: str, state: dict[str, Any]
) -> bool:
    """True when UI left the type palette for the expected next surface."""
    if not isinstance(state, dict):
        return False
    still = bool(state.get("still_on_type"))
    kind = str(state.get("modal_kind") or "")
    amount_open = bool(state.get("amount_open")) or kind == "amount"
    typ = (income_type or "").strip()
    if typ in _INCOME_AMOUNT_ONLY_TYPES:
        # Must reach Amount per month — staying on Select Income Type is failure.
        return amount_open and not still
    if typ == "None":
        # None usually closes the modal back to cards.
        return (not still) and not amount_open
    # Employed / Self-employed / Unemployed leave the type list for detail fields.
    return not still


async def _verify_income_type_selected(
    page: Page, income_type: str
) -> bool:
    """Poll briefly until the type palette is gone / amount modal is up."""
    last: dict[str, Any] = {}
    # Amount-only chips (SSA/SSDI) need a bit longer for the Amount modal.
    typ = (income_type or "").strip()
    attempts = 10 if typ in _INCOME_AMOUNT_ONLY_TYPES else 5
    for _ in range(attempts):
        last = await _income_type_select_state(page, income_type)
        if _type_select_looks_committed(income_type, last):
            logger.info(
                "income_type_selected_ok type=%r state=%r",
                income_type,
                last,
            )
            return True
        await page.wait_for_timeout(140)
    logger.warning(
        "income_type_selected_failed type=%r last=%r",
        income_type,
        last,
    )
    return False


async def _type_income_amount_via_keyboard(page: Page, value: str) -> bool:
    """Type Amount per month with real key events so React enables Save."""
    typed = _amount_digits(value) or str(value or "").strip()
    if not typed:
        return False
    try:
        focused = await page.evaluate(
            "() => {" + _income_dom_js() + " return incFocusAmount(); }"
        )
        if not (isinstance(focused, dict) and focused.get("ok")):
            return False
        loc = page.locator("[data-pc-income-amt='1']").first
        try:
            if await loc.count():
                await loc.click(timeout=2000)
        except Exception:
            pass
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.type(typed, delay=35)
        await page.wait_for_timeout(80)
        try:
            await page.evaluate(
                "() => {" + _income_dom_js() + " return incBlurAmount(); }"
            )
        except Exception:
            try:
                await page.keyboard.press("Tab")
            except Exception:
                pass
        await page.wait_for_timeout(150)
        got = await _read_income_amount(page)
        if _amounts_match(got, typed):
            logger.info(
                "income_amount_typed_ok want=%r got=%r",
                typed,
                got,
            )
            return True
        logger.warning(
            "income_amount_typed_mismatch want=%r got=%r",
            typed,
            got,
        )
        return False
    except Exception:
        logger.exception("type_income_amount_via_keyboard failed value=%r", value)
        return False


async def _fill_income_amount(page: Page, value: str) -> bool:
    """Fill Amount per month so React/Pointcare enables Save (override support)."""
    want = str(value or "").strip()
    if not want:
        return False
    # Prefer real keystrokes — native setter paints the field but leaves Save grey.
    if await _type_income_amount_via_keyboard(page, want):
        return True
    try:
        result = await page.evaluate(
            "(value) => {" + _income_dom_js() + " return incFillAmount(value); }",
            _amount_digits(want) or want,
        )
        if isinstance(result, dict):
            ok = bool(result.get("ok"))
            if ok and not _amounts_match(str(result.get("value") or ""), want):
                logger.warning(
                    "fill_income_amount value mismatch want=%r got=%r",
                    want,
                    result.get("value"),
                )
                return False
            if ok:
                # One more keyboard pass if Save is still disabled.
                state = await _income_save_button_state(page)
                if state.get("found") and not state.get("enabled"):
                    return await _type_income_amount_via_keyboard(page, want)
            return ok
        return bool(result)
    except Exception:
        logger.exception("fill_income_amount failed")
        return False


async def _read_income_amount(page: Page) -> str:
    """Read the current Amount per month field value from the income modal."""
    try:
        result = await page.evaluate(
            "() => {" + _income_dom_js() + " return incReadAmount(); }"
        )
        if isinstance(result, dict):
            return str(result.get("value") or "").strip()
        return ""
    except Exception:
        logger.exception("read_income_amount failed")
        return ""


async def _read_labeled_income_field(page: Page, needle: str) -> str:
    """Read the current value of a labeled modal input."""
    try:
        raw = await page.evaluate(
            "(needle) => {"
            + _income_dom_js()
            + " return incReadLabeledInput(needle); }",
            needle,
        )
        if isinstance(raw, dict) and raw.get("ok"):
            return str(raw.get("value") or "").strip()
    except Exception:
        logger.exception("read_labeled_income_field failed needle=%s", needle)
    return ""


def _labeled_values_match(want: str, got: str) -> bool:
    """Loose compare for currency/hours typed into RN Web inputs."""
    w = re.sub(r"[^0-9.]", "", str(want or ""))
    g = re.sub(r"[^0-9.]", "", str(got or ""))
    if w and g:
        try:
            return abs(float(w) - float(g)) < 0.01
        except ValueError:
            return w in g or g in w
    return (want or "").strip().lower() == (got or "").strip().lower()


async def _type_labeled_income_field_via_keyboard(
    page: Page, needle: str, value: str
) -> bool:
    """Focus a labeled modal field and type with real key events."""
    typed = str(value or "").strip()
    if not typed or not needle:
        return False
    try:
        focused = await page.evaluate(
            "(needle) => {"
            + _income_dom_js()
            + """
              const target = incFindLabeledInput(needle);
              if (!target) return { ok: false };
              try {
                [...document.querySelectorAll('[data-pc-income-field]')]
                  .forEach((el) => el.removeAttribute('data-pc-income-field'));
              } catch (e) {}
              target.setAttribute('data-pc-income-field', '1');
              try { target.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
              target.focus();
              try { target.select && target.select(); } catch (e) {}
              return { ok: true };
            }""",
            needle,
        )
        if not (isinstance(focused, dict) and focused.get("ok")):
            return False
        loc = page.locator("[data-pc-income-field='1']").first
        try:
            if await loc.count():
                await loc.click(timeout=2000)
        except Exception:
            pass
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.type(typed, delay=30)
        # Commit RN Web state with Tab; label re-read below verifies the target.
        try:
            await page.keyboard.press("Tab")
        except Exception:
            pass
        await page.wait_for_timeout(120)
        got = await _read_labeled_income_field(page, needle)
        if _labeled_values_match(typed, got):
            return True
        # Marked-node fallback (value may lag on the label walk).
        marked = await page.evaluate(
            """() => {
              const el = document.querySelector('[data-pc-income-field=\"1\"]');
              return el ? String(el.value || '').trim() : '';
            }"""
        )
        ok = _labeled_values_match(typed, str(marked or ""))
        if not ok:
            logger.warning(
                "type_labeled_income_field mismatch needle=%r want=%r got=%r marked=%r",
                needle,
                typed,
                got,
                marked,
            )
        return ok
    except Exception:
        logger.exception(
            "type_labeled_income_field_via_keyboard failed needle=%s", needle
        )
        return False


async def _fill_labeled_income_field(
    page: Page, needle: str, value: str
) -> bool:
    """Fill a labeled income modal text field by nearby label text."""
    if await _type_labeled_income_field_via_keyboard(page, needle, value):
        return True
    try:
        ok = await page.evaluate(
            "({ needle, value }) => {"
            + _income_dom_js()
            + " return incFillLabeled(needle, value); }",
            {"needle": needle, "value": value},
        )
        if ok:
            got = await _read_labeled_income_field(page, needle)
            if _labeled_values_match(value, got):
                return True
            logger.warning(
                "fill_labeled_income_field mismatch needle=%r want=%r got=%r",
                needle,
                value,
                got,
            )
        # Never fall back to "next visible input" — that overwrites Monthly gross
        # when Hours / Employer targeting misses.
        return False
    except Exception:
        logger.exception("fill_labeled_income_field failed needle=%s", needle)
        return False


async def _fill_employed_field(page: Page, field_key: str, value: str) -> bool:
    """Fill a labeled Employed modal text field (pay / hours / employer)."""
    needle = _EMPLOYED_FIELD_NEEDLES.get(field_key) or field_key
    return await _fill_labeled_income_field(page, needle, value)


async def _fill_self_employed_field(page: Page, field_key: str, value: str) -> bool:
    """Fill Self-employed modal fields (net income / type of work)."""
    needle = _SELF_FIELD_NEEDLES.get(field_key) or field_key
    return await _fill_labeled_income_field(page, needle, value)


async def _playwright_click_unemployment_yn(page: Page, value: str) -> bool:
    """Click Yes/No in the unemployment radiogroup via Playwright.

    Live Pointcare uses Radix radiogroup + RN Pressable. A real Playwright
    force-click on the Yes/No text is what commits RHF — synthetic DOM/React
    clicks often leave the form stuck on the default No.
    """
    want = (value or "").strip()
    if want.lower().startswith("y"):
        want = "Yes"
    elif want.lower().startswith("n"):
        want = "No"
    else:
        return False
    pattern = re.compile(rf"^\s*{re.escape(want)}\s*$", re.I)
    try:
        group = page.locator("[role=radiogroup]").filter(
            has_text=re.compile(r"\bYes\b", re.I)
        ).filter(
            has_text=re.compile(r"\bNo\b", re.I)
        ).first
        if await group.count():
            # Proven live path: force-click the Yes/No text node in the group.
            try:
                target = group.get_by_text(pattern).first
                await target.scroll_into_view_if_needed(timeout=800)
                await target.click(timeout=2000, force=True)
                return True
            except Exception:
                pass
            row = group.locator(
                "div[tabindex='0'], div[tabindex=\"0\"], [role=radio]"
            ).filter(has_text=pattern)
            if await row.count():
                try:
                    await row.first.scroll_into_view_if_needed(timeout=800)
                    await row.first.click(timeout=1500, force=True)
                    return True
                except Exception:
                    pass
            text = group.get_by_text(pattern)
            count = await text.count()
            for i in range(min(count, 6)):
                node = text.nth(i)
                try:
                    if not await node.is_visible():
                        continue
                    await node.scroll_into_view_if_needed(timeout=800)
                    await node.click(timeout=1500, force=True)
                    return True
                except Exception:
                    continue
        # Last resort: page-wide exact Yes/No near unemployment copy.
        near = page.locator("div, section, form").filter(
            has_text=re.compile(r"unemployment\s+compensation", re.I)
        ).filter(has_text=pattern).first
        if await near.count():
            target = near.get_by_text(pattern).last
            try:
                await target.click(timeout=1500, force=True)
                return True
            except Exception:
                pass
    except Exception:
        logger.exception(
            "playwright_click_unemployment_yn failed value=%r", value
        )
    return False


async def _unemployment_yn_selected(page: Page, value: str) -> bool:
    """True when RHF/UI reflects the spoken Yes/No for unemployment compensation."""
    want_raw = (value or "").strip().lower()
    if want_raw.startswith("y"):
        want = "yes"
    elif want_raw.startswith("n"):
        want = "no"
    else:
        return False
    try:
        state = await page.evaluate(
            "(want) => {"
            + _income_dom_js()
            + " return {"
            "  ok: incUnemploymentYnSelected(want),"
            "  formValue: incUnemploymentFormValue(),"
            "  amount: /amount per month/i.test(document.body.innerText || ''),"
            "}; }",
            want,
        )
        return isinstance(state, dict) and bool(state.get("ok"))
    except Exception:
        return False


async def _prime_unemployed_no_saveable(page: Page) -> bool:
    """Make Save enable when No is already the form default.

    Pointcare defaults unemploymentCompensationStatus to
    NOT_UNEMPLOYMENT_COMPENSATED. Selecting No again leaves isDirty=false and
    Save disabled, so confirm→Save would hard-fail. Shift the default so No is
    dirty/valid and the yellow Save CTA lights up.
    """
    try:
        result = await page.evaluate(
            "() => {"
            + _income_dom_js()
            + " return incPrimeUnemployedNoSaveable(); }"
        )
        await page.wait_for_timeout(200)
        ok = isinstance(result, dict) and bool(result.get("ok"))
        logger.info("unemployment_no_prime result=%r", result)
        return ok
    except Exception:
        logger.exception("unemployment_no_prime failed")
        return False


async def _click_unemployment_compensation_yn(page: Page, value: str) -> bool:
    """Click Yes/No for 'Do you receive unemployment compensation?'."""
    want = (value or "").strip()
    if not want:
        return False
    want_l = want.lower()
    is_yes = want_l.startswith("y")
    is_no = want_l.startswith("n")
    if not (is_yes or is_no):
        return False
    try:
        # Playwright first — real pointer events are required for Radix/RHF.
        clicked = await _playwright_click_unemployment_yn(page, want)
        via = "playwright" if clicked else ""
        if clicked:
            await page.wait_for_timeout(350)
        if not await _unemployment_yn_selected(page, want):
            result = await page.evaluate(
                "(value) => {"
                + _income_dom_js()
                + " return incClickUnemploymentYn(value); }",
                want,
            )
            await page.wait_for_timeout(300)
            if isinstance(result, dict) and (
                result.get("ok") or result.get("verified")
            ):
                clicked = True
                via = str(result.get("via") or "js")
            elif not clicked:
                logger.warning(
                    "click_unemployment_compensation_yn failed value=%r "
                    "playwright=%r js=%r",
                    want,
                    via or False,
                    result,
                )
                return False
        if not await _unemployment_yn_selected(page, want):
            # No is the default — aria/form may already match before any click.
            if is_no and await page.evaluate(
                "() => {"
                + _income_dom_js()
                + " return incUnemploymentFormValue() === "
                "'NOT_UNEMPLOYMENT_COMPENSATED'; }"
            ):
                clicked = True
                via = via or "default_no"
            else:
                logger.warning(
                    "click_unemployment_compensation_yn unverified value=%r via=%r",
                    want,
                    via,
                )
                return False
        if is_no:
            primed = await _prime_unemployed_no_saveable(page)
            if not primed:
                logger.warning(
                    "unemployment_no_prime_failed value=%r via=%r", want, via
                )
        logger.info(
            "unemployment_yn_ok value=%r via=%r",
            want,
            via or "verified",
        )
        return True
    except Exception:
        logger.exception(
            "click_unemployment_compensation_yn failed value=%r", want
        )
        return False


_PAY_FREQ_VALUE_RE = re.compile(
    r"^(Weekly|Bi-weekly|Biweekly|Monthly|Annually|Annual)$", re.I
)
_PAY_FREQ_LABEL_RE = re.compile(r"Pay\s*frequency", re.I)


def _pay_freq_aliases(label: str) -> list[str]:
    want = (label or "").strip()
    if not want:
        return []
    aliases = [want]
    if want == "Bi-weekly":
        aliases.extend(["Biweekly", "Bi weekly"])
    elif want == "Annually":
        aliases.extend(["Annual", "Yearly"])
    elif want == "Monthly":
        aliases.append("Month")
    elif want == "Weekly":
        aliases.append("Week")
    return aliases


async def _employed_detail_modal_open(page: Page) -> bool:
    """True when the Employed detail editor (not the Work & Income list) is up."""
    try:
        kind = await page.evaluate(
            "() => {" + _income_dom_js() + " return incModalOpen(); }"
        )
        if kind in ("open", "amount"):
            return True
        # Fallback: pay-frequency / employer cues mean the editor is still open.
        body = await page.evaluate(
            "() => (document.body && document.body.innerText || '').toLowerCase()"
        )
        text = str(body or "")
        if "progress:" in text and "add income" in text and "pay frequency" not in text:
            return False
        return "pay frequency" in text or (
            "employer name" in text and "hours per week" in text
        )
    except Exception:
        return False


async def _reopen_employed_after_modal_loss(
    page: Page,
    session: Any,
    values: dict[str, Any],
    freq: str,
) -> bool:
    """Re-open Employed editor after a bad click dismissed it to Work & Income."""
    name = str(session.filled.get("app_income_current") or "").strip()
    if not name:
        return False
    try:
        logger.warning(
            "pay_frequency_reopen_employed member=%r freq=%r values=%r",
            name,
            freq,
            {k: values.get(k) for k in ("pay", "hours")},
        )
        opened = await _open_type_and_select(page, name, "Employed")
        if not (isinstance(opened, dict) and opened.get("ok")):
            logger.warning(
                "pay_frequency_reopen_type_failed member=%r result=%r",
                name,
                opened,
            )
            return False
        await page.wait_for_timeout(250)
        pay = str(values.get("pay") or "").strip()
        hours = str(values.get("hours") or "").strip()
        if pay:
            await _fill_employed_field(page, "pay", pay)
        if hours:
            await _fill_employed_field(page, "hours", hours)
        if not await _select_employed_pay_frequency(page, freq):
            # Leave modal open for the caller to ask frequency again.
            return await _employed_detail_modal_open(page)
        return await _employed_detail_modal_open(page)
    except Exception:
        logger.exception(
            "pay_frequency_reopen_employed failed member=%r freq=%r", name, freq
        )
        return False


async def _playwright_open_pay_frequency(page: Page) -> bool:
    """Open the Pay frequency control via Playwright near the field label.

    Only click the value chip / combobox — never the label parent or Escape,
    which dismisses the whole Pointcare income modal back to Work & Income.
    """
    try:
        # Blur any focused input (hours/pay) so the dropdown click isn't eaten.
        try:
            await page.evaluate(
                "() => { const e = document.activeElement;"
                " if (e && e.blur) e.blur(); }"
            )
        except Exception:
            pass
        label = page.get_by_text(_PAY_FREQ_LABEL_RE).first
        if await label.count():
            try:
                await label.scroll_into_view_if_needed(timeout=800)
            except Exception:
                pass
            # Walk up a few ancestors only — keep the box tight so we don't
            # hit Cancel / overlay controls that close the modal.
            for depth in range(1, 5):
                box = label.locator(
                    "xpath=ancestor::*[self::div or self::label or "
                    f"self::section or self::li][{depth}]"
                )
                try:
                    if not await box.count():
                        continue
                    chip = box.get_by_text(_PAY_FREQ_VALUE_RE)
                    if await chip.count():
                        await chip.first.click(timeout=1500)
                        return True
                    combo = box.locator(
                        "[role=combobox], [aria-haspopup=listbox], "
                        "[aria-haspopup=menu], select, button"
                    )
                    if await combo.count():
                        await combo.first.click(timeout=1500)
                        return True
                except Exception:
                    continue
        # Prefer a frequency value chip near the open modal, not page-wide.
        chip = page.get_by_text(_PAY_FREQ_VALUE_RE).first
        if await chip.count() and await _employed_detail_modal_open(page):
            await chip.click(timeout=1500)
            return True
        combo = page.locator(
            "[role=combobox], [aria-haspopup=listbox], select"
        ).first
        if await combo.count() and await _employed_detail_modal_open(page):
            await combo.click(timeout=1500)
            return True
    except Exception:
        logger.exception("playwright_open_pay_frequency failed")
    return False


async def _pay_frequency_menu_open(page: Page) -> bool:
    """True when multiple frequency options are visible (menu expanded)."""
    try:
        visible = 0
        for name in _EMPLOYED_PAY_FREQUENCY_LABELS:
            loc = page.get_by_text(name, exact=True)
            count = await loc.count()
            for i in range(min(count, 3)):
                try:
                    if await loc.nth(i).is_visible():
                        visible += 1
                        break
                except Exception:
                    continue
        return visible >= 2
    except Exception:
        return False


async def _playwright_click_pay_frequency_option(page: Page, label: str) -> bool:
    """Click an open pay-frequency menu option (or native select)."""
    want = (label or "").strip()
    if not want:
        return False
    aliases = _pay_freq_aliases(want)
    try:
        native = page.locator("select").first
        if await native.count():
            for name in aliases:
                try:
                    await native.select_option(label=name)
                    return True
                except Exception:
                    try:
                        await native.select_option(value=name)
                        return True
                    except Exception:
                        continue
        for name in aliases:
            pattern = re.compile(rf"^\s*{re.escape(name)}\s*$", re.I)
            for role_name in ("option", "menuitem"):
                option = page.get_by_role(role_name, name=pattern)
                if await option.count():
                    try:
                        await option.first.click(timeout=1500)
                        return True
                    except Exception:
                        pass
            # Wait briefly for the target option text to appear, then click.
            exact = page.get_by_text(pattern)
            try:
                await exact.first.wait_for(state="visible", timeout=500)
            except Exception:
                pass
            count = await exact.count()
            # Prefer later nodes — open menus often append options after the chip.
            indices = list(range(count - 1, -1, -1))[:8]
            for i in indices:
                node = exact.nth(i)
                try:
                    if not await node.is_visible():
                        continue
                    role = (await node.get_attribute("role") or "").lower()
                    tag = await node.evaluate(
                        "el => (el.tagName || '').toLowerCase()"
                    )
                    if role in ("option", "menuitem", "button") or tag in (
                        "li",
                        "button",
                        "div",
                        "span",
                        "p",
                        "a",
                    ):
                        await node.click(timeout=1500)
                        return True
                except Exception:
                    continue
    except Exception:
        logger.exception(
            "playwright_click_pay_frequency_option failed label=%r", label
        )
    return False


async def _keyboard_select_pay_frequency(page: Page, label: str) -> bool:
    """Type the first letter after opening — works for many custom selects."""
    want = (label or "").strip()
    if not want:
        return False
    key = want[0]
    try:
        await page.keyboard.type(key, delay=40)
        await page.wait_for_timeout(80)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(120)
        got = await _read_employed_pay_frequency(page)
        if _pay_freq_equal(want, got):
            return True
        # Arrow down through options once if first letter landed wrong.
        await page.keyboard.press("ArrowDown")
        await page.wait_for_timeout(60)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(120)
        got = await _read_employed_pay_frequency(page)
        return _pay_freq_equal(want, got)
    except Exception:
        logger.warning(
            "keyboard_select_pay_frequency failed label=%r", want, exc_info=True
        )
        return False


def _pay_freq_equal(left: str, right: str) -> bool:
    a = re.sub(r"[\s\-]+", "", (left or "").strip().lower())
    b = re.sub(r"[\s\-]+", "", (right or "").strip().lower())
    if not a or not b:
        return False
    if a == b:
        return True
    aliases = {
        "biweekly": {"biweekly", "byweekly", "fortnightly", "bweekly"},
        "weekly": {"weekly", "week"},
        "monthly": {"monthly", "month"},
        "annually": {"annually", "annual", "yearly", "year"},
    }
    for _canon, group in aliases.items():
        if a in group and b in group:
            return True
    return False


async def _read_employed_pay_frequency(page: Page) -> str:
    try:
        raw = await page.evaluate(
            "() => {" + _income_dom_js() + " return incReadPayFrequency(); }"
        )
        if isinstance(raw, dict) and raw.get("ok"):
            return str(raw.get("value") or "").strip()
    except Exception:
        logger.exception("read_employed_pay_frequency failed")
    return ""


async def _select_employed_pay_frequency(page: Page, label: str) -> bool:
    """Select Weekly / Bi-weekly / Monthly / Annually in the Pay frequency dropdown.

    Never press Escape — Pointcare treats it as Cancel and returns to the
    Work & Income list. Success requires the detail modal to still be open.
    """
    want = (label or "").strip()
    if not want:
        return False

    try:
        if not await _employed_detail_modal_open(page):
            logger.warning("pay_frequency_no_modal want=%r", want)
            return False

        current = await _read_employed_pay_frequency(page)
        if _pay_freq_equal(want, current):
            logger.info("pay_frequency_already_set want=%r", want)
            return True

        async def _still_ok(got: str) -> bool:
            if not await _employed_detail_modal_open(page):
                logger.warning(
                    "pay_frequency_modal_closed want=%r got=%r", want, got
                )
                return False
            if _pay_freq_equal(want, got):
                return True
            return False

        # Fast path: open+click in one DOM pass.
        try:
            oneshot = await page.evaluate(
                "(lab) => {"
                + _income_dom_js()
                + " return incSelectPayFrequency(lab); }",
                want,
            )
            if isinstance(oneshot, dict) and oneshot.get("ok"):
                await page.wait_for_timeout(150)
                got = await _read_employed_pay_frequency(page)
                if await _still_ok(got):
                    logger.info(
                        "pay_frequency_oneshot_ok want=%r got=%r", want, got
                    )
                    return True
                # Chip can lag a beat while the modal stays open.
                if await _employed_detail_modal_open(page):
                    await page.wait_for_timeout(150)
                    got = await _read_employed_pay_frequency(page)
                    if await _still_ok(got):
                        logger.info(
                            "pay_frequency_oneshot_lag_ok want=%r got=%r",
                            want,
                            got,
                        )
                        return True
        except Exception:
            logger.warning(
                "pay_frequency_oneshot_failed want=%r", want, exc_info=True
            )

        async def _open_menu() -> bool:
            if not await _employed_detail_modal_open(page):
                return False
            if await _playwright_open_pay_frequency(page):
                return True
            opened = await page.evaluate(
                "() => {" + _income_dom_js() + " return incOpenPayFrequency(); }"
            )
            return isinstance(opened, dict) and bool(opened.get("ok"))

        async def _click_option() -> bool:
            if await _playwright_click_pay_frequency_option(page, want):
                return True
            result = await page.evaluate(
                "(lab) => {"
                + _income_dom_js()
                + " return incClickPayFrequencyOption(lab); }",
                want,
            )
            return isinstance(result, dict) and bool(result.get("ok"))

        async def _attempt() -> bool:
            if not await _open_menu():
                logger.warning("pay_frequency_open_failed want=%r", want)
                return False
            for _ in range(6):
                if await _pay_frequency_menu_open(page):
                    break
                await page.wait_for_timeout(70)
            else:
                await page.wait_for_timeout(100)
            clicked = await _click_option()
            if not clicked:
                if await _keyboard_select_pay_frequency(page, want):
                    got = await _read_employed_pay_frequency(page)
                    if await _still_ok(got):
                        logger.info(
                            "pay_frequency_keyboard_ok want=%r got=%r",
                            want,
                            got,
                        )
                        return True
                logger.warning("pay_frequency_option_click_failed want=%r", want)
                return False
            # Do NOT press Escape — that Cancels the Pointcare income modal.
            await page.wait_for_timeout(150)
            got = await _read_employed_pay_frequency(page)
            if await _still_ok(got):
                return True
            if await _employed_detail_modal_open(page):
                # Option clicked, modal still open, chip may lag — accept once.
                await page.wait_for_timeout(120)
                got2 = await _read_employed_pay_frequency(page)
                if await _still_ok(got2):
                    return True
                if not got2 and not got:
                    # Still open but unread — treat as soft UI set only if
                    # the menu closed (value committed).
                    if not await _pay_frequency_menu_open(page):
                        logger.info(
                            "pay_frequency_click_kept_modal want=%r", want
                        )
                        return True
            logger.warning(
                "pay_frequency_unverified want=%r got=%r", want, got
            )
            return False

        if await _attempt():
            return True
        if await _employed_detail_modal_open(page) and await _attempt():
            return True

        logger.warning("select_employed_pay_frequency failed label=%r", want)
        return False
    except Exception:
        logger.exception("select_employed_pay_frequency failed label=%r", want)
        return False


async def _income_card_shows_entry(
    page: Page,
    member_name: str,
    income_type: str,
    amount: str = "",
) -> dict[str, Any]:
    """One-shot scrape: is this income type (and amount) on the member card?"""
    try:
        raw = await page.evaluate(
            "({ memberName, incomeType, amountHint }) => {"
            + _income_dom_js()
            + " return incIncomeAfterSave(memberName, incomeType, amountHint); }",
            {
                "memberName": member_name or "",
                "incomeType": str(income_type or "").strip(),
                "amountHint": amount or "",
            },
        )
        return raw if isinstance(raw, dict) else {}
    except Exception:
        logger.exception(
            "income_card_shows_entry failed member=%r type=%r",
            member_name,
            income_type,
        )
        return {}


def _income_save_state_ok(state: dict[str, Any], *, want_type: str, amount: str) -> bool:
    """True when the member card shows the saved type (and amount when given).

    Never treat an open income editor as already saved — Add Another Income is
    still visible on the card behind the modal, which previously skipped Save.
    """
    if not isinstance(state, dict):
        return False
    type_ok = bool(state.get("type_on_card")) if want_type else True
    if not type_ok:
        return False
    want_amt = str(amount or "").strip()
    if want_amt and not bool(state.get("amount_on_card")):
        return False
    # modal_open mirrors editorOpen in the DOM helper — still editing.
    if bool(state.get("modal_open")):
        return False
    return True


def _pending_verify_amount(values: dict[str, Any]) -> str:
    """Amount used for post-save card verify (Employed uses pay, not amount)."""
    return str(
        (values or {}).get("amount")
        or (values or {}).get("pay")
        or ""
    ).strip()


def _normalize_employer_name(spoken: str) -> str:
    """Strip ASR trailing sentence punctuation from employer / work names."""
    text = re.sub(r"\s+", " ", (spoken or "").strip())
    # Drop trailing . ! ? … but keep internal dots (e.g. "A.B.C. Corp").
    text = re.sub(r"[.!?…]+$", "", text).strip()
    return text


async def _verify_income_saved(
    page: Page,
    member_name: str,
    income_type: str,
    amount: str = "",
) -> bool:
    """Poll until the member card shows the saved income (modal closed preferred)."""
    want_type = str(income_type or "").strip()
    last: dict[str, Any] = {}
    # Pointcare can take a few seconds to close the editor and paint the card.
    for _ in range(20):
        last = await _income_card_shows_entry(
            page, member_name, want_type, amount
        )
        if _income_save_state_ok(last, want_type=want_type, amount=amount):
            logger.info(
                "income_save_verified member=%r type=%r amount_on_card=%s state=%r",
                member_name,
                want_type,
                last.get("amount_on_card"),
                last,
            )
            return True
        await page.wait_for_timeout(300)
    logger.warning(
        "income_save_verify_failed member=%r type=%r amount=%r last=%r",
        member_name,
        want_type,
        amount,
        last,
    )
    return False


async def _playwright_click_income_save(page: Page) -> bool:
    """Click the marked / role Save control with Playwright (RN Web friendly)."""
    try:
        state = await page.evaluate(
            "() => {" + _income_dom_js() + " return incMarkModalSave(); }"
        )
    except Exception:
        state = {}
    # Prefer the element we marked in the income modal.
    try:
        loc = page.locator("[data-pc-income-save='1']").first
        if await loc.count():
            await loc.scroll_into_view_if_needed(timeout=2000)
            try:
                await loc.click(timeout=2500)
                return True
            except Exception:
                try:
                    await loc.click(timeout=2500, force=True)
                    return True
                except Exception:
                    pass
    except Exception:
        logger.exception("playwright marked save click failed")
    # Role / text fallbacks inside dialogs.
    for factory in (
        lambda: page.get_by_role("button", name=re.compile(r"^\s*Save\s*$", re.I)),
        lambda: page.get_by_text(re.compile(r"^\s*Save\s*$", re.I)),
    ):
        try:
            loc = factory().last
            if await loc.count():
                await loc.click(timeout=2500, force=True)
                return True
        except Exception:
            continue
    # Coordinate click from measured center when available.
    if isinstance(state, dict) and state.get("x") and state.get("y"):
        try:
            await page.mouse.click(int(state["x"]), int(state["y"]))
            return True
        except Exception:
            logger.exception("income_save_mouse_click failed")
    return False


async def _click_income_save(
    page: Page,
    *,
    member_name: str = "",
    income_type: str = "",
    amount: str = "",
) -> bool:
    """Click modal Save; when member/type given, require the card to show it."""
    from .live_form import _click_cta

    state = await _income_save_button_state(page)
    truly_disabled = (
        state.get("found")
        and not state.get("enabled")
        and not state.get("active_look")
    )
    if truly_disabled and amount:
        logger.info(
            "income_save_disabled_before_click; retyping amount=%r",
            amount,
        )
        await _fill_income_amount(page, amount)
        state = await _income_save_button_state(page)
        truly_disabled = (
            state.get("found")
            and not state.get("enabled")
            and not state.get("active_look")
        )
    # Unemployed + No leaves Save disabled until the form is dirtied — prime it.
    if (
        truly_disabled
        and not amount
        and (income_type or "").strip().lower() == "unemployed"
    ):
        await _prime_unemployed_no_saveable(page)
        state = await _income_save_button_state(page)
        truly_disabled = (
            state.get("found")
            and not state.get("enabled")
            and not state.get("active_look")
        )
    # Hard-stop only when DOM disabled AND it does not look like the yellow CTA.
    if truly_disabled and not amount:
        logger.warning(
            "income_save_still_disabled member=%r type=%r state=%r",
            member_name,
            income_type,
            state,
        )
        return False

    clicked = False
    click_reason = ""
    # Playwright click first — el.click() often misses RN Web Pressables.
    if await _playwright_click_income_save(page):
        clicked = True
        logger.info(
            "income_save_clicked_via_playwright member=%r type=%r",
            member_name,
            income_type,
        )
    if not clicked:
        try:
            result = await page.evaluate(
                "() => {" + _income_dom_js() + " return incClickModalButton('Save'); }"
            )
            if isinstance(result, dict):
                clicked = bool(result.get("ok"))
                click_reason = str(result.get("reason") or "")
            else:
                clicked = bool(result)
        except Exception:
            logger.exception("click_income_save evaluate failed")
            clicked = False
    if not clicked and click_reason != "disabled":
        try:
            await _click_cta(page, "Save", attempts=2)
            clicked = True
        except Exception:
            clicked = False
    if not clicked:
        return False
    if not (member_name or income_type):
        await page.wait_for_timeout(350)
        return True
    return await _verify_income_saved(
        page, member_name, income_type, amount
    )


async def _click_income_cancel(page: Page) -> bool:
    try:
        ok = await page.evaluate(
            "() => {"
            + _income_dom_js()
            + """
              const cancel = incClickModalButton('Cancel');
              if (cancel && cancel.ok) return true;
              const close = incClickModalButton('Close');
              if (close && close.ok) return true;
              // X close in modal header
              const root = incFindIncomeModalRoot() || document.body;
              const nodes = [...root.querySelectorAll(
                'button, [role=button], a'
              )].filter(incVisible);
              for (const el of nodes) {
                const t = (el.getAttribute('aria-label') || '').toLowerCase();
                if (t === 'close' || t === 'dismiss') {
                  el.click();
                  return true;
                }
              }
              return false;
            }"""
        )
        if ok:
            await page.wait_for_timeout(250)
        return bool(ok)
    except Exception:
        logger.exception("click_income_cancel failed")
        return False


async def _start_detailed_seq(
    session: Any, kind: str
) -> tuple[str, str]:
    seq = _INCOME_EMPLOYED_SEQ if kind == "employed" else _INCOME_SELF_SEQ
    _key, first_step, first_q = seq[0]
    _set_income_seq(
        session,
        {
            "kind": kind,
            "index": 0,
            "values": {},
            "keys": [item[0] for item in seq],
        },
    )
    _set_income_phase(session, kind)
    income_type = "Employed" if kind == "employed" else "Self-employed"
    member = str(session.filled.get("app_income_current") or "").strip()
    return first_step, _with_document_notice(
        first_q,
        income_type,
        session=session,
        member_name=member,
    )


async def _open_edit_or_add(
    page: Page,
    member_name: str,
    income_type: str,
    *,
    edit_mode: bool,
) -> dict[str, Any]:
    if edit_mode:
        opened = await _click_income_entry_action(
            page, member_name, income_type, "edit"
        )
        if opened.get("ok"):
            # Edit may land directly on the detail modal (type already chosen).
            return {"ok": True, "mode": "edit", "via": opened.get("via")}
        # Fall through to add/override if edit control wasn't found.
        # (Delete must never call this helper — use _delete_income_entry.)
    return await _open_type_and_select(page, member_name, income_type)


async def _begin_income_member(
    session: Any, page: Page, card: dict[str, Any], *, intro: bool = False
) -> str:
    name = str(card.get("name") or "").strip()
    index = int(card.get("index") or 0)
    session.filled["app_income_current"] = name
    session.filled["app_income_current_index"] = str(index)
    session.filled.pop("app_income_pending_type", None)
    session.filled.pop("app_income_seq", None)
    session.filled.pop("app_income_pending_summary", None)
    session.filled.pop("app_income_pending_values", None)
    session.filled.pop("app_income_edit_mode", None)
    session.filled.pop("app_income_delete_type", None)
    session.filled.pop("app_income_manage_action", None)
    session.filled.pop("app_income_add_opened", None)
    _set_income_phase(session, "type")
    existing = [
        str(e.get("type") or "").strip()
        for e in _card_entries(card)
        if str(e.get("type") or "").strip()
    ]
    if not existing:
        existing = _member_existing_types(session, name)
    elif name:
        # Prefer live DOM entries when present.
        mapping = _member_types_map(session)
        mapping[name] = existing
        _set_member_types_map(session, mapping)
    # Open the type picker now so the type answer can click the label directly.
    opened = await _click_add_income(page, name)
    if not opened:
        from .live_form import _click_cta

        try:
            await _click_cta(page, "Add Another Income", attempts=1)
            opened = True
        except Exception:
            try:
                await _click_cta(page, "Add Income", attempts=1)
                opened = True
            except Exception:
                opened = False
    if opened:
        session.filled["app_income_add_opened"] = "1"
        logger.info(
            "income_add_opened member=%r index=%s", name, index
        )
    else:
        logger.warning(
            "income_add_click_failed member=%r index=%s", name, index
        )
    return _type_ask_for_session(
        session, name, intro=intro, existing=existing
    )


async def _advance_after_income_member(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    step: str,
    cards: list[dict[str, Any]],
    index: int,
) -> dict[str, Any]:
    from .app_stage import _app_ok, _set_pending_question

    _mark_income_heard(session, index)
    _clear_income_member_state(session)
    heard = set(_income_heard(session))
    nxt = -1
    for card in cards:
        try:
            idx = int(card.get("index"))
        except (TypeError, ValueError):
            continue
        if idx not in heard:
            nxt = idx
            break
    if nxt >= 0 and nxt < len(cards):
        speech = await _begin_income_member(session, page, cards[nxt], intro=False)
        _set_pending_question(session, "app_income", speech)
        return _app_ok(
            call_id,
            session,
            text,
            "app_income",
            "income next member",
            spoken_override=speech,
        )
    return await _finish_income_review_next(call_id, session, page, text, step)


async def _finish_income_review_next(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    step: str,
) -> dict[str, Any]:
    from .app_stage import (
        _advance_after_answer,
        _app_ok,
        _click_stage_next,
        _prepare_resume_speech,
        _set_pending_question,
        _wait_for_screen_change,
        app_question_for_step,
    )

    session.filled["app_income_review_ready"] = "1"
    _set_income_phase(session, "final")
    clicked = await _click_stage_next(page)
    if not clicked:
        speech = (
            "I've finished work and income for everyone, but Next is not "
            "available yet. Say continue to try again."
        )
        _set_pending_question(session, "app_income", speech)
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "income_next_disabled",
            "step": "app_income",
            "phase": "application",
        }
    next_step = await _advance_after_answer(
        page, session, allow_extra_next=False, leave_step="app_income"
    )
    if not next_step or next_step == "app_income":
        next_step = await _wait_for_screen_change(page, "app_income", timeout_s=3.5)
    if not next_step or next_step.startswith("app_income"):
        speech = (
            "I've finished work and income, but the income screen is still open. "
            "Say continue to try Next again."
        )
        _set_pending_question(session, "app_income", speech)
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "income_next_stayed",
            "step": "app_income",
            "phase": "application",
        }
    _clear_all_income_state(session)
    try:
        next_step, spoken = await _prepare_resume_speech(session, page, next_step)
    except Exception:
        spoken = app_question_for_step(next_step, session)
    lead = "I've finished work and income for everyone. "
    return _app_ok(
        call_id,
        session,
        text,
        next_step,
        "income review advanced",
        spoken_override=lead + (spoken or ""),
    )


async def prepare_income_speech(session: Any, page: Page) -> tuple[str, str]:
    """Entry / resume speech for Work & Income."""
    from .app_stage import _set_pending_question, app_question_for_step

    cards = await scrape_income_cards(page)
    current = str(session.filled.get("app_income_current") or "").strip()
    phase = _income_phase(session)
    pending_type = str(session.filled.get("app_income_pending_type") or "").strip()

    if current and phase in _INCOME_PHASES and phase not in ("final",):
        if phase == "type":
            # Resume mid-flow — do not re-read the full type list.
            speech = _type_ask_for_session(
                session,
                current,
                existing=_member_existing_types(session, current),
            )
        elif phase == "amount":
            speech = _amount_ask_speech(
                current, pending_type or "that income", session=session
            )
        elif phase == "more":
            speech = _more_ask_speech(
                current, _member_existing_types(session, current)
            )
            _set_pending_question(session, "app_income_more", speech)
            return "app_income_more", speech
        elif phase == "confirm":
            speech = str(
                session.filled.get("app_income_pending_summary") or ""
            ).strip() or _confirm_income_speech(
                current, pending_type or "that income", _pending_income_values(session)
            )
            _set_pending_question(session, "app_income_confirm", speech)
            return "app_income_confirm", speech
        elif phase == "confirm_edit_pick":
            speech = _confirm_edit_pick_speech(
                current,
                pending_type or "that income",
                _pending_income_values(session),
            )
            _set_pending_question(session, "app_income_confirm", speech)
            return "app_income_confirm", speech
        elif phase == "confirm_edit_value":
            field_key = str(session.filled.get("app_income_edit_field") or "").strip()
            speech = _confirm_edit_value_ask(field_key, pending_type or "")
            _set_pending_question(session, "app_income_confirm", speech)
            return "app_income_confirm", speech
        elif phase == "delete_confirm":
            del_type = str(session.filled.get("app_income_delete_type") or "").strip()
            speech = _delete_confirm_speech(current, del_type or "that")
            _set_pending_question(session, "app_income_confirm", speech)
            return "app_income_confirm", speech
        elif phase in ("employed", "self"):
            state = _income_seq_state(session)
            kind = str(state.get("kind") or phase)
            seq = _INCOME_EMPLOYED_SEQ if kind == "employed" else _INCOME_SELF_SEQ
            index = int(state.get("index") or 0)
            if 0 <= index < len(seq):
                step_id = seq[index][1]
                speech = seq[index][2]
                if index == 0:
                    speech = _with_document_notice(
                        speech,
                        "Employed" if kind == "employed" else "Self-employed",
                        session=session,
                        member_name=current,
                    )
                _set_pending_question(session, step_id, speech)
                return step_id, speech
            speech = _more_ask_speech(
                current, _member_existing_types(session, current)
            )
            _set_pending_question(session, "app_income_more", speech)
            return "app_income_more", speech
        elif phase == "unemployed":
            speech = _unemployed_compensation_ask_speech(
                current, session=session
            )
            _set_pending_question(session, "app_income_unemployed", speech)
            return "app_income_unemployed", speech
        else:
            speech = app_question_for_step("app_income", session)
        _set_pending_question(session, "app_income", speech)
        return "app_income", speech

    session.filled["app_income_heard"] = "[]"
    session.filled["app_income_walk_index"] = "0"
    session.filled.pop("app_income_review_ready", None)
    if not cards:
        cards = await scrape_income_cards_with_retry(page)
    if not cards:
        speech = (
            "I'm on the work and income screen, but I can't see the household "
            "member cards yet. Please stay on this page and say try again."
        )
        logger.warning("prepare_income_speech: no income cards discovered")
        _set_pending_question(session, "app_income", speech)
        return "app_income", speech
    speech = await _begin_income_member(session, page, cards[0], intro=True)
    _set_pending_question(session, "app_income", speech)
    return "app_income", speech


async def continue_income_seq(
    call_id: str,
    session: Any,
    page: Page,
    answer: str,
) -> dict[str, Any]:
    """Store one employed/self field, then ask the next or confirm before save."""
    from .app_stage import _app_ok, _fill_next_visible_input

    state = _income_seq_state(session)
    kind = str(state.get("kind") or "")
    seq = _INCOME_EMPLOYED_SEQ if kind == "employed" else _INCOME_SELF_SEQ
    index = int(state.get("index") or 0)
    values = dict(state.get("values") or {})
    name = str(session.filled.get("app_income_current") or "").strip() or "this person"
    if index < 0 or index >= len(seq):
        _set_income_seq(session, None)
        _set_income_phase(session, "more")
        speech = _more_ask_speech(name, _member_existing_types(session, name))
        return _app_ok(
            call_id,
            session,
            answer,
            "app_income_more",
            "Income sequence reset",
            spoken_override=speech,
        )
    key, step_id, _q = seq[index]
    text = (answer or "").strip()
    if not text:
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + _q,
            "error": "income_field_empty",
            "step": session.app_step or step_id,
            "phase": "application",
        }

    store_value = text
    filled = False
    if kind == "employed" and key == "frequency":
        freq = map_employed_pay_frequency(text)
        if not freq:
            speech = (
                "Please say Weekly, Bi-weekly, Monthly, or Annually for pay frequency."
            )
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "income_frequency_clarify",
                "step": session.app_step or step_id,
                "phase": "application",
            }
        store_value = freq
        filled = await _select_employed_pay_frequency(page, freq)
        modal_ok = await _employed_detail_modal_open(page)
        if filled and modal_ok:
            session.filled.pop("app_income_freq_needs_ui", None)
        elif modal_ok:
            # Mapped + editor still open — continue; retry UI before confirm.
            logger.warning(
                "pay_frequency_soft_accept want=%r (UI set failed; continuing)",
                freq,
            )
            session.filled["app_income_freq_needs_ui"] = freq
            filled = True
        else:
            # Escape/bad click dismissed the modal back to Work & Income.
            recovered = await _reopen_employed_after_modal_loss(
                page, session, values, freq
            )
            if recovered:
                session.filled.pop("app_income_freq_needs_ui", None)
                filled = True
            else:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: The income form closed. Opening it again — "
                        "please say the pay frequency once more: "
                        "Weekly, Bi-weekly, Monthly, or Annually."
                    ),
                    "error": "income_frequency_modal_closed",
                    "step": session.app_step or step_id,
                    "phase": "application",
                }
    elif kind == "employed" and key in _EMPLOYED_FIELD_NEEDLES:
        if key == "pay":
            store_value = _normalize_amount(text)
            if not store_value:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: Please say the monthly gross income as a number, "
                        "like 400 or 3200."
                    ),
                    "error": "income_pay_clarify",
                    "step": session.app_step or step_id,
                    "phase": "application",
                }
        elif key == "hours":
            store_value = _normalize_hours(text)
            if not store_value:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: Please say the hours per week as a number, "
                        "like 20 or 40."
                    ),
                    "error": "income_hours_clarify",
                    "step": session.app_step or step_id,
                    "phase": "application",
                }
        elif key == "employer":
            store_value = _normalize_employer_name(text)
            if not store_value:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: What is the employer name?",
                    "error": "income_employer_clarify",
                    "step": session.app_step or step_id,
                    "phase": "application",
                }
        filled = await _fill_employed_field(page, key, store_value)
        if not filled and key in ("pay", "hours", "employer"):
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: I couldn't enter that value on the form. " + _q,
                "error": "income_field_not_filled",
                "step": session.app_step or step_id,
                "phase": "application",
            }
    elif kind == "self" and key in _SELF_FIELD_NEEDLES:
        if key == "pay":
            store_value = _normalize_amount(text)
            if not store_value:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": (
                        "ASK: Please say the estimated monthly net income as a "
                        "number, like 400 or 2500."
                    ),
                    "error": "income_self_pay_clarify",
                    "step": session.app_step or step_id,
                    "phase": "application",
                }
        elif key == "work":
            store_value = _normalize_employer_name(text)
            if not store_value:
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: What type of work is it?",
                    "error": "income_self_work_clarify",
                    "step": session.app_step or step_id,
                    "phase": "application",
                }
        filled = await _fill_self_employed_field(page, key, store_value)
        if not filled:
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: I couldn't enter that value on the form. " + _q,
                "error": "income_field_not_filled",
                "step": session.app_step or step_id,
                "phase": "application",
            }
    else:
        filled = await _fill_next_visible_input(page, text)
        if not filled and key in ("pay", "hours"):
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: I couldn't enter that value on the form. " + _q,
                "error": "income_field_not_filled",
                "step": session.app_step or step_id,
                "phase": "application",
            }

    values[key] = store_value
    next_index = index + 1
    if next_index < len(seq):
        _nkey, nstep, nq = seq[next_index]
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
    # All fields collected — confirm before Save.
    pending = str(session.filled.get("app_income_pending_type") or "").strip()
    if not pending:
        pending = "Employed" if kind == "employed" else "Self-employed"
        session.filled["app_income_pending_type"] = pending
    # Retry a deferred pay-frequency UI set before speaking confirm.
    pending_freq = str(session.filled.get("app_income_freq_needs_ui") or "").strip()
    if pending_freq and kind == "employed":
        ok_freq = await _select_employed_pay_frequency(page, pending_freq)
        if ok_freq:
            session.filled.pop("app_income_freq_needs_ui", None)
            values["frequency"] = pending_freq
        else:
            logger.warning(
                "pay_frequency_still_unset_before_confirm want=%r", pending_freq
            )
    _set_income_seq(
        session,
        {
            "kind": kind,
            "index": next_index,
            "values": values,
            "keys": [item[0] for item in seq],
        },
    )
    return _begin_confirm(call_id, session, answer, name, pending, values)


async def _income_type_palette_open(page: Page) -> bool:
    """True when the Select income type surface is already visible."""
    try:
        raw = await page.evaluate(
            "() => {" + _income_dom_js() + " return incModalOpen(); }"
        )
        if raw == "type":
            return True
        body = await page.evaluate(
            "() => (document.body && document.body.innerText || '').toLowerCase()"
        )
        text = str(body or "")
        return "select income type" in text or (
            "employed" in text and "self-employed" in text and "none" in text
        )
    except Exception:
        return False


async def _open_type_and_select(
    page: Page, member_name: str, income_type: str
) -> dict[str, Any]:
    logger.info(
        "open_type_and_select start member=%r type=%r",
        member_name,
        income_type,
    )
    # Member start may have already clicked Add Income / Add Another Income.
    opened = await _income_type_palette_open(page)
    if not opened:
        opened = await _click_add_income(page, member_name)
        if not opened:
            # Fallback: any Add Income CTA (single-member screens).
            # Live Pointcare often labels this "Add Another Income".
            from .live_form import _click_cta

            try:
                await _click_cta(page, "Add Another Income", attempts=1)
                opened = True
            except Exception:
                try:
                    await _click_cta(page, "Add Income", attempts=1)
                    opened = True
                except Exception:
                    opened = False
        if opened:
            await page.wait_for_timeout(350)
    if not opened:
        return {"ok": False, "reason": "add_income_failed"}

    # Long labels (Retirement / Property rental / Worker's comp) sit below the
    # fold — try click, verify UI advanced, retry once, then guarded fallbacks.
    committed = False
    for attempt in range(2):
        clicked = await _click_income_type(page, income_type)
        if clicked and await _verify_income_type_selected(page, income_type):
            committed = True
            break
        if attempt == 0:
            await page.wait_for_timeout(250)

    if not committed:
        from .live_form import _click_cta, _click_exact_choice

        for fallback in ("exact", "cta"):
            try:
                if fallback == "exact":
                    await _click_exact_choice(page, income_type)
                else:
                    await _click_cta(page, income_type, attempts=2)
            except Exception:
                continue
            if await _verify_income_type_selected(page, income_type):
                committed = True
                break
            logger.warning(
                "income_type_fallback_unverified type=%r path=%s",
                income_type,
                fallback,
            )

    if not committed:
        # Leave the type palette open so the caller can re-ask; only cancel when
        # Add somehow left a stuck half-state without a type list.
        still_palette = await _income_type_palette_open(page)
        if not still_palette:
            await _click_income_cancel(page)
        return {"ok": False, "reason": "type_select_failed"}
    return {"ok": True, "add_opened": True}


async def handle_income_step(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    lowered: str,
    step: str,
) -> dict[str, Any]:
    """Router for app_income and amount/more substeps (not employed field seq)."""
    from .app_stage import _app_ok, _set_pending_question
    from .live_form import _is_no, _is_yes

    cards = await scrape_income_cards(page)
    phase = _income_phase(session)
    current = str(session.filled.get("app_income_current") or "").strip()
    index_raw = str(session.filled.get("app_income_current_index") or "").strip()
    index = int(index_raw) if index_raw.isdigit() else 0

    # Resume / final Next retry.
    if phase == "final" or session.filled.get("app_income_review_ready"):
        if _is_yes(text) or re.search(
            r"\b(continue|next|done|looks? (good|correct|fine))\b", lowered
        ):
            return await _finish_income_review_next(
                call_id, session, page, text, step
            )

    # Bootstrap when we somehow arrived without a current member.
    if not current or phase == "type" and not current:
        if not cards:
            cards = await scrape_income_cards_with_retry(page)
        if not cards:
            speech = (
                "I'm on the work and income screen, but I can't see the household "
                "member cards yet. Please stay on this page and say try again."
            )
            _set_pending_question(session, "app_income", speech)
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "income_cards_missing",
                "step": "app_income",
                "phase": "application",
            }
        if not current:
            speech = await _begin_income_member(
                session, page, cards[0], intro=True
            )
            # Fall through only if this utterance was already a type.
            current = str(session.filled.get("app_income_current") or "").strip()
            index = int(session.filled.get("app_income_current_index") or 0)
            picked_early = map_income_type(text)
            if not picked_early:
                _set_pending_question(session, "app_income", speech)
                return _app_ok(
                    call_id,
                    session,
                    text,
                    "app_income",
                    "income begin member",
                    spoken_override=speech,
                )
            phase = "type"
            _set_income_phase(session, "type")

    # ---- Unemployed: compensation Yes/No ----
    if step == "app_income_unemployed" or phase == "unemployed":
        if _is_yes(text):
            clicked = await _click_unemployment_compensation_yn(page, "Yes")
            if not clicked:
                speech = (
                    "I couldn't select Yes for unemployment compensation. "
                    + _unemployed_compensation_ask_speech(
                        current, session=session
                    )
                )
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + speech,
                    "error": "income_unemployed_yes_failed",
                    "step": "app_income_unemployed",
                    "phase": "application",
                }
            _set_income_phase(session, "amount")
            speech = _unemployed_amount_ask_speech(current)
            _set_pending_question(session, "app_income_amount", speech)
            return _app_ok(
                call_id,
                session,
                text,
                "app_income_amount",
                "Unemployed compensation yes",
                spoken_override=speech,
            )
        if _is_no(text):
            clicked = await _click_unemployment_compensation_yn(page, "No")
            if not clicked:
                speech = (
                    "I couldn't select No for unemployment compensation. "
                    + _unemployed_compensation_ask_speech(
                        current, session=session
                    )
                )
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + speech,
                    "error": "income_unemployed_no_failed",
                    "step": "app_income_unemployed",
                    "phase": "application",
                }
            session.filled["app_income_pending_type"] = "Unemployed"
            return _begin_confirm(
                call_id,
                session,
                text,
                current,
                "Unemployed",
                {"compensation": "No"},
            )
        speech = _unemployed_compensation_ask_speech(
            current, session=session
        )
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "income_unemployed_clarify",
            "step": "app_income_unemployed",
            "phase": "application",
        }

    # ---- Confirm before Save / delete confirm / confirm-edit ----
    if step == "app_income_confirm" or phase in (
        "confirm",
        "confirm_edit_pick",
        "confirm_edit_value",
        "delete_confirm",
    ):
        # Prefer explicit phase; also honor pending delete type if phase drifted.
        del_type = str(session.filled.get("app_income_delete_type") or "").strip()
        pending_for_save = str(
            session.filled.get("app_income_pending_type") or ""
        ).strip()
        if phase == "delete_confirm" or (del_type and not pending_for_save):
            if _is_yes(text):
                deleted = await _delete_income_entry(page, current, del_type)
                if not deleted:
                    speech = (
                        f"I couldn't delete {del_type or 'that income'} for "
                        f"{current or 'this person'}. Please say delete again with "
                        "the income type, or say cancel."
                    )
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": "ASK: " + speech,
                        "error": "income_delete_failed",
                        "step": "app_income_confirm",
                        "phase": "application",
                    }
                # Drop from session tracking.
                if del_type and current:
                    mapping = _member_types_map(session)
                    mapping[current] = [
                        t for t in (mapping.get(current) or []) if t != del_type
                    ]
                    _set_member_types_map(session, mapping)
                    logger.info(
                        "income_delete_tracking_updated member=%r remaining=%r",
                        current,
                        mapping.get(current),
                    )
                session.filled.pop("app_income_delete_type", None)
                session.filled.pop("app_income_manage_action", None)
                _set_income_phase(session, "more")
                speech = _more_ask_speech(
                    current, _member_existing_types(session, current)
                )
                _set_pending_question(session, "app_income_more", speech)
                return _app_ok(
                    call_id,
                    session,
                    text,
                    "app_income_more",
                    "Deleted income entry",
                    spoken_override=speech,
                )
            if _is_no(text):
                session.filled.pop("app_income_delete_type", None)
                session.filled.pop("app_income_manage_action", None)
                _set_income_phase(session, "more")
                speech = _more_ask_speech(
                    current, _member_existing_types(session, current)
                )
                _set_pending_question(session, "app_income_more", speech)
                return _app_ok(
                    call_id,
                    session,
                    text,
                    "app_income_more",
                    "Delete cancelled",
                    spoken_override=speech,
                )
            speech = _delete_confirm_speech(current, del_type or "that")
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "income_delete_clarify",
                "step": "app_income_confirm",
                "phase": "application",
            }

        pending = str(session.filled.get("app_income_pending_type") or "").strip()
        values = _pending_income_values(session)

        async def _cancel_confirm_entry() -> dict[str, Any]:
            await _click_income_cancel(page)
            for key in (
                "app_income_pending_summary",
                "app_income_pending_values",
                "app_income_seq",
                "app_income_edit_mode",
                "app_income_edit_field",
            ):
                session.filled.pop(key, None)
            session.filled.pop("app_income_pending_type", None)
            _set_income_phase(session, "type")
            speech = (
                "Okay, I cancelled that income entry. "
                + _type_ask_for_session(
                    session,
                    current,
                    existing=_member_existing_types(session, current),
                )
            )
            _set_pending_question(session, "app_income", speech)
            return _app_ok(
                call_id,
                session,
                text,
                "app_income",
                "Income confirm cancelled",
                spoken_override=speech,
            )

        # ---- Which field to edit? ----
        if phase == "confirm_edit_pick":
            if _wants_confirm_cancel(text):
                return await _cancel_confirm_entry()
            field_key = _map_confirm_edit_field(text, pending, values)
            if not field_key:
                speech = _confirm_edit_pick_speech(current, pending, values)
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + speech,
                    "error": "income_confirm_edit_clarify",
                    "step": "app_income_confirm",
                    "phase": "application",
                }
            session.filled["app_income_edit_field"] = field_key
            _set_income_phase(session, "confirm_edit_value")
            speech = _confirm_edit_value_ask(field_key, pending)
            _set_pending_question(session, "app_income_confirm", speech)
            return _app_ok(
                call_id,
                session,
                text,
                "app_income_confirm",
                "Income confirm edit field picked",
                spoken_override=speech,
            )

        # ---- New value for the chosen field ----
        if phase == "confirm_edit_value":
            if _wants_confirm_cancel(text):
                return await _cancel_confirm_entry()
            field_key = str(session.filled.get("app_income_edit_field") or "").strip()
            if not field_key:
                _set_income_phase(session, "confirm_edit_pick")
                speech = _confirm_edit_pick_speech(current, pending, values)
                _set_pending_question(session, "app_income_confirm", speech)
                return _app_ok(
                    call_id,
                    session,
                    text,
                    "app_income_confirm",
                    "Income confirm edit field missing",
                    spoken_override=speech,
                )
            ok, store_value, err = await _apply_confirm_field_edit(
                page, pending, field_key, text
            )
            if not ok:
                speech = err or _confirm_edit_value_ask(field_key, pending)
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + speech,
                    "error": "income_confirm_edit_value_failed",
                    "step": "app_income_confirm",
                    "phase": "application",
                }
            values[field_key] = store_value
            # Unemployed: No compensation clears amount.
            if field_key == "compensation" and store_value == "No":
                values.pop("amount", None)
            session.filled.pop("app_income_edit_field", None)
            # Keep employed/self seq values in sync when present.
            seq_state = _income_seq_state(session)
            if seq_state:
                seq_vals = dict(seq_state.get("values") or {})
                seq_vals[field_key] = store_value
                seq_state["values"] = seq_vals
                _set_income_seq(session, seq_state)
            return _begin_confirm(
                call_id, session, text, current, pending or "that income", values
            )

        if _is_yes(text) and not _wants_confirm_edit(text):
            return await _save_confirmed_income(
                call_id, session, page, text, current
            )
        # No / edit / "not correct" → ask which field (do not cancel the modal).
        if _is_no(text) or _wants_confirm_edit(text):
            _set_income_phase(session, "confirm_edit_pick")
            session.filled.pop("app_income_edit_field", None)
            speech = _confirm_edit_pick_speech(current, pending, values)
            _set_pending_question(session, "app_income_confirm", speech)
            return _app_ok(
                call_id,
                session,
                text,
                "app_income_confirm",
                "Income confirm edit pick",
                spoken_override=speech,
            )
        if _wants_confirm_cancel(text):
            return await _cancel_confirm_entry()
        # Caller named a field directly while on confirm ("change the hours").
        direct_field = _map_confirm_edit_field(text, pending, values)
        if direct_field and _wants_confirm_edit(text):
            session.filled["app_income_edit_field"] = direct_field
            _set_income_phase(session, "confirm_edit_value")
            speech = _confirm_edit_value_ask(direct_field, pending)
            _set_pending_question(session, "app_income_confirm", speech)
            return _app_ok(
                call_id,
                session,
                text,
                "app_income_confirm",
                "Income confirm edit field from confirm",
                spoken_override=speech,
            )
        summary = str(session.filled.get("app_income_pending_summary") or "").strip()
        speech = summary or _confirm_income_speech(
            current,
            pending or "that income",
            values,
        )
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "income_confirm_clarify",
            "step": "app_income_confirm",
            "phase": "application",
        }

    # ---- Amount per month (benefit types + Unemployed Yes) ----
    if step == "app_income_amount" or phase == "amount":
        amount = _normalize_amount(text)
        pending = str(session.filled.get("app_income_pending_type") or "").strip()
        if not amount:
            speech = (
                _unemployed_amount_ask_speech(current)
                if pending == "Unemployed"
                else _amount_ask_speech(
                    current, pending or "that income", session=session
                )
            )
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "income_amount_clarify",
                "step": "app_income_amount",
                "phase": "application",
            }
        filled = await _fill_income_amount(page, amount)
        if not filled:
            speech = (
                "I couldn't enter that amount on the form. "
                + (
                    _unemployed_amount_ask_speech(current)
                    if pending == "Unemployed"
                    else _amount_ask_speech(
                        current, pending or "that income", session=session
                    )
                )
            )
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "income_amount_not_filled",
                "step": "app_income_amount",
                "phase": "application",
            }
        values: dict[str, Any] = {"amount": amount}
        if pending == "Unemployed":
            values["compensation"] = "Yes"
        return _begin_confirm(
            call_id,
            session,
            text,
            current,
            pending or "Other",
            values,
        )

    async def _apply_picked_income(picked: str, *, edit_mode: bool = False):
        existing = _member_types_map(session).get(current or "") or []
        override = (picked in existing and picked != "None") or edit_mode
        session.filled["app_income_edit_mode"] = "1" if edit_mode else "0"
        opened = await _open_edit_or_add(
            page, current, picked, edit_mode=edit_mode
        )
        if not opened.get("ok"):
            reason = str(opened.get("reason") or "")
            if reason == "type_select_failed":
                speech = (
                    f"I couldn't select {picked} on the form for "
                    f"{current or 'this person'}. Please say the income type again."
                )
                err = "income_type_select_failed"
            else:
                speech = (
                    f"I couldn't open {picked} for {current or 'this person'}. "
                    "Please say the income type again, or say edit followed by the type."
                )
                err = "income_open_failed"
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": err,
                "step": "app_income",
                "phase": "application",
            }

        session.filled.pop("app_income_add_opened", None)
        session.filled["app_income_pending_type"] = picked

        if picked == "None":
            _record_member_type(session, current, picked)
            session.filled.pop("app_income_pending_type", None)
            session.filled.pop("app_income_edit_mode", None)
            refreshed = await scrape_income_cards(page)
            use_cards = refreshed or cards
            return await _advance_after_income_member(
                call_id, session, page, text, "app_income", use_cards, index
            )

        if picked in _INCOME_AMOUNT_ONLY_TYPES:
            _set_income_phase(session, "amount")
            speech = _amount_ask_speech(
                current, picked, override=override, session=session
            )
            _set_pending_question(session, "app_income_amount", speech)
            return _app_ok(
                call_id,
                session,
                text,
                "app_income_amount",
                "Ask income amount",
                spoken_override=speech,
            )

        if picked == "Employed":
            next_step, spoken = await _start_detailed_seq(session, "employed")
            if override:
                spoken = (
                    f"Replacing the previous Employed entry for {current}. "
                    + spoken
                )
            _set_pending_question(session, next_step, spoken)
            return _app_ok(
                call_id,
                session,
                text,
                next_step,
                "Start employed income",
                spoken_override=spoken,
            )
        if picked == "Self-employed":
            next_step, spoken = await _start_detailed_seq(session, "self")
            if override:
                spoken = (
                    f"Replacing the previous Self-employed entry for {current}. "
                    + spoken
                )
            _set_pending_question(session, next_step, spoken)
            return _app_ok(
                call_id,
                session,
                text,
                next_step,
                "Start self-employed income",
                spoken_override=spoken,
            )

        _set_income_phase(session, "unemployed")
        speech = _unemployed_compensation_ask_speech(
            current, session=session
        )
        if override:
            speech = (
                f"Replacing the previous Unemployed entry for {current}. "
                + speech
            )
        _set_pending_question(session, "app_income_unemployed", speech)
        return _app_ok(
            call_id,
            session,
            text,
            "app_income_unemployed",
            "Start unemployed income",
            spoken_override=speech,
        )

    def _begin_delete_confirm(income_type: str) -> dict[str, Any]:
        session.filled["app_income_manage_action"] = "delete"
        session.filled["app_income_delete_type"] = income_type
        # Clear save-confirm leftovers so Yes routes to delete, not Save.
        for key in (
            "app_income_pending_type",
            "app_income_pending_values",
            "app_income_pending_summary",
            "app_income_edit_mode",
            "app_income_edit_field",
        ):
            session.filled.pop(key, None)
        _set_income_phase(session, "delete_confirm")
        speech = _delete_confirm_speech(current, income_type)
        _set_pending_question(session, "app_income_confirm", speech)
        logger.info(
            "income_delete_confirm member=%r type=%r spoken=%r",
            current,
            income_type,
            text,
        )
        return _app_ok(
            call_id,
            session,
            text,
            "app_income_confirm",
            "Confirm income delete",
            spoken_override=speech,
        )

    async def _handle_edit_delete() -> dict[str, Any] | None:
        intent = _parse_edit_delete_intent(text)
        if not intent:
            return None
        action, income_type = intent
        session.filled["app_income_manage_action"] = action
        if not income_type:
            speech = (
                f"Which income should I {action} for {current or 'this person'}? "
                "Say the income type, like Employed or SSA."
            )
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "income_manage_clarify",
                "step": step if step.startswith("app_income") else "app_income",
                "phase": "application",
            }
        if action == "delete":
            # Confirm first — never open Add Another Income for delete.
            return _begin_delete_confirm(income_type)
        session.filled.pop("app_income_manage_action", None)
        return await _apply_picked_income(income_type, edit_mode=True)

    # ---- Another income for this person? ----
    if step == "app_income_more" or phase == "more":
        managed = await _handle_edit_delete()
        if managed is not None:
            return managed
        # Follow-up after "Which income should I delete?" — type only, no
        # delete keyword. Must NOT open Add Another Income.
        pending_manage = str(
            session.filled.get("app_income_manage_action") or ""
        ).strip()
        if pending_manage == "delete":
            picked_del = map_income_type(text)
            if picked_del and picked_del != "None":
                return _begin_delete_confirm(picked_del)
            speech = (
                f"Which income should I delete for {current or 'this person'}? "
                "Say the income type, like SSA or Retirement income."
            )
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "income_delete_type_clarify",
                "step": "app_income_more",
                "phase": "application",
            }
        # "Yes, SSA" / "Workers' compensation" / bare type → start immediately.
        # Strip "yes / another income" filler first so "another" never becomes Other.
        residue = _strip_another_income_filler(text)
        picked_more = map_income_type(residue) if residue else None
        if not picked_more:
            picked_more = map_income_type(text)
            # Guard: bare "another income" must not resolve to Other.
            if picked_more == "Other" and re.search(r"\banother\b", lowered):
                picked_more = None
        wants_another = bool(
            _is_yes(text) or re.search(r"\banother(\s+income)?\b", lowered)
        )
        only_yes_or_another = bool(
            wants_another
            and not picked_more
            and not re.search(
                r"\b(employed|self|unemploy|ssa|ssdi|pension|alimony|"
                r"retirement|rental|worker|none)\b",
                lowered,
            )
        )
        if picked_more and not only_yes_or_another:
            # Hard gate: delete manage action must never reach Add Another.
            if str(session.filled.get("app_income_manage_action") or "") == "delete":
                return _begin_delete_confirm(picked_more)
            # Caller named a type — still open Add Another Income first.
            opened = await _click_add_another_income(page, current)
            if not opened.get("ok"):
                # Fall through to _apply_picked_income which opens add/type itself.
                logger.warning(
                    "income_more_type_without_add_btn member=%r type=%r opened=%r",
                    current,
                    picked_more,
                    opened,
                )
            else:
                session.filled["app_income_add_opened"] = "1"
            return await _apply_picked_income(picked_more, edit_mode=False)
        if wants_another or only_yes_or_another:
            opened = await _click_add_another_income(page, current)
            if not opened.get("ok"):
                speech = (
                    f"I couldn't click Add Another Income for "
                    f"{current or 'this person'}. "
                    "Please tap Add Another Income on their card, then say the "
                    "income type, or say try again."
                )
                logger.warning(
                    "income_more_add_another_failed member=%r result=%r",
                    current,
                    opened,
                )
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + speech,
                    "error": "income_add_another_failed",
                    "step": "app_income_more",
                    "phase": "application",
                }
            session.filled["app_income_add_opened"] = "1"
            session.filled.pop("app_income_pending_type", None)
            _set_income_phase(session, "type")
            # Options were already spoken on the first type ask — don't repeat.
            speech = (
                f"Okay, adding another income for {current or 'this person'}. "
                "What income type should I enter?"
            )
            _set_pending_question(session, "app_income", speech)
            return _app_ok(
                call_id,
                session,
                text,
                "app_income",
                "income add another",
                spoken_override=speech,
            )
        if (
            _is_no(text)
            or re.search(r"\b(continue|next|done|everyone|finished)\b", lowered)
        ):
            refreshed = await scrape_income_cards(page)
            if refreshed:
                cards = refreshed
            return await _advance_after_income_member(
                call_id, session, page, text, "app_income", cards, index
            )
        speech = _more_ask_speech(
            current, _member_existing_types(session, current)
        )
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "income_more_clarify",
            "step": "app_income_more",
            "phase": "application",
        }

    # ---- Type selection (default app_income) ----
    managed = await _handle_edit_delete()
    if managed is not None:
        return managed

    if re.search(r"\b(continue|next|done|everyone|finished)\b", lowered) and not map_income_type(
        text
    ):
        heard = set(_income_heard(session))
        if current and index not in heard:
            types = _member_types_map(session).get(current) or []
            if types:
                return await _advance_after_income_member(
                    call_id, session, page, text, "app_income", cards, index
                )
        if cards and len(heard) >= len(cards):
            return await _finish_income_review_next(
                call_id, session, page, text, "app_income"
            )

    # Pending delete on type step (same hard gate as more).
    if str(session.filled.get("app_income_manage_action") or "") == "delete":
        picked_del = map_income_type(text)
        if picked_del and picked_del != "None":
            return _begin_delete_confirm(picked_del)

    # Prefer residue after stripping yes/another filler ("Yes — SSA").
    residue = _strip_another_income_filler(text)
    picked = map_income_type(residue) if residue else None
    if not picked:
        picked = map_income_type(text)
    logger.info(
        "income_type_map raw=%r residue=%r picked=%r member=%r",
        text,
        residue,
        picked,
        current,
    )
    if not picked:
        # Clarify without re-listing every income type.
        if _looks_like_ambiguous_social_security(text):
            speech = (
                f"Did you mean S S A or S S D I for {current or 'this person'}? "
                "You can also say Social Security Administration, or "
                "Social Security Disability Insurance."
            )
            err = "income_ssa_ssdi_clarify"
        elif _looks_like_truncated_ssa_ssdi(text):
            speech = (
                f"I didn't catch that for {current or 'this person'}. "
                "Please say Social Security Administration for S S A, or "
                "Social Security Disability Insurance for S S D I."
            )
            err = "income_clarify"
        else:
            speech = (
                f"I didn't catch an income type for {current or 'this person'}. "
                "Please say the income type — for example S S A or S S D I — "
                "or ask me for the options."
            )
            err = "income_clarify"
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": err,
            "step": "app_income",
            "phase": "application",
        }

    return await _apply_picked_income(picked, edit_mode=False)
