import { useState } from "react";

import { Chip } from "./Chip";
import { Wordmark } from "./Wordmark";

export interface LayerToggles {
  boundaries: boolean;
  marks: boolean;
  gradedOnly: boolean;
  hiRes: boolean;
  damage: boolean;
}

interface SearchPanelProps {
  level: number;
  onLevel: (level: number) => void;
  toggles: LayerToggles;
  onToggle: <K extends keyof LayerToggles>(key: K, value: boolean) => void;
  onSearch: (text: string) => void;
  onHelp: () => void;
}

const LEVELS: { value: number; label: string; title: string }[] = [
  { value: 8, label: "HUC-8", title: "Basin" },
  { value: 10, label: "HUC-10", title: "Watershed" },
  { value: 12, label: "HUC-12", title: "Subwatershed" },
];

export function SearchPanel({
  level,
  onLevel,
  toggles,
  onToggle,
  onSearch,
  onHelp,
}: SearchPanelProps) {
  const [text, setText] = useState("");

  return (
    <section className="panel" id="search">
      <Wordmark>
        <span>any US watershed</span>
        <a id="methodlink" href="/methodology" title="Data sources, processing and uncertainty">
          Method
        </a>
        <button id="helpbtn" title="Replay the introduction" aria-label="Replay the introduction" onClick={onHelp}>
          ?
        </button>
      </Wordmark>

      <div className="searchrow">
        <input
          type="text"
          id="q"
          placeholder="ZIP, address, or HUC code"
          autoComplete="off"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              onSearch(text);
            }
          }}
        />
        <button id="go" onClick={() => onSearch(text)}>
          Find
        </button>
      </div>

      <div className="row">
        <span className="micro">Click picks</span>
        <div className="seg" id="seg" role="group" aria-label="Hydrologic unit level a map click resolves to">
          {LEVELS.map((l) => (
            <button
              key={l.value}
              type="button"
              aria-pressed={level === l.value}
              title={l.title}
              onClick={() => onLevel(l.value)}
            >
              {l.label}
            </button>
          ))}
        </div>
      </div>

      <div className="chips">
        <Chip id="c-bnd" label="Boundaries" on={toggles.boundaries} onChange={(v) => onToggle("boundaries", v)} />
        <Chip id="c-hwm" label="Marks" on={toggles.marks} onChange={(v) => onToggle("marks", v)} />
        <Chip id="c-q" label="Best surveys only" on={toggles.gradedOnly} onChange={(v) => onToggle("gradedOnly", v)} />
        <Chip id="c-hi" label="10 m" on={toggles.hiRes} onChange={(v) => onToggle("hiRes", v)} />
        <Chip id="c-dmg" label="Damage" on={toggles.damage} onChange={(v) => onToggle("damage", v)} />
      </div>
    </section>
  );
}
