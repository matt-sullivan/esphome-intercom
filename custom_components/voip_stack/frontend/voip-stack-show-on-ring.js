/**
 * show_on_ring: while a dashboard is open, keep the phone of its
 * `show_on_ring` card online and switch to that card's view when it rings.
 *
 * The card's own options only take effect once Lovelace renders it, which
 * happens only for the view on screen. This reads the dashboard config
 * instead, and leaves call handling (ringtone, Auto Answer, video) to the card
 * once its view opens.
 *
 * This module loads once per browser tab and one ShowOnRing lives for the whole
 * page. It follows navigation rather than being recreated:
 * - sync() runs on every navigation but acts only when the dashboard (first
 *   path segment) changes: it releases the previous dashboard's phone and
 *   starts loading the new one. Moving between views of one dashboard is a
 *   no-op, so the phone stays online.
 * - _load() runs once on entering a dashboard and again after each saved edit
 *   of it: it reads the config, finds the show_on_ring card and (re)subscribes
 *   to its phone.
 * - _onPhoneState() runs for each state of that phone and opens the card's
 *   view when it rings.
 */

const CARD_TYPE = "custom:voip-stack-card";
const HASS_POLL_MS = 500;

// Return the first show_on_ring phone card in a dashboard config, with the
// index and path segment of the view that holds it, and how many there are.
export function findShowOnRingCard(dashboardConfig) {
  const found = [];
  const visit = (node, view) => {
    if (Array.isArray(node)) {
      for (const item of node) visit(item, view);
      return;
    }
    if (!node || typeof node !== "object") return;
    if (
      node.type === CARD_TYPE &&
      node.show_on_ring === true &&
      (node.mode || node.card_mode) === "ha_softphone"
    ) {
      found.push({ config: node, view });
    }
    for (const value of Object.values(node)) visit(value, view);
  };
  const views = Array.isArray(dashboardConfig?.views) ? dashboardConfig.views : [];
  views.forEach((view, index) => {
    visit(view, String(view?.path || index));
  });
  return found.length ? { ...found[0], count: found.length } : null;
}

// The dashboard is the first path segment, e.g. "kitchen-tablet" in
// /kitchen-tablet/home.
export function dashboardFromPath(pathname) {
  return String(pathname || "").split("/").filter(Boolean)[0] || "";
}

export class ShowOnRing {
  // location, history and the event target are injectable so tests can run
  // without a browser.
  constructor(engine, { getHass, location = globalThis.location, history = globalThis.history,
    target = globalThis.window } = {}) {
    this._engine = engine;
    this._getHass = getHass;
    this._location = location;
    this._history = history;
    this._target = target;
    this._dashboard = "";
    this._generation = 0;
    this._unsubPhone = null;
    this._unsubUpdates = null;
    this._shownCallId = "";
    this._viewPath = "";
  }

  // Lovelace navigates with location-changed and the browser's back and
  // forward buttons fire popstate; either can move to another dashboard.
  start() {
    const sync = () => void this.sync();
    this._target?.addEventListener?.("location-changed", sync);
    this._target?.addEventListener?.("popstate", sync);
    return this.sync();
  }

  // On navigation: when the dashboard changes, release the old one's phone,
  // then watch the new one for edits and load it if it is a Lovelace dashboard.
  async sync() {
    const dashboard = dashboardFromPath(this._location?.pathname);
    if (dashboard === this._dashboard) return;
    this._stop();
    this._dashboard = dashboard;
    const generation = this._generation;
    const hass = await this._waitForHass();
    if (generation !== this._generation || !hass) return;
    if (hass.panels?.[dashboard]?.component_name !== "lovelace") return;
    this._watchDashboardUpdates(hass, dashboard, generation);
    await this._load(hass, dashboard, generation);
  }

  // On entering a dashboard or after an edit to it: read its config and
  // subscribe to the show_on_ring card's phone, replacing any earlier
  // subscription so an edit never leaves two.
  async _load(hass, dashboard, generation) {
    let config;
    try {
      config = await hass.callWS({
        type: "lovelace/config",
        url_path: dashboard === "lovelace" ? null : dashboard,
      });
    } catch (error) {
      console.warn("voip-stack: show_on_ring could not read the dashboard", error);
      return;
    }
    if (generation !== this._generation) return;
    this._unsubPhone?.();
    this._unsubPhone = null;
    const card = findShowOnRingCard(config);
    if (!card) return;
    if (card.count > 1) {
      console.warn(
        `voip-stack: ${card.count} phone cards on this dashboard have show_on_ring; ` +
        "only the first one is used",
      );
    }
    this._viewPath = `/${dashboard}/${card.view}`;
    const deviceId = String(card.config.device_id || "").trim();
    // No card has handed the engine a connection yet on other views.
    this._engine.configure(hass);
    // A background subscription keeps the phone online without counting as a
    // card that is showing it.
    this._unsubPhone = this._engine.subscribeSoftphoneState(
      (state) => this._onPhoneState(state),
      deviceId ? { device_id: deviceId } : {},
      { background: true },
    );
  }

  // A dashboard edit can add, move or remove the show_on_ring card.
  _watchDashboardUpdates(hass, dashboard, generation) {
    const urlPath = dashboard === "lovelace" ? null : dashboard;
    Promise.resolve(hass.connection?.subscribeEvents?.((event) => {
      if (generation !== this._generation || (event?.data?.url_path ?? null) !== urlPath) return;
      void this._load(hass, dashboard, generation);
    }, "lovelace_updated")).then((unsub) => {
      if (generation === this._generation) this._unsubUpdates = unsub || null;
      else unsub?.();
    }).catch(() => {});
  }

  // Open the card's view for an incoming call; the card there rings, auto
  // answers and shows video.
  _onPhoneState(state) {
    if (state?.state !== "ringing" || state.direction !== "incoming" || !state.call_id) return;
    // Switch once per call, so leaving the view while it rings is respected.
    if (state.call_id === this._shownCallId) return;
    this._shownCallId = state.call_id;
    if (this._engine.hasLiveSoftphoneView(state)) return;
    if (this._location.pathname === this._viewPath) return;
    // The same navigation Home Assistant's own navigate action performs.
    this._history.pushState(null, "", this._viewPath);
    this._target.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
  }

  // Release the phone when leaving the dashboard. Bumping the generation makes
  // any load or wait still in flight for it discard its result.
  _stop() {
    this._generation++;
    this._unsubPhone?.();
    this._unsubPhone = null;
    this._unsubUpdates?.();
    this._unsubUpdates = null;
    this._viewPath = "";
  }

  // The card module can load before Home Assistant has connected.
  async _waitForHass() {
    const generation = this._generation;
    for (;;) {
      const hass = this._getHass();
      if (hass?.connection) return hass;
      await new Promise((resolve) => setTimeout(resolve, HASS_POLL_MS));
      if (generation !== this._generation) return null;
    }
  }
}

// The card module imports this on every dashboard view, before or without the
// card being rendered, so a show_on_ring phone is online after any reload.
// Tests import the exports above without a document and skip this.
if (globalThis.document && !globalThis.__voipStackShowOnRing) {
  const VERSION = new URL(import.meta.url).searchParams.get("v") || "dev";
  const { voipStackEngine } = await import(`./voip-stack-engine.js?v=${encodeURIComponent(VERSION)}`);
  globalThis.__voipStackShowOnRing = new ShowOnRing(voipStackEngine, {
    getHass: () => document.querySelector("home-assistant")?.hass,
  });
  void globalThis.__voipStackShowOnRing.start();
}
