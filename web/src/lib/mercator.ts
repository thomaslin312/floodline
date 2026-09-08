/** Web Mercator metres against lng/lat. The depth grid and the WBD export both speak
 *  EPSG:3857, so these conversions outlived the Leaflet build that supplied them. */
const R = 6378137;
const D = 180 / Math.PI;

export function mercToLngLat(x: number, y: number): [number, number] {
  return [(x / R) * D, (2 * Math.atan(Math.exp(y / R)) - Math.PI / 2) * D];
}

export function lngLatToMerc(lng: number, lat: number): [number, number] {
  return [(lng / D) * R, Math.log(Math.tan(Math.PI / 4 + lat / D / 2)) * R];
}

/** Corner-pinned coordinates for an image source: top-left, top-right, bottom-right,
 *  bottom-left. Getting this order wrong flips the raster, which looks plausible. */
export function imageCorners(
  bounds: readonly [number, number, number, number],
): [[number, number], [number, number], [number, number], [number, number]] {
  const [w, s, e, n] = bounds;
  const [wl, sl] = mercToLngLat(w, s);
  const [el, nl] = mercToLngLat(e, n);
  return [
    [wl, nl],
    [el, nl],
    [el, sl],
    [wl, sl],
  ];
}

/** Bounding box of any GeoJSON geometry, as [[west, south], [east, north]]. */
export function bboxOf(geometry: GeoJSON.Geometry): [[number, number], [number, number]] {
  let w = 180;
  let s = 90;
  let e = -180;
  let n = -90;
  const walk = (a: unknown): void => {
    if (Array.isArray(a) && typeof a[0] === "number" && typeof a[1] === "number") {
      const [x, y] = a as [number, number];
      if (x < w) w = x;
      if (x > e) e = x;
      if (y < s) s = y;
      if (y > n) n = y;
      return;
    }
    if (Array.isArray(a)) for (const c of a) walk(c);
  };
  walk("coordinates" in geometry ? geometry.coordinates : []);
  return [
    [w, s],
    [e, n],
  ];
}
