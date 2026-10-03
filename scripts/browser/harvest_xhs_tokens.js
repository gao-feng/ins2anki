/*
 * Harvest Xiaohongshu `xsec_token` values for favorited notes.
 *
 * Why this is needed: a Xiaohongshu note URL only renders when it carries a
 * fresh `xsec_token`. The 收藏 page's <a href> values do NOT include one, and
 * the server-rendered state is an empty app shell, so the token can only be
 * read from the signed API responses the page itself makes while you scroll.
 *
 * Usage:
 *   1. Open your 收藏 page:
 *      https://www.xiaohongshu.com/user/profile/<uid>?tab=fav&subTab=note
 *   2. Paste this file into the browser console (type `allow pasting` first).
 *   3. It hooks fetch/XHR, auto-scrolls the list, and downloads
 *      `xhs-tokens.json` mapping note id -> xsec_token.
 *
 * Results accumulate in localStorage, so you can run it again (or click 笔记
 * and back to 收藏 to force page 1 to reload) and coverage will grow.
 */
(async () => {
  "use strict";

  const STORE_KEY = "__xhs_token_map_v1";
  const ROUNDS = 120;
  const DELAY = 1200;
  const ID_RE = /^[0-9a-f]{24}$/;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const stored = (() => {
    try {
      return JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    } catch {
      return {};
    }
  })();
  const tokens = new Map(Object.entries(stored));

  // Walk a parsed JSON response and pair every note object's id with its token.
  function walk(node) {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) {
      for (const item of node) walk(item);
      return;
    }
    const id = node.note_id ?? node.noteId ?? node.id;
    const token = node.xsec_token ?? node.xsecToken;
    if (typeof id === "string" && ID_RE.test(id) && typeof token === "string" && token.length > 16) {
      tokens.set(id, token);
    }
    for (const value of Object.values(node)) walk(value);
  }

  // Fallback for non-JSON payloads: pair a token with the nearest preceding id.
  function pairByProximity(text) {
    const re = /"xsec_?[Tt]oken"\s*:\s*"([^"]{16,})"/g;
    let match;
    while ((match = re.exec(text))) {
      const window = text.slice(Math.max(0, match.index - 3000), match.index);
      const ids = [...window.matchAll(/"(?:note_?[Ii]d|id)"\s*:\s*"([0-9a-f]{24})"/g)];
      if (ids.length) tokens.set(ids[ids.length - 1][1], match[1]);
    }
  }

  function scan(text, url) {
    if (!text || text.length < 20) return;
    const before = tokens.size;
    let parsed = null;
    try {
      parsed = JSON.parse(text);
    } catch {
      /* fall through to proximity pairing */
    }
    if (parsed) walk(parsed);
    else pairByProximity(text);
    if (tokens.size > before) {
      localStorage.setItem(STORE_KEY, JSON.stringify(Object.fromEntries(tokens)));
      console.log(`[xhs] +${tokens.size - before} tokens (total ${tokens.size}) from ${url.slice(0, 90)}`);
    }
  }

  const looksLikeApi = (url) =>
    typeof url === "string" && /xiaohongshu\.com/.test(url) && !/\.(js|css|png|jpg|webp|ico)(\?|$)/.test(url);

  const originalFetch = window.fetch;
  window.fetch = async (...args) => {
    const response = await originalFetch(...args);
    try {
      const url = typeof args[0] === "string" ? args[0] : args[0]?.url;
      if (looksLikeApi(url)) response.clone().text().then((t) => scan(t, url)).catch(() => {});
    } catch { /* ignore */ }
    return response;
  };

  const originalOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.addEventListener("load", () => {
      try {
        if (looksLikeApi(String(url))) scan(this.responseText, String(url));
      } catch { /* ignore */ }
    });
    return originalOpen.call(this, method, url, ...rest);
  };

  // Also pick up any token that does appear in a link.
  function domScan() {
    for (const anchor of document.querySelectorAll("a[href]")) {
      const abs = new URL(anchor.getAttribute("href"), location.origin);
      const match = abs.href.match(/\/(?:explore|discovery\/item)\/([0-9a-f]{24})/);
      const token = abs.searchParams.get("xsec_token");
      if (match && token) tokens.set(match[1], token);
    }
  }

  console.log("[xhs] hooks installed; scrolling to trigger the list API…");
  for (let round = 1, stable = 0; round <= ROUNDS && stable < 4; round++) {
    const before = tokens.size;
    for (const el of document.querySelectorAll("div,main,section")) {
      if (el.scrollHeight > el.clientHeight + 200) el.scrollTop = el.scrollHeight;
    }
    window.scrollTo(0, document.body.scrollHeight);
    domScan();
    await sleep(DELAY);
    stable = tokens.size === before ? stable + 1 : 0;
    if (round % 5 === 0 || stable) console.log(`[xhs] round ${round}: ${tokens.size} tokens`);
  }

  localStorage.setItem(STORE_KEY, JSON.stringify(Object.fromEntries(tokens)));
  const text = JSON.stringify(Object.fromEntries(tokens), null, 2);
  console.log(`[xhs] harvested ${tokens.size} tokens`);
  if (!tokens.size) {
    console.warn(
      "[xhs] no tokens captured. The list API had already loaded before the hook " +
        "was installed — click 笔记, then click back to 收藏, and run this again."
    );
    return null;
  }

  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([text], { type: "application/json" }));
  link.download = "xhs-tokens.json";
  link.click();
  URL.revokeObjectURL(link.href);
  return Object.fromEntries(tokens);
})();
