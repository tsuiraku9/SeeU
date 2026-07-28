import { afterEach, describe, expect, it } from "vitest";

import {
  defaultUiPreferences,
  loadUiPreferences,
  saveUiPreferences,
} from "./preferences";

afterEach(() => window.localStorage.clear());

describe("Web UI preferences", () => {
  it("uses conservative low-bandwidth defaults", () => {
    expect(loadUiPreferences()).toEqual(defaultUiPreferences);
    expect(defaultUiPreferences.lowBandwidth).toBe(true);
    expect(defaultUiPreferences.refreshSeconds).toBe(60);
  });

  it("persists allowed values and rejects unsupported values", () => {
    saveUiPreferences({
      refreshSeconds: 120,
      contentPageSize: 12,
      runPageSize: 25,
      lowBandwidth: false,
    });
    expect(loadUiPreferences()).toEqual({
      refreshSeconds: 120,
      contentPageSize: 12,
      runPageSize: 25,
      lowBandwidth: false,
    });

    window.localStorage.setItem(
      "seeu-ui-preferences-v1",
      JSON.stringify({
        refreshSeconds: 1,
        contentPageSize: 1000,
        runPageSize: -1,
        lowBandwidth: "no",
      }),
    );
    expect(loadUiPreferences()).toEqual(defaultUiPreferences);
  });
});
