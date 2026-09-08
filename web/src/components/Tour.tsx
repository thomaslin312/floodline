import { useCallback, useEffect } from "react";

import { Wordmark } from "./Wordmark";

/**
 * Four steps, because a reader who has to be told five things about a map will read
 * none of them. Each names a thing on screen and what it means, and stops there.
 *
 * It used to carry a caveat under every step - what the model cannot tell you, what
 * RMSE means, what a replacement cost is and is not. Those are all still true and all
 * still stated: on the methodology page, which is one click away and is where someone
 * who wants them will look. A tour that argues with itself at every step is one nobody
 * finishes, and an unfinished tour teaches none of it.
 */
interface Step {
  title: string;
  art: React.ReactNode;
  body: React.ReactNode;
}

const TOUR: Step[] = [
  {
    title: "A flood model for any US watershed",
    art: (
      <svg viewBox="0 0 360 74" aria-hidden="true">
        <path
          d="M10 58c26 0 26-16 52-16s26 16 52 16 26-16 52-16 26 16 52 16 26-16 52-16 26 16 52 16"
          fill="none"
          stroke="var(--accent)"
          strokeWidth="2"
          strokeLinecap="round"
          opacity=".55"
        />
        <path
          d="M10 42c26 0 26-16 52-16s26 16 52 16 26-16 52-16 26 16 52 16 26-16 52-16 26 16 52 16"
          fill="none"
          stroke="var(--accent)"
          strokeWidth="2"
          strokeLinecap="round"
        />
        <path
          d="M96 26 130 6l34 20"
          fill="none"
          stroke="var(--ink-3)"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <path
          d="M196 26 230 6l34 20"
          fill="none"
          stroke="var(--ink-3)"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          opacity=".5"
        />
      </svg>
    ),
    body: (
      <>
        <p>
          Terrain from USGS lidar, discharge from a river gauge, and every structure&rsquo;s value
          from the USACE National Structure Inventory. Type a place and it computes the flood live,
          with no dataset to download.
        </p>
      </>
    ),
  },
  {
    title: "Start with a place",
    art: (
      <svg viewBox="0 0 360 74" aria-hidden="true">
        <rect x="8" y="14" width="248" height="30" rx="7" fill="var(--solid)" stroke="var(--hair)" />
        <text x="22" y="34" fill="var(--ink-3)" fontSize="13" fontFamily="system-ui, sans-serif">
          77008
        </text>
        <rect x="264" y="14" width="60" height="30" rx="7" fill="var(--accent)" />
        <text
          x="280"
          y="34"
          fill="var(--on-accent)"
          fontSize="12.5"
          fontWeight="600"
          fontFamily="system-ui, sans-serif"
        >
          Find
        </text>
        <text x="8" y="64" fill="var(--ink-3)" fontSize="11.5" fontFamily="system-ui, sans-serif">
          ZIP · street address · HUC code · or click the map
        </text>
      </svg>
    ),
    body: (
      <>
        <p>
          A ZIP code, a full street address, a hydrologic unit code, or a click anywhere on the
          globe.
        </p>
        <p>
          <b>Click picks</b> sets how much ground a click gives you. A HUC-12 is a
          neighbourhood-sized subwatershed, usually 70 to 150 km², and computes in around twenty
          seconds. A HUC-10 runs from a couple of hundred to about fifteen hundred km², and is the
          size every accuracy figure quoted here was measured on. A HUC-8 is a whole river basin,
          several thousand km², which is a lot of terrain to route and correspondingly slower.
        </p>
      </>
    ),
  },
  {
    title: "Compute the flood, then check it",
    art: (
      <svg viewBox="0 0 360 74" aria-hidden="true">
        <defs>
          <linearGradient id="tg1" x1="0" x2="1">
            <stop offset="0" stopColor="#cfe6f2" />
            <stop offset=".45" stopColor="#3d8fc0" />
            <stop offset="1" stopColor="#062242" />
          </linearGradient>
        </defs>
        <rect x="8" y="14" width="200" height="9" rx="4.5" fill="url(#tg1)" />
        <text x="8" y="40" fill="var(--ink-3)" fontSize="11" fontFamily="ui-monospace, monospace">
          0 m
        </text>
        <text x="180" y="40" fill="var(--ink-3)" fontSize="11" fontFamily="ui-monospace, monospace">
          8 m+
        </text>
        <circle cx="240" cy="19" r="6" fill="var(--good)" stroke="#fff" strokeWidth="1.5" />
        <circle cx="266" cy="19" r="6" fill="var(--warn)" stroke="#fff" strokeWidth="1.5" />
        <circle cx="292" cy="19" r="6" fill="var(--bad)" stroke="#fff" strokeWidth="1.5" />
        <text x="228" y="40" fill="var(--ink-3)" fontSize="11" fontFamily="system-ui, sans-serif">
          surveyed marks
        </text>
      </svg>
    ),
    body: (
      <>
        <p>
          Blue is modelled water depth. The dots are real high-water marks surveyed by the USGS
          after the flood, coloured by how far the model missed them: green within a metre, red over
          three.
        </p>
      </>
    ),
  },
  {
    title: "Price it, then change the flood",
    art: (
      <svg viewBox="0 0 360 74" aria-hidden="true">
        <defs>
          <linearGradient id="tg2" x1="0" x2="1">
            <stop offset="0" stopColor="#fde3c7" />
            <stop offset=".5" stopColor="#ef7b45" />
            <stop offset="1" stopColor="#8c1c13" />
          </linearGradient>
        </defs>
        <rect x="8" y="12" width="150" height="9" rx="4.5" fill="url(#tg2)" />
        <text x="8" y="36" fill="var(--ink-3)" fontSize="11" fontFamily="system-ui, sans-serif">
          damage per cell
        </text>
        <line x1="200" y1="17" x2="344" y2="17" stroke="var(--hair)" strokeWidth="4" strokeLinecap="round" />
        <line x1="200" y1="17" x2="272" y2="17" stroke="var(--accent)" strokeWidth="4" strokeLinecap="round" />
        <circle cx="272" cy="17" r="7" fill="var(--solid)" stroke="var(--accent)" strokeWidth="2.5" />
        <text x="200" y="36" fill="var(--ink-3)" fontSize="11" fontFamily="system-ui, sans-serif">
          discharge
        </text>
        <text x="8" y="62" fill="var(--ink-2)" fontSize="12" fontFamily="system-ui, sans-serif">
          drag it and both layers follow
        </text>
      </svg>
    ),
    body: (
      <>
        <p>
          Damage follows the flood on its own. A warm layer builds over the water showing where the
          value is, with structures, residents and a total beside it.
        </p>
        <p>
          Then drag the discharge slider. It scales the gauge&rsquo;s peak up or down and both
          layers follow, so a bigger flood is priced rather than redrawn.
        </p>
      </>
    ),
  },
];

export const TOUR_SEEN = "floodline.tour.v1";

interface TourProps {
  at: number;
  onStep: (at: number) => void;
  onClose: () => void;
}

export function Tour({ at, onStep, onClose }: TourProps) {
  const step = TOUR[at] as Step;
  const last = at === TOUR.length - 1;

  const next = useCallback(() => {
    if (last) onClose();
    else onStep(at + 1);
  }, [at, last, onClose, onStep]);

  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") onClose();
      if (ev.key === "ArrowRight") next();
      if (ev.key === "ArrowLeft" && at > 0) onStep(at - 1);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [at, next, onClose, onStep]);

  return (
    <div
      className="scrim"
      id="tour"
      role="dialog"
      aria-modal="true"
      aria-labelledby="tour-title"
      onClick={(ev) => {
        if (ev.target === ev.currentTarget) onClose();
      }}
    >
      <section className="tour">
        <Wordmark>
          <span id="tour-step">
            {at + 1} of {TOUR.length}
          </span>
        </Wordmark>
        <div id="tour-body">
          <figure>{step.art}</figure>
          <h3 id="tour-title">{step.title}</h3>
          {step.body}
        </div>
        <div className="tourfoot">
          <div className="dots" id="tour-dots">
            {TOUR.map((_, i) => (
              <button
                key={i}
                type="button"
                aria-current={i === at}
                aria-label={`Step ${i + 1}`}
                onClick={() => onStep(i)}
              />
            ))}
          </div>
          <button className="skip" id="tour-skip" onClick={onClose}>
            Skip
          </button>
          <button id="tour-back" className="quiet" hidden={at === 0} onClick={() => onStep(at - 1)}>
            Back
          </button>
          <button id="tour-next" onClick={next} autoFocus>
            {last ? "Start exploring" : "Next"}
          </button>
        </div>
      </section>
    </div>
  );
}
