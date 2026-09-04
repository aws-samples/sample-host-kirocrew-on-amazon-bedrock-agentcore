import { describe, expect, it } from "vitest";

import {
  PROTOCOL_VERSION,
  type ProtocolEnvelope,
} from "../src/generated/protocol.js";
import {
  ProtocolValidationError,
  SequenceTracker,
  chunkPayload,
  operation,
  reassembleChunks,
  serializeEnvelope,
  validateEnvelope,
} from "../src/protocol.js";

const validEnvelope: ProtocolEnvelope = {
  version: PROTOCOL_VERSION,
  messageId: "01J00000000000000000000001",
  requestId: "01J00000000000000000000002",
  operation: "chat.submit",
  sequence: 0,
  timestamp: "2026-08-17T16:00:00Z",
  correlationId: "01J00000000000000000000003",
  payload: { text: "contract fixture" },
};

function expectProtocolError(
  action: () => unknown,
  code: ProtocolValidationError["code"],
): void {
  try {
    action();
  } catch (error: unknown) {
    expect(error).toBeInstanceOf(ProtocolValidationError);
    if (error instanceof ProtocolValidationError) {
      expect(error.code).toBe(code);
    }
    return;
  }
  throw new Error(`Expected protocol error ${code}.`);
}

describe("protocol validation", () => {
  it("validates and serializes a generated envelope type", (): void => {
    expect(validateEnvelope(validEnvelope)).toBe(validEnvelope);
    expect(JSON.parse(serializeEnvelope(validEnvelope))).toEqual(validEnvelope);
    expect(operation("chat.submit")).toBe("chat.submit");
  });

  it.each([
    null,
    { ...validEnvelope, messageId: "bad" },
    { ...validEnvelope, operation: "unknown" },
    { ...validEnvelope, sequence: -1 },
    { ...validEnvelope, timestamp: "invalid" },
    { ...validEnvelope, payload: [] },
  ])("rejects malformed envelopes", (value: unknown): void => {
    expect(() => validateEnvelope(value)).toThrow(ProtocolValidationError);
  });

  it("distinguishes unsupported versions and unknown operations", (): void => {
    expectProtocolError(
      () => validateEnvelope({ ...validEnvelope, version: "v2" }),
      "UNSUPPORTED_PROTOCOL",
    );
    expectProtocolError(() => operation("unknown"), "INVALID_MESSAGE");
  });
});

describe("frame chunking", () => {
  it("round-trips bounded and empty byte payloads", (): void => {
    const payload = Uint8Array.from(
      { length: 70_000 },
      (_, index) => index % 251,
    );
    const chunks = chunkPayload(payload);
    expect(chunks).toHaveLength(3);
    expect(reassembleChunks(chunks)).toEqual(payload);
    expect(chunkPayload(new Uint8Array())).toEqual([
      { index: 0, total: 1, data: new Uint8Array() },
    ]);
  });

  it("rejects invalid limits and chunk sequences", (): void => {
    expect(() => chunkPayload(new Uint8Array(), 0)).toThrow(RangeError);
    expectProtocolError(() => reassembleChunks([]), "INVALID_MESSAGE");
    expectProtocolError(
      () =>
        reassembleChunks([
          { index: 1, total: 2, data: new Uint8Array([1]) },
          { index: 0, total: 2, data: new Uint8Array([2]) },
        ]),
      "SEQUENCE_ERROR",
    );
  });
});

describe("sequence tracking", () => {
  it("accepts independent contiguous streams and rejects gaps", (): void => {
    const tracker = new SequenceTracker();
    tracker.accept("request", 0);
    tracker.accept("request", 1);
    tracker.accept("other", 0);
    expectProtocolError(() => tracker.accept("request", 3), "SEQUENCE_ERROR");
  });
});
