"""Pointcare Tax Information (Step 8) per-member review workflow.

Mirrors marital / special-condition style: scrape saved values, confirm or
change each field in order, Save and Return, then walk every household card
before clicking Next.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from playwright.async_api import Page

logger = logging.getLogger(__name__)

_TAX_STATUS_LABELS = (
    "Single",
    "Married filing jointly",
    "Married filing separately",
    "Head of Household",
    "Qualifying Widow(er) with Dependent Child",
    "Not filing",
)

_TAX_PHASES = (
    "status",
    "claim_deps",
    "claim_deps_who",
    "claimed_as",
    "claimed_as_who",
    "saving",
    "final",
)

_TAX_FILLED_KEYS = (
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
)


def _tax_dom_js() -> str:
    """DOM helpers for Tax Information review cards and edit modal."""
    return r"""
      const taxNorm = (s) => (s || '').replace(/\s+/g, ' ').trim();
      const taxLow = (s) => taxNorm(s).toLowerCase();
      const taxVisible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const st = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' &&
          st.display !== 'none' && st.opacity !== '0';
      };
      const taxStatusDisplay = [
        'Single',
        'Married filing jointly',
        'Married filing separately',
        'Head of Household',
        'Qualifying Widow(er) with Dependent Child',
        'Not filing',
      ];
      const taxStatusLabels = taxStatusDisplay.map((s) => taxLow(s)).concat([
        'qualifying widow with dependent child',
      ]);
      const taxCanonicalStatus = (t) => {
        const n = taxLow(t);
        if (!n) return '';
        for (const label of taxStatusDisplay) {
          const low = taxLow(label);
          if (n === low || n.startsWith(low)) return label;
        }
        if (/^qualifying widow/.test(n)) {
          return 'Qualifying Widow(er) with Dependent Child';
        }
        return '';
      };
      const taxIsStatus = (t) => Boolean(taxCanonicalStatus(t));
      const taxIsPerson = (line) => {
        const t = taxNorm(line).replace(/\s*\(you\)\s*/ig, ' ').trim();
        if (!t || t.length > 60) return false;
        if (taxIsStatus(t)) return false;
        if (/who\?|dependents?|filing|tax status|edit|save|cancel|next|back|continue|claim/i.test(t)) {
          return false;
        }
        if (/^(yes|no)$/i.test(t)) return false;
        return /^[A-Za-z][A-Za-z .'\-]{0,40}$/.test(t) && t.split(' ').length <= 4;
      };
      const taxReadOn = (el) => {
        if (!el) return false;
        if (el.checked === true) return true;
        const aria = (el.getAttribute('aria-checked') || '').toLowerCase();
        if (aria === 'true') return true;
        const cls = (el.className || '').toString().toLowerCase();
        return /\b(checked|selected|active|pressed)\b/.test(cls);
      };
      const taxLinesOf = (raw) => (raw || '')
        .split(/\r?\n/)
        .map(taxNorm)
        .filter(Boolean);
      const taxLabelOf = (ctrl) => {
        let raw =
          ctrl.getAttribute('aria-label') || ctrl.innerText || ctrl.textContent || '';
        const lab = ctrl.id
          ? document.querySelector(`label[for="${CSS.escape(ctrl.id)}"]`)
          : ctrl.closest('label');
        if (lab) raw = lab.innerText || lab.textContent || raw;
        const lines = taxLinesOf(raw);
        return lines[0] || taxNorm(raw);
      };
      const taxStatusLinesIn = (el) => {
        if (!el) return [];
        const found = [];
        for (const line of taxLinesOf(el.innerText || el.textContent || '')) {
          const canon = taxCanonicalStatus(line);
          if (canon && !found.includes(canon)) found.push(canon);
        }
        return found;
      };
      // Marital-style parent/sibling discovery for Pointcare custom radios.
      const taxRadioLabel = (radio) => {
        if (!radio) return '';
        const own = taxCanonicalStatus(
          (radio.getAttribute('aria-label') || '') + ' ' +
          (radio.innerText || radio.textContent || '')
        );
        if (own) return own;
        if (radio.id) {
          const lab = document.querySelector(
            'label[for="' + CSS.escape(radio.id) + '"]'
          );
          if (lab) {
            const statuses = taxStatusLinesIn(lab);
            if (statuses.length === 1) return statuses[0];
          }
        }
        const wrap = radio.closest('label');
        if (wrap) {
          const statuses = taxStatusLinesIn(wrap);
          if (statuses.length === 1) return statuses[0];
        }
        let cur = radio.parentElement;
        for (let depth = 0; cur && depth < 5; depth += 1, cur = cur.parentElement) {
          const statuses = taxStatusLinesIn(cur);
          if (statuses.length === 1) return statuses[0];
          if (statuses.length > 1) break;
        }
        const row = radio.parentElement;
        if (row) {
          for (const child of row.children || []) {
            if (child === radio) continue;
            const statuses = taxStatusLinesIn(child);
            if (statuses.length === 1) return statuses[0];
          }
          let sib = radio.nextElementSibling;
          for (let i = 0; sib && i < 3; i += 1, sib = sib.nextElementSibling) {
            const statuses = taxStatusLinesIn(sib);
            if (statuses.length === 1) return statuses[0];
          }
          sib = radio.previousElementSibling;
          for (let i = 0; sib && i < 3; i += 1, sib = sib.previousElementSibling) {
            const statuses = taxStatusLinesIn(sib);
            if (statuses.length === 1) return statuses[0];
          }
        }
        return '';
      };
      const taxStatusRoot = () => {
        const nodes = [...document.querySelectorAll(
          'section, article, fieldset, li, div'
        )];
        let best = null;
        let bestLen = Infinity;
        for (const el of nodes) {
          if (!taxVisible(el)) continue;
          const raw = el.innerText || '';
          const t = taxLow(raw);
          if (!t.includes('tax status')) continue;
          if (t.length > 2200) continue;
          const statusHits = taxStatusDisplay.filter((lab) => t.includes(taxLow(lab)));
          if (statusHits.length < 3) continue;
          if (t.length < bestLen) {
            best = el;
            bestLen = t.length;
          }
        }
        return best;
      };
      const taxStatusRadios = (root) => {
        const scope = root || document;
        return [...scope.querySelectorAll('input[type=radio], [role=radio]')]
          .filter(taxVisible)
          .filter((el) => Boolean(taxRadioLabel(el)));
      };
      const taxParseYn = (t) => {
        const n = taxLow(t);
        if (n === 'yes' || n.startsWith('yes ')) return 'Yes';
        if (n === 'no' || n.startsWith('no ')) return 'No';
        return '';
      };
      const taxYnLinesIn = (el) => {
        if (!el) return [];
        const found = [];
        for (const line of taxLinesOf(el.innerText || el.textContent || '')) {
          const yn = taxParseYn(line);
          if (yn && !found.includes(yn)) found.push(yn);
        }
        return found;
      };
      // Parent/sibling discovery for Pointcare custom Yes/No radios.
      const taxYnLabel = (ctrl) => {
        if (!ctrl) return '';
        const own = taxParseYn(
          (ctrl.getAttribute('aria-label') || '') + ' ' +
          (ctrl.innerText || ctrl.textContent || '')
        );
        if (own) return own;
        if (ctrl.id) {
          const lab = document.querySelector(
            'label[for="' + CSS.escape(ctrl.id) + '"]'
          );
          if (lab) {
            const yns = taxYnLinesIn(lab);
            if (yns.length === 1) return yns[0];
          }
        }
        const wrap = ctrl.closest('label');
        if (wrap) {
          const yns = taxYnLinesIn(wrap);
          if (yns.length === 1) return yns[0];
        }
        let cur = ctrl.parentElement;
        for (let depth = 0; cur && depth < 5; depth += 1, cur = cur.parentElement) {
          const yns = taxYnLinesIn(cur);
          if (yns.length === 1) return yns[0];
          if (yns.length > 1) break;
        }
        const row = ctrl.parentElement;
        if (row) {
          for (const child of row.children || []) {
            if (child === ctrl) continue;
            const yns = taxYnLinesIn(child);
            if (yns.length === 1) return yns[0];
          }
          let sib = ctrl.nextElementSibling;
          for (let i = 0; sib && i < 3; i += 1, sib = sib.nextElementSibling) {
            const yns = taxYnLinesIn(sib);
            if (yns.length === 1) return yns[0];
          }
          sib = ctrl.previousElementSibling;
          for (let i = 0; sib && i < 3; i += 1, sib = sib.previousElementSibling) {
            const yns = taxYnLinesIn(sib);
            if (yns.length === 1) return yns[0];
          }
        }
        return '';
      };
      const taxQuestionRoot = (needle) => {
        const want = taxLow(needle);
        const nodes = [...document.querySelectorAll(
          'section, article, fieldset, li, div'
        )];
        let best = null;
        let bestLen = Infinity;
        for (const el of nodes) {
          if (!taxVisible(el)) continue;
          const t = taxLow(el.innerText || '');
          if (!t || t.length > 2500) continue;
          if (!t.includes(want)) continue;
          // Prefer the smallest container that still holds Yes/No controls.
          // Use taxYnLabel so sibling-text radios still count.
          const hasYn = [...el.querySelectorAll(
            'input[type=radio], [role=radio], input[type=checkbox], [role=checkbox], label, button'
          )].some((c) => Boolean(taxYnLabel(c)));
          if (!hasYn && !/who\?/.test(t)) continue;
          if (t.length < bestLen) {
            best = el;
            bestLen = t.length;
          }
        }
        return best;
      };
      const taxWhoIn = (root) => {
        if (!root) return [];
        const out = [];
        const seen = {};
        for (const box of root.querySelectorAll(
          'input[type=checkbox], [role=checkbox]'
        )) {
          if (!taxVisible(box)) continue;
          let name = taxLabelOf(box);
          if (!taxIsPerson(name)) {
            // Empty role=checkbox beside a name leaf (special/circumstances pattern).
            let cur = box.parentElement;
            for (let d = 0; d < 6 && cur; d++) {
              const leaves = [...cur.querySelectorAll('div, span, p, label')]
                .map((n) => taxNorm(n.innerText || n.textContent || ''))
                .filter(taxIsPerson);
              const uniq = [...new Set(leaves)];
              if (uniq.length === 1) { name = uniq[0]; break; }
              cur = cur.parentElement;
            }
          }
          if (!taxIsPerson(name)) continue;
          const key = taxLow(name);
          const dup = seen[key] || 0;
          seen[key] = dup + 1;
          out.push({
            label: name,
            checked: taxReadOn(box),
            index: out.length,
            dupIndex: dup,
          });
        }
        return out;
      };
      const taxYnControls = (root) => {
        if (!root) return [];
        return [...root.querySelectorAll(
          'input[type=radio], [role=radio], button, label'
        )].filter(taxVisible).filter((el) => Boolean(taxYnLabel(el)));
      };
      const taxYnIn = (root) => {
        if (!root) return '';
        for (const el of taxYnControls(root)) {
          if (!taxReadOn(el) && !(el.tagName === 'INPUT' && el.checked)) continue;
          return taxYnLabel(el);
        }
        return '';
      };
      const taxClickYn = (root, value) => {
        const want = taxLow(value);
        if (!root || !want) {
          return { ok: false, stage: 'discovery', reason: 'missing_root_or_value' };
        }
        try { root.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
        const controls = taxYnControls(root);
        const discovered = controls.map((el) => ({
          label: taxYnLabel(el),
          on: !!(taxReadOn(el) || (el.tagName === 'INPUT' && el.checked)),
        }));
        const hits = controls.filter((el) => taxLow(taxYnLabel(el)) === want);
        if (hits.length < 1) {
          // Fallback: compact leaf whose sole text is Yes/No.
          let best = null;
          let bestLen = Infinity;
          for (const el of root.querySelectorAll(
            'button, [role=button], label, div, span, p, li'
          )) {
            if (!taxVisible(el)) continue;
            const lines = taxLinesOf(el.innerText || el.textContent || '');
            if (lines.length !== 1) continue;
            if (taxLow(taxParseYn(lines[0])) !== want) continue;
            const len = taxNorm(el.innerText || '').length;
            if (len < bestLen) {
              best = el;
              bestLen = len;
            }
          }
          if (!best) {
            return {
              ok: false,
              stage: 'discovery',
              reason: 'yn_not_found',
              want,
              discovered,
            };
          }
          try { best.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
          const rowRadio = (best.closest('label') || best.parentElement || best)
            .querySelector('input[type=radio], [role=radio]');
          const clickEl = rowRadio || best;
          clickEl.click();
          return {
            ok: true,
            stage: 'clicked_fallback',
            selected: value,
            want,
            discovered,
          };
        }
        const target = hits[0];
        if (taxReadOn(target) || (target.tagName === 'INPUT' && target.checked)) {
          return {
            ok: true,
            stage: 'already_selected',
            selected: taxYnLabel(target),
            want,
            discovered,
          };
        }
        try { target.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
        const clickEl = target.closest('label') || target;
        clickEl.click();
        return {
          ok: true,
          stage: 'clicked',
          selected: taxYnLabel(target),
          want,
          discovered,
        };
      };
      const taxModalOpen = () => {
        // Prefer a real dialog/overlay — never treat the list page body text alone
        // as an open editor (that left Save/advance stuck and re-asked claimed_as).
        const dialogs = [...document.querySelectorAll(
          '[role=dialog], [aria-modal=true], dialog, .modal, .MuiDialog-root, .ReactModal__Content'
        )].filter(taxVisible);
        for (const dlg of dialogs) {
          const t = taxLow(dlg.innerText || '');
          if (!t) continue;
          if (
            /will this person claim any dependents/.test(t) ||
            /will anyone claim this person as a dependent/.test(t) ||
            /review and update this person/.test(t) ||
            (/tax information/.test(t) && /tax status|filing status|save and return|save & return/.test(t))
          ) {
            return true;
          }
        }
        // Fallback: compact editor root (not the full page).
        const nodes = [...document.querySelectorAll('section, article, form, div')]
          .filter(taxVisible);
        let best = null;
        let bestLen = Infinity;
        for (const el of nodes) {
          const t = taxLow(el.innerText || '');
          if (!t || t.length > 3500) continue;
          const hasQs =
            /will this person claim any dependents/.test(t) ||
            /will anyone claim this person as a dependent/.test(t);
          const hasSave = /save and return|save & return/.test(t) ||
            (/\bsave\b/.test(t) && /\bcancel\b/.test(t));
          if (!(hasQs && hasSave)) continue;
          if (t.length < bestLen) {
            best = el;
            bestLen = t.length;
          }
        }
        return Boolean(best);
      };
      const taxModalAncestor = (el) => {
        if (!el || !el.closest) return null;
        return el.closest(
          '[role=dialog], [aria-modal=true], dialog, .modal, .MuiDialog-root, .ReactModal__Content'
        );
      };
      const taxIsEditControl = (el) => {
        if (!el) return false;
        const aria = taxLow(el.getAttribute('aria-label') || '');
        const title = taxLow(el.getAttribute('title') || '');
        const txt = taxLow(el.innerText || el.textContent || '');
        const combined = aria || title || txt;
        if (!combined || combined.length > 48) return false;
        if (combined === 'edit' || combined === 'edit >' || combined === 'edit›') {
          return true;
        }
        return /^edit(\b|\s|>|›)/.test(combined);
      };
      const taxVisibleEditControls = () => [...document.querySelectorAll(
        'button, [role=button], a, [class*="edit"], [class*="Edit"]'
      )].filter(taxVisible)
        .filter(taxIsEditControl)
        .filter((el) => !taxModalAncestor(el));
      const taxLinesFromEl = (el) => {
        // Prefer newline-split innerText; also collect short child leaves so
        // nested Pointcare cards still yield name / status / dependents lines.
        const raw = el.innerText || '';
        const lines = taxLinesOf(raw);
        const seen = {};
        const out = [];
        const push = (s) => {
          const n = taxNorm(s);
          if (!n || n.length > 80) return;
          const key = taxLow(n);
          if (seen[key]) return;
          seen[key] = 1;
          out.push(n);
        };
        for (const line of lines) push(line);
        for (const child of el.querySelectorAll('div, span, p, li, h1, h2, h3, h4, label')) {
          if (!taxVisible(child)) continue;
          const t = taxNorm(child.innerText || child.textContent || '');
          if (!t || t.length > 80) continue;
          // Skip containers that clearly hold multiple logical lines.
          if ((child.children || []).length > 3 && t.length > 40) continue;
          push(t);
        }
        return out;
      };
      const taxExtractCardFields = (el) => {
        const lines = taxLinesFromEl(el);
        const joined = lines.join(' ');
        const low = taxLow(joined);
        const personLines = lines.filter(taxIsPerson);
        let name = '';
        if (personLines.length === 1) {
          name = personLines[0];
        } else if (personLines.length > 1) {
          // Prefer the first person line that is not listed after Dependents / Dependent of.
          const depIdx = lines.findIndex((ln) => /^dependents?(?:\s+of)?:/i.test(ln));
          const beforeDep = personLines.filter((p) => {
            const idx = lines.indexOf(p);
            return depIdx < 0 || idx < depIdx;
          });
          name = (beforeDep[0] || personLines[0] || '');
        }
        let status = '';
        for (const line of lines) {
          const canon = taxCanonicalStatus(line);
          if (canon) { status = canon; break; }
        }
        if (!status) {
          for (const label of taxStatusDisplay) {
            if (low.includes(taxLow(label))) { status = label; break; }
          }
        }
        let dependents = '';
        let dependent_of = '';
        for (const line of lines) {
          const ofMatch = line.match(/^Dependent of:\s*(.*)$/i);
          if (ofMatch) {
            dependent_of = taxNorm(ofMatch[1] || '');
            continue;
          }
          const depMatch = line.match(/^Dependents?:\s*(.*)$/i);
          if (depMatch) {
            dependents = taxNorm(depMatch[1] || '');
          }
        }
        return { name, status, dependents, dependent_of, lines, joined, low };
      };
      const taxCardFromEdit = (editBtn) => {
        // Collect one-edit ancestors, then prefer a complete card (status / deps)
        // over a tiny name+Edit chip.
        const candidates = [];
        let cur = editBtn.parentElement;
        for (let depth = 0; cur && depth < 14; depth += 1, cur = cur.parentElement) {
          if (!taxVisible(cur)) continue;
          if (taxModalAncestor(cur)) break;
          const r = cur.getBoundingClientRect();
          const width = Math.round(r.width);
          const height = Math.round(r.height);
          if (width < 80 || height < 20 || height > 900 || width > 1400) continue;
          const editsIn = [...cur.querySelectorAll(
            'button, [role=button], a'
          )].filter(taxVisible).filter(taxIsEditControl);
          // Exactly one Edit — reject list wrappers that hold every member.
          if (editsIn.length !== 1) continue;
          const fields = taxExtractCardFields(cur);
          if (!fields.name) continue;
          const hasDetail = !!(
            fields.status || fields.dependents || fields.dependent_of
          );
          candidates.push({
            name: fields.name,
            status: fields.status,
            dependents: fields.dependents,
            dependent_of: fields.dependent_of,
            summary: fields.joined.slice(0, 220),
            lines: fields.lines.slice(0, 12),
            area: width * height,
            top: r.top,
            left: r.left,
            width,
            height,
            hasDetail,
            el: cur,
          });
        }
        if (!candidates.length) return null;
        const detailed = candidates.filter((c) => c.hasDetail);
        const pool = detailed.length ? detailed : candidates;
        pool.sort((a, b) => a.area - b.area);
        return pool[0];
      };
      const taxParseCard = (el) => {
        // Legacy node walk — used as fallback when Edit-anchor misses.
        const debug = {
          reason: '',
          lines: [],
          name: '',
          status: '',
          dependents: '',
          width: 0,
          height: 0,
        };
        if (!taxVisible(el)) {
          debug.reason = 'not_visible';
          return { ok: false, debug };
        }
        if (taxModalAncestor(el)) {
          debug.reason = 'inside_modal';
          return { ok: false, debug };
        }
        const r = el.getBoundingClientRect();
        debug.width = Math.round(r.width);
        debug.height = Math.round(r.height);
        if (r.width < 80 || r.height < 20 || r.height > 900) {
          debug.reason = 'size_filtered';
          return { ok: false, debug };
        }
        const fields = taxExtractCardFields(el);
        debug.lines = fields.lines.slice(0, 12);
        if (!fields.lines.length) {
          debug.reason = 'no_lines';
          return { ok: false, debug };
        }
        if (fields.joined.length > 800) {
          debug.reason = 'text_too_long';
          return { ok: false, debug };
        }
        const edits = [...el.querySelectorAll(
          'button, [role=button], a'
        )].filter(taxVisible).filter(taxIsEditControl);
        if (edits.length !== 1 && !/(?:^|\s)edit(?:\s|>|$)/i.test(fields.joined)) {
          debug.reason = edits.length === 0 ? 'no_edit_control' : 'multiple_edit_controls';
          return { ok: false, debug };
        }
        if (!fields.name) {
          debug.reason = 'no_person_line';
          return { ok: false, debug };
        }
        debug.name = fields.name;
        debug.status = fields.status;
        debug.dependents = fields.dependents || fields.dependent_of;
        // Name + Edit is enough; status / dependents are optional.
        debug.reason = 'accepted';
        return {
          ok: true,
          debug,
          card: {
            name: fields.name,
            status: fields.status,
            dependents: fields.dependents,
            dependent_of: fields.dependent_of,
            summary: fields.joined.slice(0, 220),
            area: r.width * r.height,
            top: r.top,
            left: r.left,
          },
          el,
        };
      };
      const taxCardsDetailed = () => {
        const out = [];
        const rejects = [];
        const seen = {};
        const editBtns = taxVisibleEditControls();
        const accepted = [];
        for (const btn of editBtns) {
          const card = taxCardFromEdit(btn);
          if (!card) {
            rejects.push({
              reason: 'edit_no_container',
              name: '',
              status: '',
              dependents: '',
              lines: taxLinesOf(btn.parentElement ? (btn.parentElement.innerText || '') : '').slice(0, 8),
              width: 0,
              height: 0,
            });
            continue;
          }
          accepted.push({
            card: {
              name: card.name,
              status: card.status,
              dependents: card.dependents,
              dependent_of: card.dependent_of,
              summary: card.summary,
              area: card.area,
              top: card.top,
              left: card.left,
            },
            el: card.el,
            debug: {
              reason: 'accepted_edit_anchor',
              name: card.name,
              status: card.status,
              dependents: card.dependents,
              dependent_of: card.dependent_of,
              lines: card.lines,
              width: card.width,
              height: card.height,
            },
          });
        }
        // Fallback: scan common containers when Edit-anchor found nothing.
        if (!accepted.length) {
          const nodes = [...document.querySelectorAll(
            '[class*="card"], [class*="Card"], section, article, li, div'
          )];
          for (const el of nodes) {
            const parsed = taxParseCard(el);
            if (!parsed.ok) {
              if (parsed.debug && parsed.debug.lines && parsed.debug.lines.length &&
                  ['no_person_line', 'multiple_person_lines', 'no_edit_control',
                   'multiple_edit_controls', 'no_tax_status_cue', 'inside_modal']
                    .includes(parsed.debug.reason)) {
                rejects.push(parsed.debug);
              }
              continue;
            }
            accepted.push(parsed);
          }
        }
        accepted.sort((a, b) => {
          const dy = (a.card.top - b.card.top);
          if (Math.abs(dy) > 8) return dy;
          return a.card.left - b.card.left;
        });
        for (const parsed of accepted) {
          const key = taxLow(parsed.card.name);
          const prev = seen[key];
          if (prev != null) {
            if (parsed.card.area < out[prev].area) {
              out[prev] = {
                index: prev,
                name: parsed.card.name,
                status: parsed.card.status,
                dependents: parsed.card.dependents,
                dependent_of: parsed.card.dependent_of || '',
                summary: parsed.card.summary,
                area: parsed.card.area,
              };
            }
            continue;
          }
          seen[key] = out.length;
          out.push({
            index: out.length,
            name: parsed.card.name,
            status: parsed.card.status,
            dependents: parsed.card.dependents,
            dependent_of: parsed.card.dependent_of || '',
            summary: parsed.card.summary,
            area: parsed.card.area,
          });
        }
        for (const row of out) delete row.area;
        return {
          cards: out,
          rejects: rejects.slice(0, 24),
          edit_count: editBtns.length,
          candidate_count: accepted.length,
        };
      };
      const taxCards = () => taxCardsDetailed().cards;
      const taxFindCardEl = (memberName) => {
        const want = taxLow(memberName);
        if (!want) return null;
        const detailed = taxCardsDetailed();
        for (const btn of taxVisibleEditControls()) {
          const card = taxCardFromEdit(btn);
          if (card && taxLow(card.name) === want) return card.el;
        }
        const nodes = [...document.querySelectorAll(
          '[class*="card"], [class*="Card"], section, article, li, div'
        )];
        let best = null;
        let bestArea = Infinity;
        for (const el of nodes) {
          const parsed = taxParseCard(el);
          if (!parsed.ok) continue;
          if (taxLow(parsed.card.name) !== want) continue;
          if (parsed.card.area < bestArea) {
            bestArea = parsed.card.area;
            best = el;
          }
        }
        return best;
      };
      const taxNormBtnLabel = (s) => taxLow(s)
        .replace(/&/g, ' and ')
        .replace(/[›>]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
      const taxIsSaveReturnLabel = (s) => {
        const n = taxNormBtnLabel(s);
        return n === 'save and return' || n.startsWith('save and return ');
      };
      const taxIsBareSaveLabel = (s) => taxNormBtnLabel(s) === 'save';
      const taxModalRoots = () => {
        const dialogs = [...document.querySelectorAll(
          '[role=dialog], [aria-modal=true], dialog, .modal, .MuiDialog-root, .ReactModal__Content'
        )].filter(taxVisible);
        return dialogs.length ? dialogs : [];
      };
      const taxControlLabel = (el) => taxNorm(
        el.getAttribute('aria-label') ||
        el.getAttribute('title') ||
        el.value ||
        el.innerText ||
        el.textContent ||
        ''
      );
      const taxEditorScopes = () => {
        const scopes = [];
        for (const root of taxModalRoots()) {
          const t = taxLow(root.innerText || '');
          const looksTax = (
            /will this person claim any dependents/.test(t) ||
            /will anyone claim this person as a dependent/.test(t) ||
            /review and update this person/.test(t) ||
            (/tax information/.test(t) && /tax status|filing status|save/.test(t))
          );
          if (looksTax) scopes.push(root);
        }
        if (scopes.length) return scopes;
        // Compact editor fallback (same cues as taxModalOpen).
        const nodes = [...document.querySelectorAll('section, article, form, div')]
          .filter(taxVisible);
        let best = null;
        let bestLen = Infinity;
        for (const el of nodes) {
          const t = taxLow(el.innerText || '');
          if (!t || t.length > 3500) continue;
          const hasQs =
            /will this person claim any dependents/.test(t) ||
            /will anyone claim this person as a dependent/.test(t);
          const hasSave = /save and return|save & return/.test(t) ||
            (/\bsave\b/.test(t) && /\bcancel\b/.test(t));
          if (!(hasQs && hasSave)) continue;
          if (t.length < bestLen) {
            best = el;
            bestLen = t.length;
          }
        }
        return best ? [best] : [];
      };
      const taxListModalButtons = () => {
        // Only search inside the tax editor — never the full page summary.
        const out = [];
        for (const root of taxEditorScopes()) {
          for (const el of root.querySelectorAll(
            'button, [role=button], a, input[type=submit]'
          )) {
            if (!taxVisible(el)) continue;
            out.push({ label: taxControlLabel(el), el });
          }
        }
        return out;
      };
      const taxFindSaveControl = () => {
        const buttons = taxListModalButtons();
        const labels = buttons.map((b) => b.label).filter(Boolean).slice(0, 12);
        const saveReturn = buttons.find((b) => taxIsSaveReturnLabel(b.label));
        if (saveReturn) {
          return {
            ok: true,
            kind: 'save_and_return',
            label: saveReturn.label,
            labels,
            el: saveReturn.el,
          };
        }
        const bare = buttons.find((b) => taxIsBareSaveLabel(b.label));
        if (bare) {
          return {
            ok: true,
            kind: 'save',
            label: bare.label,
            labels,
            el: bare.el,
          };
        }
        return { ok: false, kind: '', label: '', labels, el: null };
      };
      const taxClickSaveReturnOnce = () => {
        const found = taxFindSaveControl();
        if (!found.ok || !found.el) {
          return {
            ok: false,
            reason: 'save_button_missing',
            kind: '',
            label: '',
            labels: found.labels || [],
          };
        }
        try {
          found.el.scrollIntoView({ block: 'center', inline: 'nearest' });
        } catch (e) {}
        found.el.click();
        return {
          ok: true,
          reason: 'clicked',
          kind: found.kind,
          label: found.label,
          labels: found.labels || [],
        };
      };
      const taxValidationErrorInModal = () => {
        const roots = taxModalRoots();
        for (const root of roots) {
          const t = taxLow(root.innerText || '');
          if (!t) continue;
          if (
            /required|please (fix|select|choose|enter)|invalid|must (select|enter|provide)|error/.test(t)
            && (/will this person claim|will anyone claim|tax status|save and return|save & return/.test(t))
          ) {
            // Prefer compact alert/error nodes when present.
            const alert = root.querySelector('[role=alert], .error, .Mui-error, [class*="error"]');
            if (alert && taxVisible(alert)) {
              return taxNorm(alert.innerText || alert.textContent || '').slice(0, 160);
            }
            return 'validation_or_required_fields';
          }
        }
        return '';
      };
      const taxSummaryAfterSave = () => {
        const modalOpen = taxModalOpen();
        const cards = taxCardsDetailed();
        const headingEls = [...document.querySelectorAll(
          'h1, h2, h3, [role=heading]'
        )].filter(taxVisible);
        const headings = headingEls
          .map((el) => taxNorm(el.innerText || el.textContent || ''))
          .filter(Boolean);
        const headingLow = taxLow(headings.join(' | '));
        const hasTaxHeading = /tax information/.test(headingLow);
        const editCount = taxVisibleEditControls().length;
        const validation = modalOpen ? taxValidationErrorInModal() : '';
        const summaryVisible = (
          !modalOpen
          && hasTaxHeading
          && (cards.cards.length >= 1 || editCount >= 1)
        );
        return {
          modal_open: modalOpen,
          summary_visible: summaryVisible,
          card_count: cards.cards.length,
          card_names: cards.cards.map((c) => c.name),
          edit_count: editCount,
          heading: headings[0] || '',
          validation_error: validation,
        };
      };
      const taxProbeScreen = () => {
        const headingEls = [...document.querySelectorAll(
          'h1, h2, h3, [role=heading]'
        )].filter(taxVisible);
        const headings = headingEls
          .map((el) => taxNorm(el.innerText || el.textContent || ''))
          .filter(Boolean);
        const headingJoined = taxLow(headings.join(' | '));
        const hasTaxHeading = /tax information|tax details|\btax status\b/.test(headingJoined)
          || headings.some((h) => /^tax\b/i.test(h));
        // Visible body text only (innerText skips display:none / visibility:hidden).
        const bodyVisible = taxNorm(document.body ? (document.body.innerText || '') : '');
        const bodyLow = taxLow(bodyVisible);
        const editCount = taxVisibleEditControls().length;
        const statusHits = taxStatusDisplay.filter((s) => bodyLow.includes(taxLow(s)));
        const loading = (
          bodyVisible.length < 40
          || (/loading|please wait/.test(bodyLow) && bodyVisible.length < 120)
        );
        const hasTaxPhrase = /tax information/.test(bodyLow) || /filing status/.test(bodyLow);
        const hasTaxAndDep = /\btax\b/.test(bodyLow) && /dependent/.test(bodyLow);
        const isTax = !!(
          hasTaxHeading
          || (hasTaxPhrase && (editCount >= 1 || statusHits.length >= 1))
          || (editCount >= 1 && statusHits.length >= 1)
          || /filing status/.test(bodyLow)
          || (hasTaxAndDep && (editCount >= 1 || statusHits.length >= 1 || hasTaxHeading))
        );
        const circumstancesVisible = !isTax && (
          /other statuses/.test(bodyLow)
          || /daily activities/.test(bodyLow)
          || /alaska native/.test(bodyLow)
          || /american indian/.test(bodyLow)
          || (/veteran/.test(bodyLow) && (/student 18/.test(bodyLow) || /military/.test(bodyLow)))
        );
        return {
          is_tax: isTax && !loading,
          loading: loading && !isTax,
          heading: headings[0] || '',
          headings: headings.slice(0, 6),
          edit_count: editCount,
          status_hits: statusHits.slice(0, 8),
          circumstances_visible: circumstancesVisible,
          modal_open: taxModalOpen(),
          snippet: bodyVisible.slice(0, 200),
          reason: isTax
            ? (hasTaxHeading ? 'tax_heading' : (editCount ? 'edit_and_status' : 'tax_phrase'))
            : (loading ? 'loading' : (circumstancesVisible ? 'circumstances' : 'no_tax_signal')),
        };
      };
    """


async def probe_tax_information_screen(page: Page) -> dict[str, Any]:
    """Visible-DOM probe for Tax Information (ignores hidden stale nodes)."""
    try:
        raw = await page.evaluate(
            "() => {" + _tax_dom_js() + " return taxProbeScreen(); }"
        )
        if isinstance(raw, dict):
            return raw
    except Exception:
        logger.exception("probe_tax_information_screen failed")
    return {
        "is_tax": False,
        "loading": False,
        "heading": "",
        "edit_count": 0,
        "status_hits": [],
        "reason": "probe_failed",
        "snippet": "",
    }


async def _scrape_tax_cards(page: Page) -> list[dict[str, Any]]:
    """Read Tax Information review cards in visual order."""
    try:
        raw = await page.evaluate(
            "() => {" + _tax_dom_js() + " return taxCardsDetailed(); }"
        )
        if isinstance(raw, list):
            rows = raw
            rejects: list[Any] = []
            edit_count = 0
            candidate_count = len(rows)
        elif isinstance(raw, dict):
            rows = raw.get("cards") if isinstance(raw.get("cards"), list) else []
            rejects = raw.get("rejects") if isinstance(raw.get("rejects"), list) else []
            edit_count = int(raw.get("edit_count") or 0)
            candidate_count = int(raw.get("candidate_count") or len(rows))
        else:
            rows, rejects, edit_count, candidate_count = [], [], 0, 0
        names = [str(r.get("name") or "") for r in rows if isinstance(r, dict)]
        statuses = [str(r.get("status") or "") for r in rows if isinstance(r, dict)]
        deps = [str(r.get("dependents") or "") for r in rows if isinstance(r, dict)]
        dep_of = [
            str(r.get("dependent_of") or "") for r in rows if isinstance(r, dict)
        ]
        logger.info(
            "scrape_tax_cards found=%s edits=%s candidates=%s names=%s "
            "statuses=%s deps=%s dependent_of=%s rejects=%s",
            len(rows),
            edit_count,
            candidate_count,
            names,
            statuses,
            deps,
            dep_of,
            len(rejects),
        )
        for item in rejects[:12]:
            if not isinstance(item, dict):
                continue
            logger.info(
                "tax_card_reject reason=%s name=%r status=%r deps=%r lines=%s size=%sx%s",
                item.get("reason"),
                item.get("name"),
                item.get("status"),
                item.get("dependents"),
                item.get("lines"),
                item.get("width"),
                item.get("height"),
            )
        for row in rows:
            if not isinstance(row, dict):
                continue
            logger.info(
                "tax_card_accepted name=%r status=%r deps=%r summary=%r",
                row.get("name"),
                row.get("status"),
                row.get("dependents"),
                (row.get("summary") or "")[:120],
            )
        return [r for r in rows if isinstance(r, dict)]
    except Exception:
        logger.exception("scrape_tax_cards failed")
        return []


async def _open_tax_editor(page: Page, member_name: str) -> bool:
    """Click Edit on the named review card."""
    try:
        ok = await page.evaluate(
            "(memberName) => {"
            + _tax_dom_js()
            + """
              const card = taxFindCardEl(memberName);
              if (!card) return false;
              const buttons = [...card.querySelectorAll(
                'button, [role=button], a'
              )].filter(taxVisible);
              for (const btn of buttons) {
                const t = taxLow(
                  btn.innerText || btn.textContent ||
                  btn.getAttribute('aria-label') || ''
                );
                if (t === 'edit' || t.startsWith('edit') || t.includes('edit')) {
                  btn.click();
                  return true;
                }
              }
              return false;
            }""",
            member_name,
        )
        if ok:
            await page.wait_for_timeout(450)
        return bool(ok)
    except Exception:
        logger.exception("open_tax_editor failed member=%r", member_name)
        return False


async def _scrape_tax_modal(page: Page) -> dict[str, Any]:
    """Read the open Tax Information edit modal."""
    try:
        raw = await page.evaluate(
            "() => {"
            + _tax_dom_js()
            + """
              if (!taxModalOpen()) return { open: false };
              const bodyRaw = document.body && document.body.innerText || '';
              const body = taxNorm(bodyRaw);
              let member = '';
              const title = bodyRaw.match(/Tax Information\\s*[–-]\\s*([^\\r\\n]+)/i);
              if (title) {
                const cand = taxLinesOf(title[1])[0] || taxNorm(title[1]);
                if (taxIsPerson(cand)) member = cand;
              }
              let status = '';
              const statusRoot = taxStatusRoot();
              const statusRadios = taxStatusRadios(statusRoot || document);
              for (const el of statusRadios) {
                if (taxReadOn(el) || (el.tagName === 'INPUT' && el.checked)) {
                  status = taxRadioLabel(el);
                  break;
                }
              }
              const claimRoot = taxQuestionRoot('will this person claim any dependents');
              const claimedRoot = taxQuestionRoot('will anyone claim this person as a dependent');
              const claim = taxYnIn(claimRoot);
              const claimed = taxYnIn(claimedRoot);
              const claimWho = taxWhoIn(claimRoot).filter((row) => row.checked).map((r) => r.label);
              const claimWhoAll = taxWhoIn(claimRoot).map((r) => r.label);
              const claimedWho = taxWhoIn(claimedRoot).filter((row) => row.checked).map((r) => r.label);
              const claimedWhoAll = taxWhoIn(claimedRoot).map((r) => r.label);
              return {
                open: true,
                member,
                status,
                claim_deps: claim,
                claim_deps_who: claimWho,
                claim_deps_options: claimWhoAll,
                claimed_as: claimed,
                claimed_as_who: claimedWho,
                claimed_as_options: claimedWhoAll,
              };
            }"""
        )
        return raw if isinstance(raw, dict) else {"open": False}
    except Exception:
        logger.exception("scrape_tax_modal failed")
        return {"open": False}


async def _set_tax_status(page: Page, status: str) -> dict[str, Any]:
    """Select a Tax Status radio (single-select) and verify the result."""
    want = taxLow_py(status)
    if not want:
        logger.warning("set_tax_status empty status")
        return {"ok": False, "stage": "discovery", "reason": "empty_status"}
    try:
        click_result = await page.evaluate(
            "(status) => {"
            + _tax_dom_js()
            + """
              const want = taxLow(status);
              if (!want) {
                return { ok: false, stage: 'discovery', reason: 'empty_status' };
              }
              const root = taxStatusRoot();
              if (!root) {
                return { ok: false, stage: 'discovery', reason: 'status_root_missing' };
              }
              let radios = taxStatusRadios(root);
              const discovered = radios.map((r) => ({
                label: taxRadioLabel(r),
                on: !!(taxReadOn(r) || (r.tagName === 'INPUT' && r.checked)),
              }));
              let hits = radios.filter((r) => taxLow(taxRadioLabel(r)) === want);
              // Fallback: click the compact option row whose sole text is the status.
              if (hits.length !== 1) {
                let best = null;
                let bestLen = Infinity;
                for (const el of root.querySelectorAll(
                  'button, [role=button], label, div, span, p, li'
                )) {
                  if (!taxVisible(el)) continue;
                  const lines = taxLinesOf(el.innerText || el.textContent || '');
                  if (lines.length !== 1) continue;
                  const canon = taxCanonicalStatus(lines[0]);
                  if (taxLow(canon) !== want) continue;
                  const len = taxNorm(el.innerText || '').length;
                  if (len < bestLen) {
                    best = el;
                    bestLen = len;
                  }
                }
                if (!best) {
                  return {
                    ok: false,
                    stage: 'discovery',
                    reason: hits.length ? 'ambiguous_radio' : 'radio_not_found',
                    want,
                    discovered,
                  };
                }
                try { best.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
                const rowRadio = best.closest('label')
                  ? best.closest('label').querySelector(
                      'input[type=radio], [role=radio]'
                    )
                  : (best.parentElement && best.parentElement.querySelector(
                      'input[type=radio], [role=radio]'
                    ));
                const clickEl = rowRadio || best;
                clickEl.click();
                return {
                  ok: true,
                  stage: 'clicked_fallback',
                  selected: status,
                  want,
                  discovered,
                };
              }
              const target = hits[0];
              const already = taxReadOn(target) ||
                (target.tagName === 'INPUT' && target.checked);
              if (already) {
                return {
                  ok: true,
                  stage: 'already_selected',
                  selected: taxRadioLabel(target),
                  want,
                  discovered,
                };
              }
              try { target.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
              const clickEl = target.closest('label') || target;
              clickEl.click();
              return {
                ok: true,
                stage: 'clicked',
                selected: taxRadioLabel(target),
                want,
                discovered,
              };
            }""",
            status,
        )
        if not isinstance(click_result, dict) or not click_result.get("ok"):
            logger.warning(
                "set_tax_status discovery/click failed status=%r result=%r",
                status,
                click_result,
            )
            return (
                click_result
                if isinstance(click_result, dict)
                else {"ok": False, "stage": "discovery", "reason": "evaluate_failed"}
            )
        if click_result.get("stage") == "already_selected":
            logger.info(
                "set_tax_status already selected status=%r discovered=%r",
                status,
                click_result.get("discovered"),
            )
            return {"ok": True, "stage": "already_selected", "status": status}

        await page.wait_for_timeout(250)
        verify = await page.evaluate(
            "(status) => {"
            + _tax_dom_js()
            + """
              const want = taxLow(status);
              const root = taxStatusRoot();
              if (!root) {
                return { ok: false, stage: 'verify', reason: 'status_root_missing' };
              }
              const radios = taxStatusRadios(root);
              const selected = radios.filter(
                (r) => taxReadOn(r) || (r.tagName === 'INPUT' && r.checked)
              );
              const labels = selected.map((r) => taxRadioLabel(r));
              if (selected.length !== 1) {
                return {
                  ok: false,
                  stage: 'verify',
                  reason: 'expected_single_selection',
                  selected_count: selected.length,
                  labels,
                };
              }
              const got = taxLow(labels[0] || '');
              if (!(got === want || got.startsWith(want) || want.startsWith(got))) {
                return {
                  ok: false,
                  stage: 'verify',
                  reason: 'selection_mismatch',
                  labels,
                  want,
                };
              }
              return { ok: true, stage: 'verified', status: labels[0], labels };
            }""",
            status,
        )
        if not isinstance(verify, dict) or not verify.get("ok"):
            # Fallback verify via modal scrape (same helpers).
            modal = await _scrape_tax_modal(page)
            got = taxLow_py(str(modal.get("status") or ""))
            if got == want or got.startswith(want) or want.startswith(got):
                logger.info(
                    "set_tax_status verified via modal scrape status=%r got=%r",
                    status,
                    modal.get("status"),
                )
                return {"ok": True, "stage": "verified_modal", "status": status}
            logger.warning(
                "set_tax_status verify failed status=%r verify=%r modal_status=%r",
                status,
                verify,
                modal.get("status"),
            )
            return (
                verify
                if isinstance(verify, dict)
                else {"ok": False, "stage": "verify", "reason": "verify_failed"}
            )
        logger.info(
            "set_tax_status ok status=%r stage=%r click_stage=%r",
            status,
            verify.get("stage"),
            click_result.get("stage"),
        )
        return {"ok": True, "stage": "verified", "status": status}
    except Exception:
        logger.exception("set_tax_status failed status=%r", status)
        return {"ok": False, "stage": "exception", "reason": "exception"}


def taxLow_py(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


async def _set_tax_yes_no(page: Page, question: str, value: str) -> bool:
    """Set Yes/No for claim_deps or claimed_as and wait for Who? when Yes."""
    needle = (
        "will this person claim any dependents"
        if question == "claim_deps"
        else "will anyone claim this person as a dependent"
    )
    try:
        click_result = await page.evaluate(
            "({ needle, value }) => {"
            + _tax_dom_js()
            + """
              const root = taxQuestionRoot(needle);
              if (!root) {
                return { ok: false, stage: 'discovery', reason: 'question_root_missing', needle };
              }
              return taxClickYn(root, value);
            }""",
            {"needle": needle, "value": value},
        )
        # Back-compat if an older evaluate returned a bare boolean.
        if isinstance(click_result, bool):
            click_result = {
                "ok": click_result,
                "stage": "clicked" if click_result else "discovery",
                "reason": "" if click_result else "yn_not_found",
            }
        if not isinstance(click_result, dict) or not click_result.get("ok"):
            logger.warning(
                "set_tax_yes_no discovery/click failed question=%s value=%r result=%r",
                question,
                value,
                click_result,
            )
            return False
        if click_result.get("stage") == "already_selected":
            logger.info(
                "set_tax_yes_no already selected question=%s value=%r discovered=%r",
                question,
                value,
                click_result.get("discovered"),
            )
        else:
            logger.info(
                "set_tax_yes_no clicked question=%s value=%r stage=%s",
                question,
                value,
                click_result.get("stage"),
            )
            await page.wait_for_timeout(350)
        if taxLow_py(value) == "yes":
            who = await _wait_tax_who_members(page, question, timeout_ms=2500)
            if not who:
                logger.info(
                    "set_tax_yes_no who-list not visible yet question=%s "
                    "(continuing; verify Yes/No only)",
                    question,
                )
        modal = await _scrape_tax_modal(page)
        key = "claim_deps" if question == "claim_deps" else "claimed_as"
        got = taxLow_py(str(modal.get(key) or ""))
        want = taxLow_py(value)
        if got == want:
            return True
        # Direct scoped re-read in case scrape keys lagged.
        verify = await page.evaluate(
            "({ needle }) => {"
            + _tax_dom_js()
            + """
              const root = taxQuestionRoot(needle);
              return { root: !!root, value: taxYnIn(root) };
            }""",
            {"needle": needle},
        )
        got2 = taxLow_py(str((verify or {}).get("value") or ""))
        if got2 == want:
            logger.info(
                "set_tax_yes_no verified via scoped read question=%s value=%r",
                question,
                value,
            )
            return True
        logger.warning(
            "set_tax_yes_no verify failed question=%s value=%r got=%r "
            "scoped=%r click=%r",
            question,
            value,
            got,
            verify,
            click_result,
        )
        return False
    except Exception:
        logger.exception("set_tax_yes_no failed question=%s value=%r", question, value)
        return False


async def _wait_tax_who_members(
    page: Page, question: str, *, timeout_ms: int = 3000
) -> list[str]:
    """Poll until the conditional Who? list for a dependent question is readable."""
    deadline = time.monotonic() + max(0.35, timeout_ms / 1000.0)
    last: list[str] = []
    while True:
        modal = await _scrape_tax_modal(page)
        key = (
            "claim_deps_options"
            if question == "claim_deps"
            else "claimed_as_options"
        )
        last = [str(x) for x in (modal.get(key) or []) if str(x).strip()]
        if last:
            return last
        # Checked-only list is also evidence the section expanded.
        checked_key = (
            "claim_deps_who" if question == "claim_deps" else "claimed_as_who"
        )
        checked = [str(x) for x in (modal.get(checked_key) or []) if str(x).strip()]
        if checked:
            return checked
        if time.monotonic() >= deadline:
            break
        try:
            await page.wait_for_timeout(120)
        except Exception:
            break
    return last


async def _set_tax_who_members(
    page: Page, question: str, names: list[str]
) -> bool:
    """Check the requested members under the scoped Who? list (multi-select)."""
    needle = (
        "will this person claim any dependents"
        if question == "claim_deps"
        else "will anyone claim this person as a dependent"
    )
    want = [n for n in names if str(n).strip()]
    if not want:
        return False
    try:
        result = await page.evaluate(
            "({ needle, names }) => {"
            + _tax_dom_js()
            + """
              const root = taxQuestionRoot(needle);
              if (!root) return { ok: false, reason: 'root_missing' };
              const wantLows = names.map((n) => taxLow(n));
              const boxes = [...root.querySelectorAll(
                'input[type=checkbox], [role=checkbox]'
              )].filter(taxVisible);
              let matched = 0;
              for (const box of boxes) {
                let name = taxLabelOf(box);
                if (!taxIsPerson(name)) {
                  let cur = box.parentElement;
                  for (let d = 0; d < 6 && cur; d++) {
                    const leaves = [...cur.querySelectorAll('div, span, p, label')]
                      .map((n) => taxNorm(n.innerText || n.textContent || ''))
                      .filter(taxIsPerson);
                    const uniq = [...new Set(leaves)];
                    if (uniq.length === 1) { name = uniq[0]; break; }
                    cur = cur.parentElement;
                  }
                }
                if (!taxIsPerson(name)) continue;
                const low = taxLow(name);
                const hit = wantLows.some((w) =>
                  low === w || low.startsWith(w + ' ') || w.startsWith(low.split(' ')[0])
                );
                if (!hit) continue;
                if (!taxReadOn(box)) box.click();
                matched += 1;
              }
              return { ok: matched > 0, matched };
            }""",
            {"needle": needle, "names": want},
        )
        await page.wait_for_timeout(300)
        if not (isinstance(result, dict) and result.get("ok")):
            return False
        modal = await _scrape_tax_modal(page)
        checked_key = (
            "claim_deps_who" if question == "claim_deps" else "claimed_as_who"
        )
        checked = {
            taxLow_py(str(x))
            for x in (modal.get(checked_key) or [])
            if str(x).strip()
        }
        for name in want:
            low = taxLow_py(name)
            if any(
                c == low or c.startswith(low + " ") or low.startswith(c.split(" ")[0])
                for c in checked
            ):
                continue
            # Click reported matched but verify lagged — accept soft success.
            if int(result.get("matched") or 0) >= 1:
                return True
            return False
        return True
    except Exception:
        logger.exception(
            "set_tax_who_members failed question=%s names=%r", question, names
        )
        return False


async def _click_tax_save_return(page: Page) -> bool:
    """Click the visible Save and Return control once; confirm summary is back.

    Discovers the actual button inside the open tax dialog (normalizing
    and/&/casing) instead of sequentially waiting on missing label variants
    via ``_click_cta`` (that caused multi-second hangs per missing label).
    Does not press Escape / Cancel — that would discard unsaved edits.
    If the editor is already closed and the summary is visible (prior save
    finished while we were waiting), treat that as success.
    """
    started = time.monotonic()
    try:
        clicked = await page.evaluate(
            "() => {" + _tax_dom_js() + " return taxClickSaveReturnOnce(); }"
        )
    except Exception:
        logger.exception("tax_save_click_evaluate_failed")
        return False
    if not isinstance(clicked, dict) or not clicked.get("ok"):
        # Save may have already completed — modal gone, summary cards back.
        try:
            raw = await page.evaluate(
                "() => {" + _tax_dom_js() + " return taxSummaryAfterSave(); }"
            )
        except Exception:
            raw = {}
        if (
            isinstance(raw, dict)
            and raw.get("summary_visible")
            and not raw.get("modal_open")
        ):
            logger.info(
                "tax_save_already_on_summary cards=%s names=%s elapsed_ms=%s",
                raw.get("card_count"),
                raw.get("card_names"),
                int((time.monotonic() - started) * 1000),
            )
            return True
        logger.warning(
            "tax_save_button_missing elapsed_ms=%s labels=%s",
            int((time.monotonic() - started) * 1000),
            (clicked or {}).get("labels") if isinstance(clicked, dict) else [],
        )
        return False
    logger.info(
        "tax_save_clicked kind=%s label=%r elapsed_ms=%s",
        clicked.get("kind"),
        clicked.get("label"),
        int((time.monotonic() - started) * 1000),
    )
    # Short bounded poll — summary cards confirm the editor closed.
    last_state: dict[str, Any] = {}
    for _ in range(12):
        await page.wait_for_timeout(200)
        try:
            raw = await page.evaluate(
                "() => {" + _tax_dom_js() + " return taxSummaryAfterSave(); }"
            )
        except Exception:
            raw = {}
        last_state = raw if isinstance(raw, dict) else {}
        if last_state.get("validation_error"):
            logger.warning(
                "tax_save_validation_blocker error=%r modal_open=%s elapsed_ms=%s",
                last_state.get("validation_error"),
                last_state.get("modal_open"),
                int((time.monotonic() - started) * 1000),
            )
            return False
        if last_state.get("summary_visible") and not last_state.get("modal_open"):
            logger.info(
                "tax_save_ok label=%r cards=%s names=%s elapsed_ms=%s",
                clicked.get("label"),
                last_state.get("card_count"),
                last_state.get("card_names"),
                int((time.monotonic() - started) * 1000),
            )
            return True
    logger.warning(
        "tax_save_modal_still_open label=%r modal_open=%s summary=%s "
        "cards=%s heading=%r elapsed_ms=%s",
        clicked.get("label"),
        last_state.get("modal_open"),
        last_state.get("summary_visible"),
        last_state.get("card_count"),
        last_state.get("heading"),
        int((time.monotonic() - started) * 1000),
    )
    return False


def _normalize_tax_spoken(spoken: str) -> str:
    text = re.sub(r"[.!?…]+$", "", (spoken or "").strip().lower())
    text = re.sub(r"\s+", " ", text)
    # Drop light leading pronouns / fillers so "I'm single" → "single".
    text = re.sub(
        r"^(?:i(?:'m| am)|we(?:'re| are)|they(?:'re| are)|it(?:'s| is)|its)\s+",
        "",
        text,
    )
    return text.strip()


def _tax_status_needs_clarification(spoken: str) -> bool:
    """True when the caller said bare 'married' without jointly/separately."""
    text = _normalize_tax_spoken(spoken)
    if not text:
        return False
    if not re.search(r"\bmarried\b", text):
        return False
    if re.search(r"\b(joint|jointly|separate|separately)\b", text):
        return False
    return _map_tax_status(spoken) is None


def _map_tax_status(spoken: str) -> str | None:
    """Map spoken filing status onto a Pointcare tax status label.

    Supports natural / partial wording. Returns None for unknown or ambiguous
    answers (e.g. bare \"married\") so the caller can be asked to clarify.
    """
    text = _normalize_tax_spoken(spoken)
    if not text:
        return None

    # Not filing / negation of filing — before generic \"single\".
    if re.search(
        r"\b("
        r"not filing|non[\s-]?filer|won'?t file|"
        r"do(?:es)?n'?t file(?:\s+taxes)?|"
        r"i don'?t file(?:\s+taxes)?|"
        r"does not file(?:\s+taxes)?"
        r")\b",
        text,
        re.I,
    ):
        return "Not filing"

    if re.search(
        r"\b("
        r"married filing jointly|filing jointly|married jointly|"
        r"jointly|joint returns?"
        r")\b",
        text,
        re.I,
    ):
        return "Married filing jointly"

    if re.search(
        r"\b("
        r"married filing separately|filing separately|"
        r"separately|separate returns?"
        r")\b",
        text,
        re.I,
    ):
        return "Married filing separately"

    if re.search(
        r"\b(head of household|household head|hoh)\b",
        text,
        re.I,
    ):
        return "Head of Household"

    if re.search(r"\b(qualifying widow|widow(?:er)?)\b", text, re.I):
        return "Qualifying Widow(er) with Dependent Child"

    # Avoid mapping \"not single\" / \"never unmarried\" to Single.
    if re.search(r"\b(single|unmarried)\b", text, re.I) and not re.search(
        r"\b(not|n't|never)\s+(?:be\s+)?(?:a\s+)?(?:single|unmarried)\b",
        text,
        re.I,
    ):
        return "Single"

    # Bare \"married\" is ambiguous — do not guess.
    if re.fullmatch(r"married", text):
        return None

    for label in _TAX_STATUS_LABELS:
        if label.lower() == text:
            return label
    return None


def _tax_heard(session: Any) -> list[int]:
    raw = session.filled.get("app_tax_heard") or "[]"
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


def _mark_tax_heard(session: Any, index: int) -> None:
    heard = _tax_heard(session)
    if index not in heard:
        heard.append(index)
    session.filled["app_tax_heard"] = json.dumps(heard)
    session.filled["app_tax_walk_index"] = str(index)


def _tax_phase(session: Any) -> str:
    return str(session.filled.get("app_tax_phase") or "").strip() or "status"


def _set_tax_phase(session: Any, phase: str) -> None:
    session.filled["app_tax_phase"] = phase


def _clear_tax_member_state(session: Any) -> None:
    for key in (
        "app_tax_current",
        "app_tax_current_index",
        "app_tax_phase",
        "app_tax_pending_status",
        "app_tax_pending_claim",
        "app_tax_pending_claimed",
        "app_tax_who_buffer",
        "app_tax_saving_name",
        "app_tax_saving_index",
    ):
        session.filled.pop(key, None)


def _parse_member_names(text: str) -> list[str]:
    chunks = [
        part.strip()
        for part in re.split(r"\band\b|,|&", text or "", flags=re.I)
        if part.strip()
    ]
    return chunks


def _tax_status_ask_speech(name: str) -> str:
    who = name or "this person"
    return f"What is {who}'s tax filing status?"


def _tax_status_confirm_speech(name: str, status: str) -> str:
    who = name or "this person"
    current = (status or "").strip()
    if current:
        return (
            f"{who}'s current tax filing status is {current}. "
            "Is that correct?"
        )
    return _tax_status_ask_speech(who)


def _tax_claim_ask_speech(name: str) -> str:
    who = name or "this person"
    return f"Will {who} claim any dependents? Please say Yes or No."


def _tax_claimed_ask_speech(name: str) -> str:
    who = name or "this person"
    return f"Will anyone claim {who} as a dependent? Please say Yes or No."


def _tax_card_speech(card: dict[str, Any], *, intro: bool = False) -> str:
    """Lead-in for a member tax review — confirm known status when present."""
    name = str(card.get("name") or "this person").strip()
    status = str(card.get("status") or "").strip()
    if intro:
        prefix = (
            "I'll review the tax information for each household member, "
            f"starting with {name}. "
        )
    else:
        prefix = f"I'm starting with {name}. "
    return prefix + _tax_status_confirm_speech(name, status)


def _tax_claim_confirm_speech(name: str, answer: str, who: list[str]) -> str:
    """Backward-compatible alias — ask the claim-deps question directly."""
    return _tax_claim_ask_speech(name)


def _tax_claimed_confirm_speech(name: str, answer: str, who: list[str]) -> str:
    """Backward-compatible alias — ask the claimed-as question directly."""
    return _tax_claimed_ask_speech(name)


def _tax_modal_member_matches(modal: dict[str, Any], member_name: str) -> bool:
    """True when an open modal belongs to the requested member (or member unknown)."""
    from .live_form import _normalize_member_label

    got = _normalize_member_label(str(modal.get("member") or ""))
    want = _normalize_member_label(member_name or "")
    if not got or not want:
        # Title scrape sometimes misses the name — don't reject solely on that.
        return True
    return got == want or got.startswith(want) or want.startswith(got)


async def _close_tax_modal(page: Page) -> bool:
    """Dismiss an open tax editor (Cancel / Close / X) so the next member can open."""
    from .live_form import _click_cta

    for label in ("Cancel", "Close", "Back"):
        try:
            await _click_cta(page, label, attempts=1)
            await page.wait_for_timeout(300)
            if not (await _scrape_tax_modal(page)).get("open"):
                return True
        except Exception:
            continue
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(250)
    except Exception:
        pass
    return not (await _scrape_tax_modal(page)).get("open")


async def _ensure_tax_modal(
    page: Page, member_name: str
) -> dict[str, Any]:
    modal = await _scrape_tax_modal(page)
    if modal.get("open"):
        if _tax_modal_member_matches(modal, member_name):
            return modal
        logger.info(
            "ensure_tax_modal: open modal is for %r, want %r — closing first",
            modal.get("member"),
            member_name,
        )
        await _close_tax_modal(page)
    opened = await _open_tax_editor(page, member_name)
    if not opened:
        return {"open": False}
    for _ in range(10):
        modal = await _scrape_tax_modal(page)
        if modal.get("open") and _tax_modal_member_matches(modal, member_name):
            return modal
        await page.wait_for_timeout(150)
    modal = await _scrape_tax_modal(page)
    return modal if modal.get("open") else {"open": False}


async def _begin_tax_member(
    session: Any, page: Page, card: dict[str, Any], *, intro: bool = False
) -> str:
    """Open the Edit modal first, then confirm / ask filing status."""
    name = str(card.get("name") or "").strip()
    index = int(card.get("index") or 0)
    session.filled["app_tax_current"] = name
    session.filled["app_tax_current_index"] = str(index)
    session.filled.pop("app_tax_pending_claim", None)
    session.filled.pop("app_tax_pending_claimed", None)
    if intro:
        prefix = (
            "I'll review the tax information for each household member, "
            f"starting with {name}. "
        )
    else:
        prefix = f"I'm starting with {name}. "
    # Close any leftover editor, then open this member's modal before speaking.
    existing = await _scrape_tax_modal(page)
    if existing.get("open") and not _tax_modal_member_matches(existing, name):
        await _close_tax_modal(page)
    modal = await _ensure_tax_modal(page, name)
    if not modal.get("open"):
        _set_tax_phase(session, "status")
        return (
            prefix
            + f"I couldn't open tax details for {name}. "
            "Please say try again."
        )
    status = str(modal.get("status") or card.get("status") or "").strip()
    session.filled["app_tax_pending_status"] = status
    _set_tax_phase(session, "status")
    return prefix + _tax_status_confirm_speech(name, status)


async def _advance_after_tax_member(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    step: str,
    cards: list[dict[str, Any]],
    index: int,
) -> dict[str, Any]:
    from .app_stage import _app_ok, _set_pending_question

    _mark_tax_heard(session, index)
    _clear_tax_member_state(session)
    heard = set(_tax_heard(session))
    next_card: dict[str, Any] | None = None
    for card in cards:
        try:
            idx = int(card.get("index"))
        except (TypeError, ValueError):
            continue
        if idx not in heard:
            next_card = card
            break
    if next_card is not None:
        next_name = str(next_card.get("name") or "").strip()
        next_index = int(next_card.get("index") or 0)
        logger.info(
            "tax_advance_next call_id=%s from_index=%s next_name=%r next_index=%s",
            call_id,
            index,
            next_name,
            next_index,
        )
        speech = await _begin_tax_member(session, page, next_card, intro=False)
        _set_pending_question(session, step, speech)
        logger.info(
            "tax_advance_next_speech call_id=%s next_name=%r speech_prefix=%r",
            call_id,
            next_name,
            (speech or "")[:80],
        )
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "tax next member",
            spoken_override=speech,
        )
    logger.info(
        "tax_advance_all_done call_id=%s last_index=%s heard=%s",
        call_id,
        index,
        sorted(heard),
    )
    # All members done — advance without another Yes/No confirm loop.
    return await _finish_tax_review_next(call_id, session, page, text, step)


async def _finish_tax_review_next(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    step: str,
) -> dict[str, Any]:
    """Click Next after every member has been walked — no extra confirm."""
    from .app_stage import (
        _advance_after_answer,
        _app_ok,
        _click_stage_next,
        _prepare_resume_speech,
        _set_pending_question,
        _wait_for_screen_change,
        app_question_for_step,
    )

    session.filled["app_tax_review_ready"] = "1"
    _set_tax_phase(session, "final")
    # Drop prior Who?/claimed-as pending so reminders don't replay it mid-Next.
    _set_pending_question(
        session,
        step,
        "I've finished tax information for everyone. One moment.",
    )
    clicked = await _click_stage_next(page)
    if not clicked:
        speech = (
            "I've finished tax information for everyone, but Next is not "
            "available yet. Say continue to try again."
        )
        _set_pending_question(session, step, speech)
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "tax_next_disabled",
            "step": step,
            "phase": "application",
        }
    next_step = await _advance_after_answer(
        page,
        session,
        allow_extra_next=False,
        leave_step=step,
        expected_next="app_income",
    )
    if not next_step or next_step == step:
        next_step = await _wait_for_screen_change(page, step, timeout_s=4.0)
    if not next_step or next_step == step:
        # Soft confirm via income probe before declaring tax still open.
        try:
            from .income_stage import probe_income_screen

            income_probe = await probe_income_screen(page)
        except Exception:
            income_probe = {}
        if income_probe.get("is_income"):
            next_step = "app_income"
            logger.info(
                "tax_finish_income_probe_ok call_id=%s heading=%r cards=%s",
                call_id,
                income_probe.get("heading"),
                income_probe.get("card_count"),
            )
    if not next_step or next_step == step:
        speech = (
            "I've finished tax information, but I couldn't confirm the Work "
            "and Income screen yet. Please stay on Work and Income and say "
            "continue."
        )
        _set_pending_question(session, step, speech)
        logger.warning(
            "tax_next_stayed call_id=%s detected=%r",
            call_id,
            next_step,
        )
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "tax_next_stayed",
            "step": step,
            "phase": "application",
        }
    for key in _TAX_FILLED_KEYS:
        session.filled.pop(key, None)
    try:
        next_step, spoken = await _prepare_resume_speech(session, page, next_step)
    except Exception:
        spoken = app_question_for_step(next_step, session)
    lead = "I've finished tax information for everyone. "
    return _app_ok(
        call_id,
        session,
        text,
        next_step,
        "tax review advanced",
        spoken_override=lead + (spoken or ""),
    )


async def _save_tax_member_and_continue(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    step: str,
    cards: list[dict[str, Any]],
    index: int,
    name: str,
) -> dict[str, Any]:
    from .app_stage import _set_pending_question

    # Leave claimed_as / who phases immediately so resume/reminders cannot
    # re-ask the last Who? question while Save and Return is running.
    _set_tax_phase(session, "saving")
    session.filled["app_tax_saving_name"] = name
    session.filled["app_tax_saving_index"] = str(index)
    saving_speech = (
        f"I'm saving tax details for {name}. "
        "One moment."
    )
    _set_pending_question(session, step, saving_speech)
    # Reminders / ASR revisions use ctrl.last_speech — replace the prior
    # Who?/claimed_as prompt before the ~2s Save and Return wait.
    try:
        from .call_turn import get_controller

        ctrl = await get_controller(call_id)
        ctrl.last_speech = saving_speech
    except Exception:
        logger.exception(
            "tax_save_last_speech_update_failed call_id=%s member=%r",
            call_id,
            name,
        )
    logger.info(
        "tax_save_begin call_id=%s member=%r index=%s phase=saving",
        call_id,
        name,
        index,
    )

    started = time.monotonic()
    saved = await _click_tax_save_return(page)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if not saved:
        speech = (
            f"I couldn't save tax details for {name}. "
            "Please say save or continue to try again."
        )
        _set_pending_question(session, step, speech)
        logger.warning(
            "tax_save_failed call_id=%s member=%r index=%s elapsed_ms=%s "
            "advance_invoked=False",
            call_id,
            name,
            index,
            elapsed_ms,
        )
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "tax_save_failed",
            "step": step,
            "phase": "application",
        }
    session.filled.pop("app_tax_saving_name", None)
    session.filled.pop("app_tax_saving_index", None)
    # Refresh cards and soft-verify the member still appears.
    refreshed = await _scrape_tax_cards(page)
    if refreshed:
        cards = refreshed
    logger.info(
        "tax_save_advance call_id=%s member=%r index=%s elapsed_ms=%s "
        "advance_invoked=True cards=%s",
        call_id,
        name,
        index,
        elapsed_ms,
        [str(c.get("name") or "") for c in (cards or [])],
    )
    return await _advance_after_tax_member(
        call_id, session, page, text, step, cards, index
    )


def _match_who_names(spoken_chunks: list[str], options: list[str]) -> list[str]:
    """Resolve spoken names against visible Who? options."""
    from .live_form import _normalize_member_label

    picked: list[str] = []
    for chunk in spoken_chunks:
        want = _normalize_member_label(chunk)
        if not want:
            continue
        exact = [
            opt
            for opt in options
            if _normalize_member_label(opt) == want
            or opt.strip().rstrip(".") == chunk.strip().rstrip(".")
        ]
        if len(exact) == 1:
            if exact[0] not in picked:
                picked.append(exact[0])
            continue
        starts = [
            opt
            for opt in options
            if _normalize_member_label(opt).startswith(want)
            or want.startswith(_normalize_member_label(opt).split(" ")[0])
        ]
        if starts and starts[0] not in picked:
            picked.append(starts[0])
    return picked


async def _prepare_tax_review_speech(session: Any, page: Page) -> tuple[str, str]:
    from .app_stage import _set_pending_question, app_question_for_step

    cards = await _scrape_tax_cards(page)
    current = str(session.filled.get("app_tax_current") or "").strip()
    phase = _tax_phase(session)
    if current and phase in _TAX_PHASES and phase != "final":
        modal = await _scrape_tax_modal(page)
        if phase == "saving":
            who = (
                str(session.filled.get("app_tax_saving_name") or current).strip()
                or "this person"
            )
            # Mid-save speech only — do not re-ask Who? or invite retry yet.
            speech = f"I'm saving tax details for {who}. One moment."
        elif phase == "status":
            pending = str(
                session.filled.get("app_tax_pending_status")
                or (modal.get("status") if isinstance(modal, dict) else "")
                or ""
            ).strip()
            speech = _tax_status_confirm_speech(current, pending)
        elif phase == "claim_deps":
            speech = _tax_claim_ask_speech(current)
        elif phase == "claim_deps_who":
            opts = [
                str(x)
                for x in (modal.get("claim_deps_options") or [])
                if str(x).strip()
            ]
            if opts:
                speech = (
                    f"Who will {current} claim as a dependent? "
                    f"Available members are {', '.join(opts[:8])}. "
                    "Say one or more names, then say done."
                )
            else:
                speech = (
                    f"For {current}, dependents are marked Yes. "
                    "Say continue to keep going."
                )
        elif phase == "claimed_as":
            speech = _tax_claimed_ask_speech(current)
        elif phase == "claimed_as_who":
            opts = [
                str(x)
                for x in (modal.get("claimed_as_options") or [])
                if str(x).strip()
            ]
            if opts:
                speech = (
                    f"Who will claim {current} as a dependent? "
                    f"Available members are {', '.join(opts[:8])}. "
                    "Say one or more names, then say done."
                )
            else:
                speech = (
                    f"For {current}, claimed-as-dependent is marked Yes. "
                    "Say continue to save and return."
                )
        else:
            speech = app_question_for_step("app_tax", session)
        _set_pending_question(session, "app_tax", speech)
        return "app_tax", speech

    session.filled["app_tax_heard"] = "[]"
    session.filled["app_tax_walk_index"] = "0"
    session.filled.pop("app_tax_review_ready", None)
    if not cards:
        # Do not fall back to the generic Yes/No prompt — that invites the
        # caller to say Yes while discovery is still broken.
        speech = (
            "I'm on the tax information screen, but I can't see the household "
            "member cards yet. Please stay on this page and say try again."
        )
        logger.warning("prepare_tax_review_speech: no tax cards discovered")
        _set_pending_question(session, "app_tax", speech)
        return "app_tax", speech
    speech = await _begin_tax_member(session, page, cards[0], intro=True)
    _set_pending_question(session, "app_tax", speech)
    return "app_tax", speech


async def _handle_tax_step(
    call_id: str,
    session: Any,
    page: Page,
    text: str,
    lowered: str,
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
        detect_application_screen,
    )
    from .live_form import _is_no, _is_yes

    cards = await _scrape_tax_cards(page)
    phase = _tax_phase(session)
    current = str(session.filled.get("app_tax_current") or "").strip()
    index_raw = str(session.filled.get("app_tax_current_index") or "").strip()
    index = int(index_raw) if index_raw.isdigit() else 0

    # Final confirmation → Next, or reopen a named member.
    if phase == "final" or session.filled.get("app_tax_review_ready"):
        if _is_yes(text) or re.search(
            r"\b(continue|next|looks? (good|correct|fine)|done)\b", lowered
        ):
            # Prefer the shared finish helper (income expected_next + probe).
            return await _finish_tax_review_next(
                call_id, session, page, text, step
            )
        # Name a member to reopen.
        if cards:
            from .live_form import _normalize_member_label

            want = _normalize_member_label(text)
            for card in cards:
                name = str(card.get("name") or "")
                if want and (
                    _normalize_member_label(name) == want
                    or _normalize_member_label(name).startswith(want)
                ):
                    session.filled.pop("app_tax_review_ready", None)
                    speech = await _begin_tax_member(session, page, card)
                    _set_pending_question(session, step, speech)
                    return _app_ok(
                        call_id,
                        session,
                        text,
                        step,
                        "tax reopen member",
                        spoken_override=speech,
                    )
        speech = (
            "Please say Yes to continue, or say the household member's name to change."
        )
        _set_pending_question(session, step, speech)
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "tax_final_clarify",
            "step": step,
            "phase": "application",
        }

    if not cards and not current:
        live = await detect_application_screen(page)
        if live and live.startswith("app_") and live != step:
            for key in _TAX_FILLED_KEYS:
                session.filled.pop(key, None)
            try:
                live, spoken = await _prepare_resume_speech(session, page, live)
            except Exception:
                spoken = app_question_for_step(live, session)
            return _app_ok(
                call_id,
                session,
                text,
                live,
                "tax already advanced",
                spoken_override=spoken,
            )
        # One retry before failing — cards can lag after circumstances Next.
        await page.wait_for_timeout(500)
        cards = await _scrape_tax_cards(page)
        if cards:
            speech = await _begin_tax_member(session, page, cards[0], intro=True)
            _set_pending_question(session, step, speech)
            return _app_ok(
                call_id,
                session,
                text,
                step,
                "tax begin after retry",
                spoken_override=speech,
            )
        retry_ask = bool(
            re.search(r"\b(try again|retry|repeat|again)\b", lowered)
            or _is_yes(text)
        )
        speech = (
            "I still can't see the tax information cards on this page. "
            "Please stay on Tax Information and say try again."
            if retry_ask
            else (
                "I couldn't read the tax information cards. "
                "Please stay on Tax Information and say try again."
            )
        )
        logger.warning(
            "tax_cards_missing call_id=%s utterance=%r retry_ask=%s",
            call_id,
            text,
            retry_ask,
        )
        _set_pending_question(session, step, speech)
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "tax_cards_missing",
            "step": step,
            "phase": "application",
        }

    if not current and cards:
        speech = await _begin_tax_member(session, page, cards[0], intro=True)
        _set_pending_question(session, step, speech)
        return _app_ok(
            call_id, session, text, step, "tax begin", spoken_override=speech
        )

    # Retry Save and Return without re-asking the last dependent question.
    if phase == "saving":
        save_name = (
            str(session.filled.get("app_tax_saving_name") or current).strip()
            or current
        )
        save_index_raw = str(session.filled.get("app_tax_saving_index") or "").strip()
        save_index = int(save_index_raw) if save_index_raw.isdigit() else index
        # Prior click may have already closed the editor (~2s website save).
        already_saved = False
        try:
            raw = await page.evaluate(
                "() => {" + _tax_dom_js() + " return taxSummaryAfterSave(); }"
            )
            if (
                isinstance(raw, dict)
                and raw.get("summary_visible")
                and not raw.get("modal_open")
            ):
                already_saved = True
        except Exception:
            already_saved = False
        if already_saved:
            session.filled.pop("app_tax_saving_name", None)
            session.filled.pop("app_tax_saving_index", None)
            refreshed = await _scrape_tax_cards(page)
            if refreshed:
                cards = refreshed
            logger.info(
                "tax_save_already_complete call_id=%s member=%r index=%s "
                "advance_invoked=True",
                call_id,
                save_name,
                save_index,
            )
            return await _advance_after_tax_member(
                call_id, session, page, text, step, cards or [], save_index
            )
        if (
            _is_yes(text)
            or re.search(
                r"\b(save|continue|done|try again|retry|again)\b",
                lowered,
            )
        ):
            return await _save_tax_member_and_continue(
                call_id,
                session,
                page,
                text,
                step,
                cards or [],
                save_index,
                save_name,
            )
        speech = (
            f"I'm saving tax details for {save_name}. "
            "Say continue, save, or done to try again."
        )
        _set_pending_question(session, step, speech)
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "tax_saving_clarify",
            "step": step,
            "phase": "application",
        }

    modal = await _ensure_tax_modal(page, current)
    if not modal.get("open"):
        speech = (
            f"I couldn't open the tax editor for {current}. "
            "Please say repeat."
        )
        _set_pending_question(session, step, speech)
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "tax_modal_missing",
            "step": step,
            "phase": "application",
        }

    wants_yes = bool(
        _is_yes(text)
        or re.search(r"\b(continue|looks? correct|that(?:'s| is) correct)\b", lowered)
    )
    wants_no = bool(
        _is_no(text)
        or re.search(r"\b(change|wrong|incorrect|fix|edit)\b", lowered)
    )
    done_who = bool(
        re.search(r"\b(done|finished|that's all|that is all|continue)\b", lowered)
    )

    # ---- Phase: status (confirm known value, or set a spoken status) ----
    if phase == "status":
        status_now = str(
            modal.get("status")
            or session.filled.get("app_tax_pending_status")
            or ""
        ).strip()
        # Yes / continue keeps the current filing status.
        if status_now and (
            _is_yes(text)
            or re.search(
                r"\b(continue|looks? correct|that(?:'s| is) correct|keep it)\b",
                lowered,
            )
        ):
            mapped = status_now
        elif _is_no(text) and not _map_tax_status(text):
            speech = (
                f"Okay — what is {current}'s tax filing status? "
                "Say Single, Married filing jointly, Married filing separately, "
                "Head of Household, Qualifying Widow, or Not filing."
            )
            _set_pending_question(session, step, speech)
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "tax_status_change",
                "step": step,
                "phase": "application",
            }
        else:
            mapped = _map_tax_status(text)
        if _tax_status_needs_clarification(text):
            speech = (
                "Are they married filing jointly, or married filing separately?"
            )
            _set_pending_question(session, step, speech)
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "tax_status_ambiguous",
                "step": step,
                "phase": "application",
            }
        if not mapped:
            speech = _tax_status_confirm_speech(current, status_now)
            _set_pending_question(session, step, speech)
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "tax_status_clarify",
                "step": step,
                "phase": "application",
            }
        # Keep existing selection when it already matches; otherwise set it.
        if taxLow_py(mapped) != taxLow_py(status_now):
            set_result = await _set_tax_status(page, mapped)
            ok = bool(
                set_result is True
                or (isinstance(set_result, dict) and set_result.get("ok"))
            )
            if not ok:
                stage = (
                    set_result.get("stage")
                    if isinstance(set_result, dict)
                    else "unknown"
                )
                reason = (
                    set_result.get("reason")
                    if isinstance(set_result, dict)
                    else ""
                )
                logger.warning(
                    "tax_status_set_failed member=%r status=%r stage=%s reason=%s",
                    current,
                    mapped,
                    stage,
                    reason,
                )
                speech = (
                    f"I couldn't select {mapped} for {current}. "
                    "Please say the filing status again."
                )
                _set_pending_question(session, step, speech)
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + speech,
                    "error": "tax_status_set_failed",
                    "step": step,
                    "phase": "application",
                }
        _set_tax_phase(session, "claim_deps")
        speech = _tax_claim_ask_speech(current)
        _set_pending_question(session, step, speech)
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "tax status answered",
            spoken_override=speech,
        )

    # ---- Phase: claim_deps (ask Q2 directly) ----
    if phase == "claim_deps":
        answer = str(modal.get("claim_deps") or "").strip()

        async def _claim_advance_to_claimed() -> dict[str, Any]:
            _set_tax_phase(session, "claimed_as")
            speech = _tax_claimed_ask_speech(current)
            _set_pending_question(session, step, speech)
            return _app_ok(
                call_id,
                session,
                text,
                step,
                "tax claim answered",
                spoken_override=speech,
            )

        async def _claim_set_yes() -> dict[str, Any]:
            if taxLow_py(answer) != "yes":
                ok = await _set_tax_yes_no(page, "claim_deps", "Yes")
                if not ok:
                    speech = (
                        f"I couldn't select Yes for dependents for {current}. "
                        "Please say Yes again."
                    )
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": "ASK: " + speech,
                        "error": "tax_claim_set_failed",
                        "step": step,
                        "phase": "application",
                    }
            opts = await _wait_tax_who_members(page, "claim_deps")
            if not opts:
                logger.info(
                    "claim_deps Yes with no Who? list; advancing member=%r",
                    current,
                )
                return await _claim_advance_to_claimed()
            _set_tax_phase(session, "claim_deps_who")
            listed = ", ".join(opts[:8])
            speech = (
                f"Who will {current} claim as a dependent? "
                f"Available members are {listed}. Say one or more names, then say done."
            )
            _set_pending_question(session, step, speech)
            return _app_ok(
                call_id, session, text, step, "tax claim yes", spoken_override=speech
            )

        async def _claim_set_no() -> dict[str, Any]:
            if taxLow_py(answer) != "no":
                ok = await _set_tax_yes_no(page, "claim_deps", "No")
                if not ok:
                    speech = (
                        f"I couldn't select No for dependents for {current}. "
                        "Please say No again."
                    )
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": "ASK: " + speech,
                        "error": "tax_claim_set_failed",
                        "step": step,
                        "phase": "application",
                    }
            return await _claim_advance_to_claimed()

        if _is_yes(text) or re.search(r"\byes\b", lowered):
            return await _claim_set_yes()
        if _is_no(text) or re.search(r"\bno\b", lowered):
            return await _claim_set_no()
        speech = _tax_claim_ask_speech(current)
        _set_pending_question(session, step, speech)
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "tax_claim_clarify",
            "step": step,
            "phase": "application",
        }

    # ---- Phase: claim_deps_who ----
    if phase == "claim_deps_who":
        modal = await _scrape_tax_modal(page)
        options = [str(x) for x in (modal.get("claim_deps_options") or []) if str(x).strip()]
        if not options:
            options = await _wait_tax_who_members(page, "claim_deps")
        if not options:
            logger.info(
                "claim_deps_who: no member list present; advancing member=%r",
                current,
            )
            _set_tax_phase(session, "claimed_as")
            speech = _tax_claimed_ask_speech(current)
            _set_pending_question(session, step, speech)
            return _app_ok(
                call_id,
                session,
                text,
                step,
                "tax claim who skipped empty",
                spoken_override=speech,
            )
        if done_who:
            checked = [
                str(x) for x in (modal.get("claim_deps_who") or []) if str(x).strip()
            ]
            if not checked:
                speech = (
                    f"Please name at least one dependent for {current}, "
                    "or say No if they will not claim dependents."
                )
                _set_pending_question(session, step, speech)
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + speech,
                    "error": "tax_claim_who_incomplete",
                    "step": step,
                    "phase": "application",
                }
            _set_tax_phase(session, "claimed_as")
            speech = _tax_claimed_ask_speech(current)
            _set_pending_question(session, step, speech)
            return _app_ok(
                call_id,
                session,
                text,
                step,
                "tax claim who done",
                spoken_override=speech,
            )
        if _is_no(text) and not _parse_member_names(text):
            await _set_tax_yes_no(page, "claim_deps", "No")
            _set_tax_phase(session, "claimed_as")
            speech = _tax_claimed_ask_speech(current)
            _set_pending_question(session, step, speech)
            return _app_ok(
                call_id,
                session,
                text,
                step,
                "tax claim who cancelled to no",
                spoken_override=speech,
            )
        chunks = _parse_member_names(text)
        picked = _match_who_names(chunks, options)
        if not picked:
            listed = ", ".join(options[:8]) if options else "the household members"
            speech = (
                f"I couldn't match that name. Available members are {listed}. "
                "Say one or more names, then say done."
            )
            _set_pending_question(session, step, speech)
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "tax_claim_who_clarify",
                "step": step,
                "phase": "application",
            }
        ok = await _set_tax_who_members(page, "claim_deps", picked)
        if not ok:
            speech = (
                "I couldn't select those dependents. "
                "Please say the names again, then say done."
            )
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "tax_claim_who_set_failed",
                "step": step,
                "phase": "application",
            }
        people = " and ".join(picked)
        speech = (
            f"Okay, {people} for {current}. "
            "Anyone else for this question? Say another name, or say done."
        )
        _set_pending_question(session, step, speech)
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "tax claim who selected",
            spoken_override=speech,
        )

    # ---- Phase: claimed_as (ask Q3 directly) ----
    if phase == "claimed_as":
        answer = str(modal.get("claimed_as") or "").strip()

        async def _claimed_set_yes() -> dict[str, Any]:
            if taxLow_py(answer) != "yes":
                ok = await _set_tax_yes_no(page, "claimed_as", "Yes")
                if not ok:
                    speech = (
                        f"I couldn't select Yes for who claims {current}. "
                        "Please say Yes again."
                    )
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": "ASK: " + speech,
                        "error": "tax_claimed_set_failed",
                        "step": step,
                        "phase": "application",
                    }
            opts = await _wait_tax_who_members(page, "claimed_as")
            if not opts:
                logger.info(
                    "claimed_as Yes with no Who? list; saving member=%r",
                    current,
                )
                return await _save_tax_member_and_continue(
                    call_id, session, page, text, step, cards or [], index, current
                )
            _set_tax_phase(session, "claimed_as_who")
            listed = ", ".join(opts[:8])
            speech = (
                f"Who will claim {current} as a dependent? "
                f"Available members are {listed}. Say one or more names, then say done."
            )
            _set_pending_question(session, step, speech)
            return _app_ok(
                call_id, session, text, step, "tax claimed yes", spoken_override=speech
            )

        async def _claimed_set_no() -> dict[str, Any]:
            if taxLow_py(answer) != "no":
                ok = await _set_tax_yes_no(page, "claimed_as", "No")
                if not ok:
                    speech = (
                        f"I couldn't select No for who claims {current}. "
                        "Please say No again."
                    )
                    return {
                        "ok": False,
                        "call_id": call_id,
                        "field_id": "application_answer",
                        "spoken": "ASK: " + speech,
                        "error": "tax_claimed_set_failed",
                        "step": step,
                        "phase": "application",
                    }
            return await _save_tax_member_and_continue(
                call_id, session, page, text, step, cards or [], index, current
            )

        if _is_yes(text) or re.search(r"\byes\b", lowered):
            return await _claimed_set_yes()
        if _is_no(text) or re.search(r"\bno\b", lowered):
            return await _claimed_set_no()
        speech = _tax_claimed_ask_speech(current)
        _set_pending_question(session, step, speech)
        return {
            "ok": False,
            "call_id": call_id,
            "field_id": "application_answer",
            "spoken": "ASK: " + speech,
            "error": "tax_claimed_clarify",
            "step": step,
            "phase": "application",
        }

    # ---- Phase: claimed_as_who ----
    if phase == "claimed_as_who":
        modal = await _scrape_tax_modal(page)
        options = [
            str(x) for x in (modal.get("claimed_as_options") or []) if str(x).strip()
        ]
        if not options:
            options = await _wait_tax_who_members(page, "claimed_as")
        if not options:
            # No member list for this Yes — finish the member and Save and Return.
            logger.info(
                "claimed_as_who: no member list present; saving member=%r",
                current,
            )
            return await _save_tax_member_and_continue(
                call_id, session, page, text, step, cards or [], index, current
            )
        if done_who:
            checked = [
                str(x) for x in (modal.get("claimed_as_who") or []) if str(x).strip()
            ]
            if not checked:
                speech = (
                    f"Please name at least one person who will claim {current}, "
                    "or say No if nobody will."
                )
                _set_pending_question(session, step, speech)
                return {
                    "ok": False,
                    "call_id": call_id,
                    "field_id": "application_answer",
                    "spoken": "ASK: " + speech,
                    "error": "tax_claimed_who_incomplete",
                    "step": step,
                    "phase": "application",
                }
            return await _save_tax_member_and_continue(
                call_id, session, page, text, step, cards or [], index, current
            )
        if _is_no(text) and not _parse_member_names(text):
            await _set_tax_yes_no(page, "claimed_as", "No")
            return await _save_tax_member_and_continue(
                call_id, session, page, text, step, cards or [], index, current
            )
        chunks = _parse_member_names(text)
        picked = _match_who_names(chunks, options)
        if not picked:
            listed = ", ".join(options[:8]) if options else "the household members"
            speech = (
                f"I couldn't match that name. Available members are {listed}. "
                "Say one or more names, then say done."
            )
            _set_pending_question(session, step, speech)
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "tax_claimed_who_clarify",
                "step": step,
                "phase": "application",
            }
        ok = await _set_tax_who_members(page, "claimed_as", picked)
        if not ok:
            speech = (
                "I couldn't select those members. "
                "Please say the names again, then say done."
            )
            return {
                "ok": False,
                "call_id": call_id,
                "field_id": "application_answer",
                "spoken": "ASK: " + speech,
                "error": "tax_claimed_who_set_failed",
                "step": step,
                "phase": "application",
            }
        people = " and ".join(picked)
        speech = (
            f"Okay, {people} will claim {current}. "
            "Anyone else for this question? Say another name, or say done."
        )
        _set_pending_question(session, step, speech)
        return _app_ok(
            call_id,
            session,
            text,
            step,
            "tax claimed who selected",
            spoken_override=speech,
        )

    # Fallback — restart member.
    if cards:
        speech = await _begin_tax_member(session, page, cards[0], intro=False)
        _set_pending_question(session, step, speech)
        return _app_ok(
            call_id, session, text, step, "tax restart", spoken_override=speech
        )
    speech = app_question_for_step(step, session)
    _set_pending_question(session, step, speech)
    return {
        "ok": False,
        "call_id": call_id,
        "field_id": "application_answer",
        "spoken": "ASK: " + speech,
        "error": "tax_clarify",
        "step": step,
        "phase": "application",
    }
