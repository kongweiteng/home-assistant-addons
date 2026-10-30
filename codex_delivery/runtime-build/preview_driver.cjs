/* Fixed private guest CDP pipe driver. No evaluate, selector or URL input. */
'use strict';
const fs=require('node:fs'),readline=require('node:readline');
const source=fs.createReadStream(null,{fd:3,autoClose:false});
const targetPipe=fs.createWriteStream(null,{fd:4,autoClose:false});
let seq=0,session,target,buffer=Buffer.alloc(0),initialized=false,busy=false,last=0,total=0;
let loaded=null,mainFrame=null,currentLoader=null,renderPending=null;
const renderStates=new Map();
const pending=new Map();
const keys=new Set(['Tab','Enter','Escape','Backspace','Delete','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Home','End']);
const codes={Tab:9,Enter:13,Escape:27,Backspace:8,Delete:46,ArrowUp:38,ArrowDown:40,ArrowLeft:37,ArrowRight:39,Home:36,End:35};
const send=value=>process.stdout.write(JSON.stringify(value)+'\n');
function waitLoad(){
 if(loaded)throw Error('load_pending');
 return new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>{loaded=null;reject(Error('cdp_deadline'));},initialized?10000:60000);
  loaded=()=>{clearTimeout(timer);loaded=null;resolve();};
 });
}
function call(method,params={},sessionId=session){
 return new Promise((resolve,reject)=>{
  const id=++seq,timer=setTimeout(()=>{
   pending.delete(id);reject(Error(method==='Page.reload'?'reload_deadline':method==='Page.captureScreenshot'?'capture_deadline':'cdp_deadline'));
  },initialized?10000:60000);
  pending.set(id,{resolve,reject,timer,method});const message={id,method,params};if(sessionId)message.sessionId=sessionId;
  targetPipe.write(JSON.stringify(message)+'\0');
 });
}
function observeRender(value){
 if(value.sessionId!==session)return;
 const params=value.params||{};
 if(value.method==='Page.frameNavigated'&&params.frame&&!params.frame.parentId){
  mainFrame=params.frame.id;currentLoader=params.frame.loaderId;
  if(renderPending&&currentLoader!==renderPending.previous)renderPending.loader=currentLoader;
 }else if(value.method==='Page.lifecycleEvent'&&renderPending&&params.frameId===mainFrame&&
   typeof params.loaderId==='string'&&params.loaderId!==renderPending.previous&&['load','firstPaint'].includes(params.name)){
  const state=renderStates.get(params.loaderId)||{load:false,firstPaint:false};state[params.name]=true;
  renderStates.set(params.loaderId,state);if(renderStates.size>4)renderStates.delete(renderStates.keys().next().value);
 }
}
function renderReady(value){
 const state=renderStates.get(value.loader);
 return value.loader&&value.loader!==value.previous&&currentLoader===value.loader&&state?.load&&state?.firstPaint;
}
async function waitRender(value){
 const deadline=Date.now()+10000;
 while(!renderReady(value)){
  if(renderPending!==value||Date.now()>=deadline)throw Error('render_deadline');
  await new Promise(resolve=>setTimeout(resolve,25));
 }
 return value.loader;
}
source.on('data',data=>{
 buffer=Buffer.concat([buffer,data]);if(buffer.length>8*1024*1024)process.exit(2);
 let end;while((end=buffer.indexOf(0))>=0){
  let value;try{value=JSON.parse(buffer.subarray(0,end));}catch(_){process.exit(2);}buffer=buffer.subarray(end+1);
  observeRender(value);
  if(value.id){const entry=pending.get(value.id);if(!entry)process.exit(2);pending.delete(value.id);clearTimeout(entry.timer);
   value.error?entry.reject(Error(entry.method==='Page.captureScreenshot'?'capture_failed':'cdp_failed')):entry.resolve(value.result);}
  else if(value.method==='Page.loadEventFired'&&value.sessionId===session&&loaded)loaded();
  else if(value.method==='Page.javascriptDialogOpening'&&value.sessionId===session)call('Page.handleJavaScriptDialog',{accept:false}).catch(()=>{});
  else if(value.method==='Target.targetCreated'&&target&&value.params.targetInfo.type==='page'&&value.params.targetInfo.targetId!==target)
   call('Target.closeTarget',{targetId:value.params.targetInfo.targetId},null).catch(()=>{});
 }
});
source.on('end',()=>process.exit(2));
async function initialize(){
 const context=await call('Target.createBrowserContext',{disposeOnDetach:true},null);
 await call('Browser.setDownloadBehavior',{behavior:'deny',browserContextId:context.browserContextId},null);
 target=(await call('Target.createTarget',{url:'about:blank',browserContextId:context.browserContextId},null)).targetId;
 session=(await call('Target.attachToTarget',{targetId:target,flatten:true},null)).sessionId;
 await call('Page.enable');
 await call('Page.setLifecycleEventsEnabled',{enabled:true});
 await call('Emulation.setDeviceMetricsOverride',{width:1024,height:768,deviceScaleFactor:1,mobile:false});
 await call('Emulation.setTimezoneOverride',{timezoneId:'Asia/Shanghai'});
 await call('Target.setDiscoverTargets',{discover:true},null);
 const load=waitLoad();await call('Page.navigate',{url:'http://127.0.0.1:8080/'});await load;
 await new Promise(resolve=>setTimeout(resolve,150));
 // Initial compositor/font work belongs to bounded VM startup, before the
 // five-second interactive request budget opens.
 const warmDeadline=Date.now()+10000;
 for(;;){
  try{await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false,fromSurface:true,optimizeForSpeed:true});break;}
  catch(error){if(error.message!=='capture_failed'||Date.now()>=warmDeadline)throw error;}
  await new Promise(resolve=>setTimeout(resolve,200));
 }
 if(typeof mainFrame!=='string'||typeof currentLoader!=='string')throw Error('init');
 initialized=true;send({kind:'ready',version:1});
}
async function screenshot(id){
 let result;const deadline=Date.now()+3000;
 for(;;){
  try{result=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false,fromSurface:true,optimizeForSpeed:true});break;}
  catch(error){if(error.message!=='capture_failed'||Date.now()>=deadline)throw error;}
  // A known missing compositor surface during reload permits only another
  // read-only screenshot. The input event is never dispatched a second time.
  await new Promise(resolve=>setTimeout(resolve,100));
 }
 if(typeof result.data!=='string'||result.data.length>5592408)throw Error('pixel_limit');
 return {kind:'frame',id,mime:'image/png',width:1024,height:768,image_b64:result.data};
}
async function action(event){
 if(!event||typeof event.kind!=='string')throw Error('event');
 const names=Object.keys(event).sort().join(',');
 if(event.kind==='click'&&names==='kind,x,y'&&Number.isInteger(event.x)&&event.x>=0&&event.x<1024&&Number.isInteger(event.y)&&event.y>=0&&event.y<768){
  await call('Input.dispatchMouseEvent',{type:'mousePressed',x:event.x,y:event.y,button:'left',clickCount:1});
  await call('Input.dispatchMouseEvent',{type:'mouseReleased',x:event.x,y:event.y,button:'left',clickCount:1});
 }else if(event.kind==='text'&&names==='kind,text'&&typeof event.text==='string'&&event.text.length>=1&&Array.from(event.text).length<=1024&&Buffer.byteLength(event.text)<=4096&&!/[\x00-\x08\x0b-\x1f\x7f]/.test(event.text)){
  await call('Input.insertText',{text:event.text});
 }else if(event.kind==='key'&&names==='key,kind'&&keys.has(event.key)){
  const key={key:event.key,code:event.key,windowsVirtualKeyCode:codes[event.key],nativeVirtualKeyCode:codes[event.key]};
  await call('Input.dispatchKeyEvent',{type:'keyDown',...key});await call('Input.dispatchKeyEvent',{type:'keyUp',...key});
 }else if(event.kind==='scroll'&&names==='delta_y,kind'&&Number.isInteger(event.delta_y)&&event.delta_y>=-720&&event.delta_y<=720){
  await call('Input.dispatchMouseEvent',{type:'mouseWheel',x:512,y:384,deltaX:0,deltaY:event.delta_y});
 }else if(event.kind==='refresh'&&names==='kind'){
  renderStates.clear();renderPending={previous:currentLoader,loader:null};
  await call('Page.reload',{ignoreCache:false});return 'render_pending';
 }
 else throw Error('event');
 await new Promise(resolve=>setTimeout(resolve,150));
}
readline.createInterface({input:process.stdin,crlfDelay:Infinity}).on('line',line=>{
 total+=Buffer.byteLength(line);if(Buffer.byteLength(line)>8192||total>20*1024*1024||busy)process.exit(2);
 let value;try{value=JSON.parse(line);}catch(_){process.exit(2);}
 busy=true;
 (async()=>{
  if(!initialized){if(Object.keys(value).join(',')!=='kind'||value.kind!=='init')throw Error('init');await initialize();return;}
  const names=Object.keys(value).sort().join(',');
  if(!Number.isInteger(value.id)||value.id!==last+1||value.id>4096)throw Error('sequence');
  if(value.kind==='frame'&&names==='id,kind'){}
  else if(value.kind==='action'&&names==='event,id,kind'){
   if(renderPending)throw Error('render_pending');
   const result=await action(value.event);
   if(result==='render_pending'){last=value.id;send({kind:'action_ack',id:value.id,state:'render_pending'});return;}
  }
  else throw Error('request');
  last=value.id;const waiting=renderPending;
  const loader=waiting?await waitRender(waiting):null;
  const pixels=await screenshot(value.id);
  if(waiting&&(!renderReady(waiting)||waiting.loader!==loader))throw Error('render_changed');
  if(waiting){renderPending=null;renderStates.clear();}
  send(pixels);
 })().catch(error=>{
  const code=['cdp_deadline','reload_deadline','capture_deadline','cdp_failed','capture_failed','event','request','sequence','init','load_pending','pixel_limit','render_pending','render_deadline','render_changed'].includes(error.message)?'driver_'+error.message:'driver';
  process.stdout.write(JSON.stringify({kind:'error',code})+'\n',()=>process.exit(1));
 }).finally(()=>{busy=false;});
});
