import {
  CLIENT_OPERATIONS,
  PROTOCOL_VERSION,
  SERVER_OPERATIONS,
  type Operation,
  type ProtocolEnvelope,
} from "./generated/protocol.js";

export const FRAME_PAYLOAD_LIMIT = 24 * 1024;
const OPERATIONS: ReadonlySet<string> = new Set([
  ...CLIENT_OPERATIONS,
  ...SERVER_OPERATIONS,
]);
const ULID = /^[0-9A-HJKMNP-TV-Z]{26}$/u;

export class ProtocolValidationError extends Error {
  public constructor(
    public readonly code:
      | "INVALID_MESSAGE"
      | "UNSUPPORTED_PROTOCOL"
      | "SEQUENCE_ERROR",
    message: string,
  ) {
    super(message);
    this.name = "ProtocolValidationError";
  }
}

export interface FrameChunk {
  readonly index: number;
  readonly total: number;
  readonly data: Uint8Array;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isUlid(value: unknown): value is string {
  return typeof value === "string" && ULID.test(value);
}

export function validateEnvelope(value: unknown): ProtocolEnvelope {
  if (!isRecord(value)) {
    throw new ProtocolValidationError(
      "INVALID_MESSAGE",
      "Envelope must be an object.",
    );
  }
  if (value.version !== PROTOCOL_VERSION) {
    throw new ProtocolValidationError(
      "UNSUPPORTED_PROTOCOL",
      "Unsupported protocol version.",
    );
  }
  if (
    !isUlid(value.messageId) ||
    (value.requestId !== undefined &&
      value.requestId !== null &&
      !isUlid(value.requestId)) ||
    !isUlid(value.correlationId) ||
    !OPERATIONS.has(String(value.operation)) ||
    !Number.isSafeInteger(value.sequence) ||
    Number(value.sequence) < 0 ||
    typeof value.timestamp !== "string" ||
    Number.isNaN(Date.parse(value.timestamp)) ||
    !isRecord(value.payload)
  ) {
    throw new ProtocolValidationError(
      "INVALID_MESSAGE",
      "Envelope does not match the protocol contract.",
    );
  }
  return value as unknown as ProtocolEnvelope;
}

export function serializeEnvelope(envelope: ProtocolEnvelope): string {
  return JSON.stringify(validateEnvelope(envelope));
}

export function chunkPayload(
  payload: Uint8Array,
  limit = FRAME_PAYLOAD_LIMIT,
): readonly FrameChunk[] {
  if (!Number.isSafeInteger(limit) || limit <= 0) {
    throw new RangeError("Chunk limit must be a positive integer.");
  }
  const total = Math.max(1, Math.ceil(payload.byteLength / limit));
  return Array.from({ length: total }, (_, index) => ({
    index,
    total,
    data: payload.slice(
      index * limit,
      Math.min((index + 1) * limit, payload.length),
    ),
  }));
}

export function reassembleChunks(chunks: readonly FrameChunk[]): Uint8Array {
  if (chunks.length === 0) {
    throw new ProtocolValidationError(
      "INVALID_MESSAGE",
      "At least one frame chunk is required.",
    );
  }
  const total = chunks[0]?.total;
  if (
    total !== chunks.length ||
    chunks.some(
      (chunk, index) => chunk.total !== total || chunk.index !== index,
    )
  ) {
    throw new ProtocolValidationError(
      "SEQUENCE_ERROR",
      "Frame chunks are incomplete or out of order.",
    );
  }
  const size = chunks.reduce((sum, chunk) => sum + chunk.data.byteLength, 0);
  const result = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk.data, offset);
    offset += chunk.data.byteLength;
  }
  return result;
}

export class SequenceTracker {
  readonly #lastSeen = new Map<string, number>();

  public accept(streamId: string, sequence: number): void {
    const expected = (this.#lastSeen.get(streamId) ?? -1) + 1;
    if (sequence !== expected) {
      throw new ProtocolValidationError(
        "SEQUENCE_ERROR",
        "Message sequence is not contiguous.",
      );
    }
    this.#lastSeen.set(streamId, sequence);
  }
}

export function operation(value: string): Operation {
  if (!OPERATIONS.has(value)) {
    throw new ProtocolValidationError("INVALID_MESSAGE", "Unknown operation.");
  }
  return value as Operation;
}
