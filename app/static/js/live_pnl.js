/**
 * Polls /dashboard/live-pnl and updates the per-strategy P&L cells and the
 * total in place — no full page reload.
 *
 * Not literally "every second": Dhan's quote API is rate-limited to
 * 1 request/sec for the whole account, and this endpoint already batches
 * every leg of every open position into as few quote_data calls as
 * possible per poll. Polling faster than a couple of seconds just burns
 * that budget for no visible benefit — 3s is a safe, still-snappy default.
 */
(function () {
    const POLL_INTERVAL_MS = 3000;
    let timer = null;

    function formatRupees(value) {
        const sign = value >= 0 ? '+' : '-';
        return sign + '₹' + Math.abs(value).toLocaleString('en-IN', { maximumFractionDigits: 0 });
    }

    function applyColor(el, value) {
        el.classList.remove('text-success', 'text-danger', 'text-body-secondary');
        el.classList.add(value > 0 ? 'text-success' : value < 0 ? 'text-danger' : 'text-body-secondary');
    }

    async function poll() {
        let data;
        try {
            const res = await fetch('/dashboard/live-pnl', { headers: { Accept: 'application/json' } });
            if (!res.ok) return;
            data = await res.json();
        } catch (e) {
            return; // transient network/rate-limit hiccup — just try again next tick
        }

        const positions = data.positions || [];
        let total = 0;
        let anyPriced = false;

        positions.forEach((pos) => {
            const cell = document.querySelector(`[data-pnl-cell="${pos.user_strategy_id}"]`);
            if (!cell) return;
            if (pos.pnl_total === null || pos.pnl_total === undefined) {
                cell.textContent = 'pricing…';
                cell.classList.add('text-body-secondary');
                return;
            }
            anyPriced = true;
            total += pos.pnl_total;
            const pctText = pos.pnl_pct !== null && pos.pnl_pct !== undefined ? ` (${pos.pnl_pct.toFixed(1)}%)` : '';
            cell.textContent = formatRupees(pos.pnl_total) + pctText;
            applyColor(cell, pos.pnl_total);
        });

        const totalEl = document.getElementById('totalPnl');
        if (totalEl) {
            if (anyPriced) {
                totalEl.textContent = formatRupees(total);
                applyColor(totalEl, total);
            } else {
                totalEl.textContent = '—';
            }
        }

        const spinner = document.getElementById('pnlSpinner');
        if (spinner) spinner.style.display = 'none';

        // Nothing open anymore (all positions closed since page load) — stop polling.
        if (positions.length === 0 && timer) {
            clearInterval(timer);
            timer = null;
            const status = document.getElementById('pnlStatus');
            if (status) status.textContent = 'No open positions.';
        }
    }

    poll();
    timer = setInterval(poll, POLL_INTERVAL_MS);
})();
