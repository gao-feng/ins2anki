/*
 * Export a favorites/collection inventory from a logged-in browser session.
 *
 * yt-dlp cannot enumerate Instagram saved collections, Xiaohongshu favorites,
 * or Douyin collections, so the list of posts has to come from the page you are
 * already logged into. Paste this whole file into the browser console while the
 * collection is open.
 *
 *   Xiaohongshu : https://www.xiaohongshu.com/user/profile/<uid>  -> 收藏 tab
 *   Douyin      : https://www.douyin.com/user/self?showTab=favorite_collection
 *   Instagram   : https://www.instagram.com/<user>/saved/<collection-id>/
 *
 * Optional, set before running to name the collection explicitly:
 *
 *   window.__FAVORITES_NAME = "英语";
 *   window.__FAVORITES_DELAY = 1200;   // ms between scroll rounds
 *
 * The script auto-scrolls until no new links appear, merges the result into
 * localStorage (so you can run it once per collection and accumulate), then
 * logs the merged inventory, copies it to the clipboard, and downloads it as
 * `favorites-inventory.json`.
 */
(async () => {
  "use strict";

  const STORE_KEY = "__favorites_inventory_v1";
  const ROUNDS = 60;
  const DELAY = Number(window.__FAVORITES_DELAY) || 1000;

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function detectPlatform() {
    const host = location.hostname.replace(/^www\./, "");
    if (host.endsWith("instagram.com")) return "instagram";
    if (host.endsWith("xiaohongshu.com") || host.endsWith("xhslink.com")) {
      return "xiaohongshu";
    }
    if (host.endsWith("douyin.com") || host.endsWith("iesdouyin.com")) {
      return "douyin";
    }
    throw new Error(`unsupported host: ${location.hostname}`);
  }

  const PATTERNS = {
    instagram: [
      { re: /instagram\.com\/(?:p|reel|tv)\/([A-Za-z0-9_-]+)/i },
    ],
    xiaohongshu: [
      { re: /xiaohongshu\.com\/(?:explore|discovery\/item)\/([\da-fA-F]{8,32})/i },
    ],
    douyin: [
      { re: /douyin\.com\/(?:share\/)?video\/(\d+)/i, path: "video" },
      { re: /douyin\.com\/note\/(\d+)/i, path: "note" },
    ],
  };

  // Canonicalize one raw href, keeping only the parameters each platform needs.
  function canonicalize(href, platform) {
    const absolute = new URL(href, location.origin).href;
    for (const { re, path } of PATTERNS[platform]) {
      const match = absolute.match(re);
      if (!match) continue;
      const id = match[1];
      if (platform === "instagram") {
        return { id, url: `https://www.instagram.com/p/${id}/` };
      }
      if (platform === "xiaohongshu") {
        const source = new URL(absolute);
        const token = source.searchParams.get("xsec_token");
        const origin = source.searchParams.get("xsec_source");
        const query = new URLSearchParams();
        if (token) query.set("xsec_token", token);
        if (origin) query.set("xsec_source", origin);
        const suffix = query.toString() ? `?${query}` : "";
        return { id, url: `https://www.xiaohongshu.com/explore/${id}${suffix}` };
      }
      return { id, url: `https://www.douyin.com/${path}/${id}` };
    }
    return null;
  }

  function scrollableElements() {
    return Array.from(document.querySelectorAll("div, main, section")).filter(
      (el) => {
        if (el.scrollHeight <= el.clientHeight + 200) return false;
        const overflow = getComputedStyle(el).overflowY;
        return overflow === "auto" || overflow === "scroll";
      }
    );
  }

  function collect(platform, seen) {
    for (const anchor of document.querySelectorAll("a[href]")) {
      const entry = canonicalize(anchor.getAttribute("href"), platform);
      if (entry) seen.set(entry.id, entry.url);
    }
    return seen;
  }

  function collectionName(platform) {
    const override = (window.__FAVORITES_NAME || "").trim();
    if (override) return override;
    const title = (document.title || "")
      .replace(/[-–|·].*$/, "")
      .replace(/\s+/g, " ")
      .trim();
    return title || `${platform}-favorites`;
  }

  const platform = detectPlatform();
  const seen = new Map();
  let stableRounds = 0;

  for (let round = 1; round <= ROUNDS; round++) {
    const before = seen.size;
    for (const el of scrollableElements()) el.scrollTop = el.scrollHeight;
    window.scrollTo(0, document.body.scrollHeight);
    collect(platform, seen);
    console.log(
      `[favorites] round ${round}: ${seen.size} links (+${seen.size - before})`
    );
    if (seen.size === before) {
      stableRounds += 1;
      if (stableRounds >= 3) break;
    } else {
      stableRounds = 0;
    }
    await sleep(DELAY);
  }

  const entry = {
    name: collectionName(platform),
    platform,
    url: location.href,
    posts: Array.from(seen.values()),
  };

  let store = { version: 1, collections: [] };
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && Array.isArray(parsed.collections)) store = parsed;
    }
  } catch (err) {
    console.warn("[favorites] ignoring unreadable stored inventory", err);
  }

  const existing = store.collections.findIndex(
    (item) => item.name === entry.name && item.platform === entry.platform
  );
  if (existing >= 0) store.collections[existing] = entry;
  else store.collections.push(entry);
  store.exported_at = new Date().toISOString();
  localStorage.setItem(STORE_KEY, JSON.stringify(store));

  const text = JSON.stringify(store, null, 2);
  console.log(
    `[favorites] collected ${entry.posts.length} posts for "${entry.name}" ` +
      `(${platform}); inventory now holds ${store.collections.length} collection(s).`
  );
  console.log(text);

  try {
    await navigator.clipboard.writeText(text);
    console.log("[favorites] inventory copied to the clipboard.");
  } catch (err) {
    console.warn("[favorites] clipboard unavailable; copy the JSON above.", err);
  }

  const blob = new Blob([text], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "favorites-inventory.json";
  link.click();
  URL.revokeObjectURL(link.href);

  return store;
})();
