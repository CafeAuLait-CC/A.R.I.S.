"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const workbench = window.arisWorkbench;
  const AUDIT_REASON = 'Member changes saved from the member portal.';
  let reviewTimer = null, invitationTimer = null;
  let memberEditor = null;
  let overviewBusy = false, overviewVersion = 0, overviewPreset = 'week', archiveNodes = [];
  let usageAccess = Promise.resolve(null);
  let overviewSnapshot = [], renderedHardware = '', livePreview = null, openGpu = null;
  const state = {session:null, view:null, csrf:"", epoch:0, busy:false, authBusy:false, accountBusy:false, authMode:'login',invitation:null,invitationSendAfter:0,source:"registered", directoryVisited:false, directoryStatus:'', next:null, revision:null, rows:new Map(), page:[], edits:new Map(), selected:new Set(), intent:null};
  function el(tag, value, className) {const node=document.createElement(tag);if(value!==undefined)node.textContent=value;if(className)node.className=className;return node;}
  function notice(value, error=false) {$('notice').textContent=value;$('notice').hidden=!value;$('notice').classList.toggle('error',error);}
  function reviewNotice(value,error=false,working=false) {if($('review-status').textContent!==value)$('review-status').textContent=value;$('review-status').classList.toggle('error',error);$('review-status').classList.toggle('working',working);$('review-dialog').setAttribute('aria-busy',String(working));}
  function lock(value) {state.busy=value;document.querySelectorAll('#workspace button,#workspace input,#workspace select').forEach(node=>node.disabled=value);controls();}
  function controls() {
    const pending=state.intent && (!state.intent.terminal || state.intent.needsRefresh);
    const disabled=state.busy || Boolean(pending) || state.accountBusy;
    document.querySelectorAll('#members-panel input,#members-panel button,#profile-form input,#profile-form button,#account-password-form input,#account-password-form button').forEach(node=>node.disabled=disabled || node.classList.contains('no-edit'));
    $('review-selected').disabled=state.busy || pending || !state.selected.size;
    $('next-page').hidden=!state.next;
    $('resume-review').hidden=!state.intent;
    $('resume-review').disabled=state.busy;
    $('sign-out').disabled=state.busy || state.accountBusy;
    $('bulk-controls').hidden=!state.selected.size;
    $('selection-count').textContent='已选 '+state.selected.size+' 人';
    const selected=[...state.selected].map(id=>state.rows.get(id));
    $('bulk-member').disabled=disabled || !selected.some(row=>row?.user);
    for(const id of ['bulk-admin','bulk-remove'])$(id).disabled=disabled || selected.some(row=>!row?.user);
    const visible=visibleMemberRows().filter(row=>state.rows.get(row.dataset.id).eligible);
    const count=visible.filter(row=>state.selected.has(row.dataset.id)).length;
    $('select-visible').checked=visible.length>0 && count===visible.length;
    $('select-visible').indeterminate=count>0 && count<visible.length;
    $('select-visible').disabled=disabled || !visible.length;
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
    stopLivePreview(false);
    overviewSnapshot=[];renderedHardware='';openGpu=null;
    clearTimeout(reviewTimer);reviewTimer=null;
    clearTimeout(invitationTimer);invitationTimer=null;
    state.epoch++;state.session=null;state.view=null;state.csrf='';state.intent=null;state.rows.clear();state.edits.clear();state.selected.clear();state.page=[];state.next=null;state.busy=false;state.accountBusy=false;state.invitation=null;
    state.source='registered';state.directoryVisited=false;state.directoryStatus='';
    $('members-body').replaceChildren();$('review-content').replaceChildren();$('general-data').replaceChildren();$('profile-name').value='';$('profile-email').value='';
    ['login-password','invitation-code','invitation-password','invitation-confirm','current-password','account-password','account-confirm','account-username'].forEach(id=>$(id).value='');
    $('review-dialog').close();$('member-dialog').close();$('gpu-dialog').close();
    memberEditor=null;$('manage-mappings').replaceChildren();
    $('workspace').hidden=true;$('login-panel').hidden=false;
    $('login-feedback').append($('notice'));workbench.session(null);
    archiveNodes=[];overviewBusy=false;overviewVersion++;overviewPreset='week';
    $('overview-stats').replaceChildren();
    $('overview-sample').textContent='';
    $('overview-trend').replaceChildren(el('p','正在读取已记录用量…','muted small'));
    $('overview-plan-link').hidden=true;
    snapshotState([el('p','正在读取计划…','muted small')]);
    $('general-data').setAttribute('aria-busy','false');
    $('refresh-general').disabled=false;
    $('general-message').hidden=true;$('gpu-detail').replaceChildren();
    ['manage-id','manage-name','manage-email','manage-username','filter'].forEach(id=>$(id).value='');$('actor-name').textContent='';$('actor-role').textContent='';authMode('login');lock(false);
  }
  function tab(name) {
    if(name==='members' && state.session.actor.role!=='admin')name='general';
    if(name!=='general')stopLivePreview();
    if(state.view!==name)notice('');
    state.view=name;
    for(const value of ['members','profile','general']) {
      $(value+'-panel').hidden=value!==name;
    }
    workbench.view(name);
    if(name==='general' && !$('general-data').childElementCount)refreshOverview();
    if(name==='members' && !state.directoryVisited)load('registered');
  }
  function currentView() {
    const view=window.location.hash.slice(1);
    return view==='members' || view==='profile' ? view : 'general';
  }
  function configureWorkspace(actor) {
    workbench.session(actor);
    workbench.usage(null);
    archiveNodes=[];
    $('welcome-title').textContent='欢迎回来，'+actor.display_name.trim().split(/\s+/)[0];
    const epoch=state.epoch;
    usageAccess=request('/internal/usage/session').then(access=>{
      if(epoch!==state.epoch || state.session?.actor!==actor)return null;
      workbench.usage(access);
      if(actor.role==='admin')archiveNodes=access.activity_preview_nodes;
      return access;
    }).catch(()=>null);
  }
  async function connect() {
    state.session=await request('/internal/members/session',undefined,'GET');state.csrf=state.session.csrf_token;state.revision=state.session.actor.directory_revision;
    $('profile-name').value=state.session.actor.display_name;$('profile-email').value=state.session.actor.email || '';
    accountFields();
    configureWorkspace(state.session.actor);
    $('overview-live-toggle').hidden=!state.session.live_preview_available;
    $('workspace').hidden=false;$('login-panel').hidden=true;
    $('workspace-feedback').append($('notice'));
    $('general-node').replaceChildren(new Option('全部授权节点',''),...state.session.nodes.map(node=>new Option(node,node)));
    setOverviewRange(overviewPreset);
    tab(currentView());controls();
  }
  function username(value) {
    const normalized=value.trim().toLowerCase();
    if(!/^[a-z][a-z0-9_.-]{2,63}$/.test(normalized))throw new Error('Use a username of 3–64 characters, starting with a letter and containing letters, numbers, dots, underscores or hyphens.');
    return normalized;
  }
  function password(value,confirmation=value) {
    if([...value].length<6 || [...value].length>128)throw new Error('Use a password of 6–128 characters.');
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
    notice('');
    state.authMode=mode;state.invitation=null;
    $('login-form').hidden=mode!=='login';$('invitation-form').hidden=mode==='login';$('invitation-fields').hidden=true;
    $('login-title').textContent=mode==='login'?'Sign in':mode==='activate'?'Sign up':'Reset password';
    $('login-intro').textContent=mode==='login'?'Enter your username and password.':'Request an invitation code in Slack, then choose your password.';
    $('complete-invitation').textContent=mode==='activate'?'Activate account':'Reset password';
    $('invitation-message').textContent='';
    ['login-password','invitation-code','invitation-password','invitation-confirm'].forEach(id=>$(id).value='');
    $('login-signup').hidden=mode!=='login';
    $('auth-sign-in').hidden=mode==='login';
    $('auth-reset').hidden=mode==='reset';
    authControls();
  }
  for(const [id,mode] of [['auth-sign-in','login'],['auth-activate','activate'],['auth-reset','reset']]) {
    $(id).addEventListener('click',()=>{
      authMode(mode);
      $(mode==='login'?'login-username':'invitation-username').focus();
    });
  }
  async function signIn(payload) {
    if(state.authBusy)return;state.epoch++;state.authBusy=true;authControls();notice('Signing in…');
    try{const data=await request('/internal/members/login',payload);if(data.signed_in!==true)throw new Error('Sign-in could not be verified.');await connect();notice('');}
    catch(error){notice(error.message,true);}finally{state.authBusy=false;authControls();controls();}
  }
  $('login-form').addEventListener('submit',event=>{
    event.preventDefault();if(state.authBusy)return;
    try{const payload={username:username($('login-username').value),password:$('login-password').value,remember:$('remember').checked};$('login-password').value='';signIn(payload);}catch(error){notice(error.message,true);}
  });
  $('invitation-username').addEventListener('input',()=>{state.invitation=null;$('invitation-fields').hidden=true;['invitation-code','invitation-password','invitation-confirm'].forEach(id=>$(id).value='');$('invitation-message').textContent='Request a code for this username.';});
  $('send-invitation').addEventListener('click',async()=>{
    if(state.authBusy || Date.now()<state.invitationSendAfter || state.authMode==='login')return;
    let account;try{account=username($('invitation-username').value);}catch(error){notice(error.message,true);return;}
    state.authBusy=true;state.invitation=null;state.invitationSendAfter=Date.now()+60000;authControls();notice('');$('invitation-fields').hidden=true;$('invitation-message').textContent='Sending invitation code…';
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
    try {const data=await request('/internal/members/'+(invitation.purpose==='activate'?'register':'password-reset'),payload);if(data.signed_in!==true)throw new Error('Account activation could not be verified.');acknowledged=true;state.invitation=null;await connect();notice('');}
    catch(error){notice(acknowledged?'Password saved. Sign in with your new password to continue.':error.message+(error.status?'':' The result is uncertain. Try signing in with your chosen password before requesting another code.'),true);}
    finally{state.authBusy=false;authControls();controls();}
  });
  function accountFields() {
    const account=state.session?.account || {username:null,password_ready:false};
    $('account-username').value=account.username || '';$('account-username').readOnly=account.password_ready;
    $('current-password-field').hidden=!account.password_ready;$('current-password').required=account.password_ready;
    $('save-password').textContent=account.password_ready?'Update password':'Set password';
    $('account-note').textContent=account.password_ready?'Your login username is managed by an administrator.':'Set your username and password.';
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
  window.addEventListener('hashchange',()=>{if(state.session)tab(currentView());});
  function cacheRow(row, access) {
    const registered=row.registered, slack=row.slack;
    return {id:slack?.external_id || registered.external_id,user:registered?.user_id || null,name:slack?.display_name || registered.profile.display_name,profile:registered?.profile || {display_name:slack.display_name,timezone:slack.timezone || state.session.actor.timezone,email:null},status:registered?.status || 'unregistered',slackDeleted:slack?.deleted ?? null,eligible:registered || (slack?.deleted===false && slack?.is_bot===false && slack?.is_app_user!==true),mappings:registered?.mappings || [],role:access?.role || 'member',bindings:access?.bindings || [],revoked_when_member:access?.revoked_when_member || [],login_username:access?.login_username || null,password_ready:access?.password_ready===true,revision:state.revision};
  }
  async function load(source, next=false) {
    if(state.busy)return false;
    const epoch=state.epoch;
    state.directoryVisited=true;
    if(source!==state.source) {state.page=[];state.next=null;}
    state.source=source;
    state.directoryStatus='正在读取成员…';
    lock(true);render();
    try {
      if(source==='slack' && !state.session.slack_roster_available) {
        state.directoryStatus='此门户尚未配置 Slack 名单。';
        render();
        return true;
      }
      const data=source==='slack'
        ? await request('/internal/members/slack-members',{cursor:next?state.next:null})
        : await request('/internal/members/directory',{after:next?state.next:null,limit:20});
      const page=source==='slack'?data.roster:data.directory;
      const previous=state.revision;
      state.revision=page.revision;
      state.next=page.next_cursor;
      const access=new Map(data.access.map(item=>[item.user_id,item]));
      state.page=page.members.map(member=>{
        const item=source==='slack'?member:{registered:member,slack:null};
        const row=cacheRow(item,access.get(item.registered?.user_id));
        state.rows.set(row.id,row);
        return row.id;
      });
      if(source==='registered' && !next) {
        for(const row of state.rows.values())if(row.manual)state.page.push(row.id);
      }
      state.directoryStatus='';
      render();
      if(previous!==state.revision && state.edits.size)notice('成员目录有更新。已保留暂存的变更，核对前请重新读取涉及的成员。');
      return true;
    } catch(error) {
      if(epoch===state.epoch){
        state.directoryStatus='成员名单读取失败。可重新选择此页签重试。';
        render();notice(error.message,true);
      }
      return false;
    } finally {
      if(epoch===state.epoch)lock(false);
    }
  }
  function draft(row,key,fallback) {return state.edits.get(row.id)?.[key] ?? fallback;}
  function mappingsFor(row,node) {return row?.mappings.filter(link=>link.node_id===node) || [];}
  function memberAccount(row) {
    if(!row.user)return row.manual?['待登记','reserved']:['未登记','offline'];
    if(!draft(row,'active',row.status==='active'))return ['已停用','danger'];
    return row.password_ready?['已激活','available']:['待激活','reserved'];
  }
  function memberInitials(name) {
    return name.trim().split(/\s+/).slice(0,2).map(part=>Array.from(part)[0]).join('').toUpperCase();
  }
  function visibleMemberRows() {
    return [...document.querySelectorAll('#members-body tr')].filter(row=>!row.hidden);
  }
  function focusMember(id) {
    const row=visibleMemberRows().find(item=>item.dataset.id===id);
    const target=row?.querySelector('.member-manage') || $('members-'+state.source);
    target.focus();
  }
  function render() {
    const slack=state.source==='slack';
    for(const source of ['registered','slack']) {
      const tab=$('members-'+source);
      tab.setAttribute('aria-selected',String(source===state.source));
      tab.tabIndex=source===state.source?0:-1;
    }
    $('member-results').setAttribute('aria-labelledby','members-'+state.source);
    const heading=el('tr'),selectAll=el('th');
    selectAll.scope='col';selectAll.append($('select-visible'));
    heading.append(selectAll);
    const mappingHeading=state.session.nodes.length<=2?'映射 · '+state.session.nodes.join(' / '):'Linux 映射';
    const columns=slack
      ? ['显示名 / 邮箱','Slack Member ID','Slack 状态','A.R.I.S. 账户','操作']
      : ['显示名 / 邮箱','Slack Member ID','登录用户名','角色','A.R.I.S. 账户',mappingHeading,'操作'];
    for(const value of columns) {
      const cell=el('th',value);cell.scope='col';heading.append(cell);
    }
    $('members-head').replaceChildren(heading);
    $('members-body').replaceChildren();
    for(const id of state.page) {
      const row=state.rows.get(id),tr=el('tr');
      const name=draft(row,'display_name',row.profile.display_name);
      const email=draft(row,'email',row.profile.email || '');
      tr.dataset.id=id;tr.dataset.search=[name,row.name,id,email].join(' ').toLowerCase();
      tr.dataset.inactive=String(slack?row.slackDeleted!==false:row.status==='suspended');
      const check=el('input');
      check.type='checkbox';check.checked=state.selected.has(id);
      check.setAttribute('aria-label','选择 '+name);
      check.addEventListener('change',()=>{
        if(check.checked)state.selected.add(id);else state.selected.delete(id);
        controls();
      });
      const selectCell=el('td');
      if(row.eligible)selectCell.append(check);
      const who=el('td'),identity=el('div',undefined,'member-identity'),info=el('div');
      const avatar=el('span',memberInitials(name),'aris-avatar');
      avatar.setAttribute('aria-hidden','true');
      info.append(el('strong',name,'member-name'));
      if(email)info.append(el('small',email,'muted'));
      identity.append(avatar,info);who.append(identity);
      tr.append(selectCell,who,el('td',id,'member-mono'));
      if(slack) {
        const status=el('td');
        const value=row.slackDeleted===false?['活跃','available']:row.slackDeleted?['非活跃','offline']:['未知','offline'];
        status.append(badge(value));tr.append(status);
      } else {
        const role=el('td'),value=draft(row,'role',row.role);
        role.append(el('span',value==='admin'?'Admin':'Member','member-role '+(value==='admin'?'admin':'')));
        tr.append(el('td',draft(row,'login_username',row.login_username || '—'),'member-mono'),role);
      }
      const account=el('td');account.append(badge(memberAccount(row)));
      if(state.edits.has(id) || row.manual)account.append(el('small','有暂存变更','member-draft'));
      tr.append(account);
      if(!slack) {
        const links=el('td',undefined,'member-mono member-mapping-cell');
        const values=state.session.nodes.map(node=>{
          const existing=mappingsFor(row,node);
          return draft(row,'node:'+node,existing.map(link=>link.os_username).join(', ')) || '—';
        });
        links.textContent=values.join(' / ') || '—';
        links.title=state.session.nodes.map((node,index)=>node+' · '+values[index]).join('\n');
        tr.append(links);
      }
      const actions=el('td'),button=el('button',row.user || row.manual?'管理':'登记','member-manage');
      button.type='button';
      button.setAttribute('aria-label',button.textContent+' '+name);
      if(!row.eligible){
        button.classList.add('no-edit');button.title='仅可登记活跃的 Slack 人员账户。';
      }
      button.addEventListener('click',()=>openMemberEditor(row));
      actions.append(button);tr.append(actions);$('members-body').append(tr);
    }
    filterMembers();
  }
  function filterMembers() {
    const query=$('filter').value.trim().toLowerCase();
    for(const row of document.querySelectorAll('#members-body tr')) {
      row.hidden=($('active-only').checked && row.dataset.inactive==='true') || (Boolean(query) && !row.dataset.search.includes(query));
    }
    const count=visibleMemberRows().length;
    $('member-count').textContent=state.directoryStatus?'':count+' 个结果';
    $('members-empty').textContent=state.directoryStatus || '没有符合条件的成员。可调整搜索词或活跃筛选。';
    $('members-empty').hidden=!state.directoryStatus && count>0;
    controls();
  }
  function chooseMemberSource(source) {
    if(state.busy)return;
    notice('');
    load(source).then(()=>{if(state.view==='members' && state.source===source)$('members-'+source).focus();});
  }
  for(const source of ['registered','slack']) {
    const tab=$('members-'+source);
    tab.addEventListener('click',()=>chooseMemberSource(source));
    tab.addEventListener('keydown',event=>{
      if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
      event.preventDefault();
      const target=event.key==='Home'?'registered':event.key==='End'?'slack':source==='slack'?'registered':'slack';
      chooseMemberSource(target);
    });
  }
  $('filter').addEventListener('input',filterMembers);
  $('active-only').addEventListener('change',filterMembers);
  $('select-visible').addEventListener('change',()=>{
    const checked=$('select-visible').checked;
    for(const row of visibleMemberRows()) {
      const id=row.dataset.id;
      if(!state.rows.get(id).eligible)continue;
      if(checked)state.selected.add(id);else state.selected.delete(id);
      row.querySelector('input[type="checkbox"]').checked=checked;
    }
    controls();
  });
  $('clear-selection').addEventListener('click',()=>{
    state.selected.clear();render();$('select-visible').focus();
  });
  for(const [id,key,value] of [['bulk-member','role','member'],['bulk-admin','role','admin'],['bulk-remove','active',false]]) {
    $(id).addEventListener('click',()=>{
      for(const selected of state.selected) {
        if(!state.rows.get(selected).user)continue;
        state.edits.set(selected,{...state.edits.get(selected),[key]:value});
      }
      render();
    });
  }
  $('next-page').addEventListener('click',()=>load(state.source,true));

  function editorError(value) {
    const error=$('member-editor-error');
    error.textContent=value;error.hidden=!value;
  }
  function openMemberEditor(row=null) {
    const registered=Boolean(row?.user),mappingInputs=new Map();
    memberEditor={row,mappingInputs};
    $('member-dialog-title').textContent=registered || row?.manual?'管理成员':row?'登记成员':'添加成员';
    $('member-editor-summary').textContent=row?draft(row,'display_name',row.profile.display_name)+' · '+row.id:'使用已核对的 Slack Member ID 登记成员。';
    $('manage-id').value=row?.id || '';$('manage-id').readOnly=Boolean(row);
    $('manage-name').value=row?draft(row,'display_name',row.profile.display_name):'';
    $('manage-email').value=row?draft(row,'email',row.profile.email || ''):'';
    $('manage-username').value=row?draft(row,'login_username',row.login_username || ''):'';
    $('manage-role').value=row?draft(row,'role',row.role):'member';
    $('manage-active').value=registered && !draft(row,'active',row.status==='active')?'suspended':'active';
    $('manage-role').disabled=$('manage-active').disabled=!registered;
    $('member-editor-note').textContent=registered
      ? '停用将撤销登录，历史预约和用量记录会保留。'
      : '新登记账户默认为 Member。设置登录用户名后，成员可在 Slack 获取激活邀请码。';
    const fields=$('manage-mappings');fields.replaceChildren();
    for(const node of state.session.nodes) {
      const existing=mappingsFor(row,node),label=el('label',node),field=el('input');
      field.dataset.node=node;field.autocomplete='off';field.spellcheck=false;
      const aliases=existing.map(link=>link.os_username).join(', ');
      field.value=row?draft(row,'node:'+node,aliases):'';
      field.readOnly=existing.length>1;
      label.append(field);
      if(field.readOnly)label.append(el('small','现有的多个映射会保留。','muted'));
      else mappingInputs.set(node,field);
      fields.append(label);
    }
    editorError('');$('member-dialog').showModal();
    if(!row)$('manage-id').focus();
  }
  function readMemberEditor() {
    const row=memberEditor.row,id=$('manage-id').value.trim();
    const name=$('manage-name').value.trim(),email=$('manage-email').value.trim();
    const entered=$('manage-username').value.trim(), login=entered?username(entered):'';
    if(!name)throw new Error('请输入显示名。');
    if(!row && state.rows.has(id))throw new Error('此成员已在名单中，请通过该行的「管理」或「登记」进入。');
    if(row?.login_username && row.user && !login)throw new Error('请保留当前登录用户名，或输入新的登录用户名。');
    const edits={};
    if(!row || name!==row.profile.display_name)edits.display_name=name;
    if(!row || email!==(row.profile.email || ''))edits.email=email;
    if(!row || login!==(row.login_username || ''))edits.login_username=login;
    for(const [node,input] of memberEditor.mappingInputs) {
      const value=input.value.trim();
      if(/\s/.test(value))throw new Error('Linux 用户名不能包含空格。');
      if(value!==(mappingsFor(row,node)[0]?.os_username || ''))edits['node:'+node]=value;
    }
    if(row?.user && $('manage-role').value!==row.role)edits.role=$('manage-role').value;
    const active=$('manage-active').value==='active';
    if(row?.user && active!==(row.status==='active'))edits.active=active;
    return {id,edits,row:row || {
      id,user:null,name,profile:{display_name:name,timezone:state.session.actor.timezone,email:email || null},
      status:'unregistered',eligible:true,mappings:[],role:'member',bindings:[],
      login_username:login || null,password_ready:false,revision:state.revision,manual:true,
    }};
  }
  $('add-member').addEventListener('click',()=>openMemberEditor());
  for(const id of ['close-member','cancel-member'])$(id).addEventListener('click',()=>$('member-dialog').close());
  $('member-dialog').addEventListener('close',()=>{
    memberEditor=null;$('manage-mappings').replaceChildren();
  });
  $('member-editor').addEventListener('submit',event=>{
    event.preventDefault();
    if(!memberEditor || state.busy)return;
    const stage=event.submitter?.value==='stage';
    try {
      const {id,edits,row}=readMemberEditor();
      const selected=new Set([id]);
      if(!Object.keys(edits).length && row.user && !state.edits.has(id))throw new Error('没有需要保存的变更。');
      const parameters=stage?null:changes(selected,{row,edits});
      state.rows.set(id,row);
      if(row.manual && state.source==='registered' && !state.page.includes(id))state.page.push(id);
      if(Object.keys(edits).length)state.edits.set(id,edits);else state.edits.delete(id);
      if(row.user && !Object.keys(edits).length)state.selected.delete(id);else state.selected.add(id);
      $('member-dialog').close();render();focusMember(id);
      if(parameters)reviewMemberChanges(selected,parameters);
    } catch(error) {
      editorError(error.message);
    }
  });
  function changes(selected,prepared=null) {
    const result=[];
    for(const id of selected) {
      const row=prepared?.row || state.rows.get(id);if(!row?.eligible)throw new Error('Only verified active human members can be registered.');
      const values=prepared?.edits || state.edits.get(id) || {};
      const chosen=(key,fallback)=>values[key] ?? fallback;
      if(!row.manual && row.revision!==state.revision)throw new Error('Load the current page for '+id+' before review. Your edits are preserved.');
      const profile={display_name:chosen('display_name',row.profile.display_name).trim(),timezone:row.profile.timezone,email:chosen('email',row.profile.email || '').trim() || null};
      if(!profile.display_name)throw new Error('Enter a display name for '+id+'.');
      const loginValue=chosen('login_username',row.login_username || '').trim();
      if(row.user && row.login_username && !loginValue)throw new Error('Keep the existing login username or enter a new one for '+id+'.');
      const login=loginValue?username(loginValue):null;
      const mappings=[];
      for(const node of state.session.nodes) {
        const links=mappingsFor(row,node);if(links.length>1)continue;
        const value=chosen('node:'+node,links[0]?.os_username || '').trim();if(value && /\s/.test(value))throw new Error('Linux usernames cannot contain whitespace.');
        if(!row.user){if(value)mappings.push({node_id:node,os_username:value});}
        else {
          if(links[0] && links[0].os_username!==value)result.push({type:'remove_node_user',node_id:node,os_username:links[0].os_username,expected_user_id:row.user});
          if(value && (!links[0] || links[0].os_username!==value || links[0].confidence!=='verified' || links[0].source!=='manual'))result.push({type:'assign_node_user',node_id:node,os_username:value,user_id:row.user,expected_user_id:links[0]?.os_username===value?row.user:null});
        }
      }
      if(!row.user)result.push({type:'register_identity',subject:{provider:'slack',namespace:state.session.workspace_namespace,external_id:id},profile,mappings,...(login?{login_username:login}:{})});
      else {
        const role=chosen('role',row.role),active=chosen('active',row.status==='active'),change={type:'edit_member',user_id:row.user};
        if(profile.display_name!==row.profile.display_name || profile.email!==(row.profile.email || null))change.profile=profile;
        if(active!==(row.status==='active'))change.active=active;
        if(login!==(row.login_username || null))change.login_username=login;
        if(role!==row.role || values.role!==undefined || !row.bindings.some(binding=>binding.role_name===role && binding.scope.type==='global' && binding.scope.id==='*')){change.role=role;change.previous_bindings=row.bindings;}
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
  function reviewMemberChanges(selected,values) {
    begin('identity.manage_members',{expected_revision:state.revision,reason:AUDIT_REASON,changes:values},selected);
  }
  $('review-selected').addEventListener('click',()=>{try{reviewMemberChanges(state.selected,changes(state.selected));}catch(error){notice(error.message,true);}});
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
    configureWorkspace(state.session.actor);
    $('profile-name').value=state.session.actor.display_name;$('profile-email').value=state.session.actor.email || '';
    accountFields();
    if(state.session.actor.role==='admin') {
      if(!await load(state.source))throw new Error('Changes saved, but the member list could not refresh. Retrying automatically.');
    } else if(!$('members-panel').hidden) {tab('profile');}
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
          try {await applySavedChanges(intent);reviewView('terminal','Changes saved.');}
          catch(error){reviewView('terminal',error.message || 'Changes saved. Retrying the page update automatically.',true,true);}
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
  $('review-dialog').addEventListener('close',()=>{
    if(state.intent?.terminal && !state.intent.needsRefresh){
      clearTimeout(reviewTimer);reviewTimer=null;state.intent=null;$('review-content').replaceChildren();
    }
    controls();
  });

  function setOverviewRange(preset) {
    overviewPreset=preset;
    const custom=preset==='custom';
    $('overview-custom').hidden=!custom;
    for(const id of ['general-start','general-end'])$(id).disabled=!custom;
    for(const button of document.querySelectorAll('#general-range button')) {
      button.setAttribute('aria-pressed',String(button.dataset.range===preset));
    }
    if(!custom) {
      const [start,end]=window.arisCalendarRange(state.session.actor.timezone,preset);
      $('general-start').value=start;
      $('general-end').value=end;
    }
  }
  function overviewMessage(value,error=false) {
    const message=$('general-message');
    message.textContent=value;
    message.hidden=!value;
    message.classList.toggle('error',error);
  }
  const number=new Intl.NumberFormat('zh-CN',{maximumFractionDigits:1});
  const percent=value=>value===null?'—':number.format(value)+'%';
  const memory=(used,total,units)=>used===null || total===null?'—':number.format(used/units)+' / '+number.format(total/units)+' GiB';
  const gpuHours=value=>new Intl.NumberFormat('zh-CN',{maximumFractionDigits:1}).format(value/3600000000);
  const timezoneLabel=zone=>zone==='America/Vancouver'?'温哥华时间':zone;
  const shortDate=value=>value.slice(5).replace('-','/');
  const svgNs='http://www.w3.org/2000/svg';
  const iconPaths={
    server:'M5 4h14a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Zm0 9h14a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2ZM7 7.5h.01M7 16.5h.01m5-9h5m-5 9h5',
    cpu:'M8 6h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2Zm2 4h4v4h-4ZM9 3v3m6-3v3M9 18v3m6-3v3M3 9h3m-3 6h3m12-6h3m-3 6h3',
    activity:'M2 12h4l3-7 4 14 3-7h6',
    clock:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 4v5l3 2',
    search:'M17 10.5a6.5 6.5 0 1 1-13 0 6.5 6.5 0 0 1 13 0Zm-2 4.5 5 5',
  };
  function svgElement(tag,attrs,value) {
    const shape=document.createElementNS(svgNs,tag);
    for(const [key,attr] of Object.entries(attrs))shape.setAttribute(key,String(attr));
    if(value!==undefined)shape.textContent=value;
    return shape;
  }
  function overviewIcon(name) {
    const icon=svgElement('svg',{viewBox:'0 0 24 24',class:'aris-icon','aria-hidden':true});
    icon.append(svgElement('path',{d:iconPaths[name]}));
    return icon;
  }
  const gpuStates={idle:['观测空闲','available'],busy:['使用中','active'],unknown:['未知','offline']};
  const nodeStates={ONLINE:['在线','available'],STALE:['状态陈旧','reserved'],OFFLINE:['离线','offline']};
  const telemetryStates={CURRENT:'当前',STALE:'陈旧',MISSING:'无采样',ERROR:'采集异常'};
  function stamp(value) {
    if(!value)return '—';
    return new Intl.DateTimeFormat('zh-CN',{
      timeZone:state.session.actor.timezone,dateStyle:'short',timeStyle:'medium',
    }).format(new Date(value));
  }
  function metric(label,value,note) {
    const item=el('div',undefined,'overview-metric');
    item.append(el('span',label),el('strong',value));
    if(note)item.append(el('small',note));
    return item;
  }
  function stat(label,value,note,icon,unit='') {
    const item=metric(label,value,note);
    item.classList.add('overview-stat');
    const symbol=el('span',undefined,'overview-stat-icon '+icon);
    symbol.append(overviewIcon(icon));item.append(symbol);
    if(unit)item.querySelector('strong').append(el('span',unit,'overview-unit'));
    return item;
  }
  function badge([label,tone]) {
    return el('span',label,'status-badge '+tone);
  }
  function drawGpu(hardware,gpu) {
    $('gpu-title').textContent=hardware.node_id+' · GPU '+gpu.index;
    const values=el('div',undefined,'overview-metrics gpu-detail-metrics');
    values.append(
      metric('利用率',percent(gpu.utilization_percent),''),
      metric('已用显存',memory(gpu.memory_used_mib,gpu.memory_total_mib,1024),''),
      metric('温度',gpu.temperature_c===null?'—':number.format(gpu.temperature_c)+' °C',''),
    );
    const detail=$('gpu-detail');
    const summary=el('p',gpu.name,'gpu-detail-name');
    summary.append(badge(gpuStates[gpu.state]));
    detail.replaceChildren(summary,values,el('p','GPU 采样 '+stamp(hardware.observed_at),'muted small'));
    if(archiveNodes.includes(hardware.node_id)) {
      const link=el('a','查看此节点的归档观测','archive-link');
      link.href='/internal/usage/ui#activity?node='+encodeURIComponent(hardware.node_id);
      detail.append(link);
    }
  }
  function gpuRow(hardware,gpu) {
    const row=el('button',undefined,'gpu-row');
    row.type='button';
    row.dataset.gpuIndex=String(gpu.index);
    const identity=el('span',undefined,'gpu-identity');
    identity.append(el('strong','GPU '+gpu.index),badge(gpuStates[gpu.state]));
    const memoryCell=el('span',memory(gpu.memory_used_mib,gpu.memory_total_mib,1024),'gpu-memory');
    if(gpu.memory_used_mib!==null && gpu.memory_total_mib>0) {
      const load=gpu.memory_used_mib/gpu.memory_total_mib;
      const meter=el('meter');
      meter.value=load;
      meter.dataset.load=load>=.85?'high':load>=.55?'medium':'low';
      meter.setAttribute('aria-label','GPU '+gpu.index+' 显存使用比例');
      memoryCell.append(meter);
    }
    const next=el('span',undefined,'gpu-next');
    next.append(overviewIcon('search'));
    row.append(identity,el('span',percent(gpu.utilization_percent)),memoryCell,next);
    row.addEventListener('click',()=>{
      openGpu={node:hardware.node_id,index:gpu.index};
      drawGpu(hardware,gpu);
      $('gpu-dialog').showModal();
    });
    return row;
  }
  function nodeCard(hardware,live) {
    const node=el('section',undefined,'panel node-panel');
    node.dataset.nodeId=hardware.node_id;
    const heading=el('header',undefined,'node-heading');
    const identity=el('div',undefined,'node-identity'), name=el('div');
    const symbol=el('span',undefined,'node-symbol');
    symbol.append(overviewIcon('server'));
    const models=Array.from(new Set(hardware.gpus.map(gpu=>gpu.name))).join(' / ');
    name.append(el('h2',hardware.node_id,'node-name'),el('p',models || telemetryStates[hardware.telemetry_status],'muted small'));
    identity.append(symbol,name);
    const idle=hardware.gpus.filter(gpu=>gpu.state==='idle').length;
    const status=hardware.status==='ONLINE' && hardware.telemetry_status==='CURRENT' && idle
      ? [idle+' 张观测空闲','available'] : nodeStates[hardware.status] || ['状态未知','offline'];
    heading.append(identity,badge(status));
    node.append(heading);
    if(hardware.gpus.length) {
      const columns=el('div',undefined,'gpu-columns');
      ['设备状态','利用率','显存',''].forEach(label=>columns.append(el('span',label)));
      columns.setAttribute('aria-hidden','true');
      node.append(columns,...hardware.gpus.map(gpu=>gpuRow(hardware,gpu)));
    } else {
      node.append(el('p','该节点暂无 GPU 观测。','gpu-empty'));
    }
    const system=el('details',undefined,'vega-system');
    const line=el('summary');
    line.append(el('span','Vega 系统资源'),el('span',`CPU ${percent(hardware.cpu_percent)} · 内存 ${memory(
      hardware.memory_available_bytes===null?null:hardware.memory_total_bytes-hardware.memory_available_bytes,
      hardware.memory_total_bytes,2**30,
    )}`,'system-inline'));
    system.append(line);
    const systemValues=el('div',undefined,'overview-metrics system-metrics');
    const available=hardware.memory_available_bytes;
    const used=available===null?null:hardware.memory_total_bytes-available;
    systemValues.append(
      metric('CPU 利用率',percent(hardware.cpu_percent),telemetryStates[hardware.system_freshness]),
      metric('系统内存',memory(used,hardware.memory_total_bytes,2**30),available===null?'可用量未知':'可用 '+number.format(available/2**30)+' GiB（内核估计）'),
      metric('系统采样',stamp(hardware.system_observed_at),telemetryStates[hardware.system_freshness]),
      metric(live?'归档心跳':'最近心跳',stamp(hardware.heartbeat_at),'Vega 节点'),
    );
    system.append(systemValues);
    node.append(system);
    return node;
  }
  function renderHardware(results,live=false) {
    const signature=JSON.stringify([live,...results.map(result=>result.hardware)]);
    if(signature===renderedHardware)return;
    renderedHardware=signature;
    const holder=$('general-data');
    const expanded=new Set([...holder.querySelectorAll('.node-panel')].filter(
      node=>node.querySelector('details').open,
    ).map(node=>node.dataset.nodeId));
    const active=document.activeElement;
    const focused=holder.contains(active)?active.closest('.node-panel'):null;
    const gpus=results.flatMap(result=>result.hardware.gpus);
    const known=gpus.some(gpu=>gpu.state!=='unknown');
    const count=state=>known?String(gpus.filter(gpu=>gpu.state===state).length):'—';
    const partial=results.some(({hardware})=>hardware.telemetry_status!=='CURRENT') || gpus.some(gpu=>gpu.state==='unknown');
    const stateNote=partial?'仅含已观测设备':'设备忙闲观测';
    const recorded=results.reduce((total,result)=>total+result.recorded_usage.recorded_microseconds,0);
    $('overview-stats').replaceChildren(
      stat('授权节点',String(results.length),'可访问 '+results.map(({hardware})=>hardware.node_id).join(' / '),'server'),
      stat('观测空闲 GPU',count('idle'),gpus.length?gpus.length+' 张设备 · '+stateNote:'暂无 GPU 采样','cpu'),
      stat('使用中',count('busy'),stateNote,'activity'),
      stat('已记录用量',gpuHours(recorded),'所选节点 · 匿名合计','clock','GPU h'),
    );
    $('welcome-note').textContent=(known?'所选节点观测到 '+count('idle')+' 张 GPU 空闲。':'所选节点暂无可判断忙闲的 GPU 采样。')+'点击设备查看显存和利用率。';
    const timeZone=state.session.actor.timezone;
    const sampled=results.map(({hardware})=>hardware.telemetry_status==='CURRENT' && hardware.observed_at).filter(Boolean).map(Date.parse);
    const latest=sampled.length?new Intl.DateTimeFormat('zh-CN',{timeZone,timeStyle:'medium'}).format(Math.max(...sampled)):null;
    $('overview-sample').textContent=(latest?(live?'实时采样于 ':'快照采样于 ')+latest:'暂无当前 GPU 采样')+' · '+timezoneLabel(timeZone);
    const grid=el('div',undefined,results.length>1?'node-grid is-multi':'node-grid');
    const cards=results.map(({hardware})=>{
      const card=nodeCard(hardware,live);
      card.querySelector('details').open=expanded.has(hardware.node_id);
      return card;
    });
    grid.append(...cards);
    holder.replaceChildren(grid);
    const replacement=cards.find(card=>card.dataset.nodeId===focused?.dataset.nodeId);
    if(replacement) {
      const target=active.matches('summary')?replacement.querySelector('summary')
        : [...replacement.querySelectorAll('.gpu-row')].find(row=>row.dataset.gpuIndex===active.dataset.gpuIndex);
      target?.focus({preventScroll:true});
    }
    if($('gpu-dialog').open && openGpu) {
      const item=results.find(({hardware})=>hardware.node_id===openGpu.node)?.hardware;
      const gpu=item?.gpus.find(gpu=>gpu.index===openGpu.index);
      if(gpu && !$('gpu-detail').contains(active))drawGpu(item,gpu);
    }
  }
  function renderOverview(results) {
    overviewSnapshot=results;
    renderedHardware='';
    renderHardware(results);
    renderOverviewTrend(results);
  }
  function renderOverviewTrend(results) {
    const days=new Map();
    for(const {recorded_usage} of results) {
      for(const {date,recorded_microseconds} of recorded_usage.days) {
        days.set(date,(days.get(date) || 0)+recorded_microseconds);
      }
    }
    const series=[...days].sort(([left],[right])=>left.localeCompare(right));
    const maximum=Math.max(0,...days.values())/3600000000;
    if(!maximum) {
      $('overview-trend').replaceChildren(el('p','所选范围暂无已记录用量。','overview-report-empty muted'));
      return;
    }
    const graph=svgElement('svg',{viewBox:'0 0 600 134',role:'img','aria-label':'按日期统计的匿名 GPU 小时','class':'overview-chart'});
    const left=44,right=591,top=8,bottom=110;
    const magnitude=10**Math.floor(Math.log10(maximum/4));
    const step=magnitude*[1,2,2.5,5,10].find(unit=>unit*magnitude>=maximum/4);
    const ceiling=4*step;
    for(let index=0;index<=4;index++) {
      const y=bottom-(bottom-top)*index/4;
      graph.append(svgElement('line',{x1:left,x2:right,y1:y,y2:y,class:'overview-chart-grid'}));
      graph.append(svgElement('text',{x:left-6,y:y+3,'text-anchor':'end'},Number((index*step).toPrecision(3))+(index===4?'h':'')));
    }
    const slot=(right-left)/series.length,stride=Math.ceil(series.length/7);
    for(const [index,[date,value]] of series.entries()) {
      const height=(bottom-top)*value/3600000000/ceiling;
      const x=left+(index+.5)*slot;
      const bar=svgElement('rect',{
        x:x-slot*.31,y:bottom-height,width:slot*.62,height,rx:Math.min(3,slot*.1),
        class:index===series.length-1?'overview-chart-last':'overview-chart-bar',
      });
      bar.append(svgElement('title',{},date+' · '+gpuHours(value)+' GPU h'));
      graph.append(bar);
      if(index%stride===0)graph.append(svgElement('text',{x,y:129,'text-anchor':'middle'},shortDate(date)));
    }
    const values=el('details',undefined,'overview-chart-values'),list=el('dl');
    for(const [date,value] of series)list.append(el('dt',date),el('dd',gpuHours(value)+' GPU h'));
    values.append(el('summary','查看每日数值'),list);
    $('overview-trend').replaceChildren(graph,values);
  }
  function snapshotState(content,label,tone) {
    $('overview-plan').replaceChildren(...content);
    const status=$('overview-plan-status');
    status.hidden=!label;status.textContent=label || '';status.className='status-badge '+(tone || '');
  }
  async function loadSnapshotPlan(nodes,windowRange,version) {
    const access=await usageAccess;
    if(version!==overviewVersion)return;
    const link=$('overview-plan-link');
    link.hidden=!access?.saved_snapshots_available;
    if(!access?.coverage_plans_available) {
      snapshotState([el('p',access?'当前门户未配置自动周快照计划。':'暂时无法读取快照计划。','muted small')],access?'未配置':'状态未知','offline');
      return;
    }
    const eligible=access.nodes.filter(node=>nodes.includes(node.node_id));
    const responses=await Promise.allSettled(eligible.map(node=>request('/internal/usage/snapshots/query',{
      node_id:node.node_id,mode:node.node?'node':'mine',window:windowRange,limit:1,include_plans:true,
    })));
    if(version!==overviewVersion)return;
    const plans=responses.flatMap(response=>response.status==='fulfilled'?response.value.plans.items:[]);
    const partial=responses.some(response=>response.status==='rejected');
    const next=plans.sort((left,right)=>Date.parse(left.next_due_at)-Date.parse(right.next_due_at))[0];
    if(!next) {
      snapshotState([el('p',partial?'暂时无法读取快照计划。':'所选节点暂无可查看的自动周快照。','muted small')],partial?'状态未知':'暂无计划','offline');
      return;
    }
    const states={scheduled:['未到期','reserved'],pending:['待生成','reserved'],delayed:['生成延迟','danger']};
    const scheduled=new Intl.DateTimeFormat('zh-CN',{
      timeZone:next.timezone,month:'long',day:'numeric',hour:'2-digit',minute:'2-digit',hourCycle:'h23',
    }).format(new Date(next.next_due_at));
    const when=el('p',scheduled,'overview-plan-when');
    when.append(el('span',timezoneLabel(next.timezone)));
    const detail=next.last_result?'最近已保存 '+next.last_result.week+' 当周快照。':'首份自动周快照待生成。';
    const note=partial?' · 部分计划无法读取':'';
    snapshotState([el('p','下一次计划运行','small'),when,el('p',next.node_id+' · '+detail+note,'overview-plan-note muted small')],...states[next.state]);
  }
  function previewStatus(message) {
    const status=$('overview-live-status');
    status.hidden=!message;
    if(status.textContent!==message)status.textContent=message;
  }
  function liveHardware(nodes) {
    const byNode=new Map(nodes.map(node=>[node.node_id,node]));
    renderHardware(overviewSnapshot.map(result=>{
      const saved=result.hardware,frame=byNode.get(saved.node_id);
      const previous=livePreview.inventory.get(saved.node_id) || saved.gpus;
      if(frame?.telemetry_status==='CURRENT')livePreview.inventory.set(saved.node_id,frame.gpus);
      return {
        ...result,
        hardware:{
          node_id:saved.node_id,
          status:frame?.status || 'UNKNOWN',
          telemetry_status:frame?.telemetry_status || 'MISSING',
          observed_at:frame?.observed_at || null,
          system_freshness:frame?.system_freshness || 'MISSING',
          system_observed_at:frame?.system_observed_at || null,
          cpu_percent:frame?.cpu_percent ?? null,
          memory_total_bytes:frame?.memory_total_bytes ?? null,
          memory_available_bytes:frame?.memory_available_bytes ?? null,
          heartbeat_at:saved.heartbeat_at,
          gpus:frame?.telemetry_status==='CURRENT'?frame.gpus:previous.map(gpu=>({
            ...gpu,state:'unknown',memory_total_mib:null,memory_used_mib:null,
            temperature_c:null,utilization_percent:null,
          })),
        },
      };
    }),true);
  }
  function stopLivePreview(restore=true) {
    const preview=livePreview;
    if(!preview)return;
    livePreview=null;
    preview.request?.abort();clearTimeout(preview.retry);
    const toggle=$('overview-live-toggle');
    toggle.setAttribute('aria-pressed','false');toggle.textContent='开启实时预览';
    previewStatus('');
    if(restore)renderHardware(overviewSnapshot);
  }
  async function readLivePreview(preview) {
    const controller=new AbortController();
    preview.request=controller;
    let watchdog, denied=false;
    const renew=()=>{
      clearTimeout(watchdog);
      watchdog=setTimeout(()=>controller.abort(),5000);
    };
    try {
      renew();
      const response=await fetch('/internal/members/general',{
        method:'POST',credentials:'same-origin',cache:'no-store',redirect:'error',signal:controller.signal,
        headers:{Accept:'application/x-ndjson','Content-Type':'application/json','X-CSRF-Token':state.csrf,'X-Request-ID':crypto.randomUUID()},
        body:JSON.stringify({node_ids:preview.nodeIds}),
      });
      denied=[401,403].includes(response.status);
      if(!response.ok || !response.body || !response.headers.get('content-type')?.startsWith('application/x-ndjson'))return;
      const reader=response.body.getReader(),decoder=new TextDecoder();
      let buffer='';
      try {
        while(livePreview===preview) {
          const next=await reader.read();
          if(next.done || livePreview!==preview)break;
          buffer+=decoder.decode(next.value,{stream:true});
          const lines=buffer.split('\n');
          buffer=lines.pop();
          if(buffer.length>1048576)throw new Error('Preview frame exceeds the size limit');
          for(const line of lines) {
            if(!line)continue;
            if(line.length>1048576)throw new Error('Preview frame exceeds the size limit');
            const frame=JSON.parse(line);
            if(frame.error) {
              denied=frame.error==='PERMISSION_DENIED';
              return;
            }
            renew();
            liveHardware(frame.nodes);
            const fresh=frame.nodes.filter(node=>node.telemetry_status==='CURRENT').length;
            previewStatus(fresh
              ? `实时预览 · ${fresh} / ${preview.nodeIds.length} 个节点有当前 GPU 采样 · 未归档`
              : '实时预览 · 等待 Vega 采样');
          }
        }
      } finally {
        reader.releaseLock();
      }
    } catch {
      // A disconnect or stale stream falls back to unknown until the next preview frame.
    } finally {
      clearTimeout(watchdog);controller.abort();
      if(livePreview===preview) {
        liveHardware([]);
        if(denied) {
          stopLivePreview(false);$('overview-live-toggle').disabled=true;
          previewStatus('实时预览已结束，请刷新页面确认当前权限。');
        } else {
          previewStatus('实时连接暂不可用，正在重连。');
          preview.retry=setTimeout(()=>readLivePreview(preview),3000);
        }
      }
    }
  }
  $('overview-live-toggle').addEventListener('click',()=>{
    if(livePreview) {stopLivePreview();return;}
    if(!state.session?.live_preview_available || !overviewSnapshot.length || overviewBusy)return;
    const toggle=$('overview-live-toggle');
    livePreview={nodeIds:overviewSnapshot.map(result=>result.hardware.node_id),inventory:new Map()};
    toggle.setAttribute('aria-pressed','true');toggle.textContent='关闭实时预览';
    previewStatus('正在连接 Vega 实时预览…');
    liveHardware([]);
    readLivePreview(livePreview);
  });
  document.addEventListener('visibilitychange',()=>{if(document.hidden)stopLivePreview();});
  async function refreshOverview() {
    if(overviewBusy)return;
    const node=$('general-node').value,start=$('general-start'),end=$('general-end');
    const nodes=node?[node]:state.session.nodes;
    if(!nodes.length){overviewMessage('没有可查询的节点。');return;}
    if(overviewPreset==='custom' && (!start.reportValidity() || !end.reportValidity()))return;
    if(start.value>end.value){overviewMessage('开始日期需早于或等于结束日期。',true);return;}
    stopLivePreview();
    previewStatus('');
    const startDate=start.value,endDate=end.value,epoch=state.epoch,version=++overviewVersion;
    overviewBusy=true;
    const controls=[$('refresh-general'),$('general-node'),...document.querySelectorAll('#general-range button, #overview-custom button')];
    controls.forEach(control=>control.disabled=true);
    $('overview-live-toggle').disabled=true;
    const sameYear=startDate.slice(0,4)===endDate.slice(0,4);
    $('general-range-caption').textContent='· '+(sameYear?shortDate(startDate)+'–'+shortDate(endDate):startDate+' 至 '+endDate);
    $('general-data').setAttribute('aria-busy','true');
    overviewMessage('正在读取节点观测…');
    $('overview-trend').replaceChildren(el('p','正在读取已记录用量…','muted small'));
    snapshotState([el('p','正在读取计划…','muted small')]);
    try {
      const windowRange={kind:'calendar',start_date:startDate,end_date:endDate,timezone:state.session.actor.timezone};
      const settled=await Promise.allSettled(nodes.map(node_id=>request(
        '/internal/members/general',{node_id,window:windowRange,include_days:true},
      )));
      if(epoch!==state.epoch)return;
      const results=settled.filter(result=>result.status==='fulfilled').map(result=>result.value);
      if(!results.length)throw settled[0].reason;
      renderOverview(results);
      const missing=nodes.length-results.length;
      overviewMessage(missing?missing+' 个节点未能读取。汇总仅含成功返回的节点。':'',Boolean(missing));
      loadSnapshotPlan(results.map(result=>result.hardware.node_id),windowRange,version).catch(()=>{
        if(version===overviewVersion)snapshotState([el('p','暂时无法读取快照计划。','muted small')],'状态未知','offline');
      });
    } catch(error) {
      if(epoch===state.epoch){
        overviewSnapshot=[];renderedHardware='';
        $('overview-stats').replaceChildren();$('general-data').replaceChildren();$('overview-sample').textContent='';
        $('welcome-note').textContent='节点状态暂不可用。';
        $('overview-trend').replaceChildren(el('p','暂时无法读取已记录用量。','overview-report-empty muted'));
        snapshotState([el('p','暂时无法读取快照计划。','muted small')],'状态未知','offline');
        overviewMessage(error.message,true);
      }
    } finally {
      if(epoch===state.epoch) {
        overviewBusy=false;controls.forEach(control=>control.disabled=false);
        $('overview-live-toggle').disabled=!overviewSnapshot.length || !state.session.live_preview_available;
        $('general-data').setAttribute('aria-busy','false');
      }
    }
  }
  $('general-range').addEventListener('click',event=>{
    const preset=event.target.closest('button[data-range]')?.dataset.range;
    if(!preset || overviewBusy)return;
    setOverviewRange(preset);
    if(preset!=='custom')refreshOverview();
  });
  $('overview-filters').addEventListener('submit',event=>{event.preventDefault();refreshOverview();});
  $('general-node').addEventListener('change',refreshOverview);
  $('refresh-general').addEventListener('click',()=>{setOverviewRange(overviewPreset);refreshOverview();});
  $('close-gpu').addEventListener('click',()=>$('gpu-dialog').close());
  $('gpu-dialog').addEventListener('close',()=>{openGpu=null;});
  window.addEventListener('pagehide',clear);
  window.addEventListener('pageshow',event=>{if(event.persisted)connect().catch(error=>{if(error.status!==401)notice(error.message,true);});});
  connect().catch(error=>{if(error.status!==401)notice(error.message,true);});
})();
