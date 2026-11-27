const films = [...document.querySelectorAll("main video")];

for (const film of films) {
  film.addEventListener("play", () => {
    for (const otherFilm of films) {
      if (otherFilm !== film && !otherFilm.paused) otherFilm.pause();
    }
  });

  const playButton = film.parentElement.querySelector(".film-play");
  if (!playButton) continue;

  playButton.addEventListener("click", () => {
    film.controls = true;
    playButton.hidden = true;
    film.focus({ preventScroll: true });
    film.play().catch(() => {
      playButton.hidden = false;
    });
  });

  film.controls = false;
  playButton.hidden = false;
}

const demo = document.querySelector("#product-film");
const demoToggle = document.querySelector(".demo-toggle");
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
let demoVisible = false;
let pausedByViewer = false;

function showDemoControl() {
  const playing = !demo.paused && !demo.ended;
  const action = playing ? "暂停" : demo.ended ? "重播" : "播放";
  demoToggle.toggleAttribute("data-playing", playing);
  demoToggle.setAttribute("aria-label", `${action}写作实录`);
  demoToggle.title = action;
}

function playDemo() {
  demo.play().catch(showDemoControl);
}

function updateDemoPlayback() {
  if (!demoVisible || document.hidden || reducedMotion.matches) {
    demo.pause();
  } else if (!pausedByViewer && !demo.ended && demo.paused) {
    playDemo();
  }
}

demoToggle.addEventListener("click", () => {
  pausedByViewer = !demo.paused;
  if (pausedByViewer) demo.pause();
  else playDemo();
  showDemoControl();
});
for (const event of ["play", "pause", "ended"]) {
  demo.addEventListener(event, showDemoControl);
}
demo.controls = false;
demoToggle.hidden = false;

if ("IntersectionObserver" in window) {
  const visibility = new IntersectionObserver(([entry]) => {
    demoVisible = entry.intersectionRatio >= .55;
    updateDemoPlayback();
  }, { threshold: .55 });
  visibility.observe(demo);
  document.addEventListener("visibilitychange", updateDemoPlayback);
  reducedMotion.addEventListener("change", updateDemoPlayback);
}
