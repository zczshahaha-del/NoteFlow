const journey = document.querySelector("[data-journey]");
const player = journey.querySelector(".journey-player");
const film = player.querySelector("video");
const steps = [...journey.querySelectorAll(".journey-steps li")];
const links = [...player.querySelectorAll(".journey-nav a")];
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
let frameRequest = 0;
let activeStep = 0;
let mediaFailed = false;
let loadingFilm = false;

function updateFrame() {
  frameRequest = 0;
  if (player.hidden) return;

  const pinTop = Number.parseFloat(getComputedStyle(player).top);
  const tops = steps.map(step => step.getBoundingClientRect().top - pinTop);
  const nextStep = tops.findIndex(top => top > 1);
  const index = nextStep === -1 ? steps.length - 1 : Math.max(0, nextStep - 1);
  if (index !== activeStep) {
    links[activeStep].removeAttribute("aria-current");
    links[index].setAttribute("aria-current", "step");
    activeStep = index;
  }

  if (!Number.isFinite(film.duration) || film.seeking) return;

  const next = steps[index + 1];
  const startTime = Number(steps[index].dataset.time);
  const endTime = next ? Number(next.dataset.time) : film.duration - 0.05;
  const stickyEnd = journey.getBoundingClientRect().bottom - player.offsetHeight - pinTop;
  const endTop = next ? tops[index + 1] : tops[index] + (stickyEnd - tops[index]) * 0.75;
  const distance = Math.max(1, endTop - tops[index]);
  const fraction = Math.min(1, Math.max(0, -tops[index] / distance));
  const time = startTime + (endTime - startTime) * fraction;
  if (Math.abs(film.currentTime - time) > 0.025) film.currentTime = time;
}

function requestFrame() {
  if (!frameRequest && !player.hidden) frameRequest = requestAnimationFrame(updateFrame);
}

function updateMode() {
  const enable = !reducedMotion.matches && !mediaFailed
    && Boolean(film.canPlayType("video/mp4")) && CSS.supports("height", "1svh");
  if (enable === !player.hidden) return;

  const bounds = journey.getBoundingClientRect();
  const current = bounds.top <= 0 && bounds.bottom > 0 ? steps[activeStep] : null;
  player.hidden = !enable;
  journey.toggleAttribute("data-scrubbing", enable);
  if (enable) prepareFilm();
  const linked = document.getElementById(location.hash.slice(1));
  (current ?? linked)?.scrollIntoView({ block: "start", behavior: "instant" });
  requestFrame();
}

function showFallback() {
  mediaFailed = true;
  updateMode();
}

async function prepareFilm() {
  if (loadingFilm) return;
  loadingFilm = true;

  try {
    const response = await fetch(film.dataset.src);
    if (!response.ok) {
      showFallback();
      return;
    }
    // The static host can send a whole response to a byte-range request.
    // A local blob lets the browser seek after the complete file arrives.
    const footage = await response.blob();
    film.src = URL.createObjectURL(footage.slice(0, footage.size, "video/mp4"));
  } catch {
    showFallback();
  }
}

film.addEventListener("loadedmetadata", requestFrame);
film.addEventListener("seeked", requestFrame);
film.addEventListener("error", showFallback);
window.addEventListener("scroll", requestFrame, { passive: true });
window.addEventListener("resize", requestFrame);
window.addEventListener("pageshow", requestFrame);
reducedMotion.addEventListener("change", updateMode);
updateMode();
