import os
import tempfile
from pathlib import Path

_db=Path(tempfile.mkdtemp())/'test.db'
os.environ['DATABASE_URL']='sqlite:///'+str(_db)
os.environ['APP_ENV']='development'
os.environ['BOOTSTRAP_ADMIN_EMAIL']='admin@example.test'
os.environ['BOOTSTRAP_ADMIN_PASSWORD']='LongTestAdminPassword!2026'

from fastapi.testclient import TestClient
from app.main import app, SessionLocal, User, Department, password_hasher
from sqlalchemy import select

client=TestClient(app)


def login(email,password):
    response=client.post('/api/auth/login',json={'email':email,'password':password})
    assert response.status_code==200,response.text
    return response.json()['csrf']


def test_01_health_and_login():
    assert client.get('/healthz').status_code==200
    assert client.get('/api/users').status_code==401
    assert client.post('/api/auth/login',json={'email':'admin@example.test','password':'WRONG'}).status_code==401
    csrf=login('admin@example.test','LongTestAdminPassword!2026')
    assert client.get('/api/me').json()['user']['role']=='director'
    assert client.post('/api/users',json={}).status_code==403 # no csrf token
    res=client.post('/api/auth/change-password',headers={'X-CSRF-Token':csrf},json={'old_password':'LongTestAdminPassword!2026','new_password':'ChangedAdminPassword!2026'})
    assert res.status_code==200,res.text


def test_02_users_task_activity():
    csrf=login('admin@example.test','ChangedAdminPassword!2026')
    headers={'X-CSRF-Token':csrf}
    ds=client.get('/api/departments').json()
    dept1,dept2=ds[0]['id'],ds[1]['id']
    def new(name,email,dept,role='employee'):
        r=client.post('/api/users',headers=headers,json={'full_name':name,'email':email,'department_id':dept,'role':role,'temporary_password':'Temp!Password1234'})
        assert r.status_code==201,r.text
        return r.json()['id']
    e1=new('موظف متابعة','e1@sample.test',dept1)
    h1=new('رئيس المتابعة','h1@sample.test',dept1,'head')
    e2=new('موظف الهيكلة','e2@sample.test',dept2)
    t=client.post('/api/tasks',headers=headers,json={'title':'إعداد تقرير التحصيل','description':'تقرير أسبوعي','output':'تقرير','department_id':dept1,'assigned_to':e1,'due_date':'2026-12-30','priority':'high','steps':[{'title':'جمع البيانات','weight':40},{'title':'رفع التقرير','weight':60}]})
    assert t.status_code==201,t.text
    task=t.json()
    bad=client.post('/api/tasks',headers=headers,json={'title':'اختبار الأوزان','department_id':dept1,'assigned_to':e1,'due_date':'2026-12-30','steps':[{'title':'خطوة أولى','weight':50}]})
    assert bad.status_code==400
    assert client.get('/api/tasks').status_code==200
    r=client.post('/api/activity/pulse',headers=headers,json={'active':True});assert r.status_code==200
    r=client.get('/api/activity/export');assert r.status_code==200
    assert 'text/csv' in r.headers['content-type']
    assert 'اسم الموظف' in r.text
    # employee can see own task but not another department, cannot create employees
    empclient=TestClient(app)
    resp=empclient.post('/api/auth/login',json={'email':'e1@sample.test','password':'Temp!Password1234'})
    assert resp.status_code==200
    ecsrf=resp.json()['csrf']
    eh={'X-CSRF-Token':ecsrf}
    tasks=empclient.get('/api/tasks').json();assert len(tasks)==1 and tasks[0]['id']==task['id']
    assert empclient.post('/api/users',headers=eh,json={'full_name':'محاولة','email':'x@sample.test','temporary_password':'123456789012','department_id':dept1}).status_code==403
    assert empclient.get('/api/messages?channel=dept:'+str(dept2)).status_code==403
    # password change required before task changes
    assert empclient.post(f"/api/tasks/{task['id']}/steps/{task['steps'][0]['id']}",headers=eh,json={'done':True}).status_code==403
    assert empclient.post('/api/auth/change-password',headers=eh,json={'old_password':'Temp!Password1234','new_password':'EmployeeChanged!2026'}).status_code==200
    step1=task['steps'][0]['id'];step2=task['steps'][1]['id']
    assert empclient.post(f'/api/tasks/{task["id"]}/steps/{step1}',headers=eh,json={'done':True}).status_code==200
    updated=empclient.post(f'/api/tasks/{task["id"]}/steps/{step2}',headers=eh,json={'done':True}).json()
    assert updated['progress']==95 # final approval required
    assert empclient.post(f'/api/tasks/{task["id"]}/submit',headers=eh,json={'result_text':'رفع التقرير النهائي للمراجعة'}).json()['status']=='review'
    assert empclient.post(f'/api/tasks/{task["id"]}/decision',headers=eh,json={'decision':'approve'}).status_code==403
    assert client.post(f'/api/tasks/{task["id"]}/decision',headers=headers,json={'decision':'approve'}).json()['progress']==100
    # head sees department tasks but not another department, even via channel id
    headclient=TestClient(app)
    hc=headclient.post('/api/auth/login',json={'email':'h1@sample.test','password':'Temp!Password1234'}).json()['csrf']
    assert headclient.get('/api/tasks').status_code==200
    assert headclient.get('/api/activity?user_id='+str(e2)).json()==[]
    assert headclient.get('/api/messages?channel=dept:'+str(dept2)).status_code==403
    assert client.get('/api/activity?user_id='+str(e1)).status_code==200
    audit=client.get('/api/audit');assert audit.status_code==200 and len(audit.json())>4
    # logout revokes session
    assert empclient.post('/api/auth/logout',headers=eh).status_code==200
    assert empclient.get('/api/me').status_code==401
