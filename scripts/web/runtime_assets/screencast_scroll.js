async (wantedTop) => {
    const root = document.scrollingElement || document.documentElement;
    const candidates = [...document.querySelectorAll('*')];
    const explicit = candidates.filter(el => {
        if (!el || el === root || el === document.body ||
                el === document.documentElement) return false;
        if (el.scrollHeight <= el.clientHeight + 1) return false;
        const overflow = getComputedStyle(el).overflowY;
        return overflow === 'auto' || overflow === 'scroll' || overflow === 'overlay';
    });
    const rootRange = Math.max(0, root.scrollHeight - root.clientHeight);
    const nested = explicit.sort((a, b) =>
        (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight)
    )[0];
    // Prefer the browser's document scroller whenever the document itself
    // is scrollable. A body with overflow:auto can look scrollable while
    // Chromium actually owns the offset on document.scrollingElement.
    const scroller = rootRange > 1 ? root : (nested || root);
    window.__rbScreenshotScroller = scroller;
    const maximum = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
    const target = Math.max(0, Math.min(wantedTop, maximum));
    const nudge = target > 0 ? target - 1 : Math.min(1, maximum);
    const styleTargets = scroller === root
        ? [document.documentElement, document.body].filter(Boolean)
        : [scroller];
    const saved = styleTargets.map(el => ({
        el,
        value: el.style.getPropertyValue('scroll-behavior'),
        priority: el.style.getPropertyPriority('scroll-behavior'),
    }));
    const move = top => {
        if (scroller === root) {
            window.scrollTo({left: 0, top, behavior: 'instant'});
        } else {
            scroller.scrollTo({left: 0, top, behavior: 'instant'});
        }
    };
    try {
        // Archived sites commonly set html { scroll-behavior: smooth }.
        // Two animation frames then move only a few pixels (observed as
        // scrollY=10) and make the stitcher think the viewport is stale.
        for (const el of styleTargets) {
            el.style.setProperty('scroll-behavior', 'auto', 'important');
        }
        move(nudge);
        await new Promise(resolve => requestAnimationFrame(resolve));
        move(target);
        await new Promise(resolve => requestAnimationFrame(
            () => requestAnimationFrame(resolve)));
        return {
            scrollY: scroller === root
                ? (window.scrollY || 0)
                : (scroller.scrollTop || 0),
            height: scroller.scrollHeight || 0,
            viewportWidth: window.innerWidth || scroller.clientWidth || 0,
            viewportHeight: window.innerHeight || scroller.clientHeight || 0,
        };
    } finally {
        for (const {el, value, priority} of saved) {
            if (value) el.style.setProperty('scroll-behavior', value, priority);
            else el.style.removeProperty('scroll-behavior');
        }
    }
}
