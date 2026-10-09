// Compiled once at Docker build time (see Dockerfile's cssbuild stage) into
// watcher/admin/static/tailwind.css, replacing the Play CDN runtime compiler
// (cdn.tailwindcss.com) base.html used to load - confirmed (2026-10, backlog
// #26) as the cause of "the wizard's prompt textarea feels laggy": that
// script is a ~400KB JIT compiler that scans the whole page and keeps a
// MutationObserver running for the page's lifetime, explicitly called out in
// Tailwind's own docs as unsuitable for production. No template here uses a
// `dark:` variant (the whole admin UI just hardcodes dark colors directly),
// so there's no dark-mode config to carry over.
module.exports = {
  content: ["./watcher/admin/templates/**/*.html"],
  theme: { extend: {} },
  plugins: [],
};
