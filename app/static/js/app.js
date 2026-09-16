/*
 * Fantasy Cards Generator — minimal progressive-enhancement script.
 * Kept intentionally small: no framework, no build step. HTMX remains the
 * primary interaction layer; this file only adds two small affordances.
 */
(function () {
  "use strict";
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

  function bindPromptValidation() {
    var form = document.querySelector("[data-card-generator-form]");
    var prompt = form && form.querySelector('[name="prompt"]');
    if (!prompt) {
      return;
    }
    function validatePrompt() {
      var trimmed = prompt.value.trim();
      var normalized = trimmed.replace(/\s+/g, " ");
      var invalid =
        Array.from(normalized).length < prompt.minLength ||
        Array.from(trimmed).length > prompt.maxLength;
      prompt.setCustomValidity(
        invalid ? "Use 12 to 400 characters; repeated whitespace counts as one space." : ""
      );
    }
    prompt.addEventListener("input", validatePrompt);
    prompt.addEventListener("htmx:validation:validate", validatePrompt);
    validatePrompt();
  }

  function bindPhotoReferenceForm() {
    var form = document.querySelector("[data-card-generator-form]");
    if (!form || form.dataset.photoReferenceBound === "true") {
      return;
    }

    var picker = form.querySelector("[data-saved-photo-picker]");
    var pickerGrid = form.querySelector("[data-saved-photo-picker-grid]");
    var pickerFeedback = form.querySelector("[data-saved-photo-picker-feedback]");
    var clearSavedPhotoButton = form.querySelector("[data-clear-saved-photo]");
    var savedPhotoInput = form.querySelector("[data-saved-photo-id-input]");
    var selectedSavedPhotoId = "";

    if (
      !picker ||
      !pickerGrid ||
      !pickerFeedback ||
      !clearSavedPhotoButton ||
      !savedPhotoInput
    ) {
      return;
    }

    function syncSavedPhotoInput() {
      savedPhotoInput.value = selectedSavedPhotoId;
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
      pickerFeedback.textContent = "Using " + nextLabel + " as your saved reference photo.";
      clearSavedPhotoButton.hidden = false;
    }

    function renderPickerPhotos(photos) {
      pickerFeedback.dataset.loading = "";
      pickerGrid.replaceChildren();
      if (!photos.length) {
        pickerGrid.appendChild(
          createEmptyState(
            "No saved photos yet",
            "Open My Photos to upload a photo, or generate a card without a reference photo."
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
      pickerFeedback.textContent = "Select one saved photo, or generate without a reference photo.";
    }

    clearSavedPhotoButton.addEventListener("click", clearSavedPhotoSelection);

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
            "Upload a photo using the form above, then select it in the generator."
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

    var uploadForm = manager.querySelector("[data-photo-upload-form]");
    if (uploadForm) {
      var input = uploadForm.querySelector("[data-photo-input]");
      var fields = uploadForm.querySelector("[data-photo-upload-fields]");
      var preview = uploadForm.querySelector("[data-photo-preview]");
      var image = uploadForm.querySelector("[data-photo-preview-image]");
      var meta = uploadForm.querySelector("[data-photo-preview-meta]");
      var feedback = uploadForm.querySelector("[data-photo-feedback]");
      var maxBytes = Number(uploadForm.dataset.maxPhotoBytes);
      var objectUrl = null;
      var uploading = false;

      function clearPreview() {
        if (objectUrl) {
          URL.revokeObjectURL(objectUrl);
          objectUrl = null;
        }
        preview.hidden = true;
        image.removeAttribute("src");
        meta.textContent = "";
      }

      function uploadValidationError(file) {
        if (!file) {
          return "Choose a photo to save.";
        }
        if (ALLOWED_PHOTO_TYPES.indexOf(file.type) === -1) {
          return "Choose a JPG, PNG, or WebP image.";
        }
        if (file.size === 0 || file.size > maxBytes) {
          return "Choose a non-empty image no larger than " + formatPhotoSize(maxBytes) + ".";
        }
        return "";
      }

      function setUploadFeedback(message, isError) {
        feedback.textContent = message;
        feedback.dataset.invalid = isError ? "true" : "false";
        input.setAttribute("aria-invalid", isError ? "true" : "false");
      }

      input.addEventListener("change", function () {
        clearPreview();
        var file = input.files && input.files[0];
        var error = file ? uploadValidationError(file) : "";
        input.setCustomValidity(error);
        setUploadFeedback(error, Boolean(error));
        if (!file || error) {
          return;
        }
        objectUrl = URL.createObjectURL(file);
        image.src = objectUrl;
        image.alt = "Preview of " + file.name;
        meta.textContent = file.name + " \u00b7 " + formatPhotoSize(file.size);
        preview.hidden = false;
        setUploadFeedback("Ready to save to My Photos.", false);
      });

      uploadForm.addEventListener("submit", function (event) {
        event.preventDefault();
        if (uploading) {
          return;
        }
        var error = uploadValidationError(input.files && input.files[0]);
        input.setCustomValidity(error);
        setUploadFeedback(error, Boolean(error));
        if (!uploadForm.reportValidity()) {
          return;
        }
        var body = new FormData(uploadForm);
        clearError();
        uploading = true;
        fields.disabled = true;
        uploadForm.setAttribute("aria-busy", "true");
        setUploadFeedback("Checking and saving your photo\u2026", false);
        fetch(endpoint, {
          method: "POST",
          credentials: "same-origin",
          headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
          body: body,
        })
          .then(readJsonOrProblem)
          .then(function () {
            uploadForm.reset();
            input.setCustomValidity("");
            clearPreview();
            setUploadFeedback(
              "Photo saved to My Photos. You can now select it in the generator.",
              false
            );
            return loadLibrary();
          })
          .catch(function (error) {
            setUploadFeedback("Photo was not saved. Review the error and try again.", true);
            showError(
              (error.payload && error.payload.title) || "Photo Upload Failed",
              (error.payload && error.payload.detail) || "Your photo could not be saved. Please try again."
            );
          })
          .finally(function () {
            uploading = false;
            fields.disabled = false;
            uploadForm.setAttribute("aria-busy", "false");
          });
      });
      window.addEventListener("pagehide", clearPreview);
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

  function bindCardSelectionManager() {
    var form = document.querySelector("[data-card-selection]");
    if (!form || form.dataset.cardSelectionBound === "true") {
      return;
    }

    var toggle = form.querySelector("[data-card-selection-toggle]");
    var controls = form.querySelector("[data-card-selection-controls]");
    var selectAll = form.querySelector("[data-card-selection-all]");
    var countRegion = form.querySelector("[data-card-selection-count]");
    var deleteButton = form.querySelector("[data-card-selection-delete]");
    var items = form.querySelectorAll("[data-card-selection-item]");
    var checkboxes = form.querySelectorAll("[data-card-selection-checkbox]");

    if (!toggle || !controls || !selectAll || !countRegion || !deleteButton) {
      return;
    }

    function countSelected() {
      var selected = 0;
      checkboxes.forEach(function (checkbox) {
        if (checkbox.checked) {
          selected += 1;
        }
      });
      return selected;
    }

    function syncState() {
      var selected = countSelected();
      var noun = selected === 1 ? "card" : "cards";
      countRegion.textContent = selected + " " + noun + " selected";
      deleteButton.disabled = selected === 0;
      selectAll.checked = checkboxes.length > 0 && selected === checkboxes.length;
      selectAll.indeterminate = selected > 0 && selected < checkboxes.length;
      form.dataset.confirmMessage =
        "This permanently deletes " +
        selected +
        " selected " +
        noun +
        ". Artwork cleanup will continue in the background.";
    }

    function setSelectionMode(active) {
      toggle.setAttribute("aria-pressed", active ? "true" : "false");
      toggle.textContent = active ? "Cancel selection" : "Select cards";
      controls.hidden = !active;
      items.forEach(function (item) {
        item.hidden = !active;
      });
      if (!active) {
        checkboxes.forEach(function (checkbox) {
          checkbox.checked = false;
        });
      }
      syncState();
    }

    toggle.hidden = false;
    setSelectionMode(false);

    toggle.addEventListener("click", function () {
      setSelectionMode(toggle.getAttribute("aria-pressed") !== "true");
    });

    selectAll.addEventListener("change", function () {
      checkboxes.forEach(function (checkbox) {
        checkbox.checked = selectAll.checked;
      });
      syncState();
    });

    form.addEventListener("change", function (event) {
      var target = event.target;
      if (!target || !target.matches || !target.matches("[data-card-selection-checkbox]")) {
        return;
      }
      syncState();
    });

    form.addEventListener("submit", function (event) {
      if (countSelected() === 0) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    });

    form.dataset.cardSelectionBound = "true";
  }

  function bindConfirmModalForms() {
    var modal = document.querySelector("[data-confirm-modal]");
    if (!modal || modal.dataset.confirmModalBound === "true") {
      return;
    }

    var dialog = modal.querySelector('[role="dialog"]');
    var titleElement = modal.querySelector("[data-confirm-modal-title]");
    var messageElement = modal.querySelector("[data-confirm-modal-message]");
    var cancelButtons = modal.querySelectorAll("[data-confirm-modal-cancel]");
    var confirmButton = modal.querySelector("[data-confirm-modal-confirm]");
    var activeForm = null;
    var lastFocusedElement = null;

    if (!dialog || !titleElement || !messageElement || !confirmButton || !cancelButtons.length) {
      return;
    }

    function getFocusableElements() {
      return dialog.querySelectorAll(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      );
    }

    function closeModal() {
      modal.hidden = true;
      activeForm = null;
      if (lastFocusedElement && typeof lastFocusedElement.focus === "function") {
        lastFocusedElement.focus();
      }
    }

    function openModal(form, trigger) {
      activeForm = form;
      lastFocusedElement = trigger || document.activeElement;
      titleElement.textContent = form.dataset.confirmTitle || "Confirm action";
      messageElement.textContent = form.dataset.confirmMessage || "Are you sure you want to continue?";
      confirmButton.textContent = form.dataset.confirmConfirmLabel || "Confirm";
      modal.hidden = false;
      confirmButton.focus();
    }

    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (!form || !form.matches || !form.matches("[data-confirm-modal-form]")) {
        return;
      }

      if (form.dataset.confirmModalApproved === "true") {
        delete form.dataset.confirmModalApproved;
        return;
      }

      event.preventDefault();
      openModal(form);
    });

    document.addEventListener("click", function (event) {
      var target = event.target;
      if (!target || !target.closest) {
        return;
      }
      var trigger = target.closest("[data-confirm-modal-form] button[type='submit']");
      if (!trigger || !trigger.form) {
        return;
      }
      lastFocusedElement = trigger;
    });

    cancelButtons.forEach(function (button) {
      button.addEventListener("click", closeModal);
    });

    confirmButton.addEventListener("click", function () {
      if (!activeForm) {
        return;
      }
      activeForm.dataset.confirmModalApproved = "true";
      if (typeof activeForm.requestSubmit === "function") {
        activeForm.requestSubmit();
      } else {
        activeForm.submit();
      }
      modal.hidden = true;
      activeForm = null;
    });

    modal.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeModal();
        return;
      }
      if (event.key !== "Tab") {
        return;
      }
      var focusableElements = getFocusableElements();
      if (!focusableElements.length) {
        event.preventDefault();
        return;
      }
      var first = focusableElements[0];
      var last = focusableElements[focusableElements.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });

    modal.dataset.confirmModalBound = "true";
  }

  bindPromptValidation();
  bindPhotoReferenceForm();
  bindPhotoLibraryManager();
  bindCardSelectionManager();
  bindConfirmModalForms();

  // HTMX does not swap error responses by default. Only render our HTML error
  // partials in the generation region, retaining the HTTP/error semantics.
  document.body.addEventListener("htmx:beforeSwap", function (event) {
    var detail = event.detail;
    if (
      detail &&
      detail.target &&
      detail.target.id === "generation-result" &&
      detail.xhr &&
      detail.xhr.status >= 400 &&
      detail.xhr.status < 600 &&
      detail.xhr.getResponseHeader("X-Generation-Error") &&
      (detail.xhr.getResponseHeader("Content-Type") || "").startsWith("text/html")
    ) {
      detail.shouldSwap = true;
    }
  });

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
