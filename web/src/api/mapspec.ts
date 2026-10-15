/**
 * Map Spec 增量 diff（架构 §4.2 铁律：只对发生变化的图层做增量更新）。
 *
 * 判定口径：layer id 为键；style / source / status / derived_from 任一
 * 序列化不同即视为 updated。前端渲染器据此只动变化图层，不整图重绘。
 */
import type { LayerSpec, MapSpec } from "./types";

export interface LayerDiff {
  added: LayerSpec[];
  removed: string[];
  updated: LayerSpec[];
  unchanged: string[];
}

function signature(l: LayerSpec): string {
  return JSON.stringify([l.kind, l.source, l.style, l.status, l.derived_from]);
}

export function diffLayers(prev: MapSpec | null, next: MapSpec): LayerDiff {
  const before = new Map((prev?.layers ?? []).map((l) => [l.id, l]));
  const after = new Map(next.layers.map((l) => [l.id, l]));
  const diff: LayerDiff = { added: [], removed: [], updated: [], unchanged: [] };

  for (const [id, layer] of after) {
    const old = before.get(id);
    if (!old) {
      diff.added.push(layer);
    } else if (signature(old) !== signature(layer)) {
      diff.updated.push(layer);
    } else {
      diff.unchanged.push(id);
    }
  }
  for (const id of before.keys()) {
    if (!after.has(id)) diff.removed.push(id);
  }
  return diff;
}

/** 上一份 Map Spec 的快照比较是否有变化（用于跳过无谓的重渲染）。 */
export function mapSpecEquals(a: MapSpec | null, b: MapSpec | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return JSON.stringify(a.layers) === JSON.stringify(b.layers)
    && JSON.stringify(a.map_view) === JSON.stringify(b.map_view);
}
