() => {
    const viewport = window.innerWidth * window.innerHeight;
    const images = document.querySelectorAll('img');
    let largeImageCover = false;
    for (const img of images) {
        const rect = img.getBoundingClientRect();
        const area = rect.width * rect.height;
        if (area / viewport > 0.7) {
            largeImageCover = true;
            break;
        }
    }
    const text = (document.body.innerText || '').trim();
    const textSelectable = text.length > 50;
    const totalElements = document.querySelectorAll('body *').length;
    const mainEl = document.querySelector('main') || document.querySelector('#__next') || document.body;
    const mainChildren = mainEl ? mainEl.children.length : 0;

    function getDepth(el, max) {
        if (max <= 0) return 0;
        let d = 0;
        for (const c of el.children) {
            d = Math.max(d, getDepth(c, max - 1) + 1);
        }
        return d;
    }
    const maxDepth = getDepth(document.body, 50);

    return {
        large_image_cover: largeImageCover,
        text_selectable: textSelectable,
        total_elements: totalElements,
        main_children_count: mainChildren,
        max_dom_depth: maxDepth,
    };
}
