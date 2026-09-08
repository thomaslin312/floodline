import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import "maplibre-gl/dist/maplibre-gl.css";
import "./styles.css";

/* The globe is rendered on the GPU, so unlike the raster-tile map it replaced there is
   no software fallback. Say so plainly rather than leaving a blank rectangle, and say
   where the same answers are still available. */
const root = document.getElementById("root");
if (!root) throw new Error("no #root to mount into");

if (!document.createElement("canvas").getContext("webgl2")) {
  root.innerHTML =
    '<section class="panel" id="info"><div class="status bad">' +
    "This browser has no WebGL2, which the globe needs to draw. Everything is computed " +
    "server-side, so the API at <code>/api/compute/{huc}</code> still works." +
    "</div></section>";
} else {
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
