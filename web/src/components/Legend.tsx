interface LegendProps {
  showDamageKey: boolean;
  showMarkKey: boolean;
}

export function Legend({ showDamageKey, showMarkKey }: LegendProps) {
  return (
    <section className="panel" id="legend">
      <div id="dmgkey" hidden={!showDamageKey}>
        <div className="eyebrow">Damage per cell</div>
        <div className="ramp dmg" />
        <div className="rampax">
          <span>1k</span>
          <span>100k</span>
          <span>10M</span>
          <span>100M+</span>
        </div>
        <div style={{ height: 11 }} />
      </div>
      <div className="eyebrow">Modelled depth</div>
      <div className="ramp" />
      <div className="rampax">
        <span>0</span>
        <span>0.5</span>
        <span>2</span>
        <span>4.5</span>
        <span>8 m+</span>
      </div>
      <div className="keyrow" id="hwmkey" hidden={!showMarkKey}>
        <span>
          <i style={{ background: "var(--good)" }} />
          surveyed mark within 1 m
        </span>
        <span>
          <i style={{ background: "var(--warn)" }} />
          1–3 m out
        </span>
        <span>
          <i style={{ background: "var(--bad)" }} />
          over 3 m out
        </span>
        <span>
          <i style={{ background: "transparent", boxShadow: "inset 0 0 0 1.5px var(--ink-3)" }} />
          left dry by the model
        </span>
      </div>
    </section>
  );
}
