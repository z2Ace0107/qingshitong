(() => {
  "use strict";

  const VALID_STATES = new Set([
    "idle",
    "clarifying",
    "retrieving",
    "evidence_ready",
    "warning",
    "awaiting_confirmation",
    "simulated_success",
    "needs_reconfirmation",
    "degraded",
  ]);
  const VALID_RENDERERS = new Set(["svg", "raster", "sprite-sheet", "frame-sequence"]);
  const PREFS_KEY = "qst-mascot-preferences-v1";
  const stateLabels = {
    idle: "待命",
    clarifying: "等待澄清",
    retrieving: "检索依据",
    evidence_ready: "依据已就绪",
    warning: "需要注意",
    awaiting_confirmation: "等待确认",
    simulated_success: "演示完成",
    needs_reconfirmation: "需要重新确认",
    degraded: "安全降级",
  };

  const $ = (selector, root = document) => root.querySelector(selector);

  function sameOriginAsset(source) {
    if (typeof source !== "string" || !source.trim()) return false;
    try {
      const url = new URL(source, window.location.href);
      return url.origin === window.location.origin
        && ["http:", "https:"].includes(url.protocol)
        && !url.username
        && !url.password
        && !url.search
        && !url.hash
        && (url.pathname === "/mascot" || url.pathname.startsWith("/mascot/"));
    } catch {
      return false;
    }
  }

  function assetUrl(source) {
    if (typeof source !== "string" || !source.trim()) return "";
    const normalized = source.startsWith("/") ? source : `/mascot/${source.replace(/^\/+/, "")}`;
    return sameOriginAsset(normalized) ? new URL(normalized, window.location.href).href : "";
  }

  function readPreferences() {
    try {
      const value = JSON.parse(window.localStorage.getItem(PREFS_KEY) || "{}");
      return {
        muted: Boolean(value.muted),
        reducedMotion: Boolean(value.reducedMotion),
        collapsed: Boolean(value.collapsed),
        edge: value.edge === "left" || value.edge === "right" ? value.edge : null,
        position: value.position && Number.isFinite(value.position.left) && Number.isFinite(value.position.top)
          ? { left: value.position.left, top: value.position.top }
          : null,
      };
    } catch {
      return { muted: false, reducedMotion: false, collapsed: false, edge: null, position: null };
    }
  }

  function writePreferences(preferences) {
    try {
      window.localStorage.setItem(PREFS_KEY, JSON.stringify(preferences));
    } catch {
      // Preferences are optional and must never affect the service UI.
    }
  }

  function validEdge(value) {
    return value === "left" || value === "right" ? value : null;
  }

  function faceSize(root) {
    return $("#mascot-toggle", root)?.getBoundingClientRect().width || 48;
  }

  function setRootPosition(root, left, top) {
    root.style.left = `${Math.round(left)}px`;
    root.style.top = `${Math.round(top)}px`;
    root.style.right = "auto";
    root.style.bottom = "auto";
  }

  function clampTop(root, top) {
    const maxTop = Math.max(8, window.innerHeight - root.offsetHeight - 8);
    return Math.max(8, Math.min(maxTop, top));
  }

  function dockToEdge(root, edge, top = root.getBoundingClientRect().top) {
    const side = validEdge(edge) || "right";
    root.dataset.edge = side;
    root.classList.add("is-edge-hidden", "is-collapsed");
    root.style.width = "";
    const size = faceSize(root);
    const left = side === "right" ? window.innerWidth - size / 2 : -size / 2;
    setRootPosition(root, left, clampTop(root, top));
  }

  function revealFromEdge(root, edge, top = root.getBoundingClientRect().top) {
    const side = validEdge(edge) || "right";
    root.dataset.edge = side;
    root.dataset.positioning = "true";
    root.classList.remove("is-edge-hidden", "is-collapsed");
    // Give the expanded flex row room before measuring it; measuring while it is
    // still half outside the viewport causes the note to collapse into a column.
    setRootPosition(root, 12, top);
    window.requestAnimationFrame(() => {
      const left = side === "right" ? window.innerWidth - root.offsetWidth - 12 : 12;
      setRootPosition(root, left, clampTop(root, top));
      window.requestAnimationFrame(() => root.removeAttribute("data-positioning"));
    });
  }

  function frameList(manifest, state) {
    const spec = manifest.states?.[state] || manifest.states?.idle || {};
    if (Array.isArray(spec.frames)) return spec.frames;
    return [];
  }

  class AssetRenderer {
    constructor(host, manifest) {
      this.host = host;
      this.manifest = manifest;
      this.timer = null;
      this.frameIndex = 0;
      this.currentState = "idle";
      this.muted = false;
      this.reducedMotion = false;
    }

    clear() {
      if (this.timer) window.clearInterval(this.timer);
      this.timer = null;
      this.host.replaceChildren();
    }

    fallbackAsset() {
      return this.manifest.fallback?.src || this.manifest.asset?.src || "";
    }

    image(source, alt = "") {
      const src = assetUrl(source);
      if (!src) return null;
      const image = document.createElement("img");
      image.className = "mascot-asset-image";
      image.src = src;
      image.alt = alt;
      image.decoding = "async";
      image.draggable = false;
      image.addEventListener("error", () => {
        if (image.dataset.fallbackApplied === "true") return;
        image.dataset.fallbackApplied = "true";
        const fallback = assetUrl(this.fallbackAsset());
        if (fallback && image.src !== fallback) image.src = fallback;
      });
      return image;
    }

    renderSvg(spec) {
      const image = this.image(spec.src || this.manifest.asset?.src || this.fallbackAsset(), spec.alt || this.manifest.asset?.alt || this.manifest.display_name || "");
      if (image) this.host.append(image);
      else this.renderFallbackText();
    }

    renderSequence(spec) {
      const frames = frameList(this.manifest, this.currentState);
      const sources = frames.map((frame) => typeof frame === "string" ? frame : frame?.src).filter(Boolean);
      if (!sources.length) return this.renderSvg(spec);
      const show = () => {
        const source = sources[this.frameIndex % sources.length];
        const image = this.image(source, spec.alt || this.manifest.display_name || "");
        if (image) this.host.replaceChildren(image);
        this.frameIndex += 1;
      };
      show();
      const canAnimate = !this.reducedMotion && !this.muted && sources.length > 1;
      if (canAnimate) {
        const fps = Math.max(1, Math.min(24, Number(spec.fps) || 6));
        this.timer = window.setInterval(show, 1000 / fps);
      }
    }

    renderSprite(spec) {
      const atlas = this.manifest.assets?.atlas || this.manifest.atlas?.src;
      const atlasUrl = assetUrl(atlas);
      const frames = frameList(this.manifest, this.currentState);
      const definitions = this.manifest.frames || {};
      const resolved = frames.map((frame) => typeof frame === "string" ? definitions[frame] : frame).filter((frame) => frame && Number.isFinite(frame.x) && Number.isFinite(frame.y) && Number.isFinite(frame.width) && Number.isFinite(frame.height));
      if (!atlasUrl || !resolved.length) return this.renderSvg(spec);
      const atlasSize = this.manifest.atlas || {};
      const scale = Math.max(0.25, Math.min(4, Number(this.manifest.canvas?.scale) || 1));
      const show = () => {
        const frame = resolved[this.frameIndex % resolved.length];
        const sprite = document.createElement("span");
        sprite.className = "mascot-sprite-asset";
        sprite.setAttribute("role", "img");
        sprite.setAttribute("aria-label", spec.alt || this.manifest.display_name || "");
        sprite.style.width = `${frame.width * scale}px`;
        sprite.style.height = `${frame.height * scale}px`;
        sprite.style.backgroundImage = `url("${atlasUrl}")`;
        if (atlasSize.width && atlasSize.height) sprite.style.backgroundSize = `${atlasSize.width * scale}px ${atlasSize.height * scale}px`;
        sprite.style.backgroundPosition = `${-frame.x * scale}px ${-frame.y * scale}px`;
        this.host.replaceChildren(sprite);
        this.frameIndex += 1;
      };
      show();
      const canAnimate = !this.reducedMotion && !this.muted && resolved.length > 1;
      if (canAnimate) {
        const fps = Math.max(1, Math.min(24, Number(spec.fps) || 6));
        this.timer = window.setInterval(show, 1000 / fps);
      }
    }

    renderFallbackText() {
      const fallback = document.createElement("span");
      fallback.className = "mascot-fallback-mark";
      fallback.setAttribute("aria-hidden", "true");
      const eyes = document.createElement("span");
      eyes.className = "mascot-fallback-eyes";
      const mouth = document.createElement("span");
      mouth.className = "mascot-fallback-mouth";
      fallback.append(eyes, mouth);
      this.host.append(fallback);
    }

    setState(state, textEquivalent) {
      this.currentState = VALID_STATES.has(state) ? state : "idle";
      this.clear();
      const spec = this.manifest.states?.[this.currentState] || this.manifest.states?.idle || {};
      if (this.manifest.renderer === "sprite-sheet") this.renderSprite(spec);
      else if (this.manifest.renderer === "frame-sequence") this.renderSequence(spec);
      else this.renderSvg(spec);
      this.host.dataset.state = this.currentState;
      this.host.setAttribute("aria-label", textEquivalent || stateLabels[this.currentState]);
    }

    setMuted(value) {
      this.muted = Boolean(value);
      this.setState(this.currentState);
    }

    setReducedMotion(value) {
      this.reducedMotion = Boolean(value);
      this.setState(this.currentState);
    }

    destroy() {
      this.clear();
    }
  }

  const controller = {
    manifest: null,
    renderer: null,
    pending: { state: "idle", text: "" },
    preferences: readPreferences(),
    bound: false,
    suppressNextToggle: false,
    async loadManifest(manifestOrUrl = "/mascot/manifest.json") {
      try {
        const manifestUrl = typeof manifestOrUrl === "string" ? assetUrl(manifestOrUrl) : "";
        if (typeof manifestOrUrl === "string" && !manifestUrl) throw new Error("manifest_must_be_same_origin");
        const manifest = typeof manifestOrUrl === "string"
          ? await fetch(manifestUrl, { headers: { Accept: "application/json" } }).then((response) => {
            if (!response.ok) throw new Error(`manifest_${response.status}`);
            return response.json();
          })
          : manifestOrUrl;
        if (!manifest || typeof manifest !== "object" || !VALID_RENDERERS.has(manifest.renderer)) throw new Error("unsupported_manifest");
        if (this.renderer) this.renderer.destroy();
        this.manifest = manifest;
        const host = $("#mascot-visual");
        if (!host) return false;
        this.renderer = new AssetRenderer(host, manifest);
        this.renderer.setMuted(this.preferences.muted);
        this.renderer.setReducedMotion(this.preferences.reducedMotion || Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches));
        this.setState(this.pending.state, this.pending.text);
        return true;
      } catch {
        const host = $("#mascot-visual");
        if (host && !host.children.length) {
          host.innerHTML = '<span class="mascot-fallback-mark" aria-hidden="true"><span class="mascot-fallback-eyes"></span><span class="mascot-fallback-mouth"></span></span>';
        }
        return false;
      }
    },
    setState(state, textEquivalent = "") {
      const normalized = VALID_STATES.has(state) ? state : "idle";
      this.pending = { state: normalized, text: textEquivalent || "" };
      const root = $("#mascot");
      const label = $("#mascot-state-label");
      if (root) root.dataset.state = normalized;
      if (label) label.textContent = this.manifest?.states?.[normalized]?.label || stateLabels[normalized];
      if (this.renderer) this.renderer.setState(normalized, textEquivalent);
    },
    setMuted(value) {
      this.preferences.muted = Boolean(value);
      if (this.renderer) this.renderer.setMuted(this.preferences.muted);
      const button = $("#mascot-mute");
      if (button) {
        button.setAttribute("aria-pressed", String(this.preferences.muted));
        button.textContent = this.preferences.muted ? "开启声音" : "静音";
      }
      writePreferences(this.preferences);
    },
    setReducedMotion(value) {
      this.preferences.reducedMotion = Boolean(value);
      const root = $("#mascot");
      if (root) root.dataset.reducedMotion = String(this.preferences.reducedMotion);
      if (this.renderer) this.renderer.setReducedMotion(this.preferences.reducedMotion);
      const button = $("#mascot-motion");
      if (button) {
        button.setAttribute("aria-pressed", String(this.preferences.reducedMotion));
        button.textContent = this.preferences.reducedMotion ? "恢复动效" : "减少动效";
      }
      writePreferences(this.preferences);
    },
    toggleCollapsed() {
      const root = $("#mascot");
      if (!root) return;
      if (root.classList.contains("is-edge-hidden")) {
        const edge = validEdge(root.dataset.edge) || "right";
        const top = Number.parseFloat(root.style.top) || root.getBoundingClientRect().top;
        revealFromEdge(root, edge, top);
        this.preferences.collapsed = false;
        this.preferences.edge = edge;
        window.requestAnimationFrame(() => {
          const rect = root.getBoundingClientRect();
          this.preferences.position = { left: Math.round(rect.left), top: Math.round(rect.top) };
          writePreferences(this.preferences);
        });
        const toggle = $("#mascot-toggle");
        if (toggle) toggle.setAttribute("aria-expanded", "true");
        return;
      }
      root.classList.toggle("is-collapsed");
      if (root.classList.contains("is-collapsed")) root.style.width = "";
      this.preferences.collapsed = root.classList.contains("is-collapsed");
      this.preferences.edge = validEdge(root.dataset.edge);
      const toggle = $("#mascot-toggle");
      if (toggle) toggle.setAttribute("aria-expanded", String(!this.preferences.collapsed));
      writePreferences(this.preferences);
    },
    resetPosition() {
      const root = $("#mascot");
      if (!root) return;
      root.classList.remove("is-edge-hidden", "is-collapsed");
      root.removeAttribute("data-edge");
      root.style.left = "";
      root.style.top = "";
      root.style.right = "24px";
      root.style.bottom = "24px";
      root.style.width = "";
      this.preferences.position = null;
      this.preferences.edge = null;
      this.preferences.collapsed = false;
      const toggle = $("#mascot-toggle");
      if (toggle) toggle.setAttribute("aria-expanded", "true");
      writePreferences(this.preferences);
    },
    bind() {
      bindControls();
      // React mounts the mascot after the deferred bootstrap script. Load the
      // manifest again at that point so the visible asset replaces the CSS fallback.
      if (!this.manifest && $("#mascot-visual")) void this.loadManifest();
    },
  };

  function bindControls() {
    const root = $("#mascot");
    if (!root || controller.bound) return;
    controller.bound = true;
    if (controller.preferences.collapsed) root.classList.add("is-collapsed");
    controller.setMuted(controller.preferences.muted);
    controller.setReducedMotion(controller.preferences.reducedMotion);
    const toggle = $("#mascot-toggle");
    toggle?.addEventListener("click", (event) => {
      if (controller.suppressNextToggle) {
        controller.suppressNextToggle = false;
        event.preventDefault();
        return;
      }
      controller.toggleCollapsed();
    });
    $("#mascot-mute")?.addEventListener("click", () => controller.setMuted(!controller.preferences.muted));
    $("#mascot-motion")?.addEventListener("click", () => controller.setReducedMotion(!controller.preferences.reducedMotion));
    $("#mascot-reset-position")?.addEventListener("click", () => controller.resetPosition());

    const position = controller.preferences.position;
    const edge = validEdge(controller.preferences.edge);
    const compact = window.matchMedia?.("(max-width: 760px)")?.matches;
    if (position && edge && controller.preferences.collapsed) {
      dockToEdge(root, edge, position.top);
    } else if (position && position.left >= 0 && position.top >= 0) {
      if (edge) root.dataset.edge = edge;
      setRootPosition(root, position.left, position.top);
    } else if (compact) {
      dockToEdge(root, "right", window.innerHeight - 72);
      controller.preferences.edge = "right";
      controller.preferences.collapsed = true;
      const rect = root.getBoundingClientRect();
      controller.preferences.position = { left: Math.round(rect.left), top: Math.round(rect.top) };
      writePreferences(controller.preferences);
    }

    let dragging = false;
    let moved = false;
    let startedEdgeHidden = false;
    let offsetX = 0;
    let offsetY = 0;
    let pointerStartStyles = null;
    const handle = $("#mascot-toggle");
    handle?.addEventListener("pointerdown", (event) => {
      if (event.pointerType === "mouse" && event.button !== 0) return;
      // A real follow-up gesture starts with pointerdown. Clear the guard here
      // so a drag's synthetic click cannot swallow the next user tap.
      controller.suppressNextToggle = false;
      const rect = root.getBoundingClientRect();
      dragging = true;
      moved = false;
      startedEdgeHidden = root.classList.contains("is-edge-hidden");
      pointerStartStyles = {
        left: root.style.left,
        top: root.style.top,
        right: root.style.right,
        bottom: root.style.bottom,
      };
      offsetX = event.clientX - rect.left;
      offsetY = event.clientY - rect.top;
      if (!startedEdgeHidden) {
        root.dataset.dragging = "true";
        root.style.right = "auto";
        root.style.bottom = "auto";
      }
      handle.setPointerCapture?.(event.pointerId);
      handle.classList.add("is-dragging");
    });
    handle?.addEventListener("pointermove", (event) => {
      if (!dragging) return;
      if (startedEdgeHidden && !moved) {
        root.classList.remove("is-edge-hidden");
        root.dataset.dragging = "true";
        root.style.right = "auto";
        root.style.bottom = "auto";
      }
      const left = Math.max(-root.offsetWidth / 2, Math.min(window.innerWidth - root.offsetWidth / 2, event.clientX - offsetX));
      const top = Math.max(8, Math.min(window.innerHeight - root.offsetHeight - 8, event.clientY - offsetY));
      if (Math.abs(left - root.offsetLeft) > 2 || Math.abs(top - root.offsetTop) > 2) moved = true;
      setRootPosition(root, left, top);
    });
    handle?.addEventListener("pointerup", (event) => {
      if (!dragging) return;
      dragging = false;
      startedEdgeHidden = false;
      handle.releasePointerCapture?.(event.pointerId);
      handle.classList.remove("is-dragging");
      root.removeAttribute("data-dragging");
      if (!moved && pointerStartStyles) {
        root.style.left = pointerStartStyles.left;
        root.style.top = pointerStartStyles.top;
        root.style.right = pointerStartStyles.right;
        root.style.bottom = pointerStartStyles.bottom;
        pointerStartStyles = null;
        return;
      }
      pointerStartStyles = null;
      if (moved) {
        const rect = root.getBoundingClientRect();
        controller.suppressNextToggle = true;
        const edgeDistance = Math.min(rect.left, window.innerWidth - (rect.left + rect.width));
        const edge = rect.left + rect.width / 2 < window.innerWidth / 2 ? "left" : "right";
        if (edgeDistance <= 72) {
          dockToEdge(root, edge, rect.top);
          controller.preferences.edge = edge;
          controller.preferences.collapsed = true;
        } else {
          root.classList.remove("is-edge-hidden", "is-collapsed");
          controller.preferences.edge = validEdge(root.dataset.edge);
          controller.preferences.collapsed = false;
        }
        const nextRect = root.getBoundingClientRect();
        controller.preferences.position = { left: Math.round(nextRect.left), top: Math.round(nextRect.top) };
        writePreferences(controller.preferences);
        if (toggle) toggle.setAttribute("aria-expanded", String(!controller.preferences.collapsed));
        window.setTimeout(() => { controller.suppressNextToggle = false; }, 0);
      }
    });

    window.addEventListener("resize", () => {
      if (!root.classList.contains("is-edge-hidden")) return;
      const side = validEdge(root.dataset.edge) || "right";
      dockToEdge(root, side, Number.parseFloat(root.style.top) || window.innerHeight - 72);
    });
  }

  window.QSTMascot = controller;
  window.addEventListener("DOMContentLoaded", () => {
    controller.bind();
  });
})();
