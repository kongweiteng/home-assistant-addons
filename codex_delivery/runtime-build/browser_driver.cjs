/* Fixed CDP pipe driver. No model expression, executable, account or profile API. */
'use strict';
const fs=require('node:fs');
const readline=require('node:readline');
const input=fs.createReadStream(null,{fd:3,autoClose:false});
const output=fs.createWriteStream(null,{fd:4,autoClose:false});
let cdpSeq=0, fetchSeq=0, config, session, target, initialized=false, cdpBuffer=Buffer.alloc(0);
const cdpPending=new Map(), fetchPending=new Map();
const send=o=>process.stdout.write(JSON.stringify(o)+'\n');
function call(method,params={},sessionId=session) {
 return new Promise((resolve,reject)=>{
  const id=++cdpSeq,timer=setTimeout(()=>{cdpPending.delete(id);reject(Error('cdp_timeout'));},10000);
  cdpPending.set(id,{resolve,reject,timer});
  const message={id,method,params};if(sessionId)message.sessionId=sessionId;
  output.write(JSON.stringify(message)+'\0');
 });
}
function hostFetch(url,method) {
 return new Promise((resolve,reject)=>{
  const id=++fetchSeq;if(id>64)return reject(Error('request_limit'));
  const timer=setTimeout(()=>{fetchPending.delete(id);reject(Error('fetch_timeout'));},10000);
  fetchPending.set(id,{resolve,reject,timer});send({kind:'fetch',id,url,method});
 });
}
async function paused(event) {
 const req=event.request;
 try {
  if(!['GET','HEAD'].includes(req.method)||req.hasPostData||req.postData)throw Error('method');
  if(!['Document','Stylesheet','Script','Image','Font','XHR','Fetch','Other'].includes(event.resourceType))throw Error('resource');
  const response=await hostFetch(req.url,req.method);if(response.error)throw Error('rejected');
  const headers=[{name:'content-type',value:response.content_type},{name:'x-content-type-options',value:'nosniff'},
   {name:'referrer-policy',value:'no-referrer'},
   {name:'content-security-policy',value:"form-action 'none'; object-src 'none'; frame-src 'none'; worker-src 'none'; connect-src http: https:"}];
  if(response.location!==null)headers.push({name:'location',value:response.location});
  await call('Fetch.fulfillRequest',{requestId:event.requestId,responseCode:response.status,responseHeaders:headers,body:response.body});
 }catch(_){try{await call('Fetch.failRequest',{requestId:event.requestId,errorReason:'BlockedByClient'});}catch(_){}}
}
input.on('data',chunk=>{
 cdpBuffer=Buffer.concat([cdpBuffer,chunk]);if(cdpBuffer.length>8*1024*1024)process.exit(2);
 let end;while((end=cdpBuffer.indexOf(0))>=0){
  const line=cdpBuffer.subarray(0,end);cdpBuffer=cdpBuffer.subarray(end+1);
  let value;try{value=JSON.parse(line);}catch(_){process.exit(2);}
  if(value.id){const pending=cdpPending.get(value.id);if(!pending)process.exit(2);
   cdpPending.delete(value.id);clearTimeout(pending.timer);value.error?pending.reject(Error('cdp_error')):pending.resolve(value.result);}
  else if(value.method==='Fetch.requestPaused'&&value.sessionId===session)paused(value.params);
  else if(value.method==='Target.targetCreated'&&target&&value.params.targetInfo.targetId!==target)
   call('Target.closeTarget',{targetId:value.params.targetInfo.targetId},null).catch(()=>{});
 }
});
input.on('end',()=>process.exit(initialized?0:2));
async function evaluate(expression){
 const value=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:false});
 if(value.exceptionDetails||!value.result||!Object.hasOwn(value.result,'value'))throw Error('extract');
 return value.result.value;
}
async function run(c){
 const context=await call('Target.createBrowserContext',{disposeOnDetach:true},null);
 const created=await call('Target.createTarget',{url:'about:blank',browserContextId:context.browserContextId},null);target=created.targetId;
 const attached=await call('Target.attachToTarget',{targetId:target,flatten:true},null);session=attached.sessionId;
 await call('Page.enable');await call('Runtime.enable');
 await call('Network.enable');await call('Network.setBypassServiceWorker',{bypass:true});
 await call('Emulation.setDeviceMetricsOverride',{width:1280,height:900,deviceScaleFactor:1,mobile:false});
 await call('Emulation.setTimezoneOverride',{timezoneId:'Asia/Shanghai'});
 await call('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'}]});
 await call('Target.setDiscoverTargets',{discover:true},null);
 await call('Page.navigate',{url:c.url});
 const deadline=Date.now()+20000;let location='';
 while(Date.now()<deadline){
  try {const state=await evaluate('({ready:document.readyState,url:location.href})');
   if(state.ready==='complete'&&state.url!=='about:blank'&&fetchPending.size===0){location=state.url;break;}}
  catch(_){}await new Promise(resolve=>setTimeout(resolve,100));
 }
 if(!location)throw Error('navigation_timeout');
 const extracted=await evaluate(`({title:document.title.slice(0,4096),text:(document.body?document.body.innerText:'').slice(0,262144),links:Array.from(document.querySelectorAll('a[href]')).slice(0,128).map(a=>({text:a.innerText.slice(0,512),href:a.getAttribute('href').slice(0,4096)}))})`);
 let screenshot=null;if(c.screenshot){const result=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false,fromSurface:true});screenshot=result.data;}
 await new Promise(resolve=>process.stdout.write(JSON.stringify({kind:'result',url:location,title:extracted.title,text:extracted.text,links:extracted.links,screenshot})+'\n',resolve));
 initialized=true;await call('Browser.close',{},null).catch(()=>{});process.exit(0);
}
let hostBuffer=0;
readline.createInterface({input:process.stdin,crlfDelay:Infinity}).on('line',line=>{
 hostBuffer+=Buffer.byteLength(line);if(Buffer.byteLength(line)>8*1024*1024||hostBuffer>32*1024*1024)process.exit(2);
 let message;try{message=JSON.parse(line);}catch(_){process.exit(2);}
 if(!config){config=message;run(config).catch(()=>{send({kind:'error',code:'browser_guest_failed'});process.exit(1);});return;}
 const pending=fetchPending.get(message.id);if(!pending)process.exit(2);
 fetchPending.delete(message.id);clearTimeout(pending.timer);pending.resolve(message);
});
