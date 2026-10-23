// High-score wall: one machine model at a time from a venue's floor, with its all-time top 10.
// Data comes from this app's own server (api/data/<view>), refetched every 5 minutes; the rotation runs on the
// data already loaded and never fetches.
"use strict";

const REFRESH_MS = 5 * 60 * 1000;   // refetch the data this often
const RETRY_MS = 30 * 1000;         // first load failed: try again this soon
const FADE_MS = 450;                // matches .slide's transition in wall.css
const TZ = "Europe/Ljubljana";
const ERAS = { EM: "Electro-mechanical", SS: "Solid state", DMD: "Dot-matrix display", LCD: "LCD" };

const params = new URLSearchParams(location.search);
const venueCode = (params.get("venue") || "").trim().toUpperCase();
const seconds = clampSeconds(params.get("seconds"));
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

const $ = (id) => document.getElementById(id);
const fmtScore = new Intl.NumberFormat("en-GB");   // 1,234,567,890
const fmtDate = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: TZ });
const fmtTime = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: TZ });

let slides = [];        // [{model_id, title, manufacturer, release_year, era, machine_type, cabinets, out_of_order, scores}]
let current = -1;       // index into slides of the machine on screen
let timer = null;       // the pending switch to the next machine (or the draw after a fade)
let latestScore = null; // newest played_at among the loaded scores (the data's end)
let loadedAt = null;    // when the data was last fetched successfully
let failedAt = null;    // when the last refetch failed (old data stays on screen)
let retrying = false;   // a quick retry after a failed first load is pending
let refresher = null;   // the 5-minute refetch

function clampSeconds(raw) {
  const n = Number.parseInt(raw || "", 10);
  if (!Number.isFinite(n)) return 15;
  return Math.min(300, Math.max(5, n));
}

async function view(name, query) {
  const r = await fetch(`api/data/${name}?${new URLSearchParams(query)}`);
  if (!r.ok) throw new Error(`reading ${name} failed: HTTP ${r.status}`);
  return r.json();
}

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") node.className = v; else node.setAttribute(k, v);
  }
  for (const c of children) if (c !== null && c !== undefined) node.append(c);
  return node;
}

function showMessage(text) {
  const m = $("message");
  m.textContent = text;
  m.hidden = !text;
}

/* ---------------- Chooser ---------------- */

async function chooser(note) {
  document.title = "High-score wall · Goat Arcade";
  $("chooser").hidden = false;
  if (note) $("chooser").querySelector(".hint").before(el("p", { class: "hint" }, note));
  const venues = await view("venues", { select: "venue_code,venue_name,city,machines,pinball_machines", order: "venue_name" });
  const list = $("venue-list");
  for (const v of venues) {
    const q = new URLSearchParams({ venue: v.venue_code });
    if (params.has("seconds")) q.set("seconds", String(seconds));
    list.append(el("li", null, el("a", { href: `?${q}` },
      el("span", { class: "code" }, v.venue_code), v.venue_name,
      el("span", { class: "sub" }, `${v.city} · ${v.machines} machines, ${v.pinball_machines} pinball`))));
  }
}

/* ---------------- Data ---------------- */

// One slide per model on the floor: the leaderboard is per model, so two cabinets of one title share a wall.
// Models without leaderboard entries (e.g. air hockey, a claw) are skipped.
function buildSlides(floor, board) {
  const byModel = new Map();
  for (const m of floor) {
    let s = byModel.get(m.model_id);
    if (!s) {
      s = { model_id: m.model_id, title: m.title, manufacturer: m.manufacturer, release_year: m.release_year,
            era: m.era, machine_type: m.machine_type, cabinets: 0, out_of_order: 0, scores: [] };
      byModel.set(m.model_id, s);
    }
    s.cabinets += 1;
    if (m.out_of_order) s.out_of_order += 1;
  }
  for (const row of board) {
    const s = byModel.get(row.model_id);
    if (s) s.scores.push(row);
  }
  const out = [...byModel.values()].filter((s) => s.scores.length > 0);
  for (const s of out) {
    // Ties share a rank: higher score first, then whoever set it first.
    s.scores.sort((a, b) => a.rank - b.rank || b.score - a.score || a.played_at.localeCompare(b.played_at));
    s.scores = s.scores.slice(0, 10);
  }
  out.sort((a, b) => a.title.localeCompare(b.title, "en"));
  return out;
}

async function load() {
  const [venues, floor] = await Promise.all([
    view("venues", { venue_code: `eq.${venueCode}`, select: "venue_code,venue_name,city" }),
    view("machine_floor", { venue_code: `eq.${venueCode}`,
      select: "machine_id,model_id,title,manufacturer,release_year,era,machine_type,out_of_order" }),
  ]);
  if (venues.length === 0) return { unknownVenue: true };
  const models = [...new Set(floor.map((m) => m.model_id))];
  const board = models.length === 0 ? [] : await view("leaderboard", {
    model_id: `in.(${models.join(",")})`,
    select: "model_id,rank,handle,nickname,score,venue_code,played_at",
    order: "model_id,rank,score.desc,played_at",
  });
  return { venue: venues[0], slides: buildSlides(floor, board), board };
}

// Fetch and swap the data in; the machine on screen stays (by model), the rotation timer is untouched.
async function refresh() {
  let data;
  try {
    data = await load();
  } catch (err) {
    failedAt = new Date();
    renderStatus();
    if (slides.length === 0 && !retrying) {
      showMessage("Can't load the high scores right now. Trying again in 30 seconds.");
      retrying = true;
      setTimeout(() => { retrying = false; refresh(); }, RETRY_MS);
    }
    throw err; // reported by annex-errors.js; the old data stays on screen
  }
  if (data.unknownVenue) {
    $("wall").hidden = true;
    $("fullscreen").hidden = true;
    showMessage("");
    clearTimeout(timer);
    clearInterval(refresher); // nothing to refresh: the chooser is shown once
    return chooser(`There is no venue "${venueCode}". Pick one:`);
  }
  failedAt = null;
  loadedAt = new Date();
  latestScore = data.board.reduce((max, r) => Math.max(max, Date.parse(r.played_at)), 0) || null;
  const v = data.venue;
  $("venue-name").textContent = v.venue_name;
  document.title = `High scores · ${v.venue_name}`;

  const onScreen = slides[current] && slides[current].model_id;
  slides = data.slides;
  if (slides.length === 0) {
    clearTimeout(timer);
    timer = null;
    current = -1;
    showMessage(`No high scores yet for the machines at ${v.venue_name}.`);
    return;
  }
  showMessage("");
  $("wall").hidden = false;
  const keep = slides.findIndex((s) => s.model_id === onScreen);
  if (timer === null) {
    show(0, false);                  // first load (or the floor had no scores before)
  } else if (keep >= 0) {
    current = keep;                  // same machine: redraw its rows in place, keep the timer
    render(slides[current]);
  } else {
    current = Math.min(current, slides.length - 1); // the machine left the floor: carry on from here
    render(slides[current]);
  }
}

/* ---------------- Rotation ---------------- */

function show(index, animate) {
  clearTimeout(timer);
  current = ((index % slides.length) + slides.length) % slides.length;
  const slide = $("slide");
  const draw = () => {
    render(slides[current]);
    slide.classList.remove("leaving");
    restartBar();
    // The next switch counts from when this machine appears, so the bar and the timer end together.
    timer = setTimeout(() => show(current + 1, true), seconds * 1000);
  };
  if (animate && !reducedMotion.matches) {
    slide.classList.add("leaving");
    timer = setTimeout(draw, FADE_MS); // one timer: a key press during the fade cancels this draw
  } else {
    draw();
  }
}

function restartBar() {
  const bar = $("bar");
  bar.style.setProperty("--slide-seconds", `${seconds}s`);
  bar.style.setProperty("--slide-steps", String(seconds));
  bar.classList.remove("run");
  void bar.offsetWidth; // restart the CSS animation
  bar.classList.add("run");
}

function render(s) {
  $("title").textContent = s.title;
  const kind = s.machine_type === "pinball" ? `${ERAS[s.era] || s.era || "Pinball"} pinball` : "Video";
  const parts = [s.manufacturer, s.release_year, kind];
  if (s.cabinets > 1) parts.push(`${s.cabinets} on the floor`);
  $("meta").textContent = parts.filter(Boolean).join(" · ");
  const badge = $("badge");
  badge.hidden = s.out_of_order === 0;
  badge.textContent = s.cabinets > 1 && s.out_of_order < s.cabinets ? `${s.out_of_order} of ${s.cabinets} out of order` : "Out of order";

  const rows = s.scores.map((r) => el("tr", { class: r.rank <= 3 ? `r${r.rank}` : "" },
    el("td", { class: "rank" }, String(r.rank)),
    el("td", { class: "handle" }, r.handle),
    el("td", { class: "nick" }, r.nickname, r.venue_code === venueCode ? el("span", { class: "here" }, "set here") : null),
    el("td", { class: "score" }, fmtScore.format(r.score)),
    el("td", { class: "date" }, fmtDate.format(new Date(r.played_at)))));
  $("rows").replaceChildren(...rows);

  $("counter").replaceChildren("Machine ", el("b", null, String(current + 1)), ` of ${slides.length}`);
  renderStatus();
}

function renderStatus() {
  const left = latestScore ? `Members' best games at all venues, up to ${fmtDate.format(new Date(latestScore))}` : "";
  const right = failedAt
    ? el("span", { class: "stale" }, `Couldn't refresh at ${fmtTime.format(failedAt)}; showing data from ${loadedAt ? fmtTime.format(loadedAt) : "earlier"}`)
    : loadedAt ? `Updated ${fmtTime.format(loadedAt)} · next machine every ${seconds} s` : "";
  $("status").replaceChildren(el("span", null, left), el("span", null, right));
}

/* ---------------- Screen ---------------- */

function setupScreen() {
  const fs = $("fullscreen");
  fs.hidden = !document.fullscreenEnabled;
  const toggle = () => (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen());
  fs.addEventListener("click", toggle);
  document.addEventListener("fullscreenchange", () => { fs.textContent = document.fullscreenElement ? "Exit full screen" : "Full screen"; });
  document.addEventListener("keydown", (e) => {
    if (e.key === "f" || e.key === "F") toggle();
    else if (e.key === "ArrowRight" && slides.length) show(current + 1, true);
    else if (e.key === "ArrowLeft" && slides.length) show(current - 1, true);
  });
  // Hide the pointer after 3 s without movement.
  let idle;
  const wake = () => { document.body.classList.remove("idle"); clearTimeout(idle); idle = setTimeout(() => document.body.classList.add("idle"), 3000); };
  document.addEventListener("mousemove", wake);
  wake();
}

if (venueCode) {
  setupScreen();
  showMessage("Loading high scores…");
  refresh();
  refresher = setInterval(() => { refresh(); }, REFRESH_MS);
} else {
  chooser();
}
