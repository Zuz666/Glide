#!/usr/bin/env python3
"""Exercise Edge's persistent subprocess supervisor through its public request/event boundary.

A local JSONL child replaces the external Python worker. No service, keys, user config or sound device is used.
"""
import os
import subprocess
import sys
import tempfile

import desktop_ts

if not desktop_ts.node or not desktop_ts.available():
    print("SKIP: needs node and desktop/node_modules")
    sys.exit(77)

RUN = r"""
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const main = process.argv[2], root = process.argv[3];
let scenario = 'normal';
const secret = 'PRIVATE-TOKEN-DO-NOT-LOG', text = 'PRIVATE-PLAYER-TEXT-DO-NOT-LOG';
const child = `
  const readline = require('node:readline');
  const scenario = process.argv[1], audit = process.argv[2];
  const fs = require('node:fs');
  fs.appendFileSync(audit, JSON.stringify({op:'spawn', pid:process.pid})+'\\n');
  const emit = x => process.stdout.write(JSON.stringify(x)+'\\n');
  const requests = new Map();
  const ready = () => emit({type:'ready'});
  if (scenario === 'startup' || scenario === 'startup-deadline') setTimeout(ready, scenario === 'startup' ? 160 : 15500);
  else ready();
  readline.createInterface({input:process.stdin}).on('line', line => {
    const c = JSON.parse(line); fs.appendFileSync(audit, JSON.stringify(c)+'\\n');
    if (c.op === 'shutdown') { process.exit(); return; }
    if (c.op === 'start') {
      requests.set(c.id,c);
      if (scenario === 'crash') {
        process.stderr.write('Traceback '+${JSON.stringify(secret)}+' '+c.text+' wss://service/?token=SECRET\\n');
        process.exit(9); return;
      }
      if (scenario === 'malformed') { process.stdout.write('not-json PRIVATE-TOKEN-DO-NOT-LOG\\n'); return; }
      if (scenario === 'oversize') { process.stdout.write('x'.repeat(17*1024*1024)); return; }
      if (scenario === 'hang') return;
      if (scenario === 'unsafe-error') {
        emit({type:'diagnostic',id:c.id,event:'failed',url:'wss://secret/',error:c.text,token:'SECRET'});
        emit({type:'diagnostic',id:c.id,event:'turn_failed',index:0,attempt:'A-1',pool:'A',
          code:'wss://secret/?token='+${JSON.stringify(secret)}+' '+c.text,audio_bytes:0,draining:false});
        emit({type:'end',id:c.id,reason:'failed',error:'wss://service/?token=PRIVATE-TOKEN-DO-NOT-LOG '+c.text}); return;
      }
      if (scenario === 'turn-failures') {
        emit({type:'diagnostic',event:'connection_ready',pool:'A',slot:0,connections:3,renewals:0,connect_failures:0,retired:0});
        emit({type:'diagnostic',event:'connection_ready',pool:'B',slot:0,connections:3,renewals:0,connect_failures:0,retired:0});
        emit({type:'diagnostic',id:c.id,event:'turn_failed',index:0,attempt:'A-1',pool:'A',code:'zero_audio_reset',audio_bytes:0,draining:false});
        emit({type:'diagnostic',id:c.id,event:'turn_failed',index:0,attempt:'B-1',pool:'B',code:'first_audio_timeout',audio_bytes:0,draining:false});
        emit({type:'diagnostic',id:c.id,event:'turn_failed',index:0,attempt:'A-2',pool:'A',code:'zero_audio_reset',audio_bytes:0,draining:false});
        emit({type:'diagnostic',id:c.id,event:'turn_failed',index:0,attempt:'B-2',pool:'B',code:'stream_stall',audio_bytes:12,draining:false});
        emit({type:'end',id:c.id,reason:'failed'}); return;
      }
      if (scenario === 'hedged-drain') {
        for (let s=0;s<3;s++) {
          emit({type:'diagnostic',event:'connection_ready',pool:'A',slot:s,connections:s+1,renewals:0,connect_failures:0,retired:0});
          emit({type:'diagnostic',event:'connection_ready',pool:'B',slot:s,connections:s+1,renewals:0,connect_failures:0,retired:0});
        }
        emit({type:'diagnostic',id:c.id,event:'turn_failed',index:0,attempt:'A-1',pool:'A',code:'zero_audio_reset',audio_bytes:0,draining:false});
      }
      if (c.id === 'mid-stream-fail') {
        emit({type:'plan',id:c.id,fragments:[{index:0,start:0,end:3},{index:1,start:3,end:c.text.length}]});
        emit({type:'candidate',id:c.id,index:0,attempt:'A-1',start:0,end:3,audio:'AQID'});
        setTimeout(() => emit({type:'end',id:c.id,reason:'failed'}), 30);
        return;
      }
      emit({type:'plan',id:c.id,fragments:[{index:0,start:0,end:c.text.length}]});
      emit({type:'candidate',id:c.id,index:0,attempt:'A-1',start:0,end:c.text.length,audio:'AQID'});
      if (scenario === 'duplicate') emit({type:'candidate',id:c.id,index:0,attempt:'A-1',start:0,end:c.text.length,audio:'AQID'});
      if (scenario === 'premature') emit({type:'end',id:c.id,reason:'complete'});
    }
    if (c.op === 'decoded') {
      if (c.ok) {
        emit({type:'accepted',id:c.id,index:0,attempt:c.attempt});
        emit({type:'end',id:c.id,reason:'complete'});
        if (scenario === 'hedged-drain') {
          setTimeout(() => {
            emit({type:'diagnostic',id:c.id,event:'turn_failed',index:0,attempt:'B-1',pool:'B',code:'zero_audio_reset',audio_bytes:0,draining:true});
            emit({type:'diagnostic',id:c.id,event:'turn_failed',index:0,attempt:'B-2',pool:'B',code:c.id+' '+${JSON.stringify(secret)},audio_bytes:0,draining:true});
            emit({type:'diagnostic',id:c.id,event:'turn_failed',index:0,attempt:${JSON.stringify(secret)},pool:'B',code:'zero_audio_reset',audio_bytes:0,draining:true});
          }, 15);
        } else {
          emit({type:'end',id:c.id,reason:'failed'});
          if (c.id === 'owned') {
            setTimeout(() => {
              emit({type:'diagnostic',event:'connection_ready',pool:'A',slot:1,connections:2,renewals:0,connect_failures:0,retired:0});
            }, 60);
          }
        }
      } else {
        const r=requests.get(c.id);
        emit({type:'candidate',id:c.id,index:0,attempt:'B-1',start:0,end:r.text.length,audio:'BAUG'});
      }
    }
    if (c.op === 'cancel') {
      emit({type:'end',id:c.id,reason:'cancelled'});
      emit({type:'candidate',id:c.id,index:0,attempt:'late',start:0,end:1,audio:'AQID'});
    }
  }).on('close',()=>process.exit());
`;
const audit = path.join(root,'audit.jsonl');
const paths = path.join(main,'paths.js');
require.cache[paths] = { id:paths, filename:paths, loaded:true, exports:{
  pylibDir:()=>'', pythonCommand:()=>({cmd:scenario==='missing'?path.join(root,'absent-python'):process.execPath,
    args:['-e',child,'--',scenario,audit],bundled:false}),
} };
const {EdgeTts}=require(path.join(main,'edge-tts.js'));
const pause=ms=>new Promise(r=>setTimeout(r,ms));
async function until(predicate) {
  const deadline=performance.now()+2000;
  while (!(await predicate())) {
    assert(performance.now()<deadline,'expected external child/IPC event did not arrive');
    await pause(5);
  }
}
const readAudit=()=>fs.existsSync(audit)?fs.readFileSync(audit,'utf8').trim().split('\n').filter(Boolean).map(JSON.parse):[];
const logs=[];
const host=()=>new EdgeTts(()=>path.join(root,'edge_tts_worker.py'),(line,a)=>logs.push([line,a]));
function request(h,id,body=text,decode=true) {
  const events=[]; let resolve;
  const ended=new Promise(r=>resolve=r);
  h.start(id,body,'ru-RU-SvetlanaNeural',e=>{
    events.push(e);
    if (e.type==='candidate' && decode) h.decoded(id,e.attempt,true);
    if(e.type==='end') resolve(e);
  });
  return {events,ended};
}
(async()=>{
  let h=host(); h.configure(true);
  let first=request(h,'one'); assert.equal((await first.ended).reason,'complete');
  assert.deepEqual(first.events.map(e=>e.type),['plan','candidate','accepted','end']);
  assert(first.events[1].data instanceof Uint8Array); assert.deepEqual([...first.events[1].data],[1,2,3]);
  assert.equal(first.events[1].mime,'audio/mpeg'); assert(!('audio' in first.events[1]));
  const spawns=readAudit().filter(c=>c.op==='spawn').length;
  assert.throws(()=>request(h,'one'),/duplicate/i,'completed IDs cannot collide with in-flight loser drain');
  const second=request(h,'two'); assert.equal((await second.ended).reason,'complete');
  assert.equal(readAudit().filter(c=>c.op==='spawn').length,spawns,'reuse one worker');
  h.close();

  scenario='startup'; h=host();
  first=request(h,'startup'); assert.equal((await first.ended).reason,'complete');
  const command=readAudit().find(c=>c.op==='start'&&c.id==='startup');
  assert(command.budget_ms<14900 && command.budget_ms>0,'startup consumes the same deadline'); h.close();

  scenario='normal'; h=host(); first=request(h,'retry',text,false);
  await until(()=>first.events.some(e=>e.type==='candidate'));
  h.decoded('retry','A-1',false);
  await until(()=>first.events.filter(e=>e.type==='candidate').length===2);
  h.decoded('retry','B-1',true); assert.equal((await first.ended).reason,'complete');
  assert.equal(first.events.find(e=>e.type==='accepted').attempt,'B-1'); h.close();

  h=host(); first=request(h,'cancel',text,false);
  await until(()=>first.events.some(e=>e.type==='candidate'));
  assert.throws(()=>request(h,'cancel'),/duplicate/i);
  h.cancel('cancel'); assert.equal((await first.ended).reason,'cancelled');
  await pause(25); assert.equal(first.events.filter(e=>e.type==='end').length,1);
  assert.equal(first.events.filter(e=>e.type==='candidate').length,1);
  const again=request(h,'after-cancel'); assert.equal((await again.ended).reason,'complete'); h.close();

  h=host(); first=request(h,'mute',text,false); h.configure(false);
  assert.equal((await first.ended).reason,'cancelled'); h.configure(true);
  const enabled=request(h,'enabled'); assert.equal((await enabled.ended).reason,'complete'); h.close();

  for(const name of ['crash','missing','malformed','oversize','unsafe-error','premature','duplicate']) {
    scenario=name; h=host(); first=request(h,'failure-'+name,text,name!=='premature'&&name!=='duplicate');
    assert.equal((await first.ended).reason,'failed',name);
    await pause(10); assert.equal(first.events.filter(e=>e.type==='end').length,1,name);
    for(const value of [JSON.stringify(first.events),JSON.stringify(logs)]) {
      for(const forbidden of [secret,text,'wss://service','wss://secret','token=']) assert(!value.includes(forbidden),name+' privacy');
    }
    h.close();
  }

  // Active turn_failed codes appear on Edge failed & Edge complete, and post-completion draining failures log separately.
  logs.length=0; scenario='turn-failures'; h=host();
  first=request(h,'all-failed'); assert.equal((await first.ended).reason,'failed');
  const failedLog=logs.find(([l])=>l.includes('Edge failed')&&l.includes('"all-failed"'));
  assert(failedLog,'expected Edge failed log line');
  assert(failedLog[0].includes('failed_turns=4'),failedLog[0]);
  assert(failedLog[0].includes('failure_codes="zero_audio_reset:2,first_audio_timeout:1,stream_stall:1"'),failedLog[0]);
  assert(failedLog[0].includes('Edge TTS: synthesis failed (zero_audio_reset:2,first_audio_timeout:1,stream_stall:1)'),failedLog[0]);
  h.close();

  logs.length=0; scenario='hedged-drain'; h=host();
  first=request(h,'hedged-win'); assert.equal((await first.ended).reason,'complete');
  const completeLog=logs.find(([l])=>l.includes('Edge complete')&&l.includes('"hedged-win"'));
  assert(completeLog && completeLog[0].includes('failed_turns=1') && completeLog[0].includes('failure_codes="zero_audio_reset:1"'),
    'hedged Edge complete must preserve pre-completion failed_turns');
  await until(()=>logs.some(([l])=>l.includes('Edge drain failed')&&l.includes('"hedged-win"')));
  const drainLog=logs.find(([l])=>l.includes('Edge drain failed')&&l.includes('"hedged-win"'));
  assert(drainLog[0].includes('fragment=0') && drainLog[0].includes('attempt="B-1"') && drainLog[0].includes('pool="B"')
    && drainLog[0].includes('code="zero_audio_reset"') && drainLog[0].includes('audio_bytes=0'), drainLog[0]);
  assert.deepEqual(drainLog[1], {
    category:'TTS', operation:'Edge loser drain', provider:'edge',
    request_id:'hedged-win', voice:'ru-RU-SvetlanaNeural', status:'drain_failed',
  });
  for (const forbidden of [secret, text]) assert(!JSON.stringify(logs).includes(forbidden));
  h.close();
  scenario='normal'; h=host();
  assert.throws(()=>request(h,'bad','x'.repeat(4001)),/4000|limit/i);
  assert.throws(()=>h.start('voice',text,'',()=>{}),/voice/i);
  first=request(h,'unfinished',text,false);
  h.close(); assert.equal((await first.ended).reason,'cancelled');

  // Exercise the public IPC registration against window/frame boundaries, using the same real child supervisor.
  scenario='normal'; h=host();
  const {EventEmitter}=require('node:events');
  const Module=require('node:module'), load=Module._load, handlers=new Map();
  const electron={app:{},dialog:{},shell:{},ipcMain:{handle:(name,fn)=>handlers.set(name,fn)}};
  const cache=(relative,exports)=>{
    const file=path.join(main,relative+'.js');
    require.cache[file]={id:file,filename:file,loaded:true,exports};
  };
  const settings={current:{muted:false,speech:{provider:'edge',edge:{voice:'ru-RU-SvetlanaNeural'}}},
    merge(patch){this.current={...this.current,...patch};}};
  let storedKeys={};
  cache('app/settings',{settings});
  cache('app/config',{engineHome:()=>root,keyValue:k=>storedKeys[k]||'',endpointKeyEnvs:()=>[],engineOptions:()=>({}),
    configPath:()=>path.join(root,'config.json'),readConfig:()=>({}),saveConfig:async p=>p});
  cache('secrets',{KNOWN_KEYS:[],keyStatus:()=>[],setKey:async(k,v)=>{storedKeys[k]=v;}});
  let synthImpl = async () => Buffer.from([1,2,3]);
  cache('tts',{TTS_KEYS:{openai:'OPENAI_API_KEY',elevenlabs:'ELEVENLABS_API_KEY',azure:'AZURE_SPEECH_KEY',google:'GOOGLE_TTS_API_KEY'},
    synth:(...args)=>synthImpl(...args),voices:async()=>[]});
  for(const dep of ['addon','speech','store']) cache(dep,dep==='store'?{appSettingsProblem:()=>undefined}:{});
  Module._load=function(name,...args){return name==='electron'?electron:load.call(this,name,...args);};
  let registerIpc;
  try { ({registerIpc}=require(path.join(main,'app','ipc.js'))); } finally { Module._load=load; }
  const engine=new EventEmitter(); engine.note=(line,attr)=>logs.push([line,attr]);
  const {OWN_PAGES}=require(path.join(main,'app','assets.js'));
  class Window extends EventEmitter {
    constructor(){super();this.mainFrame={url:OWN_PAGES[0]};this.events=[];this.statuses=[];this.destroyed=false;}
    isDestroyed(){return this.destroyed;}
    send(channel,event){
      if (channel==='tts:edgeEvent') { this.events.push(event); return; }
      if (channel==='tts:status') { this.statuses.push(event); return; }
      if (channel==='app:settings') return;
      assert.fail('unexpected window event channel: '+channel);
    }
  }
  const a=new Window(),b=new Window();
  const broadcast=(channel,payload)=>{
    assert.notEqual(channel,'tts:edgeEvent','Edge audio events must never be broadcast');
    a.send(channel,payload); b.send(channel,payload);
  };
  let winSpeakHook=async()=>({voice:'Xenia (Russian)'});
  const windowsTts={stop(){},speak:(id,t,opts)=>winSpeakHook(id,t,opts),voices:async()=>[]};
  registerIpc({engine,edgeTts:h,windowsTts,updater:{},sampler:{},speech:{},mainWindow:a,overlay:b,
    broadcast,setMuted(m){settings.current.muted=m;h.configure(!m&&settings.current.speech?.provider==='edge');broadcast('app:settings',settings.current);},
    pushMute(){},installUpdate(){},applyOverlaySettings(){broadcast('app:settings',settings.current);},updateTray(){},ensureSpeech:async()=>{}});
  const invoke=(sender,channel,...args)=>handlers.get(channel)({sender,senderFrame:sender.mainFrame},...args);
  const spoof={sender:a,senderFrame:{url:OWN_PAGES[0]}};
  assert.throws(()=>handlers.get('tts:edgeStart')(spoof,'frame','speech'),/own pages/);
  const outside=new Window(); outside.mainFrame.url='https://untrusted.invalid/';
  assert.throws(()=>invoke(outside,'tts:edgeStart','outside','speech'),/own pages/);
  invoke(a,'tts:edgeStart','owned','speech');
  assert.throws(()=>invoke(b,'tts:edgeStart','owned','different'),/duplicate/i);
  assert.throws(()=>invoke(b,'tts:edgeDecoded','owned','A-1',true),/another window/i);
  assert.throws(()=>invoke(b,'tts:edgeCancel','owned'),/another window/i);
  await until(()=>a.events.some(e=>e.type==='candidate'));
  assert.equal(b.events.length,0,'the other authorized window receives no Edge audio');
  invoke(a,'tts:edgeDecoded','owned','A-1',true);
  await until(()=>a.events.some(e=>e.type==='end'));
  assert.equal(a.events.at(-1).reason,'complete');
  assert.equal(a.listenerCount('did-start-navigation'),0,'terminal requests release sender listeners');

  // Both main window (a) and overlay window (b) receive synchronized tts:status broadcasts across providers.
  const initStatus = await invoke(a, 'tts:status:get');
  assert.equal(initStatus.provider, 'edge');
  assert.equal(initStatus.voiceLabel, 'Edge · Svetlana');
  await invoke(a, 'tts:playbackState', { requestId: 'owned', state: 'playing' });
  assert.equal((await invoke(b, 'tts:status:get')).phase, 'speaking');
  assert(b.statuses.some(s => s.phase === 'generating') && b.statuses.some(s => s.phase === 'speaking'),
    'overlay receives generating and speaking tts:status broadcasts');
  await invoke(a, 'tts:playbackState', { requestId: 'owned', state: 'ended' });
  await invoke(a, 'tts:playbackState', { requestId: 'owned', state: 'fallback', fallbackVoice: 'Xenia (Russian)' });
  const fbStatus = await invoke(b, 'tts:status:get');
  assert.equal(fbStatus.phase, 'fallback');
  assert.equal(fbStatus.fallbackVoice, 'Xenia');
  await invoke(a, 'tts:playbackState', { requestId: 'owned', state: 'ended' });
  const heldFb = await invoke(a, 'tts:status:get');
  assert.equal(heldFb.phase, 'fallback');
  assert.equal(heldFb.holding, true);
  assert.equal(heldFb.readySlots, 0);

  const heldAt = heldFb.updatedAt;
  // 1) Real pool-slot diagnostic arriving mid-hold must republish readySlots while preserving the original hold updatedAt.
  await until(async () => (await invoke(b, 'tts:status:get')).readySlots === 1);
  const heldAfterSlot = await invoke(b, 'tts:status:get');
  assert.equal(heldAfterSlot.readySlots, 1);
  assert.equal(b.statuses.at(-1).readySlots, 1);
  assert.equal(heldAfterSlot.phase, 'fallback');
  assert.equal(heldAfterSlot.holding, true);
  assert.equal(heldAfterSlot.updatedAt, heldAt, 'pool slot diagnostic during hold must not reset hold updatedAt');
  assert.equal(b.statuses.at(-1).updatedAt, heldAt);

  // 2) Start playback A FIRST, then queue prefetch B SECOND: audible speaking A must still outrank later-inserted generating B.
  await invoke(a, 'tts:playbackState', { requestId: 'active-speak', state: 'playing' });
  invoke(a, 'tts:edgeStart', 'bg-prefetch', 'queued while speaking');
  assert.equal((await invoke(b, 'tts:status:get')).phase, 'speaking',
    'audible speaking A must outrank later-inserted prefetch B');
  invoke(a, 'tts:edgeCancel', 'bg-prefetch');

  // 3) Renderer crash clears orphaned speaking state and releases status listeners.
  const crashWin = new Window();
  await invoke(crashWin, 'tts:playbackState', { requestId: 'crash-speak', state: 'playing' });
  await invoke(a, 'tts:playbackState', { requestId: 'active-speak', state: 'ended' });
  assert.equal((await invoke(b, 'tts:status:get')).phase, 'speaking');
  crashWin.emit('render-process-gone', {}, { reason: 'crashed' });
  assert.equal(crashWin.listenerCount('did-start-navigation'), 0);
  assert.notEqual((await invoke(b, 'tts:status:get')).phase, 'speaking',
    'renderer crash must clear orphaned playback state');

  // 4) Mid-stream Edge failure while head fragment is still audibly speaking (> 250 ms) must stay speaking, not error.
  invoke(a, 'tts:edgeStart', 'mid-stream-fail', 'Hi there');
  await until(() => a.events.some(e => e.id === 'mid-stream-fail' && e.type === 'candidate'));
  invoke(a, 'tts:edgeDecoded', 'mid-stream-fail', 'A-1', true);
  await invoke(a, 'tts:playbackState', { requestId: 'mid-stream-fail', state: 'playing' });
  await until(() => a.events.some(e => e.id === 'mid-stream-fail' && e.type === 'end' && e.reason === 'failed'));
  await pause(300);
  assert.equal((await invoke(b, 'tts:status:get')).phase, 'speaking',
    'mid-stream synthesis failure must not replace active head playback with error');
  await invoke(a, 'tts:playbackState', { requestId: 'mid-stream-fail', state: 'fallback', fallbackVoice: 'Xenia (Russian)' });
  assert.equal((await invoke(b, 'tts:status:get')).phase, 'fallback');
  await invoke(a, 'tts:playbackState', { requestId: 'mid-stream-fail', state: 'ended' });
  // OpenAI missing key -> no-key, setting key -> ready -> generating -> speaking -> ready.
  await invoke(a, 'app:settings:set', { speech: { provider: 'openai', openai: { voice: 'alloy' } } });
  assert.equal((await invoke(a, 'tts:status:get')).phase, 'no-key');
  assert.equal((await invoke(a, 'tts:status:get')).voiceLabel, 'OpenAI · alloy');
  await invoke(a, 'keys:set', 'OPENAI_API_KEY', 'sk-test-local-only');
  assert.equal((await invoke(b, 'tts:status:get')).phase, 'ready');
  await invoke(a, 'tts:synth', 'Hello coach', '', '', 'online-1');
  await invoke(a, 'tts:playbackState', { requestId: 'online-1', state: 'playing' });
  assert.equal((await invoke(b, 'tts:status:get')).phase, 'speaking');
  await invoke(a, 'tts:playbackState', { requestId: 'online-1', state: 'ended' });
  assert.equal((await invoke(b, 'tts:status:get')).phase, 'ready');
  // Cancelling a slow online synth before its HTTP promise rejects must not resurrect an error hold.
  let rejectSlowSynth;
  synthImpl = () => new Promise((_, rej) => { rejectSlowSynth = rej; });
  const slowSynthPromise = invoke(a, 'tts:synth', 'Slow line', '', '', 'cancelled-synth').catch(() => {});
  await until(async () => (await invoke(b, 'tts:status:get')).phase === 'generating');
  await invoke(a, 'tts:playbackState', { requestId: 'cancelled-synth', state: 'ended' });
  assert.equal((await invoke(b, 'tts:status:get')).phase, 'ready');
  rejectSlowSynth(new Error('late network error'));
  await slowSynthPromise;
  await pause(20);
  assert.equal((await invoke(b, 'tts:status:get')).phase, 'ready',
    'late failure of cancelled synth must not trigger error hold');
  synthImpl = async () => Buffer.from([1,2,3]);

  // Restore Edge provider for the remaining lifecycle/settings tests below.
  await invoke(a, 'app:settings:set', { speech: { provider: 'edge', edge: { voice: 'ru-RU-SvetlanaNeural' } } });
  for(const lifecycle of ['navigation','crash','destroy']) {
    const sender=new Window(),id='page-'+lifecycle;
    invoke(sender,'tts:edgeStart',id,'must not survive the page');
    if(lifecycle==='navigation') sender.emit('did-start-navigation',{},'https://untrusted.invalid/',false,true);
    else if(lifecycle==='crash') sender.emit('render-process-gone',{},{reason:'crashed'});
    else {sender.destroyed=true;sender.emit('destroyed');}
    await pause(40);
    assert.equal(sender.events.length,0,lifecycle+' suppresses all late events');
    assert.equal(sender.listenerCount('did-start-navigation'),0,lifecycle+' releases listeners');
    invoke(b,'tts:edgeDecoded',id,'A-1',true); // stale verdicts are harmless
  }
  invoke(a,'tts:edgeStart','muted-via-settings','muted while awaiting decode');
  await until(()=>a.events.some(e=>e.id==='muted-via-settings'&&e.type==='candidate'));
  invoke(a,'app:settings:set',{muted:true});
  assert.equal(a.events.at(-1).reason,'cancelled');
  assert.throws(()=>invoke(a,'tts:edgeStart','while-muted','speech'),/muted/i);
  invoke(a,'app:settings:set',{muted:false});
  invoke(a,'tts:edgeStart','switch-provider','pending decode');
  await until(()=>a.events.some(e=>e.id==='switch-provider'&&e.type==='candidate'));
  invoke(a,'app:settings:set',{speech:{provider:'windows'}});
  assert.equal(a.events.at(-1).reason,'cancelled');
  assert.throws(()=>invoke(a,'tts:edgeStart','wrong-provider','speech'),/not selected/i);
  h.close();

  // Chromium decode is inside the main deadline, even when a timer has not yet had an event-loop turn.
  scenario='normal'; h=host(); first=request(h,'decode-deadline',text,false);
  await until(()=>first.events.some(e=>e.type==='candidate'));
  const descriptor=Object.getOwnPropertyDescriptor(globalThis,'performance'), nativePerformance=performance;
  Object.defineProperty(globalThis,'performance',{configurable:true,value:{now:()=>nativePerformance.now()+16000}});
  try { h.decoded('decode-deadline','A-1',true); }
  finally { Object.defineProperty(globalThis,'performance',descriptor); }
  const deadlineLog=logs.find(([l])=>l.includes('Edge deadline')&&l.includes('"decode-deadline"'));
  assert(deadlineLog,'expected Edge deadline log');
  assert.equal((await first.ended).reason,'deadline');
  h.decoded('decode-deadline','A-1',true); await pause(25);
  assert(!first.events.some(e=>e.type==='accepted'),'a late decode verdict cannot produce success');
  assert.equal(first.events.filter(e=>e.type==='end').length,1); h.close();

  scenario='startup-deadline'; h=host();
  const began=performance.now(); first=request(h,'deadline');
  assert.equal((await first.ended).reason,'deadline');
  assert(performance.now()-began<15400,'main deadline includes waiting for ready');
  await pause(650);
  assert.equal(first.events.length,1,'late ready cannot revive a deadline request');
  assert(!readAudit().some(c=>c.op==='start'&&c.id==='deadline'),'expired text never sent'); h.close();
  console.log('OK: persistent Edge supervisor ordering, decode, startup/deadline, cancellation and safe failures');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(()=>setTimeout(()=>process.exit(process.exitCode||0),25));
"""

with tempfile.TemporaryDirectory(prefix="glide-edge-tts-") as tmp:
    script = os.path.join(tmp, "check.cjs")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(RUN)
    result = subprocess.run([desktop_ts.node, script, os.path.join(desktop_ts.main(), "main"), tmp],
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    print(result.stdout + result.stderr)
    assert result.returncode == 0, "Edge supervisor behavior checks failed"
