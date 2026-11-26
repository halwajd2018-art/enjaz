"""Isolated direct-call API unit tests: no real credentials, media, or production DB."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from fastapi import HTTPException

_isolated_db = tempfile.TemporaryDirectory(prefix='enjaz-calls-')
os.environ['DATABASE_URL'] = 'sqlite:///' + _isolated_db.name + '/unit.db'
os.environ['APP_ENV'] = 'development'
from app import main as m  # noqa: E402: import after setting a throwaway SQLite DB

class DirectCallSecurityTests(unittest.TestCase):
    def setUp(self):
        m.Base.metadata.drop_all(m.engine)
        m.Base.metadata.create_all(m.engine)
        self.db = m.SessionLocal()
        d1 = m.Department(name='A')
        d2 = m.Department(name='B')
        self.db.add_all([d1,d2]); self.db.flush()
        self.owner=m.User(full_name='Owner',email='owner@test.invalid',role='director',
                          password_hash='unused',active=True)
        self.worker=m.User(full_name='Worker',email='worker@test.invalid',role='employee',
                           department_id=d1.id,password_hash='unused',active=True)
        self.peer=m.User(full_name='Peer',email='peer@test.invalid',role='employee',
                         department_id=d1.id,password_hash='unused',active=True)
        self.outsider=m.User(full_name='Outsider',email='outside@test.invalid',
                             role='employee',department_id=d2.id,
                             password_hash='unused',active=True)
        self.db.add_all([self.owner,self.worker,self.peer,self.outsider])
        self.db.commit()
        self.closed=[]
        m.video_available=lambda:True
        m.meeting_jwt=lambda room,user:'safe-test-jwt'
        m.close_livekit_room=lambda room,ids:self.closed.append((room,ids))

    def tearDown(self):
        self.db.close()

    def auth(self,user):
        return SimpleNamespace(user=user,user_id=user.id)

    def call(self, owner=None, guests=None, mode='video'):
        owner=owner or self.owner
        guests=guests or [self.worker.id]
        return m.start_direct_call(m.DirectCallCreateBody(invitee_ids=guests,mode=mode),
                                   db=self.db,s=self.auth(owner))

    def test_invitation_is_private_and_acceptance_required(self):
        call=self.call()
        cid=call['id']
        self.assertEqual(call['status'],'ringing')
        outsider_list=m.list_direct_calls(db=self.db,s=self.auth(self.outsider))
        self.assertEqual(len(outsider_list['calls']),0)
        incoming=m.list_direct_calls(db=self.db,s=self.auth(self.worker))
        self.assertEqual(len(incoming['incoming']),1)
        with self.assertRaises(HTTPException) as unauthorized:
            m.join_direct_call(cid,db=self.db,s=self.auth(self.outsider))
        self.assertEqual(unauthorized.exception.status_code,404)
        with self.assertRaises(HTTPException) as not_accepted:
            m.join_direct_call(cid,db=self.db,s=self.auth(self.worker))
        self.assertEqual(not_accepted.exception.status_code,409)
        m.accept_direct_call(cid,db=self.db,s=self.auth(self.worker))
        authorized=m.join_direct_call(cid,db=self.db,s=self.auth(self.worker))
        self.assertEqual(authorized['token'],'safe-test-jwt')
        self.assertEqual(authorized['mode'],'video')

    def test_decline_ends_one_to_one_call_and_denies_join(self):
        c=self.call(mode='audio')
        m.decline_direct_call(c['id'],db=self.db,s=self.auth(self.worker))
        mine=m.list_direct_calls(db=self.db,s=self.auth(self.owner))['calls'][0]
        self.assertEqual(mine['status'],'declined')
        with self.assertRaises(HTTPException):
            m.join_direct_call(c['id'],db=self.db,s=self.auth(self.owner))

    def test_group_invites_preserve_permissions(self):
        c=self.call(guests=[self.worker.id,self.peer.id],mode='audio')
        self.assertEqual(len(c['participants']),3)
        m.accept_direct_call(c['id'],db=self.db,s=self.auth(self.worker))
        m.decline_direct_call(c['id'],db=self.db,s=self.auth(self.peer))
        self.assertEqual(m.list_direct_calls(db=self.db,s=self.auth(self.owner))['calls'][0]['status'],'active')
        self.assertEqual(m.join_direct_call(c['id'],db=self.db,s=self.auth(self.owner))['mode'],'audio')
        with self.assertRaises(HTTPException):
            m.invite_direct_call(c['id'],m.DirectCallInviteBody(invitee_ids=[self.outsider.id]),
                                 db=self.db,s=self.auth(self.worker))
        m.leave_direct_call(c['id'],db=self.db,s=self.auth(self.worker))
        with self.assertRaises(HTTPException):
            m.join_direct_call(c['id'],db=self.db,s=self.auth(self.worker))

    def test_scope_prevents_cross_department_spam(self):
        with self.assertRaises(HTTPException) as error:
            self.call(owner=self.worker,guests=[self.outsider.id])
        self.assertEqual(error.exception.status_code,403)

    def test_ending_revokes_only_identities_issued_tokens(self):
        c=self.call(guests=[self.worker.id,self.peer.id])
        m.join_direct_call(c['id'],db=self.db,s=self.auth(self.owner))
        m.accept_direct_call(c['id'],db=self.db,s=self.auth(self.worker))
        m.join_direct_call(c['id'],db=self.db,s=self.auth(self.worker))
        m.end_direct_call(c['id'],db=self.db,s=self.auth(self.owner))
        self.assertEqual(len(self.closed),1)
        self.assertEqual(set(self.closed[0][1]),{self.owner.id,self.worker.id})
        self.assertEqual(m.list_direct_calls(db=self.db,s=self.auth(self.owner))['calls'][0]['status'],'ended')
        with self.assertRaises(HTTPException):
            m.join_direct_call(c['id'],db=self.db,s=self.auth(self.worker))

    def test_failed_room_closure_prevents_new_tokens(self):
        c=self.call()
        m.join_direct_call(c['id'],db=self.db,s=self.auth(self.owner))
        def fail(room, ids):
            raise RuntimeError('simulated LiveKit outage')
        m.close_livekit_room=fail
        with self.assertRaises(HTTPException) as failure:
            m.end_direct_call(c['id'],db=self.db,s=self.auth(self.owner))
        self.assertEqual(failure.exception.status_code,503)
        with self.assertRaises(HTTPException):
            m.join_direct_call(c['id'],db=self.db,s=self.auth(self.owner))
        m.close_livekit_room=lambda room,ids:self.closed.append((room,ids))
        self.assertEqual(m.end_direct_call(c['id'],db=self.db,s=self.auth(self.owner))['status'],'ended')

if __name__=='__main__':
    unittest.main()
