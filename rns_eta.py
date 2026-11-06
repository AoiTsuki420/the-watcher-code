"""One-shot remaining-time reminder through the existing Watcher notifier."""
import datetime
import json
import math
import os
import time


def check_eta(lc, state, notifier, now=None, dry_run=False, persist=None, logger=print):
    cfg=lc.get('eta_reminder') or {}
    if not lc.get('enabled') or not cfg.get('enabled'):return
    now=time.time() if now is None else now
    rec=state.setdefault('eta_reminders',{}).setdefault(lc['name'],{})
    if rec.get('run_key')!=cfg['run_key']:
        rec.clear();rec.update(run_key=cfg['run_key'],samples=[],notified=False)
    if rec.get('notified'):return rec
    try:
        if any(os.path.isfile(p) for p in lc.get('completion_files',[])):
            rec['status']='processing_complete';return rec
        if os.path.isfile(cfg['failure_path']):
            rec['status']='failed';return rec
        with open(cfg['active_path']) as f:active=json.load(f)
        if not 0<=now-float(active['time'])<=90:
            rec['status']='stale_coordinator';return rec
        with open(cfg['progress_path']) as f:q=json.load(f)
        assert q['schema']==1 and q['total']==cfg['expected_total']
        assert all(type(q[k]) is int and q[k]>=0 for k in ['valid','skipped','pending','total'])
        assert q['valid']+q['skipped']+q['pending']==q['total']
        observed=float(q['time']);assert math.isfinite(observed)
        if not 0<=now-observed<=300:
            rec['status']='stale_progress';return rec
    except (OSError,ValueError,KeyError,TypeError,AssertionError) as exc:
        rec.update(status='invalid_or_unavailable_progress',error_type=type(exc).__name__);return rec
    if not q['pending']:
        rec['status']='processing_complete';return rec
    processed=q['valid']+q['skipped'];samples=rec.setdefault('samples',[])
    if samples and (observed<samples[-1][0] or processed<samples[-1][1]):samples[:]=[]
    if not samples or observed>samples[-1][0]:samples.append([observed,processed])
    elif processed!=samples[-1][1]:
        rec['status']='inconsistent_timestamp';return rec
    while len(samples)>2 and samples[1][0]<=observed-3600:samples.pop(0)
    del samples[:-128]
    seconds=observed-samples[0][0];done=processed-samples[0][1]
    rec.update(pending=q['pending'],valid=q['valid'],skipped=q['skipped'],last_progress=observed)
    if seconds<900 or done<=0:
        rec['status']='warming_up_or_no_progress';return rec
    rate=done/seconds;remaining=q['pending']/rate
    rec.update(status='tracking',rows_per_hour=rate*3600,remaining_seconds=remaining,window_seconds=seconds)
    if remaining>3600:return rec
    if dry_run:
        rec['status']='would_notify';return rec
    tag='<!-- watcher-eta:'+cfg['run_key']+' -->'
    finish=datetime.datetime.fromtimestamp(now+remaining,datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
    body=('三 Pod embedding 原序列读取、融合与池化任务预计接近结束。\n\n'
        '- 预计剩余：约 %.0f 分钟\n- 预计处理结束：%s（北京时间）\n'
        '- 最近 %.0f 分钟吞吐：约 %.0f 条/小时\n'
        '- 待处理：%d；有效：%d；跳过坏缓存：%d\n\n'
        '这是按近期吞吐估算的一次性提前提醒。处理完不等于训练数据全部可用：跳过项仍需核对修复，'
        '之后还需特征与 RNS 验收；不会自动启动 16 卡训练。\n\n%s')%(
            remaining/60,finish,seconds/60,rate*3600,q['pending'],q['valid'],q['skipped'],tag)
    try:
        number=0
        # Recover a successful POST even if its response/state save was lost.
        # The bounded list is queried only when the one-hour condition is met.
        if getattr(notifier,'github_configured',False):
            issues=notifier._api('GET','/repos/%s/issues?state=all&per_page=100'%notifier.repo)
            number=next((int(x['number']) for x in issues if tag in (x.get('body') or '')),0)
        if not number:number=notifier.create_issue('⏳ embedding 任务预计剩余约 1 小时',body)
        if number:
            rec.update(notified=True,notified_at=now,issue_number=number,status='notified')
            if persist:persist()
            logger('ETA reminder delivered once: issue=%s remaining_minutes=%.1f'%(number,remaining/60))
        else:rec['status']='notification_not_delivered_retry'
    except Exception as exc:
        rec.update(status='notification_failed_retry',error_type=type(exc).__name__)
        logger('ETA reminder delivery will retry: '+type(exc).__name__)
    return rec
