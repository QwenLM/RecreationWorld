async () => {
    const root = document.scrollingElement || document.documentElement;
    const scroller = window.__rbScreenshotScroller || root;
    const top = scroller.scrollTop || 0;
    const maximum = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
    const nudged = top < maximum ? top + 1 : Math.max(0, top - 1);
    const marker = document.createElement('i');
    marker.setAttribute('data-rb-screencast-repaint', '');
    marker.style.cssText = [
        'position:fixed', 'left:0', 'top:0', 'width:1px', 'height:1px',
        'z-index:2147483647', 'pointer-events:none',
        'background:rgba(0,0,0,0.01)'
    ].join(';');
    document.documentElement.appendChild(marker);
    scroller.scrollTop = nudged;
    if (scroller === root) window.scrollTo(0, nudged);
    await new Promise(resolve => requestAnimationFrame(resolve));
    scroller.scrollTop = top;
    if (scroller === root) window.scrollTo(0, top);
    marker.remove();
    await new Promise(resolve => requestAnimationFrame(
        () => requestAnimationFrame(resolve)));
}
