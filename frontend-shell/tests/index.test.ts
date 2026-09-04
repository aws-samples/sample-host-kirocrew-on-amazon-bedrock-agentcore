import { describe, expect, it } from "vitest";

import { workspaceMetadata } from "../src/index.js";

describe("workspaceMetadata", () => {
  it("identifies the frontend package and external protocol", (): void => {
    expect(workspaceMetadata()).toEqual({
      component: "frontend-shell",
      protocolVersion: "kirocrew-agentcore.v1",
    });
  });
});
