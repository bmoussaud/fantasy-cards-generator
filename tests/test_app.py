import re
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, base_url="https://testserver")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


def test_healthz() -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "dependencies": {
            "cosmos": {
                "status": "not_applicable",
                "durationMs": 0,
                "errorCategory": "none",
            },
            "blob": {
                "status": "not_applicable",
                "durationMs": 0,
                "errorCategory": "none",
            },
            "agent": {
                "status": "ok",
                "durationMs": 0,
                "errorCategory": "none",
            },
        },
    }
    assert response.headers["cache-control"] == "no-store"
    assert REQUEST_ID_PATTERN.fullmatch(response.headers["x-request-id"])


def test_valid_request_id_is_echoed() -> None:
    request_id = "client-request_42.trace"

    response = client.get("/healthz", headers={"X-Request-ID": request_id})

    assert response.status_code == 200
    assert response.headers["x-request-id"] == request_id


@pytest.mark.parametrize(
    "request_id",
    [
        "contains spaces",
        "contains/slash",
        "contains?query=secret",
        "x" * 65,
    ],
)
def test_unsafe_request_id_is_replaced(request_id: str) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": request_id})

    replacement = response.headers["x-request-id"]
    assert response.status_code == 200
    assert replacement != request_id
    assert REQUEST_ID_PATTERN.fullmatch(replacement)


def test_home_renders_hero_and_preserves_auth_copy() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "Sign in with your Microsoft work or school account to generate cards." in response.text
    assert "Check HTMX wiring" not in response.text
    assert 'hx-get="/partials/ping"' not in response.text
    assert 'href="/static/css/app.css"' in response.text
    assert 'src="/static/js/app.js"' in response.text


def test_ping_partial_still_serves_htmx_check() -> None:
    response = client.get("/partials/ping")

    assert response.status_code == 200
    assert "HTMX is wired." in response.text


def test_generator_browser_validation_and_error_swapping() -> None:
    script = client.get("/static/js/app.js").text
    harness = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const handlers = {};
const promptHandlers = {};
const prompt = {
  value: "", minLength: 12, maxLength: 400,
  setCustomValidity(message) { this.validationMessage = message; },
  addEventListener(name, handler) { promptHandlers[name] = handler; }
};
const form = {
  dataset: {},
  querySelector(selector) { return selector === '[name="prompt"]' ? prompt : null; }
};
const document = {
  querySelector(selector) {
    return selector === "[data-card-generator-form]" ? form : null;
  },
  addEventListener() {},
  body: { addEventListener(name, handler) { handlers[name] = handler; } }
};
vm.runInNewContext(fs.readFileSync(0, "utf8"), { document });
assert.equal(typeof promptHandlers.input, "function");
for (const value of ["A wizard", "a          b", " ".repeat(20), "x".repeat(401)]) {
  prompt.value = value;
  promptHandlers.input();
  assert.ok(prompt.validationMessage);
}
for (const value of ["A moonlit guardian", "x".repeat(12), "x".repeat(400)]) {
  prompt.value = value;
  promptHandlers.input();
  assert.equal(prompt.validationMessage, "");
}
assert.equal(typeof handlers["htmx:beforeSwap"], "function");
for (const status of [401, 403, 422, 429, 502, 503, 504]) {
  const detail = {
    target: { id: "generation-result" },
    shouldSwap: false, isError: true,
    xhr: {
      status,
      getResponseHeader(name) {
        return name === "X-Generation-Error" ? "validation_error" : "text/html; charset=utf-8";
      }
    }
  };
  handlers["htmx:beforeSwap"]({ detail });
  assert.equal(detail.shouldSwap, true);
  assert.equal(detail.isError, true);
  assert.equal(detail.xhr.status, status);
}
for (const [target, status, marker, contentType, initialSwap] of [
  ["other", 422, "validation_error", "text/html", false],
  ["generation-result", 500, null, "text/html", false],
  ["generation-result", 422, "validation_error", "application/json", false],
  ["generation-result", 200, null, "text/html", true],
  ["generation-result", 302, null, "text/html", false]
]) {
  const detail = {
    target: { id: target }, shouldSwap: initialSwap, isError: status >= 400,
    xhr: {
      status,
      getResponseHeader(name) { return name === "X-Generation-Error" ? marker : contentType; }
    }
  };
  handlers["htmx:beforeSwap"]({ detail });
  assert.equal(detail.shouldSwap, initialSwap);
}
"""
    result = subprocess.run(
        ["node", "-e", harness],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_static_stylesheet_is_mounted_and_served() -> None:
    response = client.get("/static/css/app.css")

    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]
    assert "--color-accent" in response.text


def test_photo_picker_and_library_upload_browser_flows() -> None:
    script = client.get("/static/js/app.js").text
    harness = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const source = fs.readFileSync(0, "utf8");
class Element {
  constructor() {
    this.dataset = {}; this.children = []; this.handlers = {}; this.elements = {};
    this.attributes = {}; this.textContent = ""; this.value = ""; this.files = [];
    const classes = new Set();
    this.classList = {
      add(name) { classes.add(name); }, remove(name) { classes.delete(name); },
      toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); }
    };
  }
  querySelector(selector) { return this.elements[selector] || null; }
  querySelectorAll() { return this.grid ? this.grid.children : []; }
  appendChild(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  setCustomValidity(value) { this.validationMessage = value; }
}
function text(element) {
  return element.textContent + element.children.map(text).join(" ");
}
function child(parent, selector) {
  const element = new Element(); parent.elements[selector] = element; return element;
}
const photo = {
  photoId: "saved-photo", label: "Portrait", createdAt: "2026-09-16T10:00:00Z",
  image: { url: "/my/photos/saved-photo/image", sizeBytes: 100 },
  thumbnail: { url: "/my/photos/saved-photo/thumbnail" }
};
function startPage(rootSelector, root, state) {
  const handlers = {};
  const document = {
    querySelector(selector) { return selector === rootSelector ? root : null; },
    createElement() { return new Element(); },
    addEventListener() {}, body: new Element()
  };
  class FormData {
    constructor(form) {
      assert.notEqual(state.fields.disabled, true);
      this.photo = state.input.files[0];
      this.label = state.label;
    }
  }
  vm.runInNewContext(source, {
    document, FormData,
    window: { addEventListener(name, handler) { handlers[name] = handler; } },
    URL: {
      createObjectURL() { return "blob:preview"; },
      revokeObjectURL(url) { state.revoked.push(url); }
    },
    fetch(url, options) {
      state.requests.push({ url, options });
      if (options.method === "POST") {
        if (state.failure === "network") return Promise.reject(new Error("offline"));
        if (state.failure) return Promise.resolve({
          ok: false, status: 422,
          text: async () => JSON.stringify({
            title: "Saved Photo Rejected", detail: "Safety check failed."
          })
        });
        state.photos = [photo];
      }
      return Promise.resolve({
        ok: true, status: options.method === "POST" ? 201 : 200,
        text: async () => JSON.stringify(
          options.method === "POST" ? photo : { photos: state.photos }
        )
      });
    }
  });
  return handlers;
}
const flush = () => new Promise(setImmediate);
(async () => {
  const generator = new Element();
  const picker = child(generator, "[data-saved-photo-picker]");
  picker.dataset.photoLibraryEndpoint = "/my/photos";
  const grid = child(generator, "[data-saved-photo-picker-grid]"); picker.grid = grid;
  child(generator, "[data-saved-photo-picker-feedback]");
  const clear = child(generator, "[data-clear-saved-photo]");
  const savedId = child(generator, "[data-saved-photo-id-input]");
  const pickerState = { photos: [photo], requests: [] };
  startPage("[data-card-generator-form]", generator, pickerState);
  await flush();
  assert.equal(generator.dataset.photoReferenceBound, "true");
  assert.equal(grid.children.length, 1);
  assert.equal(savedId.value, photo.photoId);
  assert.equal(grid.children[0].attributes["aria-pressed"], "true");
  assert.match(
    generator.elements["[data-saved-photo-picker-feedback]"].textContent,
    /default saved reference photo/
  );
  clear.handlers.click();
  assert.equal(savedId.value, "");
  assert.equal(clear.hidden, true);
  grid.children[0].handlers.click();
  assert.equal(savedId.value, photo.photoId);
  assert.ok(pickerState.requests.every(request => !request.options.method));

  const manager = new Element();
  manager.dataset.photoLibraryEndpoint = "/my/photos";
  manager.dataset.photoLibraryCsrfToken = "test-csrf";
  const libraryGrid = child(manager, "[data-photo-library-grid]");
  const errorRegion = child(manager, "[data-photo-library-error]");
  const form = child(manager, "[data-photo-upload-form]");
  const maxBytes = 4 * 1024 * 1024;
  form.dataset.maxPhotoBytes = String(maxBytes);
  const input = child(form, "[data-photo-input]");
  const fields = child(form, "[data-photo-upload-fields]");
  const preview = child(form, "[data-photo-preview]");
  child(form, "[data-photo-preview-image]");
  child(form, "[data-photo-preview-meta]");
  const feedback = child(form, "[data-photo-feedback]");
  form.reportValidity = () => !input.validationMessage;
  form.reset = () => { input.files = []; };
  const state = { photos: [], requests: [], revoked: [], input, fields, label: "Portrait" };
  const handlers = startPage("[data-photo-library-manager]", manager, state);
  await flush();
  assert.match(text(libraryGrid), /Upload a photo using the form above/);
  const submit = () => form.handlers.submit({ preventDefault() {} });
  for (const file of [
    { name: "bad.gif", type: "image/gif", size: 10 },
    { name: "large.png", type: "image/png", size: maxBytes + 1 },
    { name: "empty.png", type: "image/png", size: 0 }
  ]) {
    input.files = [file]; input.handlers.change(); submit(); await flush();
    assert.equal(feedback.dataset.invalid, "true");
    assert.equal(state.requests.filter(request => request.options.method === "POST").length, 0);
  }
  input.files = [{ name: "portrait.png", type: "image/png", size: maxBytes }];
  input.handlers.change();
  assert.equal(preview.hidden, false);
  submit(); submit();
  assert.equal(fields.disabled, true);
  await flush();
  const posts = state.requests.filter(request => request.options.method === "POST");
  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, "/my/photos");
  assert.equal(posts[0].options.credentials, "same-origin");
  assert.equal(posts[0].options.headers["X-CSRF-Token"], "test-csrf");
  assert.equal(posts[0].options.headers["Content-Type"], undefined);
  assert.equal(posts[0].options.body.photo.name, "portrait.png");
  assert.equal(fields.disabled, false);
  assert.equal(input.files.length, 0);
  assert.equal(preview.hidden, true);
  assert.equal(libraryGrid.children.length, 1);
  assert.match(feedback.textContent, /Photo saved to My Photos/);
  assert.ok(state.revoked.length);
  for (const failure of ["rejected", "network"]) {
    state.failure = failure;
    input.files = [{ name: "retry.png", type: "image/png", size: 100 }];
    input.handlers.change(); submit(); await flush();
    assert.equal(fields.disabled, false);
    assert.equal(input.files.length, 1);
    assert.equal(errorRegion.hidden, false);
    assert.match(feedback.textContent, /Photo was not saved/);
    assert.match(
      text(errorRegion), failure === "network" ? /Photo Upload Failed/ : /Safety check failed/
    );
  }
  handlers.pagehide();
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        ["node", "-e", harness], input=script, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_photo_picker_default_selection_rules() -> None:
    script = client.get("/static/js/app.js").text
    harness = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const source = fs.readFileSync(0, "utf8");

class Element {
  constructor() {
    this.dataset = {}; this.children = []; this.handlers = {}; this.elements = {};
    this.attributes = {}; this.textContent = ""; this.value = "";
    const classes = new Set();
    this.classList = {
      add(name) { classes.add(name); }, remove(name) { classes.delete(name); },
      toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); }
    };
  }
  querySelector(selector) { return this.elements[selector] || null; }
  querySelectorAll() { return this.grid ? this.grid.children : []; }
  appendChild(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  setAttribute(name, value) { this.attributes[name] = value; }
}

function child(parent, selector) {
  const element = new Element();
  parent.elements[selector] = element;
  return element;
}

function mountGenerator(photos) {
  const root = new Element();
  const picker = child(root, "[data-saved-photo-picker]");
  picker.dataset.photoLibraryEndpoint = "/my/photos";
  const grid = child(root, "[data-saved-photo-picker-grid]");
  picker.grid = grid;
  const feedback = child(root, "[data-saved-photo-picker-feedback]");
  const clear = child(root, "[data-clear-saved-photo]");
  clear.hidden = true;
  const savedId = child(root, "[data-saved-photo-id-input]");
  const requests = [];
  vm.runInNewContext(source, {
    document: {
      querySelector(selector) {
        return selector === "[data-card-generator-form]" ? root : null;
      },
      createElement() { return new Element(); },
      addEventListener() {},
      body: new Element(),
    },
    window: { addEventListener() {}, matchMedia() { return { matches: false }; } },
    fetch(url, options) {
      requests.push({ url, options });
      return Promise.resolve({
        ok: true,
        text: async () => JSON.stringify({ photos }),
      });
    },
  });
  return { root, grid, feedback, clear, savedId, requests };
}

const flush = () => new Promise(setImmediate);
const importedSource = "entra-profile-photo";
const photo = (photoId, createdAt, source = null) => ({
  photoId,
  label: photoId,
  source,
  createdAt,
  image: { url: `/my/photos/${photoId}/image`, sizeBytes: 100 },
  thumbnail: { url: `/my/photos/${photoId}/thumbnail` },
});

(async () => {
  const zero = mountGenerator([]);
  await flush();
  assert.equal(zero.savedId.value, "");
  assert.equal(zero.clear.hidden, true);
  assert.equal(zero.grid.children.length, 1);

  const oneEligible = mountGenerator([photo("eligible-1", "2026-09-16T10:00:00Z")]);
  await flush();
  assert.equal(oneEligible.savedId.value, "eligible-1");
  assert.equal(oneEligible.grid.children[0].attributes["aria-pressed"], "true");
  assert.equal(oneEligible.clear.hidden, false);
  assert.match(oneEligible.feedback.textContent, /default saved reference photo/);

  const manyEligible = mountGenerator([
    photo("newest", "2026-09-18T10:00:00Z"),
    photo("older", "2026-09-17T10:00:00Z"),
  ]);
  await flush();
  assert.equal(manyEligible.savedId.value, "newest");

  const allImported = mountGenerator([
    photo("ms-1", "2026-09-18T10:00:00Z", importedSource),
    photo("ms-2", "2026-09-17T10:00:00Z", importedSource),
  ]);
  await flush();
  assert.equal(allImported.savedId.value, "ms-1");
  assert.equal(allImported.clear.hidden, false);
  assert.equal(allImported.grid.children[0].attributes["aria-pressed"], "true");
  assert.equal(allImported.grid.children[1].attributes["aria-pressed"], "false");
  assert.match(allImported.feedback.textContent, /default saved reference photo/);

  const mixed = mountGenerator([
    photo("ms-newest", "2026-09-18T10:00:00Z", importedSource),
    photo("eligible-next", "2026-09-17T10:00:00Z"),
  ]);
  await flush();
  assert.equal(mixed.savedId.value, "ms-newest");
  assert.equal(mixed.grid.children[0].attributes["aria-pressed"], "true");
  assert.equal(mixed.grid.children[1].attributes["aria-pressed"], "false");
  mixed.grid.children[1].handlers.click();
  assert.equal(mixed.savedId.value, "eligible-next");
  assert.equal(mixed.grid.children[0].attributes["aria-pressed"], "false");
  assert.equal(mixed.grid.children[1].attributes["aria-pressed"], "true");

  oneEligible.clear.handlers.click();
  assert.equal(oneEligible.savedId.value, "");
  assert.equal(oneEligible.clear.hidden, true);
  const reloaded = mountGenerator([photo("eligible-1", "2026-09-16T10:00:00Z")]);
  await flush();
  assert.equal(reloaded.savedId.value, "eligible-1");
  assert.equal(reloaded.clear.hidden, false);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        ["node", "-e", harness], input=script, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_card_preview_styles_preserve_generated_image_aspect_ratio() -> None:
    response = client.get("/static/css/app.css")

    assert response.status_code == 200
    placeholder_rule = re.search(r"\.card-portrait\.is-broken\s*\{([^}]*)\}", response.text)
    artwork_rule = re.search(r"\.card-portrait img\s*\{([^}]*)\}", response.text)

    assert placeholder_rule is not None
    assert re.search(r"aspect-ratio\s*:\s*4\s*/\s*3\s*;", placeholder_rule.group(1))
    assert artwork_rule is not None
    assert re.search(r"width\s*:\s*100%\s*;", artwork_rule.group(1))
    assert re.search(r"height\s*:\s*auto\s*;", artwork_rule.group(1))
    assert not re.search(r"object-fit\s*:\s*cover\s*;", artwork_rule.group(1))
