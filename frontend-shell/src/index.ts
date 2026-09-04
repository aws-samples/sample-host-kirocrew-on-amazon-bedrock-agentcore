export interface WorkspaceMetadata {
  readonly component: "frontend-shell";
  readonly protocolVersion: "kirocrew-agentcore.v1";
}

export function workspaceMetadata(): WorkspaceMetadata {
  return {
    component: "frontend-shell",
    protocolVersion: "kirocrew-agentcore.v1",
  };
}

export * from "./bootstrap.js";
export * from "./remote-transport.js";
export * from "./agentcore-channel.js";
export * from "./browser-app.js";
export * from "./lifecycle.js";
export * from "./auth.js";
export * from "./oauth.js";
export * from "./shell.js";
