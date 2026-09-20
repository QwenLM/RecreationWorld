() => {
    return Promise.race([
        document.fonts.ready,
        new Promise(resolve => setTimeout(resolve, 5000))
    ]);
}
