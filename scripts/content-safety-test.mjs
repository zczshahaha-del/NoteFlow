import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { createServer } from 'vite';
const dom = new JSDOM('<!doctype html><body></body>', {url:'https://noteflow.invalid/'});
Object.assign(globalThis, {window:dom.window, document:dom.window.document, NodeFilter:dom.window.NodeFilter});
const vite = await createServer({appType:'custom', logLevel:'silent',server:{middlewareMode:true}});
try {
 const {renderChatMarkdown} = await vite.ssrLoadModule('/src/utils/chatMarkdown.ts');
 const unsafe = [
   '<a href="javascript:alert(1)">x</a>', '<a href="java&#x09;script:alert(1)">x</a>',
   '<a href="java&#x0a;script:alert(1)">x</a>', '<a href="data:text/html,hello">x</a>',
   '<svg><a xlink:href="javascript:alert(1)">x</a></svg>',
   '<img src=x onerror="alert(1)"><iframe srcdoc="x"></iframe>',
   '<form id="document"><input name="location"></form>',
   '<button data-chat-copy data-language-option="x">copy</button>',
   '<img src="data:image/svg+xml,hi"><img srcset="javascript:alert(1)">',
   '<a href="vbscript:msgbox(1)">x</a><style>body{display:none}</style>',
 ];
 for (const input of unsafe) {
  const host=document.createElement('div');host.innerHTML=renderChatMarkdown(input);
  assert.equal(host.querySelectorAll('svg,math,iframe,script,style,form,input,button,[id],[name],[srcset],[data-chat-copy]').length,0,input);
  for(const n of host.querySelectorAll('*'))for(const a of n.attributes) assert.ok(!a.name.startsWith('on'));
  for(const n of host.querySelectorAll('a[href],img[src]')) assert.ok(['https:','http:','mailto:','tel:'].includes(new URL(n.getAttribute('href')||n.getAttribute('src'),dom.window.location.href).protocol));
 }
 const safe=renderChatMarkdown('# 标题\n\n[链接](https://example.invalid)\n\n![图片](/api/attachments/test/content)\n\n```js\nconst x = "<script>";\n```\n\n|列|\n|-|\n|值|\n\n> 提示');
 const host=document.createElement('div');host.innerHTML=safe;
 assert.ok(host.querySelector('h1'));assert.ok(host.querySelector('a[href="https://example.invalid"]'));
 assert.ok(host.querySelector('img[src="/api/attachments/test/content"]'));assert.ok(host.querySelector('.chat-code-block button[data-chat-copy]'));
 assert.ok(host.querySelector('table'));assert.ok(host.querySelector('blockquote'));
 assert.ok(host.textContent.includes('const x = "<script>";'));
 console.log('content safety: 10 malicious inputs and normal Markdown/code/image/table rendering passed');
} finally {await vite.close();dom.window.close();}
