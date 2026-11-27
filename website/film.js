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

const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

for (const toggle of document.querySelectorAll(".demo-toggle")) {
  const demo = document.getElementById(toggle.getAttribute("aria-controls"));
  const subject = toggle.dataset.demoName ?? "写作实录";
  let demoVisible = false;
  let pausedByViewer = false;

  function showControl() {
    const playing = !demo.paused && !demo.ended;
    const action = playing ? "暂停" : demo.ended ? "重播" : "播放";
    toggle.toggleAttribute("data-playing", playing);
    toggle.setAttribute("aria-label", `${action}${subject}`);
    toggle.title = action;
  }

  function playDemo() {
    demo.play().catch(showControl);
  }

  function updatePlayback() {
    if (!demoVisible || document.hidden || reducedMotion.matches) {
      demo.pause();
    } else if (!pausedByViewer && !demo.ended && demo.paused) {
      playDemo();
    }
  }

  toggle.addEventListener("click", () => {
    pausedByViewer = !demo.paused;
    if (pausedByViewer) demo.pause();
    else playDemo();
    showControl();
  });
  for (const event of ["play", "pause", "ended"]) {
    demo.addEventListener(event, showControl);
  }
  demo.controls = false;
  toggle.hidden = false;

  if ("IntersectionObserver" in window) {
    const visibility = new IntersectionObserver(([entry]) => {
      demoVisible = entry.intersectionRatio >= .55;
      updatePlayback();
    }, { threshold: .55 });
    visibility.observe(demo);
    document.addEventListener("visibilitychange", updatePlayback);
    reducedMotion.addEventListener("change", updatePlayback);
  }
}
