"""Read-only reranking of an existing complete report and its exact snapshot."""
from datetime import date
from decimal import Decimal
import hashlib,json
from pathlib import Path
import numpy as np
from .account_strategy import scan_account
from .account_config import account_config
from .experimental_features import research_rows,FEATURES
from .experimental_hgb import FrozenHGB
from .market_integrity import BAR_FIELDS,bar_integrity_mask,deduplicate_source_codes
from .recent_daily import RecentDailyStore,_digest

ROOT=Path(__file__).resolve().parents[2]

def feature_window(store,session,budget_cents):
    last=store.session_index[session]
    if last<19:raise ValueError('fewer_than20_sessions')
    identities=deduplicate_source_codes(store.codes(session),session)
    codes=np.array([r['data_code'] for r in identities])
    stable=[r['stable_id'] for r in identities]
    fields={k:np.empty((20,len(codes)),dtype=np.float64) for k in BAR_FIELDS}
    valid=np.zeros((20,len(codes)),bool);active=np.zeros_like(valid)
    for j,code in enumerate(codes):
        f={k:store.values(code,k,last-19,last) for k in BAR_FIELDS}
        for k in BAR_FIELDS:fields[k][:,j]=f[k]
        valid[:,j]=bar_integrity_mask(f)['valid']
        age=last-np.searchsorted(store.sessions,min(a for a,b in store.intervals[code]))
        active[-1,j]=age>=252
    days=np.asarray(store.sessions[last-19:last+1])
    with np.errstate(invalid='ignore',divide='ignore'):
        idx,x,_,_,ma=research_rows(fields,valid,active,days,codes,19,budget_cents)
    if not np.isfinite(x).all():raise ValueError('nonfinite_experimental_features')
    complete=active[-1]&valid.all(axis=0)
    context={'active_symbols':int(active[-1].sum()),'complete_symbols':int(complete.sum()),
        'missing_symbols':[stable[j] for j in np.flatnonzero(active[-1]&~valid.all(axis=0))],
        'reference_pool_count':len(idx),'feature_names':FEATURES,
        'reference_pool':'pre-MA pool; 20-day amount>=10m, premium<=8%, current nominal budget affordable; market median/breadth uses all age-qualified complete windows'}
    return {stable[j]:(x[i].astype(np.float32),bool(ma[i])) for i,j in enumerate(idx)},context

def rank_report(report,store,*,top,model):
    cfg=report['account_config'];session=report['data_session']
    if type(top) is not int or not 1<=top<=50:raise ValueError('top_must_be1_to50')
    if type(cfg.get('initial_capital_cents')) is not int:raise ValueError('invalid_capital_cents')
    account_config(Decimal(cfg['initial_capital_cents'])/100,cfg['max_positions'],cfg['max_holding_sessions'],
                   max_entry_gap_pct=cfg['max_entry_gap_pct'],max_ma20_premium_pct=cfg['max_ma20_premium_pct'])
    if cfg.get('reserve_fraction')!=.05:raise ValueError('unsupported_reserve_fraction')
    budget=cfg['initial_capital_cents']*95//(100*cfg['max_positions'])
    output={'schema_version':1,'version':'3.5.0-hgb33-experimental','status':'unsupported_horizon',
        'data_session':session,'source_generated_at':report.get('generated_at'),
        'source_offline_preview':bool(report.get('offline_preview')),
        'requested_top':top,'configuration':dict(cfg),'nominal_budget_yuan':budget/100,
        'model':model.metadata,'candidates':[],'all_ranked':[],
        'recommendations':[],'account_changed':False,
        'score_semantics':'uncalibrated experimental regression score, not probability or promised current return',
        'disclosure_review':'not inherited from original top list; new symbols require individual review',
        'experiment_promotion_passed':False}
    if cfg['max_holding_sessions']!=model.metadata['training']['label_holding_sessions']:
        output['reason']='model_label_horizon_does_not_match_user_input';return output
    # Labels after the last training signal may exit later in the same year.
    # The recovered fold was first used in2021, never mid-December2020.
    if int(session[:4])<=model.metadata['training']['last_year']:
        raise ValueError('signal_not_after_training_year')
    scan=scan_account(store,session,report['diagnostics']['exclusions'],top=max(1,len(store.codes(session))),config=cfg)
    for key in ('universe','evaluated','bullish','incomplete','excluded','liquidity_excluded','nominal_lot_excluded','chase_excluded'):
        if scan['stats'][key]!=report['stats'][key]:raise ValueError('source_screen_not_reproduced:'+key)
    features,context=feature_window(store,session,budget)
    allrows=scan['kline_catalog']['rows'];strict_by_code={r['code']:r for r in scan['candidates']}
    names={r['code']:r.get('name',r['code']) for r in report.get('candidates',[])}
    names.update({r['code']:r.get('name',r['code']) for r in report.get('kline_catalog',{}).get('rows',[])})
    usable=[];unscored=[]
    for row in allrows:
        found=features.get(row['code'])
        if found is None or not found[1]:unscored.append({'code':row['code'],'reason':'outside_frozen_feature_pool'});continue
        usable.append((row,found[0]))
    scores=model.predict(np.array([x for _,x in usable],dtype=np.float32).reshape(-1,len(FEATURES)))
    ranked=[]
    for (row,x),score in zip(usable,scores):
        ranked.append({**row,'name':names.get(row['code'],row['code']),'liquidity_rank':row['rank'],
            'entry_price_ceiling':strict_by_code[row['code']]['entry_price_ceiling'],
            'holding_rule':strict_by_code[row['code']]['holding_rule'],
            'price_diagnostics':strict_by_code[row['code']]['price_diagnostics'],
            'experimental_score':float(score),'r1_pct':float(x[0]*100),'r5_pct':float(x[2]*100),
            'ma5_change_pct':float(x[30]*100),'amount5_over20':float(x[7]),
            'rank_reason':'score from frozen HGB33 model; displayed indicators describe price state and are not additive model attribution'})
    ranked.sort(key=lambda r:(-r['experimental_score'],-r['mean_amount20'],-r['amount_yuan'],r['code']))
    for i,row in enumerate(ranked,1):row['rank']=i
    original=[r['code'] for r in report.get('candidates',[])][:top]
    scope_match=budget==475000 and cfg['max_ma20_premium_pct']==8 and cfg['max_entry_gap_pct']==3
    output.update(status='computed' if not unscored else 'partial',
        candidates=ranked[:top],all_ranked=ranked,unscored=unscored,
        feature_context=context,strict_eligible_count=len(allrows),ranked_count=len(ranked),
        context_complete=not context['missing_symbols'],tested_budget_and_entry_scope_matches=scope_match,
        baseline_display_codes=original,
        overlap_with_baseline=len(set(original)&{r['code'] for r in ranked[:top]}),
        reason='existing_model_research_rerank; no positive-score gate; no new account allocation')
    return output

def load_and_rank(report_path,snapshot_path=None,*,top=None):
    report_path=Path(report_path);raw=report_path.read_bytes();report=json.loads(raw)
    status=report.get('run_status',{})
    session=report.get('data_session')
    if (status.get('execution_status')!='completed' or status.get('data_status')!='complete'
        or report.get('data_issues') or not session or report.get('requested_session')!=session):
        raise ValueError('source_report_not_complete')
    if date.fromisoformat(session).isoformat()!=session:raise ValueError('invalid_session')
    refresh=report.get('daily_refresh',{})
    if refresh.get('data_session')!=session:raise ValueError('daily_refresh_session_mismatch')
    path=Path(snapshot_path or refresh.get('snapshot_path',''))
    if not path.is_file():raise ValueError('exact_snapshot_required')
    envelope=json.loads(path.read_text());digest=_digest(envelope['data'])
    if digest!=envelope.get('sha256') or digest!=refresh.get('snapshot_sha256'):raise ValueError('snapshot_hash_mismatch')
    store=RecentDailyStore(envelope['data'],path.parent)
    if store.sessions[-1]!=session:raise ValueError('snapshot_session_mismatch')
    if top is None:top=report.get('effective_configuration',{}).get('parameters',{}).get('top',{}).get('value')
    if top is None:raise ValueError('explicit_top_required')
    model_path=ROOT/'models/hgb33-fold2021.json'
    evidence=json.loads((ROOT/'references/hgb33-validation.json').read_text())
    model_hash=hashlib.sha256(model_path.read_bytes()).hexdigest()
    if model_hash!=evidence['model_recovery']['sha256']:raise ValueError('model_hash_mismatch')
    output=rank_report(report,store,top=top,model=FrozenHGB.load(model_path))
    output['source']={'report_sha256':hashlib.sha256(raw).hexdigest(),'snapshot_sha256':digest,'model_sha256':model_hash}
    output['historical_comparison']=evidence['historical_comparison']
    return output
