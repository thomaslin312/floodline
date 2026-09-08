import type { FloodBundle } from "../types/bundle";

/**
 * The line under the discharge readout: what this flow *is*, and how unusual.
 *
 * A depth map answers "how deep"; a reader asks "how unusual" first, and the annual
 * peak series is already fetched. The conditions matter more than they look — every
 * branch here is a different claim about where the number came from, and collapsing
 * them would mean captioning an assumed discharge the same way as a measured one.
 */
export function DischargeNote({
  bundle,
  multiplier,
}: {
  bundle: FloodBundle;
  multiplier: number;
}) {
  const gauge = bundle.gauge;
  const discharge = bundle.base_discharge_cms * multiplier;
  const mult = multiplier.toFixed(2);

  // Where this flow sits in the gauge's own record, shown only at the observed
  // discharge: at any other multiplier the flood being described is not one the
  // record has an opinion about.
  const history = gauge?.history;
  const atObserved = Math.abs(multiplier - 1) < 0.005;

  const events = [...new Set((bundle.marks ?? []).map((m) => m.event))].filter(Boolean);
  const marksAreThisFlood = Boolean(gauge?.event_discharge_cms);

  return (
    <div className="qnote" id="qnote">
      {bundle.gauged && gauge && gauge.event_discharge_cms ? (
        <>
          {mult}× the <b>{gauge.event_year}</b> peak at <b>{gauge.site}</b> —{" "}
          {Math.round(gauge.event_discharge_cms).toLocaleString()} m³/s on {gauge.event_date},
          matched to the {gauge.matched_marks} marks surveyed after that flood. Its peak of record
          is {Math.round(gauge.discharge_cms).toLocaleString()} m³/s ({gauge.date}).
        </>
      ) : bundle.gauged && gauge ? (
        <>
          {mult}× the peak of record at <b>{gauge.site}</b> —{" "}
          {Math.round(gauge.discharge_cms).toLocaleString()} m³/s on {gauge.date}, from{" "}
          {gauge.n_years} years
          {gauge.n_years < 15 ? <span style={{ color: "var(--warn)" }}> · short record</span> : null}
        </>
      ) : (
        <>
          {mult}× a severe-flood scenario of {(discharge / (bundle.area_km2 || 1)).toFixed(1)} m³/s
          per km² — no gauge in this basin, so this is assumed, not measured
        </>
      )}

      {history && atObserved ? (
        <>
          <br />
          <span style={{ color: "var(--ink-2)" }}>
            {/* The summary is written once and read in two places: a terminal, where
                m3/s is the safe spelling, and this panel, where the line beside it
                already says m³/s. Fixing the symbol here keeps the model's string
                portable and the page's typography consistent. */}
            {history.summary.replace(/\bm3\/s\b/g, "m³/s")}
            {history.fit_saturated
              ? " The fitted distribution cannot place it — a century of urbanisation upstream breaks the stationarity that fit assumes."
              : ""}
          </span>
          {history.larger_floods?.length ? (
            <>
              <br />
              <span style={{ color: "var(--ink-3)" }}>
                Larger on record:{" "}
                {history.larger_floods
                  .map((f) => `${f.water_year} (${f.cms.toLocaleString()} m³/s)`)
                  .join(", ")}
                .
              </span>
            </>
          ) : null}
        </>
      ) : null}

      {/* Say plainly when the marks and the discharge come from different floods. */}
      {events.length && !marksAreThisFlood ? (
        <>
          <br />
          <span style={{ color: "var(--warn)" }}>
            Marks here are from {events.slice(0, 2).join(", ")}
            {events.length > 2 ? " and others" : ""}, which is not the flood driving this model —
            the residual mixes two events.
          </span>
        </>
      ) : events.length ? (
        <>
          <br />
          Marks: {events.slice(0, 2).join(", ")}
          {events.length > 2 ? ` and ${events.length - 2} more` : ""}.
        </>
      ) : null}
    </div>
  );
}
