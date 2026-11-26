"""Isolated registration workflow tests; no external email and no production database."""
import os
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import HTTPException
from fastapi.responses import Response

_ROOT=tempfile.TemporaryDirectory(prefix='enjaz-registration-')
os.environ['DATABASE_URL']='sqlite:///'+_ROOT.name+'/test.db'
os.environ['APP_ENV']='development'
os.environ['REGISTRATION_OTP_PEPPER']='unit-test-pepper-very-long-should-not-use-in-production'
os.environ['REGISTRATION_BASE_URL']='https://example.invalid'
from app import main as m  # noqa: E402: use throwaway SQLite before importing application

class RegistrationSecurityTests(unittest.TestCase):
    def setUp(self):
        m.Base.metadata.drop_all(m.engine)
        m.Base.metadata.create_all(m.engine)
        self.db=m.SessionLocal()
        self.general=m.Department(name='الإدارة العامة')
        self.team=m.Department(name='عمليات إنجاز')
        self.db.add_all([self.general,self.team])
        self.db.flush()
        self.db.add(m.OrganizationSetting(key='general_department_id',value=str(self.general.id)))
        self.director=m.User(full_name='Platform Director',email='director@example.invalid',
            password_hash=m.password_hasher.hash('StrongPass12345'),role='director',active=True)
        self.staff=m.User(full_name='Existing Employee',email='existing@example.invalid',
            password_hash=m.password_hasher.hash('StrongPass12345'),role='employee',
            department_id=self.team.id,active=True)
        self.db.add_all([self.director,self.staff]);self.db.commit()
        self.sent=[]
        self.enabled=patch.object(m,'registration_available',return_value=True)
        self.mailer=patch.object(m,'send_registration_email',side_effect=self.fake_send)
        self.enabled.start()
        self.mailer.start()
        self.client=SimpleNamespace(client=SimpleNamespace(host='203.0.113.15'))

    def tearDown(self):
        self.mailer.stop()
        self.enabled.stop()
        self.db.close()

    def fake_send(self,email,subject,body):
        self.sent.append((email,subject,body))

    def admin(self):
        return SimpleNamespace(user=self.director,user_id=self.director.id)

    def normal_user(self):
        return SimpleNamespace(user=self.staff,user_id=self.staff.id)

    def begin(self,email='new@example.invalid',name='New Enjaz User'):
        result=m.register_start(m.RegistrationStartBody(full_name=name,email=email),
                                request=self.client,db=self.db)
        self.assertTrue(result['ok'])
        return re.search(r'\b[0-9]{6}\b',self.sent[-1][2]).group()

    def verify(self,code,email='new@example.invalid'):
        return m.register_verify(m.RegistrationVerifyBody(email=email,code=code),db=self.db)

    def approved(self,role='employee',department_id=None):
        otp=self.begin()
        self.verify(otp)
        req=self.db.scalar(m.select(m.RegistrationRequest).where(
            m.RegistrationRequest.email=='new@example.invalid'))
        result=m.approve_registration(req.id,m.RegistrationApprovalBody(
            role=role,department_id=self.team.id if department_id is None else department_id),
            db=self.db,s=self.admin())
        return req,result

    def activation_token(self):
        body=self.sent[-1][2]
        match=re.search(r'#activate=([A-Za-z0-9_-]+)',body)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_registration_is_disabled_until_mail_and_pepper_are_configured(self):
        with patch.object(m,'registration_available',return_value=False):
            self.assertFalse(m.registration_config()['registration_enabled'])
            with self.assertRaises(HTTPException) as e:
                self.begin()
            self.assertEqual(e.exception.status_code,503)
            self.assertEqual(self.db.scalar(m.select(m.func.count(m.RegistrationRequest.id))),0)

    def test_otp_required_and_no_account_created_preapproval(self):
        code=self.begin()
        req=self.db.scalar(m.select(m.RegistrationRequest).where(
            m.RegistrationRequest.email=='new@example.invalid'))
        self.assertEqual(req.status,'otp_pending')
        self.assertIsNone(self.db.scalar(m.select(m.User).where(
            m.User.email=='new@example.invalid')))
        self.assertEqual(m.admin_registrations(db=self.db,s=self.admin())['pending'],0)
        response=self.verify(code)
        self.assertEqual(response['status'],'awaiting_approval')
        self.assertEqual(m.admin_registrations(db=self.db,s=self.admin())['pending'],1)
        self.assertIsNone(req.otp_digest)
        with self.assertRaises(HTTPException):
            self.verify(code)

    def test_wrong_code_five_attempts_locked_and_timed_expiry(self):
        code=self.begin()
        wrong='000000' if code!='000000' else '999999'
        for _ in range(5):
            with self.assertRaises(HTTPException) as err:
                self.verify(wrong)
            self.assertEqual(err.exception.status_code,400)
        with self.assertRaises(HTTPException) as err:
            self.verify(code)
        self.assertEqual(err.exception.status_code,429)
        req=self.db.scalar(m.select(m.RegistrationRequest).where(
            m.RegistrationRequest.email=='new@example.invalid'))
        req.otp_expires_at=m.NOW()-m.timedelta(minutes=1)
        self.db.commit()
        with self.assertRaises(HTTPException) as err:
            self.verify(code)
        self.assertEqual(err.exception.status_code,400)

    def test_resend_limit_and_cooldown(self):
        self.begin()
        with self.assertRaises(HTTPException) as err:
            m.register_resend(m.RegistrationEmailBody(email='new@example.invalid'),
                              request=self.client,db=self.db)
        self.assertEqual(err.exception.status_code,429)
        req=self.db.scalar(m.select(m.RegistrationRequest).where(
            m.RegistrationRequest.email=='new@example.invalid'))
        for _ in range(2):
            req.last_sent_at=m.NOW()-m.timedelta(minutes=2)
            self.db.commit()
            m.register_resend(m.RegistrationEmailBody(email=req.email),request=self.client,db=self.db)
        req.last_sent_at=m.NOW()-m.timedelta(minutes=2);self.db.commit()
        with self.assertRaises(HTTPException) as err:
            m.register_resend(m.RegistrationEmailBody(email=req.email),request=self.client,db=self.db)
        self.assertEqual(err.exception.status_code,429)
        self.assertEqual(len(self.sent),3)

    def test_existing_account_cannot_request_new_user(self):
        before=len(self.sent)
        result=m.register_start(m.RegistrationStartBody(full_name='Fake Entry',
            email='existing@example.invalid'),request=self.client,db=self.db)
        self.assertTrue(result['ok'])
        self.assertEqual(len(self.sent),before)
        self.assertIsNone(self.db.scalar(m.select(m.RegistrationRequest).where(
            m.RegistrationRequest.email=='existing@example.invalid')))

    def test_manager_only_review_and_reject(self):
        code=self.begin();self.verify(code)
        request=m.admin_registrations(db=self.db,s=self.admin())['items'][0]
        with self.assertRaises(HTTPException) as forbidden:
            m.admin_registrations(db=self.db,s=self.normal_user())
        self.assertEqual(forbidden.exception.status_code,403)
        with self.assertRaises(HTTPException) as forbidden:
            m.approve_registration(request['id'],m.RegistrationApprovalBody(
                role='employee',department_id=self.team.id),db=self.db,s=self.normal_user())
        self.assertEqual(forbidden.exception.status_code,403)
        response=m.reject_registration(request['id'],m.RegistrationRejectionBody(
            reason='بيانات الموظف لم يتم التأكد منها'),db=self.db,s=self.admin())
        self.assertEqual(response['status'],'rejected')
        self.assertEqual(m.admin_registrations(db=self.db,s=self.admin())['pending'],0)
        with self.assertRaises(HTTPException):
            m.approve_registration(request['id'],m.RegistrationApprovalBody(
                role='employee',department_id=self.team.id),db=self.db,s=self.admin())

    def test_approval_creates_inactive_user_and_one_time_activation(self):
        req,result=self.approved()
        self.assertEqual(result['status'],'approved')
        user=self.db.get(m.User,result['user_id'])
        self.assertFalse(user.active)
        self.assertEqual(user.role,'employee')
        self.assertEqual(user.department_id,self.team.id)
        with self.assertRaises(HTTPException) as err:
            m.login(m.LoginBody(email=user.email,password='some-secret'),
                response=Response(),db=self.db)
        self.assertEqual(err.exception.status_code,401)
        token=self.activation_token()
        completed=m.activate_registration(m.RegistrationActivationBody(
            token=token,password='NewPrivatePassword2026!'),db=self.db)
        self.assertEqual(completed['status'],'active')
        self.assertTrue(user.active)
        self.assertTrue(m.password_hasher.verify(user.password_hash,'NewPrivatePassword2026!'))
        self.assertIsNone(req.activation_digest)
        with self.assertRaises(HTTPException):
            m.activate_registration(m.RegistrationActivationBody(
                token=token,password='AnotherLongPass12345'),db=self.db)

    def test_activation_expiry_and_manager_resend(self):
        req,result=self.approved()
        old_token=self.activation_token()
        req.activation_expires_at=m.NOW()-m.timedelta(hours=1)
        self.db.commit()
        with self.assertRaises(HTTPException) as err:
            m.activate_registration(m.RegistrationActivationBody(
                token=old_token,password='VeryLongPassword1234'),db=self.db)
        self.assertEqual(err.exception.status_code,400)
        response=m.resend_registration_activation(req.id,db=self.db,s=self.admin())
        self.assertTrue(response['ok'])
        token=self.activation_token()
        self.assertNotEqual(token,old_token)
        with self.assertRaises(HTTPException):
            m.activate_registration(m.RegistrationActivationBody(
                token=old_token,password='VeryLongPassword1234'),db=self.db)
        self.assertEqual(m.activate_registration(m.RegistrationActivationBody(
            token=token,password='VeryLongPassword1234'),db=self.db)['status'],'active')

    def test_reject_director_and_invalid_department(self):
        code=self.begin();self.verify(code)
        req=self.db.scalar(m.select(m.RegistrationRequest).where(
            m.RegistrationRequest.email=='new@example.invalid'))
        for role,department in [('director',self.team.id),
                                ('head',self.general.id),
                                ('employee',999999),('advisor',self.team.id)]:
            with self.assertRaises(HTTPException):
                m.approve_registration(req.id,m.RegistrationApprovalBody(
                    role=role,department_id=department),db=self.db,s=self.admin())
        self.assertIsNone(self.db.scalar(m.select(m.User).where(
            m.User.email==req.email)))

    def test_only_one_head_per_department(self):
        self.approved(role='head')
        otp=self.begin('second@example.invalid','Second Team Member')
        self.verify(otp,'second@example.invalid')
        req=self.db.scalar(m.select(m.RegistrationRequest).where(
            m.RegistrationRequest.email=='second@example.invalid'))
        with self.assertRaises(HTTPException) as err:
            m.approve_registration(req.id,m.RegistrationApprovalBody(
                role='head',department_id=self.team.id),db=self.db,s=self.admin())
        self.assertEqual(err.exception.status_code,409)

    def test_sector_advisor_has_no_department(self):
        code=self.begin();self.verify(code)
        req=self.db.scalar(m.select(m.RegistrationRequest).where(
            m.RegistrationRequest.email=='new@example.invalid'))
        result=m.approve_registration(req.id,m.RegistrationApprovalBody(
            role='advisor',department_id=None),db=self.db,s=self.admin())
        user=self.db.get(m.User,result['user_id'])
        self.assertEqual(user.role,'advisor')
        self.assertIsNone(user.department_id)
        self.assertFalse(user.active)

    def test_email_delivery_probe_is_director_only_and_cannot_open_registration(self):
        self.assertFalse(m.registration_mail_ready())
        self.assertEqual(len(self.sent),0)
        with self.assertRaises(HTTPException) as forbidden:
            m.test_registration_email(request=self.client,db=self.db,s=self.normal_user())
        self.assertEqual(forbidden.exception.status_code,403)
        with self.assertRaises(HTTPException) as unavailable:
            m.test_registration_email(request=self.client,db=self.db,s=self.admin())
        self.assertEqual(unavailable.exception.status_code,503)
        with patch.object(m,'registration_mail_ready',return_value=True):
            result=m.test_registration_email(request=self.client,db=self.db,s=self.admin())
        self.assertTrue(result['ok'])
        self.assertEqual(self.sent[-1][0],self.director.email)
        self.assertNotIn('otp',self.sent[-1][2].lower())
        self.assertIsNone(self.db.scalar(m.select(m.RegistrationRequest.id).limit(1)))
        self.assertEqual(m.reg_hmac('static'),m.reg_hmac('static'))

    def test_email_delivery_probe_rate_limit(self):
        with patch.object(m,'registration_mail_ready',return_value=True):
            for _ in range(3):
                self.assertTrue(m.test_registration_email(
                    request=self.client,db=self.db,s=self.admin())['ok'])
            with self.assertRaises(HTTPException) as limit:
                m.test_registration_email(request=self.client,db=self.db,s=self.admin())
            self.assertEqual(limit.exception.status_code,429)
        self.assertEqual(len(self.sent),3)

    def test_mail_failure_leaves_account_inactive_for_resend(self):
        code=self.begin();self.verify(code)
        req=self.db.scalar(m.select(m.RegistrationRequest).where(
            m.RegistrationRequest.email=='new@example.invalid'))
        with patch.object(m,'send_registration_email',side_effect=RuntimeError('SMTP offline')):
            with self.assertRaises(HTTPException) as err:
                m.approve_registration(req.id,m.RegistrationApprovalBody(
                    role='employee',department_id=self.team.id),db=self.db,s=self.admin())
        self.assertEqual(err.exception.status_code,503)
        self.assertEqual(req.status,'approved')
        user=self.db.get(m.User,req.user_id)
        self.assertFalse(user.active)
        # Resend is throttled even if the previous SMTP delivery failed.
        with self.assertRaises(HTTPException) as cooldown:
            m.resend_registration_activation(req.id,db=self.db,s=self.admin())
        self.assertEqual(cooldown.exception.status_code,429)
        # Simulate time passing beyond the one-minute cooldown.
        req.activation_expires_at=m.NOW()+m.REGISTRATION_ACTIVATION_TTL-m.timedelta(seconds=70)
        self.db.commit()
        self.assertTrue(m.resend_registration_activation(req.id,db=self.db,s=self.admin())['ok'])
        self.assertIsNotNone(self.activation_token())

    def test_failed_otp_delivery_not_verified(self):
        with patch.object(m,'send_registration_email',side_effect=RuntimeError('SMTP unavailable')):
            with self.assertRaises(HTTPException) as err:
                self.begin()
        self.assertEqual(err.exception.status_code,503)
        req=self.db.scalar(m.select(m.RegistrationRequest).where(
            m.RegistrationRequest.email=='new@example.invalid'))
        self.assertEqual(req.status,'otp_pending')
        self.assertIsNone(req.email_verified_at)

if __name__=='__main__':
    unittest.main()
