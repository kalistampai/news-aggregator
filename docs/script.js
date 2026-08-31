/* DISPATCH — reads the briefing, its dated archives, and the feed health reports
   from Supabase Postgres and renders the board. Read-only: the key below is the
   ANON key, which Row Level Security limits to SELECT on two tables.
   Two API calls pull every day at once, so filtering, collapsing, ranking,
   diffing and day-flipping all happen locally with no further requests. */

/* ======================== SUPABASE CONFIGURATION =========================== */
const CONFIG = {
  // Your project's REST endpoint, from Supabase -> Project Settings -> API.
  SUPABASE_URL: "https://baiojghilzxhkebfblzv.supabase.co",

  // The ANON (publishable) key — NOT the service_role key.
  //
  // Publishing this in a public repo is correct and intended: the anon key
  // identifies the project and carries the `anon` Postgres role, and Row Level
  // Security is what decides what that role may do. The schema migration grants
  // it SELECT on `briefings` and `feed_reports` and nothing else — no insert,
  // no update, no sight of `seen_articles`.
  //
  // That guarantee is entirely load-bearing. With RLS disabled on those tables
  // this key can become public WRITE access. If you ever add a table the board
  // reads, enable RLS and grant only the required operations in the same breath.
  //
  // The service_role key bypasses RLS. It belongs in GitHub Actions secrets for
  // the pipeline, and must never appear in this file.
  SUPABASE_ANON_KEY: "sb_publishable_nfLVr5Krdld9pxxr4f2CYQ_bsn0TNxx",

  // This shared Supabase project isolates each application in its own schema.
  // Raw PostgREST requests select it through the Accept-Profile header.
  SUPABASE_SCHEMA: "news_aggregator",

  // Days of archive to pull on load. The pipeline prunes at ARCHIVE_KEEP_DAYS
  // (30), so this only needs to match it.
  ARCHIVE_DAYS: 30,

  // IANA zone for all displayed timestamps. America/Los_Angeles switches between
  // PST and PDT automatically, so the label is always correct.
  TZ: "America/Los_Angeles",
};
/* ========================================================================== */

/* Bump on every paired HTML/JS change. index.html carries the same string on
   <body data-build>. A mismatch means one of the two files is stale — usually a
   cached script.js on GitHub Pages — which is exactly how a removed control ends
   up referenced by old code and throws "Cannot set properties of null". */
const BUILD = "2026-08-30e";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* Missing-element-tolerant helpers. Optional UI controls must never be able to
   abort boot(): one null lookup used to jump straight to the catch block and
   render the whole dashboard as OFFLINE, hiding a briefing that had loaded fine. */
const MISSING = [];

function el(sel) {
  const node = $(sel);
  if (node) return node;
  if (!MISSING.includes(sel)) MISSING.push(sel);
  // Detached stand-in: textContent / innerHTML / dataset / hidden / classList /
  // appendChild / addEventListener all work and affect nothing. Turns "control
  // was removed from the markup" from a fatal TypeError into a no-op.
  return document.createElement("span");
}

function on(sel, type, handler, opts) {
  const node = $(sel);
  if (node) node.addEventListener(type, handler, opts);
  else if (!MISSING.includes(sel)) MISSING.push(sel);
  return node;
}

function setProp(sel, prop, value) {
  const node = $(sel);
  if (node) node[prop] = value;
  else if (!MISSING.includes(sel)) MISSING.push(sel);
  return node;
}
const board = $("#board");
if (!board) {
  console.error('[DISPATCH] #board is missing from index.html — nothing can render. ' +
                'index.html and script.js are out of sync.');
}

let STORE = { dates: [], byDate: {}, reports: {} };   // dates sorted newest-first
let currentIndex = 0;
let QUERY = "";           // active keyword filter, persists across days
let MIN_SCORE = 0;        // score threshold, persists across days
let COLLAPSED = new Set();// category names collapsed by the user
let LB_SORT = "features"; // leaderboard sort key
let LB_ALL_DAYS = false;  // leaderboard scope
let DIFF_AGAINST = null;  // date the diff panel compares to
let ACTIVE_TAB = "all";   // category tab; "all" shows every section
let SHOW_ALL_NOTABLE = false;
let CARD_SEQ = 0;
const EXPANDED_CARDS = new Set();

/* ------------------------------ preferences ------------------------------ */
/* This is a real static site (not a sandboxed artifact), so localStorage is
   available and is the right place for view preferences. Every read/write is
   guarded: private-mode Safari and disabled-storage browsers throw on access. */
const PREF_KEY = "dispatch.prefs.v1";

function loadPrefs() {
  try {
    const raw = localStorage.getItem(PREF_KEY);
    if (!raw) return {};
    const p = JSON.parse(raw);
    return p && typeof p === "object" ? p : {};
  } catch (_) { return {}; }
}

function savePrefs(patch) {
  try {
    const next = { ...loadPrefs(), ...patch };
    localStorage.setItem(PREF_KEY, JSON.stringify(next));
  } catch (_) { /* storage unavailable — preferences just won't persist */ }
}

/* --------------------------- model attribution --------------------------- */
/* WHICH MODEL ACTUALLY RAN — not which one is configured.
   editor.py writes briefing.models.{editor,gatekeeper} AFTER a successful
   response, so `effective` names the model that produced the cards on screen.
   It differs from `configured` exactly when the pipeline failed over, which is
   the moment this label earns its keep.

   DISPLAY ONLY. MODEL_LABEL and the localStorage copy below are never read by
   anything that decides what to call: routing lives in pipeline/llm.py and is
   session-only (in-memory), so a model that was merely busy yesterday is still
   tried FIRST on the next run. Persisting the display value here is what keeps
   the label truthful across a reload — including a reload that cannot reach
   Supabase at all. */
const MODEL_KEY = "dispatch.model.v1";
let MODEL_LABEL = null;     // what the label currently shows
let MODEL_STORED = null;    // last value read from / written to localStorage

/* Read once and keep it in memory: render() runs on every keystroke in the
   search box, and storage access there would be pure waste. */
function loadModelLabel() {
  try {
    const raw = localStorage.getItem(MODEL_KEY);
    const v = raw ? JSON.parse(raw) : null;
    MODEL_STORED = v && typeof v === "object" && v.model ? v : null;
  } catch (_) { MODEL_STORED = null; }
  return MODEL_STORED;
}

function saveModelLabel(info) {
  if (MODEL_STORED && MODEL_STORED.model === info.model &&
      MODEL_STORED.date === info.date) return;          // unchanged
  MODEL_STORED = info;
  try { localStorage.setItem(MODEL_KEY, JSON.stringify(info)); }
  catch (_) { /* storage unavailable — the label just won't survive a reload */ }
}

/* Token totals for one stage, or null when the pipeline recorded none. Guards
   every field: a briefing from before token recording has no `usage` key at
   all, and a provider that returns no counts leaves it null on purpose. */
function usageOf(stage) {
  const u = stage && stage.usage;
  if (!u || typeof u !== "object") return null;
  const n = (v) => (Number.isFinite(v) && v >= 0 ? v : 0);
  const total = n(u.total) || n(u.prompt) + n(u.completion);
  if (!total) return null;
  return {
    prompt: n(u.prompt), completion: n(u.completion),
    reasoning: n(u.reasoning), total, calls: n(u.calls),
  };
}

/* Pull the attribution out of a briefing. Returns null for briefings archived
   before the pipeline recorded it. */
function modelInfoOf(data) {
  const m = data && data.models;
  const ed = m && m.editor;
  if (!ed || !ed.effective) return null;
  const gk = (m.gatekeeper || {});
  const edUsage = usageOf(ed), gkUsage = usageOf(gk);
  // The day's cost is both stages together — scoring every article is usually
  // the larger half, so showing only the editor's spend would understate it.
  const tokens = (edUsage || gkUsage) ? {
    editor: edUsage,
    gatekeeper: gkUsage,
    total: (edUsage ? edUsage.total : 0) + (gkUsage ? gkUsage.total : 0),
    reasoning: (edUsage ? edUsage.reasoning : 0) +
               (gkUsage ? gkUsage.reasoning : 0),
    calls: (edUsage ? edUsage.calls : 0) + (gkUsage ? gkUsage.calls : 0),
  } : null;
  // `primary` is the first-choice id AFTER the pipeline resolved it against the
  // provider's real model list, so a configured family name that resolved to a
  // concrete id is not mistaken for a failover. Older briefings carry only
  // `configured`, hence the fallback comparison.
  const first = ed.primary || ed.configured || ed.effective;
  return {
    model: ed.effective,
    configured: ed.configured || ed.effective,
    primary: first,
    fellBack: typeof ed.fell_back === "boolean"
      ? ed.fell_back
      : first !== ed.effective,
    gatekeeper: gk.effective || null,
    gatekeeperConfigured: gk.primary || gk.configured || null,
    alerts: Array.isArray(m.alerts) ? m.alerts.slice(0, 4) : [],
    events: Array.isArray(m.events) ? m.events.slice(0, 6) : [],
    tokens,
    date: data.date || "",
  };
}

/* ONE element, two jobs (the masthead already had a status tag; this is its
   sibling for model state): busy text while a fetch is in flight, the model
   label the rest of the time. */
function setModelBusy(text) {
  const node = el("#modelTag");
  node.textContent = text || "working…";
  node.dataset.state = "busy";
  node.title = "Fetching the briefing — the model that produced it is shown here " +
               "once it loads.";
  el("#tokenTag").hidden = true;   // nothing to count until a briefing loads
}

/* Compact so the masthead stays one line at phone width: a day's run is
   typically six or seven figures, and the exact digit count is in the tooltip
   for anyone who wants it. */
function fmtTokens(n) {
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 2).replace(/\.?0+$/, "") + "M";
  if (n >= 1e4) return Math.round(n / 1e3) + "K";
  return n.toLocaleString("en-US");
}

/* The token counter, rendered as a sibling of the model tag rather than as part
   of its text: the model tag turns red on a failover or an alert, and the cost
   of the run is not itself a fault condition. Driven only from renderModelTag()
   so the two can never disagree about which day is on screen. */
function renderTokenTag(info, { stale = false } = {}) {
  const node = el("#tokenTag");
  const t = info && info.tokens;
  if (!t || !t.total) {
    // Two different silences, and neither is "0 tokens": briefings archived
    // before token recording landed, and providers that returned no usage.
    node.hidden = true;
    node.textContent = "";
    node.removeAttribute("title");
    return;
  }
  node.hidden = false;
  node.textContent = `${fmtTokens(t.total)} tokens`;
  node.dataset.state = stale ? "stale" : "ok";

  const exact = (n) => n.toLocaleString("en-US");
  const lines = [`Tokens used to produce this briefing: ${exact(t.total)}`];
  const perStage = [["Editor (writes the cards)", t.editor],
                    ["Gatekeeper (scores every article)", t.gatekeeper]];
  for (const [label, u] of perStage) {
    if (!u) continue;
    lines.push(`  ${label}: ${exact(u.total)}` +
               (u.calls ? ` over ${u.calls} request${u.calls === 1 ? "" : "s"}` : ""));
  }
  lines.push("", `Input: ${exact((t.editor ? t.editor.prompt : 0) +
                                 (t.gatekeeper ? t.gatekeeper.prompt : 0))}`,
             `Output: ${exact((t.editor ? t.editor.completion : 0) +
                              (t.gatekeeper ? t.gatekeeper.completion : 0))}`);
  if (t.reasoning) {
    lines.push(`  of which reasoning: ${exact(t.reasoning)} ` +
               "(billed as output, already counted above)");
  }
  // Said plainly rather than implied, because the number looks like a bill and
  // is not one: a retried request that errored was charged but reported nothing.
  lines.push("", "Counts successful responses only, so a night with retries " +
                 "reads slightly low. Not a billing figure.");
  if (stale) lines.push("", "From an earlier visit — no fresh briefing loaded.");
  node.title = lines.join("\n");
}

function renderModelTag(info, { stale = false } = {}) {
  const node = el("#modelTag");
  if (!info) {
    node.textContent = "model not recorded";
    node.dataset.state = "unknown";
    node.title = "This briefing was archived before the pipeline recorded which " +
                 "model answered. Newer days show the model that produced them.";
    renderTokenTag(null);
    return;
  }
  MODEL_LABEL = info;
  renderTokenTag(info, { stale });
  const alerted = (info.alerts || []).length > 0;
  node.textContent = `via ${info.model}` +
    (info.fellBack ? " (fallback)" : "") + (alerted ? " ⚠" : "");
  // An alert outranks everything else: the briefing shipped, but something in
  // the chain is broken and will stay broken until someone fixes it.
  node.dataset.state = alerted ? "alert"
    : stale ? "stale" : (info.fellBack ? "fallback" : "ok");

  const lines = [];
  if (alerted) lines.push("ACTION NEEDED:", ...info.alerts, "");
  lines.push(
    `Briefing written by: ${info.model}`,
    `First choice this run: ${info.primary || info.configured}` +
      (info.configured && info.primary && info.configured !== info.primary
        ? ` (configured as ${info.configured})` : ""),
    info.fellBack
      ? `FAILED OVER — ${info.primary || info.configured} was unavailable, so ` +
        `${info.model} produced this output.`
      : "No failover: the first-choice model answered.",
  );
  if (info.gatekeeper) {
    lines.push(`Scoring (gatekeeper): ${info.gatekeeper}` +
      (info.gatekeeperConfigured && info.gatekeeperConfigured !== info.gatekeeper
        ? ` (configured: ${info.gatekeeperConfigured})` : ""));
  }
  if (info.events.length) lines.push("", ...info.events);
  if (stale) {
    lines.push("", `Last known value, from ${info.date || "an earlier visit"} — ` +
                   "this page could not load a fresh briefing.");
  }
  node.title = lines.join("\n");
}

/* ------------------------------- time ------------------------------------ */
/* Source timestamps are UTC ISO-8601 (editor.py writes datetime.now(timezone.utc)).
   Rendered in Pacific with an explicit PST/PDT label, so there is never any
   ambiguity about which offset was in effect on a given day. */
function fmtPacific(iso, { withDate = true } = {}) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const opts = {
    timeZone: CONFIG.TZ,
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    hour12: false, timeZoneName: "short",
  };
  if (withDate) {
    opts.weekday = "short"; opts.month = "short";
    opts.day = "2-digit"; opts.year = "numeric";
  }
  try {
    return new Intl.DateTimeFormat("en-US", opts).format(d);
  } catch (_) {
    return d.toUTCString().replace("GMT", "UTC");   // very old browsers
  }
}

function utcTitle(iso) {
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "" : `source timestamp: ${d.toISOString()} (UTC)`;
}

/* ==================================================================== auth ==
   Supabase Auth against the shared project. Migration 004 moved SELECT on
   briefings/feed_reports from `anon` to `authenticated`, so the session is not
   decoration: without a user JWT every table read comes back empty-handed.

   The session lives in localStorage under supabase-js's own key and is
   refreshed automatically, so a returning visitor lands straight on the board. */
let sbClient = null;
let SESSION = null;

function supa() {
  if (sbClient) return sbClient;
  if (!window.supabase || !window.supabase.createClient) return null;
  sbClient = window.supabase.createClient(
    CONFIG.SUPABASE_URL, CONFIG.SUPABASE_ANON_KEY,
    {
      db: { schema: CONFIG.SUPABASE_SCHEMA },
      auth: { persistSession: true, autoRefreshToken: true },
    });
  return sbClient;
}

/* The header pair every PostgREST call needs. Falls back to the publishable key
   as the bearer when signed out — that request is *meant* to return nothing, and
   an absent Authorization header would be a 401 that reads like a broken
   deployment rather than "you are not signed in". */
function authHeaders() {
  const token = SESSION?.access_token || CONFIG.SUPABASE_ANON_KEY;
  return {
    apikey: CONFIG.SUPABASE_ANON_KEY,
    Authorization: `Bearer ${token}`,
    "Accept-Profile": CONFIG.SUPABASE_SCHEMA,
    "Content-Profile": CONFIG.SUPABASE_SCHEMA,
  };
}

async function loadSession() {
  const client = supa();
  if (!client) throw new Error(
    "The Supabase client library did not load, so signing in is impossible. " +
    "Check the CDN <script> tag in index.html.");
  const { data, error } = await client.auth.getSession();
  if (error) throw new Error(error.message);
  SESSION = data?.session || null;
  return SESSION;
}

async function signIn(email, password) {
  const client = supa();
  if (!client) return { ok: false, error: "Supabase client failed to load." };
  const { data, error } = await client.auth.signInWithPassword({ email, password });
  if (error) return { ok: false, error: error.message };
  SESSION = data.session;
  return { ok: true };
}

/* Sign out MUST end with a signed-out browser, whatever the network says.
   Two ways this used to fail silently, both reported as "the button does nothing":

     1. `auth.signOut()` defaults to scope:'global' — a round trip to
        /auth/v1/logout. Offline, a 403, or an already-expired token rejects the
        promise, and the un-caught rejection skipped location.reload() entirely.
     2. Reloading anyway after a failed server revoke left the session sitting in
        localStorage, so the reload signed you straight back in.

   So: try the server revoke, fall back to a local-only revoke (no network), then
   purge the persisted token by hand, and reload no matter which step got there. */
async function signOut() {
  const button = $("#signOutBtn");
  if (button) { button.disabled = true; button.textContent = "…"; }

  const client = supa();
  try {
    if (client) await client.auth.signOut();
  } catch (globalErr) {
    console.warn("[DISPATCH] server sign-out failed, revoking locally:", globalErr);
    try {
      // scope:'local' only clears this browser — it cannot fail on the network.
      if (client) await client.auth.signOut({ scope: "local" });
    } catch (localErr) {
      console.warn("[DISPATCH] local sign-out failed, purging storage:", localErr);
    }
  }

  // Belt and braces: supabase-js persists under `sb-<project-ref>-auth-token`.
  // Matched by pattern rather than a derived name so a change to that scheme
  // cannot quietly resurrect the session on reload.
  try {
    for (const key of Object.keys(localStorage)) {
      if (/^sb-.*-auth-token$/.test(key)) localStorage.removeItem(key);
    }
  } catch (storageErr) {                    // private mode / blocked site data
    console.warn("[DISPATCH] could not clear stored session:", storageErr);
  }

  SESSION = null;
  location.reload();
}

function showGate(message) {
  document.body.classList.add("is-locked");
  setProp("#authScreen", "hidden", false);
  if (message) {
    setProp("#authError", "textContent", message);
    setProp("#authError", "hidden", false);
  }
  const email = el("#authEmail");
  if (email && email.focus) setTimeout(() => email.focus(), 40);
}

function hideGate() {
  document.body.classList.remove("is-locked");
  setProp("#authScreen", "hidden", true);
}

/* ------------------------------ data loading ----------------------------- */
/* Two REST reads pull every archived day at once — briefings and feed reports —
   so filtering, collapsing, ranking, diffing and day-flipping all happen locally
   with no further requests. PostgREST sends CORS headers, so this works from
   GitHub Pages with no proxy and no rate limit per visitor IP. */
async function fetchTable(table) {
  const url = `${CONFIG.SUPABASE_URL}/rest/v1/${table}` +
              `?select=date,payload&order=date.desc&limit=${CONFIG.ARCHIVE_DAYS}`;
  const r = await fetch(url, {
    /* `apikey` stays the publishable key — it identifies the project. The
       Authorization bearer is now the USER's JWT, not that same key: since
       migration 004 the anon role has no SELECT on these tables, so a request
       carrying the publishable key in both slots reads nothing. That is the
       intended behaviour, and it is what makes the sign-in screen real rather
       than cosmetic. */
    headers: { ...authHeaders(), Accept: "application/json" },
    cache: "no-store",
  });
  if (r.ok) return r.json();

  /* PostgREST answers failures with {message, hint, code}. Surfacing it turns
     the two setup mistakes that actually happen — a wrong key, and RLS enabled
     with no SELECT policy — from a blank board into a sentence naming the
     cause. Note that a missing SELECT policy is NOT an error: RLS filters the
     rows away and returns 200 with []. That case is handled in boot(). */
  let detail = "";
  try { detail = (await r.json()).message || ""; } catch (_) { /* not JSON */ }
  const because = detail ? ` — ${detail}` : "";
  if (r.status === 401 || r.status === 403) {
    throw new Error(`Supabase refused the read (${r.status}). The session may ` +
      `have expired — sign out and back in.${because}`);
  }
  if (r.status === 404) {
    throw new Error(`Supabase cannot expose "${CONFIG.SUPABASE_SCHEMA}.${table}" ` +
      `(404). Confirm the schema is listed in Data API → Exposed schemas.${because}`);
  }
  throw new Error(`Could not read "${table}" from Supabase (${r.status})${because}.`);
}

async function buildStore() {
  if (!CONFIG.SUPABASE_URL || CONFIG.SUPABASE_URL.includes("YOUR-PROJECT-ID") ||
      CONFIG.SUPABASE_ANON_KEY.startsWith("PUT_YOUR_") || !CONFIG.SUPABASE_SCHEMA) {
    throw new Error("Supabase is not configured — set SUPABASE_URL, " +
                    "SUPABASE_ANON_KEY, and SUPABASE_SCHEMA at the top of script.js.");
  }

  /* Feed health is secondary furniture: the health panel, leaderboard and diff
     all degrade to hidden without it. It must never be able to blank a briefing
     that loaded fine — the same reasoning as the UI-wiring try/catch in boot(). */
  const [briefRows, reportRows] = await Promise.all([
    fetchTable("briefings"),
    fetchTable("feed_reports").catch((e) => {
      console.warn("[DISPATCH] feed health unavailable:", e.message);
      return [];
    }),
  ]);

  const byDate = {}, reports = {};
  for (const row of briefRows) if (row && row.payload) byDate[row.date] = row.payload;
  for (const row of reportRows) if (row && row.payload) reports[row.date] = row.payload;

  const dates = Object.keys(byDate).sort((a, b) => (a < b ? 1 : a > b ? -1 : 0));
  return { dates, byDate, reports };
}

/* ---------------------------------- tabs ---------------------------------- */
/* Canonical running order, mirroring prompts/editor.txt. Categories the editor
   emits that are not in this list (e.g. the "Unsorted" degraded bucket) sort to
   the end alphabetically rather than being dropped. */
/* First three are the operator's standing priorities, highest first. */
const CATEGORY_ORDER = [
  "Scams & Fraud", "AI News", "Offensive Security", "AI / ML", "OSINT & Recon",
  "Hardware & SDR", "Linux & Homelab", "Finance", "DIY & Self-Reliance",
];

function orderCategories(names) {
  const rank = (n) => {
    const i = CATEGORY_ORDER.indexOf(n);
    return i === -1 ? CATEGORY_ORDER.length : i;
  };
  return [...names].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}

/* Tabs are built from the day's UNFILTERED data so the bar does not reshuffle
   while you type in the search box; counts reflect the active search/score
   filter so an empty tab is visibly empty before you click it. */
function renderTabs(dayData, filteredCats) {
  const bar = el("#tabBar");
  const cats = (dayData && dayData.categories) || {};
  const names = orderCategories(
    Object.keys(cats).filter((k) => (cats[k] || []).length));

  if (names.length < 2) {                 // nothing to switch between
    bar.hidden = true;
    bar.innerHTML = "";
    return;
  }
  bar.hidden = false;

  if (ACTIVE_TAB !== "all" && !names.includes(ACTIVE_TAB)) ACTIVE_TAB = "all";

  const countOf = (n) => (filteredCats && filteredCats[n] ? filteredCats[n].length : 0);
  const totalShown = names.reduce((s, n) => s + countOf(n), 0);

  const tab = (key, label, count) => {
    const on = ACTIVE_TAB === key;
    return `<button type="button" class="tab${on ? " is-on" : ""}" ` +
      `data-tab="${escapeHtml(key)}" role="tab" aria-selected="${on}"` +
      `${count === 0 ? ' data-empty="true"' : ""}>` +
      `<span class="tab__label">${escapeHtml(label)}</span>` +
      `<span class="tab__count">${count}</span></button>`;
  };

  bar.innerHTML =
    `<span class="tab__lead">// sections</span>` +
    tab("all", "All", totalShown) +
    names.map((n) => tab(n, n, countOf(n))).join("");

  requestAnimationFrame(syncTabOverflow);
}

function syncTabOverflow() {
  const bar = $("#tabBar");
  if (!bar || bar.hidden) return;
  const max = Math.max(0, bar.scrollWidth - bar.clientWidth);
  bar.dataset.overflowLeft = String(max > 4 && bar.scrollLeft > 4);
  bar.dataset.overflowRight = String(max > 4 && bar.scrollLeft < max - 4);
}

function revealActiveTab() {
  const bar = $("#tabBar"), active = bar && $(".tab.is-on", bar);
  if (!bar || !active) return;
  const left = active.offsetLeft - 16;
  const right = active.offsetLeft + active.offsetWidth + 16;
  if (left < bar.scrollLeft) bar.scrollTo({ left, behavior: "smooth" });
  else if (right > bar.scrollLeft + bar.clientWidth) {
    bar.scrollTo({ left: right - bar.clientWidth, behavior: "smooth" });
  }
  requestAnimationFrame(syncTabOverflow);
}

function setActiveTab(key) {
  ACTIVE_TAB = key || "all";
  savePrefs({ activeTab: ACTIVE_TAB });
  applyFilter();
  revealActiveTab();
  const board = el("#board");
  if (board && board.scrollIntoView) {
    board.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

/* -------------------------- filtering (search + score) -------------------- */
/* Multi-term AND matching. "cve linux" matches items containing BOTH, in any
   field. Purely client-side over the already-loaded day — no extra requests. */
function tokens(q) {
  return String(q || "").toLowerCase().split(/\s+/).filter(Boolean);
}

function cardHaystack(item) {
  return [item.title, item.source, item.reasoning, ...(item.bullets || [])]
    .join(" ").toLowerCase();
}

function notableHaystack(item) {
  return [item.title, item.source].join(" ").toLowerCase();
}

function matchesAll(hay, toks) {
  return toks.every((t) => hay.includes(t));
}

/* null (not 0) when a briefing predates scored also_notable, so "unknown" is
   distinguishable from "genuinely scored zero". */
function scoreOf(item) {
  return typeof item.score === "number" ? item.score : null;
}

/* Returns a briefing-shaped object containing only matching items.

   SCORE + NOTABLE: editor.py now writes also_notable with {score, tier}, so the
   threshold applies to notable items exactly as it does to feature cards. This
   matters most on busy days: items above MAX_FEATURES spill into also_notable
   while still being feature-tier (score >= 7), and the old "hide all notable"
   behaviour threw away the highest-scoring spillover.

   Briefings archived BEFORE that change have no score on notable items. Those
   are reported separately as `legacyNote` rather than silently kept or dropped. */
function filterBriefing(data, query, minScore, ignoreTab = false) {
  const toks = tokens(query);
  const cats = data.categories || {};
  const notable = data.also_notable || [];
  const totalFeat = Object.values(cats).reduce((n, v) => n + (v?.length || 0), 0);
  const totalNote = notable.length;
  const tabbed = !ignoreTab && ACTIVE_TAB !== "all";
  const active = toks.length > 0 || minScore > 0 || tabbed;

  if (!active) {
    return { data, active: false, scoreActive: false, legacyNote: 0, tabbed: false,
             totalFeat, totalNote, matchFeat: totalFeat, matchNote: totalNote };
  }

  const outCats = {};
  let matchFeat = 0;
  for (const [name, items] of Object.entries(cats)) {
    if (tabbed && name !== ACTIVE_TAB) continue;   // tab narrows to one section
    const keep = (items || []).filter((it) => {
      const s = scoreOf(it);
      return (s === null || s >= minScore) &&
             (!toks.length || matchesAll(cardHaystack(it), toks));
    });
    if (keep.length) { outCats[name] = keep; matchFeat += keep.length; }
  }

  let legacyNote = 0;
  // also_notable carries no category, so a section tab cannot meaningfully
  // filter it. Hide the strip while a tab is active rather than showing items
  // that belong to other sections.
  const outNote = tabbed ? [] : notable.filter((it) => {
    if (toks.length && !matchesAll(notableHaystack(it), toks)) return false;
    if (minScore <= 0) return true;
    const s = scoreOf(it);
    if (s === null) { legacyNote += 1; return false; }   // pre-score briefing
    return s >= minScore;
  });

  return {
    data: { ...data, categories: outCats, also_notable: outNote },
    active: true, scoreActive: minScore > 0, tabbed, legacyNote,
    totalFeat, totalNote, matchFeat, matchNote: outNote.length,
  };
}

function syncSearchUI(res) {
  const countEl = el("#searchCount");
  const clearEl = el("#searchClear");
  clearEl.hidden = !tokens(QUERY).length;
  countEl.textContent = res.active
    ? `${res.matchFeat}/${res.totalFeat} features · ${res.matchNote}/${res.totalNote} notable`
      + (res.legacyNote ? ` · ${res.legacyNote} unscored (archived before scoring)` : "")
    : "";
  countEl.dataset.empty = String(res.active && !res.matchFeat && !res.matchNote);
}

function syncScoreUI() {
  $$("#scorePresets [data-min-score]").forEach((btn) => {
    const on = Number(btn.dataset.minScore) === MIN_SCORE;
    btn.classList.toggle("is-on", on);
    btn.setAttribute("aria-pressed", String(on));
  });
}

function applyFilter() {
  const dates = STORE.dates;
  if (!dates.length) return;
  const data = STORE.byDate[dates[currentIndex]];
  const res = filterBriefing(data, QUERY, MIN_SCORE);
  // Counts come from a tab-agnostic pass so every tab shows its true size even
  // while another tab is selected.
  const forCounts = filterBriefing(
    data, QUERY, MIN_SCORE, /* ignoreTab */ true).data.categories;
  renderTabs(data, forCounts);
  render(res.data, res);
  syncSearchUI(res);
  syncScoreUI();
  syncCollapseUI();
}

/* --------------------------- archive navigator --------------------------- */
function syncArchiveUI() {
  const { dates } = STORE;
  const sel = el("#archiveSelect");

  // One day of data = nothing to navigate between. dispatch.py writes
  // briefing.json AND briefing-<date>.json from the same payload, so a first run
  // produces two FILES but only one DAY. The bar appears on day two.
  el("#archiveBar").hidden = dates.length <= 1;
  el("#archiveCount").textContent =
    dates.length ? `${dates.length} day${dates.length === 1 ? "" : "s"} archived` : "";

  sel.innerHTML = "";
  dates.forEach((d, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = i === 0 ? `${d}  ·  latest` : d;
    sel.appendChild(opt);
  });
  sel.value = String(currentIndex);

  el("#prevDay").disabled = currentIndex >= dates.length - 1;   // older
  el("#nextDay").disabled = currentIndex <= 0;                  // newer
  el("#latestBtn").disabled = currentIndex === 0;
  el("#archiveFlag").hidden = currentIndex === 0;
}

function showIndex(i) {
  const { dates } = STORE;
  if (i < 0 || i >= dates.length) return;
  currentIndex = i;
  SHOW_ALL_NOTABLE = false;
  applyFilter();                 // renders the day through the active filters
  renderHealth(dates[i]);
  renderLeaderboard();
  syncDiffUI();
  syncArchiveUI();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* ------------------------------ feed health ------------------------------ */
const STATUS_HELP = {
  OK: "Contributing articles",
  STALE: "Reachable, but nothing new in the lookback window",
  FILTERED: "Fresh items existed but all were pre-filtered out",
  EMPTY: "Feed parsed but contains zero entries",
  NO_FEED: "No RSS/Atom feed discoverable at this URL",
  HTTP_404: "Dead URL (404/410) — moved or removed",
  HTTP_403: "Blocked (403) — WAF, bot protection, or UA ban",
  CAPTCHA: "CAPTCHA / JS interstitial instead of content",
  PAYWALL: "Paywalled or requires authentication",
  HTTP_429: "Rate limited by the source",
  HTTP_5XX: "Source server error",
  HTTP_OTHER: "Unexpected HTTP status",
  TIMEOUT: "No response within the timeout",
  DNS_ERROR: "Hostname did not resolve",
  SSL_ERROR: "TLS / certificate failure",
  CONN_ERROR: "Connection failed or was reset",
  PARSE_ERROR: "Response was not parseable RSS/Atom",
};
const SEVERE = new Set(["HTTP_404", "DNS_ERROR", "SSL_ERROR", "NO_FEED",
                        "PARSE_ERROR", "CAPTCHA", "PAYWALL", "HTTP_403"]);

function previousDateWithReport(date) {
  const all = Object.keys(STORE.reports).sort((a, b) => (a < b ? 1 : a > b ? -1 : 0));
  const i = all.indexOf(date);
  return i === -1 ? null : (all[i + 1] || null);
}

function computeDeltas(cur, prev) {
  if (!cur || !prev) return null;
  const p = new Map((prev.sources || []).map((s) => [s.url, s.status]));
  const c = new Map((cur.sources || []).map((s) => [s.url, s.status]));
  const wentDark = [], recovered = [], changed = [], added = [], removed = [];
  for (const [url, st] of c) {
    if (!p.has(url)) { added.push({ url, to: st }); continue; }
    const was = p.get(url);
    if (was === st) continue;
    if (was === "OK") wentDark.push({ url, from: was, to: st });
    else if (st === "OK") recovered.push({ url, from: was, to: st });
    else changed.push({ url, from: was, to: st });
  }
  for (const url of p.keys()) if (!c.has(url)) removed.push({ url });
  return { wentDark, recovered, changed, added, removed };
}

function deltaBlock(title, cls, rows, fmt) {
  if (!rows.length) return "";
  return `<div class="delta delta--${cls}">` +
    `<h4>${escapeHtml(title)} <span>[${rows.length}]</span></h4><ul>` +
    rows.map((r) => `<li>${fmt(r)}</li>`).join("") + "</ul></div>";
}

function hostOf(u) {
  try { return new URL(u).hostname.replace("www.", ""); } catch (_) { return u; }
}

function renderHealth(date) {
  const panel = el("#healthPanel");
  const report = STORE.reports[date];
  if (!report) { panel.hidden = true; return; }
  panel.hidden = false;

  const sources = report.sources || [];
  const ok = sources.filter((s) => s.status === "OK").length;
  const down = sources.length - ok;
  const severe = sources.filter((s) => SEVERE.has(s.status)).length;

  el("#healthDot").dataset.level = severe ? "bad" : down ? "warn" : "good";
  el("#healthHeadline").innerHTML =
    `Feed health · <b>${ok}/${sources.length}</b>`;
  el("#healthToggle").title =
    `${ok} of ${sources.length} feeds contributing` +
    (down ? ` · ${down} inactive · ${severe} need attention` : " · all healthy");

  const prevDate = previousDateWithReport(date);
  const deltas = computeDeltas(report, STORE.reports[prevDate]);
  const tag = el("#healthDeltaTag");
  if (deltas && (deltas.wentDark.length || deltas.recovered.length)) {
    tag.hidden = false;
    tag.textContent =
      (deltas.wentDark.length ? `▼ ${deltas.wentDark.length} went dark` : "") +
      (deltas.wentDark.length && deltas.recovered.length ? " · " : "") +
      (deltas.recovered.length ? `▲ ${deltas.recovered.length} recovered` : "");
    tag.dataset.level = deltas.wentDark.length ? "bad" : "good";
  } else {
    tag.hidden = true;
  }

  const line = (r) =>
    `<code>${escapeHtml(hostOf(r.url))}</code> <span class="arrow">` +
    `${escapeHtml(r.from || "—")} → ${escapeHtml(r.to || "—")}</span>`;

  let deltaHtml = "";
  if (deltas) {
    deltaHtml =
      deltaBlock("Went dark since " + prevDate, "bad", deltas.wentDark, line) +
      deltaBlock("Recovered since " + prevDate, "good", deltas.recovered, line) +
      deltaBlock("Changed failure mode", "warn", deltas.changed, line) +
      deltaBlock("New sources", "info", deltas.added,
                 (r) => `<code>${escapeHtml(hostOf(r.url))}</code> ` +
                        `<span class="arrow">${escapeHtml(r.to)}</span>`) +
      deltaBlock("Removed from feeds.txt", "info", deltas.removed,
                 (r) => `<code>${escapeHtml(hostOf(r.url))}</code>`);
    if (!deltaHtml) {
      deltaHtml = `<p class="delta__none">No status changes since ${escapeHtml(prevDate)}.</p>`;
    }
  } else {
    deltaHtml = `<p class="delta__none">No earlier report to compare against yet — ` +
                `day-over-day changes appear from the second run onward.</p>`;
  }
  el("#healthDeltas").innerHTML = deltaHtml;

  const groups = {};
  sources.filter((s) => s.status !== "OK")
         .forEach((s) => { (groups[s.status] ||= []).push(s); });
  const order = Object.keys(groups).sort(
    (a, b) => (SEVERE.has(b) - SEVERE.has(a)) || groups[b].length - groups[a].length);

  el("#healthGroups").innerHTML = order.length
    ? order.map((st) => `
        <div class="hgroup" data-severe="${SEVERE.has(st)}">
          <div class="hgroup__head">
            <span class="hgroup__code">${escapeHtml(st)}</span>
            <span class="hgroup__n">[${groups[st].length}]</span>
            <span class="hgroup__help">${escapeHtml(STATUS_HELP[st] || "")}</span>
          </div>
          <ul class="hgroup__list">
            ${groups[st].map((s) => `<li>
                <a href="${escapeHtml(s.url)}" target="_blank" rel="noopener noreferrer">
                  ${escapeHtml(s.source || hostOf(s.url))}</a>
                ${s.detail ? `<span class="hgroup__detail">${escapeHtml(s.detail)}</span>` : ""}
              </li>`).join("")}
          </ul>
        </div>`).join("")
    : `<p class="delta__none">Every source is contributing. Nothing to fix.</p>`;

  const gen = report.generated_at ? fmtPacific(report.generated_at) : "";
  el("#healthFoot").textContent =
    `${sources.length} sources checked` +
    (report.lookback_hours ? ` · ${report.lookback_hours}h lookback` : "") +
    (gen ? ` · checked ${gen}` : "") +
    ` · run "python feedcheck.py --failures" locally for a live re-test`;
}

/* --------------------------- source leaderboard -------------------------- */
/* Ranks the sources that actually earn their slot in feeds.txt. Feature-tier
   items carry scores; also_notable items do not, so they count toward "notable"
   and "total" but never toward avg/best score. */
function tallySources(dates) {
  const t = new Map();
  const get = (src) => {
    if (!t.has(src)) t.set(src, {
      source: src, features: 0, notable: 0, scored: 0,
      scoreSum: 0, best: 0, days: new Set(),
    });
    return t.get(src);
  };

  dates.forEach((date) => {
    const day = STORE.byDate[date];
    if (!day) return;
    Object.values(day.categories || {}).forEach((items) => {
      (items || []).forEach((it) => {
        const row = get(it.source || hostOf(it.url || ""));
        row.features += 1;
        row.days.add(date);
        const s = scoreOf(it);
        if (s !== null) { row.scored += 1; row.scoreSum += s; row.best = Math.max(row.best, s); }
      });
    });
    (day.also_notable || []).forEach((it) => {
      const row = get(it.source || hostOf(it.url || ""));
      row.notable += 1;
      row.days.add(date);
      const s = scoreOf(it);                 // present since scored also_notable
      if (s !== null) { row.scored += 1; row.scoreSum += s; row.best = Math.max(row.best, s); }
    });
  });

  return Array.from(t.values()).map((r) => ({
    ...r,
    total: r.features + r.notable,
    avg: r.scored ? r.scoreSum / r.scored : 0,
    dayCount: r.days.size,
  }));
}

const LB_SORTS = {
  features: (a, b) => b.features - a.features || b.avg - a.avg,
  avg:      (a, b) => b.avg - a.avg || b.features - a.features,
  best:     (a, b) => b.best - a.best || b.features - a.features,
  total:    (a, b) => b.total - a.total || b.features - a.features,
};

function renderLeaderboard() {
  const panel = el("#boardPanel");
  if (!STORE.dates.length) { panel.hidden = true; return; }
  panel.hidden = false;

  const scope = LB_ALL_DAYS ? STORE.dates : [STORE.dates[currentIndex]];
  const rows = tallySources(scope).sort(LB_SORTS[LB_SORT] || LB_SORTS.features);

  el("#boardHeadline").innerHTML =
    `Sources · <b>${rows.length}</b>` +
    (LB_ALL_DAYS ? ` / ${scope.length}d` : "");
  el("#boardToggle").title =
    `${rows.length} ranked source${rows.length === 1 ? "" : "s"}` +
    (LB_ALL_DAYS ? ` across ${scope.length} archived days` : ` for ${scope[0]}`);

  const maxFeat = Math.max(1, ...rows.map((r) => r.features));
  el("#lbTable").innerHTML = rows.length ? `
    <div class="lb__head">
      <span>#</span><span>source</span><span>feat</span>
      <span>notable</span><span>avg</span><span>best</span>
    </div>` + rows.map((r, i) => `
    <div class="lb__row" data-top="${i < 3}">
      <span class="lb__rank">${i + 1}</span>
      <span class="lb__src">
        <span class="lb__bar" style="width:${(r.features / maxFeat) * 100}%"></span>
        <span class="lb__name">${escapeHtml(r.source)}</span>
      </span>
      <span class="lb__n">${r.features}</span>
      <span class="lb__n lb__n--dim">${r.notable}</span>
      <span class="lb__n">${r.scored ? r.avg.toFixed(1) : "—"}</span>
      <span class="lb__n ${r.best >= 9 ? "is-hot" : ""}">${r.best || "—"}</span>
    </div>`).join("")
    : `<p class="delta__none">Nothing to rank in this briefing.</p>`;

  const zero = rows.filter((r) => !r.features).length;
  el("#lbFoot").textContent =
    (LB_ALL_DAYS ? `All ${scope.length} archived day(s)` : `Day ${scope[0]}`) +
    ` · ${rows.length} sources appeared` +
    (zero ? ` · ${zero} produced notable-only (no feature-tier hits)` : "") +
    ` · sources absent entirely are in the feed health panel above`;
}

/* -------------------------------- diff view ------------------------------ */
/* Compares two briefings by item URL (the stable identity — titles get rewritten
   by the editor, URLs are re-attached from the source record and never change). */
function itemsOf(day) {
  const out = [];
  Object.entries(day.categories || {}).forEach(([cat, items]) => {
    (items || []).forEach((it) => out.push({ ...it, category: cat, tier: "feature" }));
  });
  (day.also_notable || []).forEach((it) =>
    out.push({ ...it, category: "Also notable", tier: "notable" }));
  return out;
}

function keyOf(it) {
  return it.url || ("t:" + String(it.title || "").toLowerCase().replace(/\W+/g, ""));
}

function computeDiff(curDay, prevDay) {
  const cur = new Map(itemsOf(curDay).map((i) => [keyOf(i), i]));
  const prev = new Map(itemsOf(prevDay).map((i) => [keyOf(i), i]));
  const added = [], dropped = [], carried = [], promoted = [];

  for (const [k, it] of cur) {
    if (!prev.has(k)) { added.push(it); continue; }
    const was = prev.get(k);
    if (was.tier !== it.tier) promoted.push({ ...it, from: was.tier, to: it.tier });
    else carried.push(it);
  }
  for (const [k, it] of prev) if (!cur.has(k)) dropped.push(it);

  const srcCur = new Set(itemsOf(curDay).map((i) => i.source));
  const srcPrev = new Set(itemsOf(prevDay).map((i) => i.source));
  const newSources = [...srcCur].filter((s) => !srcPrev.has(s));
  const goneSources = [...srcPrev].filter((s) => !srcCur.has(s));

  return { added, dropped, carried, promoted, newSources, goneSources };
}

function diffList(title, cls, items, opts = {}) {
  if (!items.length) return "";
  const rows = items.slice(0, opts.limit || 40).map((it) => `
    <li>
      <a href="${escapeHtml(it.url || "#")}" target="_blank" rel="noopener noreferrer">
        ${escapeHtml(it.title || "Untitled")}</a>
      <span class="diff__meta">${escapeHtml(it.source || "")}${
        typeof it.score === "number" ? ` · ${it.score}/10` : ""}${
        it.from ? ` · ${escapeHtml(it.from)} → ${escapeHtml(it.to)}` : ""}</span>
    </li>`).join("");
  const more = items.length > (opts.limit || 40)
    ? `<li class="diff__more">+ ${items.length - (opts.limit || 40)} more</li>` : "";
  return `<div class="diff__col diff__col--${cls}">
    <h4>${escapeHtml(title)} <span>[${items.length}]</span></h4>
    <ul>${rows}${more}</ul></div>`;
}

function renderDiff() {
  const panel = el("#diffPanel");
  const dates = STORE.dates;
  if (dates.length < 2) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;

  const baseDate = dates[currentIndex];
  const against = DIFF_AGAINST && STORE.byDate[DIFF_AGAINST] && DIFF_AGAINST !== baseDate
    ? DIFF_AGAINST
    : (dates[currentIndex + 1] || dates.find((d) => d !== baseDate));

  el("#diffBase").textContent = baseDate;
  const d = computeDiff(STORE.byDate[baseDate], STORE.byDate[against]);

  el("#diffDot").dataset.level = d.added.length ? "good" : "warn";
  el("#diffHeadline").innerHTML =
    `Changes · <b>+${d.added.length}</b> / <b class="down">−${d.dropped.length}</b>`;
  el("#diffToggle").title =
    `${d.added.length} new · ${d.dropped.length} gone · ${d.carried.length} carried over`;

  el("#diffGrid").innerHTML =
    diffList(`New in ${baseDate}`, "add", d.added) +
    diffList(`Gone since ${against}`, "drop", d.dropped) +
    diffList("Tier changed", "move", d.promoted) +
    (d.added.length || d.dropped.length || d.promoted.length ? "" :
      `<p class="delta__none">These two briefings contain the same items.</p>`);

  const srcBits = [];
  if (d.newSources.length) srcBits.push(`new sources: ${d.newSources.slice(0, 8).join(", ")}`);
  if (d.goneSources.length) srcBits.push(`absent today: ${d.goneSources.slice(0, 8).join(", ")}`);
  el("#diffFoot").textContent =
    `${baseDate} vs ${against} · matched by article URL` +
    (srcBits.length ? ` · ${srcBits.join(" · ")}` : "");
}

function syncDiffUI() {
  const sel = el("#diffSelect");
  const dates = STORE.dates;
  const baseDate = dates[currentIndex];
  const options = dates.filter((d) => d !== baseDate);
  sel.innerHTML = "";
  options.forEach((d) => {
    const o = document.createElement("option");
    o.value = d; o.textContent = d;
    sel.appendChild(o);
  });
  if (!options.includes(DIFF_AGAINST)) DIFF_AGAINST = options[0] || null;
  if (DIFF_AGAINST) sel.value = DIFF_AGAINST;
  renderDiff();
}

/* ------------------------------- rendering ------------------------------- */
function buildMeter(score) {
  // 0–10 score -> 5-segment meter; top segment turns red at 9+.
  const frag = document.createDocumentFragment();
  const lit = Math.round((Math.max(0, Math.min(10, score)) / 10) * 5);
  for (let i = 1; i <= 5; i++) {
    const seg = document.createElement("i");
    if (i <= lit) seg.classList.add(score >= 9 && i === 5 ? "hot" : "on");
    frag.appendChild(seg);
  }
  return frag;
}

function paintCardDetails(card, open) {
  if (!card) return;
  card.dataset.detailsOpen = String(open);
  const toggle = $(".card__details-toggle", card);
  const details = $(".card__details", card);
  const label = $(".card__details-label", card);
  if (toggle) toggle.setAttribute("aria-expanded", String(open));
  if (details) details.setAttribute("aria-hidden", String(!open));
  if (label) label.textContent = open ? "Hide details" : "Details";
}

function renderCard(item) {
  const tpl = $("#cardTpl");
  if (!tpl || !tpl.content) {
    throw new Error("#cardTpl <template> is missing from index.html — " +
                    "index.html and script.js are out of sync.");
  }
  const node = tpl.content.cloneNode(true);
  const card = $(".card", node);
  const score = typeof item.score === "number" ? item.score : 8;
  const cardKey = keyOf(item);
  const detailsId = `card-details-${++CARD_SEQ}`;

  card.dataset.cardKey = cardKey;

  $(".meter", node).appendChild(buildMeter(score));
  $(".meter", node).title = `relevance ${score}/10`;
  $(".card__src", node).textContent = item.source || "";
  const scoreEl = $(".card__score", node);
  scoreEl.textContent = `${score}/10`;
  if (score >= 9) scoreEl.classList.add("hot");

  const titleLink = $(".card__title a", node);
  titleLink.textContent = item.title || "Untitled";
  titleLink.href = item.url || "#";

  const why = $(".card__why", node);
  const whyText = $(".card__why-text", node);
  whyText.textContent = item.reasoning || "";
  why.hidden = !item.reasoning;

  const ul = $(".card__bullets", node);
  const bullets = item.bullets || [];
  bullets.forEach((b) => {
    const li = document.createElement("li");
    li.textContent = b;
    ul.appendChild(li);
  });

  const toggle = $(".card__details-toggle", node);
  const details = $(".card__details", node);
  toggle.dataset.cardDetails = cardKey;
  toggle.setAttribute("aria-controls", detailsId);
  details.id = detailsId;
  if (!bullets.length) {
    toggle.hidden = true;
    details.hidden = true;
  } else {
    paintCardDetails(card, EXPANDED_CARDS.has(cardKey));
  }

  $(".card__link", node).href = item.url || "#";
  return node;
}

/* Category id helper — stable, collision-free DOM ids for aria-controls. */
function catId(name) {
  return "cat-" + String(name).toLowerCase()
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

/* Find a rendered category section by its authoritative name.
   Reads data-cat, never parsed text content — text is escaped/uppercased by CSS
   and would not round-trip reliably. */
function sectionFor(name) {
  return $$(".category", board).find((s) => s.dataset.cat === name) || null;
}

/* THE painter. Arrow and body are updated by this one function and nowhere else,
   so they physically cannot disagree — the old failure mode was the chevron
   rotating from a CSS rule while the body stayed visible because a DIFFERENT
   CSS rule (in a stale style.css) never applied.

   Hiding is done with an INLINE style, which outranks every author stylesheet
   rule. Collapse therefore keeps working even if style.css is cached, stale, or
   fails to load entirely. The hidden attribute and the CSS rules remain as
   redundant belt-and-braces, but nothing depends on them. */
function paintCategory(section, collapsed) {
  if (!section) return;
  section.dataset.collapsed = String(collapsed);

  const body = $(".category__body", section);
  if (body) {
    body.hidden = collapsed;                        // semantics + a11y
    body.style.display = collapsed ? "none" : "";   // authoritative
  }

  // Arrow follows the SAME state. The inline transform outranks the stylesheet;
  // style.css carries an identical rule keyed to data-collapsed as a fallback,
  // so the two can never point in opposite directions.
  const chev = $(".category__chev", section);
  if (chev) chev.style.transform = collapsed ? "rotate(0deg)" : "rotate(90deg)";

  const btn = $(".category__toggle", section);
  if (btn) btn.setAttribute("aria-expanded", String(!collapsed));

  const state = $(".category__state", section);
  if (state) state.textContent = collapsed ? "show" : "hide";
}

/* Single source of truth for open/closed. Called by delegation, by the
   collapse-all / expand-all buttons, and by the keyboard shortcuts. */
function setCategoryCollapsed(name, collapsed, { persist = true } = {}) {
  if (collapsed) COLLAPSED.add(name); else COLLAPSED.delete(name);
  paintCategory(sectionFor(name), collapsed);
  if (persist) savePrefs({ collapsed: [...COLLAPSED] });
  requestReadProgress();
}

function toggleCategory(name) {
  setCategoryCollapsed(name, !COLLAPSED.has(name));
  syncCollapseUI();
}

/* Every category name in the CURRENT DAY'S DATA — not just the ones that
   survived the active search/score filter. Reading the DOM here was a bug: with
   a filter on, filtered-out sections are not rendered, so "collapse all" skipped
   them and they reappeared expanded the moment the filter was cleared. */
function allCategoryNames() {
  const dates = STORE.dates;
  if (!dates.length) return [];
  const cats = (STORE.byDate[dates[currentIndex]] || {}).categories || {};
  return Object.keys(cats).filter((k) => (cats[k] || []).length);
}

/* Keeps the "n/m collapsed" readout truthful after any state change, whether it
   came from a global button, an individual header, or a keyboard shortcut. */
function syncCollapseUI() {
  const node = el("#collapseState");          // renamed: `el` is the helper
  const names = allCategoryNames();
  const n = names.filter((x) => COLLAPSED.has(x)).length;
  node.textContent = names.length ? `${n}/${names.length} collapsed` : "";
  node.dataset.on = String(n > 0);
}

function setAllCategories(collapsed) {
  // Scoped to this day's categories so the global buttons are idempotent and
  // cannot be defeated by an active filter. setCategoryCollapsed tolerates a
  // name with no rendered section — it still updates the COLLAPSED set.
  allCategoryNames().forEach((name) =>
    setCategoryCollapsed(name, collapsed, { persist: false }));
  savePrefs({ collapsed: [...COLLAPSED] });
  syncCollapseUI();
}

/* STRUCTURE NOTE — why this is not a <button> wrapping an <h2>:
   <button> only permits PHRASING content, so an <h2> inside it is invalid HTML
   and is a known source of flaky click/flex behaviour in WebKit. The heading
   now wraps the button (valid), the button contains only spans, and the flex
   layout lives on an inner span rather than on the button itself — Safari has
   long-standing bugs with `display:flex` applied directly to <button>. */
function renderCategory(name, items) {
  const section = document.createElement("section");
  section.className = "category";
  section.dataset.cat = name;                       // authoritative key
  const collapsed = COLLAPSED.has(name);
  section.dataset.collapsed = String(collapsed);

  const bodyId = catId(name) + "-body";

  const head = document.createElement("h2");        // sticky lives here
  head.className = "category__head";

  const btn = document.createElement("button");     // the hit target
  btn.type = "button";
  btn.className = "category__toggle";
  btn.dataset.catToggle = name;                     // delegation hook
  btn.setAttribute("aria-expanded", String(!collapsed));
  btn.setAttribute("aria-controls", bodyId);
  btn.innerHTML =
    `<span class="category__inner">` +
      `<span class="category__chev" aria-hidden="true">\u25B6</span>` +
      `<span class="slash" aria-hidden="true">//</span>` +
      `<span class="category__name">${escapeHtml(name)}</span>` +
      `<span class="rule" aria-hidden="true"></span>` +
      `<span class="count">${items.length}</span>` +
      `<span class="category__state">${collapsed ? "show" : "hide"}</span>` +
    `</span>`;
  head.appendChild(btn);

  const body = document.createElement("div");
  body.className = "category__body";
  body.id = bodyId;
  items.forEach((it) => body.appendChild(renderCard(it)));

  section.appendChild(head);
  section.appendChild(body);
  paintCategory(section, collapsed);   // same path as every later toggle
  return section;
}

function renderNotable(items) {
  const limit = 24;
  const visibleItems = SHOW_ALL_NOTABLE ? items : items.slice(0, limit);
  const wrap = document.createElement("section");
  wrap.className = "notable";
  wrap.innerHTML = `<h3 class="notable__head"><span>// Also notable</span>` +
    `<span class="notable__head-count">${items.length} items</span></h3>`;
  const list = document.createElement("div");
  list.className = "notable__list";
  list.id = "notableList";
  visibleItems.forEach((it) => {
    const a = document.createElement("a");
    a.className = "notable__item";
    a.href = it.url || "#";
    a.target = "_blank"; a.rel = "noopener noreferrer";
    const s = scoreOf(it);
    if (it.tier === "overflow") a.dataset.tier = "overflow";
    a.innerHTML =
      `<span class="notable__src">${escapeHtml(it.source || "")}</span>` +
      `<span class="notable__ttl">${escapeHtml(it.title || "")}</span>` +
      (s === null ? "" :
        `<span class="notable__score"${s >= 7 ? ' data-hi="true"' : ""}>${s}</span>`);
    list.appendChild(a);
  });
  wrap.appendChild(list);
  if (items.length > limit) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "notable__more";
    button.dataset.notableToggle = "true";
    button.setAttribute("aria-controls", list.id);
    button.setAttribute("aria-expanded", String(SHOW_ALL_NOTABLE));
    button.textContent = SHOW_ALL_NOTABLE
      ? "Show fewer notable items"
      : `Show ${items.length - limit} more notable items`;
    wrap.appendChild(button);
  }
  return wrap;
}

function render(data, filterRes) {
  board.innerHTML = "";
  CARD_SEQ = 0;
  const cats = data.categories || {};
  const catNames = orderCategories(
    Object.keys(cats).filter((k) => cats[k]?.length));
  let featTotal = 0;

  catNames.forEach((name) => {
    featTotal += cats[name].length;
    board.appendChild(renderCategory(name, cats[name]));
  });

  const notable = data.also_notable || [];
  if (notable.length) board.appendChild(renderNotable(notable));

  if (!catNames.length && !notable.length) {
    const res = filterRes || {};
    const where = res.tabbed ? ` in <b>${escapeHtml(ACTIVE_TAB)}</b>` : "";
    let why;
    if (tokens(QUERY).length) {
      why = `No items match <b>${escapeHtml(QUERY)}</b>` +
            (res.scoreActive ? ` at score ≥ ${MIN_SCORE}` : "") + `${where}.`;
    } else if (res.scoreActive) {
      why = `No items scored ≥ ${MIN_SCORE}${where}.`;
    } else if (res.tabbed) {
      why = `Nothing in <b>${escapeHtml(ACTIVE_TAB)}</b> today.`;
    } else {
      why = "No items in this briefing.";
    }
    board.innerHTML = res.active
      ? `<div class="state">${why}
           <button class="state__reset" id="stateReset">clear filters</button></div>`
      : `<div class="state">${why}</div>`;
    const reset = el("#stateReset");
    if (reset && reset.addEventListener) reset.addEventListener("click", () => {
      const si = el("#searchInput"); if (si) si.value = "";
      QUERY = ""; MIN_SCORE = 0; ACTIVE_TAB = "all";
      savePrefs({ minScore: 0, activeTab: "all" });
      applyFilter();
    });
  }

  // header meta
  const tag = el("#statusTag");
  tag.textContent = "LIVE"; tag.dataset.state = "live";
  el("#dateStamp").textContent = data.date || "";
  el("#featCount").textContent = featTotal;
  el("#notableCount").textContent = notable.length;
  el("#catCount").textContent = catNames.length;

  /* Which model produced THIS day. Persisted only for the latest day: the
     stored value describes the current output, not whichever archived day the
     reader happens to have open. */
  const cached = MODEL_STORED;
  let modelInfo = modelInfoOf(data);
  if (!modelInfo && cached && cached.date && cached.date === (data.date || "")) {
    modelInfo = cached;             // same day, recorded on an earlier visit
  }
  renderModelTag(modelInfo);
  if (modelInfo && currentIndex === 0) saveModelLabel(modelInfo);

  const gen = el("#genStamp");
  if (data.generated_at) {
    gen.textContent = "compiled " + fmtPacific(data.generated_at);
    gen.title = utcTitle(data.generated_at);
  } else {
    gen.textContent = ""; gen.title = "";
  }
  el("#statBar").hidden = false;
  el("#searchBar").hidden = false;
  el("#controlBar").hidden = false;

  // The "n/m collapsed" readout is derived state; recompute it here so a day
  // flip or a filter change cannot leave a stale count on screen.
  syncCollapseUI();
  requestReadProgress();
}

function renderError(msg) {
  const tag = el("#statusTag");
  tag.textContent = "OFFLINE"; tag.dataset.state = "error";
  board.innerHTML =
    `<div class="state state--error"><p>${escapeHtml(msg)}</p></div>`;

  // The label must never be left spinning on "working…". With nothing loaded,
  // the persisted value is the only truthful thing we have — shown as stale.
  const last = MODEL_STORED;
  if (last) { renderModelTag(last, { stale: true }); return; }
  const node = el("#modelTag");
  node.textContent = "model unknown";
  node.dataset.state = "unknown";
  node.title = "No briefing loaded, and no model recorded from an earlier visit.";
  renderTokenTag(null);
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ----------------------------- read progress ----------------------------- */
let READ_PROGRESS_FRAME = 0;

function syncReadProgress() {
  READ_PROGRESS_FRAME = 0;
  const bar = el("#readProgressBar");
  if (!bar) return;
  const maxScroll = Math.max(1,
    document.documentElement.scrollHeight - window.innerHeight);
  const progress = Math.min(1, Math.max(0, window.scrollY / maxScroll));
  bar.style.transform = `scaleX(${progress})`;
}

function requestReadProgress() {
  if (READ_PROGRESS_FRAME) return;
  READ_PROGRESS_FRAME = requestAnimationFrame(syncReadProgress);
}

/* --------------------------- asset version check -------------------------- */
/* Three files must agree: index.html (data-build), script.js (BUILD) and
   style.css (--build). GitHub Pages sits behind a CDN that will serve a
   previous style.css for a while after a push, and a stale stylesheet is the
   only thing that has ever produced the "arrow moves, nothing collapses"
   symptom. Rather than let that present as a broken feature, say it out loud. */
function checkAssetVersions() {
  const htmlBuild = (document.body.dataset.build || "").trim();
  const cssBuild = getComputedStyle(document.documentElement)
    .getPropertyValue("--build").trim().replace(/^["']|["']$/g, "");

  const stale = [];
  if (htmlBuild && htmlBuild !== BUILD) stale.push(`index.html=${htmlBuild}`);
  if (!cssBuild) stale.push("style.css=(not loaded)");
  else if (cssBuild !== BUILD) stale.push(`style.css=${cssBuild}`);
  if (!stale.length) return;

  console.warn(`[DISPATCH] asset build mismatch — script.js=${BUILD}, ` +
               stale.join(", ") + ". Hard-reload (Cmd/Ctrl+Shift+R).");

  const bar = document.createElement("button");
  bar.type = "button";
  bar.className = "stalebar";
  bar.innerHTML =
    `<b>Stale cached assets</b> — script.js is ${escapeHtml(BUILD)}, but ` +
    escapeHtml(stale.join(" / ")) +
    `. Tap here to hard-reload; collapse may misbehave until you do.`;
  bar.addEventListener("click", () => {
    location.replace(location.pathname + "?cb=" + Date.now() + location.hash);
  });
  document.body.insertBefore(bar, document.body.firstChild);
}

/* ================================================================ settings ==
   Reads and writes news_aggregator.app_settings — the row the pipeline loads at
   the start of every run (pipeline/settings.py). This is now the only place
   model choice lives; .github/workflows/daily.yml no longer carries it.

   NOTHING HERE RUNS A MODEL. The pipeline is a scheduled job, so a change saved
   at noon takes effect at the next 06:17 run. The modal says so out loud,
   because "I changed the model and the briefing looks identical" is otherwise
   the obvious and wrong conclusion to draw. */
/* Bearer-token model listing in OpenAI's shape — {data:[{id}]}. Shared by the
   six compatible providers AND by OpenAI itself, since it is OpenAI's own
   format. Anthropic and Gemini differ enough to need their own. */
function openAiStyleDiscover(base) {
  return async function discover(key) {
    const r = await fetch(`${base}/models`, {
      headers: { Authorization: `Bearer ${key}` },
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      throw new Error(j?.error?.message || j?.message || `HTTP ${r.status}`);
    }
    return (j.data || j.models || []).map((m) => m.id || m.name).filter(Boolean);
  };
}

/* THE registry. Key rows and the provider dropdown are both generated from it,
   so a provider cannot exist in the markup and not in the code (or vice versa).
   `column` mirrors the app_settings column names created by migrations 004/005,
   and matches pipeline/settings.py, which derives them the same way. */
const PROVIDERS = {
  openai: {
    label: "OpenAI",
    keysUrl: "platform.openai.com/api-keys",
    placeholder: "sk-…",
    discover: openAiStyleDiscover("https://api.openai.com/v1"),
  },
  anthropic: {
    label: "Anthropic (Claude)",
    keysUrl: "console.anthropic.com/settings/keys",
    placeholder: "sk-ant-…",
    async discover(key) {
      const r = await fetch("https://api.anthropic.com/v1/models", {
        headers: {
          "x-api-key": key,
          "anthropic-version": "2023-06-01",
          // Without this the browser is refused at CORS before the request
          // is ever sent — it is not optional for a page-side call.
          "anthropic-dangerous-direct-browser-access": "true",
        },
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j?.error?.message || `HTTP ${r.status}`);
      return (j.data || []).map((m) => m.id);
    },
  },
  gemini: {
    label: "Gemini",
    keysUrl: "aistudio.google.com/apikey",
    placeholder: "AIza…",
    async discover(key) {
      const r = await fetch(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200&key=" +
        encodeURIComponent(key));
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j?.error?.message || `HTTP ${r.status}`);
      // Gemini returns "models/gemini-…"; the bare id is what the API wants back.
      return (j.models || []).map((m) => (m.name || "").replace(/^models\//, ""));
    },
  },

  /* OpenAI-wire-compatible. Base URLs are the vendors' documented
     COMPATIBILITY endpoints, which are not always their native ones — Cohere's
     native API is /v2/chat and a different shape entirely. These mirror
     llm._COMPAT exactly; changing one without the other breaks a provider. */
  groq: {
    label: "Groq",
    keysUrl: "console.groq.com/keys",
    placeholder: "gsk_…",
    discover: openAiStyleDiscover("https://api.groq.com/openai/v1"),
  },
  cerebras: {
    label: "Cerebras",
    keysUrl: "cloud.cerebras.ai",
    placeholder: "csk-…",
    discover: openAiStyleDiscover("https://api.cerebras.ai/v1"),
  },
  openrouter: {
    label: "OpenRouter",
    keysUrl: "openrouter.ai/keys",
    placeholder: "sk-or-…",
    discover: openAiStyleDiscover("https://openrouter.ai/api/v1"),
  },
  mistral: {
    label: "Mistral AI",
    keysUrl: "console.mistral.ai/api-keys",
    placeholder: "…",
    discover: openAiStyleDiscover("https://api.mistral.ai/v1"),
  },
  cohere: {
    label: "Cohere",
    keysUrl: "dashboard.cohere.com/api-keys",
    placeholder: "…",
    discover: openAiStyleDiscover("https://api.cohere.ai/compatibility/v1"),
  },
  huggingface: {
    label: "Hugging Face",
    keysUrl: "huggingface.co/settings/tokens",
    placeholder: "hf_…",
    discover: openAiStyleDiscover("https://router.huggingface.co/v1"),
  },
};

// Derived, never hand-written: one source of truth for ids across markup,
// storage columns and the pipeline.
for (const [id, p] of Object.entries(PROVIDERS)) {
  p.id = id;
  p.column = `${id}_api_key`;
  p.keyInput = `#key-${id}`;
  p.dot = `#dot-${id}`;
}

const CUSTOM_MODEL = "__custom__";

/* Build the key rows and the provider dropdown from the registry above. */
function renderProviderControls() {
  const host = el("#providerKeys");
  const select = el("#setProvider");
  if (host && host.innerHTML !== undefined) host.innerHTML = "";
  if (select && select.innerHTML !== undefined) select.innerHTML = "";

  for (const p of Object.values(PROVIDERS)) {
    if (select && select.appendChild) {
      select.appendChild(new Option(p.label, p.id));
    }
    if (!host || !host.appendChild) continue;
    const row = document.createElement("label");
    row.className = "set__field";
    row.dataset.providerKey = p.id;
    // Named for its provider ("Gemini API key"), so the field on screen can
    // never be mistaken for a different provider's once the others are hidden.
    // The dot says only stored / not stored — never a masked preview of the
    // value, which invites reading a real key off a shared screen.
    row.innerHTML =
      `<span class="set__label">${escapeHtml(p.label)} API key ` +
        `<i class="set__dot" id="dot-${p.id}"></i></span>` +
      `<span class="set__reveal">` +
        `<input type="password" id="key-${p.id}" autocomplete="off" ` +
          `spellcheck="false" placeholder="${escapeHtml(p.placeholder)}" />` +
        `<button type="button" class="set__eye" data-reveal="key-${p.id}" ` +
          `title="Show / hide">&#128065;</button>` +
      `</span>` +
      `<span class="set__hint">Key from ` +
        `<a href="https://${escapeHtml(p.keysUrl)}" target="_blank" ` +
        `rel="noopener noreferrer">${escapeHtml(p.keysUrl)}</a>. ` +
        `Stored in Supabase and read by the pipeline at run time.</span>`;
    host.appendChild(row);
  }
}

/* DENY-list, not an allow-list. An allow-list keyed to today's naming
   (/^gpt-|^claude-/) would silently hide any model released under an unfamiliar
   name, which defeats the point of asking the API what exists. Tokens match as
   whole segments so "ada" cannot hide "adaptive". */
const NON_CHAT = new RegExp(
  "(^|[-_./])(embed|embedding|embeddings|gecko|tts|stt|whisper|audio|speech|" +
  "voice|transcribe|translate|image|images|vision|dall|dalle|imagen|veo|" +
  "moderation|moderations|guard|safety|realtime|search|rerank|similarity|" +
  "edit|edits|instruct|codex|ada|babbage|curie|davinci|aqa|gemma|learnlm)" +
  "([-_./]|$)", "i");

let SETTINGS = null;             // the row as last read/saved
let DISCOVERED = {};             // provider -> [model id], this session only

const settingsUrl = () => `${CONFIG.SUPABASE_URL}/rest/v1/app_settings`;

async function loadSettings() {
  const r = await fetch(`${settingsUrl()}?id=eq.1&select=*`, {
    headers: { ...authHeaders(), Accept: "application/json" },
    cache: "no-store",
  });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).message || ""; } catch (_) { /* not JSON */ }
    if (r.status === 404) {
      throw new Error("app_settings does not exist — run migration " +
                      "004_app_settings_and_auth.sql." + (detail ? ` ${detail}` : ""));
    }
    throw new Error(`Could not read settings (${r.status})` +
                    (detail ? ` — ${detail}` : "") + ".");
  }
  const rows = await r.json();
  SETTINGS = rows[0] || {};
  return SETTINGS;
}

function chainToArray(text) {
  return String(text || "").split(",").map((s) => s.trim()).filter(Boolean);
}

/* The chosen model for one stage: the dropdown, unless it is on "Custom…", in
   which case the free-text id beside it wins. */
function modelValue(selectSel, customSel) {
  const select = el(selectSel);
  if (select.value !== CUSTOM_MODEL) return select.value || null;
  return el(customSel).value.trim() || null;
}

async function saveSettings() {
  const row = {
    id: 1,
    gatekeeper_model: modelValue("#setGatekeeper", "#setGatekeeperCustom"),
    editor_model: modelValue("#setEditor", "#setEditorCustom"),
    gatekeeper_fallback_models: chainToArray(el("#setGatekeeperChain").value),
    editor_fallback_models: chainToArray(el("#setEditorChain").value),
    openai_reasoning_effort: el("#setEffort").value || "low",
    updated_at: new Date().toISOString(),
    updated_by: SESSION?.user?.id || null,
  };
  // Keys straight off the registry, so a provider added there is saved without
  // anyone remembering to extend this list.
  for (const p of Object.values(PROVIDERS)) {
    const input = $(p.keyInput);
    row[p.column] = (input?.value || "").trim() || null;
  }
  const body = [row];
  const r = await fetch(settingsUrl(), {
    method: "POST",
    headers: {
      ...authHeaders(),
      "Content-Type": "application/json",
      Prefer: "resolution=merge-duplicates,return=representation",
    },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).message || ""; } catch (_) { /* not JSON */ }
    throw new Error(`Save failed (${r.status})${detail ? ` — ${detail}` : ""}.`);
  }
  const rows = await r.json().catch(() => []);
  if (rows[0]) SETTINGS = rows[0];
}

function keyFor(provider) {
  const input = $(PROVIDERS[provider].keyInput);
  return (input?.value || "").trim();
}

function syncKeyDots() {
  for (const [id, p] of Object.entries(PROVIDERS)) {
    const dot = $(p.dot);
    if (dot) dot.dataset.on = keyFor(id) ? "yes" : "no";
  }
}

/* Show only the controls that mean something for the selected provider: its key
   row, and any tuning that exists solely for it (a reasoning-effort dropdown
   under Gemini is not a harmless extra field — it reads as a setting that
   applies, and it does not).

   Non-active key inputs are HIDDEN, never removed. They keep their values, so
   typing a key under one provider, switching away, and switching back does not
   lose it — and saveSettings() still reads all nine, so every stored key is
   persisted on Save regardless of which one happens to be on screen. That is
   what makes the failover chain safe to span providers even though only one
   key is visible at a time. */
function syncProviderUI(provider = el("#setProvider").value) {
  $$("[data-provider]").forEach((node) => {
    node.hidden = node.dataset.provider !== provider;
  });
  $$("[data-provider-key]").forEach((node) => {
    node.hidden = node.dataset.providerKey !== provider;
  });
}

/* Populate one stage's <select>. The stored value is always present as an
   option even when discovery never ran or returned something else — otherwise
   opening Settings and pressing Save would silently rewrite the model to
   whatever happened to be first in the list. */
function fillModelSelect(sel, current, provider, customSel) {
  const node = $(sel);
  if (!node) return;
  const ids = (DISCOVERED[provider] || []).filter((id) => id && !NON_CHAT.test(id));
  const values = ids.map((id) => `${provider}:${id}`);
  if (current && !values.includes(current)) values.unshift(current);
  node.innerHTML = "";
  if (!values.length) {
    node.appendChild(new Option("— enter a key and press Refresh —", ""));
  }
  for (const v of values) node.appendChild(new Option(v, v));
  // Always reachable: several providers block browser discovery at CORS, and
  // without this a good key with an unlistable catalogue would be unusable.
  node.appendChild(new Option("Custom…", CUSTOM_MODEL));
  node.value = current || values[0] || "";
  if (customSel) syncCustomModel(sel, customSel);
}

/* Show the free-text id box only while its dropdown is on "Custom…". */
function syncCustomModel(selectSel, customSel) {
  const custom = $(customSel);
  if (custom) custom.hidden = $(selectSel)?.value !== CUSTOM_MODEL;
}

/* ============================================================ fallback chains
   THE ROUTER GUARANTEES same-provider-first ordering no matter what this box
   contains (see llm._candidates in the pipeline) — so this is not what makes
   "stay within the provider" true. It exists so the box is never left BLANK,
   which used to mean "whatever cross-provider chain the workflow's env default
   happens to carry" applies underneath, invisibly, until you look. An empty
   field now gets a same-provider suggestion the moment one is knowable, so
   what runs at 06:17 always matches what this screen shows. */

// The provider actually backing one stage's selected value — read from the
// value itself, not the shared "Provider" dropdown: the two stages are stored
// independently (gatekeeper_model / editor_model), so this stays correct even
// in the unusual case they end up on different providers.
function stageProvider(selectSel, customSel) {
  const select = el(selectSel);
  const raw = select.value === CUSTOM_MODEL
    ? (el(customSel)?.value || "").trim()
    : select.value;
  const i = raw.indexOf(":");
  return i > 0 ? raw.slice(0, i) : "";
}

// Other known models of `provider`, excluding the one currently primary for
// this stage. Comes only from DISCOVERED (this session's Refresh results) —
// deliberately no hardcoded per-vendor list, so a suggestion is never stale
// against what the key can actually see today.
function siblingModels(provider, excludeFull) {
  const ids = (DISCOVERED[provider] || []).filter((id) => id && !NON_CHAT.test(id));
  return ids
    .map((id) => `${provider}:${id}`)
    .filter((full) => full !== excludeFull);
}

// Tracks the last value THIS code wrote into each chain box, per stage, so a
// value the user typed by hand is never silently overwritten — only a box that
// is empty, or still holds exactly what we last suggested, is eligible.
const AUTO_CHAIN = { gatekeeper: undefined, editor: undefined };

function autoSuggestChain(stage, selectSel, customSel, chainSel) {
  const input = el(chainSel);
  const untouched = input.value === "" || input.value === AUTO_CHAIN[stage];
  if (!untouched) return;
  const provider = stageProvider(selectSel, customSel);
  const primaryFull = el(selectSel).value === CUSTOM_MODEL
    ? (el(customSel)?.value || "").trim()
    : el(selectSel).value;
  const siblings = provider ? siblingModels(provider, primaryFull) : [];
  if (!siblings.length) return;          // nothing to suggest yet — leave as is
  const suggestion = siblings.join(", ");
  input.value = suggestion;
  AUTO_CHAIN[stage] = suggestion;
}

function fillSettings() {
  const s = SETTINGS || {};
  setProp("#accountEmail", "textContent", SESSION?.user?.email || "—");
  for (const p of Object.values(PROVIDERS)) {
    const input = $(p.keyInput);
    if (input) input.value = s[p.column] || "";
  }

  const gateChain = (s.gatekeeper_fallback_models || []).join(", ");
  const editChain = (s.editor_fallback_models || []).join(", ");
  setProp("#setGatekeeperChain", "value", gateChain);
  setProp("#setEditorChain", "value", editChain);
  // A non-empty STORED chain is a deliberate choice (yours, or a prior
  // auto-suggestion already saved) — mark it as "known", not "blank", so
  // opening Settings again does not treat it as fair game to overwrite.
  AUTO_CHAIN.gatekeeper = gateChain || undefined;
  AUTO_CHAIN.editor = editChain || undefined;

  setProp("#setEffort", "value", s.openai_reasoning_effort || "low");

  // Default the provider picker to whatever the gatekeeper is already using,
  // so Refresh lists the models you are most likely to be changing.
  const provider = (s.gatekeeper_model || "").split(":")[0];
  setProp("#setProvider", "value", PROVIDERS[provider] ? provider : "openai");

  const active = el("#setProvider").value;
  fillModelSelect("#setGatekeeper", s.gatekeeper_model || "", active,
                  "#setGatekeeperCustom");
  fillModelSelect("#setEditor", s.editor_model || "", active, "#setEditorCustom");
  syncKeyDots();
  syncProviderUI(active);

  // Covers the case Refresh already ran earlier this session (DISCOVERED is
  // cached) and the stored chain was empty — no need to press Refresh again.
  autoSuggestChain("gatekeeper", "#setGatekeeper", "#setGatekeeperCustom",
                   "#setGatekeeperChain");
  autoSuggestChain("editor", "#setEditor", "#setEditorCustom", "#setEditorChain");
}

async function refreshModels() {
  const provider = el("#setProvider").value;
  const key = keyFor(provider);
  const hint = el("#discoveryHint");
  if (!key) {
    hint.textContent = `No ${PROVIDERS[provider].label} key entered above yet.`;
    return;
  }
  hint.textContent = "Listing models…";
  try {
    DISCOVERED[provider] = await PROVIDERS[provider].discover(key);
    const usable = DISCOVERED[provider].filter((id) => !NON_CHAT.test(id));
    hint.textContent = `${usable.length} usable models for this ` +
                       `${PROVIDERS[provider].label} key.`;
    fillModelSelect("#setGatekeeper", el("#setGatekeeper").value, provider,
                    "#setGatekeeperCustom");
    fillModelSelect("#setEditor", el("#setEditor").value, provider,
                    "#setEditorCustom");
    // Now that this provider's model list is known, an empty/untouched chain
    // box can finally be filled with same-provider siblings.
    autoSuggestChain("gatekeeper", "#setGatekeeper", "#setGatekeeperCustom",
                     "#setGatekeeperChain");
    autoSuggestChain("editor", "#setEditor", "#setEditorCustom", "#setEditorChain");
  } catch (e) {
    // A browser-side list can fail on CORS even with a perfectly good key —
    // several of these vendors send no CORS headers on /models. Say so, and
    // point at the escape hatch instead of implying the key is bad.
    hint.textContent = `Could not list models — ${e.message}. ` +
                       `If this is a CORS block, pick "Custom…" and type the ` +
                       `exact model id; the pipeline calls the API server-side.`;
  }
}

/* A key that is present but dead is the failure that is worst to discover at
   06:17, so it is checkable here, on demand, before it costs a briefing. */
async function testKeys() {
  const out = el("#testResults");
  out.innerHTML = "";
  for (const [id, p] of Object.entries(PROVIDERS)) {
    const key = keyFor(id);
    const line = document.createElement("div");
    line.className = "set__result";
    if (!key) {
      line.dataset.state = "skip";
      line.textContent = `${p.label}: no key set`;
      out.appendChild(line);
      continue;
    }
    line.dataset.state = "wait";
    line.textContent = `${p.label}: checking…`;
    out.appendChild(line);
    try {
      const ids = await p.discover(key);
      line.dataset.state = "ok";
      line.textContent = `${p.label}: ok — ${ids.length} models visible`;
    } catch (e) {
      line.dataset.state = "bad";
      line.textContent = `${p.label}: ${e.message}`;
    }
  }
}

function openSettings() {
  setProp("#settingsModal", "hidden", false);
  setProp("#saveState", "textContent", "");
  loadSettings()
    .then(fillSettings)
    .catch((e) => setProp("#saveState", "textContent", e.message));
}

function closeSettings() { setProp("#settingsModal", "hidden", true); }

function wireSettings() {
  // Build the provider rows and dropdown BEFORE anything queries them.
  renderProviderControls();

  on("#settingsBtn", "click", openSettings);
  on("#signOutBtn", "click", signOut);
  $$("[data-close-settings]").forEach((n) =>
    n.addEventListener("click", closeSettings));
  on("#refreshModels", "click", refreshModels);
  on("#testKeys", "click", testKeys);

  on("#setProvider", "change", (e) => {
    const provider = e.target.value;
    syncProviderUI(provider);
    fillModelSelect("#setGatekeeper", el("#setGatekeeper").value, provider,
                    "#setGatekeeperCustom");
    fillModelSelect("#setEditor", el("#setEditor").value, provider,
                    "#setEditorCustom");
    el("#discoveryHint").textContent = DISCOVERED[provider]
      ? `${DISCOVERED[provider].length} models cached for this provider.`
      : `Enter the ${PROVIDERS[provider].label} key above, then Refresh.`;
    // Switching providers just changed both stages' primary model — re-suggest
    // an untouched chain for each against the new provider.
    autoSuggestChain("gatekeeper", "#setGatekeeper", "#setGatekeeperCustom",
                     "#setGatekeeperChain");
    autoSuggestChain("editor", "#setEditor", "#setEditorCustom", "#setEditorChain");
  });

  // Reveal + key-dot listeners are DELEGATED: the rows are generated, so a
  // direct binding here would attach to nothing and both would silently die.
  const keys = el("#providerKeys");
  if (keys && keys.addEventListener) {
    keys.addEventListener("click", (e) => {
      const btn = e.target.closest && e.target.closest("[data-reveal]");
      if (!btn) return;
      const input = document.getElementById(btn.dataset.reveal);
      if (input) input.type = input.type === "password" ? "text" : "password";
    });
    keys.addEventListener("input", syncKeyDots);
  }

  // Custom-model boxes appear only on "Custom…"; changing which model is
  // primary also re-suggests that stage's fallback chain (still only when the
  // box is empty/untouched — see AUTO_CHAIN).
  [["gatekeeper", "#setGatekeeper", "#setGatekeeperCustom", "#setGatekeeperChain"],
   ["editor", "#setEditor", "#setEditorCustom", "#setEditorChain"]]
    .forEach(([stage, sel, custom, chain]) => {
      on(sel, "change", () => {
        syncCustomModel(sel, custom);
        autoSuggestChain(stage, sel, custom, chain);
      });
      // Typing a custom id is also a model change, just via a text field
      // instead of a select — the "input" event covers keystrokes.
      on(custom, "input", () => autoSuggestChain(stage, sel, custom, chain));
    });

  on("#saveSettings", "click", async () => {
    const state = el("#saveState");
    state.textContent = "Saving…";
    // Last chance: if a chain is still empty (Refresh was never pressed for
    // this provider), this is a no-op; otherwise it fills in same-provider
    // siblings so what gets saved is never silently cross-provider-first.
    autoSuggestChain("gatekeeper", "#setGatekeeper", "#setGatekeeperCustom",
                     "#setGatekeeperChain");
    autoSuggestChain("editor", "#setEditor", "#setEditorCustom", "#setEditorChain");
    try {
      await saveSettings();
      const gateEmpty = !el("#setGatekeeperChain").value.trim();
      const editEmpty = !el("#setEditorChain").value.trim();
      state.textContent = (gateEmpty || editEmpty)
        ? "Saved — no fallback known yet for " +
          [gateEmpty && "gatekeeper", editEmpty && "editor"].filter(Boolean).join(" / ") +
          " (press Refresh to enable one). Applies at the next 06:17 run."
        : "Saved — applies at the next 06:17 run.";
    } catch (e) {
      state.textContent = e.message;
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeSettings();
  });
}

/* --------------------------------- boot ---------------------------------- */
/* Everything past the sign-in gate. Split out of boot() so the auth check can
   decide whether to run it at all: with `anon` revoked in migration 004, a
   signed-out visitor would otherwise watch buildStore() return zero rows and
   get told "the pipeline has not run yet" — a confident and completely wrong
   diagnosis of what is really a missing session. */
async function startApp() {
  loadModelLabel();                     // last known model, for the offline path
  setModelBusy("fetching briefing…");   // idle state is set by render()/renderError()

  // restore preferences before the first render
  const prefs = loadPrefs();
  // Migrate values saved by the previous free-form range input onto the new
  // explicit reading modes so a visible preset always matches the active cut.
  const savedScore = Number.isFinite(prefs.minScore) ? prefs.minScore : 0;
  MIN_SCORE = savedScore >= 9 ? 9 : savedScore >= 8 ? 8 : savedScore >= 7 ? 7 : 0;
  COLLAPSED = new Set(Array.isArray(prefs.collapsed) ? prefs.collapsed : []);
  LB_SORT = LB_SORTS[prefs.lbSort] ? prefs.lbSort : "features";
  LB_ALL_DAYS = !!prefs.lbAllDays;
  ACTIVE_TAB = typeof prefs.activeTab === "string" ? prefs.activeTab : "all";

  try {
    STORE = await buildStore();
    // A successful read that returns nothing is the signature of RLS with no
    // SELECT policy, so say that rather than "no briefing yet".
    if (!STORE.dates.length) throw new Error(
      "Supabase returned no briefings. Either the pipeline has not run yet, or " +
      "the SELECT policy on `briefings` is missing — an anon read blocked by " +
      "RLS returns an empty list, not an error.");

    // ---- UI wiring -------------------------------------------------------
    // Isolated from data loading on purpose. A control that has been removed
    // from index.html (or a stale cached script.js referencing one) degrades to
    // a console warning instead of blanking the entire board.
    try {
      on("#archiveSelect", "change", (e) => showIndex(Number(e.target.value)));
      on("#prevDay", "click", () => showIndex(currentIndex + 1));
      on("#nextDay", "click", () => showIndex(currentIndex - 1));
      on("#latestBtn", "click", () => showIndex(0));

      const input = el("#searchInput");
      if (input) {
        input.addEventListener("input", (e) => { QUERY = e.target.value; applyFilter(); });
        input.addEventListener("keydown", (e) => {
          if (e.key === "Escape") {
            input.value = ""; QUERY = ""; applyFilter(); input.blur();
          }
        });
      }
      on("#searchClear", "click", () => {
        if (!input) return;
        input.value = ""; QUERY = ""; applyFilter(); input.focus();
      });

      // Score presets are explicit reading modes rather than an unlabelled
      // continuous slider: all noteworthy items, or increasingly strict cuts.
      $$("#scorePresets [data-min-score]").forEach((btn) => {
        btn.addEventListener("click", () => {
          MIN_SCORE = Number(btn.dataset.minScore) || 0;
          savePrefs({ minScore: MIN_SCORE });
          applyFilter();
        });
      });

      // category collapse — ONE delegated listener on the board. Survives every
      // re-render (filter, score, day-flip) because it is bound to the
      // container, not to buttons that get destroyed and rebuilt.
      // Bound to `document`, not to #board: a listener on the container dies if
      // the container is ever replaced rather than emptied, and Safari can hand
      // back a TEXT node as e.target for a touch-generated click, which has no
      // .closest(). Both are resolved here.
      document.addEventListener("click", (e) => {
        let node = e.target;
        if (node && node.nodeType === 3) node = node.parentElement;  // text node
        if (!node || typeof node.closest !== "function") return;
        const btn = node.closest("[data-cat-toggle]");
        if (btn) {
          e.preventDefault();
          toggleCategory(btn.dataset.catToggle);
          return;
        }

        const detailsToggle = node.closest("[data-card-details]");
        if (detailsToggle) {
          e.preventDefault();
          const card = detailsToggle.closest(".card");
          if (!card) return;
          const key = card.dataset.cardKey || detailsToggle.dataset.cardDetails;
          const open = card.dataset.detailsOpen !== "true";
          if (open) EXPANDED_CARDS.add(key); else EXPANDED_CARDS.delete(key);
          paintCardDetails(card, open);
          requestReadProgress();
          return;
        }

        const notableToggle = node.closest("[data-notable-toggle]");
        if (notableToggle) {
          e.preventDefault();
          SHOW_ALL_NOTABLE = !SHOW_ALL_NOTABLE;
          applyFilter();
          requestAnimationFrame(() => {
            const replacement = $("[data-notable-toggle]");
            if (replacement) replacement.focus({ preventScroll: true });
          });
        }
      });
      // Tab bar — ONE delegated listener; the bar is rebuilt on every render.
      const tabBar = el("#tabBar");
      if (tabBar && tabBar.addEventListener) {
        tabBar.addEventListener("click", (e) => {
          const btn = e.target.closest && e.target.closest("[data-tab]");
          if (btn && tabBar.contains(btn)) setActiveTab(btn.dataset.tab);
        });
        tabBar.addEventListener("scroll", syncTabOverflow, { passive: true });
      }

      on("#collapseAll", "click", (e) => { e.preventDefault(); setAllCategories(true); });
      on("#expandAll", "click", (e) => { e.preventDefault(); setAllCategories(false); });

      // leaderboard controls
      $$("#boardBody [data-sort]").forEach((btn) => {
        btn.classList.toggle("is-on", btn.dataset.sort === LB_SORT);
        btn.addEventListener("click", () => {
          LB_SORT = btn.dataset.sort;
          $$("#boardBody [data-sort]").forEach((b) =>
            b.classList.toggle("is-on", b === btn));
          savePrefs({ lbSort: LB_SORT });
          renderLeaderboard();
        });
      });
      setProp("#lbAllDays", "checked", LB_ALL_DAYS);
      on("#lbAllDays", "change", (e) => {
        LB_ALL_DAYS = e.target.checked;
        savePrefs({ lbAllDays: LB_ALL_DAYS });
        renderLeaderboard();
      });

      // diff controls
      on("#diffSelect", "change", (e) => { DIFF_AGAINST = e.target.value; renderDiff(); });

      // panel toggles
      [["#healthToggle", "#healthBody"], ["#boardToggle", "#boardBody"],
       ["#diffToggle", "#diffBody"]].forEach(([t, b]) => {
        const toggle = el(t), body = el(b);
        if (!toggle || !body) return;
        toggle.addEventListener("click", () => {
          const open = toggle.getAttribute("aria-expanded") === "true";
          if (!open) {
            $$(".diagnostics .health").forEach((panel) => {
              const peerToggle = $(".health__toggle", panel);
              const peerBody = $(".health__body", panel);
              if (peerToggle && peerToggle !== toggle) peerToggle.setAttribute("aria-expanded", "false");
              if (peerBody && peerToggle !== toggle) peerBody.hidden = true;
              if (peerToggle !== toggle) panel.classList.remove("is-expanded");
            });
          }
          toggle.setAttribute("aria-expanded", String(!open));
          body.hidden = open;
          toggle.closest(".health")?.classList.toggle("is-expanded", !open);
          requestReadProgress();
        });
      });

      window.addEventListener("resize", () => {
        syncTabOverflow();
        requestReadProgress();
      }, { passive: true });
      window.addEventListener("scroll", requestReadProgress, { passive: true });

      // keyboard: "/" filter, j/k days, c/e collapse-expand
      document.addEventListener("keydown", (e) => {
        // Ctrl/Cmd+C (copy) and Cmd+E were firing the collapse/expand shortcuts.
        if (e.metaKey || e.ctrlKey || e.altKey) return;
        const ae = document.activeElement;
        const typing = !!ae && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName) ||
                       (!!ae && ae.isContentEditable);
        if (e.key === "/" && !typing) {
          e.preventDefault(); if (input) input.focus(); return;
        }
        if (typing) return;
        if (e.key === "j") showIndex(currentIndex + 1);
        else if (e.key === "k") showIndex(currentIndex - 1);
        else if (e.key === "c") setAllCategories(true);
        else if (e.key === "e") setAllCategories(false);
        else if (e.key === "[" || e.key === "]") {
          const keys = ["all", ...orderCategories(Object.keys(
            (STORE.byDate[STORE.dates[currentIndex]] || {}).categories || {}))];
          const i = keys.indexOf(ACTIVE_TAB);
          const next = e.key === "]"
            ? keys[(i + 1) % keys.length]
            : keys[(i - 1 + keys.length) % keys.length];
          setActiveTab(next);
        }
      });

      if (MISSING.length) {
        console.warn("[DISPATCH] markup missing for:", MISSING.join(", "),
                     "\n  script.js build:", BUILD,
                     "\n  index.html build:", document.body.dataset.build || "(none)",
                     "\n  If those two differ, one file is stale — hard-reload " +
                     "(Cmd/Ctrl+Shift+R) or wait for the Pages CDN to refresh.");
      }
    } catch (wiringErr) {
      // Never let a control failure hide a briefing that loaded correctly.
      console.error("[DISPATCH] UI wiring failed:", wiringErr);
    }

    showIndex(0);
  } catch (e) {
    renderError(e.message);
  }
}

/* The gate. Wiring comes first and unconditionally: if the session check throws,
   the sign-in form still has to work, and that is exactly the moment somebody
   needs it. */
(async function boot() {
  checkAssetVersions();

  // The page now starts blank (body.is-locked, gate hidden), so an exception
  // before the showGate/hideGate decision would leave a permanently empty
  // screen with no way to sign in. Same reasoning as the UI-wiring try/catch
  // in startApp(): a broken control must never be able to hide the content.
  try {
    wireAuth();
    wireSettings();
  } catch (wiringErr) {
    console.error("[DISPATCH] auth/settings wiring failed:", wiringErr);
  }

  let session = null;
  try {
    session = await loadSession();
  } catch (e) {
    showGate(e.message);
    return;
  }
  if (!session) { showGate(); return; }

  hideGate();
  await startApp();
})();

function wireAuth() {
  on("#authForm", "submit", async (e) => {
    e.preventDefault();
    const button = el("#authSubmit");
    button.disabled = true;
    button.textContent = "Authenticating…";
    setProp("#authError", "hidden", true);

    const result = await signIn(el("#authEmail").value.trim(),
                                el("#authPassword").value);
    if (result.ok) {
      setProp("#authPassword", "value", "");
      hideGate();
      await startApp();
      return;
    }
    setProp("#authError", "textContent", result.error);
    setProp("#authError", "hidden", false);
    button.disabled = false;
    button.textContent = "Authenticate";
  });
}
