const films = [...document.querySelectorAll(".film-screen video")];

for (const film of films) {
  const playButton = film.parentElement.querySelector(".film-play");

  playButton.addEventListener("click", () => {
    film.controls = true;
    playButton.hidden = true;
    film.focus({ preventScroll: true });
    film.play().catch(() => {
      playButton.hidden = false;
    });
  });

  film.addEventListener("play", () => {
    for (const otherFilm of films) {
      if (otherFilm !== film && !otherFilm.paused) otherFilm.pause();
    }
  });

  film.controls = false;
  playButton.hidden = false;
}
