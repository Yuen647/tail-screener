"""A separately identified control basket; never relax the strict account."""
from copy import deepcopy

from .account_config import config_identity
from .account_journal import update_forward_account

POLICY='qualified_first_then_liquid_affordable_controls_v1'


def select_basket(scan, config, size=6):
    pool=scan.get('research_pool',scan.get('candidates',[]))
    selected=sorted(pool,key=lambda r:(not r.get('screen_passed',True),-r['mean_amount20'],-r['amount_yuan'],r['code']))[:size]
    strict_ranks={r['code']:r.get('rank') for r in scan.get('candidates',[])}
    rows=[]
    for index,original in enumerate(selected,1):
        row=deepcopy(original)
        row.update(rank=index,strict_rank=strict_ranks.get(row['code']),screen_passed=original.get('screen_passed',True),
                   screen_rejections=list(original.get('screen_rejections',[])),
                   entry_price_ceiling=original['price']*(1+config['max_entry_gap_pct']/100),
                   selection_kind='qualified' if original.get('screen_passed',True) else 'control')
        rows.append(row)
    return {'target_size':size,'candidates':rows,'vacancies':size-len(rows),'selection_policy':POLICY,
            'qualified_count':sum(r['screen_passed'] for r in rows),
            'control_count':sum(not r['screen_passed'] for r in rows),
            'vacancy_reason':'insufficient_verified_liquid_affordable_inputs' if len(rows)<size else None,
            'tie_policy':'capacity ties use code order only in the research control basket',
            'entry_policy':'frozen close-gap ceiling; MA state and MA20 premium are annotations, not entry gates',
            'performance_scope':'independent_control_not_strict_strategy_performance'}


def simulation_config(config,size):
    return {**config,'series_kind':'research_control','research_size':size,'selection_policy':POLICY,
            'entry_policy':'close_gap_only_no_ma20_premium_gate'}


def advance_research(store,basket,report,directory,*,next_session,action_loader=None,extra_blockers=()):
    config=simulation_config(report['account_config'],basket['target_size'])
    identity=config_identity(report['rule_sha256'],config)
    blockers=[i['code'] for i in report.get('data_issues',[]) if i['code'] in {
        'calendar_unverified','daily_session_mismatch','target_session_changed_during_run',
        'next_session_unverified','run_budget_exhausted'}]
    blockers.extend(extra_blockers)
    if not basket['candidates']:blockers.append('research_basket_empty')
    scan={'session':report['data_session'],'candidates':basket['candidates'],'stats':{
        'target_size':basket['target_size'],'selected':len(basket['candidates']),
        'qualified':basket['qualified_count'],'controls':basket['control_count'],'vacancies':basket['vacancies']}}
    evidence={'research_policy':{k:v for k,v in basket.items() if k!='candidates'},
              'review_blockers_annotated_not_bypassed_as_verified':deepcopy(report.get('data_issues',[])),
              'daily_refresh':report.get('daily_refresh'),'rule_sha256':report['rule_sha256'],
              'quotes':[r.get('quote') for r in basket['candidates']],
              'limit':'Missing held-security prices/actions remain unknown; no invented fills or dividends.'}
    forward=update_forward_account(store,scan,evidence,directory,next_session=next_session,
        entry_block_reasons=list(dict.fromkeys(blockers)),config=config,action_loader=action_loader)
    return {**basket,'status':'recorded','account_identity':identity,'account_config':config,
            'selection_scope':'partial_verified_subset' if any(i['code'] in {
                'catalog_incomplete','daily_refresh_incomplete','history_incomplete','quotes_incomplete',
                'daily_session_mismatch','calendar_unverified','empty_universe'} for i in report.get('data_issues',[])) else 'verified_current_universe',
            'review_issues':deepcopy(report.get('data_issues',[])),
            'valuation_status':'complete' if forward['metrics'].get('valuation_complete') else 'unverified',
            'forward_account':{k:v for k,v in forward.items() if k!='history_snapshots'}},forward
