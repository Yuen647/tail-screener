"""Search all screen-qualified candles; no alpha score or order allocation."""
GROUPS={'breakout':'收盘突破前5日高点','breakdown':'收盘跌破前5日低点',
        'failed_breakout':'上破前5日高点后收回','low_recovery':'跌破前5日低点后收回',
        'bull_body_engulf':'阳实体覆盖前一阴实体','bear_body_engulf':'阴实体覆盖前一阳实体',
        'inside':'内包日','outside':'外包日','upper_shadow':'上影占当日振幅至少一半',
        'lower_shadow':'下影占当日振幅至少一半','range':'收盘位于前5日高低区间内'}

def build(rows,session):
    records=[]
    for row in rows:
        k=row.get('kline_review') or {}
        groups=[key for key,label in GROUPS.items() if label==k.get('position') or label in k.get('patterns',[])] if k.get('status')=='computed' else []
        records.append({name:row.get(name) for name in ('code','display_code','price','rank','mean_amount20','amount_yuan','ma20_premium_pct')} | {
            'groups':groups,'kline_review':{key:value for key,value in k.items() if key not in ('candles','sessions')}})
    records.sort(key=lambda r:(-(r['mean_amount20'] or 0),-(r['amount_yuan'] or 0),r['code']))
    return {'schema_version':1,'session':session,'scope':'all_original_screen_qualified_rows_before_display_limit',
            'ranking':'liquidity_not_expected_return','individual_disclosures_reviewed':False,
            'count':len(records),'group_counts':{key:sum(key in r['groups'] for r in records) for key in GROUPS},
            'unknown':sum(r['kline_review'].get('status')!='computed' for r in records),'groups':GROUPS,'rows':records}

def query(report,group=None,code=None,limit=20):
    if type(limit) is not int or not 1<=limit<=500:raise ValueError('limit must be 1..500')
    if group is not None and group not in GROUPS:raise ValueError('Unknown candle group')
    catalog=report.get('kline_catalog')
    if not catalog:return {'status':'unavailable','reason':'source_report_has_no_candle_catalog','rows':[]}
    rows=[r for r in catalog.get('rows',[]) if (group is None or group in r.get('groups',[])) and (code is None or code.upper() in (r['code'],r.get('display_code'),r['code'][-6:]))]
    run=report.get('run_status') or {}
    complete=(bool(report.get('data_session')) and run.get('execution_status')=='completed' and run.get('data_status')=='complete'
              and report.get('data_session')==report.get('requested_session')==catalog.get('session')
              and not report.get('data_issues'))
    return {'status':'complete' if complete else 'partial','session':catalog.get('session'),'matched':len(rows),
            'returned':min(limit,len(rows)),'group':group,'ranking':catalog['ranking'],
            'individual_disclosures_reviewed':False,'rows':rows[:limit]}
