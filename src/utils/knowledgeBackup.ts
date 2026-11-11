import type { FileNode } from '../types';
import { parseKnowledgeImport } from './importKnowledge.ts';

export interface BackupRecord { id: string; createdAt: string; updatedAt?: string | null; deletedAt?: string | null }
export interface KnowledgeBackup {
  format: 'noteflow-knowledge-backup'; version: 2; exportedAt: string;
  categories: (BackupRecord & {name: string; parentId: string | null; sortOrder: number})[];
  notes: (BackupRecord & {title: string; categoryId: string | null; content: string; summary?: string | null; tags: string[]; pinned: boolean; favorite: boolean})[];
  versions: (BackupRecord & {noteId: string; title: string; content: string; changeSummary?: string | null; source: string})[];
  attachments: (BackupRecord & {noteId: string | null; fileName: string; contentType: string; sha256: string; data: string})[];
}
export function emptyKnowledgeBackup(): KnowledgeBackup {
  return {format:'noteflow-knowledge-backup', version:2, exportedAt:new Date().toISOString(), categories:[],notes:[],versions:[],attachments:[]};
}

export function backupAsTree(backup: KnowledgeBackup): {treeData: FileNode[]; fileContents: Record<string,string>} {
  const folders = new Map(backup.categories.filter(r=>!r.deletedAt).map(r=>[r.id,{id:r.id,name:r.name,type:'folder' as const,children:[] as FileNode[]}]));
  const root: FileNode[]=[];
  for (const row of [...backup.categories].sort((a,b)=>a.sortOrder-b.sortOrder)) {
    const node=folders.get(row.id);if(!node)continue;
    (folders.get(row.parentId??'')?.children??root).push(node);
  }
  const fileContents: Record<string,string>=Object.create(null);
  for(const r of backup.notes.filter(r=>!r.deletedAt)) {
    fileContents[r.id]=r.content;
    (folders.get(r.categoryId??'')?.children??root).push({id:r.id,name:r.title+'.md',type:'file',content:r.content,
      tags:r.tags,pinned:r.pinned,favorite:r.favorite,createdAt:r.createdAt,updatedAt:r.updatedAt??r.createdAt});
  }
  return {treeData:root,fileContents};
}

export async function parseKnowledgeBackup(file: File): Promise<KnowledgeBackup> {
  if(file.size>25*1024*1024)throw new Error('文件超过 25 MB 导入限制');
  if(/\.json$/i.test(file.name)) {
    const value=JSON.parse(await file.text());
    if(value?.format==='noteflow-knowledge-backup') {
      if(value.version!==2)throw new Error('不支持此备份版本');
      // Server validates every field and reference before writing anything.
      return value as KnowledgeBackup;
    }
    if(Array.isArray(value?.treeData)) {
      const backup=emptyKnowledgeBackup(), seen=new Set<string>();
      const walk=(nodes: FileNode[], parentId:string|null, depth:number)=>{
        if(depth>64||!Array.isArray(nodes))throw new Error('备份目录结构不正确');
        for(const [order,node] of nodes.entries()) {
          if(!node||typeof node.id!=='string'||seen.has(node.id)||typeof node.name!=='string')throw new Error('备份包含无效或重复记录');
          seen.add(node.id);if(seen.size>1000)throw new Error('备份条目过多');
          const dates={createdAt:node.createdAt??backup.exportedAt,updatedAt:node.updatedAt??node.createdAt??backup.exportedAt};
          if(node.type==='folder') {
            backup.categories.push({id:node.id,name:node.name,parentId,sortOrder:order,...dates});walk(node.children??[],node.id,depth+1);
          } else if(node.type==='file') {
            backup.notes.push({id:node.id,title:node.name.replace(/\.md$/i,''),categoryId:parentId,...dates,
              content:value.fileContents?.[node.id]??node.content??'',summary:node.summary??null,tags:node.tags??[],pinned:!!node.pinned,favorite:!!node.favorite});
          } else throw new Error('备份节点类型不正确');
        }
      };
      walk(value.treeData,null,0);return backup;
    }
  }
  const records=await parseKnowledgeImport(file), backup=emptyKnowledgeBackup(), folders=new Map<string,string>();
  for(const record of records) {
    let parentId:string|null=null,path='';
    for(const name of record.sourcePath.split('/').slice(0,-1)) {
      path+=name+'/';let id=folders.get(path);
      if(!id){id=crypto.randomUUID();folders.set(path,id);backup.categories.push({id,name,parentId,sortOrder:backup.categories.length,createdAt:backup.exportedAt});}
      parentId=id;
    }
    backup.notes.push({id:crypto.randomUUID(),title:record.title,categoryId:parentId,content:record.content,tags:record.tags,
      pinned:false,favorite:false,createdAt:backup.exportedAt});
  }
  return backup;
}
