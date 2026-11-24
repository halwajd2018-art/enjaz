'use strict';
const root=document.getElementById('root');
const state={me:null,csrf:null,page:'dashboard',users:[],departments:[],tasks:[],dashboard:{},presence:null,activity:[],channel:'general',messages:[],selectedTask:null,actFilter:{},chatTimer:null,heartbeatTimer:null,presenceTimer:null,mailTimer:null,mailUnread:0,mailFolder:'inbox',mailSelected:null,mailThread:null,mailThreads:[],mailContacts:[],noticeTimer:null,discussionTimer:null,notices:null,noticeCount:0,chatUnread:0,noticeInitialized:false,taskRequestId:null,lastInteraction:Date.now(),loginAt:Date.now()};
const $=id=>document.getElementById(id);
const safe=s=>String(s===null||s===undefined?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=dt=>dt?new Date(dt).toLocaleString('ar-SA',{dateStyle:'short',timeStyle:'short',timeZone:'Asia/Riyadh'}):'—';
const fdate=dt=>dt?new Date(dt+'T12:00:00').toLocaleDateString('ar-SA'):'—';
const roles={director:'مدير عام الإدارة',advisor:'مستشار القطاع',head:'رئيس قسم',employee:'موظف'};
const isExecutive=role=>role==='director'||role==='advisor';
const affiliationLabel=u=>u.role==='advisor'?'وظيفة مستقلة على مستوى القطاع':(u.department||'الإدارة العامة');
const statuses={new:'لم تبدأ',working:'قيد التنفيذ',review:'قيد المراجعة',revision:'بحاجة إلى تعديل',done:'معتمدة ومكتملة'};
const priorities={high:'عالية',medium:'متوسطة',low:'منخفضة'};
let toastTimer;
function toast(message){const el=$('notify');el.textContent=message;el.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('show'),3600)}
async function api(path,options={}){
  let {method='GET',data,...rest}=options;
  let headers={...(rest.headers||{})};if(data!==undefined)headers['Content-Type']='application/json';
  if(!['GET','HEAD'].includes(method)&&state.csrf)headers['X-CSRF-Token']=state.csrf;
  let res;
  try{res=await fetch('/api'+path,{...rest,method,headers,credentials:'same-origin',body:data===undefined?undefined:JSON.stringify(data)})}
  catch{throw Error('تعذّر الاتصال بالخادم. تأكد من اتصال الإنترنت')}
  const content=await res.json().catch(()=>({}));
  if(res.status===401){state.me=null;stopBackground();renderLogin();throw Error('انتهت الجلسة. يرجى تسجيل الدخول')}
  if(!res.ok){let text=typeof content.detail==='string'?content.detail:'تعذّر إتمام العملية';throw Error(text)}
  return content;
}
const attachmentTypes='.pdf,.docx,.xlsx,.pptx,.png,.jpg,.jpeg,.txt,.csv';
const maxAttachSize=8*1024*1024;
async function uploadTaskFile(taskId,file,category){
  if(file.size>maxAttachSize)throw Error('حجم '+file.name+' أكبر من 8 ميجابايت');
  const form=new FormData();form.append('file',file,file.name);form.append('category',category);
  let response;
  try{response=await fetch(`/api/tasks/${taskId}/attachments`,{method:'POST',headers:{'X-CSRF-Token':state.csrf||''},credentials:'same-origin',body:form})}
  catch{throw Error('تعذّر الاتصال بالخادم أثناء رفع المرفق')}
  const content=await response.json().catch(()=>({}));
  if(response.status===401){state.me=null;stopBackground();renderLogin();throw Error('انتهت جلسة الدخول')}
  if(!response.ok)throw Error(content.detail||'فشل رفع '+file.name);
  return content;
}
async function uploadTaskFiles(taskId,files,category){
  if(files.length>6)throw Error('يمكن رفع 6 ملفات في الدفعة الواحدة');
  let completed=0;
  for(const file of files){
    try{await uploadTaskFile(taskId,file,category);completed++}
    catch(e){throw Error(`تم رفع ${completed} من ${files.length} ملف. ${e.message}`)}
  }
  return completed;
}
function fileSize(n){return n<1024*1024?(n/1024).toFixed(0)+' ك.ب':(n/1024/1024).toFixed(1)+' م.ب'}
async function populateAttachments(taskId){
  const target=$('taskAttachments');if(!target)return;
  try{
    const items=await api(`/tasks/${taskId}/attachments`);
    const active=$('taskAttachments');if(!active)return;
    active.innerHTML=items.length?items.map(a=>`<div class="attach-item"><div><b>${safe(a.filename)}</b><small>${a.category==='instruction'?'ملف توجيهات':'مرفق إنجاز'} · ${safe(a.uploader)} · ${fmt(a.uploaded_at)} · ${fileSize(a.size_bytes)}</small></div><a class="btn secondary small" href="/api/tasks/${taskId}/attachments/${a.id}/download" download>تنزيل</a></div>`).join(''):'<p class="hint">لا توجد مرفقات لهذه المهمة بعد.</p>';
  }catch(e){const el=$('taskAttachments');if(el)el.innerHTML=`<p class="error">${safe(e.message)}</p>`}
}
function request(fn){return async(...args)=>{try{await fn(...args)}catch(e){toast(e.message||'حدث خطأ')}}}
async function init(){try{const data=await api('/me');state.me=data.user;state.csrf=data.csrf;await loadAll();startBackground();render()}catch(e){renderLogin()}}
async function loadAll(){const [users,departments,tasks,dashboard,presence,mailCount,notices]=await Promise.all([api('/users'),api('/departments'),api('/tasks'),api('/dashboard'),api('/presence'),api('/inbox/unread'),api('/notifications')]);Object.assign(state,{users,departments,tasks,dashboard,presence,mailUnread:mailCount.unread_threads,notices,noticeCount:notices.total_unread,chatUnread:notices.chat_unread});}
const noticeKinds={assigned:'تكليف جديد',review:'مهمة مكتملة تنتظر المراجعة',completed:'تم اعتماد المهمة واكتمالها',revision:'أُعيدت المهمة للتعديل',cancelled:'تم إلغاء التكليف'};
function syncNoticeBadges(){
 const bell=$('notificationCount');if(bell)bell.textContent=state.noticeCount||'';
 document.querySelectorAll('[data-page="chat"]').forEach(btn=>{btn.querySelector('[data-chat-badge]')?.remove();if(state.chatUnread){const span=document.createElement('b');span.className='mail-badge';span.dataset.chatBadge='';span.textContent=state.chatUnread;btn.appendChild(span)}});
 updateMailBadge();document.title=(state.noticeCount?'('+state.noticeCount+') ':'')+'إنجاز';
}
async function pollNotifications(quiet=false){
 if(!state.me)return;const data=await api('/notifications');
 const old=state.noticeCount;
 Object.assign(state,{notices:data,noticeCount:data.total_unread,chatUnread:data.chat_unread,mailUnread:data.mail_unread});
 syncNoticeBadges();
 if(!quiet&&state.noticeInitialized&&data.total_unread>old)toast('لديك إشعارات جديدة: رسالة أو محادثة أو تحديث مهمة');
 state.noticeInitialized=true;
}
function notificationCenterHtml(data){
 const ev=data.events||[];
 const chat=Object.entries(data.chat_channels||{}).filter(([channel,count])=>count>0);
 return `<div class="notice-summary"><span>✉ البريد: <b>${data.mail_unread}</b></span><span>▤ المحادثات: <b>${data.chat_unread}</b></span><span>☑ التكليفات: <b>${data.task_unread}</b></span></div><div class="notice-actions"><button class="btn secondary small" id="noticeInbox">فتح البريد الوارد</button><button class="btn secondary small" id="noticeChats">مركز المحادثات</button><button class="btn outline small" id="markAllNotices">تحديد إشعارات المهام كمقروءة</button></div><h3>إشعارات المهام</h3><div class="notice-items">${ev.length?ev.map(n=>`<button class="notice-item ${n.read?'read':'unread'}" data-notice-id="${n.id}" data-task-id="${n.task_id}" data-deleted="${n.deleted?'1':'0'}"><b>${safe(noticeKinds[n.kind]||'تحديث المهمة')}</b><span>${safe(n.title)}</span><small>${fmt(n.created_at)} ${n.read?'· تمت القراءة':'· جديد'}</small></button>`).join(''):'<p class="hint">لا توجد إشعارات مهام بعد.</p>'}</div>${chat.length?`<h3>محادثات غير مقروءة</h3><div class="notice-items">${chat.map(([channel,n])=>`<button class="notice-item unread" data-notice-chat="${safe(channel)}"><b>${channel.startsWith('task:')?'مناقشة مهمة':channel==='general'?'المحادثة العامة':'محادثة القسم'}</b><small>${n} رسالة غير مقروءة</small></button>`).join('')}</div>`:''}<p class="hint">تظهر الإشعارات أثناء فتح المنصة وتُحدَّث تلقائيًا؛ وليست رسائل بريد خارجية.</p>`;
}
function openNotificationCenter(){
 const data=state.notices||{events:[],chat_channels:{},mail_unread:0,chat_unread:0,task_unread:0};
 modal('مركز الإشعارات',notificationCenterHtml(data));
 $('noticeInbox').onclick=()=>{closeModal();state.page='inbox';render()};
 $('noticeChats').onclick=()=>{closeModal();state.page='chat';render()};
 $('markAllNotices').onclick=request(async()=>{await api('/notifications/read-all',{method:'POST'});await pollNotifications(true);openNotificationCenter()});
 document.querySelectorAll('[data-notice-id]').forEach(btn=>btn.onclick=request(async()=>{
    await api('/notifications/'+btn.dataset.noticeId+'/read',{method:'POST'});
    await pollNotifications(true);const taskId=Number(btn.dataset.taskId);
    closeModal();if(btn.dataset.deleted==='1'){toast('تم إلغاء هذه المهمة، وسجل الإلغاء محفوظ');return}
    await loadAll();state.page='tasks';render();if(state.tasks.some(t=>t.id===taskId))taskModal(taskId);
 }));
 document.querySelectorAll('[data-notice-chat]').forEach(btn=>btn.onclick=()=>{const ch=btn.dataset.noticeChat;closeModal();state.page='chat';state.channel=ch;render()});
}
function renderLogin(){stopBackground();root.innerHTML=`<div class="login-shell"><div class="login-card"><div class="logo-big"><span>إ</span> إنجاز</div><p class="hint">المنصة الإلكترونية لإدارة ومتابعة الأعمال عن بُعد</p><h1>تسجيل دخول الموظفين</h1><p class="hint">استخدم البريد الإلكتروني وكلمة المرور الخاصة بحسابك في المنصة.</p><form id="loginForm" class="form-stack"><label class="form-field">البريد الإلكتروني<input name="email" type="email" class="input" autocomplete="username" required maxlength="200"></label><label class="form-field">كلمة المرور<input name="password" type="password" class="input" autocomplete="current-password" required></label><p id="loginError" class="error"></p><button class="btn full" type="submit">دخول إلى المنصة</button></form><p class="hint">النسخة الأولية تستخدم حسابات داخلية مستقلة، وليست مرتبطة بعد بتسجيل الدخول المؤسسي Microsoft Entra ID.</p></div></div>`;
 $('loginForm').onsubmit=request(async e=>{e.preventDefault();let fd=new FormData(e.currentTarget);let btn=e.currentTarget.querySelector('button');btn.disabled=true;try{let response=await api('/auth/login',{method:'POST',data:{email:fd.get('email'),password:fd.get('password')}});state.me=response.user;state.csrf=response.csrf;state.page='dashboard';state.noticeInitialized=false;await loadAll();startBackground();render();if(state.me.must_change_password)changePasswordModal(true)}catch(error){$('loginError').textContent=error.message}finally{btn.disabled=false}});
}
function navItems(){return [['dashboard','▦','لوحة التحكم'],['tasks','☑','المهام والتكليفات'],['inbox','✉','البريد الوارد'],['chat','▤','مركز المحادثات'],['activity','◷','سجل الدخول والنشاط'],['employees','♧','الموظفون والأقسام'],['audit','◈','سجل الإجراءات']].filter(x=>x[0]!=='audit'||isExecutive(state.me.role))}
function renderShell(title,body){const m=state.me;root.innerHTML=`<div class="layout"><aside class="sidebar"><div class="logo">إنجاز<small>منصة إدارة الأعمال عن بُعد</small></div><nav class="nav">${navItems().map(([id,ico,label])=>`<button data-page="${id}" class="${id===state.page?'active':''}">${ico} <span>${label}</span>${id==='inbox'&&state.mailUnread?`<b class="mail-badge" data-mail-badge>${state.mailUnread}</b>`:''}${id==='chat'&&state.chatUnread?`<b class="mail-badge" data-chat-badge>${state.chatUnread}</b>`:''}</button>`).join('')}</nav><div class="sidebar-foot"><b>${safe(m.name)}</b><div>${safe(roles[m.role])}</div><small>${safe(affiliationLabel(m))}</small></div></aside><div class="workspace"><header class="topbar"><div><div class="sub">نظام إنجاز · البيئة متعددة المستخدمين</div><h1>${safe(title)}</h1></div><div class="profile"><button type="button" class="btn secondary small notice-bell" id="notificationBell" aria-label="الإشعارات">🔔 <span id="notificationCount">${state.noticeCount||''}</span></button><div class="avatar">${safe(m.name.substring(0,1))}</div><div class="label"><b>${safe(m.name)}</b><div class="hint">${safe(roles[m.role])}</div></div><button class="btn secondary small" id="changeEmailBtn">تعديل بريدي</button><button class="btn secondary small" id="changePwBtn">كلمة المرور</button><button class="btn outline small" id="logoutBtn">خروج</button></div></header><main class="page">${body}</main></div></div>`;
 document.querySelectorAll('[data-page]').forEach(btn=>btn.onclick=()=>{state.page=btn.dataset.page;state.selectedTask=null;render()});
 $('logoutBtn').onclick=request(async()=>{await api('/auth/logout',{method:'POST'});state.me=null;state.csrf=null;renderLogin()});
 $('changePwBtn').onclick=()=>changePasswordModal(false);$('changeEmailBtn').onclick=()=>changeEmailModal();$('notificationBell').onclick=()=>openNotificationCenter();
}
function render(){if(!state.me)return renderLogin();if(state.page!=='chat'&&state.chatTimer){clearInterval(state.chatTimer);state.chatTimer=null}
 switch(state.page){case'dashboard':return renderDashboard();case'tasks':return renderTasks();case'chat':return renderChat();case'inbox':return renderInbox();case'activity':return renderActivity();case'employees':return renderEmployees();case'audit':return renderAudit();default:return renderDashboard()}}
function cards(){const d=state.dashboard;return `<div class="cards">${[['إجمالي التكليفات',d.total||0,'مهمة مسجلة'],['المهام المكتملة',d.done||0,'معتمدة نهائيًا'],['المهام المتأخرة',d.late||0,'تحتاج متابعة'],['متوسط الإنجاز',(d.progress||0)+'%','متوسط الأوزان']].map(([title,val,sub])=>`<div class="stat"><span>${title}</span><div class="metric">${val}</div><small>${sub}</small></div>`).join('')}</div>`}
function status(t){return `<span class="pill ${safe(t.status)}">${safe(statuses[t.status]||t.status)}</span>`}
function miniTask(t){return `<div class="task-card"><div class="detail-head"><div class="task-title">${safe(t.title)}</div>${status(t)}</div><div class="task-desc">${safe(t.assignee)} · ${safe(t.department)}</div><div class="task-meta"><span class="pill ${safe(t.priority)}">${safe(priorities[t.priority])}</span><span class="hint">الاستحقاق: ${fdate(t.due_date)}</span><b>${t.progress}%</b></div><div class="bar"><span style="width:${t.progress}%"></span></div><div class="action-row"><button class="btn secondary small" data-task="${t.id}">فتح المهمة</button></div></div>`}
function attachTasks(){document.querySelectorAll('[data-task]').forEach(e=>e.onclick=()=>taskModal(Number(e.dataset.task)))}
function presenceBlock(p){
 if(!p)return '<section class="panel presence-board"><h3>الاتصال الحالي حسب القسم</h3><p class="hint">جارٍ قراءة حالة الاتصال...</p></section>';
 const groups=p.departments||[];
 const deptCards=groups.map(g=>`<article class="presence-dept"><div class="presence-dept-head"><div><h4>${safe(g.department)}</h4><small>المتصلون من القسم</small></div><div class="presence-count"><b>${Number(g.online_count)||0}</b><span>/ ${Number(g.total_staff)||0}</span></div></div><div class="presence-names">${g.employees.length?g.employees.map(u=>`<div class="presence-person"><span class="presence-person-dot" aria-hidden="true"></span><div class="presence-person-info"><strong>${safe(u.name)}</strong><small>${u.active_now?'نشط مؤخرًا':'متصل'} · آخر ظهور ${fmt(u.last_seen)}</small></div></div>`).join(''):'<div class="presence-empty">لا يوجد متصلون حاليًا</div>'}</div></article>`).join('');
 return `<section class="panel presence-board" aria-label="المتصلون الآن حسب القسم"><div class="presence-board-head"><div><h3><span class="presence-live-dot" aria-hidden="true"></span> المتصلون الآن حسب القسم</h3><p>عرض مباشر لأسماء المتصلين من كل قسم ضمن صلاحياتك.</p></div><div class="presence-totals"><b>${Number(p.online_total)||0} متصل</b><small>من ${Number(p.total_staff)||0} مستخدم · تحديث ${fmt(p.as_of)}</small></div></div><div class="presence-departments">${deptCards||'<div class="presence-empty">لا توجد أقسام متاحة لهذا الحساب</div>'}</div><p class="presence-note">يُعد المستخدم متصلًا عند وصول إشارة من التطبيق خلال آخر ${Number(p.online_window_seconds)||75} ثانية. هذه الحالة تقديرية ولا تثبت الحضور أو العمل الفعلي.</p></section>`;
}
async function refreshPresence(){
 if(!state.me||state.page!=='dashboard')return;
 const data=await api('/presence');
 if(!state.me||state.page!=='dashboard')return;
 state.presence=data;
 const container=$('dashboardPresence');if(container)container.innerHTML=presenceBlock(data);
}
function renderDashboard(){const ds=state.dashboard.by_department||[],t=state.tasks.slice().sort((a,b)=>a.due_date.localeCompare(b.due_date)).slice(0,5);
 const body=`<div class="page-head"><div><h2>متابعة الأعمال واستمراريتها</h2><p>تحديثات مباشرة من قاعدة البيانات المركزية، ضمن الصلاحيات المعتمدة.</p></div><button class="btn" id="refreshDashboard">تحديث البيانات ⟳</button></div><div id="dashboardPresence">${presenceBlock(state.presence)}</div>${cards()}<div class="grid2"><section class="panel"><h3>المهام ذات الأولوية</h3>${t.length?t.map(miniTask).join(''):'<div class="empty">لا توجد مهام مسندة</div>'}</section><section class="panel"><h3>مؤشرات الأقسام</h3>${ds.map(d=>`<div class="task-card"><div class="detail-head"><b>${safe(d.department)}</b><span>${d.count} مهمة · ${d.progress}%</span></div><div class="bar" style="margin-top:12px"><span style="width:${d.progress}%"></span></div></div>`).join('')}<p class="note">لا تُعتمد المهمة مكتملة بنسبة 100% إلا بعد موافقة المخوّل. سجلات الاتصال لا تمثل إثبات الحضور الوظيفي.</p><h3>المهام قيد المراجعة</h3><b>${state.dashboard.review||0}</b></section></div>`;
 renderShell('لوحة التحكم التنفيذية',body);attachTasks();$('refreshDashboard').onclick=request(async()=>{await loadAll();render();toast('تم تحديث البيانات')});refreshPresence().catch(()=>{});}
function renderTasks(){const visible=state.tasks;const body=`<div class="page-head"><div><h2>مهام الإدارة</h2><p>التكليف ومتابعة المراحل ونسب الإنجاز والاعتمادات.</p></div>${state.me.role!=='employee'?'<button id="newTask" class="btn">＋ إضافة مهمة</button>':''}</div><div class="panel"><div class="filters"><select id="taskStatus" class="input"><option value="">جميع الحالات</option>${Object.entries(statuses).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select><input id="taskSearch" class="input" placeholder="بحث في المهام..." aria-label="بحث المهام"><span class="hint">${visible.length} مهمة في نطاق صلاحيتك</span></div><div id="taskList" class="grid2"></div></div>`;
 renderShell('إدارة المهام',body);const update=()=>{const needle=$('taskSearch').value.trim(),stat=$('taskStatus').value;const ts=visible.filter(t=>(!stat||t.status===stat)&&(!needle||t.title.includes(needle)||t.assignee.includes(needle)));$('taskList').innerHTML=ts.length?ts.map(miniTask).join(''):'<div class="empty">لا توجد مهام مطابقة</div>';attachTasks()};$('taskSearch').oninput=update;$('taskStatus').onchange=update;update();if($('newTask'))$('newTask').onclick=taskCreateModal;}
function modal(title,html){closeModal();const cover=document.createElement('div');cover.className='modal-cover';cover.innerHTML=`<div class="modal" role="dialog" aria-modal="true" aria-label="${safe(title)}"><button type="button" class="modal-close" id="closeModal">إغلاق ×</button><h2>${safe(title)}</h2>${html}</div>`;document.body.appendChild(cover);$('closeModal').onclick=closeModal;cover.addEventListener('click',e=>{if(e.target===cover)closeModal()});return cover}
const closeModal=()=>{document.querySelector('.modal-cover')?.remove();if(state.discussionTimer){clearInterval(state.discussionTimer);state.discussionTimer=null}};
function field(name,label,type='text',value='',props=''){return `<label class="form-field">${label}<input class="input" name="${name}" type="${type}" value="${safe(value)}" ${props}></label>`}
function taskCreateModal(){state.taskRequestId=(typeof crypto!=='undefined'&&crypto.randomUUID)?crypto.randomUUID():('task_'+Date.now()+'_'+Math.random().toString(36).slice(2));const ds=state.departments.filter(d=>isExecutive(state.me.role)||state.me.department_id===d.id);
 modal('إنشاء تكليف جديد',`<form id="newTaskForm" class="form-stack"><div class="form-grid">${field('title','عنوان المهمة','text','','required maxlength="250"')}<label class="form-field">القسم<select name="department_id" id="taskDept" class="input">${ds.map(d=>`<option value="${d.id}">${safe(d.name)}</option>`).join('')}</select></label><label class="form-field">الموظف المكلف<select name="assigned_to" id="taskOwner" class="input"></select></label>${field('due_date','موعد الإنجاز','date',new Date(Date.now()+3*86400000).toISOString().slice(0,10),'required')}<label class="form-field">الأولوية<select name="priority" class="input"><option value="medium">متوسطة</option><option value="high">عالية</option><option value="low">منخفضة</option></select></label>${field('output','المخرج المطلوب','text','','maxlength="300"')}<label class="form-field full">وصف المهمة<textarea name="description" class="input" maxlength="4000"></textarea></label></div><div class="attachment-box"><b>📎 مرفقات التكليف (اختياري)</b><p class="hint">يمكنك إرفاق ملفات التعليمات عند إنشاء المهمة، وسيتمكن الموظف من إضافة مستندات الإنجاز من داخل المهمة لاحقًا. حتى 6 ملفات، 8 ميجابايت للملف.</p><input id="newTaskAttachments" type="file" class="input" accept="${attachmentTypes}" multiple><p id="newTaskAttachmentNames" class="hint"></p></div><div class="section-label">مراحل الإنجاز (الأوزان يجب أن تساوي 100%)</div><div class="grid3">${[1,2,3].map((n)=>`<div>${field('step'+n,'المرحلة '+n,'text',n===1?'التحضير':n===2?'التنفيذ':'الرفع للمراجعة','required')}${field('weight'+n,'الوزن %','number',n===1?30:n===2?40:30,'min="1" max="100" required')}</div>`).join('')}</div><p id="taskFormErr" class="error"></p><div class="action-row"><button type="submit" class="btn">حفظ التكليف</button><button type="button" class="btn secondary" id="cancelTask">إلغاء</button></div></form>`);
 const populate=()=>{const v=Number($('taskDept').value);const employees=state.users.filter(u=>u.active&&u.department_id===v);$('taskOwner').innerHTML=employees.map(u=>`<option value="${u.id}">${safe(u.name)} (${safe(roles[u.role])})</option>`).join('');if(!employees.length)$('taskOwner').innerHTML='<option value="">لا يوجد موظفون نشطون</option>'};$('taskDept').onchange=populate;populate();$('cancelTask').onclick=closeModal;$('newTaskAttachments').onchange=e=>{const fs=Array.from(e.target.files||[]);$('newTaskAttachmentNames').textContent=fs.length?fs.map(f=>f.name).join('، '):''};
 $('newTaskForm').onsubmit=request(async e=>{
  e.preventDefault();const form=e.currentTarget;
  if(form.dataset.submitting==='1')return;
  const data=new FormData(form);
  let payload={request_id:state.taskRequestId,title:data.get('title'),description:data.get('description'),output:data.get('output'),department_id:Number(data.get('department_id')),assigned_to:Number(data.get('assigned_to')),due_date:data.get('due_date'),priority:data.get('priority'),steps:[1,2,3].map(n=>({title:data.get('step'+n),weight:Number(data.get('weight'+n))}))};
  if(payload.steps.reduce((n,s)=>n+s.weight,0)!==100){$('taskFormErr').textContent='يجب أن يكون مجموع الأوزان 100%';return}
  const files=Array.from($('newTaskAttachments').files||[]);
  if(files.length>6||files.some(f=>f.size>maxAttachSize)){$('taskFormErr').textContent='يمكن اختيار 6 ملفات كحد أقصى، وكل ملف لا يتجاوز 8 ميجابايت';return}
  form.dataset.submitting='1';const btn=form.querySelector('button[type="submit"]');btn.disabled=true;btn.textContent='جارٍ حفظ التكليف...';
  try{
    const created=await api('/tasks',{method:'POST',data:payload});
    let error='';if(files.length){try{await uploadTaskFiles(created.id,files,'instruction')}catch(err){error=err.message}}
    closeModal();await loadAll();render();toast(error?'تم حفظ التكليف، لكن بعض المرفقات لم ترفع.':'تم حفظ التكليف بنجاح');
    if(error)taskModal(created.id);
  }finally{form.dataset.submitting='0';btn.disabled=false;btn.textContent='حفظ التكليف'}
 });
}
function taskModal(taskId){const t=state.tasks.find(t=>t.id===taskId);if(!t)return;const mine=t.assigned_to===state.me.id||isExecutive(state.me.role)||state.me.role==='head'&&state.me.department_id===t.department_id;const canReview=isExecutive(state.me.role)||state.me.role==='head'&&state.me.department_id===t.department_id;
 modal('تفاصيل التكليف',`<div class="detail-head"><div><h3>${safe(t.title)}</h3><div class="hint">${safe(t.assignee)} · ${safe(t.department)} · الاستحقاق ${fdate(t.due_date)}</div></div>${status(t)}</div><p>${safe(t.description)}</p><div class="note"><b>المخرج المطلوب:</b> ${safe(t.output||'لم يحدد')}<div><b>النتيجة المرفوعة:</b> ${safe(t.result_text||'لم تُرفع بعد')}</div><div style="margin-top:9px">نسبة الإنجاز: <b>${t.progress}%</b></div><div class="bar"><span style="width:${t.progress}%"></span></div></div><div class="detail-steps">${t.steps.map(st=>`<label class="step"><input type="checkbox" data-step="${st.id}" ${st.done?'checked':''} ${(!mine||['done','review'].includes(t.status))?'disabled':''}><div><b>${safe(st.title)}</b><small>وزن المرحلة: ${st.weight}%</small></div></label>`).join('')}</div><div class="action-row">${mine&&!['done','review'].includes(t.status)?'<button class="btn" id="submitTask">رفع للمراجعة</button>':''}${canReview&&t.status==='review'?'<button class="btn" id="approveTask">اعتماد نهائي</button><button class="btn danger" id="revisionTask">إعادة للتعديل</button>':''}${canReview&&t.status!=='done'?'<button class="btn outline" id="assignTask">إعادة إسناد</button>':''}${t.created_by===state.me.id||t.assigned_to===state.me.id?'<button class="btn secondary" id="taskChat">مناقشة خاصة للمهمة</button>':''}${(isExecutive(state.me.role)||t.created_by===state.me.id)&&t.status!=='done'?'<button class="btn danger" id="deleteTask">حذف التكليف</button>':''}</div><div class="attachment-box"><h3>📎 مرفقات المهمة</h3><p class="hint">تُحفظ المرفقات ضمن المهمة مع اسم الرافع وتاريخه، ولا تظهر إلا للمخولين بالاطلاع على المهمة.</p><div id="taskAttachments" class="attachment-list"><p class="hint">جارٍ تحميل المرفقات...</p></div>${t.status!=='done'?`<div class="attachment-upload"><label class="form-field">نوع المرفق<select class="input" id="taskAttachmentCategory">${canReview?'<option value="instruction">ملف توجيهات التكليف</option>':''}<option value="result" ${!canReview?'selected':''}>مرفق الإنجاز / النتيجة</option></select></label><label class="form-field">اختيار الملفات<input class="input" id="taskFileInput" type="file" accept="${attachmentTypes}" multiple></label><button class="btn secondary" type="button" id="taskUploadButton">رفع المرفق</button><p class="hint">الحد 8 ميجابايت للملف، 6 ملفات في الرفع الواحد و20 للمهمة، بإجمالي 40 ميجابايت للمهمة. صيغ PDF وOffice والصور وTXT وCSV.</p></div>`:''}</div><p class="hint">يحتسب الإنجاز من أوزان المراحل، مع عدم بلوغ 100% إلا بعد الاعتماد.</p>`);
 populateAttachments(t.id);
 if($('taskUploadButton'))$('taskUploadButton').onclick=request(async()=>{
   const files=Array.from($('taskFileInput').files||[]);if(!files.length)throw Error('اختر ملفًا واحدًا على الأقل');
   const btn=$('taskUploadButton');btn.disabled=true;btn.textContent='جارٍ رفع المرفقات...';
   try{let count=await uploadTaskFiles(t.id,files,$('taskAttachmentCategory').value);$('taskFileInput').value='';await populateAttachments(t.id);toast(`تم رفع ${count} مرفق`)}
   finally{btn.disabled=false;btn.textContent='رفع المرفق'}
 });
 document.querySelectorAll('[data-step]').forEach(el=>el.onchange=request(async()=>{let checked=el.checked;let updated=await api(`/tasks/${t.id}/steps/${el.dataset.step}`,{method:'POST',data:{done:checked}});await loadAll();render();taskModal(t.id);toast('تم تحديث المرحلة')}));
 if($('submitTask'))$('submitTask').onclick=request(async()=>{const result=window.prompt('اكتب ملخص النتيجة النهائية والمخرج الفعلي:')||'';if(result.trim().length<3)return;await api(`/tasks/${t.id}/submit`,{method:'POST',data:{result_text:result}});closeModal();await loadAll();render();toast('رُفعت المهمة للمراجعة')});
 if($('approveTask'))$('approveTask').onclick=request(async()=>{await api(`/tasks/${t.id}/decision`,{method:'POST',data:{decision:'approve'}});closeModal();await loadAll();render();toast('تم الاعتماد النهائي')});
 if($('revisionTask'))$('revisionTask').onclick=request(async()=>{const reason=window.prompt('ملاحظات إعادة المهمة للتعديل:')||'';if(!reason.trim())return;await api(`/tasks/${t.id}/decision`,{method:'POST',data:{decision:'revision',reason}});closeModal();await loadAll();render();toast('أُعيدت المهمة للتعديل')});
 if($('assignTask'))$('assignTask').onclick=()=>{
   const choices=state.users.filter(u=>u.active&&u.department_id===t.department_id);
   modal('إعادة إسناد المهمة',`<form id="assignForm" class="form-stack"><p class="note">ستبقى المراحل السابقة وسجل الإجراءات محفوظين.</p><label class="form-field">الموظف المسؤول<select name="assigned_to" class="input">${choices.map(u=>`<option value="${u.id}" ${u.id===t.assigned_to?'selected':''}>${safe(u.name)}</option>`).join('')}</select></label><button class="btn">حفظ الإسناد</button></form>`);
   $('assignForm').onsubmit=request(async e=>{e.preventDefault();await api(`/tasks/${t.id}/assign`,{method:'POST',data:{assigned_to:Number(new FormData(e.target).get('assigned_to'))}});closeModal();await loadAll();render();toast('تم نقل التكليف')});
 };
 if($('taskChat'))$('taskChat').onclick=()=>taskDiscussionModal(t.id);
 if($('deleteTask'))$('deleteTask').onclick=request(async()=>{const reason=window.prompt('سبب حذف التكليف (سيظل محفوظًا في سجل التدقيق):')||'';if(reason.trim().length<3)return;if(!window.confirm('تأكيد إلغاء التكليف وإخفائه عن جميع القوائم؟'))return;const b=$('deleteTask');b.disabled=true;try{await api(`/tasks/${t.id}`,{method:'DELETE',data:{reason:reason.trim()}});closeModal();await loadAll();render();toast('تم إلغاء التكليف وحفظ سجل العملية')}finally{b.disabled=false}});
}
function channels(){const list=[{id:'general',name:'المحادثة العامة للإدارة'}];state.departments.filter(d=>isExecutive(state.me.role)||state.me.department_id===d.id).forEach(d=>list.push({id:'dept:'+d.id,name:'قسم '+d.name}));state.tasks.filter(t=>t.created_by===state.me.id||t.assigned_to===state.me.id).forEach(t=>list.push({id:'task:'+t.id,name:'مهمة: '+t.title}));return list}
async function refreshMessages(){try{
 const currentChannel=state.channel, msgs=await api('/messages?channel='+encodeURIComponent(currentChannel));
 if(state.channel!==currentChannel)return;
 state.messages=msgs;
 if(state.page==='chat'&&$('messageList')){
   const list=$('messageList'),near=list.scrollHeight-list.scrollTop-list.clientHeight<85;
   list.innerHTML=msgs.map(m=>`<div class="message ${m.sender_id===state.me.id?'own':''}"><strong>${safe(m.sender)}</strong>${safe(m.text)}<time>${fmt(m.created_at)}</time></div>`).join('')||'<div class="empty">لا توجد رسائل في هذه المحادثة</div>';
   if(near)list.scrollTop=list.scrollHeight;
   if(msgs.length)await api('/messages/read',{method:'POST',data:{channel:currentChannel,last_id:msgs[msgs.length-1].id}});
   await pollNotifications(true);
 }
}catch(e){}}
async function taskDiscussionModal(taskId){
 const task=state.tasks.find(t=>t.id===taskId);if(!task)return;
 const channel='task:'+task.id;
 modal('مناقشة التكليف',`<p class="note">محادثة خاصة مستمرة بين مصدر التكليف والموظف المكلف، مرتبطة بالمهمة رقم ${task.id}. جميع الردود محفوظة في السجل نفسه.</p><div id="taskDiscussionHistory" class="messages task-discussion"><p class="hint">جارٍ تحميل سجل المناقشة...</p></div><form id="taskDiscussionForm" class="chat-compose"><input name="text" class="input" placeholder="اكتب ردك على المناقشة..." maxlength="4000" required><button class="btn" type="submit">إرسال الرد</button></form>`);
 const update=async()=>{
    const box=$('taskDiscussionHistory');if(!box)return;
    const rows=await api('/messages?channel='+encodeURIComponent(channel));if(!$('taskDiscussionHistory'))return;
    const near=box.scrollHeight-box.scrollTop-box.clientHeight<85;
    box.innerHTML=rows.map(m=>`<div class="message ${m.sender_id===state.me.id?'own':''}"><strong>${safe(m.sender)}</strong>${safe(m.text)}<time>${fmt(m.created_at)}</time></div>`).join('')||'<p class="hint">لا توجد رسائل بعد. ابدأ المناقشة هنا.</p>';
    if(near)box.scrollTop=box.scrollHeight;
    if(rows.length)await api('/messages/read',{method:'POST',data:{channel,last_id:rows[rows.length-1].id}});
    await pollNotifications(true);
 };
 $('taskDiscussionForm').onsubmit=request(async e=>{e.preventDefault();const form=e.currentTarget;if(form.dataset.sending==='1')return;const text=form.elements.text.value.trim();if(!text)return;const btn=form.querySelector('button');form.dataset.sending='1';btn.disabled=true;try{await api('/messages',{method:'POST',data:{channel,text}});form.reset();await update()}finally{btn.disabled=false;form.dataset.sending='0'}});
 await update();if(state.discussionTimer)clearInterval(state.discussionTimer);
 state.discussionTimer=setInterval(()=>{if($('taskDiscussionHistory')&&!document.hidden)update().catch(()=>{})},8000);
}

function renderChat(){const available=channels();if(!available.some(c=>c.id===state.channel))state.channel='general';const body=`<div class="page-head"><div><h2>التواصل المباشر بين الإدارة والموظفين</h2><p>محادثات دائمة مع تحديث دوري كل 8 ثوانٍ.</p></div><button id="chatRefresh" class="btn secondary">تحديث الرسائل</button></div><div class="chat-grid"><div class="panel channels">${available.map(c=>`<button data-channel="${safe(c.id)}" class="${state.channel===c.id?'selected':''}">${safe(c.name)}</button>`).join('')}</div><div class="panel"><h3 id="channelName">${safe(available.find(c=>c.id===state.channel).name)}</h3><div id="messageList" class="messages"></div><form id="messageForm" class="chat-compose"><input class="input" name="text" autocomplete="off" maxlength="4000" placeholder="اكتب توجيهًا أو رسالة..." required><button class="btn">إرسال</button></form></div></div>`;renderShell('مركز المحادثات',body);document.querySelectorAll('[data-channel]').forEach(x=>x.onclick=()=>{state.channel=x.dataset.channel;renderChat()});$('chatRefresh').onclick=refreshMessages;
 $('messageForm').onsubmit=request(async e=>{e.preventDefault();const form=e.currentTarget;if(form.dataset.sending==='1')return;const text=form.elements.text.value.trim();if(!text)return;const b=form.querySelector('button');b.disabled=true;form.dataset.sending='1';try{await api('/messages',{method:'POST',data:{channel:state.channel,text}});form.reset();await refreshMessages()}finally{b.disabled=false;form.dataset.sending='0'}});refreshMessages();if(state.chatTimer)clearInterval(state.chatTimer);state.chatTimer=setInterval(()=>{if(state.page==='chat')refreshMessages()},8000);
}
function renderEmployees(){
  const all=state.users;
  const director=state.me.role==='director';
  const advisor=state.me.role==='advisor';
  const executive=director||advisor;
  const canAdd=director||state.me.role==='head';
  const heads=new Map(all.filter(u=>u.role==='head').map(u=>[u.department_id,u]));
  const rows=all.map(u=>{
    const currentHead=heads.get(u.department_id);
    const occupied=u.role==='employee'&&!!currentHead;
    const canEdit=executive?u.role!=='director'&&(director||u.role!=='advisor'):
      state.me.role==='head'&&u.role==='employee'&&u.department_id===state.me.department_id;
    const buttons=[];
    if(executive&&u.role==='employee'){
      buttons.push(`<button class="btn secondary small" data-role-user="${u.id}" data-target-role="head" ${occupied||!u.active?'disabled':''} title="${occupied?'يجب تحويل رئيس القسم الحالي إلى موظف أولًا':!u.active?'يلزم تفعيل الحساب قبل الترقية':'تعيين رئيس قسم'}">تعيين رئيس قسم</button>`);
      if(director)buttons.push(`<button class="btn secondary small" data-role-user="${u.id}" data-target-role="advisor" ${!u.active?'disabled':''}>تعيين مستشار القطاع</button>`);
    }else if(executive&&u.role==='head'){
      buttons.push(`<button class="btn secondary small" data-role-user="${u.id}" data-target-role="employee">تحويل إلى موظف</button>`);
    }else if(director&&u.role==='advisor'){
      buttons.push(`<button class="btn secondary small" data-role-user="${u.id}" data-target-role="employee">إلغاء دور المستشار</button>`);
    }
    const roleHint=executive&&occupied?`<small class="role-hint">رئيس القسم الحالي: ${safe(currentHead.name)} — يجب تحويله إلى موظف أولًا</small>`:'';
    return `<div class="employee-row"><div><b>${safe(u.name)}</b><small>${safe(u.email)} · ${safe(affiliationLabel(u))} · ${safe(roles[u.role]||u.role)}</small>${roleHint}</div><div class="employee-actions"><span class="pill ${u.active?'done':'offline'}">${u.active?'نشط':'موقوف'}</span>${canEdit?`<button class="btn secondary small" data-edit-user="${u.id}">تعديل</button>`:''}${buttons.join('')}</div></div>`;
  }).join('');
  const managementNote=director?`<div class="note" style="margin-bottom:14px"><b>الأدوار:</b> مستشار القطاع وظيفة مستقلة ولا يتبع أي قسم. يمكن تعيينه من الموظفين، أو إنشاء حساب جديد بهذا الدور دون اختيار قسم. للمستشار الصلاحيات التنفيذية العامة باستثناء إضافة حسابات الموظفين أو الاستيراد من Excel. تعيين المستشار أو إلغاء دوره متاح للمدير العام فقط. وعند تغيير رئيس القسم يجب تحويله إلى موظف أولًا قبل تعيين البديل.</div>`:
    advisor?`<div class="note" style="margin-bottom:14px"><b>صلاحيات مستشار القطاع:</b> وظيفة مستقلة لا تتبع قسمًا، وتشمل متابعة جميع الأقسام والمهام والتقارير وإدارة الصلاحيات التنفيذية للموظفين ورؤساء الأقسام. <b>إضافة الحسابات الجديدة والاستيراد من Excel غير مسموحين</b>، ولا يمكن للمستشار تعيين مستشار آخر أو تغيير صلاحياته.</div>`:'';
  renderShell('الموظفون والأقسام',`<div class="page-head"><div><h2>سجل الموظفين</h2><p>عرض الأقسام والأدوار وحسابات الموظفين وفق الصلاحيات المعتمدة.</p></div>${canAdd?`<div class="action-row" style="margin:0">${director?'<button class="btn secondary" id="importUsers">⇧ استيراد من Excel</button>':''}<button class="btn" id="newUser">＋ إضافة موظف</button></div>`:''}</div>${managementNote}<div class="panel">${rows||'<div class="empty">لا يوجد موظفون</div>'}</div><p class="footer-note">يُحتفظ بالمهام والمراسلات والملفات السابقة بعد تغيير الصلاحية. تُلغى الجلسات النشطة للمستخدم عند تغيير دوره.</p>`);
  if($('newUser'))$('newUser').onclick=()=>userForm();
  if($('importUsers'))$('importUsers').onclick=()=>importEmployeesModal();
  document.querySelectorAll('[data-edit-user]').forEach(el=>el.onclick=()=>userForm(state.users.find(u=>u.id===Number(el.dataset.editUser))));
  document.querySelectorAll('[data-role-user]').forEach(el=>el.onclick=()=>changeRoleModal(state.users.find(u=>u.id===Number(el.dataset.roleUser)),el.dataset.targetRole));
}
function changeRoleModal(user,newRole){
  const director=state.me.role==='director',executive=isExecutive(state.me.role);
  if(!user||!executive||user.role==='director'||(user.role==='advisor'&&!director)||
     (newRole==='advisor'&&!director)||!['employee','head','advisor'].includes(newRole)){
    toast('ليست لديك صلاحية تغيير هذا الدور');return;
  }
  if(user.role===newRole){toast('الدور الحالي مطابق للدور المطلوب');return}
  if(user.role==='head'&&newRole==='advisor'){toast('حوّل رئيس القسم إلى موظف أولًا');return}
  if(user.role==='advisor'&&newRole==='head'){toast('حوّل المستشار إلى موظف أولًا');return}
  const occupant=state.users.find(u=>u.role==='head'&&u.department_id===user.department_id&&u.id!==user.id);
  if(newRole==='head'&&occupant){toast('يجب تحويل رئيس القسم الحالي إلى موظف أولًا');return}
  if(['head','advisor'].includes(newRole)&&!user.active){toast('يجب تفعيل حساب الموظف أولًا');return}
  const returnsToDepartment = user.role==='advisor' && newRole==='employee';
  const departmentsList = state.departments.map(d=>`<option value="${d.id}">${safe(d.name)}</option>`).join('');
  modal('تغيير صلاحية المستخدم',`<form id="roleChangeForm" class="form-stack"><div class="note">المستخدم: <b>${safe(user.name)}</b><br>الارتباط التنظيمي الحالي: <b>${safe(affiliationLabel(user))}</b><br>الدور الحالي: <b>${safe(roles[user.role]||user.role)}</b><br>الدور الجديد: <b>${safe(roles[newRole])}</b></div>${returnsToDepartment?`<label class="form-field">القسم عند العودة إلى دور موظف<select id="advisorReturnDepartment" class="input" required><option value="">اختر القسم</option>${departmentsList}</select></label>`:''}<p class="hint">سيتم إنهاء جميع جلسات المستخدم النشطة ويلزمه تسجيل الدخول من جديد. لا تتأثر المهام أو المراسلات السابقة. ${newRole==='advisor'?'يُزال ارتباط المستشار بأي قسم، ويصبح وظيفة مستقلة على مستوى القطاع مع الصلاحيات التنفيذية دون إضافة الموظفين.':returnsToDepartment?'يجب اختيار القسم الذي سيعمل فيه بعد سحب دور المستشار.':'لا يمكن تعيين رئيسين للقسم نفسه.'}</p><div class="action-row"><button type="submit" class="btn">تأكيد تغيير الصلاحية</button><button type="button" class="btn secondary" id="cancelRoleChange">إلغاء</button></div></form>`);
  $('cancelRoleChange').onclick=closeModal;
  $('roleChangeForm').onsubmit=request(async e=>{
    e.preventDefault();
    const department=returnsToDepartment?Number($('advisorReturnDepartment').value):null;
    if(returnsToDepartment&&!department)throw Error('حدد القسم قبل تحويل مستشار القطاع إلى موظف');
    const btn=e.currentTarget.querySelector('[type=submit]');btn.disabled=true;
    try{await api(`/users/${user.id}/role`,{method:'PATCH',data:{role:newRole,...(returnsToDepartment?{department_id:department}:{})}});closeModal();await loadAll();renderEmployees();toast(`تم تغيير صلاحية ${user.name} إلى ${roles[newRole]}`)}
    finally{btn.disabled=false}
  });
}
async function bulkUpload(path,formData){
  let response;
  try{response=await fetch('/api'+path,{method:'POST',body:formData,credentials:'same-origin',headers:{'X-CSRF-Token':state.csrf||''}})}
  catch{throw Error('تعذّر الاتصال بالخادم أثناء رفع ملف Excel')}
  const result=await response.json().catch(()=>({}));
  if(response.status===401){state.me=null;stopBackground();renderLogin();throw Error('انتهت جلسة الدخول')}
  if(!response.ok)throw Error(result.detail||'تعذّر فحص الملف');
  return result;
}
function importEmployeesModal(){
  if(state.me.role!=='director'){toast('الاستيراد الجماعي متاح للمدير العام فقط');return}
  const ds=state.departments;
  modal('استيراد الموظفين من Excel',`<div class="note">تستطيع استيراد 100 موظف في كل ملف Excel بصيغة XLSX. الحقول الإلزامية: اسم الموظف، البريد الإلكتروني، كلمة المرور، الدور. عمود «القسم» اختياري عند تحديد قسم افتراضي أدناه.</div>
  <p class="hint">الأدوار المتاحة للاستيراد: موظف ورئيس قسم ومستشار القطاع. مستشار القطاع لا يتبع أي قسم، ويمكن ترك عمود القسم فارغًا له. لا يمكن تعيين رئيس ثانٍ لأي قسم؛ يجب تحويل الرئيس الحالي إلى موظف أولًا. جميع كلمات المرور مؤقتة، ويجب تغييرها عند تسجيل الدخول الأول.</p>
  <div class="action-row" style="margin:0 0 14px"><a class="btn secondary" href="/api/users/import/template" download="enjaz_employees_template.xlsx">↓ تحميل قالب Excel</a></div>
  <form id="bulkImportForm" class="form-stack">
    <label class="form-field">ملف الموظفين (.xlsx فقط وبحد أقصى 2 ميجابايت)<input id="bulkFile" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" class="input" required></label>
    <label class="form-field">القسم الافتراضي عند ترك عمود القسم فارغًا<select id="bulkDefaultDept" class="input"><option value="">بدون قسم افتراضي (يلزم تعبئة عمود القسم)</option>${ds.map(d=>`<option value="${d.id}">${safe(d.name)}</option>`).join('')}</select></label>
    <p class="hint">ملاحظة أمنية: يحتوي Excel على كلمات المرور كنص. احفظه مؤقتًا في مكان آمن، ولا ترسله عبر المحادثات، وتخلّص منه بأمان بعد استيراده.</p>
    <div class="action-row"><button type="submit" class="btn secondary" id="bulkPreview">1. فحص البيانات</button><button type="button" class="btn" id="bulkCommit" disabled>2. اعتماد الإضافة الجماعية</button></div>
    <div id="bulkResults" aria-live="polite"></div>
  </form>`);
  const form=$('bulkImportForm'),previewBtn=$('bulkPreview'),commitBtn=$('bulkCommit'),results=$('bulkResults');
  let lastCheck=null;
  const invalidate=()=>{lastCheck=null;commitBtn.disabled=true;results.innerHTML=''};
  $('bulkFile').onchange=invalidate;$('bulkDefaultDept').onchange=invalidate;
  const makeForm=()=>{let file=$('bulkFile').files[0];if(!file)throw Error('اختر ملف Excel أولًا');if(!file.name.toLowerCase().endsWith('.xlsx'))throw Error('الملف يجب أن يكون بصيغة XLSX');if(file.size>2*1024*1024)throw Error('الملف أكبر من 2 ميجابايت');const fd=new FormData();fd.append('file',file);if($('bulkDefaultDept').value)fd.append('default_department_id',$('bulkDefaultDept').value);return fd};
  form.onsubmit=request(async event=>{event.preventDefault();commitBtn.disabled=true;previewBtn.disabled=true;results.innerHTML='<p class="hint">جارٍ فحص الأسماء والصلاحيات وتكرار عناوين البريد...</p>';
    try{const data=await bulkUpload('/users/import/preview',makeForm());lastCheck=data;
      const bad=data.errors.length?`<div class="import-errors"><b>الأخطاء المطلوب تصحيحها:</b><ul>${data.errors.map(x=>`<li>صف ${Number(x.row)}: ${safe(x.message)}</li>`).join('')}</ul></div>`:'';
      const table=data.preview.length?`<div class="table-wrap"><table class="table"><thead><tr><th>الصف</th><th>اسم الموظف</th><th>البريد الإلكتروني</th><th>الدور</th><th>القسم</th></tr></thead><tbody>${data.preview.map(x=>`<tr><td>${Number(x.row)}</td><td>${safe(x.name)}</td><td>${safe(x.email)}</td><td>${safe(roles[x.role])}</td><td>${safe(x.department)}</td></tr>`).join('')}</tbody></table></div>`:'';
      results.innerHTML=`<div class="import-summary"><b>نتائج الفحص</b><span class="pill done">صحيح: ${data.valid_count}</span><span class="pill ${data.error_count?'revision':'done'}">أخطاء: ${data.error_count}</span></div>${bad}${table}${data.valid_count>25?'<p class="hint">يظهر أول 25 موظفًا فقط للمراجعة؛ سيُعتمد جميع الموظفين الصحيحين عند خلو الملف من الأخطاء.</p>':''}`;
      commitBtn.disabled=!(data.valid_count>0&&data.error_count===0);
    }finally{previewBtn.disabled=false}});
  commitBtn.onclick=request(async()=>{if(!lastCheck||lastCheck.error_count||!lastCheck.valid_count)throw Error('افحص الملف وصحح الأخطاء أولًا');
    if(!window.confirm(`هل تريد إضافة ${lastCheck.valid_count} حسابًا؟ لا يمكن التراجع عن العملية تلقائيًا.`))return;
    commitBtn.disabled=true;previewBtn.disabled=true;results.innerHTML='<p class="hint">جارٍ إنشاء الحسابات وتشفير كلمات المرور... لا تغلق النافذة حتى تظهر النتيجة.</p>';
    try{const response=await bulkUpload('/users/import',makeForm());closeModal();await loadAll();renderEmployees();toast(`تمت إضافة ${response.created} موظفًا بنجاح`)}
    catch(error){results.innerHTML=`<p class="error">${safe(error.message)}. لم يتم حفظ دفعة جزئية.</p>`;throw error}
    finally{previewBtn.disabled=false}});
}

function userForm(u){
  const fresh=!u;
  if(fresh&&!['director','head'].includes(state.me.role)){
    toast('إضافة الموظفين غير متاحة لمستشار القطاع');return;
  }
  const ds=state.departments.filter(d=>isExecutive(state.me.role)||state.me.department_id===d.id);
  const advisorEdit=!fresh&&u.role==='advisor';
  const deptOptions=ds.map(d=>`<option value="${d.id}" ${u?.department_id===d.id?'selected':''}>${safe(d.name)}</option>`).join('');
  const deptField=advisorEdit?
    `<div class="form-field"><b>الارتباط التنظيمي</b><p class="hint">مستشار القطاع — وظيفة مستقلة على مستوى القطاع، ولا يتبع أي قسم.</p></div>`:
    `<label class="form-field" id="newUserDepartmentField">القسم<select name="department_id" id="newUserDepartment" class="input" ${(!fresh&&!isExecutive(state.me.role))?'disabled':''}>${deptOptions}</select></label>`;
  const roleField=fresh&&state.me.role==='director'?
    `<label class="form-field">الدور<select name="role" id="newUserRole" class="input"><option value="employee">موظف</option><option value="head">رئيس قسم</option><option value="advisor">مستشار القطاع</option></select></label>`:'';
  modal(fresh?'إضافة مستخدم وحساب دخول':'تعديل المستخدم',
    `<form id="userForm" class="form-stack"><div class="form-grid">${field('full_name','اسم المستخدم','text',u?.name||'','required maxlength="160"')}${fresh?field('email','البريد الإلكتروني','email','','required'):''}${deptField}${roleField}${field('temporary_password',fresh?'كلمة مرور مؤقتة (12 حرفًا أو أكثر)':'إعادة تعيين كلمة المرور (اختياري)','password','','minlength="12" autocomplete="new-password" '+(fresh?'required':''))}${!fresh?'<label class="form-field">حالة الحساب<select name="active" class="input"><option value="true" '+(u.active?'selected':'')+'>نشط</option><option value="false" '+(!u.active?'selected':'')+'>موقوف</option></select></label>':''}</div><p class="hint" id="newUserHelp">يُطلب من المستخدم تغيير كلمة المرور المؤقتة عند أول دخول. دور مستشار القطاع وظيفة مستقلة ولا يحتاج قسمًا. لتغيير صلاحية مستخدم موجود استخدم زر تغيير الصلاحية في القائمة.</p><button class="btn" type="submit">حفظ</button></form>`);
  if(fresh&&$('newUserRole')){
    const syncDepartment=()=>{
      const independent=$('newUserRole').value==='advisor';
      $('newUserDepartmentField').hidden=independent;
      $('newUserDepartment').disabled=independent;
      $('newUserHelp').textContent=independent?
        'مستشار القطاع وظيفة مستقلة لا تتبع أي قسم، وله صلاحيات المدير العام باستثناء إضافة الحسابات، ويمكن تعيينه من المدير العام فقط.':
        'الموظف ورئيس القسم يرتبطان بقسم. لا يمكن تعيين رئيسين للقسم نفسه.';
    };
    $('newUserRole').addEventListener('change',syncDepartment);
    syncDepartment();
  }
  $('userForm').onsubmit=request(async e=>{
    e.preventDefault();const data=new FormData(e.currentTarget);
    let payload={full_name:data.get('full_name')};
    if(fresh){
      payload.email=data.get('email');
      payload.role=data.get('role')||'employee';
      payload.department_id=payload.role==='advisor'?null:Number(data.get('department_id'));
      payload.temporary_password=data.get('temporary_password');
      await api('/users',{method:'POST',data:payload});
    }else{
      if(isExecutive(state.me.role)&&!advisorEdit)payload.department_id=Number(data.get('department_id'));
      payload.active=data.get('active')==='true';
      if(data.get('temporary_password'))payload.temporary_password=data.get('temporary_password');
      await api('/users/'+u.id,{method:'PATCH',data:payload});
    }
    closeModal();await loadAll();render();toast('تم حفظ بيانات المستخدم');
  });
}

function changeEmailModal(){
 modal('تغيير البريد الإلكتروني',`<p class="note">البريد الإلكتروني هو اسم الدخول إلى المنصة. لن تُرسل أي رسائل أو رموز تحقق إلى البريد الخارجي، ويلزم إدخال كلمة المرور الحالية لتأكيد التغيير.</p><form id="changeEmailForm" class="form-stack"><div class="form-field"><label>البريد الحالي</label><input class="input" value="${safe(state.me.email)}" disabled></div>${field('new_email','البريد الإلكتروني الجديد','email','','required maxlength="200" autocomplete="email"')}${field('current_password','كلمة المرور الحالية للتأكيد','password','','required autocomplete="current-password"')}<button class="btn" type="submit">حفظ البريد الجديد</button></form>`);
 $('changeEmailForm').onsubmit=request(async e=>{e.preventDefault();const btn=e.currentTarget.querySelector('button');btn.disabled=true;try{const f=new FormData(e.currentTarget);const r=await api('/me/email',{method:'PATCH',data:{new_email:f.get('new_email'),current_password:f.get('current_password')}});state.me=r.user;closeModal();render();toast('تم تحديث البريد الإلكتروني. استخدم البريد الجديد في تسجيل الدخول القادم.')}finally{btn.disabled=false}});
}
function updateMailBadge(){document.querySelectorAll('[data-page="inbox"]').forEach(btn=>{const old=btn.querySelector('[data-mail-badge]');old?.remove();if(state.mailUnread){const span=document.createElement('b');span.className='mail-badge';span.dataset.mailBadge='';span.textContent=state.mailUnread;btn.appendChild(span)}})}
async function refreshMailBadge(){if(!state.me)return;const r=await api('/inbox/unread');state.mailUnread=r.unread_threads;updateMailBadge()}
async function renderInbox(){renderShell('البريد الوارد',`<div class="page-head"><div><h2>المراسلات الداخلية</h2><p>رسائل خاصة داخل «إنجاز» فقط؛ لا تُرسل عبر Outlook أو البريد الخارجي.</p></div><div class="action-row"><button class="btn" id="composeMailBtn">＋ رسالة جديدة</button><button class="btn secondary" id="refreshMailBtn">تحديث ⟳</button></div></div><div class="mail-tabs" role="group" aria-label="صناديق البريد"><button class="mail-tab ${state.mailFolder==='inbox'?'active':''}" data-mail-folder="inbox">البريد الوارد</button><button class="mail-tab ${state.mailFolder==='sent'?'active':''}" data-mail-folder="sent">المرسل</button></div><div id="mailContent" class="panel"><p class="hint">جارٍ قراءة المراسلات...</p></div><p class="footer-note">البريد الداخلي خاص بطرفَي المراسلة. لا يمتلك المدير العام صلاحية الاطلاع على رسائل الآخرين. الرسائل محفوظة داخل قاعدة بيانات المنصة ولا تُرسل إلى أي عنوان بريد خارجي.</p>`);
 $('composeMailBtn').onclick=()=>composeInternalMail();$('refreshMailBtn').onclick=request(async()=>{await reloadInternalMail();toast('تم تحديث البريد')});
 document.querySelectorAll('[data-mail-folder]').forEach(btn=>btn.onclick=request(async()=>{state.mailFolder=btn.dataset.mailFolder;state.mailSelected=null;state.mailThread=null;await renderInbox()}));
 try{await reloadInternalMail()}catch(e){if($('mailContent'))$('mailContent').innerHTML=`<p class="error">${safe(e.message)}</p>`}
}
async function reloadInternalMail(){
 const folder=state.mailFolder;
 const [rows,contacts,unread]=await Promise.all([api('/inbox?folder='+encodeURIComponent(folder)),api('/inbox/contacts'),api('/inbox/unread')]);
 if(state.page!=='inbox'||folder!==state.mailFolder)return;
 state.mailThreads=rows.threads;state.mailContacts=contacts;state.mailUnread=unread.unread_threads;updateMailBadge();
 if(state.mailSelected&& !state.mailThreads.some(x=>x.id===state.mailSelected)){state.mailSelected=null;state.mailThread=null}
 drawInternalMail();
}
function drawInternalMail(){
 const holder=$('mailContent');if(!holder)return;
 const rows=state.mailThreads||[];const selected=state.mailThread;
 holder.innerHTML=`<div class="mail-layout"><div class="mail-index"><div class="mail-list-top"><b>${state.mailFolder==='inbox'?'الرسائل الواردة':'الرسائل المرسلة'} (${rows.length})</b><input id="mailSearch" class="input" placeholder="بحث بالاسم أو الموضوع" aria-label="بحث المراسلات"></div><div id="mailList" class="mail-list"></div></div><section class="mail-reader" id="mailReader">${selected?mailReaderHtml(selected):'<div class="mail-blank"><div class="mail-blank-icon">✉</div><b>اختر رسالة لعرض تفاصيلها</b><p class="hint">تُحفظ الرسائل والردود بالتسلسل الزمني داخل المنصة.</p></div>'}</section></div>`;
 const redrawList=()=>{const needle=($('mailSearch')?.value||'').toLowerCase().trim();const filtered=rows.filter(r=>(r.subject+' '+(r.other?.name||'')+' '+(r.other?.department||'')).toLowerCase().includes(needle));
 $('mailList').innerHTML=filtered.length?filtered.map(r=>`<button class="mail-list-item ${state.mailSelected===r.id?'selected':''} ${r.unread_count?'unread':''}" data-internal-thread="${r.id}"><span class="mail-list-item-top"><b>${safe(r.other?.name||'حساب غير متاح')}</b>${r.unread_count?`<span class="mail-count">${r.unread_count}</span>`:''}</span><strong>${safe(r.subject)}</strong><span class="mail-snippet">${safe(r.last_body)}</span><small>${fmt(r.updated_at)} · ${safe(r.other?.department||'')}</small></button>`).join(''):'<div class="mail-no-messages">لا توجد مراسلات مطابقة</div>';
 document.querySelectorAll('[data-internal-thread]').forEach(btn=>btn.onclick=request(async()=>openInternalMail(Number(btn.dataset.internalThread))));};
 $('mailSearch').oninput=redrawList;redrawList();bindMailReply();
}
function mailReaderHtml(data){const th=data.thread;return `<header class="mail-reader-head"><div><h3>${safe(th.subject)}</h3><p>مراسلة مع: <b>${safe(th.other?.name||'حساب غير متاح')}</b> · ${safe(th.other?.department||'')}</p></div><span class="pill done">مراسلة داخلية</span></header><div class="mail-letter-scroll">${data.messages.map(m=>`<div class="mail-letter ${m.sender_id===state.me.id?'mine':''}"><div class="mail-letter-head"><b>${safe(m.sender)}</b><time>${fmt(m.created_at)}</time></div><div class="mail-letter-body">${safe(m.body)}</div><small>${m.sender_id===state.me.id?m.read_at?'تمت القراءة':'تم الإرسال':'واردة'}</small></div>`).join('')}</div><form id="replyInternalMail" class="mail-reply"><label class="form-field">الرد على الرسالة<textarea name="body" class="input" rows="4" required maxlength="6000" placeholder="اكتب ردك هنا..."></textarea></label><button type="submit" class="btn">إرسال الرد</button></form>`}
function bindMailReply(){const form=$('replyInternalMail');if(!form||!state.mailSelected)return;form.onsubmit=request(async e=>{e.preventDefault();const btn=e.currentTarget.querySelector('button');btn.disabled=true;try{const body=new FormData(e.currentTarget).get('body');await api(`/inbox/${state.mailSelected}/reply`,{method:'POST',data:{body}});await reloadInternalMail();await openInternalMail(state.mailSelected);toast('تم إرسال الرد داخل المنصة')}finally{btn.disabled=false}})}
async function openInternalMail(id){state.mailSelected=id;const data=await api('/inbox/'+id);if(state.page!=='inbox'||state.mailSelected!==id)return;state.mailThread=data;await api(`/inbox/${id}/read`,{method:'POST'});const [newRows,unread]=await Promise.all([api('/inbox?folder='+state.mailFolder),api('/inbox/unread')]);if(state.page!=='inbox'||state.mailSelected!==id)return;state.mailThreads=newRows.threads;state.mailUnread=unread.unread_threads;updateMailBadge();await pollNotifications(true);drawInternalMail();}
async function composeInternalMail(){
 const contacts=state.mailContacts.length?state.mailContacts:await api('/inbox/contacts');
 if(!contacts.length){toast('لا يوجد موظفون نشطون آخرون لإرسال المراسلة');return}
 modal('إنشاء رسالة داخلية',`<form id="composeMailForm" class="form-stack"><p class="note">هذه المراسلة تصل إلى صندوق وارد الموظف داخل تطبيق إنجاز فقط؛ لا يوجد إرسال بريد إلكتروني خارجي.</p><label class="form-field">إلى الموظف<select name="recipient_id" class="input" required>${contacts.map(u=>`<option value="${u.id}">${safe(u.name)} — ${safe(u.department)}</option>`).join('')}</select></label>${field('subject','موضوع الرسالة','text','','required maxlength="180"')}<label class="form-field">نص الرسالة<textarea name="body" class="input" rows="7" required maxlength="6000" placeholder="نص الرسالة..." ></textarea></label><button type="submit" class="btn">إرسال داخل المنصة</button></form>`);
 $('composeMailForm').onsubmit=request(async e=>{e.preventDefault();const btn=e.currentTarget.querySelector('button');btn.disabled=true;try{const f=new FormData(e.currentTarget);const r=await api('/inbox',{method:'POST',data:{recipient_id:Number(f.get('recipient_id')),subject:f.get('subject'),body:f.get('body')}});closeModal();state.mailFolder='sent';state.mailSelected=r.id;state.mailThread=null;await renderInbox();await openInternalMail(r.id);toast('تم إرسال الرسالة إلى البريد الداخلي')}finally{btn.disabled=false}});
}

function changePasswordModal(mandatory){modal('تغيير كلمة المرور',`<p class="note">${mandatory?'يجب تغيير كلمة المرور المؤقتة قبل تنفيذ أي تعديل في التطبيق.':'غيّر كلمة المرور الخاصة بحسابك.'}</p><form id="passwordForm" class="form-stack">${field('old_password','كلمة المرور الحالية','password','','required autocomplete="current-password"')}${field('new_password','كلمة المرور الجديدة (12 حرفًا على الأقل)','password','','required minlength="12" autocomplete="new-password"')}<button class="btn">تحديث كلمة المرور</button></form>`);if(mandatory){$('closeModal').onclick=()=>{closeModal();toast('يجب تغيير كلمة المرور المؤقتة')}}$('passwordForm').onsubmit=request(async e=>{e.preventDefault();const f=new FormData(e.currentTarget);await api('/auth/change-password',{method:'POST',data:{old_password:f.get('old_password'),new_password:f.get('new_password')}});state.me.must_change_password=false;closeModal();toast('تم تحديث كلمة المرور')});}
async function loadActivity(filters={}){const qs=new URLSearchParams();Object.entries(filters).forEach(([k,v])=>{if(v)qs.set(k,v)});state.activity=await api('/activity?'+qs.toString())}
function activitySummary(rows){const online=new Set(rows.filter(a=>a.status==='online').map(a=>a.user_id)).size;return `<div class="cards"><div class="stat"><span>جلسات الاتصال</span><div class="metric">${rows.length}</div><small>ضمن التصفية الحالية</small></div><div class="stat"><span>الموظفون أونلاين</span><div class="metric">${online}</div><small>إشارات آخر 75 ثانية</small></div><div class="stat"><span>إجمالي الاتصال المرصود</span><div class="metric">${Math.round(rows.reduce((v,r)=>v+r.duration_minutes,0))}</div><small>دقيقة تقريبية</small></div><div class="stat"><span>إجمالي النشاط المرصود</span><div class="metric">${Math.round(rows.reduce((v,r)=>v+r.active_minutes,0))}</div><small>دقيقة تقريبية</small></div></div>`}
async function renderActivity(){renderShell('سجل الدخول والنشاط',`<div class="page-head"><div><h2>متابعة الجلسات وحالة الاتصال</h2><p>سجلات مركزية بوقت الخادم، قابلة للتصفية والتصدير.</p></div><button id="activityReload" class="btn secondary">تحديث ⟳</button></div><div class="panel"><div class="report-filters"><label>الموظف<select id="activityUser" class="input"><option value="">جميع الموظفين</option>${state.users.map(u=>`<option value="${u.id}">${safe(u.name)}</option>`).join('')}</select></label><label>من تاريخ<input id="activityStart" type="date" class="input"></label><label>إلى تاريخ<input id="activityEnd" type="date" class="input"></label><button id="activityApply" class="btn secondary">تطبيق التصفية</button><button id="activityExport" class="btn">تصدير CSV</button></div></div><div id="activityResult" style="margin-top:15px"><div class="panel empty">جارٍ تحميل سجل الجلسات...</div></div><p class="footer-note">أونلاين: آخر إشارة خلال 75 ثانية. الفترات تقريبية، والنشاط المرصود من تفاعل المتصفح المبلّغ عنه، ولا يمثل ساعات العمل أو إنتاجية الموظف. التقارير تظهر حسب صلاحيات المستخدم.</p>`);$('activityReload').onclick=()=>refreshAct();$('activityApply').onclick=()=>refreshAct();$('activityExport').onclick=()=>{const p=getActFilter();window.location.href='/api/activity/export?'+new URLSearchParams(Object.entries(p).filter(([k,v])=>v)).toString()};await refreshAct();}
function getActFilter(){return{user_id:$('activityUser')?.value||'',start:$('activityStart')?.value||'',end:$('activityEnd')?.value||''}}
async function refreshAct(){try{let p=getActFilter();await loadActivity(p);$('activityResult').innerHTML=`${activitySummary(state.activity)}<section class="panel"><h3>السجل التاريخي</h3><div class="table-wrap"><table class="table"><thead><tr><th>الموظف</th><th>القسم</th><th>الدخول</th><th>آخر ظهور</th><th>الخروج</th><th>الاتصال</th><th>النشاط</th><th>الحالة</th></tr></thead><tbody>${state.activity.map(a=>`<tr><td>${safe(a.name)}</td><td>${safe(a.department)}</td><td>${fmt(a.login_at)}</td><td>${fmt(a.last_seen)}</td><td>${fmt(a.logout_at)}</td><td>${a.duration_minutes} د</td><td>${a.active_minutes} د</td><td><span class="pill ${a.status==='online'?'done':'offline'}"><span class="dot ${a.status==='online'?'':'offline'}"></span>${a.status==='online'?'أونلاين':'أوفلاين'}</span></td></tr>`).join('')||'<tr><td colspan="8">لا توجد سجلات مطابقة</td></tr>'}</tbody></table></div></section>`}catch(e){toast(e.message)}}
async function renderAudit(){renderShell('سجل الإجراءات',`<div class="page-head"><div><h2>سجل التدقيق</h2><p>أبرز عمليات إنشاء وتعديل المستخدمين والمهام والمراسلات.</p></div></div><div id="auditRows" class="panel">جارٍ التحميل...</div>`);try{const rows=await api('/audit');$('auditRows').innerHTML=`<div class="table-wrap"><table class="table"><thead><tr><th>التاريخ</th><th>معرف المنفّذ</th><th>الإجراء</th><th>التفاصيل</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${fmt(r.created_at)}</td><td>${r.actor_id||'—'}</td><td>${safe(r.action)}</td><td>${safe(r.details)}</td></tr>`).join('')}</tbody></table></div>`}catch(e){toast(e.message)}}
function startBackground(){stopBackground();state.lastInteraction=Date.now();for(const ev of ['click','keydown','input','scroll'])document.addEventListener(ev,trackInteraction,{passive:true});const pulse=()=>{if(state.me){const active=!document.hidden&&Date.now()-state.lastInteraction<120000;api('/activity/pulse',{method:'POST',data:{active}}).catch(()=>{})}};pulse();state.heartbeatTimer=setInterval(pulse,25000);state.presenceTimer=setInterval(()=>{if(state.page==='dashboard'&&!document.hidden)refreshPresence().catch(()=>{})},30000);state.mailTimer=setInterval(()=>{if(!document.hidden)pollNotifications().catch(()=>{})},20000);pollNotifications(true).catch(()=>{})}
function trackInteraction(){state.lastInteraction=Date.now()}
function stopBackground(){if(state.chatTimer)clearInterval(state.chatTimer);if(state.heartbeatTimer)clearInterval(state.heartbeatTimer);if(state.presenceTimer)clearInterval(state.presenceTimer);if(state.mailTimer)clearInterval(state.mailTimer);if(state.discussionTimer)clearInterval(state.discussionTimer);if(state.noticeTimer)clearInterval(state.noticeTimer);state.chatTimer=state.heartbeatTimer=state.presenceTimer=state.mailTimer=state.discussionTimer=state.noticeTimer=null;for(const ev of ['click','keydown','input','scroll'])document.removeEventListener(ev,trackInteraction)}
init();
