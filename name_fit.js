// Keep full nicknames on one line. Measuring and fitting happen only in the browser.
(() => {
  const style = document.createElement("style");
  style.textContent = `
    .fit-name { display: block; min-width: 0; white-space: nowrap; overflow: hidden; }
    .name-fit-text { display: inline-block; white-space: nowrap; max-width: none; }
  `;
  document.head.appendChild(style);
  const observed = new Map();
  let scheduled = false;
  const resize = new ResizeObserver(entries => {
    if (entries.some(({ target }) => observed.get(target) !== target.clientWidth)) schedule();
  });
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(fit);
  }
  function fit() {
    scheduled = false;
    const names = new Set(document.querySelectorAll(".fit-name"));
    for (const el of observed.keys()) {
      if (!names.has(el)) { resize.unobserve(el); observed.delete(el); }
    }
    const measurements = [];
    for (const el of names) {
      if (!observed.has(el)) resize.observe(el);
      observed.set(el, el.clientWidth);
      // Hidden tabs/rooms are fitted when their available width becomes nonzero.
      if (!el.clientWidth || !el.getClientRects().length) continue;
      let text = el.firstElementChild;
      if (el.childNodes.length !== 1 || !text?.classList.contains("name-fit-text")) {
        text = document.createElement("span");
        text.className = "name-fit-text";
        while (el.firstChild) text.appendChild(el.firstChild);
        el.appendChild(text);
      }
      el.title = el.textContent.trim();
      const css = getComputedStyle(el);
      const base = parseFloat(css.fontSize);
      const width = el.clientWidth - parseFloat(css.paddingLeft) - parseFloat(css.paddingRight);
      measurements.push({ text, base, width });
    }
    // Capture every available width before restoring fonts, so one long title cannot
    // temporarily widen a grid and corrupt measurements of the names that follow it.
    for (const { text, base } of measurements) text.style.fontSize = `${base}px`;
    const sizes = measurements.map(({ text, base, width }) => ({
      text, size: text.scrollWidth <= width ? base
        : Math.max(1, base * Math.max(0, width - 1) / Math.max(1, text.scrollWidth))
    }));
    for (const { text, size } of sizes) text.style.fontSize = `${size}px`;
  }
  // Re-rendered text is refitted, and detached elements are unobserved rather than retained.
  new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true, characterData: true });
  window.addEventListener("resize", schedule);
  document.fonts.ready.then(schedule);
  schedule();
})();
