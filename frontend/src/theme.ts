export type Theme = "light" | "dark";
const storageKey = "atlas-theme";

export function readTheme(): Theme {
  try {
    return window.localStorage.getItem(storageKey) === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
  try {
    window.localStorage.setItem(storageKey, theme);
  } catch {
    // The switch still works when browser storage is unavailable.
  }
}
