// Exercises only the caller's dedicated fake Codex home, never real user cleanup.
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
const bundled=path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const {chromium}=require(process.env.CLEANUP_PLAYWRIGHT_MODULE||(fs.existsSync(bundled)?bundled:'playwright'));
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:319,height:690}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.PANEL_TEST_URL);
  await page.waitForFunction(()=>document.querySelector('#job').textContent.includes('已就绪'));
  for(const viewport of [{width:319,height:690},{width:768,height:600},{width:1280,height:800}]){
   await page.setViewportSize(viewport);
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   await page.locator('#scan').click();
   await page.waitForFunction(()=>document.querySelector('#job').textContent.startsWith('检查完成'));
   assert.equal(await page.locator('#eligible').textContent(),'2');
   assert.ok(await page.locator('#elapsed').textContent());
  }
  const previous=await page.locator('#report').textContent();
  await page.locator('#scan').click();
  await page.waitForFunction(p=>document.querySelector('#report').textContent!==p,previous);
  assert.equal(await page.locator('dialog[open]').count(),0);
  await page.setViewportSize({width:319,height:690});
  await page.locator('#select-all').click();
  assert.equal(await page.locator('#candidates input:checked').count(),2);
  await page.locator('#select-none').click();
  assert.equal(await page.locator('#candidates input:checked').count(),0);
  await page.locator('body').click({position:{x:3,y:3}});
  await page.keyboard.press('Control+Alt+a');
  assert.equal(await page.locator('#candidates input:checked').count(),2);
  await page.locator('#recycle').click();
  await page.locator('#cleanup-dialog[open]').waitFor();
  assert.equal(await page.locator('#cleanup-confirm').isDisabled(),true);
  assert.equal(await page.locator('#cleanup-consent').isChecked(),false);
  assert.equal(await page.locator('#sync-consent').isChecked(),false);
  assert.ok(await page.evaluate(()=>['cleanup-close','cleanup-cancel','cleanup-consent','cleanup-confirm'].every(id=>{const r=document.getElementById(id).getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight;})));
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('dialog[open]').count(),0);
  await page.locator('#recycle').click();await page.locator('#cleanup-dialog[open]').waitFor();
  await page.locator('#cleanup-consent').check();
  assert.equal(await page.locator('#cleanup-confirm').isDisabled(),true);
  await page.locator('#sync-consent').check();await page.locator('#cleanup-confirm').click();
  await page.waitForFunction(()=>document.querySelector('#job').textContent==='已移入回收站，记录中可以恢复。回收站仍占空间。',{},{timeout:30000});
  assert.equal(await page.locator('#candidates input').count(),0);
  await page.getByText('清理与恢复记录',{exact:true}).click();
  await page.locator('#history [data-record-kind="files"]').getByRole('button',{name:'查看恢复清单',exact:true}).click();
  await page.locator('#consent').check();await page.locator('#restore-confirm').click();
  await page.waitForFunction(()=>document.querySelector('#job').textContent.includes('恢复操作结束'));

  await page.locator('#history [data-record-kind="legacy"]').getByRole('button',{name:'查看恢复清单',exact:true}).click();
  const visible=await page.evaluate(()=>['close-dialog','consent','cancel','restore-confirm'].map(id=>{
   const r=document.getElementById(id).getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight&&r.left>=0&&r.right<=innerWidth;
  }));
  assert.ok(visible.every(Boolean));
  assert.equal(await page.locator('#restore-confirm').isDisabled(),true);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('dialog[open]').count(),0);
  await page.locator('#history [data-record-kind="legacy"]').getByRole('button',{name:'查看恢复清单',exact:true}).click();
  await page.locator('#consent').check();await page.locator('#restore-confirm').click();
  await page.waitForFunction(()=>document.querySelector('#job').textContent.includes('恢复操作结束'));
  assert.deepEqual(errors,[]);
  console.log('Passed responsive 319/768/1280 widths, repeat Codex-only scans, no cleanup, explicit recovery');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
