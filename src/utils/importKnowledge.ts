import { crc32 } from "./exportKnowledgeZip.ts";
import type { FileNode } from "../types";

export interface KnowledgeImportRecord {
  title: string;
  content: string;
  sourcePath: string;
  tags: string[];
}

const decoder = new TextDecoder("utf-8", { fatal: false });
const MAX_ARCHIVE_BYTES = 25 * 1024 * 1024;
const MAX_ENTRY_BYTES = 8 * 1024 * 1024;
const MAX_IMPORT_FILES = 500;

function cleanTitle(value: string): string {
  const segment = value.split("/").pop() || value;
  return segment.replace(/\.(?:md|markdown)$/i, "").replace(/[<>:"/\\|?*]/g, " ").trim().slice(0, 120) || "导入笔记";
}

function safePath(value: string): string {
  return value.replace(/\\/g, "/").split("/").filter((segment) => segment && segment !== "." && segment !== "..").join("/");
}

async function inflateRaw(bytes: Uint8Array): Promise<Uint8Array> {
  if (typeof DecompressionStream === "undefined") {
    throw new Error("当前浏览器不支持解压这种 ZIP，请先解压后导入笔记文件");
  }
  const reader = new Blob([Uint8Array.from(bytes).buffer]).stream().pipeThrough(new DecompressionStream("deflate-raw")).getReader();
  const chunks: Uint8Array[]=[];let size=0;
  try {
    while(true) {
      const {value,done}=await reader.read();if(done)break;
      size+=value.length;
      if(size>MAX_ENTRY_BYTES)throw new Error("ZIP 解压内容超过 8 MB 单文件限制");
      chunks.push(value);
    }
  } finally {await reader.cancel().catch(()=>{});reader.releaseLock();}
  const result=new Uint8Array(size);let offset=0;
  for(const chunk of chunks){result.set(chunk,offset);offset+=chunk.length;}
  return result;
}

export async function parseZipMarkdown(buffer: ArrayBuffer): Promise<KnowledgeImportRecord[]> {
  if (buffer.byteLength > MAX_ARCHIVE_BYTES) throw new Error("ZIP 超过 25 MB 导入限制");
  const bytes = new Uint8Array(buffer);
  const view = new DataView(buffer);
  let eocd = -1;
  for (let offset = bytes.length - 22; offset >= Math.max(0, bytes.length - 65_557); offset -= 1) {
    if (view.getUint32(offset, true) === 0x06054b50) { eocd = offset; break; }
  }
  if (eocd < 0) throw new Error("无法识别 ZIP 中央目录");

  const count = view.getUint16(eocd + 10, true);
  if (count > MAX_IMPORT_FILES) throw new Error(`ZIP 文件数超过 ${MAX_IMPORT_FILES} 个`);
  let offset = view.getUint32(eocd + 16, true);
  const records: KnowledgeImportRecord[] = [];
  let totalBytes = 0;

  for (let index = 0; index < count; index += 1) {
    if (offset + 46 > bytes.length || view.getUint32(offset, true) !== 0x02014b50) {
      throw new Error("ZIP 中央目录损坏");
    }
    if(view.getUint16(offset+8,true)&1)throw new Error("不支持加密 ZIP");
    const checksum=view.getUint32(offset+16,true);
    const method = view.getUint16(offset + 10, true);
    const compressedSize = view.getUint32(offset + 20, true);
    const uncompressedSize = view.getUint32(offset + 24, true);
    const nameLength = view.getUint16(offset + 28, true);
    const extraLength = view.getUint16(offset + 30, true);
    const commentLength = view.getUint16(offset + 32, true);
    const localOffset = view.getUint32(offset + 42, true);
    if(offset+46+nameLength+extraLength+commentLength>eocd)throw new Error("ZIP 目录越界");
    const sourcePath = safePath(decoder.decode(bytes.slice(offset + 46, offset + 46 + nameLength)));
    offset += 46 + nameLength + extraLength + commentLength;

    if (!/\.(?:md|markdown)$/i.test(sourcePath) || !sourcePath || sourcePath.startsWith("__MACOSX/")) continue;
    if (uncompressedSize > MAX_ENTRY_BYTES) throw new Error(`${sourcePath} 超过 8 MB 单文件限制`);
    if (localOffset + 30 > bytes.length || view.getUint32(localOffset, true) !== 0x04034b50) {
      throw new Error(`${sourcePath} 的本地文件头损坏`);
    }
    const localNameLength = view.getUint16(localOffset + 26, true);
    const localExtraLength = view.getUint16(localOffset + 28, true);
    const dataStart = localOffset + 30 + localNameLength + localExtraLength;
    if(dataStart+compressedSize>bytes.length||dataStart+compressedSize>view.getUint32(eocd+16,true))throw new Error("ZIP 数据越界");
    if(view.getUint16(localOffset+8,true)!==method)throw new Error("ZIP 压缩方法不一致");
    if(method===0&&compressedSize>MAX_ENTRY_BYTES)throw new Error("ZIP 内容超过 8 MB 单文件限制");
    const compressed = bytes.slice(dataStart, dataStart + compressedSize);
    const contentBytes = method === 0 ? compressed : method === 8 ? await inflateRaw(compressed) : null;
    if (!contentBytes) throw new Error(`${sourcePath} 使用了不支持的 ZIP 压缩算法 ${method}`);
    if(contentBytes.length!==uncompressedSize||crc32(contentBytes)!==checksum)throw new Error(`${sourcePath} 的大小或校验和不正确`);
    totalBytes+=contentBytes.length;
    if(totalBytes>MAX_ARCHIVE_BYTES)throw new Error("ZIP 解压总量超过 25 MB 限制");
    records.push({
      title: cleanTitle(sourcePath),
      content: decoder.decode(contentBytes),
      sourcePath,
      tags: ["导入"],
    });
  }

  if (!records.length) throw new Error("ZIP 中没有找到支持的笔记文件");
  return records;
}

function flattenSnapshot(nodes: FileNode[], fileContents: Record<string, string>, parents: string[] = []): KnowledgeImportRecord[] {
  return nodes.flatMap((node) => {
    if (node.type === "file") {
      const sourcePath = [...parents, node.name].join("/");
      return [{ title: cleanTitle(node.name), content: fileContents[node.id] ?? node.content ?? "", sourcePath, tags: ["导入", ...(node.tags ?? [])] }];
    }
    return flattenSnapshot(node.children ?? [], fileContents, [...parents, node.name]);
  });
}

export async function parseKnowledgeImport(file: File): Promise<KnowledgeImportRecord[]> {
  if (file.size > MAX_ARCHIVE_BYTES) throw new Error(`${file.name} 超过 25 MB 导入限制`);
  if (/\.zip$/i.test(file.name)) return parseZipMarkdown(await file.arrayBuffer());
  if (/\.json$/i.test(file.name)) {
    const value = JSON.parse(await file.text()) as { treeData?: FileNode[]; fileContents?: Record<string, string>; notes?: Array<{ title?: string; content?: string; tags?: string[] }> };
    if (Array.isArray(value.treeData)) return flattenSnapshot(value.treeData, value.fileContents ?? {});
    if (Array.isArray(value.notes)) {
      return value.notes.slice(0, MAX_IMPORT_FILES).map((note, index) => ({
        title: cleanTitle(note.title || `导入笔记 ${index + 1}`),
        content: typeof note.content === "string" ? note.content : "",
        sourcePath: note.title || `note-${index + 1}.md`,
        tags: ["导入", ...(Array.isArray(note.tags) ? note.tags : [])],
      }));
    }
    throw new Error("JSON 不是可识别的 NoteFlow 备份");
  }
  if (/\.(?:md|markdown)$/i.test(file.name)) {
    if(file.size>MAX_ENTRY_BYTES)throw new Error("笔记超过 8 MB 单文件限制");
    return [{ title: cleanTitle(file.name), content: await file.text(), sourcePath: file.name, tags: ["导入"] }];
  }
  throw new Error(`不支持导入 ${file.name}`);
}
