export interface UiPreferences {
  refreshSeconds: number;
  contentPageSize: number;
  runPageSize: number;
  lowBandwidth: boolean;
}

export const defaultUiPreferences: UiPreferences = {
  refreshSeconds: 60,
  contentPageSize: 24,
  runPageSize: 50,
  lowBandwidth: true,
};

const allowedRefresh = new Set([0, 30, 60, 120, 300]);
const allowedContentSizes = new Set([12, 24, 48]);
const allowedRunSizes = new Set([25, 50, 100]);
const storageKey = "seeu-ui-preferences-v1";

export function loadUiPreferences(): UiPreferences {
  try {
    const value = JSON.parse(window.localStorage.getItem(storageKey) || "{}") as Partial<UiPreferences>;
    return {
      refreshSeconds: allowedRefresh.has(Number(value.refreshSeconds))
        ? Number(value.refreshSeconds)
        : defaultUiPreferences.refreshSeconds,
      contentPageSize: allowedContentSizes.has(Number(value.contentPageSize))
        ? Number(value.contentPageSize)
        : defaultUiPreferences.contentPageSize,
      runPageSize: allowedRunSizes.has(Number(value.runPageSize))
        ? Number(value.runPageSize)
        : defaultUiPreferences.runPageSize,
      lowBandwidth:
        typeof value.lowBandwidth === "boolean"
          ? value.lowBandwidth
          : defaultUiPreferences.lowBandwidth,
    };
  } catch {
    return defaultUiPreferences;
  }
}

export function saveUiPreferences(value: UiPreferences): void {
  window.localStorage.setItem(storageKey, JSON.stringify(value));
}
