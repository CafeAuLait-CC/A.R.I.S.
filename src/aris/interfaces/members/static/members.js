"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const AUDIT_REASON = 'Member changes saved from the member portal.';
  let reviewTimer = null, invitationTimer = null;
  const state = {session:null, csrf:"", epoch:0, busy:false, authBusy:false, accountBusy:false, authMode:'login',invitation:null,invitationSendAfter:0,source:"registered", next:null, revision:null, rows:new Map(), page:[], edits:new Map(), selected:new Set(), intent:null};
  function el(tag, value, className) {const node=document.createElement(tag);if(value!==undefined)node.textContent=value;if(className)node.className=className;return node;}
  function notice(value, error=false) {$('notice').textContent=value;$('notice').hidden=!value;$('notice').classList.toggle('error',error);}
  function reviewNotice(value,error=false,working=false) {if($('review-status').textContent!==value)$('review-status').textContent=value;$('review-status').classList.toggle('error',error);$('review-status').classList.toggle('working',working);$('review-dialog').setAttribute('aria-busy',String(working));}
  function lock(value) {state.busy=value;document.querySelectorAll('#workspace button,#workspace input,#workspace select').forEach(node=>node.disabled=value);controls();}
  function controls() {
    const pending=state.intent && (!state.intent.terminal || state.intent.needsRefresh);
    document.querySelectorAll('#members-panel input,#members-panel select,#members-panel button,#profile-form input,#profile-form button,#account-password-form input,#account-password-form button').forEach(node=>node.disabled=state.busy || pending || state.accountBusy);
    $('review-selected').disabled=state.busy || pending || !state.selected.size;
    $('next-page').disabled=state.busy || pending || !state.next;
    $('resume-review').hidden=!state.intent;
    $('resume-review').disabled=state.busy;
    $('sign-out').disabled=state.busy || state.accountBusy;
    $('selection-count').textContent=state.selected.size+' selected';
    $('load-slack').hidden=!state.session?.slack_roster_available;
    document.querySelectorAll('.no-edit').forEach(node=>node.disabled=true);
  }
  async function request(path, body, method='POST', key=null) {
    const epoch=state.epoch, controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),12000);
    try {
      const response=await fetch(path,{method,credentials:'same-origin',cache:'no-store',redirect:'error',referrerPolicy:'no-referrer',headers:{'X-CSRF-Token':state.csrf,'X-Request-ID':crypto.randomUUID(),...(body!==undefined?{'Content-Type':'application/json'}:{}),...(key?{'Idempotency-Key':key}:{})},body:body===undefined?undefined:JSON.stringify(body),signal:controller.signal});
      if(epoch!==state.epoch)throw new Error('The session changed.');
      const data=await response.json();if(epoch!==state.epoch)throw new Error('The session changed.');
      if(!response.ok){const error=new Error(data.error?.message || 'The request could not be verified.');error.status=response.status;throw error;}
      return data;
    } finally {clearTimeout(timeout);}
  }
  function clear() {
    clearTimeout(reviewTimer);reviewTimer=null;
    clearTimeout(invitationTimer);invitationTimer=null;
    state.epoch++;state.session=null;state.csrf='';state.intent=null;state.rows.clear();state.edits.clear();state.selected.clear();state.page=[];state.next=null;state.busy=false;state.accountBusy=false;state.invitation=null;
    $('credential').value='';$('members-body').replaceChildren();$('review-content').replaceChildren();$('general-data').replaceChildren();$('profile-name').value='';$('profile-email').value='';
    ['login-password','invitation-code','invitation-password','invitation-confirm','current-password','account-password','account-confirm','account-username'].forEach(id=>$(id).value='');
    $('review-dialog').close();$('add-dialog').close();$('workspace').hidden=true;$('account').hidden=true;$('login-panel').hidden=false;
    $('legacy-login').open=false;
    ['new-id','new-name','new-email','new-username','filter'].forEach(id=>$(id).value='');$('actor-name').textContent='';$('actor-role').textContent='';authMode('login');lock(false);
  }
  function tab(name) {for(const value of ['members','profile','general'])$(value+'-panel').hidden=value!==name;}
  async function connect() {
    state.session=await request('/internal/members/session',undefined,'GET');state.csrf=state.session.csrf_token;state.revision=state.session.actor.directory_revision;
    $('actor-name').textContent=state.session.actor.display_name;$('actor-role').textContent=state.session.actor.role;
    $('profile-name').value=state.session.actor.display_name;$('profile-email').value=state.session.actor.email || '';
    accountFields();
    $('members-tab').hidden=state.session.actor.role!=='admin';$('account').hidden=false;$('workspace').hidden=false;$('login-panel').hidden=true;
    $('general-node').replaceChildren(...state.session.nodes.map(node=>new Option(node,node)));
    const date=new Intl.DateTimeFormat('en-CA',{timeZone:state.session.actor.timezone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date()),parts=Object.fromEntries(date.map(part=>[part.type,part.value]));
    const today=parts.year+'-'+parts.month+'-'+parts.day;$('general-start').value=today;$('general-end').value=today;
    tab(state.session.actor.role==='admin'?'members':'profile');controls();
  }
  function username(value) {
    const normalized=value.trim().toLowerCase();
    if(!/^[a-z][a-z0-9_.-]{2,63}$/.test(normalized))throw new Error('Use a username of 3–64 characters, starting with a letter and containing letters, numbers, dots, underscores or hyphens.');
    return normalized;
  }
  function password(value,confirmation=value) {
    if([...value].length<15 || [...value].length>128)throw new Error('Use a password of 15–128 characters.');
    if(value!==confirmation)throw new Error('The passwords do not match.');
    return value;
  }
  function authControls() {
    document.querySelectorAll('#login-panel input,#login-panel button').forEach(node=>node.disabled=state.authBusy);
    $('send-invitation').disabled=state.authBusy || Date.now()<state.invitationSendAfter;
    if(Date.now()<state.invitationSendAfter && invitationTimer===null)invitationTimer=setTimeout(()=>{invitationTimer=null;authControls();},state.invitationSendAfter-Date.now());
  }
  function authMode(mode) {
    if(state.authBusy)return;
    state.authMode=mode;state.invitation=null;
    $('login-form').hidden=mode!=='login';$('invitation-form').hidden=mode==='login';$('legacy-login').hidden=mode!=='login';$('invitation-fields').hidden=true;
    $('login-title').textContent=mode==='login'?'Sign in':mode==='activate'?'Activate account':'Reset password';
    $('login-intro').textContent=mode==='login'?'Enter your username and password.':'Request an invitation code in Slack, then choose your password.';
    $('complete-invitation').textContent=mode==='activate'?'Activate account':'Reset password';
    $('invitation-message').textContent='';
    ['login-password','invitation-code','invitation-password','invitation-confirm','credential'].forEach(id=>$(id).value='');
    for(const [id,value] of [['auth-sign-in','login'],['auth-activate','activate'],['auth-reset','reset']])$(id).setAttribute('aria-current',value===mode?'page':'false');
    authControls();
  }
  $('auth-sign-in').addEventListener('click',()=>authMode('login'));$('auth-activate').addEventListener('click',()=>authMode('activate'));$('auth-reset').addEventListener('click',()=>authMode('reset'));
  async function signIn(payload) {
    if(state.authBusy)return;state.epoch++;state.authBusy=true;authControls();notice('Signing in…');
    try{const data=await request('/internal/members/login',payload);if(data.signed_in!==true)throw new Error('Sign-in could not be verified.');await connect();notice('Signed in.');}
    catch(error){notice(error.message,true);}finally{state.authBusy=false;authControls();controls();}
  }
  $('login-form').addEventListener('submit',event=>{
    event.preventDefault();if(state.authBusy)return;
    try{const payload={username:username($('login-username').value),password:$('login-password').value,remember:$('remember').checked};$('login-password').value='';signIn(payload);}catch(error){notice(error.message,true);}
  });
  $('legacy-form').addEventListener('submit',event=>{event.preventDefault();if(state.authBusy)return;const credential=$('credential').value.trim();$('credential').value='';signIn({credential,remember:$('legacy-remember').checked});});
  $('invitation-username').addEventListener('input',()=>{state.invitation=null;$('invitation-fields').hidden=true;['invitation-code','invitation-password','invitation-confirm'].forEach(id=>$(id).value='');$('invitation-message').textContent='Request a code for this username.';});
  $('send-invitation').addEventListener('click',async()=>{
    if(state.authBusy || Date.now()<state.invitationSendAfter || state.authMode==='login')return;
    let account;try{account=username($('invitation-username').value);}catch(error){notice(error.message,true);return;}
    state.authBusy=true;state.invitation=null;state.invitationSendAfter=Date.now()+60000;authControls();$('invitation-fields').hidden=true;$('invitation-message').textContent='Sending invitation code…';
    clearTimeout(invitationTimer);invitationTimer=setTimeout(()=>{invitationTimer=null;authControls();},60000);
    try {
      const data=await request('/internal/members/invitation',{username:account,purpose:state.authMode});
      if(typeof data.challenge_id!=='string' || !data.challenge_id || !Number.isFinite(data.expires_in) || data.expires_in<=0 || data.expires_in>600)throw new Error('Invitation delivery could not be verified.');
      state.invitation={username:account,purpose:state.authMode,id:data.challenge_id,expiresAt:Date.now()+data.expires_in*1000};
      $('invitation-fields').hidden=false;$('invitation-message').textContent=data.message || 'Check Slack for your invitation code.';
    } catch(error){$('invitation-message').textContent=error.message+(error.status?'':' Delivery is uncertain. Wait before requesting another code.');}
    finally{state.authBusy=false;authControls();}
  });
  $('invitation-form').addEventListener('submit',async event=>{
    event.preventDefault();if(state.authBusy)return;
    let payload,invitation=state.invitation;
    try {
      if(!invitation || invitation.username!==username($('invitation-username').value) || invitation.purpose!==state.authMode)throw new Error('Request an invitation code for this username first.');
      if(Date.now()>=invitation.expiresAt)throw new Error('This invitation code expired. Request a new code.');
      const code=$('invitation-code').value.trim();if(!/^[0-9]{6}$/.test(code))throw new Error('Enter the six-digit invitation code from Slack.');
      payload={username:invitation.username,challenge_id:invitation.id,code,password:password($('invitation-password').value,$('invitation-confirm').value),remember:$('invitation-remember').checked};
    }catch(error){notice(error.message,true);return;}
    state.authBusy=true;authControls();['invitation-code','invitation-password','invitation-confirm'].forEach(id=>$(id).value='');notice(state.authMode==='activate'?'Activating account…':'Resetting password…');
    let acknowledged=false;
    try {const data=await request('/internal/members/'+(invitation.purpose==='activate'?'register':'password-reset'),payload);if(data.signed_in!==true)throw new Error('Account activation could not be verified.');acknowledged=true;state.invitation=null;await connect();notice('Signed in.');}
    catch(error){notice(acknowledged?'Password saved. Sign in with your new password to continue.':error.message+(error.status?'':' The result is uncertain. Try signing in with your chosen password before requesting another code.'),true);}
    finally{state.authBusy=false;authControls();controls();}
  });
  function accountFields() {
    const account=state.session?.account || {username:null,password_ready:false};
    $('account-username').value=account.username || '';$('account-username').readOnly=account.password_ready;
    $('current-password-field').hidden=!account.password_ready;$('current-password').required=account.password_ready;
    $('save-password').textContent=account.password_ready?'Update password':'Set password';
    $('account-note').textContent=account.password_ready?'Your login username is managed by an administrator.':'Set your username and password to finish migrating your account.';
    ['current-password','account-password','account-confirm'].forEach(id=>$(id).value='');$('account-message').textContent='';
  }
  $('account-password-form').addEventListener('submit',async event=>{
    event.preventDefault();if(state.accountBusy || state.busy || (state.intent && (!state.intent.terminal || state.intent.needsRefresh)))return;
    let payload;try{payload={username:username($('account-username').value),password:password($('account-password').value,$('account-confirm').value)};if(state.session.account?.password_ready)payload.current_password=$('current-password').value;}catch(error){$('account-message').textContent=error.message;return;}
    state.accountBusy=true;controls();$('account-message').textContent='Saving…';['current-password','account-password','account-confirm'].forEach(id=>$(id).value='');
    let acknowledged=false;
    try{const data=await request('/internal/members/password',payload);if(data.signed_in!==true)throw new Error('The password change could not be verified.');acknowledged=true;await connect();tab('profile');$('account-message').textContent='Password saved.';}
    catch(error){$('account-message').textContent=acknowledged?'Password saved. Sign in again to continue.':error.message+(error.status?'':' The result is uncertain. Try signing in with your new password before another change.');}
    finally{state.accountBusy=false;controls();}
  });
  $('sign-out').addEventListener('click',async()=>{if(state.busy)return;try{await request('/internal/members/logout',{});clear();notice('Signed out.');}catch(error){notice(error.message,true);}});
  $('members-tab').addEventListener('click',()=>tab('members'));
  $('profile-tab').addEventListener('click',()=>tab('profile'));
  $('general-tab').addEventListener('click',()=>tab('general'));
  function cacheRow(row, access) {
    const registered=row.registered, slack=row.slack;
    return {id:slack?.external_id || registered.external_id,user:registered?.user_id || null,name:slack?.display_name || registered.profile.display_name,profile:registered?.profile || {display_name:slack.display_name,timezone:slack.timezone || state.session.actor.timezone,email:null},status:registered?.status || 'unregistered',active:slack?.deleted!==true,eligible:registered || (slack?.deleted===false && slack?.is_bot===false && slack?.is_app_user!==true),mappings:registered?.mappings || [],role:access?.role || 'member',bindings:access?.bindings || [],revoked_when_member:access?.revoked_when_member || [],login_username:access?.login_username || null,password_ready:access?.password_ready===true,revision:state.revision};
  }
  async function load(source, next=false) {
    if(state.busy)return;lock(true);notice('Loading members…');
    try {
      const data=source==='slack'?await request('/internal/members/slack-members',{cursor:next?state.next:null}):await request('/internal/members/directory',{after:next?state.next:null,limit:20});
      const page=source==='slack'?data.roster:data.directory;
      const previous=state.revision;state.revision=page.revision;state.source=source;state.next=page.next_cursor;
      const access=new Map(data.access.map(item=>[item.user_id,item]));state.page=[];
      for(const item of source==='slack'?page.members:page.members.map(member=>({registered:member,slack:null}))) {
        const row=cacheRow(item,access.get(item.registered?.user_id));state.rows.set(row.id,row);state.page.push(row.id);
      }
      $('directory-snapshot').textContent='Workspace '+(source==='slack'?page.namespace:page.namespace.namespace)+' · revision '+state.revision;
      render();notice(previous!==state.revision && state.edits.size?'Directory changed. Your edited fields are preserved; load any selected older pages before review.':'Select members, edit their details, then review the selection once.');
      return true;
    } catch(error){notice(error.message,true);return false;}finally{lock(false);}
  }
  function edit(row, key, value) {if(!state.edits.has(row.id))state.edits.set(row.id,{});state.edits.get(row.id)[key]=value;state.selected.add(row.id);document.querySelectorAll('#members-body tr').forEach(tr=>{if(tr.dataset.id===row.id)tr.querySelector('input[type="checkbox"]').checked=true;});controls();}
  function draft(row,key,fallback) {return state.edits.get(row.id)?.[key] ?? fallback;}
  function field(row,key,value,label) {const node=el('input');node.value=draft(row,key,value);node.setAttribute('aria-label',row.id+' '+label);node.autocomplete='off';node.addEventListener('input',()=>edit(row,key,node.value));return node;}
  function render() {
    const heading=el('tr');['','Member','Profile','Account','Role / access',...state.session.nodes,'Status'].forEach(value=>heading.append(el('th',value)));$('members-head').replaceChildren(heading);$('members-body').replaceChildren();
    for(const id of state.page) {
      const row=state.rows.get(id),tr=el('tr');tr.dataset.id=id;tr.dataset.search=(row.name+' '+id).toLowerCase();tr.dataset.inactive=String(!row.active || row.status==='suspended');
      const check=el('input');check.type='checkbox';check.checked=state.selected.has(id);check.setAttribute('aria-label','Select '+id);check.addEventListener('change',()=>{if(check.checked)state.selected.add(id);else state.selected.delete(id);controls();});
      const selectCell=el('td');selectCell.append(check);tr.append(selectCell);
      const who=el('td');who.append(el('div',row.name,'member-name'),el('div',id,'member-id'),el('p',row.user?'Registered':'Not registered','muted small'));if(!row.eligible){who.append(el('p','Not an active human Slack account','muted small'));check.disabled=true;check.classList.add('no-edit');}tr.append(who);
      const profile=el('td'),email=field(row,'email',row.profile.email || '','email');email.type='email';email.maxLength=254;profile.append(field(row,'display_name',row.profile.display_name,'display name'),email);tr.append(profile);
      const account=el('td'),login=field(row,'login_username',row.login_username || '','login username');login.maxLength=64;login.spellcheck=false;account.append(login,el('p',row.password_ready?'Password set':'Needs activation','muted small'));tr.append(account);
      const roleCell=el('td'),role=el('select');role.setAttribute('aria-label',id+' role');role.append(new Option('Member','member'),new Option('Admin','admin'));role.value=draft(row,'role',row.role);role.addEventListener('change',()=>edit(row,'role',role.value));if(!row.user){role.disabled=true;role.classList.add('no-edit');}roleCell.append(role);
      tr.append(roleCell);
      for(const node of state.session.nodes){const cell=el('td'),links=row.mappings.filter(link=>link.node_id===node);if(links.length>1){cell.append(el('p',links.map(link=>link.os_username).join(', ')),el('p','Multiple aliases: retained','muted small'));}else cell.append(field(row,'node:'+node,links[0]?.os_username || '',node+' Linux username'));tr.append(cell);}
      const status=el('td');status.append(el('span',draft(row,'active',row.status!=='suspended')?'Active':'Removed','state'));
      if(row.user){const action=el('button',row.status==='suspended'?'Restore':'Remove','quiet small-button');action.type='button';action.addEventListener('click',()=>{edit(row,'active',row.status==='suspended');render();});status.append(action);}tr.append(status);$('members-body').append(tr);
    }
    filter();controls();
  }
  function filter() {const query=$('filter').value.trim().toLowerCase();document.querySelectorAll('#members-body tr').forEach(row=>row.hidden=($('active-only').checked && row.dataset.inactive==='true') || (!!query && !row.dataset.search.includes(query)));}
  $('filter').addEventListener('input',filter);$('active-only').addEventListener('change',filter);
  $('select-visible').addEventListener('change',()=>{document.querySelectorAll('#members-body tr').forEach(tr=>{const row=state.rows.get(tr.dataset.id);if(!tr.hidden && row.eligible){if($('select-visible').checked)state.selected.add(row.id);else state.selected.delete(row.id);}});render();});
  $('clear-selection').addEventListener('click',()=>{state.selected.clear();$('select-visible').checked=false;render();});
  for(const [id,key,value] of [['bulk-member','role','member'],['bulk-admin','role','admin'],['bulk-remove','active',false]])$(id).addEventListener('click',()=>{if((id==='bulk-admin' || id==='bulk-remove') && [...state.selected].some(selected=>!state.rows.get(selected).user)){notice('Register new members before changing their role or removing their account.',true);return;}for(const selected of state.selected){const row=state.rows.get(selected);if(row.user)edit(row,key,value);}render();});
  $('load-members').addEventListener('click',()=>load('registered'));
  $('load-slack').addEventListener('click',()=>load('slack'));
  $('next-page').addEventListener('click',()=>load(state.source,true));
  $('add-member').addEventListener('click',()=>{$('new-id').value='';$('new-name').value='';$('new-email').value='';$('new-username').value='';$('add-dialog').showModal();});
  $('close-add').addEventListener('click',()=>$('add-dialog').close());
  $('add-form').addEventListener('submit',event=>{event.preventDefault();const id=$('new-id').value.trim();if(state.rows.has(id)){notice('This member is already loaded. Edit their existing row.',true);return;}let login=null;try{if($('new-username').value.trim())login=username($('new-username').value);}catch(error){notice(error.message,true);return;}const row={id,user:null,name:$('new-name').value.trim(),profile:{display_name:$('new-name').value.trim(),timezone:state.session.actor.timezone,email:$('new-email').value.trim() || null},status:'unregistered',active:true,eligible:true,mappings:[],role:'member',bindings:[],login_username:login,password_ready:false,revision:state.revision,manual:true};state.rows.set(id,row);state.page.push(id);state.selected.add(id);$('add-dialog').close();render();});
  function changes() {
    const result=[];
    for(const id of state.selected) {
      const row=state.rows.get(id);if(!row?.eligible)throw new Error('Only verified active human members can be registered.');
      if(!row.manual && row.revision!==state.revision)throw new Error('Load the current page for '+id+' before review. Your edits are preserved.');
      const profile={display_name:draft(row,'display_name',row.profile.display_name).trim(),timezone:row.profile.timezone,email:draft(row,'email',row.profile.email || '').trim() || null};
      if(!profile.display_name)throw new Error('Enter a display name for '+id+'.');
      const loginValue=draft(row,'login_username',row.login_username || '').trim();
      if(row.user && row.login_username && !loginValue)throw new Error('Keep the existing login username or enter a new one for '+id+'.');
      const login=loginValue?username(loginValue):null;
      const mappings=[];
      for(const node of state.session.nodes) {
        const links=row.mappings.filter(link=>link.node_id===node);if(links.length>1)continue;
        const value=draft(row,'node:'+node,links[0]?.os_username || '').trim();if(value && /\s/.test(value))throw new Error('Linux usernames cannot contain whitespace.');
        if(!row.user){if(value)mappings.push({node_id:node,os_username:value});}
        else {
          if(links[0] && links[0].os_username!==value)result.push({type:'remove_node_user',node_id:node,os_username:links[0].os_username,expected_user_id:row.user});
          if(value && (!links[0] || links[0].os_username!==value || links[0].confidence!=='verified' || links[0].source!=='manual'))result.push({type:'assign_node_user',node_id:node,os_username:value,user_id:row.user,expected_user_id:links[0]?.os_username===value?row.user:null});
        }
      }
      if(!row.user)result.push({type:'register_identity',subject:{provider:'slack',namespace:state.session.workspace_namespace,external_id:id},profile,mappings,...(login?{login_username:login}:{})});
      else {
        const role=draft(row,'role',row.role),active=draft(row,'active',row.status==='active'),change={type:'edit_member',user_id:row.user};
        if(profile.display_name!==row.profile.display_name || profile.email!==(row.profile.email || null))change.profile=profile;
        if(active!==(row.status==='active'))change.active=active;
        if(login!==(row.login_username || null))change.login_username=login;
        if(role!==row.role || state.edits.get(id)?.role!==undefined || !row.bindings.some(binding=>binding.role_name===role && binding.scope.type==='global' && binding.scope.id==='*')){change.role=role;change.previous_bindings=row.bindings;}
        if(Object.keys(change).length>2)result.push(change);
      }
    }
    if(!result.length)throw new Error('The selected members have no changes.');
    if(result.length>30)throw new Error('This selection is too large. Split it into smaller batches.');
    return result;
  }
  function begin(command,parameters,selected) {
    if(state.busy || state.accountBusy || (state.intent && (!state.intent.terminal || state.intent.needsRefresh)))return;
    clearTimeout(reviewTimer);reviewTimer=null;
    state.intent={command,parameters:structuredClone(parameters),selected:[...selected],users:new Set([...selected].map(id=>state.rows.get(id)?.user).filter(Boolean)),key:crypto.randomUUID(),confirmKey:crypto.randomUUID(),withdrawKey:crypto.randomUUID(),commandId:null,proof:null,busy:false,terminal:false,needsRefresh:false,successApplied:false,pollCount:0,confirmedAt:null,pendingOperation:null,uncertain:false,awaitingUser:true,requestedSave:false};
    state.intent.reviewParameters=structuredClone(parameters);
    const content=$('review-content');content.replaceChildren();describeReview(content,state.intent,parameters);
    reviewView('confirmation','Confirm these changes to save.');$('confirm-review').disabled=false;$('withdraw-review').disabled=false;
    $('review-dialog').showModal();controls();
  }
  $('review-selected').addEventListener('click',()=>{try{begin('identity.manage_members',{expected_revision:state.revision,reason:AUDIT_REASON,changes:changes()},state.selected);}catch(error){notice(error.message,true);}});
  $('profile-form').addEventListener('submit',event=>{event.preventDefault();begin('identity.edit_own_profile',{expected_revision:state.session.actor.directory_revision,profile:{display_name:$('profile-name').value.trim(),timezone:state.session.actor.timezone,email:$('profile-email').value.trim() || null}},new Set());});
  function reviewView(phase,message,error=false,working=false) {
    $('review-dialog').classList.toggle('preparing',phase==='preparing');
    $('review-dialog').classList.toggle('saving',['saving','cancelling'].includes(phase));
    $('review-dialog').setAttribute('aria-busy',String(working));
    $('confirm-review').hidden=phase!=='confirmation';$('confirm-review').disabled=true;
    $('withdraw-review').hidden=phase!=='confirmation';$('withdraw-review').disabled=true;
    reviewNotice(message,error,working);
  }
  function scheduleReview(intent,immediate=false) {
    clearTimeout(reviewTimer);reviewTimer=null;
    if(state.intent!==intent || intent.awaitingUser || (intent.terminal && !intent.needsRefresh))return;
    const delay=immediate?250:Math.min(5000,500*2**Math.min(intent.pollCount++,4));
    reviewTimer=setTimeout(()=>{reviewTimer=null;advanceReview();},delay);
  }
  function savingText(intent) {return intent.confirmedAt && Date.now()-intent.confirmedAt>=15000?'Still saving…':'Saving…';}
  function safeTarget(change,intent) {
    if(change.type==='register_identity')return change.subject.provider==='slack' && change.subject.namespace===state.session.workspace_namespace && intent.selected.includes(change.subject.external_id) && change.mappings.every(link=>state.session.nodes.includes(link.node_id));
    if(change.type==='remove_node_user')return intent.users.has(change.expected_user_id) && state.session.nodes.includes(change.node_id);
    if(change.type==='assign_node_user')return intent.users.has(change.user_id) && state.session.nodes.includes(change.node_id);
    return change.type==='edit_member' && intent.users.has(change.user_id);
  }
  function reviewFingerprint(parameters) {
    // The server fills optional no-op defaults and stamps its role-policy version.
    // Every actual value, target, array order and expected revision must still match.
    function ordered(value) {
      if(Array.isArray(value))return value.map(ordered);
      if(value && typeof value==='object')return Object.fromEntries(Object.keys(value).sort().filter(key=>value[key]!==null && !(Array.isArray(value[key]) && !value[key].length)).map(key=>[key,ordered(value[key])]));
      return value;
    }
    const copy=structuredClone(parameters);delete copy.role_policy_fingerprint;
    return JSON.stringify(ordered(copy));
  }
  function describeReview(content,intent,parameters) {
    if(intent.command==='identity.edit_own_profile') {
      content.append(el('h3','Your profile'),el('p','Display name: '+parameters.profile.display_name),el('p','Email: '+(parameters.profile.email || 'Not set')));
    } else {
      const groups=new Map();
      for(const change of parameters.changes) {
        const user=change.user_id || change.expected_user_id,row=change.type==='register_identity'?state.rows.get(change.subject.external_id):[...state.rows.values()].find(row=>row.user===user);
        const key=change.type==='register_identity'?change.subject.external_id:user;
        if(!groups.has(key))groups.set(key,{row,title:(change.type==='register_identity'?change.profile.display_name:row?.name || user)+(row?.id?' · '+row.id:''),lines:[]});
        const lines=groups.get(key).lines;
        if(change.type==='register_identity') {
          lines.push('Register as Member.','Display name: '+change.profile.display_name,'Email: '+(change.profile.email || 'Not set'));
          if(change.login_username)lines.push('Login username: '+change.login_username);
          if(!change.mappings.length)lines.push('No Linux username links.');
          change.mappings.forEach(link=>lines.push('Link '+link.node_id+' / '+link.os_username));
        } else if(change.type==='edit_member') {
          if(change.profile)lines.push('Display name: '+change.profile.display_name,'Email: '+(change.profile.email || 'Not set'));
          if(change.active===false)lines.push('Remove this member and disable login. History is retained.');
          if(change.active===true)lines.push('Restore this member. They can request an invitation code to activate or reset their password.');
          if(change.login_username)lines.push('Login username: '+change.login_username);
          if(change.role) {
            lines.push('Set role to '+(change.role==='admin'?'Admin':'Member')+'.');
            const remove=change.role==='member'?(row?.revoked_when_member || []):change.previous_bindings.filter(binding=>['admin','member'].includes(binding.role_name) && !(binding.role_name===change.role && binding.scope.type==='global' && binding.scope.id==='*'));
            remove.forEach(binding=>lines.push('Remove access binding: '+binding.role_name+' ('+binding.scope.type+' / '+binding.scope.id+')'));
            const removed=new Set(remove.map(binding=>JSON.stringify(binding)));
            const retained=change.previous_bindings.filter(binding=>!removed.has(JSON.stringify(binding)));
            if(retained.length)lines.push('Keep existing access: '+retained.map(binding=>binding.role_name+' ('+binding.scope.id+')').join(', '));
          }
        } else if(change.type==='assign_node_user')lines.push('Link '+change.node_id+' / '+change.os_username);
        else if(change.type==='remove_node_user')lines.push('Unlink '+change.node_id+' / '+change.os_username);
      }
      for(const group of groups.values()){content.append(el('h3',group.title));const list=el('ul');group.lines.forEach(line=>list.append(el('li',line)));content.append(list);}
    }

  }
  async function applySavedChanges(intent) {
    intent.needsRefresh=true;
    if(!intent.successApplied) {
      for(const id of intent.selected){state.edits.delete(id);state.selected.delete(id);}
      intent.successApplied=true;
    }
    state.session=await request('/internal/members/session',undefined,'GET');state.csrf=state.session.csrf_token;state.revision=state.session.actor.directory_revision;
    $('actor-name').textContent=state.session.actor.display_name;$('actor-role').textContent=state.session.actor.role;$('profile-name').value=state.session.actor.display_name;$('profile-email').value=state.session.actor.email || '';
    accountFields();
    if(state.session.actor.role==='admin') {
      if(!await load('registered'))throw new Error('Changes saved, but the member list could not refresh. Retrying automatically.');
    } else {$('members-tab').hidden=true;tab('profile');}
    intent.needsRefresh=false;notice('Changes saved.');
  }
  function failedMessage(code) {
    const known={CONFLICT:'The member directory changed. Reload the selected members before trying again.',PERMISSION_DENIED:'Your permission changed. Sign in with an authorized account.',VALIDATION_ERROR:'Some member details were rejected. Check the entries before trying again.',DEPENDENCY_UNAVAILABLE:'A required service was unavailable.'};
    return 'Changes were not saved. '+(known[code] || 'Please check your entries and try again.')+' Your edits are kept.';
  }
  async function advanceReview() {
    const intent=state.intent;if(!intent || intent.busy || intent.awaitingUser)return;
    clearTimeout(reviewTimer);reviewTimer=null;intent.busy=true;intent.proof=null;
    $('confirm-review').disabled=true;$('withdraw-review').disabled=true;
    try {
      if(!intent.commandId) {
        reviewView('saving',savingText(intent),false,true);
        const data=await request('/internal/commands',{command:intent.command,parameters:intent.parameters},'POST',intent.key);
        intent.commandId=data.outcome?.command_id;
        if(!intent.commandId)throw new Error('The request has not been verified.');
      }
      const data=await request('/internal/commands/'+encodeURIComponent(intent.commandId)+'/review',undefined,'GET');
      if(data.command_id!==intent.commandId || data.outcome.command_id!==intent.commandId || data.request.command!==intent.command || data.request.scope.type!=='global' || data.request.scope.id!=='*')throw new Error('The canonical request target differs. Confirmation is blocked.');
      const parameters=data.request.parameters;
      intent.reviewParameters=structuredClone(parameters);
      if(intent.command==='identity.manage_members' && !parameters.changes.every(change=>safeTarget(change,intent)))throw new Error('The canonical member selection differs. Confirmation is blocked.');
      const signature=JSON.stringify(parameters);
      if(intent.reviewSignature!==signature){const content=$('review-content');content.replaceChildren();describeReview(content,intent,parameters);intent.reviewSignature=signature;}
      const outcome=data.outcome;
      if(['succeeded','failed','expired','cancelled'].includes(outcome.state)) {
        intent.terminal=true;intent.pendingOperation=null;
        if(outcome.state==='succeeded') {
          reviewView('terminal','Changes saved.');
          try {await applySavedChanges(intent);reviewNotice('Changes saved.');}
          catch(error){reviewNotice(error.message || 'Changes saved. Retrying the page update automatically.',true,true);}
        } else if(outcome.state==='failed')reviewView('terminal',failedMessage(outcome.code),true);
        else if(outcome.state==='expired')reviewView('terminal','These changes expired before they could be saved. Your edits are kept.',true);
        else reviewView('terminal','Changes cancelled. Nothing was saved. Your edits are kept.');
        return;
      }
      if(outcome.state==='queued') {
        intent.confirmedAt ||= Date.now();
        if(intent.pendingOperation?.kind==='confirm')intent.pendingOperation=null;
      }
      if(outcome.state==='awaiting_confirmation' && intent.requestedSave && !intent.pendingOperation) {
        if(reviewFingerprint(parameters)!==intent.consentSignature) {
          intent.requestedSave=false;intent.confirmedAt=null;intent.awaitingUser=true;intent.proof=outcome.proof;
          reviewView('confirmation','Some details changed. Review these changes before saving.');
          $('confirm-review').disabled=false;$('withdraw-review').disabled=false;
          return;
        }
        intent.pendingOperation={kind:'confirm',proof:outcome.proof};
      }
      if(intent.pendingOperation && ['awaiting_confirmation','queued'].includes(outcome.state)) {
        const operation=intent.pendingOperation;
        reviewView(operation.kind==='withdraw'?'cancelling':'saving',operation.kind==='withdraw'?'Cancelling…':savingText(intent),false,true);
        try {
          const action=operation.kind==='withdraw'?(document.documentElement.dataset.cancelPath || 'withdraw'):operation.kind;
          const path='/internal/commands/'+encodeURIComponent(intent.commandId)+'/'+action;
          await request(path,operation.kind==='confirm'?{proof:operation.proof}:{},'POST',operation.kind==='confirm'?intent.confirmKey:intent.withdrawKey);
        } catch(error) {
          intent.uncertain=true;
          reviewNotice('The result is not yet verified. '+error.message+' Checking this same request automatically.',true,true);
        }
        return;
      }
      if(outcome.state==='queued' || intent.confirmedAt) {
        reviewView('saving',savingText(intent),false,true);
      } else if(outcome.state==='awaiting_confirmation') {
        intent.awaitingUser=true;intent.proof=outcome.proof;
        reviewView('confirmation','Confirm these changes to save.');$('withdraw-review').disabled=false;$('confirm-review').disabled=false;
      } else throw new Error('The request state is not yet verified.');
    } catch(error) {
      if(state.intent!==intent)return;
      if(!intent.commandId && !intent.uncertain && [400,401,403,409,422].includes(error.status)) {
        intent.terminal=true;
        reviewView('terminal','Changes were not saved. '+error.message+' Your edits are kept.',true);
      } else {
        intent.uncertain=true;
        reviewView('checking','The result is not yet verified. '+error.message+' Checking this same request automatically. You can close this window and use View save progress.',true,true);
      }
    } finally {
      intent.busy=false;
      if(state.intent===intent){controls();scheduleReview(intent);}
    }
  }
  function operate(kind) {
    const intent=state.intent;if(!intent || intent.busy || intent.terminal || intent.pendingOperation)return;
    if(kind==='withdraw' && !intent.commandId) {
      intent.awaitingUser=false;intent.terminal=true;
      reviewView('terminal','Changes cancelled. Nothing was saved. Your edits are kept.');controls();return;
    }
    intent.awaitingUser=false;intent.pollCount=0;
    if(kind==='confirm') {
      intent.consentSignature=reviewFingerprint(intent.reviewParameters);intent.requestedSave=true;intent.confirmedAt=Date.now();
      if(intent.proof)intent.pendingOperation={kind,proof:intent.proof};
    } else intent.pendingOperation={kind,proof:intent.proof};
    reviewView(kind==='withdraw'?'cancelling':'saving',kind==='withdraw'?'Cancelling…':'Saving…',false,true);
    advanceReview();
  }
  $('confirm-review').addEventListener('click',()=>operate('confirm'));$('withdraw-review').addEventListener('click',()=>operate('withdraw'));$('close-review').addEventListener('click',()=>$('review-dialog').close());
  $('resume-review').addEventListener('click',()=>{$('review-dialog').showModal();advanceReview();});
  $('review-dialog').addEventListener('close',()=>{if(state.intent?.terminal && !state.intent.needsRefresh){clearTimeout(reviewTimer);reviewTimer=null;state.intent=null;$('review-content').replaceChildren();}controls();});
  $('refresh-general').addEventListener('click',async()=>{if(state.busy)return;lock(true);notice('Reading general statistics…');try{const data=await request('/internal/members/general',{node_id:$('general-node').value,window:{kind:'calendar',start_date:$('general-start').value,end_date:$('general-end').value,timezone:state.session.actor.timezone}});const content=$('general-data');content.replaceChildren();content.append(el('h3',data.hardware.node_id+' · '+data.hardware.status),el('p','Recorded '+(data.recorded_usage.recorded_microseconds/3600000000).toLocaleString(undefined,{maximumFractionDigits:3})+' GPU hours. Total actual usage is unknown.','muted'));const grid=el('div',undefined,'general-grid');for(const gpu of data.hardware.gpus){const card=el('section',undefined,'general-card');card.append(el('strong','GPU '+gpu.index+' · '+gpu.name),el('p',gpu.state),el('p',gpu.utilization_percent===null?'Utilization unknown':'GPU '+gpu.utilization_percent+'%'),el('p',gpu.memory_used_mib===null?'Memory unknown':(gpu.memory_used_mib/1024).toFixed(1)+' / '+(gpu.memory_total_mib/1024).toFixed(1)+' GB'));grid.append(card);}content.append(grid);notice('');}catch(error){notice(error.message,true);}finally{lock(false);}});
  window.addEventListener('pagehide',clear);
  window.addEventListener('pageshow',event=>{if(event.persisted)connect().catch(error=>{if(error.status!==401)notice(error.message,true);});});
  connect().catch(error=>{if(error.status!==401)notice(error.message,true);});
})();
