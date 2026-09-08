/**
 * What the server actually sends.
 *
 * These are the shapes the old page read out of untyped JSON, and they are where its
 * bugs lived: `stats.ladder` against `reference_multipliers` against `per_building`
 * keyed by a multiplier that was not the rung it looked like. Writing them down is
 * most of the reason to have TypeScript here at all.
 *
 * Optional means optional on the wire, not "probably there". A watershed with no gauge
 * has no `gauge`; a run that could not reach NSI has no `ladder`. The types say so, so
 * the compiler asks about it.
 */

export interface Watershed {
  huc: string;
  name: string | null;
  area_km2: number;
  analysis_crs: string;
  geometry: GeoJSON.Geometry;
  cells_at: Record<string, number>;
  too_big_at_10m?: boolean;
  /** Sites the NWIS bbox query returned. Zero proves there is no gauge; non-zero
   *  promises nothing, because a site still has to snap to the network and carry a
   *  peak record. `null` means the lookup itself failed. */
  gauges_in_bbox: number | null;
}

export interface Gauge {
  site: string;
  name: string;
  lon: number;
  lat: number;
  discharge_cms: number;
  date: string;
  n_years: number;
  /** Present only when surveyed marks agreed on a year and the model was driven by
   *  that flood's peak instead of the record's. */
  event_discharge_cms?: number;
  event_year?: number;
  event_date?: string;
  matched_marks?: number;
  history?: GaugeHistory;
}

export interface GaugeHistory {
  summary: string;
  fit_saturated?: boolean;
  larger_floods?: { water_year: number; cms: number }[];
}

export interface Mark {
  lon: number;
  lat: number;
  elev_m: number;
  ground_m: number;
  quality: number;
  event: string;
  event_date?: string;
  environment?: string;
}

export interface FloodBundle {
  huc: string;
  width: number;
  height: number;
  /** Web Mercator metres, [west, south, east, north]. */
  bounds: [number, number, number, number];
  n_reaches: number;
  /** Block-reduction from the analysis grid to the display grid. */
  reduction: number;
  multipliers: number[];
  base_discharge_cms: number;
  area_km2?: number;
  gauged: boolean;
  gauge?: Gauge;
  marks?: Mark[];
  cached?: boolean;
  tiles_read?: number;
  seconds?: Record<string, number>;
  /** Data URLs: HAND in the red channel, reach id across red and green, and the
   *  per-reach stage table as one row per reach and one column per rung. */
  hand: string;
  reach: string;
  stage_table: string;
  stats: { resolution_m?: number; valid_km2?: number };
}

/** Damage at each rung of the same multiplier ladder the stage table uses. */
export interface Ladder {
  multipliers: number[];
  discharge_cms: number[];
  damage: number[];
  structure: number[];
  contents: number[];
  inundated: number[];
  residents: number[];
  /** Damage from structures standing on a drainage cell. Absent on bundles computed
   *  before that was measured, which is why every reader has to tolerate it missing. */
  in_channel?: number[];
  max_in_channel_share?: number;
}

export interface ExposureStats {
  structures?: number;
  inundated?: number;
  inundated_low?: number;
  inundated_high?: number;
  night_population?: number;
  damage?: number;
  exposed_value?: number;
  ladder?: Ladder;
}

export interface ExposureBundle {
  huc: string;
  bounds: [number, number, number, number];
  currency?: string;
  /** Four channels, one per reference multiplier, each log-scaled damage per cell. */
  damage_png?: string;
  reference_multipliers?: number[];
  stats?: ExposureStats;
  gaps?: string[];
}

export interface GeocodeHit {
  lon: number;
  lat: number;
  label: string;
}
