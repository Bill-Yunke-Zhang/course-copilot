import { useMemo, useState } from "react";
import type { ResourceItem } from "../lib/api";
import { TagChips } from "./TagChips";

/**
 * Collapsible tree view of a multi-item resource (a repo or an ingested
 * directory). Folders can be tagged as a unit: the folder-level chips show
 * the tags every descendant shares, and saving applies the chosen set to
 * all files under that folder (a bulk edit over the same per-item tags —
 * there is no separate "folder tag" concept).
 */

interface TreeDir {
  name: string;
  path: string;
  dirs: TreeDir[];
  files: ResourceItem[];
}

function buildTree(items: ResourceItem[]): TreeDir {
  const root: TreeDir = { name: "", path: "", dirs: [], files: [] };
  for (const item of items) {
    const parts = item.path.split("/");
    let node = root;
    for (const part of parts.slice(0, -1)) {
      let child = node.dirs.find((d) => d.name === part);
      if (!child) {
        child = {
          name: part,
          path: node.path ? `${node.path}/${part}` : part,
          dirs: [],
          files: [],
        };
        node.dirs.push(child);
      }
      node = child;
    }
    node.files.push(item);
  }
  const sortTree = (node: TreeDir) => {
    node.dirs.sort((a, b) => a.name.localeCompare(b.name));
    node.files.sort((a, b) => a.path.localeCompare(b.path));
    node.dirs.forEach(sortTree);
  };
  sortTree(root);
  return root;
}

function descendants(dir: TreeDir): ResourceItem[] {
  return [...dir.files, ...dir.dirs.flatMap(descendants)];
}

function sharedTags(items: ResourceItem[]): string[] {
  if (items.length === 0) return [];
  return items[0].tags.filter((t) => items.every((i) => i.tags.includes(t)));
}

function FileRow({
  item,
  depth,
  vocabulary,
  onSaveTags,
}: {
  item: ResourceItem;
  depth: number;
  vocabulary: string[];
  onSaveTags: (itemId: number, tags: string[]) => void;
}) {
  return (
    <li
      className="flex flex-wrap items-center gap-2 py-0.5"
      style={{ paddingLeft: depth * 16 }}
    >
      <span className="font-mono text-xs text-slate-500 dark:text-slate-400">
        {item.path.split("/").pop()}
      </span>
      <TagChips
        tags={item.tags}
        vocabulary={vocabulary}
        onChange={(tags) => onSaveTags(item.id, tags)}
      />
    </li>
  );
}

function DirNode({
  dir,
  depth,
  vocabulary,
  onSaveTags,
  onBulkTags,
}: {
  dir: TreeDir;
  depth: number;
  vocabulary: string[];
  onSaveTags: (itemId: number, tags: string[]) => void;
  onBulkTags: (pathPrefix: string, tags: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const all = descendants(dir);
  return (
    <li>
      <div
        className="flex flex-wrap items-center gap-2 py-0.5"
        style={{ paddingLeft: depth * 16 }}
      >
        <button
          onClick={() => setOpen((o) => !o)}
          className="text-slate-400 hover:text-slate-600 dark:text-slate-500 dark:hover:text-slate-300"
        >
          {open ? "▾" : "▸"}
        </button>
        <span className="text-xs font-medium">{dir.name}/</span>
        <span className="text-xs text-slate-400 dark:text-slate-500">
          {all.length} file{all.length === 1 ? "" : "s"}
        </span>
        {/* Shared tags of every descendant; saving tags the whole folder. */}
        <TagChips
          tags={sharedTags(all)}
          vocabulary={vocabulary}
          onChange={(tags) => onBulkTags(dir.path, tags)}
        />
      </div>
      {open && (
        <ul>
          {dir.dirs.map((child) => (
            <DirNode
              key={child.path}
              dir={child}
              depth={depth + 1}
              vocabulary={vocabulary}
              onSaveTags={onSaveTags}
              onBulkTags={onBulkTags}
            />
          ))}
          {dir.files.map((item) => (
            <FileRow
              key={item.id}
              item={item}
              depth={depth + 1}
              vocabulary={vocabulary}
              onSaveTags={onSaveTags}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

export function ItemTree({
  items,
  vocabulary,
  onSaveTags,
  onBulkTags,
}: {
  items: ResourceItem[];
  vocabulary: string[];
  onSaveTags: (itemId: number, tags: string[]) => void;
  onBulkTags: (pathPrefix: string, tags: string[]) => void;
}) {
  const root = useMemo(() => buildTree(items), [items]);
  return (
    <ul>
      {root.dirs.map((dir) => (
        <DirNode
          key={dir.path}
          dir={dir}
          depth={0}
          vocabulary={vocabulary}
          onSaveTags={onSaveTags}
          onBulkTags={onBulkTags}
        />
      ))}
      {root.files.map((item) => (
        <FileRow
          key={item.id}
          item={item}
          depth={0}
          vocabulary={vocabulary}
          onSaveTags={onSaveTags}
        />
      ))}
    </ul>
  );
}
