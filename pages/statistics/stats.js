// Shared helpers for the Stats pages. Each page loads its JSON (built by
// scripts/build_stats.py after every index) and uses these.
// esc, fmtDate and the episode-name parser come from site.js (loaded first).
// deno-lint-ignore no-unused-vars -- S is the global the stats pages use
//
// Another site can use these pages with fewer shows (the public Fish stats site does): it sets
// window.STATS_SITE before this file loads. Without it, everything is as on the full site.
//   shows:  the shows it has. With one, the show pickers are hidden, <html> gets class
//           "one-show" (stats.css then hides .multi-show text and shows .one-show-text).
//   search: false = only the stats pages, none of the full site's others: no main search
//           (S.searchUrl gives "", so pages show plain text), and <html> gets class "stats-only"
//           (stats.css then hides .full-site-only, e.g. a link to the Facts page, and shows .stats-only-text).
//   link(podcast, episode, t): where an episode link goes instead of the transcript.
//   skip:   hub cards (their hrefs) the hub leaves out.
//   allKinds: true = every episode counts, bonus ones too: pages drop their "Main episodes / All"
//           toggles (S.ALL_KINDS) and count everything.
const S = (() => {
  const { $, esc, fmtDate } = Site;
  const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const FISH = "No Such Thing As A Fish";
  const SITE = window.STATS_SITE || {};
  const SHOWS = SITE.shows || [FISH];
  const ONE_SHOW = SHOWS.length === 1;
  const ALL_KINDS = SITE.allKinds === true;
  if (ONE_SHOW) document.documentElement.classList.add("one-show");
  if (SITE.search === false) document.documentElement.classList.add("stats-only");
  // The main search (index.html#q=...), or "" on a site without one.
  const searchUrl = params => SITE.search === false ? "" : "../index.html#" + new URLSearchParams(params);
  const SHORT = { [FISH]: "Fish" };
  const PERSON = { James: "--james", Andy: "--andy", Anna: "--anna", Dan: "--dan", Rhys: "--rhys" };
  // The chart palette, all from site.css. People keep their own colour everywhere. Anyone else who
  // is named (a guest) gets the one --guest colour; "Unknown" gets the neutral --other and an
  // "Other" (everyone else) band the darker --line2.
  // The show gets a colour no person has (site.css may define --fish to override it).
  // ONE is for single-series charts that aren't about a person; SERIES for up to
  // four lines that aren't people or shows (the word tracker's compared words).
  const UNKNOWN = new Set(["", "?", "Other", "Unknown"]);
  function color(name) {
    if (PERSON[name]) return css(PERSON[name]);
    if (name === "Other") return css("--line2");     // "everyone else" bands: darker than Unknown
    return css(UNKNOWN.has(name || "") ? "--other" : "--guest");
  }
  const SHOW_VAR = { [FISH]: ["--fish", "#cbd5e1"] };
  function showColor(p) {
    const [v, d] = SHOW_VAR[p] || ["--other", "--other"];
    return css(v) || (d.startsWith("--") ? css(d) : d);
  }
  const isHost = n => !!PERSON[n];
  const one = () => css("--accent2");
  const series = i => css(["--accent2", "--warn", "--bad", "--good"][i % 4]);
  // A name for display: the "?" the data uses for an unlabelled speaker reads "Unknown".
  const who = n => UNKNOWN.has(n || "") ? (n === "Other" ? "Other" : "Unknown") : n;
  // "1 tick", "2 ticks".
  const plural = (n, word, many) => `${num(n)} ${n === 1 ? word : many || word + "s"}`;
  const link = SITE.link || Site.link;
  const mmss = s => { s = Math.round(s || 0); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}` : `${m}:${String(x).padStart(2, "0")}`; };
  const year = d => (d || "").slice(0, 4);
  const pct = x => (100 * x).toFixed(1) + "%";
  const num = x => Number(x).toLocaleString("en-GB");
  // "#373 Title". build_stats.py writes the bare title as `t`; otherwise it is read off the full episode name.
  const epLabel = r => (r.n ? "#" + r.n + " " : "") + (r.t || Site.episode(r.e).title);
  const load = name => fetch(name + ".json", { cache: "no-cache" }).then(r => { if (!r.ok) throw new Error(name); return r.json(); });
  const mean = a => a.length ? a.reduce((x, y) => x + y, 0) / a.length : 0;
  const median = a => { if (!a.length) return 0; const b = [...a].sort((x, y) => x - y), m = b.length >> 1; return b.length % 2 ? b[m] : (b[m - 1] + b[m]) / 2; };

  function built(d) {
    const el = $("built"); if (!el || !d.built) return;
    el.textContent = `Updated ${fmtDate(d.built.slice(0, 10))}, ${d.built.slice(11)} · rebuilds after each new episode`;
  }

  // Loading and error states: "Loading..." in the tiles until the data arrives, a plain message if it never does.
  function page(promise, render) {
    const t = $("tiles");
    if (t) t.innerHTML = '<p class="empty">Loading…</p>';
    return promise.then(d => { render(d); if (t && t.textContent === "Loading…") t.innerHTML = ""; }).catch(e => { console.error(e);
      const msg = '<p class="error" role="alert">Couldn\'t load the data. Check your connection and refresh the page.</p>';
      if (t) { t.innerHTML = msg; document.body.classList.add("failed"); } else document.querySelector(".page").insertAdjacentHTML("beforeend", msg); });
  }

  // Headline tiles. A value that isn't a number (a name, a phrase) gets the smaller text size.
  function tiles(el, list) {
    el.innerHTML = list.map(([k, v, s]) => `<div class="tile"><div class="k">${k}</div><div class="v${/^[\d\s.,:%–h/minx-]*$/.test(String(v)) ? "" : " txt"}">${v}</div><div class="s">${s || ""}</div></div>`).join("");
  }

  // Sortable table. cols: [{key, label, num, fmt(row)->html, sort(row)->value, cls}]
  // cls "long" lets long text (episode titles) wrap; cls "lo" hides the column on phones.
  function table(el, cols, rows, opt = {}) {
    let key = opt.sort || cols[0].key, dir = opt.dir || "desc", shown = opt.page || 100;
    const val = (c, r) => c.sort ? c.sort(r) : r[c.key];
    const cl = x => [x.num ? "num" : "", x.cls || ""].join(" ").trim();
    function draw() {
      const c = cols.find(x => x.key === key) || cols[0];
      const sorted = [...rows].sort((a, b) => { const x = val(c, a), y = val(c, b);
        const r = (x === y) ? 0 : (x === undefined || x === null || x === "") ? 1 : (y === undefined || y === null || y === "") ? -1 : x < y ? -1 : 1;
        return dir === "asc" ? r : -r; });
      const left = sorted.length - shown;
      el.innerHTML = `<div class="tbl"><table><thead><tr>${cols.map(x => `<th class="${cl(x)}" scope="col"
          ${x.key === key ? `aria-sort="${dir === "asc" ? "ascending" : "descending"}"` : ""}><button type="button" data-k="${esc(x.key)}" title="Sort by ${esc(x.label || "this")}">${esc(x.label)}</button></th>`).join("")}</tr></thead>
        <tbody>${sorted.slice(0, shown).map(r => `<tr>${cols.map(x => `<td class="${cl(x)}">${x.fmt ? x.fmt(r) : esc(r[x.key])}</td>`).join("")}</tr>`).join("")
        || `<tr><td colspan="${cols.length}" class="empty">Nothing matches.</td></tr>`}</tbody></table></div>
        ${left > 0 ? `<button class="btn alt more" type="button">${left <= 200 ? `Show all ${num(left)}` : `Show 200 more (${num(left)} left)`}</button>` : ""}`;
      el.querySelectorAll("th button").forEach(b => b.onclick = () => {
        const k = b.dataset.k; if (k === key) dir = dir === "asc" ? "desc" : "asc"; else { key = k; dir = cols.find(x => x.key === k).num ? "desc" : "asc"; }
        draw(); const nb = el.querySelector(`th button[data-k="${CSS.escape(k)}"]`); if (nb) nb.focus(); });
      const m = el.querySelector(".more"); if (m) m.onclick = () => { shown += 200; draw(); };
      fade(el.querySelector(".tbl"));
    }
    draw();
    return { set(newRows) { rows = newRows; shown = opt.page || 100; draw(); } };
  }

  // A right-edge fade on a table box while there are more columns off to the right.
  function fade(box) {
    if (!box) return;
    const upd = () => box.classList.toggle("more-right", box.scrollWidth - box.clientWidth - box.scrollLeft > 4);
    box.addEventListener("scroll", upd, { passive: true }); upd();
  }
  addEventListener("resize", () => document.querySelectorAll(".tbl").forEach(b =>
    b.classList.toggle("more-right", b.scrollWidth - b.clientWidth - b.scrollLeft > 4)));

  // Chart.js with the site's colours.
  const charts = {};
  function chart(id, config) {
    if (charts[id]) charts[id].destroy();
    const grid = css("--line"), tick = css("--muted");
    Chart.defaults.color = tick; Chart.defaults.font.family = css("--font"); Chart.defaults.borderColor = grid;
    config.options = Object.assign({ responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { labels: { boxWidth: 10, boxHeight: 10 } } } }, config.options || {});
    // Lines get a filled legend box in their own colour, like the bars.
    if (config.type === "line") config.data.datasets.forEach(d => { if (!d.backgroundColor) d.backgroundColor = d.borderColor; });
    const cv = $(id);
    cv.setAttribute("role", "img");
    if (!cv.getAttribute("aria-label")) { const h = cv.closest(".chart") && cv.closest(".chart").previousElementSibling;
      cv.setAttribute("aria-label", "Chart: " + (h && /^H[23]$/.test(h.tagName) ? h.textContent : id)); }
    charts[id] = new Chart(cv, config);
    return charts[id];
  }

  // Horizontal bar chart that shows every label: the box grows with the number of bars and long
  // labels are shortened so phones don't cut them off.
  const clip = (s, n) => s.length > n ? s.slice(0, n - 1) + "…" : s;
  function hbar(id, labels, data, colors, opt = {}) {
    const box = $(id).closest(".chart");
    box.style.height = (labels.length * 24 + 48) + "px";
    const n = matchMedia("(max-width: 760px)").matches ? 18 : 30;
    return chart(id, { type: "bar", data: { labels, datasets: [{ data, backgroundColor: colors }] },
      options: Object.assign({ indexAxis: "y", plugins: { legend: { display: false },
        tooltip: { callbacks: { title: c => labels[c[0].dataIndex] } } },
        scales: { y: { ticks: { autoSkip: false, callback: (_v, i) => clip(String(labels[i]), n) } } } }, opt) });
  }


  // Scatter dots that lead somewhere. With a mouse: hover shows the tooltip, a click opens it.
  // On a touch screen the first tap shows the tooltip, a second tap on the same dot opens it.
  // Either way the note under the chart names the last dot touched, with a real link.
  const touch = matchMedia("(hover: none)").matches;
  function dots(noteId, describe) {
    let lastHit = null;
    const note = $(noteId), base = note ? note.innerHTML : "";
    const show = r => { if (note) note.innerHTML = r ? `${describe(r).html} · <a href="${describe(r).href}">Open it</a>` : base; };
    return {
      elements: { point: { hitRadius: 10 } },
      onHover: (_ev, els) => { if (!touch && els[0]) show(els[0].element.$context.raw.r); },
      onClick: (_ev, els) => {
        if (!els[0]) return;
        const r = els[0].element.$context.raw.r, k = els[0].datasetIndex + ":" + els[0].index;
        show(r);
        if (touch && lastHit !== k) { lastHit = k; return; }
        location.href = describe(r).href;
      },
    };
  }

  function seg(el, options, value, onChange) {
    // A show picker on a one-show site: nothing to pick, so it isn't shown.
    const isShow = v => v === FISH;
    if (options.some(([v]) => isShow(v))) {
      options = options.filter(([v]) => !isShow(v) || SHOWS.includes(v));
      if (ONE_SHOW) { (el.closest(".ctl") || el).hidden = true; return; }
    }
    el.innerHTML = options.map(([v, l]) => `<button type="button" class="pill" data-v="${esc(v)}" aria-pressed="${v === value}">${esc(l)}</button>`).join("");
    el.onclick = e => { const b = e.target.closest("button"); if (!b) return;
      el.querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", x === b)); onChange(b.dataset.v); };
  }

  // Remember a page's controls in the URL (#show=Fish&mode=strict) so links and refreshes keep them.
  const state = {
    get: (k, d) => new URLSearchParams(location.hash.slice(1)).get(k) ?? d,
    set: (k, v) => { const p = new URLSearchParams(location.hash.slice(1)); if (v === null || v === "") p.delete(k); else p.set(k, v);
      history.replaceState(null, "", "#" + p.toString()); },
  };

  return { SITE, SHOWS, ONE_SHOW, ALL_KINDS, searchUrl, esc, css, isHost, color, showColor, one, series, who, plural, link, mmss, fmtDate, year, pct, num, epLabel, load, mean, median, built, page, tiles, table, chart, hbar, clip,
           dots, seg, state, FISH, SHORT };
})();
