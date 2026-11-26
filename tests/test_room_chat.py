"""Chat access, persistence and attachments in scheduled meetings/direct calls.

Runs only against a throwaway SQLite database. No real employees, LiveKit keys or network.
"""
import asyncio
import io
import os
import tempfile
import unittest
from types import SimpleNamespace

from fastapi import HTTPException
from starlette.datastructures import UploadFile

_db_root=tempfile.TemporaryDirectory(prefix='enjaz-roomchat-')
os.environ['DATABASE_URL']='sqlite:///'+_db_root.name+'/test.db'
os.environ['APP_ENV']='development'
from app import main as m  # noqa: E402

class InRoomChatTests(unittest.TestCase):
    def setUp(self):
        m.Base.metadata.drop_all(m.engine)
        m.Base.metadata.create_all(m.engine)
        self.db=m.SessionLocal()
        dept=m.Department(name='Team')
        self.db.add(dept);self.db.flush()
        self.organizer=m.User(full_name='Organizer',email='org@test.invalid',password_hash='unused',
                              role='director',active=True)
        self.guest=m.User(full_name='Guest',email='guest@test.invalid',password_hash='unused',
                          role='employee',department_id=dept.id,active=True)
        self.stranger=m.User(full_name='Stranger',email='no@test.invalid',password_hash='unused',
                             role='employee',department_id=dept.id,active=True)
        self.db.add_all([self.organizer,self.guest,self.stranger]);self.db.flush()
        self.meeting=m.VideoMeeting(title='Weekly sync',description='',minutes_text='',
                                    organizer_id=self.organizer.id,room_name='test-meeting-room',
                                    start_at=m.NOW(),duration_minutes=60,status='scheduled')
        self.db.add(self.meeting);self.db.flush()
        for u in (self.organizer,self.guest):
            self.db.add(m.VideoMeetingInvitation(meeting_id=self.meeting.id,user_id=u.id))
        self.call=m.DirectCall(initiator_id=self.organizer.id,room_name='test-direct-room',
                               mode='video',status='active',created_at=m.NOW())
        self.db.add(self.call);self.db.flush()
        self.db.add(m.DirectCallMember(call_id=self.call.id,user_id=self.organizer.id,
                                       status='accepted',invited_at=m.NOW(),accepted_at=m.NOW()))
        self.db.add(m.DirectCallMember(call_id=self.call.id,user_id=self.guest.id,
                                       status='accepted',invited_at=m.NOW(),accepted_at=m.NOW()))
        self.db.add(m.DirectCallMember(call_id=self.call.id,user_id=self.stranger.id,
                                       status='invited',invited_at=m.NOW()))
        self.db.commit()
        self.meeting_channel=f'meeting:{self.meeting.id}'
        self.call_channel=f'call:{self.call.id}'

    def tearDown(self):
        self.db.close()

    def session(self,user):
        return SimpleNamespace(user=user,user_id=user.id)

    def write(self,channel,user,text='Hello'):
        return m.post_message(m.MessageCreate(channel=channel,text=text),
                              db=self.db,s=self.session(user))

    def read(self,channel,user):
        return m.get_messages(channel=channel,after=0,db=self.db,s=self.session(user))

    def upload(self,channel,user,filename='memo.txt',payload=b'room note'):
        file=UploadFile(file=io.BytesIO(payload),filename=filename)
        return asyncio.run(m.post_message_with_files(channel=channel,text='Attached',
                           files=[file],db=self.db,s=self.session(user)))

    def test_meeting_chat_private_and_available_for_invitees(self):
        self.write(self.meeting_channel,self.organizer,'Private meeting message')
        self.assertEqual(self.read(self.meeting_channel,self.guest)[0]['text'],'Private meeting message')
        with self.assertRaises(HTTPException):
            self.read(self.meeting_channel,self.stranger)
        with self.assertRaises(HTTPException):
            self.write(self.meeting_channel,self.stranger)

    def test_meeting_archive_read_only_after_end(self):
        self.write(self.meeting_channel,self.guest)
        self.meeting.status='ended';self.db.commit()
        self.assertEqual(len(self.read(self.meeting_channel,self.guest)),1)
        with self.assertRaises(HTTPException) as e:
            self.write(self.meeting_channel,self.guest)
        self.assertEqual(e.exception.status_code,403)

    def test_meeting_early_writes_denied(self):
        self.meeting.start_at=m.NOW()+m.timedelta(days=3);self.db.commit()
        with self.assertRaises(HTTPException):
            self.write(self.meeting_channel,self.guest)

    def test_direct_call_requires_acceptance_and_cannot_mix_rooms(self):
        self.write(self.call_channel,self.organizer,'Private call message')
        self.assertEqual(self.read(self.call_channel,self.guest)[0]['text'],'Private call message')
        with self.assertRaises(HTTPException):
            self.read(self.call_channel,self.stranger)
        with self.assertRaises(HTTPException):
            self.write(self.call_channel,self.stranger)
        self.assertEqual(self.read(self.meeting_channel,self.guest),[])

    def test_direct_call_archive_and_leave_read_only(self):
        self.write(self.call_channel,self.guest)
        guest_member=self.db.scalar(m.select(m.DirectCallMember).where(
            m.DirectCallMember.call_id==self.call.id,
            m.DirectCallMember.user_id==self.guest.id))
        guest_member.status='left';self.db.commit()
        self.assertEqual(len(self.read(self.call_channel,self.guest)),1)
        with self.assertRaises(HTTPException):
            self.write(self.call_channel,self.guest)
        self.call.status='ended';self.db.commit()
        with self.assertRaises(HTTPException):
            self.write(self.call_channel,self.organizer)

    def test_attachments_are_scoped_to_room_participants(self):
        result=self.upload(self.meeting_channel,self.guest)
        self.assertEqual(result['files'],1)
        messages=self.read(self.meeting_channel,self.organizer)
        self.assertEqual(len(messages[0]['attachments']),1)
        attachment=messages[0]['attachments'][0]
        delivered=m.get_chat_file(attachment['id'],inline=False,
                                  db=self.db,s=self.session(self.organizer))
        self.assertEqual(delivered.body,b'room note')
        with self.assertRaises(HTTPException):
            m.get_chat_file(attachment['id'],inline=False,
                            db=self.db,s=self.session(self.stranger))
        self.meeting.status='ended';self.db.commit()
        with self.assertRaises(HTTPException):
            self.upload(self.meeting_channel,self.guest)
        self.assertEqual(len(self.read(self.meeting_channel,self.guest)),1)

    def test_bad_file_and_path_are_rejected(self):
        with self.assertRaises(HTTPException):
            self.upload(self.call_channel,self.organizer,'virus.exe',b'abc')
        with self.assertRaises(HTTPException):
            self.upload(self.call_channel,self.organizer,'../bad.txt',b'x')

if __name__=='__main__':
    unittest.main()
