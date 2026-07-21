/* DISPATCH — fetches briefing.json from a GitHub Gist and renders the board.
   No build step, no secrets in the browser: it only ever READS a public Gist. */

/* ========================= CONFIG — EDIT THESE TWO ========================= */
const CONFIG = {
  // Preferred: the Gist API endpoint. Always returns the latest revision and sends
  // CORS headers. Unauthenticated requests are rate-limited to 60/hr per IP — fine
  // for one reader. Put your Gist ID here.
  GIST_ID: "368b2174f9c6e7a09df1eae9d814940f",
  FILENAME: "briefing.json",

  // Fallback: a raw Gist URL WITHOUT the commit hash (…/raw/briefing.json) always
  // serves the newest content. Leave "" to skip. Used only if the API call fails.
  RAW_URL: "",
};
/* ========================================================================== */

const $ = (sel, root = document) => root.querySelector(sel);
const board = $("#board");

async function fetchBriefing() {
  // Try the API endpoint first.
  if (CONFIG.GIST_ID && CONFIG.GIST_ID !== "PUT_YOUR_GIST_ID_HERE") {
    try {
      const r = await fetch(`https://api.github.com/gists/${CONFIG.GIST_ID}`, {
        headers: { Accept: "application/vnd.github+json" },
        cache: "no-store",
      });
      if (r.ok) {
        const gist = await r.json();
        const file = gist.files[CONFIG.FILENAME];
        if (file) {
          // Large files come back truncated; follow raw_url in that case.
          const text = file.truncated ? await (await fetch(file.raw_url)).text()
                                       : file.content;
          return JSON.parse(text);
        }
      }
    } catch (_) { /* fall through to RAW_URL */ }
  }
  if (CONFIG.RAW_URL) {
    const r = await fetch(CONFIG.RAW_URL, { cache: "no-store" });
    if (r.ok) return r.json();
  }
  throw new Error("Could not load the briefing. Check GIST_ID / RAW_URL in script.js.");
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

function renderCard(item) {
  const node = $("#cardTpl").content.cloneNode(true);
  const score = typeof item.score === "number" ? item.score : 8;

  $(".meter", node).appendChild(buildMeter(score));
  $(".card__src", node).textContent = item.source || "";
  const scoreEl = $(".card__score", node);
  scoreEl.textContent = `${score}/10`;
  if (score >= 9) scoreEl.classList.add("hot");

  const titleLink = $(".card__title a", node);
  titleLink.textContent = item.title || "Untitled";
  titleLink.href = item.url || "#";

  $(".card__why", node).textContent = item.reasoning || "";

  const ul = $(".card__bullets", node);
  (item.bullets || []).forEach((b) => {
    const li = document.createElement("li");
    li.textContent = b;
    ul.appendChild(li);
  });

  $(".card__link", node).href = item.url || "#";
  return node;
}

function renderCategory(name, items) {
  const section = document.createElement("section");
  section.className = "category";
  const head = document.createElement("div");
  head.className = "category__head";
  head.innerHTML =
    `<span class="slash">//</span><h2>${escapeHtml(name)}</h2>` +
    `<span class="rule"></span><span class="count">[${items.length}]</span>`;
  section.appendChild(head);
  items.forEach((it) => section.appendChild(renderCard(it)));
  return section;
}

function renderNotable(items) {
  const wrap = document.createElement("section");
  wrap.className = "notable";
  wrap.innerHTML = `<h3 class="notable__head">// Also notable</h3>`;
  const list = document.createElement("div");
  list.className = "notable__list";
  items.forEach((it) => {
    const a = document.createElement("a");
    a.className = "notable__item";
    a.href = it.url || "#";
    a.target = "_blank"; a.rel = "noopener noreferrer";
    a.innerHTML =
      `<span class="notable__src">${escapeHtml(it.source || "")}</span>` +
      `<span class="notable__ttl">${escapeHtml(it.title || "")}</span>`;
    list.appendChild(a);
  });
  wrap.appendChild(list);
  return wrap;
}

function render(data) {
  board.innerHTML = "";
  const cats = data.categories || {};
  const catNames = Object.keys(cats).filter((k) => cats[k]?.length);
  let featTotal = 0;

  catNames.forEach((name) => {
    featTotal += cats[name].length;
    board.appendChild(renderCategory(name, cats[name]));
  });

  const notable = data.also_notable || [];
  if (notable.length) board.appendChild(renderNotable(notable));

  if (!catNames.length && !notable.length) {
    board.innerHTML = `<div class="state">No items in today's briefing.</div>`;
  }

  // header meta
  const tag = $("#statusTag");
  tag.textContent = "LIVE"; tag.dataset.state = "live";
  $("#dateStamp").textContent = data.date || "";
  $("#featCount").textContent = featTotal;
  $("#notableCount").textContent = notable.length;
  $("#catCount").textContent = catNames.length;
  if (data.generated_at) {
    $("#genStamp").textContent =
      "compiled " + new Date(data.generated_at).toUTCString().replace("GMT", "UTC");
  }
  $("#statBar").hidden = false;
}

function renderError(msg) {
  const tag = $("#statusTag");
  tag.textContent = "OFFLINE"; tag.dataset.state = "error";
  board.innerHTML =
    `<div class="state state--error"><p>${escapeHtml(msg)}</p></div>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* --------------------------------- boot ---------------------------------- */
fetchBriefing().then(render).catch((e) => renderError(e.message));
