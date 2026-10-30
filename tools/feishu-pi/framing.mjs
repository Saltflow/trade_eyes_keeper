import { createHash, randomUUID } from "node:crypto";
export const MAX_FRAME_BYTES = 2 * 1024 * 1024;
export const MAX_MESSAGE_BYTES = 48 * 1024 * 1024;
const CHUNK_BYTES = 1024 * 1024;
export function encodeFrames(value) {
  const data = Buffer.from(JSON.stringify(value), "utf8");
  if (data.length > MAX_MESSAGE_BYTES) throw new Error("Logical message exceeds 48 MiB");
  if (data.length + 1 <= MAX_FRAME_BYTES) return [`${data.toString("utf8")}\n`];
  const id = randomUUID();
  const count = Math.ceil(data.length / CHUNK_BYTES);
  const sha256 = createHash("sha256").update(data).digest("hex");
  const frames = [];
  for (let index = 0; index < count; index += 1) frames.push(`${JSON.stringify({
    type: "frame", id, index, count, bytes: data.length, sha256,
    data: data.subarray(index * CHUNK_BYTES, (index + 1) * CHUNK_BYTES).toString("base64"),
  })}\n`);
  return frames;
}
export class FrameDecoder {
  current;
  accept(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Protocol requires object");
    if (value.type !== "frame") {
      if (this.current) throw new Error("Interrupted frame sequence");
      return value;
    }
    const { id, index, count, bytes, sha256, data } = value;
    if (typeof id !== "string" || !id || !Number.isSafeInteger(index) || index < 0 ||
        !Number.isSafeInteger(count) || count < 2 || count > 48 || index >= count ||
        !Number.isSafeInteger(bytes) || bytes < 1 || bytes > MAX_MESSAGE_BYTES ||
        !/^[a-f0-9]{64}$/.test(sha256) || typeof data !== "string" || !/^[A-Za-z0-9+/]+={0,2}$/.test(data)) throw new Error("Invalid frame metadata");
    if (!this.current) {
      if (index !== 0) throw new Error("Missing initial frame");
      this.current = { id, count, bytes, sha256, chunks: [], received: 0 };
    }
    const current = this.current;
    if (id !== current.id || count !== current.count || bytes !== current.bytes || sha256 !== current.sha256 || index !== current.chunks.length) throw new Error("Missing, repeated or reordered frame");
    const chunk = Buffer.from(data, "base64");
    if (chunk.toString("base64") !== data || chunk.length > CHUNK_BYTES) throw new Error("Invalid frame body");
    current.received += chunk.length;
    if (current.received > bytes) throw new Error("Frame byte count exceeded");
    current.chunks.push(chunk);
    if (current.chunks.length !== count) return undefined;
    const buffer = Buffer.concat(current.chunks);
    this.current = undefined;
    if (buffer.length !== bytes || createHash("sha256").update(buffer).digest("hex") !== sha256) throw new Error("Frame integrity mismatch");
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(buffer));
  }
}
