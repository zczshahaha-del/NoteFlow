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

filmPlay.addEventListener("click", playFilm);
productFilm.controls = false;
filmPlay.hidden = false;
