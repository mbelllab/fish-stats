// Shared helpers for every page (Facts, Stats). Each page loads this
// before its own script and uses Site.$, Site.esc, Site.fmtDate, Site.episode, Site.link and Site.plural.
// It also wires up every sideways-scrolling row (.sitebar, .scroll-row) so the hidden end fades out.
const Site = (() => {
  // The site root relative to this page: "" at the root, "../" from statistics/.
  const ROOT = (document.currentScript.getAttribute("src") || "").replace(/site\.js(\?.*)?$/, "");
  // An element by its id: the one lookup every page script uses.
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  // "2025-12-01" (or an episode name, which starts with its date) -> "1 Dec 2025"; "" when there is no date.
  const fmtDate = d => { const m = /^(\d{4})-(\d\d)-(\d\d)/.exec(d || ""); return m ? `${+m[3]} ${MONTHS[m[2] - 1]} ${m[1]}` : ""; };
  // An episode's name taken apart. The shapes the feeds use:
  //   Fish     "2024-07-11 - 539. Title", older "2014-03-08 - 1 Title"      -> num "539" / "1"
  //   plain    "2024-12-12 Brian Blessed"                                     -> num ""
  // label is "#539 Title", or just the title when there is no number.
  function episode(name) {
    name = String(name || "");
    const date = (/^\d{4}-\d\d-\d\d/.exec(name) || [""])[0];
    let rest = name.slice(date.length).replace(/^\s*-?\s*/, "");
    const num = (/^#?(\d+(?:\.\d+)?)\b/.exec(rest) || [, ""])[1];
    rest = rest.replace(/^\d+(?:\.\d+)?\.?\s+(?=#)/, "").replace(/^#?\d+(?:\.\d+)?\.?\s+/, "");
    const title = rest || name;
    return { date, num, title, label: num ? `#${num} ${title}` : title };
  }
  // A link to a transcript at a moment: t in seconds, started a few seconds early so the line isn't cut off.
  const link = (podcast, ep, t, lead = 3) =>
    ROOT + "index.html#" + new URLSearchParams({ v: "tr", p: podcast, e: ep, t: Math.max(0, Math.floor((t || 0) - lead)) });
  // "1 tick", "3 ticks"; plural defaults to word + "s".
  const plural = (n, word, many = word + "s") => `${Number(n).toLocaleString()} ${n === 1 ? word : many}`;

  // A row that scrolls sideways with its scrollbar hidden: .more-left / .more-right say which edge has more
  // to show (site.css fades that edge). Kept up to date on scroll, resize, new content and when fonts arrive.
  function scrollRow(el) {
    if (!el || el.dataset.scrollRow) return;
    el.dataset.scrollRow = "1";
    const upd = () => {
      const left = el.scrollLeft, rest = el.scrollWidth - el.clientWidth - left;
      el.classList.toggle("more-left", left > 4);
      el.classList.toggle("more-right", rest > 4);
    };
    el.addEventListener("scroll", upd, { passive: true });
    if (globalThis.ResizeObserver) new ResizeObserver(upd).observe(el);
    new MutationObserver(upd).observe(el, { childList: true, subtree: true, characterData: true });
    if (document.fonts) document.fonts.ready.then(upd);
    upd();
  }
  const rows = () => document.querySelectorAll(".sitebar, .scroll-row").forEach(scrollRow);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", rows); else rows();

  // Back to top: one button on every page (a page opts out with <body data-no-totop>). Scrolls to 0, so no #top element is needed.
  function backToTop() {
    if (document.body.hasAttribute("data-no-totop")) return;
    const b = document.createElement("a");
    b.href = "#"; b.className = "btn totop"; b.textContent = "Back to top"; b.hidden = true;
    b.addEventListener("click", e => {
      e.preventDefault();
      const calm = matchMedia("(prefers-reduced-motion: reduce)").matches;
      scrollTo({ top: 0, behavior: calm ? "auto" : "smooth" });
    });
    document.body.appendChild(b);
    let queued = false;
    const upd = () => { queued = false; b.hidden = scrollY < 900; };
    addEventListener("scroll", () => { if (!queued) { queued = true; requestAnimationFrame(upd); } }, { passive: true });
    upd();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", backToTop); else backToTop();

  return { ROOT, $, esc, MONTHS, fmtDate, episode, link, plural, scrollRow };
})();
