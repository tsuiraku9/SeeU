import { describe, expect, it } from "vitest";
import { formatBytes, mediaUrl } from "./utils";

describe("display helpers", () => {
  it("formats archive sizes", () => expect(formatBytes(1024 * 1024)).toBe("1.00 MB"));
  it("creates an authenticated media route", () => expect(mediaUrl(4, "media/a b.mp4")).toBe("/api/media/4/a%20b.mp4"));
});

