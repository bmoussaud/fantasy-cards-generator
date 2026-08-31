/*
 * Fantasy Cards Generator — minimal progressive-enhancement script.
 * Kept intentionally small: no framework, no build step. HTMX remains the
 * primary interaction layer; this file only adds two small affordances.
 */
(function () {
  "use strict";

  /**
   * Runtime artwork failures (broken URL, network error) are not something
   * the server can detect ahead of time. When an <img data-card-artwork>
   * fails to load, flag its portrait frame so the CSS "missing artwork"
   * placeholder state takes over.
   */
  document.addEventListener(
    "error",
    function (event) {
      var target = event.target;
      if (!target || !target.matches || !target.matches("[data-card-artwork]")) {
        return;
      }
      var frame = target.closest("[data-artwork-frame]");
      if (frame) {
        frame.classList.add("is-broken");
      }
    },
    true
  );

  /**
   * After HTMX swaps a new card/error into the result region, bring it into
   * view. Respects prefers-reduced-motion.
   */
  document.body.addEventListener("htmx:afterSwap", function (event) {
    var target = event.target;
    if (!target || target.id !== "generation-result" || !target.firstElementChild) {
      return;
    }
    var prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    target.scrollIntoView({
      behavior: prefersReducedMotion ? "auto" : "smooth",
      block: "start",
    });
  });
})();
