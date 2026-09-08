import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { DischargeNote } from "./components/DischargeNote";
import { ExposurePanel } from "./components/ExposurePanel";
import { Legend } from "./components/Legend";
import { SearchPanel, type LayerToggles } from "./components/SearchPanel";
import { StatsGrid, type Stat } from "./components/StatsGrid";
import { TOUR_SEEN, Tour } from "./components/Tour";
import { api } from "./lib/api";
import { damageAt } from "./lib/damage";
import { agreementAt, scorableMarks, type Grid } from "./lib/grid";
import { decodeGrids, pixels, type Pixels } from "./lib/pixels";
import { stageAt } from "./lib/stage";
import { MapController } from "./map/controller";
import type {
  ExposureBundle,
  FloodBundle,
  GeocodeHit,
  Mark,
  Watershed,
} from "./types/bundle";

type Tone = "" | "warn" | "bad";

interface Status {
  text: React.ReactNode;
  tone: Tone;
}

/** Everything decoded from one computed bundle, kept together because a half-swapped
 *  set would paint one watershed's depth through another's stage table. */
interface Model {
  bundle: FloodBundle;
  grid: Grid;
  lut: Uint8Array;
  nMult: number;
}

export function App() {
  const mapNode = useRef<HTMLDivElement>(null);
  const controller = useRef<MapController | null>(null);

  const [unit, setUnit] = useState<Watershed | null>(null);
  const [unitLabel, setUnitLabel] = useState<string | null>(null);
  const [model, setModel] = useState<Model | null>(null);
  const [expo, setExpo] = useState<ExposureBundle | null>(null);
  const damagePixels = useRef<Pixels | null>(null);

  const [level, setLevel] = useState(12);
  const [sliderPct, setSliderPct] = useState(100);
  const [status, setStatus] = useState<Status>({
    text: "Ready.",
    tone: "",
  });
  const [progress, setProgress] = useState(0);
  const [busy, setBusy] = useState(false);
  const [expoSeconds, setExpoSeconds] = useState<number | null>(null);
  const [expoError, setExpoError] = useState<string | null>(null);
  const [tourAt, setTourAt] = useState<number | null>(null);

  const [toggles, setToggles] = useState<LayerToggles>({
    boundaries: true,
    marks: true,
    gradedOnly: true,
    hiRes: false,
    damage: false,
  });

  const multiplier = sliderPct / 100;

  /* ---------------- the map, created once ---------------- */
  useEffect(() => {
    if (!mapNode.current || controller.current) return;
    const c = new MapController(mapNode.current, (lng, lat) => void pick(lng, lat, null));
    controller.current = c;
    const observer = new ResizeObserver(() => c.resize());
    observer.observe(mapNode.current);
    const scheme = window.matchMedia("(prefers-color-scheme: dark)");
    const onScheme = () => c.refreshBasemap();
    scheme.addEventListener("change", onScheme);
    return () => {
      observer.disconnect();
      scheme.removeEventListener("change", onScheme);
      c.destroy();
      controller.current = null;
    };
    // Created once for the life of the page; `pick` is stable enough via the ref.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!localStorage.getItem(TOUR_SEEN)) setTourAt(0);
  }, []);

  useEffect(() => {
    controller.current?.setBoundariesVisible(toggles.boundaries);
  }, [toggles.boundaries]);

  useEffect(() => {
    controller.current?.setDamageVisible(toggles.damage && !!expo);
  }, [toggles.damage, expo]);

  /* ---------------- derived: stage, marks, agreement ---------------- */
  const stage = useMemo(() => {
    if (!model) return null;
    return stageAt(model.lut, model.nMult, model.grid.nReaches, model.bundle.multipliers, multiplier);
  }, [model, multiplier]);

  const counted = useMemo(() => {
    const marks = model?.bundle.marks ?? [];
    return new Set<Mark>(scorableMarks(marks, toggles.gradedOnly));
  }, [model, toggles.gradedOnly]);

  /* ---------------- painting, driven by state ---------------- */
  const [coverage, setCoverage] = useState({ wetCells: 0, maxDepthTenths: 0 });

  useEffect(() => {
    const c = controller.current;
    if (!c || !model || !stage) return;
    setCoverage(c.paintFlood(model.bundle, model.grid, stage));
    const marks = toggles.marks ? (model.bundle.marks ?? []) : [];
    c.drawMarks(marks, counted, model.grid, stage);
  }, [model, stage, counted, toggles.marks]);

  useEffect(() => {
    const c = controller.current;
    if (!c || !expo || !damagePixels.current) return;
    const pick = damageAt(expo.reference_multipliers ?? [], multiplier);
    c.paintDamage(expo.bounds, damagePixels.current, pick, toggles.damage);
  }, [expo, multiplier, toggles.damage]);

  /* ---------------- selecting a watershed ---------------- */
  const clear = useCallback(() => {
    controller.current?.clearModel();
    setModel(null);
    setExpo(null);
    setExpoError(null);
    damagePixels.current = null;
    setToggles((t) => ({ ...t, damage: false }));
    // The slider is a multiplier, so it goes back to 1.00× with the model it
    // described. Carrying a best-fit multiplier into the next watershed opens it at a
    // discharge that means nothing there.
    setSliderPct(100);
  }, []);

  const showUnit = useCallback(
    (next: Watershed, label: string | null) => {
      clear();
      setUnit(next);
      setUnitLabel(label);
      controller.current?.showOutline(next.geometry, fitPadding());
      const cells = next.cells_at["10"] ?? 0;
      if (next.gauges_in_bbox === 0) {
        setStatus({
          tone: "warn",
          text: "No USGS discharge gauge in this basin. The depth map will run from an assumed severe-flood scenario, and exposure and damage will not — pricing buildings needs a measured discharge.",
        });
      } else if (next.too_big_at_10m) {
        setStatus({
          tone: "warn",
          text: `${(cells / 1e6).toFixed(0)}M cells at 10 m — too large for one pass, so this runs at 30 m.`,
        });
      } else {
        setStatus({
          tone: "",
          text: `Ready. 30 m by default; 10 m is ${(cells / 1e6).toFixed(1)}M cells and slower.`,
        });
      }
    },
    [clear],
  );

  const pick = useCallback(
    async (lon: number, lat: number, label: string | null) => {
      setStatus({ tone: "", text: <Spinner>Identifying watershed…</Spinner> });
      setProgress(0.3);
      try {
        const found = await api<Watershed>(`/api/watershed?lon=${lon}&lat=${lat}&level=${level}`);
        showUnit(found, label);
      } catch (e) {
        setStatus({ tone: "bad", text: `No watershed there: ${message(e)}` });
      } finally {
        setProgress(1);
      }
    },
    [level, showUnit],
  );

  const search = useCallback(
    async (text: string) => {
      const q = text.trim();
      if (!q) return;
      if (/^\d+$/.test(q) && q.length >= 8 && q.length % 2 === 0) {
        setStatus({ tone: "", text: <Spinner>Looking up HUC…</Spinner> });
        try {
          showUnit(await api<Watershed>(`/api/watershed/${q}`), "by HUC");
        } catch (e) {
          setStatus({ tone: "bad", text: `No such HUC: ${message(e)}` });
        }
        return;
      }
      setStatus({ tone: "", text: <Spinner>Geocoding…</Spinner> });
      try {
        const hit = await api<GeocodeHit>(`/api/geocode?q=${encodeURIComponent(q)}`);
        await pick(hit.lon, hit.lat, hit.label);
      } catch (e) {
        setStatus({ tone: "bad", text: `Could not find that: ${message(e)}` });
      }
    },
    [pick, showUnit],
  );

  /* ---------------- computing ---------------- */
  const runExposure = useCallback(
    async (huc: string, resolution: number) => {
      setExpoError(null);
      const started = performance.now();
      const tick = window.setInterval(
        () => setExpoSeconds(Math.round((performance.now() - started) / 1000)),
        300,
      );
      try {
        // Same resolution as the model on screen. Pricing damage off a 30 m depth
        // raster while the map draws a 10 m one would put the layers on different
        // ground.
        const bundle = await api<ExposureBundle>(
          `/api/exposure/${huc}?resolution=${resolution}`,
        );
        damagePixels.current = bundle.damage_png ? await pixels(bundle.damage_png) : null;
        setExpo(bundle);
        setToggles((t) => ({ ...t, damage: true }));
      } catch (e) {
        setExpoError(message(e));
      } finally {
        window.clearInterval(tick);
        setExpoSeconds(null);
      }
    },
    [],
  );

  const run = useCallback(async () => {
    if (!unit || busy) return;
    setBusy(true);
    const resolution = toggles.hiRes && !unit.too_big_at_10m ? 10 : 30;
    const started = performance.now();
    let p = 0.05;
    const tick = window.setInterval(() => {
      p = Math.min(0.92, p + 0.03);
      setProgress(p);
      setStatus({
        tone: "",
        text: (
          <Spinner>
            Computing at {resolution} m — reading elevation tiles…{" "}
            {((performance.now() - started) / 1000).toFixed(0)}s
          </Spinner>
        ),
      });
    }, 300);
    try {
      const bundle = await api<FloodBundle>(`/api/compute/${unit.huc}?resolution=${resolution}`);
      window.clearInterval(tick);
      setProgress(0.96);
      const [h, r, t] = await Promise.all([
        pixels(bundle.hand),
        pixels(bundle.reach),
        pixels(bundle.stage_table),
      ]);
      const decoded = decodeGrids(h, r, t, bundle.width, bundle.height);
      setModel({
        bundle,
        lut: decoded.lut,
        nMult: decoded.nMult,
        grid: {
          hand: decoded.hand,
          reach: decoded.reach,
          width: bundle.width,
          height: bundle.height,
          bounds: bundle.bounds,
          nReaches: bundle.n_reaches,
        },
      });
      if (bundle.gauge) controller.current?.showGauge(bundle.gauge);
      const seconds = bundle.seconds ?? {};
      setStatus({
        tone: "",
        text: bundle.cached
          ? "Loaded from cache."
          : `Done in ${((performance.now() - started) / 1000).toFixed(1)}s · ${bundle.tiles_read} elevation tiles · terrain ${(seconds["terrain"] ?? 0).toFixed(1)}s`,
      });
      setProgress(1);
      // Exposure is part of the answer, not an upsell, so it runs here rather than
      // behind a second button. It is the slower half, which is why depth is drawn
      // first and this fills in behind it.
      if (bundle.gauged) void runExposure(unit.huc, resolution);
    } catch (e) {
      window.clearInterval(tick);
      setProgress(1);
      setStatus({ tone: "bad", text: `Compute failed: ${message(e)}` });
    } finally {
      setBusy(false);
    }
  }, [busy, runExposure, toggles.hiRes, unit]);

  /* ---------------- the figures ---------------- */
  const stats: Stat[] = useMemo(() => {
    if (!model || !stage) return [];
    const { bundle, grid } = model;
    const cellKm2 = Math.pow(bundle.reduction * (bundle.stats.resolution_m ?? 10), 2) / 1e6;
    const extent = coverage.wetCells * cellKm2;
    const marks = scorableMarks(bundle.marks ?? [], toggles.gradedOnly);
    const agreement = agreementAt(grid, stage, marks);
    const rmse = agreement?.rmse ?? null;
    const tone = rmse === null ? "na" : rmse <= 1.5 ? "good" : rmse <= 3 ? "warn" : "bad";
    return [
      { key: "Extent", value: `${extent.toFixed(1)} km²` },
      {
        key: "Of basin",
        value: bundle.stats.valid_km2
          ? `${Math.round((extent / bundle.stats.valid_km2) * 100)}%`
          : "—",
      },
      { key: "Max depth", value: `${(coverage.maxDepthTenths / 10).toFixed(1)} m` },
      { key: "Resolution", value: `${bundle.stats.resolution_m ?? 10} m` },
      {
        key: "Marks",
        value: agreement ? `${agreement.wet} / ${agreement.n} wet` : "none scored",
        tone: agreement ? "" : "na",
      },
      { key: "RMSE", value: rmse === null ? "—" : `${rmse.toFixed(2)} m`, tone },
    ];
  }, [model, stage, coverage, toggles.gradedOnly]);

  const hasMarks = Boolean(model?.bundle.marks?.length) && toggles.marks;

  return (
    <>
      <div id="map" ref={mapNode} />
      <div className="bar" id="bar" style={{ width: `${progress * 100}%` }} />

      {tourAt !== null ? (
        <Tour
          at={tourAt}
          onStep={setTourAt}
          onClose={() => {
            setTourAt(null);
            try {
              localStorage.setItem(TOUR_SEEN, "1");
            } catch {
              /* private window: show it again next time */
            }
          }}
        />
      ) : null}

      <SearchPanel
        level={level}
        onLevel={(l) => {
          setLevel(l);
          setStatus({ tone: "", text: "Level changed — click the map to pick at this level." });
        }}
        toggles={toggles}
        onToggle={(key, value) => setToggles((t) => ({ ...t, [key]: value }))}
        onSearch={(t) => void search(t)}
        onHelp={() => setTourAt(0)}
      />

      <section className="panel" id="info">
        <div className="eyebrow">Watershed</div>
        <h2 className="name" id="uname">
          {unit ? (unit.name ?? "Unnamed watershed") : "Click any watershed"}
        </h2>
        <div className="meta" id="umeta">
          {unit ? (
            <>
              HUC-{unit.huc.length} <code>{unit.huc}</code> · {unit.area_km2.toLocaleString()} km² ·{" "}
              {unit.analysis_crs}
              {unitLabel ? ` · ${unitLabel}` : ""}
            </>
          ) : (
            "The boundary layer shows every hydrologic unit in the country. Zoom in for finer ones."
          )}
        </div>
        <div className={`status${status.tone ? ` ${status.tone}` : ""}`} id="status">
          {status.text}
        </div>
        <button className="wide" id="run" disabled={!unit || busy} onClick={() => void run()}>
          Compute this watershed
        </button>

        {model ? (
          <div id="model">
            <div className="qhead">
              <div>
                <div className="eyebrow">Discharge at outlet</div>
                <span className="qval" id="qval">
                  {Math.round(model.bundle.base_discharge_cms * multiplier).toLocaleString()}
                </span>{" "}
                <span className="qunit">m³/s</span>
              </div>
              <span className={`src ${model.bundle.gauged ? "obs" : "est"}`} id="qsrc">
                {model.bundle.gauged ? "observed" : "estimated"}
              </span>
            </div>
            <input
              type="range"
              id="qs"
              min={0}
              max={300}
              step={2}
              value={sliderPct}
              style={{ "--pct": `${(sliderPct / 300) * 100}%` } as React.CSSProperties}
              onChange={(e) => setSliderPct(Number(e.target.value))}
            />
            <DischargeNote bundle={model.bundle} multiplier={multiplier} />
            <StatsGrid id="stats" stats={stats} />

            <ExposurePanel
              expo={expo}
              multiplier={multiplier}
              ungauged={!model.bundle.gauged}
              busySeconds={expoSeconds}
              error={expoError}
              canRetry={Boolean(expoError) && model.bundle.gauged}
              onRetry={() =>
                unit
                  ? void runExposure(unit.huc, model.bundle.stats.resolution_m ?? 30)
                  : undefined
              }
            />
          </div>
        ) : null}
      </section>

      <Legend showDamageKey={toggles.damage && !!expo} showMarkKey={hasMarks} />
    </>
  );
}

function Spinner({ children }: { children: React.ReactNode }) {
  return (
    <>
      <span className="spin" />
      {children}
    </>
  );
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** The panels float over the map, so fitting to the full viewport can leave the whole
 *  watershed behind one of them. Padding is measured from the panels and clamped,
 *  because fitBounds rejects padding larger than the space available. */
function fitPadding() {
  const map = document.getElementById("map")?.getBoundingClientRect();
  const search = document.getElementById("search")?.getBoundingClientRect();
  const info = document.getElementById("info")?.getBoundingClientRect();
  if (!map || !search || !info) return { top: 32, bottom: 32, left: 32, right: 32 };
  const cap = (v: number, span: number) => Math.max(24, Math.min(v, span * 0.42));
  return window.innerWidth <= 900
    ? {
        top: cap(search.height + 32, map.height),
        bottom: cap(info.height + 24, map.height),
        left: 24,
        right: 24,
      }
    : {
        top: 32,
        bottom: 32,
        left: cap(search.width + 40, map.width),
        right: cap(info.width + 40, map.width),
      };
}
