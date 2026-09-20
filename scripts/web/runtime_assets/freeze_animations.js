(() => {
    const style = document.createElement('style');
    style.id = '__bench_freeze_animations__';
    style.textContent = `
        *, *::before, *::after {
            animation-duration: 0.001ms !important;
            animation-delay: 0.001ms !important;
            transition-duration: 0.001ms !important;
            transition-delay: 0.001ms !important;
        }
    `;
    const inject = () => {
        if (document.head && !document.getElementById('__bench_freeze_animations__')) {
            document.head.appendChild(style.cloneNode(true));
        }
    };
    inject();
    document.addEventListener('DOMContentLoaded', inject);
})();
