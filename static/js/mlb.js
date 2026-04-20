/* MLB hub — lightweight rail enhancements.
   - Keyboard left/right arrow nav within a focused rail
   - Wheel -> horizontal scroll translation (desktop only, when Shift not held)
*/
(function () {
    'use strict';

    function tileWidth(rail) {
        var first = rail.querySelector('[role="listitem"]');
        if (!first) return 280;
        var style = window.getComputedStyle(rail);
        var gap = parseInt(style.columnGap || style.gap || '12', 10) || 12;
        return first.getBoundingClientRect().width + gap;
    }

    function onKeydown(e) {
        var rail = e.currentTarget;
        if (e.key === 'ArrowRight') {
            rail.scrollBy({ left: tileWidth(rail), behavior: 'smooth' });
            e.preventDefault();
        } else if (e.key === 'ArrowLeft') {
            rail.scrollBy({ left: -tileWidth(rail), behavior: 'smooth' });
            e.preventDefault();
        }
    }

    function onWheel(e) {
        // Only translate vertical wheel -> horizontal when horizontal intent
        // is ambiguous and the rail actually overflows. Respects shift key
        // (user probably wants page scroll).
        if (e.shiftKey) return;
        if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
        var rail = e.currentTarget;
        if (rail.scrollWidth <= rail.clientWidth) return;
        rail.scrollBy({ left: e.deltaY, behavior: 'auto' });
        e.preventDefault();
    }

    document.querySelectorAll('[data-mlb-rail]').forEach(function (rail) {
        rail.setAttribute('tabindex', '0');
        rail.addEventListener('keydown', onKeydown);
        rail.addEventListener('wheel', onWheel, { passive: false });
    });

    // Mock-bet entry — tile buttons carry a data-mlb-prefill JSON blob.
    // Defers to the existing openMockBetModal() from place_bet_modal.html.
    window.openMLBBet = function (btn) {
        var raw = btn && btn.getAttribute('data-mlb-prefill');
        if (!raw || typeof window.openMockBetModal !== 'function') return;
        try {
            window.openMockBetModal(JSON.parse(raw));
        } catch (err) { /* malformed payload — ignore */ }
    };
})();
