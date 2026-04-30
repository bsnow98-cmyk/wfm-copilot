import "@testing-library/jest-dom/vitest";

// vitest's jsdom in this version exposes `localStorage` but its methods are
// not callable from the top-level scope ChatPanel uses. Force a working shim.
{
  const store = new Map<string, string>();
  const shim: Storage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    get length() {
      return store.size;
    },
  };
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: shim,
  });
  Object.defineProperty(globalThis, "sessionStorage", {
    configurable: true,
    value: shim,
  });
}

// jsdom doesn't implement Element.scrollTo; the chat panel calls it on the
// scroll ref after each render.
if (typeof Element !== "undefined" && !Element.prototype.scrollTo) {
  Element.prototype.scrollTo = () => {};
}
