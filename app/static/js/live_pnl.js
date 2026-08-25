/**
 * Polls /dashboard/live-pnl and updates the per-strategy P&L cells and the
 * total in place — no full page reload.
 *
 * Not literally "every second": Dhan's quote API is rate-limited to
 * 1 request/sec per account, and this endpoint already batches every leg
 * of every open position into as few quote_data calls as possible per
 * poll. Polling faster than a few seconds just burns that budget for no
 * visible benefit and competes with the background scheduler's own quote
 * calls for the same account (see app/dhan/helpers.py's per-account
 * throttle) — 5s (widened from 3s on 2026-08-25, after a real sustained
 * 429 outage) is a still-snappy default with more headroom.
 */
(function () {
    const POLL_INTERVAL_MS = 5000;
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
            const res = await fetch((window.BASE_PATH || '') + '/dashboard/live-pnl', { headers: { Accept: 'application/json' } });
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
            if (cell) {
                if (pos.pnl_total === null || pos.pnl_total === undefined) {
                    cell.textContent = 'pricing…';
                    cell.classList.add('text-body-secondary');
                } else {
                    anyPriced = true;
                    total += pos.pnl_total;
                    const pctText = pos.pnl_pct !== null && pos.pnl_pct !== undefined ? ` (${pos.pnl_pct.toFixed(1)}%)` : '';
                    cell.textContent = formatRupees(pos.pnl_total) + pctText;
                    applyColor(cell, pos.pnl_total);
                }
            }

            // Per-leg current price + live P&L on the Dashboard's Running
            // Orders table — keyed "{user_strategy_id}:{security_id}",
            // distinct from the plain-UUID key above (whole-position
            // aggregate), so the two never collide on the same attribute.
            (pos.legs || []).forEach((leg) => {
                const key = `${pos.user_strategy_id}:${leg.security_id}`;
                const priceCell = document.querySelector(`[data-price-cell="${key}"]`);
                if (!priceCell) return;

                const pctCell = document.querySelector(`[data-pct-cell="${key}"]`);
                const legPnlCell = document.querySelector(`[data-pnl-cell="${key}"]`);

                if (leg.current_price === null || leg.current_price === undefined) {
                    priceCell.textContent = 'pricing…';
                    if (pctCell) pctCell.textContent = '—';
                    // A quote fetch failing later must not leave a stale
                    // number sitting under "Live P&L" claiming to still be
                    // current — reset it the same way, not just the price.
                    if (legPnlCell) {
                        legPnlCell.textContent = 'pricing…';
                        legPnlCell.classList.remove('text-success', 'text-danger');
                        legPnlCell.classList.add('text-body-secondary');
                    }
                    return;
                }
                priceCell.textContent = leg.current_price.toFixed(2);
                priceCell.classList.remove('text-body-secondary');

                const entryPrice = parseFloat(priceCell.dataset.entryPrice);
                const qty = parseFloat(priceCell.dataset.qty);

                // Plain premium move (current vs entry), not P&L-adjusted for
                // side — "how far has the price itself moved", the number a
                // SL/target percentage is actually measured against.
                if (pctCell && !Number.isNaN(entryPrice) && entryPrice !== 0) {
                    const pct = ((leg.current_price - entryPrice) / entryPrice) * 100;
                    pctCell.textContent = (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%';
                    pctCell.classList.remove('text-body-secondary', 'text-success', 'text-danger');
                    pctCell.classList.add(pct > 0 ? 'text-success' : pct < 0 ? 'text-danger' : 'text-body-secondary');
                }

                if (legPnlCell && !Number.isNaN(entryPrice) && !Number.isNaN(qty)) {
                    const perUnit = priceCell.dataset.side === 'SELL'
                        ? (entryPrice - leg.current_price)
                        : (leg.current_price - entryPrice);
                    const legPnl = perUnit * qty;
                    legPnlCell.textContent = formatRupees(legPnl);
                    applyColor(legPnlCell, legPnl);
                }
            });
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
