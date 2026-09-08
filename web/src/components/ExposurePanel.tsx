import { ladderAt } from "../lib/ladder";
import { money } from "../lib/format";
import type { ExposureBundle } from "../types/bundle";
import { StatsGrid, type Stat } from "./StatsGrid";

interface ExposurePanelProps {
  /** Null while nothing has been priced yet. */
  expo: ExposureBundle | null;
  multiplier: number;
  /** Set when the basin has no gauge, so nothing can be priced at all. */
  ungauged: boolean;
  busySeconds: number | null;
  error: string | null;
  onRetry: () => void;
  canRetry: boolean;
}

export function ExposurePanel({
  expo,
  multiplier,
  ungauged,
  busySeconds,
  error,
  onRetry,
  canRetry,
}: ExposurePanelProps) {
  const stats = expo?.stats ?? {};
  const unit = expo?.currency ?? "USD";
  const at = ladderAt(stats.ladder, multiplier);

  // Without a ladder the panel still has the run's own totals, computed at the
  // observed discharge. It just cannot move with the slider.
  const now = at ?? {
    damage: stats.damage ?? null,
    inundated: stats.inundated ?? 0,
    residents: stats.night_population ?? null,
    reportable: true,
    inChannelShare: 0,
  };

  const ratio = stats.exposed_value && now.damage != null ? now.damage / stats.exposed_value : null;
  const priced = now.reportable !== false;

  const rows: Stat[] = [
    { key: "Structures", value: (stats.structures ?? 0).toLocaleString() },
    { key: "Inundated", value: Math.round(now.inundated ?? 0).toLocaleString() },
    {
      key: "Interval",
      value: `${(stats.inundated_low ?? 0).toLocaleString()}–${(stats.inundated_high ?? 0).toLocaleString()}`,
    },
    {
      key: "Residents",
      value: now.residents != null ? Math.round(now.residents).toLocaleString() : "—",
    },
    { key: "Damage", value: priced ? money(now.damage, unit) : "—", tone: priced ? "" : "na" },
    {
      key: "Loss ratio",
      value: priced && ratio != null ? `${(ratio * 100).toFixed(1)}%` : "—",
      tone: priced ? "" : "na",
    },
  ];

  return (
    <div id="exposure">
      <div className="eyebrow" style={{ marginTop: 14 }}>
        Exposure and damage
      </div>

      {canRetry ? (
        <button className="wide quiet" id="expo-run" onClick={onRetry}>
          Try valuing the buildings again
        </button>
      ) : null}

      <div className="qnote" id="expo-note">
        {busySeconds != null ? (
          <>
            <span className="spin" />
            Reading the structure inventory and pricing damage… {busySeconds}s
          </>
        ) : error ? (
          <span style={{ color: "var(--bad)" }}>Exposure failed: {error}</span>
        ) : ungauged ? (
          <>
            <b style={{ color: "var(--warn)" }}>No gauge in this basin.</b> The depth above comes
            from an assumed severe-flood scenario, not a measurement, so there is no observed
            discharge to price. Exposure and damage are withheld rather than computed from an
            assumption: a building count and a dollar total read as measurements whatever the
            caption says. The depth map, the marks and the discharge slider all still work.
          </>
        ) : expo ? (
          <ExposureCaveat priced={priced} share={now.inChannelShare} gaps={expo.gaps ?? []} />
        ) : null}
      </div>

      {expo && !error ? <StatsGrid id="expo-stats" stats={rows} /> : null}
    </div>
  );
}

/**
 * What is left after the numbers: only what is conditional and cannot be inferred from
 * them. The currency total is the one output with a failed validation behind it, and
 * the map has to say so or the figure gets screenshotted without the caveat that lives
 * three clicks away. Below roughly bankfull it is withheld outright instead — a number
 * on screen gets quoted, and one that is absent cannot be.
 */
function ExposureCaveat({
  priced,
  share,
  gaps,
}: {
  priced: boolean;
  share: number;
  gaps: readonly string[];
}) {
  return (
    <>
      {priced ? (
        <span style={{ color: "var(--warn)" }}>
          Counts and loss ratio are validated; <b>the currency total is not</b>.{" "}
          <a href="/methodology#accuracy" target="_blank" rel="noopener">
            Why
          </a>
          .
        </span>
      ) : (
        <span style={{ color: "var(--warn)" }}>
          <b>Damage withheld at this discharge.</b> {(share * 100).toFixed(0)}% of it would come
          from structures the model places in the channel itself, where any flow at all puts water
          on them. Extent, depth and counts still stand.{" "}
          <a href="/methodology#accuracy" target="_blank" rel="noopener">
            Why
          </a>
          .
        </span>
      )}
      {gaps.map((g) => (
        <span key={g} style={{ color: "var(--warn)" }}>
          <br />
          {g}
        </span>
      ))}
    </>
  );
}
