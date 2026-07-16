import type { Platform } from "./types";

export const platformNames: Record<Platform, string> = {
  bilibili: "Bilibili",
  weibo: "微博",
  douyin: "抖音",
  xiaohongshu: "小红书"
};

export function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size.toFixed(size >= 10 ? 1 : 2)} ${units[index]}`;
}

export function formatDate(value: string | null): string {
  if (!value) return "尚未运行";
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

export function mediaUrl(contentId: number, path: string): string {
  const clean = path.replace(/^media\//, "");
  return `/api/media/${contentId}/${clean.split("/").map(encodeURIComponent).join("/")}`;
}

