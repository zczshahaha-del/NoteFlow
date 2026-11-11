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
