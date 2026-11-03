#!/usr/bin/env python3
"""Runtime tests for the show_on_ring dashboard keeper."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.js_runtime


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-stack-show-on-ring.js"


def _run(body: str) -> None:
    script = rf"""
import assert from "assert/strict";
import {{ pathToFileURL }} from "url";
const {{ ShowOnRing, findShowOnRingCard, dashboardFromPath }} = await import(
  pathToFileURL({json.dumps(str(MODULE))})
);
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const phoneCard = (extra = {{}}) => ({{
  type: "custom:voip-stack-card", mode: "ha_softphone", device_id: "kitchen-tablet", ...extra,
}});

function harness({{ config, pathname = "/kitchen-tablet/home", panels }} = {{}}) {{
  const location = {{ pathname }};
  const target = new EventTarget();
  const pushed = [];
  const navigations = [];
  target.addEventListener("location-changed", () => navigations.push(location.pathname));
  const history = {{ pushState(_state, _title, path) {{ pushed.push(path); location.pathname = path; }} }};
  const subscriptions = [];
  const engine = {{
    live: false,
    configured: 0,
    configure() {{ this.configured++; }},
    hasLiveSoftphoneView() {{ return this.live; }},
    subscribeSoftphoneState(cb, selector, options) {{
      const record = {{ cb, selector, options, active: true }};
      subscriptions.push(record);
      return () => {{ record.active = false; }};
    }},
  }};
  const updates = [];
  const hass = {{
    panels: panels || {{
      "kitchen-tablet": {{ component_name: "lovelace" }},
      lovelace: {{ component_name: "lovelace" }},
      config: {{ component_name: "config" }},
    }},
    requests: [],
    async callWS(message) {{ this.requests.push(message); return config; }},
    connection: {{
      async subscribeEvents(cb, type) {{
        assert.equal(type, "lovelace_updated");
        updates.push(cb);
        return () => {{}};
      }},
    }},
  }};
  const keeper = new ShowOnRing(engine, {{ getHass: () => hass, location, history, target }});
  const active = () => subscriptions.filter((record) => record.active);
  const ring = (callId = "call-1") => active().forEach((record) => record.cb({{
    state: "ringing", direction: "incoming", call_id: callId, device_id: "kitchen-tablet",
  }}));
  return {{ keeper, engine, hass, location, target, pushed, navigations, subscriptions, active, ring, updates }};
}}

{body}
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_show_on_ring_finds_one_card_and_its_view() -> None:
    _run(
        r"""
assert.equal(dashboardFromPath("/kitchen-tablet/home"), "kitchen-tablet");
assert.equal(dashboardFromPath("/"), "");

// Cards nested in sections and stacks are found, with the view's path or index.
const found = findShowOnRingCard({ views: [
  { path: "home", cards: [phoneCard()] },
  { sections: [{ cards: [{ type: "vertical-stack", cards: [phoneCard({ show_on_ring: true })] }] }] },
  { path: "other", cards: [phoneCard({ show_on_ring: true, device_id: "second" })] },
] });
assert.equal(found.view, "1");
assert.equal(found.config.device_id, "kitchen-tablet");
assert.equal(found.count, 2);

assert.equal(findShowOnRingCard({ views: [{ path: "call", cards: [phoneCard({ show_on_ring: true })] }] }).view, "call");
// ESP mirror cards and unset options are ignored, as are dashboards without views.
assert.equal(findShowOnRingCard({ views: [{ cards: [
  phoneCard({ mode: "esp_mirror", show_on_ring: true }), phoneCard(),
] }] }), null);
assert.equal(findShowOnRingCard({ strategy: { type: "original-states" } }), null);
"""
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_show_on_ring_keeps_phone_online_and_opens_view_once_per_call() -> None:
    _run(
        r"""
const h = harness({ config: { views: [
  { path: "home", cards: [] },
  { path: "call", cards: [phoneCard({ show_on_ring: true })] },
] } });
await h.keeper.start();
assert.deepEqual(h.hass.requests, [{ type: "lovelace/config", url_path: "kitchen-tablet" }]);
assert.equal(h.engine.configured, 1);
assert.equal(h.active().length, 1);
assert.deepEqual(h.active()[0].selector, { device_id: "kitchen-tablet" });
assert.deepEqual(h.active()[0].options, { background: true }, "The keeper must not count as a view");

// Idle or outgoing states never navigate.
h.active()[0].cb({ state: "idle", call_id: "" });
h.active()[0].cb({ state: "ringing", direction: "outgoing", call_id: "out" });
assert.deepEqual(h.pushed, []);

h.ring("call-1");
assert.deepEqual(h.pushed, ["/kitchen-tablet/call"]);
assert.deepEqual(h.navigations, ["/kitchen-tablet/call"]);

// Someone leaves the view while it still rings: do not switch back.
h.location.pathname = "/kitchen-tablet/home";
h.ring("call-1");
assert.equal(h.pushed.length, 1);

// A card for the phone is already showing: leave the screen alone.
h.engine.live = true;
h.ring("call-2");
assert.equal(h.pushed.length, 1);
h.engine.live = false;
h.ring("call-3");
assert.deepEqual(h.pushed, ["/kitchen-tablet/call", "/kitchen-tablet/call"]);
"""
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_show_on_ring_follows_dashboard_and_edits() -> None:
    _run(
        r"""
const h = harness({ pathname: "/lovelace/0", config: { views: [{ cards: [phoneCard({ show_on_ring: true })] }] } });
await h.keeper.start();
// The default dashboard is requested without a url_path.
assert.deepEqual(h.hass.requests, [{ type: "lovelace/config", url_path: null }]);
assert.equal(h.active().length, 1);

// Moving to another view of the same dashboard keeps the phone online.
h.location.pathname = "/lovelace/1";
h.target.dispatchEvent(new Event("location-changed"));
await tick();
assert.equal(h.hass.requests.length, 1);
assert.equal(h.active().length, 1);

// Leaving for a panel that is not a dashboard releases it.
h.location.pathname = "/config/integrations";
h.target.dispatchEvent(new Event("location-changed"));
await tick();
assert.equal(h.active().length, 0);

// A dashboard without a show_on_ring card holds nothing, until an edit adds one.
const edited = harness({ config: { views: [{ cards: [phoneCard()] }] } });
await edited.keeper.start();
assert.equal(edited.active().length, 0);
assert.equal(edited.engine.configured, 0, "Dashboards without the option leave the engine alone");
edited.hass.callWS = async () => ({ views: [{ path: "phone", cards: [phoneCard({ show_on_ring: true })] }] });
edited.updates[0]({ data: { url_path: "another-dashboard" } });
await tick();
assert.equal(edited.active().length, 0, "Edits to other dashboards are ignored");
edited.updates[0]({ data: { url_path: "kitchen-tablet" } });
await tick();
assert.equal(edited.active().length, 1);
edited.ring();
assert.deepEqual(edited.pushed, ["/kitchen-tablet/phone"]);
"""
    )
