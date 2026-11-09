"""Pointcare Household Expenses (Q11) per-member Collect Mode workflow.

Mirrors income_stage patterns without pay-frequency dropdowns:
walk each household card, Add Expense, select type, collect amount (or Medical
Yes/No), confirm, Save. Supports multiple expenses, edit/delete via pencil,
same-type override, and once-per-member document notices.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from playwright.async_api import Page, expect

from .income_stage import _normalize_amount

logger = logging.getLogger(__name__)

# Exact Pointcare Select an Expense labels (screenshot order).
_EXPENSE_TYPE_LABELS = (
    "Student Loans",
    "Tuition Fees",
    "Alimony Paid",
    "Moving Expenses",
    "IRA Contributions",
    "Childcare",
    "Medical Expenses",
    "Other",
    "None",
)

_EXPENSE_AMOUNT_ONLY_TYPES = frozenset(
    {
        "Student Loans",
        "Tuition Fees",
        "Alimony Paid",
        "Moving Expenses",
        "IRA Contributions",
        "Childcare",
        "Other",
    }
)

_EXPENSE_FILLED_KEYS = (
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
)

_EXPENSE_TYPE_OPTIONS_SPEECH = (
    "You can say Student Loans, Tuition Fees, Alimony Paid, Moving Expenses, "
    "IRA Contributions, Childcare, Medical Expenses, Other, or None."
)

_EXPENSE_PHASES = (
    "type",
    "amount",
    "medical",
    "more",
    "manage",
    "confirm",
    "delete_confirm",
    "final",
)


def _expense_dom_js() -> str:
    """DOM helpers scoped to Household Expenses (Add Expense cards)."""
    return r"""
      const expNorm = (s) => (s || '').replace(/\s+/g, ' ').trim();
      const expLow = (s) => expNorm(s).toLowerCase();
      const expVisible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const st = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' &&
          st.display !== 'none' && st.opacity !== '0';
      };
      const expIsPerson = (line) => {
        const t = expNorm(line).replace(/\s*\(you\)\s*/ig, ' ').trim();
        if (!t || t.length > 60) return false;
        if (/add(?:\s+another|\s+an)?\s+expense|select an expense|amount per month|progress|household expense|next|back|continue|cancel|save|edit|remove|medical expenses in the last/i.test(t)) {
          return false;
        }
        if (/^(yes|no|student loans|tuition fees|alimony paid|moving expenses|ira contributions|childcare|medical expenses|other|none)$/i.test(t)) {
          return false;
        }
        return /^[A-Za-z][A-Za-z .'\-]{0,40}$/.test(t) && t.split(' ').length <= 4;
      };
      const expBtnText = (btn) => expLow(
        btn.innerText || btn.textContent ||
        btn.getAttribute('aria-label') || ''
      );
      const expHasAddExpenseCue = (s) =>
        /\badd(?:\s+another|\s+an)?\s+expense\b/.test(expLow(s || ''));
      const expIsAddExpense = (btn) => {
        const t = expBtnText(btn);
        const aria = expLow(btn.getAttribute('aria-label') || '');
        return expHasAddExpenseCue(t) || expHasAddExpenseCue(aria);
      };
      const expTypeLabels = [
        'Student Loans', 'Tuition Fees', 'Alimony Paid', 'Moving Expenses',
        'IRA Contributions', 'Childcare', 'Medical Expenses', 'Other', 'None',
      ];
      const expMatchType = (line) => {
        const n = expLow(line);
        const labels = [...expTypeLabels].sort(
          (a, b) => expLow(b).length - expLow(a).length
        );
        for (const label of labels) {
          const low = expLow(label);
          if (n === low) return label;
          if (n.startsWith(low + ' ') || n.endsWith(' ' + low)) return label;
          if (n.includes(' ' + low + ' ')) return label;
        }
        return '';
      };
      const expIsEditBtn = (btn) => {
        const t = expBtnText(btn);
        const aria = expLow(btn.getAttribute('aria-label') || '');
        const title = expLow(btn.getAttribute('title') || '');
        if (
          t === 'edit' || t.startsWith('edit ') || t.includes('pencil') ||
          aria.includes('edit') || aria.includes('pencil') ||
          title.includes('edit') || title.includes('pencil')
        ) return true;
        const bare = !t || t.length <= 1;
        if (!bare) return false;
        if (aria.includes('close') || aria.includes('cancel') ||
            aria.includes('dismiss') || aria.includes('delete') ||
            aria.includes('add')) return false;
        if (expIsAddExpense(btn)) return false;
        return Boolean(btn.querySelector('svg, img, [class*="icon" i]'));
      };
      const expIsDeleteBtn = (btn) => {
        const t = expBtnText(btn);
        const aria = expLow(btn.getAttribute('aria-label') || '');
        return t === 'delete' || t === 'remove' || t.startsWith('delete') ||
          t.startsWith('remove') || aria.includes('delete') ||
          aria.includes('remove') || t.includes('trash');
      };
      const expAmountNearText = (blob) => {
        const t = String(blob || '');
        const m = t.match(/\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)/);
        if (m) return m[1].replace(/,/g, '');
        const m2 = t.match(/\b([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*[/]\s*month\b/i);
        if (m2) return m2[1].replace(/,/g, '');
        return '';
      };
      const expEntriesIn = (root) => {
        if (!root) return [];
        const out = [];
        const seen = {};
        const pushEntry = (typ, summary) => {
          if (!typ || typ === 'None') return;
          const key = expLow(typ);
          if (seen[key]) return;
          seen[key] = true;
          let editBtn = null;
          for (const btn of root.querySelectorAll('button, [role=button], a')) {
            if (!expVisible(btn)) continue;
            let cur = btn.parentElement;
            let near = false;
            for (let d = 0; cur && d < 6; d += 1, cur = cur.parentElement) {
              const ct = expLow(cur.innerText || '');
              if (ct.includes(key)) { near = true; break; }
            }
            if (!near) continue;
            if (!editBtn && expIsEditBtn(btn)) editBtn = btn;
          }
          out.push({
            type: typ,
            summary: summary || typ,
            amount: expAmountNearText(summary),
            hasEdit: Boolean(editBtn),
            index: out.length,
          });
        };
        const text = expNorm(root.innerText || '');
        const lines = text.split(/\r?\n/).map(expNorm).filter(Boolean);
        for (let i = 0; i < lines.length; i += 1) {
          const line = lines[i];
          const typ = expMatchType(line);
          if (!typ || typ === 'None') continue;
          const next = lines[i + 1] || '';
          const summary = next && /month|\$|\d/.test(next)
            ? (line + ' ' + next)
            : line;
          pushEntry(typ, summary);
        }
        for (const child of root.querySelectorAll(
          'div, span, p, li, label, h1, h2, h3, h4'
        )) {
          if (!expVisible(child)) continue;
          const t = expNorm(child.innerText || child.textContent || '');
          if (!t || t.length > 60) continue;
          const typ = expMatchType(t);
          if (!typ || typ === 'None') continue;
          if (expLow(t) !== expLow(typ) && !expLow(t).startsWith(expLow(typ))) {
            continue;
          }
          let parentBlob = '';
          let cur = child.parentElement;
          for (let d = 0; cur && d < 4; d += 1, cur = cur.parentElement) {
            parentBlob = expNorm(cur.innerText || '');
            if (parentBlob.length > 20 && parentBlob.length < 400) break;
          }
          pushEntry(typ, parentBlob || t);
        }
        return out;
      };
      const expLinesFromEl = (el) => {
        const raw = el.innerText || '';
        const lines = raw.split(/\r?\n/).map(expNorm).filter(Boolean);
        const seen = {};
        const out = [];
        const push = (s) => {
          const n = expNorm(s);
          if (!n || n.length > 80) return;
          const key = expLow(n);
          if (seen[key]) return;
          seen[key] = 1;
          out.push(n);
        };
        for (const line of lines) push(line);
        for (const child of el.querySelectorAll(
          'div, span, p, li, h1, h2, h3, h4, label'
        )) {
          if (!expVisible(child)) continue;
          const t = expNorm(child.innerText || child.textContent || '');
          if (!t || t.length > 80) continue;
          if ((child.children || []).length > 3 && t.length > 40) continue;
          push(t);
        }
        return out;
      };
      const expNameFromCardText = (text) => {
        const blob = expNorm(text || '');
        if (!blob) return '';
        const m = blob.match(
          /^([A-Za-z][A-Za-z .'\-]{0,40}?)\s+Add(?:\s+Another|\s+An)?\s+Expense\b/i
        );
        if (m && expIsPerson(m[1])) return expNorm(m[1]);
        return '';
      };
      const expAreaPenalty = (area) => {
        if (!area || area < 1) return 0;
        return Math.min(8, Math.floor(Math.log10(area)));
      };
      const expCardFromAddBtn = (btn) => {
        const candidates = [];
        let cur = btn.parentElement;
        for (let depth = 0; cur && depth < 14; depth += 1, cur = cur.parentElement) {
          if (!expVisible(cur)) continue;
          const r = cur.getBoundingClientRect();
          const width = Math.round(r.width);
          const height = Math.round(r.height);
          if (width < 80 || height < 20 || height > 1100 || width > 1400) continue;
          const addsIn = [...cur.querySelectorAll(
            'button, [role=button], a'
          )].filter(expVisible).filter(expIsAddExpense);
          if (addsIn.length !== 1) continue;
          const lines = expLinesFromEl(cur);
          const people = lines.filter(expIsPerson);
          let name = people.length === 1 ? people[0] : '';
          if (!name) name = expNameFromCardText(cur.innerText || '');
          if (!name) continue;
          if (people.length > 1) continue;
          const entries = expEntriesIn(cur);
          const blob = expLow(cur.innerText || '');
          const hasAddAnother = /add\s+another\s+expense/.test(blob);
          const hasMoney = /\$\s*\d|[/]\s*month/.test(blob);
          let score = 0;
          score += Math.min(entries.length, 4) * 40;
          if (hasAddAnother) score += 25;
          if (hasMoney) score += 20;
          if (height >= 70) score += 15;
          if (height >= 120) score += 10;
          if (height > 700) score -= 20;
          score -= expAreaPenalty(width * height);
          candidates.push({
            name, area: width * height, height, el: cur, entries, score,
          });
        }
        if (!candidates.length) return null;
        candidates.sort((a, b) => b.score - a.score || a.area - b.area);
        return candidates[0];
      };
      const expFindCards = () => {
        const buttons = [...document.querySelectorAll(
          'button, [role=button], a'
        )].filter(expVisible).filter(expIsAddExpense);
        const cards = [];
        const seen = {};
        for (const btn of buttons) {
          const card = expCardFromAddBtn(btn);
          if (!card) continue;
          const key = expLow(card.name);
          if (seen[key] !== undefined) continue;
          seen[key] = cards.length;
          cards.push({
            index: cards.length,
            name: card.name,
            hasAddExpense: true,
            entries: card.entries || [],
          });
        }
        return cards;
      };
      const expFindCardEl = (memberName) => {
        const want = expLow(memberName);
        const buttons = [...document.querySelectorAll(
          'button, [role=button], a'
        )].filter(expVisible).filter(expIsAddExpense);
        for (const btn of buttons) {
          const card = expCardFromAddBtn(btn);
          if (!card) continue;
          const n = expLow(card.name);
          if (n === want || n.startsWith(want) || want.startsWith(n)) {
            return card.el;
          }
        }
        return null;
      };
      const expPressEl = (el) => {
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
      const expRowForType = (card, wantType) => {
        const leaves = [...card.querySelectorAll(
          'div, span, p, li, label, h1, h2, h3, h4'
        )].filter(expVisible);
        let best = null;
        let bestScore = -1;
        for (const leaf of leaves) {
          const raw = expNorm(leaf.innerText || leaf.textContent || '');
          if (!raw || raw.length > 80) continue;
          const matched = expMatchType(raw);
          if (!matched || expLow(matched) !== wantType) continue;
          let cur = leaf;
          for (let d = 0; cur && d < 8; d += 1, cur = cur.parentElement) {
            if (!expVisible(cur)) continue;
            const r = cur.getBoundingClientRect();
            if (r.height > 220 || r.width < 40) continue;
            const btns = [...cur.querySelectorAll(
              'button, [role=button], a'
            )].filter(expVisible);
            const editBtns = btns.filter(expIsEditBtn);
            const delBtns = btns.filter(expIsDeleteBtn);
            const addBtns = btns.filter(expIsAddExpense);
            if (addBtns.length) continue;
            if (!editBtns.length && !delBtns.length) continue;
            let score = (editBtns.length ? 50 : 0) + (delBtns.length ? 20 : 0);
            score += Math.max(0, 40 - Math.floor(r.height / 4));
            if (score > bestScore) {
              bestScore = score;
              best = { el: cur, editBtns, delBtns };
            }
          }
        }
        return best;
      };
      const expClickEntryAction = (memberName, expenseType, action) => {
        const card = expFindCardEl(memberName);
        if (!card) return { ok: false, reason: 'card_not_found' };
        const wantType = expLow(expenseType);
        const wantAction = expLow(action);
        const row = expRowForType(card, wantType);
        if (row) {
          if (wantAction === 'edit' && row.editBtns.length) {
            expPressEl(row.editBtns[0]);
            return { ok: true, action: 'edit', via: 'type_row' };
          }
          if ((wantAction === 'delete' || wantAction === 'remove') &&
              row.delBtns.length) {
            expPressEl(row.delBtns[0]);
            return { ok: true, action: 'delete', via: 'type_row' };
          }
          if ((wantAction === 'delete' || wantAction === 'remove') &&
              row.editBtns.length) {
            expPressEl(row.editBtns[0]);
            return { ok: true, action: 'edit', via: 'type_row_pencil_for_delete' };
          }
        }
        const buttons = [...card.querySelectorAll(
          'button, [role=button], a'
        )].filter(expVisible);
        for (const btn of buttons) {
          let cur = btn.parentElement;
          let nearType = false;
          for (let d = 0; cur && d < 6; d += 1, cur = cur.parentElement) {
            const t = expLow(cur.innerText || '');
            if (t.includes(wantType)) { nearType = true; break; }
          }
          if (!nearType) continue;
          if (wantAction === 'edit' && expIsEditBtn(btn)) {
            expPressEl(btn);
            return { ok: true, action: 'edit', via: 'near_type' };
          }
          if ((wantAction === 'delete' || wantAction === 'remove') &&
              expIsDeleteBtn(btn)) {
            expPressEl(btn);
            return { ok: true, action: 'delete', via: 'near_type' };
          }
        }
        return { ok: false, reason: 'action_not_found', wantType, wantAction };
      };
      const expFindModalRoot = () => {
        const body = document.body;
        const candidates = [];
        for (const el of body.querySelectorAll('div, section, form, [role=dialog]')) {
          if (!expVisible(el)) continue;
          const t = expLow(el.innerText || '');
          const r = el.getBoundingClientRect();
          if (r.width < 200 || r.height < 120 || r.height > 900) continue;
          let score = 0;
          if (t.includes('amount per month')) score += 40;
          if (t.includes('medical expenses in the last')) score += 40;
          if (t.includes('select an expense')) score += 30;
          if (/\bsave\b/.test(t) && /\bcancel\b/.test(t)) score += 25;
          if (t.includes('upload proof of this expense')) score += 20;
          if (score < 30) continue;
          candidates.push({ el, score, area: r.width * r.height });
        }
        if (!candidates.length) return null;
        candidates.sort((a, b) => b.score - a.score || a.area - b.area);
        return candidates[0].el;
      };
      const expControlDisabled = (el) => {
        if (!el) return true;
        if (el.disabled) return true;
        const aria = (el.getAttribute('aria-disabled') || '').toLowerCase();
        if (aria === 'true') return true;
        return false;
      };
      const expFindModalSaveButton = () => {
        const root = expFindModalRoot() || document.body;
        const nodes = [...root.querySelectorAll(
          'button, [role=button], a'
        )].filter(expVisible);
        for (const el of nodes) {
          if (expBtnText(el) === 'save') return el;
        }
        return null;
      };
      const expClickModalButton = (label) => {
        const want = expLow(label);
        if (want === 'save') {
          const el = expFindModalSaveButton();
          if (!el) return { ok: false, reason: 'not_found' };
          if (expControlDisabled(el)) return { ok: false, reason: 'disabled' };
          expPressEl(el);
          return { ok: true };
        }
        const root = expFindModalRoot();
        const scopes = root ? [root, document.body] : [document.body];
        for (const scope of scopes) {
          const nodes = [...scope.querySelectorAll(
            'button, [role=button], a'
          )].filter(expVisible);
          for (const el of nodes) {
            const t = expBtnText(el);
            const aria = expLow(el.getAttribute('aria-label') || '');
            if (t === want || aria === want || t.startsWith(want + ' ') ||
                (want === 'delete' && (t === 'remove' || aria.includes('delete')))) {
              expPressEl(el);
              return { ok: true };
            }
          }
          if (root && scope === root) continue;
          break;
        }
        return { ok: false, reason: 'not_found', want };
      };
      const expClickType = (label) => {
        const want = expLow(label);
        const root = expFindModalRoot() || document.body;
        const nodes = [...root.querySelectorAll(
          'button, [role=button], a, div, span, li, label'
        )].filter(expVisible);
        let best = null;
        let bestLen = 1e9;
        for (const el of nodes) {
          const t = expNorm(el.innerText || el.textContent || '');
          if (!t || t.length > 80) continue;
          const low = expLow(t);
          if (low === want || low.startsWith(want)) {
            const len = t.length;
            if (len < bestLen) { best = el; bestLen = len; }
          }
        }
        if (!best) return { ok: false, reason: 'type_not_found', want };
        expPressEl(best);
        return { ok: true, label };
      };
      const expTypePaletteOpen = () => {
        const root = expFindModalRoot();
        const blob = expLow((root || document.body).innerText || '');
        return blob.includes('select an expense') ||
          (blob.includes('student loans') && blob.includes('medical expenses') &&
           blob.includes('none'));
      };
      const expFillAmount = (value) => {
        const root = expFindModalRoot() || document.body;
        const inputs = [...root.querySelectorAll(
          'input, textarea, [contenteditable=true]'
        )].filter(expVisible);
        let target = null;
        for (const el of inputs) {
          let cur = el.parentElement;
          let near = false;
          for (let d = 0; cur && d < 5; d += 1, cur = cur.parentElement) {
            if (expLow(cur.innerText || '').includes('amount per month')) {
              near = true; break;
            }
          }
          if (near) { target = el; break; }
        }
        if (!target && inputs.length) target = inputs[0];
        if (!target) return { ok: false, reason: 'amount_input_not_found' };
        const digits = String(value || '').replace(/[^0-9.]/g, '');
        try { target.focus(); } catch (e) {}
        try {
          const proto = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value'
          );
          if (proto && proto.set) proto.set.call(target, digits);
          else target.value = digits;
        } catch (e) {
          target.value = digits;
        }
        try {
          target.dispatchEvent(new Event('input', { bubbles: true }));
          target.dispatchEvent(new Event('change', { bubbles: true }));
        } catch (e) {}
        return { ok: true, value: digits };
      };
      const expSynthEvent = (el) => ({
        nativeEvent: { target: el, currentTarget: el },
        target: el,
        currentTarget: el,
        preventDefault() {},
        stopPropagation() {},
      });
      const expReactPress = (el) => {
        // RN Web / Radix RadioItem commits via onPress on Pressable.
        if (!el) return false;
        const fk = Object.keys(el).find((k) => k.startsWith('__reactFiber'));
        let fiber = fk ? el[fk] : null;
        for (let d = 0; fiber && d < 14; d += 1, fiber = fiber.return) {
          const p = fiber.memoizedProps || {};
          if (typeof p.onPress === 'function') {
            try { p.onPress(expSynthEvent(el)); return true; } catch (e) {}
          }
        }
        return false;
      };
      const expParseYn = (s) => {
        const t = expLow(s);
        if (t === 'yes' || t.startsWith('yes ')) return 'yes';
        if (t === 'no' || t.startsWith('no ')) return 'no';
        return '';
      };
      const expClickYnEl = (el) => {
        if (!el) return false;
        try { el.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
        if (expReactPress(el)) return true;
        const radio = (el.getAttribute('role') === 'radio')
          ? el
          : (el.querySelector && el.querySelector('[role=radio]'));
        if (radio && radio !== el && expReactPress(radio)) return true;
        expPressEl(el);
        if (radio && radio !== el) expPressEl(radio);
        return true;
      };
      const expMedicalYnSelected = (want) => {
        const wantLow = want === 'yes' ? 'yes' : 'no';
        for (const g of document.querySelectorAll('[role=radiogroup]')) {
          if (!expVisible(g)) continue;
          const t = expLow(g.innerText || '');
          if (!(t.includes('yes') && t.includes('no'))) continue;
          for (const r of g.querySelectorAll('[role=radio]')) {
            if ((r.getAttribute('aria-checked') || '') !== 'true') continue;
            const label = expParseYn(
              (r.parentElement && (r.parentElement.innerText || '')) ||
              r.getAttribute('aria-label') ||
              (r.innerText || '')
            );
            if (label === wantLow) return true;
          }
        }
        return false;
      };
      const expClickMedicalYn = (want) => {
        const wantRaw = expLow(want);
        const wantLow = (wantRaw === 'yes' || wantRaw.startsWith('yes '))
          ? 'yes'
          : ((wantRaw === 'no' || wantRaw.startsWith('no ')) ? 'no' : '');
        if (!wantLow) return { ok: false, reason: 'empty_value' };
        const root = expFindModalRoot() || document.body;
        const groups = [...root.querySelectorAll('[role=radiogroup]')]
          .filter(expVisible)
          .filter((g) => {
            const t = expLow(g.innerText || '');
            return t.includes('yes') && t.includes('no');
          });
        let group = null;
        for (const g of groups) {
          let cur = g.parentElement;
          let near = false;
          for (let d = 0; cur && d < 8; d += 1, cur = cur.parentElement) {
            const blob = expLow(cur.innerText || '');
            if (blob.includes('medical expenses in the last') ||
                blob.includes('need help paying')) {
              near = true;
              break;
            }
          }
          if (near || groups.length === 1) { group = g; break; }
        }
        if (!group && groups.length) group = groups[0];
        if (group) {
          for (const radio of [...group.querySelectorAll('[role=radio]')].filter(expVisible)) {
            const rowText = expNorm(
              (radio.parentElement && radio.parentElement.innerText) ||
              (radio.getAttribute('aria-label') || '') ||
              (radio.innerText || '')
            );
            if (expParseYn(rowText) !== wantLow) continue;
            const row = radio.closest('[tabindex]') || radio.parentElement;
            if (expClickYnEl(radio) || (row && expClickYnEl(row))) {
              return {
                ok: true,
                value: wantLow,
                via: 'radiogroup',
                verified: expMedicalYnSelected(wantLow),
              };
            }
          }
        }
        // Fallback: exact Yes/No text near the medical question.
        const nodes = [...root.querySelectorAll(
          'button, [role=button], [role=radio], label, div, span'
        )].filter(expVisible);
        for (const el of nodes) {
          const t = expLow(
            el.innerText || el.textContent || el.getAttribute('aria-label') || ''
          );
          if (!(t === wantLow || t.startsWith(wantLow + ' '))) continue;
          let near = false;
          let cur = el;
          for (let d = 0; cur && d < 6; d += 1, cur = cur.parentElement) {
            const blob = expLow(cur.innerText || '');
            if (blob.includes('medical expenses in the last') ||
                blob.includes('need help paying')) {
              near = true; break;
            }
          }
          if (!near && groups.length) continue;
          if (expClickYnEl(el)) {
            return {
              ok: true,
              value: wantLow,
              via: 'near_text',
              verified: expMedicalYnSelected(wantLow),
            };
          }
        }
        return {
          ok: false,
          reason: 'yn_not_found',
          want: wantLow,
          verified: expMedicalYnSelected(wantLow),
        };
      };
      const expClickAddExpense = (memberName) => {
        const card = expFindCardEl(memberName);
        const scope = card || document.body;
        const buttons = [...scope.querySelectorAll(
          'button, [role=button], a'
        )].filter(expVisible).filter(expIsAddExpense);
        if (!buttons.length) return { ok: false, reason: 'add_not_found' };
        expPressEl(buttons[0]);
        return { ok: true };
      };
      const expClickAddAnotherExpense = (memberName) => {
        const card = expFindCardEl(memberName);
        if (!card) return { ok: false, reason: 'card_not_found' };
        const buttons = [...card.querySelectorAll(
          'button, [role=button], a'
        )].filter(expVisible);
        for (const btn of buttons) {
          const t = expBtnText(btn);
          const aria = expLow(btn.getAttribute('aria-label') || '');
          if (t.includes('add another expense') || aria.includes('add another expense') ||
              (expIsAddExpense(btn) && (t.includes('another') || aria.includes('another')))) {
            expPressEl(btn);
            return { ok: true };
          }
        }
        for (const btn of buttons) {
          if (expIsAddExpense(btn)) {
            expPressEl(btn);
            return { ok: true, fallback: true };
          }
        }
        return { ok: false, reason: 'add_another_not_found' };
      };
    """


def _expense_phase(session: Any) -> str:
    phase = str(session.filled.get("app_expense_phase") or "").strip()
    return phase if phase in _EXPENSE_PHASES else "type"


def _set_expense_phase(session: Any, phase: str) -> None:
    session.filled["app_expense_phase"] = phase


def _expense_heard(session: Any) -> list[int]:
    raw = session.filled.get("app_expense_heard") or "[]"
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [int(x) for x in data if str(x).isdigit() or isinstance(x, int)]
    except Exception:
        pass
    return []


def _mark_expense_heard(session: Any, index: int) -> None:
    heard = _expense_heard(session)
    if index not in heard:
        heard.append(index)
    session.filled["app_expense_heard"] = json.dumps(heard)


def _member_types_map(session: Any) -> dict[str, list[str]]:
    raw = session.filled.get("app_expense_member_types") or "{}"
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
    session.filled["app_expense_member_types"] = json.dumps(data)


def _record_member_type(session: Any, member: str, expense_type: str) -> bool:
    key = (member or "").strip() or "_unknown"
    mapping = _member_types_map(session)
    existing = list(mapping.get(key) or [])
    overridden = expense_type in existing
    if overridden:
        existing = [t for t in existing if t != expense_type]
    existing.append(expense_type)
    mapping[key] = existing
    _set_member_types_map(session, mapping)
    return overridden


def _member_existing_types(session: Any, name: str) -> list[str]:
    return list(_member_types_map(session).get((name or "").strip()) or [])


def _clear_expense_member_state(session: Any) -> None:
    for key in (
        "app_expense_current",
        "app_expense_current_index",
        "app_expense_pending_type",
        "app_expense_pending_summary",
        "app_expense_pending_values",
        "app_expense_edit_mode",
        "app_expense_delete_type",
        "app_expense_manage_action",
        "app_expense_add_opened",
    ):
        session.filled.pop(key, None)
    _set_expense_phase(session, "type")


def _clear_all_expense_state(session: Any) -> None:
    for key in _EXPENSE_FILLED_KEYS:
        session.filled.pop(key, None)


def map_expense_type(spoken: str) -> str | None:
    """Map spoken text onto a Pointcare expense-type label."""
    lowered = re.sub(r"[.!?…]+$", "", (spoken or "").strip().lower())
    lowered = re.sub(r"\s+", " ", lowered).strip()
    lowered = lowered.replace("\u2019", "'").replace("`", "'")
    if not lowered:
        return None
    lowered = re.sub(
        r"^(?:yes|yeah|yep|yup|yea|ok|okay)(?:[.,!]+\s*|\s+)",
        "",
        lowered,
    ).strip()
    lowered = re.sub(r"^(?:it'?s|it\s+is|that'?s)\s+", "", lowered).strip()
    if not lowered:
        return None
    if re.search(r"\bstudent\s+loans?\b", lowered) or (
        "student" in lowered and re.search(r"\bloans?\b", lowered)
    ):
        return "Student Loans"
    # Tuition — include common ASR misspellings / near-homophones.
    # Live miss: "Tuition fees" → Retell "Fusion fees."
    compact = re.sub(r"[\s\-]+", "", lowered)
    if (
        re.search(r"\btuition\b", lowered)
        or re.search(r"\btution\b", lowered)
        or re.search(r"\btuition\s+fees?\b", lowered)
        or re.search(r"\btution\s+fees?\b", lowered)
        or re.search(r"\btooshun\b", lowered)
        or re.search(r"\btoo\s*shun\b", lowered)
        or re.search(r"\btwo\s*shun\b", lowered)
        or re.search(r"\btew\s*shun\b", lowered)
        or re.search(r"\bchoo\s*shun\b", lowered)
        or re.search(r"\bfusion\s+fees?\b", lowered)
        or re.search(r"\bfusion\b", lowered)
        or re.search(r"\bcontusion\s+fees?\b", lowered)
        or re.search(r"\bintuition\s+fees?\b", lowered)
        or re.search(r"\btuitionfee", compact)
        or re.search(r"\btutionfee", compact)
        or re.search(r"\bfusionfee", compact)
        or re.search(r"\btooshunfee", compact)
    ):
        return "Tuition Fees"
    if re.search(r"\balimony\s+paid\b", lowered) or (
        "alimony" in lowered and "paid" in lowered
    ):
        return "Alimony Paid"
    if re.search(r"\bmoving\b", lowered):
        return "Moving Expenses"
    if re.search(r"\bira\b", lowered) or re.search(r"\bcontribution", lowered):
        return "IRA Contributions"
    if re.search(r"\bchild\s*care\b", lowered) or re.search(r"\bchildcare\b", lowered):
        return "Childcare"
    if re.search(r"\bmedical\b", lowered):
        return "Medical Expenses"
    if re.search(r"\bnone\b", lowered) or "no expense" in lowered:
        return "None"
    if re.search(r"(?<![a-z])other(?![a-z])", lowered) and not re.search(
        r"\banother\b", lowered
    ):
        return "Other"
    for label in _EXPENSE_TYPE_LABELS:
        low = label.lower()
        if low == "other":
            continue
        if low in lowered:
            return label
    return None


def _expense_types_listed(session: Any | None) -> bool:
    if session is None:
        return False
    return str(session.filled.get("app_expense_types_listed") or "") == "1"


def _mark_expense_types_listed(session: Any | None) -> None:
    if session is None:
        return
    session.filled["app_expense_types_listed"] = "1"


def _document_notice(expense_type: str) -> str:
    typ = (expense_type or "").strip()
    if typ == "Student Loans":
        examples = "student loan statements or award letters from your school"
    elif typ == "Medical Expenses":
        examples = "medical bills or receipts from providers or pharmacies"
    elif typ == "Childcare":
        examples = "childcare invoices or provider statements"
    elif typ == "Tuition Fees":
        examples = "tuition bills or school statements"
    else:
        examples = "receipts, statements, or award letters"
    return (
        "We'll ask you to upload proof of this expense later, so have the "
        f"relevant documents ready. This could include {examples}."
    )


def _doc_notice_members(session: Any | None) -> list[str]:
    if session is None:
        return []
    raw = session.filled.get("app_expense_doc_notice_members") or "[]"
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
    session.filled["app_expense_doc_notice_members"] = json.dumps(members)


def _with_document_notice(
    speech: str,
    expense_type: str,
    *,
    session: Any | None = None,
    member_name: str = "",
) -> str:
    body = (speech or "").strip()
    who = (member_name or "").strip()
    if session is not None and who and _doc_notice_said_for(session, who):
        return body
    notice = _document_notice(expense_type)
    if session is not None and who:
        _mark_doc_notice_said(session, who)
    if not body:
        return notice
    return f"{notice} {body}"


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
            "I'll add deductible and medical expenses for each person. "
            + prefix
        )
    types = [t for t in (existing or []) if t and t != "None"]
    have = ""
    if types:
        have = f" I already see {', '.join(types[:6])} on file."
    ask = "What expense type should I enter?"
    if include_options:
        ask = f"{ask} {_EXPENSE_TYPE_OPTIONS_SPEECH}"
    return prefix + have + " " + ask


def _type_ask_for_session(
    session: Any,
    name: str,
    *,
    intro: bool = False,
    existing: list[str] | None = None,
    force_options: bool = False,
) -> str:
    include = bool(force_options) or not _expense_types_listed(session)
    if include:
        _mark_expense_types_listed(session)
    return _type_ask_speech(
        name, intro=intro, existing=existing, include_options=include
    )


def _amount_ask_speech(
    name: str,
    expense_type: str,
    *,
    override: bool = False,
    session: Any | None = None,
) -> str:
    who = name or "this person"
    lead = (
        f"Okay, replacing the previous {expense_type} amount for {who}. "
        if override
        else f"Okay, {expense_type} for {who}. "
    )
    return _with_document_notice(
        lead + "What is the amount per month?",
        expense_type,
        session=session,
        member_name=who,
    )


def _medical_ask_speech(name: str, *, session: Any | None = None) -> str:
    who = name or "this person"
    return _with_document_notice(
        f"For {who}, do you have medical expenses in the last 3 months that "
        "you need help paying for? Please say Yes or No.",
        "Medical Expenses",
        session=session,
        member_name=who,
    )


def _more_ask_speech(name: str, existing: list[str] | None = None) -> str:
    """Yes/No only — type and edit/delete are asked on later turns."""
    del existing  # kept for call-site compatibility
    who = name or "this person"
    return (
        f"Do you want to add another expense for {who}? "
        "Please say Yes or No."
    )


def _manage_ask_speech(name: str, existing: list[str] | None = None) -> str:
    """Asked only after the caller declines adding another expense."""
    who = name or "this person"
    types = [t for t in (existing or []) if t and t != "None"]
    listed = ", ".join(types[:6]) if types else ""
    if listed:
        return (
            f"Do you want to edit or delete an existing expense for {who} "
            f"({listed})? Say edit, delete, or No if you're done."
        )
    return (
        f"Do you want to edit or delete an existing expense for {who}? "
        "Say edit, delete, or No if you're done."
    )


def _confirm_expense_speech(
    name: str, expense_type: str, values: dict[str, Any]
) -> str:
    who = name or "this person"
    typ = expense_type or "that expense"
    parts: list[str] = []
    if "amount" in values and values.get("amount"):
        parts.append(f"amount per month {values.get('amount')}")
    if "medical_help" in values:
        parts.append(
            "needs help paying for medical expenses in the last 3 months"
            if str(values.get("medical_help")).lower() == "yes"
            else "does not need help paying for medical expenses in the last 3 months"
        )
    detail = "; ".join(parts) if parts else "the details we collected"
    return (
        f"For {who}, I have {typ}: {detail}. "
        "Does that look correct? Say Yes to save, or say edit if you want to change something."
    )


def _delete_confirm_speech(name: str, expense_type: str) -> str:
    who = name or "this person"
    label = (expense_type or "that").strip() or "that"
    if not re.search(r"\bexpense", label, flags=re.I):
        label = f"{label} expense"
    return f"Can I delete the {label} for {who}? Please say Yes or No."


def _parse_edit_delete_intent(spoken: str) -> tuple[str, str] | None:
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
    residue = re.sub(
        r"\b(please\s+)?(delete|deleted|deleting|remove|removed|removing|"
        r"edit|change|update)\b",
        " ",
        lowered,
        flags=re.I,
    )
    residue = re.sub(r"\b(the|expense|for|entry|source)\b", " ", residue)
    picked = map_expense_type(residue) or map_expense_type(text)
    if not picked or picked == "None":
        return action, ""
    return action, picked


def _strip_another_expense_filler(spoken: str) -> str:
    text = (spoken or "").strip()
    if not text:
        return ""
    out = re.sub(
        r"(?i)\b(yes|yeah|yep|okay|ok)?\s*(i\s+want\s+to\s+)?add\s+"
        r"(another\s+)?expense\b",
        " ",
        text,
    )
    out = re.sub(r"(?i)\banother\s+expense\b", " ", out)
    return re.sub(r"\s+", " ", out).strip()


def _card_entries(card: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(card, dict):
        return []
    raw = card.get("entries") or []
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict) and str(e.get("type") or "").strip()]


def _pending_expense_values(session: Any) -> dict[str, Any]:
    raw = session.filled.get("app_expense_pending_values") or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _set_pending_confirm(
    session: Any, expense_type: str, values: dict[str, Any], summary: str
) -> None:
    session.filled["app_expense_pending_type"] = expense_type
    session.filled["app_expense_pending_values"] = json.dumps(values)
    session.filled["app_expense_pending_summary"] = summary
    _set_expense_phase(session, "confirm")


async def probe_expense_screen(page: Page) -> dict[str, Any]:
    try:
        raw = await page.evaluate(
            "() => {"
            + _expense_dom_js()
            + """
              const body = expLow(document.body && document.body.innerText);
              const cards = expFindCards();
              const addButtons = [...document.querySelectorAll(
                'button, [role=button], a'
              )].filter(expVisible).filter(expIsAddExpense);
              const isExpense =
                body.includes('household expense') ||
                body.includes('add deductible') ||
                addButtons.length >= 1 ||
                cards.length >= 1;
              return {
                is_expense: isExpense,
                add_count: addButtons.length,
                card_count: cards.length,
                card_names: cards.map((c) => c.name).slice(0, 8),
                reason: isExpense
                  ? (body.includes('household expense')
                      ? 'expense_heading'
                      : (addButtons.length ? 'add_expense_buttons' : 'cards'))
                  : 'no_expense_signal',
              };
            }"""
        )
        return raw if isinstance(raw, dict) else {}
    except Exception:
        logger.exception("probe_expense_screen failed")
        return {"is_expense": False, "reason": "probe_failed"}


async def scrape_expense_cards(page: Page) -> list[dict[str, Any]]:
    try:
        raw = await page.evaluate(
            "() => {" + _expense_dom_js() + " return expFindCards(); }"
        )
        if not isinstance(raw, list):
            return []
        rows = [
            r for r in raw if isinstance(r, dict) and str(r.get("name") or "").strip()
        ]
        logger.info(
            "scrape_expense_cards found=%s names=%s",
            len(rows),
            [str(r.get("name") or "") for r in rows],
        )
        return rows
    except Exception:
        logger.exception("scrape_expense_cards failed")
        return []


async def scrape_expense_cards_with_retry(
    page: Page, *, attempts: int = 3, delay_ms: int = 400
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for attempt in range(max(1, attempts)):
        cards = await scrape_expense_cards(page)
        if cards:
            return cards
        if attempt + 1 < attempts:
            await page.wait_for_timeout(delay_ms)
    return cards


async def _click_add_expense(page: Page, member_name: str) -> bool:
    try:
        result = await page.evaluate(
            "(memberName) => {"
            + _expense_dom_js()
            + " return expClickAddExpense(memberName); }",
            member_name,
        )
        if isinstance(result, dict) and result.get("ok"):
            await page.wait_for_timeout(350)
            return True
        return False
    except Exception:
        logger.exception("click_add_expense failed member=%r", member_name)
        return False


async def _click_add_another_expense(page: Page, member_name: str) -> dict[str, Any]:
    try:
        result = await page.evaluate(
            "(memberName) => {"
            + _expense_dom_js()
            + " return expClickAddAnotherExpense(memberName); }",
            member_name,
        )
        out = result if isinstance(result, dict) else {"ok": False}
        if out.get("ok"):
            await page.wait_for_timeout(350)
        return out
    except Exception:
        logger.exception("click_add_another_expense failed member=%r", member_name)
        return {"ok": False, "reason": "exception"}


async def _expense_type_palette_open(page: Page) -> bool:
    try:
        return bool(
            await page.evaluate(
                "() => {" + _expense_dom_js() + " return expTypePaletteOpen(); }"
            )
        )
    except Exception:
        return False


async def _click_expense_type(page: Page, label: str) -> bool:
    want = (label or "").strip()
    if not want:
        return False
    try:
        result = await page.evaluate(
            "(label) => {" + _expense_dom_js() + " return expClickType(label); }",
            want,
        )
        if isinstance(result, dict) and result.get("ok"):
            await page.wait_for_timeout(200)
            return True
        pattern = re.compile(rf"^\s*{re.escape(want)}\s*$", re.I)
        for role_name in ("button", "option", "radio", "menuitem"):
            role = page.get_by_role(role_name, name=pattern)
            if await role.count():
                try:
                    await role.first.scroll_into_view_if_needed(timeout=800)
                    await role.first.click(timeout=1500)
                    await page.wait_for_timeout(180)
                    return True
                except Exception:
                    pass
        loc = page.get_by_text(pattern)
        if await loc.count():
            try:
                await loc.first.click(timeout=1500)
                await page.wait_for_timeout(180)
                return True
            except Exception:
                pass
        logger.warning("click_expense_type failed label=%r result=%r", want, result)
        return False
    except Exception:
        logger.exception("click_expense_type failed label=%r", want)
        return False


async def _open_type_and_select(
    page: Page, member_name: str, expense_type: str
) -> dict[str, Any]:
    logger.info(
        "open_expense_type_and_select member=%r type=%r",
        member_name,
        expense_type,
    )
    opened = await _expense_type_palette_open(page)
    if not opened:
        opened = await _click_add_expense(page, member_name)
        if not opened:
            from .live_form import _click_cta

            try:
                await _click_cta(page, "Add Another Expense", attempts=1)
                opened = True
            except Exception:
                try:
                    await _click_cta(page, "Add Expense", attempts=1)
                    opened = True
                except Exception:
                    opened = False
        if opened:
            await page.wait_for_timeout(350)
    if not opened and not await _expense_type_palette_open(page):
        return {"ok": False, "reason": "add_expense_failed"}
    for attempt in range(2):
        if await _click_expense_type(page, expense_type):
            await page.wait_for_timeout(250)
            # Palette should close / detail modal open.
            if expense_type == "None" or not await _expense_type_palette_open(page):
                return {"ok": True}
            # Medical / amount screens still count as success if type text visible.
            try:
                body = (await page.locator("body").inner_text()).lower()
            except Exception:
                body = ""
            if expense_type.lower() in body and (
                "amount per month" in body
                or "medical expenses in the last" in body
                or "upload proof of this expense" in body
            ):
                return {"ok": True}
        if attempt == 0:
            await page.wait_for_timeout(250)
    return {"ok": False, "reason": "type_select_failed"}


async def _click_expense_entry_action(
    page: Page, member_name: str, expense_type: str, action: str
) -> dict[str, Any]:
    try:
        result = await page.evaluate(
            "({ memberName, expenseType, action }) => {"
            + _expense_dom_js()
            + " return expClickEntryAction(memberName, expenseType, action); }",
            {
                "memberName": member_name,
                "expenseType": expense_type,
                "action": action,
            },
        )
        out = result if isinstance(result, dict) else {"ok": False}
        if out.get("ok"):
            await page.wait_for_timeout(400)
        else:
            logger.warning(
                "click_expense_entry_action failed member=%r type=%r action=%r result=%r",
                member_name,
                expense_type,
                action,
                out,
            )
        return out
    except Exception:
        logger.exception(
            "click_expense_entry_action failed member=%r type=%r action=%r",
            member_name,
            expense_type,
            action,
        )
        return {"ok": False, "reason": "exception"}


async def _open_edit_or_add(
    page: Page,
    member_name: str,
    expense_type: str,
    *,
    edit_mode: bool,
) -> dict[str, Any]:
    if edit_mode:
        opened = await _click_expense_entry_action(
            page, member_name, expense_type, "edit"
        )
        if opened.get("ok"):
            return {"ok": True, "mode": "edit", "via": opened.get("via")}
    return await _open_type_and_select(page, member_name, expense_type)


async def _fill_expense_amount(page: Page, value: str) -> bool:
    want = str(value or "").strip()
    if not want:
        return False
    digits = re.sub(r"[^0-9.]", "", want)
    # Prefer Playwright keyboard for React.
    try:
        getter = getattr(page, "get_by_label", None)
        if callable(getter):
            loc = getter(re.compile(r"amount per month", re.I))
            if hasattr(loc, "count"):
                count = loc.count()
                if hasattr(count, "__await__"):
                    count = await count
                if count:
                    first = loc.first
                    await first.click(timeout=1500)
                    await first.fill("")
                    await first.type(digits or want, delay=30)
                    await page.wait_for_timeout(120)
                    return True
    except Exception:
        pass
    try:
        result = await page.evaluate(
            "(value) => {" + _expense_dom_js() + " return expFillAmount(value); }",
            digits or want,
        )
        return bool(isinstance(result, dict) and result.get("ok"))
    except Exception:
        logger.exception("fill_expense_amount failed")
        return False


async def _playwright_click_medical_yn(page: Page, value: str) -> bool:
    """Force-click Yes/No in the medical radiogroup (Radix / RN Pressable)."""
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
        # Scope to medical question copy when possible.
        near = page.locator("div, section, form").filter(
            has_text=re.compile(
                r"medical expenses in the last|need help paying", re.I
            )
        ).filter(has_text=pattern).first
        if await near.count():
            try:
                await near.get_by_text(pattern).last.click(timeout=1500, force=True)
                return True
            except Exception:
                pass
        loc = page.get_by_role("radio", name=pattern)
        if await loc.count():
            await loc.first.click(timeout=1500, force=True)
            return True
    except Exception:
        logger.exception("playwright_click_medical_yn failed value=%r", value)
    return False


async def _medical_yn_selected(page: Page, value: str) -> bool:
    want_raw = (value or "").strip().lower()
    if want_raw.startswith("y"):
        want = "yes"
    elif want_raw.startswith("n"):
        want = "no"
    else:
        return False
    try:
        return bool(
            await page.evaluate(
                "(want) => {"
                + _expense_dom_js()
                + " return expMedicalYnSelected(want); }",
                want,
            )
        )
    except Exception:
        return False


async def _click_medical_yn(page: Page, value: str) -> bool:
    """Click Yes/No for medical expenses (last 3 months help paying)."""
    want = (value or "").strip()
    if not want:
        return False
    want_l = want.lower()
    is_yes = want_l.startswith("y")
    is_no = want_l.startswith("n")
    if not (is_yes or is_no):
        return False
    try:
        # Playwright first — real pointer events commit Radix/RHF.
        clicked = await _playwright_click_medical_yn(page, want)
        via = "playwright" if clicked else ""
        if clicked:
            await page.wait_for_timeout(350)
        if not await _medical_yn_selected(page, want):
            result = await page.evaluate(
                "(want) => {"
                + _expense_dom_js()
                + " return expClickMedicalYn(want); }",
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
                    "click_medical_yn failed value=%r playwright=%r js=%r",
                    want,
                    via or False,
                    result,
                )
                return False
        selected = await _medical_yn_selected(page, want)
        if not selected:
            # Retry Playwright once more before giving up.
            if await _playwright_click_medical_yn(page, want):
                await page.wait_for_timeout(300)
                selected = await _medical_yn_selected(page, want)
        logger.info(
            "click_medical_yn value=%r via=%r selected=%s",
            want,
            via or "none",
            selected,
        )
        return bool(selected)
    except Exception:
        logger.exception("click_medical_yn failed value=%r", value)
        return False


async def _click_expense_cancel(page: Page) -> bool:
    try:
        result = await page.evaluate(
            "() => {"
            + _expense_dom_js()
            + """
              const cancel = expClickModalButton('Cancel');
              if (cancel && cancel.ok) return true;
              const close = expClickModalButton('Close');
              return Boolean(close && close.ok);
            }"""
        )
        if result:
            await page.wait_for_timeout(250)
        return bool(result)
    except Exception:
        logger.exception("click_expense_cancel failed")
        return False


async def _click_expense_save(page: Page) -> bool:
    try:
        result = await page.evaluate(
            "() => {" + _expense_dom_js() + " return expClickModalButton('Save'); }"
        )
        if isinstance(result, dict) and result.get("ok"):
            await page.wait_for_timeout(400)
            return True
        loc = page.get_by_role("button", name=re.compile(r"^\s*save\s*$", re.I))
        if await loc.count():
            await loc.first.click(timeout=2000, force=True)
            await page.wait_for_timeout(400)
            return True
        return False
    except Exception:
        logger.exception("click_expense_save failed")
        return False


async def _click_expense_modal_delete(page: Page) -> dict[str, Any]:
    try:
        result = await page.evaluate(
            "() => {" + _expense_dom_js() + " return expClickModalButton('Delete'); }"
        )
        if isinstance(result, dict) and result.get("ok"):
            return {"ok": True, "via": "dom"}
    except Exception:
        result = {"ok": False}
    try:
        loc = page.get_by_role("button", name=re.compile(r"^\s*delete\s*$", re.I))
        await expect(loc.first).to_be_visible(timeout=2500)
        await loc.first.click(timeout=2500, force=True)
        return {"ok": True, "via": "playwright"}
    except Exception as exc:
        return {"ok": False, "reason": "delete_not_clicked", "error": str(exc), "dom": result}


async def _verify_expense_absent(
    page: Page, member_name: str, expense_type: str
) -> bool:
    want = (expense_type or "").strip().lower()
    who = (member_name or "").strip().lower()
    if not want:
        return False
    for _ in range(8):
        cards = await scrape_expense_cards(page)
        for card in cards:
            name = str(card.get("name") or "").strip().lower()
            if who and not (
                name == who or name.startswith(who) or who.startswith(name)
            ):
                continue
            present = any(
                str(e.get("type") or "").strip().lower() == want
                for e in _card_entries(card)
            )
            return not present
        await page.wait_for_timeout(250)
    return False


async def _delete_expense_entry(
    page: Page, member_name: str, expense_type: str
) -> bool:
    logger.info(
        "delete_expense_entry start member=%r type=%r", member_name, expense_type
    )
    pencil = await _click_expense_entry_action(
        page, member_name, expense_type, "edit"
    )
    if not pencil.get("ok"):
        direct = await _click_expense_entry_action(
            page, member_name, expense_type, "delete"
        )
        if direct.get("ok") and direct.get("action") == "delete":
            return await _verify_expense_absent(page, member_name, expense_type)
        if not direct.get("ok"):
            return False
    try:
        await page.get_by_role(
            "button", name=re.compile(r"^\s*delete\s*$", re.I)
        ).first.wait_for(state="visible", timeout=3000)
    except Exception:
        await page.wait_for_timeout(500)
    clicked = await _click_expense_modal_delete(page)
    if not clicked.get("ok"):
        await _click_expense_cancel(page)
        return False
    await page.wait_for_timeout(400)
    await _verify_expense_absent(page, member_name, expense_type)
    return True


def _begin_confirm(
    call_id: str,
    session: Any,
    answer: str,
    name: str,
    expense_type: str,
    values: dict[str, Any],
) -> dict[str, Any]:
    from .app_stage import _app_ok, _set_pending_question

    summary = _confirm_expense_speech(name, expense_type, values)
    _set_pending_confirm(session, expense_type, values, summary)
    _set_pending_question(session, "app_expenses_confirm", summary)
    return _app_ok(
        call_id,
        session,
        answer,
        "app_expenses_confirm",
        "Confirm expense before save",
        spoken_override=summary,
    )


async def _finish_expense_saved_ok(
    call_id: str,
    session: Any,
    text: str,
    name: str,
    pending: str,
) -> dict[str, Any]:
    from .app_stage import _app_ok, _set_pending_question

    if pending:
        _record_member_type(session, name, pending)
    for key in (
        "app_expense_pending_type",
        "app_expense_pending_values",
        "app_expense_pending_summary",
        "app_expense_edit_mode",
    ):
        session.filled.pop(key, None)
    _set_expense_phase(session, "more")
    speech = _more_ask_speech(name, _member_existing_types(session, name))
    _set_pending_question(session, "app_expenses_more", speech)
    return _app_ok(
        call_id,
        session,
        text,
        "app_expenses_more",
        "Saved expense after confirm",
        spoken_override=speech,
    )


async def _save_confirmed_expense(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    name: str,
) -> dict[str, Any]:
    from .app_stage import _set_pending_question

    pending = str(session.filled.get("app_expense_pending_type") or "").strip()
    values = _pending_expense_values(session)
    if pending in _EXPENSE_AMOUNT_ONLY_TYPES and values.get("amount"):
        await _fill_expense_amount(page, str(values.get("amount")))
    if pending == "Medical Expenses" and values.get("medical_help"):
        await _click_medical_yn(page, str(values.get("medical_help")))
    saved = await _click_expense_save(page)
    if not saved:
        speech = (
            str(session.filled.get("app_expense_pending_summary") or "").strip()
            or _confirm_expense_speech(name, pending or "that expense", values)
        )
        speech = "I couldn't save that expense yet. " + speech
        _set_pending_question(session, "app_expenses_confirm", speech)
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "expense_save_failed",
            "step": "app_expenses_confirm",
            "phase": "application",
        }
    return await _finish_expense_saved_ok(call_id, session, text, name, pending)


async def _begin_expense_member(
    session: Any, page: Page, card: dict[str, Any], *, intro: bool = False
) -> str:
    name = str(card.get("name") or "").strip()
    index = int(card.get("index") or 0)
    session.filled["app_expense_current"] = name
    session.filled["app_expense_current_index"] = str(index)
    _clear_expense_member_state(session)
    session.filled["app_expense_current"] = name
    session.filled["app_expense_current_index"] = str(index)
    _set_expense_phase(session, "type")
    existing = [
        str(e.get("type") or "").strip()
        for e in _card_entries(card)
        if str(e.get("type") or "").strip()
    ]
    if not existing:
        existing = _member_existing_types(session, name)
    elif name:
        mapping = _member_types_map(session)
        mapping[name] = existing
        _set_member_types_map(session, mapping)
    opened = await _click_add_expense(page, name)
    if not opened:
        from .live_form import _click_cta

        try:
            await _click_cta(page, "Add Another Expense", attempts=1)
            opened = True
        except Exception:
            try:
                await _click_cta(page, "Add Expense", attempts=1)
                opened = True
            except Exception:
                opened = False
    if opened:
        session.filled["app_expense_add_opened"] = "1"
    return _type_ask_for_session(session, name, intro=intro, existing=existing)


async def _advance_after_expense_member(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    cards: list[dict[str, Any]],
    index: int,
) -> dict[str, Any]:
    from .app_stage import _app_ok, _set_pending_question

    _mark_expense_heard(session, index)
    _clear_expense_member_state(session)
    heard = set(_expense_heard(session))
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
        speech = await _begin_expense_member(session, page, cards[nxt], intro=False)
        _set_pending_question(session, "app_expenses", speech)
        return _app_ok(
            call_id,
            session,
            text,
            "app_expenses",
            "expense next member",
            spoken_override=speech,
        )
    return await _finish_expense_review_next(call_id, session, page, text)


async def _finish_expense_review_next(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
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

    session.filled["app_expense_review_ready"] = "1"
    _set_expense_phase(session, "final")
    clicked = await _click_stage_next(page)
    if not clicked:
        speech = (
            "I've finished household expenses for everyone, but Next is not "
            "available yet. Say continue to try again."
        )
        _set_pending_question(session, "app_expenses", speech)
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "expense_next_disabled",
            "step": "app_expenses",
            "phase": "application",
        }
    next_step = await _advance_after_answer(
        page, session, allow_extra_next=False, leave_step="app_expenses"
    )
    if not next_step or next_step == "app_expenses":
        next_step = await _wait_for_screen_change(page, "app_expenses", timeout_s=3.5)
    if not next_step or str(next_step).startswith("app_expenses"):
        speech = (
            "I've finished household expenses, but the expenses screen is still open. "
            "Say continue to try Next again."
        )
        _set_pending_question(session, "app_expenses", speech)
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "expense_next_stayed",
            "step": "app_expenses",
            "phase": "application",
        }
    _clear_all_expense_state(session)
    try:
        next_step, spoken = await _prepare_resume_speech(session, page, next_step)
    except Exception:
        spoken = app_question_for_step(next_step, session)
    lead = "I've finished household expenses for everyone. "
    return _app_ok(
        call_id,
        session,
        text,
        next_step,
        "expense review advanced",
        spoken_override=lead + (spoken or ""),
    )


async def prepare_expense_speech(session: Any, page: Page) -> tuple[str, str]:
    """Entry / resume speech for Household Expenses."""
    from .app_stage import _set_pending_question, app_question_for_step

    cards = await scrape_expense_cards(page)
    current = str(session.filled.get("app_expense_current") or "").strip()
    phase = _expense_phase(session)
    pending_type = str(session.filled.get("app_expense_pending_type") or "").strip()

    if current and phase in _EXPENSE_PHASES and phase not in ("final",):
        if phase == "type":
            speech = _type_ask_for_session(
                session,
                current,
                existing=_member_existing_types(session, current),
            )
        elif phase == "amount":
            speech = _amount_ask_speech(
                current, pending_type or "that expense", session=session
            )
        elif phase == "medical":
            speech = _medical_ask_speech(current, session=session)
            _set_pending_question(session, "app_expenses_medical", speech)
            return "app_expenses_medical", speech
        elif phase == "more":
            speech = _more_ask_speech(
                current, _member_existing_types(session, current)
            )
            _set_pending_question(session, "app_expenses_more", speech)
            return "app_expenses_more", speech
        elif phase == "manage":
            speech = _manage_ask_speech(
                current, _member_existing_types(session, current)
            )
            _set_pending_question(session, "app_expenses_more", speech)
            return "app_expenses_more", speech
        elif phase == "confirm":
            speech = str(
                session.filled.get("app_expense_pending_summary") or ""
            ).strip() or _confirm_expense_speech(
                current, pending_type or "that expense", _pending_expense_values(session)
            )
            _set_pending_question(session, "app_expenses_confirm", speech)
            return "app_expenses_confirm", speech
        elif phase == "delete_confirm":
            del_type = str(session.filled.get("app_expense_delete_type") or "").strip()
            speech = _delete_confirm_speech(current, del_type or "that")
            _set_pending_question(session, "app_expenses_confirm", speech)
            return "app_expenses_confirm", speech
        else:
            speech = app_question_for_step("app_expenses", session)
        _set_pending_question(session, "app_expenses", speech)
        return "app_expenses", speech

    session.filled["app_expense_heard"] = "[]"
    session.filled["app_expense_walk_index"] = "0"
    session.filled.pop("app_expense_review_ready", None)
    if not cards:
        cards = await scrape_expense_cards_with_retry(page)
    if not cards:
        speech = (
            "I'm on the household expenses screen, but I can't see the member cards "
            "yet. Please stay on this page and say try again."
        )
        _set_pending_question(session, "app_expenses", speech)
        return "app_expenses", speech
    speech = await _begin_expense_member(session, page, cards[0], intro=True)
    _set_pending_question(session, "app_expenses", speech)
    return "app_expenses", speech


async def handle_expense_step(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    lowered: str,
    step: str,
) -> dict[str, Any]:
    """Router for app_expenses and amount/medical/more/confirm substeps."""
    from .app_stage import _app_ok, _set_pending_question
    from .live_form import _is_no, _is_yes

    cards = await scrape_expense_cards(page)
    phase = _expense_phase(session)
    current = str(session.filled.get("app_expense_current") or "").strip()
    index_raw = str(session.filled.get("app_expense_current_index") or "").strip()
    index = int(index_raw) if index_raw.isdigit() else 0

    if phase == "final" or session.filled.get("app_expense_review_ready"):
        if _is_yes(text) or re.search(
            r"\b(continue|next|done|looks? (good|correct|fine))\b", lowered
        ):
            return await _finish_expense_review_next(call_id, session, page, text)

    if not current:
        if not cards:
            cards = await scrape_expense_cards_with_retry(page)
        if not cards:
            speech = (
                "I'm on the household expenses screen, but I can't see the household "
                "member cards yet. Please stay on this page and say try again."
            )
            _set_pending_question(session, "app_expenses", speech)
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "expense_cards_missing",
                "step": "app_expenses",
                "phase": "application",
            }
        speech = await _begin_expense_member(session, page, cards[0], intro=True)
        current = str(session.filled.get("app_expense_current") or "").strip()
        index = int(session.filled.get("app_expense_current_index") or 0)
        picked_early = map_expense_type(text)
        if not picked_early:
            _set_pending_question(session, "app_expenses", speech)
            return _app_ok(
                call_id,
                session,
                text,
                "app_expenses",
                "expense begin member",
                spoken_override=speech,
            )
        phase = "type"
        _set_expense_phase(session, "type")

    def _begin_delete_confirm(expense_type: str) -> dict[str, Any]:
        session.filled["app_expense_manage_action"] = "delete"
        session.filled["app_expense_delete_type"] = expense_type
        for key in (
            "app_expense_pending_type",
            "app_expense_pending_values",
            "app_expense_pending_summary",
            "app_expense_edit_mode",
        ):
            session.filled.pop(key, None)
        _set_expense_phase(session, "delete_confirm")
        speech = _delete_confirm_speech(current, expense_type)
        _set_pending_question(session, "app_expenses_confirm", speech)
        return _app_ok(
            call_id,
            session,
            text,
            "app_expenses_confirm",
            "Confirm expense delete",
            spoken_override=speech,
        )

    async def _apply_picked_expense(picked: str, *, edit_mode: bool = False):
        existing = _member_types_map(session).get(current or "") or []
        override = (picked in existing and picked != "None") or edit_mode
        session.filled["app_expense_edit_mode"] = "1" if edit_mode else "0"
        opened = await _open_edit_or_add(
            page, current, picked, edit_mode=edit_mode
        )
        if not opened.get("ok"):
            speech = (
                f"I couldn't open {picked} for {current or 'this person'}. "
                "Please say the expense type again."
            )
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "expense_open_failed",
                "step": "app_expenses",
                "phase": "application",
            }
        session.filled.pop("app_expense_add_opened", None)
        session.filled["app_expense_pending_type"] = picked
        if picked == "None":
            _record_member_type(session, current, picked)
            session.filled.pop("app_expense_pending_type", None)
            session.filled.pop("app_expense_edit_mode", None)
            refreshed = await scrape_expense_cards(page)
            use_cards = refreshed or cards
            return await _advance_after_expense_member(
                call_id, session, page, text, use_cards, index
            )
        if picked == "Medical Expenses":
            _set_expense_phase(session, "medical")
            speech = _medical_ask_speech(current, session=session)
            if override:
                speech = (
                    f"Replacing the previous Medical Expenses entry for {current}. "
                    + speech
                )
            _set_pending_question(session, "app_expenses_medical", speech)
            return _app_ok(
                call_id,
                session,
                text,
                "app_expenses_medical",
                "Start medical expense",
                spoken_override=speech,
            )
        _set_expense_phase(session, "amount")
        speech = _amount_ask_speech(
            current, picked, override=override, session=session
        )
        _set_pending_question(session, "app_expenses_amount", speech)
        return _app_ok(
            call_id,
            session,
            text,
            "app_expenses_amount",
            "Ask expense amount",
            spoken_override=speech,
        )

    async def _handle_edit_delete() -> dict[str, Any] | None:
        intent = _parse_edit_delete_intent(text)
        if not intent:
            return None
        action, expense_type = intent
        session.filled["app_expense_manage_action"] = action
        if not expense_type:
            speech = (
                f"Which expense should I {action} for {current or 'this person'}? "
                "Say the expense type, like Childcare or Student Loans."
            )
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "expense_manage_clarify",
                "step": step if step.startswith("app_expenses") else "app_expenses",
                "phase": "application",
            }
        if action == "delete":
            return _begin_delete_confirm(expense_type)
        session.filled.pop("app_expense_manage_action", None)
        return await _apply_picked_expense(expense_type, edit_mode=True)

    # ---- Medical Yes/No ----
    if step == "app_expenses_medical" or phase == "medical":
        if _is_yes(text):
            clicked = await _click_medical_yn(page, "Yes")
            if not clicked:
                speech = (
                    "I couldn't select Yes for medical expenses. "
                    + _medical_ask_speech(current, session=session)
                )
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + speech,
                    "error": "expense_medical_yes_failed",
                    "step": "app_expenses_medical",
                    "phase": "application",
                }
            return _begin_confirm(
                call_id,
                session,
                text,
                current,
                "Medical Expenses",
                {"medical_help": "Yes"},
            )
        if _is_no(text):
            clicked = await _click_medical_yn(page, "No")
            if not clicked:
                speech = (
                    "I couldn't select No for medical expenses. "
                    + _medical_ask_speech(current, session=session)
                )
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + speech,
                    "error": "expense_medical_no_failed",
                    "step": "app_expenses_medical",
                    "phase": "application",
                }
            return _begin_confirm(
                call_id,
                session,
                text,
                current,
                "Medical Expenses",
                {"medical_help": "No"},
            )
        speech = _medical_ask_speech(current, session=session)
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "expense_medical_clarify",
            "step": "app_expenses_medical",
            "phase": "application",
        }

    # ---- Confirm / delete confirm ----
    if step == "app_expenses_confirm" or phase in ("confirm", "delete_confirm"):
        del_type = str(session.filled.get("app_expense_delete_type") or "").strip()
        pending_for_save = str(
            session.filled.get("app_expense_pending_type") or ""
        ).strip()
        if phase == "delete_confirm" or (del_type and not pending_for_save):
            if _is_yes(text):
                deleted = await _delete_expense_entry(page, current, del_type)
                if not deleted:
                    speech = (
                        f"I couldn't delete {del_type or 'that expense'} for "
                        f"{current or 'this person'}. Please say delete again with "
                        "the expense type, or say cancel."
                    )
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": "ASK: " + speech,
                        "error": "expense_delete_failed",
                        "step": "app_expenses_confirm",
                        "phase": "application",
                    }
                if del_type and current:
                    mapping = _member_types_map(session)
                    mapping[current] = [
                        t for t in (mapping.get(current) or []) if t != del_type
                    ]
                    _set_member_types_map(session, mapping)
                session.filled.pop("app_expense_delete_type", None)
                session.filled.pop("app_expense_manage_action", None)
                _set_expense_phase(session, "more")
                speech = _more_ask_speech(
                    current, _member_existing_types(session, current)
                )
                _set_pending_question(session, "app_expenses_more", speech)
                return _app_ok(
                    call_id,
                    session,
                    text,
                    "app_expenses_more",
                    "Deleted expense entry",
                    spoken_override=speech,
                )
            if _is_no(text):
                session.filled.pop("app_expense_delete_type", None)
                session.filled.pop("app_expense_manage_action", None)
                _set_expense_phase(session, "more")
                speech = _more_ask_speech(
                    current, _member_existing_types(session, current)
                )
                _set_pending_question(session, "app_expenses_more", speech)
                return _app_ok(
                    call_id,
                    session,
                    text,
                    "app_expenses_more",
                    "Delete cancelled",
                    spoken_override=speech,
                )
            speech = _delete_confirm_speech(current, del_type or "that")
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "expense_delete_clarify",
                "step": "app_expenses_confirm",
                "phase": "application",
            }

        pending = str(session.filled.get("app_expense_pending_type") or "").strip()
        values = _pending_expense_values(session)
        if _is_yes(text):
            return await _save_confirmed_expense(
                call_id, session, page, text, current
            )
        if _is_no(text) or re.search(r"\b(edit|change|correct|fix)\b", lowered):
            await _click_expense_cancel(page)
            for key in (
                "app_expense_pending_summary",
                "app_expense_pending_values",
                "app_expense_edit_mode",
            ):
                session.filled.pop(key, None)
            session.filled.pop("app_expense_pending_type", None)
            _set_expense_phase(session, "type")
            speech = (
                "Okay, I cancelled that expense entry. "
                + _type_ask_for_session(
                    session,
                    current,
                    existing=_member_existing_types(session, current),
                )
            )
            _set_pending_question(session, "app_expenses", speech)
            return _app_ok(
                call_id,
                session,
                text,
                "app_expenses",
                "Expense confirm cancelled",
                spoken_override=speech,
            )
        summary = str(session.filled.get("app_expense_pending_summary") or "").strip()
        speech = summary or _confirm_expense_speech(
            current, pending or "that expense", values
        )
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "expense_confirm_clarify",
            "step": "app_expenses_confirm",
            "phase": "application",
        }

    # ---- Amount ----
    if step == "app_expenses_amount" or phase == "amount":
        amount = _normalize_amount(text)
        pending = str(session.filled.get("app_expense_pending_type") or "").strip()
        if not amount:
            speech = _amount_ask_speech(
                current, pending or "that expense", session=session
            )
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "expense_amount_clarify",
                "step": "app_expenses_amount",
                "phase": "application",
            }
        filled = await _fill_expense_amount(page, amount)
        if not filled:
            speech = (
                "I couldn't enter that amount on the form. "
                + _amount_ask_speech(
                    current, pending or "that expense", session=session
                )
            )
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "expense_amount_not_filled",
                "step": "app_expenses_amount",
                "phase": "application",
            }
        return _begin_confirm(
            call_id,
            session,
            text,
            current,
            pending or "Other",
            {"amount": amount},
        )

    # ---- Manage existing expenses (after declining "add another") ----
    if phase == "manage":
        managed = await _handle_edit_delete()
        if managed is not None:
            return managed
        pending_manage = str(
            session.filled.get("app_expense_manage_action") or ""
        ).strip()
        if pending_manage in ("delete", "edit"):
            picked_m = map_expense_type(text)
            if picked_m and picked_m != "None":
                if pending_manage == "delete":
                    return _begin_delete_confirm(picked_m)
                session.filled.pop("app_expense_manage_action", None)
                return await _apply_picked_expense(picked_m, edit_mode=True)
            speech = (
                f"Which expense should I {pending_manage} for "
                f"{current or 'this person'}? "
                "Say the expense type, like Childcare or Student Loans."
            )
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "expense_manage_type_clarify",
                "step": "app_expenses_more",
                "phase": "application",
            }
        if _is_no(text) or re.search(
            r"\b(continue|next|done|everyone|finished)\b", lowered
        ):
            refreshed = await scrape_expense_cards(page)
            if refreshed:
                cards = refreshed
            return await _advance_after_expense_member(
                call_id, session, page, text, cards, index
            )
        speech = _manage_ask_speech(
            current, _member_existing_types(session, current)
        )
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "expense_manage_clarify",
            "step": "app_expenses_more",
            "phase": "application",
        }

    # ---- More ----
    if step == "app_expenses_more" or phase == "more":
        managed = await _handle_edit_delete()
        if managed is not None:
            return managed
        pending_manage = str(
            session.filled.get("app_expense_manage_action") or ""
        ).strip()
        if pending_manage == "delete":
            picked_del = map_expense_type(text)
            if picked_del and picked_del != "None":
                return _begin_delete_confirm(picked_del)
            speech = (
                f"Which expense should I delete for {current or 'this person'}? "
                "Say the expense type, like Childcare or Student Loans."
            )
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "expense_delete_type_clarify",
                "step": "app_expenses_more",
                "phase": "application",
            }
        residue = _strip_another_expense_filler(text)
        picked_more = map_expense_type(residue) if residue else None
        if not picked_more:
            picked_more = map_expense_type(text)
            if picked_more == "Other" and re.search(r"\banother\b", lowered):
                picked_more = None
        wants_another = bool(
            _is_yes(text) or re.search(r"\banother(\s+expense)?\b", lowered)
        )
        only_yes_or_another = bool(
            wants_another
            and not picked_more
            and not re.search(
                r"\b(student|tuition|alimony|moving|ira|child\s*care|"
                r"medical|other|none)\b",
                lowered,
            )
        )
        if picked_more and not only_yes_or_another:
            if pending_manage == "delete":
                return _begin_delete_confirm(picked_more)
            opened = await _click_add_another_expense(page, current)
            if opened.get("ok"):
                session.filled["app_expense_add_opened"] = "1"
            return await _apply_picked_expense(picked_more, edit_mode=False)
        if wants_another or only_yes_or_another:
            opened = await _click_add_another_expense(page, current)
            if not opened.get("ok"):
                speech = (
                    f"I couldn't click Add Another Expense for "
                    f"{current or 'this person'}. Please tap Add Another Expense, "
                    "then say the expense type, or say try again."
                )
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + speech,
                    "error": "expense_add_another_failed",
                    "step": "app_expenses_more",
                    "phase": "application",
                }
            session.filled["app_expense_add_opened"] = "1"
            session.filled.pop("app_expense_pending_type", None)
            _set_expense_phase(session, "type")
            speech = (
                f"Okay, adding another expense for {current or 'this person'}. "
                "What expense type should I enter?"
            )
            _set_pending_question(session, "app_expenses", speech)
            return _app_ok(
                call_id,
                session,
                text,
                "app_expenses",
                "expense add another",
                spoken_override=speech,
            )
        if _is_no(text) or re.search(
            r"\b(continue|next|done|everyone|finished)\b", lowered
        ):
            _set_expense_phase(session, "manage")
            speech = _manage_ask_speech(
                current, _member_existing_types(session, current)
            )
            _set_pending_question(session, "app_expenses_more", speech)
            return _app_ok(
                call_id,
                session,
                text,
                "app_expenses_more",
                "expense manage ask",
                spoken_override=speech,
            )
        speech = _more_ask_speech(
            current, _member_existing_types(session, current)
        )
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "expense_more_clarify",
            "step": "app_expenses_more",
            "phase": "application",
        }

    # ---- Type selection ----
    managed = await _handle_edit_delete()
    if managed is not None:
        return managed
    if str(session.filled.get("app_expense_manage_action") or "") == "delete":
        picked_del = map_expense_type(text)
        if picked_del and picked_del != "None":
            return _begin_delete_confirm(picked_del)

    if re.search(r"\b(continue|next|done|everyone|finished)\b", lowered) and not map_expense_type(
        text
    ):
        heard = set(_expense_heard(session))
        if current and index not in heard:
            types = _member_types_map(session).get(current) or []
            if types:
                return await _advance_after_expense_member(
                    call_id, session, page, text, cards, index
                )
        if cards and len(heard) >= len(cards):
            return await _finish_expense_review_next(call_id, session, page, text)

    residue = _strip_another_expense_filler(text)
    picked = map_expense_type(residue) if residue else None
    if not picked:
        picked = map_expense_type(text)
    logger.info(
        "expense_type_map raw=%r residue=%r picked=%r member=%r",
        text,
        residue,
        picked,
        current,
    )
    if not picked:
        speech = (
            f"I didn't catch an expense type for {current or 'this person'}. "
            "Please say the expense type — for example Student Loans or Medical "
            "Expenses — or ask me for the options."
        )
        if re.search(r"\boptions?\b|\blist\b|\bchoices?\b", lowered):
            speech = _type_ask_for_session(
                session,
                current,
                existing=_member_existing_types(session, current),
                force_options=True,
            )
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "expense_clarify",
            "step": "app_expenses",
            "phase": "application",
        }
    return await _apply_picked_expense(picked, edit_mode=False)
