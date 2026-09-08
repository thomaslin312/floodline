import maplibregl, { type Map as MapLibreMap, type Marker, Popup } from "maplibre-gl";

import type { FloodBundle, Gauge, Mark } from "../types/bundle";
import { depthAtPixel, markPixel, type Grid } from "../lib/grid";
import { bboxOf, imageCorners } from "../lib/mercator";
import { DAMAGE_RAMP, DEPTH_RAMP, sample } from "../lib/ramp";
import type { ChannelPick } from "../lib/damage";

/**
 * Everything that touches MapLibre.
 *
 * React holds one of these in a ref and calls methods on it; it never renders map
 * state. That is deliberate rather than lazy. The map is a mutable GPU-backed object
 * with its own lifecycle, sources and layers keyed by string id, and a style that
 * loads asynchronously - modelling it as derived state would mean diffing against
 * something that is already the source of truth. So the boundary is: React owns the
 * panels, this owns the canvas, and the only things crossing are method calls one way
 * and callbacks the other.
 */

const css = (name: string): string =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const dark = (): boolean => window.matchMedia("(prefers-color-scheme: dark)").matches;

const ESRI = "https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/";
// Esri's grey canvas: a deliberately quiet basemap with the labels published as a
// separate layer, which is what lets place names sit *above* the flood instead of
// disappearing under it. Keyless, unlike Carto, which now watermarks without one.
const esri = (kind: string): string =>
  `${ESRI}World_${dark() ? "Dark" : "Light"}_Gray_${kind}/MapServer/tile/{z}/{y}/{x}`;

// The WBD renders boundary images and swaps HUC level by scale on its own, so the
// whole national grid is browsable without shipping geometry. `showLabels:false` strips
// the HUC code printed across every polygon; at city scale those are most of the ink.
const WBD_SPEC = [1, 2, 3, 4, 5, 6].map((id) => ({
  id,
  source: { type: "mapLayer", mapLayerId: id },
  drawingInfo: { showLabels: false },
}));
const WBD =
  "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer/export" +
  "?bbox={bbox-epsg-3857}&bboxSR=3857&imageSR=3857&size=256,256" +
  `&dynamicLayers=${encodeURIComponent(JSON.stringify(WBD_SPEC))}` +
  "&format=png32&transparent=true&dpi=96&f=image";

const EMPTY: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };

const raster = (tiles: string, attribution?: string) => ({
  type: "raster" as const,
  tiles: [tiles],
  tileSize: 256,
  maxzoom: 16,
  ...(attribution ? { attribution } : {}),
});

export interface MarkStyle {
  graded: boolean;
  depth: number | null;
}

export class MapController {
  private readonly map: MapLibreMap;
  private readonly tip: Popup;
  private styleReady = false;
  private queued: (() => void)[] = [];
  private gaugeMarker: Marker | null = null;

  constructor(container: HTMLElement, onPick: (lng: number, lat: number) => void) {
    this.map = new maplibregl.Map({
      container,
      center: [-98.5, 39.5],
      zoom: 2.1,
      minZoom: 1.4,
      maxZoom: 16,
      attributionControl: { compact: false },
      style: {
        version: 8,
        projection: { type: "globe" },
        sources: {
          base: raster(esri("Base"), "Esri, HERE, Garmin, &copy; OpenStreetMap contributors"),
          wbd: raster(WBD, "USGS Watershed Boundary Dataset"),
          labels: raster(esri("Reference")),
        },
        // Layer order replaces Leaflet's panes: basemap, boundaries, flood, outline,
        // marks, place names. Everything added later goes beforeId "labels".
        layers: [
          { id: "bg", type: "background", paint: { "background-color": css("--bg") } },
          { id: "base", type: "raster", source: "base" },
          { id: "wbd", type: "raster", source: "wbd", paint: { "raster-opacity": 0.5 } },
          { id: "labels", type: "raster", source: "labels" },
        ],
      },
    });

    this.map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "bottom-right");
    this.map.dragRotate.disable();
    this.map.touchZoomRotate.disableRotation();

    // One popup instance, moved between features - MapLibre has no bindTooltip.
    this.tip = new maplibregl.Popup({
      closeButton: false,
      closeOnClick: false,
      offset: 12,
      maxWidth: "280px",
      focusAfterOpen: false,
    });

    // Readiness comes from "styledata", not "load": "load" waits on a first render and
    // a backgrounded tab never renders, so a compute that finished while the tab was
    // hidden used to throw "Style is not done loading" from addSource.
    const markReady = () => {
      if (this.styleReady || !this.map.isStyleLoaded()) return;
      this.styleReady = true;
      this.sky();
      for (const fn of this.queued.splice(0)) fn();
    };
    this.map.on("styledata", markReady);
    this.map.on("load", markReady);
    // A pan leaves the pointer somewhere else entirely, and mouseleave never fires.
    this.map.on("movestart", () => this.tip.remove());
    this.map.on("click", (e) => {
      // A click on a mark opens its readout; only empty map picks a watershed.
      if (
        this.map.getLayer("hwm") &&
        this.map.queryRenderedFeatures(e.point, { layers: ["hwm"] }).length
      ) {
        return;
      }
      onPick(e.lngLat.lng, e.lngLat.lat);
    });
  }

  destroy(): void {
    this.map.remove();
  }

  resize(): void {
    this.map.resize();
  }

  /** Run once the style can take sources and layers, replaying if it cannot yet. */
  private whenReady(fn: () => void): void {
    if (this.styleReady) fn();
    else this.queued.push(fn);
  }

  /** The atmosphere is what makes the sphere read as a globe rather than a circle. It
   *  fades out on the way in, so at watershed scale nothing is tinted. */
  private sky(): void {
    this.map.setSky({
      "sky-color": dark() ? "#04141c" : "#a9c9dd",
      "horizon-color": dark() ? "#0d3242" : "#dbe8ef",
      "fog-color": dark() ? "#061216" : "#e9edee",
      "sky-horizon-blend": 0.6,
      "horizon-fog-blend": 0.5,
      "fog-ground-blend": 0.4,
      "atmosphere-blend": ["interpolate", ["linear"], ["zoom"], 0, 0.9, 5, 0.4, 7, 0],
    });
  }

  /** Re-tile for a light/dark change. */
  refreshBasemap(): void {
    if (!this.styleReady) return;
    (this.map.getSource("base") as maplibregl.RasterTileSource).setTiles([esri("Base")]);
    (this.map.getSource("labels") as maplibregl.RasterTileSource).setTiles([esri("Reference")]);
    this.map.setPaintProperty("bg", "background-color", css("--bg"));
    if (this.map.getLayer("outline-line")) {
      this.map.setPaintProperty("outline-line", "line-color", css("--accent"));
      this.map.setPaintProperty("outline-fill", "fill-color", css("--accent"));
    }
    this.sky();
  }

  /** Sources and layers are created once, then fed. Creating them lazily keeps the
   *  ordering explicit: everything sits under "labels" so place names stay legible. */
  private ensureLayers(): void {
    if (!this.styleReady || this.map.getSource("outline")) return;
    this.map.addSource("outline", { type: "geojson", data: EMPTY });
    this.map.addSource("hwm", { type: "geojson", data: EMPTY });
    this.map.addLayer(
      {
        id: "outline-fill",
        type: "fill",
        source: "outline",
        paint: { "fill-color": css("--accent"), "fill-opacity": 0.06 },
      },
      "labels",
    );
    this.map.addLayer(
      {
        id: "outline-line",
        type: "line",
        source: "outline",
        layout: { "line-join": "round" },
        paint: { "line-color": css("--accent"), "line-width": 2.2, "line-opacity": 0.95 },
      },
      "labels",
    );
    this.map.addLayer(
      {
        id: "hwm",
        type: "circle",
        source: "hwm",
        paint: {
          "circle-radius": ["get", "r"],
          "circle-color": ["get", "fill"],
          "circle-opacity": ["get", "alpha"],
          "circle-stroke-width": ["get", "sw"],
          "circle-stroke-color": ["get", "stroke"],
        },
      },
      "labels",
    );
    this.map.on("mousemove", "hwm", (e) => {
      this.map.getCanvas().style.cursor = "pointer";
      const f = e.features?.[0];
      if (!f || f.geometry.type !== "Point") return;
      this.tip
        .setLngLat(f.geometry.coordinates as [number, number])
        .setHTML(String(f.properties?.["html"] ?? ""))
        .addTo(this.map);
    });
    this.map.on("mouseleave", "hwm", () => {
      this.map.getCanvas().style.cursor = "";
      this.tip.remove();
    });
  }

  private setGeo(id: string, data: GeoJSON.GeoJSON): void {
    const source = this.map.getSource(id) as maplibregl.GeoJSONSource | undefined;
    source?.setData(data);
  }

  /** Frame a watershed, keeping it clear of the floating panels. Padding is measured
   *  from the panels themselves and clamped, because fitBounds rejects padding that
   *  exceeds the space available. */
  showOutline(geometry: GeoJSON.Geometry, padding: maplibregl.PaddingOptions): void {
    this.whenReady(() => {
      this.ensureLayers();
      this.setGeo("outline", { type: "Feature", geometry, properties: {} });
      this.map.fitBounds(bboxOf(geometry), { padding, maxZoom: 13, duration: 900 });
    });
  }

  clearModel(): void {
    this.whenReady(() => {
      if (this.map.getLayer("flood")) this.map.setLayoutProperty("flood", "visibility", "none");
      this.setGeo("hwm", EMPTY);
    });
    this.gaugeMarker?.remove();
    this.gaugeMarker = null;
    this.tip.remove();
  }

  showGauge(gauge: Gauge): void {
    const el = document.createElement("div");
    el.className = "gauge-dot";
    this.gaugeMarker = new maplibregl.Marker({ element: el })
      .setLngLat([gauge.lon, gauge.lat])
      .setPopup(
        new maplibregl.Popup({ closeButton: false, offset: 12 }).setHTML(
          `<div class="tt"><b>${gauge.site}</b><br><code>${gauge.name}</code><br>` +
            `<code>peak ${Math.round(gauge.discharge_cms).toLocaleString()} m³/s · ` +
            `${gauge.date} · ${gauge.n_years} yr record</code></div>`,
        ),
      )
      .addTo(this.map);
  }

  setBoundariesVisible(on: boolean): void {
    this.whenReady(() => this.map.setLayoutProperty("wbd", "visibility", on ? "visible" : "none"));
  }

  setDamageVisible(on: boolean): void {
    this.whenReady(() => {
      if (this.map.getLayer("damage")) {
        this.map.setLayoutProperty("damage", "visibility", on ? "visible" : "none");
      }
    });
  }

  /** Paint the depth raster for one stage field, and report what it covered. */
  paintFlood(
    bundle: FloodBundle,
    grid: Grid,
    stage: Float32Array,
  ): { wetCells: number; maxDepthTenths: number } {
    const canvas = document.createElement("canvas");
    canvas.width = bundle.width;
    canvas.height = bundle.height;
    const ctx = canvas.getContext("2d");
    if (!ctx) return { wetCells: 0, maxDepthTenths: 0 };
    const img = ctx.createImageData(bundle.width, bundle.height);
    const px = img.data;

    let wetCells = 0;
    let maxDepthTenths = 0;
    for (let p = 0; p < grid.hand.length; p++) {
      const depth = depthAtPixel(grid, stage, p);
      if (depth === null) continue;
      const tenths = depth * 10;
      wetCells++;
      if (tenths > maxDepthTenths) maxDepthTenths = tenths;
      const c = sample(DEPTH_RAMP, Math.sqrt(tenths / 80));
      const o = p * 4;
      px[o] = c[0];
      px[o + 1] = c[1];
      px[o + 2] = c[2];
      px[o + 3] = 214;
    }
    ctx.putImageData(img, 0, 0);
    const url = canvas.toDataURL();
    const coordinates = imageCorners(bundle.bounds);

    this.whenReady(() => {
      this.ensureLayers();
      const source = this.map.getSource("flood") as maplibregl.ImageSource | undefined;
      if (source) source.updateImage({ url, coordinates });
      else this.map.addSource("flood", { type: "image", url, coordinates });
      if (!this.map.getLayer("flood")) {
        this.map.addLayer(
          {
            id: "flood",
            type: "raster",
            source: "flood",
            paint: {
              "raster-opacity": 0.88,
              "raster-fade-duration": 0,
              "raster-resampling": "nearest",
            },
          },
          "outline-line",
        );
      }
      this.map.setLayoutProperty("flood", "visibility", "visible");
    });

    return { wetCells, maxDepthTenths };
  }

  /** One data-driven circle layer rather than a marker per point: on a globe the
   *  renderer handles occlusion behind the sphere for free, and 176 DOM nodes do not
   *  have to be created and destroyed on every slider frame. */
  drawMarks(marks: readonly Mark[], counted: ReadonlySet<Mark>, grid: Grid, stage: Float32Array): void {
    if (!marks.length) {
      this.whenReady(() => this.setGeo("hwm", EMPTY));
      return;
    }
    const features: GeoJSON.Feature[] = marks.map((m) => {
      const graded = counted.has(m);
      const depth = depthAtPixel(grid, stage, markPixel(grid, m));
      const residual = depth === null ? null : m.ground_m + depth - m.elev_m;
      const off = residual === null ? null : Math.abs(residual);
      return {
        type: "Feature",
        geometry: { type: "Point", coordinates: [m.lon, m.lat] },
        properties: {
          r: graded ? 5 : 3.2,
          sw: graded ? 1.5 : 1,
          fill:
            off === null
              ? "rgba(0,0,0,0)"
              : off <= 1
                ? css("--good")
                : off <= 3
                  ? css("--warn")
                  : css("--bad"),
          stroke: residual === null ? css("--ink-3") : "#ffffff",
          alpha: residual === null ? 0 : graded ? 1 : 0.45,
          html:
            `<div class="tt"><b>${m.event}</b>${m.event_date ? ` <code>${m.event_date}</code>` : ""}` +
            `<br><code>survey quality ${m.quality}${graded ? "" : " — not scored"}</code><br>` +
            `<code>surveyed ${m.elev_m.toFixed(2)} m · ground ${m.ground_m.toFixed(2)} m</code><br>` +
            (residual === null
              ? `<code>model: dry here</code>`
              : `<code>modelled ${(m.ground_m + (depth ?? 0)).toFixed(2)} m · ` +
                `${residual >= 0 ? "+" : ""}${residual.toFixed(2)} m</code>`) +
            `</div>`,
        },
      };
    });
    this.whenReady(() => {
      this.ensureLayers();
      this.setGeo("hwm", { type: "FeatureCollection", features });
    });
  }

  /** Recolour the four-channel damage image for the slider's position. */
  paintDamage(
    bounds: readonly [number, number, number, number],
    source: { d: Uint8ClampedArray; w: number; h: number },
    pick: ChannelPick | null,
    visible: boolean,
  ): void {
    const { d: src, w, h } = source;
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const img = ctx.createImageData(w, h);
    const px = img.data;

    for (let i = 0; i < w * h; i++) {
      const o = i * 4;
      let v: number;
      if (!pick) v = src[o] ?? 0;
      else if (pick.a < 0) v = (src[o + pick.b] ?? 0) * pick.t;
      else {
        const lo = src[o + pick.a] ?? 0;
        const hi = src[o + pick.b] ?? 0;
        v = lo + (hi - lo) * pick.t;
      }
      if (v < 1) {
        px[o + 3] = 0;
        continue;
      }
      const c = sample(DAMAGE_RAMP, (v - 1) / 254);
      px[o] = c[0];
      px[o + 1] = c[1];
      px[o + 2] = c[2];
      px[o + 3] = 225;
    }
    ctx.putImageData(img, 0, 0);
    const url = canvas.toDataURL();
    const coordinates = imageCorners(bounds);

    this.whenReady(() => {
      this.ensureLayers();
      const existing = this.map.getSource("damage") as maplibregl.ImageSource | undefined;
      if (existing) existing.updateImage({ url, coordinates });
      else this.map.addSource("damage", { type: "image", url, coordinates });
      if (!this.map.getLayer("damage")) {
        this.map.addLayer(
          {
            id: "damage",
            type: "raster",
            source: "damage",
            paint: {
              "raster-opacity": 0.92,
              "raster-fade-duration": 0,
              "raster-resampling": "nearest",
            },
          },
          "hwm",
        );
      }
      this.map.setLayoutProperty("damage", "visibility", visible ? "visible" : "none");
    });
  }
}
