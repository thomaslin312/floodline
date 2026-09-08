import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The build lands inside the Python package, because that is what ships: the wheel
// force-includes `src/floodline/web`, and `floodline serve` hands the file straight to
// FastAPI. Emptying the directory is off - `methodology.html` is hand-written and lives
// there too.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/floodline/web/dist",
    emptyOutDir: true,
  },
});
