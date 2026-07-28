import "@testing-library/jest-dom/vitest";

Object.defineProperty(window, "scrollTo", { value: () => undefined, writable: true });

const localValues = new Map<string, string>();
Object.defineProperty(window, "localStorage", {
  configurable: true,
  value: {
    get length() { return localValues.size; },
    clear: () => localValues.clear(),
    getItem: (key: string) => localValues.get(key) ?? null,
    key: (index: number) => [...localValues.keys()][index] ?? null,
    removeItem: (key: string) => localValues.delete(key),
    setItem: (key: string, value: string) => localValues.set(key, String(value)),
  } satisfies Storage,
});
