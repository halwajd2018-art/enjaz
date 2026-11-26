"""Tests for the director-managed permission matrix. No production DB or media."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from datetime import timedelta
from fastapi import HTTPException
from pydantic import ValidationError

_testdir=tempfile.TemporaryDirectory(prefix='enjaz-permissions-')
os.environ['DATABASE_URL']='sqlite:///'+_testdir.name+'/unit.db'
os.environ['APP_ENV']='development'
from app import main as m  # noqa: E402

class PermissionManagementTests(unittest.TestCase):
    def setUp(self):
        m.Base.metadata.drop_all(m.engine)
        m.Base.metadata.create_all(m.engine)
        self.db=m.SessionLocal()
        self.department=m.Department(name='Support')
        self.other_dept=m.Department(name='Outside')
        self.db.add_all([self.department,self.other_dept]);self.db.flush()
        self.director=m.User(full_name='Director',email='director@example.invalid',
                             role='director',password_hash='unused',active=True)
        self.head=m.User(full_name='Head',email='head@example.invalid',
                         role='head',department_id=self.department.id,
                         password_hash='unused',active=True)
        self.employee=m.User(full_name='Employee',email='employee@example.invalid',
                             role='employee',department_id=self.department.id,
                             password_hash='unused',active=True)
        self.friend=m.User(full_name='Friend',email='friend@example.invalid',
                           role='employee',department_id=self.department.id,
                           password_hash='unused',active=True)
        self.outside=m.User(full_name='Outside',email='outside@example.invalid',
                            role='employee',department_id=self.other_dept.id,
                            password_hash='unused',active=True)
        self.db.add_all([self.director,self.head,self.employee,self.friend,self.outside])
        self.db.commit()
        self.video_function=m.video_available
        m.video_available=lambda:True

    def tearDown(self):
        m.video_available=self.video_function
        self.db.close()

    def as_user(self,user):
        return SimpleNamespace(user=user,user_id=user.id)

    def save(self,who,decisions,reason='اختبار تغيير صلاحيات المستخدم'):
        return m.update_user_permissions(who.id,
                  m.PermissionOverridesUpdate(decisions=decisions,reason=reason),
                  db=self.db,s=self.as_user(self.director))

    def test_catalog_is_complete_and_defaults_are_backward_compatible(self):
        catalog=m.permission_catalog(db=self.db,s=self.as_user(self.director))
        self.assertGreaterEqual(len(catalog['permissions']),35)
        perms={row['code']:row for row in catalog['permissions']}
        self.assertTrue(perms['meetings.create']['editable'])
        self.assertTrue(perms['calls.start.video']['editable'])
        self.assertFalse(perms['permissions.manage']['editable'])
        self.assertFalse(perms['meetings.end']['editable'])
        self.assertFalse(perms['calls.end']['editable'])
        self.assertTrue(m.permission_allowed(self.db,self.employee,'calls.start.audio'))
        self.assertTrue(m.permission_allowed(self.db,self.employee,'calls.start.video'))
        self.assertTrue(m.permission_allowed(self.db,self.employee,'meetings.create'))
        self.assertFalse(m.permission_allowed(self.db,self.employee,'tasks.create'))

    def test_only_director_can_read_and_edit_other_users(self):
        with self.assertRaises(HTTPException) as denied:
            m.permission_catalog(db=self.db,s=self.as_user(self.employee))
        self.assertEqual(denied.exception.status_code,403)
        with self.assertRaises(HTTPException) as denied:
            m.get_user_permissions(self.head.id,db=self.db,s=self.as_user(self.employee))
        self.assertEqual(denied.exception.status_code,403)
        with self.assertRaises(HTTPException) as denied:
            m.update_user_permissions(self.employee.id,
                m.PermissionOverridesUpdate(decisions={'calls.start.audio':'deny'},reason='تقييد اتصال هذا الموظف'),
                db=self.db,s=self.as_user(self.head))
        self.assertEqual(denied.exception.status_code,403)
        own=m.my_permissions(db=self.db,s=self.as_user(self.employee))
        self.assertEqual(own['user_id'],self.employee.id)

    def test_audio_and_video_are_independent_and_enforced_by_server(self):
        self.save(self.employee,{'calls.start.audio':'deny'})
        self.assertFalse(m.permission_allowed(self.db,self.employee,'calls.start.audio'))
        self.assertTrue(m.permission_allowed(self.db,self.employee,'calls.start.video'))
        with self.assertRaises(HTTPException) as denied:
            m.start_direct_call(
                m.DirectCallCreateBody(invitee_ids=[self.friend.id],mode='audio'),
                db=self.db,s=self.as_user(self.employee))
        self.assertEqual(denied.exception.status_code,403)
        call=m.start_direct_call(m.DirectCallCreateBody(invitee_ids=[self.friend.id],mode='video'),
                                 db=self.db,s=self.as_user(self.employee))
        self.assertEqual(call['mode'],'video')

    def test_meeting_permission_revoke_restore_and_scope(self):
        self.save(self.employee,{'meetings.create':'deny'},reason='عدم إتاحة تنظيم الاجتماعات')
        request=m.MeetingCreateBody(title='Weekly check',
                start_at=m.NOW()+timedelta(minutes=30),
                duration_minutes=30,invitee_ids=[self.friend.id])
        with self.assertRaises(HTTPException) as denied:
            m.create_meeting(request,db=self.db,s=self.as_user(self.employee))
        self.assertEqual(denied.exception.status_code,403)
        self.save(self.employee,{'meetings.create':'inherit'})
        meeting=m.create_meeting(request,db=self.db,s=self.as_user(self.employee))
        self.assertEqual(meeting['organizer_id'],self.employee.id)
        forbidden=m.MeetingCreateBody(title='Outside team',
                start_at=m.NOW()+timedelta(minutes=30),
                duration_minutes=30,invitee_ids=[self.outside.id])
        with self.assertRaises(HTTPException) as denied:
            m.create_meeting(forbidden,db=self.db,s=self.as_user(self.employee))
        self.assertEqual(denied.exception.status_code,403)

    def test_sensitive_permissions_never_escalate_the_role(self):
        with self.assertRaises(HTTPException) as e:
            self.save(self.employee,{'users.create':'allow'})
        self.assertEqual(e.exception.status_code,422)
        with self.assertRaises(HTTPException) as e:
            self.save(self.employee,{'permissions.manage':'allow'})
        self.assertEqual(e.exception.status_code,422)
        with self.assertRaises(HTTPException) as e:
            self.save(self.employee,{'calls.end':'deny'})
        self.assertEqual(e.exception.status_code,422)
        self.assertFalse(m.permission_allowed(self.db,self.employee,'users.create'))
        self.assertTrue(m.permission_allowed(self.db,self.director,'permissions.manage'))

    def test_no_self_escalation_or_lockout_and_atomic_validation(self):
        with self.assertRaises(HTTPException) as e:
            self.save(self.director,{'meetings.create':'deny'})
        self.assertEqual(e.exception.status_code,403)
        with self.assertRaises(HTTPException):
            self.save(self.employee,{'calls.start.audio':'deny','audit.view':'allow'})
        self.assertTrue(m.permission_allowed(self.db,self.employee,'calls.start.audio'))
        with self.assertRaises(ValidationError):
            self.save(self.employee,{'calls.start.audio':'deny'},reason='قصير')
        self.assertTrue(m.permission_allowed(self.db,self.employee,'calls.start.audio'))

    def test_restoring_role_inheritance_clears_override_and_audits(self):
        saved=self.save(self.head,{'meetings.create':'deny','tasks.create':'deny'})
        self.assertEqual(saved['updated'],2)
        self.assertFalse(m.permission_allowed(self.db,self.head,'tasks.create'))
        self.assertFalse(m.permission_allowed(self.db,self.head,'meetings.create'))
        saved=self.save(self.head,{'tasks.create':'allow'})
        self.assertEqual(saved['permissions']['tasks.create']['decision'],'allow')
        self.assertTrue(m.permission_allowed(self.db,self.head,'tasks.create'))
        restored=self.save(self.head,{'tasks.create':'inherit'})
        self.assertEqual(restored['permissions']['tasks.create']['decision'],'inherit')
        self.assertTrue(m.permission_allowed(self.db,self.head,'tasks.create'))
        audit=self.db.scalars(m.select(m.Audit).where(
            m.Audit.action=='user_permissions_changed')).all()
        self.assertGreaterEqual(len(audit),3)
        self.assertTrue(any('reason=' in row.details for row in audit))

    def test_task_role_and_edit_scope_not_bypassed_by_overrides(self):
        self.save(self.head,{'users.edit':'allow'})
        self.assertTrue(m.permission_allowed(self.db,self.head,'users.edit'))
        with self.assertRaises(HTTPException) as e:
            m.edit_user(self.outside.id,m.UserEdit(full_name='Changed'),
                        db=self.db,s=self.as_user(self.head))
        self.assertEqual(e.exception.status_code,403)
        with self.assertRaises(HTTPException) as e:
            self.save(self.employee,{'tasks.create':'allow'})
        self.assertEqual(e.exception.status_code,422)

if __name__=='__main__':
    unittest.main()
