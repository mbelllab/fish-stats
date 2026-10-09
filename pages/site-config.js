// Fish Stats' settings for the shared stats pages (statistics/stats.js
// reads window.STATS_SITE; see the comment at its top). Loaded after site.js and
// episode-links.js, before stats.js, on every page.
window.STATS_SITE = {
  shows: ["No Such Thing As A Fish"],
  allKinds: true,                             // everything in the public feed counts: no "Main episodes" toggles
  search: false,                              // no transcripts here, so no search to link to
  episodes: "episodes.html",                  // the hub's episode count links to the full list
  // Episode links go to the episode's official page (from the public feed), not a transcript.
  link: (podcast, episode) => (window.EPISODE_LINKS || {})[episode] || "https://www.nosuchthingasafish.com/",
};
