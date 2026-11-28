import assert from "node:assert/strict";
import { createKnowledgeZipBlob } from "../src/utils/exportKnowledgeZip.ts";
import { parseZipMarkdown } from "../src/utils/importKnowledge.ts";
import type { FileNode } from "../src/types.ts";

const tree: FileNode[] = [{
  id: "folder",
  name: "项目",
  type: "folder",
  children: [
    { id: "a", name: "计划.md", type: "file", tags: ["项目"] },
    { id: "b", name: "复盘.md", type: "file" },
  ],
}];
const contents = { a: "# 计划\n\n- [ ] M1", b: "# 复盘\n\n完成。" };
const blob = createKnowledgeZipBlob(tree, contents);
const records = await parseZipMarkdown(await blob.arrayBuffer());
assert.equal(records.length, 2);
assert.deepEqual(records.map((record) => record.sourcePath), ["项目/计划.md", "项目/复盘.md"]);
assert.equal(records[0].content, "# 计划\n\n- [ ] M1", "A matching document title is not a chapter");
assert.equal(records[1].content, "# 复盘\n\n完成。");
const reexported = createKnowledgeZipBlob(tree, { a: records[0].content, b: records[1].content });
assert.deepEqual((await parseZipMarkdown(await reexported.arrayBuffer())).map(r => r.content), records.map(r => r.content), "Reimporting a numbered Markdown export never adds a second number");
assert.equal(contents.a, "# 计划\n\n- [ ] M1", "Export does not update the source store");
console.log(JSON.stringify({ ok: true, imported: records.length }));

const {parseKnowledgeBackup,backupAsTree}=await import('../src/utils/knowledgeBackup.ts');
const legacy={treeData:[{id:'folder-original',name:'原目录',type:'folder',children:[{id:'note-original',name:'原笔记.md',type:'file',pinned:true,favorite:true,tags:['原标签'],createdAt:'2020-01-01T00:00:00Z'}]},{id:'empty-folder',name:'空目录',type:'folder',children:[]}],fileContents:{'note-original':'完整正文'}};
const parsed=await parseKnowledgeBackup({name:'backup.json',size:100,text:async()=>JSON.stringify(legacy)} as File);
assert.equal(parsed.categories.length,2);assert.equal(parsed.notes[0].categoryId,'folder-original');
assert.equal(parsed.notes[0].pinned,true);assert.equal(parsed.notes[0].favorite,true);
assert.equal(parsed.notes[0].createdAt,'2020-01-01T00:00:00Z');assert.equal(parsed.notes[0].content,'完整正文');
assert.deepEqual(parsed.notes[0].tags,['原标签']);
const recovered=backupAsTree(parsed);assert.equal(recovered.treeData[0].children![0].id,'note-original');assert.equal(recovered.treeData[1].name,'空目录');
const tampered=new Uint8Array(await blob.arrayBuffer());
for(let i=0;i<tampered.length-46;i++)if(new DataView(tampered.buffer).getUint32(i,true)===0x02014b50){new DataView(tampered.buffer).setUint32(i+24,1,true);break;}
await assert.rejects(()=>parseZipMarkdown(tampered.buffer),/大小或校验和/);
const importedZip=await parseKnowledgeBackup({name:'notes.zip',size:blob.size,arrayBuffer:()=>blob.arrayBuffer()} as File);
assert.equal(importedZip.categories[0].name,'项目');assert.equal(importedZip.notes[0].categoryId,importedZip.categories[0].id);
console.log('backup compatibility, metadata, hierarchy and tampered ZIP rejection passed');

// A tiny compressed archive lies about expanded size; reject while streaming,
// before retaining the whole oversized document.
const {deflateRawSync}=await import('node:zlib');
const {crc32}=await import('../src/utils/exportKnowledgeZip.ts');
function compressedArchive(entries:{path:string;content:Uint8Array;declaredSize?:number}[]):ArrayBuffer {
  const locals:Uint8Array[]=[],central:Uint8Array[]=[];let offset=0;
  const put16=(v:DataView,o:number,n:number)=>v.setUint16(o,n,true),put32=(v:DataView,o:number,n:number)=>v.setUint32(o,n,true);
  for(const e of entries){
    const name=new TextEncoder().encode(e.path),content=new Uint8Array(deflateRawSync(e.content));
    const local=new Uint8Array(30),l=new DataView(local.buffer),header=new Uint8Array(46),c=new DataView(header.buffer);
    put32(l,0,0x04034b50);put16(l,4,20);put16(l,8,8);put32(l,14,crc32(e.content));put32(l,18,content.length);put32(l,22,e.declaredSize??e.content.length);put16(l,26,name.length);
    put32(c,0,0x02014b50);put16(c,4,20);put16(c,6,20);put16(c,10,8);put32(c,16,crc32(e.content));put32(c,20,content.length);put32(c,24,e.declaredSize??e.content.length);put16(c,28,name.length);put32(c,42,offset);
    locals.push(local,name,content);central.push(header,name);offset+=local.length+name.length+content.length;
  }
  const centralSize=central.reduce((n,a)=>n+a.length,0),end=new Uint8Array(22),v=new DataView(end.buffer);
  put32(v,0,0x06054b50);put16(v,8,entries.length);put16(v,10,entries.length);put32(v,12,centralSize);put32(v,16,offset);
  const all=[...locals,...central,end],result=new Uint8Array(all.reduce((n,a)=>n+a.length,0));let cursor=0;
  for(const a of all){result.set(a,cursor);cursor+=a.length;}return result.buffer;
}
const oversized=compressedArchive([{path:'oversized.md',content:new Uint8Array(8*1024*1024+1).fill(65),declaredSize:1}]);
assert.ok(oversized.byteLength<10000);
await assert.rejects(()=>parseZipMarkdown(oversized),/8 MB/);
const aggregate=compressedArchive(Array.from({length:4},(_,i)=>({path:`file-${i}.md`,content:new Uint8Array(7*1024*1024).fill(65)})));
await assert.rejects(()=>parseZipMarkdown(aggregate),/解压总量超过 25 MB/);
console.log('streaming ZIP expansion and aggregate limits passed');

const reserved=structuredClone(parsed);reserved.notes[0].id='__proto__';
const reservedTree=backupAsTree(reserved);
assert.equal(reservedTree.fileContents['__proto__'],'完整正文','Reserved property names cannot corrupt exported contents');
