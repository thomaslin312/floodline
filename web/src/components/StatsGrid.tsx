export interface Stat {
  key: string;
  value: string;
  tone?: "good" | "warn" | "bad" | "na" | "";
}

/** The figure grid both panels use. Values are already formatted: the caller knows
 *  what a withheld number looks like, this does not. */
export function StatsGrid({ id, stats, hidden }: { id: string; stats: Stat[]; hidden?: boolean }) {
  return (
    <dl className="grid" id={id} hidden={hidden}>
      {stats.map((s) => (
        <div key={s.key}>
          <dt>{s.key}</dt>
          <dd className={s.tone ?? ""}>{s.value}</dd>
        </div>
      ))}
    </dl>
  );
}
