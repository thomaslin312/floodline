import base64
import json
from pathlib import Path

VIZ = Path("outputs/viz")
OUT = Path(
    "/private/tmp/claude-501/-Users-thomaslin-Downloads-Floodline/34bfa88f-eb53-4f82-a40b-6ee0b301ae79/scratchpad/harvey_watershed.html"
)
d = json.loads((VIZ / "layers.json").read_text())

uris = {}
for layer in d["layers"]:
    raw = (VIZ / layer["file"]).read_bytes()
    uris[layer["name"]] = "data:image/png;base64," + base64.b64encode(raw).decode()

payload = {
    "width": d["width"],
    "height": d["height"],
    "reduction": d["reduction"],
    "bounds": d["bounds"],
    "crs": d["crs"],
    "notes": d["notes"],
    "layers": [{k: v for k, v in L.items() if k != "file"} for L in d["layers"]],
    "marks": d["points"]["high_water_marks"],
    "gauge": d["points"]["gauge"],
    "curves": d["series"]["rating_curves"],
    "uris": uris,
}
n = d["notes"]
OUT.write_text(
    TEMPLATE := (Path(__file__).parent / "page_template.html")
    .read_text()
    .replace("__PAYLOAD__", json.dumps(payload))
    .replace("__HUC__", n["huc"])
    .replace("__NAME__", n["name"])
)
print(f"wrote {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")
