import copy,importlib.util,json,os,sys,tempfile,time
from pathlib import Path
from eta import check_eta
HERE=Path(__file__).resolve().parent

class Fake:
    github_configured=False
    events=[]
    def __init__(self,*a,**kw):pass
    def create_issue(self,title,body):self.events.append(('create',title,body));return 42
    def comment(self,*a):self.events.append(('comment',a))
    def close(self,*a):self.events.append(('close',a))

def main():
    tests=[]
    with tempfile.TemporaryDirectory(dir=str(HERE),prefix='isolated_eta_test_') as folder:
        p=Path(folder);now=100000.0
        lc=dict(name='batch-job',enabled=True,completion_files=[str(p/'DONE')],eta_reminder=dict(
            enabled=True,run_key='example-run',
            progress_path=str(p/'progress.json'),active_path=str(p/'ACTIVE.json'),failure_path=str(p/'FAILED.json')))
        def setup(processed=80000,skipped=6):
            Fake.events=[]
            (p/'ACTIVE.json').write_text(json.dumps(dict(time=now)))
            (p/'progress.json').write_text(json.dumps(dict(schema=1,time=now,total=100000,completed=processed)))
            for f in ['DONE','FAILED.json']:
                if (p/f).exists():(p/f).unlink()
            return {'eta_reminders':{'batch-job':dict(run_key='example-run',samples=[[now-900,processed-12000]],notified=False)}}
        s=setup(20000);check_eta(lc,s,Fake(),now=now);assert not Fake.events;tests.append('quiet_above_one_hour')
        s=setup();saved=[];r=check_eta(lc,s,Fake(),now=now,persist=lambda:saved.append(copy.deepcopy(s)));assert r['notified'] and len(Fake.events)==1 and saved;assert 'batch-job' in Fake.events[0][1] and 'embedding' not in Fake.events[0][2];tests.append('generic_notification_and_persist')
        check_eta(lc,json.loads(json.dumps(s)),Fake(),now=now+300);assert len(Fake.events)==1;tests.append('restart_dedup')
        s=setup();r=check_eta(lc,s,Fake(),now=now,dry_run=True);assert r['status']=='would_notify' and not Fake.events;tests.append('dry_run_no_send')
        s=setup();(p/'ACTIVE.json').write_text(json.dumps(dict(time=now-91)));r=check_eta(lc,s,Fake(),now=now);assert r['status']=='stale_heartbeat' and not Fake.events;tests.append('stale_heartbeat_suppression')
        s=setup();q=json.loads((p/'progress.json').read_text());q['time']=now-301;(p/'progress.json').write_text(json.dumps(q));r=check_eta(lc,s,Fake(),now=now);assert r['status']=='stale_progress' and not Fake.events;tests.append('stale_progress_suppression')
        s=setup();q=json.loads((p/'progress.json').read_text());q['completed']=q['total']+1;(p/'progress.json').write_text(json.dumps(q));r=check_eta(lc,s,Fake(),now=now);assert r['status']=='invalid_or_unavailable_progress';tests.append('invalid_counters_suppression')
        s=setup();(p/'FAILED.json').write_text('{}');r=check_eta(lc,s,Fake(),now=now);assert r['status']=='failed' and not Fake.events;tests.append('failure_suppression')
        s=setup();(p/'DONE').write_text('{}');r=check_eta(lc,s,Fake(),now=now);assert r['status']=='processing_complete' and not Fake.events;tests.append('completion_suppression')
        s=setup(100000);r=check_eta(lc,s,Fake(),now=now);assert r['status']=='processing_complete' and not Fake.events;tests.append('completed_job_quiet')
        s=setup();s['eta_reminders']['batch-job']['samples']=[];r=check_eta(lc,s,Fake(),now=now);assert r['status']=='warming_up_or_no_progress';tests.append('minimum_15_min_window')
        s=setup();s['eta_reminders']['batch-job']['samples']=[[now-900,99000]];r=check_eta(lc,s,Fake(),now=now);assert r['status']=='warming_up_or_no_progress';tests.append('regression_reset')
        class Failed(Fake):
            def create_issue(self,*a):raise TimeoutError()
        s=setup();r=check_eta(lc,s,Failed(),now=now);assert not r['notified'] and r['status']=='notification_failed_retry';tests.append('delivery_failure_retries')
        s=setup();check_eta(lc,s,Fake(),now=now);recovery_body=Fake.events[0][2]
        class Recovered(Fake):
            github_configured=True
            repo='example/private'
            def _api(self,*a):return [dict(number=77,body=recovery_body)]
        s=setup();r=check_eta(lc,s,Recovered(),now=now);assert r['notified'] and r['issue_number']==77 and not Fake.events;tests.append('recover_remote_delivery_without_duplicate')
        s=setup();disabled=copy.deepcopy(lc);disabled['enabled']=False;check_eta(disabled,s,Fake(),now=now);assert not Fake.events;tests.append('disabled_target_quiet')
        s=setup();custom=copy.deepcopy(lc);custom['eta_reminder']['threshold_seconds']=1200
        r=check_eta(custom,s,Fake(),now=now);assert r['status']=='tracking' and not Fake.events;tests.append('configurable_threshold')
        s=setup();custom=copy.deepcopy(lc);custom['eta_reminder'].update(window_seconds=300,min_window_seconds=60,unit='files')
        s['eta_reminders']['batch-job']['samples']=[[now-60,79000]]
        r=check_eta(custom,s,Fake(),now=now);assert r['notified'] and 'files/小时' in Fake.events[0][2];tests.append('configurable_window_and_unit')
        s=setup();custom=copy.deepcopy(lc);custom['eta_reminder'].pop('active_path');custom['eta_reminder'].pop('failure_path')
        r=check_eta(custom,s,Fake(),now=now);assert r['notified'];tests.append('optional_heartbeat_and_failure_marker')
        s=setup();s['eta_reminders']['batch-job']['samples']=[[now-7200,0]]
        assert check_eta(lc,s,Fake(),now=now)['status']=='warming_up_or_no_progress';tests.append('stale_history_not_used')
        s=setup();s['eta_reminders']['batch-job']['total']=90000
        assert check_eta(lc,s,Fake(),now=now)['status']=='warming_up_or_no_progress';tests.append('changed_total_resets_samples')
        s=setup();custom=copy.deepcopy(lc);custom['eta_reminder']['run_key']='new-run'
        assert check_eta(custom,s,Fake(),now=now)['status']=='warming_up_or_no_progress';tests.append('new_run_resets_samples')
        s=setup();custom=copy.deepcopy(lc);custom['eta_reminder']['threshold_seconds']=-1
        assert check_eta(custom,s,Fake(),now=now)['status']=='invalid_or_unavailable_progress';tests.append('invalid_config_suppressed')
        s=setup();q=json.loads((p/'progress.json').read_text());q['time']=float('nan');(p/'progress.json').write_text(json.dumps(q))
        assert check_eta(lc,s,Fake(),now=now)['status']=='invalid_or_unavailable_progress';tests.append('nonfinite_timestamp_suppressed')
        s=setup();custom=copy.deepcopy(lc);custom['name']='another-job'
        s['eta_reminders']['another-job']=copy.deepcopy(s['eta_reminders']['batch-job'])
        r=check_eta(custom,s,Recovered(),now=now);assert r['issue_number']==42 and Fake.events;tests.append('remote_dedup_isolates_targets')
        # Exercise the real patched main with an isolated config and Fake notifier.
        spec=importlib.util.spec_from_file_location('isolated_watch',HERE/'train_watch.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);m.Notifier=Fake
        monitored=p/'current.log';monitored.write_text('healthy\n')
        cfg=dict(logs=[dict(name='batch-job',enabled=True,path=str(monitored),stall_minutes=0,start_at='end'),dict(name='paused-training',enabled=False,path=str(p/'old.log'))])
        (p/'config.json').write_text(json.dumps(cfg));(p/'state.json').write_text(json.dumps(dict(active_rules={'paused-training:stall':{}},incident=dict(number=99),files={})))
        oldenv=os.environ.get('TRAIN_WATCH_DIR');oldargv=sys.argv[:]
        try:
            os.environ['TRAIN_WATCH_DIR']=str(p);sys.argv=['train_watch.py'];Fake.events=[];m.main()
            state=json.loads((p/'state.json').read_text());assert not state['active_rules'] and 'incident' not in state;assert [x[0] for x in Fake.events]==['comment','close'];tests.append('real_main_paused_stall_incident_cleanup')
            before=(p/'state.json').read_bytes();Fake.events=[];sys.argv=['train_watch.py','--dry-run'];m.main();assert (p/'state.json').read_bytes()==before and not Fake.events;tests.append('real_main_dry_run_preserves_state')
        finally:
            sys.argv=oldargv
            if oldenv is None:os.environ.pop('TRAIN_WATCH_DIR',None)
            else:os.environ['TRAIN_WATCH_DIR']=oldenv
    assert not Path(folder).exists()
    print('WATCHER_TESTS_PASS', len(tests), 'Fake Notifier; no network or email')

if __name__ == '__main__':
    main()
