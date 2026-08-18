// Left-sidebar mobile toggle: the sidebar is always visible on desktop
// (lg and up); below that it's off-canvas, opened via the topbar's
// hamburger button and closed via its own overlay backdrop. No-op on
// pages without a sidebar (the logged-out top navbar layout never
// includes #sidebar).
document.addEventListener('DOMContentLoaded', () => {
    const sidebar = document.getElementById('sidebar');
    const toggle = document.getElementById('sidebarToggle');
    const overlay = document.getElementById('sidebarOverlay');
    if (!sidebar || !toggle) return;

    function closeMobileSidebar() {
        sidebar.classList.remove('mobile-open');
        if (overlay) overlay.classList.remove('show');
    }

    toggle.addEventListener('click', () => {
        sidebar.classList.toggle('mobile-open');
        if (overlay) overlay.classList.toggle('show', sidebar.classList.contains('mobile-open'));
    });

    if (overlay) overlay.addEventListener('click', closeMobileSidebar);

    // A nav click is a plain link (full page reload), so no explicit
    // close-on-navigate handler is needed — the next page load starts closed.
    window.addEventListener('resize', () => {
        if (window.innerWidth >= 992) closeMobileSidebar();
    });
});
