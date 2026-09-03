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

  function formatUtcTimestamp(timestamp) {
    if (!timestamp) {
      return "";
    }
    var parsed = new Date(timestamp);
    if (Number.isNaN(parsed.getTime())) {
      return timestamp;
    }
    return (
      new Intl.DateTimeFormat("en-US", {
        month: "short",
        day: "numeric",
        year: "numeric",
        hour: "numeric",
        minute: "2-digit",
        timeZone: "UTC",
      }).format(parsed) + " UTC"
    );
  }

  function createEmptyState(title, detail) {
    var wrapper = document.createElement("div");
    wrapper.className = "empty-state";
    wrapper.innerHTML =
      '<svg class="empty-state__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M4 5.5h16v13H4z"></path><path d="M8 9.5h8M8 13h5" stroke-linecap="round"></path></svg>';
    var heading = document.createElement("h2");
    heading.textContent = title;
    var paragraph = document.createElement("p");
    paragraph.textContent = detail;
    wrapper.appendChild(heading);
    wrapper.appendChild(paragraph);
    return wrapper;
  }

  function createProblemPanel(title, detail) {
    var panel = document.createElement("div");
    panel.className = "error-panel";
    panel.setAttribute("role", "alert");
    panel.innerHTML =
      '<svg class="error-panel__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M12 3l9.5 17H2.5z" stroke-linejoin="round"></path><path d="M12 9.5v4.5M12 17h.01" stroke-linecap="round"></path></svg>';
    var body = document.createElement("div");
    body.className = "error-panel__body";
    var heading = document.createElement("h2");
    heading.textContent = title;
    var paragraph = document.createElement("p");
    paragraph.textContent = detail;
    body.appendChild(heading);
    body.appendChild(paragraph);
    panel.appendChild(body);
    return panel;
  }

  function readJsonOrProblem(response) {
    return response.text().then(function (text) {
      var payload = {};
      if (text) {
        try {
          payload = JSON.parse(text);
        } catch (_error) {
          payload = { detail: text };
        }
      }
      if (response.ok) {
        return payload;
      }
      var error = new Error(payload.detail || payload.title || "Request failed.");
      error.payload = payload;
      error.status = response.status;
      throw error;
    });
  }

  function fetchSavedPhotos(endpoint) {
    return fetch(endpoint, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    }).then(readJsonOrProblem);
  }

  function createSavedPhotoOption(photo) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "saved-photo-option";
    button.dataset.photoId = photo.photoId;
    button.dataset.photoLabel = photo.label || "";
    button.setAttribute("aria-pressed", "false");

    var image = document.createElement("img");
    image.className = "saved-photo-option__image";
    image.src = photo.thumbnail.url;
    image.alt = photo.label ? "Saved photo: " + photo.label : "Saved photo thumbnail";
    image.loading = "lazy";

    var body = document.createElement("span");
    body.className = "saved-photo-option__body";

    var label = document.createElement("span");
    label.className = "saved-photo-option__label";
    label.textContent = photo.label || "Saved photo";

    var meta = document.createElement("span");
    meta.className = "saved-photo-option__meta";
    meta.textContent = "Saved " + formatUtcTimestamp(photo.createdAt);

    body.appendChild(label);
    body.appendChild(meta);
    button.appendChild(image);
    button.appendChild(body);
    return button;
  }

  function createPhotoLibraryCard(photo) {
    var article = document.createElement("article");
    article.className = "photo-library-card";
    article.dataset.photoId = photo.photoId;

    var media = document.createElement("div");
    media.className = "photo-library-card__media";
    var image = document.createElement("img");
    image.src = photo.thumbnail.url;
    image.alt = photo.label ? "Saved photo: " + photo.label : "Saved photo thumbnail";
    image.loading = "lazy";
    media.appendChild(image);

    var body = document.createElement("div");
    body.className = "photo-library-card__body";

    var title = document.createElement("h2");
    title.textContent = photo.label || "Saved photo";

    var meta = document.createElement("p");
    meta.className = "photo-library-card__meta";
    meta.textContent = "Saved ";
    var time = document.createElement("time");
    time.dateTime = photo.createdAt;
    time.textContent = formatUtcTimestamp(photo.createdAt);
    meta.appendChild(time);

    var actions = document.createElement("div");
    actions.className = "photo-library-card__actions";

    var viewLink = document.createElement("a");
    viewLink.className = "btn btn-ghost";
    viewLink.href = photo.image.url;
    viewLink.target = "_blank";
    viewLink.rel = "noreferrer";
    viewLink.textContent = "Open image";

    var deleteButton = document.createElement("button");
    deleteButton.className = "btn btn-secondary";
    deleteButton.type = "button";
    deleteButton.textContent = "Delete";
    deleteButton.dataset.photoDelete = photo.photoId;

    actions.appendChild(viewLink);
    actions.appendChild(deleteButton);
    body.appendChild(title);
    body.appendChild(meta);
    body.appendChild(actions);
    article.appendChild(media);
    article.appendChild(body);
    return article;
  }

  function bindPhotoReferenceForm() {
    var form = document.querySelector("[data-card-generator-form]");
    if (!form || form.dataset.photoReferenceBound === "true") {
      return;
    }

    var input = form.querySelector("[data-photo-input]");
    var preview = form.querySelector("[data-photo-preview]");
    var image = form.querySelector("[data-photo-preview-image]");
    var meta = form.querySelector("[data-photo-preview-meta]");
    var feedback = form.querySelector("[data-photo-feedback]");
    var picker = form.querySelector("[data-saved-photo-picker]");
    var pickerGrid = form.querySelector("[data-saved-photo-picker-grid]");
    var pickerFeedback = form.querySelector("[data-saved-photo-picker-feedback]");
    var clearSavedPhotoButton = form.querySelector("[data-clear-saved-photo]");
    var savedPhotoInput = form.querySelector("[data-saved-photo-id-input]");
    var savePhotoToggle = form.querySelector("[data-save-photo-toggle]");
    var photoLabelField = form.querySelector("[data-photo-label-field]");
    var photoLabelInput = form.querySelector("[data-photo-label-input]");
    var objectUrl = null;
    var selectedSavedPhotoId = "";

    if (
      !input ||
      !preview ||
      !image ||
      !meta ||
      !feedback ||
      !picker ||
      !pickerGrid ||
      !pickerFeedback ||
      !clearSavedPhotoButton ||
      !savedPhotoInput ||
      !savePhotoToggle ||
      !photoLabelField ||
      !photoLabelInput
    ) {
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

    function setUploadFeedback(message, isError) {
      feedback.textContent = message;
      feedback.dataset.invalid = isError ? "true" : "false";
      input.setAttribute("aria-invalid", isError ? "true" : "false");
    }

    function getCurrentUploadValidation() {
      var file = input.files && input.files[0];
      if (!file) {
        return { file: null, valid: false, error: "" };
      }
      if (ALLOWED_PHOTO_TYPES.indexOf(file.type) === -1) {
        return { file: file, valid: false, error: "Choose a JPG, PNG, or WebP image." };
      }
      if (file.size > MAX_PHOTO_BYTES) {
        return { file: file, valid: false, error: "Choose an image that is 5 MB or smaller." };
      }
      return { file: file, valid: true, error: "" };
    }

    function syncSavePhotoControls() {
      var uploadState = getCurrentUploadValidation();
      var disableSave = !uploadState.valid || Boolean(selectedSavedPhotoId);
      savePhotoToggle.disabled = disableSave;
      if (disableSave) {
        savePhotoToggle.checked = false;
      }
      photoLabelField.hidden = !uploadState.valid || !savePhotoToggle.checked || Boolean(selectedSavedPhotoId);
      photoLabelInput.disabled = photoLabelField.hidden;
      if (photoLabelInput.disabled) {
        photoLabelInput.value = "";
      }
    }

    function syncSavedPhotoInput() {
      savedPhotoInput.value = selectedSavedPhotoId;
      savedPhotoInput.disabled = !selectedSavedPhotoId;
    }

    function clearSavedPhotoSelection() {
      selectedSavedPhotoId = "";
      syncSavedPhotoInput();
      clearSavedPhotoButton.hidden = true;
      picker.querySelectorAll(".saved-photo-option").forEach(function (button) {
        button.classList.remove("is-selected");
        button.setAttribute("aria-pressed", "false");
      });
      if (!pickerFeedback.dataset.loading) {
        pickerFeedback.textContent = "";
      }
      syncSavePhotoControls();
    }

    function selectSavedPhoto(button) {
      var nextPhotoId = button.dataset.photoId || "";
      var nextLabel = button.dataset.photoLabel || "Saved photo";
      if (selectedSavedPhotoId === nextPhotoId) {
        clearSavedPhotoSelection();
        return;
      }
      clearSavedPhotoSelection();
      selectedSavedPhotoId = nextPhotoId;
      syncSavedPhotoInput();
      picker.querySelectorAll(".saved-photo-option").forEach(function (item) {
        var isSelected = item === button;
        item.classList.toggle("is-selected", isSelected);
        item.setAttribute("aria-pressed", isSelected ? "true" : "false");
      });
      input.value = "";
      hidePreview();
      setUploadFeedback("Using a saved photo instead of a new upload.", false);
      pickerFeedback.textContent = "Using " + nextLabel + " as your saved reference photo.";
      clearSavedPhotoButton.hidden = false;
      syncSavePhotoControls();
    }

    function renderPickerPhotos(photos) {
      pickerFeedback.dataset.loading = "";
      pickerGrid.replaceChildren();
      if (!photos.length) {
        pickerGrid.appendChild(
          createEmptyState(
            "No saved photos yet",
            "Upload a reference photo and choose to save it if you want to reuse it later."
          )
        );
        pickerFeedback.textContent = "";
        clearSavedPhotoButton.hidden = true;
        return;
      }

      photos.forEach(function (photo) {
        var button = createSavedPhotoOption(photo);
        button.addEventListener("click", function () {
          selectSavedPhoto(button);
        });
        pickerGrid.appendChild(button);
      });
      pickerFeedback.textContent = "Select one saved photo or upload a new one.";
    }

    input.addEventListener("change", function () {
      var uploadState = getCurrentUploadValidation();
      if (!uploadState.file) {
        hidePreview();
        setUploadFeedback("", false);
        syncSavePhotoControls();
        return;
      }

      clearSavedPhotoSelection();
      if (!uploadState.valid) {
        hidePreview();
        setUploadFeedback(uploadState.error, true);
        syncSavePhotoControls();
        return;
      }

      clearObjectUrl();
      objectUrl = URL.createObjectURL(uploadState.file);
      image.src = objectUrl;
      image.alt = "Preview of " + uploadState.file.name;
      meta.textContent = uploadState.file.name + " \u00b7 " + formatPhotoSize(uploadState.file.size);
      preview.hidden = false;
      setUploadFeedback("Selected photo is ready to use as your reference image.", false);
      syncSavePhotoControls();
    });

    savePhotoToggle.addEventListener("change", syncSavePhotoControls);
    clearSavedPhotoButton.addEventListener("click", clearSavedPhotoSelection);
    window.addEventListener("pagehide", clearObjectUrl);

    pickerFeedback.dataset.loading = "true";
    fetchSavedPhotos(picker.dataset.photoLibraryEndpoint)
      .then(function (payload) {
        renderPickerPhotos(payload.photos || []);
      })
      .catch(function () {
        pickerFeedback.dataset.loading = "";
        pickerGrid.replaceChildren(
          createEmptyState(
            "Could not load saved photos",
            "Refresh the page or open My Photos to try again."
          )
        );
        pickerFeedback.textContent = "";
      });

    syncSavePhotoControls();
    syncSavedPhotoInput();
    form.dataset.photoReferenceBound = "true";
  }

  function bindPhotoLibraryManager() {
    var manager = document.querySelector("[data-photo-library-manager]");
    if (!manager || manager.dataset.photoLibraryBound === "true") {
      return;
    }

    var endpoint = manager.dataset.photoLibraryEndpoint;
    var csrfToken = manager.dataset.photoLibraryCsrfToken;
    var grid = manager.querySelector("[data-photo-library-grid]");
    var errorRegion = manager.querySelector("[data-photo-library-error]");

    if (!endpoint || !csrfToken || !grid || !errorRegion) {
      return;
    }

    function clearError() {
      errorRegion.hidden = true;
      errorRegion.replaceChildren();
    }

    function showError(title, detail) {
      errorRegion.hidden = false;
      errorRegion.replaceChildren(createProblemPanel(title, detail));
    }

    function renderLibrary(photos) {
      grid.replaceChildren();
      if (!photos.length) {
        grid.appendChild(
          createEmptyState(
            "No saved photos yet",
            "Generate a card with a new upload and choose to save it to build your library."
          )
        );
        return;
      }
      photos.forEach(function (photo) {
        grid.appendChild(createPhotoLibraryCard(photo));
      });
    }

    function loadLibrary() {
      clearError();
      return fetchSavedPhotos(endpoint)
        .then(function (payload) {
          renderLibrary(payload.photos || []);
        })
        .catch(function (error) {
          renderLibrary([]);
          showError(
            (error.payload && error.payload.title) || "Photo Library Unavailable",
            (error.payload && error.payload.detail) || "We could not load your saved photos."
          );
        });
    }

    manager.addEventListener("click", function (event) {
      var target = event.target;
      if (!target || !target.matches || !target.matches("[data-photo-delete]")) {
        return;
      }

      var photoId = target.dataset.photoDelete;
      if (!photoId) {
        return;
      }

      if (!window.confirm("Delete this saved photo from your library?")) {
        return;
      }

      clearError();
      target.disabled = true;
      fetch(endpoint + "/" + photoId, {
        method: "DELETE",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "X-CSRF-Token": csrfToken,
        },
      })
        .then(readJsonOrProblem)
        .then(function () {
          return loadLibrary();
        })
        .catch(function (error) {
          showError(
            (error.payload && error.payload.title) || "Photo Library Unavailable",
            (error.payload && error.payload.detail) || "The saved photo could not be deleted."
          );
        })
        .finally(function () {
          target.disabled = false;
        });
    });

    loadLibrary();
    manager.dataset.photoLibraryBound = "true";
  }

  bindPhotoReferenceForm();
  bindPhotoLibraryManager();

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
