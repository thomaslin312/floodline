interface ChipProps {
  id: string;
  label: string;
  on: boolean;
  onChange: (on: boolean) => void;
}

/** A toggle that looks like a chip. The checkbox is real so it stays keyboard
 *  reachable and announced; the label carries the styling. */
export function Chip({ id, label, on, onChange }: ChipProps) {
  return (
    <label className={on ? "chip on" : "chip"} id={id}>
      <input type="checkbox" checked={on} onChange={(e) => onChange(e.target.checked)} />
      <i />
      {label}
    </label>
  );
}
