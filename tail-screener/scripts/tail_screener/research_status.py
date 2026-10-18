"""Evidence dimensions: obtaining text does not perform semantic review."""


def notice_status(report):
    review = report.get('review_status', {})
    if 'notice_bodies' in review:
        return review['notice_bodies']
    rows = report.get('candidates', [])
    if rows and all(r.get('body_review', {}).get('status') == 'complete' for r in rows):
        return 'mirror_bodies_collected'
    return review.get('notice_bodies', 'unverified')


def evidence_dimensions(report):
    run = report.get('run_status', {})
    metrics = report.get('forward_account', {}).get('metrics', {})
    quotes = report.get('short_term', {})
    checked=quotes.get('candidates',[])
    quote_state=('not_applicable' if not checked else 'outside_continuous_trading'
                 if quotes.get('phase')!='continuous_trading' else 'blocked'
                 if any(row.get('status')=='blocked' for row in checked) else 'passed_research_only'
                 if all(row.get('status')=='quote_checks_passed_research_only' for row in checked) else 'unverified')
    return {
        'execution': run.get('execution_status', 'unknown'),
        'required_data': run.get('data_status', 'unknown'),
        'notice_bodies': notice_status(report),
        'disclosure_semantics': report.get('review_status', {}).get('semantic_review', 'not_performed'),
        'account_valuation': ('conditional_inputs_complete' if metrics.get('valuation_complete') is True
                              else 'incomplete' if metrics.get('valuation_complete') is False else 'unverified'),
        'corporate_actions_audited': False,
        'quote_checks': quote_state,
        'strategy_profitability': 'not_validated',
        'real_fills': 'unverified',
        'recommendation_eligible': False}
