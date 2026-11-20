"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const state = {session:null, csrf:"", epoch:0, busy:false, source:"registered", next:null, revision:null, rows:new Map(), page:[], edits:new Map(), selected:new Set(), secrets:new Map(), intent:null};
  function el(tag, value, className) {const node=document.createElement(tag);if(value!==undefined)node.textContent=value;if(className)node.className=className;return node;}
  function notice(value, error=false) {$('notice').textContent=value;$('notice').hidden=!value;$('notice').classList.toggle('error',error);}
  function reviewNotice(value,error=false) {$('review-status').textContent=value;$('review-status').classList.toggle('error',error);}
  function lock(value) {state.busy=value;document.querySelectorAll('#workspace button,#workspace input,#workspace select').forEach(node=>node.disabled=value);controls();}
  function controls() {
    const pending=state.intent && !state.intent.terminal;
    document.querySelectorAll('#members-panel input,#members-panel select,#members-panel button,#profile-form input,#profile-form button').forEach(node=>node.disabled=state.busy || pending);
    $('review-selected').disabled=state.busy || pending || !state.selected.size;
    $('next-page').disabled=state.busy || pending || !state.next;
    $('resume-review').hidden=!state.intent;
    $('resume-review').disabled=state.busy;
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
    state.epoch++;state.session=null;state.csrf='';state.intent=null;state.rows.clear();state.edits.clear();state.selected.clear();state.secrets.clear();state.page=[];state.next=null;state.busy=false;
    $('credential').value='';$('members-body').replaceChildren();$('review-content').replaceChildren();$('general-data').replaceChildren();$('profile-name').value='';$('profile-zone').value='';$('reason').value='';
    $('review-dialog').close();$('add-dialog').close();$('workspace').hidden=true;$('account').hidden=true;$('login-panel').hidden=false;
    ['new-id','new-name','new-zone','filter'].forEach(id=>$(id).value='');$('actor-name').textContent='';$('actor-role').textContent='';lock(false);
  }
  function tab(name) {for(const value of ['members','profile','general'])$(value+'-panel').hidden=value!==name;}
  async function connect() {
    state.session=await request('/internal/members/session',undefined,'GET');state.csrf=state.session.csrf_token;state.revision=state.session.actor.directory_revision;
    $('actor-name').textContent=state.session.actor.display_name;$('actor-role').textContent=state.session.actor.role;
    $('profile-name').value=state.session.actor.display_name;$('profile-zone').value=state.session.actor.timezone;
    $('members-tab').hidden=state.session.actor.role!=='admin';$('account').hidden=false;$('workspace').hidden=false;$('login-panel').hidden=true;
    $('general-node').replaceChildren(...state.session.nodes.map(node=>new Option(node,node)));
    const date=new Intl.DateTimeFormat('en-CA',{timeZone:state.session.actor.timezone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date()),parts=Object.fromEntries(date.map(part=>[part.type,part.value]));
    const today=parts.year+'-'+parts.month+'-'+parts.day;$('general-start').value=today;$('general-end').value=today;
    tab(state.session.actor.role==='admin'?'members':'profile');controls();
  }
  $('login-form').addEventListener('submit',async event=>{
    event.preventDefault();if(state.busy)return;state.epoch++;state.busy=true;$('sign-in').disabled=true;
    const credential=$('credential').value.trim();$('credential').value='';
    try{await request('/internal/members/login',{credential,remember:$('remember').checked});await connect();notice('Signed in.');}
    catch(error){notice(error.message,true);}finally{state.busy=false;$('sign-in').disabled=false;controls();}
  });
  $('sign-out').addEventListener('click',async()=>{if(state.busy)return;try{await request('/internal/members/logout',{});clear();notice('Signed out.');}catch(error){notice(error.message,true);}});
  $('members-tab').addEventListener('click',()=>tab('members'));
  $('profile-tab').addEventListener('click',()=>tab('profile'));
  $('general-tab').addEventListener('click',()=>tab('general'));
  function cacheRow(row, access) {
    const registered=row.registered, slack=row.slack;
    return {id:slack?.external_id || registered.external_id,user:registered?.user_id || null,name:slack?.display_name || registered.profile.display_name,profile:registered?.profile || {display_name:slack.display_name,timezone:slack.timezone || ''},status:registered?.status || 'unregistered',active:slack?.deleted!==true,eligible:registered || (slack?.deleted===false && slack?.is_bot===false && slack?.is_app_user!==true),mappings:registered?.mappings || [],role:access?.role || 'member',bindings:access?.bindings || [],revoked_when_member:access?.revoked_when_member || [],generation:access?.credential_generation || null,credential:access?.credential_available || false,revision:state.revision};
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
    } catch(error){notice(error.message,true);}finally{lock(false);}
  }
  function edit(row, key, value) {if(!state.edits.has(row.id))state.edits.set(row.id,{});state.edits.get(row.id)[key]=value;state.selected.add(row.id);document.querySelectorAll('#members-body tr').forEach(tr=>{if(tr.dataset.id===row.id)tr.querySelector('input[type="checkbox"]').checked=true;});controls();}
  function draft(row,key,fallback) {return state.edits.get(row.id)?.[key] ?? fallback;}
  function field(row,key,value,label) {const node=el('input');node.value=draft(row,key,value);node.setAttribute('aria-label',row.id+' '+label);node.autocomplete='off';node.addEventListener('input',()=>edit(row,key,node.value));return node;}
  function render() {
    const heading=el('tr');['','Member','Profile','Role / access',...state.session.nodes,'Status'].forEach(value=>heading.append(el('th',value)));$('members-head').replaceChildren(heading);$('members-body').replaceChildren();
    for(const id of state.page) {
      const row=state.rows.get(id),tr=el('tr');tr.dataset.id=id;tr.dataset.search=(row.name+' '+id).toLowerCase();tr.dataset.inactive=String(!row.active || row.status==='suspended');
      const check=el('input');check.type='checkbox';check.checked=state.selected.has(id);check.setAttribute('aria-label','Select '+id);check.addEventListener('change',()=>{if(check.checked)state.selected.add(id);else state.selected.delete(id);controls();});
      const selectCell=el('td');selectCell.append(check);tr.append(selectCell);
      const who=el('td');who.append(el('div',row.name,'member-name'),el('div',id,'member-id'),el('p',row.user?'Registered':'Not registered','muted small'));if(!row.eligible){who.append(el('p','Not an active human Slack account','muted small'));check.disabled=true;check.classList.add('no-edit');}tr.append(who);
      const profile=el('td');profile.append(field(row,'display_name',row.profile.display_name,'display name'),field(row,'timezone',row.profile.timezone,'timezone'));tr.append(profile);
      const roleCell=el('td'),role=el('select');role.setAttribute('aria-label',id+' role');role.append(new Option('Member','member'),new Option('Admin','admin'));role.value=draft(row,'role',row.role);role.addEventListener('change',()=>edit(row,'role',role.value));if(!row.user){role.disabled=true;role.classList.add('no-edit');}roleCell.append(role);
      if(row.user){const credential=el('button','Prepare new credential','quiet small-button');credential.type='button';credential.addEventListener('click',()=>prepareCredential(row));if(row.user===state.session.actor.user_id){credential.disabled=true;credential.classList.add('no-edit');credential.title='Another administrator must rotate your login credential.';}roleCell.append(el('p',row.credential?'Personal login configured':'Uses configured login or no credential','muted small'),credential);if(state.secrets.has(id))roleCell.append(el('p','New credential prepared','changed-note'));}tr.append(roleCell);
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
  $('add-member').addEventListener('click',()=>{$('new-id').value='';$('new-name').value='';$('new-zone').value=state.session.actor.timezone;$('add-dialog').showModal();});
  $('close-add').addEventListener('click',()=>$('add-dialog').close());
  $('add-form').addEventListener('submit',event=>{event.preventDefault();const id=$('new-id').value.trim();if(state.rows.has(id)){notice('This member is already loaded. Edit their existing row.',true);return;}const row={id,user:null,name:$('new-name').value.trim(),profile:{display_name:$('new-name').value.trim(),timezone:$('new-zone').value.trim()},status:'unregistered',active:true,eligible:true,mappings:[],role:'member',bindings:[],generation:null,credential:false,revision:state.revision,manual:true};state.rows.set(id,row);state.page.push(id);state.selected.add(id);$('add-dialog').close();render();});
  async function prepareCredential(row) {
    if(state.busy || row.status!=='active')return;const epoch=state.epoch;
    try {const bytes=crypto.getRandomValues(new Uint8Array(32)),raw=btoa(String.fromCharCode(...bytes)).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(raw))),byte=>byte.toString(16).padStart(2,'0')).join('');if(epoch!==state.epoch)return;state.secrets.set(row.id,{raw,digest,generation:row.generation});state.selected.add(row.id);render();notice('A new personal credential is prepared. Review and confirm to activate it; the secret is shown only after success.');}catch(error){if(epoch===state.epoch)notice('A secure credential could not be generated in this browser.',true);}
  }
  function changes() {
    const result=[];
    for(const id of state.selected) {
      const row=state.rows.get(id);if(!row?.eligible)throw new Error('Only verified active human members can be registered.');
      if(!row.manual && row.revision!==state.revision)throw new Error('Load the current page for '+id+' before review. Your edits are preserved.');
      const profile={display_name:draft(row,'display_name',row.profile.display_name).trim(),timezone:draft(row,'timezone',row.profile.timezone).trim()};
      if(!profile.display_name || !profile.timezone)throw new Error('Enter a display name and timezone for '+id+'.');
      try{new Intl.DateTimeFormat('en',{timeZone:profile.timezone}).format();}catch{throw new Error('Enter a valid IANA timezone for '+id+'.');}
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
      if(!row.user)result.push({type:'register_identity',subject:{provider:'slack',namespace:state.session.workspace_namespace,external_id:id},profile,mappings});
      else {
        const role=draft(row,'role',row.role),active=draft(row,'active',row.status==='active'),change={type:'edit_member',user_id:row.user};
        if(profile.display_name!==row.profile.display_name || profile.timezone!==row.profile.timezone)change.profile=profile;
        if(active!==(row.status==='active'))change.active=active;
        if(role!==row.role || state.edits.get(id)?.role!==undefined){change.role=role;change.previous_bindings=row.bindings;}
        if(Object.keys(change).length>2)result.push(change);
        const secret=state.secrets.get(id);if(secret)result.push({type:'issue_login_credential',user_id:row.user,token_digest:secret.digest,expected_generation:secret.generation});
      }
    }
    if(!result.length)throw new Error('The selected members have no changes.');
    if(result.length>30)throw new Error('This selection is too large. Split it into smaller batches.');
    return result;
  }
  function begin(command,parameters,selected) {
    if(state.busy || (state.intent && !state.intent.terminal))return;
    state.intent={command,parameters,selected:[...selected],users:new Set([...selected].map(id=>state.rows.get(id)?.user).filter(Boolean)),key:crypto.randomUUID(),confirmKey:crypto.randomUUID(),withdrawKey:crypto.randomUUID(),commandId:null,proof:null,busy:false,terminal:false,secretCopy:new Map([...selected].filter(id=>state.secrets.has(id)).map(id=>[id,state.secrets.get(id)]))};
    $('review-content').replaceChildren();$('review-dialog').showModal();submit();controls();
  }
  $('review-selected').addEventListener('click',()=>{try{const reason=$('reason').value.trim();if(!reason)throw new Error('Enter a reason for these changes.');begin('identity.manage_members',{expected_revision:state.revision,reason,changes:changes()},state.selected);}catch(error){notice(error.message,true);}});
  $('profile-form').addEventListener('submit',event=>{event.preventDefault();begin('identity.edit_own_profile',{expected_revision:state.session.actor.directory_revision,profile:{display_name:$('profile-name').value.trim(),timezone:$('profile-zone').value.trim()}},new Set());});
  function reviewLock() {$('confirm-review').disabled=$('withdraw-review').disabled=$('refresh-review').disabled=true;}
  async function submit() {
    const intent=state.intent;if(!intent || intent.busy)return;intent.busy=true;reviewLock();reviewNotice('Preparing this request. Nothing has changed yet.');
    try{const data=await request('/internal/commands',{command:intent.command,parameters:intent.parameters},'POST',intent.key);intent.commandId=data.outcome?.command_id;if(!intent.commandId)throw new Error('The request was not accepted.');intent.busy=false;await refreshReview();}
    catch(error){reviewNotice(error.message+' Refresh retries the same request.',true);$('refresh-review').disabled=false;}finally{intent.busy=false;}
  }
  function safeTarget(change,intent) {
    if(change.type==='register_identity')return change.subject.provider==='slack' && change.subject.namespace===state.session.workspace_namespace && intent.selected.includes(change.subject.external_id) && change.mappings.every(link=>state.session.nodes.includes(link.node_id));
    if(change.type==='remove_node_user')return intent.users.has(change.expected_user_id) && state.session.nodes.includes(change.node_id);
    if(change.type==='assign_node_user')return intent.users.has(change.user_id) && state.session.nodes.includes(change.node_id);
    return ['edit_member','issue_login_credential'].includes(change.type) && intent.users.has(change.user_id);
  }
  function describeReview(content,intent,parameters) {
    if(intent.command==='identity.edit_own_profile') {
      content.append(el('h3','Your profile'),el('p','Display name: '+parameters.profile.display_name),el('p','Timezone: '+parameters.profile.timezone));
    } else {
      const groups=new Map();
      for(const change of parameters.changes) {
        const user=change.user_id || change.expected_user_id,row=change.type==='register_identity'?state.rows.get(change.subject.external_id):[...state.rows.values()].find(row=>row.user===user);
        const key=change.type==='register_identity'?change.subject.external_id:user;
        if(!groups.has(key))groups.set(key,{row,title:(change.type==='register_identity'?change.profile.display_name:row?.name || user)+(row?.id?' · '+row.id:''),lines:[]});
        const lines=groups.get(key).lines;
        if(change.type==='register_identity') {
          lines.push('Register as Member.','Display name: '+change.profile.display_name,'Timezone: '+change.profile.timezone);
          if(!change.mappings.length)lines.push('No Linux username links.');
          change.mappings.forEach(link=>lines.push('Link '+link.node_id+' / '+link.os_username));
        } else if(change.type==='edit_member') {
          if(change.profile)lines.push('Display name: '+change.profile.display_name,'Timezone: '+change.profile.timezone);
          if(change.active===false)lines.push('Remove this member and disable login. History is retained.');
          if(change.active===true)lines.push('Restore this member. Issue a new credential if the old one was revoked.');
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
        else lines.push('Replace the personal login credential and sign out existing sessions. The new secret is shown only after success.');
      }
      for(const group of groups.values()){content.append(el('h3',group.title));const list=el('ul');group.lines.forEach(line=>list.append(el('li',line)));content.append(list);}
      content.append(el('h3','Reason'),el('p',parameters.reason));
    }
    const details=el('details',undefined,'request-details');details.append(el('summary','Request details'),el('p','Request ID: '+intent.commandId,'muted small'),el('pre',JSON.stringify(parameters,null,2)));content.append(details);
  }
  async function refreshReview() {
    const intent=state.intent;if(!intent || intent.busy)return;if(!intent.commandId)return submit();intent.busy=true;reviewLock();
    try {
      const data=await request('/internal/commands/'+encodeURIComponent(intent.commandId)+'/review',undefined,'GET');intent.proof=null;
      if(data.request.command!==intent.command || data.request.scope.type!=='global' || data.request.scope.id!=='*')throw new Error('The canonical request target differs.');
      const parameters=data.request.parameters;
      if(intent.command==='identity.manage_members' && !parameters.changes.every(change=>safeTarget(change,intent)))throw new Error('The canonical member selection differs.');
      const content=$('review-content');content.replaceChildren();describeReview(content,intent,parameters);
      if(intent.command==='identity.manage_members')content.append(el('p','New registrations receive Member access. Removing users disables login and preserves history. Role changes retain unrelated business bindings; a downgrade removes old directory-admin/full-statistics bindings.','muted small'));
      const outcome=data.outcome;reviewNotice('Request: '+outcome.state);$('withdraw-review').disabled=!['awaiting_confirmation','queued'].includes(outcome.state);
      if(outcome.state==='awaiting_confirmation'){const current=await request('/internal/members/session',undefined,'GET');if(current.actor.directory_revision!==parameters.expected_revision)reviewNotice('Directory changed. Withdraw, reload the selected members, and review again.',true);else{intent.proof=outcome.proof;$('confirm-review').disabled=false;reviewNotice('Nothing has changed yet. Confirm queues exactly this request.');}}
      if(['succeeded','failed','expired','cancelled'].includes(outcome.state)) {
        intent.terminal=true;
        if(outcome.state==='succeeded') {
          reviewNotice('Changes saved.');
          for(const id of intent.selected){state.edits.delete(id);state.selected.delete(id);state.secrets.delete(id);}
          for(const [id,secret] of intent.secretCopy){content.append(el('h3','Personal credential · '+id));const output=el('input');output.type='password';output.readOnly=true;output.value=secret.raw;output.className='credential-output';output.setAttribute('aria-label',id+' new personal credential');const reveal=el('button','Reveal once','quiet small-button');reveal.type='button';reveal.addEventListener('click',()=>{output.type=output.type==='password'?'text':'password';reveal.textContent=output.type==='password'?'Reveal once':'Hide';});content.append(output,reveal,el('p','Save this credential now. It cannot be retrieved after closing or reloading.','muted small'));}
          state.session=await request('/internal/members/session',undefined,'GET');state.csrf=state.session.csrf_token;state.revision=state.session.actor.directory_revision;
          $('actor-name').textContent=state.session.actor.display_name;$('actor-role').textContent=state.session.actor.role;$('profile-name').value=state.session.actor.display_name;$('profile-zone').value=state.session.actor.timezone;
          if(state.session.actor.role==='admin')await load('registered');else{$('members-tab').hidden=true;tab('profile');}
        } else reviewNotice('Request '+outcome.state+'. Existing edits are retained; reload the selected rows before another review.',outcome.state==='failed');
      }
      $('refresh-review').disabled=false;
    } catch(error){reviewNotice(error.message,true);$('refresh-review').disabled=false;}
    finally {intent.busy=false;controls();}
  }
  async function operate(kind) {const intent=state.intent;if(!intent || intent.busy || (kind==='confirm' && !intent.proof))return;intent.busy=true;reviewLock();try{await request('/internal/commands/'+encodeURIComponent(intent.commandId)+'/'+kind,kind==='confirm'?{proof:intent.proof}:{},'POST',kind==='confirm'?intent.confirmKey:intent.withdrawKey);intent.busy=false;await refreshReview();}catch(error){reviewNotice(error.message+' Refresh to verify the result.',true);$('refresh-review').disabled=false;}finally{intent.busy=false;}}
  $('confirm-review').addEventListener('click',()=>operate('confirm'));$('withdraw-review').addEventListener('click',()=>operate('withdraw'));$('refresh-review').addEventListener('click',refreshReview);$('close-review').addEventListener('click',()=>$('review-dialog').close());
  $('resume-review').addEventListener('click',()=>{$('review-dialog').showModal();refreshReview();});
  $('review-dialog').addEventListener('close',()=>{if(state.intent?.terminal){state.intent=null;$('review-content').replaceChildren();}controls();});
  $('refresh-general').addEventListener('click',async()=>{if(state.busy)return;lock(true);notice('Reading general statistics…');try{const data=await request('/internal/members/general',{node_id:$('general-node').value,window:{kind:'calendar',start_date:$('general-start').value,end_date:$('general-end').value,timezone:state.session.actor.timezone}});const content=$('general-data');content.replaceChildren();content.append(el('h3',data.hardware.node_id+' · '+data.hardware.status),el('p','Recorded '+(data.recorded_usage.recorded_microseconds/3600000000).toLocaleString(undefined,{maximumFractionDigits:3})+' GPU hours. Total actual usage is unknown.','muted'));const grid=el('div',undefined,'general-grid');for(const gpu of data.hardware.gpus){const card=el('section',undefined,'general-card');card.append(el('strong','GPU '+gpu.index+' · '+gpu.name),el('p',gpu.state),el('p',gpu.utilization_percent===null?'Utilization unknown':'GPU '+gpu.utilization_percent+'%'),el('p',gpu.memory_used_mib===null?'Memory unknown':(gpu.memory_used_mib/1024).toFixed(1)+' / '+(gpu.memory_total_mib/1024).toFixed(1)+' GB'));grid.append(card);}content.append(grid);notice('');}catch(error){notice(error.message,true);}finally{lock(false);}});
  window.addEventListener('pagehide',clear);
  window.addEventListener('pageshow',event=>{if(event.persisted)connect().catch(error=>{if(error.status!==401)notice(error.message,true);});});
  connect().catch(error=>{if(error.status!==401)notice(error.message,true);});
})();
