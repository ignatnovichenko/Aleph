// Keep a branch in normal document flow so siblings track its expanding edge.
const moving = new WeakMap<HTMLElement, Animation>();

export function moveTaskBranch(panel: HTMLElement, open: boolean, reduced: boolean, content: () => string) {
  const previous = moving.get(panel);
  const canAnimate = !reduced && typeof panel.animate === "function";
  const from = !canAnimate || panel.hidden ? 0 : panel.getBoundingClientRect().height;
  previous?.cancel();
  moving.delete(panel);

  if (open && !panel.firstElementChild) panel.innerHTML = content();
  panel.inert = !open;
  panel.hidden = false;
  const finish = () => {
    panel.style.removeProperty("overflow");
    if (!open) {
      panel.hidden = true;
      panel.replaceChildren();
    }
  };
  if (!canAnimate) {
    finish();
    return;
  }
  const to = open ? panel.scrollHeight : 0;
  if (Math.max(from, to) > innerHeight * 2.5) {
    finish();
    return;
  }
  panel.style.overflow = "clip";
  const timing = {duration: open ? 285 : 215, easing: "cubic-bezier(.23,.83,.26,1)"};
  const animation = panel.animate([{height: from + "px"}, {height: to + "px"}], timing);
  moving.set(panel, animation);
  if (open && !previous) panel.firstElementChild?.animate(
    [{opacity: .48, transform: "translateY(-3px)"}, {opacity: 1, transform: "translateY(0)"}],
    {duration: 210, easing: timing.easing});
  void animation.finished.then(() => {
    if (moving.get(panel) !== animation) return;
    moving.delete(panel);
    finish();
  }, () => {});
}
