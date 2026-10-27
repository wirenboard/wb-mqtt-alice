"""Durability/crash checks for the opt-in protected journal. No MQTT or actuation."""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def journal_module():
    path=Path(__file__).resolve().parents[1]/'wb/mqtt_alice/client/protected_journal.py'
    spec=importlib.util.spec_from_file_location('alice_protected_journal_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_durable_reserve_dedup_conflict_and_crash_never_make_a_replay_queue(tmp_path,journal_module):
    m=journal_module;path=tmp_path/'journal.sqlite3'
    with m.ProtectedJournal(path,enabled=True) as journal:
        first=journal.reserve('command','request-1',{'session_nonce':'fixture-session','desired_value':'closed'})
        assert first['created'] and first['record']['state']=='reserved'
        assert journal.status()['sqlite']==dict(journal_mode='delete',synchronous=3)
        assert not journal.reserve('command','request-1',{'session_nonce':'fixture-session','desired_value':'closed'})['created']
        with pytest.raises(m.Conflict):journal.reserve('command','request-1',{'desired_value':'open'})
        journal.dispatched('request-1',first['record']['digest'])
    with m.ProtectedJournal(path,enabled=True) as journal:
        assert journal.get('request-1')['state']=='unknown'
        assert not journal.reserve('command','request-1',{'session_nonce':'fixture-session','desired_value':'closed'})['created']
        with pytest.raises(m.Conflict):journal.finish('request-1','readback_confirmed',{'coils':'closed'})
        journal.finish('request-1','readback_confirmed',{'coils':'closed'},reconcile_ref='owner-readback:fixture')
        assert journal.get('request-1')['state']=='readback_confirmed'


def test_single_writer_and_common_capacity_do_not_evict_unknown_or_channel_results(tmp_path,journal_module):
    m=journal_module;path=tmp_path/'journal.sqlite3'
    with m.ProtectedJournal(path,enabled=True,max_records=3) as journal:
        with pytest.raises(m.Unavailable):m.ProtectedJournal(path,enabled=True)
        journal.reserve('command','unknown-command',{})
        journal.reserve('incident','incident-1',{'state':'eligible','raw_wet':True})
        row=journal.reserve('channel','incident-1-push',{'incident_id':'incident-1','kind':'push'})
        journal.dispatched('incident-1-push',row['record']['digest'])
        journal.finish('incident-1-push','accepted',{'http_status':200})
        with pytest.raises(m.CapacityBlocked):journal.reserve('command','new-command',{})
        assert journal.status()['capacity_blocked']
        assert journal.get('unknown-command')['state']=='reserved'
        assert journal.get('incident-1-push')['state']=='accepted'
        assert not journal.reserve('channel','incident-1-push',{'incident_id':'incident-1','kind':'push'})['created']
        assert journal.status()['record_count']==3


def test_opt_in_permissions_and_secret_exclusion(tmp_path,journal_module):
    m=journal_module;path=tmp_path/'journal.sqlite3'
    with pytest.raises(m.Unavailable):m.ProtectedJournal(path)
    assert not path.exists()
    with m.ProtectedJournal(path,enabled=True) as journal:
        with pytest.raises(m.JournalError):journal.reserve('channel','leaky',{'authorization':'fixture-private-value'})
        assert journal.status()['record_count']==0
        assert path.stat().st_mode&0o777==0o600
    assert b'fixture-private-value' not in path.read_bytes()


@pytest.mark.parametrize('stage',['reserved','dispatched'])
def test_actual_process_crash_preserves_commit_as_unknown_without_callback(tmp_path,journal_module,stage):
    import subprocess,sys
    m=journal_module;path=tmp_path/'crash.sqlite3'
    code='''import importlib.util,os,sys
s=importlib.util.spec_from_file_location('journal_under_test',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
j=m.ProtectedJournal(sys.argv[2],enabled=True)
r=j.reserve('command','crashed-request',{'desired_value':'closed'})
if sys.argv[3]=='dispatched':j.dispatched('crashed-request',r['record']['digest'])
os._exit(17)
'''
    result=subprocess.run([sys.executable,'-c',code,m.__file__,str(path),stage],capture_output=True,timeout=10)
    assert result.returncode==17
    with m.ProtectedJournal(path,enabled=True) as journal:
        assert journal.get('crashed-request')['state']=='unknown'
        assert journal.status()['record_count']==1
        assert not journal.reserve('command','crashed-request',{'desired_value':'closed'})['created']


def test_commit_failure_blocks_new_reserves_and_does_not_leave_false_success(tmp_path,journal_module):
    import sqlite3
    m=journal_module
    with m.ProtectedJournal(tmp_path/'disk.sqlite3',enabled=True) as journal:
        original=journal.connection
        class DiskFailure:
            def __getattr__(self,key):return getattr(original,key)
            def execute(self,sql,*args):
                if sql=='COMMIT':raise sqlite3.OperationalError('fixture disk I/O failure')
                return original.execute(sql,*args)
        journal.connection=DiskFailure()
        with pytest.raises(m.Unavailable):journal.reserve('command','disk-request',{})
        assert original.execute('SELECT count(*) FROM records').fetchone()[0]==0
        with pytest.raises(m.Unavailable):journal.reserve('command','later-request',{})


def test_byte_budget_rolls_back_and_corrupt_digest_is_not_reconciled(tmp_path,journal_module):
    import sqlite3
    m=journal_module;path=tmp_path/'limited.sqlite3'
    with m.ProtectedJournal(path,enabled=True,max_bytes=32768) as journal:
        with pytest.raises(m.CapacityBlocked):journal.reserve('command','too-big',{'padding':'x'*40000})
        assert journal.get('too-big') is None
        row=journal.reserve('command','known',{})
        with pytest.raises(m.Conflict):journal.finish('known','readback_confirmed',{})
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE records SET digest=? WHERE id='known'",('0'*64,))
    with pytest.raises(m.Unavailable):m.ProtectedJournal(path,enabled=True)
