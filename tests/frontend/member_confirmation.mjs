import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// Execute the shipped script against a small DOM/HTTP fixture. No real account,
// browser, database, timer delay or external request is used by these checks.
const layouts=JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
  constructor(tag='div'){this.tagName=tag;this.children=[];this.listeners={};this.dataset={};this.value='';this.textContent='';this.hidden=false;this.disabled=false;this.checked=false;const values=new Set();this.classList={toggle:(k,on)=>on?values.add(k):values.delete(k),contains:k=>values.has(k),add:k=>values.add(k)};}
  append(...values){this.children.push(...values);}
  replaceChildren(...values){this.children=values;}
  setAttribute(){}
  addEventListener(name,fn){(this.listeners[name] ||= []).push(fn);}
  showModal(){this.open=true;}
  close(){this.open=false;}
}
const settle=async()=>{for(let i=0;i<3;i++)await new Promise(setImmediate);};
async function page(layout,options={}) {
  const elements=new Map([...layout.html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)].map(match=>[match[1],new Element()]));
  const calls=[],timers=new Map();let timerID=0;
  const control={submitted:0,confirmed:0,cancelled:0,phase:'awaiting_confirmation',...options};
  const session=()=>({actor:{user_id:'operator',display_name:'Operator',email:'operator@example.test',role:'admin',timezone:'UTC',directory_revision:2},account:{username:null,password_ready:false},nodes:['node-a'],workspace_namespace:'TDEMO',csrf_token:'fixture-csrf',slack_roster_available:false});
  const response=(data,status=200)=>({ok:status>=200 && status<300,status,json:async()=>structuredClone(data)});
  const fetch=async(path,request={})=>{
    calls.push({path,...request});
    if(path===layout.account+'/session')return control.signedOut?response({error:{message:'Please sign in to continue'}},401):response(session());
    if(path===layout.account+'/login')return response({error:{message:'Please sign in to continue'}},401);
    if(path===layout.account+'/invitation')return response({challenge_id:'fixture-challenge',expires_in:600,message:'Check Slack for your invitation code.'});
    if(path===layout.account+'/directory')return response({directory:{namespace:{namespace:'TDEMO'},revision:2,next_cursor:null,members:[]},access:[]});
    if(path===layout.commands){
      control.submitted++;const payload=JSON.parse(request.body);control.command=payload.command;control.parameters=structuredClone(payload.parameters);
      if(control.blockSubmit)await new Promise(resolve=>control.release=resolve);
      if(control.loseSubmit && control.submitted===1)throw new Error('Synthetic lost response');
      return response({outcome:{command_id:'fixture-command'}});
    }
    if(path.endsWith('/review')){
      const parameters=structuredClone(control.parameters);
      if(control.changeDetails)parameters.profile.email='changed@example.test';
      if(control.defaults){parameters.role_policy_fingerprint='a'.repeat(64);Object.assign(parameters.changes[0],{login_username:null,active:null,role:null,previous_bindings:[]});}
      if(control.changeRole)Object.assign(parameters.changes[0],{role:'admin',previous_bindings:[]});
      return response({command_id:'fixture-command',request:{command:control.wrongCommand?'other.command':control.command,scope:{type:'global',id:'*'},parameters},outcome:{command_id:'fixture-command',state:control.phase,proof:'f'.repeat(64)}});
    }
    if(path.endsWith('/confirm')){control.confirmed++;assert.equal(JSON.parse(request.body).proof,'f'.repeat(64));control.phase='queued';return response({outcome:{state:'queued'}});}
    if(path.endsWith('/'+layout.cancel)){control.cancelled++;control.phase='cancelled';return response({outcome:{state:'cancelled'}});}
    throw new Error('Unexpected fixture request '+path);
  };
  const document={documentElement:{dataset:{cancelPath:layout.cancel}},getElementById:id=>elements.get(id),createElement:tag=>new Element(tag),querySelectorAll:()=>[]};
  const context={document,window:{addEventListener(){}},fetch,structuredClone,crypto:{randomUUID:()=>crypto.randomUUID()},AbortController,Intl,Date,console,Option:class extends Element{constructor(label,value){super('option');this.textContent=label;this.value=value;}},setTimeout:(fn,delay)=>{const id=++timerID;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id)};
  vm.runInNewContext(layout.source.replace('\n})();','\n globalThis.testHooks={state,begin,operate};\n})();'),context);
  await settle();const hooks=context.testHooks,$=id=>elements.get(id);
  const begin=()=>hooks.begin('identity.edit_own_profile',{expected_revision:2,profile:{display_name:'Updated',timezone:'UTC',email:'draft@example.test'}},new Set());
  const beginMember=()=>{
    hooks.state.rows.set('UMEMBER',{id:'UMEMBER',user:'member-id',name:'Member',bindings:[],revoked_when_member:[]});
    hooks.begin('identity.manage_members',{expected_revision:2,reason:'Fixture update',changes:[{type:'edit_member',user_id:'member-id',profile:{display_name:'Updated member',timezone:'UTC',email:null}}]},new Set(['UMEMBER']));
  };
  const tick=async()=>{const next=[...timers].sort((a,b)=>a[1].delay-b[1].delay)[0];assert(next,'result follows automatically');timers.delete(next[0]);next[1].fn();await settle();};
  return {hooks,$,begin,beginMember,tick,calls,control};
}
let checks=0;
for(const layout of layouts){
  assert(!layout.html.includes('id="account-remember"'));
  assert(!layout.source.includes("$('account-remember')"));
  {
    const p=await page(layout,{signedOut:true});
    p.$('login-username').value='test-only';p.$('login-password').value='fixture-password-123';
    p.$('login-form').listeners.submit[0]({preventDefault(){}});await settle();
    assert.equal(p.$('notice').textContent,'Please sign in to continue');
    assert(p.$('notice').classList.contains('error'));
    p.$('auth-reset').listeners.click[0]();
    assert(p.$('notice').hidden,'switching to password reset clears the previous login error');
    p.$('invitation-form').listeners.submit[0]({preventDefault(){}});await settle();
    assert(!p.$('notice').hidden,'current validation errors remain visible');
    p.$('invitation-username').value='test-only';
    await p.$('send-invitation').listeners.click[0]();
    assert(p.$('notice').hidden,'successful code delivery must not retain an earlier error');
    assert.equal(p.$('notice').classList.contains('error'),false);
    assert.equal(p.$('invitation-fields').hidden,false);
    assert.equal(p.$('invitation-message').textContent,'Check Slack for your invitation code.');
    p.$('invitation-code').value='incorrect';
    await p.$('invitation-form').listeners.submit[0]({preventDefault(){}});
    assert.equal(p.$('notice').textContent,'Enter the six-digit invitation code from Slack.');
    assert(!p.$('notice').hidden,'actual errors in the reset flow remain visible');checks++;
  }
  {
    const p=await page(layout);p.begin();
    assert(p.$('review-dialog').open);assert.equal(p.$('confirm-review').disabled,false);
    assert.equal(p.$('review-dialog').classList.contains('preparing'),false);
    assert(p.$('review-content').children.length);assert.equal(p.control.submitted,0);
    p.hooks.operate('withdraw');await settle();
    assert.equal(p.control.submitted,0);assert.equal(p.control.cancelled,0);
    assert(p.hooks.state.intent.terminal);assert(p.$('review-status').textContent.includes('Nothing was saved'));checks++;
  }
  {
    const p=await page(layout,{blockSubmit:true});p.begin();p.hooks.operate('confirm');
    assert.equal(p.$('review-status').textContent,'Saving…');assert.equal(p.control.submitted,1);assert.equal(p.control.confirmed,0);
    p.control.release();await settle();assert.equal(p.control.confirmed,1);
    assert.notEqual(p.$('review-status').textContent,'Changes saved.');
    p.control.phase='succeeded';await p.tick();assert.equal(p.$('review-status').textContent,'Changes saved.');checks++;
  }
  {
    const p=await page(layout,{changeDetails:true});p.begin();p.hooks.operate('confirm');await settle();
    assert.equal(p.control.confirmed,0);assert(p.hooks.state.intent.awaitingUser);
    assert(p.$('review-status').textContent.includes('Some details changed'));
    assert(p.$('review-content').children.some(node=>node.textContent.includes('changed@example.test')));
    p.hooks.operate('confirm');await settle();assert.equal(p.control.confirmed,1);checks++;
  }
  {
    const p=await page(layout,{changeDetails:true});p.begin();p.hooks.operate('confirm');await settle();
    p.hooks.operate('withdraw');await settle();assert.equal(p.control.cancelled,1);assert.equal(p.control.confirmed,0);
    await p.tick();assert(p.hooks.state.intent.terminal);assert(p.$('review-status').textContent.includes('Nothing was saved'));checks++;
  }
  {
    const p=await page(layout,{loseSubmit:true});p.begin();p.hooks.operate('confirm');await settle();
    assert.equal(p.control.confirmed,0);await p.tick();
    const submissions=p.calls.filter(call=>call.path===layout.commands);
    assert.equal(submissions.length,2);assert.equal(submissions[0].headers['Idempotency-Key'],submissions[1].headers['Idempotency-Key']);
    assert.equal(p.control.confirmed,1);checks++;
  }
  {
    const p=await page(layout,{wrongCommand:true});p.begin();p.hooks.operate('confirm');await settle();
    assert.equal(p.control.confirmed,0);assert(p.$('confirm-review').disabled);assert(!p.hooks.state.intent.terminal);checks++;
  }
  {
    const p=await page(layout,{defaults:true});p.beginMember();p.hooks.operate('confirm');await settle();
    assert.equal(p.control.confirmed,1,'optional no-op defaults and the policy stamp do not require a second click');checks++;
  }
  {
    const p=await page(layout,{changeRole:true});p.beginMember();p.hooks.operate('confirm');await settle();
    assert.equal(p.control.confirmed,0,'an added privilege change requires new consent');
    assert(p.hooks.state.intent.awaitingUser);assert(!p.$('confirm-review').disabled);checks++;
  }
  {
    const p=await page(layout,{phase:'failed'});p.begin();p.hooks.operate('confirm');await settle();
    assert.equal(p.control.confirmed,0);assert(p.hooks.state.intent.terminal);
    assert(p.$('review-status').textContent.includes('Changes were not saved.'));checks++;
  }
}
console.log(`Member confirmation: ${checks} workflow checks passed across both URL layouts`);
