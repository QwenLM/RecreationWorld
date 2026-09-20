() => {
    const out = {};
    out.lang = document.documentElement.hasAttribute('lang') && !!document.documentElement.lang.trim();
    out.viewport = !!document.querySelector('meta[name="viewport"]');

    // descriptive link text
    const links = [...document.querySelectorAll('a')].filter(a => a.getAttribute('href'));
    let badLinks = 0;
    for (const a of links) {
        const t = (a.textContent || '').trim();
        if (t.length < 2 && !a.querySelector('img') && !a.getAttribute('aria-label')
            && !a.getAttribute('title')) badLinks++;
    }
    out.link_score = links.length ? (1 - badLinks / links.length) : 1.0;

    // form-control labels
    const inputs = [...document.querySelectorAll('input:not([type="hidden"]), select, textarea')];
    let unlabeled = 0;
    for (const el of inputs) {
        const id = el.getAttribute('id');
        const hasFor = id && document.querySelector(`label[for="${CSS.escape(id)}"]`);
        const wrapLabel = el.closest('label');
        if (!hasFor && !wrapLabel && !el.getAttribute('aria-label')
            && !el.getAttribute('aria-labelledby') && !el.getAttribute('placeholder')) unlabeled++;
    }
    out.form_score = inputs.length ? (1 - unlabeled / inputs.length) : 1.0;

    // contrast (WCAG AA, font-size aware)
    function lum(r, g, b) {
        const a = [r, g, b].map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
        return 0.2126 * a[0] + 0.7152 * a[1] + 0.0722 * a[2];
    }
    function parseRGB(s) {
        const m = (s || '').match(/rgba?\(([^)]+)\)/); if (!m) return null;
        const p = m[1].split(',').map(x => parseFloat(x));
        return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
    }
    function effBg(el) {
        let e = el;
        while (e) { const c = parseRGB(getComputedStyle(e).backgroundColor); if (c && c.a > 0.1) return c; e = e.parentElement; }
        return { r: 255, g: 255, b: 255, a: 1 };
    }
    const cand = [...document.querySelectorAll('p,span,a,li,h1,h2,h3,h4,h5,h6,button,td,th,label')]
        .filter(el => {
            if (el.offsetParent === null) return false;
            for (const n of el.childNodes) if (n.nodeType === 3 && n.textContent.trim().length > 2) return true;
            return false;
        }).slice(0, 40);
    let total = 0, pass = 0;
    for (const el of cand) {
        const cs = getComputedStyle(el);
        const fg = parseRGB(cs.color); if (!fg) continue;
        const bg = effBg(el);
        const L1 = lum(fg.r, fg.g, fg.b), L2 = lum(bg.r, bg.g, bg.b);
        const ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
        const fs = parseFloat(cs.fontSize) || 16;
        const bold = (parseInt(cs.fontWeight) || 400) >= 700;
        const large = fs >= 24 || (fs >= 18.66 && bold);
        total++; if (ratio >= (large ? 3.0 : 4.5)) pass++;
    }
    out.contrast = total ? pass / total : 1.0;
    out.contrast_samples = total;
    return out;
}
