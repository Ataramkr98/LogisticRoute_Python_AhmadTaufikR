import Alpine from "@alpinejs/csp";
import {
  CategoryScale,
  Chart,
  Filler,
  Legend,
  LineController,
  LineElement,
  LinearScale,
  PointElement,
  Tooltip,
} from "chart.js";
import htmx from "htmx.org";
import L from "leaflet";
import "leaflet.markercluster";
import Sortable from "sortablejs";

// Only the pieces the throughput chart actually uses are registered. Importing
// `chart.js/auto` pulled every controller, renderer and scale into the bundle and
// added ~200 kB of code this app never executes.
Chart.register(
  CategoryScale,
  LinearScale,
  LineController,
  LineElement,
  PointElement,
  Filler,
  Legend,
  Tooltip
);

window.Alpine = Alpine;
window.htmx = htmx;
window.L = L;
window.Sortable = Sortable;

// Chart.js is configured from the CSS custom properties rather than literal
// colours so the chart cannot drift away from the design tokens when they change.
function tokenColor(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function renderThroughputChart(canvas) {
  if (canvas.dataset.chartReady === "true") return;
  canvas.dataset.chartReady = "true";
  const numbers = (value) =>
    (value || "")
      .split(",")
      .filter((part) => part !== "")
      .map(Number);
  const labels = (canvas.dataset.labels || "").split(",").filter(Boolean);
  new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Scheduled",
          data: numbers(canvas.dataset.scheduled),
          borderColor: tokenColor("--brand", "#1264e8"),
          backgroundColor: tokenColor("--brand", "#1264e8"),
          tension: 0,
          borderWidth: 2,
          pointRadius: 3,
        },
        {
          label: "Completed",
          data: numbers(canvas.dataset.delivered),
          borderColor: tokenColor("--success", "#126b46"),
          backgroundColor: tokenColor("--success", "#126b46"),
          tension: 0,
          borderWidth: 2,
          borderDash: [5, 4],
          pointRadius: 3,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "bottom", labels: { boxWidth: 10, usePointStyle: true } },
      },
      scales: {
        y: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: tokenColor("--line", "#d9ddd8") } },
        x: { grid: { display: false } },
      },
    },
  });
}

document.querySelectorAll("[data-chart='throughput']").forEach(renderThroughputChart);

// The Content-Security-Policy served by SecurityHeadersMiddleware is
// `script-src 'self'` with no `unsafe-eval`. htmx still defaults `allowEval` to
// true, which routes hx-on, hx-vals="js:" and `hx-trigger="[...]"` through
// `new Function()`. The browser blocks that, and because the call sits inside an
// event listener the resulting EvalError escapes and breaks the interaction.
// Turning it off makes htmx emit `htmx:evalDisallowedError` and degrade
// predictably instead of throwing. Nothing in this project uses those three
// features, so nothing regresses; the setting is what keeps it that way.
htmx.config.allowEval = false;

// htmx swaps innerHTML, which produces nodes Alpine has never walked. Without
// re-initialising, the first Alpine directive or Leaflet map added to a swapped
// partial would be silently inert.
document.body.addEventListener("htmx:afterSwap", (event) => {
  if (window.Alpine) window.Alpine.initTree(event.target);
  document.querySelectorAll("[data-live-map]").forEach(setupLiveMap);
  document.querySelectorAll("[data-chart='throughput']").forEach(renderThroughputChart);
});

document.body.addEventListener("htmx:configRequest", (event) => {
  const token = document.querySelector("[name=csrfmiddlewaretoken]")?.value;
  if (token) event.detail.headers["X-CSRFToken"] = token;
});

function addTileLayer(map, element) {
  L.tileLayer(element.dataset.tileUrl, {
    attribution: element.dataset.attribution,
    maxZoom: 19,
  }).addTo(map);
}

function textElement(tagName, text, className = "") {
  const element = document.createElement(tagName);
  element.textContent = String(text ?? "");
  if (className) element.className = className;
  return element;
}

async function fetchJSON(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

document.querySelectorAll("[data-route-map]").forEach((element) => {
  const map = L.map(element, { zoomControl: true });
  addTileLayer(map, element);
  const dataElement = document.getElementById(element.dataset.routeMap);
  let routes = [];
  try {
    routes = dataElement ? JSON.parse(dataElement.textContent) : [];
  } catch (error) {
    console.warn("Route map data could not be parsed", error);
  }
  const group = L.featureGroup().addTo(map);
  routes.forEach((route) => {
    if (!route.coordinates?.length) return;
    const latLngs = route.coordinates.map(([longitude, latitude]) => [latitude, longitude]);
    L.polyline(latLngs, { color: route.color, weight: 5, opacity: 0.85 })
      .bindTooltip(textElement("span", `${route.vehicle} · Route ${route.id}`))
      .addTo(group);
    latLngs.slice(1, -1).forEach((latLng, index) => {
      L.marker(latLng, {
        icon: L.divIcon({
          className: "",
          html: '<div class="marker-pin" aria-label="Stop ' + (index + 1) + '">' + (index + 1) + "</div>",
          iconSize: [28, 28],
          iconAnchor: [14, 14],
        }),
      }).addTo(group);
    });
  });
  if (group.getLayers().length) map.fitBounds(group.getBounds(), { padding: [36, 36] });
  else map.setView([-6.2146, 106.8272], 11);
});

// Exported as a named function so the htmx:afterSwap hook above can bind a map
// that arrives inside a swapped partial. Guarded with a dataset flag because
// Leaflet throws on a second init for the same container.
function setupLiveMap(element) {
  if (element.dataset.liveMapReady === "true") return;
  element.dataset.liveMapReady = "true";
  const map = L.map(element);
  addTileLayer(map, element);
  map.setView([-6.2146, 106.8272], 11);
  const routeColors = ["#1264E8", "#126B46", "#E76F24", "#7C3AED"];
  const routeLayer = L.geoJSON(null, {
    style: (feature) => ({
      color: routeColors[(Number(feature?.properties?.route_id) || 0) % routeColors.length],
      weight: 4,
      opacity: 0.78,
    }),
    onEachFeature: (feature, layer) => layer.bindTooltip(textElement(
      "span",
      `${feature.properties.vehicle} · ${feature.properties.driver || "Unassigned"}`
    )),
  }).addTo(map);
  const vehicleLayer = L.markerClusterGroup().addTo(map);
  const refresh = async () => {
    try {
      const [routes, vehicles] = await Promise.all([
        fetchJSON("/api/v1/live/routes/", { headers: { Accept: "application/json" } }),
        fetchJSON("/api/v1/live/vehicles/", { headers: { Accept: "application/json" } }),
      ]);
      routeLayer.clearLayers().addData(routes);
      vehicleLayer.clearLayers();
      L.geoJSON(vehicles, {
        pointToLayer: (feature, latLng) => L.marker(latLng, {
          icon: L.divIcon({
            className: "",
            html: '<div class="marker-pin is-vehicle ' + (feature.properties.stale ? "opacity-50" : "") + '"><i class="ti ti-truck-delivery" aria-hidden="true"></i></div>',
            iconSize: [32, 32],
            iconAnchor: [16, 16],
          }),
        }),
        onEachFeature: (feature, layer) => {
          const popup = document.createElement("div");
          popup.append(
            textElement("strong", feature.properties.vehicle),
            document.createElement("br"),
            document.createTextNode(feature.properties.driver || "No driver")
          );
          layer.bindPopup(popup);
        },
      }).eachLayer((layer) => vehicleLayer.addLayer(layer));
    } catch (error) {
      console.warn("Live map refresh failed", error);
    }
  };
  refresh();
  window.setInterval(refresh, 15000);
}

document.querySelectorAll("[data-live-map]").forEach(setupLiveMap);

document.querySelectorAll("[data-planning-progress]").forEach((element) => {
  const runId = element.dataset.planningProgress;
  // Read the review URL from the DOM rather than hardcoding a path, so a
  // deployment under a prefix does not silently redirect to a 404.
  const reviewUrl = element.dataset.reviewUrl || `/app/planning/review/${runId}/`;
  const endpoint = "/api/v1/planning-runs/" + runId + "/progress/";
  const source = new EventSource(endpoint);

  // The phase labels were rendered with server-side threshold classes and had
  // no data hook, so they stayed frozen while the bar and percentage animated.
  // The thresholds live here, next to the code that applies them.
  const PHASES = [
    ["readiness", 10],
    ["matrix", 30],
    ["solver", 60],
    ["routes", 85],
  ];
  const paint = (snapshot) => {
    const percent = Number(snapshot.percent || 0);
    const bar = element.querySelector("[data-progress-bar]");
    const label = element.querySelector("[data-progress-percent]");
    const message = element.querySelector("[data-progress-message]");
    if (bar) bar.style.width = percent + "%";
    if (label) label.textContent = percent + "%";
    if (message) message.textContent = snapshot.message || snapshot.status || "";
    PHASES.forEach(([name, threshold]) => {
      const node = element.querySelector(`[data-phase="${name}"]`);
      if (!node) return;
      const reached = percent >= threshold;
      node.classList.toggle("text-blue-700", reached);
      node.classList.toggle("text-slate-400", !reached);
    });
    return snapshot;
  };

  const apply = (payload) => {
    const snapshot = paint(payload);
    if (["REVIEW", "OPTIMIZED"].includes(snapshot.status) || snapshot.percent === 100) {
      window.location.reload();
    }
    if (["FAILED", "CANCELLED"].includes(snapshot.status)) {
      source.close();
      window.location.reload();
    }
  };

  // Redis is optional, so the stream endpoint reports an unavailable channel
  // instead of failing. Polling the same URL without the SSE Accept header
  // returns the identical snapshot, which keeps the page live on a deployment
  // that runs without a broker. Polling backs off once the run is finished.
  let poller = 0;
  const poll = async () => {
    if (poller) return;
    poller = window.setInterval(async () => {
      try {
        const response = await fetch(endpoint, { headers: { Accept: "application/json" } });
        if (!response.ok) return;
        const snapshot = paint(await response.json());
        if (["REVIEW", "OPTIMIZED", "FAILED", "CANCELLED"].includes(snapshot.status)) {
          window.clearInterval(poller);
          poller = 0;
          window.location.reload();
        }
      } catch (error) {
        console.warn("Optimization progress poll failed", error);
      }
    }, 3000);
  };

  source.addEventListener("update", (event) => apply(JSON.parse(event.data)));
  source.addEventListener("unavailable", () => {
    source.close();
    poll();
  });
  source.onerror = () => {
    source.close();
    window.setTimeout(() => window.location.reload(), 4000);
  };
});

document.querySelectorAll("[data-select-all]").forEach((master) => {
  const name = master.dataset.selectAll;
  const scope = master.closest("form") || document;
  const boxes = () => Array.from(scope.querySelectorAll(`[name="${name}"]`));

  const sync = () => {
    const all = boxes();
    const checked = all.filter((box) => box.checked).length;
    master.checked = checked > 0 && checked === all.length;
    // The mixed state is what tells the operator "some are selected" without
    // them having to count rows.
    master.indeterminate = checked > 0 && checked < all.length;
  };

  master.addEventListener("change", () => {
    boxes().forEach((box) => {
      box.checked = master.checked;
    });
    sync();
  });

  const container = master.closest("label")?.parentElement || document;
  container.addEventListener("change", (event) => {
    if (event.target instanceof HTMLInputElement && event.target.name === name) sync();
  });
  sync();
});

document.querySelectorAll("[data-sortable-route]").forEach((list) => {
  new Sortable(list, {
    group: "route-stops",
    animation: 160,
    handle: "[data-drag-handle]",
    draggable: "[data-stop-id]",
    ghostClass: "opacity-40",
    onEnd: async (event) => {
      const item = event.item;
      const target = event.to;
      const token = document.querySelector("[name=csrfmiddlewaretoken]")?.value;
      item.classList.add("opacity-50", "pointer-events-none");
      try {
        const response = await fetch(`/api/v1/route-stops/${item.dataset.stopId}/move/`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": token || "",
            "If-Match": `W/"${item.dataset.stopVersion}"`,
          },
          body: JSON.stringify({
            target_route_id: Number(target.dataset.sortableRoute),
            target_sequence: event.newIndex + 2,
          }),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.message || "The stop could not be moved.");
        // The server returns the new plan version, so reload the URL the page
        // was rendered with instead of assuming a path shape.
        const reviewUrl = document.querySelector("[data-review-url]")?.dataset.reviewUrl;
        if (reviewUrl) window.location.assign(reviewUrl);
        else window.location.reload();
      } catch (error) {
        window.alert(error.message || "The stop could not be moved.");
        window.location.reload();
      }
    },
  });
});

const DB_NAME = "logistics-driver";
const STORE = "events";
function openQueue() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE, { keyPath: "idempotency_key" });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
async function queueEvent(event) {
  const db = await openQueue();
  const transaction = db.transaction(STORE, "readwrite");
  transaction.objectStore(STORE).put(event);
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => { db.close(); resolve(); };
    transaction.onerror = () => { db.close(); reject(transaction.error); };
    transaction.onabort = () => { db.close(); reject(transaction.error); };
  });
}
async function queuedEvents() {
  const db = await openQueue();
  const request = db.transaction(STORE).objectStore(STORE).getAll();
  return new Promise((resolve, reject) => {
    request.onsuccess = () => { db.close(); resolve(request.result); };
    request.onerror = () => { db.close(); reject(request.error); };
  });
}
async function removeQueued(keys) {
  const db = await openQueue();
  const transaction = db.transaction(STORE, "readwrite");
  keys.forEach((key) => transaction.objectStore(STORE).delete(key));
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => { db.close(); resolve(); };
    transaction.onerror = () => { db.close(); reject(transaction.error); };
    transaction.onabort = () => { db.close(); reject(transaction.error); };
  });
}

function deleteDriverQueue() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.deleteDatabase(DB_NAME);
    request.onsuccess = resolve;
    request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new Error("The offline queue is still in use."));
  });
}
async function syncQueue() {
  const events = await queuedEvents();
  if (!events.length || !navigator.onLine) return [];
  const token = document.querySelector("[name=csrfmiddlewaretoken]")?.value;
  const headers = { "Content-Type": "application/json" };
  if (token) headers["X-CSRFToken"] = token;
  try {
    const response = await fetch("/api/v1/driver/offline-batch/", {
      method: "POST",
      headers,
      body: JSON.stringify({ events }),
    });
    if (!response.ok) throw new Error(`Synchronization failed (${response.status}).`);
    const payload = await response.json();
    const settled = payload.results
      .filter((item) => ["accepted", "rejected"].includes(item.status))
      .map((item) => item.idempotency_key)
      .filter(Boolean);
    await removeQueued(settled);
    document.dispatchEvent(new CustomEvent("driver-queue-updated", { detail: payload }));
    return payload.results;
  } catch (error) {
    console.warn("Driver queue synchronization failed", error);
    document.dispatchEvent(new CustomEvent("driver-queue-updated", {
      detail: { error: error.message },
    }));
    return null;
  }
}

document.querySelectorAll("[data-driver-action]").forEach((button) => {
  button.addEventListener("click", async () => {
    button.disabled = true;
    const event = {
      event_type: button.dataset.eventType,
      route_id: Number(button.dataset.routeId),
      stop_id: button.dataset.stopId ? Number(button.dataset.stopId) : null,
      idempotency_key: crypto.randomUUID(),
      occurred_at: new Date().toISOString(),
      sequence: Date.now(),
      expected_version: Number(button.dataset.expectedVersion || 1),
      payload: { reason_code: button.dataset.reasonCode || undefined },
    };
    if (navigator.geolocation) await new Promise((resolve) => {
      navigator.geolocation.getCurrentPosition((position) => {
        event.payload.longitude = position.coords.longitude;
        event.payload.latitude = position.coords.latitude;
        event.payload.accuracy_m = position.coords.accuracy;
        resolve();
      }, resolve, { timeout: 5000, maximumAge: 30000 });
    });
    try {
      await queueEvent(event);
      const results = await syncQueue();
      const current = results?.find((item) => item.idempotency_key === event.idempotency_key);
      if (current?.status === "rejected") {
        button.disabled = false;
        return;
      }
      window.location.reload();
    } catch (error) {
      document.dispatchEvent(new CustomEvent("driver-queue-updated", {
        detail: { error: error.message || "The action could not be queued." },
      }));
      button.disabled = false;
    }
  });
});

async function updateOnlineState(event) {
  const count = await queuedEvents().then((items) => items.length).catch(() => 0);
  document.querySelectorAll("[data-online-label]").forEach((item) => {
    if (navigator.onLine) item.textContent = count ? `Online · ${count} queued` : "Online";
    else item.textContent = `Offline · ${count} queued`;
  });
  const message = document.querySelector("[data-driver-sync-message]");
  if (!message) return;
  const rejected = event?.detail?.results?.find((item) => item.status === "rejected");
  const error = event?.detail?.error;
  const text = rejected?.message || error || "";
  message.textContent = text;
  message.hidden = !text;
}
if (document.body.hasAttribute("data-driver-shell")) {
  window.addEventListener("online", syncQueue);
  window.addEventListener("online", updateOnlineState);
  window.addEventListener("offline", updateOnlineState);
  document.addEventListener("driver-queue-updated", updateOnlineState);
  updateOnlineState();
  syncQueue();
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/service-worker.js").catch((error) => {
      console.warn("Driver service worker registration failed", error);
    });
  }
  const logoutForm = document.querySelector("[data-driver-logout]");
  logoutForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = logoutForm.querySelector("button");
    if (button) button.disabled = true;
    await syncQueue();
    const remaining = await queuedEvents().catch(() => ["unknown"]);
    if (remaining.length) {
      document.dispatchEvent(new CustomEvent("driver-queue-updated", {
        detail: { error: "Connect and synchronize queued actions before signing out." },
      }));
      if (button) button.disabled = false;
      return;
    }
    try {
      await deleteDriverQueue();
      if ("caches" in window) {
        const cacheKeys = await caches.keys();
        await Promise.all(
          cacheKeys.filter((key) => key.startsWith("logistics-driver-")).map((key) => caches.delete(key))
        );
      }
      if ("serviceWorker" in navigator) {
        const registrations = await navigator.serviceWorker.getRegistrations();
        await Promise.all(registrations.map((registration) => registration.unregister()));
      }
      logoutForm.submit();
    } catch (error) {
      document.dispatchEvent(new CustomEvent("driver-queue-updated", {
        detail: { error: error.message || "Offline data could not be cleared." },
      }));
      if (button) button.disabled = false;
    }
  });
}
Alpine.start();
