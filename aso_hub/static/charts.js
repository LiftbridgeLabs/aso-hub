// Hover or focus a chart column to see its value. The table under the charts has the same numbers.
(() => {
  const tip = document.getElementById("chart-tip");
  if (!tip) return;
  const show = (target, x, y) => {
    tip.textContent = target.dataset.tip;
    tip.hidden = false;
    const left = Math.min(x + 12, window.innerWidth - tip.offsetWidth - 8);
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${y - tip.offsetHeight - 10}px`;
    document.querySelectorAll(".hit.on").forEach((el) => el.classList.remove("on"));
    target.classList.add("on");
  };
  const hide = (target) => {
    tip.hidden = true;
    target.classList.remove("on");
  };
  document.querySelectorAll(".chart .hit").forEach((hit) => {
    hit.addEventListener("pointermove", (e) => show(hit, e.clientX, e.clientY));
    hit.addEventListener("pointerleave", () => hide(hit));
    hit.addEventListener("focus", () => {
      const box = hit.getBoundingClientRect();
      show(hit, box.left + box.width / 2, box.top + 24);
    });
    hit.addEventListener("blur", () => hide(hit));
  });
})();
