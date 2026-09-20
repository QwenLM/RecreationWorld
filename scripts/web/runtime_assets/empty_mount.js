() => {
    // Primary signal: a known SPA mount root exists but has NO children (the app
    // failed to mount). This is the definitive, low-false-positive case.
    const roots = ['#root', '#app', '#__next', '#___gatsby', '#svelte'];
    const emptyRoot = roots.some(s => {
        const el = document.querySelector(s);
        return el && el.children.length === 0;
    });
    // Secondary: the body itself is essentially empty (stricter thresholds so a
    // genuinely sparse-but-valid page is not needlessly reloaded).
    const bareBody = (document.body
        ? (document.body.querySelectorAll('*').length < 5
           && document.body.innerText.trim().length < 10)
        : true);
    return emptyRoot || bareBody;
}
