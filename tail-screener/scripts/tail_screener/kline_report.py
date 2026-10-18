"""Local SVG from verified numerical bars; no chart-image interpretation."""
from html import escape
import math

def render(review):
    if review.get('status')!='computed':return '<p>K线结构：完整日线不足或字段未核验。</p>'
    bars=review.get('candles',[])
    if len(bars)!=20:return '<p>K线图：缺少20根完整日线。</p>'
    try:
        low=min(float(b['low']) for b in bars);high=max(float(b['high']) for b in bars)
        if not all(math.isfinite(float(b[k])) for b in bars for k in ('open','high','low','close','amount')):raise ValueError()
    except (KeyError,TypeError,ValueError):return '<p>K线图：日线字段未核验。</p>'
    span=max(high-low,high*.02);bottom=low-span*.07;top=high+span*.07
    y=lambda price:18+(top-price)/(top-bottom)*150
    svg=['<svg viewBox="0 0 640 235" role="img" aria-label="最近20根完整日K线，红涨绿跌，悬停可读数值" style="width:100%;max-width:800px;background:#fafcff">']
    for price in (high,(high+low)/2,low):
        yy=y(price);svg.append(f'<line x1="46" x2="634" y1="{yy:.2f}" y2="{yy:.2f}" stroke="#dce3eb"/><text x="2" y="{yy+4:.2f}" font-size="11" fill="#526174">{price:.2f}</text>')
    amount_max=max(b['amount'] for b in bars)
    dates=review.get('sessions',[])
    for i,b in enumerate(bars):
        x=59+i*29;col='#c7353e' if b['close']>b['open'] else '#17866a' if b['close']<b['open'] else '#65748b'
        label=str(dates[i]) if len(dates)==20 else f'倒数第{20-i}根'
        tip=escape(f"{label} 开 {b['open']:.3f} 高 {b['high']:.3f} 低 {b['low']:.3f} 收 {b['close']:.3f} 成交额 {b['amount']/1e8:.2f}亿元")
        svg.append(f'<g><title>{tip}</title><line x1="{x}" x2="{x}" y1="{y(b["high"]):.2f}" y2="{y(b["low"]):.2f}" stroke="{col}"/><rect x="{x-8}" y="{min(y(b["open"]),y(b["close"])):.2f}" width="16" height="{max(1,abs(y(b["open"])-y(b["close"]))):.2f}" fill="{col}"/>')
        vol=30*b['amount']/amount_max if amount_max>0 else 0
        svg.append(f'<rect x="{x-8}" y="{211-vol:.2f}" width="16" height="{vol:.2f}" fill="{col}" opacity=".55"/></g>')
        if i in (0,9,19):svg.append(f'<text x="{x}" y="228" text-anchor="middle" font-size="10" fill="#526174">{escape(label)}</text>')
    svg.append('</svg>')
    pattern='；'.join(review.get('patterns',[])) or '没有命中已定义的包络、吞没或长影线条件'
    return ('<div class="kline-evidence">'+''.join(svg)+'<p><strong>K线结构：</strong>'+escape(review['position'])+'；'+escape(pattern)+'。</p>'
        +f'<p>实体占振幅 {review["body_range_ratio"]:.1%}；上影 {review["upper_shadow_ratio"]:.1%}；下影 {review["lower_shadow_ratio"]:.1%}；成交额 / 前5日均额 {review["amount_vs_previous5"]:.2f} 倍。前5日高低参考区间 {review["prior5_low"]:.3f}—{review["prior5_high"]:.3f} 元。</p>'
        +'<p class="meta">图中历史价格按信号日股价对齐复权回看；下方为成交额。参考区间不等于止损或目标价。</p></div>')
