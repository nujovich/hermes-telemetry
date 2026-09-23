/**
 * dashboard/i18n.test.js — i18n round-trip + regression harness (shipped code).
 * Runnable: node --test dashboard/ (no deps, stdlib only)
 *
 * Loads SHIPPED code via vm in one context: i18n_ru.js + i18n.js.
 * No local copy of i18n_t — drift fails CI by construction.
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const dir = import.meta.dirname;
const ruSrc = fs.readFileSync(path.join(dir, 'i18n_ru.js'), 'utf8');
const implSrc = fs.readFileSync(path.join(dir, 'i18n.js'), 'utf8');
const html = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');

// One shared context: dict first, then implementation (needs i18nRU global).
const ctx = {};
vm.createContext(ctx);
vm.runInContext(ruSrc, ctx, { filename: 'i18n_ru.js' });
vm.runInContext(implSrc, ctx, { filename: 'i18n.js' });
const shipped = ctx.__i18n;
assert.ok(shipped && typeof shipped.i18n_t === 'function', 'shipped __i18n.i18n_t loaded');
const i18nRU = ctx.i18nRU ?? shipped.__RU;
assert.ok(i18nRU && typeof i18nRU === 'object', 'i18nRU loaded');
assert.ok(Object.keys(i18nRU).length > 100, 'dict not empty');

function ru(en) {
  shipped.lang = 'ru';
  return shipped.i18n_t(en);
}
function en(enVal) {
  shipped.lang = 'en';
  return shipped.i18n_t(enVal);
}

// Static literals i18n_t('...') / i18n_t("...") in index.html (no backticks).
function staticLiterals(src) {
  const re = /i18n_t\(\s*('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")\s*\)/g;
  const out = [];
  let m;
  while ((m = re.exec(src))) out.push(eval(m[1])); // eslint-disable-line no-eval
  return [...new Set(out)];
}

describe('i18n EN source -> RU overlay (shipped i18n.js)', () => {
  it('A1 RU OK→Успех / EN OK→OK', () => {
    assert.equal(en('OK'), 'OK');
    assert.equal(ru('OK'), 'Успех');
  });

  it('A2 Total→Итого', () => {
    assert.equal(en('Total'), 'Total');
    assert.equal(ru('Total'), 'Итого');
  });

  it('shadowing: every dynamic key V-substitutes, most-specific-first', () => {
    // __DYN precompiled most-specific-first (static length desc).
    // NOTE: shipped.__DYN lives in a vm realm — normalize to main-realm
    // arrays first, else deepStrictEqual fails on prototype, not values.
    const lens = [...shipped.__DYN.map((d) => d.statLen)];
    assert.deepEqual([...lens].sort((a, b) => b - a), lens, '__DYN sorted desc');
    const dynKeys = Object.keys(i18nRU).filter((k) => k.includes('${'));
    assert.ok(dynKeys.length > 10, 'dynamic keys present');
    for (const k of dynKeys) {
      const n = (k.match(/\$\{\d+\}/g) || []).length;
      const vals = Array.from({ length: n }, (_, i) => `V${i}`);
      let enStr = k;
      let ruStr = i18nRU[k];
      vals.forEach((v, i) => {
        enStr = enStr.split('${' + i + '}').join(v);
        ruStr = ruStr.split('${' + i + '}').join(v);
      });
      assert.equal(en(enStr), enStr, `EN passthrough: ${k}`);
      assert.equal(ru(enStr), ruStr, `RU dynamic: ${k}`);
    }
    // Regression spot: Window label with 3 groups.
    assert.equal(
      ru('Window: 2026-01-01 → 2026-01-02 (UTC)'),
      'Окно: 2026-01-01 → 2026-01-02 (UTC)',
    );
  });

  it('coverage: every static i18n_t literal in index.html', () => {
    const lits = staticLiterals(html);
    assert.ok(lits.length > 200, `literals found (${lits.length})`);
    const missing = [];
    for (const lit of lits) {
      assert.ok(!lit.includes('<'), `no markup literal: ${JSON.stringify(lit).slice(0, 120)}`);
      shipped.lang = 'ru';
      const got = shipped.i18n_t(lit);
      assert.equal(typeof got, 'string', `string result: ${lit}`);
      if (i18nRU[lit] != null) assert.equal(got, i18nRU[lit], `RU overlay: ${lit}`);
      else {
        assert.equal(got, lit, `EN fallback: ${lit}`);
        missing.push(lit);
      }
    }
    // Every static literal must have a RU key (reworded English = red build).
    assert.deepEqual(missing.sort(), [], `all static literals keyed, missing: ${JSON.stringify(missing)}`);
  });

  it('$& safety (function replacement)', () => {
    assert.equal(ru('Model: $&'), 'Модель: $&');
    assert.equal(ru('Model: $`'), 'Модель: $`');
    assert.equal(ru("Model: $'"), 'Модель: $\'');
  });

  it('chart leaf labels (no markup keys)', () => {
    assert.equal(ru('Model: Other'), 'Модель: другие');
    assert.equal(ru('Tokens'), 'Токены');
    assert.equal(ru('Cache Read'), 'Чтение кэша');
    for (const k of Object.keys(i18nRU)) {
      assert.ok(!k.includes('<'), `no markup key: ${JSON.stringify(k).slice(0, 120)}`);
    }
  });
});
