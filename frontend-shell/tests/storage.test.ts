import { afterEach, describe, expect, it } from "vitest";

import { durableStorage } from "../src/storage.js";

class MemoryStorage implements Storage {
  #values = new Map<string, string>();

  public get length(): number {
    return this.#values.size;
  }

  public clear(): void {
    this.#values.clear();
  }

  public getItem(key: string): string | null {
    return this.#values.get(key) ?? null;
  }

  public key(index: number): string | null {
    return [...this.#values.keys()][index] ?? null;
  }

  public removeItem(key: string): void {
    this.#values.delete(key);
  }

  public setItem(key: string, value: string): void {
    this.#values.set(key, value);
  }
}

function define(
  name: "localStorage" | "sessionStorage",
  get: () => Storage,
): void {
  Object.defineProperty(globalThis, name, { configurable: true, get });
}

afterEach(() => {
  for (const name of ["localStorage", "sessionStorage"] as const) {
    Reflect.deleteProperty(globalThis, name);
  }
});

describe("durableStorage", () => {
  it("keeps the session in localStorage so it outlives the tab", () => {
    const durable = new MemoryStorage();
    const perTab = new MemoryStorage();
    define("localStorage", () => durable);
    define("sessionStorage", () => perTab);
    expect(durableStorage()).toBe(durable);
  });

  it("falls back to sessionStorage when the durable store is blocked", () => {
    const perTab = new MemoryStorage();
    // A private window or a cookie policy makes the *access* throw, not answer.
    define("localStorage", () => {
      throw new Error("SecurityError");
    });
    define("sessionStorage", () => perTab);
    expect(durableStorage()).toBe(perTab);
  });
});
