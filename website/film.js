const films = [...document.querySelectorAll("main video")];
const motionToggle = document.querySelector(".motion-toggle");

if (motionToggle && "IntersectionObserver" in window) {
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const visibleRatios = new Map();
  const startThreshold = .4;
  let motionPreference;
  let activeFilm = null;

  function motionEnabled() {
    return motionPreference ?? !reducedMotion.matches;
  }

  function updatePlayback() {
    const enabled = motionEnabled();
    const action = enabled ? "暂停页面动效" : "播放页面动效";
    motionToggle.toggleAttribute("data-enabled", enabled);
    motionToggle.setAttribute("aria-label", action);
    motionToggle.title = action;

    let nextFilm = null;
    if (enabled && !document.hidden) {
      let highestRatio = 0;
      for (const film of films) {
        const ratio = visibleRatios.get(film) ?? 0;
        if (ratio >= startThreshold && ratio > highestRatio) {
          nextFilm = film;
          highestRatio = ratio;
        }
      }
    }
    if (nextFilm !== activeFilm) {
      activeFilm?.pause();
      activeFilm = nextFilm;
    }

    if (nextFilm?.paused) {
      nextFilm.play().catch(error => {
        if (error.name === "NotAllowedError" && activeFilm === nextFilm) {
          motionPreference = false;
          updatePlayback();
        }
      });
    }
  }

  const observer = new IntersectionObserver(entries => {
    for (const entry of entries) visibleRatios.set(entry.target, entry.intersectionRatio);
    updatePlayback();
  }, { threshold: [0, startThreshold, .55, .7, .85, 1] });

  for (const film of films) {
    film.controls = false;
    observer.observe(film);
  }
  motionToggle.addEventListener("click", () => {
    motionPreference = !motionEnabled();
    updatePlayback();
  });
  document.addEventListener("visibilitychange", updatePlayback);
  reducedMotion.addEventListener("change", updatePlayback);
  updatePlayback();
  motionToggle.hidden = false;
}
