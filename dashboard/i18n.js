/* dashboard/i18n.js — EN source, RU overlay (see dashboard/i18n_ru.js). Shipped logic, shared by browser (<script src="/i18n.js">) and Node tests via vm. */
const __DASH_LANG_KEY = 'hermes_telemetry_lang';
const __RU = (typeof i18nRU !== 'undefined' && i18nRU) || {};
let __dashLang = 'en';
try { __dashLang = (typeof localStorage !== 'undefined' && localStorage.getItem(__DASH_LANG_KEY)) || 'en'; } catch (e) {}
if (typeof document !== 'undefined' && document.documentElement) document.documentElement.lang = __dashLang;
const __DYN = Object.keys(__RU).filter((k) => k.indexOf('${') !== -1).map((k) => {
  const statLen = k.replace(/\$\{[^}]*\}/g, '').length;
  const pattern = k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\\\$\\\{[^}]*\\\}/g, '(.*)');
  return { k, re: new RegExp('^' + pattern + '$'), statLen };
}).sort((a, b) => b.statLen - a.statLen);
function i18n_t(en) {
  if (__dashLang !== 'ru' || en == null) return en;
  if (__RU[en] != null) return __RU[en];
  for (const { k, re } of __DYN) {
    const m = en.match(re);
    if (m) {
      let out = __RU[k];
      m.slice(1).forEach((g, i) => { out = out.replace('${' + i + '}', () => g); });
      return out;
    }
  }
  return en;
}
globalThis.__i18n = { get lang() { return __dashLang; }, set lang(v) { __dashLang = v; }, i18n_t, __DYN, __RU, __DASH_LANG_KEY };
