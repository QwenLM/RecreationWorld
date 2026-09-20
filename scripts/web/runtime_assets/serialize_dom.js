() => {
    function serialize(node) {
        if (node.nodeType === 3) {
            const text = node.textContent.trim();
            return text ? { type: 'text', content: text } : null;
        }
        if (node.nodeType !== 1) return null;
        const tag = node.tagName.toLowerCase();
        if (['script', 'style', 'noscript', 'svg', 'link', 'meta', 'template', 'iframe'].includes(tag)) return null;

        const id = node.id || '';
        if (['onetrust-consent-sdk', 'onetrust-banner-sdk', 'CybotCookiebotDialog',
             'google_tag_manager', 'fb-root'].includes(id)) return null;

        const classes = [...node.classList].filter(c => {
            if (/^(sc-|css-|svelte-|styled-)[a-zA-Z0-9]/.test(c)) return false;
            if (/^_[a-zA-Z0-9]{5,}$/.test(c)) return false;
            return true;
        });

        const el = { tag: tag };
        if (node.id) el.id = node.id;
        if (classes.length > 0) el.classes = classes;

        const attrs = {};
        for (const attr of ['href', 'src', 'alt', 'role', 'aria-label', 'aria-expanded',
                            'data-testid', 'type', 'placeholder', 'name', 'action', 'method']) {
            if (node.hasAttribute(attr)) attrs[attr] = node.getAttribute(attr);
        }
        if (Object.keys(attrs).length > 0) el.attributes = attrs;

        const children = [];
        for (const child of node.childNodes) {
            const s = serialize(child);
            if (s) children.push(s);
        }
        if (children.length > 0) el.children = children;

        return el;
    }
    return {
        title: document.title || '',
        body: document.body ? serialize(document.body) : null
    };
}
