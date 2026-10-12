// Deterministic browser-client protocol test; no claim of actual browser playback.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('src/smartvoice/web/streaming.html','utf8');
const source=html.split('<script>')[1].split('</script>')[0];
const noop=()=>{};
const elements=new Map();
function element(id){if(!elements.has(id))elements.set(id,{value:'',textContent:'',disabled:false,classList:{toggle:noop,add:noop},append:noop,replaceChildren:noop,setAttribute:noop});return elements.get(id)}
element('file').files=[{size:2560,arrayBuffer:async()=>new ArrayBuffer(2560)}];
element('glossary').value='[]';element('language').value='en';
const document={readyState:'loading',addEventListener:noop,getElementById:element,querySelectorAll:()=>[],
    createDocumentFragment:()=>({append:noop}),createElement:()=>({classList:{toggle:noop},append:noop})};
const server={sequence:0,samples:0,frames:[],connections:0,resumes:0,ack:0,finished:false};
class Socket{
 static OPEN=1;
 constructor(){this.readyState=0;this.bufferedAmount=0;server.connections++;setTimeout(()=>{this.readyState=1;this.onopen()},0)}
 message(row){queueMicrotask(()=>{if(this.readyState===1)this.onmessage({data:JSON.stringify(row)})})}
 send(data){
  assert.equal(this.readyState,1);
  if(typeof data!=='string'){
   const header=this.header;this.header=null;assert(header);
   assert.equal(header.sequence,server.frames.length+1);assert.equal(header.start_sample,server.samples);
   server.frames.push(header.sequence);server.samples+=data.byteLength/2;
   if(server.frames.length<=2){
    // Accept input but lose its ACK. Output is durable on the client before loss.
    this.message({type:'source_unit_partial',event_sequence:++server.sequence,unit_id:'u',revision:server.frames.length,text:server.frames.length===1?'Hello':'Hello again',committed:false});
    setTimeout(()=>this.close(),5);
   }else this.message({type:'input_ack',input_sequence:server.frames.length,accepted_samples:server.samples});
   return;
  }
  const row=JSON.parse(data);
  if(row.type==='configure'){
   assert.equal(row.resumable,true);
   this.message({type:'session_ready',session_id:'s',event_sequence:++server.sequence,
      recovery:{resume_token:'secret',grace_seconds:20},heartbeat_interval_seconds:20});
  }else if(row.type==='resume'){
   server.resumes++;assert.equal(row.session_id,'s');assert.equal(row.resume_token,'secret');assert.equal(row.last_event_sequence,server.ack);
   this.message({type:'session_resumed',session_id:'s',input_sequence:server.frames.length,
       accepted_samples:server.samples,input_finished:false,context_version:1,heartbeat_interval_seconds:20});
  }else if(row.type==='audio_frame')this.header=row;
  else if(row.type==='ack')server.ack=row.event_sequence;
  else if(row.type==='finish'){
   server.finished=true;this.message({type:'session_complete',status:'complete',event_sequence:++server.sequence,counts:{}});
   setTimeout(()=>this.close(),10);
  }
 }
 close(){if(this.readyState===3)return;this.readyState=3;setTimeout(()=>this.onclose?.(),0)}
}
class AudioContext{
 currentTime=0;
 async resume(){}
 async close(){}
 async decodeAudioData(){return {duration:.08}}
}
class OfflineAudioContext{
 destination={};
 createBufferSource(){return {connect:noop,start:noop}}
 async startRendering(){return {getChannelData:()=>new Float32Array(1280)}}
}
const sandbox={document,location:{search:'',hash:'',protocol:'http:',host:'localhost'},
    URLSearchParams,performance,WebSocket:Socket,AudioContext,OfflineAudioContext,
    setTimeout,clearTimeout,setInterval,clearInterval,queueMicrotask,console,window:{addEventListener:noop},
    localStorage:{getItem:()=>null},AbortController,fetch:async()=>{throw Error('unexpected fetch')}};
vm.createContext(sandbox);vm.runInContext(source,sandbox);
vm.runInContext("capabilitiesLoaded=true;capabilities={enabled:true,limits:{},chains:[{mode:'transcription',source_language:'en',available:true}]};start()",sandbox);
const deadline=setTimeout(()=>{console.error('Browser recovery harness timed out');process.exit(1)},5000);
(async()=>{
 while(vm.runInContext('starting',sandbox))await new Promise(resolve=>setTimeout(resolve,20));
 clearTimeout(deadline);
 assert.equal(server.connections,3);assert.equal(server.resumes,2);
 assert.deepEqual(server.frames,[1,2,3,4]);assert.equal(server.samples,1280);assert(server.finished);
 assert.equal(vm.runInContext('canceled',sandbox),false);
 assert.equal(vm.runInContext('inputPending.size',sandbox),0);
 assert.equal(vm.runInContext('lastEventSequence',sandbox),server.sequence);
 assert.equal(vm.runInContext("sourceRows.get('u').text",sandbox),'Hello again');
 await vm.runInContext(`(async()=>{
  const sent=[],nodes=[];
  ws={readyState:WebSocket.OPEN,send:value=>sent.push(JSON.parse(value))};
  context={currentTime:0,destination:{},decodeAudioData:async()=>({duration:.1}),
    createBufferSource:()=>{const node={connect:()=>{},start:()=>{},stop:()=>{}};nodes.push(node);return node}};
  lastAudioPlayed=0;audioDescriptor=null;nextAudioTime=0;
  const descriptor={type:'audio_segment',audio_sequence:1,event_sequence:lastEventSequence+1,audio_bytes:4,text:'Hello'};
  const deliver=async()=>{
    await handle({data:JSON.stringify(descriptor)},performance.now(),run);
    await handle({data:new ArrayBuffer(4)},performance.now(),run);
  };
  await deliver();if(nodes.length!==1)throw Error('Initial playback was not scheduled');
  const old=nodes[0];stopNodes();old.onended();
  if(lastAudioPlayed!==0||sent.some(row=>row.type==='audio_played'))throw Error('Stale playback callback advanced completed audio');
  await deliver();nodes[1].onended();
  if(lastAudioPlayed!==1||sent.filter(row=>row.type==='audio_played').length!==1)throw Error('Replayed chunk did not complete once');
  await deliver();if(nodes.length!==2)throw Error('Already completed audio replayed');
  const previous=transportGeneration++;audioDescriptor=null;
  await handle({data:JSON.stringify(descriptor)},performance.now(),run,previous);
  if(audioDescriptor!==null)throw Error('Old transport descriptor was accepted');
  descriptor.audio_sequence=2;descriptor.event_sequence++;
  let resolveDecode;context.decodeAudioData=()=>new Promise(resolve=>resolveDecode=resolve);
  await handle({data:JSON.stringify(descriptor)},performance.now(),run);
  const pending=handle({data:new ArrayBuffer(4)},performance.now(),run);
  stopNodes();resolveDecode({duration:.1});await pending;
  if(nodes.length!==2||lastAudioPlayed!==1)throw Error('Stale asynchronous decode scheduled playback');
 })()`,sandbox);
 console.log('Browser client resumed twice after lost input ACKs; revised subtitle retained, no input gap or duplicate');
 console.log('Playback callback/decode/transport isolation and whole completed chunk deduplication passed');
})().catch(error=>{console.error(error);process.exit(1)});
