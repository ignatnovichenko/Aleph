interface Drag {
  header: HTMLElement;
  pointer: number;
  x: number;
  start: number;
  y: number;
  time: number;
  height: number;
  velocity: number;
  anchor?: number;
}

export function bindSheetMotion(sheet: HTMLDialogElement, reducedMotion: MediaQueryList, busy: () => boolean) {
  let drag: Drag | undefined;
  let frame = 0;
  let stopTransition: (() => void) | undefined;
  let suppressClick = false;

  function containTouch(event: TouchEvent) {
    if (drag?.anchor !== undefined && event.cancelable) event.preventDefault();
  }

  function clearDrag() {
    const previous = drag;
    drag = undefined;
    cancelAnimationFrame(frame);
    frame = 0;
    previous?.header.removeEventListener("touchmove", containTouch);
    if (previous?.header.hasPointerCapture(previous.pointer)) previous.header.releasePointerCapture(previous.pointer);
  }

  function clearMotion() {
    stopTransition?.();
    sheet.classList.remove("opening", "dragging", "settling", "closing");
    sheet.style.removeProperty("--sheet-y");
    sheet.style.removeProperty("--sheet-shade");
    sheet.style.removeProperty("--sheet-duration");
  }
  function reset() {
    clearDrag();
    clearMotion();
  }

  function paint(offset: number, shade: number) {
    sheet.style.setProperty("--sheet-y", `${offset}px`);
    sheet.style.setProperty("--sheet-shade", String(shade));
  }

  function offset() {
    return new DOMMatrixReadOnly(getComputedStyle(sheet).transform).m42;
  }

  function settle(dismiss: boolean, velocity = 0) {
    if (!sheet.open || sheet.classList.contains("closing")) return;
    const from = offset();
    const shade = Number(getComputedStyle(sheet,"::backdrop").opacity);
    const target = dismiss ? Math.max(from + 1, innerHeight - sheet.getBoundingClientRect().top + from + 1) : 0;
    reset();
    if (reducedMotion.matches || (!dismiss && from < .5 && shade > .99)) {
      if (dismiss) sheet.close();
      return;
    }
    const duration = dismiss ? Math.max(160, Math.min(270, 235 - velocity * 48)) : 215;
    paint(from, shade);
    sheet.style.setProperty("--sheet-duration", `${duration}ms`);
    sheet.classList.add(dismiss ? "closing" : "settling");
    // Establish the release position once, so the transition starts at the finger.
    sheet.getBoundingClientRect();
    paint(target, dismiss ? 0 : 1);

    const finish = (event?: TransitionEvent) => {
      if (event && (event.target !== sheet || event.pseudoElement || event.propertyName !== "transform")) return;
      clearMotion();
      if (dismiss && sheet.open) sheet.close();
    };
    const timer = window.setTimeout(finish, duration + 60);
    sheet.addEventListener("transitionend", finish);
    stopTransition = () => {
      clearTimeout(timer);
      sheet.removeEventListener("transitionend", finish);
      stopTransition = undefined;
    };
  }

  function draw() {
    frame = 0;
    if (!drag || drag.anchor === undefined) return;
    const y = Math.max(0, drag.anchor + drag.y - drag.start);
    paint(y, Math.max(0, 1 - y / drag.height));
  }

  sheet.addEventListener("animationend", event => {
    if (event.target === sheet && event.animationName === "sheet-in") sheet.classList.remove("opening");
  });
  sheet.addEventListener("pointerdown", event => {
    if (!event.isPrimary) {
      if (drag?.anchor !== undefined) settle(false);
      else clearDrag();
      return;
    }
    suppressClick = false;
    if (busy() || drag || sheet.classList.contains("closing") || event.button !== 0 || !(event.target instanceof Element)) return;
    const header = event.target.closest<HTMLElement>(".sheet-header");
    if (!header || event.target.closest("button,a,input,select,textarea")) return;
    drag = {header, pointer: event.pointerId, x: event.clientX, start: event.clientY,
      y: event.clientY, time: event.timeStamp, height: Math.max(1, sheet.offsetHeight), velocity: 0};
    header.addEventListener("touchmove", containTouch, {passive: false});
  });
  window.addEventListener("pointermove", event => {
    if (!drag || drag.pointer !== event.pointerId) return;
    if (drag.anchor === undefined) {
      const vertical = event.clientY - drag.start;
      const horizontal = Math.abs(event.clientX - drag.x);
      if (Math.max(Math.abs(vertical), horizontal) < 6) return;
      if (horizontal > Math.abs(vertical) || vertical < 0) {
        clearDrag();
        return;
      }
      drag.anchor = offset();
      const shade = Number(getComputedStyle(sheet,"::backdrop").opacity);
      stopTransition?.();
      sheet.classList.remove("opening", "settling");
      sheet.classList.add("dragging");
      paint(drag.anchor, shade);
      suppressClick = true;
      try { drag.header.setPointerCapture(event.pointerId); } catch {}
    }
    const elapsed = event.timeStamp - drag.time;
    const speed = (event.clientY - drag.y) / Math.max(8, elapsed);
    drag.velocity = elapsed > 100 ? 0 : drag.velocity * .4 + speed * .6;
    drag.time = event.timeStamp;
    drag.y = event.clientY;
    if (!frame) frame = requestAnimationFrame(draw);
  }, {passive: true});
  function release(event: PointerEvent) {
    if (!drag || drag.pointer !== event.pointerId) return;
    if (drag.anchor === undefined) { clearDrag(); return; }
    if (event.type === "pointerup") drag.y = event.clientY;
    const distance = drag.y - drag.start;
    const speed = event.timeStamp - drag.time < 90 ? drag.velocity : 0;
    const threshold = Math.max(65, Math.min(115, drag.height * .24));
    const projected = distance + Math.max(-55, Math.min(75, speed * 105));
    const dismiss = event.type === "pointerup" && !busy() && distance >= 26 && projected >= threshold;
    cancelAnimationFrame(frame);
    draw();
    settle(dismiss, Math.max(0, speed));
  }
  window.addEventListener("pointerup", release);
  window.addEventListener("pointercancel", release);
  sheet.addEventListener("lostpointercapture", event => {
    if (event.target === drag?.header) release(event);
  });
  window.addEventListener("blur", () => {
    if (drag?.anchor !== undefined) settle(false);
    else clearDrag();
  });
  sheet.addEventListener("click", event => {
    if (!suppressClick || event.detail === 0) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);
  sheet.addEventListener("close", reset);

  return {
    reset,
    open() {
      sheet.classList.add("opening");
      sheet.showModal();
    },
    close: () => settle(true),
  };
}
