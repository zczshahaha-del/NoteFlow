const watchFilm = document.querySelector("#watch-film");
const productFilm = document.querySelector("#product-film");
const filmPlay = document.querySelector(".film-play");

function playFilm() {
  productFilm.controls = true;
  filmPlay.hidden = true;
  productFilm.focus({ preventScroll: true });
  productFilm.play().catch(() => {
    filmPlay.hidden = false;
  });
}

watchFilm.addEventListener("click", (event) => {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  playFilm();
});
filmPlay.addEventListener("click", playFilm);
productFilm.controls = false;
filmPlay.hidden = false;
