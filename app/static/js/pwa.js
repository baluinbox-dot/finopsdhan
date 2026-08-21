// Registers the service worker so the app is installable (Add to Home
// Screen / Chrome's install prompt). The service worker itself does no
// caching — see app/static/sw.js for why.
if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
        navigator.serviceWorker.register(window.BASE_PATH + "/sw.js").catch((err) => {
            console.warn("Service worker registration failed:", err);
        });
    });
}
