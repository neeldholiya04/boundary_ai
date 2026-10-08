"use client";

import { GuardMode } from "@/lib/types";

const MODES: { mode: GuardMode; label: string; hint: string }[] = [
  { mode: "off", label: "Off", hint: "Not checked" },
  { mode: "shadow", label: "Shadow", hint: "Checked and logged, never acted on" },
  { mode: "enforce", label: "Enforce", hint: "Checked and acted on" }
];

/**
 * The three guard modes as one segmented control. The current mode is shown by colour and weight
 * (enforce in the accent, shadow in amber, off muted) and stays readable: it is the selected option,
 * not a disabled one.
 */
export function ModeSwitch({
  value,
  label,
  busy = false,
  onChange
}: {
  value: GuardMode;
  label: string;
  busy?: boolean;
  onChange: (mode: GuardMode) => void;
}) {
  return (
    <div className="seg" role="radiogroup" aria-label={label} aria-busy={busy}>
      {MODES.map(({ mode, label: text, hint }) => {
        const selected = value === mode;
        return (
          <button
            key={mode}
            type="button"
            role="radio"
            aria-checked={selected}
            title={hint}
            className={`seg-option ${mode}`}
            disabled={busy}
            onClick={() => {
              if (!selected) onChange(mode);
            }}
          >
            {text}
          </button>
        );
      })}
    </div>
  );
}
