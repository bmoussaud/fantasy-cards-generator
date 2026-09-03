/*
 * Fantasy Cards Generator — minimal progressive-enhancement script.
 * Kept intentionally small: no framework, no build step. HTMX remains the
 * primary interaction layer; this file only adds two small affordances.
 */
(function () {
  "use strict";
  var MAX_PHOTO_BYTES = 5 * 1024 * 1024;
  var ALLOWED_PHOTO_TYPES = ["image/jpeg", "image/png", "image/webp"];

  function formatPhotoSize(bytes) {
    if (bytes < 1024 * 1024) {
      return Math.max(1, Math.round(bytes / 1024)) + " KB";
    }
    return (bytes / (1024 * 1024)).toFixed(1).replace(/\.0$/, "") + " MB";
  }

  function bindPhotoPreview() {
    var input = document.querySelector("[data-photo-input]");
    var preview = document.querySelector("[data-photo-preview]");
    var image = document.querySelector("[data-photo-preview-image]");
    var meta = document.querySelector("[data-photo-preview-meta]");
    var feedback = document.querySelector("[data-photo-feedback]");
    var objectUrl = null;

    if (!input || !preview || !image || !meta || !feedback || input.dataset.photoPreviewBound === "true") {
      return;
    }

    function clearObjectUrl() {
      if (objectUrl) {
        URL.revokeObjectURL(objectUrl);
        objectUrl = null;
      }
    }

    function hidePreview() {
      clearObjectUrl();
      preview.hidden = true;
      image.removeAttribute("src");
      meta.textContent = "";
    }

    function setFeedback(message, isError) {
      feedback.textContent = message;
      feedback.dataset.invalid = isError ? "true" : "false";
      input.setAttribute("aria-invalid", isError ? "true" : "false");
    }

    input.addEventListener("change", function () {
      var file = input.files && input.files[0];
      if (!file) {
        hidePreview();
        setFeedback("", false);
        return;
      }

      if (ALLOWED_PHOTO_TYPES.indexOf(file.type) === -1) {
        hidePreview();
        setFeedback("Choose a JPG, PNG, or WebP image.", true);
        return;
      }

      if (file.size > MAX_PHOTO_BYTES) {
        hidePreview();
        setFeedback("Choose an image that is 5 MB or smaller.", true);
        return;
      }

      clearObjectUrl();
      objectUrl = URL.createObjectURL(file);
      image.src = objectUrl;
      image.alt = "Preview of " + file.name;
      meta.textContent = file.name + " \u00b7 " + formatPhotoSize(file.size);
      preview.hidden = false;
      setFeedback("Selected photo is ready to use as your reference image.", false);
    });

    window.addEventListener("pagehide", clearObjectUrl);
    input.dataset.photoPreviewBound = "true";
  }

  bindPhotoPreview();

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
