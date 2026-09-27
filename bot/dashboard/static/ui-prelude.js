// Theme/scale/accent/density bootstrap, loaded synchronously in <head> so a
// saved preference never flashes the wrong look for one frame on load.
// Per-browser (localStorage), not server-side — a viewer preference, not bot
// configuration. Identical in the dashboard (bot/dashboard/static/) and the
// desktop app (desktop-app/ui/); tests/test_csp.py fails if the two differ.
// It used to be an inline <script>; it is a file so each page's
// Content-Security-Policy can forbid inline script (see bot/dashboard/server.py).
(function () {
  try {
    var root = document.documentElement;
    var theme = localStorage.getItem('bs-ui-theme');
    if (theme === 'light' || theme === 'dark') root.setAttribute('data-theme', theme);
    var scale = localStorage.getItem('bs-ui-scale');
    if (scale) root.style.zoom = scale;
    var accent = localStorage.getItem('bs-ui-accent');
    if (accent && accent !== 'blue') root.setAttribute('data-accent', accent);
    var density = localStorage.getItem('bs-ui-density');
    if (density === 'compact') root.setAttribute('data-density', density);
    // Applied here too (not just after body loads) so a returning viewer's
    // collapsed sidebar never flashes wide for one frame.
    if (localStorage.getItem('bs-sidebar-collapsed') === '1') root.classList.add('bs-sidebar-collapsed');
  } catch (_e) { /* localStorage unavailable (private mode, etc.) — fall back to defaults */ }
})();
