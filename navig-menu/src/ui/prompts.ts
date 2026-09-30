import enquirer from "enquirer";

// enquirer ships CJS; grab the functional prompt API through the default import.
const { prompt } = enquirer as unknown as {
  prompt: (q: Record<string, unknown>) => Promise<Record<string, unknown>>;
};

/** Returned when the user escapes/Ctrl+C's a prompt — callers treat this as "back/cancel". */
export const CANCEL = Symbol("cancel");
export type Cancellable<T> = T | typeof CANCEL;

export interface Choice {
  name: string;
  message?: string;
  hint?: string;
  disabled?: boolean | string;
}

export async function select(opts: {
  message: string;
  choices: Choice[];
  initial?: number;
}): Promise<Cancellable<string>> {
  try {
    const res = await prompt({
      type: "select",
      name: "value",
      message: opts.message,
      choices: opts.choices,
      initial: opts.initial,
    });
    return res.value as string;
  } catch {
    return CANCEL;
  }
}

export async function autocomplete(opts: {
  message: string;
  choices: Choice[];
}): Promise<Cancellable<string>> {
  try {
    const res = await prompt({
      type: "autocomplete",
      name: "value",
      message: opts.message,
      choices: opts.choices,
      limit: 12,
    });
    return res.value as string;
  } catch {
    return CANCEL;
  }
}

export async function confirmPrompt(message: string, initial = false): Promise<Cancellable<boolean>> {
  try {
    const res = await prompt({ type: "confirm", name: "value", message, initial });
    return res.value as boolean;
  } catch {
    return CANCEL;
  }
}

export async function input(message: string): Promise<Cancellable<string>> {
  try {
    const res = await prompt({ type: "input", name: "value", message });
    return res.value as string;
  } catch {
    return CANCEL;
  }
}
