// A switch that looks like one: a sliding pill, grey off and green on.
//
// A real checkbox under a label rather than a styled div, so it keeps
// keyboard focus, the space bar, the checked state a screen reader reads and
// the click target the label gives it. The pill is the input's own ::before
// and ::after, drawn in styles.css.
export function Switch({
  checked,
  onChange,
  label,
  inherited = false,
  disabled = false,
  title,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  // Whether this level is silent and the position shown was decided above.
  // The slider cannot hold three states, so the third is drawn rather than
  // slid: a dashed track, and the word beside it saying where it came from.
  inherited?: boolean;
  disabled?: boolean;
  title?: string;
}) {
  const state = disabled
    ? "switch-gated"
    : inherited
      ? "switch-inherited"
      : checked
        ? "switch-on"
        : "switch-off";
  return (
    <label className={`switch ${state}`} title={title}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="slider" aria-hidden="true" />
      <span className="switch-label">{label}</span>
    </label>
  );
}
