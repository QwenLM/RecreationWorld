() => {
    const TAGS = ['header','nav','main','section','footer','h1','h2','h3','img','article','aside'];
    const els = [];
    for (const tag of TAGS) {
        for (const el of document.querySelectorAll(tag)) {
            const r = el.getBoundingClientRect();
            const w = r.width, h = r.height;
            if (w <= 0 || h <= 0) continue;
            els.push({tag: tag, bbox: {
                x: r.left + window.scrollX, y: r.top + window.scrollY, w: w, h: h
            }});
        }
    }
    return {
        viewport: {width: window.innerWidth, height: window.innerHeight},
        page_height: (document.body ? document.body.scrollHeight : window.innerHeight),
        elements: els
    };
}
