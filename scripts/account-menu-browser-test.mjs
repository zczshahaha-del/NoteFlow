// Actual AccountMenu + production CSS, synthetic state and callbacks, no real network.
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { build } from 'vite';
const { chromium }=createRequire(import.meta.url)(process.env.NOTEFLOW_PLAYWRIGHT_MODULE||'playwright');
const path=n=>fileURLToPath(new URL('../src/'+n,import.meta.url));
const assets=new URL('../dist/assets/',import.meta.url);
const css=readdirSync(assets).filter(n=>/^index-.*\.css$/.test(n)).map(n=>readFileSync(new URL(n,assets),'utf8')).join('\n');
assert(css,'Build CSS first');
const entry=`import React from 'react';import{createRoot}from'react-dom/client';
import AccountMenu from ${JSON.stringify(path('components/AppNav.tsx'))};
import{useAppStore}from ${JSON.stringify(path('store/index.tsx'))};
useAppStore.setState({treeData:[],fileContents:{},deletedNotes:[],serverSyncReady:false,hydratedUserId:null});
let memoryEnabled=true;
window.fetch=async(input,init={})=>{
  const url=String(input);
  let data=[];
  if(url.endsWith('/api/settings')){
    if(init.method==='PUT')memoryEnabled=JSON.parse(init.body).memoryEnabled;
    data={settings:{memoryEnabled,preferences:{}}};
  }else if(url.endsWith('/api/memories')){
    const examples=[
      '用户表示“小林”是准确的 | Involving: user',
      '用户的名字是小林，用户更正了自己的称呼。 | Involving: 小林 | 旧记录',
      '用户的名字是小雨 | Involving: 小雨',
      '用户澄清自己不叫小林，之前的称呼有误。 | Involving: user',
      '喜欢简洁的回答。',
      '喜欢简洁的回答。 | Involving: user',
      '偏好使用示例来解释复杂概念。',
      '测试长记忆内容。'.repeat(50),
      '学习技术术语时，希望先讲原理。',
      '正在整理阅读笔记。',
      '喜欢浅色模式。',
    ];
    data={memories:examples.map((content,index)=>({
      id:'browser-memory-'+index,content,provider:'hindsight',scope:'global',
      status:index===7?'pending':index===10?'archived':'active',
    }))};
  }
  return new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
};
const root=createRoot(document.getElementById('anchor'));
const props={compact:true,themeMode:'light',userEmail:'example@example.invalid',userEmailVerified:true,userName:'示例用户',onThemeModeChange(){},onSignOut(){window.signOutCount++;},trashCount:13};
window.signOutCount=0;window.accountTest={update(p){Object.assign(props,p);root.render(React.createElement(React.StrictMode,null,React.createElement(AccountMenu,props)));}};
window.accountTest.update({});`;
const bundle=await build({configFile:false,logLevel:'silent',plugins:[{name:'account-fixture',resolveId(id){if(id==='virtual:entry')return '\0entry';},load(id){if(id==='\0entry')return entry;}}],build:{write:false,minify:false,rolldownOptions:{input:'virtual:entry',output:{codeSplitting:false}}}});
const chunks=bundle.output.filter(n=>n.type==='chunk');assert.equal(chunks.length,1);
const script=chunks[0].code.replaceAll('</script','<\\/script');
const browser=await chromium.launch({executablePath:process.env.NOTEFLOW_BROWSER_EXECUTABLE||undefined,headless:true});
const output=mkdtempSync(join(tmpdir(),'noteflow-account-menu-')),reports=[];
async function settled(page){await page.evaluate(async()=>{await Promise.all(document.getAnimations().map(a=>a.finished.catch(()=>{})));});}
async function open(page){await page.locator('.nf-account-trigger').click();await page.locator('.nf-account-popover[data-open="true"]').waitFor();await settled(page);}
async function framelessLauncherCheck(page, width){
 const trigger=page.locator('.nf-account-trigger'), avatar=trigger.locator('.nf-avatar');
 const box=await trigger.boundingBox();
 const check=async()=>{
  assert.equal(await trigger.evaluate(n=>getComputedStyle(n).backgroundColor),'rgba(0, 0, 0, 0)','Compact avatar never acquires a rectangular plate');
  assert.equal(await trigger.evaluate(n=>getComputedStyle(n).borderRadius),'50%');
  assert.deepEqual(await trigger.boundingBox(),box,'Feedback must not move the launcher');
 };
 await check();
 assert.equal(await avatar.evaluate(n=>getComputedStyle(n).boxShadow),'none');
 if(width!==320){
  await trigger.hover();await settled(page);await check();
  assert.notEqual(await avatar.evaluate(n=>getComputedStyle(n).boxShadow),'none','Hover feedback belongs to the circular avatar');
  await page.mouse.down();
  assert.equal(await trigger.evaluate(n=>getComputedStyle(n).transform),'none','Press must not shift the avatar');
  await page.mouse.move(width-5,8);await page.mouse.up();await settled(page);
  assert.equal(await avatar.evaluate(n=>getComputedStyle(n).boxShadow),'none','Mouse leave clears hover feedback');
 }
 assert(box.width>=44&&box.height>=44,'Preserve the existing touch target');
}
try{
 for(const width of [1280,736,320]){
  const context=await browser.newContext({viewport:{width,height:720},hasTouch:width===320}),requests=[],errors=[];
  await context.route('**/*',r=>{requests.push(r.request().url());return r.abort();});
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
  await page.setContent('<html><head><style>'+css+' body{background:#fdfdfc}#anchor{position:absolute;left:12px;bottom:16px;width:236px}</style></head><body><div id="anchor"></div><script type="module">'+script+'</script></body></html>');
  await page.locator('.nf-account-trigger').waitFor();const triggerBefore=await page.locator('.nf-account-trigger').boundingBox();
  await framelessLauncherCheck(page,width);
  await open(page);
  assert.equal(await page.locator('.nf-account-trigger').evaluate(n=>getComputedStyle(n).backgroundColor),'rgba(0, 0, 0, 0)');
  assert.notEqual(await page.locator('.nf-account-trigger .nf-avatar').evaluate(n=>getComputedStyle(n).boxShadow),'none','Open menu keeps only circular feedback');
  const panel=page.locator('.nf-account-popover');
  assert.equal(await panel.locator('.nf-account-separator').count(),2);
  const geometry=await panel.evaluate(n=>({box:n.getBoundingClientRect().toJSON(),radius:getComputedStyle(n).borderRadius,avatar:n.querySelector('.nf-avatar').getBoundingClientRect().width,footer:document.querySelector('.nf-account-trigger .nf-avatar').getBoundingClientRect().width,rows:[...n.querySelectorAll('.nf-menu-item')].map(b=>b.getBoundingClientRect().height)}));
  assert.equal(geometry.box.width,224);assert.equal(geometry.radius,'12px');assert.equal(geometry.avatar,28);assert.equal(geometry.footer,34);
  assert(geometry.box.x>=0&&geometry.box.right<=width);assert(geometry.box.y>=0&&geometry.box.bottom<=720);
  assert.equal(geometry.box.x,triggerBefore.x+54,'Open to the right of the avatar launcher');
  assert.equal(geometry.box.bottom,triggerBefore.y+triggerBefore.height,'Align menu bottom with launcher');
  assert(geometry.rows.every(h=>h===(width===320?44:36)));
  assert.deepEqual(await page.locator('.nf-account-trigger').boundingBox(),triggerBefore,'Launcher must not change');
  assert.equal(await panel.locator('button').count(),3);assert.equal(await panel.getByText('13',{exact:true}).count(),1);
  assert.equal(await panel.locator('[title]').count(),0);
  if(width===1280)await page.screenshot({path:join(output,'menu.png')});
  await panel.getByRole('button',{name:'设置',exact:true}).press('Escape');await panel.waitFor({state:'detached'});
  assert.equal(await page.locator('.nf-account-trigger').evaluate(n=>n===document.activeElement),true);
  assert.equal(await page.locator('.nf-account-trigger').evaluate(n=>getComputedStyle(n).outlineStyle),'solid','Keyboard return retains a visible circular focus');
  await open(page);await page.locator('.nf-account-trigger').click();assert.equal(await panel.evaluate(n=>n.hasAttribute('inert')),true);
  await open(page);await page.waitForTimeout(210);assert.equal(await panel.count(),1);
  const settingsHome=page.getByRole('dialog',{name:'设置',exact:true});
  const checkSettingsWidth=async dialog=>assert.equal((await dialog.boundingBox()).width,Math.min(776,width-(width<=620?24:32)));
  await panel.getByRole('button',{name:'设置',exact:true}).click();await settingsHome.waitFor();await settled(page);
  const nav=settingsHome.getByRole('navigation',{name:'设置分类'});
  assert.deepEqual(await nav.getByRole('button').allTextContents(),['通用','AI 记忆','笔记数据','回收站']);
  await checkSettingsWidth(settingsHome);
  assert.equal(await settingsHome.locator('.nf-segment button').count(),2);
  assert.equal(await settingsHome.locator('.nf-segment button[aria-pressed="true"]').evaluate(n=>getComputedStyle(n).backgroundColor),'rgb(251, 250, 248)');
  await settingsHome.screenshot({path:join(output,'settings-'+width+'.png')});
  await settingsHome.getByRole('button',{name:'更换邮箱',exact:true}).click();
  assert.equal(await settingsHome.getByRole('textbox',{name:'登录邮箱地址',exact:true}).inputValue(),'example@example.invalid');
  await nav.getByRole('button',{name:'AI 记忆',exact:true}).click();
  const memoryPage=settingsHome.getByRole('region',{name:'AI 记忆',exact:true});
  const memorySwitch=memoryPage.getByRole('switch',{name:'长期记忆',exact:true});
  await memorySwitch.click();
  await memoryPage.locator('[role="switch"][aria-checked="false"]:enabled').waitFor();
  const claim=memoryPage.getByRole('button',{name:/^称呼：小林/});
  await claim.waitFor();await settled(page);
  assert.match(await claim.textContent(),/2 条记录/);
  assert.equal(await memoryPage.getByRole('heading',{name:'需要核对'}).count(),1);
  assert.equal(await memoryPage.getByRole('heading',{name:'已保存'}).count(),1);
  assert.equal(await memoryPage.getByRole('button',{name:'编辑记忆'}).count(),0);
  assert.equal(await memoryPage.getByRole('button',{name:'删除记忆'}).count(),0);
  assert.doesNotMatch(await memoryPage.textContent(),/Involving:/);
  const memoryLayout=await settingsHome.evaluate(dialog=>{
    const header=dialog.querySelector('header');
    const before=header.getBoundingClientRect().top;
    const scroll=dialog.querySelector('.nf-settings-scroll');
    scroll.scrollTop=scroll.scrollHeight;
    const result={
      headerFixed:scroll.scrollTop>0&&header.getBoundingClientRect().top===before,
      noHorizontalScroll:scroll.scrollWidth===scroll.clientWidth,
    };
    scroll.scrollTop=0;
    return result;
  });
  assert(memoryLayout.headerFixed&&memoryLayout.noHorizontalScroll);
  await settingsHome.screenshot({path:join(output,'memory-'+width+'.png')});
  const search=memoryPage.getByRole('searchbox',{name:'搜索记忆'});
  await search.fill('不存在');
  assert.equal(await memoryPage.getByText('找不到匹配的记忆').count(),1);
  await search.fill('');
  await claim.click();
  await memoryPage.getByText('查看原始记录',{exact:true}).click();
  assert.equal(await memoryPage.getByText(/Involving: user/).isVisible(),true);
  await memoryPage.getByRole('combobox',{name:'查看同内容的记录'}).selectOption('browser-memory-1');
  assert.equal(await memoryPage.getByText('用户的名字是小林，用户更正了自己的称呼。',{exact:true}).isVisible(),true);
  await memoryPage.getByRole('button',{name:'编辑记忆',exact:true}).click();
  assert.doesNotMatch(await memoryPage.getByRole('textbox',{name:'记忆内容'}).inputValue(),/Involving:/);
  await memoryPage.getByRole('button',{name:'取消编辑'}).click();
  await memoryPage.getByRole('button',{name:'删除记忆',exact:true}).click();
  assert.equal(await memoryPage.getByRole('heading',{name:'删除这条记忆？'}).isVisible(),true);
  await memoryPage.getByRole('button',{name:'取消',exact:true}).click();
  await memoryPage.getByRole('button',{name:'返回记忆列表',exact:true}).click();
  assert.equal(await claim.evaluate(n=>n===document.activeElement),true);
  await nav.getByRole('button',{name:'笔记数据',exact:true}).click();
  const dataPage=settingsHome.getByRole('region',{name:'笔记数据',exact:true});
  assert.equal(await dataPage.getByRole('button',{name:'导出知识库备份',exact:true}).count(),1);
  assert.equal(await dataPage.getByRole('button',{name:'导出 Markdown 文件',exact:true}).count(),1);
  assert.match(await dataPage.textContent(),/不含聊天与长期记忆/);
  assert.equal(await dataPage.locator('input[type=file]').evaluate(n=>n.multiple),false);
  await nav.getByRole('button',{name:'回收站',exact:true}).click();
  assert.equal(await settingsHome.getByRole('heading',{name:'回收站',exact:true}).isVisible(),true);
  await page.keyboard.press('Escape');await settingsHome.waitFor({state:'hidden'});
  await open(page);await panel.getByRole('button',{name:/^回收站/}).click();
  await settingsHome.getByRole('heading',{name:'回收站',exact:true}).waitFor();
  await page.keyboard.press('Escape');await settingsHome.waitFor({state:'hidden'});
  await page.evaluate(()=>window.accountTest.update({userName:'很长的名字'.repeat(8),compact:false}));await open(page);
  assert.equal(await page.locator('.nf-account-trigger').evaluate(n=>getComputedStyle(n).borderRadius),'11px','Expanded account row keeps its original styling');
  assert.notEqual(await page.locator('.nf-account-trigger').evaluate(n=>getComputedStyle(n).backgroundColor),'rgba(0, 0, 0, 0)');
  assert.equal(await panel.locator('.nf-username').textContent(),'很长的名字'.repeat(8));
  assert.equal(await panel.evaluate(n=>n.scrollWidth<=n.clientWidth),true);
  assert.equal(await panel.evaluate(n=>n.getBoundingClientRect().width),224);
  const fullTrigger=await page.locator('.nf-account-trigger').boundingBox();
  assert.equal(await panel.evaluate(n=>n.getBoundingClientRect().left),fullTrigger.x+54);
  assert.equal(await panel.evaluate(n=>n.getBoundingClientRect().bottom),fullTrigger.y+fullTrigger.height);
  assert(await panel.evaluate(n=>n.getBoundingClientRect().right)<=width);
  await panel.getByRole('button',{name:'设置',exact:true}).press('Escape');await settled(page);
  await page.evaluate(()=>window.accountTest.update({userName:'示例用户',compact:true,themeMode:'dark'}));await open(page);
  assert.equal(await page.locator('.nf-account-trigger').evaluate(n=>getComputedStyle(n).backgroundColor),'rgba(0, 0, 0, 0)','Dark compact launcher has no plate');
  assert.equal(await panel.evaluate(n=>getComputedStyle(n).backgroundColor),'rgb(37, 43, 51)');
  assert.equal(await panel.locator('.nf-account-heading .nf-avatar').evaluate(n=>n.getBoundingClientRect().width),28);
  await panel.getByRole('button',{name:'设置',exact:true}).press('Escape');await settled(page);
  await open(page);await panel.getByRole('button',{name:'设置',exact:true}).click();await settingsHome.waitFor();await settled(page);
  assert.equal(await settingsHome.evaluate(n=>getComputedStyle(n).backgroundColor),'rgb(37, 43, 51)');
  assert.equal(await settingsHome.locator('.nf-segment button[aria-pressed="true"]').evaluate(n=>getComputedStyle(n).backgroundColor),'rgb(75, 88, 103)');
  if(width===1280)await settingsHome.screenshot({path:join(output,'settings-dark.png')});
  await page.keyboard.press('Escape');await settled(page);
  await page.emulateMedia({reducedMotion:'reduce'});await open(page);
  assert.equal(await panel.evaluate(n=>getComputedStyle(n).animationName),'none');
  assert.equal(await page.locator('.nf-account-trigger .nf-avatar').evaluate(n=>getComputedStyle(n).transitionDuration),'0s');
  await panel.getByRole('button',{name:'退出登录',exact:true}).click();assert.equal(await page.evaluate(()=>window.signOutCount),1);
  await page.setViewportSize({width,height:240});await open(page);
  assert(await panel.evaluate(n=>n.getBoundingClientRect().top)>=0);assert.equal(await panel.evaluate(n=>getComputedStyle(n).overflowY),'auto');
  await page.emulateMedia({forcedColors:'active'});
  assert.equal(await panel.evaluate(n=>getComputedStyle(n).outlineStyle),'solid');
  await panel.getByRole('button',{name:'设置',exact:true}).click();await settingsHome.waitFor();
  assert(await settingsHome.evaluate(n=>n.getBoundingClientRect().height<=240));
  assert(await settingsHome.locator('.nf-settings-scroll').evaluate(n=>n.scrollHeight>n.clientHeight));
  assert.deepEqual(requests,[]);assert.deepEqual(errors,[]);
  reports.push({width,compactWidth:224,rightOfAvatar:true,bottomAligned:true,avatar:28,launcherUnchanged:true,framelessCompactLauncher:true,circularFeedback:true,keyboardFocus:true,expandedRowUnchanged:true,rows:geometry.rows,settingsAndTrash:true,backupScope:true,longName:true,dark:true,reducedMotion:true,shortViewport:true,requests,errors});await context.close();
 }
 console.log(JSON.stringify({actualAccountMenu:true,syntheticOnly:true,output,reports},null,2));
}finally{await browser.close();}
